from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.rag.adapters.paper_context_projection import paper_chunk_to_context_metadata
from backend.research.rag.adapters.paper_field_text import extract_field_texts
from backend.research.rag.adapters.paper_source_locator import source_locator_from_paper_chunk
from framework.rag.core import RAGChunk, RAGEvidence, RAGScoreBreakdown, SourceLocator

# Evidence metadata key carrying the authoritative chunk record. Harness evidence
# summaries are bounded previews, so consumers that need the passage itself
# (answer generation) must rebuild it from this record, never from the summary.
PAPER_CHUNK_METADATA_KEY = "paper_chunk"

_SCORE_COMPONENT_KEYS: dict[str, tuple[str, ...]] = {
    "child_similarity": ("child_similarity", "child_semantic_score"),
    "parent_relevance": ("parent_relevance", "parent_relevance_score", "parent_child_relevance_score"),
    "field_score": ("field_score", "field_embedding_score"),
    "section_heading_score": ("section_heading_score", "parent_section_heading_score"),
    "position_bonus": ("position_bonus", "parent_position_score", "child_position_score"),
    "rerank_score": (
        "rerank_score",
        "field_rerank_score",
        "parent_rerank_score",
        "table_context_rerank_score",
    ),
    "final_score": ("final_score", "fused_score", "child_final_score", "parent_final_score"),
}

_SCORE_FALLBACK_KEYS = (
    "final_score",
    "fused_score",
    "child_final_score",
    "parent_final_score",
    "field_score",
    "field_embedding_score",
    "text_score",
)


class PaperChunkAdapter:
    def to_rag_chunk(self, chunk: PaperChunk) -> RAGChunk:
        fields = _chunk_fields(chunk)
        locator = source_locator_from_paper_chunk(chunk)
        return RAGChunk(
            chunk_id=chunk.chunk_id,
            document_id=chunk.paper_id,
            text=chunk.content,
            chunk_type=chunk.chunk_type,
            fields=fields,
            source_locator=locator,
            metadata=_chunk_metadata(chunk, locator),
        )

    def to_rag_evidence(self, chunk: PaperChunk, *, score: float | None = None) -> RAGEvidence:
        rag_chunk = self.to_rag_chunk(chunk)
        breakdown = _score_breakdown(chunk.metadata)
        resolved_score = score if score is not None else paper_chunk_evidence_score(chunk)
        return RAGEvidence(
            evidence_id=chunk.chunk_id,
            chunk_id=rag_chunk.chunk_id,
            document_id=rag_chunk.document_id,
            text=rag_chunk.text,
            score=resolved_score if resolved_score is not None else 0.0,
            score_breakdown=breakdown,
            source_locator=rag_chunk.source_locator,
            metadata={
                **dict(rag_chunk.metadata),
                **paper_chunk_to_context_metadata(chunk),
                PAPER_CHUNK_METADATA_KEY: chunk.model_dump(mode="json"),
            },
        )


def paper_chunk_evidence_score(chunk: PaperChunk) -> float | None:
    """Score the chunk carries into evidence ranking; None when it has no retrieval score."""
    return _first_number(chunk.metadata, _SCORE_FALLBACK_KEYS)


def paper_chunk_from_evidence_record(raw: Any) -> PaperChunk | None:
    """Rebuild the chunk stored under ``PAPER_CHUNK_METADATA_KEY``; None when absent or invalid."""
    if isinstance(raw, PaperChunk):
        return raw
    if not isinstance(raw, Mapping):
        return None
    try:
        return PaperChunk(**raw)
    except (TypeError, ValueError):
        return None


def paper_chunk_to_rag_chunk(chunk: PaperChunk) -> RAGChunk:
    return PaperChunkAdapter().to_rag_chunk(chunk)


def paper_chunk_to_rag_evidence(chunk: PaperChunk, *, score: float | None = None) -> RAGEvidence:
    return PaperChunkAdapter().to_rag_evidence(chunk, score=score)


def paper_chunk_to_evidence_metadata(chunk: PaperChunk) -> dict[str, Any]:
    evidence = paper_chunk_to_rag_evidence(chunk)
    metadata = paper_chunk_to_context_metadata(chunk)
    metadata.update({
        "rag_document_id": evidence.document_id,
        "rag_chunk_id": evidence.chunk_id,
        "rag_score": evidence.score,
        "rag_score_breakdown": evidence.score_breakdown.to_dict(),
    })
    if evidence.source_locator is not None:
        metadata["rag_source_locator"] = evidence.source_locator.to_dict()
    return metadata


def _chunk_fields(chunk: PaperChunk) -> dict[str, str]:
    field_text = extract_field_texts(chunk)
    fields = field_text.non_empty()
    if chunk.formula_latex.strip():
        fields.setdefault("formula", chunk.formula_latex)
    visual_description = str(chunk.metadata.get("visual_description") or "").strip()
    if visual_description:
        fields.setdefault("visual_description", visual_description)
    return fields


def _chunk_metadata(chunk: PaperChunk, locator: SourceLocator | None) -> dict[str, Any]:
    metadata = dict(chunk.metadata)
    metadata.update({
        "paper_id": chunk.paper_id,
        "parse_source": chunk.parse_source,
        "chunk_type": chunk.chunk_type,
        "parent_chunk_id": chunk.parent_chunk_id,
        "section_title": chunk.section_title,
        "section_role": list(chunk.section_role),
        "section_index": chunk.section_index,
        "has_formula": chunk.has_formula,
        "has_figure": chunk.has_figure,
        "figure_id": chunk.figure_id,
        "has_table": chunk.has_table,
        "references": list(chunk.references),
        "propositions_generated": chunk.propositions_generated,
        "proposition_quality": chunk.proposition_quality,
    })
    if locator is not None:
        metadata["source_locator_structured"] = locator.to_dict()
    field_text = extract_field_texts(chunk)
    metadata.setdefault("field_text_available_fields", field_text.available_fields())
    metadata.setdefault("field_text_sources", field_text.sources)
    return metadata


def _score_breakdown(metadata: Mapping[str, Any]) -> RAGScoreBreakdown:
    values: dict[str, float] = {}
    for component, keys in _SCORE_COMPONENT_KEYS.items():
        number = _first_number(metadata, keys)
        if number is not None:
            values[component] = number
    extra = {
        str(key): number
        for key, raw in metadata.items()
        if key.endswith("_score")
        and key not in {alias for aliases in _SCORE_COMPONENT_KEYS.values() for alias in aliases}
        and (number := _as_optional_float(raw)) is not None
    }
    return RAGScoreBreakdown.from_mapping({**values, **extra})


def _first_number(metadata: Mapping[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        number = _as_optional_float(metadata.get(key))
        if number is not None:
            return number
    return None


def _as_optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "PAPER_CHUNK_METADATA_KEY",
    "PaperChunkAdapter",
    "paper_chunk_evidence_score",
    "paper_chunk_from_evidence_record",
    "paper_chunk_to_evidence_metadata",
    "paper_chunk_to_rag_chunk",
    "paper_chunk_to_rag_evidence",
    "source_locator_from_paper_chunk",
]
