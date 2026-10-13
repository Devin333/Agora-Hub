from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from framework.shared.env import env_values_from_root
from framework.shared.local_desktop import (
    LocalDesktopConfig,
    LocalDesktopConfigError,
    read_runtime_config,
    resolve_local_desktop_config,
)
from scripts.local_desktop.storage_contract import resolve_research_storage_paths

REAL_DATA_MODE = "real"
_PUBLIC_MOCK_ENV_NAMES = frozenset({"NEXT_PUBLIC_NEWSROOM_USE_MOCKS"})
_FALSE_ENV_VALUES = frozenset({"", "0", "false", "no", "off"})

_PATH_ENV_NAMES = frozenset(
    {
        "NEWS_ARTIFACT_ROOT",
        "NEWS_RESEARCH_ARTIFACT_ROOT",
        "NEWS_RESEARCH_ROOT",
        "NEWS_RESEARCH_RUN_STORE_ROOT",
        "NEWS_RESEARCH_RAG_LOCAL_ROOT",
        "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT",
        "NEWS_AUDIT_LOG_PATH",
        "NEWS_MODELS_CONFIG",
        "NEWS_RESEARCH_MODELS_CONFIG",
        "NEWS_SOURCES_CONFIG",
        "NEWS_VISUAL_IMAGE_ROOT",
        "NEWS_VISUAL_DESCRIPTION_IMAGE_ROOT",
        "NEWSROOM_AUTH_USERS_PATH",
        "NEWSROOM_AUTH_SESSIONS_PATH",
        "NEWSROOM_AUTH_OTP_PATH",
        "NEWSROOM_AUTH_OAUTH_PATH",
        "NEWSROOM_RESEARCH_HISTORY_PATH",
        "NEWSROOM_RESEARCH_IMPORT_DIR",
        "NEWSROOM_PARSER_RUN_ROOT",
        "NEWSROOM_MARKER_CACHE_DIR",
        "NEWSROOM_MINERU_CACHE_DIR",
        "NEWSROOM_MINERU_CONFIG_DIR",
        "NEWSROOM_PAPERS_DATA_PATH",
        "NEWSROOM_DESKTOP_CONTROL_PATH",
    }
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_root_environment(*, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    return dict(environ) if environ is not None else env_values_from_root()


def load_config(*, cli_overrides: Mapping[str, Any] | None = None, environ: Mapping[str, str] | None = None) -> LocalDesktopConfig:
    root = project_root()
    values = load_root_environment(environ=environ)
    preliminary = resolve_local_desktop_config(project_root=root, cli_overrides=cli_overrides, root_env=values)
    runtime_path_value = values.get("NEWSROOM_DESKTOP_CONFIG_PATH")
    runtime_path = Path(runtime_path_value).expanduser() if runtime_path_value else preliminary.runtime_config_path
    if not runtime_path.is_absolute():
        runtime_path = root / runtime_path
    runtime_values = read_runtime_config(runtime_path.resolve(strict=False))
    return resolve_local_desktop_config(project_root=root, cli_overrides=cli_overrides, runtime_config=runtime_values, root_env=values)


def sanitized_environment(config: LocalDesktopConfig, *, base: Mapping[str, str] | None = None) -> dict[str, str]:
    values = require_real_data_environment(load_root_environment(environ=base))
    values.update(config.as_environment())
    for name in list(values):
        if name.startswith("NEXT_PUBLIC_") and any(token in name for token in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "CREDENTIAL")):
            raise LocalDesktopConfigError("desktop_public_secret_rejected", f"secret-like browser environment key is not allowed: {name}")
    for name in _PATH_ENV_NAMES:
        raw = str(values.get(name) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = config.project_root / path
        values[name] = str(path.resolve(strict=False))
    research_paths = resolve_research_storage_paths(config.project_root, values)
    values.update(
        {
            "NEWS_RESEARCH_ARTIFACT_ROOT": str(research_paths.artifact_root),
            "NEWS_RESEARCH_ROOT": str(research_paths.research_root),
            "NEWS_RESEARCH_RUN_STORE_ROOT": str(research_paths.run_store_root),
            "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(research_paths.rag_local_root),
            "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(research_paths.rag_transcript_root),
            "NEWSROOM_AUTH_USERS_PATH": str(_resolved_default_path(config, values, "NEWSROOM_AUTH_USERS_PATH", ".newsroom/auth/users.json")),
            "NEWSROOM_AUTH_SESSIONS_PATH": str(_resolved_default_path(config, values, "NEWSROOM_AUTH_SESSIONS_PATH", ".newsroom/auth/sessions.json")),
            "NEWSROOM_AUTH_OTP_PATH": str(_resolved_default_path(config, values, "NEWSROOM_AUTH_OTP_PATH", ".newsroom/auth/otp_challenges.json")),
            "NEWSROOM_AUTH_OAUTH_PATH": str(_resolved_default_path(config, values, "NEWSROOM_AUTH_OAUTH_PATH", ".newsroom/auth/oauth_states.json")),
            "NEWSROOM_RESEARCH_HISTORY_PATH": str(_resolved_default_path(config, values, "NEWSROOM_RESEARCH_HISTORY_PATH", ".newsroom/research/history.json")),
            "NEWSROOM_RESEARCH_IMPORT_DIR": str(_resolved_default_path(config, values, "NEWSROOM_RESEARCH_IMPORT_DIR", ".newsroom/research/imports")),
            "NEWSROOM_PAPERS_DATA_PATH": str(_resolved_default_path(config, values, "NEWSROOM_PAPERS_DATA_PATH", ".newsroom/papers/arxiv-papers.json")),
        }
    )
    values.update(config.as_environment())
    values["PYTHONUTF8"] = "1"
    return values


def require_real_data_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Return a copy that cannot enable browser-side fixture or mock data."""

    result = dict(values)
    for name in _PUBLIC_MOCK_ENV_NAMES:
        raw = str(result.get(name) or "").strip().casefold()
        if raw not in _FALSE_ENV_VALUES:
            raise LocalDesktopConfigError(
                "desktop_mock_data_rejected",
                f"local desktop production runtime does not allow mock data: {name}",
                field_name=name,
            )
        result.pop(name, None)
    return result


def _resolved_default_path(config: LocalDesktopConfig, values: Mapping[str, str], name: str, default: str) -> Path:
    selected = Path(str(values.get(name) or default)).expanduser()
    if not selected.is_absolute():
        selected = config.project_root / selected
    return selected.resolve(strict=False)


__all__ = ["load_config", "load_root_environment", "project_root", "sanitized_environment"]
