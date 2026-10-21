from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, Sequence

from backend.research.document.pdf_parser_backend import build_pdf_document_parser
from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument


class PdfUrlFetcher(Protocol):
    def fetch_pdf(self, url: str) -> bytes:
        ...


class PdfParser(Protocol):
    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        ...


@dataclass(frozen=True)
class PdfUrlIngestSource:
    source_id: str
    paper_id: str
    url: str


@dataclass(frozen=True)
class ParserUrlBakeoffIngestItem:
    source_id: str
    paper_id: str
    url: str
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
            "source_id": self.source_id,
            "paper_id": self.paper_id,
            "url": self.url,
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
class ParserUrlBakeoffIngestReport:
    backend: str
    papers_dir: Path
    requested: int
    succeeded: int
    skipped: int
    failed: int
    items: tuple[ParserUrlBakeoffIngestItem, ...] = field(default_factory=tuple)

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


class UrllibPdfUrlFetcher:
    def __init__(self, *, timeout_seconds: int = 120) -> None:
        self._timeout_seconds = timeout_seconds

    def fetch_pdf(self, url: str) -> bytes:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "AgoraHubResearch/0.1 (parser bakeoff)",
                "Accept": "application/pdf,*/*",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
            payload = response.read()
        if not payload.startswith(b"%PDF"):
            raise ValueError(f"URL did not return PDF bytes: {url}")
        return payload


def ingest_parser_bakeoff_pdf_urls(
    sources: Sequence[str | PdfUrlIngestSource],
    *,
    backend: str,
    papers_dir: Path,
    force: bool = False,
    max_papers: int | None = None,
    manifest_path: Path | None = None,
    fetcher: PdfUrlFetcher | None = None,
    parser: PdfParser | None = None,
) -> ParserUrlBakeoffIngestReport:
    normalized_sources = _normalize_sources(sources)
    if max_papers is not None:
        normalized_sources = normalized_sources[:max(0, max_papers)]
    fetcher = fetcher or UrllibPdfUrlFetcher()
    parser = parser or build_pdf_document_parser(backend)

    items: list[ParserUrlBakeoffIngestItem] = []
    for source in normalized_sources:
        output_path = papers_dir / source.paper_id / "research_document.json"
        start = time.perf_counter()
        if output_path.exists() and not force:
            items.append(ParserUrlBakeoffIngestItem(
                source_id=source.source_id,
                paper_id=source.paper_id,
                url=source.url,
                status="skipped",
                backend=backend,
                reason="research_document_exists",
                duration_seconds=_elapsed(start),
                output_path=str(output_path),
            ))
            continue
        try:
            pdf_bytes = fetcher.fetch_pdf(source.url)
            document = parser.parse(source.paper_id, pdf_bytes)
            document = _with_url_source_metadata(document, source)
            _write_document(document, output_path)
            _copy_original_pdf(pdf_bytes, output_path.parent, source.paper_id)
        except Exception as exc:  # noqa: BLE001 - batch ingest records failures and continues
            items.append(ParserUrlBakeoffIngestItem(
                source_id=source.source_id,
                paper_id=source.paper_id,
                url=source.url,
                status="failed",
                backend=backend,
                reason=f"{type(exc).__name__}: {exc}",
                duration_seconds=_elapsed(start),
                output_path=str(output_path),
            ))
            continue
        items.append(ParserUrlBakeoffIngestItem(
            source_id=source.source_id,
            paper_id=source.paper_id,
            url=source.url,
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

    report = ParserUrlBakeoffIngestReport(
        backend=backend,
        papers_dir=papers_dir,
        requested=len(normalized_sources),
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


def acl_long_pdf_sources(year: int, start: int, count: int) -> list[PdfUrlIngestSource]:
    if count < 0:
        raise ValueError("count must be non-negative")
    if start <= 0:
        raise ValueError("start must be positive")
    return [
        _source_from_acl_id(f"{year}.acl-long.{index}")
        for index in range(start, start + count)
    ]


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    raw_sources = list(args.source)
    if args.sources_file:
        raw_sources.extend(_read_sources_file(Path(args.sources_file)))
    if args.acl_long_year:
        raw_sources.extend(acl_long_pdf_sources(
            year=args.acl_long_year,
            start=args.acl_long_start,
            count=args.acl_long_count,
        ))
    if not raw_sources:
        raise ValueError("provide PDF URLs/source ids, --sources-file, or --acl-long-year")
    report = ingest_parser_bakeoff_pdf_urls(
        raw_sources,
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
        prog="python -m backend.research.rag.evaluation.paper_parser_url_bakeoff_ingest",
        description="Fetch arbitrary PDF URLs and parse them with one PDF parser backend.",
    )
    parser.add_argument("source", nargs="*", help="PDF URL, ACL Anthology id, or SOURCE_ID=URL entry.")
    parser.add_argument("--sources-file", help="Optional newline-delimited PDF URL/source list.")
    parser.add_argument("--acl-long-year", type=int, help="Generate ACL long ids, for example 2025.")
    parser.add_argument("--acl-long-start", type=int, default=1)
    parser.add_argument("--acl-long-count", type=int, default=30)
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


def _normalize_sources(sources: Sequence[str | PdfUrlIngestSource]) -> list[PdfUrlIngestSource]:
    out: list[PdfUrlIngestSource] = []
    seen: set[str] = set()
    for source in sources:
        normalized = source if isinstance(source, PdfUrlIngestSource) else _source_from_raw(source)
        if normalized.url in seen:
            continue
        seen.add(normalized.url)
        out.append(normalized)
    return out


def _source_from_raw(raw: str) -> PdfUrlIngestSource:
    value = raw.split("#", 1)[0].strip()
    if not value:
        raise ValueError("empty source entry")
    if "=" in value and not value.startswith(("http://", "https://")):
        source_id, url = value.split("=", 1)
        source_id = source_id.strip()
        url = url.strip()
        if not source_id or not url:
            raise ValueError(f"invalid SOURCE_ID=URL entry: {raw!r}")
        return PdfUrlIngestSource(
            source_id=source_id,
            paper_id=_safe_paper_id(source_id),
            url=_normalize_pdf_url(url),
        )
    if _looks_like_acl_id(value):
        return _source_from_acl_id(value)
    url = _normalize_pdf_url(value)
    source_id = _source_id_from_url(url)
    return PdfUrlIngestSource(
        source_id=source_id,
        paper_id=_safe_paper_id(source_id),
        url=url,
    )


def _source_from_acl_id(acl_id: str) -> PdfUrlIngestSource:
    value = acl_id.strip().removesuffix(".pdf")
    if not _looks_like_acl_id(value):
        raise ValueError(f"invalid ACL Anthology id: {acl_id!r}")
    return PdfUrlIngestSource(
        source_id=value,
        paper_id=_safe_paper_id(value),
        url=f"https://aclanthology.org/{value}.pdf",
    )


def _looks_like_acl_id(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", value.strip()))


def _normalize_pdf_url(value: str) -> str:
    url = value.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"expected http(s) PDF URL: {value!r}")
    return url


def _source_id_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.strip("/")
    stem = Path(path).name.removesuffix(".pdf") or parsed.netloc
    if parsed.netloc.endswith("aclanthology.org") and _looks_like_acl_id(stem):
        return stem
    return f"{parsed.netloc}/{path or stem}".strip("/")


def _safe_paper_id(value: str) -> str:
    return re.sub(r"[^\w\-]+", "_", value.replace(".", "_")).strip("_")


def _read_sources_file(path: Path) -> list[str]:
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            values.append(value)
    return values


def _with_url_source_metadata(
    document: ResearchDocument,
    source: PdfUrlIngestSource,
) -> ResearchDocument:
    original_ref = f"arxiv://{document.paper_id}/pdf"
    source_ref = f"pdf-url://{source.source_id}"
    payload = document.model_dump(mode="python")
    payload["lineage"] = SourceLineage(
        source_refs=[source_ref],
        source_hash=document.source_hash,
    ).model_dump(mode="python")
    metadata = dict(payload.get("metadata") or {})
    metadata.update({
        "source_type": "pdf_url",
        "source_id": source.source_id,
        "source_url": source.url,
        "pdf_url_ingest": True,
    })
    payload["metadata"] = metadata
    for collection_name in ("sections", "figures", "tables", "equations", "references"):
        for item in payload.get(collection_name) or []:
            if not isinstance(item, dict):
                continue
            item["source_ref"] = _replace_source_ref(str(item.get("source_ref") or ""), original_ref, source_ref)
            item_metadata = dict(item.get("metadata") or {})
            if item_metadata.get("source_locator"):
                item_metadata["source_locator"] = _replace_source_ref(
                    str(item_metadata["source_locator"]),
                    original_ref,
                    source_ref,
                )
            item_metadata.setdefault("source_id", source.source_id)
            item_metadata.setdefault("source_url", source.url)
            item["metadata"] = item_metadata
    return ResearchDocument.model_validate(payload)


def _replace_source_ref(value: str, original_ref: str, source_ref: str) -> str:
    if value.startswith(original_ref):
        return source_ref + value[len(original_ref):]
    return value


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
