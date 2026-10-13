from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness import (
    FakeMemoryPort,
    FakeRAGPlanner,
    FakeRAGSessionController,
    RAGBudget,
    RAGDecisionType,
    RAGSessionStatus,
    SourceVerifier,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
    fake_rag_session_spec,
)
from framework.harness.rag.fake import fake_reader_repair_memory, fake_research_evidence_packs
from framework.harness.rag.relevance import RelevanceScorerPort
from framework.harness.rag.models import AnswerClaim, GroundedAnswerCandidate, RAGContextPack
from framework.harness.rag.session import BoundedRAGSessionController
from framework.harness.retrieval.fake import FakeRetrievalPort
from framework.harness.retrieval.evidence_pack import EvidencePack


def test_bounded_rag_controller_returns_verified_context_pack() -> None:
    result = FakeRAGSessionController().run_fake_session()

    assert result.status == RAGSessionStatus.SUCCEEDED
    assert result.decision.decision_type == RAGDecisionType.RETURN_CONTEXT_PACK
    assert result.context_pack is not None
    assert result.budget_snapshot == result.context_pack.budget_snapshot
    assert result.budget_snapshot.context_items_used > 0
    assert result.budget_snapshot.context_tokens_used > 0
    assert result.context_pack.accepted_evidence[0].lineage
    assert "retrieval://fake/1" in result.context_pack.artifact_refs
    assert result.context_pack.metadata["artifact_refs"] == list(result.context_pack.artifact_refs)
    assert result.context_pack.evidence_trace[0]["status"] == "accepted"
    assert result.context_pack.evidence_trace[0]["span_refs"]
    assert result.context_pack.metadata["context_snapshot_ref"]


def test_controller_halts_when_query_budget_is_exhausted() -> None:
    spec = fake_rag_session_spec(
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=0,
            max_source_reads=4,
            max_memory_hits=4,
            max_context_items=4,
            max_context_tokens=1024,
            max_worker_calls=4,
        )
    )

    result = FakeRAGSessionController().run_fake_session(spec)

    assert result.status == RAGSessionStatus.HALTED
    assert result.decision.decision_type == RAGDecisionType.HALTED
    assert any(event["event_type"] == "rag_halted" for event in result.transcript.events)


def test_controller_replans_when_required_evidence_is_missing() -> None:
    spec = fake_rag_session_spec(required_evidence_types=("method", "limitation"))
    result = FakeRAGSessionController(
        retrieval=FakeRetrievalPort(fake_research_evidence_packs()[:1]),
        memory=FakeMemoryPort(fake_reader_repair_memory()),
    ).run_fake_session(spec)

    assert result.status == RAGSessionStatus.INSUFFICIENT_EVIDENCE
    assert result.context_pack is None
    assert [item.evidence_type for item in result.accepted_evidence] == ["method"]
    assert result.rejected_evidence == ()
    assert result.conflicting_evidence == ()
    assert result.gap_report["missing_evidence_types"] == ["limitation"]
    assert result.budget_snapshot.rounds_used == 2
    serialized = result.to_dict()
    assert serialized["accepted_evidence"][0]["evidence_type"] == "method"
    assert serialized["gap_report"]["missing_evidence_types"] == ["limitation"]
    assert serialized["budget_snapshot"] == result.budget_snapshot.to_dict()
    assert any(event["event_type"] == "rag_replanned" for event in result.transcript.events)
    assert result.decision.metadata["gap_report"]["missing_evidence_types"] == ["limitation"]


def test_controller_drops_memory_hits_outside_namespace() -> None:
    memory = FakeMemoryPort(
        (
            {
                "memory_ref": "memory://research.private/1",
                "namespace": "research.private",
                "summary": "This private note must not cross the namespace boundary.",
                "relevance": 0.99,
            },
            *fake_reader_repair_memory(),
        )
    )

    result = FakeRAGSessionController(memory=memory).run_fake_session()

    assert result.status == RAGSessionStatus.SUCCEEDED
    assert result.context_pack is not None
    assert all(item["namespace"] == "research.reader_repair" for item in result.context_pack.memory_context)


def test_controller_plan_failure_uses_replan_then_controlled_halt() -> None:
    bad_plan = RetrievalPlanCandidate(
        candidate_id="bad-plan",
        queries=(
            RetrievalStepSpec(
                step_id="bad-query",
                operation=RetrievalOperation.SEARCH_CORPUS,
                query="reader repair",
                corpus="unauthorized-corpus",
            ),
        ),
    )
    spec = fake_rag_session_spec(
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=2,
            max_source_reads=2,
            max_memory_hits=2,
            max_context_items=2,
            max_context_tokens=512,
            max_worker_calls=2,
        )
    )

    result = FakeRAGSessionController(planner=FakeRAGPlanner((bad_plan,))).run_fake_session(spec)

    assert result.status == RAGSessionStatus.HALTED
    assert result.decision.reason == "plan gate failed and no replan budget remains"
    assert any(event["payload"].get("gate") == "rag_tool_allowlist" for event in result.transcript.events if event["event_type"] == "rag_gate_failed")


def test_controller_gap_report_includes_low_relevance_rejection_summary() -> None:
    spec = fake_rag_session_spec(
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=2,
            max_source_reads=2,
            max_memory_hits=2,
            max_context_items=2,
            max_context_tokens=512,
            max_worker_calls=4,
        )
    )
    controller = BoundedRAGSessionController(
        retrieval=FakeRetrievalPort(fake_research_evidence_packs()[:1]),
        planner=FakeRAGPlanner(),
        memory=FakeMemoryPort(fake_reader_repair_memory()),
        source_verifier=SourceVerifier(relevance_scorer=_Scorer((0.1,))),
    )

    result = controller.run(spec)

    assert result.status == RAGSessionStatus.INSUFFICIENT_EVIDENCE
    summary = result.decision.metadata["gap_report"]["rejection_summary"]
    assert summary == {"low_relevance": {"count": 1, "evidence_types": {"method": 1}}}
    source_event = [event for event in result.transcript.events if event["event_type"] == "rag_source_verified"][0]
    assert source_event["payload"]["rejected"][0]["metadata"]["rejection_reason"] == "low_relevance"


def test_controller_propagates_tenant_scope_only_to_business_retrieval_request() -> None:
    base = fake_rag_session_spec()
    scope = {
        "paper_id": "paper-scope-a",
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "memory_namespace": "research:tenant:tenant-a:user:user-1",
    }
    spec = replace(
        base,
        goal=replace(base.goal, metadata={**base.goal.metadata, **scope}),
        source_policy={**base.source_policy, "tenant_id": "tenant-a"},
        metadata={**base.metadata, **scope},
    )
    retrieval = FakeRetrievalPort(fake_research_evidence_packs()[:1])
    controller = BoundedRAGSessionController(
        retrieval=retrieval,
        planner=FakeRAGPlanner(),
        memory=FakeMemoryPort(fake_reader_repair_memory()),
    )

    result = controller.run(spec)

    assert retrieval.requests[0].filters["paper_id"] == "paper-scope-a"
    assert retrieval.requests[0].filters["run_id"] == spec.run_id
    assert retrieval.requests[0].filters["tenant_id"] == "tenant-a"
    assert retrieval.requests[0].filters["user_id"] == "user-1"
    assert retrieval.requests[0].metadata["paper_id"] == "paper-scope-a"
    assert retrieval.requests[0].metadata["run_id"] == spec.run_id
    assert retrieval.requests[0].metadata["tenant_id"] == "tenant-a"
    assert retrieval.requests[0].metadata["user_id"] == "user-1"
    assert retrieval.requests[0].metadata["memory_namespace"] == "research:tenant:tenant-a:user:user-1"
    assert result.metrics is not None
    assert {
        "tenant_id",
        "user_id",
        "memory_namespace",
        "trace_id",
        "root_span_id",
        "context_pack_id",
    }.isdisjoint(result.metrics.to_dict())


@pytest.mark.parametrize(
    ("tenant_id", "visible_ids"),
    [
        (None, ["public"]),
        ("tenant-a", ["public", "tenant-a"]),
        ("tenant-b", ["public", "tenant-b"]),
    ],
)
def test_controller_filters_store_results_before_context_pack_assembly(
    tenant_id: str | None,
    visible_ids: list[str],
) -> None:
    base = fake_rag_session_spec()
    scope = {"tenant_id": tenant_id} if tenant_id else {}
    spec = replace(
        base,
        goal=replace(base.goal, metadata={**base.goal.metadata, **scope}),
        source_policy={**base.source_policy, **scope},
        generation_policy={"enabled": True, "max_attempts": 1},
        metadata={**base.metadata, **scope},
    )
    retrieval = FakeRetrievalPort(_tenant_evidence_packs())
    answer_worker = _PackAnswerWorker()
    controller = BoundedRAGSessionController(
        retrieval=retrieval,
        planner=FakeRAGPlanner(),
        answer_worker=answer_worker,
    )

    result = controller.run(spec)

    assert retrieval.requests[0].filters["tenant_id"] == tenant_id
    assert [item.evidence_id for item in result.accepted_evidence] == visible_ids
    assert result.context_pack is not None
    assert [item.evidence_id for item in result.context_pack.accepted_evidence] == visible_ids
    assert answer_worker.context_ids == [visible_ids]
    assert result.answer is not None
    assert list(result.answer.cited_evidence_ids) == visible_ids
    step_event = next(
        event for event in result.transcript.events if event["event_type"] == "rag_step_executed"
    )
    assert [item["evidence_id"] for item in step_event["payload"]["items"]] == visible_ids


class _Scorer(RelevanceScorerPort):
    def __init__(self, scores: tuple[float, ...]) -> None:
        self.scores = scores

    def score(self, question: str, passages: list[str]) -> list[float]:
        return list(self.scores)


class _PackAnswerWorker:
    def __init__(self) -> None:
        self.context_ids: list[list[str]] = []

    def generate_answer(self, *, question: str, pack: RAGContextPack) -> GroundedAnswerCandidate:
        evidence_ids = [item.evidence_id for item in pack.accepted_evidence]
        self.context_ids.append(evidence_ids)
        claims = tuple(
            AnswerClaim(
                claim_id=f"claim-{evidence_id}",
                text=f"Grounded by {evidence_id} evidence.",
                evidence_ids=(evidence_id,),
            )
            for evidence_id in evidence_ids
        )
        return GroundedAnswerCandidate(
            answer_id="answer-visibility",
            question=question,
            answer_text=" ".join(
                f"Grounded by {evidence_id} evidence. [{evidence_id}]"
                for evidence_id in evidence_ids
            ),
            cited_evidence_ids=tuple(evidence_ids),
            claims=claims,
        )


def _tenant_evidence_packs() -> tuple[EvidencePack, ...]:
    packs = []
    for scope in ("public", "tenant-a", "tenant-b"):
        metadata = {"section_refs": [f"section-{scope}"]}
        if scope != "public":
            metadata["tenant_id"] = scope
        packs.append(EvidencePack(
            evidence_id=scope,
            title=f"{scope} evidence",
            summary=f"Method evidence visible to {scope}.",
            source_refs=(f"source://{scope}",),
            confidence=0.9,
            freshness="recorded",
            lineage=("retrieval.visibility-test",),
            metadata=metadata,
        ))
    return tuple(packs)


class _FailBeforeSecondStepRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def record_event(self, *, spec, sequence, event_type, payload) -> None:
        del spec, sequence
        if event_type == "rag_step_execution_started" and payload.get("step_id") == "query-2":
            raise RuntimeError("durable journal unavailable")
        self.events.append((event_type, dict(payload)))


def test_durable_step_result_stops_before_next_retrieval_side_effect() -> None:
    spec = fake_rag_session_spec(
        budget=RAGBudget(
            max_rounds=1,
            max_replans=0,
            max_queries=2,
            max_source_reads=0,
            max_memory_hits=0,
            max_context_items=4,
            max_context_tokens=1024,
            max_worker_calls=2,
        )
    )
    plan = RetrievalPlanCandidate(
        candidate_id="two-step-plan",
        queries=(
            RetrievalStepSpec(
                step_id="query-1",
                operation=RetrievalOperation.SEARCH_CORPUS,
                query="first",
                corpus=spec.allowed_corpora[0],
                max_results=1,
                metadata={
                    "evidence_type": spec.goal.required_evidence_types[0],
                    "scope_ref": spec.source_policy["scope_ref"],
                },
            ),
            RetrievalStepSpec(
                step_id="query-2",
                operation=RetrievalOperation.SEARCH_CORPUS,
                query="second",
                corpus=spec.allowed_corpora[0],
                max_results=1,
                metadata={
                    "evidence_type": spec.goal.required_evidence_types[0],
                    "scope_ref": spec.source_policy["scope_ref"],
                },
            ),
        ),
    )
    retrieval = FakeRetrievalPort(fake_research_evidence_packs()[:1])
    recorder = _FailBeforeSecondStepRecorder()
    controller = BoundedRAGSessionController(
        retrieval=retrieval,
        planner=FakeRAGPlanner((plan,)),
        event_recorder=recorder,
    )

    with pytest.raises(RuntimeError, match="durable journal unavailable"):
        controller.run(spec)

    assert len(retrieval.requests) == 1
    assert any(
        event_type == "rag_step_executed" and payload.get("step_id") == "query-1"
        for event_type, payload in recorder.events
    )
