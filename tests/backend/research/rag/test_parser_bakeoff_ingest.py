from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchSection
from backend.research.rag.evaluation.paper_parser_bakeoff_ingest import (
    _build_parser,
    ingest_parser_bakeoff_pdfs,
)


@dataclass(frozen=True)
class _FakePackage:
    content: bytes


class _FakePdfFetcher:
    def fetch_pdf_package(self, arxiv_id: str) -> _FakePackage:
        return _FakePackage(content=f"%PDF-1.7\n{arxiv_id}".encode("utf-8"))


class _FakePdfParser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        self.calls.append((paper_id, source_bytes))
        return ResearchDocument(
            paper_id=paper_id,
            source_hash="hash",
            sections=[
                ResearchSection(
                    section_id=f"{paper_id}-intro",
                    title="Introduction",
                    text="Parsed from PDF bytes.",
                    source_ref=f"arxiv://{paper_id}/pdf",
                )
            ],
            lineage=SourceLineage(source_refs=[f"arxiv://{paper_id}/pdf"], source_hash="hash"),
            metadata={"parse_source": "marker"},
        )


class _FailingPdfParser(_FakePdfParser):
    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        raise RuntimeError("parse failed")


def test_parser_bakeoff_ingest_fetches_pdf_and_writes_manifest(tmp_path: Path) -> None:
    parser = _FakePdfParser()
    manifest = tmp_path / "manifest.json"

    report = ingest_parser_bakeoff_pdfs(
        ["https://arxiv.org/pdf/1706.03762.pdf", "1706.03762", "2006.11239"],
        backend="marker",
        papers_dir=tmp_path / "papers",
        manifest_path=manifest,
        fetcher=_FakePdfFetcher(),
        parser=parser,
    )

    assert report.requested == 2
    assert report.succeeded == 2
    assert report.failed == 0
    assert parser.calls[0][0] == "1706.03762"
    assert parser.calls[0][1].startswith(b"%PDF")
    payload = json.loads((tmp_path / "papers" / "1706.03762" / "research_document.json").read_text(encoding="utf-8"))
    assert payload["paper_id"] == "1706.03762"
    assert payload["metadata"]["parse_source"] == "marker"
    assert (tmp_path / "papers" / "1706.03762" / "1706.03762_original.pdf").exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["succeeded"] == 2


def test_parser_bakeoff_ingest_skips_existing_documents(tmp_path: Path) -> None:
    paper_dir = tmp_path / "papers" / "1706.03762"
    paper_dir.mkdir(parents=True)
    (paper_dir / "research_document.json").write_text("{}", encoding="utf-8")

    report = ingest_parser_bakeoff_pdfs(
        ["1706.03762"],
        backend="marker",
        papers_dir=tmp_path / "papers",
        fetcher=_FakePdfFetcher(),
        parser=_FakePdfParser(),
    )

    assert report.skipped == 1
    assert report.items[0].reason == "research_document_exists"


def test_parser_bakeoff_ingest_records_failures(tmp_path: Path) -> None:
    report = ingest_parser_bakeoff_pdfs(
        ["1706.03762"],
        backend="marker",
        papers_dir=tmp_path / "papers",
        fetcher=_FakePdfFetcher(),
        parser=_FailingPdfParser(),
    )

    assert report.failed == 1
    assert report.items[0].status == "failed"
    assert "RuntimeError: parse failed" in report.items[0].reason


def test_parser_bakeoff_cli_accepts_marker_backend() -> None:
    parser = _build_parser()

    args = parser.parse_args([
        "--papers-dir",
        "papers",
        "--manifest",
        "manifest.json",
        "--pdf-parser-backend",
        "marker",
    ])

    assert args.pdf_parser_backend == "marker"
