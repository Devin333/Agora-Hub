from __future__ import annotations

from threading import Barrier
from typing import Any

import pytest

from framework.tool import (
    ToolBatchExecutor,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolStatus,
)
from framework.tool.models import ToolSideEffect
from framework.tool.runtime.evidence import ToolEvidencePersistenceError


def _read_tool(name: str, handler) -> tuple[ToolDefinition, Any]:
    return (
        ToolDefinition(
            name=name,
            input_schema={"properties": {"value": {}}},
            side_effect=ToolSideEffect.READ_ONLY,
            concurrency_safe=True,
        ),
        handler,
    )


def _registry(*tools: tuple[ToolDefinition, Any]) -> ToolRegistry:
    registry = ToolRegistry()
    for definition, handler in tools:
        registry.register(definition, handler)
    return registry


def _policy(*names: str, **overrides) -> ToolPolicy:
    values = {
        "allowed_tools": list(names),
        "require_explicit_allowlist": True,
        "require_approval_for_side_effects": False,
        "max_tool_calls_per_iteration": 8,
    }
    values.update(overrides)
    return ToolPolicy(**values)


class _EscapingExecutor(ToolExecutor):
    """Executor whose boundary itself raises for one call (e.g. a broken sink)."""

    def __init__(self, registry: ToolRegistry, *, escape_call_id: str, error: Exception) -> None:
        super().__init__(registry)
        self._escape_call_id = escape_call_id
        self._error = error

    def execute(self, call, policy=None):
        if call.call_id == self._escape_call_id:
            raise self._error
        return super().execute(call, policy)


def test_parallel_tool_failure_keeps_every_sibling_observation() -> None:
    barrier = Barrier(3, timeout=5)

    def ok(arguments):
        barrier.wait()
        return {"value": arguments["value"]}

    def broken(_arguments):
        barrier.wait()
        raise RuntimeError("upstream search unavailable")

    registry = _registry(_read_tool("sample.ok", ok), _read_tool("sample.broken", broken))
    calls = [
        ToolCall(tool_name="sample.ok", call_id="ok-1", arguments={"value": 1}),
        ToolCall(tool_name="sample.broken", call_id="broken", arguments={"value": 2}),
        ToolCall(tool_name="sample.ok", call_id="ok-2", arguments={"value": 3}),
    ]

    observations = ToolBatchExecutor.from_registry(registry, max_workers=3).execute_batch(
        calls,
        _policy("sample.ok", "sample.broken"),
    )

    assert [item.call.call_id for item in observations] == ["ok-1", "broken", "ok-2"]
    assert [item.status for item in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
        ToolStatus.SUCCEEDED,
    ]
    assert observations[1].result.error_message == "upstream search unavailable"
    assert [item.result.output for item in (observations[0], observations[2])] == [
        {"value": 1},
        {"value": 3},
    ]


def test_exception_escaping_one_parallel_call_is_isolated_as_indeterminate() -> None:
    registry = _registry(_read_tool("sample.ok", lambda arguments: {"value": arguments["value"]}))
    executor = _EscapingExecutor(
        registry,
        escape_call_id="escaped",
        error=RuntimeError("event sink closed"),
    )
    calls = [
        ToolCall(tool_name="sample.ok", call_id="before", arguments={"value": 1}),
        ToolCall(tool_name="sample.ok", call_id="escaped", arguments={"value": 2}),
        ToolCall(tool_name="sample.ok", call_id="after", arguments={"value": 3}),
    ]

    observations = ToolBatchExecutor(executor, max_workers=3).execute_batch(
        calls,
        _policy("sample.ok"),
    )

    assert [item.status for item in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
        ToolStatus.SUCCEEDED,
    ]
    escaped = observations[1]
    assert escaped.result.error_type == "RuntimeError"
    assert escaped.result.error_message == "event sink closed"
    assert escaped.result.indeterminate is True
    assert escaped.result.metadata["execution_disposition"] == "indeterminate"


def test_evidence_failure_settles_in_flight_calls_before_failing_closed() -> None:
    finished: list[str] = []
    registry = _registry(
        _read_tool(
            "sample.ok",
            lambda arguments: finished.append(str(arguments["value"])) or {"ok": True},
        )
    )
    executor = _EscapingExecutor(
        registry,
        escape_call_id="evidence",
        error=ToolEvidencePersistenceError("required observation evidence write failed"),
    )
    calls = [
        ToolCall(tool_name="sample.ok", call_id="first", arguments={"value": "first"}),
        ToolCall(tool_name="sample.ok", call_id="evidence", arguments={"value": "evidence"}),
        ToolCall(tool_name="sample.ok", call_id="last", arguments={"value": "last"}),
    ]

    with pytest.raises(ToolEvidencePersistenceError, match="evidence write failed"):
        ToolBatchExecutor(executor, max_workers=3).execute_batch(calls, _policy("sample.ok"))

    assert sorted(finished) == ["first", "last"]
    assert sorted(record.tool_call.call_id for record in executor.list_records()) == [
        "first",
        "last",
    ]


def test_serial_batch_pauses_at_approval_without_invoking_later_requests() -> None:
    invoked: list[str] = []
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="sample.publish", side_effect=ToolSideEffect.WRITES_EXTERNAL_STATE),
        lambda _arguments: invoked.append("publish") or {"ok": True},
    )
    registry.register(*_read_tool("sample.ok", lambda _a: invoked.append("ok") or {"ok": True}))
    calls = [
        ToolCall(tool_name="sample.ok", call_id="read-first"),
        ToolCall(tool_name="sample.publish", call_id="publish"),
        ToolCall(tool_name="sample.ok", call_id="read-after"),
    ]

    observations = ToolBatchExecutor.from_registry(registry, max_workers=1).execute_batch(
        calls,
        _policy(
            "sample.ok",
            "sample.publish",
            require_approval_for=["sample.publish"],
        ),
    )

    assert invoked == ["ok"]
    assert [item.status for item in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.APPROVAL_REQUIRED,
        ToolStatus.BLOCKED,
    ]
    skipped = observations[2]
    assert skipped.result.error_type == "ToolBatchNotInvoked"
    assert skipped.result.metadata["execution_disposition"] == "not_invoked"


def test_approval_gated_calls_never_take_the_parallel_lane() -> None:
    registry = _registry(_read_tool("sample.ok", lambda _arguments: {"ok": True}))
    batch = ToolBatchExecutor.from_registry(registry, max_workers=4)
    policy = _policy("sample.ok", require_approval_for=["sample.ok"])

    safe, unsafe = batch._split_by_parallelism(
        [ToolCall(tool_name="sample.ok", call_id="gated")],
        policy,
    )

    assert safe == []
    assert [call.call_id for call in unsafe] == ["gated"]


def test_parallel_batch_counts_every_call_on_the_shared_executor() -> None:
    registry = _registry(_read_tool("sample.ok", lambda arguments: {"value": arguments["value"]}))
    batch = ToolBatchExecutor.from_registry(registry, max_workers=8)
    calls = [
        ToolCall(tool_name="sample.ok", call_id=f"call-{index}", arguments={"value": index})
        for index in range(8)
    ]

    observations = batch.execute_batch(calls, _policy("sample.ok"))

    assert [item.status for item in observations] == [ToolStatus.SUCCEEDED] * 8
    assert batch.executor.metrics.total_calls == 8
    assert batch.executor.metrics.succeeded_calls == 8
    assert len(batch.executor.list_records()) == 8


def test_batch_rejects_duplicate_call_ids() -> None:
    registry = _registry(_read_tool("sample.ok", lambda _arguments: {"ok": True}))

    with pytest.raises(ValueError, match="call_ids must be unique"):
        ToolBatchExecutor.from_registry(registry).execute_batch(
            [
                ToolCall(tool_name="sample.ok", call_id="same"),
                ToolCall(tool_name="sample.ok", call_id="same"),
            ],
            _policy("sample.ok"),
        )


def test_failed_compensation_is_logged_and_does_not_stop_the_rollback(caplog) -> None:
    rolled_back: list[str] = []

    def failing_rollback(_observation):
        raise RuntimeError("rollback endpoint down")

    registry = ToolRegistry()
    registry.register(ToolDefinition(name="sample.first"), lambda _arguments: {"ok": True})
    registry.register(ToolDefinition(name="sample.second"), lambda _arguments: {"ok": True})
    registry.register(
        ToolDefinition(name="sample.fail"),
        lambda _arguments: (_ for _ in ()).throw(RuntimeError("write rejected")),
    )
    batch = ToolBatchExecutor.from_registry(
        registry,
        compensating_actions={
            "sample.first": lambda observation: rolled_back.append(observation.call.call_id),
            "sample.second": failing_rollback,
        },
    )

    with caplog.at_level("ERROR", logger="framework.tool.runtime.batch_executor"):
        observations = batch.execute_batch(
            [
                ToolCall(tool_name="sample.first", call_id="first"),
                ToolCall(tool_name="sample.second", call_id="second"),
                ToolCall(tool_name="sample.fail", call_id="fail"),
            ],
            _policy("sample.first", "sample.second", "sample.fail"),
            mode="strict",
        )

    assert [item.status for item in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
    ]
    assert rolled_back == ["first"]
    assert any("compensation failed" in record.message for record in caplog.records)
