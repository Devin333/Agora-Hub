from __future__ import annotations

from backend.research.document.models import PaperChunk
from infrastructure.storage.vector.fake_store import InMemoryVectorStore
from infrastructure.storage.vector.paper_field_chunk_store import PaperFieldChunkStore


def _chunk(chunk_id: str, *, caption: str = "Transformer architecture.") -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id="p1",
        parse_source="latex",
        chunk_type="figure",
        section_title="Model Architecture",
        section_role=["method"],
        section_index=2,
        has_figure=True,
        figure_id="fig1",
        content=f"[Figure fig1]\nCaption:\n{caption}",
        metadata={
            "caption_text": caption,
            "source_locator": "paper://p1/pdf#page=3&pdf_rect=1,2,3,4",
            "caption_source_locator": "paper://p1/pdf#page=3&pdf_rect=1,2,3,4",
        },
    )


def test_field_chunk_store_indexes_and_searches_selected_fields():
    store = PaperFieldChunkStore(InMemoryVectorStore())
    store.ensure_collection()
    store.index_chunks([
        _chunk("fig-arch", caption="Transformer architecture overview."),
        _chunk("fig-baseline", caption="Baseline chart."),
    ])

    hits = store.search_field_vectors(
        "p1",
        "architecture overview",
        field_names=("caption",),
        limit=5,
    )

    assert hits
    assert hits[0].chunk_id == "fig-arch"
    assert hits[0].field_name == "caption"
    assert hits[0].metadata["paper_id"] == "p1"
    assert hits[0].metadata["field_text_sources"]


def test_field_chunk_store_searches_multiple_fields_in_one_filter():
    store = PaperFieldChunkStore(InMemoryVectorStore())
    store.ensure_collection()
    store.index_chunks([
        _chunk("fig-arch", caption="Transformer architecture overview."),
        _chunk("fig-baseline", caption="Baseline chart."),
    ])

    hits = store.search_field_vectors(
        "p1",
        "architecture overview",
        field_names=("caption", "body"),
        limit=5,
    )

    assert hits
    assert hits[0].chunk_id == "fig-arch"


def test_field_chunk_store_deletes_paper_vectors():
    store = PaperFieldChunkStore(InMemoryVectorStore())
    store.ensure_collection()
    store.index_chunks([_chunk("fig-arch")])

    store.delete_paper_chunks("p1")

    assert store.search_field_vectors("p1", "architecture", field_names=("caption",), limit=5) == []
