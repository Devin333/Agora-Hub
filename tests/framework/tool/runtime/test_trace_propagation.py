from __future__ import annotations

from threading import Lock
from typing import Any, Mapping

from framework.events import (
    TraceContext,
    W3CSpanContext,
    W3CTracePropagator,
    current_trace_context,
    is_valid_span_id,
    trace_context_scope,
)
from framework.shared import GraphExecutionIdentity
from framework.tool import (
    MCPServerConfig,
    MCPToolAdapter,
    ToolBatchExecutor,
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolStatus,
    TraceAwareHTTPToolTransport,
)


def test_tool_mcp_outbound_overwrites_untrusted_carrier_with_child_context() -> None:
    trace = TraceContext.root(
        execution_identity=GraphExecutionIdentity(
            run_id="run-1",
            graph_id="test.graph",
            graph_version="1",
            graph_ref="test.graph@1",
            graph_checksum="sha256:" + "a" * 64,
            node_id="analyze",
            node_instance_id="analyze:1",
            activity_id="activity-1",
            attempt=1,
        ),
        trace_id="1" * 32,
        span_id="2" * 16,
    )
    client = _MCPClient()
    adapter = MCPToolAdapter(client, trace_context=trace)
    registry = ToolRegistry()
    server = MCPServerConfig(
        server_id="remote",
        name="Remote",
        transport="in_memory",
        trace_carrier={
            "traceparent": f"00-{'3' * 32}-{'4' * 16}-01",
            "baggage": "run_id=attacker-selected",
        },
    )

    adapter.register_tools(registry, server)
    observation = ToolExecutor(registry, trace_context=trace).execute(
        ToolCall(tool_name="mcp.remote.echo", arguments={"value": 7}),
        ToolPolicy(
            allowed_tools=["mcp.remote.echo"],
            allow_mcp_tools=True,
        ),
    )

    assert observation.status is ToolStatus.SUCCEEDED
    outbound = client.calls[0][0].trace_carrier
    extracted = W3CTracePropagator().extract_span(outbound)
    assert extracted.context.trace_id == trace.trace_id
    assert extracted.context.span_id != trace.span_id
    assert "baggage" not in outbound
    assert client.calls[0][2] == {"value": 7}


def test_tool_http_outbound_injects_scoped_context_and_restores_caller() -> None:
    parent = W3CSpanContext.root()
    outer = W3CSpanContext.root()
    client = _HTTPClient()
    transport = TraceAwareHTTPToolTransport(client, trace_context=parent)

    with trace_context_scope(outer):
        result = transport.request(
            "post",
            "https://example.test/tool",
            headers={
                "traceparent": f"00-{'3' * 32}-{'4' * 16}-01",
                "baggage": "run_id=attacker-selected",
                "x-safe": "kept",
            },
            body=b"{}",
            timeout_seconds=2,
        )
        assert current_trace_context() is outer

    method, _url, headers, _body, timeout, active = client.calls[0]
    extracted = W3CTracePropagator().extract_span(headers)
    assert result == {"ok": True}
    assert method == "POST"
    assert timeout == 2.0
    assert headers["x-safe"] == "kept"
    assert "baggage" not in headers
    assert extracted.context.trace_id == parent.trace_id
    assert isinstance(active, W3CSpanContext)
    assert active.span_id == extracted.context.span_id


def test_parallel_tool_batch_copies_trace_scope_into_worker_threads() -> None:
    parent = W3CSpanContext.root()
    registry = ToolRegistry()
    observed: list[W3CSpanContext] = []
    lock = Lock()

    def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        context = current_trace_context()
        assert isinstance(context, W3CSpanContext)
        with lock:
            observed.append(context)
        return {"value": arguments["value"]}

    for name in ("sample.one", "sample.two"):
        registry.register(
            ToolDefinition(
                name=name,
                input_schema={"required": ["value"]},
                side_effect="read_only",
                concurrency_safe=True,
            ),
            handler,
        )
    calls = [
        ToolCall(tool_name="sample.one", arguments={"value": 1}),
        ToolCall(tool_name="sample.two", arguments={"value": 2}),
    ]

    with trace_context_scope(parent):
        results = ToolBatchExecutor.from_registry(registry, max_workers=2).execute_batch(
            calls,
            ToolPolicy(allowed_tools=["sample.one", "sample.two"]),
        )
        assert current_trace_context() is parent

    assert all(result.status is ToolStatus.SUCCEEDED for result in results)
    assert len(observed) == 2
    assert {context.trace_id for context in observed} == {parent.trace_id}
    assert {context.parent_span_id for context in observed} == {parent.span_id}
    assert len({context.span_id for context in observed}) == 2


def test_tool_batch_executor_runs_every_call_on_one_environment_bound_executor(
    monkeypatch,
) -> None:
    import framework.tool.runtime.batch_executor as batch_module

    constructed: list[object] = []
    executed: list[tuple[object, str]] = []

    class _CapturingToolExecutor:
        def __init__(self, registry, **kwargs) -> None:
            self.registry = registry
            self.execution_evidence = kwargs["execution_evidence"]
            constructed.append(kwargs["execution_environment"])

        def execute(self, call, _policy):
            from framework.tool.models import ToolObservation, ToolResult

            executed.append((self, call.call_id))
            return ToolObservation(
                call=call,
                result=ToolResult(
                    status=ToolStatus.SUCCEEDED,
                    output={"ok": True},
                    call_id=call.call_id,
                    tool_name=call.tool_name,
                ),
                elapsed_ms=0.0,
            )

        def record_pre_execution_rejection(self, observation):
            return observation

    monkeypatch.setattr(batch_module, "ToolExecutor", _CapturingToolExecutor)
    registry = ToolRegistry()
    registry.register(ToolDefinition(name="sample.echo"), lambda _args: {"ok": True})
    environment = object()
    batch = ToolBatchExecutor.from_registry(
        registry,
        execution_environment=environment,
    )
    result = batch.execute_batch(
        [
            ToolCall(tool_name="sample.echo", call_id="echo-1"),
            ToolCall(tool_name="sample.echo", call_id="echo-2"),
        ],
        ToolPolicy(allowed_tools=["sample.echo"]),
    )

    assert [item.status for item in result] == [ToolStatus.SUCCEEDED] * 2
    assert constructed == [environment]
    assert executed == [(batch.executor, "echo-1"), (batch.executor, "echo-2")]


def test_tool_executor_preserves_transport_only_w3c_context_in_all_outputs() -> None:
    parent = W3CSpanContext.root()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.echo",
            input_schema={"required": ["value"]},
        ),
        lambda arguments: {"value": arguments["value"]},
    )
    executor = ToolExecutor(registry, trace_context=parent)

    observation = executor.execute(
        ToolCall(
            tool_name="sample.echo",
            arguments={"value": 7},
            call_id="transport-call",
        ),
        ToolPolicy(allowed_tools=["sample.echo"]),
    )

    event = executor.list_events()[0].to_dict()
    record = executor.list_records()[0].to_dict()
    result = observation.result.to_dict()
    assert observation.status is ToolStatus.SUCCEEDED
    assert is_valid_span_id(event["span_id"])
    assert event["trace_id"] == parent.trace_id
    assert event["parent_span_id"] == parent.span_id
    assert event["run_id"] is None
    for payload in (result, record):
        assert payload["trace_id"] == event["trace_id"]
        assert payload["span_id"] == event["span_id"]
        assert payload["parent_span_id"] == event["parent_span_id"]
    assert not event["span_id"].startswith("tool:")


class _MCPClient:
    def __init__(self) -> None:
        self.calls: list[tuple[MCPServerConfig, str, dict[str, Any]]] = []

    def list_tools(self, server: MCPServerConfig) -> list[dict[str, Any]]:
        return [{
            "name": "echo",
            "inputSchema": {"required": ["value"]},
            "side_effect": "read_only",
            "is_dangerous": False,
            "requires_approval": False,
        }]

    def call_tool(
        self,
        server: MCPServerConfig,
        remote_tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((server, remote_tool_name, dict(arguments)))
        return {"value": arguments["value"]}


class _HTTPClient:
    def __init__(self) -> None:
        self.calls: list[
            tuple[
                str,
                str,
                dict[str, str],
                bytes | None,
                float,
                W3CSpanContext | None,
            ]
        ] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> dict[str, bool]:
        context = current_trace_context()
        self.calls.append(
            (
                method,
                url,
                dict(headers),
                body,
                timeout_seconds,
                context if isinstance(context, W3CSpanContext) else None,
            )
        )
        return {"ok": True}
