from __future__ import annotations

import threading
import time

import pytest

import framework.tool.runtime.executor as executor_module
from framework.shared.attempts import (
    AttemptContext,
    AttemptSupervisor,
    bind_attempt_context,
    current_attempt_context,
)
from framework.tool import (
    MCPServerConfig,
    MCPToolAdapter,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
    ToolStatus,
    ToolTimeoutError,
)


@pytest.fixture
def attempt_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    now = [0.0]
    monkeypatch.setattr(
        executor_module,
        "AttemptSupervisor",
        lambda **options: AttemptSupervisor(clock=lambda: now[0], **options),
    )
    return now


def test_unconfirmed_tool_timeout_never_overlaps_retry_or_duplicates_late_effect(
    attempt_clock: list[float],
) -> None:
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    lock = threading.Lock()
    calls = 0
    active = 0
    max_active = 0
    effects: list[str] = []

    def execute(_arguments: dict[str, object]) -> dict[str, bool]:
        nonlocal calls, active, max_active
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        attempt_clock[0] = 2.0
        started.set()
        release.wait()
        effects.append("published")
        with lock:
            active -= 1
        finished.set()
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.unconfirmed",
            side_effect=ToolSideEffect.READ_ONLY,
            timeout_seconds=1.0,
            max_attempts=3,
        ),
        execute,
    )

    executor = ToolExecutor(registry)
    try:
        observation = executor.execute(
            ToolCall(tool_name="sample.unconfirmed", call_id="logical-call"),
            ToolPolicy(
                require_explicit_allowlist=False,
                require_approval_for_side_effects=False,
                cancellation_grace_seconds=0.005,
            ),
        )

        assert started.is_set()
        assert observation.status == ToolStatus.TIMEOUT
        assert observation.result.termination_confirmed is False
        assert observation.result.indeterminate is True
        assert observation.result.retry_count == 0
        assert calls == 1
        assert max_active == 1
        assert effects == []
    finally:
        release.set()
        assert finished.wait(5)
    assert effects == ["published"]
    assert calls == 1


def test_confirmed_read_only_timeout_retries_with_stable_logical_key(
    attempt_clock: list[float],
) -> None:
    contexts: list[tuple[str, str, int]] = []

    def execute(_arguments: dict[str, object]) -> dict[str, bool]:
        context = current_attempt_context()
        assert context is not None
        contexts.append(
            (context.attempt_id, context.idempotency_key, context.local_attempt_no)
        )
        if len(contexts) == 1:
            # Expire the real supervisor's deadline without racing OS scheduling.
            attempt_clock[0] = 2.0
            return {"late": True}
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.read",
            side_effect=ToolSideEffect.READ_ONLY,
            timeout_seconds=1.0,
            max_attempts=2,
        ),
        execute,
    )

    executor = ToolExecutor(registry)
    observation = executor.execute(
        ToolCall(tool_name="sample.read", call_id="stable-call"),
        ToolPolicy(
            require_explicit_allowlist=False,
            require_approval_for_side_effects=False,
            cancellation_grace_seconds=0.05,
        ),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output == {"ok": True}
    assert observation.result.retry_count == 1
    assert len(contexts) == 2
    assert contexts[0][0] != contexts[1][0]
    assert contexts[0][1] == contexts[1][1] == "tool:stable-call"
    assert [item[2] for item in contexts] == [1, 2]
    terminal_events = [
        event for event in executor.list_events()
        if event.event_type == "attempt_terminal"
    ]
    assert [event.payload["state"] for event in terminal_events] == [
        "TIMED_OUT", "SUCCEEDED",
    ]
    assert terminal_events[0].payload["termination_confirmed"] is True
    assert terminal_events[0].payload["indeterminate"] is False


def test_external_write_timeout_is_indeterminate_even_after_confirmed_exit() -> None:
    calls = 0
    effects: list[str] = []

    def execute(_arguments: dict[str, object]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        effects.append("accepted")
        context = current_attempt_context()
        assert context is not None
        assert context.cancel_event.wait(1)
        return {"accepted": True}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.publish",
            side_effect=ToolSideEffect.WRITES_EXTERNAL_STATE,
            timeout_seconds=0.01,
            max_attempts=3,
        ),
        execute,
    )

    executor = ToolExecutor(registry)
    observation = executor.execute(
        ToolCall(tool_name="sample.publish", call_id="publish-call"),
        ToolPolicy(
            require_explicit_allowlist=False,
            require_approval_for_side_effects=False,
            cancellation_grace_seconds=0.05,
        ),
    )

    assert observation.status == ToolStatus.TIMEOUT
    assert observation.result.termination_confirmed is True
    assert observation.result.indeterminate is True
    assert observation.result.error_envelope is not None
    assert observation.result.error_envelope["retryable"] is False
    assert calls == 1
    assert effects == ["accepted"]
    terminal = next(
        event for event in executor.list_events() if event.event_type == "attempt_terminal"
    )
    assert terminal.payload["state"] == "INDETERMINATE"


def test_external_write_ordinary_failure_is_indeterminate_and_not_retried() -> None:
    calls = 0
    effects: list[str] = []

    def execute(_arguments: dict[str, object]) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        effects.append("accepted")
        raise RuntimeError("remote acknowledgement lost")

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.publish_failure",
            side_effect=ToolSideEffect.WRITES_EXTERNAL_STATE,
            max_attempts=3,
        ),
        execute,
    )

    executor = ToolExecutor(registry)
    observation = executor.execute(
        ToolCall(tool_name="sample.publish_failure", call_id="publish-failure"),
        ToolPolicy(
            require_explicit_allowlist=False,
            require_approval_for_side_effects=False,
        ),
    )

    assert observation.status == ToolStatus.FAILED
    assert observation.result.indeterminate is True
    assert observation.result.retry_count == 0
    assert observation.result.error_envelope is not None
    assert observation.result.error_envelope["retryable"] is False
    assert calls == 1
    assert effects == ["accepted"]
    terminal = next(
        event for event in executor.list_events() if event.event_type == "attempt_terminal"
    )
    assert terminal.payload["state"] == "INDETERMINATE"


class _BlockingMCPClient:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.finished = threading.Event()

    def list_tools(self, _server: MCPServerConfig) -> list[dict[str, object]]:
        self.release.wait(1)
        self.finished.set()
        return []

    def call_tool(
        self,
        _server: MCPServerConfig,
        _remote_tool_name: str,
        _arguments: dict[str, object],
    ) -> object:
        return None


def test_mcp_timeout_does_not_report_future_cancellation_as_termination() -> None:
    client = _BlockingMCPClient()
    adapter = MCPToolAdapter(client)
    server = MCPServerConfig(
        server_id="blocking",
        name="Blocking",
        transport="in_memory",
        timeout_seconds=0.01,
    )

    with pytest.raises(ToolTimeoutError) as raised:
        adapter.list_tools(server)

    assert raised.value.termination_confirmed is False
    assert raised.value.indeterminate is True
    client.release.set()
    assert client.finished.wait(1)


def test_mcp_operation_key_is_derived_from_parent_logical_scope() -> None:
    keys: list[str] = []

    class _CapturingClient:
        def list_tools(self, _server: MCPServerConfig) -> list[dict[str, object]]:
            context = current_attempt_context()
            assert context is not None
            keys.append(context.idempotency_key)
            return []

        def call_tool(
            self,
            _server: MCPServerConfig,
            _remote_tool_name: str,
            _arguments: dict[str, object],
        ) -> object:
            return None

    adapter = MCPToolAdapter(_CapturingClient())
    server = MCPServerConfig(
        server_id="shared",
        name="Shared",
        transport="in_memory",
    )
    first_parent = AttemptContext.create(
        attempt_id="parent-1",
        idempotency_key="graph:step:one:tool:call-1",
    )
    second_parent = AttemptContext.create(
        attempt_id="parent-2",
        idempotency_key="graph:step:one:tool:call-2",
    )

    with bind_attempt_context(first_parent):
        adapter.list_tools(server)
        adapter.list_tools(server)
    with bind_attempt_context(second_parent):
        adapter.list_tools(server)

    assert keys[0] == keys[1]
    assert keys[0] != keys[2]
    assert keys[0].startswith(first_parent.idempotency_key)
    assert keys[2].startswith(second_parent.idempotency_key)


def test_tool_deadline_rejection_never_emits_tool_started() -> None:
    called = False

    def execute(_arguments: dict[str, object]) -> dict[str, bool]:
        nonlocal called
        called = True
        return {"ok": True}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.deadline_rejected",
            side_effect=ToolSideEffect.READ_ONLY,
            timeout_seconds=1.0,
            min_start_window_seconds=0.1,
        ),
        execute,
    )
    executor = ToolExecutor(registry)
    parent = AttemptContext.create(
        attempt_id="deadline-parent",
        idempotency_key="graph:deadline-parent",
        deadline=time.monotonic() + 0.02,
    )

    with bind_attempt_context(parent):
        observation = executor.execute(
            ToolCall(
                tool_name="sample.deadline_rejected",
                call_id="deadline-call",
            ),
            ToolPolicy(
                require_explicit_allowlist=False,
                require_approval_for_side_effects=False,
            ),
        )

    event_types = [event.event_type for event in executor.list_events()]
    assert observation.status is ToolStatus.FAILED
    assert called is False
    assert "attempt_admission_rejected" in event_types
    assert "attempt_started" not in event_types
    assert "tool_started" not in event_types
