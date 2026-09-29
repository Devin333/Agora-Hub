"""Typed gate evidence and its immutable artifact boundary.

The evidence model records a deterministic evaluator outcome.  Artifact refs
prove immutable scoped content only; result acceptance and evaluator provenance
remain owned by :mod:`framework.harness.task_plan.verification`.  Composition
must grant the writer capability only to the trusted verifier.  The artifact
owner cannot distinguish that verifier from a malicious caller that already
holds the same writer capability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    exact_reference,
    optional_text,
)
from framework.harness.task_plan.models import TaskInstance, ValidatedTaskPlan
from framework.harness.workers.result import HarnessWorkerResult


TASK_PLAN_GATE_INPUT_SCHEMA = "newsroom.harness-task-plan-gate-input/v1"
TASK_PLAN_GATE_EVIDENCE_SCHEMA = "newsroom.harness-task-plan-gate-evidence/v1"


@dataclass(frozen=True, slots=True)
class TaskPlanGateEvidence:
    """Reference-only outcome for one exact deterministic task gate."""

    gate_ref: str
    input_checksum: str
    result_checksum: str
    passed: bool
    reason_code: str | None = None
    evidence_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "gate_ref", exact_reference(self.gate_ref, "gate_ref"))
        object.__setattr__(self, "input_checksum", checksum(self.input_checksum, "input_checksum"))
        object.__setattr__(self, "result_checksum", checksum(self.result_checksum, "result_checksum"))
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be a bool")
        reason_code = optional_text(self.reason_code, "reason_code")
        if not self.passed and reason_code is None:
            raise HarnessValidationError(
                "failed TaskPlan gate evidence requires a stable reason code",
                code="task_plan_gate_evidence_invalid",
            )
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "evidence_checksum", canonical_payload_checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "gate_ref": self.gate_ref,
            "input_checksum": self.input_checksum,
            "result_checksum": self.result_checksum,
            "passed": self.passed,
            "reason_code": self.reason_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "evidence_checksum": self.evidence_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> TaskPlanGateEvidence:
        """Hydrate persisted gate evidence without accepting schema drift."""

        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "gate_ref",
                    "input_checksum",
                    "result_checksum",
                    "passed",
                    "reason_code",
                    "evidence_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("evidence_checksum"), "evidence_checksum")
        evidence = cls(**payload)
        if supplied != evidence.evidence_checksum:
            raise HarnessValidationError(
                "TaskPlan gate evidence checksum does not match its content",
                code="task_plan_gate_evidence_checksum_mismatch",
            )
        return evidence


@dataclass(frozen=True, slots=True)
class TaskPlanGateArtifactRefs:
    """Content-addressed handles returned after one complete artifact write."""

    input_checksum: str
    evidence_checksums: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_checksum", checksum(self.input_checksum, "input_checksum"))
        if not isinstance(self.evidence_checksums, tuple):
            raise TypeError("evidence_checksums must be a tuple")
        normalized = _gate_evidence_refs(self.evidence_checksums)
        object.__setattr__(self, "evidence_checksums", normalized)


@dataclass(frozen=True, slots=True)
class TaskPlanGateVerificationArtifacts:
    """Strictly decoded gate input and evidence from immutable artifacts."""

    refs: TaskPlanGateArtifactRefs
    instance: TaskInstance
    worker_result: HarnessWorkerResult
    evidences: tuple[TaskPlanGateEvidence, ...]


@dataclass(frozen=True, slots=True)
class TaskPlanWorkerResultInputArtifacts:
    """Strictly decoded verifier input for one terminal worker failure."""

    input_checksum: str
    instance: TaskInstance
    worker_result: HarnessWorkerResult

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "input_checksum",
            checksum(self.input_checksum, "input_checksum"),
        )


@runtime_checkable
class TaskPlanGateArtifactWriterPort(Protocol):
    """Persist verifier-owned worker input and deterministic gate evidence."""

    def persist_worker_result_input(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
    ) -> str: ...

    def persist_gate_verification(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
        evidences: Sequence[TaskPlanGateEvidence],
    ) -> TaskPlanGateArtifactRefs: ...


@runtime_checkable
class TaskPlanGateEvidenceReaderPort(Protocol):
    """Recover immutable verifier inputs and gate evidence through scoped refs."""

    def read_worker_result_input(
        self,
        run_id: str,
        stage_id: str,
        input_checksum: str,
    ) -> TaskPlanWorkerResultInputArtifacts: ...

    def read_gate_evidence(
        self,
        run_id: str,
        stage_id: str,
        gate_evidence_refs: Sequence[str],
    ) -> TaskPlanGateVerificationArtifacts: ...


@runtime_checkable
class TaskPlanGateArtifactOwnerPort(
    TaskPlanGateArtifactWriterPort,
    TaskPlanGateEvidenceReaderPort,
    Protocol,
):
    """Combined immutable owner implemented by the durable TaskPlan store."""


def normalize_gate_evidence_refs(values: Sequence[str]) -> tuple[str, ...]:
    """Validate the persisted result refs used to start a strict read."""

    return _gate_evidence_refs(values)


def _gate_evidence_refs(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("gate_evidence_refs must be a sequence")
    normalized = tuple(checksum(value, "evidence_checksum") for value in values)
    if not normalized:
        raise HarnessValidationError(
            "gate artifact refs require at least one evidence checksum",
            code="task_plan_gate_artifact_refs_invalid",
        )
    if len(normalized) != len(set(normalized)):
        raise HarnessValidationError(
            "gate artifact evidence checksums must be unique",
            code="task_plan_gate_artifact_refs_invalid",
        )
    return normalized


__all__ = [
    "TASK_PLAN_GATE_EVIDENCE_SCHEMA",
    "TASK_PLAN_GATE_INPUT_SCHEMA",
    "TaskPlanGateArtifactOwnerPort",
    "TaskPlanGateArtifactRefs",
    "TaskPlanGateArtifactWriterPort",
    "TaskPlanGateEvidence",
    "TaskPlanGateEvidenceReaderPort",
    "TaskPlanGateVerificationArtifacts",
    "TaskPlanWorkerResultInputArtifacts",
    "normalize_gate_evidence_refs",
]
