from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, cast

from framework.llm.models.message import LLMMessage
from framework.llm.redaction.redactor import redact_sensitive_values
from framework.llm.structured_output.contracts import StructuredOutputContract
from framework.llm.structured_output.projection import (
    ProviderSchemaProjection,
    ProviderStructuredOutputPolicy,
)
from framework.shared.graph_identity import GraphExecutionIdentity


@dataclass(frozen=True)
class LLMRequest:
    messages: list[dict[str, Any] | LLMMessage]
    model: str | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_identity: GraphExecutionIdentity | None = None
    timeout_seconds: float | None = None
    max_transport_attempts: int | None = None
    response_format: str | dict[str, Any] | None = None
    output_schema: Any | None = None
    output_schema_name: str = "structured_output"
    structured_output_policy: ProviderStructuredOutputPolicy = field(
        default_factory=ProviderStructuredOutputPolicy
    )
    _output_schema_source: Any | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _structured_output_contract: StructuredOutputContract | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )
    _provider_schema_projection: ProviderSchemaProjection | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if self.max_transport_attempts is not None and (
            isinstance(self.max_transport_attempts, bool)
            or not isinstance(self.max_transport_attempts, int)
            or self.max_transport_attempts <= 0
        ):
            raise ValueError("max_transport_attempts must be a positive integer")
        object.__setattr__(
            self,
            "messages",
            [_message_to_dict(message) for message in self.messages],
        )
        if self.execution_identity is not None and not isinstance(
            self.execution_identity, GraphExecutionIdentity
        ):
            object.__setattr__(
                self,
                "execution_identity",
                GraphExecutionIdentity.from_dict(self.execution_identity),
            )
        policy = ProviderStructuredOutputPolicy.from_any(
            self.structured_output_policy
        )
        if self.execution_identity is not None:
            policy = policy.for_execution_identity(self.execution_identity)
        object.__setattr__(self, "structured_output_policy", policy)
        schema = self.output_schema
        if schema is None:
            return
        if isinstance(schema, dict):
            object.__setattr__(self, "output_schema", deepcopy(schema))
            return
        model_json_schema = getattr(schema, "model_json_schema", None)
        if not callable(model_json_schema):
            raise TypeError("output_schema must be an object or Pydantic model class")
        exported = model_json_schema()
        if not isinstance(exported, dict):
            raise TypeError("output_schema model_json_schema() must return an object")
        object.__setattr__(self, "_output_schema_source", schema)
        object.__setattr__(self, "output_schema", deepcopy(exported))

    def estimated_prompt_text(self) -> str:
        return "\n".join(str(message.get("content") or "") for message in self._message_dicts())

    def structured_output_schema_source(self) -> Any | None:
        return self._output_schema_source or self.output_schema

    def structured_output_contract(self) -> StructuredOutputContract | None:
        return self._structured_output_contract

    def provider_schema_projection(self) -> ProviderSchemaProjection | None:
        return self._provider_schema_projection

    def with_structured_output_execution(
        self,
        *,
        contract: StructuredOutputContract,
        projection: ProviderSchemaProjection,
    ) -> LLMRequest:
        if self.output_schema is None:
            raise ValueError("structured-output execution requires output_schema")
        if projection.contract_digest != contract.schema_digest:
            raise ValueError("provider projection does not match structured-output contract")
        if projection.graph_scope != self.structured_output_policy.graph_scope:
            raise ValueError(
                "provider projection Graph scope does not match request authorization"
            )
        result = self.clone()
        object.__setattr__(result, "_structured_output_contract", contract)
        object.__setattr__(result, "_provider_schema_projection", projection)
        return result

    def clone(self, **changes: Any) -> LLMRequest:
        values: dict[str, Any] = {
            "messages": deepcopy(self.messages),
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "tools": deepcopy(self.tools),
            "metadata": deepcopy(self.metadata),
            "execution_identity": self.execution_identity,
            "timeout_seconds": self.timeout_seconds,
            "max_transport_attempts": self.max_transport_attempts,
            "response_format": deepcopy(self.response_format),
            "output_schema": self.structured_output_schema_source(),
            "output_schema_name": self.output_schema_name,
            "structured_output_policy": self.structured_output_policy,
        }
        values.update(changes)
        result = LLMRequest(**values)
        schema_changed = bool(
            {"output_schema", "output_schema_name"}.intersection(changes)
        )
        policy_changed = "structured_output_policy" in changes
        if not schema_changed and self._structured_output_contract is not None:
            object.__setattr__(
                result,
                "_structured_output_contract",
                self._structured_output_contract,
            )
        if (
            not schema_changed
            and not policy_changed
            and self._provider_schema_projection is not None
        ):
            object.__setattr__(
                result,
                "_provider_schema_projection",
                self._provider_schema_projection,
            )
        return result

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": [dict(message) for message in self._message_dicts()],
            "tools": deepcopy(self.tools),
            "metadata": dict(self.metadata),
        }
        if self.execution_identity is not None:
            payload["execution_identity"] = self.execution_identity.to_dict()
        if self.timeout_seconds is not None:
            payload["timeout_seconds"] = self.timeout_seconds
        if self.max_transport_attempts is not None:
            payload["max_transport_attempts"] = self.max_transport_attempts
        if self.model is not None:
            payload["model"] = self.model
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.response_format is not None:
            payload["response_format"] = deepcopy(self.response_format)
        if self.output_schema is not None:
            payload["output_schema"] = deepcopy(self.output_schema)
            payload["output_schema_name"] = self.output_schema_name
            payload["structured_output_policy"] = (
                self.structured_output_policy.to_dict()
            )
        if redact:
            return redact_sensitive_values(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LLMRequest:
        return cls(
            messages=list(payload.get("messages") or []),
            model=payload.get("model"),
            temperature=(
                float(payload["temperature"]) if payload.get("temperature") is not None else None
            ),
            max_tokens=int(payload["max_tokens"]) if payload.get("max_tokens") is not None else None,
            tools=list(payload.get("tools") or []),
            metadata=dict(payload.get("metadata") or {}),
            execution_identity=(
                GraphExecutionIdentity.from_dict(payload["execution_identity"])
                if payload.get("execution_identity") is not None
                else None
            ),
            response_format=deepcopy(payload.get("response_format")),
            output_schema=deepcopy(payload.get("output_schema")),
            output_schema_name=str(payload.get("output_schema_name") or "structured_output"),
            structured_output_policy=ProviderStructuredOutputPolicy.from_any(
                payload.get("structured_output_policy")
            ),
        )

    def _message_dicts(self) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], self.messages)


def _message_to_dict(message: dict[str, Any] | LLMMessage) -> dict[str, Any]:
    if isinstance(message, LLMMessage):
        return message.to_dict()
    return dict(message)
