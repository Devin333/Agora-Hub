from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    identifier,
    non_negative_int,
    positive_int,
    task_reference_producer,
)
from framework.harness.task_plan.dag import task_dependency_depths
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import (
    ResolvedTaskSpec,
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    TaskProjection,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.queue import TaskPlanQueueProjection
from framework.harness.task_plan.schema import (
    GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
)


@dataclass(frozen=True, slots=True)
class TaskPlanReadyDecision:
    task_instances: tuple[TaskInstance, ...]
    blocked_task_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    decision_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        instances = tuple(self.task_instances)
        if any(not isinstance(item, TaskInstance) for item in instances):
            raise TypeError("task_instances must contain TaskInstance values")
        task_ids = tuple(item.task_id for item in instances)
        if len(task_ids) != len(set(task_ids)):
            raise HarnessValidationError(
                "ready decision must contain each task at most once",
                code="task_plan_duplicate_ready_task",
            )
        object.__setattr__(self, "task_instances", instances)
        object.__setattr__(self, "blocked_task_ids", tuple(sorted(set(self.blocked_task_ids))))
        object.__setattr__(self, "decision_checksum", canonical_payload_checksum(self.checksum_projection()))

    @property
    def task_requests(self) -> tuple[TaskInstance, ...]:
        return self.task_instances

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "task_instances": [item.to_dict() for item in self.task_instances],
            "blocked_task_ids": list(self.blocked_task_ids),
            "reason_code": self.reason_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "decision_checksum": self.decision_checksum}


class TaskPlanScheduler:
    """Pure readiness and reservation calculator over an accepted plan."""

    def next_ready_tasks(
        self,
        projection: TaskPlanProjection,
        max_count: int,
        *,
        plan: ValidatedTaskPlan,
        policy: TaskPlanPolicy | None = None,
        worker_capacity: int | None = None,
        available_input_refs: tuple[str, ...] | Mapping[str, Any] = (),
    ) -> TaskPlanReadyDecision:
        _require_projection_matches_plan(projection, plan)
        maximum = non_negative_int(max_count, "max_count")
        if policy is not None:
            maximum = min(maximum, policy.max_parallelism)
        if worker_capacity is not None:
            if isinstance(worker_capacity, bool) or not isinstance(worker_capacity, int) or worker_capacity < 0:
                raise HarnessValidationError("worker_capacity must be non-negative", code="invalid_task_plan_limit")
            maximum = min(maximum, worker_capacity)
        states = {item.task_id: item for item in projection.tasks}
        definitions = {item.task_id: item for item in plan.tasks}
        missing_states = sorted(set(definitions) - set(states))
        if missing_states:
            raise HarnessValidationError(
                "TaskPlan projection is missing accepted task state",
                code="task_plan_projection_incomplete",
                details={"task_ids": missing_states},
            )
        active = sum(
            state.status
            in {
                TaskLifecycle.READY,
                TaskLifecycle.DISPATCHED,
                TaskLifecycle.RUNNING,
            }
            for state in projection.tasks
        )
        parallelism = policy.max_parallelism if policy is not None else plan.limits.max_parallelism
        new_slots = max(parallelism - active, 0)
        available = set(available_input_refs.values()) if isinstance(available_input_refs, Mapping) else set(available_input_refs)
        depths = _task_depths(definitions)
        candidates: list[ResolvedTaskSpec] = []
        blocked: list[str] = []
        for task_id in sorted(definitions):
            definition = definitions[task_id]
            state = states[task_id]
            if state.status not in {TaskLifecycle.PENDING, TaskLifecycle.READY}:
                continue
            dependency_states = tuple(states[dependency].status for dependency in definition.depends_on)
            if any(
                status in {
                    TaskLifecycle.FAILED,
                    TaskLifecycle.BLOCKED,
                    TaskLifecycle.BLOCKED_DEPENDENCY,
                    TaskLifecycle.SKIPPED,
                }
                for status in dependency_states
            ):
                blocked.append(task_id)
                continue
            if any(status is not TaskLifecycle.SUCCEEDED for status in dependency_states):
                continue
            if not _inputs_available(definition, available, states):
                continue
            candidates.append(definition)
        candidates.sort(
            key=lambda item: (
                item.priority,
                depths[item.task_id],
                item.task_id,
                item.task_definition_checksum,
            )
        )
        selected: list[TaskInstance] = []
        reservations = _reservation_totals(projection.consumed_budget)
        aggregate_limit = (policy or _policy_from_plan_limits(plan)).aggregate_task_budget
        budget_blocked = False
        for definition in candidates:
            if len(selected) >= maximum:
                break
            requested = definition.normalized_budget
            state = states[definition.task_id]
            already_reserved = state.status is TaskLifecycle.READY and state.active_instance_id is not None
            if not already_reserved and new_slots == 0:
                continue
            proposed = {
                name: reservations[name] + (0 if already_reserved else getattr(requested, name))
                for name in reservations
            }
            if any(proposed[name] > getattr(aggregate_limit, name) for name in proposed):
                budget_blocked = True
                continue
            reservations = proposed
            if not already_reserved:
                new_slots -= 1
            attempt = state.attempts if state.status is TaskLifecycle.READY and state.active_instance_id else state.attempts + 1
            selected.append(
                task_instance_for_attempt(
                    plan,
                    definition.task_id,
                    attempt,
                    task_instance_id=state.active_instance_id,
                )
            )
        reason = None
        if candidates and not selected:
            reason = "budget_exhausted" if budget_blocked else "parallelism_or_capacity_bound"
        return TaskPlanReadyDecision(
            task_instances=tuple(selected),
            blocked_task_ids=tuple(blocked),
            reason_code=reason,
        )

    def reserve_ready_tasks(
        self,
        projection: TaskPlanProjection,
        decision: TaskPlanReadyDecision,
    ) -> TaskPlanProjection:
        selected = {item.task_id: item for item in decision.task_instances}
        if not selected:
            return projection
        states = {item.task_id: item for item in projection.tasks}
        for task_id, instance in selected.items():
            state = states.get(task_id)
            if state is None:
                raise HarnessValidationError(
                    "ready decision references an unknown task",
                    code="task_plan_projection_incomplete",
                    details={"task_id": task_id},
                )
            if state.status is TaskLifecycle.READY:
                if state.active_instance_id != instance.task_instance_id or state.attempts != instance.attempt:
                    raise HarnessValidationError(
                        "ready decision does not match existing reservation",
                        code="task_plan_task_instance_mismatch",
                        details={"task_id": task_id},
                    )
            elif state.status is not TaskLifecycle.PENDING:
                raise HarnessValidationError(
                    "only pending tasks may be reserved",
                    code="task_plan_task_not_pending",
                    details={"task_id": task_id, "status": state.status.value},
                )
            if not instance.matches_plan_projection_identity(projection):
                raise HarnessValidationError(
                    "ready decision task instance is outside the accepted projection",
                    code="task_plan_task_instance_mismatch",
                    details={"task_id": task_id},
                )
        tasks = tuple(
            replace(
                state,
                status=TaskLifecycle.READY,
                attempts=selected[state.task_id].attempt,
                active_instance_id=selected[state.task_id].task_instance_id,
            )
            if state.task_id in selected
            else state
            for state in projection.tasks
        )
        ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
        if (ledger.run_id, ledger.stage_id, ledger.policy_ref) != (projection.run_id, projection.stage_id, projection.policy_ref):
            raise HarnessValidationError("budget ledger owner differs from projection", code="task_plan_budget_identity_conflict")
        budget = ledger.reserve(decision.task_instances).snapshot()
        return replace(projection, tasks=tasks, consumed_budget=budget)

    @staticmethod
    def mark_dispatched(projection: TaskPlanProjection, instance: TaskInstance) -> TaskPlanProjection:
        return _transition_task(projection, instance, TaskLifecycle.DISPATCHED)

    @staticmethod
    def mark_started(projection: TaskPlanProjection, instance: TaskInstance) -> TaskPlanProjection:
        return _transition_task(projection, instance, TaskLifecycle.RUNNING)

    @staticmethod
    def reclaim_stale(
        projection: TaskPlanProjection,
        task_id: str,
        *,
        task_instance_id: str | None = None,
    ) -> TaskPlanProjection:
        """Return a leased task to READY without allocating a new attempt."""
        found = False
        tasks = []
        for state in projection.tasks:
            if state.task_id != task_id:
                tasks.append(state)
                continue
            found = True
            if state.status not in {TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}:
                raise HarnessValidationError("only dispatched or running tasks may be reclaimed", code="task_plan_task_not_stale")
            if task_instance_id is not None and state.active_instance_id != task_instance_id:
                raise HarnessValidationError("stale task instance does not match projection", code="task_plan_task_instance_mismatch")
            tasks.append(replace(state, status=TaskLifecycle.READY))
        if not found:
            raise HarnessValidationError("task is missing from projection", code="task_plan_projection_incomplete")
        return replace(projection, tasks=tuple(tasks))


def materialize_queue_task(
    instance: TaskInstance,
    *,
    queue_name: str = "framework:queue:default",
) -> Any:
    """Create the exact Graph-only queue projection for one accepted attempt."""
    if not instance.is_graph_only:
        raise HarnessValidationError(
            "live TaskPlan queue projection requires Graph-only task identity",
            code="legacy_task_plan_queue_projection_forbidden",
        )
    return TaskPlanQueueProjection.for_instance(
        instance,
        queue_name=queue_name,
    ).to_task()


def task_instance_for_attempt(
    plan: ValidatedTaskPlan,
    task_id: str,
    attempt: int,
    *,
    task_instance_id: str | None = None,
) -> TaskInstance:
    """Rebuild the exact accepted task-attempt identity without live I/O."""

    if not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("plan must be ValidatedTaskPlan")
    normalized_task_id = identifier(task_id, "task_id")
    normalized_attempt = positive_int(attempt, "attempt")
    definition = next(
        (item for item in plan.tasks if item.task_id == normalized_task_id),
        None,
    )
    if definition is None:
        raise HarnessValidationError(
            "task attempt references a task outside the accepted plan",
            code="task_plan_unknown_task",
            details={"task_id": normalized_task_id, "plan_version": plan.version},
        )
    identity: dict[str, Any] = {
        "run_id": plan.run_id,
        "stage_id": plan.stage_id,
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "plan_checksum": plan.plan_checksum,
        "task_id": definition.task_id,
        "task_definition_checksum": definition.task_definition_checksum,
        "attempt": normalized_attempt,
    }
    graph_identity: dict[str, Any] = {}
    if plan.is_graph_only:
        graph_identity = {
            "graph_id": plan.graph_id,
            "graph_version": plan.graph_version,
            "graph_ref": plan.graph_ref,
            "graph_schema_version": plan.graph_schema_version,
            "compiler_version": plan.compiler_version,
            "condition_policy_version": plan.condition_policy_version,
            "graph_checksum": plan.graph_checksum,
            "stage_binding_checksum": plan.stage_binding_checksum,
            "stage_identity_schema": plan.stage_identity_schema,
            "stage_identity_checksum": plan.stage_identity_checksum,
            "schema_version": GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
        }
    digest = canonical_payload_checksum(identity).removeprefix("sha256:")
    expected_instance_id = f"ti_{digest}"
    if task_instance_id is not None:
        supplied_instance_id = identifier(task_instance_id, "task_instance_id")
        if supplied_instance_id != expected_instance_id:
            raise HarnessValidationError(
                "recorded task instance does not match deterministic attempt identity",
                code="task_plan_task_instance_mismatch",
                details={
                    "task_id": normalized_task_id,
                    "attempt": normalized_attempt,
                    "expected": expected_instance_id,
                    "actual": supplied_instance_id,
                },
            )
    return TaskInstance(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_checksum=plan.plan_checksum,
        task_id=definition.task_id,
        task_definition_checksum=definition.task_definition_checksum,
        task_instance_id=expected_instance_id,
        attempt=normalized_attempt,
        worker_ref=definition.worker_ref,
        idempotency_key=f"idem_{digest}",
        fencing_token=f"fence_{digest}",
        budget_snapshot=definition.normalized_budget,
        **graph_identity,
    )


def _require_projection_matches_plan(projection: TaskPlanProjection, plan: ValidatedTaskPlan) -> None:
    if not isinstance(projection, TaskPlanProjection) or not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("projection and plan must be typed TaskPlan values")
    if not projection.matches_plan_identity(plan):
        raise HarnessValidationError(
            "TaskPlan projection does not match accepted plan identity",
            code="task_plan_projection_identity_mismatch",
        )


def _transition_task(
    projection: TaskPlanProjection,
    instance: TaskInstance,
    status: TaskLifecycle,
) -> TaskPlanProjection:
    if not instance.matches_plan_projection_identity(projection):
        raise HarnessValidationError(
            "task instance is outside the accepted projection",
            code="task_plan_task_instance_mismatch",
            details={"task_id": instance.task_id},
        )
    found = False
    tasks: list[TaskProjection] = []
    for state in projection.tasks:
        if state.task_id != instance.task_id:
            tasks.append(state)
            continue
        found = True
        if state.active_instance_id != instance.task_instance_id or state.attempts != instance.attempt:
            raise HarnessValidationError(
                "task instance does not match reserved projection",
                code="task_plan_task_instance_mismatch",
                details={"task_id": instance.task_id},
            )
        if state.status is status:
            tasks.append(state)
            continue
        allowed_previous = {
            TaskLifecycle.DISPATCHED: {TaskLifecycle.READY},
            TaskLifecycle.RUNNING: {TaskLifecycle.DISPATCHED},
        }.get(status, frozenset())
        if state.status not in allowed_previous:
            raise HarnessValidationError(
                "invalid TaskPlan task state transition",
                code="task_plan_invalid_task_transition",
                details={"task_id": instance.task_id, "from": state.status.value, "to": status.value},
            )
        tasks.append(replace(state, status=status))
    if not found:
        raise HarnessValidationError("task is missing from projection", code="task_plan_projection_incomplete")
    return replace(projection, tasks=tuple(tasks))


def _task_depths(definitions: Mapping[str, ResolvedTaskSpec]) -> dict[str, int]:
    return task_dependency_depths(
        {task_id: definition.depends_on for task_id, definition in definitions.items()}
    )


def _inputs_available(
    definition: ResolvedTaskSpec,
    available: set[str],
    states: Mapping[str, TaskProjection],
) -> bool:
    for input_ref in definition.task.input_refs:
        if input_ref in available:
            continue
        producer = task_reference_producer(input_ref, tuple(states))
        if producer is None:
            return False
        state = states.get(producer)
        if state is None or state.status is not TaskLifecycle.SUCCEEDED or state.result is None:
            return False
    return True


def _reservation_totals(value: Mapping[str, Any]) -> dict[str, int]:
    return TaskPlanBudgetLedger.from_snapshot(value).allocated_totals()


def _policy_from_plan_limits(plan: ValidatedTaskPlan):
    class _LimitsPolicy:
        aggregate_task_budget = plan.limits.aggregate_task_budget

    return _LimitsPolicy()


__all__ = [
    "TaskPlanReadyDecision",
    "TaskPlanScheduler",
    "materialize_queue_task",
    "task_instance_for_attempt",
]
