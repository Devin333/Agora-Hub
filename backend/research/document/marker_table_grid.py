from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from backend.foundation import build_stable_id
from backend.research.document.marker_math import (
    MarkerTextSegment,
    MarkerTextSource,
    compose_marker_math_text,
    marker_math_text,
)
from backend.research.domain.document_fidelity import InlineMathSpan, TableCell, TableGrid, TableHeaderPath


class _CellHTMLParser(HTMLParser):
    """Read a single HTML table into source rows without flattening empty slots."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.rows: list[list[dict[str, Any]]] = []
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.invalid_span = False
        self.invalid_structure = False
        self.suppressed_depth = 0
        self.math_depth = 0
        self.math_parts: list[str] = []
        self.math_display = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self.suppressed_depth += 1
            return
        if self.suppressed_depth:
            return
        if tag == "table":
            if self.table_depth:
                self.invalid_structure = True
            self.table_depth += 1
            return
        if self.table_depth != 1:
            return
        if tag == "math" and self.cell is not None:
            if self.math_depth:
                self.cell["unreadable_reason"] = "marker_cell_math_markup_unbalanced"
            values = {key.casefold(): value for key, value in attrs}
            self.math_depth += 1
            self.math_parts = []
            self.math_display = str(values.get("display") or "").casefold() in {"block", "display"}
            return
        if tag == "tr":
            if self.row is not None:
                self.invalid_structure = True
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            if self.cell is not None:
                self.invalid_structure = True
            rowspan = _positive_span(values.get("rowspan"))
            colspan = _positive_span(values.get("colspan"))
            if rowspan is None or colspan is None:
                self.invalid_span = True
                rowspan = colspan = 1
            self.cell = {
                "segments": [],
                "header": tag == "th",
                "row_span": rowspan,
                "column_span": colspan,
                "scope": str(values.get("scope") or "").casefold(),
            }

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript", "template"} and self.suppressed_depth:
            self.suppressed_depth -= 1
            return
        if self.suppressed_depth:
            return
        if self.table_depth == 1 and tag == "math" and self.cell is not None:
            if not self.math_depth:
                self.cell["unreadable_reason"] = "marker_cell_math_markup_unbalanced"
                return
            self.math_depth -= 1
            if self.math_depth == 0:
                raw_latex = "".join(self.math_parts)
                self.cell["segments"].append(MarkerTextSegment(
                    text=raw_latex, raw_latex=raw_latex, display=self.math_display,
                ))
                self.math_parts = []
                self.math_display = False
            return
        if self.table_depth == 1 and tag in {"td", "th"} and self.row is not None and self.cell is not None:
            if self.math_depth:
                self.cell["unreadable_reason"] = "marker_cell_math_markup_unbalanced"
            parsed = compose_marker_math_text(self.cell.pop("segments"))
            self.cell["text"] = parsed.text
            self.cell["math_spans"] = parsed.spans
            self.row.append(self.cell)
            self.cell = None
            # Inline markup never owns a table boundary. Quarantine the cell's
            # content, not its explicit row/column slot or subsequent cells.
            self.math_depth = 0
            self.math_parts = []
            self.math_display = False
        elif self.table_depth == 1 and tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.suppressed_depth or self.cell is None:
            return
        if self.math_depth:
            self.math_parts.append(data)
        else:
            self.cell["segments"].append(MarkerTextSegment(data))


def build_marker_table_grid(
    block: dict[str, Any],
    *,
    table_id: str,
    block_id: str,
    source_locator: str,
) -> TableGrid | None:
    """Create a bounded, explicit grid from Marker matrix/dict/HTML table evidence."""
    source_rows, inferred_header, invalid_span = _source_rows(block)
    if invalid_span or not source_rows:
        return None

    row_count = len(source_rows)
    occupancy: dict[tuple[int, int], tuple[tuple[int, int], dict[str, Any]]] = {}
    anchors: dict[tuple[int, int], dict[str, Any]] = {}
    column_count = 0
    for row_index, row in enumerate(source_rows):
        column_index = 0
        for raw_cell in row:
            while (row_index, column_index) in occupancy:
                column_index += 1
            row_span = int(raw_cell.get("row_span", 1))
            column_span = int(raw_cell.get("column_span", 1))
            if row_span < 1 or column_span < 1:
                return None
            if row_index + row_span > 1_000 or column_index + column_span > 1_000:
                return None
            next_row_count = max(row_count, row_index + row_span)
            next_column_count = max(column_count, column_index + column_span)
            if next_row_count * next_column_count > 20_000:
                return None
            anchor = (row_index, column_index)
            cell_id = build_stable_id("cell", table_id, block_id, str(row_index), str(column_index))
            data = {**raw_cell, "cell_id": cell_id, "row_span": row_span, "column_span": column_span}
            anchors[anchor] = data
            for covered_row in range(row_index, row_index + row_span):
                for covered_column in range(column_index, column_index + column_span):
                    coordinate = (covered_row, covered_column)
                    if coordinate in occupancy:
                        return None
                    occupancy[coordinate] = (anchor, data)
            row_count = max(row_count, row_index + row_span)
            column_index += column_span
            column_count = max(column_count, column_index)
            if row_count * column_count > 20_000:
                return None

    if row_count < 1 or column_count < 1 or row_count * column_count > 20_000:
        return None

    cells: list[TableCell] = []
    column_headers: dict[int, list[tuple[int, str]]] = {column: [] for column in range(column_count)}
    for row_index in range(row_count):
        for column_index in range(column_count):
            coordinate = (row_index, column_index)
            owner = occupancy.get(coordinate)
            locator = _cell_locator(source_locator, row_index, column_index)
            if owner is None:
                cells.append(TableCell(
                    cell_id=build_stable_id("cell", table_id, block_id, "missing", str(row_index), str(column_index)),
                    row_index=row_index,
                    column_index=column_index,
                    status="missing",
                    source_locator=locator,
                    bbox_unknown_reason="Marker did not provide cell-level geometry",
                ))
                continue
            owner_coordinate, data = owner
            owner_row, owner_column = owner_coordinate
            if coordinate != owner_coordinate:
                cells.append(TableCell(
                    cell_id=build_stable_id("cell", table_id, block_id, "covered", str(row_index), str(column_index)),
                    row_index=row_index,
                    column_index=column_index,
                    status="covered",
                    covered_by_cell_id=data["cell_id"],
                    source_locator=locator,
                    bbox_unknown_reason="Marker did not provide cell-level geometry",
                ))
                continue
            raw_text = str(data.get("text") or "")
            parsed_math = marker_math_text(raw_text, source=MarkerTextSource.PLAIN)
            text = parsed_math.text
            source_math = data.get("math_spans") or parsed_math.spans
            inline_math = tuple(InlineMathSpan(
                start_offset=span.start_offset,
                end_offset=span.end_offset,
                offset_text_version="normalized",
                raw_latex=span.raw_latex,
                display=span.display,
                source_locator=f"{locator}&math={index}",
            ) for index, span in enumerate(source_math))
            is_header = bool(data.get("header"))
            scope = str(data.get("scope") or "")
            role = "row" if scope in {"row", "rowgroup"} else "column" if is_header else "none"
            cells.append(TableCell(
                cell_id=data["cell_id"],
                row_index=row_index,
                column_index=column_index,
                row_span=data["row_span"],
                column_span=data["column_span"],
                status=(
                    "missing" if data.get("missing")
                    else "unreadable" if data.get("unreadable_reason")
                    else "content" if text.strip() or inline_math
                    else "blank"
                ),
                header_role=role,
                text="" if data.get("missing") or data.get("unreadable_reason") else text,
                raw_text=None if data.get("missing") or data.get("unreadable_reason") else raw_text,
                inline_math=() if data.get("missing") or data.get("unreadable_reason") else inline_math,
                unreadable_reason=data.get("unreadable_reason"),
                source_locator=locator,
                bbox_unknown_reason="Marker did not provide cell-level geometry",
            ))
            if role == "column":
                for column in range(column_index, column_index + data["column_span"]):
                    column_headers[column].append((row_index, data["cell_id"]))

    has_header = bool(inferred_header) and all(column_headers.values())
    header_paths = tuple(
        TableHeaderPath(
            column_index=column,
            cell_ids=tuple(cell_id for _, cell_id in sorted(column_headers[column])),
        )
        for column in range(column_count)
    ) if has_header else ()
    return TableGrid(
        table_id=table_id,
        block_id=block_id,
        row_count=row_count,
        column_count=column_count,
        has_header=has_header,
        source_locator=source_locator,
        cells=tuple(cells),
        header_paths=header_paths,
    )


def _source_rows(block: dict[str, Any]) -> tuple[list[list[dict[str, Any]]], bool, bool]:
    candidates = [block]
    children = block.get("children") or block.get("blocks") or []
    if isinstance(children, list):
        candidates.extend(child for child in children if isinstance(child, dict))
    for candidate in candidates:
        rows = candidate.get("table_rows") or candidate.get("rows")
        if isinstance(rows, list) and rows:
            if all(isinstance(row, dict) for row in rows):
                columns = list(dict.fromkeys(str(key) for row in rows for key in row))
                result = [[{"text": key, "header": True} for key in columns]]
                for row in rows:
                    result.append([
                        {
                            "text": str(row[key]) if key in row and row[key] is not None else "",
                            "missing": key not in row or row[key] is None,
                        }
                        for key in columns
                    ])
                return result, True, False
            if all(isinstance(row, list) for row in rows):
                return [[{"text": _cell_text(cell), "missing": cell is None} for cell in row] for row in rows], False, False
        raw_html = next((candidate.get(key) for key in ("html", "table_html", "table_body", "content") if isinstance(candidate.get(key), str) and "<table" in candidate[key].casefold()), None)
        if raw_html:
            parser = _CellHTMLParser()
            parser.feed(raw_html)
            invalid = (
                parser.invalid_span
                or parser.invalid_structure
                or parser.table_depth != 0
                or parser.row is not None
                or parser.cell is not None
                or parser.suppressed_depth != 0
            )
            return parser.rows, any(cell["header"] for row in parser.rows for cell in row), invalid
    return [], False, False


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("text", value.get("content", ""))
    return str(value)


def _positive_span(value: str | None) -> int | None:
    if value is None:
        return 1
    if not re.fullmatch(r"[1-9][0-9]*", value.strip()):
        return None
    parsed = int(value)
    return parsed if parsed <= 1_000 else None


def _cell_locator(locator: str, row: int, column: int) -> str:
    separator = "&" if "#" in locator else "#"
    return f"{locator}{separator}table_cell={row},{column}"


__all__ = ["build_marker_table_grid"]
