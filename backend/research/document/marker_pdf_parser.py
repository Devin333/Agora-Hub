from __future__ import annotations

import json
import math
import os
import re
import time
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from backend.foundation import build_stable_id
from backend.research.document import marker_cache_identity
from backend.research.document.docker_pdf_parser import (
    first_existing_file,
    metadata_text,
    page_rects,
    run_docker_command,
    safe_paper_id,
    source_locator,
    stage_pdf_for_docker,
)
from backend.research.document.marker_table_grid import build_marker_table_grid
from backend.research.document.marker_assets import copy_marker_image, write_embedded_marker_image
from backend.research.document.marker_geometry import (
    map_block_geometry,
    pdf_page_rotations,
    marker_id_page_value,
    resolve_page,
)
from backend.research.domain.common import SourceLineage
from backend.research.domain.document_fidelity import (
    BoundingBox,
    DocumentBlock,
    DocumentFidelity,
    DocumentPage,
    InlineMathSpan,
)
from backend.research.domain.document import (
    ResearchDocument,
    ResearchEquation,
    ResearchFigure,
    ResearchSection,
    ResearchTable,
)


class MarkerPdfDocumentParser:
    """Docker-backed Marker parser for PDF parser bake-off and cascade use."""

    def __init__(self, *, command_runner: Callable[..., Any] | None = None) -> None:
        self._command_runner = command_runner or run_docker_command

    def cache_identity(self) -> dict[str, Any] | None:
        return marker_cache_identity.verified_marker_cache_identity(self._command_runner)

    def parse(
        self,
        paper_id: str,
        source_bytes: bytes,
        *,
        execution_identity: Any | None = None,
    ) -> ResearchDocument:
        source_hash = sha256(source_bytes).hexdigest()
        source_ref = f"arxiv://{paper_id}/pdf"
        started_at = time.perf_counter()
        input_dir, output_dir, _pdf_path = stage_pdf_for_docker(
            backend="marker",
            paper_id=paper_id,
            pdf_bytes=source_bytes,
        )
        command = _marker_command(input_dir=input_dir, output_dir=output_dir)
        runner_kwargs: dict[str, Any] = {
            "timeout_seconds": _marker_timeout_seconds(),
        }
        if self._command_runner is not run_docker_command:
            runner_kwargs.update(
                execution_identity=execution_identity,
                paper_id=paper_id,
                backend="marker",
            )
        outcome = self._command_runner(command, **runner_kwargs)
        document = _document_from_marker_output(
            paper_id=paper_id,
            source_ref=source_ref,
            source_hash=source_hash,
            pdf_bytes=source_bytes,
            output_dir=output_dir,
            duration_seconds=time.perf_counter() - started_at,
            command=command,
        )
        return _with_execution_receipt(document, outcome)



def _with_execution_receipt(document: ResearchDocument, outcome: Any) -> ResearchDocument:
    receipt = getattr(outcome, "receipt", None)
    if receipt is None:
        return document
    return document.model_copy(
        update={
            "metadata": {
                **dict(document.metadata),
                "execution_receipt_ref": f"execution-receipt://{receipt.execution_id}",
                "execution_receipt_checksum": receipt.receipt_checksum,
                "execution_provider": receipt.provider_id,
                "execution_status": receipt.status.value,
            }
        }
    )


def _marker_command(*, input_dir: Path, output_dir: Path) -> list[str]:
    image = os.environ.get("NEWSROOM_MARKER_DOCKER_IMAGE", "newsroom-marker:latest").strip()
    extra_args = _split_env_args(os.environ.get("NEWSROOM_MARKER_DOCKER_ARGS", ""))
    cache_dir = os.environ.get("NEWSROOM_MARKER_CACHE_DIR", "").strip()
    return [
        "docker",
        "run",
        "--rm",
        "--gpus",
        "all",
        "-e",
        "TORCH_DEVICE=cuda",
        "-e",
        "HF_HOME=/root/.cache/huggingface",
        *extra_args,
        *(
            [
                "-v",
                f"{Path(cache_dir).resolve()}:/root/.cache",
            ]
            if cache_dir
            else []
        ),
        "-v",
        f"{input_dir.resolve()}:/input",
        "-v",
        f"{output_dir.resolve()}:/output",
        image,
        "marker_single",
        "/input/input.pdf",
        "--output_format",
        "json",
        "--output_dir",
        "/output",
    ]


def _split_env_args(raw: str) -> list[str]:
    return [part for part in raw.split() if part.strip()]


def _marker_timeout_seconds() -> int:
    raw = os.environ.get("NEWSROOM_MARKER_TIMEOUT_SECONDS", "1800")
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("NEWSROOM_MARKER_TIMEOUT_SECONDS must be an integer") from exc
    if value <= 0:
        raise ValueError("NEWSROOM_MARKER_TIMEOUT_SECONDS must be positive")
    return value


def _document_from_marker_output(
    *,
    paper_id: str,
    source_ref: str,
    source_hash: str,
    pdf_bytes: bytes,
    output_dir: Path,
    duration_seconds: float,
    command: list[str],
) -> ResearchDocument:
    json_path = _marker_json_path(output_dir)
    markdown_path = _marker_markdown_path(output_dir)
    rects = page_rects(pdf_bytes)
    rotations = pdf_page_rotations(pdf_bytes)
    artifact_root = _paper_artifact_dir(paper_id)
    figures_dir = artifact_root / "figures"
    tables_dir = artifact_root / "tables"

    warnings: list[str] = []
    warning_codes: list[dict[str, Any]] = []
    if json_path is not None:
        blocks = _load_marker_blocks(json_path)
        components = _components_from_blocks(
            paper_id=paper_id,
            source_ref=source_ref,
            blocks=blocks,
            page_rects_by_number=rects,
            page_rotations_by_number=rotations,
            output_root=json_path.parent,
            figures_dir=figures_dir,
            tables_dir=tables_dir,
            warnings=warnings,
            warning_codes=warning_codes,
        )
        parser_content_ref = str(json_path)
    elif markdown_path is not None:
        components = _components_from_markdown(
            paper_id=paper_id,
            source_ref=source_ref,
            markdown=markdown_path.read_text(encoding="utf-8"),
        )
        parser_content_ref = str(markdown_path)
        warnings.append("json artifact missing; parsed markdown fallback")
        warning_codes.append({"code": "marker_json_missing"})
    else:
        raise FileNotFoundError(f"Marker did not produce JSON or Markdown output under {output_dir}")

    reading_order_candidates: list[dict[str, Any]] = []
    reading_order_candidates_omitted = {"pages": 0, "source_ids": 0, "candidate_ids": 0}
    fidelity = _build_marker_fidelity(
        source_hash=source_hash,
        source_ref=source_ref,
        parser_content_ref=parser_content_ref,
        blocks=blocks if json_path is not None else [],
        page_rects_by_number=rects,
        page_rotations_by_number=rotations,
        tables=components.tables,
        equations=components.equations,
        figures=components.figures,
        reading_order_candidates=reading_order_candidates,
        reading_order_candidates_omitted=reading_order_candidates_omitted,
    )
    metadata = {
        "parse_source": "marker",
        "parser_backend": "marker",
        "parser_output_ref": str(output_dir),
        "parser_content_ref": parser_content_ref,
        "parser_duration_seconds": round(duration_seconds, 3),
        "parser_command": _redacted_command(command),
        "parser_warnings": warnings,
        "parser_warning_codes": warning_codes,
        "fidelity_status": "validated" if fidelity is not None else "unavailable",
        "reading_order_candidates": reading_order_candidates,
        "reading_order_candidates_omitted": reading_order_candidates_omitted,
        **({} if fidelity is not None else {
            "fidelity_unavailable_reason": "marker_output_contains_unlocated_or_unmapped_structure",
        }),
        "parse_quality": _parse_quality(
            components.sections,
            components.figures,
            components.tables,
            components.equations,
        ),
    }
    return ResearchDocument(
        paper_id=paper_id,
        source_hash=source_hash,
        sections=components.sections,
        figures=components.figures,
        tables=components.tables,
        equations=components.equations,
        fidelity=fidelity,
        lineage=SourceLineage(source_refs=[source_ref], source_hash=source_hash),
        metadata=metadata,
    )


_MAX_READING_ORDER_DIAGNOSTIC_PAGES = 100
_MAX_READING_ORDER_DIAGNOSTIC_BLOCK_IDS_PER_PAGE = 200


def _build_marker_fidelity(**inputs: Any) -> DocumentFidelity | None:
    """Fail closed on invalid or incomplete fidelity, without failing document parsing."""
    try:
        return _build_marker_fidelity_unchecked(**inputs)
    except (OSError, TypeError, ValueError, ValidationError):
        return None


def _build_marker_fidelity_unchecked(
    *,
    source_hash: str,
    source_ref: str,
    parser_content_ref: str,
    blocks: list[dict[str, Any]],
    page_rects_by_number: dict[int, Any],
    page_rotations_by_number: dict[int, int],
    reading_order_candidates: list[dict[str, Any]] | None = None,
    reading_order_candidates_omitted: dict[str, int] | None = None,
    tables: list[ResearchTable],
    equations: list[ResearchEquation],
    figures: list[ResearchFigure],
) -> DocumentFidelity | None:
    """Build the typed fidelity graph only from explicitly scoreable Marker blocks."""
    if not page_rects_by_number or not blocks:
        return None
    parser_source_digest = sha256(Path(__file__).read_bytes()).hexdigest()
    geometry_source_digest = sha256(Path(__file__).with_name("marker_geometry.py").read_bytes()).hexdigest()
    table_source_digest = sha256(Path(__file__).with_name("marker_table_grid.py").read_bytes()).hexdigest()
    parser_fingerprint = "sha256:" + sha256(json.dumps({
        "parser_source": parser_source_digest,
        "geometry_source": geometry_source_digest,
        "table_grid_source": table_source_digest,
        "asset_source": sha256(Path(__file__).with_name("marker_assets.py").read_bytes()).hexdigest(),
        "image": os.environ.get("NEWSROOM_MARKER_DOCKER_IMAGE", "newsroom-marker:latest"),
        "args": os.environ.get("NEWSROOM_MARKER_DOCKER_ARGS", ""),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    fidelity_schema_digest = sha256(
        Path(__file__).parents[1].joinpath("domain", "document_fidelity.py").read_bytes()
    ).hexdigest()
    document_schema_digest = sha256(
        Path(__file__).parents[1].joinpath("domain", "document.py").read_bytes()
    ).hexdigest()
    profile_fingerprint = "sha256:" + sha256(json.dumps({
        "schema": "research-document-fidelity.v1",
        "fidelity_schema_source": fidelity_schema_digest,
        "document_schema_source": document_schema_digest,
        "normalization": "research-marker-normalizer.v1",
        "geometry": "marker-geometry.v1",
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        content_digest = sha256(Path(parser_content_ref).read_bytes()).hexdigest()
    except OSError:
        return None
    page_models = tuple(
        DocumentPage(
            pdf_page_number=number,
            width=float(rect.width),
            height=float(rect.height),
            dimension_unit="pt",
            rotation_degrees=int(page_rotations_by_number.get(number, 0)) % 360,
        )
        for number, rect in sorted(page_rects_by_number.items())
    )
    table_by_block: dict[str, ResearchTable] = {}
    for table in tables:
        provenance = table.metadata.get("marker_provenance")
        if isinstance(provenance, dict) and provenance.get("block_id"):
            table_by_block[str(provenance["block_id"])] = table
    figure_by_block: dict[str, ResearchFigure] = {}
    for figure in figures:
        provenance = figure.metadata.get("marker_provenance")
        if isinstance(provenance, dict) and provenance.get("block_id"):
            figure_by_block[str(provenance["block_id"])] = figure
    equation_by_block: dict[str, ResearchEquation] = {}
    for equation in equations:
        provenance = equation.metadata.get("marker_provenance")
        if isinstance(provenance, dict) and provenance.get("block_id"):
            equation_by_block[str(provenance["block_id"])] = equation

    selected: list[tuple[dict[str, Any], int, tuple[float, float, float, float] | None]] = []
    for block in blocks:
        mapping = map_block_geometry(
            block,
            page_rects=page_rects_by_number,
            page_rotations=page_rotations_by_number,
        )
        if not mapping.page.scoreable:
            return None
        selected.append((block, mapping.page.page_number, mapping.pdf_rect))
    if len(selected) != len(blocks):
        return None

    ids_by_marker: dict[str, str] = {}
    for block, _, _ in selected:
        provenance = block.get("_marker_provenance") or {}
        marker_id = str(provenance.get("block_id") or "")
        if not marker_id or marker_id in ids_by_marker:
            return None
        ids_by_marker[marker_id] = marker_id
    blocks_by_page: dict[int, list[tuple[dict[str, Any], tuple[float, float, float, float] | None]]] = {}
    for block, page, rect in selected:
        blocks_by_page.setdefault(page, []).append((block, rect))

    fidelity_blocks: list[DocumentBlock] = []
    table_grids = []
    for page, page_blocks in sorted(blocks_by_page.items()):
        if reading_order_candidates is not None and reading_order_candidates_omitted is not None:
            if len(reading_order_candidates) < _MAX_READING_ORDER_DIAGNOSTIC_PAGES:
                order_candidate = _page_reading_order(
                    page_blocks,
                    page_rects_by_number[page],
                    page_rotation=page_rotations_by_number.get(page, 0),
                )
                candidate_rows = sorted(order_candidate.items(), key=lambda item: item[1][0])
                source_ids = [
                    str((block.get("_marker_provenance") or {}).get("block_id") or index)
                    for index, (block, _) in enumerate(page_blocks)
                ]
                limit = _MAX_READING_ORDER_DIAGNOSTIC_BLOCK_IDS_PER_PAGE
                reading_order_candidates.append({
                    "page": page,
                    "source_ids": source_ids[:limit],
                    "candidate_ids": [block_id for block_id, _ in candidate_rows[:limit]],
                    "reason": next((reason for _, (_, _, reason) in candidate_rows if reason), None),
                    "admission": "candidate" if all(status != "uncertain" for _, (_, status, _) in candidate_rows) else "uncertain",
                    "source_ids_omitted": max(0, len(source_ids) - limit),
                    "candidate_ids_omitted": max(0, len(candidate_rows) - limit),
                })
                reading_order_candidates_omitted["source_ids"] += max(0, len(source_ids) - limit)
                reading_order_candidates_omitted["candidate_ids"] += max(0, len(candidate_rows) - limit)
            else:
                reading_order_candidates_omitted["pages"] += 1
        for source_order, (block, rect) in enumerate(page_blocks):
            provenance = block.get("_marker_provenance") or {}
            block_id = str(provenance.get("block_id") or f"marker-block-{page}-{source_order}")
            parent_raw = str(provenance.get("parent_id") or "")
            if parent_raw and parent_raw not in ids_by_marker:
                return None
            parent_id = parent_raw or None
            owner_raw = str(provenance.get("owner_id") or "")
            if owner_raw and owner_raw not in ids_by_marker:
                return None
            content_owner_id = owner_raw or block_id
            # Candidate order is not the adopted order until the frozen baseline,
            # profile and cross-consumer regression gates approve it. Preserve the
            # backend order in both domain and chunk consumers in the meantime.
            reading_order = source_order
            order_status = "uncertain" if rect is None else "preserved"
            order_reason = "layout_geometry_unavailable" if rect is None else None
            kind = "other" if _is_group_child_element(block) else _fidelity_block_kind(block)
            element_ref = None
            table = table_by_block.get(block_id)
            if kind == "table":
                if table is None or table.grid is None:
                    return None
                else:
                    element_ref = table.table_id
                    table_grids.append(table.grid)
            elif kind == "figure":
                figure = figure_by_block.get(block_id)
                if figure is None:
                    return None
                element_ref = figure.figure_id
            elif kind == "equation":
                equation = equation_by_block.get(block_id)
                if equation is None:
                    return None
                else:
                    element_ref = f"equation:{equation.equation_id}"
            box = None
            unknown_reason = None
            if rect is not None:
                box = BoundingBox(
                    x0=rect[0], y0=rect[1], x1=rect[2], y1=rect[3],
                    coordinate_space="pdf_points", unit="pt", origin="top_left",
                    rotation_degrees=int(page_rotations_by_number.get(page, 0)) % 360,
                )
            else:
                unknown_reason = "Marker block has no reliable geometry"
            block_text = (
                equation.latex
                if kind == "equation" and equation is not None
                else _block_text(block)
            )
            math_spans = (
                (InlineMathSpan(
                    start_offset=0,
                    end_offset=len(equation.latex),
                    offset_text_version="raw",
                    raw_latex=equation.latex,
                    display=True,
                    source_locator=equation.source_ref,
                ),)
                if kind == "equation" and equation is not None and equation.latex
                else ()
            )
            fidelity_blocks.append(DocumentBlock(
                block_id=block_id,
                page_number=page,
                parent_id=parent_id,
                kind=kind,
                source_order=source_order,
                reading_order=reading_order,
                order_status=order_status,
                order_reason=order_reason,
                element_ref=element_ref,
                source_locator=source_locator(source_ref, page=page, pdf_rect=rect),
                bbox=box,
                bbox_unknown_reason=unknown_reason,
                text=block_text,
                inline_math=math_spans,
                content_owner_id=content_owner_id,
            ))
    try:
        return DocumentFidelity(
            schema_version="research-document-fidelity.v1",
            normalization_version="research-marker-normalizer.v1",
            source_hash=source_hash,
            parser_fingerprint=parser_fingerprint,
            profile_fingerprint=profile_fingerprint,
            content_checksum="sha256:" + content_digest,
            artifact_refs=(),
            pages=page_models,
            blocks=tuple(fidelity_blocks),
            tables=tuple(table_grids),
        )
    except ValidationError:
        return None


def _page_reading_order(
    page_blocks: list[tuple[dict[str, Any], tuple[float, float, float, float] | None]],
    page_rect: Any,
    *,
    page_rotation: int = 0,
) -> dict[str, tuple[int, str, str | None]]:
    """Return a conservative two-column candidate; uncertain pages retain backend order."""
    source_ids = [
        str((block.get("_marker_provenance") or {}).get("block_id") or index)
        for index, (block, _) in enumerate(page_blocks)
    ]

    def preserve(reason: str | None = None) -> dict[str, tuple[int, str, str | None]]:
        status = "uncertain" if reason else "preserved"
        return {block_id: (index, status, reason) for index, block_id in enumerate(source_ids)}

    if not page_blocks:
        return {}
    page_width = float(getattr(page_rect, "width", 0.0) or 0.0)
    page_height = float(getattr(page_rect, "height", 0.0) or 0.0)
    if page_width <= 0 or page_height <= 0:
        return preserve("invalid_page_geometry")
    rotation = page_rotation or getattr(page_rect, "rotation", 0)
    if rotation not in (None, 0, 360):
        return preserve("rotated_page_layout_unsupported")

    eligible = {"text", "heading", "caption", "table", "figure", "equation", "list"}
    geometry: dict[int, tuple[float, float, float, float]] = {}
    candidates: list[int] = []
    for index, (block, rect) in enumerate(page_blocks):
        kind = _fidelity_block_kind(block)
        block_rotation = block.get("rotation_degrees", block.get("rotation", 0))
        if block_rotation not in (None, 0, 360, "0", "360"):
            return preserve("rotated_block_layout_unsupported")
        if _block_children(block) or _is_group_child_element(block):
            continue
        if kind not in eligible:
            continue
        if rect is None:
            return preserve("layout_geometry_unavailable")
        x0, y0, x1, y1 = (float(value) for value in rect)
        if not all(map(math.isfinite, (x0, y0, x1, y1))) or x1 <= x0 or y1 <= y0:
            return preserve("invalid_block_geometry")
        # Geometry outside the page usually indicates a coordinate-schema mismatch.
        if x0 < -page_width * .02 or y0 < -page_height * .02 or x1 > page_width * 1.02 or y1 > page_height * 1.02:
            return preserve("block_geometry_outside_page")
        geometry[index] = (x0, y0, x1, y1)
        candidates.append(index)
    if len(candidates) < 4:
        return preserve("insufficient_two_column_evidence")

    # Require a wide, centered center gap between narrow blocks, supported by
    # repeated blocks on both sides. Do not infer columns from a single outlier.
    narrow = [i for i in candidates if geometry[i][2] - geometry[i][0] <= page_width * .62]
    centers = sorted((geometry[i][0] + geometry[i][2]) / 2 for i in narrow)
    gaps = [(centers[i + 1] - centers[i], i) for i in range(len(centers) - 1)]
    gap, gap_index = max(gaps, default=(0.0, -1))
    if gap < page_width * .18:
        return preserve("no_reliable_center_gap")
    split = (centers[gap_index] + centers[gap_index + 1]) / 2
    if not page_width * .35 <= split <= page_width * .65:
        return preserve("off_center_column_gap")

    left: list[int] = []
    right: list[int] = []
    spanning: list[int] = []
    for index in candidates:
        x0, y0, x1, y1 = geometry[index]
        width = x1 - x0
        if width >= page_width * .62 or x0 < split < x1:
            # It must bridge the inferred gutter, rather than merely overlap it.
            if x0 > split - page_width * .08 or x1 < split + page_width * .08:
                return preserve("ambiguous_column_overlap")
            spanning.append(index)
        elif (x0 + x1) / 2 < split:
            if x1 > split - page_width * .04:
                return preserve("ambiguous_column_overlap")
            left.append(index)
        else:
            if x0 < split + page_width * .04:
                return preserve("ambiguous_column_overlap")
            right.append(index)
    if len(left) < 2 or len(right) < 2:
        return preserve("insufficient_two_column_evidence")

    # Three or more distinct narrow x-center populations are outside this policy.
    left_centers = sorted((geometry[i][0] + geometry[i][2]) / 2 for i in left)
    right_centers = sorted((geometry[i][0] + geometry[i][2]) / 2 for i in right)
    if max(left_centers) >= split or min(right_centers) <= split:
        return preserve("complex_column_layout_unsupported")
    for centers_in_column in (left_centers, right_centers):
        lane_gaps = [
            (centers_in_column[index + 1] - centers_in_column[index], index)
            for index in range(len(centers_in_column) - 1)
        ]
        lane_gap, lane_index = max(lane_gaps, default=(0.0, -1))
        if lane_gap >= page_width * .16 and lane_index >= 1 and len(centers_in_column) - lane_index - 1 >= 2:
            return preserve("complex_column_layout_unsupported")
    for boundary_index in spanning:
        _, boundary_top, _, boundary_bottom = geometry[boundary_index]
        if any(geometry[index][1] < boundary_bottom and geometry[index][3] > boundary_top
               for index in (*left, *right)):
            return preserve("overlapping_band_conflict")

    def column_key(index: int) -> tuple[float, float, int]:
        x0, y0, _, _ = geometry[index]
        return y0, x0, index

    # Full-width blocks are vertical band boundaries. Within each band the
    # reading direction is left column then right column; boundary ties retain
    # the backend's source order. No page-wide x/y sort is performed.
    ordered: list[int] = []
    cursor_y = -math.inf
    for boundary_y in sorted({geometry[i][1] for i in spanning}):
        band_left = [i for i in left if cursor_y <= geometry[i][1] < boundary_y]
        band_right = [i for i in right if cursor_y <= geometry[i][1] < boundary_y]
        ordered.extend(sorted(band_left, key=column_key))
        ordered.extend(sorted(band_right, key=column_key))
        boundary = [i for i in spanning if geometry[i][1] == boundary_y]
        ordered.extend(sorted(boundary))
        cursor_y = boundary_y
    band_left = [i for i in left if geometry[i][1] >= cursor_y]
    band_right = [i for i in right if geometry[i][1] >= cursor_y]
    ordered.extend(sorted(band_left, key=column_key))
    ordered.extend(sorted(band_right, key=column_key))
    if len(ordered) != len(candidates) or len(set(ordered)) != len(candidates):
        return preserve("overlapping_band_conflict")

    # Non-candidate blocks stay anchored to their backend slots. Candidate
    # positions are replaced as a group, preserving a complete local permutation.
    candidate_set = set(candidates)
    iterator = iter(ordered)
    final_order = [next(iterator) if i in candidate_set else i for i in range(len(page_blocks))]
    return {
        source_ids[source_index]: (
            order,
            "reordered" if source_index != order else "preserved",
            "high_confidence_two_column_vertical_bands" if source_index != order else None,
        )
        for order, source_index in enumerate(final_order)
    }


def _heading_level(block: dict[str, Any]) -> int:
    """Read explicit Marker hierarchy metadata; use level 1 when it is absent."""
    for key in ("heading_level", "level", "text_level"):
        value = block.get(key)
        if type(value) is int and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit() and int(value.strip()) > 0:
            return int(value.strip())
    hierarchy = block.get("section_hierarchy")
    if isinstance(hierarchy, dict):
        levels = [int(key) for key in hierarchy if isinstance(key, str) and key.isdigit() and int(key) > 0]
        if levels:
            return max(levels)
    return 1


def _fidelity_block_kind(block: dict[str, Any]) -> str:
    kind = _block_kind(block)
    return kind if kind in {"text", "heading", "caption", "table", "figure", "equation", "list", "footnote", "header", "footer"} else "other"


class _Components:
    def __init__(self) -> None:
        self.sections: list[ResearchSection] = []
        self.figures: list[ResearchFigure] = []
        self.tables: list[ResearchTable] = []
        self.equations: list[ResearchEquation] = []


def _marker_json_path(output_dir: Path) -> Path | None:
    direct = first_existing_file(
        output_dir,
        (
            "input.json",
            "input_meta.json",
            "input_content.json",
            "output.json",
            "document.json",
        ),
    )
    if direct is not None:
        return direct
    candidates = [
        path for path in output_dir.rglob("*.json")
        if path.is_file() and not path.name.startswith(".")
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.name.endswith("_meta.json"), len(path.parts), path.name))
    return candidates[0]


def _marker_markdown_path(output_dir: Path) -> Path | None:
    return first_existing_file(output_dir, ("input.md", "output.md", "document.md"))


def _load_marker_blocks(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    blocks = _extract_blocks(payload)
    if not blocks:
        raise ValueError(f"Marker JSON did not contain parse blocks: {path}")
    return blocks


def _extract_blocks(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("blocks", "children", "items", "content", "pages"):
        value = payload.get(key)
        if isinstance(value, list):
            if key == "pages":
                return _flatten_pages(value)
            return _flatten_blocks(value)
    if any(key in payload for key in ("text", "html", "markdown", "block_type", "type")):
        return [payload]
    return []


def _flatten_pages(pages: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page_position, page in enumerate(pages):
        if not isinstance(page, dict):
            continue
        raw_blocks = page.get("blocks") or page.get("children") or page.get("items") or []
        if not isinstance(raw_blocks, list):
            continue
        children = _flatten_blocks(
            raw_blocks,
            path_prefix=("pages", page_position),
            page_context=_declared_page_context(page, is_page_container=True),
        )
        for child in children:
            child["_marker_provenance"]["source_order"] += len(out)
        out.extend(children)
    return out


def _flatten_blocks(
    blocks: list[Any],
    *,
    parent_id: str | None = None,
    owner_id: str | None = None,
    owner_kind: str | None = None,
    parent_kind: str | None = None,
    page_context: dict[str, Any] | None = None,
    path_prefix: tuple[int | str, ...] = (),
) -> list[dict[str, Any]]:
    """Flatten Marker trees in stable preorder without collapsing equal content."""
    out: list[dict[str, Any]] = []
    for position, item in enumerate(blocks):
        if not isinstance(item, dict):
            continue
        path = (*path_prefix, position)
        block = dict(item)
        raw_id = block.get("id")
        block_id = str(raw_id) if raw_id is not None and str(raw_id).strip() else (
            "marker:block:" + "/".join(str(part) for part in path)
        )
        kind = _block_kind(block)
        semantic_owner_id = block_id if kind in {"table", "figure"} else owner_id
        semantic_owner_kind = kind if kind in {"table", "figure"} else owner_kind
        record: dict[str, Any] = {
            "block_id": block_id,
            "original_id": raw_id,
            "parent_id": parent_id,
            "child_position": position,
            "tree_path": list(path),
            "source_order": len(out),
            "owner_id": semantic_owner_id,
            "owner_kind": semantic_owner_kind,
            "caption_of": owner_id if kind == "caption" else None,
            "caption_block_ids": [],
        }
        block["_marker_provenance"] = record
        block["_marker_parent_kind"] = parent_kind or ""
        inherited_page = page_context
        if _is_marker_page_container(block):
            inherited_page = _merge_page_context(
                page_context,
                _declared_page_context(block, is_page_container=True),
            )
        if inherited_page is not None:
            block["_marker_page_context"] = inherited_page
        out.append(block)
        children = block.get("children") or block.get("blocks") or block.get("items")
        if isinstance(children, list):
            flattened_children = _flatten_blocks(
                children,
                parent_id=block_id,
                owner_id=semantic_owner_id,
                owner_kind=semantic_owner_kind,
                parent_kind=kind,
                page_context=inherited_page,
                path_prefix=path,
            )
            for child in flattened_children:
                child_provenance = child["_marker_provenance"]
                if child_provenance.get("caption_of") == block_id:
                    record["caption_block_ids"].append(child_provenance["block_id"])
            for child in flattened_children:
                child["_marker_provenance"]["source_order"] += len(out)
            out.extend(flattened_children)
    return out


def _is_marker_page_container(block: dict[str, Any]) -> bool:
    raw_type = (
        block.get("type")
        or block.get("block_type")
        or block.get("blockType")
        or block.get("category")
        or ""
    )
    normalized = str(raw_type).strip().casefold().replace("_", "").replace("-", "")
    return normalized == "page"


def _declared_page_context(
    block: dict[str, Any],
    *,
    is_page_container: bool = False,
) -> dict[str, Any] | None:
    declarations = []
    for key in ("page", "page_number", "page_idx", "page_id", "page_index"):
        if key in block:
            declarations.append({"field": key, "value": block[key], "scheme": _page_convention(block)})
    marker_id = metadata_text(block.get("id"))
    marker_page = marker_id_page_value(marker_id)
    if marker_page is not None:
        declarations.append({
            "field": "id",
            "value": marker_page,
            "scheme": "marker_id_page_segment:zero_based",
        })
    context: dict[str, Any] = {}
    if declarations:
        context.update(declarations[0])
        context["declarations"] = declarations
    page_bbox = block.get("page_bbox")
    if is_page_container and page_bbox is None:
        page_bbox = block.get("bbox")
    if any((
        block.get("coordinate_schema") is not None,
        page_bbox is not None,
        block.get("rotation") is not None,
    )):
        context.update({
            "coordinate_schema": block.get("coordinate_schema"),
            "page_bbox": page_bbox,
            "page_rotation": block.get("rotation"),
        })
    return context or None


def _merge_page_context(
    parent: dict[str, Any] | None,
    child: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if parent is None:
        return child
    if child is None:
        return parent
    merged = {**parent, **{key: value for key, value in child.items() if value is not None}}
    # Preserve the primary inherited declaration for audit presentation, while
    # retaining every declaration below for conflict detection by resolve_page.
    for key in ("field", "value", "scheme"):
        if key in parent:
            merged[key] = parent[key]
    parent_declarations = parent.get("declarations") or (
        [{key: parent[key] for key in ("field", "value", "scheme") if key in parent}]
        if "field" in parent else []
    )
    child_declarations = child.get("declarations") or (
        [{key: child[key] for key in ("field", "value", "scheme") if key in child}]
        if "field" in child else []
    )
    merged["declarations"] = [*parent_declarations, *child_declarations]
    return merged


def _page_convention(block: dict[str, Any]) -> str:
    resolved = resolve_page(block, page_count=2**31 - 1)
    return resolved.convention.value


def _block_provenance(
    block: dict[str, Any], *, page: int | None, locator: str
) -> dict[str, Any]:
    raw = block.get("_marker_provenance")
    raw = raw if isinstance(raw, dict) else {}
    return {
        "block_id": raw.get("block_id"),
        "original_id": raw.get("original_id"),
        "parent_id": raw.get("parent_id"),
        "child_position": raw.get("child_position"),
        "source_order": raw.get("source_order"),
        "page": {
            "value": _raw_page_value(block),
            "scheme": _page_convention(block),
            "normalized": page,
        },
        "locator": locator,
        "owner": {
            "block_id": raw.get("owner_id"),
            "kind": raw.get("owner_kind"),
            "caption_of": raw.get("caption_of"),
            "caption_block_ids": list(raw.get("caption_block_ids") or []),
        },
    }


def _raw_page_value(block: dict[str, Any]) -> Any:
    for key in ("page", "page_number", "page_idx", "page_id", "page_index"):
        if key in block:
            return {"field": key, "value": block[key], "declared_on": "block"}
    context = block.get("_marker_page_context")
    if isinstance(context, dict):
        public_context = {key: value for key, value in context.items() if key != "declarations"}
        return {**public_context, "declared_on": "page_container"}
    marker_id = metadata_text(block.get("id"))
    marker_page = marker_id_page_value(marker_id)
    if marker_page is not None:
        return {
            "field": "id",
            "value": marker_page,
            "scheme": "marker_id_page_segment:zero_based",
            "declared_on": "block_id",
        }
    return None


def _section_provenance(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    entries = list(blocks)
    first = entries[0] if entries else {}
    return {
        "block_id": first.get("block_id"),
        "parent_id": first.get("parent_id"),
        "source_order": [entry.get("source_order") for entry in entries],
        "page_scheme": [entry.get("page", {}).get("scheme") for entry in entries],
        "locator": [entry.get("locator") for entry in entries],
        "owner": [entry.get("owner") for entry in entries],
        "blocks": entries,
    }



def _section_page_range(entries: list[dict[str, Any]]) -> tuple[int | None, int | None, bool]:
    """Return an exact page extent only when every contributing block is locatable."""
    if not entries:
        return None, None, False
    pages: list[int] = []
    for entry in entries:
        page = entry.get("page")
        number = page.get("normalized") if isinstance(page, dict) else None
        if type(number) is not int or number < 1:
            return None, None, False
        pages.append(number)
    return min(pages), max(pages), True

def _components_from_blocks(
    *,
    paper_id: str,
    source_ref: str,
    blocks: list[dict[str, Any]],
    page_rects_by_number: dict[int, Any],
    page_rotations_by_number: dict[int, int],
    output_root: Path,
    figures_dir: Path,
    tables_dir: Path,
    warnings: list[str],
    warning_codes: list[dict[str, Any]],
) -> _Components:
    components = _Components()
    current_title = "Document"
    current_level = 1
    current_parts: list[str] = []
    current_provenance: list[dict[str, Any]] = []
    section_index = 0

    def flush_section() -> None:
        nonlocal current_parts, current_title, current_level, current_provenance, section_index
        text = "\n\n".join(part for part in current_parts if part.strip()).strip()
        if not text:
            current_parts = []
            current_provenance = []
            return
        page_start, page_end, page_range_complete = _section_page_range(current_provenance)
        locator = source_locator(source_ref, page=page_start)
        components.sections.append(ResearchSection(
            section_id=build_stable_id("sec", paper_id, "marker", current_title, str(section_index)),
            title=current_title,
            level=max(1, current_level),
            text=text,
            page_start=page_start,
            page_end=page_end,
            source_ref=locator,
            metadata={
                "parse_source": "marker",
                "source_locator": locator,
                "page_range_complete": page_range_complete,
                "marker_provenance": _section_provenance(current_provenance),
            },
        ))
        section_index += 1
        current_parts = []
        current_provenance = []

    for index, block in enumerate(blocks):
        kind = _block_kind(block)
        if _is_group_child_element(block):
            continue
        geometry = map_block_geometry(
            block,
            page_rects=page_rects_by_number,
            page_rotations=page_rotations_by_number,
        )
        page = geometry.page.page_number
        pdf_rect = geometry.pdf_rect
        locator = source_locator(source_ref, page=page, pdf_rect=pdf_rect)
        if kind == "heading":
            text = _block_text(block)
            if not text:
                continue
            flush_section()
            current_title = text[:160]
            current_level = _heading_level(block)
            current_provenance.append(_block_provenance(block, page=page, locator=locator))
            continue
        if kind == "text":
            text = _block_text(block)
            if not text:
                continue
            current_parts.append(text)
            current_provenance.append(_block_provenance(block, page=page, locator=locator))
            continue
        if kind == "equation":
            latex = _block_equation_text(block)
            if latex:
                components.equations.append(ResearchEquation(
                    equation_id=build_stable_id("eq", paper_id, "marker", str(index), latex[:80]),
                    latex=latex,
                    source_ref=locator,
                    page=page,
                    metadata={
                        "parse_source": "marker",
                        "source_locator": locator,
                        "pdf_rect": list(pdf_rect) if pdf_rect else None,
                        "marker_geometry": geometry.audit_dict(),
                        "marker_block": _compact_block_metadata(block),
                        "marker_provenance": _block_provenance(block, page=page, locator=locator),
                    },
                ))
            else:
                warnings.append(f"block_{index}: equation block has no usable source content")
                warning_codes.append({"code": "formula_source_missing", "block_index": index})
            continue
        if kind == "figure":
            caption = _block_caption(block) or f"Figure from page {page or '?'}"
            image_ref = _copy_block_asset(block, output_root, figures_dir)
            components.figures.append(ResearchFigure(
                figure_id=build_stable_id("fig", paper_id, "marker", str(index), caption),
                caption=caption[:500],
                source_ref=locator,
                image_ref=image_ref,
                page=page,
                metadata={
                    "parse_source": "marker",
                    "source_locator": locator,
                    "pdf_rect": list(pdf_rect) if pdf_rect else None,
                    "marker_geometry": geometry.audit_dict(),
                    "marker_block": _compact_block_metadata(block),
                    "marker_provenance": _block_provenance(block, page=page, locator=locator),
                    **({"image_ref": image_ref} if image_ref else {}),
                },
            ))
            continue
        if kind == "table":
            caption = _block_caption(block) or f"Table from page {page or '?'}"
            columns, rows = _table_rows(block)
            table_id = build_stable_id("tbl", paper_id, "marker", str(index), caption)
            marker_provenance = _block_provenance(block, page=page, locator=locator)
            marker_block_id = str(marker_provenance.get("block_id") or table_id)
            table_grid = build_marker_table_grid(
                block,
                table_id=table_id,
                block_id=marker_block_id,
                source_locator=locator,
            )
            if table_grid is None:
                warnings.append(f"block_{index}: table grid unavailable or invalid")
                warning_codes.append({"code": "table_grid_unavailable", "block_index": index})
            image_ref = _copy_block_asset(block, output_root, tables_dir)
            components.tables.append(ResearchTable(
                table_id=table_id,
                caption=caption[:500],
                source_ref=locator,
                columns=columns,
                rows=rows,
                page=page,
                grid=table_grid,
                metadata={
                    "parse_source": "marker",
                    "source_locator": locator,
                    "pdf_rect": list(pdf_rect) if pdf_rect else None,
                    "marker_geometry": geometry.audit_dict(),
                    "table_structure_source": "marker_json",
                    "table_grid_status": "available" if table_grid is not None else "unavailable",
                    "marker_block": _compact_block_metadata(block),
                    "marker_provenance": marker_provenance,
                    **({"image_ref": image_ref} if image_ref else {}),
                },
            ))
            continue
        if kind in _IGNORED_BLOCK_KINDS:
            continue
        warnings.append(f"block_{index}: unsupported Marker type {kind!r}")
        warning_codes.append({"code": "unsupported_marker_type", "block_index": index})

    flush_section()
    if not components.sections:
        text = "\n\n".join(_block_text(block) for block in blocks if _block_text(block)).strip()
        if text:
            locator = source_locator(source_ref, page=None)
            components.sections.append(ResearchSection(
                section_id=build_stable_id("sec", paper_id, "marker", "fallback"),
                title="Document",
                level=1,
                text=text,
                source_ref=locator,
                metadata={"parse_source": "marker", "source_locator": locator},
            ))
    return components


def _components_from_markdown(*, paper_id: str, source_ref: str, markdown: str) -> _Components:
    components = _Components()
    matches = list(_MARKDOWN_HEADING_RE.finditer(markdown))
    preamble = markdown[: matches[0].start()].strip() if matches else markdown.strip()
    if preamble:
        components.sections.append(ResearchSection(
            section_id=build_stable_id("sec", paper_id, "marker", "preamble"),
            title="Document",
            level=1,
            text=preamble,
            source_ref=source_ref,
            metadata={"parse_source": "marker", "source_locator": source_ref},
        ))
    for index, match in enumerate(matches):
        title = match.group(2).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        body = markdown[start:end].strip()
        if not body:
            continue
        components.sections.append(ResearchSection(
            section_id=build_stable_id("sec", paper_id, "marker", title, str(index)),
            title=title,
            level=len(match.group(1)),
            text=body,
            source_ref=source_ref,
            metadata={"parse_source": "marker", "source_locator": source_ref},
        ))
    return components


_MARKDOWN_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")


def _block_kind(block: dict[str, Any]) -> str:
    raw = (
        block.get("type")
        or block.get("block_type")
        or block.get("blockType")
        or block.get("category")
        or ""
    )
    value = str(raw).strip().lower().replace("-", "_")
    aliases = {
        "caption": "caption",
        "code": "text",
        "document": "ignored",
        "equation": "equation",
        "figure": "figure",
        "figuregroup": "figure",
        "form": "ignored",
        "handwriting": "text",
        "heading": "heading",
        "image": "figure",
        "line": "text",
        "list": "text",
        "listgroup": "text",
        "page": "ignored",
        "pagefooter": "ignored",
        "pageheader": "ignored",
        "pagenumber": "ignored",
        "picture": "figure",
        "picturegroup": "figure",
        "sectionheader": "heading",
        "table": "table",
        "tablecell": "ignored",
        "tablegroup": "table",
        "tableofcontents": "ignored",
        "text": "text",
        "textinlineequation": "text",
        "textinlinemath": "text",
        "title": "heading",
    }
    return aliases.get(value, value or "text")


_IGNORED_BLOCK_KINDS = {"caption", "ignored", "pagefooter", "pageheader", "pagenumber"}


def _is_group_child_element(block: dict[str, Any]) -> bool:
    kind = _block_kind(block)
    parent_kind = metadata_text(block.get("_marker_parent_kind"))
    if parent_kind == "table" and kind == "table":
        return True
    if parent_kind == "figure" and kind == "figure":
        return True
    return False


def _page_number(block: dict[str, Any], *, page_count: int = 2**31 - 1) -> int | None:
    # Preserve the explicitly normalized value for provenance callers; the typed
    # resolution reason still marks out-of-range values non-scoreable. Production
    # geometry and locators use map_block_geometry's page-count-aware result.
    return resolve_page(block, page_count=page_count).page_number


def _block_pdf_rect(
    block: dict[str, Any],
    rects: dict[int, Any],
) -> tuple[float, float, float, float] | None:
    return map_block_geometry(block, page_rects=rects).pdf_rect


def _block_text(block: dict[str, Any]) -> str:
    return _html_to_text(metadata_text(
        block.get("text")
        or block.get("content")
        or block.get("html")
        or block.get("markdown")
        or block.get("md")
    ))


def _block_equation_text(block: dict[str, Any]) -> str:
    for source_field in ("latex", "tex"):
        raw_formula = block.get(source_field)
        if isinstance(raw_formula, str) and raw_formula.strip():
            return raw_formula
    return _html_to_text(metadata_text(
        block.get("text")
        or block.get("content")
        or block.get("html")
    ))


def _block_caption(block: dict[str, Any]) -> str:
    direct = metadata_text(
        block.get("caption")
        or block.get("caption_text")
        or block.get("image_caption")
        or block.get("table_caption")
    )
    if direct:
        return _strip_caption_prefix(direct)
    children = block.get("children") or block.get("blocks") or []
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            if _block_kind(child) == "caption":
                text = _block_text(child)
                if text:
                    return _strip_caption_prefix(text)
    return ""


def _strip_caption_prefix(value: str) -> str:
    return _html_to_text(value)


def _table_rows(block: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    for table_block in _candidate_table_blocks(block):
        rows_value = table_block.get("table_rows") or table_block.get("rows")
        if isinstance(rows_value, list) and rows_value:
            if all(isinstance(row, dict) for row in rows_value):
                columns: list[str] = []
                for row in rows_value:
                    for key in row:
                        if str(key) not in columns:
                            columns.append(str(key))
                return columns, [
                    {column: row[column] if column in row else None for column in columns}
                    for row in rows_value
                    if isinstance(row, dict)
                ]
            if all(isinstance(row, list) for row in rows_value):
                matrix = [[metadata_text(cell) for cell in row] for row in rows_value]
                return _matrix_to_rows(matrix)
        html = metadata_text(
            table_block.get("html")
            or table_block.get("table_html")
            or table_block.get("table_body")
            or table_block.get("content")
        )
        if "<table" in html.lower():
            columns, rows = _html_table_rows(html)
            if columns or rows:
                return columns, rows
        if "|" in html:
            matrix = [
                [cell.strip() for cell in line.strip("|").split("|")]
                for line in html.splitlines()
                if "|" in line
            ]
            return _matrix_to_rows(matrix)
    return [], []


def _candidate_table_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    out = [block] if _block_kind(block) == "table" else []
    for child in _block_children(block):
        if _block_kind(child) == "table":
            out.append(child)
    return out or [block]


def _block_children(block: dict[str, Any]) -> list[dict[str, Any]]:
    children = block.get("children") or block.get("blocks") or block.get("items") or []
    if not isinstance(children, list):
        return []
    return [child for child in children if isinstance(child, dict)]


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        if tag in {"br", "p", "div", "tr"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"} and self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if self._suppressed_depth:
            return
        if tag in {"p", "div", "tr", "table"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    if "<" not in value or ">" not in value:
        return _normalize_text(value)
    parser = _TextHTMLParser()
    parser.feed(value)
    return _normalize_text(" ".join(parser.parts))


class _TableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._in_cell = False
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            self._suppressed_depth += 1
            return
        if self._suppressed_depth:
            return
        if tag == "tr":
            self._row = []
            return
        if tag in {"td", "th"} and self._row is not None:
            self._cell_parts = []
            self._in_cell = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"} and self._suppressed_depth:
            self._suppressed_depth -= 1
            return
        if self._suppressed_depth:
            return
        if tag in {"td", "th"} and self._row is not None and self._cell_parts is not None:
            self._row.append(_normalize_text(" ".join(self._cell_parts)))
            self._cell_parts = None
            self._in_cell = False
            return
        if tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth and self._in_cell and self._cell_parts is not None:
            self._cell_parts.append(data)


def _html_table_rows(html: str) -> tuple[list[str], list[dict[str, Any]]]:
    parser = _TableHTMLParser()
    parser.feed(html)
    return _matrix_to_rows(parser.rows)


def _normalize_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", normalized)


def _matrix_to_rows(matrix: list[list[str]]) -> tuple[list[str], list[dict[str, Any]]]:
    """Build the legacy row projection without shifting or dropping source slots."""
    if not matrix:
        return [], []
    width = max((len(row) for row in matrix), default=0)
    if width == 0:
        return [], []
    header = list(matrix[0])
    columns = _unique_columns(
        header + [f"column_{index + 1}" for index in range(len(header), width)]
    )
    rows: list[dict[str, Any]] = []
    for row in matrix[1:]:
        # None marks a source coordinate that was absent; "" is an explicit
        # blank cell. Consumers may render both as empty, but storage keeps them distinct.
        padded: list[Any] = list(row) + [None] * max(0, width - len(row))
        rows.append(dict(zip(columns, padded[: len(columns)])))
    return columns, rows


def _unique_columns(values: list[str]) -> list[str]:
    names = [value.strip() or f"column_{index + 1}" for index, value in enumerate(values)]
    reserved = set(names)
    used: set[str] = set()
    counts: dict[str, int] = {}
    out: list[str] = []
    for name in names:
        candidate = name
        if candidate in used:
            counter = counts.get(name, 1) + 1
            candidate = f"{name}_{counter}"
            while candidate in used or candidate in reserved:
                counter += 1
                candidate = f"{name}_{counter}"
            counts[name] = counter
        used.add(candidate)
        out.append(candidate)
    return out


def _copy_block_asset(block: dict[str, Any], output_root: Path, target_dir: Path) -> str | None:
    for asset_block in _candidate_asset_blocks(block):
        raw = metadata_text(
            asset_block.get("image_path")
            or asset_block.get("img_path")
            or asset_block.get("path")
            or asset_block.get("file_path")
            or asset_block.get("table_img_path")
        )
        if raw:
            copied = _copy_marker_path_asset(raw, output_root, target_dir)
            if copied:
                return copied
        for name, raw_image in _iter_marker_images(asset_block):
            written = _write_base64_marker_image(
                raw_image,
                target_dir=target_dir,
                stem=safe_paper_id(name or metadata_text(asset_block.get("id")) or "marker_image"),
            )
            if written:
                return written
            copied = _copy_marker_path_asset(metadata_text(raw_image), output_root, target_dir)
            if copied:
                return copied
    return None


def _candidate_asset_blocks(block: dict[str, Any]) -> list[dict[str, Any]]:
    out = [block]
    out.extend(_block_children(block))
    return out


def _iter_marker_images(block: dict[str, Any]) -> list[tuple[str, Any]]:
    images = block.get("images")
    if isinstance(images, dict):
        return [(metadata_text(key), value) for key, value in images.items()]
    if isinstance(images, list):
        return [(f"image_{index}", value) for index, value in enumerate(images)]
    return []


def _marker_artifact_root() -> Path:
    root = Path(os.environ.get("NEWS_ARTIFACT_ROOT", ".newsroom/runs"))
    # The configured storage parent is trusted; parser-controlled descendants are not.
    return root.parent.resolve() / "papers"


def _copy_marker_path_asset(raw: str, output_root: Path, target_dir: Path) -> str | None:
    return copy_marker_image(
        raw, output_root=output_root, target_dir=target_dir,
        artifact_root=_marker_artifact_root(),
    )


def _write_base64_marker_image(raw: Any, *, target_dir: Path, stem: str) -> str | None:
    # Names from Marker are deliberately not used as filesystem paths.
    return write_embedded_marker_image(
        metadata_text(raw), target_dir=target_dir, artifact_root=_marker_artifact_root(),
    )


def _paper_artifact_dir(paper_id: str) -> Path:
    name = safe_paper_id(paper_id)
    if name in {"", ".", ".."}:
        name = "paper-" + sha256(paper_id.encode("utf-8")).hexdigest()
    return _marker_artifact_root() / name


def _compact_block_metadata(block: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "type",
        "block_type",
        "page",
        "page_idx",
        "page_id",
        "bbox",
        "box",
        "polygon",
        "heading_level",
        "level",
        "image_path",
        "img_path",
        "table_img_path",
    )
    return {key: block[key] for key in keys if key in block}


def _parse_quality(
    sections: list[ResearchSection],
    figures: list[ResearchFigure],
    tables: list[ResearchTable],
    equations: list[ResearchEquation],
) -> dict[str, Any]:
    return {
        "sections": {
            "total": len(sections),
            "with_page_bounds": sum(1 for item in sections if item.page_start is not None),
            "with_source_locator": sum(1 for item in sections if item.metadata.get("source_locator")),
        },
        "figures": {
            "total": len(figures),
            "with_image": sum(1 for item in figures if item.image_ref),
            "with_page": sum(1 for item in figures if item.page is not None),
            "with_bbox": sum(1 for item in figures if item.metadata.get("pdf_rect")),
            "with_source_locator": sum(1 for item in figures if item.metadata.get("source_locator")),
        },
        "tables": {
            "total": len(tables),
            "with_rows": sum(1 for item in tables if item.rows),
            "with_image": sum(1 for item in tables if item.metadata.get("image_ref")),
            "with_bbox": sum(1 for item in tables if item.metadata.get("pdf_rect")),
            "with_source_locator": sum(1 for item in tables if item.metadata.get("source_locator")),
        },
        "equations": {
            "total": len(equations),
            "with_page": sum(1 for item in equations if item.page is not None),
            "with_bbox": sum(1 for item in equations if item.metadata.get("pdf_rect")),
            "with_source_locator": sum(1 for item in equations if item.metadata.get("source_locator")),
        },
    }


def _redacted_command(command: list[str]) -> list[str]:
    return [
        "<repo-path>"
        if any(marker in part for marker in ("NewsRoom", "Agora-Hub", "Agora Hub"))
        else part
        for part in command
    ]


__all__ = ["MarkerPdfDocumentParser"]
