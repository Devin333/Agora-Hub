"""Pure dependency-failure propagation for accepted TaskPlan projections.

This module deliberately has no store, event, worker, or coordinator dependency.
The caller owns durable event emission and commits the returned projection in the
same batch as its ``TASK_BLOCKED_UPSTREAM_FAILURE`` fact.
"""
from __future__ import annotations

from dataclasses import replace

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.dag import task_dependency_depths
from framework.harness.task_plan.task_lifecycle import DEPENDENCY_FAILURE_STATES
from framework.harness.task_plan.models import (
    ResolvedTaskSpec,
    TaskLifecycle,
    TaskPlanProjection,
    TaskProjection,
    ValidatedTaskPlan,
)


TASK_BLOCKED_UPSTREAM_FAILURE = "TASK_BLOCKED_UPSTREAM_FAILURE"


def terminal_task_failure(plan: ValidatedTaskPlan, task_projection: TaskProjection) -> bool:
    """Return whether a failed task can no longer make a policy-authorized retry.

    A retryable reason remains non-terminal only while the pinned per-task
    retry limit has not been reached. A missing reason is fail-closed and is
    therefore terminal.
    """

    definition = _definition_for_projection(plan, task_projection)
    if task_projection.status not in DEPENDENCY_FAILURE_STATES:
        return False
    if task_projection.status is not TaskLifecycle.FAILED:
        return True
    retry_policy = definition.normalized_retry_policy
    return (
        task_projection.failure_reason_code
        not in retry_policy.retryable_reason_codes
        or task_projection.attempts >= retry_policy.max_attempts
    )


def dependency_blocked_task_ids(
    plan: ValidatedTaskPlan,
    projection: TaskPlanProjection,
) -> tuple[str, ...]:
    """Return unadmitted descendants of terminal failures in stable DAG order.

    Existing ``BLOCKED_DEPENDENCY`` tasks are causal terminals, but are not
    returned a second time. This makes a crash between individual durable
    block events resumable without broadening the scope to independent tasks.
    """

    definitions, states, depths = _validated_plan_projection(plan, projection)
    reverse_dependencies = _reverse_dependencies(definitions)
    causal_roots = {
        task_id
        for task_id, state in states.items()
        if terminal_task_failure(plan, state)
        or _is_recorded_dependency_block(state)
    }
    reachable = _descendants(causal_roots, reverse_dependencies)
    return tuple(
        task_id
        for task_id in sorted(reachable, key=lambda item: (depths[item], item))
        if _is_unadmitted_for_dependency_block(states[task_id])
    )


def dependency_blocking_predecessor_ids(
    plan: ValidatedTaskPlan,
    projection: TaskPlanProjection,
    task_id: str,
) -> tuple[str, ...]:
    """Return direct causal predecessors for one dependency-block event."""

    definitions, states, _depths = _validated_plan_projection(plan, projection)
    definition = definitions.get(task_id)
    if definition is None:
        raise HarnessValidationError(
            "dependency block target is not part of the accepted plan",
            code="task_plan_unknown_task",
            details={"task_id": task_id},
        )
    return tuple(
        dependency_id
        for dependency_id in definition.depends_on
        if terminal_task_failure(plan, states[dependency_id])
        or _is_recorded_dependency_block(states[dependency_id])
    )


def block_dependency_task(
    plan: ValidatedTaskPlan,
    projection: TaskPlanProjection,
    task_id: str,
) -> TaskPlanProjection:
    """Return a projection with one unadmitted dependent task closed.

    Reapplying the same block is idempotent. A target that has already entered
    a retry, dispatch, or running lifecycle is rejected rather than being
    relabelled as a task that never entered a wave.
    """

    definitions, states, _depths = _validated_plan_projection(plan, projection)
    definition = definitions.get(task_id)
    if definition is None:
        raise HarnessValidationError(
            "dependency block target is not part of the accepted plan",
            code="task_plan_unknown_task",
            details={"task_id": task_id},
        )
    state = states[task_id]
    if _is_recorded_dependency_block(state):
        return projection
    if not _is_unadmitted_for_dependency_block(state):
        raise HarnessValidationError(
            "dependency block target has already entered an attempt lifecycle",
            code="task_plan_dependency_block_not_unadmitted",
            details={"task_id": task_id, "status": state.status.value, "attempts": state.attempts},
        )
    predecessors = dependency_blocking_predecessor_ids(plan, projection, task_id)
    if not predecessors:
        raise HarnessValidationError(
            "dependency block target has no terminal upstream failure",
            code="task_plan_dependency_block_cause_missing",
            details={"task_id": task_id},
        )

    from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger

    budget = dict(projection.consumed_budget)
    if state.status is TaskLifecycle.READY:
        budget = TaskPlanBudgetLedger.from_snapshot(budget).release_unstarted(
            state.active_instance_id, task_id, state.attempts,
            reason_code=TASK_BLOCKED_UPSTREAM_FAILURE,
        ).snapshot()
    blocked = state.transitioned(
        TaskLifecycle.BLOCKED_DEPENDENCY,
        active_instance_id=None,
        result=None,
        failure_reason_code=TASK_BLOCKED_UPSTREAM_FAILURE,
    )
    return replace(
        projection,
        tasks=tuple(
            blocked if item.task_id == task_id else item
            for item in projection.tasks
        ),
        consumed_budget=budget,
    )


def _validated_plan_projection(
    plan: ValidatedTaskPlan,
    projection: TaskPlanProjection,
) -> tuple[dict[str, ResolvedTaskSpec], dict[str, TaskProjection], dict[str, int]]:
    if not isinstance(plan, ValidatedTaskPlan) or not isinstance(projection, TaskPlanProjection):
        raise TypeError("plan and projection must be typed TaskPlan values")
    if not projection.matches_plan_identity(plan):
        raise HarnessValidationError(
            "TaskPlan projection does not match accepted plan identity",
            code="task_plan_projection_identity_mismatch",
        )
    definitions = {item.task_id: item for item in plan.tasks}
    states = {item.task_id: item for item in projection.tasks}
    if set(definitions) != set(states):
        raise HarnessValidationError(
            "TaskPlan projection task scope does not match accepted plan",
            code="task_plan_projection_incomplete",
            details={
                "missing_task_ids": sorted(set(definitions) - set(states)),
                "unexpected_task_ids": sorted(set(states) - set(definitions)),
            },
        )
    for task_id, state in states.items():
        if state.task_definition_checksum != definitions[task_id].task_definition_checksum:
            raise HarnessValidationError(
                "TaskPlan projection task definition does not match accepted plan",
                code="task_plan_task_instance_mismatch",
                details={"task_id": task_id},
            )
    depths = task_dependency_depths(
        {task_id: definition.depends_on for task_id, definition in definitions.items()}
    )
    return definitions, states, depths


def _definition_for_projection(
    plan: ValidatedTaskPlan,
    task_projection: TaskProjection,
) -> ResolvedTaskSpec:
    if not isinstance(plan, ValidatedTaskPlan) or not isinstance(task_projection, TaskProjection):
        raise TypeError("plan and task_projection must be typed TaskPlan values")
    definition = next(
        (item for item in plan.tasks if item.task_id == task_projection.task_id),
        None,
    )
    if definition is None or definition.task_definition_checksum != task_projection.task_definition_checksum:
        raise HarnessValidationError(
            "TaskPlan task projection does not match accepted plan",
            code="task_plan_task_instance_mismatch",
            details={"task_id": task_projection.task_id},
        )
    return definition


def _reverse_dependencies(
    definitions: dict[str, ResolvedTaskSpec],
) -> dict[str, tuple[str, ...]]:
    reverse: dict[str, list[str]] = {task_id: [] for task_id in definitions}
    for task_id, definition in definitions.items():
        for dependency_id in definition.depends_on:
            reverse[dependency_id].append(task_id)
    return {task_id: tuple(sorted(task_ids)) for task_id, task_ids in reverse.items()}


def _descendants(
    roots: set[str],
    reverse_dependencies: dict[str, tuple[str, ...]],
) -> set[str]:
    seen: set[str] = set()
    pending = list(sorted(roots))
    while pending:
        task_id = pending.pop(0)
        for successor_id in reverse_dependencies[task_id]:
            if successor_id in seen:
                continue
            seen.add(successor_id)
            pending.append(successor_id)
    return seen


def _is_recorded_dependency_block(state: TaskProjection) -> bool:
    return (
        state.status is TaskLifecycle.BLOCKED_DEPENDENCY
        and state.failure_reason_code == TASK_BLOCKED_UPSTREAM_FAILURE
    )


def _is_unadmitted_for_dependency_block(state: TaskProjection) -> bool:
    return (
        state.status is TaskLifecycle.PENDING and state.attempts == 0
    ) or (
        state.status is TaskLifecycle.READY and state.attempts == 1
    )


__all__ = [
    "TASK_BLOCKED_UPSTREAM_FAILURE",
    "block_dependency_task",
    "dependency_blocked_task_ids",
    "dependency_blocking_predecessor_ids",
    "terminal_task_failure",
]
