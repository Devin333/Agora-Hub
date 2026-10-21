from __future__ import annotations

from pathlib import Path

import pytest

from backend.research.ports.evaluation_dataset import DatasetRevision
from backend.research.ports.evaluation_sources import (
    CorpusSnapshotRevision,
    EvidenceMappingRevision,
    Sha256BlobIdentity,
)
from infrastructure.research.evaluation_sources import (
    EvaluationSourceReadError,
    FilesystemEvidenceMappingReader,
    FilesystemSourceCorpusReader,
)


def test_readers_return_exact_bounded_bytes_from_fixed_layouts(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    mapping_root = tmp_path / "mapping"
    snapshot = CorpusSnapshotRevision("corpus-v1.0.0")
    digest = "a" * 64
    corpus_dir = corpus_root / snapshot.corpus_snapshot_id
    (corpus_dir / "materials").mkdir(parents=True)
    (corpus_dir / "manifest.json").write_bytes(b'{"manifest":"bytes"}')
    (corpus_dir / "materials" / digest).write_bytes(b"complete source bytes")

    revision = EvidenceMappingRevision(
        DatasetRevision("dataset-a", "1.0.0"),
        "case-001",
    )
    mapping_dir = mapping_root / "dataset-a" / "1.0.0" / "mappings"
    mapping_dir.mkdir(parents=True)
    (mapping_dir / "case-001.json").write_bytes(b'{"mapping":"bytes"}')

    corpus_reader = FilesystemSourceCorpusReader(corpus_root)
    mapping_reader = FilesystemEvidenceMappingReader(mapping_root)

    assert corpus_reader.read_corpus_manifest(snapshot) == b'{"manifest":"bytes"}'
    assert corpus_reader.read_corpus_material(
        snapshot, Sha256BlobIdentity(digest)
    ) == b"complete source bytes"
    assert mapping_reader.read_evidence_mapping(revision) == b'{"mapping":"bytes"}'


def test_readers_enforce_bounds_and_report_sanitized_locations(tmp_path: Path) -> None:
    corpus_root = tmp_path / "corpus"
    snapshot = CorpusSnapshotRevision("corpus-v1")
    corpus_dir = corpus_root / snapshot.corpus_snapshot_id
    (corpus_dir / "materials").mkdir(parents=True)
    (corpus_dir / "manifest.json").write_bytes(b"0123456789")

    reader = FilesystemSourceCorpusReader(corpus_root, manifest_max_bytes=4)
    with pytest.raises(EvaluationSourceReadError) as captured:
        reader.read_corpus_manifest(snapshot)

    assert captured.value.code == "evaluation_source.object_too_large"
    assert captured.value.location == "manifest.json"
    assert str(tmp_path) not in str(captured.value)


def test_readers_reject_path_escape_symlinks_hardlinks_and_floating_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError):
        CorpusSnapshotRevision("../escape")
    with pytest.raises(ValueError):
        CorpusSnapshotRevision("latest")
    with pytest.raises(ValueError):
        EvidenceMappingRevision(DatasetRevision("dataset-a", "1.0.0"), "../case")
    with pytest.raises(ValueError):
        Sha256BlobIdentity("A" * 64)

    corpus_root = tmp_path / "corpus"
    snapshot = CorpusSnapshotRevision("corpus-v1")
    digest = "b" * 64
    material_dir = corpus_root / snapshot.corpus_snapshot_id / "materials"
    material_dir.mkdir(parents=True)
    (corpus_root / snapshot.corpus_snapshot_id / "manifest.json").write_bytes(b"{}")
    material = material_dir / digest
    material.write_bytes(b"source")
    reader = FilesystemSourceCorpusReader(corpus_root)

    original = Path.resolve
    escaped = tmp_path.parent / "escaped-source"

    def simulated_escape(path: Path, strict: bool = False) -> Path:
        if path.name == digest:
            return escaped
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", simulated_escape)
    with pytest.raises(EvaluationSourceReadError) as captured:
        reader.read_corpus_material(snapshot, Sha256BlobIdentity(digest))
    assert captured.value.code == "evaluation_source.path_escape"
    assert captured.value.location == f"materials/{digest}"
