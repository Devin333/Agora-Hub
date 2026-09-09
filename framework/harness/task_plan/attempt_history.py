"""Immutable, checksum-bound history for one physical TaskPlan attempt.

``TaskResultRecord`` remains the verified candidate-result contract.  This
module records the wider attempt lifecycle without fabricating result payloads
for cancellation, reclaim, or an outcome that is still indeterminate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import (
    canonical_payload_checksum,
    checksum,
    exact_keys,
    frozen_mapping,
    identifier,
    optional_text,
    thaw_mapping,
)
from framework.harness.task_plan.models import (
    TaskInstance,
    TaskLifecycle,
    ValidatedTaskPlan,
)
from framework.harness.task_plan.scheduler import task_instance_for_attempt

if TYPE_CHECKING:
    from framework.harness.task_plan.store import TaskResultRecord


TASK_ATTEMPT_HISTORY_SCHEMA = "newsroom.harness-task-attempt-history/v1"


class TaskAttemptOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INDETERMINATE = "INDETERMINATE"
    RECLAIMED = "RECLAIMED"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class TaskAttemptHistoryRecord:
    """One append-only lifecycle fact for an allocated physical attempt.

    A single attempt may legitimately have more than one record over time,
    for example ``INDETERMINATE`` followed by ``QUARANTINED`` after a late
    terminal receipt.  Cross-record ordering and conflict rules therefore
    belong to the durable store/reducer rather than this value object.
    """

    instance: TaskInstance | Mapping[str, Any]
    binding_checksum: str
    outcome: TaskAttemptOutcome | str
    result: TaskResultRecord | Mapping[str, Any] | None = None
    group: Mapping[str, Any] | None = None
    wave: Mapping[str, Any] | None = None
    operation_key: str | None = None
    child_id: str | None = None
    terminal_receipt: Mapping[str, Any] | None = None
    recovered_from: str | None = None
    recovery_receipt: Mapping[str, Any] | None = None
    reason_code: str | None = None
    schema_version: str = TASK_ATTEMPT_HISTORY_SCHEMA
    record_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != TASK_ATTEMPT_HISTORY_SCHEMA:
            raise HarnessValidationError(
                "TaskAttemptHistoryRecord schema is unsupported",
                code="task_plan_attempt_history_schema_unsupported",
            )

        instance = _task_instance(self.instance)
        binding_checksum = checksum(self.binding_checksum, "binding_checksum")
        try:
            outcome = TaskAttemptOutcome(self.outcome)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "TaskPlan attempt history outcome is invalid",
                code="task_plan_attempt_history_invalid",
            ) from exc
        result = _task_result(self.result)
        group, group_snapshot = _dispatch_group(self.group)
        wave, wave_snapshot = _dispatch_wave(self.wave)
        operation_key = (
            identifier(self.operation_key, "operation_key")
            if self.operation_key is not None
            else None
        )
        child_id = (
            identifier(self.child_id, "child_id")
            if self.child_id is not None
            else None
        )
        terminal_receipt, receipt_snapshot = _child_terminal_receipt(
            self.terminal_receipt
        )
        recovered_from = checksum(self.recovered_from, "recovered_from") if self.recovered_from is not None else None
        recovery_receipt = _subagent_recovery_receipt(self.recovery_receipt)
        if (recovered_from is None) != (recovery_receipt is None):
            raise HarnessValidationError("subagent recovery requires source and receipt", code="task_plan_attempt_recovery_evidence_required")
        reason_code = optional_text(self.reason_code, "reason_code")

        if result is not None:
            _validate_result_identity(
                result,
                instance=instance,
                binding_checksum=binding_checksum,
            )
        _validate_outcome(outcome, result)
        _validate_admission_identity(
            instance=instance,
            binding_checksum=binding_checksum,
            group=group,
            wave=wave,
            operation_key=operation_key,
            child_id=child_id,
            terminal_receipt=terminal_receipt,
            result=result,
            outcome=outcome,
            recovery_receipt=recovery_receipt,
        )

        object.__setattr__(self, "instance", instance)
        object.__setattr__(self, "binding_checksum", binding_checksum)
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "result", result)
        object.__setattr__(self, "group", group_snapshot)
        object.__setattr__(self, "wave", wave_snapshot)
        object.__setattr__(self, "operation_key", operation_key)
        object.__setattr__(self, "child_id", child_id)
        object.__setattr__(self, "terminal_receipt", receipt_snapshot)
        object.__setattr__(self, "recovered_from", recovered_from)
        object.__setattr__(self, "recovery_receipt", frozen_mapping(recovery_receipt.to_dict(), "recovery_receipt") if recovery_receipt else None)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(
            self,
            "record_checksum",
            canonical_payload_checksum(self.checksum_projection()),
        )

    @property
    def run_id(self) -> str:
        return self.instance.run_id

    @property
    def stage_id(self) -> str:
        return self.instance.stage_id

    @property
    def plan_id(self) -> str:
        return self.instance.plan_id

    @property
    def plan_version(self) -> int:
        return self.instance.plan_version

    @property
    def task_id(self) -> str:
        return self.instance.task_id

    @property
    def task_instance_id(self) -> str:
        return self.instance.task_instance_id

    @property
    def attempt(self) -> int:
        return self.instance.attempt

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "instance": self.instance.to_dict(),
            "binding_checksum": self.binding_checksum,
            "outcome": self.outcome.value,
            "result": self.result.to_dict() if self.result is not None else None,
            "group": thaw_mapping(self.group) if self.group is not None else None,
            "wave": thaw_mapping(self.wave) if self.wave is not None else None,
            "operation_key": self.operation_key,
            "child_id": self.child_id,
            "terminal_receipt": (
                thaw_mapping(self.terminal_receipt)
                if self.terminal_receipt is not None
                else None
            ),
            "reason_code": self.reason_code,
            **({
                "recovered_from": self.recovered_from,
                "recovery_receipt": thaw_mapping(self.recovery_receipt),
            } if self.recovered_from is not None else {}),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "record_checksum": self.record_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskAttemptHistoryRecord":
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "schema_version",
                    "instance",
                    "binding_checksum",
                    "outcome",
                    "result",
                    "group",
                    "wave",
                    "operation_key",
                    "child_id",
                    "terminal_receipt",
                    "reason_code",
                    "record_checksum",
                }
            ),
            optional=frozenset({"recovered_from", "recovery_receipt"}),
            model=cls.__name__,
        )
        supplied = checksum(payload.pop("record_checksum"), "record_checksum")
        record = cls(**payload)
        if supplied != record.record_checksum:
            raise HarnessValidationError(
                "TaskPlan attempt history checksum does not match canonical content",
                code="task_plan_attempt_history_checksum_mismatch",
            )
        return record

    @classmethod
    def for_result(
        cls,
        plan: ValidatedTaskPlan,
        result: TaskResultRecord,
        *,
        instance: TaskInstance | None = None,
        group: Mapping[str, Any] | None = None,
        wave: Mapping[str, Any] | None = None,
        operation_key: str | None = None,
        child_id: str | None = None,
        terminal_receipt: Mapping[str, Any] | None = None,
        recovered_from: str | None = None,
        recovery_receipt: Mapping[str, Any] | None = None,
    ) -> "TaskAttemptHistoryRecord":
        from framework.harness.task_plan.store import TaskResultRecord

        if not isinstance(plan, ValidatedTaskPlan):
            raise TypeError("plan must be a ValidatedTaskPlan")
        if not isinstance(result, TaskResultRecord):
            raise TypeError("result must be a TaskResultRecord")
        attempt_instance = instance or task_instance_for_attempt(
            plan,
            result.task_id,
            result.attempt,
            task_instance_id=result.task_instance_id,
        )
        if not isinstance(attempt_instance, TaskInstance):
            raise TypeError("instance must be a TaskInstance")
        if not attempt_instance.matches_plan_identity(plan):
            raise HarnessValidationError(
                "TaskPlan attempt instance does not match accepted plan",
                code="task_plan_attempt_history_identity_mismatch",
            )
        definition = next(
            (item for item in plan.tasks if item.task_id == attempt_instance.task_id),
            None,
        )
        if definition is None:
            raise HarnessValidationError(
                "TaskPlan attempt references an unknown task",
                code="task_plan_attempt_history_identity_mismatch",
            )
        if (
            definition.task_definition_checksum
            != attempt_instance.task_definition_checksum
            or definition.worker_ref != attempt_instance.worker_ref
            or result.binding_checksum != definition.binding_checksum
        ):
            raise HarnessValidationError(
                "TaskPlan attempt binding does not match accepted plan",
                code="task_plan_attempt_history_binding_mismatch",
            )
        outcome = (
            TaskAttemptOutcome.ACCEPTED
            if result.status is TaskLifecycle.SUCCEEDED
            else (
                TaskAttemptOutcome.FAILED
                if result.error_code == "task_worker_failed"
                else TaskAttemptOutcome.REJECTED
            )
        )
        return cls(
            instance=attempt_instance,
            binding_checksum=definition.binding_checksum,
            outcome=outcome,
            result=result,
            group=group,
            wave=wave,
            operation_key=operation_key,
            child_id=child_id,
            terminal_receipt=terminal_receipt,
            recovered_from=recovered_from,
            recovery_receipt=recovery_receipt,
            reason_code=result.error_code,
        )


def _task_instance(value: TaskInstance | Mapping[str, Any]) -> TaskInstance:
    if isinstance(value, TaskInstance):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("instance must be a TaskInstance")
    return TaskInstance.from_dict(value)


def _task_result(value: Any) -> TaskResultRecord | None:
    from framework.harness.task_plan.store import TaskResultRecord

    if value is None:
        return None
    if isinstance(value, TaskResultRecord):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("result must be a TaskResultRecord")
    return TaskResultRecord.from_dict(value)


def _dispatch_group(value: Any) -> tuple[Any | None, Mapping[str, Any] | None]:
    if value is None:
        return None, None
    from framework.harness.task_plan.parallel import DispatchGroup

    if isinstance(value, DispatchGroup):
        group = value
    elif isinstance(value, Mapping):
        group = DispatchGroup.from_dict(value)
    else:
        raise TypeError("group must be a DispatchGroup admission snapshot")
    return group, frozen_mapping(group.to_dict(), "group")


def _dispatch_wave(value: Any) -> tuple[Any | None, Mapping[str, Any] | None]:
    if value is None:
        return None, None
    from framework.harness.task_plan.parallel import DispatchWave

    if isinstance(value, DispatchWave):
        wave = value
    elif isinstance(value, Mapping):
        wave = DispatchWave.from_dict(thaw_mapping(value))
    else:
        raise TypeError("wave must be a DispatchWave admission snapshot")
    return wave, frozen_mapping(wave.to_dict(), "wave")


def _child_terminal_receipt(
    value: Any,
) -> tuple[Any | None, Mapping[str, Any] | None]:
    if value is None:
        return None, None
    from framework.harness.subagents.supervisor import ChildAgentTerminalReceipt

    if isinstance(value, ChildAgentTerminalReceipt):
        receipt = value
    elif isinstance(value, Mapping):
        payload = exact_keys(
            value,
            required=frozenset(
                {
                    "child_id",
                    "operation_id",
                    "parent_graph_identity",
                    "status",
                    "reason_code",
                    "result_ref",
                    "result_checksum",
                    "termination_confirmed",
                    "completed_at",
                    "receipt_checksum",
                }
            ),
            model="ChildAgentTerminalReceipt",
        )
        supplied = checksum(payload.pop("receipt_checksum"), "receipt_checksum")
        completed_at = payload.get("completed_at")
        if isinstance(completed_at, str):
            try:
                payload["completed_at"] = datetime.fromisoformat(completed_at)
            except ValueError as exc:
                raise HarnessValidationError(
                    "child terminal receipt timestamp is invalid",
                    code="task_plan_attempt_history_receipt_invalid",
                ) from exc
        try:
            receipt = ChildAgentTerminalReceipt(**payload)
        except (TypeError, ValueError) as exc:
            raise HarnessValidationError(
                "child terminal receipt is invalid",
                code="task_plan_attempt_history_receipt_invalid",
            ) from exc
        if supplied != receipt.receipt_checksum:
            raise HarnessValidationError(
                "child terminal receipt checksum is invalid",
                code="task_plan_attempt_history_receipt_invalid",
            )
    else:
        raise TypeError("terminal_receipt must be a ChildAgentTerminalReceipt")
    return receipt, frozen_mapping(receipt.to_dict(), "terminal_receipt")


def _subagent_recovery_receipt(value: Any) -> Any | None:
    if value is None:
        return None
    from framework.harness.subagents.transcript import SubAgentTranscriptReceipt
    if isinstance(value, SubAgentTranscriptReceipt):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("recovery_receipt must be a SubAgentTranscriptReceipt")
    return SubAgentTranscriptReceipt.from_dict(thaw_mapping(value))


def _validate_result_identity(
    result: TaskResultRecord,
    *,
    instance: TaskInstance,
    binding_checksum: str,
) -> None:
    identity_fields = (
        "run_id",
        "stage_id",
        "plan_id",
        "plan_version",
        "task_id",
        "task_instance_id",
        "attempt",
        "worker_ref",
    )
    graph_fields = (
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
    if (
        any(getattr(result, name) != getattr(instance, name) for name in identity_fields)
        or any(getattr(result, name) != getattr(instance, name) for name in graph_fields)
        or result.task_checksum != instance.task_definition_checksum
        or result.binding_checksum != binding_checksum
    ):
        raise HarnessValidationError(
            "TaskPlan attempt result identity does not match its instance and binding",
            code="task_plan_attempt_history_identity_mismatch",
        )


def _validate_outcome(
    outcome: TaskAttemptOutcome,
    result: TaskResultRecord | None,
) -> None:
    if outcome is TaskAttemptOutcome.ACCEPTED:
        valid = result is not None and result.status is TaskLifecycle.SUCCEEDED
    elif outcome is TaskAttemptOutcome.REJECTED:
        valid = result is not None and result.status is TaskLifecycle.FAILED
    elif outcome is TaskAttemptOutcome.FAILED:
        valid = result is None or result.status is TaskLifecycle.FAILED
    elif outcome is TaskAttemptOutcome.QUARANTINED:
        valid = True
    else:
        valid = result is None
    if not valid:
        raise HarnessValidationError(
            "TaskPlan attempt outcome and candidate result are inconsistent",
            code="task_plan_attempt_history_outcome_mismatch",
        )


def _validate_admission_identity(
    *,
    instance: TaskInstance,
    binding_checksum: str,
    group: Any | None,
    wave: Any | None,
    operation_key: str | None,
    child_id: str | None,
    terminal_receipt: Any | None,
    result: TaskResultRecord | None,
    outcome: TaskAttemptOutcome,
    recovery_receipt: Any | None = None,
) -> None:
    if (group is None) != (wave is None):
        raise HarnessValidationError(
            "TaskPlan attempt group and wave snapshots must be present together",
            code="task_plan_attempt_history_admission_mismatch",
        )
    if group is None:
        if any(value is not None for value in (operation_key, child_id, terminal_receipt, recovery_receipt)):
            raise HarnessValidationError(
                "static TaskPlan attempt cannot fabricate child receipt identity",
                code="task_plan_attempt_history_admission_mismatch",
            )
        return

    from framework.harness.task_plan.parallel_lifecycle import (
        DispatchGroupState,
        DispatchWaveState,
        ReservationState,
    )

    if (
        group.state is not DispatchGroupState.ADMITTED
        or wave.state is not DispatchWaveState.ADMITTED
        or wave.terminal_outcome is not None
        or any(
            reservation.state is not ReservationState.RESERVED
            for reservation in wave.reservations
        )
    ):
        raise HarnessValidationError(
            "TaskPlan attempt history must retain the immutable admission snapshots",
            code="task_plan_attempt_history_admission_mismatch",
        )

    graph_identity = group.parent_graph_identity
    if (
        group.run_id != instance.run_id
        or group.stage_id != instance.stage_id
        or group.plan_id != instance.plan_id
        or group.plan_version != instance.plan_version
        or group.plan_checksum != instance.plan_checksum
        or instance.task_id not in group.task_ids
        or graph_identity.run_id != instance.run_id
        or graph_identity.graph_id != instance.graph_id
        or graph_identity.graph_version != instance.graph_version
        or graph_identity.graph_ref != instance.graph_ref
        or graph_identity.graph_checksum != instance.graph_checksum
        or wave.group_id != group.group_id
        or instance.task_id not in wave.task_ids
    ):
        raise HarnessValidationError(
            "TaskPlan attempt group or wave does not match its accepted identity",
            code="task_plan_attempt_history_admission_mismatch",
        )
    reservations = [
        item for item in wave.reservations if item.task_id == instance.task_id
    ]
    if (
        len(reservations) != 1
        or reservations[0].idempotency_key != instance.idempotency_key
        or dict(reservations[0].budget) != instance.budget_snapshot.to_dict()
    ):
        raise HarnessValidationError(
            "TaskPlan attempt wave reservation does not match its instance",
            code="task_plan_attempt_history_reservation_mismatch",
        )

    if wave.execution_mode != "SUPERVISED":
        if any(value is not None for value in (operation_key, child_id, terminal_receipt, recovery_receipt)):
            raise HarnessValidationError(
                "non-supervised TaskPlan attempt cannot fabricate child receipt identity",
                code="task_plan_attempt_history_admission_mismatch",
            )
        return

    from framework.harness.subagents.supervisor import ChildAgentState
    from framework.harness.task_plan.parallel import spawn_operation_key

    expected_operation_key = spawn_operation_key(
        group.group_id,
        wave.wave_id,
        instance.task_instance_id,
        instance.attempt,
    )
    if operation_key != expected_operation_key:
        raise HarnessValidationError(
            "TaskPlan attempt operation key does not match group/wave admission",
            code="task_plan_attempt_history_admission_mismatch",
        )
    if terminal_receipt is not None:
        if (
            child_id is None
            or terminal_receipt.child_id != child_id
            or terminal_receipt.operation_id != operation_key
            or terminal_receipt.parent_graph_identity != group.parent_graph_identity
        ):
            raise HarnessValidationError(
                "TaskPlan attempt terminal receipt identity does not match admission",
                code="task_plan_attempt_history_receipt_mismatch",
            )
        if terminal_receipt.status is ChildAgentState.SUCCEEDED:
            if result is None and outcome is not TaskAttemptOutcome.QUARANTINED:
                raise HarnessValidationError(
                    "successful child receipt requires its recorded task result",
                    code="task_plan_attempt_history_receipt_mismatch",
                )
            if result is not None:
                expected_result_checksum = canonical_payload_checksum(
                    {
                        "task_result": result.to_dict(),
                        "task_result_checksum": result.result_checksum,
                    }
                )
                if terminal_receipt.result_checksum != expected_result_checksum:
                    raise HarnessValidationError(
                        "child terminal receipt payload does not match task result",
                        code="task_plan_attempt_history_receipt_mismatch",
                    )

    if recovery_receipt is not None:
        if (
            outcome not in {TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.REJECTED, TaskAttemptOutcome.FAILED}
            or result is None or result.status not in {TaskLifecycle.SUCCEEDED, TaskLifecycle.FAILED}
            or terminal_receipt is None
            or terminal_receipt.status is not ChildAgentState.FAILED
            or not terminal_receipt.termination_confirmed
            or recovery_receipt.parent_run_id != instance.run_id
            or recovery_receipt.task_instance_id != instance.task_instance_id
            or recovery_receipt.attempt != instance.attempt
            or recovery_receipt.transcript_ref != result.transcript_ref
            or recovery_receipt.transcript_checksum != result.transcript_checksum
            or recovery_receipt.output_ref != result.subagent_output_ref
            or recovery_receipt.output_checksum != result.subagent_output_checksum
        ):
            raise HarnessValidationError("subagent recovery receipt does not match result", code="task_plan_attempt_recovery_evidence_mismatch")
        return
    if outcome in {TaskAttemptOutcome.ACCEPTED, TaskAttemptOutcome.REJECTED}:
        if (
            terminal_receipt is None
            or terminal_receipt.status is not ChildAgentState.SUCCEEDED
            or not terminal_receipt.termination_confirmed
        ):
            raise HarnessValidationError(
                "verified supervised result requires a confirmed successful child receipt",
                code="task_plan_attempt_history_receipt_required",
            )
    elif outcome is TaskAttemptOutcome.FAILED:
        if result is None:
            if (
                terminal_receipt is None
                or terminal_receipt.status is not ChildAgentState.FAILED
                or not terminal_receipt.termination_confirmed
            ):
                raise HarnessValidationError(
                    "runtime-failed supervised attempt requires a confirmed failed receipt",
                    code="task_plan_attempt_history_receipt_required",
                )
        elif (
            terminal_receipt is None
            or terminal_receipt.status is not ChildAgentState.SUCCEEDED
            or not terminal_receipt.termination_confirmed
        ):
            raise HarnessValidationError(
                "failed supervised result requires its confirmed child result receipt",
                code="task_plan_attempt_history_receipt_required",
            )
    elif outcome is TaskAttemptOutcome.CANCELLED:
        if (
            terminal_receipt is None
            or terminal_receipt.status is not ChildAgentState.CANCELLED
            or not terminal_receipt.termination_confirmed
        ):
            raise HarnessValidationError(
                "cancelled supervised attempt requires a confirmed cancelled receipt",
                code="task_plan_attempt_history_receipt_required",
            )
    elif outcome is TaskAttemptOutcome.RECLAIMED:
        if (
            terminal_receipt is None
            or terminal_receipt.status is not ChildAgentState.LOST
            or not terminal_receipt.termination_confirmed
            or terminal_receipt.reason_code != "child_lease_expired"
        ):
            raise HarnessValidationError(
                "reclaimed supervised attempt requires a confirmed lease-expiry receipt",
                code="task_plan_attempt_history_receipt_required",
            )
    elif outcome is TaskAttemptOutcome.INDETERMINATE:
        if (
            terminal_receipt is not None
            and terminal_receipt.status is not ChildAgentState.LOST
        ):
            raise HarnessValidationError(
                "indeterminate attempt receipt must describe a lost child",
                code="task_plan_attempt_history_receipt_mismatch",
            )
    elif outcome is TaskAttemptOutcome.QUARANTINED and result is not None:
        if terminal_receipt is not None and terminal_receipt.status is not ChildAgentState.SUCCEEDED:
            raise HarnessValidationError(
                "quarantined candidate result requires its successful child receipt",
                code="task_plan_attempt_history_receipt_mismatch",
            )


__all__ = [
    "TASK_ATTEMPT_HISTORY_SCHEMA",
    "TaskAttemptHistoryRecord",
    "TaskAttemptOutcome",
]
