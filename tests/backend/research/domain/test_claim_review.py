import pytest

from backend.research.domain.claim_review import (
    AssertionKind,
    CheckExecutionStatus,
    CheckObservation,
    ClaimRevision,
    EvidenceRequirement,
    RequirementStatus,
    ReviewBinding,
    ReviewConclusion,
    ReviewObservation,
    ReviewerType,
)


def make_claim() -> ClaimRevision:
    return ClaimRevision(
        claim_id="claim-1",
        revision=1,
        text="M reports 85 percent accuracy on dataset D.",
        assertion_kind=AssertionKind.REPORTED_RESULT,
        claim_type="experiment",
        scope={"paper_id": "paper-1"},
        objective_revision="objective-1",
    )


def make_binding(claim: ClaimRevision | None = None) -> ReviewBinding:
    return ReviewBinding(
        tenant_id="tenant-1",
        actor_scope={"tenant_id": "tenant-1", "user_id": "reviewer"},
        run_id="run-1",
        paper_id="paper-1",
        paper_snapshot_checksum="paper-snapshot-1",
        evidence_revision="evidence-1",
        evidence_checksum="evidence-checksum-1",
        claim=claim or make_claim(),
        rule_version="rule-1",
        strategy_version="strategy-1",
    )


def test_claim_revision_checksum_is_frozen_and_changes_with_text() -> None:
    first = make_claim()
    second = ClaimRevision(
        claim_id="claim-1", revision=2, text="A narrower result.",
        assertion_kind=AssertionKind.REPORTED_RESULT, claim_type="experiment",
        scope={"paper_id": "paper-1"},
        objective_revision="objective-1", predecessor_revision=1,
        change_reason="narrowed scope",
    )
    assert first.text_checksum.startswith("sha256:")
    assert first.checksum != second.checksum
    with pytest.raises((AttributeError, TypeError)):
        first.text = "mutated"  # type: ignore[misc]


def test_binding_rejects_cross_tenant_scope() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        ReviewBinding(
            tenant_id="tenant-1", actor_scope={"tenant_id": "tenant-2"}, run_id="run-1",
            paper_id="paper-1", paper_snapshot_checksum="paper-1", evidence_revision="e-1",
            evidence_checksum="e-1", claim=make_claim(), rule_version="r-1", strategy_version="s-1",
        )


def test_check_execution_status_cannot_carry_semantic_result_when_unavailable() -> None:
    with pytest.raises(ValueError):
        CheckObservation(
            check_id="citation", binding_checksum="sha256:x", checker_id="checker",
            checker_version="1", execution_status=CheckExecutionStatus.UNAVAILABLE,
            result="pass",
        )


def test_present_requirement_requires_evidence() -> None:
    with pytest.raises(ValueError):
        EvidenceRequirement("metric", "metric", RequirementStatus.PRESENT)


def test_not_applicable_requirement_requires_rationale_and_has_no_refs() -> None:
    requirement = EvidenceRequirement(
        "appendix", "appendix", RequirementStatus.NOT_APPLICABLE,
        applicable=False, rationale="paper has no appendix section",
    )
    assert requirement.to_dict()["rationale"] == "paper has no appendix section"
    with pytest.raises(ValueError):
        EvidenceRequirement(
            "appendix", "appendix", RequirementStatus.NOT_APPLICABLE,
            applicable=False, rationale="not relevant", evidence_refs=("evidence-1",),
        )
    with pytest.raises(ValueError, match="missing requirement"):
        EvidenceRequirement(
            "metric", "metric", RequirementStatus.MISSING,
            evidence_refs=("evidence-1",),
        )


def test_contract_to_dict_roundtrip_rehydrates_and_verifies_checksum() -> None:
    claim = make_claim()
    assert ClaimRevision.from_dict(claim.to_dict()) == claim
    binding = make_binding(claim)
    assert ReviewBinding.from_dict(binding.to_dict()) == binding

    tampered = claim.to_dict()
    tampered["text_checksum"] = "sha256:tampered"
    with pytest.raises(ValueError, match="text_checksum"):
        ClaimRevision.from_dict(tampered)


def test_claim_revision_change_fields_are_revision_specific() -> None:
    with pytest.raises(ValueError, match="change_reason"):
        ClaimRevision(
            claim_id="claim-1", revision=1, text="same", assertion_kind=AssertionKind.REPORTED_RESULT,
            claim_type="experiment", scope={"paper_id": "paper-1"}, objective_revision="objective-1",
            change_reason="introduced",
        )
    with pytest.raises(ValueError, match="previous revision"):
        ClaimRevision(
            claim_id="claim-1", revision=3, text="changed", assertion_kind=AssertionKind.REPORTED_RESULT,
            claim_type="experiment", scope={"paper_id": "paper-1"}, objective_revision="objective-1",
            predecessor_revision=1, change_reason="changed",
        )


def test_review_observation_requires_specific_grounds_for_refutation() -> None:
    with pytest.raises(ValueError):
        ReviewObservation(
            observation_id="o-1", binding_checksum="sha256:x", reviewer_id="human-1",
            reviewer_type=ReviewerType.HUMAN, conclusion=ReviewConclusion.REFUTES,
            rationale="no",
        )
