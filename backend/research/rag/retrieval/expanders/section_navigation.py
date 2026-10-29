"""Structure-aware expansion: read the sections a navigator picks from the outline.

Flat retrieval concentrates its top passages in one or two sections. After ranking,
the navigator sees the paper outline with the sections the ranked passages come
from marked as read, and names unread sections likely to hold the rest of the
answer. Their best passages take the tail slots of the ranked list: they replace
the lowest-ranked passages and inherit those passages' scores, so they hold the
same place in the context budget's confidence ordering instead of being cut as
unscored extras. The ranked list never shrinks below half of its passages.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from framework.rag.retrieval import expansion_metadata
from framework.shared.sync_bridge import run_coroutine_sync

from backend.research.document.models import PaperChunk
from backend.research.document.section_outline import (
    DEFAULT_OUTLINE_PREVIEW_CHARS,
    PaperSectionOutline,
)
from backend.research.ports.chunk_store import ChunkStorePort
from backend.research.ports.paper_answer_model import PaperAnswerModelError
from backend.research.ports.section_navigator import (
    SectionNavigationDecision,
    SectionNavigationRequest,
    SectionNavigatorPort,
)
from backend.research.rag.adapters.paper_chunk_adapter import paper_chunk_evidence_score
from backend.research.rag.retrieval.channels.sparse_lexical import chunk_matches_filters
from backend.research.rag.retrieval.filtering import filter_chunks_for_request, request_filters
from backend.research.rag.retrieval.trace import RetrievalDegradation, RetrievalTrace
from backend.research.services.tenant_visibility import strip_tenant_filters

SECTION_NAVIGATION_REASON = "section_navigation"
SECTION_NAVIGATION_EDGE = "section_outline"
# Metadata key holding the passage's own ranking score when it was a ranked candidate.
SECTION_NAVIGATION_RANK_SCORE_KEY = "section_navigation_rank_score"
DEFAULT_NAVIGATION_MAX_SECTIONS = 2
DEFAULT_NAVIGATION_MAX_PASSAGES_PER_SECTION = 2


@dataclass(frozen=True)
class SectionNavigationConfig:
    max_sections: int = DEFAULT_NAVIGATION_MAX_SECTIONS
    max_passages_per_section: int = DEFAULT_NAVIGATION_MAX_PASSAGES_PER_SECTION
    preview_chars: int = DEFAULT_OUTLINE_PREVIEW_CHARS

    def __post_init__(self) -> None:
        for name in ("max_sections", "max_passages_per_section"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if type(self.preview_chars) is not int or self.preview_chars < 0:
            raise ValueError("preview_chars must be a non-negative integer")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_sections": self.max_sections,
            "max_passages_per_section": self.max_passages_per_section,
            "preview_chars": self.preview_chars,
        }


@dataclass(frozen=True)
class SectionNavigationOutcome:
    child_chunks: list[PaperChunk]
    report: dict[str, Any] = field(default_factory=dict)


class SectionNavigationExpander:
    name = SECTION_NAVIGATION_REASON

    def __init__(
        self,
        chunk_store: ChunkStorePort,
        navigator: SectionNavigatorPort,
        config: SectionNavigationConfig | None = None,
    ) -> None:
        if not isinstance(navigator, SectionNavigatorPort):
            raise TypeError("navigator must implement SectionNavigatorPort")
        self._store = chunk_store
        self._navigator = navigator
        self._config = config or SectionNavigationConfig()
        self._outlines: dict[tuple[str, str], PaperSectionOutline] = {}

    @property
    def measurement_identity(self) -> dict[str, Any]:
        return {
            "component": type(self).__name__,
            "config": self._config.to_dict(),
            "navigator": dict(self._navigator.measurement_identity),
        }

    def expand(
        self,
        child_chunks: list[PaperChunk],
        scored: list[tuple[PaperChunk, float]],
        request: Any,
        *,
        limit: int,
        trace: RetrievalTrace | None = None,
    ) -> SectionNavigationOutcome:
        if not child_chunks:
            return SectionNavigationOutcome(child_chunks, {"skipped": "no_ranked_passages"})
        outline = self._outline(request)
        if not outline.navigable:
            return SectionNavigationOutcome(child_chunks, {"skipped": "no_section_structure"})
        section_of = outline.section_of()
        read = tuple(dict.fromkeys(
            section_of[chunk.chunk_id] for chunk in child_chunks if chunk.chunk_id in section_of
        ))
        try:
            decision = run_coroutine_sync(self._navigator.choose(SectionNavigationRequest(
                question=request.question,
                outline=outline,
                read_section_indexes=read,
                max_sections=self._config.max_sections,
            )))
        except PaperAnswerModelError as exc:
            # Navigation only adds evidence; flat retrieval stands when the model fails.
            logging.getLogger(__name__).warning("section navigation failed: %s", exc.code)
            if trace is not None:
                trace.append_degradation_once(RetrievalDegradation(
                    code=f"section_navigation_{exc.code}",
                    stage=SECTION_NAVIGATION_REASON,
                    paper_id=request.paper_id,
                    reason=str(exc),
                ))
            return SectionNavigationOutcome(child_chunks, {
                "read_sections": list(read),
                "error": exc.code,
                "retryable": exc.retryable,
            })

        passages = self._navigated_passages(decision, outline, scored, request, child_chunks)
        keep, slots = _take_tail_slots(child_chunks, passages, limit=limit)
        anchor_id = child_chunks[0].chunk_id
        annotated = [
            _with_navigation_metadata(
                chunk, anchor_id=anchor_id, rank=rank, slot_score=slot_score,
                section_index=section_of.get(chunk.chunk_id, chunk.section_index),
            )
            for rank, (chunk, slot_score) in enumerate(slots, start=1)
        ]
        return SectionNavigationOutcome([*child_chunks[:keep], *annotated], {
            "read_sections": list(read),
            "chosen_sections": list(decision.section_indexes),
            "added_chunk_ids": [chunk.chunk_id for chunk in annotated],
            "displaced_chunk_ids": [chunk.chunk_id for chunk in child_chunks[keep:]],
            "candidate_passages": len(passages),
            "model": decision.model,
            "usage": decision.usage.to_dict(),
            "diagnostics": dict(decision.diagnostics),
        })

    def _outline(self, request: Any) -> PaperSectionOutline:
        filters = request_filters(request)
        key = (request.paper_id, json.dumps(filters, sort_keys=True, default=str))
        cached = self._outlines.get(key)
        if cached is not None:
            return cached
        storage_filters, _tenant = strip_tenant_filters(filters)
        chunks = [
            chunk for chunk in self._store.list_chunks(request.paper_id)
            if chunk.paper_id == request.paper_id and chunk_matches_filters(chunk, storage_filters)
        ]
        outline = PaperSectionOutline.from_chunks(
            request.paper_id,
            filter_chunks_for_request(chunks, request),
            preview_chars=self._config.preview_chars,
        )
        self._outlines[key] = outline
        return outline

    def _navigated_passages(
        self,
        decision: SectionNavigationDecision,
        outline: PaperSectionOutline,
        scored: list[tuple[PaperChunk, float]],
        request: Any,
        child_chunks: list[PaperChunk],
    ) -> list[PaperChunk]:
        """Best passages of each chosen section, interleaved so every section gets a slot first."""
        ranked = {chunk.chunk_id: (position, chunk, score) for position, (chunk, score) in enumerate(scored)}
        present = {chunk.chunk_id for chunk in child_chunks}
        per_section: list[list[PaperChunk]] = []
        for section_index in decision.section_indexes:
            entry = outline.entry(section_index)
            if entry is None:
                continue
            reading_order = {chunk_id: offset for offset, chunk_id in enumerate(entry.chunk_ids)}
            # Ranked passages first by retrieval rank, then unranked ones in reading order.
            ordered = sorted(
                (chunk_id for chunk_id in entry.chunk_ids if chunk_id not in present),
                key=lambda chunk_id: (
                    (0, ranked[chunk_id][0]) if chunk_id in ranked else (1, reading_order[chunk_id])
                ),
            )
            chosen: list[PaperChunk] = []
            for chunk_id in ordered:
                if len(chosen) >= self._config.max_passages_per_section:
                    break
                chunk = self._resolve(chunk_id, ranked, request)
                if chunk is not None:
                    chosen.append(chunk)
            if chosen:
                per_section.append(chosen)
        interleaved: list[PaperChunk] = []
        for depth in range(self._config.max_passages_per_section):
            interleaved.extend(passages[depth] for passages in per_section if depth < len(passages))
        return interleaved

    def _resolve(
        self,
        chunk_id: str,
        ranked: Mapping[str, tuple[int, PaperChunk, float]],
        request: Any,
    ) -> PaperChunk | None:
        if chunk_id in ranked:
            _position, chunk, score = ranked[chunk_id]
            metadata = {**chunk.metadata, SECTION_NAVIGATION_RANK_SCORE_KEY: round(float(score), 6)}
            return chunk.model_copy(update={"metadata": metadata})
        chunk = self._store.get_chunk(chunk_id)
        if chunk is None or chunk.paper_id != request.paper_id:
            return None
        visible = filter_chunks_for_request([chunk], request)
        return visible[0] if visible else None


def _take_tail_slots(
    ranked: list[PaperChunk],
    navigated: list[PaperChunk],
    *,
    limit: int,
) -> tuple[int, list[tuple[PaperChunk, float]]]:
    """How many ranked passages stay, and the navigated passages with the slot score each takes.

    Free slots up to ``limit`` are used first and score at the lowest kept ranked
    passage; further navigated passages displace ranked ones from the tail, never
    more than half of them, and take over the displaced passages' scores.
    """
    if not navigated:
        return len(ranked), []
    free = max(0, limit - len(ranked))
    displace = min(max(0, len(navigated) - free), len(ranked) // 2)
    keep = len(ranked) - displace
    taken = navigated[: displace + free]
    displaced_scores = sorted((_evidence_score(chunk) for chunk in ranked[keep:]), reverse=True)
    floor = min((_evidence_score(chunk) for chunk in ranked[:keep]), default=0.0)
    slot_scores = [*displaced_scores, *([floor] * (len(taken) - displace))]
    return keep, list(zip(taken, slot_scores))


def _evidence_score(chunk: PaperChunk) -> float:
    score = paper_chunk_evidence_score(chunk)
    return float(score) if score is not None else 0.0


def _with_navigation_metadata(
    chunk: PaperChunk,
    *,
    anchor_id: str,
    rank: int,
    slot_score: float,
    section_index: int,
) -> PaperChunk:
    metadata = dict(chunk.metadata)
    metadata.update(expansion_metadata(
        expanded_from_id=anchor_id,
        reason=SECTION_NAVIGATION_REASON,
        edge=SECTION_NAVIGATION_EDGE,
        rank=rank,
    ))
    metadata["section_navigation_section_index"] = section_index
    metadata["section_navigation_slot_score"] = round(float(slot_score), 6)
    # Evidence ranking reads ``final_score`` first; the passage takes its slot's place.
    metadata["final_score"] = round(float(slot_score), 6)
    return chunk.model_copy(update={"metadata": metadata})


__all__ = [
    "DEFAULT_NAVIGATION_MAX_PASSAGES_PER_SECTION",
    "DEFAULT_NAVIGATION_MAX_SECTIONS",
    "SECTION_NAVIGATION_EDGE",
    "SECTION_NAVIGATION_RANK_SCORE_KEY",
    "SECTION_NAVIGATION_REASON",
    "SectionNavigationConfig",
    "SectionNavigationExpander",
    "SectionNavigationOutcome",
]
