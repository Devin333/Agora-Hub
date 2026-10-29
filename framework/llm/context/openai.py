from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from framework.llm.context.normalization import (
    NormalizedLLMRequest,
    structured_output_projected_messages,
    structured_output_projection_format,
)
from framework.llm.clients.tool_adapters import to_openai_tools
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


def build_openai_chat_payload(
    request: LLMRequest,
    *,
    model: str,
) -> dict[str, Any]:
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")
    payload: dict[str, Any] = {
        "model": model.strip(),
        "messages": structured_output_projected_messages(request),
    }
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if request.tools:
        payload["tools"] = to_openai_tools(request.tools)
    response_format = openai_response_format(request)
    if response_format is not None:
        payload["response_format"] = response_format
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
