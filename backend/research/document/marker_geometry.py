from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from backend.research.document.docker_pdf_parser import coerce_rect
from backend.research.document.pymupdf_loader import load_pymupdf


class PageConvention(StrEnum):
    ONE_BASED_PAGE = "page:one_based"
    ONE_BASED_PAGE_NUMBER = "page_number:one_based"
    ZERO_BASED_PAGE_IDX = "page_idx:zero_based"
    ZERO_BASED_PAGE_INDEX = "page_index:zero_based"
    ZERO_BASED_PAGE_ID = "page_id:zero_based"
    ZERO_BASED_MARKER_ID = "marker_id_page_segment:zero_based"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class PageResolution:
    page_number: int | None
    convention: PageConvention
    raw: Any
    reason: str | None = None

    @property
    def scoreable(self) -> bool:
        return self.page_number is not None and self.reason is None


@dataclass(frozen=True, slots=True)
class GeometryMapping:
    pdf_rect: tuple[float, float, float, float] | None
    page: PageResolution
    status: str
    reason: str | None
    coordinate_schema: str | None
    source_rect: tuple[float, float, float, float] | None
    source_coordinate_system: str | None
    conversion_factors: tuple[float, float] | None
    transform_matrix: tuple[float, float, float, float, float, float] | None
    rotation_degrees: int | None
    physical_page_rect: tuple[float, float, float, float] | None

    def audit_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "page": {
                "number": self.page.page_number,
                "convention": self.page.convention.value,
                "raw": self.page.raw,
                "reason": self.page.reason,
            },
            "coordinate_schema": self.coordinate_schema,
            "source_coordinate_system": self.source_coordinate_system,
            "source_rect": list(self.source_rect) if self.source_rect else None,
            "pdf_rect": list(self.pdf_rect) if self.pdf_rect else None,
            "conversion_factors": list(self.conversion_factors) if self.conversion_factors else None,
            "transform_matrix": list(self.transform_matrix) if self.transform_matrix else None,
            "rotation_degrees": self.rotation_degrees,
            "physical_page_rect": list(self.physical_page_rect) if self.physical_page_rect else None,
        }


_MARKER_PAGE_ID_RE = re.compile(r"(?:^|/)page/(\d+)(?:/|$)", re.IGNORECASE)
_PAGE_FIELDS: tuple[tuple[str, PageConvention, bool], ...] = (
    ("page", PageConvention.ONE_BASED_PAGE, True),
    ("page_number", PageConvention.ONE_BASED_PAGE_NUMBER, True),
    ("page_idx", PageConvention.ZERO_BASED_PAGE_IDX, False),
    ("page_index", PageConvention.ZERO_BASED_PAGE_INDEX, False),
    ("page_id", PageConvention.ZERO_BASED_PAGE_ID, False),
)
MARKER_PAGE_CONTAINER_SCHEMA = "marker_page_container_bbox_v1"
MARKER_PAGE_CONTAINER_ROTATED_SCHEMA = "marker_page_container_bbox_rotated_v1"
MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA = "marker_page_container_bbox_unrotated_v1"
_MARKER_PAGE_CONTAINER_SCHEMAS = {
    MARKER_PAGE_CONTAINER_SCHEMA,
    MARKER_PAGE_CONTAINER_ROTATED_SCHEMA,
    MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA,
}


def resolve_page(block: Mapping[str, Any], *, page_count: int) -> PageResolution:
    """Resolve page declarations without coercion, precedence guessing, or clamping."""
    candidates: list[tuple[int, PageConvention, str, Any]] = []
    invalid: list[tuple[str, Any, str]] = []
    context = block.get("_marker_page_context")

    for key, convention, one_based in _PAGE_FIELDS:
        if key in block:
            value = block[key]
            if type(value) is not int:
                invalid.append((key, value, "not_strict_integer"))
            else:
                candidates.append((value if one_based else value + 1, convention, key, value))

    if isinstance(context, Mapping):
        declarations = context.get("declarations")
        if not isinstance(declarations, list):
            declarations = [context] if "field" in context else []
        for declaration in declarations:
            if not isinstance(declaration, Mapping):
                invalid.append(("parent", declaration, "invalid_parent_page_declaration"))
                continue
            key = declaration.get("field")
            value = declaration.get("value")
            convention = (
                PageConvention.ZERO_BASED_MARKER_ID
                if key == "id" and declaration.get("scheme") == PageConvention.ZERO_BASED_MARKER_ID.value
                else _convention_for_field(key)
            )
            if convention is None:
                invalid.append((str(key), value, "unknown_parent_page_convention"))
            elif type(value) is not int:
                invalid.append((str(key), value, "not_strict_integer"))
            else:
                one_based = convention in {PageConvention.ONE_BASED_PAGE, PageConvention.ONE_BASED_PAGE_NUMBER}
                candidates.append((value if one_based else value + 1, convention, str(key), value))

    marker_id = block.get("id")
    match = _MARKER_PAGE_ID_RE.search(marker_id) if isinstance(marker_id, str) else None
    if match:
        raw_id_page = int(match.group(1))
        candidates.append((raw_id_page + 1, PageConvention.ZERO_BASED_MARKER_ID, "id", raw_id_page))

    raw = [{"field": field, "value": value, "normalized": number, "convention": conv.value}
           for number, conv, field, value in candidates]
    raw.extend({"field": field, "value": value, "reason": reason} for field, value, reason in invalid)
    if invalid:
        first = invalid[0]
        return PageResolution(None, PageConvention.UNKNOWN, raw, f"invalid_page_declaration:{first[0]}:{first[2]}")
    if not candidates:
        return PageResolution(None, PageConvention.UNKNOWN, None, "page_not_declared")
    values = {entry[0] for entry in candidates}
    if len(values) != 1:
        return PageResolution(None, PageConvention.UNKNOWN, raw, "conflicting_page_declarations")
    page_number = candidates[0][0]
    if page_number < 1 or page_number > page_count:
        return PageResolution(page_number, candidates[0][1], raw, "page_out_of_range")
    if len(candidates) > 1:
        # Equal redundant declarations are valid; retain all conventions in raw audit.
        convention = candidates[0][1]
    else:
        convention = candidates[0][1]
    return PageResolution(page_number, convention, raw, None)


def marker_id_page_value(value: Any) -> int | None:
    """Return Marker id's explicitly zero-based page segment, if well-formed."""
    match = _MARKER_PAGE_ID_RE.search(value) if isinstance(value, str) else None
    return int(match.group(1)) if match else None


def pdf_page_rotations(pdf_bytes: bytes) -> dict[int, int]:
    """Read the PDF's physical page rotations; never infer them from bbox sizes."""
    document = load_pymupdf().open(stream=pdf_bytes, filetype="pdf")
    try:
        return {index: int(page.rotation) % 360 for index, page in enumerate(document, start=1)}
    finally:
        document.close()


def map_block_geometry(
    block: Mapping[str, Any],
    *,
    page_rects: Mapping[int, Any],
    page_rotations: Mapping[int, int] | None = None,
) -> GeometryMapping:
    page = resolve_page(block, page_count=len(page_rects))
    schema, source_rect_value, rotation_value = _schema_context(block)
    if not page.scoreable:
        return _unknown(page, "unknown_page", schema=schema)
    physical = page_rects.get(page.page_number)
    physical_rect = _rect_tuple(physical)
    physical_rotation = (page_rotations or {}).get(page.page_number)
    if physical_rotation is None:
        physical_rotation = getattr(physical, "rotation", None)
    rotation = _rotation(physical_rotation)
    if physical_rect is None:
        return _unknown(page, "physical_page_rect_missing_or_invalid", schema=schema, rotation=rotation)
    if rotation_value is not None:
        declared_rotation = _rotation(rotation_value)
        if declared_rotation is None:
            return _unknown(page, "declared_page_rotation_invalid", schema=schema, rotation=rotation, physical=physical_rect)
        if rotation is not None and declared_rotation != rotation:
            return _unknown(page, "declared_page_rotation_mismatch", schema=schema, rotation=rotation, physical=physical_rect)
    bbox = _first_present(block, ("bbox", "box", "polygon", "poly"))
    source_rect = _rect_tuple(source_rect_value)
    if bbox is None:
        return _unknown(page, "bbox_missing", schema=schema, rotation=rotation, physical=physical_rect)
    if schema not in _MARKER_PAGE_CONTAINER_SCHEMAS:
        return _unknown(page, "unrecognized_coordinate_schema", schema=schema, rotation=rotation, physical=physical_rect)
    if rotation is None:
        return _unknown(page, "physical_page_rotation_missing_or_invalid", schema=schema, rotation=None, physical=physical_rect)
    if rotation and schema == MARKER_PAGE_CONTAINER_SCHEMA:
        return _unknown(page, "rotation_coordinate_orientation_unknown", schema=schema, rotation=rotation, physical=physical_rect)
    if source_rect is None:
        return _unknown(page, "page_container_source_rect_missing_or_invalid", schema=schema, rotation=rotation, physical=physical_rect)
    box = _rect_tuple(bbox)
    if box is None:
        return _unknown(page, "bbox_invalid", schema=schema, source=source_rect, rotation=rotation, physical=physical_rect)
    sx0, sy0, sx1, sy1 = source_rect
    px0, py0, px1, py1 = physical_rect
    source_width, source_height = sx1 - sx0, sy1 - sy0
    page_width, page_height = px1 - px0, py1 - py0
    if source_width <= 0 or source_height <= 0 or page_width <= 0 or page_height <= 0:
        return _unknown(page, "source_or_physical_rect_degenerate", schema=schema, source=source_rect, rotation=rotation, physical=physical_rect)
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0:
        return _unknown(page, "bbox_degenerate", schema=schema, source=source_rect, rotation=rotation, physical=physical_rect)
    if x0 < sx0 or y0 < sy0 or x1 > sx1 or y1 > sy1:
        return _unknown(page, "bbox_outside_page_container", schema=schema, source=source_rect, rotation=rotation, physical=physical_rect)
    u0, u1 = (x0 - sx0) / source_width, (x1 - sx0) / source_width
    v0, v1 = (y0 - sy0) / source_height, (y1 - sy0) / source_height
    if rotation and schema == MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA:
        if rotation == 90:
            corners = ((1 - v1, u0), (1 - v0, u0), (1 - v1, u1), (1 - v0, u1))
            factors = (-page_width / source_height, page_height / source_width)
        elif rotation == 180:
            corners = ((1 - u1, 1 - v1), (1 - u0, 1 - v1), (1 - u1, 1 - v0), (1 - u0, 1 - v0))
            factors = (-page_width / source_width, -page_height / source_height)
        else:  # 270 degrees clockwise
            corners = ((v0, 1 - u1), (v1, 1 - u1), (v0, 1 - u0), (v1, 1 - u0))
            factors = (page_width / source_height, -page_height / source_width)
    else:
        corners = ((u0, v0), (u1, v0), (u0, v1), (u1, v1))
        factors = (page_width / source_width, page_height / source_height)
    mapped_x = [px0 + u * page_width for u, _ in corners]
    mapped_y = [py0 + v * page_height for _, v in corners]
    mapped = (min(mapped_x), min(mapped_y), max(mapped_x), max(mapped_y))
    matrix = _affine_transform(
        schema=schema,
        rotation=rotation,
        source_rect=source_rect,
        physical_rect=physical_rect,
    )
    source_system = {
        MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA: "marker_page_container_bbox_unrotated",
        MARKER_PAGE_CONTAINER_ROTATED_SCHEMA: "marker_page_container_bbox_rotated",
        MARKER_PAGE_CONTAINER_SCHEMA: "marker_page_container_bbox_page_oriented",
    }[schema]
    return GeometryMapping(mapped, page, "mapped", None, schema, source_rect,
                           source_system, factors, matrix, rotation, physical_rect)


def _affine_transform(
    *,
    schema: str,
    rotation: int,
    source_rect: tuple[float, float, float, float],
    physical_rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float, float, float]:
    sx0, sy0, sx1, sy1 = source_rect
    px0, py0, px1, py1 = physical_rect
    source_width, source_height = sx1 - sx0, sy1 - sy0
    page_width, page_height = px1 - px0, py1 - py0
    if schema != MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA or rotation == 0:
        a, e = page_width / source_width, page_height / source_height
        return (a, 0.0, px0 - a * sx0, 0.0, e, py0 - e * sy0)
    if rotation == 90:
        b, d = -page_width / source_height, page_height / source_width
        return (0.0, b, px0 + page_width - b * sy0, d, 0.0, py0 - d * sx0)
    if rotation == 180:
        a, e = -page_width / source_width, -page_height / source_height
        return (a, 0.0, px0 + page_width - a * sx0, 0.0, e, py0 + page_height - e * sy0)
    b, d = page_width / source_height, -page_height / source_width
    return (0.0, b, px0 - b * sy0, d, 0.0, py0 + page_height - d * sx0)


def _schema_context(block: Mapping[str, Any]) -> tuple[str | None, Any, Any]:
    schema = block.get("coordinate_schema")
    source_rect = block.get("page_bbox")
    rotation = block.get("page_rotation")
    context = block.get("_marker_page_context")
    if isinstance(context, Mapping):
        schema = schema if schema is not None else context.get("coordinate_schema")
        source_rect = source_rect if source_rect is not None else context.get("page_bbox")
        rotation = rotation if rotation is not None else context.get("page_rotation")
    return schema if isinstance(schema, str) else None, source_rect, rotation


def _unknown(page: PageResolution, reason: str, *, schema: str | None,
             source: tuple[float, float, float, float] | None = None,
             rotation: int | None = None,
             physical: tuple[float, float, float, float] | None = None) -> GeometryMapping:
    return GeometryMapping(None, page, "unknown", reason, schema, source, None,
                           None, None, rotation, physical)


def _rect_tuple(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, dict) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        coerced = coerce_rect(value)
        if coerced is not None:
            values = coerced
        else:
            return None
    elif hasattr(value, "x0") and hasattr(value, "y0") and hasattr(value, "x1") and hasattr(value, "y1"):
        values = (value.x0, value.y0, value.x1, value.y1)
    else:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) for item in values):
        return None
    return tuple(float(item) for item in values)  # type: ignore[return-value]


def _rotation(value: Any) -> int | None:
    if type(value) is not int or value % 90:
        return None
    return value % 360


def _convention_for_field(field: Any) -> PageConvention | None:
    return next((convention for key, convention, _ in _PAGE_FIELDS if key == field), None)


def _first_present(block: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in block:
            return block[key]
    return None
