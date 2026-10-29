"""Composition of the desktop reader Q&A answer path.

Owns how the configured RAG settings become the reader session budget and the
answer worker, so the item budget, the rendered answer context and the optional
evidence selection (RCS + Adaptive-k) always agree with each other, and how the
optional section navigation of the reader retrieval is built.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from backend.research.application.paper_answer_model import OpenAICompatiblePaperAnswerModel
from backend.research.rag.adapters import PaperAnswerWorker
from backend.research.rag.retrieval.answer_evidence_selection import RelevanceEvidenceSelector
from backend.research.rag.retrieval.expanders.section_navigation import SectionNavigationConfig
from backend.research.rag.retrieval.paper_answer_generator import (
    AnswerGenerator,
    context_chars_for_passages,
)
from backend.research.rag.retrieval.paper_evidence_summarizer import (
    DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS,
    LLMEvidenceSummarizer,
)
from backend.research.rag.retrieval.paper_section_navigator import (
    DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
    LLMSectionNavigator,
)
from framework.harness import RAGBudget
from framework.llm.clients.openai_compatible import OpenAICompatibleClient
from interfaces.composition.research_settings import ResearchRAGSettings


def reader_rag_budget(settings: ResearchRAGSettings) -> RAGBudget:
    """Session budget for reader Q&A.

    Reader questions keep the bounded default loop (rounds, queries, reads); the
    context limits come from the configured RAG settings. With evidence selection
    the session admits the whole candidate pool and the answer generator then lets
    at most ``max_context_items`` of it reach the answer model.
    """
    return replace(
        RAGBudget.safe_default(),
        max_context_items=settings.reader_context_items,
        max_context_tokens=settings.max_context_tokens,
    )


def reader_evidence_selector(
    llm_client: OpenAICompatibleClient,
    settings: ResearchRAGSettings,
) -> RelevanceEvidenceSelector | None:
    """Evidence selector on the configured Research LLM, or None when disabled."""
    if not settings.evidence_selection_enabled:
        return None
    evidence_model = OpenAICompatiblePaperAnswerModel(
        llm_client,
        max_output_tokens=DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS,
        temperature=0.0,
    )
    return RelevanceEvidenceSelector(
        LLMEvidenceSummarizer(evidence_model),
        pool_size=settings.evidence_pool_size,
        min_passages=settings.evidence_min_passages,
        min_relevance=settings.evidence_min_relevance,
        render_mode=settings.evidence_render_mode,  # type: ignore[arg-type]
    )


def reader_section_navigation(
    llm_client: OpenAICompatibleClient,
    settings: ResearchRAGSettings,
) -> tuple[LLMSectionNavigator, SectionNavigationConfig] | tuple[None, None]:
    """Section navigator and its config on the configured Research LLM, or (None, None)."""
    if not settings.section_navigation_enabled:
        return None, None
    navigation_model = OpenAICompatiblePaperAnswerModel(
        llm_client,
        max_output_tokens=DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
        temperature=0.0,
    )
    config = SectionNavigationConfig(
        max_sections=settings.navigation_max_sections,
        max_passages_per_section=settings.navigation_max_passages_per_section,
    )
    return LLMSectionNavigator(navigation_model), config


def reader_answer_worker_factory(
    llm_client: OpenAICompatibleClient,
    settings: ResearchRAGSettings,
    *,
    max_output_tokens: int,
) -> Callable[[], PaperAnswerWorker]:
    answer_model = OpenAICompatiblePaperAnswerModel(
        llm_client,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
    )
    evidence_selector = reader_evidence_selector(llm_client, settings)
    max_context_chunks = settings.max_context_items
    return lambda: PaperAnswerWorker(AnswerGenerator(
        answer_model,
        max_context_chunks=max_context_chunks,
        max_context_chars=context_chars_for_passages(max_context_chunks),
        evidence_selector=evidence_selector,
    ))


__all__ = [
    "reader_answer_worker_factory",
    "reader_evidence_selector",
    "reader_rag_budget",
    "reader_section_navigation",
]
