"""Durable, owner-fenced child-supervisor lifecycle state."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
import hashlib
from threading import RLock
from typing import Any
from uuid import uuid4

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
_STATE_SCHEMA = "newsroom.harness-child-lifecycle-state/v2"
_MAX_CAS_RETRIES = 16
_RESERVED_EVENTS_PER_CHILD = 3


def _serialized(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return invoke


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _utc(value: datetime, field_name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ChildLifecycleOwnerToken:
    owner_id: str
    generation: int
    lease_id: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner_id", _required_text(self.owner_id, "owner_id"))
        object.__setattr__(self, "lease_id", _required_text(self.lease_id, "lease_id"))
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 1
        ):
            raise ValueError("generation must be positive")
        issued_at = _utc(self.issued_at, "issued_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if expires_at <= issued_at:
            raise ValueError("expires_at must be after issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)

    def is_expired(self, now: datetime) -> bool:
        return _utc(now, "now") >= self.expires_at

    def to_dict(self) -> dict[str, object]:
        return {
            "owner_id": self.owner_id,
            "generation": self.generation,
            "lease_id": self.lease_id,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ChildLifecycleOwnerToken":
        return cls(
            owner_id=value["owner_id"],  # type: ignore[arg-type]
            generation=value["generation"],  # type: ignore[arg-type]
            lease_id=value["lease_id"],  # type: ignore[arg-type]
            issued_at=datetime.fromisoformat(value["issued_at"]),  # type: ignore[arg-type]
            expires_at=datetime.fromisoformat(
                value["expires_at"],
            ),  # type: ignore[arg-type]
        )


class DurableChildAgentEventLog:
    """Shared lifecycle event log and admission-owner authority.

    The owner, run-to-tenant bindings, events, and terminal reservations all
    live in one canonical transactional-state record.  A replacement owner
    may take over an expired lease, but active/unknown children remain durable
    capacity occupants until a confirmed terminal fact is committed.
    """

    def __init__(
        self,
        *,
        state_runtime: TransactionalStateRuntimePort,
        state_reader: TransactionalStateReaderPort,
        state_key: str,
        max_events: int = 10_000,
        max_state_bytes: int = 16 * 1024 * 1024,
        max_event_bytes: int = 128 * 1024,
        owner_lease_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(state_runtime, TransactionalStateRuntimePort):
            raise TypeError(
                "state_runtime must implement TransactionalStateRuntimePort"
            )
        if not isinstance(state_reader, TransactionalStateReaderPort):
            raise TypeError("state_reader must implement TransactionalStateReaderPort")
        self._state_key = _required_text(state_key, "state_key")
        if (
            isinstance(max_events, bool)
            or not isinstance(max_events, int)
            or max_events < 4
        ):
            raise ValueError("max_events must be at least 4")
        if (
            isinstance(max_state_bytes, bool)
            or not isinstance(max_state_bytes, int)
            or max_state_bytes < 1024
        ):
            raise ValueError("max_state_bytes must be at least 1024")
        if (
            isinstance(max_event_bytes, bool)
            or not isinstance(max_event_bytes, int)
            or max_event_bytes < 1024
        ):
            raise ValueError("max_event_bytes must be at least 1024")
        if max_event_bytes * _RESERVED_EVENTS_PER_CHILD >= max_state_bytes:
            raise ValueError("max_state_bytes must exceed one terminal reservation")
        if (
            isinstance(owner_lease_seconds, bool)
            or not isinstance(owner_lease_seconds, (int, float))
            or not 0 < owner_lease_seconds <= 3600
        ):
            raise ValueError("owner_lease_seconds must be in (0, 3600]")
        self._runtime = state_runtime
        self._reader = state_reader
        self._max_events = max_events
        self._max_state_bytes = max_state_bytes
        self._max_event_bytes = max_event_bytes
        self._owner_lease_seconds = float(owner_lease_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owner_token: ChildLifecycleOwnerToken | None = None
        self._lock = RLock()

    @property
    def owner_token(self) -> ChildLifecycleOwnerToken | None:
        return self._owner_token

    @property
    def max_event_bytes(self) -> int:
        return self._max_event_bytes

    @property
    def owner_lease_seconds(self) -> float:
        return self._owner_lease_seconds

    @_serialized
    def acquire_owner(self, owner_id: str) -> ChildLifecycleOwnerToken:
        normalized_owner = _required_text(owner_id, "owner_id")
        if self._owner_token is not None:
            if normalized_owner != self._owner_token.owner_id:
                raise ChildAgentOperationConflict(
                    "event log is bound to another owner", code="child_owner_conflict"
                )
            self.assert_owner(self._owner_token)
            return self._owner_token
        for _ in range(_MAX_CAS_RETRIES):
            now = _utc(self._clock(), "clock")
            current = self._load_snapshot()
            state = self._validated_state(current)
            incumbent = state["owner"]
            if incumbent is not None and not incumbent.is_expired(now):
                raise ChildAgentOperationConflict(
                    "child lifecycle scope already has a live owner",
                    code="child_owner_conflict",
                )
            token = ChildLifecycleOwnerToken(
                owner_id=normalized_owner,
                generation=state["generation"] + 1,
                lease_id=f"child-owner-{uuid4().hex}",
                issued_at=now,
                expires_at=now + timedelta(seconds=self._owner_lease_seconds),
            )
            state["owner"] = token
            state["generation"] = token.generation
            self._assert_capacity(state)
            if self._commit(current, state):
                self._owner_token = token
                return token
        self._contention()

    @_serialized
    def renew_owner(
        self, token: ChildLifecycleOwnerToken | None = None
    ) -> ChildLifecycleOwnerToken:
        expected = token or self._require_local_owner()
        for _ in range(_MAX_CAS_RETRIES):
            now = _utc(self._clock(), "clock")
            current = self._load_snapshot()
            state = self._validated_state(current)
            self._verify_owner(state["owner"], expected, now=now)
            renewed = ChildLifecycleOwnerToken(
                expected.owner_id,
                expected.generation,
                expected.lease_id,
                now,
                now + timedelta(seconds=self._owner_lease_seconds),
            )
            state["owner"] = renewed
            if self._commit(current, state):
                self._owner_token = renewed
                return renewed
        self._contention()

    @_serialized
    def assert_owner(
        self, token: ChildLifecycleOwnerToken | None = None
    ) -> ChildLifecycleOwnerToken:
        expected = token or self._require_local_owner()
        state = self._validated_state(self._load_snapshot())
        self._verify_owner(state["owner"], expected, now=_utc(self._clock(), "clock"))
        return expected

    @_serialized
    def release_owner(self, token: ChildLifecycleOwnerToken | None = None) -> bool:
        expected = token or self._require_local_owner()
        for _ in range(_MAX_CAS_RETRIES):
            now = _utc(self._clock(), "clock")
            current = self._load_snapshot()
            state = self._validated_state(current)
            self._verify_owner(state["owner"], expected, now=now)
            state["owner"] = None
            if self._commit(current, state):
                self._owner_token = None
                return True
        self._contention()

    @_serialized
    def register_run_scope(self, run_id: str, tenant_id: str) -> str:
        normalized_run = _required_text(run_id, "run_id")
        normalized_tenant = _required_text(tenant_id, "tenant_id")
        for _ in range(_MAX_CAS_RETRIES):
            current = self._load_snapshot()
            state = self._validated_owned_state(current)
            previous = state["run_scopes"].get(normalized_run)
            if previous is not None:
                if previous != normalized_tenant:
                    raise ChildAgentOperationConflict(
                        "run scope is already bound to another tenant",
                        code="child_run_scope_conflict",
                    )
                return previous
            state["run_scopes"][normalized_run] = normalized_tenant
            self._assert_capacity(state)
            if self._commit(current, state):
                return normalized_tenant
        self._contention()

    @_serialized
    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        item = _event(event)
        event_type = item.get("event_type")
        operation_id = _required_text(item.get("operation_id"), "operation_id")
        for _ in range(_MAX_CAS_RETRIES):
            current = self._load_snapshot()
            state = self._validated_owned_state(current)
            item = self._bind_tenant(item, state["run_scopes"])
            previous = next(
                (
                    candidate
                    for candidate in state["events"]
                    if candidate["event_id"] == item["event_id"]
                ),
                None,
            )
            if previous is not None:
                if stable_json_dumps(previous) != stable_json_dumps(item):
                    raise ChildAgentOperationConflict(
                        "child lifecycle event identity has conflicting content",
                        code="event_identity_conflict",
                    )
                return dict(previous)
            item_bytes = len(stable_json_dumps(item).encode("utf-8"))
            if item_bytes > self._max_event_bytes:
                raise ChildAgentSupervisorError(
                    "child lifecycle event exceeds its byte limit",
                    code="child_event_too_large",
                )
            reservations = state["terminal_reservations"]
            if event_type == "child_spawned":
                if item_bytes > self._max_event_bytes // 2:
                    raise ChildAgentSupervisorError(
                        "child admission leaves insufficient terminal envelope space",
                        code="child_event_too_large",
                    )
                if any(
                    candidate.get("child_id") == item.get("child_id")
                    or candidate.get("operation_id") == operation_id
                    for candidate in state["events"]
                ):
                    raise ChildAgentOperationConflict(
                        "child admission identity is already used",
                        code="operation_identity_conflict",
                    )
                if operation_id in reservations:
                    raise ChildAgentOperationConflict(
                        "child operation already has a terminal reservation",
                        code="operation_identity_conflict",
                    )
                reservations[operation_id] = {
                    "remaining_events": _RESERVED_EVENTS_PER_CHILD,
                    "remaining_bytes": self._max_event_bytes
                    * _RESERVED_EVENTS_PER_CHILD,
                }
            elif event_type in {
                "child_cancel_requested",
                "child_terminal",
                "child_closed",
            }:
                reservation = reservations.get(operation_id)
                if reservation is None or reservation["remaining_events"] < 1:
                    raise ChildAgentSupervisorError(
                        "child terminal storage was not reserved",
                        code="child_terminal_reservation_missing",
                    )
                reservation["remaining_events"] -= 1
                reservation["remaining_bytes"] -= self._max_event_bytes
                if event_type == "child_terminal":
                    reservation["remaining_events"] = 1
                    reservation["remaining_bytes"] = self._max_event_bytes
                if event_type == "child_closed" or reservation["remaining_events"] == 0:
                    reservations.pop(operation_id, None)
            state["events"].append(item)
            try:
                projected = _lifecycle_reservations(state["events"])
            except (TypeError, ValueError) as exc:
                raise ChildAgentOperationConflict(
                    "child lifecycle event conflicts with committed history",
                    code="child_lifecycle_conflict",
                ) from exc
            if projected != {
                key: value["remaining_events"]
                for key, value in reservations.items()
            }:
                raise ChildAgentOperationConflict(
                    "child lifecycle event conflicts with terminal reservations",
                    code="child_lifecycle_conflict",
                )
            self._assert_capacity(state)
            if self._commit(current, state):
                return dict(item)
        self._contention()

    @_serialized
    def read_events(self) -> tuple[dict[str, Any], ...]:
        try:
            state = self._validated_state(self._load_snapshot())
            return tuple(dict(event) for event in state["events"])
        except ChildAgentSupervisorError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise ChildAgentSupervisorError(
                "child lifecycle durable read failed",
                code="child_event_store_unavailable",
            ) from exc

    def for_child(self, child_id: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            event for event in self.read_events() if event.get("child_id") == child_id
        )

    def _bind_tenant(
        self, item: Mapping[str, Any], run_scopes: Mapping[str, str]
    ) -> dict[str, Any]:
        identity = item.get("parent_graph_identity")
        if not isinstance(identity, Mapping):
            raise ChildAgentSupervisorError(
                "child lifecycle event has no parent Graph identity",
                code="child_run_scope_missing",
            )
        run_id = identity.get("run_id")
        tenant_id = run_scopes.get(run_id.strip()) if isinstance(run_id, str) else None
        if tenant_id is None:
            raise ChildAgentSupervisorError(
                "child run is not registered in the admission scope",
                code="child_run_scope_unregistered",
            )
        supplied = item.get("tenant_id")
        if supplied is not None and supplied != tenant_id:
            raise ChildAgentOperationConflict(
                "child lifecycle event tenant does not match trusted scope",
                code="child_run_scope_conflict",
            )
        bound = dict(item)
        bound["tenant_id"] = tenant_id
        return bound

    def _load_snapshot(self) -> TransactionalStateSnapshot | None:
        try:
            return self._reader.load_transactional_state(
                CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE, self._state_key
            )
        except ChildAgentSupervisorError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise ChildAgentSupervisorError(
                "child lifecycle durable read failed",
                code="child_event_store_unavailable",
            ) from exc

    def _validated_owned_state(
        self, snapshot: TransactionalStateSnapshot | None
    ) -> dict[str, Any]:
        state = self._validated_state(snapshot)
        self._verify_owner(
            state["owner"],
            self._require_local_owner(),
            now=_utc(self._clock(), "clock"),
        )
        return state

    def _validated_state(
        self, snapshot: TransactionalStateSnapshot | None
    ) -> dict[str, Any]:
        if snapshot is None:
            if self._owner_token is not None:
                raise ChildAgentSupervisorError(
                    "owned child lifecycle state disappeared",
                    code="child_event_store_corrupt",
                )
            return {
                "owner": None,
                "generation": 0,
                "run_scopes": {},
                "events": [],
                "terminal_reservations": {},
            }
        payload = thaw_canonical_json(snapshot.payload)
        if payload.get("schema_version") != _STATE_SCHEMA:
            raise ChildAgentSupervisorError(
                "child lifecycle durable state schema is unsupported",
                code="child_event_store_corrupt",
            )
        raw_owner, raw_scopes = payload.get("owner"), payload.get("run_scopes")
        raw_events, raw_reservations = payload.get("events"), payload.get(
            "terminal_reservations"
        )
        if (
            not isinstance(raw_scopes, Mapping)
            or not isinstance(raw_events, list)
            or not isinstance(raw_reservations, Mapping)
        ):
            raise ChildAgentSupervisorError(
                "child lifecycle durable state structure is invalid",
                code="child_event_store_corrupt",
            )
        try:
            owner = (
                None
                if raw_owner is None
                else ChildLifecycleOwnerToken.from_dict(raw_owner)
            )
            generation = payload.get("generation")
            if (
                isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
                or (owner is not None and owner.generation != generation)
            ):
                raise ValueError("invalid owner generation")
            run_scopes = {
                _required_text(run_id, "run_id"): _required_text(tenant_id, "tenant_id")
                for run_id, tenant_id in raw_scopes.items()
            }
            events = [_event(item) for item in raw_events]
            reservations: dict[str, dict[str, int]] = {}
            for operation_id, value in raw_reservations.items():
                if not isinstance(value, Mapping):
                    raise ValueError("terminal reservation must be an object")
                remaining_events, remaining_bytes = value.get(
                    "remaining_events"
                ), value.get("remaining_bytes")
                if (
                    isinstance(remaining_events, bool)
                    or not isinstance(remaining_events, int)
                    or not 1 <= remaining_events <= _RESERVED_EVENTS_PER_CHILD
                    or remaining_bytes != remaining_events * self._max_event_bytes
                ):
                    raise ValueError("invalid terminal reservation")
                reservations[_required_text(operation_id, "operation_id")] = {
                    "remaining_events": remaining_events,
                    "remaining_bytes": remaining_bytes,
                }
        except (TypeError, ValueError, KeyError) as exc:
            raise ChildAgentSupervisorError(
                "child lifecycle durable state contains invalid data",
                code="child_event_store_corrupt",
            ) from exc
        if (
            payload.get("event_count") != len(events)
            or len({event["event_id"] for event in events}) != len(events)
            or payload.get("history_checksum") != _history_checksum(events)
        ):
            raise ChildAgentSupervisorError(
                "child lifecycle durable history checksum is invalid",
                code="child_event_store_corrupt",
            )
        for event in events:
            bound = self._bind_tenant(event, run_scopes)
            if bound != event:
                raise ChildAgentSupervisorError(
                    "child event has no trusted tenant",
                    code="child_event_store_corrupt",
                )
        try:
            expected_reservations = _lifecycle_reservations(events)
        except (TypeError, ValueError) as exc:
            raise ChildAgentSupervisorError(
                "child lifecycle history has an invalid transition",
                code="child_event_store_corrupt",
            ) from exc
        if {key: value for key, value in expected_reservations.items() if value} != {
            key: value["remaining_events"] for key, value in reservations.items()
        }:
            raise ChildAgentSupervisorError(
                "terminal reservations conflict with history",
                code="child_event_store_corrupt",
            )
        state = {
            "owner": owner,
            "generation": generation,
            "run_scopes": run_scopes,
            "events": events,
            "terminal_reservations": reservations,
        }
        self._assert_capacity(state)
        return state

    def _assert_capacity(self, state: Mapping[str, Any]) -> None:
        reserved_events = sum(
            item["remaining_events"] for item in state["terminal_reservations"].values()
        )
        if len(state["events"]) + reserved_events > self._max_events:
            raise ChildAgentSupervisorError(
                "child lifecycle durable history is full",
                code="child_event_store_capacity_exhausted",
            )
        reserved_bytes = sum(
            item["remaining_bytes"] for item in state["terminal_reservations"].values()
        )
        if (
            len(stable_json_dumps(self._payload(state)).encode("utf-8"))
            + reserved_bytes
            + 1024
            > self._max_state_bytes
        ):
            raise ChildAgentSupervisorError(
                "child lifecycle durable history exceeds its byte limit",
                code="child_event_store_capacity_exhausted",
            )

    def _commit(
        self, current: TransactionalStateSnapshot | None, state: Mapping[str, Any]
    ) -> bool:
        next_snapshot = TransactionalStateSnapshot.create(
            namespace=CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
            key=self._state_key,
            revision=1 if current is None else current.revision + 1,
            payload=self._payload(state),
        )
        try:
            self._runtime.compare_and_swap_transactional_state(
                next_snapshot,
                expected_revision=None if current is None else current.revision,
                expected_checksum=None if current is None else current.checksum,
            )
        except EventStoreContentionError:
            return False
        except ChildAgentSupervisorError:
            raise
        except BaseException as exc:  # noqa: BLE001
            raise ChildAgentSupervisorError(
                "child lifecycle durable CAS failed",
                code="child_event_store_unavailable",
            ) from exc
        return True

    @staticmethod
    def _payload(state: Mapping[str, Any]) -> dict[str, Any]:
        events = list(state["events"])
        owner = state["owner"]
        return {
            "schema_version": _STATE_SCHEMA,
            "owner": None if owner is None else owner.to_dict(),
            "generation": state["generation"],
            "run_scopes": dict(state["run_scopes"]),
            "event_count": len(events),
            "history_checksum": _history_checksum(events),
            "events": events,
            "terminal_reservations": {
                key: dict(value)
                for key, value in state["terminal_reservations"].items()
            },
        }

    def _require_local_owner(self) -> ChildLifecycleOwnerToken:
        if self._owner_token is None:
            raise ChildAgentSupervisorError(
                "child lifecycle admission scope has not been started",
                code="child_owner_not_started",
            )
        return self._owner_token

    @staticmethod
    def _verify_owner(
        incumbent: ChildLifecycleOwnerToken | None,
        expected: ChildLifecycleOwnerToken,
        *,
        now: datetime,
    ) -> None:
        if (
            incumbent is None
            or (incumbent.owner_id, incumbent.generation, incumbent.lease_id)
            != (expected.owner_id, expected.generation, expected.lease_id)
            or incumbent.is_expired(now)
        ):
            raise ChildAgentOperationConflict(
                "child lifecycle owner fencing token is no longer valid",
                code="child_owner_lost",
            )

    @staticmethod
    def _contention() -> None:
        raise ChildAgentSupervisorError(
            "child lifecycle durable CAS did not converge",
            code="child_event_store_contention",
        )


def _lifecycle_reservations(events: list[dict[str, Any]]) -> dict[str, int]:
    """Project valid, one-way child transitions onto reserved terminal slots."""
    progress: dict[str, dict[str, Any]] = {}
    for event in events:
        operation_id = _required_text(event.get("operation_id"), "operation_id")
        child_id = _required_text(event.get("child_id"), "child_id")
        kind = event.get("event_type")
        if kind == "child_spawned":
            if operation_id in progress:
                raise ValueError("child operation has multiple admissions")
            progress[operation_id] = {
                "child_id": child_id,
                "remaining": _RESERVED_EVENTS_PER_CHILD,
                "cancel_requested": False,
                "terminal": False,
                "closed": False,
            }
            continue
        current = progress.get(operation_id)
        if current is None or current["child_id"] != child_id:
            raise ValueError("child lifecycle event lacks its own admission")
        if current["closed"] or (current["terminal"] and kind != "child_closed"):
            raise ValueError("child lifecycle event follows a terminal transition")
        if kind == "child_cancel_requested":
            if current["cancel_requested"] or current["remaining"] < 2:
                raise ValueError("child cancellation was already requested")
            current["cancel_requested"] = True
            current["remaining"] -= 1
        elif kind == "child_terminal":
            current["terminal"] = True
            current["remaining"] = 1
        elif kind == "child_closed":
            if not current["terminal"]:
                raise ValueError("child closed before terminal receipt")
            current["closed"] = True
            current["remaining"] = 0
        elif kind not in {"child_status", "child_heartbeat", "child_boundary_violation"}:
            raise ValueError("unknown child lifecycle event")
    return {
        operation_id: current["remaining"]
        for operation_id, current in progress.items()
        if current["remaining"]
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
    return (
        "sha256:"
        + hashlib.sha256(stable_json_dumps(events).encode("utf-8")).hexdigest()
    )


__all__ = [
    "CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE",
    "ChildLifecycleOwnerToken",
    "DurableChildAgentEventLog",
]
