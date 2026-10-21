from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    canonical_json_bytes,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    CaseManifestEntry,
    DatasetManifest,
    EvaluationCase,
    QuestionInput,
    QuestionScope,
    ReferenceRecord,
    QuestionRecord,
    ScopedPaper,
)

NOW = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
SOURCE_SHA = "a" * 64
FULLTEXT_SHA = "b" * 64


def _scope_dict() -> dict[str, object]:
    return {"sources": [{"paper_id": "paper-1", "document_version": "v2", "source_sha256": SOURCE_SHA}]}


def _source_dict() -> dict[str, object]:
    return {
        "paper_id": "paper-1",
        "document_version": "v2",
        "source_uri": "https://example.test/paper-1-v2.pdf",
        "source_sha256": SOURCE_SHA,
        "retrieved_at": NOW,
        "fulltext_artifact_digest": FULLTEXT_SHA,
        "completeness": "complete",
        "access_status": "available",
        "source_format": "pdf",
        "license_reference_id": "fixture-license",
    }


def _review_dict(*, status: str = "approved") -> dict[str, object]:
    return {
        "status": status,
        "reviewer_id": "fixture-human-reviewer" if status == "approved" else None,
        "reviewer_kind": "human" if status == "approved" else None,
        "reviewed_at": NOW if status == "approved" else None,
        "notes": "Simulated human decision used only by this contract test fixture.",
        "source_completeness": "complete" if status == "approved" else "unknown",
        "checked_scope": _scope_dict() if status == "approved" else None,
        "unanswerable_reason": None,
        "evidence_reference": "artifact://fixture/case-review",
    }


def _case_content(*, split: str = "dev") -> dict[str, object]:
    question = QuestionInput(question_text="Which optimization objective is reported?", scope=QuestionScope(sources=(ScopedPaper(paper_id="paper-1", document_version="v2", source_sha256=SOURCE_SHA),)))
    excerpt = "The model minimizes the evidence-grounded contrastive objective."
    return {
        "record_type": "case",
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "dataset_id": "fixture-dataset",
        "dataset_version": "1.0.0",
        "case_id": "case-001",
        "question_text": question.question_text,
        "scope": _scope_dict(),
        "question_type": "method",
        "paper_group_id": "family-1",
        "split": split,
        "answerability": "answerable",
        "expected_behavior": "answer",
        "reference_answer": "It minimizes an evidence-grounded contrastive objective.",
        "reference_claims": [{"claim_id": "claim-objective", "claim_text": "The objective is evidence-grounded contrastive learning."}],
        "evidence_anchors": [
            {
                "anchor_id": "anchor-method-a",
                "paper_id": "paper-1",
                "document_version": "v2",
                "source_sha256": SOURCE_SHA,
                "claim_ids": ["claim-objective"],
                "location": {
                    "content_type": "text",
                    "pdf_page_index": 3,
                    "printed_page": "4",
                    "section": "Methods",
                    "paragraph_id": "methods-p2",
                    "char_start": 120,
                    "char_end": 186,
                    "element_id": None,
                    "table_id": None,
                    "row_labels": [],
                    "column_labels": [],
                    "cell_references": [],
                    "figure_id": None,
                    "caption": None,
                    "normalized_fulltext_sha256": FULLTEXT_SHA,
                },
                "source_excerpt": excerpt,
                "source_excerpt_digest": content_digest(excerpt),
            },
            {
                "anchor_id": "anchor-method-b",
                "paper_id": "paper-1",
                "document_version": "v2",
                "source_sha256": SOURCE_SHA,
                "claim_ids": ["claim-objective"],
                "location": {
                    "content_type": "text",
                    "pdf_page_index": None,
                    "printed_page": None,
                    "section": "Training",
                    "paragraph_id": "training-p1",
                    "char_start": None,
                    "char_end": None,
                    "element_id": "latex:training:p1",
                    "table_id": None,
                    "row_labels": [],
                    "column_labels": [],
                    "cell_references": [],
                    "figure_id": None,
                    "caption": None,
                    "normalized_fulltext_sha256": FULLTEXT_SHA,
                },
                "source_excerpt": excerpt,
                "source_excerpt_digest": content_digest(excerpt),
            },
        ],
        "required_evidence_groups": [
            {
                "group_id": "method",
                "claim_ids": ["claim-objective"],
                "alternative_sufficient_sets": [
                    {"set_id": "pdf-path", "required_anchor_ids": ["anchor-method-a"]},
                    {"set_id": "latex-path", "required_anchor_ids": ["anchor-method-b"]},
                ],
            }
        ],
        "provenance": {
            "authoring_method": "abstract_only",
            "original_dataset_id": None,
            "original_case_id": None,
            "derived_from_case_ids": [],
            "question_author_id": "fixture-question-author",
            "author_input_manifest": {
                "visible_materials": ["title", "abstract", "declared_scope"],
                "input_digest": "c" * 64,
            },
            "question_digest": content_digest(question),
            "question_frozen_at": NOW,
            "annotator_id": "fixture-annotator",
            "annotation_input_manifest": {
                "visible_materials": ["fulltext", "declared_scope"],
                "input_digest": "2" * 64,
            },
            "annotation_started_at": datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
            "assistance": [
                {
                    "actor_kind": "llm",
                    "actor_id": "fixture-authoring-assistant",
                    "model_or_rule_version": "fixture-model/v1",
                    "input_scope": ["title", "abstract", "declared_scope"],
                    "generation_config_digest": "d" * 64,
                }
            ],
            "sources": [_source_dict()],
        },
        "exposure_history": {
            "status": "clean",
            "events": [
                {
                    "subject": "paper_fulltext",
                    "purpose": "normal_corpus_indexing",
                    "exposed_to": "fixture-evaluation-index",
                    "occurred_at": NOW,
                    "evidence_reference": "artifact://fixture/exposure-1",
                }
            ],
            "assessed_at": NOW,
            "assessed_by": "fixture-human-reviewer",
            "assessment_evidence_reference": "artifact://fixture/exposure-assessment",
        },
        "review": _review_dict(),
    }


def _case(*, split: str = "dev") -> EvaluationCase:
    return EvaluationCase.create(**_case_content(split=split))


def _manifest_content(case: EvaluationCase) -> dict[str, object]:
    return {
        "record_type": "dataset",
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "dataset_id": case.dataset_id,
        "dataset_version": case.dataset_version,
        "lifecycle_state": "frozen",
        "release_purpose": "dev_regression",
        "corpus_snapshot_id": "fixture-corpus-v1",
        "corpus_snapshot_digest": "e" * 64,
        "paper_versions": [_source_dict()],
        "case_manifest": [
            {
                "case_id": case.case_id,
                "case_digest": case.case_digest,
                "question_digest": content_digest(case.question_projection()),
                "reference_digest": content_digest(case.reference_projection()),
                "paper_group_id": case.paper_group_id,
                "split": case.split,
                "review_status": case.review.status,
            }
        ],
        "split_manifest": {
            "method": "paper-family",
            "method_version": "fixture/v1",
            "deterministic_seed": "fixed-fixture-seed",
            "groups": [
                {
                    "paper_group_id": "family-1",
                    "split": case.split,
                    "sources": _scope_dict()["sources"],
                    "grouping_basis": "Same paper revision family.",
                    "dispute_resolution": None,
                }
            ],
        },
        "review_manifest": {
            "status": "approved",
            "reviewer_id": "fixture-human-reviewer",
            "reviewer_kind": "human",
            "reviewed_at": NOW,
            "approved_case_ids": [case.case_id],
            "corpus_checked": True,
            "split_checked": True,
            "contract_checked": True,
            "evidence_reference": "artifact://fixture/dataset-review",
        },
        "annotation_version": "fixture-annotation/v1",
        "license_reference": {
            "reference_id": "fixture-license",
            "reference_uri": "https://example.test/license",
            "terms_digest": "f" * 64,
            "permitted_use": "internal_evaluation",
        },
        "authoring_protocol": {
            "protocol_id": "question-first",
            "protocol_version": "fixture/v1",
            "protocol_digest": "1" * 64,
        },
        "supersedes": None,
        "correction_reason": None,
        "frozen_at": NOW,
    }


def test_common_digest_is_canonical_and_rejects_non_finite_values() -> None:
    assert canonical_json_bytes({"b": "中文", "a": 1}) == b'{"a":1,"b":"\xe4\xb8\xad\xe6\x96\x87"}'.decode("unicode_escape").encode("latin1")
    assert content_digest({"a": 1, "b": 2}) == content_digest({"b": 2, "a": 1})
    for non_finite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json_bytes({"bad": non_finite})


def test_case_round_trip_schema_projection_and_immutable_contract() -> None:
    case = _case()
    restored = EvaluationCase.model_validate_json(case.model_dump_json())
    assert restored == case
    assert EvaluationCase.model_json_schema()["additionalProperties"] is False
    assert case.question_projection().model_dump(mode="json") == {
        "question_text": case.question_text,
        "scope": {"sources": [{"paper_id": "paper-1", "document_version": "v2", "source_sha256": SOURCE_SHA}]},
    }
    reference = case.reference_projection().model_dump(mode="json")
    assert "case_id" not in reference and "question_text" not in reference
    assert reference["reference_answer"] == case.reference_answer
    with pytest.raises(ValidationError):
        case.question_text = "mutated"  # type: ignore[misc]


def test_question_input_deeply_rejects_gold_handles_metadata_and_complete_case() -> None:
    allowed = {"question_text": "Question?", "scope": _scope_dict()}
    for injected in (
        {"gold_chunk_ids": ["gold-1"]},
        {"reference_path": "references.jsonl"},
        {"reference_callback": "lookup"},
        {"case": _case().model_dump(mode="json")},
        {"case_id": "case-001"},
        {"metadata": {"tenant": "wrong-layer"}},
    ):
        with pytest.raises(ValidationError):
            QuestionInput.model_validate_json(json.dumps({**allowed, **injected}))
    nested = deepcopy(allowed)
    nested["scope"]["sources"][0]["gold_anchor_ids"] = ["anchor-method-a"]  # type: ignore[index]
    with pytest.raises(ValidationError):
        QuestionInput.model_validate_json(json.dumps(nested))



def test_physical_question_and_reference_records_are_linked_but_permission_separated() -> None:
    case = _case()
    question = case.question_record()
    reference = case.reference_record()
    assert question.case_digest == reference.case_digest == case.case_digest
    assert question.question_input == case.question_projection()
    assert reference.reference_label == case.reference_projection()
    question_wire = question.model_dump(mode="json")
    assert "reference_answer" not in json.dumps(question_wire)
    assert "case_id" not in question_wire["question_input"]
    assert QuestionRecord.model_validate_json(question.model_dump_json()) == question
    assert ReferenceRecord.model_validate_json(reference.model_dump_json()) == reference


def test_physical_projection_records_verify_their_content_digests() -> None:
    question = _case().question_record().model_dump(mode="json")
    question["question_input"]["question_text"] = "Tampered"
    with pytest.raises(ValidationError, match="question_digest"):
        QuestionRecord.model_validate_json(json.dumps(question))
    reference = _case().reference_record().model_dump(mode="json")
    reference["reference_label"]["reference_answer"] = "Tampered"
    with pytest.raises(ValidationError, match="reference_digest"):
        ReferenceRecord.model_validate_json(json.dumps(reference))

def test_wire_versions_are_explicit_and_unknown_or_missing_versions_are_rejected() -> None:
    content = _case_content()
    for field in ("record_type", "schema_version", "protocol_version"):
        missing = deepcopy(content)
        del missing[field]
        with pytest.raises(ValidationError):
            EvaluationCase.create(**missing)
    wrong = deepcopy(content)
    wrong["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        EvaluationCase.create(**wrong)



def test_immutable_versions_reject_latest_across_dataset_source_and_annotation() -> None:
    latest_case = _case_content()
    latest_case["dataset_version"] = "LATEST"
    with pytest.raises(ValidationError, match="latest"):
        EvaluationCase.create(**latest_case)
    latest_source = _case_content()
    latest_source["scope"]["sources"][0]["document_version"] = "latest"  # type: ignore[index]
    latest_source["provenance"]["sources"][0]["document_version"] = "latest"  # type: ignore[index]
    latest_source["review"]["checked_scope"]["sources"][0]["document_version"] = "latest"  # type: ignore[index]
    with pytest.raises(ValidationError, match="latest"):
        EvaluationCase.create(**latest_source)
    case = _case()
    latest_annotation = _manifest_content(case)
    latest_annotation["annotation_version"] = "latest"
    with pytest.raises(ValidationError, match="latest"):
        DatasetManifest.create(**latest_annotation)


def test_physical_wire_records_require_explicit_case_record_type_and_versions() -> None:
    for record in (_case().question_record(), _case().reference_record()):
        payload = record.model_dump(mode="json")
        assert payload["record_type"] == "case"
        for required in ("record_type", "schema_version", "protocol_version"):
            missing = deepcopy(payload)
            del missing[required]
            with pytest.raises(ValidationError):
                type(record).model_validate_json(json.dumps(missing))


def test_approved_human_labels_require_external_review_evidence() -> None:
    case_content = _case_content()
    case_content["review"]["evidence_reference"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="external evidence"):
        EvaluationCase.create(**case_content)
    case = _case()
    manifest_content = _manifest_content(case)
    manifest_content["review_manifest"]["evidence_reference"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="external evidence"):
        DatasetManifest.create(**manifest_content)

def test_missing_explicit_null_and_inconsistent_answerability_are_rejected() -> None:
    missing = _case_content()
    del missing["reference_answer"]
    with pytest.raises(ValidationError):
        EvaluationCase.create(**missing)
    inconsistent = _case_content()
    inconsistent["expected_behavior"] = "abstain"
    with pytest.raises(ValidationError):
        EvaluationCase.create(**inconsistent)


def test_dangling_claim_anchor_and_incomplete_alternative_set_are_rejected() -> None:
    dangling_claim = _case_content()
    dangling_claim["evidence_anchors"][0]["claim_ids"] = ["missing-claim"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown claim_id"):
        EvaluationCase.create(**dangling_claim)
    dangling_anchor = _case_content()
    dangling_anchor["required_evidence_groups"][0]["alternative_sufficient_sets"][0]["required_anchor_ids"] = ["missing-anchor"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown anchor_id"):
        EvaluationCase.create(**dangling_anchor)
    unsupported = _case_content()
    unsupported["reference_claims"].append({"claim_id": "claim-extra", "claim_text": "An extra required fact."})  # type: ignore[union-attr]
    unsupported["required_evidence_groups"][0]["claim_ids"].append("claim-extra")  # type: ignore[index]
    with pytest.raises(ValidationError, match="does not support every claim"):
        EvaluationCase.create(**unsupported)


def test_duplicate_identifiers_and_source_excerpt_tampering_are_rejected() -> None:
    duplicate = _case_content()
    duplicate["reference_claims"].append(deepcopy(duplicate["reference_claims"][0]))  # type: ignore[union-attr,index]
    with pytest.raises(ValidationError, match="duplicate"):
        EvaluationCase.create(**duplicate)
    tampered = _case_content()
    tampered["evidence_anchors"][0]["source_excerpt"] = "Changed without updating its digest."  # type: ignore[index]
    with pytest.raises(ValidationError, match="source_excerpt_digest"):
        EvaluationCase.create(**tampered)


def test_abstract_only_and_machine_approval_boundaries_are_enforced() -> None:
    leaked = _case_content()
    leaked["provenance"]["author_input_manifest"]["visible_materials"] = ["title", "fulltext"]  # type: ignore[index]
    with pytest.raises(ValidationError, match="abstract_only"):
        EvaluationCase.create(**leaked)
    auto_approved = _case_content()
    auto_approved["review"]["reviewer_kind"] = "llm"  # type: ignore[index]
    with pytest.raises(ValidationError, match="named human"):
        EvaluationCase.create(**auto_approved)



def test_question_first_requires_independent_annotation_and_strict_stage_order() -> None:
    same_actor = _case_content()
    same_actor["provenance"]["annotator_id"] = "fixture-question-author"  # type: ignore[index]
    with pytest.raises(ValidationError, match="independent annotator"):
        EvaluationCase.create(**same_actor)
    wrong_order = _case_content()
    wrong_order["provenance"]["annotation_started_at"] = NOW  # type: ignore[index]
    with pytest.raises(ValidationError, match="after question_frozen_at"):
        EvaluationCase.create(**wrong_order)


def test_table_evidence_requires_semantic_row_column_or_cell_localization() -> None:
    content = _case_content()
    location = content["evidence_anchors"][0]["location"]  # type: ignore[index]
    location.update(content_type="table", table_id="table-2", row_labels=[], column_labels=[], cell_references=[])
    with pytest.raises(ValidationError, match="table evidence"):
        EvaluationCase.create(**content)


def test_legacy_import_cannot_be_approved_as_heldout() -> None:
    content = _case_content(split="heldout")
    content["provenance"]["authoring_method"] = "legacy_import"  # type: ignore[index]
    with pytest.raises(ValidationError, match="legacy imports"):
        EvaluationCase.create(**content)

def test_needs_review_cannot_be_frozen_and_has_no_decided_behavior() -> None:
    content = _case_content()
    content["answerability"] = "needs_review"
    content["expected_behavior"] = None
    content["reference_answer"] = None
    content["review"] = _review_dict(status="needs_review")
    candidate = EvaluationCase.create(**content)
    assert candidate.answerability == "needs_review"
    entry = CaseManifestEntry(
        case_id=candidate.case_id,
        case_digest=candidate.case_digest,
        question_digest=content_digest(candidate.question_projection()),
        reference_digest=content_digest(candidate.reference_projection()),
        paper_group_id=candidate.paper_group_id,
        split=candidate.split,
        review_status="needs_review",
    )
    manifest_content = _manifest_content(_case())
    manifest_content["case_manifest"] = [entry.model_dump(mode="json")]
    manifest_content["review_manifest"]["approved_case_ids"] = []  # type: ignore[index]
    with pytest.raises(ValidationError, match="approved"):
        DatasetManifest.create(**manifest_content)


def test_unanswerable_requires_complete_human_scope_review_and_explicit_empty_groups() -> None:
    content = _case_content()
    content.update(answerability="unanswerable", expected_behavior="abstain", reference_answer=None, reference_claims=[], evidence_anchors=[], required_evidence_groups=[])
    content["review"]["unanswerable_reason"] = "The complete scoped paper does not report this experiment."  # type: ignore[index]
    case = EvaluationCase.create(**content)
    assert case.reference_answer is None and case.required_evidence_groups == ()
    invalid = deepcopy(content)
    invalid["review"]["source_completeness"] = "incomplete"  # type: ignore[index]
    with pytest.raises(ValidationError):
        EvaluationCase.create(**invalid)


def test_heldout_unknown_or_tuning_exposure_cannot_receive_clean_approval() -> None:
    unknown = _case_content(split="heldout")
    unknown["exposure_history"]["status"] = "unknown"  # type: ignore[index]
    with pytest.raises(ValidationError, match="heldout"):
        EvaluationCase.create(**unknown)
    tuning = _case_content(split="heldout")
    tuning["exposure_history"]["events"][0]["purpose"] = "prompt_tuning"  # type: ignore[index]
    with pytest.raises(ValidationError):
        EvaluationCase.create(**tuning)


def test_case_digest_detects_tampering_and_create_computes_it() -> None:
    case = _case()
    assert case.case_digest == case.calculated_digest()
    payload = case.model_dump(mode="json")
    payload["reference_answer"] = "Tampered answer"
    with pytest.raises(ValidationError, match="case_digest"):
        EvaluationCase.model_validate_json(json.dumps(payload))


def test_manifest_roundtrip_membership_digest_duplicate_and_dangling_group_checks() -> None:
    case = _case()
    manifest = DatasetManifest.create(**_manifest_content(case))
    assert DatasetManifest.model_validate_json(manifest.model_dump_json()) == manifest
    assert manifest.dataset_digest == manifest.calculated_digest()
    manifest.validate_cases([case])
    with pytest.raises(ValueError, match="duplicate case_id"):
        manifest.validate_cases([case, case])
    dangling = _manifest_content(case)
    dangling["case_manifest"][0]["paper_group_id"] = "missing-family"  # type: ignore[index]
    with pytest.raises(ValidationError, match="unknown paper_group_id"):
        DatasetManifest.create(**dangling)


def test_manifest_rejects_cross_split_family_and_content_or_membership_tampering() -> None:
    case = _case()
    manifest = DatasetManifest.create(**_manifest_content(case))
    payload = manifest.model_dump(mode="json")
    payload["annotation_version"] = "tampered"
    with pytest.raises(ValidationError, match="dataset_digest"):
        DatasetManifest.model_validate_json(json.dumps(payload))
    other = EvaluationCase.create(**{**_case_content(), "case_id": "case-002"})
    with pytest.raises(ValueError, match="membership mismatch"):
        manifest.validate_cases([other])
    crossed = _manifest_content(case)
    duplicate_group = deepcopy(crossed["split_manifest"]["groups"][0])  # type: ignore[index]
    duplicate_group["paper_group_id"] = "family-2"
    duplicate_group["split"] = "heldout"
    crossed["split_manifest"]["groups"].append(duplicate_group)  # type: ignore[index]
    with pytest.raises(ValidationError, match="multiple paper groups|cross"):
        DatasetManifest.create(**crossed)



def test_validate_cases_requires_exact_provenance_source_record_not_only_three_keys() -> None:
    altered = _case_content()
    altered["provenance"]["sources"][0]["source_uri"] = "https://example.test/mirror/paper-1-v2.pdf"  # type: ignore[index]
    altered_case = EvaluationCase.create(**altered)
    manifest_content = _manifest_content(altered_case)
    manifest_content["paper_versions"] = [_source_dict()]
    manifest = DatasetManifest.create(**manifest_content)
    with pytest.raises(ValueError, match="exactly match paper_versions"):
        manifest.validate_cases([altered_case])


def test_manifest_rejects_source_license_that_differs_from_dataset_license() -> None:
    case = _case()
    content = _manifest_content(case)
    content["paper_versions"][0]["license_reference_id"] = "another-license"  # type: ignore[index]
    with pytest.raises(ValidationError, match="license_reference"):
        DatasetManifest.create(**content)


def test_needs_review_and_multi_source_cases_refuse_legacy_single_paper_projection() -> None:
    needs_review = _case_content()
    needs_review["answerability"] = "needs_review"
    needs_review["expected_behavior"] = None
    needs_review["reference_answer"] = None
    needs_review["review"] = _review_dict(status="needs_review")
    unresolved = EvaluationCase.create(**needs_review)
    with pytest.raises(ValueError, match="needs_review"):
        unresolved.to_evidence_qa_pair_diagnostic(verified_anchor_chunk_ids=None)

    multi = _case_content()
    second_scope = {"paper_id": "paper-2", "document_version": "v1", "source_sha256": "3" * 64}
    second_source = {**_source_dict(), **second_scope, "source_uri": "https://example.test/paper-2-v1.pdf", "fulltext_artifact_digest": "4" * 64}
    multi["scope"]["sources"].append(second_scope)  # type: ignore[index]
    multi["review"]["checked_scope"]["sources"].append(second_scope)  # type: ignore[index]
    multi["provenance"]["sources"].append(second_source)  # type: ignore[index]
    question = QuestionInput.model_validate_json(json.dumps({"question_text": multi["question_text"], "scope": multi["scope"]}))
    multi["provenance"]["question_digest"] = content_digest(question)  # type: ignore[index]
    multi_case = EvaluationCase.create(**multi)
    with pytest.raises(ValueError, match="exactly one scoped paper"):
        multi_case.to_evidence_qa_pair_diagnostic(verified_anchor_chunk_ids={"anchor-method-a": ["c1"], "anchor-method-b": ["c2"]})


def test_normalized_locator_digest_and_source_format_rules_are_enforced() -> None:
    digest_mismatch = _case_content()
    digest_mismatch["evidence_anchors"][0]["location"]["normalized_fulltext_sha256"] = "9" * 64  # type: ignore[index]
    with pytest.raises(ValidationError, match="normalized fulltext digest"):
        EvaluationCase.create(**digest_mismatch)

    missing_normalized = _case_content()
    missing_normalized["evidence_anchors"][0]["location"]["normalized_fulltext_sha256"] = None  # type: ignore[index]
    with pytest.raises(ValidationError, match="normalized_fulltext_sha256"):
        EvaluationCase.create(**missing_normalized)

    latex_pdf_page = _case_content()
    latex_pdf_page["provenance"]["sources"][0]["source_format"] = "latex"  # type: ignore[index]
    with pytest.raises(ValidationError, match="LaTeX source"):
        EvaluationCase.create(**latex_pdf_page)


def test_table_location_requires_cells_or_both_row_and_column_labels() -> None:
    row_only = _case_content()
    location = row_only["evidence_anchors"][0]["location"]  # type: ignore[index]
    location.update(content_type="table", table_id="table-2", row_labels=["Accuracy"], column_labels=[], cell_references=[])
    with pytest.raises(ValidationError, match="both row and column"):
        EvaluationCase.create(**row_only)


def test_diagnostic_projection_is_explicit_and_never_invents_chunk_mapping() -> None:
    case = _case()
    with pytest.raises(ValueError, match="verified anchor mappings"):
        case.to_evidence_qa_pair_diagnostic(verified_anchor_chunk_ids=None)
    pair = case.to_evidence_qa_pair_diagnostic(
        verified_anchor_chunk_ids={"anchor-method-a": ["chunk-1"], "anchor-method-b": ["chunk-2"]}
    )
    assert isinstance(pair, EvidenceQAPair)
    assert pair.gold_chunk_ids == ["chunk-1", "chunk-2"]
    assert pair.metadata["projection"] == "source_anchor_diagnostic"


def test_models_reject_non_strict_inputs_and_portability_leaks() -> None:
    scope = _scope_dict()
    scope["sources"][0]["paper_id"] = 1  # type: ignore[index]
    with pytest.raises(ValidationError):
        QuestionScope.model_validate_json(json.dumps(scope))
    local_path = _case_content()
    local_path["provenance"]["sources"][0]["source_uri"] = "F:/private/paper.pdf"  # type: ignore[index]
    with pytest.raises(ValidationError, match="local path"):
        EvaluationCase.create(**local_path)


def test_dumped_values_are_plain_json_and_nested_values_are_not_mutable_aliases() -> None:
    case = _case()
    dumped = case.model_dump(mode="json")
    json.dumps(dumped, allow_nan=False)
    assert isinstance(case.scope.sources, tuple)
    readonly = MappingProxyType(dumped)
    assert readonly["case_id"] == "case-001"


@pytest.mark.parametrize("alias", ["current", "CURRENT", "head", "HEAD"])
def test_shared_version_contract_rejects_other_floating_aliases(alias: str) -> None:
    content = _case_content()
    content["dataset_version"] = alias
    with pytest.raises(ValidationError, match="immutable"):
        EvaluationCase.create(**content)
    source = _scope_dict()["sources"][0]
    source["document_version"] = alias
    with pytest.raises(ValidationError, match="immutable"):
        ScopedPaper.model_validate(source)
