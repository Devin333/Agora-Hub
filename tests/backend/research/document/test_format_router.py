from __future__ import annotations

from hashlib import sha256

import fitz

from backend.research.document.format_router import MultiFormatDocumentParser
from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchSection


class _Parser:
    def __init__(self, backend: str) -> None:
        self.backend = backend
        self.calls = 0

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        self.calls += 1
        source_hash = sha256(source_bytes).hexdigest()
        source_ref = f"paper://{paper_id}/{self.backend}"
        return ResearchDocument(
            paper_id=paper_id,
            source_hash=source_hash,
            sections=[ResearchSection(
                section_id=f"{self.backend}-section",
                title=self.backend,
                text="parsed",
                source_ref=source_ref,
            )],
            lineage=SourceLineage(source_refs=[source_ref], source_hash=source_hash),
            metadata={"parser_backend": self.backend},
        )


def _pdf() -> bytes:
    document = fitz.open()
    document.new_page().insert_text((50, 72), "Selectable PDF text")
    content = document.tobytes()
    document.close()
    return content


def test_pdf_parser_is_independent_from_arxiv_parser() -> None:
    arxiv = _Parser("arxiv")
    pdf = _Parser("pymupdf")
    parser = MultiFormatDocumentParser(arxiv_parser=arxiv, pdf_parser=pdf)  # type: ignore[arg-type]

    result = parser.parse("paper-1", _pdf())

    assert result.metadata["parser_backend"] == "pymupdf"
    assert pdf.calls == 1
    assert arxiv.calls == 0


def test_latex_keeps_the_arxiv_parser_path() -> None:
    arxiv = _Parser("arxiv")
    pdf = _Parser("pymupdf")
    parser = MultiFormatDocumentParser(arxiv_parser=arxiv, pdf_parser=pdf)  # type: ignore[arg-type]

    result = parser.parse("paper-1", b"\\section{Methods} body")

    assert result.metadata["parser_backend"] == "arxiv"
    assert arxiv.calls == 1
    assert pdf.calls == 0
