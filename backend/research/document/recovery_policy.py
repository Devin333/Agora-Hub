"""Deterministic, fail-closed policy for document parser recovery.

The policy owns admission and evidence classification only.  It never starts a
process, selects a provider dynamically, or persists a parallel workflow state.
Live work remains owned by the Harness execution adapter and its durable
attempt/receipt/event chain.
"""

from __future__ import annotations

import html
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit


RECOVERY_POLICY_VERSION = "research-document-recovery-policy/v1"


class RecoveryKind(StrEnum):
    PARSER = "parser"
    FORMULA_ROI = "formula_roi"
    TABLE_CELL_ROI = "table_cell_roi"


class RecoveryDecisionStatus(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


class RecoveryCandidateStatus(StrEnum):
    SELECTED = "selected"
    REJECTED = "rejected"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class RecoveryRequest:
    kind: RecoveryKind | str
    backend: str
    provider_id: str | None
    profile_id: str | None
    attempt_number: int
    turn_number: int = 1
    replan_number: int = 0
    elapsed_seconds: float = 0.0
    page_numbers: tuple[int, ...] = ()
    region_refs: tuple[str, ...] = ()
    geometry_reliable: bool = False
    source_ref: str | None = None
    crop_ref: str | None = None
    replay: bool = False
    cancelled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", RecoveryKind(self.kind))
        object.__setattr__(self, "backend", _identifier(self.backend, "backend"))
        object.__setattr__(
            self,
            "provider_id",
            _optional_identifier(self.provider_id, "provider_id"),
        )
        object.__setattr__(
            self,
            "profile_id",
            _optional_identifier(self.profile_id, "profile_id"),
        )
        for name in ("attempt_number", "turn_number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.replan_number, bool)
            or not isinstance(self.replan_number, int)
            or self.replan_number < 0
        ):
            raise ValueError("replan_number must be a non-negative integer")
        elapsed = self.elapsed_seconds
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            raise ValueError("elapsed_seconds must be a finite non-negative number")
        try:
            elapsed = float(elapsed)
        except OverflowError as exc:
            raise ValueError("elapsed_seconds must be a finite non-negative number") from exc
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("elapsed_seconds must be a finite non-negative number")
        object.__setattr__(self, "elapsed_seconds", elapsed)
        for name in ("replay", "cancelled", "geometry_reliable"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        pages = tuple(self.page_numbers)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in pages
        ) or len(pages) != len(set(pages)):
            raise ValueError("page_numbers must contain unique positive integers")
        regions = tuple(_bounded_text(value, "region_ref", 512) for value in self.region_refs)
        if len(regions) != len(set(regions)):
            raise ValueError("region_refs must be unique")
        object.__setattr__(self, "page_numbers", pages)
        object.__setattr__(self, "region_refs", regions)
        object.__setattr__(self, "source_ref", _optional_text(self.source_ref, 2048))
        object.__setattr__(self, "crop_ref", _optional_text(self.crop_ref, 2048))


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    status: RecoveryDecisionStatus
    reason_code: str
    budget: Mapping[str, Any]

    @property
    def allowed(self) -> bool:
        return self.status is RecoveryDecisionStatus.ALLOWED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "budget": dict(self.budget),
        }


@dataclass(frozen=True, slots=True)
class DocumentRecoveryPolicy:
    enabled: bool = False
    parser_recovery_enabled: bool = False
    formula_roi_enabled: bool = False
    table_cell_roi_enabled: bool = False
    allowed_parser_backends: tuple[str, ...] = ("marker",)
    authorized_provider_ids: tuple[str, ...] = ()
    max_attempts: int = 2
    max_pages: int = 3
    max_regions: int = 3
    max_seconds: float = 120.0
    max_memory_bytes: int = 17_179_869_184
    max_cpu_seconds: float = 120.0
    max_turns: int = 1
    max_replans: int = 0
    version: str = RECOVERY_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.version != RECOVERY_POLICY_VERSION:
            raise ValueError("unsupported recovery policy version")
        for name in (
            "enabled",
            "parser_recovery_enabled",
            "formula_roi_enabled",
            "table_cell_roi_enabled",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be boolean")
        backends = tuple(_identifier(value, "allowed_parser_backend") for value in self.allowed_parser_backends)
        providers = tuple(_identifier(value, "authorized_provider_id") for value in self.authorized_provider_ids)
        if len(backends) != len(set(backends)) or len(providers) != len(set(providers)):
            raise ValueError("recovery allowlists must not contain duplicates")
        object.__setattr__(self, "allowed_parser_backends", backends)
        object.__setattr__(self, "authorized_provider_ids", providers)
        _bounded_int(self.max_attempts, "max_attempts", 1, 8)
        _bounded_int(self.max_pages, "max_pages", 1, 16)
        _bounded_int(self.max_regions, "max_regions", 1, 32)
        _bounded_float(self.max_seconds, "max_seconds", 1.0, 1800.0)
        _bounded_int(
            self.max_memory_bytes,
            "max_memory_bytes",
            268_435_456,
            68_719_476_736,
        )
        _bounded_float(self.max_cpu_seconds, "max_cpu_seconds", 1.0, 1800.0)
        _bounded_int(self.max_turns, "max_turns", 1, 4)
        _bounded_int(self.max_replans, "max_replans", 0, 2)

    def authorize(self, request: RecoveryRequest) -> RecoveryDecision:
        budget = self.budget_projection(request)
        if request.replay:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "replay_live_execution_forbidden",
                budget,
            )
        if request.cancelled:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_cancelled",
                budget,
            )
        if not self.enabled:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_disabled",
                budget,
            )
        if request.attempt_number > self.max_attempts:
            return RecoveryDecision(
                RecoveryDecisionStatus.EXHAUSTED,
                "recovery_attempt_budget_exhausted",
                budget,
            )
        if request.turn_number > self.max_turns:
            return RecoveryDecision(
                RecoveryDecisionStatus.EXHAUSTED,
                "recovery_turn_budget_exhausted",
                budget,
            )
        if request.replan_number > self.max_replans:
            return RecoveryDecision(
                RecoveryDecisionStatus.EXHAUSTED,
                "recovery_replan_budget_exhausted",
                budget,
            )
        if request.elapsed_seconds >= self.max_seconds:
            return RecoveryDecision(
                RecoveryDecisionStatus.EXHAUSTED,
                "recovery_time_budget_exhausted",
                budget,
            )
        if len(request.page_numbers) > self.max_pages:
            return RecoveryDecision(
                RecoveryDecisionStatus.EXHAUSTED,
                "recovery_page_budget_exhausted",
                budget,
            )
        if len(request.region_refs) > self.max_regions:
            return RecoveryDecision(
                RecoveryDecisionStatus.EXHAUSTED,
                "recovery_region_budget_exhausted",
                budget,
            )
        if not request.page_numbers:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_page_scope_unknown",
                budget,
            )
        if not request.source_ref:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_source_missing",
                budget,
            )
        if (
            request.kind in {RecoveryKind.FORMULA_ROI, RecoveryKind.TABLE_CELL_ROI}
            and not request.region_refs
        ):
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_region_scope_unknown",
                budget,
            )
        if request.kind is RecoveryKind.PARSER:
            if tuple(sorted(request.page_numbers)) != tuple(
                range(1, len(request.page_numbers) + 1)
            ):
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "parser_whole_document_page_scope_incomplete",
                    budget,
                )
            if not self.parser_recovery_enabled:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "parser_recovery_disabled",
                    budget,
                )
            if request.backend not in self.allowed_parser_backends:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "parser_backend_not_allowed",
                    budget,
                )
        elif request.kind is RecoveryKind.FORMULA_ROI:
            if not self.formula_roi_enabled:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "formula_roi_recovery_disabled",
                    budget,
                )
            if not request.crop_ref:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "formula_roi_source_crop_missing",
                    budget,
                )
            if not request.geometry_reliable:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "formula_roi_geometry_unreliable",
                    budget,
                )
        elif request.kind is RecoveryKind.TABLE_CELL_ROI:
            if not self.table_cell_roi_enabled:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "table_cell_roi_recovery_disabled",
                    budget,
                )
            if not request.geometry_reliable or not request.crop_ref:
                return RecoveryDecision(
                    RecoveryDecisionStatus.BLOCKED,
                    "table_cell_geometry_unreliable",
                    budget,
                )
        if not request.profile_id:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_execution_profile_missing",
                budget,
            )
        if not request.provider_id or request.provider_id not in self.authorized_provider_ids:
            return RecoveryDecision(
                RecoveryDecisionStatus.BLOCKED,
                "recovery_provider_unauthorized",
                budget,
            )
        return RecoveryDecision(
            RecoveryDecisionStatus.ALLOWED,
            "recovery_authorized",
            budget,
        )

    def budget_projection(self, request: RecoveryRequest | None = None) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "max_attempts": self.max_attempts,
            "max_pages": self.max_pages,
            "max_regions": self.max_regions,
            "max_seconds": self.max_seconds,
            "max_memory_bytes": self.max_memory_bytes,
            "max_cpu_seconds": self.max_cpu_seconds,
            "max_processes": 1,
            "max_turns": self.max_turns,
            "max_replans": self.max_replans,
        }
        if request is not None:
            projection["used"] = {
                "attempt": request.attempt_number,
                "pages": len(request.page_numbers),
                "regions": len(request.region_refs),
                "elapsed_seconds": round(request.elapsed_seconds, 6),
                "turn": request.turn_number,
                "replan": request.replan_number,
            }
        return projection

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "enabled": self.enabled,
            "parser_recovery_enabled": self.parser_recovery_enabled,
            "formula_roi_enabled": self.formula_roi_enabled,
            "table_cell_roi_enabled": self.table_cell_roi_enabled,
            "allowed_parser_backends": list(self.allowed_parser_backends),
            "authorized_provider_ids": list(self.authorized_provider_ids),
            **self.budget_projection(),
        }


@dataclass(frozen=True, slots=True)
class RecoveryEvidenceSnapshot:
    backend: str
    source_hash: str | None
    content_checksum: str
    content_chars: int
    source_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    content_ref: str | None = None
    profile_id: str | None = None
    execution_receipt_ref: str | None = None
    execution_receipt_checksum: str | None = None

    @classmethod
    def from_content(
        cls,
        *,
        backend: str,
        content: str,
        source_hash: str | None,
        source_refs: Sequence[str] = (),
        artifact_refs: Sequence[str] = (),
        content_ref: str | None = None,
        profile_id: str | None = None,
        execution_receipt_ref: str | None = None,
        execution_receipt_checksum: str | None = None,
    ) -> "RecoveryEvidenceSnapshot":
        normalized = str(content)
        return cls(
            backend=_identifier(backend, "backend"),
            source_hash=_optional_text(source_hash, 256),
            content_checksum="sha256:" + sha256(normalized.encode("utf-8")).hexdigest(),
            content_chars=len(normalized),
            source_refs=tuple(_bounded_text(value, "source_ref", 2048) for value in source_refs),
            artifact_refs=tuple(_bounded_text(value, "artifact_ref", 2048) for value in artifact_refs),
            content_ref=_optional_text(content_ref, 2048),
            profile_id=_optional_identifier(profile_id, "profile_id"),
            execution_receipt_ref=_optional_text(execution_receipt_ref, 2048),
            execution_receipt_checksum=_optional_text(execution_receipt_checksum, 256),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "source_hash": self.source_hash,
            "content_checksum": self.content_checksum,
            "content_chars": self.content_chars,
            "source_refs": list(self.source_refs),
            "artifact_refs": list(self.artifact_refs),
            "content_ref": self.content_ref,
            "profile_id": self.profile_id,
            "execution_receipt_ref": self.execution_receipt_ref,
            "execution_receipt_checksum": self.execution_receipt_checksum,
        }


@dataclass(frozen=True, slots=True)
class RecoveryCandidateRecord:
    kind: RecoveryKind
    status: RecoveryCandidateStatus
    reason_code: str
    original: RecoveryEvidenceSnapshot
    candidate: RecoveryEvidenceSnapshot
    deterministic_checks: Mapping[str, bool] = field(default_factory=dict)
    source_crop_ref: str | None = None
    uncertainty: str = "content_semantics_not_verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "original": self.original.to_dict(),
            "candidate": self.candidate.to_dict(),
            "deterministic_checks": dict(self.deterministic_checks),
            "source_crop_ref": self.source_crop_ref,
            "uncertainty": self.uncertainty,
        }


def evaluate_parser_candidate(
    *,
    original: RecoveryEvidenceSnapshot,
    candidate: RecoveryEvidenceSnapshot,
    quality_passed: bool,
    expected_source_hash: str,
) -> RecoveryCandidateRecord:
    source_identity_verified = bool(
        candidate.source_hash and candidate.source_hash == expected_source_hash
    )
    checks = {
        "quality_gate_passed": bool(quality_passed),
        "source_identity_verified": source_identity_verified,
        "longer_than_original": candidate.content_chars > original.content_chars,
    }
    if not quality_passed:
        status = RecoveryCandidateStatus.REJECTED
        reason = "candidate_quality_gate_failed"
    elif not source_identity_verified:
        status = RecoveryCandidateStatus.UNVERIFIED
        reason = "candidate_source_identity_unverified"
    else:
        status = RecoveryCandidateStatus.SELECTED
        reason = "deterministic_quality_and_source_checks_passed"
    return RecoveryCandidateRecord(
        kind=RecoveryKind.PARSER,
        status=status,
        reason_code=reason,
        original=original,
        candidate=candidate,
        deterministic_checks=checks,
    )


def evaluate_roi_candidate(
    *,
    kind: RecoveryKind | str,
    original: RecoveryEvidenceSnapshot,
    candidate: RecoveryEvidenceSnapshot,
    source_crop_ref: str | None,
    geometry_reliable: bool,
    renderable: bool = False,
) -> RecoveryCandidateRecord:
    resolved_kind = RecoveryKind(kind)
    if resolved_kind not in {RecoveryKind.FORMULA_ROI, RecoveryKind.TABLE_CELL_ROI}:
        raise ValueError("ROI candidate kind must be formula_roi or table_cell_roi")
    crop_present = bool(source_crop_ref)
    checks = {
        "geometry_reliable": bool(geometry_reliable),
        "source_crop_present": crop_present,
        "renderable": bool(renderable),
        "longer_than_original": candidate.content_chars > original.content_chars,
    }
    reason = (
        "roi_geometry_or_crop_unreliable"
        if not geometry_reliable or not crop_present
        else "roi_content_semantics_unverified"
    )
    return RecoveryCandidateRecord(
        kind=resolved_kind,
        status=RecoveryCandidateStatus.UNVERIFIED,
        reason_code=reason,
        original=original,
        candidate=candidate,
        deterministic_checks=checks,
        source_crop_ref=source_crop_ref,
    )


@dataclass(frozen=True, slots=True)
class UntrustedInputDecision:
    allowed: bool
    reason_code: str


_UNSAFE_LATEX = re.compile(
    r"\\(?:write18|input|include|includeonly|openin|openout|read|usepackage|"
    r"immediate|special|catcode|newread|newwrite|loop)\b",
    re.IGNORECASE,
)
_UNSAFE_LATEX_URL = re.compile(r"(?:https?|ftp|file)://", re.IGNORECASE)


def inspect_untrusted_latex(value: str, *, max_chars: int = 20_000) -> UntrustedInputDecision:
    text = str(value)
    if len(text) > max_chars:
        return UntrustedInputDecision(False, "latex_input_too_large")
    if "\x00" in text or _UNSAFE_LATEX.search(text):
        return UntrustedInputDecision(False, "latex_unsafe_command")
    if _UNSAFE_LATEX_URL.search(text):
        return UntrustedInputDecision(False, "latex_external_reference_forbidden")
    return UntrustedInputDecision(True, "latex_data_only")


_ALLOWED_HTML_TAGS = frozenset(
    {
        "table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
        "colgroup", "col", "span", "br", "sub", "sup", "math", "mrow",
        "mi", "mn", "mo", "msub", "msup", "mfrac", "semantics", "annotation",
    }
)
_ALLOWED_HTML_ATTRS = {
    "th": frozenset({"rowspan", "colspan", "scope"}),
    "td": frozenset({"rowspan", "colspan"}),
    "col": frozenset({"span"}),
    "math": frozenset({"display"}),
    "annotation": frozenset({"encoding"}),
}
_DROP_CONTENT_TAGS = frozenset({"script", "style", "iframe", "object", "embed", "svg"})


class _SafeTableHTMLParser(HTMLParser):
    def __init__(self, *, max_output_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_output_chars = max_output_chars
        self.parts: list[str] = []
        self.output_chars = 0
        self.drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in _DROP_CONTENT_TAGS:
            self.drop_depth += 1
            return
        if self.drop_depth or tag not in _ALLOWED_HTML_TAGS:
            return
        allowed = _ALLOWED_HTML_ATTRS.get(tag, frozenset())
        rendered_attrs: list[str] = []
        for name, value in attrs:
            normalized_name = name.casefold()
            if normalized_name.startswith("on") or normalized_name not in allowed:
                continue
            normalized_value = "" if value is None else str(value).strip()
            if len(normalized_value) > 128 or not _safe_attribute_value(normalized_value):
                continue
            rendered_attrs.append(
                f' {normalized_name}="{html.escape(normalized_value, quote=True)}"'
            )
        self._append(f"<{tag}{''.join(rendered_attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() in _ALLOWED_HTML_TAGS and tag.casefold() != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _DROP_CONTENT_TAGS:
            if self.drop_depth:
                self.drop_depth -= 1
            return
        if not self.drop_depth and tag in _ALLOWED_HTML_TAGS and tag != "br":
            self._append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.drop_depth:
            self._append(html.escape(data, quote=False))

    def _append(self, value: str) -> None:
        if self.output_chars + len(value) > self.max_output_chars:
            raise ValueError("sanitized HTML exceeds output limit")
        self.parts.append(value)
        self.output_chars += len(value)


def sanitize_untrusted_table_html(value: str, *, max_chars: int = 200_000) -> str:
    text = str(value)
    if len(text) > max_chars:
        raise ValueError("table HTML exceeds input limit")
    parser = _SafeTableHTMLParser(max_output_chars=max_chars)
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def admit_crop_path(
    root: str | Path,
    candidate: str | Path,
    *,
    allowed_suffixes: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp"),
) -> Path:
    root_path = Path(root).resolve(strict=False)
    raw = str(candidate)
    if "\x00" in raw:
        raise ValueError("crop path must be a local path")
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc:
            raise ValueError("crop path must be a local path")
        candidate_path = root_path / candidate_path
    resolved = candidate_path.resolve(strict=False)
    try:
        resolved.relative_to(root_path)
    except ValueError as exc:
        raise ValueError("crop path escapes the authorized root") from exc
    if resolved.suffix.casefold() not in {suffix.casefold() for suffix in allowed_suffixes}:
        raise ValueError("crop path suffix is not allowed")
    return resolved


def _safe_attribute_value(value: str) -> bool:
    lowered = value.casefold()
    return not any(
        token in lowered
        for token in ("javascript:", "data:", "vbscript:", "file:", "http:", "https:")
    ) and all(char not in value for char in ("\x00", "\r", "\n"))


def _identifier(value: Any, field_name: str) -> str:
    text = str(value or "").strip().lower()
    if not text or len(text) > 128 or re.fullmatch(r"[a-z0-9][a-z0-9_.:/-]*", text) is None:
        raise ValueError(f"{field_name} is invalid")
    return text


def _optional_identifier(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, field_name)


def _bounded_text(value: Any, field_name: str, max_length: int) -> str:
    text = str(value).strip()
    if not text or len(text) > max_length or "\x00" in text:
        raise ValueError(f"{field_name} is invalid")
    return text


def _optional_text(value: Any, max_length: int) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, "value", max_length)


def _bounded_int(value: int, field_name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} is outside the allowed range")


def _bounded_float(value: float, field_name: str, minimum: float, maximum: float) -> None:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} is outside the allowed range") from exc
    if numeric != numeric or not minimum <= numeric <= maximum:
        raise ValueError(f"{field_name} is outside the allowed range")


__all__ = [
    "DocumentRecoveryPolicy",
    "RECOVERY_POLICY_VERSION",
    "RecoveryCandidateRecord",
    "RecoveryCandidateStatus",
    "RecoveryDecision",
    "RecoveryDecisionStatus",
    "RecoveryEvidenceSnapshot",
    "RecoveryKind",
    "RecoveryRequest",
    "UntrustedInputDecision",
    "admit_crop_path",
    "evaluate_parser_candidate",
    "evaluate_roi_candidate",
    "inspect_untrusted_latex",
    "sanitize_untrusted_table_html",
]
