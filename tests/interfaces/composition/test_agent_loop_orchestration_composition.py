from __future__ import annotations

import pytest

from framework.agent.models import DelegateBatchCandidate, DelegateBatchProposal
from framework.harness.agent_loop import (
    AgentOrchestrationRequest,
    AgentOrchestrationResult,
    AgentOrchestrationTaskProfile,
    ParentObservation,
    ParentObservationLimits,
    ParentTaskSummary,
)
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.capability import (
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
)
from framework.harness.task_plan.checkpoint import TaskPlanCheckpointStorePort
from framework.harness.task_plan.durable_store import DurableTaskPlanStore
from framework.harness.task_plan.models import TaskBudget
from framework.harness.task_plan.parallel import (
    ParallelAgentCoordinator,
    SerialTaskExecutorAdapter,
)
from framework.harness.task_plan.policy import TaskPlanPolicy, TaskPlanPolicyRegistry
from framework.harness.task_plan.verification import TaskPlanResultVerifier
from framework.harness.workers.result import HarnessWorkerResult
from interfaces.composition.agent_loop_graph import (
    AgentLoopOrchestrationFeature,
    build_agent_loop_harness_orchestration_runtime,
    build_agent_loop_orchestration_binding,
)
from tests.fixtures.task_plan import build_task_plan_stage_binding


class _DurableStoreStub(DurableTaskPlanStore):
    """Protocol-shaped store; no persistence is exercised by composition tests."""

    def __init__(self) -> None:
        pass


class _CheckpointStub(TaskPlanCheckpointStorePort):
    is_durable = True

    def save(self, checkpoint):
        return checkpoint

    def load(self, checkpoint_id):
        raise KeyError(checkpoint_id)


class _Worker:
    worker_id = "structure-worker"
    worker_version = "1"
    worker_type = HarnessWorkerType.LLM

    def execute(self, _task):
        return HarnessWorkerResult(status="succeeded", output={})


class _CandidateBuilder:
    def build_candidate(self, request):
        raise AssertionError("composition test must not execute the candidate builder")


class _PlanningPort:
    def observe(self, request):
        raise AssertionError("composition test must not execute planning tools")

    def replay(self, request):
        raise AssertionError("composition test must not replay planning tools")

    def validate_source_refs(self, source_observation_refs, *, run_id, stage_id, planner_turn_id, policy_checksum):
        return ()


def _factory_kwargs(*, candidate_builder=None, result_verifier=None, planning_observation_port=None):
    policy = TaskPlanPolicy(
        policy_id="composition.delegate",
        version="1",
        stage_id="delegate_stage",
        allowed_worker_capabilities=("cap.structure",),
        allowed_subagent_ids=(),
        allowed_tool_ids=("tool.read",),
        allowed_memory_namespaces=("memory.read",),
        allowed_input_refs=("document",),
        allowed_output_roles=("structure",),
        required_output_roles=("structure",),
        allowed_output_schema_refs=("schema://result@1",),
        allowed_gate_refs=("gate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={"cap.structure": "structure-worker@1"},
        required_worker_contract_refs={"cap.structure": "structure-contract@1"},
        max_tasks=1,
        max_depth=1,
        max_parallelism=1,
        max_replans=0,
        max_task_attempts=1,
        max_plan_build_calls=1,
        max_plan_build_turns=1,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(max_turns=1),
        aggregate_task_budget=TaskBudget(max_turns=1),
        max_planning_tool_calls=1,
    )
    stage_binding = build_task_plan_stage_binding(
        graph_id="composition",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
    )
    worker = _Worker()
    registration = TaskCapabilityRegistration(
        "cap.structure",
        HarnessWorkerBinding(
            HarnessContractReference(HarnessContractKind.WORKER, "structure-worker", "1"),
            HarnessWorkerType.LLM,
            worker,
        ),
        "structure-contract@1",
        "schema://input@1",
        "schema://result@1",
    )
    return {
        "stage_binding": stage_binding,
        "policy_registry": TaskPlanPolicyRegistry((policy,)),
        "capability_registry": TaskCapabilityRegistry((registration,)),
        "store": _DurableStoreStub(),
        "child_supervisor": ChildAgentSupervisor(max_children=1),
        "candidate_builder": candidate_builder or _CandidateBuilder(),
        "worker_executor": lambda *_args: HarnessWorkerResult(status="succeeded", output={}),
        "task_profiles": (
            AgentOrchestrationTaskProfile(
                capability_hint="cap.structure",
                output_role="structure",
                output_schema_ref="schema://result@1",
                gate_refs=("gate@1",),
            ),
        ),
        "result_verifier": result_verifier,
        "planning_observation_port": planning_observation_port,
        "checkpoint_store": _CheckpointStub(),
    }


def _request() -> AgentOrchestrationRequest:
    return AgentOrchestrationRequest(
        parent_agent_id="parent",
        parent_turn_id="parent-turn-1",
        run_id=None,
        execution_identity=None,
        graph_checkpoint_ref=None,
        policy_ref="parent-policy@1",
        max_tasks_per_group=2,
        parent_observation_limits=ParentObservationLimits(),
        candidate=DelegateBatchCandidate(
            correlation_id="turn-1",
            tasks=(
                DelegateBatchProposal(
                    logical_task_id="structure",
                    objective="Analyze structure",
                    capability_hint="research.structure@1",
                    input_refs=("artifact://source",),
                    output_role="structure",
                ),
            ),
        ),
    )


def _result(*, task_id: str = "structure") -> AgentOrchestrationResult:
    return AgentOrchestrationResult(
        status="succeeded",
        observation=ParentObservation(
            group_id="group-1",
            group_status="succeeded",
            plan_version="1",
            task_summaries=(
                ParentTaskSummary(
                    logical_task_id=task_id,
                    status="succeeded",
                    summary="verified summary",
                ),
            ),
        ),
    )


def test_composition_binding_wraps_real_harness_dispatcher() -> None:
    requests: list[AgentOrchestrationRequest] = []

    def dispatch(request: AgentOrchestrationRequest) -> AgentOrchestrationResult:
        requests.append(request)
        return _result()

    binding = build_agent_loop_orchestration_binding(
        feature_enabled=True,
        dispatch=dispatch,
    )

    assert binding.available is True
    assert binding.port is not None
    assert binding.port.dispatch(_request()) == _result()
    assert requests == [_request()]


@pytest.mark.parametrize(
    ("feature_enabled", "reason"),
    [(True, "agent_orchestration_unavailable"), (False, "feature_disabled")],
)
def test_composition_binding_reports_stable_unavailability(
    feature_enabled: bool,
    reason: str,
) -> None:
    binding = build_agent_loop_orchestration_binding(
        feature_enabled=feature_enabled,
        dispatch=None,
    )

    assert binding.available is False
    assert binding.port is None
    assert binding.availability_reason == reason


def test_composition_adapter_rejects_join_result_outside_candidate() -> None:
    binding = build_agent_loop_orchestration_binding(
        feature_enabled=True,
        dispatch=lambda _request: _result(task_id="unrelated"),
    )

    assert binding.port is not None
    with pytest.raises(ValueError, match="outside the submitted candidate"):
        binding.port.dispatch(_request())


def test_feature_defaults_off_and_dynamic_research_is_explicitly_scoped() -> None:
    default = AgentLoopOrchestrationFeature()
    assert default.enabled_for("generic") is False
    assert default.enabled_for("research_dynamic") is False

    research = AgentLoopOrchestrationFeature(enabled=True, rollout_scope="research_dynamic")
    assert research.enabled_for("research_dynamic") is True
    assert research.enabled_for("generic") is False


def test_parallel_production_requires_an_explicit_serial_adapter() -> None:
    with pytest.raises(ValueError, match="ChildAgentSupervisor or SerialTaskExecutorPort"):
        ParallelAgentCoordinator(max_workers=1)

    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    assert coordinator.serial_executor is not None


def test_production_factory_rejects_missing_plan_builder() -> None:
    kwargs = _factory_kwargs(candidate_builder=object())
    with pytest.raises((TypeError, ValueError), match="candidate_builder"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_missing_worker_binding() -> None:
    kwargs = _factory_kwargs(candidate_builder=_CandidateBuilder())
    kwargs["capability_registry"] = TaskCapabilityRegistry()
    with pytest.raises((TypeError, ValueError), match="(worker|capability|binding)"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_missing_result_evidence_verifier() -> None:
    kwargs = _factory_kwargs(result_verifier=None, planning_observation_port=_PlanningPort())
    with pytest.raises((TypeError, ValueError), match="(result_verifier|artifact|transcript)"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_missing_planning_observation_port() -> None:
    kwargs = _factory_kwargs(result_verifier=TaskPlanResultVerifier())
    with pytest.raises((TypeError, ValueError), match="planning_observation_port"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_unbound_planning_port() -> None:
    kwargs = _factory_kwargs(result_verifier=TaskPlanResultVerifier(), planning_observation_port=_PlanningPort())
    with pytest.raises(ValueError, match="durable execution-bound reference authority"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_planning_production_factory_and_stage_require_admitted_execution(tmp_path):
    from dataclasses import replace

    from framework.harness.control_plane.errors import HarnessValidationError
    from framework.harness.task_plan.planning_observation import PlanningObservationRequest
    from framework.tool import ToolDefinition, ToolExecutor, ToolRegistry
    from interfaces.composition.agent_loop_graph import build_agent_loop_planning_observation_service
    from tests.framework.harness.test_ref_snapshot_store import _snapshot, _store

    kwargs = _factory_kwargs(result_verifier=TaskPlanResultVerifier())
    binding = kwargs["stage_binding"]
    policy = kwargs["policy_registry"].resolve(binding.policy_ref, stage_id=binding.stage_id)
    parent = _snapshot()
    descriptor = replace(parent.descriptors[0], stage_id=binding.stage_id)
    parent = replace(
        parent, stage_id=binding.stage_id,
        execution_identity=replace(
            parent.execution_identity, graph_id=binding.graph_id, graph_version=binding.graph_version,
            graph_ref=binding.graph.graph_ref.exact_ref, graph_checksum=binding.graph_checksum,
            node_id=binding.node_id,
        ),
        stage_binding_checksum=binding.binding_checksum, task_policy_checksum=policy.policy_checksum,
        descriptors=(descriptor,), policy=replace(parent.policy, stage_id=binding.stage_id),
    )
    grants, events = _store(tmp_path)
    grants.commit(parent)
    registry = ToolRegistry()
    calls = []
    registry.register(ToolDefinition(
        name="tool.read", version="1", side_effect="read_only", input_schema={"type": "object"},
    ), lambda args: calls.append(args) or {"evidence": "recorded"})
    factory = dict(
        policy=policy, executor=ToolExecutor(registry), registry=registry,
        receipt_path=tmp_path / "planning", input_snapshot=parent,
        snapshot_store=grants, planner_turn=1,
    )
    planning = build_agent_loop_planning_observation_service(**factory)
    kwargs["planning_observation_port"] = planning
    runtime = build_agent_loop_harness_orchestration_runtime(**kwargs)
    request = PlanningObservationRequest(
        request_id="lookup", run_id=parent.run_id, stage_id=parent.stage_id,
        planner_turn_id=planning.planning_ref_authority.planner_turn_id,
        policy_checksum=policy.policy_checksum, correlation_id="lookup", tool_name="tool.read",
        purpose="Read a planning fact",
    )
    with pytest.raises(HarnessValidationError, match="Graph execution"):
        runtime.observe_for_planning(request)
    assert calls == []
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
    receipt = runtime.observe_for_planning(request, execution_identity=parent.execution_identity)
    assert receipt.status == "SUCCEEDED"
    assert len(calls) == 1
    assert planning.replay(request) == receipt
    with pytest.raises(ValueError, match="planner_turn"):
        build_agent_loop_planning_observation_service(**{**factory, "planner_turn": 2})
    with pytest.raises(ValueError, match="durable reference"):
        build_agent_loop_planning_observation_service(**{**factory, "snapshot_store": object()})
