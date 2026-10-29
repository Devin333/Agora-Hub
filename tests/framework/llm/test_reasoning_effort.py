from __future__ import annotations

import json
import pickle

import pytest

from framework.llm.cache import CacheMode, LLMCacheKeyFactory, LLMCachePolicy
from framework.llm.clients.config import load_openai_compatible_deployment
from framework.llm.clients.openai_compatible import (
    LLMConfigurationError,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from framework.llm.context import (
    MaxOutputTokensField,
    OpenAIChatDialect,
    TemperaturePolicy,
    build_openai_chat_payload,
)
from framework.llm.models import LLMRequest
from framework.llm.models.reasoning import (
    ReasoningEffort,
    ReasoningParameterStyle,
    default_effort,
    reasoning_request_fields,
)
from framework.shared.secret_text import SecretText

E = ReasoningEffort
S = ReasoningParameterStyle


@pytest.mark.parametrize(
    ("style", "effort", "expected"),
    [
        (S.NONE, E.HIGH, {}),
        (S.OPENAI_REASONING_EFFORT, E.MEDIUM, {"reasoning_effort": "medium"}),
        (S.OPENAI_REASONING_EFFORT, E.OFF, {"reasoning_effort": "none"}),
        (S.OPENAI_REASONING_EFFORT, E.MAX, {"reasoning_effort": "max"}),
        (S.OPENROUTER_REASONING, E.XHIGH, {"reasoning": {"effort": "xhigh"}}),
        (S.OPENROUTER_REASONING, E.OFF, {"reasoning": {"effort": "none"}}),
        (S.DEEPSEEK_THINKING, E.OFF, {"thinking": {"type": "disabled"}}),
        (S.DEEPSEEK_THINKING, E.MINIMAL, {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
        (S.DEEPSEEK_THINKING, E.MEDIUM, {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}),
        (S.DEEPSEEK_THINKING, E.MAX, {"thinking": {"type": "enabled"}, "reasoning_effort": "max"}),
        (S.DASHSCOPE_THINKING, E.OFF, {"enable_thinking": False}),
        (S.DASHSCOPE_THINKING, E.MEDIUM, {"enable_thinking": True, "thinking_budget": 8192}),
        (S.DASHSCOPE_THINKING, E.MAX, {"enable_thinking": True}),
        (S.DASHSCOPE_THINKING_TOGGLE, E.OFF, {"enable_thinking": False}),
        (S.DASHSCOPE_THINKING_TOGGLE, E.MAX, {"enable_thinking": True}),
    ],
)
def test_each_style_spells_the_tier_as_its_vendor_expects(style, effort, expected) -> None:
    assert reasoning_request_fields(style, effort) == expected


def test_no_effort_sends_nothing_and_anthropic_is_left_to_its_client() -> None:
    assert reasoning_request_fields(S.OPENAI_REASONING_EFFORT, None) == {}
    with pytest.raises(ValueError):
        reasoning_request_fields(S.ANTHROPIC_EFFORT, E.LOW)


def test_default_effort_prefers_medium_then_the_next_stronger_tier() -> None:
    assert default_effort(tuple(E)) is E.MEDIUM
    assert default_effort((E.OFF, E.LOW, E.HIGH, E.MAX)) is E.HIGH
    assert default_effort((E.OFF, E.LOW)) is E.LOW


def test_request_effort_round_trips_and_rejects_unknown_tiers() -> None:
    request = LLMRequest(messages=[{"role": "user", "content": "q"}], reasoning_effort="High")
    assert request.reasoning_effort is E.HIGH
    assert request.to_dict()["reasoning_effort"] == "high"
    assert LLMRequest.from_dict(request.to_dict()).reasoning_effort is E.HIGH
    assert request.clone().reasoning_effort is E.HIGH
    assert request.clone(reasoning_effort=None).reasoning_effort is None
    with pytest.raises(ValueError):
        LLMRequest(messages=[], reasoning_effort="extreme")


def test_payload_maps_effort_and_drops_temperature_while_reasoning() -> None:
    dialect = OpenAIChatDialect(
        reasoning_parameter=S.OPENAI_REASONING_EFFORT,
        max_output_tokens_field=MaxOutputTokensField.MAX_COMPLETION_TOKENS,
        temperature_policy=TemperaturePolicy.OMIT_WHEN_REASONING,
    )
    request = LLMRequest(messages=[{"role": "user", "content": "q"}], temperature=0.0, max_tokens=64)

    reasoning = build_openai_chat_payload(
        request, model="m", dialect=dialect, default_reasoning_effort=E.LOW
    )
    assert reasoning["reasoning_effort"] == "low"
    assert reasoning["max_completion_tokens"] == 64
    assert "max_tokens" not in reasoning and "temperature" not in reasoning

    off = build_openai_chat_payload(
        request.clone(reasoning_effort=E.OFF), model="m", dialect=dialect, default_reasoning_effort=E.LOW
    )
    assert off["reasoning_effort"] == "none"
    assert off["temperature"] == 0.0


def test_default_dialect_keeps_the_existing_payload() -> None:
    request = LLMRequest(
        messages=[{"role": "user", "content": "q"}], temperature=0.2, max_tokens=8, reasoning_effort=E.HIGH
    )
    assert build_openai_chat_payload(request, model="m") == {
        "model": "m",
        "messages": [{"role": "user", "content": "q"}],
        "temperature": 0.2,
        "max_tokens": 8,
    }


def test_cache_key_separates_reasoning_tiers() -> None:
    def request(effort):
        return LLMRequest(
            messages=[{"role": "user", "content": "q"}],
            reasoning_effort=effort,
            metadata={
                "task_type": "classify",
                "llm_cache": {
                    "scope": {"tenant_id": "t", "project_id": "p", "policy_scope": "v1"},
                    "dependencies": {"prompt_revision": "r1"},
                },
            },
        )

    policy = LLMCachePolicy(
        mode=CacheMode.READ_WRITE,
        cacheable_task_types=("classify",),
        required_dependencies=("prompt_revision",),
    )
    factory = LLMCacheKeyFactory(secret="0123456789abcdef")

    def key(effort):
        built = request(effort)
        return factory.build(
            request=built,
            context=policy.evaluate(built).context,
            deployment_id="d",
            provider="p",
            model="m",
        )

    assert key(E.LOW) != key(E.HIGH)
    assert key(E.LOW) == key(E.LOW)


def test_config_takes_exactly_one_credential_source_and_hides_the_key() -> None:
    with pytest.raises(LLMConfigurationError):
        OpenAICompatibleConfig(provider="p", base_url="https://x/v1", model="m")
    with pytest.raises(LLMConfigurationError):
        OpenAICompatibleConfig(
            provider="p", base_url="https://x/v1", model="m", api_key_env="K", api_key="sk-secret-value"
        )
    config = OpenAICompatibleConfig(provider="p", base_url="https://x/v1", model="m", api_key="sk-secret-value")
    assert "sk-secret-value" not in repr(config)
    assert config.resolve_api_key() == "sk-secret-value"


def test_client_sends_the_connection_key_and_effort() -> None:
    sent = []

    def transport(http_request, _timeout):
        sent.append(http_request)
        return json.dumps(
            {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}], "usage": {}}
        ).encode("utf-8")

    client = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            provider="deepseek",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-pro",
            api_key="sk-user-key",
            dialect=OpenAIChatDialect(reasoning_parameter=S.DEEPSEEK_THINKING),
            reasoning_effort=E.MEDIUM,
        ),
        transport=transport,
    )
    client.complete(LLMRequest(messages=[{"role": "user", "content": "q"}]))

    assert sent[0].get_header("Authorization") == "Bearer sk-user-key"
    body = json.loads(sent[0].data)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "high"


def test_models_yaml_deployment_reads_the_reasoning_dialect(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
model_groups:
  answer:
    deployments:
      - deployment_id: answer-v1
        provider: openai-compatible
        model: gpt-6.1-sol
        api_base: https://api.openai.com/v1
        api_key_env: OPENAI_API_KEY
        reasoning_parameter: openai_reasoning_effort
        reasoning_effort: low
        max_output_tokens_field: max_completion_tokens
        temperature_policy: omit_when_reasoning
routes:
  answer:
    model_group: answer
""",
        encoding="utf-8",
    )
    deployment = load_openai_compatible_deployment(path, route_id="answer", apply_environment_overrides=False)
    assert deployment.config.reasoning_effort is E.LOW
    assert deployment.config.dialect == OpenAIChatDialect(
        reasoning_parameter=S.OPENAI_REASONING_EFFORT,
        max_output_tokens_field=MaxOutputTokensField.MAX_COMPLETION_TOKENS,
        temperature_policy=TemperaturePolicy.OMIT_WHEN_REASONING,
    )


def test_models_yaml_rejects_an_effort_without_a_parameter_style(tmp_path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
model_groups:
  answer:
    deployments:
      - deployment_id: answer-v1
        provider: openai-compatible
        model: m
        api_base: https://llm.example/v1
        api_key_env: OPENAI_API_KEY
        reasoning_effort: low
routes:
  answer:
    model_group: answer
""",
        encoding="utf-8",
    )
    with pytest.raises(LLMConfigurationError):
        load_openai_compatible_deployment(path, route_id="answer", apply_environment_overrides=False)


def test_secret_text_never_prints_or_pickles_its_value() -> None:
    secret = SecretText("  sk-abc  ")
    assert secret.reveal() == "sk-abc"
    assert "sk-abc" not in repr(secret) and "sk-abc" not in str(secret)
    with pytest.raises(TypeError):
        pickle.dumps(secret)
    with pytest.raises(ValueError):
        SecretText("   ")
