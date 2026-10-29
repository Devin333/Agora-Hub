from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from framework.llm.clients.provider_errors import LLMProviderError
from framework.llm.models import LLMRequest, LLMResponse
from framework.llm.models.reasoning import ReasoningEffort, ReasoningParameterStyle
from framework.llm.providers import (
    PROVIDERS,
    AnthropicThinkingOff,
    DiscoveredModel,
    ModelDiscovery,
    ProviderClientSettings,
    ProviderKind,
    build_provider_client,
    describe_model,
    probe_model,
    provider_spec,
)
from framework.shared.secret_text import SecretText

E = ReasoningEffort
KEY = SecretText("sk-test-discovery-key")


def _json_transport(pages):
    calls = []

    def transport(request, _timeout):
        calls.append(request)
        return json.dumps(pages[len(calls) - 1]).encode("utf-8")

    return transport, calls


def test_every_known_model_preselects_a_tier_it_offers() -> None:
    for spec in PROVIDERS.values():
        for model in spec.known_models:
            assert model.default_reasoning_effort in model.reasoning_efforts
            if model.reasoning_efforts and E.MEDIUM in model.reasoning_efforts:
                assert model.default_reasoning_effort is E.MEDIUM


def test_custom_endpoint_tiers_follow_the_selected_parameter_style() -> None:
    spec = provider_spec(ProviderKind.OPENAI_COMPATIBLE)
    assert spec.base_url_required
    assert spec.efforts_for("any-model") == ()
    assert spec.efforts_for("any-model", reasoning_parameter=ReasoningParameterStyle.DEEPSEEK_THINKING) == (
        E.OFF, E.LOW, E.HIGH, E.MAX,
    )
    dialect = spec.chat_dialect("any-model", reasoning_parameter=ReasoningParameterStyle.OPENROUTER_REASONING)
    assert dialect.reasoning_parameter is ReasoningParameterStyle.OPENROUTER_REASONING


def test_named_vendors_ignore_a_selected_parameter_style() -> None:
    spec = provider_spec("deepseek")
    dialect = spec.chat_dialect("deepseek-v4-pro", reasoning_parameter=ReasoningParameterStyle.NONE)
    assert dialect.reasoning_parameter is ReasoningParameterStyle.DEEPSEEK_THINKING


def test_dashscope_deepseek_only_toggles_thinking() -> None:
    spec = provider_spec(ProviderKind.DASHSCOPE)
    profile = describe_model(spec, "deepseek-v4-flash")
    assert profile.reasoning_parameter is ReasoningParameterStyle.DASHSCOPE_THINKING_TOGGLE
    assert profile.reasoning_efforts == (E.OFF, E.MAX)
    assert profile.context_window_tokens == 1_000_000
    qwen = describe_model(spec, "qwen3.8-plus")
    assert qwen.reasoning_parameter is ReasoningParameterStyle.DASHSCOPE_THINKING
    assert qwen.context_window_tokens is None
    assert qwen.default_reasoning_effort is E.MEDIUM


def test_openai_compatible_listing_reads_windows_and_dedupes() -> None:
    transport, calls = _json_transport([
        {
            "data": [
                {"id": "z/model", "name": "Z", "context_length": 200000,
                 "top_provider": {"max_completion_tokens": 32000}},
                {"id": "a/model"},
                {"id": "a/model"},
                {"id": "  "},
                "not-an-object",
            ]
        }
    ])
    models = ModelDiscovery(transport=transport).list_models(
        provider_spec("openrouter"), base_url="https://openrouter.ai/api/v1/", api_key=KEY
    )
    assert [model.model_id for model in models] == ["a/model", "z/model"]
    assert models[1].context_window_tokens == 200000
    assert models[1].max_output_tokens == 32000
    assert calls[0].full_url == "https://openrouter.ai/api/v1/models"
    assert calls[0].get_header("Authorization") == "Bearer sk-test-discovery-key"


def test_anthropic_listing_pages_and_reads_capabilities() -> None:
    def effort(*levels):
        return {"supported": True, **{level: {"supported": level in levels} for level in
                                      ("low", "medium", "high", "xhigh", "max")}}

    transport, calls = _json_transport([
        {
            "data": [{
                "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                "max_input_tokens": 1000000, "max_tokens": 128000,
                "capabilities": {
                    "effort": effort("low", "medium", "high", "xhigh", "max"),
                    "thinking": {"supported": True, "types": {"adaptive": {"supported": True},
                                                              "disabled": {"supported": False}}},
                },
            }],
            "has_more": True,
            "last_id": "claude-opus-5-5",
        },
        {
            "data": [{
                "id": "claude-sonnet-5-5", "max_input_tokens": 1000000, "max_tokens": 128000,
                "capabilities": {
                    "effort": effort("low", "medium", "high"),
                    "thinking": {"types": {"between_tools": {"supported": True}}},
                },
            }],
            "has_more": False,
        },
    ])
    models = ModelDiscovery(transport=transport).list_models(
        provider_spec("anthropic"), base_url="https://api.anthropic.com", api_key=KEY
    )

    opus, sonnet = models
    assert opus.reasoning_efforts == (E.LOW, E.MEDIUM, E.HIGH, E.XHIGH, E.MAX)
    assert opus.thinking_off is None
    assert sonnet.reasoning_efforts == (E.OFF, E.LOW, E.MEDIUM, E.HIGH)
    assert sonnet.thinking_off is AnthropicThinkingOff.BETWEEN_TOOLS
    assert calls[0].get_header("X-api-key") == "sk-test-discovery-key"
    assert calls[0].get_header("Anthropic-version")
    assert parse_qs(urlsplit(calls[1].full_url).query)["after_id"] == ["claude-opus-5-5"]


def test_live_capabilities_win_over_the_known_model_table() -> None:
    spec = provider_spec("anthropic")
    live = DiscoveredModel(
        "claude-opus-5-5",
        context_window_tokens=500_000,
        reasoning_efforts=(E.LOW, E.HIGH),
    )
    profile = describe_model(spec, "claude-opus-5-5", discovered=live)
    assert profile.context_window_tokens == 500_000
    assert profile.max_output_tokens == 128_000
    assert profile.reasoning_efforts == (E.LOW, E.HIGH)
    assert profile.default_reasoning_effort is E.HIGH
    assert not profile.accepts(E.MEDIUM)


def test_listing_failure_is_categorised_without_echoing_the_key() -> None:
    def transport(request, _timeout):
        body = json.dumps({"error": {"message": "invalid key sk-test-discovery-key"}}).encode()
        raise HTTPError(request.full_url, 401, "Unauthorized", {}, BytesIO(body))

    with pytest.raises(LLMProviderError) as caught:
        ModelDiscovery(transport=transport).list_models(
            provider_spec("openai"), base_url="https://api.openai.com/v1", api_key=KEY
        )
    assert caught.value.error_type == "auth_error"
    assert caught.value.status_code == 401
    assert "sk-test-discovery-key" not in str(caught.value)


def test_probe_reports_success_and_failure_as_outcomes() -> None:
    requests = []

    class Client:
        def __init__(self, error=None):
            self.error = error

        def complete(self, request):
            requests.append(request)
            if self.error:
                raise self.error
            return LLMResponse(content="OK")

        def stream(self, request):
            raise NotImplementedError

    ok = probe_model(Client(), reasoning_effort=E.LOW)
    assert ok.succeeded and ok.error_type is None
    assert requests[0].reasoning_effort is E.LOW
    assert requests[0].max_transport_attempts == 1

    failed = probe_model(
        Client(LLMProviderError("nope", error_type="rate_limit", retryable=True, status_code=429)),
        reasoning_effort=None,
    )
    assert not failed.succeeded
    assert failed.to_dict()["error_type"] == "rate_limit"
    assert failed.retryable and failed.status_code == 429


def test_factory_builds_a_chat_client_with_the_vendor_dialect() -> None:
    sent = []

    def transport(request, _timeout):
        sent.append(request)
        return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode()

    client = build_provider_client(
        ProviderClientSettings(
            provider="openai",
            deployment_id="conn-1@3",
            base_url="https://api.openai.com/v1",
            model="gpt-6.1-sol",
            api_key="sk-user",
            reasoning_effort="high",
        ),
        transport=transport,
    )
    client.complete(
        LLMRequest(
            messages=[{"role": "user", "content": "q"}],
            temperature=0,
            max_tokens=10,
            output_schema={"type": "object", "properties": {}, "additionalProperties": False},
            structured_output_policy={"allow_json_object_local_gate": True},
        )
    )
    body = json.loads(sent[0].data)
    assert body["reasoning_effort"] == "high"
    assert body["max_completion_tokens"] == 10
    assert "temperature" not in body
    assert body["response_format"] == {"type": "json_object"}
    assert "sk-user" not in repr(client.config)
