from __future__ import annotations

from backend.research.document.models import PaperChunk
from backend.research.rag.adapters import (
    PAPER_CHUNK_METADATA_KEY,
    PaperChunkAdapter,
    paper_chunk_from_evidence_record,
    paper_chunk_to_evidence_metadata,
    paper_chunk_to_rag_chunk,
    paper_chunk_to_rag_evidence,
    source_locator_from_paper_chunk,
)
from framework.rag.core import RAGChunk, RAGEvidence


def _chunk(**metadata):
    return PaperChunk(
        chunk_id="chunk-1",
        paper_id="paper-1",
        parse_source="nougat",
        chunk_type="formula",
        parent_chunk_id="parent-1",
        section_title="3 Method",
        section_role=["method"],
        section_index=3,
        has_formula=True,
        formula_latex="y = Wx",
        formula_description="A linear projection.",
        has_figure=False,
        has_table=False,
        references=["ref-1"],
        content="Equation: y = Wx\nCaption: projection equation",
        metadata={
            "source_locator": "paper://paper-1/pdf#page=6&pdf_rect=1,2,3,4",
            "caption_text": "projection equation",
            "visual_description": "A diagram-like equation crop.",
            "child_semantic_score": "0.7",
            "parent_relevance_score": 0.6,
            "field_score": 0.5,
            "parent_section_heading_score": 0.4,
            "parent_position_score": 0.3,
            "field_rerank_score": 0.2,
            "fused_score": 0.9,
            **metadata,
        },
    )


def test_paper_chunk_projects_to_rag_chunk_with_fields_and_locator():
    chunk = _chunk()

    rag_chunk = paper_chunk_to_rag_chunk(chunk)

    assert isinstance(rag_chunk, RAGChunk)
    assert rag_chunk.chunk_id == "chunk-1"
    assert rag_chunk.document_id == "paper-1"
    assert rag_chunk.text == chunk.content
    assert rag_chunk.chunk_type == "formula"
    assert rag_chunk.fields["title"] == "3 Method"
    assert "y = Wx" in rag_chunk.fields["formula"]
    assert "linear projection" in rag_chunk.fields["equation"]
    assert "projection equation" in rag_chunk.fields["caption"]
    assert "diagram-like equation crop" in rag_chunk.fields["caption"]
    assert rag_chunk.fields["visual_description"] == "A diagram-like equation crop."
    assert rag_chunk.source_locator is not None
    assert rag_chunk.source_locator.source_id == "paper://paper-1/pdf#page=6&pdf_rect=1,2,3,4"
    assert rag_chunk.source_locator.page == 6
    assert rag_chunk.source_locator.bbox == (1.0, 2.0, 3.0, 4.0)
    assert rag_chunk.source_locator.raw_locator == "paper://paper-1/pdf#page=6&pdf_rect=1,2,3,4"
    assert rag_chunk.metadata["paper_id"] == "paper-1"
    assert rag_chunk.metadata["chunk_type"] == "formula"
    assert rag_chunk.metadata["parent_chunk_id"] == "parent-1"
    assert rag_chunk.metadata["section_role"] == ["method"]


def test_paper_chunk_projects_to_rag_evidence_with_score_breakdown():
    evidence = paper_chunk_to_rag_evidence(_chunk())

    assert isinstance(evidence, RAGEvidence)
    assert evidence.score == 0.9
    assert evidence.score_breakdown.to_dict() == {
        "child_similarity": 0.7,
        "parent_relevance": 0.6,
        "field_score": 0.5,
        "section_heading_score": 0.4,
        "position_bonus": 0.3,
        "rerank_score": 0.2,
        "final_score": 0.9,
    }


def test_paper_chunk_adapter_does_not_invent_missing_breakdown_values():
    evidence = PaperChunkAdapter().to_rag_evidence(_chunk(
        child_semantic_score=None,
        parent_relevance_score=None,
        field_score=None,
        parent_section_heading_score=None,
        parent_position_score=None,
        field_rerank_score=None,
        fused_score=None,
    ))

    assert evidence.score == 0.0
    assert evidence.score_breakdown.to_dict() == {}


def test_paper_chunk_projects_to_evidence_metadata_for_harness_pack():
    metadata = paper_chunk_to_evidence_metadata(_chunk())

    assert metadata["chunk_type"] == "formula"
    assert metadata["section_role"] == ["method"]
    assert metadata["parent_chunk_id"] == "parent-1"
    assert metadata["has_formula"] is True
    assert metadata["formula_latex"] == "y = Wx"
    assert metadata["source_locator"] == "paper://paper-1/pdf#page=6&pdf_rect=1,2,3,4"
    assert metadata["rag_document_id"] == "paper-1"
    assert metadata["rag_chunk_id"] == "chunk-1"
    assert metadata["rag_score"] == 0.9
    assert metadata["rag_score_breakdown"] == {
        "child_similarity": 0.7,
        "parent_relevance": 0.6,
        "field_score": 0.5,
        "section_heading_score": 0.4,
        "position_bonus": 0.3,
        "rerank_score": 0.2,
        "final_score": 0.9,
    }
    assert metadata["rag_source_locator"]["page"] == 6
    assert metadata["rag_source_locator"]["bbox"] == [1.0, 2.0, 3.0, 4.0]


def test_source_locator_uses_metadata_page_and_rect_when_raw_locator_is_less_structured():
    chunk = _chunk(source_locator="paper://paper-1/text", page=8, pdf_rect=[10, 20, 30, 40])

    locator = source_locator_from_paper_chunk(chunk)

    assert locator is not None
    assert locator.page == 8
    assert locator.bbox == (10.0, 20.0, 30.0, 40.0)


def test_rag_evidence_carries_a_lossless_chunk_record():
    chunk = _chunk()

    evidence = paper_chunk_to_rag_evidence(chunk)
    rebuilt = paper_chunk_from_evidence_record(evidence.metadata[PAPER_CHUNK_METADATA_KEY])

    assert rebuilt == chunk


def test_chunk_record_rejects_missing_or_malformed_payloads():
    assert paper_chunk_from_evidence_record(None) is None
    assert paper_chunk_from_evidence_record({"chunk_id": "only-an-id"}) is None
