"""Bounded, confined filesystem readers for evaluation corpus and mappings.

Storage layouts are deliberately fixed and content-addressed::

    <corpus_root>/<corpus_snapshot_id>/manifest.json
    <corpus_root>/<corpus_snapshot_id>/materials/<lowercase-sha256>

    <mapping_root>/<dataset_id>/<dataset_version>/mappings/<case_id>.json

The adapters never follow ``source_uri`` values and expose only concrete bytes.
"""

from __future__ import annotations

import os
from pathlib import Path

from backend.research.ports.evaluation_sources import (
    CorpusSnapshotRevision,
    EvidenceMappingReadPort,
    EvidenceMappingRevision,
    Sha256BlobIdentity,
    SourceCorpusReadPort,
)
from infrastructure.research.evaluation_dataset import (
    EvaluationDatasetReadError,
    _DatasetFilesystem,
)

DEFAULT_CORPUS_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_CORPUS_MATERIAL_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_EVIDENCE_MAPPING_MAX_BYTES = 64 * 1024 * 1024


class EvaluationSourceReadError(OSError):
    """Sanitized, machine-classifiable corpus/mapping storage failure."""

    def __init__(self, *, code: str, location: str, reason: str) -> None:
        self.code = code
        self.location = location
        self.reason = reason
        super().__init__(reason)


class _BoundReadFilesystem(_DatasetFilesystem):
    """Reuse the dataset adapter's checked open/read path with fixed bounds."""

    def __init__(self, root: str | os.PathLike[str], *, limits: dict[str, int]) -> None:
        # The parent validates and pins the configured root.  Its standard
        # object limits are unused except for manifest.json; custom stable
        # object classes below avoid path-derived dictionary keys.
        super().__init__(
            root,
            manifest_max_bytes=limits.get("manifest.json", 1),
            jsonl_max_bytes=1,
        )
        self._limits.update(limits)

    def _checked_directory(self, relative: tuple[str, ...], *, location: str) -> Path:
        self._assert_root()
        path = self.dataset_root
        for component in relative:
            path = path / component
            self._reject_link(path, location=location)
            self._resolve_confined(path, location=location, strict=False)
        try:
            self._require_directory(path, location=location)
        except EvaluationDatasetReadError as exc:
            raise _source_error(exc, location=location) from exc
        return path

    def _checked_read(self, path: Path, *, object_class: str, location: str) -> bytes:
        try:
            return self._read_file(path, object_name=object_class)
        except EvaluationDatasetReadError as exc:
            raise _source_error(exc, location=location) from exc


class FilesystemSourceCorpusReader(_BoundReadFilesystem, SourceCorpusReadPort):
    def __init__(
        self,
        corpus_root: str | os.PathLike[str],
        *,
        manifest_max_bytes: int = DEFAULT_CORPUS_MANIFEST_MAX_BYTES,
        material_max_bytes: int = DEFAULT_CORPUS_MATERIAL_MAX_BYTES,
    ) -> None:
        super().__init__(
            corpus_root,
            limits={
                "manifest.json": manifest_max_bytes,
                "corpus-material": material_max_bytes,
            },
        )

    def read_corpus_manifest(self, revision: CorpusSnapshotRevision) -> bytes:
        revision = _corpus_revision(revision)
        directory = self._checked_directory(
            (revision.corpus_snapshot_id,),
            location="corpus_snapshot_id",
        )
        return self._checked_read(
            directory / "manifest.json",
            object_class="manifest.json",
            location="manifest.json",
        )

    def read_corpus_material(
        self,
        revision: CorpusSnapshotRevision,
        identity: Sha256BlobIdentity,
    ) -> bytes:
        revision = _corpus_revision(revision)
        identity = _blob_identity(identity)
        directory = self._checked_directory(
            (revision.corpus_snapshot_id, "materials"),
            location="materials",
        )
        return self._checked_read(
            directory / identity.sha256,
            object_class="corpus-material",
            location=f"materials/{identity.sha256}",
        )


class FilesystemEvidenceMappingReader(_BoundReadFilesystem, EvidenceMappingReadPort):
    def __init__(
        self,
        mapping_root: str | os.PathLike[str],
        *,
        mapping_max_bytes: int = DEFAULT_EVIDENCE_MAPPING_MAX_BYTES,
    ) -> None:
        super().__init__(mapping_root, limits={"evidence-mapping": mapping_max_bytes})

    def read_evidence_mapping(self, revision: EvidenceMappingRevision) -> bytes:
        if type(revision) is not EvidenceMappingRevision:
            raise TypeError("revision must be an EvidenceMappingRevision")
        revision = EvidenceMappingRevision(revision.dataset, revision.case_id)
        directory = self._checked_directory(
            (
                revision.dataset.dataset_id,
                revision.dataset.dataset_version,
                "mappings",
            ),
            location="mappings",
        )
        return self._checked_read(
            directory / f"{revision.case_id}.json",
            object_class="evidence-mapping",
            location=f"mappings/{revision.case_id}.json",
        )


def _corpus_revision(value: CorpusSnapshotRevision) -> CorpusSnapshotRevision:
    if type(value) is not CorpusSnapshotRevision:
        raise TypeError("revision must be a CorpusSnapshotRevision")
    return CorpusSnapshotRevision(value.corpus_snapshot_id)


def _blob_identity(value: Sha256BlobIdentity) -> Sha256BlobIdentity:
    if type(value) is not Sha256BlobIdentity:
        raise TypeError("identity must be a Sha256BlobIdentity")
    return Sha256BlobIdentity(value.sha256)


def _source_error(exc: EvaluationDatasetReadError, *, location: str) -> EvaluationSourceReadError:
    suffix = exc.code.removeprefix("dataset.")
    return EvaluationSourceReadError(
        code=f"evaluation_source.{suffix}",
        location=location,
        reason=exc.reason.replace("dataset", "evaluation source"),
    )


__all__ = [
    "DEFAULT_CORPUS_MANIFEST_MAX_BYTES",
    "DEFAULT_CORPUS_MATERIAL_MAX_BYTES",
    "DEFAULT_EVIDENCE_MAPPING_MAX_BYTES",
    "EvaluationSourceReadError",
    "FilesystemEvidenceMappingReader",
    "FilesystemSourceCorpusReader",
]
