from __future__ import annotations

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

from framework.shared import windows_paths as long_paths
from infrastructure.storage import json_file_store
from infrastructure.storage.json_file_store import (
    JsonFileInvalidError,
    locked_json_file,
    read_json_object_unlocked,
    write_json_object_unlocked,
)


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
    target = parent / "research-parse-1234567890abcdef.intent.json"
    assert len(os.fspath(target.parent)) > 260
    assert len(os.fspath(target)) > 260
    assert target.is_relative_to(owned_storage_root)
    return target

def test_non_windows_path_branch_preserves_existing_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(long_paths, "_WINDOWS", False)
    monkeypatch.chdir(tmp_path)
    relative = Path("storage") / ".." / "state.json"

    assert long_paths.storage_io_path(relative) == relative
    assert long_paths.storage_io_path(str(relative)) == relative
    assert long_paths.resolve_storage_path(relative) == relative.resolve()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows drive and UNC path semantics")
@pytest.mark.parametrize("as_string", [False, True])
def test_windows_relative_and_extended_paths_normalize_without_prefix_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_string: bool,
) -> None:
    monkeypatch.chdir(tmp_path)
    relative = Path("nested") / ".." / "state.json"
    source = str(relative) if as_string else relative
    expected = tmp_path / "state.json"

    io_path = long_paths.storage_io_path(source)

    assert io_path == _native_io_path(expected)
    assert long_paths.storage_io_path(io_path) == io_path
    assert long_paths.resolve_storage_path(source) == expected.resolve()
    assert long_paths.resolve_storage_path(io_path) == expected.resolve()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows drive and UNC path semantics")
@pytest.mark.parametrize("as_string", [False, True])
def test_windows_unc_conversion_is_local_and_idempotent(as_string: bool) -> None:
    # No network share is contacted: this checks only the local namespace conversion.
    logical = Path(r"\\server\share\folder\..\payload.json")
    source = str(logical) if as_string else logical
    expected_io = Path(r"\\?\UNC\server\share\payload.json")

    assert long_paths.storage_io_path(source) == expected_io
    assert long_paths.storage_io_path(expected_io) == expected_io
    assert long_paths._logical_windows_path(expected_io) == Path(r"\\server\share\payload.json")
    assert long_paths.storage_io_path(r"\\?\unc\server\share\payload.json") == expected_io


@pytest.mark.parametrize("as_string", [False, True])
def test_real_long_parent_lock_read_write_and_atomic_replace(
    long_target: Path,
    monkeypatch: pytest.MonkeyPatch,
    as_string: bool,
) -> None:
    replacements: list[tuple[Path, Path]] = []
    fsync_calls: list[int] = []
    real_replace = Path.replace
    real_fsync = os.fsync

    def sync_before_replace(fd: int) -> None:
        assert not replacements
        real_fsync(fd)
        fsync_calls.append(fd)

    def replace_synced_temporary(temporary: Path, target: Path) -> Path:
        assert fsync_calls
        assert len(os.fspath(temporary)) > 260
        assert re.fullmatch(
            rf"{re.escape(long_target.name)}\.{os.getpid()}\.{threading.get_ident()}\.[0-9a-f]{{32}}\.tmp",
            temporary.name,
        )
        assert json.loads(temporary.read_text(encoding="utf-8"))["publicPath"] == str(long_target)
        replacements.append((temporary, target))
        return real_replace(temporary, target)

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
    assert replacements[0] == (long_paths.storage_io_path(replacements[0][0]), _native_io_path(long_target))
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
import sys
from pathlib import Path
from infrastructure.storage.json_file_store import locked_json_file, read_json_object_unlocked, write_json_object_unlocked
path, ready, entered = map(Path, sys.argv[1:])
ready.write_text('ready', encoding='utf-8')
with locked_json_file(path) as resolved:
    entered.write_text('entered', encoding='utf-8')
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
            while not ready.exists():
                assert child.poll() is None, child.communicate(timeout=5)
                assert time.monotonic() < deadline, "child did not reach the real lock"
                time.sleep(0.01)
            time.sleep(0.15)
            assert child.poll() is None, child.communicate(timeout=5)
            assert not entered.exists(), "a different process entered the held file lock"
        stdout, stderr = child.communicate(timeout=15)
        assert child.returncode == 0, stderr
        assert json.loads(stdout) == {"path": str(long_target), "value": {"revision": 2}}
        assert entered.read_text(encoding="utf-8") == "entered"
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


def test_research_run_intent_uses_real_atomic_storage_with_long_temporary_path(
    owned_storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infrastructure.research.catalog_store import FilesystemResearchEventSink

    root = owned_storage_root / "paper-catalog"
    run_id = "research-parse-" + "1234567890abcdef" * 2
    scope = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "memory_namespace": "research:tenant:tenant-a:user:user-a",
    }
    sink = FilesystemResearchEventSink(root)
    target = sink._run_record_path(run_id, scope, "intent")
    while len(os.fspath(target)) <= 225:
        root /= "intent-storage-abcdefghij"
        sink = FilesystemResearchEventSink(root)
        target = sink._run_record_path(run_id, scope, "intent")
    replacements: list[Path] = []
    real_replace = Path.replace

    def replace_intent(temporary: Path, destination: Path) -> Path:
        assert len(os.fspath(temporary)) > 260
        assert destination == _native_io_path(target)
        replacements.append(temporary)
        return real_replace(temporary, destination)

    monkeypatch.setattr(Path, "replace", replace_intent)
    # Keep the caller's logical Path operations long-path capable too, as they are in the
    # imported clean-room runtime that exposed this storage failure.
    original_exists = Path.exists

    def long_path_exists(path: Path) -> bool:
        if path == target:
            return original_exists(_native_io_path(path))
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", long_path_exists)
    sink.create_run_intent(run_id, request_fingerprint="original-fingerprint", actor_scope=scope)

    payload = read_json_object_unlocked(target, default={}, strict=True)
    assert payload["run_id"] == run_id
    assert payload["record_id"] == f"{run_id}:intent"
    assert payload["request_fingerprint"] == "original-fingerprint"
    assert payload["actor_scope"] == scope
    assert payload["status"] == "pending"
    assert len(replacements) == 1
    assert _temporary_siblings(target) == []


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


@pytest.mark.skipif(os.name != "nt", reason="requires Windows UNC path semantics")
@pytest.mark.parametrize("source", [
    r"\\server\share\folder\..\journal.json",
    Path(r"\\server\share\folder\..\journal.json"),
    r"\\?\UNC\server\share\folder\..\journal.json",
    r"\\?\unc\server\share\journal.json",
])
def test_windows_unc_conversion_does_not_resolve_or_touch_share(
    source: str | Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_io(*_args, **_kwargs):
        pytest.fail("UNC namespace conversion must not perform filesystem or share I/O")

    with monkeypatch.context() as guard:
        guard.setattr(Path, "resolve", unexpected_io)
        guard.setattr(Path, "stat", unexpected_io)
        guard.setattr(Path, "open", unexpected_io)
        guard.setattr(os.path, "realpath", unexpected_io)
        converted = long_paths.storage_io_path(source)
        assert converted == Path(r"\\?\UNC\server\share\journal.json")
        assert long_paths.storage_io_path(converted) == converted
