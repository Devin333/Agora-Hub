from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    frozen_mapping,
    identifier,
    required_text,
    thaw_mapping,
)


PARENT_CONTINUATION_SCHEMA = "newsroom.harness-parent-continuation/v1"
PARENT_CONTINUATION_EVENT = "PARENT_OBSERVATION_CONTINUATION"
_STATUSES = frozenset({"PENDING", "DELIVERED", "CANCELLED"})
_TERMINAL_GROUP_STATES = frozenset({
    "SUCCEEDED", "FAILED", "CANCELLED", "INDETERMINATE", "HALTED", "SUPERSEDED",
})


@dataclass(frozen=True, slots=True)
class ParentContinuation:
    """Durable wake-up envelope for one parent turn.

    The continuation is deliberately a control-plane identity only. It carries
    no child prompt, transcript or raw observation payload; the observation is
    resolved by its checksum-bound identity at delivery time.
    """

    run_id: str
    stage_id: str
    parent_turn_id: str
    observation_id: str
    observation_version: int
    group_id: str
    observation_checksum: str
    status: str = "PENDING"
    submission_id: str | None = None
    group_state: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = PARENT_CONTINUATION_SCHEMA
    continuation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("run_id", "stage_id", "parent_turn_id", "observation_id", "group_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        if isinstance(self.observation_version, bool) or not isinstance(self.observation_version, int) or self.observation_version < 1:
            raise HarnessValidationError(
                "parent continuation observation_version must be positive",
                code="parent_continuation_invalid",
            )
        object.__setattr__(self, "observation_checksum", checksum(self.observation_checksum, "observation_checksum"))
        status = required_text(self.status, "status").upper()
        if status not in _STATUSES:
            raise HarnessValidationError(
                "parent continuation status is unsupported",
                code="parent_continuation_invalid",
            )
        object.__setattr__(self, "status", status)
        if status in {"DELIVERED", "CANCELLED"} and self.group_state is None:
            raise HarnessValidationError(
                "terminal parent continuation requires terminal group state",
                code="parent_continuation_state_invalid",
            )
        if self.submission_id is not None:
            object.__setattr__(self, "submission_id", identifier(self.submission_id, "submission_id"))
        if self.group_state is not None:
            state = required_text(self.group_state, "group_state").upper()
            object.__setattr__(self, "group_state", state)
            if status in {"DELIVERED", "CANCELLED"} and state not in _TERMINAL_GROUP_STATES:
                raise HarnessValidationError(
                    "delivered parent continuation requires terminal group state",
                    code="parent_continuation_state_invalid",
                )
        object.__setattr__(self, "metadata", frozen_mapping(self.metadata, "metadata"))
        if self.schema_version != PARENT_CONTINUATION_SCHEMA:
            raise HarnessValidationError(
                "unsupported parent continuation schema",
                code="parent_continuation_schema_unsupported",
            )
        object.__setattr__(self, "continuation_checksum", canonical_payload_checksum(self.checksum_projection()))

    def identity_key(self) -> tuple[str, str, str, int]:
        return (self.run_id, self.stage_id, self.observation_id, self.observation_version)

    def parent_scope(self) -> tuple[str, str, str, str]:
        return (self.run_id, self.stage_id, self.parent_turn_id, self.group_id)

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "parent_turn_id": self.parent_turn_id,
            "observation_id": self.observation_id,
            "observation_version": self.observation_version,
            "group_id": self.group_id,
            "observation_checksum": self.observation_checksum,
            "status": self.status,
            "submission_id": self.submission_id,
            "group_state": self.group_state,
            "metadata": thaw_mapping(self.metadata),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "continuation_checksum": self.continuation_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ParentContinuation":
        required = frozenset({
            "schema_version", "run_id", "stage_id", "parent_turn_id", "observation_id",
            "observation_version", "group_id", "observation_checksum", "status",
            "submission_id", "group_state", "metadata", "continuation_checksum",
        })
        payload = exact_keys(value, required=required, model=cls.__name__)
        supplied = checksum(payload.pop("continuation_checksum"), "continuation_checksum")
        continuation = cls(**payload)
        if supplied != continuation.continuation_checksum:
            raise HarnessValidationError(
                "parent continuation checksum does not match content",
                code="parent_continuation_checksum_mismatch",
            )
        return continuation


def continuation_from_event(event: Any) -> ParentContinuation:
    """Decode a continuation event from TaskPlanEvent or a plain event mapping."""
    event_type = getattr(event, "event_type", None)
    payload = getattr(event, "payload", None)
    if isinstance(event, Mapping):
        event_type = event.get("event_type")
        payload = event.get("payload", event)
    if event_type != PARENT_CONTINUATION_EVENT or not isinstance(payload, Mapping):
        raise HarnessValidationError(
            "event is not a parent continuation",
            code="parent_continuation_event_invalid",
        )
    raw = payload.get("continuation", payload)
    if not isinstance(raw, Mapping):
        raise HarnessValidationError(
            "parent continuation event payload is invalid",
            code="parent_continuation_event_invalid",
        )
    return ParentContinuation.from_dict(raw)


def validate_continuation_projection(
    continuation: ParentContinuation,
    *,
    run_id: str,
    stage_id: str,
    group: Mapping[str, Any] | None,
    observation_checksum: str | None,
) -> None:
    """Bind a continuation to recorded group state, never to a claimed outcome."""
    if (
        continuation.run_id != run_id
        or continuation.stage_id != stage_id
        or group is None
        or group.get("group_id") != continuation.group_id
    ):
        raise HarnessValidationError(
            "parent continuation is outside recorded group scope",
            code="parent_continuation_scope_mismatch",
        )
    if observation_checksum is not None and continuation.observation_checksum != observation_checksum:
        raise HarnessValidationError(
            "parent continuation observation checksum differs from recorded evidence",
            code="parent_continuation_observation_mismatch",
        )
    if continuation.status in {"DELIVERED", "CANCELLED"} and (
        continuation.group_state != group.get("state")
        or group.get("state") not in _TERMINAL_GROUP_STATES
        or observation_checksum is None
    ):
        raise HarnessValidationError(
            "terminal parent continuation lacks matching group observation",
            code="parent_continuation_terminal_mismatch",
        )


def validate_parent_continuation_append(
    history: Sequence[Any], events: Sequence[Any],
) -> None:
    """Validate immutable, idempotent continuation facts at the canonical CAS."""
    records: dict[tuple[str, str, str, int], ParentContinuation] = {}
    latest: dict[tuple[str, str, str], int] = {}
    for item in (*history, *events):
        if getattr(item, "event_type", item.get("event_type") if isinstance(item, Mapping) else None) != PARENT_CONTINUATION_EVENT:
            continue
        continuation = continuation_from_event(item)
        key = continuation.identity_key()
        existing = records.get(key)
        if existing is not None:
            if existing.continuation_checksum != continuation.continuation_checksum:
                if (
                    existing.status == "PENDING"
                    and continuation.status in {"DELIVERED", "CANCELLED"}
                    and existing.observation_checksum == continuation.observation_checksum
                    and existing.parent_scope() == continuation.parent_scope()
                    and existing.submission_id == continuation.submission_id
                    and existing.metadata == continuation.metadata
                ):
                    records[key] = continuation
                    continue
                raise HarnessValidationError(
                    "parent continuation identity has conflicting content",
                    code="parent_continuation_conflict",
                    details={"observation_id": continuation.observation_id, "observation_version": continuation.observation_version},
                )
            continue
        scope = continuation.parent_scope()[:3]
        prior_version = latest.get(scope)
        if prior_version is not None and continuation.observation_version != prior_version + 1:
            raise HarnessValidationError(
                "parent continuation observation version regressed or is not contiguous",
                code="parent_continuation_version_conflict",
            )
        records[key] = continuation
        latest[scope] = max(continuation.observation_version, prior_version or 0)


# Short alias used by stores that validate multiple canonical append families.
validate_continuation_append = validate_parent_continuation_append


__all__ = [
    "PARENT_CONTINUATION_EVENT",
    "PARENT_CONTINUATION_SCHEMA",
    "ParentContinuation",
    "continuation_from_event",
    "validate_continuation_append",
    "validate_continuation_projection",
    "validate_parent_continuation_append",
]
