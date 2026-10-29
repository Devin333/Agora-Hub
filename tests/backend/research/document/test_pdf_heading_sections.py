from __future__ import annotations

import pymupdf

from backend.research.document.cascade_parser import PyMuPDFTextDocumentParser
from backend.research.document.chunker import PaperDocumentChunker

# Plain prose that names no section role, so roles come from the headings.
BODY = (
    "The text describes measured quantities with careful wording across several "
    "plain sentences written for this check. "
)


def _styled_paper(blocks: list[tuple[str, str]]) -> bytes:
    """Write ("heading" | "body" | "notice", text) blocks the way papers set them."""

    document = pymupdf.open()
    page = document.new_page()
    y = 60.0
    for kind, text in blocks:
        if y > 760:
            page = document.new_page()
            y = 60.0
        if kind == "heading":
            page.insert_text((50, y), text, fontname="hebo", fontsize=12)
            y += 22
        elif kind == "notice":
            # Large but regular: an arXiv stamp or license line, not a heading.
            page.insert_text((50, y), text, fontname="helv", fontsize=16)
            y += 26
        else:
            rect = pymupdf.Rect(50, y, 545, y + 120)
            page.insert_textbox(rect, text, fontname="helv", fontsize=10)
            y += 126
    payload = document.tobytes()
    document.close()
    return payload


def _paper() -> bytes:
    return _styled_paper([
        ("notice", "arXiv:2601.00001v1 [cs.CL] 1 Jan 2026"),
        ("heading", "A Study of Grounded Retrieval"),
        ("body", "Ada Lovelace and Grace Hopper. " * 3),
        ("heading", "1 Introduction"),
        ("body", BODY * 4),
        ("heading", "2 Method"),
        ("body", BODY * 4),
        ("heading", "3 Results"),
        ("heading", "3.1 Machine Translation"),
        ("body", BODY * 4),
        ("heading", "4 Conclusion"),
        ("body", BODY * 3),
        ("heading", "References"),
        ("body", "[1] A. Author. A cited work. 2025. " * 4),
    ])


def test_native_pdf_text_is_split_at_styled_headings() -> None:
    document = PyMuPDFTextDocumentParser().parse("paper-headings", _paper())

    titles = [section.title for section in document.sections]
    assert titles == [
        "Front matter",
        "1 Introduction",
        "2 Method",
        "3 Results",
        "3.1 Machine Translation",
        "4 Conclusion",
    ]
    # References are kept out of retrieval content, as for LaTeX sources.
    assert document.metadata["section_structure"] == "headings"
    levels = {section.title: section.level for section in document.sections}
    assert levels["3 Results"] == 1
    assert levels["3.1 Machine Translation"] == 2
    # The large regular stamp and the title block stay in the front matter.
    front = document.sections[0].text
    assert "arXiv:2601.00001v1" in front and "A Study of Grounded Retrieval" in front
    assert all(section.source_ref.startswith("paper://paper-headings/pdf#page=") for section in document.sections)
    # Sections sharing a page still have distinct locators.
    assert len({section.source_ref for section in document.sections}) == len(document.sections)


def test_heading_sections_pass_the_reading_quality_report() -> None:
    from backend.research.application.parse_paper import _quality_report

    document = PyMuPDFTextDocumentParser().parse("paper-quality", _paper())
    document = document.model_copy(update={"title": "A Study of Grounded Retrieval"})

    report = _quality_report(document, profile="reading")

    codes = {failure["code"] for failure in report["hardFailures"]}
    assert "locator_coverage_insufficient" not in codes


def test_pdf_without_styled_headings_keeps_one_section_per_page() -> None:
    document = pymupdf.open()
    for _ in range(2):
        document.new_page().insert_textbox(pymupdf.Rect(50, 60, 545, 790), BODY * 6, fontsize=10)
    payload = document.tobytes()
    document.close()

    parsed = PyMuPDFTextDocumentParser().parse("paper-plain", payload)

    assert [section.title for section in parsed.sections] == ["PDF Text Page 1", "PDF Text Page 2"]
    assert parsed.metadata["section_structure"] == "pages"


def test_subsection_takes_the_role_of_its_parent_heading() -> None:
    document = PyMuPDFTextDocumentParser().parse("paper-roles", _paper())

    chunks = PaperDocumentChunker().chunk(document, "pymupdf")

    roles = {
        chunk.section_title: tuple(chunk.section_role)
        for chunk in chunks
        if chunk.chunk_type == "paragraph" and chunk.parent_chunk_id is None
    }
    # "3 Results" has no body of its own; its subsection carries the role.
    assert roles["3.1 Machine Translation"][0] == "experiment"
    assert "3 Results" not in roles
    assert "conclusion" in roles["4 Conclusion"]
