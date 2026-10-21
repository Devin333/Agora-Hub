from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from backend.research.ports.evaluation_dataset import (
    DatasetBundleBytes,
    DatasetObjectDigests,
    DatasetPublicationParent,
    DatasetPublicationReceipt,
    DatasetRevision,
    DatasetRevisionNotFoundError,
)
from backend.research.application.evaluation import (
    EvaluationApplicationService,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_dataset import DatasetManifest, EvaluationCase
from backend.research.rag.evaluation.protocol_dataset_io import (
    DatasetBundle,
    DatasetValidationMode,
    load_dataset_bundle,
    serialize_dataset_bundle,
)
from tests.backend.research.rag.test_protocol_dataset_io import _frozen_bundle


@dataclass
class BytesDatasetReadPort:
    objects: DatasetBundleBytes
    calls: list[DatasetRevision]

    def read_dataset_objects(self, revision: DatasetRevision) -> DatasetBundleBytes:
        self.calls.append(revision)
        return self.objects


def test_application_service_reuses_injected_bytes_port_and_returns_typed_validation() -> None:
    manifest, _, objects = _frozen_bundle()
    port = BytesDatasetReadPort(objects=objects, calls=[])
    service = EvaluationApplicationService(dataset_reader=port)
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)

    result = service.validate_dataset(revision, mode=DatasetValidationMode.FROZEN)

    assert port.calls == [revision]
    assert result.report.valid is True
    assert result.report.ready_for_execution_data is True
    assert result.report.formal_ready is False
    assert result.bundle is not None
    assert result.bundle.manifest.dataset_digest == manifest.dataset_digest


def test_service_blind_inputs_never_return_reference_records_or_handles() -> None:
    manifest, _, objects = _frozen_bundle()
    service = EvaluationApplicationService(
        dataset_reader=BytesDatasetReadPort(objects=objects, calls=[])
    )
    bundle = service.validate_dataset(
        DatasetRevision(manifest.dataset_id, manifest.dataset_version),
        mode=DatasetValidationMode.FROZEN,
    ).require_bundle()

    blind = service.blind_inputs(bundle)

    assert len(blind) == 1
    assert blind[0].case_id == "case-001"
    assert blind[0].question_input.model_fields_set == {"question_text", "scope"}
    assert not hasattr(blind[0], "reference_records")
    assert not hasattr(blind[0], "reference_label")


@pytest.mark.parametrize(
    ("dataset_id", "dataset_version", "error"),
    [
        ("../dataset", "1.0.0", ValueError),
        ("dataset", "../1.0.0", ValueError),
        ("F:dataset", "1.0.0", ValueError),
        ("dataset", "latest", ValueError),
        ("dataset", "current", ValueError),
        ("dataset", "head", ValueError),
        (123, "1.0.0", TypeError),
        ("dataset", 1, TypeError),
    ],
)
def test_dataset_revision_is_strict_non_floating_and_path_safe(
    dataset_id: object,
    dataset_version: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        DatasetRevision(dataset_id=dataset_id, dataset_version=dataset_version)  # type: ignore[arg-type]


def test_service_rejects_reader_returning_another_revision() -> None:
    manifest, _, objects = _frozen_bundle()
    port = BytesDatasetReadPort(objects=objects, calls=[])
    service = EvaluationApplicationService(dataset_reader=port)

    result = service.validate_dataset(
        DatasetRevision("another-dataset", manifest.dataset_version),
        mode=DatasetValidationMode.FROZEN,
    )

    assert result.bundle is None
    assert result.report.valid is False
    assert result.report.ready_for_execution_data is False
    assert result.report.formal_ready is False
    assert result.report.issues[-1].code == "port.revision_mismatch"


def test_service_requires_concrete_bytes_objects_not_paths_or_lazy_handles() -> None:
    class BadPort:
        def read_dataset_objects(self, revision: DatasetRevision) -> object:
            return {"manifest": "F:/private/manifest.json"}

    service = EvaluationApplicationService(dataset_reader=BadPort())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="DatasetBundleBytes"):
        service.validate_dataset(DatasetRevision("dataset", "1.0.0"))



@dataclass
class MemoryDatasetPort:
    objects: dict[DatasetRevision, DatasetBundleBytes]
    publish_calls: list[tuple[DatasetRevision, DatasetPublicationParent | None, tuple[str, ...]]]

    def read_dataset_objects(self, revision: DatasetRevision) -> DatasetBundleBytes:
        try:
            return self.objects[revision]
        except KeyError as exc:
            raise DatasetRevisionNotFoundError() from exc

    def publish_dataset_objects(
        self,
        revision: DatasetRevision,
        objects: DatasetBundleBytes,
        *,
        dataset_digest: str,
        parent: DatasetPublicationParent | None = None,
        change_scope: tuple[str, ...] = (),
    ) -> DatasetPublicationReceipt:
        self.publish_calls.append((revision, parent, change_scope))
        self.objects[revision] = objects
        return DatasetPublicationReceipt(
            revision=revision,
            dataset_digest=dataset_digest,
            object_digests=DatasetObjectDigests.from_objects(objects),
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            disposition="published",
            permission_policy="windows_read_only_attribute",
            read_only=True,
            parent=parent,
            change_scope=change_scope,
        )


def _case_manifest_entry(case: EvaluationCase) -> dict[str, object]:
    return {
        "case_id": case.case_id,
        "case_digest": case.case_digest,
        "question_digest": content_digest(case.question_projection()),
        "reference_digest": content_digest(case.reference_projection()),
        "paper_group_id": case.paper_group_id,
        "split": case.split,
        "review_status": case.review.status,
    }


def _correction_fixture(
    *, split: str = "dev",
) -> tuple[DatasetBundleBytes, DatasetBundle, DatasetPublicationParent]:
    previous_manifest, previous_cases, previous_objects = _frozen_bundle()
    reviewed_at = previous_manifest.frozen_at + timedelta(days=1)
    case_content = previous_cases[0].model_dump(mode="python", exclude={"case_digest"})
    case_content.update(
        dataset_version="1.0.1",
        split=split,
        reference_answer="Corrected reference answer.",
    )
    case_content["review"] = {
        **case_content["review"],
        "reviewed_at": reviewed_at,
        "evidence_reference": "artifact://fixture/case-review-correction",
    }
    corrected_case = EvaluationCase.create(**case_content)

    manifest_content = previous_manifest.model_dump(mode="python", exclude={"dataset_digest"})
    manifest_content.update(
        dataset_version="1.0.1",
        supersedes=previous_manifest.dataset_version,
        correction_reason="Correct the frozen reference answer.",
        frozen_at=reviewed_at,
        annotation_version="fixture-annotation/v2",
        case_manifest=[_case_manifest_entry(corrected_case)],
    )
    manifest_content["review_manifest"] = {
        **manifest_content["review_manifest"],
        "reviewed_at": reviewed_at,
        "evidence_reference": "artifact://fixture/dataset-review-correction",
    }
    if split != previous_cases[0].split:
        manifest_content["release_purpose"] = "heldout_qualification"
        groups = list(manifest_content["split_manifest"]["groups"])
        groups[0] = {**groups[0], "split": split}
        manifest_content["split_manifest"] = {
            **manifest_content["split_manifest"],
            "groups": groups,
        }
    corrected_manifest = DatasetManifest.create(**manifest_content)
    corrected_objects = serialize_dataset_bundle(corrected_manifest, (corrected_case,))
    corrected_bundle = load_dataset_bundle(
        corrected_objects, mode=DatasetValidationMode.FROZEN,
    ).require_bundle()
    parent = DatasetPublicationParent(
        revision=DatasetRevision(previous_manifest.dataset_id, previous_manifest.dataset_version),
        dataset_digest=previous_manifest.dataset_digest,
        evidence_reference=corrected_manifest.review_manifest.evidence_reference,
    )
    return previous_objects, corrected_bundle, parent


def _non_frozen_bundle(*, unreviewed: bool) -> DatasetBundle:
    manifest, cases, _ = _frozen_bundle()
    case = cases[0]
    lifecycle_state = "draft"
    if unreviewed:
        lifecycle_state = "annotated"
        case_content = case.model_dump(mode="python", exclude={"case_digest"})
        case_content.update(
            answerability="needs_review",
            expected_behavior=None,
            reference_answer=None,
            reference_claims=[],
            evidence_anchors=[],
            required_evidence_groups=[],
        )
        case_content["review"] = {
            **case_content["review"],
            "status": "needs_review",
            "reviewer_id": None,
            "reviewer_kind": None,
            "reviewed_at": None,
            "source_completeness": "unknown",
            "checked_scope": None,
            "evidence_reference": None,
        }
        case = EvaluationCase.create(**case_content)

    manifest_content = manifest.model_dump(mode="python", exclude={"dataset_digest"})
    manifest_content.update(
        lifecycle_state=lifecycle_state,
        frozen_at=None,
        case_manifest=[_case_manifest_entry(case)],
    )
    if unreviewed:
        manifest_content["review_manifest"] = {
            **manifest_content["review_manifest"],
            "status": "needs_review",
            "reviewer_id": None,
            "reviewer_kind": None,
            "reviewed_at": None,
            "approved_case_ids": [],
            "corpus_checked": False,
            "split_checked": False,
            "contract_checked": False,
            "evidence_reference": None,
        }
    candidate_manifest = DatasetManifest.create(**manifest_content)
    objects = serialize_dataset_bundle(candidate_manifest, (case,))
    return load_dataset_bundle(objects, mode=DatasetValidationMode.DRAFT).require_bundle()


@pytest.mark.parametrize(
    "change_scope",
    [
        ("manifest.review_manifest", "cases.case-001.review"),
        ("cases.case-001.review", "cases.case-001.review"),
        (" cases.case-001.review",),
    ],
)
def test_publication_receipt_requires_unique_sorted_canonical_change_scope(
    change_scope: tuple[str, ...],
) -> None:
    parent = DatasetPublicationParent(
        DatasetRevision("fixture-dataset", "1.0.0"),
        "1" * 64,
        "artifact://fixture/review",
    )

    with pytest.raises(ValueError, match="change_scope"):
        DatasetPublicationReceipt(
            revision=DatasetRevision("fixture-dataset", "1.0.1"),
            dataset_digest="2" * 64,
            object_digests=DatasetObjectDigests("3" * 64, "4" * 64, "5" * 64),
            published_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
            disposition="published",
            permission_policy="windows_read_only_attribute",
            read_only=True,
            parent=parent,
            change_scope=change_scope,
        )


def test_publish_correction_preserves_parent_revision_and_records_exact_change_scope() -> None:
    previous_objects, correction, parent = _correction_fixture()
    original_snapshot = previous_objects
    port = MemoryDatasetPort(objects={parent.revision: previous_objects}, publish_calls=[])
    service = EvaluationApplicationService(dataset_reader=port, dataset_publisher=port)

    receipt = service.publish_dataset(correction, parent=parent)

    corrected_revision = DatasetRevision(
        correction.manifest.dataset_id, correction.manifest.dataset_version,
    )
    assert receipt.parent == parent
    assert receipt.change_scope == (
        "cases.case-001.reference_answer",
        "cases.case-001.review",
        "manifest.annotation_version",
        "manifest.review_manifest",
    )
    assert port.objects[parent.revision] == original_snapshot
    old_bundle = service.validate_dataset(
        parent.revision, mode=DatasetValidationMode.FROZEN,
    ).require_bundle()
    assert old_bundle.manifest.dataset_digest == parent.dataset_digest
    published_bundle = service.validate_dataset(
        corrected_revision, mode=DatasetValidationMode.FROZEN,
    ).require_bundle()
    assert published_bundle == correction


@pytest.mark.parametrize("mismatch", ["digest", "evidence"])
def test_publish_correction_rejects_unpinned_parent_or_unmatched_rereview(mismatch: str) -> None:
    previous_objects, correction, parent = _correction_fixture()
    if mismatch == "digest":
        parent = DatasetPublicationParent(parent.revision, "0" * 64, parent.evidence_reference)
    else:
        parent = DatasetPublicationParent(parent.revision, parent.dataset_digest, "artifact://fixture/other-review")
    port = MemoryDatasetPort(objects={parent.revision: previous_objects}, publish_calls=[])
    service = EvaluationApplicationService(dataset_reader=port, dataset_publisher=port)

    with pytest.raises(ValueError, match="digest|evidence"):
        service.publish_dataset(correction, parent=parent)

    assert port.publish_calls == []


def test_publish_correction_rejects_recutting_a_development_question_as_blind_heldout() -> None:
    previous_objects, correction, parent = _correction_fixture(split="heldout")
    port = MemoryDatasetPort(objects={parent.revision: previous_objects}, publish_calls=[])
    service = EvaluationApplicationService(dataset_reader=port, dataset_publisher=port)

    prohibited = (
        "must not relabel a development release"
        "|development question cannot become blind heldout"
    )
    with pytest.raises(ValueError, match=prohibited):
        service.publish_dataset(correction, parent=parent)

    assert port.publish_calls == []


@pytest.mark.parametrize("unreviewed", [False, True], ids=["draft", "unreviewed"])
def test_publication_rejects_non_frozen_or_unreviewed_bundles(unreviewed: bool) -> None:
    candidate = _non_frozen_bundle(unreviewed=unreviewed)
    port = MemoryDatasetPort(objects={}, publish_calls=[])
    service = EvaluationApplicationService(dataset_reader=port, dataset_publisher=port)

    with pytest.raises(ValueError):
        service.publish_dataset(candidate)

    assert port.publish_calls == []
