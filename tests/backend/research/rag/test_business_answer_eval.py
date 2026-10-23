from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation import business_answer_eval as business_answer_eval_module
from backend.research.rag.evaluation.business_answer_eval import run_business_answer_eval
from backend.research.rag.evaluation.business_question_bank import (
    BusinessQuestionBankError,
    load_business_question_bank,
)
from backend.research.rag.evaluation.evidence_eval_runner import live_answer_sample
from backend.research.rag.evaluation.golden_corpus_snapshot import load_golden_corpus_snapshot
from backend.research.rag.evaluation.paper_answer_eval import EvidenceAnswerEvaluator
from backend.research.rag.retrieval.paper_answer_generator import ABSTENTION_TOKEN
from tests.fixtures.paper_answer_model import FakePaperAnswerModel

BANK = Path("data/eval/business_question_bank_v1_draft.jsonl")
SNAPSHOT = Path("data/eval/golden_corpus_snapshot.json")
GOLDEN = Path("data/eval/golden_set.json")
_REAL_EQUIVALENT_PARENT_CHILD_PAIRS = (
    ("chunk_eb320183e948", "chunk_80edd06882c4"),
    ("chunk_34a0b45c1acd", "chunk_2b509ff3c83d"),
    ("chunk_d1d5933ef2f0", "chunk_f6a8eee4a2c6"),
)


def test_loader_scores_reviewed_single_paper_cases_and_reports_skips() -> None:
    bank = load_business_question_bank(BANK)

    assert bank.coverage() == {
        "total_rows": 89,
        "scored": 41,
        "skipped": 48,
        "scored_by_category": {"insufficient_evidence": 12, "single_paper": 29},
        "skipped_by_reason": {"needs_annotation": 48},
    }
    assert "single_007" not in {pair.metadata["business_case_id"] for pair in bank.pairs}
    abstain = [pair for pair in bank.pairs if pair.expected_behavior == "abstain"]
    assert len(abstain) == 12
    assert all(pair.gold_chunk_ids for pair in bank.pairs if pair.expected_behavior == "answer")


def _write_bank(tmp_path: Path, *rows: dict) -> Path:
    path = tmp_path / "bank.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _row(**overrides) -> dict:
    row = {
        "case_id": "c1",
        "category": "single_paper",
        "question": "What is shown?",
        "expected_behavior": "answer",
        "source_papers": ["p1"],
        "gold_evidence": {"gold_chunk_ids": ["chunk-1"]},
        "annotation_status": "reviewed",
    }
    row.update(overrides)
    return row


def _chunk(
    chunk_id: str,
    *,
    paper_id: str = "p1",
    parent_chunk_id: str | None = None,
    content: str = "same content",
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        parse_source="latex",
        parent_chunk_id=parent_chunk_id,
        content=content,
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"annotation_status": "mystery"}, "unknown annotation_status"),
        ({"gold_evidence": {"gold_chunk_ids": []}}, "has no gold_chunk_ids"),
        ({"expected_behavior": "maybe"}, "expected_behavior must be one of"),
        ({"question": ""}, "question is required"),
    ],
)
def test_loader_rejects_schema_violations(tmp_path: Path, overrides: dict, message: str) -> None:
    with pytest.raises(BusinessQuestionBankError, match=message):
        load_business_question_bank(_write_bank(tmp_path, _row(**overrides)))


def test_loader_rejects_duplicate_case_ids_and_skips_multi_paper(tmp_path: Path) -> None:
    with pytest.raises(BusinessQuestionBankError, match="duplicate case_id"):
        load_business_question_bank(_write_bank(tmp_path, _row(), _row()))

    bank = load_business_question_bank(
        _write_bank(tmp_path, _row(case_id="multi", source_papers=["p1", "p2"]))
    )
    assert bank.pairs == ()
    assert bank.coverage()["skipped_by_reason"] == {"multi_paper_unsupported": 1}


@pytest.mark.parametrize(
    ("parent_id", "child_id", "gold_is_parent"),
    [
        (parent_id, child_id, gold_is_parent)
        for parent_id, child_id in _REAL_EQUIVALENT_PARENT_CHILD_PAIRS
        for gold_is_parent in (True, False)
    ],
)
def test_loader_expands_all_real_parent_child_duplicates_in_both_directions(
    tmp_path: Path,
    parent_id: str,
    child_id: str,
    gold_is_parent: bool,
) -> None:
    snapshot = load_golden_corpus_snapshot(SNAPSHOT, golden_set_path=GOLDEN)
    chunks_by_id = {chunk.chunk_id: chunk for chunk in snapshot.chunks}
    parent = chunks_by_id[parent_id]
    child = chunks_by_id[child_id]
    assert child.parent_chunk_id == parent_id
    assert child.paper_id == parent.paper_id
    assert " ".join(child.content.split()) == " ".join(parent.content.split())
    gold_id, equivalent_id = (parent_id, child_id) if gold_is_parent else (child_id, parent_id)

    bank = load_business_question_bank(
        _write_bank(
            tmp_path,
            _row(
                source_papers=[parent.paper_id],
                gold_evidence={"gold_chunk_ids": [gold_id]},
            ),
        ),
        chunks=snapshot.chunks,
    )

    assert bank.pairs[0].gold_chunk_ids == [gold_id]
    assert bank.pairs[0].equivalent_gold_chunk_ids == [gold_id, equivalent_id]


def test_loader_equivalence_closure_normalizes_whitespace_and_resists_cycles(
    tmp_path: Path,
) -> None:
    chunks = (
        _chunk("chunk-a", parent_chunk_id="chunk-c", content="alpha  beta"),
        _chunk("chunk-b", parent_chunk_id="chunk-a", content="alpha\nbeta"),
        _chunk("chunk-c", parent_chunk_id="chunk-b", content=" alpha\t beta "),
    )
    bank = load_business_question_bank(
        _write_bank(tmp_path, _row(gold_evidence={"gold_chunk_ids": ["chunk-a"]})),
        chunks=chunks,
    )

    assert bank.pairs[0].gold_chunk_ids == ["chunk-a"]
    assert bank.pairs[0].equivalent_gold_chunk_ids == ["chunk-a", "chunk-b", "chunk-c"]


def test_loader_does_not_expand_non_equivalent_or_unrelated_chunks(tmp_path: Path) -> None:
    blank_child = PaperChunk.model_construct(
        chunk_id="blank-child",
        paper_id="p1",
        parse_source="latex",
        parent_chunk_id="gold",
        content="  \n\t ",
    )
    chunks = (
        _chunk("gold"),
        _chunk("different-child", parent_chunk_id="gold", content="different content"),
        _chunk("unrelated-same", content="same content"),
        _chunk("cross-paper-child", paper_id="p2", parent_chunk_id="gold"),
        blank_child,
    )
    bank = load_business_question_bank(
        _write_bank(tmp_path, _row(gold_evidence={"gold_chunk_ids": ["gold"]})),
        chunks=chunks,
    )

    assert bank.pairs[0].equivalent_gold_chunk_ids == ["gold"]


@pytest.mark.parametrize(
    ("gold_id", "chunks"),
    [("unknown", (_chunk("known"),)), ("known-without-corpus", None)],
)
def test_loader_preserves_gold_when_unknown_or_chunks_are_not_provided(
    tmp_path: Path,
    gold_id: str,
    chunks: tuple[PaperChunk, ...] | None,
) -> None:
    bank = load_business_question_bank(
        _write_bank(tmp_path, _row(gold_evidence={"gold_chunk_ids": [gold_id]})),
        chunks=chunks,
    )

    assert bank.pairs[0].gold_chunk_ids == [gold_id]
    assert bank.pairs[0].equivalent_gold_chunk_ids == [gold_id]


def test_single_001_child_citation_scores_as_equivalent_but_not_strict() -> None:
    snapshot = load_golden_corpus_snapshot(SNAPSHOT, golden_set_path=GOLDEN)
    bank = load_business_question_bank(BANK, chunks=snapshot.chunks)
    pair = next(pair for pair in bank.pairs if pair.metadata["business_case_id"] == "single_001")
    child_id = "chunk_80edd06882c4"

    score = EvidenceAnswerEvaluator().score(
        live_answer_sample(
            pair,
            {
                "status": "answered",
                "answer": "The table shows training, development, and test datapoint counts [1].",
                "passages": [{"chunk_id": child_id}],
                "citations": [{"chunk_id": child_id}],
            },
        )
    )

    assert pair.gold_chunk_ids == ["chunk_eb320183e948"]
    assert pair.equivalent_gold_chunk_ids == ["chunk_eb320183e948", child_id]
    assert score.answer_success is True
    assert score.citation_gold_coverage == 1.0
    assert score.strict_citation_gold_coverage == 0.0


def test_business_eval_records_outcomes_latency_and_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abstain_questions = {
        pair.question for pair in load_business_question_bank(BANK).pairs if pair.expected_behavior == "abstain"
    }
    received_chunks: list[PaperChunk] = []

    def load_with_snapshot(path: str | Path, *, chunks=None):
        received_chunks.extend(chunks or ())
        return load_business_question_bank(path, chunks=chunks)

    monkeypatch.setattr(business_answer_eval_module, "load_business_question_bank", load_with_snapshot)

    def reply(request) -> str:
        if any(question in request.user_prompt for question in abstain_questions):
            return f"{ABSTENTION_TOKEN}: the paper does not cover this"
        return "The paper reports the requested result [1]."

    ticks = itertools.count()
    run = run_business_answer_eval(
        question_bank_path=BANK,
        corpus_snapshot_path=SNAPSHOT,
        golden_set_path=GOLDEN,
        output_dir=tmp_path / "out",
        answer_model=FakePaperAnswerModel(reply),
        retrieval_policy="paper_blind_semantic_rag_v1",
        clock=lambda: float(next(ticks)),
    )

    assert len(received_chunks) == 35
    assert {chunk.chunk_id for chunk in received_chunks} >= {
        chunk_id for pair in _REAL_EQUIVALENT_PARENT_CHILD_PAIRS for chunk_id in pair
    }
    counts = run.answer.outcome_counts()
    assert sum(counts.values()) == 41
    assert run.answer.system_failure_rate() == 0.0
    assert run.models == ("fake-paper-answer-model",)
    assert run.efficiency["end_to_end_ms"] == {
        "n": 41,
        "p50": 1000.0,
        "p95": 1000.0,
        "max": 1000.0,
    }
    assert run.efficiency["model_calls_recorded"] > 0
    assert run.efficiency["tokens"]["output_tokens"] > 0
    assert run.efficiency["estimated_cost_usd"] is None
    assert run.efficiency["prompt_chars"]["n"] == run.efficiency["model_calls_recorded"]
    assert 0 < run.efficiency["prompt_chars"]["max"] <= 12_000 + 2_000

    rows = [
        json.loads(line)
        for line in (tmp_path / "out" / "answers.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 41
    assert {row["outcome"] for row in rows} <= {"answered", "abstained", "insufficient_evidence"}
    assert all(row["prompt_chars"] for row in rows if row["model_usage"])
    report = json.loads(
        (tmp_path / "out" / "evidence_regression_report.json").read_text(encoding="utf-8")
    )
    assert report["metadata"]["question_bank_coverage"]["scored"] == 41
    assert report["metadata"]["production_qualification"]["eligible"] is False


def test_business_eval_fails_closed_when_corpus_lacks_a_paper(tmp_path: Path) -> None:
    bank = _write_bank(tmp_path, _row(source_papers=["not-in-corpus"]))

    with pytest.raises(ValueError, match="lacks papers"):
        run_business_answer_eval(
            question_bank_path=bank,
            corpus_snapshot_path=SNAPSHOT,
            golden_set_path=GOLDEN,
            output_dir=tmp_path / "out",
            answer_model=FakePaperAnswerModel("unused"),
            retrieval_policy="paper_blind_semantic_rag_v1",
        )
