from __future__ import annotations

from datetime import UTC, datetime, timedelta
import threading
import time

import pytest

from framework.harness.subagents.supervisor import (
    ChildAgentHeartbeat,
    ChildAgentSpawnRequest,
    ChildAgentState,
    ChildAgentSupervisor,
    ChildAgentSupervisorError,
)
from framework.harness.subagents.supervisor_store import DurableChildAgentEventLog
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


def test_worker_start_failure_is_durable_terminal_failure() -> None:
    events: list[dict[str, object]] = []
    supervisor = ChildAgentSupervisor(
        event_sink=events.append,
        worker_factory=lambda _: (lambda _handle: {"candidate": "ok"}),
    )
    supervisor.shutdown()
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
    with pytest.raises(Exception, match="not found"):
        supervisor.status("child-does-not-exist")


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
    first = ChildAgentSupervisor(
        event_sink=first_log,
        event_reader=first_log,
        worker_factory=worker,
    )
    handle = first.spawn(_request())
    committed = first.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    first.shutdown()

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


def test_sqlite_restart_preserves_cancellation_uncertainty_and_blocks_replacement(tmp_path) -> None:
    database = _sqlite_lifecycle(tmp_path)
    store = SQLiteEventStore(database, initialize=False)
    runtime = EventRuntime(store=store, schema_catalog=EventSchemaCatalog(), backend="sqlite")
    log = DurableChildAgentEventLog(state_runtime=runtime, state_reader=store, state_key="run-1")
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
    first = ChildAgentSupervisor(event_sink=log, event_reader=log, worker_factory=lambda _: {"candidate": "ok"})
    handle = first.spawn(_request())
    first.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
    first.shutdown()

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
    restored = ChildAgentSupervisor(
        event_sink=reopened_log,
        event_reader=reopened_log,
        worker_factory=lambda _: (_ for _ in ()).throw(AssertionError("duplicate worker invocation")),
    )
    restored.recover()

    with pytest.raises(Exception, match="identity"):
        restored.spawn(_request(operation_id=handle.operation_id, task_id="changed-task"))
    restored.shutdown()
