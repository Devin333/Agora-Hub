from __future__ import annotations

from backend.research.services.citation_verifier import CitationVerifier
from backend.research.services.claim_review_resolver import ClaimReviewResolver
from backend.research.services.claim_evidence_eligibility import (
    NumericComparisonEligibilityResolver,
    build_qualified_claim_projection,
    numeric_comparison_claim_id,
    numeric_comparison_table_locator,
    trusted_claim_evidence_binding_values,
    trusted_numeric_comparison_contracts,
)
from backend.research.services.claim_extractor import ClaimExtractor
from backend.research.services.evidence_builder import ResearchEvidenceBuilder
from backend.research.services.profile_builder import ResearchProfileBuilder
from backend.research.services.quality_gate import ResearchQualityGate
from backend.research.services.rag_policy import ResearchRAGPolicyBuilder
from backend.research.services.reader_issue_detector import ReaderIssueDetector
from backend.research.services.reader_repair_gate import ReaderRepairGate
from backend.research.services.tenant_visibility import (
    chunk_visible_to_tenant,
    metadata_tenant_ids,
    public_metrics,
)

__all__ = [
    "CitationVerifier",
    "NumericComparisonEligibilityResolver",
    "build_qualified_claim_projection",
    "ClaimReviewResolver",
    "ClaimExtractor",
    "ReaderIssueDetector",
    "ReaderRepairGate",
    "ResearchEvidenceBuilder",
    "ResearchProfileBuilder",
    "ResearchQualityGate",
    "ResearchRAGPolicyBuilder",
    "chunk_visible_to_tenant",
    "metadata_tenant_ids",
    "numeric_comparison_claim_id",
    "numeric_comparison_table_locator",
    "public_metrics",
    "trusted_claim_evidence_binding_values",
    "trusted_numeric_comparison_contracts",
]
