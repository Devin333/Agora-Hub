"""Immutable, execution-bound reference grants issued by Harness."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    RefAccessMode,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    normalize_ref_descriptors,
)
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.harness.task_plan.canonical import checksum, identifier, positive_int
from framework.shared.graph_identity import GraphExecutionIdentity


REF_AUTHORITY_SNAPSHOT_SCHEMA = "newsroom.harness-ref-authority-snapshot/v1"


class RefSnapshotPhase(StrEnum):
    INPUT_ADMISSION = "INPUT_ADMISSION"
    CHILD_INPUT = "CHILD_INPUT"
    RESULT_ACCEPTANCE = "RESULT_ACCEPTANCE"
    MATERIALIZED_RESULT = "MATERIALIZED_RESULT"


def _invalid(message: str, code: str = "REF_SNAPSHOT_INVALID") -> HarnessValidationError:
    return HarnessValidationError(message, code=code)


@dataclass(frozen=True, slots=True)
class RefAuthoritySnapshot:
    execution_identity: GraphExecutionIdentity
    stage_id: str
    stage_binding_checksum: str
    task_policy_checksum: str
    source_checksum: str
    policy: RefAccessPolicy
    descriptors: tuple[RefDescriptor, ...]
    phase: RefSnapshotPhase | str = RefSnapshotPhase.INPUT_ADMISSION
    parent_snapshot_ref: str | None = None
    attempt_identity: SubAgentAttemptIdentity | None = None
    schema_version: str = REF_AUTHORITY_SNAPSHOT_SCHEMA
    binding_key: str = field(init=False)
    snapshot_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != REF_AUTHORITY_SNAPSHOT_SCHEMA:
            raise _invalid("unsupported reference snapshot schema")
        if not isinstance(self.execution_identity, GraphExecutionIdentity):
            raise TypeError("execution_identity must be GraphExecutionIdentity")
        object.__setattr__(self, "stage_id", identifier(self.stage_id, "stage_id"))
        for name in ("stage_binding_checksum", "task_policy_checksum", "source_checksum"):
            object.__setattr__(self, name, checksum(getattr(self, name), name))
        try:
            object.__setattr__(self, "phase", RefSnapshotPhase(self.phase))
        except (TypeError, ValueError) as exc:
            raise _invalid("unsupported reference snapshot phase") from exc
        if not isinstance(self.policy, RefAccessPolicy):
            raise TypeError("policy must be RefAccessPolicy")
        RefAuthority.require_scope(self.policy, run_id=self.run_id, stage_id=self.stage_id)
        if isinstance(self.descriptors, (str, bytes)) or not isinstance(self.descriptors, Sequence):
            raise _invalid("reference snapshot descriptors must be an array")
        if any(not isinstance(item, RefDescriptor) for item in self.descriptors):
            raise _invalid("reference snapshot contains an invalid descriptor")
        descriptors = tuple(sorted(self.descriptors, key=lambda item: item.ref))
        refs = tuple(item.ref for item in descriptors)
        if len(set(refs)) != len(refs) or refs != self.policy.allowed_refs:
            raise _invalid("reference snapshot must describe exactly the pinned allowlist")
        normalize_ref_descriptors({item.ref: item for item in descriptors})
        authority = RefAuthority()
        for item in descriptors:
            authority.authorize(
                item,
                self.policy,
                requested_access=(
                    RefAccessMode.READ_WRITE
                    if item.ref in self.policy.writable_refs
                    else RefAccessMode.READ_ONLY
                ),
            )
        object.__setattr__(self, "descriptors", descriptors)
        if self.phase is RefSnapshotPhase.INPUT_ADMISSION:
            if self.parent_snapshot_ref is not None or self.attempt_identity is not None:
                raise _invalid("input admission cannot carry a child or parent grant")
        else:
            object.__setattr__(self, "parent_snapshot_ref", checksum(self.parent_snapshot_ref, "parent_snapshot_ref"))
            self._require_attempt()
        object.__setattr__(self, "binding_key", checksum_for(self.binding_projection()))
        object.__setattr__(self, "snapshot_checksum", checksum_for(self.checksum_projection()))

    @property
    def run_id(self) -> str:
        return self.execution_identity.run_id

    @property
    def snapshot_ref(self) -> str:
        return self.snapshot_checksum

    def _require_attempt(self) -> None:
        attempt = self.attempt_identity
        if not isinstance(attempt, SubAgentAttemptIdentity):
            raise _invalid("child reference snapshot requires its accepted attempt")
        execution = self.execution_identity
        actual = self.execution_for_attempt(attempt)
        if actual != execution or (
            attempt.stage_id != self.stage_id
            or attempt.stage_binding_checksum != self.stage_binding_checksum
            or attempt.child_run_id != self.policy.owner_id
        ):
            raise _invalid("reference snapshot is outside its accepted attempt", "REF_SNAPSHOT_BINDING_MISMATCH")

    def binding_projection(self) -> dict[str, Any]:
        attempt = self.attempt_identity
        return {
            "schema_version": self.schema_version,
            "phase": self.phase.value,
            "execution_identity": self.execution_identity.to_dict(),
            "stage_id": self.stage_id,
            "stage_binding_checksum": self.stage_binding_checksum,
            "task_instance_id": None if attempt is None else attempt.task_instance_id,
            "attempt": None if attempt is None else attempt.attempt,
        }

    @staticmethod
    def admission_binding_key(execution_identity: GraphExecutionIdentity, stage_id: str, stage_binding_checksum: str) -> str:
        return checksum_for({
            "schema_version": REF_AUTHORITY_SNAPSHOT_SCHEMA,
            "phase": RefSnapshotPhase.INPUT_ADMISSION.value,
            "execution_identity": execution_identity.to_dict(),
            "stage_id": identifier(stage_id, "stage_id"),
            "stage_binding_checksum": checksum(stage_binding_checksum, "stage_binding_checksum"),
            "task_instance_id": None,
            "attempt": None,
        })

    @staticmethod
    def execution_for_attempt(attempt: SubAgentAttemptIdentity) -> GraphExecutionIdentity:
        if not isinstance(attempt, SubAgentAttemptIdentity):
            raise TypeError("attempt must be SubAgentAttemptIdentity")
        return GraphExecutionIdentity(
            run_id=attempt.parent_run_id, graph_id=attempt.graph_id,
            graph_version=attempt.graph_version, graph_ref=attempt.graph_ref,
            graph_checksum=attempt.graph_checksum, node_id=attempt.node_id,
            node_instance_id=attempt.node_instance_id, activity_id=attempt.activity_id,
            attempt=attempt.activity_attempt,
        )

    @staticmethod
    def task_binding_key(
        execution: GraphExecutionIdentity, stage_id: str, stage_binding_checksum: str,
        task_instance_id: str, attempt: int, phase: RefSnapshotPhase,
    ) -> str:
        phase = RefSnapshotPhase(phase)
        if phase is RefSnapshotPhase.INPUT_ADMISSION:
            raise _invalid("task reference binding requires a child or result phase")

        return checksum_for({
            "schema_version": REF_AUTHORITY_SNAPSHOT_SCHEMA,
            "phase": phase.value,
            "execution_identity": execution.to_dict(),
            "stage_id": identifier(stage_id, "stage_id"),
            "stage_binding_checksum": checksum(stage_binding_checksum, "stage_binding_checksum"),
            "task_instance_id": identifier(task_instance_id, "task_instance_id"),
            "attempt": positive_int(attempt, "attempt"),
        })

    @classmethod
    def attempt_binding_key(cls, attempt: SubAgentAttemptIdentity, phase: RefSnapshotPhase) -> str:
        return cls.task_binding_key(
            cls.execution_for_attempt(attempt), attempt.stage_id,
            attempt.stage_binding_checksum, attempt.task_instance_id, attempt.attempt, phase,
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_identity": self.execution_identity.to_dict(),
            "stage_id": self.stage_id,
            "stage_binding_checksum": self.stage_binding_checksum,
            "task_policy_checksum": self.task_policy_checksum,
            "source_checksum": self.source_checksum,
            "policy": self.policy.to_dict(),
            "descriptors": [item.to_dict() for item in self.descriptors],
            "phase": self.phase.value,
            "parent_snapshot_ref": self.parent_snapshot_ref,
            "attempt_identity": None if self.attempt_identity is None else self.attempt_identity.to_dict(),
            "binding_key": self.binding_key,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "snapshot_checksum": self.snapshot_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RefAuthoritySnapshot":
        if not isinstance(value, Mapping) or set(value) != set(cls.__dataclass_fields__):
            raise _invalid("reference snapshot fields do not match its schema")
        payload = dict(value)
        expected_checksum = checksum(payload.pop("snapshot_checksum"), "snapshot_checksum")
        expected_binding = checksum(payload.pop("binding_key"), "binding_key")
        payload["execution_identity"] = GraphExecutionIdentity.from_dict(payload["execution_identity"])
        payload["policy"] = RefAccessPolicy.from_dict(payload["policy"])
        descriptors = payload["descriptors"]
        if isinstance(descriptors, (str, bytes)) or not isinstance(descriptors, Sequence):
            raise _invalid("reference snapshot descriptors must be an array")
        payload["descriptors"] = tuple(RefDescriptor.from_dict(item) for item in descriptors)
        if payload["attempt_identity"] is not None:
            payload["attempt_identity"] = SubAgentAttemptIdentity.from_dict(payload["attempt_identity"])
        result = cls(**payload)
        if result.binding_key != expected_binding or result.snapshot_checksum != expected_checksum:
            raise _invalid("reference snapshot checksum does not match its content", "REF_CHECKSUM_MISMATCH")
        return result

    def validate_parent(self, parent: "RefAuthoritySnapshot") -> None:
        if (
            self.phase is RefSnapshotPhase.INPUT_ADMISSION
            or parent.snapshot_ref != self.parent_snapshot_ref
            or parent.execution_identity != self.execution_identity
            or parent.stage_id != self.stage_id
            or parent.stage_binding_checksum != self.stage_binding_checksum
            or parent.task_policy_checksum != self.task_policy_checksum
            or parent.policy.tenant_id != self.policy.tenant_id
        ):
            raise _invalid("reference snapshot parent binding does not match", "REF_SNAPSHOT_BINDING_MISMATCH")
        if self.phase is RefSnapshotPhase.CHILD_INPUT:
            if parent.phase is not RefSnapshotPhase.INPUT_ADMISSION:
                raise _invalid("child input grant requires input admission")
            inherited = {item.ref: item for item in parent.descriptors}
            if self.policy.writable_refs or any(inherited.get(item.ref) != item for item in self.descriptors):
                raise _invalid("child input grant cannot create or modify references", "REF_UNAUTHORIZED")
            if self.source_checksum != parent.source_checksum:
                raise _invalid("child input grant changed its source checksum", "REF_CHECKSUM_MISMATCH")
        else:
            required_parent = (
                RefSnapshotPhase.CHILD_INPUT
                if self.phase is RefSnapshotPhase.RESULT_ACCEPTANCE
                else RefSnapshotPhase.RESULT_ACCEPTANCE
            )
            if parent.phase is not required_parent or parent.attempt_identity != self.attempt_identity:
                raise _invalid("result grant requires the same admitted child attempt", "REF_SNAPSHOT_BINDING_MISMATCH")
            if self.policy.writable_refs:
                raise _invalid("result grants must remain read-only", "REF_ACCESS_MODE_DENIED")


@runtime_checkable
class RefAuthoritySnapshotStorePort(Protocol):
    def commit(self, snapshot: RefAuthoritySnapshot) -> str: ...

    def get(self, *, run_id: str, snapshot_ref: str) -> RefAuthoritySnapshot: ...

    def find(self, *, run_id: str, binding_key: str) -> RefAuthoritySnapshot | None: ...


class SnapshotRefResolutionPort:
    """Resolve only descriptors in one already-verified immutable grant."""

    def __init__(self, snapshot: RefAuthoritySnapshot) -> None:
        if not isinstance(snapshot, RefAuthoritySnapshot):
            raise TypeError("snapshot must be RefAuthoritySnapshot")
        self._descriptors = normalize_ref_descriptors({item.ref: item for item in snapshot.descriptors})

    def resolve(self, ref: str) -> RefDescriptor | None:
        descriptor = self._descriptors.get(ref)
        return descriptor if descriptor is not None and descriptor.ref == ref else None

    def resolve_namespace(self, namespace: str) -> RefDescriptor | None:
        descriptor = self._descriptors.get(namespace)
        return descriptor if descriptor is not None and descriptor.namespace == namespace else None
