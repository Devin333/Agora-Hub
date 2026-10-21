from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend.research.domain.common import SourceLineage, stable_research_id
from backend.research.domain.document import ResearchDocument
from backend.research.domain.document_fidelity import (
    MAX_TABLE_SLOTS,
    BoundingBox,
    DocumentBlock,
    DocumentFidelity,
    DocumentPage,
    InlineMathSpan,
    TableCell,
    TableGrid,
)


def _bbox() -> dict[str, object]:
    return {
        "x0": 10.0,
        "y0": 20.0,
        "x1": 100.0,
        "y1": 120.0,
        "coordinate_space": "pdf_points",
        "unit": "pt",
        "origin": "top_left",
        "rotation_degrees": 0,
    }


def _cell(
    cell_id: str,
    row: int,
    column: int,
    status: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "cell_id": cell_id,
        "row_index": row,
        "column_index": column,
        "status": status,
        "source_locator": f"pdf://source/page/1/table/t1/cell/{cell_id}",
        "bbox": _bbox(),
        **extra,
    }


def _complex_grid() -> TableGrid:
    cells = (
        TableCell(
            **_cell("h-group", 0, 0, "content", column_span=2, header_role="column", text="Model"),
        ),
        TableCell(
            **_cell("h-group-covered", 0, 1, "covered", covered_by_cell_id="h-group"),
        ),
        TableCell(**_cell("h-score", 0, 2, "content", header_role="column", text="Score")),
        TableCell(**_cell("h-model", 1, 0, "content", header_role="column", text="Model")),
        TableCell(**_cell("h-variant", 1, 1, "content", header_role="column", text="Variant")),
        TableCell(**_cell("h-score-2", 1, 2, "content", header_role="column", text="Score")),
        TableCell(**_cell("v-model", 2, 0, "content", text="U-Net")),
        TableCell(**_cell("v-variant", 2, 1, "blank")),
        TableCell(**_cell("v-score", 2, 2, "content", text="−0.01")),
        TableCell(**_cell("v-model-2", 3, 0, "content", text="Baseline")),
        TableCell(**_cell("v-missing", 3, 1, "missing")),
        TableCell(
            **_cell("v-unreadable", 3, 2, "unreadable", unreadable_reason="source pixels are obscured"),
        ),
    )
    return TableGrid(
        table_id="t1",
        block_id="table-block",
        row_count=4,
        column_count=3,
        has_header=True,
        source_locator="pdf://source/page/1/table/t1",
        cells=cells,
        header_paths=(
            {"column_index": 0, "cell_ids": ("h-group", "h-model")},
            {"column_index": 1, "cell_ids": ("h-group", "h-variant")},
            {"column_index": 2, "cell_ids": ("h-score", "h-score-2")},
        ),
    )


def _valid_fidelity() -> DocumentFidelity:
    body = "Prior α + β follows."
    formula_start = body.index("α")
    return DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="research-normalizer.v1",
        source_hash="sha256:source",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:content",
        artifact_refs=("artifact://raw/marker.json",),
        pages=(
            DocumentPage(
                pdf_page_number=1,
                printed_page_label="iv",
                width=612.0,
                height=792.0,
                dimension_unit="pt",
                rotation_degrees=0,
                backend_page_id="marker-page-0",
            ),
        ),
        blocks=(
            DocumentBlock(
                block_id="body",
                page_number=1,
                kind="text",
                source_order=0,
                reading_order=0,
                order_status="preserved",
                source_locator="pdf://source/page/1/block/body",
                bbox=BoundingBox(**_bbox()),
                text=body,
                inline_math=(
                    InlineMathSpan(
                        start_offset=formula_start,
                        end_offset=formula_start + len("α + β"),
                        raw_latex=r"\alpha + \beta",
                        offset_text_version="raw",
                        normalized_latex=r"\alpha+\beta",
                        source_locator="pdf://source/page/1/block/body/math/0",
                    ),
                ),
                content_owner_id="body",
            ),
            DocumentBlock(
                block_id="table-block",
                page_number=1,
                parent_id="body",
                kind="table",
                source_order=1,
                reading_order=1,
                order_status="preserved",
                element_ref="t1",
                source_locator="pdf://source/page/1/block/table",
                bbox_unknown_reason="backend did not provide reliable table bounds",
                content_owner_id="table-block",
            ),
            DocumentBlock(
                block_id="display-equation",
                page_number=1,
                kind="equation",
                source_order=2,
                reading_order=2,
                order_status="preserved",
                element_ref="equation:eq-1",
                source_locator="pdf://source/page/1/equation/eq-1",
                bbox=_bbox(),
                text="E=mc²",
                inline_math=(
                    InlineMathSpan(
                        start_offset=0,
                        end_offset=5,
                        raw_latex="E=mc^2",
                        offset_text_version="raw",
                        display=True,
                        source_locator="pdf://source/page/1/equation/eq-1/math",
                    ),
                ),
                content_owner_id="display-equation",
            ),
        ),
        tables=(_complex_grid(),),
    )


def _fidelity_payload() -> dict[str, object]:
    return _valid_fidelity().model_dump(mode="json")


def test_complex_table_math_geometry_and_order_contract_roundtrip() -> None:
    fidelity = _valid_fidelity()

    restored = DocumentFidelity.model_validate_json(fidelity.model_dump_json())

    assert restored == fidelity
    grid = restored.tables[0]
    assert grid.row_count == 4 and grid.column_count == 3
    assert grid.cells[1].status == "covered"
    assert grid.cells[7].status == "blank"
    assert grid.cells[10].status == "missing"
    assert grid.cells[11].status == "unreadable"
    assert grid.header_paths[0].cell_ids == ("h-group", "h-model")
    assert restored.blocks[0].inline_math[0].raw_latex == r"\alpha + \beta"
    assert restored.blocks[2].inline_math[0].display is True
    with pytest.raises((ValidationError, TypeError)):
        fidelity.blocks[0].text = "mutated"  # type: ignore[misc]


def test_old_research_document_has_unverified_fidelity_and_unchanged_identity() -> None:
    document = ResearchDocument(
        paper_id="paper-123",
        source_hash="source-hash",
        lineage=SourceLineage(source_refs=["https://example.test/paper.pdf"]),
    )

    assert document.fidelity is None
    assert document.document_id == stable_research_id("research_document", "paper-123", "source-hash")
    payload = document.model_dump(mode="json")
    assert payload["fidelity"] is None
    assert ResearchDocument.model_validate(payload).fidelity is None


def test_document_fidelity_roundtrips_inside_document_json_schema() -> None:
    document = ResearchDocument(
        paper_id="paper-123",
        source_hash="source-hash",
        lineage=SourceLineage(source_refs=["https://example.test/paper.pdf"]),
        fidelity=_valid_fidelity(),
    )

    restored = ResearchDocument.model_validate_json(document.model_dump_json())

    assert restored.fidelity == document.fidelity
    assert restored.document_id == document.document_id
    assert json.loads(restored.model_dump_json())["fidelity"]["schema_version"] == "research-document-fidelity.v1"


def test_page_numbers_bbox_and_unknown_bbox_reason_are_strict() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        DocumentPage(pdf_page_number=0, width=612, height=792, dimension_unit="pt")
    with pytest.raises(ValidationError, match="finite number"):
        BoundingBox(**{**_bbox(), "x0": float("inf")})
    with pytest.raises(ValidationError, match="unit does not match"):
        BoundingBox(**{**_bbox(), "unit": "px"})
    with pytest.raises(ValidationError, match="non-whitespace"):
        DocumentPage(
            pdf_page_number=1,
            printed_page_label="   ",
            width=612.0,
            height=792.0,
            dimension_unit="pt",
        )

    block = _valid_fidelity().blocks[1]
    with pytest.raises(ValidationError, match="exactly one"):
        DocumentBlock.model_validate({**block.model_dump(), "bbox": _bbox()})


def test_inline_math_offsets_must_be_nonempty_nonoverlapping_and_within_text() -> None:
    span = {
        "start_offset": 2,
        "end_offset": 5,
        "raw_latex": "x+y",
        "offset_text_version": "raw",
        "source_locator": "pdf://page/1/math/0",
    }
    base = {
        "block_id": "b",
        "page_number": 1,
        "kind": "text",
        "source_order": 0,
        "reading_order": 0,
        "order_status": "preserved",
        "source_locator": "pdf://page/1/block/b",
        "bbox": _bbox(),
        "text": "abc",
        "content_owner_id": "b",
    }
    with pytest.raises(ValidationError, match="within block text"):
        DocumentBlock(**{**base, "inline_math": (span,)})
    with pytest.raises(ValidationError, match="overlap"):
        DocumentBlock(
            **{
                **base,
                "text": "0123456789",
                "inline_math": (
                    {**span, "start_offset": 1, "end_offset": 5},
                    {**span, "start_offset": 4, "end_offset": 8},
                ),
            }
        )
    with pytest.raises(ValidationError, match="non-empty"):
        InlineMathSpan(
            start_offset=2, end_offset=2, raw_latex="x", offset_text_version="raw", source_locator="loc"
        )


def test_block_identity_ownership_parent_and_order_coverage_are_validated() -> None:
    payload = _fidelity_payload()
    blocks = payload["blocks"]
    assert isinstance(blocks, list)

    duplicate = [*blocks, dict(blocks[0])]
    with pytest.raises(ValidationError, match="block IDs must be unique"):
        DocumentFidelity.model_validate({**payload, "blocks": duplicate})

    parent_cycle = [dict(item) for item in blocks]
    parent_cycle[0]["parent_id"] = "table-block"
    with pytest.raises(ValidationError, match="parent_id relationships must be acyclic"):
        DocumentFidelity.model_validate({**payload, "blocks": parent_cycle})

    owner_cycle = [dict(item) for item in blocks]
    owner_cycle[0]["content_owner_id"] = "table-block"
    owner_cycle[1]["content_owner_id"] = "body"
    with pytest.raises(ValidationError, match="content_owner_id relationships must be acyclic"):
        DocumentFidelity.model_validate({**payload, "blocks": owner_cycle})

    duplicate_order = [dict(item) for item in blocks]
    duplicate_order[2]["reading_order"] = 1
    with pytest.raises(ValidationError, match="reading_order must cover"):
        DocumentFidelity.model_validate({**payload, "blocks": duplicate_order})

    missing_reorder_reason = [dict(item) for item in blocks]
    missing_reorder_reason[1]["order_status"] = "reordered"
    with pytest.raises(ValidationError, match="requires order_reason"):
        DocumentFidelity.model_validate({**payload, "blocks": missing_reorder_reason})


def test_table_spans_covered_slots_and_header_paths_fail_closed() -> None:
    payload = _valid_fidelity().tables[0].model_dump(mode="json")
    cells = payload["cells"]
    assert isinstance(cells, list)

    missing_covered = [dict(item) for item in cells]
    missing_covered[1]["status"] = "missing"
    missing_covered[1].pop("covered_by_cell_id", None)
    with pytest.raises(ValidationError, match="spans overlap"):
        TableGrid.model_validate({**payload, "cells": missing_covered})

    bad_owner = [dict(item) for item in cells]
    bad_owner[1]["covered_by_cell_id"] = "v-model"
    with pytest.raises(ValidationError, match="covered cell owner"):
        TableGrid.model_validate({**payload, "cells": bad_owner})

    out_of_bounds = [dict(item) for item in cells]
    out_of_bounds[0]["column_span"] = 4
    with pytest.raises(ValidationError, match="span exceeds"):
        TableGrid.model_validate({**payload, "cells": out_of_bounds})

    overlapping_spans = [dict(item) for item in cells]
    overlapping_spans[3]["row_span"] = 2
    with pytest.raises(ValidationError, match="spans overlap"):
        TableGrid.model_validate({**payload, "cells": overlapping_spans})

    duplicate_coordinate = [dict(item) for item in cells]
    duplicate_coordinate[1]["column_index"] = 0
    with pytest.raises(ValidationError, match="coordinates must be unique"):
        TableGrid.model_validate({**payload, "cells": duplicate_coordinate})

    bad_path = [{"column_index": 0, "cell_ids": ("v-model",)}]
    with pytest.raises(ValidationError, match="header role"):
        TableGrid.model_validate({**payload, "header_paths": bad_path})


def test_table_grid_slot_limit_rejects_resource_bombs_before_expansion() -> None:
    base = _valid_fidelity().tables[0].model_dump(mode="json")

    with pytest.raises(ValidationError, match="addressable slots"):
        TableGrid.model_validate(
            {
                **base,
                "row_count": 200,
                "column_count": 101,
                "cells": (),
            }
        )

    with pytest.raises(ValidationError, match="20000 items"):
        TableGrid.model_validate({**base, "cells": [*base["cells"]] * 1_667})


def test_fidelity_rejects_unspecified_pages_and_missing_table_grid() -> None:
    payload = _fidelity_payload()
    with pytest.raises(ValidationError, match="ascending one-based"):
        DocumentFidelity.model_validate({**payload, "pages": [payload["pages"][0], payload["pages"][0]]})

    # Keep the declared table block: the violated invariant is that every
    # table block has exactly one grid, not that absent tables need a grid.
    with pytest.raises(ValidationError, match="every table block must have exactly one table grid"):
        DocumentFidelity.model_validate({**payload, "tables": []})

