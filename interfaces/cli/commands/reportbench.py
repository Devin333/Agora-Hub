"""Offline ReportBench adapter commands for cross-source research reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from backend.research.application.reportbench import ReportBenchEvaluationService


def register(subparsers: argparse._SubParsersAction) -> None:
    reportbench = subparsers.add_parser(
        "reportbench",
        help="Validate and export cross-source research report artifacts offline",
    )
    operations = reportbench.add_subparsers(dest="reportbench_command", required=True)

    validate = operations.add_parser("validate", help="Validate local tasks and results")
    _add_task_result_args(validate)
    validate.set_defaults(handler=validate_reportbench)

    export = operations.add_parser("export", help="Export validated results as JSONL")
    _add_task_result_args(export)
    export.add_argument("--output", required=True, help="Destination JSONL path")
    export.set_defaults(handler=export_reportbench)

    plan = operations.add_parser("run-plan", help="Create a bounded, approval-required run manifest")
    plan.add_argument("--tasks", required=True, help="Local task JSONL path")
    plan.add_argument("--output", required=True, help="Run plan JSON path")
    plan.add_argument("--task-id", action="append", default=[], help="Select a task ID; repeatable")
    plan.add_argument("--limit", type=int, default=None, help="Maximum selected cases")
    plan.add_argument("--budget-usd", type=float, default=0.0, help="Declared diagnostic budget")
    plan.set_defaults(handler=run_plan)


def _add_task_result_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tasks", required=True, help="Local task JSONL path")
    parser.add_argument("--results", required=True, help="Local result JSONL path")


def validate_reportbench(args: argparse.Namespace) -> int:
    service = ReportBenchEvaluationService()
    try:
        tasks = service.load_tasks(args.tasks)
        results = service.load_results(args.results)
        summary = service.validate(tasks, results)
        _print({"operation": "validate", "scope": "cross_source_reportbench_contract", **summary.to_dict()})
        return 0 if summary.valid else 1
    except (OSError, ValueError) as exc:
        _print(_failure("validate", str(exc)))
        return 2


def export_reportbench(args: argparse.Namespace) -> int:
    service = ReportBenchEvaluationService()
    try:
        tasks = service.load_tasks(args.tasks)
        results = service.load_results(args.results)
        summary = service.validate(tasks, results)
        if not summary.valid:
            _print({"operation": "export", "scope": "cross_source_reportbench_contract", **summary.to_dict(), "exported": False})
            return 1
        service.export(tasks, results, args.output)
        _print({"operation": "export", "scope": "cross_source_reportbench_contract", **summary.to_dict(), "exported": True, "output": str(Path(args.output))})
        return 0
    except FileExistsError as exc:
        _print(_failure("export", str(exc), output=str(Path(args.output))))
        return 2
    except (OSError, ValueError) as exc:
        _print(_failure("export", str(exc)))
        return 2


def run_plan(args: argparse.Namespace) -> int:
    service = ReportBenchEvaluationService()
    try:
        tasks = service.load_tasks(args.tasks)
        plan = service.build_run_plan(
            tasks,
            args.output,
            task_ids=args.task_id,
            limit=args.limit,
            budget_usd=args.budget_usd,
        )
        _print({"operation": "run-plan", "scope": "cross_source_reportbench_contract", **plan})
        return 0
    except FileExistsError as exc:
        _print(_failure("run-plan", str(exc), output=str(Path(args.output))))
        return 2
    except (OSError, ValueError) as exc:
        _print(_failure("run-plan", str(exc)))
        return 2


def _failure(operation: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "operation": operation,
        "scope": "cross_source_reportbench_contract",
        "valid": False,
        "issues": [{"code": "cli.failed", "location": operation, "reason": reason}],
        "network_calls": 0,
        "paid_calls": 0,
        **extra,
    }


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


__all__ = ["register", "validate_reportbench", "export_reportbench", "run_plan"]
