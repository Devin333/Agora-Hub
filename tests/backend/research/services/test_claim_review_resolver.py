from backend.research.domain.claim_review import (
    AssertionKind,
    CheckExecutionStatus,
    CheckObservation,
    CheckResult,
    ClaimConclusionStatus,
    EvidenceRequirement,
    RequirementStatus,
    ReviewConclusion,
    ReviewObservation,
    ReviewerType,
)
from backend.research.services.claim_review_resolver import ClaimReviewResolver
from tests.backend.research.domain.test_claim_review import make_binding


def check(binding, result=CheckResult.PASS):
    return CheckObservation(
        check_id="citation", binding_checksum=binding.checksum, checker_id="citation-verifier",
        checker_version="1", execution_status=CheckExecutionStatus.COMPLETED, result=result,
    )


def human(binding, conclusion=ReviewConclusion.SUPPORTS, **kwargs):
    return ReviewObservation(
        observation_id=kwargs.pop("observation_id", "review-1"), binding_checksum=binding.checksum,
        reviewer_id=kwargs.pop("reviewer_id", "human-1"), reviewer_type=ReviewerType.HUMAN,
        conclusion=conclusion, rationale="specific source-bound rationale",
        evidence_refs=kwargs.pop("evidence_refs", ("evidence-1",)), **kwargs,
    )


def test_missing_required_review_stays_pending() -> None:
    binding = make_binding()
    result = ClaimReviewResolver().resolve(
        binding,
        checks=[check(binding)],
        requirements=[EvidenceRequirement("metric", "metric", RequirementStatus.PRESENT, evidence_refs=("evidence-1",))],
    )
    assert result.status is ClaimConclusionStatus.PENDING
    assert "independent_human_review_missing" in result.reasons


def test_experimental_reported_result_cannot_be_supported_by_citation_pass_alone() -> None:
    binding = make_binding()
    result = ClaimReviewResolver().resolve(
        binding,
        checks=[check(binding)],
        requirements=[EvidenceRequirement("metric", "metric", RequirementStatus.PRESENT, evidence_refs=("evidence-1",))],
    )
    assert result.status is ClaimConclusionStatus.PENDING
    assert "independent_human_review_missing" in result.reasons


def test_non_experimental_reported_result_can_use_completed_requirements_and_checks() -> None:
    from backend.research.domain.claim_review import ClaimRevision

    claim = ClaimRevision(
        claim_id="claim-observed", revision=1, text="The paper reports a result.",
        assertion_kind=AssertionKind.REPORTED_RESULT, claim_type="survey",
        scope={"paper_id": "paper-1"}, objective_revision="objective-1",
    )
    binding = make_binding(claim)
    result = ClaimReviewResolver().resolve(
        binding,
        checks=[check(binding)],
        requirements=[EvidenceRequirement("metric", "metric", RequirementStatus.PRESENT, evidence_refs=("evidence-1",))],
    )
    assert result.status is ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE


def test_supported_requires_completed_check_and_independent_human_review() -> None:
    binding = make_binding(
        binding_claim := binding_claim_for_interpretation()
    )
    result = ClaimReviewResolver().resolve(
        binding,
        checks=[check(binding)],
        requirements=[EvidenceRequirement("metric", "metric", RequirementStatus.PRESENT, evidence_refs=("evidence-1",))],
        observations=[human(binding)],
    )
    assert result.status is ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE


def binding_claim_for_interpretation():
    from backend.research.domain.claim_review import ClaimRevision
    return ClaimRevision(
        claim_id="claim-interpretation", revision=1, text="M is better under D.",
        assertion_kind=AssertionKind.EVIDENCE_INTERPRETATION, claim_type="experiment",
        scope={"paper_id": "paper-1"},
        objective_revision="objective-1",
    )


def test_unavailable_check_is_not_contradiction() -> None:
    binding = make_binding()
    unavailable = CheckObservation(
        check_id="citation", binding_checksum=binding.checksum, checker_id="checker", checker_version="1",
        execution_status=CheckExecutionStatus.UNAVAILABLE, reason_codes=("reader_timeout",),
    )
    result = ClaimReviewResolver().resolve(binding, checks=[unavailable], observations=[human(binding)])
    assert result.status is ClaimConclusionStatus.VERIFICATION_UNAVAILABLE


def test_conflicting_reviews_remain_disputed_without_vote_count() -> None:
    binding = make_binding()
    result = ClaimReviewResolver().resolve(
        binding,
        checks=[check(binding)],
        observations=[
            human(binding, observation_id="support", conclusion=ReviewConclusion.SUPPORTS),
            human(binding, observation_id="refute", reviewer_id="human-2", conclusion=ReviewConclusion.REFUTES),
        ],
    )
    assert result.status is ClaimConclusionStatus.DISPUTED
    assert "support_and_refutation_for_same_binding" in result.unresolved_items
