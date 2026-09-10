from __future__ import annotations

from collections.abc import Mapping

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan import (
    FakePlanCandidateBuilder,
    InMemoryTaskPlanStore,
    PlanBuildBudget,
    PlanCandidate,
    TaskAcceptanceCriteria,
    TaskAdmissionOwner,
    TaskBudget,
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
    TaskOutputContract,
    TaskPlanPolicy,
    TaskPlanStageIdentity,
    TaskPlanStageRequest,
    TaskPlanStageRunner,
    TaskPlanValidationContext,
    TaskPlanValidator,
    TaskSpec,
    task_instance_for_attempt,
)
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.capacity import FirstFitPacking
from framework.harness.task_plan.parallel import (
    DispatchWave,
    DispatchWaveState,
    ParallelAgentCoordinator,
    ParallelDispatchRequest,
    TaskReservation,
    child_budget_reservation,
    spawn_operation_key,
)
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision
from framework.shared.graph_identity import GraphExecutionIdentity
from tests.fixtures.task_plan import build_task_plan_stage_binding


class _Worker:
    worker_id = "receipt-worker"
    worker_version = "1"
    worker_type = HarnessWorkerType.LLM

    def execute(self, _task):
        raise AssertionError("receipt history tests do not execute workers")


def _runner_fixture():
    policy = TaskPlanPolicy(
        policy_id="spawn.receipt-history",
        version="1",
        stage_id="receipt-stage",
        allowed_worker_capabilities=("receipt-capability",),
        allowed_subagent_ids=(),
        allowed_tool_ids=(),
        allowed_memory_namespaces=(),
        allowed_input_refs=("document",),
        allowed_output_roles=("analysis.receipt",),
        required_output_roles=("analysis.receipt",),
        allowed_output_schema_refs=("schema://receipt@1",),
        allowed_gate_refs=("ReceiptGate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={"receipt-capability": "receipt-worker@1"},
        required_worker_contract_refs={"receipt-capability": "receipt-contract@1"},
        max_tasks=1,
        max_depth=1,
        max_parallelism=1,
        max_replans=0,
        max_task_attempts=1,
        max_plan_build_calls=1,
        max_plan_build_turns=1,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(
            max_turns=1,
            token_limit=1024,
            time_limit_ms=60_000,
        ),
        aggregate_task_budget=TaskBudget(
            max_turns=1,
            token_limit=1024,
            time_limit_ms=60_000,
        ),
    )
    binding = build_task_plan_stage_binding(
        graph_id="spawn.receipt-history",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        input_keys=("document",),
    )
    registry = TaskCapabilityRegistry(
        (
            TaskCapabilityRegistration(
                "receipt-capability",
                HarnessWorkerBinding(
                    HarnessContractReference(
                        HarnessContractKind.WORKER,
                        "receipt-worker",
                        "1",
                    ),
                    HarnessWorkerType.LLM,
                    _Worker(),
                ),
                "receipt-contract@1",
                "schema://input@1",
                "schema://receipt@1",
            ),
        )
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=TaskPlanStageIdentity("receipt-run", binding),
        candidate_id="receipt-candidate",
        input_context_refs=("document",),
        tasks=(
            TaskSpec(
                task_id="receipt-task",
                objective="persist a spawn receipt",
                worker_capability="receipt-capability",
                input_refs=("document",),
                output_contract=TaskOutputContract(
                    "schema://receipt@1",
                    "analysis.receipt",
                ),
                acceptance_criteria=TaskAcceptanceCriteria(("ReceiptGate@1",)),
                budget_request=TaskBudget(
                    max_turns=1,
                    token_limit=1024,
                    time_limit_ms=60_000,
                ),
                retry_policy={"max_attempts": 1, "retryable_reason_codes": []},
            ),
        ),
        required_output_roles=("analysis.receipt",),
        generated_by="receipt-planner@1",
        requested_plan_budget=PlanBuildBudget(max_builder_calls=1, max_turns=1),
        requested_max_parallelism=1,
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=TaskPlanValidationContext(
            run_id="receipt-run",
            stage_binding=binding,
            available_input_refs=("document",),
            registered_gate_refs=("ReceiptGate@1",),
        ),
        accepted_at="2026-09-06T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    execution_identity = GraphExecutionIdentity(
        run_id=plan.run_id,
        graph_id=plan.graph_id,
        graph_version=plan.graph_version,
        graph_ref=plan.graph_ref,
        graph_checksum=plan.graph_checksum,
        node_id=plan.stage_id,
        node_instance_id=f"{plan.stage_id}-node-1",
        activity_id=plan.stage_id,
        attempt=1,
    )
    request = TaskPlanStageRequest(
        run_id=plan.run_id,
        stage_binding=binding,
        context_refs={"document": "document"},
        policy=policy,
        candidate=candidate,
        accepted_at="2026-09-06T00:00:00Z",
        execution_identity=execution_identity,
    )
    runner = TaskPlanStageRunner(
        candidate_builder=FakePlanCandidateBuilder(candidate),
        capability_registry=registry,
        store=store,
    )
    return runner, request, plan, store


def _seed_spawn_intent(runner, request, plan, store) -> dict[str, object]:
    instance = task_instance_for_attempt(plan, "receipt-task", 1)
    ledger_before = TaskPlanBudgetLedger.for_plan(plan)
    dispatch_request = ParallelDispatchRequest(
        plan=plan,
        task_instances=(instance,),
        budget_snapshot=ledger_before.snapshot(),
        requested_parallelism=1,
        capability_capacity=1,
        supervisor_capacity=1,
        available_concurrency_reservations=1,
        parent_graph_identity=request.execution_identity,
    )
    group_events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(max_children=1)
    try:
        group = ParallelAgentCoordinator(
            max_workers=1,
            child_supervisor=supervisor,
        ).create_group(
            dispatch_request,
            event_sink=group_events.append,
        )
    finally:
        supervisor.shutdown()
    assert len(group_events) == 1
    runner._record_parallel_events(request, plan, tuple(group_events))

    initial = store.load_projection(plan.run_id, plan.stage_id)
    readiness = TaskPlanReadyDecision(
        (instance,),
        logical_ready_task_ids=(instance.task_id,),
    )
    runner._persist_logical_readiness(request, plan, initial, readiness)
    ready = store.load_projection(plan.run_id, plan.stage_id)
    assert ready.logical_ready_order == (instance.task_id,)
    assert ready.tasks[0].active_instance_id is None
    ready_ledger = TaskPlanBudgetLedger.from_snapshot(ready.consumed_budget)
    assert ready_ledger.to_dict() == ledger_before.to_dict()

    admitted = runner.scheduler.admit_task_plan_tasks(
        ready,
        (instance,),
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    admitted_ledger = TaskPlanBudgetLedger.from_snapshot(admitted.consumed_budget)
    before_checksum = ready_ledger.to_dict()["ledger_checksum"]
    after_checksum = admitted_ledger.to_dict()["ledger_checksum"]
    packing = FirstFitPacking(
        ready_order=(instance.task_id,),
        selected=(instance.task_id,),
        overflow=(),
        reservations=(),
        reasons={},
        budget_before_checksum=before_checksum,
        budget_after_checksum=after_checksum,
        admitted_budget_snapshot=admitted.consumed_budget,
    )
    wave = DispatchWave(
        group_id=group.group_id,
        ordinal=1,
        task_ids=(instance.task_id,),
        effective_parallelism=1,
        reservations=(
            TaskReservation(
                instance.task_id,
                instance.idempotency_key,
                instance.budget_snapshot.to_dict(),
            ),
        ),
        state=DispatchWaveState.ADMITTED,
        execution_mode="SUPERVISED",
        packing=packing,
    )
    operation_key = spawn_operation_key(
        group.group_id,
        wave.wave_id,
        instance.task_instance_id,
        instance.attempt,
    )
    intent = {
        "event_type": "TASK_ATTEMPT_SPAWN_INTENT",
        "group_id": group.group_id,
        "wave_id": wave.wave_id,
        "task_id": instance.task_id,
        "task_instance_id": instance.task_instance_id,
        "attempt": instance.attempt,
        "operation_key": operation_key,
        "idempotency_key": operation_key,
        "budget_reservation": child_budget_reservation(
            admitted_ledger,
            instance,
            group_id=group.group_id,
            wave_id=wave.wave_id,
        ),
    }
    admission = {
        "event_type": "TASK_WAVE_ADMITTED",
        "group": group.to_dict(),
        "wave": wave.to_dict(),
        "requested_parallelism": 1,
        "effective_parallelism": 1,
        "budget_before_checksum": before_checksum,
        "budget_after_checksum": after_checksum,
        "packing_checksum": packing.packing_checksum,
        "queue_wait_ms": 0,
        "idempotency_key": wave.wave_id,
    }
    runner._record_parallel_events(request, plan, (admission, intent))
    committed = store.load_projection(plan.run_id, plan.stage_id)
    assert committed.tasks == admitted.tasks
    assert committed.logical_ready_order == admitted.logical_ready_order == ()
    assert committed.consumed_budget == admitted.consumed_budget
    return {
        "event_type": "TASK_ATTEMPT_SPAWN_CONFIRMED",
        "group_id": group.group_id,
        "wave_id": wave.wave_id,
        "task_id": instance.task_id,
        "task_instance_id": instance.task_instance_id,
        "attempt": instance.attempt,
        "operation_key": operation_key,
        "spawn_status": "SPAWN_CONFIRMED",
        "child_id": "child-1",
        "idempotency_key": operation_key,
    }


def test_identical_spawn_receipt_redelivery_is_reused() -> None:
    runner, request, plan, store = _runner_fixture()
    receipt = _seed_spawn_intent(runner, request, plan, store)

    runner._record_parallel_events(request, plan, (receipt, dict(receipt)))
    history = store.read_events(plan.run_id, plan.stage_id)

    runner._record_parallel_events(request, plan, (dict(receipt),))

    assert [event.event_type for event in history].count(
        "TASK_ATTEMPT_SPAWN_CONFIRMED"
    ) == 1
    assert store.read_events(plan.run_id, plan.stage_id) == history


@pytest.mark.parametrize(
    "changes",
    (
        {"event_type": "TASK_ATTEMPT_SPAWN_UNKNOWN", "spawn_status": "SPAWN_UNKNOWN"},
        {"child_id": "child-2"},
        {"task_id": "other-task"},
    ),
)
def test_conflicting_spawn_receipt_is_rejected_before_history_write(
    changes: Mapping[str, object],
) -> None:
    runner, request, plan, store = _runner_fixture()
    receipt = _seed_spawn_intent(runner, request, plan, store)
    runner._record_parallel_events(request, plan, (receipt,))
    history = store.read_events(plan.run_id, plan.stage_id)
    conflicting = {**receipt, **changes}
    if conflicting["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN":
        conflicting.pop("child_id")

    with pytest.raises(HarnessValidationError) as error:
        runner._record_parallel_events(request, plan, (conflicting,))

    assert error.value.code == "task_plan_event_history_conflict"
    assert store.read_events(plan.run_id, plan.stage_id) == history
