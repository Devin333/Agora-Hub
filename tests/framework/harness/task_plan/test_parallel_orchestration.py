from __future__ import annotations

import json
from threading import Barrier, Event, Lock, Thread
from time import monotonic, sleep

import pytest
from dataclasses import replace

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.activity import HarnessWorkerType
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.subagents.supervisor import ChildAgentSupervisor, ChildAgentSupervisorError
from framework.harness.task_plan import (
    PlanBuildBudget,
    PlanCandidate,
    TaskAcceptanceCriteria,
    TaskBudget,
    TaskCapabilityRegistration,
    TaskCapabilityRegistry,
    TaskLifecycle,
    TaskOutputContract,
    TaskPlanPolicy,
    TaskPlanStageIdentity,
    TaskPlanValidationContext,
    TaskPlanValidator,
    TaskResultRecord,
    TaskRetryPolicy,
    TaskSpec,
    task_instance_for_attempt,
)
from framework.harness.task_plan.parallel import (
    DispatchGroup,
    DispatchGroupState,
    DispatchWave,
    DispatchWaveState,
    DispatchWaveTerminalOutcome,
    JoinPolicy,
    ParallelAgentCoordinator,
    ParallelDispatchRequest,
    ParallelEventSink,
    ParentObservation,
    ParentObservationLimits,
    ReservationState,
    SerialTaskExecutorAdapter,
    SideEffectClass,
    TaskReservation,
)
from framework.harness.task_plan.capacity import CapacityPool, TaskCapacityDemand
from framework.shared.graph_identity import GraphExecutionIdentity
from tests.fixtures.task_plan import build_task_plan_stage_binding


class _CapabilityWorker:
    worker_type = HarnessWorkerType.LLM

    def __init__(self, capability: str) -> None:
        self.worker_id = f"{capability}-worker"
        self.worker_version = "1"

    def execute(self, _task):
        raise AssertionError("capability worker is not the parallel child worker")


def _accepted_parallel_plan(
    task_ids: tuple[str, ...] = ("task-1", "task-2", "task-3"),
):
    capabilities = tuple(f"cap-{index}" for index in range(1, len(task_ids) + 1))
    roles = tuple(f"analysis.role-{index}" for index in range(1, len(task_ids) + 1))
    policy = TaskPlanPolicy(
        policy_id="parallel.acceptance",
        version="1",
        stage_id="parallel-stage",
        allowed_worker_capabilities=capabilities,
        allowed_subagent_ids=(),
        allowed_tool_ids=("research.read",),
        allowed_memory_namespaces=("research.public",),
        allowed_input_refs=("document",),
        allowed_output_roles=roles,
        required_output_roles=roles,
        allowed_output_schema_refs=tuple(f"schema://{role}@1" for role in roles),
        allowed_gate_refs=("ResultGate@1",),
        deterministic_aggregator_refs={},
        pinned_capability_bindings={
            capability: f"{capability}-worker@1" for capability in capabilities
        },
        required_worker_contract_refs={
            capability: f"{capability}-contract@1" for capability in capabilities
        },
        max_tasks=8,
        max_depth=4,
        max_parallelism=2,
        max_replans=1,
        max_task_attempts=1,
        max_plan_build_calls=1,
        max_plan_build_turns=2,
        max_plan_build_tool_calls=0,
        per_task_budget=TaskBudget(max_turns=1, max_tool_calls=1),
        aggregate_task_budget=TaskBudget(max_turns=8, max_tool_calls=8),
    )
    stage_binding = build_task_plan_stage_binding(
        graph_id="parallel.acceptance",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
    )
    registrations = []
    tasks = []
    for task_id, capability, role in zip(task_ids, capabilities, roles, strict=True):
        worker = _CapabilityWorker(capability)
        registrations.append(
            TaskCapabilityRegistration(
                capability=capability,
                worker_binding=HarnessWorkerBinding(
                    HarnessContractReference(
                        HarnessContractKind.WORKER,
                        worker.worker_id,
                        worker.worker_version,
                    ),
                    HarnessWorkerType.LLM,
                    worker,
                ),
                worker_contract_ref=f"{capability}-contract@1",
                input_schema_ref="schema://parallel-input@1",
                output_schema_ref=f"schema://{role}@1",
            )
        )
        tasks.append(
            TaskSpec(
                task_id=task_id,
                objective=f"parallel acceptance {task_id}",
                worker_capability=capability,
                input_refs=("document",),
                output_contract=TaskOutputContract(f"schema://{role}@1", role),
                acceptance_criteria=TaskAcceptanceCriteria(("ResultGate@1",)),
                requested_tools=("research.read",),
                requested_memory_namespaces=("research.public",),
                budget_request=TaskBudget(max_turns=1, max_tool_calls=1),
                retry_policy=TaskRetryPolicy(max_attempts=1),
            )
        )
    candidate = PlanCandidate.for_stage(
        stage_identity=TaskPlanStageIdentity("parallel-run", stage_binding),
        candidate_id="parallel-candidate",
        input_context_refs=("document",),
        tasks=tuple(tasks),
        required_output_roles=roles,
        generated_by="parallel-planner@1",
        requested_plan_budget=PlanBuildBudget(max_builder_calls=1, max_turns=1),
        requested_max_parallelism=2,
    )
    return TaskPlanValidator().accept(
        candidate,
        policy,
        TaskCapabilityRegistry(registrations),
        context=TaskPlanValidationContext(
            run_id="parallel-run",
            stage_binding=stage_binding,
            available_input_refs=("document",),
            registered_gate_refs=("ResultGate@1",),
        ),
        accepted_at="2026-09-04T00:00:00Z",
    )


def _parent_identity(plan) -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
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


def _request(plan, *, join_policy: JoinPolicy = JoinPolicy.WAIT_ALL) -> ParallelDispatchRequest:
    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

    return ParallelDispatchRequest(
        plan=plan,
        budget_snapshot=TaskPlanBudgetLedger.for_plan(plan).snapshot(),
        task_instances=tuple(
            task_instance_for_attempt(plan, task.task_id, 1) for task in plan.tasks
        ),
        requested_parallelism=2,
        capability_capacity=2,
        supervisor_capacity=2,
        available_concurrency_reservations=2,
        join_policy=join_policy,
        parent_graph_identity=_parent_identity(plan),
    )


def _admitted_request(request):
    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

    ledger = TaskPlanBudgetLedger.from_snapshot(request.budget_snapshot).reserve(request.task_instances)
    return replace(request, budget_snapshot=ledger.snapshot())


def _result(plan, instance, *, status: TaskLifecycle = TaskLifecycle.SUCCEEDED) -> TaskResultRecord:
    definition = next(task for task in plan.tasks if task.task_id == instance.task_id)
    if status is TaskLifecycle.FAILED:
        return TaskResultRecord.for_plan(
            plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            status=status,
            error_code="parallel_worker_failed",
        )
    return TaskResultRecord.for_plan(
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        status=status,
        result_ref=f"result://{instance.task_id}",
        output_refs=(f"artifact://{instance.task_id}",),
        output_roles=(definition.output_role,),
        output_schema_ref=definition.task.output_contract.schema_ref,
        verified_gate_refs=definition.gate_refs,
        gate_evidence_refs=(f"evidence://{instance.task_id}",),
    )


def _observation() -> ParentObservation:
    return ParentObservation(
        run_id="run-parallel",
        stage_id="analysis-stage",
        plan_version=1,
        group_id="group-parallel",
        group_state="SUCCEEDED",
        task_summaries=tuple(
            {
                "task_id": f"task-{index}",
                "status": "succeeded",
                "summary": "bounded summary " * 200,
                "result_ref": f"artifact://task-{index}",
                "checksum": f"sha256:{index:064x}",
            }
            for index in range(8)
        ),
        wave_summaries=tuple(
            {
                "wave_id": f"wave-{index}",
                "ordinal": index + 1,
                "status": "TERMINAL",
                "task_ids": [f"task-{index}"],
            }
            for index in range(8)
        ),
        diagnostics=tuple("diagnostic " * 100 for _ in range(8)),
        refs=tuple(f"artifact://task-{index}" for index in range(8)),
        requested_parallelism=8,
        effective_parallelism=2,
    )


def test_parent_observation_projection_enforces_total_byte_limit() -> None:
    projected = _observation().project(
        ParentObservationLimits(
            max_task_summaries=8,
            max_summary_bytes=96,
            max_diagnostics=8,
            max_refs=8,
            max_observation_bytes=512,
        )
    )

    assert len(
        json.dumps(
            projected,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) <= 512
    assert projected["group_id"] == "group-parallel"
    assert projected["truncated"] is True


def test_parent_observation_rejects_limit_below_identity_envelope() -> None:
    with pytest.raises(HarnessValidationError) as exc_info:
        _observation().project(
            ParentObservationLimits(max_observation_bytes=1)
        )

    assert exc_info.value.code == "PARENT_OBSERVATION_LIMIT_TOO_SMALL"


@pytest.mark.parametrize("max_bytes", [1, 2, 3, 4, 5, 8])
def test_parent_observation_bounds_utf8_summary_and_diagnostics(max_bytes: int) -> None:
    text = "\u8bc1\u636e\u5206\u6790" * 5
    observation = replace(
        _observation(),
        task_summaries=({"task_id": "task-1", "status": "succeeded", "summary": text},),
        diagnostics=(text,),
        wave_summaries=(),
        refs=(),
    )

    projected = observation.project(ParentObservationLimits(max_summary_bytes=max_bytes))

    assert len(projected["tasks"][0]["summary"].encode("utf-8")) <= max_bytes
    assert len(projected["diagnostics"][0].encode("utf-8")) <= max_bytes
    assert projected["tasks"][0]["summary_truncated"] is True
    assert projected["truncated"] is True
    assert projected == observation.project(ParentObservationLimits(max_summary_bytes=max_bytes))


def test_production_parallel_coordinator_requires_child_supervisor() -> None:
    with pytest.raises(ValueError, match="requires ChildAgentSupervisor"):
        ParallelAgentCoordinator(max_workers=2)

    supervisor = ChildAgentSupervisor(max_children=2)
    try:
        coordinator = ParallelAgentCoordinator(
            max_workers=2,
            child_supervisor=supervisor,
        )
        assert coordinator.child_supervisor is supervisor
    finally:
        supervisor.shutdown()


def test_group_admission_append_failure_does_not_leave_ghost_session() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    supervisor = ChildAgentSupervisor(max_children=2)
    calls = 0

    def append(_event):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("admission append failed")

    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(append, lambda _events: None),
    )
    try:
        with pytest.raises(RuntimeError, match="admission append failed"):
            coordinator.create_group(request)
        assert coordinator._sessions == {}
        admitted = coordinator.create_group(request)
        assert admitted.state is DispatchGroupState.ADMITTED
        assert calls == 2
    finally:
        supervisor.shutdown()


def test_group_admission_requires_complete_accepted_plan_membership() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = replace(
        _request(plan),
        task_instances=(),
        group_task_ids=("task-1",),
    )
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        coordinator.create_group(request, check_capacity=False)

    assert exc_info.value.code == "TASK_GROUP_SCOPE_MISMATCH"
    assert coordinator._sessions == {}


def test_group_admission_rejects_policy_and_resource_binding_drift() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = replace(
        _request(plan),
        side_effect_class=SideEffectClass.FENCED_MUTATION,
        resource_conflict_key="resource-a",
    )
    changed = replace(request, resource_conflict_key="resource-b")
    events: list[dict[str, object]] = []
    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
        event_sink=ParallelEventSink(events.append, events.extend),
    )

    admitted = coordinator.create_group(request, check_capacity=False)
    proposed = coordinator._group_definition(changed)
    with pytest.raises(HarnessValidationError) as exc_info:
        coordinator.create_group(changed, check_capacity=False)

    assert proposed.group_id == admitted.group_id
    assert proposed.group_checksum != admitted.group_checksum
    assert exc_info.value.code == "TASK_GROUP_ADMISSION_CONFLICT"
    assert [event["event_type"] for event in events] == ["TASK_GROUP_ADMITTED"]
    assert coordinator._sessions[admitted.group_id].group == admitted


def test_group_admission_identity_ignores_live_capacity_availability() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    demand = {
        task.task_id: TaskCapacityDemand(task.task_id, {"cpu": 1})
        for task in plan.tasks
    }
    request = replace(
        _request(plan),
        available_concurrency_reservations=2,
        capacity_pools=(CapacityPool("cpu", 3, reserved=0, policy_version="v1"),),
        task_capacity_demands=demand,
    )
    availability_changed = replace(
        request,
        available_concurrency_reservations=1,
        capacity_pools=(CapacityPool("cpu", 3, reserved=1, policy_version="v1"),),
    )
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )

    admitted = coordinator.create_group(request, check_capacity=False)
    repeated = coordinator.create_group(availability_changed, check_capacity=False)

    assert repeated.group_id == admitted.group_id
    assert repeated.group_checksum == admitted.group_checksum
    assert repeated.admission_policy_checksum == admitted.admission_policy_checksum


def _restore_snapshots(plan, request) -> tuple[DispatchGroup, tuple[DispatchWave, ...]]:
    definition = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )._group_definition(request)
    first, second = request.task_instances
    terminal = DispatchWave(
        definition.group_id,
        1,
        (first.task_id,),
        1,
        (
            TaskReservation(
                first.task_id,
                first.idempotency_key,
                first.budget_snapshot.to_dict(),
                ReservationState.CONSUMED,
            ),
        ),
        DispatchWaveState.TERMINAL,
        terminal_outcome=DispatchWaveTerminalOutcome.SUCCEEDED,
    )
    active = DispatchWave(
        definition.group_id,
        2,
        (second.task_id,),
        1,
        (
            TaskReservation(
                second.task_id,
                second.idempotency_key,
                second.budget_snapshot.to_dict(),
                ReservationState.RESERVED,
            ),
        ),
        DispatchWaveState.ADMITTED,
    )
    return definition, (terminal, active)


def test_restore_group_preserves_terminal_and_active_waves_without_admission_event() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    group, waves = _restore_snapshots(plan, request)
    events: list[dict[str, object]] = []
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
        event_sink=ParallelEventSink(events.append, events.extend),
    )

    restored = coordinator.restore_group(request, group, waves)
    restored_again = coordinator.restore_group(request, group, waves)
    session = coordinator._sessions[group.group_id]

    assert restored == group
    assert restored_again == group
    assert events == []
    assert session.waves == list(waves)
    assert session.next_wave_ordinal == 3
    assert session.reserved == {waves[1].task_ids[0]}
    assert session.waves[0].reservations[0].state is ReservationState.CONSUMED
    assert session.waves[1].reservations[0].state is ReservationState.RESERVED


def test_restore_group_rejects_a_changed_history_instead_of_silently_ignoring_it() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    group, waves = _restore_snapshots(plan, request)
    coordinator = ParallelAgentCoordinator(max_workers=2, serial_executor=SerialTaskExecutorAdapter())
    coordinator.restore_group(request, group, waves[:1])
    with pytest.raises(HarnessValidationError, match="local admission history"):
        coordinator.restore_group(request, group, waves)
    assert coordinator._sessions[group.group_id].waves == list(waves[:1])


@pytest.mark.parametrize("drift", ("scope", "policy"))
def test_restore_group_rejects_scope_or_policy_drift(drift: str) -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    group, waves = _restore_snapshots(plan, request)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    if drift == "scope":
        group = replace(group, task_ids=("task-1",))
    else:
        request = replace(
            request,
            side_effect_class=SideEffectClass.FENCED_MUTATION,
            resource_conflict_key="restored-resource",
        )

    with pytest.raises(HarnessValidationError) as exc_info:
        coordinator.restore_group(request, group, waves)

    assert exc_info.value.code == "TASK_GROUP_ADMISSION_CONFLICT"
    assert coordinator._sessions == {}


@pytest.mark.parametrize("invalid_history", ("ordinal_gap", "two_active"))
def test_restore_group_rejects_ordinal_gap_or_multiple_active_waves(invalid_history: str) -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    group, waves = _restore_snapshots(plan, request)
    if invalid_history == "ordinal_gap":
        invalid = (waves[0], replace(waves[1], ordinal=3))
    else:
        invalid = (
            replace(
                waves[0],
                state=DispatchWaveState.ADMITTED,
                terminal_outcome=None,
            ),
            waves[1],
        )
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )

    with pytest.raises(HarnessValidationError) as exc_info:
        coordinator.restore_group(request, group, invalid)

    assert exc_info.value.code == "TASK_GROUP_RECOVERY_WAVE_INVALID"
    assert coordinator._sessions == {}


def test_concurrent_group_admission_waits_for_durable_success_before_publishing_session() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = replace(_request(plan), serial_fallback=True)
    append_started = Event()
    release_append = Event()
    events: list[dict[str, object]] = []

    def append(event):
        append_started.set()
        assert coordinator._sessions == {}
        assert release_append.wait(timeout=2)
        events.append(dict(event))

    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
        event_sink=ParallelEventSink(append, lambda _events: None),
    )
    results: list[DispatchGroup] = []
    errors: list[BaseException] = []

    def admit() -> None:
        try:
            results.append(coordinator.create_group(request))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = Thread(target=admit)
    second = Thread(target=admit)
    first.start()
    assert append_started.wait(timeout=2)
    second.start()
    sleep(0.05)
    assert coordinator._sessions == {}
    assert second.is_alive()
    release_append.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert results[0] == results[1]
    assert list(coordinator._sessions) == [results[0].group_id]
    assert [event["event_type"] for event in events] == ["TASK_GROUP_ADMITTED"]


def test_concurrent_group_admission_failure_never_publishes_a_session() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = replace(_request(plan), serial_fallback=True)
    append_started = Event()
    release_append = Event()

    def append(_event):
        append_started.set()
        assert release_append.wait(timeout=2)
        raise RuntimeError("admission append failed")

    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
        event_sink=ParallelEventSink(append, lambda _events: None),
    )
    errors: list[BaseException] = []

    def admit() -> None:
        try:
            coordinator.create_group(request)
        except BaseException as exc:
            errors.append(exc)

    first = Thread(target=admit)
    second = Thread(target=admit)
    first.start()
    assert append_started.wait(timeout=2)
    second.start()
    sleep(0.05)
    assert coordinator._sessions == {}
    assert second.is_alive()
    release_append.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert coordinator._sessions == {}
    assert coordinator._pending_admissions == {}
    assert len(errors) == 2
    assert all(isinstance(error, RuntimeError) for error in errors)


def test_serial_fallback_requires_explicit_adapter_and_preserves_group_waves() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = replace(_request(plan), serial_fallback=True)
    events: list[dict[str, object]] = []
    calls: list[str] = []
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
        event_sink=ParallelEventSink(events.append, events.extend),
    )

    def invoke(instance):
        calls.append(instance.task_id)
        return _result(plan, instance)

    dispatched = coordinator.dispatch(request, invoke)

    assert dispatched.succeeded is True
    assert calls == ["task-1"]
    assert [wave.effective_parallelism for wave in dispatched.waves] == [1]
    assert dispatched.observation.effective_parallelism == 1
    degraded = [event for event in events if event["event_type"] == "DEGRADED_SERIAL"]
    assert [event["reason_code"] for event in degraded] == ["wave_adapter_unavailable"]


def test_serial_fallback_flag_does_not_disable_healthy_supervised_parallelism() -> None:
    plan = _accepted_parallel_plan()
    request = replace(_request(plan), serial_fallback=True)
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    barrier = Barrier(2)

    def invoke(instance):
        if instance.task_id in {"task-1", "task-2"}:
            barrier.wait(timeout=2)
        return _result(plan, instance)

    try:
        dispatched = coordinator.dispatch(request, invoke)
        assert dispatched.succeeded is True
        assert dispatched.waves[0].effective_parallelism == 2
        assert not any(event["event_type"] == "DEGRADED_SERIAL" for event in events)
    finally:
        supervisor.shutdown()


def test_supervised_capacity_two_runs_three_tasks_in_two_waves_with_overlap() -> None:
    plan = _accepted_parallel_plan()
    request = _request(plan)
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    barrier = Barrier(2)
    intervals: dict[str, tuple[float, float]] = {}
    interval_lock = Lock()

    def invoke(instance):
        started = monotonic()
        if instance.task_id in {"task-1", "task-2"}:
            barrier.wait(timeout=2)
            sleep(0.03)
        finished = monotonic()
        with interval_lock:
            intervals[instance.task_id] = (started, finished)
        return _result(plan, instance)

    try:
        admitted = coordinator.create_group(request)
        assert coordinator.create_group(request) == admitted
        assert [event["event_type"] for event in events].count("TASK_GROUP_ADMITTED") == 1

        dispatched = coordinator.dispatch(request, invoke)
        joined_again = coordinator.join(request)

        assert dispatched.succeeded is True
        assert dispatched.group.group_id == admitted.group_id
        assert [wave.ordinal for wave in dispatched.waves] == [1, 2]
        assert [wave.task_ids for wave in dispatched.waves] == [
            ("task-1", "task-2"),
            ("task-3",),
        ]
        assert all(wave.state is DispatchWaveState.TERMINAL for wave in dispatched.waves)
        assert [item.task_id for item in dispatched.results] == [
            "task-1",
            "task-2",
            "task-3",
        ]
        assert max(intervals["task-1"][0], intervals["task-2"][0]) < min(
            intervals["task-1"][1], intervals["task-2"][1]
        )
        assert dispatched.dispatch_checksum == joined_again.dispatch_checksum
        assert dispatched.observation.observation_checksum == joined_again.observation.observation_checksum
    finally:
        supervisor.shutdown()


def test_reconcile_spawn_intents_reuses_confirmed_children_without_spawn() -> None:
    """A crash after receipts but before dispatch must be status-only on recovery."""
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    durable_events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(max_children=2)
    fail_once = True

    def event_sink(event):
        nonlocal fail_once
        durable_events.append(dict(event))
        if event["event_type"] == "TASK_WAVE_DISPATCHED" and fail_once:
            fail_once = False
            raise RuntimeError("simulated process crash at dispatch receipt")

    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(
            event_sink,
            lambda batch: durable_events.extend(dict(item) for item in batch),
        ),
    )

    def invoke(instance):
        return _result(plan, instance)

    try:
        with pytest.raises(RuntimeError, match="simulated process crash"):
            coordinator.dispatch(request, invoke)
        intents = tuple(
            event for event in durable_events
            if event["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT"
        )
        assert len(intents) == 2
        group_id = intents[0]["group_id"]
        session = coordinator._sessions[group_id]
        with coordinator._lock:
            # Model a lost receipt projection while retaining the supervisor's
            # authoritative child handles and the admitted wave.
            session.spawn_receipts.clear()
            admitted_wave = session.waves[0]

        def must_not_spawn(*_args, **_kwargs):
            raise AssertionError("recovery must not invoke spawn_batch")

        supervisor.spawn_batch = must_not_spawn
        recovery_events: list[dict[str, object]] = []
        restarted = ParallelAgentCoordinator(
            max_workers=2,
            child_supervisor=supervisor,
        )
        recovered = restarted.reconcile_spawn_intents(
            _admitted_request(request),
            intents,
            invoke,
            admitted_waves=(admitted_wave,),
            admitted_group=session.group,
            event_sink=recovery_events.append,
        )

        assert recovered.group.state is DispatchGroupState.RUNNING
        assert any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in recovery_events)
        assert {
            event["spawn_status"]
            for event in recovery_events
            if event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED"
        } == {"SPAWN_CONFIRMED"}
    finally:
        supervisor.shutdown()


def test_reconcile_spawn_intents_fails_closed_when_supervisor_status_is_unknown() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    durable_events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(
            durable_events.append,
            lambda batch: durable_events.extend(dict(item) for item in batch),
        ),
    )

    def fail_after_receipt(event):
        durable_events.append(dict(event))
        if event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED":
            raise RuntimeError("simulated crash after spawn receipt")

    coordinator.event_sink = ParallelEventSink(
        fail_after_receipt,
        lambda batch: durable_events.extend(dict(item) for item in batch),
    )

    try:
        with pytest.raises(RuntimeError, match="after spawn receipt"):
            coordinator.dispatch(request, lambda instance: _result(plan, instance))
        intent = next(item for item in durable_events if item["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT")
        session = coordinator._sessions[intent["group_id"]]
        admitted_wave = session.waves[0]
        handle = supervisor.status(f"parallel-{intent['task_instance_id']}", operation_id=intent["operation_key"])
        operation = supervisor.cancel(handle.child_id, operation_id=handle.operation_id, reason="test_reconcile_unknown")
        assert operation.receipt is not None and operation.receipt.termination_confirmed
        supervisor.close(handle.child_id, operation_id=handle.operation_id)
        def unknown_status(*_args, **_kwargs):
            raise ChildAgentSupervisorError("supervisor receipt unavailable", code="operation_not_found")
        supervisor.status = unknown_status

        recovery_events: list[dict[str, object]] = []
        restarted = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
        first = restarted.reconcile_spawn_intents(
            _admitted_request(request),
            (intent,),
            lambda instance: _result(plan, instance),
            admitted_waves=(admitted_wave,),
            admitted_group=session.group,
            event_sink=recovery_events.append,
        )
        assert first.group.state is DispatchGroupState.INDETERMINATE
        assert not any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in recovery_events)
        unknown_count = sum(event["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN" for event in recovery_events)
        assert unknown_count == 1

        second = restarted.reconcile_spawn_intents(
            _admitted_request(request),
            (intent,),
            lambda instance: _result(plan, instance),
            admitted_waves=(admitted_wave,),
            event_sink=recovery_events.append,
        )
        assert second.dispatch_checksum == first.dispatch_checksum
        assert sum(event["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN" for event in recovery_events) == unknown_count
    finally:
        supervisor.shutdown()


def test_fail_fast_isolates_late_child_and_releases_unstarted_wave() -> None:
    plan = _accepted_parallel_plan()
    request = _request(plan, join_policy=JoinPolicy.FAIL_FAST)
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    sibling_started = Event()
    release_sibling = Event()
    invoked: list[str] = []

    def invoke(instance):
        invoked.append(instance.task_id)
        if instance.task_id == "task-2":
            sibling_started.set()
            assert release_sibling.wait(timeout=2)
        if instance.task_id == "task-1":
            assert sibling_started.wait(timeout=2)
            return _result(plan, instance, status=TaskLifecycle.FAILED)
        return _result(plan, instance)

    try:
        with pytest.raises(HarnessValidationError) as exc_info:
            coordinator.dispatch(request, invoke)

        assert exc_info.value.code == "TASK_GROUP_INDETERMINATE"
        group = coordinator.create_group(request)
        joined = coordinator.join(request)
        assert joined.group.group_id == group.group_id
        assert joined.group.state is DispatchGroupState.INDETERMINATE
        assert "task-3" not in invoked
        assert joined.results == ()
        assert "TASK_GROUP_CANCEL_REQUESTED" in [event["event_type"] for event in events]
        assert "TASK_GROUP_INDETERMINATE" in [event["event_type"] for event in events]
        assert all(
            reservation.state is ReservationState.RELEASED
            for wave in joined.waves
            if wave.ordinal == 2
            for reservation in wave.reservations
        )
    finally:
        release_sibling.set()
        supervisor.shutdown()


def test_offline_recovery_uses_durable_results_without_live_worker_invocation() -> None:
    plan = _accepted_parallel_plan()
    request = _request(plan)
    live_worker_calls = 0

    def live_worker(_handle):
        nonlocal live_worker_calls
        live_worker_calls += 1
        raise AssertionError("offline recovery must not invoke a live worker")

    supervisor = ChildAgentSupervisor(
        max_children=2,
        worker_factory=lambda _handle: live_worker,
    )
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
    )
    durable_results = tuple(_result(plan, instance) for instance in request.task_instances)
    try:
        replayed = coordinator.recover(request, durable_results)
        joined = coordinator.join(request)

        assert replayed.group.state is DispatchGroupState.RUNNING
        assert joined.succeeded is True
        assert [item.task_id for item in joined.results] == [
            "task-1",
            "task-2",
            "task-3",
        ]
        assert live_worker_calls == 0
        assert supervisor.events.events == []
    finally:
        supervisor.shutdown()


def test_recovery_reuses_terminal_worker_result_after_parent_append_crash() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
    events: list[dict[str, object]] = []
    failed_once = True

    def sink(event):
        nonlocal failed_once
        if event["event_type"] == "TASK_WAVE_DISPATCHED" and failed_once:
            failed_once = False
            events.append(dict(event))
            raise RuntimeError("crash after child terminal result")
        events.append(dict(event))

    try:
        with pytest.raises(RuntimeError, match="crash after child terminal result"):
            coordinator.dispatch(
                request,
                lambda instance: _result(plan, instance),
                event_sink=ParallelEventSink(sink, lambda batch: events.extend(dict(item) for item in batch)),
            )
        session = next(iter(coordinator._sessions.values()))
        handle, worker = next(iter(session.active_children.values()))
        supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
        recovered = coordinator.recover(request, (), event_sink=sink)
        assert recovered.group.state is DispatchGroupState.RUNNING
        assert [item.task_id for item in recovered.results] == ["task-1"]
        assert recovered.results[0].result_checksum == _result(
            plan, request.task_instances[0]
        ).result_checksum
        assert session.active_children == {}
    finally:
        supervisor.shutdown()


def test_fresh_coordinator_recovers_embedded_terminal_task_result() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    supervisor = ChildAgentSupervisor(max_children=1)
    coordinator = ParallelAgentCoordinator(max_workers=1, child_supervisor=supervisor)
    failed_once = True

    def sink(event):
        nonlocal failed_once
        if event["event_type"] == "TASK_WAVE_DISPATCHED" and failed_once:
            failed_once = False
            raise RuntimeError("parent append interrupted")

    try:
        with pytest.raises(RuntimeError, match="parent append interrupted"):
            coordinator.dispatch(
                request,
                lambda instance: _result(plan, instance),
                event_sink=ParallelEventSink(sink, lambda _batch: None),
            )
        session = next(iter(coordinator._sessions.values()))
        handle, _worker = next(iter(session.active_children.values()))
        supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
        durable_child_events = list(supervisor.events.events)

        restored_supervisor = ChildAgentSupervisor(
            max_children=1,
            events=type(supervisor.events)(durable_child_events),
        )
        restored_handles = restored_supervisor.recover()
        restored_coordinator = ParallelAgentCoordinator(
            max_workers=1,
            child_supervisor=restored_supervisor,
        )
        restored_coordinator.create_group(
            request,
            event_sink=lambda _event: None,
            check_capacity=False,
        )
        restored_session = next(iter(restored_coordinator._sessions.values()))
        restored_session.active_children["task-1"] = (restored_handles[0], None)

        recovered = restored_coordinator.recover(request, (), event_sink=lambda _event: None)

        assert recovered.group.state is DispatchGroupState.RUNNING
        assert [item.task_id for item in recovered.results] == ["task-1"]
        assert recovered.results[0].result_checksum == _result(
            plan, request.task_instances[0]
        ).result_checksum
        assert restored_session.active_children == {}
    finally:
        supervisor.shutdown()
        if "restored_supervisor" in locals():
            restored_supervisor.shutdown()


def test_supervised_join_rejects_result_from_wrong_attempt() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    supervisor = ChildAgentSupervisor(max_children=1)
    coordinator = ParallelAgentCoordinator(max_workers=1, child_supervisor=supervisor)

    def invoke(instance):
        result = _result(plan, instance)
        return replace(result, task_instance_id="wrong-instance")

    try:
        with pytest.raises(HarnessValidationError, match="does not match its admitted attempt") as exc_info:
            coordinator.dispatch(request, invoke)
        assert exc_info.value.code == "RESULT_IDENTITY_MISMATCH"
    finally:
        supervisor.shutdown()


def test_supervised_join_rejects_result_with_wrong_binding_evidence() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    supervisor = ChildAgentSupervisor(max_children=1)
    coordinator = ParallelAgentCoordinator(max_workers=1, child_supervisor=supervisor)

    def invoke(instance):
        result = _result(plan, instance)
        return replace(result, binding_checksum="sha256:" + "0" * 64)

    try:
        with pytest.raises(HarnessValidationError, match="does not match its accepted task") as exc_info:
            coordinator.dispatch(request, invoke)
        assert exc_info.value.code == "RESULT_IDENTITY_MISMATCH"
    finally:
        supervisor.shutdown()


def test_parallel_contracts_round_trip_and_validate_derived_identity() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    group = coordinator.create_group(replace(request, serial_fallback=True))
    restored_group = DispatchGroup.from_dict(group.to_dict())
    assert restored_group == group

    reservation = TaskReservation(
        "task-1",
        "reservation-key",
        {"turns": 1},
    )
    wave = DispatchWave(
        group.group_id,
        1,
        ("task-1",),
        1,
        (reservation,),
        DispatchWaveState.ADMITTED,
    )
    assert DispatchWave.from_dict(wave.to_dict()) == wave
    terminal = wave.transitioned(DispatchWaveState.DISPATCHING).transitioned(
        DispatchWaveState.RUNNING
    ).transitioned(
        DispatchWaveState.TERMINAL,
        terminal_outcome=DispatchWaveTerminalOutcome.SUCCEEDED,
    )
    assert DispatchWave.from_dict(terminal.to_dict()) == terminal


def test_parallel_contracts_round_trip_multi_pool_policy_evidence() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    pool = CapacityPool("cpu", 1, policy_version="policy-v1")
    reservation = TaskReservation(
        "task-1",
        "reservation-key",
        {"turns": 1},
        capacity_allocations={"cpu": 1},
        capacity_policy_checksums={"cpu": pool.policy_checksum},
    )
    wave = DispatchWave(
        "group-1", 1, ("task-1",), 1, (reservation,), DispatchWaveState.ADMITTED
    )
    restored = DispatchWave.from_dict(wave.to_dict())
    assert restored == wave
    assert restored.reservations[0].capacity_policy_checksums["cpu"] == pool.policy_checksum


def test_supervised_spawn_budget_carries_versioned_reservation_identity() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    wave = DispatchWave(
        "group-1", 1, ("task-1",), 1,
        (TaskReservation("task-1", "reservation-key", {"turns": 1}),),
        DispatchWaveState.ADMITTED,
    )
    item = request.task_instances[0]
    spawn = ParallelAgentCoordinator._spawn_request(_admitted_request(request), wave, item)
    assert spawn.budget["schema_version"] == "agora.harness-budget-reservation/v1"
    assert spawn.budget["ledger_version"] == 1
    assert spawn.budget["reservation_key"] == spawn.operation_id
    assert spawn.budget["reservation_checksum"].startswith("sha256:")
    assert spawn.budget["attempt_allocation"]["turns"] == 1
    assert spawn.budget["parent_allocation"]["turns"] == plan.limits.aggregate_task_budget.max_turns


@pytest.mark.parametrize(
    "factory,field,mutator,code",
    [
        (TaskReservation.from_dict, "reservation_checksum", lambda value: "sha256:" + "0" * 64, "TASK_RESERVATION_CHECKSUM_MISMATCH"),
        (DispatchGroup.from_dict, "group_checksum", lambda value: "sha256:" + "0" * 64, "TASK_GROUP_CHECKSUM_MISMATCH"),
    ],
)
def test_parallel_contract_readback_rejects_tampered_checksums(factory, field, mutator, code) -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    coordinator = ParallelAgentCoordinator(max_workers=1, serial_executor=SerialTaskExecutorAdapter())
    group = coordinator.create_group(replace(request, serial_fallback=True))
    if field == "reservation_checksum":
        value = TaskReservation("task-1", "reservation-key", {"turns": 1}).to_dict()
    else:
        value = group.to_dict()
    value[field] = mutator(value[field])
    with pytest.raises(HarnessValidationError) as exc_info:
        factory(value)
    assert exc_info.value.code == code


def test_parallel_contract_readback_rejects_unknown_fields() -> None:
    reservation = TaskReservation("task-1", "reservation-key", {"turns": 1})
    payload = reservation.to_dict()
    payload["publication"] = True
    with pytest.raises(HarnessValidationError) as exc_info:
        TaskReservation.from_dict(payload)
    assert exc_info.value.code == "invalid_task_plan_payload_fields"


def test_parallel_replay_requires_versioned_wave_snapshot() -> None:
    from framework.harness.task_plan.replay import _normalize_parallel_wave

    reservation = TaskReservation("task-1", "reservation-key", {"turns": 1})
    wave = DispatchWave(
        "group-1", 1, ("task-1",), 1, (reservation,), DispatchWaveState.ADMITTED
    )
    payload = wave.to_dict()
    payload.pop("schema_version")
    group = {
        "group_id": "group-1",
        "task_ids": ["task-1"],
        "max_parallelism": 1,
    }
    event = type("ReplayEvent", (), {"event_type": "TASK_WAVE_ADMITTED", "sequence": 1})()
    with pytest.raises(HarnessValidationError):
        _normalize_parallel_wave(payload, group, event)


def test_parallel_contracts_reject_illegal_state_transitions_and_missing_wave_outcome() -> None:
    reservation = TaskReservation("task-1", "reservation-key", {"turns": 1})
    wave = DispatchWave("group-1", 1, ("task-1",), 1, (reservation,))
    with pytest.raises(HarnessValidationError) as exc_info:
        wave.transitioned(DispatchWaveState.RUNNING)
    assert exc_info.value.code == "TASK_WAVE_INVALID_TRANSITION"
    terminal_payload = wave.transitioned(DispatchWaveState.ADMITTED).to_dict()
    terminal_payload["state"] = DispatchWaveState.TERMINAL.value
    with pytest.raises(HarnessValidationError) as exc_info:
        DispatchWave.from_dict(terminal_payload)
    assert exc_info.value.code == "TASK_WAVE_TERMINAL_OUTCOME_REQUIRED"


@pytest.mark.parametrize("allocations", [{"cpu": 0}, {"cpu": -1}, {"cpu": True}, {"cpu": "1"}])
def test_task_reservation_rejects_invalid_capacity_allocations(allocations) -> None:
    with pytest.raises(HarnessValidationError, match="capacity allocation") as exc_info:
        TaskReservation("task-1", "reservation-key", {"turns": 1}, capacity_allocations=allocations)
    assert exc_info.value.code == "CAPACITY_RESERVATION_INVALID"
