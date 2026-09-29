from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, runtime_checkable

if TYPE_CHECKING:
    from framework.harness.task_plan.attempt_history import TaskAttemptHistoryRecord
    from framework.harness.task_plan.capacity import CapacityScopeSnapshot, PoolReservation

from framework.agent.artifacts.models import ArtifactRef, ArtifactWriteRequest
from framework.events.canonical import (
    BusinessContext,
    ProducerIdentity,
    StoredEvent,
    thaw_canonical_json,
)
from framework.events.errors import (
    EventContractError,
    EventIdentityCollisionError,
    EventStoreContentionError,
    EventStreamVersionConflictError,
)
from framework.events.projection import (
    GRAPH_EVENT_CONTEXT_EXTENSION,
    GraphEventContext,
    GraphEventExecutionVersion,
    graph_event_context,
)
from framework.shared.graph_identity import GraphExecutionIdentity, GraphRunIdentity
from framework.events.ports import (
    TransactionalStateReaderPort,
    TransactionalStateRuntimePort,
)
from framework.events.runtime.models import StreamReadRequest, TransactionalStateSnapshot
from framework.events.runtime.publisher import EventPublishRequest
from framework.events.schema.security import SecurityClassification
from framework.harness.control_plane.activity_execution import (
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_json,
    canonical_payload_checksum,
    checksum,
    identifier,
)
from framework.harness.task_plan.submission import (
    CandidateDedupIdentity,
    CandidateSubmission,
    CandidateSubmissionAdmission,
    require_submission_stage_available,
    submissions_from_events,
    validate_submission_event_append,
)
from framework.harness.task_plan.models import (
    PlanCandidate,
    PlanPatch,
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    TaskResultReference,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.gate_evidence import (
    TASK_PLAN_GATE_EVIDENCE_SCHEMA,
    TASK_PLAN_GATE_INPUT_SCHEMA,
    TaskPlanGateArtifactOwnerPort,
    TaskPlanGateArtifactRefs,
    TaskPlanGateArtifactWriterPort,
    TaskPlanGateEvidence,
    TaskPlanGateEvidenceReaderPort,
    TaskPlanGateVerificationArtifacts,
    TaskPlanWorkerResultInputArtifacts,
    normalize_gate_evidence_refs,
)
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from framework.harness.task_plan.store import (
    TASK_PLAN_EVENT_SCHEMAS,
    TASK_PLAN_EVENT_TYPES,
    TaskPlanEvent,
    TaskResultRecord,
    _candidate_event,
    _plan_contains_task_version,
    _plan_event,
    _projection_for_plan,
    _replacement_mapping,
    _validate_patch_transition_targets,
    _require_event_matches_plan,
    _require_live_graph_only,
    _require_subagent_result_evidence,
    _require_projection_transition_identity,
    _result_event,
    _require_same_submission,
    _require_submission_scope,
    _require_initial_plan_submission_binding,
    _settle_result_budget,
    _terminal_result_event,
    _classify_atomic_event_batch_history,
    _validate_capacity_admission_contract,
    _reject_uncoordinated_capacity_transition,
    _validate_wave_admission_projection_contract,
    _validate_logical_readiness_projection_contract,
    _validate_queue_admission_projection_contract,
    _validate_wave_completion_projection_contract,
    _validate_atomic_event_batch,
    _validate_transition_projections,
    _validate_result_usage,
)
from framework.shared.time import utc_now


TASK_PLAN_STORAGE_EXTENSION = "task_plan_storage"
TASK_PLAN_STORAGE_SCHEMA = "newsroom.harness-task-plan-storage/v1"
TASK_PLAN_EVENT_SOURCE = "framework.harness.task_plan"
TASK_PLAN_CAPACITY_STATE_NAMESPACE = "newsroom.harness.task-plan.capacity/v1"
TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE = (
    "newsroom.harness.task-plan.parent-execution-context/v1"
)
_MAX_EVENT_APPEND_RETRIES = 8
_EVENT_PAGE_SIZE = 500


@runtime_checkable
class TaskPlanArtifactStorePort(Protocol):
    """Existing immutable artifact boundary used by durable TaskPlan state."""

    def write(self, artifact: ArtifactWriteRequest) -> ArtifactRef: ...

    def read(self, artifact_ref: ArtifactRef) -> bytes: ...

    def exists(self, artifact_ref: ArtifactRef) -> bool: ...


@dataclass(frozen=True, slots=True)
class _DocumentReference:
    kind: str
    domain_ref: str
    artifact_id: str
    run_id: str
    path: str
    content_checksum: str
    size_bytes: int
    content_type: str = "application/json"
    schema: str = TASK_PLAN_STORAGE_SCHEMA

    def __post_init__(self) -> None:
        if self.kind not in {
            "candidate",
            "gate_evidence",
            "gate_input",
            "patch",
            "plan",
            "projection",
            "result",
        }:
            raise HarnessValidationError(
                "TaskPlan artifact kind is unsupported",
                code="task_plan_artifact_kind_unsupported",
                details={"kind": str(self.kind)},
            )
        object.__setattr__(self, "domain_ref", checksum(self.domain_ref, "domain_ref"))
        object.__setattr__(self, "run_id", identifier(self.run_id, "run_id"))
        if not isinstance(self.artifact_id, str) or not self.artifact_id:
            raise HarnessValidationError(
                "TaskPlan artifact id is invalid",
                code="task_plan_artifact_ref_invalid",
            )
        if not isinstance(self.path, str) or not self.path:
            raise HarnessValidationError(
                "TaskPlan artifact path is invalid",
                code="task_plan_artifact_ref_invalid",
            )
        object.__setattr__(
            self,
            "content_checksum",
            checksum(self.content_checksum, "content_checksum"),
        )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise HarnessValidationError(
                "TaskPlan artifact size is invalid",
                code="task_plan_artifact_ref_invalid",
            )
        if self.content_type != "application/json":
            raise HarnessValidationError(
                "TaskPlan artifacts must use application/json",
                code="task_plan_artifact_ref_invalid",
            )
        if self.schema != TASK_PLAN_STORAGE_SCHEMA:
            raise HarnessValidationError(
                "TaskPlan artifact reference schema is unsupported",
                code="task_plan_artifact_schema_unsupported",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "domain_ref": self.domain_ref,
            "artifact_id": self.artifact_id,
            "run_id": self.run_id,
            "path": self.path,
            "content_type": self.content_type,
            "content_checksum": self.content_checksum,
            "size_bytes": self.size_bytes,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> _DocumentReference:
        expected = {
            "schema",
            "kind",
            "domain_ref",
            "artifact_id",
            "run_id",
            "path",
            "content_type",
            "content_checksum",
            "size_bytes",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise HarnessValidationError(
                "TaskPlan artifact reference fields are invalid",
                code="task_plan_artifact_ref_invalid",
            )
        return cls(**dict(value))

    def artifact_ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            run_id=self.run_id,
            artifact_type=f"harness.task-plan.{self.kind}",
            path=self.path,
            content_type=self.content_type,
            size_bytes=self.size_bytes,
            checksum=self.content_checksum.removeprefix("sha256:"),
            redacted=True,
            metadata={
                "task_plan_schema": self.schema,
                "task_plan_kind": self.kind,
                "task_plan_domain_ref": self.domain_ref,
            },
        )


_DocumentT = TypeVar(
    "_DocumentT",
    PlanCandidate,
    PlanPatch,
    TaskPlanProjection,
    TaskResultRecord,
    ValidatedTaskPlan,
)


class DurableTaskPlanStore:
    """TaskPlan adapter over the canonical run stream and artifact store.

    Artifact writes happen before their referencing event.  An interrupted write
    may therefore leave an unreachable immutable artifact, but it can never make
    a TaskPlan transition authoritative.  Reads discover state only through a
    committed event reference and fail closed when that artifact is unavailable.
    """

    def __init__(
        self,
        runtime: TransactionalStateRuntimePort,
        reader: TransactionalStateReaderPort,
        *,
        artifact_store: TaskPlanArtifactStorePort,
        tenant_id: str | None = None,
        security_classification: SecurityClassification | str = (
            SecurityClassification.INTERNAL
        ),
        producer: ProducerIdentity = ProducerIdentity(
            component="framework.harness.task_plan",
            version="1",
        ),
        clock: Callable[[], Any] = utc_now,
    ) -> None:
        if not isinstance(runtime, TransactionalStateRuntimePort):
            raise TypeError("runtime must implement TransactionalStateRuntimePort")
        if not isinstance(reader, TransactionalStateReaderPort):
            raise TypeError("reader must implement TransactionalStateReaderPort")
        if not isinstance(artifact_store, TaskPlanArtifactStorePort):
            raise TypeError("artifact_store must implement TaskPlanArtifactStorePort")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if tenant_id is not None:
            tenant_id = str(tenant_id).strip()
            if not tenant_id:
                raise ValueError("tenant_id must not be blank")
        self._runtime = runtime
        self._reader = reader
        self._artifact_store = artifact_store
        self._tenant_id = tenant_id
        self._security_classification = SecurityClassification(
            security_classification
        )
        self._producer = producer
        self._clock = clock

    def persist_worker_result_input(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
    ) -> str:
        """Persist one verifier-observed terminal worker failure input."""

        _, gate_input, input_checksum, scope = self._validated_worker_result_input(
            plan,
            instance,
            worker_result,
            require_succeeded=False,
        )
        self._put_document(
            "gate_input",
            plan.run_id,
            plan.stage_id,
            input_checksum,
            {
                "schema_version": TASK_PLAN_GATE_INPUT_SCHEMA,
                "scope": scope,
                "gate_input": gate_input,
            },
        )
        return input_checksum

    def persist_gate_verification(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
        evidences: Sequence[TaskPlanGateEvidence],
    ) -> TaskPlanGateArtifactRefs:
        """Persist the exact input and typed output of completed gate evaluation.

        This method does not evaluate gates or grant result authority.  Its
        caller is responsible for invoking the pinned deterministic evaluator
        before crossing this immutable artifact boundary.
        """

        if isinstance(evidences, (str, bytes)) or not isinstance(
            evidences,
            Sequence,
        ):
            raise TypeError("evidences must be a sequence of TaskPlanGateEvidence")
        evidence_items = tuple(evidences)
        if not evidence_items or any(
            not isinstance(item, TaskPlanGateEvidence) for item in evidence_items
        ):
            raise TypeError("evidences must contain TaskPlanGateEvidence values")
        task, gate_input, input_checksum, scope = self._validated_worker_result_input(
            plan,
            instance,
            worker_result,
            require_succeeded=True,
        )
        result_checksum = canonical_payload_checksum(gate_input["worker_result"])
        if tuple(item.gate_ref for item in evidence_items) != task.gate_refs:
            raise HarnessValidationError(
                "gate evidence does not match the accepted task gate order",
                code="task_plan_gate_artifact_evidence_mismatch",
            )
        if any(
            item.input_checksum != input_checksum
            or item.result_checksum != result_checksum
            for item in evidence_items
        ):
            raise HarnessValidationError(
                "gate evidence does not match its persisted input",
                code="task_plan_gate_artifact_evidence_mismatch",
            )

        self._put_document(
            "gate_input",
            plan.run_id,
            plan.stage_id,
            input_checksum,
            {
                "schema_version": TASK_PLAN_GATE_INPUT_SCHEMA,
                "scope": scope,
                "gate_input": gate_input,
            },
        )
        for evidence in evidence_items:
            self._put_document(
                "gate_evidence",
                plan.run_id,
                plan.stage_id,
                evidence.evidence_checksum,
                {
                    "schema_version": TASK_PLAN_GATE_EVIDENCE_SCHEMA,
                    "scope": scope,
                    "gate_evidence": evidence.to_dict(),
                },
            )
        return TaskPlanGateArtifactRefs(
            input_checksum=input_checksum,
            evidence_checksums=tuple(
                item.evidence_checksum for item in evidence_items
            ),
        )

    def _validated_worker_result_input(
        self,
        plan: ValidatedTaskPlan,
        instance: TaskInstance,
        worker_result: HarnessWorkerResult,
        *,
        require_succeeded: bool,
    ) -> tuple[Any, dict[str, Any], str, dict[str, Any]]:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if not isinstance(instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        if not isinstance(worker_result, HarnessWorkerResult):
            raise TypeError("worker_result must be HarnessWorkerResult")
        if not instance.matches_plan_identity(plan):
            raise HarnessValidationError(
                "worker result input is outside the accepted plan scope",
                code="task_plan_gate_artifact_scope_mismatch",
            )
        task = next(
            (item for item in plan.tasks if item.task_id == instance.task_id),
            None,
        )
        if (
            task is None
            or task.task_definition_checksum != instance.task_definition_checksum
            or task.worker_ref != instance.worker_ref
        ):
            raise HarnessValidationError(
                "worker result input binding differs from the accepted plan",
                code="task_plan_gate_artifact_scope_mismatch",
            )
        if worker_result.effect_intent is not None:
            raise HarnessValidationError(
                "worker result input cannot carry a side-effect intent",
                code="task_plan_gate_artifact_input_invalid",
            )
        if require_succeeded != (
            worker_result.status is HarnessWorkerStatus.SUCCEEDED
        ):
            raise HarnessValidationError(
                "worker result input status does not match its artifact purpose",
                code="task_plan_gate_artifact_input_invalid",
            )
        gate_input = {
            "instance": instance.checksum_projection(),
            "worker_result": worker_result.candidate_payload(),
        }
        return (
            task,
            gate_input,
            canonical_payload_checksum(gate_input),
            _gate_artifact_scope(
                self._tenant_id,
                plan.run_id,
                plan.stage_id,
            ),
        )

    def read_worker_result_input(
        self,
        run_id: str,
        stage_id: str,
        input_checksum: str,
    ) -> TaskPlanWorkerResultInputArtifacts:
        """Read one exact scoped worker result input by content checksum."""

        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        input_ref = checksum(input_checksum, "input_checksum")
        instance, worker_result = _decode_gate_input_document(
            self._load_raw_document("gate_input", run, stage, input_ref),
            expected_scope=_gate_artifact_scope(self._tenant_id, run, stage),
            expected_checksum=input_ref,
        )
        return TaskPlanWorkerResultInputArtifacts(
            input_checksum=input_ref,
            instance=instance,
            worker_result=worker_result,
        )

    def read_gate_evidence(
        self,
        run_id: str,
        stage_id: str,
        gate_evidence_refs: Sequence[str],
    ) -> TaskPlanGateVerificationArtifacts:
        """Recover evidence and its input from result-owned evidence refs."""

        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        evidence_refs = normalize_gate_evidence_refs(gate_evidence_refs)
        expected_scope = _gate_artifact_scope(self._tenant_id, run, stage)
        evidence_items = tuple(
            _decode_gate_evidence_document(
                self._load_raw_document(
                    "gate_evidence",
                    run,
                    stage,
                    evidence_checksum,
                ),
                expected_scope=expected_scope,
                expected_checksum=evidence_checksum,
            )
            for evidence_checksum in evidence_refs
        )
        input_checksums = {item.input_checksum for item in evidence_items}
        result_checksums = {item.result_checksum for item in evidence_items}
        if len(input_checksums) != 1 or len(result_checksums) != 1:
            raise HarnessValidationError(
                "gate evidence refs do not identify one input and result",
                code="task_plan_gate_artifact_evidence_mismatch",
            )
        gate_refs = tuple(item.gate_ref for item in evidence_items)
        if len(gate_refs) != len(set(gate_refs)):
            raise HarnessValidationError(
                "gate evidence contains duplicate gate identities",
                code="task_plan_gate_artifact_evidence_mismatch",
            )
        input_checksum = next(iter(input_checksums))
        input_content = self._load_raw_document(
            "gate_input",
            run,
            stage,
            input_checksum,
        )
        instance, worker_result = _decode_gate_input_document(
            input_content,
            expected_scope=expected_scope,
            expected_checksum=input_checksum,
        )
        result_checksum = canonical_payload_checksum(
            worker_result.candidate_payload()
        )
        if result_checksum != next(iter(result_checksums)):
            raise HarnessValidationError(
                "gate evidence does not match its persisted input",
                code="task_plan_gate_artifact_evidence_mismatch",
            )
        refs = TaskPlanGateArtifactRefs(
            input_checksum=input_checksum,
            evidence_checksums=evidence_refs,
        )
        return TaskPlanGateVerificationArtifacts(
            refs=refs,
            instance=instance,
            worker_result=worker_result,
            evidences=evidence_items,
        )

    def append_candidate(
        self,
        candidate: PlanCandidate,
        *,
        event_type: str = "PLAN_CANDIDATE_BUILT",
    ) -> str:
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        _require_live_graph_only(candidate, "candidate")
        if event_type not in {"PLAN_CANDIDATE_BUILT", "PLAN_CANDIDATE_REJECTED"}:
            raise HarnessValidationError(
                "candidate event type is invalid",
                code="task_plan_unknown_event",
            )
        candidate_ref = self._put_document(
            "candidate",
            candidate.run_id,
            candidate.stage_id,
            candidate.candidate_checksum,
            candidate.to_dict(),
        )
        events = self.read_events(candidate.run_id, candidate.stage_id)
        existing = _event_for_input(events, event_type, candidate.candidate_checksum)
        if existing is not None:
            return candidate.candidate_checksum
        sequence = len(events) + 1
        event = _candidate_event(candidate, event_type, sequence)
        refs: dict[str, _DocumentReference] = {"candidate": candidate_ref}
        current = self._optional_projection(candidate.run_id, candidate.stage_id)
        if current is not None:
            _require_projection_matches_event(current, event)
            projection = replace(current, last_sequence=sequence)
            refs["projection"] = self._put_projection(projection)
        self._publish((event,), (refs,))
        return candidate.candidate_checksum

    def admit_candidate_submission(
        self,
        candidate: PlanCandidate,
        identity: CandidateDedupIdentity,
        *,
        accepted_at: str,
        candidate_checksum: str | None = None,
    ) -> CandidateSubmission:
        return self.submit_candidate(
            candidate, identity, accepted_at=accepted_at, candidate_checksum=candidate_checksum,
        ).submission

    def submit_candidate(
        self, candidate: PlanCandidate, identity: CandidateDedupIdentity, *,
        accepted_at: str, candidate_checksum: str | None = None,
        exclusive_stage: bool = False,
    ) -> CandidateSubmissionAdmission:
        from uuid import uuid4

        if not isinstance(exclusive_stage, bool):
            raise TypeError("exclusive_stage must be boolean")
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        if not isinstance(identity, CandidateDedupIdentity):
            raise TypeError("identity must be CandidateDedupIdentity")
        if exclusive_stage:
            require_submission_stage_available(self.read_events(identity.run_id, identity.stage_id), identity)
        _require_live_graph_only(candidate, "candidate")
        _require_submission_scope(candidate, identity)
        submitted = CandidateSubmission(
            identity=identity,
            candidate_checksum=(
                candidate.candidate_checksum
                if candidate_checksum is None
                else candidate_checksum
            ),
            candidate_ref=candidate.candidate_checksum,
            accepted_at=accepted_at,
            admission_id=uuid4().hex,
        )
        existing = self.candidate_submission(identity)
        if existing is not None:
            _require_same_submission(existing, submitted)
            return CandidateSubmissionAdmission(existing, created=False)

        candidate_ref = self._put_document(
            "candidate",
            candidate.run_id,
            candidate.stage_id,
            candidate.candidate_checksum,
            candidate.to_dict(),
        )
        events = self.read_events(candidate.run_id, candidate.stage_id)
        persisted = next(
            (
                item
                for item in submissions_from_events(events)
                if item.identity.dedup_key == identity.dedup_key
            ),
            None,
        )
        if persisted is not None:
            _require_same_submission(persisted, submitted)
            if self.candidate_for(
                candidate.run_id,
                candidate.stage_id,
                persisted.candidate_ref,
            ) is None:
                raise HarnessValidationError(
                    "candidate submission references unavailable candidate evidence",
                    code="task_plan_artifact_missing",
                    details={"candidate_ref": persisted.candidate_ref},
                )
            return CandidateSubmissionAdmission(persisted, created=False)
        sequence = len(events) + 1
        event = _candidate_event(
            candidate,
            "PLAN_CANDIDATE_BUILT",
            sequence,
            submission=submitted,
        )
        refs: dict[str, _DocumentReference] = {"candidate": candidate_ref}
        current = self._optional_projection(candidate.run_id, candidate.stage_id)
        if current is not None:
            _require_projection_matches_event(current, event)
            refs["projection"] = self._put_projection(
                replace(current, last_sequence=sequence)
            )
        try:
            self._publish((event,), (refs,), exclusive_submission=identity if exclusive_stage else None)
        except HarnessValidationError as exc:
            if exc.code not in {
                "task_plan_sequence_conflict",
                "task_plan_event_history_conflict",
                "task_plan_event_store_contention",
            }:
                raise
            existing = self.candidate_submission(identity)
            if existing is None:
                raise
            _require_same_submission(existing, submitted)
            return CandidateSubmissionAdmission(existing, created=False)
        return CandidateSubmissionAdmission(submitted, created=True)

    def candidate_submission(
        self,
        identity: CandidateDedupIdentity,
    ) -> CandidateSubmission | None:
        if not isinstance(identity, CandidateDedupIdentity):
            raise TypeError("identity must be CandidateDedupIdentity")
        submissions = self.submissions_for(identity.run_id, identity.stage_id)
        matches = [
            submission
            for submission in submissions
            if submission.identity.dedup_key == identity.dedup_key
        ]
        if len(matches) > 1:
            raise HarnessValidationError(
                "candidate submission history contains duplicate dedup identity",
                code="CANDIDATE_IDEMPOTENCY_CONFLICT",
                details={"dedup_key": identity.dedup_key},
            )
        return matches[0] if matches else None

    def submissions_for(
        self,
        run_id: str,
        stage_id: str,
    ) -> tuple[CandidateSubmission, ...]:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        submissions = submissions_from_events(self.read_events(run, stage))
        for submission in submissions:
            candidate = self.candidate_for(run, stage, submission.candidate_ref)
            if candidate is None:
                raise HarnessValidationError(
                    "candidate submission references unavailable candidate evidence",
                    code="task_plan_artifact_missing",
                    details={"candidate_ref": submission.candidate_ref},
                )
        return submissions

    def candidate_for(
        self,
        run_id: str,
        stage_id: str,
        candidate_ref: str,
    ) -> PlanCandidate | None:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        ref = checksum(candidate_ref, "candidate_ref")
        matches = [
            event
            for event in self.read_events(run, stage)
            if event.event_type == "PLAN_CANDIDATE_BUILT" and event.input_checksum == ref
        ]
        if not matches:
            return None
        if any(event.payload.get("candidate_ref") != ref for event in matches):
            raise HarnessValidationError(
                "candidate event does not reference its immutable candidate",
                code="candidate_submission_event_invalid",
            )
        candidate = self._load_document("candidate", run, stage, ref, PlanCandidate)
        if (
            candidate.candidate_checksum != ref
            or candidate.run_id != run
            or candidate.stage_id != stage
            or any(
                not event.matches_contract_identity(candidate)
                for event in matches
            )
        ):
            raise HarnessValidationError(
                "candidate artifact conflicts with durable event evidence",
                code="candidate_submission_event_invalid",
            )
        return candidate

    def append_rejected_candidate(
        self,
        candidate: PlanCandidate,
        *,
        reason_code: str,
    ) -> str:
        if not isinstance(candidate, PlanCandidate):
            raise TypeError("candidate must be PlanCandidate")
        _require_live_graph_only(candidate, "candidate")
        candidate_ref = self._put_document(
            "candidate",
            candidate.run_id,
            candidate.stage_id,
            candidate.candidate_checksum,
            candidate.to_dict(),
        )
        events = self.read_events(candidate.run_id, candidate.stage_id)
        rejected = _event_for_input(
            events,
            "PLAN_CANDIDATE_REJECTED",
            candidate.candidate_checksum,
        )
        failed = _event_for_input(
            events,
            "PLAN_VALIDATION_FAILED",
            candidate.candidate_checksum,
        )
        if rejected is not None or failed is not None:
            if rejected is None or failed is None or failed.reason_code != reason_code:
                raise HarnessValidationError(
                    "candidate rejection history is incomplete or conflicting",
                    code="task_plan_event_history_conflict",
                )
            return candidate.candidate_checksum

        first_sequence = len(events) + 1
        rejected_event = _candidate_event(
            candidate,
            "PLAN_CANDIDATE_REJECTED",
            first_sequence,
        )
        failed_event = _candidate_event(
            candidate,
            "PLAN_VALIDATION_FAILED",
            first_sequence + 1,
            reason_code=reason_code,
        )
        first_refs: dict[str, _DocumentReference] = {"candidate": candidate_ref}
        second_refs: dict[str, _DocumentReference] = {"candidate": candidate_ref}
        current = self._optional_projection(candidate.run_id, candidate.stage_id)
        if current is not None:
            _require_projection_matches_event(current, rejected_event)
            _require_projection_matches_event(current, failed_event)
            first_refs["projection"] = self._put_projection(
                replace(current, last_sequence=first_sequence)
            )
            second_refs["projection"] = self._put_projection(
                replace(current, last_sequence=first_sequence + 1)
            )
        self._publish(
            (rejected_event, failed_event),
            (first_refs, second_refs),
        )
        return candidate.candidate_checksum

    def accept_plan(self, plan: ValidatedTaskPlan) -> str:
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        _require_live_graph_only(plan, "plan")
        _require_initial_plan_submission_binding(
            plan,
            self.submissions_for(plan.run_id, plan.stage_id),
        )
        events = self.read_events(plan.run_id, plan.stage_id)
        accepted = [item for item in events if item.event_type == "PLAN_ACCEPTED"]
        same_version = [item for item in accepted if item.plan_version == plan.version]
        if same_version:
            event = same_version[0]
            if (
                len(same_version) != 1
                or event.plan_id != plan.plan_id
                or event.input_checksum != plan.plan_checksum
            ):
                raise HarnessValidationError(
                    "plan version checksum conflict",
                    code="task_plan_checksum_conflict",
                )
            stored = self.plan(plan.run_id, plan.stage_id, plan.version)
            if stored is None or stored.plan_checksum != plan.plan_checksum:
                raise HarnessValidationError(
                    "accepted plan evidence is unavailable",
                    code="task_plan_artifact_missing",
                )
            return plan.plan_checksum

        current = self.plan(plan.run_id, plan.stage_id)
        if current is None:
            if plan.version != 1:
                raise HarnessValidationError(
                    "initial TaskPlan version must be 1",
                    code="task_plan_version_conflict",
                )
        elif (
            plan.version != current.version + 1
            or plan.parent_plan_id != current.plan_id
        ):
            raise HarnessValidationError(
                "TaskPlan version is not monotonic",
                code="task_plan_version_conflict",
            )
        self._require_source_document(plan)

        sequence = len(events) + 1
        event = _plan_event(plan, "PLAN_ACCEPTED", sequence)
        validate_submission_event_append(events, (event,))

        plan_ref = self._put_document(
            "plan",
            plan.run_id,
            plan.stage_id,
            plan.plan_checksum,
            plan.to_dict(),
        )
        projection = _projection_for_plan(
            plan,
            sequence=sequence,
            previous=self._optional_projection(plan.run_id, plan.stage_id),
        )
        projection_ref = self._put_projection(projection)
        self._publish(
            (event,),
            ({"plan": plan_ref, "projection": projection_ref},),
        )
        return plan.plan_checksum

    def append_patch(self, patch: PlanPatch, *, accepted: bool = False) -> str:
        if not isinstance(patch, PlanPatch):
            raise TypeError("patch must be PlanPatch")
        _require_live_graph_only(patch, "patch")
        plan = self.plan(patch.run_id, patch.stage_id)
        if plan is None:
            raise HarnessValidationError(
                "cannot append a patch without an accepted base plan",
                code="task_plan_projection_missing",
            )
        if patch.base_plan_id != plan.plan_id or patch.base_plan_version != plan.version:
            raise HarnessValidationError(
                "patch base plan is stale",
                code="task_plan_version_conflict",
            )
        if not patch.matches_plan_identity(plan):
            raise HarnessValidationError(
                "patch identity does not match the accepted base plan",
                code="task_plan_patch_scope_mismatch",
            )
        patch_ref = self._put_document(
            "patch",
            patch.run_id,
            patch.stage_id,
            patch.patch_checksum,
            patch.to_dict(),
        )
        event_type = "PLAN_PATCH_ACCEPTED" if accepted else "PLAN_PATCH_PROPOSED"
        events = self.read_events(patch.run_id, patch.stage_id)
        existing = _event_for_input(events, event_type, patch.patch_checksum)
        if existing is not None:
            return patch.patch_checksum
        sequence = len(events) + 1
        projection = replace(self.load_projection(patch.run_id, patch.stage_id), last_sequence=sequence)
        projection_ref = self._put_projection(projection)
        event = TaskPlanEvent.for_plan(
            event_type,
            plan,
            input_checksum=patch.patch_checksum,
            reason_code=patch.reason_code,
            payload={"patch_ref": patch.patch_checksum},
            sequence=sequence,
        )
        self._publish(
            (event,),
            ({"patch": patch_ref, "projection": projection_ref},),
        )
        return patch.patch_checksum

    def accept_patched_plan(
        self,
        patch: PlanPatch,
        plan: ValidatedTaskPlan,
        *,
        skipped_task_ids: tuple[str, ...] = (),
    ) -> str:
        """Persist patch, plan version, skip transitions, and projections together."""

        if not isinstance(patch, PlanPatch) or not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("patch and plan must use TaskPlan contracts")
        _require_live_graph_only(patch, "patch")
        _require_live_graph_only(plan, "plan")
        current = self.plan(patch.run_id, patch.stage_id)
        if current is None or not patch.matches_plan_identity(current):
            raise HarnessValidationError("patch base plan is stale", code="task_plan_stale_patch")
        if (
            not plan.shares_stage_identity(current)
            or plan.policy_ref != current.policy_ref
            or plan.policy_checksum != current.policy_checksum
        ):
            raise HarnessValidationError(
                "patched plan identity does not match its accepted base",
                code="task_plan_patch_scope_mismatch",
            )
        if plan.parent_plan_id != current.plan_id or plan.version != current.version + 1:
            raise HarnessValidationError("patched plan version is not monotonic", code="task_plan_version_conflict")
        if plan.source_candidate_ref != patch.patch_checksum:
            raise HarnessValidationError("patched plan source does not match patch", code="task_plan_patch_checksum_mismatch")
        current_projection = self.load_projection(patch.run_id, patch.stage_id)
        skip_ids = tuple(
            sorted(set(identifier(item, "skipped_task_id") for item in skipped_task_ids))
        )
        replacements = _replacement_mapping(patch)
        _validate_patch_transition_targets(
            current,
            plan,
            current_projection=current_projection,
            replacements=replacements,
            skipped_task_ids=skip_ids,
        )
        existing = self.plan(plan.run_id, plan.stage_id, plan.version)
        if existing is not None:
            if existing.plan_checksum != plan.plan_checksum:
                raise HarnessValidationError("patched plan checksum conflicts", code="task_plan_checksum_conflict")
            return plan.plan_checksum
        patch_ref = self._put_document(
            "patch", patch.run_id, patch.stage_id, patch.patch_checksum, patch.to_dict()
        )
        # The patch artifact is the source evidence for the next immutable
        # plan version.  Materialize it before checking that evidence so a
        # first-time atomic patch can be validated and committed in one call.
        self._require_source_document(plan)
        plan_ref = self._put_document(
            "plan", plan.run_id, plan.stage_id, plan.plan_checksum, plan.to_dict()
        )
        events = self.read_events(plan.run_id, plan.stage_id)
        first_sequence = len(events) + 1
        patch_event = TaskPlanEvent.for_plan(
            "PLAN_PATCH_ACCEPTED",
            current,
            input_checksum=patch.patch_checksum,
            reason_code=patch.reason_code,
            payload={"patch_ref": patch.patch_checksum},
            sequence=first_sequence,
        )
        plan_event = _plan_event(plan, "PLAN_ACCEPTED", first_sequence + 1)
        projection = _projection_for_plan(
            plan,
            sequence=plan_event.sequence,
            previous=self.load_projection(plan.run_id, plan.stage_id),
        )
        references: list[Mapping[str, _DocumentReference]] = [
            {"patch": patch_ref, "projection": self._put_projection(replace(projection, last_sequence=first_sequence))},
            {"plan": plan_ref, "projection": self._put_projection(projection)},
        ]
        events_to_publish: list[TaskPlanEvent] = [patch_event, plan_event]
        for replaced_task_id, replacement_task_id in sorted(replacements.items()):
            old_state = next((item for item in projection.tasks if item.task_id == replaced_task_id), None)
            new_state = next((item for item in projection.tasks if item.task_id == replacement_task_id), None)
            if old_state is None or new_state is None:
                raise HarnessValidationError(
                    "patched plan replacement references unknown task",
                    code="task_plan_unknown_task",
                    details={"replaced_task_id": replaced_task_id, "replacement_task_id": replacement_task_id},
                )
            projection = replace(
                projection,
                tasks=tuple(
                    replace(
                        item,
                        status=TaskLifecycle.SKIPPED,
                        active_instance_id=None,
                        failure_reason_code="plan_patch_replaced",
                    )
                    if item.task_id == replaced_task_id else item
                    for item in projection.tasks
                ),
                last_sequence=projection.last_sequence + 1,
            )
            events_to_publish.append(
                TaskPlanEvent.for_plan(
                    "TASK_REPLACED",
                    plan,
                    task_id=replaced_task_id,
                    input_checksum=plan.plan_checksum,
                    reason_code="plan_patch_replaced",
                    payload={
                        "replaced_task_id": replaced_task_id,
                        "replacement_task_id": replacement_task_id,
                    },
                    sequence=projection.last_sequence,
                )
            )
            references.append({"projection": self._put_projection(projection)})
        for task_id in skip_ids:
            state = next((item for item in projection.tasks if item.task_id == task_id), None)
            if state is None:
                raise HarnessValidationError("skip target is unknown", code="task_plan_unknown_task")
            projection = replace(
                projection,
                tasks=tuple(
                    replace(item, status=TaskLifecycle.SKIPPED, active_instance_id=None, failure_reason_code="plan_patch_skip")
                    if item.task_id == task_id else item
                    for item in projection.tasks
                ),
                last_sequence=projection.last_sequence + 1,
            )
            events_to_publish.append(
                TaskPlanEvent.for_plan(
                    "TASK_SKIPPED",
                    plan,
                    task_id=task_id,
                    input_checksum=plan.plan_checksum,
                    reason_code="plan_patch_skip",
                    sequence=projection.last_sequence,
                )
            )
            references.append({"projection": self._put_projection(projection)})
        self._publish(tuple(events_to_publish), tuple(references))
        return plan.plan_checksum

    def append_result(self, result: TaskResultRecord) -> str:
        if not isinstance(result, TaskResultRecord):
            raise TypeError("result must be TaskResultRecord")
        _require_live_graph_only(result, "result")
        from framework.harness.runtime_contract import (
            validate_task_result_contract,
            validate_task_result_owner_contract,
        )

        validate_task_result_owner_contract(result)
        events = self.read_events(result.run_id, result.stage_id)
        plan = self.plan(result.run_id, result.stage_id, result.plan_version)
        if plan is None:
            raise HarnessValidationError(
                "task result plan is unavailable",
                code="task_plan_stale_result",
            )
        if not result.matches_plan_identity(plan):
            raise HarnessValidationError(
                "task result identity does not match accepted plan",
                code="task_plan_result_identity_mismatch",
            )
        existing_events = [
            event
            for event in events
            if event.event_type in {"TASK_RESULT_ACCEPTED", "TASK_RESULT_REJECTED"}
            and event.task_instance_id == result.task_instance_id
            and event.attempt == result.attempt
            and event.plan_version == result.plan_version
        ]
        if existing_events:
            if (
                len(existing_events) != 1
                or existing_events[0].payload.get("result_checksum")
                != result.result_checksum
            ):
                raise HarnessValidationError(
                    "conflicting duplicate task result",
                    code="task_plan_duplicate_result_conflict",
                )
            stored = self._load_document(
                "result",
                result.run_id,
                result.stage_id,
                result.result_checksum,
                TaskResultRecord,
            )
            if stored != result:
                raise HarnessValidationError(
                    "conflicting duplicate task result",
                    code="task_plan_duplicate_result_conflict",
                )
            from framework.harness.task_plan.result_proof import verify_task_result_proof

            verify_task_result_proof(
                plan,
                result,
                gate_evidence_reader=self,
            )
            return result.result_checksum

        projection = self.load_projection(result.run_id, result.stage_id)
        if (
            projection.plan_id != result.plan_id
            or projection.plan_version != result.plan_version
        ):
            raise HarnessValidationError(
                "task result belongs to stale plan",
                code="task_plan_stale_result",
            )
        from framework.harness.task_plan.attempt_history_index import history_record_for_result

        if not projection.matches_plan_identity(plan):
            raise HarnessValidationError(
                "TaskPlan projection does not match accepted plan identity",
                code="task_plan_projection_identity_mismatch",
            )
        state = next(
            (item for item in projection.tasks if item.task_id == result.task_id),
            None,
        )
        definition = next(
            (item for item in plan.tasks if item.task_id == result.task_id),
            None,
        )
        if state is None or definition is None:
            raise HarnessValidationError(
                "task result references unknown task",
                code="task_plan_unknown_task",
            )
        if definition.task_definition_checksum != result.task_checksum:
            raise HarnessValidationError(
                "task result definition checksum does not match accepted plan",
                code="task_plan_result_identity_mismatch",
            )
        if (
            definition.worker_ref != result.worker_ref
            or definition.binding_checksum != result.binding_checksum
        ):
            raise HarnessValidationError(
                "task result binding does not match accepted plan",
                code="task_plan_wrong_binding",
            )
        if (
            state.active_instance_id != result.task_instance_id
            or state.attempts != result.attempt
        ):
            raise HarnessValidationError(
                "task result belongs to a different attempt",
                code="task_plan_wrong_attempt",
            )
        if state.status in {TaskLifecycle.SUCCEEDED, TaskLifecycle.SKIPPED}:
            raise HarnessValidationError(
                "task already has a committed terminal result",
                code="task_plan_duplicate_result_conflict",
            )
        validate_task_result_contract(plan, projection, result)
        history_record_for_result(plan, result, events)
        _require_subagent_result_evidence(result, definition)
        _validate_result_usage(result, definition)
        from framework.harness.task_plan.result_proof import verify_task_result_proof

        verify_task_result_proof(
            plan,
            result,
            gate_evidence_reader=self,
        )

        if result.status is TaskLifecycle.SUCCEEDED:
            if result.output_schema_ref != definition.task.output_contract.schema_ref:
                raise HarnessValidationError(
                    "task result output schema does not match accepted task",
                    code="task_plan_output_schema_mismatch",
                )
            if result.output_roles != (definition.output_role,):
                raise HarnessValidationError(
                    "task result output role does not match accepted task",
                    code="task_plan_output_role_mismatch",
                )
            result_reference = TaskResultReference(
                result_ref=result.result_ref or "task-result:" + result.result_checksum,
                result_checksum=result.result_checksum,
                output_role=result.output_roles[0],
                output_schema_ref=result.output_schema_ref,
            )
            updated = replace(
                state,
                status=TaskLifecycle.SUCCEEDED,
                attempts=result.attempt,
                active_instance_id=None,
                admission_owner=None,
                result=result_reference,
                failure_reason_code=None,
            )
            result_event_type = "TASK_RESULT_ACCEPTED"
            terminal_event_type = "TASK_COMPLETED"
        else:
            updated = replace(
                state,
                status=TaskLifecycle.FAILED,
                attempts=result.attempt,
                active_instance_id=None,
                admission_owner=None,
                failure_reason_code=result.error_code or "task_failed",
            )
            result_event_type = "TASK_RESULT_REJECTED"
            terminal_event_type = "TASK_FAILED"

        result_ref = self._put_document(
            "result",
            result.run_id,
            result.stage_id,
            result.result_checksum,
            result.to_dict(),
        )
        result_sequence = len(events) + 1
        terminal_sequence = result_sequence + 1
        result_event = _result_event(
            result,
            result_event_type,
            result_sequence,
            plan=plan,
        )
        terminal_event = _terminal_result_event(
            result,
            terminal_event_type,
            terminal_sequence,
            plan=plan,
        )
        intermediate_projection = replace(projection, last_sequence=result_sequence)
        settled_budget = _settle_result_budget(
            projection.consumed_budget,
            definition,
            result,
        )
        terminal_projection = replace(
            projection,
            tasks=tuple(
                updated if item.task_id == result.task_id else item
                for item in projection.tasks
            ),
            consumed_budget=settled_budget,
            last_sequence=terminal_sequence,
        )
        intermediate_ref = self._put_projection(intermediate_projection)
        terminal_ref = self._put_projection(terminal_projection)
        self._publish(
            (result_event, terminal_event),
            (
                {"result": result_ref, "projection": intermediate_ref},
                {"result": result_ref, "projection": terminal_ref},
            ),
        )
        return result.result_checksum

    def load_projection(self, run_id: str, stage_id: str) -> TaskPlanProjection:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        stored, _ = self._read_snapshot(run)
        for canonical_event in reversed(stored):
            event = self._stored_to_domain(canonical_event)
            if event.stage_id != stage:
                continue
            reference = self._reference_from_event(canonical_event, "projection")
            if reference is None:
                continue
            projection = self._read_reference(reference, TaskPlanProjection)
            if (
                projection.run_id != run
                or projection.stage_id != stage
                or projection.last_sequence != event.sequence
            ):
                raise HarnessValidationError(
                    "TaskPlan projection reference conflicts with its event",
                    code="task_plan_projection_mismatch",
                )
            _require_projection_matches_event(projection, event)
            plan = self.plan(run, stage, projection.plan_version)
            if plan is None or not projection.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "TaskPlan projection conflicts with its accepted plan",
                    code="task_plan_projection_mismatch",
                )
            return projection
        raise HarnessValidationError(
            "TaskPlan projection is missing",
            code="task_plan_projection_missing",
            details={"run_id": run, "stage_id": stage},
        )

    def read_events(self, run_id: str, stage_id: str) -> tuple[TaskPlanEvent, ...]:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        stored, _ = self._read_snapshot(run)
        events = tuple(
            event
            for item in stored
            if (event := self._stored_to_domain(item)).stage_id == stage
        )
        for expected, event in enumerate(events, start=1):
            if event.sequence != expected:
                raise HarnessValidationError(
                    "TaskPlan event sequence is not contiguous",
                    code="task_plan_sequence_conflict",
                    details={"expected": expected, "actual": event.sequence},
                )
        return events

    def update_projection(self, projection: TaskPlanProjection) -> None:
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        _require_live_graph_only(projection, "projection")
        current = self.load_projection(projection.run_id, projection.stage_id)
        if current.projection_checksum != projection.projection_checksum:
            raise HarnessValidationError(
                "durable projection changes require a causal TaskPlan event",
                code="task_plan_projection_event_required",
            )

    def results_for(
        self,
        run_id: str,
        stage_id: str,
        plan_id: str,
        plan_version: int,
    ) -> tuple[TaskResultRecord, ...]:
        history = self.result_history_for(run_id, stage_id, plan_id, plan_version)
        projection = self.load_projection(run_id, stage_id)
        accepted_checksums = {
            item.result.result_checksum
            for item in projection.tasks
            if item.status is TaskLifecycle.SUCCEEDED and item.result is not None
        }
        matching = (
            record.result for record in history
            if record.result is not None
            and record.result.result_checksum in accepted_checksums
            and record.outcome.value == "ACCEPTED"
        )
        return tuple(sorted(matching, key=lambda item: (item.task_id, item.attempt, item.result_checksum)))

    def result_history_for(
        self, run_id: str, stage_id: str, plan_id: str, plan_version: int,
    ) -> tuple["TaskAttemptHistoryRecord", ...]:
        from framework.harness.task_plan.attempt_history_index import result_history_from_events

        requested = self.plan(run_id, stage_id, plan_version)
        if requested is None or requested.plan_id != plan_id:
            return ()
        return result_history_from_events(
            (self.plan(run_id, stage_id, version) for version in range(1, plan_version + 1)),
            self.read_events(run_id, stage_id),
            self._result_records_for(run_id, stage_id, plan_id, plan_version),
        )

    def _result_records_for(
        self,
        run_id: str,
        stage_id: str,
        plan_id: str,
        plan_version: int,
    ) -> tuple[TaskResultRecord, ...]:
        """Return accepted and rejected attempts required for replay."""

        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        requested_plan = self.plan(run, stage, plan_version)
        if requested_plan is None or requested_plan.plan_id != plan_id:
            return ()
        plans = {
            (run, stage, version): plan
            for version in range(1, plan_version + 1)
            if (plan := self.plan(run, stage, version)) is not None
        }
        records: dict[tuple[str, int, int, str], TaskResultRecord] = {}
        for event in self.read_events(run, stage):
            if event.event_type not in {"TASK_RESULT_ACCEPTED", "TASK_RESULT_REJECTED"}:
                continue
            if event.plan_version is not None and event.plan_version > plan_version:
                continue
            raw_checksum = event.payload.get("result_checksum")
            if not isinstance(raw_checksum, str):
                raise HarnessValidationError(
                    "TaskPlan result event has no result checksum",
                    code="task_plan_result_artifact_missing",
                )
            record = self._load_document(
                "result",
                run,
                stage,
                raw_checksum,
                TaskResultRecord,
            )
            record_plan = plans.get((run, stage, record.plan_version))
            if (
                record_plan is None
                or not record.matches_plan_identity(record_plan)
                or not event.matches_contract_identity(record_plan)
                or event.plan_id != record.plan_id
                or event.plan_version != record.plan_version
                or event.task_id != record.task_id
                or event.task_instance_id != record.task_instance_id
                or event.attempt != record.attempt
                or event.payload.get("result_checksum") != record.result_checksum
                or event.event_type
                != (
                    "TASK_RESULT_ACCEPTED"
                    if record.status is TaskLifecycle.SUCCEEDED
                    else "TASK_RESULT_REJECTED"
                )
            ):
                raise HarnessValidationError(
                    "TaskPlan result artifact conflicts with its event or accepted plan",
                    code="task_plan_result_identity_mismatch",
                )
            if not (
                record.plan_id == plan_id and record.plan_version == plan_version
            ) and not _plan_contains_task_version(plans, requested_plan, record):
                continue
            records[
                (
                    record.task_id,
                    record.attempt,
                    record.plan_version,
                    record.result_checksum,
                )
            ] = record
        return tuple(
            sorted(
                records.values(),
                key=lambda item: (
                    item.task_id,
                    item.attempt,
                    item.plan_version,
                    item.task_instance_id,
                    item.result_checksum,
                ),
            )
        )

    def append_event(self, event: TaskPlanEvent) -> str:
        if not isinstance(event, TaskPlanEvent):
            raise TypeError("event must be TaskPlanEvent")
        _require_live_graph_only(event, "event")
        events = self.read_events(event.run_id, event.stage_id)
        if event.sequence <= len(events):
            existing = events[event.sequence - 1]
            if existing.event_checksum == event.event_checksum:
                return event.event_checksum
            raise HarnessValidationError(
                "event sequence contains different TaskPlan content",
                code="task_plan_sequence_conflict",
            )
        if event.sequence != len(events) + 1:
            raise HarnessValidationError(
                "event sequence is not monotonic",
                code="task_plan_sequence_conflict",
                details={"expected": len(events) + 1, "actual": event.sequence},
            )
        plan = self.plan(event.run_id, event.stage_id)
        if plan is not None:
            _require_event_matches_plan(event, plan)
        _reject_uncoordinated_capacity_transition((event,), events)
        from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

        validate_submission_event_append(events, (event,))
        validate_parallel_admission_append(events, (event,), plan_lookup=lambda version: self.plan(event.run_id, event.stage_id, version))
        refs: dict[str, _DocumentReference] = {}
        current = self._optional_projection(event.run_id, event.stage_id)
        if current is not None:
            _require_projection_matches_event(current, event)
            refs["projection"] = self._put_projection(
                replace(current, last_sequence=event.sequence)
            )
        self._publish((event,), (refs,))
        return event.event_checksum

    def append_events(self, events: tuple[TaskPlanEvent, ...]) -> tuple[str, ...]:
        """Atomically append a contiguous batch with per-event projections.

        The projection snapshots are written before the canonical event batch,
        but become reachable only through their corresponding committed event.
        This preserves a readable checkpoint for every committed prefix.
        """

        batch = _validate_atomic_event_batch(events)
        run_id = batch[0].run_id
        stage_id = batch[0].stage_id
        history = self.read_events(run_id, stage_id)
        if _classify_atomic_event_batch_history(batch, history):
            return tuple(event.event_checksum for event in batch)
        _reject_uncoordinated_capacity_transition(batch, history)

        plan = self.plan(run_id, stage_id)
        if plan is not None:
            for event in batch:
                _require_event_matches_plan(event, plan)

        from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

        validate_submission_event_append(history, batch)
        validate_parallel_admission_append(history, batch, plan_lookup=lambda version: self.plan(run_id, stage_id, version))
        current = self._optional_projection(run_id, stage_id)
        refs: list[dict[str, _DocumentReference]] = []
        for event in batch:
            event_refs: dict[str, _DocumentReference] = {}
            if current is not None:
                _require_projection_matches_event(current, event)
                event_refs["projection"] = self._put_projection(
                    replace(current, last_sequence=event.sequence)
                )
            refs.append(event_refs)

        self._publish(batch, tuple(refs))
        return tuple(event.event_checksum for event in batch)

    def install_capacity_snapshot(
        self,
        snapshot: "CapacityScopeSnapshot",
    ) -> "CapacityScopeSnapshot":
        """Install one trusted capacity baseline through the durable CAS row."""

        from framework.harness.task_plan.capacity import CapacityScopeSnapshot

        if not isinstance(snapshot, CapacityScopeSnapshot):
            raise TypeError("snapshot must be CapacityScopeSnapshot")
        if snapshot.revision != 1:
            raise HarnessValidationError(
                "initial capacity scope revision must be 1",
                code="CAPACITY_RESERVATION_CONFLICT",
            )
        state = _capacity_state_snapshot(snapshot)
        self._runtime.compare_and_swap_transactional_state(
            state,
            expected_revision=None,
            expected_checksum=None,
        )
        return snapshot

    def load_capacity_snapshot(self, owner_scope: str) -> "CapacityScopeSnapshot":
        """Load one authoritative shared-capacity snapshot from the event store."""

        scope = identifier(owner_scope, "capacity_scope")
        state = self._reader.load_transactional_state(
            TASK_PLAN_CAPACITY_STATE_NAMESPACE,
            scope,
        )
        if state is None:
            raise HarnessValidationError(
                "required shared capacity scope is missing",
                code="CAPACITY_POLICY_MISSING",
                details={"owner_scope": scope},
            )
        return _capacity_snapshot_from_state(state)

    def register_parent_execution_context(
        self,
        context: HarnessGraphActivityTaskContext,
        *,
        execution_identity: GraphExecutionIdentity,
        stage_id: str,
        stage_binding_checksum: str,
    ) -> HarnessGraphActivityTaskContext:
        """Persist the immutable physical parent context used by child execution."""

        identity, normalized_stage, binding_checksum = (
            _require_parent_execution_context_binding(
                context,
                execution_identity=execution_identity,
                stage_id=stage_id,
                stage_binding_checksum=stage_binding_checksum,
            )
        )
        expected = _parent_execution_context_state_snapshot(
            context,
            execution_identity=identity,
            stage_id=normalized_stage,
            stage_binding_checksum=binding_checksum,
        )
        current = self._reader.load_transactional_state(
            TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
            expected.key,
        )
        if current is not None:
            return _require_registered_parent_execution_context(
                current,
                expected=expected,
                execution_identity=identity,
                stage_id=normalized_stage,
                stage_binding_checksum=binding_checksum,
            )
        try:
            persisted = self._runtime.compare_and_swap_transactional_state(
                expected,
                expected_revision=None,
                expected_checksum=None,
            )
        except EventStoreContentionError as exc:
            persisted = self._reader.load_transactional_state(
                TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
                expected.key,
            )
            if persisted is None:
                raise HarnessValidationError(
                    "parent execution context registration lost its durable CAS",
                    code="task_plan_parent_execution_context_conflict",
                ) from exc
        return _require_registered_parent_execution_context(
            persisted,
            expected=expected,
            execution_identity=identity,
            stage_id=normalized_stage,
            stage_binding_checksum=binding_checksum,
        )

    def load_parent_execution_context(
        self,
        execution_identity: GraphExecutionIdentity,
        *,
        stage_id: str,
        stage_binding_checksum: str,
    ) -> HarnessGraphActivityTaskContext:
        """Read the exact checkpoint-bound context for one parent Graph activity."""

        if not isinstance(execution_identity, GraphExecutionIdentity):
            raise TypeError("execution_identity must be GraphExecutionIdentity")
        normalized_stage = identifier(stage_id, "stage_id")
        binding_checksum = checksum(
            stage_binding_checksum,
            "stage_binding_checksum",
        )
        key = _parent_execution_context_state_key(
            execution_identity,
            stage_id=normalized_stage,
            stage_binding_checksum=binding_checksum,
        )
        state = self._reader.load_transactional_state(
            TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
            key,
        )
        if state is None:
            raise HarnessValidationError(
                "parent execution context is missing",
                code="task_plan_parent_execution_context_missing",
                details={
                    "run_id": execution_identity.run_id,
                    "stage_id": normalized_stage,
                    "activity_id": execution_identity.activity_id,
                },
            )
        return _parent_execution_context_from_state(
            state,
            execution_identity=execution_identity,
            stage_id=normalized_stage,
            stage_binding_checksum=binding_checksum,
        )

    def commit_wave_admission(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        expected_capacity_revision: int,
        capacity_scope: str,
        capacity_before_checksum: str,
        capacity_after: "CapacityScopeSnapshot",
        pool_reservations: tuple["PoolReservation", ...],
    ) -> tuple[str, ...]:
        """Publish wave facts, projection snapshots and shared capacity atomically."""

        batch = _validate_atomic_event_batch(events)
        _validate_transition_projections(batch, projections)
        before, _reservations = _validate_capacity_admission_contract(
            batch,
            expected_capacity_revision=expected_capacity_revision,
            capacity_scope=capacity_scope,
            capacity_before_checksum=capacity_before_checksum,
            capacity_after=capacity_after,
            pool_reservations=pool_reservations,
        )
        expected_checksum = checksum(
            expected_projection_checksum,
            "expected_projection_checksum",
        )
        first = batch[0]
        history = self.read_events(first.run_id, first.stage_id)
        replayed = _classify_atomic_event_batch_history(batch, history)
        plan = self.plan(
            first.run_id,
            first.stage_id,
            first.plan_version if replayed else None,
        )
        if plan is None:
            raise HarnessValidationError(
                "TaskPlan transition requires an accepted plan",
                code="task_plan_projection_missing",
            )
        current: TaskPlanProjection | None = None
        if not replayed:
            current = self.load_projection(first.run_id, first.stage_id)
            if current.projection_checksum != expected_checksum:
                raise HarnessValidationError(
                    "projection CAS precondition differs from current state",
                    code="task_plan_projection_mismatch",
                )
            if self.load_capacity_snapshot(before.owner_scope) != before:
                raise HarnessValidationError(
                    "capacity CAS precondition differs from current shared scope",
                    code="CAPACITY_RESERVATION_CONFLICT",
                    details={"owner_scope": before.owner_scope},
                )
            _validate_wave_admission_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
        for event, projection in zip(batch, projections, strict=True):
            _require_event_matches_plan(event, plan)
            if not projection.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "projection does not match the accepted plan",
                    code="task_plan_projection_mismatch",
                )
            if current is not None:
                _require_projection_transition_identity(current, projection)
        if replayed:
            self._historical_projection_refs(batch, projections)
            # Capacity evidence is embedded in the already checksummed wave.
            # Do not compare it with a later shared-scope revision or reserve again.
            return tuple(event.event_checksum for event in batch)

        from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

        validate_submission_event_append(history, batch)
        validate_parallel_admission_append(
            history,
            batch,
            plan_lookup=lambda version: self.plan(first.run_id, first.stage_id, version),
        )
        refs = tuple(
            {"projection": self._put_projection(projection)}
            for projection in projections
        )
        self._publish_with_state_cas(
            batch,
            refs,
            state_before=_capacity_state_snapshot(before),
            state_after=_capacity_state_snapshot(capacity_after),
        )
        return tuple(event.event_checksum for event in batch)

    def commit_wave_completion(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
        expected_capacity_revision: int,
        capacity_scope: str,
        capacity_before_checksum: str,
        capacity_after: "CapacityScopeSnapshot",
        settled_pool_reservations: tuple["PoolReservation", ...],
    ) -> tuple[str, ...]:
        """Persist confirmed capacity settlement with its wave completion."""

        from framework.harness.task_plan.store import _validate_capacity_completion_contract

        batch = _validate_atomic_event_batch(events)
        _validate_transition_projections(batch, projections)
        first = batch[0]
        history = self.read_events(first.run_id, first.stage_id)
        before, _settlements = _validate_capacity_completion_contract(
            batch,
            history,
            expected_capacity_revision=expected_capacity_revision,
            capacity_scope=capacity_scope,
            capacity_before_checksum=capacity_before_checksum,
            capacity_after=capacity_after,
            settled_pool_reservations=settled_pool_reservations,
        )
        replayed = _classify_atomic_event_batch_history(batch, history)
        plan = self.plan(
            first.run_id,
            first.stage_id,
            first.plan_version if replayed else None,
        )
        if plan is None:
            raise HarnessValidationError(
                "TaskPlan transition requires an accepted plan",
                code="task_plan_projection_missing",
            )
        current: TaskPlanProjection | None = None
        if not replayed:
            current = self.load_projection(first.run_id, first.stage_id)
            if current.projection_checksum != checksum(
                expected_projection_checksum,
                "expected_projection_checksum",
            ):
                raise HarnessValidationError(
                    "projection CAS precondition differs from current state",
                    code="task_plan_projection_mismatch",
                )
            if self.load_capacity_snapshot(before.owner_scope) != before:
                raise HarnessValidationError(
                    "capacity settlement CAS differs from current shared scope",
                    code="CAPACITY_RESERVATION_CONFLICT",
                )
        for event, projection in zip(batch, projections, strict=True):
            _require_event_matches_plan(event, plan)
            if not projection.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "projection does not match the accepted plan",
                    code="task_plan_projection_mismatch",
                )
            if current is not None:
                _require_projection_transition_identity(current, projection)
        if current is not None:
            _validate_wave_completion_projection_contract(
                current,
                batch,
                projections,
            )
        if replayed:
            self._historical_projection_refs(batch, projections)
            return tuple(event.event_checksum for event in batch)

        from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

        validate_submission_event_append(history, batch)
        validate_parallel_admission_append(
            history,
            batch,
            plan_lookup=lambda version: self.plan(first.run_id, first.stage_id, version),
        )
        refs = tuple(
            {"projection": self._put_projection(projection)}
            for projection in projections
        )
        self._publish_with_state_cas(
            batch,
            refs,
            state_before=_capacity_state_snapshot(before),
            state_after=_capacity_state_snapshot(capacity_after),
        )
        return tuple(event.event_checksum for event in batch)

    def commit_events(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
        *,
        expected_projection_checksum: str,
    ) -> tuple[str, ...]:
        """Atomically publish a transition batch and its prefix projections."""

        batch = _validate_atomic_event_batch(events)
        _validate_transition_projections(batch, projections)
        expected_checksum = checksum(
            expected_projection_checksum,
            "expected_projection_checksum",
        )
        first = batch[0]
        history = self.read_events(first.run_id, first.stage_id)
        replayed = _classify_atomic_event_batch_history(batch, history)
        if not replayed:
            _reject_uncoordinated_capacity_transition(batch, history)
        if not replayed:
            current = self.load_projection(first.run_id, first.stage_id)
            if current.projection_checksum != expected_checksum:
                raise HarnessValidationError(
                    "projection CAS precondition differs from current state",
                    code="task_plan_projection_mismatch",
                )
        plan = self.plan(
            first.run_id,
            first.stage_id,
            first.plan_version if replayed else None,
        )
        if plan is None:
            raise HarnessValidationError(
                "TaskPlan transition requires an accepted plan",
                code="task_plan_projection_missing",
            )
        if not replayed:
            _validate_wave_admission_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
            _validate_logical_readiness_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
            _validate_queue_admission_projection_contract(
                current,
                plan,
                batch,
                projections,
            )
            _validate_wave_completion_projection_contract(
                current,
                batch,
                projections,
            )
        for event, projection in zip(batch, projections, strict=True):
            _require_event_matches_plan(event, plan)
            if not projection.matches_plan_identity(plan):
                raise HarnessValidationError(
                    "projection does not match the accepted plan",
                    code="task_plan_projection_mismatch",
                )
            if not replayed:
                _require_projection_transition_identity(current, projection)

        if replayed:
            stored, _watermark = self._read_snapshot(first.run_id)
            committed = {
                domain.sequence: canonical
                for canonical in stored
                if (domain := self._stored_to_domain(canonical)).stage_id == first.stage_id
            }
            historical_refs = []
            for event, projection in zip(batch, projections, strict=True):
                reference = self._reference_from_event(committed[event.sequence], "projection")
                if reference is None:
                    raise HarnessValidationError("committed projection artifact is missing", code="task_plan_artifact_missing")
                persisted = self._read_reference(reference, TaskPlanProjection)
                if persisted.projection_checksum != projection.projection_checksum:
                    raise HarnessValidationError("committed projection differs from retry", code="task_plan_projection_mismatch")
                historical_refs.append({"projection": reference})
            refs = tuple(historical_refs)
        else:
            from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

            validate_submission_event_append(history, batch)
            validate_parallel_admission_append(history, batch, plan_lookup=lambda version: self.plan(first.run_id, first.stage_id, version))
            # Immutable artifacts become authoritative only with their event batch.
            refs = tuple({"projection": self._put_projection(projection)} for projection in projections)
        self._publish(batch, refs)
        return tuple(event.event_checksum for event in batch)

    def commit_event(
        self,
        event: TaskPlanEvent,
        projection: TaskPlanProjection,
    ) -> str:
        """Commit one event through the atomic transition boundary."""

        if not isinstance(event, TaskPlanEvent):
            raise TypeError("event must be TaskPlanEvent")
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        history = self.read_events(event.run_id, event.stage_id)
        expected = (
            projection.projection_checksum
            if event.sequence <= len(history)
            else self.load_projection(event.run_id, event.stage_id).projection_checksum
        )
        return self.commit_events(
            (event,),
            (projection,),
            expected_projection_checksum=expected,
        )[0]

    def plan(
        self,
        run_id: str,
        stage_id: str,
        version: int | None = None,
    ) -> ValidatedTaskPlan | None:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        accepted = [
            event
            for event in self.read_events(run, stage)
            if event.event_type == "PLAN_ACCEPTED"
        ]
        if not accepted:
            return None
        if version is None:
            event = max(accepted, key=lambda item: item.plan_version or 0)
        else:
            matches = [item for item in accepted if item.plan_version == version]
            if not matches:
                return None
            if len(matches) != 1:
                raise HarnessValidationError(
                    "TaskPlan history contains duplicate plan versions",
                    code="task_plan_version_conflict",
                )
            event = matches[0]
        raw_ref = event.payload.get("plan_ref")
        if not isinstance(raw_ref, str) or raw_ref != event.input_checksum:
            raise HarnessValidationError(
                "PLAN_ACCEPTED is missing matching plan evidence",
                code="task_plan_artifact_missing",
            )
        plan = self._load_document("plan", run, stage, raw_ref, ValidatedTaskPlan)
        if (
            plan.plan_id != event.plan_id
            or plan.version != event.plan_version
            or not event.matches_contract_identity(plan)
        ):
            raise HarnessValidationError(
                "accepted plan artifact conflicts with its event",
                code="task_plan_artifact_identity_mismatch",
            )
        return plan

    def patches_for(
        self,
        run_id: str,
        stage_id: str,
    ) -> tuple[PlanPatch, ...]:
        """Load all recorded patch evidence for deterministic offline replay."""

        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        patches: dict[str, PlanPatch] = {}
        for event in self.read_events(run, stage):
            if event.event_type not in {
                "PLAN_PATCH_PROPOSED",
                "PLAN_PATCH_REJECTED",
                "PLAN_PATCH_ACCEPTED",
            }:
                continue
            patch_ref = event.payload.get("patch_ref")
            if not isinstance(patch_ref, str):
                raise HarnessValidationError(
                    "TaskPlan patch event has no patch reference",
                    code="task_plan_artifact_missing",
                )
            patch = self._load_document("patch", run, stage, patch_ref, PlanPatch)
            if (
                patch.patch_checksum != patch_ref
                or patch.base_plan_id != event.plan_id
                or patch.base_plan_version != event.plan_version
                or any(
                    getattr(event, field_name) != getattr(patch, field_name)
                    for field_name in (
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
                )
            ):
                raise HarnessValidationError(
                    "TaskPlan patch artifact conflicts with its event",
                    code="task_plan_artifact_identity_mismatch",
                )
            existing = patches.get(patch.patch_checksum)
            if existing is not None and existing != patch:
                raise HarnessValidationError(
                    "TaskPlan history contains conflicting patch evidence",
                    code="task_plan_checksum_conflict",
                )
            patches[patch.patch_checksum] = patch
        return tuple(
            sorted(
                patches.values(),
                key=lambda item: (item.base_plan_version, item.patch_checksum),
            )
        )

    def _optional_projection(
        self,
        run_id: str,
        stage_id: str,
    ) -> TaskPlanProjection | None:
        try:
            return self.load_projection(run_id, stage_id)
        except HarnessValidationError as exc:
            if exc.code == "task_plan_projection_missing":
                return None
            raise

    def _require_source_document(self, plan: ValidatedTaskPlan) -> None:
        kinds = ("candidate",) if plan.version == 1 else ("patch",)
        for kind in kinds:
            try:
                self._load_raw_document(
                    kind,
                    plan.run_id,
                    plan.stage_id,
                    plan.source_candidate_ref,
                )
                return
            except HarnessValidationError as exc:
                if exc.code != "task_plan_artifact_missing":
                    raise
        raise HarnessValidationError(
            "accepted plan source artifact is missing",
            code="task_plan_candidate_missing",
            details={"source_candidate_ref": plan.source_candidate_ref},
        )

    def _put_projection(
        self,
        projection: TaskPlanProjection,
    ) -> _DocumentReference:
        return self._put_document(
            "projection",
            projection.run_id,
            projection.stage_id,
            projection.projection_checksum,
            projection.to_dict(),
        )

    def _put_document(
        self,
        kind: str,
        run_id: str,
        stage_id: str,
        domain_ref: str,
        payload: Mapping[str, Any],
    ) -> _DocumentReference:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        ref = checksum(domain_ref, "domain_ref")
        content = canonical_json(payload).encode("utf-8")
        reference = _document_reference(kind, run, stage, ref, content)
        artifact_ref = reference.artifact_ref()
        try:
            if self._artifact_store.exists(artifact_ref):
                existing = self._artifact_store.read(
                    replace(artifact_ref, checksum=None, size_bytes=None)
                )
                if existing != content:
                    raise HarnessValidationError(
                        "TaskPlan artifact identity contains different content",
                        code="task_plan_artifact_checksum_mismatch",
                        details={"domain_ref": ref},
                    )
                return reference
            written = self._artifact_store.write(
                ArtifactWriteRequest(
                    run_id=run,
                    artifact_id=reference.artifact_id,
                    artifact_type=f"harness.task-plan.{kind}",
                    relative_path=reference.path,
                    content=content,
                    content_type="application/json",
                    redacted=True,
                    metadata={
                        "task_plan_schema": TASK_PLAN_STORAGE_SCHEMA,
                        "task_plan_kind": kind,
                        "task_plan_stage_id": stage,
                        "task_plan_domain_ref": ref,
                    },
                )
            )
        except HarnessValidationError:
            raise
        except FileNotFoundError as exc:
            raise HarnessValidationError(
                "TaskPlan artifact disappeared during an immutable write",
                code="task_plan_artifact_store_failed",
            ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskPlan artifact store failed",
                code="task_plan_artifact_store_failed",
            ) from exc
        if (
            written.artifact_id != reference.artifact_id
            or written.run_id != run
            or written.path != reference.path
            or written.content_type != "application/json"
            or written.size_bytes != len(content)
            or written.checksum != reference.content_checksum.removeprefix("sha256:")
        ):
            raise HarnessValidationError(
                "TaskPlan artifact store returned a conflicting reference",
                code="task_plan_artifact_ref_invalid",
            )
        return reference

    def _load_document(
        self,
        kind: str,
        run_id: str,
        stage_id: str,
        domain_ref: str,
        model: type[_DocumentT],
    ) -> _DocumentT:
        content = self._load_raw_document(kind, run_id, stage_id, domain_ref)
        try:
            parsed = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HarnessValidationError(
                "TaskPlan artifact is not canonical JSON",
                code="task_plan_artifact_corrupt",
            ) from exc
        if not isinstance(parsed, Mapping):
            raise HarnessValidationError(
                "TaskPlan artifact payload must be an object",
                code="task_plan_artifact_corrupt",
            )
        try:
            value = model.from_dict(parsed)
        except HarnessValidationError:
            raise
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskPlan artifact does not match its schema",
                code="task_plan_artifact_corrupt",
            ) from exc
        actual_ref = _domain_ref(value)
        if actual_ref != domain_ref:
            raise HarnessValidationError(
                "TaskPlan artifact checksum does not match its event reference",
                code="task_plan_artifact_checksum_mismatch",
                details={"expected": domain_ref, "actual": actual_ref},
            )
        return value

    def _load_raw_document(
        self,
        kind: str,
        run_id: str,
        stage_id: str,
        domain_ref: str,
    ) -> bytes:
        run = identifier(run_id, "run_id")
        stage = identifier(stage_id, "stage_id")
        ref = checksum(domain_ref, "domain_ref")
        reference = _document_reference(kind, run, stage, ref, b"")
        # Size and byte checksum are not derivable from the domain checksum.
        # Resolve the committed descriptor when available; otherwise the
        # deterministic path still lets source validation distinguish missing
        # evidence from an unavailable event.
        committed = self._committed_reference(run, stage, kind, ref)
        if committed is not None:
            reference = committed
        else:
            reference = replace(
                reference,
                content_checksum="sha256:" + "0" * 64,
                size_bytes=0,
            )
        artifact_ref = reference.artifact_ref()
        if committed is None:
            artifact_ref = replace(artifact_ref, checksum=None, size_bytes=None)
        try:
            if not self._artifact_store.exists(artifact_ref):
                raise HarnessValidationError(
                    "TaskPlan artifact is missing",
                    code="task_plan_artifact_missing",
                    details={"kind": kind, "domain_ref": ref},
                )
            return self._artifact_store.read(artifact_ref)
        except HarnessValidationError:
            raise
        except FileNotFoundError as exc:
            raise HarnessValidationError(
                "TaskPlan artifact is missing",
                code="task_plan_artifact_missing",
                details={"kind": kind, "domain_ref": ref},
            ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskPlan artifact failed integrity verification",
                code="task_plan_artifact_checksum_mismatch",
                details={"kind": kind, "domain_ref": ref},
            ) from exc

    def _read_reference(
        self,
        reference: _DocumentReference,
        model: type[_DocumentT],
    ) -> _DocumentT:
        try:
            content = self._artifact_store.read(reference.artifact_ref())
        except FileNotFoundError as exc:
            raise HarnessValidationError(
                "TaskPlan artifact referenced by an event is missing",
                code="task_plan_artifact_missing",
                details={"kind": reference.kind, "domain_ref": reference.domain_ref},
            ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskPlan artifact referenced by an event failed integrity verification",
                code="task_plan_artifact_checksum_mismatch",
                details={"kind": reference.kind, "domain_ref": reference.domain_ref},
            ) from exc
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise HarnessValidationError(
                "TaskPlan artifact referenced by an event is corrupt",
                code="task_plan_artifact_corrupt",
            ) from exc
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "TaskPlan artifact referenced by an event is corrupt",
                code="task_plan_artifact_corrupt",
            )
        document = model.from_dict(value)
        if _domain_ref(document) != reference.domain_ref:
            raise HarnessValidationError(
                "TaskPlan artifact domain checksum does not match its event reference",
                code="task_plan_artifact_checksum_mismatch",
            )
        return document

    def _committed_reference(
        self,
        run_id: str,
        stage_id: str,
        kind: str,
        domain_ref: str,
    ) -> _DocumentReference | None:
        stored, _ = self._read_snapshot(run_id)
        for event in reversed(stored):
            domain = self._stored_to_domain(event)
            if domain.stage_id != stage_id:
                continue
            reference = self._reference_from_event(event, kind)
            if reference is not None and reference.domain_ref == domain_ref:
                return reference
        return None

    def _reference_from_event(
        self,
        event: StoredEvent,
        kind: str,
    ) -> _DocumentReference | None:
        extension = thaw_canonical_json(
            event.extensions.get(TASK_PLAN_STORAGE_EXTENSION, {})
        )
        if not extension:
            return None
        if not isinstance(extension, Mapping) or set(extension) != {"schema", "refs"}:
            raise HarnessValidationError(
                "TaskPlan storage extension is invalid",
                code="task_plan_artifact_ref_invalid",
            )
        if extension.get("schema") != TASK_PLAN_STORAGE_SCHEMA:
            raise HarnessValidationError(
                "TaskPlan storage extension schema is unsupported",
                code="task_plan_artifact_schema_unsupported",
            )
        refs = extension.get("refs")
        if not isinstance(refs, Mapping):
            raise HarnessValidationError(
                "TaskPlan storage refs must be an object",
                code="task_plan_artifact_ref_invalid",
            )
        value = refs.get(kind)
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "TaskPlan storage ref must be an object",
                code="task_plan_artifact_ref_invalid",
            )
        reference = _DocumentReference.from_dict(value)
        if reference.kind != kind:
            raise HarnessValidationError(
                "TaskPlan storage ref kind is inconsistent",
                code="task_plan_artifact_ref_invalid",
            )
        return reference

    def _publish(
        self,
        events: Sequence[TaskPlanEvent],
        refs: Sequence[Mapping[str, _DocumentReference]],
        *, exclusive_submission: CandidateDedupIdentity | None = None,
    ) -> tuple[StoredEvent, ...]:
        if not events or len(events) != len(refs):
            raise ValueError("events and refs must have the same non-zero length")
        run_id = events[0].run_id
        stage_id = events[0].stage_id
        if any(
            event.run_id != run_id or event.stage_id != stage_id
            for event in events
        ):
            raise HarnessValidationError(
                "TaskPlan atomic append cannot cross a run or stage",
                code="task_plan_event_scope_mismatch",
            )

        for _ in range(_MAX_EVENT_APPEND_RETRIES):
            stored, high_watermark = self._read_snapshot(run_id)
            restored = tuple(
                (item, self._stored_to_domain(item)) for item in stored
            )
            stage_history = [
                (item, event)
                for item, event in restored
                if event.stage_id == stage_id
            ]
            if exclusive_submission is not None:
                require_submission_stage_available(tuple(event for _, event in stage_history), exclusive_submission)
            missing: list[tuple[TaskPlanEvent, Mapping[str, _DocumentReference]]] = []
            for event, event_refs in zip(events, refs, strict=True):
                if event.sequence <= len(stage_history):
                    canonical, existing = stage_history[event.sequence - 1]
                    if existing.event_checksum != event.event_checksum:
                        raise HarnessValidationError(
                            "TaskPlan sequence already contains different content",
                            code="task_plan_sequence_conflict",
                        )
                    self._validate_stored_refs(canonical, event_refs)
                    continue
                expected = len(stage_history) + len(missing) + 1
                if event.sequence != expected:
                    raise HarnessValidationError(
                        "TaskPlan event sequence is not monotonic",
                        code="task_plan_sequence_conflict",
                        details={"expected": expected, "actual": event.sequence},
                    )
                missing.append((event, event_refs))
            if not missing:
                return tuple(
                    stage_history[event.sequence - 1][0] for event in events
                )
            if len(missing) != len(events):
                raise HarnessValidationError(
                    "TaskPlan atomic event batch is only partially present",
                    code="task_plan_event_history_conflict",
                )
            from framework.harness.task_plan.parallel_admission import validate_parallel_admission_append

            validate_submission_event_append(tuple(event for _, event in stage_history), events)
            validate_parallel_admission_append((event for _, event in stage_history), events)
            requests = tuple(
                self._publish_request(event, event_refs)
                for event, event_refs in missing
            )
            try:
                committed = (
                    (self._runtime.publish(
                        requests[0],
                        expected_last_sequence=high_watermark,
                    ),)
                    if len(requests) == 1
                    else self._runtime.publish_batch(
                        requests,
                        expected_last_sequence=high_watermark,
                    )
                )
            except EventStreamVersionConflictError:
                continue
            except EventIdentityCollisionError:
                # A concurrent exact logical append can differ only in the
                # canonical observation timestamp.  Re-read and require every
                # domain event and immutable artifact reference to match.
                continue
            for stored_event, (event, event_refs) in zip(
                committed,
                missing,
                strict=True,
            ):
                restored = self._stored_to_domain(stored_event)
                if restored.event_checksum != event.event_checksum:
                    raise HarnessValidationError(
                        "canonical event store returned different TaskPlan content",
                        code="task_plan_event_commit_mismatch",
                    )
                self._validate_stored_refs(stored_event, event_refs)
            return tuple(committed)
        raise HarnessValidationError(
            "TaskPlan event append exceeded bounded contention retries",
            code="task_plan_event_store_contention",
        )

    def _historical_projection_refs(
        self,
        events: tuple[TaskPlanEvent, ...],
        projections: tuple[TaskPlanProjection, ...],
    ) -> None:
        stored, _watermark = self._read_snapshot(events[0].run_id)
        committed = {
            domain.sequence: canonical
            for canonical in stored
            if (domain := self._stored_to_domain(canonical)).stage_id
            == events[0].stage_id
        }
        for event, projection in zip(events, projections, strict=True):
            canonical = committed.get(event.sequence)
            if canonical is None:
                raise HarnessValidationError(
                    "committed wave event is missing from its canonical stream",
                    code="task_plan_event_history_conflict",
                )
            reference = self._reference_from_event(canonical, "projection")
            if reference is None:
                raise HarnessValidationError(
                    "committed projection artifact is missing",
                    code="task_plan_artifact_missing",
                )
            persisted = self._read_reference(reference, TaskPlanProjection)
            if persisted.projection_checksum != projection.projection_checksum:
                raise HarnessValidationError(
                    "committed projection differs from retry",
                    code="task_plan_projection_mismatch",
                )

    def _publish_with_state_cas(
        self,
        events: Sequence[TaskPlanEvent],
        refs: Sequence[Mapping[str, _DocumentReference]],
        *,
        state_before: TransactionalStateSnapshot,
        state_after: TransactionalStateSnapshot,
    ) -> tuple[StoredEvent, ...]:
        """Publish one whole TaskPlan batch beside one capacity-scope CAS."""

        if not events or len(events) != len(refs):
            raise ValueError("events and refs must have the same non-zero length")
        if (
            state_before.namespace != TASK_PLAN_CAPACITY_STATE_NAMESPACE
            or state_after.namespace != TASK_PLAN_CAPACITY_STATE_NAMESPACE
            or (state_before.namespace, state_before.key)
            != (state_after.namespace, state_after.key)
            or state_after.revision != state_before.revision + 1
        ):
            raise HarnessValidationError(
                "capacity state CAS snapshots are inconsistent",
                code="CAPACITY_RESERVATION_CONFLICT",
            )
        run_id = events[0].run_id
        stage_id = events[0].stage_id
        for _ in range(_MAX_EVENT_APPEND_RETRIES):
            stored, high_watermark = self._read_snapshot(run_id)
            stage_history = [
                (item, domain)
                for item in stored
                if (domain := self._stored_to_domain(item)).stage_id == stage_id
            ]
            missing: list[tuple[TaskPlanEvent, Mapping[str, _DocumentReference]]] = []
            for event, event_refs in zip(events, refs, strict=True):
                if event.sequence <= len(stage_history):
                    canonical, existing = stage_history[event.sequence - 1]
                    if existing.event_checksum != event.event_checksum:
                        raise HarnessValidationError(
                            "TaskPlan sequence already contains different content",
                            code="task_plan_sequence_conflict",
                        )
                    self._validate_stored_refs(canonical, event_refs)
                    continue
                expected = len(stage_history) + len(missing) + 1
                if event.sequence != expected:
                    raise HarnessValidationError(
                        "TaskPlan event sequence is not monotonic",
                        code="task_plan_sequence_conflict",
                        details={"expected": expected, "actual": event.sequence},
                    )
                missing.append((event, event_refs))
            if not missing:
                return tuple(
                    stage_history[event.sequence - 1][0] for event in events
                )
            if len(missing) != len(events):
                raise HarnessValidationError(
                    "TaskPlan atomic event batch is only partially present",
                    code="task_plan_event_history_conflict",
                )
            requests = tuple(
                self._publish_request(event, event_refs)
                for event, event_refs in missing
            )
            try:
                committed, persisted_state = self._runtime.publish_batch_with_state_cas(
                    requests,
                    expected_last_sequence=high_watermark,
                    state_namespace=state_before.namespace,
                    state_key=state_before.key,
                    expected_state_revision=state_before.revision,
                    expected_state_checksum=state_before.checksum,
                    next_state=state_after,
                )
            except (EventStreamVersionConflictError, EventIdentityCollisionError):
                continue
            if persisted_state != state_after:
                raise HarnessValidationError(
                    "capacity state commit returned different content",
                    code="CAPACITY_RESERVATION_CONFLICT",
                )
            for stored_event, (event, event_refs) in zip(
                committed,
                missing,
                strict=True,
            ):
                restored = self._stored_to_domain(stored_event)
                if restored.event_checksum != event.event_checksum:
                    raise HarnessValidationError(
                        "canonical event store returned different TaskPlan content",
                        code="task_plan_event_commit_mismatch",
                    )
                self._validate_stored_refs(stored_event, event_refs)
            return tuple(committed)
        raise HarnessValidationError(
            "TaskPlan capacity admission exceeded bounded stream contention retries",
            code="task_plan_event_store_contention",
        )

    def _publish_request(
        self,
        event: TaskPlanEvent,
        refs: Mapping[str, _DocumentReference],
    ) -> EventPublishRequest:
        occurred_at = self._clock()
        payload = event.to_dict()
        payload.pop("event_type")
        payload["details"] = payload.pop("payload")
        extensions: dict[str, Any] = {
            TASK_PLAN_STORAGE_EXTENSION: {
                "schema": TASK_PLAN_STORAGE_SCHEMA,
                "refs": {
                    key: value.to_dict()
                    for key, value in sorted(refs.items())
                },
            }
        }
        graph_context = _graph_event_context_for_task_plan_event(event)
        business_context = BusinessContext(
            run_id=event.run_id,
            graph_id=event.graph_id,
            graph_version=event.graph_version,
            graph_ref=event.graph_ref,
            graph_checksum=event.graph_checksum,
            stage_id=event.stage_id,
            node_instance_id=graph_context.node_instance_id,
            task_id=event.task_id,
        )
        extensions[GRAPH_EVENT_CONTEXT_EXTENSION] = graph_context.to_dict()
        return EventPublishRequest(
            event_id=_event_id(event, self._tenant_id),
            event_type=event.event_type,
            data_schema=event.schema_version,
            source=TASK_PLAN_EVENT_SOURCE,
            occurred_at=occurred_at,
            stream_id=f"run:{event.run_id}",
            subject=event.task_id or event.stage_id,
            correlation_id=event.run_id,
            causation_id=event.causal_event_ref,
            business_context=business_context,
            producer=self._producer,
            tenant_id=self._tenant_id,
            security_classification=self._security_classification,
            payload=payload,
            extensions=extensions,
        )

    def _read_snapshot(self, run_id: str) -> tuple[tuple[StoredEvent, ...], int]:
        stream_id = f"run:{identifier(run_id, 'run_id')}"
        high_watermark = self._reader.get_stream_high_watermark(
            stream_id,
            tenant_id=self._tenant_id,
        )
        if high_watermark is None:
            return (), 0
        cursor = None
        events: list[StoredEvent] = []
        while True:
            page = self._reader.read_stream(
                StreamReadRequest(
                    stream_id=stream_id,
                    cursor=cursor,
                    through_sequence=high_watermark,
                    limit=_EVENT_PAGE_SIZE,
                    tenant_id=self._tenant_id,
                    event_types=frozenset(TASK_PLAN_EVENT_TYPES),
                    data_schemas=frozenset(TASK_PLAN_EVENT_SCHEMAS),
                )
            )
            events.extend(page.events)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        return tuple(events), high_watermark

    def _stored_to_domain(self, stored: StoredEvent) -> TaskPlanEvent:
        stored.verify_integrity()
        if (
            stored.event_type not in TASK_PLAN_EVENT_TYPES
            or stored.data_schema not in TASK_PLAN_EVENT_SCHEMAS
            or stored.source != TASK_PLAN_EVENT_SOURCE
            or stored.stream_id
            != f"run:{stored.business_context.run_id or ''}"
            or stored.tenant_id != self._tenant_id
        ):
            raise HarnessValidationError(
                "canonical TaskPlan event envelope is inconsistent",
                code="task_plan_event_identity_mismatch",
            )
        payload = thaw_canonical_json(stored.payload or {})
        if not isinstance(payload, Mapping):
            raise HarnessValidationError(
                "canonical TaskPlan event payload is invalid",
                code="task_plan_event_payload_invalid",
            )
        restored_payload = dict(payload)
        restored_payload["event_type"] = stored.event_type
        restored_payload["payload"] = restored_payload.pop("details", None)
        event = TaskPlanEvent.from_dict(restored_payload)
        if stored.data_schema != event.schema_version:
            raise HarnessValidationError(
                "canonical TaskPlan event schema conflicts with its payload",
                code="task_plan_event_identity_mismatch",
            )
        if (
            stored.business_context.run_id != event.run_id
            or stored.business_context.task_id != event.task_id
            or stored.event_type != event.event_type
        ):
            raise HarnessValidationError(
                "canonical TaskPlan event context conflicts with its payload",
                code="task_plan_event_identity_mismatch",
            )
        try:
            context = graph_event_context(stored)
        except EventContractError as exc:
            raise HarnessValidationError(
                "canonical Graph TaskPlan event context is invalid",
                code="task_plan_event_identity_mismatch",
            ) from exc
        if context != _graph_event_context_for_task_plan_event(event):
            raise HarnessValidationError(
                "canonical Graph TaskPlan event context conflicts with its payload",
                code="task_plan_event_identity_mismatch",
            )
        return event

    def _validate_stored_refs(
        self,
        stored: StoredEvent,
        expected: Mapping[str, _DocumentReference],
    ) -> None:
        extension = thaw_canonical_json(
            stored.extensions.get(TASK_PLAN_STORAGE_EXTENSION, {})
        )
        actual = {
            "schema": TASK_PLAN_STORAGE_SCHEMA,
            "refs": {
                key: value.to_dict() for key, value in sorted(expected.items())
            },
        }
        if extension != actual:
            raise HarnessValidationError(
                "canonical TaskPlan event artifact refs differ from the commit request",
                code="task_plan_event_commit_mismatch",
            )


def _document_reference(
    kind: str,
    run_id: str,
    stage_id: str,
    domain_ref: str,
    content: bytes,
) -> _DocumentReference:
    digest = domain_ref.removeprefix("sha256:")
    stage_digest = sha256(stage_id.encode("utf-8")).hexdigest()[:24]
    content_digest = sha256(content).hexdigest()
    return _DocumentReference(
        kind=kind,
        domain_ref=domain_ref,
        artifact_id=f"task-plan-{kind}-{digest}",
        run_id=run_id,
        path=f"_task_plan/{stage_digest}/{kind}/{digest}.json",
        content_checksum=f"sha256:{content_digest}",
        size_bytes=len(content),
    )


def _gate_artifact_scope(
    tenant_id: str | None,
    run_id: str,
    stage_id: str,
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "run_id": identifier(run_id, "run_id"),
        "stage_id": identifier(stage_id, "stage_id"),
    }


def _decode_gate_input_document(
    content: bytes,
    *,
    expected_scope: Mapping[str, Any],
    expected_checksum: str,
) -> tuple[TaskInstance, HarnessWorkerResult]:
    value = _decode_gate_artifact_json(content)
    if set(value) != {"schema_version", "scope", "gate_input"}:
        raise HarnessValidationError(
            "gate input artifact fields are invalid",
            code="task_plan_gate_artifact_corrupt",
        )
    if value["schema_version"] != TASK_PLAN_GATE_INPUT_SCHEMA:
        raise HarnessValidationError(
            "gate input artifact schema is unsupported",
            code="task_plan_gate_artifact_schema_unsupported",
        )
    _require_gate_artifact_scope(value["scope"], expected_scope)
    gate_input = value["gate_input"]
    if not isinstance(gate_input, Mapping) or set(gate_input) != {
        "instance",
        "worker_result",
    }:
        raise HarnessValidationError(
            "gate input preimage must contain exactly instance and worker_result",
            code="task_plan_gate_artifact_corrupt",
        )
    actual_checksum = canonical_payload_checksum(gate_input)
    if actual_checksum != expected_checksum:
        raise HarnessValidationError(
            "gate input artifact checksum does not match its reference",
            code="task_plan_gate_artifact_checksum_mismatch",
            details={"expected": expected_checksum, "actual": actual_checksum},
        )
    raw_instance = gate_input["instance"]
    raw_worker_result = gate_input["worker_result"]
    if not isinstance(raw_instance, Mapping) or "instance_checksum" in raw_instance:
        raise HarnessValidationError(
            "gate input instance projection is invalid",
            code="task_plan_gate_artifact_corrupt",
        )
    if not isinstance(raw_worker_result, Mapping):
        raise HarnessValidationError(
            "gate input worker result is invalid",
            code="task_plan_gate_artifact_corrupt",
        )
    try:
        instance_projection = dict(raw_instance)
        instance = TaskInstance.from_dict(
            {
                **instance_projection,
                "instance_checksum": canonical_payload_checksum(
                    instance_projection
                ),
            }
        )
        worker_result = HarnessWorkerResult.from_dict(raw_worker_result)
    except (HarnessValidationError, TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "gate input artifact does not match its typed schema",
            code="task_plan_gate_artifact_corrupt",
        ) from exc
    if (
        instance.checksum_projection() != instance_projection
        or worker_result.candidate_payload() != dict(raw_worker_result)
    ):
        raise HarnessValidationError(
            "gate input artifact is not canonical",
            code="task_plan_gate_artifact_corrupt",
        )
    if (
        instance.run_id != expected_scope["run_id"]
        or instance.stage_id != expected_scope["stage_id"]
    ):
        raise HarnessValidationError(
            "gate input instance is outside the artifact scope",
            code="task_plan_gate_artifact_scope_mismatch",
        )
    return instance, worker_result


def _decode_gate_evidence_document(
    content: bytes,
    *,
    expected_scope: Mapping[str, Any],
    expected_checksum: str,
) -> TaskPlanGateEvidence:
    value = _decode_gate_artifact_json(content)
    if set(value) != {"schema_version", "scope", "gate_evidence"}:
        raise HarnessValidationError(
            "gate evidence artifact fields are invalid",
            code="task_plan_gate_artifact_corrupt",
        )
    if value["schema_version"] != TASK_PLAN_GATE_EVIDENCE_SCHEMA:
        raise HarnessValidationError(
            "gate evidence artifact schema is unsupported",
            code="task_plan_gate_artifact_schema_unsupported",
        )
    _require_gate_artifact_scope(value["scope"], expected_scope)
    raw_evidence = value["gate_evidence"]
    if not isinstance(raw_evidence, Mapping):
        raise HarnessValidationError(
            "gate evidence artifact payload is invalid",
            code="task_plan_gate_artifact_corrupt",
        )
    try:
        evidence = TaskPlanGateEvidence.from_dict(raw_evidence)
    except HarnessValidationError as exc:
        if exc.code == "task_plan_gate_evidence_checksum_mismatch":
            raise HarnessValidationError(
                "gate evidence artifact checksum does not match its content",
                code="task_plan_gate_artifact_checksum_mismatch",
            ) from exc
        raise HarnessValidationError(
            "gate evidence artifact does not match its typed schema",
            code="task_plan_gate_artifact_corrupt",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "gate evidence artifact does not match its typed schema",
            code="task_plan_gate_artifact_corrupt",
        ) from exc
    if evidence.evidence_checksum != expected_checksum:
        raise HarnessValidationError(
            "gate evidence artifact checksum does not match its reference",
            code="task_plan_gate_artifact_checksum_mismatch",
            details={
                "expected": expected_checksum,
                "actual": evidence.evidence_checksum,
            },
        )
    return evidence


def _decode_gate_artifact_json(content: bytes) -> Mapping[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessValidationError(
            "gate artifact is not JSON",
            code="task_plan_gate_artifact_corrupt",
        ) from exc
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            "gate artifact must be an object",
            code="task_plan_gate_artifact_corrupt",
        )
    return value


def _require_gate_artifact_scope(
    actual: Any,
    expected: Mapping[str, Any],
) -> None:
    if not isinstance(actual, Mapping) or dict(actual) != dict(expected):
        raise HarnessValidationError(
            "gate artifact scope does not match the reader scope",
            code="task_plan_gate_artifact_scope_mismatch",
        )


def _domain_ref(value: Any) -> str:
    # Projection documents also carry the plan identity fields, so resolve
    # their own checksum before the broader plan/result alternatives.
    for field_name in (
        "projection_checksum",
        "candidate_checksum",
        "patch_checksum",
        "plan_checksum",
        "result_checksum",
    ):
        reference = getattr(value, field_name, None)
        if isinstance(reference, str):
            return reference
    raise TypeError("TaskPlan document has no domain checksum")


def _event_for_input(
    events: Sequence[TaskPlanEvent],
    event_type: str,
    input_checksum: str,
) -> TaskPlanEvent | None:
    matches = [
        event
        for event in events
        if event.event_type == event_type and event.input_checksum == input_checksum
    ]
    if len(matches) > 1:
        raise HarnessValidationError(
            "TaskPlan history contains duplicate logical events",
            code="task_plan_event_history_conflict",
        )
    return matches[0] if matches else None


def _event_id(event: TaskPlanEvent, tenant_id: str | None) -> str:
    scope = tenant_id or "unscoped"
    digest = sha256(
        f"{scope}|{event.run_id}|{event.stage_id}|{event.event_checksum}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"task-plan-event:{digest}"


def _capacity_state_snapshot(
    snapshot: "CapacityScopeSnapshot",
) -> TransactionalStateSnapshot:
    from framework.harness.task_plan.capacity import CapacityScopeSnapshot

    if not isinstance(snapshot, CapacityScopeSnapshot):
        raise TypeError("snapshot must be CapacityScopeSnapshot")
    if snapshot.revision < 1:
        raise HarnessValidationError(
            "durable capacity scope revision must be positive",
            code="CAPACITY_RESERVATION_CONFLICT",
        )
    return TransactionalStateSnapshot.create(
        namespace=TASK_PLAN_CAPACITY_STATE_NAMESPACE,
        key=snapshot.owner_scope,
        revision=snapshot.revision,
        payload=snapshot.to_dict(),
    )


def _capacity_snapshot_from_state(
    state: TransactionalStateSnapshot,
) -> "CapacityScopeSnapshot":
    from framework.harness.task_plan.capacity import CapacityScopeSnapshot

    if not isinstance(state, TransactionalStateSnapshot):
        raise TypeError("state must be TransactionalStateSnapshot")
    if state.namespace != TASK_PLAN_CAPACITY_STATE_NAMESPACE:
        raise HarnessValidationError(
            "capacity state namespace is invalid",
            code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
        )
    payload = thaw_canonical_json(state.payload)
    if not isinstance(payload, Mapping):
        raise HarnessValidationError(
            "capacity state payload is invalid",
            code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
        )
    try:
        snapshot = CapacityScopeSnapshot.from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "capacity state payload is invalid",
            code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
        ) from exc
    if snapshot.owner_scope != state.key or snapshot.revision != state.revision:
        raise HarnessValidationError(
            "capacity state envelope conflicts with its snapshot",
            code="CAPACITY_POLICY_CHECKSUM_MISMATCH",
        )
    return snapshot


def _parent_execution_context_identity(
    context: HarnessGraphActivityTaskContext,
) -> GraphExecutionIdentity:
    if not isinstance(context, HarnessGraphActivityTaskContext):
        raise TypeError("context must be HarnessGraphActivityTaskContext")
    activity = context.activity
    graph = activity.graph_ref
    return GraphExecutionIdentity(
        run_id=activity.run_id,
        graph_id=graph.graph_id,
        graph_version=graph.identity_version,
        graph_ref=graph.identity_ref.exact_ref,
        graph_checksum=graph.checksum,
        node_id=activity.node_id,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id,
        attempt=activity.attempt,
    )


def _require_parent_execution_context_binding(
    context: HarnessGraphActivityTaskContext,
    *,
    execution_identity: GraphExecutionIdentity,
    stage_id: str,
    stage_binding_checksum: str,
) -> tuple[GraphExecutionIdentity, str, str]:
    if not isinstance(context, HarnessGraphActivityTaskContext):
        raise TypeError("context must be HarnessGraphActivityTaskContext")
    if not isinstance(execution_identity, GraphExecutionIdentity):
        raise TypeError("execution_identity must be GraphExecutionIdentity")
    normalized_stage = identifier(stage_id, "stage_id")
    binding_checksum = checksum(stage_binding_checksum, "stage_binding_checksum")
    if (
        _parent_execution_context_identity(context) != execution_identity
        or not _context_stage_matches(
            context.activity.step_ref.contract_id,
            normalized_stage,
        )
    ):
        raise HarnessValidationError(
            "parent execution context is outside its admitted Graph stage",
            code="task_plan_parent_execution_context_mismatch",
        )
    return execution_identity, normalized_stage, binding_checksum


def _context_stage_matches(contract_id: str, stage_id: str) -> bool:
    """Accept compiler-qualified step ids while retaining exact stage binding."""

    return contract_id == stage_id or contract_id.endswith(":" + stage_id)


def _parent_execution_context_state_key(
    execution_identity: GraphExecutionIdentity,
    *,
    stage_id: str,
    stage_binding_checksum: str,
) -> str:
    return canonical_payload_checksum(
        {
            "schema_version": TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
            "execution_identity": execution_identity.to_dict(),
            "stage_id": stage_id,
            "stage_binding_checksum": stage_binding_checksum,
        }
    )


def _parent_execution_context_state_snapshot(
    context: HarnessGraphActivityTaskContext,
    *,
    execution_identity: GraphExecutionIdentity,
    stage_id: str,
    stage_binding_checksum: str,
) -> TransactionalStateSnapshot:
    return TransactionalStateSnapshot.create(
        namespace=TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
        key=_parent_execution_context_state_key(
            execution_identity,
            stage_id=stage_id,
            stage_binding_checksum=stage_binding_checksum,
        ),
        revision=1,
        payload={
            "schema_version": TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
            "execution_identity": execution_identity.to_dict(),
            "stage_id": stage_id,
            "stage_binding_checksum": stage_binding_checksum,
            "context": context.to_dict(),
        },
    )


def _parent_execution_context_from_state(
    state: TransactionalStateSnapshot,
    *,
    execution_identity: GraphExecutionIdentity,
    stage_id: str,
    stage_binding_checksum: str,
) -> HarnessGraphActivityTaskContext:
    if not isinstance(state, TransactionalStateSnapshot):
        raise TypeError("state must be TransactionalStateSnapshot")
    expected_key = _parent_execution_context_state_key(
        execution_identity,
        stage_id=stage_id,
        stage_binding_checksum=stage_binding_checksum,
    )
    if (
        state.namespace != TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE
        or state.key != expected_key
        or state.revision != 1
    ):
        raise HarnessValidationError(
            "parent execution context state envelope is invalid",
            code="task_plan_parent_execution_context_corrupt",
        )
    payload = thaw_canonical_json(state.payload)
    expected_fields = {
        "schema_version",
        "execution_identity",
        "stage_id",
        "stage_binding_checksum",
        "context",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise HarnessValidationError(
            "parent execution context state payload is invalid",
            code="task_plan_parent_execution_context_corrupt",
        )
    try:
        stored_identity = GraphExecutionIdentity.from_dict(
            payload["execution_identity"]
        )
        context = HarnessGraphActivityTaskContext.from_dict(payload["context"])
    except (HarnessValidationError, TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "parent execution context state payload is invalid",
            code="task_plan_parent_execution_context_corrupt",
        ) from exc
    if (
        payload["schema_version"] != TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE
        or stored_identity != execution_identity
        or payload["stage_id"] != stage_id
        or payload["stage_binding_checksum"] != stage_binding_checksum
    ):
        raise HarnessValidationError(
            "parent execution context state identity is invalid",
            code="task_plan_parent_execution_context_corrupt",
        )
    try:
        _require_parent_execution_context_binding(
            context,
            execution_identity=execution_identity,
            stage_id=stage_id,
            stage_binding_checksum=stage_binding_checksum,
        )
    except (HarnessValidationError, TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "parent execution context state binding is invalid",
            code="task_plan_parent_execution_context_corrupt",
        ) from exc
    return context


def _require_registered_parent_execution_context(
    state: TransactionalStateSnapshot,
    *,
    expected: TransactionalStateSnapshot,
    execution_identity: GraphExecutionIdentity,
    stage_id: str,
    stage_binding_checksum: str,
) -> HarnessGraphActivityTaskContext:
    context = _parent_execution_context_from_state(
        state,
        execution_identity=execution_identity,
        stage_id=stage_id,
        stage_binding_checksum=stage_binding_checksum,
    )
    if state != expected:
        raise HarnessValidationError(
            "parent execution context is already registered with different content",
            code="task_plan_parent_execution_context_conflict",
        )
    return context


def _graph_event_context_for_task_plan_event(
    event: TaskPlanEvent,
) -> GraphEventContext:
    assert event.graph_id is not None
    assert event.graph_version is not None
    assert event.graph_schema_version is not None
    assert event.compiler_version is not None
    return GraphEventContext(
        identity=GraphRunIdentity(
            run_id=event.run_id,
            graph_id=event.graph_id,
            graph_version=event.graph_version,
            graph_ref=f"{event.graph_id}@{event.graph_version}",
            graph_checksum=event.graph_checksum,
        ),
        execution_version=GraphEventExecutionVersion(
            graph_schema_version=event.graph_schema_version,
            compiler_version=event.compiler_version,
            normalized_graph_checksum=event.graph_checksum,
        ),
    )


def _require_projection_matches_event(
    projection: TaskPlanProjection,
    event: TaskPlanEvent,
) -> None:
    if (
        projection.run_id != event.run_id
        or projection.stage_id != event.stage_id
        or projection.graph_checksum != event.graph_checksum
    ):
        raise HarnessValidationError(
            "TaskPlan projection identity conflicts with its event",
            code="task_plan_projection_mismatch",
        )
    if event.plan_id is not None and (
        projection.plan_id != event.plan_id
        or projection.plan_version != event.plan_version
    ):
        raise HarnessValidationError(
            "TaskPlan projection plan version conflicts with its event",
            code="task_plan_projection_mismatch",
        )
    graph_fields = (
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_schema_version",
        "compiler_version",
        "condition_policy_version",
        "stage_binding_checksum",
        "stage_identity_schema",
        "stage_identity_checksum",
    )
    if any(
        getattr(projection, name) != getattr(event, name)
        for name in graph_fields
    ):
        raise HarnessValidationError(
            "Graph-only TaskPlan projection identity conflicts with its event",
            code="task_plan_projection_mismatch",
        )


__all__ = [
    "DurableTaskPlanStore",
    "TASK_PLAN_CAPACITY_STATE_NAMESPACE",
    "TASK_PLAN_GATE_EVIDENCE_SCHEMA",
    "TASK_PLAN_GATE_INPUT_SCHEMA",
    "TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE",
    "TASK_PLAN_EVENT_SOURCE",
    "TASK_PLAN_STORAGE_EXTENSION",
    "TASK_PLAN_STORAGE_SCHEMA",
    "TaskPlanArtifactStorePort",
    "TaskPlanGateArtifactOwnerPort",
    "TaskPlanGateArtifactRefs",
    "TaskPlanGateArtifactWriterPort",
    "TaskPlanGateEvidenceReaderPort",
    "TaskPlanGateVerificationArtifacts",
]
