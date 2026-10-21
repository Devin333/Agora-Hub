from __future__ import annotations

import os
from typing import Literal

from backend.research.document.pdf_compiler import PdfDocumentParser
from backend.research.domain.document import ResearchDocument

PdfParserBackendName = Literal["nougat", "marker", "cascade"]


def pdf_parser_backend_name(value: str | None = None) -> PdfParserBackendName:
    raw = (value or os.environ.get("NEWSROOM_PDF_PARSER_BACKEND") or "nougat").strip().lower()
    if raw in {"", "nougat", "default"}:
        return "nougat"
    if raw == "marker":
        return "marker"
    if raw == "cascade":
        return "cascade"
    raise ValueError(
        "NEWSROOM_PDF_PARSER_BACKEND must be one of: nougat, marker, cascade"
    )


def build_pdf_document_parser(
    backend: str | None = None,
) -> "PdfDocumentParser | MarkerPdfDocumentParser | CascadeDocumentParser":
    name = pdf_parser_backend_name(backend)
    if name == "cascade":
        from backend.research.document.cascade_parser import build_default_pdf_cascade_parser

        return build_default_pdf_cascade_parser()
    if name == "marker":
        from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser

        return MarkerPdfDocumentParser()
    return PdfDocumentParser()


class PdfBackendDocumentParser:
    """Small adapter used by tests and ingest call sites that only parse PDFs."""

    def __init__(self, backend: str | None = None) -> None:
        self._parser = build_pdf_document_parser(backend)

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        return self._parser.parse(paper_id, source_bytes)


__all__ = [
    "PdfBackendDocumentParser",
    "PdfParserBackendName",
    "build_pdf_document_parser",
    "pdf_parser_backend_name",
]
