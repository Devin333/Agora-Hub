from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import sys
import re
from typing import Any

from framework.shared.redaction import redact_sensitive_values
from scripts.local_desktop.common import DesktopOperationError, OperationResult
from scripts.local_desktop.lifecycle_cancellation import (
    MAX_STARTUP_CONTROL_BYTES as _MAX_STARTUP_CONTROL_BYTES,
    parse_startup_control_message as _parse_startup_control_message,
)

MAX_PROTOCOL_BYTES = 64 * 1024
MAX_STARTUP_CONTROL_BYTES = _MAX_STARTUP_CONTROL_BYTES
parse_startup_control_message = _parse_startup_control_message

_MAX_DEPTH = 7
_MAX_ITEMS = 50
_MAX_STRING = 1024
_MAX_PUBLIC_CONFIG_URL = 2048
_PUBLIC_CONFIG_URL_KEYS = frozenset({"api_url", "base_url"})


@dataclass(frozen=True)
class CommandResponse:
    ok: bool
    command: str
    state: str
    code: str
    message: str
    data: Mapping[str, Any]
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "ok": self.ok,
            "command": self.command,
            "state": self.state,
            "code": self.code,
            "message": self.message,
            "data": dict(self.data),
        }
        bounded = _bounded(payload)
        redacted = _bounded(redact_sensitive_values(bounded))
        restored = _restore_safe_confirmation_tokens(redacted, bounded)
        return _restore_safe_clear_fields(restored, bounded, command=self.command)


def project_operation(
    result: OperationResult,
    *,
    command: str,
    canonical_origin: str,
    instance_key: str,
    api_base_url: str,
    extra: Mapping[str, Any] | None = None,
) -> CommandResponse:
    details = dict(result.details or {})
    data = {
        **details,
        **dict(extra or {}),
        "canonical_origin": canonical_origin,
        "api_base_url": api_base_url,
        "instance_key": instance_key,
    }
    return CommandResponse(
        ok=bool(result.ok),
        command=command,
        state=str(result.status or "UNKNOWN"),
        code=str(result.reason_code or "desktop_operation_unknown"),
        message=str(result.message or ""),
        data=data,
        exit_code=int(result.exit_code if not result.ok else 0),
    )


def project_error(
    error: Exception,
    *,
    command: str,
    canonical_origin: str | None = None,
    instance_key: str | None = None,
    api_base_url: str | None = None,
) -> CommandResponse:
    if isinstance(error, DesktopOperationError):
        code = error.reason_code
        message = str(error)
        details = error.details
        exit_code = error.exit_code
    else:
        code = str(getattr(error, "reason_code", "desktop_app_internal_error"))
        message = str(error) or "desktop app command failed"
        details = {}
        exit_code = int(getattr(error, "exit_code", 2))
    return CommandResponse(
        ok=False,
        command=command,
        state="ERROR",
        code=code,
        message=message,
        data={
            "canonical_origin": canonical_origin,
            "api_base_url": api_base_url,
            "instance_key": instance_key,
            "details": details,
        },
        exit_code=exit_code if exit_code else 2,
    )


_SAFE_CONFIRMATION_TOKEN = re.compile(
    r"^(?:desktop-config:[0-9a-f]{32}:[0-9a-f]{16}|restore-personal-memory:[A-Za-z0-9_.-]{1,160})$"
)


def _restore_safe_confirmation_tokens(redacted: Any, original: Any, *, parent_key: str | None = None) -> Any:
    if isinstance(redacted, Mapping) and isinstance(original, Mapping):
        restored: dict[str, Any] = {}
        for key, value in redacted.items():
            source = original.get(key)
            if key == "confirmation_token" and isinstance(source, str) and _SAFE_CONFIRMATION_TOKEN.fullmatch(source):
                restored[str(key)] = source
            elif key == "credential_configured" and parent_key == "llm" and (source is None or isinstance(source, bool)):
                restored[str(key)] = source
            else:
                restored[str(key)] = _restore_safe_confirmation_tokens(value, source, parent_key=str(key))
        return restored
    if isinstance(redacted, list) and isinstance(original, list):
        return [
            _restore_safe_confirmation_tokens(value, original[index] if index < len(original) else None, parent_key=parent_key)
            for index, value in enumerate(redacted)
        ]
    return redacted



def read_bounded_stdin_json(*, stream: Any | None = None) -> dict[str, Any]:
    """Read exactly one bounded JSON object; duplicate keys and trailing values fail closed."""
    source = stream if stream is not None else sys.stdin.buffer
    raw = source.read(MAX_PROTOCOL_BYTES + 1)
    if not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode("utf-8")
    if not raw or len(raw) > MAX_PROTOCOL_BYTES:
        raise DesktopOperationError("desktop_data_clear_protocol_invalid", "bounded stdin JSON is missing or too large")
    try:
        text = bytes(raw).decode("utf-8", errors="strict")
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result
        value = json.loads(text, object_pairs_hook=pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DesktopOperationError("desktop_data_clear_protocol_invalid", "bounded stdin must contain exactly one valid JSON object") from exc
    if not isinstance(value, dict):
        raise DesktopOperationError("desktop_data_clear_protocol_invalid", "bounded stdin JSON must be an object")
    return value


def _restore_safe_clear_fields(redacted: Any, original: Any, *, command: str) -> Any:
    if not isinstance(redacted, Mapping) or not isinstance(original, Mapping):
        return redacted
    restored = dict(redacted)
    data = restored.get("data")
    source = original.get("data")
    if not isinstance(data, Mapping) or not isinstance(source, Mapping):
        return restored
    projected = dict(data)
    if command == "data-clear-preview":
        preview = source.get("previewId")
        if isinstance(preview, str) and re.fullmatch(r"[A-Za-z0-9_-]{20,256}", preview):
            projected["previewId"] = preview
    if command in {"data-clear-apply", "data-clear-status", "data-clear-finalize"}:
        operation = source.get("operationId")
        if isinstance(operation, str) and re.fullmatch(r"[0-9a-f]{32}", operation):
            projected["operationId"] = operation
    if command == "data-clear-apply":
        capability = source.get("finalizerCapability")
        if isinstance(capability, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}", capability):
            projected["finalizerCapability"] = capability
        if isinstance(source.get("exitRequired"), bool):
            projected["exitRequired"] = source["exitRequired"]
    restored["data"] = projected
    return restored

def _bounded(value: Any, *, depth: int = 0, parent_key: str | None = None) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        limit = _MAX_PUBLIC_CONFIG_URL if parent_key in _PUBLIC_CONFIG_URL_KEYS else _MAX_STRING
        return value if len(value) <= limit else value[: limit - 14] + "...<truncated>"
    if depth >= _MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, key in enumerate(sorted(value, key=lambda item: str(item))):
            if index >= _MAX_ITEMS:
                result["<truncated>"] = len(value) - _MAX_ITEMS
                break
            result[str(key)] = _bounded(value[key], depth=depth + 1, parent_key=str(key))
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = list(value)
        result = [_bounded(item, depth=depth + 1, parent_key=parent_key) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            result.append({"<truncated>": len(items) - _MAX_ITEMS})
        return result
    return _bounded(str(value), depth=depth + 1, parent_key=parent_key)


__all__ = ["CommandResponse", "MAX_PROTOCOL_BYTES", "project_error", "project_operation", "read_bounded_stdin_json"]
