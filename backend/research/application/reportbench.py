from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

from backend.research.benchmark.reportbench import (
    SCHEMA_VERSION,
    CrossSourceResearchResult,
    ReportBenchTask,
    dump_results_jsonl,
    load_jsonl,
    result_from_record,
    validate_result,
    validate_tasks,
)


class CrossSourceResearchRunner(Protocol):
    def run(self, task: ReportBenchTask) -> CrossSourceResearchResult: ...


@dataclass(frozen=True)
class ValidationSummary:
    valid: bool
    task_count: int
    result_count: int
    issues: tuple[dict[str, str], ...]
    schema_version: str = SCHEMA_VERSION
    network_calls: int = 0
    paid_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "valid": self.valid,
            "task_count": self.task_count,
            "result_count": self.result_count,
            "case_count": self.result_count,
            "issues": list(self.issues),
            "network_calls": self.network_calls,
            "paid_calls": self.paid_calls,
            "quality_gate": {
                "evaluated": False,
                "passed": None,
                "reason": "offline contract validation does not make a research quality decision",
            },
        }


class ReportBenchEvaluationService:
    """Offline adapter between local research artifacts and ReportBench JSONL."""

    def load_tasks(self, path: str | Path) -> list[ReportBenchTask]:
        return load_jsonl(path, ReportBenchTask)

    def load_results(self, path: str | Path) -> list[CrossSourceResearchResult]:
        with Path(path).open("r", encoding="utf-8") as stream:
            results: list[CrossSourceResearchResult] = []
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    results.append(result_from_record(json.loads(line)))
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid result JSONL at line {line_number}") from exc
            return results

    def validate(
        self,
        tasks: Sequence[ReportBenchTask],
        results: Sequence[CrossSourceResearchResult],
    ) -> ValidationSummary:
        issues = list(validate_tasks(tasks))
        task_by_id = {task.task_id: task for task in tasks}
        seen_results: set[str] = set()
        for index, result in enumerate(results):
            if result.task_id in seen_results:
                issues.append({"code": "result.duplicate_task_id", "location": f"results[{index}].task_id", "reason": "duplicate result task_id"})
            seen_results.add(result.task_id)
            task = task_by_id.get(result.task_id)
            if task is None:
                issues.append({"code": "result.unknown_task", "location": f"results[{index}].task_id", "reason": "result has no matching task"})
            issues.extend(issue.to_dict() for issue in validate_result(result, task))
        missing = sorted(set(task_by_id) - seen_results)
        issues.extend({"code": "result.missing", "location": f"tasks[{task_by_id[task_id].task_id}]", "reason": "task has no result"} for task_id in missing)
        return ValidationSummary(
            valid=not issues,
            task_count=len(tasks),
            result_count=len(results),
            issues=tuple(issues),
        )

    def export(
        self,
        tasks: Sequence[ReportBenchTask],
        results: Sequence[CrossSourceResearchResult],
        destination: str | Path,
    ) -> ValidationSummary:
        summary = self.validate(tasks, results)
        if not summary.valid:
            return summary
        content = dump_results_jsonl(results)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            if existing != content:
                raise FileExistsError("refusing to overwrite a different ReportBench export")
        else:
            target.write_text(content, encoding="utf-8", newline="\n")
        return summary

    def build_run_plan(
        self,
        tasks: Sequence[ReportBenchTask],
        destination: str | Path,
        *,
        task_ids: Sequence[str] = (),
        limit: int | None = None,
        budget_usd: float = 0.0,
    ) -> dict[str, Any]:
        issues = validate_tasks(tasks)
        if issues:
            raise ValueError(json.dumps([issue.to_dict() for issue in issues], ensure_ascii=False))
        selected = [task for task in tasks if not task_ids or task.task_id in set(task_ids)]
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be positive")
            selected = selected[:limit]
        if task_ids:
            missing = sorted(set(task_ids) - {task.task_id for task in selected})
            if missing:
                raise ValueError(f"unknown task ids: {', '.join(missing)}")
        if budget_usd < 0:
            raise ValueError("budget_usd must be non-negative")
        plan = {
            "schema_version": SCHEMA_VERSION,
            "benchmark": "ReportBench",
            "selected_task_ids": [task.task_id for task in selected],
            "tasks": [{"task_id": task.task_id, "question": task.question} for task in selected],
            "output_dir": str(Path(destination)),
            "budget_usd": budget_usd,
            "execution_profile": "diagnostic",
            "qualification_status": "not_qualified",
            "requires_approval": True,
            "execution_started": False,
            "network_calls": 0,
            "paid_calls": 0,
            "plan_digest": hashlib.sha256(
                json.dumps([task.task_id for task in selected], separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if target.exists() and target.read_text(encoding="utf-8") != content:
            raise FileExistsError("refusing to overwrite a different ReportBench run plan")
        if not target.exists():
            target.write_text(content, encoding="utf-8", newline="\n")
        return plan

    def run_with_runner(
        self,
        tasks: Iterable[ReportBenchTask],
        runner: CrossSourceResearchRunner,
    ) -> list[CrossSourceResearchResult]:
        """Run an already-approved injected runner; this boundary does no routing or publishing."""
        results: list[CrossSourceResearchResult] = []
        for task in tasks:
            try:
                result = runner.run(task)
            except Exception as exc:  # sanitized failure boundary
                result = CrossSourceResearchResult.failed(task, type(exc).__name__.lower())
            results.append(result)
        return results


__all__ = ["CrossSourceResearchRunner", "ReportBenchEvaluationService", "ValidationSummary"]
