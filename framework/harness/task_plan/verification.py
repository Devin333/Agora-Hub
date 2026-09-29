"""Deterministic verification for one resolved TaskPlan task result.

The planner and worker may propose content only.  This module turns a worker
result into a durable result record only after the exact gate references pinned
in ``ResolvedTaskSpec`` have produced bounded evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from framework.harness.ref_results import HarnessResultRefAuthority

from framework.harness.artifacts import ArtifactReferenceVerifierPort
from framework.harness.context.models import ContextEnvelope
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_RESULT,
    RefAccessPolicy,
    RefAuthority,
    RefDescriptor,
    RefResolutionPort,
    normalize_ref_descriptors,
    validate_ref_configuration,
)
from framework.harness.subagents.transcript import (
    SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3,
    SubAgentAttemptIdentity,
    SubAgentOutputDocument,
    SubAgentTranscriptReceipt,
    SubAgentTranscriptStorePort,
)
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_reference,
    identifier,
    stable_text_tuple,
    thaw_mapping,
)
from framework.harness.task_plan.gate_evidence import (
    TaskPlanGateArtifactRefs,
    TaskPlanGateArtifactWriterPort,
    TaskPlanGateEvidence,
)
from framework.harness.task_plan.models import (
    ResolvedTaskSpec,
    TaskInstance,
    TaskLifecycle,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.store import TaskPlanEvent, TaskResultRecord
from framework.harness.workers.result import (
    HarnessWorkerEvidence,
    HarnessWorkerResult,
    HarnessWorkerStatus,
)
from framework.shared.graph_identity import GraphExecutionIdentity


SUBAGENT_ATTEMPT_EVIDENCE_TYPE = "subagent_attempt"


@dataclass(frozen=True, slots=True)
class TaskPlanGateRequest:
    """Immutable, capability-free input passed to a task gate implementation."""

    task: ResolvedTaskSpec
    instance: TaskInstance
    worker_result: HarnessWorkerResult
    input_checksum: str

    def __post_init__(self) -> None:
        if not isinstance(self.task, ResolvedTaskSpec):
            raise TypeError("task must be ResolvedTaskSpec")
        if not isinstance(self.instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        if not isinstance(self.worker_result, HarnessWorkerResult):
            raise TypeError("worker_result must be HarnessWorkerResult")
        object.__setattr__(self, "input_checksum", checksum(self.input_checksum, "input_checksum"))


TaskPlanGateCallable = Callable[[TaskPlanGateRequest], bool | TaskPlanGateEvidence]


@runtime_checkable
class TaskPlanGateEvaluatorPort(Protocol):
    def evaluate(self, gate_ref: str, request: TaskPlanGateRequest) -> TaskPlanGateEvidence: ...


class TaskPlanGateRegistry(TaskPlanGateEvaluatorPort):
    """Exact-version registry for deterministic gate implementations."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._gates: dict[str, TaskPlanGateCallable] = {}

    @property
    def refs(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._gates))

    def register(
        self,
        gate_ref: str,
        evaluator: TaskPlanGateCallable,
        *,
        deterministic: bool = False,
    ) -> None:
        ref = exact_reference(gate_ref, "gate_ref")
        if not callable(evaluator):
            raise TypeError("evaluator must be callable")
        if deterministic is not True:
            raise HarnessValidationError(
                "TaskPlan gate registration requires deterministic=True",
                code="task_plan_gate_not_deterministic",
                details={"gate_ref": ref},
            )
        with self._lock:
            if ref in self._gates:
                raise HarnessValidationError(
                    "TaskPlan gate is already registered",
                    code="task_plan_duplicate_gate",
                    details={"gate_ref": ref},
                )
            self._gates[ref] = evaluator

    def evaluate(self, gate_ref: str, request: TaskPlanGateRequest) -> TaskPlanGateEvidence:
        ref = exact_reference(gate_ref, "gate_ref")
        if not isinstance(request, TaskPlanGateRequest):
            raise TypeError("request must be TaskPlanGateRequest")
        with self._lock:
            evaluator = self._gates.get(ref)
        if evaluator is None:
            raise HarnessValidationError(
                "exact TaskPlan gate is unavailable",
                code="task_plan_gate_unavailable",
                details={"gate_ref": ref},
            )
        value = evaluator(request)
        if isinstance(value, TaskPlanGateEvidence):
            evidence = value
            if (
                evidence.gate_ref != ref
                or evidence.input_checksum != request.input_checksum
                or evidence.result_checksum
                != canonical_payload_checksum(request.worker_result.candidate_payload())
            ):
                raise HarnessValidationError(
                    "TaskPlan gate returned mismatched evidence identity",
                    code="task_plan_gate_evidence_mismatch",
                    details={"gate_ref": ref},
                )
            return evidence
        if not isinstance(value, bool):
            raise HarnessValidationError(
                "TaskPlan gate must return bool or TaskPlanGateEvidence",
                code="task_plan_gate_result_invalid",
                details={"gate_ref": ref},
            )
        return TaskPlanGateEvidence(
            gate_ref=ref,
            input_checksum=request.input_checksum,
            result_checksum=canonical_payload_checksum(request.worker_result.candidate_payload()),
            passed=value,
            reason_code=None if value else "task_gate_failed",
        )


@dataclass(frozen=True, slots=True)
class TaskPlanResultVerificationRequest:
    plan: ValidatedTaskPlan
    task: ResolvedTaskSpec
    instance: TaskInstance
    worker_result: HarnessWorkerResult
    execution_identity: GraphExecutionIdentity | None = None
    admission_event: TaskPlanEvent | None = None
    spawn_intent: TaskPlanEvent | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if not isinstance(self.task, ResolvedTaskSpec):
            raise TypeError("task must be ResolvedTaskSpec")
        if not isinstance(self.instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        if not isinstance(self.worker_result, HarnessWorkerResult):
            raise TypeError("worker_result must be HarnessWorkerResult")
        _require_plan_task_instance_identity(self.plan, self.task, self.instance)
        if self.admission_event is None and self.spawn_intent is not None:
            raise HarnessValidationError("spawn intent requires its wave admission", code="task_plan_result_admission_mismatch")
        if self.admission_event is not None:
            from framework.harness.task_plan.parallel import DispatchGroup, DispatchWave, spawn_operation_key
            from framework.harness.task_plan.parallel_admission import validate_group_plan_binding

            event = self.admission_event
            if not isinstance(event, TaskPlanEvent) or event.event_type != "TASK_WAVE_ADMITTED" or not event.matches_contract_identity(self.plan):
                raise HarnessValidationError("verification requires a canonical wave admission", code="task_plan_result_admission_mismatch")
            admission = thaw_mapping(event.payload)
            group = DispatchGroup.from_dict(admission["group"])
            wave = DispatchWave.from_dict(admission["wave"])
            validate_group_plan_binding(event.payload["group"], self.plan)
            reservation = next((item for item in wave.reservations if item.task_id == self.instance.task_id), None)
            if (wave.group_id != group.group_id or reservation is None
                or reservation.idempotency_key != self.instance.idempotency_key
                or dict(reservation.budget) != self.instance.budget_snapshot.to_dict()):
                raise HarnessValidationError("verification differs from its wave reservation", code="task_plan_result_admission_mismatch")
            if wave.execution_mode == "SUPERVISED":
                intent = self.spawn_intent
                expected = {
                    "group_id": group.group_id, "wave_id": wave.wave_id,
                    "task_id": self.instance.task_id, "task_instance_id": self.instance.task_instance_id,
                    "attempt": self.instance.attempt,
                    "operation_key": spawn_operation_key(group.group_id, wave.wave_id, self.instance.task_instance_id, self.instance.attempt),
                }
                if (not isinstance(intent, TaskPlanEvent) or intent.event_type != "TASK_ATTEMPT_SPAWN_INTENT"
                    or not intent.matches_contract_identity(self.plan)
                    or any(intent.payload.get(key) != value for key, value in expected.items())):
                    raise HarnessValidationError("verification differs from its admitted spawn intent", code="task_plan_result_admission_mismatch")
            elif self.spawn_intent is not None:
                raise HarnessValidationError("serial verification cannot fabricate a spawn intent", code="task_plan_result_admission_mismatch")
        if self.execution_identity is not None:
            if not isinstance(self.execution_identity, GraphExecutionIdentity):
                raise TypeError("execution_identity must be GraphExecutionIdentity")
            if (
                self.execution_identity.run_id != self.plan.run_id
                or self.execution_identity.graph_id != self.plan.graph_id
                or self.execution_identity.graph_version != self.plan.graph_version
                or self.execution_identity.graph_ref != self.plan.graph_ref
                or self.execution_identity.graph_checksum != self.plan.graph_checksum
            ):
                raise HarnessValidationError(
                    "TaskPlan verification execution identity is outside its accepted Graph",
                    code="task_plan_execution_identity_mismatch",
                )


class TaskPlanResultVerifier:
    """Validate identity, usage boundaries and exact task gates before commit."""

    def __init__(
        self,
        gates: TaskPlanGateEvaluatorPort | None = None,
        *,
        transcript_store: SubAgentTranscriptStorePort | None = None,
        artifact_reference_verifier: ArtifactReferenceVerifierPort | None = None,
        ref_authority: RefAuthority | None = None,
        ref_policy: RefAccessPolicy | None = None,
        ref_resolution: RefResolutionPort | None = None,
        ref_descriptors: Mapping[str, RefDescriptor] | None = None,
        result_ref_authority: HarnessResultRefAuthority | None = None,
        gate_artifact_writer: TaskPlanGateArtifactWriterPort | None = None,
    ) -> None:
        self._gates = gates or TaskPlanGateRegistry()
        if not isinstance(self._gates, TaskPlanGateEvaluatorPort):
            raise TypeError("gates must implement TaskPlanGateEvaluatorPort")
        if transcript_store is not None and not isinstance(
            transcript_store,
            SubAgentTranscriptStorePort,
        ):
            raise TypeError("transcript_store must implement SubAgentTranscriptStorePort")
        self._transcript_store = transcript_store
        if artifact_reference_verifier is not None and not isinstance(
            artifact_reference_verifier,
            ArtifactReferenceVerifierPort,
        ):
            raise TypeError(
                "artifact_reference_verifier must implement "
                "ArtifactReferenceVerifierPort"
            )
        self._artifact_reference_verifier = artifact_reference_verifier
        normalized_descriptors = normalize_ref_descriptors(ref_descriptors)
        validate_ref_configuration(
            ref_authority,
            ref_policy,
            ref_resolution,
            normalized_descriptors,
        )
        self._ref_authority = ref_authority
        self._ref_policy = ref_policy
        self._ref_resolution = ref_resolution
        self._ref_descriptors = normalized_descriptors
        if result_ref_authority is not None:
            from framework.harness.ref_results import HarnessResultRefAuthority

            if not isinstance(result_ref_authority, HarnessResultRefAuthority) or result_ref_authority.transcript_store is not transcript_store:
                raise TypeError("result_ref_authority must own this transcript store")
            if ref_authority is not None:
                raise ValueError("static and execution-bound result authority cannot be combined")
        self.result_ref_authority = result_ref_authority
        if gate_artifact_writer is not None and not isinstance(
            gate_artifact_writer,
            TaskPlanGateArtifactWriterPort,
        ):
            raise TypeError(
                "gate_artifact_writer must implement "
                "TaskPlanGateArtifactWriterPort"
            )
        self._gate_artifact_writer = gate_artifact_writer

    @property
    def gate_registry(self) -> TaskPlanGateEvaluatorPort:
        """Expose the deterministic gate owner for production composition."""
        return self._gates

    @property
    def registered_gate_refs(self) -> tuple[str, ...]:
        """Expose the concrete gate registry for PLAN preflight validation."""

        refs = getattr(self._gates, "refs", ())
        return tuple(refs)

    @property
    def transcript_store(self) -> SubAgentTranscriptStorePort | None:
        """Expose the bound transcript owner for production composition checks."""

        return self._transcript_store

    @property
    def artifact_reference_verifier(self) -> ArtifactReferenceVerifierPort | None:
        """Expose the artifact-reference verifier for production composition checks."""

        return self._artifact_reference_verifier

    @property
    def ref_authority(self) -> RefAuthority | None:
        """Expose the shared reference authority for composition checks."""

        return self._ref_authority

    @property
    def gate_artifact_writer(self) -> TaskPlanGateArtifactWriterPort | None:
        """Expose the immutable owner required before result construction."""

        return self._gate_artifact_writer

    def verify(
        self,
        result: HarnessWorkerResult,
        *,
        task: ResolvedTaskSpec,
        request: TaskPlanResultVerificationRequest,
    ) -> TaskResultRecord:
        if not isinstance(request, TaskPlanResultVerificationRequest):
            raise TypeError("request must be TaskPlanResultVerificationRequest")
        if request.task != task or request.worker_result is not result:
            raise HarnessValidationError(
                "TaskPlan verification request result does not match verifier input",
                code="task_plan_result_identity_mismatch",
            )
        plan = request.plan
        instance = request.instance
        if self._ref_authority is not None:
            self._ref_authority.require_scope(
                self._ref_policy,
                run_id=plan.run_id,
                stage_id=plan.stage_id,
            )
        if result.effect_intent is not None:
            raise HarnessValidationError(
                "dynamic TaskPlan workers cannot propose side effects",
                code="task_plan_result_side_effect_forbidden",
            )

        receipt, output_document = self._verify_subagent_evidence(
            result,
            plan=plan,
            task=task,
            instance=instance,
            execution_identity=request.execution_identity,
        )
        if receipt is None:
            if self.result_ref_authority is not None:
                raise HarnessValidationError("result authority requires an admitted SubAgent attempt", code="REF_SNAPSHOT_MISSING")
            self._authorize_result_ref(
                result.candidate_result_ref,
                expected_checksum=result.candidate_result_ref,
            )
            self._authorize_result_refs(result.artifacts)

        if result.status is not HarnessWorkerStatus.SUCCEEDED:
            if self._gate_artifact_writer is None:
                raise HarnessValidationError(
                    "TaskPlan worker result artifact owner is unavailable",
                    code="task_plan_gate_artifact_owner_unavailable",
                )
            worker_result_proof_ref = (
                self._gate_artifact_writer.persist_worker_result_input(
                    plan,
                    instance,
                    result,
                )
            )
            expected_proof_ref = canonical_payload_checksum(
                {
                    "instance": instance.checksum_projection(),
                    "worker_result": result.candidate_payload(),
                }
            )
            if worker_result_proof_ref != expected_proof_ref:
                raise HarnessValidationError(
                    "TaskPlan worker result artifact owner returned a conflicting ref",
                    code="task_plan_gate_artifact_ref_mismatch",
                )
            return _failure_record(
                plan,
                task,
                instance,
                result,
                "task_worker_failed",
                worker_result_proof_ref=worker_result_proof_ref,
                receipt=receipt,
            )

        _validate_worker_usage(result.metrics, task)
        _validate_worker_boundary_diagnostics(result.diagnostics, task)
        input_checksum = canonical_payload_checksum(
            {
                "instance": instance.checksum_projection(),
                "worker_result": result.candidate_payload(),
            }
        )
        gate_request = TaskPlanGateRequest(
            task=task,
            instance=instance,
            worker_result=result,
            input_checksum=input_checksum,
        )
        if self._gate_artifact_writer is None:
            raise HarnessValidationError(
                "TaskPlan gate artifact owner is unavailable",
                code="task_plan_gate_artifact_owner_unavailable",
            )
        evidence = tuple(
            self._gates.evaluate(gate_ref, gate_request)
            for gate_ref in task.gate_refs
        )
        persisted = self._gate_artifact_writer.persist_gate_verification(
            plan,
            instance,
            result,
            evidence,
        )
        if not isinstance(persisted, TaskPlanGateArtifactRefs) or (
            persisted.input_checksum != input_checksum
            or persisted.evidence_checksums
            != tuple(item.evidence_checksum for item in evidence)
        ):
            raise HarnessValidationError(
                "TaskPlan gate artifact owner returned conflicting refs",
                code="task_plan_gate_artifact_ref_mismatch",
            )
        failed = next((item for item in evidence if not item.passed), None)
        if failed is not None:
            return _failure_record(
                plan,
                task,
                instance,
                result,
                failed.reason_code or "task_gate_failed",
                verified_gate_refs=tuple(item.gate_ref for item in evidence),
                gate_evidence_refs=persisted.evidence_checksums,
                receipt=receipt,
            )
        return TaskResultRecord.for_plan(
            plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            status=TaskLifecycle.SUCCEEDED,
            result_ref=(
                receipt.output_ref if receipt is not None else result.candidate_result_ref
            ),
            output_refs=(
                output_document.artifact_refs
                if output_document is not None
                else result.artifacts
            ),
            output_roles=(task.output_role,),
            output_schema_ref=task.task.output_contract.schema_ref,
            usage=dict(result.metrics),
            verified_gate_refs=tuple(item.gate_ref for item in evidence),
            gate_evidence_refs=persisted.evidence_checksums,
            transcript_ref=receipt.transcript_ref if receipt else None,
            transcript_checksum=receipt.transcript_checksum if receipt else None,
            subagent_output_ref=receipt.output_ref if receipt else None,
            subagent_output_checksum=receipt.output_checksum if receipt else None,
        )

    def _verify_subagent_evidence(
        self,
        result: HarnessWorkerResult,
        *,
        plan: ValidatedTaskPlan,
        task: ResolvedTaskSpec,
        instance: TaskInstance,
        execution_identity: GraphExecutionIdentity | None,
    ) -> tuple[SubAgentTranscriptReceipt | None, SubAgentOutputDocument | None]:
        entries = tuple(
            item
            for item in result.evidence
            if item.evidence_type == SUBAGENT_ATTEMPT_EVIDENCE_TYPE
        )
        if task.subagent_id is None:
            if entries:
                raise HarnessValidationError(
                    "non-subagent task must not carry subagent attempt evidence",
                    code="task_plan_unexpected_subagent_evidence",
                )
            return None, None
        if self._transcript_store is None:
            raise HarnessValidationError(
                "subagent TaskPlan verification requires a transcript store",
                code="task_plan_subagent_transcript_store_required",
            )
        if execution_identity is None:
            raise HarnessValidationError(
                "subagent TaskPlan verification requires physical Graph identity",
                code="task_plan_execution_identity_required",
            )
        if len(entries) != 1:
            raise HarnessValidationError(
                "subagent TaskPlan result requires exactly one attempt receipt",
                code="task_plan_subagent_evidence_required",
            )
        receipt = _receipt_from_evidence(entries[0])
        self._authorize_result_ref(
            receipt.output_ref,
            expected_checksum=receipt.output_checksum,
        )
        self._authorize_result_ref(
            receipt.transcript_ref,
            expected_checksum=receipt.transcript_checksum,
        )
        self._authorize_result_refs(result.artifacts)
        store = self._transcript_store
        if self.result_ref_authority is not None:
            store = authorized_task_result_store(
                self.result_ref_authority, plan=plan, task=task,
                instance=instance, execution_identity=execution_identity,
            )
            store.authorize_artifacts(result.artifacts)
        store.verify(receipt)
        transcript = store.read(receipt.transcript_ref)
        output = store.read_output(receipt.output_ref)
        identity = transcript.identity
        if (
            not _subagent_identity_matches_plan(identity, plan, task, instance)
            or not _subagent_identity_matches_execution(identity, execution_identity)
            or identity.invocation_id != receipt.invocation_id
            or output.identity != identity
        ):
            raise HarnessValidationError(
                "subagent evidence does not match accepted TaskPlan attempt",
                code="task_plan_subagent_evidence_mismatch",
            )
        if canonical_payload_checksum(output.output) != canonical_payload_checksum(result.output):
            raise HarnessValidationError(
                "subagent durable output does not match worker result",
                code="task_plan_subagent_output_mismatch",
            )
        if output.artifact_refs != result.artifacts:
            raise HarnessValidationError(
                "subagent durable artifact refs do not match worker result",
                code="task_plan_subagent_output_mismatch",
            )
        _verify_artifact_references(
            output.artifact_refs,
            expected_run_id=instance.run_id,
            verifier=self._artifact_reference_verifier,
        )
        if (
            result.status is HarnessWorkerStatus.SUCCEEDED
            and output.status != "succeeded"
        ) or (
            result.status is not HarnessWorkerStatus.SUCCEEDED
            and output.status == "succeeded"
        ):
            raise HarnessValidationError(
                "subagent durable status does not match worker result",
                code="task_plan_subagent_output_mismatch",
            )
        return receipt, output

    def _authorize_result_refs(self, refs: tuple[str, ...]) -> None:
        for ref in dict.fromkeys(ref for ref in refs if ref):
            self._authorize_result_ref(ref)

    def _authorize_result_ref(
        self,
        ref: str,
        *,
        expected_checksum: str | None = None,
    ) -> None:
        if self._ref_authority is None:
            return
        self._ref_authority.authorize_ref(
            ref,
            self._ref_policy,
            resolver=self._ref_resolution,
            descriptors=self._ref_descriptors,
            expected_kind=REF_KIND_RESULT,
            expected_checksum=expected_checksum,
        )


def authorized_task_result_store(
    authority: HarnessResultRefAuthority, *, plan: ValidatedTaskPlan,
    task: ResolvedTaskSpec, instance: TaskInstance,
    execution_identity: GraphExecutionIdentity,
):
    _require_plan_task_instance_identity(plan, task, instance)
    identity = authority.accepted_attempt(
        execution=execution_identity, stage_id=plan.stage_id,
        stage_binding_checksum=plan.stage_binding_checksum,
        task_instance_id=instance.task_instance_id, attempt=instance.attempt,
        task_policy_checksum=plan.policy_checksum,
    )
    if not _subagent_identity_matches_plan(identity, plan, task, instance) or not _subagent_identity_matches_execution(identity, execution_identity):
        raise HarnessValidationError("result authority differs from accepted TaskPlan", code="REF_SNAPSHOT_BINDING_MISMATCH")
    return authority.for_attempt(identity)


def _verify_artifact_references(
    refs: tuple[str, ...],
    *,
    expected_run_id: str,
    verifier: ArtifactReferenceVerifierPort | None,
) -> None:
    if not refs:
        return
    if verifier is None:
        raise HarnessValidationError(
            "subagent artifact refs require a canonical verifier",
            code="task_plan_subagent_artifact_verifier_required",
        )
    for index, ref in enumerate(refs):
        try:
            verifier.verify_artifact_ref(ref, expected_run_id=expected_run_id)
        except Exception as exc:
            raise HarnessValidationError(
                "subagent artifact ref could not be verified by its canonical owner",
                code="task_plan_subagent_artifact_unverified",
                details={"artifact_index": index},
            ) from exc


def _failure_record(
    plan: ValidatedTaskPlan,
    task: ResolvedTaskSpec,
    instance: TaskInstance,
    result: HarnessWorkerResult,
    reason_code: str,
    *,
    verified_gate_refs: tuple[str, ...] = (),
    gate_evidence_refs: tuple[str, ...] = (),
    worker_result_proof_ref: str | None = None,
    receipt: SubAgentTranscriptReceipt | None = None,
) -> TaskResultRecord:
    return TaskResultRecord.for_plan(
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        status=TaskLifecycle.FAILED,
        output_schema_ref=task.task.output_contract.schema_ref,
        usage=dict(result.metrics),
        error_code=identifier(reason_code, "reason_code"),
        verified_gate_refs=verified_gate_refs,
        gate_evidence_refs=gate_evidence_refs,
        worker_result_proof_ref=worker_result_proof_ref,
        transcript_ref=receipt.transcript_ref if receipt else None,
        transcript_checksum=receipt.transcript_checksum if receipt else None,
        subagent_output_ref=receipt.output_ref if receipt else None,
        subagent_output_checksum=receipt.output_checksum if receipt else None,
    )


def subagent_attempt_evidence(
    receipt: SubAgentTranscriptReceipt,
) -> HarnessWorkerEvidence:
    if not isinstance(receipt, SubAgentTranscriptReceipt):
        raise TypeError("receipt must be SubAgentTranscriptReceipt")
    return HarnessWorkerEvidence(
        evidence_type=SUBAGENT_ATTEMPT_EVIDENCE_TYPE,
        payload=receipt.to_dict(),
    )


def _receipt_from_evidence(
    evidence: HarnessWorkerEvidence,
) -> SubAgentTranscriptReceipt:
    try:
        return SubAgentTranscriptReceipt.from_dict(evidence.payload)
    except HarnessValidationError:
        raise
    except Exception as exc:
        raise HarnessValidationError(
            "subagent worker evidence receipt is invalid",
            code="task_plan_subagent_evidence_mismatch",
        ) from exc


def task_plan_subagent_attempt_identity(
    plan: ValidatedTaskPlan,
    instance: TaskInstance,
    *,
    invocation_id: str,
    child_run_id: str,
    subagent_id: str,
    context_pack: ContextEnvelope | None = None,
) -> SubAgentAttemptIdentity:
    """Derive one SubAgent attempt identity from accepted TaskPlan authority."""

    if not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("plan must be ValidatedTaskPlan")
    if not isinstance(instance, TaskInstance):
        raise TypeError("instance must be TaskInstance")
    definition = next(
        (item for item in plan.tasks if item.task_id == instance.task_id),
        None,
    )
    if definition is None:
        raise HarnessValidationError(
            "SubAgent attempt task is outside the accepted plan",
            code="task_plan_result_identity_mismatch",
        )
    _require_plan_task_instance_identity(plan, definition, instance)
    if definition.subagent_id is None or definition.subagent_id != subagent_id:
        raise HarnessValidationError(
            "SubAgent attempt identity does not match the accepted capability binding",
            code="task_plan_subagent_evidence_mismatch",
        )
    common: dict[str, Any] = {
        "invocation_id": invocation_id,
        "parent_run_id": plan.run_id,
        "child_run_id": child_run_id,
        "stage_id": plan.stage_id,
        "task_id": instance.task_id,
        "task_instance_id": instance.task_instance_id,
        "attempt": instance.attempt,
        "subagent_id": subagent_id,
    }
    expected_context_fields = {
            "parent_run_id": plan.run_id,
            "graph_id": plan.graph_id,
            "graph_version": plan.graph_version,
            "graph_ref": plan.graph_ref,
            "graph_schema_version": plan.graph_schema_version,
            "compiler_version": plan.compiler_version,
            "condition_policy_version": plan.condition_policy_version,
            "graph_checksum": plan.graph_checksum,
            "stage_id": plan.stage_id,
            "stage_binding_checksum": plan.stage_binding_checksum,
            "stage_identity_schema": plan.stage_identity_schema,
            "stage_identity_checksum": plan.stage_identity_checksum,
            "plan_id": plan.plan_id,
            "plan_version": plan.version,
            "plan_checksum": plan.plan_checksum,
            "task_id": instance.task_id,
            "task_definition_checksum": definition.task_definition_checksum,
            "task_instance_id": instance.task_instance_id,
            "attempt": instance.attempt,
        }
    if (
        not isinstance(context_pack, ContextEnvelope)
        or not context_pack.is_graph_only
        or context_pack.phase != "EXECUTE"
        or not context_pack.matches_graph_fields(expected_context_fields)
        or context_pack.checksum is None
    ):
        raise HarnessValidationError(
            "Graph-only SubAgent attempt requires its exact execution context",
            code="task_plan_result_identity_mismatch",
        )
    graph_identity = context_pack.graph_identity
    if (
        graph_identity is None
        or graph_identity.node_id is None
        or graph_identity.node_instance_id is None
        or graph_identity.activity_id is None
        or graph_identity.activity_attempt is None
    ):
        raise HarnessValidationError(
            "Graph-only SubAgent attempt requires physical Graph identity",
            code="task_plan_execution_identity_required",
        )
    common.update(
        {
                "schema_version": SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3,
                "graph_id": plan.graph_id,
                "graph_version": plan.graph_version,
                "graph_ref": plan.graph_ref,
                "graph_schema_version": plan.graph_schema_version,
                "compiler_version": plan.compiler_version,
                "condition_policy_version": plan.condition_policy_version,
                "graph_checksum": plan.graph_checksum,
                "stage_binding_checksum": plan.stage_binding_checksum,
                "stage_identity_schema": plan.stage_identity_schema,
                "stage_identity_checksum": plan.stage_identity_checksum,
                "plan_id": plan.plan_id,
                "plan_version": plan.version,
                "plan_checksum": plan.plan_checksum,
                "task_definition_checksum": definition.task_definition_checksum,
                "context_envelope_id": context_pack.envelope_id,
                "context_envelope_checksum": context_pack.checksum,
                "node_id": graph_identity.node_id,
                "node_instance_id": graph_identity.node_instance_id,
                "activity_id": graph_identity.activity_id,
                "activity_attempt": graph_identity.activity_attempt,
        }
    )
    return SubAgentAttemptIdentity(**common)


def _subagent_identity_matches_plan(
    identity: SubAgentAttemptIdentity,
    plan: ValidatedTaskPlan,
    task: ResolvedTaskSpec,
    instance: TaskInstance,
) -> bool:
    if (
        identity.parent_run_id != plan.run_id
        or identity.stage_id != plan.stage_id
        or identity.task_id != instance.task_id
        or identity.task_instance_id != instance.task_instance_id
        or identity.attempt != instance.attempt
        or identity.subagent_id != task.subagent_id
    ):
        return False
    graph_fields = (
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_schema_version",
        "compiler_version",
        "condition_policy_version",
        "graph_checksum",
        "stage_binding_checksum",
        "stage_identity_schema",
        "stage_identity_checksum",
    )
    if not all(
        getattr(identity, field_name) == getattr(plan, field_name)
        for field_name in graph_fields
    ):
        return False
    if identity.schema_version != SUBAGENT_ATTEMPT_IDENTITY_SCHEMA_V3:
        return False
    return (
        identity.plan_id == plan.plan_id
        and identity.plan_version == plan.version
        and identity.plan_checksum == plan.plan_checksum
        and identity.task_definition_checksum == task.task_definition_checksum
    )


def _subagent_identity_matches_execution(
    identity: SubAgentAttemptIdentity,
    execution_identity: GraphExecutionIdentity,
) -> bool:
    return (
        identity.parent_run_id == execution_identity.run_id
        and identity.graph_id == execution_identity.graph_id
        and identity.graph_version == execution_identity.graph_version
        and identity.graph_ref == execution_identity.graph_ref
        and identity.graph_checksum == execution_identity.graph_checksum
        and identity.node_id == execution_identity.node_id
        and identity.node_instance_id == execution_identity.node_instance_id
        and identity.activity_id == execution_identity.activity_id
        and identity.activity_attempt == execution_identity.attempt
    )


def _require_plan_task_instance_identity(
    plan: ValidatedTaskPlan,
    task: ResolvedTaskSpec,
    instance: TaskInstance,
) -> None:
    definition = next(
        (item for item in plan.tasks if item.task_id == task.task_id),
        None,
    )
    if definition != task:
        raise HarnessValidationError(
            "TaskPlan result verification task is outside the accepted plan",
            code="task_plan_result_identity_mismatch",
        )
    expected_instance = task_instance_for_attempt(
        plan,
        task.task_id,
        instance.attempt,
        task_instance_id=instance.task_instance_id,
    )
    if expected_instance != instance:
        raise HarnessValidationError(
            "TaskPlan result verification attempt is outside the accepted plan",
            code="task_plan_result_identity_mismatch",
        )
    _require_task_instance_identity(task, instance)


def _require_task_instance_identity(task: ResolvedTaskSpec, instance: TaskInstance) -> None:
    if (
        task.task_id != instance.task_id
        or task.task_definition_checksum != instance.task_definition_checksum
        or task.worker_ref != instance.worker_ref
    ):
        raise HarnessValidationError(
            "TaskPlan result verification identity is outside the accepted task",
            code="task_plan_result_identity_mismatch",
        )


def _validate_worker_usage(value: Mapping[str, Any], task: ResolvedTaskSpec) -> None:
    from framework.harness.task_plan.budget_ledger import result_budget_usage

    result_budget_usage(value, task.normalized_budget.to_dict())


def _validate_worker_boundary_diagnostics(value: Mapping[str, Any], task: ResolvedTaskSpec) -> None:
    if not isinstance(value, Mapping):
        raise HarnessValidationError("worker diagnostics must be an object", code="task_plan_result_invalid")
    used_tools = stable_text_tuple(value.get("used_tools", ()), "used_tools")
    used_memory = stable_text_tuple(value.get("used_memory_namespaces", ()), "used_memory_namespaces")
    denied_tools = sorted(set(used_tools) - set(task.allowed_tools))
    denied_memory = sorted(set(used_memory) - set(task.allowed_memory_namespaces))
    if denied_tools or denied_memory:
        raise HarnessValidationError(
            "dynamic worker reported usage outside its pinned boundary",
            code="task_plan_result_boundary_violation",
            details={"denied_tools": denied_tools, "denied_memory_namespaces": denied_memory},
        )


__all__ = [
    "TaskPlanGateCallable",
    "TaskPlanGateEvidence",
    "TaskPlanGateEvaluatorPort",
    "TaskPlanGateRegistry",
    "TaskPlanGateRequest",
    "TaskPlanResultVerificationRequest",
    "TaskPlanResultVerifier",
    "subagent_attempt_evidence",
    "task_plan_subagent_attempt_identity",
]
