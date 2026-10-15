from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from framework.shared.windows_paths import resolve_storage_path, storage_io_path

_PATH_LOCKS: dict[Path, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class JsonFileInvalidError(RuntimeError):
    """Raised when a durable JSON state file cannot be decoded safely."""


@contextmanager
def locked_json_file(path: str | Path) -> Iterator[Path]:
    resolved = resolve_storage_path(path)
    storage_io_path(resolved.parent).mkdir(parents=True, exist_ok=True)
    thread_lock = _path_lock(resolved)
    with thread_lock:
        lock_path = resolved.with_name(f"{resolved.name}.lock")
        with storage_io_path(lock_path).open("a+b") as handle:
            _lock_handle(handle)
            try:
                yield resolved
            finally:
                _unlock_handle(handle)


def read_json_object(path: str | Path, *, default: Mapping[str, Any], strict: bool = False) -> dict[str, Any]:
    with locked_json_file(path) as resolved:
        return read_json_object_unlocked(resolved, default=default, strict=strict)


def read_json_object_unlocked(path: str | Path, *, default: Mapping[str, Any], strict: bool = False) -> dict[str, Any]:
    resolved = Path(path)
    io_path = storage_io_path(resolved)
    if not io_path.exists():
        return dict(default)
    try:
        payload = json.loads(io_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise JsonFileInvalidError(f"{resolved} could not be read as a JSON object") from exc
        return dict(default)
    if not isinstance(payload, Mapping):
        if strict:
            raise JsonFileInvalidError(f"{resolved} JSON root must be an object")
        return dict(default)
    return dict(payload)


def write_json_object(path: str | Path, payload: Mapping[str, Any]) -> None:
    with locked_json_file(path) as resolved:
        write_json_object_unlocked(resolved, payload)


def write_json_object_unlocked(path: str | Path, payload: Mapping[str, Any]) -> None:
    resolved = Path(path)
    storage_io_path(resolved.parent).mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(
        f"{resolved.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"
    )
    temporary_io = storage_io_path(temporary)
    resolved_io = storage_io_path(resolved)
    temporary_created = False
    try:
        handle = temporary_io.open("x", encoding="utf-8")
        temporary_created = True
        with handle:
            handle.write(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temporary_io, resolved_io)
    except BaseException as exc:
        if temporary_created:
            try:
                _cleanup_temporary_file(temporary_io)
            except BaseException as cleanup_error:
                exc.add_note(f"Could not remove temporary JSON file {temporary}: {cleanup_error!r}")
        raise


def _path_lock(path: Path) -> threading.RLock:
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[path] = lock
        return lock


def _replace_with_retry(temp_path: Path, resolved: Path) -> None:
    attempts = 8 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            temp_path.replace(resolved)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def _cleanup_temporary_file(temporary: Path) -> None:
    attempts = 8 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            temporary.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def _lock_handle(handle: Any) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle: Any) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
