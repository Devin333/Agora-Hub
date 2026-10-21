from __future__ import annotations

import re
from typing import Any

from backend.research.domain.document import ResearchDocument, ResearchEquation, ResearchFigure, ResearchTable
from backend.research.domain.document_fidelity import DocumentBlock
from backend.research.document.citation_spans import build_paragraph_span_metadata
from backend.research.document.models import ChunkType, PaperChunk, ParseSource, SectionRole
from backend.research.document.section_detector import classify_section_role, is_abstract_section
from backend.research.document.special_element_scanner import ScannedElements, scan_special_elements
from framework.rag.core import build_chunk_semantic_key, build_rag_stable_id, content_fingerprint, normalize_semantic_text

# Roles that support proposition decomposition (PRD §5)
_PROPOSITION_ROLES: frozenset[str] = frozenset(["related_work", "experiment", "conclusion"])

_PARAGRAPH_SEP = re.compile(r"\n\n+")
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")
_LATEX_FORMULA = re.compile(r"\\begin\{(?:equation|align|gather)[^}]*\}.*?\\end\{[^}]+\}", re.DOTALL)
_INLINE_FORMULA = re.compile(r"\$\$[^$]+\$\$|\$[^$\n]+\$")
_LATEX_TAG = re.compile(r"\\tag\{([^}]+)\}")
_LATEX_COMMAND = re.compile(r"\\[A-Za-z]+")
_FORMULA_SYMBOL = re.compile(r"[A-Za-z](?:_[A-Za-z0-9]+|\^[A-Za-z0-9]+)?|[A-Za-z]")
_FORMULA_OPERATOR = re.compile(r"\\(?:operatorname|mathrm|text)\{([^}]+)\}|\\([A-Za-z]+)|([+\-*/=<>≤≥≈∑∏∫])")
_FIGURE_REF = re.compile(r"图\s*(\w+)|[Ff]ig(?:ure)?[.s]?\s*(\w+)")
_LOCATOR_PAGE_RE = re.compile(r"(?:#|&)page=(\d+)")
_EQUATION_BODY_REF = re.compile(
    r"\b((?:Equation|Eq)(?:\.|\b)\s*[\(\[]?\s*([A-Za-z0-9][A-Za-z0-9.\-_:]*)\s*[\)\]]?)",
    re.IGNORECASE,
)
_CHINESE_FORMULA_BODY_REF = re.compile(
    r"((?:\u516c\u5f0f)\s*[\(（]?\s*([A-Za-z0-9][A-Za-z0-9.\-_:]*)\s*[\)）]?)"
)
_LATEX_EQUATION_REF = re.compile(r"(\\(?:eq)?ref\{([^}]+)\})")
_FIGURE_BODY_REF = re.compile(r"\b(Fig(?:ure)?\.?\s*([A-Za-z0-9][A-Za-z0-9.\-]*))", re.IGNORECASE)
_TABLE_BODY_REF = re.compile(r"\b(Table\s+([A-Za-z0-9][A-Za-z0-9.\-]*))", re.IGNORECASE)

# Minimum sections to count as "structure detected" (PRD §3)
_MIN_STRUCTURED_SECTIONS = 3
_TABLE_PARENT_ROW_LIMIT = 20
_TABLE_ROW_GROUP_SIZE = 20


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in _PARAGRAPH_SEP.split(text) if p.strip()]


def _trailing_sentences(text: str, n: int = 1) -> str:
    sentences = _SENTENCE_END.split(text.strip())
    return " ".join(sentences[-n:]) if sentences else ""


def _extract_formula_latex(text: str) -> str:
    m = _LATEX_FORMULA.search(text) or _INLINE_FORMULA.search(text)
    return m.group(0).strip() if m else ""


def _find_figure_ref(text: str) -> tuple[bool, str]:
    m = _FIGURE_REF.search(text)
    if not m:
        return False, ""
    return True, (m.group(1) or m.group(2) or "").strip()


def _normalize_ref_label(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "", str(value).casefold())
    text = re.sub(r"^(?:figures?|fig|tables?|tab)", "", text)
    if text.isdigit():
        return str(int(text))
    return text


def _compact_formula_label(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    text = re.sub(r"[^a-z0-9]+", "", raw.casefold())
    if text.isdigit():
        return str(int(text))
    return text


def _normalize_formula_label(value: Any) -> str:
    text = _compact_formula_label(value)
    text = re.sub(r"^(?:equations?|eq|formula)", "", text)
    if text.isdigit():
        return str(int(text))
    return text


def _formula_label_aliases(value: Any) -> set[str]:
    return {
        alias for alias in (
            _compact_formula_label(value),
            _normalize_formula_label(value),
        )
        if alias
    }


def _latex_equation_tag(latex: str) -> str:
    match = _LATEX_TAG.search(latex)
    return match.group(1).strip() if match else ""


def _formula_reference_keys(eq: ResearchEquation) -> set[str]:
    values = [
        eq.equation_id,
        eq.metadata.get("equation_id"),
        eq.metadata.get("equation_number"),
        eq.metadata.get("equation_label"),
        eq.metadata.get("label"),
        _latex_equation_tag(eq.latex),
    ]
    keys: set[str] = set()
    for value in values:
        keys.update(_formula_label_aliases(value))
    return keys


def _body_formula_references(text: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    patterns = (
        _EQUATION_BODY_REF,
        _CHINESE_FORMULA_BODY_REF,
        _LATEX_EQUATION_REF,
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            label = _normalize_formula_label(match.group(2))
            text_ref = match.group(1).strip()
            if not label or not text_ref:
                continue
            key = (label, text_ref)
            if key in seen:
                continue
            seen.add(key)
            refs.append({
                "kind": "formula",
                "label": label,
                "text_ref": text_ref,
            })
    return refs


def _formula_lookup(
    equations: dict[str, ResearchEquation],
) -> dict[str, ResearchEquation]:
    lookup: dict[str, ResearchEquation] = {}
    for eq in equations.values():
        for key in _formula_reference_keys(eq):
            lookup.setdefault(key, eq)
    return lookup


def _matched_formula_references(
    text: str,
    formula_lookup: dict[str, ResearchEquation],
) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ref in _body_formula_references(text):
        eq = formula_lookup.get(ref["label"])
        if eq is None:
            continue
        key = (eq.equation_id, ref["text_ref"])
        if key in seen:
            continue
        seen.add(key)
        refs.append({
            **ref,
            "equation_id": eq.equation_id,
        })
    return refs


def _body_visual_references(text: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for kind, pattern in (("figure", _FIGURE_BODY_REF), ("table", _TABLE_BODY_REF)):
        for match in pattern.finditer(text):
            label = _normalize_ref_label(match.group(2))
            if not label:
                continue
            refs.append({
                "kind": kind,
                "label": label,
                "text_ref": match.group(1).strip(),
            })
    return refs


def _caption_ref_label(kind: str, text: str) -> str:
    pattern = _FIGURE_BODY_REF if kind == "figure" else _TABLE_BODY_REF
    match = pattern.search(text)
    return _normalize_ref_label(match.group(2)) if match else ""


def _visual_reference_keys(
    kind: str,
    element_id: str,
    caption: str,
    metadata: dict[str, Any],
) -> set[str]:
    return {
        key for key in (
            _normalize_ref_label(element_id),
            _caption_ref_label(kind, caption),
            _caption_ref_label(kind, _metadata_text(metadata, "caption_text", "surya_caption")),
            _normalize_ref_label(metadata.get(f"{kind}_number", "")),
            _normalize_ref_label(metadata.get("caption_number", "")),
        )
        if key
    }


def _visual_lookup(
    kind: str,
    elements: dict[str, ResearchFigure] | dict[str, ResearchTable],
) -> dict[str, ResearchFigure | ResearchTable]:
    lookup: dict[str, ResearchFigure | ResearchTable] = {}
    for element_id, element in elements.items():
        for key in _visual_reference_keys(kind, element_id, element.caption, element.metadata):
            lookup.setdefault(key, element)
    return lookup


def _table_rows_to_lines(rows: list[dict[str, Any]], columns: list[str]) -> list[str]:
    lines: list[str] = []
    for row in rows:
        if columns:
            values = ["" if row.get(column) is None else str(row.get(column, "")) for column in columns]
        else:
            values = ["" if value is None else str(value) for value in row.values()]
        lines.append(" | ".join(values))
    return lines


def _element_source_locator(source_ref: str, metadata: dict[str, Any]) -> str:
    return str(metadata.get("source_locator") or source_ref)


def _content_sources(*items: tuple[str, Any]) -> list[str]:
    return [name for name, value in items if bool(value)]


def _metadata_text(metadata: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _region_metadata(
    *,
    page: int | None,
    source_locator: str,
    rect: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if page is not None:
        out["page"] = page
    if source_locator:
        out["source_locator"] = source_locator
    if rect:
        out["pdf_rect"] = rect
    return out


def _caption_alignment_metadata(
    *,
    caption: str,
    page: int | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    caption_text = _metadata_text(metadata, "caption_text", "surya_caption") or caption
    caption_locator = str(metadata.get("caption_source_locator") or "")
    caption_page = (
        _coerce_page(metadata.get("caption_text_page"))
        or _locator_page(caption_locator)
        or page
    )
    out: dict[str, Any] = {
        "caption_text": caption_text,
        "caption_region": _region_metadata(
            page=caption_page,
            source_locator=caption_locator,
            rect=metadata.get("caption_pdf_rect"),
        ),
        "caption_match_strategy": str(
            metadata.get("alignment_strategy")
            or metadata.get("caption_match_strategy")
            or "unknown"
        ),
    }
    score = metadata.get("alignment_score", metadata.get("caption_match_score"))
    if score is not None:
        out["caption_match_confidence"] = score
    return out


def _visual_alignment_metadata(
    *,
    kind: str,
    element_id: str,
    caption: str,
    page: int | None,
    source_ref: str,
    metadata: dict[str, Any],
    parent: PaperChunk | None,
    parent_strategy: str,
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    source_locator = _element_source_locator(source_ref, metadata)
    visual_region = _region_metadata(
        page=page or _metadata_page(metadata),
        source_locator=source_locator,
        rect=metadata.get("pdf_rect"),
    )
    base: dict[str, Any] = {
        "visual_element_kind": kind,
        "visual_element_id": element_id,
        "visual_region": visual_region,
        "caption_alignment": _caption_alignment_metadata(
            caption=caption,
            page=page,
            metadata=metadata,
        ),
        "nearby_context_chunk_id": parent.chunk_id if parent else "",
        "nearby_context_match_strategy": parent_strategy,
        "referenced_by_chunks": references,
    }
    ref_labels = sorted(_visual_reference_keys(kind, element_id, caption, metadata))
    if ref_labels:
        base["reference_labels"] = ref_labels
    return base


def _table_to_text(
    tbl: ResearchTable,
    *,
    parent: PaperChunk | None = None,
    rows: list[dict[str, Any]] | None = None,
    row_start: int | None = None,
    row_end: int | None = None,
) -> tuple[str, list[str]]:
    selected_rows = rows if rows is not None else tbl.rows[:_TABLE_PARENT_ROW_LIMIT]
    lines = [f"[Table {tbl.table_id}]", "Caption:", tbl.caption]
    if tbl.columns:
        lines.extend(["", "Columns:", " | ".join(tbl.columns)])
    if selected_rows:
        lines.extend(["", "Rows:"])
        if row_start is not None and row_end is not None:
            lines.append(f"row_range={row_start}-{row_end}")
        lines.extend(_table_rows_to_lines(selected_rows, tbl.columns))
    if parent is not None:
        lines.extend(["", "Nearby Context:", _context_excerpt(parent.content)])
        if parent.section_title:
            lines.extend(["", f"Section: {parent.section_title}"])
    source_locator = _element_source_locator(tbl.source_ref, tbl.metadata)
    if source_locator:
        lines.extend(["", f"Source: {source_locator}"])
    sources = _content_sources(
        ("caption", tbl.caption),
        ("columns", tbl.columns),
        ("rows", selected_rows),
        ("nearby_context", parent),
        ("source_locator", source_locator),
    )
    return "\n".join(lines), sources


_FIDELITY_CHUNK_MAP_LIMIT = 128
_FIDELITY_CHUNK_MATH_MAP_LIMIT = 128


def _paragraph_text_ranges(text: str) -> list[tuple[str, int, int]]:
    ranges: list[tuple[str, int, int]] = []
    range_start = 0
    for separator in _PARAGRAPH_SEP.finditer(text):
        range_end = separator.start()
        stripped_start, stripped_end = range_start, range_end
        while stripped_start < stripped_end and text[stripped_start].isspace():
            stripped_start += 1
        while stripped_end > stripped_start and text[stripped_end - 1].isspace():
            stripped_end -= 1
        if stripped_start < stripped_end:
            ranges.append((text[stripped_start:stripped_end], stripped_start, stripped_end))
        range_start = separator.end()
    stripped_start, stripped_end = range_start, len(text)
    while stripped_start < stripped_end and text[stripped_start].isspace():
        stripped_start += 1
    while stripped_end > stripped_start and text[stripped_end - 1].isspace():
        stripped_end -= 1
    if stripped_start < stripped_end:
        ranges.append((text[stripped_start:stripped_end], stripped_start, stripped_end))
    return ranges


def _paragraph_source_range(
    blocks: list[DocumentBlock],
    *,
    section_text: str,
    paragraph_text: str,
    paragraph_index: int,
) -> tuple[DocumentBlock, int, int] | None:
    section_paragraphs = _split_paragraphs(section_text)
    if not (0 <= paragraph_index < len(section_paragraphs)):
        return None
    if section_paragraphs[paragraph_index] != paragraph_text:
        return None

    candidates: list[tuple[str, DocumentBlock, int, int]] = []
    for block in blocks:
        # Marker section projections include the section heading in provenance,
        # but the heading text itself is not copied into ResearchSection.text.
        if block.kind == "heading":
            continue
        candidates.extend(
            (text, block, start_offset, end_offset)
            for text, start_offset, end_offset in _paragraph_text_ranges(block.text)
        )

    candidate_index = 0
    aligned: list[tuple[DocumentBlock, int, int]] = []
    for paragraph in section_paragraphs:
        match: tuple[DocumentBlock, int, int] | None = None
        while candidate_index < len(candidates):
            candidate_text, block, start_offset, end_offset = candidates[candidate_index]
            candidate_index += 1
            if candidate_text == paragraph:
                match = (block, start_offset, end_offset)
                break
        if match is None:
            return None
        aligned.append(match)
    return aligned[paragraph_index]


def _raw_math_span_uses_block_text(
    block: DocumentBlock,
    span: Any,
    *,
    raw_text: str | None,
) -> bool:
    if span.offset_text_version != "raw":
        return True
    return raw_text is not None and raw_text == block.text


def _paragraph_mapping_scope(
    excluded_chunk_content: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "mapping_scope": "paragraph_source_text",
        "chunk_content_coverage": "partial" if excluded_chunk_content else "complete",
        "excluded_chunk_content": list(excluded_chunk_content),
    }


def _fidelity_chunk_source_map(
    document: ResearchDocument,
    *,
    metadata: dict[str, Any],
    element_ref: str | None = None,
    section_text: str | None = None,
    paragraph_text: str | None = None,
    paragraph_index: int | None = None,
    excluded_chunk_content: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    fidelity = document.fidelity
    if fidelity is None:
        return None
    if fidelity.source_hash != document.source_hash:
        return {
            "schema_version": fidelity.schema_version,
            "source_hash": fidelity.source_hash,
            "mapping_status": "quarantined",
            "reason": "fidelity_source_hash_mismatch",
            "expected_block_count": 0,
            "matched_block_count": 0,
            "missing_block_count": 0,
            "omitted_block_count": 0,
            "blocks": [],
        }
    provenance = metadata.get("marker_provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    raw_ids: list[str] = []
    raw_text_by_block_id: dict[str, str] = {}
    if isinstance(provenance.get("blocks"), list):
        for item in provenance["blocks"]:
            if not isinstance(item, dict) or not item.get("block_id"):
                continue
            block_id = str(item["block_id"])
            raw_ids.append(block_id)
            if isinstance(item.get("raw_text"), str):
                raw_text_by_block_id[block_id] = item["raw_text"]
    if provenance.get("block_id"):
        block_id = str(provenance["block_id"])
        raw_ids.append(block_id)
        if isinstance(provenance.get("raw_text"), str):
            raw_text_by_block_id[block_id] = provenance["raw_text"]
    if isinstance(provenance.get("caption_block_ids"), list):
        raw_ids.extend(str(item) for item in provenance["caption_block_ids"] if item)
    requested_ids = list(dict.fromkeys(raw_ids))
    requested = set(requested_ids)
    paragraph_mapping_requested = (
        section_text is not None or paragraph_text is not None or paragraph_index is not None
    )
    mapped_text_ranges: dict[str, list[tuple[int, int]]] = {}
    if requested:
        selected = [block for block in fidelity.blocks if block.block_id in requested]
        missing_count = len(requested.difference(block.block_id for block in selected))
        expected_count = len(requested)
    elif element_ref:
        selected = [block for block in fidelity.blocks if block.element_ref == element_ref]
        missing_count = 0 if selected else 1
        expected_count = 1
    else:
        return {
            "schema_version": fidelity.schema_version,
            "normalization_version": fidelity.normalization_version,
            "source_hash": fidelity.source_hash,
            "mapping_status": "unavailable",
            **({"reason": "paragraph_source_range_unavailable"} if paragraph_mapping_requested else {}),
            **(_paragraph_mapping_scope(excluded_chunk_content) if paragraph_mapping_requested else {}),
            "expected_block_count": 1 if paragraph_mapping_requested else None,
            "matched_block_count": 0,
            "missing_block_count": 1 if paragraph_mapping_requested else None,
            "omitted_block_count": 0,
            "blocks": [],
        }
    if paragraph_mapping_requested:
        if section_text is None or paragraph_text is None or paragraph_index is None:
            paragraph_source = None
        else:
            selected_by_id = {block.block_id: block for block in selected}
            provenance_ordered_blocks = [
                selected_by_id[block_id]
                for block_id in requested_ids
                if block_id in selected_by_id
            ]
            paragraph_source = _paragraph_source_range(
                provenance_ordered_blocks,
                section_text=section_text,
                paragraph_text=paragraph_text,
                paragraph_index=paragraph_index,
            )
        if paragraph_source is None:
            return {
                "schema_version": fidelity.schema_version,
                "normalization_version": fidelity.normalization_version,
                "source_hash": fidelity.source_hash,
                "mapping_status": "unavailable",
                "reason": "paragraph_source_range_unavailable",
                **_paragraph_mapping_scope(excluded_chunk_content),
                "expected_block_count": 1,
                "matched_block_count": 0,
                "missing_block_count": 1,
                "omitted_block_count": 0,
                "blocks": [],
            }
        block, start_offset, end_offset = paragraph_source
        selected = [block]
        expected_count = 1
        missing_count = 0
        mapped_text_ranges[block.block_id] = [(start_offset, end_offset)]
    selected.sort(key=lambda block: (block.page_number, block.reading_order, block.source_order, block.block_id))
    visible = selected[:_FIDELITY_CHUNK_MAP_LIMIT]
    entries: list[dict[str, Any]] = []
    math_budget = _FIDELITY_CHUNK_MATH_MAP_LIMIT
    omitted_math_count = 0
    for block in visible:
        text_ranges = mapped_text_ranges.get(block.block_id)
        raw_text = raw_text_by_block_id.get(block.block_id)
        raw_spans = tuple(
            span for span in block.inline_math
            if span.offset_text_version == "raw"
        )
        if text_ranges is None:
            relevant_spans = block.inline_math
            unmapped_spans: tuple[Any, ...] = ()
        else:
            comparable_spans = tuple(
                span for span in block.inline_math
                if _raw_math_span_uses_block_text(block, span, raw_text=raw_text)
            )
            unmapped_spans = tuple(
                span for span in block.inline_math
                if not _raw_math_span_uses_block_text(block, span, raw_text=raw_text)
            )
            relevant_spans = tuple(
                span
                for span in comparable_spans
                if any(
                    span.start_offset >= start_offset and span.end_offset <= end_offset
                    for start_offset, end_offset in text_ranges
                )
            )
        spans = relevant_spans[:math_budget]
        math_budget -= len(spans)
        budget_omitted_spans = len(relevant_spans) - len(spans)
        omitted_spans = budget_omitted_spans + len(unmapped_spans)
        omitted_math_count += omitted_spans
        entries.append({
            "block_id": block.block_id,
            "page_number": block.page_number,
            "parent_id": block.parent_id,
            "kind": block.kind,
            "source_order": block.source_order,
            "reading_order": block.reading_order,
            "order_status": block.order_status,
            **({"order_reason": block.order_reason} if block.order_reason else {}),
            "source_locator": block.source_locator,
            **({"element_ref": block.element_ref} if block.element_ref else {}),
            **({"bbox": block.bbox.model_dump(mode="json")} if block.bbox else {}),
            **({"block_text_ranges": [{
                "start_offset": start_offset,
                "end_offset": end_offset,
                "offset_basis": "unicode_codepoint",
                "offset_text_version": "normalized",
            } for start_offset, end_offset in text_ranges]} if text_ranges is not None else {}),
            "inline_math_candidate_span_count": len(block.inline_math),
            **({"raw_math_offset_compatibility": (
                "confirmed" if raw_text == block.text
                else "mismatch" if raw_text is not None
                else "unavailable"
            )} if text_ranges is not None and raw_spans else {}),
            "inline_math_span_count": len(relevant_spans),
            "unmapped_inline_math_span_count": len(unmapped_spans),
            "budget_omitted_inline_math_span_count": budget_omitted_spans,
            "omitted_inline_math_span_count": omitted_spans,
            "inline_math_spans": [{
                "start_offset": span.start_offset,
                "end_offset": span.end_offset,
                "offset_text_version": span.offset_text_version,
                "display": span.display,
                "source_locator": span.source_locator,
            } for span in spans],
        })
    omitted_count = len(selected) - len(visible)
    return {
        "schema_version": fidelity.schema_version,
        "normalization_version": fidelity.normalization_version,
        "source_hash": fidelity.source_hash,
        "mapping_status": (
            "unavailable" if not selected
            else "partial" if (
                missing_count or omitted_count or omitted_math_count or excluded_chunk_content
            )
            else "complete"
        ),
        "expected_block_count": expected_count,
        "matched_block_count": len(selected),
        "missing_block_count": missing_count,
        "omitted_block_count": omitted_count,
        "omitted_inline_math_span_count": omitted_math_count,
        "inline_math_mapping_scope": "included_blocks",
        **(_paragraph_mapping_scope(excluded_chunk_content) if paragraph_mapping_requested else {}),
        "blocks": entries,
    }


def _table_grid_source_map(
    tbl: ResearchTable,
    *,
    body_row_start: int,
    body_row_end: int,
) -> dict[str, Any] | None:
    grid = tbl.grid
    if grid is None:
        return None
    cells_by_row: dict[int, list[Any]] = {}
    for cell in grid.cells:
        cells_by_row.setdefault(cell.row_index, []).append(cell)

    def references(row_indexes: list[int]) -> list[dict[str, Any]]:
        return [
            {
                "cell_id": cell.cell_id,
                "row_index": cell.row_index,
                "column_index": cell.column_index,
                "status": cell.status,
                "source_locator": cell.source_locator,
                **({"covered_by_cell_id": cell.covered_by_cell_id} if cell.covered_by_cell_id else {}),
            }
            for row_index in row_indexes
            for cell in sorted(cells_by_row.get(row_index, []), key=lambda item: item.column_index)
        ]

    # The legacy rows projection treats source row 0 as its columns row. This
    # explicit mapping keeps that transformation traceable without asserting
    # that an unmarked source row is semantically a header.
    projection_header = references([0])
    source_rows = [
        row_index + 1
        for row_index in range(max(0, body_row_start), min(body_row_end + 1, len(tbl.rows)))
        if row_index + 1 < grid.row_count
    ]
    projected = references(source_rows)
    reference_count = len(projection_header) + len(projected)
    result: dict[str, Any] = {
        "schema_version": "research-table-chunk-map.v1",
        "table_id": grid.table_id,
        "source_locator": grid.source_locator,
        "row_count": grid.row_count,
        "column_count": grid.column_count,
        "has_header": grid.has_header,
        "header_paths": [path.model_dump(mode="json") for path in grid.header_paths],
        "projection_header_row": 0,
        "projected_body_row_range": (
            [body_row_start, body_row_end] if body_row_end >= body_row_start else []
        ),
        "source_grid_body_rows": source_rows,
    }
    if reference_count > 2_000:
        result["cell_mapping_status"] = "omitted_size_limit"
        result["cell_mapping_cell_count"] = reference_count
        return result
    result["cell_mapping_status"] = "complete"
    result["projection_header_cells"] = projection_header
    result["projected_body_cells"] = projected
    return result


def _table_semantic_text(tbl: ResearchTable, rows: list[dict[str, Any]]) -> str:
    lines = [tbl.table_id, tbl.caption]
    if tbl.columns:
        lines.append(" | ".join(tbl.columns))
    lines.extend(_table_rows_to_lines(rows, tbl.columns))
    return "\n".join(lines)


def _table_visual_identity(tbl: ResearchTable) -> str:
    metadata = tbl.metadata
    identity_parts = [
        str(metadata.get("source_locator") or ""),
        str(metadata.get("image_ref") or ""),
        str(metadata.get("pdf_rect") or metadata.get("bbox") or metadata.get("rect") or ""),
        str(tbl.page or ""),
        _table_semantic_text(tbl, tbl.rows[:_TABLE_PARENT_ROW_LIMIT]),
    ]
    identity_text = "\n".join(part for part in identity_parts if part)
    if not identity_text:
        identity_text = f"{tbl.table_id}\n{tbl.caption}"
    return _content_hash(identity_text)


def _formula_to_text(eq: ResearchEquation, parent: PaperChunk | None) -> str:
    referenced_texts = _formula_referenced_texts(eq)
    lines = [
        f"[Equation {eq.equation_id}]",
        "LaTeX:",
        eq.latex,
    ]
    normalized = _normalize_formula_text(eq.latex)
    if normalized and normalized != eq.latex:
        lines.extend(["", "Normalized LaTeX:", normalized])
    symbols = _formula_symbols(eq.latex)
    if symbols:
        lines.extend(["", "Symbols:", " ".join(symbols)])
    operators = _formula_operators(eq.latex)
    if operators:
        lines.extend(["", "Operators:", " ".join(operators)])
    if parent is not None:
        lines.extend([
            "",
            "Context:",
            _context_excerpt(parent.content),
        ])
        if parent.section_title:
            lines.extend(["", f"Section: {parent.section_title}"])
    if referenced_texts:
        lines.extend(["", "Referenced By:"])
        lines.extend(referenced_texts)
    source_locator = eq.metadata.get("source_locator")
    if source_locator:
        lines.extend(["", f"Source: {source_locator}"])
    return "\n".join(lines)


def _figure_to_text(fig: ResearchFigure, parent: PaperChunk | None) -> tuple[str, list[str]]:
    caption_region_text = _metadata_text(fig.metadata, "caption_text", "surya_caption")
    ocr_text = _metadata_text(
        fig.metadata,
        "ocr_text",
        "figure_ocr_text",
        "crop_ocr_text",
        "image_ocr_text",
    )
    lines = [f"[Figure {fig.figure_id}]", "Caption:", fig.caption]
    if caption_region_text and _normalize_semantic_text(caption_region_text) != _normalize_semantic_text(fig.caption):
        lines.extend(["", "Caption Region Text:", caption_region_text])
    if parent is not None:
        lines.extend(["", "Nearby Context:", _context_excerpt(parent.content)])
        if parent.section_title:
            lines.extend(["", f"Section: {parent.section_title}"])
    if ocr_text:
        lines.extend(["", "OCR Text:", ocr_text])
    source_locator = _element_source_locator(fig.source_ref, fig.metadata)
    if source_locator:
        lines.extend(["", f"Source: {source_locator}"])
    sources = _content_sources(
        ("caption", fig.caption),
        ("caption_region_text", caption_region_text),
        ("nearby_context", parent),
        ("ocr", ocr_text),
        ("source_locator", source_locator),
    )
    return "\n".join(lines), sources


def _figure_visual_identity(fig: ResearchFigure) -> str:
    metadata = fig.metadata
    identity_parts = [
        str(metadata.get("source_locator") or ""),
        str(fig.image_ref or metadata.get("image_ref") or ""),
        str(metadata.get("pdf_rect") or metadata.get("bbox") or metadata.get("rect") or ""),
        str(fig.page or ""),
        fig.caption,
    ]
    identity_text = "\n".join(part for part in identity_parts if part)
    if not identity_text:
        identity_text = fig.figure_id
    return _content_hash(identity_text)


def _context_excerpt(text: str, *, max_chars: int = 900) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + "..."


def _normalize_formula_text(text: str) -> str:
    stripped = text.strip()
    for prefix, suffix in (("\\[", "\\]"), ("$$", "$$"), ("$", "$")):
        if stripped.startswith(prefix) and stripped.endswith(suffix):
            stripped = stripped[len(prefix):-len(suffix)]
            break
    return re.sub(r"\s+", "", stripped).casefold()


def _formula_symbols(latex: str) -> list[str]:
    normalized = _LATEX_COMMAND.sub(" ", latex)
    return sorted({match.group(0) for match in _FORMULA_SYMBOL.finditer(normalized)})


def _formula_operators(latex: str) -> list[str]:
    operators: set[str] = set()
    for match in _FORMULA_OPERATOR.finditer(latex):
        value = next((group for group in match.groups() if group), "")
        if value:
            operators.add(value)
    return sorted(operators)


def _formula_referenced_texts(eq: ResearchEquation, *, max_items: int = 4) -> list[str]:
    refs = eq.metadata.get("referenced_by_chunks")
    if not isinstance(refs, list):
        return []
    texts: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        text = str(ref.get("text") or ref.get("snippet") or ref.get("context") or "")
        if not text:
            text_ref = str(ref.get("text_ref") or "")
            section_title = str(ref.get("section_title") or "")
            text = " ".join(part for part in (section_title, text_ref) if part)
        if text:
            texts.append(_context_excerpt(text, max_chars=280))
        if len(texts) >= max_items:
            break
    return texts


def _locator_page(locator: Any) -> int | None:
    if not locator:
        return None
    match = _LOCATOR_PAGE_RE.search(str(locator))
    return int(match.group(1)) if match else None


def _coerce_page(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata_page(metadata: dict[str, Any]) -> int | None:
    return (
        _coerce_page(metadata.get("page"))
        or _coerce_page(metadata.get("caption_text_page"))
        or _locator_page(metadata.get("source_locator"))
        or _locator_page(metadata.get("caption_source_locator"))
    )


def _chunk_page(chunk: PaperChunk) -> int | None:
    return _metadata_page(chunk.metadata)


def _paragraph_child_chunks(chunks: list[PaperChunk]) -> list[PaperChunk]:
    return [
        chunk for chunk in chunks
        if chunk.chunk_type == "paragraph" and not chunk.metadata.get("is_parent")
    ]


def _find_formula_parent(eq: ResearchEquation, chunks: list[PaperChunk]) -> tuple[PaperChunk | None, str]:
    paragraph_chunks = _paragraph_child_chunks(chunks)
    if not paragraph_chunks:
        return None, "none"

    eq_latex = _normalize_formula_text(eq.latex)
    for chunk in paragraph_chunks:
        if eq_latex and (
            eq_latex == _normalize_formula_text(chunk.formula_latex)
            or eq_latex in _normalize_formula_text(chunk.content)
        ):
            return chunk, "latex_text"

    eq_page = eq.page or _locator_page(eq.metadata.get("source_locator"))
    if eq_page is not None:
        for chunk in paragraph_chunks:
            if _locator_page(chunk.metadata.get("source_locator")) == eq_page:
                return chunk, "page_locator"

    return paragraph_chunks[0], "first_paragraph_fallback"


def _find_visual_parent(
    *,
    caption: str,
    page: int | None,
    metadata: dict[str, Any],
    chunks: list[PaperChunk],
) -> tuple[PaperChunk | None, str]:
    paragraph_chunks = _paragraph_child_chunks(chunks)
    if not paragraph_chunks:
        return None, "none"

    caption_candidates = [
        caption,
        _metadata_text(metadata, "caption_text", "surya_caption"),
    ]
    normalized_captions = [
        normalized for text in caption_candidates
        if (normalized := _normalize_semantic_text(text))
    ]
    for chunk in paragraph_chunks:
        haystack = _normalize_semantic_text(chunk.content)
        if any(caption_text and caption_text in haystack for caption_text in normalized_captions):
            return chunk, "caption_text"

    element_page = _coerce_page(page) or _metadata_page(metadata)
    if element_page is not None:
        for chunk in paragraph_chunks:
            if _chunk_page(chunk) == element_page:
                return chunk, "page_nearest"

    return paragraph_chunks[0], "first_paragraph_fallback"


def _visual_references_by_element(
    chunks: list[PaperChunk],
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for chunk in _paragraph_child_chunks(chunks):
        refs = chunk.metadata.get("visual_references")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            kind = str(ref.get("kind") or "")
            element_id = str(ref.get("element_id") or "")
            if not kind or not element_id:
                continue
            entry = {
                "chunk_id": chunk.chunk_id,
                "section_title": chunk.section_title,
                "page": _chunk_page(chunk),
                "source_locator": chunk.metadata.get("source_locator", ""),
                "text_ref": str(ref.get("text_ref") or ""),
            }
            out.setdefault((kind, element_id), []).append(entry)
    return out


def _formula_references_by_equation(chunks: list[PaperChunk]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str, str]] = set()
    for chunk in _paragraph_child_chunks(chunks):
        refs = chunk.metadata.get("formula_references")
        if not isinstance(refs, list):
            continue
        for ref in refs:
            if not isinstance(ref, dict):
                continue
            equation_id = str(ref.get("equation_id") or "")
            text_ref = str(ref.get("text_ref") or "")
            if not equation_id or not text_ref:
                continue
            key = (equation_id, chunk.chunk_id, text_ref)
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "chunk_id": chunk.chunk_id,
                "section_title": chunk.section_title,
                "page": _chunk_page(chunk),
                "source_locator": chunk.metadata.get("source_locator", ""),
                "text_ref": text_ref,
                "text": _context_excerpt(chunk.content, max_chars=360),
            }
            out.setdefault(equation_id, []).append(entry)
    return out


def _stable_chunk_id(paper_id: str, *parts: str) -> str:
    return build_rag_stable_id("chunk", paper_id, *parts)


def _dedupe_generated_chunk_id(
    paper_id: str,
    parts: list[str],
    used_ids: dict[str, int],
) -> tuple[str, str]:
    chunk_id = _stable_chunk_id(paper_id, *parts)
    occurrence = used_ids.get(chunk_id, 0) + 1
    used_ids[chunk_id] = occurrence
    if occurrence == 1:
        return chunk_id, ""
    return (
        _stable_chunk_id(paper_id, *parts, "occurrence", str(occurrence)),
        str(occurrence),
    )


def _normalize_semantic_text(text: str) -> str:
    return normalize_semantic_text(text)


def _content_hash(text: str) -> str:
    return content_fingerprint(text)


def _semantic_key(
    *,
    paper_id: str,
    chunk_type: ChunkType,
    section_title: str,
    source_locator: str,
    semantic_text: str,
) -> tuple[str, str, dict[str, str]]:
    semantic = build_chunk_semantic_key(
        document_id=paper_id,
        chunk_type=chunk_type,
        section_title=section_title,
        source_locator=source_locator,
        content=semantic_text,
    )
    return semantic.key, semantic.content_hash, dict(semantic.parts)


def _chunk_metadata(
    *,
    paper_id: str,
    chunk_type: ChunkType,
    section_title: str,
    source_ref: str,
    semantic_text: str,
    source_locator: str | None = None,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(base or {})
    locator = str(source_locator or metadata.get("source_locator") or source_ref)
    semantic_key, content_hash, parts = _semantic_key(
        paper_id=paper_id,
        chunk_type=chunk_type,
        section_title=section_title,
        source_locator=locator,
        semantic_text=semantic_text,
    )
    metadata.update({
        "source_ref": source_ref,
        "source_locator": locator,
        "semantic_key": semantic_key,
        "semantic_key_parts": parts,
        "content_hash": content_hash,
    })
    return metadata


class PaperDocumentChunker:
    """
    Converts a ResearchDocument into a three-level PaperChunk hierarchy:
      abstract  → abstract chunk (+ propositions async)
      section   → parent chunk (chapter level)
      paragraph → child chunk  (retrieval unit, with 1-sentence overlap)

    Special elements (formula triplets, figure+caption, tables) are bound
    into their surrounding paragraph chunks and also emitted as standalone chunks.
    """

    def chunk(self, doc: ResearchDocument, parse_source: ParseSource) -> list[PaperChunk]:
        elements = scan_special_elements(doc)
        non_abstract = [s for s in doc.sections if not is_abstract_section(s.title)]
        structure_detected = len(non_abstract) >= _MIN_STRUCTURED_SECTIONS

        chunks: list[PaperChunk] = []
        if not structure_detected:
            chunks = self._fallback_fixed_token_chunks(doc, parse_source, elements)
            self._add_formula_chunks(doc, parse_source, structure_detected, chunks)
            self._add_figure_chunks(doc, parse_source, structure_detected, chunks)
            self._add_table_chunks(doc, parse_source, structure_detected, chunks)
            return chunks

        self._add_abstract_chunk(doc, parse_source, structure_detected, chunks)
        self._add_section_chunks(doc, parse_source, structure_detected, non_abstract, elements, chunks)
        self._add_formula_chunks(doc, parse_source, structure_detected, chunks)
        self._add_figure_chunks(doc, parse_source, structure_detected, chunks)
        self._add_table_chunks(doc, parse_source, structure_detected, chunks)

        return chunks

    # ── private helpers ──────────────────────────────────────────────────────

    def _add_abstract_chunk(self, doc, parse_source, structure_detected, out):
        abstract = next((s for s in doc.sections if is_abstract_section(s.title)), None)
        if not abstract or not abstract.text.strip():
            return
        out.append(PaperChunk(
            chunk_id=_stable_chunk_id(doc.paper_id, "abstract"),
            paper_id=doc.paper_id,
            parse_source=parse_source,
            structure_detected=structure_detected,
            chunk_type="abstract",
            section_title=abstract.title,
            section_role=["background"],
            section_index=0,
            propositions_generated=False,
            content=abstract.text,
            metadata=_chunk_metadata(
                paper_id=doc.paper_id,
                chunk_type="abstract",
                section_title=abstract.title,
                source_ref=abstract.source_ref,
                source_locator=abstract.metadata.get("source_locator"),
                semantic_text=abstract.text,
                base={
                    "section_id": abstract.section_id,
                    "section_level": abstract.level,
                    "page_start": abstract.page_start,
                    "page_end": abstract.page_end,
                },
            ),
        ))

    def _add_section_chunks(self, doc, parse_source, structure_detected, sections, elements, out):
        figure_lookup = _visual_lookup("figure", elements.figures)
        table_lookup = _visual_lookup("table", elements.tables)
        formula_lookup = _formula_lookup(elements.equations)
        for idx, section in enumerate(sections):
            if not section.text.strip():
                continue  # skip blank sections — no chunk to emit
            snippet = " ".join(section.text.split()[:60])
            roles: list[SectionRole] = classify_section_role(section.title, snippet)
            cross_refs = elements.section_cross_refs.get(section.section_id, [])
            need_propositions = any(r in _PROPOSITION_ROLES for r in roles)

            # section-level parent chunk
            parent_id = _stable_chunk_id(doc.paper_id, "sec", section.section_id)
            out.append(PaperChunk(
                chunk_id=parent_id,
                paper_id=doc.paper_id,
                parse_source=parse_source,
                structure_detected=structure_detected,
                chunk_type="paragraph",
                section_title=section.title,
                section_role=roles,
                section_index=idx,
                references=cross_refs,
                content=section.text,
                metadata=_chunk_metadata(
                    paper_id=doc.paper_id,
                    chunk_type="paragraph",
                    section_title=section.title,
                    source_ref=section.source_ref,
                    source_locator=section.metadata.get("source_locator"),
                    semantic_text=section.text,
                    base={
                        "is_parent": True,
                        "level": section.level,
                        "section_id": section.section_id,
                        "section_level": section.level,
                        "page_start": section.page_start,
                        "page_end": section.page_end,
                        **({"fidelity_source_map": _fidelity_chunk_source_map(
                            doc, metadata=section.metadata,
                        )} if doc.fidelity is not None else {}),
                    },
                ),
            ))

            # paragraph-level child chunks
            paragraphs = _split_paragraphs(section.text)
            for para_idx, raw_para in enumerate(paragraphs):
                para_text = raw_para
                overlap_text = ""
                overlap_origin_chunk_id = ""
                overlap_origin_source_locator = ""
                # prepend 1-sentence overlap from previous paragraph
                if para_idx > 0:
                    overlap_text = _trailing_sentences(paragraphs[para_idx - 1], 1)
                    if overlap_text and not para_text.startswith(overlap_text):
                        para_text = overlap_text + "\n" + para_text
                        previous_chunk = out[-1] if out else None
                        overlap_origin_chunk_id = (
                            previous_chunk.chunk_id
                            if previous_chunk is not None
                            else _stable_chunk_id(doc.paper_id, section.section_id, str(para_idx - 1))
                        )
                        overlap_origin_source_locator = str(
                            (
                                previous_chunk.metadata.get("source_locator")
                                if previous_chunk is not None
                                else section.metadata.get("source_locator")
                            )
                            or section.source_ref
                        )
                    else:
                        overlap_text = ""

                has_formula = bool(_LATEX_FORMULA.search(para_text) or _INLINE_FORMULA.search(para_text))
                formula_latex = _extract_formula_latex(para_text) if has_formula else ""
                formula_references = _matched_formula_references(raw_para, formula_lookup)

                has_figure, fig_key = _find_figure_ref(para_text)
                figure_id = ""
                generated_element_context = False
                visual_references: list[dict[str, Any]] = []
                if has_figure and fig_key:
                    figure_id = next(
                        (fid for fid in elements.figures if fig_key.lower() in fid.lower()), ""
                    )
                    if figure_id:
                        fig = elements.figures[figure_id]
                        para_text = para_text + f"\n[{fig.figure_id}: {fig.caption}]"
                        generated_element_context = True
                for ref in _body_visual_references(raw_para):
                    lookup = figure_lookup if ref["kind"] == "figure" else table_lookup
                    element = lookup.get(ref["label"])
                    if element is None:
                        continue
                    visual_references.append({
                        **ref,
                        "element_id": element.figure_id if ref["kind"] == "figure" else element.table_id,
                    })
                    if ref["kind"] == "figure" and not figure_id:
                        figure_id = element.figure_id

                chunk_type: ChunkType = "paragraph"

                out.append(PaperChunk(
                    chunk_id=_stable_chunk_id(doc.paper_id, section.section_id, str(para_idx)),
                    paper_id=doc.paper_id,
                    parse_source=parse_source,
                    structure_detected=structure_detected,
                    chunk_type=chunk_type,
                    parent_chunk_id=parent_id,
                    section_title=section.title,
                    section_role=roles,
                    section_index=idx,
                    has_formula=has_formula,
                    formula_latex=formula_latex,
                    has_figure=has_figure,
                    figure_id=figure_id,
                    references=cross_refs,
                    propositions_generated=False if need_propositions else True,
                    content=para_text,
                    metadata=_chunk_metadata(
                        paper_id=doc.paper_id,
                        chunk_type="paragraph",
                        section_title=section.title,
                        source_ref=section.source_ref,
                        source_locator=section.metadata.get("source_locator"),
                        semantic_text=raw_para,
                        base={
                            "is_parent": False,
                            "para_index": para_idx,
                            "section_id": section.section_id,
                            "section_level": section.level,
                            "page_start": section.page_start,
                            "page_end": section.page_end,
                            "needs_proposition_decomposition": need_propositions,
                            "formula_references": formula_references,
                            "visual_references": visual_references,
                            **({"fidelity_source_map": _fidelity_chunk_source_map(
                                doc,
                                metadata=section.metadata,
                                section_text=section.text,
                                paragraph_text=raw_para,
                                paragraph_index=para_idx,
                                excluded_chunk_content=tuple(
                                    reason
                                    for reason, excluded in (
                                        ("overlap_prefix", bool(overlap_text)),
                                        ("generated_element_context", generated_element_context),
                                    )
                                    if excluded
                                ),
                            )} if doc.fidelity is not None else {}),
                            **build_paragraph_span_metadata(
                                content=para_text,
                                overlap_text=overlap_text,
                                overlap_origin_chunk_id=overlap_origin_chunk_id,
                                overlap_origin_source_locator=overlap_origin_source_locator,
                            ),
                        },
                    ),
                ))

    def _add_formula_chunks(self, doc, parse_source, structure_detected, out):
        references_by_equation = _formula_references_by_equation(out)
        for eq in doc.equations:
            parent, match_strategy = _find_formula_parent(eq, out)
            section_title = parent.section_title if parent else "formula"
            section_role = parent.section_role if parent else []
            section_index = parent.section_index if parent else 0
            referenced_by_chunks = references_by_equation.get(eq.equation_id, [])
            formula_metadata = {
                **eq.metadata,
                "referenced_by_chunks": referenced_by_chunks,
            }
            enriched_eq = eq.model_copy(update={"metadata": formula_metadata})
            normalized_latex = _normalize_formula_text(eq.latex)
            formula_symbols = _formula_symbols(eq.latex)
            formula_operators = _formula_operators(eq.latex)
            formula_referenced_text = _formula_referenced_texts(enriched_eq)
            formula_text = _formula_to_text(enriched_eq, parent)
            out.append(PaperChunk(
                chunk_id=_stable_chunk_id(doc.paper_id, "eq", eq.equation_id),
                paper_id=doc.paper_id,
                parse_source=parse_source,
                structure_detected=structure_detected,
                chunk_type="formula",
                parent_chunk_id=parent.chunk_id if parent else None,
                section_title=section_title,
                section_role=section_role,
                section_index=section_index,
                has_formula=True,
                formula_latex=eq.latex,
                content=formula_text,
                metadata=_chunk_metadata(
                    paper_id=doc.paper_id,
                    chunk_type="formula",
                    section_title=section_title,
                    source_ref=eq.source_ref,
                    source_locator=eq.metadata.get("source_locator"),
                    semantic_text=f"{eq.equation_id}\n{eq.latex}",
                    base={
                        **formula_metadata,
                        "equation_id": eq.equation_id,
                        "page": eq.page,
                        "formula_normalized_latex": normalized_latex,
                        "formula_symbols": formula_symbols,
                        "formula_operators": formula_operators,
                        "formula_referenced_text": formula_referenced_text,
                        "reference_labels": sorted(_formula_reference_keys(eq)),
                        "formula_parent_match_strategy": match_strategy,
                        **({"fidelity_source_map": _fidelity_chunk_source_map(
                            doc, metadata=eq.metadata, element_ref=eq.equation_id,
                        )} if doc.fidelity is not None else {}),
                    },
                ),
            ))

    def _add_figure_chunks(self, doc, parse_source, structure_detected, out):
        references_by_element = _visual_references_by_element(out)
        figure_id_counts: dict[str, int] = {}
        for fig in doc.figures:
            figure_id_counts[fig.figure_id] = figure_id_counts.get(fig.figure_id, 0) + 1
        used_figure_chunk_ids: dict[str, int] = {}
        for fig in doc.figures:
            parent, match_strategy = _find_visual_parent(
                caption=fig.caption,
                page=fig.page,
                metadata=fig.metadata,
                chunks=out,
            )
            section_title = parent.section_title if parent else "figure"
            section_role = parent.section_role if parent else []
            section_index = parent.section_index if parent else 0
            figure_text = f"{fig.figure_id}\n{fig.caption}"
            content, content_sources = _figure_to_text(fig, parent)
            figure_identity = ""
            figure_id_parts = ["fig", fig.figure_id]
            if figure_id_counts.get(fig.figure_id, 0) > 1:
                figure_identity = _figure_visual_identity(fig)
                figure_id_parts.extend(["visual", figure_identity])
            figure_chunk_id, figure_chunk_occurrence = _dedupe_generated_chunk_id(
                doc.paper_id,
                figure_id_parts,
                used_figure_chunk_ids,
            )
            alignment_metadata = _visual_alignment_metadata(
                kind="figure",
                element_id=fig.figure_id,
                caption=fig.caption,
                page=fig.page,
                source_ref=fig.source_ref,
                metadata=fig.metadata,
                parent=parent,
                parent_strategy=match_strategy,
                references=references_by_element.get(("figure", fig.figure_id), []),
            )
            out.append(PaperChunk(
                chunk_id=figure_chunk_id,
                paper_id=doc.paper_id,
                parse_source=parse_source,
                structure_detected=structure_detected,
                chunk_type="figure",
                parent_chunk_id=parent.chunk_id if parent else None,
                section_title=section_title,
                section_role=section_role,
                section_index=section_index,
                has_figure=True,
                figure_id=fig.figure_id,
                content=content,
                metadata=_chunk_metadata(
                    paper_id=doc.paper_id,
                    chunk_type="figure",
                    section_title=section_title,
                    source_ref=fig.source_ref,
                    source_locator=fig.metadata.get("source_locator"),
                    semantic_text=figure_text,
                    base={
                        **fig.metadata,
                        **alignment_metadata,
                        "image_ref": fig.image_ref or fig.metadata.get("image_ref") or "",
                        "page": fig.page,
                        "figure_parent_match_strategy": match_strategy,
                        "content_sources": content_sources,
                        "figure_chunk_identity": figure_identity,
                        "figure_chunk_occurrence": figure_chunk_occurrence,
                        **({"fidelity_source_map": _fidelity_chunk_source_map(
                            doc, metadata=fig.metadata, element_ref=fig.figure_id,
                        )} if doc.fidelity is not None else {}),
                    },
                ),
            ))

    def _add_table_chunks(self, doc, parse_source, structure_detected, out):
        references_by_element = _visual_references_by_element(out)
        table_id_counts: dict[str, int] = {}
        for tbl in doc.tables:
            table_id_counts[tbl.table_id] = table_id_counts.get(tbl.table_id, 0) + 1
        used_table_chunk_ids: dict[str, int] = {}
        for tbl in doc.tables:
            parent, match_strategy = _find_visual_parent(
                caption=tbl.caption,
                page=tbl.page,
                metadata=tbl.metadata,
                chunks=out,
            )
            section_title = parent.section_title if parent else "table"
            section_role = parent.section_role if parent else []
            section_index = parent.section_index if parent else 0
            table_text, content_sources = _table_to_text(tbl, parent=parent)
            table_semantic_text = _table_semantic_text(
                tbl,
                tbl.rows[:_TABLE_PARENT_ROW_LIMIT],
            )
            table_identity = ""
            table_id_parts = ["tbl", tbl.table_id]
            if table_id_counts.get(tbl.table_id, 0) > 1:
                table_identity = _table_visual_identity(tbl)
                table_id_parts.extend(["visual", table_identity])
            table_chunk_id, table_chunk_occurrence = _dedupe_generated_chunk_id(
                doc.paper_id,
                table_id_parts,
                used_table_chunk_ids,
            )
            alignment_metadata = _visual_alignment_metadata(
                kind="table",
                element_id=tbl.table_id,
                caption=tbl.caption,
                page=tbl.page,
                source_ref=tbl.source_ref,
                metadata=tbl.metadata,
                parent=parent,
                parent_strategy=match_strategy,
                references=references_by_element.get(("table", tbl.table_id), []),
            )
            out.append(PaperChunk(
                chunk_id=table_chunk_id,
                paper_id=doc.paper_id,
                parse_source=parse_source,
                structure_detected=structure_detected,
                chunk_type="table",
                parent_chunk_id=parent.chunk_id if parent else None,
                section_title=section_title,
                section_role=section_role,
                section_index=section_index,
                has_table=True,
                content=table_text,
                metadata=_chunk_metadata(
                    paper_id=doc.paper_id,
                    chunk_type="table",
                    section_title=section_title,
                    source_ref=tbl.source_ref,
                    source_locator=tbl.metadata.get("source_locator"),
                    semantic_text=table_semantic_text,
                    base={
                        **tbl.metadata,
                        **alignment_metadata,
                        "table_id": tbl.table_id,
                        "page": tbl.page,
                        "table_parent_match_strategy": match_strategy,
                        "content_sources": content_sources,
                        "row_count": len(tbl.rows),
                        "table_chunk_identity": table_identity,
                        "table_chunk_occurrence": table_chunk_occurrence,
                        **({"fidelity_source_map": _fidelity_chunk_source_map(
                            doc, metadata=tbl.metadata, element_ref=tbl.table_id,
                        )} if doc.fidelity is not None else {}),
                        **({"table_grid_source_map": _table_grid_source_map(
                            tbl, body_row_start=0,
                            body_row_end=min(len(tbl.rows), _TABLE_PARENT_ROW_LIMIT) - 1,
                        )} if tbl.grid is not None else {}),
                    },
                ),
            ))
            if len(tbl.rows) <= _TABLE_PARENT_ROW_LIMIT:
                continue
            for row_start in range(0, len(tbl.rows), _TABLE_ROW_GROUP_SIZE):
                row_end = min(row_start + _TABLE_ROW_GROUP_SIZE, len(tbl.rows))
                group_rows = tbl.rows[row_start:row_end]
                group_text, group_sources = _table_to_text(
                    tbl,
                    parent=parent,
                    rows=group_rows,
                    row_start=row_start,
                    row_end=row_end - 1,
                )
                group_semantic_text = _table_semantic_text(tbl, group_rows)
                row_group_id_parts = [
                    *table_id_parts,
                    "rows",
                    str(row_start),
                    str(row_end - 1),
                ]
                if table_chunk_occurrence:
                    row_group_id_parts.extend(["parent_occurrence", table_chunk_occurrence])
                out.append(PaperChunk(
                    chunk_id=_stable_chunk_id(doc.paper_id, *row_group_id_parts),
                    paper_id=doc.paper_id,
                    parse_source=parse_source,
                    structure_detected=structure_detected,
                    chunk_type="table",
                    parent_chunk_id=table_chunk_id,
                    section_title=section_title,
                    section_role=section_role,
                    section_index=section_index,
                    has_table=True,
                    content=group_text,
                    metadata=_chunk_metadata(
                        paper_id=doc.paper_id,
                        chunk_type="table",
                        section_title=section_title,
                        source_ref=tbl.source_ref,
                        source_locator=tbl.metadata.get("source_locator"),
                        semantic_text=group_semantic_text,
                        base={
                            **tbl.metadata,
                            **alignment_metadata,
                            "table_id": tbl.table_id,
                            "page": tbl.page,
                            "table_parent_match_strategy": match_strategy,
                            "content_sources": group_sources,
                            "row_count": len(tbl.rows),
                            "row_start": row_start,
                            "row_end": row_end - 1,
                            "parent_table_chunk_id": table_chunk_id,
                            "is_table_row_group": True,
                            **({"fidelity_source_map": _fidelity_chunk_source_map(
                                doc, metadata=tbl.metadata, element_ref=tbl.table_id,
                            )} if doc.fidelity is not None else {}),
                            **({"table_grid_source_map": _table_grid_source_map(
                                tbl, body_row_start=row_start, body_row_end=row_end - 1,
                            )} if tbl.grid is not None else {}),
                            "table_chunk_identity": table_identity,
                            "table_chunk_occurrence": table_chunk_occurrence,
                        },
                    ),
                ))

    def _fallback_fixed_token_chunks(
        self,
        doc: ResearchDocument,
        parse_source: ParseSource,
        elements: ScannedElements,
        token_limit: int = 1500,
    ) -> list[PaperChunk]:
        """Fixed token-window chunking for poorly structured documents (PRD §3 fallback)."""
        full_text = "\n\n".join(s.text for s in doc.sections)
        words = full_text.split()
        chunks: list[PaperChunk] = []
        formula_lookup = _formula_lookup(elements.equations)
        source_ref = (
            doc.lineage.source_refs[0]
            if doc.lineage.source_refs
            else f"paper://{doc.paper_id}"
        )
        for chunk_idx in range(0, len(words), token_limit):
            content = " ".join(words[chunk_idx : chunk_idx + token_limit])
            formula_references = _matched_formula_references(content, formula_lookup)
            chunks.append(PaperChunk(
                chunk_id=_stable_chunk_id(doc.paper_id, "fallback", str(chunk_idx // token_limit)),
                paper_id=doc.paper_id,
                parse_source=parse_source,
                structure_detected=False,
                chunk_type="paragraph",
                content=content,
                metadata=_chunk_metadata(
                    paper_id=doc.paper_id,
                    chunk_type="paragraph",
                    section_title="fallback",
                    source_ref=source_ref,
                    semantic_text=content,
                    base={
                        "fallback": True,
                        "chunk_index": chunk_idx // token_limit,
                        "section_ids": [
                            section.section_id
                            for section in doc.sections
                            if section.text.strip()
                        ],
                        "token_start": chunk_idx,
                        "token_end": min(chunk_idx + token_limit, len(words)),
                        "formula_references": formula_references,
                        **({"fidelity_source_map": _fidelity_chunk_source_map(
                            doc, metadata={}, paragraph_text=content,
                        )} if doc.fidelity is not None else {}),
                    },
                ),
            ))
        return chunks


__all__ = ["PaperDocumentChunker"]
