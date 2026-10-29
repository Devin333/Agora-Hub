"""Per-search result size and context selection across replan rounds."""
from __future__ import annotations

from dataclasses import replace

from framework.harness import RAGBudget, RetrievalOperation, fake_rag_session_spec
from framework.harness.rag.models import EvidenceCandidate
from framework.harness.rag.planner import DeterministicRAGPlanner
from framework.harness.rag.session import _context_evidence


def _evidence(evidence_id: str, confidence: float) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=evidence_id,
        title=evidence_id,
        summary=f"summary of {evidence_id}",
        source_ref=f"source://{evidence_id}",
        span_refs=(f"span://{evidence_id}",),
        evidence_type="method",
        confidence=confidence,
        lineage=("paper",),
    )


def test_one_search_can_fill_the_context_window_regardless_of_query_budget() -> None:
    budget = RAGBudget(
        max_rounds=2, max_replans=1, max_queries=2, max_source_reads=8, max_memory_hits=4,
        max_context_items=8, max_context_tokens=2048, max_worker_calls=8,
    )
    spec = fake_rag_session_spec(budget=budget)

    plan = DeterministicRAGPlanner().plan(spec, round_index=0, gap_report={})

    searches = [step for step in plan.queries if step.operation == RetrievalOperation.SEARCH_CORPUS]
    assert [step.max_results for step in searches] == [8]
    narrow = DeterministicRAGPlanner().plan(
        replace(spec, budget=replace(budget, max_context_items=3)), round_index=0, gap_report={},
    )
    assert [step.max_results for step in narrow.queries] == [3]


def test_context_keeps_the_most_relevant_evidence_across_rounds() -> None:
    # Round one found weak evidence first; round two found stronger evidence later.
    accumulated = [
        _evidence("round1-a", 0.31), _evidence("round1-b", 0.30),
        _evidence("round2-a", 0.62), _evidence("round2-b", 0.55),
    ]

    selected = _context_evidence(accumulated, 3)

    assert [item.evidence_id for item in selected] == ["round2-a", "round2-b", "round1-a"]
    assert [item.evidence_id for item in accumulated][0] == "round1-a"


def test_context_selection_keeps_accumulation_order_for_equal_confidence() -> None:
    accumulated = [_evidence("first", 0.5), _evidence("second", 0.5), _evidence("third", 0.5)]

    assert [item.evidence_id for item in _context_evidence(accumulated, 2)] == ["first", "second"]
