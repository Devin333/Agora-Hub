from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

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
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.subagents.gates import FakeSubAgentGateSuite
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.capability import (
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
)
from framework.harness.task_plan.checkpoint import JsonlTaskPlanCheckpointStore
from framework.harness.task_plan.models import TaskBudget
from framework.harness.task_plan.parallel import (
    ParallelAgentCoordinator,
    SerialTaskExecutorAdapter,
)
from framework.harness.task_plan.policy import TaskPlanPolicy, TaskPlanPolicyRegistry
from framework.harness.task_plan.verification import TaskPlanGateRegistry, TaskPlanResultVerifier
from framework.harness.workers.result import HarnessWorkerResult
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.harness import FilesystemSubAgentTranscriptStore
from interfaces.composition.agent_loop_graph import (
    AgentLoopOrchestrationFeature,
    AgentOrchestrationCompositionState,
    build_agent_loop_harness_orchestration_runtime,
    build_agent_loop_orchestration_binding,
)
from tests.fixtures.agent_loop_delegation import (
    build_child_dependencies,
    build_ref_admission,
    build_task_plan_store,
)
from tests.fixtures.task_plan import build_task_plan_stage_binding


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


_DEFAULT_VERIFIER = object()


def _policy(*, max_planning_tool_calls: int = 0) -> TaskPlanPolicy:
    return TaskPlanPolicy(
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
        max_planning_tool_calls=max_planning_tool_calls,
    )


def _factory_kwargs(
    root,
    *,
    policy=None,
    store=None,
    admission=None,
    candidate_builder=None,
    result_verifier=_DEFAULT_VERIFIER,
    planning_observation_port=None,
):
    policy = policy or _policy()
    store = store or build_task_plan_store(root / "task-plan")
    admission = admission or build_ref_admission(root / "ref-authority")
    child = build_child_dependencies(
        root / "child",
        policy=policy,
        store=store,
        admission=admission,
        trusted_execution=True,
    )
    policy = child.policy
    stage_binding = build_task_plan_stage_binding(
        graph_id="composition",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        worker_type=HarnessWorkerType.AGENT_LOOP,
    )
    return {
        "stage_binding": stage_binding,
        "policy_registry": TaskPlanPolicyRegistry((policy,)),
        "capability_registry": child.capability_registry,
        "store": store,
        "child_supervisor": child.owned_runtime.supervisor,
        "candidate_builder": candidate_builder or _CandidateBuilder(),
        "worker_executor": child.executor,
        "task_profiles": (
            AgentOrchestrationTaskProfile(
                capability_hint="cap.structure",
                output_role="structure",
                output_schema_ref="schema://result@1",
                gate_refs=("gate@1",),
            ),
        ),
        "result_verifier": child.verifier if result_verifier is _DEFAULT_VERIFIER else result_verifier,
        "planning_observation_port": planning_observation_port,
        "checkpoint_store": JsonlTaskPlanCheckpointStore(root / "checkpoints.jsonl"),
        "ref_admission_service": admission,
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
    assert binding.composition_state is AgentOrchestrationCompositionState.ENABLED_PARALLEL
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
    assert binding.composition_state is (
        AgentOrchestrationCompositionState.DEPENDENCY_UNAVAILABLE
        if feature_enabled
        else AgentOrchestrationCompositionState.FEATURE_DISABLED
    )


def test_composition_binding_can_select_explicit_serial_rollout() -> None:
    binding = build_agent_loop_orchestration_binding(
        feature_enabled=True,
        dispatch=lambda _request: _result(),
        composition_state=AgentOrchestrationCompositionState.DEGRADED_SERIAL,
    )

    assert binding.available is True
    assert binding.composition_state is AgentOrchestrationCompositionState.DEGRADED_SERIAL
    assert binding.feature_enabled is True
    assert binding.availability_reason is None


def test_composition_state_is_not_derived_from_worker_result() -> None:
    binding = build_agent_loop_orchestration_binding(
        feature_enabled=True,
        dispatch=lambda _request: _result(),
    )

    assert binding.composition_state is AgentOrchestrationCompositionState.ENABLED_PARALLEL
    assert binding.port is not None


def test_disabled_composition_suppresses_dispatch_even_when_callback_is_supplied() -> None:
    binding = build_agent_loop_orchestration_binding(
        feature_enabled=False,
        dispatch=lambda _request: _result(),
    )

    assert binding.available is False
    assert binding.port is None
    assert binding.composition_state is AgentOrchestrationCompositionState.FEATURE_DISABLED
    assert binding.availability_reason == "feature_disabled"


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


def test_production_factory_rejects_missing_plan_builder(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path, candidate_builder=object())
    with pytest.raises((TypeError, ValueError), match="candidate_builder"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_missing_worker_binding(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path, candidate_builder=_CandidateBuilder())
    kwargs["capability_registry"] = TaskCapabilityRegistry()
    with pytest.raises((TypeError, ValueError), match="(worker|capability|binding)"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_binds_one_authorized_executor_for_execution_and_recovery(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path)

    runtime = build_agent_loop_harness_orchestration_runtime(**kwargs)
    executor = kwargs["worker_executor"]
    verifier = kwargs["result_verifier"]

    assert runtime.has_durable_input_admission is True
    assert runtime._stage_runner.worker_executor is executor
    assert runtime._stage_runner.worker_result_recovery == executor.recover
    assert executor.store is kwargs["store"]
    assert executor.ref_admission_service is kwargs["ref_admission_service"]
    assert executor.execution_service is not None
    assert executor.result_ref_authority is verifier.result_ref_authority
    assert executor.runtime.transcript_store is verifier.transcript_store
    assert (
        verifier.artifact_reference_verifier
        is executor.result_ref_authority.artifact_descriptors
    )


def test_production_factory_rejects_untrusted_child_execution_service(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path)
    kwargs["worker_executor"] = build_child_dependencies(
        tmp_path / "untrusted-child",
        policy=kwargs["policy_registry"].policies[0],
        store=kwargs["store"],
        admission=kwargs["ref_admission_service"],
        trusted_execution=False,
    ).executor

    with pytest.raises(ValueError, match="HarnessChildExecutionService"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_runtime_state_prefers_bound_parallel_supervisor_over_serial_fallback(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path)
    runtime = build_agent_loop_harness_orchestration_runtime(**kwargs)
    runtime._stage_runner.parallel_coordinator.serial_executor = SerialTaskExecutorAdapter()

    assert runtime.composition_state is AgentOrchestrationCompositionState.ENABLED_PARALLEL


def _break_production_child_binding(kwargs, root, case: str) -> str:
    policy = kwargs["policy_registry"].policies[0]
    executor = kwargs["worker_executor"]
    authority = executor.result_ref_authority

    if case == "arbitrary_executor":
        kwargs["worker_executor"] = lambda *_args: HarnessWorkerResult(
            status="succeeded", output={"summary": "bypassed"}
        )
        return "HarnessSubAgentTaskExecutor"
    if case == "task_plan_store":
        other_store = build_task_plan_store(root / "other-task-plan")
        kwargs["worker_executor"] = build_child_dependencies(
            root / "other-store-child",
            policy=policy,
            store=other_store,
            admission=kwargs["ref_admission_service"],
            trusted_execution=True,
        ).executor
        return "configured TaskPlan store"
    if case == "input_admission":
        other_admission = build_ref_admission(root / "other-admission")
        kwargs["worker_executor"] = build_child_dependencies(
            root / "other-admission-child",
            policy=policy,
            store=kwargs["store"],
            admission=other_admission,
            trusted_execution=True,
        ).executor
        return "configured input admission service"
    if case == "result_authority":
        kwargs["result_verifier"] = build_child_dependencies(
            root / "other-authority-child",
            policy=policy,
            store=kwargs["store"],
            admission=kwargs["ref_admission_service"],
            trusted_execution=True,
        ).verifier
        return "share result authority"
    if case == "transcript_store":
        kwargs["result_verifier"]._transcript_store = FilesystemSubAgentTranscriptStore(
            root / "other-transcripts"
        )
        return "share result authority and transcript store"
    if case == "artifact_owner":
        kwargs["result_verifier"] = TaskPlanResultVerifier(
            kwargs["result_verifier"].gate_registry,
            transcript_store=executor.runtime.transcript_store,
            artifact_reference_verifier=FilesystemHarnessArtifactPort(
                root / "other-artifacts"
            ),
            result_ref_authority=authority,
        )
        return "canonical artifact owner"
    if case == "gate_registry":
        kwargs["result_verifier"] = TaskPlanResultVerifier(
            TaskPlanGateRegistry(),
            transcript_store=executor.runtime.transcript_store,
            artifact_reference_verifier=executor.runtime.result_ref_authority.artifact_descriptors,
            result_ref_authority=authority,
        )
        return "every profile gate"
    if case == "runtime_worker":
        subagent_id = next(iter(executor.runtime.workers))
        executor.runtime.workers[subagent_id] = object()
        return "pinned capability binding"
    if case == "subagent_gates":
        executor.runtime.gates = SimpleNamespace(**vars(FakeSubAgentGateSuite()))
        return "deterministic SubAgent gate suite"
    if case == "worker_type":
        worker = _Worker()
        kwargs["capability_registry"] = TaskCapabilityRegistry(
            (
                TaskCapabilityRegistration(
                    "cap.structure",
                    HarnessWorkerBinding(
                        HarnessContractReference(
                            HarnessContractKind.WORKER,
                            worker.worker_id,
                            worker.worker_version,
                        ),
                        HarnessWorkerType.LLM,
                        worker,
                    ),
                    "structure-contract@1",
                    "schema://generic-child-input@1",
                    "schema://result@1",
                ),
            )
        )
        return "SUBAGENT worker"
    raise AssertionError(f"unknown child binding case: {case}")


@pytest.mark.parametrize(
    "case",
    (
        "arbitrary_executor",
        "task_plan_store",
        "input_admission",
        "result_authority",
        "transcript_store",
        "artifact_owner",
        "gate_registry",
        "runtime_worker",
        "subagent_gates",
        "worker_type",
    ),
)
def test_production_factory_rejects_child_dependency_mismatch(tmp_path, case: str) -> None:
    kwargs = _factory_kwargs(tmp_path / "base")
    expected = _break_production_child_binding(kwargs, tmp_path / case, case)

    with pytest.raises((TypeError, ValueError), match=expected):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_missing_result_evidence_verifier(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path, result_verifier=None)
    with pytest.raises((TypeError, ValueError), match="(result_verifier|artifact|transcript)"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_missing_planning_observation_port(tmp_path) -> None:
    kwargs = _factory_kwargs(tmp_path, policy=_policy(max_planning_tool_calls=1))
    with pytest.raises((TypeError, ValueError), match="planning_observation_port"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_production_factory_rejects_unbound_planning_port(tmp_path) -> None:
    kwargs = _factory_kwargs(
        tmp_path,
        policy=_policy(max_planning_tool_calls=1),
        planning_observation_port=_PlanningPort(),
    )
    with pytest.raises(ValueError, match="durable execution-bound reference authority"):
        build_agent_loop_harness_orchestration_runtime(**kwargs)


def test_planning_production_factory_and_stage_require_admitted_execution(tmp_path):
    from framework.harness.control_plane.errors import HarnessValidationError
    from framework.harness.task_plan.planning_observation import PlanningObservationRequest
    from framework.tool import ToolDefinition, ToolExecutor, ToolRegistry
    from interfaces.composition.agent_loop_graph import build_agent_loop_planning_observation_service
    from tests.framework.harness.test_ref_snapshot_store import _snapshot, _store

    grants, events = _store(tmp_path / "grants")
    admission = HarnessRefAdmissionService(grants)
    kwargs = _factory_kwargs(
        tmp_path / "composition",
        policy=_policy(max_planning_tool_calls=1),
        admission=admission,
    )
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
    assert runtime.has_durable_input_admission
    with pytest.raises(ValueError, match="share the canonical snapshot store"):
        build_agent_loop_harness_orchestration_runtime(**{**kwargs, "ref_admission_service": None})
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


def test_production_orchestration_requires_durable_parent_admission_with_planning_disabled(tmp_path):
    kwargs = _factory_kwargs(tmp_path)
    with pytest.raises(ValueError, match="durable input admission"):
        build_agent_loop_harness_orchestration_runtime(
            **{**kwargs, "ref_admission_service": None}
        )
