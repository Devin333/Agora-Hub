"""Low-cardinality TaskPlan metrics and trace projections."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.models import TaskPlanProjection, ValidatedTaskPlan
from framework.harness.task_plan.store import TaskPlanEvent
from framework.harness.graph.canonical import required_text


_METRIC_LABELS = frozenset(
    {
        "outcome",
        "status",
        "reason_code",
        "stage_kind",
        "worker_capability",
        "recovery_outcome",
    }
)
_MAX_TRACE_EVENTS = 512
_MAX_TELEMETRY_IDENTIFIERS = 64


@dataclass(frozen=True, slots=True)
class TaskPlanMetricSample:
    name: str
    value: float
    labels: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", required_text(self.name, "metric.name"))
        if isinstance(self.value, bool) or not isinstance(self.value, int | float):
            raise TypeError("metric value must be numeric")
        labels = {str(key): str(value) for key, value in dict(self.labels).items()}
        if set(labels).difference(_METRIC_LABELS):
            raise HarnessValidationError(
                "TaskPlan metric contains a high-cardinality label",
                code="task_plan_metric_label_rejected",
            )
        if any(not value or len(value) > 128 for value in labels.values()):
            raise HarnessValidationError(
                "TaskPlan metric label value is invalid",
                code="task_plan_metric_label_rejected",
            )
        object.__setattr__(
            self,
            "labels",
            MappingProxyType({key: labels[key] for key in sorted(labels)}),
        )
        object.__setattr__(self, "value", float(self.value))

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "labels": dict(self.labels)}


@dataclass(frozen=True, slots=True)
class TaskPlanTraceEvent:
    event_type: str
    sequence: int
    plan_version: int | None
    task_status: str | None
    reason_code: str | None
    input_checksum: str | None
    output_ref_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", required_text(self.event_type, "event_type"))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence <= 0:
            raise HarnessValidationError(
                "TaskPlan trace sequence must be positive",
                code="task_plan_trace_invalid",
            )
        if self.plan_version is not None and (
            isinstance(self.plan_version, bool)
            or not isinstance(self.plan_version, int)
            or self.plan_version <= 0
        ):
            raise HarnessValidationError(
                "TaskPlan trace plan_version must be positive when present",
                code="task_plan_trace_invalid",
            )
        if self.task_status is not None:
            object.__setattr__(
                self,
                "task_status",
                required_text(self.task_status, "task_status"),
            )
        if self.reason_code is not None:
            object.__setattr__(
                self,
                "reason_code",
                required_text(self.reason_code, "reason_code"),
            )
        if self.input_checksum is not None:
            value = self.input_checksum
            if not (
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
            ):
                raise HarnessValidationError(
                    "TaskPlan trace input checksum is invalid",
                    code="task_plan_trace_invalid",
                )
        if (
            isinstance(self.output_ref_count, bool)
            or not isinstance(self.output_ref_count, int)
            or self.output_ref_count < 0
        ):
            raise HarnessValidationError(
                "TaskPlan trace output ref count must be non-negative",
                code="task_plan_trace_invalid",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "sequence": self.sequence,
            "plan_version": self.plan_version,
            "task_status": self.task_status,
            "reason_code": self.reason_code,
            "input_checksum": self.input_checksum,
            "output_ref_count": self.output_ref_count,
        }


@dataclass(frozen=True, slots=True)
class TaskPlanTelemetryEvidence:
    """Serializable, observational telemetry for one accepted TaskPlan.

    IDs are retained as bounded evidence fields so operators can correlate a
    run, while metric labels remain restricted to ``_METRIC_LABELS``.  This
    object never participates in admission, routing, gates, or publication.
    """

    run_id: str
    stage_id: str
    plan_id: str
    plan_version: int
    plan_checksum: str
    group_ids: tuple[str, ...] = ()
    wave_ids: tuple[str, ...] = ()
    capability_admission_count: int = 0
    wait_duration_ms: int = 0
    run_duration_ms: int = 0
    join_duration_ms: int = 0
    budget_usage: Mapping[str, int] = field(default_factory=dict)
    retry_count: int = 0
    recovery_count: int = 0
    cancellation_count: int = 0
    degraded_count: int = 0
    evidence_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("run_id", "stage_id", "plan_id"):
            object.__setattr__(self, name, required_text(getattr(self, name), name))
        if (
            isinstance(self.plan_version, bool)
            or not isinstance(self.plan_version, int)
            or self.plan_version <= 0
        ):
            raise HarnessValidationError(
                "telemetry plan_version is invalid",
                code="task_plan_telemetry_invalid",
            )
        if not (
            isinstance(self.plan_checksum, str)
            and self.plan_checksum.startswith("sha256:")
            and len(self.plan_checksum) == 71
        ):
            raise HarnessValidationError(
                "telemetry plan checksum is invalid",
                code="task_plan_telemetry_invalid",
            )
        object.__setattr__(
            self,
            "group_ids",
            _bounded_identifiers(self.group_ids, "group_ids"),
        )
        object.__setattr__(
            self,
            "wave_ids",
            _bounded_identifiers(self.wave_ids, "wave_ids"),
        )
        for name in (
            "capability_admission_count", "wait_duration_ms", "run_duration_ms",
            "join_duration_ms", "retry_count", "recovery_count",
            "cancellation_count", "degraded_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessValidationError(
                    f"telemetry {name} must be non-negative",
                    code="task_plan_telemetry_invalid",
                )
        usage = dict(self.budget_usage)
        if len(usage) > 64:
            raise HarnessValidationError(
                "telemetry budget usage is too large",
                code="task_plan_telemetry_invalid",
            )
        normalized: dict[str, int] = {}
        for key, value in sorted(usage.items()):
            key = required_text(key, "budget_usage.key")
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise HarnessValidationError(
                    "telemetry budget usage is invalid",
                    code="task_plan_telemetry_invalid",
                )
            normalized[key] = value
        object.__setattr__(self, "budget_usage", MappingProxyType(normalized))
        object.__setattr__(
            self,
            "evidence_checksum",
            canonical_payload_checksum(self.to_dict(include_checksum=False)),
        )

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "group_ids": list(self.group_ids),
            "wave_ids": list(self.wave_ids),
            "capability_admission_count": self.capability_admission_count,
            "wait_duration_ms": self.wait_duration_ms,
            "run_duration_ms": self.run_duration_ms,
            "join_duration_ms": self.join_duration_ms,
            "budget_usage": dict(self.budget_usage),
            "retry_count": self.retry_count,
            "recovery_count": self.recovery_count,
            "cancellation_count": self.cancellation_count,
            "degraded_count": self.degraded_count,
        }
        if include_checksum:
            value["evidence_checksum"] = self.evidence_checksum
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskPlanTelemetryEvidence":
        if not isinstance(value, Mapping):
            raise TypeError("telemetry evidence must be an object")
        payload = dict(value)
        supplied = payload.pop("evidence_checksum", None)
        result = cls(**payload)
        if supplied != result.evidence_checksum:
            raise HarnessValidationError(
                "telemetry evidence checksum mismatch",
                code="task_plan_telemetry_checksum_mismatch",
            )
        return result


def task_plan_metric_samples(
    projection: TaskPlanProjection,
    plan: ValidatedTaskPlan,
    events: Iterable[TaskPlanEvent],
    *,
    replay_verified: bool | None = None,
) -> tuple[TaskPlanMetricSample, ...]:
    if not isinstance(projection, TaskPlanProjection):
        raise TypeError("projection must be TaskPlanProjection")
    if not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("plan must be ValidatedTaskPlan")
    if projection.plan_checksum != plan.plan_checksum:
        raise HarnessValidationError(
            "TaskPlan metrics require a projection for the accepted plan",
            code="task_plan_metric_projection_mismatch",
        )
    event_values = tuple(events)
    if not all(isinstance(item, TaskPlanEvent) for item in event_values):
        raise TypeError("events must contain TaskPlanEvent values")
    base_labels = {"stage_kind": "dynamic_task_plan"}
    event_counts = Counter(item.event_type for item in event_values)
    samples: list[TaskPlanMetricSample] = [
        TaskPlanMetricSample(
            "harness_task_plan_candidate_total",
            event_counts["PLAN_CANDIDATE_BUILT"],
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_candidate_rejected_total",
            event_counts["PLAN_CANDIDATE_REJECTED"]
            + event_counts["PLAN_VALIDATION_FAILED"],
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_accepted_total",
            event_counts["PLAN_ACCEPTED"],
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_patch_total",
            event_counts["PLAN_PATCH_ACCEPTED"],
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_retry_total",
            event_counts["TASK_RETRY_SCHEDULED"],
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_halt_total",
            event_counts["TASK_PLAN_HALTED"],
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_replan_total",
            event_counts["PLAN_PATCH_ACCEPTED"]
            + event_counts["TASK_GROUP_REPLAN_PENDING"],
            base_labels,
        ),
    ]
    status_counts = Counter(item.status.value for item in projection.tasks)
    for status, value in sorted(status_counts.items()):
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_tasks",
                value,
                {**base_labels, "status": status},
            )
        )
    definitions = {item.task_id: item for item in plan.tasks}
    capability_counts = Counter(
        definitions[item.task_id].task.worker_capability
        for item in projection.tasks
        if item.task_id in definitions
    )
    for capability, value in sorted(capability_counts.items()):
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_task_definitions",
                value,
                {**base_labels, "worker_capability": capability},
            )
        )
    for name, value in _bounded_budget_metrics(projection.consumed_budget):
        samples.append(TaskPlanMetricSample(name, value, base_labels))
    for reason in ("task_plan_stale_result", "task_plan_duplicate_result_conflict", "task_plan_wrong_binding", "task_plan_checksum_mismatch"):
        count = sum(1 for item in event_values if item.reason_code == reason)
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_rejected_result_total",
                count,
                {**base_labels, "reason_code": reason},
            )
        )
    if replay_verified is not None:
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_replay_verification",
                int(replay_verified),
                {**base_labels, "outcome": "passed" if replay_verified else "failed"},
            )
        )
    samples.extend(_parallel_metric_samples(event_values, base_labels))
    return tuple(samples)


def _parallel_metric_samples(
    events: tuple[TaskPlanEvent, ...],
    base_labels: Mapping[str, str],
) -> tuple[TaskPlanMetricSample, ...]:
    """Project low-cardinality parallel facts from the durable event stream."""

    group_admissions = tuple(
        item for item in events if item.event_type == "TASK_GROUP_ADMITTED"
    )
    if not group_admissions:
        return ()
    requested = [
        value
        for item in group_admissions
        if isinstance((value := item.payload.get("requested_parallelism")), int)
        and not isinstance(value, bool)
        and value >= 0
    ]
    effective = [
        value
        for item in events
        if item.event_type in {"TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED"}
        if isinstance((value := item.payload.get("effective_parallelism")), int)
        and not isinstance(value, bool)
        and value >= 0
    ]
    samples: list[TaskPlanMetricSample] = [
        TaskPlanMetricSample(
            "harness_task_plan_parallel_group_total",
            len(group_admissions),
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_parallel_requested",
            max(requested, default=0),
            base_labels,
        ),
        TaskPlanMetricSample(
            "harness_task_plan_parallel_effective",
            max(effective, default=0),
            base_labels,
        ),
    ]
    for event_type, metric_name, payload_key in (
        ("TASK_WAVE_DISPATCHED", "harness_task_plan_parallel_queue_duration_ms", "queue_wait_ms"),
        ("TASK_WAVE_COMPLETED", "harness_task_plan_parallel_run_duration_ms", "run_duration_ms"),
        ("TASK_GROUP_JOINED", "harness_task_plan_parallel_join_duration_ms", "join_duration_ms"),
    ):
        duration = sum(
            value
            for item in events
            if item.event_type == event_type
            if isinstance((value := item.payload.get(payload_key)), int)
            and not isinstance(value, bool)
            and value >= 0
        )
        samples.append(TaskPlanMetricSample(metric_name, duration, base_labels))

    child_states: Counter[str] = Counter()
    for item in events:
        if item.event_type != "TASK_WAVE_COMPLETED":
            continue
        states = item.payload.get("child_states")
        if not isinstance(states, Mapping):
            continue
        for state in states.values():
            if isinstance(state, str) and state:
                child_states[state.casefold()] += 1
    for state, count in sorted(child_states.items()):
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_parallel_children",
                count,
                {**base_labels, "status": state},
            )
        )

    recovery_outcomes = Counter(
        str(item.payload["recovery_outcome"])
        for item in events
        if item.event_type == "TASK_GROUP_RECOVERY"
        and isinstance(item.payload.get("recovery_outcome"), str)
        and item.payload["recovery_outcome"]
    )
    for outcome, count in sorted(recovery_outcomes.items()):
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_parallel_recovery_total",
                count,
                {**base_labels, "recovery_outcome": outcome},
            )
        )

    degraded_reasons = Counter(
        item.reason_code or str(item.payload.get("reason_code") or "unknown")
        for item in events
        if item.event_type == "DEGRADED_SERIAL"
    )
    for reason, count in sorted(degraded_reasons.items()):
        samples.append(
            TaskPlanMetricSample(
                "harness_task_plan_parallel_degraded_serial_total",
                count,
                {**base_labels, "reason_code": reason},
            )
        )
    return tuple(samples)


def task_plan_trace_events(
    events: Iterable[TaskPlanEvent],
    *,
    limit: int = _MAX_TRACE_EVENTS,
) -> tuple[TaskPlanTraceEvent, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise HarnessValidationError(
            "TaskPlan trace limit must be positive",
            code="task_plan_trace_limit_invalid",
        )
    values = tuple(events)
    if not all(isinstance(item, TaskPlanEvent) for item in values):
        raise TypeError("events must contain TaskPlanEvent values")
    selected = values[-limit:]
    return tuple(
        TaskPlanTraceEvent(
            event_type=item.event_type,
            sequence=item.sequence,
            plan_version=item.plan_version,
            task_status=_event_task_status(item.event_type),
            reason_code=item.reason_code,
            input_checksum=item.input_checksum,
            output_ref_count=len(item.output_refs),
        )
        for item in selected
    )


def task_plan_telemetry_evidence(
    plan: ValidatedTaskPlan,
    projection: TaskPlanProjection,
    events: Iterable[TaskPlanEvent],
) -> TaskPlanTelemetryEvidence:
    """Project telemetry from a checksum-validated projection and its events."""

    if not isinstance(plan, ValidatedTaskPlan):
        raise TypeError("plan must be ValidatedTaskPlan")
    if not isinstance(projection, TaskPlanProjection):
        raise TypeError("projection must be TaskPlanProjection")
    if (
        projection.run_id != plan.run_id
        or projection.stage_id != plan.stage_id
        or projection.plan_id != plan.plan_id
        or projection.plan_version != plan.version
        or projection.plan_checksum != plan.plan_checksum
    ):
        raise HarnessValidationError(
            "telemetry projection does not match the accepted plan",
            code="task_plan_telemetry_projection_mismatch",
        )
    values = tuple(events)
    if not all(isinstance(item, TaskPlanEvent) for item in values):
        raise TypeError("events must contain TaskPlanEvent values")
    scoped = tuple(
        item
        for item in values
        if item.run_id == plan.run_id
        and item.stage_id == plan.stage_id
        and item.plan_id == plan.plan_id
        and item.plan_version == plan.version
    )
    groups: set[str] = set()
    waves: set[str] = set()
    budget = {
        key: value
        for key, value in sorted(projection.consumed_budget.items())
        if key.startswith(("reserved_", "consumed_", "released_"))
        and isinstance(value, int)
        and not isinstance(value, bool)
    }
    if len(budget) > 64:
        raise HarnessValidationError(
            "telemetry budget usage exceeds its bounded dimension limit",
            code="task_plan_telemetry_invalid",
        )
    wait = run = join = 0
    for item in scoped:
        payload = item.payload
        snapshot = payload.get("group")
        if isinstance(snapshot, Mapping) and isinstance(snapshot.get("group_id"), str):
            groups.add(snapshot["group_id"])
        wave = payload.get("wave")
        if isinstance(wave, Mapping) and isinstance(wave.get("wave_id"), str):
            waves.add(wave["wave_id"])
        for key, target in (
            ("queue_wait_ms", "wait"),
            ("run_duration_ms", "run"),
            ("join_duration_ms", "join"),
        ):
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                if target == "wait":
                    wait += value
                elif target == "run":
                    run += value
                else:
                    join += value
    return TaskPlanTelemetryEvidence(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_checksum=plan.plan_checksum,
        group_ids=tuple(sorted(groups)),
        wave_ids=tuple(sorted(waves)),
        capability_admission_count=sum(
            item.event_type in {"TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED"}
            for item in scoped
        ),
        wait_duration_ms=wait,
        run_duration_ms=run,
        join_duration_ms=join,
        budget_usage=budget,
        retry_count=sum(item.event_type == "TASK_RETRY_SCHEDULED" for item in scoped),
        recovery_count=sum(item.event_type == "TASK_GROUP_RECOVERY" for item in scoped),
        cancellation_count=sum(
            item.event_type in {"TASK_GROUP_CANCELLED", "TASK_CANCELLED"}
            for item in scoped
        ),
        degraded_count=sum(item.event_type == "DEGRADED_SERIAL" for item in scoped),
    )


def _bounded_budget_metrics(value: Mapping[str, Any]) -> tuple[tuple[str, int], ...]:
    totals: list[tuple[str, int]] = []
    for key, item in sorted(value.items()):
        if len(totals) >= 16:
            break
        if not isinstance(item, int) or isinstance(item, bool):
            continue
        if key.startswith("reserved_"):
            metric = "harness_task_plan_budget_reserved"
        elif key.startswith("consumed_"):
            metric = "harness_task_plan_budget_consumed"
        elif key.startswith("released_"):
            metric = "harness_task_plan_budget_released"
        else:
            continue
        totals.append((metric, item))
    return tuple(totals)


def _event_task_status(event_type: str) -> str | None:
    return {
        "TASK_READY": "ready",
        "TASK_DISPATCHED": "dispatched",
        "TASK_STARTED": "running",
        "TASK_RESULT_ACCEPTED": "succeeded",
        "TASK_COMPLETED": "succeeded",
        "TASK_RESULT_REJECTED": "failed",
        "TASK_FAILED": "failed",
        "TASK_BLOCKED": "blocked",
        "TASK_SKIPPED": "skipped",
        "TASK_REPLACED": "replaced",
    }.get(event_type)


def _bounded_identifiers(values: Iterable[str], field_name: str) -> tuple[str, ...]:
    raw = tuple(values)
    if len(raw) > _MAX_TELEMETRY_IDENTIFIERS:
        raise HarnessValidationError(
            f"telemetry {field_name} exceeds its bounded item limit",
            code="task_plan_telemetry_invalid",
        )
    normalized = tuple(
        sorted(required_text(value, f"{field_name}.item") for value in raw)
    )
    if len(set(normalized)) != len(normalized):
        raise HarnessValidationError(
            f"telemetry {field_name} contains duplicates",
            code="task_plan_telemetry_invalid",
        )
    return normalized


__all__ = [
    "TaskPlanMetricSample",
    "TaskPlanTelemetryEvidence",
    "TaskPlanTraceEvent",
    "task_plan_metric_samples",
    "task_plan_telemetry_evidence",
    "task_plan_trace_events",
]
