from __future__ import annotations

import inspect
from hashlib import sha256
import json
import pytest

from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchSection
from backend.research.domain.document_fidelity import (
    DocumentBlock, DocumentFidelity, DocumentPage, InlineMathSpan, TableCell, TableGrid, TableHeaderPath,
)
from backend.research.rag.evaluation.document_parsing_export import (
    ExportStatus, PageConvention, export_research_document,
)
from backend.research.rag.evaluation.document_parsing_render import (
    render_production_pages, write_production_predictions,
)

UNKNOWN = {"bbox_unknown_reason": "backend_has_no_cell_coordinates"}


def _block(identity: str, text: str, order: int, **options) -> DocumentBlock:
    return DocumentBlock(
        block_id=identity, page_number=1, kind="text", text=text,
        source_order=order, reading_order=order, order_status="preserved",
        source_locator=f"pdf://paper/page/1/{identity}",
        content_owner_id=options.pop("content_owner_id", identity), **UNKNOWN, **options,
    )


def _document(*, blocks=(), tables=(), pages=2, **options) -> ResearchDocument:
    return ResearchDocument(
        paper_id="paper", source_hash="sha256:source",
        lineage=SourceLineage(source_refs=["pdf://paper"], source_hash="sha256:source"),
        fidelity=DocumentFidelity(
            schema_version="research-document-fidelity.v1", normalization_version="normalizer-v1",
            source_hash="sha256:source", parser_fingerprint="parser-v1", profile_fingerprint="profile-v1",
            content_checksum="sha256:content",
            pages=tuple(DocumentPage(pdf_page_number=n, width=600., height=800., dimension_unit="pt")
                        for n in range(1, pages + 1)), blocks=blocks, tables=tables,
        ), **options,
    )


def _render(document):
    return render_production_pages(export_research_document(
        document, page_count=len(document.fidelity.pages),
        page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED,
    ))


def test_renderer_accepts_only_production_export_not_raw_or_gold():
    assert tuple(inspect.signature(render_production_pages).parameters) == ("report",)
    with pytest.raises(TypeError):
        render_production_pages({"raw": "must not be used"})


def test_adopted_order_owner_uniqueness_and_legacy_data_are_preserved():
    first = _block("first", "later paragraph", 0).model_copy(update={
        "reading_order": 1, "order_status": "reordered", "order_reason": "validated_band_candidate",
    })
    second = _block("second", "earlier paragraph", 1).model_copy(update={
        "reading_order": 0, "order_status": "reordered", "order_reason": "validated_band_candidate",
    })
    child = _block("child", "must not repeat owned text", 2, content_owner_id="first")
    doc = _document(blocks=(first, second, child), sections=[ResearchSection(
        section_id="legacy", title="ignored", text="not adopted", source_ref="pdf://paper#page=1",
    )])
    before = doc.model_dump_json()
    pages = _render(doc)
    assert pages[0].markdown == "earlier paragraph\n\nlater paragraph"
    assert pages[0].projected_block_ids == ("second", "first")
    assert pages[0].status is ExportStatus.SUPPORTED
    assert pages[1].status is ExportStatus.MISSING_OUTPUT
    assert doc.model_dump_json() == before


@pytest.mark.parametrize("text,expected,start,end", [
    ("中文 x<y then", "中文 $x<y$ then", 3, 6),
    (r"a \(x<y\) b", r"a \(x<y\) b", 4, 7),
    ("a $x<y$ b", "a $x<y$ b", 3, 6),
    (r"cost \$5, x_y", r"cost \$5, $x_y$", 10, 13),
])
def test_inline_math_offsets_unicode_existing_and_escaped_delimiters(text, expected, start, end):
    span = InlineMathSpan(start_offset=start, end_offset=end, offset_text_version="normalized",
        raw_latex="DO NOT RESTORE RAW", normalized_latex=text[start:end], source_locator="pdf://math")
    doc = _document(blocks=(_block("math", text, 0, inline_math=(span,)),))
    assert _render(doc)[0].markdown == expected


def test_display_formula_is_not_reinterpreted_as_html():
    formula = r"\begin{aligned}a_1&<b^2\\c&=\frac{1}{2}\end{aligned}"
    block = _block("equation", formula, 0).model_copy(update={"kind": "equation"})
    assert _render(_document(blocks=(block,)))[0].markdown == "$$\n" + formula + "\n$$"


def test_table_spans_empty_missing_math_and_script_are_not_truth_repairs():
    table_block = _block("table", "DO NOT SCORE BLOCK SUMMARY", 0).model_copy(update={
        "kind": "table", "element_ref": "table-1",
    })
    def cell(identity, row, column, status="content", text="", **values):
        return TableCell(cell_id=identity, row_index=row, column_index=column,
            status=status, text=text, source_locator=f"pdf://cell/{identity}", **UNKNOWN, **values)
    cells = (
        cell("c00", 0, 0, text="Header", column_span=2, header_role="column"),
        cell("c01", 0, 1, "covered", covered_by_cell_id="c00"),
        cell("c02", 0, 2, "blank", header_role="column"),
        cell("c10", 1, 0, text="-1.20 µg <script>alert(1)</script>"),
        cell("c11", 1, 1, text="x<y", raw_text="99", inline_math=(InlineMathSpan(
            start_offset=0, end_offset=3, offset_text_version="normalized",
            raw_latex="99", source_locator="pdf://cellmath"),)),
        cell("c12", 1, 2, "missing"),
    )
    grid = TableGrid(table_id="table-1", block_id="table", row_count=2, column_count=3,
        has_header=True, source_locator="pdf://table", cells=cells, header_paths=(
            TableHeaderPath(column_index=0, cell_ids=("c00",)),
            TableHeaderPath(column_index=1, cell_ids=("c00",)),
            TableHeaderPath(column_index=2, cell_ids=("c02",)),
        ))
    doc = _document(
        blocks=(table_block,), tables=(grid,), metadata={"visual_description": "value is 100"},
    )
    page = _render(doc)[0]
    assert '<th colspan="2">Header</th><th></th>' in page.markdown
    assert "-1.20 µg &lt;script&gt;alert(1)&lt;/script&gt;" in page.markdown
    assert "$x&lt;y$" in page.markdown
    assert "99" not in page.markdown and "100" not in page.markdown
    assert "BLOCK SUMMARY" not in page.markdown
    assert page.status is ExportStatus.PARTIAL
    assert [(issue.code, issue.content_id) for issue in page.issues] == [("table_cell_missing", "c12")]


def test_legacy_content_is_not_mixed_or_given_invented_mixed_order():
    doc = ResearchDocument(paper_id="old", source_hash="sha256:old",
        lineage=SourceLineage(source_refs=["pdf://old"], source_hash="sha256:old"),
        sections=[ResearchSection(section_id="s", title="t", text="available separately", page_start=1,
                                  page_end=1, source_ref="pdf://old#page=1")])
    report = export_research_document(doc, page_count=2, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)
    assert all(not page.markdown for page in render_production_pages(report))
    assert render_production_pages(report)[0].status is ExportStatus.UNAVAILABLE


def test_forged_page_mapping_and_incomplete_page_ledger_are_rejected():
    doc = _document(blocks=(_block("one", "text", 0),))
    report = export_research_document(doc, page_count=2, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)
    bad = report.model_copy(update={"pages": (report.pages[0].model_copy(update={"content_ids": ("wrong",)}), report.pages[1])})
    with pytest.raises(ValueError, match="content IDs"):
        render_production_pages(bad)
    with pytest.raises(ValueError, match="every physical"):
        render_production_pages(report.model_copy(update={"pages": report.pages[:1]}))


def test_uncertain_order_remains_partial_not_silent_success():
    block = _block("uncertain", "source order", 0).model_copy(update={
        "order_status": "uncertain", "order_reason": "rotation_not_supported",
    })
    page = _render(_document(blocks=(block,)))[0]
    assert page.status is ExportStatus.PARTIAL
    assert page.markdown == "source order"
    assert page.issues[0].code == "reading_order_uncertain"


def test_exclusive_prediction_publish_preserves_missing_pages_and_complete_ledger(tmp_path):
    pages = _render(_document(blocks=(_block("one", "text", 0),)))
    target = tmp_path / "new"
    write_production_predictions(pages, page_filenames={1: "first.md", 2: "empty.md"}, output_path=target)
    assert (target / "empty.md").read_bytes() == b""
    ledger = json.loads((target / "rendering-ledger.json").read_text(encoding="utf-8"))
    assert ledger["requested_pages"] == ledger["terminal_pages"] == 2
    assert ledger["pages"][1]["status"] == "missing_output"
    assert "markdown" not in ledger["pages"][0]
    for item in ledger["pages"]:
        payload = (target / item["filename"]).read_bytes()
        assert item["markdown_sha256"] == sha256(payload).hexdigest()
        assert item["markdown_bytes"] == len(payload)
    with pytest.raises(FileExistsError):
        write_production_predictions(pages, page_filenames={1: "first.md", 2: "empty.md"}, output_path=target)
    assert (target / "first.md").read_text(encoding="utf-8") == "text\n"


@pytest.mark.parametrize("mapping", [
    {1: "../escape.md", 2: "ok.md"}, {1: r"C:\escape.md", 2: "ok.md"},
    {1: "same.md", 2: "SAME.md"}, {1: "CON.md", 2: "ok.md"},
    {True: "first.md", 2: "ok.md"}, {1: "only-one.md"},
])
def test_invalid_filename_mapping_is_rejected_before_creating_output(tmp_path, mapping):
    pages = _render(_document(blocks=(_block("one", "text", 0),)))
    target = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        write_production_predictions(pages, page_filenames=mapping, output_path=target)
    assert not target.exists()


@pytest.mark.parametrize("formula", [r"$$x<y$$", r"\[x<y\]"])
def test_already_delimited_entire_display_span_is_not_wrapped_twice(formula):
    span = InlineMathSpan(start_offset=0, end_offset=len(formula), offset_text_version="raw",
        raw_latex=formula, display=True, source_locator="pdf://equation")
    block = _block("equation", formula, 0, inline_math=(span,)).model_copy(update={"kind": "equation"})
    assert _render(_document(blocks=(block,)))[0].markdown == formula
