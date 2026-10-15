from __future__ import annotations

import os
from pathlib import Path

_WINDOWS = os.name == "nt"
_EXTENDED_PREFIX = "\\\\?\\"
_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"


def resolve_storage_path(path: str | Path) -> Path:
    """Resolve a public storage path without exposing a Win32 I/O namespace."""

    expanded = Path(path).expanduser()
    if not _WINDOWS:
        return expanded.resolve()

    resolved = storage_io_path(expanded).resolve()
    return _logical_windows_path(resolved)


def storage_io_path(path: str | Path) -> Path:
    """Return an internal path suitable for long-path storage I/O."""

    logical = Path(path)
    if not _WINDOWS:
        return logical

    absolute = os.path.abspath(os.fspath(_logical_windows_path(logical)))
    if absolute.startswith("\\\\"):
        return Path(_EXTENDED_UNC_PREFIX + absolute[2:])
    return Path(_EXTENDED_PREFIX + absolute)


def _logical_windows_path(path: Path) -> Path:
    raw = os.fspath(path)
    if raw[: len(_EXTENDED_UNC_PREFIX)].casefold() == _EXTENDED_UNC_PREFIX.casefold():
        return Path("\\\\" + raw[len(_EXTENDED_UNC_PREFIX) :])
    if raw.startswith(_EXTENDED_PREFIX):
        return Path(raw[len(_EXTENDED_PREFIX) :])
    return path
