from __future__ import annotations

from hashlib import sha256
from typing import Any

import pymupdf

from backend.research.domain.document import ResearchDocument, ResearchFigure, ResearchTable

from .pdf_compiler import (
    _attach_figure_images,
    _attach_table_images,
    _build_parse_quality,
    _extract_page_text_evidence,
    _extract_pdf_images,
    _extract_surya_layout_artifacts,
    _page_text_evidence_summary,
)


def merge_pdf_visual_sidecar(
    document: ResearchDocument,
    pdf_bytes: bytes,
    *,
    paper_id: str | None = None,
) -> ResearchDocument:
    """Attach PDF-derived visual artifacts to a document parsed from another source.

    This is intentionally narrower than the full PDF parser: it does not replace the
    LaTeX/Nougat text structure, it only uses the PDF sidecar for figure/table image
    crops, page/bbox metadata, and table rows when Surya/PyMuPDF can recover them.
    """
    actual_paper_id = paper_id or document.paper_id
    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError("pdf sidecar must start with %PDF")

    pdf_source_ref = f"arxiv://{actual_paper_id.replace('_', '.')}/pdf"
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf_doc:
        page_texts = _extract_page_text_evidence(pdf_doc)
        surya_error = ""
        try:
            surya_artifacts = _extract_surya_layout_artifacts(pdf_doc, actual_paper_id)
            figure_images = list(surya_artifacts.figure_images)
            table_images = list(surya_artifacts.table_images)
        except Exception as exc:  # noqa: BLE001 - keep main ingest usable when Surya is down
            surya_error = f"{type(exc).__name__}: {exc}"
            surya_artifacts = None
            figure_images = []
            table_images = []
        figure_image_source = "surya_layout" if figure_images else "pdf_embedded"
        if not figure_images:
            figure_images = _extract_pdf_images(pdf_doc, actual_paper_id)
            if not figure_images:
                figure_image_source = "none"

        figures = _attach_figure_images(
            _with_pdf_source_ref(document.figures, pdf_source_ref),
            figure_images,
            page_texts,
        )
        tables = _attach_table_images(
            _with_pdf_source_ref(document.tables, pdf_source_ref),
            table_images,
            page_texts,
        )

    metadata = dict(document.metadata)
    metadata.update({
        "pdf_sidecar_enabled": True,
        "pdf_sidecar_source_ref": pdf_source_ref,
        "pdf_sidecar_hash": sha256(pdf_bytes).hexdigest(),
        "pdf_sidecar_figure_image_source": figure_image_source,
        "pdf_sidecar_figure_images": len(figure_images),
        "pdf_sidecar_table_images": len(table_images),
        "pdf_sidecar_visual_merged_figures": sum(1 for figure in figures if figure.image_ref),
        "pdf_sidecar_visual_merged_tables": sum(1 for table in tables if table.metadata.get("image_ref")),
        "pdf_sidecar_page_text_evidence": _page_text_evidence_summary(page_texts),
    })
    if surya_artifacts is not None:
        metadata["pdf_sidecar_surya_layout_ref"] = surya_artifacts.layout_ref or ""
        metadata["pdf_sidecar_surya_layout_regions"] = surya_artifacts.region_count
    if surya_error:
        metadata["pdf_sidecar_surya_layout_error"] = surya_error
    metadata["parse_quality"] = _build_parse_quality(
        sections=document.sections,
        figures=figures,
        tables=tables,
        equations=document.equations,
        missing_pages=set(),
        text_fallback_pages=[],
        ocr_pages=[item.page for item in page_texts if item.selected_source == "surya_ocr"],
        ocr_attempted_pages=[item.page for item in page_texts if item.ocr_attempted],
        low_native_text_pages=[item.page for item in page_texts if item.ocr_attempted],
    )

    lineage = document.lineage.model_copy(update={
        "source_refs": [*document.lineage.source_refs, pdf_source_ref],
    })
    return document.model_copy(update={
        "figures": figures,
        "tables": tables,
        "lineage": lineage,
        "metadata": metadata,
    })


def _with_pdf_source_ref(items: list[Any], pdf_source_ref: str) -> list[Any]:
    out: list[Any] = []
    for item in items:
        out.append(item.model_copy(update={"source_ref": pdf_source_ref}))
    return out


__all__ = ["merge_pdf_visual_sidecar"]
