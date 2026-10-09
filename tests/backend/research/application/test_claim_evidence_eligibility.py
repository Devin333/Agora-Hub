from __future__ import annotations

from dataclasses import replace

import pytest

from framework.events.canonical import checksum_for

from backend.research.application.claim_evidence_eligibility import (
    ClaimEvidenceEligibilityService,
    numeric_comparison_claim_id,
    render_numeric_comparison_claim_text,
)
from backend.research.domain.claim_review import (
    AssertionKind,
    ClaimConclusionStatus,
    ClaimRevision,
    EvidenceAvailability,
    EvidenceSourceLocator,
    INSUFFICIENT_VERIFIED_PROGRESS,
    NumericComparator,
    NumericComparisonClaim,
    NumericComparisonEligibilityBundle,
    NumericComparisonObservation,
    ObservationProducer,
    ReviewBinding,
    ResearchProgressObservation,
    ResearchProgressScope,
    V1_NUMERIC_COMPARISON_CLAIM_TYPE,
)


def _bundle(
    *,
    subject_value: str = "92",
    observation_conditions: dict[str, str] | None = None,
    availability: EvidenceAvailability = EvidenceAvailability.AVAILABLE,
    duplicate: bool = False,
) -> NumericComparisonEligibilityBundle:
    conditions = {
        "dataset": "segmentation-v1",
        "split": "test",
        "table_id": "table-results",
        "label_column": "Method",
        "value_column": "Accuracy (%)",
        "source_scale": "percent",
    }
    contract = NumericComparisonClaim(
        metric_id="accuracy",
        subject_label="u-net",
        comparator_label="baseline",
        subject_value="92",
        comparator_value="83",
        unit="percent",
        comparator=NumericComparator.GREATER_THAN,
        difference_value="9",
        difference_unit="percentage_point",
        conditions=conditions,
    )
    claim = ClaimRevision(
        claim_id=numeric_comparison_claim_id(
            paper_id="paper-1",
            table_id="table-results",
            contract=contract,
        ),
        revision=1,
        text=render_numeric_comparison_claim_text(contract),
        assertion_kind=AssertionKind.REPORTED_RESULT,
        claim_type=V1_NUMERIC_COMPARISON_CLAIM_TYPE,
        scope={"paper_id": "paper-1"},
        objective_revision="objective-1",
    )
    evidence_checksum = checksum_for({"trusted": "evidence"})
    locator = EvidenceSourceLocator(
        source_id="source-1",
        source_revision="source-revision-1",
        source_checksum=evidence_checksum,
        locator_type="research_table_cells",
        locator="table:table-results;label_column:Method;subject:u-net;comparator:baseline;value_column:Accuracy (%)",
        availability=availability,
        unavailable_reason="source reader unavailable" if availability is EvidenceAvailability.UNAVAILABLE else None,
    )
    binding = ReviewBinding(
        tenant_id="tenant-1",
        actor_scope={"tenant_id": "tenant-1", "user_id": "user-1"},
        run_id="run-1",
        paper_id="paper-1",
        paper_snapshot_checksum=checksum_for({"source": "paper-1"}),
        evidence_revision="source-revision-1",
        evidence_checksum=evidence_checksum,
        claim=claim,
        rule_version="numeric-comparison-rule-1",
        strategy_version="numeric-comparison-strategy-1",
        evidence_locators=(locator,),
        applicable_conditions=conditions,
    )
    observation = NumericComparisonObservation(
        observation_id="observation-1",
        claim_checksum=claim.checksum,
        source_locator_checksum=locator.checksum,
        producer_id="table-parser-1",
        producer_type=ObservationProducer.PARSER,
        metric_id="accuracy",
        subject_label="u-net",
        comparator_label="baseline",
        subject_value=subject_value,
        comparator_value="83",
        unit="percent",
        comparator=NumericComparator.GREATER_THAN,
        difference_value=str(int(subject_value) - 83),
        difference_unit="percentage_point",
        conditions=observation_conditions or conditions,
    )
    observations = (observation, observation) if duplicate else (observation,)
    return NumericComparisonEligibilityBundle(
        binding=binding,
        claim_contract=contract,
        observations=observations,
    )


def test_supported_numeric_comparison_is_the_only_eligible_status() -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(_bundle())

    assert decision.resolution.status is ClaimConclusionStatus.SUPPORTED_WITHIN_SCOPE
    assert decision.eligible is True
    assert decision.qualification_ref == f"claim-qualification:{decision.checksum}"
    assert decision.progress_observations == ()


def test_typed_value_disagreement_is_contradicted() -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(_bundle(subject_value="91"))

    assert decision.resolution.status is ClaimConclusionStatus.CONTRADICTED
    assert decision.eligible is False
    assert decision.qualification_ref is None
    assert "typed_observation_contract_mismatch" in decision.reason_codes


def test_condition_conflict_is_insufficient_and_emits_observation_only_progress() -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(
        _bundle(observation_conditions={
            "dataset": "segmentation-v2",
            "split": "test",
            "table_id": "table-results",
            "label_column": "Method",
            "value_column": "Accuracy (%)",
            "source_scale": "percent",
        })
    )

    assert decision.resolution.status is ClaimConclusionStatus.INSUFFICIENT_EVIDENCE
    assert decision.eligible is False
    assert decision.qualification_ref is None
    assert decision.progress_observations[0].reason_code == INSUFFICIENT_VERIFIED_PROGRESS
    assert decision.progress_observations[0].observation_only is True
    assert "observation_condition_mismatch" in decision.reason_codes


def test_unavailable_source_is_distinct_from_contradiction() -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(
        _bundle(availability=EvidenceAvailability.UNAVAILABLE)
    )

    assert decision.resolution.status is ClaimConclusionStatus.VERIFICATION_UNAVAILABLE
    assert decision.eligible is False
    assert decision.qualification_ref is None


@pytest.mark.parametrize(
    ("bundle", "status"),
    (
        (_bundle(subject_value="91"), ClaimConclusionStatus.CONTRADICTED),
        (
            _bundle(observation_conditions={"dataset": "segmentation-v2"}),
            ClaimConclusionStatus.INSUFFICIENT_EVIDENCE,
        ),
        (
            _bundle(availability=EvidenceAvailability.UNAVAILABLE),
            ClaimConclusionStatus.VERIFICATION_UNAVAILABLE,
        ),
    ),
)
def test_noneligible_progress_is_numeric_and_bound_to_resolver_context(
    bundle: NumericComparisonEligibilityBundle,
    status: ClaimConclusionStatus,
) -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(bundle)
    observation, = decision.progress_observations

    assert decision.resolution.status is status
    assert decision.eligible is False
    assert decision.qualification_ref is None
    assert observation.run_id == bundle.binding.run_id
    assert observation.paper_id == bundle.binding.paper_id
    assert observation.claim_id == bundle.binding.claim.claim_id
    assert observation.observation_scope is ResearchProgressScope.CLAIM
    assert observation.resolver_status is status
    assert observation.metrics == {
        "eligible_claims": 0,
        "required_claims": 1,
        "verified_progress_ratio": 0.0,
    }
    assert isinstance(observation.metrics["eligible_claims"], int)
    assert isinstance(observation.metrics["verified_progress_ratio"], float)
    assert "resolver_status" not in observation.metrics
    assert observation.evidence_context_refs["binding"] == bundle.binding.checksum
    assert observation.evidence_context_refs["resolution"] == decision.resolution.checksum
    assert observation.evidence_context_refs["claim_contract"] == bundle.claim_contract.checksum
    assert observation.evidence_context_refs["source_context"] == checksum_for({
        "source_locator_checksums": decision.source_locator_checksums,
        "typed_observation_checksums": decision.typed_observation_checksums,
    })
    assert ResearchProgressObservation.from_dict(observation.to_dict()) == observation
    assert ClaimEvidenceEligibilityService().evaluate(bundle) == decision


def test_progress_metric_changes_cannot_grant_claim_qualification() -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(
        _bundle(availability=EvidenceAvailability.UNAVAILABLE)
    )
    progress, = decision.progress_observations
    optimistic_observation = replace(
        progress,
        metrics={
            "eligible_claims": 1,
            "required_claims": 1,
            "verified_progress_ratio": 1.0,
        },
    )
    observed_decision = replace(
        decision,
        progress_observations=(optimistic_observation,),
    )

    assert observed_decision.resolution == decision.resolution
    assert observed_decision.eligible is False
    assert observed_decision.qualification_ref is None
    assert observed_decision.progress_observations[0].metrics["eligible_claims"] == 1
    assert observed_decision.checksum != decision.checksum


def test_semantically_duplicate_typed_observations_are_deduplicated() -> None:
    decision = ClaimEvidenceEligibilityService().evaluate(_bundle(duplicate=True))

    assert decision.eligible is True
    assert len(decision.typed_observation_checksums) == 1


def test_claim_text_must_match_the_typed_contract() -> None:
    bundle = _bundle()
    forged_claim = ClaimRevision(
        claim_id=bundle.binding.claim.claim_id,
        revision=1,
        text="u-net reports 95 percent accuracy.",
        assertion_kind=AssertionKind.REPORTED_RESULT,
        claim_type=V1_NUMERIC_COMPARISON_CLAIM_TYPE,
        scope={"paper_id": "paper-1"},
        objective_revision="objective-1",
    )
    forged_binding = ReviewBinding(
        tenant_id=bundle.binding.tenant_id,
        actor_scope=bundle.binding.actor_scope,
        run_id=bundle.binding.run_id,
        paper_id=bundle.binding.paper_id,
        paper_snapshot_checksum=bundle.binding.paper_snapshot_checksum,
        evidence_revision=bundle.binding.evidence_revision,
        evidence_checksum=bundle.binding.evidence_checksum,
        claim=forged_claim,
        rule_version=bundle.binding.rule_version,
        strategy_version=bundle.binding.strategy_version,
        evidence_locators=bundle.binding.evidence_locators,
        applicable_conditions=bundle.binding.applicable_conditions,
    )
    forged = NumericComparisonEligibilityBundle(
        binding=forged_binding,
        claim_contract=bundle.claim_contract,
        observations=(),
    )

    try:
        ClaimEvidenceEligibilityService().evaluate(forged)
    except ValueError as exc:
        assert "claim text" in str(exc)
    else:  # pragma: no cover - explicit security assertion
        raise AssertionError("forged claim text was accepted")



def test_application_service_composes_parser_observation_from_accepted_table() -> None:
    from backend.research.domain import ResearchTable

    original = _bundle()
    table = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://paper-1/table-results",
        columns=["Method", "Accuracy (%)", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy (%)": "92%",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy (%)": "83%",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
        ],
    )

    bundle, decision = ClaimEvidenceEligibilityService().prepare_and_evaluate(
        binding=original.binding,
        table=table,
        producer_id="trusted-table-parser-1",
    )

    assert bundle.observations[0].producer_type is ObservationProducer.PARSER
    assert bundle.observations[0].source_locator_checksum == (
        original.binding.evidence_locators[0].checksum
    )
    assert decision.eligible is True



def test_application_collection_builds_binding_from_accepted_graph_artifacts() -> None:
    from backend.research.domain import (
        PaperSourceRecord,
        ResearchDocument,
        ResearchTable,
        SourceLineage,
    )
    from backend.research.services import ResearchEvidenceBuilder

    actor_scope = {
        "tenant_id": "tenant-1",
        "user_id": "user-1",
        "memory_namespace": "research-1",
    }
    source = PaperSourceRecord(
        source_id="source-1",
        paper_id="paper-1",
        source_type="arxiv",
        source_url="https://arxiv.org/abs/2607.00001",
        source_hash="sha256:trusted-source-content",
        actor_scope=actor_scope,
        metadata={"source_revision": "source-revision-1"},
    )
    table = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark",
        source_ref="paper://paper-1/table-results",
        columns=["Method", "Accuracy (%)", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy (%)": "92%",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy (%)": "83%",
                "Dataset": "segmentation-v1",
                "Split": "test",
            },
        ],
    )
    document = ResearchDocument(
        paper_id="paper-1",
        source_hash=source.source_hash or "",
        source_snapshot_id="source-revision-1",
        tables=[table],
        lineage=SourceLineage(
            source_refs=[table.source_ref],
            source_hash=source.source_hash,
        ),
        actor_scope=actor_scope,
    )
    evidence_pack = ResearchEvidenceBuilder().build_from_document(
        document=document,
        include_numeric_comparison_tables=True,
    )

    bundle, decision = ClaimEvidenceEligibilityService().collect_and_evaluate(
        tenant_id="tenant-1",
        actor_scope=actor_scope,
        run_id="run-1",
        source=source,
        document=document,
        evidence_pack=evidence_pack,
        objective_revision="objective-1",
        producer_id="trusted-table-parser-1",
    )

    assert bundle.binding.evidence_checksum.startswith("sha256:")
    assert bundle.binding.claim.claim_id in evidence_pack.items[-1].claim_refs
    assert decision.eligible is True


@pytest.mark.parametrize("invalid_kind", ["claim", "conditions", "locator"])
def test_one_valid_observation_cannot_mask_an_unbound_observation(invalid_kind: str) -> None:
    bundle = _bundle()
    valid = bundle.observations[0]
    updates = {"observation_id": "invalid-observation"}
    if invalid_kind == "claim":
        updates["claim_checksum"] = checksum_for({"claim": "another-revision"})
    elif invalid_kind == "conditions":
        updates["conditions"] = {**valid.conditions, "dataset": "another-dataset"}
    else:
        updates["source_locator_checksum"] = checksum_for({"locator": "another-cell"})
    invalid = replace(valid, **updates)

    decision = ClaimEvidenceEligibilityService().evaluate(
        replace(bundle, observations=(valid, invalid))
    )

    assert decision.resolution.status is ClaimConclusionStatus.INSUFFICIENT_EVIDENCE
    assert decision.eligible is False
    assert decision.qualification_ref is None
    assert any(reason.startswith("observation_") for reason in decision.reason_codes)


def test_available_locator_cannot_authorize_observation_of_an_unavailable_locator() -> None:
    bundle = _bundle()
    unavailable = replace(
        bundle.binding.evidence_locators[0],
        availability=EvidenceAvailability.UNAVAILABLE,
        unavailable_reason="table is unreadable",
    )
    binding = replace(
        bundle.binding,
        evidence_locators=(*bundle.binding.evidence_locators, unavailable),
    )
    observation = replace(
        bundle.observations[0],
        source_locator_checksum=unavailable.checksum,
    )

    decision = ClaimEvidenceEligibilityService().evaluate(
        replace(bundle, binding=binding, observations=(observation,))
    )

    assert decision.resolution.status is ClaimConclusionStatus.INSUFFICIENT_EVIDENCE
    assert decision.qualification_ref is None
    assert "observation_source_unavailable" in decision.reason_codes


def test_semantic_duplicate_candidate_producers_have_order_independent_decision() -> None:
    bundle = _bundle()
    worker_observation = replace(
        bundle.observations[0],
        observation_id="worker-observation",
        producer_id="worker-1",
        producer_type=ObservationProducer.WORKER,
    )
    service = ClaimEvidenceEligibilityService()

    first = service.evaluate(replace(bundle, observations=(*bundle.observations, worker_observation)))
    second = service.evaluate(replace(bundle, observations=(worker_observation, *bundle.observations)))

    assert first.to_dict() == second.to_dict()
    assert first.eligible is True
    assert len(first.typed_observation_checksums) == 1


def test_valid_observation_with_disagreeing_values_is_contradicted() -> None:
    bundle = _bundle()
    disagreement = replace(
        bundle.observations[0],
        observation_id="disagreeing-observation",
        subject_value="91",
        difference_value="8",
    )

    decision = ClaimEvidenceEligibilityService().evaluate(
        replace(bundle, observations=(*bundle.observations, disagreement))
    )

    assert decision.resolution.status is ClaimConclusionStatus.CONTRADICTED
    assert decision.qualification_ref is None
    assert "typed_observation_contract_mismatch" in decision.reason_codes
