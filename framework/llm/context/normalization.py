from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Protocol

from framework.llm.models.request import LLMRequest
from framework.llm.structured_output.request_adapters import adapt_structured_output_request


@dataclass(frozen=True)
class NormalizedLLMRequest:
    request: LLMRequest
    payload: dict[str, Any]
    provider: str
    normalizer_revision: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "request", self.request.clone())
        object.__setattr__(self, "payload", deepcopy(self.payload))


class LLMRequestNormalizer(Protocol):
    def normalize(
        self,
        request: LLMRequest,
        *,
        provider: str,
        model: str,
    ) -> NormalizedLLMRequest:
        ...


class LLMRequestNormalizerRegistry:
    def __init__(self) -> None:
        self._normalizers: dict[tuple[str, str], LLMRequestNormalizer] = {}

    def register(
        self,
        *,
        provider: str,
        revision: str,
        normalizer: LLMRequestNormalizer,
    ) -> None:
        key = (_key_text(provider, field="provider"), _key_text(revision, field="revision"))
        if key in self._normalizers:
            raise ValueError(f"request normalizer is already registered: {key!r}")
        self._normalizers[key] = normalizer

    def resolve(self, *, provider: str, revision: str) -> LLMRequestNormalizer | None:
        return self._normalizers.get(
            (_key_text(provider, field="provider"), _key_text(revision, field="revision"))
        )


@dataclass(frozen=True)
class CanonicalLLMRequestNormalizer:
    revision: str = "canonical-request-v1"

    def normalize(
        self,
        request: LLMRequest,
        *,
        provider: str,
        model: str,
    ) -> NormalizedLLMRequest:
        normalized_request = request.clone(model=model)
        payload: dict[str, Any] = {
            "model": model,
            "messages": structured_output_projected_messages(normalized_request),
        }
        if normalized_request.temperature is not None:
            payload["temperature"] = normalized_request.temperature
        if normalized_request.max_tokens is not None:
            payload["max_tokens"] = normalized_request.max_tokens
        if normalized_request.tools:
            payload["tools"] = deepcopy(normalized_request.tools)
        projected_response_format = structured_output_projection_format(
            normalized_request
        )
        if projected_response_format is not None:
            payload["response_format"] = projected_response_format
        elif normalized_request.response_format is not None:
            payload["response_format"] = deepcopy(normalized_request.response_format)
        elif normalized_request.output_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": normalized_request.output_schema_name,
                    "schema": deepcopy(normalized_request.output_schema),
                    "strict": True,
                },
            }
        return NormalizedLLMRequest(
            request=normalized_request,
            payload=payload,
            provider=provider,
            normalizer_revision=self.revision,
        )


def structured_output_projection_format(
    request: LLMRequest,
) -> dict[str, Any] | None:
    adapted = adapt_structured_output_request(request)
    return adapted[1] if adapted is not None else None


def structured_output_projected_messages(request: LLMRequest) -> list[dict[str, Any]]:
    """Messages as the provider receives them once the projection is applied."""

    adapted = adapt_structured_output_request(request)
    if adapted is None:
        return deepcopy(request.messages)
    return adapted[0]


def _key_text(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip().casefold()
