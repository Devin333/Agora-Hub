from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.models import TaskInstance, TaskLifecycle, TaskPlanProjection, TaskProjection, TaskResultReference
from framework.harness.task_plan.parallel import (
    DispatchGroup, DispatchGroupState, DispatchWave, DispatchWaveTerminalOutcome,
    ParallelAgentCoordinator, SerialTaskExecutorAdapter, TaskReservation,
)
from framework.harness.task_plan.parallel_state import validate_group_transition, validate_wave_transition
from framework.harness.task_plan.replay import _projection_for_plan
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler, task_instance_for_attempt
from framework.harness.task_plan.task_lifecycle import validate_task_transition
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request


OUTCOMES = {"succeeded", "failed", "cancelled", "indeterminate", "quarantined"}
SUCCESSORS = {
    "pending": {"ready", "skipped", "blocked", "blocked_dependency", "cancelled"},
    "ready": OUTCOMES | {"admitted", "dispatched", "skipped", "blocked", "blocked_dependency"},
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


def _projection(status, *, attempts=1, active_instance_id=None, result=None):
    return TaskProjection(
        "task-1", canonical_payload_checksum({"task": "task-1"}), status,
        attempts=attempts, active_instance_id=active_instance_id, result=result,
    )


@pytest.mark.parametrize("status", tuple(TaskLifecycle))
def test_all_canonical_task_states_have_versioned_checksum_roundtrip(status):
    result = TaskResultReference(
        "result://one", canonical_payload_checksum({"result": 1}), "analysis.structure", "schema://analysis@1",
    ) if status is TaskLifecycle.SUCCEEDED else None
    active = "ti_one" if status in {TaskLifecycle.READY, TaskLifecycle.ADMITTED, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING} else None
    original = _projection(status, active_instance_id=active, result=result)
    assert original.schema_version == "newsroom.harness-task-projection/v3"
    assert TaskProjection.from_dict(original.to_dict()) == original
    tampered = {**original.to_dict(), "attempts": 2}
    with pytest.raises(HarnessValidationError):
        TaskProjection.from_dict(tampered)
    with pytest.raises(HarnessValidationError):
        TaskProjection.from_dict({**original.to_dict(), "schema_version": "newsroom.harness-task-projection/v2"})


@pytest.mark.parametrize("status,attempts,active", (
    ("running", 0, "ti_one"), ("admitted", 1, None), ("dispatched", 1, None),
    ("pending", 1, "ti_one"), ("blocked_dependency", 1, "ti_one"),
    ("indeterminate", 0, None), ("quarantined", 0, None),
    ("ready", 0, None), ("ready", 1, None), ("cancelled", 1, "ti_one"),
    ("indeterminate", 1, "ti_one"), ("quarantined", 1, "ti_one"),
))
def test_projection_rejects_incoherent_attempt_state(status, attempts, active):
    with pytest.raises(HarnessValidationError) as error:
        _projection(status, attempts=attempts, active_instance_id=active)
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
    ready = scheduler.reserve_ready_tasks(_projection_for_plan(plan, sequence=1), TaskPlanReadyDecision((instance,)))
    admitted = scheduler.mark_admitted(ready, instance)
    assert admitted.tasks[0].status is TaskLifecycle.ADMITTED
    assert admitted.tasks[0].attempts == 1
    assert admitted.consumed_budget == ready.consumed_budget
    assert scheduler.mark_admitted(admitted, instance) == admitted
    assert scheduler.next_ready_tasks(admitted, 1, plan=plan).task_instances == ()
    with pytest.raises(HarnessValidationError):
        scheduler.reclaim_stale(admitted, "task-1")
    with pytest.raises(HarnessValidationError):
        scheduler.mark_admitted(admitted, task_instance_for_attempt(plan, "task-1", 2))
    dispatched = scheduler.mark_dispatched(admitted, instance)
    started = scheduler.mark_started(dispatched, instance)
    assert started.tasks[0].status is TaskLifecycle.RUNNING
    assert TaskInstance.from_dict(instance.to_dict()) == instance
    retry = task_instance_for_attempt(plan, "task-1", 2)
    assert retry.task_instance_id != instance.task_instance_id
    assert retry.idempotency_key != instance.idempotency_key
    assert retry.fencing_token != instance.fencing_token


def test_nested_task_plan_and_attempt_schema_versions_fail_closed_on_old_contract():
    plan = _accepted_parallel_plan(("task-1",))
    instance = task_instance_for_attempt(plan, "task-1", 1)
    projection = TaskPlanScheduler().reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1), TaskPlanReadyDecision((instance,)),
    )
    assert instance.schema_version == "newsroom.harness-task-instance/v3"
    assert projection.schema_version == "newsroom.harness-task-plan-projection/v3"
    assert TaskPlanProjection.from_dict(projection.to_dict()) == projection
    old_instance = {**instance.checksum_projection(), "schema_version": "newsroom.harness-task-instance/v2"}
    old_instance["instance_checksum"] = canonical_payload_checksum(old_instance)
    with pytest.raises(HarnessValidationError) as error:
        TaskInstance.from_dict(old_instance)
    assert error.value.code == "unsupported_task_plan_schema"
    old_projection = projection.checksum_projection()
    old_projection["schema_version"] = "newsroom.harness-task-plan-projection/v2"
    for task in old_projection["tasks"]:
        task.pop("projection_checksum")
        task["schema_version"] = "newsroom.harness-task-projection/v2"
        task["projection_checksum"] = canonical_payload_checksum(task)
    old_projection["projection_checksum"] = canonical_payload_checksum(old_projection)
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanProjection.from_dict(old_projection)
    assert error.value.code == "unsupported_task_plan_schema"


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
