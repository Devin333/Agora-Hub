from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    identifier,
    non_negative_int,
)
from framework.harness.task_plan.checkpoint import TaskPlanCheckpoint
from framework.harness.task_plan.models import TaskInstance, TaskLifecycle, ValidatedTaskPlan
from framework.harness.task_plan.queue import (
    TaskPlanQueueProjection,
    TaskPlanQueueReclaimContinuation,
    TaskPlanQueueReadPort,
    TaskPlanQueueReadback,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer, TaskPlanReplayReport
from framework.harness.task_plan.schema import GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA
from framework.harness.task_plan.scheduler import materialize_queue_task
from framework.harness.task_plan.store import TaskPlanEvent, TaskResultRecord


@dataclass(frozen=True, slots=True)
class TaskPlanRecovery:
    """Pure recovery output; callers decide when to commit or dispatch it."""

    report: TaskPlanReplayReport
    checkpoint_verified: bool
    recovered_from_sequence: int
    missing_queue_projections: tuple[Any, ...] = ()
    confirmed_queue_readbacks: tuple[TaskPlanQueueReadback, ...] = ()
    reclaim_continuations: tuple[TaskPlanQueueReclaimContinuation, ...] = ()
    awaiting_reclaim: tuple[TaskInstance, ...] = ()
    recovery_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.report, TaskPlanReplayReport):
            raise TypeError("report must be TaskPlanReplayReport")
        if not isinstance(self.checkpoint_verified, bool):
            raise TypeError("checkpoint_verified must be bool")
        object.__setattr__(
            self,
            "recovered_from_sequence",
            non_negative_int(self.recovered_from_sequence, "recovered_from_sequence"),
        )
        graph_states = {
            item.task_id: item for item in self.report.projection.tasks
        }
        graph_active = {
            item.task_instance_id: item for item in self.report.active_task_instances
        }
        if not self.report.projection.is_graph_only:
            raise HarnessValidationError(
                "live TaskPlan recovery requires a Graph-only projection",
                code="legacy_task_plan_live_recovery_forbidden",
            )
        queue_items = tuple(self.missing_queue_projections)
        queue_instance_ids: list[str] = []
        for item in queue_items:
            if getattr(item, "payload", None) != {}:
                raise HarnessValidationError(
                    "TaskPlan recovery queue projection must remain identity-only",
                    code="task_plan_recovery_queue_payload_rejected",
                )
            projection = TaskPlanQueueProjection.from_task(item)
            instance = graph_active.get(projection.task_instance_id)
            state = graph_states.get(instance.task_id) if instance else None
            if (
                instance is None
                or state is None
                or state.status is not TaskLifecycle.READY
                or not projection.matches_instance(instance)
                or not projection.task_instance.matches_plan_projection_identity(
                    self.report.projection
                )
            ):
                raise HarnessValidationError(
                    "TaskPlan recovery queue projection has mismatched Graph identity",
                    code="task_plan_queue_projection_identity_mismatch",
                )
            queue_instance_ids.append(projection.task_instance_id)
        if len(queue_instance_ids) != len(set(queue_instance_ids)):
            raise HarnessValidationError(
                "TaskPlan recovery contains duplicate queue projections",
                code="task_plan_queue_projection_conflict",
            )
        object.__setattr__(self, "missing_queue_projections", queue_items)
        readbacks = tuple(self.confirmed_queue_readbacks)
        if any(not isinstance(item, TaskPlanQueueReadback) for item in readbacks):
            raise TypeError(
                "confirmed_queue_readbacks must contain TaskPlanQueueReadback values"
            )
        for readback in readbacks:
            instance = graph_active.get(readback.task_instance_id)
            state = graph_states.get(instance.task_id) if instance else None
            if (
                instance is None
                or state is None
                or state.status is not TaskLifecycle.READY
                or not readback.matches_instance(instance)
            ):
                raise HarnessValidationError(
                    "TaskPlan recovery queue read-back has mismatched Graph identity",
                    code="task_plan_queue_readback_identity_mismatch",
                )
        readback_instance_ids = [item.task_instance_id for item in readbacks]
        readback_message_ids = [item.message_id for item in readbacks]
        if (
            len(readback_instance_ids) != len(set(readback_instance_ids))
            or len(readback_message_ids) != len(set(readback_message_ids))
            or set(readback_instance_ids).intersection(queue_instance_ids)
        ):
            raise HarnessValidationError(
                "TaskPlan recovery queue evidence is conflicting",
                code="task_plan_queue_readback_conflict",
            )
        object.__setattr__(
            self,
            "confirmed_queue_readbacks",
            tuple(sorted(readbacks, key=lambda item: item.task_instance_id)),
        )
        awaiting = tuple(self.awaiting_reclaim)
        if any(not isinstance(item, TaskInstance) for item in awaiting):
            raise TypeError("awaiting_reclaim must contain TaskInstance values")
        awaiting_ids = [item.task_instance_id for item in awaiting]
        if len(awaiting_ids) != len(set(awaiting_ids)):
            raise HarnessValidationError(
                "TaskPlan recovery contains duplicate reclaim continuations",
                code="task_plan_reclaim_identity_mismatch",
            )
        for instance in awaiting:
            expected = graph_active.get(instance.task_instance_id)
            state = graph_states.get(instance.task_id)
            if (
                expected != instance
                or state is None
                or state.status
                not in {TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}
            ):
                raise HarnessValidationError(
                    "TaskPlan reclaim continuation has mismatched Graph identity",
                    code="task_plan_reclaim_identity_mismatch",
                )
        object.__setattr__(
            self,
            "awaiting_reclaim",
            tuple(sorted(awaiting, key=lambda item: (item.task_id, item.attempt))),
        )
        continuations = tuple(self.reclaim_continuations)
        if any(
            not isinstance(item, TaskPlanQueueReclaimContinuation)
            for item in continuations
        ):
            raise TypeError(
                "reclaim_continuations must contain "
                "TaskPlanQueueReclaimContinuation values"
            )
        continuation_ids = [item.task_instance_id for item in continuations]
        if (
            len(continuation_ids) != len(set(continuation_ids))
            or set(continuation_ids) != set(awaiting_ids)
        ):
            raise HarnessValidationError(
                "TaskPlan reclaim continuations do not match awaiting attempts",
                code="task_plan_reclaim_identity_mismatch",
            )
        awaiting_by_id = {item.task_instance_id: item for item in awaiting}
        if any(
            not continuation.matches_instance(
                awaiting_by_id[continuation.task_instance_id]
            )
            for continuation in continuations
        ):
            raise HarnessValidationError(
                "TaskPlan reclaim continuation has mismatched attempt identity",
                code="task_plan_reclaim_identity_mismatch",
            )
        object.__setattr__(
            self,
            "reclaim_continuations",
            tuple(sorted(continuations, key=lambda item: item.task_instance_id)),
        )
        object.__setattr__(
            self,
            "recovery_checksum",
            canonical_payload_checksum(self.checksum_projection()),
        )

    @property
    def pending_terminal_results(self) -> tuple[TaskResultRecord, ...]:
        return self.report.pending_terminal_results

    def checksum_projection(self) -> dict[str, Any]:
        payload = {
            "replay_checksum": self.report.replay_checksum,
            "checkpoint_verified": self.checkpoint_verified,
            "recovered_from_sequence": self.recovered_from_sequence,
            "missing_queue_task_ids": [
                str(getattr(item, "task_id")) for item in self.missing_queue_projections
            ],
            "awaiting_reclaim": [item.instance_checksum for item in self.awaiting_reclaim],
            "pending_terminal_result_checksums": [
                item.result_checksum for item in self.pending_terminal_results
            ],
        }
        payload.update(
            {
                "missing_queue_projection_checksums": [
                    TaskPlanQueueProjection.from_task(item).projection_checksum
                    for item in self.missing_queue_projections
                ],
                "confirmed_queue_readbacks": [
                    item.to_dict() for item in self.confirmed_queue_readbacks
                ],
                "reclaim_continuations": [
                    item.to_dict() for item in self.reclaim_continuations
                ],
            }
        )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "recovery_checksum": self.recovery_checksum}


class TaskPlanRecoveryService:
    """Rebuild TaskPlan state and queue projections from recorded evidence only."""

    def __init__(
        self,
        reducer: TaskPlanReplayReducer | None = None,
        *,
        queue_reader: TaskPlanQueueReadPort | None = None,
    ) -> None:
        self._reducer = reducer or TaskPlanReplayReducer()
        if queue_reader is not None and not isinstance(
            queue_reader,
            TaskPlanQueueReadPort,
        ):
            raise TypeError("queue_reader must implement TaskPlanQueueReadPort")
        self._queue_reader = queue_reader

    def recover(
        self,
        plans: Iterable[ValidatedTaskPlan],
        events: Iterable[TaskPlanEvent],
        *,
        results: Iterable[TaskResultRecord] = (),
        patches: Iterable[Any] = (),
        checkpoint: TaskPlanCheckpoint | None = None,
        queued_instance_ids: Iterable[str] = (),
        queue_name: str = "framework:queue:default",
    ) -> TaskPlanRecovery:
        plan_history = tuple(plans)
        recorded_events = tuple(events)
        recorded_results = tuple(results)
        if not plan_history:
            raise HarnessValidationError(
                "TaskPlan recovery requires accepted plan history",
                code="task_plan_recovery_plan_missing",
            )
        first_plan = plan_history[0]
        if not isinstance(first_plan, ValidatedTaskPlan):
            raise TypeError("plans must contain ValidatedTaskPlan values")
        if first_plan.schema_version != GRAPH_ONLY_VALIDATED_TASK_PLAN_SCHEMA:
            raise HarnessValidationError(
                "live TaskPlan recovery requires Graph-only plan history",
                code="legacy_task_plan_live_recovery_forbidden",
            )
        queue_name = identifier(queue_name, "queue_name")
        queue_reader = self._queue_reader
        if queue_reader is None:
            raise HarnessValidationError(
                "Graph-only TaskPlan recovery requires durable queue read authority",
                code="graph_task_plan_queue_read_port_unavailable",
            )
        queued_id_values = tuple(queued_instance_ids)
        if queued_id_values:
            raise HarnessValidationError(
                "Graph-only TaskPlan recovery requires exact durable queue read-back evidence",
                code="graph_task_plan_queue_readback_required",
            )
        checkpoint_verified = False
        recovered_from_sequence = 0
        if checkpoint is not None:
            if not isinstance(checkpoint, TaskPlanCheckpoint):
                raise TypeError("checkpoint must be TaskPlanCheckpoint")
            checkpoint_report = self._reducer.replay(
                plan_history,
                recorded_events,
                results=recorded_results,
                patches=patches,
                through_sequence=checkpoint.last_sequence,
                require_terminal_events=False,
                apply_unterminated_results=False,
                require_latest_plan=False,
            )
            checkpoint.verify_replay(checkpoint_report)
            checkpoint_verified = True
            recovered_from_sequence = checkpoint.last_sequence

        report = self._reducer.replay(
            plan_history,
            recorded_events,
            results=recorded_results,
            patches=patches,
            require_terminal_events=False,
            apply_unterminated_results=False,
        )
        if not report.projection.is_graph_only:
            raise HarnessValidationError(
                "live TaskPlan recovery replay produced a legacy projection",
                code="legacy_task_plan_live_recovery_forbidden",
            )
        failure_sequences = tuple(
            event.sequence for event in recorded_events if event.event_type == "TASK_FAILED"
        )
        has_halt_after_failure = bool(failure_sequences) and any(
            event.event_type == "TASK_PLAN_HALTED"
            and event.sequence > max(failure_sequences)
            for event in recorded_events
        )
        has_terminal_failure = any(
            item.status is TaskLifecycle.FAILED for item in report.projection.tasks
        )
        has_actionable_task = any(
            item.status
            in {
                TaskLifecycle.PENDING,
                TaskLifecycle.READY,
                TaskLifecycle.ADMITTED,
                TaskLifecycle.DISPATCHED,
                TaskLifecycle.RUNNING,
            }
            for item in report.projection.tasks
        )
        if has_terminal_failure and not has_actionable_task and not has_halt_after_failure:
            raise HarnessValidationError(
                "TaskPlan terminal failure has no durable halt evidence",
                code="task_plan_recovery_halt_missing",
                details={
                    "stage_id": plan_history[-1].stage_id,
                    "plan_version": plan_history[-1].version,
                    "reason_codes": sorted(
                        {
                            item.failure_reason_code
                            for item in report.projection.tasks
                            if item.failure_reason_code is not None
                        }
                    ),
                },
            )
        states = {item.task_id: item for item in report.projection.tasks}
        active_instances = {
            item.task_instance_id: item for item in report.active_task_instances
        }
        ready_instance_ids = tuple(
            sorted(
                instance.task_instance_id
                for instance in report.active_task_instances
                if states[instance.task_id].status is TaskLifecycle.READY
            )
        )
        raw_readbacks = queue_reader.read_task_plan_queue(
            queue_name=queue_name,
            task_instance_ids=ready_instance_ids,
        )
        if raw_readbacks is None:
            raise TypeError(
                "TaskPlanQueueReadPort must return TaskPlanQueueReadback values"
            )
        readbacks = tuple(
            TaskPlanQueueReadback.from_dict(item)
            if isinstance(item, Mapping)
            else item
            for item in raw_readbacks
        )
        if any(not isinstance(item, TaskPlanQueueReadback) for item in readbacks):
            raise TypeError(
                "TaskPlanQueueReadPort must return TaskPlanQueueReadback values"
            )
        readback_instance_ids = [item.task_instance_id for item in readbacks]
        readback_message_ids = [item.message_id for item in readbacks]
        if (
            len(readback_instance_ids) != len(set(readback_instance_ids))
            or len(readback_message_ids) != len(set(readback_message_ids))
        ):
            raise HarnessValidationError(
                "TaskPlan recovery contains conflicting queue read-back evidence",
                code="task_plan_queue_readback_conflict",
            )
        for readback in readbacks:
            instance = active_instances.get(readback.task_instance_id)
            state = states.get(instance.task_id) if instance is not None else None
            if (
                instance is None
                or state is None
                or state.status is not TaskLifecycle.READY
                or readback.projection.queue_name != queue_name
                or not readback.matches_instance(instance)
            ):
                raise HarnessValidationError(
                    "TaskPlan queue read-back does not match an active READY attempt",
                    code="task_plan_queue_readback_identity_mismatch",
                    details={"task_instance_id": readback.task_instance_id},
                )
        queued = set(readback_instance_ids)
        pending_result_instances = {
            item.task_instance_id for item in report.pending_terminal_results
        }
        missing_queue = []
        awaiting_reclaim = []
        reclaim_continuations = []
        for instance in report.active_task_instances:
            state = states[instance.task_id]
            if instance.task_instance_id in pending_result_instances:
                continue
            if state.status is TaskLifecycle.READY:
                if instance.task_instance_id not in queued:
                    missing_queue.append(
                        materialize_queue_task(
                            instance,
                            queue_name=queue_name,
                        )
                    )
            elif state.status in {TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}:
                awaiting_reclaim.append(instance)
                reclaim_continuations.append(
                    TaskPlanQueueReclaimContinuation.for_instance(
                        instance,
                        queue_name=queue_name,
                    )
                )
        return TaskPlanRecovery(
            report=report,
            checkpoint_verified=checkpoint_verified,
            recovered_from_sequence=recovered_from_sequence,
            missing_queue_projections=tuple(missing_queue),
            confirmed_queue_readbacks=readbacks,
            reclaim_continuations=tuple(reclaim_continuations),
            awaiting_reclaim=tuple(awaiting_reclaim),
        )


__all__ = ["TaskPlanRecovery", "TaskPlanRecoveryService"]
