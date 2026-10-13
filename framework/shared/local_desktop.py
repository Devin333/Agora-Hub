from __future__ import annotations

import ipaddress
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

LOCAL_DESKTOP_PROFILE = "local-desktop"
LOCAL_DESKTOP_CONFIG_SCHEMA = "newsroom.local-desktop-config/v1"
DEFAULT_FRONTEND_HOST = "127.0.0.1"
DEFAULT_FRONTEND_PORT = 3000
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8000
MAX_LIFECYCLE_TIMEOUT_SECONDS = 300.0

_ENV_TO_FIELD = {
    "NEWSROOM_DEPLOYMENT_PROFILE": "deployment_profile",
    "NEWSROOM_DESKTOP_FRONTEND_HOST": "frontend_host",
    "NEWSROOM_DESKTOP_FRONTEND_PORT": "frontend_port",
    "NEWSROOM_DESKTOP_API_HOST": "api_host",
    "NEWSROOM_DESKTOP_API_PORT": "api_port",
    "NEWSROOM_CANONICAL_ORIGIN": "canonical_origin",
    "NEWSROOM_DESKTOP_CANONICAL_ORIGIN": "canonical_origin",
    "NEWSROOM_API_BASE_URL": "api_base_url",
    "NEWSROOM_DESKTOP_RUNTIME_ROOT": "runtime_root",
    "NEWSROOM_DESKTOP_RELEASE_ROOT": "release_root",
    "NEWSROOM_DESKTOP_BACKUP_ROOT": "backup_root",
    "NEWSROOM_DESKTOP_CONTROL_PATH": "control_path",
    "NEWSROOM_DESKTOP_PARSER_MODE": "parser_mode",
    "NEWS_RESEARCH_ROOT": "research_root",
    "NEWS_RESEARCH_ARTIFACT_ROOT": "artifact_root",
}
_ALLOWED_FIELDS = frozenset(_ENV_TO_FIELD.values())
_SECRET_NAME_TOKENS = ("secret", "token", "password", "credential", "api_key", "apikey", "cookie")
_NON_SECRET_BUDGET_FIELDS = frozenset({
    "timeout_seconds",
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_tokens",
    "estimated_input_tokens",
    "max_cost_usd",
    "estimated_cost_usd",
})


class LocalDesktopConfigError(ValueError):
    def __init__(self, reason_code: str, message: str, *, field_name: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.field_name = field_name

    def to_dict(self) -> dict[str, str]:
        payload = {"reason_code": self.reason_code, "message": str(self)}
        if self.field_name:
            payload["field"] = self.field_name
        return payload


class CapabilityState(str, Enum):
    UNKNOWN = "unknown"
    NO = "no"
    YES = "yes"


@dataclass(frozen=True)
class CapabilityProjection:
    capability_id: str
    required: bool
    configured: CapabilityState
    reachable: CapabilityState = CapabilityState.UNKNOWN
    verified: CapabilityState = CapabilityState.UNKNOWN
    reason_code: str | None = None
    checked_at: str | None = None
    remediation: tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.capability_id or len(self.capability_id) > 128:
            raise ValueError("capability_id must be a non-empty stable identifier")
        if self.verified is CapabilityState.YES and self.reachable is not CapabilityState.YES:
            raise ValueError("verified capability must also be reachable")
        if self.reachable is CapabilityState.YES and self.configured is not CapabilityState.YES:
            raise ValueError("reachable capability must also be configured")

    @property
    def available(self) -> bool:
        return self.configured is CapabilityState.YES and self.reachable is CapabilityState.YES and self.verified is CapabilityState.YES

    def to_frontend_dict(self, *, label: str, reason_code: str | None = None) -> dict[str, Any]:
        """Project tri-state diagnostics onto the strict frontend boolean capability DTO."""
        return {
            "id": self.capability_id,
            "label": label,
            "configured": self.configured is CapabilityState.YES,
            "reachable": self.reachable is CapabilityState.YES,
            "verified": self.verified is CapabilityState.YES,
            "reasonCode": reason_code or self.reason_code,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "required": self.required,
            "configured": self.configured.value,
            "reachable": self.reachable.value,
            "verified": self.verified.value,
            "available": self.available,
            "reason_code": self.reason_code,
            "checked_at": self.checked_at,
            "remediation": list(self.remediation),
            "details": _redact_details(self.details),
        }


@dataclass(frozen=True)
class ProbeBudget:
    timeout_seconds: float = 10.0
    max_requests: int = 3
    max_input_tokens: int = 256
    max_output_tokens: int = 64
    max_cost_usd: float = 0.02

    def __post_init__(self) -> None:
        if not 0 < self.timeout_seconds <= 60:
            raise ValueError("timeout_seconds must be in (0, 60]")
        if not 0 < self.max_requests <= 5:
            raise ValueError("max_requests must be in [1, 5]")
        if not 0 <= self.max_input_tokens <= 2048:
            raise ValueError("max_input_tokens must be in [0, 2048]")
        if not 0 <= self.max_output_tokens <= 512:
            raise ValueError("max_output_tokens must be in [0, 512]")
        if not 0 <= self.max_cost_usd <= 1:
            raise ValueError("max_cost_usd must be in [0, 1]")

    def to_dict(self) -> dict[str, int | float]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_requests": self.max_requests,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_cost_usd": self.max_cost_usd,
        }


@dataclass(frozen=True)
class LocalDesktopConfig:
    project_root: Path
    deployment_profile: str
    frontend_host: str
    frontend_port: int
    api_host: str
    api_port: int
    canonical_origin: str
    api_base_url: str
    runtime_root: Path
    release_root: Path
    backup_root: Path
    control_path: Path
    parser_mode: str
    config_schema: str = LOCAL_DESKTOP_CONFIG_SCHEMA
    research_root: Path | None = None
    artifact_root: Path | None = None

    @property
    def python_executable(self) -> Path:
        return self.project_root / ".venv" / "Scripts" / "python.exe"

    @property
    def frontend_root(self) -> Path:
        return self.project_root / "frontend"

    @property
    def runtime_config_path(self) -> Path:
        return self.runtime_root / "config.json"

    @property
    def instance_key(self) -> str:
        return f"{self.deployment_profile}:{self.canonical_origin}"

    def as_environment(self) -> dict[str, str]:
        values = {
            "NEWSROOM_DEPLOYMENT_PROFILE": self.deployment_profile,
            "NEWSROOM_DESKTOP_INSTANCE_KEY": self.instance_key,
            "NEWSROOM_DESKTOP_FRONTEND_HOST": self.frontend_host,
            "NEWSROOM_DESKTOP_FRONTEND_PORT": str(self.frontend_port),
            "NEWSROOM_DESKTOP_API_HOST": self.api_host,
            "NEWSROOM_DESKTOP_API_PORT": str(self.api_port),
            "NEWSROOM_CANONICAL_ORIGIN": self.canonical_origin,
            "NEWSROOM_DESKTOP_CANONICAL_ORIGIN": self.canonical_origin,
            "NEWSROOM_API_BASE_URL": self.api_base_url,
            "NEWSROOM_DESKTOP_RUNTIME_ROOT": str(self.runtime_root),
            "NEWSROOM_DESKTOP_RELEASE_ROOT": str(self.release_root),
            "NEWSROOM_DESKTOP_BACKUP_ROOT": str(self.backup_root),
            "NEWSROOM_DESKTOP_CONTROL_PATH": str(self.control_path),
            "NEWSROOM_DESKTOP_PARSER_MODE": self.parser_mode,
            "NEWS_RESEARCH_PARSER_BACKENDS": self.parser_mode,
            "NEWSROOM_PDF_PARSER_CASCADE": self.parser_mode,
        }
        if self.research_root is not None:
            values["NEWS_RESEARCH_ROOT"] = str(self.research_root)
        if self.artifact_root is not None:
            values["NEWS_RESEARCH_ARTIFACT_ROOT"] = str(self.artifact_root)
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.config_schema,
            "deployment_profile": self.deployment_profile,
            "frontend": {"host": self.frontend_host, "port": self.frontend_port},
            "api": {"host": self.api_host, "port": self.api_port, "base_url": self.api_base_url},
            "canonical_origin": self.canonical_origin,
            "project_root": str(self.project_root),
            "runtime_root": str(self.runtime_root),
            "release_root": str(self.release_root),
            "backup_root": str(self.backup_root),
            "control_path": str(self.control_path),
            "parser_mode": self.parser_mode,
            "research_root": str(self.research_root) if self.research_root is not None else None,
            "artifact_root": str(self.artifact_root) if self.artifact_root is not None else None,
        }


def resolve_local_desktop_config(
    *,
    project_root: str | Path,
    cli_overrides: Mapping[str, Any] | None = None,
    runtime_config: Mapping[str, Any] | None = None,
    root_env: Mapping[str, str] | None = None,
) -> LocalDesktopConfig:
    """Resolve CLI > runtime config > root environment > defaults without reading secrets."""

    root = Path(project_root).expanduser().resolve()
    if not root.is_dir():
        raise LocalDesktopConfigError("desktop_project_root_missing", "project root does not exist", field_name="project_root")
    runtime_values = _normalize_mapping(runtime_config or {}, source="runtime config")
    cli_values = _normalize_mapping(cli_overrides or {}, source="CLI")
    env_values = _env_fields(root_env if root_env is not None else os.environ)
    defaults: dict[str, Any] = {
        "deployment_profile": LOCAL_DESKTOP_PROFILE,
        "frontend_host": DEFAULT_FRONTEND_HOST,
        "frontend_port": DEFAULT_FRONTEND_PORT,
        "api_host": DEFAULT_API_HOST,
        "api_port": DEFAULT_API_PORT,
        "canonical_origin": f"http://{DEFAULT_FRONTEND_HOST}:{DEFAULT_FRONTEND_PORT}",
        "api_base_url": f"http://{DEFAULT_API_HOST}:{DEFAULT_API_PORT}",
        "runtime_root": root / ".newsroom" / "desktop",
        "release_root": root / ".newsroom" / "desktop" / "releases",
        "backup_root": root / ".newsroom" / "desktop" / "backups",
        "control_path": root / ".newsroom" / "local-desktop" / "control.json",
        "parser_mode": "pymupdf",
        "research_root": None,
        "artifact_root": None,
    }
    merged = {**defaults, **env_values, **runtime_values, **cli_values}
    profile = str(merged["deployment_profile"]).strip()
    if profile != LOCAL_DESKTOP_PROFILE:
        raise LocalDesktopConfigError("desktop_profile_invalid", "NEWSROOM_DEPLOYMENT_PROFILE must be local-desktop", field_name="deployment_profile")
    frontend_host = _loopback_host(merged["frontend_host"], "frontend_host")
    api_host = _loopback_host(merged["api_host"], "api_host")
    frontend_port = _port(merged["frontend_port"], "frontend_port")
    api_port = _port(merged["api_port"], "api_port")
    if frontend_host == api_host and frontend_port == api_port:
        raise LocalDesktopConfigError("desktop_port_conflict", "frontend and API cannot use the same bind endpoint")
    canonical_origin = _origin(merged["canonical_origin"], expected_host=frontend_host, expected_port=frontend_port, field_name="canonical_origin")
    api_base_url = _origin(merged["api_base_url"], expected_host=api_host, expected_port=api_port, field_name="api_base_url")
    runtime_root = _resolved_path(root, merged["runtime_root"], "runtime_root")
    release_root = _resolved_path(root, merged["release_root"], "release_root")
    backup_root = _resolved_path(root, merged["backup_root"], "backup_root")
    control_path = _resolved_path(root, merged["control_path"], "control_path")
    if release_root == backup_root or release_root.is_relative_to(backup_root) or backup_root.is_relative_to(release_root):
        raise LocalDesktopConfigError("desktop_storage_roots_overlap", "release_root and backup_root must not overlap")
    if control_path.is_dir():
        raise LocalDesktopConfigError("desktop_control_path_invalid", "control_path must name a file", field_name="control_path")
    parser_mode = str(merged["parser_mode"]).strip().lower()
    if parser_mode not in {"pymupdf", "marker", "mineru"}:
        raise LocalDesktopConfigError("desktop_parser_mode_invalid", "parser_mode must be pymupdf, marker, or mineru", field_name="parser_mode")
    return LocalDesktopConfig(
        project_root=root,
        deployment_profile=profile,
        frontend_host=frontend_host,
        frontend_port=frontend_port,
        api_host=api_host,
        api_port=api_port,
        canonical_origin=canonical_origin,
        api_base_url=api_base_url,
        runtime_root=runtime_root,
        release_root=release_root,
        backup_root=backup_root,
        control_path=control_path,
        parser_mode=parser_mode,
        research_root=_resolved_path(root, merged["research_root"], "research_root") if merged["research_root"] is not None else None,
        artifact_root=_resolved_path(root, merged["artifact_root"], "artifact_root") if merged["artifact_root"] is not None else None,
    )


def read_runtime_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalDesktopConfigError("desktop_runtime_config_invalid", "runtime config is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise LocalDesktopConfigError("desktop_runtime_config_invalid", "runtime config must be a JSON object")
    schema = payload.pop("schema", LOCAL_DESKTOP_CONFIG_SCHEMA)
    if schema != LOCAL_DESKTOP_CONFIG_SCHEMA:
        raise LocalDesktopConfigError("desktop_runtime_config_schema_unsupported", "runtime config schema is not supported")
    return payload


def validate_lifecycle_timeout(
    value: Any,
    *,
    field_name: str,
    maximum_seconds: float = MAX_LIFECYCLE_TIMEOUT_SECONDS,
) -> float:
    """Validate direct service calls and CLI values before any lifecycle side effect."""
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite number in (0, {maximum_seconds:g}]") from exc
    if isinstance(value, bool) or not math.isfinite(numeric) or not 0 < numeric <= maximum_seconds:
        raise ValueError(f"{field_name} must be a finite number in (0, {maximum_seconds:g}]")
    return numeric


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_mapping(values: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    unknown = sorted(set(values) - _ALLOWED_FIELDS - {"schema"})
    if unknown:
        raise LocalDesktopConfigError("desktop_config_unknown_field", f"{source} contains unknown fields: {', '.join(unknown)}")
    return {key: value for key, value in values.items() if key != "schema" and value is not None}


def _env_fields(values: Mapping[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for env_name, field_name in _ENV_TO_FIELD.items():
        value = values.get(env_name)
        if value is None or str(value).strip() == "":
            continue
        normalized = str(value).strip()
        if field_name in result and str(result[field_name]).strip() != normalized:
            raise LocalDesktopConfigError(
                "desktop_config_env_conflict",
                f"{env_name} conflicts with {sources[field_name]}",
                field_name=field_name,
            )
        result[field_name] = value
        sources[field_name] = env_name
    return result


def _loopback_host(value: Any, field_name: str) -> str:
    host = str(value).strip().lower()
    if host == "localhost":
        host = DEFAULT_FRONTEND_HOST
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise LocalDesktopConfigError("desktop_bind_not_loopback", f"{field_name} must be a literal loopback address", field_name=field_name) from exc
    if address.version != 4 or str(address) != DEFAULT_FRONTEND_HOST:
        raise LocalDesktopConfigError("desktop_bind_not_loopback", f"{field_name} must be the literal 127.0.0.1 loopback address", field_name=field_name)
    return DEFAULT_FRONTEND_HOST


def _port(value: Any, field_name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise LocalDesktopConfigError("desktop_port_invalid", f"{field_name} must be an integer", field_name=field_name) from exc
    if not 1 <= port <= 65535:
        raise LocalDesktopConfigError("desktop_port_invalid", f"{field_name} must be between 1 and 65535", field_name=field_name)
    return port


def _origin(value: Any, *, expected_host: str, expected_port: int, field_name: str) -> str:
    raw = str(value).strip()
    parsed = urlsplit(raw)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LocalDesktopConfigError("desktop_origin_invalid", f"{field_name} must be a plain loopback HTTP origin", field_name=field_name)
    if parsed.path not in {"", "/"} or parsed.hostname != expected_host or parsed.port != expected_port:
        raise LocalDesktopConfigError("desktop_origin_conflict", f"{field_name} must match its configured bind endpoint", field_name=field_name)
    return f"http://{expected_host}:{expected_port}"


def _resolved_path(root: Path, value: Any, field_name: str) -> Path:
    raw = str(value).strip()
    if not raw or "\x00" in raw:
        raise LocalDesktopConfigError("desktop_path_invalid", f"{field_name} is invalid", field_name=field_name)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise LocalDesktopConfigError("desktop_path_unsafe", f"{field_name} cannot be a filesystem root", field_name=field_name)
    return resolved


def _redact_details(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).casefold().replace("-", "_")
        sensitive = normalized not in _NON_SECRET_BUDGET_FIELDS and any(token in normalized for token in _SECRET_NAME_TOKENS)
        result[str(key)] = "***REDACTED***" if sensitive else item
    return result


__all__ = [
    "CapabilityProjection",
    "CapabilityState",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "DEFAULT_FRONTEND_HOST",
    "DEFAULT_FRONTEND_PORT",
    "LOCAL_DESKTOP_CONFIG_SCHEMA",
    "LOCAL_DESKTOP_PROFILE",
    "MAX_LIFECYCLE_TIMEOUT_SECONDS",
    "validate_lifecycle_timeout",
    "LocalDesktopConfig",
    "LocalDesktopConfigError",
    "ProbeBudget",
    "read_runtime_config",
    "resolve_local_desktop_config",
    "utc_timestamp",
]
