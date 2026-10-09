from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.rag.retrieval import dedupe_by_key

from backend.research.document.models import PaperChunk
from backend.research.rag.retrieval.paper_policy import QueryIntent


@dataclass
class RetrievalRequest:
    paper_id: str
    question: str
    current_section_index: int = 0
    limit: int = 10
    filters: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    parent_chunks: list[PaperChunk]
    child_chunks: list[PaperChunk]
    ref_chunks: list[PaperChunk]
    intent: QueryIntent
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_evidence_candidates(self) -> list[dict[str, Any]]:
        """Minimal dict representation for EvidenceCandidate construction."""

        out = []
        for chunk in _dedupe_chunks([*self.child_chunks, *self.ref_chunks, *self.parent_chunks]):
            source_ref = chunk.metadata.get("source_ref", f"arxiv://{chunk.paper_id}")
            out.append({
                "evidence_id": chunk.chunk_id,
                "title": chunk.section_title or chunk.chunk_type,
                "summary": chunk.content[:1200],
                "source_ref": source_ref,
                "span_refs": (chunk.chunk_id,),
                "evidence_type": chunk.chunk_type,
                "lineage": (chunk.paper_id,),
                "metadata": {
                    "section_role": chunk.section_role,
                    "section_index": chunk.section_index,
                    "has_formula": chunk.has_formula,
                    "has_figure": chunk.has_figure,
                    "intent": self.intent,
                    "expansion_reason": chunk.metadata.get("expansion_reason", ""),
                    "expanded_from_chunk_id": chunk.metadata.get("expanded_from_chunk_id", ""),
                    "expansion_edge": chunk.metadata.get("expansion_edge", ""),
                    "table_context_rerank_score": chunk.metadata.get("table_context_rerank_score"),
                    "table_context_rerank_strategy": chunk.metadata.get("table_context_rerank_strategy", ""),
                    "parent_expansion_reason": chunk.metadata.get("parent_expansion_reason", ""),
                    "parent_anchor_child_id": chunk.metadata.get("parent_anchor_child_id", ""),
                    "parent_snippet": chunk.metadata.get("parent_snippet", False),
                    "source_parent_chunk_id": chunk.metadata.get("source_parent_chunk_id", ""),
                    "parent_rerank_score": chunk.metadata.get("parent_rerank_score"),
                    "parent_rerank_strategy": chunk.metadata.get("parent_rerank_strategy", ""),
                    "parent_child_relevance_score": chunk.metadata.get("parent_child_relevance_score"),
                    "parent_relevance_score": chunk.metadata.get("parent_relevance_score"),
                    "parent_section_heading_score": chunk.metadata.get("parent_section_heading_score"),
                    "parent_position_score": chunk.metadata.get("parent_position_score"),
                    "parent_final_score": chunk.metadata.get("parent_final_score"),
                    "parent_score_strategy": chunk.metadata.get("parent_score_strategy", ""),
                    "parent_score_weights": chunk.metadata.get("parent_score_weights", {}),
                    "claim_index_hit": chunk.metadata.get("claim_index_hit", False),
                    "claim_index_score": chunk.metadata.get("claim_index_score"),
                    "claim_id": chunk.metadata.get("claim_id", ""),
                    "claim_text": chunk.metadata.get("claim_text", ""),
                    "claim_type": chunk.metadata.get("claim_type", ""),
                    "claim_source_locator": chunk.metadata.get("claim_source_locator", ""),
                    "title_score": chunk.metadata.get("title_score"),
                    "abstract_score": chunk.metadata.get("abstract_score"),
                    "caption_score": chunk.metadata.get("caption_score"),
                    "equation_score": chunk.metadata.get("equation_score"),
                    "body_score": chunk.metadata.get("body_score"),
                    "field_score": chunk.metadata.get("field_score"),
                    "field_score_weights": chunk.metadata.get("field_score_weights", {}),
                    "field_score_strategy": chunk.metadata.get("field_score_strategy", ""),
                    "title_embedding_score": chunk.metadata.get("title_embedding_score"),
                    "abstract_embedding_score": chunk.metadata.get("abstract_embedding_score"),
                    "caption_embedding_score": chunk.metadata.get("caption_embedding_score"),
                    "equation_embedding_score": chunk.metadata.get("equation_embedding_score"),
                    "body_embedding_score": chunk.metadata.get("body_embedding_score"),
                    "field_embedding_score": chunk.metadata.get("field_embedding_score"),
                    "field_embedding_scores": chunk.metadata.get("field_embedding_scores", {}),
                    "field_embedding_hits": chunk.metadata.get("field_embedding_hits", []),
                    "best_embedding_field": chunk.metadata.get("best_embedding_field", ""),
                    "field_embedding_strategy": chunk.metadata.get("field_embedding_strategy", ""),
                    "field_rerank_score": chunk.metadata.get("field_rerank_score"),
                    "field_rerank_strategy": chunk.metadata.get("field_rerank_strategy", ""),
                    "best_matching_field": chunk.metadata.get("best_matching_field", ""),
                    "element_label_score": chunk.metadata.get("element_label_score"),
                    "element_label_match": chunk.metadata.get("element_label_match", False),
                    "formula_symbol_score": chunk.metadata.get("formula_symbol_score"),
                    "formula_operator_score": chunk.metadata.get("formula_operator_score"),
                    "formula_label_score": chunk.metadata.get("formula_label_score"),
                    "formula_structure_score": chunk.metadata.get("formula_structure_score"),
                    "formula_context_score": chunk.metadata.get("formula_context_score"),
                    "formula_sparse_score": chunk.metadata.get("formula_sparse_score"),
                    "formula_sparse_boost": chunk.metadata.get("formula_sparse_boost"),
                    "formula_sparse_strategy": chunk.metadata.get("formula_sparse_strategy", ""),
                    "graph_score": chunk.metadata.get("graph_score"),
                    "child_score_strategy": chunk.metadata.get("child_score_strategy", ""),
                    "child_score_components": chunk.metadata.get("child_score_components", {}),
                    "field_text_available_fields": chunk.metadata.get("field_text_available_fields", ()),
                    "field_text_sources": chunk.metadata.get("field_text_sources", {}),
                    "content_span_unit": chunk.metadata.get("content_span_unit", ""),
                    "main_span": chunk.metadata.get("main_span", {}),
                    "overlap_spans": chunk.metadata.get("overlap_spans", []),
                    "child_semantic_score": chunk.metadata.get("child_semantic_score"),
                    "child_position_score": chunk.metadata.get("child_position_score"),
                    "child_final_score": chunk.metadata.get("child_final_score"),
                    "child_score_weights": chunk.metadata.get("child_score_weights", {}),
                },
            })
        return out


def _dedupe_chunks(chunks: list[PaperChunk]) -> list[PaperChunk]:
    return dedupe_by_key(chunks, key=lambda chunk: chunk.chunk_id)


__all__ = ["RetrievalRequest", "RetrievalResult"]
