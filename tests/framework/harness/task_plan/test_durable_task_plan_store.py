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
from framework.events.errors import EventStreamVersionConflictError
from framework.events.projection import (
    GRAPH_EVENT_CONTEXT_EXTENSION,
    graph_event_context,
)
from framework.events.runtime.models import (
    AppendResult,
    EventPage,
    StreamReadRequest,
)
from framework.events.runtime.publisher import EventPublishRequest, EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.checkpoint import TASK_PLAN_CHECKPOINT_SCHEMA_V3
from framework.harness.task_plan import (
    DEFAULT_TASK_PLAN_SCHEMA_REGISTRY,
    DurableTaskPlanStore,
    GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PATCH_SCHEMA,
    GRAPH_ONLY_TASK_PLAN_PROJECTION_SCHEMA,
    GRAPH_ONLY_TASK_PROJECTION_SCHEMA,
    InMemoryTaskPlanStore,
    TASK_PLAN_EVENT_SCHEMA_V2,
    TASK_PLAN_REPLAY_REDUCER_VERSION_V3,
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
    TaskPlanCheckpoint,
    TaskPlanContractKind,
    TaskPlanReadyDecision,
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
from framework.harness.task_plan.patches import TaskPlanPatchValidator
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.activity import HarnessWorkerType
from framework.workers.models.status import TaskStatus as WorkerTaskStatus
from framework.workers.models.task import Task as WorkerTask
from infrastructure.storage.workers.redis_queue import RedisStreamTaskQueue
from infrastructure.storage.workers.task_plan_queue import (
    RedisTaskPlanQueueReadAdapter,
)
from tests.fixtures.task_plan import build_task_plan_stage_binding


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


def _sha256(content: bytes) -> str:
    from hashlib import sha256

    return sha256(content).hexdigest()


class _UnitOfWork:
    def __init__(self, store: "_EventStore") -> None:
        self.store = store
        self.pending: list[StoredEvent] = []
        self.finished = False

    def __enter__(self) -> "_UnitOfWork":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if not self.finished:
            self.rollback()
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
        with self.store._lock:
            self.store._events.extend(self.pending)
        self.finished = True

    def rollback(self) -> None:
        self.pending.clear()
        self.finished = True


class _EventStore:
    """In-process canonical event store with the production stream contract."""

    def __init__(self, *, fail_on_event_type: str | None = None) -> None:
        self._events: list[StoredEvent] = []
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


def _runtime(store: _EventStore) -> EventRuntime:
    return EventRuntime(store=store, schema_catalog=default_event_schema_catalog(), monotonic=lambda: 1.0)


def _policy_and_registry(*, two_tasks: bool = False):
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
        per_task_budget=TaskBudget(max_turns=1),
        aggregate_task_budget=TaskBudget(max_turns=8),
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


def _accepted_plan(tasks: tuple[TaskSpec, ...], *, two_tasks: bool = False):
    policy, registry = _policy_and_registry(two_tasks=two_tasks)
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
    instance = task_instance_for_attempt(plan, task_id, 1)
    projection = store.load_projection(plan.run_id, plan.stage_id)
    decision = TaskPlanReadyDecision((instance,))
    projection = scheduler.reserve_ready_tasks(projection, decision)
    first_sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
    for sequence, event_type, transition in (
        (first_sequence, "TASK_READY", lambda value: value),
        (first_sequence + 1, "TASK_DISPATCHED", lambda value: scheduler.mark_dispatched(value, instance)),
        (first_sequence + 2, "TASK_STARTED", lambda value: scheduler.mark_started(value, instance)),
    ):
        projection = replace(transition(projection), last_sequence=sequence)
        store.commit_event(_lifecycle_event(event_type, sequence, plan, instance), projection)
    return instance


def _result(plan: ValidatedTaskPlan, instance, *, status: TaskLifecycle, role: str = "analysis.structure") -> TaskResultRecord:
    definition = next(item for item in plan.tasks if item.task_id == instance.task_id)
    if plan.is_graph_only:
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
        event.schema_version == TASK_PLAN_EVENT_SCHEMA_V2 for event in events
    )
    assert events[0].matches_contract_identity(candidate)
    assert events[1].matches_contract_identity(plan)
    assert artifacts._content

    assert len(event_store._events) == 2
    for stored in event_store._events:
        assert stored.data_schema == TASK_PLAN_EVENT_SCHEMA_V2
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
    result = _result(plan, instance, status=TaskLifecycle.SUCCEEDED)

    assert instance.schema_version == GRAPH_ONLY_TASK_INSTANCE_SCHEMA
    assert instance.instance_checksum == (
        "sha256:d88ed9b0ca23952947d1b058a4d5c214480df195721bf17f3812542a6d3698a5"
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
    assert queue_projection.schema_version == TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2
    assert queue_projection.projection_checksum == (
        "sha256:9f8c1638e2e8abb0f89a5ab7e7c9cb092110d38cb40fa45407054da13911bcbd"
    )
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
    assert projection.last_sequence == len(events) == 7
    assert [event.event_type for event in events[-5:]] == [
        "TASK_READY",
        "TASK_DISPATCHED",
        "TASK_STARTED",
        "TASK_RESULT_ACCEPTED",
        "TASK_COMPLETED",
    ]
    assert all(event.schema_version == TASK_PLAN_EVENT_SCHEMA_V2 for event in events)
    assert all(event.matches_contract_identity(plan) for event in events)
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
    report = TaskPlanReplayReducer().replay(
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
    assert report.reducer_version == TASK_PLAN_REPLAY_REDUCER_VERSION_V3
    assert report.replay_checksum == (
        "sha256:79656d0d41070d0bbd923a1d3b34732d1ba2a8f4eb71dbd67c4406038ec3866a"
    )
    assert report.projection.projection_checksum == projection.projection_checksum
    assert report.projection.matches_plan_identity(plan)
    checkpoint = TaskPlanCheckpoint.from_replay(
        "graph-checkpoint-1",
        plan,
        report,
        created_at="2026-08-02T00:00:01Z",
    )
    checkpoint_payload = checkpoint.to_dict()
    assert checkpoint.schema_version == TASK_PLAN_CHECKPOINT_SCHEMA_V3
    assert checkpoint.reducer_version == TASK_PLAN_REPLAY_REDUCER_VERSION_V3
    assert checkpoint.budget_snapshot == projection.consumed_budget
    assert checkpoint.checkpoint_checksum.startswith("sha256:")
    assert checkpoint.graph_ref == plan.graph_ref
    assert "workflow_id" not in checkpoint_payload
    restored_checkpoint = TaskPlanCheckpoint.from_dict(checkpoint_payload)
    assert restored_checkpoint == checkpoint
    restored_checkpoint.verify_replay(report)

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
        TaskPlanReplayReducer().replay(
            (plan,),
            events[:-1],
            results=(result,),
        )
    assert (
        missing_terminal_error.value.code
        == "task_plan_replay_terminal_event_missing"
    )
    pending_projection = TaskPlanReplayReducer().reduce(
        plan,
        events[:-1],
        results=(result,),
        require_terminal_events=False,
    )
    assert pending_projection.tasks[0].status is TaskLifecycle.RUNNING
    assert pending_projection.tasks[0].result is None
    pending_report = TaskPlanReplayReducer().replay(
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
        TaskPlanReplayReducer().replay(
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
    result = _result(plan, instance, status=TaskLifecycle.SUCCEEDED)
    store.append_result(result)
    terminal_events = store.read_events(plan.run_id, plan.stage_id)
    pending_result_events = terminal_events[:-1]
    pending_report = TaskPlanReplayReducer().replay(
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
    terminal_report = TaskPlanReplayReducer().replay(
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
    service = TaskPlanRecoveryService(queue_reader=queue_reader)

    pending = service.recover((plan,), accepted_events)
    assert pending.missing_queue_projections == ()
    assert pending.confirmed_queue_readbacks == ()
    assert pending.reclaim_continuations == ()
    assert pending.awaiting_reclaim == ()

    ready = service.recover((plan,), running_events[:3])
    assert queue_reader.calls[-1] == (
        "framework:queue:default",
        (instance.task_instance_id,),
    )
    assert len(ready.missing_queue_projections) == 1
    ready_task = ready.missing_queue_projections[0]
    ready_projection = TaskPlanQueueProjection.from_task(ready_task)
    assert ready_projection.task_instance == instance
    assert ready_projection.queue_name == "framework:queue:default"
    assert ready.confirmed_queue_readbacks == ()
    assert ready.reclaim_continuations == ()
    assert ready.awaiting_reclaim == ()

    ready_task.status = WorkerTaskStatus.QUEUED
    readback = TaskPlanQueueReadback.from_queue_task("1700000000000-0", ready_task)
    restored_readback = TaskPlanQueueReadback.from_dict(readback.to_dict())
    queue_reader.readbacks = (restored_readback,)
    already_queued = service.recover(
        (plan,),
        running_events[:3],
    )
    assert already_queued.missing_queue_projections == ()
    assert already_queued.confirmed_queue_readbacks == (restored_readback,)
    assert already_queued.reclaim_continuations == ()
    assert already_queued.awaiting_reclaim == ()

    queue_reader.readbacks = ()
    dispatched = service.recover((plan,), running_events[:4])
    assert dispatched.missing_queue_projections == ()
    assert dispatched.confirmed_queue_readbacks == ()
    assert dispatched.awaiting_reclaim == (instance,)
    assert len(dispatched.reclaim_continuations) == 1
    continuation = dispatched.reclaim_continuations[0]
    assert continuation.schema_version == TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2
    assert continuation.task_instance == instance
    assert continuation.queue_name == "framework:queue:default"
    assert continuation.continuation_checksum == (
        "sha256:5c15369b510d3cb62f8e5d3c6b861d3418db25736b32ce3695ba31ab016ebdf7"
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
    ready_events = store.read_events(plan.run_id, plan.stage_id)[:3]
    queue_task = materialize_queue_task(instance)
    queue_task.status = WorkerTaskStatus.QUEUED
    readback = TaskPlanQueueReadback.from_queue_task("1700000000000-0", queue_task)

    with pytest.raises(HarnessValidationError) as missing_port_error:
        TaskPlanRecoveryService().recover((plan,), ready_events)
    assert (
        missing_port_error.value.code
        == "graph_task_plan_queue_read_port_unavailable"
    )

    with pytest.raises(HarnessValidationError) as bare_id_error:
        TaskPlanRecoveryService(queue_reader=_TaskPlanQueueReader()).recover(
            (plan,),
            ready_events,
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
            ready_events,
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
            ready_events,
        )
    assert duplicate_error.value.code == "task_plan_queue_readback_conflict"

    with pytest.raises(HarnessValidationError) as queue_error:
        TaskPlanRecoveryService(
            queue_reader=_TaskPlanQueueReader((readback,))
        ).recover(
            (plan,),
            ready_events,
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
            ready_events,
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
        "sha256:9fecbee0bb65075ec82bfb7014ddd9765a39653dc6e6fafb186bc1cf11c75cf0"
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
    result = _result(plan, instance, status=status)
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
    structure_result = _result(plan, structure_instance, status=TaskLifecycle.SUCCEEDED)
    store.append_result(structure_result)
    helper_instance = _start(store, plan, "helper")
    helper_failure = _result(plan, helper_instance, status=TaskLifecycle.FAILED)
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
