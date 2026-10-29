from __future__ import annotations

from typing import Any

import pytest

from backend.research.document.models import PaperChunk
from backend.research.ports.paper_answer_model import PaperAnswerModelError
from backend.research.ports.section_navigator import (
    SectionNavigationDecision,
    SectionNavigationRequest,
)
from backend.research.rag.adapters import paper_chunk_to_rag_evidence
from backend.research.rag.retrieval.contracts import RetrievalRequest
from backend.research.rag.retrieval.expanders.section_navigation import (
    SECTION_NAVIGATION_RANK_SCORE_KEY,
    SECTION_NAVIGATION_REASON,
    SectionNavigationConfig,
    SectionNavigationExpander,
)
from backend.research.rag.retrieval.trace import RetrievalTrace
from framework.llm.models.usage import TokenUsage

_SECTIONS = {1: "Introduction", 2: "Method", 3: "Datasets", 4: "Results"}


def _chunk(
    chunk_id: str,
    section_index: int,
    *,
    score: float | None = None,
    parent: bool = False,
    metadata: dict[str, Any] | None = None,
    paper_id: str = "p1",
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        parse_source="latex",
        chunk_type="paragraph",
        section_title=_SECTIONS.get(section_index, ""),
        section_role=["method"],  # type: ignore[arg-type]
        section_index=section_index,
        content=f"Passage {chunk_id}.",
        metadata={
            **({"is_parent": True} if parent else {}),
            **({"final_score": score} if score is not None else {}),
            **(metadata or {}),
        },
    )


class _Store:
    def __init__(self, chunks: list[PaperChunk]) -> None:
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        self.list_calls = 0

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        self.list_calls += 1
        return [chunk for chunk in self._chunks.values() if chunk.paper_id == paper_id]

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        return self._chunks.get(chunk_id)


class _Navigator:
    def __init__(self, *decisions: tuple[int, ...] | Exception) -> None:
        self._decisions = list(decisions)
        self.requests: list[SectionNavigationRequest] = []

    @property
    def measurement_identity(self) -> dict[str, Any]:
        return {"component": "fake-navigator"}

    async def choose(self, request: SectionNavigationRequest) -> SectionNavigationDecision:
        self.requests.append(request)
        decision = self._decisions[min(len(self.requests) - 1, len(self._decisions) - 1)]
        if isinstance(decision, Exception):
            raise decision
        return SectionNavigationDecision(
            decision, model="fake", usage=TokenUsage(input_tokens=10, output_tokens=2),
            diagnostics={"model_calls": 1},
        )


def _paper() -> list[PaperChunk]:
    return [
        _chunk("intro-parent", 1, parent=True),
        _chunk("i1", 1),
        _chunk("i2", 1),
        _chunk("m1", 2),
        _chunk("m2", 2),
        _chunk("d1", 3),
        _chunk("d2", 3),
        _chunk("d3", 3),
        _chunk("r1", 4),
    ]


def _ranked(*items: tuple[str, int, float]) -> list[PaperChunk]:
    return [_chunk(chunk_id, section, score=score) for chunk_id, section, score in items]


def _request(**kwargs: Any) -> RetrievalRequest:
    return RetrievalRequest(paper_id="p1", question="Which datasets are used?", **kwargs)


def test_navigated_passages_fill_free_slots_at_the_lowest_ranked_score() -> None:
    navigator = _Navigator((3,))
    expander = SectionNavigationExpander(_Store(_paper()), navigator)
    ranked = _ranked(("i1", 1, 0.9), ("m1", 2, 0.6))

    outcome = expander.expand(ranked, [(chunk, 0.0) for chunk in ranked], _request(), limit=4)

    assert [chunk.chunk_id for chunk in outcome.child_chunks] == ["i1", "m1", "d1", "d2"]
    added = outcome.child_chunks[2:]
    assert all(chunk.metadata["expansion_reason"] == SECTION_NAVIGATION_REASON for chunk in added)
    assert all(chunk.metadata["expanded_from_chunk_id"] == "i1" for chunk in added)
    assert [chunk.metadata["final_score"] for chunk in added] == [0.6, 0.6]
    assert added[0].metadata["section_navigation_section_index"] == 3
    assert navigator.requests[0].read_section_indexes == (1, 2)
    assert outcome.report["chosen_sections"] == [3]
    assert outcome.report["added_chunk_ids"] == ["d1", "d2"]
    assert outcome.report["displaced_chunk_ids"] == []
    assert outcome.report["usage"]["input_tokens"] == 10


def test_navigated_passages_displace_the_tail_and_take_its_scores() -> None:
    expander = SectionNavigationExpander(
        _Store(_paper()), _Navigator((3, 4)), SectionNavigationConfig(max_passages_per_section=2),
    )
    ranked = _ranked(("i1", 1, 0.9), ("i2", 1, 0.8), ("m1", 2, 0.5), ("m2", 2, 0.4))

    outcome = expander.expand(ranked, [(chunk, 0.0) for chunk in ranked], _request(), limit=4)

    # Three navigated passages exist but at most half of the ranked list is displaced.
    assert [chunk.chunk_id for chunk in outcome.child_chunks] == ["i1", "i2", "d1", "r1"]
    assert [chunk.metadata["final_score"] for chunk in outcome.child_chunks[2:]] == [0.5, 0.4]
    assert outcome.report["displaced_chunk_ids"] == ["m1", "m2"]
    assert outcome.report["candidate_passages"] == 3
    # Evidence ranking sees the slot score, so the passages keep their place.
    evidence_scores = [paper_chunk_to_rag_evidence(chunk).score for chunk in outcome.child_chunks]
    assert evidence_scores == sorted(evidence_scores, reverse=True)


def test_ranked_candidates_of_a_chosen_section_come_first_and_keep_their_rank_score() -> None:
    expander = SectionNavigationExpander(
        _Store(_paper()), _Navigator((3,)), SectionNavigationConfig(max_passages_per_section=1),
    )
    ranked = _ranked(("i1", 1, 0.9))
    scored = [(ranked[0], 0.9), (_chunk("d3", 3, score=0.3), 0.3)]

    outcome = expander.expand(ranked, scored, _request(), limit=2)

    added = outcome.child_chunks[1]
    assert added.chunk_id == "d3"
    assert added.metadata[SECTION_NAVIGATION_RANK_SCORE_KEY] == 0.3
    assert added.metadata["final_score"] == 0.9


def test_sections_get_one_slot_each_before_any_gets_a_second() -> None:
    expander = SectionNavigationExpander(_Store(_paper()), _Navigator((3, 2)))
    ranked = _ranked(("i1", 1, 0.9))

    outcome = expander.expand(ranked, [(ranked[0], 0.9)], _request(), limit=3)

    assert [chunk.chunk_id for chunk in outcome.child_chunks] == ["i1", "d1", "m1"]


def test_already_present_passages_are_not_added_twice() -> None:
    expander = SectionNavigationExpander(_Store(_paper()), _Navigator((3,)))
    ranked = _ranked(("i1", 1, 0.9), ("d1", 3, 0.7))

    outcome = expander.expand(ranked, [(chunk, 0.0) for chunk in ranked], _request(), limit=4)

    assert [chunk.chunk_id for chunk in outcome.child_chunks] == ["i1", "d1", "d2", "d3"]


def test_navigation_is_skipped_for_papers_without_section_structure() -> None:
    store = _Store([_chunk("w1", 0), _chunk("w2", 0)])
    navigator = _Navigator((0,))
    ranked = _ranked(("w1", 0, 0.9))

    outcome = SectionNavigationExpander(store, navigator).expand(ranked, [], _request(), limit=4)

    assert outcome.child_chunks == ranked
    assert outcome.report == {"skipped": "no_section_structure"}
    assert navigator.requests == []


def test_model_failure_keeps_flat_retrieval_and_records_a_degradation() -> None:
    navigator = _Navigator(PaperAnswerModelError("provider_unavailable", "down", retryable=True))
    trace = RetrievalTrace(policy_name="p", policy_hash="h")
    ranked = _ranked(("i1", 1, 0.9))

    outcome = SectionNavigationExpander(_Store(_paper()), navigator).expand(
        ranked, [], _request(), limit=4, trace=trace,
    )

    assert outcome.child_chunks == ranked
    assert outcome.report["error"] == "provider_unavailable"
    assert outcome.report["retryable"] is True
    assert trace.degradations[0].code == "section_navigation_provider_unavailable"


def test_outline_respects_request_filters_and_tenant_visibility_and_is_cached() -> None:
    chunks = [
        *_paper(),
        _chunk("d-other-run", 3, metadata={"run_id": "other"}),
        _chunk("d-tenant", 3, metadata={"tenant_id": "t2"}),
    ]
    chunks = [
        chunk if chunk.chunk_id in {"d-other-run"} else chunk.model_copy(
            update={"metadata": {**chunk.metadata, "run_id": "r1"}}
        )
        for chunk in chunks
    ]
    store = _Store(chunks)
    navigator = _Navigator((3,))
    expander = SectionNavigationExpander(
        store, navigator, SectionNavigationConfig(max_passages_per_section=5),
    )
    request = _request(filters={"run_id": "r1", "tenant_id": "t1"})
    ranked = [chunks[1]]

    first = expander.expand(ranked, [], request, limit=10)
    expander.expand(ranked, [], request, limit=10)

    assert [chunk.chunk_id for chunk in first.child_chunks[1:]] == ["d1", "d2", "d3"]
    entry = navigator.requests[0].outline.entry(3)
    assert entry is not None and "d-other-run" not in entry.chunk_ids and "d-tenant" not in entry.chunk_ids
    assert store.list_calls == 1


def test_empty_ranked_list_is_left_alone() -> None:
    navigator = _Navigator((3,))

    outcome = SectionNavigationExpander(_Store(_paper()), navigator).expand([], [], _request(), limit=4)

    assert outcome.child_chunks == []
    assert navigator.requests == []


def test_identity_includes_config_and_navigator() -> None:
    expander = SectionNavigationExpander(
        _Store([]), _Navigator(()), SectionNavigationConfig(max_sections=3),
    )

    assert expander.measurement_identity == {
        "component": "SectionNavigationExpander",
        "config": {"max_sections": 3, "max_passages_per_section": 2, "preview_chars": 160},
        "navigator": {"component": "fake-navigator"},
    }


def test_config_and_navigator_are_validated() -> None:
    with pytest.raises(ValueError):
        SectionNavigationConfig(max_sections=0)
    with pytest.raises(ValueError):
        SectionNavigationConfig(preview_chars=-1)
    with pytest.raises(TypeError):
        SectionNavigationExpander(_Store([]), object())  # type: ignore[arg-type]
