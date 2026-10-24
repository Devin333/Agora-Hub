"""QASPER evaluation commands nested under the existing ``rag-eval`` parser."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_POSITIVE_INTEGER = re.compile(r"[0-9]+\Z")


def register(operations: argparse._SubParsersAction) -> None:
    """Register QASPER mapping, experiment, scoring, and report operations."""
    qasper = operations.add_parser("qasper", help="Evaluate QASPER retrieval and answering")
    qasper.add_argument("--dataset-path", type=Path, help="Optional QASPER fixture/dataset path")
    qasper.add_argument("--cache-path", type=Path, help="Optional local cache path (offline by default)")
    qasper.set_defaults(handler=_dispatch)
    namespaces = qasper.add_subparsers(dest="qasper_namespace", required=True)

    mapping = namespaces.add_parser("mapping", help="Manage QASPER evidence mappings")
    mapping_commands = mapping.add_subparsers(dest="mapping_command", required=True)
    freeze = mapping_commands.add_parser("freeze", help="Freeze a mapping from the local fixture")
    freeze.add_argument("--output-dir", type=Path, required=True)
    freeze.set_defaults(_qasper_action="freeze_mapping")
    validate = mapping_commands.add_parser("validate", help="Validate a frozen mapping")
    validate.add_argument("--mapping-dir", type=Path, required=True)
    validate.set_defaults(_qasper_action="validate_mapping")

    ablation = namespaces.add_parser("ablation", help="Run a QASPER ablation")
    ablation_commands = ablation.add_subparsers(dest="ablation_command", required=True)
    run = ablation_commands.add_parser("run", help="Run one profile and parameter configuration")
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--mapping-dir", type=Path, required=True)
    run.add_argument("--profile", required=True)
    _add_parameters(run, required=True)
    run.set_defaults(_qasper_action="run")

    sweep = namespaces.add_parser("sweep", help="Run a parameter/profile sweep")
    sweep.add_argument("--output-dir", type=Path, required=True)
    sweep.add_argument("--mapping-dir", type=Path, required=True)
    sweep.add_argument("--profiles", nargs="+", required=True)
    _add_grid(sweep)
    sweep.set_defaults(_qasper_action="sweep")

    score = namespaces.add_parser("score", help="Score a completed QASPER run")
    score.add_argument("--run-dir", type=Path, required=True)
    score.set_defaults(_qasper_action="score")
    report = namespaces.add_parser("report", help="Create a report for a QASPER run")
    report.add_argument("--run-dir", type=Path, required=True)
    report.set_defaults(_qasper_action="report")

    answer = namespaces.add_parser("answer", help="End-to-end answer evaluation with live models")
    answer_commands = answer.add_subparsers(dest="answer_command", required=True)
    answer_run = answer_commands.add_parser(
        "run", help="Stratified QASPER dev run through the production gated answer path",
    )
    answer_run.add_argument("--output-dir", type=Path, required=True)
    answer_run.add_argument(
        "--query-vector-path", type=Path, required=True,
        help="Supplementary query-vector cache; created if absent and reused on reruns",
    )
    answer_run.add_argument("--seed", default="qasper-answer-v1")
    answer_run.add_argument("--per-type", type=int, default=75)
    answer_run.add_argument("--max-cases", type=int, default=None)
    answer_run.add_argument("--no-judge", action="store_true", help="Skip the LLM judge")
    answer_run.add_argument(
        "--rerun-failed-from", type=Path, default=None,
        help="Re-run only the generation system failures of this completed run, "
             "with its seed and stratum size",
    )
    answer_run.add_argument(
        "--retrieval-profile",
        choices=("chunk_only", "field_only", "field_lightweight"),
        default=None,
        help="Explicit offline retrieval profile; defaults to NEWS_QASPER_RETRIEVAL_PROFILE or chunk_only",
    )
    answer_run.add_argument(
        "--retrieval-overfetch",
        type=int,
        default=None,
        help="Optional positive integer retrieval overfetch multiplier",
    )
    answer_run.set_defaults(_qasper_action="answer_run")

    answer_replay = answer_commands.add_parser(
        "replay", help="Replay frozen answer cases through offline retrieval/context assembly only",
    )
    answer_replay.add_argument("--source-run-dir", type=Path, required=True)
    answer_replay.add_argument("--output-dir", type=Path, required=True)
    answer_replay.add_argument("--query-vector-path", type=Path, required=True)
    answer_replay.set_defaults(_qasper_action="answer_replay")

    answer_merge = answer_commands.add_parser(
        "merge", help="Replace a run's failed cases with their re-run records and re-summarize",
    )
    answer_merge.add_argument("--base-run-dir", type=Path, required=True)
    answer_merge.add_argument("--rerun-run-dir", type=Path, required=True)
    answer_merge.add_argument("--output-dir", type=Path, required=True)
    answer_merge.set_defaults(_qasper_action="answer_merge")

    answer_rejudge = answer_commands.add_parser(
        "rejudge", help="Re-run failed judges without regenerating answers or retrieval",
    )
    answer_rejudge.add_argument("--source-run-dir", type=Path, required=True)
    answer_rejudge.add_argument("--output-dir", type=Path, required=True)
    answer_rejudge.add_argument(
        "--include-missing-judge", action="store_true",
        help="Also judge answered legacy cases that have no verdict",
    )
    answer_rejudge.add_argument(
        "--legacy-judge-model", default=None, metavar="NAME",
        help="User-confirmed historical judge model for legacy runs lacking judge identity",
    )
    answer_rejudge.set_defaults(_qasper_action="answer_rejudge")

    calibrate = answer_commands.add_parser("calibrate", help="Human calibration of the answer judge")
    calibrate_commands = calibrate.add_subparsers(dest="calibrate_command", required=True)
    calibrate_export = calibrate_commands.add_parser(
        "export", help="Export a stratified calibration sheet from a completed answer run",
    )
    calibrate_export.add_argument("--run-dir", type=Path, required=True)
    calibrate_export.add_argument("--output-dir", type=Path, required=True)
    calibrate_export.add_argument("--size", type=int, default=50)
    calibrate_export.add_argument("--seed", default="qasper-judge-calibration-v1")
    calibrate_export.set_defaults(_qasper_action="calibrate_export")
    calibrate_score = calibrate_commands.add_parser(
        "score", help="Score filled labels.csv against the judge verdicts",
    )
    calibrate_score.add_argument("--calibration-dir", type=Path, required=True)
    calibrate_score.set_defaults(_qasper_action="calibrate_score")


def _add_parameters(parser: argparse.ArgumentParser, *, required: bool) -> None:
    # Values stay as text until _dispatch so every contract error is returned as
    # the command's structured JSON response, before service composition or I/O.
    parser.add_argument("--candidate-limit", required=required, help="Positive integer")
    parser.add_argument("--overfetch", required=required, help="Positive integer multiplier")
    parser.add_argument("--rrf-k", required=required, help="Positive integer reciprocal-rank fusion constant")
    parser.add_argument("--rerank", choices=("true", "false"), required=required)
    parser.add_argument("--threshold", required=required, help="Finite numeric retrieval threshold")
    parser.add_argument("--weights", required=required, help="JSON object of finite positive channel weights")


def _add_grid(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate-limit-grid", dest="candidate_limit", required=True, help="JSON array")
    parser.add_argument("--overfetch-grid", required=True, help="JSON array")
    parser.add_argument("--rrf-k-grid", dest="rrf_k", required=True, help="JSON array of positive integers")
    parser.add_argument("--rerank-grid", required=True, help="JSON array of booleans")
    parser.add_argument("--threshold-grid", required=True, help="JSON array of finite numbers")
    parser.add_argument("--weights-grid", required=True, help="JSON array of channel-weight objects")


def _build_service(dataset_path: Path | None, cache_path: Path | None) -> Any:
    # Keep composition optional for parser/help use and allow tests to inject a fake.
    from interfaces.composition.qasper_evaluation import build_qasper_evaluation_service

    return build_qasper_evaluation_service(dataset_path=dataset_path, cache_path=cache_path)


def _build_answer_service(args: argparse.Namespace) -> Any:
    from interfaces.composition.qasper_evaluation import build_qasper_answer_evaluation_service

    if args.dataset_path is None or args.cache_path is None:
        raise ValueError("answer run requires --dataset-path and --cache-path")
    rerun_failed_from = getattr(args, "rerun_failed_from", None)
    if rerun_failed_from is not None and getattr(args, "max_cases", None) is not None:
        raise ValueError("--rerun-failed-from cannot be combined with --max-cases")
    return build_qasper_answer_evaluation_service(
        progress=_print_progress,
        case_started=_print_case_started,
        rerun_failed_from=(rerun_failed_from.resolve() if rerun_failed_from is not None else None),
        dataset_path=args.dataset_path.resolve(),
        cache_path=args.cache_path.resolve(),
        query_vector_path=args.query_vector_path.resolve(),
        output_directory=args.output_dir.resolve(),
        seed=args.seed,
        per_type=args.per_type,
        max_cases=args.max_cases,
        judge=not args.no_judge,
        retrieval_profile=args.retrieval_profile,
        retrieval_overfetch=args.retrieval_overfetch,
    )


def _print_progress(done: int, total: int, case: Any) -> None:
    # Progress goes to stderr so stdout stays the single JSON result.
    detail = case.generation_error or (case.judge.error if case.judge is not None and case.judge.error else "")
    print(
        f"[{done}/{total}] END {case.case_id} {case.outcome.value} "
        f"{case.end_to_end_ms:.0f}ms {detail}".rstrip(),
        file=sys.stderr, flush=True,
    )


def _print_case_started(done: int, total: int, case_id: str) -> None:
    print(f"[{done}/{total}] START {case_id}", file=sys.stderr, flush=True)


def _dispatch_answer_merge(args: argparse.Namespace) -> int:
    from backend.research.application.qasper_answer_merge import QasperAnswerMergeError
    from interfaces.composition.qasper_evaluation import merge_qasper_answer_runs

    try:
        report = merge_qasper_answer_runs(
            base_directory=args.base_run_dir.resolve(),
            rerun_directory=args.rerun_run_dir.resolve(),
            output_directory=args.output_dir.resolve(),
        )
    except QasperAnswerMergeError as exc:
        # Merge diagnostics name identity fields only, so they are safe to show.
        _print_json({"operation": "answer_merge", "reason": str(exc), "valid": False})
        return 2
    except (ValueError, OSError, KeyError) as exc:
        _print_json({"operation": "answer_merge", "reason": _safe_reason(exc), "valid": False})
        return 2
    _print_json({"operation": "answer_merge", "valid": True, **report})
    return 0


def _build_answer_rejudge_service(args: argparse.Namespace) -> Any:
    from interfaces.composition.qasper_evaluation import build_qasper_answer_rejudge_service

    return build_qasper_answer_rejudge_service(
        source_directory=args.source_run_dir.resolve(),
        output_directory=args.output_dir.resolve(),
        dataset_path=args.dataset_path.resolve() if args.dataset_path is not None else None,
        cache_path=args.cache_path.resolve() if args.cache_path is not None else None,
        include_missing_judge=bool(getattr(args, "include_missing_judge", False)),
        legacy_judge_model=getattr(args, "legacy_judge_model", None),
        progress=_print_rejudge_progress,
    )


def _print_rejudge_progress(progress: Any) -> None:
    suffix = "" if progress.recovered is None else f" recovered={str(progress.recovered).lower()}"
    print(
        f"[{progress.index}/{progress.total}] JUDGE_{progress.phase.upper()} "
        f"{progress.case_id}{suffix}",
        file=sys.stderr,
        flush=True,
    )


def _dispatch_answer_rejudge(args: argparse.Namespace) -> int:
    from backend.research.application.qasper_answer_rejudge import QasperAnswerRejudgeError

    try:
        report = _build_answer_rejudge_service(args).run()
    except QasperAnswerRejudgeError as exc:
        _print_json({"operation": "answer_rejudge", "reason": str(exc), "valid": False})
        return 2
    except (ValueError, OSError, RuntimeError) as exc:
        _print_json({"operation": "answer_rejudge", "reason": _safe_reason(exc), "valid": False})
        return 2
    _print_json({"operation": "answer_rejudge", "valid": True, **report})
    return 0


def _dispatch_answer(args: argparse.Namespace) -> int:
    from framework.shared.errors import FrameworkError

    try:
        report = _build_answer_service(args).run()
    except (ValueError, OSError, RuntimeError, FrameworkError) as exc:
        _print_json({"operation": "answer_run", "reason": _safe_reason(exc), "valid": False})
        return 2
    _print_json({"operation": "answer_run", "valid": True, **report})
    return 0


def _build_replay_service(args: argparse.Namespace) -> Any:
    from interfaces.composition.qasper_retrieval_replay import build_qasper_retrieval_replay_service

    if args.dataset_path is None or args.cache_path is None:
        raise ValueError("answer replay requires --dataset-path and --cache-path")
    return build_qasper_retrieval_replay_service(
        dataset_path=args.dataset_path.resolve(),
        cache_path=args.cache_path.resolve(),
        query_vector_path=args.query_vector_path.resolve(),
        source_run_directory=args.source_run_dir.resolve(),
        output_directory=args.output_dir.resolve(),
    )


def _dispatch_replay(args: argparse.Namespace) -> int:
    try:
        report = _build_replay_service(args).run()
    except (ValueError, OSError, RuntimeError) as exc:
        _print_json({"operation": "answer_replay", "reason": _safe_reason(exc), "valid": False})
        return 2
    valid = report.get("status") == "completed"
    _print_json({"operation": "answer_replay", "valid": valid, **report})
    return 0 if valid else 1


def _build_calibration_service(args: argparse.Namespace) -> Any:
    from interfaces.composition.qasper_evaluation import build_qasper_answer_calibration_service

    if args.dataset_path is None or args.cache_path is None:
        raise ValueError("calibration requires --dataset-path and --cache-path")
    return build_qasper_answer_calibration_service(
        dataset_path=args.dataset_path.resolve(), cache_path=args.cache_path.resolve(),
    )


def _dispatch_calibration(args: argparse.Namespace) -> int:
    from backend.research.rag.evaluation.qasper_answer_calibration import CalibrationLabelError

    action = args._qasper_action
    try:
        service = _build_calibration_service(args)
        if action == "calibrate_export":
            result = service.export(
                run_directory=str(args.run_dir.resolve()), output_directory=str(args.output_dir.resolve()),
                size=args.size, seed=args.seed,
            )
        else:
            result = service.score(calibration_directory=str(args.calibration_dir.resolve()))
    except CalibrationLabelError as exc:
        # Label diagnostics name only case ids and columns, so the reviewer can fix the sheet.
        _print_json({"operation": action, "reason": str(exc), "valid": False})
        return 2
    except (ValueError, OSError, KeyError) as exc:
        _print_json({"operation": action, "reason": _safe_reason(exc), "valid": False})
        return 2
    _print_json({"operation": action, "valid": True, **result})
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    action = args._qasper_action
    if action == "answer_run":
        return _dispatch_answer(args)
    if action == "answer_merge":
        return _dispatch_answer_merge(args)
    if action == "answer_rejudge":
        return _dispatch_answer_rejudge(args)
    if action == "answer_replay":
        return _dispatch_replay(args)
    if action in {"calibrate_export", "calibrate_score"}:
        return _dispatch_calibration(args)
    parameters = None
    grid = None
    profiles: tuple[str, ...] = ()
    try:
        if action == "run":
            profiles = _validate_profiles((args.profile,))
            parameters = _parameters(args, profiles=profiles)
        elif action == "sweep":
            profiles = _validate_profiles(args.profiles)
            grid = _grid(args, profiles=profiles)
    except ValueError as exc:
        # These messages are fixed contract diagnostics and contain no paths,
        # source text, credentials, or application exception details.
        _print_json({"operation": action, "reason": str(exc), "valid": False})
        return 2

    try:
        # Parameter/profile validation intentionally precedes composition because
        # the real builder can inspect git state and later operations read large
        # dataset/cache files.
        service = _build_service(
            getattr(args, "dataset_path", None), getattr(args, "cache_path", None),
        )
        if action == "freeze_mapping":
            result = service.freeze_mapping(args.output_dir)
        elif action == "validate_mapping":
            result = service.validate_mapping(args.mapping_dir)
        elif action == "run":
            result = service.run(args.output_dir, args.mapping_dir, profiles[0], parameters)
        elif action == "sweep":
            result = service.sweep(args.output_dir, args.mapping_dir, grid, list(profiles))
        elif action == "score":
            result = service.score(args.run_dir)
        elif action == "report":
            result = service.report(args.run_dir)
        else:  # pragma: no cover - protected by parser registration
            raise ValueError("unsupported QASPER operation")
    except (ValueError, OSError, RuntimeError) as exc:
        _print_json({"operation": action, "reason": _safe_reason(exc), "valid": False})
        return 2

    if not isinstance(result, dict):
        model_dump = getattr(result, "model_dump", None)
        result = model_dump(mode="json") if callable(model_dump) else {"result": result}
    payload = {"operation": action, **result}
    _print_json(payload)
    if payload.get("valid") is False or payload.get("status") == "needs_review":
        return 1
    return 0


def _profile_contracts() -> Mapping[str, frozenset[str]]:
    from backend.research.rag.evaluation.qasper_experiments import PROFILES

    return {
        name: frozenset(
            channel
            for channel, enabled in (
                ("semantic", profile.semantic),
                ("bm25", profile.bm25),
                ("field", profile.field),
            )
            if enabled
        )
        for name, profile in PROFILES.items()
    }


def _validate_profiles(values: Sequence[str]) -> tuple[str, ...]:
    profiles = tuple(values)
    supported = _profile_contracts()
    if (
        not profiles
        or len(set(profiles)) != len(profiles)
        or any(type(profile) is not str or profile not in supported for profile in profiles)
    ):
        raise ValueError("profiles must be unique supported QASPER retrieval profiles")
    return profiles


def _parameters(args: argparse.Namespace, *, profiles: Sequence[str]) -> dict[str, Any]:
    return {
        "candidate_limit": _positive_int(args.candidate_limit, "candidate_limit"),
        "overfetch": _positive_int(args.overfetch, "overfetch"),
        "rrf_k": _positive_int(args.rrf_k, "rrf_k"),
        "rerank": args.rerank == "true",
        "threshold": _finite_number(args.threshold, "threshold"),
        "weights": _weights(_json_value(args.weights, "weights"), "weights", profiles=profiles),
    }


def _grid(args: argparse.Namespace, *, profiles: Sequence[str]) -> dict[str, list[Any]]:
    grid = {
        "candidate_limit": _json_value(args.candidate_limit, "candidate_limit grid"),
        "overfetch": _json_value(args.overfetch_grid, "overfetch grid"),
        "rrf_k": _json_value(args.rrf_k, "rrf_k grid"),
        "rerank": _json_value(args.rerank_grid, "rerank grid"),
        "threshold": _json_value(args.threshold_grid, "threshold grid"),
        "weights": _json_value(args.weights_grid, "weights grid"),
    }
    for key, values in grid.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"{key} grid must be a non-empty JSON array")
    for key in ("candidate_limit", "overfetch", "rrf_k"):
        grid[key] = [_positive_int(value, f"{key} grid value") for value in grid[key]]
    if any(type(value) is not bool for value in grid["rerank"]):
        raise ValueError("rerank grid values must be booleans")
    grid["threshold"] = [_finite_number(value, "threshold grid value") for value in grid["threshold"]]
    grid["weights"] = [
        _weights(value, "weights grid value", profiles=profiles) for value in grid["weights"]
    ]
    return grid


def _positive_int(value: Any, label: str) -> int:
    if type(value) is int:
        number = value
    elif type(value) is str and _POSITIVE_INTEGER.fullmatch(value):
        number = int(value)
    else:
        raise ValueError(f"{label} must be a positive integer")
    if number <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return number


def _weights(value: Any, label: str, *, profiles: Sequence[str]) -> dict[str, int | float]:
    contracts = _profile_contracts()
    enabled_for_every_profile = set.intersection(*(set(contracts[profile]) for profile in profiles))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object with finite positive channel weights")
    if any(
        type(key) is not str
        or key not in enabled_for_every_profile
        or not _is_number(weight)
        or weight <= 0
        for key, weight in value.items()
    ):
        raise ValueError(
            f"{label} must contain only finite positive weights for channels enabled by every selected profile"
        )
    return value


def _json_value(value: str, label: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _safe_reason(exc: Exception) -> str:
    # Exception text can contain paths, source text, or credentials; expose only the class.
    return f"{type(exc).__name__}: QASPER operation could not be completed"


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rag-eval qasper")
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return _dispatch(args)


__all__ = ["main", "register"]
