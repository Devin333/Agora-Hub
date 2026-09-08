"""Filesystem persistence for immutable, content-addressed memory namespaces."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping

from framework.agent.artifacts.paths import resolve_artifact_descendant
from framework.agent.artifacts.stores.errors import ArtifactStoreMetadataError
from framework.agent.artifacts.stores.fs_safety import (
    is_link_or_reparse_point,
    reject_link_chain,
    verified_atomic_create,
    verified_exclusive_file_lock,
)
from framework.memory.namespace import (
    MemoryNamespaceDescriptor,
    MemoryNamespaceError,
    MemoryNamespaceRevision,
    namespace_bytes,
    namespace_revision,
)


DEFAULT_MAX_NAMESPACE_PAYLOAD_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_NAMESPACE_METADATA_BYTES = 16 * 1024
DEFAULT_MAX_NAMESPACE_RECORDS = 1024


class FilesystemMemoryNamespaceStore:
    """Persist metadata before payload without exposing a mutable latest alias."""

    is_durable = True

    def __init__(
        self,
        root: str | Path,
        *,
        max_payload_bytes: int = DEFAULT_MAX_NAMESPACE_PAYLOAD_BYTES,
        max_records: int = DEFAULT_MAX_NAMESPACE_RECORDS,
        max_metadata_bytes: int = DEFAULT_MAX_NAMESPACE_METADATA_BYTES,
    ) -> None:
        self.max_payload_bytes = _positive(max_payload_bytes, "max_payload_bytes")
        self.max_records = _positive(max_records, "max_records")
        self.max_metadata_bytes = _positive(max_metadata_bytes, "max_metadata_bytes")
        expanded = Path(root).expanduser()
        self.root = Path(os.path.abspath(expanded))
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            info = os.lstat(self.root)
        except OSError as exc:
            raise _storage_error("memory namespace store root is unavailable") from exc
        if is_link_or_reparse_point(info) or not stat.S_ISDIR(info.st_mode):
            raise _storage_error("memory namespace store root must be a real directory")

    def commit(
        self,
        revision: MemoryNamespaceRevision,
    ) -> MemoryNamespaceDescriptor:
        if not isinstance(revision, MemoryNamespaceRevision):
            raise TypeError("revision must be MemoryNamespaceRevision")
        descriptor = revision.descriptor
        self._require_descriptor_bounds(descriptor)
        metadata = namespace_bytes(descriptor.to_dict())
        if len(metadata) > self.max_metadata_bytes:
            raise _limit_error("memory namespace metadata exceeds its configured limit")

        # Revalidate values supplied by alternate trusted writers before writing.
        revision = MemoryNamespaceRevision(descriptor, revision.payload)
        payload_path, metadata_path, lock_path = self._paths(descriptor.exact_ref)
        try:
            with verified_exclusive_file_lock(
                lock_path,
                root=self.root,
                identity=descriptor.exact_ref,
            ):
                payload_info = self._stat_or_none(payload_path, role="payload")
                metadata_info = self._stat_or_none(metadata_path, role="metadata")
                if (payload_info is None) != (metadata_info is None):
                    raise _incomplete("memory namespace revision is only partially committed")
                if payload_info is not None:
                    existing = self.describe(descriptor.exact_ref)
                    if existing != descriptor:
                        raise _conflict("memory namespace revision has different metadata")
                    if self.read(descriptor.exact_ref) != revision:
                        raise _conflict("memory namespace revision has different payload")
                    return descriptor

                if not verified_atomic_create(
                    metadata_path,
                    metadata,
                    root=self.root,
                    identity=f"{descriptor.exact_ref}/metadata",
                ):
                    raise _conflict("memory namespace metadata appeared during commit")
                if not verified_atomic_create(
                    payload_path,
                    revision.payload,
                    root=self.root,
                    identity=descriptor.exact_ref,
                ):
                    raise _conflict("memory namespace payload appeared during commit")

                stored = self.read(descriptor.exact_ref)
                if stored != revision:
                    raise _corrupt("memory namespace revision changed during commit")
                return stored.descriptor
        except MemoryNamespaceError:
            raise
        except (ArtifactStoreMetadataError, OSError) as exc:
            raise _storage_error("memory namespace commit failed") from exc

    def describe(self, ref: str) -> MemoryNamespaceDescriptor | None:
        payload_path, metadata_path, _ = self._paths(ref)
        payload_info = self._stat_or_none(payload_path, role="payload")
        metadata_info = self._stat_or_none(metadata_path, role="metadata")
        if payload_info is None and metadata_info is None:
            return None
        if payload_info is None or metadata_info is None:
            raise _incomplete("memory namespace revision is only partially committed")
        if payload_info.st_size > self.max_payload_bytes:
            raise _limit_error("memory namespace payload exceeds its configured limit")
        if metadata_info.st_size > self.max_metadata_bytes:
            raise _limit_error("memory namespace metadata exceeds its configured limit")

        try:
            raw = self._read_regular(
                metadata_path,
                before=metadata_info,
                limit=self.max_metadata_bytes,
                role="metadata",
            )
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, Mapping) or namespace_bytes(value) != raw:
                raise _corrupt("memory namespace metadata is not canonical")
            descriptor = MemoryNamespaceDescriptor.from_dict(value)
        except MemoryNamespaceError as exc:
            if exc.code in {
                "MEMORY_NAMESPACE_INCOMPLETE",
                "MEMORY_NAMESPACE_LIMIT_EXCEEDED",
            }:
                raise
            raise _corrupt("memory namespace metadata is corrupt") from exc
        except (json.JSONDecodeError, TypeError, UnicodeError, ValueError) as exc:
            raise _corrupt("memory namespace metadata is corrupt") from exc

        self._require_descriptor_bounds(descriptor)
        if descriptor.exact_ref != ref:
            raise _corrupt("memory namespace metadata does not match its exact reference")
        expected_payload, expected_metadata, _ = self._paths(descriptor.exact_ref)
        if expected_payload != payload_path or expected_metadata != metadata_path:
            raise _corrupt("memory namespace metadata path identity mismatch")
        if descriptor.payload_size_bytes != payload_info.st_size:
            raise _corrupt("memory namespace payload size differs from metadata")

        current = self._stat_or_none(payload_path, role="payload")
        if (
            current is None
            or not os.path.samestat(current, payload_info)
            or current.st_size != payload_info.st_size
            or current.st_mtime_ns != payload_info.st_mtime_ns
        ):
            raise _corrupt("memory namespace payload changed during metadata read")
        return descriptor

    def read(self, ref: str) -> MemoryNamespaceRevision:
        descriptor = self.describe(ref)
        if descriptor is None:
            raise MemoryNamespaceError(
                "memory namespace revision is missing",
                code="MEMORY_NAMESPACE_MISSING",
            )
        payload_path, _, _ = self._paths(ref)
        before = self._stat_or_none(payload_path, role="payload")
        if before is None:
            raise _incomplete("memory namespace payload is missing")
        payload = self._read_regular(
            payload_path,
            before=before,
            limit=self.max_payload_bytes,
            role="payload",
        )
        try:
            return MemoryNamespaceRevision(descriptor, payload)
        except MemoryNamespaceError as exc:
            raise _corrupt("memory namespace payload is corrupt") from exc

    def _read_regular(
        self,
        path: Path,
        *,
        before: os.stat_result,
        limit: int,
        role: str,
    ) -> bytes:
        if before.st_size < 1:
            raise _corrupt(f"memory namespace {role} is empty")
        if before.st_size > limit:
            raise _limit_error(f"memory namespace {role} exceeds its configured limit")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not os.path.samestat(before, opened):
                    raise _corrupt(f"memory namespace {role} changed while opening")
                with os.fdopen(descriptor, "rb") as handle:
                    descriptor = -1
                    content = handle.read(limit + 1)
                    finished = os.fstat(handle.fileno())
            finally:
                if descriptor != -1:
                    os.close(descriptor)
            after = self._stat_or_none(path, role=role)
        except MemoryNamespaceError:
            raise
        except FileNotFoundError as exc:
            raise _incomplete(f"memory namespace {role} disappeared during read") from exc
        except OSError as exc:
            raise _corrupt(f"memory namespace {role} cannot be read") from exc
        if (
            after is None
            or not os.path.samestat(opened, after)
            or any(
                item.st_size != before.st_size or item.st_mtime_ns != before.st_mtime_ns
                for item in (opened, finished, after)
            )
            or len(content) != before.st_size
        ):
            raise _corrupt(f"memory namespace {role} changed during read")
        return content

    def _stat_or_none(self, path: Path, *, role: str) -> os.stat_result | None:
        try:
            reject_link_chain(
                path,
                root=self.root,
                identity=path.name,
                role=f"memory namespace {role}",
            )
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        except ArtifactStoreMetadataError as exc:
            raise _corrupt(f"memory namespace {role} path is unsafe") from exc
        except OSError as exc:
            raise _storage_error(f"memory namespace {role} path is unavailable") from exc
        if is_link_or_reparse_point(info) or not stat.S_ISREG(info.st_mode):
            raise _corrupt(f"memory namespace {role} must be a regular file")
        if info.st_nlink != 1:
            raise _corrupt(f"memory namespace {role} must not have hard links")
        return info

    def _require_descriptor_bounds(self, descriptor: MemoryNamespaceDescriptor) -> None:
        if descriptor.record_count > self.max_records:
            raise _limit_error("memory namespace record count exceeds its configured limit")
        if descriptor.payload_size_bytes > self.max_payload_bytes:
            raise _limit_error("memory namespace payload exceeds its configured limit")

    def _paths(self, ref: str) -> tuple[Path, Path, Path]:
        digest = namespace_revision(ref)
        return tuple(
            resolve_artifact_descendant(
                self.root,
                f"{digest}.{suffix}",
                field="memory namespace storage path",
            )
            for suffix in ("json", "meta", "lock")
        )  # type: ignore[return-value]


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _storage_error(message: str) -> MemoryNamespaceError:
    return MemoryNamespaceError(message, code="MEMORY_NAMESPACE_STORAGE_UNAVAILABLE")


def _limit_error(message: str) -> MemoryNamespaceError:
    return MemoryNamespaceError(message, code="MEMORY_NAMESPACE_LIMIT_EXCEEDED")


def _incomplete(message: str) -> MemoryNamespaceError:
    return MemoryNamespaceError(message, code="MEMORY_NAMESPACE_INCOMPLETE")


def _conflict(message: str) -> MemoryNamespaceError:
    return MemoryNamespaceError(message, code="MEMORY_NAMESPACE_CONFLICT")


def _corrupt(message: str) -> MemoryNamespaceError:
    return MemoryNamespaceError(message, code="MEMORY_NAMESPACE_CORRUPT")


__all__ = [
    "DEFAULT_MAX_NAMESPACE_METADATA_BYTES",
    "DEFAULT_MAX_NAMESPACE_PAYLOAD_BYTES",
    "DEFAULT_MAX_NAMESPACE_RECORDS",
    "FilesystemMemoryNamespaceStore",
]
