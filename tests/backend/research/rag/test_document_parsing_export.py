from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchEquation, ResearchSection, ResearchTable
from backend.research.domain.document_fidelity import (
    DOCUMENT_FIDELITY_SCHEMA,
    DocumentBlock,
    DocumentFidelity,
    DocumentPage,
    InlineMathSpan,
    TableCell,
    TableGrid,
)
from backend.research.rag.evaluation.document_parsing_export import (
    ContentKind,
    ExportStatus,
    MAX_EXPORTED_PAGES,
    PageConvention,
    export_research_document,
)


def _document(**values: object) -> ResearchDocument:
    return ResearchDocument(
        paper_id="paper-1",
        source_hash="sha256:source",
        lineage=SourceLineage(source_refs=["paper://paper-1"], source_hash="sha256:source"),
        **values,
    )


def test_missing_document_is_explicit_and_export_api_accepts_no_raw_or_gold() -> None:
    raw_backend_with_formula = {"equations": [{"latex": "x=1", "page": 1}]}
    assert raw_backend_with_formula["equations"]
    parameters = inspect.signature(export_research_document).parameters
    assert set(parameters) == {"document", "page_count", "page_convention"}

    report = export_research_document(None, page_count=2)

    assert report.track == "production_document"
    assert report.status is ExportStatus.MISSING_OUTPUT
    assert [page.status for page in report.pages] == [ExportStatus.MISSING_OUTPUT] * 2
    assert report.missing_output_reason
    assert report.content == ()


def test_empty_research_document_preserves_identity_and_missing_output() -> None:
    document = _document()

    report = export_research_document(document, page_count=2, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)

    assert report.document_id == document.document_id
    assert report.paper_id == "paper-1"
    assert report.source_hash == "sha256:source"
    assert report.status is ExportStatus.MISSING_OUTPUT
    assert len(report.pages) == 2
    assert all(page.status is ExportStatus.MISSING_OUTPUT for page in report.pages)
    assert report.content == ()


def test_legacy_sections_are_exported_per_page_but_mixed_reading_order_is_unavailable() -> None:
    document = _document(
        sections=[
            ResearchSection(
                section_id="section-1",
                title="Methods",
                text="First paragraph.",
                page_start=1,
                page_end=1,
                source_ref="paper://paper-1#page=1",
            )
        ],
        equations=[
            ResearchEquation(
                equation_id="equation-1",
                latex=r"\frac{a}{b} < c",
                page=1,
                source_ref="paper://paper-1#page=1",
            )
        ],
    )

    report = export_research_document(document, page_count=1, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)

    assert report.status is ExportStatus.SUPPORTED
    assert [item.identity for item in report.content] == ["section-1", "equation-1"]
    assert report.content[0].text == "First paragraph."
    assert report.content[1].latex == r"\frac{a}{b} < c"
    assert report.pages[0].content_ids == ("section:section-1", "equation:equation-1")
    assert report.status is ExportStatus.SUPPORTED
    assert report.supported_content_count == 2
    reading_order = next(item for item in report.unsupported_dimensions if item.dimension == "reading_order")
    assert "separate arrays" in reading_order.reason
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        report.status = ExportStatus.UNAVAILABLE  # type: ignore[misc]


def test_cross_page_section_and_invalid_physical_page_are_not_clamped_or_split() -> None:
    document = _document(
        sections=[
            ResearchSection(
                section_id="cross-page",
                title="Continued",
                text="This text spans pages.",
                page_start=1,
                page_end=2,
                source_ref="paper://paper-1#page=1",
            )
        ],
        equations=[
            ResearchEquation(
                equation_id="bad-page-equation",
                latex="x = 1",
                page=4,
                source_ref="paper://paper-1#page=4",
            )
        ],
    )

    report = export_research_document(document, page_count=2, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)

    cross_page, invalid_page = report.content
    assert cross_page.status is ExportStatus.UNAVAILABLE
    assert cross_page.reason == "cross_page_section_not_splittable"
    assert cross_page.text is None
    assert invalid_page.status is ExportStatus.UNAVAILABLE
    assert invalid_page.reason == "page_out_of_range"
    assert invalid_page.page == 4
    assert [page.page for page in report.pages] == [1, 2]
    assert [page.status for page in report.pages] == [ExportStatus.UNAVAILABLE, ExportStatus.UNAVAILABLE]
    assert report.pages[0].content_ids == ("section:cross-page",)
    assert report.pages[1].content_ids == ("section:cross-page",)
    assert report.status is ExportStatus.UNAVAILABLE


def test_zero_based_or_missing_page_is_not_guessed_and_dict_rows_are_not_a_grid() -> None:
    document = _document(
        equations=[
            ResearchEquation(
                equation_id="zero-page",
                latex="x=2",
                page=0,
                source_ref="paper://paper-1#page=0",
            ),
            ResearchEquation(
                equation_id="unknown-page",
                latex="y=3",
                page=None,
                source_ref="paper://paper-1/equation/unknown-page",
            ),
        ],
        tables=[
            ResearchTable(
                table_id="legacy-table",
                caption="Results",
                columns=["Method", "Score"],
                rows=[{"Method": "A", "Score": 1}],
                page=1,
                source_ref="paper://paper-1#page=1",
            )
        ],
    )

    report = export_research_document(document, page_count=1, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)

    zero_page, unknown_page, table = report.content
    assert zero_page.page == 0
    assert zero_page.status is ExportStatus.UNAVAILABLE
    assert zero_page.reason == "page_out_of_range"
    assert unknown_page.status is ExportStatus.UNAVAILABLE
    assert unknown_page.reason == "page_missing"
    assert table.kind is ContentKind.TABLE
    assert table.status is ExportStatus.UNAVAILABLE
    assert table.reason == "table_grid_unavailable_dict_rows_are_lossy"
    assert table.text is None
    assert any(item.dimension == "table_grid" for item in report.unsupported_dimensions)


def test_page_count_unknown_prevents_claiming_page_local_scoreability() -> None:
    document = _document(
        equations=[
            ResearchEquation(
                equation_id="equation-1",
                latex="x=1",
                page=1,
                source_ref="paper://paper-1#page=1",
            )
        ]
    )

    report = export_research_document(document, page_count=None, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)

    assert report.content[0].status is ExportStatus.UNAVAILABLE
    assert report.content[0].reason == "page_count_unknown"
    assert report.pages == ()


def test_page_count_must_be_positive() -> None:
    with pytest.raises(ValueError, match="between 1 and"):
        export_research_document(_document(), page_count=0, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED)


def test_unknown_page_convention_is_explicitly_unavailable_without_guessing() -> None:
    document = _document(
        equations=[
            ResearchEquation(
                equation_id="equation-1",
                latex="x=1",
                page=1,
                source_ref="paper://paper-1#page=1",
            )
        ]
    )

    report = export_research_document(document, page_count=1)

    assert report.status is ExportStatus.UNAVAILABLE
    assert report.content[0].reason == "page_convention_unknown"
    assert report.content[0].page == 1


def test_same_kind_duplicate_identity_is_rejected_and_cross_kind_ids_are_qualified() -> None:
    repeated = ResearchEquation(
        equation_id="shared-id",
        latex="x=1",
        page=1,
        source_ref="paper://paper-1#page=1",
    )
    with pytest.raises(ValueError, match="duplicate same-kind"):
        export_research_document(
            _document(equations=[repeated, repeated]),
            page_count=1,
            page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED,
        )

    report = export_research_document(
        _document(
            equations=[repeated],
            tables=[
                ResearchTable(
                    table_id="shared-id",
                    caption="Table",
                    page=1,
                    source_ref="paper://paper-1#page=1",
                )
            ],
        ),
        page_count=1,
        page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED,
    )
    assert [item.content_id for item in report.content] == ["equation:shared-id", "table:shared-id"]
    assert report.pages[0].content_ids == ("equation:shared-id", "table:shared-id")
    assert report.status is ExportStatus.PARTIAL
    assert report.supported_content_count == 1
    assert report.unavailable_content_count == 1


@pytest.mark.parametrize("page_count", [True, 1.5, "2"])
def test_page_count_requires_exact_integer_type(page_count: object) -> None:
    with pytest.raises(TypeError, match="exact int"):
        export_research_document(_document(), page_count=page_count)  # type: ignore[arg-type]


def test_page_count_resource_limit_rejects_without_truncation() -> None:
    with pytest.raises(ValueError, match=f"1 and {MAX_EXPORTED_PAGES}"):
        export_research_document(_document(), page_count=MAX_EXPORTED_PAGES + 1)


def test_page_convention_requires_enum_value() -> None:
    with pytest.raises(TypeError, match="PageConvention enum"):
        export_research_document(  # type: ignore[arg-type]
            _document(),
            page_count=1,
            page_convention="physical_pdf_one_based",
        )


def test_report_model_uses_strict_validated_fields() -> None:
    from backend.research.rag.evaluation.document_parsing_export import ResearchDocumentParsingExport

    with pytest.raises(ValidationError):
        ResearchDocumentParsingExport(page_count=True, status=ExportStatus.UNAVAILABLE)
    with pytest.raises(ValidationError):
        ResearchDocumentParsingExport(
            page_count=MAX_EXPORTED_PAGES + 1,
            status=ExportStatus.UNAVAILABLE,
        )



def _fidelity(source_hash: str = "sha256:source") -> DocumentFidelity:
    bbox_unknown = {"bbox": None, "bbox_unknown_reason": "synthetic fixture has no geometry"}
    blocks = (
        DocumentBlock(
            block_id="p1-source-first",
            page_number=1,
            kind="text",
            source_order=0,
            reading_order=1,
            order_status="reordered",
            order_reason="column reconstruction",
            source_locator="pdf://paper-1/page/1/block/0",
            text="Value is x = 1",
            inline_math=(
                InlineMathSpan(
                    start_offset=9,
                    end_offset=14,
                    offset_text_version="raw",
                    raw_latex="x = 1",
                    normalized_latex="x=1",
                    source_locator="pdf://paper-1/page/1/math/0",
                ),
            ),
            content_owner_id="p1-source-first",
            **bbox_unknown,
        ),
        DocumentBlock(
            block_id="p1-source-second",
            page_number=1,
            kind="table",
            source_order=1,
            reading_order=0,
            order_status="reordered",
            order_reason="table precedes paragraph in reading order",
            element_ref="table-1",
            source_locator="pdf://paper-1/page/1/table/0",
            text="",
            content_owner_id="p1-source-second",
            **bbox_unknown,
        ),
        DocumentBlock(
            block_id="p2-block",
            page_number=2,
            kind="heading",
            source_order=0,
            reading_order=0,
            order_status="preserved",
            source_locator="pdf://paper-1/page/2/block/0",
            text="Conclusion",
            content_owner_id="p2-block",
            **bbox_unknown,
        ),
    )
    cells = (
        TableCell(
            cell_id="c00", row_index=0, column_index=0, row_span=2,
            status="content", text="A", source_locator="pdf://paper-1/page/1/cell/0",
            header_role="column", **bbox_unknown,
        ),
        TableCell(
            cell_id="c01", row_index=0, column_index=1,
            status="content", text="B", source_locator="pdf://paper-1/page/1/cell/1",
            header_role="column", **bbox_unknown,
        ),
        TableCell(
            cell_id="c10", row_index=1, column_index=0,
            status="covered", covered_by_cell_id="c00",
            source_locator="pdf://paper-1/page/1/cell/2", **bbox_unknown,
        ),
        TableCell(
            cell_id="c11", row_index=1, column_index=1,
            status="content", text="2", source_locator="pdf://paper-1/page/1/cell/3",
            **bbox_unknown,
        ),
    )
    return DocumentFidelity(
        schema_version=DOCUMENT_FIDELITY_SCHEMA,
        normalization_version="normalization-v1",
        source_hash=source_hash,
        parser_fingerprint="parser-v1",
        profile_fingerprint="profile-v1",
        content_checksum="sha256:content",
        pages=(
            DocumentPage(pdf_page_number=1, width=612, height=792, dimension_unit="pt"),
            DocumentPage(pdf_page_number=2, width=612, height=792, dimension_unit="pt"),
        ),
        blocks=blocks,
        tables=(
            TableGrid(
                table_id="table-1",
                block_id="p1-source-second",
                row_count=2,
                column_count=2,
                has_header=False,
                source_locator="pdf://paper-1/page/1/table/0",
                cells=cells,
            ),
        ),
    )


def test_fidelity_is_authoritative_immutable_typed_roundtrip_with_order_spans_and_grid() -> None:
    fidelity = _fidelity()
    document = _document(
        fidelity=fidelity,
        # Conflicting legacy projections must not be mixed into fidelity output.
        sections=[ResearchSection(
            section_id="legacy-section", title="Legacy", text="not authoritative",
            page_start=1, page_end=1, source_ref="paper://paper-1#page=1",
        )],
        tables=[ResearchTable(
            table_id="legacy-table", caption="Legacy table", page=1,
            source_ref="paper://paper-1#page=1",
        )],
    )

    report = export_research_document(
        document, page_count=2, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED
    )

    assert report.status is ExportStatus.SUPPORTED
    assert report.fidelity is fidelity
    assert report.content == ()
    assert report.unsupported_dimensions == ()
    assert [page.content_ids for page in report.pages] == [
        ("p1-source-second", "p1-source-first"), ("p2-block",)
    ]
    assert [(block.block_id, block.source_order, block.reading_order) for block in report.fidelity.blocks] == [
        ("p1-source-first", 0, 1), ("p1-source-second", 1, 0), ("p2-block", 0, 0)
    ]
    assert report.fidelity.blocks[0].inline_math[0].raw_latex == "x = 1"
    assert report.fidelity.blocks[0].inline_math[0].source_locator.endswith("math/0")
    assert report.fidelity.tables[0].cells[0].row_span == 2
    assert report.fidelity.tables[0].cells[2].covered_by_cell_id == "c00"

    restored = type(report).model_validate_json(report.model_dump_json())
    assert restored == report
    assert isinstance(restored.fidelity, DocumentFidelity)
    assert isinstance(restored.fidelity.blocks, tuple)
    with pytest.raises((AttributeError, TypeError, ValidationError)):
        report.fidelity.blocks[0].text = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("fidelity_hash", "page_count", "reason"),
    [
        ("sha256:other", 2, "fidelity_source_hash_mismatch"),
        ("sha256:source", 1, "fidelity_page_count_mismatch"),
    ],
)
def test_fidelity_hash_or_page_count_mismatch_fails_closed_without_legacy_fallback(
    fidelity_hash: str, page_count: int, reason: str
) -> None:
    document = _document(
        fidelity=_fidelity(fidelity_hash),
        sections=[ResearchSection(
            section_id="legacy", title="Legacy", text="must not leak", page_start=1,
            page_end=1, source_ref="paper://paper-1#page=1",
        )],
    )

    report = export_research_document(
        document, page_count=page_count, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED
    )

    assert report.status is ExportStatus.UNAVAILABLE
    assert report.fidelity is None
    assert report.content == ()
    assert report.supported_content_count == 0
    assert report.unsupported_dimensions[0].reason == reason
    assert all(page.status is ExportStatus.UNAVAILABLE and page.reason == reason for page in report.pages)


def test_fidelity_none_keeps_legacy_projection_partial_and_marks_limits() -> None:
    document = _document(
        sections=[ResearchSection(
            section_id="legacy", title="Legacy", text="legacy body", page_start=1,
            page_end=1, source_ref="paper://paper-1#page=1",
        )],
        tables=[ResearchTable(
            table_id="legacy-table", caption="Legacy table", page=1,
            source_ref="paper://paper-1#page=1",
        )],
    )

    report = export_research_document(
        document, page_count=1, page_convention=PageConvention.PHYSICAL_PDF_ONE_BASED
    )

    assert report.fidelity is None
    assert report.status is ExportStatus.PARTIAL
    assert report.content[0].text == "legacy body"
    assert report.content[1].status is ExportStatus.UNAVAILABLE
    assert {item.dimension for item in report.unsupported_dimensions} >= {"reading_order", "table_grid"}
