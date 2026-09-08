from __future__ import annotations

from dataclasses import replace

from framework.harness import FakeMemoryPort, RAGBudget, RAGDecisionType, RAGSessionStatus, fake_rag_session_spec
from framework.harness.rag.fake import FakeRAGPlanner, fake_reader_repair_memory, fake_research_evidence_packs
from framework.harness.rag.models import AnswerClaim, GroundedAnswerCandidate, RAGContextPack
from framework.harness.rag.session import BoundedRAGSessionController
from framework.harness.retrieval.evidence_pack import EvidencePack, EvidencePackCollection
from framework.harness.retrieval.fake import FakeRetrievalPort
from framework.harness.retrieval.request import RetrievalRequest


def test_generation_phase_is_disabled_by_default() -> None:
    worker = _AnswerWorker(_grounded_answer())
    controller = _controller(worker)

    result = controller.run(fake_rag_session_spec())

    assert result.status == RAGSessionStatus.SUCCEEDED
    assert result.decision.decision_type == RAGDecisionType.RETURN_CONTEXT_PACK
    assert result.answer is None
    assert worker.calls == []


def test_generation_phase_returns_answered_when_answer_gate_passes() -> None:
    worker = _AnswerWorker(_grounded_answer())
    controller = _controller(worker)

    result = controller.run(_generation_spec())

    assert result.status == RAGSessionStatus.ANSWERED
    assert result.decision.decision_type == RAGDecisionType.RETURN_ANSWER
    assert result.answer is not None
    assert result.answer.answer_text == "The method retrieves evidence. [ev-1]"
    assert any(event["event_type"] == "rag_answer_returned" for event in result.transcript.events)
    assert result.metrics is not None
    assert result.metrics.status == RAGSessionStatus.ANSWERED
    assert result.metrics.decision_type == RAGDecisionType.RETURN_ANSWER.value
    assert result.metrics.transcript_event_count == len(result.transcript.events)
    assert result.metrics.budget_snapshot["rounds_used"] == 1
    assert result.metrics.accepted_evidence_count == 1
    assert result.metrics.answer_attempts == 1
    assert result.metrics.answer_present is True
    assert result.metrics.gate_failures_count == 0
    assert result.to_dict()["metrics"]["status"] == "answered"


def test_generation_phase_forwards_physical_execution_identity() -> None:
    worker = _AnswerWorker(_grounded_answer())
    base_spec = _generation_spec()
    spec = replace(
        base_spec,
        graph_identity=base_spec.graph_identity.with_physical_activity(
            node_id=base_spec.graph_identity.stage_id,
            node_instance_id="build-evidence-context:1",
            activity_id="activity-1",
            activity_attempt=2,
        ),
    )

    result = _controller(
        worker, memory=_ExecutionBoundMemory(spec.graph_identity.to_graph_execution_identity()),
    ).run(spec)

    assert result.status == RAGSessionStatus.ANSWERED
    assert worker.execution_identities == [spec.graph_identity.to_graph_execution_identity()]


def test_generation_phase_abstains_when_answer_gate_fails() -> None:
    candidate = GroundedAnswerCandidate(
        answer_id="ans-1",
        question="Q?",
        answer_text="Unsupported answer.",
        cited_evidence_ids=("missing",),
        claims=(AnswerClaim("c1", "Unsupported answer.", ("missing",)),),
    )
    result = _controller(_AnswerWorker(candidate)).run(_generation_spec())

    assert result.status == RAGSessionStatus.ABSTAINED
    assert result.decision.decision_type == RAGDecisionType.ABSTAIN
    assert result.decision.reason == "answer gate failed"
    assert any(item["gate"] == "rag_answer_citation_integrity" for item in result.decision.gate_results)
    assert result.metrics is not None
    assert result.metrics.status == RAGSessionStatus.ABSTAINED
    assert result.metrics.answer_attempts == 1
    assert result.metrics.gate_failures_by_gate == {"rag_answer_citation_integrity": 1}
    assert result.metrics.answer_present is True
    assert result.metrics.answer_abstained is False


def test_generation_phase_returns_verified_abstention() -> None:
    candidate = GroundedAnswerCandidate(
        answer_id="ans-1",
        question="Q?",
        answer_text="",
        cited_evidence_ids=(),
        claims=(),
        abstained=True,
    )
    result = _controller(_AnswerWorker(candidate)).run(_generation_spec())

    assert result.status == RAGSessionStatus.ABSTAINED
    assert result.decision.reason == "answer worker produced a verified abstention"
    assert result.answer is not None
    assert result.answer.abstained is True


def test_generation_phase_retries_after_supplemental_round_and_returns_answered() -> None:
    packs = fake_research_evidence_packs()
    retrieval = _SequentialRetrievalPort(((packs[0],), (packs[1],)))
    worker = _AnswerWorker(
        _unsupported_answer("ans-1"),
        _grounded_answer(
            answer_id="ans-2",
            answer_text="The repair accuracy is reported in the experiment table. [ev-2]",
            evidence_id="evidence:sparse-mixture-reader:experiment",
            claim_text="The repair accuracy is reported in the experiment table.",
        ),
    )

    result = _controller(worker, retrieval=retrieval).run(
        _generation_spec(generation_policy={"enabled": True, "max_attempts": 2})
    )

    assert result.status == RAGSessionStatus.ANSWERED
    assert result.decision.decision_type == RAGDecisionType.RETURN_ANSWER
    assert len(worker.calls) == 2
    assert len(retrieval.requests) == 2
    assert result.context_pack is not None
    assert "evidence:sparse-mixture-reader:experiment" in {
        item.evidence_id for item in result.context_pack.accepted_evidence
    }
    assert "The experiment table reports repair accuracy." in retrieval.requests[1].query
    assert any(event["event_type"] == "rag_answer_supplemental_gap_created" for event in result.transcript.events)
    assert any(event["event_type"] == "rag_answer_supplemental_round_completed" for event in result.transcript.events)
    assert result.metrics is not None
    assert result.metrics.answer_attempts == 2
    assert result.metrics.supplemental_rounds_started == 1
    assert result.metrics.supplemental_rounds_completed == 1
    assert result.metrics.gate_failures_by_gate == {"rag_answer_claim_coverage": 1}


def test_generation_phase_abstains_when_supplemental_retry_still_fails() -> None:
    packs = fake_research_evidence_packs()
    retrieval = _SequentialRetrievalPort(((packs[0],), (packs[1],)))
    worker = _AnswerWorker(
        _unsupported_answer("ans-1"),
        _unsupported_answer("ans-2"),
    )

    result = _controller(worker, retrieval=retrieval).run(
        _generation_spec(generation_policy={"enabled": True, "max_attempts": 2})
    )

    assert result.status == RAGSessionStatus.ABSTAINED
    assert result.decision.decision_type == RAGDecisionType.ABSTAIN
    assert len(worker.calls) == 2
    assert len(retrieval.requests) == 2
    assert any(item["gate"] == "rag_answer_claim_coverage" for item in result.decision.gate_results)
    assert any(event["event_type"] == "rag_answer_supplemental_round_completed" for event in result.transcript.events)


def test_generation_phase_supplemental_round_uses_independent_budget() -> None:
    packs = fake_research_evidence_packs()
    retrieval = _SequentialRetrievalPort(((packs[0],), (packs[1],)))
    worker = _AnswerWorker(
        _unsupported_answer("ans-1"),
        _grounded_answer(
            answer_id="ans-2",
            answer_text="The repair accuracy is reported in the experiment table. [ev-2]",
            evidence_id="evidence:sparse-mixture-reader:experiment",
            claim_text="The repair accuracy is reported in the experiment table.",
        ),
    )
    budget = RAGBudget(
        max_rounds=1,
        max_replans=0,
        max_queries=2,
        max_source_reads=2,
        max_memory_hits=8,
        max_context_items=8,
        max_context_tokens=2048,
        max_worker_calls=8,
    )

    result = _controller(worker, retrieval=retrieval).run(
        _generation_spec(
            budget=budget,
            generation_policy={
                "enabled": True,
                "max_attempts": 2,
                "max_supplemental_rounds": 1,
            },
        )
    )

    assert result.status == RAGSessionStatus.ANSWERED
    assert len(worker.calls) == 2
    assert len(retrieval.requests) == 2
    assert result.metrics is not None
    assert result.metrics.supplemental_rounds_started == 1
    assert result.metrics.supplemental_rounds_completed == 1
    assert result.metrics.budget_snapshot["rounds_used"] == 1
    assert result.metrics.budget_snapshot["replans_used"] == 0


def test_generation_phase_does_not_retry_when_supplemental_budget_is_exhausted() -> None:
    retrieval = _SequentialRetrievalPort(((fake_research_evidence_packs()[0],),))
    worker = _AnswerWorker(_unsupported_answer("ans-1"))
    budget = RAGBudget(
        max_rounds=1,
        max_replans=0,
        max_queries=2,
        max_source_reads=2,
        max_memory_hits=2,
        max_context_items=4,
        max_context_tokens=1024,
        max_worker_calls=4,
    )

    result = _controller(worker, retrieval=retrieval).run(
        _generation_spec(
            budget=budget,
            generation_policy={
                "enabled": True,
                "max_attempts": 2,
                "max_supplemental_rounds": 0,
            },
        )
    )

    assert result.status == RAGSessionStatus.ABSTAINED
    assert len(worker.calls) == 1
    assert len(retrieval.requests) == 1
    assert any(event["event_type"] == "rag_answer_supplemental_gap_created" for event in result.transcript.events)
    skipped = [
        event
        for event in result.transcript.events
        if event["event_type"] == "rag_answer_supplemental_round_skipped"
    ]
    assert skipped
    assert skipped[0]["payload"]["reason_code"] == "supplemental_round_budget_exhausted"
    assert result.metrics is not None
    assert result.metrics.supplemental_rounds_skipped == 1
    assert result.metrics.supplemental_round_skip_reasons == {"supplemental_round_budget_exhausted": 1}
    assert result.to_dict()["metrics"]["supplemental_round_skip_reasons"] == {
        "supplemental_round_budget_exhausted": 1
    }
    assert result.metrics.gate_failures_by_gate == {"rag_answer_claim_coverage": 1}


def _controller(
    worker: _AnswerWorker,
    *,
    retrieval: FakeRetrievalPort | _SequentialRetrievalPort | None = None,
    memory: FakeMemoryPort | None = None,
) -> BoundedRAGSessionController:
    return BoundedRAGSessionController(
        retrieval=retrieval or FakeRetrievalPort(fake_research_evidence_packs()[:1]),
        planner=FakeRAGPlanner(),
        memory=memory or FakeMemoryPort(fake_reader_repair_memory()),
        answer_worker=worker,
    )


class _ExecutionBoundMemory(FakeMemoryPort):
    """Keep the identity propagation fixture bound to its physical caller."""

    def __init__(self, execution_identity):
        super().__init__(fake_reader_repair_memory())
        self.execution_identity = execution_identity

    def validate_execution(self, execution_identity):
        assert execution_identity == self.execution_identity


def _generation_spec(
    *,
    generation_policy: dict[str, object] | None = None,
    budget: RAGBudget | None = None,
):
    spec = fake_rag_session_spec()
    return type(spec)(
        session_id=spec.session_id,
        graph_identity=spec.graph_identity,
        goal=spec.goal,
        allowed_corpora=spec.allowed_corpora,
        allowed_memory_namespaces=spec.allowed_memory_namespaces,
        allowed_tools=spec.allowed_tools,
        source_policy=spec.source_policy,
        budget=budget or spec.budget,
        context_policy=spec.context_policy,
        generation_policy=generation_policy or {"enabled": True},
        metadata=spec.metadata,
    )


def _grounded_answer(
    *,
    answer_id: str = "ans-1",
    answer_text: str = "The method retrieves evidence. [ev-1]",
    evidence_id: str = "evidence:sparse-mixture-reader:method",
    claim_text: str = "The method retrieves evidence.",
) -> GroundedAnswerCandidate:
    return GroundedAnswerCandidate(
        answer_id=answer_id,
        question="Q?",
        answer_text=answer_text,
        cited_evidence_ids=(evidence_id,),
        claims=(
            AnswerClaim(
                "c1",
                claim_text,
                (evidence_id,),
                span_refs=(_span_ref_for_evidence_id(evidence_id),),
            ),
        ),
    )


def _unsupported_answer(answer_id: str) -> GroundedAnswerCandidate:
    return GroundedAnswerCandidate(
        answer_id=answer_id,
        question="Q?",
        answer_text="Unsupported answer.",
        cited_evidence_ids=(),
        claims=(AnswerClaim("c1", "The experiment table reports repair accuracy.", ()),),
    )


def _span_ref_for_evidence_id(evidence_id: str) -> str:
    if evidence_id.endswith(":experiment"):
        return "source://paper/sparse-mixture-reader#table=2"
    return "source://paper/sparse-mixture-reader#paragraph=method-3"


class _AnswerWorker:
    def __init__(self, *candidates: GroundedAnswerCandidate) -> None:
        self.candidates = list(candidates)
        self.calls: list[tuple[str, str]] = []
        self.execution_identities = []

    def generate_answer(self, *, question: str, pack: RAGContextPack, execution_identity=None) -> GroundedAnswerCandidate:
        self.calls.append((question, pack.pack_id))
        self.execution_identities.append(execution_identity)
        index = min(len(self.calls) - 1, len(self.candidates) - 1)
        return self.candidates[index]


class _SequentialRetrievalPort:
    def __init__(self, rounds: tuple[tuple[EvidencePack, ...], ...]) -> None:
        self.rounds = rounds
        self.requests: list[RetrievalRequest] = []

    def retrieve(self, request: RetrievalRequest) -> EvidencePackCollection:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self.rounds) - 1)
        return EvidencePackCollection(
            packs=self.rounds[index][: request.limit],
            request_ref=f"retrieval://sequential/{len(self.requests)}",
            metadata={"query": request.query, "scope": request.scope},
        )
