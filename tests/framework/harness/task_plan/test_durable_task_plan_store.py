from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from threading import Lock

import pytest

from backend.research.graphs import (
    RESEARCH_DYNAMIC_STAGE_ID,
    build_dynamic_paper_analysis_graph_definition,
)
from framework.agent.artifacts.models import ArtifactRef, ArtifactWriteRequest
from framework.events.canonical import EventCandidate, StoredEvent
from framework.events.errors import (
    EventStoreContentionError,
    EventStreamVersionConflictError,
)
from framework.events.projection import (
    GRAPH_EVENT_CONTEXT_EXTENSION,
    graph_event_context,
)
from framework.events.runtime.models import (
    AppendResult,
    EventPage,
    StreamReadRequest,
    TransactionalStateSnapshot,
)
from framework.events.runtime.publisher import EventPublishRequest, EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.checkpoint import (
    TASK_PLAN_CHECKPOINT_SCHEMA_V3,
    TASK_PLAN_CHECKPOINT_SCHEMA_V4,
)
from framework.harness.task_plan.models import TaskAdmissionOwner
from framework.harness.task_plan.replay import (
    TASK_PLAN_REPLAY_REDUCER_VERSION_V3,
    TASK_PLAN_REPLAY_REDUCER_VERSION_V4,
)
from framework.harness.task_plan.store import (
    LogicalTaskReadiness,
    TASK_PLAN_EVENT_SCHEMA_V2,
    TASK_PLAN_EVENT_SCHEMA_V3,
    TaskQueueAdmissionEvidence,
    _result_event,
    _terminal_result_event,
)
from framework.harness.task_plan import (
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
    DurableTaskPlanStore,
    GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
    InMemoryTaskPlanStore,
    TASK_PLAN_QUEUE_METADATA_KEY,
    TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2,
    TASK_PLAN_QUEUE_READBACK_SCHEMA_V2,
    TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2,
    PlanBuildBudget,
    PlanCandidate,
    PlanPatch,
    PlanPatchOperation,
    PlanPatchOperationType,
    TaskAcceptanceCriteria,
    TaskBudget,
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
    TaskLifecycle,
    TaskOutputContract,
    TaskPlanEvent,
    TaskPlanGateArtifactOwnerPort,
    TaskPlanGateEvidence,
    TaskPlanGateRegistry,
    TaskPlanCheckpoint,
    TaskPlanContractKind,
    TaskPlanReadyDecision,
    TaskPlanResultVerificationRequest,
    TaskPlanResultVerifier,
    TaskPlanRecoveryService,
    TaskPlanReplayReducer,
    TaskPlanQueueProjection,
    TaskPlanQueueReclaimContinuation,
    TaskPlanQueueReadback,
    TaskPlanScheduler,
    TaskPlanValidationContext,
    TaskPlanValidator,
    TaskPlanStageBinding,
    TaskPlanStageIdentity,
    TaskResultRecord,
    TaskRetryPolicy,
    TaskSpec,
    ValidatedTaskPlan,
    materialize_queue_task,
    task_instance_for_attempt,
)
from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from framework.harness.task_plan.patches import TaskPlanPatchValidator
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.activity import HarnessWorkerType
from framework.workers.models.status import TaskStatus as WorkerTaskStatus
from framework.workers.models.task import Task as WorkerTask
from infrastructure.storage.events.sqlite import SQLiteEventStore
from infrastructure.storage.artifacts import FilesystemArtifactStore
from infrastructure.storage.workers.redis_queue import RedisStreamTaskQueue
from infrastructure.storage.workers.task_plan_queue import (
    RedisTaskPlanQueueReadAdapter,
)
from tests.fixtures.task_plan import (
    InMemoryTaskPlanGateArtifactWriter,
    build_task_plan_stage_binding,
)


FIXED_NOW = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)


class _Worker:
    worker_type = HarnessWorkerType.LLM

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.worker_version = "1"

    def execute(self, task):
        return {"status": "succeeded", "task_id": task.get("task_id")}


class _TaskPlanQueueReader:
    def __init__(self, readbacks=()) -> None:
        self.readbacks = tuple(readbacks)
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def read_task_plan_queue(
        self,
        *,
        queue_name: str,
        task_instance_ids: tuple[str, ...],
    ):
        self.calls.append((queue_name, task_instance_ids))
        return self.readbacks


class _ArtifactStore:
    """Small immutable artifact adapter used to exercise the durable boundary."""

    def __init__(self) -> None:
        self._content: dict[tuple[str, str], bytes] = {}

    def write(self, artifact: ArtifactWriteRequest) -> ArtifactRef:
        content = artifact.content_bytes()
        if artifact.relative_path is None or artifact.artifact_id is None:
            raise AssertionError("TaskPlan adapter must pin artifact identity and path")
        key = (artifact.run_id, artifact.relative_path)
        previous = self._content.get(key)
        if previous is not None and previous != content:
            raise AssertionError("immutable artifact identity was reused")
        self._content[key] = content
        return ArtifactRef(
            artifact_id=artifact.artifact_id,
            run_id=artifact.run_id,
            artifact_type=artifact.artifact_type,
            path=artifact.relative_path,
            content_type=artifact.content_type,
            size_bytes=len(content),
            checksum=_sha256(content),
            redacted=artifact.redacted,
            metadata=dict(artifact.metadata),
        )

    def read(self, artifact_ref: ArtifactRef) -> bytes:
        try:
            content = self._content[(artifact_ref.run_id, artifact_ref.path)]
        except KeyError as exc:
            raise FileNotFoundError(artifact_ref.path) from exc
        if artifact_ref.checksum is not None and _sha256(content) != artifact_ref.checksum:
            raise ValueError("artifact checksum mismatch")
        if artifact_ref.size_bytes is not None and len(content) != artifact_ref.size_bytes:
            raise ValueError("artifact size mismatch")
        return content

    def exists(self, artifact_ref: ArtifactRef) -> bool:
        return (artifact_ref.run_id, artifact_ref.path) in self._content


class _FailOnceGateEvidenceArtifactStore(_ArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def write(self, artifact: ArtifactWriteRequest) -> ArtifactRef:
        if (
            not self.failed
            and artifact.relative_path is not None
            and "/gate_evidence/" in artifact.relative_path
        ):
            self.failed = True
            raise OSError("interrupted gate evidence write")
        return super().write(artifact)


def _sha256(content: bytes) -> str:
    from hashlib import sha256

    return sha256(content).hexdigest()


class _UnitOfWork:
    def __init__(self, store: "_EventStore") -> None:
        self.store = store
        self.pending: list[StoredEvent] = []
        self.pending_states: dict[
            tuple[str, str], TransactionalStateSnapshot
        ] = {}
        self.finished = False

    def __enter__(self) -> "_UnitOfWork":
        self.store._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            if not self.finished:
                self.rollback()
        finally:
            self.store._lock.release()
        return False

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        stream_events = self.store._stream_events(event.stream_id, event.tenant_id)
        current = (stream_events[-1].stream_sequence if stream_events else 0) + len(self.pending)
        if expected_last_sequence is not None and expected_last_sequence != current:
            raise EventStreamVersionConflictError(
                stream_id=event.stream_id,
                expected_last_sequence=expected_last_sequence,
                actual_last_sequence=current,
            )
        for existing in (*stream_events, *self.pending):
            if existing.event_id == event.event_id:
                if existing.content_checksum != event.content_checksum:
                    raise ValueError("event identity collision")
                return AppendResult(event=existing, created=False, pending_delivery_count=0)
        stored = StoredEvent(
            candidate=event,
            observed_at=FIXED_NOW,
            stream_sequence=current + 1,
        )
        self.pending.append(stored)
        return AppendResult(event=stored, created=True, pending_delivery_count=0)

    def commit(self) -> None:
        if self.finished:
            raise RuntimeError("unit of work already finished")
        self.store._events.extend(self.pending)
        self.store._transactional_states.update(self.pending_states)
        self.finished = True

    def rollback(self) -> None:
        self.pending.clear()
        self.pending_states.clear()
        self.finished = True

    def load_transactional_state(
        self,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        state_key = (namespace, key)
        return self.pending_states.get(
            state_key,
            self.store._transactional_states.get(state_key),
        )

    def cas_transactional_state(
        self,
        *,
        namespace: str,
        key: str,
        expected_revision: int | None,
        expected_checksum: str | None,
        next_snapshot: TransactionalStateSnapshot,
    ) -> TransactionalStateSnapshot:
        if not isinstance(next_snapshot, TransactionalStateSnapshot):
            raise TypeError("next_snapshot must be TransactionalStateSnapshot")
        if (next_snapshot.namespace, next_snapshot.key) != (namespace, key):
            raise ValueError("next_snapshot namespace/key must match the CAS target")
        if (expected_revision is None) != (expected_checksum is None):
            raise ValueError(
                "expected_revision and expected_checksum must both be set or absent"
            )

        state_key = (namespace, key)
        current = self.load_transactional_state(namespace, key)
        if current == next_snapshot:
            return current
        if current is None:
            if expected_revision is not None or next_snapshot.revision != 1:
                raise EventStoreContentionError(
                    "transactional state creation requires revision 1"
                )
        else:
            if expected_revision is None:
                raise EventStoreContentionError(
                    "transactional state already exists"
                )
            if (
                current.revision != expected_revision
                or current.checksum != expected_checksum
            ):
                raise EventStoreContentionError(
                    "transactional state revision/checksum conflict"
                )
            if next_snapshot.revision != current.revision + 1:
                raise ValueError(
                    "next_snapshot revision must increment current revision by one"
                )
        self.pending_states[state_key] = next_snapshot
        return next_snapshot


class _EventStore:
    """In-process canonical event store with the production stream contract."""

    def __init__(self, *, fail_on_event_type: str | None = None) -> None:
        self._events: list[StoredEvent] = []
        self._transactional_states: dict[
            tuple[str, str], TransactionalStateSnapshot
        ] = {}
        self._lock = Lock()
        self.fail_on_event_type = fail_on_event_type

    def unit_of_work(self) -> _UnitOfWork:
        return _FailingUnitOfWork(self) if self.fail_on_event_type else _UnitOfWork(self)

    def get_event(self, event_id: str, *, tenant_id: str | None = None) -> StoredEvent | None:
        with self._lock:
            return next(
                (item for item in self._events if item.event_id == event_id and item.tenant_id == tenant_id),
                None,
            )

    def get_stream_high_watermark(self, stream_id: str, *, tenant_id: str | None = None) -> int | None:
        with self._lock:
            events = self._stream_events(stream_id, tenant_id)
            return events[-1].stream_sequence if events else None

    def load_transactional_state(
        self,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        with self._lock:
            return self._transactional_states.get((namespace, key))

    def read_stream(self, request: StreamReadRequest) -> EventPage:
        with self._lock:
            events = list(self._stream_events(request.stream_id, request.tenant_id))
        high = events[-1].stream_sequence if events else None
        if high is None:
            return EventPage(request.stream_id, (), None, tenant_id=request.tenant_id)
        start = request.cursor.after_sequence if request.cursor is not None else 0
        through = request.through_sequence or high
        selected = [
            item
            for item in events
            if start < item.stream_sequence <= through
            and (not request.event_types or item.event_type in request.event_types)
            and (not request.data_schemas or item.data_schema in request.data_schemas)
        ][: request.limit]
        next_cursor = None
        if selected and selected[-1].stream_sequence < through:
            from framework.events.runtime.models import StreamSequenceCursor

            next_cursor = StreamSequenceCursor(
                request.stream_id,
                selected[-1].stream_sequence,
                through,
                request.tenant_id,
            )
        return EventPage(
            request.stream_id,
            tuple(selected),
            high,
            next_cursor,
            request.tenant_id,
        )

    def _stream_events(self, stream_id: str, tenant_id: str | None) -> list[StoredEvent]:
        return [
            item
            for item in self._events
            if item.stream_id == stream_id and item.tenant_id == tenant_id
        ]


class _ReadOnlyEventReader:
    """Deliberately lacks the shared transactional-state read capability."""

    def __init__(self, delegate: _EventStore) -> None:
        self._delegate = delegate

    def get_event(
        self,
        event_id: str,
        *,
        tenant_id: str | None = None,
    ) -> StoredEvent | None:
        return self._delegate.get_event(event_id, tenant_id=tenant_id)

    def read_stream(self, request: StreamReadRequest) -> EventPage:
        return self._delegate.read_stream(request)

    def get_stream_high_watermark(
        self,
        stream_id: str,
        *,
        tenant_id: str | None = None,
    ) -> int | None:
        return self._delegate.get_stream_high_watermark(
            stream_id,
            tenant_id=tenant_id,
        )


class _FailingUnitOfWork(_UnitOfWork):
    def append_event(self, event: EventCandidate, *, expected_last_sequence: int | None = None) -> AppendResult:
        if event.event_type == self.store.fail_on_event_type:
            raise RuntimeError("injected batch failure")
        return super().append_event(event, expected_last_sequence=expected_last_sequence)


class _ConflictOnceRuntime:
    def __init__(self, delegate: EventRuntime) -> None:
        self.delegate = delegate
        self.conflicts = 0

    def publish(self, event: EventPublishRequest, *, expected_last_sequence=None, unit_of_work=None):
        if self.conflicts == 0:
            self.conflicts += 1
            raise EventStreamVersionConflictError(
                stream_id=event.stream_id,
                expected_last_sequence=int(expected_last_sequence or 0),
                actual_last_sequence=int(expected_last_sequence or 0) + 1,
            )
        return self.delegate.publish(event, expected_last_sequence=expected_last_sequence, unit_of_work=unit_of_work)

    def publish_batch(self, events: Sequence[EventPublishRequest], *, expected_last_sequence=None):
        return self.delegate.publish_batch(events, expected_last_sequence=expected_last_sequence)

    def publish_batch_with_state_cas(
        self,
        events: Sequence[EventPublishRequest],
        **kwargs,
    ):
        return self.delegate.publish_batch_with_state_cas(events, **kwargs)

    def compare_and_swap_transactional_state(self, next_snapshot, **kwargs):
        return self.delegate.compare_and_swap_transactional_state(
            next_snapshot,
            **kwargs,
        )


class _FailBatchOnceRuntime:
    """Leave prewritten artifacts unreachable, then allow an idempotent retry."""

    def __init__(self, delegate: EventRuntime) -> None:
        self.delegate = delegate
        self.failed = False

    def publish(self, event: EventPublishRequest, **kwargs):
        return self.delegate.publish(event, **kwargs)

    def publish_batch(self, events: Sequence[EventPublishRequest], **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("injected result batch interruption")
        return self.delegate.publish_batch(events, **kwargs)

    def publish_batch_with_state_cas(
        self,
        events: Sequence[EventPublishRequest],
        **kwargs,
    ):
        return self.delegate.publish_batch_with_state_cas(events, **kwargs)

    def compare_and_swap_transactional_state(self, next_snapshot, **kwargs):
        return self.delegate.compare_and_swap_transactional_state(
            next_snapshot,
            **kwargs,
        )


class _PublishOnlyRuntime:
    """Implements event publication but intentionally lacks state CAS."""

    def __init__(self, delegate: EventRuntime) -> None:
        self.delegate = delegate

    def publish(self, event: EventPublishRequest, **kwargs):
        return self.delegate.publish(event, **kwargs)

    def publish_batch(self, events: Sequence[EventPublishRequest], **kwargs):
        return self.delegate.publish_batch(events, **kwargs)


def _runtime(store: _EventStore) -> EventRuntime:
    return EventRuntime(store=store, schema_catalog=default_event_schema_catalog(), monotonic=lambda: 1.0)


def _policy_and_registry(*, two_tasks: bool = False, explicit_execution_budget: bool = False):
    capabilities = ("research.structure", "research.helper") if two_tasks else ("research.structure",)
    roles = ("analysis.structure", "analysis.helper") if two_tasks else ("analysis.structure",)
    workers = [_Worker(f"{capability}-worker") for capability in capabilities]
    registrations = tuple(
        TaskCapabilityRegistration(
            capability=capability,
            worker_binding=HarnessWorkerBinding(
                HarnessContractReference(HarnessContractKind.WORKER, worker.worker_id, "1"),
                HarnessWorkerType.LLM,
                worker,
            ),
            worker_contract_ref=f"{capability}-contract@1",
            input_schema_ref="schema://input@1",
            output_schema_ref=f"schema://{roles[index]}@1",
        )
        for index, (capability, worker) in enumerate(zip(capabilities, workers, strict=True))
    )
    per_task_budget = (
        TaskBudget(max_turns=1, token_limit=4096, time_limit_ms=900_000)
        if explicit_execution_budget
        else TaskBudget(max_turns=1)
    )
    aggregate_task_budget = (
        TaskBudget(max_turns=8, token_limit=32_768, time_limit_ms=7_200_000)
        if explicit_execution_budget
        else TaskBudget(max_turns=8)
    )
    policy = TaskPlanPolicy(
        policy_id="research.analysis",
        version="1",
        stage_id="dynamic_analysis_stage",
        allowed_worker_capabilities=capabilities,
        allowed_subagent_ids=(),
        allowed_tool_ids=(),
        allowed_memory_namespaces=(),
        allowed_input_refs=("document",),
        allowed_output_roles=roles,
        required_output_roles=("analysis.structure",),
        allowed_output_schema_refs=tuple(f"schema://{role}@1" for role in roles),
        allowed_gate_refs=("SummaryGate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={capability: f"{capability}-worker@1" for capability in capabilities},
        required_worker_contract_refs={capability: f"{capability}-contract@1" for capability in capabilities},
        max_tasks=8,
        max_depth=4,
        max_parallelism=2,
        max_replans=2,
        max_task_attempts=2,
        max_plan_build_calls=1,
        max_plan_build_turns=1,
        max_plan_build_tool_calls=0,
        per_task_budget=per_task_budget,
        aggregate_task_budget=aggregate_task_budget,
    )
    return policy, TaskCapabilityRegistry(registrations)


def _task(task_id: str, *, capability: str = "research.structure", role: str = "analysis.structure") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        objective=f"Analyze {task_id}",
        worker_capability=capability,
        input_refs=("document",),
        output_contract=TaskOutputContract(f"schema://{role}@1", role),
        acceptance_criteria=TaskAcceptanceCriteria(("SummaryGate@1",)),
        budget_request=TaskBudget(max_turns=1),
        retry_policy=TaskRetryPolicy(max_attempts=1),
    )


def _candidate(tasks: tuple[TaskSpec, ...], *, stage_binding, two_tasks: bool = False) -> PlanCandidate:
    stage_identity = TaskPlanStageIdentity(
        run_id="durable-run",
        stage_binding=stage_binding,
    )
    return PlanCandidate.for_stage(
        stage_identity=stage_identity,
        candidate_id="candidate-1" if not two_tasks else "candidate-2",
        input_context_refs=("document",),
        tasks=tasks,
        required_output_roles=("analysis.structure",),
        generated_by="planner@1",
        requested_plan_budget=PlanBuildBudget(),
    )


def _accepted_plan(
    tasks: tuple[TaskSpec, ...],
    *,
    two_tasks: bool = False,
    explicit_execution_budget: bool = False,
):
    policy, registry = _policy_and_registry(
        two_tasks=two_tasks,
        explicit_execution_budget=explicit_execution_budget,
    )
    if two_tasks:
        # Multi-task fixtures declare their business execution order explicitly;
        # the scheduler still materializes every currently eligible task into
        # the complete logical READY order before admitting its head.
        tasks = tuple(
            replace(task, priority=index)
            for index, task in enumerate(tasks)
        )
    if explicit_execution_budget:
        tasks = tuple(
            replace(task, budget_request=policy.per_task_budget)
            for task in tasks
        )
    stage_binding = build_task_plan_stage_binding(
        graph_id="research.dynamic",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        input_keys=("document",),
    )
    candidate = _candidate(
        tasks,
        stage_binding=stage_binding,
        two_tasks=two_tasks,
    )
    context = TaskPlanValidationContext(
        run_id=candidate.run_id,
        stage_binding=stage_binding,
        available_input_refs=("document",),
        registered_gate_refs=policy.allowed_gate_refs,
    )
    plan = TaskPlanValidator().accept(candidate, policy, registry, context=context, accepted_at="2026-08-02T00:00:00Z")
    return candidate, plan, policy, registry


def _graph_only_candidate_and_plan(
    *,
    run_id: str = "durable-run",
    graph_id: str | None = None,
):
    legacy_candidate, legacy_plan, _, _ = _accepted_plan((_task("structure"),))
    graph_definition = build_dynamic_paper_analysis_graph_definition()
    if graph_id is not None:
        graph_definition = replace(
            graph_definition,
            graph_id=graph_id,
            root=replace(graph_definition.root, graph_id=graph_id),
            definition_checksum=None,
        )
    graph = HarnessGraphCompiler().compile(graph_definition).graph
    stage_identity = TaskPlanStageIdentity(
        run_id=run_id,
        stage_binding=TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID),
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=stage_identity,
        candidate_id="graph-candidate-1",
        input_context_refs=legacy_candidate.input_context_refs,
        tasks=legacy_candidate.tasks,
        required_output_roles=legacy_candidate.required_output_roles,
        generated_by=legacy_candidate.generated_by,
        requested_plan_budget=legacy_candidate.requested_plan_budget,
    )
    assert legacy_plan.policy_checksum is not None
    plan = ValidatedTaskPlan.from_candidate(
        candidate,
        plan_id="graph-plan-1",
        version=1,
        parent_plan_id=None,
        source_candidate_ref=candidate.candidate_checksum,
        policy_ref=legacy_plan.policy_ref,
        policy_checksum=legacy_plan.policy_checksum,
        tasks=legacy_plan.tasks,
        required_output_roles=legacy_plan.required_output_roles,
        limits=legacy_plan.limits,
        accepted_at=legacy_plan.accepted_at,
    )
    return candidate, plan


def _store(event_store: _EventStore, artifacts: _ArtifactStore, *, runtime=None) -> DurableTaskPlanStore:
    return DurableTaskPlanStore(
        runtime or _runtime(event_store),
        event_store,
        artifact_store=artifacts,
        clock=lambda: FIXED_NOW,
    )


def _gate_verification_fixture(plan: ValidatedTaskPlan):
    instance = task_instance_for_attempt(plan, "structure", 1)
    worker_result = HarnessWorkerResult(
        status=HarnessWorkerStatus.SUCCEEDED,
        output={"summary": "bounded gate candidate"},
        metrics={"turns": 1},
    )
    input_checksum = canonical_payload_checksum(
        {
            "instance": instance.checksum_projection(),
            "worker_result": worker_result.candidate_payload(),
        }
    )
    evidence = TaskPlanGateEvidence(
        gate_ref="SummaryGate@1",
        input_checksum=input_checksum,
        result_checksum=canonical_payload_checksum(
            worker_result.candidate_payload()
        ),
        passed=True,
    )
    return instance, worker_result, evidence


def _worker_failure_result(
    plan: ValidatedTaskPlan,
    instance,
    owner: TaskPlanGateArtifactOwnerPort,
    *,
    status: HarnessWorkerStatus = HarnessWorkerStatus.FAILED,
    metrics=None,
):
    worker_result = HarnessWorkerResult(
        status=status,
        diagnostics={"reason_code": "worker_unavailable"},
        metrics=metrics or {},
        error="worker unavailable",
    )
    result = TaskPlanResultVerifier(
        TaskPlanGateRegistry(),
        gate_artifact_writer=owner,
    ).verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
        ),
    )
    return result, worker_result


def test_durable_task_plan_store_requires_transactional_state_reader() -> None:
    event_store = _EventStore()

    with pytest.raises(TypeError, match="TransactionalStateReaderPort"):
        DurableTaskPlanStore(
            _runtime(event_store),
            _ReadOnlyEventReader(event_store),
            artifact_store=_ArtifactStore(),
            clock=lambda: FIXED_NOW,
        )


def test_durable_task_plan_store_requires_transactional_state_runtime() -> None:
    event_store = _EventStore()

    with pytest.raises(TypeError, match="TransactionalStateRuntimePort"):
        DurableTaskPlanStore(
            _PublishOnlyRuntime(_runtime(event_store)),
            event_store,
            artifact_store=_ArtifactStore(),
            clock=lambda: FIXED_NOW,
        )


def _lifecycle_event(event_type: str, sequence: int, plan: ValidatedTaskPlan, instance) -> TaskPlanEvent:
    return TaskPlanEvent.for_plan(
        event_type,
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        input_checksum=instance.task_definition_checksum,
        sequence=sequence,
    )


def _start(store: DurableTaskPlanStore, plan: ValidatedTaskPlan, task_id: str):
    scheduler = TaskPlanScheduler()
    projection = store.load_projection(plan.run_id, plan.stage_id)
    decision = scheduler.next_ready_tasks(
        projection,
        plan.limits.max_parallelism,
        plan=plan,
        available_input_refs=("document",),
    )
    target_order = decision.logical_ready_task_ids
    if task_id not in target_order:
        raise AssertionError(f"test task is not logically ready: {task_id}")
    states = {item.task_id: item for item in projection.tasks}
    newly_ready = tuple(
        ready_task_id
        for ready_task_id in target_order
        if states[ready_task_id].status is TaskLifecycle.PENDING
    )
    definitions = {item.task_id: item for item in plan.tasks}
    committed_ready = set(projection.logical_ready_order)
    for ready_task_id in newly_ready:
        committed_ready.add(ready_task_id)
        ordered_prefix = tuple(
            candidate
            for candidate in target_order
            if candidate in committed_ready
        )
        projection = scheduler.reserve_ready_tasks(
            projection,
            TaskPlanReadyDecision(logical_ready_task_ids=ordered_prefix),
        )
        readiness = LogicalTaskReadiness(
            task_id=ready_task_id,
            task_definition_checksum=(
                definitions[ready_task_id].task_definition_checksum
            ),
            logical_ready_order=ordered_prefix,
        )
        sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
        projection = replace(projection, last_sequence=sequence)
        store.commit_event(TaskPlanEvent.for_plan(
            "TASK_READY",
            plan,
            task_id=ready_task_id,
            input_checksum=readiness.task_definition_checksum,
            sequence=sequence,
            payload={"logical_readiness": readiness.to_dict()},
        ), projection)

    projection = store.load_projection(plan.run_id, plan.stage_id)
    if not projection.logical_ready_order or projection.logical_ready_order[0] != task_id:
        raise AssertionError(
            f"test task does not own the next logical READY position: {task_id}"
        )
    state = next(item for item in projection.tasks if item.task_id == task_id)
    instance = task_instance_for_attempt(plan, task_id, state.attempts + 1)
    before_admission = projection
    projection = scheduler.mark_admitted(
        before_admission,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    admission = TaskQueueAdmissionEvidence(
        task_instance=instance,
        budget_before_checksum=TaskPlanBudgetLedger.from_snapshot(
            before_admission.consumed_budget
        ).to_dict()["ledger_checksum"],
        budget_after_checksum=TaskPlanBudgetLedger.from_snapshot(
            projection.consumed_budget
        ).to_dict()["ledger_checksum"],
    )
    sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
    projection = replace(projection, last_sequence=sequence)
    store.commit_event(TaskPlanEvent.for_plan(
        "TASK_QUEUE_ADMITTED",
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        input_checksum=instance.task_definition_checksum,
        sequence=sequence,
        payload={"queue_admission": admission.to_dict()},
    ), projection)

    for event_type, transition in (
        ("TASK_DISPATCHED", lambda value: scheduler.mark_dispatched(value, instance)),
        ("TASK_STARTED", lambda value: scheduler.mark_started(value, instance)),
    ):
        sequence += 1
        projection = replace(transition(projection), last_sequence=sequence)
        store.commit_event(
            _lifecycle_event(event_type, sequence, plan, instance),
            projection,
        )
    return instance


def _result(
    plan: ValidatedTaskPlan,
    instance,
    *,
    status: TaskLifecycle,
    role: str = "analysis.structure",
    gate_artifact_owner: TaskPlanGateArtifactOwnerPort | None = None,
    metrics=None,
) -> TaskResultRecord:
    definition = next(item for item in plan.tasks if item.task_id == instance.task_id)
    if plan.is_graph_only:
        if gate_artifact_owner is None:
            return TaskResultRecord.for_plan(
                plan,
                task_id=instance.task_id,
                task_instance_id=instance.task_instance_id,
                attempt=instance.attempt,
                status=status,
                result_ref=(
                    f"result://{instance.task_id}"
                    if status is TaskLifecycle.SUCCEEDED
                    else None
                ),
                output_refs=(
                    (f"artifact://{instance.task_id}",)
                    if status is TaskLifecycle.SUCCEEDED
                    else ()
                ),
                output_roles=(role,) if status is TaskLifecycle.SUCCEEDED else (),
                output_schema_ref=f"schema://{role}@1",
                error_code=None if status is TaskLifecycle.SUCCEEDED else "worker_failed",
            )
        worker_result = HarnessWorkerResult(
            status=(
                HarnessWorkerStatus.SUCCEEDED
                if status is TaskLifecycle.SUCCEEDED
                else HarnessWorkerStatus.FAILED
            ),
            artifacts=(
                (f"artifact://{instance.task_id}",)
                if status is TaskLifecycle.SUCCEEDED
                else ()
            ),
            metrics=metrics if metrics is not None else {},
            error=None if status is TaskLifecycle.SUCCEEDED else "worker_failed",
        )
        gates = TaskPlanGateRegistry()
        gates.register("SummaryGate@1", lambda _request: True, deterministic=True)
        return TaskPlanResultVerifier(
            gates,
            gate_artifact_writer=gate_artifact_owner,
        ).verify(
            worker_result,
            task=definition,
            request=TaskPlanResultVerificationRequest(
                plan=plan,
                task=definition,
                instance=instance,
                worker_result=worker_result,
            ),
        )
    if status is TaskLifecycle.SUCCEEDED:
        return TaskResultRecord(
            run_id=plan.run_id,
            workflow_id=plan.workflow_id,
            stage_id=plan.stage_id,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            worker_ref=instance.worker_ref,
            task_checksum=instance.task_definition_checksum,
            binding_checksum=definition.binding_checksum,
            status=status,
            result_ref=f"result://{instance.task_id}",
            output_refs=(f"artifact://{instance.task_id}",),
            output_roles=(role,),
            output_schema_ref=f"schema://{role}@1",
        )
    return TaskResultRecord(
        run_id=plan.run_id,
        workflow_id=plan.workflow_id,
        stage_id=plan.stage_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        worker_ref=instance.worker_ref,
        task_checksum=instance.task_definition_checksum,
        binding_checksum=definition.binding_checksum,
        status=status,
        error_code="worker_failed",
    )


def test_durable_store_rebuilds_plan_projection_and_artifacts_after_reopen():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan, _, _ = _accepted_plan((_task("structure"),))

    assert store.append_candidate(candidate) == candidate.candidate_checksum
    assert store.accept_plan(plan) == plan.plan_checksum
    reopened = _store(event_store, artifacts)

    assert reopened.plan(plan.run_id, plan.stage_id) == plan
    assert reopened.plan(plan.run_id, plan.stage_id, 1) == plan
    projection = reopened.load_projection(plan.run_id, plan.stage_id)
    assert projection.plan_checksum == plan.plan_checksum
    assert projection.last_sequence == 2
    assert [event.event_type for event in reopened.read_events(plan.run_id, plan.stage_id)] == [
        "PLAN_CANDIDATE_BUILT",
        "PLAN_ACCEPTED",
    ]


def test_gate_artifact_owner_round_trips_typed_input_and_evidence_in_memory():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, evidence = _gate_verification_fixture(plan)

    assert isinstance(store, TaskPlanGateArtifactOwnerPort)
    refs = store.persist_gate_verification(
        plan,
        instance,
        worker_result,
        (evidence,),
    )
    restored = store.read_gate_evidence(
        plan.run_id,
        plan.stage_id,
        refs.evidence_checksums,
    )

    assert refs.input_checksum == evidence.input_checksum
    assert refs.evidence_checksums == (evidence.evidence_checksum,)
    assert restored.instance == instance
    assert restored.worker_result == worker_result
    assert restored.evidences == (evidence,)
    assert event_store._events == []

    with pytest.raises(TypeError, match="TaskPlanGateEvidence"):
        store.persist_gate_verification(
            plan,
            instance,
            worker_result,
            ({"passed": True},),
        )


@pytest.mark.parametrize(
    ("gate_passes", "expected_status"),
    (
        (True, TaskLifecycle.SUCCEEDED),
        (False, TaskLifecycle.FAILED),
    ),
)
def test_result_verifier_persists_real_gate_verdict_before_building_result(
    gate_passes,
    expected_status,
):
    artifacts = _ArtifactStore()
    store = _store(_EventStore(), artifacts)
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, _ = _gate_verification_fixture(plan)
    gates = TaskPlanGateRegistry()
    gates.register(
        "SummaryGate@1",
        lambda _request: gate_passes,
        deterministic=True,
    )
    verifier = TaskPlanResultVerifier(
        gates,
        gate_artifact_writer=store,
    )

    result = verifier.verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
        ),
    )

    assert result.status is expected_status
    assert tuple(
        "gate_input" if "/gate_input/" in path else "gate_evidence"
        for _, path in artifacts._content
    ) == ("gate_input", "gate_evidence")
    restored = store.read_gate_evidence(
        plan.run_id,
        plan.stage_id,
        result.gate_evidence_refs,
    )
    assert restored.instance == instance
    assert restored.worker_result == worker_result
    assert tuple(item.passed for item in restored.evidences) == (gate_passes,)


def test_result_verifier_fails_closed_when_gate_artifact_owner_is_missing():
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, _ = _gate_verification_fixture(plan)
    gate_calls = 0
    gates = TaskPlanGateRegistry()

    def gate(_request):
        nonlocal gate_calls
        gate_calls += 1
        return True

    gates.register("SummaryGate@1", gate, deterministic=True)
    verifier = TaskPlanResultVerifier(gates)

    with pytest.raises(HarnessValidationError) as error:
        verifier.verify(
            worker_result,
            task=plan.tasks[0],
            request=TaskPlanResultVerificationRequest(
                plan=plan,
                task=plan.tasks[0],
                instance=instance,
                worker_result=worker_result,
            ),
        )
    assert error.value.code == "task_plan_gate_artifact_owner_unavailable"
    assert gate_calls == 0


def test_result_verifier_retries_after_interrupted_gate_evidence_write():
    artifacts = _FailOnceGateEvidenceArtifactStore()
    store = _store(_EventStore(), artifacts)
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, _ = _gate_verification_fixture(plan)
    gates = TaskPlanGateRegistry()
    gates.register("SummaryGate@1", lambda _request: True, deterministic=True)
    verifier = TaskPlanResultVerifier(gates, gate_artifact_writer=store)
    request = TaskPlanResultVerificationRequest(
        plan=plan,
        task=plan.tasks[0],
        instance=instance,
        worker_result=worker_result,
    )

    with pytest.raises(HarnessValidationError) as interrupted:
        verifier.verify(worker_result, task=plan.tasks[0], request=request)
    assert interrupted.value.code == "task_plan_artifact_store_failed"
    assert [
        path for _, path in artifacts._content if "/gate_input/" in path
    ]
    assert not [
        path for _, path in artifacts._content if "/gate_evidence/" in path
    ]

    result = verifier.verify(worker_result, task=plan.tasks[0], request=request)
    restored = store.read_gate_evidence(
        plan.run_id,
        plan.stage_id,
        result.gate_evidence_refs,
    )
    assert restored.refs.input_checksum == restored.evidences[0].input_checksum


@pytest.mark.parametrize(
    ("kind", "mutation", "expected_code"),
    (
        (
            "gate_input",
            lambda payload: payload.update(schema_version="unknown/gate-input/v99"),
            "task_plan_gate_artifact_schema_unsupported",
        ),
        (
            "gate_input",
            lambda payload: payload["gate_input"].update(unexpected=True),
            "task_plan_gate_artifact_corrupt",
        ),
        (
            "gate_input",
            lambda payload: payload["gate_input"]["worker_result"]["output"].update(
                summary="tampered"
            ),
            "task_plan_gate_artifact_checksum_mismatch",
        ),
        (
            "gate_evidence",
            lambda payload: payload["gate_evidence"].update(unexpected=True),
            "task_plan_gate_artifact_corrupt",
        ),
        (
            "gate_evidence",
            lambda payload: payload["gate_evidence"].pop("reason_code"),
            "task_plan_gate_artifact_corrupt",
        ),
    ),
)
def test_gate_artifact_reader_rejects_schema_drift_and_pollution(
    kind,
    mutation,
    expected_code,
):
    artifacts = _ArtifactStore()
    store = _store(_EventStore(), artifacts)
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, evidence = _gate_verification_fixture(plan)
    refs = store.persist_gate_verification(
        plan,
        instance,
        worker_result,
        (evidence,),
    )
    key = next(key for key in artifacts._content if f"/{kind}/" in key[1])
    payload = json.loads(artifacts._content[key].decode("utf-8"))
    mutation(payload)
    artifacts._content[key] = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")

    with pytest.raises(HarnessValidationError) as error:
        store.read_gate_evidence(
            plan.run_id,
            plan.stage_id,
            refs.evidence_checksums,
        )
    assert error.value.code == expected_code


def test_gate_artifact_reader_rejects_missing_scope_and_same_ref_conflicts():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = DurableTaskPlanStore(
        _runtime(event_store),
        event_store,
        artifact_store=artifacts,
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, evidence = _gate_verification_fixture(plan)
    refs = store.persist_gate_verification(
        plan,
        instance,
        worker_result,
        (evidence,),
    )
    input_key = next(
        key for key in artifacts._content if "/gate_input/" in key[1]
    )
    original = artifacts._content.pop(input_key)
    with pytest.raises(HarnessValidationError) as missing:
        store.read_gate_evidence(
            plan.run_id,
            plan.stage_id,
            refs.evidence_checksums,
        )
    assert missing.value.code == "task_plan_artifact_missing"

    artifacts._content[input_key] = original
    wrong_tenant = DurableTaskPlanStore(
        _runtime(event_store),
        event_store,
        artifact_store=artifacts,
        tenant_id="tenant-b",
        clock=lambda: FIXED_NOW,
    )
    with pytest.raises(HarnessValidationError) as scope:
        wrong_tenant.read_gate_evidence(
            plan.run_id,
            plan.stage_id,
            refs.evidence_checksums,
        )
    assert scope.value.code == "task_plan_gate_artifact_scope_mismatch"

    polluted = json.loads(original.decode("utf-8"))
    polluted["scope"]["tenant_id"] = "tenant-b"
    artifacts._content[input_key] = json.dumps(
        polluted,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    with pytest.raises(HarnessValidationError) as conflict:
        store.persist_gate_verification(
            plan,
            instance,
            worker_result,
            (evidence,),
        )
    assert conflict.value.code == "task_plan_artifact_checksum_mismatch"


def test_gate_artifacts_reopen_with_filesystem_and_sqlite_and_reject_tampering(
    tmp_path,
):
    database = tmp_path / "events.sqlite3"
    artifact_root = tmp_path / "artifacts"
    events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    store = DurableTaskPlanStore(
        _runtime(events),
        events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    _, plan, _, _ = _accepted_plan((_task("structure"),))
    instance, worker_result, evidence = _gate_verification_fixture(plan)
    refs = store.persist_gate_verification(
        plan,
        instance,
        worker_result,
        (evidence,),
    )

    reopened_events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    reopened = DurableTaskPlanStore(
        _runtime(reopened_events),
        reopened_events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    restored = reopened.read_gate_evidence(
        plan.run_id,
        plan.stage_id,
        refs.evidence_checksums,
    )
    assert restored.instance == instance
    assert restored.evidences == (evidence,)

    evidence_path = next(
        path
        for path in (artifact_root / plan.run_id).rglob("*.json")
        if "gate_evidence" in path.parts
    )
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["gate_evidence"]["reason_code"] = "tampered-verdict"
    evidence_path.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessValidationError) as tampered:
        reopened.read_gate_evidence(
            plan.run_id,
            plan.stage_id,
            refs.evidence_checksums,
        )
    assert tampered.value.code == "task_plan_gate_artifact_checksum_mismatch"


@pytest.mark.parametrize(
    "worker_status",
    (
        HarnessWorkerStatus.FAILED,
        HarnessWorkerStatus.BLOCKED,
        HarnessWorkerStatus.WAITING_APPROVAL,
    ),
)
def test_in_memory_worker_failure_proof_binds_terminal_status_and_offline_replay(
    worker_status,
):
    candidate, plan = _graph_only_candidate_and_plan()
    owner = InMemoryTaskPlanGateArtifactWriter()
    store = InMemoryTaskPlanStore(gate_evidence_reader=owner)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    result, worker_result = _worker_failure_result(
        plan,
        instance,
        owner,
        status=worker_status,
        metrics={"turns": 1},
    )

    assert result.status is TaskLifecycle.FAILED
    assert result.error_code == "task_worker_failed"
    assert result.worker_result_proof_ref is not None
    assert result.verified_gate_refs == ()
    assert result.gate_evidence_refs == ()
    restored = owner.read_worker_result_input(
        plan.run_id,
        plan.stage_id,
        result.worker_result_proof_ref,
    )
    assert restored.instance == instance
    assert restored.worker_result == worker_result

    assert store.append_result(result) == result.result_checksum
    events = store.read_events(plan.run_id, plan.stage_id)
    assert events[-2].payload["worker_result_proof_ref"] == result.worker_result_proof_ref
    assert events[-1].payload["worker_result_proof_ref"] == result.worker_result_proof_ref
    replay = TaskPlanReplayReducer(gate_evidence_reader=owner).replay(
        (plan,),
        events,
        results=(result,),
    )
    assert replay.projection.tasks[0].status is TaskLifecycle.FAILED


def test_worker_failure_result_rejects_missing_forged_and_wrong_attempt_proofs():
    candidate, plan = _graph_only_candidate_and_plan()
    owner = InMemoryTaskPlanGateArtifactWriter()
    store = InMemoryTaskPlanStore(gate_evidence_reader=owner)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    result, _ = _worker_failure_result(
        plan,
        instance,
        owner,
        metrics={"turns": 1},
    )

    with pytest.raises(HarnessValidationError) as missing:
        replace(result, worker_result_proof_ref=None)
    assert missing.value.code == "task_plan_result_invalid"
    with pytest.raises(HarnessValidationError) as wrong_error:
        replace(result, error_code="forged_worker_error")
    assert wrong_error.value.code == "task_plan_result_invalid"

    payload = result.to_dict()
    payload["usage"] = {"turns": 1, "forged": 1}
    payload["result_checksum"] = canonical_payload_checksum(
        {key: value for key, value in payload.items() if key != "result_checksum"}
    )
    forged = TaskResultRecord.from_dict(payload)
    with pytest.raises(HarnessValidationError) as self_checksummed:
        store.append_result(forged)
    assert self_checksummed.value.code == "task_plan_result_proof_mismatch"

    other_instance = task_instance_for_attempt(plan, instance.task_id, 2)
    _, other_worker_result = _worker_failure_result(
        plan,
        other_instance,
        owner,
        metrics={"turns": 1},
    )
    wrong_attempt_ref = owner.persist_worker_result_input(
        plan,
        other_instance,
        other_worker_result,
    )
    with pytest.raises(HarnessValidationError) as wrong_attempt:
        store.append_result(
            replace(result, worker_result_proof_ref=wrong_attempt_ref)
        )
    assert wrong_attempt.value.code == "task_plan_result_proof_mismatch"

    succeeded_worker = HarnessWorkerResult(
        status=HarnessWorkerStatus.SUCCEEDED,
        metrics={"turns": 1},
    )
    succeeded_input_checksum = canonical_payload_checksum(
        {
            "instance": instance.checksum_projection(),
            "worker_result": succeeded_worker.candidate_payload(),
        }
    )
    succeeded_evidence = TaskPlanGateEvidence(
        gate_ref=plan.tasks[0].gate_refs[0],
        input_checksum=succeeded_input_checksum,
        result_checksum=canonical_payload_checksum(
            succeeded_worker.candidate_payload()
        ),
        passed=True,
    )
    succeeded_refs = owner.persist_gate_verification(
        plan,
        instance,
        succeeded_worker,
        (succeeded_evidence,),
    )
    with pytest.raises(HarnessValidationError) as wrong_status:
        store.append_result(
            replace(
                result,
                worker_result_proof_ref=succeeded_refs.input_checksum,
            )
        )
    assert wrong_status.value.code == "task_plan_result_proof_mismatch"


def test_worker_failure_artifact_is_scoped_content_addressed_and_reopens(
    tmp_path,
):
    database = tmp_path / "worker-proof.sqlite3"
    artifact_root = tmp_path / "artifacts"
    events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    store = DurableTaskPlanStore(
        _runtime(events),
        events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    result, worker_result = _worker_failure_result(
        plan,
        instance,
        store,
        metrics={"turns": 1},
    )
    assert result.worker_result_proof_ref is not None

    wrong_tenant = DurableTaskPlanStore(
        _runtime(events),
        events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-b",
        clock=lambda: FIXED_NOW,
    )
    with pytest.raises(HarnessValidationError) as wrong_scope:
        wrong_tenant.read_worker_result_input(
            plan.run_id,
            plan.stage_id,
            result.worker_result_proof_ref,
        )
    assert wrong_scope.value.code == "task_plan_gate_artifact_scope_mismatch"

    input_path = next(
        path
        for path in (artifact_root / plan.run_id).rglob("*.json")
        if "gate_input" in path.parts
    )
    original = input_path.read_bytes()
    input_path.unlink()
    with pytest.raises(HarnessValidationError) as missing_artifact:
        store.append_result(result)
    assert missing_artifact.value.code == "task_plan_artifact_missing"
    input_path.write_bytes(original)

    polluted = json.loads(original.decode("utf-8"))
    polluted["scope"]["tenant_id"] = "tenant-b"
    input_path.write_text(
        json.dumps(
            polluted,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(HarnessValidationError) as cas_conflict:
        store.persist_worker_result_input(plan, instance, worker_result)
    assert cas_conflict.value.code == "task_plan_artifact_checksum_mismatch"
    input_path.write_bytes(original)

    assert store.append_result(result) == result.result_checksum
    committed_events = store.read_events(plan.run_id, plan.stage_id)
    reopened_events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    reopened = DurableTaskPlanStore(
        _runtime(reopened_events),
        reopened_events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    restored = reopened.read_worker_result_input(
        plan.run_id,
        plan.stage_id,
        result.worker_result_proof_ref,
    )
    assert restored.instance == instance
    assert restored.worker_result == worker_result
    history = reopened.result_history_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    )
    assert len(history) == 1
    assert history[0].result == result
    replay = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (plan,),
        committed_events,
        results=(result,),
    )
    assert replay.projection.tasks[0].status is TaskLifecycle.FAILED


def test_result_proof_repairs_result_only_sqlite_crash_without_reexecuting_gate(
    tmp_path,
):
    database = tmp_path / "e.db"
    artifact_root = tmp_path / "a"
    events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    store = DurableTaskPlanStore(
        _runtime(events),
        events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    gate_calls = 0
    worker_result = HarnessWorkerResult(
        status=HarnessWorkerStatus.SUCCEEDED,
        artifacts=("artifact://structure",),
    )
    gates = TaskPlanGateRegistry()

    def gate(_request):
        nonlocal gate_calls
        gate_calls += 1
        return True

    gates.register("SummaryGate@1", gate, deterministic=True)
    result = TaskPlanResultVerifier(
        gates,
        gate_artifact_writer=store,
    ).verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
        ),
    )
    assert gate_calls == 1
    before_events = store.read_events(plan.run_id, plan.stage_id)
    before_projection = store.load_projection(plan.run_id, plan.stage_id)
    before_files = tuple(sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*.json")))

    forged_usage = replace(result, usage={"turns": 1})
    with pytest.raises(HarnessValidationError) as forged:
        store.append_result(forged_usage)
    assert forged.value.code == "task_plan_result_proof_mismatch"
    assert store.read_events(plan.run_id, plan.stage_id) == before_events
    assert store.load_projection(plan.run_id, plan.stage_id) == before_projection
    assert tuple(sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*.json"))) == before_files

    crashing = DurableTaskPlanStore(
        _FailBatchOnceRuntime(_runtime(events)),
        events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    with pytest.raises(RuntimeError, match="result batch interruption"):
        crashing.append_result(result)
    assert crashing.read_events(plan.run_id, plan.stage_id) == before_events
    assert crashing.load_projection(plan.run_id, plan.stage_id) == before_projection
    assert any("result" in path.parts for path in artifact_root.rglob("*.json"))

    reopened_events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    reopened = DurableTaskPlanStore(
        _runtime(reopened_events),
        reopened_events,
        artifact_store=FilesystemArtifactStore(artifact_root),
        tenant_id="tenant-a",
        clock=lambda: FIXED_NOW,
    )
    recovery = TaskPlanRecoveryService(
        queue_reader=_TaskPlanQueueReader(),
        gate_evidence_reader=reopened,
    ).recover(
        (plan,),
        before_events,
        results=(result,),
    )
    assert recovery.pending_terminal_results == (result,)
    assert recovery.missing_queue_projections == ()
    assert gate_calls == 1

    assert reopened.append_result(result) == result.result_checksum
    committed_events = reopened.read_events(plan.run_id, plan.stage_id)
    assert len(committed_events) == len(before_events) + 2
    assert committed_events[-2].event_type == "TASK_RESULT_ACCEPTED"
    assert committed_events[-1].event_type == "TASK_COMPLETED"
    committed_files = tuple(sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*.json")))
    assert reopened.append_result(result) == result.result_checksum
    assert reopened.read_events(plan.run_id, plan.stage_id) == committed_events
    assert tuple(sorted(path.relative_to(artifact_root) for path in artifact_root.rglob("*.json"))) == committed_files
    assert gate_calls == 1


def test_in_memory_result_owner_requires_the_same_gate_proof_reader():
    candidate, plan = _graph_only_candidate_and_plan()
    gate_owner = InMemoryTaskPlanGateArtifactWriter()
    source = InMemoryTaskPlanStore(gate_evidence_reader=gate_owner)
    source.append_candidate(candidate)
    source.accept_plan(plan)
    instance = _start(source, plan, plan.tasks[0].task_id)
    worker_result = HarnessWorkerResult(status=HarnessWorkerStatus.SUCCEEDED)
    gates = TaskPlanGateRegistry()
    gates.register("SummaryGate@1", lambda _request: True, deterministic=True)
    result = TaskPlanResultVerifier(
        gates,
        gate_artifact_writer=gate_owner,
    ).verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
        ),
    )

    unbound = InMemoryTaskPlanStore()
    unbound.append_candidate(candidate)
    unbound.accept_plan(plan)
    _start(unbound, plan, plan.tasks[0].task_id)
    with pytest.raises(HarnessValidationError) as missing_reader:
        unbound.append_result(result)
    assert missing_reader.value.code == "task_plan_gate_evidence_reader_required"
    assert not {
        "TASK_RESULT_ACCEPTED",
        "TASK_COMPLETED",
    }.intersection(
        event.event_type for event in unbound.read_events(plan.run_id, plan.stage_id)
    )

    assert source.append_result(result) == result.result_checksum


def test_result_owner_rejects_forged_gate_failure_and_accepts_real_first_failure():
    artifacts = _ArtifactStore()
    store = _store(_EventStore(), artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    proofless = _result(
        plan,
        instance,
        status=TaskLifecycle.SUCCEEDED,
    )
    with pytest.raises(HarnessValidationError) as missing_proof:
        store.append_result(proofless)
    assert missing_proof.value.code == "task_plan_gate_proof_required"

    worker_result = HarnessWorkerResult(
        status=HarnessWorkerStatus.SUCCEEDED,
        artifacts=("artifact://structure",),
    )
    gates = TaskPlanGateRegistry()
    gates.register("SummaryGate@1", lambda _request: False, deterministic=True)
    result = TaskPlanResultVerifier(
        gates,
        gate_artifact_writer=store,
    ).verify(
        worker_result,
        task=plan.tasks[0],
        request=TaskPlanResultVerificationRequest(
            plan=plan,
            task=plan.tasks[0],
            instance=instance,
            worker_result=worker_result,
        ),
    )
    assert result.status is TaskLifecycle.FAILED
    assert result.error_code == "task_gate_failed"

    before_events = store.read_events(plan.run_id, plan.stage_id)
    before_artifacts = dict(artifacts._content)
    forged = replace(result, error_code="self_declared_failure")
    with pytest.raises(HarnessValidationError) as rejected:
        store.append_result(forged)
    assert rejected.value.code == "task_plan_result_proof_mismatch"
    assert store.read_events(plan.run_id, plan.stage_id) == before_events
    assert artifacts._content == before_artifacts

    assert store.append_result(result) == result.result_checksum
    events = store.read_events(plan.run_id, plan.stage_id)
    with pytest.raises(HarnessValidationError) as missing_reader:
        TaskPlanReplayReducer().replay(
            (plan,),
            events,
            results=(result,),
        )
    assert missing_reader.value.code == "task_plan_gate_evidence_reader_required"

    replay = TaskPlanReplayReducer(gate_evidence_reader=store).replay(
        (plan,),
        events,
        results=(result,),
    )
    assert replay.projection.tasks[0].status is TaskLifecycle.FAILED

    forged_result = replace(result, error_code="self_declared_failure")
    forged_events = (
        *events[:-2],
        _result_event(
            forged_result,
            "TASK_RESULT_REJECTED",
            events[-2].sequence,
            plan=plan,
        ),
        _terminal_result_event(
            forged_result,
            "TASK_FAILED",
            events[-1].sequence,
            plan=plan,
        ),
    )
    with pytest.raises(HarnessValidationError) as self_consistent_forgery:
        TaskPlanReplayReducer(gate_evidence_reader=store).replay(
            (plan,),
            forged_events,
            results=(forged_result,),
        )
    assert self_consistent_forgery.value.code == "task_plan_result_proof_mismatch"


def test_retired_task_plan_contracts_are_not_readable():
    with pytest.raises(HarnessValidationError) as error:
        DEFAULT_TASK_PLAN_SCHEMA_REGISTRY.require_readable(
            TaskPlanContractKind.PLAN_CANDIDATE,
            "newsroom.harness-task-plan-candidate/v1",
        )
    assert error.value.code == "unsupported_task_plan_schema"


def test_graph_only_candidate_and_plan_round_trip_through_durable_event_store():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()

    assert store.append_candidate(candidate) == candidate.candidate_checksum
    assert store.accept_plan(plan) == plan.plan_checksum

    reopened = _store(event_store, artifacts)
    assert reopened.plan(plan.run_id, plan.stage_id) == plan
    events = reopened.read_events(plan.run_id, plan.stage_id)
    assert [event.event_type for event in events] == [
        "PLAN_CANDIDATE_BUILT",
        "PLAN_ACCEPTED",
    ]
    assert all(
        event.schema_version == TASK_PLAN_EVENT_SCHEMA_V3 for event in events
    )
    assert events[0].matches_contract_identity(candidate)
    assert events[1].matches_contract_identity(plan)
    assert artifacts._content

    assert len(event_store._events) == 2
    for stored in event_store._events:
        assert stored.data_schema == TASK_PLAN_EVENT_SCHEMA_V3
        assert not hasattr(stored.business_context, "workflow_id")
        assert not hasattr(stored.business_context, "step_id")
        assert "workflow_id" not in (stored.payload or {})
        context = graph_event_context(stored)
        assert context.identity.run_id == plan.run_id
        assert context.identity.graph_id == plan.graph_id
        assert context.identity.graph_checksum == plan.graph_checksum

    stored = event_store._events[0]
    extensions = dict(stored.extensions)
    graph_context = dict(extensions[GRAPH_EVENT_CONTEXT_EXTENSION])
    graph_context["graph_id"] = "other.graph"
    extensions[GRAPH_EVENT_CONTEXT_EXTENSION] = graph_context
    event_store._events[0] = StoredEvent(
        candidate=replace(stored.candidate, extensions=extensions),
        observed_at=stored.observed_at,
        stream_sequence=stored.stream_sequence,
    )
    with pytest.raises(HarnessValidationError) as mismatch:
        reopened.read_events(plan.run_id, plan.stage_id)
    assert mismatch.value.code == "task_plan_event_identity_mismatch"


def test_planning_receipts_round_trip_through_canonical_event_store():
    from framework.harness.task_plan.planning_build import PlanBuildAttempt, validate_build_history
    from framework.harness.task_plan.store import _task_plan_event_identity_kwargs

    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    intent = PlanBuildAttempt(request_checksum=canonical_payload_checksum({"request": "planning"}),
                              policy_checksum=plan.policy_checksum, attempt=1, max_calls=1, timeout_ms=30000)
    identity = _task_plan_event_identity_kwargs(candidate)
    store.append_event(TaskPlanEvent("PLAN_BUILD_INTENT", **identity,
                                    payload={"build_attempt": intent.to_dict()}, sequence=1))
    store.append_candidate(candidate)
    receipt = replace(intent, status="SUCCEEDED", elapsed_ms=1, candidate_checksum=candidate.candidate_checksum)
    store.append_event(TaskPlanEvent("PLAN_BUILD_RECEIPT", **identity,
                                    payload={"build_attempt": receipt.to_dict()}, sequence=3))
    store.accept_plan(plan)
    reopened = _store(event_store, artifacts)
    events = reopened.read_events(plan.run_id, plan.stage_id)
    assert validate_build_history(events) == (receipt,)
    assert reopened.candidate_for(plan.run_id, plan.stage_id, receipt.candidate_checksum) == candidate
    projection = TaskPlanReplayReducer().reduce(plan, events, require_terminal_events=False)
    assert projection.last_sequence == 4


def test_graph_only_patch_is_bound_to_its_base_plan_and_replays_after_replan():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    legacy_candidate, legacy_plan, policy, registry = _accepted_plan(
        (
            _task("structure"),
            _task(
                "helper",
                capability="research.helper",
                role="analysis.helper",
            ),
        ),
        two_tasks=True,
    )
    graph = HarnessGraphCompiler().compile(
        build_dynamic_paper_analysis_graph_definition()
    ).graph
    identity = TaskPlanStageIdentity(
        run_id=legacy_candidate.run_id,
        stage_binding=TaskPlanStageBinding(graph, RESEARCH_DYNAMIC_STAGE_ID),
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=identity,
        candidate_id="graph-patch-candidate-1",
        input_context_refs=legacy_candidate.input_context_refs,
        tasks=legacy_candidate.tasks,
        required_output_roles=legacy_candidate.required_output_roles,
        generated_by=legacy_candidate.generated_by,
        requested_plan_budget=legacy_candidate.requested_plan_budget,
    )
    plan = ValidatedTaskPlan.from_candidate(
        candidate,
        plan_id="graph-patch-plan-1",
        version=1,
        parent_plan_id=None,
        source_candidate_ref=candidate.candidate_checksum,
        policy_ref=legacy_plan.policy_ref,
        policy_checksum=legacy_plan.policy_checksum,
        tasks=legacy_plan.tasks,
        required_output_roles=legacy_plan.required_output_roles,
        limits=legacy_plan.limits,
        accepted_at=legacy_plan.accepted_at,
    )
    store.append_candidate(candidate)
    store.accept_plan(plan)

    patch = PlanPatch.for_plan(
        plan,
        patch_id="graph-patch-1",
        reason_code="repair",
        source_candidate_ref="candidate://graph-patch-1",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.UPDATE_PENDING_DEPENDENCY,
                target_task_id="helper",
                depends_on=("structure",),
            ),
        ),
    )
    assert patch.schema_version == GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA
    assert "workflow_id" not in patch.to_dict()
    assert PlanPatch.from_dict(patch.to_dict()) == patch
    assert patch.matches_plan_identity(plan)

    projection = store.load_projection(plan.run_id, plan.stage_id)
    next_plan = TaskPlanPatchValidator().apply(
        plan,
        patch,
        projection,
        policy,
        registry,
        accepted_at="2026-08-02T00:01:00Z",
        available_input_refs=("document",),
    )
    assert next_plan.shares_stage_identity(plan)

    forged_plan_payload = next_plan.to_dict()
    forged_plan_payload["graph_id"] = "research.other.dynamic"
    forged_plan_payload["graph_ref"] = (
        f"research.other.dynamic@{next_plan.graph_version}"
    )
    forged_plan_payload["stage_identity_checksum"] = canonical_payload_checksum(
        {
            "schema_version": forged_plan_payload["stage_identity_schema"],
            "run_id": forged_plan_payload["run_id"],
            "graph_schema_version": forged_plan_payload["graph_schema_version"],
            "compiler_version": forged_plan_payload["compiler_version"],
            "condition_policy_version": forged_plan_payload[
                "condition_policy_version"
            ],
            "graph_id": forged_plan_payload["graph_id"],
            "graph_version": forged_plan_payload["graph_version"],
            "graph_checksum": forged_plan_payload["graph_checksum"],
            "stage_id": forged_plan_payload["stage_id"],
            "stage_binding_checksum": forged_plan_payload["stage_binding_checksum"],
            "graph_ref": forged_plan_payload["graph_ref"],
        }
    )
    forged_plan_payload["plan_checksum"] = canonical_payload_checksum(
        {
            key: value
            for key, value in forged_plan_payload.items()
            if key != "plan_checksum"
        }
    )
    forged_plan = ValidatedTaskPlan.from_dict(forged_plan_payload)
    memory_store = InMemoryTaskPlanStore()
    memory_store.append_candidate(candidate)
    memory_store.accept_plan(plan)
    for candidate_store in (store, memory_store):
        with pytest.raises(HarnessValidationError) as forged_plan_error:
            candidate_store.accept_patched_plan(patch, forged_plan)
        assert forged_plan_error.value.code == "task_plan_patch_scope_mismatch"

    assert store.accept_patched_plan(patch, next_plan) == next_plan.plan_checksum

    reopened = _store(event_store, artifacts)
    events = reopened.read_events(plan.run_id, plan.stage_id)
    replay = TaskPlanReplayReducer().replay(
        (plan, next_plan),
        events,
        patches=(patch,),
    )
    assert replay.projection.matches_plan_identity(next_plan)
    assert replay.projection.plan_version == 2
    assert [event.event_type for event in events[-2:]] == [
        "PLAN_PATCH_ACCEPTED",
        "PLAN_ACCEPTED",
    ]

    alias_payload = patch.to_dict()
    alias_payload["workflow_id"] = "legacy-workflow"
    with pytest.raises(HarnessValidationError) as alias_error:
        PlanPatch.from_dict(alias_payload)
    assert alias_error.value.code == "invalid_task_plan_payload_fields"

    cross_graph_payload = patch.to_dict()
    cross_graph_payload["graph_id"] = "research.other.dynamic"
    cross_graph_payload["graph_ref"] = (
        f"research.other.dynamic@{patch.graph_version}"
    )
    cross_graph_payload["stage_identity_checksum"] = canonical_payload_checksum(
        {
            "schema_version": cross_graph_payload["stage_identity_schema"],
            "run_id": cross_graph_payload["run_id"],
            "graph_schema_version": cross_graph_payload["graph_schema_version"],
            "compiler_version": cross_graph_payload["compiler_version"],
            "condition_policy_version": cross_graph_payload[
                "condition_policy_version"
            ],
            "graph_id": cross_graph_payload["graph_id"],
            "graph_version": cross_graph_payload["graph_version"],
            "graph_checksum": cross_graph_payload["graph_checksum"],
            "stage_id": cross_graph_payload["stage_id"],
            "stage_binding_checksum": cross_graph_payload["stage_binding_checksum"],
            "graph_ref": cross_graph_payload["graph_ref"],
        }
    )
    cross_graph_payload["patch_checksum"] = canonical_payload_checksum(
        {
            key: value
            for key, value in cross_graph_payload.items()
            if key != "patch_checksum"
        }
    )
    cross_graph_patch = PlanPatch.from_dict(cross_graph_payload)
    assert not cross_graph_patch.matches_plan_identity(plan)
    cross_graph_store = _store(_EventStore(), _ArtifactStore())
    cross_graph_store.append_candidate(candidate)
    cross_graph_store.accept_plan(plan)
    with pytest.raises(HarnessValidationError) as cross_graph_error:
        cross_graph_store.append_patch(cross_graph_patch)
    assert cross_graph_error.value.code == "task_plan_patch_scope_mismatch"


def test_graph_only_task_lifecycle_and_result_round_trip_through_durable_store():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    result = _result(
        plan,
        instance,
        status=TaskLifecycle.SUCCEEDED,
        gate_artifact_owner=store,
    )

    expected_instance_projection = {
        "schema_version": "newsroom.harness-task-instance/v3",
        "run_id": "durable-run",
        "graph_id": "research.paper_analysis.dynamic.graph",
        "graph_version": "1",
        "graph_ref": "research.paper_analysis.dynamic.graph@1",
        "graph_schema_version": "newsroom.harness-normalized-graph/v2",
        "compiler_version": "newsroom.harness-graph-compiler/v2",
        "condition_policy_version": (
            "newsroom.harness-graph-condition-policy/v1"
        ),
        "graph_checksum": (
            "sha256:b0c0a7a7e70c512199119fe227145531eb885fae331a97819e04ef3cef16741d"
        ),
        "stage_binding_checksum": (
            "sha256:02bf966fef56dbab04d2fe1b9f2fea33b5dee4f4675af468bc333764534fac97"
        ),
        "stage_identity_schema": (
            "newsroom.harness-task-plan-stage-identity/v2"
        ),
        "stage_identity_checksum": (
            "sha256:0b92442703c5b2cd67e13f5af1e3d51e31e1d3d71d06dd8db2c6d2024f96bd2d"
        ),
        "stage_id": "dynamic_analysis_stage",
        "plan_id": "graph-plan-1",
        "plan_version": 1,
        "plan_checksum": (
            "sha256:3561ba56abd01e55b59cabca911c069e11fc126a520e4638e1089c789e969619"
        ),
        "task_id": "structure",
        "task_definition_checksum": (
            "sha256:7b9dcd518c41e18f4ede3fb5f7fcf03a8d287b870d79dd16fa7121219e846eef"
        ),
        "task_instance_id": (
            "ti_32c3c023bfa2d3cfa265d2e055ae21cac781dfc93828b3b0b438c7950b8b2bc7"
        ),
        "attempt": 1,
        "worker_ref": "research.structure-worker@1",
        "idempotency_key": (
            "idem_32c3c023bfa2d3cfa265d2e055ae21cac781dfc93828b3b0b438c7950b8b2bc7"
        ),
        "fencing_token": (
            "fence_32c3c023bfa2d3cfa265d2e055ae21cac781dfc93828b3b0b438c7950b8b2bc7"
        ),
        "budget_snapshot": {
            "max_turns": 1,
            "max_tool_calls": 0,
            "max_memory_ops": 0,
            "max_output_tokens": 0,
        },
    }
    assert instance.checksum_projection() == expected_instance_projection
    assert instance.schema_version == GRAPH_ONLY_TASK_INSTANCE_SCHEMA
    assert canonical_payload_checksum(expected_instance_projection) == (
        "sha256:dd2238f34a23eff3b6303e3dc0b1012eb11eaea833cdcdb067e7da502145bf57"
    )
    assert instance.instance_checksum == (
        "sha256:dd2238f34a23eff3b6303e3dc0b1012eb11eaea833cdcdb067e7da502145bf57"
    )
    old_instance_identity = {
        **expected_instance_projection,
        "schema_version": "newsroom.harness-task-instance/v2",
    }
    assert canonical_payload_checksum(old_instance_identity) == (
        "sha256:337199602827337ffa6cb74d3bd8a549c3ac9422be2f53607ce272c8ab2a9dcf"
    )
    assert instance.matches_plan_identity(plan)
    assert "workflow_id" not in instance.to_dict()
    assert type(instance).from_dict(instance.to_dict()) == instance
    invalid_instance = instance.to_dict()
    invalid_instance["workflow_id"] = "legacy-workflow"
    with pytest.raises(HarnessValidationError) as instance_schema_error:
        type(instance).from_dict(invalid_instance)
    assert instance_schema_error.value.code == "invalid_task_plan_payload_fields"
    unknown_instance = instance.to_dict()
    unknown_instance["schema_version"] = "newsroom.harness-task-instance/v999"
    with pytest.raises(HarnessValidationError) as instance_version_error:
        type(instance).from_dict(unknown_instance)
    assert instance_version_error.value.code == "unsupported_task_plan_schema"
    queue_task = materialize_queue_task(instance)
    queue_projection = TaskPlanQueueProjection.from_task(queue_task)
    assert queue_task.payload == {}
    assert set(queue_task.metadata) == {TASK_PLAN_QUEUE_METADATA_KEY}
    expected_queue_projection = {
        "schema_version": "newsroom.harness-task-plan-queue-projection/v2",
        "queue_name": "framework:queue:default",
        "task_type": "harness_task_plan",
        "max_attempts": 1,
        "payload": {},
        "task_instance": {
            "schema_version": "newsroom.harness-task-instance/v3",
            "run_id": "durable-run",
            "graph_id": "research.paper_analysis.dynamic.graph",
            "graph_version": "1",
            "graph_ref": "research.paper_analysis.dynamic.graph@1",
            "graph_schema_version": "newsroom.harness-normalized-graph/v2",
            "compiler_version": "newsroom.harness-graph-compiler/v2",
            "condition_policy_version": (
                "newsroom.harness-graph-condition-policy/v1"
            ),
            "graph_checksum": (
                "sha256:b0c0a7a7e70c512199119fe227145531eb885fae331a97819e04ef3cef16741d"
            ),
            "stage_binding_checksum": (
                "sha256:02bf966fef56dbab04d2fe1b9f2fea33b5dee4f4675af468bc333764534fac97"
            ),
            "stage_identity_schema": (
                "newsroom.harness-task-plan-stage-identity/v2"
            ),
            "stage_identity_checksum": (
                "sha256:0b92442703c5b2cd67e13f5af1e3d51e31e1d3d71d06dd8db2c6d2024f96bd2d"
            ),
            "stage_id": "dynamic_analysis_stage",
            "plan_id": "graph-plan-1",
            "plan_version": 1,
            "plan_checksum": (
                "sha256:3561ba56abd01e55b59cabca911c069e11fc126a520e4638e1089c789e969619"
            ),
            "task_id": "structure",
            "task_definition_checksum": (
                "sha256:7b9dcd518c41e18f4ede3fb5f7fcf03a8d287b870d79dd16fa7121219e846eef"
            ),
            "task_instance_id": (
                "ti_32c3c023bfa2d3cfa265d2e055ae21cac781dfc93828b3b0b438c7950b8b2bc7"
            ),
            "attempt": 1,
            "worker_ref": "research.structure-worker@1",
            "idempotency_key": (
                "idem_32c3c023bfa2d3cfa265d2e055ae21cac781dfc93828b3b0b438c7950b8b2bc7"
            ),
            "attempt_fence_ref": (
                "fence_32c3c023bfa2d3cfa265d2e055ae21cac781dfc93828b3b0b438c7950b8b2bc7"
            ),
            "budget_snapshot": {
                "max_turns": 1,
                "max_tool_calls": 0,
                "max_memory_ops": 0,
                "max_output_units": 0,
            },
            "instance_checksum": (
                "sha256:dd2238f34a23eff3b6303e3dc0b1012eb11eaea833cdcdb067e7da502145bf57"
            ),
        },
    }
    assert queue_projection.checksum_projection() == expected_queue_projection
    assert queue_projection.schema_version == TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2
    assert canonical_payload_checksum(expected_queue_projection) == (
        "sha256:fbc35a97956e7eccbf8cb519bea433d2e70d59ea72294d1c984b22e1cbbeddd7"
    )
    assert queue_projection.projection_checksum == (
        "sha256:fbc35a97956e7eccbf8cb519bea433d2e70d59ea72294d1c984b22e1cbbeddd7"
    )
    assert queue_projection.to_dict() == {
        **expected_queue_projection,
        "projection_checksum": (
            "sha256:fbc35a97956e7eccbf8cb519bea433d2e70d59ea72294d1c984b22e1cbbeddd7"
        ),
    }
    assert queue_projection.task_instance == instance
    assert "workflow_id" not in queue_projection.to_dict()["task_instance"]

    assert store.append_result(result) == result.result_checksum
    reopened = _store(event_store, artifacts)
    projection = reopened.load_projection(plan.run_id, plan.stage_id)
    events = reopened.read_events(plan.run_id, plan.stage_id)

    assert projection.schema_version == GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA
    assert projection.matches_plan_identity(plan)
    assert projection.tasks[0].schema_version == GRAPH_ONLY_TASK_PROJECTION_SCHEMA
    assert projection.tasks[0].status is TaskLifecycle.SUCCEEDED
    invalid_projection = projection.to_dict()
    invalid_projection["workflow_id"] = "legacy-workflow"
    with pytest.raises(HarnessValidationError) as projection_schema_error:
        type(projection).from_dict(invalid_projection)
    assert projection_schema_error.value.code == "invalid_task_plan_payload_fields"
    unknown_projection = projection.to_dict()
    unknown_projection["schema_version"] = (
        "newsroom.harness-task-plan-projection/v999"
    )
    with pytest.raises(HarnessValidationError) as projection_version_error:
        type(projection).from_dict(unknown_projection)
    assert projection_version_error.value.code == "unsupported_task_plan_schema"
    with pytest.raises(HarnessValidationError) as nested_schema_error:
        replace(
            projection,
            tasks=(
                replace(
                    projection.tasks[0],
                        schema_version="newsroom.harness-task-projection/v1",
                ),
            ),
        )
    assert nested_schema_error.value.code == "unsupported_task_plan_schema"
    assert projection.last_sequence == len(events) == 8
    assert [event.event_type for event in events[-6:]] == [
        "TASK_READY",
        "TASK_QUEUE_ADMITTED",
        "TASK_DISPATCHED",
        "TASK_STARTED",
        "TASK_RESULT_ACCEPTED",
        "TASK_COMPLETED",
    ]
    assert all(event.schema_version == TASK_PLAN_EVENT_SCHEMA_V3 for event in events)
    assert all(event.matches_contract_identity(plan) for event in events)
    ready_event, admission_event = events[-6:-4]
    assert ready_event.task_instance_id is None
    assert ready_event.attempt is None
    readiness = LogicalTaskReadiness.from_dict(
        ready_event.payload["logical_readiness"]
    )
    assert readiness.logical_ready_order == (instance.task_id,)
    queue_admission = TaskQueueAdmissionEvidence.from_dict(
        admission_event.payload["queue_admission"]
    )
    assert queue_admission.task_instance == instance
    assert queue_admission.admission_owner is TaskAdmissionOwner.QUEUE

    retired_ready_event = ready_event.to_dict()
    retired_ready_event["schema_version"] = TASK_PLAN_EVENT_SCHEMA_V2
    with pytest.raises(HarnessValidationError) as retired_event_error:
        TaskPlanEvent.from_dict(retired_ready_event)
    assert retired_event_error.value.code == "unsupported_task_plan_event_schema"
    assert reopened.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == (result,)
    assert reopened.results_for(
        plan.run_id,
        plan.stage_id,
        "sha256:" + "0" * 64,
        plan.version,
    ) == ()
    assert reopened.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version + 99,
    ) == ()
    report = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (plan,),
        events,
        results=(result,),
    )
    assert report.projection == projection
    record = projection.consumed_budget["ledger"]["records"][instance.idempotency_key]
    assert record["instance"] == instance.to_dict()
    assert record["status"] == "SETTLED"
    assert record["result_checksum"] == result.result_checksum
    assert record["consumed"] == instance.budget_snapshot.to_dict()
    assert record["released"] == dict.fromkeys(instance.budget_snapshot.to_dict(), 0)
    assert record["reserved_revision"] == 1
    assert record["settled_revision"] == 2
    assert report.reducer_version == TASK_PLAN_REPLAY_REDUCER_VERSION_V4
    # Replay binds the complete accepted attempt and ledger receipt, including
    # the canonical readiness/admission split, to one deterministic checksum.
    wire_report = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
        (ValidatedTaskPlan.from_dict(plan.to_dict()),),
        tuple(TaskPlanEvent.from_dict(event.to_dict()) for event in events),
        results=(TaskResultRecord.from_dict(result.to_dict()),),
    )
    assert report.replay_checksum == wire_report.replay_checksum
    assert report.projection == wire_report.projection
    assert report.projection.projection_checksum == projection.projection_checksum
    assert report.projection.matches_plan_identity(plan)
    checkpoint = TaskPlanCheckpoint.from_replay(
        "graph-checkpoint-1",
        plan,
        report,
        created_at="2026-08-02T00:00:01Z",
    )
    checkpoint_payload = checkpoint.to_dict()
    assert checkpoint.schema_version == TASK_PLAN_CHECKPOINT_SCHEMA_V4
    assert checkpoint.reducer_version == TASK_PLAN_REPLAY_REDUCER_VERSION_V4
    assert checkpoint.budget_snapshot == projection.consumed_budget
    assert checkpoint.checkpoint_checksum.startswith("sha256:")
    assert checkpoint.graph_ref == plan.graph_ref
    assert "workflow_id" not in checkpoint_payload
    restored_checkpoint = TaskPlanCheckpoint.from_dict(checkpoint_payload)
    assert restored_checkpoint == checkpoint
    restored_checkpoint.verify_replay(report)

    with pytest.raises(HarnessValidationError) as retired_reducer_error:
        replace(report, reducer_version=TASK_PLAN_REPLAY_REDUCER_VERSION_V3)
    assert (
        retired_reducer_error.value.code
        == "unsupported_task_plan_replay_reducer"
    )

    retired_checkpoint = dict(checkpoint_payload)
    retired_checkpoint["schema_version"] = TASK_PLAN_CHECKPOINT_SCHEMA_V3
    with pytest.raises(HarnessValidationError) as retired_checkpoint_error:
        TaskPlanCheckpoint.from_dict(retired_checkpoint)
    assert (
        retired_checkpoint_error.value.code
        == "unsupported_task_plan_checkpoint_schema"
    )

    aliased_checkpoint = dict(checkpoint_payload)
    aliased_checkpoint["workflow_id"] = "legacy-workflow"
    with pytest.raises(HarnessValidationError) as checkpoint_alias_error:
        TaskPlanCheckpoint.from_dict(aliased_checkpoint)
    assert checkpoint_alias_error.value.code == "invalid_task_plan_payload_fields"

    unknown_checkpoint = dict(checkpoint_payload)
    unknown_checkpoint["schema_version"] = (
        "newsroom.harness-task-plan-checkpoint/v999"
    )
    with pytest.raises(HarnessValidationError) as checkpoint_version_error:
        TaskPlanCheckpoint.from_dict(unknown_checkpoint)
    assert (
        checkpoint_version_error.value.code
        == "unsupported_task_plan_checkpoint_schema"
    )

    cross_graph_checkpoint = dict(checkpoint_payload)
    cross_graph_checkpoint["graph_id"] = "research.other.dynamic"
    cross_graph_checkpoint["graph_ref"] = (
        f"research.other.dynamic@{checkpoint.graph_version}"
    )
    with pytest.raises(HarnessValidationError) as checkpoint_identity_error:
        TaskPlanCheckpoint.from_dict(cross_graph_checkpoint)
    assert (
        checkpoint_identity_error.value.code
        == "task_plan_checkpoint_identity_mismatch"
    )

    with pytest.raises(HarnessValidationError) as missing_terminal_error:
        TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
            (plan,),
            events[:-1],
            results=(result,),
        )
    assert (
        missing_terminal_error.value.code
        == "task_plan_replay_terminal_event_missing"
    )
    pending_projection = TaskPlanReplayReducer(
        gate_evidence_reader=reopened,
    ).reduce(
        plan,
        events[:-1],
        results=(result,),
        require_terminal_events=False,
    )
    assert pending_projection.tasks[0].status is TaskLifecycle.RUNNING
    assert pending_projection.tasks[0].result is None
    pending_report = TaskPlanReplayReducer(gate_evidence_reader=store).replay(
        (plan,),
        events[:-1],
        results=(result,),
        require_terminal_events=False,
        apply_unterminated_results=False,
    )
    pending_checkpoint = TaskPlanCheckpoint.from_replay(
        "graph-checkpoint-pending-result",
        plan,
        pending_report,
        created_at="2026-08-02T00:00:02Z",
    )
    assert pending_checkpoint.active_task_instances == (instance,)
    assert pending_checkpoint.pending_terminal_results == (result,)
    assert TaskPlanCheckpoint.from_dict(pending_checkpoint.to_dict()) == (
        pending_checkpoint
    )
    with pytest.raises(HarnessValidationError) as inferred_terminal_error:
        TaskPlanReplayReducer(gate_evidence_reader=reopened).replay(
            (plan,),
            events[:-1],
            results=(result,),
            require_terminal_events=False,
            apply_unterminated_results=True,
        )
    assert (
        inferred_terminal_error.value.code
        == "task_plan_replay_terminal_event_missing"
    )
    assert any("/result/" in path for _, path in artifacts._content)
    assert all(
        not hasattr(stored.business_context, "workflow_id")
        and not hasattr(stored.business_context, "step_id")
        for stored in event_store._events
    )


def test_sqlite_result_owner_rejects_tampering_before_any_durable_write(
    tmp_path,
) -> None:
    database = tmp_path / "task-result-owner.sqlite3"
    artifacts = _ArtifactStore()
    event_store = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    store = _store(event_store, artifacts, runtime=_runtime(event_store))
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    result = _result(
        plan,
        instance,
        status=TaskLifecycle.SUCCEEDED,
        gate_artifact_owner=store,
    )

    before_events = store.read_events(plan.run_id, plan.stage_id)
    before_projection = store.load_projection(plan.run_id, plan.stage_id)
    before_results = store.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    )
    before_artifacts = dict(artifacts._content)
    stream_id = f"run:{plan.run_id}"
    before_watermark = event_store.get_stream_high_watermark(stream_id)

    wrong_binding = replace(result, worker_ref="forged-worker@1")
    with pytest.raises(HarnessValidationError) as binding_error:
        store.append_result(wrong_binding)
    assert binding_error.value.code == "task_plan_wrong_binding"
    assert store.read_events(plan.run_id, plan.stage_id) == before_events
    assert store.load_projection(plan.run_id, plan.stage_id) == before_projection
    assert artifacts._content == before_artifacts
    assert event_store.get_stream_high_watermark(stream_id) == before_watermark

    forged = replace(result)
    object.__setattr__(forged, "result_checksum", "sha256:" + "f" * 64)
    with pytest.raises(HarnessValidationError) as forged_error:
        store.append_result(forged)
    assert forged_error.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"
    assert store.read_events(plan.run_id, plan.stage_id) == before_events
    assert store.load_projection(plan.run_id, plan.stage_id) == before_projection
    assert store.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == before_results
    assert artifacts._content == before_artifacts
    assert event_store.get_stream_high_watermark(stream_id) == before_watermark

    reopened_events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    reopened = _store(
        reopened_events,
        artifacts,
        runtime=_runtime(reopened_events),
    )
    assert reopened.plan(plan.run_id, plan.stage_id) == plan
    assert reopened.load_projection(plan.run_id, plan.stage_id) == before_projection
    assert reopened.read_events(plan.run_id, plan.stage_id) == before_events
    assert reopened.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == before_results

    assert reopened.append_result(result) == result.result_checksum
    reopened_again_events = SQLiteEventStore(database, clock=lambda: FIXED_NOW)
    reopened_again = _store(
        reopened_again_events,
        artifacts,
        runtime=_runtime(reopened_again_events),
    )
    assert reopened_again.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == (result,)
    committed_events = reopened_again.read_events(plan.run_id, plan.stage_id)
    committed_projection = reopened_again.load_projection(plan.run_id, plan.stage_id)
    committed_artifacts = dict(artifacts._content)
    committed_watermark = reopened_again_events.get_stream_high_watermark(stream_id)

    assert reopened_again.append_result(result) == result.result_checksum
    assert reopened_again.read_events(plan.run_id, plan.stage_id) == committed_events
    assert reopened_again.load_projection(plan.run_id, plan.stage_id) == committed_projection
    assert artifacts._content == committed_artifacts
    assert reopened_again_events.get_stream_high_watermark(stream_id) == committed_watermark

    conflicting_duplicate = replace(
        result,
        result_ref="result://conflicting-duplicate",
    )
    with pytest.raises(HarnessValidationError) as conflict_error:
        reopened_again.append_result(conflicting_duplicate)
    assert conflict_error.value.code == "task_plan_duplicate_result_conflict"
    assert reopened_again.read_events(plan.run_id, plan.stage_id) == committed_events
    assert reopened_again.load_projection(plan.run_id, plan.stage_id) == committed_projection
    assert artifacts._content == committed_artifacts
    assert reopened_again_events.get_stream_high_watermark(stream_id) == committed_watermark

    forged_duplicate = replace(result)
    object.__setattr__(
        forged_duplicate,
        "result_ref",
        "result://forged-duplicate",
    )
    with pytest.raises(HarnessValidationError) as duplicate_error:
        reopened_again.append_result(forged_duplicate)
    assert duplicate_error.value.code == "RUNTIME_CONTRACT_CHECKSUM_MISMATCH"
    assert reopened_again.read_events(plan.run_id, plan.stage_id) == committed_events
    assert reopened_again.load_projection(plan.run_id, plan.stage_id) == committed_projection
    assert reopened_again.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == (result,)
    assert artifacts._content == committed_artifacts
    assert reopened_again_events.get_stream_high_watermark(stream_id) == committed_watermark


def test_graph_only_recovery_continues_each_recorded_lifecycle_without_io():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    accepted_events = store.read_events(plan.run_id, plan.stage_id)
    instance = _start(store, plan, plan.tasks[0].task_id)
    running_events = store.read_events(plan.run_id, plan.stage_id)
    result = _result(
        plan,
        instance,
        status=TaskLifecycle.SUCCEEDED,
        gate_artifact_owner=store,
    )
    store.append_result(result)
    terminal_events = store.read_events(plan.run_id, plan.stage_id)
    pending_result_events = terminal_events[:-1]
    pending_report = TaskPlanReplayReducer(gate_evidence_reader=store).replay(
        (plan,),
        pending_result_events,
        results=(result,),
        require_terminal_events=False,
        apply_unterminated_results=False,
    )
    pending_checkpoint = TaskPlanCheckpoint.from_replay(
        "graph-recovery-pending-result",
        plan,
        pending_report,
        created_at="2026-08-02T00:00:03Z",
    )
    terminal_report = TaskPlanReplayReducer(gate_evidence_reader=store).replay(
        (plan,),
        terminal_events,
        results=(result,),
    )
    terminal_checkpoint = TaskPlanCheckpoint.from_replay(
        "graph-recovery-terminal",
        plan,
        terminal_report,
        created_at="2026-08-02T00:00:04Z",
    )
    queue_reader = _TaskPlanQueueReader()
    service = TaskPlanRecoveryService(
        queue_reader=queue_reader,
        gate_evidence_reader=store,
    )

    pending = service.recover((plan,), accepted_events)
    assert pending.missing_queue_projections == ()
    assert pending.confirmed_queue_readbacks == ()
    assert pending.reclaim_continuations == ()
    assert pending.awaiting_reclaim == ()

    logical_ready = service.recover((plan,), running_events[:3])
    assert logical_ready.missing_queue_projections == ()
    assert logical_ready.confirmed_queue_readbacks == ()
    assert logical_ready.reclaim_continuations == ()
    assert logical_ready.awaiting_reclaim == ()
    assert queue_reader.calls == []

    admitted = service.recover((plan,), running_events[:4])
    assert queue_reader.calls[-1] == (
        "framework:queue:default",
        (instance.task_instance_id,),
    )
    assert len(admitted.missing_queue_projections) == 1
    ready_task = admitted.missing_queue_projections[0]
    ready_projection = TaskPlanQueueProjection.from_task(ready_task)
    assert ready_projection.task_instance == instance
    assert ready_projection.queue_name == "framework:queue:default"
    assert admitted.confirmed_queue_readbacks == ()
    assert admitted.reclaim_continuations == ()
    assert admitted.awaiting_reclaim == ()

    ready_task.status = WorkerTaskStatus.QUEUED
    readback = TaskPlanQueueReadback.from_queue_task("1700000000000-0", ready_task)
    restored_readback = TaskPlanQueueReadback.from_dict(readback.to_dict())
    queue_reader.readbacks = (restored_readback,)
    already_queued = service.recover(
        (plan,),
        running_events[:4],
    )
    assert already_queued.missing_queue_projections == ()
    assert already_queued.confirmed_queue_readbacks == (restored_readback,)
    assert already_queued.reclaim_continuations == ()
    assert already_queued.awaiting_reclaim == ()

    queue_reader.readbacks = ()
    dispatched = service.recover((plan,), running_events[:5])
    assert dispatched.missing_queue_projections == ()
    assert dispatched.confirmed_queue_readbacks == ()
    assert dispatched.awaiting_reclaim == (instance,)
    assert len(dispatched.reclaim_continuations) == 1
    continuation = dispatched.reclaim_continuations[0]
    assert continuation.schema_version == TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2
    assert continuation.task_instance == instance
    assert continuation.queue_name == "framework:queue:default"
    assert continuation.continuation_checksum == (
        "sha256:c91945b7cf14a023979e0e46564136ffdced37c3b74b47453e9cdba6c106e322"
    )
    assert (
        TaskPlanQueueReclaimContinuation.from_dict(continuation.to_dict())
        == continuation
    )
    unauthorized_reclaim = continuation.to_dict()
    unauthorized_reclaim["continuation_type"] = "reclaim_now"
    with pytest.raises(HarnessValidationError) as reclaim_action_error:
        TaskPlanQueueReclaimContinuation.from_dict(unauthorized_reclaim)
    assert reclaim_action_error.value.code == "task_plan_reclaim_action_mismatch"

    running = service.recover((plan,), running_events)
    assert running.missing_queue_projections == ()
    assert running.confirmed_queue_readbacks == ()
    assert running.awaiting_reclaim == (instance,)
    assert running.reclaim_continuations == (continuation,)

    pending_result = service.recover(
        (plan,),
        pending_result_events,
        results=(result,),
        checkpoint=pending_checkpoint,
    )
    assert pending_result.checkpoint_verified is True
    assert pending_result.pending_terminal_results == (result,)
    assert pending_result.missing_queue_projections == ()
    assert pending_result.confirmed_queue_readbacks == ()
    assert pending_result.reclaim_continuations == ()
    assert pending_result.awaiting_reclaim == ()

    terminal = service.recover(
        (plan,),
        terminal_events,
        results=(result,),
        checkpoint=terminal_checkpoint,
    )
    assert terminal.checkpoint_verified is True
    assert terminal.pending_terminal_results == ()
    assert terminal.missing_queue_projections == ()
    assert terminal.confirmed_queue_readbacks == ()
    assert terminal.reclaim_continuations == ()
    assert terminal.awaiting_reclaim == ()


def test_graph_only_recovery_requires_exact_queue_readback_identity():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    admitted_events = store.read_events(plan.run_id, plan.stage_id)[:4]
    queue_task = materialize_queue_task(instance)
    queue_task.status = WorkerTaskStatus.QUEUED
    readback = TaskPlanQueueReadback.from_queue_task("1700000000000-0", queue_task)

    with pytest.raises(HarnessValidationError) as missing_port_error:
        TaskPlanRecoveryService().recover((plan,), admitted_events)
    assert (
        missing_port_error.value.code
        == "graph_task_plan_queue_read_port_unavailable"
    )

    with pytest.raises(HarnessValidationError) as bare_id_error:
        TaskPlanRecoveryService(queue_reader=_TaskPlanQueueReader()).recover(
            (plan,),
            admitted_events,
            queued_instance_ids=(instance.task_instance_id,),
        )
    assert bare_id_error.value.code == "graph_task_plan_queue_readback_required"

    _, other_plan = _graph_only_candidate_and_plan(
        run_id="other-durable-run",
        graph_id="research.other.dynamic",
    )
    other_instance = task_instance_for_attempt(
        other_plan,
        other_plan.tasks[0].task_id,
        1,
    )
    other_task = materialize_queue_task(other_instance)
    other_task.status = WorkerTaskStatus.QUEUED
    cross_graph_readback = TaskPlanQueueReadback.from_queue_task(
        "1700000000001-0",
        other_task,
    )

    with pytest.raises(HarnessValidationError) as cross_graph_error:
        TaskPlanRecoveryService(
            queue_reader=_TaskPlanQueueReader((cross_graph_readback,))
        ).recover(
            (plan,),
            admitted_events,
        )
    assert (
        cross_graph_error.value.code
        == "task_plan_queue_readback_identity_mismatch"
    )

    with pytest.raises(HarnessValidationError) as duplicate_error:
        TaskPlanRecoveryService(
            queue_reader=_TaskPlanQueueReader((readback, readback))
        ).recover(
            (plan,),
            admitted_events,
        )
    assert duplicate_error.value.code == "task_plan_queue_readback_conflict"

    with pytest.raises(HarnessValidationError) as queue_error:
        TaskPlanRecoveryService(
            queue_reader=_TaskPlanQueueReader((readback,))
        ).recover(
            (plan,),
            admitted_events,
            queue_name="framework:queue:other",
        )
    assert queue_error.value.code == "task_plan_queue_readback_identity_mismatch"

    aliased = readback.to_dict()
    aliased["projection"]["task_instance"]["workflow_id"] = "legacy-workflow"
    with pytest.raises(HarnessValidationError) as alias_error:
        TaskPlanRecoveryService(
            queue_reader=_TaskPlanQueueReader((aliased,))
        ).recover(
            (plan,),
            admitted_events,
        )
    assert alias_error.value.code == "invalid_task_plan_payload_fields"

    unknown_schema = readback.to_dict()
    unknown_schema["projection"]["schema_version"] = (
        "newsroom.harness-task-plan-queue-projection/v999"
    )
    with pytest.raises(HarnessValidationError) as schema_error:
        TaskPlanQueueReadback.from_dict(unknown_schema)
    assert (
        schema_error.value.code
        == "unsupported_task_plan_queue_projection_schema"
    )


def test_live_task_plan_queue_has_no_legacy_workflow_identity_argument():
    _, plan = _graph_only_candidate_and_plan()
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)

    with pytest.raises(TypeError, match="workflow_id"):
        materialize_queue_task(instance, workflow_id="legacy-workflow")


def test_graph_only_queue_projection_survives_redis_transport_readback():
    class _CaptureRedis:
        def __init__(self):
            self.entries = []

        def xadd(self, queue_name, fields):
            self.entries.append((queue_name, fields))
            return b"1700000000000-0"

    _, plan = _graph_only_candidate_and_plan()
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    queue_task = materialize_queue_task(instance)
    redis = _CaptureRedis()

    message_id = RedisStreamTaskQueue(redis).enqueue(queue_task)
    _, fields = redis.entries[0]
    durable_payload = json.loads(fields["task"])
    durable_task = WorkerTask.from_dict(durable_payload)
    readback = TaskPlanQueueReadback.from_queue_task(message_id.decode(), durable_task)

    assert readback.schema_version == TASK_PLAN_QUEUE_READBACK_SCHEMA_V2
    assert readback.readback_checksum == (
        "sha256:fb668702cb7491d2e21584df70bb39508326ec49a74c1d7b31bc8b28421a1b65"
    )
    assert readback.projection.task_instance == instance
    assert TaskPlanQueueReadback.from_dict(readback.to_dict()) == readback
    serialized_metadata = json.dumps(
        durable_payload["metadata"],
        ensure_ascii=True,
        sort_keys=True,
    )
    assert "fencing_token" not in serialized_metadata
    assert "max_output_tokens" not in serialized_metadata
    assert "attempt_fence_ref" in serialized_metadata
    assert "max_output_units" in serialized_metadata

    tampered_task = WorkerTask.from_dict(durable_payload)
    tampered_task.payload = {"worker_may_activate": True}
    with pytest.raises(HarnessValidationError) as payload_error:
        TaskPlanQueueReadback.from_queue_task(
            message_id.decode(),
            tampered_task,
        )
    assert payload_error.value.code == "task_plan_queue_transport_mismatch"

    tampered_projection = readback.to_dict()
    tampered_projection["projection"]["task_instance"][
        "attempt_fence_ref"
    ] = "fence_tampered"
    with pytest.raises(HarnessValidationError):
        TaskPlanQueueReadback.from_dict(tampered_projection)


def test_graph_only_redis_queue_reader_proves_undelivered_records_atomically():
    class _CaptureRedis:
        def __init__(self):
            self.entries = []

        def xadd(self, queue_name, fields):
            self.entries.append((queue_name, fields))
            return b"1700000000000-0"

    class _AtomicReadRedis:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def eval(self, *args):
            self.calls.append(args)
            return self.response

    _, plan = _graph_only_candidate_and_plan()
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    capture = _CaptureRedis()
    message_id = RedisStreamTaskQueue(capture).enqueue(
        materialize_queue_task(instance)
    )
    queue_name, fields = capture.entries[0]
    record = [
        message_id,
        [b"task", fields["task"].encode("utf-8")],
    ]
    redis = _AtomicReadRedis(
        [
            b"ok",
            b"1",
            b"0-0",
            b"1",
            [record],
            b"0",
            [],
            b"0",
            [],
        ]
    )
    adapter = RedisTaskPlanQueueReadAdapter(redis, max_scan=10)

    readbacks = adapter.read_task_plan_queue(
        queue_name=queue_name,
        task_instance_ids=(instance.task_instance_id,),
    )

    assert len(readbacks) == 1
    assert readbacks[0].message_id == message_id.decode()
    assert readbacks[0].projection.task_instance == instance
    script, key_count, called_queue, group_name, scan_limit = redis.calls[0]
    assert key_count == 1
    assert called_queue == queue_name
    assert group_name == "framework-workers"
    assert scan_limit == 11
    assert all(command in script for command in ("XINFO", "XPENDING", "XRANGE"))
    assert all(command not in script for command in ("XADD", "XACK", "XCLAIM"))


@pytest.mark.parametrize("delivery_state", ("pending", "acknowledged"))
def test_graph_only_redis_queue_reader_rejects_delivered_ready_attempts(
    delivery_state,
):
    class _AtomicReadRedis:
        def __init__(self, response):
            self.response = response

        def eval(self, *args):
            return self.response

    _, plan = _graph_only_candidate_and_plan()
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    task = materialize_queue_task(instance)
    task.status = WorkerTaskStatus.QUEUED
    task_payload = json.dumps(task.to_dict(), ensure_ascii=False, sort_keys=True)
    record = [b"1700000000000-0", [b"task", task_payload.encode("utf-8")]]
    pending_records = [record] if delivery_state == "pending" else []
    response = [
        b"ok",
        b"1",
        b"1700000000000-0",
        b"0",
        [],
        str(len(pending_records)).encode(),
        pending_records,
        b"1",
        [record],
    ]
    adapter = RedisTaskPlanQueueReadAdapter(_AtomicReadRedis(response))

    with pytest.raises(HarnessValidationError) as error:
        adapter.read_task_plan_queue(
            queue_name=task.queue_name,
            task_instance_ids=(instance.task_instance_id,),
        )

    assert error.value.code == "task_plan_queue_delivery_state_mismatch"


def test_graph_only_redis_queue_reader_fails_closed_when_scan_is_incomplete():
    class _AtomicReadRedis:
        def eval(self, *args):
            return [b"ok", b"1", b"2-0", b"0", [], b"0", [], b"2", []]

    _, plan = _graph_only_candidate_and_plan()
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    adapter = RedisTaskPlanQueueReadAdapter(_AtomicReadRedis(), max_scan=1)

    with pytest.raises(HarnessValidationError) as error:
        adapter.read_task_plan_queue(
            queue_name="framework:queue:default",
            task_instance_ids=(instance.task_instance_id,),
        )

    assert error.value.code == "task_plan_queue_readback_scan_incomplete"


def test_graph_only_lifecycle_has_no_legacy_event_constructor_argument():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
    before_events = tuple(event_store._events)
    before_artifacts = dict(artifacts._content)

    with pytest.raises(TypeError, match="workflow_id"):
        TaskPlanEvent(
            "TASK_READY",
            run_id=plan.run_id,
            workflow_id="legacy-workflow",
            stage_id=plan.stage_id,
            graph_checksum=plan.graph_checksum,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            input_checksum=instance.task_definition_checksum,
            sequence=sequence,
        )

    assert tuple(event_store._events) == before_events
    assert artifacts._content == before_artifacts


def test_rejected_candidate_batch_is_atomic_when_second_event_fails():
    artifacts = _ArtifactStore()
    event_store = _EventStore(fail_on_event_type="PLAN_VALIDATION_FAILED")
    store = _store(event_store, artifacts)
    candidate, _, _, _ = _accepted_plan((_task("structure"),))

    with pytest.raises(RuntimeError, match="injected batch failure"):
        store.append_rejected_candidate(candidate, reason_code="invalid_candidate")

    assert event_store.get_stream_high_watermark("run:durable-run") is None
    assert store.read_events(candidate.run_id, candidate.stage_id) == ()


@pytest.mark.parametrize(
    ("status", "failed_event_type"),
    (
        (TaskLifecycle.SUCCEEDED, "TASK_COMPLETED"),
        (TaskLifecycle.FAILED, "TASK_FAILED"),
    ),
)
def test_result_document_and_terminal_events_are_atomic(
    status: TaskLifecycle,
    failed_event_type: str,
):
    artifacts = _ArtifactStore()
    event_store = _EventStore(fail_on_event_type=failed_event_type)
    store = _store(event_store, artifacts)
    candidate, plan, _, _ = _accepted_plan((_task("structure"),))
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, "structure")
    result = _result(
        plan,
        instance,
        status=status,
        gate_artifact_owner=store,
    )
    before_events = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(RuntimeError, match="injected batch failure"):
        store.append_result(result)

    # The result artifact and speculative projections are not authoritative
    # until both result and terminal events become visible together.
    assert store.read_events(plan.run_id, plan.stage_id) == before_events
    projection = store.load_projection(plan.run_id, plan.stage_id)
    assert projection.tasks[0].status is TaskLifecycle.RUNNING
    assert store.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    ) == ()

    event_store.fail_on_event_type = None
    assert store.append_result(result) == result.result_checksum
    event_types = [
        event.event_type for event in store.read_events(plan.run_id, plan.stage_id)
    ]
    assert event_types[-2:] == [
        "TASK_RESULT_ACCEPTED"
        if status is TaskLifecycle.SUCCEEDED
        else "TASK_RESULT_REJECTED",
        failed_event_type,
    ]


def test_durable_store_retries_a_concurrent_sequence_conflict_without_duplicate_event():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    delegate = _runtime(event_store)
    runtime = _ConflictOnceRuntime(delegate)
    store = _store(event_store, artifacts, runtime=runtime)
    candidate, _, _, _ = _accepted_plan((_task("structure"),))

    store.append_candidate(candidate)

    assert runtime.conflicts == 1
    events = store.read_events(candidate.run_id, candidate.stage_id)
    assert len(events) == 1
    assert events[0].event_type == "PLAN_CANDIDATE_BUILT"
    assert event_store.get_stream_high_watermark("run:durable-run") == 1


def test_patch_and_terminal_result_are_recoverable_from_event_and_artifact_refs():
    artifacts = _ArtifactStore()
    event_store = _EventStore()
    store = _store(event_store, artifacts)
    candidate, plan, policy, registry = _accepted_plan(
        (_task("structure"), _task("helper", capability="research.helper", role="analysis.helper")),
        two_tasks=True,
    )
    store.append_candidate(candidate)
    store.accept_plan(plan)

    structure_instance = _start(store, plan, "structure")
    structure_result = _result(
        plan,
        structure_instance,
        status=TaskLifecycle.SUCCEEDED,
        gate_artifact_owner=store,
    )
    store.append_result(structure_result)
    helper_instance = _start(store, plan, "helper")
    helper_failure = _result(
        plan,
        helper_instance,
        status=TaskLifecycle.FAILED,
        gate_artifact_owner=store,
    )
    store.append_result(helper_failure)

    patch = PlanPatch.for_plan(
        plan,
        patch_id="patch-1",
        reason_code="replacement",
        source_candidate_ref="candidate://replacement",
        operations=(
            PlanPatchOperation(
                PlanPatchOperationType.ADD_REPLACEMENT_TASK,
                target_task_id="helper",
                replacement_task=_task(
                    "helper-replacement",
                    capability="research.helper",
                    role="analysis.helper",
                ),
            ),
        ),
    )
    next_plan = TaskPlanPatchValidator().apply(
        plan,
        patch,
        store.load_projection(plan.run_id, plan.stage_id),
        policy,
        registry,
        accepted_at="2026-08-02T00:01:00Z",
        available_input_refs=("document",),
    )
    store.accept_patched_plan(patch, next_plan)

    reopened = _store(event_store, artifacts)
    assert reopened.plan(plan.run_id, plan.stage_id, 1) == plan
    assert reopened.plan(plan.run_id, plan.stage_id, 2) == next_plan
    recovered = reopened.load_projection(plan.run_id, plan.stage_id)
    assert next(item for item in recovered.tasks if item.task_id == "structure").status is TaskLifecycle.SUCCEEDED
    assert next(item for item in recovered.tasks if item.task_id == "helper").status is TaskLifecycle.SKIPPED
    assert next(item for item in recovered.tasks if item.task_id == "helper-replacement").status is TaskLifecycle.PENDING
    assert reopened.results_for(plan.run_id, plan.stage_id, next_plan.plan_id, 2) == (structure_result,)
    event_types = [event.event_type for event in reopened.read_events(plan.run_id, plan.stage_id)]
    assert "PLAN_PATCH_ACCEPTED" in event_types
    assert event_types[-1] == "TASK_REPLACED"
