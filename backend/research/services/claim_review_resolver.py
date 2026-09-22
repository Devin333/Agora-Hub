from __future__ import annotations

from collections.abc import Iterable

from backend.research.domain.claim_review import (
    CheckExecutionStatus,
    CheckObservation,
    CheckResult,
    ClaimConclusionStatus,
    DisputeCase,
    EvidenceRequirement,
    RequirementStatus,
    ResolutionRecord,
    ReviewBinding,
    ReviewConclusion,
    ReviewObservation,
    ReviewerType,
)


class ClaimReviewResolver:
    """Replay-only resolver for a frozen claim review contract."""

    def resolve(
        self,
        binding: ReviewBinding,
        *,
        requirements: Iterable[EvidenceRequirement] = (),
        checks: Iterable[CheckObservation] = (),
        observations: Iterable[ReviewObservation] = (),
        disputes: Iterable[DisputeCase] = (),
    ) -> ResolutionRecord:
        requirements = tuple(requirements)
        checks = tuple(checks)
        observations = tuple(observations)
        disputes = tuple(disputes)
        for check in checks:
            self._require_binding(check.binding_checksum, binding.checksum, "check")
        for observation in observations:
            self._require_binding(observation.binding_checksum, binding.checksum, "observation")
        for dispute in disputes:
            self._require_binding(dispute.binding_checksum, binding.checksum, "dispute")

        reasons: list[str] = []
        unresolved: list[str] = []
        status = ClaimConclusionStatus.PENDING
        if not checks:
            reasons.append("no_check_observations")
        elif any(c.execution_status is not CheckExecutionStatus.COMPLETED for c in checks):
            status = ClaimConclusionStatus.VERIFICATION_UNAVAILABLE
            reasons.append("check_execution_incomplete")
        elif any(c.result is CheckResult.FAIL for c in checks):
            status = ClaimConclusionStatus.CONTRADICTED
            reasons.append("required_check_failed")

        applicable = [r for r in requirements if r.applicable and r.status is not RequirementStatus.NOT_APPLICABLE]
        missing = [r.requirement_id for r in applicable if r.status is RequirementStatus.MISSING]
        if missing:
            status = ClaimConclusionStatus.INSUFFICIENT_EVIDENCE
            unresolved.extend(f"missing_requirement:{item}" for item in missing)
            reasons.append("required_evidence_missing")

        active_reviews = [o for o in observations if o.substantive]
        support = [o for o in active_reviews if o.conclusion is ReviewConclusion.SUPPORTS]
        refute = [o for o in active_reviews if o.conclusion is ReviewConclusion.REFUTES]
        insufficient = [o for o in active_reviews if o.conclusion is ReviewConclusion.INSUFFICIENT_EVIDENCE]
        if binding.requires_independent_human_review:
            humans = {o.reviewer_id for o in active_reviews if o.reviewer_type is ReviewerType.HUMAN}
            if not humans:
                status = ClaimConclusionStatus.PENDING
                reasons.append("required_review_missing")
                reasons.append("independent_human_review_missing")
        if insufficient:
            status = ClaimConclusionStatus.INSUFFICIENT_EVIDENCE
            reasons.append("review_reports_insufficient_evidence")
        if support and refute:
            status = ClaimConclusionStatus.DISPUTED
            reasons.append("unresolved_review_conflict")
            unresolved.append("support_and_refutation_for_same_binding")
        elif refute and status is ClaimConclusionStatus.PENDING:
            status = ClaimConclusionStatus.CONTRADICTED
            reasons.append("review_identifies_counterevidence")
        if any(d.lifecycle.value != "closed" for d in disputes):
            status = ClaimConclusionStatus.DISPUTED
            reasons.append("open_dispute_case")
            unresolved.append("open_dispute_case")

        # A failed or unavailable check always wins over a review's asserted meaning.
        if checks and any(c.execution_status is not CheckExecutionStatus.COMPLETED for c in checks):
            status = ClaimConclusionStatus.VERIFICATION_UNAVAILABLE
        elif checks and any(c.result is CheckResult.FAIL for c in checks) and not (support and refute):
            status = ClaimConclusionStatus.CONTRADICTED

        if status is ClaimConclusionStatus.PENDING and checks and all(c.result is CheckResult.PASS for c in checks) and not missing and not insufficient and not (support and refute):
            if binding.requires_independent_human_review and not any(o.reviewer_type is ReviewerType.HUMAN for o in active_reviews):
                status = ClaimConclusionStatus.PENDING
            else:
                status = ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE
                reasons.append("frozen_contract_requirements_satisfied")
        return ResolutionRecord(
            binding_checksum=binding.checksum,
            claim_id=binding.claim.claim_id,
            claim_revision=binding.claim.revision,
            status=status,
            check_ids=tuple(c.check_id for c in checks),
            observation_ids=tuple(o.observation_id for o in observations),
            rule_version=binding.rule_version,
            reasons=tuple(dict.fromkeys(reasons)),
            unresolved_items=tuple(dict.fromkeys(unresolved)),
        )

    @staticmethod
    def _require_binding(actual: str, expected: str, kind: str) -> None:
        if actual != expected:
            raise ValueError(f"{kind} is bound to a different review contract")


__all__ = ["ClaimReviewResolver"]
