from __future__ import annotations

from typing import Any

from backend.research.document.models import PaperChunk
from backend.research.ports.visual_chunk_index import VisualChunkHit
from backend.research.rag.retrieval.contracts import RetrievalRequest
from backend.research.rag.retrieval.factory import build_retrieval_pipeline
from backend.research.rag.retrieval.policies import RetrievalPolicy


def _chunk(chunk_id: str, *, chunk_type: str = "figure") -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="latex",
        chunk_type=chunk_type,  # type: ignore[arg-type]
        section_title="Figures",
        section_role=["experiment"],  # type: ignore[arg-type]
        section_index=2,
        content="Architecture figure caption.",
        metadata={},
    )


class _ChunkStore:
    def __init__(self, chunks: list[PaperChunk]) -> None:
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}

    def ensure_collection(self) -> None:
        return None

    def search_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        score_threshold: float | None = None,
    ) -> list[PaperChunk]:
        return [chunk for chunk, _score in self.search_with_scores(
            paper_id,
            query_text,
            filters=filters,
            limit=limit,
        )]

    def search_with_scores(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 30,
    ) -> list[tuple[PaperChunk, float]]:
        out: list[tuple[PaperChunk, float]] = []
        for chunk in self._chunks.values():
            if chunk.paper_id != paper_id:
                continue
            if filters and any(getattr(chunk, key, chunk.metadata.get(key)) != value for key, value in filters.items()):
                continue
            out.append((chunk, 0.5))
            if len(out) >= limit:
                break
        return out

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        return self._chunks.get(chunk_id)

    def get_parent_chunk(self, chunk: PaperChunk) -> PaperChunk | None:
        return None

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        return [chunk for chunk in self._chunks.values() if chunk.paper_id == paper_id]


class _VisualStore:
    def search_visual_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[VisualChunkHit]:
        return [VisualChunkHit("fig-1", 0.9)]


def test_factory_builds_pipeline_and_preserves_optional_adapter_metadata() -> None:
    pipeline = build_retrieval_pipeline(
        _ChunkStore([_chunk("fig-1")]),
        policy=RetrievalPolicy(overfetch_multiplier=1),
        visual_store=_VisualStore(),
    )

    result = pipeline.retrieve(
        RetrievalRequest(paper_id="p1", question="What does Figure 1 architecture show?", limit=1)
    )

    assert result.child_chunks[0].chunk_id == "fig-1"
    assert result.metadata["visual_fusion_enabled"] is True
    assert result.metadata["field_embedding_enabled"] is False
    assert result.metadata["claim_index_enabled"] is False
    assert result.child_chunks[0].metadata["visual_hit"] is True


class _SectionNavigator:
    def __init__(self, sections: tuple[int, ...]) -> None:
        self._sections = sections
        self.calls = 0

    @property
    def measurement_identity(self) -> dict[str, Any]:
        return {"component": "fake-navigator"}

    async def choose(self, request):
        from backend.research.ports.section_navigator import SectionNavigationDecision

        self.calls += 1
        return SectionNavigationDecision(self._sections, diagnostics={"model_calls": 1})


def _section_chunk(chunk_id: str, section_index: int, title: str, content: str) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="latex",
        chunk_type="paragraph",
        section_title=title,
        section_role=["method"],  # type: ignore[arg-type]
        section_index=section_index,
        content=content,
        metadata={},
    )


def test_factory_wires_optional_section_navigation_after_ranking() -> None:
    from backend.research.rag.retrieval.expanders.section_navigation import SectionNavigationConfig

    store = _ChunkStore([
        _section_chunk("intro", 1, "Introduction", "We study question answering."),
        _section_chunk("data", 2, "Datasets", "We evaluate on SQuAD."),
    ])
    navigator = _SectionNavigator((2,))
    pipeline = build_retrieval_pipeline(
        store,
        policy=RetrievalPolicy(overfetch_multiplier=1),
        section_navigator=navigator,
        section_navigation=SectionNavigationConfig(max_sections=1, max_passages_per_section=1),
    )

    result = pipeline.retrieve(RetrievalRequest(paper_id="p1", question="What do they study?", limit=1))

    assert navigator.calls == 1
    assert [chunk.chunk_id for chunk in result.child_chunks] == ["intro"]
    report = result.metadata["section_navigation"]
    assert report["read_sections"] == [1]
    assert report["chosen_sections"] == [2]


def test_factory_without_section_navigation_reports_nothing() -> None:
    pipeline = build_retrieval_pipeline(
        _ChunkStore([_section_chunk("intro", 1, "Introduction", "Text.")]),
        policy=RetrievalPolicy(overfetch_multiplier=1),
    )

    result = pipeline.retrieve(RetrievalRequest(paper_id="p1", question="What?", limit=1))

    assert "section_navigation" not in result.metadata


def test_factory_rejects_navigation_config_without_a_navigator() -> None:
    import pytest

    from backend.research.rag.retrieval.expanders.section_navigation import SectionNavigationConfig

    with pytest.raises(ValueError):
        build_retrieval_pipeline(
            _ChunkStore([]), policy=RetrievalPolicy(), section_navigation=SectionNavigationConfig(),
        )
