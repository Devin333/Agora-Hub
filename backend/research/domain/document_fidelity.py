from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from backend.foundation import PrimitiveModel


DOCUMENT_FIDELITY_SCHEMA = "research-document-fidelity.v1"
MAX_PAGES = 10_000
MAX_BLOCKS = 50_000
MAX_TABLE_ROWS = 1_000
MAX_TABLE_COLUMNS = 1_000
MAX_TABLE_SLOTS = 20_000
MAX_TEXT_CHARS = 1_000_000
MAX_LOCATOR_CHARS = 4_096
MAX_MATH_CHARS = 100_000

def _require_non_whitespace(value: str) -> str:
    if not value.strip():
        raise ValueError("value must contain non-whitespace text")
    return value


NonEmptyText = Annotated[
    str,
    Field(min_length=1, max_length=MAX_LOCATOR_CHARS),
    AfterValidator(_require_non_whitespace),
]
BoundedText = Annotated[str, Field(max_length=MAX_TEXT_CHARS)]
Index = Annotated[int, Field(ge=0)]
PositiveIndex = Annotated[int, Field(ge=1)]
FiniteCoordinate = Annotated[float, Field(ge=-1_000_000, le=1_000_000, allow_inf_nan=False)]


def _as_tuple(value: object) -> object:
    # JSON arrays and ordinary Python lists are the natural wire/input shape;
    # normalize only the container so strict scalar validation remains intact.
    return tuple(value) if isinstance(value, list) else value


_FROZEN_SEQUENCE = BeforeValidator(_as_tuple)


class _FidelityModel(PrimitiveModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        populate_by_name=True,
        validate_assignment=True,
        arbitrary_types_allowed=False,
    )


class BoundingBox(_FidelityModel):
    """A finite rectangle with explicit coordinate semantics."""

    x0: FiniteCoordinate
    y0: FiniteCoordinate
    x1: FiniteCoordinate
    y1: FiniteCoordinate
    coordinate_space: Literal["pdf_points", "page_pixels", "normalized"]
    unit: Literal["pt", "px", "ratio"]
    origin: Literal["top_left", "bottom_left"]
    rotation_degrees: Literal[0, 90, 180, 270] = 0

    @model_validator(mode="after")
    def _validate_rectangle(self) -> "BoundingBox":
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("bounding box must have positive width and height")
        expected_unit = {
            "pdf_points": "pt",
            "page_pixels": "px",
            "normalized": "ratio",
        }[self.coordinate_space]
        if self.unit != expected_unit:
            raise ValueError("bounding box unit does not match coordinate_space")
        if self.coordinate_space == "normalized" and (
            self.x0 < 0 or self.y0 < 0 or self.x1 > 1 or self.y1 > 1
        ):
            raise ValueError("normalized bounding box coordinates must be within [0, 1]")
        return self


class DocumentPage(_FidelityModel):
    pdf_page_number: PositiveIndex
    printed_page_label: NonEmptyText | None = None
    width: Annotated[float, Field(gt=0, le=100_000, allow_inf_nan=False)]
    height: Annotated[float, Field(gt=0, le=100_000, allow_inf_nan=False)]
    dimension_unit: Literal["pt", "px"]
    rotation_degrees: Literal[0, 90, 180, 270] = 0
    backend_page_id: NonEmptyText | None = None


class InlineMathSpan(_FidelityModel):
    start_offset: Index
    end_offset: PositiveIndex
    offset_basis: Literal["unicode_codepoint"] = "unicode_codepoint"
    offset_text_version: Literal["raw", "normalized"]
    raw_latex: Annotated[str, Field(min_length=1, max_length=MAX_MATH_CHARS)]
    normalized_latex: Annotated[str, Field(max_length=MAX_MATH_CHARS)] | None = None
    display: bool = False
    source_locator: NonEmptyText

    @model_validator(mode="after")
    def _validate_span(self) -> "InlineMathSpan":
        if self.end_offset <= self.start_offset:
            raise ValueError("inline math offset range must be non-empty")
        return self


class DocumentBlock(_FidelityModel):
    block_id: NonEmptyText
    page_number: PositiveIndex
    parent_id: NonEmptyText | None = None
    kind: Literal[
        "text", "heading", "caption", "table", "figure", "equation", "list",
        "footnote", "header", "footer", "other",
    ]
    source_order: Index
    reading_order: Index
    order_status: Literal["preserved", "reordered", "uncertain"]
    order_reason: NonEmptyText | None = None
    element_ref: NonEmptyText | None = None
    source_locator: NonEmptyText
    bbox: BoundingBox | None = None
    bbox_unknown_reason: NonEmptyText | None = None
    text: BoundedText = ""
    # Structural presence does not imply that a formula was transcribed.
    # Unavailable equations retain their source identity but cannot claim facts.
    content_status: Literal["available", "unavailable"] = "available"
    content_unavailable_reason: NonEmptyText | None = None
    inline_math: Annotated[tuple[InlineMathSpan, ...], _FROZEN_SEQUENCE, Field(max_length=10_000)] = ()
    # Every block points to the single block that owns its projected content.
    # Self-reference denotes ownership; a container may own several child nodes.
    content_owner_id: NonEmptyText

    @model_validator(mode="after")
    def _validate_local_contract(self) -> "DocumentBlock":
        if self.content_status == "unavailable":
            if self.kind != "equation":
                raise ValueError("only equation blocks support unavailable content")
            if self.content_unavailable_reason is None:
                raise ValueError("unavailable equation content requires a reason")
            if self.text or self.inline_math or self.element_ref is not None:
                raise ValueError("unavailable equation cannot claim text, math, or an element reference")
        elif self.content_unavailable_reason is not None:
            raise ValueError("available content cannot declare an unavailable reason")
        if (self.bbox is None) == (self.bbox_unknown_reason is None):
            raise ValueError("provide exactly one of bbox or bbox_unknown_reason")
        if self.order_status in {"uncertain", "reordered"} and self.order_reason is None:
            raise ValueError("uncertain or reordered reading order requires order_reason")
        if self.order_status == "preserved" and self.order_reason is not None:
            raise ValueError("preserved reading order cannot claim a reorder reason")
        for span in self.inline_math:
            if span.end_offset > len(self.text):
                raise ValueError("inline math offsets must be within block text")
        spans = sorted(self.inline_math, key=lambda item: (item.start_offset, item.end_offset))
        if any(current.start_offset < previous.end_offset for previous, current in zip(spans, spans[1:])):
            raise ValueError("inline math spans must not overlap")
        return self


class TableHeaderPath(_FidelityModel):
    column_index: Index
    cell_ids: Annotated[tuple[NonEmptyText, ...], _FROZEN_SEQUENCE, Field(min_length=1, max_length=100)]


class TableCell(_FidelityModel):
    cell_id: NonEmptyText
    row_index: Index
    column_index: Index
    row_span: Annotated[int, Field(ge=1, le=MAX_TABLE_ROWS)] = 1
    column_span: Annotated[int, Field(ge=1, le=MAX_TABLE_COLUMNS)] = 1
    status: Literal["content", "blank", "covered", "missing", "unreadable"]
    covered_by_cell_id: NonEmptyText | None = None
    header_role: Literal["column", "row", "corner", "none"] = "none"
    text: BoundedText = ""
    raw_text: BoundedText | None = None
    inline_math: Annotated[tuple[InlineMathSpan, ...], _FROZEN_SEQUENCE, Field(max_length=1_000)] = ()
    source_locator: NonEmptyText
    bbox: BoundingBox | None = None
    bbox_unknown_reason: NonEmptyText | None = None
    unreadable_reason: NonEmptyText | None = None

    @model_validator(mode="after")
    def _validate_state(self) -> "TableCell":
        if (self.bbox is None) == (self.bbox_unknown_reason is None):
            raise ValueError("provide exactly one of bbox or bbox_unknown_reason")
        if self.status == "covered":
            if self.covered_by_cell_id is None:
                raise ValueError("covered cell must identify its spanning owner")
            if self.row_span != 1 or self.column_span != 1 or self.text or self.raw_text is not None or self.inline_math:
                raise ValueError("covered cell cannot own span or content")
            if self.header_role != "none":
                raise ValueError("covered cell cannot declare a header role")
        elif self.covered_by_cell_id is not None:
            raise ValueError("only covered cells may reference covered_by_cell_id")
        if self.status == "blank" and (self.text or self.inline_math):
            raise ValueError("blank cell cannot contain normalized text or math")
        if self.status == "blank" and self.raw_text is not None and self.raw_text.strip():
            raise ValueError("blank cell raw text must contain only whitespace")
        if self.status in {"missing", "unreadable"} and (self.text or self.raw_text is not None or self.inline_math):
            raise ValueError("missing or unreadable cell cannot claim content")
        if self.status == "missing" and (self.row_span != 1 or self.column_span != 1):
            raise ValueError("missing cell cannot span grid slots")
        if self.status == "unreadable" and self.unreadable_reason is None:
            raise ValueError("unreadable cell requires unreadable_reason")
        if self.status != "unreadable" and self.unreadable_reason is not None:
            raise ValueError("unreadable_reason is only valid for unreadable cells")
        for span in self.inline_math:
            offset_text = self.raw_text if span.offset_text_version == "raw" and self.raw_text is not None else self.text
            if span.end_offset > len(offset_text):
                raise ValueError("cell inline math offsets must be within the declared cell text version")
        spans = sorted(self.inline_math, key=lambda item: (item.start_offset, item.end_offset))
        if any(current.start_offset < previous.end_offset for previous, current in zip(spans, spans[1:])):
            raise ValueError("cell inline math spans must not overlap")
        return self


class TableGrid(_FidelityModel):
    table_id: NonEmptyText
    block_id: NonEmptyText
    row_count: Annotated[int, Field(ge=1, le=MAX_TABLE_ROWS)]
    column_count: Annotated[int, Field(ge=1, le=MAX_TABLE_COLUMNS)]
    has_header: bool
    source_locator: NonEmptyText
    cells: Annotated[tuple[TableCell, ...], _FROZEN_SEQUENCE, Field(max_length=MAX_TABLE_SLOTS)]
    header_paths: Annotated[tuple[TableHeaderPath, ...], _FROZEN_SEQUENCE, Field(max_length=MAX_TABLE_COLUMNS)] = ()

    @model_validator(mode="after")
    def _validate_complete_grid(self) -> "TableGrid":
        slot_count = self.row_count * self.column_count
        if slot_count > MAX_TABLE_SLOTS:
            raise ValueError(f"table grid exceeds {MAX_TABLE_SLOTS} addressable slots")
        if len(self.cells) != slot_count:
            raise ValueError("table grid must explicitly represent every coordinate")

        by_id: dict[str, TableCell] = {}
        by_coordinate: dict[tuple[int, int], TableCell] = {}
        for cell in self.cells:
            if cell.cell_id in by_id:
                raise ValueError("table cell IDs must be unique")
            if cell.row_index >= self.row_count or cell.column_index >= self.column_count:
                raise ValueError("table cell coordinate is outside grid bounds")
            coordinate = (cell.row_index, cell.column_index)
            if coordinate in by_coordinate:
                raise ValueError("table cell coordinates must be unique")
            by_id[cell.cell_id] = cell
            by_coordinate[coordinate] = cell

        # The slot cap is checked before this bounded occupancy walk; no dense
        # matrix proportional to attacker-controlled dimensions is allocated.
        owners: dict[tuple[int, int], str] = {}
        projected_slots = 0
        for cell in self.cells:
            if cell.status == "covered":
                continue
            row_end = cell.row_index + cell.row_span
            column_end = cell.column_index + cell.column_span
            if row_end > self.row_count or column_end > self.column_count:
                raise ValueError("cell span exceeds table grid bounds")
            projected_slots += cell.row_span * cell.column_span
            if projected_slots > slot_count:
                raise ValueError("table cell spans overlap")
            for row in range(cell.row_index, row_end):
                for column in range(cell.column_index, column_end):
                    coordinate = (row, column)
                    if coordinate in owners:
                        raise ValueError("table cell spans overlap")
                    owners[coordinate] = cell.cell_id

        if len(owners) != slot_count:
            raise ValueError("table grid contains unaccounted coordinates")
        # Validate the explicit slot records only after all anchor footprints
        # are known, so overlapping spans are diagnosed regardless of input
        # cell order and cannot be masked by a conflicting covered-slot status.
        for coordinate, owner_id in owners.items():
            slot = by_coordinate.get(coordinate)
            if slot is None:
                raise ValueError("table grid contains an unaccounted coordinate")
            owner = by_id[owner_id]
            if coordinate == (owner.row_index, owner.column_index):
                if slot.cell_id != owner_id:
                    raise ValueError("table grid anchor coordinate has conflicting ownership")
            elif slot.status != "covered":
                raise ValueError("spanned coordinate must have an explicit covered cell")
            elif slot.covered_by_cell_id != owner_id:
                raise ValueError("covered cell owner does not match spanning cell")

        for cell in self.cells:
            if cell.status == "covered":
                owner = by_id.get(cell.covered_by_cell_id or "")
                if owner is None or owner.status == "covered":
                    raise ValueError("covered cell must reference a spanning anchor")
                if owners.get((cell.row_index, cell.column_index)) != owner.cell_id:
                    raise ValueError("covered cell is not inside its owner's span")

        paths_by_column: set[int] = set()
        for path in self.header_paths:
            if path.column_index >= self.column_count or path.column_index in paths_by_column:
                raise ValueError("header paths must have unique in-range columns")
            paths_by_column.add(path.column_index)
            previous_header_row = -1
            for cell_id in path.cell_ids:
                cell = by_id.get(cell_id)
                if cell is None or cell.status not in {"content", "blank"}:
                    raise ValueError("header path must reference readable anchor cells")
                if cell.row_index <= previous_header_row:
                    raise ValueError("header path cells must be ordered from top to bottom")
                previous_header_row = cell.row_index
                if cell.header_role not in {"column", "corner"}:
                    raise ValueError("header path cells must have column or corner header role")
                if not (cell.column_index <= path.column_index < cell.column_index + cell.column_span):
                    raise ValueError("header path cell does not cover its declared column")
        if self.has_header and paths_by_column != set(range(self.column_count)):
            raise ValueError("a table declaring headers must provide a path for every column")
        if not self.has_header and self.header_paths:
            raise ValueError("header paths are invalid when has_header is false")
        return self


class DocumentFidelity(_FidelityModel):
    """Validated, versioned source-fidelity structure; presence means validated."""

    schema_version: Literal["research-document-fidelity.v1"]
    normalization_version: NonEmptyText
    source_hash: NonEmptyText
    parser_fingerprint: NonEmptyText
    profile_fingerprint: NonEmptyText
    content_checksum: NonEmptyText
    artifact_refs: Annotated[tuple[NonEmptyText, ...], _FROZEN_SEQUENCE, Field(max_length=10_000)] = ()
    pages: Annotated[tuple[DocumentPage, ...], _FROZEN_SEQUENCE, Field(min_length=1, max_length=MAX_PAGES)]
    blocks: Annotated[tuple[DocumentBlock, ...], _FROZEN_SEQUENCE, Field(max_length=MAX_BLOCKS)] = ()
    tables: Annotated[tuple[TableGrid, ...], _FROZEN_SEQUENCE, Field(max_length=MAX_BLOCKS)] = ()

    @field_validator("artifact_refs")
    @classmethod
    def _unique_artifact_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("artifact_refs must be unique")
        return value

    @model_validator(mode="after")
    def _validate_document_graph(self) -> "DocumentFidelity":
        page_numbers = [page.pdf_page_number for page in self.pages]
        if page_numbers != sorted(set(page_numbers)):
            raise ValueError("pages must have unique ascending one-based physical page numbers")
        page_set = set(page_numbers)

        blocks_by_id: dict[str, DocumentBlock] = {}
        per_page: dict[int, list[DocumentBlock]] = {number: [] for number in page_numbers}
        for block in self.blocks:
            if block.block_id in blocks_by_id:
                raise ValueError("block IDs must be unique")
            if block.page_number not in page_set:
                raise ValueError("block references a page absent from document fidelity")
            blocks_by_id[block.block_id] = block
            per_page[block.page_number].append(block)

        for page_blocks in per_page.values():
            expected = list(range(len(page_blocks)))
            if sorted(block.source_order for block in page_blocks) != expected:
                raise ValueError("source_order must cover each page contiguously without duplicates")
            if sorted(block.reading_order for block in page_blocks) != expected:
                raise ValueError("reading_order must cover each page contiguously without duplicates")

        for block in self.blocks:
            if block.parent_id is not None:
                parent = blocks_by_id.get(block.parent_id)
                if parent is None:
                    raise ValueError("parent_id must reference an existing block")
                if parent.page_number != block.page_number:
                    raise ValueError("parent and child blocks must belong to the same page")
            if block.content_owner_id not in blocks_by_id:
                raise ValueError("content_owner_id must reference an existing block")
            if block.kind == "table" and block.element_ref is None:
                raise ValueError("table blocks must reference their canonical element")

        self._assert_acyclic(blocks_by_id, relation="parent_id")
        self._assert_acyclic(blocks_by_id, relation="content_owner_id")
        for block in self.blocks:
            owner = blocks_by_id[block.content_owner_id]
            if owner.content_owner_id != owner.block_id:
                raise ValueError("content ownership must resolve to a self-owning block")

        table_ids: set[str] = set()
        table_block_ids: set[str] = set()
        for table in self.tables:
            if table.table_id in table_ids:
                raise ValueError("table IDs must be unique")
            if table.block_id in table_block_ids:
                raise ValueError("a block cannot own multiple table grids")
            block = blocks_by_id.get(table.block_id)
            if block is None or block.kind != "table":
                raise ValueError("table grid must reference an existing table block")
            if block.element_ref != table.table_id:
                raise ValueError("table block element_ref must match table_id")
            table_ids.add(table.table_id)
            table_block_ids.add(table.block_id)
        if {block.block_id for block in self.blocks if block.kind == "table"} != table_block_ids:
            raise ValueError("every table block must have exactly one table grid")
        return self

    @staticmethod
    def _assert_acyclic(blocks: dict[str, DocumentBlock], *, relation: str) -> None:
        # Each node has at most one outgoing edge, so tri-color traversal checks
        # the complete graph in O(nodes) time rather than re-walking every path.
        state: dict[str, int] = {}
        for start_id in blocks:
            if state.get(start_id, 0) == 2:
                continue
            path: list[str] = []
            current_id: str | None = start_id
            while current_id is not None and state.get(current_id, 0) == 0:
                state[current_id] = 1
                path.append(current_id)
                current = blocks[current_id]
                next_id = getattr(current, relation)
                if relation == "content_owner_id" and next_id == current_id:
                    next_id = None
                current_id = next_id
            if current_id is not None and state.get(current_id) == 1:
                raise ValueError(f"{relation} relationships must be acyclic")
            for block_id in path:
                state[block_id] = 2


__all__ = [
    "DOCUMENT_FIDELITY_SCHEMA",
    "MAX_BLOCKS",
    "MAX_PAGES",
    "MAX_TABLE_COLUMNS",
    "MAX_TABLE_ROWS",
    "MAX_TABLE_SLOTS",
    "BoundingBox",
    "DocumentBlock",
    "DocumentFidelity",
    "DocumentPage",
    "InlineMathSpan",
    "TableCell",
    "TableGrid",
    "TableHeaderPath",
]
