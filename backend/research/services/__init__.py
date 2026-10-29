from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

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

_LAZY = LazyExports(
    __name__,
    {
        "CitationVerifier": ("backend.research.services.citation_verifier", "CitationVerifier"),
        "ClaimReviewResolver": ("backend.research.services.claim_review_resolver", "ClaimReviewResolver"),
        "NumericComparisonEligibilityResolver": ("backend.research.services.claim_evidence_eligibility", "NumericComparisonEligibilityResolver"),
        "build_qualified_claim_projection": ("backend.research.services.claim_evidence_eligibility", "build_qualified_claim_projection"),
        "numeric_comparison_claim_id": ("backend.research.services.claim_evidence_eligibility", "numeric_comparison_claim_id"),
        "numeric_comparison_table_locator": ("backend.research.services.claim_evidence_eligibility", "numeric_comparison_table_locator"),
        "trusted_claim_evidence_binding_values": ("backend.research.services.claim_evidence_eligibility", "trusted_claim_evidence_binding_values"),
        "trusted_numeric_comparison_contracts": ("backend.research.services.claim_evidence_eligibility", "trusted_numeric_comparison_contracts"),
        "ClaimExtractor": ("backend.research.services.claim_extractor", "ClaimExtractor"),
        "ResearchEvidenceBuilder": ("backend.research.services.evidence_builder", "ResearchEvidenceBuilder"),
        "ResearchProfileBuilder": ("backend.research.services.profile_builder", "ResearchProfileBuilder"),
        "ResearchQualityGate": ("backend.research.services.quality_gate", "ResearchQualityGate"),
        "ResearchRAGPolicyBuilder": ("backend.research.services.rag_policy", "ResearchRAGPolicyBuilder"),
        "ReaderIssueDetector": ("backend.research.services.reader_issue_detector", "ReaderIssueDetector"),
        "ReaderRepairGate": ("backend.research.services.reader_repair_gate", "ReaderRepairGate"),
        "chunk_visible_to_tenant": ("backend.research.services.tenant_visibility", "chunk_visible_to_tenant"),
        "metadata_tenant_ids": ("backend.research.services.tenant_visibility", "metadata_tenant_ids"),
        "public_metrics": ("backend.research.services.tenant_visibility", "public_metrics"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.services.citation_verifier import (
        CitationVerifier,
    )
    from backend.research.services.claim_review_resolver import (
        ClaimReviewResolver,
    )
    from backend.research.services.claim_evidence_eligibility import (
        NumericComparisonEligibilityResolver,
        build_qualified_claim_projection,
        numeric_comparison_claim_id,
        numeric_comparison_table_locator,
        trusted_claim_evidence_binding_values,
        trusted_numeric_comparison_contracts,
    )
    from backend.research.services.claim_extractor import (
        ClaimExtractor,
    )
    from backend.research.services.evidence_builder import (
        ResearchEvidenceBuilder,
    )
    from backend.research.services.profile_builder import (
        ResearchProfileBuilder,
    )
    from backend.research.services.quality_gate import (
        ResearchQualityGate,
    )
    from backend.research.services.rag_policy import (
        ResearchRAGPolicyBuilder,
    )
    from backend.research.services.reader_issue_detector import (
        ReaderIssueDetector,
    )
    from backend.research.services.reader_repair_gate import (
        ReaderRepairGate,
    )
    from backend.research.services.tenant_visibility import (
        chunk_visible_to_tenant,
        metadata_tenant_ids,
        public_metrics,
    )
