from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from backend.research.application.parsing_evaluation_summary import (
    ParsingEvaluationSummaryService,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m interfaces.cli.commands.parsing_eval",
        description="Summarize a stored document-parsing evaluation manifest.",
    )
    parser.add_argument("manifest", type=Path, help="Path to a parsing evaluation manifest JSON file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = ParsingEvaluationSummaryService().summarize_file(args.manifest)
    except (OSError, ValueError):
        payload: dict[str, Any] = {
            "operation": "summarize-parsing",
            "valid": False,
            "issues": [
                {
                    "code": "parsing_manifest.invalid",
                    "reason": "manifest file is unavailable or invalid",
                }
            ],
            "quality_gate": {
                "evaluated": False,
                "passed": None,
                "reason": "manifest was not summarized; no quality or publication decision was evaluated",
            },
        }
        exit_code = 2
    else:
        exit_code = 0

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_parser", "main"]
