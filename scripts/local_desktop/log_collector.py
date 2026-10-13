from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from framework.shared.redaction import redact_sensitive_values
from scripts.local_desktop.common import protect_private_path

MAX_RAW_LINE_BYTES = 64 * 1024
MAX_OUTPUT_LINE_BYTES = 4096
_SECRET_PATTERN = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|x-api-key|api[_-]?key|token|password|secret|credential|session)\b\s*[:=]\s*[^,;\s]+"
)
_ALLOWED_LOG_KEYS = frozenset(
    {
        "timestamp", "time", "level", "severity", "event", "event_type", "operation",
        "status", "reason_code", "error_type", "exception_type",
        "code", "http_status", "method", "route", "duration_ms", "retryable",
        "process", "role", "pid", "request_id", "run_id", "release_id",
    }
)
_EXCLUDED_LOG_KEYS = frozenset(
    {
        "prompt", "prompts", "message", "messages", "content", "body", "payload",
        "request", "response", "raw", "completion", "input", "output", "headers",
        "authorization", "cookie", "set-cookie", "token", "session", "password",
        "secret", "credential", "api_key", "apikey",
    }
)
_SAFE_OPERATIONAL_VALUE = re.compile(r"^[A-Za-z0-9_./:@+-]{1,256}$")
_SAFE_PLAIN_LINE = re.compile(
    r"(?i)^(?:(?:debug|info|warning|error|critical)\s+)?(?:level|severity|event|operation|status|reason_code|error_type|code|http_status|method|route|duration_ms|role|pid|request_id|run_id|release_id)=[A-Za-z0-9_./:-]+(?:\s+(?:event|operation|status|reason_code|error_type|code|http_status|method|route|duration_ms|role|pid|request_id|run_id|release_id)=[A-Za-z0-9_./:-]+)*$"
)


def _redact_line(value: bytes, *, truncated: bool) -> bytes:
    text = value.decode("utf-8", errors="replace").rstrip("\r\n")
    safe = _safe_log_text(text)
    if truncated:
        safe = f"{safe[:3500]} [line truncated by local desktop log collector]"
    encoded = safe.encode("utf-8", errors="replace")[:MAX_OUTPUT_LINE_BYTES]
    return encoded + b"\n"


def _safe_log_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, Mapping):
        safe = _operational_fields(parsed)
        return json.dumps(safe or {"event": "log_content_omitted"}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if _SECRET_PATTERN.search(text) or not _SAFE_PLAIN_LINE.fullmatch(text.strip()):
        return "event=log_content_omitted"
    return text.strip()


def _operational_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key).strip()
        normalized = key.casefold().replace("-", "_")
        if normalized in _EXCLUDED_LOG_KEYS or normalized not in _ALLOWED_LOG_KEYS:
            continue
        if isinstance(raw_value, bool) or raw_value is None:
            result[key] = raw_value
        elif isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            result[key] = raw_value
        elif isinstance(raw_value, str):
            candidate = str(redact_sensitive_values(raw_value)).strip()
            if candidate != raw_value.strip() or _SECRET_PATTERN.search(candidate):
                result[key] = "***REDACTED***"
            elif _SAFE_OPERATIONAL_VALUE.fullmatch(candidate):
                result[key] = candidate
    return result


def _read_bounded_line(stream: BinaryIO) -> tuple[bytes, bool] | None:
    value = stream.readline(MAX_RAW_LINE_BYTES + 1)
    if not value:
        return None
    truncated = len(value) > MAX_RAW_LINE_BYTES and not value.endswith(b"\n")
    if truncated:
        while True:
            remainder = stream.readline(MAX_RAW_LINE_BYTES + 1)
            if not remainder or remainder.endswith(b"\n"):
                break
        value = value[:MAX_RAW_LINE_BYTES]
    return value, truncated


def _rotate(path: Path, *, retain: int) -> None:
    for index in range(retain - 1, 0, -1):
        older = path.with_name(f"{path.name}.{index}")
        newer = path.with_name(f"{path.name}.{index + 1}")
        if older.exists():
            os.replace(older, newer)
    if path.exists():
        os.replace(path, path.with_name(f"{path.name}.1"))


def collect(path: Path, *, max_bytes: int, retain: int, stream: BinaryIO) -> None:
    resolved = path.expanduser().resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ValueError("log path cannot be a filesystem root")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    handle = resolved.open("ab", buffering=0)
    protect_private_path(resolved)
    role = resolved.name.split(".", 1)[0]
    handle.write(f"event=collector_started role={role}\n".encode("utf-8"))
    try:
        while True:
            item = _read_bounded_line(stream)
            if item is None:
                break
            payload = _redact_line(item[0], truncated=item[1])
            if handle.tell() + len(payload) > max_bytes:
                handle.close()
                _rotate(resolved, retain=retain)
                handle = resolved.open("ab", buffering=0)
                protect_private_path(resolved)
            handle.write(payload)
        handle.write(f"event=collector_stream_closed role={role}\n".encode("utf-8"))
    finally:
        handle.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True)
    parser.add_argument("--max-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--retain", type=int, default=5)
    args = parser.parse_args(argv)
    if not 1024 <= args.max_bytes <= 1024 * 1024 * 1024 or not 1 <= args.retain <= 20:
        return 2
    try:
        collect(Path(args.path), max_bytes=args.max_bytes, retain=args.retain, stream=sys.stdin.buffer)
    except (OSError, ValueError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
