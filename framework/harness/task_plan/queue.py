from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    identifier,
)
from framework.harness.task_plan.models import TaskInstance
from framework.shared.graph_identity import GraphRunIdentity


TASK_PLAN_QUEUE_TASK_TYPE = "harness_task_plan"
TASK_PLAN_QUEUE_METADATA_KEY = "task_plan_queue_projection"
TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2 = (
    "newsroom.harness-task-plan-queue-projection/v2"
)
TASK_PLAN_QUEUE_READBACK_SCHEMA_V2 = "newsroom.harness-task-plan-queue-readback/v2"
TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2 = "newsroom.harness-task-plan-queue-reclaim/v2"

_QUEUE_INSTANCE_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_schema_version",
        "compiler_version",
        "condition_policy_version",
        "graph_checksum",
        "stage_binding_checksum",
        "stage_identity_schema",
        "stage_identity_checksum",
        "stage_id",
        "plan_id",
        "plan_version",
        "plan_checksum",
        "task_id",
        "task_definition_checksum",
        "task_instance_id",
        "attempt",
        "worker_ref",
        "idempotency_key",
        "attempt_fence_ref",
        "budget_snapshot",
        "instance_checksum",
    }
)
_QUEUE_BUDGET_FIELDS = frozenset(
    {
        "max_turns",
        "max_tool_calls",
        "max_memory_ops",
        "max_output_units",
    }
)


@runtime_checkable
class TaskPlanQueueReadPort(Protocol):
    """Read exact queued TaskPlan attempts without granting queue authority."""

    def read_task_plan_queue(
        self,
        *,
        queue_name: str,
        task_instance_ids: tuple[str, ...],
    ) -> tuple["TaskPlanQueueReadback", ...]: ...


@dataclass(frozen=True, slots=True)
class TaskPlanQueueProjection:
    """Immutable Graph-only identity carried by a generic queue task."""

    queue_name: str
    task_instance: TaskInstance | Mapping[str, Any]
    schema_version: str = TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2
    projection_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2:
            raise HarnessValidationError(
                "unsupported TaskPlan queue projection schema",
                code="unsupported_task_plan_queue_projection_schema",
                details={"schema_version": str(self.schema_version)},
            )
        object.__setattr__(
            self,
            "queue_name",
            identifier(self.queue_name, "queue_name"),
        )
        instance = self.task_instance
        if isinstance(instance, Mapping):
            instance = _task_instance_from_queue_wire(instance)
        if not isinstance(instance, TaskInstance):
            raise TypeError("task_instance must be TaskInstance")
        if not instance.is_graph_only:
            raise HarnessValidationError(
                "TaskPlan queue projection v2 requires Graph-only identity",
                code="task_plan_queue_projection_identity_mismatch",
            )
        object.__setattr__(self, "task_instance", instance)
        object.__setattr__(
            self,
            "projection_checksum",
            canonical_payload_checksum(self.checksum_projection()),
        )

    @classmethod
    def for_instance(
        cls,
        instance: TaskInstance,
        *,
        queue_name: str,
    ) -> "TaskPlanQueueProjection":
        return cls(queue_name=queue_name, task_instance=instance)

    @property
    def task_instance_id(self) -> str:
        return self.task_instance.task_instance_id

    def matches_instance(self, instance: TaskInstance) -> bool:
        if not isinstance(instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        return self.task_instance == instance

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "queue_name": self.queue_name,
            "task_type": TASK_PLAN_QUEUE_TASK_TYPE,
            "max_attempts": 1,
            "payload": {},
            "task_instance": _task_instance_to_queue_wire(self.task_instance),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "projection_checksum": self.projection_checksum,
        }

    def to_task(self) -> Any:
        from framework.workers.models.task import Task

        instance = self.task_instance
        return Task(
            task_id=instance.task_instance_id,
            task_type=TASK_PLAN_QUEUE_TASK_TYPE,
            queue_name=self.queue_name,
            payload={},
            execution_scope="graph",
            graph_identity=_task_instance_graph_identity(instance),
            metadata={TASK_PLAN_QUEUE_METADATA_KEY: self.to_dict()},
            dedup_key=instance.idempotency_key,
            max_attempts=1,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskPlanQueueProjection":
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "TaskPlan queue projection payload must be an object",
                code="invalid_task_plan_payload",
            )
        schema_version = value.get("schema_version")
        if schema_version != TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2:
            raise HarnessValidationError(
                "unsupported TaskPlan queue projection schema",
                code="unsupported_task_plan_queue_projection_schema",
                details={"schema_version": str(schema_version)},
            )
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "queue_name",
                    "task_type",
                    "max_attempts",
                    "payload",
                    "task_instance",
                    "projection_checksum",
                }
            ),
            model=cls.__name__,
        )
        if payload.pop("task_type") != TASK_PLAN_QUEUE_TASK_TYPE:
            raise HarnessValidationError(
                "TaskPlan queue projection task type does not match",
                code="task_plan_queue_transport_mismatch",
            )
        max_attempts = payload.pop("max_attempts")
        if (
            type(max_attempts) is not int
            or max_attempts != 1
            or payload.pop("payload") != {}
        ):
            raise HarnessValidationError(
                "TaskPlan queue projection must remain identity-only",
                code="task_plan_queue_transport_mismatch",
            )
        supplied = checksum(
            payload.pop("projection_checksum"),
            "projection_checksum",
        )
        projection = cls(**payload)
        if supplied != projection.projection_checksum:
            raise HarnessValidationError(
                "TaskPlan queue projection checksum does not match",
                code="task_plan_queue_projection_checksum_mismatch",
            )
        return projection

    @classmethod
    def from_task(
        cls,
        task: Any,
        *,
        require_queued: bool = False,
    ) -> "TaskPlanQueueProjection":
        from framework.workers.models.status import TaskStatus
        from framework.workers.models.task import Task

        if not isinstance(task, Task):
            raise TypeError("task must be framework.workers.models.task.Task")
        allowed_statuses = (
            {TaskStatus.QUEUED}
            if require_queued
            else {TaskStatus.CREATED, TaskStatus.QUEUED}
        )
        try:
            status = TaskStatus(task.status)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskPlan queue read-back has an invalid transport status",
                code="task_plan_queue_readback_status_mismatch",
                details={"status": str(task.status)},
            ) from exc
        if status not in allowed_statuses:
            raise HarnessValidationError(
                "TaskPlan queue read-back is not an active queued record",
                code="task_plan_queue_readback_status_mismatch",
                details={"status": status.value},
            )
        if (
            not isinstance(task.metadata, Mapping)
            or set(task.metadata) != {TASK_PLAN_QUEUE_METADATA_KEY}
        ):
            metadata_fields = (
                sorted(str(key) for key in task.metadata)
                if isinstance(task.metadata, Mapping)
                else []
            )
            raise HarnessValidationError(
                "TaskPlan queue metadata does not match the Graph-only contract",
                code="task_plan_queue_transport_mismatch",
                details={"metadata_fields": metadata_fields},
            )
        projection = cls.from_dict(task.metadata[TASK_PLAN_QUEUE_METADATA_KEY])
        instance = projection.task_instance
        if (
            task.task_type != TASK_PLAN_QUEUE_TASK_TYPE
            or task.task_id != instance.task_instance_id
            or task.queue_name != projection.queue_name
            or task.payload != {}
            or task.graph_identity != _task_instance_graph_identity(instance)
            or task.dedup_key != instance.idempotency_key
            or type(task.max_attempts) is not int
            or task.max_attempts != 1
            or type(task.attempts) is not int
            or task.attempts != 0
            or type(task.priority) is not int
            or task.priority != 0
            or task.timeout_seconds is not None
            or task.leased_by is not None
            or task.lease_expires_at is not None
            or task.scheduled_for is not None
        ):
            raise HarnessValidationError(
                "generic queue task does not match its TaskPlan projection",
                code="task_plan_queue_transport_mismatch",
                details={"task_instance_id": instance.task_instance_id},
            )
        return projection


def _task_instance_graph_identity(instance: TaskInstance) -> GraphRunIdentity:
    """Project a Graph-only TaskPlan attempt onto the generic queue carrier."""

    return GraphRunIdentity(
        run_id=instance.run_id,
        graph_id=instance.graph_id,
        graph_version=instance.graph_version,
        graph_ref=instance.graph_ref,
        graph_checksum=instance.graph_checksum,
    )


@dataclass(frozen=True, slots=True)
class TaskPlanQueueReadback:
    """Checksum-bound evidence obtained by reading a durable queued record."""

    message_id: str
    projection: TaskPlanQueueProjection | Mapping[str, Any]
    schema_version: str = TASK_PLAN_QUEUE_READBACK_SCHEMA_V2
    readback_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TASK_PLAN_QUEUE_READBACK_SCHEMA_V2:
            raise HarnessValidationError(
                "unsupported TaskPlan queue read-back schema",
                code="unsupported_task_plan_queue_readback_schema",
                details={"schema_version": str(self.schema_version)},
            )
        object.__setattr__(
            self,
            "message_id",
            identifier(self.message_id, "message_id"),
        )
        projection = self.projection
        if isinstance(projection, Mapping):
            projection = TaskPlanQueueProjection.from_dict(projection)
        if not isinstance(projection, TaskPlanQueueProjection):
            raise TypeError("projection must be TaskPlanQueueProjection")
        object.__setattr__(self, "projection", projection)
        object.__setattr__(
            self,
            "readback_checksum",
            canonical_payload_checksum(self.checksum_projection()),
        )

    @property
    def task_instance_id(self) -> str:
        return self.projection.task_instance_id

    def matches_instance(self, instance: TaskInstance) -> bool:
        return self.projection.matches_instance(instance)

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "message_id": self.message_id,
            "projection": self.projection.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "readback_checksum": self.readback_checksum,
        }

    @classmethod
    def from_queue_task(
        cls,
        message_id: str,
        task: Any,
    ) -> "TaskPlanQueueReadback":
        """Parse a record after the queue port proves it is still undelivered."""

        return cls(
            message_id=message_id,
            projection=TaskPlanQueueProjection.from_task(task, require_queued=True),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskPlanQueueReadback":
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "TaskPlan queue read-back payload must be an object",
                code="invalid_task_plan_payload",
            )
        schema_version = value.get("schema_version")
        if schema_version != TASK_PLAN_QUEUE_READBACK_SCHEMA_V2:
            raise HarnessValidationError(
                "unsupported TaskPlan queue read-back schema",
                code="unsupported_task_plan_queue_readback_schema",
                details={"schema_version": str(schema_version)},
            )
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "message_id",
                    "projection",
                    "readback_checksum",
                }
            ),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("readback_checksum"), "readback_checksum")
        readback = cls(**payload)
        if supplied != readback.readback_checksum:
            raise HarnessValidationError(
                "TaskPlan queue read-back checksum does not match",
                code="task_plan_queue_readback_checksum_mismatch",
            )
        return readback


@dataclass(frozen=True, slots=True)
class TaskPlanQueueReclaimContinuation:
    """Pure handoff; the queue owner must still prove lease staleness and fence it."""

    queue_name: str
    task_instance: TaskInstance | Mapping[str, Any]
    schema_version: str = TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2
    continuation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2:
            raise HarnessValidationError(
                "unsupported TaskPlan queue reclaim schema",
                code="unsupported_task_plan_queue_reclaim_schema",
                details={"schema_version": str(self.schema_version)},
            )
        object.__setattr__(
            self,
            "queue_name",
            identifier(self.queue_name, "queue_name"),
        )
        instance = self.task_instance
        if isinstance(instance, Mapping):
            instance = _task_instance_from_queue_wire(instance)
        if not isinstance(instance, TaskInstance):
            raise TypeError("task_instance must be TaskInstance")
        if not instance.is_graph_only:
            raise HarnessValidationError(
                "TaskPlan queue reclaim v2 requires Graph-only identity",
                code="task_plan_reclaim_identity_mismatch",
            )
        object.__setattr__(self, "task_instance", instance)
        object.__setattr__(
            self,
            "continuation_checksum",
            canonical_payload_checksum(self.checksum_projection()),
        )

    @classmethod
    def for_instance(
        cls,
        instance: TaskInstance,
        *,
        queue_name: str,
    ) -> "TaskPlanQueueReclaimContinuation":
        return cls(queue_name=queue_name, task_instance=instance)

    @property
    def task_instance_id(self) -> str:
        return self.task_instance.task_instance_id

    def matches_instance(self, instance: TaskInstance) -> bool:
        if not isinstance(instance, TaskInstance):
            raise TypeError("instance must be TaskInstance")
        return self.task_instance == instance

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "continuation_type": "await_stale_reclaim",
            "queue_name": self.queue_name,
            "task_instance": _task_instance_to_queue_wire(self.task_instance),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "continuation_checksum": self.continuation_checksum,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
    ) -> "TaskPlanQueueReclaimContinuation":
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "TaskPlan queue reclaim payload must be an object",
                code="invalid_task_plan_payload",
            )
        schema_version = value.get("schema_version")
        if schema_version != TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2:
            raise HarnessValidationError(
                "unsupported TaskPlan queue reclaim schema",
                code="unsupported_task_plan_queue_reclaim_schema",
                details={"schema_version": str(schema_version)},
            )
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "continuation_type",
                    "queue_name",
                    "task_instance",
                    "continuation_checksum",
                }
            ),
            model=cls.__name__,
        )
        if payload.pop("continuation_type") != "await_stale_reclaim":
            raise HarnessValidationError(
                "TaskPlan queue reclaim action is not supported",
                code="task_plan_reclaim_action_mismatch",
            )
        supplied = checksum(
            payload.pop("continuation_checksum"),
            "continuation_checksum",
        )
        continuation = cls(**payload)
        if supplied != continuation.continuation_checksum:
            raise HarnessValidationError(
                "TaskPlan queue reclaim checksum does not match",
                code="task_plan_reclaim_checksum_mismatch",
            )
        return continuation


def _task_instance_to_queue_wire(instance: TaskInstance) -> dict[str, Any]:
    payload = instance.to_dict()
    payload["attempt_fence_ref"] = payload.pop("fencing_token")
    budget = dict(payload["budget_snapshot"])
    budget["max_output_units"] = budget.pop("max_output_tokens")
    payload["budget_snapshot"] = budget
    return payload


def _task_instance_from_queue_wire(value: Mapping[str, Any]) -> TaskInstance:
    payload = exact_keys(
        value,
        required=_QUEUE_INSTANCE_FIELDS,
        model="TaskPlanQueueTaskInstance",
    )
    budget = exact_keys(
        payload["budget_snapshot"],
        required=_QUEUE_BUDGET_FIELDS,
        optional=frozenset({"token_limit", "time_limit_ms", "cost_limit"}),
        model="TaskPlanQueueTaskBudget",
    )
    budget["max_output_tokens"] = budget.pop("max_output_units")
    payload["budget_snapshot"] = budget
    payload["fencing_token"] = identifier(
        payload.pop("attempt_fence_ref"),
        "attempt_fence_ref",
    )
    return TaskInstance.from_dict(payload)


__all__ = [
    "TASK_PLAN_QUEUE_METADATA_KEY",
    "TASK_PLAN_QUEUE_PROJECTION_SCHEMA_V2",
    "TASK_PLAN_QUEUE_READBACK_SCHEMA_V2",
    "TASK_PLAN_QUEUE_RECLAIM_SCHEMA_V2",
    "TASK_PLAN_QUEUE_TASK_TYPE",
    "TaskPlanQueueProjection",
    "TaskPlanQueueReadPort",
    "TaskPlanQueueReadback",
    "TaskPlanQueueReclaimContinuation",
]
