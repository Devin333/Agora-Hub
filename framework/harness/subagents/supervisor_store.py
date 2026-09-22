"""Durable child-supervisor lifecycle storage over canonical state CAS."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from framework.events import (
    EventStoreContentionError,
    TransactionalStateReaderPort,
    TransactionalStateRuntimePort,
    TransactionalStateSnapshot,
    thaw_canonical_json,
)
from framework.harness.subagents.supervisor import (
    ChildAgentOperationConflict,
    ChildAgentSupervisorError,
)
from framework.shared.json import stable_json_dumps


CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE = "newsroom.harness-child-lifecycle/v1"
_STATE_SCHEMA = "newsroom.harness-child-lifecycle-state/v1"
_MAX_CAS_RETRIES = 16


class DurableChildAgentEventLog:
    """Checksum-bound lifecycle history shared across supervisor processes.

    The caller chooses a stable ``state_key`` for one supervision scope, such
    as a Harness run. SQLite and PostgreSQL provide the same CAS semantics via
    the existing event runtime ports, so lifecycle recovery does not introduce
    another persistence authority.
    """

    def __init__(
        self,
        *,
        state_runtime: TransactionalStateRuntimePort,
        state_reader: TransactionalStateReaderPort,
        state_key: str,
        max_events: int = 10_000,
        max_state_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if not isinstance(state_runtime, TransactionalStateRuntimePort):
            raise TypeError("state_runtime must implement TransactionalStateRuntimePort")
        if not isinstance(state_reader, TransactionalStateReaderPort):
            raise TypeError("state_reader must implement TransactionalStateReaderPort")
        normalized_key = str(state_key).strip()
        if not normalized_key:
            raise ValueError("state_key is required")
        if isinstance(max_events, bool) or not isinstance(max_events, int) or max_events < 1:
            raise ValueError("max_events must be a positive integer")
        if (
            isinstance(max_state_bytes, bool)
            or not isinstance(max_state_bytes, int)
            or max_state_bytes < 1024
        ):
            raise ValueError("max_state_bytes must be at least 1024")
        self._runtime = state_runtime
        self._reader = state_reader
        self._state_key = normalized_key
        self._max_events = max_events
        self._max_state_bytes = max_state_bytes

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        item = _event(event)
        for _ in range(_MAX_CAS_RETRIES):
            current = self._reader.load_transactional_state(
                CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
                self._state_key,
            )
            events = list(self._validated_events(current))
            previous = next(
                (candidate for candidate in events if candidate["event_id"] == item["event_id"]),
                None,
            )
            if previous is not None:
                if stable_json_dumps(previous) != stable_json_dumps(item):
                    raise ChildAgentOperationConflict(
                        "child lifecycle event identity has conflicting content",
                        code="event_identity_conflict",
                    )
                return dict(previous)
            if len(events) >= self._max_events:
                raise ChildAgentSupervisorError(
                    "child lifecycle durable history is full",
                    code="child_event_store_capacity_exhausted",
                )
            events.append(item)
            payload = self._payload(events)
            if len(stable_json_dumps(payload).encode("utf-8")) > self._max_state_bytes:
                raise ChildAgentSupervisorError(
                    "child lifecycle durable history exceeds its byte limit",
                    code="child_event_store_capacity_exhausted",
                )
            next_snapshot = TransactionalStateSnapshot.create(
                namespace=CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
                key=self._state_key,
                revision=1 if current is None else current.revision + 1,
                payload=payload,
            )
            try:
                self._runtime.compare_and_swap_transactional_state(
                    next_snapshot,
                    expected_revision=None if current is None else current.revision,
                    expected_checksum=None if current is None else current.checksum,
                )
            except EventStoreContentionError:
                continue
            except ChildAgentSupervisorError:
                raise
            except BaseException as exc:  # noqa: BLE001 - durable boundary
                raise ChildAgentSupervisorError(
                    "child lifecycle durable append failed",
                    code="child_event_store_unavailable",
                ) from exc
            return dict(item)
        raise ChildAgentSupervisorError(
            "child lifecycle durable append CAS did not converge",
            code="child_event_store_contention",
        )

    def read_events(self) -> tuple[dict[str, Any], ...]:
        try:
            current = self._reader.load_transactional_state(
                CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
                self._state_key,
            )
            return tuple(dict(event) for event in self._validated_events(current))
        except ChildAgentSupervisorError:
            raise
        except BaseException as exc:  # noqa: BLE001 - durable boundary
            raise ChildAgentSupervisorError(
                "child lifecycle durable read failed",
                code="child_event_store_unavailable",
            ) from exc

    def for_child(self, child_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            event for event in self.read_events() if event.get("child_id") == child_id
        )

    def _validated_events(
        self,
        snapshot: TransactionalStateSnapshot | None,
    ) -> tuple[dict[str, Any], ...]:
        if snapshot is None:
            return ()
        payload = thaw_canonical_json(snapshot.payload)
        if payload.get("schema_version") != _STATE_SCHEMA:
            raise ChildAgentSupervisorError(
                "child lifecycle durable state schema is unsupported",
                code="child_event_store_corrupt",
            )
        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            raise ChildAgentSupervisorError(
                "child lifecycle durable state has no event list",
                code="child_event_store_corrupt",
            )
        try:
            events = tuple(_event(item) for item in raw_events)
        except (TypeError, ValueError) as exc:
            raise ChildAgentSupervisorError(
                "child lifecycle durable state contains an invalid event",
                code="child_event_store_corrupt",
            ) from exc
        if payload.get("event_count") != len(events):
            raise ChildAgentSupervisorError(
                "child lifecycle durable event count is invalid",
                code="child_event_store_corrupt",
            )
        event_ids = tuple(event["event_id"] for event in events)
        if len(event_ids) != len(set(event_ids)):
            raise ChildAgentSupervisorError(
                "child lifecycle durable event identities are not unique",
                code="child_event_store_corrupt",
            )
        if payload.get("history_checksum") != _history_checksum(events):
            raise ChildAgentSupervisorError(
                "child lifecycle durable history checksum is invalid",
                code="child_event_store_corrupt",
            )
        return events

    @staticmethod
    def _payload(events: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "schema_version": _STATE_SCHEMA,
            "event_count": len(events),
            "history_checksum": _history_checksum(events),
            "events": events,
        }


def _event(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("child lifecycle event must be an object")
    item = thaw_canonical_json(value)
    event_id = item.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("child lifecycle event_id is required")
    stable_json_dumps(item)
    return dict(item)


def _history_checksum(events: object) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(
        stable_json_dumps(events).encode("utf-8")
    ).hexdigest()


__all__ = [
    "CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE",
    "DurableChildAgentEventLog",
]
