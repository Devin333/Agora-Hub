"""Deterministic proof checks for one persisted TaskPlan result.

This module does not evaluate gates.  It binds a result envelope back to the
immutable gate input and evaluator outcomes written by the online verifier.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.transcript import SubAgentTranscriptReceipt
from framework.harness.task_plan.canonical import canonical_payload_checksum, thaw_mapping
from framework.harness.task_plan.gate_evidence import (
    TaskPlanGateEvidenceReaderPort,
    TaskPlanGateVerificationArtifacts,
    TaskPlanWorkerResultInputArtifacts,
)
from framework.harness.task_plan.models import TaskLifecycle, ValidatedTaskPlan
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.workers.result import HarnessWorkerStatus

if TYPE_CHECKING:
    from framework.harness.task_plan.store import TaskResultRecord


_SUBAGENT_ATTEMPT_EVIDENCE_TYPE = "subagent_attempt"
_WORKER_FAILURE_CODE = "task_worker_failed"


@dataclass(frozen=True, slots=True)
class TaskPlanResultProof:
    """Decoded proof used by append, replay, and recovery callers."""

    artifacts: TaskPlanGateVerificationArtifacts | None
    worker_input: TaskPlanWorkerResultInputArtifacts | None
    worker_failed: bool


def verify_task_result_proof(
    plan: ValidatedTaskPlan,
    result: TaskResultRecord,
    *,
    gate_evidence_reader: TaskPlanGateEvidenceReaderPort | None,
) -> TaskPlanResultProof:
    """Bind one result to its accepted task and immutable gate artifacts.

    Worker failures have no gate verdict because gates are not invoked for
    them, but they still require the verifier-persisted immutable worker input.
    Every result must bind back to content readable through the scoped owner.
    """

    from framework.harness.task_plan.store import TaskResultRecord

    if not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("plan must be ValidatedTaskPlan")
    if not isinstance(result, TaskResultRecord):
        raise TypeError("result must be TaskResultRecord")
    if not result.matches_plan_identity(plan):
        _fail("result proof is outside the accepted plan")
    definition = next(
        (item for item in plan.tasks if item.task_id == result.task_id),
        None,
    )
    if definition is None:
        _fail("result proof references an unknown accepted task")
    expected_instance = task_instance_for_attempt(
        plan,
        result.task_id,
        result.attempt,
        task_instance_id=result.task_instance_id,
    )
    if (
        result.worker_ref != expected_instance.worker_ref
        or result.task_checksum != expected_instance.task_definition_checksum
        or result.binding_checksum != definition.binding_checksum
    ):
        _fail("result proof differs from its accepted task binding")

    has_gate_claim = bool(result.verified_gate_refs or result.gate_evidence_refs)
    if not has_gate_claim:
        if result.verified_gate_refs or result.gate_evidence_refs:
            _fail("result carries an incomplete gate proof")
        if result.status is TaskLifecycle.FAILED:
            if (
                result.error_code != _WORKER_FAILURE_CODE
                or result.worker_result_proof_ref is None
            ):
                _fail("failed result has no worker or gate proof")
            reader = _require_reader(gate_evidence_reader, result.task_id)
            worker_input = reader.read_worker_result_input(
                result.run_id,
                result.stage_id,
                result.worker_result_proof_ref,
            )
            worker_result = worker_input.worker_result
            expected_input_checksum = canonical_payload_checksum(
                {
                    "instance": expected_instance.checksum_projection(),
                    "worker_result": worker_result.candidate_payload(),
                }
            )
            if (
                worker_input.instance != expected_instance
                or worker_input.input_checksum != result.worker_result_proof_ref
                or worker_input.input_checksum != expected_input_checksum
                or worker_result.status is HarnessWorkerStatus.SUCCEEDED
                or worker_result.effect_intent is not None
                or thaw_mapping(result.usage) != worker_result.metrics
            ):
                _fail("worker failure differs from its immutable worker input")
            _validate_receipt_binding(
                definition,
                result,
                worker_result,
                require_result_ref=False,
            )
            return TaskPlanResultProof(
                artifacts=None,
                worker_input=worker_input,
                worker_failed=True,
            )
        if definition.gate_refs:
            raise HarnessValidationError(
                "successful gated result is missing immutable gate proof",
                code="task_plan_gate_proof_required",
                details={"task_id": result.task_id},
            )
        return TaskPlanResultProof(
            artifacts=None,
            worker_input=None,
            worker_failed=False,
        )

    if not result.verified_gate_refs or not result.gate_evidence_refs:
        _fail("result carries an incomplete gate proof")
    reader = _require_reader(gate_evidence_reader, result.task_id)

    artifacts = reader.read_gate_evidence(
        result.run_id,
        result.stage_id,
        result.gate_evidence_refs,
    )
    worker_result = artifacts.worker_result
    evidences = artifacts.evidences
    expected_input_checksum = canonical_payload_checksum(
        {
            "instance": expected_instance.checksum_projection(),
            "worker_result": worker_result.candidate_payload(),
        }
    )
    expected_result_checksum = canonical_payload_checksum(
        worker_result.candidate_payload()
    )
    if (
        artifacts.instance != expected_instance
        or artifacts.refs.evidence_checksums != result.gate_evidence_refs
        or artifacts.refs.input_checksum != expected_input_checksum
        or tuple(item.gate_ref for item in evidences) != definition.gate_refs
        or result.verified_gate_refs != definition.gate_refs
        or tuple(item.evidence_checksum for item in evidences)
        != result.gate_evidence_refs
        or any(
            item.input_checksum != expected_input_checksum
            or item.result_checksum != expected_result_checksum
            for item in evidences
        )
        or worker_result.status is not HarnessWorkerStatus.SUCCEEDED
        or worker_result.effect_intent is not None
        or thaw_mapping(result.usage) != worker_result.metrics
    ):
        _fail("result differs from its immutable gate input or evidence")

    _validate_receipt_binding(
        definition,
        result,
        worker_result,
        require_result_ref=result.status is TaskLifecycle.SUCCEEDED,
    )

    failed = next((item for item in evidences if not item.passed), None)
    if failed is None:
        if result.status is not TaskLifecycle.SUCCEEDED or result.error_code is not None:
            _fail("all passing gates cannot produce a failed result")
    elif (
        result.status is not TaskLifecycle.FAILED
        or result.error_code != failed.reason_code
    ):
        _fail("failed gate result does not use the first failed reason")
    return TaskPlanResultProof(
        artifacts=artifacts,
        worker_input=None,
        worker_failed=False,
    )


def _require_reader(
    reader: TaskPlanGateEvidenceReaderPort | None,
    task_id: str,
) -> TaskPlanGateEvidenceReaderPort:
    if reader is None:
        raise HarnessValidationError(
            "TaskPlan result proof requires its durable evidence reader",
            code="task_plan_gate_evidence_reader_required",
            details={"task_id": task_id},
        )
    if not isinstance(reader, TaskPlanGateEvidenceReaderPort):
        raise TypeError(
            "gate_evidence_reader must implement TaskPlanGateEvidenceReaderPort"
        )
    return reader


def _validate_receipt_binding(
    definition: object,
    result: TaskResultRecord,
    worker_result: object,
    *,
    require_result_ref: bool,
) -> None:
    receipt = _subagent_receipt(worker_result)
    if definition.subagent_id is None:
        if receipt is not None:
            _fail("non-subagent result carries a subagent receipt")
        if require_result_ref and (
            result.result_ref != worker_result.candidate_result_ref
            or result.output_refs != worker_result.artifacts
        ):
            _fail("accepted result refs differ from the gated worker candidate")
        return
    if receipt is None or (
        receipt.parent_run_id != result.run_id
        or receipt.task_instance_id != result.task_instance_id
        or receipt.attempt != result.attempt
        or result.transcript_ref != receipt.transcript_ref
        or result.transcript_checksum != receipt.transcript_checksum
        or result.subagent_output_ref != receipt.output_ref
        or result.subagent_output_checksum != receipt.output_checksum
        or (require_result_ref and result.result_ref != receipt.output_ref)
    ):
        _fail("subagent result differs from the worker receipt")


def _subagent_receipt(worker_result: object) -> SubAgentTranscriptReceipt | None:
    evidence = tuple(
        item
        for item in worker_result.evidence
        if item.evidence_type == _SUBAGENT_ATTEMPT_EVIDENCE_TYPE
    )
    if not evidence:
        return None
    if len(evidence) != 1:
        _fail("gated worker candidate has conflicting subagent receipts")
    payload = evidence[0].payload
    if not isinstance(payload, Mapping):
        _fail("gated worker candidate has an invalid subagent receipt")
    return SubAgentTranscriptReceipt.from_dict(payload)


def _fail(message: str) -> None:
    raise HarnessValidationError(message, code="task_plan_result_proof_mismatch")


__all__ = ["TaskPlanResultProof", "verify_task_result_proof"]
