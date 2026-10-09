from __future__ import annotations

from types import SimpleNamespace

from backend.research.application.claim_evidence_eligibility import ClaimEvidenceEligibilityService
from backend.research.application.single_paper_runtime import ResearchSinglePaperRuntime
from backend.research.domain import (
    PaperSourceRecord, ResearchClaim, ResearchDocument, ResearchPaper,
    ResearchTable, SourceLineage, ThreeMinuteRead, research_event_tenant_id,
)
from backend.research.domain.claim_review import ClaimEligibilityDecision, NumericComparisonEligibilityBundle
from backend.research.graphs.claim_evidence_eligibility_gate import CLAIM_ELIGIBILITY_OUTPUT_KEY
from backend.research.services import CitationVerifier, ResearchEvidenceBuilder, ResearchQualityGate
from backend.research.services.claim_evidence_eligibility import build_qualified_claim_projection


def test_runtime_verification_and_final_analysis_share_the_actual_qualified_v1_claim() -> None:
    request_scope = {"tenant_id": "tenant-1", "user_id": "user-1", "memory_namespace": "research-1"}
    scope = {**request_scope, "tenant_id": research_event_tenant_id(request_scope)}
    source = PaperSourceRecord(
        source_id="source-1", paper_id="paper-1", source_type="other",
        source_url="paper://paper-1", source_hash="source-content-hash",
        actor_scope=scope, metadata={"source_revision": "source-revision-1"},
    )
    table = ResearchTable(
        table_id="results", caption="Accuracy", source_ref="paper://paper-1/results",
        columns=["Method", "Accuracy(%)", "Dataset", "Split"],
        rows=[
            {"Method": "atlas", "Accuracy(%)": "92%", "Dataset": "atlas-v1", "Split": "test"},
            {"Method": "baseline", "Accuracy(%)": "83%", "Dataset": "atlas-v1", "Split": "test"},
        ],
    )
    document = ResearchDocument(
        paper_id="paper-1", source_hash=source.source_hash, tables=[table], actor_scope=scope,
        lineage=SourceLineage(source_hash=source.source_hash, source_refs=[table.source_ref]),
    )
    evidence = ResearchEvidenceBuilder().build_from_document(
        document=document, include_numeric_comparison_tables=True,
    )
    candidate = ResearchClaim(
        claim_id="worker-candidate", text="A candidate experiment interpretation", claim_type="experiment",
        evidence_ids=[evidence.items[0].evidence_id], confidence=0.3,
    )
    workspace = SimpleNamespace(
        request=SimpleNamespace(
            run_id="run-1", tenant_id="tenant-1", user_id="user-1", memory_namespace="research-1",
            options={"research_input_quality_enabled": True},
        ),
        paper=ResearchPaper(paper_id="paper-1", title="Accuracy paper"),
        source_record=source, document=document, evidence_pack=evidence,
        input_context=SimpleNamespace(profile=SimpleNamespace(task_revision="objective-1")),
        summary=ThreeMinuteRead(problem="compare accuracy", core_idea="atlas improves accuracy",
                                evidence_refs=[evidence.items[0].as_ref()]),
        claims=[candidate], contributions=[], taxonomy_candidates=[], llm_candidate_warnings=[],
        score_gate_results=[], research_rag_context=None,
    )
    runtime = object.__new__(ResearchSinglePaperRuntime)
    runtime.claim_evidence_eligibility_service = ClaimEvidenceEligibilityService()
    runtime.citation_verifier = CitationVerifier()
    runtime.quality_gate = ResearchQualityGate()
    task = {"inputs": {"analysis_branch_refs": [
        {"producer_node_id": "analyze_structure", "output_key": "structure_candidate"},
        {"producer_node_id": "analyze_contribution", "output_key": "contribution_candidate"},
        {"producer_node_id": "analyze_experiments", "output_key": "experiment_candidate"},
    ]}}

    verified = runtime._verify_claims(task, workspace)
    assert CLAIM_ELIGIBILITY_OUTPUT_KEY in verified.output, verified.output.get(
        "claim_evidence_eligibility_collection", verified.output,
    )
    payload = verified.output[CLAIM_ELIGIBILITY_OUTPUT_KEY]
    bundle = NumericComparisonEligibilityBundle.from_dict(payload["bundle"])
    decision = ClaimEligibilityDecision.from_dict(payload["decision"])
    expected = build_qualified_claim_projection(bundle, decision, evidence)
    final = runtime._quality_gate({}, workspace)

    assert bundle.binding.tenant_id == scope["tenant_id"]
    assert decision.eligible is True
    assert verified.output["claim_models"] == [candidate.to_dict(), expected]
    assert final.output["analysis"]["claims"] == verified.output["claim_models"]
    assert expected["text"] in final.output["analysis"]["experiments"]
    assert candidate.metadata == {}
    qualified = [claim for claim in workspace.analysis.claims if claim.metadata.get("qualification_ref")]
    assert [claim.to_dict() for claim in qualified] == [expected]
    assert workspace.analysis.claims[0].metadata.get("conclusion_status") != "verified_conclusion"
    assert workspace.quality.passed is True
