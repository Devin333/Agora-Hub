from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.agent.loop.runner import AgentRunner
from framework.agent.models import AgentLoopPolicy
from framework.events.runtime.publisher import EventRuntime
from framework.events.canonical import checksum_for
from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.compiler import HarnessGraphCompiler
from framework.harness.graph.definition import HarnessGraphDefinition, HarnessGraphTaskPlanStageBinding
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.side_effects import (
    CountingHarnessSideEffectHandler, HarnessSideEffectDisposition,
    HarnessSideEffectHandlerBinding, HarnessSideEffectRegistry, InMemoryHarnessSideEffectStore,
)
from framework.harness.task_plan.checkpoint import JsonlTaskPlanCheckpointStore
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.harness.task_plan.submission import CandidateDedupIdentity
from framework.harness.workers.result import HarnessWorkerResult
from framework.llm import FakeLLMClient
from framework.tool import ToolRegistry
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.conversation import LocalJsonConversationStore
from infrastructure.storage.events.sqlite import SQLiteEventStore
from infrastructure.storage.harness import SQLiteHarnessNodeOutputResource
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


def _setup(root, *, include_document=True):
    template, template_identity = _runtime()
    policy = replace(template._policy_registry.policies[0], stage_id="run-agent-loop", max_planning_tool_calls=0)
    spec = _runtime_run_spec("parent-ref-run", identity_scope_ref=checksum_for("production"))
    declaration = HarnessGraphTaskPlanStageBinding(
        activity_id=policy.stage_id, worker_ref=WORKER_REF, activity_ref=ACTIVITY_REF,
        policy_ref=policy.exact_ref, task_plan_schema=template._stage_binding.task_plan_schema,
        required_output_roles=policy.required_output_roles,
        support_refs=template._stage_binding.support_refs,
    )
    definition = replace(spec.graph, task_plan_stage_bindings=(declaration,), definition_checksum=None)
    binding = TaskPlanStageBinding(HarnessGraphCompiler().compile(definition).graph, policy.stage_id)
    business_inputs = {"parent_private": "private-parent-only"}
    if include_document:
        business_inputs["document"] = {"text": "trusted document"}
    spec = replace(spec, graph=definition, inputs={**spec.inputs, "inputs": business_inputs})
    grants, events = _store(root / "grants")
    admission = _RecordingAdmission(grants)
    task_events = SQLiteEventStore(root / "tasks.sqlite3")
    task_store = DurableTaskPlanStore(
        EventRuntime(store=task_events, schema_catalog=default_event_schema_catalog()),
        task_events, artifact_store=FilesystemArtifactStore(root / "task-artifacts"),
    )
    child_calls = []

    def worker(binding, instance, identity):
        child_calls.append((instance, identity))
        assert "private-parent-only" not in str(instance.to_dict())
        return HarnessWorkerResult(status="succeeded", output={"summary": "completed"})

    configured, _ = _runtime(
        policy=policy, stage_binding=binding, store=task_store, worker_executor=worker,
    )
    runtime = build_agent_loop_harness_orchestration_runtime(
        stage_binding=binding, policy_registry=configured._policy_registry,
        capability_registry=configured._capability_registry, store=task_store,
        child_supervisor=configured._child_supervisor, candidate_builder=_CandidateBuilder(),
        worker_executor=worker, task_profiles=tuple(configured._profiles.values()),
        result_verifier=configured._stage_runner.result_verifier,
        checkpoint_store=JsonlTaskPlanCheckpointStore(root / "checkpoints.jsonl"),
        ref_admission_service=admission,
    )
    agent = replace(_agent(), loop_policy=AgentLoopPolicy(max_iterations=2, allow_subagents=True), metadata={
        "agent_orchestration": {"policy_ref": policy.exact_ref, "max_tasks_per_group": 2},
    })
    candidate = _request(template_identity).candidate
    batch = {"action_type": "delegate_batch", **candidate.to_dict()}

    class InspectingLLM(FakeLLMClient):
        def complete(self, request):
            snapshot = admission.snapshot
            assert snapshot.execution_identity == request.execution_identity
            assert grants.get(run_id=snapshot.run_id, snapshot_ref=snapshot.snapshot_ref) == snapshot
            return super().complete(request)

    llm = InspectingLLM([
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
    restored, _ = _runtime(
        policy=setup.policy, stage_binding=setup.binding,
        store=DurableTaskPlanStore(
            EventRuntime(store=setup.task_events, schema_catalog=default_event_schema_catalog()),
            setup.task_events, artifact_store=FilesystemArtifactStore(tmp_path / "task-artifacts"),
        ), ref_admission_service=HarnessRefAdmissionService(reopened),
        worker_executor=lambda *_args: pytest.fail("redelivery must not invoke a child"),
    )
    before_tasks = setup.task_store.read_events(snapshot.run_id, snapshot.stage_id)
    repeated = restored.dispatch(request)
    assert repeated.status == "succeeded", repeated
    assert setup.task_store.read_events(snapshot.run_id, snapshot.stage_id) == before_tasks


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
