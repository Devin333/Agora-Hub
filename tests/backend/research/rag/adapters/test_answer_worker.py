from __future__ import annotations

from backend.research.document.models import PaperChunk
from backend.research.rag.adapters.answer_worker import PaperAnswerWorker
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from framework.harness.rag.fake import fake_rag_session_spec
from framework.harness.rag.models import EvidenceCandidate, RAGContextPack


def test_paper_answer_worker_maps_generated_chunk_ids_to_evidence_ids() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Method evidence" in prompt
        return "The paper uses a retrieval method supported by context [1]."

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="What method does the paper use?",
        accepted_evidence=(
            _evidence(
                "ev-method",
                _chunk(
                    "chunk-method",
                    "Method evidence: the system retrieves and verifies paper evidence.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(question="What method does the paper use?", pack=pack)

    assert candidate.abstained is False
    assert candidate.answer_text == "The paper uses a retrieval method supported by context [1]."
    assert candidate.cited_evidence_ids == ("ev-method",)
    assert candidate.claims[0].evidence_ids == ("ev-method",)
    assert candidate.claims[0].span_refs == ("chunk-method",)
    assert candidate.metadata["context_chunk_ids"] == ["chunk-method"]
    assert candidate.metadata["chunk_to_evidence_id"] == {"chunk-method": "ev-method"}
    assert candidate.metadata["evidence_id_to_span_refs"] == {"ev-method": ["chunk-method"]}
    assert candidate.metadata["claims_degraded"] is True


def test_paper_answer_worker_forwards_physical_execution_identity_to_llm() -> None:
    identities = []

    async def fake_llm(prompt: str, *, execution_identity=None) -> str:
        identities.append(execution_identity)
        return "The paper uses a retrieval method supported by context [1]."

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="What method does the paper use?",
        accepted_evidence=(_evidence("ev-method", _chunk("chunk-method", "Method evidence.")),),
    )
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
        pack=pack,
        execution_identity=identity,
    )

    assert candidate.abstained is False
    assert identities == [identity]


def test_paper_answer_worker_can_rebuild_chunk_from_flat_evidence_metadata() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Table evidence" in prompt
        return "The table shows stronger results for the proposed model [1]."

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
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
    pack = RAGContextPack(pack_id="pack-1", query="What do results show?", accepted_evidence=(evidence,))

    candidate = worker.generate_answer(question="What do results show?", pack=pack)

    assert candidate.abstained is False
    assert candidate.cited_evidence_ids == ("ev-table",)
    assert candidate.claims[0].span_refs == ("chunk-table",)
    assert candidate.metadata["context_chunk_ids"] == ["chunk-table"]


def test_paper_answer_worker_abstains_when_context_lacks_paper_chunk_metadata() -> None:
    async def fake_llm(prompt: str) -> str:
        raise AssertionError("LLM should not be called without paper context")

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm))
    pack = RAGContextPack(
        pack_id="pack-missing",
        query="What method does the paper use?",
        accepted_evidence=(
            EvidenceCandidate(
                evidence_id="ev-1",
                title="Unstructured evidence",
                summary="A short summary without paper ids.",
                source_ref="source://x",
                span_refs=("span-1",),
                evidence_type="method",
                confidence=0.9,
                lineage=("retrieval.test",),
            ),
        ),
    )

    candidate = worker.generate_answer(question="What method does the paper use?", pack=pack)

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.cited_evidence_ids == ()
    assert candidate.metadata["abstention_reason"] == "context pack lacks paper_chunk metadata"


def test_paper_answer_worker_normalizes_context_absence_answer_to_abstention() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Method evidence" in prompt
        return (
            "No. The provided context describes retrieval and verification, "
            "but it does not provide operating instructions for a microwave oven [1]."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper provide operating instructions for a microwave oven?",
        accepted_evidence=(
            _evidence(
                "ev-method",
                _chunk(
                    "chunk-method",
                    "Method evidence: the system retrieves and verifies paper evidence.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper provide operating instructions for a microwave oven?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.cited_evidence_ids == ()
    assert candidate.claims == ()
    assert candidate.metadata["abstention_reason"] == "answer generator reported insufficient context"
    assert "microwave oven" in candidate.metadata["generated_answer"]["answer"]


def test_paper_answer_worker_normalizes_context_does_not_indicate_answer_to_abstention() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Prompt evidence" in prompt
        return (
            "The provided context does not indicate that the paper discloses a private API key. "
            "The passages only show example prompts and no API key disclosure is mentioned [1]."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper disclose a private API key used by the authors?",
        accepted_evidence=(
            _evidence(
                "ev-prompt",
                _chunk(
                    "chunk-prompt",
                    "Prompt evidence: the paper shows example prompts without secrets.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper disclose a private API key used by the authors?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.metadata["abstention_reason"] == "answer generator reported insufficient context"
    assert "private API key" in candidate.metadata["generated_answer"]["answer"]


def test_paper_answer_worker_normalizes_context_does_not_state_answer_to_abstention() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Grid evidence" in prompt
        return (
            "The provided context does not state that the paper reports carbon emissions "
            "from a national power grid, so I cannot conclude that it does from these passages alone."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper report carbon emissions from a national power grid?",
        accepted_evidence=(
            _evidence(
                "ev-grid",
                _chunk(
                    "chunk-grid",
                    "Grid evidence: the paper reports model evaluation details, not grid emissions.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper report carbon emissions from a national power grid?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.metadata["abstention_reason"] == "answer generator reported insufficient context"


def test_paper_answer_worker_normalizes_contains_no_mention_answer_to_abstention() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Weather evidence" in prompt
        return (
            "No. The provided context contains no mention of weather measurements, "
            "Shanghai, or July 6, 2026. It discusses retrieval results [1]."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper report weather measurements from Shanghai on July 6, 2026?",
        accepted_evidence=(
            _evidence(
                "ev-weather",
                _chunk(
                    "chunk-weather",
                    "Weather evidence: the paper discusses retrieval-augmented language models.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper report weather measurements from Shanghai on July 6, 2026?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.metadata["abstention_reason"] == "answer generator reported insufficient context"


def test_paper_answer_worker_normalizes_contains_nothing_about_answer_to_abstention() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Math evidence" in prompt
        return (
            "No. The provided passages discuss diffusion models and reverse-process objectives; "
            "they contain nothing about the Riemann hypothesis or a proof of it [1]."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper include a proof of the Riemann hypothesis?",
        accepted_evidence=(
            _evidence(
                "ev-math",
                _chunk(
                    "chunk-math",
                    "Math evidence: the paper discusses diffusion objectives and training.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper include a proof of the Riemann hypothesis?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.metadata["abstention_reason"] == "answer generator reported insufficient context"


def test_paper_answer_worker_abstains_when_negative_presence_answer_recites_unrelated_context() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Axial attention evidence" in prompt
        return (
            "The general idea of axial attention is to perform multiple attention operations, "
            "each along a single axis of the input tensor [1]."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper specify the launch date of a commercial smartphone product?",
        accepted_evidence=(
            _evidence(
                "ev-attention",
                _chunk(
                    "chunk-attention",
                    "Axial attention evidence: the paper studies efficient attention layers for images.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper specify the launch date of a commercial smartphone product?",
        pack=pack,
    )

    assert candidate.abstained is True
    assert candidate.answer_text == ""
    assert candidate.cited_evidence_ids == ()
    assert candidate.claims == ()
    assert candidate.metadata["abstention_reason"] == "negative presence answer lacked target relevance"
    relevance = candidate.metadata["negative_presence_relevance"]
    assert relevance["target_terms"] == ["commercial", "date", "launch", "product", "smartphone"]
    assert relevance["overlap_terms"] == []
    assert relevance["overlap_ratio"] == 0.0
    assert relevance["context_overlap_terms"] == []
    assert relevance["context_overlap_ratio"] == 0.0
    assert "axial attention" in candidate.metadata["generated_answer"]["answer"]


def test_paper_answer_worker_keeps_supported_negative_presence_answer_with_target_overlap() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "Launch evidence" in prompt
        return (
            "Yes. The paper specifies the commercial smartphone product launch date "
            "as July 16, 2024 [1]."
        )

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-1",
        query="Does this paper specify the launch date of a commercial smartphone product?",
        accepted_evidence=(
            _evidence(
                "ev-launch",
                _chunk(
                    "chunk-launch",
                    "Launch evidence: the commercial smartphone product launched on July 16, 2024.",
                    chunk_type="paragraph",
                ),
            ),
        ),
    )

    candidate = worker.generate_answer(
        question="Does this paper specify the launch date of a commercial smartphone product?",
        pack=pack,
    )

    assert candidate.abstained is False
    assert candidate.answer_text == (
        "Yes. The paper specifies the commercial smartphone product launch date as July 16, 2024 [1]."
    )
    assert candidate.cited_evidence_ids == ("ev-launch",)
    assert "negative_presence_relevance" not in candidate.metadata


def test_paper_answer_worker_discloses_only_explicitly_cited_evidence() -> None:
    async def fake_llm(prompt: str) -> str:
        assert "First evidence" in prompt and "Second evidence" in prompt
        return "The second context supports the answer [2]."

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=2))
    pack = RAGContextPack(
        pack_id="pack-selective",
        query="Which context supports the answer?",
        accepted_evidence=(
            _evidence("ev-first", _chunk("chunk-first", "First evidence.")),
            _evidence("ev-second", _chunk("chunk-second", "Second evidence.")),
        ),
    )

    candidate = worker.generate_answer(
        question="Which context supports the answer?",
        pack=pack,
    )

    assert candidate.abstained is False
    assert candidate.cited_evidence_ids == ("ev-second",)
    assert candidate.claims[0].evidence_ids == ("ev-second",)
    assert candidate.claims[0].span_refs == ("chunk-second",)
    assert candidate.metadata["context_chunk_ids"] == ["chunk-first", "chunk-second"]


def test_answer_generator_repairs_zero_index_citation_without_fabricating_a_locator() -> None:
    async def fake_llm(_prompt: str) -> str:
        return "An invalid generated citation points at context zero [0]."

    worker = PaperAnswerWorker(AnswerGenerator(fake_llm, max_context_chunks=1))
    pack = RAGContextPack(
        pack_id="pack-zero",
        query="What does the evidence say?",
        accepted_evidence=(
            _evidence("ev-method", _chunk("chunk-method", "Verified method evidence.")),
        ),
    )

    candidate = worker.generate_answer(question="What does the evidence say?", pack=pack)

    assert candidate.abstained is False
    assert "[0]" not in candidate.answer_text
    assert candidate.cited_evidence_ids == ("ev-method",)
    assert candidate.claims[0].span_refs == ("chunk-method",)


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
