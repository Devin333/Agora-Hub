"""Canonical task lifecycle shared by execution and offline replay."""
from __future__ import annotations

from enum import StrEnum

from framework.harness.control_plane.errors import HarnessValidationError


class TaskLifecycle(StrEnum):
    PENDING = "pending"
    READY = "ready"
    ADMITTED = "admitted"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INDETERMINATE = "indeterminate"
    QUARANTINED = "quarantined"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    BLOCKED_DEPENDENCY = "blocked_dependency"


ACTIVE_TASK_STATES = frozenset({
    TaskLifecycle.READY, TaskLifecycle.ADMITTED, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING,
})

DEPENDENCY_FAILURE_STATES = frozenset({
    TaskLifecycle.FAILED, TaskLifecycle.CANCELLED, TaskLifecycle.INDETERMINATE,
    TaskLifecycle.QUARANTINED, TaskLifecycle.BLOCKED, TaskLifecycle.BLOCKED_DEPENDENCY,
    TaskLifecycle.SKIPPED,
})

_ATTEMPT_OUTCOMES = frozenset({
    TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED, TaskLifecycle.CANCELLED,
    TaskLifecycle.INDETERMINATE, TaskLifecycle.QUARANTINED,
})
_TASK_TRANSITIONS = {
    TaskLifecycle.PENDING: frozenset({
        TaskLifecycle.READY, TaskLifecycle.SKIPPED, TaskLifecycle.BLOCKED,
        TaskLifecycle.BLOCKED_DEPENDENCY, TaskLifecycle.CANCELLED,
    }),
    TaskLifecycle.READY: _ATTEMPT_OUTCOMES | frozenset({
        TaskLifecycle.ADMITTED, TaskLifecycle.DISPATCHED, TaskLifecycle.SKIPPED,
        TaskLifecycle.BLOCKED, TaskLifecycle.BLOCKED_DEPENDENCY,
    }),
    TaskLifecycle.ADMITTED: _ATTEMPT_OUTCOMES | frozenset({
        TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING,
    }),
    TaskLifecycle.DISPATCHED: _ATTEMPT_OUTCOMES | frozenset({
        TaskLifecycle.RUNNING, TaskLifecycle.READY,
    }),
    TaskLifecycle.RUNNING: _ATTEMPT_OUTCOMES | frozenset({TaskLifecycle.READY}),
    # The retry owner must validate policy and budget before resetting the task.
    TaskLifecycle.FAILED: frozenset({TaskLifecycle.PENDING}),
    TaskLifecycle.SUCCEEDED: frozenset(),
    TaskLifecycle.CANCELLED: frozenset(),
    TaskLifecycle.INDETERMINATE: frozenset(),
    TaskLifecycle.QUARANTINED: frozenset(),
    TaskLifecycle.SKIPPED: frozenset(),
    TaskLifecycle.BLOCKED: frozenset(),
    TaskLifecycle.BLOCKED_DEPENDENCY: frozenset(),
}


def validate_task_transition(
    current: TaskLifecycle | str,
    target: TaskLifecycle | str,
    *,
    code: str = "task_plan_invalid_task_transition",
) -> None:
    try:
        source, destination = TaskLifecycle(current), TaskLifecycle(target)
    except (TypeError, ValueError) as exc:
        raise HarnessValidationError("unknown TaskPlan task state", code=code) from exc
    if destination is source:
        return
    if destination not in _TASK_TRANSITIONS[source]:
        raise HarnessValidationError(
            "invalid TaskPlan task state transition", code=code,
            details={"from": source.value, "to": destination.value},
        )


__all__ = ["ACTIVE_TASK_STATES", "DEPENDENCY_FAILURE_STATES", "TaskLifecycle", "validate_task_transition"]
