from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "EvidenceRef",
    "GateResult",
    "PaperCardBuilder",
    "QualityFlag",
    "READER_REPAIR_NAMESPACE",
    "ReaderIssue",
    "ReaderIssueSignature",
    "ReaderRepairAttempt",
    "ReaderRepairCandidate",
    "ReaderRepairCase",
    "ReaderRepairContextPack",
    "ReaderRepairMemoryQuery",
    "ReaderRepairRAGPolicy",
    "ReaderRepairResult",
    "ReaderRepairSkillCandidateSeed",
    "ReaderRepairStrategy",
    "ResearchAnalysis",
    "ResearchClaim",
    "ResearchDocument",
    "ResearchEvidenceItem",
    "ResearchEvidencePack",
    "ResearchPaper",
    "ResearchPaperCard",
    "ResearchQualityResult",
    "ResearchReaderPayload",
    "ResearchSection",
    "SourceLineage",
    "TaxonomyAssignment",
    "TaxonomyCandidate",
    "TaxonomyRegistry",
    "TaxonomyTerm",
    "ThreeMinuteRead",
]

_LAZY = LazyExports(
    __name__,
    {
        "EvidenceRef": ("backend.research.domain", "EvidenceRef"),
        "GateResult": ("backend.research.domain", "GateResult"),
        "QualityFlag": ("backend.research.domain", "QualityFlag"),
        "READER_REPAIR_NAMESPACE": ("backend.research.domain", "READER_REPAIR_NAMESPACE"),
        "ReaderIssue": ("backend.research.domain", "ReaderIssue"),
        "ReaderIssueSignature": ("backend.research.domain", "ReaderIssueSignature"),
        "ReaderRepairAttempt": ("backend.research.domain", "ReaderRepairAttempt"),
        "ReaderRepairCandidate": ("backend.research.domain", "ReaderRepairCandidate"),
        "ReaderRepairCase": ("backend.research.domain", "ReaderRepairCase"),
        "ReaderRepairContextPack": ("backend.research.domain", "ReaderRepairContextPack"),
        "ReaderRepairMemoryQuery": ("backend.research.domain", "ReaderRepairMemoryQuery"),
        "ReaderRepairRAGPolicy": ("backend.research.domain", "ReaderRepairRAGPolicy"),
        "ReaderRepairResult": ("backend.research.domain", "ReaderRepairResult"),
        "ReaderRepairSkillCandidateSeed": ("backend.research.domain", "ReaderRepairSkillCandidateSeed"),
        "ReaderRepairStrategy": ("backend.research.domain", "ReaderRepairStrategy"),
        "ResearchAnalysis": ("backend.research.domain", "ResearchAnalysis"),
        "ResearchClaim": ("backend.research.domain", "ResearchClaim"),
        "ResearchDocument": ("backend.research.domain", "ResearchDocument"),
        "ResearchEvidenceItem": ("backend.research.domain", "ResearchEvidenceItem"),
        "ResearchEvidencePack": ("backend.research.domain", "ResearchEvidencePack"),
        "ResearchPaper": ("backend.research.domain", "ResearchPaper"),
        "ResearchQualityResult": ("backend.research.domain", "ResearchQualityResult"),
        "ResearchReaderPayload": ("backend.research.domain", "ResearchReaderPayload"),
        "ResearchSection": ("backend.research.domain", "ResearchSection"),
        "SourceLineage": ("backend.research.domain", "SourceLineage"),
        "ThreeMinuteRead": ("backend.research.domain", "ThreeMinuteRead"),
        "PaperCardBuilder": ("backend.research.paper_card", "PaperCardBuilder"),
        "ResearchPaperCard": ("backend.research.paper_card", "ResearchPaperCard"),
        "TaxonomyAssignment": ("backend.research.taxonomy", "TaxonomyAssignment"),
        "TaxonomyCandidate": ("backend.research.taxonomy", "TaxonomyCandidate"),
        "TaxonomyRegistry": ("backend.research.taxonomy", "TaxonomyRegistry"),
        "TaxonomyTerm": ("backend.research.taxonomy", "TaxonomyTerm"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.domain import (
        EvidenceRef,
        GateResult,
        QualityFlag,
        READER_REPAIR_NAMESPACE,
        ReaderIssue,
        ReaderIssueSignature,
        ReaderRepairAttempt,
        ReaderRepairCandidate,
        ReaderRepairCase,
        ReaderRepairContextPack,
        ReaderRepairMemoryQuery,
        ReaderRepairRAGPolicy,
        ReaderRepairResult,
        ReaderRepairSkillCandidateSeed,
        ReaderRepairStrategy,
        ResearchAnalysis,
        ResearchClaim,
        ResearchDocument,
        ResearchEvidenceItem,
        ResearchEvidencePack,
        ResearchPaper,
        ResearchQualityResult,
        ResearchReaderPayload,
        ResearchSection,
        SourceLineage,
        ThreeMinuteRead,
    )
    from backend.research.paper_card import (
        PaperCardBuilder,
        ResearchPaperCard,
    )
    from backend.research.taxonomy import (
        TaxonomyAssignment,
        TaxonomyCandidate,
        TaxonomyRegistry,
        TaxonomyTerm,
    )
