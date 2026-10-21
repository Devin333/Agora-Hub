from __future__ import annotations

from pathlib import Path

import pytest

from backend.research.ports.evaluation_dataset import DatasetRevision
from backend.research.ports.evaluation_dataset import DatasetBundleBytes
from infrastructure.research.evaluation_dataset import (
    EvaluationDatasetReadError,
    FilesystemEvaluationDatasetReader,
)
from tests.backend.research.rag.test_protocol_dataset_io import _frozen_bundle


def _write_revision(
    root: Path,
    revision: DatasetRevision,
    objects: DatasetBundleBytes,
) -> Path:
    revision_dir = root / revision.dataset_id / revision.dataset_version
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(objects.manifest_json)
    (revision_dir / "questions.jsonl").write_bytes(objects.questions_jsonl)
    (revision_dir / "references.jsonl").write_bytes(objects.references_jsonl)
    return revision_dir


def test_reader_returns_exact_three_objects_as_bytes(tmp_path: Path) -> None:
    _, _, objects = _frozen_bundle()
    revision = DatasetRevision("fixture-dataset", "v1")
    _write_revision(tmp_path, revision, objects)

    loaded = FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)

    assert loaded == objects
    assert type(loaded.manifest_json) is bytes
    assert type(loaded.questions_jsonl) is bytes
    assert type(loaded.references_jsonl) is bytes


def test_reader_rejects_missing_object_without_disclosing_absolute_path(
    tmp_path: Path,
) -> None:
    _, _, objects = _frozen_bundle()
    revision = DatasetRevision("fixture-dataset", "v1")
    revision_dir = _write_revision(tmp_path, revision, objects)
    (revision_dir / "references.jsonl").unlink()

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)

    assert captured.value.code == "dataset.object_unavailable"
    assert captured.value.location == "references.jsonl"
    assert str(tmp_path) not in str(captured.value)


def test_reader_rejects_directory_masquerading_as_object(tmp_path: Path) -> None:
    _, _, objects = _frozen_bundle()
    revision = DatasetRevision("fixture-dataset", "v1")
    revision_dir = _write_revision(tmp_path, revision, objects)
    (revision_dir / "references.jsonl").unlink()
    (revision_dir / "references.jsonl").mkdir()

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)

    assert captured.value.code == "dataset.object_not_file"
    assert captured.value.location == "references.jsonl"


def test_reader_enforces_bounded_reads(tmp_path: Path) -> None:
    _, _, objects = _frozen_bundle()
    revision = DatasetRevision("fixture-dataset", "v1")
    _write_revision(tmp_path, revision, objects)

    reader = FilesystemEvaluationDatasetReader(
        tmp_path,
        manifest_max_bytes=max(1, len(objects.manifest_json) - 1),
    )
    with pytest.raises(EvaluationDatasetReadError) as captured:
        reader.read_dataset_objects(revision)

    assert captured.value.code == "dataset.object_too_large"
    assert captured.value.location == "manifest.json"


def test_reader_rejects_symlinked_object_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, objects = _frozen_bundle()
    revision = DatasetRevision("fixture-dataset", "v1")
    _write_revision(tmp_path, revision, objects)
    original = Path.is_symlink

    def simulated_symlink(path: Path) -> bool:
        if path.name == "references.jsonl":
            return True
        return original(path)

    monkeypatch.setattr(Path, "is_symlink", simulated_symlink)

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)

    assert captured.value.code == "dataset.symlink_forbidden"
    assert captured.value.location == "references.jsonl"


def test_reader_rejects_resolved_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, objects = _frozen_bundle()
    revision = DatasetRevision("fixture-dataset", "v1")
    _write_revision(tmp_path, revision, objects)
    reader = FilesystemEvaluationDatasetReader(tmp_path)
    original = Path.resolve
    escaped = tmp_path.parent / "outside-references.jsonl"

    def simulated_resolve(path: Path, strict: bool = False) -> Path:
        if path.name == "references.jsonl":
            return escaped
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", simulated_resolve)

    with pytest.raises(EvaluationDatasetReadError) as captured:
        reader.read_dataset_objects(revision)

    assert captured.value.code == "dataset.path_escape"
    assert captured.value.location == "references.jsonl"


@pytest.mark.parametrize(
    ("dataset_id", "dataset_version"),
    [
        ("../escape", "v1"),
        ("dataset", "latest"),
        ("dataset", "C:/absolute"),
        ("dataset/child", "v1"),
    ],
)
def test_revision_rejects_traversal_absolute_and_floating_segments(
    dataset_id: str,
    dataset_version: str,
) -> None:
    with pytest.raises(ValueError):
        DatasetRevision(dataset_id, dataset_version)


def _publish_fixture(root: Path):
    from backend.research.ports.evaluation_dataset import DatasetObjectDigests
    from infrastructure.research.evaluation_dataset import FilesystemEvaluationDatasetPublisher

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    publisher = FilesystemEvaluationDatasetPublisher(root)
    receipt = publisher.publish_dataset_objects(revision, objects, dataset_digest=manifest.dataset_digest)
    assert receipt.object_digests == DatasetObjectDigests.from_objects(objects)
    return revision, objects, receipt, publisher


def test_publication_is_read_only_and_same_bytes_are_verified_idempotently(tmp_path: Path) -> None:
    import os
    import stat

    revision, objects, receipt, publisher = _publish_fixture(tmp_path)
    path = tmp_path / revision.dataset_id / revision.dataset_version
    before = {member.name: (member.read_bytes(), member.stat().st_mtime_ns) for member in path.iterdir()}
    assert set(before) == {"manifest.json", "questions.jsonl", "references.jsonl", ".publication.json"}
    for member in path.iterdir():
        metadata = member.stat()
        if os.name == "nt":
            assert metadata.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        else:
            assert metadata.st_mode & 0o222 == 0
    assert receipt.read_only is True
    assert receipt.disposition == "published"
    assert FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision) == objects

    reused = publisher.publish_dataset_objects(revision, objects, dataset_digest=receipt.dataset_digest)

    assert reused.disposition == "reused"
    assert reused.published_at == receipt.published_at
    assert reused.object_digests == receipt.object_digests
    assert before == {member.name: (member.read_bytes(), member.stat().st_mtime_ns) for member in path.iterdir()}
    assert not list(path.parent.glob(".publish-*"))


@pytest.mark.parametrize("change", ["bytes", "digest"])
def test_publication_never_overwrites_an_existing_identity(tmp_path: Path, change: str) -> None:
    from backend.research.ports.evaluation_dataset import DatasetPublicationConflict

    revision, objects, receipt, publisher = _publish_fixture(tmp_path)
    changed_objects = objects
    changed_digest = receipt.dataset_digest
    if change == "bytes":
        changed_objects = DatasetBundleBytes(objects.manifest_json, objects.questions_jsonl + b" ", objects.references_jsonl)
    else:
        changed_digest = "0" * 64

    with pytest.raises(DatasetPublicationConflict, match="different bytes or lineage"):
        publisher.publish_dataset_objects(revision, changed_objects, dataset_digest=changed_digest)

    assert FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision) == objects


@pytest.mark.parametrize("empty", [True, False])
def test_existing_empty_or_partial_versions_are_not_repaired(tmp_path: Path, empty: bool) -> None:
    from backend.research.ports.evaluation_dataset import DatasetPublicationConflict
    from infrastructure.research.evaluation_dataset import FilesystemEvaluationDatasetPublisher

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    path = tmp_path / revision.dataset_id / revision.dataset_version
    path.mkdir(parents=True)
    if not empty:
        (path / "manifest.json").write_bytes(b"existing unverified content")
    snapshot = {member.name: member.read_bytes() for member in path.iterdir()}

    with pytest.raises(DatasetPublicationConflict):
        FilesystemEvaluationDatasetPublisher(tmp_path).publish_dataset_objects(
            revision, objects, dataset_digest=manifest.dataset_digest,
        )

    assert snapshot == {member.name: member.read_bytes() for member in path.iterdir()}


def test_external_byte_tamper_is_detected_even_if_read_only_attribute_is_restored(tmp_path: Path) -> None:
    import stat
    from backend.research.ports.evaluation_dataset import DatasetPublicationConflict

    revision, objects, receipt, publisher = _publish_fixture(tmp_path)
    path = tmp_path / revision.dataset_id / revision.dataset_version / "references.jsonl"
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.write_bytes(objects.references_jsonl + b"tampered")
    path.chmod(stat.S_IREAD)

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)
    assert captured.value.code == "dataset.publication_tampered"
    with pytest.raises(DatasetPublicationConflict):
        publisher.publish_dataset_objects(revision, objects, dataset_digest=receipt.dataset_digest)
    assert path.read_bytes().endswith(b"tampered")


def test_correction_publication_rejects_a_tampered_parent_before_staging(tmp_path: Path) -> None:
    import stat
    from backend.research.ports.evaluation_dataset import DatasetPublicationParent

    parent_revision, objects, receipt, publisher = _publish_fixture(tmp_path)
    parent_object = tmp_path / parent_revision.dataset_id / parent_revision.dataset_version / "manifest.json"
    parent_object.chmod(stat.S_IREAD | stat.S_IWRITE)
    parent_object.write_bytes(objects.manifest_json + b"tampered")
    parent_object.chmod(stat.S_IREAD)
    correction_revision = DatasetRevision(parent_revision.dataset_id, "1.0.1")
    parent = DatasetPublicationParent(
        revision=parent_revision,
        dataset_digest=receipt.dataset_digest,
        evidence_reference="artifact://fixture/correction-review",
    )

    with pytest.raises(EvaluationDatasetReadError) as captured:
        publisher.publish_dataset_objects(
            correction_revision,
            DatasetBundleBytes(
                objects.manifest_json,
                objects.questions_jsonl,
                objects.references_jsonl + b"correction",
            ),
            dataset_digest="0" * 64,
            parent=parent,
            change_scope=("cases.case-001.reference_answer",),
        )

    assert captured.value.code == "dataset.publication_tampered"
    assert not (tmp_path / correction_revision.dataset_id / correction_revision.dataset_version).exists()
    assert not list((tmp_path / correction_revision.dataset_id).glob(".publish-*"))


def test_external_permission_tamper_is_not_silently_repaired(tmp_path: Path) -> None:
    import stat
    from backend.research.ports.evaluation_dataset import DatasetPublicationConflict

    revision, objects, receipt, publisher = _publish_fixture(tmp_path)
    path = tmp_path / revision.dataset_id / revision.dataset_version / "questions.jsonl"
    path.chmod(stat.S_IREAD | stat.S_IWRITE)

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)
    assert captured.value.code == "dataset.read_only_policy_violated"
    with pytest.raises(DatasetPublicationConflict):
        publisher.publish_dataset_objects(revision, objects, dataset_digest=receipt.dataset_digest)
    assert path.stat().st_mode & stat.S_IWRITE


def test_strict_receipt_rejects_unknown_metadata_fields(tmp_path: Path) -> None:
    import json
    import stat

    revision, _, _, _ = _publish_fixture(tmp_path)
    path = tmp_path / revision.dataset_id / revision.dataset_version / ".publication.json"
    metadata = json.loads(path.read_bytes())
    metadata["reference_callback"] = "not-a-contract-field"
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.write_text(json.dumps(metadata), encoding="utf-8")
    path.chmod(stat.S_IREAD)

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)
    assert captured.value.code == "dataset.receipt_invalid"


@pytest.mark.parametrize("failure", ["write", "fsync", "permissions", "rename"])
def test_publication_failure_does_not_expose_a_partial_version_or_delete_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import os
    from backend.research.ports.evaluation_dataset import DatasetPublicationError
    from infrastructure.research import evaluation_dataset as storage

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    unrelated = tmp_path / "unrelated-owner.txt"
    unrelated.write_bytes(b"keep")
    publisher = storage.FilesystemEvaluationDatasetPublisher(tmp_path)
    if failure == "write":
        original_write = publisher._write_owned_file

        def fail_write(staging, name, payload, owned_files):
            if name == "questions.jsonl":
                raise OSError("injected write failure with secret content")
            return original_write(staging, name, payload, owned_files)

        monkeypatch.setattr(publisher, "_write_owned_file", fail_write)
    elif failure == "fsync":
        def fail_sync(descriptor):
            raise OSError("injected fsync failure")

        monkeypatch.setattr(os, "fsync", fail_sync)
    elif failure == "permissions":
        def fail_permissions(staging):
            raise OSError("injected permission failure")

        monkeypatch.setattr(publisher, "_set_read_only", fail_permissions)
    else:
        def fail_rename(source, destination):
            raise OSError("injected atomic commit failure")

        monkeypatch.setattr(storage, "_rename_no_replace", fail_rename)

    with pytest.raises(DatasetPublicationError) as captured:
        publisher.publish_dataset_objects(revision, objects, dataset_digest=manifest.dataset_digest)

    assert str(tmp_path) not in str(captured.value)
    assert "secret content" not in str(captured.value)
    assert not (tmp_path / revision.dataset_id / revision.dataset_version).exists()
    assert not list((tmp_path / revision.dataset_id).glob(".publish-*"))
    assert unrelated.read_bytes() == b"keep"


def test_cleanup_does_not_delete_unowned_staging_members(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from infrastructure.research import evaluation_dataset as storage

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    publisher = storage.FilesystemEvaluationDatasetPublisher(tmp_path)
    original_write = publisher._write_owned_file
    foreign: list[Path] = []

    def fail_write(staging, name, payload, owned_files):
        if name == "questions.jsonl":
            foreign.append(staging / "foreign-owned.txt")
            foreign[0].write_bytes(b"never delete another owner's data")
            raise OSError("failure after unrelated file appeared")
        return original_write(staging, name, payload, owned_files)

    monkeypatch.setattr(publisher, "_write_owned_file", fail_write)
    with pytest.raises(OSError):
        publisher.publish_dataset_objects(revision, objects, dataset_digest=manifest.dataset_digest)

    assert foreign[0].read_bytes() == b"never delete another owner's data"
    assert not (foreign[0].parent / "manifest.json").exists()
    assert not (tmp_path / revision.dataset_id / revision.dataset_version).exists()


@pytest.mark.parametrize("linked_component", ["dataset_id", "dataset_version", "root"])
def test_publisher_uses_shared_no_link_confinement_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked_component: str,
) -> None:
    from infrastructure.research.evaluation_dataset import FilesystemEvaluationDatasetPublisher

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    names = {"dataset_id": revision.dataset_id, "dataset_version": revision.dataset_version, "root": tmp_path.name}
    original = Path.is_symlink

    def simulated_link(path: Path) -> bool:
        return path.name == names[linked_component] or original(path)

    monkeypatch.setattr(Path, "is_symlink", simulated_link)
    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetPublisher(tmp_path).publish_dataset_objects(
            revision, objects, dataset_digest=manifest.dataset_digest,
        )
    assert captured.value.code == "dataset.symlink_forbidden"
    assert not (tmp_path / revision.dataset_id).exists()


def test_publisher_rejects_resolved_parent_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from infrastructure.research.evaluation_dataset import FilesystemEvaluationDatasetPublisher

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    publisher = FilesystemEvaluationDatasetPublisher(tmp_path)
    original = Path.resolve

    def simulated_resolve(path: Path, strict: bool = False) -> Path:
        if path.name == revision.dataset_id:
            return tmp_path.parent / "outside-publish-root"
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", simulated_resolve)
    with pytest.raises(EvaluationDatasetReadError) as captured:
        publisher.publish_dataset_objects(revision, objects, dataset_digest=manifest.dataset_digest)
    assert captured.value.code == "dataset.path_escape"
    assert not (tmp_path / revision.dataset_id).exists()


def test_reader_rejects_hard_link_aliases(tmp_path: Path) -> None:
    import os

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    path = _write_revision(tmp_path, revision, objects)
    os.link(path / "references.jsonl", tmp_path / "reference-alias.jsonl")

    with pytest.raises(EvaluationDatasetReadError) as captured:
        FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)
    assert captured.value.code == "dataset.hardlink_forbidden"


def test_identical_concurrent_publication_has_one_winner_and_verified_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from infrastructure.research import evaluation_dataset as storage

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    barrier = Barrier(4)
    original = storage._rename_no_replace

    def synchronized_commit(source, destination):
        barrier.wait(timeout=15)
        return original(source, destination)

    monkeypatch.setattr(storage, "_rename_no_replace", synchronized_commit)

    def publish():
        return storage.FilesystemEvaluationDatasetPublisher(tmp_path).publish_dataset_objects(
            revision, objects, dataset_digest=manifest.dataset_digest,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: publish(), range(4)))

    assert sorted(receipt.disposition for receipt in receipts) == ["published", "reused", "reused", "reused"]
    assert len({receipt.published_at for receipt in receipts}) == 1
    assert len({receipt.object_digests for receipt in receipts}) == 1
    assert storage.FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision) == objects
    assert not list((tmp_path / revision.dataset_id).glob(".publish-*"))


def test_conflicting_concurrent_publication_never_replaces_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from backend.research.ports.evaluation_dataset import DatasetPublicationConflict
    from backend.research.rag.evaluation.protocol_dataset import DatasetManifest
    from backend.research.rag.evaluation.protocol_dataset_io import serialize_dataset_bundle
    from infrastructure.research import evaluation_dataset as storage

    manifest, cases, objects = _frozen_bundle()
    content = manifest.model_dump(mode="python", exclude={"dataset_digest"})
    content["annotation_version"] = "fixture-annotation/v2-conflict"
    changed = DatasetManifest.create(**content)
    changed_objects = serialize_dataset_bundle(changed, cases)
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    barrier = Barrier(2)
    original = storage._rename_no_replace

    def synchronized_commit(source, destination):
        barrier.wait(timeout=15)
        return original(source, destination)

    monkeypatch.setattr(storage, "_rename_no_replace", synchronized_commit)

    def publish(pair):
        selected, selected_objects = pair
        try:
            receipt = storage.FilesystemEvaluationDatasetPublisher(tmp_path).publish_dataset_objects(
                revision, selected_objects, dataset_digest=selected.dataset_digest,
            )
            return receipt, selected_objects
        except DatasetPublicationConflict as error:
            return error, selected_objects

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, ((manifest, objects), (changed, changed_objects))))

    successful = [(receipt, raw) for receipt, raw in results if not isinstance(receipt, Exception)]
    assert len(successful) == 1
    assert sum(isinstance(receipt, DatasetPublicationConflict) for receipt, _ in results) == 1
    assert storage.FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision) == successful[0][1]
    assert not list((tmp_path / revision.dataset_id).glob(".publish-*"))


def test_reader_sees_no_revision_until_atomic_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from backend.research.ports.evaluation_dataset import DatasetRevisionNotFoundError
    from infrastructure.research import evaluation_dataset as storage

    manifest, _, objects = _frozen_bundle()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    staged, release = Event(), Event()
    original = storage._rename_no_replace

    def paused_commit(source, destination):
        staged.set()
        assert release.wait(timeout=15)
        return original(source, destination)

    monkeypatch.setattr(storage, "_rename_no_replace", paused_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            storage.FilesystemEvaluationDatasetPublisher(tmp_path).publish_dataset_objects,
            revision, objects, dataset_digest=manifest.dataset_digest,
        )
        try:
            assert staged.wait(timeout=15)
            with pytest.raises(DatasetRevisionNotFoundError):
                storage.FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision)
        finally:
            release.set()
        assert future.result(timeout=15).disposition == "published"

    assert storage.FilesystemEvaluationDatasetReader(tmp_path).read_dataset_objects(revision) == objects


@pytest.mark.parametrize("segment", ["CON", "nul.json", "lpt1", "version.", "a" * 256])
def test_revision_refuses_windows_devices_and_nonportable_segments(segment: str) -> None:
    with pytest.raises(ValueError):
        DatasetRevision("fixture-dataset", segment)
