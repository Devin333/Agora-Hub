from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from framework.events import EventRuntime, EventSchemaCatalog
from framework.harness.subagents import (
    ChildAgentHeartbeat,
    ChildAgentState,
    ChildAgentSupervisor,
    ChildAgentSupervisorError,
    DurableChildAgentEventLog,
    HarnessOwnedChildAgentRuntime,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.subagents.test_child_agent_supervisor import _request


def _log(tmp_path, **kwargs):
    store = SQLiteEventStore(tmp_path / "events.sqlite3")
    runtime = EventRuntime(
        store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite"
    )
    return DurableChildAgentEventLog(
        state_runtime=runtime, state_reader=store, state_key="shared-children", **kwargs
    )


def _owned(log, **kwargs):
    runtime = HarnessOwnedChildAgentRuntime(
        event_log=log, max_children=2, renewal_interval_seconds=None, **kwargs
    )
    runtime.start()
    runtime.register_run_scope("run-1", "tenant-a")
    return runtime


@pytest.mark.parametrize("event_type", ("child_spawned", "child_terminal"))
def test_lost_commit_ack_blocks_live_writes_until_durable_recovery(
    tmp_path, monkeypatch, event_type
):
    log = _log(tmp_path)
    log.acquire_owner("first")
    log.register_run_scope("run-1", "tenant-a")
    supervisor = ChildAgentSupervisor(
        event_sink=log, event_reader=log, owner_guard=log.assert_owner, max_children=2
    )
    original_commit = log._runtime.compare_and_swap_transactional_state

    def commit_then_lose_ack(snapshot, **kwargs):
        result = original_commit(snapshot, **kwargs)
        events = snapshot.payload.get("events", ())
        if events and events[-1]["event_type"] == event_type:
            raise OSError("storage committed but acknowledgement was lost")
        return result

    try:
        if event_type == "child_terminal":
            handle = supervisor.spawn(_request(lease_seconds=60))
        monkeypatch.setattr(
            log._runtime, "compare_and_swap_transactional_state", commit_then_lose_ack
        )
        with pytest.raises(ChildAgentSupervisorError) as raised:
            if event_type == "child_spawned":
                supervisor.spawn(
                    _request(lease_seconds=60),
                    worker=lambda _: pytest.fail(
                        "worker started before acknowledged spawn"
                    ),
                )
            else:
                supervisor.complete(
                    handle.child_id,
                    operation_id=handle.operation_id,
                    output={"candidate": "original"},
                )
        assert raised.value.code == "child_event_store_unavailable"
        monkeypatch.setattr(
            log._runtime, "compare_and_swap_transactional_state", original_commit
        )
        history = log.read_events()
        with pytest.raises(ChildAgentSupervisorError) as raised:
            supervisor.spawn(_request(operation_id="replacement"))
        assert raised.value.code == "child_admission_closed"
        if event_type == "child_terminal":
            with pytest.raises(ChildAgentSupervisorError) as raised:
                supervisor.complete(
                    handle.child_id,
                    operation_id=handle.operation_id,
                    output={"candidate": "changed"},
                )
            assert raised.value.code == "child_event_store_recovery_required"
        assert log.read_events() == history
        supervisor.shutdown()
        log.release_owner()
        recovered = _owned(_log(tmp_path))
        try:
            assert recovered.supervisor.available_capacity == 1
            if event_type == "child_terminal":
                result = recovered.supervisor.wait(
                    handle.child_id, operation_id=handle.operation_id
                )
                assert result.receipt.status is ChildAgentState.SUCCEEDED
                assert result.result == {"candidate": "original"}
        finally:
            recovered.close()
    finally:
        supervisor.shutdown()


def test_durable_store_rejects_second_terminal_or_events_after_close(tmp_path):
    log = _log(tmp_path)
    runtime = _owned(log)
    try:
        supervisor = runtime.supervisor
        handle = supervisor.spawn(_request(lease_seconds=60))
        supervisor.complete(
            handle.child_id,
            operation_id=handle.operation_id,
            output={"candidate": "committed"},
        )
        committed_events = log.read_events()
        duplicate_terminal = deepcopy(committed_events[-1])
        duplicate_terminal["event_id"] = "second-terminal-event"
        with pytest.raises(ChildAgentSupervisorError) as raised:
            log.record(duplicate_terminal)
        assert raised.value.code == "child_lifecycle_conflict"
        assert log.read_events() == committed_events

        supervisor.close(handle.child_id, operation_id=handle.operation_id)
        closed_events = log.read_events()
        late_status = deepcopy(closed_events[0])
        late_status["event_type"] = "child_status"
        late_status["event_id"] = "post-close-status"
        with pytest.raises(ChildAgentSupervisorError) as raised:
            log.record(late_status)
        assert raised.value.code == "child_lifecycle_conflict"
        assert log.read_events() == closed_events
    finally:
        runtime.close()


def test_live_recovery_cannot_discard_running_future_or_capacity():
    release = Event()
    started = Event()

    def worker(handle):
        started.set()
        assert release.wait(5)
        return {"candidate": "ok"}

    supervisor = ChildAgentSupervisor(max_children=1)
    try:
        handle = supervisor.spawn(_request(lease_seconds=60), worker=worker)
        assert started.wait(2)
        with pytest.raises(ChildAgentSupervisorError) as raised:
            supervisor.recover()
        assert raised.value.code == "child_recovery_live_runtime"
        assert supervisor.available_capacity == 0
        release.set()
        assert (
            supervisor.wait(
                handle.child_id, operation_id=handle.operation_id, timeout_seconds=2
            ).receipt.status
            is ChildAgentState.SUCCEEDED
        )
    finally:
        release.set()
        supervisor.shutdown()


def test_malformed_spawn_blocks_entire_scope_even_with_spare_capacity():
    source = ChildAgentSupervisor()
    source.spawn(_request())
    events = deepcopy(source.events.events)
    events[0]["budget"] = {"turns": -1}
    restored = ChildAgentSupervisor(max_children=10)
    try:
        with pytest.raises(ChildAgentSupervisorError) as raised:
            restored.recover(events)
        assert raised.value.code == "child_recovery_corrupt"
        with pytest.raises(ChildAgentSupervisorError) as raised:
            restored.spawn(_request(operation_id="replacement"))
        assert raised.value.code == "child_admission_closed"
    finally:
        source.shutdown()
        restored.shutdown()


@pytest.mark.parametrize(
    "field,value",
    (
        ("task_id", "foreign-task"),
        ("budget", {"turns": 100}),
        ("allowed_tools", ["tool.write"]),
    ),
)
def test_recovery_does_not_accept_terminal_identity_or_policy_drift(field, value):
    original = ChildAgentSupervisor()
    handle = original.spawn(_request(), worker={"candidate": "ok"})
    events = deepcopy(original.events.events)
    events[-1][field] = value
    restored = ChildAgentSupervisor(max_children=1)
    try:
        recovered = restored.recover(events)
        assert recovered[0].state is ChildAgentState.LOST
        result = restored.wait(handle.child_id, operation_id=handle.operation_id)
        assert result.receipt is None
        assert result.result is None
        assert restored.available_capacity == 0
    finally:
        original.shutdown()
        restored.shutdown()


def test_uncertain_cancellation_retains_same_budget_online_and_after_recovery():
    class Uncertain:
        def run(self, handle):
            assert release.wait(5)
            return {"candidate": "late"}

        def cancel(self, handle):
            return False

    release = Event()
    first = ChildAgentSupervisor(max_children=2)
    restored = ChildAgentSupervisor(max_children=2)
    try:
        request = _request(budget={"turns": 2, "remaining_turns": 2})
        handle = first.spawn(request, worker=Uncertain())
        result = first.cancel(handle.child_id, operation_id=handle.operation_id)
        assert result.receipt.termination_confirmed is False
        restored.recover(deepcopy(first.events.events))
        assert first._budget_reservations == restored._budget_reservations
        assert first._budget_by_operation == restored._budget_by_operation
        assert request.operation_id in first._budget_by_operation
        for supervisor in (first, restored):
            with pytest.raises(ChildAgentSupervisorError) as raised:
                supervisor.spawn(
                    _request(
                        operation_id="replacement",
                        budget={"turns": 1, "remaining_turns": 2},
                    )
                )
            assert raised.value.code == "child_budget_exhausted"
    finally:
        release.set()
        first.shutdown()
        restored.shutdown()


def test_takeover_retains_occupancy_and_fences_every_old_owner_entry(tmp_path):
    now = datetime(2026, 9, 23, tzinfo=UTC)
    first_log = _log(tmp_path, clock=lambda: now, owner_lease_seconds=10)
    first = _owned(first_log, clock=lambda: now, owner_id="first")
    old_supervisor = first.supervisor
    handle = old_supervisor.spawn(_request(lease_seconds=60))
    now += timedelta(seconds=11)
    second_log = _log(tmp_path, clock=lambda: now, owner_lease_seconds=10)
    second = _owned(second_log, clock=lambda: now, owner_id="second")
    try:
        assert second.supervisor.available_capacity == 1
        assert second_log.owner_token.generation > first_log.owner_token.generation
        for operation in (
            lambda: old_supervisor.spawn(_request(operation_id="new")),
            lambda: old_supervisor.status(handle.child_id),
            lambda: old_supervisor.reclaim_stale(),
            lambda: old_supervisor.cancel(
                handle.child_id, operation_id=handle.operation_id
            ),
            lambda: first_log.renew_owner(),
            lambda: first_log.release_owner(),
        ):
            with pytest.raises(ChildAgentSupervisorError) as raised:
                operation()
            assert raised.value.code == "child_owner_lost"
    finally:
        first.close()
        second.close()


def test_owner_generation_survives_clean_release_and_old_token_is_rejected(tmp_path):
    first_log = _log(tmp_path)
    first = _owned(first_log, owner_id="same-owner-id")
    old_token = first_log.owner_token
    first.close()
    second_log = _log(tmp_path)
    second = _owned(second_log, owner_id="same-owner-id")
    try:
        assert second_log.owner_token.generation == old_token.generation + 1
        with pytest.raises(ChildAgentSupervisorError) as raised:
            second_log.renew_owner(old_token)
        assert raised.value.code == "child_owner_lost"
    finally:
        second.close()


def test_full_history_reserves_cancel_terminal_and_close(tmp_path):
    log = _log(tmp_path, max_events=5)
    runtime = _owned(log)
    try:
        supervisor = runtime.supervisor
        handle = supervisor.spawn(_request(lease_seconds=60))
        handle = supervisor.heartbeat(
            ChildAgentHeartbeat(
                handle.child_id, handle.lease.lease_id, 1, datetime.now(UTC)
            )
        )
        with pytest.raises(ChildAgentSupervisorError) as raised:
            supervisor.heartbeat(
                ChildAgentHeartbeat(
                    handle.child_id, handle.lease.lease_id, 2, datetime.now(UTC)
                )
            )
        assert raised.value.code == "child_event_store_capacity_exhausted"
        result = supervisor.cancel(handle.child_id, operation_id=handle.operation_id)
        assert result.receipt.termination_confirmed
        assert result.receipt.status is ChildAgentState.CANCELLED
        supervisor.close(handle.child_id, operation_id=handle.operation_id)
        assert [event["event_type"] for event in log.read_events()] == [
            "child_spawned",
            "child_heartbeat",
            "child_cancel_requested",
            "child_terminal",
            "child_closed",
        ]
    finally:
        runtime.close()


def test_oversized_result_commits_small_failed_receipt_and_closes(tmp_path):
    log = _log(tmp_path, max_event_bytes=8192, max_state_bytes=65536)
    runtime = _owned(log)
    try:
        supervisor = runtime.supervisor
        handle = supervisor.spawn(
            _request(lease_seconds=60), worker={"candidate": "x" * 16384}
        )
        result = supervisor.wait(handle.child_id, operation_id=handle.operation_id)
        assert result.receipt.status is ChildAgentState.FAILED
        assert result.receipt.reason_code == "child_result_too_large"
        assert result.receipt.termination_confirmed
        supervisor.close(handle.child_id, operation_id=handle.operation_id)
        assert all(len(str(event)) < 8192 for event in log.read_events())
    finally:
        runtime.close()


def test_unregistered_run_and_tenant_drift_reject_before_worker_invocation(tmp_path):
    log = _log(tmp_path)
    runtime = HarnessOwnedChildAgentRuntime(
        event_log=log, max_children=1, renewal_interval_seconds=None
    )
    runtime.start()
    try:
        with pytest.raises(ChildAgentSupervisorError) as raised:
            runtime.supervisor.spawn(
                _request(), worker=lambda _: pytest.fail("unregistered worker invoked")
            )
        assert raised.value.code == "child_run_scope_unregistered"
        runtime.register_run_scope("run-1", "tenant-a")
        with pytest.raises(ChildAgentSupervisorError) as raised:
            runtime.register_run_scope("run-1", "tenant-b")
        assert raised.value.code == "child_run_scope_conflict"
        assert log.read_events() == ()
    finally:
        runtime.close()


def test_automatic_owner_renewal_extends_lease_and_stops_on_close(
    tmp_path, monkeypatch
):
    now = datetime(2026, 9, 23, tzinfo=UTC)
    log = _log(tmp_path, clock=lambda: now, owner_lease_seconds=30)
    renewed = Event()
    original = log.renew_owner

    def observe_renewal():
        token = original()
        renewed.set()
        return token

    monkeypatch.setattr(log, "renew_owner", observe_renewal)
    runtime = HarnessOwnedChildAgentRuntime(
        event_log=log, max_children=1, renewal_interval_seconds=0.05
    )
    runtime.start()
    try:
        now += timedelta(seconds=20)
        assert renewed.wait(2)
        now += timedelta(seconds=20)
        rival = _log(tmp_path, clock=lambda: now, owner_lease_seconds=30)
        with pytest.raises(ChildAgentSupervisorError) as raised:
            rival.acquire_owner("rival")
        assert raised.value.code == "child_owner_conflict"
    finally:
        runtime.close()
    assert not runtime._renewer.is_alive()
    assert log.owner_token is None


def test_failed_renewal_closes_admission_before_any_new_worker(tmp_path, monkeypatch):
    log = _log(tmp_path)

    def fail_renewal():
        raise OSError("canonical backend unavailable")

    monkeypatch.setattr(log, "renew_owner", fail_renewal)
    runtime = HarnessOwnedChildAgentRuntime(
        event_log=log, max_children=1, renewal_interval_seconds=0.05
    )
    runtime.start()
    supervisor = runtime.supervisor
    runtime._renewer.join(timeout=2)
    try:
        assert not runtime._renewer.is_alive()
        with pytest.raises(ChildAgentSupervisorError) as raised:
            supervisor.spawn(
                _request(),
                worker=lambda _: pytest.fail("worker invoked after renewal loss"),
            )
        assert raised.value.code == "child_owner_lost"
        assert log.read_events() == ()
    finally:
        runtime.close()


def test_restart_between_cancel_and_terminal_does_not_spend_terminal_reservation_twice(
    tmp_path, monkeypatch
):
    log = _log(tmp_path, max_events=4)
    log.acquire_owner("first")
    log.register_run_scope("run-1", "tenant-a")
    supervisor = ChildAgentSupervisor(
        event_sink=log, event_reader=log, owner_guard=log.assert_owner
    )
    handle = supervisor.spawn(_request(lease_seconds=60))
    original = log.record

    def fail_terminal(event):
        if event["event_type"] == "child_terminal":
            raise ChildAgentSupervisorError(
                "injected commit failure", code="child_event_store_unavailable"
            )
        return original(event)

    monkeypatch.setattr(log, "record", fail_terminal)
    with pytest.raises(ChildAgentSupervisorError) as raised:
        supervisor.cancel(handle.child_id, operation_id=handle.operation_id)
    assert raised.value.code == "child_event_store_unavailable"
    with pytest.raises(ChildAgentSupervisorError) as raised:
        supervisor.status(handle.child_id)
    assert raised.value.code == "child_event_store_recovery_required"
    assert log.read_events()[-1]["state"] == ChildAgentState.CANCEL_REQUESTED.value
    supervisor.shutdown()
    log.release_owner()

    recovered_log = _log(tmp_path, max_events=4)
    recovered = _owned(recovered_log)
    try:
        result = recovered.supervisor.cancel(
            handle.child_id, operation_id=handle.operation_id
        )
        assert result.receipt.status is ChildAgentState.LOST
        assert result.receipt.termination_confirmed is False
        assert (
            sum(
                event["event_type"] == "child_cancel_requested"
                for event in recovered_log.read_events()
            )
            == 1
        )
        assert len(recovered_log.read_events()) == 3
    finally:
        recovered.close()
