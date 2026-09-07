from dataclasses import replace

from framework.harness.task_plan.models import TaskLifecycle
from types import SimpleNamespace

import pytest

from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import ChildAgentNotFoundError, ChildAgentOperationConflict, ChildAgentSupervisor
from framework.harness.task_plan.parallel import (
    DispatchGroupState, ParallelAgentCoordinator, ParallelEventSink,
)
from framework.harness.task_plan.replay import _apply_parallel_event, _projection_for_plan
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from framework.harness.task_plan.stage import TaskPlanStageRunner
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request, _result, _admitted_request
from tests.framework.harness.agent_loop.test_orchestration_runtime import _runtime, _request as _parent_request
from tests.framework.harness.task_plan.test_durable_task_plan_store import _store, _EventStore, _ArtifactStore


@pytest.fixture
def crashed_wave():
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    events, calls = [], []
    supervisor = ChildAgentSupervisor(max_children=2)

    def append(event):
        if event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED":
            raise RuntimeError("crash before receipt commit")
        events.append(event)

    coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor,
                                          event_sink=ParallelEventSink(append, events.extend))

    def invoke(item):
        calls.append(item.task_id)
        return _result(plan, item)

    with pytest.raises(RuntimeError, match="crash before receipt"):
        coordinator.dispatch(request, invoke)
    session = next(iter(coordinator._sessions.values()))
    intents = tuple(item for item in events if item["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT")
    try:
        yield SimpleNamespace(plan=plan, request=_admitted_request(request), events=events, calls=calls,
                              supervisor=supervisor, coordinator=coordinator, session=session,
                              intents=intents, wave=session.waves[0], invoke=invoke)
    finally:
        supervisor.shutdown()


def recover(fixture, *, coordinator=None, intents=None, sink=None, group=None):
    return (coordinator or fixture.coordinator).reconcile_spawn_intents(
        fixture.request, fixture.intents if intents is None else intents, fixture.invoke,
        admitted_waves=(fixture.wave,), admitted_group=group or fixture.session.group,
        event_sink=sink or fixture.events.append,
    )


def replay(fixture):
    projection = TaskPlanScheduler().reserve_ready_tasks(
        _projection_for_plan(fixture.plan, sequence=1), TaskPlanReadyDecision(fixture.request.task_instances),
    )
    groups, waves, reservations, diagnostics, operations = {}, {}, {}, [], {}
    for sequence, payload in enumerate(fixture.events, 1):
        _apply_parallel_event(
            SimpleNamespace(event_type=payload["event_type"], payload=payload, sequence=sequence,
                            reason_code=payload.get("reason_code")),
            projection, groups, waves, reservations, diagnostics, operations,
        )
    return groups, waves, reservations, diagnostics, operations


def test_receipt_write_failure_recovery_is_audited_idempotent_and_replayable(crashed_wave, monkeypatch):
    fixture = crashed_wave
    assert fixture.session.spawn_receipts == {}
    original_status = fixture.supervisor.status
    reads = []

    def status(child_id, **kwargs):
        assert fixture.events[-1]["event_type"] == "RECOVERY_STATUS_READ"
        reads.append(child_id)
        return original_status(child_id, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("recovery invoked a live worker/spawn")

    monkeypatch.setattr(fixture.supervisor, "status", status)
    monkeypatch.setattr(fixture.supervisor, "spawn_batch", forbidden)
    original_workers = {key: value[1] for key, value in fixture.session.active_children.items()}
    result = recover(fixture)
    assert result.group.state is DispatchGroupState.RUNNING
    assert len(reads) == 2
    assert {key: value[1] for key, value in fixture.session.active_children.items()} == original_workers
    snapshot = list(fixture.events)
    assert recover(fixture).dispatch_checksum == result.dispatch_checksum
    assert fixture.events == snapshot and len(reads) == 2
    monkeypatch.setattr(fixture.supervisor, "status", forbidden)
    first = replay(fixture)
    assert replay(fixture) == first
    assert first[0][result.group.group_id]["state"] == result.group.state.value
    assert first[1][fixture.wave.wave_id]["state"] == "RUNNING"
    assert all(value["state"] == "RESERVED" for value in first[2].values())
    assert len(first[3]) >= 4


@pytest.mark.parametrize("field", ["group_id", "task_instance_id", "operation_key", "idempotency_key", "attempt"])
def test_all_intents_are_validated_before_live_reads_or_mutation(crashed_wave, monkeypatch, field):
    fixture = crashed_wave
    bad = dict(fixture.intents[-1])
    bad[field] = True if field == "attempt" else "incorrect-identity"
    events = list(fixture.events)
    monkeypatch.setattr(fixture.supervisor, "status", lambda *args, **kwargs: pytest.fail("unexpected status"))
    with pytest.raises(HarnessValidationError):
        recover(fixture, intents=(*fixture.intents[:-1], bad))
    assert fixture.events == events and fixture.session.spawn_receipts == {}


@pytest.mark.parametrize("subset", ["partial", "duplicate"])
def test_partial_or_duplicate_intents_cannot_dispatch_wave(crashed_wave, subset):
    fixture = crashed_wave
    intents = fixture.intents[:1] if subset == "partial" else (fixture.intents[0], fixture.intents[0])
    before = list(fixture.events)
    with pytest.raises(HarnessValidationError):
        recover(fixture, intents=intents)
    assert fixture.events == before


@pytest.mark.parametrize("failed_type", ["RECOVERY_STATUS_READ", "TASK_ATTEMPT_SPAWN_CONFIRMED", "RECOVERY_RECONCILED", "TASK_WAVE_DISPATCHED"])
def test_recovery_audit_write_failures_preserve_retryable_receipt_boundary(crashed_wave, failed_type):
    fixture = crashed_wave
    failed = False

    def sink(event):
        nonlocal failed
        if event["event_type"] == failed_type and not failed:
            failed = True
            raise RuntimeError("audit write failure")
        fixture.events.append(event)

    with pytest.raises(RuntimeError, match="audit write failure"):
        recover(fixture, sink=sink)
    result = recover(fixture)
    assert result.group.state is DispatchGroupState.RUNNING
    # Repeated delivery is deduplicated by the canonical sink; this raw fixture
    # removes exact receipt redelivery to emulate the persisted stream.
    seen, unique = set(), []
    for event in fixture.events:
        key = (event["event_type"], event.get("idempotency_key"))
        if key not in seen:
            unique.append(event)
            seen.add(key)
    fixture.events[:] = unique
    assert replay(fixture)[0][result.group.group_id]["state"] == "RUNNING"


def test_supervisor_identity_conflict_is_not_unknown_receipt(crashed_wave, monkeypatch):
    fixture = crashed_wave

    def conflict(*args, **kwargs):
        raise ChildAgentOperationConflict("identity conflict", code="operation_identity_conflict")

    monkeypatch.setattr(fixture.supervisor, "status", conflict)
    with pytest.raises(ChildAgentOperationConflict):
        recover(fixture)
    assert not any(event["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN" for event in fixture.events)
    assert any(event.get("reason_code") == "SPAWN_IDENTITY_CONFLICT" for event in fixture.events)
    replay(fixture)


def test_unknown_status_closes_admission_without_releasing_uncertain_reservations(crashed_wave, monkeypatch):
    fixture = crashed_wave

    def missing(*args, **kwargs):
        raise ChildAgentNotFoundError("unavailable child", code="child_not_found")

    monkeypatch.setattr(fixture.supervisor, "status", missing)
    result = recover(fixture)
    assert result.group.state is DispatchGroupState.INDETERMINATE
    assert fixture.session.reserved == {"task-1", "task-2"}
    assert not any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in fixture.events)
    snapshot = list(fixture.events)
    assert recover(fixture).dispatch_checksum == result.dispatch_checksum
    assert fixture.events == snapshot
    groups, _, reservations, _, _ = replay(fixture)
    assert groups[result.group.group_id]["state"] == "INDETERMINATE"
    assert all(value["state"] == "RESERVED" for value in reservations.values())


@pytest.mark.parametrize("change", [{"task_id": "wrong-task"}, {"state": "LOST"}, {"state": "CLOSED"}])
def test_untrackable_or_mismatched_handle_cannot_dispatch(crashed_wave, monkeypatch, change):
    fixture = crashed_wave
    status = fixture.supervisor.status
    monkeypatch.setattr(fixture.supervisor, "status", lambda *args, **kwargs: replace(status(*args, **kwargs), **change))
    with pytest.raises(HarnessValidationError):
        recover(fixture)
    assert not any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in fixture.events)
    assert not any(event["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN" for event in fixture.events)
    replay(fixture)


@pytest.mark.parametrize("state", ["FAILED", "CANCELLED", "HALTED", "SUPERSEDED", "SUCCEEDED"])
def test_restart_preserves_durable_terminal_group(crashed_wave, state):
    fixture = crashed_wave
    restarted = ParallelAgentCoordinator(max_workers=2, child_supervisor=fixture.supervisor)
    before = list(fixture.events)
    with pytest.raises(HarnessValidationError, match="terminal group"):
        recover(fixture, coordinator=restarted, group=replace(fixture.session.group, state=state))
    assert fixture.events == before and restarted._sessions == {}


@pytest.mark.parametrize("mutation", ["missing-read", "wrong-attempt", "wrong-child", "wrong-operation"])
def test_replay_rejects_unverifiable_recovery_audit(crashed_wave, mutation):
    fixture = crashed_wave
    recover(fixture)
    if mutation == "missing-read":
        fixture.events[:] = [event for event in fixture.events if event["event_type"] != "RECOVERY_STATUS_READ"]
    else:
        event = next(event for event in fixture.events if event["event_type"] == "RECOVERY_RECONCILED")
        field = {"wrong-attempt": "attempt", "wrong-child": "child_id", "wrong-operation": "operation_key"}[mutation]
        event[field] = 7 if field == "attempt" else "incorrect"
    with pytest.raises(HarnessValidationError):
        replay(fixture)


@pytest.mark.parametrize("later_outcome", ["conflict", "pending", "stale-confirmation"])
def test_newer_recovery_conflict_supersedes_an_older_confirmation(crashed_wave, later_outcome):
    fixture = crashed_wave
    recovered = recover(fixture)
    fixture.coordinator._mark_indeterminate(
        recovered.group.group_id, reason_code="child_runtime_indeterminate", event_sink=fixture.events.append,
    )
    confirmations = [event for event in fixture.events if event["event_type"] == "RECOVERY_RECONCILED"]
    for index, original in enumerate(confirmations):
        identity = {key: original[key] for key in (
            "group_id", "wave_id", "task_id", "task_instance_id", "attempt", "operation_key",
        )}
        recovery_id = f"later-recovery-{index}"
        fixture.events.append({
            "event_type": "RECOVERY_STATUS_READ", **identity, "recovery_id": recovery_id,
            "recovery_outcome": "status_read", "idempotency_key": f"{recovery_id}:status-read",
        })
        if index == 0 and later_outcome == "pending":
            continue
        if index == 0 and later_outcome == "stale-confirmation":
            fixture.events.append({
                "event_type": "RECOVERY_STATUS_READ", **identity, "recovery_id": "newest-pending",
                "recovery_outcome": "status_read", "idempotency_key": "newest-pending:status-read",
            })
        fixture.events.append({
            **identity, "recovery_id": recovery_id,
            **({"event_type": "RECOVERY_HALTED", "reason_code": "SPAWN_IDENTITY_CONFLICT",
                "idempotency_key": f"{recovery_id}:halted"} if index == 0 and later_outcome == "conflict" else {
                "event_type": "RECOVERY_RECONCILED", "recovery_outcome": "SPAWN_CONFIRMED",
                "child_id": original["child_id"], "idempotency_key": f"{recovery_id}:reconciled",
            }),
        })
    assert replay(fixture)[0][recovered.group.group_id]["state"] == "INDETERMINATE"


def test_durable_receipt_conflict_is_audited_before_recovery_stops(crashed_wave):
    fixture = crashed_wave

    def sink(event):
        if event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED":
            raise HarnessValidationError("conflicting durable receipt", code="task_plan_event_history_conflict")
        fixture.events.append(event)

    with pytest.raises(HarnessValidationError) as error:
        recover(fixture, sink=sink)
    assert error.value.code == "task_plan_event_history_conflict"
    assert fixture.session.spawn_receipts == {}
    assert fixture.session.group.state is DispatchGroupState.INDETERMINATE
    assert fixture.events[-1]["event_type"] == "RECOVERY_HALTED"
    assert fixture.events[-1]["reason_code"] == "SPAWN_IDENTITY_CONFLICT"
    replay(fixture)


def test_restart_finishes_active_wave_before_join_without_spawning_again(monkeypatch):
    events, artifacts = _EventStore(), _ArtifactStore()
    runtime, identity = _runtime(store=_store(events, artifacts))
    runner = runtime._stage_runner
    record = runner._record_parallel_events
    captured = {}

    class ProcessCrash(BaseException):
        pass

    def crash(request, plan, batch):
        captured.update(request=request, plan=plan)
        record(request, plan, batch)
        if any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in batch):
            raise ProcessCrash()

    monkeypatch.setattr(runner, "_record_parallel_events", crash)
    try:
        with pytest.raises(ProcessCrash):
            runtime.dispatch(_parent_request(identity))
        monkeypatch.setattr(runner, "_record_parallel_events", record)
        runner.parallel_coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=runtime._child_supervisor)
        monkeypatch.setattr(runtime._child_supervisor, "spawn_batch", lambda *args, **kwargs: pytest.fail("duplicate child"))
        request, plan = captured["request"], captured["plan"]
        runner._execute_plan_parallel(request, plan)
        report = runner._replay_history(request, plan)
        assert all(task.status is TaskLifecycle.SUCCEEDED for task in report.projection.tasks)
        assert all(wave["state"] == "TERMINAL" for wave in report.parallel_waves.values())
        assert all(group["state"] == "SUCCEEDED" for group in report.parallel_groups.values())
        assert sum(event.event_type == "TASK_GROUP_ADMITTED" for event in events._events) == 1
        assert sum(event.event_type == "TASK_WAVE_COMPLETED" for event in events._events) == 1
    finally:
        runtime._child_supervisor.shutdown()


@pytest.mark.parametrize("failed_point", ["receipt", "before-dispatch", "after-dispatch"])
def test_stage_recovers_canonical_admission_and_checkpoints_without_new_children(monkeypatch, failed_point):
    events, artifacts = _EventStore(), _ArtifactStore()
    runtime, identity = _runtime(store=_store(events, artifacts))
    runner = runtime._stage_runner
    captured = {}
    record = runner._record_parallel_events

    class ProcessCrash(BaseException):
        pass

    def crash(request, plan, batch):
        captured.update(request=request, plan=plan)
        failed_type = "TASK_ATTEMPT_SPAWN_CONFIRMED" if failed_point == "receipt" else "TASK_WAVE_DISPATCHED"
        if any(event["event_type"] == failed_type for event in batch):
            if failed_point == "after-dispatch":
                record(request, plan, batch)
            raise ProcessCrash()
        return record(request, plan, batch)

    monkeypatch.setattr(runner, "_record_parallel_events", crash)
    try:
        with pytest.raises(ProcessCrash):
            runtime.dispatch(_parent_request(identity))
        monkeypatch.setattr(runner, "_record_parallel_events", record)
        supervisor = runtime._child_supervisor
        runner.parallel_coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
        monkeypatch.setattr(supervisor, "spawn_batch", lambda *args, **kwargs: pytest.fail("duplicate child"))
        request, plan = captured["request"], captured["plan"]
        sink = runner._parallel_event_sink(request, plan)
        runner._recover_parallel_spawn_admission(request, plan, sink)
        count = len(events._events)
        runner._recover_parallel_spawn_admission(request, plan, sink)
        assert len(events._events) == count
        monkeypatch.setattr(supervisor, "status", lambda *args, **kwargs: pytest.fail("live replay read"))
        report = runner._replay_history(request, plan)
        assert set(group["state"] for group in report.parallel_groups.values()) == {"RUNNING"}
        assert all(item["state"] == "RESERVED" for item in report.parallel_reservations.values())
        assert report.replay_checksum == runner._replay_history(request, plan).replay_checksum
        audit_types = {event.event_type for event in events._events if event.event_type.startswith("RECOVERY_")}
        assert audit_types == {"RECOVERY_STATUS_READ", "RECOVERY_RECONCILED"}
        assert sum(event.event_type == "TASK_WAVE_DISPATCHED" for event in events._events) == 1
        catalog = default_event_schema_catalog()
        for event in events._events:
            catalog.validate(event.event_type, event.data_schema, event.payload)

        # A fresh process must re-read supervisor status to rebuild live
        # handles, while reusing the durable receipt and dispatch facts.
        original_status = ChildAgentSupervisor.status.__get__(supervisor)
        status_calls = []

        def audited_status(child_id, **kwargs):
            status_calls.append((child_id, kwargs["operation_id"]))
            return original_status(child_id, **kwargs)

        monkeypatch.setattr(supervisor, "status", audited_status)
        fresh_coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
        fresh_runner = TaskPlanStageRunner(
            candidate_builder=runner.candidate_builder,
            capability_registry=runner.capability_registry,
            store=runner.store,
            result_verifier=runner.result_verifier,
            worker_executor=runner.worker_executor,
            parallel_coordinator=fresh_coordinator,
            child_supervisor_capacity=supervisor.capacity,
            checkpoint_store=runner.checkpoint_store,
        )
        fresh_runner._recover_parallel_spawn_admission(
            request, plan, fresh_runner._parallel_event_sink(request, plan)
        )
        assert len(events._events) == count + 4
        assert len(status_calls) == 2
        assert sum(
            event.event_type in {"TASK_ATTEMPT_SPAWN_CONFIRMED", "TASK_ATTEMPT_SPAWN_UNKNOWN"}
            for event in events._events
        ) == 2
        assert sum(event.event_type == "TASK_WAVE_DISPATCHED" for event in events._events) == 1
    finally:
        runtime._child_supervisor.shutdown()
