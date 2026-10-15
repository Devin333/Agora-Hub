from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import NoReturn

from scripts.local_desktop.common import DesktopOperationError, reject_reparse_path, windows_extended_path

_READ_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class SafeTreeSnapshot:
    """Operation-scoped identities from one no-follow tree traversal."""

    resources: dict[str, tuple[int, str]]
    nodes: frozenset[str]
    directories: frozenset[str]
    captured: dict[str, bytes]


def scan_tree(
    root: Path,
    *,
    excluded: set[str] | frozenset[str] = frozenset(),
    captured: set[str] | frozenset[str] = frozenset(),
    deferred: Sequence[str] = (),
    filesystem_reason_code: str,
    filesystem_message: str,
    node_type_reason_code: str,
) -> SafeTreeSnapshot:
    """Validate and hash a tree once without following links or reparse points.

    The returned value is deliberately operation-scoped. Callers must not retain it
    across independent verification operations because filesystem authority must be
    re-established for every operation.
    """

    root = Path(root)
    reject_reparse_path(root, root=root)
    root_metadata = _lstat(
        root,
        filesystem_reason_code=filesystem_reason_code,
        filesystem_message=filesystem_message,
    )
    _validate_node(root, root_metadata, node_type_reason_code=node_type_reason_code, expected_kind="directory")

    resources: dict[str, tuple[int, str]] = {}
    nodes: set[str] = set()
    directories: set[str] = set()
    captured_data: dict[str, bytes] = {}
    deferred_order = tuple(dict.fromkeys(deferred))
    deferred_set = set(deferred_order)
    deferred_files: dict[str, tuple[Path, os.stat_result]] = {}
    directory_states: dict[str, tuple[Path, os.stat_result]] = {"": (root, root_metadata)}
    stack: list[tuple[Path, tuple[str, ...], os.stat_result]] = [(root, (), root_metadata)]

    while stack:
        lexical_directory, parent_parts, discovered_metadata = stack.pop()
        before_scan = _lstat(
            lexical_directory,
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
        )
        _require_unchanged_node(
            lexical_directory,
            discovered_metadata,
            before_scan,
            expected_kind="directory",
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
            node_type_reason_code=node_type_reason_code,
        )
        try:
            with os.scandir(windows_extended_path(lexical_directory)) as iterator:
                entries = sorted(iterator, key=lambda item: (item.name.casefold(), item.name))
        except OSError as exc:
            _raise_scan_error(exc, reason_code=filesystem_reason_code, message=filesystem_message)
        after_scan = _lstat(
            lexical_directory,
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
        )
        _require_unchanged_node(
            lexical_directory,
            before_scan,
            after_scan,
            expected_kind="directory",
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
            node_type_reason_code=node_type_reason_code,
        )
        directory_key = Path(*parent_parts).as_posix() if parent_parts else ""
        directory_states[directory_key] = (lexical_directory, after_scan)

        child_directories: list[tuple[Path, tuple[str, ...], os.stat_result]] = []
        for entry in entries:
            relative_parts = (*parent_parts, entry.name)
            relative = Path(*relative_parts).as_posix()
            lexical = root.joinpath(*relative_parts)
            metadata = _lstat(
                lexical,
                filesystem_reason_code=filesystem_reason_code,
                filesystem_message=filesystem_message,
            )
            node_kind = _validate_node(lexical, metadata, node_type_reason_code=node_type_reason_code)
            nodes.add(relative)
            if node_kind == "directory":
                directories.add(relative)
                child_directories.append((lexical, relative_parts, metadata))
                continue
            if relative in deferred_set:
                deferred_files[relative] = (lexical, metadata)
                continue
            _record_file(
                relative,
                lexical,
                metadata,
                resources=resources,
                captured_data=captured_data,
                excluded=excluded,
                captured=captured,
                filesystem_reason_code=filesystem_reason_code,
                filesystem_message=filesystem_message,
                node_type_reason_code=node_type_reason_code,
            )
        stack.extend(reversed(child_directories))

    for relative in deferred_order:
        pending = deferred_files.get(relative)
        if pending is None:
            continue
        lexical, metadata = pending
        _record_file(
            relative,
            lexical,
            metadata,
            resources=resources,
            captured_data=captured_data,
            excluded=excluded,
            captured=captured,
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
            node_type_reason_code=node_type_reason_code,
        )

    final_directories = sorted(
        directory_states.items(),
        key=lambda item: (-len(Path(item[0]).parts) if item[0] else 0, item[0]),
    )
    for _relative, (directory, expected_metadata) in final_directories:
        completed_metadata = _lstat(
            directory,
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
        )
        _require_unchanged_node(
            directory,
            expected_metadata,
            completed_metadata,
            expected_kind="directory",
            filesystem_reason_code=filesystem_reason_code,
            filesystem_message=filesystem_message,
            node_type_reason_code=node_type_reason_code,
        )
    reject_reparse_path(root, root=root)

    return SafeTreeSnapshot(
        resources=resources,
        nodes=frozenset(nodes),
        directories=frozenset(directories),
        captured=captured_data,
    )


def _record_file(
    relative: str,
    path: Path,
    metadata: os.stat_result,
    *,
    resources: dict[str, tuple[int, str]],
    captured_data: dict[str, bytes],
    excluded: set[str] | frozenset[str],
    captured: set[str] | frozenset[str],
    filesystem_reason_code: str,
    filesystem_message: str,
    node_type_reason_code: str,
) -> None:
    if relative in excluded and relative not in captured:
        return
    size, digest, data = _hash_file(
        path,
        metadata,
        capture=relative in captured,
        filesystem_reason_code=filesystem_reason_code,
        filesystem_message=filesystem_message,
        node_type_reason_code=node_type_reason_code,
    )
    if relative not in excluded:
        resources[relative] = (size, digest)
    if data is not None:
        captured_data[relative] = data


def _lstat(path: Path, *, filesystem_reason_code: str, filesystem_message: str) -> os.stat_result:
    try:
        return windows_extended_path(path).lstat()
    except OSError as exc:
        _raise_scan_error(exc, reason_code=filesystem_reason_code, message=filesystem_message)


def _validate_node(
    path: Path,
    metadata: os.stat_result,
    *,
    node_type_reason_code: str,
    expected_kind: str | None = None,
) -> str:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or attributes & reparse:
        raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {path.name}")
    if stat.S_ISDIR(metadata.st_mode):
        kind = "directory"
    elif stat.S_ISREG(metadata.st_mode):
        kind = "file"
    else:
        raise DesktopOperationError(node_type_reason_code, f"filesystem tree entry is not a regular file or directory: {path.name}")
    if expected_kind is not None and kind != expected_kind:
        raise DesktopOperationError(node_type_reason_code, f"filesystem tree entry changed type: {path.name}")
    _require_reliable_identity(path, metadata, reason_code=node_type_reason_code)
    return kind


def _require_reliable_identity(path: Path, metadata: os.stat_result, *, reason_code: str) -> None:
    if not getattr(metadata, "st_dev", 0) or not getattr(metadata, "st_ino", 0):
        raise DesktopOperationError(reason_code, f"filesystem tree entry identity is unavailable: {path.name}")


def _require_unchanged_node(
    path: Path,
    expected: os.stat_result,
    actual: os.stat_result,
    *,
    expected_kind: str,
    filesystem_reason_code: str,
    filesystem_message: str,
    node_type_reason_code: str,
) -> None:
    _validate_node(path, actual, node_type_reason_code=node_type_reason_code, expected_kind=expected_kind)
    if not _same_identity(expected, actual) or not _same_change_state(expected, actual):
        raise DesktopOperationError(
            filesystem_reason_code,
            f"{filesystem_message}: tree entry changed during verification",
        )


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return bool(first.st_dev and first.st_ino and first.st_dev == second.st_dev and first.st_ino == second.st_ino)


def _same_content_state(first: os.stat_result, second: os.stat_result) -> bool:
    # These fields have consistent semantics between Windows lstat and fstat.
    return (
        first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _same_change_state(first: os.stat_result, second: os.stat_result) -> bool:
    # Only compare change state from the same stat source. On Windows, lstat
    # and fstat can report different ctime semantics for an unchanged file.
    return _same_content_state(first, second) and first.st_ctime_ns == second.st_ctime_ns


def _hash_file(
    path: Path,
    metadata: os.stat_result,
    *,
    capture: bool,
    filesystem_reason_code: str,
    filesystem_message: str,
    node_type_reason_code: str,
) -> tuple[int, str, bytes | None]:
    digest = sha256()
    size = 0
    captured = bytearray() if capture else None
    try:
        with open(windows_extended_path(path), "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or not _same_identity(metadata, opened) or not _same_content_state(metadata, opened):
                raise DesktopOperationError(
                    filesystem_reason_code,
                    f"{filesystem_message}: tree entry changed while it was being opened",
                )
            while chunk := handle.read(_READ_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
                if captured is not None:
                    captured.extend(chunk)
            completed = os.fstat(handle.fileno())
            if not _same_identity(opened, completed) or not _same_change_state(opened, completed) or size != completed.st_size:
                raise DesktopOperationError(
                    filesystem_reason_code,
                    f"{filesystem_message}: resource changed while it was being read",
                )
            completed_path = _lstat(
                path,
                filesystem_reason_code=filesystem_reason_code,
                filesystem_message=filesystem_message,
            )
            _require_unchanged_node(
                path,
                metadata,
                completed_path,
                expected_kind="file",
                filesystem_reason_code=filesystem_reason_code,
                filesystem_message=filesystem_message,
                node_type_reason_code=node_type_reason_code,
            )
            if not _same_identity(completed_path, completed) or not _same_content_state(completed_path, completed):
                raise DesktopOperationError(
                    filesystem_reason_code,
                    f"{filesystem_message}: resource path changed while it was being read",
                )
    except DesktopOperationError:
        raise
    except OSError as exc:
        _raise_scan_error(exc, reason_code=filesystem_reason_code, message=filesystem_message)
    return size, digest.hexdigest(), bytes(captured) if captured is not None else None


def _raise_scan_error(error: OSError, *, reason_code: str, message: str) -> NoReturn:
    raise DesktopOperationError(
        reason_code,
        message,
        details={"winerror": getattr(error, "winerror", None), "errno": error.errno},
    ) from error
