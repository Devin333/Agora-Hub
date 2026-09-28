from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import threading
import time

import pytest

from framework.harness.subagents.supervisor import (
    ChildAgentHandle,
    ChildAgentHeartbeat,
    ChildAgentOperationConflict,
    ChildAgentSpawnRequest,
    ChildAgentState,
    ChildAgentSupervisor,
    ChildAgentSupervisorError,
    ChildAgentTerminalReceipt,
    _checksum,
)
from framework.harness.subagents.supervisor_store import (
    CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
    DurableChildAgentEventLog,
)
from framework.events import EventRuntime, EventSchemaCatalog
from framework.events.runtime.projection import RuntimeEventProjection
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.storage.events.sqlite import SQLiteEventStore


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-1",
        graph_id="graph",
        graph_version="1.0.0",
        graph_ref="graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="node",
        node_instance_id="node-1",
        activity_id="activity",
        attempt=1,
    )


def _request(**kwargs: object) -> ChildAgentSpawnRequest:
    values = {
        "parent_graph_identity": _identity(),
        "stage_id": "stage",
        "task_id": "task",
        "task_instance_id": "task-instance",
        "attempt": 1,
        "allowed_tools": ("tool.read",),
        "allowed_memory_namespaces": ("research",),
        "budget": {"turns": 2},
        "operation_id": "op-1",
        "lease_seconds": 1.0,
    }
    values.update(kwargs)
    return ChildAgentSpawnRequest(**values)


def test_spawn_wait_and_close_are_idempotent() -> None:
    supervisor = ChildAgentSupervisor(worker_factory=lambda _: {"candidate": "ok"})
    handle = supervisor.spawn(_request())
    result = supervisor.wait(handle.child_id, operation_id="op-1", timeout_seconds=1)
    assert result.handle.state is ChildAgentState.SUCCEEDED
    same = supervisor.spawn(_request())
    assert same.child_id == handle.child_id
    closed = supervisor.close(handle.child_id, operation_id="op-1")
    assert closed.handle.state is ChildAgentState.CLOSED
    assert supervisor.close(handle.child_id, operation_id="op-1") == closed


def test_child_handle_contract_roundtrips_with_checksum() -> None:
    supervisor = ChildAgentSupervisor()
    handle = supervisor.spawn(_request())
    contract = handle.handle_contract()

    restored = ChildAgentHandle.from_dict(contract["canonical_payload"])

    assert contract["schema_version"] == "newsroom.child-agent-handle/v1"
    assert contract["handle_checksum"].startswith("sha256:")
    assert restored == handle
    assert restored.handle_contract() == contract


def test_recovery_rejects_tampered_child_handle_contract() -> None:
    source = ChildAgentSupervisor()
    source.spawn(_request())
    events = [dict(event) for event in source.events.events]
    contract = dict(events[0]["handle_contract"])
    payload = dict(contract["canonical_payload"])
    payload["task_id"] = "tampered-task"
    contract["canonical_payload"] = payload
    events[0]["handle_contract"] = contract

    with pytest.raises(ChildAgentSupervisorError) as raised:
        ChildAgentSupervisor().recover(events)

    assert raised.value.code == "child_recovery_corrupt"


def test_recovery_rejects_unknown_lease_fields_even_with_valid_checksum() -> None:
    source = ChildAgentSupervisor()
    source.spawn(_request())
    events = [dict(event) for event in source.events.events]
    contract = dict(events[0]["handle_contract"])
    payload = dict(contract["canonical_payload"])
    payload["lease"] = {**payload["lease"], "unexpected": "value"}
    checksum = _checksum(payload)
    contract["canonical_payload"] = payload
    contract["handle_checksum"] = checksum
    events[0]["handle_contract"] = contract
    events[0]["handle_checksum"] = checksum

    with pytest.raises(ChildAgentSupervisorError) as raised:
        ChildAgentSupervisor().recover(events)

    assert raised.value.code == "child_recovery_corrupt"


def test_recovery_rejects_noncanonical_resigned_child_handle_payload() -> None:
    source = ChildAgentSupervisor()
    source.spawn(_request(allowed_tools=("tool.read", "tool.write")))
    events = [dict(event) for event in source.events.events]
    contract = dict(events[0]["handle_contract"])
    payload = dict(contract["canonical_payload"])
    payload["allowed_tools"] = list(reversed(payload["allowed_tools"]))
    checksum = _checksum(payload)
    contract["canonical_payload"] = payload
    contract["handle_checksum"] = checksum
    events[0]["handle_contract"] = contract
    events[0]["handle_checksum"] = checksum

    with pytest.raises(ChildAgentSupervisorError) as raised:
        ChildAgentSupervisor().recover(events)

    assert raised.value.code == "child_recovery_corrupt"


def test_spawn_and_recovery_validate_child_handle_contract() -> None:
    events: list[dict[str, object]] = []
    source = ChildAgentSupervisor(event_sink=events.append)
    handle = source.spawn(_request())
    spawn_event = events[0]
    assert spawn_event["handle_schema_version"] == "newsroom.child-agent-handle/v1"
    assert spawn_event["handle_checksum"] == spawn_event["handle_contract"]["handle_checksum"]
    assert spawn_event["handle_contract"]["canonical_payload"]["child_id"] == handle.child_id

    restored = ChildAgentSupervisor(events=type(source.events)(events)).recover()
    assert restored[0].child_id == handle.child_id
    assert restored[0].operation_id == handle.operation_id
    assert restored[0].parent_graph_identity == handle.parent_graph_identity
    assert restored[0].child_graph_identity == handle.child_graph_identity


def test_child_control_output_is_rejected() -> None:
    supervisor = ChildAgentSupervisor()
    handle = supervisor.spawn(_request())
    with pytest.raises(ChildAgentSupervisorError, match="control authority"):
        supervisor.complete(
            handle.child_id,
            operation_id="op-1",
            output={"candidate": {}, "routing": {"next": "publish"}},
        )
    assert any(item["event_type"] == "child_boundary_violation" for item in supervisor.events.events)


def test_heartbeat_extends_lease_and_stale_reclaim_is_harness_owned() -> None:
    supervisor = ChildAgentSupervisor(default_lease_seconds=1)
    handle = supervisor.spawn(_request(lease_seconds=1))
    now = datetime.now(UTC)
    renewed = supervisor.heartbeat(
        ChildAgentHeartbeat(
            child_id=handle.child_id,
            lease_id=handle.lease.lease_id,
            heartbeat_seq=1,
            observed_at=now,
        )
    )
    assert renewed.lease.heartbeat_seq == 1
    lost = supervisor.reclaim_stale(now=renewed.lease.expires_at + timedelta(seconds=1))
    assert lost[0].state is ChildAgentState.LOST


def test_heartbeat_cannot_use_future_or_expired_time_to_revive_child() -> None:
    supervisor = ChildAgentSupervisor(default_lease_seconds=1)
    handle = supervisor.spawn(_request(lease_seconds=1))
    with pytest.raises(Exception, match="future"):
        supervisor.heartbeat(
            ChildAgentHeartbeat(
                child_id=handle.child_id,
                lease_id=handle.lease.lease_id,
                heartbeat_seq=1,
                observed_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
    expired_at = handle.lease.expires_at + timedelta(seconds=1)
    supervisor.reclaim_stale(now=expired_at)
    lost = supervisor.heartbeat(
        ChildAgentHeartbeat(
            child_id=handle.child_id,
            lease_id=handle.lease.lease_id,
            heartbeat_seq=2,
            observed_at=expired_at,
        )
    )
    assert lost.state is ChildAgentState.LOST


def test_recovery_replays_latest_heartbeat_lease() -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(event_sink=events.append)
    handle = supervisor.spawn(_request(lease_seconds=30))
    renewed = supervisor.heartbeat(
        ChildAgentHeartbeat(
            child_id=handle.child_id,
            lease_id=handle.lease.lease_id,
            heartbeat_seq=1,
            observed_at=datetime.now(UTC),
        )
    )
    recovered = ChildAgentSupervisor(events=type(supervisor.events)(events)).recover()
    assert recovered[0].lease.heartbeat_seq == renewed.lease.heartbeat_seq
    assert recovered[0].lease.expires_at == renewed.lease.expires_at


def test_ambiguous_cancellation_is_lost_and_not_retried() -> None:
    started = threading.Event()
    release = threading.Event()

    class Worker:
        def run(self, _handle: object) -> dict[str, str]:
            started.set()
            release.wait(timeout=2)
            return {"candidate": "ok"}

        def cancel(self, _handle: object) -> bool:
            release.set()
            return False

    worker = Worker()
    supervisor = ChildAgentSupervisor(worker_factory=lambda _: worker, max_children=1)
    handle = supervisor.spawn(_request())
    assert started.wait(timeout=1)
    result = supervisor.cancel(handle.child_id, operation_id=handle.operation_id)
    assert result.receipt is not None
    assert result.receipt.status is ChildAgentState.LOST
    with pytest.raises(Exception, match="capacity"):
        supervisor.spawn(_request(operation_id="op-3"))


@pytest.mark.parametrize(
    "foreign_operation_state, requested_operation_id",
    [
        ("active", "op-b"),
        ("completed", "op-b"),
        ("active", "op-unknown"),
    ],
)
def test_cancel_rejects_unbound_operation_before_any_side_effect(
    foreign_operation_state: str,
    requested_operation_id: str,
) -> None:
    class RecordingWorker:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.cancel_calls: list[str] = []

        def run(self, _handle: object) -> dict[str, str]:
            self.started.set()
            self.release.wait(timeout=2)
            return {"candidate": "ok"}

        def cancel(self, handle: object) -> bool:
            self.cancel_calls.append(handle.child_id)
            self.release.set()
            return True

    worker_a = RecordingWorker()
    worker_b = RecordingWorker()
    supervisor = ChildAgentSupervisor(max_children=2)
    try:
        child_a = supervisor.spawn(
            _request(child_id="child-a", operation_id="op-a"),
            worker=worker_a,
        )
        child_b = supervisor.spawn(
            _request(child_id="child-b", operation_id="op-b"),
            worker=worker_b,
        )
        assert worker_a.started.wait(timeout=1)
        assert worker_b.started.wait(timeout=1)
        assert child_a.parent_graph_identity == child_b.parent_graph_identity

        if foreign_operation_state == "completed":
            supervisor.complete(
                child_b.child_id,
                operation_id=child_b.operation_id,
                output={"candidate": "completed"},
            )

        states_before = {
            child_a.child_id: supervisor.status(child_a.child_id).state,
            child_b.child_id: supervisor.status(child_b.child_id).state,
        }
        cancel_events_before = sum(
            event["event_type"] == "child_cancel_requested"
            for event in supervisor.events.events
        )

        with pytest.raises(ChildAgentOperationConflict) as raised:
            supervisor.cancel(
                child_a.child_id,
                operation_id=requested_operation_id,
            )

        assert raised.value.code == "operation_identity_conflict"
        assert {
            child_a.child_id: supervisor.status(child_a.child_id).state,
            child_b.child_id: supervisor.status(child_b.child_id).state,
        } == states_before
        assert worker_a.cancel_calls == []
        assert worker_b.cancel_calls == []
        assert sum(
            event["event_type"] == "child_cancel_requested"
            for event in supervisor.events.events
        ) == cancel_events_before
    finally:
        worker_a.release.set()
        worker_b.release.set()
        supervisor.shutdown()


def test_cancel_with_bound_operation_is_idempotent_and_does_not_affect_sibling() -> None:
    class RecordingWorker:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.cancel_calls: list[str] = []

        def run(self, _handle: object) -> dict[str, str]:
            self.started.set()
            self.release.wait(timeout=2)
            return {"candidate": "ok"}

        def cancel(self, handle: object) -> bool:
            self.cancel_calls.append(handle.child_id)
            self.release.set()
            return True

    worker_a = RecordingWorker()
    worker_b = RecordingWorker()
    supervisor = ChildAgentSupervisor(max_children=2)
    try:
        child_a = supervisor.spawn(
            _request(child_id="child-a", operation_id="op-a"),
            worker=worker_a,
        )
        child_b = supervisor.spawn(
            _request(child_id="child-b", operation_id="op-b"),
            worker=worker_b,
        )
        assert worker_a.started.wait(timeout=1)
        assert worker_b.started.wait(timeout=1)

        first = supervisor.cancel(child_a.child_id, operation_id=child_a.operation_id)
        second = supervisor.cancel(child_a.child_id, operation_id=child_a.operation_id)

        assert second == first
        assert first.handle.state is ChildAgentState.CANCELLED
        assert worker_a.cancel_calls == [child_a.child_id]
        assert worker_b.cancel_calls == []
        assert supervisor.status(child_b.child_id).state is ChildAgentState.RUNNING
    finally:
        worker_a.release.set()
        worker_b.release.set()
        supervisor.shutdown()


def test_complete_is_idempotent_and_runtime_events_are_projected() -> None:
    projection = RuntimeEventProjection()
    supervisor = ChildAgentSupervisor(runtime_event_sink=projection)
    handle = supervisor.spawn(_request())
    first = supervisor.complete(handle.child_id, operation_id=handle.operation_id, output={"candidate": "ok"})
    second = supervisor.complete(handle.child_id, operation_id=handle.operation_id, output={"candidate": "different"})
    assert second == first
    assert projection.status(run_id="run-1")


def test_recovery_reuses_terminal_receipt_and_result() -> None:
    events = []
    supervisor = ChildAgentSupervisor(event_sink=events.append)
    handle = supervisor.spawn(_request())
    committed = supervisor.complete(
        handle.child_id,
        operation_id=handle.operation_id,
        output={"candidate": "ok"},
        result_ref="result://one",
    )
    recovered = ChildAgentSupervisor(
        events=type(supervisor.events)(events),
        result_resolver=lambda ref: {"candidate": "ok"} if ref == "result://one" else None,
    ).recover()
    assert recovered[0].state is ChildAgentState.SUCCEEDED
    restored = ChildAgentSupervisor(
        events=type(supervisor.events)(events),
        result_resolver=lambda ref: {"candidate": "ok"} if ref == "result://one" else None,
    )
    restored.recover()
    result = restored.wait(handle.child_id, operation_id=handle.operation_id)
    assert result.receipt == committed.receipt
    assert result.result == {"candidate": "ok"}


def test_recovery_reuses_embedded_terminal_result_without_resolver() -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(event_sink=events.append)
    handle = supervisor.spawn(_request())
    committed = supervisor.complete(
        handle.child_id,
        operation_id=handle.operation_id,
        output={"candidate": "ok"},
    )

    restored = ChildAgentSupervisor(events=type(supervisor.events)(events))
    restored.recover()
    result = restored.wait(handle.child_id, operation_id=handle.operation_id)

    assert result.receipt == committed.receipt
    assert result.result == {"candidate": "ok"}


def test_recovery_rejects_tampered_embedded_terminal_result() -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(event_sink=events.append)
    handle = supervisor.spawn(_request())
    supervisor.complete(handle.child_id, operation_id=handle.operation_id, output={"candidate": "ok"})
    terminal = next(item for item in events if item["event_type"] == "child_terminal")
    metadata = dict(terminal["metadata"])
    metadata["result"] = {"candidate": "tampered"}
    terminal["metadata"] = metadata

    restored = ChildAgentSupervisor(events=type(supervisor.events)(events))
    recovered = restored.recover()

    assert recovered[0].state is ChildAgentState.LOST
    assert restored.status(handle.child_id).terminal_receipt_ref is None


def test_recovery_treats_corrupt_terminal_receipt_as_lost_and_occupies_capacity() -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(event_sink=events.append)
    handle = supervisor.spawn(_request())
    supervisor.complete(handle.child_id, operation_id=handle.operation_id, output={"candidate": "ok"})
    terminal = next(item for item in events if item["event_type"] == "child_terminal")
    receipt = dict(terminal["terminal_receipt"])
    receipt["reason_code"] = "tampered"
    terminal["terminal_receipt"] = receipt
    recovered = ChildAgentSupervisor(events=type(supervisor.events)(events), max_children=1)
    restored = recovered.recover()
    assert restored[0].state is ChildAgentState.LOST
    with pytest.raises(Exception, match="capacity"):
        recovered.spawn(_request(operation_id="replacement"))


def test_recovery_rejects_tampered_lifecycle_event_identity() -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(event_sink=events.append)
    handle = supervisor.spawn(_request())
    events[0]["event_id"] = "child-event-tampered"
    recovered = ChildAgentSupervisor(events=type(supervisor.events)(events))
    restored = recovered.recover()
    assert restored[0].state is ChildAgentState.LOST
    assert recovered.status(handle.child_id).terminal_receipt_ref is None


def test_worker_returning_no_output_is_failed() -> None:
    supervisor = ChildAgentSupervisor(worker_factory=lambda _: (lambda _handle: None))
    handle = supervisor.spawn(_request())
    result = supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    assert result.handle.state is ChildAgentState.FAILED
    assert result.receipt is not None
    assert result.receipt.reason_code == "worker_output_missing"


def test_worker_start_failure_is_durable_terminal_failure(monkeypatch) -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(
        event_sink=events.append,
        worker_factory=lambda _: (lambda _handle: {"candidate": "ok"}),
    )
    def fail_submit(*args, **kwargs):
        raise RuntimeError("executor submission failed")

    monkeypatch.setattr(supervisor._executor, "submit", fail_submit)
    with pytest.raises(RuntimeError):
        supervisor.spawn(_request())
    assert any(item["event_type"] == "child_terminal" for item in events)
    recovered = ChildAgentSupervisor(events=type(supervisor.events)(events)).recover()
    assert recovered[0].state is ChildAgentState.FAILED


def test_non_runnable_worker_is_failed_instead_of_staying_running() -> None:
    supervisor = ChildAgentSupervisor(worker_factory=lambda _: object())
    handle = supervisor.spawn(_request())
    assert handle.state is ChildAgentState.FAILED


def test_recovery_restores_consumed_budget_before_new_admission() -> None:
    events = []
    supervisor = ChildAgentSupervisor(event_sink=events.append, max_children=2)
    handle = supervisor.spawn(
        _request(
            operation_id="budget-op-1",
            child_id="budget-child-1",
            budget={"turns": 2, "remaining_turns": 3},
        )
    )
    supervisor.complete(handle.child_id, operation_id=handle.operation_id, output={"candidate": "ok"})
    recovered = ChildAgentSupervisor(events=type(supervisor.events)(events), max_children=2)
    recovered.recover()
    with pytest.raises(Exception, match="budget"):
        recovered.spawn(
            _request(
                operation_id="budget-op-2",
                child_id="budget-child-2",
                budget={"turns": 2, "remaining_turns": 3},
            )
        )


def test_budget_and_wildcard_capabilities_are_rejected() -> None:
    with pytest.raises(ValueError):
        _request(allowed_tools=("*",))
    with pytest.raises(ValueError):
        _request(budget={"remaining_turns": 0})


def test_parent_budget_reservation_is_atomic_across_children() -> None:
    supervisor = ChildAgentSupervisor(max_children=3)
    first = supervisor.spawn(
        _request(
            child_id="child-a",
            operation_id="op-a",
            budget={"turns": 2, "remaining_turns": 3},
        )
    )
    assert first.child_id == "child-a"
    with pytest.raises(Exception, match="budget"):
        supervisor.spawn(
            _request(
                child_id="child-b",
                operation_id="op-b",
                budget={"turns": 2, "remaining_turns": 3},
            )
        )


def test_success_receipt_requires_result_integrity_fields() -> None:
    supervisor = ChildAgentSupervisor()
    handle = supervisor.spawn(_request())
    with pytest.raises(ValueError):
        from framework.harness.subagents.supervisor import ChildAgentTerminalReceipt

        ChildAgentTerminalReceipt(
            child_id=handle.child_id,
            operation_id=handle.operation_id,
            parent_graph_identity=handle.parent_graph_identity,
            status=ChildAgentState.SUCCEEDED,
            reason_code="worker_completed",
            result_ref=None,
            result_checksum=None,
            termination_confirmed=True,
            completed_at=datetime.now(UTC),
        )


def test_confirmed_terminal_receipt_is_required_for_success() -> None:
    supervisor = ChildAgentSupervisor()
    handle = supervisor.spawn(_request())
    with pytest.raises(ValueError, match="confirmed termination"):
        from framework.harness.subagents.supervisor import ChildAgentTerminalReceipt

        ChildAgentTerminalReceipt(
            child_id=handle.child_id,
            operation_id=handle.operation_id,
            parent_graph_identity=handle.parent_graph_identity,
            status=ChildAgentState.SUCCEEDED,
            reason_code="worker_completed",
            result_ref="result://one",
            result_checksum="sha256:" + "a" * 64,
            termination_confirmed=False,
            completed_at=datetime.now(UTC),
        )


def test_event_sink_failure_does_not_admit_child() -> None:
    def fail(_event: object) -> None:
        raise RuntimeError("durable sink unavailable")

    supervisor = ChildAgentSupervisor(event_sink=fail)
    with pytest.raises(RuntimeError, match="durable sink unavailable"):
        supervisor.spawn(_request())
    with pytest.raises(ChildAgentSupervisorError) as raised:
        supervisor.status("child-does-not-exist")
    assert raised.value.code == "child_event_store_recovery_required"


def _sqlite_lifecycle(tmp_path):
    database = tmp_path / "child-lifecycle.sqlite3"
    SQLiteEventStore(database)
    return database


def test_sqlite_restart_reuses_committed_result_without_worker_reinvocation(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    calls = 0

    def worker(_handle: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"candidate": "committed"}

    first_store = SQLiteEventStore(database, initialize=False)
    first_runtime = EventRuntime(
        store=first_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    first_log = DurableChildAgentEventLog(
        state_runtime=first_runtime,
        state_reader=first_store,
        state_key="run-1",
    )
    first_log.acquire_owner("first")
    first_log.register_run_scope("run-1", "tenant-1")
    first = ChildAgentSupervisor(
        event_sink=first_log,
        event_reader=first_log,
        worker_factory=worker,
    )
    handle = first.spawn(_request())
    committed = first.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    first.shutdown()
    first_log.release_owner()

    reopened_store = SQLiteEventStore(database, initialize=False)
    reopened_runtime = EventRuntime(
        store=reopened_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    reopened_log = DurableChildAgentEventLog(
        state_runtime=reopened_runtime,
        state_reader=reopened_store,
        state_key="run-1",
    )
    restored_calls = 0

    def should_not_run(_handle: object) -> dict[str, str]:
        nonlocal restored_calls
        restored_calls += 1
        return {"candidate": "duplicate"}

    reopened_log.acquire_owner("restored")
    restored = ChildAgentSupervisor(
        event_sink=reopened_log,
        event_reader=reopened_log,
        worker_factory=should_not_run,
    )
    recovered = restored.recover()
    result = restored.wait(
        recovered[0].child_id,
        operation_id=recovered[0].operation_id,
    )

    assert database.exists()
    assert calls == 1
    assert restored_calls == 0
    assert committed.receipt is not None
    assert result.receipt == committed.receipt
    assert result.result == {"candidate": "committed"}
    restored.shutdown()


def test_durable_child_log_rejects_rechecksummed_receipt_for_another_operation(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("owner")
    log.register_run_scope("run-1", "tenant-1")
    supervisor = ChildAgentSupervisor(
        event_sink=log,
        event_reader=log,
        worker_factory=lambda _: {"candidate": "verified"},
    )
    handle = supervisor.spawn(_request())
    supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    events_before = log.read_events()
    snapshot_before = store.load_transactional_state(CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE, "run-1")
    terminal = next(event for event in events_before if event["event_type"] == "child_terminal")
    receipt = ChildAgentTerminalReceipt.from_dict(terminal["terminal_receipt"])
    forged_receipt = replace(receipt, operation_id="another-operation").to_dict()
    forged = dict(terminal)
    forged["event_id"] = "forged-terminal-receipt"
    forged["terminal_receipt"] = forged_receipt
    forged["metadata"] = {**terminal["metadata"], "terminal_receipt": forged_receipt}

    with pytest.raises(ValueError, match="receipt identity conflicts"):
        log.record(forged)

    assert log.read_events() == events_before
    assert store.load_transactional_state(CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE, "run-1") == snapshot_before
    supervisor.shutdown()


def test_durable_child_log_rejects_rechecksummed_terminal_parent_drift_before_cas(
    tmp_path,
) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("owner")
    log.register_run_scope("run-1", "tenant-1")
    fixed_now = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
    worker_calls: list[str] = []
    accepted = ChildAgentSupervisor(
        event_sink=log,
        event_reader=log,
        clock=lambda: fixed_now,
        max_children=1,
        worker_factory=lambda _: worker_calls.append("accepted"),
    )
    forged_events: list[dict[str, object]] = []
    forger = ChildAgentSupervisor(
        event_sink=forged_events.append,
        clock=lambda: fixed_now,
        max_children=1,
        worker_factory=lambda _: worker_calls.append("forger"),
    )
    try:
        request = _request(
            child_id="bound-child",
            operation_id="bound-operation",
            lease_seconds=60,
        )
        accepted_handle = accepted.spawn(request, worker=None)
        events_before = log.read_events()
        snapshot_before = store.load_transactional_state(
            CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
            "run-1",
        )
        capacity_before = accepted.available_capacity

        foreign_parent = replace(
            accepted_handle.parent_graph_identity,
            node_id="foreign-node",
            node_instance_id="foreign-node-1",
            activity_id="foreign-activity",
        )
        foreign_handle = forger.spawn(
            _request(
                child_id=accepted_handle.child_id,
                operation_id=accepted_handle.operation_id,
                parent_graph_identity=foreign_parent,
                child_graph_identity=accepted_handle.child_graph_identity,
                lease_seconds=60,
            ),
            worker=None,
        )
        forger.complete(
            foreign_handle.child_id,
            operation_id=foreign_handle.operation_id,
            output={"candidate": "forged"},
        )
        forged_terminal = next(
            event
            for event in forged_events
            if event["event_type"] == "child_terminal"
        )

        with pytest.raises(ChildAgentOperationConflict) as exc_info:
            log.record(forged_terminal)

        assert exc_info.value.code == "child_lifecycle_conflict"
        assert log.read_events() == events_before
        assert store.load_transactional_state(
            CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
            "run-1",
        ) == snapshot_before
        assert accepted.available_capacity == capacity_before
        assert worker_calls == []
    finally:
        accepted.shutdown()
        forger.shutdown()


def test_durable_child_log_rejects_cancelled_terminal_without_cancel_request(
    tmp_path,
) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("owner")
    log.register_run_scope("run-1", "tenant-1")
    fixed_now = datetime(2026, 9, 29, 9, 15, tzinfo=UTC)
    accepted = ChildAgentSupervisor(
        event_sink=log,
        event_reader=log,
        clock=lambda: fixed_now,
        max_children=1,
    )
    source_events: list[dict[str, object]] = []
    source = ChildAgentSupervisor(
        event_sink=source_events.append,
        clock=lambda: fixed_now,
        max_children=1,
    )
    try:
        request = _request(
            child_id="cancel-child",
            operation_id="cancel-operation",
            lease_seconds=60,
        )
        accepted.spawn(request, worker=None)
        source_handle = source.spawn(request, worker=None)
        cancelled = source.cancel(
            source_handle.child_id,
            operation_id=source_handle.operation_id,
        )
        assert cancelled.receipt is not None
        assert cancelled.receipt.status is ChildAgentState.CANCELLED
        terminal = next(
            event
            for event in source_events
            if event["event_type"] == "child_terminal"
        )
        events_before = log.read_events()
        snapshot_before = store.load_transactional_state(
            CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
            "run-1",
        )
        capacity_before = accepted.available_capacity

        with pytest.raises(ChildAgentOperationConflict) as exc_info:
            log.record(terminal)

        assert exc_info.value.code == "child_lifecycle_conflict"
        assert log.read_events() == events_before
        assert store.load_transactional_state(
            CHILD_AGENT_LIFECYCLE_STATE_NAMESPACE,
            "run-1",
        ) == snapshot_before
        assert accepted.available_capacity == capacity_before
    finally:
        accepted.shutdown()
        source.shutdown()


def test_durable_child_log_reopens_markerless_v2_history(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    source_events: list[dict[str, object]] = []
    fixed_now = datetime(2026, 9, 29, 9, 30, tzinfo=UTC)
    source = ChildAgentSupervisor(
        event_sink=source_events.append,
        clock=lambda: fixed_now,
    )
    try:
        handle = source.spawn(
            _request(
                child_id="legacy-child",
                operation_id="legacy-operation",
                lease_seconds=60,
            ),
            worker=None,
        )
        expected = source.complete(
            handle.child_id,
            operation_id=handle.operation_id,
            output={"candidate": "legacy"},
        )
    finally:
        source.shutdown()
    markerless_events = [
        {
            key: value
            for key, value in event.items()
            if key not in {"handle_contract", "handle_schema_version", "handle_checksum"}
        }
        for event in source_events
    ]

    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("first")
    log.register_run_scope("run-1", "tenant-1")
    for event in markerless_events:
        log.record(event)
    log.release_owner()

    reopened_store = SQLiteEventStore(database, initialize=False)
    reopened_runtime = EventRuntime(
        store=reopened_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    reopened_log = DurableChildAgentEventLog(
        state_runtime=reopened_runtime,
        state_reader=reopened_store,
        state_key="run-1",
    )
    reopened_log.acquire_owner("restored")
    restored = ChildAgentSupervisor(
        event_sink=reopened_log,
        event_reader=reopened_log,
        clock=lambda: fixed_now,
    )
    try:
        recovered = restored.recover()
        result = restored.wait(
            recovered[0].child_id,
            operation_id=recovered[0].operation_id,
        )

        assert len(reopened_log.read_events()) == len(markerless_events)
        assert result.receipt == expected.receipt
        assert result.result == {"candidate": "legacy"}
    finally:
        restored.shutdown()


def test_sqlite_restart_preserves_cancellation_uncertainty_and_blocks_replacement(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("first")
    log.register_run_scope("run-1", "tenant-1")
    started = threading.Event()
    release = threading.Event()

    class UncertainWorker:
        def run(self, _handle: object) -> dict[str, str]:
            started.set()
            release.wait(timeout=2)
            return {"candidate": "late"}

        def cancel(self, _handle: object) -> bool:
            return False

    first = ChildAgentSupervisor(
        event_sink=log,
        event_reader=log,
        worker_factory=lambda _: UncertainWorker(),
        max_children=1,
        cancel_timeout_seconds=0.1,
    )
    handle = first.spawn(_request())
    assert started.wait(timeout=1)
    cancelled = first.cancel(handle.child_id, operation_id=handle.operation_id)
    first.shutdown(wait=False)
    log.release_owner()

    reopened_store = SQLiteEventStore(database, initialize=False)
    reopened_runtime = EventRuntime(
        store=reopened_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    reopened_log = DurableChildAgentEventLog(
        state_runtime=reopened_runtime,
        state_reader=reopened_store,
        state_key="run-1",
    )
    reopened_log.acquire_owner("restored")
    restored = ChildAgentSupervisor(
        event_sink=reopened_log,
        event_reader=reopened_log,
        max_children=1,
    )
    recovered = restored.recover()

    assert cancelled.receipt is not None
    assert cancelled.receipt.status is ChildAgentState.LOST
    assert cancelled.receipt.termination_confirmed is False
    assert recovered[0].state is ChildAgentState.LOST
    with pytest.raises(Exception, match="capacity"):
        restored.spawn(_request(operation_id="replacement"))
    with pytest.raises(Exception, match="termination"):
        restored.close(handle.child_id, operation_id=handle.operation_id)
    release.set()
    restored.shutdown(wait=False)


def test_sqlite_replay_of_spawn_intent_does_not_invoke_external_worker_again(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("first")
    log.register_run_scope("run-1", "tenant-1")
    side_effects: list[str] = []

    class ExternalWorker:
        def run(self, handle: object) -> dict[str, str]:
            side_effects.append("external-write")
            return {"candidate": "ok"}

    first = ChildAgentSupervisor(
        event_sink=log,
        event_reader=log,
        worker_factory=lambda _: ExternalWorker(),
    )
    handle = first.spawn(_request())
    first.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    first.shutdown()
    log.release_owner()

    reopened_store = SQLiteEventStore(database, initialize=False)
    reopened_runtime = EventRuntime(
        store=reopened_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    reopened_log = DurableChildAgentEventLog(
        state_runtime=reopened_runtime,
        state_reader=reopened_store,
        state_key="run-1",
    )
    reopened_log.acquire_owner("restored")
    restored = ChildAgentSupervisor(
        event_sink=reopened_log,
        event_reader=reopened_log,
        worker_factory=lambda _: (_ for _ in ()).throw(AssertionError("duplicate worker invocation")),
    )
    restored.recover()
    replayed = restored.spawn(_request())

    assert replayed.child_id == handle.child_id
    assert side_effects == ["external-write"]
    restored.shutdown()


def test_sqlite_replay_rejects_operation_identity_with_changed_admission(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
    log.acquire_owner("first")
    log.register_run_scope("run-1", "tenant-1")
    first = ChildAgentSupervisor(event_sink=log, event_reader=log, worker_factory=lambda _: {"candidate": "ok"})
    handle = first.spawn(_request())
    first.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    first.shutdown()
    log.release_owner()

    reopened_store = SQLiteEventStore(database, initialize=False)
    reopened_runtime = EventRuntime(
        store=reopened_store,
        schema_catalog=EventSchemaCatalog(),
        backend="sqlite",
    )
    reopened_log = DurableChildAgentEventLog(
        state_runtime=reopened_runtime,
        state_reader=reopened_store,
        state_key="run-1",
    )
    reopened_log.acquire_owner("restored")
    restored = ChildAgentSupervisor(
        event_sink=reopened_log,
        event_reader=reopened_log,
        worker_factory=lambda _: (_ for _ in ()).throw(AssertionError("duplicate worker invocation")),
    )
    restored.recover()

    with pytest.raises(Exception, match="identity"):
        restored.spawn(_request(operation_id=handle.operation_id, task_id="changed-task"))
    restored.shutdown()
