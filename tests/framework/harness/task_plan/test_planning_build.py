from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.planning_build import (
    PLAN_BUILD_INTENT, PLAN_BUILD_RECEIPT, PlanBuildAttempt, validate_build_history,
)


def _intent():
    return PlanBuildAttempt(
        request_checksum="sha256:" + "1" * 64,
        policy_checksum="sha256:" + "2" * 64,
        attempt=1, max_calls=2, timeout_ms=30000,
    )


def _event(attempt):
    return SimpleNamespace(
        event_type=PLAN_BUILD_INTENT if attempt.status == "STARTED" else PLAN_BUILD_RECEIPT,
        payload={"build_attempt": attempt.to_dict()},
        plan_id=None, task_id=None, task_instance_id=None,
    )


def test_failed_attempt_remains_in_history_before_bounded_retry():
    first = _intent()
    failed = replace(first, status="FAILED", reason_code="provider_unavailable", elapsed_ms=100, retryable=True)
    second = replace(first, attempt=2)
    succeeded = replace(second, status="SUCCEEDED", candidate_checksum="sha256:" + "3" * 64, elapsed_ms=200)
    candidate_event = SimpleNamespace(event_type="PLAN_CANDIDATE_BUILT", payload={"candidate_ref": succeeded.candidate_checksum})
    events = (_event(first), _event(failed), _event(second), candidate_event, _event(succeeded))
    assert validate_build_history(events) == (failed, succeeded)
    assert PlanBuildAttempt.from_dict(succeeded.to_dict()) == succeeded


@pytest.mark.parametrize("status", ["STARTED", "SUCCEEDED", "TIMED_OUT"])
def test_retry_cannot_follow_unconfirmed_successful_or_timed_out_call(status):
    intent = _intent()
    events = [_event(intent)]
    if status == "SUCCEEDED":
        events.append(_event(replace(intent, status=status, candidate_checksum="sha256:" + "3" * 64)))
    elif status == "TIMED_OUT":
        events.append(_event(replace(intent, status=status, reason_code="planning_timeout", elapsed_ms=30001)))
    events.append(_event(replace(intent, attempt=2)))
    with pytest.raises(HarnessValidationError):
        validate_build_history(tuple(events))


@pytest.mark.parametrize("mutation", [
    {"request_checksum": "sha256:" + "4" * 64},
    {"policy_checksum": "sha256:" + "4" * 64},
    {"max_calls": 3}, {"timeout_ms": 31000}, {"attempt": 2},
])
def test_recomputed_receipt_checksum_cannot_change_attempt_identity(mutation):
    intent = _intent()
    receipt = replace(intent, status="FAILED", reason_code="provider_error", **mutation)
    with pytest.raises(HarnessValidationError):
        validate_build_history((_event(intent), _event(receipt)))


def test_missing_intent_duplicate_receipt_and_call_exhaustion_are_rejected():
    intent = _intent()
    receipt = replace(intent, status="FAILED", reason_code="provider_error")
    with pytest.raises(HarnessValidationError):
        validate_build_history((_event(receipt),))
    with pytest.raises(HarnessValidationError):
        validate_build_history((_event(intent), _event(receipt), _event(receipt)))
    with pytest.raises(HarnessValidationError):
        replace(intent, attempt=3)
    with pytest.raises(HarnessValidationError):
        replace(intent, status="SUCCEEDED", candidate_checksum="sha256:" + "3" * 64, elapsed_ms=30001)
