"""Checksum-bound provenance for explicitly shared predecessor outputs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import REF_KIND_RESULT, RefAccessMode, RefDescriptor, RefScope
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.task_plan.canonical import checksum, identifier, task_output_reference_producer


DEPENDENCY_RESULT_BINDING_SCHEMA = "newsroom.harness-dependency-result-binding/v1"


@dataclass(frozen=True, slots=True)
class DependencyResultBinding:
    logical_ref: str
    producer_attempt: SubAgentAttemptIdentity
    source_snapshot_ref: str
    accepted_result_checksum: str
    output_role: str
    descriptor: RefDescriptor
    schema_version: str = DEPENDENCY_RESULT_BINDING_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != DEPENDENCY_RESULT_BINDING_SCHEMA:
            raise HarnessValidationError("unsupported dependency binding schema", code="REF_SNAPSHOT_INVALID")
        if not isinstance(self.producer_attempt, SubAgentAttemptIdentity) or not isinstance(self.descriptor, RefDescriptor):
            raise TypeError("dependency binding requires a producer identity and trusted descriptor")
        for name in ("source_snapshot_ref", "accepted_result_checksum"):
            object.__setattr__(self, name, checksum(getattr(self, name), name))
        object.__setattr__(self, "output_role", identifier(self.output_role, "output_role"))
        producer = self.producer_attempt
        descriptor = self.descriptor
        if (
            task_output_reference_producer(self.logical_ref, (producer.task_id,)) != producer.task_id
            or descriptor.run_id != producer.parent_run_id
            or descriptor.stage_id != producer.stage_id
            or descriptor.owner_id != producer.child_run_id
            or descriptor.ref_kind != REF_KIND_RESULT
            or descriptor.artifact_type != "subagent_output"
            or descriptor.access_mode is not RefAccessMode.READ_ONLY
        ):
            raise HarnessValidationError("dependency binding is outside its producer output", code="REF_SNAPSHOT_BINDING_MISMATCH")

    @property
    def shared_descriptor(self) -> RefDescriptor:
        # Sharing changes only the scope of the derived child grant. The source
        # descriptor and its original private result grant remain immutable.
        return replace(self.descriptor, scope=RefScope.SHARED_READ_ONLY)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "logical_ref": self.logical_ref,
            "producer_attempt": self.producer_attempt.to_dict(),
            "source_snapshot_ref": self.source_snapshot_ref,
            "accepted_result_checksum": self.accepted_result_checksum,
            "output_role": self.output_role,
            "descriptor": self.descriptor.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DependencyResultBinding:
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise HarnessValidationError("invalid dependency binding fields", code="REF_SNAPSHOT_INVALID")
        return cls(**{
            **value,
            "producer_attempt": SubAgentAttemptIdentity.from_dict(value["producer_attempt"]),
            "descriptor": RefDescriptor.from_dict(value["descriptor"]),
        })
