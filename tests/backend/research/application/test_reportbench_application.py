from __future__ import annotations

import json

import pytest

from backend.research.application.reportbench import ReportBenchEvaluationService
from backend.research.benchmark.reportbench import (
    CrossSourceResearchResult,
    ReportBenchCitation,
    ReportBenchSource,
    ReportBenchTask,
)


def task(task_id: str = "case-1") -> ReportBenchTask:
    return ReportBenchTask(task_id=task_id, question="What does the evidence show?")


def result(task_id: str = "case-1") -> CrossSourceResearchResult:
    return CrossSourceResearchResult(
        task_id=task_id,
        question="What does the evidence show?",
        report="# Findings\n\nThe evidence is consistent.",
        sources=[
            ReportBenchSource(source_id="paper-1", url="https://example.com/paper", title="Paper", kind="paper"),
            ReportBenchSource(source_id="web-1", url="https://example.com/article", title="Article", kind="article"),
        ],
        citations=[ReportBenchCitation(source_id="paper-1", locator="p. 3")],
    )


def test_validate_requires_one_result_per_task() -> None:
    summary = ReportBenchEvaluationService().validate([task()], [])
    assert not summary.valid
    assert any(issue["code"] == "result.missing" for issue in summary.issues)
    assert summary.network_calls == 0
    assert summary.paid_calls == 0


def test_export_is_conflict_safe(tmp_path) -> None:
    service = ReportBenchEvaluationService()
    target = tmp_path / "results.jsonl"
    summary = service.export([task()], [result()], target)
    assert summary.valid
    first = target.read_text(encoding="utf-8")
    service.export([task()], [result()], target)
    assert target.read_text(encoding="utf-8") == first
    target.write_text("different\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        service.export([task()], [result()], target)


def test_run_plan_is_bounded_and_does_not_run_runner(tmp_path) -> None:
    plan_path = tmp_path / "plan.json"
    plan = ReportBenchEvaluationService().build_run_plan([task(), task("case-2")], plan_path, task_ids=["case-2"])
    assert plan["selected_task_ids"] == ["case-2"]
    assert plan["requires_approval"] is True
    assert plan["execution_started"] is False
    assert plan["qualification_status"] == "not_qualified"
    assert json.loads(plan_path.read_text(encoding="utf-8"))["network_calls"] == 0
