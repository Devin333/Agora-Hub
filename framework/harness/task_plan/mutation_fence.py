"""Bounded resource fence history; only a durable authority may commit transitions.

Expiry is evidence of uncertain ownership, never permission to repeat a write.
The pure history reducer is shared by authority adapters and offline review.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    identifier,
)


MUTATION_FENCE_SCHEMA = "agora.task-mutation-fence/v1"
MAX_FENCE_HISTORY = 1024
_EVENT_FIELDS = frozenset(
    {
        "operation_key",
        "action",
        "owner_id",
        "generation",
        "at_ms",
        "expires_at_ms",
        "termination_confirmed",
        "revision",
        "previous_checksum",
        "event_checksum",
    }
)
_FENCE_ACTIONS = frozenset(
    {
        "ACQUIRED",
        "RENEWED",
        "RELEASED",
        "LOST",
        "RECOVERED_ACTIVE",
        "RECOVERED_RELEASED",
    }
)


class MutationFenceState(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    INDETERMINATE = "INDETERMINATE"


def _fail(message: str, code: str = "SIDE_EFFECT_FENCE_INVALID") -> None:
    raise HarnessValidationError(message, code=code)


def _int(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        _fail(f"{name} must be an integer >= {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class MutationFence:
    owner_scope: str
    resource_key: str
    history: tuple[Mapping[str, Any], ...]
    schema_version: str = MUTATION_FENCE_SCHEMA
    fence_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        from framework.harness.task_plan.canonical import frozen_mapping
        identifier(self.owner_scope, "owner_scope")
        identifier(self.resource_key, "resource_key")
        if self.schema_version != MUTATION_FENCE_SCHEMA:
            _fail("unsupported resource fence schema")
        if not isinstance(self.history, (tuple, list)) or not 1 <= len(
            self.history
        ) <= MAX_FENCE_HISTORY:
            _fail("resource fence history must contain 1..1024 events")
        normalized = []
        previous = None
        keys = set()
        state = None
        for revision, raw in enumerate(self.history, start=1):
            event = exact_keys(raw, required=_EVENT_FIELDS, model="MutationFenceEvent")
            identifier(event["operation_key"], "operation_key")
            identifier(event["owner_id"], "owner_id")
            if event["operation_key"] in keys:
                _fail("duplicate fence operation key")
            keys.add(event["operation_key"])
            if _int(event["revision"], "revision", 1) != revision:
                _fail("fence history revisions are not contiguous")
            _int(event["generation"], "generation", 1)
            _int(event["at_ms"], "at_ms")
            _int(event["expires_at_ms"], "expires_at_ms", 1)
            if not isinstance(event["termination_confirmed"], bool):
                _fail("fence termination confirmation must be boolean")
            action = event["action"]
            if not isinstance(action, str) or action not in _FENCE_ACTIONS:
                _fail("unknown fence action")
            previous_checksum = event["previous_checksum"]
            if previous is None:
                if previous_checksum is not None:
                    _fail("first fence event must not reference a previous checksum")
            else:
                try:
                    previous_checksum = checksum(previous_checksum, "previous_checksum")
                except HarnessValidationError:
                    _fail("fence previous checksum is invalid")
                if previous_checksum != previous["event_checksum"]:
                    _fail("fence history checksum chain is invalid")
            supplied = checksum(event["event_checksum"], "event_checksum")
            expected = self._event_checksum(
                {
                    key: value
                    for key, value in event.items()
                    if key != "event_checksum"
                }
            )
            if supplied != expected:
                _fail("fence history checksum chain is invalid")
            if previous is not None and event["at_ms"] < previous["at_ms"]:
                _fail("fence time cannot move backwards")
            if action == "ACQUIRED":
                if previous is not None and state is not MutationFenceState.RELEASED:
                    _fail("fence cannot be acquired until the previous owner is confirmed released")
                expected_generation = previous["generation"] + 1 if previous else 1
                if (
                    event["generation"] != expected_generation
                    or event["expires_at_ms"] <= event["at_ms"]
                    or event["termination_confirmed"]
                ):
                    _fail("invalid fence acquisition generation or TTL")
                state = MutationFenceState.ACTIVE
            else:
                if (
                    previous is None
                    or event["owner_id"] != previous["owner_id"]
                    or event["generation"] != previous["generation"]
                ):
                    _fail("fence owner or generation changed without acquisition")
                if action == "RENEWED":
                    if (
                        state is not MutationFenceState.ACTIVE
                        or event["at_ms"] >= previous["expires_at_ms"]
                        or event["expires_at_ms"] <= previous["expires_at_ms"]
                        or event["termination_confirmed"]
                    ):
                        _fail("expired, lost or released fences cannot renew")
                elif action in {"RELEASED", "RECOVERED_RELEASED"}:
                    if (
                        state is MutationFenceState.RELEASED
                        or not event["termination_confirmed"]
                        or event["expires_at_ms"] != previous["expires_at_ms"]
                    ):
                        _fail("fence release requires current ownership and confirmed termination")
                    if action == "RELEASED" and (
                        state is not MutationFenceState.ACTIVE
                        or event["at_ms"] >= previous["expires_at_ms"]
                    ):
                        _fail("uncertain fence requires audited recovery")
                    if (
                        action == "RECOVERED_RELEASED"
                        and state is not MutationFenceState.INDETERMINATE
                    ):
                        _fail("recovered release requires an indeterminate fence")
                    state = MutationFenceState.RELEASED
                elif action == "LOST":
                    if (
                        state is not MutationFenceState.ACTIVE
                        or event["expires_at_ms"] != previous["expires_at_ms"]
                        or event["termination_confirmed"]
                    ):
                        _fail("invalid fence loss transition")
                    state = MutationFenceState.INDETERMINATE
                elif action == "RECOVERED_ACTIVE":
                    if (
                        state is not MutationFenceState.INDETERMINATE
                        or event["at_ms"] >= previous["expires_at_ms"]
                        or event["expires_at_ms"] != previous["expires_at_ms"]
                        or event["termination_confirmed"]
                    ):
                        _fail("uncertain ownership cannot recover into active execution")
                    state = MutationFenceState.ACTIVE
            normalized.append(frozen_mapping(event, "fence_event"))
            previous = event
        object.__setattr__(self, "history", tuple(normalized))
        object.__setattr__(
            self,
            "fence_checksum",
            canonical_payload_checksum(self.to_dict(include_checksum=False)),
        )

    def _event_checksum(self, event: Mapping[str, Any]) -> str:
        return canonical_payload_checksum(
            {
                "owner_scope": self.owner_scope,
                "resource_key": self.resource_key,
                "event": event,
            }
        )

    @property
    def state(self) -> MutationFenceState:
        action = self.history[-1]["action"]
        if action in {"RELEASED", "RECOVERED_RELEASED"}:
            return MutationFenceState.RELEASED
        if action == "LOST":
            return MutationFenceState.INDETERMINATE
        return MutationFenceState.ACTIVE

    def require_owned(self, *, owner_id: str, generation: int, now_ms: int) -> None:
        latest = self.history[-1]
        _int(generation, "generation", 1)
        _int(now_ms, "now_ms")
        if (
            self.state is not MutationFenceState.ACTIVE
            or latest["owner_id"] != owner_id
            or latest["generation"] != generation
            or not latest["at_ms"] <= now_ms < latest["expires_at_ms"]
        ):
            _fail(
                "resource fence ownership is lost or uncertain; execution must halt",
                "SIDE_EFFECT_FENCE_LOST",
            )

    @classmethod
    def acquire(
        cls,
        *,
        owner_scope: str,
        resource_key: str,
        operation_key: str,
        owner_id: str,
        now_ms: int,
        ttl_ms: int,
        previous: MutationFence | None = None,
    ) -> MutationFence:
        _int(ttl_ms, "ttl_ms", 1)
        _int(now_ms, "now_ms")
        if previous is not None and (
            previous.owner_scope,
            previous.resource_key,
        ) != (owner_scope, resource_key):
            _fail("fence recovery resource identity conflicts")
        prior_operation = (
            next(
                (
                    event
                    for event in previous.history
                    if event["operation_key"] == operation_key
                ),
                None,
            )
            if previous
            else None
        )
        generation = (
            prior_operation["generation"]
            if prior_operation
            else previous.history[-1]["generation"] + 1
            if previous
            else 1
        )
        return cls._append(
            owner_scope,
            resource_key,
            previous,
            operation_key=operation_key,
            action="ACQUIRED",
            owner_id=owner_id,
            generation=generation,
            at_ms=now_ms,
            expires_at_ms=now_ms + ttl_ms,
            termination_confirmed=False,
        )

    def transitioned(
        self,
        action: str,
        *,
        operation_key: str,
        owner_id: str,
        generation: int,
        now_ms: int,
        expires_at_ms: int | None = None,
        termination_confirmed: bool = False,
    ) -> MutationFence:
        if action == "ACQUIRED":
            _fail("use acquisition to create a new generation")
        return self._append(
            self.owner_scope,
            self.resource_key,
            self,
            operation_key=operation_key,
            action=action,
            owner_id=owner_id,
            generation=generation,
            at_ms=now_ms,
            expires_at_ms=(
                self.history[-1]["expires_at_ms"]
                if expires_at_ms is None
                else expires_at_ms
            ),
            termination_confirmed=termination_confirmed,
        )

    def renew(
        self,
        *,
        operation_key: str,
        owner_id: str,
        generation: int,
        now_ms: int,
        expires_at_ms: int,
    ) -> MutationFence:
        return self.transitioned(
            "RENEWED",
            operation_key=operation_key,
            owner_id=owner_id,
            generation=generation,
            now_ms=now_ms,
            expires_at_ms=expires_at_ms,
        )

    def release(
        self,
        *,
        operation_key: str,
        owner_id: str,
        generation: int,
        now_ms: int,
        termination_confirmed: bool = True,
    ) -> MutationFence:
        return self.transitioned(
            "RELEASED",
            operation_key=operation_key,
            owner_id=owner_id,
            generation=generation,
            now_ms=now_ms,
            termination_confirmed=termination_confirmed,
        )

    def mark_lost(
        self,
        *,
        operation_key: str,
        owner_id: str,
        generation: int,
        now_ms: int,
    ) -> MutationFence:
        return self.transitioned(
            "LOST",
            operation_key=operation_key,
            owner_id=owner_id,
            generation=generation,
            now_ms=now_ms,
        )

    def recover_active(
        self,
        *,
        operation_key: str,
        owner_id: str,
        generation: int,
        now_ms: int,
    ) -> MutationFence:
        return self.transitioned(
            "RECOVERED_ACTIVE",
            operation_key=operation_key,
            owner_id=owner_id,
            generation=generation,
            now_ms=now_ms,
        )

    def recover_release(
        self,
        *,
        operation_key: str,
        owner_id: str,
        generation: int,
        now_ms: int,
        termination_confirmed: bool = True,
    ) -> MutationFence:
        return self.transitioned(
            "RECOVERED_RELEASED",
            operation_key=operation_key,
            owner_id=owner_id,
            generation=generation,
            now_ms=now_ms,
            termination_confirmed=termination_confirmed,
        )

    @classmethod
    def _append(
        cls,
        owner_scope: str,
        resource_key: str,
        previous: MutationFence | None,
        **fields: Any,
    ) -> MutationFence:
        history = previous.history if previous else ()
        for event in history:
            if event["operation_key"] == fields["operation_key"]:
                if all(event[name] == value for name, value in fields.items()):
                    return previous
                _fail(
                    "fence operation key conflicts with durable history",
                    "SIDE_EFFECT_FENCE_CONFLICT",
                )
        event = {
            **fields,
            "revision": len(history) + 1,
            "previous_checksum": history[-1]["event_checksum"] if history else None,
        }
        event["event_checksum"] = canonical_payload_checksum(
            {
                "owner_scope": owner_scope,
                "resource_key": resource_key,
                "event": event,
            }
        )
        return cls(owner_scope, resource_key, (*history, event))

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = {
            "schema_version": self.schema_version,
            "owner_scope": self.owner_scope,
            "resource_key": self.resource_key,
            "history": [dict(event) for event in self.history],
        }
        if include_checksum:
            value["fence_checksum"] = self.fence_checksum
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> MutationFence:
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "owner_scope",
                    "resource_key",
                    "history",
                    "fence_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("fence_checksum"), "fence_checksum")
        result = cls(**payload)
        if result.fence_checksum != supplied:
            _fail("fence snapshot checksum mismatch")
        return result
