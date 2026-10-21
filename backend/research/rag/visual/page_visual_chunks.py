from __future__ import annotations

from pathlib import Path
from typing import Iterable

from backend.research.document.models import PaperChunk
from framework.rag.core import build_rag_stable_id


def build_page_visual_chunks(
    chunks: Iterable[PaperChunk],
    *,
    papers_dir: Path,
    render_pages: bool = True,
) -> list[PaperChunk]:
    """Create page-level visual chunks from original PDFs for visual retrieval."""
    by_paper_page: dict[tuple[str, int], list[PaperChunk]] = {}
    for chunk in chunks:
        page = _chunk_page(chunk)
        if page is None:
            continue
        if chunk.chunk_type not in {"figure", "table"}:
            continue
        by_paper_page.setdefault((chunk.paper_id, page), []).append(chunk)

    out: list[PaperChunk] = []
    for (paper_id, page), page_chunks in sorted(by_paper_page.items()):
        image_ref = _page_image_ref(paper_id, page)
        image_path = papers_dir / paper_id / image_ref
        if render_pages and not image_path.exists():
            pdf_path = _find_original_pdf(papers_dir / paper_id, paper_id)
            if pdf_path is not None:
                _render_pdf_page(pdf_path, image_path, page=page)
        if not image_path.exists():
            continue
        out.append(_page_visual_chunk(
            paper_id=paper_id,
            page=page,
            image_ref=image_ref,
            page_chunks=page_chunks,
        ))
    return out


def _page_visual_chunk(
    *,
    paper_id: str,
    page: int,
    image_ref: str,
    page_chunks: list[PaperChunk],
) -> PaperChunk:
    related = [
        {
            "chunk_id": chunk.chunk_id,
            "chunk_type": chunk.chunk_type,
            "image_ref": str(chunk.metadata.get("image_ref") or ""),
            "source_locator": str(chunk.metadata.get("source_locator") or ""),
        }
        for chunk in page_chunks
    ]
    content = "\n".join([
        f"[Page {page}]",
        "Visual page containing paper figures/tables.",
        *(_page_chunk_summary(chunk) for chunk in page_chunks),
    ])
    chunk_id = build_rag_stable_id("chunk", paper_id, "page_visual", str(page))
    return PaperChunk(
        chunk_id=chunk_id,
        paper_id=paper_id,
        parse_source=page_chunks[0].parse_source,
        structure_detected=page_chunks[0].structure_detected,
        chunk_type="figure",
        section_title=f"Page {page}",
        section_role=[],
        section_index=min((chunk.section_index for chunk in page_chunks), default=0),
        content=content,
        metadata={
            "is_parent": False,
            "page_visual": True,
            "visual_element_kind": "page",
            "page": page,
            "image_ref": image_ref,
            "source_ref": f"paper://{paper_id}/pdf#page={page}",
            "source_locator": f"paper://{paper_id}/pdf#page={page}",
            "related_visual_chunks": related,
        },
    )


def _page_chunk_summary(chunk: PaperChunk) -> str:
    label = chunk.metadata.get("figure_id") or chunk.metadata.get("table_id") or chunk.chunk_id
    caption = " ".join(chunk.content.split())[:240]
    return f"- {chunk.chunk_type} {label}: {caption}"


def _page_image_ref(paper_id: str, page: int) -> str:
    return f"page_images/{paper_id}_page_{page:03d}.png"


def _find_original_pdf(paper_dir: Path, paper_id: str) -> Path | None:
    candidates = [
        paper_dir / f"{paper_id}_original.pdf",
        paper_dir / f"{paper_id.replace('_', '.')}_original.pdf",
    ]
    candidates.extend(sorted(paper_dir.glob("*_original.pdf")))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _render_pdf_page(pdf_path: Path, image_path: Path, *, page: int) -> None:
    import pymupdf

    image_path.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(str(pdf_path)) as document:
        if page < 1 or page > len(document):
            return
        pixmap = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        pixmap.save(str(image_path))


def _chunk_page(chunk: PaperChunk) -> int | None:
    for value in (
        chunk.metadata.get("page"),
        chunk.metadata.get("caption_text_page"),
        _page_from_locator(chunk.metadata.get("source_locator")),
        _page_from_locator(chunk.metadata.get("caption_source_locator")),
    ):
        if value is None:
            continue
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0:
            return page
    return None


def _page_from_locator(value: object) -> int | None:
    import re

    match = re.search(r"(?:#|&)page=(\d+)", str(value or ""))
    return int(match.group(1)) if match else None


__all__ = ["build_page_visual_chunks"]
