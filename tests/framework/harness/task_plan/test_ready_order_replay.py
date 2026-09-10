"""Durable acceptance for scheduler-owned logical READY ordering."""

from dataclasses import replace

import pytest

from framework.harness.task_plan import TaskLifecycle, TaskPlanReplayReducer
from framework.harness.task_plan import capacity as capacity_module
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import thaw_mapping
from framework.harness.task_plan.capacity import pack_first_fit
from framework.harness.task_plan.models import TaskAdmissionOwner, TaskBudget
from framework.harness.task_plan.parallel import (
    DispatchWave,
    ParallelAgentCoordinator,
    SerialTaskExecutorAdapter,
    TaskReservation,
)
from framework.harness.task_plan.scheduler import (
    TaskPlanReadyDecision,
    TaskPlanScheduler,
    task_instance_for_attempt,
)
from framework.harness.task_plan.store import LogicalTaskReadiness, TaskPlanEvent
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    FIXED_NOW,
    _ArtifactStore,
    _result,
    _start,
    _store,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _request as _parallel_request,
)
from tests.framework.harness.task_plan.test_task_plan_contract_matrix import (
    _accepted_plan,
    _candidate,
    _policy,
    _task,
)


def _commit_complete_ready_order(store, plan, policy):
    scheduler = TaskPlanScheduler()
    projection = store.load_projection(plan.run_id, plan.stage_id)
    decision = scheduler.next_ready_tasks(
        projection,
        1,
        plan=plan,
        policy=policy,
        worker_capacity=1,
        available_input_refs=("document", "evidence_pack"),
    )
    target_order = decision.logical_ready_task_ids
    states = {item.task_id: item for item in projection.tasks}
    committed_ready = set(projection.logical_ready_order)
    definitions = {item.task_id: item for item in plan.tasks}
    for task_id in target_order:
        if states[task_id].status is not TaskLifecycle.PENDING:
            continue
        committed_ready.add(task_id)
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
            task_id=task_id,
            task_definition_checksum=(
                definitions[task_id].task_definition_checksum
            ),
            logical_ready_order=ordered_prefix,
        )
        sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
        projection = replace(projection, last_sequence=sequence)
        store.commit_event(
            TaskPlanEvent.for_plan(
                "TASK_READY",
                plan,
                task_id=task_id,
                input_checksum=readiness.task_definition_checksum,
                payload={"logical_readiness": readiness.to_dict()},
                sequence=sequence,
            ),
            projection,
        )
    return decision, store.load_projection(plan.run_id, plan.stage_id)


def _parallel_event(plan, event_type, sequence, payload):
    return TaskPlanEvent.for_plan(
        event_type,
        plan,
        sequence=sequence,
        payload={
            "event_type": event_type,
            "parallel_event_idempotency_key": (
                f"{event_type}:{sequence}"
            ),
            "idempotency_key": str(sequence),
            **payload,
        },
    )


def test_ready_order_and_capacity_overflow_survive_sqlite_offline_replay(
    tmp_path,
    monkeypatch,
):
    capabilities = (
        "research.seed",
        "research.priority",
        "research.depth-a",
        "research.depth-b",
        "research.low",
    )
    roles = (
        "analysis.seed",
        "analysis.priority",
        "analysis.depth-a",
        "analysis.depth-b",
        "analysis.low",
    )
    policy = replace(
        _policy(
            capabilities=capabilities,
            roles=roles,
            required_roles=roles,
        ),
        per_task_budget=TaskBudget(
            max_turns=2,
            max_tool_calls=1,
            max_output_tokens=256,
            token_limit=4_096,
            time_limit_ms=900_000,
        ),
        aggregate_task_budget=TaskBudget(
            max_turns=16,
            max_tool_calls=8,
            max_output_tokens=2_048,
            token_limit=32_768,
            time_limit_ms=7_200_000,
        ),
    )
    tasks = tuple(replace(task, budget_request=policy.per_task_budget) for task in (
        _task(
            "seed",
            capability=capabilities[0],
            role=roles[0],
            priority=0,
        ),
        _task(
            "z-priority",
            capability=capabilities[1],
            role=roles[1],
            priority=1,
        ),
        _task(
            "a-depth",
            capability=capabilities[2],
            role=roles[2],
            depends_on=("seed",),
            priority=1,
        ),
        _task(
            "b-depth",
            capability=capabilities[3],
            role=roles[3],
            depends_on=("seed",),
            priority=1,
        ),
        _task(
            "a-low",
            capability=capabilities[4],
            role=roles[4],
            priority=2,
        ),
    ))
    plan, _, _, _ = _accepted_plan(tasks, policy=policy)
    candidate = _candidate(
        tasks,
        required_roles=policy.required_output_roles,
        policy=policy,
    )
    assert candidate.candidate_checksum == plan.source_candidate_ref

    event_store = SQLiteEventStore(
        tmp_path / "ready-order.sqlite3",
        clock=lambda: FIXED_NOW,
    )
    artifacts = _ArtifactStore()
    store = _store(event_store, artifacts)
    store.append_candidate(candidate)
    store.accept_plan(plan)

    seed_instance = _start(store, plan, "seed")
    store.append_result(
        _result(
            plan,
            seed_instance,
            status=TaskLifecycle.SUCCEEDED,
            role=roles[0],
        )
    )
    before_ready = store.load_projection(plan.run_id, plan.stage_id)
    budget_before_ready = before_ready.consumed_budget
    # ``_start`` persists every initially eligible task, not only the queue
    # head. The newly eligible depth-one tasks must be merged into this
    # durable prefix by the scheduler's complete ordering rule.
    assert before_ready.logical_ready_order == ("z-priority", "a-low")
    assert all(
        next(
            item
            for item in before_ready.tasks
            if item.task_id == task_id
        ).status is TaskLifecycle.PENDING
        for task_id in ("a-depth", "b-depth")
    )
    ledger_before_ready = TaskPlanBudgetLedger.from_snapshot(
        budget_before_ready
    )
    assert set(ledger_before_ready.records) == {seed_instance.idempotency_key}
    assert (
        ledger_before_ready.records[seed_instance.idempotency_key]["status"]
        == "SETTLED"
    )

    decision, ready_projection = _commit_complete_ready_order(
        store,
        plan,
        policy,
    )
    expected_order = (
        "z-priority",
        "a-depth",
        "b-depth",
        "a-low",
    )
    assert decision.logical_ready_task_ids == expected_order
    assert ready_projection.logical_ready_order == expected_order
    ready_states = {
        item.task_id: item
        for item in ready_projection.tasks
        if item.task_id in expected_order
    }
    assert all(
        state.status is TaskLifecycle.READY
        and state.attempts == 0
        and state.active_instance_id is None
        and state.admission_owner is None
        for state in ready_states.values()
    )
    assert ready_projection.consumed_budget == budget_before_ready

    ready_instances = tuple(
        task_instance_for_attempt(plan, task_id, 1)
        for task_id in expected_order
    )
    dispatch_request = replace(
        _parallel_request(plan),
        task_instances=ready_instances,
        budget_snapshot=ready_projection.consumed_budget,
        requested_parallelism=2,
        capability_capacity=2,
        supervisor_capacity=1,
        available_concurrency_reservations=1,
        serial_fallback=True,
    )
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    effective_parallelism = coordinator.dispatch_parallelism(dispatch_request)
    assert effective_parallelism == 1
    group = coordinator.create_group(dispatch_request)
    group_initial = store.load_projection(plan.run_id, plan.stage_id)
    group_event = _parallel_event(
        plan,
        "TASK_GROUP_ADMITTED",
        group_initial.last_sequence + 1,
        {
            "group": group.to_dict(),
            "requested_parallelism": 2,
            "effective_parallelism": effective_parallelism,
            "idempotency_key": group.group_id,
        },
    )
    group_projection = replace(
        group_initial,
        last_sequence=group_event.sequence,
    )
    store.commit_event(group_event, group_projection)

    selected_instance = ready_instances[0]
    ledger_before_wave = TaskPlanBudgetLedger.from_snapshot(
        group_projection.consumed_budget
    )
    packing = pack_first_fit(
        expected_order,
        {},
        {},
        max_tasks=effective_parallelism,
        owner_scope=(
            f"{plan.run_id}/{plan.stage_id}/{plan.plan_id}"
        ),
        reservation_keys={
            item.task_id: item.idempotency_key for item in ready_instances
        },
        task_instances={item.task_id: item for item in ready_instances},
        budget_snapshot=group_projection.consumed_budget,
    )
    assert packing.selected == ("z-priority",)
    assert packing.overflow == ("a-depth", "b-depth", "a-low")
    assert packing.overflow != tuple(sorted(packing.overflow))
    admitted_projection = TaskPlanScheduler.admit_ready_tasks(
        group_projection,
        (selected_instance,),
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    assert (
        packing.admitted_budget_snapshot
        == admitted_projection.consumed_budget
    )
    ledger_after_wave = TaskPlanBudgetLedger.from_snapshot(
        admitted_projection.consumed_budget
    )
    wave = DispatchWave(
        group_id=group.group_id,
        ordinal=1,
        task_ids=packing.selected,
        effective_parallelism=effective_parallelism,
        execution_mode="SERIAL",
        reservations=(
            TaskReservation(
                selected_instance.task_id,
                selected_instance.idempotency_key,
                selected_instance.budget_snapshot.to_dict(),
            ),
        ),
        packing=packing,
        state="ADMITTED",
    )
    wave_event = _parallel_event(
        plan,
        "TASK_WAVE_ADMITTED",
        group_projection.last_sequence + 1,
        {
            "group": group.to_dict(),
            "wave": wave.to_dict(),
            "budget_before_checksum": (
                ledger_before_wave.to_dict()["ledger_checksum"]
            ),
            "budget_after_checksum": (
                ledger_after_wave.to_dict()["ledger_checksum"]
            ),
            "packing_checksum": packing.packing_checksum,
            "idempotency_key": wave.wave_id,
        },
    )
    admitted_projection = replace(
        admitted_projection,
        last_sequence=wave_event.sequence,
    )
    store.commit_events(
        (wave_event,),
        (admitted_projection,),
        expected_projection_checksum=group_projection.projection_checksum,
    )

    admitted_states = {
        item.task_id: item for item in admitted_projection.tasks
    }
    selected_state = admitted_states["z-priority"]
    assert selected_state.status is TaskLifecycle.ADMITTED
    assert selected_state.attempts == 1
    assert selected_state.active_instance_id == selected_instance.task_instance_id
    assert selected_state.admission_owner is TaskAdmissionOwner.GROUP_WAVE
    assert admitted_projection.logical_ready_order == packing.overflow
    assert all(
        admitted_states[task_id].status is TaskLifecycle.READY
        and admitted_states[task_id].attempts == 0
        and admitted_states[task_id].active_instance_id is None
        and admitted_states[task_id].admission_owner is None
        for task_id in packing.overflow
    )
    assert set(ledger_after_wave.records) == {
        seed_instance.idempotency_key,
        selected_instance.idempotency_key,
    }
    assert (
        ledger_after_wave.records[selected_instance.idempotency_key]["status"]
        == "RESERVED"
    )
    assert (
        ledger_after_wave.records[seed_instance.idempotency_key]["status"]
        == "SETTLED"
    )

    history = store.read_events(plan.run_id, plan.stage_id)
    reopened = _store(
        SQLiteEventStore(event_store.database, clock=lambda: FIXED_NOW),
        artifacts,
    )
    reopened_plan = reopened.plan(plan.run_id, plan.stage_id)
    assert reopened_plan == plan
    reopened_projection = reopened.load_projection(plan.run_id, plan.stage_id)
    assert reopened_projection == admitted_projection
    assert reopened_projection.logical_ready_order == packing.overflow
    assert (
        reopened_projection.consumed_budget
        == admitted_projection.consumed_budget
    )
    reopened_history = reopened.read_events(plan.run_id, plan.stage_id)
    assert reopened_history == history
    assert tuple(
        event.event_type
        for event in reopened_history
        if event.event_type in {"TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED"}
    ) == ("TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED")
    durable_wave_event = next(
        event
        for event in reopened_history
        if event.event_type == "TASK_WAVE_ADMITTED"
    )
    durable_wave = DispatchWave.from_dict(
        thaw_mapping(durable_wave_event.payload["wave"])
    )
    assert durable_wave.packing.selected == ("z-priority",)
    assert durable_wave.packing.overflow == packing.overflow
    assert durable_wave.packing.packing_checksum == packing.packing_checksum
    assert durable_wave_event.payload["packing_checksum"] == packing.packing_checksum

    def forbidden_live_capacity_read():
        pytest.fail("offline replay consulted live capacity time")

    monkeypatch.setattr(
        capacity_module,
        "capacity_now_ms",
        forbidden_live_capacity_read,
    )
    results = reopened.results_for(
        plan.run_id,
        plan.stage_id,
        plan.plan_id,
        plan.version,
    )
    report = TaskPlanReplayReducer().replay(
        (reopened_plan,),
        reopened.read_events(plan.run_id, plan.stage_id),
        results=results,
        require_terminal_events=False,
    )
    assert reopened.read_events(plan.run_id, plan.stage_id) == history
    assert report.projection == reopened_projection
    assert report.projection.logical_ready_order == packing.overflow
    recorded_wave = DispatchWave.from_dict(
        thaw_mapping(report.parallel_waves[wave.wave_id])
    )
    assert recorded_wave == wave
    assert recorded_wave.packing.selected == ("z-priority",)
    assert recorded_wave.packing.overflow == (
        "a-depth",
        "b-depth",
        "a-low",
    )
    assert (
        recorded_wave.packing.packing_checksum
        == packing.packing_checksum
    )
    replay_states = {
        item.task_id: item
        for item in report.projection.tasks
        if item.task_id in packing.overflow
    }
    assert all(
        state.status is TaskLifecycle.READY
        and state.attempts == 0
        and state.active_instance_id is None
        and state.admission_owner is None
        for state in replay_states.values()
    )
    replay_selected = next(
        item
        for item in report.projection.tasks
        if item.task_id == selected_instance.task_id
    )
    assert replay_selected.status is TaskLifecycle.ADMITTED
    assert replay_selected.attempts == 1
    assert (
        replay_selected.active_instance_id
        == selected_instance.task_instance_id
    )
    assert replay_selected.admission_owner is TaskAdmissionOwner.GROUP_WAVE
    replay_ledger = TaskPlanBudgetLedger.from_snapshot(
        report.projection.consumed_budget
    )
    assert all(
        task_instance_for_attempt(plan, task_id, 1).idempotency_key
        not in replay_ledger.records
        for task_id in packing.overflow
    )
    assert set(replay_ledger.records) == {
        seed_instance.idempotency_key,
        selected_instance.idempotency_key,
    }
    assert (
        replay_ledger.records[seed_instance.idempotency_key]["status"]
        == "SETTLED"
    )
    assert (
        replay_ledger.records[selected_instance.idempotency_key]["status"]
        == "RESERVED"
    )
    assert (
        report.projection.consumed_budget
        == admitted_projection.consumed_budget
    )
