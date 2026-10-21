from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from interfaces.services import json_file_store
from interfaces.services.json_file_store import (
    JsonFileInvalidError,
    locked_json_file,
    read_json_object,
    read_json_object_unlocked,
    write_json_object,
    write_json_object_unlocked,
)


def test_package_import_keeps_optional_services_out_of_json_boundary() -> None:
    code = r"""
import importlib.abc
import json
import sys

class BlockCryptography(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'cryptography' or fullname.startswith('cryptography.'):
            raise ImportError('cryptography is outside the JSON service import boundary')
        return None

sys.meta_path.insert(0, BlockCryptography())
from interfaces.services.json_file_store import locked_json_file
print(json.dumps({
    'auth_loaded': 'interfaces.services.auth_service' in sys.modules,
    'callable': callable(locked_json_file),
    'redis_loaded': 'infrastructure.storage.redis_llm_cache' in sys.modules,
}))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-s", "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "auth_loaded": False,
        "callable": True,
        "redis_loaded": False,
    }


@pytest.fixture
def owned_storage_root(tmp_path: Path) -> Iterator[Path]:
    # Never shorten pytest's root. Teardown touches only this new fixture's tree.
    owned_root = tmp_path / "json-file-store-owned"
    _native_io_path(owned_root).mkdir()
    try:
        yield owned_root
    finally:
        shutil.rmtree(_native_io_path(owned_root))


@pytest.fixture
def long_target(owned_storage_root: Path) -> Path:
    parent = owned_storage_root / "paper-catalog" / "runs" / "research-scope"
    index = 0
    while len(os.fspath(parent)) <= 300:
        parent /= f"segment-{index:02d}-abcdefghijklmnopqrstuvwxyz"
        index += 1
    target = parent / ("journal-" + hashlib.sha256(b"paper-rag-session:tenant:user").hexdigest() + ".json")
    assert len(os.fspath(target.parent)) > 260
    assert len(os.fspath(target)) > 260
    assert target.is_relative_to(owned_storage_root)
    return target


@pytest.mark.parametrize("as_string", [False, True])
def test_real_long_parent_lock_read_write_and_atomic_replace(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_string: bool,
) -> None:
    replacements: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []
    temporary_modes: list[str] = []
    real_open = Path.open
    real_replace = Path.replace
    real_fsync = os.fsync

    def sync_before_replace(fd: int) -> None:
        assert not replacements
        assert os.fstat(fd).st_size > 0, "buffered JSON must be flushed before fsync"
        real_fsync(fd)
        fsync_calls.append(fd)

    def replace_synced_temporary(temporary: Path, target: Path) -> Path:
        assert fsync_calls
        assert len(os.fspath(temporary)) > 260
        assert re.fullmatch(
            rf"{re.escape(long_target.name)}\.{os.getpid()}\.{threading.get_ident()}\.[0-9a-f]{{32}}\.tmp",
            temporary.name,
        )
        assert UUID(temporary.name.split(".")[-2]).version == 4
        assert temporary_modes == ["x"]
        assert json.loads(temporary.read_text(encoding="utf-8"))["publicPath"] == str(long_target)
        replacements.append((temporary, target))
        return real_replace(temporary, target)

    def open_exclusive_temporary(path: Path, mode: str = "r", *args, **kwargs):
        if path.name.endswith(".tmp") and mode != "r":
            temporary_modes.append(mode)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_exclusive_temporary)
    monkeypatch.setattr(json_file_store.os, "fsync", sync_before_replace)
    monkeypatch.setattr(Path, "replace", replace_synced_temporary)
    source = str(long_target) if as_string else long_target
    payload = {"status": "就绪", "items": [1, 2], "publicPath": str(long_target)}

    with locked_json_file(source) as resolved:
        assert resolved == long_target
        assert not os.fspath(resolved).startswith("\\\\?\\")
        write_json_object_unlocked(resolved, payload)
        assert read_json_object_unlocked(str(resolved), default={}, strict=True) == payload

    assert _native_io_path(long_target).is_file()
    assert _native_io_path(long_target.with_name(f"{long_target.name}.lock")).read_bytes() == b"\0"
    assert len(fsync_calls) == len(replacements) == 1
    assert replacements[0] == (_native_io_path(long_target.with_name(replacements[0][0].name)), _native_io_path(long_target))
    assert json.loads(_native_io_path(long_target).read_text(encoding="utf-8")) == payload
    assert _temporary_siblings(long_target) == []


@pytest.mark.parametrize("as_string", [False, True])
def test_unlocked_relative_str_and_path_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_string: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = Path("nested") / "state.json"
    source = str(relative) if as_string else relative

    write_json_object_unlocked(source, {"revision": 1})
    assert read_json_object_unlocked(source, default={}) == {"revision": 1}
    with locked_json_file(source) as resolved:
        assert resolved == relative.resolve()
        assert not str(resolved).startswith("\\\\?\\")


def test_real_long_path_lock_serializes_another_process(long_target: Path, tmp_path: Path) -> None:
    ready = tmp_path / "child-ready"
    entered = tmp_path / "child-entered"
    child_code = """
import json
import socket
import sys
from contextlib import redirect_stdout
from pathlib import Path

def prohibit_network(*_args, **_kwargs):
    raise AssertionError('network forbidden in cross-process JSON lock test')

socket.socket.connect = prohibit_network
socket.socket.connect_ex = prohibit_network
socket.create_connection = prohibit_network
# Package initializers may emit dependency notices; stdout is the strict JSON IPC.
# Preserve those notices on stderr rather than filtering or ignoring stdout bytes.
with redirect_stdout(sys.stderr):
    from interfaces.services.json_file_store import locked_json_file, read_json_object_unlocked, write_json_object_unlocked
    from framework.shared.windows_paths import storage_io_path
path, ready, entered = map(Path, sys.argv[1:])
storage_io_path(ready).write_text('ready', encoding='utf-8')
with locked_json_file(path) as resolved:
    storage_io_path(entered).write_text('entered', encoding='utf-8')
    value = read_json_object_unlocked(resolved, default={}, strict=True)
    value['revision'] += 1
    write_json_object_unlocked(resolved, value)
    print(json.dumps({'path': str(resolved), 'value': value}), flush=True)
"""
    child: subprocess.Popen | None = None
    try:
        with locked_json_file(long_target) as resolved:
            write_json_object_unlocked(resolved, {"revision": 1})
            child = subprocess.Popen(
                [sys.executable, "-B", "-s", "-c", child_code, str(long_target), str(ready), str(entered)],
                cwd=Path(__file__).resolve().parents[3],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            deadline = time.monotonic() + 15
            while not _native_io_path(ready).exists():
                assert child.poll() is None, child.communicate(timeout=5)
                assert time.monotonic() < deadline, "child did not reach the real lock"
                time.sleep(0.01)
            time.sleep(0.15)
            assert child.poll() is None, child.communicate(timeout=5)
            assert not _native_io_path(entered).exists(), "a different process entered the held file lock"
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stderr
        assert json.loads(stdout) == {"path": str(long_target), "value": {"revision": 2}}
        assert _native_io_path(entered).read_text(encoding="utf-8") == "entered"
        assert read_json_object_unlocked(long_target, default={}, strict=True) == {"revision": 2}
        assert _temporary_siblings(long_target) == []
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows delete-sharing denial")
def test_real_windows_permission_error_retries_then_replaces(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json_object_unlocked(long_target, {"revision": 1})
    blocking_handle = _native_io_path(long_target).open("rb")
    attempts: list[Path] = []
    delays: list[float] = []
    real_replace = Path.replace

    def attempt_replace(temporary: Path, target: Path) -> Path:
        attempts.append(temporary)
        return real_replace(temporary, target)

    def release_target(delay: float) -> None:
        # First failure and its PermissionError come from the actual Windows filesystem.
        delays.append(delay)
        blocking_handle.close()

    monkeypatch.setattr(Path, "replace", attempt_replace)
    monkeypatch.setattr(json_file_store, "time", SimpleNamespace(sleep=release_target))
    try:
        write_json_object_unlocked(long_target, {"revision": 2})
    finally:
        blocking_handle.close()

    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert delays == [0.01]
    assert read_json_object_unlocked(long_target, default={}) == {"revision": 2}
    assert _temporary_siblings(long_target) == []


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows delete-sharing denial")
def test_real_terminal_replace_denial_preserves_old_target_and_other_writers_temp(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json_object_unlocked(long_target, {"revision": 1})
    old_bytes = _native_io_path(long_target).read_bytes()
    unrelated = long_target.with_name(f"{long_target.name}.another-writer.tmp")
    _native_io_path(unrelated).write_bytes(b"keep")
    attempts: list[Path] = []
    delays: list[float] = []
    real_replace = Path.replace

    def attempt_replace(temporary: Path, target: Path) -> Path:
        attempts.append(temporary)
        return real_replace(temporary, target)

    monkeypatch.setattr(Path, "replace", attempt_replace)
    monkeypatch.setattr(json_file_store, "time", SimpleNamespace(sleep=delays.append))
    with _native_io_path(long_target).open("rb"):
        with pytest.raises(PermissionError):
            write_json_object_unlocked(long_target, {"revision": 2})

    assert len(attempts) == 8
    assert len(set(attempts)) == 1
    assert delays == [0.01 * index for index in range(1, 8)]
    assert _native_io_path(long_target).read_bytes() == old_bytes
    assert _native_io_path(unrelated).read_bytes() == b"keep"
    assert _temporary_siblings(long_target) == [unrelated.name]


@pytest.mark.parametrize("failure", [OSError("durability failure"), KeyboardInterrupt("cancelled write")])
def test_fsync_failure_preserves_primary_error_old_target_and_cleans_temp(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    write_json_object_unlocked(long_target, {"revision": 1})
    old_bytes = _native_io_path(long_target).read_bytes()

    def fail_fsync(_fd: int) -> None:
        raise failure

    monkeypatch.setattr(json_file_store.os, "fsync", fail_fsync)
    with pytest.raises(type(failure)) as caught:
        write_json_object_unlocked(long_target, {"revision": 2})

    assert caught.value is failure
    assert _native_io_path(long_target).read_bytes() == old_bytes
    assert _temporary_siblings(long_target) == []


@pytest.mark.parametrize(
    ("payload", "error"),
    [({"bad": {1, 2}}, TypeError), ({"bad": "\ud800"}, UnicodeEncodeError)],
)
def test_serialization_or_encoding_failure_leaves_no_temp_and_old_target_unchanged(
    long_target: Path,
    payload: dict,
    error: type[Exception],
) -> None:
    write_json_object_unlocked(long_target, {"revision": 1})
    old_bytes = _native_io_path(long_target).read_bytes()

    with pytest.raises(error):
        write_json_object_unlocked(long_target, payload)

    assert _native_io_path(long_target).read_bytes() == old_bytes
    assert _temporary_siblings(long_target) == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows cleanup retry budget")
def test_cleanup_retries_transient_permission_denial_and_preserves_primary_error(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = OSError("primary durability failure")
    cleanup_attempts: list[Path] = []
    delays: list[float] = []
    real_unlink = Path.unlink

    def fail_fsync(_fd: int) -> None:
        raise failure

    def flaky_unlink(path: Path, *args, **kwargs) -> None:
        cleanup_attempts.append(path)
        if len(cleanup_attempts) < 3:
            raise PermissionError("transient cleanup denial")
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(json_file_store.os, "fsync", fail_fsync)
        patch.setattr(Path, "unlink", flaky_unlink)
        patch.setattr(json_file_store, "time", SimpleNamespace(sleep=delays.append))
        with pytest.raises(OSError) as caught:
            write_json_object_unlocked(long_target, {"revision": 2})

    assert caught.value is failure
    assert len(cleanup_attempts) == 3
    assert len(set(cleanup_attempts)) == 1
    assert delays == [0.01, 0.02]
    assert _temporary_siblings(long_target) == []


@pytest.mark.parametrize("cleanup_error", [PermissionError("cleanup denied"), OSError("cleanup I/O failure")])
def test_terminal_cleanup_failure_is_not_silent_and_does_not_mask_primary_error(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_error: OSError,
) -> None:
    failure = OSError("primary durability failure")
    cleanup_attempts: list[Path] = []

    def fail_fsync(_fd: int) -> None:
        raise failure

    def fail_unlink(path: Path, *args, **kwargs) -> None:
        cleanup_attempts.append(path)
        raise cleanup_error

    with monkeypatch.context() as patch:
        patch.setattr(json_file_store.os, "fsync", fail_fsync)
        patch.setattr(Path, "unlink", fail_unlink)
        patch.setattr(json_file_store, "time", SimpleNamespace(sleep=lambda _delay: None))
        with pytest.raises(OSError) as caught:
            write_json_object_unlocked(long_target, {"revision": 2})

    assert caught.value is failure
    expected_attempts = 8 if os.name == "nt" and isinstance(cleanup_error, PermissionError) else 1
    assert len(cleanup_attempts) == expected_attempts
    assert len(set(cleanup_attempts)) == 1
    assert len(failure.__notes__) == 1
    assert "Could not remove temporary JSON file" in failure.__notes__[0]
    assert str(cleanup_error) in failure.__notes__[0]
    leftovers = _temporary_siblings(long_target)
    assert leftovers == [cleanup_attempts[0].name]
    assert not _native_io_path(long_target).exists()
    # Persistent OS denial cannot be bypassed. After restoring access, remove this fixture's own file.
    _native_io_path(long_target.with_name(leftovers[0])).unlink()
    assert _temporary_siblings(long_target) == []


def test_existing_temporary_collision_is_neither_truncated_nor_removed(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json_object_unlocked(long_target, {"revision": 1})
    identity = UUID("f" * 32)
    collision = long_target.with_name(
        f"{long_target.name}.{os.getpid()}.{threading.get_ident()}.{identity.hex}.tmp"
    )
    _native_io_path(collision).write_bytes(b"another writer owns this")
    monkeypatch.setattr(json_file_store, "uuid4", lambda: identity)

    with pytest.raises(FileExistsError):
        write_json_object_unlocked(long_target, {"revision": 2})

    assert read_json_object_unlocked(long_target, default={}) == {"revision": 1}
    assert _native_io_path(collision).read_bytes() == b"another writer owns this"
    assert _temporary_siblings(long_target) == [collision.name]


@pytest.mark.parametrize("raw", ["[1, 2]", "{invalid"])
def test_long_path_reader_keeps_default_and_strict_validation(long_target: Path, raw: str) -> None:
    default = {"fallback": True}
    missing = read_json_object_unlocked(long_target, default=default, strict=True)
    assert missing == default
    assert missing is not default
    _native_io_path(long_target.parent).mkdir(parents=True)
    _native_io_path(long_target).write_text(raw, encoding="utf-8")

    assert read_json_object_unlocked(long_target, default=default) == default
    with pytest.raises(JsonFileInvalidError) as caught:
        read_json_object_unlocked(long_target, default=default, strict=True)
    assert not str(caught.value).startswith("\\\\?\\")


def test_non_windows_replace_and_cleanup_do_not_retry_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = tmp_path / "state.tmp"
    temporary.write_bytes(b"candidate")
    target = tmp_path / "state.json"
    attempts: list[Path] = []
    failure = PermissionError("not a Windows retry")

    def deny_replace(path: Path, _target: Path) -> Path:
        attempts.append(path)
        raise failure

    def deny_unlink(path: Path, *args, **kwargs) -> None:
        attempts.append(path)
        raise failure

    def unexpected_retry(_delay: float) -> None:
        pytest.fail("non-Windows I/O must not enter a Windows retry loop")

    with monkeypatch.context() as patch:
        # Change only this module's OS branch, not process-global os.name or pathlib.
        patch.setattr(json_file_store, "os", SimpleNamespace(name="posix"))
        patch.setattr(json_file_store, "time", SimpleNamespace(sleep=unexpected_retry))
        patch.setattr(Path, "replace", deny_replace)
        patch.setattr(Path, "unlink", deny_unlink)
        with pytest.raises(PermissionError) as caught:
            json_file_store._replace_with_retry(temporary, target)
        with pytest.raises(PermissionError) as cleanup_caught:
            json_file_store._cleanup_temporary_file(temporary)

    assert caught.value is failure
    assert len(attempts) == 2
    assert temporary.read_bytes() == b"candidate"
    assert cleanup_caught.value is failure


def test_non_permission_replace_failure_is_not_retried_and_preserves_old_target(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_json_object_unlocked(long_target, {"revision": 1})
    failure = OSError("replace failed")
    attempts: list[Path] = []

    def fail_replace(temporary: Path, target: Path) -> Path:
        attempts.append(temporary)
        raise failure

    def unexpected_retry(_delay: float) -> None:
        pytest.fail("non-permission replacement errors must propagate without retry")

    monkeypatch.setattr(Path, "replace", fail_replace)
    monkeypatch.setattr(json_file_store, "time", SimpleNamespace(sleep=unexpected_retry))
    with pytest.raises(OSError) as caught:
        write_json_object_unlocked(long_target, {"revision": 2})

    assert caught.value is failure
    assert len(attempts) == 1
    assert read_json_object_unlocked(long_target, default={}) == {"revision": 1}
    assert _temporary_siblings(long_target) == []


def _native_io_path(path: Path) -> Path:
    # Independent test oracle: do not use the production helper to prove actual I/O works.
    if os.name != "nt":
        return path
    raw = os.fspath(path.absolute())
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _temporary_siblings(target: Path) -> list[str]:
    parent_io = _native_io_path(target.parent)
    if not parent_io.exists():
        return []
    return sorted(
        candidate.name
        for candidate in parent_io.iterdir()
        if candidate.name.startswith(f"{target.name}.") and candidate.name.endswith(".tmp")
    )


@pytest.mark.parametrize("as_string", [False, True])
def test_public_wrappers_preserve_logical_path_and_long_json(
    long_target: Path,
    as_string: bool,
) -> None:
    source = str(long_target) if as_string else long_target
    default = {"status": "new"}
    first = read_json_object(source, default=default, strict=True)
    assert first == default
    assert first is not default
    write_json_object(source, {"status": "persisted", "publicPath": str(long_target)})
    assert read_json_object(source, default={}, strict=True) == {
        "status": "persisted", "publicPath": str(long_target),
    }
    with locked_json_file(source) as resolved:
        assert resolved == long_target
        assert not str(resolved).startswith("\\\\?\\")
    assert _temporary_siblings(long_target) == []


@pytest.mark.skipif(os.name != "nt", reason="requires real Win32 MAX_PATH boundary")
def test_real_long_temp_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the normal pytest root and the SHA64 business filename. Only the actual
    # PID/thread/full-UUID temporary path exceeds MAX_PATH in this reproduction.
    parent = tmp_path / "paper-rag-journal"
    name = "journal-" + hashlib.sha256(b"actual-paper-rag-session").hexdigest() + ".json"
    remaining = 240 - len(os.fspath(parent / name))
    if remaining > 1:
        parent /= "journal-storage-"[:remaining - 1].ljust(remaining - 1, "s")
    target = parent / name
    assert 230 <= len(os.fspath(target)) <= 250
    assert len(os.fspath(target.with_name(target.name + ".lock"))) < 260
    replacements: list[Path] = []
    real_replace = Path.replace

    def replace_real_temporary(temporary: Path, resolved: Path) -> Path:
        assert len(os.fspath(temporary)) > 260
        assert re.fullmatch(
            rf"{re.escape(name)}\.{os.getpid()}\.{threading.get_ident()}\.[0-9a-f]{{32}}\.tmp",
            temporary.name,
        )
        assert UUID(temporary.name.split(".")[-2]).version == 4
        replacements.append(temporary)
        return real_replace(temporary, resolved)

    monkeypatch.setattr(Path, "replace", replace_real_temporary)
    write_json_object(target, {"session": "actual-paper-rag-session", "revision": 1})
    assert read_json_object(target, default={}, strict=True) == {
        "session": "actual-paper-rag-session", "revision": 1,
    }
    # The public target and lock also work through ordinary, unprefixed Win32 paths.
    assert target.is_file()
    assert target.with_name(target.name + ".lock").is_file()
    assert len(replacements) == 1
    assert _temporary_siblings(target) == []


@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended path alias")
@pytest.mark.parametrize("as_string", [False, True])
def test_extended_alias_yields_same_public_path_and_thread_lock(
    long_target: Path,
    as_string: bool,
) -> None:
    alias = _native_io_path(long_target)
    source = str(alias) if as_string else alias
    write_json_object(source, {"revision": 1})
    with locked_json_file(source) as resolved:
        assert resolved == long_target
        assert not str(resolved).startswith("\\\\?\\")
        assert json_file_store._path_lock(resolved) is json_file_store._path_lock(long_target)
        assert read_json_object_unlocked(resolved, default={}, strict=True) == {"revision": 1}
    assert read_json_object(long_target, default={}, strict=True) == {"revision": 1}
    assert _temporary_siblings(long_target) == []


def test_two_threads_keep_atomic_update_under_the_same_lock(long_target: Path) -> None:
    write_json_object(long_target, {"revision": 0})
    start = threading.Barrier(2)
    failures: list[BaseException] = []

    def update() -> None:
        try:
            start.wait(timeout=10)
            for _ in range(10):
                with locked_json_file(long_target) as resolved:
                    value = read_json_object_unlocked(resolved, default={}, strict=True)
                    value["revision"] += 1
                    write_json_object_unlocked(resolved, value)
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=update, daemon=True) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert read_json_object(long_target, default={}, strict=True) == {"revision": 20}
    assert _temporary_siblings(long_target) == []
