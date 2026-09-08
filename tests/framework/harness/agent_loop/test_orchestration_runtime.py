from __future__ import annotations

from framework.agent.models import DelegateBatchCandidate, DelegateBatchProposal
from framework.harness.agent_loop.orchestration import (
    AgentOrchestrationRequest,
    AgentOrchestrationTaskProfile,
    HarnessAgentOrchestrationRuntime,
    ParentObservationLimits,
)
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.capability import (
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
)
from framework.harness.task_plan.models import TaskBudget
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.harness.task_plan.policy import TaskPlanPolicy, TaskPlanPolicyRegistry
from framework.harness.task_plan.stage import TaskPlanStageRunner
from framework.harness.task_plan.store import InMemoryTaskPlanStore, TaskResultRecord
from framework.harness.task_plan.checkpoint import InMemoryTaskPlanCheckpointStore
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphExecutionIdentity
from interfaces.composition.agent_loop_graph import (
    build_agent_loop_orchestration_binding,
)
from tests.fixtures.task_plan import build_task_plan_stage_binding


class _AcceptingResultVerifier:
    registered_gate_refs = ("gate@1",)

    def verify(self, result, *, task, request):
        instance = request.instance
        return TaskResultRecord.for_plan(
            request.plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            status="succeeded",
            result_ref=f"result://{instance.task_id}",
            output_roles=(task.output_role,),
            output_schema_ref=task.task.output_contract.schema_ref,
            verified_gate_refs=task.gate_refs,
        )


class _BoundWorker:
    worker_version = "1"
    worker_type = HarnessWorkerType.LLM

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id

    def execute(self, _task):
        return HarnessWorkerResult(status="succeeded", output={"summary": "completed"})


def _runtime(*, store=None, worker_executor=None, result_verifier=None, ref_admission_service=None, agent_delegation=False, policy=None, stage_binding=None) -> tuple[HarnessAgentOrchestrationRuntime, GraphExecutionIdentity]:
    policy = policy or TaskPlanPolicy(
        policy_id="agent.loop.delegate",
        version="1",
        stage_id="delegate_stage",
        allowed_worker_capabilities=("cap.structure", "cap.contribution"),
        allowed_subagent_ids=(),
        allowed_tool_ids=("tool.read",),
        allowed_memory_namespaces=("memory.read",),
        allowed_input_refs=("document",),
        allowed_output_roles=("structure", "contribution"),
        required_output_roles=("structure", "contribution"),
        allowed_output_schema_refs=("schema://result@1",),
        allowed_gate_refs=("gate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={
            "cap.structure": "structure-worker@1",
            "cap.contribution": "contribution-worker@1",
        },
        required_worker_contract_refs={
            "cap.structure": "structure-contract@1",
            "cap.contribution": "contribution-contract@1",
        },
        max_tasks=2,
        max_depth=2,
        max_parallelism=2,
        max_replans=0,
        max_task_attempts=1,
        max_plan_build_calls=1,
        max_plan_build_turns=1,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(max_turns=1),
        aggregate_task_budget=TaskBudget(max_turns=2),
        capability_capacity=2,
        available_concurrency_reservations=2,
        max_tasks_per_group=2,
    )
    stage_binding = stage_binding or build_task_plan_stage_binding(
        graph_id="agent-graph",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        worker_type=HarnessWorkerType.AGENT_LOOP if agent_delegation else HarnessWorkerType.TASK_PLAN,
    )
    registrations = []
    for capability, worker_id in (
        ("cap.structure", "structure-worker"),
        ("cap.contribution", "contribution-worker"),
    ):
        binding = HarnessWorkerBinding(
            HarnessContractReference(HarnessContractKind.WORKER, worker_id, "1"),
            HarnessWorkerType.LLM,
            _BoundWorker(worker_id),
        )
        registrations.append(
            TaskCapabilityRegistration(
                capability,
                binding,
                f"{capability.rsplit('.', 1)[-1]}-contract@1",
                "schema://input@1",
                "schema://result@1",
            )
        )
    capability_registry = TaskCapabilityRegistry(registrations)
    store = store if store is not None else InMemoryTaskPlanStore()
    checkpoint_store = InMemoryTaskPlanCheckpointStore()
    supervisor = ChildAgentSupervisor(max_children=2)
    runner = TaskPlanStageRunner(
        candidate_builder=object(),
        capability_registry=capability_registry,
        store=store,
        result_verifier=result_verifier or _AcceptingResultVerifier(),
        worker_executor=worker_executor or (lambda _binding, _task, _identity: HarnessWorkerResult(
            status="succeeded",
            output={"summary": "completed"},
        )),
        parallel_coordinator=ParallelAgentCoordinator(
            max_workers=2,
            child_supervisor=supervisor,
        ),
        child_supervisor_capacity=supervisor.capacity,
        checkpoint_store=checkpoint_store,
    )
    runtime = HarnessAgentOrchestrationRuntime(
        stage_binding=stage_binding,
        policy_registry=TaskPlanPolicyRegistry((policy,)),
        capability_registry=capability_registry,
        store=store,
        stage_runner=runner,
        child_supervisor=supervisor,
        task_profiles=(
            AgentOrchestrationTaskProfile(
                capability_hint="cap.structure",
                output_role="structure",
                output_schema_ref="schema://result@1",
                gate_refs=("gate@1",),
            ),
            AgentOrchestrationTaskProfile(
                capability_hint="cap.contribution",
                output_role="contribution",
                output_schema_ref="schema://result@1",
                gate_refs=("gate@1",),
            ),
        ),
        require_durable_store=False,
        ref_admission_service=ref_admission_service,
    )
    graph = stage_binding.graph
    identity = GraphExecutionIdentity(
        run_id="run-1",
        graph_id=graph.graph_id,
        graph_version=graph.graph_version,
        graph_ref=graph.identity_ref.exact_ref,
        graph_checksum=stage_binding.graph_checksum,
        node_id=stage_binding.node_id,
        node_instance_id="parent-node-1",
        activity_id="parent-activity-1",
        attempt=1,
    )
    return runtime, identity


def _request(identity: GraphExecutionIdentity) -> AgentOrchestrationRequest:
    return AgentOrchestrationRequest(
        parent_agent_id="parent",
        parent_turn_id="parent-turn-1",
        run_id=identity.run_id,
        execution_identity=identity,
        graph_checkpoint_ref="graph-checkpoint://run-1/1/checksum",
        policy_ref="agent.loop.delegate@1",
        max_tasks_per_group=2,
        parent_observation_limits=ParentObservationLimits(),
        candidate=DelegateBatchCandidate(
            correlation_id="turn-1",
            parallelism_hint=2,
            tasks=(
                DelegateBatchProposal(
                    logical_task_id="structure",
                    objective="Analyze the document structure",
                    capability_hint="cap.structure",
                    input_refs=("document",),
                    output_role="structure",
                ),
                DelegateBatchProposal(
                    logical_task_id="contribution",
                    objective="Analyze the document contribution",
                    capability_hint="cap.contribution",
                    input_refs=("document",),
                    output_role="contribution",
                ),
            ),
        ),
    )


def test_harness_runtime_materializes_and_joins_delegate_batch() -> None:
    runtime, identity = _runtime()

    result = runtime.dispatch(_request(identity))

    assert result.status == "succeeded"
    assert result.observation.group_status == "succeeded"
    assert [item.logical_task_id for item in result.observation.task_summaries] == [
        "contribution",
        "structure",
    ]
    assert len(result.observation.wave_summaries) == 1
    assert result.observation.aggregate_ref is not None


def test_runtime_binding_is_available_only_when_a_real_runtime_is_supplied() -> None:
    unavailable = build_agent_loop_orchestration_binding(
        feature_enabled=True,
        runtime=None,
        dispatch=None,
    )
    runtime, _identity = _runtime()
    available = build_agent_loop_orchestration_binding(
        feature_enabled=True,
        runtime=runtime,
    )

    assert unavailable.available is False
    assert unavailable.availability_reason == "agent_orchestration_unavailable"
    assert available.available is True
    assert available.port is runtime
