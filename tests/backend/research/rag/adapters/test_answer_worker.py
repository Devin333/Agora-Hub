from __future__ import annotations

import pytest

from backend.research.document.models import PaperChunk
from backend.research.ports.paper_answer_model import PaperAnswerModelError
from backend.research.rag.adapters.answer_worker import PaperAnswerWorker
from backend.research.rag.retrieval.paper_answer_generator import ABSTENTION_TOKEN, AnswerGenerator
from framework.harness.rag.answer_gate import RAGAnswerGate
from framework.harness.rag.answer_worker import AnswerWorkerError
from framework.harness.rag.fake import fake_rag_session_spec
from framework.harness.rag.models import EvidenceCandidate, RAGContextPack
from tests.fixtures.paper_answer_model import FakePaperAnswerModel


def test_paper_answer_worker_maps_generated_chunk_ids_to_evidence_ids() -> None:
    model = FakePaperAnswerModel("The paper uses a retrieval method supported by context [1].")
    worker = PaperAnswerWorker(AnswerGenerator(model, max_context_chunks=1))
    pack = _pack(_evidence("ev-method", _chunk("chunk-method", "Method evidence: the system retrieves evidence.")))

    candidate = worker.generate_answer(question="What method does the paper use?", pack=pack)

    assert "Method evidence" in model.last_request.user_prompt
    assert candidate.abstained is False
    assert candidate.answer_text == "The paper uses a retrieval method supported by context [1]."
    assert candidate.cited_evidence_ids == ("ev-method",)
    assert candidate.unresolved_citations == ()
    assert candidate.claims[0].evidence_ids == ("ev-method",)
    assert candidate.claims[0].span_refs == ("chunk-method",)
    assert candidate.metadata["context_chunk_ids"] == ["chunk-method"]
    assert candidate.metadata["chunk_to_evidence_id"] == {"chunk-method": "ev-method"}
    assert candidate.metadata["evidence_id_to_span_refs"] == {"ev-method": ["chunk-method"]}
    assert candidate.metadata["claims_degraded"] is True
    assert candidate.metadata["model"] == "fake-paper-answer-model"
    assert candidate.metadata["model_usage"]["output_tokens"] > 0
    assert all(result.passed for result in RAGAnswerGate().evaluate(candidate, pack))


def test_paper_answer_worker_forwards_physical_execution_identity_to_model() -> None:
    model = FakePaperAnswerModel("The paper uses a retrieval method supported by context [1].")
    worker = PaperAnswerWorker(AnswerGenerator(model, max_context_chunks=1))
    base_spec = fake_rag_session_spec()
    identity = (
        base_spec.graph_identity.with_physical_activity(
            node_id=base_spec.graph_identity.stage_id,
            node_instance_id="build-evidence-context:1",
            activity_id="activity-1",
            activity_attempt=1,
        )
        .to_graph_execution_identity()
    )

    candidate = worker.generate_answer(
        question="What method does the paper use?",
        pack=_pack(_evidence("ev-method", _chunk("chunk-method", "Method evidence."))),
        execution_identity=identity,
    )

    assert candidate.abstained is False
    assert model.last_request.execution_identity == identity


def test_paper_answer_worker_can_rebuild_chunk_from_flat_evidence_metadata() -> None:
    model = FakePaperAnswerModel("The table shows stronger results for the proposed model [1].")
    worker = PaperAnswerWorker(AnswerGenerator(model, max_context_chunks=1))
    evidence = EvidenceCandidate(
        evidence_id="ev-table",
        title="Table",
        summary="Table evidence: proposed model improves accuracy.",
        source_ref="paper://p1/pdf#page=4",
        span_refs=("chunk-table",),
        evidence_type="experiment",
        confidence=0.9,
        lineage=("retrieval.test",),
        metadata={
            "rag_chunk_id": "chunk-table",
            "paper_id": "p1",
            "parse_source": "pymupdf",
            "chunk_type": "table",
            "has_table": True,
            "section_title": "Experiments",
            "section_role": ["experiment"],
            "content": "Table evidence: proposed model improves accuracy.",
        },
    )

    candidate = worker.generate_answer(question="What do results show?", pack=_pack(evidence))

    assert "Table evidence" in model.last_request.user_prompt
    assert candidate.abstained is False
    assert candidate.cited_evidence_ids == ("ev-table",)
    assert candidate.claims[0].span_refs == ("chunk-table",)
    assert candidate.metadata["context_chunk_ids"] == ["chunk-table"]


def test_paper_answer_worker_fails_when_context_lacks_paper_chunk_metadata() -> None:
    model = FakePaperAnswerModel("unused")
    worker = PaperAnswerWorker(AnswerGenerator(model))
    pack = _pack(
        EvidenceCandidate(
            evidence_id="ev-1",
            title="Unstructured evidence",
            summary="A short summary without paper ids.",
            source_ref="source://x",
            span_refs=("span-1",),
            evidence_type="method",
            confidence=0.9,
            lineage=("retrieval.test",),
        )
    )

    with pytest.raises(AnswerWorkerError) as raised:
        worker.generate_answer(question="What method does the paper use?", pack=pack)

    assert raised.value.code == "context_pack_not_projectable"
    assert model.requests == []


def test_paper_answer_worker_returns_model_declared_abstention() -> None:
    model = FakePaperAnswerModel(f"{ABSTENTION_TOKEN}: the paper does not report weather measurements")
    worker = PaperAnswerWorker(AnswerGenerator(model, max_context_chunks=1))
    pack = _pack(_evidence("ev-weather", _chunk("chunk-weather", "The paper discusses retrieval models.")))

    candidate = worker.generate_answer(
        question="Does this paper report weather measurements from Shanghai?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.cited_evidence_ids == ()
    assert candidate.claims == ()
    assert candidate.metadata["abstention_source"] == "model"
    assert candidate.metadata["abstention_reason"] == "the paper does not report weather measurements"
    assert all(result.passed for result in RAGAnswerGate().evaluate(candidate, pack))


def test_paper_answer_worker_does_not_reclassify_hedged_answers_as_abstentions() -> None:
    reply = "No. The provided context contains no mention of weather measurements [1]."
    worker = PaperAnswerWorker(AnswerGenerator(FakePaperAnswerModel(reply), max_context_chunks=1))
    pack = _pack(_evidence("ev-weather", _chunk("chunk-weather", "The paper discusses retrieval models.")))

    candidate = worker.generate_answer(
        question="Does this paper report weather measurements from Shanghai?",
        pack=pack,
    )

    assert candidate.abstained is False
    assert candidate.answer_text == reply
    assert candidate.cited_evidence_ids == ("ev-weather",)


def test_paper_answer_worker_keeps_answers_to_negative_presence_questions_verbatim() -> None:
    reply = "Axial attention performs attention along one axis at a time [1]."
    worker = PaperAnswerWorker(AnswerGenerator(FakePaperAnswerModel(reply), max_context_chunks=1))
    pack = _pack(_evidence("ev-attention", _chunk("chunk-attention", "Axial attention evidence.")))

    candidate = worker.generate_answer(
        question="Does this paper specify the launch date of a commercial smartphone product?",
        pack=pack,
    )

    assert candidate.abstained is False
    assert candidate.answer_text == reply
    assert "negative_presence_relevance" not in candidate.metadata


def test_paper_answer_worker_discloses_only_explicitly_cited_evidence() -> None:
    model = FakePaperAnswerModel("The second context supports the answer [2].")
    worker = PaperAnswerWorker(AnswerGenerator(model, max_context_chunks=2))
    pack = _pack(
        _evidence("ev-first", _chunk("chunk-first", "First evidence.")),
        _evidence("ev-second", _chunk("chunk-second", "Second evidence.")),
    )

    candidate = worker.generate_answer(question="Which context supports the answer?", pack=pack)

    assert "First evidence" in model.last_request.user_prompt
    assert "Second evidence" in model.last_request.user_prompt
    assert candidate.cited_evidence_ids == ("ev-second",)
    assert candidate.claims[0].evidence_ids == ("ev-second",)
    assert candidate.claims[0].span_refs == ("chunk-second",)
    assert candidate.metadata["context_chunk_ids"] == ["chunk-first", "chunk-second"]


@pytest.mark.parametrize("marker", ["[0]", "[5]"])
def test_paper_answer_worker_reports_unresolved_citation_markers_for_the_gate(marker: str) -> None:
    reply = f"The method retrieves evidence {marker}."
    worker = PaperAnswerWorker(AnswerGenerator(FakePaperAnswerModel(reply), max_context_chunks=1))
    pack = _pack(_evidence("ev-method", _chunk("chunk-method", "Verified method evidence.")))

    candidate = worker.generate_answer(question="What does the evidence say?", pack=pack)

    assert candidate.answer_text == reply
    assert candidate.unresolved_citations == (marker,)
    assert candidate.cited_evidence_ids == ()
    integrity = next(
        result
        for result in RAGAnswerGate().evaluate(candidate, pack)
        if result.gate_name == "rag_answer_citation_integrity"
    )
    assert integrity.passed is False


def test_paper_answer_worker_leaves_uncited_answers_to_claim_coverage_gate() -> None:
    worker = PaperAnswerWorker(
        AnswerGenerator(FakePaperAnswerModel("The method retrieves evidence."), max_context_chunks=1)
    )
    pack = _pack(_evidence("ev-method", _chunk("chunk-method", "Verified method evidence.")))

    candidate = worker.generate_answer(question="What does the evidence say?", pack=pack)

    assert candidate.answer_text == "The method retrieves evidence."
    assert candidate.cited_evidence_ids == ()
    assert candidate.claims[0].evidence_ids == ()
    coverage = next(
        result
        for result in RAGAnswerGate().evaluate(candidate, pack)
        if result.gate_name == "rag_answer_claim_coverage"
    )
    assert coverage.passed is False


def test_paper_answer_worker_maps_model_failure_to_worker_error() -> None:
    model = FakePaperAnswerModel(PaperAnswerModelError("timeout", "model timed out", retryable=True))
    worker = PaperAnswerWorker(AnswerGenerator(model, max_context_chunks=1))
    pack = _pack(_evidence("ev-method", _chunk("chunk-method", "Verified method evidence.")))

    with pytest.raises(AnswerWorkerError) as raised:
        worker.generate_answer(question="What does the evidence say?", pack=pack)

    assert raised.value.code == "model_timeout"
    assert raised.value.retryable is True


def _pack(*evidence: EvidenceCandidate) -> RAGContextPack:
    return RAGContextPack(pack_id="pack-1", query="question", accepted_evidence=tuple(evidence))


def _chunk(chunk_id: str, content: str, *, chunk_type: str = "paragraph") -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="pymupdf",
        chunk_type=chunk_type,
        section_title="Method",
        section_role=["method"],
        content=content,
        metadata={"source_locator": "paper://p1/pdf#page=1"},
    )


def _evidence(evidence_id: str, chunk: PaperChunk) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=evidence_id,
        title=chunk.section_title,
        summary=chunk.content,
        source_ref="paper://p1/pdf#page=1",
        span_refs=(chunk.chunk_id,),
        evidence_type="method",
        confidence=0.9,
        lineage=("retrieval.test",),
        metadata={"paper_chunk": chunk.model_dump(mode="json")},
    )
