from __future__ import annotations

from copy import deepcopy
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_common import PROTOCOL_VERSION, SCHEMA_VERSION
from backend.research.rag.evaluation.protocol_corpus import (
    CorpusContractError,
    CorpusMaterialBytes,
    CorpusMaterialDescriptor,
    CorpusMaterialIdentity,
    CorpusSourceEntry,
    IndexInputArtifact,
    SourceCorpusManifest,
    build_verified_corpus_provenance,
    sha256_bytes,
    verify_index_corpus,
    verify_source_corpus,
)
from backend.research.rag.evaluation.protocol_dataset import (
    DatasetManifest,
    QuestionScope,
    ScopedPaper,
    SourceRecord,
)

NOW = datetime(2026, 10, 14, 8, 30, tzinfo=timezone.utc)
INDEX_BYTES = b"canonical serialized index snapshot bytes"

MAIN_PRIMARY = b"%PDF-1.7 complete main paper with figures and tables"
MAIN_FULLTEXT = (
    b"Main paper title\nMethods nongold background.\n"
    b"Gold fact appears here.\nResults and limitations nongold content."
)
MAIN_ATTACHMENT = b"Supplementary table S1\nall rows, labels, and values"
DISTRACTOR_PRIMARY = b"%PDF-1.7 complete declared distractor paper"
DISTRACTOR_FULLTEXT = b"Distractor paper full text with plausible but irrelevant methods."


def _source(
    paper_id: str,
    primary: bytes,
    fulltext: bytes,
    *,
    source_uri: str,
    completeness: str = "complete",
) -> SourceRecord:
    return SourceRecord(
        paper_id=paper_id,
        document_version="v1.0.0",
        source_uri=source_uri,
        source_sha256=sha256_bytes(primary),
        retrieved_at=NOW,
        fulltext_artifact_digest=sha256_bytes(fulltext),
        completeness=completeness,
        access_status="available",
        source_format="pdf",
        license_reference_id="fixture-license",
    )


def _descriptor(
    material_id: str,
    kind: str,
    content: bytes,
    *,
    uses: tuple[str, ...],
    required: bool = True,
) -> CorpusMaterialDescriptor:
    return CorpusMaterialDescriptor(
        material_id=material_id,
        kind=kind,
        artifact_reference=f"artifact://fixture/corpus/{material_id}",
        media_type="application/pdf" if kind != "fulltext" else "text/plain",
        sha256=sha256_bytes(content),
        byte_length=len(content),
        required=required,
        uses=uses,
    )


def _fixture() -> tuple[
    SourceCorpusManifest,
    DatasetManifest,
    dict[str, QuestionScope],
    tuple[CorpusMaterialBytes, ...],
    dict[str, bytes],
]:
    main = _source(
        "paper-main",
        MAIN_PRIMARY,
        MAIN_FULLTEXT,
        source_uri="https://example.test/paper-main-v1.pdf",
    )
    distractor = _source(
        "paper-distractor",
        DISTRACTOR_PRIMARY,
        DISTRACTOR_FULLTEXT,
        source_uri="https://example.test/paper-distractor-v1.pdf",
    )
    main_entry = CorpusSourceEntry(
        source=main,
        role="question_scope",
        materials=(
            _descriptor(
                "main-primary",
                "primary_source",
                MAIN_PRIMARY,
                uses=("source_verification",),
            ),
            _descriptor(
                "main-fulltext",
                "fulltext",
                MAIN_FULLTEXT,
                uses=("source_verification", "retrieval_index", "oracle_context"),
            ),
            _descriptor(
                "main-supplement",
                "attachment",
                MAIN_ATTACHMENT,
                uses=("source_verification", "retrieval_index", "oracle_context"),
            ),
        ),
    )
    distractor_entry = CorpusSourceEntry(
        source=distractor,
        role="distractor",
        materials=(
            _descriptor(
                "distractor-primary",
                "primary_source",
                DISTRACTOR_PRIMARY,
                uses=("source_verification",),
            ),
            _descriptor(
                "distractor-fulltext",
                "fulltext",
                DISTRACTOR_FULLTEXT,
                uses=("source_verification", "retrieval_index"),
            ),
        ),
    )
    corpus = SourceCorpusManifest.create(
        record_type="corpus",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id="heldout-dataset",
        dataset_version="1.0.0",
        corpus_snapshot_id="corpus-v1.0.0",
        inventory_version="1.0.0",
        sources=(main_entry, distractor_entry),
    )
    main_scope = ScopedPaper(
        paper_id=main.paper_id,
        document_version=main.document_version,
        source_sha256=main.source_sha256,
    )
    distractor_scope = ScopedPaper(
        paper_id=distractor.paper_id,
        document_version=distractor.document_version,
        source_sha256=distractor.source_sha256,
    )
    dataset = DatasetManifest.create(
        record_type="dataset",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id="heldout-dataset",
        dataset_version="1.0.0",
        lifecycle_state="annotated",
        release_purpose="heldout_qualification",
        corpus_snapshot_id=corpus.corpus_snapshot_id,
        corpus_snapshot_digest=corpus.corpus_snapshot_digest,
        paper_versions=(main, distractor),
        case_manifest=(
            {
                "case_id": "case-1",
                "case_digest": "1" * 64,
                "question_digest": "2" * 64,
                "reference_digest": "3" * 64,
                "paper_group_id": "family-main",
                "split": "heldout",
                "review_status": "pending",
            },
            {
                "case_id": "case-2",
                "case_digest": "4" * 64,
                "question_digest": "5" * 64,
                "reference_digest": "6" * 64,
                "paper_group_id": "family-main",
                "split": "heldout",
                "review_status": "pending",
            },
        ),
        split_manifest={
            "method": "reviewed-paper-family",
            "method_version": "1.0.0",
            "deterministic_seed": None,
            "groups": (
                {
                    "paper_group_id": "family-main",
                    "split": "heldout",
                    "sources": (main_scope,),
                    "grouping_basis": "same paper and revisions",
                    "dispute_resolution": None,
                },
                {
                    "paper_group_id": "family-distractor",
                    "split": "heldout",
                    "sources": (distractor_scope,),
                    "grouping_basis": "declared distractor family",
                    "dispute_resolution": None,
                },
            ),
        },
        review_manifest={
            "status": "pending",
            "reviewer_id": None,
            "reviewer_kind": None,
            "reviewed_at": None,
            "approved_case_ids": (),
            "corpus_checked": False,
            "split_checked": False,
            "contract_checked": False,
            "evidence_reference": None,
        },
        annotation_version="1.0.0",
        license_reference={
            "reference_id": "fixture-license",
            "reference_uri": "https://example.test/licenses/internal-eval-v1",
            "terms_digest": "7" * 64,
            "permitted_use": "internal_evaluation",
        },
        authoring_protocol={
            "protocol_id": "fixture-authoring",
            "protocol_version": "1.0.0",
            "protocol_digest": "8" * 64,
        },
        supersedes=None,
        correction_reason=None,
        frozen_at=None,
    )
    scopes = {
        "case-1": QuestionScope(sources=(main_scope,)),
        "case-2": QuestionScope(sources=(main_scope,)),
    }
    bytes_by_id = {
        "main-primary": MAIN_PRIMARY,
        "main-fulltext": MAIN_FULLTEXT,
        "main-supplement": MAIN_ATTACHMENT,
        "distractor-primary": DISTRACTOR_PRIMARY,
        "distractor-fulltext": DISTRACTOR_FULLTEXT,
    }
    objects = tuple(
        CorpusMaterialBytes(
            identity=CorpusMaterialIdentity(
                paper_id=entry.source.paper_id,
                document_version=entry.source.document_version,
                source_sha256=entry.source.source_sha256,
                material_id=material.material_id,
                kind=material.kind,
            ),
            content_bytes=bytes_by_id[material.material_id],
        )
        for entry in corpus.sources
        for material in entry.materials
    )
    return corpus, dataset, scopes, objects, bytes_by_id


def _source_verification():
    corpus, dataset, scopes, objects, bytes_by_id = _fixture()
    verification = verify_source_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
    )
    return corpus, dataset, scopes, objects, bytes_by_id, verification


def _index_artifacts(
    corpus: SourceCorpusManifest,
    bytes_by_id: dict[str, bytes],
) -> tuple[IndexInputArtifact, ...]:
    artifacts: list[IndexInputArtifact] = []
    for entry in corpus.sources:
        for material in entry.materials:
            if "retrieval_index" not in material.uses:
                continue
            artifacts.append(
                IndexInputArtifact(
                    artifact_id=f"index-{material.material_id}",
                    artifact_type="source_material",
                    content_bytes=bytes_by_id[material.material_id],
                    source_identity=CorpusMaterialIdentity(
                        paper_id=entry.source.paper_id,
                        document_version=entry.source.document_version,
                        source_sha256=entry.source.source_sha256,
                        material_id=material.material_id,
                        kind=material.kind,
                    ),
                )
            )
    return tuple(artifacts)


def test_complete_source_corpus_validates_real_bytes_distractors_and_heldout_indexing() -> None:
    corpus, dataset, scopes, objects, bytes_by_id, source = _source_verification()

    assert source.formal_ready is True
    assert source.planned_case_ids == ("case-1", "case-2")
    assert {item.identity.material_id for item in source.verified_materials} == {
        "main-primary",
        "main-fulltext",
        "main-supplement",
        "distractor-primary",
        "distractor-fulltext",
    }
    assert len(objects) == 5

    index = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
        artifacts=_index_artifacts(corpus, bytes_by_id),
    )
    assert index.formal_ready is True
    assert index.planned_case_ids == source.planned_case_ids
    assert {item.disposition for item in index.case_statuses} == {"ready"}
    assert "index-distractor-fulltext" in index.accepted_artifact_ids

    provenance = build_verified_corpus_provenance(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
        artifacts=_index_artifacts(corpus, bytes_by_id),
        index_snapshot_id="heldout-index-v1",
        index_content_bytes=INDEX_BYTES,
        evidence_references=("artifact://fixture/corpus-verification",),
    )
    assert provenance.corpus_content_sha256 == corpus.corpus_snapshot_digest
    assert provenance.index_content_sha256 == sha256_bytes(INDEX_BYTES)
    assert provenance.source_material == "complete_fulltext"
    assert provenance.gold_labels_indexed is False


def test_missing_source_attachment_is_needs_review_not_parser_failure() -> None:
    corpus, dataset, scopes, objects, bytes_by_id = _fixture()
    without_attachment = tuple(
        item for item in objects if item.identity.material_id != "main-supplement"
    )
    source = verify_source_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=without_attachment,
    )

    assert source.disposition == "needs_review"
    assert "source.required_material_missing" in {issue.code for issue in source.issues}
    assert {item.disposition for item in source.case_statuses} == {"needs_review"}

    index = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=without_attachment,
        artifacts=_index_artifacts(corpus, bytes_by_id),
    )
    assert index.disposition == "needs_review"
    assert {item.disposition for item in index.case_statuses} == {"needs_review"}
    assert index.planned_case_ids == ("case-1", "case-2")


def test_verified_source_lost_or_truncated_during_indexing_is_system_failure_without_shrinking_denominator() -> None:
    corpus, dataset, scopes, objects, bytes_by_id, _source = _source_verification()
    artifacts = list(_index_artifacts(corpus, bytes_by_id))
    fulltext = next(item for item in artifacts if item.artifact_id == "index-main-fulltext")
    artifacts[artifacts.index(fulltext)] = fulltext.model_copy(
        update={"content_bytes": b"Gold fact appears here."}
    )
    artifacts = [item for item in artifacts if item.artifact_id != "index-main-supplement"]

    index = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
        artifacts=artifacts,
    )

    assert index.disposition == "system_failure"
    codes = {issue.code for issue in index.issues}
    assert "index.source_bytes_mismatch" in codes
    assert "index.required_material_missing" in codes
    assert index.planned_case_ids == ("case-1", "case-2")
    assert {item.disposition for item in index.case_statuses} == {"system_failure"}


@pytest.mark.parametrize(
    "artifact_type",
    ["question", "reference", "derived_note", "tuning_feedback"],
)
def test_index_rejects_evaluation_artifact_pollution(artifact_type: str) -> None:
    corpus, dataset, scopes, objects, bytes_by_id, _source = _source_verification()
    artifacts = list(_index_artifacts(corpus, bytes_by_id))
    artifacts.append(
        IndexInputArtifact(
            artifact_id=f"pollution-{artifact_type}",
            artifact_type=artifact_type,
            content_bytes=b"heldout label or feedback bytes",
            source_identity=None,
        )
    )

    index = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
        artifacts=artifacts,
    )

    assert index.disposition == "system_failure"
    assert f"index.forbidden_{artifact_type}" in {issue.code for issue in index.issues}


def test_gold_only_snapshot_and_metadata_source_claim_do_not_pass_as_full_corpus() -> None:
    corpus, dataset, scopes, objects, _, _source = _source_verification()
    main_entry = corpus.sources[0]
    fulltext = next(item for item in main_entry.materials if item.kind == "fulltext")
    claimed_source = IndexInputArtifact(
        artifact_id="gold-only-claimed-as-source",
        artifact_type="source_material",
        content_bytes=b"Gold fact appears here.",
        source_identity=CorpusMaterialIdentity(
            paper_id=main_entry.source.paper_id,
            document_version=main_entry.source.document_version,
            source_sha256=main_entry.source.source_sha256,
            material_id=fulltext.material_id,
            kind=fulltext.kind,
        ),
    )
    explicit_gold = IndexInputArtifact(
        artifact_id="gold-only-explicit",
        artifact_type="gold_snippet",
        content_bytes=b"Gold fact appears here.",
        source_identity=None,
    )

    index = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
        artifacts=(claimed_source, explicit_gold),
    )

    codes = {issue.code for issue in index.issues}
    assert index.disposition == "system_failure"
    assert "index.source_bytes_mismatch" in codes
    assert "index.forbidden_gold_snippet" in codes
    assert "index.required_material_missing" in codes


def test_question_scopes_must_be_exact_and_cannot_target_declared_distractors() -> None:
    corpus, dataset, scopes, objects, _ = _fixture()
    distractor = corpus.sources[1].source
    bad_scopes = dict(scopes)
    bad_scopes["case-1"] = QuestionScope(
        sources=(
            ScopedPaper(
                paper_id=distractor.paper_id,
                document_version=distractor.document_version,
                source_sha256=distractor.source_sha256,
            ),
        )
    )
    with pytest.raises(CorpusContractError, match="outside its paper group|distractor"):
        verify_source_corpus(
            corpus=corpus,
            dataset=dataset,
            question_scopes=bad_scopes,
            material_objects=objects,
        )

    missing_scope = {"case-1": scopes["case-1"]}
    with pytest.raises(CorpusContractError, match="scope membership mismatch"):
        verify_source_corpus(
            corpus=corpus,
            dataset=dataset,
            question_scopes=missing_scope,
            material_objects=objects,
        )


def test_wrong_version_attachment_duplicate_and_digest_mismatch_remain_needs_review() -> None:
    corpus, dataset, scopes, objects, _ = _fixture()
    attachment = next(item for item in objects if item.identity.material_id == "main-supplement")
    wrong_version = attachment.model_copy(
        update={
            "identity": attachment.identity.model_copy(update={"document_version": "v2.0.0"})
        }
    )
    wrong_bytes = attachment.model_copy(update={"content_bytes": b"truncated supplement"})
    supplied = tuple(
        item for item in objects if item.identity.material_id != "main-supplement"
    ) + (wrong_version, wrong_bytes, wrong_bytes)

    source = verify_source_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=supplied,
    )

    codes = {issue.code for issue in source.issues}
    assert source.disposition == "needs_review"
    assert "source.unknown_material_or_version" in codes
    assert "source.duplicate_material_object" in codes
    assert "source.digest_mismatch" in codes
    assert "source.byte_length_mismatch" in codes


def test_manifest_rejects_tampering_latest_versions_duplicate_materials_and_wrong_digests() -> None:
    corpus, _, _, _, _ = _fixture()
    payload = corpus.model_dump(mode="json")
    payload["inventory_version"] = "2.0.0"
    with pytest.raises(ValidationError, match="corpus_snapshot_digest"):
        SourceCorpusManifest.model_validate_json(json.dumps(payload))

    create_payload = corpus.model_dump(exclude={"corpus_snapshot_digest"})
    create_payload["inventory_version"] = "latest"
    with pytest.raises(ValidationError, match="latest"):
        SourceCorpusManifest.create(**create_payload)

    duplicate = deepcopy(corpus.model_dump(exclude={"corpus_snapshot_digest"}))
    duplicate["sources"][1]["materials"][0]["material_id"] = "main-primary"
    with pytest.raises(ValidationError, match="unique across the corpus"):
        SourceCorpusManifest.create(**duplicate)

    wrong_primary = deepcopy(corpus.model_dump(exclude={"corpus_snapshot_digest"}))
    wrong_primary["sources"][0]["materials"][0]["sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="SourceRecord.source_sha256"):
        SourceCorpusManifest.create(**wrong_primary)


def test_index_rejects_real_bytes_outside_declared_usage_scope() -> None:
    corpus, dataset, scopes, objects, bytes_by_id, _source = _source_verification()
    primary_entry = corpus.sources[0]
    primary = next(item for item in primary_entry.materials if item.kind == "primary_source")
    artifacts = list(_index_artifacts(corpus, bytes_by_id))
    artifacts.append(
        IndexInputArtifact(
            artifact_id="index-main-primary-wrong-scope",
            artifact_type="source_material",
            content_bytes=MAIN_PRIMARY,
            source_identity=CorpusMaterialIdentity(
                paper_id=primary_entry.source.paper_id,
                document_version=primary_entry.source.document_version,
                source_sha256=primary_entry.source.source_sha256,
                material_id=primary.material_id,
                kind=primary.kind,
            ),
        )
    )

    index = verify_index_corpus(
        corpus=corpus,
        dataset=dataset,
        question_scopes=scopes,
        material_objects=objects,
        artifacts=artifacts,
    )

    assert index.disposition == "system_failure"
    assert "index.material_outside_usage_scope" in {issue.code for issue in index.issues}
