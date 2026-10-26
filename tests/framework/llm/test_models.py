from __future__ import annotations

from framework.llm import (
    LLMMessage,
    LLMMessageRole,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMToolCall,
    ProviderStructuredOutputPolicy,
    TokenUsage,
    structured_output_graph_scope,
)
from framework.shared.graph_identity import GraphExecutionIdentity


def test_message_helpers_round_trip() -> None:
    message = LLMMessage.tool("done", "call_1")

    payload = message.to_dict()

    assert payload == {"role": "tool", "content": "done", "tool_call_id": "call_1"}
    assert LLMMessage.from_dict(payload) == message
    assert LLMMessage.system("s").role == LLMMessageRole.SYSTEM.value


def test_request_accepts_prd_and_legacy_messages() -> None:
    request = LLMRequest(
        messages=[LLMMessage.user("hello"), {"role": "assistant", "content": "hi"}],
        model="demo",
        temperature=0.2,
        max_tokens=128,
    )

    payload = request.to_dict(redact=False)
    restored = LLMRequest.from_dict(payload)

    assert payload["messages"][0] == {"role": "user", "content": "hello"}
    assert restored.model == "demo"
    assert restored.estimated_prompt_text() == "hello\nhi"


def test_response_tool_call_usage_round_trip() -> None:
    tool_call = LLMToolCall(
        tool_call_id="call_1",
        tool_name="memory.recall",
        raw_arguments='{"query": "agent"}',
    )
    response = LLMResponse(
        content=None,
        usage=TokenUsage(input_tokens=2, output_tokens=3),
        tool_calls=[tool_call],
        model="demo",
        raw={"id": "resp_1"},
    )

    restored = LLMResponse.from_dict(response.to_dict(redact=False))

    assert response.usage.total_tokens == 5
    assert response.usage.total_tokens() == 5
    assert response.has_tool_calls() is True
    assert response.tool_calls[0].arguments_dict() == {"query": "agent"}
    assert restored.model == "demo"
    assert restored.raw == {"id": "resp_1"}


def test_request_response_round_trip_exact_graph_execution_identity() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-1",
        graph_id="graph",
        graph_version="v1",
        graph_ref="graph@v1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="node",
        node_instance_id="node-instance",
        activity_id="activity",
        attempt=1,
    )
    request = LLMRequest(messages=[{"role": "user", "content": "hello"}], execution_identity=identity)
    response = LLMResponse(content="ok", execution_identity=identity)

    assert LLMRequest.from_dict(request.to_dict(redact=False)).execution_identity == identity
    assert LLMResponse.from_dict(response.to_dict(redact=False)).execution_identity == identity


def test_graph_request_rebinds_structured_output_scope_to_exact_definition() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-structured",
        graph_id="graph",
        graph_version="v3",
        graph_ref="graph@v3",
        graph_checksum="sha256:" + "c" * 64,
        node_id="writer",
        node_instance_id="writer-instance",
        activity_id="activity",
        attempt=1,
    )

    request = LLMRequest(
        messages=[],
        execution_identity=identity,
        structured_output_policy=ProviderStructuredOutputPolicy(
            graph_scope="research.candidate"
        ),
    )

    assert request.structured_output_policy.graph_scope == structured_output_graph_scope(
        identity
    )


def test_stream_event_round_trip_exact_graph_execution_identity() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-stream",
        graph_id="graph",
        graph_version="v1",
        graph_ref="graph@v1",
        graph_checksum="sha256:" + "b" * 64,
        node_id="node",
        node_instance_id="node-instance",
        activity_id="activity",
        attempt=2,
    )
    event = LLMStreamEvent(
        event_type="text_delta",
        text_delta="chunk",
        execution_identity=identity,
    )

    restored = LLMStreamEvent.from_any(event.to_dict(redact=False))

    assert restored == event


def test_token_usage_sum_adds_counts_and_keeps_cost_only_when_every_call_has_one() -> None:
    priced = TokenUsage.sum([
        TokenUsage(input_tokens=2, output_tokens=1, estimated_cost_usd=0.5),
        TokenUsage(input_tokens=3, reasoning_tokens=4, cached_input_tokens=1, estimated_cost_usd=0.25),
    ])
    partial = TokenUsage.sum([TokenUsage(input_tokens=1, estimated_cost_usd=0.5), TokenUsage(input_tokens=1)])

    assert (priced.input_tokens, priced.output_tokens, priced.reasoning_tokens) == (5, 1, 4)
    assert priced.cached_input_tokens == 1
    assert priced.estimated_cost_usd == 0.75
    assert partial.estimated_cost_usd is None
    assert TokenUsage.sum([]) == TokenUsage()
