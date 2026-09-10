"""Complete waiting reasons survive offline replay without allocating attempts."""

from types import SimpleNamespace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan import capacity
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.capacity import FirstFitPacking
from framework.harness.task_plan.parallel import _capacity_waiting_evidence
from framework.harness.task_plan.replay import _apply_parallel_event
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from framework.harness.task_plan.store import _projection_for_plan
from tests.framework.harness.task_plan.capacity_fixtures import (
    capacity_pool,
    capacity_scope_snapshot,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
)


def _waiting_fixture(reasons):
    plan = _accepted_parallel_plan(("a", "b"))
    projection = TaskPlanScheduler().reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1),
        TaskPlanReadyDecision(logical_ready_task_ids=("a", "b")),
    )
    ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
    ledger_checksum = ledger.to_dict()["ledger_checksum"]
    # Recorded snapshots are deliberately old: offline replay must not apply
    # today's capacity policy or wall clock to a historical waiting decision.
    scope = capacity_scope_snapshot((capacity_pool("cpu", 1, expires_at_ms=1000),))
    packing = FirstFitPacking(
        ready_order=("a", "b"),
        selected=(),
        overflow=("a", "b"),
        reservations=(),
        reasons=reasons,
        capacity_before=scope,
        capacity_after=scope,
        budget_before_checksum=ledger_checksum,
        budget_after_checksum=ledger_checksum,
        admitted_budget_snapshot=ledger.snapshot(),
    )
    group = SimpleNamespace(
        group_id="waiting-group",
        group_checksum=canonical_payload_checksum({"group": "waiting-group"}),
        absolute_deadline_ms=900000,
    )
    waiting = _capacity_waiting_evidence(group, packing, budget_checksum=ledger_checksum)
    event = SimpleNamespace(
        event_type="TASK_GROUP_CAPACITY_WAITING",
        sequence=2,
        reason_code="CAPACITY_NOT_AVAILABLE",
        payload={
            "group_id": group.group_id,
            "waiting": waiting,
            "idempotency_key": waiting["waiting_key"],
        },
    )
    return projection, event, {group.group_id: vars(group).copy()}


@pytest.mark.parametrize("temporary_reason", ["CAPACITY_NOT_AVAILABLE", "RESOURCE_CONFLICT"])
def test_mixed_budget_and_temporary_wait_replays_idempotently_without_live_reads(
    temporary_reason, monkeypatch
):
    reasons = {"a": "BUDGET_EXCEEDED", "b": temporary_reason}
    projection, event, groups = _waiting_fixture(reasons)
    before = projection.to_dict()
    waves, reservations, diagnostics = {}, {}, []

    def forbidden_clock():
        pytest.fail("offline replay consulted live capacity time")

    monkeypatch.setattr(capacity, "capacity_now_ms", forbidden_clock)
    for _ in range(2):
        _apply_parallel_event(event, projection, groups, waves, reservations, diagnostics)

    assert len(diagnostics) == 1
    assert diagnostics[0]["waiting"]["reasons"] == reasons
    assert projection.to_dict() == before
    assert projection.logical_ready_order == ("a", "b")
    assert all(task.active_instance_id is None for task in projection.tasks)
    assert waves == reservations == {}


@pytest.mark.parametrize(
    "reasons",
    [
        {"a": "BUDGET_EXCEEDED", "b": "BUDGET_EXCEEDED"},
        {"a": "CAPACITY_POLICY_MISSING", "b": "CAPACITY_NOT_AVAILABLE"},
        {"a": "CAPACITY_POLICY_STALE", "b": "RESOURCE_CONFLICT"},
        {"a": "unknown", "b": "CAPACITY_NOT_AVAILABLE"},
    ],
)
def test_non_temporary_or_invalid_policy_reasons_cannot_become_capacity_wait(reasons):
    projection, event, groups = _waiting_fixture(reasons)
    diagnostics = []
    with pytest.raises(HarnessValidationError, match="capacity wait evidence differs"):
        _apply_parallel_event(event, projection, groups, {}, {}, diagnostics)
    assert diagnostics == []


@pytest.mark.parametrize("invalid_reason", [None, 7, True, [], {}])
def test_recomputed_wait_checksum_does_not_allow_non_string_reason(invalid_reason):
    projection, event, groups = _waiting_fixture(
        {"a": "BUDGET_EXCEEDED", "b": "CAPACITY_NOT_AVAILABLE"}
    )
    waiting = event.payload["waiting"]
    waiting["reasons"]["a"] = invalid_reason
    waiting.pop("waiting_key")
    waiting["waiting_key"] = "capacity-wait:" + canonical_payload_checksum(waiting)
    event.payload["idempotency_key"] = waiting["waiting_key"]
    diagnostics = []
    with pytest.raises(HarnessValidationError, match="capacity wait evidence differs"):
        _apply_parallel_event(event, projection, groups, {}, {}, diagnostics)
    assert diagnostics == []
