from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import quote
from urllib.request import Request, urlopen

from backend.research.document.arxiv_parser import ArxivDocumentParser
from backend.research.document.pdf_visual_sidecar import merge_pdf_visual_sidecar
from backend.research.document.source_format import detect_source_format
from backend.research.domain.document import ResearchDocument
from framework.shared.env import load_root_env

DEFAULT_BENCHMARK_ARXIV_IDS = (
    # NLP / ML systems / vision / speech / bio / optimization / IR / robotics / graphs
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
    "2305.14314",
    "2005.14165",
    "1910.10683",
    "1904.09751",
    "1807.03748",
    "1412.6980",
    "1609.04747",
    "1710.10903",
    "1803.01271",
    "1811.12808",
    "1906.08237",
    "2003.08934",
    "2106.09685",
    "2203.02155",
    "2302.13971",
    "2307.09288",
    "2402.17764",
    "2104.09864",
    "2004.10934",
    "1808.05326",
)


class SourcePackageFetcher(Protocol):
    def fetch_source_package(self, arxiv_id: str) -> Any:
        ...


class DocumentParser(Protocol):
    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        ...


@dataclass(frozen=True)
class ArxivSourcePackage:
    arxiv_id: str
    content: bytes


class UrllibArxivSourcePackageFetcher:
    def __init__(self, *, timeout_seconds: int = 120) -> None:
        self._timeout_seconds = timeout_seconds

    def fetch_source_package(self, arxiv_id: str) -> ArxivSourcePackage:
        normalized = _normalize_arxiv_id(arxiv_id)
        return ArxivSourcePackage(
            arxiv_id=normalized,
            content=self._fetch_bytes(f"https://arxiv.org/e-print/{quote(normalized, safe='.')}"),
        )

    def fetch_pdf_package(self, arxiv_id: str) -> ArxivSourcePackage:
        normalized = _normalize_arxiv_id(arxiv_id)
        return ArxivSourcePackage(
            arxiv_id=normalized,
            content=self._fetch_bytes(f"https://arxiv.org/pdf/{quote(normalized, safe='/.')}"),
        )

    def _fetch_bytes(self, url: str) -> bytes:
        request = Request(
            url,
            headers={"User-Agent": "AgoraHubResearch/0.1 (paper benchmark ingest)"},
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            return response.read()


@dataclass(frozen=True)
class PaperIngestItem:
    arxiv_id: str
    paper_id: str
    status: str
    reason: str = ""
    parse_source: str = ""
    detected_format: str = ""
    bytes_fetched: int = 0
    sections: int = 0
    figures: int = 0
    tables: int = 0
    equations: int = 0
    duration_seconds: float = 0.0
    output_path: str = ""
    pdf_sidecar_status: str = "disabled"
    pdf_sidecar_reason: str = ""
    pdf_sidecar_bytes: int = 0
    pdf_sidecar_visual_merged_figures: int = 0
    pdf_sidecar_visual_merged_tables: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "arxiv_id": self.arxiv_id,
            "paper_id": self.paper_id,
            "status": self.status,
            "reason": self.reason,
            "parse_source": self.parse_source,
            "detected_format": self.detected_format,
            "bytes_fetched": self.bytes_fetched,
            "sections": self.sections,
            "figures": self.figures,
            "tables": self.tables,
            "equations": self.equations,
            "duration_seconds": self.duration_seconds,
            "output_path": self.output_path,
            "pdf_sidecar_status": self.pdf_sidecar_status,
            "pdf_sidecar_reason": self.pdf_sidecar_reason,
            "pdf_sidecar_bytes": self.pdf_sidecar_bytes,
            "pdf_sidecar_visual_merged_figures": self.pdf_sidecar_visual_merged_figures,
            "pdf_sidecar_visual_merged_tables": self.pdf_sidecar_visual_merged_tables,
        }


@dataclass(frozen=True)
class PaperIngestReport:
    papers_dir: Path
    requested: int
    succeeded: int
    skipped: int
    failed: int
    items: tuple[PaperIngestItem, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "papers_dir": str(self.papers_dir),
            "requested": self.requested,
            "succeeded": self.succeeded,
            "skipped": self.skipped,
            "failed": self.failed,
            "items": [item.to_dict() for item in self.items],
        }


def ingest_benchmark_papers(
    arxiv_ids: Sequence[str],
    *,
    papers_dir: Path,
    force: bool = False,
    max_papers: int | None = None,
    manifest_path: Path | None = None,
    fetcher: SourcePackageFetcher | None = None,
    parser: DocumentParser | None = None,
    pdf_parser_backend: str | None = None,
    with_pdf_sidecar: bool = False,
    pdf_sidecar_mode: str = "missing",
    merge_pdf_visuals: bool = True,
) -> PaperIngestReport:
    load_root_env()
    _configure_paper_artifact_root(papers_dir)
    ids = _normalize_ids(arxiv_ids)
    if max_papers is not None:
        ids = ids[:max(0, max_papers)]
    fetcher = fetcher or _default_source_fetcher()
    parser = parser or ArxivDocumentParser(pdf_parser_backend=pdf_parser_backend)

    items: list[PaperIngestItem] = []
    for arxiv_id in ids:
        paper_id = _paper_id(arxiv_id)
        start = time.perf_counter()
        output_path = papers_dir / paper_id / "research_document.json"
        if output_path.exists() and not force:
            items.append(PaperIngestItem(
                arxiv_id=arxiv_id,
                paper_id=paper_id,
                status="skipped",
                reason="research_document_exists",
                output_path=str(output_path),
                duration_seconds=_elapsed(start),
            ))
            continue
        try:
            package = fetcher.fetch_source_package(arxiv_id)
            source_format, _canonical = detect_source_format(package.content)
            document = parser.parse(paper_id, package.content)
            _copy_original_pdf_if_available(package.content, papers_dir / paper_id, paper_id)
            sidecar = _maybe_merge_pdf_sidecar(
                document,
                arxiv_id=arxiv_id,
                paper_id=paper_id,
                papers_dir=papers_dir,
                fetcher=fetcher,
                enabled=with_pdf_sidecar,
                mode=pdf_sidecar_mode,
                merge_pdf_visuals=merge_pdf_visuals,
            )
            document = sidecar.document
            _write_document(document, output_path)
        except Exception as exc:  # noqa: BLE001 - batch ingest records failures and continues
            items.append(PaperIngestItem(
                arxiv_id=arxiv_id,
                paper_id=paper_id,
                status="failed",
                reason=f"{type(exc).__name__}: {exc}",
                output_path=str(output_path),
                duration_seconds=_elapsed(start),
            ))
            continue
        items.append(PaperIngestItem(
            arxiv_id=arxiv_id,
            paper_id=paper_id,
            status="succeeded",
            parse_source=str(document.metadata.get("parse_source") or ""),
            detected_format=source_format.value,
            bytes_fetched=len(package.content),
            sections=len(document.sections),
            figures=len(document.figures),
            tables=len(document.tables),
            equations=len(document.equations),
            output_path=str(output_path),
            duration_seconds=_elapsed(start),
            pdf_sidecar_status=sidecar.status,
            pdf_sidecar_reason=sidecar.reason,
            pdf_sidecar_bytes=sidecar.bytes_fetched,
            pdf_sidecar_visual_merged_figures=sidecar.visual_merged_figures,
            pdf_sidecar_visual_merged_tables=sidecar.visual_merged_tables,
        ))

    report = PaperIngestReport(
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
        ids = list(DEFAULT_BENCHMARK_ARXIV_IDS)
    report = ingest_benchmark_papers(
        ids,
        papers_dir=Path(args.papers_dir),
        force=args.force,
        max_papers=args.max_papers,
        manifest_path=Path(args.manifest) if args.manifest else None,
        with_pdf_sidecar=args.with_pdf_sidecar,
        pdf_sidecar_mode=args.pdf_sidecar_mode,
        merge_pdf_visuals=not args.no_merge_pdf_visuals,
        pdf_parser_backend=args.pdf_parser_backend,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), end="\n")
    return 0 if report.failed == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.research.rag.evaluation.paper_benchmark_ingest",
        description="Fetch and parse real arXiv papers into .newsroom/papers for the RAG benchmark suite.",
    )
    parser.add_argument("arxiv_id", nargs="*", help="arXiv ids or URLs. Defaults to a mixed-domain seed set.")
    parser.add_argument("--ids-file", help="Optional newline-delimited arXiv id file.")
    parser.add_argument("--papers-dir", default=".newsroom/papers")
    parser.add_argument("--manifest", default=".newsroom/eval/paper-rag-ingest-manifest.json")
    parser.add_argument("--max-papers", type=int)
    parser.add_argument("--force", action="store_true", help="Re-parse papers even when research_document.json exists.")
    parser.add_argument(
        "--pdf-parser-backend",
        choices=("nougat", "marker", "cascade"),
        default=None,
        help="PDF parser backend for PDF source packages. Defaults to NEWSROOM_PDF_PARSER_BACKEND or nougat.",
    )
    parser.add_argument(
        "--with-pdf-sidecar",
        action="store_true",
        help="Fetch the arXiv PDF after source parsing and merge Surya/PyMuPDF visual artifacts.",
    )
    parser.add_argument(
        "--pdf-sidecar-mode",
        choices=("missing", "always"),
        default="missing",
        help="When to fetch the PDF sidecar. 'missing' only fetches when figure/table images are incomplete.",
    )
    parser.add_argument(
        "--no-merge-pdf-visuals",
        action="store_true",
        help="Fetch/copy the PDF sidecar but do not merge visual crops into research_document.json.",
    )
    return parser


def _configure_paper_artifact_root(papers_dir: Path) -> None:
    root = papers_dir.parent / "runs"
    os.environ.setdefault("NEWS_ARTIFACT_ROOT", str(root))
    os.environ.setdefault("NEWSROOM_PDF_WRITE_ARTIFACTS", "1")


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


def _default_source_fetcher() -> SourcePackageFetcher:
    return UrllibArxivSourcePackageFetcher()


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


def _read_ids_file(path: Path) -> list[str]:
    ids = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            ids.append(value)
    return ids


def _paper_id(arxiv_id: str) -> str:
    return _normalize_arxiv_id(arxiv_id).replace("/", "_")


def _write_document(document: ResearchDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _copy_original_pdf_if_available(source_bytes: bytes, paper_dir: Path, paper_id: str) -> None:
    if not source_bytes.startswith(b"%PDF"):
        return
    pdf_path = paper_dir / f"{paper_id}_original.pdf"
    if pdf_path.exists():
        return
    paper_dir.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(source_bytes)
    dotted = paper_id.replace("_", ".")
    if dotted != paper_id:
        dotted_path = paper_dir / f"{dotted}_original.pdf"
        if not dotted_path.exists():
            shutil.copyfile(pdf_path, dotted_path)


@dataclass(frozen=True)
class _PdfSidecarResult:
    document: ResearchDocument
    status: str
    reason: str = ""
    bytes_fetched: int = 0
    visual_merged_figures: int = 0
    visual_merged_tables: int = 0


def _maybe_merge_pdf_sidecar(
    document: ResearchDocument,
    *,
    arxiv_id: str,
    paper_id: str,
    papers_dir: Path,
    fetcher: Any,
    enabled: bool,
    mode: str,
    merge_pdf_visuals: bool,
) -> _PdfSidecarResult:
    if not enabled:
        return _PdfSidecarResult(document=document, status="disabled")
    normalized_mode = str(mode or "missing").strip().casefold()
    if normalized_mode == "missing" and not _has_missing_visual_images(document):
        return _PdfSidecarResult(document=document, status="skipped", reason="visual_images_already_present")
    fetch_pdf_package = getattr(fetcher, "fetch_pdf_package", None)
    if not callable(fetch_pdf_package):
        return _PdfSidecarResult(document=document, status="failed", reason="fetcher_missing_fetch_pdf_package")
    try:
        pdf_package = fetch_pdf_package(arxiv_id)
        pdf_bytes = bytes(pdf_package.content)
        _copy_original_pdf_if_available(pdf_bytes, papers_dir / paper_id, paper_id)
        if not merge_pdf_visuals:
            return _PdfSidecarResult(
                document=document,
                status="fetched",
                reason="merge_disabled",
                bytes_fetched=len(pdf_bytes),
            )
        merged = merge_pdf_visual_sidecar(document, pdf_bytes, paper_id=paper_id)
        return _PdfSidecarResult(
            document=merged,
            status="merged",
            bytes_fetched=len(pdf_bytes),
            visual_merged_figures=sum(1 for figure in merged.figures if figure.image_ref),
            visual_merged_tables=sum(1 for table in merged.tables if table.metadata.get("image_ref")),
        )
    except Exception as exc:  # noqa: BLE001 - sidecar should not fail the main source parse
        return _PdfSidecarResult(
            document=document,
            status="failed",
            reason=f"{type(exc).__name__}: {exc}",
        )


def _has_missing_visual_images(document: ResearchDocument) -> bool:
    return any(not figure.image_ref for figure in document.figures) or any(
        not table.metadata.get("image_ref") for table in document.tables
    )


def _elapsed(start: float) -> float:
    return round(time.perf_counter() - start, 3)


if __name__ == "__main__":
    raise SystemExit(main())
