from __future__ import annotations

import argparse
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

from backend.research.document.pdf_parser_backend import build_pdf_document_parser
from backend.research.domain.document import ResearchDocument

DEFAULT_PARSER_BAKEOFF_ARXIV_IDS = (
    "1706.03762",
    "1505.04597",
    "1810.04805",
    "2006.11239",
    "1512.03385",
    "1409.0473",
    "1606.03498",
    "1905.11946",
    "2103.00020",
    "2201.11903",
)


class PdfPackageFetcher(Protocol):
    def fetch_pdf_package(self, arxiv_id: str) -> Any:
        ...


class PdfParser(Protocol):
    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        ...


@dataclass(frozen=True)
class ArxivPdfPackage:
    arxiv_id: str
    content: bytes


class UrllibArxivPdfPackageFetcher:
    def __init__(self, *, timeout_seconds: int = 120) -> None:
        self._timeout_seconds = timeout_seconds

    def fetch_pdf_package(self, arxiv_id: str) -> ArxivPdfPackage:
        normalized = _normalize_arxiv_id(arxiv_id)
        request = Request(
            f"https://arxiv.org/pdf/{quote(normalized, safe='/.')}",
            headers={"User-Agent": "AgoraHubResearch/0.1 (parser bakeoff ingest)"},
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            return ArxivPdfPackage(arxiv_id=normalized, content=response.read())


@dataclass(frozen=True)
class ParserBakeoffIngestItem:
    arxiv_id: str
    paper_id: str
    status: str
    backend: str
    reason: str = ""
    bytes_fetched: int = 0
    sections: int = 0
    figures: int = 0
    tables: int = 0
    equations: int = 0
    duration_seconds: float = 0.0
    output_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "paper_id": self.paper_id,
            "status": self.status,
            "backend": self.backend,
            "reason": self.reason,
            "bytes_fetched": self.bytes_fetched,
            "sections": self.sections,
            "figures": self.figures,
            "tables": self.tables,
            "equations": self.equations,
            "duration_seconds": self.duration_seconds,
            "output_path": self.output_path,
        }


@dataclass(frozen=True)
class ParserBakeoffIngestReport:
    backend: str
    papers_dir: Path
    requested: int
    succeeded: int
    skipped: int
    failed: int
    items: tuple[ParserBakeoffIngestItem, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "papers_dir": str(self.papers_dir),
            "requested": self.requested,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "items": [item.to_dict() for item in self.items],
        }


def ingest_parser_bakeoff_pdfs(
    arxiv_ids: Sequence[str],
    *,
    backend: str,
    papers_dir: Path,
    force: bool = False,
    max_papers: int | None = None,
    manifest_path: Path | None = None,
    fetcher: PdfPackageFetcher | None = None,
    parser: PdfParser | None = None,
) -> ParserBakeoffIngestReport:
    ids = _normalize_ids(arxiv_ids)
    if max_papers is not None:
        ids = ids[:max(0, max_papers)]
    fetcher = fetcher or UrllibArxivPdfPackageFetcher()
    parser = parser or build_pdf_document_parser(backend)

    items: list[ParserBakeoffIngestItem] = []
    for arxiv_id in ids:
        paper_id = _paper_id(arxiv_id)
        output_path = papers_dir / paper_id / "research_document.json"
        start = time.perf_counter()
        if output_path.exists() and not force:
            items.append(ParserBakeoffIngestItem(
                arxiv_id=arxiv_id,
                paper_id=paper_id,
                status="skipped",
                backend=backend,
                reason="research_document_exists",
                duration_seconds=_elapsed(start),
                output_path=str(output_path),
            ))
            continue
        try:
            package = fetcher.fetch_pdf_package(arxiv_id)
            pdf_bytes = bytes(package.content)
            document = parser.parse(paper_id, pdf_bytes)
            _write_document(document, output_path)
            _copy_original_pdf(pdf_bytes, output_path.parent, paper_id)
        except Exception as exc:  # noqa: BLE001 - batch ingest records failures and continues
            items.append(ParserBakeoffIngestItem(
                arxiv_id=arxiv_id,
                paper_id=paper_id,
                status="failed",
                backend=backend,
                reason=f"{type(exc).__name__}: {exc}",
                duration_seconds=_elapsed(start),
                output_path=str(output_path),
            ))
            continue
        items.append(ParserBakeoffIngestItem(
            arxiv_id=arxiv_id,
            paper_id=paper_id,
            status="succeeded",
            backend=backend,
            bytes_fetched=len(pdf_bytes),
            sections=len(document.sections),
            figures=len(document.figures),
            tables=len(document.tables),
            equations=len(document.equations),
            duration_seconds=_elapsed(start),
            output_path=str(output_path),
        ))

    report = ParserBakeoffIngestReport(
        backend=backend,
        papers_dir=papers_dir,
        requested=len(ids),
        succeeded=sum(1 for item in items if item.status == "succeeded"),
        skipped=sum(1 for item in items if item.status == "skipped"),
        failed=sum(1 for item in items if item.status == "failed"),
        items=tuple(items),
    )
    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    ids = list(args.arxiv_id)
    if args.ids_file:
        ids.extend(_read_ids_file(Path(args.ids_file)))
    if not ids:
        ids = list(DEFAULT_PARSER_BAKEOFF_ARXIV_IDS)
    report = ingest_parser_bakeoff_pdfs(
        ids,
        backend=args.pdf_parser_backend,
        papers_dir=Path(args.papers_dir),
        force=args.force,
        max_papers=args.max_papers,
        manifest_path=Path(args.manifest) if args.manifest else None,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), end="\n")
    return 0 if report.failed == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.research.rag.evaluation.paper_parser_bakeoff_ingest",
        description="Fetch arXiv PDFs and parse them with one PDF parser backend for parser bake-off.",
    )
    parser.add_argument("arxiv_id", nargs="*", help="arXiv ids or URLs. Defaults to a 10-paper smoke set.")
    parser.add_argument("--ids-file", help="Optional newline-delimited arXiv id file.")
    parser.add_argument("--papers-dir", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--max-papers", type=int)
    parser.add_argument("--force", action="store_true", help="Re-parse papers even when research_document.json exists.")
    parser.add_argument(
        "--pdf-parser-backend",
        choices=("nougat", "marker"),
        required=True,
        help="PDF parser backend to run for every fetched PDF.",
    )
    return parser


def _normalize_ids(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        normalized = _normalize_arxiv_id(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _read_ids_file(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            ids.append(value)
    return ids


def _paper_id(arxiv_id: str) -> str:
    return _normalize_arxiv_id(arxiv_id).replace("/", "_")


def _normalize_arxiv_id(value: str) -> str:
    text = value.strip().rstrip(".,;:)]}>'\"")
    marker_found = False
    for marker in ("arxiv.org/abs/", "arxiv.org/pdf/", "arxiv.org/e-print/", "arxiv.org/src/"):
        if marker in text:
            text = text.rsplit(marker, 1)[-1]
            marker_found = True
            break
    if "://" in text and not marker_found:
        return ""
    if text.endswith(".pdf"):
        text = text[:-4]
    return text.split("?", 1)[0].split("#", 1)[0].strip("/")


def _write_document(document: ResearchDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_original_pdf(pdf_bytes: bytes, paper_dir: Path, paper_id: str) -> None:
    paper_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = paper_dir / f"{paper_id}_original.pdf"
    pdf_path.write_bytes(pdf_bytes)
    dotted = paper_id.replace("_", ".")
    if dotted != paper_id:
        shutil.copyfile(pdf_path, paper_dir / f"{dotted}_original.pdf")


def _elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 3)


if __name__ == "__main__":
    raise SystemExit(main())
