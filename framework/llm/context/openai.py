from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from framework.llm.context.normalization import (
    NormalizedLLMRequest,
    structured_output_projected_messages,
    structured_output_projection_format,
)
from framework.llm.clients.tool_adapters import to_openai_tools
from framework.llm.models.reasoning import (
    ReasoningEffort,
    ReasoningParameterStyle,
    reasoning_is_active,
    reasoning_request_fields,
)
from framework.llm.models.request import LLMRequest


OPENAI_CHAT_NORMALIZER_REVISION = "openai-chat-completions-v1"


@dataclass(frozen=True)
class OpenAICompatibleRequestNormalizer:
    revision: str = OPENAI_CHAT_NORMALIZER_REVISION

    def normalize(
        self,
        request: LLMRequest,
        *,
        provider: str,
        model: str,
    ) -> NormalizedLLMRequest:
        normalized_request = request.clone(model=model)
        return NormalizedLLMRequest(
            request=normalized_request,
            payload=build_openai_chat_payload(normalized_request, model=model),
            provider=provider,
            normalizer_revision=self.revision,
        )


class TemperaturePolicy(str, Enum):
    """Whether the provider accepts ``temperature`` alongside the configured reasoning."""

    SEND = "send"
    OMIT_WHEN_REASONING = "omit_when_reasoning"
    OMIT = "omit"


class MaxOutputTokensField(str, Enum):
    MAX_TOKENS = "max_tokens"
    MAX_COMPLETION_TOKENS = "max_completion_tokens"


@dataclass(frozen=True)
class OpenAIChatDialect:
    """Provider-specific spelling of the optional Chat Completions fields."""

    reasoning_parameter: ReasoningParameterStyle = ReasoningParameterStyle.NONE
    max_output_tokens_field: MaxOutputTokensField = MaxOutputTokensField.MAX_TOKENS
    temperature_policy: TemperaturePolicy = TemperaturePolicy.SEND

    def __post_init__(self) -> None:
        style = ReasoningParameterStyle.parse(self.reasoning_parameter)
        if style is ReasoningParameterStyle.ANTHROPIC_EFFORT:
            raise ValueError("anthropic_effort is not a Chat Completions reasoning parameter")
        object.__setattr__(self, "reasoning_parameter", style)
        object.__setattr__(
            self, "max_output_tokens_field", MaxOutputTokensField(self.max_output_tokens_field)
        )
        object.__setattr__(self, "temperature_policy", TemperaturePolicy(self.temperature_policy))

    def sends_temperature(self, effort: ReasoningEffort | None) -> bool:
        if self.temperature_policy is TemperaturePolicy.OMIT:
            return False
        if self.temperature_policy is TemperaturePolicy.OMIT_WHEN_REASONING:
            return not reasoning_is_active(self.reasoning_parameter, effort)
        return True


def build_openai_chat_payload(
    request: LLMRequest,
    *,
    model: str,
    dialect: OpenAIChatDialect | None = None,
    default_reasoning_effort: ReasoningEffort | None = None,
) -> dict[str, Any]:
    """Chat Completions body; the request's effort wins over the deployment default."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    dialect = dialect or OpenAIChatDialect()
    effort = request.reasoning_effort or default_reasoning_effort
    payload: dict[str, Any] = {
        "model": model.strip(),
        "messages": structured_output_projected_messages(request),
    }
    if request.temperature is not None and dialect.sends_temperature(effort):
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload[dialect.max_output_tokens_field.value] = request.max_tokens
    if request.tools:
        payload["tools"] = to_openai_tools(request.tools)
    response_format = openai_response_format(request)
    if response_format is not None:
        payload["response_format"] = response_format
    payload.update(reasoning_request_fields(dialect.reasoning_parameter, effort))
    return payload


def openai_response_format(request: LLMRequest) -> dict[str, Any] | None:
    projected = structured_output_projection_format(request)
    if projected is not None:
        return projected
    if request.response_format is not None:
        if isinstance(request.response_format, str):
            return {"type": request.response_format}
        return deepcopy(request.response_format)
    if request.output_schema is not None:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": request.output_schema_name,
                "schema": deepcopy(request.output_schema),
                "strict": True,
            },
        }
    return None
