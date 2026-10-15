from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from threading import Lock

from backend.research.application.personal_memory import PersonalMemoryService
from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore
from interfaces.composition.research_settings import research_storage_paths_from_env
from interfaces.services.personal_memory_service import PersonalMemoryApplicationService


PERSONAL_MEMORY_PATH_ENV = "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH"
_RESEARCH_ROOT_ENV = "NEWS_RESEARCH_ROOT"
_RESEARCH_ARTIFACT_ROOT_ENVS = (
    "NEWS_RESEARCH_ARTIFACT_ROOT",
    "NEWS_ARTIFACT_ROOT",
)


@dataclass(slots=True)
class PersonalMemoryRuntime:
    store: SQLitePersonalMemoryStore
    application: PersonalMemoryService
    service: PersonalMemoryApplicationService

    def close(self) -> None:
        self.store.close()


class PersonalMemoryRuntimeResolver:
    """Thread-safe, process-local owner for the default personal-memory runtime."""

    def __init__(self, factory: Callable[[], PersonalMemoryRuntime]) -> None:
        if not callable(factory):
            raise TypeError("personal memory runtime factory must be callable")
        self._factory = factory
        self._lock = Lock()
        self._runtime: PersonalMemoryRuntime | None = None
        self._closed = False

    def get(self) -> PersonalMemoryRuntime:
        with self._lock:
            if self._closed:
                raise RuntimeError("personal memory runtime resolver is closed")
            if self._runtime is None:
                self._runtime = self._factory()
            return self._runtime

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtime = self._runtime
            self._runtime = None
        if runtime is not None:
            runtime.close()


@dataclass(frozen=True, slots=True)
class _PersonalMemoryDatabasePath:
    lexical: Path
    resolved: Path


def personal_memory_database_path_from_env(
    env: Mapping[str, str] | None = None,
    *,
    cwd: str | PathLike[str] | None = None,
) -> Path:
    return _personal_memory_database_path(env, cwd=cwd).resolved


def _personal_memory_database_path(
    env: Mapping[str, str] | None = None,
    *,
    cwd: str | PathLike[str] | None = None,
) -> _PersonalMemoryDatabasePath:
    values = os.environ if env is None else env
    configured = str(values.get(PERSONAL_MEMORY_PATH_ENV) or "").strip()
    if configured:
        lexical = Path(configured).expanduser()
        if not lexical.is_absolute():
            raise ValueError(f"{PERSONAL_MEMORY_PATH_ENV} must be an absolute file path")
    else:
        lexical_root = _lexical_research_root(values, cwd=cwd)
        lexical = lexical_root / "personal-memory.sqlite3"
    _reject_linked_existing_path_chain(lexical)
    if configured:
        resolved = resolve_storage_path(lexical)
    else:
        # Preserve the shared settings validation contract, using native paths
        # only inside its filesystem boundary; the runtime owns a logical path.
        base = _lexical_absolute_path(
            Path.cwd() if cwd is None else Path(cwd).expanduser(),
            base=Path.cwd(),
        )
        storage_values = dict(values)
        for name in (
            *_RESEARCH_ARTIFACT_ROOT_ENVS,
            _RESEARCH_ROOT_ENV,
            "NEWS_RESEARCH_RUN_STORE_ROOT",
            "NEWS_RESEARCH_RAG_LOCAL_ROOT",
            "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT",
        ):
            raw = str(values.get(name) or "").strip()
            if raw and "\x00" not in raw:
                storage_values[name] = os.fspath(
                    storage_io_path(
                        _lexical_absolute_path(Path(raw).expanduser(), base=base)
                    )
                )
        storage_paths = research_storage_paths_from_env(
            storage_values, cwd=storage_io_path(base)
        )
        resolved = resolve_storage_path(
            storage_paths.research_root / "personal-memory.sqlite3"
        )
    if resolved.name in {"", ".", ".."}:
        raise ValueError("personal memory path must name a database file")
    return _PersonalMemoryDatabasePath(lexical=lexical, resolved=resolved)


def build_personal_memory_runtime(
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | PathLike[str] | None = None,
) -> PersonalMemoryRuntime:
    path_plan = _personal_memory_database_path(env, cwd=cwd)
    # Recheck the lexical chain immediately before the first filesystem write.
    # Resolving first would erase evidence that a junction was configured.
    _reject_linked_existing_path_chain(path_plan.lexical)
    database_path = path_plan.resolved
    database_parent_io = storage_io_path(database_path.parent)
    database_parent_io.mkdir(parents=True, exist_ok=True)
    if not database_parent_io.is_dir():
        raise ValueError("personal memory database parent must be a directory")
    store = SQLitePersonalMemoryStore(database_path)
    application = PersonalMemoryService(store)
    service = PersonalMemoryApplicationService(
        application,
        database_path=store.database_path,
    )
    return PersonalMemoryRuntime(
        store=store,
        application=application,
        service=service,
    )


def _lexical_research_root(
    values: Mapping[str, str],
    *,
    cwd: str | PathLike[str] | None,
) -> Path:
    raw_base = Path.cwd() if cwd is None else Path(cwd).expanduser()
    base = (
        raw_base
        if raw_base.is_absolute()
        else _lexical_absolute_path(raw_base, base=Path.cwd())
    )
    configured_root = str(values.get(_RESEARCH_ROOT_ENV) or "").strip()
    if configured_root:
        return _lexical_absolute_path(Path(configured_root).expanduser(), base=base)

    configured_artifact_root = next(
        (
            str(values.get(name) or "").strip()
            for name in _RESEARCH_ARTIFACT_ROOT_ENVS
            if str(values.get(name) or "").strip()
        ),
        "",
    )
    artifact_root = (
        _lexical_absolute_path(Path(configured_artifact_root).expanduser(), base=base)
        if configured_artifact_root
        else base / ".newsroom" / "runs"
    )
    return artifact_root.parent / "research"


def _lexical_absolute_path(path: Path, *, base: Path | None) -> Path:
    if "\x00" in os.fspath(path):
        raise ValueError("personal memory path contains a null byte")
    if path.is_absolute():
        return path
    if base is None:
        raise ValueError("personal memory cwd must be absolute")
    return base / path


def _reject_linked_existing_path_chain(path: Path) -> None:
    if not path.is_absolute():
        raise ValueError("personal memory path must be absolute")
    current = path
    while True:
        try:
            details = storage_io_path(current).lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError("personal memory path cannot be inspected") from exc
        else:
            attributes = int(getattr(details, "st_file_attributes", 0) or 0)
            reparse_flag = int(
                getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            if stat.S_ISLNK(details.st_mode) or attributes & reparse_flag:
                raise ValueError(
                    "personal memory path must not traverse symlinks or reparse points"
                )
        if current.parent == current:
            break
        current = current.parent


__all__ = [
    "PERSONAL_MEMORY_PATH_ENV",
    "PersonalMemoryRuntime",
    "PersonalMemoryRuntimeResolver",
    "build_personal_memory_runtime",
    "personal_memory_database_path_from_env",
]
