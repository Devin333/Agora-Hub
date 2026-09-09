from __future__ import annotations

from backend.research.document.cascade_parser import CascadeArxivDocumentParser
from backend.research.document.html_parser import HtmlDocumentParser
from backend.research.document.source_format import SourceFormat, detect_source_format
from backend.research.domain.document import ResearchDocument
from backend.research.ports.document_parser import DocumentParserPort


class MultiFormatDocumentParser:
    """Route raw source bytes to the existing LaTeX/PDF cascade or HTML adapter."""

    def __init__(
        self,
        *,
        arxiv_parser: CascadeArxivDocumentParser | None = None,
        html_parser: HtmlDocumentParser | None = None,
        pdf_parser: DocumentParserPort | None = None,
    ) -> None:
        self._arxiv = arxiv_parser or CascadeArxivDocumentParser()
        self._html = html_parser or HtmlDocumentParser()
        self._pdf = pdf_parser

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        source_format, canonical = detect_source_format(source_bytes)
        if source_format is SourceFormat.HTML:
            return self._html.parse(paper_id, canonical)
        if source_format is SourceFormat.PDF and self._pdf is not None:
            return self._pdf.parse(paper_id, canonical)
        if source_format in {SourceFormat.PDF, SourceFormat.LATEX}:
            return self._arxiv.parse(paper_id, canonical)
        raise ValueError(f"unsupported research source format: {source_format.value}")


__all__ = ["MultiFormatDocumentParser"]
