from dataclasses import replace
from threading import Event
from types import SimpleNamespace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.parallel import (
    DispatchGroupState,
    ParallelAgentCoordinator,
    ParallelEventSink,
    spawn_operation_key,
)
from framework.harness.task_plan.replay import _apply_parallel_event, _projection_for_plan
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.models import TaskLifecycle
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from tests.framework.harness.agent_loop.test_orchestration_runtime import (
    _request as _parent_request,
    _runtime,
)
from tests.framework.harness.task_plan.test_durable_task_plan_store import _ArtifactStore, _EventStore, _store
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request, _result


@pytest.mark.parametrize("failed_event", ["TASK_READY", "TASK_WAVE_ADMITTED", "TASK_ATTEMPT_SPAWN_INTENT"])
def test_durable_admission_failure_exposes_no_wave_intent_or_child(failed_event):
    events = _EventStore(fail_on_event_type=failed_event)
    store = _store(events, _ArtifactStore())
    calls = []
    runtime, identity = _runtime(
        store=store,
        worker_executor=lambda *args: calls.append(args),
    )

    result = runtime.dispatch(_parent_request(identity))

    assert result.status != "succeeded"
    assert calls == []
    assert not any(event.event_type in {
        "TASK_READY", "TASK_DISPATCHED", "TASK_STARTED",
        "TASK_WAVE_ADMITTED", "TASK_ATTEMPT_SPAWN_INTENT", "TASK_ATTEMPT_SPAWN_CONFIRMED",
        "TASK_WAVE_DISPATCHED",
    } for event in events._events)
    projection = store.load_projection(identity.run_id, "delegate_stage")
    ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
    assert ledger.ledger_version == 0 and not ledger.records
    assert not any(ledger.counters().values())
    assert all(task.status is TaskLifecycle.PENDING and task.attempts == 0 for task in projection.tasks)
    runtime._child_supervisor.shutdown()


def test_real_stage_commits_ready_budget_wave_and_intents_before_supervisor(monkeypatch):
    events = _EventStore()
    store = _store(events, _ArtifactStore())
    runtime, identity = _runtime(store=store)
    supervisor = runtime._child_supervisor
    original = supervisor.spawn_batch
    admissions = []

    def spawn_batch(requests, *, workers):
        history = store.read_events(identity.run_id, "delegate_stage")
        projection = store.load_projection(identity.run_id, "delegate_stage")
        ledger = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
        assert [event.event_type for event in history[-5:]] == [
            "TASK_READY", "TASK_READY", "TASK_WAVE_ADMITTED",
            "TASK_ATTEMPT_SPAWN_INTENT", "TASK_ATTEMPT_SPAWN_INTENT",
        ]
        assert ledger.ledger_version == len(ledger.records) == len(requests) == 2
        assert all(task.status is TaskLifecycle.READY for task in projection.tasks)
        assert all(record["status"] == "RESERVED" for record in ledger.records.values())
        assert sorted(item.budget["ledger_version"] for item in requests) == [1, 2]
        assert history[-3].payload["budget_after_checksum"] == ledger.to_dict()["ledger_checksum"]
        assert [dict(item.budget) for item in requests] == [dict(event.payload["budget_reservation"]) for event in history[-2:]]
        admissions.append(ledger.to_dict())
        return original(requests, workers=workers)

    monkeypatch.setattr(supervisor, "spawn_batch", spawn_batch)
    try:
        assert runtime.dispatch(_parent_request(identity)).status == "succeeded"
        assert len(admissions) == 1
        ledger = TaskPlanBudgetLedger.from_snapshot(store.load_projection(identity.run_id, "delegate_stage").consumed_budget)
        assert ledger.ledger_version == 4
        assert ledger.counters()["consumed_max_turns"] == 2
        assert ledger.counters()["reserved_max_turns"] == 0
        history = store.read_events(identity.run_id, "delegate_stage")
        last_receipt = max(event.sequence for event in history if event.event_type == "TASK_ATTEMPT_SPAWN_CONFIRMED")
        assert all(event.sequence > last_receipt for event in history if event.event_type in {"TASK_DISPATCHED", "TASK_STARTED"})
    finally:
        supervisor.shutdown()


def test_admission_commits_all_reservations_and_intents_before_spawn_and_dispatch_before_wait():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    events = []
    batches = []
    dispatched = Event()
    worker_saw_dispatch = []

    class CheckingSupervisor(ChildAgentSupervisor):
        def spawn_batch(self, requests, *, workers):
            assert len(batches) == 1
            admission, *intents = batches[0]
            assert admission["event_type"] == "TASK_WAVE_ADMITTED"
            assert len(admission["wave"]["reservations"]) == len(requests) == len(intents) == 2
            assert all(item["state"] == "RESERVED" for item in admission["wave"]["reservations"])
            assert [item["operation_key"] for item in intents] == [item.operation_id for item in requests]
            assert events[-3:] == list(batches[0])
            return super().spawn_batch(requests, workers=workers)

    def append(event):
        events.append(event)
        if event["event_type"] == "TASK_WAVE_DISPATCHED":
            assert sum(item["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED" for item in events) == 2
            dispatched.set()

    def append_batch(batch):
        batches.append(batch)
        events.extend(batch)

    def invoke(instance):
        worker_saw_dispatch.append(dispatched.wait(timeout=3))
        return _result(plan, instance)

    supervisor = CheckingSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2, child_supervisor=supervisor,
        event_sink=ParallelEventSink(append, append_batch),
    )
    try:
        result = coordinator.dispatch(request, invoke)
        assert result.succeeded
        assert worker_saw_dispatch == [True, True]
        assert result.waves[0].execution_mode == "SUPERVISED"
    finally:
        dispatched.set()
        supervisor.shutdown()


def test_non_atomic_sink_is_rejected_without_admission_or_worker_calls():
    plan = _accepted_parallel_plan(("task-1",))
    events = []
    calls = []
    supervisor = ChildAgentSupervisor(max_children=1)
    coordinator = ParallelAgentCoordinator(max_workers=1, child_supervisor=supervisor, event_sink=events.append)
    try:
        with pytest.raises(HarnessValidationError) as error:
            coordinator.dispatch(_request(plan), lambda item: calls.append(item))
        assert error.value.code == "TASK_WAVE_ATOMIC_SINK_REQUIRED"
        assert calls == []
        assert [item["event_type"] for item in events] == ["TASK_GROUP_ADMITTED", "DEGRADED_SERIAL"]
        session = next(iter(coordinator._sessions.values()))
        assert session.waves == []
        assert session.reserved == set()
    finally:
        supervisor.shutdown()


def test_partial_spawn_records_each_outcome_without_dispatch_or_duplicate_child():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    events = []
    calls = []

    class PartialSupervisor(ChildAgentSupervisor):
        def spawn_batch(self, requests, *, workers):
            super().spawn_batch(requests[:1], workers=workers[:1])
            raise RuntimeError("injected partial spawn")

    supervisor = PartialSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2, child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )

    def invoke(instance):
        calls.append(instance.task_id)
        return _result(plan, instance)

    try:
        with pytest.raises(RuntimeError, match="injected partial spawn"):
            coordinator.dispatch(_request(plan), invoke)
        result = coordinator.dispatch(_request(plan), invoke)
        assert result.group.state is DispatchGroupState.INDETERMINATE
        receipts = [item for item in events if item["event_type"] in {
            "TASK_ATTEMPT_SPAWN_CONFIRMED", "TASK_ATTEMPT_SPAWN_UNKNOWN",
        }]
        assert [(item["task_id"], item["spawn_status"]) for item in receipts] == [
            ("task-1", "SPAWN_CONFIRMED"), ("task-2", "SPAWN_UNKNOWN"),
        ]
        assert "child_id" not in receipts[1]
        assert not any(item["event_type"] == "TASK_WAVE_DISPATCHED" for item in events)
    finally:
        supervisor.shutdown()
    assert calls == ["task-1"]


def test_receipt_write_failure_keeps_all_started_children_trackable():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    events = []

    def append(event):
        if event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED":
            raise RuntimeError("injected receipt write failure")
        events.append(event)

    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2, child_supervisor=supervisor,
        event_sink=ParallelEventSink(append, events.extend),
    )
    try:
        with pytest.raises(RuntimeError, match="injected receipt write failure"):
            coordinator.dispatch(_request(plan), lambda instance: _result(plan, instance))
        session = next(iter(coordinator._sessions.values()))
        assert set(session.active_children) == {"task-1", "task-2"}
        assert session.reserved == {"task-1", "task-2"}
        assert session.group.state is DispatchGroupState.INDETERMINATE
        assert not any(item["event_type"] == "TASK_WAVE_DISPATCHED" for item in events)
    finally:
        supervisor.shutdown()


@pytest.fixture
def spawn_history():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    events = []
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2, child_supervisor=supervisor,
        event_sink=ParallelEventSink(events.append, events.extend),
    )
    try:
        assert coordinator.dispatch(request, lambda instance: _result(plan, instance)).succeeded
    finally:
        supervisor.shutdown()
    projection = TaskPlanScheduler().reserve_ready_tasks(
        _projection_for_plan(plan, sequence=1), TaskPlanReadyDecision(request.task_instances),
    )
    return projection, [dict(item) for item in events if item["event_type"] in {
        "TASK_GROUP_ADMITTED", "TASK_WAVE_ADMITTED", "TASK_WAVE_DISPATCHED",
        "TASK_ATTEMPT_SPAWN_INTENT", "TASK_ATTEMPT_SPAWN_CONFIRMED",
    }]


def _replay(projection, events):
    groups, waves, reservations, diagnostics, spawns = {}, {}, {}, [], {}
    for sequence, event in enumerate(events, 1):
        _apply_parallel_event(
            SimpleNamespace(event_type=event["event_type"], payload=event, sequence=sequence),
            projection, groups, waves, reservations, diagnostics, spawns,
        )
    return spawns


@pytest.mark.parametrize("missing", ["all", "intent", "receipt"])
def test_replay_rejects_dispatch_with_missing_spawn_evidence(spawn_history, missing):
    projection, events = spawn_history
    removed = {
        "all": {"TASK_ATTEMPT_SPAWN_INTENT", "TASK_ATTEMPT_SPAWN_CONFIRMED"},
        "intent": {"TASK_ATTEMPT_SPAWN_INTENT"},
        "receipt": {"TASK_ATTEMPT_SPAWN_CONFIRMED"},
    }[missing]
    events = [item for item in events if item["event_type"] not in removed]
    with pytest.raises(HarnessValidationError) as error:
        _replay(projection, events)
    assert error.value.code == "task_plan_replay_parallel_mismatch"


def test_replay_reuses_identical_receipts_but_rejects_conflicting_identity(spawn_history):
    projection, events = spawn_history
    expected = _replay(projection, events)
    receipt = next(item for item in events if item["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED")
    assert _replay(projection, [*events, dict(receipt)]) == expected
    for changed in ({"child_id": "other-child"}, {"task_id": "task-2"}):
        with pytest.raises(HarnessValidationError):
            _replay(projection, [*events, {**receipt, **changed}])


def test_unknown_spawn_is_not_a_trackable_dispatch_receipt(spawn_history):
    projection, events = spawn_history
    receipt = next(item for item in events if item["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED")
    receipt.update(event_type="TASK_ATTEMPT_SPAWN_UNKNOWN", spawn_status="SPAWN_UNKNOWN")
    receipt.pop("child_id")
    with pytest.raises(HarnessValidationError, match="confirmed per-task"):
        _replay(projection, events)


def test_replay_rejects_fabricated_attempt_even_with_matching_operation_key(spawn_history):
    projection, events = spawn_history
    intent = next(item for item in events if item["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT")
    intent["attempt"] += 1
    key = spawn_operation_key(intent["group_id"], intent["wave_id"], intent["task_instance_id"], intent["attempt"])
    intent.update(operation_key=key, idempotency_key=key)
    with pytest.raises(HarnessValidationError, match="admitted task attempt"):
        _replay(projection, events)


def test_replay_rejects_tampered_spawn_budget_reservation(spawn_history):
    projection, events = spawn_history
    intent = next(item for item in events if item["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT")
    intent["budget_reservation"] = dict(intent["budget_reservation"])
    intent["budget_reservation"]["reservation_checksum"] = "sha256:" + "0" * 64
    with pytest.raises(HarnessValidationError, match="budget reservation checksum"):
        _replay(projection, events)


@pytest.mark.parametrize("field", ["ledger_version", "parent_allocation", "attempt_allocation"])
def test_replay_rejects_self_consistent_budget_not_backed_by_ledger(spawn_history, field):
    projection, events = spawn_history
    intent = next(item for item in events if item["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT")
    budget = dict(intent["budget_reservation"])
    budget[field] = 99 if field == "ledger_version" else {"turns": 99}
    budget["reservation_checksum"] = canonical_payload_checksum({name: value for name, value in budget.items() if name != "reservation_checksum"})
    intent["budget_reservation"] = budget
    with pytest.raises(HarnessValidationError, match="differs from attempt ledger"):
        _replay(projection, events)


def test_dispatch_request_rejects_an_instance_with_unaccepted_budget():
    from framework.harness.task_plan.models import TaskBudget

    request = _request(_accepted_parallel_plan(("task-1",)))
    fabricated = replace(request.task_instances[0], budget_snapshot=TaskBudget(max_turns=7))
    with pytest.raises(HarnessValidationError, match="differs from accepted task"):
        replace(request, task_instances=(fabricated,))


@pytest.mark.parametrize("missing", ["last_intent", "admission", "interleaved"])
def test_replay_rejects_non_atomic_intent_history(spawn_history, missing):
    from framework.harness.task_plan.replay import _validate_atomic_wave_intents

    _projection, events = spawn_history
    intent_indices = [index for index, event in enumerate(events) if event["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT"]
    if missing == "last_intent":
        events = events[:intent_indices[-1]]
    elif missing == "admission":
        events = [event for event in events if event["event_type"] != "TASK_WAVE_ADMITTED"]
    else:
        events.insert(intent_indices[-1], {"event_type": "TASK_STARTED"})
    history = tuple(SimpleNamespace(event_type=event["event_type"], payload=event, sequence=index) for index, event in enumerate(events, 1))
    with pytest.raises(HarnessValidationError):
        _validate_atomic_wave_intents(history)
