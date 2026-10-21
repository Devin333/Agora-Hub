from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from backend.research.application.evaluation import EvaluationApplicationService
from backend.research.document.models import PaperChunk
from backend.research.ports.evaluation_dataset import DatasetRevision
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    canonical_json_bytes,
    content_digest,
)
from backend.research.rag.evaluation.protocol_corpus import (
    CorpusMaterialDescriptor,
    CorpusSourceEntry,
    SourceCorpusManifest,
    sha256_bytes,
)
from backend.research.rag.evaluation.protocol_dataset import (
    DatasetManifest,
    EvaluationCase,
    SourceRecord,
)
from backend.research.rag.evaluation.protocol_dataset_io import serialize_dataset_bundle
from backend.research.rag.evaluation.protocol_evidence_mapping import (
    CheckedSourceText,
    ChunkSourceSpan,
    ChunkSpanDescriptor,
    IndexBinding,
    SourceLocatorRegion,
    build_index_evidence_mapping,
    calculate_index_content_sha256,
    mapping_to_json,
    text_sha256,
)
from infrastructure.research.evaluation_dataset import (
    FilesystemEvaluationDatasetPublisher,
    FilesystemEvaluationDatasetReader,
)
from infrastructure.research.evaluation_sources import (
    FilesystemEvidenceMappingReader,
    FilesystemSourceCorpusReader,
)
from tests.backend.research.rag.test_protocol_dataset import (
    NOW,
    _case_content,
    _manifest_content,
)

PRIMARY = b"%PDF-1.7 complete fixture paper bytes with figures"
ATTACHMENT = b"supplementary table with all declared rows"
DISTRACTOR_PRIMARY = b"%PDF-1.7 complete distractor"
DISTRACTOR_FULLTEXT = b"distractor methods and unrelated results"
EXCERPT = "The model minimizes the evidence-grounded contrastive objective."
SOURCE_TEXT = ("x" * 120) + EXCERPT + "\nComplete nongold methods, results, and limitations."
FULLTEXT = SOURCE_TEXT.encode("utf-8")


def _source(
    *,
    paper_id: str,
    primary: bytes,
    fulltext: bytes,
    source_uri: str,
) -> SourceRecord:
    return SourceRecord(
        paper_id=paper_id,
        document_version="v2",
        source_uri=source_uri,
        source_sha256=sha256_bytes(primary),
        retrieved_at=NOW,
        fulltext_artifact_digest=sha256_bytes(fulltext),
        completeness="complete",
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
) -> CorpusMaterialDescriptor:
    return CorpusMaterialDescriptor(
        material_id=material_id,
        kind=kind,
        artifact_reference=f"artifact://fixture/{material_id}",
        media_type="text/plain" if kind == "fulltext" else "application/pdf",
        sha256=sha256_bytes(content),
        byte_length=len(content),
        required=True,
        uses=uses,
    )


def validation_fixture() -> tuple[
    DatasetManifest,
    tuple[EvaluationCase, ...],
    SourceCorpusManifest,
    dict[str, bytes],
]:
    main = _source(
        paper_id="paper-1",
        primary=PRIMARY,
        fulltext=FULLTEXT,
        source_uri="https://example.test/paper-1-v2.pdf",
    )
    distractor = _source(
        paper_id="paper-distractor",
        primary=DISTRACTOR_PRIMARY,
        fulltext=DISTRACTOR_FULLTEXT,
        source_uri="https://example.test/distractor-v2.pdf",
    )
    source_sha = main.source_sha256
    normalized_sha = text_sha256(SOURCE_TEXT)
    case_content = deepcopy(_case_content())
    scope = {
        "sources": [{
            "paper_id": main.paper_id,
            "document_version": main.document_version,
            "source_sha256": source_sha,
        }]
    }
    case_content["scope"] = scope
    case_content["provenance"]["question_digest"] = content_digest(  # type: ignore[index]
        {"question_text": case_content["question_text"], "scope": scope}
    )
    case_content["provenance"]["sources"] = [main.model_dump(mode="python")]  # type: ignore[index]
    case_content["review"]["checked_scope"] = scope  # type: ignore[index]
    for anchor in case_content["evidence_anchors"]:  # type: ignore[index]
        anchor["source_sha256"] = source_sha
        anchor["location"]["normalized_fulltext_sha256"] = normalized_sha
    first_anchor = case_content["evidence_anchors"][0]  # type: ignore[index]
    first_anchor["location"]["char_start"] = 120
    first_anchor["location"]["char_end"] = 120 + len(EXCERPT)
    case = EvaluationCase.create(**case_content)

    corpus = SourceCorpusManifest.create(
        record_type="corpus",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id=case.dataset_id,
        dataset_version=case.dataset_version,
        corpus_snapshot_id="fixture-corpus-v1",
        inventory_version="1.0.0",
        sources=(
            CorpusSourceEntry(
                source=main,
                role="question_scope",
                materials=(
                    _descriptor(
                        "main-primary", "primary_source", PRIMARY,
                        uses=("source_verification",),
                    ),
                    _descriptor(
                        "main-fulltext", "fulltext", FULLTEXT,
                        uses=("source_verification", "retrieval_index", "oracle_context"),
                    ),
                    _descriptor(
                        "main-attachment", "attachment", ATTACHMENT,
                        uses=("source_verification", "retrieval_index", "oracle_context"),
                    ),
                ),
            ),
            CorpusSourceEntry(
                source=distractor,
                role="distractor",
                materials=(
                    _descriptor(
                        "distractor-primary", "primary_source", DISTRACTOR_PRIMARY,
                        uses=("source_verification",),
                    ),
                    _descriptor(
                        "distractor-fulltext", "fulltext", DISTRACTOR_FULLTEXT,
                        uses=("source_verification", "retrieval_index"),
                    ),
                ),
            ),
        ),
    )

    manifest_content = deepcopy(_manifest_content(case))
    manifest_content["corpus_snapshot_digest"] = corpus.corpus_snapshot_digest
    manifest_content["paper_versions"] = [
        main.model_dump(mode="python"),
        distractor.model_dump(mode="python"),
    ]
    manifest_content["split_manifest"]["groups"] = [  # type: ignore[index]
        {
            "paper_group_id": "family-1",
            "split": "dev",
            "sources": scope["sources"],
            "grouping_basis": "Main paper revision family.",
            "dispute_resolution": None,
        },
        {
            "paper_group_id": "family-distractor",
            "split": "dev",
            "sources": [{
                "paper_id": distractor.paper_id,
                "document_version": distractor.document_version,
                "source_sha256": distractor.source_sha256,
            }],
            "grouping_basis": "Declared retrieval distractor.",
            "dispute_resolution": None,
        },
    ]
    manifest = DatasetManifest.create(**manifest_content)
    material_bytes = {
        sha256_bytes(PRIMARY): PRIMARY,
        sha256_bytes(FULLTEXT): FULLTEXT,
        sha256_bytes(ATTACHMENT): ATTACHMENT,
        sha256_bytes(DISTRACTOR_PRIMARY): DISTRACTOR_PRIMARY,
        sha256_bytes(DISTRACTOR_FULLTEXT): DISTRACTOR_FULLTEXT,
    }
    return manifest, (case,), corpus, material_bytes


def build_mapping(
    manifest: DatasetManifest,
    case: EvaluationCase,
    *,
    partial: bool = False,
    missing: bool = False,
    ambiguous: bool = False,
    stale_dataset_digest: bool = False,
):
    source = CheckedSourceText.create(
        paper_id="paper-1",
        document_version="v2",
        source_sha256=sha256_bytes(PRIMARY),
        normalized_text=SOURCE_TEXT,
        locator_regions=() if ambiguous else (SourceLocatorRegion(
            source_start=120,
            source_end=120 + len(EXCERPT),
            content_type="text",
            section="Training",
            paragraph_id="training-p1",
            element_id="latex:training:p1",
            table_id=None,
            figure_id=None,
            pdf_page_index=None,
        ),),
    )
    content = EXCERPT[: len(EXCERPT) // 2] if partial else EXCERPT
    chunk = PaperChunk(
        chunk_id="chunk-method",
        paper_id="paper-1",
        parse_source="pdf",
        content=content,
        section_title="Methods",
        metadata={
            "source_locator": "paper://paper-1#methods-p2",
            "document_version": "v2",
            "source_sha256": sha256_bytes(PRIMARY),
        },
    )
    span = ChunkSourceSpan(
        source_start=120,
        source_end=120 + len(content),
        chunk_start=0,
        chunk_end=len(content),
        source_locator="paper://paper-1#methods-p2",
        normalized_text_sha256=source.normalized_text_sha256,
    )
    descriptor = ChunkSpanDescriptor.for_chunk(
        chunk,
        document_version="v2",
        source_sha256=sha256_bytes(PRIMARY),
        source_locator="paper://paper-1#methods-p2",
        spans=(span,),
    )
    chunks = () if missing else (chunk,)
    descriptors = () if missing else (descriptor,)
    binding = IndexBinding(
        corpus_snapshot_id=manifest.corpus_snapshot_id,
        index_snapshot_id="index-v1.0.0",
        index_content_sha256=calculate_index_content_sha256(chunks),
        parser_id="fixture-parser",
        parser_version="1.0.0",
        parser_config_sha256="1" * 64,
        chunker_id="fixture-chunker",
        chunker_version="1.0.0",
        chunker_config_sha256="2" * 64,
        index_config_sha256="3" * 64,
    )
    return build_index_evidence_mapping(
        case=case,
        dataset_digest=("0" * 64 if stale_dataset_digest else manifest.dataset_digest),
        mapping_version="1.0.0",
        binding=binding,
        actual_chunks=chunks,
        checked_sources=(source,),
        chunk_span_descriptors=descriptors,
    )


def write_corpus(root: Path, corpus: SourceCorpusManifest, materials: dict[str, bytes]) -> None:
    directory = root / corpus.corpus_snapshot_id
    (directory / "materials").mkdir(parents=True)
    (directory / "manifest.json").write_bytes(canonical_json_bytes(corpus))
    for digest, content in materials.items():
        (directory / "materials" / digest).write_bytes(content)


def write_mapping(root: Path, manifest: DatasetManifest, case_id: str, mapping: object) -> None:
    directory = root / manifest.dataset_id / manifest.dataset_version / "mappings"
    directory.mkdir(parents=True)
    (directory / f"{case_id}.json").write_text(mapping_to_json(mapping), encoding="utf-8")


def build_service(
    tmp_path: Path,
    *,
    missing_material_digest: str | None = None,
    corrupt_material_digest: str | None = None,
    mapping_kind: str = "valid",
) -> tuple[EvaluationApplicationService, DatasetRevision]:
    manifest, cases, corpus, materials = validation_fixture()
    dataset_root = tmp_path / "datasets"
    corpus_root = tmp_path / "corpus"
    mapping_root = tmp_path / "mappings"
    dataset_root.mkdir()
    corpus_root.mkdir()
    mapping_root.mkdir()

    objects = serialize_dataset_bundle(manifest, cases)
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    receipt = FilesystemEvaluationDatasetPublisher(dataset_root).publish_dataset_objects(
        revision,
        objects,
        dataset_digest=manifest.dataset_digest,
    )
    assert receipt.read_only is True
    if missing_material_digest is not None:
        materials.pop(missing_material_digest)
    if corrupt_material_digest is not None:
        materials[corrupt_material_digest] = b"tampered material bytes"
    write_corpus(corpus_root, corpus, materials)
    mapping = build_mapping(
        manifest,
        cases[0],
        partial=mapping_kind == "partial",
        missing=mapping_kind == "missing",
        ambiguous=mapping_kind == "ambiguous",
        stale_dataset_digest=mapping_kind == "stale",
    )
    write_mapping(mapping_root, manifest, cases[0].case_id, mapping)
    service = EvaluationApplicationService(
        dataset_reader=FilesystemEvaluationDatasetReader(dataset_root),
        source_corpus_reader=FilesystemSourceCorpusReader(corpus_root),
        evidence_mapping_reader=FilesystemEvidenceMappingReader(mapping_root),
    )
    return service, revision


def test_real_published_dataset_complete_corpus_and_mapping_roundtrip(tmp_path: Path) -> None:
    service, revision = build_service(tmp_path)

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        require_complete_corpus=True,
        validate_mappings=True,
    )

    assert result.report.valid is True
    assert result.preflight_complete is True
    assert result.report.formal_ready is False
    assert result.diagnostics["source_corpus"]["status"] == "verified"
    assert result.diagnostics["source_corpus"]["verified_material_count"] == 5
    assert result.diagnostics["source_corpus"]["distractor_source_count"] == 1
    assert result.diagnostics["mapping"]["status"] == "valid"
    assert result.diagnostics["mapping"]["actual_index_binding"]["status"] == "not_evaluated"
    assert result.diagnostics["mapping"]["cases"][0]["required_evidence_anchor_count"] == 2
    assert {item.code for item in result.report.readiness_requirements} == {
        "readiness.actual_runtime_and_budget_required"
    }


def test_missing_source_material_preserves_cases_and_reports_needs_review(tmp_path: Path) -> None:
    missing = sha256_bytes(ATTACHMENT)
    service, revision = build_service(tmp_path, missing_material_digest=missing)

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        require_complete_corpus=True,
    )

    corpus = result.diagnostics["source_corpus"]
    assert result.report.valid is True
    assert result.preflight_complete is False
    assert result.preflight_status == "needs_review"
    assert corpus["status"] == "needs_review"
    assert corpus["planned_case_ids"] == ["case-001"]
    assert corpus["case_statuses"] == [
        {"case_id": "case-001", "disposition": "needs_review"}
    ]
    assert "source.required_material_missing" in {item["code"] for item in corpus["issues"]}


def test_wrong_material_digest_is_needs_review_with_actual_case_trace(tmp_path: Path) -> None:
    service, revision = build_service(
        tmp_path,
        corrupt_material_digest=sha256_bytes(ATTACHMENT),
    )

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        require_complete_corpus=True,
    )

    corpus = result.diagnostics["source_corpus"]
    assert result.report.valid is True
    assert corpus["status"] == "needs_review"
    assert corpus["planned_case_ids"] == ["case-001"]
    digest_issue = next(item for item in corpus["issues"] if item["code"] == "source.digest_mismatch")
    assert digest_issue["case_ids"] == ["case-001"]


def test_wrong_scope_binding_is_rejected_and_preserves_planned_cases(tmp_path: Path) -> None:
    manifest, cases, corpus, materials = validation_fixture()
    corpus_content = corpus.model_dump(mode="python", exclude={"corpus_snapshot_digest"})
    corpus_content["sources"][0]["role"] = "distractor"
    corpus_content["sources"][1]["role"] = "question_scope"
    wrong_scope_corpus = SourceCorpusManifest.create(**corpus_content)
    manifest_content = manifest.model_dump(mode="python", exclude={"dataset_digest"})
    manifest_content["corpus_snapshot_digest"] = wrong_scope_corpus.corpus_snapshot_digest
    wrong_manifest = DatasetManifest.create(**manifest_content)

    dataset_root = tmp_path / "datasets"
    corpus_root = tmp_path / "corpus"
    dataset_root.mkdir()
    corpus_root.mkdir()
    revision = DatasetRevision(wrong_manifest.dataset_id, wrong_manifest.dataset_version)
    FilesystemEvaluationDatasetPublisher(dataset_root).publish_dataset_objects(
        revision,
        serialize_dataset_bundle(wrong_manifest, cases),
        dataset_digest=wrong_manifest.dataset_digest,
    )
    write_corpus(corpus_root, wrong_scope_corpus, materials)
    service = EvaluationApplicationService(
        dataset_reader=FilesystemEvaluationDatasetReader(dataset_root),
        source_corpus_reader=FilesystemSourceCorpusReader(corpus_root),
    )

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        require_complete_corpus=True,
    )

    corpus_diagnostics = result.diagnostics["source_corpus"]
    assert result.report.valid is False
    assert corpus_diagnostics["status"] == "invalid"
    assert corpus_diagnostics["planned_case_ids"] == ["case-001"]
    assert corpus_diagnostics["case_statuses"] == [
        {"case_id": "case-001", "disposition": "needs_review"}
    ]
    assert {issue.code for issue in result.report.issues} == {"corpus.binding_invalid"}


def test_partial_parser_mapping_is_valid_diagnostic_system_failure(tmp_path: Path) -> None:
    service, revision = build_service(tmp_path, mapping_kind="partial")

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        validate_mappings=True,
    )

    mapping = result.diagnostics["mapping"]
    assert result.report.valid is True
    assert result.preflight_complete is False
    assert result.preflight_status == "system_failure"
    assert mapping["status"] == "system_failure"
    assert mapping["planned_case_ids"] == ["case-001"]
    assert mapping["cases"][0]["required_evidence_anchor_count"] == 2
    assert mapping["cases"][0]["mapping_anchor_count"] == 2
    assert "mapping.anchor_partial" in {
        item["code"] for item in mapping["cases"][0]["issues"]
    }


def test_missing_parser_evidence_keeps_gold_and_reports_system_failure(tmp_path: Path) -> None:
    service, revision = build_service(tmp_path, mapping_kind="missing")

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        validate_mappings=True,
    )

    case = result.diagnostics["mapping"]["cases"][0]
    assert result.report.valid is True
    assert result.preflight_status == "system_failure"
    assert case["required_evidence_anchor_count"] == 2
    assert case["mapping_anchor_count"] == 2
    assert case["anchor_status_counts"] == {"missing": 2}
    assert {item["code"] for item in case["issues"]} == {"mapping.anchor_missing"}


def test_ambiguous_mapping_requires_review_without_becoming_gold_failure(tmp_path: Path) -> None:
    service, revision = build_service(tmp_path, mapping_kind="ambiguous")

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        validate_mappings=True,
    )

    case = result.diagnostics["mapping"]["cases"][0]
    assert result.report.valid is True
    assert result.preflight_status == "needs_review"
    assert case["disposition"] == "needs_review"
    assert case["required_evidence_anchor_count"] == 2
    assert "mapping.anchor_ambiguous" in {item["code"] for item in case["issues"]}


def test_stale_mapping_is_rejected_without_lowering_gold_membership(tmp_path: Path) -> None:
    service, revision = build_service(tmp_path, mapping_kind="stale")

    result = service.validate_dataset_preflight(
        revision,
        validate_source_corpus=True,
        validate_mappings=True,
    )

    assert result.report.valid is False
    assert result.preflight_complete is False
    assert result.diagnostics["mapping"]["status"] == "invalid"
    assert result.diagnostics["mapping"]["planned_case_ids"] == ["case-001"]
    assert {issue.code for issue in result.report.issues} == {"mapping.binding_stale"}
