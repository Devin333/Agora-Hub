from __future__ import annotations

from types import SimpleNamespace

from dataclasses import replace
from framework.events.canonical import checksum_for
import pytest

from backend.research.application.claim_evidence_eligibility import (
    ClaimEvidenceEligibilityService,
    numeric_comparison_claim_id,
    render_numeric_comparison_claim_text,
    trusted_numeric_comparison_contracts,
)
from backend.research.domain import (
    PaperSourceRecord,
    ResearchDocument,
    ResearchTable,
    SourceLineage,
)
from backend.research.domain.claim_review import (
    AssertionKind,
    ClaimRevision,
    EvidenceAvailability,
    EvidenceSourceLocator,
    NumericComparator,
    NumericComparisonClaim,
    NumericComparisonEligibilityBundle,
    NumericComparisonObservation,
    ObservationProducer,
    ReviewBinding,
    ResearchProgressObservation,
    ResearchProgressScope,
    ClaimConclusionStatus,
    V1_NUMERIC_COMPARISON_CLAIM_TYPE,
)
from backend.research.graphs.claim_evidence_eligibility_gate import (
    CLAIM_ELIGIBILITY_OUTPUT_KEY,
    ResearchClaimEvidenceEligibilityGate,
    build_claim_evidence_eligibility_payload,
    numeric_comparison_table_locator,
    trusted_claim_evidence_binding_values,
)
from backend.research.graphs.gates import build_paper_analysis_gate_registry
from backend.research.services.claim_evidence_eligibility import build_qualified_claim_projection
from backend.research.services import ResearchEvidenceBuilder


_SCOPE = {"tenant_id": "tenant-1", "user_id": "user-1", "memory_namespace": "research-1"}
_SOURCE_REF = "paper://paper-1/table-results"


def _trusted_models(
    *,
    table_subject_value: str = "92%",
    dataset: str = "segmentation-v1",
):
    source = PaperSourceRecord(
        source_id="source-1",
        paper_id="paper-1",
        source_type="arxiv",
        source_url="https://arxiv.org/abs/2607.00001",
        source_hash="sha256:trusted-source-content",
        actor_scope=_SCOPE,
        metadata={"source_revision": "source-revision-1"},
    )
    table = ResearchTable(
        table_id="table-results",
        caption="Accuracy benchmark results",
        source_ref=_SOURCE_REF,
        columns=["Method", "Accuracy (%)", "Dataset", "Split"],
        rows=[
            {
                "Method": "u-net",
                "Accuracy (%)": table_subject_value,
                "Dataset": dataset,
                "Split": "test",
            },
            {
                "Method": "baseline",
                "Accuracy (%)": "83%",
                "Dataset": dataset,
                "Split": "test",
            },
        ],
        page=7,
    )
    document = ResearchDocument(
        paper_id="paper-1",
        source_hash=source.source_hash or "",
        source_snapshot_id="source-revision-1",
        tables=[table],
        lineage=SourceLineage(source_refs=[_SOURCE_REF], source_hash=source.source_hash),
        actor_scope=_SCOPE,
    )
    evidence_pack = ResearchEvidenceBuilder().build_from_document(
        pack_id="pack-1",
        document=document,
        include_numeric_comparison_tables=True,
    )
    return source, document, evidence_pack


def _bundle(
    source: PaperSourceRecord,
    document: ResearchDocument,
    evidence_pack: ResearchEvidencePack,
    *,
    locator_source_id: str | None = None,
    availability: EvidenceAvailability = EvidenceAvailability.AVAILABLE,
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
    trusted = trusted_claim_evidence_binding_values(source, document, evidence_pack)
    locator = EvidenceSourceLocator(
        source_id=locator_source_id or source.source_id,
        source_revision=trusted["evidence_revision"],
        source_checksum=trusted["evidence_checksum"],
        locator_type="research_table_cells",
        locator=numeric_comparison_table_locator(
            table_id="table-results",
            label_column="Method",
            subject_label="u-net",
            comparator_label="baseline",
            value_column="Accuracy (%)",
        ),
        availability=availability,
        unavailable_reason="table reader unavailable" if availability is EvidenceAvailability.UNAVAILABLE else None,
    )
    binding = ReviewBinding(
        tenant_id="tenant-1",
        actor_scope=_SCOPE,
        run_id="run-1",
        paper_id="paper-1",
        paper_snapshot_checksum=trusted["paper_snapshot_checksum"],
        evidence_revision=trusted["evidence_revision"],
        evidence_checksum=trusted["evidence_checksum"],
        claim=claim,
        rule_version="numeric-comparison-rule-1",
        strategy_version="numeric-comparison-strategy-1",
        evidence_locators=(locator,),
        applicable_conditions=conditions,
    )
    observation = NumericComparisonObservation(
        observation_id="parser-observation-1",
        claim_checksum=claim.checksum,
        source_locator_checksum=locator.checksum,
        producer_id="table-parser-1",
        producer_type=ObservationProducer.PARSER,
        metric_id=contract.metric_id,
        subject_label=contract.subject_label,
        comparator_label=contract.comparator_label,
        subject_value=contract.subject_value,
        comparator_value=contract.comparator_value,
        unit=contract.unit,
        comparator=contract.comparator,
        difference_value=contract.difference_value,
        difference_unit=contract.difference_unit,
        conditions=conditions,
    )
    return NumericComparisonEligibilityBundle(
        binding=binding,
        claim_contract=contract,
        observations=(observation,),
    )


def _context(bundle, source, document, evidence_pack, *, extra_output=None):
    decision = ClaimEvidenceEligibilityService().evaluate(bundle)
    try:
        claim_models = (
            [build_qualified_claim_projection(bundle, decision, evidence_pack)]
            if decision.eligible
            else []
        )
    except ValueError:
        claim_models = []
    output = {
        CLAIM_ELIGIBILITY_OUTPUT_KEY: build_claim_evidence_eligibility_payload(bundle),
        "claim_models": claim_models,
    }
    output.update(extra_output or {})
    return SimpleNamespace(
        run_spec=SimpleNamespace(
            run_id="run-1",
            inputs={"paper_id": "paper-1", **_SCOPE},
            metadata={"graph_terminal_tenant_id": "tenant-1"},
        ),
        outputs={
            "paper_source": {"source_record": source.model_dump(mode="json")},
            "document": {"document": document.model_dump(mode="json")},
            "evidence_pack": {"evidence_pack": evidence_pack.model_dump(mode="json")},
        },
        worker_result=SimpleNamespace(output=output),
    )


def test_gate_is_registered_by_exact_version_and_replays_trusted_evidence() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)
    gate = build_paper_analysis_gate_registry().resolve(
        "ResearchClaimEvidenceEligibilityGate@1"
    ).gate

    result = gate.evaluate(_context(bundle, source, document, evidence_pack))

    assert result.passed is True
    assert result.details["qualification_ref"].startswith("claim-qualification:sha256:")
    assert result.details["harness_gate"]["reference"] == "ResearchClaimEvidenceEligibilityGate@1"
    assert result.details["trusted_document_ref"].startswith("sha256:")


def test_eligible_bundle_cannot_qualify_tampered_report_claim_text() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)
    context = _context(bundle, source, document, evidence_pack)
    context.worker_result.output["claim_models"][0]["text"] = "forged result text"

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert result.passed is False
    assert result.details["reason_code"] == "claim_projection_mismatch"


def test_self_consistent_forged_bundle_and_decision_cannot_replace_run_owned_source() -> None:
    source, document, evidence_pack = _trusted_models()
    forged = _bundle(
        source,
        document,
        evidence_pack,
        locator_source_id="attacker-source",
    )

    result = ResearchClaimEvidenceEligibilityGate().evaluate(
        _context(forged, source, document, evidence_pack)
    )

    assert result.passed is False
    assert result.details["reason_code"] == "claim_source_locator_mismatch"


def test_typed_observation_must_match_the_trusted_table_cells() -> None:
    source, document, evidence_pack = _trusted_models(table_subject_value="91%")
    bundle = _bundle(source, document, evidence_pack)

    result = ResearchClaimEvidenceEligibilityGate().evaluate(
        _context(bundle, source, document, evidence_pack)
    )

    assert result.passed is False
    assert result.details["reason_code"] == "claim_typed_observation_mismatch"
    assert result.details["trusted_contract_refs"]


def test_candidate_publication_and_qualification_fields_are_rejected() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)

    result = ResearchClaimEvidenceEligibilityGate().evaluate(
        _context(
            bundle,
            source,
            document,
            evidence_pack,
            extra_output={"passed": True, "qualification_ref": "caller-controlled"},
        )
    )

    assert result.passed is False
    assert result.details == {
        "reason_code": "candidate_control_field_rejected",
        "forbidden_fields": ["passed", "qualification_ref"],
    }


def test_unavailable_resolver_status_fails_gate_without_becoming_contradiction() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(
        source,
        document,
        evidence_pack,
        availability=EvidenceAvailability.UNAVAILABLE,
    )

    result = ResearchClaimEvidenceEligibilityGate().evaluate(
        _context(bundle, source, document, evidence_pack)
    )

    assert result.passed is False
    assert result.details["reason_code"] == "claim_eligibility_verification_unavailable"
    assert result.details["qualification_ref"] is None
    progress, = result.details["progress_observations"]
    observation = ResearchProgressObservation.from_dict(progress)
    assert observation.reason_code == "insufficient_verified_progress"
    assert observation.observation_only is True
    assert observation.resolver_status is ClaimConclusionStatus.VERIFICATION_UNAVAILABLE
    assert observation.metrics == {
        "eligible_claims": 0,
        "required_claims": 1,
        "verified_progress_ratio": 0.0,
    }
    assert result.details["progress_observation_refs"] == [observation.checksum]



def test_candidate_dataset_cannot_override_trusted_table_conditions() -> None:
    source, document, evidence_pack = _trusted_models(dataset="segmentation-v2")
    bundle = _bundle(source, document, evidence_pack)

    result = ResearchClaimEvidenceEligibilityGate().evaluate(
        _context(bundle, source, document, evidence_pack)
    )

    assert result.passed is False
    assert result.details["reason_code"] == "claim_typed_observation_mismatch"
    assert result.details["trusted_contract_refs"]



def test_eligibility_gate_keeps_existing_claim_citation_gate_as_dependency() -> None:
    registry = build_paper_analysis_gate_registry()

    bindings = registry.bindings_for("ResearchClaimEvidenceEligibilityGate@1")

    assert [str(binding.reference) for binding in bindings] == [
        "ClaimEvidenceGate@1",
        "ResearchClaimEvidenceEligibilityGate@1",
    ]



def test_gate_uses_graph_terminal_tenant_when_request_has_no_tenant_id() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)
    context = _context(bundle, source, document, evidence_pack)
    context.run_spec.inputs.pop("tenant_id")

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert result.passed is True
    assert result.details["reason_code"] == "claim_eligibility_supported"



def test_missing_v1_result_is_insufficient_and_never_publishes() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)
    context = _context(bundle, source, document, evidence_pack)
    context.worker_result.output.pop(CLAIM_ELIGIBILITY_OUTPUT_KEY)

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert result.passed is False
    assert result.details["reason_code"] == "claim_eligibility_insufficient_evidence"
    progress = ResearchProgressObservation.from_dict(
        result.details["progress_observations"][0]
    )
    assert progress.reason_code == "insufficient_verified_progress"
    assert progress.observation_only is True
    assert progress.run_id == context.run_spec.run_id
    assert progress.paper_id == context.run_spec.inputs["paper_id"]
    assert progress.observation_scope is ResearchProgressScope.RUN
    assert progress.claim_id is None
    assert progress.resolver_status is None
    assert progress.metrics == {
        "eligible_claims": 0,
        "required_claims": 1,
        "verified_progress_ratio": 0.0,
    }
    assert dict(progress.evidence_context_refs) == {
        key: checksum_for(value)
        for key, value in context.outputs.items()
    }
    assert result.details["progress_observation_refs"] == [progress.checksum]


def test_optimistic_progress_cannot_override_deterministic_gate_replay() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(
        source,
        document,
        evidence_pack,
        availability=EvidenceAvailability.UNAVAILABLE,
    )
    context = _context(bundle, source, document, evidence_pack)
    decision = ClaimEvidenceEligibilityService().evaluate(bundle)
    progress, = decision.progress_observations
    optimistic = replace(
        progress,
        metrics={
            "eligible_claims": 1,
            "required_claims": 1,
            "verified_progress_ratio": 1.0,
        },
    )
    candidate_decision = replace(decision, progress_observations=(optimistic,))
    context.worker_result.output[CLAIM_ELIGIBILITY_OUTPUT_KEY]["decision"] = (
        candidate_decision.to_dict()
    )

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert candidate_decision.eligible is False
    assert candidate_decision.qualification_ref is None
    assert result.passed is False
    assert result.details["reason_code"] == "claim_eligibility_replay_mismatch"


@pytest.mark.parametrize("field", ["publish", "route", "memory_write", "tool_authorization"])
def test_observation_cannot_carry_control_fields(field: str) -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(
        source,
        document,
        evidence_pack,
        availability=EvidenceAvailability.UNAVAILABLE,
    )
    context = _context(bundle, source, document, evidence_pack)
    progress = context.worker_result.output[CLAIM_ELIGIBILITY_OUTPUT_KEY][
        "decision"
    ]["progress_observations"][0]
    progress[field] = True

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert result.passed is False
    assert result.details["reason_code"] == "claim_eligibility_replay_invalid"


@pytest.mark.parametrize("metadata", [
    {"conclusion_status": "verified_conclusion"},
    {"qualification_ref": "claim-qualification:untrusted"},
    {"eligibility_gate_ref": "OtherGate@1"},
    {"eligibility_decision_ref": "sha256:untrusted"},
])
def test_unrelated_candidate_cannot_self_report_qualification(metadata: dict[str, str]) -> None:
    source, document, evidence_pack = _trusted_models()
    context = _context(_bundle(source, document, evidence_pack), source, document, evidence_pack)
    context.worker_result.output["claim_models"].append({
        "claim_id": "worker-extra-claim",
        "text": "an unverified candidate result",
        "claim_type": "experiment",
        "evidence_ids": [],
        "confidence": 0.5,
        "metadata": metadata,
    })

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert result.passed is False
    assert result.details["reason_code"] == "claim_projection_unauthorized_qualification"
    assert result.details["claim_ids"] == ["worker-extra-claim"]


@pytest.mark.parametrize("mutation,expected", [
    ("duplicate", "claim_projection_ambiguous"),
    ("evidence", "claim_projection_mismatch"),
    ("qualification", "claim_projection_mismatch"),
    ("revision", "claim_projection_mismatch"),
])
def test_actual_report_projection_rejects_duplicate_or_wrong_binding(mutation: str, expected: str) -> None:
    source, document, evidence_pack = _trusted_models()
    context = _context(_bundle(source, document, evidence_pack), source, document, evidence_pack)
    claim = context.worker_result.output["claim_models"][0]
    if mutation == "duplicate":
        context.worker_result.output["claim_models"].append(dict(claim))
    elif mutation == "evidence":
        claim["evidence_ids"] = ["wrong-evidence-item"]
    elif mutation == "qualification":
        claim["metadata"]["qualification_ref"] = "claim-qualification:forged"
    else:
        claim["metadata"]["claim_revision"] = 2

    result = ResearchClaimEvidenceEligibilityGate().evaluate(context)

    assert result.passed is False
    assert result.details["reason_code"] == expected


@pytest.mark.parametrize("mutation", ["revision", "hash", "actor", "source_ref"])
def test_collection_rejects_inconsistent_accepted_artifacts_before_qualification(mutation: str) -> None:
    source, document, evidence_pack = _trusted_models()
    if mutation == "revision":
        document = document.model_copy(update={"metadata": {"source_revision": "stale-revision"}})
    elif mutation == "hash":
        document = document.model_copy(update={
            "lineage": document.lineage.model_copy(update={"source_hash": "different-hash"})
        })
    elif mutation == "actor":
        source = source.model_copy(update={"actor_scope": {"tenant_id": "tenant-1"}})
    else:
        table = document.tables[0].model_copy(update={"source_ref": "paper://another-paper/table-results"})
        document = document.model_copy(update={"tables": [table]})
        evidence_pack = ResearchEvidenceBuilder().build_from_document(
            document=document, include_numeric_comparison_tables=True,
        )

    with pytest.raises(ValueError):
        ClaimEvidenceEligibilityService().collect_and_evaluate(
            tenant_id="tenant-1", actor_scope=_SCOPE, run_id="run-1",
            source=source, document=document, evidence_pack=evidence_pack,
            objective_revision="objective-1", producer_id="table-parser",
        )


def test_projection_builder_replays_decision_instead_of_accepting_forged_receipt() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)
    decision = ClaimEvidenceEligibilityService().evaluate(bundle)
    forged = replace(decision, gate_ref="ForgedGate@1")

    with pytest.raises(ValueError, match="matching eligible decision"):
        build_qualified_claim_projection(bundle, forged, evidence_pack)


def test_projection_builder_requires_unique_typed_evidence_binding() -> None:
    source, document, evidence_pack = _trusted_models()
    bundle = _bundle(source, document, evidence_pack)
    decision = ClaimEvidenceEligibilityService().evaluate(bundle)
    duplicated = evidence_pack.model_copy(update={"items": [*evidence_pack.items, evidence_pack.items[-1]]})

    with pytest.raises(ValueError, match="exactly one"):
        build_qualified_claim_projection(bundle, decision, duplicated)
