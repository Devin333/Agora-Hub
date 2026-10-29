from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.business_question_bank import load_business_question_bank
from backend.research.rag.evaluation.golden_corpus_snapshot import load_golden_corpus_snapshot
from backend.research.rag.retrieval.answer_context_budget import (
    ELISION,
    ContextLine,
    allocate_budgets,
    fit_lines,
    question_terms,
    render_lines,
)
from backend.research.rag.retrieval.contracts import RetrievalResult
from backend.research.rag.retrieval.paper_answer_generator import (
    CONTEXT_BUDGET_STRATEGY,
    AnswerGenerator,
)
from tests.fixtures.paper_answer_model import FakePaperAnswerModel


def test_allocate_budgets_sends_everything_when_it_fits() -> None:
    assert allocate_budgets([100, 900, 300], tiers=[0, 0, 1], total=2000, floor=50) == [100, 900, 300]


def test_allocate_budgets_water_fills_so_short_passages_stay_whole() -> None:
    budgets = allocate_budgets([100, 5000, 5000], tiers=[0, 0, 0], total=6000, floor=50)

    assert budgets == [100, 2950, 2950]
    assert sum(budgets) == 6000


def test_allocate_budgets_funds_primary_evidence_before_support() -> None:
    # Index 1 is primary (tier 0); index 0 is interpretation context (tier 1).
    budgets = allocate_budgets([3000, 3000], tiers=[1, 0], total=3500, floor=400)

    assert budgets == [500, 3000]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"lengths": [500, 500], "tiers": [0, 0], "total": 600, "floor": 400}, "cannot hold"),
        ({"lengths": [500], "tiers": [0, 1], "total": 600, "floor": 100}, "same size"),
        ({"lengths": [500], "tiers": [0], "total": 0, "floor": 100}, "positive"),
    ],
)
def test_allocate_budgets_rejects_impossible_budgets(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        allocate_budgets(**kwargs)


def _passage(*body_sentences: str) -> list[ContextLine]:
    return [
        ContextLine("Title", "Experiments", pinned=True),
        ContextLine("Body", " ".join(body_sentences)),
        ContextLine("Source locator", "arxiv://0000.00000/latex", pinned=True),
    ]


_FILLER = [
    "The results table summarizes overall accuracy across all benchmarks.",
    "Figure 2 reports the average score for every baseline in the table.",
    "We also report results on additional benchmarks in the appendix.",
    "Accuracy and scores improve steadily over the learning steps shown.",
]
_ANSWER = "The agent retried failed tasks until it produced three consecutive failed attempts."


def test_fit_lines_returns_text_unchanged_when_it_fits() -> None:
    lines = _passage(*_FILLER, _ANSWER)

    assert fit_lines(lines, max_chars=10_000, terms=frozenset()) == render_lines(lines)


def test_fit_lines_keeps_the_mid_passage_answer_over_generic_result_terms() -> None:
    lines = _passage(_FILLER[0], _FILLER[1], _ANSWER, _FILLER[2], _FILLER[3])
    terms = question_terms("What stopping condition was used when the agent retried failed tasks?")

    text = fit_lines(lines, max_chars=220, terms=terms)

    assert len(text) <= 220
    assert _ANSWER in text
    assert text.startswith("Title: Experiments")
    assert "Source locator: arxiv://0000.00000/latex" in text
    assert ELISION in text
    # The old renderer favoured these generic words; they must not displace the answer.
    assert "overall accuracy" not in text


def test_fit_lines_preserves_original_sentence_order() -> None:
    first = "Retries are allowed only for failed tasks in this setting."
    second = "The run stops after three consecutive failed attempts on a task."
    lines = _passage(first, *_FILLER, second)
    terms = question_terms("When does retrying failed tasks stop after consecutive attempts?")

    text = fit_lines(lines, max_chars=320, terms=terms)

    assert first in text and second in text
    assert text.index(first) < text.index(second)


def test_fit_lines_truncates_a_single_oversized_sentence_within_budget() -> None:
    lines = [ContextLine("Body", "word " * 400)]

    text = fit_lines(lines, max_chars=120, terms=frozenset({"word"}))

    assert len(text) <= 120
    assert text.endswith(ELISION)


def test_question_terms_drop_stopwords_and_question_scaffolding() -> None:
    terms = question_terms("What does the paper report about the stopping condition of retries?")

    assert "condition" in terms
    assert not {"what", "does", "the", "paper", "report", "about"} & terms


# Real-corpus regression: these five answer sentences were cut by the old fixed
# 1000-char head/tail window (2026-10-22 business question bank audit).
_AUDITED = {
    "single_019": "3 consecutive failed attempts",
    "single_023": "standard deviation",
    "single_026": "intended to provide a human baseline",
    "single_027": "integer value of 2",
    "single_028": "generated at the time the memory object is created",
}


@pytest.fixture(scope="module")
def corpus() -> tuple[dict, dict[str, PaperChunk]]:
    bank = load_business_question_bank(Path("data/eval/business_question_bank_v1_draft.jsonl"))
    snapshot = load_golden_corpus_snapshot(
        Path("data/eval/golden_corpus_snapshot.json"),
        golden_set_path=Path("data/eval/golden_set.json"),
    )
    pairs = {pair.metadata["business_case_id"]: pair for pair in bank.pairs}
    return pairs, {chunk.chunk_id: chunk for chunk in snapshot.chunks}


@pytest.mark.parametrize("case_id", sorted(_AUDITED))
def test_audited_answer_sentences_reach_the_model(corpus, case_id: str) -> None:
    pairs, chunks = corpus
    pair = pairs[case_id]
    model = FakePaperAnswerModel("Answer [1].")
    retrieval = RetrievalResult(
        parent_chunks=[],
        child_chunks=[chunks[chunk_id] for chunk_id in pair.gold_chunk_ids],
        ref_chunks=[],
        intent="concept_method",
    )

    answer = asyncio.run(AnswerGenerator(model).generate(pair.question, retrieval))

    assert _AUDITED[case_id] in model.last_request.user_prompt
    assert answer.context_metadata["context_budget"]["truncated_chunk_ids"] == []


def test_relevance_selection_keeps_the_trueskill_answer_under_a_tight_budget(corpus) -> None:
    pairs, chunks = corpus
    pair = pairs["single_023"]
    model = FakePaperAnswerModel("Answer [1].")
    retrieval = RetrievalResult(
        parent_chunks=[],
        child_chunks=[chunks[pair.gold_chunk_ids[0]]],
        ref_chunks=[],
        intent="concept_method",
    )

    answer = asyncio.run(
        AnswerGenerator(model, max_context_chunks=1, max_context_chars=600).generate(pair.question, retrieval)
    )

    assert len(answer.contexts[0]) <= 600
    assert "standard deviation" in answer.contexts[0]
    assert answer.context_metadata["context_budget"]["truncated_chunk_ids"] == [pair.gold_chunk_ids[0]]


def _chunk(chunk_id: str, content: str) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="latex",
        chunk_type="paragraph",
        content=content,
    )


def test_generator_records_per_passage_budget_and_prioritizes_primary_evidence() -> None:
    primary = _chunk("primary", "Primary evidence sentence about retries. " * 80)
    support = _chunk("support", "Supporting context sentence about setup. " * 80)
    retrieval = RetrievalResult(
        parent_chunks=[support],
        child_chunks=[primary],
        ref_chunks=[],
        intent="concept_method",
    )

    answer = asyncio.run(
        AnswerGenerator(FakePaperAnswerModel("Answer [1]."), max_context_chunks=2, max_context_chars=2400).generate(
            "How are retries handled?", retrieval
        )
    )

    budget = answer.context_metadata["context_budget"]
    passages = {item["chunk_id"]: item for item in budget["passages"]}
    assert budget["strategy"] == CONTEXT_BUDGET_STRATEGY
    assert budget["total_chars"] == 2400
    assert budget["kept_chars"] <= 2400
    assert set(budget["truncated_chunk_ids"]) == {"primary", "support"}
    assert passages["primary"]["budget_chars"] > passages["support"]["budget_chars"]
    for item in passages.values():
        assert item["kept_chars"] <= item["budget_chars"] < item["original_chars"]


def test_generator_rejects_a_budget_below_the_passage_floor() -> None:
    with pytest.raises(ValueError, match="must hold 8 passages"):
        AnswerGenerator(FakePaperAnswerModel("Answer [1]."), max_context_chars=1000)
