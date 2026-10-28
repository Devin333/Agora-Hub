from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from backend.foundation import PrimitiveModel
from backend.research.domain.common import require_text

ParseSource = Literal[
    "latex",
    "html",
    "publisher",
    "openreview",
    "pdf",
    "marker",
    "pymupdf",
    "nougat",
    "arxiv_abstract",
    # Readable source text kept when every structured parser failed; the
    # document stays outside the catalog but can still be read and asked.
    "text_fallback",
]
ChunkType = Literal["abstract", "paragraph", "proposition", "formula", "figure", "table"]
SectionRole = Literal["background", "related_work", "method", "experiment", "analysis", "conclusion"]
PropositionQuality = Literal["high", "low", "unknown"]


class PaperChunk(PrimitiveModel):
    chunk_id: str
    paper_id: str
    parse_source: ParseSource
    structure_detected: bool = True
    chunk_type: ChunkType = "paragraph"
    parent_chunk_id: str | None = None  # proposition -> paragraph; paragraph -> section (parent)
    section_title: str = ""
    section_role: list[SectionRole] = Field(default_factory=list)
    section_index: int = 0
    has_formula: bool = False
    formula_latex: str = ""           # LaTeX raw text when available
    formula_description: str = ""     # LLM-generated natural language description
    has_figure: bool = False
    figure_id: str = ""
    has_table: bool = False
    references: list[str] = Field(default_factory=list)  # cross-section soft-link chunk_ids
    propositions_generated: bool = False
    proposition_quality: PropositionQuality = "unknown"
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("chunk_id", "paper_id", "content")
    @classmethod
    def _required_text(cls, value: str) -> str:
        return require_text(value, "chunk fields")


__all__ = ["ChunkType", "PaperChunk", "ParseSource", "PropositionQuality", "SectionRole"]
