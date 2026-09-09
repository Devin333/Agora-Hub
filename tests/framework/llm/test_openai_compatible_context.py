from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from framework.llm import (
    LLMProviderContextOverflow,
    LLMProviderError,
    LLMRequest,
    LLMRetryPolicy,
    LOCAL_STRUCTURED_OUTPUT_DIALECT,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    ProviderStructuredOutputCapability,
    build_openai_chat_payload,
)
from tests.framework.llm._structured_output_release import (
    approved_structured_output_release,
)


def _config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        provider="test-provider",
        base_url="https://llm.example/v1",
        model="deployment-model",
        api_key_env="TEST_CONTEXT_API_KEY",
    )


def test_request_budget_limits_transport_timeout_and_retry_count(monkeypatch):
    monkeypatch.setenv("TEST_CONTEXT_API_KEY", "test-key")
    timeouts = []

    def fail(_request, timeout):
        timeouts.append(timeout)
        raise TimeoutError("provider did not respond")

    client = OpenAICompatibleClient(_config(), transport=fail)
    request = LLMRequest(messages=[{"role": "user", "content": "plan"}], timeout_seconds=2.0, max_transport_attempts=1)
    assert request.clone().timeout_seconds == 2.0
    assert request.clone().max_transport_attempts == 1
    with pytest.raises(LLMProviderError):
        client.complete(request)
    assert len(timeouts) == 1
    assert 0 < timeouts[0] <= 2.0


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
def test_request_rejects_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        LLMRequest(messages=[], timeout_seconds=timeout)


def _success_body() -> bytes:
    return json.dumps(
        {
            "id": "response-1",
            "choices": [
                {
                    "message": {"content": '{"answer":"ok"}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
    ).encode("utf-8")


def _http_error(status: int, payload: dict) -> HTTPError:
    return HTTPError(
        "https://llm.example/v1/chat/completions",
        status,
        "provider error",
        hdrs=None,
        fp=BytesIO(json.dumps(payload).encode("utf-8")),
    )


def _native_capability() -> ProviderStructuredOutputCapability:
    return ProviderStructuredOutputCapability(
        provider="test-provider",
        deployment="direct-context-test",
        mode="native_strict",
        supported_dialect=LOCAL_STRUCTURED_OUTPUT_DIALECT,
        supported_keywords=frozenset({"properties", "required", "type"}),
        supports_stream_terminal_validation=True,
        revision="direct-context-native-v1",
        release=approved_structured_output_release(
            provider="test-provider",
            deployment="direct-context-test",
            capability_revision="direct-context-native-v1",
        ),
    )


def test_complete_and_stream_wire_payloads_share_normalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTEXT_API_KEY", "api-key-must-not-leak")
    complete_payloads: list[dict] = []
    stream_payloads: list[dict] = []

    def transport(http_request, timeout):  # type: ignore[no-untyped-def]
        complete_payloads.append(json.loads(http_request.data.decode("utf-8")))
        return _success_body()

    def stream_transport(http_request, timeout):  # type: ignore[no-untyped-def]
        stream_payloads.append(json.loads(http_request.data.decode("utf-8")))
        yield (
            b'data: {"choices":[{"delta":{"content":"{\\"answer\\":\\"ok\\"}"},'
            b'"finish_reason":"stop"}]}\n'
        )
        yield b"data: [DONE]\n"

    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        stream_transport=stream_transport,
        structured_output_capability=_native_capability(),
    )
    request = LLMRequest(
        messages=[
            {"role": "user", "content": "hello"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "inspect"},
                    {"type": "image_url", "image_url": {"url": "https://example/image"}},
                ],
            },
        ],
        temperature=0,
        max_tokens=321,
        tools=[
            {
                "name": "lookup",
                "description": "look up a value",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            }
        ],
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
        output_schema_name="answer_contract",
    )

    client.complete(request)
    events = list(client.stream(request))

    expected = build_openai_chat_payload(request, model="deployment-model")
    assert complete_payloads == [expected]
    assert stream_payloads == [{**expected, "stream": True}]
    assert [event.event_type for event in events] == [
        "message_start",
        "text_delta",
        "message_complete",
    ]
    assert events[1].metadata["provisional"] is True
    assert events[-1].structured_output == {"answer": "ok"}


def test_http_413_is_non_retryable_context_overflow_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTEXT_API_KEY", "secret")
    calls = 0
    sleeps: list[float] = []

    def transport(http_request, timeout):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        raise _http_error(
            413,
            {
                "error": {
                    "message": "raw provider body must not leak",
                    "details": {
                        "context_limit_tokens": 128000,
                        "requested_tokens": 130001,
                    },
                }
            },
        )

    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        retry_policy=LLMRetryPolicy(max_attempts=3, retry_delay_seconds=(1, 2)),
        sleep=sleeps.append,
    )

    with pytest.raises(LLMProviderContextOverflow) as raised:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "too large"}]))

    assert calls == 1
    assert sleeps == []
    assert raised.value.retryable is False
    assert raised.value.status_code == 413
    assert raised.value.provider_reported_limit_tokens == 128000
    assert raised.value.provider_reported_usage_tokens == 130001
    assert "raw provider body" not in json.dumps(raised.value.to_dict())


def test_structured_http_400_context_code_maps_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTEXT_API_KEY", "secret")

    def transport(http_request, timeout):  # type: ignore[no-untyped-def]
        raise _http_error(
            400,
            {
                "error": {
                    "code": "context_length_exceeded",
                    "details": {
                        "max_context_tokens": 1000000,
                        "input_tokens": 1000001,
                        "unmapped_secret": "do-not-retain",
                    },
                }
            },
        )

    client = OpenAICompatibleClient(_config(), transport=transport)

    with pytest.raises(LLMProviderContextOverflow) as raised:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    payload = raised.value.to_dict(redact=False)
    assert payload["provider_error_code"] == "context_length_exceeded"
    assert payload["provider_reported_limit_tokens"] == 1000000
    assert payload["provider_reported_usage_tokens"] == 1000001
    assert "unmapped_secret" not in payload


def test_unmapped_http_400_message_is_not_guessed_as_context_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTEXT_API_KEY", "secret")

    def transport(http_request, timeout):  # type: ignore[no-untyped-def]
        raise _http_error(
            400,
            {
                "error": {
                    "code": "invalid_request",
                    "message": "token length exceeds a business field",
                }
            },
        )

    client = OpenAICompatibleClient(_config(), transport=transport)

    with pytest.raises(LLMProviderError) as raised:
        client.complete(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    assert not isinstance(raised.value, LLMProviderContextOverflow)
    assert raised.value.error_type == "invalid_request"


def test_stream_http_overflow_happens_before_message_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONTEXT_API_KEY", "secret")

    def stream_transport(http_request, timeout):  # type: ignore[no-untyped-def]
        raise _http_error(413, {"error": {"code": "context_length_exceeded"}})
        yield b"unreachable"

    client = OpenAICompatibleClient(_config(), stream_transport=stream_transport)
    stream = client.stream(LLMRequest(messages=[{"role": "user", "content": "x"}]))

    with pytest.raises(LLMProviderContextOverflow):
        next(stream)
