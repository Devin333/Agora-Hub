from __future__ import annotations

import os
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.local_desktop.safe_tree as safe_tree_module
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.lifecycle_cancellation import DesktopStartCancelled


def _scan(root: Path):
    return safe_tree_module.scan_tree(
        root,
        filesystem_reason_code="test_tree_scan_failed",
        filesystem_message="test tree scan failed",
        node_type_reason_code="test_tree_node_invalid",
    )


def _changed_metadata(metadata: os.stat_result, **changes):
    values = {
        "st_mode": metadata.st_mode,
        "st_dev": metadata.st_dev,
        "st_ino": metadata.st_ino,
        "st_size": metadata.st_size,
        "st_mtime_ns": metadata.st_mtime_ns,
        "st_ctime_ns": metadata.st_ctime_ns,
        "st_file_attributes": getattr(metadata, "st_file_attributes", 0),
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_scan_tree_uses_long_path_lstat_when_direntry_identity_is_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    payload = b"trusted payload"
    (root / "resource.bin").write_bytes(payload)
    real_scandir = safe_tree_module.os.scandir
    direntry_stat_calls = 0

    class ZeroIdentityEntry:
        def __init__(self, entry) -> None:
            self.name = entry.name
            self.path = entry.path

        def stat(self, *, follow_symlinks: bool):
            nonlocal direntry_stat_calls
            direntry_stat_calls += 1
            assert follow_symlinks is False
            return SimpleNamespace(
                st_mode=0,
                st_dev=0,
                st_ino=0,
                st_size=0,
                st_mtime_ns=0,
                st_ctime_ns=0,
                st_file_attributes=0,
            )

    class WrappedScan:
        def __init__(self, path) -> None:
            self._inner = real_scandir(path)

        def __enter__(self):
            return iter(ZeroIdentityEntry(entry) for entry in self._inner.__enter__())

        def __exit__(self, exc_type, exc_value, traceback):
            return self._inner.__exit__(exc_type, exc_value, traceback)

    monkeypatch.setattr(safe_tree_module.os, "scandir", lambda path: WrappedScan(path))

    snapshot = _scan(root)

    assert direntry_stat_calls == 0
    assert snapshot.resources == {
        "resource.bin": (len(payload), sha256(payload).hexdigest()),
    }


def test_scan_tree_finalizes_deepest_directories_before_root_and_rechecks_ancestor_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    leaf = root / "parent" / "leaf"
    leaf.mkdir(parents=True)
    (leaf / "resource.bin").write_bytes(b"payload")
    original_lstat = safe_tree_module._lstat
    original_reject = safe_tree_module.reject_reparse_path
    directory_calls: dict[str, int] = {}
    directory_paths = {root, root / "parent", leaf}
    final_order: list[str] = []
    ancestor_checks = 0

    def track_directory_validation(path: Path, **kwargs):
        metadata = original_lstat(path, **kwargs)
        if path in directory_paths:
            relative = path.relative_to(root).as_posix()
            directory_calls[relative] = directory_calls.get(relative, 0) + 1
            if directory_calls[relative] == 4:
                final_order.append(relative)
        return metadata

    def track_ancestor_chain(path: Path, *, root: Path):
        nonlocal ancestor_checks
        ancestor_checks += 1
        return original_reject(path, root=root)

    monkeypatch.setattr(safe_tree_module, "_lstat", track_directory_validation)
    monkeypatch.setattr(safe_tree_module, "reject_reparse_path", track_ancestor_chain)

    _scan(root)

    assert final_order == ["parent/leaf", "parent", "."]
    assert directory_calls == {".": 4, "parent": 4, "parent/leaf": 4}
    assert ancestor_checks == 2


def test_scan_tree_rejects_directory_identity_change_during_final_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "resource.bin").write_bytes(b"payload")
    original_lstat = safe_tree_module._lstat
    root_calls = 0

    def changed_final_directory(path: Path, **kwargs):
        nonlocal root_calls
        metadata = original_lstat(path, **kwargs)
        if path == root:
            root_calls += 1
            if root_calls == 4:
                return _changed_metadata(metadata, st_ino=metadata.st_ino + 1)
        return metadata

    monkeypatch.setattr(safe_tree_module, "_lstat", changed_final_directory)

    with pytest.raises(DesktopOperationError) as raised:
        _scan(root)

    assert raised.value.reason_code == "test_tree_scan_failed"
    assert root_calls == 4


@pytest.mark.parametrize("changed_field", ["st_mtime_ns", "st_ctime_ns"])
def test_scan_tree_rejects_same_identity_file_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "resource.bin").write_bytes(b"payload")
    original_fstat = safe_tree_module.os.fstat
    file_fstat_calls = 0

    def changed_after_read(descriptor: int):
        nonlocal file_fstat_calls
        metadata = original_fstat(descriptor)
        file_fstat_calls += 1
        if file_fstat_calls == 2:
            return _changed_metadata(metadata, **{changed_field: getattr(metadata, changed_field) + 1})
        return metadata

    monkeypatch.setattr(safe_tree_module.os, "fstat", changed_after_read)

    with pytest.raises(DesktopOperationError) as raised:
        _scan(root)

    assert raised.value.reason_code == "test_tree_scan_failed"
    assert file_fstat_calls == 2


def test_scan_tree_accepts_real_file_written_after_creation(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    resource = root / "resource.bin"
    resource.touch()
    created = resource.lstat()
    payload = b"content written after creation"
    resource.write_bytes(payload)
    # Advance the real last-write timestamp deterministically, without sleeping
    # or relying on the resolution of a particular filesystem's clock.
    birth_ns = getattr(created, "st_birthtime_ns", created.st_mtime_ns)
    written_ns = birth_ns + 2_000_000_000
    os.utime(resource, ns=(written_ns, written_ns))
    written = resource.lstat()
    assert written.st_mtime_ns != birth_ns
    if hasattr(written, "st_birthtime_ns"):
        assert written.st_birthtime_ns == birth_ns
        assert written.st_birthtime_ns != written.st_mtime_ns

    snapshot = _scan(root)

    assert snapshot.resources == {"resource.bin": (len(payload), sha256(payload).hexdigest())}


def test_scan_tree_accepts_source_specific_ctime_with_bounded_identity_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    resource = root / "resource.bin"
    payload = b"trusted payload"
    resource.write_bytes(payload)
    original_lstat = safe_tree_module._lstat
    original_fstat = safe_tree_module.os.fstat
    path_ctime_ns = resource.lstat().st_ctime_ns
    lstat_calls: dict[Path, int] = {}
    fstat_calls = 0

    def track_lstat(path: Path, **kwargs):
        lstat_calls[path] = lstat_calls.get(path, 0) + 1
        return original_lstat(path, **kwargs)

    def source_specific_fstat(descriptor: int):
        nonlocal fstat_calls
        fstat_calls += 1
        metadata = original_fstat(descriptor)
        return _changed_metadata(metadata, st_ctime_ns=path_ctime_ns + 1)

    monkeypatch.setattr(safe_tree_module, "_lstat", track_lstat)
    monkeypatch.setattr(safe_tree_module.os, "fstat", source_specific_fstat)

    snapshot = _scan(root)

    assert snapshot.resources == {"resource.bin": (len(payload), sha256(payload).hexdigest())}
    assert lstat_calls == {root: 4, resource: 2}
    assert fstat_calls == 2


@pytest.mark.parametrize("changed_field", ["st_dev", "st_ino", "st_size", "st_mtime_ns"])
def test_scan_tree_rejects_cross_source_identity_or_content_drift_on_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "resource.bin").write_bytes(b"payload")
    original_fstat = safe_tree_module.os.fstat
    fstat_calls = 0

    def changed_on_open(descriptor: int):
        nonlocal fstat_calls
        fstat_calls += 1
        metadata = original_fstat(descriptor)
        return _changed_metadata(metadata, **{changed_field: getattr(metadata, changed_field) + 1})

    monkeypatch.setattr(safe_tree_module.os, "fstat", changed_on_open)

    with pytest.raises(DesktopOperationError) as raised:
        _scan(root)

    assert raised.value.reason_code == "test_tree_scan_failed"
    assert fstat_calls == 1


@pytest.mark.parametrize(
    ("changed_field", "reason_code"),
    [
        ("st_dev", "test_tree_scan_failed"),
        ("st_ino", "test_tree_scan_failed"),
        ("st_size", "test_tree_scan_failed"),
        ("st_mtime_ns", "test_tree_scan_failed"),
        ("st_ctime_ns", "test_tree_scan_failed"),
        ("st_file_attributes", "desktop_reparse_point_rejected"),
    ],
)
def test_scan_tree_rejects_path_change_after_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_field: str,
    reason_code: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    resource = root / "resource.bin"
    resource.write_bytes(b"payload")
    original_lstat = safe_tree_module._lstat
    file_lstat_calls = 0

    def changed_after_read(path: Path, **kwargs):
        nonlocal file_lstat_calls
        metadata = original_lstat(path, **kwargs)
        if path == resource:
            file_lstat_calls += 1
            if file_lstat_calls == 2:
                changed_value = (
                    getattr(metadata, "st_file_attributes", 0) | 0x400
                    if changed_field == "st_file_attributes"
                    else getattr(metadata, changed_field) + 1
                )
                return _changed_metadata(metadata, **{changed_field: changed_value})
        return metadata

    monkeypatch.setattr(safe_tree_module, "_lstat", changed_after_read)

    with pytest.raises(DesktopOperationError) as raised:
        _scan(root)

    assert raised.value.reason_code == reason_code
    assert file_lstat_calls == 2


def test_scan_tree_checks_startup_cancellation_between_file_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "large-resource.bin").write_bytes(b"x" * 128)
    checks = 0

    def cancel_during_hash() -> None:
        nonlocal checks
        checks += 1
        if checks == 12:
            raise DesktopStartCancelled()

    monkeypatch.setattr(safe_tree_module, "_READ_CHUNK_SIZE", 4)
    monkeypatch.setattr(safe_tree_module, "check_startup_cancellation", cancel_during_hash)

    with pytest.raises(DesktopStartCancelled) as raised:
        _scan(root)

    assert raised.value.reason_code == "desktop_start_cancelled"
    assert checks == 12
