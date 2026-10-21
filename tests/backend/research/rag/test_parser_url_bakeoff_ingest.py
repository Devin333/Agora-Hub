from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchFigure, ResearchSection
from backend.research.rag.evaluation.paper_parser_url_bakeoff_ingest import (
    PdfUrlIngestSource,
    _build_parser,
    acl_long_pdf_sources,
    ingest_parser_bakeoff_pdf_urls,
)


@dataclass(frozen=True)
class _FakeUrlFetcher:
    payload: bytes = b"%PDF-1.7\nfake"

    def fetch_pdf(self, url: str) -> bytes:
        return self.payload + url.encode("utf-8")


class _FakePdfParser:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes]] = []

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        self.calls.append((paper_id, source_bytes))
        source_ref = f"arxiv://{paper_id}/pdf"
        return ResearchDocument(
            paper_id=paper_id,
            source_hash="hash",
            sections=[
                ResearchSection(
                    section_id=f"{paper_id}-intro",
                    title="Introduction",
                    text="Parsed URL PDF.",
                    source_ref=f"{source_ref}#page=1",
                    metadata={"source_locator": f"{source_ref}#page=1"},
                )
            ],
            figures=[
                ResearchFigure(
                    figure_id=f"{paper_id}-fig",
                    caption="Figure 1",
                    source_ref=f"{source_ref}#page=2",
                    metadata={"source_locator": f"{source_ref}#page=2"},
                )
            ],
            lineage=SourceLineage(source_refs=[source_ref], source_hash="hash"),
            metadata={"parse_source": "nougat"},
        )


class _FailingParser(_FakePdfParser):
    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        raise RuntimeError("parse failed")


def test_acl_long_pdf_sources_generate_acl_anthology_urls() -> None:
    sources = acl_long_pdf_sources(2025, 7, 2)

    assert sources == [
        PdfUrlIngestSource(
            source_id="2025.acl-long.7",
            paper_id="2025_acl-long_7",
            url="https://aclanthology.org/2025.acl-long.7.pdf",
        ),
        PdfUrlIngestSource(
            source_id="2025.acl-long.8",
            paper_id="2025_acl-long_8",
            url="https://aclanthology.org/2025.acl-long.8.pdf",
        ),
    ]


def test_url_bakeoff_ingest_fetches_pdf_and_rewrites_source_refs(tmp_path: Path) -> None:
    parser = _FakePdfParser()
    manifest = tmp_path / "manifest.json"

    report = ingest_parser_bakeoff_pdf_urls(
        ["2025.acl-long.1", "paper-x=https://example.test/paper-x.pdf"],
        backend="nougat",
        papers_dir=tmp_path / "papers",
        manifest_path=manifest,
        fetcher=_FakeUrlFetcher(),
        parser=parser,
    )

    assert report.requested == 2
    assert report.succeeded == 2
    assert report.failed == 0
    assert parser.calls[0][0] == "2025_acl-long_1"
    payload = json.loads(
        (tmp_path / "papers" / "2025_acl-long_1" / "research_document.json").read_text(encoding="utf-8")
    )
    assert payload["metadata"]["source_type"] == "pdf_url"
    assert payload["metadata"]["source_id"] == "2025.acl-long.1"
    assert payload["metadata"]["source_url"] == "https://aclanthology.org/2025.acl-long.1.pdf"
    assert payload["lineage"]["source_refs"] == ["pdf-url://2025.acl-long.1"]
    assert payload["sections"][0]["source_ref"] == "pdf-url://2025.acl-long.1#page=1"
    assert payload["figures"][0]["metadata"]["source_locator"] == "pdf-url://2025.acl-long.1#page=2"
    assert (tmp_path / "papers" / "2025_acl-long_1" / "2025_acl-long_1_original.pdf").exists()
    assert json.loads(manifest.read_text(encoding="utf-8"))["succeeded"] == 2


def test_url_bakeoff_ingest_records_failures(tmp_path: Path) -> None:
    report = ingest_parser_bakeoff_pdf_urls(
        ["https://example.test/paper.pdf"],
        backend="marker",
        papers_dir=tmp_path / "papers",
        fetcher=_FakeUrlFetcher(),
        parser=_FailingParser(),
    )

    assert report.failed == 1
    assert report.items[0].status == "failed"
    assert "RuntimeError: parse failed" in report.items[0].reason


def test_url_bakeoff_cli_accepts_marker_backend() -> None:
    parser = _build_parser()

    args = parser.parse_args([
        "2025.acl-long.1",
        "--papers-dir",
        "papers",
        "--manifest",
        "manifest.json",
        "--pdf-parser-backend",
        "marker",
    ])

    assert args.pdf_parser_backend == "marker"
