from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from enum import Enum
from typing import Any


class OutputSourceHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    COOLING_DOWN = "cooling_down"
    DISABLED = "disabled"


@dataclass(frozen=True)
class OutputReport:
    title: str
    sections: list[dict[str, Any]]
    source_urls: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "sections": [dict(section) for section in self.sections],
            "source_urls": list(self.source_urls),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class OutputReportRecord:
    report_id: str
    run_id: str
    status: str
    title: str | None = None
    report_json: dict[str, Any] | None = None
    report_markdown: str | None = None
    quality_score: float | None = None
    citation_coverage_score: float | None = None
    manifest_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "run_id": self.run_id,
            "status": self.status,
            "title": self.title,
            "report_json": dict(self.report_json) if isinstance(self.report_json, dict) else self.report_json,
            "report_markdown": self.report_markdown,
            "quality_score": self.quality_score,
            "citation_coverage_score": self.citation_coverage_score,
            "manifest_path": self.manifest_path,
        }


@dataclass(frozen=True)
class OutputSourceError:
    source_id: str
    error_type: str
    error_message: str
    source_name: str | None = None
    url: str | None = None
    retryable: bool | None = None
    request_ref: Any | None = None
    response_ref: Any | None = None
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "occurred_at", _parse_datetime(self.occurred_at))
        if self.retryable is None:
            object.__setattr__(self, "retryable", True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "url": self.url,
            "retryable": self.retryable,
            "request_ref": _object_ref(self.request_ref),
            "response_ref": _object_ref(self.response_ref),
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class OutputSourceHealth:
    source_id: str
    source_name: str | None = None
    url: str | None = None
    status: OutputSourceHealthStatus = OutputSourceHealthStatus.HEALTHY
    consecutive_failures: int = 0
    success_count_24h: int = 0
    failure_count_24h: int = 0
    avg_latency_ms_24h: float | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    cooldown_until: datetime | None = None
    last_error: OutputSourceError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        status = OutputSourceHealthStatus(self.status)
        if status == OutputSourceHealthStatus.COOLING_DOWN:
            status = OutputSourceHealthStatus.DOWN
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "last_success_at", _parse_datetime_optional(self.last_success_at))
        object.__setattr__(self, "last_failure_at", _parse_datetime_optional(self.last_failure_at))
        object.__setattr__(self, "cooldown_until", _parse_datetime_optional(self.cooldown_until))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "url": self.url,
            "status": self.status.value,
            "health_status": self.status.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_failure_count": self.consecutive_failures,
            "success_count_24h": self.success_count_24h,
            "failure_count_24h": self.failure_count_24h,
            "avg_latency_ms_24h": self.avg_latency_ms_24h,
            "last_success_at": _dt(self.last_success_at),
            "last_failure_at": _dt(self.last_failure_at),
            "cooldown_until": _dt(self.cooldown_until),
            "last_error_type": self.last_error.error_type if self.last_error else None,
            "last_error_message": self.last_error.error_message if self.last_error else None,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "metadata": dict(self.metadata),
        }


def render_output_report_markdown(report: OutputReport) -> str:
    lines = [f"# {report.title}", ""]
    for section in report.sections:
        lines.extend([f"## {section['title']}", str(section.get("content") or ""), ""])
    if report.source_urls:
        lines.extend(["## Sources", *[f"- {url}" for url in report.source_urls], ""])
    return "\n".join(lines)


def _object_ref(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _dt(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _parse_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_datetime_optional(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)
