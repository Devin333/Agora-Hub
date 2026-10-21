"""One deterministic, schema-bound LLM call shared by evaluation adapters.

Owns transport (an injected ``LLMClient``), strict JSON decoding, and pydantic
validation of the provider payload. Callers own prompts and map the two failure
kinds to their own error types.
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from framework.llm.clients.openai_compatible import LLMConfigurationError, LLMProviderError
from framework.llm.models import LLMClient, LLMRequest, LLMResponse
from framework.llm.structured_output import (
    LLMStructuredOutputError,
    ProviderStructuredOutputPolicy,
    decode_structured_output,
)

_T = TypeVar("_T", bound=BaseModel)


class StructuredCallProviderFailure(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class StructuredCallOutputFailure(Exception):
    pass


async def call_structured(
    client: LLMClient,
    *,
    system: str,
    user: str,
    schema: type[_T],
    schema_name: str,
    max_output_tokens: int,
    timeout_seconds: float,
    metadata: Mapping[str, Any],
    graph_scope: str = "research.evaluation",
) -> _T:
    request = LLMRequest(
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0,
        max_tokens=max_output_tokens,
        timeout_seconds=timeout_seconds,
        metadata=dict(metadata),
        output_schema=schema,
        output_schema_name=schema_name,
        structured_output_policy=ProviderStructuredOutputPolicy(graph_scope=graph_scope),
    )
    try:
        response = await asyncio.to_thread(client.complete, request)
    except (LLMConfigurationError, LLMProviderError) as exc:
        raise StructuredCallProviderFailure(
            f"{schema_name} provider request failed",
            retryable=bool(getattr(exc, "retryable", False)),
        ) from exc
    normalized = LLMResponse.from_any(response)
    raw: Any = normalized.structured_output
    if not isinstance(raw, dict):
        try:
            raw = decode_structured_output(normalized.content or "")
        except LLMStructuredOutputError as exc:
            raise StructuredCallOutputFailure(f"{schema_name} output is not strict JSON") from exc
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        raise StructuredCallOutputFailure(f"{schema_name} output does not match its schema") from exc


__all__ = [
    "StructuredCallOutputFailure",
    "StructuredCallProviderFailure",
    "call_structured",
]
