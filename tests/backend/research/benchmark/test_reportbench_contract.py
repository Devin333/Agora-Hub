from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.research.benchmark.reportbench import (
    CrossSourceResearchResult,
    ReportBenchSource,
    ReportBenchTask,
    validate_result,
)


def test_successful_result_requires_report_and_sources() -> None:
    task = ReportBenchTask(task_id="case-1", question="Q")
    result = CrossSourceResearchResult(task_id="case-1", question="Q", report=None, sources=[])
    issues = validate_result(result, task)
    assert {issue.code for issue in issues} == {"report.required", "sources.required"}


def test_duplicate_sources_are_rejected_deterministically() -> None:
    result = CrossSourceResearchResult(
        task_id="case-1",
        question="Q",
        report="R",
        sources=[
            ReportBenchSource(source_id="same", url="https://example.org/a", title="A", kind="article"),
            ReportBenchSource(source_id="same", url="https://example.org/a#fragment", title="A2", kind="article"),
        ],
    )
    codes = {issue.code for issue in validate_result(result)}
    assert codes == {"source.duplicate_id", "source.duplicate_url"}


def test_unsafe_source_url_is_not_accepted() -> None:
    with pytest.raises(ValidationError):
        ReportBenchSource(source_id="s", url="http://example.org/a", title="A", kind="article")
    with pytest.raises(ValidationError):
        ReportBenchSource(source_id="s", url="https://user:pass@example.org/a", title="A", kind="article")


def test_failed_result_has_no_fabricated_content() -> None:
    result = CrossSourceResearchResult.failed(ReportBenchTask(task_id="case-1", question="Q"), "ProviderTimeout!")
    assert result.status == "failed"
    assert result.report is None
    assert result.sources == []
    assert result.execution.error_code == "providertimeout_"
