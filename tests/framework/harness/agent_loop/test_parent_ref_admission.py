from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.agent.loop.runner import AgentRunner
from framework.agent.models import AgentLoopMetrics, AgentLoopPolicy, AgentSpec
from framework.events.runtime.publisher import EventRuntime
from framework.events.canonical import checksum_for
from framework.events.schema import default_event_schema_catalog
from framework.execution_environment.registry import ExecutionEnvironmentRegistry
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.agent_loop.child_executor import HarnessSubAgentTaskExecutor
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.compiler import HarnessGraphCompiler
from framework.harness.graph.definition import HarnessGraphDefinition, HarnessGraphTaskPlanStageBinding
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_results import HarnessResultRefAuthority
from framework.harness.ref_snapshot import RefAuthoritySnapshot, RefSnapshotPhase
from framework.harness.subagents.models import SubAgentSpec
from framework.harness.subagents.runtime import SubAgentRuntime, subagent_attempt_identity
from framework.harness.subagents.agent_runner import ChildAgentRunnerAdapter
from framework.harness.subagents.execution import (
    HarnessChildExecutionService,
    TrustedChildUsage,
    validate_trusted_execution_event,
)
from framework.harness.subagents.execution_providers import (
    AdmissionChildToolEvidenceLimitsProvider,
    CanonicalChildBudgetTrackerProvider,
    CanonicalChildExecutionUsageMeter,
)
from framework.harness.subagents.owned_runtime import HarnessOwnedChildAgentRuntime
from framework.harness.subagents.supervisor_store import DurableChildAgentEventLog
from framework.harness.subagents.transcript import (
    SubAgentAttemptIdentity,
    SubAgentTranscriptReceipt,
)
from framework.harness.task_plan.capability import TaskCapabilityRegistration, TaskCapabilityRegistry
from framework.harness.side_effects import (
    CountingHarnessSideEffectHandler, HarnessSideEffectDisposition,
    HarnessSideEffectHandlerBinding, HarnessSideEffectRegistry, InMemoryHarnessSideEffectStore,
)
from framework.harness.task_plan.checkpoint import JsonlTaskPlanCheckpointStore
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.harness.task_plan.submission import CandidateDedupIdentity
from framework.harness.task_plan.scheduler import task_instance_for_attempt
from framework.harness.task_plan.verification import TaskPlanGateRegistry, TaskPlanResultVerifier, TaskPlanResultVerificationRequest
from framework.harness.workers.result import HarnessWorkerResult
from framework.llm import FakeLLMClient
from framework.llm.budget import GlobalBudgetPolicy, GlobalBudgetTracker
from framework.memory.models import MemoryRecord
from framework.memory.namespace import MemoryNamespacePublisher
from framework.memory.policy import MemoryPolicy
from framework.tool import ToolPolicy, ToolRegistry
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.conversation import LocalJsonConversationStore
from infrastructure.storage.events.sqlite import SQLiteEventStore
from infrastructure.storage.harness import SQLiteHarnessNodeOutputResource
from infrastructure.storage.harness.subagent_transcript import FilesystemSubAgentTranscriptStore
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from interfaces.composition.agent_loop_graph import (
    build_agent_loop_graph_runtime_composition,
    build_agent_loop_harness_orchestration_runtime,
)
from tests.framework.harness.agent_loop.test_orchestration_runtime import _runtime, _request
from tests.framework.harness.test_ref_snapshot_store import _store
from tests.interfaces.composition.test_agent_loop_orchestration_composition import _CandidateBuilder
from tests.interfaces.services.test_agent_loop_graph_service import (
    ACTIVITY_REF, WORKER_REF, _agent, _durable_event_port, _runtime_run_spec,
)


class _RecordingAdmission(HarnessRefAdmissionService):
    def admit_graph_inputs(self, task, **kwargs):
        self.task = deepcopy(task)
        self.snapshot = super().admit_graph_inputs(task, **kwargs)
        return self.snapshot


class _RecordingChildRunnerAdapter(ChildAgentRunnerAdapter):
    """Use the real child AgentRunner while preserving fixture observations."""

    def __init__(
        self,
        *,
        worker,
        authority,
        role,
        worker_id,
        root,
        underreport_runner_metrics=False,
    ):
        super().__init__(
            registered_agent=AgentSpec(
                agent_id=worker_id,
                name=role,
                role=role,
                goal=f"Analyze {role}",
                instructions=f"Analyze {role}",
                output_key="summary",
                output_schema={
                    "required": ["summary"],
                    "properties": {"summary": {"type": "string"}},
                },
                allowed_tools=["tool.read"],
                memory_enabled=False,
                max_iterations=1,
            ),
            llm_client=FakeLLMClient(
                ['{"action_type":"final_output","output":{"summary":"completed"}}']
            ),
            tool_registry=ToolRegistry(),
            conversation_store=LocalJsonConversationStore(root),
            execution_environment=ExecutionEnvironmentRegistry(),
            require_explicit_execution_profile=False,
            worker_id=worker_id,
            worker_version="1",
        )
        self._fixture_worker = worker
        self._fixture_authority = authority
        self._underreport_runner_metrics = underreport_runner_metrics
        self.observed_results = []

    def invoke(self, invocation, *, parent_task_context, ref_admission_service,
               global_budget_tracker, tool_execution_evidence, input_reader=None):
        identity = subagent_attempt_identity(invocation)
        task = {
            "invocation": invocation.to_dict(),
            "context": invocation.context_envelope.to_dict(),
            "input_refs": list(invocation.input_refs),
            "budget": invocation.subagent_spec.budget,
        }
        dependency_outputs = self._fixture_authority.read_dependency_outputs(
            identity,
            input_refs=invocation.input_refs,
        )
        if dependency_outputs:
            task["dependency_outputs"] = dependency_outputs
        self._fixture_worker.execute(
            task,
            execution_identity=RefAuthoritySnapshot.execution_for_attempt(identity),
        )
        result = super().invoke(
            invocation,
            parent_task_context=parent_task_context,
            ref_admission_service=ref_admission_service,
            global_budget_tracker=global_budget_tracker,
            tool_execution_evidence=tool_execution_evidence,
            input_reader=input_reader,
        )
        if self._underreport_runner_metrics:
            result = replace(result, metrics=AgentLoopMetrics())
        self.observed_results.append(result)
        return result


def _setup(
    root,
    *,
    include_document=True,
    include_memory=False,
    dependency_ref=None,
    share_dependency=True,
    underreport_runner_metrics=False,
):
    template, template_identity = _runtime()
    template_policy = template._policy_registry.policies[0]
    policy = replace(
        template_policy,
        stage_id="run-agent-loop",
        max_planning_tool_calls=0,
        allowed_subagent_ids=("structure-worker", "contribution-worker"),
        shared_dependency_output_roles=("structure",) if dependency_ref is not None and share_dependency else (),
        per_task_budget=replace(
            template_policy.per_task_budget,
            max_memory_ops=max(1, template_policy.per_task_budget.max_memory_ops),
            max_output_tokens=max(template_policy.per_task_budget.token_limit, template_policy.per_task_budget.max_output_tokens),
        ),
        aggregate_task_budget=replace(
            template_policy.aggregate_task_budget,
            max_memory_ops=max(2, template_policy.aggregate_task_budget.max_memory_ops),
            max_output_tokens=max(template_policy.aggregate_task_budget.token_limit, template_policy.aggregate_task_budget.max_output_tokens),
        ),
    )
    spec = _runtime_run_spec("parent-ref-run", identity_scope_ref=checksum_for("production"))
    declaration = HarnessGraphTaskPlanStageBinding(
        activity_id=policy.stage_id, worker_ref=WORKER_REF, activity_ref=ACTIVITY_REF,
        policy_ref=policy.exact_ref, task_plan_schema=template._stage_binding.task_plan_schema,
        required_output_roles=policy.required_output_roles,
        support_refs=template._stage_binding.support_refs,
    )
    definition = replace(spec.graph, task_plan_stage_bindings=(declaration,), definition_checksum=None)
    if include_memory:
        definition = replace(definition, activities=tuple(
            replace(activity, metadata={**activity.metadata, "tool_allowlist": ["memory.recall"]})
            for activity in definition.activities
        ), definition_checksum=None)
    binding = TaskPlanStageBinding(HarnessGraphCompiler().compile(definition).graph, policy.stage_id)
    business_inputs = {"parent_private": "private-parent-only"}
    if include_document:
        business_inputs["document"] = {"text": "trusted document"}
    spec = replace(
        spec, graph=definition,
        inputs={**spec.inputs, "inputs": business_inputs, "tenant_scope_ref": checksum_for("production")},
        metadata={**spec.metadata, "tenant_scope_ref": checksum_for("production")},
    )
    grants, events = _store(root / "grants")
    namespace_store = FilesystemMemoryNamespaceStore(root / "memory-namespaces") if include_memory else None
    namespace_metadata = None
    if include_memory:
        namespace_metadata = MemoryNamespacePublisher(
            namespace_store, namespace="memory.read", tenant_id=checksum_for("production"),
            owner_id=checksum_for("production"), shared_read_only=False, policy=MemoryPolicy(),
        ).publish((MemoryRecord(
            content="admitted memory evidence note", memory_id="parent-note",
            namespace="memory.read", tenant_id=checksum_for("production"),
            actor=checksum_for("production"), refs={"source_id": "verified-source"},
        ),))
    admission = _RecordingAdmission(
        grants, memory_namespaces=namespace_store,
        memory_namespace_refs=() if namespace_metadata is None else (namespace_metadata.exact_ref,),
    )
    task_events = SQLiteEventStore(root / "tasks.sqlite3")
    task_store = DurableTaskPlanStore(
        EventRuntime(store=task_events, schema_catalog=default_event_schema_catalog()),
        task_events, artifact_store=FilesystemArtifactStore(root / "task-artifacts"),
    )
    child_calls = []
    child_inputs = []

    class Worker:
        worker_version = "1"
        worker_type = HarnessWorkerType.SUBAGENT

        def __init__(self, worker_id):
            self.worker_id = worker_id

        def execute(self, task, *, execution_identity):
            invocation = task["invocation"]
            child_calls.append((invocation, execution_identity))
            child_inputs.append(deepcopy(task))
            assert "private-parent-only" not in str(task)
            if dependency_ref is not None and self.worker_id == "contribution-worker":
                assert task["dependency_outputs"] == {dependency_ref: {"summary": "completed"}}
                assert all(ref.startswith("subagent-output://") for ref in invocation["input_refs"])
            else:
                assert invocation["input_refs"] == ["document"]
            return HarnessWorkerResult(status="succeeded", output={"summary": "completed"})

    transcripts = FilesystemSubAgentTranscriptStore(root / "transcripts")
    child_artifacts = FilesystemHarnessArtifactPort(root / "child-artifacts")
    authority = HarnessResultRefAuthority(
        grants,
        transcript_store=transcripts,
        artifact_descriptors=child_artifacts,
        tenant_id="production",
    )
    registrations = []
    workers = {}
    for role in ("structure", "contribution"):
        worker_id = f"{role}-worker"
        worker = Worker(worker_id)
        worker_implementation = _RecordingChildRunnerAdapter(
            worker=worker,
            authority=authority,
            role=role,
            worker_id=worker_id,
            root=root / "child-conversations" / role,
            underreport_runner_metrics=underreport_runner_metrics,
        )
        workers[worker_id] = worker_implementation
        registrations.append(TaskCapabilityRegistration(
            f"cap.{role}", HarnessWorkerBinding(
                HarnessContractReference(HarnessContractKind.WORKER, worker_id, "1"),
                HarnessWorkerType.SUBAGENT, worker_implementation,
            ), f"{role}-contract@1", "schema://input@1", "schema://result@1",
            subagent_spec=SubAgentSpec(
                subagent_id=worker_id, role=role, purpose=f"Analyze {role}",
                input_schema={"required": ["input_refs"]},
                output_schema={"required": ["summary"], "properties": {"summary": {"type": "string"}}},
                allowed_tools=policy.allowed_tool_ids, allowed_memory_namespaces=policy.allowed_memory_namespaces,
                budget={
                    "max_turns": policy.per_task_budget.max_turns,
                    "max_tool_calls": policy.per_task_budget.max_tool_calls,
                    "max_memory_ops": policy.per_task_budget.max_memory_ops,
                },
            ),
        ))
    capabilities = TaskCapabilityRegistry(registrations)
    subagents = SubAgentRuntime(workers=workers, transcript_store=transcripts, result_ref_authority=authority)
    canonical_tracker = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=10_000))
    budget_trackers = CanonicalChildBudgetTrackerProvider(canonical_tracker)
    execution_service = HarnessChildExecutionService(
        store=task_store,
        ref_admission_service=admission,
        state_runtime=task_store._runtime,
        state_reader=task_store._reader,
        artifact_store=task_store._artifact_store,
        budget_trackers=budget_trackers,
        leases=None,
        evidence_limits=AdmissionChildToolEvidenceLimitsProvider(),
        usage_meter=CanonicalChildExecutionUsageMeter(budget_trackers),
        input_reader=None,
    )
    executor = HarnessSubAgentTaskExecutor(
        store=task_store,
        runtime=subagents,
        ref_admission_service=admission,
        task_policy=policy,
        execution_service=execution_service,
    )
    gates = TaskPlanGateRegistry()
    gates.register("gate@1", lambda request: request.worker_result.output.get("summary") == "completed", deterministic=True)
    verifier = TaskPlanResultVerifier(gates, transcript_store=transcripts, artifact_reference_verifier=child_artifacts, result_ref_authority=authority)
    child_event_log = DurableChildAgentEventLog(
        state_runtime=task_store._runtime,
        state_reader=task_store._reader,
        state_key=f"agent-loop-parent-child:{root.resolve()}",
    )
    owned_child_runtime = HarnessOwnedChildAgentRuntime(
        event_log=child_event_log,
        max_children=max(1, policy.max_parallelism),
        renewal_interval_seconds=None,
    )
    owned_child_runtime.start()

    configured, _ = _runtime(
        policy=policy, stage_binding=binding, store=task_store,
    )
    runtime = build_agent_loop_harness_orchestration_runtime(
        stage_binding=binding, policy_registry=configured._policy_registry,
        capability_registry=capabilities, store=task_store,
        child_supervisor=owned_child_runtime.supervisor, candidate_builder=_CandidateBuilder(),
        worker_executor=executor, task_profiles=tuple(configured._profiles.values()),
        result_verifier=verifier,
        checkpoint_store=JsonlTaskPlanCheckpointStore(root / "checkpoints.jsonl"),
        ref_admission_service=admission,
    )
    agent = replace(_agent(), loop_policy=AgentLoopPolicy(max_iterations=3 if include_memory else 2, allow_subagents=True), metadata={
        "agent_orchestration": {"policy_ref": policy.exact_ref, "max_tasks_per_group": 2},
    })
    if include_memory:
        agent = replace(agent, allowed_tools=["memory.recall"], tool_policy=ToolPolicy(allowed_tools=["memory.recall"]))
    candidate = _request(template_identity).candidate
    if dependency_ref is not None:
        candidate = replace(candidate, tasks=(candidate.tasks[0], replace(
            candidate.tasks[1], depends_on=("structure",), input_refs=(dependency_ref,),
        )))
    batch = {"action_type": "delegate_batch", **candidate.to_dict()}

    class InspectingLLM(FakeLLMClient):
        def complete(self, request):
            snapshot = admission.snapshot
            assert snapshot.execution_identity == request.execution_identity
            assert grants.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref) == snapshot
            if include_memory:
                assert "admitted memory evidence note" in str(request.messages)
            return super().complete(request)

    llm = InspectingLLM(([
        json.dumps({"action_type": "tool_call", "tool_name": "memory.recall", "tool_args": {"query": "memory evidence"}}),
    ] if include_memory else []) + [
        json.dumps(batch),
        json.dumps({"action_type": "final_output", "output": {"analysis_result": {"summary": "done"}}}),
    ])
    runner = AgentRunner(
        llm_client=llm, tool_registry=ToolRegistry(),
        conversation_store=LocalJsonConversationStore(root / "conversation"),
    )
    effects = InMemoryHarnessSideEffectStore()
    effect_registry = HarnessSideEffectRegistry((HarnessSideEffectHandlerBinding(
        "production.agent-loop-terminal@1", "artifact",
        CountingHarnessSideEffectHandler(effects, disposition=HarnessSideEffectDisposition.ACCEPTED),
    ),))
    graph_runtime = build_agent_loop_graph_runtime_composition(
        agent_runner=runner, agent=agent, artifact_port=FilesystemHarnessArtifactPort(root / "graph-artifacts"),
        node_output_resource=SQLiteHarnessNodeOutputResource(root / "nodes.sqlite3"),
        event_port=_durable_event_port(root / "graph-events"),
        worker_ref=WORKER_REF, activity_ref=ACTIVITY_REF,
        orchestration_runtime=runtime, orchestration_feature_enabled=True,
        side_effect_store=effects, side_effect_registry=effect_registry,
    )
    return SimpleNamespace(**locals())

def test_real_graph_recall_and_tool_share_admitted_namespace(tmp_path):
    setup = _setup(tmp_path, include_memory=True)
    result = setup.graph_runtime.run(setup.spec)
    assert result.succeeded, result
    assert setup.llm.call_count == 3
    assert len(setup.child_calls) == 2
    assert "admitted memory evidence note" in str(setup.llm.requests[1].messages)
    assert setup.runner._tool_registry.maybe_get("memory.recall") is None
    assert setup.admission.snapshot.policy.allowed_memory_namespaces == ("memory.read",)


def test_real_graph_corrupt_memory_halts_before_llm_or_children(tmp_path, monkeypatch):
    setup = _setup(tmp_path, include_memory=True)

    def corrupt_payload(ref):
        raise HarnessValidationError("memory checksum corrupt", code="REF_CHECKSUM_MISMATCH")

    monkeypatch.setattr(setup.namespace_store, "read", corrupt_payload)
    result = setup.graph_runtime.run(setup.spec)
    assert not result.succeeded
    assert setup.llm.call_count == 0
    assert setup.child_calls == []
    assert setup.admission.snapshot is not None


def _admitted_request(setup):
    snapshot = setup.admission.snapshot
    return replace(_request(snapshot.execution_identity), policy_ref=setup.policy.exact_ref)


def test_real_graph_admits_before_llm_and_keeps_parent_physical_identity(tmp_path):
    setup = _setup(tmp_path)
    result = setup.graph_runtime.run(setup.spec)
    assert result.succeeded, result
    assert setup.llm.call_count == 2
    snapshot = setup.admission.snapshot
    assert snapshot.policy.allowed_refs == ("document",)
    assert snapshot.policy.writable_refs == ()
    assert "private-parent-only" not in str(snapshot.to_dict())
    assert len(setup.child_calls) == 2
    assert all(identity == snapshot.execution_identity for _, identity in setup.child_calls)
    assert ":delegate:" not in snapshot.execution_identity.activity_id
    plan = setup.task_store.plan(snapshot.run_id, snapshot.stage_id)
    assert plan.stage_binding_checksum == setup.binding.binding_checksum
    before = setup.events.get_stream_high_watermark(f"run:{snapshot.run_id}", tenant_id="control")
    reopened, _ = _store(tmp_path / "grants")
    assert HarnessRefAdmissionService(reopened).admit_graph_inputs(
        setup.admission.task, stage_binding=setup.binding, task_policy=setup.policy,
    ) == snapshot
    assert setup.events.get_stream_high_watermark(f"run:{snapshot.run_id}", tenant_id="control") == before
    original = next(item for item in setup.task_store.read_events(snapshot.run_id, snapshot.stage_id)
                    if item.event_type == "TASK_GROUP_ADMITTED")
    assert original.payload["group"]["parent_graph_identity"] == snapshot.execution_identity.to_dict()

    # Reopen both durable stores and return the original submission outcome.
    submission, = setup.task_store.submissions_for(snapshot.run_id, snapshot.stage_id)
    request = replace(
        _admitted_request(setup), parent_turn_id=submission.identity.parent_turn_id,
    )
    restored, restored_executor = _reopen_runtime(setup, tmp_path)
    before_tasks = setup.task_store.read_events(snapshot.run_id, snapshot.stage_id)
    repeated = restored.dispatch(request)
    assert repeated.status == "succeeded", repeated
    assert setup.task_store.read_events(snapshot.run_id, snapshot.stage_id) == before_tasks
    for task in plan.tasks:
        instance = task_instance_for_attempt(plan, task.task_id, 1)
        binding = setup.capabilities.resolve(task.task.worker_capability, setup.policy)
        recovered = restored_executor.recover(binding, instance, snapshot.execution_identity)
        assert recovered.status.value == "succeeded"
        assert recovered.evidence[0].evidence_type == "subagent_attempt"
    assert len(setup.child_calls) == 2
    assert setup.events.get_stream_high_watermark(f"run:{snapshot.run_id}", tenant_id="control") == before


def test_trusted_child_usage_survives_runner_underreport_through_task_result(
    tmp_path,
):
    setup = _setup(tmp_path, underreport_runner_metrics=True)

    outcome = setup.graph_runtime.run(setup.spec)

    assert outcome.succeeded, outcome
    assert len(setup.child_calls) == 2
    assert all(
        runner.observed_results
        and runner.observed_results[0].metrics == AgentLoopMetrics()
        for runner in setup.workers.values()
    )
    snapshot = setup.admission.snapshot
    plan = setup.task_store.plan(snapshot.run_id, snapshot.stage_id)
    records = {
        record.task_id: record
        for record in setup.task_store.results_for(
            plan.run_id,
            plan.stage_id,
            plan.plan_id,
            plan.version,
        )
    }
    assert set(records) == {task.task_id for task in plan.tasks}

    for task in plan.tasks:
        instance = task_instance_for_attempt(plan, task.task_id, 1)
        binding = setup.capabilities.resolve(
            task.task.worker_capability,
            setup.policy,
        )
        recovered = setup.executor.recover(
            binding,
            instance,
            snapshot.execution_identity,
        )
        receipt = SubAgentTranscriptReceipt.from_dict(
            recovered.evidence[0].payload
        )
        transcript = setup.transcripts.read(receipt.transcript_ref)
        trusted_events = tuple(
            event
            for event in transcript.events
            if event.get("schema_version")
            == "newsroom.trusted-child-execution/v1"
        )
        assert len(trusted_events) == 1
        trusted = validate_trusted_execution_event(
            trusted_events[0],
            identity=transcript.identity,
            tool_call_refs=transcript.tool_call_refs,
        )
        usage = TrustedChildUsage.from_dict(trusted["usage"])
        canonical_budget_usage = usage.budget_usage()

        assert usage.llm_calls == 1
        assert canonical_budget_usage["tokens"] > 0
        assert canonical_budget_usage["output_tokens"] > 0
        assert canonical_budget_usage == recovered.metrics
        assert canonical_budget_usage == dict(records[task.task_id].usage)
        assert recovered.diagnostics["used_tools"] == list(trusted["used_tools"])
        assert recovered.diagnostics["used_memory_namespaces"] == list(
            trusted["used_memory_namespaces"]
        )
        assert transcript.tool_call_refs == (
            trusted["tool_evidence"]["ref"],
        )
        assert trusted["tool_evidence"]["disposition"] == "NO_TOOL_REQUESTS"
        assert trusted["used_tools"] == ()
        # Input memory refs and execution namespace accounting are distinct.
        assert transcript.memory_context_refs == ()
        assert trusted["used_memory_namespaces"] == ("memory.read",)

    assert len(setup.child_calls) == 2


def _reopen_runtime(setup, root):
    grants, _ = _store(root / "grants")
    admission = HarnessRefAdmissionService(grants)
    task_events = SQLiteEventStore(root / "tasks.sqlite3")
    store = DurableTaskPlanStore(
        EventRuntime(store=task_events, schema_catalog=default_event_schema_catalog()),
        task_events, artifact_store=FilesystemArtifactStore(root / "task-artifacts"),
    )
    transcripts = FilesystemSubAgentTranscriptStore(root / "transcripts")
    artifacts = FilesystemHarnessArtifactPort(root / "child-artifacts")
    authority = HarnessResultRefAuthority(grants, transcript_store=transcripts, artifact_descriptors=artifacts, tenant_id="production")
    subagents = SubAgentRuntime(workers=setup.workers, transcript_store=transcripts, result_ref_authority=authority)
    canonical_tracker = GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=10_000))
    budget_trackers = CanonicalChildBudgetTrackerProvider(canonical_tracker)
    execution_service = HarnessChildExecutionService(
        store=store,
        ref_admission_service=admission,
        state_runtime=store._runtime,
        state_reader=store._reader,
        artifact_store=store._artifact_store,
        budget_trackers=budget_trackers,
        leases=None,
        evidence_limits=AdmissionChildToolEvidenceLimitsProvider(),
        usage_meter=CanonicalChildExecutionUsageMeter(budget_trackers),
        input_reader=None,
    )
    executor = HarnessSubAgentTaskExecutor(
        store=store,
        runtime=subagents,
        ref_admission_service=admission,
        task_policy=setup.policy,
        execution_service=execution_service,
    )
    verifier = TaskPlanResultVerifier(setup.gates, transcript_store=transcripts, artifact_reference_verifier=artifacts, result_ref_authority=authority)
    child_event_log = DurableChildAgentEventLog(
        state_runtime=store._runtime,
        state_reader=store._reader,
        state_key=f"agent-loop-parent-child-reopen:{root.resolve()}",
    )
    owned_child_runtime = HarnessOwnedChildAgentRuntime(
        event_log=child_event_log,
        max_children=max(1, setup.policy.max_parallelism),
        renewal_interval_seconds=None,
    )
    owned_child_runtime.start()
    return build_agent_loop_harness_orchestration_runtime(
        stage_binding=setup.binding, policy_registry=setup.configured._policy_registry,
        capability_registry=setup.capabilities, store=store,
        child_supervisor=owned_child_runtime.supervisor, candidate_builder=_CandidateBuilder(),
        worker_executor=executor, task_profiles=tuple(setup.configured._profiles.values()), result_verifier=verifier,
        checkpoint_store=JsonlTaskPlanCheckpointStore(root / "checkpoints.jsonl"), ref_admission_service=admission,
    ), executor


@pytest.fixture(scope="module")
def completed_children(tmp_path_factory):
    setup = _setup(tmp_path_factory.mktemp("generic-child-authority"))
    assert setup.graph_runtime.run(setup.spec).succeeded
    return setup


def test_child_and_result_grants_bind_the_exact_parent_and_distinct_attempts(completed_children):
    setup = completed_children
    parent = setup.admission.snapshot
    owners = set()
    for raw, execution in setup.child_calls:
        identity = SubAgentAttemptIdentity.from_dict(raw["attempt_identity"])
        child = setup.grants.find(run_id=parent.run_id, binding_key=RefAuthoritySnapshot.attempt_binding_key(identity, RefSnapshotPhase.CHILD_INPUT))
        result = setup.grants.find(run_id=parent.run_id, binding_key=RefAuthoritySnapshot.attempt_binding_key(identity, RefSnapshotPhase.RESULT_ACCEPTANCE))
        assert child.parent_snapshot_ref == parent.snapshot_ref
        assert result.parent_snapshot_ref == child.snapshot_ref
        assert child.execution_identity == result.execution_identity == execution == parent.execution_identity
        assert child.attempt_identity == result.attempt_identity == identity
        assert child.policy.allowed_refs == ("document",)
        assert child.policy.shared_read_only_refs == ("document",)
        assert child.policy.writable_refs == result.policy.writable_refs == ()
        assert child.policy.owner_id == result.policy.owner_id == identity.child_run_id
        assert len(result.descriptors) == 3
        owners.add(child.policy.owner_id)
    assert len(owners) == 2


@pytest.mark.parametrize("field,value", [
    ("node_id", "sibling"), ("node_instance_id", "other-instance"),
    ("activity_id", "other-activity"), ("attempt", 2), ("run_id", "other-run"),
])
def test_child_executor_rejects_other_physical_attempt_before_payload(completed_children, monkeypatch, field, value):
    setup = completed_children
    execution = setup.admission.snapshot.execution_identity
    plan = setup.task_store.plan(execution.run_id, setup.policy.stage_id)
    task = plan.tasks[0]
    instance = task_instance_for_attempt(plan, task.task_id, 1)
    binding = setup.capabilities.resolve(task.task.worker_capability, setup.policy)
    for name in ("read", "read_output", "read_context"):
        monkeypatch.setattr(setup.transcripts, name, lambda *_a, **_kw: pytest.fail("unauthorized payload read"))
    before = setup.events.get_stream_high_watermark(f"run:{execution.run_id}", tenant_id="control")
    with pytest.raises(HarnessValidationError):
        setup.executor.recover(binding, instance, replace(execution, **{field: value}))
    assert len(setup.child_calls) == 2
    assert setup.events.get_stream_high_watermark(f"run:{execution.run_id}", tenant_id="control") == before


def test_child_executor_cannot_issue_a_grant_for_an_unadmitted_attempt(completed_children):
    setup = completed_children
    execution = setup.admission.snapshot.execution_identity
    plan = setup.task_store.plan(execution.run_id, setup.policy.stage_id)
    task = plan.tasks[0]
    binding = setup.capabilities.resolve(task.task.worker_capability, setup.policy)
    future = task_instance_for_attempt(plan, task.task_id, 2)
    before = setup.events.get_stream_high_watermark(f"run:{execution.run_id}", tenant_id="control")
    with pytest.raises(HarnessValidationError, match="no admitted attempt"):
        setup.executor(binding, future, execution)
    assert len(setup.child_calls) == 2
    assert setup.events.get_stream_high_watermark(f"run:{execution.run_id}", tenant_id="control") == before


def test_child_result_verifier_rejects_sibling_receipt_before_payload(completed_children, monkeypatch):
    setup = completed_children
    execution = setup.admission.snapshot.execution_identity
    plan = setup.task_store.plan(execution.run_id, setup.policy.stage_id)
    own, sibling = plan.tasks
    own_instance = task_instance_for_attempt(plan, own.task_id, 1)
    sibling_result = setup.executor.recover(
        setup.capabilities.resolve(sibling.task.worker_capability, setup.policy),
        task_instance_for_attempt(plan, sibling.task_id, 1), execution,
    )
    for name in ("read", "read_output", "read_context"):
        monkeypatch.setattr(setup.transcripts, name, lambda *_a, **_kw: pytest.fail("sibling payload read"))
    with pytest.raises(HarnessValidationError):
        setup.verifier.verify(sibling_result, task=own, request=TaskPlanResultVerificationRequest(
            plan=plan, task=own, instance=own_instance, worker_result=sibling_result, execution_identity=execution,
        ))
    assert len(setup.child_calls) == 2


def test_generic_child_recovers_interrupted_result_grant_without_reexecuting(tmp_path, monkeypatch):
    setup = _setup(tmp_path)
    commit = setup.grants.commit
    interrupted = []

    def interrupt_result(snapshot):
        if snapshot.phase is RefSnapshotPhase.RESULT_ACCEPTANCE:
            interrupted.append(snapshot.attempt_identity)
            raise OSError("result grant publication interrupted")
        return commit(snapshot)

    monkeypatch.setattr(setup.grants, "commit", interrupt_result)
    assert not setup.graph_runtime.run(setup.spec).succeeded
    assert len(setup.child_calls) == 2
    assert interrupted
    identity = interrupted[0]
    metadata = setup.transcripts.describe_attempt(identity)
    assert metadata is not None
    before = setup.events.get_stream_high_watermark(f"run:{identity.parent_run_id}", tenant_id="control")
    with pytest.raises(HarnessValidationError):
        setup.authority.for_attempt(identity).find_by_identity(identity)
    assert setup.events.get_stream_high_watermark(f"run:{identity.parent_run_id}", tenant_id="control") == before
    _, executor = _reopen_runtime(setup, tmp_path)
    plan = executor.store.plan(identity.parent_run_id, identity.stage_id)
    task = next(item for item in plan.tasks if item.task_id == identity.task_id)
    result = executor.recover(
        setup.capabilities.resolve(task.task.worker_capability, setup.policy),
        task_instance_for_attempt(plan, identity.task_id, identity.attempt), setup.admission.snapshot.execution_identity,
    )
    assert result.status.value == "succeeded"
    assert result.output == {"summary": "completed"}
    assert len(setup.child_calls) == 2
    assert executor.result_ref_authority.for_attempt(identity).find_by_identity(identity) == metadata.receipt
    assert setup.events.get_stream_high_watermark(f"run:{identity.parent_run_id}", tenant_id="control") == before + 1


@pytest.mark.parametrize("field,value", [
    ("node_id", "sibling"), ("node_instance_id", "other-instance"),
    ("activity_id", "other-activity"), ("attempt", 2), ("run_id", "other-run"),
])
def test_parent_dispatch_cannot_rebind_admitted_execution(tmp_path, field, value):
    setup = _setup(tmp_path)
    assert setup.graph_runtime.run(setup.spec).succeeded
    request = _admitted_request(setup)
    snapshot = setup.admission.snapshot
    before = setup.task_store.read_events(snapshot.run_id, snapshot.stage_id)
    changed = replace(request.execution_identity, **{field: value})
    rejected = setup.runtime.dispatch(replace(request, run_id=changed.run_id, execution_identity=changed))
    assert rejected.status == "rejected"
    assert rejected.reason_code in {"agent_orchestration_graph_identity_mismatch", "REF_SNAPSHOT_MISSING"}
    assert len(setup.child_calls) == 2
    assert setup.task_store.read_events(snapshot.run_id, snapshot.stage_id) == before


def test_allowlisted_but_absent_parent_input_rejects_before_submission(tmp_path):
    setup = _setup(tmp_path, include_document=False)
    assert not setup.graph_runtime.run(setup.spec).succeeded
    assert setup.llm.call_count == 1
    assert setup.admission.snapshot.policy.allowed_refs == ()
    assert setup.child_calls == []
    assert setup.task_store.read_events(setup.spec.run_id, setup.policy.stage_id) == ()
    rejected = setup.runtime.dispatch(_admitted_request(setup))
    assert rejected.reason_code == "REF_UNRESOLVED"
    dedup = CandidateDedupIdentity(
        run_id=setup.spec.run_id, stage_id=setup.policy.stage_id,
        parent_turn_id="parent-turn-1", action_correlation_id="turn-1",
    )
    assert setup.task_store.candidate_submission(dedup) is None


def test_graph_worker_rejects_changed_input_before_parent_llm(tmp_path):
    setup = _setup(tmp_path)
    assert setup.graph_runtime.run(setup.spec).succeeded
    task = deepcopy(setup.admission.task)
    task["inputs"]["inputs"]["document"] = {"text": "changed"}
    worker = setup.graph_runtime.binding_bundle.worker_binding.implementation
    with pytest.raises(HarnessValidationError) as error:
        worker.execute(task)
    assert error.value.code == "REF_INPUT_CHECKSUM_MISMATCH"
    assert setup.llm.call_count == 2
    assert len(setup.child_calls) == 2


def test_explicit_agent_delegation_definition_roundtrip_and_conflicting_leaf_rejection(tmp_path):
    setup = _setup(tmp_path)
    definition = setup.spec.graph
    restored = HarnessGraphDefinition.from_dict(definition.to_dict())
    binding = TaskPlanStageBinding(HarnessGraphCompiler().compile(restored).graph, setup.policy.stage_id)
    assert binding == setup.binding
    assert binding.is_agent_delegation
    declaration = definition.task_plan_stage_bindings[0]
    with pytest.raises(HarnessValidationError) as error:
        replace(definition, definition_checksum=None, task_plan_stage_bindings=(
            replace(declaration, worker_ref=replace(WORKER_REF, contract_id="other-worker")),
        ))
    assert error.value.code == "graph_agent_delegation_binding_mismatch"
    with pytest.raises(HarnessValidationError) as error:
        forged = replace(
            definition, definition_checksum=None, task_plan_stage_bindings=(),
            activities=(replace(definition.activities[0], metadata={"dynamic_stage": True}),),
        )
        HarnessGraphCompiler().compile(forged)
    assert error.value.code == "graph_task_plan_authority_metadata_forbidden"
