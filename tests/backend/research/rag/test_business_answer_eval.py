from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from backend.research.rag.evaluation.business_answer_eval import run_business_answer_eval
from backend.research.rag.evaluation.business_question_bank import (
    BusinessQuestionBankError,
    load_business_question_bank,
)
from backend.research.rag.retrieval.paper_answer_generator import ABSTENTION_TOKEN
from tests.fixtures.paper_answer_model import FakePaperAnswerModel

BANK = Path("data/eval/business_question_bank_v1_draft.jsonl")
SNAPSHOT = Path("data/eval/golden_corpus_snapshot.json")
GOLDEN = Path("data/eval/golden_set.json")


def test_loader_scores_reviewed_single_paper_cases_and_reports_skips() -> None:
    bank = load_business_question_bank(BANK)

    assert bank.coverage() == {
        "total_rows": 90,
        "scored": 42,
        "skipped": 48,
        "scored_by_category": {"insufficient_evidence": 12, "single_paper": 30},
        "skipped_by_reason": {"needs_annotation": 48},
    }
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


def test_business_eval_records_outcomes_latency_and_tokens(tmp_path: Path) -> None:
    abstain_questions = {
        pair.question for pair in load_business_question_bank(BANK).pairs if pair.expected_behavior == "abstain"
    }

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

    counts = run.answer.outcome_counts()
    assert sum(counts.values()) == 42
    assert run.answer.system_failure_rate() == 0.0
    assert run.models == ("fake-paper-answer-model",)
    assert run.efficiency["end_to_end_ms"] == {"n": 42, "p50": 1000.0, "p95": 1000.0, "max": 1000.0}
    assert run.efficiency["model_calls_recorded"] > 0
    assert run.efficiency["tokens"]["output_tokens"] > 0
    assert run.efficiency["estimated_cost_usd"] is None
    assert run.efficiency["prompt_chars"]["n"] == run.efficiency["model_calls_recorded"]
    assert 0 < run.efficiency["prompt_chars"]["max"] <= 12_000 + 2_000

    rows = [json.loads(line) for line in (tmp_path / "out" / "answers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 42
    assert {row["outcome"] for row in rows} <= {"answered", "abstained", "insufficient_evidence"}
    assert all(row["prompt_chars"] for row in rows if row["model_usage"])
    report = json.loads((tmp_path / "out" / "evidence_regression_report.json").read_text(encoding="utf-8"))
    assert report["metadata"]["question_bank_coverage"]["scored"] == 42
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
