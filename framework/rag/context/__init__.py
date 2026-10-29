from __future__ import annotations

from framework.rag.context.adaptive_k import (
    ADAPTIVE_K_STRATEGY,
    AdaptiveKDecision,
    AdaptiveKPolicy,
    select_adaptive_k,
)
from framework.rag.context.assembler import RAGContextAssembler
from framework.rag.context.budget import ContextBudget, trim_evidence_to_budget
from framework.rag.context.citation import CitationResolution, resolve_citation
from framework.rag.context.nearby_context import (
    DEFAULT_DIRECT_CONTEXT_KEYS,
    DEFAULT_REFERENCE_LIST_KEYS,
    NearbyContextIds,
    collect_nearby_context_ids,
)
from framework.rag.context.source_span import (
    CONTENT_SPAN_UNIT,
    SourceSpanResolution,
    build_main_overlap_span_metadata,
    locate_snippet_span,
    remap_span_origin_ids,
    resolve_source_span,
)

__all__ = [
    "ADAPTIVE_K_STRATEGY",
    "AdaptiveKDecision",
    "AdaptiveKPolicy",
    "CONTENT_SPAN_UNIT",
    "CitationResolution",
    "ContextBudget",
    "DEFAULT_DIRECT_CONTEXT_KEYS",
    "DEFAULT_REFERENCE_LIST_KEYS",
    "NearbyContextIds",
    "RAGContextAssembler",
    "SourceSpanResolution",
    "build_main_overlap_span_metadata",
    "collect_nearby_context_ids",
    "locate_snippet_span",
    "remap_span_origin_ids",
    "resolve_citation",
    "resolve_source_span",
    "select_adaptive_k",
    "trim_evidence_to_budget",
]
