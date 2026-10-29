from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backend.research.rag.evaluation.business_answer_eval import run_business_answer_eval
from backend.research.rag.retrieval.policies import PAPER_BLIND_SEMANTIC_RAG_V1_POLICY

DEFAULT_QUESTION_BANK = Path("data/eval/business_question_bank_v1_draft.jsonl")
DEFAULT_CORPUS_SNAPSHOT = Path("data/eval/golden_corpus_snapshot.json")
DEFAULT_GOLDEN_SET = Path("data/eval/golden_set.json")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from backend.research.application.llm_client import build_unity_paper_answer_model

    output_dir = Path(args.output_dir) if args.output_dir else Path(
        ".newsroom/eval"
    ) / f"business_question_bank_v1_live_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    run = run_business_answer_eval(
        question_bank_path=Path(args.question_bank),
        corpus_snapshot_path=Path(args.corpus_snapshot),
        golden_set_path=Path(args.golden_set),
        output_dir=output_dir,
        answer_model=build_unity_paper_answer_model(max_tokens=args.max_output_tokens),
        retrieval_policy=args.retrieval_policy,
        context_limit=args.context_limit,
    )
    print(json.dumps(run.summary(), ensure_ascii=False, indent=2))
    # A measurement run: system failures are reported as data, not as a process failure.
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.research.rag.cli.run_business_answer_eval",
        description="Run the business question bank through the gated Paper RAG answer path with the configured model.",
    )
    parser.add_argument("--question-bank", default=str(DEFAULT_QUESTION_BANK))
    parser.add_argument("--corpus-snapshot", default=str(DEFAULT_CORPUS_SNAPSHOT))
    parser.add_argument("--golden-set", default=str(DEFAULT_GOLDEN_SET))
    parser.add_argument("--output-dir", help="Defaults to a new timestamped directory under .newsroom/eval.")
    parser.add_argument("--retrieval-policy", default=PAPER_BLIND_SEMANTIC_RAG_V1_POLICY)
    parser.add_argument("--context-limit", type=int, default=8)
    parser.add_argument("--max-output-tokens", type=int, default=600)
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
