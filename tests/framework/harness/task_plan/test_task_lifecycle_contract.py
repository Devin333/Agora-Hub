from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.models import (
    TaskAdmissionOwner,
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    TaskProjection,
    TaskResultReference,
)
from framework.harness.task_plan.parallel import (
    DispatchGroup, DispatchGroupState, DispatchWave, DispatchWaveTerminalOutcome,
    ParallelAgentCoordinator, SerialTaskExecutorAdapter, TaskReservation,
)
from framework.harness.task_plan.parallel_state import validate_group_transition, validate_wave_transition
from framework.harness.task_plan.replay import _projection_for_plan
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler, task_instance_for_attempt
from framework.harness.task_plan.task_lifecycle import validate_task_transition
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _packing,
    _request,
)


OUTCOMES = {"succeeded", "failed", "cancelled", "indeterminate", "quarantined"}
SUCCESSORS = {
    "pending": {"ready", "skipped", "blocked", "blocked_dependency", "cancelled"},
    "ready": {"admitted", "skipped", "blocked", "blocked_dependency", "cancelled"},
    "admitted": OUTCOMES | {"dispatched", "running"},
    "dispatched": OUTCOMES | {"running", "ready"},
    "running": OUTCOMES | {"ready"},
    "failed": {"pending"},
    **{state: set() for state in ("succeeded", "cancelled", "indeterminate", "quarantined", "skipped", "blocked", "blocked_dependency")},
}


@pytest.mark.parametrize("source", tuple(TaskLifecycle))
@pytest.mark.parametrize("target", tuple(TaskLifecycle))
def test_canonical_task_transition_matrix(source, target):
    assert {state.value for state in TaskLifecycle} == set(SUCCESSORS)
    if source is target or target.value in SUCCESSORS[source.value]:
        validate_task_transition(source, target)
    else:
        with pytest.raises(HarnessValidationError) as error:
            validate_task_transition(source, target)
        assert error.value.code == "task_plan_invalid_task_transition"


@pytest.mark.parametrize("state", ("REPLAN_PENDING", "unknown", None))
def test_unknown_task_state_is_typed_failure(state):
    with pytest.raises(HarnessValidationError):
        validate_task_transition("pending", state)


def _projection(
    status,
    *,
    attempts=1,
    active_instance_id=None,
    admission_owner=None,
    result=None,
):
    return TaskProjection(
        "task-1", canonical_payload_checksum({"task": "task-1"}), status,
        attempts=attempts,
        active_instance_id=active_instance_id,
        admission_owner=admission_owner,
        result=result,
    )


@pytest.mark.parametrize("status", tuple(TaskLifecycle))
def test_all_canonical_task_states_have_versioned_checksum_roundtrip(status):
    result = TaskResultReference(
        "result://one", canonical_payload_checksum({"result": 1}), "analysis.structure", "schema://analysis@1",
    ) if status is TaskLifecycle.SUCCEEDED else None
    active = "ti_one" if status in {TaskLifecycle.ADMITTED, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING} else None
    admission_owner = TaskAdmissionOwner.QUEUE if active else None
    original = _projection(
        status,
        active_instance_id=active,
        admission_owner=admission_owner,
        result=result,
    )
    assert original.schema_version == "newsroom.harness-task-projection/v4"
    assert TaskProjection.from_dict(original.to_dict()) == original
    tampered = {**original.to_dict(), "attempts": 2}
    with pytest.raises(HarnessValidationError):
        TaskProjection.from_dict(tampered)
    with pytest.raises(HarnessValidationError):
        TaskProjection.from_dict({**original.to_dict(), "schema_version": "newsroom.harness-task-projection/v3"})


def test_allocated_ready_v3_is_not_silently_reinterpreted_as_logical_ready():
    legacy = {
        "schema_version": "newsroom.harness-task-projection/v3",
        "task_id": "task-1",
        "task_definition_checksum": canonical_payload_checksum({"task": "task-1"}),
        "status": "ready",
        "attempts": 1,
        "active_instance_id": "ti_one",
        "result": None,
        "failure_reason_code": None,
    }
    legacy["projection_checksum"] = canonical_payload_checksum(legacy)
    with pytest.raises(HarnessValidationError) as error:
        TaskProjection.from_dict(legacy)
    assert error.value.code == "unsupported_task_plan_schema"


def test_v4_task_projection_requires_explicit_admission_owner_wire_field():
    payload = _projection("ready", attempts=0).to_dict()
    payload.pop("admission_owner")
    with pytest.raises(HarnessValidationError):
        TaskProjection.from_dict(payload)


@pytest.mark.parametrize("status,attempts,active", (
    ("running", 0, "ti_one"), ("admitted", 1, None), ("dispatched", 1, None),
    ("pending", 1, "ti_one"), ("blocked_dependency", 1, "ti_one"),
    ("indeterminate", 0, None), ("quarantined", 0, None),
    ("ready", 0, "ti_one"), ("ready", 1, "ti_one"), ("cancelled", 1, "ti_one"),
    ("indeterminate", 1, "ti_one"), ("quarantined", 1, "ti_one"),
))
def test_projection_rejects_incoherent_attempt_state(status, attempts, active):
    with pytest.raises(HarnessValidationError) as error:
        _projection(
            status,
            attempts=attempts,
            active_instance_id=active,
            admission_owner="QUEUE" if active else None,
        )
    assert error.value.code == "invalid_task_projection"


@pytest.mark.parametrize("status", ("admitted", "dispatched", "running"))
def test_active_attempt_requires_durable_admission_owner(status):
    with pytest.raises(HarnessValidationError) as error:
        _projection(status, active_instance_id="ti_one")
    assert error.value.code == "invalid_task_projection"
    with pytest.raises(HarnessValidationError) as error:
        _projection(
            status,
            active_instance_id="ti_one",
            admission_owner="CURRENT_COORDINATOR",
        )
    assert error.value.code == "invalid_task_projection"


def test_logical_ready_allocates_no_attempt_and_admission_allocates_exactly_one():
    ready = _projection("ready", attempts=2)
    assert ready.active_instance_id is None
    assert ready.admission_owner is None

    with pytest.raises(HarnessValidationError) as error:
        ready.transitioned(
            "admitted",
            attempts=2,
            active_instance_id="ti_three",
            admission_owner="QUEUE",
        )
    assert error.value.code == "invalid_task_projection"

    admitted = ready.transitioned(
        "admitted",
        attempts=3,
        active_instance_id="ti_three",
        admission_owner="QUEUE",
    )
    assert admitted.attempts == 3
    assert admitted.admission_owner is TaskAdmissionOwner.QUEUE
    dispatched = admitted.transitioned("dispatched")
    assert dispatched.active_instance_id == admitted.active_instance_id
    assert dispatched.admission_owner is TaskAdmissionOwner.QUEUE


def test_ready_cannot_skip_admission_or_carry_execution_owner():
    ready = _projection("ready", attempts=1)
    with pytest.raises(HarnessValidationError):
        ready.transitioned(
            "dispatched",
            attempts=2,
            active_instance_id="ti_two",
            admission_owner="GROUP_WAVE",
        )
    with pytest.raises(HarnessValidationError):
        _projection("ready", admission_owner="GROUP_WAVE")


def test_same_admission_identity_is_idempotent_and_owner_cannot_be_rewritten():
    admitted = _projection(
        "admitted",
        attempts=1,
        active_instance_id="ti_one",
        admission_owner="GROUP_WAVE",
    )
    assert admitted.transitioned("admitted") == admitted
    with pytest.raises(HarnessValidationError) as error:
        admitted.transitioned("admitted", admission_owner="QUEUE")
    assert error.value.code == "invalid_task_projection"


def test_terminal_projection_cannot_be_reopened_or_have_evidence_rewritten():
    quarantined = _projection("quarantined")
    assert quarantined.transitioned("quarantined") == quarantined
    for status in ("ready", "pending", "running"):
        with pytest.raises(HarnessValidationError):
            quarantined.transitioned(status)
    with pytest.raises(HarnessValidationError):
        quarantined.transitioned("quarantined", failure_reason_code="different-receipt")
    with pytest.raises(HarnessValidationError):
        quarantined.transitioned("quarantined", task_id="other")


@pytest.mark.parametrize("field,value", (
    ("task_instance_id", "ti_forged"), ("idempotency_key", "idem_forged"),
    ("fencing_token", "fence_forged"), ("run_id", "other-run"),
    ("stage_id", "other-stage"), ("plan_id", "other-plan"), ("plan_version", 2),
    ("plan_checksum", "sha256:" + "0" * 64), ("task_id", "other-task"),
    ("task_definition_checksum", "sha256:" + "0" * 64), ("attempt", 2),
))
def test_attempt_model_rejects_identity_substitution_even_with_recomputed_checksum(field, value):
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    with pytest.raises(HarnessValidationError) as error:
        replace(instance, **{field: value})
    expected_code = "task_plan_stage_identity_checksum_invalid" if field in {"run_id", "stage_id"} else "task_plan_task_instance_mismatch"
    assert error.value.code == expected_code
    payload = {**instance.checksum_projection(), field: value}
    payload["instance_checksum"] = canonical_payload_checksum(payload)
    with pytest.raises(HarnessValidationError) as error:
        TaskInstance.from_dict(payload)
    assert error.value.code == expected_code


def test_admission_is_stable_and_not_a_fresh_ready_or_reclaimable_queue_attempt():
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    scheduler = TaskPlanScheduler()
    initial = _projection_for_plan(plan, sequence=1)
    ready = scheduler.reserve_ready_tasks(
        initial,
        TaskPlanReadyDecision(logical_ready_task_ids=(instance.task_id,)),
    )
    assert ready.tasks[0].status is TaskLifecycle.READY
    assert ready.tasks[0].attempts == 0
    assert ready.tasks[0].active_instance_id is None
    assert ready.tasks[0].admission_owner is None
    assert ready.consumed_budget == initial.consumed_budget

    admitted = scheduler.mark_admitted(
        ready,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    assert admitted.tasks[0].status is TaskLifecycle.ADMITTED
    assert admitted.tasks[0].attempts == 1
    assert admitted.tasks[0].active_instance_id == instance.task_instance_id
    assert admitted.tasks[0].admission_owner is TaskAdmissionOwner.QUEUE
    assert admitted.consumed_budget != ready.consumed_budget
    assert admitted.consumed_budget["ledger"]["ledger_version"] == 1
    assert tuple(admitted.consumed_budget["ledger"]["records"]) == (
        instance.idempotency_key,
    )
    ledger_before_redelivery = admitted.consumed_budget
    redelivered = scheduler.mark_admitted(
        admitted,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    assert redelivered is admitted
    assert redelivered.consumed_budget == ledger_before_redelivery
    assert redelivered.consumed_budget["ledger"]["ledger_version"] == 1
    assert scheduler.next_ready_tasks(admitted, 1, plan=plan).logical_ready_task_ids == ()
    with pytest.raises(HarnessValidationError):
        scheduler.reclaim_stale(admitted, "task-1", plan=plan)
    with pytest.raises(HarnessValidationError):
        scheduler.mark_admitted(
            admitted,
            task_instance_for_attempt(plan, "task-1", 2),
            admission_owner=TaskAdmissionOwner.QUEUE,
        )
    dispatched = scheduler.mark_dispatched(admitted, instance)
    started = scheduler.mark_started(dispatched, instance)
    assert started.tasks[0].status is TaskLifecycle.RUNNING
    assert TaskInstance.from_dict(instance.to_dict()) == instance
    retry = task_instance_for_attempt(plan, "task-1", 2)
    assert retry.task_instance_id != instance.task_instance_id
    assert retry.idempotency_key != instance.idempotency_key
    assert retry.fencing_token != instance.fencing_token


@pytest.mark.parametrize("payload", ("worker_ref", "budget_snapshot"))
def test_admission_redelivery_rejects_same_attempt_id_with_different_payload(payload):
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    scheduler = TaskPlanScheduler()
    ready = scheduler.reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1),
        TaskPlanReadyDecision(logical_ready_task_ids=(instance.task_id,)),
    )
    admitted = scheduler.mark_admitted(
        ready,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    conflicting = (
        replace(instance, worker_ref="different-worker@1")
        if payload == "worker_ref"
        else replace(
            instance,
            budget_snapshot=replace(
                instance.budget_snapshot,
                max_turns=instance.budget_snapshot.max_turns + 1,
            ),
        )
    )
    ledger_before = admitted.consumed_budget

    with pytest.raises(HarnessValidationError) as error:
        scheduler.mark_admitted(
            admitted,
            conflicting,
            admission_owner=TaskAdmissionOwner.QUEUE,
        )

    assert error.value.code == "task_plan_budget_identity_conflict"
    assert admitted.consumed_budget == ledger_before


def test_admission_redelivery_rejects_missing_budget_reservation_without_inserting_it():
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    scheduler = TaskPlanScheduler()
    ready = scheduler.reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1),
        TaskPlanReadyDecision(logical_ready_task_ids=(instance.task_id,)),
    )
    admitted = scheduler.mark_admitted(
        ready,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    missing = replace(admitted, consumed_budget=ready.consumed_budget)
    ledger_before = missing.consumed_budget

    with pytest.raises(HarnessValidationError) as error:
        scheduler.mark_admitted(
            missing,
            instance,
            admission_owner=TaskAdmissionOwner.QUEUE,
        )

    assert error.value.code == "task_plan_budget_reservation_missing"
    assert missing.consumed_budget == ledger_before
    assert missing.consumed_budget["ledger"]["records"] == {}


def test_admission_redelivery_rejects_terminal_budget_reservation():
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    scheduler = TaskPlanScheduler()
    ready = scheduler.reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1),
        TaskPlanReadyDecision(logical_ready_task_ids=(instance.task_id,)),
    )
    admitted = scheduler.mark_admitted(
        ready,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    terminal_ledger = TaskPlanBudgetLedger.from_snapshot(
        admitted.consumed_budget
    ).release_unstarted(
        instance.task_instance_id,
        instance.task_id,
        instance.attempt,
        reason_code="cancelled_before_dispatch",
    )
    terminal = replace(admitted, consumed_budget=terminal_ledger.snapshot())
    ledger_before = terminal.consumed_budget

    with pytest.raises(HarnessValidationError) as error:
        scheduler.mark_admitted(
            terminal,
            instance,
            admission_owner=TaskAdmissionOwner.QUEUE,
        )

    assert error.value.code == "task_plan_budget_identity_conflict"
    assert terminal.consumed_budget == ledger_before
    assert terminal.consumed_budget["ledger"]["records"][
        instance.idempotency_key
    ]["status"] == "RELEASED"


def test_nested_task_plan_and_attempt_schema_versions_fail_closed_on_old_contract():
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    projection = _projection_for_plan(plan, sequence=1)
    assert instance.schema_version == "newsroom.harness-task-instance/v3"
    assert projection.schema_version == "newsroom.harness-task-plan-projection/v4"
    assert TaskPlanProjection.from_dict(projection.to_dict()) == projection
    old_instance = {**instance.checksum_projection(), "schema_version": "newsroom.harness-task-instance/v2"}
    old_instance["instance_checksum"] = canonical_payload_checksum(old_instance)
    with pytest.raises(HarnessValidationError) as error:
        TaskInstance.from_dict(old_instance)
    assert error.value.code == "unsupported_task_plan_schema"
    old_projection = projection.checksum_projection()
    old_projection["schema_version"] = "newsroom.harness-task-plan-projection/v3"
    for task in old_projection["tasks"]:
        task.pop("projection_checksum")
        task["schema_version"] = "newsroom.harness-task-projection/v3"
        task["projection_checksum"] = canonical_payload_checksum(task)
    old_projection["projection_checksum"] = canonical_payload_checksum(old_projection)
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanProjection.from_dict(old_projection)
    assert error.value.code == "unsupported_task_plan_schema"


def test_plan_projection_persists_complete_logical_ready_order():
    plan = _accepted_parallel_plan(("task-a", "task-b"))
    initial = _projection_for_plan(plan, sequence=1)
    ready_tasks = tuple(
        item.transitioned(TaskLifecycle.READY)
        for item in initial.tasks
    )
    projection = replace(
        initial,
        tasks=ready_tasks,
        logical_ready_order=("task-b", "task-a"),
    )
    assert projection.logical_ready_order == ("task-b", "task-a")
    assert TaskPlanProjection.from_dict(projection.to_dict()) == projection

    tampered_order = projection.to_dict()
    tampered_order["logical_ready_order"] = ["task-a", "task-b"]
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanProjection.from_dict(tampered_order)
    assert error.value.code == "task_plan_checksum_mismatch"

    with pytest.raises(HarnessValidationError) as error:
        replace(projection, logical_ready_order=("task-a",))
    assert error.value.code == "task_plan_projection_ready_order_mismatch"
    with pytest.raises(HarnessValidationError) as error:
        replace(projection, logical_ready_order=("task-a", "task-a"))
    assert error.value.code == "task_plan_projection_ready_order_mismatch"

    missing_ready_order = projection.to_dict()
    missing_ready_order.pop("logical_ready_order")
    with pytest.raises(HarnessValidationError):
        TaskPlanProjection.from_dict(missing_ready_order)


def _logical_ready_projection():
    plan = _accepted_parallel_plan(("a", "b"))
    initial = _projection_for_plan(plan, sequence=1)
    return replace(
        initial,
        tasks=tuple(
            item.transitioned(TaskLifecycle.READY)
            for item in initial.tasks
        ),
        logical_ready_order=("a", "b"),
    )


@pytest.mark.parametrize(
    "invalid_order",
    (
        "ab",
        b"ab",
        {"a": None, "b": None},
        {"a", "b"},
        frozenset({"a", "b"}),
        None,
        2,
        2.5,
    ),
)
def test_plan_projection_constructor_rejects_non_ordered_ready_sequence(invalid_order):
    projection = _logical_ready_projection()
    with pytest.raises(HarnessValidationError) as error:
        replace(projection, logical_ready_order=invalid_order)
    assert error.value.code == "invalid_task_plan_payload"


@pytest.mark.parametrize(
    "invalid_order",
    (
        "ab",
        b"ab",
        {"a": None, "b": None},
        {"a", "b"},
        frozenset({"a", "b"}),
        None,
        2,
        2.5,
    ),
)
def test_plan_projection_wire_rejects_non_ordered_ready_sequence(invalid_order):
    payload = _logical_ready_projection().to_dict()
    payload["logical_ready_order"] = invalid_order
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanProjection.from_dict(payload)
    assert error.value.code == "invalid_task_plan_payload"


@pytest.mark.parametrize("ready_order", (["a", "b"], ("a", "b")))
def test_plan_projection_accepts_ordered_list_and_tuple_roundtrip(ready_order):
    projection = replace(
        _logical_ready_projection(),
        logical_ready_order=ready_order,
    )
    assert projection.logical_ready_order == ("a", "b")
    payload = projection.to_dict()
    payload["logical_ready_order"] = ready_order
    assert TaskPlanProjection.from_dict(payload) == projection


@pytest.mark.parametrize("target", tuple(DispatchGroupState))
def test_replan_pending_is_nonterminal_with_only_policy_completion_successors(target):
    plan = _accepted_parallel_plan(("task-1",))
    coordinator = ParallelAgentCoordinator(max_workers=1, serial_executor=SerialTaskExecutorAdapter())
    group = coordinator.create_group(replace(_request(plan), serial_fallback=True))
    pending = group.transitioned("JOINING").transitioned("REPLAN_PENDING")
    assert pending.group_id == group.group_id
    assert pending.group_checksum == group.group_checksum
    assert canonical_payload_checksum(pending.to_dict()) != canonical_payload_checksum(group.to_dict())
    assert DispatchGroup.from_dict(pending.to_dict()) == pending
    if target.value in {"REPLAN_PENDING", "SUPERSEDED", "FAILED", "HALTED"}:
        updated = pending.transitioned(target)
        validate_group_transition(pending.state, target)
        assert updated.group_id == pending.group_id
        assert DispatchGroup.from_dict(updated.to_dict()) == updated
    else:
        with pytest.raises(HarnessValidationError) as error:
            pending.transitioned(target)
        assert error.value.code == "TASK_GROUP_INVALID_TRANSITION"
        with pytest.raises(HarnessValidationError):
            validate_group_transition(pending.state, target)


@pytest.mark.parametrize("outcome", tuple(DispatchWaveTerminalOutcome))
def test_every_wave_outcome_roundtrips_without_changing_physical_admission_identity(outcome):
    wave = DispatchWave(
        "group-1", 1, ("task-1",), 1,
        (TaskReservation("task-1", "reservation-1", {"turns": 1}),),
        packing=_packing("task-1"),
        state="ADMITTED",
    )
    terminal = wave.transitioned("TERMINAL", terminal_outcome=outcome)
    assert terminal.wave_id == wave.wave_id
    assert canonical_payload_checksum(terminal.to_dict()) != canonical_payload_checksum(wave.to_dict())
    assert DispatchWave.from_dict(terminal.to_dict()) == terminal
    assert terminal.transitioned("TERMINAL", terminal_outcome=outcome) == terminal
    other = "FAILED" if outcome.value != "FAILED" else "SUCCEEDED"
    with pytest.raises(HarnessValidationError):
        terminal.transitioned("TERMINAL", terminal_outcome=other)
    with pytest.raises(HarnessValidationError):
        terminal.transitioned("ADMITTED")


@pytest.mark.parametrize("validator,code", (
    (validate_group_transition, "TASK_GROUP_INVALID_TRANSITION"),
    (validate_wave_transition, "TASK_WAVE_INVALID_TRANSITION"),
))
@pytest.mark.parametrize("state", (None, "unknown", "admitted"))
def test_unknown_parallel_state_fails_with_typed_contract_error(validator, code, state):
    with pytest.raises(HarnessValidationError) as error:
        validator("ADMITTED", state)
    assert error.value.code == code
