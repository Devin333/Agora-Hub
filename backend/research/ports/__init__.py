from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "DocumentCompilerPort",
    "ResearchDocumentRepository",
    "ResearchEvidencePackRepository",
    "ResearchEventSink",
    "ResearchPaperReadRepository",
    "ResolvedPaperSource",
    "ResearchSourceResolver",
    "CatalogRepository",
    "PaperIdentityRepository",
    "ResearchPaperCatalogRepository",
    "ResearchPaperCatalogQueryPort",
    "ResearchCodeRepositoryProfileRepository",
    "ResearchSOTAClaimRepository",
    "ResearchPaperIdentityRepository",
    "ResearchPaperRelationRepository",
    "ResearchSourceSnapshotRepository",
    "SourceSnapshotRepository",
    "EvidencePackRepository",
    "FieldEmbeddingHit",
    "FieldEmbeddingIndexerPort",
    "FieldEmbeddingIndexPort",
    "FieldEmbeddingSearchPort",
    "GithubRepositoryPort",
    "PaperCardRepository",
    "BoundPaperSource",
    "BoundPaperSourceProvider",
    "PaperSourceProvider",
    "READER_REPAIR_FAILURE_DIAGNOSTIC_EFFECT_KIND",
    "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_ID",
    "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_REF",
    "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_VERSION",
    "READER_REPAIR_FAILURE_DIAGNOSTIC_SCHEMA_VERSION",
    "READER_REPAIR_FAILURE_DIAGNOSTIC_TERMINAL_ACTION",
    "READER_REPAIR_MEMORY_EFFECT_KIND",
    "READER_REPAIR_MEMORY_HANDLER_ID",
    "READER_REPAIR_MEMORY_HANDLER_REF",
    "READER_REPAIR_MEMORY_HANDLER_VERSION",
    "READER_REPAIR_MEMORY_SCHEMA_VERSION",
    "READER_REPAIR_MEMORY_STEP_ID",
    "ReaderRepairFailureDiagnosticCandidate",
    "ReaderRepairFailureDiagnosticCommitPort",
    "ReaderRepairFailureDiagnosticCommitReceipt",
    "ReaderRepairFailureDiagnosticCommitRequest",
    "ReaderRepairMemoryCandidateProjection",
    "ReaderRepairMemoryCommitPort",
    "ReaderRepairMemoryCommitReceipt",
    "ReaderRepairMemoryCommitRequest",
    "ReaderRepairMemoryPort",
    "ReaderRepairMemoryRecallPort",
    "ReaderRepairMemoryVersion",
    "ReadingSessionRepository",
    "ResearchArtifactStorePort",
    "ResearchArtifactReadPort",
    "ResearchArtifactDiagnosticClaim",
    "ResearchArtifactDiagnosticReader",
    "ResearchArtifactReadClaim",
    "ResearchArtifactReadResolution",
    "ResearchGraphStorageIndexPublisherPort",
    "ResearchCandidateWorkerPort",
    "ResearchMemoryPort",
    "ResearchPaperRepository",
    "ResearchRetrievalProjectionPort",
    "ResearchRunDiagnosticStore",
    "ResearchRunDisposition",
    "ResearchRunDispositionReason",
    "ResearchRunRecord",
    "ResearchRunStore",
    "ResearchRunStoreConflictError",
    "ResearchRunStoreCorruptionError",
    "ResearchRunStoreError",
    "ResearchRunStoreReason",
    "ResearchRunStoreUnavailableError",
    "ResearchRunStoreValidationError",
]

_LAZY = LazyExports(
    __name__,
    {
        "GithubRepositoryPort": ("backend.research.code_repository.ports", "GithubRepositoryPort"),
        "ResearchArtifactDiagnosticClaim": ("backend.research.ports.artifact_publication", "ResearchArtifactDiagnosticClaim"),
        "ResearchArtifactDiagnosticReader": ("backend.research.ports.artifact_publication", "ResearchArtifactDiagnosticReader"),
        "ResearchArtifactReadClaim": ("backend.research.ports.artifact_publication", "ResearchArtifactReadClaim"),
        "ResearchArtifactReadResolution": ("backend.research.ports.artifact_publication", "ResearchArtifactReadResolution"),
        "ResearchGraphStorageIndexPublisherPort": ("backend.research.ports.artifact_publication", "ResearchGraphStorageIndexPublisherPort"),
        "ResearchArtifactReadPort": ("backend.research.ports.artifact_store", "ResearchArtifactReadPort"),
        "ResearchArtifactStorePort": ("backend.research.ports.artifact_store", "ResearchArtifactStorePort"),
        "CatalogRepository": ("backend.research.ports.catalog", "CatalogRepository"),
        "PaperIdentityRepository": ("backend.research.ports.catalog", "PaperIdentityRepository"),
        "ResearchPaperCatalogRepository": ("backend.research.ports.catalog", "ResearchPaperCatalogRepository"),
        "ResearchPaperCatalogQueryPort": ("backend.research.ports.catalog", "ResearchPaperCatalogQueryPort"),
        "ResearchCodeRepositoryProfileRepository": ("backend.research.ports.catalog", "ResearchCodeRepositoryProfileRepository"),
        "ResearchSOTAClaimRepository": ("backend.research.ports.catalog", "ResearchSOTAClaimRepository"),
        "ResearchPaperIdentityRepository": ("backend.research.ports.catalog", "ResearchPaperIdentityRepository"),
        "ResearchPaperRelationRepository": ("backend.research.ports.catalog", "ResearchPaperRelationRepository"),
        "ResearchSourceSnapshotRepository": ("backend.research.ports.catalog", "ResearchSourceSnapshotRepository"),
        "SourceSnapshotRepository": ("backend.research.ports.catalog", "SourceSnapshotRepository"),
        "DocumentCompilerPort": ("backend.research.ports.document_compiler", "DocumentCompilerPort"),
        "ResearchDocumentRepository": ("backend.research.ports.paper_ingest", "ResearchDocumentRepository"),
        "ResearchEvidencePackRepository": ("backend.research.ports.paper_ingest", "ResearchEvidencePackRepository"),
        "ResearchEventSink": ("backend.research.ports.paper_ingest", "ResearchEventSink"),
        "ResearchPaperReadRepository": ("backend.research.ports.paper_ingest", "ResearchPaperReadRepository"),
        "ResolvedPaperSource": ("backend.research.ports.source_resolver", "ResolvedPaperSource"),
        "ResearchSourceResolver": ("backend.research.ports.source_resolver", "ResearchSourceResolver"),
        "FieldEmbeddingHit": ("backend.research.ports.field_embedding_index", "FieldEmbeddingHit"),
        "FieldEmbeddingIndexerPort": ("backend.research.ports.field_embedding_index", "FieldEmbeddingIndexerPort"),
        "FieldEmbeddingIndexPort": ("backend.research.ports.field_embedding_index", "FieldEmbeddingIndexPort"),
        "FieldEmbeddingSearchPort": ("backend.research.ports.field_embedding_index", "FieldEmbeddingSearchPort"),
        "ResearchCandidateWorkerPort": ("backend.research.ports.llm_worker", "ResearchCandidateWorkerPort"),
        "ResearchMemoryPort": ("backend.research.ports.memory", "ResearchMemoryPort"),
        "READER_REPAIR_FAILURE_DIAGNOSTIC_EFFECT_KIND": ("backend.research.ports.reader_repair_failure_diagnostic", "READER_REPAIR_FAILURE_DIAGNOSTIC_EFFECT_KIND"),
        "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_ID": ("backend.research.ports.reader_repair_failure_diagnostic", "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_ID"),
        "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_REF": ("backend.research.ports.reader_repair_failure_diagnostic", "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_REF"),
        "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_VERSION": ("backend.research.ports.reader_repair_failure_diagnostic", "READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_VERSION"),
        "READER_REPAIR_FAILURE_DIAGNOSTIC_SCHEMA_VERSION": ("backend.research.ports.reader_repair_failure_diagnostic", "READER_REPAIR_FAILURE_DIAGNOSTIC_SCHEMA_VERSION"),
        "READER_REPAIR_FAILURE_DIAGNOSTIC_TERMINAL_ACTION": ("backend.research.ports.reader_repair_failure_diagnostic", "READER_REPAIR_FAILURE_DIAGNOSTIC_TERMINAL_ACTION"),
        "ReaderRepairFailureDiagnosticCandidate": ("backend.research.ports.reader_repair_failure_diagnostic", "ReaderRepairFailureDiagnosticCandidate"),
        "ReaderRepairFailureDiagnosticCommitPort": ("backend.research.ports.reader_repair_failure_diagnostic", "ReaderRepairFailureDiagnosticCommitPort"),
        "ReaderRepairFailureDiagnosticCommitReceipt": ("backend.research.ports.reader_repair_failure_diagnostic", "ReaderRepairFailureDiagnosticCommitReceipt"),
        "ReaderRepairFailureDiagnosticCommitRequest": ("backend.research.ports.reader_repair_failure_diagnostic", "ReaderRepairFailureDiagnosticCommitRequest"),
        "READER_REPAIR_MEMORY_EFFECT_KIND": ("backend.research.ports.repair_memory", "READER_REPAIR_MEMORY_EFFECT_KIND"),
        "READER_REPAIR_MEMORY_HANDLER_ID": ("backend.research.ports.repair_memory", "READER_REPAIR_MEMORY_HANDLER_ID"),
        "READER_REPAIR_MEMORY_HANDLER_REF": ("backend.research.ports.repair_memory", "READER_REPAIR_MEMORY_HANDLER_REF"),
        "READER_REPAIR_MEMORY_HANDLER_VERSION": ("backend.research.ports.repair_memory", "READER_REPAIR_MEMORY_HANDLER_VERSION"),
        "READER_REPAIR_MEMORY_SCHEMA_VERSION": ("backend.research.ports.repair_memory", "READER_REPAIR_MEMORY_SCHEMA_VERSION"),
        "READER_REPAIR_MEMORY_STEP_ID": ("backend.research.ports.repair_memory", "READER_REPAIR_MEMORY_STEP_ID"),
        "ReaderRepairMemoryCandidateProjection": ("backend.research.ports.repair_memory", "ReaderRepairMemoryCandidateProjection"),
        "ReaderRepairMemoryCommitPort": ("backend.research.ports.repair_memory", "ReaderRepairMemoryCommitPort"),
        "ReaderRepairMemoryCommitReceipt": ("backend.research.ports.repair_memory", "ReaderRepairMemoryCommitReceipt"),
        "ReaderRepairMemoryCommitRequest": ("backend.research.ports.repair_memory", "ReaderRepairMemoryCommitRequest"),
        "ReaderRepairMemoryPort": ("backend.research.ports.repair_memory", "ReaderRepairMemoryPort"),
        "ReaderRepairMemoryRecallPort": ("backend.research.ports.repair_memory", "ReaderRepairMemoryRecallPort"),
        "ReaderRepairMemoryVersion": ("backend.research.ports.repair_memory", "ReaderRepairMemoryVersion"),
        "EvidencePackRepository": ("backend.research.ports.repositories", "EvidencePackRepository"),
        "PaperCardRepository": ("backend.research.ports.repositories", "PaperCardRepository"),
        "ReadingSessionRepository": ("backend.research.ports.repositories", "ReadingSessionRepository"),
        "ResearchPaperRepository": ("backend.research.ports.repositories", "ResearchPaperRepository"),
        "ResearchRetrievalProjectionPort": ("backend.research.ports.retrieval", "ResearchRetrievalProjectionPort"),
        "ResearchRunDiagnosticStore": ("backend.research.ports.run_store", "ResearchRunDiagnosticStore"),
        "ResearchRunDisposition": ("backend.research.ports.run_store", "ResearchRunDisposition"),
        "ResearchRunDispositionReason": ("backend.research.ports.run_store", "ResearchRunDispositionReason"),
        "ResearchRunRecord": ("backend.research.ports.run_store", "ResearchRunRecord"),
        "ResearchRunStore": ("backend.research.ports.run_store", "ResearchRunStore"),
        "ResearchRunStoreConflictError": ("backend.research.ports.run_store", "ResearchRunStoreConflictError"),
        "ResearchRunStoreCorruptionError": ("backend.research.ports.run_store", "ResearchRunStoreCorruptionError"),
        "ResearchRunStoreError": ("backend.research.ports.run_store", "ResearchRunStoreError"),
        "ResearchRunStoreReason": ("backend.research.ports.run_store", "ResearchRunStoreReason"),
        "ResearchRunStoreUnavailableError": ("backend.research.ports.run_store", "ResearchRunStoreUnavailableError"),
        "ResearchRunStoreValidationError": ("backend.research.ports.run_store", "ResearchRunStoreValidationError"),
        "BoundPaperSource": ("backend.research.ports.source_provider", "BoundPaperSource"),
        "BoundPaperSourceProvider": ("backend.research.ports.source_provider", "BoundPaperSourceProvider"),
        "PaperSourceProvider": ("backend.research.ports.source_provider", "PaperSourceProvider"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.code_repository.ports import (
        GithubRepositoryPort,
    )
    from backend.research.ports.artifact_publication import (
        ResearchArtifactDiagnosticClaim,
        ResearchArtifactDiagnosticReader,
        ResearchArtifactReadClaim,
        ResearchArtifactReadResolution,
        ResearchGraphStorageIndexPublisherPort,
    )
    from backend.research.ports.artifact_store import (
        ResearchArtifactReadPort,
        ResearchArtifactStorePort,
    )
    from backend.research.ports.catalog import (
        CatalogRepository,
        PaperIdentityRepository,
        ResearchPaperCatalogRepository,
        ResearchPaperCatalogQueryPort,
        ResearchCodeRepositoryProfileRepository,
        ResearchSOTAClaimRepository,
        ResearchPaperIdentityRepository,
        ResearchPaperRelationRepository,
        ResearchSourceSnapshotRepository,
        SourceSnapshotRepository,
    )
    from backend.research.ports.document_compiler import (
        DocumentCompilerPort,
    )
    from backend.research.ports.paper_ingest import (
        ResearchDocumentRepository,
        ResearchEvidencePackRepository,
        ResearchEventSink,
        ResearchPaperReadRepository,
    )
    from backend.research.ports.source_resolver import (
        ResolvedPaperSource,
        ResearchSourceResolver,
    )
    from backend.research.ports.field_embedding_index import (
        FieldEmbeddingHit,
        FieldEmbeddingIndexerPort,
        FieldEmbeddingIndexPort,
        FieldEmbeddingSearchPort,
    )
    from backend.research.ports.llm_worker import (
        ResearchCandidateWorkerPort,
    )
    from backend.research.ports.memory import (
        ResearchMemoryPort,
    )
    from backend.research.ports.reader_repair_failure_diagnostic import (
        READER_REPAIR_FAILURE_DIAGNOSTIC_EFFECT_KIND,
        READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_ID,
        READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_REF,
        READER_REPAIR_FAILURE_DIAGNOSTIC_HANDLER_VERSION,
        READER_REPAIR_FAILURE_DIAGNOSTIC_SCHEMA_VERSION,
        READER_REPAIR_FAILURE_DIAGNOSTIC_TERMINAL_ACTION,
        ReaderRepairFailureDiagnosticCandidate,
        ReaderRepairFailureDiagnosticCommitPort,
        ReaderRepairFailureDiagnosticCommitReceipt,
        ReaderRepairFailureDiagnosticCommitRequest,
    )
    from backend.research.ports.repair_memory import (
        READER_REPAIR_MEMORY_EFFECT_KIND,
        READER_REPAIR_MEMORY_HANDLER_ID,
        READER_REPAIR_MEMORY_HANDLER_REF,
        READER_REPAIR_MEMORY_HANDLER_VERSION,
        READER_REPAIR_MEMORY_SCHEMA_VERSION,
        READER_REPAIR_MEMORY_STEP_ID,
        ReaderRepairMemoryCandidateProjection,
        ReaderRepairMemoryCommitPort,
        ReaderRepairMemoryCommitReceipt,
        ReaderRepairMemoryCommitRequest,
        ReaderRepairMemoryPort,
        ReaderRepairMemoryRecallPort,
        ReaderRepairMemoryVersion,
    )
    from backend.research.ports.repositories import (
        EvidencePackRepository,
        PaperCardRepository,
        ReadingSessionRepository,
        ResearchPaperRepository,
    )
    from backend.research.ports.retrieval import (
        ResearchRetrievalProjectionPort,
    )
    from backend.research.ports.run_store import (
        ResearchRunDiagnosticStore,
        ResearchRunDisposition,
        ResearchRunDispositionReason,
        ResearchRunRecord,
        ResearchRunStore,
        ResearchRunStoreConflictError,
        ResearchRunStoreCorruptionError,
        ResearchRunStoreError,
        ResearchRunStoreReason,
        ResearchRunStoreUnavailableError,
        ResearchRunStoreValidationError,
    )
    from backend.research.ports.source_provider import (
        BoundPaperSource,
        BoundPaperSourceProvider,
        PaperSourceProvider,
    )
