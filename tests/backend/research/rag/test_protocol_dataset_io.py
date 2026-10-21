from __future__ import annotations

from backend.research.ports.evaluation_dataset import DatasetBundleBytes

import json
from dataclasses import fields
from typing import Any

import pytest

from backend.research.rag.evaluation.protocol_common import canonical_json_bytes, content_digest
from backend.research.rag.evaluation.protocol_dataset import DatasetManifest, EvaluationCase, QuestionInput
from backend.research.rag.evaluation.protocol_dataset_io import (
    DatasetBundle,
    DatasetValidationMode,
    FrozenDatasetRevisionConflict,
    ensure_frozen_revision_is_not_replaced,
    load_dataset_bundle,
    serialize_dataset_bundle,
)
from tests.backend.research.rag.test_protocol_dataset import (
    _case,
    _case_content,
    _manifest_content,
)


def _manifest_for(cases: tuple[EvaluationCase, ...]) -> DatasetManifest:
    content = _manifest_content(cases[0])
    content["case_manifest"] = [
        {
            "case_id": case.case_id,
            "case_digest": case.case_digest,
            "question_digest": content_digest(case.question_projection()),
            "reference_digest": content_digest(case.reference_projection()),
            "paper_group_id": case.paper_group_id,
            "split": case.split,
            "review_status": case.review.status,
        }
        for case in cases
    ]
    content["review_manifest"]["approved_case_ids"] = [case.case_id for case in cases]  # type: ignore[index]
    return DatasetManifest.create(**content)


def _frozen_bundle(*, two_cases: bool = False) -> tuple[DatasetManifest, tuple[EvaluationCase, ...], DatasetBundleBytes]:
    first = _case()
    cases = (first,)
    if two_cases:
        second_content = _case_content()
        second_content["case_id"] = "case-002"
        second = EvaluationCase.create(**second_content)
        cases = (first, second)
    manifest = _manifest_for(cases)
    return manifest, cases, serialize_dataset_bundle(manifest, cases)


def _jsonl_values(raw: bytes) -> list[dict[str, Any]]:
    return [json.loads(line) for line in raw.decode("utf-8").splitlines()]


def _jsonl_bytes(values: list[dict[str, Any]]) -> bytes:
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def _codes(result: Any) -> set[str]:
    return {issue.code for issue in result.report.issues}



def test_bundle_objects_require_concrete_bytes() -> None:
    with pytest.raises(TypeError, match="manifest_json"):
        DatasetBundleBytes("manifest.json", b"", b"")  # type: ignore[arg-type]


def test_unanswerable_explicit_null_reference_roundtrips_but_missing_field_does_not() -> None:
    content = _case_content()
    content.update(
        answerability="unanswerable",
        expected_behavior="abstain",
        reference_answer=None,
        reference_claims=[],
        evidence_anchors=[],
        required_evidence_groups=[],
    )
    content["review"]["unanswerable_reason"] = "Complete scoped material does not report this result."  # type: ignore[index]
    case = EvaluationCase.create(**content)
    manifest = _manifest_for((case,))
    objects = serialize_dataset_bundle(manifest, (case,))

    valid = load_dataset_bundle(objects, mode=DatasetValidationMode.FROZEN)
    assert valid.report.valid is True
    assert valid.require_bundle().cases[0].reference_answer is None

    rows = _jsonl_values(objects.references_jsonl)
    rows[0]["reference_label"].pop("reference_answer")
    invalid = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, objects.questions_jsonl, _jsonl_bytes(rows))
    )
    assert invalid.report.valid is False
    assert "reference.invalid" in _codes(invalid)


def test_mixed_case_paper_family_metadata_does_not_bypass_exact_manifest_binding() -> None:
    _, _, objects = _frozen_bundle()
    rows = _jsonl_values(objects.questions_jsonl)
    rows[0]["paper_group_id"] = "Family-1"

    result = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, _jsonl_bytes(rows), objects.references_jsonl)
    )

    assert result.report.valid is False
    assert "bundle.group_split_mismatch" in _codes(result)

def test_valid_three_object_frozen_roundtrip_is_execution_data_ready_but_not_quality_pass() -> None:
    manifest, cases, objects = _frozen_bundle(two_cases=True)

    result = load_dataset_bundle(objects, mode=DatasetValidationMode.FROZEN)

    assert result.report.valid is True
    assert result.report.ready_for_execution_data is True
    assert result.report.formal_ready is False
    assert result.report.issues == ()
    assert [issue.code for issue in result.report.readiness_requirements] == [
        "readiness.source_corpus_validation_required"
    ]
    bundle = result.require_bundle()
    assert bundle.manifest == manifest
    assert bundle.cases == cases
    assert serialize_dataset_bundle(bundle.manifest, bundle.cases) == objects
    assert b"F:/" not in objects.manifest_json + objects.questions_jsonl + objects.references_jsonl
    assert not hasattr(result.report, "quality_pass")


def test_blind_projection_contains_no_reference_or_reference_handle() -> None:
    _, _, objects = _frozen_bundle()
    bundle = load_dataset_bundle(objects, mode=DatasetValidationMode.FROZEN).require_bundle()

    blind = bundle.blind_inputs()[0]

    assert {field.name for field in fields(blind)} == {"case_id", "question_input"}
    assert isinstance(blind.question_input, QuestionInput)
    payload = blind.question_input.model_dump(mode="json")
    assert set(payload) == {"question_text", "scope"}
    rendered = json.dumps(payload)
    for forbidden in ("reference", "answerability", "expected_behavior", "gold", "callback", "path"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("member", "mutate", "expected_code"),
    [
        (
            "questions",
            lambda rows: rows[0].__setitem__("schema_version", "999"),
            "question.invalid",
        ),
        (
            "questions",
            lambda rows: rows[0].__setitem__("unexpected", True),
            "question.invalid",
        ),
        (
            "references",
            lambda rows: rows[0].__setitem__("unexpected", True),
            "reference.invalid",
        ),
        (
            "references",
            lambda rows: rows[0]["reference_label"].pop("reference_answer"),
            "reference.invalid",
        ),
    ],
)
def test_unknown_schema_fields_and_missing_explicit_reference_null_are_rejected(
    member: str,
    mutate: Any,
    expected_code: str,
) -> None:
    _, _, objects = _frozen_bundle()
    rows = _jsonl_values(objects.questions_jsonl if member == "questions" else objects.references_jsonl)
    mutate(rows)
    changed = DatasetBundleBytes(
        manifest_json=objects.manifest_json,
        questions_jsonl=_jsonl_bytes(rows) if member == "questions" else objects.questions_jsonl,
        references_jsonl=_jsonl_bytes(rows) if member == "references" else objects.references_jsonl,
    )

    result = load_dataset_bundle(changed)

    assert result.report.valid is False
    assert expected_code in _codes(result)


def test_duplicate_json_keys_invalid_json_duplicate_cases_and_missing_reference_are_rejected() -> None:
    _, _, objects = _frozen_bundle()
    question_line = objects.questions_jsonl.rstrip(b"\n")

    duplicate_key = DatasetBundleBytes(
        manifest_json=b'{"record_type":"dataset","record_type":"dataset"}',
        questions_jsonl=objects.questions_jsonl,
        references_jsonl=objects.references_jsonl,
    )
    assert "json.duplicate_key" in _codes(load_dataset_bundle(duplicate_key))

    invalid_json = DatasetBundleBytes(
        manifest_json=objects.manifest_json,
        questions_jsonl=b"{not-json}\n",
        references_jsonl=objects.references_jsonl,
    )
    assert "json.invalid" in _codes(load_dataset_bundle(invalid_json))

    duplicate_case = DatasetBundleBytes(
        manifest_json=objects.manifest_json,
        questions_jsonl=question_line + b"\n" + question_line + b"\n",
        references_jsonl=objects.references_jsonl,
    )
    assert "record.duplicate_case_id" in _codes(load_dataset_bundle(duplicate_case))

    missing_reference = DatasetBundleBytes(
        manifest_json=objects.manifest_json,
        questions_jsonl=objects.questions_jsonl,
        references_jsonl=b"",
    )
    missing_result = load_dataset_bundle(missing_reference)
    assert {"jsonl.empty", "bundle.missing_member"}.issubset(_codes(missing_result))


def test_deep_reference_leak_in_questions_is_rejected_before_projection() -> None:
    _, _, objects = _frozen_bundle()
    rows = _jsonl_values(objects.questions_jsonl)
    rows[0]["question_input"]["scope"]["sources"][0]["metadata"] = {
        "nested": {"reference_answer": "secret"}
    }

    result = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, _jsonl_bytes(rows), objects.references_jsonl)
    )

    assert result.report.valid is False
    assert "question.reference_leak" in _codes(result)


def test_manifest_membership_digest_order_and_group_split_mismatches_are_rejected() -> None:
    _, _, objects = _frozen_bundle(two_cases=True)

    reordered = _jsonl_values(objects.questions_jsonl)
    reordered.reverse()
    order_result = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, _jsonl_bytes(reordered), objects.references_jsonl)
    )
    assert "bundle.question_order_mismatch" in _codes(order_result)

    digest_rows = _jsonl_values(objects.questions_jsonl)
    digest_rows[0]["question_digest"] = "0" * 64
    assert "question.invalid" in _codes(
        load_dataset_bundle(DatasetBundleBytes(objects.manifest_json, _jsonl_bytes(digest_rows), objects.references_jsonl))
    )

    split_rows = _jsonl_values(objects.questions_jsonl)
    split_rows[0]["paper_group_id"] = "other-family"
    split_result = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, _jsonl_bytes(split_rows), objects.references_jsonl)
    )
    assert "bundle.group_split_mismatch" in _codes(split_result)

    reference_rows = _jsonl_values(objects.references_jsonl)
    reference_rows.pop()
    missing_result = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, objects.questions_jsonl, _jsonl_bytes(reference_rows))
    )
    assert "bundle.missing_member" in _codes(missing_result)


def test_logical_merge_runs_full_evaluation_case_validation_not_dictionary_join() -> None:
    _, _, objects = _frozen_bundle()
    question_rows = _jsonl_values(objects.questions_jsonl)
    question_rows[0]["exposure_history"] = {
        "status": "unknown",
        "events": [],
        "assessed_at": "2026-01-02T03:04:00Z",
        "assessed_by": "reviewer",
        "assessment_evidence_reference": None,
    }

    result = load_dataset_bundle(
        DatasetBundleBytes(objects.manifest_json, _jsonl_bytes(question_rows), objects.references_jsonl)
    )

    assert result.report.valid is False
    assert "bundle.case_invalid" in _codes(result)


def test_draft_validation_is_valid_but_not_execution_ready_and_frozen_mode_rejects_it() -> None:
    case = _case()
    content = _manifest_content(case)
    content["lifecycle_state"] = "reviewed"
    content["frozen_at"] = None
    manifest = DatasetManifest.create(**content)
    objects = serialize_dataset_bundle(manifest, (case,))

    draft = load_dataset_bundle(objects, mode=DatasetValidationMode.DRAFT)
    assert draft.report.valid is True
    assert draft.report.ready_for_execution_data is False
    assert {issue.code for issue in draft.report.readiness_requirements} == {
        "readiness.dataset_not_frozen",
        "readiness.source_corpus_validation_required",
    }

    strict = load_dataset_bundle(objects, mode=DatasetValidationMode.FROZEN)
    assert strict.report.valid is False
    assert "requirement.dataset_not_frozen" in _codes(strict)


def test_frozen_same_version_content_replacement_is_rejected_but_same_bundle_is_allowed() -> None:
    manifest, cases, objects = _frozen_bundle()
    existing = load_dataset_bundle(objects, mode=DatasetValidationMode.FROZEN).require_bundle()
    changed_content = manifest.model_dump(mode="python", exclude={"dataset_digest"})
    changed_content["annotation_version"] = "fixture-annotation/v2-tampered-in-place"
    changed_manifest = DatasetManifest.create(**changed_content)
    candidate_objects = serialize_dataset_bundle(changed_manifest, cases)
    candidate = load_dataset_bundle(candidate_objects, mode=DatasetValidationMode.FROZEN).require_bundle()

    ensure_frozen_revision_is_not_replaced(existing, existing)
    with pytest.raises(FrozenDatasetRevisionConflict, match="cannot be replaced in place"):
        ensure_frozen_revision_is_not_replaced(existing, candidate)
