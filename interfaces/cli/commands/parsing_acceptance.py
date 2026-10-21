from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.research.application.parsing_evaluation_summary import ParsingEvaluationSummaryService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m interfaces.cli.commands.parsing_acceptance",
        description="Evaluate paired document-parsing evidence against an immutable acceptance profile.",
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--profile", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    service = ParsingEvaluationSummaryService()
    try:
        result = service.evaluate_acceptance_files(
            baseline_path=args.baseline,
            candidate_path=args.candidate,
            profile_path=args.profile,
        )
    except (OSError, ValueError):
        result: dict[str, Any] = {
            "schema_version": "research-document-parsing-acceptance-result/v1",
            "profile_id": None,
            "status": "blocked",
            "reasons": ["acceptance_evidence_or_profile_invalid"],
            "metrics": [],
            "quality_gate_decision": "not_evaluated",
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
