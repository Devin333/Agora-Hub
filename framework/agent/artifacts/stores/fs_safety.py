from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePath

from framework.agent.artifacts.paths import (
    artifact_path_key,
    artifact_path_relative_to,
)
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.shared.windows_paths import resolve_storage_path, storage_io_path


_REPARSE_POINT_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_FILE_LOCKS_GUARD = threading.Lock()
_FILE_LOCKS: dict[PurePath, threading.RLock] = {}


def is_link_or_reparse_point(info: os.stat_result) -> bool:
    """Return true for POSIX links and Windows symlink/junction nodes."""

    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & _REPARSE_POINT_FLAG)


@contextmanager
def verified_exclusive_file_lock(
    path: Path,
    *,
    root: Path,
    identity: str,
) -> Iterator[None]:
    """Serialize one filesystem lifecycle operation without following links."""

    canonical_root = resolve_storage_path(root)
    path, canonical_root, _ = _lexical_descendant(
        path,
        root=canonical_root,
        identity=identity,
    )
    _ensure_directory_chain(path.parent, root=canonical_root, identity=identity)
    thread_lock = _file_lock(path)
    with thread_lock:
        reject_link_chain(
            path,
            root=canonical_root,
            identity=identity,
            role="artifact lock",
        )
        existing = _regular_file_or_missing(path, identity=identity)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(storage_io_path(path), flags, 0o600)
        except OSError as exc:
            raise ArtifactStoreMetadataError(
                f"artifact lock could not be opened: {identity}"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            _require_regular(opened, identity=identity, role="artifact lock")
            committed = os.lstat(storage_io_path(path))
            _require_regular(committed, identity=identity, role="artifact lock")
            if is_link_or_reparse_point(committed) or not os.path.samestat(
                opened,
                committed,
            ):
                raise ArtifactStoreMetadataError(
                    f"artifact lock identity changed: {identity}"
                )
            if existing is not None and not os.path.samestat(existing, opened):
                raise ArtifactStoreMetadataError(
                    f"artifact lock target changed: {identity}"
                )
            _lock_descriptor(descriptor)
            try:
                yield
            finally:
                _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _file_lock(path: Path) -> threading.RLock:
    key = artifact_path_key(storage_io_path(path))
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _FILE_LOCKS[key] = lock
        return lock


def _lock_descriptor(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            # Windows may lock byte zero beyond EOF. Do not initialize the
            # file before acquisition: another process may already own that
            # byte after an empty-file stat snapshot.
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as exc:
        raise ArtifactStoreMetadataError("artifact lock acquisition failed") from exc


def _unlock_descriptor(descriptor: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError:
        pass


def verified_atomic_write(
    target: Path,
    content: bytes,
    *,
    root: Path,
    identity: str,
) -> None:
    """Atomically replace one regular file without traversing untrusted links."""

    canonical_root = resolve_storage_path(root)
    target, canonical_root, _ = _lexical_descendant(
        target,
        root=canonical_root,
        identity=identity,
    )
    _ensure_directory_chain(
        target.parent,
        root=canonical_root,
        identity=identity,
    )
    parent_before = _verified_directory(
        target.parent,
        identity=identity,
        role="parent",
    )
    target_before = _regular_file_or_missing(target, identity=identity)

    guard_descriptor = -1
    guard_path: Path | None = None
    guard_owned: os.stat_result | None = None
    directory_descriptor = -1
    temporary_path: Path | None = None
    temporary_owned: os.stat_result | None = None
    try:
        if os.name == "nt":
            guard_descriptor, guard_raw = tempfile.mkstemp(
                dir=storage_io_path(target.parent),
                prefix=".g.",
                suffix=".tmp",
            )
            guard_path = Path(guard_raw)
            guard_owned = os.fstat(guard_descriptor)
            _require_regular(guard_owned, identity=identity, role="write guard")
        else:
            flags = os.O_RDONLY
            flags |= getattr(os, "O_DIRECTORY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            directory_descriptor = os.open(storage_io_path(target.parent), flags)
            opened_parent = os.fstat(directory_descriptor)
            _require_directory(
                opened_parent,
                identity=identity,
                role="opened parent",
            )
            if not os.path.samestat(parent_before, opened_parent):
                raise ArtifactStoreMetadataError(
                    f"artifact write parent identity changed: {identity}"
                )

        descriptor, raw_path = tempfile.mkstemp(
            dir=storage_io_path(target.parent),
            prefix=".t.",
            suffix=".tmp",
        )
        temporary_path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
        temporary_owned = os.lstat(storage_io_path(temporary_path))
        _require_regular(
            temporary_owned,
            identity=identity,
            role="owned temporary file",
        )

        _assert_parent_unchanged(
            target.parent,
            expected=parent_before,
            root=canonical_root,
            identity=identity,
        )
        _assert_target_unchanged(
            target,
            expected=target_before,
            identity=identity,
        )
        if directory_descriptor != -1:
            os.replace(
                temporary_path.name,
                target.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
        else:
            os.replace(storage_io_path(temporary_path), storage_io_path(target))

        committed = os.lstat(storage_io_path(target))
        _require_regular(committed, identity=identity, role="committed target")
        if not os.path.samestat(temporary_owned, committed):
            raise ArtifactStoreMetadataError(
                f"artifact committed target identity mismatch: {identity}"
            )
        temporary_path = None
        temporary_owned = None
        _assert_parent_unchanged(
            target.parent,
            expected=parent_before,
            root=canonical_root,
            identity=identity,
        )
        _fsync_directory(target.parent, directory_descriptor)
    finally:
        if directory_descriptor != -1:
            os.close(directory_descriptor)
        if guard_descriptor != -1:
            os.close(guard_descriptor)
        if temporary_path is not None and temporary_owned is not None:
            _cleanup_owned_file(temporary_path, temporary_owned)
        if guard_path is not None and guard_owned is not None:
            _cleanup_owned_file(guard_path, guard_owned)


def verified_atomic_create(
    target: Path,
    content: bytes,
    *,
    root: Path,
    identity: str,
) -> bool:
    """Publish one immutable regular file, returning false if it already exists.

    A same-directory hard link makes publication create-if-absent on Windows and
    POSIX. The fully written temporary file remains owned by this call and is
    removed after the link succeeds or loses a concurrent race.
    """

    canonical_root = resolve_storage_path(root)
    target, canonical_root, _ = _lexical_descendant(
        target,
        root=canonical_root,
        identity=identity,
    )
    _ensure_directory_chain(target.parent, root=canonical_root, identity=identity)
    parent_before = _verified_directory(target.parent, identity=identity, role="parent")
    existing = _regular_file_or_missing(target, identity=identity)
    if existing is not None:
        return False

    descriptor, raw_path = tempfile.mkstemp(
        dir=storage_io_path(target.parent),
        # Keep staging names bounded: repeating a long target basename can
        # exceed Windows MAX_PATH even when the committed target fits.
        prefix=".artifact-",
        suffix=".tmp",
    )
    temporary_path = Path(raw_path)
    temporary_owned: os.stat_result | None = None
    try:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor != -1:
                os.close(descriptor)
        temporary_owned = os.lstat(storage_io_path(temporary_path))
        _require_regular(temporary_owned, identity=identity, role="owned temporary file")
        _assert_parent_unchanged(
            target.parent,
            expected=parent_before,
            root=canonical_root,
            identity=identity,
        )
        try:
            os.link(storage_io_path(temporary_path), storage_io_path(target))
        except FileExistsError:
            _regular_file_or_missing(target, identity=identity)
            return False
        except OSError as exc:
            if storage_io_path(target).exists():
                _regular_file_or_missing(target, identity=identity)
                return False
            raise ArtifactStoreMetadataError(
                f"immutable artifact create failed: {identity}"
            ) from exc
        committed = os.lstat(storage_io_path(target))
        _require_regular(committed, identity=identity, role="committed target")
        if not os.path.samestat(temporary_owned, committed):
            raise ArtifactStoreMetadataError(
                f"immutable artifact committed target identity mismatch: {identity}"
            )
        _assert_parent_unchanged(
            target.parent,
            expected=parent_before,
            root=canonical_root,
            identity=identity,
        )
        _fsync_directory(target.parent, -1)
        return True
    finally:
        if temporary_owned is not None:
            _cleanup_owned_file(temporary_path, temporary_owned)


def reject_link_chain(
    path: Path,
    *,
    root: Path,
    identity: str,
    role: str,
) -> None:
    canonical_root = resolve_storage_path(root)
    path, canonical_root, relative_parts = _lexical_descendant(
        path,
        root=canonical_root,
        identity=identity,
    )
    current = canonical_root
    for index, part in enumerate(relative_parts):
        current = current / part
        try:
            info = os.lstat(storage_io_path(current))
        except FileNotFoundError:
            if index == len(relative_parts) - 1:
                return
            raise
        if is_link_or_reparse_point(info):
            raise ArtifactStoreMetadataError(
                f"{role} path contains a symlink, junction, or reparse point: {identity}"
            )
        if index < len(relative_parts) - 1:
            _require_directory(info, identity=identity, role=f"{role} parent")


def _ensure_directory_chain(
    directory: Path,
    *,
    root: Path,
    identity: str,
) -> None:
    directory, root, relative_parts = _lexical_descendant(
        directory,
        root=root,
        identity=identity,
    )
    storage_io_path(root).mkdir(parents=True, exist_ok=True)
    root_info = os.lstat(storage_io_path(root))
    if is_link_or_reparse_point(root_info):
        raise ArtifactStoreMetadataError(
            f"artifact root is a symlink, junction, or reparse point: {identity}"
        )
    _require_directory(root_info, identity=identity, role="artifact root")

    current = root
    for part in relative_parts:
        current = current / part
        try:
            os.mkdir(storage_io_path(current))
        except FileExistsError:
            pass
        info = os.lstat(storage_io_path(current))
        if is_link_or_reparse_point(info):
            raise ArtifactStoreMetadataError(
                "artifact write path contains a symlink, junction, or reparse "
                f"point: {identity}"
            )
        _require_directory(info, identity=identity, role="artifact write parent")


def _assert_parent_unchanged(
    parent: Path,
    *,
    expected: os.stat_result,
    root: Path,
    identity: str,
) -> None:
    reject_link_chain(
        parent,
        root=root,
        identity=identity,
        role="artifact write",
    )
    current = _verified_directory(parent, identity=identity, role="parent")
    if not os.path.samestat(expected, current):
        raise ArtifactStoreMetadataError(
            f"artifact write parent identity changed: {identity}"
        )


def _assert_target_unchanged(
    target: Path,
    *,
    expected: os.stat_result | None,
    identity: str,
) -> None:
    current = _regular_file_or_missing(target, identity=identity)
    if expected is None:
        if current is not None:
            raise ArtifactStoreMetadataError(
                f"artifact target appeared during write: {identity}"
            )
        return
    if current is None or not os.path.samestat(expected, current):
        raise ArtifactStoreMetadataError(
            f"artifact target identity changed during write: {identity}"
        )


def _regular_file_or_missing(
    path: Path,
    *,
    identity: str,
) -> os.stat_result | None:
    try:
        info = os.lstat(storage_io_path(path))
    except FileNotFoundError:
        return None
    if is_link_or_reparse_point(info):
        raise ArtifactStoreMetadataError(
            f"artifact target is a symlink, junction, or reparse point: {identity}"
        )
    _require_regular(info, identity=identity, role="artifact target")
    return info


def _verified_directory(
    path: Path,
    *,
    identity: str,
    role: str,
) -> os.stat_result:
    info = os.lstat(storage_io_path(path))
    if is_link_or_reparse_point(info):
        raise ArtifactStoreMetadataError(
            f"{role} is a symlink, junction, or reparse point: {identity}"
        )
    _require_directory(info, identity=identity, role=role)
    return info


def _require_regular(
    info: os.stat_result,
    *,
    identity: str,
    role: str,
) -> None:
    if not stat.S_ISREG(info.st_mode) or is_link_or_reparse_point(info):
        raise ArtifactStoreMetadataError(
            f"{role} is not a regular file: {identity}"
        )


def _require_directory(
    info: os.stat_result,
    *,
    identity: str,
    role: str,
) -> None:
    if not stat.S_ISDIR(info.st_mode) or is_link_or_reparse_point(info):
        raise ArtifactStoreMetadataError(
            f"{role} is not a directory: {identity}"
        )


def _lexical_descendant(
    path: Path,
    *,
    root: Path,
    identity: str,
) -> tuple[Path, Path, tuple[str, ...]]:
    try:
        relative_parts = artifact_path_relative_to(path, root).parts
    except ValueError as exc:
        raise ArtifactStoreMetadataError(
            f"artifact path escapes the artifact root: {identity}"
        ) from exc
    # Rebase from the candidate rather than resolving ancestors: this preserves
    # lexical junction/reparse-point checks. Equivalent DOS/UNC namespaces are
    # normalized only for identity comparison and at the storage I/O boundary.
    operational_root = path
    for _ in relative_parts:
        operational_root = operational_root.parent
    return path, operational_root, tuple(relative_parts)


def _cleanup_owned_file(path: Path, owned: os.stat_result) -> None:
    try:
        current = os.lstat(storage_io_path(path))
    except FileNotFoundError:
        return
    if is_link_or_reparse_point(current) or not os.path.samestat(owned, current):
        return
    try:
        storage_io_path(path).unlink()
    except FileNotFoundError:
        return


def _fsync_directory(directory: Path, descriptor: int) -> None:
    if descriptor != -1:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        return
    if os.name == "nt":
        return
    try:
        opened = os.open(storage_io_path(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(opened)
    except OSError:
        pass
    finally:
        os.close(opened)


__all__ = [
    "is_link_or_reparse_point",
    "reject_link_chain",
    "verified_atomic_create",
    "verified_atomic_write",
    "verified_exclusive_file_lock",
]
