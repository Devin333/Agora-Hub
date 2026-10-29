from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "FIELD_NAMES",
    "PaperChunkAdapter",
    "PaperChunkFieldText",
    "PaperAnswerWorker",
    "LLMResearchRAGPlanCandidateWorker",
    "ResearchRAGMemoryPort",
    "ResearchRAGPlanWorker",
    "RerankerRelevanceScorer",
    "paper_chunk_to_evidence_metadata",
    "paper_chunk_to_context_metadata",
    "paper_chunk_to_rag_chunk",
    "paper_chunk_to_rag_evidence",
    "extract_field_texts",
    "source_locator_from_paper_chunk",
]

_LAZY = LazyExports(
    __name__,
    {
        "PaperAnswerWorker": ("backend.research.rag.adapters.answer_worker", "PaperAnswerWorker"),
        "LLMResearchRAGPlanCandidateWorker": ("backend.research.rag.adapters.llm_plan_worker", "LLMResearchRAGPlanCandidateWorker"),
        "ResearchRAGMemoryPort": ("backend.research.rag.adapters.memory_port", "ResearchRAGMemoryPort"),
        "PaperChunkAdapter": ("backend.research.rag.adapters.paper_chunk_adapter", "PaperChunkAdapter"),
        "paper_chunk_to_evidence_metadata": ("backend.research.rag.adapters.paper_chunk_adapter", "paper_chunk_to_evidence_metadata"),
        "paper_chunk_to_rag_chunk": ("backend.research.rag.adapters.paper_chunk_adapter", "paper_chunk_to_rag_chunk"),
        "paper_chunk_to_rag_evidence": ("backend.research.rag.adapters.paper_chunk_adapter", "paper_chunk_to_rag_evidence"),
        "paper_chunk_to_context_metadata": ("backend.research.rag.adapters.paper_context_projection", "paper_chunk_to_context_metadata"),
        "FIELD_NAMES": ("backend.research.rag.adapters.paper_field_text", "FIELD_NAMES"),
        "PaperChunkFieldText": ("backend.research.rag.adapters.paper_field_text", "PaperChunkFieldText"),
        "extract_field_texts": ("backend.research.rag.adapters.paper_field_text", "extract_field_texts"),
        "source_locator_from_paper_chunk": ("backend.research.rag.adapters.paper_source_locator", "source_locator_from_paper_chunk"),
        "ResearchRAGPlanWorker": ("backend.research.rag.adapters.plan_worker", "ResearchRAGPlanWorker"),
        "RerankerRelevanceScorer": ("backend.research.rag.adapters.relevance_scorer", "RerankerRelevanceScorer"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.rag.adapters.answer_worker import (
        PaperAnswerWorker,
    )
    from backend.research.rag.adapters.llm_plan_worker import (
        LLMResearchRAGPlanCandidateWorker,
    )
    from backend.research.rag.adapters.memory_port import (
        ResearchRAGMemoryPort,
    )
    from backend.research.rag.adapters.paper_chunk_adapter import (
        PaperChunkAdapter,
        paper_chunk_to_evidence_metadata,
        paper_chunk_to_rag_chunk,
        paper_chunk_to_rag_evidence,
    )
    from backend.research.rag.adapters.paper_context_projection import (
        paper_chunk_to_context_metadata,
    )
    from backend.research.rag.adapters.paper_field_text import (
        FIELD_NAMES,
        PaperChunkFieldText,
        extract_field_texts,
    )
    from backend.research.rag.adapters.paper_source_locator import (
        source_locator_from_paper_chunk,
    )
    from backend.research.rag.adapters.plan_worker import (
        ResearchRAGPlanWorker,
    )
    from backend.research.rag.adapters.relevance_scorer import (
        RerankerRelevanceScorer,
    )
