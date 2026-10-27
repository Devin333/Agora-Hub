from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "InMemoryReaderRepairMemory",
    "READER_REPAIR_APPLICATION_CHECK_IDS",
    "READER_REPAIR_APPLICATION_SCHEMA",
    "READER_REPAIR_APPLICATION_VERIFICATION_SCHEMA",
    "READER_REPAIR_MAX_PATCH_OPERATIONS",
    "READER_REPAIR_NAMESPACE",
    "ReaderIssue",
    "ReaderIssueSignature",
    "ReaderRepairApplicationCandidate",
    "ReaderRepairApplicationCheck",
    "ReaderRepairApplicationError",
    "ReaderRepairApplicationObservationCandidate",
    "ReaderRepairApplicationVerificationRecord",
    "ReaderRepairAttempt",
    "ReaderRepairCandidate",
    "ReaderRepairCase",
    "ReaderRepairConsolidator",
    "ReaderRepairContextBuilder",
    "ReaderRepairContextPack",
    "ReaderRepairGateSuite",
    "ReaderRepairIssueDetector",
    "ReaderRepairMemoryQuery",
    "ReaderRepairMemoryService",
    "ReaderRepairPatchCandidate",
    "ReaderRepairPatchOperation",
    "ReaderRepairPreconditionError",
    "ReaderRepairRAGPolicy",
    "ReaderRepairRemoveAnalysisOperation",
    "ReaderRepairRemoveEvidenceOperation",
    "ReaderRepairReplaceAnalysisOperation",
    "ReaderRepairReplaceAnnotationsOperation",
    "ReaderRepairReplaceDocumentOperation",
    "ReaderRepairReplaceEvidenceOperation",
    "ReaderRepairReplaceNavigationOperation",
    "ReaderRepairReplaceQualityOperation",
    "ReaderRepairReplaceSourceLineageOperation",
    "ReaderRepairResult",
    "ReaderRepairService",
    "ReaderRepairSkillCandidateSeed",
    "ReaderRepairStrategy",
    "build_reader_issue_signature",
    "build_reader_repair_subagent_specs",
    "apply_reader_repair_candidate",
    "reader_repair_component_checksum",
    "verify_reader_repair_application",
]

_LAZY = LazyExports(
    __name__,
    {
        "READER_REPAIR_APPLICATION_CHECK_IDS": ("backend.research.domain.reader_repair", "READER_REPAIR_APPLICATION_CHECK_IDS"),
        "READER_REPAIR_APPLICATION_SCHEMA": ("backend.research.domain.reader_repair", "READER_REPAIR_APPLICATION_SCHEMA"),
        "READER_REPAIR_APPLICATION_VERIFICATION_SCHEMA": ("backend.research.domain.reader_repair", "READER_REPAIR_APPLICATION_VERIFICATION_SCHEMA"),
        "READER_REPAIR_MAX_PATCH_OPERATIONS": ("backend.research.domain.reader_repair", "READER_REPAIR_MAX_PATCH_OPERATIONS"),
        "READER_REPAIR_NAMESPACE": ("backend.research.domain.reader_repair", "READER_REPAIR_NAMESPACE"),
        "ReaderIssue": ("backend.research.domain.reader_repair", "ReaderIssue"),
        "ReaderIssueSignature": ("backend.research.domain.reader_repair", "ReaderIssueSignature"),
        "ReaderRepairApplicationCandidate": ("backend.research.domain.reader_repair", "ReaderRepairApplicationCandidate"),
        "ReaderRepairApplicationCheck": ("backend.research.domain.reader_repair", "ReaderRepairApplicationCheck"),
        "ReaderRepairApplicationObservationCandidate": ("backend.research.domain.reader_repair", "ReaderRepairApplicationObservationCandidate"),
        "ReaderRepairApplicationVerificationRecord": ("backend.research.domain.reader_repair", "ReaderRepairApplicationVerificationRecord"),
        "ReaderRepairAttempt": ("backend.research.domain.reader_repair", "ReaderRepairAttempt"),
        "ReaderRepairCandidate": ("backend.research.domain.reader_repair", "ReaderRepairCandidate"),
        "ReaderRepairCase": ("backend.research.domain.reader_repair", "ReaderRepairCase"),
        "ReaderRepairContextPack": ("backend.research.domain.reader_repair", "ReaderRepairContextPack"),
        "ReaderRepairMemoryQuery": ("backend.research.domain.reader_repair", "ReaderRepairMemoryQuery"),
        "ReaderRepairPatchCandidate": ("backend.research.domain.reader_repair", "ReaderRepairPatchCandidate"),
        "ReaderRepairPatchOperation": ("backend.research.domain.reader_repair", "ReaderRepairPatchOperation"),
        "ReaderRepairRAGPolicy": ("backend.research.domain.reader_repair", "ReaderRepairRAGPolicy"),
        "ReaderRepairRemoveAnalysisOperation": ("backend.research.domain.reader_repair", "ReaderRepairRemoveAnalysisOperation"),
        "ReaderRepairRemoveEvidenceOperation": ("backend.research.domain.reader_repair", "ReaderRepairRemoveEvidenceOperation"),
        "ReaderRepairReplaceAnalysisOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceAnalysisOperation"),
        "ReaderRepairReplaceAnnotationsOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceAnnotationsOperation"),
        "ReaderRepairReplaceDocumentOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceDocumentOperation"),
        "ReaderRepairReplaceEvidenceOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceEvidenceOperation"),
        "ReaderRepairReplaceNavigationOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceNavigationOperation"),
        "ReaderRepairReplaceQualityOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceQualityOperation"),
        "ReaderRepairReplaceSourceLineageOperation": ("backend.research.domain.reader_repair", "ReaderRepairReplaceSourceLineageOperation"),
        "ReaderRepairResult": ("backend.research.domain.reader_repair", "ReaderRepairResult"),
        "ReaderRepairSkillCandidateSeed": ("backend.research.domain.reader_repair", "ReaderRepairSkillCandidateSeed"),
        "ReaderRepairStrategy": ("backend.research.domain.reader_repair", "ReaderRepairStrategy"),
        "ReaderRepairApplicationError": ("backend.research.reader_repair.application", "ReaderRepairApplicationError"),
        "apply_reader_repair_candidate": ("backend.research.reader_repair.application", "apply_reader_repair_candidate"),
        "reader_repair_component_checksum": ("backend.research.reader_repair.application", "reader_repair_component_checksum"),
        "verify_reader_repair_application": ("backend.research.reader_repair.application", "verify_reader_repair_application"),
        "ReaderRepairConsolidator": ("backend.research.reader_repair.consolidation", "ReaderRepairConsolidator"),
        "ReaderRepairIssueDetector": ("backend.research.reader_repair.issue_detector", "ReaderRepairIssueDetector"),
        "build_reader_issue_signature": ("backend.research.reader_repair.issue_signature", "build_reader_issue_signature"),
        "ReaderRepairContextBuilder": ("backend.research.reader_repair.repair_context", "ReaderRepairContextBuilder"),
        "ReaderRepairGateSuite": ("backend.research.reader_repair.repair_gates", "ReaderRepairGateSuite"),
        "InMemoryReaderRepairMemory": ("backend.research.reader_repair.repair_memory", "InMemoryReaderRepairMemory"),
        "ReaderRepairMemoryService": ("backend.research.reader_repair.repair_memory", "ReaderRepairMemoryService"),
        "ReaderRepairPreconditionError": ("backend.research.reader_repair.repair_service", "ReaderRepairPreconditionError"),
        "ReaderRepairService": ("backend.research.reader_repair.repair_service", "ReaderRepairService"),
        "build_reader_repair_subagent_specs": ("backend.research.graphs.reader_repair", "build_reader_repair_subagent_specs"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.domain.reader_repair import (
        READER_REPAIR_APPLICATION_CHECK_IDS,
        READER_REPAIR_APPLICATION_SCHEMA,
        READER_REPAIR_APPLICATION_VERIFICATION_SCHEMA,
        READER_REPAIR_MAX_PATCH_OPERATIONS,
        READER_REPAIR_NAMESPACE,
        ReaderIssue,
        ReaderIssueSignature,
        ReaderRepairApplicationCandidate,
        ReaderRepairApplicationCheck,
        ReaderRepairApplicationObservationCandidate,
        ReaderRepairApplicationVerificationRecord,
        ReaderRepairAttempt,
        ReaderRepairCandidate,
        ReaderRepairCase,
        ReaderRepairContextPack,
        ReaderRepairMemoryQuery,
        ReaderRepairPatchCandidate,
        ReaderRepairPatchOperation,
        ReaderRepairRAGPolicy,
        ReaderRepairRemoveAnalysisOperation,
        ReaderRepairRemoveEvidenceOperation,
        ReaderRepairReplaceAnalysisOperation,
        ReaderRepairReplaceAnnotationsOperation,
        ReaderRepairReplaceDocumentOperation,
        ReaderRepairReplaceEvidenceOperation,
        ReaderRepairReplaceNavigationOperation,
        ReaderRepairReplaceQualityOperation,
        ReaderRepairReplaceSourceLineageOperation,
        ReaderRepairResult,
        ReaderRepairSkillCandidateSeed,
        ReaderRepairStrategy,
    )
    from backend.research.reader_repair.application import (
        ReaderRepairApplicationError,
        apply_reader_repair_candidate,
        reader_repair_component_checksum,
        verify_reader_repair_application,
    )
    from backend.research.reader_repair.consolidation import (
        ReaderRepairConsolidator,
    )
    from backend.research.reader_repair.issue_detector import (
        ReaderRepairIssueDetector,
    )
    from backend.research.reader_repair.issue_signature import (
        build_reader_issue_signature,
    )
    from backend.research.reader_repair.repair_context import (
        ReaderRepairContextBuilder,
    )
    from backend.research.reader_repair.repair_gates import (
        ReaderRepairGateSuite,
    )
    from backend.research.reader_repair.repair_memory import (
        InMemoryReaderRepairMemory,
        ReaderRepairMemoryService,
    )
    from backend.research.reader_repair.repair_service import (
        ReaderRepairPreconditionError,
        ReaderRepairService,
    )
    from backend.research.graphs.reader_repair import (
        build_reader_repair_subagent_specs,
    )
