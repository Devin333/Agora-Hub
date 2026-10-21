"""Automated answer fact evaluation commands under ``rag-eval answer``.

``qasper-run`` evaluates production answers against QASPER gold answers;
``reportbench-grounding`` checks ReportBench reports against their cited quotes;
``report`` re-reads and verifies a finished run. Model endpoints are JSON
objects naming the environment variable that holds each key; keys are never
passed on the command line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


def register(operations: argparse._SubParsersAction) -> None:
    answer = operations.add_parser("answer", help="Automated answer fact and grounding evaluation")
    commands = answer.add_subparsers(dest="answer_command", required=True)

    run = commands.add_parser("qasper-run", help="Evaluate generated answers against QASPER gold answers")
    run.add_argument("--dataset-path", type=Path, required=True)
    run.add_argument("--cache-path", type=Path, help="Offline embedding cache (dense/field profiles)")
    _add_panel(run)
    run.add_argument("--dataset-version", required=True, help="QASPER release, e.g. 0.3")
    run.add_argument("--license-reference-id", default="cc-by-4.0")
    run.add_argument("--questions-frozen-at", required=True, help="ISO-8601 time questions were frozen")
    run.add_argument("--answers-started-at", required=True, help="ISO-8601 time full-text answering began")
    run.add_argument("--retrieved-at", required=True, help="ISO-8601 time this copy was obtained")
    run.add_argument("--profile", default="full_hybrid")
    run.add_argument("--case-id", action="append", default=[])
    run.add_argument("--max-cases", type=int)
    run.add_argument("--answer-model", required=True, help="JSON model endpoint")
    run.add_argument("--min-calibration-variants", type=int, default=50)
    run.add_argument("--run-id")
    run.set_defaults(handler=_qasper_run)

    grounding = commands.add_parser(
        "reportbench-grounding", help="Check ReportBench reports against the quotes they cite",
    )
    grounding.add_argument("--results", type=Path, required=True, help="ReportBench result JSONL")
    grounding.add_argument("--dataset-version", required=True)
    grounding.add_argument(
        "--calibration-from", type=Path,
        help="QASPER answer run directory whose judge calibration backs this panel",
    )
    grounding.add_argument("--run-id", required=True)
    _add_panel(grounding)
    grounding.set_defaults(handler=_reportbench_grounding)

    report = commands.add_parser("report", help="Verify and print a finished answer evaluation run")
    report.add_argument("--run-dir", type=Path, required=True)
    report.set_defaults(handler=_report)


def _add_panel(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--replay-mode", choices=("record", "replay"), default="replay")
    parser.add_argument("--extractor-model", required=True, help="JSON model endpoint")
    parser.add_argument(
        "--judge-model", action="append", default=[], help="JSON model endpoint; repeat for each judge",
    )
    parser.add_argument("--max-concurrency", type=int, default=4)


def _panel(args: argparse.Namespace) -> Any:
    from interfaces.composition.answer_fact_evaluation import ModelPanelSettings

    return ModelPanelSettings(
        replay_directory=_absolute(args.replay_dir),
        replay_mode=args.replay_mode,
        extractor_model=_endpoint(args.extractor_model, "extractor-model"),
        judge_models=tuple(_endpoint(item, "judge-model") for item in args.judge_model),
        max_concurrency=args.max_concurrency,
    )


def _endpoint(raw: str, label: str) -> Any:
    from interfaces.composition.answer_fact_evaluation import ModelEndpoint

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    allowed = {"provider", "base_url", "model", "api_key_env", "model_revision", "timeout_seconds"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    if "api_key" in payload:
        raise ValueError(f"{label} must name api_key_env, never contain a key")
    return ModelEndpoint(**payload)


def _time(raw: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if value.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _absolute(path: Path | None) -> Path | None:
    return None if path is None else path.resolve()


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))


def _summary(report: Any) -> dict[str, Any]:
    return {
        "run_id": report.run_id,
        "cases": report.cases,
        "runtime_errors": report.runtime_errors,
        "judges_applied": report.judges_applied,
        "calibration_passed": None if report.calibration is None else report.calibration.passed,
        "calibration_failures": [] if report.calibration is None else list(report.calibration.failures),
        "metrics": {
            item.metric_id: {
                "value": item.value,
                "numerator": item.numerator,
                "denominator": item.denominator,
                "status_counts": item.status_counts,
            }
            for item in report.metrics
        },
        "digest": report.digest,
    }


def _qasper_run(args: argparse.Namespace) -> int:
    from backend.research.rag.evaluation.protocol_judge_calibration import JudgeCalibrationGate
    from backend.research.rag.evaluation.qasper_reference import QasperGoldIdentity
    from interfaces.composition.answer_fact_evaluation import (
        AnswerFactEvaluationSettings,
        compose_qasper_answer_evaluation,
    )

    try:
        identity = QasperGoldIdentity(
            dataset_version=args.dataset_version,
            license_reference_id=args.license_reference_id,
            questions_frozen_at=_time(args.questions_frozen_at, "questions-frozen-at"),
            answers_started_at=_time(args.answers_started_at, "answers-started-at"),
            retrieved_at=_time(args.retrieved_at, "retrieved-at"),
        )
        settings = AnswerFactEvaluationSettings(
            dataset=_absolute(args.dataset_path),
            embedding_cache=_absolute(args.cache_path),
            panel=_panel(args),
            answer_model=_endpoint(args.answer_model, "answer-model"),
            gold_identity=identity,
            profile=args.profile,
            case_ids=tuple(args.case_id),
            max_cases=args.max_cases,
            calibration_gate=JudgeCalibrationGate(min_variants=args.min_calibration_variants),
            run_id=args.run_id,
        )
        service = compose_qasper_answer_evaluation(settings)
    except ValueError as exc:
        _print({"operation": "qasper-run", "valid": False, "reason": str(exc)})
        return 2
    try:
        report = asyncio.run(service.run(str(_absolute(args.output_dir))))
    except (ValueError, OSError, RuntimeError) as exc:
        # Provider and replay errors can echo request text; report the class only.
        _print({"operation": "qasper-run", "valid": False, "reason": f"{type(exc).__name__}"})
        return 2
    _print({"operation": "qasper-run", "valid": True, **_summary(report)})
    return 0


def _reportbench_grounding(args: argparse.Namespace) -> int:
    from backend.research.application.reportbench import ReportBenchEvaluationService
    from backend.research.rag.evaluation.protocol_answer_fact_report import AnswerFactRunReport
    from interfaces.composition.answer_fact_evaluation import compose_reportbench_grounding, publish_run

    try:
        evaluator = compose_reportbench_grounding(_panel(args))
        results = ReportBenchEvaluationService().load_results(_absolute(args.results))
        calibration = None
        if args.calibration_from is not None:
            source = AnswerFactRunReport.model_validate_json(
                (_absolute(args.calibration_from) / "report.json").read_bytes()
            )
            source.verify_digest()
            calibration = source.calibration
    except (ValueError, OSError) as exc:
        _print({"operation": "reportbench-grounding", "valid": False, "reason": str(exc)})
        return 2
    try:
        cases, report = asyncio.run(evaluator.evaluate_run(
            results, run_id=args.run_id, dataset_id="reportbench",
            dataset_version=args.dataset_version, calibration=calibration,
        ))
        publish_run(_absolute(args.output_dir), {
            "cases.json": _json([item.model_dump(mode="json") for item in cases]),
            "report.json": _json(report.model_dump(mode="json")),
        })
    except (ValueError, OSError, RuntimeError) as exc:
        _print({"operation": "reportbench-grounding", "valid": False, "reason": f"{type(exc).__name__}"})
        return 2
    _print({"operation": "reportbench-grounding", "valid": True, **_summary(report)})
    return 0


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _report(args: argparse.Namespace) -> int:
    from backend.research.rag.evaluation.protocol_answer_fact_report import AnswerFactRunReport

    try:
        report = AnswerFactRunReport.model_validate_json((_absolute(args.run_dir) / "report.json").read_bytes())
        report.verify_digest()
    except (ValueError, OSError) as exc:
        _print({"operation": "report", "valid": False, "reason": f"{type(exc).__name__}"})
        return 2
    _print({"operation": "report", "valid": True, **_summary(report)})
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-eval")
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return args.handler(args)


__all__ = ["main", "register"]
