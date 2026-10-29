"""Provider failure types and the redacted diagnostics attached to them.

Shared by every provider client so callers see one error vocabulary whatever
wire protocol produced the failure.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError

from framework.llm.redaction import REDACTED_VALUE, redact_sensitive_values


class LLMConfigurationError(RuntimeError):
    """Raised when LLM provider configuration is incomplete."""


class LLMProviderError(RuntimeError):
    """Raised when an LLM provider request fails."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        deployment_id: str | None = None,
        error_type: str = "unknown_llm_error",
        retryable: bool = False,
        status_code: int | None = None,
        attempts: int = 1,
        diagnostics: Iterable[dict[str, Any]] | None = None,
        response_fingerprint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.deployment_id = deployment_id
        self.error_type = error_type
        self.retryable = retryable
        self.status_code = status_code
        self.attempts = attempts
        self.diagnostics = tuple(dict(item) for item in (diagnostics or ()))
        self.response_fingerprint = response_fingerprint

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "message": str(self),
            "provider": self.provider,
            "model": self.model,
            "deployment_id": self.deployment_id,
            "error_type": self.error_type,
            "error_category": _canonical_error_category(self.error_type),
            "retryable": self.retryable,
            "status_code": self.status_code,
            "attempts": self.attempts,
        }
        if self.diagnostics:
            payload["diagnostics"] = [dict(item) for item in self.diagnostics]
        if self.response_fingerprint:
            payload["response_fingerprint"] = self.response_fingerprint
        if redact:
            return redact_sensitive_values(payload)
        return payload


class LLMProviderContextOverflow(LLMProviderError):
    """Stable non-transient provider context-window overflow."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        deployment_id: str | None = None,
        status_code: int | None = None,
        attempts: int = 1,
        provider_error_code: str | None = None,
        provider_reported_limit_tokens: int | None = None,
        provider_reported_usage_tokens: int | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            deployment_id=deployment_id,
            error_type="context_length",
            retryable=False,
            status_code=status_code,
            attempts=attempts,
        )
        self.provider_error_code = provider_error_code
        self.provider_reported_limit_tokens = provider_reported_limit_tokens
        self.provider_reported_usage_tokens = provider_reported_usage_tokens

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        payload = super().to_dict(redact=False)
        payload.update(
            {
                "provider_error_code": self.provider_error_code,
                "provider_reported_limit_tokens": self.provider_reported_limit_tokens,
                "provider_reported_usage_tokens": self.provider_reported_usage_tokens,
            }
        )
        if redact:
            return redact_sensitive_values(payload)
        return payload


def error_type_from_http_status(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request"
    if status_code in (401, 403):
        return "auth_error"
    if status_code == 404:
        return "unsupported_model"
    if status_code == 408:
        return "timeout"
    if status_code == 409:
        return "transient_network"
    if status_code == 413:
        return "context_length"
    if status_code == 429:
        return "rate_limit"
    if 500 <= status_code <= 599:
        return "server_error"
    if 400 <= status_code <= 499:
        return "invalid_request"
    return "unknown_llm_error"


def _canonical_error_category(error_type: str) -> str:
    aliases = {
        "rate_limited": "rate_limit",
        "provider_timeout": "timeout",
        "provider_connection_error": "transient_network",
        "temporary_provider_error": "transient_network",
        "provider_server_error": "server_error",
        "invalid_api_key": "auth_error",
        "invalid_request_schema": "invalid_request",
        "context_length_exceeded": "context_length",
        "invalid_model": "unsupported_model",
        "provider_response_shape_invalid": "schema_error",
        "provider_stream_chunk_invalid": "schema_error",
        "tool_call_parse_error": "schema_error",
        "stream_tool_call_parse_error": "schema_error",
        "structured_output_parse_error": "schema_error",
        "provider_schema_ineligible": "schema_error",
        "structured_output_schema_error": "schema_error",
        "structured_output_validation_error": "schema_error",
    }
    return aliases.get(error_type, error_type)


_MAX_PROVIDER_ERROR_BODY_BYTES = 64 * 1024
_MAX_PROVIDER_DIAGNOSTIC_CHARS = 2_000
_MAX_PROVIDER_FACT_CHARS = 512
_DIAGNOSTIC_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b[A-Za-z0-9_.-]*(?:api[_-]?key|authorization|client[_-]?secret|cookie|"
    r"credential|dsn|password|private[_-]?key|secret|token)[A-Za-z0-9_.-]*"
    r"[\"']?\s*[:=]\s*[^\r\n]*"
)
_DIAGNOSTIC_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_MAX_PROVIDER_TOKEN_DIAGNOSTIC = 2_000_000_000
CONTEXT_OVERFLOW_CODES = {
    "context_length_exceeded",
    "context_window_exceeded",
    "max_context_length_exceeded",
}
_PROVIDER_LIMIT_TOKEN_KEYS = (
    "context_limit_tokens",
    "context_window_tokens",
    "limit_tokens",
    "max_context_tokens",
    "max_context_length",
)
_PROVIDER_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "prompt_tokens",
    "requested_tokens",
    "total_tokens",
)


def read_bounded_http_error_body(exc: HTTPError) -> tuple[bytes, bool]:
    try:
        raw_body = exc.read(_MAX_PROVIDER_ERROR_BODY_BYTES + 1)
    except Exception:
        return b"", False
    if not isinstance(raw_body, bytes):
        return b"", False
    truncated = len(raw_body) > _MAX_PROVIDER_ERROR_BODY_BYTES
    return raw_body[:_MAX_PROVIDER_ERROR_BODY_BYTES], truncated


def decode_provider_error_payload(
    raw_body: bytes,
    *,
    truncated: bool,
) -> dict[str, Any] | None:
    if truncated or not raw_body:
        return None
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def provider_failure_diagnostic(
    *,
    payload: Any = None,
    raw_body: bytes = b"",
    raw_body_truncated: bool = False,
    stage: str,
) -> dict[str, Any]:
    diagnostic: dict[str, Any] = {"stage": stage}
    source = payload.get("error") if isinstance(payload, Mapping) else None
    if not isinstance(source, Mapping):
        source = payload if isinstance(payload, Mapping) else None
    if isinstance(source, Mapping):
        for target, keys in (
            ("provider_error_type", ("type", "error_type")),
            ("provider_error_code", ("code", "error_code")),
            ("provider_message", ("message", "detail", "error")),
        ):
            value = _first_provider_text(source, keys)
            if value is None and source is not payload and isinstance(payload, Mapping):
                value = _first_provider_text(payload, keys)
            if value is not None:
                safe, truncated = _bounded_redacted_text(value, _MAX_PROVIDER_FACT_CHARS)
                diagnostic[target] = safe
                diagnostic[f"{target}_truncated"] = truncated
                diagnostic[f"{target}_limit"] = _MAX_PROVIDER_FACT_CHARS
    elif isinstance(payload, Mapping):
        value = payload.get("error")
        if isinstance(value, str):
            safe, truncated = _bounded_redacted_text(value, _MAX_PROVIDER_FACT_CHARS)
            diagnostic["provider_message"] = safe
            diagnostic["provider_message_truncated"] = truncated
            diagnostic["provider_message_limit"] = _MAX_PROVIDER_FACT_CHARS

    excerpt_source: Any = payload
    excerpt_pre_redacted = False
    if excerpt_source is None and raw_body:
        excerpt_source = raw_body.decode("utf-8", errors="replace")
    if excerpt_source is not None:
        if isinstance(excerpt_source, (Mapping, list, tuple)):
            redacted = _redact_diagnostic_value(excerpt_source)
            excerpt_source = json.dumps(
                redacted,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            excerpt_pre_redacted = True
        excerpt, excerpt_truncated = _bounded_redacted_text(
            excerpt_source,
            _MAX_PROVIDER_DIAGNOSTIC_CHARS,
            pre_redacted=excerpt_pre_redacted,
        )
        if excerpt:
            diagnostic.update({
                "raw_excerpt": excerpt,
                "raw_excerpt_truncated": raw_body_truncated or excerpt_truncated,
                "raw_excerpt_limit": _MAX_PROVIDER_DIAGNOSTIC_CHARS,
            })
    return diagnostic


def network_failure_diagnostic(
    exc: TimeoutError | URLError | OSError,
    *,
    reason: Any,
) -> dict[str, Any]:
    source = reason if reason is not None else exc
    safe, truncated = _bounded_redacted_text(source, _MAX_PROVIDER_DIAGNOSTIC_CHARS)
    return {
        "stage": "network_transport",
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "network_reason": safe,
        "raw_excerpt": safe,
        "raw_excerpt_truncated": truncated,
        "raw_excerpt_limit": _MAX_PROVIDER_DIAGNOSTIC_CHARS,
    }


def _first_provider_text(source: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            text = str(value).strip()
            if text:
                return text
    return None


def _bounded_redacted_text(
    value: Any,
    limit: int,
    *,
    pre_redacted: bool = False,
) -> tuple[str, bool]:
    safe = str(value or "") if pre_redacted else _redact_diagnostic_text(value)
    safe = "".join(char for char in safe if char in "\n\t" or char.isprintable()).strip()
    return safe[:limit], len(safe) > limit


def _redact_diagnostic_value(value: Any) -> Any:
    redacted = redact_sensitive_values(value)
    if isinstance(redacted, Mapping):
        return {
            str(key): _redact_diagnostic_value(item)
            for key, item in redacted.items()
        }
    if isinstance(redacted, (list, tuple)):
        return [_redact_diagnostic_value(item) for item in redacted]
    if isinstance(redacted, str):
        return _redact_diagnostic_text(redacted)
    return redacted


def _redact_diagnostic_text(value: Any) -> str:
    safe = str(value or "")
    safe = _DIAGNOSTIC_PRIVATE_KEY.sub(REDACTED_VALUE, safe)
    safe = _DIAGNOSTIC_SECRET_ASSIGNMENT.sub(REDACTED_VALUE, safe)
    safe = str(redact_sensitive_values(safe))
    return safe


def payload_error_code(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    error = payload.get("error")
    raw_code = error.get("code") if isinstance(error, dict) else payload.get("code")
    if not isinstance(raw_code, str):
        return None
    code = raw_code.strip().casefold()
    if not code or len(code) > 128:
        return None
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in code):
        return None
    return code


def provider_overflow_token_diagnostics(
    payload: dict[str, Any] | None,
) -> tuple[int | None, int | None]:
    if payload is None:
        return None, None
    error = payload.get("error")
    sources: list[dict[str, Any]] = [payload]
    if isinstance(error, dict):
        sources.insert(0, error)
        details = error.get("details")
        if isinstance(details, dict):
            sources.insert(0, details)
    return (
        _first_bounded_token_value(sources, _PROVIDER_LIMIT_TOKEN_KEYS),
        _first_bounded_token_value(sources, _PROVIDER_USAGE_TOKEN_KEYS),
    )


def _first_bounded_token_value(
    sources: Iterable[dict[str, Any]],
    keys: tuple[str, ...],
) -> int | None:
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                continue
            if 0 <= value <= _MAX_PROVIDER_TOKEN_DIAGNOSTIC:
                return value
    return None


__all__ = [
    "CONTEXT_OVERFLOW_CODES",
    "LLMConfigurationError",
    "LLMProviderContextOverflow",
    "LLMProviderError",
    "decode_provider_error_payload",
    "error_type_from_http_status",
    "network_failure_diagnostic",
    "payload_error_code",
    "provider_failure_diagnostic",
    "provider_overflow_token_diagnostics",
    "read_bounded_http_error_body",
]
