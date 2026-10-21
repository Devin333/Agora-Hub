"""Storage-facing read contracts for immutable evaluation corpus and mappings.

Only concrete bytes, lowercase SHA-256 blob identities, and portable immutable
revisions cross these ports.  Source records, codecs, paths, callbacks, and lazy
handles remain outside the storage boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from backend.research.ports.evaluation_dataset import DatasetRevision

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _portable_component(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a strict string")
    # Reuse the dataset revision's cross-platform path and floating-alias guard
    # instead of maintaining a second filesystem identifier policy.
    DatasetRevision("evaluation-artifact", value)
    return value


@dataclass(frozen=True, slots=True)
class CorpusSnapshotRevision:
    corpus_snapshot_id: str

    def __post_init__(self) -> None:
        _portable_component(self.corpus_snapshot_id, "corpus_snapshot_id")


@dataclass(frozen=True, slots=True)
class Sha256BlobIdentity:
    sha256: str

    def __post_init__(self) -> None:
        if type(self.sha256) is not str:
            raise TypeError("sha256 must be a strict string")
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")


@dataclass(frozen=True, slots=True)
class EvidenceMappingRevision:
    dataset: DatasetRevision
    case_id: str

    def __post_init__(self) -> None:
        if type(self.dataset) is not DatasetRevision:
            raise TypeError("dataset must be a DatasetRevision")
        DatasetRevision(self.dataset.dataset_id, self.dataset.dataset_version)
        _portable_component(self.case_id, "case_id")


class SourceCorpusReadPort(Protocol):
    """Read a manifest and its content-addressed material blobs as bytes."""

    def read_corpus_manifest(self, revision: CorpusSnapshotRevision) -> bytes:
        ...

    def read_corpus_material(
        self,
        revision: CorpusSnapshotRevision,
        identity: Sha256BlobIdentity,
    ) -> bytes:
        ...


class EvidenceMappingReadPort(Protocol):
    """Read one canonical case mapping from an immutable dataset revision."""

    def read_evidence_mapping(self, revision: EvidenceMappingRevision) -> bytes:
        ...


__all__ = [
    "CorpusSnapshotRevision",
    "EvidenceMappingReadPort",
    "EvidenceMappingRevision",
    "Sha256BlobIdentity",
    "SourceCorpusReadPort",
]
