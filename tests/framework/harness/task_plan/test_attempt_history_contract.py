from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import (
    ChildAgentState,
    ChildAgentTerminalReceipt,
)
from framework.harness.task_plan.attempt_history import (
    TASK_ATTEMPT_HISTORY_SCHEMA,
    TaskAttemptHistoryRecord,
    TaskAttemptOutcome,
)
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    thaw_mapping,
)
from framework.harness.task_plan.models import TaskLifecycle
from framework.harness.task_plan.replay import _apply_parallel_recovery, _projection_for_plan
from framework.harness.task_plan.scheduler import TaskPlanScheduler, TaskPlanReadyDecision
from framework.harness.task_plan.parallel import (
    DispatchGroupState,
    DispatchWave,
    DispatchWaveState,
    ParallelAgentCoordinator,
    TaskReservation,
    spawn_operation_key,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _request,
    _result,
)


def _fixture():
    plan = _accepted_parallel_plan(("task-1",))
    request = _request(plan)
    instance = request.task_instances[0]
    definition = plan.tasks[0]
    group = ParallelAgentCoordinator(
        max_workers=1,
        allow_test_executor=True,
    ).create_group(request)
    assert group.state is DispatchGroupState.ADMITTED
    wave = DispatchWave(
        group_id=group.group_id,
        ordinal=1,
        task_ids=(instance.task_id,),
        effective_parallelism=1,
        reservations=(
            TaskReservation(
                instance.task_id,
                instance.idempotency_key,
                instance.budget_snapshot.to_dict(),
            ),
        ),
        state=DispatchWaveState.ADMITTED,
        execution_mode="SUPERVISED",
    )
    operation_key = spawn_operation_key(
        group.group_id,
        wave.wave_id,
        instance.task_instance_id,
        instance.attempt,
    )
    return plan, instance, definition, group, wave, operation_key


def _terminal_receipt(
    group,
    operation_key: str,
    *,
    status: ChildAgentState,
    result=None,
    child_id: str = "child-1",
    reason_code: str | None = None,
    termination_confirmed: bool = True,
) -> ChildAgentTerminalReceipt:
    payload = (
        {
            "task_result": result.to_dict(),
            "task_result_checksum": result.result_checksum,
        }
        if result is not None
        else None
    )
    return ChildAgentTerminalReceipt(
        child_id=child_id,
        operation_id=operation_key,
        parent_graph_identity=group.parent_graph_identity,
        status=status,
        reason_code=reason_code
        or {
            ChildAgentState.SUCCEEDED: "worker_completed",
            ChildAgentState.FAILED: "worker_failed",
            ChildAgentState.CANCELLED: "cancelled",
            ChildAgentState.LOST: "child_lease_expired",
        }[status],
        result_ref=(
            f"child-result://{child_id}/{operation_key}"
            if payload is not None
            else None
        ),
        result_checksum=(
            canonical_payload_checksum(payload) if payload is not None else None
        ),
        termination_confirmed=termination_confirmed,
        completed_at=datetime(2026, 9, 9, tzinfo=UTC),
    )


def _parallel_record(
    outcome: TaskAttemptOutcome,
    *,
    result=None,
    receipt: ChildAgentTerminalReceipt | None = None,
    reason_code: str | None = None,
) -> TaskAttemptHistoryRecord:
    _plan, instance, definition, group, wave, operation_key = _fixture()
    return TaskAttemptHistoryRecord(
        instance=instance,
        binding_checksum=definition.binding_checksum,
        outcome=outcome,
        result=result,
        group=group,
        wave=wave,
        operation_key=operation_key,
        child_id=receipt.child_id if receipt is not None else None,
        terminal_receipt=receipt,
        reason_code=reason_code,
    )


def test_static_attempt_history_roundtrips_all_outcomes_without_fabricated_receipts() -> None:
    plan, instance, definition, _group, _wave, _operation_key = _fixture()
    succeeded = _result(plan, instance)
    rejected = _result(plan, instance, status=TaskLifecycle.FAILED)
    values = (
        (TaskAttemptOutcome.ACCEPTED, succeeded),
        (TaskAttemptOutcome.REJECTED, rejected),
        (TaskAttemptOutcome.FAILED, None),
        (TaskAttemptOutcome.CANCELLED, None),
        (TaskAttemptOutcome.INDETERMINATE, None),
        (TaskAttemptOutcome.RECLAIMED, None),
        (TaskAttemptOutcome.QUARANTINED, succeeded),
    )

    for outcome, result in values:
        record = TaskAttemptHistoryRecord(
            instance=instance,
            binding_checksum=definition.binding_checksum,
            outcome=outcome,
            result=result,
            reason_code=None if outcome is TaskAttemptOutcome.ACCEPTED else outcome.value.lower(),
        )
        restored = TaskAttemptHistoryRecord.from_dict(record.to_dict())

        assert restored == record
        assert restored.schema_version == TASK_ATTEMPT_HISTORY_SCHEMA
        assert restored.outcome is outcome
        assert restored.group is None and restored.wave is None
        assert restored.operation_key is None and restored.terminal_receipt is None
        assert (
            restored.run_id,
            restored.stage_id,
            restored.plan_id,
            restored.plan_version,
            restored.task_id,
            restored.task_instance_id,
            restored.attempt,
        ) == (
            instance.run_id,
            instance.stage_id,
            instance.plan_id,
            instance.plan_version,
            instance.task_id,
            instance.task_instance_id,
            instance.attempt,
        )


def test_supervised_accepted_result_binds_full_admission_and_terminal_receipt() -> None:
    plan, instance, definition, group, wave, operation_key = _fixture()
    result = _result(plan, instance)
    receipt = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.SUCCEEDED,
        result=result,
    )

    record = TaskAttemptHistoryRecord.for_result(
        plan,
        result,
        group=group,
        wave=wave,
        operation_key=operation_key,
        child_id=receipt.child_id,
        terminal_receipt=receipt,
    )
    restored = TaskAttemptHistoryRecord.from_dict(record.to_dict())

    assert restored == record
    assert restored.outcome is TaskAttemptOutcome.ACCEPTED
    assert restored.binding_checksum == definition.binding_checksum
    assert restored.group is not None
    assert restored.wave is not None
    assert restored.terminal_receipt is not None
    assert thaw_mapping(restored.group) == group.to_dict()
    assert thaw_mapping(restored.wave) == wave.to_dict()
    assert thaw_mapping(restored.terminal_receipt) == receipt.to_dict()
    assert restored.result == result
    with pytest.raises(TypeError):
        restored.group["state"] = "FAILED"


def test_history_requires_admission_snapshots_not_later_mutable_lifecycle_views() -> None:
    plan, instance, definition, group, wave, operation_key = _fixture()
    result = _result(plan, instance)
    receipt = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.SUCCEEDED,
        result=result,
    )

    for changes in (
        {"group": group.transitioned(DispatchGroupState.RUNNING)},
        {"wave": wave.transitioned(DispatchWaveState.RUNNING)},
    ):
        values = {
            "instance": instance,
            "binding_checksum": definition.binding_checksum,
            "outcome": TaskAttemptOutcome.ACCEPTED,
            "result": result,
            "group": group,
            "wave": wave,
            "operation_key": operation_key,
            "child_id": receipt.child_id,
            "terminal_receipt": receipt,
            **changes,
        }
        with pytest.raises(HarnessValidationError) as captured:
            TaskAttemptHistoryRecord(**values)
        assert captured.value.code == "task_plan_attempt_history_admission_mismatch"


def test_for_result_classifies_accepted_rejected_and_runtime_failed_candidates() -> None:
    plan, instance, _definition, _group, _wave, _operation_key = _fixture()
    accepted = _result(plan, instance)
    rejected = replace(
        _result(plan, instance, status=TaskLifecycle.FAILED),
        error_code="gate_rejected",
    )
    failed = replace(rejected, error_code="task_worker_failed")

    assert TaskAttemptHistoryRecord.for_result(plan, accepted).outcome is TaskAttemptOutcome.ACCEPTED
    assert TaskAttemptHistoryRecord.for_result(plan, rejected).outcome is TaskAttemptOutcome.REJECTED
    assert TaskAttemptHistoryRecord.for_result(plan, failed).outcome is TaskAttemptOutcome.FAILED


@pytest.mark.parametrize("status", [TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED])
def test_recovery_event_requires_prior_non_quarantined_history_for_active_candidate(status):
    plan, instance, _definition, group, wave, operation_key = _fixture()
    result = _result(plan, instance, status=status)
    receipt = _terminal_receipt(group, operation_key, status=ChildAgentState.SUCCEEDED, result=result)
    record = TaskAttemptHistoryRecord.for_result(plan, result, group=group, wave=wave,
                                                operation_key=operation_key, child_id=receipt.child_id,
                                                terminal_receipt=receipt)
    projection = TaskPlanScheduler().reserve_ready_tasks(_projection_for_plan(plan, sequence=1),
                                                        TaskPlanReadyDecision((instance,)))
    projection = TaskPlanScheduler.mark_dispatched(projection, instance)
    payload = {"recovery_outcome": "receipts_reconciled", "recovered_results": [{
        "task_id": result.task_id, "task_instance_id": result.task_instance_id,
        "attempt": result.attempt, "status": result.status.value, "result_checksum": result.result_checksum,
    }]}
    event = SimpleNamespace(event_type="TASK_GROUP_RECOVERY", sequence=5)
    group_snapshot = group.transitioned(DispatchGroupState.RUNNING).to_dict()
    _apply_parallel_recovery(payload, dict(group_snapshot), projection, event, {record.record_checksum: record})
    assert projection.tasks[0].status is TaskLifecycle.DISPATCHED
    quarantined = replace(record, outcome=TaskAttemptOutcome.QUARANTINED, reason_code="late_result")
    for history in ({}, {quarantined.record_checksum: quarantined}):
        with pytest.raises(HarnessValidationError) as rejected:
            _apply_parallel_recovery(payload, dict(group_snapshot), projection, event, history)
        assert rejected.value.code == "task_plan_replay_parallel_mismatch"


@pytest.mark.parametrize(
    "outcome,result_status",
    (
        (TaskAttemptOutcome.ACCEPTED, TaskLifecycle.FAILED),
        (TaskAttemptOutcome.REJECTED, TaskLifecycle.SUCCEEDED),
        (TaskAttemptOutcome.CANCELLED, TaskLifecycle.SUCCEEDED),
        (TaskAttemptOutcome.INDETERMINATE, TaskLifecycle.FAILED),
        (TaskAttemptOutcome.RECLAIMED, TaskLifecycle.FAILED),
    ),
)
def test_outcome_rejects_incompatible_candidate_result(outcome, result_status) -> None:
    plan, instance, definition, _group, _wave, _operation_key = _fixture()
    result = _result(plan, instance, status=result_status)

    with pytest.raises(HarnessValidationError) as captured:
        TaskAttemptHistoryRecord(
            instance=instance,
            binding_checksum=definition.binding_checksum,
            outcome=outcome,
            result=result,
        )

    assert captured.value.code == "task_plan_attempt_history_outcome_mismatch"


def test_result_identity_and_binding_are_checked_against_the_exact_instance() -> None:
    plan, instance, definition, _group, _wave, _operation_key = _fixture()
    result = _result(plan, instance)

    for changed in (
        replace(result, task_instance_id="different-instance"),
        replace(result, binding_checksum="sha256:" + "0" * 64),
        replace(result, worker_ref="different-worker@1"),
    ):
        with pytest.raises(HarnessValidationError) as captured:
            TaskAttemptHistoryRecord(
                instance=instance,
                binding_checksum=definition.binding_checksum,
                outcome=TaskAttemptOutcome.ACCEPTED,
                result=changed,
            )
        assert captured.value.code == "task_plan_attempt_history_identity_mismatch"


def test_parallel_admission_checks_group_wave_reservation_and_operation_identity() -> None:
    plan, instance, definition, group, wave, operation_key = _fixture()
    result = _result(plan, instance)
    receipt = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.SUCCEEDED,
        result=result,
    )
    wrong_reservation = replace(
        wave.reservations[0],
        idempotency_key="different-reservation",
    )
    wrong_wave = replace(wave, reservations=(wrong_reservation,))

    invalid_values = (
        {"group": replace(group, plan_id="different-plan")},
        {"wave": wrong_wave},
        {"operation_key": "parallel:different-operation"},
    )
    for changes in invalid_values:
        values = {
            "instance": instance,
            "binding_checksum": definition.binding_checksum,
            "outcome": TaskAttemptOutcome.ACCEPTED,
            "result": result,
            "group": group,
            "wave": wave,
            "operation_key": operation_key,
            "child_id": receipt.child_id,
            "terminal_receipt": receipt,
            **changes,
        }
        with pytest.raises(HarnessValidationError):
            TaskAttemptHistoryRecord(**values)


def test_successful_child_receipt_must_checksum_bind_the_exact_task_result() -> None:
    plan, instance, definition, group, wave, operation_key = _fixture()
    result = _result(plan, instance)
    other_result = replace(result, result_ref="result://different")
    receipt = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.SUCCEEDED,
        result=other_result,
    )

    with pytest.raises(HarnessValidationError) as captured:
        TaskAttemptHistoryRecord(
            instance=instance,
            binding_checksum=definition.binding_checksum,
            outcome=TaskAttemptOutcome.ACCEPTED,
            result=result,
            group=group,
            wave=wave,
            operation_key=operation_key,
            child_id=receipt.child_id,
            terminal_receipt=receipt,
        )

    assert captured.value.code == "task_plan_attempt_history_receipt_mismatch"


def test_supervised_cancel_reclaim_and_runtime_failure_require_matching_receipts() -> None:
    _plan, instance, definition, group, wave, operation_key = _fixture()
    cancelled = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.CANCELLED,
    )
    reclaimed = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.LOST,
        reason_code="child_lease_expired",
    )
    failed = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.FAILED,
    )

    for outcome, receipt in (
        (TaskAttemptOutcome.CANCELLED, cancelled),
        (TaskAttemptOutcome.RECLAIMED, reclaimed),
        (TaskAttemptOutcome.FAILED, failed),
    ):
        record = TaskAttemptHistoryRecord(
            instance=instance,
            binding_checksum=definition.binding_checksum,
            outcome=outcome,
            group=group,
            wave=wave,
            operation_key=operation_key,
            child_id=receipt.child_id,
            terminal_receipt=receipt,
            reason_code=receipt.reason_code,
        )
        assert record.outcome is outcome

    for outcome, receipt in (
        (TaskAttemptOutcome.CANCELLED, failed),
        (TaskAttemptOutcome.RECLAIMED, cancelled),
        (TaskAttemptOutcome.FAILED, cancelled),
    ):
        with pytest.raises(HarnessValidationError):
            TaskAttemptHistoryRecord(
                instance=instance,
                binding_checksum=definition.binding_checksum,
                outcome=outcome,
                group=group,
                wave=wave,
                operation_key=operation_key,
                child_id=receipt.child_id,
                terminal_receipt=receipt,
            )


def test_indeterminate_then_quarantined_is_valid_as_two_facts_for_one_attempt() -> None:
    plan, instance, definition, group, wave, operation_key = _fixture()
    late_result = _result(plan, instance)
    late_receipt = _terminal_receipt(
        group,
        operation_key,
        status=ChildAgentState.SUCCEEDED,
        result=late_result,
    )
    indeterminate = TaskAttemptHistoryRecord(
        instance=instance,
        binding_checksum=definition.binding_checksum,
        outcome=TaskAttemptOutcome.INDETERMINATE,
        group=group,
        wave=wave,
        operation_key=operation_key,
        child_id="child-1",
        reason_code="termination_unconfirmed",
    )
    quarantined = TaskAttemptHistoryRecord(
        instance=instance,
        binding_checksum=definition.binding_checksum,
        outcome=TaskAttemptOutcome.QUARANTINED,
        result=late_result,
        group=group,
        wave=wave,
        operation_key=operation_key,
        child_id=late_receipt.child_id,
        terminal_receipt=late_receipt,
        reason_code="late_after_indeterminate",
    )

    assert indeterminate.task_instance_id == quarantined.task_instance_id
    assert indeterminate.attempt == quarantined.attempt
    assert indeterminate.record_checksum != quarantined.record_checksum


def test_static_attempt_rejects_child_identity_and_nested_checksum_tamper() -> None:
    plan, instance, definition, _group, _wave, _operation_key = _fixture()
    result = _result(plan, instance)
    with pytest.raises(HarnessValidationError) as captured:
        TaskAttemptHistoryRecord(
            instance=instance,
            binding_checksum=definition.binding_checksum,
            outcome=TaskAttemptOutcome.ACCEPTED,
            result=result,
            child_id="fabricated-child",
        )
    assert captured.value.code == "task_plan_attempt_history_admission_mismatch"

    record = TaskAttemptHistoryRecord.for_result(plan, result)
    tampered = record.to_dict()
    tampered["reason_code"] = "changed"
    with pytest.raises(HarnessValidationError) as checksum_error:
        TaskAttemptHistoryRecord.from_dict(tampered)
    assert checksum_error.value.code == "task_plan_attempt_history_checksum_mismatch"
