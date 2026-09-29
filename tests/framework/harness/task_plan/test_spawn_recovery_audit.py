from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace

import pytest

from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import (
    ChildAgentNotFoundError,
    ChildAgentOperationConflict,
    ChildAgentState,
    ChildAgentSupervisor,
    ChildAgentTerminalReceipt,
)
from framework.harness.task_plan.attempt_history import TaskAttemptHistoryRecord
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskAdmissionOwner, TaskLifecycle
from framework.harness.task_plan.parallel import (
    DispatchGroupState, ParallelAgentCoordinator, ParallelEventSink,
)
from framework.harness.task_plan.replay import _apply_parallel_event, _projection_for_plan
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from framework.harness.task_plan.stage import TaskPlanStageRunner
from framework.harness.task_plan.verification import (
    TaskPlanGateRegistry,
    TaskPlanResultVerifier,
)
from framework.harness.workers.result import HarnessWorkerResult
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.task_plan.test_parallel_orchestration import _accepted_parallel_plan, _request, _result, _admitted_request
from tests.framework.harness.agent_loop.test_orchestration_runtime import _runtime, _request as _parent_request
from tests.framework.harness.task_plan.test_durable_task_plan_store import _store, _EventStore, _ArtifactStore


def _runtime_with_gate_owner(store):
    gates = TaskPlanGateRegistry()
    gates.register("gate@1", lambda _request: True, deterministic=True)
    return _runtime(
        store=store,
        result_verifier=TaskPlanResultVerifier(
            gates,
            gate_artifact_writer=store,
        ),
        worker_executor=lambda _binding, instance, _identity: HarnessWorkerResult(
            status="succeeded",
            output={"summary": instance.task_id},
        ),
    )


def _wait_for_spawned_wave(supervisor, session):
    """Join workers admitted before the simulated coordinator crash."""
    for task_id in sorted(session.active_children):
        handle, _worker = session.active_children[task_id]
        operation = supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
        )
        assert operation.receipt is not None


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
    # The receipt append fails after spawn_batch has submitted the whole wave.
    # Join those already-admitted workers before tests snapshot calls so later
    # mutations can only come from recovery dispatching work again.
    _wait_for_spawned_wave(supervisor, session)
    assert sorted(calls) == ["task-1", "task-2"]
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


def _admitted_projection(fixture):
    scheduler = TaskPlanScheduler()
    ready_order = tuple(
        instance.task_id for instance in fixture.request.task_instances
    )
    ready = scheduler.reserve_ready_tasks(
        _projection_for_plan(fixture.plan, sequence=1),
        TaskPlanReadyDecision(
            fixture.request.task_instances,
            logical_ready_task_ids=ready_order,
        ),
    )
    admission = next(
        event
        for event in fixture.events
        if event["event_type"] == "TASK_WAVE_ADMITTED"
    )
    wave = admission["wave"]
    packing = wave["packing"]
    assert tuple(wave["task_ids"]) == ready_order
    assert tuple(packing["ready_order"]) == ready_order
    assert tuple(packing["selected"]) == ready_order
    assert tuple(packing["overflow"]) == ()

    ledger_before = TaskPlanBudgetLedger.from_snapshot(ready.consumed_budget)
    projection = scheduler.admit_ready_tasks(
        ready,
        fixture.request.task_instances,
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    ledger_after = TaskPlanBudgetLedger.from_snapshot(projection.consumed_budget)
    assert admission["budget_before_checksum"] == ledger_before.to_dict()[
        "ledger_checksum"
    ]
    assert admission["budget_after_checksum"] == ledger_after.to_dict()[
        "ledger_checksum"
    ]
    assert packing["budget_before_checksum"] == admission["budget_before_checksum"]
    assert packing["budget_after_checksum"] == admission["budget_after_checksum"]
    assert projection.consumed_budget == fixture.request.budget_snapshot
    return projection


def replay(fixture):
    projection = _admitted_projection(fixture)
    groups, waves, reservations, diagnostics, operations = {}, {}, {}, [], {}
    for sequence, payload in enumerate(fixture.events, 1):
        if payload["event_type"] == "TASK_ATTEMPT_RECORDED":
            # The full reducer owns attempt history; this helper projects only
            # group/wave recovery state. Still verify the extra audit envelope.
            record = TaskAttemptHistoryRecord.from_dict(payload["history_record"])
            assert record.group["group_checksum"] == groups[payload["group_id"]]["group_checksum"]
            assert record.wave["wave_id"] == payload["wave_id"]
            operation = next(item for item in operations.values() if item["operation_key"] == record.operation_key)
            if record.child_id is None:
                assert record.outcome.value == "INDETERMINATE"
                assert operation["status"] in {"INTENT", "SPAWN_UNKNOWN"}
            else:
                assert operation["status"] == "SPAWN_CONFIRMED"
                assert record.child_id == operation["child_id"]
            continue
        _apply_parallel_event(
            SimpleNamespace(event_type=payload["event_type"], payload=payload, sequence=sequence,
                            reason_code=payload.get("reason_code")),
            projection, groups, waves, reservations, diagnostics, operations,
        )
    return groups, waves, reservations, diagnostics, operations


def _deduplicating_sink(events):
    def append(event):
        key = (event["event_type"], event.get("idempotency_key"))
        previous = next(
            (
                item
                for item in events
                if (item["event_type"], item.get("idempotency_key")) == key
            ),
            None,
        )
        if previous is None:
            events.append(event)
            return
        assert previous == event

    return append


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
    calls_before_recovery = tuple(fixture.calls)

    def missing(*args, **kwargs):
        raise ChildAgentNotFoundError("unavailable child", code="child_not_found")

    monkeypatch.setattr(fixture.supervisor, "status", missing)
    monkeypatch.setattr(
        fixture.supervisor,
        "spawn_batch",
        lambda *args, **kwargs: pytest.fail("unknown status authorized a new spawn"),
    )
    result = recover(fixture)
    assert result.group.state is DispatchGroupState.INDETERMINATE
    assert tuple(fixture.calls) == calls_before_recovery
    assert fixture.session.reserved == {"task-1", "task-2"}
    assert not any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in fixture.events)
    assert sum(
        event["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN"
        for event in fixture.events
    ) == 2
    snapshot = list(fixture.events)
    assert recover(fixture).dispatch_checksum == result.dispatch_checksum
    assert fixture.events == snapshot
    groups, _, reservations, _, _ = replay(fixture)
    assert groups[result.group.group_id]["state"] == "INDETERMINATE"
    assert all(value["state"] == "RESERVED" for value in reservations.values())


def test_indeterminate_event_write_failure_retries_same_coordinator_without_duplicate_status(
    monkeypatch,
):
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    events = []
    calls = []
    supervisor = ChildAgentSupervisor(max_children=2)
    receipt_failed = False
    indeterminate_failures = 0

    def append(event):
        nonlocal receipt_failed, indeterminate_failures
        if event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED" and not receipt_failed:
            receipt_failed = True
            raise RuntimeError("receipt write failure")
        if event["event_type"] == "TASK_GROUP_INDETERMINATE" and indeterminate_failures < 2:
            indeterminate_failures += 1
            raise RuntimeError("indeterminate write failure")
        events.append(event)

    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(append, events.extend),
    )

    def invoke(item):
        calls.append(item.task_id)
        return _result(plan, item)

    try:
        with pytest.raises(RuntimeError, match="indeterminate write failure"):
            coordinator.dispatch(request, invoke)
        session = next(iter(coordinator._sessions.values()))
        # Group dispatch admission advanced before the receipt write failed;
        # the failed terminal event must preserve that exact pre-transition.
        assert session.group.state is DispatchGroupState.DISPATCHING
        intents = tuple(
            event
            for event in events
            if event["event_type"] == "TASK_ATTEMPT_SPAWN_INTENT"
        )
        wave = session.waves[0]
        admitted_request = _admitted_request(request)
        _wait_for_spawned_wave(supervisor, session)
        assert sorted(calls) == ["task-1", "task-2"]
        calls_before_recovery = tuple(calls)
        reads = []

        def missing(child_id, **kwargs):
            reads.append((child_id, kwargs["operation_id"]))
            raise ChildAgentNotFoundError(
                "unavailable child",
                code="child_not_found",
            )

        monkeypatch.setattr(supervisor, "status", missing)
        monkeypatch.setattr(
            supervisor,
            "spawn_batch",
            lambda *args, **kwargs: pytest.fail("recovery spawned after audit failure"),
        )

        with pytest.raises(RuntimeError, match="indeterminate write failure"):
            coordinator.reconcile_spawn_intents(
                admitted_request,
                intents,
                invoke,
                admitted_waves=(wave,),
                admitted_group=session.group,
            )
        assert session.group.state is DispatchGroupState.DISPATCHING
        assert len(reads) == 2

        result = coordinator.reconcile_spawn_intents(
            admitted_request,
            intents,
            invoke,
            admitted_waves=(wave,),
            admitted_group=session.group,
        )
        assert result.group.state is DispatchGroupState.INDETERMINATE
        assert len(reads) == 2
        assert tuple(calls) == calls_before_recovery
        assert session.reserved == {"task-1", "task-2"}
        assert sum(
            event["event_type"] == "TASK_GROUP_INDETERMINATE"
            for event in events
        ) == 1
        assert sum(
            event["event_type"] == "TASK_ATTEMPT_RECORDED"
            for event in events
        ) == 2
        assert not any(
            event["event_type"] == "TASK_WAVE_DISPATCHED"
            for event in events
        )
    finally:
        supervisor.shutdown()


def test_fresh_supervisors_reaudit_missing_state_without_spawning_or_dispatching(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    durable_append = _deduplicating_sink(fixture.events)
    calls_before_recovery = tuple(fixture.calls)
    status_calls = []

    for restart in range(2):
        supervisor = ChildAgentSupervisor(max_children=2)
        coordinator = ParallelAgentCoordinator(
            max_workers=2,
            child_supervisor=supervisor,
        )
        original_status = supervisor.status

        def status(child_id, *, operation_id, _restart=restart):
            status_calls.append((_restart, child_id, operation_id))
            return original_status(child_id, operation_id=operation_id)

        monkeypatch.setattr(supervisor, "status", status)
        monkeypatch.setattr(
            supervisor,
            "spawn_batch",
            lambda *args, **kwargs: pytest.fail("fresh supervisor spawned unknown operation"),
        )
        try:
            result = recover(
                fixture,
                coordinator=coordinator,
                sink=durable_append,
            )
            assert result.group.state is DispatchGroupState.INDETERMINATE
            before_repeat = list(fixture.events)
            assert recover(
                fixture,
                coordinator=coordinator,
                sink=durable_append,
            ).dispatch_checksum == result.dispatch_checksum
            assert fixture.events == before_repeat
        finally:
            supervisor.shutdown()

    assert tuple(fixture.calls) == calls_before_recovery
    assert len(status_calls) == 4
    reads = [
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_STATUS_READ"
    ]
    halted = [
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_HALTED"
        and event.get("reason_code") == "SPAWN_UNKNOWN"
    ]
    assert len(reads) == len(halted) == 4
    assert len({event["recovery_id"] for event in reads}) == 4
    assert {event["recovery_id"] for event in reads} == {
        event["recovery_id"] for event in halted
    }
    assert sum(
        event["event_type"] == "TASK_ATTEMPT_SPAWN_UNKNOWN"
        for event in fixture.events
    ) == 2
    assert sum(
        event["event_type"] == "TASK_ATTEMPT_RECORDED"
        for event in fixture.events
    ) == 2
    assert not any(
        event["event_type"] == "TASK_WAVE_DISPATCHED"
        for event in fixture.events
    )
    groups, _, reservations, _, _ = replay(fixture)
    assert groups[fixture.session.group.group_id]["state"] == "INDETERMINATE"
    assert all(item["state"] == "RESERVED" for item in reservations.values())


def test_partial_confirmed_and_unknown_recovery_does_not_dispatch_or_respawn(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    supervisor = fixture.supervisor
    coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
    original_status = supervisor.status
    confirmed_child_id = fixture.intents[0]["task_instance_id"]
    reads = []

    def partial_status(child_id, *, operation_id):
        reads.append((child_id, operation_id))
        if child_id.endswith(confirmed_child_id):
            return original_status(child_id, operation_id=operation_id)
        raise ChildAgentNotFoundError("unavailable child", code="child_not_found")

    monkeypatch.setattr(supervisor, "status", partial_status)
    monkeypatch.setattr(
        supervisor,
        "spawn_batch",
        lambda *args, **kwargs: pytest.fail("partial recovery respawned a sibling"),
    )
    calls_before_recovery = tuple(fixture.calls)
    result = recover(fixture, coordinator=coordinator)

    assert result.group.state is DispatchGroupState.INDETERMINATE
    assert tuple(fixture.calls) == calls_before_recovery
    assert len(reads) == 2
    receipts = [
        event["event_type"]
        for event in fixture.events
        if event["event_type"] in {
            "TASK_ATTEMPT_SPAWN_CONFIRMED",
            "TASK_ATTEMPT_SPAWN_UNKNOWN",
        }
    ]
    assert receipts.count("TASK_ATTEMPT_SPAWN_CONFIRMED") == 1
    assert receipts.count("TASK_ATTEMPT_SPAWN_UNKNOWN") == 1
    assert len(coordinator._sessions[result.group.group_id].active_children) == 1
    assert not any(
        event["event_type"] == "TASK_WAVE_DISPATCHED"
        for event in fixture.events
    )
    groups, _, reservations, _, _ = replay(fixture)
    assert groups[result.group.group_id]["state"] == "INDETERMINATE"
    assert all(item["state"] == "RESERVED" for item in reservations.values())


def test_recovery_wait_audit_failure_blocks_live_call_until_retry(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    recover(fixture)
    wait_calls = []
    original_wait = fixture.supervisor.wait
    failed = False

    def audited_wait(child_id, *, operation_id, timeout_seconds=None):
        wait_calls.append((child_id, operation_id))
        return original_wait(
            child_id,
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )

    def sink(event):
        nonlocal failed
        if event["event_type"] == "RECOVERY_OPERATION_INTENT" and not failed:
            failed = True
            raise RuntimeError("recovery wait audit failure")
        fixture.events.append(event)

    monkeypatch.setattr(fixture.supervisor, "wait", audited_wait)
    with pytest.raises(RuntimeError, match="recovery wait audit failure"):
        fixture.coordinator.recover(
            fixture.request,
            (),
            event_sink=sink,
        )
    assert wait_calls == []
    assert not any(
        event["event_type"] == "RECOVERY_OPERATION_INTENT"
        for event in fixture.events
    )

    fixture.coordinator.recover(
        fixture.request,
        (),
        event_sink=sink,
    )
    assert len(wait_calls) == 2
    intents = [
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_OPERATION_INTENT"
        and event["recovery_operation"] == "wait"
    ]
    reconciled = [
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_OPERATION_RECONCILED"
        and event["recovery_operation"] == "wait"
    ]
    assert len(intents) == len(reconciled) == 2
    assert {
        (event["recovery_id"], event["operation_key"])
        for event in intents
    } == {
        (event["recovery_id"], event["operation_key"])
        for event in reconciled
    }


def test_online_recovery_without_audit_sink_does_not_wait_or_close(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=fixture.supervisor,
    )
    # Rebind the admitted in-memory session to the fresh coordinator without
    # supplying an event sink; the first live recovery operation must fail
    # closed before touching the supervisor.
    coordinator._sessions[fixture.session.group.group_id] = fixture.session
    wait_calls = []
    close_calls = []
    original_wait = fixture.supervisor.wait
    original_close = fixture.supervisor.close

    def audited_wait(child_id, *, operation_id, timeout_seconds=None):
        wait_calls.append((child_id, operation_id))
        return original_wait(
            child_id,
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )

    def audited_close(child_id, *, operation_id):
        close_calls.append((child_id, operation_id))
        return original_close(child_id, operation_id=operation_id)

    monkeypatch.setattr(fixture.supervisor, "wait", audited_wait)
    monkeypatch.setattr(fixture.supervisor, "close", audited_close)
    with pytest.raises(HarnessValidationError) as captured:
        coordinator.recover(fixture.request, (), event_sink=None)
    assert captured.value.code == "TASK_GROUP_RECOVERY_AUDIT_REQUIRED"
    assert wait_calls == []
    assert close_calls == []


def test_recovery_wait_requires_confirmed_terminal_receipt_before_success_audit(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    recover(fixture)
    original_wait = fixture.supervisor.wait
    close_calls = []
    first_recovery_event = len(fixture.events)

    def unconfirmed_wait(child_id, *, operation_id, timeout_seconds=None):
        operation = original_wait(
            child_id,
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )
        assert operation.receipt is not None
        return replace(
            operation,
            receipt=replace(
                operation.receipt,
                status=ChildAgentState.LOST,
                reason_code="termination_unconfirmed",
                result_ref=None,
                result_checksum=None,
                termination_confirmed=False,
            ),
        )

    monkeypatch.setattr(fixture.supervisor, "wait", unconfirmed_wait)
    monkeypatch.setattr(
        fixture.supervisor,
        "close",
        lambda child_id, *, operation_id: close_calls.append((child_id, operation_id)),
    )
    with pytest.raises(HarnessValidationError) as captured:
        fixture.coordinator.recover(
            fixture.request,
            (),
            event_sink=fixture.events.append,
        )
    assert captured.value.code == "TASK_GROUP_RECOVERY_UNCONFIRMED"
    recovery_events = fixture.events[first_recovery_event:]
    assert [item["event_type"] for item in recovery_events] == [
        "RECOVERY_OPERATION_INTENT",
        "RECOVERY_OPERATION_HALTED",
    ]
    assert close_calls == []


def test_recovery_wait_rejects_forged_operation_identity_before_success_audit(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    recover(fixture)
    original_wait = fixture.supervisor.wait
    first_recovery_event = len(fixture.events)

    def forged_wait(child_id, *, operation_id, timeout_seconds=None):
        operation = original_wait(
            child_id,
            operation_id=operation_id,
            timeout_seconds=timeout_seconds,
        )
        return replace(operation, child_id="forged-child")

    monkeypatch.setattr(fixture.supervisor, "wait", forged_wait)
    with pytest.raises(HarnessValidationError) as captured:
        fixture.coordinator.recover(
            fixture.request,
            (),
            event_sink=fixture.events.append,
        )
    assert captured.value.code == "TASK_GROUP_RECOVERY_IDENTITY_MISMATCH"
    assert [item["event_type"] for item in fixture.events[first_recovery_event:]] == [
        "RECOVERY_OPERATION_INTENT",
        "RECOVERY_OPERATION_HALTED",
    ]


def test_closed_child_recovery_retries_after_close_outcome_audit_failure(
    crashed_wave,
    monkeypatch,
):
    fixture = crashed_wave
    recover(fixture)
    close_calls = []
    failed = False
    original_close = fixture.supervisor.close

    def audited_close(child_id, *, operation_id):
        close_calls.append((child_id, operation_id))
        return original_close(child_id, operation_id=operation_id)

    def sink(event):
        nonlocal failed
        if (
            event["event_type"] == "RECOVERY_OPERATION_RECONCILED"
            and event["recovery_operation"] == "close"
            and not failed
        ):
            failed = True
            raise RuntimeError("close outcome audit failure")
        fixture.events.append(event)

    monkeypatch.setattr(fixture.supervisor, "close", audited_close)
    monkeypatch.setattr(
        fixture.supervisor,
        "spawn_batch",
        lambda *args, **kwargs: pytest.fail("recovery retried a confirmed spawn"),
    )
    with pytest.raises(RuntimeError, match="close outcome audit failure"):
        fixture.coordinator.recover(fixture.request, (), event_sink=sink)
    assert len(close_calls) == 1

    fixture.coordinator.recover(fixture.request, (), event_sink=sink)

    assert len(close_calls) == 3
    wait_outcomes = [
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_OPERATION_RECONCILED"
        and event["recovery_operation"] == "wait"
    ]
    assert any(event["child_state"] == "CLOSED" for event in wait_outcomes)
    receipts_by_operation = {}
    for event in (
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_OPERATION_RECONCILED"
    ):
        receipt = event["terminal_receipt"]
        receipts_by_operation.setdefault(event["operation_key"], receipt["receipt_checksum"])
        assert receipts_by_operation[event["operation_key"]] == receipt["receipt_checksum"]


def test_replay_rejects_recovery_receipt_replacement_after_first_confirmation(
    crashed_wave,
):
    fixture = crashed_wave
    recover(fixture)
    fixture.coordinator.recover(
        fixture.request,
        (),
        event_sink=fixture.events.append,
    )
    outcome = next(
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_OPERATION_RECONCILED"
        and event["recovery_operation"] == "close"
    )
    raw_receipt = dict(outcome["terminal_receipt"])
    supplied_checksum = raw_receipt.pop("receipt_checksum")
    assert supplied_checksum
    raw_receipt["completed_at"] = datetime.fromisoformat(raw_receipt["completed_at"])
    receipt = ChildAgentTerminalReceipt(**raw_receipt)
    outcome["terminal_receipt"] = replace(
        receipt,
        reason_code="replacement-receipt",
    ).to_dict()
    with pytest.raises(HarnessValidationError):
        replay(fixture)


def test_replay_rejects_reused_recovery_id_across_status_and_live_operation(
    crashed_wave,
):
    fixture = crashed_wave
    recover(fixture)
    fixture.coordinator.recover(
        fixture.request,
        (),
        event_sink=fixture.events.append,
    )
    status_read = next(
        event for event in fixture.events
        if event["event_type"] == "RECOVERY_STATUS_READ"
    )
    operation_id = next(
        event["recovery_id"] for event in fixture.events
        if event["event_type"] == "RECOVERY_OPERATION_INTENT"
    )
    for event in fixture.events:
        if event.get("recovery_id") != operation_id:
            continue
        event["recovery_id"] = status_read["recovery_id"]
        if event["event_type"] == "RECOVERY_OPERATION_INTENT":
            suffix = "intent"
        elif event["event_type"] == "RECOVERY_OPERATION_RECONCILED":
            suffix = "reconciled"
        else:
            suffix = "halted"
        event["idempotency_key"] = (
            f"{event['recovery_id']}:{event['recovery_operation']}:{suffix}"
        )
    with pytest.raises(HarnessValidationError):
        replay(fixture)


@pytest.mark.parametrize("change", [{"task_id": "wrong-task"}, {"state": "LOST"}])
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
    runtime, identity = _runtime_with_gate_owner(_store(events, artifacts))
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


def test_confirmed_receipts_are_not_rewritten_when_fresh_supervisor_loses_state(
    monkeypatch,
):
    events, artifacts = _EventStore(), _ArtifactStore()
    runtime, identity = _runtime(store=_store(events, artifacts))
    runner = runtime._stage_runner
    record = runner._record_parallel_events
    captured = {}

    class ProcessCrash(BaseException):
        pass

    def crash_before_dispatch(request, plan, batch):
        captured.update(request=request, plan=plan)
        if any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in batch):
            raise ProcessCrash()
        return record(request, plan, batch)

    monkeypatch.setattr(runner, "_record_parallel_events", crash_before_dispatch)
    try:
        with pytest.raises(ProcessCrash):
            runtime.dispatch(_parent_request(identity))
    finally:
        runtime._child_supervisor.shutdown()

    request, plan = captured["request"], captured["plan"]
    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
    fresh_runner = TaskPlanStageRunner(
        candidate_builder=runner.candidate_builder,
        capability_registry=runner.capability_registry,
        store=runner.store,
        result_verifier=runner.result_verifier,
        worker_executor=runner.worker_executor,
        parallel_coordinator=coordinator,
        child_supervisor_capacity=supervisor.capacity,
        checkpoint_store=runner.checkpoint_store,
    )
    status_calls = []
    original_status = supervisor.status

    def audited_missing_status(child_id, *, operation_id):
        status_calls.append((child_id, operation_id))
        return original_status(child_id, operation_id=operation_id)

    monkeypatch.setattr(supervisor, "status", audited_missing_status)
    monkeypatch.setattr(
        supervisor,
        "spawn_batch",
        lambda *args, **kwargs: pytest.fail("confirmed receipt was respawned"),
    )
    monkeypatch.setattr(
        fresh_runner,
        "_invoke",
        lambda *args, **kwargs: pytest.fail("confirmed receipt re-invoked a worker"),
    )
    try:
        with pytest.raises(HarnessValidationError) as error:
            fresh_runner._recover_parallel_spawn_admission(
                request,
                plan,
                fresh_runner._parallel_event_sink(request, plan),
            )
        assert error.value.code == "task_plan_event_history_conflict"
        assert len(status_calls) == 1
        event_types = [event.event_type for event in events._events]
        assert event_types.count("TASK_ATTEMPT_SPAWN_CONFIRMED") == 2
        assert event_types.count("TASK_ATTEMPT_SPAWN_UNKNOWN") == 0
        assert event_types.count("TASK_WAVE_DISPATCHED") == 0
        assert event_types.count("RECOVERY_STATUS_READ") == 1
        halted = [
            event
            for event in events._events
            if event.event_type == "RECOVERY_HALTED"
        ]
        assert len(halted) == 1
        assert halted[0].payload["reason_code"] == "SPAWN_IDENTITY_CONFLICT"
        report = fresh_runner._replay_history(request, plan)
        assert {
            group["state"] for group in report.parallel_groups.values()
        } == {"INDETERMINATE"}
        assert len(report.parallel_reservations) == 2
        assert all(
            reservation["state"] == "RESERVED"
            for reservation in report.parallel_reservations.values()
        )
        ledger = TaskPlanBudgetLedger.from_snapshot(
            report.projection.consumed_budget
        )
        assert len(ledger.records) == 2
        attempt_events = [
            event
            for event in events._events
            if event.event_type == "TASK_ATTEMPT_RECORDED"
        ]
        assert len(attempt_events) == 2
        attempt_identities = {
            (record.task_instance_id, record.attempt)
            for event in attempt_events
            for record in (
                TaskAttemptHistoryRecord.from_dict(
                    event.payload["details"]["history_record"]
                ),
            )
        }
        confirmed_attempt_identities = {
            (
                event.payload["details"]["task_instance_id"],
                event.payload["details"]["attempt"],
            )
            for event in events._events
            if event.event_type == "TASK_ATTEMPT_SPAWN_CONFIRMED"
        }
        assert len(attempt_identities) == 2
        assert attempt_identities == confirmed_attempt_identities
    finally:
        supervisor.shutdown()


def test_sqlite_reopen_with_same_supervisor_recovers_confirmed_wave_once(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "confirmed-spawn-recovery.sqlite3"
    events = SQLiteEventStore(database)
    artifacts = _ArtifactStore()
    runtime, identity = _runtime_with_gate_owner(_store(events, artifacts))
    runner = runtime._stage_runner
    record = runner._record_parallel_events
    captured = {}
    physical_spawns = []
    original_spawn_batch = runtime._child_supervisor.spawn_batch

    class ProcessCrash(BaseException):
        pass

    def counted_spawn_batch(requests, **kwargs):
        physical_spawns.append(tuple(request.operation_id for request in requests))
        return original_spawn_batch(requests, **kwargs)

    def crash_before_dispatch(request, plan, batch):
        captured.update(request=request, plan=plan)
        if any(event["event_type"] == "TASK_WAVE_DISPATCHED" for event in batch):
            raise ProcessCrash()
        return record(request, plan, batch)

    supervisor = runtime._child_supervisor
    monkeypatch.setattr(supervisor, "spawn_batch", counted_spawn_batch)
    monkeypatch.setattr(runner, "_record_parallel_events", crash_before_dispatch)
    try:
        with pytest.raises(ProcessCrash):
            runtime.dispatch(_parent_request(identity))
        assert len(physical_spawns) == 1
        monkeypatch.setattr(runner, "_record_parallel_events", record)

        request, plan = captured["request"], captured["plan"]
        reopened_store = _store(SQLiteEventStore(database), artifacts)
        status_calls = []
        original_status = ChildAgentSupervisor.status.__get__(supervisor)
        wait_calls = []
        original_wait = supervisor.wait
        close_calls = []
        original_close = supervisor.close

        def audited_status(child_id, *, operation_id):
            status_calls.append((child_id, operation_id))
            return original_status(child_id, operation_id=operation_id)

        def audited_wait(child_id, *, operation_id, timeout_seconds=None):
            wait_calls.append((child_id, operation_id))
            return original_wait(
                child_id,
                operation_id=operation_id,
                timeout_seconds=timeout_seconds,
            )

        def audited_close(child_id, *, operation_id):
            close_calls.append((child_id, operation_id))
            return original_close(child_id, operation_id=operation_id)

        monkeypatch.setattr(supervisor, "status", audited_status)
        monkeypatch.setattr(supervisor, "wait", audited_wait)
        monkeypatch.setattr(supervisor, "close", audited_close)
        monkeypatch.setattr(
            supervisor,
            "spawn_batch",
            lambda *args, **kwargs: pytest.fail("confirmed SQLite wave respawned"),
        )
        coordinator = ParallelAgentCoordinator(
            max_workers=2,
            child_supervisor=supervisor,
        )
        fresh_runner = TaskPlanStageRunner(
            candidate_builder=runner.candidate_builder,
            capability_registry=runner.capability_registry,
            store=reopened_store,
            result_verifier=runner.result_verifier,
            worker_executor=runner.worker_executor,
            parallel_coordinator=coordinator,
            child_supervisor_capacity=supervisor.capacity,
            checkpoint_store=runner.checkpoint_store,
        )
        fresh_runner._recover_parallel_spawn_admission(
            request,
            plan,
            fresh_runner._parallel_event_sink(request, plan),
        )
        recovery_status_calls = tuple(status_calls)
        assert len(recovery_status_calls) == 2
        assert wait_calls == []
        recovered_history = reopened_store.read_events(
            request.run_id,
            request.stage_id,
        )
        assert sum(
            event.event_type == "RECOVERY_STATUS_READ"
            for event in recovered_history
        ) == 2
        assert sum(
            event.event_type == "TASK_WAVE_DISPATCHED"
            for event in recovered_history
        ) == 1

        fresh_runner._execute_plan_parallel(request, plan)

        assert len(physical_spawns) == 1
        assert len(wait_calls) == 2
        assert len(close_calls) == 2
        assert tuple(status_calls[:2]) == recovery_status_calls
        assert tuple(status_calls[2:]) == tuple(wait_calls)
        assert set(status_calls[2:]) == set(recovery_status_calls)
        history = reopened_store.read_events(request.run_id, request.stage_id)
        event_types = [event.event_type for event in history]
        assert event_types.count("TASK_GROUP_ADMITTED") == 1
        assert event_types.count("TASK_WAVE_ADMITTED") == 1
        assert event_types.count("TASK_ATTEMPT_SPAWN_INTENT") == 2
        assert event_types.count("TASK_ATTEMPT_SPAWN_CONFIRMED") == 2
        assert event_types.count("TASK_ATTEMPT_SPAWN_UNKNOWN") == 0
        assert event_types.count("TASK_WAVE_DISPATCHED") == 1
        assert event_types.count("TASK_WAVE_COMPLETED") == 1
        operation_intents = [
            event for event in history
            if event.event_type == "RECOVERY_OPERATION_INTENT"
        ]
        operation_outcomes = [
            event for event in history
            if event.event_type == "RECOVERY_OPERATION_RECONCILED"
        ]
        assert len(operation_intents) == len(operation_outcomes) == 4
        assert not any(
            event.event_type == "RECOVERY_OPERATION_HALTED"
            for event in history
        )
        audit_operations = {
            (
                event.payload["recovery_id"],
                event.payload["recovery_operation"],
                event.payload["child_id"],
                event.payload["operation_key"],
            )
            for event in operation_intents
        }
        assert len(audit_operations) == 4
        assert audit_operations == {
            (
                event.payload["recovery_id"],
                event.payload["recovery_operation"],
                event.payload["child_id"],
                event.payload["operation_key"],
            )
            for event in operation_outcomes
        }
        assert {event.payload["recovery_operation"] for event in operation_intents} == {
            "wait", "close",
        }
        assert {
            event.payload["operation_key"]
            for event in operation_intents
            if event.payload["recovery_operation"] == "wait"
        } == {operation_id for _, operation_id in wait_calls}
        assert {
            event.payload["operation_key"]
            for event in operation_intents
            if event.payload["recovery_operation"] == "close"
        } == {operation_id for _, operation_id in close_calls}

        report = fresh_runner._replay_history(request, plan)
        assert all(
            task.status is TaskLifecycle.SUCCEEDED and task.attempts == 1
            for task in report.projection.tasks
        )
        assert {
            group["state"] for group in report.parallel_groups.values()
        } == {"SUCCEEDED"}
        assert len(report.parallel_reservations) == 2
        assert all(
            reservation["state"] == "CONSUMED"
            for reservation in report.parallel_reservations.values()
        )
        ledger = TaskPlanBudgetLedger.from_snapshot(
            report.projection.consumed_budget
        )
        assert len(ledger.records) == 2
        intent_instances = {
            event.payload["task_instance_id"]
            for event in history
            if event.event_type == "TASK_ATTEMPT_SPAWN_INTENT"
        }
        assert {
            event.payload["task_instance_id"]
            for event in history
            if event.event_type == "TASK_ATTEMPT_RECORDED"
        } == intent_instances
    finally:
        supervisor.shutdown()


def test_sqlite_reopen_with_fresh_supervisor_fails_closed_on_missing_spawn_receipts(
    tmp_path,
    monkeypatch,
):
    database = tmp_path / "spawn-recovery.sqlite3"
    events = SQLiteEventStore(database)
    artifacts = _ArtifactStore()
    runtime, identity = _runtime(store=_store(events, artifacts))
    runner = runtime._stage_runner
    record = runner._record_parallel_events
    captured = {}
    physical_spawns = []
    original_spawn_batch = runtime._child_supervisor.spawn_batch

    class ProcessCrash(BaseException):
        pass

    def counted_spawn_batch(requests, **kwargs):
        physical_spawns.append(tuple(request.operation_id for request in requests))
        return original_spawn_batch(requests, **kwargs)

    def crash_before_receipt(request, plan, batch):
        captured.update(request=request, plan=plan)
        if any(
            event["event_type"] == "TASK_ATTEMPT_SPAWN_CONFIRMED"
            for event in batch
        ):
            raise ProcessCrash()
        return record(request, plan, batch)

    monkeypatch.setattr(runtime._child_supervisor, "spawn_batch", counted_spawn_batch)
    monkeypatch.setattr(runner, "_record_parallel_events", crash_before_receipt)
    try:
        with pytest.raises(ProcessCrash):
            runtime.dispatch(_parent_request(identity))
        assert len(physical_spawns) == 1
    finally:
        runtime._child_supervisor.shutdown()

    request, plan = captured["request"], captured["plan"]
    reopened_events = SQLiteEventStore(database)
    reopened_store = _store(reopened_events, artifacts)
    supervisor = ChildAgentSupervisor(max_children=2)
    status_calls = []
    original_status = supervisor.status

    def audited_missing_status(child_id, *, operation_id):
        status_calls.append((child_id, operation_id))
        return original_status(child_id, operation_id=operation_id)

    monkeypatch.setattr(supervisor, "status", audited_missing_status)
    monkeypatch.setattr(
        supervisor,
        "spawn_batch",
        lambda *args, **kwargs: pytest.fail("SQLite recovery started a new child"),
    )
    coordinator = ParallelAgentCoordinator(max_workers=2, child_supervisor=supervisor)
    fresh_runner = TaskPlanStageRunner(
        candidate_builder=runner.candidate_builder,
        capability_registry=runner.capability_registry,
        store=reopened_store,
        result_verifier=runner.result_verifier,
        worker_executor=runner.worker_executor,
        parallel_coordinator=coordinator,
        child_supervisor_capacity=supervisor.capacity,
        checkpoint_store=runner.checkpoint_store,
    )
    monkeypatch.setattr(
        fresh_runner,
        "_invoke",
        lambda *args, **kwargs: pytest.fail("SQLite recovery invoked a worker"),
    )

    try:
        sink = fresh_runner._parallel_event_sink(request, plan)
        with pytest.raises(HarnessValidationError) as error:
            fresh_runner._recover_parallel_spawn_admission(request, plan, sink)
        assert error.value.code == "task_plan_spawn_recovery_indeterminate"
        assert len(status_calls) == 2
        assert len(physical_spawns) == 1

        history = reopened_store.read_events(request.run_id, request.stage_id)
        event_types = [event.event_type for event in history]
        assert event_types.count("TASK_ATTEMPT_SPAWN_INTENT") == 2
        assert event_types.count("TASK_ATTEMPT_SPAWN_UNKNOWN") == 2
        assert event_types.count("TASK_ATTEMPT_SPAWN_CONFIRMED") == 0
        assert event_types.count("TASK_WAVE_DISPATCHED") == 0
        assert event_types.count("RECOVERY_STATUS_READ") == 2
        assert event_types.count("RECOVERY_HALTED") == 2

        before_repeat = tuple(history)
        with pytest.raises(HarnessValidationError) as repeated:
            fresh_runner._recover_parallel_spawn_admission(request, plan, sink)
        assert repeated.value.code == "task_plan_spawn_recovery_indeterminate"
        assert reopened_store.read_events(request.run_id, request.stage_id) == before_repeat
        assert len(status_calls) == 2
        assert len(physical_spawns) == 1

        monkeypatch.setattr(
            supervisor,
            "status",
            lambda *args, **kwargs: pytest.fail("offline replay read supervisor state"),
        )
        report = fresh_runner._replay_history(request, plan)
        assert report.replay_checksum == fresh_runner._replay_history(
            request,
            plan,
        ).replay_checksum
        assert {
            group["state"] for group in report.parallel_groups.values()
        } == {"INDETERMINATE"}
        assert len(report.parallel_reservations) == 2
        assert all(
            reservation["state"] == "RESERVED"
            for reservation in report.parallel_reservations.values()
        )
        ledger = TaskPlanBudgetLedger.from_snapshot(
            report.projection.consumed_budget
        )
        assert len(ledger.records) == 2
        attempt_events = [
            event
            for event in before_repeat
            if event.event_type == "TASK_ATTEMPT_RECORDED"
        ]
        assert len(attempt_events) == 2
        assert len({
            event.payload["task_instance_id"] for event in attempt_events
        }) == 2
    finally:
        supervisor.shutdown()
