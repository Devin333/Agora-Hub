"""Relevance-based evidence selection before answering (RCS + Adaptive-k).

The retriever hands over a wide candidate pool. An evidence summarizer scores every
candidate for the question, the passages are ranked by that score, and Adaptive-k
keeps the passages above the largest score drop. Chosen passages may be condensed to
the summarizer's question-focused summary and verbatim quotes. Citations always keep
pointing at the original passages; condensing only changes what the answer model reads.

The selector works on passage ids and texts only, so it stays independent of how the
answer generator assembles or renders paper chunks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.research.ports.evidence_summarizer import (
    EVIDENCE_RELEVANCE_MAX,
    EVIDENCE_RELEVANCE_MIN,
    EvidencePassage,
    EvidenceSummarizerPort,
    EvidenceSummaryRequest,
    PassageSummary,
)
from backend.research.rag.retrieval.answer_context_budget import ContextLine
from framework.llm.models.usage import TokenUsage
from framework.rag.context import AdaptiveKPolicy, select_adaptive_k
from framework.shared.graph_identity import GraphExecutionIdentity

EVIDENCE_SELECTION_STRATEGY = "summarized_relevance_adaptive_k_v1"
EvidenceRenderMode = Literal["summary", "passage"]
EVIDENCE_RENDER_MODES: tuple[str, ...] = ("summary", "passage")
DEFAULT_EVIDENCE_POOL_SIZE = 48
DEFAULT_EVIDENCE_MIN_PASSAGES = 4
# 4+ is "context needed to interpret an answer" on the summarizer rubric; passages that
# only share the topic are not worth answer-model attention unless needed to reach min_k.
DEFAULT_EVIDENCE_MIN_RELEVANCE = 4
DEFAULT_EVIDENCE_MIN_GAP = 1.0


@dataclass(frozen=True)
class EvidenceSelectionOutcome:
    # Chosen passage ids in the order the answer model reads them.
    passage_ids: tuple[str, ...]
    # Replacement lines for passages condensed to summary and quotes; other chosen
    # passages are rendered from their original text.
    condensed_lines: Mapping[str, tuple[ContextLine, ...]]
    metadata: dict[str, Any]
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


class RelevanceEvidenceSelector:
    """Chooses and condenses answer evidence from a scored candidate pool."""

    def __init__(
        self,
        summarizer: EvidenceSummarizerPort,
        *,
        pool_size: int = DEFAULT_EVIDENCE_POOL_SIZE,
        min_passages: int = DEFAULT_EVIDENCE_MIN_PASSAGES,
        min_relevance: int = DEFAULT_EVIDENCE_MIN_RELEVANCE,
        min_gap: float = DEFAULT_EVIDENCE_MIN_GAP,
        render_mode: EvidenceRenderMode = "summary",
    ) -> None:
        if not isinstance(summarizer, EvidenceSummarizerPort):
            raise TypeError("summarizer must implement EvidenceSummarizerPort")
        for name, value in (("pool_size", pool_size), ("min_passages", min_passages)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if min_passages > pool_size:
            raise ValueError("min_passages must not exceed pool_size")
        if type(min_relevance) is not int or not (
            EVIDENCE_RELEVANCE_MIN <= min_relevance <= EVIDENCE_RELEVANCE_MAX
        ):
            raise ValueError(
                f"min_relevance must be an integer in [{EVIDENCE_RELEVANCE_MIN}, {EVIDENCE_RELEVANCE_MAX}]"
            )
        if render_mode not in EVIDENCE_RENDER_MODES:
            raise ValueError(f"render_mode must be one of {EVIDENCE_RENDER_MODES}")
        # Validates min_gap with the same rules the policy applies at selection time.
        AdaptiveKPolicy(min_k=min_passages, max_k=pool_size, min_gap=min_gap)
        self._summarizer = summarizer
        self._pool_size = pool_size
        self._min_passages = min_passages
        self._min_relevance = min_relevance
        self._min_gap = float(min_gap)
        self._render_mode: EvidenceRenderMode = render_mode

    @property
    def pool_size(self) -> int:
        """How many retrieved candidates are scored before choosing answer evidence."""
        return self._pool_size

    @property
    def measurement_identity(self) -> dict[str, Any]:
        return {
            "strategy": EVIDENCE_SELECTION_STRATEGY,
            "pool_size": self._pool_size,
            "min_passages": self._min_passages,
            "min_relevance": self._min_relevance,
            "min_gap": self._min_gap,
            "render_mode": self._render_mode,
            "summarizer": dict(self._summarizer.measurement_identity),
        }

    async def select(
        self,
        question: str,
        passages: Sequence[EvidencePassage],
        *,
        max_passages: int,
        pinned_ids: Sequence[str] = (),
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> EvidenceSelectionOutcome:
        """Choose at most ``max_passages`` of ``passages`` (given in retrieval order).

        ``pinned_ids`` are passages the caller requires (for example a passage the
        reader selected); they are kept ahead of scored evidence whatever their score.
        """
        if type(max_passages) is not int or max_passages < 1:
            raise ValueError("max_passages must be a positive integer")
        pool = list(passages[: self._pool_size])
        if not pool:
            return EvidenceSelectionOutcome((), {}, self._metadata([], {}, None, (), {}))
        result = await self._summarizer.summarize(EvidenceSummaryRequest(
            question=question,
            passages=tuple(pool),
            execution_identity=execution_identity,
        ))
        summaries = result.by_passage_id()
        order = {passage.passage_id: index for index, passage in enumerate(pool)}
        pinned = [pid for pid in dict.fromkeys(pinned_ids) if pid in order][:max_passages]
        pinned_set = set(pinned)

        def relevance(pid: str) -> int | None:
            summary = summaries.get(pid)
            return summary.relevance if summary is not None else None

        assessed = sorted(
            (pid for pid in order if pid not in pinned_set and relevance(pid) is not None),
            key=lambda pid: (-int(relevance(pid) or 0), order[pid]),
        )
        unassessed = [pid for pid in order if pid not in pinned_set and relevance(pid) is None]
        room = max_passages - len(pinned)
        eligible = [pid for pid in assessed if int(relevance(pid) or 0) >= self._min_relevance]
        decision = None
        chosen: list[str] = []
        if room > 0:
            policy = AdaptiveKPolicy(
                min_k=min(self._min_passages, room), max_k=room, min_gap=self._min_gap,
            )
            decision = select_adaptive_k([float(relevance(pid) or 0) for pid in eligible], policy)
            chosen = eligible[: decision.k]
            # Too few confident passages: keep the next best scored ones so the answer
            # model can still judge, rather than starving it on a scorer miss.
            for pid in assessed:
                if len(chosen) >= policy.min_k:
                    break
                if pid not in chosen:
                    chosen.append(pid)
            # Passages the summarizer could not assess are never dropped on its account.
            for pid in unassessed:
                if len(chosen) >= room:
                    break
                chosen.append(pid)
        final = [*pinned, *chosen]
        condensed = (
            {
                pid: lines
                for pid in final
                if (lines := _condensed_lines(summaries.get(pid))) is not None
            }
            if self._render_mode == "summary"
            else {}
        )
        return EvidenceSelectionOutcome(
            passage_ids=tuple(final),
            condensed_lines=condensed,
            metadata=self._metadata(final, summaries, decision, pinned, result.diagnostics, condensed),
            model=result.model,
            usage=result.usage,
        )

    def _metadata(
        self,
        final: Sequence[str],
        summaries: Mapping[str, PassageSummary],
        decision: Any,
        pinned: Sequence[str],
        diagnostics: Mapping[str, Any],
        condensed: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            **self.measurement_identity,
            "candidate_count": len(summaries),
            "relevance": {pid: summary.relevance for pid, summary in summaries.items()},
            "adaptive_k": decision.to_dict() if decision is not None else None,
            "pinned_ids": list(pinned),
            "selected_ids": list(final),
            "condensed_ids": sorted(condensed or {}),
            "summarizer_diagnostics": dict(diagnostics),
        }


def _condensed_lines(summary: PassageSummary | None) -> tuple[ContextLine, ...] | None:
    if summary is None or not summary.assessed or not (summary.summary or summary.quotes):
        return None
    lines: list[ContextLine] = []
    if summary.summary:
        lines.append(ContextLine("Summary", summary.summary))
    lines.extend(ContextLine("Quote", quote) for quote in summary.quotes)
    return tuple(lines)


__all__ = [
    "DEFAULT_EVIDENCE_MIN_GAP",
    "DEFAULT_EVIDENCE_MIN_PASSAGES",
    "DEFAULT_EVIDENCE_MIN_RELEVANCE",
    "DEFAULT_EVIDENCE_POOL_SIZE",
    "EVIDENCE_RENDER_MODES",
    "EVIDENCE_SELECTION_STRATEGY",
    "EvidenceRenderMode",
    "EvidenceSelectionOutcome",
    "RelevanceEvidenceSelector",
]
