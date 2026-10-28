"""Express a provider schema projection on an OpenAI-style chat request.

A projection says *how* a deployment enforces a structured-output contract;
an adapter turns that decision into the request a provider actually receives.
Native modes hand the schema to the provider's constrained decoder. The
JSON-object local gate cannot: the provider only guarantees syntactically
valid JSON, so the adapter must tell the model which object to produce and
the local validator enforces the contract on the reply.

Both the client payload builder and the context-preflight normalizer apply
the same adapter, so token accounting always sees the request that is sent.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from framework.llm.structured_output.projection import (
    ProviderSchemaProjection,
    ProviderSchemaProjectionMode,
)

if TYPE_CHECKING:
    from framework.llm.models.request import LLMRequest


class StructuredOutputRequestAdapter(Protocol):
    mode: ProviderSchemaProjectionMode
    revision: str
    empty_content_retryable: bool

    def response_format(
        self,
        request: LLMRequest,
        projection: ProviderSchemaProjection,
    ) -> dict[str, Any]:
        ...

    def messages(
        self,
        request: LLMRequest,
        projection: ProviderSchemaProjection,
    ) -> list[dict[str, Any]]:
        ...


@dataclass(frozen=True)
class NativeJsonSchemaRequestAdapter:
    """Provider-enforced schema: the decoder itself is constrained."""

    mode: ProviderSchemaProjectionMode
    revision: str = "openai-json-schema-strict-v1"
    empty_content_retryable: bool = False

    def response_format(
        self,
        request: LLMRequest,
        projection: ProviderSchemaProjection,
    ) -> dict[str, Any]:
        if projection.provider_schema is None:
            raise ValueError("provider schema projection is missing its schema")
        return {
            "type": "json_schema",
            "json_schema": {
                "name": request.output_schema_name,
                "schema": deepcopy(projection.provider_schema),
                "strict": True,
            },
        }

    def messages(
        self,
        request: LLMRequest,
        projection: ProviderSchemaProjection,
    ) -> list[dict[str, Any]]:
        return deepcopy(request.messages)


JSON_OBJECT_SCHEMA_INSTRUCTION = (
    "Respond with exactly one JSON object and nothing else: no prose, no "
    "Markdown fences. The object must validate against this JSON Schema "
    "(draft 2020-12); every required property must be present and no other "
    "properties are allowed where additionalProperties is false.\n"
    "JSON Schema:\n"
)


@dataclass(frozen=True)
class JsonObjectLocalGateRequestAdapter:
    """Provider guarantees JSON syntax only; the schema travels in the prompt.

    Providers that offer JSON-object mode (DashScope, DeepSeek) refuse the
    request unless a message mentions JSON, and DeepSeek documents that the
    mode can return empty content, which is a transient provider outcome
    rather than a contract violation.
    """

    mode: ProviderSchemaProjectionMode = "json_object_local_gate"
    revision: str = "openai-json-object-schema-instruction-v1"
    empty_content_retryable: bool = True

    def response_format(
        self,
        request: LLMRequest,
        projection: ProviderSchemaProjection,
    ) -> dict[str, Any]:
        return {"type": "json_object"}

    def messages(
        self,
        request: LLMRequest,
        projection: ProviderSchemaProjection,
    ) -> list[dict[str, Any]]:
        contract = request.structured_output_contract()
        if contract is None:
            raise ValueError("JSON-object local gate requires a compiled contract")
        schema_text = json.dumps(
            contract.canonical_schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        instruction = {
            "role": "system",
            "content": JSON_OBJECT_SCHEMA_INSTRUCTION + schema_text,
        }
        messages = deepcopy(request.messages)
        # Keep the caller's system prompt first and the conversation intact:
        # the instruction joins the leading system block.
        insert_at = 0
        while insert_at < len(messages) and messages[insert_at].get("role") == "system":
            insert_at += 1
        messages.insert(insert_at, instruction)
        return messages


_ADAPTERS: dict[str, StructuredOutputRequestAdapter] = {
    "native_strict": NativeJsonSchemaRequestAdapter(mode="native_strict"),
    "json_object_local_gate": JsonObjectLocalGateRequestAdapter(),
}


def structured_output_request_adapter(
    mode: str,
) -> StructuredOutputRequestAdapter | None:
    """Return the adapter that can express ``mode``, or ``None``.

    ``constrained`` has no OpenAI-compatible wire form, so no adapter exists
    and callers must refuse the projection instead of sending a weaker one.
    """

    return _ADAPTERS.get(mode)


def supports_structured_output_projection(projection: ProviderSchemaProjection) -> bool:
    return structured_output_request_adapter(projection.mode) is not None


def require_structured_output_request_adapter(
    projection: ProviderSchemaProjection,
) -> StructuredOutputRequestAdapter:
    adapter = structured_output_request_adapter(projection.mode)
    if adapter is None:
        raise ValueError(
            f"no request adapter can express projection mode {projection.mode!r}"
        )
    return adapter


def adapt_structured_output_request(
    request: LLMRequest,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Return the messages and response_format for a projected request."""

    projection = request.provider_schema_projection()
    if projection is None:
        return None
    adapter = require_structured_output_request_adapter(projection)
    return adapter.messages(request, projection), adapter.response_format(request, projection)


__all__ = [
    "JSON_OBJECT_SCHEMA_INSTRUCTION",
    "JsonObjectLocalGateRequestAdapter",
    "NativeJsonSchemaRequestAdapter",
    "StructuredOutputRequestAdapter",
    "adapt_structured_output_request",
    "require_structured_output_request_adapter",
    "structured_output_request_adapter",
    "supports_structured_output_projection",
]
