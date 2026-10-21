"""QASPER evaluation commands nested under the existing ``rag-eval`` parser."""

from __future__ import annotations

import argparse
import json
import math
import re
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


def _dispatch(args: argparse.Namespace) -> int:
    action = args._qasper_action
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



