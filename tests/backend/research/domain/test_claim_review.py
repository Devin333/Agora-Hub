import pytest

from backend.research.domain.claim_review import (
    AssertionKind,
    CheckExecutionStatus,
    CheckObservation,
    ClaimConclusionStatus,
    ClaimEligibilityDecision,
    ClaimRevision,
    EvidenceRequirement,
    RequirementStatus,
    ResearchProgressObservation,
    ResearchProgressReasonCode,
    ResearchProgressScope,
    ResolutionRecord,
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



def test_source_locator_requires_exact_checksum_and_unavailable_reason() -> None:
    from backend.research.domain.claim_review import (
        EvidenceAvailability,
        EvidenceSourceLocator,
    )

    with pytest.raises(ValueError, match="sha256"):
        EvidenceSourceLocator(
            source_id="source-1",
            source_revision="revision-1",
            source_checksum="caller-value",
            locator_type="research_table_cells",
            locator="table:1",
        )
    with pytest.raises(ValueError, match="unavailable_reason"):
        EvidenceSourceLocator(
            source_id="source-1",
            source_revision="revision-1",
            source_checksum="sha256:" + "0" * 64,
            locator_type="research_table_cells",
            locator="table:1",
            availability=EvidenceAvailability.UNAVAILABLE,
        )


def test_numeric_comparison_contract_binds_difference_and_conditions() -> None:
    from backend.research.domain.claim_review import (
        NumericComparator,
        NumericComparisonClaim,
    )

    with pytest.raises(ValueError, match="difference_value"):
        NumericComparisonClaim(
            metric_id="accuracy",
            subject_label="u-net",
            comparator_label="baseline",
            subject_value="92",
            comparator_value="83",
            unit="percent",
            comparator=NumericComparator.GREATER_THAN,
            difference_value="8",
            difference_unit="percentage_point",
            conditions={"dataset": "segmentation-v1", "split": "test"},
        )


def _progress_observation(
    *,
    scope: ResearchProgressScope = ResearchProgressScope.CLAIM,
    resolver_status: ClaimConclusionStatus | None = (
        ClaimConclusionStatus.INSUFFICIENT_EVIDENCE
    ),
) -> ResearchProgressObservation:
    return ResearchProgressObservation(
        run_id="run-1",
        paper_id="paper-1",
        observation_scope=scope,
        claim_id="claim-1" if scope is ResearchProgressScope.CLAIM else None,
        reason_code=ResearchProgressReasonCode.INSUFFICIENT_VERIFIED_PROGRESS,
        resolver_status=resolver_status,
        metrics={
            "eligible_claims": 0,
            "required_claims": 1,
            "verified_progress_ratio": 0.0,
        },
        evidence_context_refs={
            "evidence_pack": "sha256:" + "3" * 64,
        },
    )


def test_progress_observation_is_immutable_and_checksum_replay_safe() -> None:
    metrics = {
        "eligible_claims": 0,
        "required_claims": 1,
        "verified_progress_ratio": 0.0,
    }
    context_refs = {"evidence_pack": "sha256:" + "3" * 64}
    observation = ResearchProgressObservation(
        run_id="run-1",
        paper_id="paper-1",
        observation_scope=ResearchProgressScope.CLAIM,
        claim_id="claim-1",
        reason_code=ResearchProgressReasonCode.INSUFFICIENT_VERIFIED_PROGRESS,
        resolver_status=ClaimConclusionStatus.INSUFFICIENT_EVIDENCE,
        metrics=metrics,
        evidence_context_refs=context_refs,
    )
    metrics["eligible_claims"] = 1
    context_refs["evidence_pack"] = "sha256:" + "4" * 64

    assert observation.metrics["eligible_claims"] == 0
    assert observation.evidence_context_refs["evidence_pack"] == (
        "sha256:" + "3" * 64
    )
    with pytest.raises(TypeError):
        observation.metrics["eligible_claims"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        observation.evidence_context_refs["document"] = (  # type: ignore[index]
            "sha256:" + "5" * 64
        )
    assert ResearchProgressObservation.from_dict(observation.to_dict()) == observation

    tampered = observation.to_dict()
    tampered["metrics"]["eligible_claims"] = 1
    tampered["metrics"]["verified_progress_ratio"] = 1.0
    with pytest.raises(ValueError, match="checksum"):
        ResearchProgressObservation.from_dict(tampered)


@pytest.mark.parametrize(
    ("field", "value", "error_type"),
    (
        ("eligible_claims", True, TypeError),
        ("eligible_claims", -1, ValueError),
        ("required_claims", 0, ValueError),
        ("required_claims", 1.0, TypeError),
        ("verified_progress_ratio", True, TypeError),
        ("verified_progress_ratio", float("nan"), ValueError),
        ("verified_progress_ratio", float("inf"), ValueError),
        ("verified_progress_ratio", "0", TypeError),
    ),
)
def test_progress_metrics_reject_nonfinite_or_invalid_values(
    field: str,
    value: object,
    error_type: type[Exception],
) -> None:
    metrics = {
        "eligible_claims": 0,
        "required_claims": 1,
        "verified_progress_ratio": 0.0,
    }
    metrics[field] = value

    with pytest.raises(error_type):
        ResearchProgressObservation(
            run_id="run-1",
            paper_id="paper-1",
            observation_scope=ResearchProgressScope.CLAIM,
            claim_id="claim-1",
            reason_code=(
                ResearchProgressReasonCode.INSUFFICIENT_VERIFIED_PROGRESS
            ),
            resolver_status=ClaimConclusionStatus.INSUFFICIENT_EVIDENCE,
            metrics=metrics,
            evidence_context_refs={
                "evidence_pack": "sha256:" + "3" * 64,
            },
        )


def test_run_scoped_progress_has_no_fabricated_claim_or_resolver_status() -> None:
    observation = ResearchProgressObservation.insufficient_verified_progress(
        run_id="run-1",
        paper_id="paper-1",
        evidence_context_refs={
            "document": "sha256:" + "4" * 64,
        },
    )

    assert observation.observation_scope is ResearchProgressScope.RUN
    assert observation.claim_id is None
    assert observation.resolver_status is None

    payload = observation.to_dict()
    payload["publish"] = True
    with pytest.raises(ValueError, match="unknown contract fields"):
        ResearchProgressObservation.from_dict(payload)
    with pytest.raises(ValueError, match="observation-only"):
        ResearchProgressObservation(
            run_id="run-1",
            paper_id="paper-1",
            observation_scope=ResearchProgressScope.RUN,
            claim_id=None,
            reason_code=(
                ResearchProgressReasonCode.INSUFFICIENT_VERIFIED_PROGRESS
            ),
            resolver_status=None,
            metrics={
                "eligible_claims": 0,
                "required_claims": 1,
                "verified_progress_ratio": 0.0,
            },
            evidence_context_refs={
                "document": "sha256:" + "4" * 64,
            },
            observation_only=False,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_id", "run-other"),
        ("paper_id", "paper-other"),
        ("claim_id", "claim-other"),
        ("resolver_status", "verification_unavailable"),
        ("evidence_context_refs", {"document": "sha256:" + "4" * 64}),
    ),
)
def test_progress_context_tampering_is_rejected_by_checksum(
    field: str,
    value: object,
) -> None:
    payload = _progress_observation().to_dict()
    payload[field] = value

    with pytest.raises(ValueError, match="checksum"):
        ResearchProgressObservation.from_dict(payload)


@pytest.mark.parametrize("observation_only", (False, 0, 1, "true", None))
def test_progress_observation_only_requires_literal_true(
    observation_only: object,
) -> None:
    payload = _progress_observation().to_dict()
    payload["observation_only"] = observation_only

    with pytest.raises(ValueError, match="observation-only"):
        ResearchProgressObservation.from_dict(payload)


def test_progress_persisted_contract_requires_checksum_and_rejects_text_metrics() -> None:
    payload = _progress_observation().to_dict()
    payload.pop("checksum")
    with pytest.raises(ValueError, match="requires checksum"):
        ResearchProgressObservation.from_dict(payload)

    payload = _progress_observation().to_dict()
    payload["metrics"]["resolver_status"] = "insufficient_evidence"
    with pytest.raises(ValueError, match="stable schema"):
        ResearchProgressObservation.from_dict(payload)

    payload = _progress_observation().to_dict()
    payload["evidence_context_refs"]["document"] = "not-a-checksum"
    with pytest.raises(ValueError, match="sha256"):
        ResearchProgressObservation.from_dict(payload)


@pytest.mark.parametrize(
    "checksum",
    (
        "sha256:" + "A" * 64,
        "sha256:+" + "1" * 63,
        " sha256:" + "1" * 64,
        "sha256:" + "g" * 64,
    ),
)
def test_progress_evidence_context_requires_canonical_sha256(checksum: str) -> None:
    payload = _progress_observation().to_dict()
    payload["evidence_context_refs"]["document"] = checksum

    with pytest.raises(ValueError, match="canonical sha256"):
        ResearchProgressObservation.from_dict(payload)


@pytest.mark.parametrize(
    ("scope", "claim_id", "status"),
    (
        ("claim", None, "insufficient_evidence"),
        ("claim", "claim-1", None),
        ("run", "claim-1", None),
        ("run", None, "insufficient_evidence"),
    ),
)
def test_progress_scope_cannot_invent_or_omit_claim_resolution_identity(
    scope: str,
    claim_id: str | None,
    status: str | None,
) -> None:
    payload = _progress_observation().to_dict()
    payload.update(
        observation_scope=scope,
        claim_id=claim_id,
        resolver_status=status,
    )

    with pytest.raises(ValueError, match="scoped progress"):
        ResearchProgressObservation.from_dict(payload)


def _eligibility_decision(
    status: ClaimConclusionStatus,
    *,
    eligible: bool | None = None,
) -> ClaimEligibilityDecision:
    resolution = ResolutionRecord(
        binding_checksum="binding-1",
        claim_id="claim-1",
        claim_revision=1,
        status=status,
        check_ids=("check-1",),
        observation_ids=("observation-1",),
        rule_version="rule-1",
    )
    return ClaimEligibilityDecision(
        binding_checksum="binding-1",
        claim_id="claim-1",
        claim_revision=1,
        claim_contract_checksum="contract-1",
        resolution=resolution,
        resolver_ref="ResearchNumericComparisonResolver@1",
        gate_ref="ResearchClaimEvidenceEligibilityGate@1",
        source_locator_checksums=("sha256:" + "1" * 64,),
        typed_observation_checksums=("sha256:" + "2" * 64,),
        eligible=(
            status is ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE
            if eligible is None
            else eligible
        ),
    )


def test_claim_qualification_ref_exists_only_for_supported_eligible_decision() -> None:
    supported = _eligibility_decision(
        ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE
    )

    assert supported.eligible is True
    assert supported.qualification_ref == f"claim-qualification:{supported.checksum}"
    assert ClaimEligibilityDecision.from_dict(supported.to_dict()) == supported


@pytest.mark.parametrize(
    "status",
    (
        ClaimConclusionStatus.CONTRADICTED,
        ClaimConclusionStatus.INSUFFICIENT_EVIDENCE,
        ClaimConclusionStatus.VERIFICATION_UNAVAILABLE,
    ),
)
def test_ineligible_decision_has_no_qualification_ref(
    status: ClaimConclusionStatus,
) -> None:
    decision = _eligibility_decision(status)

    assert decision.eligible is False
    assert decision.qualification_ref is None
    assert decision.checksum.startswith("sha256:")
    assert decision.resolution.checksum.startswith("sha256:")
    assert decision.to_dict()["qualification_ref"] is None
    assert ClaimEligibilityDecision.from_dict(decision.to_dict()) == decision


@pytest.mark.parametrize(
    ("status", "eligible"),
    (
        (ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE, False),
        (ClaimConclusionStatus.CONTRADICTED, True),
        (ClaimConclusionStatus.INSUFFICIENT_EVIDENCE, True),
        (ClaimConclusionStatus.VERIFICATION_UNAVAILABLE, True),
    ),
)
def test_eligibility_cannot_be_forged_against_resolution_status(
    status: ClaimConclusionStatus,
    eligible: bool,
) -> None:
    with pytest.raises(ValueError, match="eligible must be derived"):
        _eligibility_decision(status, eligible=eligible)


def test_noneligible_persisted_decision_cannot_forge_qualification_ref() -> None:
    decision = _eligibility_decision(ClaimConclusionStatus.INSUFFICIENT_EVIDENCE)
    payload = decision.to_dict()
    payload["qualification_ref"] = "claim-qualification:sha256:" + "f" * 64

    with pytest.raises(ValueError, match="qualification_ref"):
        ClaimEligibilityDecision.from_dict(payload)


def test_supported_persisted_qualification_ref_remains_checksum_bound() -> None:
    decision = _eligibility_decision(
        ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE
    )
    payload = decision.to_dict()
    payload["qualification_ref"] = "claim-qualification:sha256:" + "f" * 64

    with pytest.raises(ValueError, match="qualification_ref"):
        ClaimEligibilityDecision.from_dict(payload)
