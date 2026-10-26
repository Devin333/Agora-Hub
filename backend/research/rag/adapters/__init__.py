from __future__ import annotations

from backend.research.rag.adapters.answer_worker import PaperAnswerWorker
from backend.research.rag.adapters.llm_plan_worker import LLMResearchRAGPlanCandidateWorker
from backend.research.rag.adapters.memory_port import ResearchRAGMemoryPort
from backend.research.rag.adapters.paper_chunk_adapter import (
    PAPER_CHUNK_METADATA_KEY,
    PaperChunkAdapter,
    paper_chunk_evidence_score,
    paper_chunk_from_evidence_record,
    paper_chunk_to_evidence_metadata,
    paper_chunk_to_rag_chunk,
    paper_chunk_to_rag_evidence,
)
from backend.research.rag.adapters.paper_context_projection import paper_chunk_to_context_metadata
from backend.research.rag.adapters.paper_field_text import FIELD_NAMES, PaperChunkFieldText, extract_field_texts
from backend.research.rag.adapters.paper_source_locator import source_locator_from_paper_chunk
from backend.research.rag.adapters.plan_worker import ResearchRAGPlanWorker
from backend.research.rag.adapters.relevance_scorer import RerankerRelevanceScorer

__all__ = [
    "FIELD_NAMES",
    "PAPER_CHUNK_METADATA_KEY",
    "PaperChunkAdapter",
    "PaperChunkFieldText",
    "PaperAnswerWorker",
    "LLMResearchRAGPlanCandidateWorker",
    "ResearchRAGMemoryPort",
    "ResearchRAGPlanWorker",
    "RerankerRelevanceScorer",
    "paper_chunk_evidence_score",
    "paper_chunk_from_evidence_record",
    "paper_chunk_to_evidence_metadata",
    "paper_chunk_to_context_metadata",
    "paper_chunk_to_rag_chunk",
    "paper_chunk_to_rag_evidence",
    "extract_field_texts",
    "source_locator_from_paper_chunk",
]
