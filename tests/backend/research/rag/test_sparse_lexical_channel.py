from __future__ import annotations

from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.rag.retrieval.bm25_index import write_bm25_index
from backend.research.rag.retrieval.channels.sparse_lexical import SparseLexicalChannel
from backend.research.rag.retrieval.trace import RetrievalTrace


def _chunk(
    chunk_id: str,
    content: str,
    *,
    paper_id: str = "p1",
    chunk_type: str = "paragraph",
    metadata: dict[str, Any] | None = None,
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        parse_source="latex",
        chunk_type=chunk_type,  # type: ignore[arg-type]
        section_title="Method",
        section_role=["method"],
        section_index=1,
        content=content,
        has_formula=chunk_type == "formula",
        formula_latex=content if chunk_type == "formula" else "",
        metadata=metadata or {},
    )


class _ChunkStore:
    def __init__(self, chunks: list[PaperChunk]) -> None:
        self._chunks = chunks

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        return [chunk for chunk in self._chunks if chunk.paper_id == paper_id]


def _trace() -> RetrievalTrace:
    return RetrievalTrace(policy_name="test", policy_hash="hash")


def test_sparse_channel_uses_persisted_bm25_index(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(tmp_path / "runs"))
    generic = _chunk("generic", "Generic benchmark results.")
    rare = _chunk("rare", "Exact raremetric42 ablation results.")
    write_bm25_index("p1", [generic, rare])
    trace = _trace()

    hits = SparseLexicalChannel(_ChunkStore([generic])).recall_chunks(
        paper_id="p1",
        query_text="Which result reports raremetric42?",
        filters={},
        limit=2,
        trace=trace,
    )

    assert [chunk.chunk_id for chunk, _score in hits] == ["rare"]
    assert hits[0][0].metadata["sparse_candidate_source"] == "bm25_index"
    assert trace.degradations == []


def test_sparse_channel_reuses_unchanged_persisted_bm25_index(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(tmp_path / "runs"))
    chunk = _chunk("rare", "Exact raremetric42 ablation results.")
    write_bm25_index("p1", [chunk])
    channel = SparseLexicalChannel(_ChunkStore([chunk]))

    first = channel.recall_chunks(
        paper_id="p1",
        query_text="raremetric42",
        filters={},
        limit=1,
        trace=_trace(),
    )
    second = channel.recall_chunks(
        paper_id="p1",
        query_text="raremetric42",
        filters={},
        limit=1,
        trace=_trace(),
    )

    assert first[0][0].metadata["sparse_candidate_source"] == "bm25_index"
    assert second[0][0].metadata["sparse_candidate_source"] == "bm25_index"


def test_sparse_channel_missing_index_falls_back_observably(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(tmp_path / "runs"))
    generic = _chunk("generic", "Generic benchmark results.")
    rare = _chunk("rare", "Exact raremetric42 ablation results.")
    trace = _trace()

    hits = SparseLexicalChannel(_ChunkStore([generic, rare])).recall_chunks(
        paper_id="p1",
        query_text="Which result reports raremetric42?",
        filters={},
        limit=2,
        trace=trace,
    )

    assert [chunk.chunk_id for chunk, _score in hits] == ["rare"]
    assert hits[0][0].metadata["sparse_candidate_source"] == "bm25_list_chunks_fallback"
    assert [item.code for item in trace.degradations] == ["sparse_bm25_index_missing"]


def test_sparse_channel_preserves_formula_sparse_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(tmp_path / "runs"))
    formula = _chunk(
        "eq-attn",
        r"\operatorname{Attention}(Q,K,V)",
        chunk_type="formula",
        metadata={"reference_labels": ["2"]},
    )
    trace = _trace()

    hits = SparseLexicalChannel(_ChunkStore([formula])).recall_chunks(
        paper_id="p1",
        query_text="Attention Q K V equation",
        filters={"chunk_type": "formula"},
        limit=2,
        formula_sparse_enabled=True,
        trace=trace,
    )

    assert [chunk.chunk_id for chunk, _score in hits] == ["eq-attn"]
    assert hits[0][0].metadata["formula_sparse_hit"] is True
    assert hits[0][0].metadata["formula_operator_score"] > 0.0
