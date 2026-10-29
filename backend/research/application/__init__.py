from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "PreparedResearchInputContext",
    "ResearchInputContractError",
    "ResearchInputContractService",
    "ResearchVerifiedConclusionQualificationPort",
    "research_input_quality_enabled",
    "CatalogError",
    "ClaimEvidenceEligibilityService",
    "CatalogLeaderboardResult",
    "AnalyzePaperUseCase",
    "AnalyzePaperRequest",
    "AskPaperUseCase",
    "InMemoryResearchCatalogRepository",
    "InMemoryResearchEventSink",
    "BuildPaperCardUseCase",
    "BuildReaderUseCase",
    "GenerateReadingNoteUseCase",
    "RESEARCH_CLAIM_ELIGIBILITY_GATE_REF",
    "RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF",
    "numeric_comparison_claim_id",
    "numeric_comparison_table_locator",
    "render_numeric_comparison_claim_text",
    "trusted_claim_evidence_binding_values",
    "trusted_numeric_comparison_contracts",
    "RESEARCH_NODE_RESULT_POLICIES",
    "RESEARCH_WORKER_CANDIDATE_SCHEMA",
    "ResearchAnalysisResult",
    "ResearchPaperCatalogService",
    "ResearchDynamicTaskPlanUnavailableError",
    "ResearchGraphResultCommitter",
    "ResearchGraphArtifactAlertList",
    "ResearchGraphArtifactGcApplyResult",
    "ResearchGraphArtifactGovernanceService",
    "ResearchGraphArtifactQuotaInspection",
    "ResearchGraphArtifactReconciliation",
    "ResearchNodeResultPolicy",
    "ResearchTaskPlanResultMaterializer",
    "MetadataOnlySourceResolver",
    "ParsePaperError",
    "ParsePaperRequest",
    "ParsePaperResult",
    "ParsePaperUseCase",
    "ResolvedPaperSource",
    "ResearchCatalogProjection",
    "ResearchSourceResolver",
    "identity_from_paper",
    "infer_source_type",
    "ResearchActorScope",
    "ResearchRunDispositionDecision",
    "ResearchRunDispositionReconciler",
    "ResearchRunFailureRecoverySource",
    "ResearchRunRecoverySource",
    "classify_research_run_record",
    "derive_research_run_disposition",
    "research_identity_scope_ref",
    "research_node_result_policy",
    "research_subject_scope_ref",
    "ReaderRepairGraphApplicationService",
    "ReaderRepairGraphRequest",
    "ReaderRepairGraphRunResult",
]

_LAZY = LazyExports(
    __name__,
    {
        "AnalyzePaperRequest": ("backend.research.application.analyze_paper", "AnalyzePaperRequest"),
        "AnalyzePaperUseCase": ("backend.research.application.analyze_paper", "AnalyzePaperUseCase"),
        "ResearchAnalysisResult": ("backend.research.application.analyze_paper", "ResearchAnalysisResult"),
        "ResearchDynamicTaskPlanUnavailableError": ("backend.research.application.analyze_paper", "ResearchDynamicTaskPlanUnavailableError"),
        "ClaimEvidenceEligibilityService": ("backend.research.application.claim_evidence_eligibility", "ClaimEvidenceEligibilityService"),
        "RESEARCH_CLAIM_ELIGIBILITY_GATE_REF": ("backend.research.application.claim_evidence_eligibility", "RESEARCH_CLAIM_ELIGIBILITY_GATE_REF"),
        "RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF": ("backend.research.application.claim_evidence_eligibility", "RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF"),
        "numeric_comparison_claim_id": ("backend.research.application.claim_evidence_eligibility", "numeric_comparison_claim_id"),
        "numeric_comparison_table_locator": ("backend.research.application.claim_evidence_eligibility", "numeric_comparison_table_locator"),
        "render_numeric_comparison_claim_text": ("backend.research.application.claim_evidence_eligibility", "render_numeric_comparison_claim_text"),
        "trusted_claim_evidence_binding_values": ("backend.research.application.claim_evidence_eligibility", "trusted_claim_evidence_binding_values"),
        "trusted_numeric_comparison_contracts": ("backend.research.application.claim_evidence_eligibility", "trusted_numeric_comparison_contracts"),
        "PreparedResearchInputContext": ("backend.research.application.research_input_contract", "PreparedResearchInputContext"),
        "ResearchInputContractError": ("backend.research.application.research_input_contract", "ResearchInputContractError"),
        "ResearchInputContractService": ("backend.research.application.research_input_contract", "ResearchInputContractService"),
        "ResearchVerifiedConclusionQualificationPort": ("backend.research.application.research_input_contract", "ResearchVerifiedConclusionQualificationPort"),
        "research_input_quality_enabled": ("backend.research.application.research_input_contract", "research_input_quality_enabled"),
        "CatalogError": ("backend.research.application.catalog", "CatalogError"),
        "CatalogLeaderboardResult": ("backend.research.application.catalog", "CatalogLeaderboardResult"),
        "InMemoryResearchCatalogRepository": ("backend.research.application.catalog", "InMemoryResearchCatalogRepository"),
        "ResearchPaperCatalogService": ("backend.research.application.catalog", "ResearchPaperCatalogService"),
        "InMemoryResearchEventSink": ("backend.research.application.parse_paper", "InMemoryResearchEventSink"),
        "MetadataOnlySourceResolver": ("backend.research.application.parse_paper", "MetadataOnlySourceResolver"),
        "ParsePaperError": ("backend.research.application.parse_paper", "ParsePaperError"),
        "ParsePaperRequest": ("backend.research.application.parse_paper", "ParsePaperRequest"),
        "ParsePaperResult": ("backend.research.application.parse_paper", "ParsePaperResult"),
        "ParsePaperUseCase": ("backend.research.application.parse_paper", "ParsePaperUseCase"),
        "ResolvedPaperSource": ("backend.research.application.parse_paper", "ResolvedPaperSource"),
        "ResearchCatalogProjection": ("backend.research.application.parse_paper", "ResearchCatalogProjection"),
        "ResearchSourceResolver": ("backend.research.application.parse_paper", "ResearchSourceResolver"),
        "identity_from_paper": ("backend.research.application.parse_paper", "identity_from_paper"),
        "infer_source_type": ("backend.research.application.parse_paper", "infer_source_type"),
        "AskPaperUseCase": ("backend.research.application.ask_paper", "AskPaperUseCase"),
        "ResearchActorScope": ("backend.research.application.ask_paper", "ResearchActorScope"),
        "BuildPaperCardUseCase": ("backend.research.application.build_paper_card", "BuildPaperCardUseCase"),
        "BuildReaderUseCase": ("backend.research.application.build_reader", "BuildReaderUseCase"),
        "GenerateReadingNoteUseCase": ("backend.research.application.generate_reading_note", "GenerateReadingNoteUseCase"),
        "RESEARCH_NODE_RESULT_POLICIES": ("backend.research.application.graph_result_committer", "RESEARCH_NODE_RESULT_POLICIES"),
        "RESEARCH_WORKER_CANDIDATE_SCHEMA": ("backend.research.application.graph_result_committer", "RESEARCH_WORKER_CANDIDATE_SCHEMA"),
        "ResearchGraphResultCommitter": ("backend.research.application.graph_result_committer", "ResearchGraphResultCommitter"),
        "ResearchNodeResultPolicy": ("backend.research.application.graph_result_committer", "ResearchNodeResultPolicy"),
        "ResearchTaskPlanResultMaterializer": ("backend.research.application.graph_result_committer", "ResearchTaskPlanResultMaterializer"),
        "research_node_result_policy": ("backend.research.application.graph_result_committer", "research_node_result_policy"),
        "ResearchGraphArtifactAlertList": ("backend.research.application.graph_artifact_governance", "ResearchGraphArtifactAlertList"),
        "ResearchGraphArtifactGcApplyResult": ("backend.research.application.graph_artifact_governance", "ResearchGraphArtifactGcApplyResult"),
        "ResearchGraphArtifactGovernanceService": ("backend.research.application.graph_artifact_governance", "ResearchGraphArtifactGovernanceService"),
        "ResearchGraphArtifactQuotaInspection": ("backend.research.application.graph_artifact_governance", "ResearchGraphArtifactQuotaInspection"),
        "ResearchGraphArtifactReconciliation": ("backend.research.application.graph_artifact_governance", "ResearchGraphArtifactReconciliation"),
        "ResearchRunDispositionDecision": ("backend.research.application.run_disposition", "ResearchRunDispositionDecision"),
        "ResearchRunDispositionReconciler": ("backend.research.application.run_disposition", "ResearchRunDispositionReconciler"),
        "ResearchRunFailureRecoverySource": ("backend.research.application.run_disposition", "ResearchRunFailureRecoverySource"),
        "ResearchRunRecoverySource": ("backend.research.application.run_disposition", "ResearchRunRecoverySource"),
        "classify_research_run_record": ("backend.research.application.run_disposition", "classify_research_run_record"),
        "derive_research_run_disposition": ("backend.research.application.run_disposition", "derive_research_run_disposition"),
        "research_identity_scope_ref": ("backend.research.application.run_disposition", "research_identity_scope_ref"),
        "research_subject_scope_ref": ("backend.research.application.run_disposition", "research_subject_scope_ref"),
        "ReaderRepairGraphApplicationService": ("backend.research.application.reader_repair_runtime", "ReaderRepairGraphApplicationService"),
        "ReaderRepairGraphRequest": ("backend.research.application.reader_repair_runtime", "ReaderRepairGraphRequest"),
        "ReaderRepairGraphRunResult": ("backend.research.application.reader_repair_runtime", "ReaderRepairGraphRunResult"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.application.analyze_paper import (
        AnalyzePaperRequest,
        AnalyzePaperUseCase,
        ResearchAnalysisResult,
        ResearchDynamicTaskPlanUnavailableError,
    )
    from backend.research.application.claim_evidence_eligibility import (
        ClaimEvidenceEligibilityService,
        RESEARCH_CLAIM_ELIGIBILITY_GATE_REF,
        RESEARCH_CLAIM_ELIGIBILITY_RESOLVER_REF,
        numeric_comparison_claim_id,
        numeric_comparison_table_locator,
        render_numeric_comparison_claim_text,
        trusted_claim_evidence_binding_values,
        trusted_numeric_comparison_contracts,
    )
    from backend.research.application.research_input_contract import (
        PreparedResearchInputContext,
        ResearchInputContractError,
        ResearchInputContractService,
        ResearchVerifiedConclusionQualificationPort,
        research_input_quality_enabled,
    )
    from backend.research.application.catalog import (
        CatalogError,
        CatalogLeaderboardResult,
        InMemoryResearchCatalogRepository,
        ResearchPaperCatalogService,
    )
    from backend.research.application.parse_paper import (
        InMemoryResearchEventSink,
        MetadataOnlySourceResolver,
        ParsePaperError,
        ParsePaperRequest,
        ParsePaperResult,
        ParsePaperUseCase,
        ResolvedPaperSource,
        ResearchCatalogProjection,
        ResearchSourceResolver,
        identity_from_paper,
        infer_source_type,
    )
    from backend.research.application.ask_paper import (
        AskPaperUseCase,
        ResearchActorScope,
    )
    from backend.research.application.build_paper_card import (
        BuildPaperCardUseCase,
    )
    from backend.research.application.build_reader import (
        BuildReaderUseCase,
    )
    from backend.research.application.generate_reading_note import (
        GenerateReadingNoteUseCase,
    )
    from backend.research.application.graph_result_committer import (
        RESEARCH_NODE_RESULT_POLICIES,
        RESEARCH_WORKER_CANDIDATE_SCHEMA,
        ResearchGraphResultCommitter,
        ResearchNodeResultPolicy,
        ResearchTaskPlanResultMaterializer,
        research_node_result_policy,
    )
    from backend.research.application.graph_artifact_governance import (
        ResearchGraphArtifactAlertList,
        ResearchGraphArtifactGcApplyResult,
        ResearchGraphArtifactGovernanceService,
        ResearchGraphArtifactQuotaInspection,
        ResearchGraphArtifactReconciliation,
    )
    from backend.research.application.run_disposition import (
        ResearchRunDispositionDecision,
        ResearchRunDispositionReconciler,
        ResearchRunFailureRecoverySource,
        ResearchRunRecoverySource,
        classify_research_run_record,
        derive_research_run_disposition,
        research_identity_scope_ref,
        research_subject_scope_ref,
    )
    from backend.research.application.reader_repair_runtime import (
        ReaderRepairGraphApplicationService,
        ReaderRepairGraphRequest,
        ReaderRepairGraphRunResult,
    )
