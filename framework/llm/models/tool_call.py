from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from framework.llm.redaction.redactor import redact_sensitive_values


TOOL_CALL_ARGUMENT_ERROR_KEY = "argument_error"


@dataclass(frozen=True)
class LLMToolCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str | None = None
    provider_tool_call_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def with_invalid_arguments(
        cls,
        *,
        tool_call_id: str,
        tool_name: str,
        raw_arguments: str | None,
        error: str,
        provider_tool_call_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LLMToolCall:
        """A requested call whose arguments could not be decoded.

        The call stays in the response so that one malformed request rejects
        only itself; the sibling calls of the same model turn remain usable.
        """

        return cls(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            raw_arguments=raw_arguments,
            provider_tool_call_id=provider_tool_call_id,
            metadata={**dict(metadata or {}), TOOL_CALL_ARGUMENT_ERROR_KEY: error},
        )

    @property
    def argument_error(self) -> str | None:
        error = self.metadata.get(TOOL_CALL_ARGUMENT_ERROR_KEY)
        return str(error) if error else None

    def arguments_dict(self) -> dict[str, Any]:
        if self.arguments:
            return deepcopy(self.arguments)
        if self.raw_arguments:
            parsed = json.loads(self.raw_arguments)
            if not isinstance(parsed, dict):
                raise ValueError("tool call arguments must decode to an object")
            return parsed
        return {}

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload = {
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "arguments": deepcopy(self.arguments),
            "raw_arguments": self.raw_arguments,
            "provider_tool_call_id": self.provider_tool_call_id,
            "metadata": dict(self.metadata),
        }
        if redact:
            return redact_sensitive_values(payload)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | LLMToolCall) -> LLMToolCall:
        if isinstance(payload, LLMToolCall):
            return payload
        return cls(
            tool_call_id=str(payload.get("tool_call_id") or payload.get("id") or ""),
            tool_name=str(payload.get("tool_name") or payload.get("name") or ""),
            arguments=dict(payload.get("arguments") or {}),
            raw_arguments=(
                str(payload["raw_arguments"]) if payload.get("raw_arguments") is not None else None
            ),
            provider_tool_call_id=(
                str(payload["provider_tool_call_id"])
                if payload.get("provider_tool_call_id") is not None
                else None
            ),
            metadata=dict(payload.get("metadata") or {}),
        )

