from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "ArxivDocumentParser",
    "AsyncChunkPreprocessor",
    "CascadeArxivDocumentParser",
    "CascadeDocumentParser",
    "ChunkType",
    "DocumentQualityProbe",
    "PaperChunk",
    "PaperDocumentChunker",
    "ParseSource",
    "PdfTextExtractionError",
    "MarkerPdfDocumentParser",
    "PdfDocumentParser",
    "PyMuPDFTextDocumentParser",
    "SelectableTextPdfDocumentParser",
    "SectionRole",
    "SourceFormat",
    "detect_source_format",
    "build_pdf_document_parser",
    "pdf_parser_backend_name",
]

_LAZY = LazyExports(
    __name__,
    {
        "PaperChunk": ("backend.research.document.models", "PaperChunk"),
        "ParseSource": ("backend.research.document.models", "ParseSource"),
        "ChunkType": ("backend.research.document.models", "ChunkType"),
        "SectionRole": ("backend.research.document.models", "SectionRole"),
        "CascadeArxivDocumentParser": ("backend.research.document.cascade_parser", "CascadeArxivDocumentParser"),
        "CascadeDocumentParser": ("backend.research.document.cascade_parser", "CascadeDocumentParser"),
        "DocumentQualityProbe": ("backend.research.document.cascade_parser", "DocumentQualityProbe"),
        "PdfTextExtractionError": ("backend.research.document.cascade_parser", "PdfTextExtractionError"),
        "PyMuPDFTextDocumentParser": ("backend.research.document.cascade_parser", "PyMuPDFTextDocumentParser"),
        "SelectableTextPdfDocumentParser": ("backend.research.document.cascade_parser", "SelectableTextPdfDocumentParser"),
        "PaperDocumentChunker": ("backend.research.document.chunker", "PaperDocumentChunker"),
        "AsyncChunkPreprocessor": ("backend.research.document.async_preprocessor", "AsyncChunkPreprocessor"),
        "ArxivDocumentParser": ("backend.research.document.arxiv_parser", "ArxivDocumentParser"),
        "MarkerPdfDocumentParser": ("backend.research.document.marker_pdf_parser", "MarkerPdfDocumentParser"),
        "PdfDocumentParser": ("backend.research.document.pdf_compiler", "PdfDocumentParser"),
        "build_pdf_document_parser": ("backend.research.document.pdf_parser_backend", "build_pdf_document_parser"),
        "pdf_parser_backend_name": ("backend.research.document.pdf_parser_backend", "pdf_parser_backend_name"),
        "SourceFormat": ("backend.research.document.source_format", "SourceFormat"),
        "detect_source_format": ("backend.research.document.source_format", "detect_source_format"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.document.models import (
        PaperChunk,
        ParseSource,
        ChunkType,
        SectionRole,
    )
    from backend.research.document.cascade_parser import (
        CascadeArxivDocumentParser,
        CascadeDocumentParser,
        DocumentQualityProbe,
        PdfTextExtractionError,
        PyMuPDFTextDocumentParser,
        SelectableTextPdfDocumentParser,
    )
    from backend.research.document.chunker import (
        PaperDocumentChunker,
    )
    from backend.research.document.async_preprocessor import (
        AsyncChunkPreprocessor,
    )
    from backend.research.document.arxiv_parser import (
        ArxivDocumentParser,
    )
    from backend.research.document.marker_pdf_parser import (
        MarkerPdfDocumentParser,
    )
    from backend.research.document.pdf_compiler import (
        PdfDocumentParser,
    )
    from backend.research.document.pdf_parser_backend import (
        build_pdf_document_parser,
        pdf_parser_backend_name,
    )
    from backend.research.document.source_format import (
        SourceFormat,
        detect_source_format,
    )
