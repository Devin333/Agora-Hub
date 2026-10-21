from __future__ import annotations

from types import SimpleNamespace

import pymupdf
import pytest

from backend.research.document.marker_geometry import (
    MARKER_PAGE_CONTAINER_ROTATED_SCHEMA,
    MARKER_PAGE_CONTAINER_SCHEMA,
    MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA,
    PageConvention,
    map_block_geometry,
    marker_id_page_value,
    pdf_page_rotations,
    resolve_page,
)


def _block(page_fields: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    return {**(page_fields or {}), **extra}


def test_page_conventions_are_explicit_and_marker_id_is_zero_based() -> None:
    cases = [
        ({"page": 2}, 2, PageConvention.ONE_BASED_PAGE),
        ({"page_number": 2}, 2, PageConvention.ONE_BASED_PAGE_NUMBER),
        ({"page_idx": 1}, 2, PageConvention.ZERO_BASED_PAGE_IDX),
        ({"page_index": 1}, 2, PageConvention.ZERO_BASED_PAGE_INDEX),
        ({"page_id": 1}, 2, PageConvention.ZERO_BASED_PAGE_ID),
        ({"id": "document/page/1/text/4"}, 2, PageConvention.ZERO_BASED_MARKER_ID),
    ]
    for declared, expected_page, expected_convention in cases:
        result = resolve_page(declared, page_count=3)
        assert result.page_number == expected_page
        assert result.convention is expected_convention
        assert result.scoreable
    assert marker_id_page_value("document/page/1/text/4") == 1
    assert marker_id_page_value("not-a-page-id") is None


@pytest.mark.parametrize("field,value", [
    ("page", True), ("page_idx", 1.0), ("page_index", "0"), ("page_id", "2"),
])
def test_page_declarations_reject_non_strict_integers(field: str, value: object) -> None:
    result = resolve_page({field: value}, page_count=4)
    assert result.page_number is None
    assert not result.scoreable
    assert result.reason is not None and "not_strict_integer" in result.reason


def test_page_conflicts_parent_conflicts_and_out_of_range_are_unknown() -> None:
    conflicting = resolve_page({"page": 2, "page_idx": 0}, page_count=4)
    assert conflicting.page_number is None
    assert conflicting.reason == "conflicting_page_declarations"

    parent_conflict = resolve_page({
        "page_idx": 1,
        "_marker_page_context": {
            "field": "page_idx", "value": 0, "scheme": "page_idx:zero_based"
        },
    }, page_count=4)
    assert parent_conflict.page_number is None
    assert parent_conflict.reason == "conflicting_page_declarations"

    negative = resolve_page({"page_idx": -4}, page_count=4)
    assert negative.page_number == -3  # Explicit conversion retained for audit, never scoreable.
    assert negative.reason == "page_out_of_range"
    assert not negative.scoreable


@pytest.mark.parametrize(
    ("rotation", "physical_rect", "expected"),
    [
        (90, (5, 7, 105, 207), (45, 27, 95, 127)),
        (180, (5, 7, 205, 107), (85, 47, 185, 97)),
        (270, (5, 7, 105, 207), (15, 87, 65, 187)),
    ],
)
def test_unrotated_page_container_bbox_is_rotated_explicitly(
    rotation: int,
    physical_rect: tuple[int, int, int, int],
    expected: tuple[int, int, int, int],
) -> None:
    physical_page = SimpleNamespace(x0=physical_rect[0], y0=physical_rect[1],
                                    x1=physical_rect[2], y1=physical_rect[3])
    block = {
        "page_idx": 0,
        "bbox": [20, 10, 120, 60],
        "_marker_page_context": {
            "field": "page_idx",
            "value": 0,
            "scheme": "page_idx:zero_based",
            "coordinate_schema": MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA,
            "page_bbox": [0, 0, 200, 100],
        },
    }
    result = map_block_geometry(
        block,
        page_rects={1: physical_page},
        page_rotations={1: rotation},
    )
    assert result.status == "mapped"
    assert result.pdf_rect == pytest.approx(expected)
    assert result.rotation_degrees == rotation
    assert result.audit_dict()["coordinate_schema"] == MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA


def test_scaled_unrotated_page_container_bbox_maps_with_explicit_factors() -> None:
    physical_page = SimpleNamespace(x0=10, y0=20, x1=610, y1=820)
    block = {
        "page": 1,
        "bbox": [300, 600, 900, 1400],
        "coordinate_schema": MARKER_PAGE_CONTAINER_SCHEMA,
        "page_bbox": [100, 200, 1300, 1800],
    }
    result = map_block_geometry(
        block,
        page_rects={1: physical_page},
        page_rotations={1: 0},
    )
    assert result.status == "mapped"
    assert result.pdf_rect == pytest.approx((110, 220, 410, 620))
    assert result.conversion_factors == pytest.approx((0.5, 0.5))
    assert result.rotation_degrees == 0


def test_rotated_geometry_fails_closed_without_source_orientation_contract() -> None:
    physical_page = SimpleNamespace(x0=0, y0=0, x1=800, y1=600)
    block = {
        "page": 1,
        "bbox": [20, 10, 120, 60],
        "coordinate_schema": MARKER_PAGE_CONTAINER_SCHEMA,
        "page_bbox": [0, 0, 200, 100],
    }
    result = map_block_geometry(block, page_rects={1: physical_page}, page_rotations={1: 90})
    assert result.status == "unknown"
    assert result.pdf_rect is None
    assert result.reason == "rotation_coordinate_orientation_unknown"


def test_explicitly_rotated_source_bbox_is_scaled_without_second_rotation() -> None:
    physical_page = SimpleNamespace(x0=0, y0=0, x1=100, y1=200)
    block = {
        "page": 1,
        "bbox": [20, 20, 60, 120],
        "coordinate_schema": MARKER_PAGE_CONTAINER_ROTATED_SCHEMA,
        "page_bbox": [0, 0, 100, 200],
    }
    result = map_block_geometry(block, page_rects={1: physical_page}, page_rotations={1: 90})
    assert result.status == "mapped"
    assert result.pdf_rect == pytest.approx((20, 20, 60, 120))
    assert result.rotation_degrees == 90


def test_pdf_page_rotations_reads_physical_rotation_instead_of_inferring_from_bbox() -> None:
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.set_rotation(270)
    payload = document.tobytes()
    document.close()

    assert pdf_page_rotations(payload) == {1: 270}


def test_unknown_geometry_never_guesses_coordinates() -> None:
    physical = SimpleNamespace(x0=0, y0=0, x1=600, y1=800, rotation=0)
    base = {"page": 1, "bbox": [0, 0, 100, 100]}
    cases = [
        ({**base}, "unrecognized_coordinate_schema"),
        ({**base, "coordinate_schema": MARKER_PAGE_CONTAINER_SCHEMA}, "page_container_source_rect_missing_or_invalid"),
        ({**base, "coordinate_schema": MARKER_PAGE_CONTAINER_SCHEMA,
          "page_bbox": [0, 0, 100, 0]}, "page_container_source_rect_missing_or_invalid"),
        ({"page_idx": -1, "bbox": [0, 0, 100, 100], "coordinate_schema": MARKER_PAGE_CONTAINER_SCHEMA,
          "page_bbox": [0, 0, 100, 100]}, "unknown_page"),
    ]
    for block, reason in cases:
        result = map_block_geometry(block, page_rects={1: physical}, page_rotations={1: 0})
        assert result.pdf_rect is None
        assert result.status == "unknown"
        assert result.reason == reason
        assert result.audit_dict()["pdf_rect"] is None



def test_declared_page_rotation_must_match_pdf_rotation() -> None:
    physical_page = SimpleNamespace(x0=0, y0=0, x1=800, y1=600)
    block = {
        "page": 1,
        "bbox": [20, 10, 120, 60],
        "coordinate_schema": MARKER_PAGE_CONTAINER_UNROTATED_SCHEMA,
        "page_bbox": [0, 0, 200, 100],
        "page_rotation": 270,
    }

    result = map_block_geometry(block, page_rects={1: physical_page}, page_rotations={1: 90})

    assert result.status == "unknown"
    assert result.pdf_rect is None
    assert result.reason == "declared_page_rotation_mismatch"
