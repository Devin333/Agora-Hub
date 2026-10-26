from __future__ import annotations

import pytest

from backend.research.document.models import PaperChunk
from backend.research.document.section_outline import PaperSectionOutline


def _chunk(
    chunk_id: str,
    section_index: int,
    title: str,
    content: str,
    *,
    parent: bool = False,
    paper_id: str = "p1",
    metadata: dict | None = None,
) -> PaperChunk:
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        parse_source="latex",
        chunk_type="paragraph",
        section_title=title,
        section_role=["method"],  # type: ignore[arg-type]
        section_index=section_index,
        content=content,
        metadata={**({"is_parent": True} if parent else {}), **(metadata or {})},
    )


def test_outline_groups_children_by_section_in_reading_order() -> None:
    chunks = [
        _chunk("s2-a", 2, "Experiments ::: Datasets", "We use SQuAD and NewsQA."),
        _chunk("s1-parent", 1, "Introduction", "Whole intro text.", parent=True),
        _chunk("s1-a", 1, "Introduction", "Reading comprehension matters."),
        _chunk("s1-b", 1, "Introduction", "We propose a model."),
        _chunk("other", 1, "Introduction", "Another paper.", paper_id="p2"),
    ]

    outline = PaperSectionOutline.from_chunks("p1", chunks)

    assert [entry.section_index for entry in outline.entries] == [1, 2]
    intro, datasets = outline.entries
    # Parent chunks name the section but are not passages of it.
    assert intro.chunk_ids == ("s1-a", "s1-b")
    assert intro.title == "Introduction"
    assert intro.level == 1
    assert datasets.level == 2
    assert intro.preview == "Reading comprehension matters. We propose a model."
    assert outline.section_of() == {"s1-a": 1, "s1-b": 1, "s2-a": 2}
    assert outline.entry(2) is datasets
    assert outline.entry(9) is None
    assert outline.navigable


def test_outline_preview_is_cut_at_a_word_boundary() -> None:
    outline = PaperSectionOutline.from_chunks(
        "p1", [_chunk("a", 1, "Method", "alpha beta gamma delta")], preview_chars=12,
    )

    assert outline.entries[0].preview == "alpha beta ..."


def test_outline_keeps_the_larger_of_reported_and_heading_path_level() -> None:
    outline = PaperSectionOutline.from_chunks("p1", [
        _chunk("a", 1, "Method", "x", metadata={"section_level": 3}),
        _chunk("b", 2, "A ::: B ::: C", "y", metadata={"section_level": 1}),
    ])

    assert [entry.level for entry in outline.entries] == [3, 3]


def test_paper_without_titled_sections_is_not_navigable() -> None:
    outline = PaperSectionOutline.from_chunks("p1", [
        _chunk("a", 0, "", "fixed window one"),
        _chunk("b", 1, "", "fixed window two"),
    ])

    assert not outline.navigable


def test_outline_rejects_negative_preview_size() -> None:
    with pytest.raises(ValueError):
        PaperSectionOutline.from_chunks("p1", [], preview_chars=-1)
