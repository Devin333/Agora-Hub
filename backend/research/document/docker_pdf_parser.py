from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf

from framework.execution_environment.errors import ExecutionEnvironmentUnavailableError


@dataclass(frozen=True)
class DockerParseRun:
    input_dir: Path
    output_dir: Path
    pdf_path: Path
    command: tuple[str, ...]


def parser_run_root() -> Path:
    configured = os.environ.get("NEWSROOM_PARSER_RUN_ROOT", "").strip()
    if configured:
        return Path(configured)
    return Path.cwd() / ".newsroom" / "parser-runs"


def safe_paper_id(paper_id: str) -> str:
    return re.sub(r"[^\w.\-]", "_", paper_id)


def stage_pdf_for_docker(
    *,
    backend: str,
    paper_id: str,
    pdf_bytes: bytes,
) -> tuple[Path, Path, Path]:
    run_dir = parser_run_root() / backend / safe_paper_id(paper_id)
    input_dir = run_dir / "input"
    output_dir = run_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = input_dir / "input.pdf"
    pdf_path.write_bytes(pdf_bytes)
    return input_dir, output_dir, pdf_path


def run_docker_command(
    command: list[str],
    *,
    timeout_seconds: int,
    execution_identity: Any | None = None,
    paper_id: str | None = None,
    backend: str | None = None,
) -> None:
    """Reject an unbound parser process invocation.

    Production composition injects the infrastructure execution adapter into
    the parser.  Keeping this compatibility symbol lets parser bake-off tests
    replace it with a fake runner without leaving a host-process escape hatch.
    """

    raise ExecutionEnvironmentUnavailableError(
        "Research parser execution requires a composed external-process adapter",
        details={
            "reason": "direct_parser_process_disabled",
            "paper_id": paper_id,
            "backend": backend,
            "execution_identity_present": execution_identity is not None,
            "timeout_seconds": timeout_seconds,
            "command_length": len(command),
        },
    )


def source_locator(source_ref: str, *, page: int | None, pdf_rect: Any = None) -> str:
    locator = source_ref
    if page is not None:
        locator = f"{locator}#page={page}"
    rect = coerce_rect(pdf_rect)
    if rect is not None:
        separator = "&" if "#" in locator else "#"
        locator = f"{locator}{separator}pdf_rect={format_rect(rect)}"
    return locator


def format_rect(rect: tuple[float, float, float, float]) -> str:
    return ",".join(f"{value:.2f}".rstrip("0").rstrip(".") for value in rect)


def coerce_rect(value: Any) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = [
            value.get("x0", value.get("left")),
            value.get("y0", value.get("top")),
            value.get("x1", value.get("right")),
            value.get("y1", value.get("bottom")),
        ]
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(not isinstance(item, (list, tuple, dict)) for item in value):
        values = value
    else:
        points = _polygon_points(value)
        if len(points) < 2:
            return None
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        values = [min(xs), min(ys), max(xs), max(ys)]
    try:
        rect = tuple(float(v) for v in values[:4])
    except (TypeError, ValueError):
        return None
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _polygon_points(value: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    if not isinstance(value, (list, tuple)):
        return points
    for item in value:
        if isinstance(item, dict):
            raw_x, raw_y = item.get("x"), item.get("y")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            raw_x, raw_y = item[0], item[1]
        else:
            continue
        try:
            points.append((float(raw_x), float(raw_y)))
        except (TypeError, ValueError):
            continue
    return points


def rect_to_pdf_points(
    rect: Any,
    *,
    page_rect: pymupdf.Rect,
) -> tuple[float, float, float, float] | None:
    box = coerce_rect(rect)
    if box is None:
        return None
    x0, y0, x1, y1 = box
    max_value = max(abs(value) for value in box)
    if max_value <= 1.5:
        return (
            x0 * page_rect.width,
            y0 * page_rect.height,
            x1 * page_rect.width,
            y1 * page_rect.height,
        )
    if max_value <= 1000.0:
        return (
            x0 / 1000.0 * page_rect.width,
            y0 / 1000.0 * page_rect.height,
            x1 / 1000.0 * page_rect.width,
            y1 / 1000.0 * page_rect.height,
        )
    return box


def page_rects(pdf_bytes: bytes) -> dict[int, pymupdf.Rect]:
    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return {index: page.rect for index, page in enumerate(document, start=1)}
    finally:
        document.close()


def first_existing_file(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        direct = root / name
        if direct.exists():
            return direct
    for name in names:
        matches = list(root.rglob(name))
        if matches:
            return matches[0]
    return None


def metadata_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(metadata_text(item) for item in value).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "html", "latex"):
            text = metadata_text(value.get(key))
            if text:
                return text
    return str(value).strip()


__all__ = [
    "DockerParseRun",
    "coerce_rect",
    "first_existing_file",
    "format_rect",
    "metadata_text",
    "page_rects",
    "parser_run_root",
    "rect_to_pdf_points",
    "run_docker_command",
    "safe_paper_id",
    "source_locator",
    "stage_pdf_for_docker",
]
