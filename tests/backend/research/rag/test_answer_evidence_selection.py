from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from backend.research.document.models import PaperChunk
from backend.research.ports.evidence_summarizer import (
    EvidencePassage,
    EvidenceSummaryRequest,
    EvidenceSummaryResult,
    PassageSummary,
)
from backend.research.ports.paper_answer_model import PaperAnswerModelError
from backend.research.rag.retrieval.answer_evidence_selection import RelevanceEvidenceSelector
from backend.research.rag.retrieval.paper_answer_generator import (
    AnswerGenerator,
    context_chars_for_passages,
)
from backend.research.rag.retrieval.paper_retriever import RetrievalResult
from framework.llm.models.usage import TokenUsage
from tests.fixtures.paper_answer_model import FakePaperAnswerModel


class ScriptedSummarizer:
    """Fake summarizer: fixed relevance per passage id, summary echoes the id."""

    def __init__(self, relevance: Mapping[str, int | None], *, error: Exception | None = None) -> None:
        self._relevance = dict(relevance)
        self._error = error
        self.requests: list[EvidenceSummaryRequest] = []

    @property
    def measurement_identity(self) -> dict[str, str]:
        return {"component": "ScriptedSummarizer"}

    async def summarize(self, request: EvidenceSummaryRequest) -> EvidenceSummaryResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        return EvidenceSummaryResult(
            summaries=tuple(
                PassageSummary(
                    passage.passage_id,
                    relevance=self._relevance.get(passage.passage_id),
                    summary=f"Summary of {passage.passage_id}." if self._relevance.get(passage.passage_id) else "",
                    quotes=(passage.text[:12],) if self._relevance.get(passage.passage_id) else (),
                )
                for passage in request.passages
            ),
            model="fake-summarizer",
            usage=TokenUsage(input_tokens=7, output_tokens=3),
            diagnostics={"model_calls": 1},
        )


def _passages(*ids: str) -> list[EvidencePassage]:
    return [EvidencePassage(pid, f"Text of {pid} with detail.") for pid in ids]


def _select(selector: RelevanceEvidenceSelector, ids, **kwargs):
    return asyncio.run(selector.select("Which datasets?", _passages(*ids), **kwargs))


def test_selector_ranks_by_relevance_and_cuts_at_the_largest_drop() -> None:
    summarizer = ScriptedSummarizer({"a": 3, "b": 9, "c": 8, "d": 9, "e": 4, "f": 5})
    selector = RelevanceEvidenceSelector(summarizer, pool_size=6, min_passages=2, min_relevance=4)

    outcome = _select(selector, "abcdef", max_passages=5)

    # Ties keep retrieval order; the 8 -> 5 drop is the largest inside [2, 5].
    assert outcome.passage_ids == ("b", "d", "c")
    assert outcome.metadata["adaptive_k"]["gap_applied"] is True
    assert outcome.metadata["relevance"]["a"] == 3
    assert set(outcome.condensed_lines) == {"b", "d", "c"}
    assert outcome.usage.input_tokens == 7


def test_selector_backfills_to_min_passages_when_few_are_confident() -> None:
    summarizer = ScriptedSummarizer({"a": 2, "b": 9, "c": 1, "d": 0})
    selector = RelevanceEvidenceSelector(summarizer, pool_size=4, min_passages=3, min_relevance=4)

    outcome = _select(selector, "abcd", max_passages=4)

    assert outcome.passage_ids == ("b", "a", "c")


def test_selector_keeps_unassessed_passages_in_retrieval_order() -> None:
    summarizer = ScriptedSummarizer({"a": 9, "b": None, "c": 9, "d": None})
    selector = RelevanceEvidenceSelector(
        summarizer, pool_size=4, min_passages=1, min_relevance=4, min_gap=0.0,
    )

    outcome = _select(selector, "abcd", max_passages=3)

    assert outcome.passage_ids == ("a", "c", "b")
    # An unassessed passage has nothing to condense to and keeps its original text.
    assert "b" not in outcome.condensed_lines


def test_selector_puts_pinned_passages_first_whatever_their_score() -> None:
    summarizer = ScriptedSummarizer({"a": 9, "b": 0, "c": 9})
    selector = RelevanceEvidenceSelector(summarizer, pool_size=3, min_passages=1)

    outcome = _select(selector, "abc", max_passages=2, pinned_ids=["b", "missing"])

    assert outcome.passage_ids == ("b", "a")
    assert outcome.metadata["pinned_ids"] == ["b"]


def test_selector_scores_only_the_pool() -> None:
    summarizer = ScriptedSummarizer({pid: 5 for pid in "abcdef"})
    selector = RelevanceEvidenceSelector(summarizer, pool_size=4, min_passages=1)

    _select(selector, "abcdef", max_passages=2)

    assert [p.passage_id for p in summarizer.requests[0].passages] == ["a", "b", "c", "d"]


def test_passage_render_mode_never_condenses() -> None:
    summarizer = ScriptedSummarizer({"a": 9})
    selector = RelevanceEvidenceSelector(summarizer, pool_size=2, min_passages=1, render_mode="passage")

    outcome = _select(selector, "a", max_passages=1)

    assert outcome.condensed_lines == {}
    assert outcome.metadata["render_mode"] == "passage"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"pool_size": 0},
        {"pool_size": 2, "min_passages": 3},
        {"min_relevance": 11},
        {"min_gap": -1.0},
        {"render_mode": "full"},
    ],
)
def test_selector_rejects_invalid_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        RelevanceEvidenceSelector(ScriptedSummarizer({}), **kwargs)


def _chunk(chunk_id: str, content: str) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="latex",
        chunk_type="paragraph",  # type: ignore[arg-type]
        content=content,
        metadata={},
    )


def _retrieval(*chunks: PaperChunk) -> RetrievalResult:
    return RetrievalResult(
        parent_chunks=[],
        child_chunks=list(chunks),
        ref_chunks=[],
        intent="concept_method",  # type: ignore[arg-type]
    )


def _generator(model, summarizer, *, max_chunks=2, pool_size=4, render_mode="summary"):
    return AnswerGenerator(
        model,
        max_context_chunks=max_chunks,
        max_context_chars=context_chars_for_passages(max_chunks),
        evidence_selector=RelevanceEvidenceSelector(
            summarizer, pool_size=pool_size, min_passages=1, min_gap=0.0, render_mode=render_mode,
        ),
    )


def test_generator_answers_from_selected_condensed_evidence_and_cites_originals() -> None:
    chunks = [
        _chunk("p-1", "Background on the topic only."),
        _chunk("p-2", "Background again without the answer."),
        _chunk("p-3", "We evaluate on SQuAD and TriviaQA."),
    ]
    summarizer = ScriptedSummarizer({"p-1": 1, "p-2": 2, "p-3": 9})
    model = FakePaperAnswerModel("SQuAD and TriviaQA [1].")

    answer = asyncio.run(_generator(model, summarizer).generate("Which datasets?", _retrieval(*chunks)))

    # All three candidates are scored even though only two passages fit the prompt.
    assert len(summarizer.requests[0].passages) == 3
    assert answer.context_chunk_ids[0] == "p-3"
    assert answer.cited_chunk_ids == ["p-3"]
    prompt = model.last_request.user_prompt
    assert "Summary: Summary of p-3." in prompt
    assert "Quote: " in prompt
    assert "Background" not in prompt
    selection = answer.context_metadata["evidence_selection"]
    assert selection["selected_ids"] == answer.context_chunk_ids
    assert selection["usage"]["input_tokens"] == 7
    assert answer.context_metadata["context_source_buckets"].keys() == set(answer.context_chunk_ids)
    assert answer.context_metadata["retrieved_chunk_ids"] == ["p-1", "p-2", "p-3"]


def test_generator_in_passage_mode_sends_original_text_of_reranked_evidence() -> None:
    chunks = [_chunk("p-1", "Unrelated."), _chunk("p-2", "We evaluate on SQuAD.")]
    summarizer = ScriptedSummarizer({"p-1": 0, "p-2": 9})
    model = FakePaperAnswerModel("SQuAD [1].")

    answer = asyncio.run(
        _generator(model, summarizer, max_chunks=1, render_mode="passage").generate(
            "Which datasets?", _retrieval(*chunks),
        )
    )

    assert answer.context_chunk_ids == ["p-2"]
    assert "We evaluate on SQuAD." in model.last_request.user_prompt
    assert "Summary:" not in model.last_request.user_prompt


def test_generator_names_the_pool_when_the_summarizer_fails() -> None:
    error = PaperAnswerModelError("provider_unavailable", "gateway down")
    model = FakePaperAnswerModel("unused")
    chunks = [_chunk("p-1", "A."), _chunk("p-2", "B.")]

    with pytest.raises(PaperAnswerModelError) as raised:
        asyncio.run(_generator(model, ScriptedSummarizer({}, error=error)).generate("Q?", _retrieval(*chunks)))

    assert raised.value.context_chunk_ids == ("p-1", "p-2")
    assert model.requests == []


def test_generator_rejects_a_pool_smaller_than_the_answer_context() -> None:
    with pytest.raises(ValueError, match="pool_size"):
        _generator(FakePaperAnswerModel("x"), ScriptedSummarizer({}), max_chunks=4, pool_size=2)
