from __future__ import annotations

import gzip

import fitz
import pytest

from backend.research.document.cascade_parser import (
    CascadeArxivDocumentParser,
    CascadeDocumentParser,
    DocumentQualityProbe,
    PdfTextExtractionError,
    PyMuPDFTextDocumentParser,
    SelectableTextPdfDocumentParser,
    parser_cascade_backend_names,
)
from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchSection
from framework.execution_environment import ExecutionEnvironmentUnavailableError
from framework.shared.graph_identity import GraphExecutionIdentity


class _Parser:
    def __init__(self, document: ResearchDocument | None = None, exc: Exception | None = None) -> None:
        self.document = document
        self.exc = exc
        self.calls = 0

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        assert self.document is not None
        return self.document.model_copy(update={"paper_id": paper_id})


class _LatexParser:
    def __init__(self) -> None:
        self.calls = 0

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        self.calls += 1
        return _doc(paper_id, "latex", sections=3, chars=120)


class _ExecutionAwareParser(_Parser):
    def __init__(self, document: ResearchDocument) -> None:
        super().__init__(document)
        self.identities: list[GraphExecutionIdentity | None] = []

    def parse(
        self,
        paper_id: str,
        source_bytes: bytes,
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> ResearchDocument:
        self.identities.append(execution_identity)
        return super().parse(paper_id, source_bytes)


def _doc(
    paper_id: str,
    backend: str,
    *,
    sections: int,
    chars: int,
) -> ResearchDocument:
    text = "x" * chars
    source_ref = f"arxiv://{paper_id}/pdf"
    return ResearchDocument(
        paper_id=paper_id,
        source_hash="hash",
        sections=[
            ResearchSection(
                section_id=f"{backend}-s{index}",
                title=f"Section {index}",
                text=text,
                source_ref=source_ref,
                metadata={"parse_source": backend, "source_locator": source_ref},
            )
            for index in range(sections)
        ],
        lineage=SourceLineage(source_refs=[source_ref], source_hash="hash"),
        metadata={"parse_source": backend, "parser_backend": backend},
    )


def _pdf(text_pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in text_pages:
        page = doc.new_page()
        page.insert_text((50, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _probe() -> DocumentQualityProbe:
    return DocumentQualityProbe(
        min_sections=2,
        min_body_chars=100,
        min_non_empty_section_ratio=0.5,
        max_replacement_char_ratio=0.2,
    )


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-parser-cascade",
        graph_id="research-graph",
        graph_version="1.0.0",
        graph_ref="research-graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="compile_document",
        node_instance_id="compile-document-1",
        activity_id="compile-document-activity",
        attempt=1,
    )


def test_cascade_returns_first_backend_when_quality_passes() -> None:
    first = _Parser(_doc("paper-1", "mineru", sections=2, chars=80))
    second = _Parser(_doc("paper-1", "marker", sections=2, chars=80))
    parser = CascadeDocumentParser([("mineru", first), ("marker", second)], probe=_probe())

    doc = parser.parse("paper-1", b"%PDF-1.7")

    assert doc.metadata["parse_source"] == "mineru"
    assert doc.metadata["parser_cascade"]["used_backend"] == "mineru"
    assert doc.metadata["parser_cascade"]["attempts"][0]["status"] == "success"
    assert second.calls == 0


def test_cascade_falls_through_after_parse_error() -> None:
    first = _Parser(exc=RuntimeError("boom"))
    second = _Parser(_doc("paper-1", "marker", sections=2, chars=80))
    parser = CascadeDocumentParser([("mineru", first), ("marker", second)], probe=_probe())

    doc = parser.parse("paper-1", b"%PDF-1.7")

    attempts = doc.metadata["parser_cascade"]["attempts"]
    assert doc.metadata["parse_source"] == "marker"
    assert attempts[0]["backend"] == "mineru"
    assert attempts[0]["status"] == "parse_error"
    assert "RuntimeError: boom" in attempts[0]["reason"]
    assert attempts[1]["backend"] == "marker"
    assert attempts[1]["status"] == "success"


def test_cascade_forwards_exact_execution_identity_to_aware_backend() -> None:
    backend = _ExecutionAwareParser(
        _doc("paper-1", "mineru", sections=2, chars=80)
    )
    parser = CascadeDocumentParser([("mineru", backend)], probe=_probe())
    identity = _identity()

    parser.parse(
        "paper-1",
        b"%PDF-1.7",
        execution_identity=identity,
    )

    assert backend.identities == [identity]


def test_cascade_propagates_execution_denial_without_fallback() -> None:
    denied = _Parser(exc=ExecutionEnvironmentUnavailableError("docker unavailable"))
    second = _Parser(_doc("paper-1", "marker", sections=2, chars=80))
    fallback = _Parser(_doc("paper-1", "pymupdf", sections=2, chars=80))
    parser = CascadeDocumentParser(
        [("mineru", denied), ("marker", second)],
        probe=_probe(),
        fallback=fallback,
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError, match="docker unavailable"):
        parser.parse("paper-1", b"%PDF-1.7", execution_identity=_identity())

    assert denied.calls == 1
    assert second.calls == 0
    assert fallback.calls == 0


def test_cascade_falls_through_after_quality_rejection() -> None:
    first = _Parser(_doc("paper-1", "mineru", sections=1, chars=10))
    second = _Parser(_doc("paper-1", "marker", sections=2, chars=80))
    parser = CascadeDocumentParser([("mineru", first), ("marker", second)], probe=_probe())

    doc = parser.parse("paper-1", b"%PDF-1.7")

    attempts = doc.metadata["parser_cascade"]["attempts"]
    assert doc.metadata["parse_source"] == "marker"
    assert attempts[0]["status"] == "quality_rejected"
    assert attempts[0]["reason"] == "sections_below_threshold"
    assert attempts[0]["quality"]["passed"] is False


def test_cascade_uses_pymupdf_fallback_after_all_attempts_fail() -> None:
    parser = CascadeDocumentParser(
        [
            ("mineru", _Parser(exc=RuntimeError("mineru failed"))),
            ("marker", _Parser(_doc("paper-1", "marker", sections=1, chars=5))),
        ],
        probe=_probe(),
    )

    doc = parser.parse("paper-1", _pdf(["Native text page one."]))

    attempts = doc.metadata["parser_cascade"]["attempts"]
    assert doc.metadata["parse_source"] == "pymupdf"
    assert doc.metadata["degraded"] is True
    assert doc.status == "degraded"
    assert attempts[-1]["backend"] == "pymupdf"
    assert attempts[-1]["status"] == "fallback"
    assert doc.sections[0].title == "PDF Text Page 1"


def test_pymupdf_fallback_extracts_gzipped_pdf_via_cascade_arxiv_parser() -> None:
    latex = _LatexParser()
    pdf_parser = CascadeDocumentParser(
        [("mineru", _Parser(exc=RuntimeError("down")))],
        probe=_probe(),
        fallback=PyMuPDFTextDocumentParser(),
    )
    parser = CascadeArxivDocumentParser(latex_parser=latex, pdf_parser=pdf_parser)

    doc = parser.parse("paper-1", gzip.compress(_pdf(["Gzipped PDF text."])))

    assert latex.calls == 0
    assert doc.metadata["parse_source"] == "pymupdf"
    assert "Gzipped PDF text." in doc.sections[0].text


def test_selectable_text_pdf_parser_admits_bounded_native_text() -> None:
    parser = SelectableTextPdfDocumentParser(probe=_probe())

    doc = parser.parse("paper-1", _pdf(["Methods " + "m" * 80, "Results " + "r" * 80]))

    assert doc.status == "parsed"
    assert doc.metadata["text_only"] is True
    assert doc.metadata["compiled_reader_ready"] is False
    assert doc.metadata["text_admission"]["passed"] is True
    assert all(section.source_ref.startswith("paper://paper-1/pdf#page=") for section in doc.sections)


def test_selectable_text_pdf_parser_rejects_scan_and_insufficient_text() -> None:
    parser = SelectableTextPdfDocumentParser(probe=_probe())

    with pytest.raises(PdfTextExtractionError, match="pdf_no_extractable_text"):
        parser.parse("scan", _pdf([""]))
    with pytest.raises(PdfTextExtractionError, match="sections_below_threshold"):
        parser.parse("short", _pdf(["Only one short page."]))


def test_selectable_text_pdf_parser_rejects_garbled_extraction() -> None:
    extractor = _Parser(_doc("paper-1", "pymupdf", sections=2, chars=80).model_copy(update={
        "sections": [
            ResearchSection(
                section_id=f"garbled-{index}",
                title=f"Page {index}",
                text="\ufffd" * 80,
                source_ref=f"paper://paper-1/pdf#page={index}",
            )
            for index in range(1, 3)
        ]
    }))
    parser = SelectableTextPdfDocumentParser(extractor=extractor, probe=_probe())

    with pytest.raises(PdfTextExtractionError, match="replacement_char_ratio_above_threshold"):
        parser.parse("paper-1", b"%PDF-1.7")


def test_cascade_arxiv_parser_forwards_execution_identity_to_pdf_cascade() -> None:
    backend = _ExecutionAwareParser(
        _doc("paper-1", "mineru", sections=2, chars=80)
    )
    pdf_parser = CascadeDocumentParser([("mineru", backend)], probe=_probe())
    parser = CascadeArxivDocumentParser(
        latex_parser=_LatexParser(),
        pdf_parser=pdf_parser,
    )
    identity = _identity()

    parser.parse(
        "paper-1",
        _pdf(["Identity-aware PDF."]),
        execution_identity=identity,
    )

    assert backend.identities == [identity]


def test_cascade_arxiv_parser_keeps_latex_routing() -> None:
    latex = _LatexParser()
    pdf_parser = CascadeDocumentParser(
        [("mineru", _Parser(exc=RuntimeError("should not run")))],
        probe=_probe(),
    )
    parser = CascadeArxivDocumentParser(latex_parser=latex, pdf_parser=pdf_parser)

    doc = parser.parse("paper-1", b"\\section{Intro} latex")

    assert latex.calls == 1
    assert doc.metadata["parse_source"] == "latex"


def test_parser_cascade_backend_names_validates_env(monkeypatch) -> None:
    monkeypatch.setenv("NEWSROOM_PDF_PARSER_CASCADE", "mineru,marker")

    assert parser_cascade_backend_names() == ("mineru", "marker")

    with pytest.raises(ValueError, match="mineru, marker, pymupdf"):
        parser_cascade_backend_names("docling")
