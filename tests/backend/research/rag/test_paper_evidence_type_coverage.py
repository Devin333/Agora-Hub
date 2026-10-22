from __future__ import annotations

from pathlib import Path

import pytest

from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.paper_rag_session import PaperRAGSession
from backend.research.graphs import build_paper_analysis_context_graph_identity
from backend.research.rag.evaluation import evidence_eval_runner
from backend.research.rag.evaluation.business_question_bank import load_business_question_bank
from backend.research.rag.evaluation.golden_corpus_snapshot import load_golden_corpus_snapshot
from backend.research.services.rag_policy import ResearchRAGPolicyBuilder
from tests.fixtures.paper_answer_model import FakePaperAnswerModel


def _identity():
    return build_paper_analysis_context_graph_identity(run_id="run-1", stage_id="run_research_rag")


def _goal():
    return AskPaperUseCase().build_paper_ask_goal(paper_id="p1", question="What is the method?")


def test_policy_builder_requires_evidence_types_by_default() -> None:
    spec = ResearchRAGPolicyBuilder().build_session_spec(graph_identity=_identity(), session_id="s1", goal=_goal())

    assert spec.source_policy["evidence_type_coverage"] == "required"


def test_policy_builder_rejects_unknown_coverage_mode() -> None:
    with pytest.raises(ValueError, match="evidence_type_coverage"):
        ResearchRAGPolicyBuilder().build_session_spec(
            graph_identity=_identity(), session_id="s1", goal=_goal(), evidence_type_coverage="lenient",
        )


def test_paper_rag_session_uses_advisory_evidence_type_coverage(monkeypatch) -> None:
    captured = {}
    session = PaperRAGSession(chunk_store=object())  # type: ignore[arg-type]
    monkeypatch.setattr(session, "run_spec", lambda spec, **_: captured.setdefault("spec", spec))

    session.run(_goal(), graph_identity=_identity(), session_id="s1")

    assert captured["spec"].source_policy["evidence_type_coverage"] == "advisory"


# 2026-10-22 audit: gold evidence was retrieved and accepted in all six cases, but the
# type gate blocked generation. 029/030 carried both roles (fixed by multi-type
# resolution); the others are heuristic intent/type mismatches (advisory coverage).
_RESOLVED_BY_MULTI_TYPE = {"single_029", "single_030"}
_RESOLVED_BY_ADVISORY = {"single_011", "single_012", "single_014", "single_021"}


def test_type_gated_business_cases_reach_the_answer_model() -> None:
    bank = {
        pair.metadata["business_case_id"]: pair
        for pair in load_business_question_bank(Path("data/eval/business_question_bank_v1_draft.jsonl")).pairs
    }
    snapshot = load_golden_corpus_snapshot(
        Path("data/eval/golden_corpus_snapshot.json"),
        golden_set_path=Path("data/eval/golden_set.json"),
    )
    model = FakePaperAnswerModel("Answer supported by [1].")
    ask = evidence_eval_runner.build_in_memory_live_answer_ask(
        list(snapshot.chunks),
        retrieval_policy="paper_blind_semantic_rag_v1",
        visual_enabled=False,
        image_root=None,
        lightweight_reranker=False,
        limit=8,
        answer_model=model,
    )

    for case_id in sorted(_RESOLVED_BY_MULTI_TYPE | _RESOLVED_BY_ADVISORY):
        payload = ask(bank[case_id])
        metrics = payload["metrics"]
        assert payload["status"] == "answered", case_id
        assert metrics["evidence_type_gap_accepted"] is (case_id in _RESOLVED_BY_ADVISORY), case_id
        if case_id in _RESOLVED_BY_MULTI_TYPE:
            assert "rag_evidence_coverage" not in metrics["gate_failures_by_gate"], case_id
    assert len(model.requests) == 6
