"""Read-only attempt history indexes derived from canonical TaskPlan events."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.attempt_history import TaskAttemptHistoryRecord, TaskAttemptOutcome
from framework.harness.task_plan.canonical import thaw_mapping
from framework.harness.task_plan.models import (
    TaskInstance,
    TaskLifecycle,
    TaskPlanProjection,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.scheduler import task_instance_for_attempt

if TYPE_CHECKING:
    from framework.harness.task_plan.parallel import DispatchGroup, DispatchWave
    from framework.harness.task_plan.store import TaskPlanEvent, TaskResultRecord


ATTEMPT_HISTORY_EVENT = "TASK_ATTEMPT_RECORDED"


def accepted_results_from_history(
    history: Iterable[TaskAttemptHistoryRecord], projection: TaskPlanProjection | None,
) -> tuple[TaskResultRecord, ...]:
    """Select current accepted outputs without reconstructing history again."""
    checksums = {
        item.result.result_checksum for item in (projection.tasks if projection else ())
        if item.status is TaskLifecycle.SUCCEEDED and item.result is not None
    }
    results = (
        item.result for item in history
        if item.outcome is TaskAttemptOutcome.ACCEPTED and item.result is not None
        and item.result.result_checksum in checksums
    )
    return tuple(sorted(results, key=lambda item: (item.task_id, item.attempt, item.result_checksum)))


def recovery_results_from_history(
    history: Iterable[TaskAttemptHistoryRecord], projection: TaskPlanProjection,
) -> tuple[TaskResultRecord, ...]:
    """Include an active attempt whose result predates the parent projection."""
    records = tuple(history)
    active_attempts = {
        (state.task_id, state.active_instance_id, state.attempts)
        for state in projection.tasks
        if state.active_instance_id is not None
        and state.status in {TaskLifecycle.ADMITTED, TaskLifecycle.DISPATCHED, TaskLifecycle.RUNNING}
    }
    results = {item.result_checksum: item for item in accepted_results_from_history(records, projection)}
    for record in records:
        result = record.result
        if (
            record.outcome in {TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.REJECTED, TaskAttemptOutcome.FAILED}
            and result is not None
            and (result.run_id, result.stage_id, result.plan_id, result.plan_version)
            == (projection.run_id, projection.stage_id, projection.plan_id, projection.plan_version)
            and (result.task_id, result.task_instance_id, result.attempt) in active_attempts
        ):
            results[result.result_checksum] = result
    return tuple(sorted(results.values(), key=lambda item: (item.task_id, item.attempt, item.result_checksum)))


def _fail(message: str, code: str = "task_plan_attempt_history_identity_mismatch") -> None:
    raise HarnessValidationError(message, code=code)


def validate_history_record(
    record: TaskAttemptHistoryRecord,
    plan: ValidatedTaskPlan,
    history: Iterable[TaskPlanEvent],
) -> None:
    """Bind a record to prior admission facts, never its self-reported identity."""
    history = tuple(history)
    expected = task_instance_for_attempt(plan, record.task_id, record.attempt)
    definition = next(item for item in plan.tasks if item.task_id == record.task_id)
    if record.instance != expected or record.binding_checksum != definition.binding_checksum:
        _fail("attempt history differs from the accepted task/binding")
    if record.recovered_from is not None:
        source = next((item.payload["history_record"] for item in history
                       if item.event_type == ATTEMPT_HISTORY_EVENT
                       and item.payload["history_record"].get("record_checksum") == record.recovered_from), None)
        if source is None:
            _fail("recovered result requires its source failure", "task_plan_attempt_recovery_source_missing")
        source_record = TaskAttemptHistoryRecord.from_dict(source)
        if (source_record.outcome is not TaskAttemptOutcome.FAILED or source_record.result is not None
            or source_record.instance != record.instance or source_record.group != record.group
            or source_record.wave != record.wave or source_record.operation_key != record.operation_key
            or source_record.child_id != record.child_id or source_record.terminal_receipt != record.terminal_receipt):
            _fail("recovered result source is not the matching failed attempt", "task_plan_attempt_recovery_evidence_mismatch")
    validate_history_admission(record, history)


def validate_history_admission(
    record: TaskAttemptHistoryRecord,
    history: Iterable[TaskPlanEvent],
) -> None:
    prefix = tuple(history)
    record = TaskAttemptHistoryRecord.from_dict(record.to_dict())
    queue_admissions = _queue_admissions_for_attempt(record, prefix)
    if record.group is None:
        if len(queue_admissions) != 1:
            _fail("static attempt history requires one canonical queue admission")
        if _wave_admissions_for_attempt(record, prefix):
            _fail("attempt history has conflicting QUEUE and GROUP_WAVE admission")
        return
    if queue_admissions:
        _fail("parallel attempt history cannot use QUEUE admission ownership")
    group_id = record.group["group_id"]
    wave_id = record.wave["wave_id"]
    if record.outcome in {TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.REJECTED, TaskAttemptOutcome.FAILED}:
        latest_version = max((event.plan_version for event in prefix
                              if event.event_type == "PLAN_ACCEPTED" and event.plan_version is not None), default=record.plan_version)
        if record.plan_version != latest_version:
            _fail("superseded plan outcome must be quarantined", "task_plan_attempt_quarantined")
        closed = {
            "TASK_GROUP_CANCELLED", "TASK_GROUP_HALTED",
            "TASK_GROUP_SUPERSEDED", "TASK_GROUP_FAILED", "TASK_GROUP_JOINED",
        }
        if any(event.event_type in closed and (
            event.payload.get("group_id") == group_id
            or isinstance(event.payload.get("group"), Mapping) and event.payload["group"].get("group_id") == group_id
        ) for event in prefix):
            _fail("closed group outcome must be quarantined", "task_plan_attempt_quarantined")
    admitted = _wave_admissions_by_id(record, prefix, wave_id)
    if len(admitted) != 1:
        _fail("attempt history requires one canonical wave admission")
    admission, group, wave = admitted[0]
    # Group lifecycle state is mutable after admission (for example, the
    # second wave is admitted while the durable group is already RUNNING),
    # whereas attempt history deliberately stores the canonical ADMITTED
    # admission snapshot.  Compare the immutable group checksum and require
    # the history snapshot to remain an admission fact instead of comparing
    # the later mutable state verbatim.
    from framework.harness.task_plan.parallel import DispatchGroup
    from framework.harness.task_plan.parallel_lifecycle import DispatchGroupState
    recorded_group = DispatchGroup.from_dict(thaw_mapping(record.group))
    if (
        group.group_id != group_id
        or recorded_group.state is not DispatchGroupState.ADMITTED
        or recorded_group.group_checksum != group.group_checksum
        or wave.group_id != group_id
        or wave.wave_id != wave_id
        or wave.to_dict() != thaw_mapping(record.wave)
        or admission.plan_id != record.plan_id
        or admission.plan_version != record.plan_version
        or not _event_matches_instance_scope(admission, record.instance)
    ):
        _fail("attempt history group/wave differs from durable admission")
    reservations = tuple(
        reservation
        for reservation in wave.reservations
        if reservation.task_id == record.task_id
    )
    if (
        len(reservations) != 1
        or reservations[0].idempotency_key != record.instance.idempotency_key
        or dict(reservations[0].budget) != record.instance.budget_snapshot.to_dict()
    ):
        _fail("attempt history differs from its exact wave reservation")
    if record.operation_key is not None:
        intents = [
            event for event in prefix
            if event.event_type == "TASK_ATTEMPT_SPAWN_INTENT"
            and event.payload.get("operation_key") == record.operation_key
        ]
        if len(intents) != 1:
            _fail("supervised attempt history requires one durable spawn intent")
        identity = {
            "group_id": group_id, "wave_id": wave_id,
            "task_id": record.task_id, "task_instance_id": record.task_instance_id,
            "attempt": record.attempt,
        }
        if any(intents[0].payload.get(key) != value for key, value in identity.items()):
            _fail("attempt history differs from its spawn operation")
        receipts = [
            event for event in prefix
            if event.event_type == "TASK_ATTEMPT_SPAWN_CONFIRMED"
            and event.payload.get("operation_key") == record.operation_key
        ]
        if record.child_id is not None and (
            len(receipts) != 1
            or receipts[0].payload.get("child_id") != record.child_id
            or any(receipts[0].payload.get(key) != value for key, value in identity.items())
        ):
            _fail("attempt terminal receipt has no matching confirmed child")


def _queue_admissions_for_attempt(
    record: TaskAttemptHistoryRecord,
    history: tuple[TaskPlanEvent, ...],
) -> tuple[TaskPlanEvent, ...]:
    """Return exact QUEUE admissions after validating the complete envelope."""

    from framework.harness.task_plan.store import TaskQueueAdmissionEvidence

    admissions: list[TaskPlanEvent] = []
    for event in history:
        if (
            event.event_type != "TASK_QUEUE_ADMITTED"
            or event.plan_id != record.plan_id
            or event.plan_version != record.plan_version
            or event.task_id != record.task_id
        ):
            continue
        raw = event.payload.get("queue_admission")
        if not isinstance(raw, Mapping):
            _fail("queue admission is missing its canonical evidence")
        evidence = TaskQueueAdmissionEvidence.from_dict(raw)
        instance = evidence.task_instance
        if (
            event.task_instance_id != instance.task_instance_id
            or event.attempt != instance.attempt
            or event.input_checksum != instance.task_definition_checksum
            or not _event_matches_instance_scope(event, instance)
        ):
            _fail("queue admission event differs from its canonical evidence")
        if instance == record.instance:
            admissions.append(event)
    return tuple(admissions)


def _wave_admissions_by_id(
    record: TaskAttemptHistoryRecord,
    history: tuple[TaskPlanEvent, ...],
    wave_id: str,
) -> tuple[tuple[TaskPlanEvent, DispatchGroup, DispatchWave], ...]:
    from framework.harness.task_plan.parallel import DispatchGroup, DispatchWave

    admissions = []
    for event in history:
        if (
            event.event_type != "TASK_WAVE_ADMITTED"
            or event.plan_id != record.plan_id
            or event.plan_version != record.plan_version
        ):
            continue
        raw_group = event.payload.get("group")
        raw_wave = event.payload.get("wave")
        if not isinstance(raw_group, Mapping) or not isinstance(raw_wave, Mapping):
            _fail("wave admission is missing its canonical group/wave evidence")
        group = DispatchGroup.from_dict(thaw_mapping(raw_group))
        wave = DispatchWave.from_dict(thaw_mapping(raw_wave))
        if wave.wave_id == wave_id:
            admissions.append((event, group, wave))
    return tuple(admissions)


def _wave_admissions_for_attempt(
    record: TaskAttemptHistoryRecord,
    history: tuple[TaskPlanEvent, ...],
) -> tuple[TaskPlanEvent, ...]:
    """Find GROUP_WAVE admissions whose reservation owns this exact attempt."""

    from framework.harness.task_plan.parallel import DispatchWave

    admissions = []
    for event in history:
        if (
            event.event_type != "TASK_WAVE_ADMITTED"
            or event.plan_id != record.plan_id
            or event.plan_version != record.plan_version
        ):
            continue
        raw_wave = event.payload.get("wave")
        if not isinstance(raw_wave, Mapping):
            _fail("wave admission is missing its canonical wave evidence")
        wave = DispatchWave.from_dict(thaw_mapping(raw_wave))
        reservations = tuple(
            reservation
            for reservation in wave.reservations
            if reservation.task_id == record.task_id
            and reservation.idempotency_key == record.instance.idempotency_key
        )
        if reservations:
            if (
                len(reservations) != 1
                or dict(reservations[0].budget)
                != record.instance.budget_snapshot.to_dict()
            ):
                _fail("attempt history differs from its exact wave reservation")
            admissions.append(event)
    return tuple(admissions)


def _event_matches_instance_scope(event: TaskPlanEvent, instance: TaskInstance) -> bool:
    return all(
        getattr(event, name) == getattr(instance, name)
        for name in (
            "run_id",
            "stage_id",
            "graph_id",
            "graph_version",
            "graph_ref",
            "graph_checksum",
            "graph_schema_version",
            "compiler_version",
            "condition_policy_version",
            "stage_binding_checksum",
            "stage_identity_schema",
            "stage_identity_checksum",
        )
    )


def validate_attempt_history_append(
    history: Iterable[TaskPlanEvent], events: Iterable[TaskPlanEvent],
) -> None:
    prefix = list(history)
    for event in events:
        if event.event_type == ATTEMPT_HISTORY_EVENT:
            record = TaskAttemptHistoryRecord.from_dict(event.payload.get("history_record"))
            validate_history_admission(record, prefix)
            for name in ("task_id", "task_instance_id", "attempt"):
                if event.payload.get(name) != getattr(record, name):
                    _fail("attempt event differs from its history record")
            if (
                record.group is None
                or event.payload.get("group_id") != record.group["group_id"]
                or event.payload.get("wave_id") != record.wave["wave_id"]
            ):
                _fail("attempt event group/wave differs from its history record")
            if any(getattr(event, name) != getattr(record.instance, name) for name in (
                "run_id", "stage_id", "graph_id", "graph_version", "graph_ref", "graph_checksum",
                "graph_schema_version", "compiler_version", "condition_policy_version",
                "stage_binding_checksum", "stage_identity_schema", "stage_identity_checksum",
            )):
                _fail("attempt event Graph scope differs from its history record")
            for previous in prefix:
                if previous.event_type != ATTEMPT_HISTORY_EVENT:
                    continue
                raw = previous.payload["history_record"]
                same_attempt = raw["instance"]["task_instance_id"] == record.task_instance_id
                previous_outcome = TaskAttemptOutcome(raw["outcome"])
                is_recovery = same_attempt and raw["record_checksum"] == record.recovered_from
                if (same_attempt and previous_outcome not in {TaskAttemptOutcome.INDETERMINATE, record.outcome}
                    and record.outcome is not TaskAttemptOutcome.QUARANTINED and not is_recovery):
                    _fail("terminal attempt cannot acquire another terminal outcome", "task_plan_attempt_history_conflict")
                if (
                    same_attempt
                    and raw["outcome"] == record.outcome.value
                    and raw["record_checksum"] != record.record_checksum
                    and not is_recovery
                ):
                    _fail("one attempt outcome has conflicting evidence", "task_plan_attempt_history_conflict")
        prefix.append(event)


def history_record_for_result(
    plan: ValidatedTaskPlan, result: TaskResultRecord, history: Iterable[TaskPlanEvent],
) -> TaskAttemptHistoryRecord:
    prefix = tuple(history)
    matching: list[TaskAttemptHistoryRecord] = []
    for index, event in enumerate(prefix):
        if event.event_type != ATTEMPT_HISTORY_EVENT:
            continue
        record = TaskAttemptHistoryRecord.from_dict(event.payload["history_record"])
        if record.result is not None and record.result.result_checksum == result.result_checksum:
            validate_history_record(record, plan, prefix[:index])
            matching.append(record)
    if any(item.outcome is TaskAttemptOutcome.QUARANTINED for item in matching):
        _fail("quarantined attempt cannot be accepted", "task_plan_attempt_quarantined")
    candidates = [item for item in matching if item.outcome in {
        TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.REJECTED, TaskAttemptOutcome.FAILED,
    }]
    if candidates:
        if len({item.record_checksum for item in candidates}) != 1:
            _fail("result has conflicting attempt history", "task_plan_attempt_history_conflict")
        return candidates[0]
    if any(event.event_type == "TASK_GROUP_ADMITTED" and event.plan_id == plan.plan_id for event in prefix):
        _fail("parallel result requires a recorded attempt receipt", "task_plan_attempt_history_missing")
    record = TaskAttemptHistoryRecord.for_result(plan, result)
    validate_history_record(record, plan, prefix)
    return record


def result_history_from_events(
    plans: Iterable[ValidatedTaskPlan], events: Iterable[TaskPlanEvent],
    results: Iterable[TaskResultRecord],
) -> tuple[TaskAttemptHistoryRecord, ...]:
    """Preserve causal order, including superseded plans and late audit facts."""
    plans_by_version = {plan.version: plan for plan in plans}
    result_by_checksum = {item.result_checksum: item for item in results}
    prefix: list[TaskPlanEvent] = []
    records: dict[str, TaskAttemptHistoryRecord] = {}
    for event in events:
        record = None
        if event.event_type == ATTEMPT_HISTORY_EVENT:
            record = TaskAttemptHistoryRecord.from_dict(event.payload["history_record"])
            plan = plans_by_version.get(record.plan_version)
            if plan is None:
                prefix.append(event)
                continue
            validate_attempt_history_append(prefix, (event,))
            validate_history_record(record, plan, prefix)
        elif event.event_type in {"TASK_RESULT_ACCEPTED", "TASK_RESULT_REJECTED"}:
            plan = plans_by_version.get(event.plan_version)
            if plan is not None:
                result = result_by_checksum.get(event.payload.get("result_checksum"))
                if result is None:
                    _fail("result event has no stored candidate envelope", "task_plan_result_artifact_missing")
                record = history_record_for_result(plan, result, prefix)
        if record is not None:
            records.setdefault(record.record_checksum, record)
        prefix.append(event)
    return tuple(records.values())
