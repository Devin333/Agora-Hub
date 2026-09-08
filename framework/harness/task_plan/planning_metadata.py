"""Trusted metadata for durable planning-observation receipts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, Self, runtime_checkable

if TYPE_CHECKING:
    from framework.harness.ref_snapshot import RefAuthoritySnapshot

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    checksum,
    identifier,
    positive_int,
    reference,
    stable_text_tuple,
)
from framework.harness.task_plan.planning_observation import (
    PLANNING_OBSERVATION_RECEIPT_SCHEMA,
    PlanningObservationReceipt,
)


PLANNING_OBSERVATION_DESCRIPTOR_SCHEMA = (
    "newsroom.harness-planning-observation-descriptor/v1"
)


class PlanningObservationStorageError(HarnessValidationError):
    """Stable typed failure from planning-observation persistence."""


class PlanningObservationIncompleteError(PlanningObservationStorageError):
    """Only one side of an immutable metadata/payload pair is present."""


class PlanningObservationConflictError(PlanningObservationStorageError):
    """One request identity was reused for different receipt content."""


class PlanningObservationCorruptError(PlanningObservationStorageError):
    """Committed planning-observation evidence failed integrity checks."""


@dataclass(frozen=True, slots=True)
class PlanningObservationDescriptor:
    """Metadata-only receipt authority; excludes arguments and observation text."""

    input_snapshot_ref: str
    request_checksum: str
    run_id: str
    stage_id: str
    planner_turn_id: str
    policy_checksum: str
    request_id: str
    attempt: int
    source_ref: str
    receipt_checksum: str
    status: str
    tool_call_id: str | None
    artifact_refs: tuple[str, ...]
    payload_checksum: str
    payload_size_bytes: int
    receipt_schema: str = PLANNING_OBSERVATION_RECEIPT_SCHEMA
    schema_version: str = PLANNING_OBSERVATION_DESCRIPTOR_SCHEMA
    descriptor_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != PLANNING_OBSERVATION_DESCRIPTOR_SCHEMA:
            raise _invalid("unsupported planning observation descriptor schema")
        if self.receipt_schema != PLANNING_OBSERVATION_RECEIPT_SCHEMA:
            raise _invalid("unsupported planning observation receipt schema")
        object.__setattr__(
            self,
            "input_snapshot_ref",
            checksum(self.input_snapshot_ref, "input_snapshot_ref"),
        )
        for name in ("request_checksum", "policy_checksum", "receipt_checksum", "payload_checksum"):
            object.__setattr__(self, name, checksum(getattr(self, name), name))
        for name in ("run_id", "stage_id", "planner_turn_id", "request_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        object.__setattr__(self, "attempt", positive_int(self.attempt, "attempt"))
        object.__setattr__(self, "source_ref", reference(self.source_ref, "source_ref"))
        if self.source_ref != f"planning-observation://{self.receipt_checksum}":
            raise _invalid("planning observation source ref does not match its receipt")
        if self.status not in {"SUCCEEDED", "REJECTED", "FAILED", "TIMED_OUT"}:
            raise _invalid("planning observation descriptor status is invalid")
        if self.tool_call_id is not None:
            object.__setattr__(
                self,
                "tool_call_id",
                identifier(self.tool_call_id, "tool_call_id"),
            )
        object.__setattr__(
            self,
            "artifact_refs",
            stable_text_tuple(
                self.artifact_refs,
                "artifact_refs",
                item_kind="reference",
            ),
        )
        if (
            isinstance(self.payload_size_bytes, bool)
            or not isinstance(self.payload_size_bytes, int)
            or self.payload_size_bytes <= 0
        ):
            raise _invalid("planning observation payload size must be positive")
        object.__setattr__(
            self,
            "descriptor_checksum",
            checksum_for(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "input_snapshot_ref": self.input_snapshot_ref,
            "request_checksum": self.request_checksum,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "planner_turn_id": self.planner_turn_id,
            "policy_checksum": self.policy_checksum,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "source_ref": self.source_ref,
            "receipt_checksum": self.receipt_checksum,
            "status": self.status,
            "tool_call_id": self.tool_call_id,
            "artifact_refs": list(self.artifact_refs),
            "payload_checksum": self.payload_checksum,
            "payload_size_bytes": self.payload_size_bytes,
            "receipt_schema": self.receipt_schema,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "descriptor_checksum": self.descriptor_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        expected = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        if not isinstance(value, Mapping) or set(value) != expected:
            raise _invalid("planning observation descriptor fields are invalid")
        payload = dict(value)
        supplied = checksum(payload.pop("descriptor_checksum"), "descriptor_checksum")
        descriptor = cls(**payload)
        if descriptor.descriptor_checksum != supplied:
            raise PlanningObservationCorruptError(
                "planning observation descriptor checksum mismatch",
                code="planning_observation_descriptor_corrupt",
            )
        return descriptor

    @classmethod
    def for_receipt(
        cls,
        receipt: PlanningObservationReceipt,
        *,
        input_snapshot_ref: str,
        payload_checksum: str,
        payload_size_bytes: int,
    ) -> Self:
        if not isinstance(receipt, PlanningObservationReceipt):
            raise TypeError("receipt must be PlanningObservationReceipt")
        request = receipt.request
        return cls(
            input_snapshot_ref=input_snapshot_ref,
            request_checksum=request.request_checksum,
            run_id=request.run_id,
            stage_id=request.stage_id,
            planner_turn_id=request.planner_turn_id,
            policy_checksum=request.policy_checksum,
            request_id=request.request_id,
            attempt=request.attempt,
            source_ref=receipt.source_ref,
            receipt_checksum=receipt.receipt_checksum,
            status=receipt.status,
            tool_call_id=receipt.tool_call_id,
            artifact_refs=receipt.artifact_refs,
            payload_checksum=payload_checksum,
            payload_size_bytes=payload_size_bytes,
            receipt_schema=receipt.schema_version,
        )


@runtime_checkable
class PlanningObservationDescriptorPort(Protocol):
    @property
    def input_snapshot(self) -> RefAuthoritySnapshot: ...

    def describe_request(
        self,
        request_checksum: str,
    ) -> PlanningObservationDescriptor | None: ...

    def describe_ref(
        self,
        source_ref: str,
    ) -> PlanningObservationDescriptor | None: ...

    def descriptors_for_scope(
        self,
        run_id: str,
        stage_id: str,
        planner_turn_id: str,
    ) -> tuple[PlanningObservationDescriptor, ...]: ...


def _invalid(message: str) -> PlanningObservationStorageError:
    return PlanningObservationStorageError(
        message,
        code="planning_observation_descriptor_invalid",
    )


__all__ = [
    "PLANNING_OBSERVATION_DESCRIPTOR_SCHEMA",
    "PlanningObservationConflictError",
    "PlanningObservationCorruptError",
    "PlanningObservationDescriptor",
    "PlanningObservationDescriptorPort",
    "PlanningObservationIncompleteError",
    "PlanningObservationStorageError",
]
