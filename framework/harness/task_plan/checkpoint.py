from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    exact_reference,
    frozen_mapping,
    identifier,
    non_negative_int,
    reference,
    required_text,
    thaw_mapping,
)
from framework.harness.task_plan.models import (
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.replay import (
    TASK_PLAN_REPLAY_REDUCER_VERSION,
    TASK_PLAN_REPLAY_REDUCER_VERSION_V3,
    TaskPlanReplayReport,
    _freeze_parallel_projection_mapping,
    _validate_parallel_report_projection,
)
from framework.harness.task_plan.store import TaskResultRecord
from framework.shared.time import format_datetime, parse_datetime


TASK_PLAN_CHECKPOINT_SCHEMA_V2 = "newsroom.harness-task-plan-checkpoint/v2"
TASK_PLAN_CHECKPOINT_SCHEMA_V3 = "newsroom.harness-task-plan-checkpoint/v3"
TASK_PLAN_CHECKPOINT_SCHEMA = TASK_PLAN_CHECKPOINT_SCHEMA_V3
TASK_PLAN_CHECKPOINT_SCHEMAS = (
    TASK_PLAN_CHECKPOINT_SCHEMA_V2,
    TASK_PLAN_CHECKPOINT_SCHEMA_V3,
)
_GRAPH_CHECKPOINT_IDENTITY_FIELDS = (
    "graph_id",
    "graph_version",
    "graph_ref",
    "graph_schema_version",
    "compiler_version",
    "condition_policy_version",
    "stage_binding_checksum",
    "stage_identity_schema",
    "stage_identity_checksum",
)
_ACTIVE_TASK_STATES = frozenset(
    {TaskLifecycle.READY, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}
)


@runtime_checkable
class TaskPlanCheckpointStorePort(Protocol):
    def save(self, checkpoint: "TaskPlanCheckpoint") -> "TaskPlanCheckpoint": ...

    def load(self, checkpoint_id: str) -> "TaskPlanCheckpoint": ...


@dataclass(frozen=True, slots=True)
class TaskPlanCheckpoint:
    """Detached validation snapshot backed by canonical TaskPlan history."""

    checkpoint_id: str
    run_id: str
    stage_id: str
    graph_checksum: str
    plan_id: str
    plan_version: int
    plan_checksum: str
    policy_ref: str
    projection: TaskPlanProjection | Mapping[str, Any]
    active_task_instances: tuple[TaskInstance | Mapping[str, Any], ...]
    ready_order: tuple[str, ...]
    accepted_output_refs: tuple[str, ...]
    pending_terminal_results: tuple[TaskResultRecord | Mapping[str, Any], ...]
    budget_snapshot: Mapping[str, Any]
    retry_counts: Mapping[str, int]
    replan_count: int
    last_sequence: int
    event_history_checksum: str
    replay_checksum: str
    created_at: str
    aggregate_ref: str | None = None
    aggregate_checksum: str | None = None
    graph_id: str | None = None
    graph_version: str | None = None
    graph_ref: str | None = None
    graph_schema_version: str | None = None
    compiler_version: str | None = None
    condition_policy_version: str | None = None
    stage_binding_checksum: str | None = None
    stage_identity_schema: str | None = None
    stage_identity_checksum: str | None = None
    parallel_groups: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    parallel_waves: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    parallel_reservations: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    parallel_diagnostics: tuple[Mapping[str, Any], ...] = ()
    parallel_event_sequence: int = 0
    schema_version: str = TASK_PLAN_CHECKPOINT_SCHEMA
    reducer_version: str = TASK_PLAN_REPLAY_REDUCER_VERSION
    checkpoint_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "checkpoint_id", identifier(self.checkpoint_id, "checkpoint_id"))
        for field_name in ("run_id", "stage_id", "plan_id"):
            object.__setattr__(self, field_name, identifier(getattr(self, field_name), field_name))
        _normalize_checkpoint_identity(self)
        object.__setattr__(self, "graph_checksum", checksum(self.graph_checksum, "graph_checksum"))
        if isinstance(self.plan_version, bool) or not isinstance(self.plan_version, int) or self.plan_version < 1:
            raise HarnessValidationError(
                "TaskPlan checkpoint plan_version must be positive",
                code="invalid_task_plan_checkpoint",
            )
        object.__setattr__(self, "plan_checksum", checksum(self.plan_checksum, "plan_checksum"))
        object.__setattr__(self, "policy_ref", exact_reference(self.policy_ref, "policy_ref"))

        projection = self.projection
        if isinstance(projection, Mapping):
            projection = TaskPlanProjection.from_dict(projection)
        if not isinstance(projection, TaskPlanProjection):
            raise TypeError("projection must be TaskPlanProjection")
        identity = (
            projection.run_id,
            projection.stage_id,
            projection.graph_checksum,
            projection.plan_id,
            projection.plan_version,
            projection.plan_checksum,
            projection.policy_ref,
        )
        expected_identity = (
            self.run_id,
            self.stage_id,
            self.graph_checksum,
            self.plan_id,
            self.plan_version,
            self.plan_checksum,
            self.policy_ref,
        )
        if identity != expected_identity:
            raise HarnessValidationError(
                "TaskPlan checkpoint identity does not match projection",
                code="task_plan_checkpoint_identity_mismatch",
            )
        if any(
            getattr(self, field_name) != getattr(projection, field_name)
            for field_name in _GRAPH_CHECKPOINT_IDENTITY_FIELDS
        ):
            raise HarnessValidationError(
                "TaskPlan checkpoint Graph identity does not match projection",
                code="task_plan_checkpoint_identity_mismatch",
            )
        object.__setattr__(self, "projection", projection)

        instances = tuple(
            TaskInstance.from_dict(item) if isinstance(item, Mapping) else item
            for item in self.active_task_instances
        )
        if any(not isinstance(item, TaskInstance) for item in instances):
            raise TypeError("active_task_instances must contain TaskInstance values")
        instance_by_id = {item.task_instance_id: item for item in instances}
        if len(instance_by_id) != len(instances):
            raise HarnessValidationError(
                "TaskPlan checkpoint contains duplicate active task instances",
                code="task_plan_checkpoint_duplicate_instance",
            )
        active_states = {
            item.active_instance_id: item
            for item in projection.tasks
            if item.status in _ACTIVE_TASK_STATES and item.active_instance_id is not None
        }
        if set(active_states) != set(instance_by_id):
            raise HarnessValidationError(
                "TaskPlan checkpoint active attempts do not match projection",
                code="task_plan_checkpoint_attempt_mismatch",
            )
        for instance_id, state in active_states.items():
            instance = instance_by_id[instance_id]
            if (
                not instance.matches_plan_projection_identity(projection)
                or instance.task_id != state.task_id
                or instance.task_definition_checksum != state.task_definition_checksum
                or instance.attempt != state.attempts
            ):
                raise HarnessValidationError(
                    "TaskPlan checkpoint attempt identity is inconsistent",
                    code="task_plan_checkpoint_attempt_mismatch",
                    details={"task_id": state.task_id},
                )
        object.__setattr__(self, "active_task_instances", instances)

        ready_order = tuple(identifier(item, "ready_order") for item in self.ready_order)
        if len(ready_order) != len(set(ready_order)) or set(ready_order) != {
            item.task_id for item in instances
        }:
            raise HarnessValidationError(
                "TaskPlan checkpoint ready order does not match active attempts",
                code="task_plan_checkpoint_ready_order_mismatch",
            )
        object.__setattr__(self, "ready_order", ready_order)

        accepted_refs = tuple(
            sorted(reference(item, "accepted_output_refs") for item in self.accepted_output_refs)
        )
        projection_refs = tuple(
            sorted(
                item.result.result_ref
                for item in projection.tasks
                if item.status is TaskLifecycle.SUCCEEDED and item.result is not None
            )
        )
        if accepted_refs != projection_refs:
            raise HarnessValidationError(
                "TaskPlan checkpoint accepted outputs do not match projection",
                code="task_plan_checkpoint_output_mismatch",
            )
        object.__setattr__(self, "accepted_output_refs", accepted_refs)

        pending_results = tuple(
            TaskResultRecord.from_dict(item) if isinstance(item, Mapping) else item
            for item in self.pending_terminal_results
        )
        if any(not isinstance(item, TaskResultRecord) for item in pending_results):
            raise TypeError("pending_terminal_results must contain TaskResultRecord values")
        for result in pending_results:
            instance = instance_by_id.get(result.task_instance_id)
            if (
                instance is None
                or result.attempt != instance.attempt
                or result.task_id != instance.task_id
                or not _result_matches_checkpoint_projection(result, projection)
            ):
                raise HarnessValidationError(
                    "TaskPlan checkpoint pending result does not match active attempt",
                    code="task_plan_checkpoint_result_mismatch",
                    details={"task_id": result.task_id},
                )
        object.__setattr__(
            self,
            "pending_terminal_results",
            tuple(
                sorted(
                    pending_results,
                    key=lambda item: (item.task_id, item.attempt, item.result_checksum),
                )
            ),
        )

        budget_snapshot = frozen_mapping(self.budget_snapshot, "budget_snapshot")
        if thaw_mapping(budget_snapshot) != thaw_mapping(projection.consumed_budget):
            raise HarnessValidationError(
                "TaskPlan checkpoint budget does not match projection",
                code="task_plan_checkpoint_budget_mismatch",
            )
        object.__setattr__(self, "budget_snapshot", budget_snapshot)

        task_ids = {item.task_id for item in projection.tasks}
        retry_counts = {
            identifier(task_id, "retry_counts.task_id"): non_negative_int(count, "retry_counts.count")
            for task_id, count in self.retry_counts.items()
        }
        if not set(retry_counts).issubset(task_ids):
            raise HarnessValidationError(
                "TaskPlan checkpoint retry counters reference unknown tasks",
                code="task_plan_checkpoint_retry_mismatch",
            )
        object.__setattr__(self, "retry_counts", frozen_mapping(dict(sorted(retry_counts.items())), "retry_counts"))
        object.__setattr__(self, "replan_count", non_negative_int(self.replan_count, "replan_count"))
        if self.replan_count != self.plan_version - 1:
            raise HarnessValidationError(
                "TaskPlan checkpoint replan count does not match plan version",
                code="task_plan_checkpoint_replan_mismatch",
            )
        object.__setattr__(self, "last_sequence", non_negative_int(self.last_sequence, "last_sequence"))
        if self.last_sequence != projection.last_sequence or self.last_sequence == 0:
            raise HarnessValidationError(
                "TaskPlan checkpoint sequence does not match projection",
                code="task_plan_checkpoint_sequence_mismatch",
            )
        object.__setattr__(self, "event_history_checksum", checksum(self.event_history_checksum, "event_history_checksum"))
        object.__setattr__(self, "replay_checksum", checksum(self.replay_checksum, "replay_checksum"))
        if self.aggregate_ref is not None:
            object.__setattr__(self, "aggregate_ref", reference(self.aggregate_ref, "aggregate_ref"))
        if self.aggregate_checksum is not None:
            object.__setattr__(self, "aggregate_checksum", checksum(self.aggregate_checksum, "aggregate_checksum"))
        if (self.aggregate_ref is None) != (self.aggregate_checksum is None):
            raise HarnessValidationError(
                "TaskPlan checkpoint aggregate ref and checksum must be present together",
                code="task_plan_checkpoint_aggregate_mismatch",
            )
        parallel_groups = _freeze_parallel_projection_mapping(
            self.parallel_groups,
            "parallel_groups",
        )
        parallel_waves = _freeze_parallel_projection_mapping(
            self.parallel_waves,
            "parallel_waves",
        )
        parallel_reservations = _freeze_parallel_projection_mapping(
            self.parallel_reservations,
            "parallel_reservations",
        )
        parallel_diagnostics = tuple(
            frozen_mapping(item, "parallel_diagnostics.item")
            for item in self.parallel_diagnostics
        )
        if any(not isinstance(item, Mapping) for item in self.parallel_diagnostics):
            raise TypeError("parallel_diagnostics must contain mappings")
        _validate_parallel_report_projection(
            parallel_groups,
            parallel_waves,
            parallel_reservations,
        )
        parallel_event_sequence = non_negative_int(
            self.parallel_event_sequence,
            "parallel_event_sequence",
        )
        has_parallel_facts = bool(
            parallel_groups
            or parallel_waves
            or parallel_reservations
            or parallel_diagnostics
        )
        if self.schema_version == TASK_PLAN_CHECKPOINT_SCHEMA_V2:
            if has_parallel_facts or parallel_event_sequence:
                raise HarnessValidationError(
                    "parallel checkpoint facts require schema v3",
                    code="task_plan_checkpoint_parallel_schema_required",
                )
        else:
            if has_parallel_facts and parallel_event_sequence < 1:
                raise HarnessValidationError(
                    "parallel checkpoint facts require an event sequence",
                    code="task_plan_checkpoint_parallel_sequence_missing",
                )
            if not has_parallel_facts and parallel_event_sequence:
                raise HarnessValidationError(
                    "parallel checkpoint sequence requires checkpoint facts",
                    code="task_plan_checkpoint_parallel_sequence_unexpected",
                )
            if parallel_event_sequence > self.last_sequence:
                raise HarnessValidationError(
                    "parallel checkpoint sequence exceeds checkpoint history",
                    code="task_plan_checkpoint_parallel_sequence_mismatch",
                )
        object.__setattr__(self, "parallel_groups", parallel_groups)
        object.__setattr__(self, "parallel_waves", parallel_waves)
        object.__setattr__(self, "parallel_reservations", parallel_reservations)
        object.__setattr__(self, "parallel_diagnostics", parallel_diagnostics)
        object.__setattr__(self, "parallel_event_sequence", parallel_event_sequence)
        expected_reducer_version = TASK_PLAN_REPLAY_REDUCER_VERSION_V3
        if self.reducer_version != expected_reducer_version:
            raise HarnessValidationError(
                "unsupported TaskPlan checkpoint reducer",
                code="unsupported_task_plan_replay_reducer",
                details={
                    "expected": expected_reducer_version,
                    "reducer_version": str(self.reducer_version),
                },
            )
        created_at = required_text(self.created_at, "created_at")
        parsed = parse_datetime(created_at)
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise HarnessValidationError(
                "TaskPlan checkpoint created_at must be timezone-aware",
                code="invalid_task_plan_checkpoint_timestamp",
            )
        object.__setattr__(self, "created_at", format_datetime(parsed))
        object.__setattr__(self, "checkpoint_checksum", canonical_payload_checksum(self.checksum_projection()))

    @classmethod
    def from_replay(
        cls,
        checkpoint_id: str,
        plan: ValidatedTaskPlan,
        report: TaskPlanReplayReport,
        *,
        created_at: str,
    ) -> "TaskPlanCheckpoint":
        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be ValidatedTaskPlan")
        if not isinstance(report, TaskPlanReplayReport):
            raise TypeError("report must be TaskPlanReplayReport")
        projection = report.projection
        if (
            projection.run_id != plan.run_id
            or projection.stage_id != plan.stage_id
            or projection.plan_id != plan.plan_id
            or projection.plan_version != plan.version
            or projection.plan_checksum != plan.plan_checksum
            or not projection.matches_plan_identity(plan)
        ):
            raise HarnessValidationError(
                "TaskPlan replay report does not match checkpoint plan",
                code="task_plan_checkpoint_identity_mismatch",
            )
        graph_identity = {
            field_name: getattr(plan, field_name)
            for field_name in _GRAPH_CHECKPOINT_IDENTITY_FIELDS
        }
        return cls(
            checkpoint_id=checkpoint_id,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            graph_checksum=plan.graph_checksum,
            plan_id=plan.plan_id,
            plan_version=plan.version,
            plan_checksum=plan.plan_checksum,
            policy_ref=plan.policy_ref,
            projection=projection,
            active_task_instances=report.active_task_instances,
            ready_order=report.ready_order,
            accepted_output_refs=report.accepted_output_refs,
            pending_terminal_results=report.pending_terminal_results,
            budget_snapshot=projection.consumed_budget,
            retry_counts=report.retry_counts,
            replan_count=report.replan_count,
            last_sequence=projection.last_sequence,
            event_history_checksum=report.event_history_checksum,
            replay_checksum=report.replay_checksum,
            created_at=created_at,
            aggregate_ref=report.aggregate_ref,
            aggregate_checksum=report.aggregate_checksum,
            parallel_groups=report.parallel_groups,
            parallel_waves=report.parallel_waves,
            parallel_reservations=report.parallel_reservations,
            parallel_diagnostics=report.parallel_diagnostics,
            parallel_event_sequence=report.parallel_event_sequence,
            schema_version=TASK_PLAN_CHECKPOINT_SCHEMA_V3,
            reducer_version=report.reducer_version,
            **graph_identity,
        )

    def verify_replay(self, report: TaskPlanReplayReport) -> None:
        if not isinstance(report, TaskPlanReplayReport):
            raise TypeError("report must be TaskPlanReplayReport")
        checks = {
            "projection_checksum": (
                self.projection.projection_checksum,
                report.projection.projection_checksum,
            ),
            "last_sequence": (self.last_sequence, report.projection.last_sequence),
            "event_history_checksum": (
                self.event_history_checksum,
                report.event_history_checksum,
            ),
            "replay_checksum": (self.replay_checksum, report.replay_checksum),
            "parallel_groups": (
                thaw_mapping(self.parallel_groups),
                thaw_mapping(report.parallel_groups),
            ),
            "parallel_waves": (
                thaw_mapping(self.parallel_waves),
                thaw_mapping(report.parallel_waves),
            ),
            "parallel_reservations": (
                thaw_mapping(self.parallel_reservations),
                thaw_mapping(report.parallel_reservations),
            ),
            "parallel_diagnostics": (
                tuple(thaw_mapping(item) for item in self.parallel_diagnostics),
                tuple(thaw_mapping(item) for item in report.parallel_diagnostics),
            ),
            "parallel_event_sequence": (
                self.parallel_event_sequence,
                report.parallel_event_sequence,
            ),
        }
        mismatches = sorted(name for name, values in checks.items() if values[0] != values[1])
        if report.reducer_version != self.reducer_version:
            mismatches.append("reducer_version")
        if mismatches:
            raise HarnessValidationError(
                "TaskPlan checkpoint does not match replayed history",
                code="task_plan_checkpoint_replay_mismatch",
                details={"mismatches": mismatches},
            )

    def checksum_projection(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "reducer_version": self.reducer_version,
            "checkpoint_id": self.checkpoint_id,
            "run_id": self.run_id,
        }
        payload.update(
            {
                field_name: getattr(self, field_name)
                for field_name in _GRAPH_CHECKPOINT_IDENTITY_FIELDS
            }
        )
        payload.update({
            "stage_id": self.stage_id,
            "graph_checksum": self.graph_checksum,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "policy_ref": self.policy_ref,
            "projection": self.projection.to_dict(),
            "active_task_instances": [item.to_dict() for item in self.active_task_instances],
            "ready_order": list(self.ready_order),
            "accepted_output_refs": list(self.accepted_output_refs),
            "pending_terminal_results": [item.to_dict() for item in self.pending_terminal_results],
            "budget_snapshot": thaw_mapping(self.budget_snapshot),
            "retry_counts": thaw_mapping(self.retry_counts),
            "replan_count": self.replan_count,
            "last_sequence": self.last_sequence,
            "event_history_checksum": self.event_history_checksum,
            "replay_checksum": self.replay_checksum,
            "created_at": self.created_at,
            "aggregate_ref": self.aggregate_ref,
            "aggregate_checksum": self.aggregate_checksum,
        })
        if self.schema_version == TASK_PLAN_CHECKPOINT_SCHEMA_V3:
            payload.update(
                {
                    "parallel_groups": thaw_mapping(self.parallel_groups),
                    "parallel_waves": thaw_mapping(self.parallel_waves),
                    "parallel_reservations": thaw_mapping(self.parallel_reservations),
                    "parallel_diagnostics": [
                        thaw_mapping(item) for item in self.parallel_diagnostics
                    ],
                    "parallel_event_sequence": self.parallel_event_sequence,
                }
            )
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "checkpoint_checksum": self.checkpoint_checksum}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskPlanCheckpoint":
        schema_version = value.get("schema_version")
        _require_checkpoint_schema(schema_version)
        common = frozenset(
            {
                "schema_version",
                "reducer_version",
                "checkpoint_id",
                "run_id",
                "stage_id",
                "graph_checksum",
                "plan_id",
                "plan_version",
                "plan_checksum",
                "policy_ref",
                "projection",
                "active_task_instances",
                "ready_order",
                "accepted_output_refs",
                "pending_terminal_results",
                "budget_snapshot",
                "retry_counts",
                "replan_count",
                "last_sequence",
                "event_history_checksum",
                "replay_checksum",
                "created_at",
                "aggregate_ref",
                "aggregate_checksum",
                "checkpoint_checksum",
            }
        )
        parallel = frozenset(
            {
                "parallel_groups",
                "parallel_waves",
                "parallel_reservations",
                "parallel_diagnostics",
                "parallel_event_sequence",
            }
        )
        identity = frozenset(_GRAPH_CHECKPOINT_IDENTITY_FIELDS)
        payload = exact_keys(
            value,
            required=(common | identity | parallel)
            if schema_version == TASK_PLAN_CHECKPOINT_SCHEMA_V3
            else common | identity,
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("checkpoint_checksum"), "checkpoint_checksum")
        checkpoint = cls(**payload)
        if supplied != checkpoint.checkpoint_checksum:
            raise HarnessValidationError(
                "TaskPlan checkpoint checksum does not match content",
                code="task_plan_checkpoint_checksum_mismatch",
            )
        return checkpoint

    @property
    def is_graph_only(self) -> bool:
        return self.schema_version in TASK_PLAN_CHECKPOINT_SCHEMAS


class InMemoryTaskPlanCheckpointStore:
    """Deterministic test-only checkpoint store."""

    is_durable = False

    def __init__(self) -> None:
        self._checkpoints: dict[str, TaskPlanCheckpoint] = {}

    def save(self, checkpoint: TaskPlanCheckpoint) -> TaskPlanCheckpoint:
        if not isinstance(checkpoint, TaskPlanCheckpoint):
            raise TypeError("checkpoint must be TaskPlanCheckpoint")
        existing = self._checkpoints.get(checkpoint.checkpoint_id)
        if existing is not None and existing.checkpoint_checksum != checkpoint.checkpoint_checksum:
            raise HarnessValidationError(
                "TaskPlan checkpoint id already contains different content",
                code="task_plan_checkpoint_conflict",
                details={"checkpoint_id": checkpoint.checkpoint_id},
            )
        self._checkpoints[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def load(self, checkpoint_id: str) -> TaskPlanCheckpoint:
        normalized = identifier(checkpoint_id, "checkpoint_id")
        try:
            return self._checkpoints[normalized]
        except KeyError as exc:
            raise HarnessValidationError(
                "TaskPlan checkpoint was not found",
                code="task_plan_checkpoint_missing",
                details={"checkpoint_id": normalized},
            ) from exc


class JsonlTaskPlanCheckpointStore(InMemoryTaskPlanCheckpointStore):
    """Append-only, checksummed TaskPlan checkpoint store for production composition.

    The TaskPlan event stream remains the source of truth.  Checkpoints are
    replay-derived acceleration snapshots, so this store only accepts exact
    idempotent rewrites and fails closed on a conflicting checkpoint id.
    """

    is_durable = True

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self._path = Path(path)
        self._lock = RLock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            for line_number, line in enumerate(
                self._path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                if not line.strip():
                    continue
                try:
                    checkpoint = TaskPlanCheckpoint.from_dict(json.loads(line))
                except Exception as exc:
                    raise HarnessValidationError(
                        "TaskPlan checkpoint log contains invalid content",
                        code="task_plan_checkpoint_log_corrupt",
                        details={"path": str(self._path), "line": line_number},
                    ) from exc
                super().save(checkpoint)

    @property
    def path(self) -> Path:
        return self._path

    def save(self, checkpoint: TaskPlanCheckpoint) -> TaskPlanCheckpoint:
        if not isinstance(checkpoint, TaskPlanCheckpoint):
            raise TypeError("checkpoint must be TaskPlanCheckpoint")
        with self._lock:
            existing = self._checkpoints.get(checkpoint.checkpoint_id)
            if existing is not None:
                return super().save(checkpoint)
            encoded = json.dumps(
                checkpoint.to_dict(),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n"
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            return super().save(checkpoint)


def _require_checkpoint_schema(value: Any) -> str:
    if value not in TASK_PLAN_CHECKPOINT_SCHEMAS:
        raise HarnessValidationError(
            "unsupported TaskPlan checkpoint schema",
            code="unsupported_task_plan_checkpoint_schema",
            details={"schema_version": str(value)},
        )
    return str(value)


def _normalize_checkpoint_identity(checkpoint: TaskPlanCheckpoint) -> None:
    _require_checkpoint_schema(checkpoint.schema_version)
    normalized = {
        "graph_id": identifier(checkpoint.graph_id, "graph_id"),
        "graph_version": identifier(checkpoint.graph_version, "graph_version"),
        "graph_ref": exact_reference(checkpoint.graph_ref, "graph_ref"),
        "graph_schema_version": required_text(
            checkpoint.graph_schema_version,
            "graph_schema_version",
        ),
        "compiler_version": required_text(
            checkpoint.compiler_version,
            "compiler_version",
        ),
        "condition_policy_version": required_text(
            checkpoint.condition_policy_version,
            "condition_policy_version",
        ),
        "stage_binding_checksum": checksum(
            checkpoint.stage_binding_checksum,
            "stage_binding_checksum",
        ),
        "stage_identity_schema": required_text(
            checkpoint.stage_identity_schema,
            "stage_identity_schema",
        ),
        "stage_identity_checksum": checksum(
            checkpoint.stage_identity_checksum,
            "stage_identity_checksum",
        ),
    }
    if normalized["graph_ref"] != (
        f"{normalized['graph_id']}@{normalized['graph_version']}"
    ):
        raise HarnessValidationError(
            "Graph-only TaskPlan checkpoint ref does not match identity",
            code="task_plan_checkpoint_identity_mismatch",
        )
    for field_name, normalized_value in normalized.items():
        object.__setattr__(checkpoint, field_name, normalized_value)


def _result_matches_checkpoint_projection(
    result: TaskResultRecord,
    projection: TaskPlanProjection,
) -> bool:
    if (
        result.run_id != projection.run_id
        or result.stage_id != projection.stage_id
        or result.plan_id != projection.plan_id
        or result.plan_version != projection.plan_version
    ):
        return False
    return all(
        getattr(result, field_name) == getattr(projection, field_name)
        for field_name in (*_GRAPH_CHECKPOINT_IDENTITY_FIELDS, "graph_checksum")
    )


__all__ = [
    "InMemoryTaskPlanCheckpointStore",
    "JsonlTaskPlanCheckpointStore",
    "TASK_PLAN_CHECKPOINT_SCHEMA",
    "TASK_PLAN_CHECKPOINT_SCHEMA_V2",
    "TASK_PLAN_CHECKPOINT_SCHEMA_V3",
    "TASK_PLAN_CHECKPOINT_SCHEMAS",
    "TaskPlanCheckpoint",
    "TaskPlanCheckpointStorePort",
]
