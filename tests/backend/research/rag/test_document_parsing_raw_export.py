from __future__ import annotations

import pytest

from backend.research.rag.evaluation.document_parsing_raw_export import (
    MARKER_TREE_PAGE_ID_V1,
    BackendRawSchema,
    PageConvention,
    RawExportIssueCode,
    SourcePage,
    export_backend_raw,
)


@pytest.fixture
def source_pages() -> tuple[SourcePage, ...]:
    return (SourcePage("dataset-page-a"), SourcePage("dataset-page-b"))


def page(index: int, children: list[dict[str, object]], *, suffix: int = 10) -> dict[str, object]:
    return {
        "id": f"/page/{index}/Page/{suffix + index}",
        "block_type": "Page",
        "bbox": [0, 0, 100, 100],
        "children": children,
    }


def document(*pages: dict[str, object]) -> dict[str, object]:
    return {"block_type": "Document", "children": list(pages)}


def codes(result) -> set[RawExportIssueCode]:
    return {issue.code for issue in result.issues}


def test_marker_page_id_zero_maps_to_physical_page_one(source_pages) -> None:
    result = export_backend_raw(
        document(page(0, [{"id": "/page/0/Equation/1", "block_type": "Equation", "text": "raw", "html": "<math>raw</math>", "latex": r"\alpha + 1"}]), page(1, [])),
        artifact_ref="artifact://raw/input.json",
        input_pages=source_pages,
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert result.pages[0].pdf_page_number == 1
    assert result.pages[0].source_page_identity == "dataset-page-a"
    assert result.pages[0].blocks[0].backend_block_identity == "/page/0/Equation/1"
    assert result.pages[0].blocks[0].text == "raw"
    assert result.pages[0].blocks[0].html == "<math>raw</math>"
    assert result.pages[0].blocks[0].latex == r"\alpha + 1"
    assert result.status == "diagnostic"
    assert not result.issues


def test_one_based_page_number_schema_maps_without_guessing(source_pages) -> None:
    schema = BackendRawSchema(
        schema_id="fixture.page-number.v1",
        root_block_type="Document",
        page_block_type="Page",
        page_convention=PageConvention.PAGE_NUMBER_ONE_BASED,
        page_field="page_number",
        container_block_types=frozenset({"Document", "Page"}),
        supported_block_types=frozenset({"Text"}),
    )
    result = export_backend_raw(
        document(
            {"id": "backend-page-a", "block_type": "Page", "page_number": 1, "children": []},
            {"id": "backend-page-b", "block_type": "Page", "page_number": 2, "children": []},
        ),
        artifact_ref="artifact://raw/one-based.json",
        input_pages=source_pages,
        schema=schema,
    )

    assert [item.pdf_page_number for item in result.pages] == [1, 2]
    assert not result.issues


def test_out_of_range_page_is_not_clamped(source_pages) -> None:
    result = export_backend_raw(
        document(page(0, []), page(2, [])),
        artifact_ref="artifact://raw/out-of-range.json",
        input_pages=source_pages,
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert RawExportIssueCode.PAGE_OUT_OF_RANGE in codes(result)
    assert RawExportIssueCode.MISSING_PAGE in codes(result)
    assert [item.pdf_page_number for item in result.pages] == [1, 2]
    assert result.pages[1].blocks == ()


def test_conflicting_page_fields_fail_closed(source_pages) -> None:
    first = page(0, [])
    first["page_idx"] = 1  # /page/0 is physical page 1, page_idx=1 is page 2.
    result = export_backend_raw(
        document(first, page(1, [])),
        artifact_ref="artifact://raw/conflict.json",
        input_pages=source_pages,
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert RawExportIssueCode.CONFLICTING_PAGE_FIELDS in codes(result)
    assert result.pages[0].blocks == ()


def test_missing_page_and_empty_page_are_both_accounted(source_pages) -> None:
    result = export_backend_raw(
        document(page(0, [])),
        artifact_ref="artifact://raw/missing-page.json",
        input_pages=source_pages,
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert len(result.pages) == 2
    assert result.pages[0].blocks == ()  # present, valid, intentionally empty
    assert result.pages[1].blocks == ()  # requested but absent from backend output
    assert RawExportIssueCode.MISSING_PAGE in codes(result)


def test_nested_table_exports_only_semantic_content_owner() -> None:
    payload = document(
        page(0, [
            {
                "id": "/page/0/TableGroup/1",
                "block_type": "TableGroup",
                "html": "group summary",
                "children": [{
                    "id": "/page/0/Table/2",
                    "block_type": "Table",
                    "html": "<table><tr><td>cell</td></tr></table>",
                    "children": [{
                        "id": "/page/0/TableCell/3",
                        "block_type": "TableCell",
                        "html": "<td>cell</td>",
                        "children": [],
                    }],
                }, {
                    "id": "/page/0/Caption/4",
                    "block_type": "Caption",
                    "text": "table caption",
                    "children": [],
                }],
            }
        ]),
        page(1, []),
    )
    result = export_backend_raw(
        payload,
        artifact_ref="artifact://raw/nested.json",
        input_pages=(SourcePage("source-a"), SourcePage("source-b")),
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert [block.backend_block_type for block in result.pages[0].blocks] == ["Table", "Caption"]
    assert result.pages[0].blocks[0].html == "<table><tr><td>cell</td></tr></table>"
    assert result.pages[0].blocks[0].source_locator.endswith("#block=/page/0/Table/2")
    assert result.pages[0].blocks[1].text == "table caption"
    assert not result.issues


def test_nested_duplicate_content_has_one_owner(source_pages) -> None:
    result = export_backend_raw(
        document(page(0, [{
            "id": "/page/0/Text/1",
            "block_type": "Text",
            "text": "same",
            "children": [{
                "id": "/page/0/Text/2",
                "block_type": "Text",
                "text": "same",
                "children": [],
            }],
        }]), page(1, [])),
        artifact_ref="artifact://raw/duplicate.json",
        input_pages=source_pages,
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert [block.backend_block_identity for block in result.pages[0].blocks] == ["/page/0/Text/2"]
    assert not result.issues


def test_unknown_node_format_is_not_serialized(source_pages) -> None:
    result = export_backend_raw(
        document(page(0, [{"id": "/page/0/Mystery/1", "block_type": "Mystery", "text": "x"}]), page(1, [])),
        artifact_ref="artifact://raw/unknown.json",
        input_pages=source_pages,
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert RawExportIssueCode.UNKNOWN_BLOCK_FORMAT in codes(result)
    assert result.pages[0].blocks == ()


def test_invalid_batch_page_identity_is_reported() -> None:
    result = export_backend_raw(
        document(page(0, [])),
        artifact_ref="artifact://raw/batch.json",
        input_pages=(SourcePage("duplicate"), SourcePage("duplicate")),
        schema=MARKER_TREE_PAGE_ID_V1,
    )

    assert RawExportIssueCode.BATCH_MAPPING_ERROR in codes(result)
