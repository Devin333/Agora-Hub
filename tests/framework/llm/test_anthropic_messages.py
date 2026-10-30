from __future__ import annotations

import json

import httpx2
import pytest

from framework.llm import (
    LLMProviderContextOverflow,
    LLMProviderError,
    LLMRequest,
    LLMRetryPolicy,
    ProviderStructuredOutputCapability,
    ProviderStructuredOutputPolicy,
)
from framework.llm.clients import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.anthropic_messages import AnthropicMessagesClient
from framework.llm.context.anthropic import build_anthropic_messages_payload
from framework.llm.models.reasoning import ReasoningEffort, ReasoningSetting, ReasoningWire

KEY = "sk-ant-account-secret"
_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}


def _capability() -> ProviderStructuredOutputCapability:
    return ProviderStructuredOutputCapability(
        provider="anthropic",
        deployment="anthropic/claude-sonnet-5-5",
        mode="json_object",
        revision="anthropic-json-local-gate-v1",
    )


def _message(text: str = "ok", *, stop_reason: str = "end_turn", content=None) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content if content is not None else [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 4,
            "cache_creation_input_tokens": 0,
        },
    }


class _Recorder:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.requests: list[httpx2.Request] = []
        self._responses = list(responses)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return response

    def body(self, index: int = 0) -> dict:
        return json.loads(self.requests[index].content.decode("utf-8"))


def _client(recorder: _Recorder, **overrides) -> AnthropicMessagesClient:
    endpoint = LLMEndpoint(
        base_url="https://api.anthropic.example",
        model="claude-sonnet-5-5",
        api_key=KEY,
        provider="anthropic",
        protocol=LLMWireProtocol.ANTHROPIC_MESSAGES,
        structured_output_capability=overrides.pop("capability", _capability()),
        reasoning=overrides.pop(
            "reasoning", ReasoningSetting(ReasoningWire.ANTHROPIC_EFFORT, ReasoningEffort.HIGH)
        ),
        max_output_tokens=overrides.pop("max_output_tokens", 64_000),
    )
    return AnthropicMessagesClient.for_endpoint(
        endpoint,
        timeout_seconds=30,
        http_client=httpx2.Client(transport=httpx2.MockTransport(recorder)),
        sleep=lambda _seconds: None,
        **overrides,
    )


def _request(**overrides) -> LLMRequest:
    values = dict(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "hello"},
        ],
        max_tokens=900,
        temperature=0.0,
    )
    values.update(overrides)
    return LLMRequest(**values)


def test_complete_sends_messages_api_body_with_the_account_key():
    recorder = _Recorder(httpx2.Response(200, json=_message("hi there")))

    response = _client(recorder).complete(_request(model="route-model"))

    sent = recorder.requests[0]
    assert str(sent.url) == "https://api.anthropic.example/v1/messages"
    assert sent.headers["authorization"] == f"Bearer {KEY}"
    body = recorder.body()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "Be brief."
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert body["output_config"] == {"effort": "high"}
    assert "temperature" not in body
    # 900 visible + 16384 reasoning allowance for "high".
    assert body["max_tokens"] == 900 + 16_384
    assert response.content == "hi there"
    assert response.metadata["provider"] == "anthropic"
    assert response.usage.input_tokens == 14
    assert response.usage.cached_input_tokens == 4


def test_reasoning_off_disables_thinking_and_keeps_the_visible_budget():
    recorder = _Recorder(httpx2.Response(200, json=_message()))
    client = _client(
        recorder, reasoning=ReasoningSetting(ReasoningWire.ANTHROPIC_EFFORT, ReasoningEffort.OFF)
    )

    client.complete(_request())

    body = recorder.body()
    assert body["thinking"] == {"type": "disabled"}
    assert "output_config" not in body
    assert body["max_tokens"] == 900


def test_structured_output_goes_through_the_local_gate():
    recorder = _Recorder(httpx2.Response(200, json=_message('```json\n{"answer": "42"}\n```')))
    request = _request(
        output_schema=_SCHEMA,
        output_schema_name="answer",
        structured_output_policy=ProviderStructuredOutputPolicy(allow_json_object_local_gate=True),
    )

    response = _client(recorder).complete(request)

    body = recorder.body()
    assert "JSON Schema" in body["system"]
    assert "output_config" not in body or "format" not in body["output_config"]
    assert response.structured_output == {"answer": "42"}
    assert response.metadata["structured_output_validation"]["projection_mode"] == "json_object_local_gate"


def test_structured_output_that_violates_the_contract_is_refused():
    recorder = _Recorder(httpx2.Response(200, json=_message('{"answer": ""}')))
    request = _request(
        output_schema=_SCHEMA,
        output_schema_name="answer",
        structured_output_policy=ProviderStructuredOutputPolicy(allow_json_object_local_gate=True),
    )

    with pytest.raises(LLMProviderError) as raised:
        _client(recorder).complete(request)

    assert raised.value.error_type == "structured_output_validation_error"


def test_endpoint_without_capability_refuses_structured_output_before_sending():
    recorder = _Recorder(httpx2.Response(200, json=_message("{}")))

    with pytest.raises(LLMProviderError) as raised:
        _client(recorder, capability=None).complete(_request(output_schema=_SCHEMA, output_schema_name="answer"))

    assert raised.value.error_type == "provider_schema_ineligible"
    assert recorder.requests == []


def test_refusal_and_context_window_stops_are_typed_failures():
    refusal = _Recorder(httpx2.Response(200, json=_message("", stop_reason="refusal")))
    overflow = _Recorder(httpx2.Response(200, json=_message("", stop_reason="model_context_window_exceeded")))

    with pytest.raises(LLMProviderError) as refused:
        _client(refusal).complete(_request())
    with pytest.raises(LLMProviderContextOverflow):
        _client(overflow).complete(_request())

    assert refused.value.error_type == "provider_refusal"
    assert refused.value.retryable is False


def test_http_errors_are_redacted_and_retried_by_the_project_policy():
    leaked = {"type": "error", "error": {"type": "overloaded_error", "message": f"echo {KEY}"}}
    recorder = _Recorder(
        httpx2.Response(529, json=leaked),
        httpx2.Response(200, json=_message("after retry")),
    )
    client = _client(recorder, retry_policy=LLMRetryPolicy(max_attempts=2, retryable_status_codes=(529,)))

    response = client.complete(_request())

    assert response.content == "after retry"
    assert response.metadata["attempts"] == 2
    assert len(recorder.requests) == 2

    failing = _Recorder(httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": KEY}}))
    with pytest.raises(LLMProviderError) as raised:
        _client(failing).complete(_request())
    assert raised.value.error_type == "auth_error"
    assert raised.value.retryable is False
    assert KEY not in json.dumps(raised.value.to_dict(redact=False))
    assert len(failing.requests) == 1


def test_tool_calls_round_trip_in_messages_shape():
    content = [
        {"type": "text", "text": "Looking it up."},
        {"type": "tool_use", "id": "toolu_1", "name": "search", "input": {"q": "agent"}},
    ]
    recorder = _Recorder(httpx2.Response(200, json=_message(content=content, stop_reason="tool_use")))
    tools = [{"name": "search", "description": "Search papers", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}]

    response = _client(recorder).complete(_request(tools=tools))

    assert recorder.body()["tools"][0]["name"] == "search"
    assert response.tool_calls[0].tool_name == "search"
    assert response.tool_calls[0].tool_call_id == "toolu_1"
    assert response.tool_calls[0].arguments == {"q": "agent"}


def test_payload_translates_tool_turns_and_merges_system_prompts():
    request = LLMRequest(
        messages=[
            {"role": "system", "content": "A"},
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "search", "arguments": '{"q": "x"}'}}],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
            {"role": "system", "content": "B"},
        ],
    )

    body = build_anthropic_messages_payload(request, model="claude-haiku-5-5", max_output_tokens=8_000)

    assert body["system"] == "A\n\nB"
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": "c1", "name": "search", "input": {"q": "x"}}],
    }
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert body["max_tokens"] == 8_000


def _sse(*events: dict) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode("utf-8")


def test_stream_yields_text_and_a_validated_terminal_event():
    stream = _sse(
        {"type": "message_start", "message": {**_message(content=[]), "stop_reason": None}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hel"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    )
    recorder = _Recorder(httpx2.Response(200, content=stream, headers={"content-type": "text/event-stream"}))

    events = list(_client(recorder).stream(_request()))

    assert recorder.body()["stream"] is True
    assert [event.text_delta for event in events if event.event_type == "text_delta"] == ["hel", "lo"]
    usage = next(event for event in events if event.event_type == "usage_delta").usage_delta
    assert usage.output_tokens == 7
    terminal = events[-1]
    assert terminal.event_type == "message_complete"
    assert terminal.metadata["finish_reason"] == "end_turn"


def test_stream_without_terminal_event_fails():
    stream = _sse(
        {"type": "message_start", "message": {**_message(content=[]), "stop_reason": None}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    recorder = _Recorder(httpx2.Response(200, content=stream, headers={"content-type": "text/event-stream"}))

    with pytest.raises(LLMProviderError) as raised:
        list(_client(recorder).stream(_request()))

    assert raised.value.error_type == "provider_stream_incomplete"


def test_list_models_reads_the_models_endpoint():
    listing = {
        "data": [
            {"id": "claude-sonnet-5-5", "type": "model", "display_name": "Sonnet", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "claude-haiku-5-5", "type": "model", "display_name": "Haiku", "created_at": "2026-01-01T00:00:00Z"},
        ],
        "has_more": False,
        "first_id": "claude-sonnet-5-5",
        "last_id": "claude-haiku-5-5",
    }
    recorder = _Recorder(httpx2.Response(200, json=listing))

    models = _client(recorder).list_models()

    assert models == ("claude-sonnet-5-5", "claude-haiku-5-5")
    assert recorder.requests[0].url.path == "/v1/models"
    assert recorder.requests[0].headers["authorization"] == f"Bearer {KEY}"


def test_client_refuses_an_openai_endpoint():
    endpoint = LLMEndpoint(base_url="https://a.example/v1", model="m", api_key=KEY, provider="p")
    with pytest.raises(ValueError):
        AnthropicMessagesClient.for_endpoint(endpoint, timeout_seconds=10)


def test_refused_endpoint_is_a_final_configuration_failure():
    from framework.shared.errors import BoundaryViolationError

    attempts: list[httpx2.Request] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        raise BoundaryViolationError("outbound target rejected: non_public_address", code="non_public_address")

    client = _client(_Recorder(httpx2.Response(200, json=_message())), retry_policy=LLMRetryPolicy(max_attempts=3))
    client._sdk._client = httpx2.Client(transport=httpx2.MockTransport(refuse))

    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request())

    assert raised.value.error_type == "endpoint_not_allowed"
    assert raised.value.retryable is False
    assert len(attempts) == 1
