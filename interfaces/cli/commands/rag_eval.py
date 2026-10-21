"""Unified Research RAG evaluation CLI operations implemented to date."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from typing import Any

from backend.research.application.evaluation import EvaluationDatasetValidationResult
from backend.research.ports.evaluation_dataset import DatasetRevision
from backend.research.rag.evaluation.protocol_dataset_io import (
    DatasetLoadResult,
    DatasetValidationIssue,
    DatasetValidationMode,
)
from interfaces.composition.evaluation import build_evaluation_application_service


_SCOPE = "dataset_contract"
_NO_QUALITY_DECISION = (
    "dataset validation does not run a RAG quality gate or make a publication decision"
)


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register only the rag-eval operations that are actually implemented."""

    rag_eval = subparsers.add_parser(
        "rag-eval",
        help="Validate Research RAG evaluation assets",
    )
    operations = rag_eval.add_subparsers(dest="rag_eval_command", required=True)
    from interfaces.cli.commands.answer_eval import register as register_answer
    from interfaces.cli.commands.qasper_eval import register as register_qasper

    register_qasper(operations)
    register_answer(operations)
    validate = operations.add_parser(
        "validate-dataset",
        help="Validate the read-only three-object dataset contract",
    )
    validate.add_argument("--dataset-root", required=True)
    validate.add_argument("--dataset-id", required=True)
    validate.add_argument("--dataset-version", required=True)
    validate.add_argument(
        "--corpus-root",
        help=(
            "Optional complete-corpus store: "
            "<root>/<corpus_snapshot_id>/manifest.json and materials/<sha256>"
        ),
    )
    validate.add_argument(
        "--mapping-root",
        help=(
            "Optional mapping store: "
            "<root>/<dataset_id>/<dataset_version>/mappings/<case_id>.json"
        ),
    )
    validate.add_argument(
        "--require-complete-corpus",
        action="store_true",
        help="Return nonzero unless every declared source material byte is verified",
    )
    validate.add_argument(
        "--require-frozen",
        action="store_true",
        help="Require lifecycle_state=frozen while validating the dataset contract",
    )
    validate.set_defaults(handler=validate_dataset)




def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.dev")
    subparsers = parser.add_subparsers(dest="command", required=True)
    register(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


def validate_dataset(args: argparse.Namespace) -> int:
    mode = (
        DatasetValidationMode.FROZEN
        if bool(args.require_frozen)
        else DatasetValidationMode.DRAFT
    )
    try:
        revision = DatasetRevision(
            dataset_id=args.dataset_id,
            dataset_version=args.dataset_version,
        )
    except (TypeError, ValueError) as exc:
        payload = _failure_payload(
            dataset_id="<invalid>",
            dataset_version="<invalid>",
            mode=mode,
            issue=DatasetValidationIssue(
                code="dataset.invalid_revision",
                location="revision",
                reason=_revision_error_reason(exc),
            ),
        )
        _print_json(payload)
        return 2

    external_requested = bool(
        args.corpus_root or args.mapping_root or args.require_complete_corpus
    )
    try:
        composition_options: dict[str, Any] = {"dataset_root": args.dataset_root}
        if args.corpus_root is not None:
            composition_options["corpus_root"] = args.corpus_root
        if args.mapping_root is not None:
            composition_options["mapping_root"] = args.mapping_root
        service = build_evaluation_application_service(**composition_options)
        if external_requested:
            result = service.validate_dataset_preflight(
                revision,
                mode=mode,
                validate_source_corpus=bool(args.corpus_root),
                require_complete_corpus=bool(args.require_complete_corpus),
                validate_mappings=bool(args.mapping_root),
            )
            diagnostics = result.diagnostics
        else:
            result = service.validate_dataset(revision, mode=mode)
            diagnostics = service.describe_dataset(result)
    except OSError as exc:
        payload = _failure_payload(
            dataset_id=revision.dataset_id,
            dataset_version=revision.dataset_version,
            mode=mode,
            issue=_read_issue(exc),
        )
        _print_json(payload)
        return 2

    payload = _result_payload(
        revision=revision,
        mode=mode,
        result=result,
        diagnostics=diagnostics,
    )
    _print_json(payload)
    if isinstance(result, EvaluationDatasetValidationResult):
        return 0 if result.report.valid and result.preflight_complete else 1
    return 0 if result.report.valid else 1


def _result_payload(
    *,
    revision: DatasetRevision,
    mode: DatasetValidationMode,
    result: DatasetLoadResult | EvaluationDatasetValidationResult,
    diagnostics: dict[str, Any],
) -> dict[str, Any]:
    report = result.report
    return {
        "operation": "validate-dataset",
        "scope": _SCOPE,
        "dataset": {
            "dataset_id": revision.dataset_id,
            "dataset_version": revision.dataset_version,
        },
        "validation_mode": mode.value,
        "valid": report.valid,
        "ready_for_execution_data": report.ready_for_execution_data,
        "formal_ready": report.formal_ready,
        "issues": [_issue_payload(issue) for issue in report.issues],
        "readiness_requirements": [
            _issue_payload(issue) for issue in report.readiness_requirements
        ],
        "diagnostics": diagnostics,
        "quality_gate": {
            "evaluated": False,
            "passed": None,
            "reason": _NO_QUALITY_DECISION,
        },
        "network_calls": 0,
        "paid_calls": 0,
        **(
            {
                "preflight_status": result.preflight_status,
                "preflight_complete": result.preflight_complete,
            }
            if isinstance(result, EvaluationDatasetValidationResult)
            else {}
        ),
    }


def _failure_payload(
    *,
    dataset_id: str,
    dataset_version: str,
    mode: DatasetValidationMode,
    issue: DatasetValidationIssue,
) -> dict[str, Any]:
    return {
        "operation": "validate-dataset",
        "scope": _SCOPE,
        "dataset": {
            "dataset_id": dataset_id,
            "dataset_version": dataset_version,
        },
        "validation_mode": mode.value,
        "valid": False,
        "ready_for_execution_data": False,
        "formal_ready": False,
        "issues": [_issue_payload(issue)],
        "readiness_requirements": [],
        "diagnostics": None,
        "quality_gate": {
            "evaluated": False,
            "passed": None,
            "reason": _NO_QUALITY_DECISION,
        },
        "network_calls": 0,
        "paid_calls": 0,
    }

def _read_issue(exc: OSError) -> DatasetValidationIssue:
    code = getattr(exc, "code", None)
    location = getattr(exc, "location", None)
    reason = getattr(exc, "reason", None)
    if not all(isinstance(value, str) and value for value in (code, location, reason)):
        return DatasetValidationIssue(
            code="dataset.read_failed",
            location="dataset",
            reason="dataset objects could not be read",
        )
    return DatasetValidationIssue(code=code, location=location, reason=reason)


def _issue_payload(issue: DatasetValidationIssue) -> dict[str, str]:
    return {
        "code": issue.code,
        "location": issue.location,
        "reason": issue.reason,
    }


def _revision_error_reason(exc: Exception) -> str:
    message = str(exc)
    allowed = (
        "dataset_id must be a strict string",
        "dataset_version must be a strict string",
        "dataset_id must be a portable identifier without path separators or traversal segments",
        "dataset_version must be a portable identifier without path separators or traversal segments",
        "dataset_version must identify an immutable revision, not a floating alias",
    )
    return message if message in allowed else "dataset revision is invalid"

def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main", "register", "validate_dataset"]
