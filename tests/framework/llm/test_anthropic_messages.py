from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from framework.llm.clients.anthropic_messages import (
    ANTHROPIC_API_VERSION,
    AnthropicMessagesClient,
    AnthropicMessagesConfig,
)
from framework.llm.clients.provider_errors import LLMProviderContextOverflow, LLMProviderError
from framework.llm.clients.transport import LLMRetryPolicy
from framework.llm.context import build_default_request_preparer
from framework.llm.context.anthropic import (
    ANTHROPIC_CONTENT_FIELD,
    ANTHROPIC_MESSAGES_NORMALIZER_REVISION,
    AnthropicModelOptions,
    AnthropicRequestError,
    AnthropicThinkingOff,
    build_anthropic_messages_payload,
)
from framework.llm.context.profile import ModelContextProfile
from framework.llm.models import LLMRequest
from framework.llm.models.reasoning import ReasoningEffort
from framework.llm.providers import ProviderClientSettings, build_provider_client
from framework.llm.providers.factory import json_object_capability

E = ReasoningEffort
KEY = "sk-ant-test-key"
SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
LOCAL_GATE = {"allow_json_object_local_gate": True}


def _message(content, *, stop_reason="end_turn", usage=None):
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "usage": usage or {"input_tokens": 10, "output_tokens": 5},
    }


def _client(transport=None, *, stream_lines=None, options=None, sleeps=None, max_attempts=1):
    def stream_transport(request, _timeout):
        sent.append(request)
        return iter(stream_lines or [])

    sent: list = []

    def recording(request, timeout):
        sent.append(request)
        return transport(request, timeout)

    client = AnthropicMessagesClient(
        AnthropicMessagesConfig(
            base_url="https://api.anthropic.com/",
            model="claude-opus-5-5",
            api_key=KEY,
            options=options or AnthropicModelOptions(),
        ),
        transport=recording if transport else None,
        stream_transport=stream_transport,
        retry_policy=LLMRetryPolicy(
            max_attempts=max_attempts,
            retry_delay_seconds=(0.0,),
            retryable_status_codes=(429, 529),
        ),
        structured_output_capability=json_object_capability(provider="anthropic", deployment_id="conn@1"),
        sleep=(sleeps.append if sleeps is not None else None),
    )
    return client, sent


def _returns(*bodies):
    queue = list(bodies)

    def transport(request, _timeout):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item).encode("utf-8")

    return transport


def _http_error(status, payload):
    return HTTPError("https://api.anthropic.com/v1/messages", status, "err", {},
                     BytesIO(json.dumps(payload).encode()))


def _sse(*events):
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}\n".encode())
        lines.append(f"data: {json.dumps(event)}\n".encode())
        lines.append(b"\n")
    return lines


# --- payload translation -------------------------------------------------


def test_payload_lifts_system_and_translates_a_tool_round_trip() -> None:
    request = LLMRequest(
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "system", "content": "cite sources"},
            {"role": "user", "content": [
                {"type": "text", "text": "look"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "assistant", "content": "checking", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "search.papers", "arguments": "{\"q\": \"rag\"}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "found 2"},
            {"role": "user", "content": "go on"},
        ],
        tools=[{"name": "search.papers", "description": "Search papers",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}}],
        temperature=0.3,
        max_tokens=500,
    )
    payload = build_anthropic_messages_payload(request, model="claude-opus-5-5")

    assert payload["system"] == "be brief\n\ncite sources"
    assert payload["max_tokens"] == 500
    assert "temperature" not in payload
    assert payload["tools"] == [{
        "name": "search_papers",
        "description": "Search papers",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]
    user, assistant, tool_turn = payload["messages"]
    assert user["content"][1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }
    assert assistant["content"][1] == {
        "type": "tool_use", "id": "call_1", "name": "search_papers", "input": {"q": "rag"},
    }
    # The tool result and the following user text share one user turn.
    assert tool_turn["role"] == "user"
    assert tool_turn["content"][0] == {"type": "tool_result", "tool_use_id": "call_1", "content": "found 2"}
    assert tool_turn["content"][1] == {"type": "text", "text": "go on"}


def test_payload_replays_provider_content_blocks_verbatim() -> None:
    blocks = [{"type": "thinking", "thinking": "hm", "signature": "sig"},
              {"type": "tool_use", "id": "toolu_1", "name": "x", "input": {}}]
    request = LLMRequest(messages=[
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", ANTHROPIC_CONTENT_FIELD: blocks},
        {"role": "tool", "tool_call_id": "toolu_1", "content": "ok"},
    ])
    payload = build_anthropic_messages_payload(request, model="claude-opus-5-5")
    assert payload["messages"][1]["content"] == blocks


@pytest.mark.parametrize(
    ("effort", "thinking", "wire_effort"),
    [(E.MINIMAL, "adaptive", "low"), (E.MEDIUM, "adaptive", "medium"), (E.MAX, "adaptive", "max")],
)
def test_effort_maps_to_adaptive_thinking(effort, thinking, wire_effort) -> None:
    payload = build_anthropic_messages_payload(
        LLMRequest(messages=[{"role": "user", "content": "q"}], reasoning_effort=effort),
        model="claude-opus-5-5",
    )
    assert payload["thinking"] == {"type": thinking}
    assert payload["output_config"] == {"effort": wire_effort}


def test_off_uses_the_models_own_off_switch_or_is_rejected() -> None:
    request = LLMRequest(messages=[{"role": "user", "content": "q"}], reasoning_effort=E.OFF)
    payload = build_anthropic_messages_payload(
        request, model="claude-sonnet-5-5",
        options=AnthropicModelOptions(thinking_off=AnthropicThinkingOff.BETWEEN_TOOLS),
    )
    assert payload["thinking"] == {"type": "between_tools"}
    assert "output_config" not in payload
    with pytest.raises(AnthropicRequestError):
        build_anthropic_messages_payload(request, model="claude-opus-5-5")


def test_request_effort_overrides_the_connection_default() -> None:
    payload = build_anthropic_messages_payload(
        LLMRequest(messages=[{"role": "user", "content": "q"}], reasoning_effort=E.HIGH),
        model="claude-opus-5-5",
        options=AnthropicModelOptions(reasoning_effort=E.LOW),
    )
    assert payload["output_config"]["effort"] == "high"


def test_preparer_resolves_the_anthropic_normalizer() -> None:
    profile = ModelContextProfile(
        provider="anthropic",
        model="claude-opus-5-5",
        deployment_id="conn@1",
        physical_context_window_tokens=1_000_000,
        max_output_tokens=128_000,
        default_output_tokens=16_000,
        tokenizer_family="conservative",
        tokenizer_revision="v1",
        normalizer_revision=ANTHROPIC_MESSAGES_NORMALIZER_REVISION,
        profile_revision="p1",
        allow_conservative_fallback=True,
    )
    preparer = build_default_request_preparer([profile])
    prepared = preparer.prepare(LLMRequest(messages=[{"role": "user", "content": "q"}]), profile=profile)
    assert prepared.normalizer_revision == ANTHROPIC_MESSAGES_NORMALIZER_REVISION
    assert prepared.admission.status.value == "admitted"


# --- complete --------------------------------------------------------------


def test_complete_sends_headers_and_parses_text_tools_and_usage() -> None:
    client, sent = _client(_returns(_message(
        [
            {"type": "thinking", "thinking": "plan", "signature": "s"},
            {"type": "text", "text": "Calling "},
            {"type": "text", "text": "search."},
            {"type": "tool_use", "id": "toolu_9", "name": "search_papers", "input": {"q": "x"}},
        ],
        stop_reason="tool_use",
        usage={"input_tokens": 100, "cache_read_input_tokens": 40,
               "cache_creation_input_tokens": 10, "output_tokens": 30},
    )), options=AnthropicModelOptions(reasoning_effort=E.HIGH))

    response = client.complete(LLMRequest(
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "search.papers", "description": "d", "input_schema": {"type": "object"}}],
    ))

    request = sent[0]
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("X-api-key") == KEY
    assert request.get_header("Anthropic-version") == ANTHROPIC_API_VERSION
    body = json.loads(request.data)
    assert body["output_config"] == {"effort": "high"}
    assert response.content == "Calling search."
    assert response.tool_calls[0].tool_name == "search.papers"
    assert response.tool_calls[0].arguments == {"q": "x"}
    assert response.usage.input_tokens == 150
    assert response.usage.cached_input_tokens == 40
    assert response.usage.output_tokens == 30
    assert response.metadata["finish_reason"] == "tool_calls"
    assert response.metadata[ANTHROPIC_CONTENT_FIELD][0]["type"] == "thinking"
    assert KEY not in repr(client.config)


def test_structured_output_uses_the_local_gate_and_tolerates_a_fence() -> None:
    client, sent = _client(_returns(_message([
        {"type": "text", "text": "```json\n{\"answer\": \"42\"}\n```"},
    ])))
    response = client.complete(LLMRequest(
        messages=[{"role": "user", "content": "Return JSON."}],
        output_schema=SCHEMA,
        structured_output_policy=LOCAL_GATE,
    ))
    assert response.structured_output == {"answer": "42"}
    assert "output_config" not in json.loads(sent[0].data)
    validation = response.metadata["structured_output_validation"]
    assert validation["validated"] and validation["projection_mode"] == "json_object_local_gate"
    assert validation["provider_native_json_mode"] is False


def test_structured_output_failing_the_schema_is_rejected() -> None:
    client, _ = _client(_returns(_message([{"type": "text", "text": "{\"other\": 1}"}])))
    with pytest.raises(LLMProviderError) as caught:
        client.complete(LLMRequest(
            messages=[{"role": "user", "content": "q"}],
            output_schema=SCHEMA,
            structured_output_policy=LOCAL_GATE,
        ))
    assert caught.value.error_type == "structured_output_validation_error"


def test_structured_output_without_local_gate_permission_is_ineligible() -> None:
    client, sent = _client(_returns())
    with pytest.raises(LLMProviderError) as caught:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}], output_schema=SCHEMA))
    assert caught.value.error_type == "provider_schema_ineligible"
    assert sent == []


def test_refusal_is_a_non_retryable_failure() -> None:
    client, _ = _client(_returns({**_message([]), "stop_reason": "refusal",
                                  "stop_details": {"category": "cyber"}}))
    with pytest.raises(LLMProviderError) as caught:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}]))
    assert caught.value.error_type == "content_refused"
    assert not caught.value.retryable
    assert caught.value.diagnostics[0]["refusal_category"] == "cyber"


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"type": "error", "error": {"type": "invalid_request_error",
                                          "message": "prompt is too long: 1200000 tokens > 1000000 maximum"}}),
        (413, {"type": "error", "error": {"type": "request_too_large", "message": "too large"}}),
    ],
)
def test_oversized_prompts_map_to_context_overflow(status, payload) -> None:
    client, _ = _client(_returns(_http_error(status, payload)))
    with pytest.raises(LLMProviderContextOverflow) as caught:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}]))
    assert caught.value.status_code == status


def test_overloaded_is_retried_then_succeeds() -> None:
    sleeps: list[float] = []
    client, sent = _client(
        _returns(_http_error(529, {"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}),
                 _message([{"type": "text", "text": "ok"}])),
        sleeps=sleeps,
        max_attempts=2,
    )
    response = client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}]))
    assert response.content == "ok"
    assert response.metadata["attempts"] == 2
    assert len(sent) == 2 and sleeps == [0.0]


def test_auth_failure_does_not_echo_the_key() -> None:
    client, _ = _client(_returns(_http_error(
        401, {"type": "error", "error": {"type": "authentication_error", "message": f"bad key {KEY}"}},
    )))
    with pytest.raises(LLMProviderError) as caught:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}]))
    assert caught.value.error_type == "auth_error"
    assert KEY not in json.dumps(caught.value.to_dict(redact=True))


# --- stream ----------------------------------------------------------------


def _stream_events(text_parts, *, tool=None, stop_reason="end_turn"):
    events = [
        {"type": "message_start", "message": {"id": "msg_s", "type": "message", "role": "assistant",
                                              "content": [], "usage": {"input_tokens": 12, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        *({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": part}}
          for part in text_parts),
        {"type": "content_block_stop", "index": 1},
    ]
    if tool is not None:
        events += [
            {"type": "content_block_start", "index": 2,
             "content_block": {"type": "tool_use", "id": "toolu_s", "name": tool, "input": {}}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": "{\"q\":"}},
            {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": " \"y\"}"}},
            {"type": "content_block_stop", "index": 2},
        ]
    events += [
        {"type": "ping"},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 25}},
        {"type": "message_stop"},
    ]
    return _sse(*events)


def test_stream_emits_text_tool_calls_usage_and_completion() -> None:
    client, sent = _client(stream_lines=_stream_events(["Hel", "lo"], tool="search_papers", stop_reason="tool_use"))
    events = list(client.stream(LLMRequest(
        messages=[{"role": "user", "content": "q"}],
        tools=[{"name": "search.papers", "description": "d", "input_schema": {"type": "object"}}],
    )))

    assert json.loads(sent[0].data)["stream"] is True
    kinds = [event.event_type for event in events]
    assert kinds == ["message_start", "text_delta", "text_delta", "usage_delta",
                     "tool_call_complete", "message_complete"]
    assert "".join(event.text_delta for event in events if event.text_delta) == "Hello"
    usage = events[3].usage_delta
    assert (usage.input_tokens, usage.output_tokens) == (12, 25)
    tool_call = events[4].tool_call
    assert tool_call.tool_name == "search.papers" and tool_call.arguments == {"q": "y"}
    done = events[-1].metadata
    assert done["finish_reason"] == "tool_calls"
    assert done[ANTHROPIC_CONTENT_FIELD][0] == {"type": "thinking", "thinking": "hm", "signature": "sig"}


def test_stream_validates_structured_output_at_the_end() -> None:
    client, _ = _client(stream_lines=_stream_events(["{\"answer\"", ": \"yes\"}"]))
    events = list(client.stream(LLMRequest(
        messages=[{"role": "user", "content": "q"}],
        output_schema=SCHEMA,
        structured_output_policy=LOCAL_GATE,
    )))
    assert events[0].metadata["provisional"] is True
    assert events[-1].structured_output == {"answer": "yes"}


def test_stream_without_message_stop_is_incomplete() -> None:
    lines = _stream_events(["partial"])[:-6]
    client, _ = _client(stream_lines=lines)
    with pytest.raises(LLMProviderError) as caught:
        list(client.stream(LLMRequest(messages=[{"role": "user", "content": "q"}])))
    assert caught.value.error_type == "provider_stream_incomplete"


def test_stream_error_event_is_categorised() -> None:
    lines = _sse({"type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
    client, _ = _client(stream_lines=lines)
    with pytest.raises(LLMProviderError) as caught:
        list(client.stream(LLMRequest(messages=[{"role": "user", "content": "q"}])))
    assert caught.value.error_type == "server_error" and caught.value.retryable


# --- factory -----------------------------------------------------------------


def test_factory_builds_an_anthropic_client_from_the_known_model_table() -> None:
    sent = []

    def transport(request, _timeout):
        sent.append(request)
        return json.dumps(_message([{"type": "text", "text": "ok"}])).encode()

    client = build_provider_client(
        ProviderClientSettings(
            provider="anthropic",
            deployment_id="conn-2@1",
            base_url="https://api.anthropic.com",
            model="claude-sonnet-5-5",
            api_key=KEY,
            reasoning_effort="off",
        ),
        transport=transport,
    )
    assert isinstance(client, AnthropicMessagesClient)
    client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}]))
    body = json.loads(sent[0].data)
    assert body["thinking"] == {"type": "between_tools"}
    assert body["max_tokens"] == 16_000
