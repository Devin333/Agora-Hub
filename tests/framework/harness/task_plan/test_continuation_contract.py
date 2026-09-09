from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.continuation import (
    PARENT_CONTINUATION_EVENT,
    ParentContinuation,
    validate_parent_continuation_append,
)


def _continuation(**changes: object) -> ParentContinuation:
    base = ParentContinuation(
        run_id="run-1",
        stage_id="stage-1",
        parent_turn_id="turn-1",
        observation_id="observation-1",
        observation_version=1,
        group_id="group-1",
        observation_checksum="sha256:" + "1" * 64,
        submission_id="submission-1",
    )
    return replace(base, **changes)


def _event(value: ParentContinuation) -> dict[str, object]:
    return {"event_type": PARENT_CONTINUATION_EVENT, "payload": {"continuation": value.to_dict()}}


def test_continuation_checksum_roundtrip_and_identity() -> None:
    value = _continuation()
    assert ParentContinuation.from_dict(value.to_dict()) == value
    assert value.identity_key() == ("run-1", "stage-1", "observation-1", 1)


def test_identical_redelivery_is_idempotent() -> None:
    event = _event(_continuation())
    validate_parent_continuation_append((event,), (event,))


def test_pending_to_delivered_keeps_the_same_observation_identity() -> None:
    pending = _continuation(status="PENDING", group_state="JOINING")
    delivered = _continuation(status="DELIVERED", group_state="SUCCEEDED")
    validate_parent_continuation_append((_event(pending),), (_event(delivered),))


def test_conflicting_same_observation_version_is_rejected() -> None:
    first = _continuation()
    second = _continuation(observation_checksum="sha256:" + "2" * 64)
    with pytest.raises(HarnessValidationError, match="conflicting content"):
        validate_parent_continuation_append((_event(first),), (_event(second),))


def test_observation_version_regression_is_rejected() -> None:
    first = _continuation(observation_version=2)
    second = _continuation(observation_version=1)
    with pytest.raises(HarnessValidationError, match="version regressed"):
        validate_parent_continuation_append((_event(first),), (_event(second),))


def test_observation_version_gap_is_rejected() -> None:
    first = _continuation(observation_version=1)
    third = _continuation(observation_version=3)
    with pytest.raises(HarnessValidationError, match="not contiguous"):
        validate_parent_continuation_append((_event(first),), (_event(third),))


def test_another_parent_turn_has_an_independent_version_sequence() -> None:
    first = _continuation(observation_id="observation-parent-one", parent_turn_id="turn-one")
    other = _continuation(observation_id="observation-parent-two", parent_turn_id="turn-two")
    validate_parent_continuation_append((_event(first),), (_event(other),))


def test_pending_to_terminal_cannot_change_submission_identity() -> None:
    pending = _continuation(status="PENDING", group_state="JOINING", submission_id="submission-one")
    delivered = _continuation(status="DELIVERED", group_state="SUCCEEDED", submission_id="submission-two")
    with pytest.raises(HarnessValidationError, match="conflicting content"):
        validate_parent_continuation_append((_event(pending),), (_event(delivered),))


def test_delivered_continuation_requires_terminal_group_state() -> None:
    with pytest.raises(HarnessValidationError, match="terminal group state"):
        _continuation(status="DELIVERED", group_state="RUNNING")
