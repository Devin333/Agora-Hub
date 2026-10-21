"""Read-only official-scorer serialization of the persisted document projection.

No backend artifact, gold annotation, geometric inference, or recognition repair
is accepted here. Missing content stays missing and is recorded in the page ledger.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape
from hashlib import sha256
import json
from pathlib import Path, PureWindowsPath
from typing import Mapping

from backend.research.domain.document_fidelity import DocumentBlock, InlineMathSpan, TableGrid
from backend.research.rag.evaluation.document_parsing_export import (
    ExportStatus, ResearchDocumentParsingExport,
)

PRODUCTION_RENDER_VERSION = "research-document-scorer-markdown/v1"
_WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    prefix + number for prefix in ("COM", "LPT") for number in "123456789¹²³"
}


@dataclass(frozen=True, slots=True)
class RenderingIssue:
    code: str
    content_id: str | None = None
    source_locator: str | None = None


@dataclass(frozen=True, slots=True)
class RenderedParsingPage:
    page_number: int
    status: ExportStatus
    markdown: str
    projected_block_ids: tuple[str, ...]
    issues: tuple[RenderingIssue, ...]


def _unescaped(text: str, offset: int) -> bool:
    backslashes = 0
    offset -= 1
    while offset >= 0 and text[offset] == "\\":
        backslashes += 1
        offset -= 1
    return backslashes % 2 == 0


def _delimit_math(text: str, spans: tuple[InlineMathSpan, ...]) -> str:
    """Add syntax only, retaining the adopted text rather than restoring raw math."""
    pieces: list[str] = []
    cursor = 0
    for span in sorted(spans, key=lambda item: item.start_offset):
        start, end = span.start_offset, span.end_offset
        delimiters = (("$$", "$$"), ("$", "$"), (r"\(", r"\)"), (r"\[", r"\]"))
        enclosed = any(
            text[max(0, start - len(left)):start] == left and text.startswith(right, end)
            and _unescaped(text, start - len(left)) and _unescaped(text, end)
            for left, right in delimiters
        )
        pieces.append(text[cursor:start])
        adopted = text[start:end]
        stripped = adopted.strip()
        enclosed = enclosed or any(
            stripped.startswith(left) and stripped.endswith(right)
            and len(stripped) > len(left) + len(right)
            and _unescaped(stripped, len(stripped) - len(right))
            for left, right in delimiters
        )
        delimiter = "$$" if span.display else "$"
        pieces.append(adopted if enclosed else delimiter + adopted + delimiter)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _render_block_text(block: DocumentBlock) -> str:
    text = _delimit_math(block.text, block.inline_math)
    if block.kind == "equation" and text.strip() and not block.inline_math:
        stripped = text.strip()
        if not ((stripped.startswith("$$") and stripped.endswith("$$")) or
                (stripped.startswith(r"\[") and stripped.endswith(r"\]"))):
            text = "$$\n" + text + "\n$$"
    return text


def _render_grid(grid: TableGrid) -> tuple[str, tuple[RenderingIssue, ...]]:
    cells = {(cell.row_index, cell.column_index): cell for cell in grid.cells}
    rows: list[str] = []
    issues: list[RenderingIssue] = []
    for row in range(grid.row_count):
        columns: list[str] = []
        for column in range(grid.column_count):
            cell = cells[row, column]
            if cell.status == "covered":
                continue
            if cell.status in {"missing", "unreadable"}:
                issues.append(RenderingIssue("table_cell_" + cell.status, cell.cell_id, cell.source_locator))
            # Only adopted, canonical cell text is scoreable. raw_text and visual
            # descriptions must not be used to fill holes in this projection.
            value = _delimit_math(cell.text, cell.inline_math) if cell.status == "content" else ""
            tag = "th" if cell.header_role != "none" else "td"
            attributes = ""
            if cell.row_span != 1:
                attributes += f' rowspan="{cell.row_span}"'
            if cell.column_span != 1:
                attributes += f' colspan="{cell.column_span}"'
            columns.append(f"<{tag}{attributes}>" + escape(value, quote=False) + f"</{tag}>")
        rows.append("<tr>" + "".join(columns) + "</tr>")
    return "<table>" + "".join(rows) + "</table>", tuple(issues)


def render_production_pages(report: ResearchDocumentParsingExport) -> tuple[RenderedParsingPage, ...]:
    """Serialize only canonical owners in the document's adopted reading order.

    Legacy category arrays do not establish mixed-content order and therefore
    cannot supply formal page predictions. An empty prediction is still emitted
    for each requested page, alongside its explicit non-success status.
    """
    if not isinstance(report, ResearchDocumentParsingExport):
        raise TypeError("report must be a ResearchDocumentParsingExport")
    # Revalidate model_copy/construct inputs; do not trust a forged typed wrapper.
    report = ResearchDocumentParsingExport.model_validate_json(report.model_dump_json())
    if report.page_count is None:
        raise ValueError("physical source page count is required")
    expected = tuple(range(1, report.page_count + 1))
    if tuple(page.page for page in report.pages) != expected:
        raise ValueError("export must account for every physical source page exactly once")
    if report.fidelity is None:
        return tuple(RenderedParsingPage(
            page_number=page.page,
            status=ExportStatus.MISSING_OUTPUT if page.status is ExportStatus.MISSING_OUTPUT else ExportStatus.UNAVAILABLE,
            markdown="", projected_block_ids=(),
            issues=(RenderingIssue(page.reason or "canonical_fidelity_unavailable"),),
        ) for page in report.pages)
    fidelity = report.fidelity
    if (fidelity.source_hash != report.source_hash or len(fidelity.pages) != report.page_count
            or tuple(page.pdf_page_number for page in fidelity.pages) != expected):
        raise ValueError("fidelity source or physical pages do not match the export")
    by_id = {block.block_id: block for block in fidelity.blocks}
    grids = {grid.block_id: grid for grid in fidelity.tables}
    by_page: dict[int, list[DocumentBlock]] = {number: [] for number in expected}
    for block in fidelity.blocks:
        by_page[block.page_number].append(block)
    result: list[RenderedParsingPage] = []
    for page in report.pages:
        ordered = sorted(by_page[page.page], key=lambda item: item.reading_order)
        if page.content_ids != tuple(block.block_id for block in ordered):
            raise ValueError("page content IDs must match the adopted fidelity order")
        pieces: list[str] = []
        projected: list[str] = []
        issues: list[RenderingIssue] = []
        for block in ordered:
            owner = by_id[block.content_owner_id]
            if owner.page_number != block.page_number:
                raise ValueError("content ownership cannot cross physical pages")
            if block.content_owner_id != block.block_id:
                continue
            if block.kind == "table":
                text, cell_issues = _render_grid(grids[block.block_id])
                issues.extend(cell_issues)
            else:
                text = _render_block_text(block)
            if text.strip():
                pieces.append(text)
                projected.append(block.block_id)
            elif block.kind not in {"figure", "other"}:
                issues.append(RenderingIssue("empty_owned_block", block.block_id, block.source_locator))
            if block.order_status == "uncertain":
                issues.append(RenderingIssue("reading_order_uncertain", block.block_id, block.source_locator))
        status = ExportStatus.SUPPORTED
        if not pieces:
            status = ExportStatus.MISSING_OUTPUT
            issues.append(RenderingIssue("page_has_no_scoreable_content"))
        elif issues or page.status is not ExportStatus.SUPPORTED:
            status = ExportStatus.PARTIAL
        result.append(RenderedParsingPage(
            page_number=page.page, status=status, markdown="\n\n".join(pieces),
            projected_block_ids=tuple(projected), issues=tuple(issues),
        ))
    return tuple(result)


def write_production_predictions(
    pages: tuple[RenderedParsingPage, ...], *, page_filenames: Mapping[int, str], output_path: str | Path,
) -> Path:
    """Publish a new prediction directory and a complete rendering-only ledger."""
    if not pages or any(not isinstance(page, RenderedParsingPage) for page in pages):
        raise ValueError("pages must be a non-empty tuple of RenderedParsingPage records")
    numbers = tuple(page.page_number for page in pages)
    if numbers != tuple(range(1, len(pages) + 1)):
        raise ValueError("pages must cover contiguous one-based physical pages")
    if any(type(key) is not int for key in page_filenames) or set(page_filenames) != set(numbers):
        raise ValueError("explicit filename mapping must cover every requested physical page")
    names: set[str] = set()
    for name in page_filenames.values():
        if (not isinstance(name, str) or not name or len(name) > 240
                or name in {".", ".."} or '/' in name or '\\' in name
                or any(char in name for char in ':*?"<>|\x00\r\n')
                or PureWindowsPath(name).is_absolute() or not name.endswith(".md")
                or name.partition(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES):
            raise ValueError("prediction names must be safe Markdown basenames")
        if name.casefold() in names:
            raise ValueError("prediction filenames must be unique on all supported filesystems")
        names.add(name.casefold())
    output = Path(output_path)
    # Exclusive creation avoids overwriting history, following symlinks, or
    # inadvertently mixing predictions from different parser snapshots.
    output.mkdir(parents=True, exist_ok=False)
    identities: dict[int, tuple[str, int]] = {}
    for page in pages:
        payload = (page.markdown + ("\n" if page.markdown else "")).encode("utf-8")
        with (output / page_filenames[page.page_number]).open("xb") as stream:
            stream.write(payload)
        identities[page.page_number] = (sha256(payload).hexdigest(), len(payload))
    ledger = {
        "schema_version": PRODUCTION_RENDER_VERSION,
        "track": "production_document",
        "interpretation": "serialization evidence only; not an execution receipt or quality decision",
        "requested_pages": len(pages), "terminal_pages": len(pages),
        "pages": [{
            "page_number": page.page_number, "status": page.status.value,
            "filename": page_filenames[page.page_number],
            "markdown_sha256": identities[page.page_number][0],
            "markdown_bytes": identities[page.page_number][1],
            "projected_block_ids": list(page.projected_block_ids),
            "issues": [asdict(issue) for issue in page.issues],
        } for page in pages],
    }
    with (output / "rendering-ledger.json").open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(ledger, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return output


__all__ = ["PRODUCTION_RENDER_VERSION", "RenderedParsingPage", "RenderingIssue",
           "render_production_pages", "write_production_predictions"]
