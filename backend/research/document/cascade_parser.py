from __future__ import annotations

import inspect
import os
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Mapping, Sequence

from backend.foundation import build_stable_id
from backend.research.document.docker_pdf_parser import page_rects
from backend.research.document.latex_compiler import LatexSourceParser
from backend.research.document.pymupdf_loader import load_pymupdf
from backend.research.document.recovery_policy import (
    DocumentRecoveryPolicy,
    RecoveryCandidateRecord,
    RecoveryCandidateStatus,
    RecoveryEvidenceSnapshot,
    RecoveryKind,
    RecoveryRequest,
    evaluate_parser_candidate,
)
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
    audit: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "status": self.status,
            "reason": self.reason,
            "elapsed_ms": round(self.elapsed_ms, 3),
            **({"quality": self.quality} if self.quality is not None else {}),
            **({"audit": self.audit} if self.audit is not None else {}),
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
        fidelity = document.fidelity
        fidelity_source_matches = (
            fidelity is None or fidelity.source_hash == document.source_hash
        )
        cascade = document.metadata.get("parser_cascade")
        cascade_degraded = isinstance(cascade, dict) and cascade.get("degraded") is True
        fidelity_unavailable = str(document.metadata.get("fidelity_status") or "").casefold() == "unavailable"
        parser_warning_codes = document.metadata.get("parser_warning_codes")
        parser_warning_codes = parser_warning_codes if isinstance(parser_warning_codes, list) else []
        missing_formula_sources = sum(
            isinstance(item, dict) and item.get("code") == "formula_source_missing"
            for item in parser_warning_codes
        )
        if not parser_warning_codes:
            parser_warnings = document.metadata.get("parser_warnings")
            parser_warnings = parser_warnings if isinstance(parser_warnings, list) else []
            missing_formula_sources = sum(
                "equation block has no usable source content" in str(warning)
                for warning in parser_warnings
            )
        parser_attempts = document.parser_attempts or document.metadata.get("parser_attempts", [])
        parser_attempts = parser_attempts if isinstance(parser_attempts, list) else []
        metrics = {
            "quality_policy_version": "research-document-quality-policy/v1",
            "dimensions": {
                "execution": {
                    "status": "degraded" if cascade_degraded or document.status == "degraded" else "completed",
                    "used_backend": document.parser_backend,
                    "attempt_count": len(parser_attempts),
                    "cascade_degraded": cascade_degraded,
                },
                "structure_and_source": {
                    "status": (
                        "quarantined" if not fidelity_source_matches
                        else "degraded" if fidelity_unavailable
                        else "validated" if fidelity is not None
                        else "unknown"
                    ),
                    "source_hash_matches": fidelity_source_matches,
                    "fidelity_schema_version": fidelity.schema_version if fidelity is not None else None,
                    "page_count": len(fidelity.pages) if fidelity is not None else None,
                    "block_count": len(fidelity.blocks) if fidelity is not None else None,
                    "reading_order_uncertain_blocks": (
                        sum(block.order_status == "uncertain" for block in fidelity.blocks)
                        if fidelity is not None else None
                    ),
                    "unknown_geometry_blocks": (
                        sum(block.bbox is None for block in fidelity.blocks)
                        if fidelity is not None else None
                    ),
                    "fidelity_unavailable": fidelity_unavailable,
                    "missing_formula_source_count": missing_formula_sources,
                },
                "content": {"status": "measured"},
            },
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
        def result(passed: bool, reason: str | None) -> QualityProbeResult:
            metrics["dimensions"]["content"]["status"] = "passed" if passed else "degraded"
            metrics["dimensions"]["content"]["reason"] = reason
            return QualityProbeResult(passed, reason, metrics)

        if fidelity_unavailable:
            return result(False, "source_fidelity_unavailable")
        if missing_formula_sources:
            return result(False, "formula_source_missing")
        if section_count < self.min_sections:
            return result(False, "sections_below_threshold")
        if body_chars < self.min_body_chars:
            return result(False, "body_chars_below_threshold")
        if non_empty_ratio < self.min_non_empty_section_ratio:
            return result(False, "non_empty_section_ratio_below_threshold")
        if replacement_ratio > self.max_replacement_char_ratio:
            return result(False, "replacement_char_ratio_above_threshold")
        if table_count and table_row_coverage < self.min_table_row_coverage_if_tables:
            return result(False, "table_row_coverage_below_threshold")
        if not fidelity_source_matches:
            return result(False, "fidelity_source_hash_mismatch")
        return result(True, None)


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
        # Resolved outside the open guard: a missing PyMuPDF is an install error, not a bad PDF.
        pymupdf = load_pymupdf()
        try:
            doc = pymupdf.open(stream=source_bytes, filetype="pdf")
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
                "parser_version": str(load_pymupdf().version[0]),
                "normalization_version": "native-page-text-v1",
                "metadata": metadata,
            }
        )


class CascadeDocumentParser:
    """PDF parser cascade with deterministic quality and recovery gates."""

    def __init__(
        self,
        backends: Sequence[tuple[str, DocumentParserPort]],
        probe: DocumentQualityProbe | None = None,
        fallback: DocumentParserPort | None = None,
        *,
        recovery_policy: DocumentRecoveryPolicy | None = None,
        backend_provider_ids: Mapping[str, str] | None = None,
        backend_profile_ids: Mapping[str, str] | None = None,
    ) -> None:
        if not backends:
            raise ValueError("CascadeDocumentParser requires at least one backend")
        self._backends = tuple(backends)
        self._probe = probe or DocumentQualityProbe.from_env()
        self._fallback = fallback or PyMuPDFTextDocumentParser()
        self._recovery_policy = recovery_policy
        self._backend_provider_ids = {
            str(key).strip().lower(): str(value).strip().lower()
            for key, value in dict(backend_provider_ids or {}).items()
        }
        self._backend_profile_ids = {
            str(key).strip().lower(): str(value).strip().lower()
            for key, value in dict(backend_profile_ids or {}).items()
        }

    def parse(
        self,
        paper_id: str,
        source_bytes: bytes,
        *,
        execution_identity: Any | None = None,
    ) -> ResearchDocument:
        attempts: list[ParserAttempt] = []
        recovery_candidates: list[RecoveryCandidateRecord] = []
        recovery_decisions: list[dict[str, Any]] = []
        original_snapshot: RecoveryEvidenceSnapshot | None = None
        expected_source_hash = sha256(source_bytes).hexdigest()
        recovery_started = time.monotonic()
        recovery_page_numbers: tuple[int, ...] | None = None
        stop_reason: str | None = None

        for index, (backend, parser) in enumerate(self._backends):
            normalized_backend = str(backend).strip().lower()
            if index > 0 and self._recovery_policy is not None:
                if recovery_page_numbers is None:
                    try:
                        # Every backend receives the full source bytes. Account
                        # for every physical page, never just the budget slice.
                        recovery_page_numbers = tuple(sorted(page_rects(source_bytes)))
                    except Exception:
                        # Unknown/encrypted/malformed source scope cannot admit
                        # another live parser invocation.
                        recovery_page_numbers = ()
                request = RecoveryRequest(
                    kind=RecoveryKind.PARSER,
                    backend=normalized_backend,
                    provider_id=self._backend_provider_ids.get(normalized_backend),
                    profile_id=self._backend_profile_ids.get(normalized_backend),
                    attempt_number=index + 1,
                    elapsed_seconds=time.monotonic() - recovery_started,
                    page_numbers=recovery_page_numbers,
                    source_ref=f"paper://{paper_id}/pdf",
                )
                decision = self._recovery_policy.authorize(request)
                recovery_decisions.append({
                    "request": {
                        "kind": request.kind.value,
                        "backend": request.backend,
                        "provider_id": request.provider_id,
                        "profile_id": request.profile_id,
                        "source_ref": request.source_ref,
                        "source_hash": expected_source_hash,
                        "page_numbers": list(request.page_numbers),
                    },
                    **decision.to_dict(),
                })
                if not decision.allowed:
                    stop_reason = decision.reason_code
                    break

            started = time.perf_counter()
            try:
                document = _parse_with_execution_identity(
                    parser,
                    paper_id,
                    source_bytes,
                    execution_identity=execution_identity,
                )
            except ExecutionEnvironmentError:
                # The Harness attempt/receipt chain is authoritative for
                # cancelled, timed-out, rejected, and indeterminate execution.
                # Never start another parser or local fallback after it fires.
                raise
            except Exception as exc:  # noqa: BLE001 - cascade records and falls through
                attempts.append(ParserAttempt(
                    backend=normalized_backend,
                    status="parse_error",
                    reason=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=_elapsed_ms(started),
                ))
                continue

            quality = self._probe.evaluate(document)
            snapshot = _document_recovery_snapshot(
                document,
                backend=normalized_backend,
                profile_id=self._backend_profile_ids.get(normalized_backend),
            )
            candidate_record: RecoveryCandidateRecord | None = None
            if original_snapshot is not None and self._recovery_policy is not None:
                candidate_record = evaluate_parser_candidate(
                    original=original_snapshot,
                    candidate=snapshot,
                    quality_passed=quality.passed,
                    expected_source_hash=expected_source_hash,
                )
                recovery_candidates.append(candidate_record)

            candidate_selected = (
                candidate_record is None
                or candidate_record.status is RecoveryCandidateStatus.SELECTED
            )
            if quality.passed and candidate_selected:
                attempts.append(ParserAttempt(
                    backend=normalized_backend,
                    status="success",
                    elapsed_ms=_elapsed_ms(started),
                    quality=quality.to_dict(),
                    audit=_document_execution_audit(document),
                ))
                attempts = _with_execution_attempt_dimensions(attempts)
                return _with_cascade_metadata(
                    document,
                    used_backend=normalized_backend,
                    degraded=False,
                    attempts=attempts,
                    recovery_policy=self._recovery_policy,
                    recovery_decisions=recovery_decisions,
                    recovery_candidates=recovery_candidates,
                    recovery_stop_reason=None,
                )

            reason = quality.reason
            if quality.passed and candidate_record is not None:
                reason = candidate_record.reason_code
            attempts.append(ParserAttempt(
                backend=normalized_backend,
                status="quality_rejected" if not quality.passed else "candidate_unverified",
                reason=reason,
                elapsed_ms=_elapsed_ms(started),
                quality=quality.to_dict(),
                audit=_document_execution_audit(document),
            ))
            if quality.passed and candidate_record is not None:
                stop_reason = candidate_record.reason_code
            if original_snapshot is None:
                original_snapshot = snapshot

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
            reason=stop_reason or "all_configured_backends_failed_or_rejected",
            elapsed_ms=_elapsed_ms(started),
            quality=quality.to_dict(),
            audit=_document_execution_audit(document),
        ))
        return _with_cascade_metadata(
            document,
            used_backend="pymupdf",
            degraded=True,
            attempts=attempts,
            recovery_policy=self._recovery_policy,
            recovery_decisions=recovery_decisions,
            recovery_candidates=recovery_candidates,
            recovery_stop_reason=stop_reason,
        )


class CascadeArxivDocumentParser:
    """Document parser that keeps LaTeX routing and cascades only PDF bytes."""

    def __init__(
        self,
        latex_parser: LatexSourceParser | None = None,
        pdf_parser: DocumentParserPort | None = None,
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
            return _parse_with_execution_identity(
                self._pdf,
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


def build_default_pdf_cascade_parser() -> DocumentParserPort:
    from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser

    factories: dict[str, Any] = {
        "marker": MarkerPdfDocumentParser,
    }
    backends: list[tuple[str, DocumentParserPort]] = []
    names = parser_cascade_backend_names()
    for name in names:
        if name == "pymupdf":
            continue
        factory = factories.get(name)
        if factory is None:
            raise ValueError(
                "NEWSROOM_PDF_PARSER_CASCADE must contain only: marker, pymupdf"
            )
        backends.append((name, factory()))
    if not backends:
        return PyMuPDFTextDocumentParser()
    return CascadeDocumentParser(backends=backends, probe=DocumentQualityProbe.from_env())


def parser_cascade_backend_names(value: str | None = None) -> tuple[str, ...]:
    raw = (value or os.environ.get("NEWSROOM_PDF_PARSER_CASCADE") or "marker").strip()
    names = tuple(
        part.strip().lower()
        for part in raw.split(",")
        if part.strip()
    )
    if not names:
        raise ValueError("NEWSROOM_PDF_PARSER_CASCADE must contain at least one backend")
    allowed = {"marker", "pymupdf"}
    unknown = sorted(set(names) - allowed)
    if unknown:
        raise ValueError(
            "NEWSROOM_PDF_PARSER_CASCADE must contain only: marker, pymupdf"
        )
    return names


def _with_cascade_metadata(
    document: ResearchDocument,
    *,
    used_backend: str,
    degraded: bool,
    attempts: list[ParserAttempt],
    recovery_policy: DocumentRecoveryPolicy | None = None,
    recovery_decisions: Sequence[Mapping[str, Any]] = (),
    recovery_candidates: Sequence[RecoveryCandidateRecord] = (),
    recovery_stop_reason: str | None = None,
) -> ResearchDocument:
    metadata = dict(document.metadata)
    metadata["parse_source"] = used_backend
    metadata["parser_backend"] = used_backend
    attempt_payloads: list[dict[str, Any]] = []
    recovered = any(attempt.status in {"parse_error", "quality_rejected"} for attempt in attempts)
    failed_count = sum(attempt.status in {"parse_error", "quality_rejected"} for attempt in attempts)
    for attempt in attempts:
        payload = attempt.to_dict()
        quality = payload.get("quality")
        if isinstance(quality, dict):
            metrics_payload = quality.get("metrics")
            metrics_payload = metrics_payload if isinstance(metrics_payload, dict) else {}
            dimensions = metrics_payload.get("dimensions")
            if isinstance(dimensions, dict):
                execution = dimensions.get("execution")
                if isinstance(execution, dict):
                    dimensions["execution"] = {
                        **execution,
                        "status": "recovered" if recovered and attempt.status == "success" else (
                            "degraded" if attempt.status in {"parse_error", "quality_rejected", "fallback"}
                            else "completed"
                        ),
                        "attempt_count": len(attempts),
                        "failed_attempt_count": failed_count,
                    }
                metrics_payload["dimensions"] = dimensions
                quality["metrics"] = metrics_payload
        attempt_payloads.append(payload)
    metadata["parser_cascade"] = {
        "used_backend": used_backend,
        "degraded": degraded,
        "attempts": attempt_payloads,
        "recovery": {
            "policy": recovery_policy.to_dict() if recovery_policy is not None else None,
            "status": (
                "indeterminate" if recovery_stop_reason == "execution_termination_indeterminate"
                else "blocked" if recovery_stop_reason
                else "degraded" if any(
                    item.status is not RecoveryCandidateStatus.SELECTED
                    for item in recovery_candidates
                )
                else "completed" if recovery_candidates
                else "not_requested"
            ),
            "stop_reason": recovery_stop_reason,
            "decisions": [dict(item) for item in recovery_decisions],
            "candidates": [item.to_dict() for item in recovery_candidates],
        },
    }
    if degraded:
        metadata["degraded"] = True
    return document.model_copy(
        update={
            "metadata": metadata,
            **({"status": "degraded"} if degraded else {}),
        }
    )



def _with_execution_attempt_dimensions(attempts: list[ParserAttempt]) -> list[ParserAttempt]:
    failed_count = sum(
        attempt.status in {"parse_error", "quality_rejected", "candidate_unverified"}
        for attempt in attempts
    )
    return [
        ParserAttempt(
            backend=item.backend,
            status=item.status,
            reason=item.reason,
            elapsed_ms=item.elapsed_ms,
            quality={
                **dict(item.quality or {}),
                "dimensions": {
                    **dict((item.quality or {}).get("dimensions", {})),
                    "execution": {
                        **dict((item.quality or {}).get("dimensions", {}).get("execution", {})),
                        "attempt_count": len(attempts),
                        "failed_attempt_count": failed_count,
                        "status": "recovered" if failed_count and item.status == "success" else "completed",
                    },
                },
            },
            audit=item.audit,
        )
        for item in attempts
    ]


def _document_recovery_snapshot(
    document: ResearchDocument,
    *,
    backend: str,
    profile_id: str | None,
) -> RecoveryEvidenceSnapshot:
    content = "\n".join(section.text or "" for section in document.sections)
    refs = tuple(
        dict.fromkeys(
            str(section.source_ref).strip()
            for section in document.sections
            if str(section.source_ref or "").strip()
        )
    )
    metadata = document.metadata
    return RecoveryEvidenceSnapshot.from_content(
        backend=backend,
        content=content,
        source_hash=document.source_hash,
        source_refs=refs[:256],
        artifact_refs=tuple(document.artifact_refs)[:256],
        content_ref=_optional_metadata_text(metadata, "parser_content_ref"),
        profile_id=profile_id,
        execution_receipt_ref=_optional_metadata_text(metadata, "execution_receipt_ref"),
        execution_receipt_checksum=_optional_metadata_text(
            metadata,
            "execution_receipt_checksum",
        ),
    )


def _document_execution_audit(document: ResearchDocument) -> dict[str, Any] | None:
    metadata = document.metadata
    receipt_ref = _optional_metadata_text(metadata, "execution_receipt_ref")
    receipt_checksum = _optional_metadata_text(metadata, "execution_receipt_checksum")
    if receipt_ref is None and receipt_checksum is None:
        return None
    return {
        "execution_receipt_ref": receipt_ref,
        "execution_receipt_checksum": receipt_checksum,
        "execution_provider": _optional_metadata_text(metadata, "execution_provider"),
        "execution_status": _optional_metadata_text(metadata, "execution_status"),
    }


def _optional_metadata_text(metadata: Mapping[str, Any], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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
