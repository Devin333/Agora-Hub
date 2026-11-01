from __future__ import annotations

import json

import httpx2

from framework.llm import LLMRequest, LLMStreamEvent
from framework.llm.clients import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.anthropic_messages import AnthropicMessagesClient
from framework.llm.clients.tool_adapters import parse_openai_tool_calls
from framework.llm.models import LLMStreamAccumulator, LLMToolCall

_TOOLS = [
    {
        "name": "search.papers",
        "description": "Search papers",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
    {
        "name": "search.news",
        "description": "Search news",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
]


def test_openai_malformed_arguments_reject_only_their_own_call() -> None:
    calls = parse_openai_tool_calls(
        [
            {
                "id": "call_ok",
                "type": "function",
                "function": {"name": "search_papers", "arguments": '{"q": "agents"}'},
            },
            {
                "id": "call_bad",
                "type": "function",
                "function": {"name": "search_news", "arguments": '{"q": "agents"'},
            },
            {
                "id": "call_list",
                "type": "function",
                "function": {"name": "search_news", "arguments": "[1, 2]"},
            },
        ],
        _TOOLS,
    )

    assert [call.tool_call_id for call in calls] == ["call_ok", "call_bad", "call_list"]
    assert [call.tool_name for call in calls] == [
        "search.papers",
        "search.news",
        "search.news",
    ]
    assert calls[0].argument_error is None
    assert calls[0].arguments == {"q": "agents"}
    assert "not valid JSON" in (calls[1].argument_error or "")
    assert calls[1].arguments == {}
    assert calls[1].raw_arguments == '{"q": "agents"'
    assert calls[2].argument_error == "tool call arguments must be a JSON object"


def test_stream_accumulator_keeps_siblings_of_a_truncated_tool_call() -> None:
    accumulator = LLMStreamAccumulator()
    for event in (
        LLMStreamEvent(event_type="message_start"),
        LLMStreamEvent(
            event_type="tool_call_delta",
            tool_call_delta={"tool_call_id": "a", "tool_name": "search.papers", "arguments": '{"q":'},
        ),
        LLMStreamEvent(
            event_type="tool_call_delta",
            tool_call_delta={"tool_call_id": "a", "arguments": ' "agents"}'},
        ),
        LLMStreamEvent(
            event_type="tool_call_delta",
            tool_call_delta={"tool_call_id": "b", "tool_name": "search.news", "arguments": '{"q": "cut'},
        ),
        LLMStreamEvent(event_type="message_complete"),
    ):
        accumulator.add_event(event)

    response = accumulator.to_response()

    assert [call.tool_call_id for call in response.tool_calls] == ["a", "b"]
    assert response.tool_calls[0].arguments == {"q": "agents"}
    assert response.tool_calls[0].argument_error is None
    assert "not valid JSON" in (response.tool_calls[1].argument_error or "")


def test_invalid_argument_marker_survives_serialization() -> None:
    call = LLMToolCall.with_invalid_arguments(
        tool_call_id="call_bad",
        tool_name="search.news",
        raw_arguments="{",
        error="tool call arguments are not valid JSON: Expecting property name",
    )

    restored = LLMToolCall.from_dict(call.to_dict(redact=False))

    assert restored.argument_error == call.argument_error
    assert restored.raw_arguments == "{"


def _sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode("utf-8")


def _anthropic_client(content: bytes) -> AnthropicMessagesClient:
    endpoint = LLMEndpoint(
        base_url="https://api.anthropic.example",
        model="claude-sonnet-5-5",
        api_key="sk-ant-test",
        provider="anthropic",
        protocol=LLMWireProtocol.ANTHROPIC_MESSAGES,
        max_output_tokens=8_000,
    )
    response = httpx2.Response(200, content=content, headers={"content-type": "text/event-stream"})
    return AnthropicMessagesClient.for_endpoint(
        endpoint,
        timeout_seconds=30,
        http_client=httpx2.Client(transport=httpx2.MockTransport(lambda _request: response)),
        sleep=lambda _seconds: None,
    )


def test_anthropic_stream_keeps_siblings_of_malformed_tool_input() -> None:
    message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }
    stream = _sse(
        {"type": "message_start", "message": message},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_ok", "name": "search_papers", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"q": "agents"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_bad", "name": "search_news", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"q": "cut'},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 9},
        },
        {"type": "message_stop"},
    )

    events = list(
        _anthropic_client(stream).stream(
            LLMRequest(messages=[{"role": "user", "content": "find"}], tools=_TOOLS)
        )
    )

    calls = [event.tool_call for event in events if event.event_type == "tool_call_complete"]
    assert [call.tool_call_id for call in calls] == ["toolu_ok", "toolu_bad"]
    assert [call.tool_name for call in calls] == ["search.papers", "search.news"]
    assert calls[0].arguments == {"q": "agents"}
    assert calls[0].argument_error is None
    assert "not valid JSON" in (calls[1].argument_error or "")
    assert calls[1].raw_arguments == '{"q": "cut'
    assert events[-1].event_type == "message_complete"
