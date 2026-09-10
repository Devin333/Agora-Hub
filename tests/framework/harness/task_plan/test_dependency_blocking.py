from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.dependency import (
    TASK_BLOCKED_UPSTREAM_FAILURE,
    block_dependency_task,
    dependency_blocked_task_ids,
    dependency_blocking_predecessor_ids,
    terminal_task_failure,
)
from framework.harness.task_plan.models import (
    TaskAdmissionOwner,
    TaskLifecycle,
    TaskPlanProjection,
)
from framework.harness.task_plan.store import InMemoryTaskPlanStore
from framework.harness.task_plan.scheduler import (
    TaskPlanReadyDecision,
    TaskPlanScheduler,
    task_instance_for_attempt,
)
from tests.framework.harness.task_plan.test_task_plan_runtime import (
    _candidate,
    _setup,
    _task,
    validator_context,
)
from framework.harness.task_plan import TaskPlanValidator, TaskRetryPolicy


def _accepted_plan(*, retryable_a: bool = False):
    graph, policy, registry = _setup(
        roles=("role_a", "role_b", "role_c", "role_d"),
        capabilities=("cap_a", "cap_b", "cap_c", "cap_d"),
    )
    retry = TaskRetryPolicy(
        max_attempts=2 if retryable_a else 1,
        retryable_reason_codes=("transport",) if retryable_a else (),
    )
    candidate = _candidate(
        graph,
        (
            replace(_task("a", "cap_a", "role_a"), retry_policy=retry),
            _task("b", "cap_b", "role_b", depends_on=("a",)),
            _task("c", "cap_c", "role_c", depends_on=("b",)),
            _task("d", "cap_d", "role_d"),
        ),
        roles=("role_a", "role_b", "role_c", "role_d"),
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        context=validator_context(graph),
        accepted_at="2026-09-05T00:00:00Z",
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    return plan, store.load_projection(plan.run_id, plan.stage_id)


def _with_task(projection: TaskPlanProjection, task_id: str, **changes):
    tasks = tuple(
        replace(task, **changes) if task.task_id == task_id else task
        for task in projection.tasks
    )
    ready_task_ids = {task.task_id for task in tasks if task.status is TaskLifecycle.READY}
    logical_ready_order = tuple(
        ready_task_id
        for ready_task_id in projection.logical_ready_order
        if ready_task_id in ready_task_ids
    ) + tuple(
        task.task_id
        for task in tasks
        if task.task_id in ready_task_ids
        and task.task_id not in projection.logical_ready_order
    )
    return replace(
        projection,
        tasks=tasks,
        logical_ready_order=logical_ready_order,
    )


def _terminal_failure(projection: TaskPlanProjection, task_id: str = "a"):
    return _with_task(
        projection,
        task_id,
        status=TaskLifecycle.FAILED,
        attempts=1,
        active_instance_id=None,
        admission_owner=None,
        failure_reason_code="fatal",
    )


def test_dependency_failure_selects_only_unadmitted_transitive_descendants_in_stable_order():
    plan, projection = _accepted_plan()
    failed = _terminal_failure(projection)

    assert terminal_task_failure(plan, next(item for item in failed.tasks if item.task_id == "a"))
    assert dependency_blocked_task_ids(plan, failed) == ("b", "c")
    assert "d" not in dependency_blocked_task_ids(plan, failed)


def test_retryable_failure_below_pinned_limit_does_not_block_descendants():
    plan, projection = _accepted_plan(retryable_a=True)
    failed = _with_task(
        projection,
        "a",
        status=TaskLifecycle.FAILED,
        attempts=1,
        active_instance_id=None,
        admission_owner=None,
        failure_reason_code="transport",
    )

    state = next(item for item in failed.tasks if item.task_id == "a")
    assert terminal_task_failure(plan, state) is False
    assert dependency_blocked_task_ids(plan, failed) == ()


@pytest.mark.parametrize("status", (
    TaskLifecycle.FAILED, TaskLifecycle.CANCELLED,
    TaskLifecycle.INDETERMINATE, TaskLifecycle.QUARANTINED,
))
def test_all_terminal_failure_states_close_transitive_dependency_wait(status):
    plan, projection = _accepted_plan()
    failed = _with_task(
        projection, "a", status=status, attempts=1,
        active_instance_id=None, failure_reason_code="fatal",
    )
    assert dependency_blocked_task_ids(plan, failed) == ("b", "c")
    decision = TaskPlanScheduler().next_ready_tasks(failed, 4, plan=plan)
    assert "b" in decision.blocked_task_ids
    assert all(item.task_id not in {"a", "b", "c"} for item in decision.task_instances)
    closed = block_dependency_task(plan, block_dependency_task(plan, failed, "b"), "c")
    assert dependency_blocked_task_ids(plan, closed) == ()
    assert all(item.attempts == 0 and item.active_instance_id is None for item in closed.tasks if item.task_id in {"b", "c"})
    assert all(item.status is TaskLifecycle.BLOCKED_DEPENDENCY for item in closed.tasks if item.task_id in {"b", "c"})


def test_blocking_closure_uses_recorded_block_as_the_next_causal_predecessor():
    plan, projection = _accepted_plan()
    failed = _terminal_failure(projection)

    assert dependency_blocking_predecessor_ids(plan, failed, "b") == ("a",)
    blocked_b = block_dependency_task(plan, failed, "b")
    assert dependency_blocking_predecessor_ids(plan, blocked_b, "c") == ("b",)
    blocked_c = block_dependency_task(plan, blocked_b, "c")

    assert next(item for item in blocked_c.tasks if item.task_id == "b").status is TaskLifecycle.BLOCKED_DEPENDENCY
    assert next(item for item in blocked_c.tasks if item.task_id == "c").status is TaskLifecycle.BLOCKED_DEPENDENCY
    assert dependency_blocked_task_ids(plan, blocked_c) == ()


def test_logically_ready_dependency_block_does_not_release_unallocated_budget():
    plan, projection = _accepted_plan()
    failed = _terminal_failure(projection)
    ready = _with_task(
        failed,
        "b",
        status=TaskLifecycle.READY,
        attempts=0,
        active_instance_id=None,
        admission_owner=None,
    )
    budget_before = ready.consumed_budget

    blocked = block_dependency_task(plan, ready, "b")
    state = next(item for item in blocked.tasks if item.task_id == "b")
    assert state.status is TaskLifecycle.BLOCKED_DEPENDENCY
    assert state.active_instance_id is None
    assert state.admission_owner is None
    assert state.result is None
    assert state.failure_reason_code == TASK_BLOCKED_UPSTREAM_FAILURE
    assert blocked.logical_ready_order == ()
    assert blocked.consumed_budget == budget_before
    assert block_dependency_task(plan, blocked, "b") is blocked


@pytest.mark.parametrize(
    "status",
    (TaskLifecycle.ADMITTED, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING),
)
def test_dependency_block_rejects_tasks_with_an_active_admitted_attempt(status):
    plan, projection = _accepted_plan()
    failed = _terminal_failure(projection)
    scheduler = TaskPlanScheduler()
    ready = scheduler.reserve_ready_tasks(
        failed,
        TaskPlanReadyDecision(logical_ready_task_ids=("b",)),
    )
    instance = task_instance_for_attempt(plan, "b", 1)
    target = scheduler.admit_ready_tasks(
        ready,
        (instance,),
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    if status is TaskLifecycle.DISPATCHED:
        target = scheduler.mark_dispatched(target, instance)
    elif status is TaskLifecycle.RUNNING:
        target = scheduler.mark_started(target, instance)
    budget_before = target.consumed_budget

    with pytest.raises(HarnessValidationError) as exc_info:
        block_dependency_task(plan, target, "b")
    assert exc_info.value.code == "task_plan_dependency_block_not_unadmitted"
    assert target.consumed_budget == budget_before
    active = next(item for item in target.tasks if item.task_id == "b")
    assert active.status is status
    assert active.active_instance_id == instance.task_instance_id
    assert active.admission_owner is TaskAdmissionOwner.GROUP_WAVE


@pytest.mark.parametrize("status", (TaskLifecycle.PENDING, TaskLifecycle.READY))
def test_historical_attempt_count_does_not_turn_unallocated_task_into_active_attempt(status):
    plan, projection = _accepted_plan()
    failed = _terminal_failure(projection)
    target = _with_task(
        failed,
        "b",
        status=status,
        attempts=2,
        active_instance_id=None,
        admission_owner=None,
    )
    if status is TaskLifecycle.READY:
        target = _with_task(
            _with_task(
                target,
                "d",
                status=TaskLifecycle.READY,
                attempts=1,
                active_instance_id=None,
                admission_owner=None,
            ),
            "b",
            status=TaskLifecycle.READY,
            attempts=2,
            active_instance_id=None,
            admission_owner=None,
        )
    budget_before = target.consumed_budget
    ready_order_before = target.logical_ready_order
    assert "b" in dependency_blocked_task_ids(plan, target)

    blocked = block_dependency_task(plan, target, "b")
    state = next(item for item in blocked.tasks if item.task_id == "b")
    assert state.status is TaskLifecycle.BLOCKED_DEPENDENCY
    assert state.attempts == 2
    assert state.active_instance_id is None
    assert state.admission_owner is None
    assert blocked.consumed_budget == budget_before
    assert blocked.logical_ready_order == tuple(
        task_id for task_id in ready_order_before if task_id != "b"
    )


def test_blocked_projection_has_a_new_verifiable_checksum_without_mutating_source():
    plan, projection = _accepted_plan()
    failed = _terminal_failure(projection)
    original_checksum = failed.projection_checksum

    blocked = block_dependency_task(plan, failed, "b")

    assert blocked.projection_checksum != original_checksum
    assert TaskPlanProjection.from_dict(blocked.to_dict()).projection_checksum == blocked.projection_checksum
    assert next(item for item in failed.tasks if item.task_id == "b").status is TaskLifecycle.PENDING
