from __future__ import annotations

import inspect
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Sequence

import fitz

from backend.foundation import build_stable_id
from backend.research.document.latex_compiler import LatexSourceParser
from backend.research.document.source_format import SourceFormat, detect_source_format
from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument, ResearchSection
from backend.research.ports.document_parser import DocumentParserPort
from framework.execution_environment.errors import ExecutionEnvironmentError


@dataclass(frozen=True)
class ParserAttempt:
    backend: str
    status: str
    reason: str | None = None
    elapsed_ms: float = 0.0
    quality: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "status": self.status,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 3),
            **({"quality": self.quality} if self.quality is not None else {}),
        }


@dataclass(frozen=True)
class QualityProbeResult:
    passed: bool
    reason: str | None
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "metrics": self.metrics,
        }


class PdfTextExtractionError(ValueError):
    """A PDF cannot be admitted as trustworthy selectable text."""

    def __init__(
        self,
        reason: str,
        *,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.metrics = dict(metrics or {})


@dataclass(frozen=True)
class DocumentQualityProbe:
    min_sections: int = 3
    min_body_chars: int = 3000
    min_non_empty_section_ratio: float = 0.8
    max_replacement_char_ratio: float = 0.02
    min_table_row_coverage_if_tables: float = 0.5

    @classmethod
    def from_env(cls) -> "DocumentQualityProbe":
        return cls(
            min_sections=_env_int("NEWSROOM_PARSER_QUALITY_MIN_SECTIONS", 3),
            min_body_chars=_env_int("NEWSROOM_PARSER_QUALITY_MIN_BODY_CHARS", 3000),
            min_non_empty_section_ratio=_env_float(
                "NEWSROOM_PARSER_QUALITY_MIN_NON_EMPTY_SECTION_RATIO",
                0.8,
            ),
            max_replacement_char_ratio=_env_float(
                "NEWSROOM_PARSER_QUALITY_MAX_REPLACEMENT_CHAR_RATIO",
                0.02,
            ),
            min_table_row_coverage_if_tables=_env_float(
                "NEWSROOM_PARSER_QUALITY_MIN_TABLE_ROW_COVERAGE",
                0.5,
            ),
        )

    def evaluate(self, document: ResearchDocument) -> QualityProbeResult:
        section_count = len(document.sections)
        body_chars = sum(len(section.text or "") for section in document.sections)
        non_empty_sections = sum(1 for section in document.sections if (section.text or "").strip())
        non_empty_ratio = non_empty_sections / section_count if section_count else 0.0
        text = "\n".join(section.text or "" for section in document.sections)
        replacement_ratio = (text.count("\ufffd") / len(text)) if text else 0.0
        table_count = len(document.tables)
        tables_with_rows = sum(1 for table in document.tables if table.rows)
        table_row_coverage = (tables_with_rows / table_count) if table_count else 1.0
        metrics = {
            "sections_count": section_count,
            "body_char_count": body_chars,
            "non_empty_section_ratio": round(non_empty_ratio, 4),
            "replacement_char_ratio": round(replacement_ratio, 4),
            "tables_detected": table_count,
            "tables_with_rows": tables_with_rows,
            "table_row_coverage": round(table_row_coverage, 4),
            "thresholds": {
                "min_sections": self.min_sections,
                "min_body_chars": self.min_body_chars,
                "min_non_empty_section_ratio": self.min_non_empty_section_ratio,
                "max_replacement_char_ratio": self.max_replacement_char_ratio,
                "min_table_row_coverage_if_tables": self.min_table_row_coverage_if_tables,
            },
        }
        if section_count < self.min_sections:
            return QualityProbeResult(False, "sections_below_threshold", metrics)
        if body_chars < self.min_body_chars:
            return QualityProbeResult(False, "body_chars_below_threshold", metrics)
        if non_empty_ratio < self.min_non_empty_section_ratio:
            return QualityProbeResult(False, "non_empty_section_ratio_below_threshold", metrics)
        if replacement_ratio > self.max_replacement_char_ratio:
            return QualityProbeResult(False, "replacement_char_ratio_above_threshold", metrics)
        if table_count and table_row_coverage < self.min_table_row_coverage_if_tables:
            return QualityProbeResult(False, "table_row_coverage_below_threshold", metrics)
        return QualityProbeResult(True, None, metrics)


class PyMuPDFTextDocumentParser:
    """Extract native page text without OCR or external processes."""

    def __init__(self, *, max_pages: int = 500, max_text_chars: int = 2_000_000) -> None:
        if isinstance(max_pages, bool) or not 1 <= int(max_pages) <= 5_000:
            raise ValueError("max_pages must be between 1 and 5000")
        if isinstance(max_text_chars, bool) or not 1_000 <= int(max_text_chars) <= 10_000_000:
            raise ValueError("max_text_chars must be between 1000 and 10000000")
        self._max_pages = int(max_pages)
        self._max_text_chars = int(max_text_chars)

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        source_ref = f"paper://{paper_id}/pdf"
        source_hash = sha256(source_bytes).hexdigest()
        sections: list[ResearchSection] = []
        extracted_chars = 0
        try:
            doc = fitz.open(stream=source_bytes, filetype="pdf")
        except Exception as exc:
            raise PdfTextExtractionError("pdf_open_failed") from exc
        try:
            if doc.needs_pass:
                raise PdfTextExtractionError("pdf_password_required")
            if doc.page_count > self._max_pages:
                raise PdfTextExtractionError(
                    "pdf_page_limit_exceeded",
                    metrics={"pages": doc.page_count, "max_pages": self._max_pages},
                )
            for page_index, page in enumerate(doc, start=1):
                text = page.get_text("text", sort=True).strip()
                if not text:
                    continue
                extracted_chars += len(text)
                if extracted_chars > self._max_text_chars:
                    raise PdfTextExtractionError(
                        "pdf_text_limit_exceeded",
                        metrics={
                            "text_chars": extracted_chars,
                            "max_text_chars": self._max_text_chars,
                        },
                    )
                locator = f"{source_ref}#page={page_index}"
                sections.append(ResearchSection(
                    section_id=build_stable_id("sec", paper_id, "pymupdf", str(page_index)),
                    title=f"PDF Text Page {page_index}",
                    level=1,
                    text=text,
                    page_start=page_index,
                    page_end=page_index,
                    source_ref=locator,
                    metadata={
                        "parse_source": "pymupdf",
                        "source_locator": locator,
                    },
                ))
        except PdfTextExtractionError:
            raise
        except Exception as exc:
            raise PdfTextExtractionError("pdf_text_extraction_failed") from exc
        finally:
            doc.close()
        if not sections:
            raise PdfTextExtractionError("pdf_no_extractable_text")
        return ResearchDocument(
            paper_id=paper_id,
            source_hash=source_hash,
            sections=sections,
            lineage=SourceLineage(source_refs=[source_ref], source_hash=source_hash),
            metadata={
                "parse_source": "pymupdf",
                "parser_backend": "pymupdf",
                "parse_quality": {
                    "sections": {
                        "total": len(sections),
                        "with_page_bounds": len(sections),
                        "with_source_locator": len(sections),
                    },
                    "figures": {"total": 0},
                    "tables": {"total": 0},
                    "equations": {"total": 0},
                },
            },
        )


class SelectableTextPdfDocumentParser:
    """Admit only bounded PDFs whose embedded text passes deterministic quality checks."""

    def __init__(
        self,
        *,
        extractor: DocumentParserPort | None = None,
        probe: DocumentQualityProbe | None = None,
    ) -> None:
        self._extractor = extractor or PyMuPDFTextDocumentParser()
        self._probe = probe or DocumentQualityProbe.from_env()

    def parse(self, paper_id: str, source_bytes: bytes) -> ResearchDocument:
        document = self._extractor.parse(paper_id, source_bytes)
        quality = self._probe.evaluate(document)
        if not quality.passed:
            raise PdfTextExtractionError(
                quality.reason or "pdf_text_quality_rejected",
                metrics=quality.metrics,
            )
        metadata = {
            **dict(document.metadata),
            "parse_source": "pymupdf",
            "parser_backend": "pymupdf",
            "text_only": True,
            "compiled_reader_ready": False,
            "text_admission": quality.to_dict(),
        }
        return document.model_copy(
            update={
                "parser_backend": "pymupdf",
                "parser_version": str(fitz.version[0]),
                "normalization_version": "native-page-text-v1",
                "metadata": metadata,
            }
        )


class CascadeDocumentParser:
    """PDF parser cascade with deterministic quality gates."""

    def __init__(
        self,
        backends: Sequence[tuple[str, DocumentParserPort]],
        probe: DocumentQualityProbe | None = None,
        fallback: DocumentParserPort | None = None,
    ) -> None:
        if not backends:
            raise ValueError("CascadeDocumentParser requires at least one backend")
        self._backends = tuple(backends)
        self._probe = probe or DocumentQualityProbe.from_env()
        self._fallback = fallback or PyMuPDFTextDocumentParser()

    def parse(
        self,
        paper_id: str,
        source_bytes: bytes,
        *,
        execution_identity: Any | None = None,
    ) -> ResearchDocument:
        attempts: list[ParserAttempt] = []
        for backend, parser in self._backends:
            started = time.perf_counter()
            try:
                document = _parse_with_execution_identity(
                    parser,
                    paper_id,
                    source_bytes,
                    execution_identity=execution_identity,
                )
            except ExecutionEnvironmentError:
                raise
            except Exception as exc:  # noqa: BLE001 - cascade records and falls through
                attempts.append(ParserAttempt(
                    backend=backend,
                    status="parse_error",
                    reason=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=_elapsed_ms(started),
                ))
                continue
            quality = self._probe.evaluate(document)
            if quality.passed:
                attempts.append(ParserAttempt(
                    backend=backend,
                    status="success",
                    elapsed_ms=_elapsed_ms(started),
                    quality=quality.to_dict(),
                ))
                return _with_cascade_metadata(
                    document,
                    used_backend=backend,
                    degraded=False,
                    attempts=attempts,
                )
            attempts.append(ParserAttempt(
                backend=backend,
                status="quality_rejected",
                reason=quality.reason,
                elapsed_ms=_elapsed_ms(started),
                quality=quality.to_dict(),
            ))

        started = time.perf_counter()
        document = _parse_with_execution_identity(
            self._fallback,
            paper_id,
            source_bytes,
            execution_identity=execution_identity,
        )
        quality = self._probe.evaluate(document)
        attempts.append(ParserAttempt(
            backend="pymupdf",
            status="fallback",
            reason="all_configured_backends_failed_or_rejected",
            elapsed_ms=_elapsed_ms(started),
            quality=quality.to_dict(),
        ))
        return _with_cascade_metadata(
            document,
            used_backend="pymupdf",
            degraded=True,
            attempts=attempts,
        )


class CascadeArxivDocumentParser:
    """Document parser that keeps LaTeX routing and cascades only PDF bytes."""

    def __init__(
        self,
        latex_parser: LatexSourceParser | None = None,
        pdf_parser: CascadeDocumentParser | None = None,
    ) -> None:
        self._latex = latex_parser or LatexSourceParser()
        self._pdf = pdf_parser or build_default_pdf_cascade_parser()

    def parse(
        self,
        paper_id: str,
        source_bytes: bytes,
        *,
        execution_identity: Any | None = None,
    ) -> ResearchDocument:
        fmt, canonical = detect_source_format(source_bytes)
        if fmt is SourceFormat.PDF:
            return self._pdf.parse(
                paper_id,
                canonical,
                execution_identity=execution_identity,
            )
        if fmt in (SourceFormat.HTML, SourceFormat.ZIP, SourceFormat.UNKNOWN):
            raise NotImplementedError(
                f"CascadeArxivDocumentParser does not support format '{fmt.value}' — "
                "add a dedicated parser for this source type."
            )
        return self._latex.parse(paper_id, source_bytes)


def build_default_pdf_cascade_parser() -> CascadeDocumentParser:
    from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser
    from backend.research.document.mineru_pdf_parser import MinerUPdfDocumentParser

    factories: dict[str, Any] = {
        "mineru": MinerUPdfDocumentParser,
        "marker": MarkerPdfDocumentParser,
    }
    backends: list[tuple[str, DocumentParserPort]] = []
    for name in parser_cascade_backend_names():
        if name == "pymupdf":
            continue
        factory = factories.get(name)
        if factory is None:
            raise ValueError(
                "NEWSROOM_PDF_PARSER_CASCADE must contain only: mineru, marker, pymupdf"
            )
        backends.append((name, factory()))
    return CascadeDocumentParser(backends=backends, probe=DocumentQualityProbe.from_env())


def parser_cascade_backend_names(value: str | None = None) -> tuple[str, ...]:
    raw = (value or os.environ.get("NEWSROOM_PDF_PARSER_CASCADE") or "mineru,marker").strip()
    names = tuple(
        part.strip().lower()
        for part in raw.split(",")
        if part.strip()
    )
    if not names:
        raise ValueError("NEWSROOM_PDF_PARSER_CASCADE must contain at least one backend")
    allowed = {"mineru", "marker", "pymupdf"}
    unknown = sorted(set(names) - allowed)
    if unknown:
        raise ValueError(
            "NEWSROOM_PDF_PARSER_CASCADE must contain only: mineru, marker, pymupdf"
        )
    structured = tuple(name for name in names if name != "pymupdf")
    if not structured:
        raise ValueError("NEWSROOM_PDF_PARSER_CASCADE must include mineru or marker before fallback")
    return names


def _with_cascade_metadata(
    document: ResearchDocument,
    *,
    used_backend: str,
    degraded: bool,
    attempts: list[ParserAttempt],
) -> ResearchDocument:
    metadata = dict(document.metadata)
    metadata["parse_source"] = used_backend
    metadata["parser_backend"] = used_backend
    metadata["parser_cascade"] = {
        "used_backend": used_backend,
        "degraded": degraded,
        "attempts": [attempt.to_dict() for attempt in attempts],
    }
    if degraded:
        metadata["degraded"] = True
    return document.model_copy(
        update={
            "metadata": metadata,
            **({"status": "degraded"} if degraded else {}),
        }
    )


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _parse_with_execution_identity(
    parser: DocumentParserPort,
    paper_id: str,
    source_bytes: bytes,
    *,
    execution_identity: Any | None,
) -> ResearchDocument:
    parse_method = parser.parse
    if _accepts_keyword(parse_method, "execution_identity"):
        return parse_method(
            paper_id,
            source_bytes,
            execution_identity=execution_identity,
        )
    return parse_method(paper_id, source_bytes)


def _accepts_keyword(callable_value: Any, keyword: str) -> bool:
    try:
        parameter = inspect.signature(callable_value).parameters.get(keyword)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


__all__ = [
    "CascadeArxivDocumentParser",
    "CascadeDocumentParser",
    "DocumentQualityProbe",
    "ParserAttempt",
    "PdfTextExtractionError",
    "PyMuPDFTextDocumentParser",
    "QualityProbeResult",
    "SelectableTextPdfDocumentParser",
    "build_default_pdf_cascade_parser",
    "parser_cascade_backend_names",
]
