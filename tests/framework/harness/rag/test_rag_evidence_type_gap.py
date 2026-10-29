from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness import FakeMemoryPort, RAGBudget, RAGDecisionType, RAGSessionStatus, fake_rag_session_spec
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.fake import FakeRAGPlanner, fake_reader_repair_memory, fake_research_evidence_packs
from framework.harness.rag.gates import RAGGateResult, RAGGateSuite
from framework.harness.rag.models import (
    AnswerClaim,
    EvidenceCandidate,
    GroundedAnswerCandidate,
    RAGContextPack,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
    present_evidence_types,
)
from framework.harness.rag.policy import RAGExecutionPolicy
from framework.harness.rag.session import BoundedRAGSessionController
from framework.harness.rag.source_verifier import _with_tenant_rejection_metadata
from framework.harness.retrieval.fake import FakeRetrievalPort

_METHOD_EVIDENCE_ID = "evidence:sparse-mixture-reader:method"


class _Worker:
    def __init__(self) -> None:
        self.calls = 0

    def generate_answer(self, *, question: str, pack: RAGContextPack, execution_identity=None) -> GroundedAnswerCandidate:
        self.calls += 1
        return GroundedAnswerCandidate(
            answer_id=f"ans-{self.calls}",
            question=question,
            answer_text="The method retrieves evidence. [ev-1]",
            cited_evidence_ids=(_METHOD_EVIDENCE_ID,),
            claims=(
                AnswerClaim(
                    "c1",
                    "The method retrieves evidence.",
                    (_METHOD_EVIDENCE_ID,),
                    span_refs=("source://paper/sparse-mixture-reader#paragraph=method-3",),
                ),
            ),
        )


def _method_pack(*, extra_types: tuple[str, ...] = ()):
    """Evidence whose content resolves to ``method`` (plus any extra supported types)."""
    pack = fake_research_evidence_packs()[0]
    return replace(
        pack,
        metadata={
            **pack.metadata,
            "evidence_type": "method",
            "evidence_types": ["method", *extra_types],
            "evidence_type_source": "content_resolved",
        },
    )


def _spec(*, coverage: str | None, generation: bool = True, budget: RAGBudget | None = None):
    spec = fake_rag_session_spec(
        required_evidence_types=("limitation",),
        budget=budget
        or RAGBudget(
            max_rounds=2,
            max_replans=1,
            max_queries=4,
            max_source_reads=4,
            max_memory_hits=4,
            max_context_items=8,
            max_context_tokens=1024,
            max_worker_calls=8,
        ),
    )
    source_policy = dict(spec.source_policy)
    if coverage is not None:
        source_policy["evidence_type_coverage"] = coverage
    return replace(
        spec,
        source_policy=source_policy,
        generation_policy={"enabled": True} if generation else {},
    )


def _controller(worker, *, pack=None, planner=None, gates=None) -> BoundedRAGSessionController:
    return BoundedRAGSessionController(
        retrieval=FakeRetrievalPort((pack or _method_pack(),)),
        planner=planner or FakeRAGPlanner(),
        memory=FakeMemoryPort(fake_reader_repair_memory()),
        answer_worker=worker,
        gates=gates,
    )


def _events(result, event_type: str) -> list[dict]:
    return [event["payload"] for event in result.transcript.events if event["event_type"] == event_type]


def test_advisory_type_gap_over_accepted_evidence_goes_to_the_answer_worker() -> None:
    worker = _Worker()

    result = _controller(worker).run(_spec(coverage="advisory"))

    assert result.status == RAGSessionStatus.ANSWERED
    assert worker.calls == 1
    gap = _events(result, "rag_evidence_type_gap_accepted")
    assert gap == [
        {
            "missing_evidence_types": ["limitation"],
            "present_evidence_types": ["method"],
            "accepted_evidence_ids": [_METHOD_EVIDENCE_ID],
            "evidence_type_coverage": "advisory",
        }
    ]
    # The replan still ran before the gap was accepted.
    assert len(_events(result, "rag_replanned")) == 1
    assert result.metrics is not None and result.metrics.evidence_type_gap_accepted is True


def test_required_type_coverage_still_blocks_by_default() -> None:
    worker = _Worker()

    result = _controller(worker).run(_spec(coverage=None))

    assert result.status == RAGSessionStatus.INSUFFICIENT_EVIDENCE
    assert worker.calls == 0
    assert _events(result, "rag_evidence_type_gap_accepted") == []
    assert result.metrics is not None and result.metrics.evidence_type_gap_accepted is False


def test_advisory_gap_needs_generation_to_judge_it() -> None:
    worker = _Worker()

    result = _controller(worker).run(_spec(coverage="advisory", generation=False))

    assert result.status == RAGSessionStatus.INSUFFICIENT_EVIDENCE
    assert worker.calls == 0


class _LineageFailingGates(RAGGateSuite):
    def verify_sources(self, evidence, *, spec, policy, memory_context=()):
        return tuple(
            RAGGateResult(result.gate_name, False, "lineage missing")
            if result.gate_name == "rag_lineage"
            else result
            for result in super().verify_sources(evidence, spec=spec, policy=policy, memory_context=memory_context)
        )


def test_advisory_relaxes_only_the_type_requirement() -> None:
    worker = _Worker()

    result = _controller(worker, gates=_LineageFailingGates()).run(_spec(coverage="advisory"))

    assert result.status == RAGSessionStatus.INSUFFICIENT_EVIDENCE
    assert worker.calls == 0
    assert _events(result, "rag_evidence_type_gap_accepted") == []


def test_secondary_evidence_type_satisfies_required_coverage() -> None:
    worker = _Worker()

    result = _controller(worker, pack=_method_pack(extra_types=("limitation",))).run(_spec(coverage=None))

    assert result.status == RAGSessionStatus.ANSWERED
    assert _events(result, "rag_evidence_type_gap_accepted") == []
    assert len(_events(result, "rag_replanned")) == 0
    assert result.accepted_evidence[0].evidence_type == "method"
    assert result.accepted_evidence[0].evidence_types == ("method", "limitation")


def test_advisory_gap_is_accepted_when_queries_are_exhausted() -> None:
    repeated_plan = RetrievalPlanCandidate(
        candidate_id="repeated-plan",
        queries=(
            RetrievalStepSpec(
                step_id="repeated-query",
                operation=RetrievalOperation.SEARCH_CORPUS,
                query="reader repair limitation",
                corpus="research-papers",
                metadata={"scope_ref": "paper://sparse-mixture-reader", "evidence_type": "limitation"},
            ),
        ),
    )
    worker = _Worker()
    budget = RAGBudget(
        max_rounds=3,
        max_replans=1,
        max_queries=4,
        max_source_reads=4,
        max_memory_hits=4,
        max_context_items=8,
        max_context_tokens=1024,
        max_worker_calls=8,
    )

    result = _controller(worker, planner=FakeRAGPlanner((repeated_plan,))).run(
        _spec(coverage="advisory", budget=budget)
    )

    assert result.status == RAGSessionStatus.ANSWERED
    assert worker.calls == 1
    assert len(_events(result, "rag_evidence_type_gap_accepted")) == 1
    assert any(
        event.get("gate") == "rag_query_dedup" for event in _events(result, "rag_gate_failed")
    )


def test_unknown_evidence_type_coverage_mode_is_rejected() -> None:
    spec = _spec(coverage="lenient")

    with pytest.raises(HarnessValidationError, match="evidence_type_coverage"):
        RAGExecutionPolicy.from_session_spec(spec).evidence_type_coverage


def test_candidate_keeps_primary_type_first_and_round_trips_types() -> None:
    candidate = EvidenceCandidate.from_evidence_pack(_method_pack(extra_types=("experiment",)), evidence_type="limitation")

    assert candidate.evidence_type == "method"
    assert candidate.evidence_types == ("method", "experiment")
    assert present_evidence_types([candidate]) == {"method", "experiment"}
    restored = EvidenceCandidate.from_evidence_pack(candidate.to_evidence_pack(), evidence_type="limitation")
    assert restored.evidence_types == ("method", "experiment")
    assert candidate.to_dict()["evidence_types"] == ["method", "experiment"]


def test_requested_type_is_not_extended_by_stale_pack_types() -> None:
    pack = _method_pack(extra_types=("experiment",))
    unresolved = replace(pack, metadata={**pack.metadata, "evidence_type_source": "requested_fallback"})

    candidate = EvidenceCandidate.from_evidence_pack(unresolved, evidence_type="limitation")

    assert candidate.evidence_type == "limitation"
    assert candidate.evidence_types == ("limitation",)


def test_source_verifier_rejection_preserves_every_evidence_type() -> None:
    candidate = EvidenceCandidate.from_evidence_pack(_method_pack(extra_types=("experiment",)), evidence_type="method")

    rejected = _with_tenant_rejection_metadata(candidate, violation={"reason": "other tenant"})

    assert rejected.evidence_types == ("method", "experiment")
    assert rejected.metadata["rejection_reason"] == "tenant_scope_violation"
