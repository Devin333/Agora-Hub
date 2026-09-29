from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger, result_budget_usage
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.models import TaskAdmissionOwner, TaskBudget, TaskLifecycle
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler, task_instance_for_attempt
from framework.harness.task_plan.store import (
    InMemoryTaskPlanStore,
    LogicalTaskReadiness,
    TaskPlanEvent,
    TaskQueueAdmissionEvidence,
    TaskResultRecord,
)
from framework.harness.task_plan import TaskPlanCheckpoint, TaskPlanReplayReducer, TaskPlanValidator
from tests.fixtures.task_plan import InMemoryTaskPlanGateArtifactWriter
from tests.framework.harness.task_plan.test_dependency_blocking import _accepted_plan
from tests.framework.harness.task_plan.test_task_plan_runtime import _candidate, _setup, _task, validator_context
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _ArtifactStore, _EventStore, _graph_only_candidate_and_plan, _result, _start, _store,
)


def _metered_plan():
    graph, policy, registry = _setup()
    allocation = TaskBudget(2, 3, 2, 10)
    policy = replace(policy, per_task_budget=allocation, aggregate_task_budget=TaskBudget(8, 12, 8, 40))
    candidate = _candidate(graph, (replace(_task("a", "cap", "role"), budget_request=allocation),))
    return TaskPlanValidator().accept(candidate, policy, registry, context=validator_context(graph), accepted_at="2026-09-07T00:00:00Z")


def _failed(plan, instance, **kwargs):
    return TaskResultRecord.for_plan(
        plan, task_id=instance.task_id, task_instance_id=instance.task_instance_id,
        attempt=instance.attempt, status=TaskLifecycle.FAILED, error_code="worker_failed", **kwargs,
    )


def test_reservation_and_result_settlement_retain_exact_attempt_accounting():
    plan = _metered_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    initial = TaskPlanBudgetLedger.for_plan(plan)
    reserved = initial.reserve((instance,))
    assert initial.ledger_version == 0
    assert reserved.reserve((instance,)).snapshot() == reserved.snapshot()
    result = _failed(plan, instance, usage={"turns": 1, "tool_calls": 2, "memory_ops": 0, "output_tokens": 4})
    settled = reserved.settle(result)
    record = settled.records[instance.idempotency_key]
    assert record["consumed"] == {"max_turns": 1, "max_tool_calls": 2, "max_memory_ops": 0, "max_output_tokens": 4}
    assert record["released"] == {"max_turns": 1, "max_tool_calls": 1, "max_memory_ops": 2, "max_output_tokens": 6}
    assert record["result_checksum"] == result.result_checksum
    assert record["reserved_revision"] == 1
    assert record["settled_revision"] == settled.ledger_version == 2
    assert settled.settle(result) is settled
    assert settled.allocated_totals() == instance.budget_snapshot.to_dict()
    assert TaskPlanBudgetLedger.from_snapshot(settled.snapshot()) == settled


def test_batch_reservation_is_all_or_nothing_and_retry_cannot_reuse_released_budget():
    plan, _ = _accepted_plan()
    initial = replace(TaskPlanBudgetLedger.for_plan(plan), parent_allocation=TaskBudget(1).to_dict())
    first = task_instance_for_attempt(plan, "a", 1)
    second = task_instance_for_attempt(plan, "b", 1)
    before = initial.snapshot()
    with pytest.raises(HarnessValidationError, match="exceed"):
        initial.reserve((first, second))
    assert initial.snapshot() == before
    released = initial.reserve((first,)).settle(_failed(plan, first, usage={"turns": 0}))
    assert released.snapshot()["released_max_turns"] == 1
    with pytest.raises(HarnessValidationError, match="exceed"):
        released.reserve((task_instance_for_attempt(plan, "a", 2),))
    assert released.ledger_version == 2


def test_retry_uses_new_identity_and_preserves_failed_history():
    plan, _ = _accepted_plan()
    first = task_instance_for_attempt(plan, "a", 1)
    second = task_instance_for_attempt(plan, "a", 2)
    reserved = TaskPlanBudgetLedger.for_plan(plan).reserve((first,))
    with pytest.raises(HarnessValidationError, match="retry"):
        reserved.reserve((second,))
    failed = _failed(plan, first)
    retried = reserved.settle(failed).reserve((second,))
    assert len(retried.records) == 2
    assert retried.records[first.idempotency_key]["result_checksum"] == failed.result_checksum
    assert retried.records[second.idempotency_key]["status"] == "RESERVED"
    assert retried.snapshot()["consumed_max_turns"] == retried.snapshot()["reserved_max_turns"] == 1
    with pytest.raises(HarnessValidationError, match="terminal reservation"):
        retried.reserve((first,))


def test_conflicting_result_or_reservation_cannot_change_ledger():
    plan, _ = _accepted_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    reserved = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    with pytest.raises(HarnessValidationError, match="identity conflicts"):
        reserved.reserve((replace(instance, budget_snapshot=TaskBudget(2)),))
    # Matching aggregate counters do not authorize a different task/attempt.
    other = task_instance_for_attempt(plan, "b", 1)
    with pytest.raises(HarnessValidationError, match="no matching"):
        reserved.settle(_failed(plan, other))
    settled = reserved.settle(_failed(plan, instance))
    with pytest.raises(HarnessValidationError, match="conflicting duplicate"):
        settled.settle(_failed(plan, instance, usage={"turns": 0}))
    with pytest.raises(HarnessValidationError, match="conflicting duplicate"):
        settled.release_unstarted(instance.task_instance_id, instance.task_id, 1, reason_code="blocked")
    assert reserved.ledger_version == 1
    assert settled.ledger_version == 2


def test_unstarted_release_is_idempotent_and_conflicting_reason_fails_closed():
    plan, _ = _accepted_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    reserved = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    released = reserved.release_unstarted(instance.task_instance_id, "a", 1, reason_code="blocked")
    assert released.release_unstarted(instance.task_instance_id, "a", 1, reason_code="blocked") is released
    assert released.snapshot()["released_max_turns"] == 1
    assert released.snapshot()["reserved_max_turns"] == 0
    with pytest.raises(HarnessValidationError, match="conflicting duplicate"):
        released.release_unstarted(instance.task_instance_id, "a", 1, reason_code="cancelled")


@pytest.mark.parametrize("tamper", ["counter", "checksum", "partition", "revision", "owner"])
def test_restore_rejects_tampered_accounting(tamper):
    plan, _ = _accepted_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,)).settle(_failed(plan, instance))
    snapshot = ledger.snapshot()
    if tamper == "counter":
        snapshot["consumed_max_turns"] = 0
    elif tamper == "checksum":
        snapshot["ledger"]["ledger_checksum"] = "sha256:" + "0" * 64
    else:
        raw = snapshot["ledger"]
        if tamper == "partition":
            raw["records"][instance.idempotency_key]["released"]["max_turns"] = 1
        elif tamper == "revision":
            raw["ledger_version"] = 2**63
        else:
            raw["run_id"] = "another-run"
        raw["ledger_checksum"] = canonical_payload_checksum({key: value for key, value in raw.items() if key != "ledger_checksum"})
    with pytest.raises(HarnessValidationError):
        TaskPlanBudgetLedger.from_snapshot(snapshot)


@pytest.mark.parametrize("usage", [{"turns": True}, {"turns": -1}, {"turns": 2}, {"turns": 0, "max_turns": 1}])
def test_invalid_or_conflicting_usage_cannot_settle(usage):
    with pytest.raises(HarnessValidationError):
        result_budget_usage(usage, TaskBudget(1).to_dict())


@pytest.mark.parametrize("parallelism", [1, 2])
def test_ready_recovery_does_not_reserve_or_charge_the_same_attempt_twice(parallelism):
    graph, policy, registry = _setup()
    policy = replace(policy, per_task_budget=TaskBudget(1), aggregate_task_budget=TaskBudget(1), max_parallelism=parallelism)
    candidate = replace(_candidate(graph, (_task("a", "cap", "role"),)), requested_max_parallelism=parallelism)
    plan = TaskPlanValidator().accept(candidate, policy, registry, context=validator_context(graph), accepted_at="2026-09-07T00:00:00Z")
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    projection = store.load_projection(plan.run_id, plan.stage_id)
    instance = task_instance_for_attempt(plan, "a", 1)
    scheduler = TaskPlanScheduler()
    decision = scheduler.next_ready_tasks(
        projection,
        1,
        plan=plan,
        available_input_refs=("document",),
    )
    assert decision.logical_ready_task_ids == ("a",)
    ready = scheduler.reserve_ready_tasks(projection, decision)
    ready_state = ready.tasks[0]
    assert ready_state.status is TaskLifecycle.READY
    assert ready_state.attempts == 0
    assert ready_state.active_instance_id is None
    assert ready_state.admission_owner is None
    assert ready.consumed_budget == projection.consumed_budget

    sequence = len(store.read_events(plan.run_id, plan.stage_id)) + 1
    readiness = LogicalTaskReadiness(
        task_id=instance.task_id,
        task_definition_checksum=instance.task_definition_checksum,
        logical_ready_order=decision.logical_ready_task_ids,
    )
    ready = replace(ready, last_sequence=sequence)
    store.commit_event(TaskPlanEvent.for_plan(
        "TASK_READY",
        plan,
        task_id=instance.task_id,
        input_checksum=instance.task_definition_checksum,
        sequence=sequence,
        payload={"logical_readiness": readiness.to_dict()},
    ), ready)

    before_admission = ready
    admitted = scheduler.mark_admitted(
        before_admission,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    admission = TaskQueueAdmissionEvidence(
        task_instance=instance,
        budget_before_checksum=TaskPlanBudgetLedger.from_snapshot(
            before_admission.consumed_budget
        ).to_dict()["ledger_checksum"],
        budget_after_checksum=TaskPlanBudgetLedger.from_snapshot(
            admitted.consumed_budget
        ).to_dict()["ledger_checksum"],
    )
    sequence += 1
    admitted = replace(admitted, last_sequence=sequence)
    admitted_event = TaskPlanEvent.for_plan(
        "TASK_QUEUE_ADMITTED",
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        input_checksum=instance.task_definition_checksum,
        sequence=sequence,
        payload={"queue_admission": admission.to_dict()},
    )
    admission_checksum = store.commit_event(admitted_event, admitted)
    snapshot = admitted.consumed_budget

    assert store.commit_event(admitted_event, admitted) == admission_checksum
    projection_after_admission = admitted
    for event_type, transition in (
        ("TASK_DISPATCHED", lambda state: scheduler.mark_dispatched(state, instance)),
        ("TASK_STARTED", lambda state: scheduler.mark_started(state, instance)),
    ):
        sequence += 1
        projection_after_admission = replace(
            transition(projection_after_admission),
            last_sequence=sequence,
        )
        store.commit_event(TaskPlanEvent.for_plan(
            event_type,
            plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            input_checksum=instance.task_definition_checksum,
            sequence=sequence,
        ), projection_after_admission)

    recovered = store.load_projection(plan.run_id, plan.stage_id)
    recovered_state = recovered.tasks[0]
    assert recovered_state.status is TaskLifecycle.RUNNING
    assert recovered_state.attempts == 1
    assert recovered_state.active_instance_id == instance.task_instance_id
    assert recovered_state.admission_owner is TaskAdmissionOwner.QUEUE
    assert recovered.consumed_budget == snapshot
    event_types = [
        event.event_type
        for event in store.read_events(plan.run_id, plan.stage_id)
    ]
    assert event_types[-4:] == [
        "TASK_READY",
        "TASK_QUEUE_ADMITTED",
        "TASK_DISPATCHED",
        "TASK_STARTED",
    ]
    assert event_types.count("TASK_QUEUE_ADMITTED") == 1


def test_ready_recovery_does_not_allocate_slots_already_reserved_by_other_tasks():
    plan, projection = _accepted_plan()
    scheduler = TaskPlanScheduler()
    decision = scheduler.next_ready_tasks(
        projection,
        2,
        plan=plan,
        available_input_refs=("document",),
    )
    assert decision.logical_ready_task_ids == ("a", "d")
    ready = scheduler.reserve_ready_tasks(projection, decision)
    instance = task_instance_for_attempt(plan, "a", 1)
    admitted = scheduler.mark_admitted(
        ready,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )

    recovered = scheduler.next_ready_tasks(
        admitted,
        2,
        plan=plan,
        available_input_refs=("document",),
    )
    recovered_projection = scheduler.reserve_ready_tasks(admitted, recovered)
    states = {item.task_id: item for item in recovered_projection.tasks}
    ledger = TaskPlanBudgetLedger.from_snapshot(recovered_projection.consumed_budget)

    assert recovered.logical_ready_task_ids == ("d",)
    assert states["a"].status is TaskLifecycle.ADMITTED
    assert states["a"].admission_owner is TaskAdmissionOwner.QUEUE
    assert states["d"].status is TaskLifecycle.READY
    assert states["d"].attempts == 0
    assert states["d"].active_instance_id is None
    assert states["d"].admission_owner is None
    assert tuple(ledger.records) == (instance.idempotency_key,)


def test_settlement_cannot_use_a_different_accepted_allocation():
    plan = _metered_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    with pytest.raises(HarnessValidationError, match="accepted allocation"):
        ledger.settle(_failed(plan, instance), expected_allocation=TaskBudget(1).to_dict())
    assert ledger.ledger_version == 1


def test_unknown_outcome_keeps_the_outstanding_reservation():
    from types import SimpleNamespace

    plan = _metered_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,))
    payload = _failed(plan, instance).to_dict()
    payload["status"] = "INDETERMINATE"
    unknown = SimpleNamespace(**payload)
    before = ledger.snapshot()
    with pytest.raises(HarnessValidationError, match="typed terminal"):
        ledger.settle(unknown)
    assert ledger.snapshot() == before


def test_restore_rejects_overlapping_retry_history_even_with_valid_checksum():
    plan = _metered_plan()
    first, second = (task_instance_for_attempt(plan, "a", attempt) for attempt in (1, 2))
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((first,)).settle(_failed(plan, first)).reserve((second,)).settle(_failed(plan, second))
    snapshot = ledger.snapshot()
    raw = snapshot["ledger"]
    raw["records"][first.idempotency_key]["settled_revision"] = 3
    raw["records"][second.idempotency_key]["reserved_revision"] = 2
    raw["ledger_checksum"] = canonical_payload_checksum({key: value for key, value in raw.items() if key != "ledger_checksum"})
    with pytest.raises(HarnessValidationError, match="overlaps"):
        TaskPlanBudgetLedger.from_snapshot(snapshot)


def test_duplicate_result_checks_recorded_usage_partition():
    plan = _metered_plan()
    instance = task_instance_for_attempt(plan, "a", 1)
    result = _failed(plan, instance, usage={"turns": 1})
    ledger = TaskPlanBudgetLedger.for_plan(plan).reserve((instance,)).settle(result)
    records = ledger.snapshot()["ledger"]["records"]
    records[instance.idempotency_key]["consumed"]["max_turns"] = 0
    records[instance.idempotency_key]["released"]["max_turns"] = 2
    altered = replace(ledger, records=records)
    with pytest.raises(HarnessValidationError, match="conflicts with result usage"):
        TaskPlanBudgetLedger.from_snapshot(altered.snapshot()).settle(result)


@pytest.mark.parametrize("failed_event_type", ["TASK_RESULT_ACCEPTED", "TASK_COMPLETED"])
def test_memory_store_rolls_back_result_events_and_budget_together(failed_event_type, monkeypatch):
    from framework.harness.task_plan.store import InMemoryTaskPlanStore

    store = InMemoryTaskPlanStore()
    gate_artifact_owner = InMemoryTaskPlanGateArtifactWriter()
    store.bind_gate_evidence_reader(gate_artifact_owner)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    instance = _start(store, plan, plan.tasks[0].task_id)
    result = _result(
        plan,
        instance,
        status=TaskLifecycle.SUCCEEDED,
        gate_artifact_owner=gate_artifact_owner,
    )
    before = store.load_projection(plan.run_id, plan.stage_id)
    history = store.read_events(plan.run_id, plan.stage_id)
    append = store._append_event

    def fail_after_append(event):
        append(event)
        if event.event_type == failed_event_type:
            raise RuntimeError("injected result transition failure")

    monkeypatch.setattr(store, "_append_event", fail_after_append)
    with pytest.raises(RuntimeError, match="injected"):
        store.append_result(result)
    assert store.load_projection(plan.run_id, plan.stage_id) == before
    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version) == ()
    monkeypatch.setattr(store, "_append_event", append)
    store.append_result(result)
    after = store.load_projection(plan.run_id, plan.stage_id)
    assert after.consumed_budget["reserved_max_turns"] == 0
    assert after.consumed_budget["consumed_max_turns"] == 1
    assert after.consumed_budget["ledger"]["ledger_version"] == 2
    assert len(store.read_events(plan.run_id, plan.stage_id)) == len(history) + 2
    assert store.append_result(result) == result.result_checksum
    assert store.load_projection(plan.run_id, plan.stage_id) == after


@pytest.mark.parametrize("failed_event_type", ["TASK_READY", "TASK_COMPLETED"])
def test_durable_budget_transition_failure_reopen_and_offline_replay(failed_event_type, monkeypatch):
    from framework.harness.task_plan.stage import TaskPlanStageRunner

    def live_call(*args, **kwargs):
        pytest.fail("replay invoked live worker")

    monkeypatch.setattr(TaskPlanStageRunner, "_invoke", live_call)
    events = _EventStore(fail_on_event_type=failed_event_type)
    artifacts = _ArtifactStore()
    store = _store(events, artifacts)
    candidate, plan = _graph_only_candidate_and_plan()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    task_id = plan.tasks[0].task_id
    if failed_event_type == "TASK_READY":
        before = store.load_projection(plan.run_id, plan.stage_id)
        with pytest.raises(RuntimeError, match="injected batch failure"):
            _start(store, plan, task_id)
        assert _store(events, artifacts).load_projection(plan.run_id, plan.stage_id) == before
        events.fail_on_event_type = None
    instance = _start(store, plan, task_id)
    before = store.load_projection(plan.run_id, plan.stage_id)
    result = _result(
        plan,
        instance,
        status=TaskLifecycle.SUCCEEDED,
        gate_artifact_owner=store,
        metrics={"turns": 0},
    )
    if failed_event_type == "TASK_COMPLETED":
        with pytest.raises(RuntimeError, match="injected batch failure"):
            store.append_result(result)
        assert _store(events, artifacts).load_projection(plan.run_id, plan.stage_id) == before
        events.fail_on_event_type = None
    store.append_result(result)
    reopened = _store(events, artifacts)
    projection = reopened.load_projection(plan.run_id, plan.stage_id)
    history = reopened.read_events(plan.run_id, plan.stage_id)
    reopened.append_result(result)
    assert reopened.read_events(plan.run_id, plan.stage_id) == history
    report = TaskPlanReplayReducer(gate_evidence_reader=reopened).replay((plan,), history, results=reopened.result_history_for(plan.run_id, plan.stage_id, plan.plan_id, plan.version))
    assert report.projection == projection
    assert projection.consumed_budget["reserved_max_turns"] == 0
    assert projection.consumed_budget["consumed_max_turns"] == 0
    assert projection.consumed_budget["released_max_turns"] == 1
    checkpoint = TaskPlanCheckpoint.from_replay("budget-checkpoint", plan, report, created_at="2026-09-07T00:00:00Z")
    assert TaskPlanCheckpoint.from_dict(checkpoint.to_dict()).budget_snapshot == projection.consumed_budget
    for value in (report, checkpoint):
        with pytest.raises(HarnessValidationError) as old_reducer:
            replace(value, reducer_version="newsroom.harness-task-plan-replay/v2")
        assert old_reducer.value.code == "unsupported_task_plan_replay_reducer"
