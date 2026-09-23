from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import framework.tool.runtime.executor as executor_module
from framework.events import EventRuntime, EventSchemaCatalog
from framework.harness.subagents import (
    ChildAgentAdmissionError,
    ChildAgentSpawnRequest,
    ChildAgentState,
    ChildAgentSupervisorError,
    DurableChildAgentEventLog,
    HarnessOwnedChildAgentRuntime,
)
from framework.shared.attempts import AttemptSupervisor, current_attempt_context
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
    ToolStatus,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore


_STATE_KEY = "integration.execution-recovery.children"
_RUN_ID = "execution-recovery-run"
_TENANT_ID = "tenant-a"
_DRIVER = Path(__file__).with_name("execution_recovery_integration_driver.py")


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id=_RUN_ID,
        graph_id="execution-recovery",
        graph_version="1",
        graph_ref="execution-recovery@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="controller",
        node_instance_id="controller:1",
        activity_id="controller-activity",
        attempt=1,
    )


def _request(*, operation_id: str = "external-effect-operation") -> ChildAgentSpawnRequest:
    return ChildAgentSpawnRequest(
        parent_graph_identity=_identity(),
        stage_id="integration",
        task_id="execute-tool",
        task_instance_id="execute-tool:1",
        attempt=1,
        allowed_tools=("sample.publish", "sample.read"),
        allowed_memory_namespaces=("integration",),
        budget={"turns": 2},
        operation_id=operation_id,
        child_id=f"child-{operation_id}",
        lease_seconds=60,
    )


def _open_runtime(
    database: Path,
    *,
    owner_id: str,
    max_children: int = 1,
) -> tuple[SQLiteEventStore, DurableChildAgentEventLog, HarnessOwnedChildAgentRuntime]:
    store = SQLiteEventStore(database)
    event_log = DurableChildAgentEventLog(
        state_runtime=EventRuntime(
            store=store,
            schema_catalog=EventSchemaCatalog(),
            backend="sqlite",
        ),
        state_reader=store,
        state_key=_STATE_KEY,
    )
    runtime = HarnessOwnedChildAgentRuntime(
        event_log=event_log,
        max_children=max_children,
        owner_id=owner_id,
        renewal_interval_seconds=None,
    )
    runtime.start()
    runtime.register_run_scope(_RUN_ID, _TENANT_ID)
    return store, event_log, runtime


def _close_runtime(runtime: HarnessOwnedChildAgentRuntime, _store: SQLiteEventStore) -> None:
    runtime.close()


def _tool_policy() -> ToolPolicy:
    return ToolPolicy(
        require_explicit_allowlist=False,
        require_approval_for_side_effects=False,
        cancellation_grace_seconds=0.05,
    )


def test_controller_restart_reuses_terminal_tool_result_without_duplicate_external_effect(
    tmp_path: Path,
) -> None:
    database = tmp_path / "events.sqlite3"
    effect_log = tmp_path / "external-effects.jsonl"
    first_signal = tmp_path / "first.json"
    first_release = tmp_path / "release-first"
    first = subprocess.Popen(
        [
            sys.executable,
            str(_DRIVER),
            "commit",
            str(database),
            str(first_signal),
            str(first_release),
            str(effect_log),
        ],
        cwd=Path(__file__).resolve().parents[3],
    )
    deadline = time.monotonic() + 15
    while not first_signal.exists():
        if time.monotonic() >= deadline:
            first.kill()
            raise AssertionError("first controller did not commit terminal result")
        time.sleep(0.01)
    committed = json.loads(first_signal.read_text(encoding="utf-8"))
    first.terminate()
    first.wait(timeout=15)

    second_signal = tmp_path / "second.json"
    second_release = tmp_path / "release-second"
    second = subprocess.Popen(
        [
            sys.executable,
            str(_DRIVER),
            "recover",
            str(database),
            str(second_signal),
            str(second_release),
            str(effect_log),
        ],
        cwd=Path(__file__).resolve().parents[3],
    )
    try:
        deadline = time.monotonic() + 15
        while not second_signal.exists():
            if second.poll() is not None:
                raise AssertionError(f"recovery controller exited with {second.returncode}")
            if time.monotonic() >= deadline:
                raise AssertionError("recovery controller did not reuse terminal result")
            time.sleep(0.01)
        recovered = json.loads(second_signal.read_text(encoding="utf-8"))

        assert effect_log.read_text(encoding="utf-8").splitlines() == [
            '{"document_id": "paper-1"}'
        ]
        assert recovered == committed
        assert recovered["receipt"]["receipt_checksum"]
        assert recovered["receipt"]["result_checksum"]
    finally:
        second_release.touch()
        second.wait(timeout=15)


def test_uncertain_child_cancellation_survives_restart_and_blocks_replacement(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    class UncertainWorker:
        def run(self, _handle: object) -> dict[str, str]:
            release.wait(5)
            return {"candidate": "late"}

        def cancel(self, _handle: object) -> bool:
            return False

    database = tmp_path / "events.sqlite3"
    first_store, first_log, first_runtime = _open_runtime(
        database,
        owner_id="uncertain-controller",
    )
    try:
        handle = first_runtime.supervisor.spawn(
            _request(operation_id="uncertain-operation"),
            worker=UncertainWorker(),
        )
        cancelled = first_runtime.supervisor.cancel(
            handle.child_id,
            operation_id=handle.operation_id,
        )
        assert cancelled.receipt is not None
        assert cancelled.receipt.status is ChildAgentState.LOST
        assert cancelled.receipt.termination_confirmed is False
        assert first_runtime.supervisor.available_capacity == 0
        terminal_checksum = cancelled.receipt.receipt_checksum
        durable_events = first_log.for_child(handle.child_id)
    finally:
        release.set()
        _close_runtime(first_runtime, first_store)

    second_store, second_log, second_runtime = _open_runtime(
        database,
        owner_id="uncertain-controller-restarted",
    )
    replacement_started = False
    try:
        recovered = second_runtime.supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
        )
        assert recovered.receipt is not None
        assert recovered.receipt.status is ChildAgentState.LOST
        assert recovered.receipt.termination_confirmed is False
        assert recovered.receipt.receipt_checksum == terminal_checksum
        assert second_runtime.supervisor.available_capacity == 0

        def replacement(_handle: object) -> dict[str, str]:
            nonlocal replacement_started
            replacement_started = True
            return {"candidate": "unsafe-retry"}

        with pytest.raises(ChildAgentAdmissionError) as raised:
            second_runtime.supervisor.spawn(
                _request(operation_id="replacement-operation"),
                worker=replacement,
            )
        assert raised.value.code == "child_capacity_exhausted"
        assert replacement_started is False
        assert second_log.for_child(handle.child_id) == durable_events
    finally:
        _close_runtime(second_runtime, second_store)


def test_confirmed_read_only_timeout_retries_with_stable_logical_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(
        executor_module,
        "AttemptSupervisor",
        lambda **options: AttemptSupervisor(clock=lambda: now[0], **options),
    )
    observed_attempts: list[tuple[str, str, int]] = []

    def read(_arguments: dict[str, object]) -> dict[str, bool]:
        context = current_attempt_context()
        assert context is not None
        observed_attempts.append(
            (context.attempt_id, context.idempotency_key, context.local_attempt_no)
        )
        if len(observed_attempts) == 1:
            now[0] = 2.0
            return {"late": True}
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.read",
            side_effect=ToolSideEffect.READ_ONLY,
            timeout_seconds=1,
            max_attempts=2,
        ),
        read,
    )

    observation = ToolExecutor(registry, graph_identity=_identity()).execute(
        ToolCall(
            tool_name="sample.read",
            call_id="stable-read",
            graph_identity=_identity(),
        ),
        _tool_policy(),
    )

    assert observation.status is ToolStatus.SUCCEEDED
    assert observation.result.retry_count == 1
    assert observed_attempts[0][0] != observed_attempts[1][0]
    assert [attempt[1] for attempt in observed_attempts] == [
        "tool:stable-read",
        "tool:stable-read",
    ]
    assert [attempt[2] for attempt in observed_attempts] == [1, 2]


def test_recovered_lost_child_retains_capacity_and_terminal_receipt_checksum(
    tmp_path: Path,
) -> None:
    release = threading.Event()

    class UnconfirmedWorker:
        def run(self, _handle: object) -> dict[str, str]:
            release.wait(5)
            return {"candidate": "late"}

        def cancel(self, _handle: object) -> bool:
            return False

    database = tmp_path / "events.sqlite3"
    store, event_log, runtime = _open_runtime(
        database,
        owner_id="lost-child-controller",
    )
    try:
        handle = runtime.supervisor.spawn(
            _request(operation_id="lost-child-operation"),
            worker=UnconfirmedWorker(),
        )
        expired = runtime.supervisor.reclaim_stale(
            now=handle.lease.expires_at + timedelta(seconds=1)
        )
        assert expired[0].state is ChildAgentState.LOST
        lost = runtime.supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
        )
        assert lost.receipt is not None
        assert lost.receipt.termination_confirmed is False
        checksum = lost.receipt.receipt_checksum
    finally:
        release.set()
        _close_runtime(runtime, store)

    reopened_store, reopened_log, reopened_runtime = _open_runtime(
        database,
        owner_id="lost-child-controller-restarted",
    )
    try:
        recovered = reopened_runtime.supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
        )
        assert recovered.receipt is not None
        assert recovered.receipt.receipt_checksum == checksum
        assert recovered.receipt.termination_confirmed is False
        assert reopened_runtime.supervisor.available_capacity == 0
        terminal_event = next(
            event
            for event in reopened_log.for_child(handle.child_id)
            if event["event_type"] == "child_terminal"
        )
        assert terminal_event["terminal_receipt"]["receipt_checksum"] == checksum
        with pytest.raises(ChildAgentSupervisorError) as raised:
            reopened_runtime.supervisor.close(
                handle.child_id,
                operation_id=handle.operation_id,
            )
        assert raised.value.code == "termination_unconfirmed"
    finally:
        _close_runtime(reopened_runtime, reopened_store)
