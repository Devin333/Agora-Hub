from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan import (
    TaskPlanCheckpoint,
    TaskPlanReplayReducer,
    task_instance_for_attempt,
)
from framework.harness.task_plan.attempt_history import TaskAttemptOutcome
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.continuation import ParentContinuation
from framework.harness.task_plan.parallel import ParentObservation
from tests.framework.harness.task_plan.test_task_plan_recovery import (
    _attempt_history_record,
    _admission_events,
    _history_fixture,
)


def _checkpoint_with_history() -> dict:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,), (*base_events, *_admission_events(plan, instance, sequence=3))
    )
    record = _attempt_history_record(plan, instance, TaskAttemptOutcome.ACCEPTED)
    report = replace(report, attempt_history=(record,))
    checkpoint = TaskPlanCheckpoint.from_replay(
        "evidence-checkpoint",
        plan,
        report,
        created_at="2026-08-01T00:00:01Z",
    )
    return checkpoint.to_dict()


def _checkpoint_with_observation() -> dict:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,), (*base_events, *_admission_events(plan, instance, sequence=3))
    )
    observation = ParentObservation(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        plan_version=plan.version,
        group_id="group-1",
        group_state="SUCCEEDED",
        task_summaries=(),
    )
    observation_checksum = observation.observation_checksum
    report = replace(
        report,
        parallel_diagnostics=(
            {
                "event_type": "TASK_GROUP_JOINED",
                "observation": observation.to_dict(),
            },
        ),
        parallel_event_sequence=1,
        observation_checksum=observation_checksum,
    )
    return TaskPlanCheckpoint.from_replay(
        "observation-checkpoint",
        plan,
        report,
        created_at="2026-08-01T00:00:01Z",
    ).to_dict()


def _checkpoint_with_continuation() -> dict:
    plan, base_events, _worker = _history_fixture()
    instance = task_instance_for_attempt(plan, "recover-task", 1)
    report = TaskPlanReplayReducer().replay(
        (plan,), (*base_events, *_admission_events(plan, instance, sequence=3))
    )
    continuation = ParentContinuation(
        run_id=plan.run_id,
        stage_id=plan.stage_id,
        parent_turn_id="parent-turn-1",
        observation_id="observation-1",
        observation_version=1,
        group_id="group-1",
        observation_checksum="sha256:" + "b" * 64,
    )
    report = replace(
        report,
        continuation=continuation.to_dict(),
        parallel_groups={"group-1": {"group_id": "group-1", "state": "RUNNING"}},
        parallel_event_sequence=1,
    )
    return TaskPlanCheckpoint.from_replay(
        "continuation-checkpoint",
        plan,
        report,
        created_at="2026-08-01T00:00:01Z",
    ).to_dict()


def _with_recomputed_checkpoint_checksum(payload: dict) -> dict:
    payload = deepcopy(payload)
    payload.pop("checkpoint_checksum", None)
    payload["checkpoint_checksum"] = canonical_payload_checksum(payload)
    return payload


def test_checkpoint_rejects_wrong_history_index_checksum() -> None:
    payload = _checkpoint_with_history()
    payload["history_index_checksum"] = "sha256:" + "f" * 64

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanCheckpoint.from_dict(_with_recomputed_checkpoint_checksum(payload))

    assert exc_info.value.code == "task_plan_attempt_history_index_checksum_mismatch"


def test_checkpoint_rejects_missing_history_index_checksum() -> None:
    payload = _checkpoint_with_history()
    payload.pop("history_index_checksum")

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanCheckpoint.from_dict(_with_recomputed_checkpoint_checksum(payload))

    assert exc_info.value.code == "task_plan_checkpoint_history_index_missing"


def test_checkpoint_rejects_wrong_observation_checksum() -> None:
    payload = _checkpoint_with_observation()
    payload["observation_checksum"] = "sha256:" + "f" * 64

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanCheckpoint.from_dict(_with_recomputed_checkpoint_checksum(payload))

    assert exc_info.value.code == "task_plan_observation_checksum_mismatch"


def test_checkpoint_rejects_budget_ledger_that_differs_from_projection() -> None:
    payload = _checkpoint_with_history()
    ledger = TaskPlanBudgetLedger.from_snapshot(payload["budget_ledger"])
    instance = next(iter(ledger.records.values()))["instance"]
    altered = ledger.release_unstarted(
        instance["task_instance_id"],
        instance["task_id"],
        instance["attempt"],
        reason_code="tampered",
    )
    payload["budget_ledger"] = altered.snapshot()

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanCheckpoint.from_dict(_with_recomputed_checkpoint_checksum(payload))

    assert exc_info.value.code == "task_plan_checkpoint_budget_mismatch"


def test_checkpoint_rejects_malformed_continuation() -> None:
    payload = _checkpoint_with_continuation()
    payload["continuation"].pop("group_id")

    with pytest.raises(HarnessValidationError) as exc_info:
        TaskPlanCheckpoint.from_dict(_with_recomputed_checkpoint_checksum(payload))

    assert exc_info.value.code == "invalid_task_plan_payload_fields"
