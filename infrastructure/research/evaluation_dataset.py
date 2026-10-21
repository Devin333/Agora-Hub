"""Safe filesystem reads and atomic, read-only dataset revision publication.

Publication is a same-filesystem, no-replace directory commit. Only the three
byte objects and a strict storage receipt are staged; no codec or application
logic is imported here. Root/administrator tampering is outside the permission
policy, and remains detectable through pinned logical and byte digests.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import sys
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.research.ports.evaluation_dataset import (
    DatasetBundleBytes,
    DatasetObjectDigests,
    DatasetPublicationConflict,
    DatasetPublicationError,
    DatasetPublicationParent,
    DatasetPublicationReceipt,
    DatasetPublishPort,
    DatasetReadPort,
    DatasetRevision,
    DatasetRevisionNotFoundError,
)


DEFAULT_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
DEFAULT_JSONL_MAX_BYTES = 64 * 1024 * 1024
_RECEIPT_FILE = ".publication.json"
_RECEIPT_MAX_BYTES = 256 * 1024
_DATASET_OBJECTS = (
    ("manifest.json", "manifest_json"),
    ("questions.jsonl", "questions_jsonl"),
    ("references.jsonl", "references_jsonl"),
)


class EvaluationDatasetReadError(OSError):
    """A sanitized, machine-classifiable dataset storage failure."""

    def __init__(self, *, code: str, location: str, reason: str) -> None:
        self.code = code
        self.location = location
        self.reason = reason
        super().__init__(reason)


class _DatasetFilesystem:
    """Shared confinement, concrete bounded reads and permission checks."""

    def __init__(
        self,
        dataset_root: str | os.PathLike[str],
        *,
        manifest_max_bytes: int = DEFAULT_MANIFEST_MAX_BYTES,
        jsonl_max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
    ) -> None:
        self._limits = {
            "manifest.json": _positive_limit(manifest_max_bytes, "manifest_max_bytes"),
            "questions.jsonl": _positive_limit(jsonl_max_bytes, "jsonl_max_bytes"),
            "references.jsonl": _positive_limit(jsonl_max_bytes, "jsonl_max_bytes"),
            _RECEIPT_FILE: _RECEIPT_MAX_BYTES,
        }
        root = Path(dataset_root).expanduser().absolute()
        # Resolving first would hide junctions/symlinks in configured roots.
        for component in (*reversed(root.parents), root):
            self._reject_link(component, location="dataset_root")
        try:
            self._root = root.resolve(strict=True)
            metadata = self._root.stat()
        except (OSError, RuntimeError, ValueError) as exc:
            raise EvaluationDatasetReadError(
                code="dataset.root_unavailable", location="dataset_root",
                reason="dataset root is unavailable",
            ) from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise EvaluationDatasetReadError(
                code="dataset.root_not_directory", location="dataset_root",
                reason="dataset root must be a directory",
            )
        self._root_identity = _identity(metadata)

    @property
    def dataset_root(self) -> Path:
        return self._root

    def _assert_root(self) -> None:
        self._reject_link(self._root, location="dataset_root")
        try:
            metadata = self._root.stat()
        except OSError as exc:
            raise EvaluationDatasetReadError(
                code="dataset.root_unavailable", location="dataset_root",
                reason="dataset root is unavailable",
            ) from exc
        if _identity(metadata) != self._root_identity:
            raise EvaluationDatasetReadError(
                code="dataset.root_changed", location="dataset_root",
                reason="dataset root changed after adapter construction",
            )

    def _revision_path(self, revision: DatasetRevision) -> Path:
        if type(revision) is not DatasetRevision:
            raise TypeError("revision must be a DatasetRevision")
        # Re-validate rather than trusting unsafe mutation of a frozen DTO.
        DatasetRevision(revision.dataset_id, revision.dataset_version)
        self._assert_root()
        parent = self._root / revision.dataset_id
        path = parent / revision.dataset_version
        self._reject_link(parent, location="dataset_id")
        self._reject_link(path, location="dataset_version")
        self._resolve_confined(parent, location="dataset_id", strict=False)
        self._resolve_confined(path, location="dataset_version", strict=False)
        return path

    def _resolve_confined(self, path: Path, *, location: str, strict: bool) -> Path:
        try:
            resolved = path.resolve(strict=strict)
        except (OSError, RuntimeError, ValueError) as exc:
            raise EvaluationDatasetReadError(
                code="dataset.path_unavailable", location=location,
                reason="dataset path could not be resolved",
            ) from exc
        root_comparison = _path_for_comparison(self._root)
        try:
            common = os.path.commonpath((
                root_comparison,
                _path_for_comparison(resolved),
            ))
        except ValueError as exc:
            common = None
            comparison_error = exc
        else:
            comparison_error = None
        if common != root_comparison:
            raise EvaluationDatasetReadError(
                code="dataset.path_escape", location=location,
                reason="dataset path resolves outside the configured root",
            ) from comparison_error
        return resolved

    def _require_directory(self, path: Path, *, location: str = "revision") -> None:
        self._reject_link(path, location=location)
        try:
            metadata = path.stat()
        except FileNotFoundError as exc:
            if location == "revision":
                raise DatasetRevisionNotFoundError() from exc
            raise EvaluationDatasetReadError(
                code="dataset.path_unavailable", location=location,
                reason="dataset directory is unavailable",
            ) from exc
        except OSError as exc:
            raise EvaluationDatasetReadError(
                code="dataset.path_unavailable", location=location,
                reason="dataset directory could not be inspected",
            ) from exc
        self._resolve_confined(path, location=location, strict=True)
        if not stat.S_ISDIR(metadata.st_mode):
            raise EvaluationDatasetReadError(
                code="dataset.revision_not_directory", location=location,
                reason="dataset revision must be a directory",
            )

    def _read_objects(self, path: Path) -> DatasetBundleBytes:
        self._require_directory(path)
        return DatasetBundleBytes(**{
            field: self._read_file(path / name, object_name=name)
            for name, field in _DATASET_OBJECTS
        })

    def _read_file(self, path: Path, *, object_name: str) -> bytes:
        self._reject_link(path, location=object_name)
        try:
            metadata = path.stat()
        except OSError as exc:
            raise EvaluationDatasetReadError(
                code="dataset.object_unavailable", location=object_name,
                reason="required dataset object is unavailable",
            ) from exc
        resolved = self._resolve_confined(path, location=object_name, strict=True)
        if not stat.S_ISREG(metadata.st_mode):
            raise EvaluationDatasetReadError(
                code="dataset.object_not_file", location=object_name,
                reason="required dataset object must be a regular file",
            )
        # A hard-link alias would permit modifying a supposedly frozen object
        # through another location without changing this path's permissions.
        if metadata.st_nlink != 1:
            raise EvaluationDatasetReadError(
                code="dataset.hardlink_forbidden", location=object_name,
                reason="dataset objects must not have hard-link aliases",
            )
        max_bytes = self._limits[object_name]
        if metadata.st_size > max_bytes:
            self._too_large(object_name, max_bytes)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(resolved, flags)
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if _identity(metadata) != _identity(opened) or not stat.S_ISREG(opened.st_mode):
                    raise EvaluationDatasetReadError(
                        code="dataset.object_changed", location=object_name,
                        reason="dataset object changed while it was being opened",
                    )
                if opened.st_size > max_bytes:
                    self._too_large(object_name, max_bytes)
                payload = stream.read(max_bytes + 1)
                after = os.fstat(stream.fileno())
        except EvaluationDatasetReadError:
            raise
        except OSError as exc:
            raise EvaluationDatasetReadError(
                code="dataset.object_read_failed", location=object_name,
                reason="required dataset object could not be read",
            ) from exc
        if len(payload) > max_bytes:
            self._too_large(object_name, max_bytes)
        if (opened.st_size, opened.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise EvaluationDatasetReadError(
                code="dataset.object_changed", location=object_name,
                reason="dataset object changed while it was being read",
            )
        return payload

    @staticmethod
    def _too_large(location: str, max_bytes: int) -> None:
        raise EvaluationDatasetReadError(
            code="dataset.object_too_large", location=location,
            reason=f"dataset object exceeds the {max_bytes}-byte read limit",
        )

    @staticmethod
    def _reject_link(path: Path, *, location: str) -> None:
        link = False
        try:
            link = path.is_symlink() or getattr(path, "is_junction", lambda: False)()
            metadata = path.lstat()
            link = link or bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        except FileNotFoundError:
            if not link:
                return
        except OSError as exc:
            raise EvaluationDatasetReadError(
                code="dataset.path_unavailable", location=location,
                reason="dataset path could not be inspected",
            ) from exc
        if link:
            raise EvaluationDatasetReadError(
                code="dataset.symlink_forbidden", location=location,
                reason="symlinked or reparse-point dataset paths are not allowed",
            )

    def _read_receipt(self, path: Path) -> DatasetPublicationReceipt:
        try:
            raw = self._read_file(path / _RECEIPT_FILE, object_name=_RECEIPT_FILE)
            receipt = _decode_receipt(raw)
        except (ValueError, TypeError, KeyError) as exc:
            raise EvaluationDatasetReadError(
                code="dataset.receipt_invalid", location=_RECEIPT_FILE,
                reason="dataset publication receipt is invalid",
            ) from exc
        if receipt.disposition != "published":
            raise EvaluationDatasetReadError(
                code="dataset.receipt_invalid", location=_RECEIPT_FILE,
                reason="stored receipt must describe the original publication",
            )
        if receipt.permission_policy != _permission_policy():
            raise EvaluationDatasetReadError(
                code="dataset.permission_policy_mismatch", location=_RECEIPT_FILE,
                reason="publication permission policy does not match this platform",
            )
        self._verify_read_only(path)
        return receipt

    def _verify_read_only(self, path: Path) -> None:
        for name in (*(name for name, _ in _DATASET_OBJECTS), _RECEIPT_FILE):
            target = path / name
            self._reject_link(target, location=name)
            metadata = target.stat()
            if os.name == "nt":
                read_only = bool(metadata.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)
            else:
                read_only = not metadata.st_mode & 0o222
            if not read_only:
                raise EvaluationDatasetReadError(
                    code="dataset.read_only_policy_violated", location=name,
                    reason="published dataset file no longer satisfies its read-only policy",
                )
        if os.name != "nt" and path.stat().st_mode & 0o222:
            raise EvaluationDatasetReadError(
                code="dataset.read_only_policy_violated", location="revision",
                reason="published revision directory must not be writable",
            )

    def _verify_published_objects(
        self, path: Path, revision: DatasetRevision, objects: DatasetBundleBytes,
    ) -> DatasetPublicationReceipt:
        receipt = self._read_receipt(path)
        if receipt.revision != revision:
            raise EvaluationDatasetReadError(
                code="dataset.receipt_revision_mismatch", location=_RECEIPT_FILE,
                reason="publication receipt belongs to another dataset revision",
            )
        try:
            receipt.object_digests.verify(objects)
        except ValueError as exc:
            raise EvaluationDatasetReadError(
                code="dataset.publication_tampered", location="revision",
                reason="published dataset bytes do not match the immutable receipt",
            ) from exc
        return receipt


class FilesystemEvaluationDatasetReader(_DatasetFilesystem, DatasetReadPort):
    """Read the three bounded objects; verify a receipt when physically published."""

    def read_dataset_objects(self, revision: DatasetRevision) -> DatasetBundleBytes:
        path = self._revision_path(revision)
        objects = self._read_objects(path)
        receipt_path = path / _RECEIPT_FILE
        self._reject_link(receipt_path, location=_RECEIPT_FILE)
        if receipt_path.exists():
            self._verify_published_objects(path, revision, objects)
        return objects


class FilesystemEvaluationDatasetPublisher(_DatasetFilesystem, DatasetPublishPort):
    """Publish a complete read-only revision, never replace an existing target.

    Concurrent writers stage independently. An atomic no-replace commit selects
    one winner; identical losers verify/reuse the winner, different losers fail.
    There are no persistent/stale lock files and no recursive cleanup operations.
    """

    def publish_dataset_objects(
        self,
        revision: DatasetRevision,
        objects: DatasetBundleBytes,
        *,
        dataset_digest: str,
        parent: DatasetPublicationParent | None = None,
        change_scope: tuple[str, ...] = (),
    ) -> DatasetPublicationReceipt:
        path = self._revision_path(revision)
        object_digests = DatasetObjectDigests.from_objects(objects)
        candidate = DatasetPublicationReceipt(
            revision=revision, dataset_digest=dataset_digest,
            object_digests=object_digests, published_at=datetime.now(timezone.utc),
            disposition="published", permission_policy=_permission_policy(),
            read_only=True, parent=parent, change_scope=change_scope,
        )
        for name, field in _DATASET_OBJECTS:
            if len(getattr(objects, field)) > self._limits[name]:
                self._too_large(name, self._limits[name])
        if path.exists():
            return self._reuse(path, candidate, objects)
        if parent is not None:
            parent_path = self._revision_path(parent.revision)
            parent_objects = self._read_objects(parent_path)
            parent_receipt = self._verify_published_objects(parent_path, parent.revision, parent_objects)
            if parent_receipt.dataset_digest != parent.dataset_digest:
                raise DatasetPublicationConflict(
                    code="dataset.parent_digest_mismatch", location="parent",
                    reason="correction parent does not match its pinned publication digest",
                )
        parent_dir = path.parent
        try:
            parent_dir.mkdir(mode=0o700, exist_ok=True)
            self._require_directory(parent_dir, location="dataset_id")
        except OSError as exc:
            raise DatasetPublicationError(
                code="dataset.publish_parent_unavailable", location="dataset_id",
                reason="dataset publication directory is unavailable",
            ) from exc

        staging = parent_dir / f".publish-{uuid.uuid4().hex}"
        owned_files: dict[str, tuple[int, int]] = {}
        staging_identity: tuple[int, int] | None = None
        committed = False
        try:
            staging.mkdir(mode=0o700)
            staging_identity = _identity(staging.stat())
            for name, field in _DATASET_OBJECTS:
                self._write_owned_file(staging, name, getattr(objects, field), owned_files)
            self._write_owned_file(staging, _RECEIPT_FILE, _encode_receipt(candidate), owned_files)
            self._set_read_only(staging)
            # Verify on-disk bytes, identity and policy BEFORE exposing a version.
            staged_objects = self._read_objects(staging)
            self._verify_published_objects(staging, revision, staged_objects)
            if staged_objects != objects:
                raise DatasetPublicationError(
                    code="dataset.staging_mismatch", location="revision",
                    reason="staged dataset bytes do not match the publication input",
                )
            _sync_directory(staging)
            self._revision_path(revision)
            self._require_directory(staging, location="staging")
            if _identity(staging.stat()) != staging_identity or {
                member.name for member in staging.iterdir()
            } != set(owned_files):
                raise DatasetPublicationError(
                    code="dataset.staging_changed", location="staging",
                    reason="staging identity or membership changed before publication",
                )
            try:
                _rename_no_replace(staging, path)
            except OSError:
                # Windows may report an existing directory as access denied;
                # inspect/verify it rather than assuming every collision is EEXIST.
                if path.exists():
                    return self._reuse(path, candidate, objects)
                raise
            committed = True
            # A post-commit durability failure must not delete a committed version.
            _sync_directory(parent_dir)
            return candidate
        except (EvaluationDatasetReadError, DatasetPublicationError):
            raise
        except OSError as exc:
            raise DatasetPublicationError(
                code="dataset.publish_failed", location="revision",
                reason="dataset publication failed; no existing revision was overwritten",
            ) from exc
        finally:
            if staging_identity is not None and not committed:
                self._cleanup_owned_staging(staging, staging_identity, owned_files)

    def _reuse(
        self, path: Path, candidate: DatasetPublicationReceipt, objects: DatasetBundleBytes,
    ) -> DatasetPublicationReceipt:
        try:
            existing_objects = self._read_objects(path)
            existing = self._verify_published_objects(path, candidate.revision, existing_objects)
        except EvaluationDatasetReadError as exc:
            raise DatasetPublicationConflict(
                code="dataset.existing_revision_unverified", location="revision",
                reason="existing revision cannot be verified as a read-only publication",
            ) from exc
        if (
            existing.dataset_digest != candidate.dataset_digest
            or existing.object_digests != candidate.object_digests
            or existing_objects != objects
            or existing.parent != candidate.parent
            or existing.change_scope != candidate.change_scope
        ):
            raise DatasetPublicationConflict(
                code="dataset.revision_conflict", location="revision",
                reason="immutable dataset revision is already bound to different bytes or lineage",
            )
        return replace(existing, disposition="reused")

    def _write_owned_file(
        self, staging: Path, name: str, payload: bytes,
        owned_files: dict[str, tuple[int, int]],
    ) -> None:
        path = staging / name
        self._resolve_confined(path, location=name, strict=False)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
        owned_files[name] = _identity(os.fstat(descriptor))
        with os.fdopen(descriptor, "wb") as stream:
            if stream.write(payload) != len(payload):
                raise OSError("short dataset object write")
            stream.flush()
            os.fsync(stream.fileno())

    def _set_read_only(self, staging: Path) -> None:
        for name in (*(name for name, _ in _DATASET_OBJECTS), _RECEIPT_FILE):
            path = staging / name
            self._reject_link(path, location=name)
            path.chmod(stat.S_IREAD if os.name == "nt" else 0o400)
        if os.name != "nt":
            staging.chmod(0o500)
        self._verify_read_only(staging)

    def _cleanup_owned_staging(
        self, staging: Path, identity: tuple[int, int], owned_files: dict[str, tuple[int, int]],
    ) -> None:
        """Remove only exact files/identities created by THIS call; never recurse."""
        self._assert_root()
        self._reject_link(staging.parent, location="dataset_id")
        self._reject_link(staging, location="staging")
        self._resolve_confined(staging, location="staging", strict=True)
        if _identity(staging.stat()) != identity or not staging.name.startswith(".publish-"):
            raise DatasetPublicationError(
                code="dataset.cleanup_ownership_lost", location="staging",
                reason="refusing cleanup because staging ownership changed",
            )
        if os.name != "nt":
            staging.chmod(0o700)
        for name, owned_identity in owned_files.items():
            path = staging / name
            self._reject_link(path, location=name)
            self._resolve_confined(path, location=name, strict=True)
            if _identity(path.stat()) != owned_identity:
                raise DatasetPublicationError(
                    code="dataset.cleanup_ownership_lost", location=name,
                    reason="refusing cleanup because temporary file ownership changed",
                )
            path.chmod(stat.S_IWRITE | stat.S_IREAD if os.name == "nt" else 0o600)
            path.unlink()
        staging.rmdir()


def _path_for_comparison(path: Path) -> str:
    """Normalize equivalent Windows DOS/extended paths for confinement checks."""

    value = os.path.normcase(os.path.normpath(os.fspath(path)))
    if os.name == "nt":
        if value.startswith("\\\\?\\unc\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return value


def _permission_policy() -> str:
    return "windows_read_only_attribute" if os.name == "nt" else "posix_read_only"


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _positive_limit(value: int, field_name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _rename_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        # Unlike POSIX rename(), Windows os.rename never replaces a destination.
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        rename = getattr(library, "renameat2", None)
        if rename is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace publication is unavailable")
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return
    # Never silently fall back to a replacing rename on unsupported platforms.
    raise OSError(errno.ENOTSUP, "atomic no-replace publication is unavailable")


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # Individual objects are fsynced; portable Windows has no dir fsync.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode_receipt(receipt: DatasetPublicationReceipt) -> bytes:
    parent = receipt.parent
    value = {
        "receipt_version": "1.0",
        "dataset_id": receipt.revision.dataset_id,
        "dataset_version": receipt.revision.dataset_version,
        "dataset_digest": receipt.dataset_digest,
        "object_digests": {
            "manifest_sha256": receipt.object_digests.manifest_sha256,
            "questions_sha256": receipt.object_digests.questions_sha256,
            "references_sha256": receipt.object_digests.references_sha256,
        },
        "published_at": receipt.published_at.isoformat(),
        "disposition": receipt.disposition,
        "permission_policy": receipt.permission_policy,
        "read_only": receipt.read_only,
        "parent": None if parent is None else {
            "dataset_id": parent.revision.dataset_id,
            "dataset_version": parent.revision.dataset_version,
            "dataset_digest": parent.dataset_digest,
            "evidence_reference": parent.evidence_reference,
        },
        "change_scope": list(receipt.change_scope),
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_receipt(raw: bytes) -> DatasetPublicationReceipt:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate receipt field")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    fields = {
        "receipt_version", "dataset_id", "dataset_version", "dataset_digest",
        "object_digests", "published_at", "disposition", "permission_policy",
        "read_only", "parent", "change_scope",
    }
    if type(value) is not dict or set(value) != fields or value["receipt_version"] != "1.0":
        raise ValueError("invalid receipt fields/version")
    digest_fields = {"manifest_sha256", "questions_sha256", "references_sha256"}
    if type(value["object_digests"]) is not dict or set(value["object_digests"]) != digest_fields:
        raise ValueError("invalid receipt object digests")
    parent = value["parent"]
    if parent is not None:
        if type(parent) is not dict or set(parent) != {
            "dataset_id", "dataset_version", "dataset_digest", "evidence_reference",
        }:
            raise ValueError("invalid receipt parent")
        parent = DatasetPublicationParent(
            revision=DatasetRevision(parent["dataset_id"], parent["dataset_version"]),
            dataset_digest=parent["dataset_digest"], evidence_reference=parent["evidence_reference"],
        )
    if type(value["change_scope"]) is not list or type(value["published_at"]) is not str:
        raise ValueError("invalid receipt change scope/timestamp")
    return DatasetPublicationReceipt(
        revision=DatasetRevision(value["dataset_id"], value["dataset_version"]),
        dataset_digest=value["dataset_digest"],
        object_digests=DatasetObjectDigests(**value["object_digests"]),
        published_at=datetime.fromisoformat(value["published_at"]),
        disposition=value["disposition"], permission_policy=value["permission_policy"],
        read_only=value["read_only"], parent=parent, change_scope=tuple(value["change_scope"]),
    )


__all__ = [
    "DEFAULT_JSONL_MAX_BYTES",
    "DEFAULT_MANIFEST_MAX_BYTES",
    "EvaluationDatasetReadError",
    "FilesystemEvaluationDatasetPublisher",
    "FilesystemEvaluationDatasetReader",
]
