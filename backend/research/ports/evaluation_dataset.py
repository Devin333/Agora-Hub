"""Storage-facing contracts for immutable Research evaluation dataset objects.

Only concrete bytes, portable identities and publication metadata cross these
ports. No codec, application orchestration, labels or lazy handles live here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)


@dataclass(frozen=True, slots=True)
class DatasetBundleBytes:
    """Exactly the three concrete storage objects consumed by the codec."""

    manifest_json: bytes
    questions_jsonl: bytes
    references_jsonl: bytes

    def __post_init__(self) -> None:
        for field_name in ("manifest_json", "questions_jsonl", "references_jsonl"):
            if type(getattr(self, field_name)) is not bytes:
                raise TypeError(f"{field_name} must be concrete bytes")


@dataclass(frozen=True, slots=True)
class DatasetRevision:
    dataset_id: str
    dataset_version: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("dataset_id", self.dataset_id),
            ("dataset_version", self.dataset_version),
        ):
            if type(value) is not str:
                raise TypeError(f"{field_name} must be a strict string")
            if (
                not _SAFE_COMPONENT.fullmatch(value)
                or len(value) > 255
                or value.endswith(".")
                or value.partition(".")[0].casefold() in _WINDOWS_DEVICES
            ):
                raise ValueError(
                    f"{field_name} must be a portable identifier without path separators or traversal segments"
                )
        if self.dataset_version.casefold() in {"latest", "current", "head"}:
            raise ValueError("dataset_version must identify an immutable revision, not a floating alias")


@dataclass(frozen=True, slots=True)
class DatasetObjectDigests:
    """Byte-level SHA-256 identities, distinct from the logical dataset digest."""

    manifest_sha256: str
    questions_sha256: str
    references_sha256: str

    def __post_init__(self) -> None:
        for field_name in ("manifest_sha256", "questions_sha256", "references_sha256"):
            _strict_digest(getattr(self, field_name), field_name)

    @classmethod
    def from_objects(cls, objects: DatasetBundleBytes) -> DatasetObjectDigests:
        if type(objects) is not DatasetBundleBytes:
            raise TypeError("objects must be DatasetBundleBytes")
        return cls(
            manifest_sha256=hashlib.sha256(objects.manifest_json).hexdigest(),
            questions_sha256=hashlib.sha256(objects.questions_jsonl).hexdigest(),
            references_sha256=hashlib.sha256(objects.references_jsonl).hexdigest(),
        )

    def verify(self, objects: DatasetBundleBytes) -> None:
        if self != self.from_objects(objects):
            raise ValueError("dataset object bytes do not match the publication digests")


@dataclass(frozen=True, slots=True)
class DatasetPublicationParent:
    """Pinned frozen parent and the external re-review evidence for a correction."""

    revision: DatasetRevision
    dataset_digest: str
    evidence_reference: str

    def __post_init__(self) -> None:
        if type(self.revision) is not DatasetRevision:
            raise TypeError("parent revision must be DatasetRevision")
        _strict_digest(self.dataset_digest, "parent dataset_digest")
        value = self.evidence_reference
        if type(value) is not str or not value.strip() or value != value.strip():
            raise ValueError("evidence_reference must be a non-empty strict string")
        if len(value) > 4096 or re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/|file:)", value, re.IGNORECASE):
            raise ValueError("evidence_reference must be a portable external reference, not a local path")


@dataclass(frozen=True, slots=True)
class DatasetPublicationReceipt:
    """Verified storage identity, audit lineage and effective read-only policy.

    Read-only modes/attributes prevent ordinary accidental writes. They do not
    promise protection against a root/administrator changing storage or ACLs.
    """

    revision: DatasetRevision
    dataset_digest: str
    object_digests: DatasetObjectDigests
    published_at: datetime
    disposition: Literal["published", "reused"]
    permission_policy: Literal["posix_read_only", "windows_read_only_attribute"]
    read_only: bool
    parent: DatasetPublicationParent | None = None
    change_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.revision) is not DatasetRevision:
            raise TypeError("receipt revision must be DatasetRevision")
        _strict_digest(self.dataset_digest, "dataset_digest")
        if type(self.object_digests) is not DatasetObjectDigests:
            raise TypeError("object_digests must be DatasetObjectDigests")
        if type(self.published_at) is not datetime or self.published_at.utcoffset() is None:
            raise ValueError("published_at must be a timezone-aware datetime")
        if type(self.disposition) is not str or self.disposition not in {"published", "reused"}:
            raise ValueError("disposition must be published or reused")
        if type(self.permission_policy) is not str or self.permission_policy not in {
            "posix_read_only", "windows_read_only_attribute",
        }:
            raise ValueError("permission_policy must name an enforced platform policy")
        if type(self.read_only) is not bool or self.read_only is not True:
            raise ValueError("publication receipts require verified read_only=True")
        if self.parent is not None:
            if type(self.parent) is not DatasetPublicationParent:
                raise TypeError("parent must be DatasetPublicationParent or None")
            if self.parent.revision.dataset_id != self.revision.dataset_id:
                raise ValueError("correction parent must belong to the same dataset")
            if self.parent.revision.dataset_version == self.revision.dataset_version:
                raise ValueError("corrections require a new dataset_version")
            if self.parent.dataset_digest == self.dataset_digest:
                raise ValueError("corrections require a new dataset_digest")
        if type(self.change_scope) is not tuple or any(
            type(field) is not str
            or not field.strip()
            or field != field.strip()
            or len(field) > 512
            for field in self.change_scope
        ):
            raise ValueError("change_scope must be a tuple of canonical non-empty field locations")
        if tuple(sorted(set(self.change_scope))) != self.change_scope:
            raise ValueError("change_scope must be unique and sorted canonically")
        if bool(self.change_scope) != (self.parent is not None):
            raise ValueError("corrections require change_scope and initial releases must not claim one")


class DatasetRevisionNotFoundError(FileNotFoundError):
    """Only an absent revision, not an incomplete or corrupt existing release."""

    code = "dataset.revision_unavailable"
    location = "revision"
    reason = "dataset revision directory is unavailable"

    def __init__(self) -> None:
        super().__init__(self.reason)


class DatasetPublicationError(OSError):
    """Sanitized storage publication failure; never contains reference bytes."""

    def __init__(self, *, code: str, location: str, reason: str) -> None:
        self.code = code
        self.location = location
        self.reason = reason
        super().__init__(reason)


class DatasetPublicationConflict(DatasetPublicationError):
    """The selected immutable storage identity is already bound differently."""


class DatasetReadPort(Protocol):
    def read_dataset_objects(self, revision: DatasetRevision) -> DatasetBundleBytes:
        """Read concrete bytes; raise DatasetRevisionNotFoundError only if absent."""
        ...


class DatasetPublishPort(Protocol):
    def publish_dataset_objects(
        self,
        revision: DatasetRevision,
        objects: DatasetBundleBytes,
        *,
        dataset_digest: str,
        parent: DatasetPublicationParent | None = None,
        change_scope: tuple[str, ...] = (),
    ) -> DatasetPublicationReceipt:
        """Atomically publish once or verify and reuse an identical release.

        Every object must be committed together with its strict audit receipt.
        Existing versions cannot be repaired or overwritten, including partial
        or externally tampered versions. New versions preserve pinned lineage.
        """
        ...


def _strict_digest(value: str, field_name: str) -> None:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


__all__ = [
    "DatasetBundleBytes",
    "DatasetObjectDigests",
    "DatasetPublicationConflict",
    "DatasetPublicationError",
    "DatasetPublicationParent",
    "DatasetPublicationReceipt",
    "DatasetPublishPort",
    "DatasetReadPort",
    "DatasetRevision",
    "DatasetRevisionNotFoundError",
]
