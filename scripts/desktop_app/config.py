from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

from interfaces.composition.research_settings import DESKTOP_RESEARCH_LLM_ENABLED_ENV
from framework.shared.local_desktop import LocalDesktopConfig, LocalDesktopConfigError, resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError, protect_private_path, windows_extended_path
from scripts.local_desktop.config import sanitized_environment
from scripts.desktop_app.owned_clear import fence_normal_initialization
from scripts.local_desktop.runtime_config import (
    read_runtime_config_document,
    recover_runtime_config_document,
    runtime_config_paths_for_data_root,
    runtime_environment_overlay,
)

BUNDLE_MANIFEST_NAME = "desktop-bundle.json"
PERSONAL_MEMORY_ENV = "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH"
_RUNTIME_ENV_KEYS = (
    DESKTOP_RESEARCH_LLM_ENABLED_ENV,
    "NEWS_MODELS_CONFIG",
    "NEWS_RESEARCH_MODELS_CONFIG",
    "NEWS_SOURCES_CONFIG",
    "NEWS_RESEARCH_LLM_ROUTE_ID",
    "NEWS_RESEARCH_LLM_PROVIDER",
    "NEWS_RESEARCH_LLM_MODEL",
    "NEWS_RESEARCH_LLM_BASE_URL",
    "NEWS_RESEARCH_LLM_API_KEY_ENV",
    "NEWS_RESEARCH_SOURCE_PROVIDER",
    "NEWS_RESEARCH_ARXIV_API_URL",
    "NEWS_RESEARCH_SOURCE_TIMEOUT_SECONDS",
)

_PATH_KEYS = (
    "NEWS_ARTIFACT_ROOT",
    "NEWS_RESEARCH_ARTIFACT_ROOT",
    "NEWS_RESEARCH_ROOT",
    "NEWS_RESEARCH_RUN_STORE_ROOT",
    "NEWS_RESEARCH_RAG_LOCAL_ROOT",
    "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT",
    "NEWS_AUDIT_LOG_PATH",
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
    "NEWSROOM_PAPERS_DATA_PATH",
    "NEWSROOM_DESKTOP_CONTROL_PATH",
    PERSONAL_MEMORY_ENV,
)


@dataclass(frozen=True)
class DesktopAppConfig:
    resource_root: Path
    data_root: Path
    desktop: LocalDesktopConfig
    environment: Mapping[str, str]

    @property
    def bundle_manifest_path(self) -> Path:
        return self.resource_root / BUNDLE_MANIFEST_NAME

    @property
    def packaged(self) -> bool:
        return self.bundle_manifest_path.is_file()

    @property
    def python_executable(self) -> Path:
        return self.resource_root / ".venv" / "Scripts" / "python.exe"

    @property
    def node_root(self) -> Path:
        return self.resource_root / "node"

    @property
    def node_executable(self) -> Path:
        return self.node_root / "node.exe"

    @property
    def npm_executable(self) -> Path:
        return self.node_root / "npm.cmd"




def resolve_desktop_app_config(
    *,
    resource_root: str | Path,
    data_root: str | Path,
    frontend_port: int = 3120,
    api_port: int = 8120,
    environ: Mapping[str, str] | None = None,
) -> DesktopAppConfig:
    resources = _absolute_lexical_path(resource_root, field="resource_root")
    data = _absolute_lexical_path(data_root, field="data_root")
    fence_normal_initialization(data)
    _reject_config_reparse_chain(resources, field="resource_root")
    _reject_config_reparse_chain(data, field="data_root")
    for config_path in (resources / "configs", resources / "configs" / "models.yaml", resources / "configs" / "sources.yaml"):
        _reject_config_reparse_chain(config_path, field="resource_root")
    if not resources.is_dir():
        raise LocalDesktopConfigError(
            "desktop_app_resource_root_missing",
            "desktop app resource root does not exist",
            field_name="resource_root",
        )
    if _paths_overlap(resources, data):
        raise LocalDesktopConfigError(
            "desktop_app_roots_overlap",
            "resource root and user data root must be separate and non-overlapping",
            field_name="data_root",
        )
    if data.exists() and not data.is_dir():
        raise LocalDesktopConfigError(
            "desktop_app_data_root_invalid",
            "desktop app data root must be a directory",
            field_name="data_root",
        )

    inherited = dict(environ or os.environ)
    for key in _RUNTIME_ENV_KEYS:
        inherited.pop(key, None)
    base = _owned_environment(resources, data, inherited)
    runtime_paths = runtime_config_paths_for_data_root(data)
    _reject_config_reparse_chain(runtime_paths.document, field="runtime_config_path")
    _reject_config_reparse_chain(runtime_paths.models, field="runtime_models_path")
    runtime_document = recover_runtime_config_document(runtime_paths)
    configured_llm = runtime_document["research"]["llm"] is not None
    if configured_llm and not runtime_paths.models.is_file() and not runtime_paths.journal.is_file():
        raise LocalDesktopConfigError(
            "desktop_runtime_models_missing",
            "configured desktop runtime models file is missing after runtime configuration recovery",
            field_name="runtime_config_path",
        )
    if not configured_llm:
        # The runtime configuration owner is authoritative even on first run.
        # An absent document resolves to llm=null and must not inherit a model
        # provider packaged with the immutable application resources.
        base.pop("NEWS_MODELS_CONFIG", None)
        base.pop("NEWS_RESEARCH_MODELS_CONFIG", None)
    base.update(runtime_environment_overlay(runtime_document, models_path=runtime_paths.models if configured_llm else None))
    for key in _PATH_KEYS:
        raw = str(base.get(key) or "").strip()
        if not raw:
            continue
        owner_path = _absolute_lexical_path(raw, field=key)
        if not _same_or_descendant(owner_path, data):
            raise LocalDesktopConfigError("desktop_app_owner_path_escape", f"{key} must stay under data root", field_name=key)
        _reject_config_reparse_chain(owner_path, field=key)

    overrides = {
        "frontend_port": frontend_port,
        "api_port": api_port,
        "canonical_origin": f"http://127.0.0.1:{int(frontend_port)}",
        "api_base_url": f"http://127.0.0.1:{int(api_port)}",
        # LifecycleManager owns instance.json, active-release.json and logs. Binding
        # runtime_root to data_root keeps its fixed logs child at <data-root>/logs.
        "runtime_root": str(data),
        "release_root": str(data / "releases"),
        "backup_root": str(data / "backups"),
        "control_path": str(data / "desktop" / "control" / "control.json"),
        "storage_mode": "owned-local",
        "research_root": str(data / "research"),
        "artifact_root": str(data / "runs" / "artifacts"),
    }
    for name in ("runtime_root", "release_root", "backup_root", "control_path", "research_root", "artifact_root"):
        _reject_config_reparse_chain(Path(str(overrides[name])), field=name)
    desktop = resolve_local_desktop_config(
        project_root=resources,
        cli_overrides=overrides,
        root_env={},
    )
    environment = sanitized_environment(desktop, base=base)
    if not configured_llm:
        for key in _RUNTIME_ENV_KEYS:
            if key.startswith("NEWS_RESEARCH_LLM_") or key in {"NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG"}:
                environment.pop(key, None)
    for key in _PATH_KEYS:
        raw = str(environment.get(key) or "").strip()
        if not raw:
            continue
        owner_path = _absolute_lexical_path(raw, field=key)
        if not _same_or_descendant(owner_path, data):
            raise LocalDesktopConfigError("desktop_app_owner_path_escape", f"{key} must stay under data root", field_name=key)
        _reject_config_reparse_chain(owner_path, field=key)
    return DesktopAppConfig(resources, data, desktop, environment)


def initialize_data_root(config: DesktopAppConfig) -> tuple[Path, ...]:
    directories = (
        config.data_root,
        config.data_root / "auth",
        config.data_root / "research" / "history",
        config.data_root / "research" / "imports",
        config.data_root / "research" / "catalog",
        config.data_root / "runs" / "artifacts",
        config.data_root / "runs" / "research",
        config.data_root / "desktop" / "control",
        config.data_root / "desktop" / "cache" / "marker",
        config.data_root / "releases",
        config.data_root / "backups",
        config.data_root / "logs",
    )
    _reject_lexical_reparse_chain(config.resource_root)
    _reject_lexical_reparse_chain(config.data_root)
    for key in _PATH_KEYS:
        raw = str(config.environment.get(key) or "").strip()
        if not raw:
            continue
        owner_path = Path(raw)
        if not _same_or_descendant(owner_path, config.data_root):
            raise DesktopOperationError("desktop_app_data_path_escape", "desktop app owner path escaped the owned root")
        _reject_lexical_reparse_chain(owner_path)
    for directory in directories:
        if not _same_or_descendant(directory, config.data_root):
            raise DesktopOperationError("desktop_app_data_path_escape", "desktop app data directory escaped the owned root")
        _reject_lexical_reparse_chain(directory)

    protected: set[Path] = set()
    for directory in directories:
        _ensure_private_directory(directory, owned_root=config.data_root, protected=protected)
    return directories


@contextmanager
def controlled_process_environment(config: DesktopAppConfig) -> Iterator[None]:
    updates = dict(config.environment)
    path_parts = [
        str(path)
        for path, executable in (
            (config.node_root, config.node_executable),
            (config.python_executable.parent, config.python_executable),
        )
        if executable.is_file()
    ]
    current_path = str(os.environ.get("PATH") or "")
    updates["PATH"] = os.pathsep.join((*path_parts, current_path)) if path_parts and current_path else (os.pathsep.join(path_parts) or current_path)
    managed_keys = tuple(dict.fromkeys((*updates, *_RUNTIME_ENV_KEYS, "PYTHONHOME", "PYTHONPATH")))
    previous = {key: os.environ.get(key) for key in managed_keys}
    try:
        os.environ.pop("PYTHONHOME", None)
        os.environ.pop("PYTHONPATH", None)
        # Explicit empty values prevent both parent inheritance and root .env
        # loading from reviving absent owner-managed settings. Restore the exact
        # previous environment on exit; ordinary profiles remain unaffected.
        for key in _RUNTIME_ENV_KEYS:
            if key not in updates:
                os.environ[key] = ""
        os.environ.update({key: str(value) for key, value in updates.items()})
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _owned_environment(resource_root: Path, data_root: Path, source: Mapping[str, str]) -> dict[str, str]:
    values = dict(source)
    values.pop("PYTHONHOME", None)
    values.pop("PYTHONPATH", None)
    values.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NEWS_DATABASE_DSN": "",
            "NEWS_MEMORY_POSTGRES_ENABLED": "0",
            "NEWS_RESEARCH_RAG_BACKEND": "local",
            "NEWS_ARTIFACT_ROOT": str(data_root / "runs" / "artifacts"),
            "NEWS_RESEARCH_ARTIFACT_ROOT": str(data_root / "runs" / "artifacts"),
            "NEWS_RESEARCH_ROOT": str(data_root / "research"),
            "NEWS_RESEARCH_RUN_STORE_ROOT": str(data_root / "runs" / "research"),
            "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(data_root / "research" / "catalog" / "chunks"),
            "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(data_root / "research" / "history" / "transcripts"),
            "NEWS_AUDIT_LOG_PATH": str(data_root / "logs" / "audit.jsonl"),
            "NEWS_VISUAL_IMAGE_ROOT": str(data_root / "runs" / "artifacts" / "visual" / "images"),
            "NEWS_VISUAL_DESCRIPTION_IMAGE_ROOT": str(data_root / "runs" / "artifacts" / "visual" / "descriptions"),
            "NEWSROOM_AUTH_USERS_PATH": str(data_root / "auth" / "users.json"),
            "NEWSROOM_AUTH_SESSIONS_PATH": str(data_root / "auth" / "sessions.json"),
            "NEWSROOM_AUTH_OTP_PATH": str(data_root / "auth" / "otp-challenges.json"),
            "NEWSROOM_AUTH_OAUTH_PATH": str(data_root / "auth" / "oauth-states.json"),
            "NEWSROOM_RESEARCH_HISTORY_PATH": str(data_root / "research" / "history" / "history.json"),
            "NEWSROOM_RESEARCH_IMPORT_DIR": str(data_root / "research" / "imports"),
            "NEWSROOM_PAPERS_DATA_PATH": str(data_root / "research" / "catalog" / "arxiv-papers.json"),
            "NEWSROOM_PARSER_RUN_ROOT": str(data_root / "runs" / "parser"),
            "NEWSROOM_MARKER_CACHE_DIR": str(data_root / "desktop" / "cache" / "marker"),
            "NEWSROOM_DESKTOP_CONTROL_PATH": str(data_root / "desktop" / "control" / "control.json"),
            PERSONAL_MEMORY_ENV: str(data_root / "research" / "personal-memory.sqlite3"),
        }
    )
    configs = resource_root / "configs"
    models = configs / "models.yaml"
    sources = configs / "sources.yaml"
    if models.is_file():
        values["NEWS_MODELS_CONFIG"] = str(models)
        values["NEWS_RESEARCH_MODELS_CONFIG"] = str(models)
    else:
        values.pop("NEWS_MODELS_CONFIG", None)
        values.pop("NEWS_RESEARCH_MODELS_CONFIG", None)
    if sources.is_file():
        values["NEWS_SOURCES_CONFIG"] = str(sources)
    else:
        values.pop("NEWS_SOURCES_CONFIG", None)
    return values


def _absolute_lexical_path(value: str | Path, *, field: str) -> Path:
    selected = Path(value).expanduser()
    if ".." in selected.parts:
        raise LocalDesktopConfigError("desktop_app_path_invalid", f"{field} must not contain parent traversal", field_name=field)
    return Path(os.path.abspath(os.fspath(selected)))


def _same_or_descendant(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(candidate), os.fspath(root)))
    except ValueError:
        return False
    return os.path.normcase(common) == os.path.normcase(os.fspath(root))


def _paths_overlap(first: Path, second: Path) -> bool:
    return _same_or_descendant(first, second) or _same_or_descendant(second, first)


def _reject_config_reparse_chain(path: Path, *, field: str) -> None:
    try:
        _reject_lexical_reparse_chain(path)
    except DesktopOperationError as exc:
        raise LocalDesktopConfigError(
            "desktop_app_reparse_path_rejected",
            f"{field} contains a symbolic link or Windows reparse point",
            field_name=field,
        ) from exc


def _reject_lexical_reparse_chain(path: Path) -> None:
    if not path.is_absolute() or not path.anchor:
        raise DesktopOperationError("desktop_app_path_invalid", "desktop app path must be absolute")
    anchor = Path(path.anchor)
    probes = [anchor]
    current = anchor
    for part in path.parts[1:]:
        current = current / part
        probes.append(current)
    for probe in probes:
        try:
            metadata = windows_extended_path(probe).lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise DesktopOperationError("desktop_app_path_inspection_failed", "desktop app path could not be inspected safely") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {probe.name or probe}")


def _ensure_private_directory(path: Path, *, owned_root: Path, protected: set[Path]) -> None:
    anchor = Path(path.anchor)
    current = anchor
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = windows_extended_path(current).lstat()
        except FileNotFoundError:
            try:
                current.mkdir()
            except FileExistsError:
                pass
            _reject_lexical_reparse_chain(current)
            metadata = windows_extended_path(current).lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {current.name or current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise DesktopOperationError("desktop_app_data_path_invalid", "desktop app data path is not a directory")
        if _same_or_descendant(current, owned_root) and current not in protected:
            protect_private_path(current)
            protected.add(current)


__all__ = [
    "BUNDLE_MANIFEST_NAME",
    "DesktopAppConfig",
    "PERSONAL_MEMORY_ENV",
    "controlled_process_environment",
    "initialize_data_root",
    "resolve_desktop_app_config",
]
