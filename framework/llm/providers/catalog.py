"""Supported model vendors and what is known about their models.

Each vendor entry says how to reach it (default address, wire protocol, how to
list models) and how it spells the optional request fields. The known-model
table fills in facts the vendor's model list does not return; anything absent
there must come from the live model list or from the user.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from framework.llm.context.anthropic import AnthropicThinkingOff
from framework.llm.context.openai import (
    MaxOutputTokensField,
    OpenAIChatDialect,
    TemperaturePolicy,
)
from framework.llm.models.reasoning import (
    EFFORT_ORDER,
    ReasoningEffort,
    ReasoningParameterStyle,
    default_effort,
    ordered_efforts,
)


class ProviderKind(str, Enum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"
    DASHSCOPE = "dashscope"
    OPENROUTER = "openrouter"
    OPENAI_COMPATIBLE = "openai_compatible"

    @classmethod
    def parse(cls, value: object) -> ProviderKind:
        if isinstance(value, ProviderKind):
            return value
        try:
            return cls(str(value).strip().casefold())
        except ValueError as exc:
            raise ValueError(f"unknown model provider: {value!r}") from exc


class WireProtocol(str, Enum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"


@dataclass(frozen=True)
class KnownModel:
    model_id: str
    context_window_tokens: int
    max_output_tokens: int
    reasoning_efforts: tuple[ReasoningEffort, ...]
    # The tier preselected for new connections: medium when offered.
    default_reasoning_effort: ReasoningEffort | None = None
    thinking_off: AnthropicThinkingOff | None = None
    # Overrides the vendor's parameter style for models that spell reasoning differently.
    reasoning_parameter: ReasoningParameterStyle | None = None
    source: str = ""

    def __post_init__(self) -> None:
        efforts = ordered_efforts(self.reasoning_efforts)
        object.__setattr__(self, "reasoning_efforts", efforts)
        if self.default_reasoning_effort is None and efforts:
            object.__setattr__(self, "default_reasoning_effort", default_effort(efforts))
        if self.default_reasoning_effort is not None and self.default_reasoning_effort not in efforts:
            raise ValueError(f"{self.model_id}: default effort is not among its efforts")
        if self.thinking_off is not None and ReasoningEffort.OFF not in efforts:
            raise ValueError(f"{self.model_id}: thinking_off requires the off effort")


@dataclass(frozen=True)
class ProviderSpec:
    kind: ProviderKind
    display_name: str
    protocol: WireProtocol
    default_base_url: str | None
    reasoning_parameter: ReasoningParameterStyle
    # Tiers offered for a model the known-model table does not describe.
    generic_reasoning_efforts: tuple[ReasoningEffort, ...]
    max_output_tokens_field: MaxOutputTokensField = MaxOutputTokensField.MAX_TOKENS
    temperature_policy: TemperaturePolicy = TemperaturePolicy.SEND
    known_models: tuple[KnownModel, ...] = ()

    @property
    def base_url_required(self) -> bool:
        return self.default_base_url is None

    @property
    def reasoning_parameter_selectable(self) -> bool:
        return self.kind is ProviderKind.OPENAI_COMPATIBLE

    def known_model(self, model_id: str) -> KnownModel | None:
        wanted = model_id.strip()
        return next((model for model in self.known_models if model.model_id == wanted), None)

    def efforts_for(
        self,
        model_id: str,
        *,
        reasoning_parameter: ReasoningParameterStyle | None = None,
    ) -> tuple[ReasoningEffort, ...]:
        known = self.known_model(model_id)
        if known is not None:
            return known.reasoning_efforts
        style = self.reasoning_parameter_for(model_id, selected=reasoning_parameter)
        if style is ReasoningParameterStyle.NONE:
            return ()
        return self.generic_reasoning_efforts or GENERIC_EFFORTS[style]

    def reasoning_parameter_for(
        self,
        model_id: str,
        *,
        selected: ReasoningParameterStyle | None = None,
    ) -> ReasoningParameterStyle:
        if self.reasoning_parameter_selectable:
            return ReasoningParameterStyle.parse(selected or ReasoningParameterStyle.NONE)
        known = self.known_model(model_id)
        if known is not None and known.reasoning_parameter is not None:
            return known.reasoning_parameter
        return self.reasoning_parameter

    def chat_dialect(
        self,
        model_id: str,
        *,
        reasoning_parameter: ReasoningParameterStyle | None = None,
    ) -> OpenAIChatDialect:
        if self.protocol is not WireProtocol.OPENAI_CHAT:
            raise ValueError(f"{self.kind.value} does not use the Chat Completions protocol")
        return OpenAIChatDialect(
            reasoning_parameter=self.reasoning_parameter_for(model_id, selected=reasoning_parameter),
            max_output_tokens_field=self.max_output_tokens_field,
            temperature_policy=self.temperature_policy,
        )


_ALL = EFFORT_ORDER
_OFF_LOW_TO_MAX = (
    ReasoningEffort.OFF,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH,
    ReasoningEffort.XHIGH,
    ReasoningEffort.MAX,
)
_LOW_TO_MAX = _OFF_LOW_TO_MAX[1:]
_DEEPSEEK_TIERS = (ReasoningEffort.OFF, ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MAX)
# On/off only: MAX means "thinking on, model's own depth".
_THINKING_TOGGLE = (ReasoningEffort.OFF, ReasoningEffort.MAX)

# Tiers a self-described OpenAI-compatible endpoint may be asked for, per parameter style.
GENERIC_EFFORTS: dict[ReasoningParameterStyle, tuple[ReasoningEffort, ...]] = {
    ReasoningParameterStyle.NONE: (),
    ReasoningParameterStyle.OPENAI_REASONING_EFFORT: _ALL,
    ReasoningParameterStyle.OPENROUTER_REASONING: _ALL,
    ReasoningParameterStyle.DEEPSEEK_THINKING: _DEEPSEEK_TIERS,
    ReasoningParameterStyle.DASHSCOPE_THINKING: _ALL,
    ReasoningParameterStyle.DASHSCOPE_THINKING_TOGGLE: _THINKING_TOGGLE,
    ReasoningParameterStyle.ANTHROPIC_EFFORT: _LOW_TO_MAX,
}

_OPENAI_MODELS_SOURCE = "https://developers.openai.com/api/docs/models"
_ANTHROPIC_MODELS_SOURCE = "https://platform.claude.com/docs/en/about-claude/models/overview"
_DEEPSEEK_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing"
_DASHSCOPE_DEEPSEEK_SOURCE = "https://help.aliyun.com/zh/model-studio/deepseek-v4-flash"


def _openai(model_id: str, efforts: tuple[ReasoningEffort, ...]) -> KnownModel:
    return KnownModel(model_id, 1_050_000, 128_000, efforts, source=_OPENAI_MODELS_SOURCE)


def _claude(
    model_id: str,
    *,
    context: int = 1_000_000,
    output: int = 128_000,
    thinking_off: AnthropicThinkingOff | None,
) -> KnownModel:
    efforts = _OFF_LOW_TO_MAX if thinking_off is not None else _LOW_TO_MAX
    return KnownModel(
        model_id,
        context,
        output,
        efforts,
        thinking_off=thinking_off,
        source=_ANTHROPIC_MODELS_SOURCE,
    )


def _deepseek(model_id: str, source: str) -> KnownModel:
    return KnownModel(model_id, 1_000_000, 393_216, _DEEPSEEK_TIERS, source=source)


PROVIDERS: dict[ProviderKind, ProviderSpec] = {
    ProviderKind.OPENAI: ProviderSpec(
        kind=ProviderKind.OPENAI,
        display_name="OpenAI",
        protocol=WireProtocol.OPENAI_CHAT,
        default_base_url="https://api.openai.com/v1",
        reasoning_parameter=ReasoningParameterStyle.OPENAI_REASONING_EFFORT,
        generic_reasoning_efforts=_ALL,
        max_output_tokens_field=MaxOutputTokensField.MAX_COMPLETION_TOKENS,
        temperature_policy=TemperaturePolicy.OMIT_WHEN_REASONING,
        known_models=(
            _openai("gpt-6-astra", _LOW_TO_MAX),
            _openai("gpt-6.1-sol", _LOW_TO_MAX),
            _openai("gpt-6.1-terra", _OFF_LOW_TO_MAX),
            _openai("gpt-6-luna", _OFF_LOW_TO_MAX),
        ),
    ),
    ProviderKind.ANTHROPIC: ProviderSpec(
        kind=ProviderKind.ANTHROPIC,
        display_name="Anthropic",
        protocol=WireProtocol.ANTHROPIC_MESSAGES,
        default_base_url="https://api.anthropic.com",
        reasoning_parameter=ReasoningParameterStyle.ANTHROPIC_EFFORT,
        generic_reasoning_efforts=_LOW_TO_MAX,
        temperature_policy=TemperaturePolicy.OMIT,
        known_models=(
            _claude("claude-fable-5-1", thinking_off=None),
            _claude("claude-opus-5-5", thinking_off=None),
            _claude("claude-opus-5", thinking_off=AnthropicThinkingOff.DISABLED),
            _claude("claude-opus-4-8", thinking_off=AnthropicThinkingOff.DISABLED),
            _claude("claude-sonnet-5-5", thinking_off=AnthropicThinkingOff.BETWEEN_TOOLS),
            _claude("claude-sonnet-5", thinking_off=AnthropicThinkingOff.DISABLED),
            _claude("claude-haiku-5-5", thinking_off=AnthropicThinkingOff.DISABLED),
        ),
    ),
    ProviderKind.DEEPSEEK: ProviderSpec(
        kind=ProviderKind.DEEPSEEK,
        display_name="DeepSeek",
        protocol=WireProtocol.OPENAI_CHAT,
        default_base_url="https://api.deepseek.com",
        reasoning_parameter=ReasoningParameterStyle.DEEPSEEK_THINKING,
        generic_reasoning_efforts=_DEEPSEEK_TIERS,
        known_models=(
            _deepseek("deepseek-flash", _DEEPSEEK_SOURCE),
            _deepseek("deepseek-v4-pro", _DEEPSEEK_SOURCE),
            _deepseek("deepseek-v4-flash", _DEEPSEEK_SOURCE),
        ),
    ),
    ProviderKind.DASHSCOPE: ProviderSpec(
        kind=ProviderKind.DASHSCOPE,
        display_name="通义千问（阿里云百炼）",
        protocol=WireProtocol.OPENAI_CHAT,
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        reasoning_parameter=ReasoningParameterStyle.DASHSCOPE_THINKING,
        generic_reasoning_efforts=_ALL,
        known_models=(
            # DeepSeek on 百炼 has no thinking budget, only enable_thinking:
            # https://help.aliyun.com/zh/model-studio/deep-thinking
            KnownModel(
                "deepseek-v4-flash",
                1_000_000,
                393_216,
                _THINKING_TOGGLE,
                reasoning_parameter=ReasoningParameterStyle.DASHSCOPE_THINKING_TOGGLE,
                source=_DASHSCOPE_DEEPSEEK_SOURCE,
            ),
        ),
    ),
    ProviderKind.OPENROUTER: ProviderSpec(
        kind=ProviderKind.OPENROUTER,
        display_name="OpenRouter",
        protocol=WireProtocol.OPENAI_CHAT,
        default_base_url="https://openrouter.ai/api/v1",
        reasoning_parameter=ReasoningParameterStyle.OPENROUTER_REASONING,
        generic_reasoning_efforts=_ALL,
    ),
    ProviderKind.OPENAI_COMPATIBLE: ProviderSpec(
        kind=ProviderKind.OPENAI_COMPATIBLE,
        display_name="自定义 OpenAI 兼容地址",
        protocol=WireProtocol.OPENAI_CHAT,
        default_base_url=None,
        reasoning_parameter=ReasoningParameterStyle.NONE,
        generic_reasoning_efforts=(),
    ),
}


def provider_spec(kind: ProviderKind | str) -> ProviderSpec:
    return PROVIDERS[ProviderKind.parse(kind)]


# Parameter styles a user may pick for a custom OpenAI-compatible endpoint.
SELECTABLE_REASONING_PARAMETERS: tuple[ReasoningParameterStyle, ...] = (
    ReasoningParameterStyle.NONE,
    ReasoningParameterStyle.OPENAI_REASONING_EFFORT,
    ReasoningParameterStyle.OPENROUTER_REASONING,
    ReasoningParameterStyle.DEEPSEEK_THINKING,
    ReasoningParameterStyle.DASHSCOPE_THINKING,
    ReasoningParameterStyle.DASHSCOPE_THINKING_TOGGLE,
)


__all__ = [
    "GENERIC_EFFORTS",
    "PROVIDERS",
    "SELECTABLE_REASONING_PARAMETERS",
    "AnthropicThinkingOff",
    "KnownModel",
    "ProviderKind",
    "ProviderSpec",
    "WireProtocol",
    "provider_spec",
]
