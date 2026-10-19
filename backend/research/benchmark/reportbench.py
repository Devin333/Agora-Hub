from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, field_validator, model_validator

from backend.foundation import PrimitiveModel


SCHEMA_VERSION = "reportbench-cross-source-v1"
ALLOWED_SOURCE_KINDS = frozenset({"paper", "article", "website", "dataset", "repository", "report", "other"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ReportBenchTask(PrimitiveModel):
    task_id: str
    question: str
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("task_id", "question")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value


class ReportBenchSource(PrimitiveModel):
    source_id: str
    url: str
    title: str
    kind: str
    locator: str | None = None

    @field_validator("source_id", "title", "kind")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @field_validator("kind")
    @classmethod
    def _allowed_kind(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ALLOWED_SOURCE_KINDS:
            raise ValueError(f"unsupported source kind: {value}")
        return value

    @field_validator("url")
    @classmethod
    def _safe_url(cls, value: str) -> str:
        value = value.strip()
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or parts.scheme.lower() != "https":
            raise ValueError("source URL must use HTTPS")
        if not parts.netloc or parts.username or parts.password or any(ord(c) < 32 for c in value):
            raise ValueError("source URL is unsafe")
        return urlunsplit(("https", parts.netloc.lower(), parts.path or "/", parts.query, parts.fragment))


class ReportBenchCitation(PrimitiveModel):
    source_id: str
    locator: str | None = None
    quote: str | None = None

    @field_validator("source_id")
    @classmethod
    def _required_source_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value


class ReportBenchExecution(PrimitiveModel):
    execution_profile: Literal["diagnostic", "qualified"] = "diagnostic"
    qualification_status: Literal["not_qualified", "qualified"] = "not_qualified"
    run_id: str | None = None
    code_revision: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    cost_usd: float | None = None
    network_calls: int = 0
    paid_calls: int = 0
    error_code: str | None = None

    @field_validator("network_calls", "paid_calls")
    @classmethod
    def _nonnegative_int(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("call counters must be non-negative")
        return value

    @field_validator("error_code")
    @classmethod
    def _sanitize_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        sanitized = re.sub(r"[^a-z0-9_.-]+", "_", value.strip().lower())[:80]
        return sanitized or "runner.failed"

    @field_validator("duration_ms")
    @classmethod
    def _nonnegative_duration(cls, value: int | None) -> int | None:
        if value is not None and (isinstance(value, bool) or value < 0):
            raise ValueError("duration_ms must be non-negative")
        return value

    @field_validator("cost_usd")
    @classmethod
    def _nonnegative_cost(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("cost_usd must be non-negative")
        return value


class CrossSourceResearchResult(PrimitiveModel):
    task_id: str
    question: str
    report: str | None = None
    sources: list[ReportBenchSource] = Field(default_factory=list)
    citations: list[ReportBenchCitation] = Field(default_factory=list)
    execution: ReportBenchExecution = Field(default_factory=ReportBenchExecution)
    status: Literal["succeeded", "failed"] = "succeeded"

    @field_validator("task_id", "question")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must be non-empty")
        return value

    @field_validator("report")
    @classmethod
    def _normalize_report(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _failed_result_has_no_content(self) -> "CrossSourceResearchResult":
        if self.status == "failed" and (self.report is not None or self.sources or self.citations):
            raise ValueError("failed result cannot contain report, sources, or citations")
        if self.status == "failed" and not self.execution.error_code:
            raise ValueError("failed result requires execution.error_code")
        return self

    @classmethod
    def failed(cls, task: ReportBenchTask, error_code: str) -> "CrossSourceResearchResult":
        safe_code = re.sub(r"[^a-z0-9_.-]+", "_", error_code.strip().lower())[:80] or "runner.failed"
        return cls(
            task_id=task.task_id,
            question=task.question,
            status="failed",
            execution=ReportBenchExecution(error_code=safe_code),
        )


@dataclass(frozen=True)
class ReportBenchIssue:
    code: str
    location: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "location": self.location, "reason": self.reason}


def normalized_source_url(url: str) -> str:
    parts = urlsplit(url.strip())
    # Fragment identifiers locate a passage within the same source and are not
    # a distinct source identity.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def validate_task(task: ReportBenchTask, *, index: int | None = None) -> list[ReportBenchIssue]:
    issues: list[ReportBenchIssue] = []
    if not _SAFE_ID.fullmatch(task.task_id):
        issues.append(ReportBenchIssue("task.invalid_id", _loc(index, "task_id"), "task_id is not portable"))
    return issues


def validate_tasks(tasks: Iterable[ReportBenchTask]) -> list[ReportBenchIssue]:
    issues: list[ReportBenchIssue] = []
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        issues.extend(validate_task(task, index=index))
        if task.task_id in seen:
            issues.append(ReportBenchIssue("task.duplicate_id", f"tasks[{index}].task_id", "duplicate task_id"))
        seen.add(task.task_id)
    return issues


def validate_result(result: CrossSourceResearchResult, task: ReportBenchTask | None = None) -> list[ReportBenchIssue]:
    issues: list[ReportBenchIssue] = []
    prefix = f"results[{result.task_id}]"
    if task is not None and result.question != task.question:
        issues.append(ReportBenchIssue("result.question_mismatch", f"{prefix}.question", "question does not match task"))
    if result.status == "failed":
        return issues
    if not result.report:
        issues.append(ReportBenchIssue("report.required", f"{prefix}.report", "successful result requires a report"))
    if not result.sources:
        issues.append(ReportBenchIssue("sources.required", f"{prefix}.sources", "successful result requires structured sources"))
    source_ids: set[str] = set()
    source_urls: set[str] = set()
    for index, source in enumerate(result.sources):
        if source.source_id in source_ids:
            issues.append(ReportBenchIssue("source.duplicate_id", f"{prefix}.sources[{index}].source_id", "duplicate source_id"))
        if normalized_source_url(source.url) in source_urls:
            issues.append(ReportBenchIssue("source.duplicate_url", f"{prefix}.sources[{index}].url", "duplicate normalized source URL"))
        source_ids.add(source.source_id)
        source_urls.add(normalized_source_url(source.url))
    for index, citation in enumerate(result.citations):
        if citation.source_id not in source_ids:
            issues.append(ReportBenchIssue("citation.unknown_source", f"{prefix}.citations[{index}].source_id", "citation references an unknown source"))
    return issues


def result_to_record(result: CrossSourceResearchResult) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": result.task_id,
        "question": result.question,
        "report": result.report,
        "sources": [source.to_dict() for source in result.sources],
        "citations": [citation.to_dict() for citation in result.citations],
        "execution": result.execution.to_dict(),
        "status": result.status,
    }


def result_from_record(record: dict[str, Any]) -> CrossSourceResearchResult:
    payload = dict(record)
    payload.pop("schema_version", None)
    return CrossSourceResearchResult.model_validate(payload)


def load_jsonl(path: str | Path, model: type[Any]) -> list[Any]:
    records: list[Any] = []
    with Path(path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                records.append(model.model_validate(json.loads(line)))
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid JSONL at line {line_number}") from exc
    return records


def dump_results_jsonl(results: Iterable[CrossSourceResearchResult]) -> str:
    return "".join(json.dumps(result_to_record(result), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for result in results)


def _loc(index: int | None, field: str) -> str:
    return f"tasks[{index}].{field}" if index is not None else f"task.{field}"


__all__ = [
    "ALLOWED_SOURCE_KINDS",
    "SCHEMA_VERSION",
    "CrossSourceResearchResult",
    "ReportBenchCitation",
    "ReportBenchExecution",
    "ReportBenchIssue",
    "ReportBenchSource",
    "ReportBenchTask",
    "dump_results_jsonl",
    "load_jsonl",
    "normalized_source_url",
    "result_from_record",
    "result_to_record",
    "validate_result",
    "validate_task",
    "validate_tasks",
]
