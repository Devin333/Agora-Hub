from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.research.rag.evaluation.quality_foundation_cases import (
    QUALITY_FOUNDATION_CASE_SET_VERSION,
    _decision_case,
    evaluate_quality_foundation_cases,
)


def test_quality_foundation_fixed_cases_cover_input_source_and_claim_outcomes() -> None:
    cases = evaluate_quality_foundation_cases()
    by_id = {case["case_id"]: case for case in cases}

    assert {
        "input-final-projection-preserves-contract",
        "input-final-request-constraint-loss-rejected",
        "input-final-request-objective-loss-rejected",
        "input-final-request-objective-tamper-rejected",
        "input-final-request-source-loss-rejected",
        "input-final-request-source-revision-change-rejected",
        "claim-supported",
        "claim-contradicted",
        "claim-insufficient",
        "claim-unavailable",
        "claim-duplicate-observation-deduplicated",
        "claim-trusted-source-gate-positive",
        "claim-missing-v1-result-is-insufficient",
        "claim-candidate-control-rejected",
        "claim-forged-source-binding-rejected",
        "claim-source-lineage-drift-rejected",
        "claim-source-revision-change-rejected",
        "claim-evidence-checksum-change-rejected",
        "claim-locator-wrong-cells-rejected",
        "claim-tampered-report-text-rejected",
        "claim-cross-condition-table-rejected",
        "claim-source-table-condition-conflict-rejected",
        "worker-direct-routing-control-rejected",
        "worker-nested-routing-control-rejected",
        "worker-direct-quality-control-rejected",
        "worker-nested-quality-control-rejected",
        "worker-direct-memory-control-rejected",
        "worker-nested-memory-control-rejected",
        "worker-direct-tool-control-rejected",
        "worker-nested-tool-control-rejected",
        "worker-direct-publication-control-rejected",
        "worker-nested-publication-control-rejected",
        "worker-direct-qualification-control-rejected",
        "worker-nested-qualification-control-rejected",
    }.issubset(by_id)
    assert QUALITY_FOUNDATION_CASE_SET_VERSION == "research-quality-foundation-cases-v2"
    assert all(case["case_set_version"] == QUALITY_FOUNDATION_CASE_SET_VERSION for case in cases)
    assert all(case["passed"] is True for case in cases), cases
    assert by_id["claim-supported"]["diagnostics"]["qualification_ref"].startswith(
        "claim-qualification:sha256:"
    )
    assert by_id["claim-insufficient"]["diagnostics"]["qualification_ref"] is None
    assert by_id["claim-unavailable"]["diagnostics"]["qualification_ref"] is None
    expected_source_reasons = {
        "claim-source-revision-change-rejected": "claim_evidence_revision_mismatch",
        "claim-evidence-checksum-change-rejected": "claim_evidence_revision_mismatch",
        "claim-locator-wrong-cells-rejected": "claim_source_locator_mismatch",
        "claim-source-table-condition-conflict-rejected": "source_table_condition_conflict",
    }
    for case_id, reason_code in expected_source_reasons.items():
        case = by_id[case_id]
        assert case["actual_reason"] == reason_code
        assert case["diagnostics"]["reason_code"] == reason_code
    assert by_id["claim-source-revision-change-rejected"]["diagnostics"][
        "source_locator_checksums"
    ]
    assert by_id["claim-evidence-checksum-change-rejected"]["diagnostics"][
        "source_locator_checksums"
    ]
    assert by_id["claim-locator-wrong-cells-rejected"]["diagnostics"][
        "source_locator_checksums"
    ]
    assert len(by_id) == len(cases)
    assert by_id["worker-direct-routing-control-rejected"]["diagnostics"][
        "forbidden_paths"
    ] == ["output.route"]
    assert by_id["worker-nested-routing-control-rejected"]["diagnostics"][
        "forbidden_paths"
    ] == ["output.metadata.route"]
    assert by_id["worker-direct-qualification-control-rejected"][
        "actual_reason"
    ] == "candidate_control_field_rejected"
    assert by_id["worker-nested-qualification-control-rejected"][
        "actual_reason"
    ] == "claim_projection_mismatch"
    for case in cases:
        diagnostics = case["diagnostics"]
        assert diagnostics["expected"] == case["expected_reason"]
        assert diagnostics["actual"] == case["actual_reason"]
        assert diagnostics["reason_code"] == case["actual_reason"]
        assert isinstance(diagnostics["source_locator_checksums"], list)
        assert "qualification_ref" in diagnostics


@pytest.mark.parametrize(
    ("status", "eligible", "qualification_ref"),
    (
        ("insufficient_evidence", False, "claim-qualification:forged"),
        ("contradicted", False, "claim-qualification:forged"),
        ("verification_unavailable", False, "claim-qualification:forged"),
        ("supported_within_scope", True, None),
    ),
)
def test_fixed_decision_case_rejects_qualification_eligibility_mismatch(
    status: str,
    eligible: bool,
    qualification_ref: str | None,
) -> None:
    decision = SimpleNamespace(
        resolution=SimpleNamespace(status=SimpleNamespace(value=status)),
        eligible=eligible,
        qualification_ref=qualification_ref,
        source_locator_checksums=(),
        reason_codes=(),
    )

    case = _decision_case("qualification-invariant", decision, status, eligible)

    assert case["passed"] is False
    assert case["diagnostics"]["qualification_ref"] == qualification_ref
