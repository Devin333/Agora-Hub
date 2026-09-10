from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

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
    TaskAdmissionOwner,
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    TaskProjection,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.policy import TaskPlanPolicy
from framework.harness.task_plan.task_lifecycle import DEPENDENCY_FAILURE_STATES
from framework.harness.task_plan.queue import TaskPlanQueueProjection
from framework.harness.task_plan.schema import (
    GRAPH_ONLY_TASK_INSTANCE_SCHEMA,
)


@dataclass(frozen=True, slots=True)
class TaskPlanReadyDecision:
    task_instances: tuple[TaskInstance, ...] = ()
    logical_ready_task_ids: tuple[str, ...] = ()
    blocked_task_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    decision_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.task_instances, (str, bytes, bytearray, Mapping, set, frozenset))
            or not isinstance(self.task_instances, Sequence)
        ):
            raise TypeError("task_instances must be an ordered sequence")
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
        if (
            isinstance(self.logical_ready_task_ids, (str, bytes, bytearray, Mapping, set, frozenset))
            or not isinstance(self.logical_ready_task_ids, Sequence)
        ):
            raise TypeError("logical_ready_task_ids must be an ordered sequence")
        ready_task_ids = tuple(
            identifier(item, "logical_ready_task_id")
            for item in self.logical_ready_task_ids
        )
        if len(ready_task_ids) != len(set(ready_task_ids)):
            raise HarnessValidationError(
                "logical ready decision must contain each task at most once",
                code="task_plan_duplicate_ready_task",
            )
        if task_ids and ready_task_ids and task_ids != ready_task_ids:
            raise HarnessValidationError(
                "admission instances must preserve the logical ready order",
                code="task_plan_ready_order_mismatch",
            )
        object.__setattr__(self, "logical_ready_task_ids", ready_task_ids)
        if (
            isinstance(self.blocked_task_ids, (str, bytes, bytearray, Mapping, set, frozenset))
            or not isinstance(self.blocked_task_ids, Sequence)
        ):
            raise TypeError("blocked_task_ids must be an ordered sequence")
        blocked_task_ids = tuple(
            identifier(item, "blocked_task_id") for item in self.blocked_task_ids
        )
        object.__setattr__(
            self,
            "blocked_task_ids",
            tuple(sorted(set(blocked_task_ids))),
        )
        object.__setattr__(self, "decision_checksum", canonical_payload_checksum(self.checksum_projection()))

    @property
    def task_requests(self) -> tuple[TaskInstance, ...]:
        return self.task_instances

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "task_instances": [item.to_dict() for item in self.task_instances],
            "logical_ready_task_ids": list(self.logical_ready_task_ids),
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
        # Physical capacity does not truncate logical eligibility.  Keep these
        # parameters for the Control Plane API boundary, but use them only for
        # strict input validation; wave packing owns the physical limit.
        non_negative_int(max_count, "max_count")
        if worker_capacity is not None:
            if isinstance(worker_capacity, bool) or not isinstance(worker_capacity, int) or worker_capacity < 0:
                raise HarnessValidationError("worker_capacity must be non-negative", code="invalid_task_plan_limit")
        states = {item.task_id: item for item in projection.tasks}
        definitions = {item.task_id: item for item in plan.tasks}
        missing_states = sorted(set(definitions) - set(states))
        if missing_states:
            raise HarnessValidationError(
                "TaskPlan projection is missing accepted task state",
                code="task_plan_projection_incomplete",
                details={"task_ids": missing_states},
            )
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
                status in DEPENDENCY_FAILURE_STATES
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
        return TaskPlanReadyDecision(
            logical_ready_task_ids=tuple(item.task_id for item in candidates),
            blocked_task_ids=tuple(blocked),
        )

    def reserve_ready_tasks(
        self,
        projection: TaskPlanProjection,
        decision: TaskPlanReadyDecision,
    ) -> TaskPlanProjection:
        """Persist logical READY without allocating an execution attempt.

        The compatibility name remains at the Control Plane boundary.  In v4
        this method records readiness only; :meth:`admit_ready_tasks` is the
        sole operation that allocates an attempt and execution budget.
        """

        ready_order = decision.logical_ready_task_ids or tuple(
            item.task_id for item in decision.task_instances
        )
        selected = set(ready_order)
        states = {item.task_id: item for item in projection.tasks}
        for task_id in selected:
            state = states.get(task_id)
            if state is None:
                raise HarnessValidationError(
                    "ready decision references an unknown task",
                    code="task_plan_projection_incomplete",
                    details={"task_id": task_id},
                )
            if state.status not in {TaskLifecycle.PENDING, TaskLifecycle.READY}:
                raise HarnessValidationError(
                    "only pending or ready tasks may be marked logically ready",
                    code="task_plan_task_not_pending",
                    details={"task_id": task_id, "status": state.status.value},
                )
        tasks = tuple(
            state.transitioned(TaskLifecycle.READY)
            if state.task_id in selected
            else state
            for state in projection.tasks
        )
        return replace(projection, tasks=tasks, logical_ready_order=ready_order)

    @staticmethod
    def admit_ready_tasks(
        projection: TaskPlanProjection,
        instances: Sequence[TaskInstance],
        *,
        admission_owner: TaskAdmissionOwner,
    ) -> TaskPlanProjection:
        """Atomically allocate exact attempts and their budget reservations."""

        if not isinstance(admission_owner, TaskAdmissionOwner):
            raise HarnessValidationError(
                "task admission requires an explicit durable owner",
                code="invalid_task_projection",
            )
        selected = tuple(instances)
        if not selected or any(not isinstance(item, TaskInstance) for item in selected):
            raise HarnessValidationError(
                "task admission requires typed task instances",
                code="task_plan_task_instance_mismatch",
            )
        selected_by_id = {item.task_id: item for item in selected}
        if len(selected_by_id) != len(selected):
            raise HarnessValidationError(
                "task admission contains duplicate task ids",
                code="task_plan_duplicate_ready_task",
            )
        states = {item.task_id: item for item in projection.tasks}
        exact_existing = tuple(
            item
            for item in selected
            if (
                (state := states.get(item.task_id)) is not None
                and state.status is TaskLifecycle.ADMITTED
                and state.attempts == item.attempt
                and state.active_instance_id == item.task_instance_id
                and state.admission_owner is admission_owner
                and item.matches_plan_projection_identity(projection)
                and item.task_definition_checksum == state.task_definition_checksum
            )
        )
        if len(exact_existing) == len(selected):
            # Projection identity alone is insufficient: TaskInstance's
            # deterministic attempt id deliberately excludes transport and
            # allocation payloads.  Exact admission redelivery must therefore
            # be backed by the original outstanding ledger records before it
            # can be treated as idempotent.
            ledger = TaskPlanBudgetLedger.from_snapshot(
                projection.consumed_budget
            )
            for item in selected:
                if item.idempotency_key not in ledger.records:
                    raise HarnessValidationError(
                        "admitted attempt has no budget reservation",
                        code="task_plan_budget_reservation_missing",
                        details={"task_id": item.task_id},
                    )
            verified = ledger.reserve(selected)
            if verified is not ledger:
                # The pre-check above makes insertion impossible.  Keep this
                # guard explicit so future ledger changes cannot silently turn
                # a missing record into a successful idempotent admission.
                raise HarnessValidationError(
                    "admission redelivery changed the budget ledger",
                    code="task_plan_budget_identity_conflict",
                )
            return projection
        if exact_existing:
            raise HarnessValidationError(
                "task admission is partially present",
                code="task_plan_task_instance_mismatch",
            )
        ready_order = projection.logical_ready_order
        selected_order = tuple(item.task_id for item in selected)
        if tuple(task_id for task_id in ready_order if task_id in selected_by_id) != selected_order:
            raise HarnessValidationError(
                "task admission does not preserve logical ready order",
                code="task_plan_ready_order_mismatch",
            )
        for item in selected:
            state = states.get(item.task_id)
            if state is None or state.status is not TaskLifecycle.READY:
                raise HarnessValidationError(
                    "only logically ready tasks may be admitted",
                    code="task_plan_task_not_pending",
                    details={"task_id": item.task_id},
                )
            if not item.matches_plan_projection_identity(projection):
                raise HarnessValidationError(
                    "admission task is outside the accepted projection",
                    code="task_plan_task_instance_mismatch",
                )
            if (
                item.attempt != state.attempts + 1
                or item.task_definition_checksum != state.task_definition_checksum
            ):
                raise HarnessValidationError(
                    "admission must allocate the exact next task attempt",
                    code="task_plan_task_instance_mismatch",
                    details={"task_id": item.task_id},
                )
        tasks = tuple(
            state.transitioned(
                TaskLifecycle.ADMITTED,
                attempts=selected_by_id[state.task_id].attempt,
                active_instance_id=selected_by_id[state.task_id].task_instance_id,
                admission_owner=admission_owner,
            )
            if state.task_id in selected_by_id
            else state
            for state in projection.tasks
        )
        ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
        if (ledger.run_id, ledger.stage_id, ledger.policy_ref) != (
            projection.run_id,
            projection.stage_id,
            projection.policy_ref,
        ):
            raise HarnessValidationError(
                "budget ledger owner differs from projection",
                code="task_plan_budget_identity_conflict",
            )
        admitted_ledger = ledger.reserve(selected)
        return replace(
            projection,
            tasks=tasks,
            logical_ready_order=tuple(
                task_id for task_id in ready_order if task_id not in selected_by_id
            ),
            consumed_budget=admitted_ledger.snapshot(),
        )

    @staticmethod
    def mark_admitted(
        projection: TaskPlanProjection,
        instance: TaskInstance,
        *,
        admission_owner: TaskAdmissionOwner,
    ) -> TaskPlanProjection:
        return TaskPlanScheduler.admit_ready_tasks(
            projection,
            (instance,),
            admission_owner=admission_owner,
        )

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
        plan: ValidatedTaskPlan,
        task_instance_id: str | None = None,
    ) -> TaskPlanProjection:
        """Return a leased task to READY without allocating a new attempt."""
        _require_projection_matches_plan(projection, plan)
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
            tasks.append(state.transitioned(
                TaskLifecycle.READY,
                active_instance_id=None,
                admission_owner=None,
            ))
        if not found:
            raise HarnessValidationError("task is missing from projection", code="task_plan_projection_incomplete")
        ready_ids = set(projection.logical_ready_order).union({task_id})
        definitions = {item.task_id: item for item in plan.tasks}
        depths = _task_depths(definitions)
        stable_ready_order = tuple(sorted(
            ready_ids,
            key=lambda item: (
                definitions[item].priority,
                depths[item],
                item,
                definitions[item].task_definition_checksum,
            ),
        ))
        return replace(
            projection,
            tasks=tuple(tasks),
            logical_ready_order=stable_ready_order,
        )


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
        if (
            state.active_instance_id != instance.task_instance_id
            or state.attempts != instance.attempt
            or state.task_definition_checksum != instance.task_definition_checksum
        ):
            raise HarnessValidationError(
                "task instance does not match reserved projection",
                code="task_plan_task_instance_mismatch",
                details={"task_id": instance.task_id},
            )
        if state.status is status:
            tasks.append(state)
            continue
        tasks.append(state.transitioned(status))
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


__all__ = [
    "TaskPlanReadyDecision",
    "TaskPlanScheduler",
    "materialize_queue_task",
    "task_instance_for_attempt",
]
