from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan import InMemoryTaskPlanStore, TaskPlanRecoveryService, TaskPlanReplayReducer, ValidatedTaskPlan
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.planning_observation import PlanningObservationReceipt, PlanningObservationRequest
from framework.tool import ToolExecutor
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _graph_only_candidate_and_plan, _TaskPlanQueueReader,
)


def _history(*, receipt_transform=lambda receipt: receipt):
    candidate, base = _graph_only_candidate_and_plan()
    request = PlanningObservationRequest(
        request_id="planning-1", run_id=base.run_id, stage_id=base.stage_id,
        policy_checksum=base.policy_checksum, planner_turn_id="planner-turn-1",
        correlation_id="planning-correlation-1", tool_name="research.lookup",
        purpose="read source evidence", arguments={"query": "paper"},
    )
    receipt = receipt_transform(PlanningObservationReceipt(
        request=request, status="SUCCEEDED", tool_call_id="call-1",
        result_checksum=canonical_payload_checksum({"result": "recorded"}), elapsed_ms=10,
    ))
    candidate = replace(candidate, source_observation_refs=(receipt.source_ref,),
                        metadata={"planner_turn_id": request.planner_turn_id})
    plan = ValidatedTaskPlan.from_candidate(
        candidate, plan_id=base.plan_id, version=1, parent_plan_id=None,
        source_candidate_ref=candidate.candidate_checksum, policy_ref=base.policy_ref,
        policy_checksum=base.policy_checksum, tasks=base.tasks,
        required_output_roles=base.required_output_roles, limits=base.limits,
        accepted_at=base.accepted_at,
    )
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    return plan, store.read_events(plan.run_id, plan.stage_id), receipt


def test_plan_checksum_pins_planning_sources_and_turn():
    plan, _, _ = _history()
    assert ValidatedTaskPlan.from_dict(plan.to_dict()) == plan
    changed = replace(plan, planner_turn_id="different-turn")
    assert changed.plan_checksum != plan.plan_checksum
    payload = plan.to_dict()
    del payload["source_observation_refs"]
    del payload["planner_turn_id"]
    with pytest.raises(HarnessValidationError):
        ValidatedTaskPlan.from_dict(payload)


def test_standalone_replay_and_recovery_consume_only_recorded_receipts(monkeypatch):
    plan, events, receipt = _history()

    def no_live_calls(*args, **kwargs):
        raise AssertionError("offline replay must never execute a tool")

    monkeypatch.setattr(ToolExecutor, "execute", no_live_calls)
    report = TaskPlanReplayReducer().replay((plan,), events, planning_receipts=(receipt,), require_terminal_events=False)
    queue_reader = _TaskPlanQueueReader()
    recovered = TaskPlanRecoveryService(queue_reader=queue_reader).recover(
        (plan,), events, planning_receipts=iter((receipt,)),
    )
    assert recovered.report.replay_checksum == report.replay_checksum
    assert queue_reader.calls == []
    assert report.projection.plan_checksum == plan.plan_checksum


def test_standalone_replay_fails_closed_without_planning_evidence():
    plan, events, _ = _history()
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanReplayReducer().replay((plan,), events)
    assert error.value.code == "planning_observation_receipt_missing"
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanRecoveryService(queue_reader=_TaskPlanQueueReader()).recover((plan,), events)
    assert error.value.code == "planning_observation_receipt_missing"


@pytest.mark.parametrize("field,value", [
    ("run_id", "foreign-run"), ("stage_id", "foreign-stage"),
    ("planner_turn_id", "foreign-turn"), ("policy_checksum", "sha256:" + "0" * 64),
])
def test_resealed_cross_scope_receipt_is_rejected(field, value):
    plan, events, receipt = _history(receipt_transform=lambda r: replace(r, request=replace(r.request, **{field: value})))
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanReplayReducer().replay((plan,), events, planning_receipts=(receipt,))
    assert error.value.code == "planning_observation_receipt_scope_mismatch"


def test_resealed_failure_receipt_is_not_successful_source_evidence():
    plan, events, receipt = _history(receipt_transform=lambda r: replace(
        r, status="FAILED", reason_code="tool_failed", result_checksum=None,
    ))
    with pytest.raises(HarnessValidationError) as error:
        TaskPlanReplayReducer().replay((plan,), events, planning_receipts=(receipt,))
    assert error.value.code == "planning_observation_receipt_unusable"
