"""Port for the language model that drafts grounded paper answers.

The answer generator owns the prompt and the abstention protocol; this port only
transports one request to a model and returns its raw text plus usage. Every way the
model can fail to return text is a typed ``PaperAnswerModelError``; callers never
receive an empty or fabricated response in place of a failure.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from framework.llm.redaction import REDACTED_VALUE, redact_sensitive_values
from framework.llm.models.usage import TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity

PaperAnswerModelErrorCode = Literal[
    "provider_unavailable",
    "timeout",
    "configuration",
    "empty_output",
    "malformed_output",
]
_ERROR_CODES: frozenset[str] = frozenset(PaperAnswerModelErrorCode.__args__)  # type: ignore[attr-defined]

_DIAGNOSTIC_TEXT_LIMIT = 2048
_DIAGNOSTIC_COLLECTION_LIMIT = 20
_DIAGNOSTIC_DEPTH_LIMIT = 4
_TRACKED_DIAGNOSTIC_TEXT_KEYS = frozenset({"message", "output_excerpt", "raw_excerpt"})
_DIAGNOSTIC_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b[A-Za-z0-9_.-]*(?:api[_-]?key|authorization|client[_-]?secret|cookie|"
    r"credential|dsn|password|private[_-]?key|secret|token)[A-Za-z0-9_.-]*"
    r"[\"']?\s*[:=]\s*[^\r\n]*"
)
_DIAGNOSTIC_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)


class PaperAnswerModelError(RuntimeError):
    """The answer model did not produce a usable response."""

    def __init__(
        self,
        code: PaperAnswerModelErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"unknown paper answer model error code: {code!r}")
        safe_message, message_truncated = _safe_diagnostic_text(message)
        super().__init__(safe_message)
        self.code: PaperAnswerModelErrorCode = code
        self.retryable = bool(retryable)
        # Redacted diagnostics (provider error type, status, a bounded output excerpt)
        # so a failure can be attributed without re-running it.
        self.details = _safe_diagnostic_mapping(details)
        self.details.setdefault("message_truncated", message_truncated)
        self.details.setdefault("message_limit", _DIAGNOSTIC_TEXT_LIMIT)


def _safe_diagnostic_text(value: object) -> tuple[str, bool]:
    text = str(value or "")
    # Redact before truncating so a long credential cannot be cut into a form that
    # evades the shared redactor.
    text = _DIAGNOSTIC_PRIVATE_KEY.sub(REDACTED_VALUE, text)
    text = _DIAGNOSTIC_SECRET_ASSIGNMENT.sub(REDACTED_VALUE, text)
    text = str(redact_sensitive_values(text))
    text = "".join(char for char in text if char in "\n\t" or char.isprintable()).strip()
    return text[:_DIAGNOSTIC_TEXT_LIMIT], len(text) > _DIAGNOSTIC_TEXT_LIMIT


def _safe_diagnostic_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    redacted = redact_sensitive_values(dict(value))
    result: dict[str, Any] = {}
    for key, item in list(redacted.items())[:_DIAGNOSTIC_COLLECTION_LIMIT]:
        normalized_key = str(key)
        if isinstance(item, str):
            safe_text, truncated = _safe_diagnostic_text(item)
            result[normalized_key] = safe_text
            if normalized_key in _TRACKED_DIAGNOSTIC_TEXT_KEYS:
                result[f"{normalized_key}_truncated"] = truncated
                result[f"{normalized_key}_limit"] = _DIAGNOSTIC_TEXT_LIMIT
        else:
            result[normalized_key] = _safe_diagnostic_value(item, depth=1)
    return result


def _safe_diagnostic_value(value: Any, *, depth: int) -> Any:
    if depth > _DIAGNOSTIC_DEPTH_LIMIT:
        return "[diagnostic depth limit]"
    if isinstance(value, Mapping):
        return {
            str(key): _safe_diagnostic_value(item, depth=depth + 1)
            for key, item in list(value.items())[:_DIAGNOSTIC_COLLECTION_LIMIT]
        }
    if isinstance(value, (list, tuple)):
        return [
            _safe_diagnostic_value(item, depth=depth + 1)
            for item in list(value)[:_DIAGNOSTIC_COLLECTION_LIMIT]
        ]
    if isinstance(value, str):
        return _safe_diagnostic_text(value)[0]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_diagnostic_text(value)[0]


@dataclass(frozen=True)
class PaperAnswerModelRequest:
    system_prompt: str
    user_prompt: str
    execution_identity: GraphExecutionIdentity | None = None
    # None defers to the adapter's configured transport retry policy.
    max_transport_attempts: int | None = None

    def __post_init__(self) -> None:
        if not str(self.system_prompt).strip():
            raise ValueError("system_prompt is required")
        if not str(self.user_prompt).strip():
            raise ValueError("user_prompt is required")
        if self.max_transport_attempts is not None and (
            isinstance(self.max_transport_attempts, bool)
            or not isinstance(self.max_transport_attempts, int)
            or self.max_transport_attempts < 1
        ):
            raise ValueError("max_transport_attempts must be a positive integer")


@dataclass(frozen=True)
class PaperAnswerModelResponse:
    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)


@runtime_checkable
class PaperAnswerModelPort(Protocol):
    async def complete(self, request: PaperAnswerModelRequest) -> PaperAnswerModelResponse:
        """Return the model's raw text or raise ``PaperAnswerModelError``."""
        ...


__all__ = [
    "PaperAnswerModelError",
    "PaperAnswerModelErrorCode",
    "PaperAnswerModelPort",
    "PaperAnswerModelRequest",
    "PaperAnswerModelResponse",
]
