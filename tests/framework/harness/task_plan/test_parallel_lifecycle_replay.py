from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan import TaskLifecycle
from framework.harness.task_plan.capacity import CapacityPool, TaskCapacityDemand
from framework.harness.task_plan.parallel import (
    JoinPolicy,
    ParallelAgentCoordinator,
    ParallelEventSink,
    SerialTaskExecutorAdapter,
)
from framework.harness.task_plan.replay import (
    _apply_parallel_event,
    _projection_for_plan,
    _validate_parallel_report_projection,
)
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _request,
    _result,
)


def _event(value: dict[str, object], sequence: int) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=value["event_type"],
        payload=value,
        sequence=sequence,
    )


def _reserved_projection(plan):
    return TaskPlanScheduler().reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1), TaskPlanReadyDecision(_request(plan).task_instances),
    )


def _coordinator_events(*, status: TaskLifecycle = TaskLifecycle.SUCCEEDED, with_capacity: bool = False):
    plan = _accepted_parallel_plan(("task-1",))
    request = replace(_request(plan, join_policy=JoinPolicy.WAIT_ALL), serial_fallback=True)
    if with_capacity:
        request = replace(
            request,
            capacity_pools=(CapacityPool("cpu", 1, policy_version="policy-v1"),),
            task_capacity_demands={"task-1": TaskCapacityDemand("task-1", {"cpu": 1})},
        )
    events: list[dict[str, object]] = []
    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    coordinator.dispatch(request, lambda instance: _result(plan, instance, status=status))
    return plan, events


def test_capacity_policy_evidence_survives_terminal_replay() -> None:
    plan, raw_events = _coordinator_events(with_capacity=True)
    projection = _reserved_projection(plan)
    groups: dict[str, dict[str, object]] = {}
    waves: dict[str, dict[str, object]] = {}
    reservations: dict[str, dict[str, object]] = {}
    diagnostics: list[dict[str, object]] = []
    relevant = [
        item for item in raw_events
        if item["event_type"] in {
            "TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED", "TASK_WAVE_DISPATCHED", "TASK_WAVE_COMPLETED",
        }
    ]
    for sequence, item in enumerate(relevant, start=1):
        _apply_parallel_event(
            _event(item, sequence), projection, groups, waves, reservations, diagnostics,
        )
    reservation = next(iter(reservations.values()))
    assert reservation["capacity_allocations"] == {"cpu": 1}
    assert reservation["capacity_policy_checksums"]["cpu"].startswith("sha256:")
    _validate_parallel_report_projection(groups, waves, reservations)


def test_coordinator_wave_events_replay_to_terminal_reservation_checksum() -> None:
    plan, raw_events = _coordinator_events()
    projection = _reserved_projection(plan)
    groups: dict[str, dict[str, object]] = {}
    waves: dict[str, dict[str, object]] = {}
    reservations: dict[str, dict[str, object]] = {}
    diagnostics: list[dict[str, object]] = []

    relevant = [
        item
        for item in raw_events
        if item["event_type"] in {
            "TASK_GROUP_ADMITTED",
            "TASK_WAVE_ADMITTED",
            "TASK_WAVE_DISPATCHED",
            "TASK_WAVE_COMPLETED",
        }
    ]
    for sequence, item in enumerate(relevant, start=1):
        _apply_parallel_event(
            _event(item, sequence),
            projection,
            groups,
            waves,
            reservations,
            diagnostics,
        )

    assert groups
    assert all(item["state"] == "TERMINAL" for item in waves.values())
    assert all(item["state"] == "CONSUMED" for item in reservations.values())
    for reservation in reservations.values():
        expected = reservation["reservation_checksum"]
        _validate_parallel_report_projection(groups, waves, reservations)
        assert isinstance(expected, str)
        assert expected.startswith("sha256:")
    embedded = next(iter(waves.values()))["reservations"][0]
    assert embedded["state"] == "CONSUMED"
    assert embedded["reservation_checksum"] == next(iter(reservations.values()))["reservation_checksum"]


def test_replay_rejects_group_terminal_event_with_wrong_snapshot_target() -> None:
    plan, raw_events = _coordinator_events()
    projection = _reserved_projection(plan)
    group_event = next(item for item in raw_events if item["event_type"] == "TASK_GROUP_ADMITTED")
    groups: dict[str, dict[str, object]] = {}
    waves: dict[str, dict[str, object]] = {}
    reservations: dict[str, dict[str, object]] = {}
    diagnostics: list[dict[str, object]] = []
    _apply_parallel_event(_event(group_event, 1), projection, groups, waves, reservations, diagnostics)

    bad = dict(group_event)
    bad["event_type"] = "TASK_GROUP_FAILED"
    bad["group"] = dict(group_event["group"])
    bad["group"]["state"] = "RUNNING"
    with pytest.raises(HarnessValidationError) as exc_info:
        _apply_parallel_event(_event(bad, 2), projection, groups, waves, reservations, diagnostics)
    assert exc_info.value.code == "task_plan_replay_parallel_mismatch"


def test_replay_rejects_wave_completion_with_success_outcome_for_failed_child() -> None:
    plan, raw_events = _coordinator_events(status=TaskLifecycle.FAILED)
    projection = _reserved_projection(plan)
    groups: dict[str, dict[str, object]] = {}
    waves: dict[str, dict[str, object]] = {}
    reservations: dict[str, dict[str, object]] = {}
    diagnostics: list[dict[str, object]] = []
    sequence = 0
    for item in raw_events:
        if item["event_type"] not in {
            "TASK_GROUP_ADMITTED",
            "TASK_WAVE_ADMITTED",
            "TASK_WAVE_DISPATCHED",
            "TASK_WAVE_COMPLETED",
        }:
            continue
        sequence += 1
        if item["event_type"] == "TASK_WAVE_COMPLETED":
            item = dict(item)
            item["terminal_outcome"] = "SUCCEEDED"
        if item["event_type"] == "TASK_WAVE_COMPLETED":
            with pytest.raises(HarnessValidationError) as exc_info:
                _apply_parallel_event(
                    _event(item, sequence),
                    projection,
                    groups,
                    waves,
                    reservations,
                    diagnostics,
                )
            assert exc_info.value.code == "task_plan_replay_parallel_mismatch"
            return
        _apply_parallel_event(
            _event(item, sequence),
            projection,
            groups,
            waves,
            reservations,
            diagnostics,
        )
    raise AssertionError("coordinator did not emit a wave completion event")


def test_replay_rejects_second_terminal_group_transition() -> None:
    plan, raw_events = _coordinator_events()
    projection = _reserved_projection(plan)
    groups: dict[str, dict[str, object]] = {}
    waves: dict[str, dict[str, object]] = {}
    reservations: dict[str, dict[str, object]] = {}
    diagnostics: list[dict[str, object]] = []
    for sequence, item in enumerate(
        [
            event
            for event in raw_events
            if event["event_type"]
            in {
                "TASK_GROUP_ADMITTED",
                "TASK_WAVE_ADMITTED",
                "TASK_WAVE_DISPATCHED",
                "TASK_WAVE_COMPLETED",
            }
        ],
        start=1,
    ):
        _apply_parallel_event(_event(item, sequence), projection, groups, waves, reservations, diagnostics)
    group = next(iter(groups.values()))
    terminal = dict(group)
    terminal["state"] = "FAILED"
    failed_event = {
        "event_type": "TASK_GROUP_FAILED",
        "group": terminal,
        "group_id": group["group_id"],
        "reason_code": "TASK_FAILED",
    }
    _apply_parallel_event(
        _event(failed_event, len(raw_events) + 1),
        projection,
        groups,
        waves,
        reservations,
        diagnostics,
    )
    with pytest.raises(HarnessValidationError):
        _apply_parallel_event(
            _event(failed_event, len(raw_events) + 2),
            projection,
            groups,
            waves,
            reservations,
            diagnostics,
        )
