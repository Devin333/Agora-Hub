from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from backend.research.rag.evaluation.paper_benchmark_suite import BenchmarkSuiteConfig, run_benchmark_suite


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_output()
    args = _build_parser().parse_args(argv)
    result = run_benchmark_suite(BenchmarkSuiteConfig(
        papers_dir=Path(args.papers_dir),
        output_dir=Path(args.output_dir),
        image_root=Path(args.image_root) if args.image_root else Path(args.papers_dir),
        retrieval_policy=args.retrieval_policy,
        max_pairs_per_type=args.max_pairs_per_type,
        min_papers=args.min_papers,
        target_min_per_type=args.target_min_per_type,
        split_seed=args.split_seed,
        include_negative=not args.no_negative,
        question_profile=args.question_profile,
        visual=not args.no_visual,
        page_visual=not args.no_page_visual,
        render_page_visual=not args.no_render_page_visual,
        lightweight_reranker=args.lightweight_reranker,
        gold_audit_sample_size=args.gold_audit_sample_size,
        gold_judge_mode=args.gold_judge,
        gold_judge_sample_size=args.gold_judge_sample_size,
        gold_judge_max_evidence_chars=args.gold_judge_max_evidence_chars,
        answer_eval_enabled=args.answer_eval,
        generation_track=args.generation_track,
        answer_eval_sample_size=args.answer_eval_sample_size,
        answer_max_context_chunks=args.answer_max_context_chunks,
        answer_max_chars_per_chunk=args.answer_max_chars_per_chunk,
        answer_judge_mode=args.answer_judge,
        answer_judge_sample_size=args.answer_judge_sample_size,
        spot_check_sample_size=args.spot_check_sample_size,
        spot_check_annotations_path=Path(args.spot_check_annotations) if args.spot_check_annotations else None,
        quality_thresholds=_parse_thresholds(args.quality_threshold),
        include_fixed_window_baseline=args.with_fixed_window_baseline,
        fixed_window_tokens=args.fixed_window_tokens,
        fixed_window_overlap_tokens=args.fixed_window_overlap_tokens,
    ))
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), end="\n")
    return 0


def _configure_utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.research.rag.cli.run_benchmark_suite",
        description="Run train/dev/test paper RAG benchmark with gold evidence audit.",
    )
    parser.add_argument("--papers-dir", required=True, help="Directory containing per-paper research_document.json files.")
    parser.add_argument("--output-dir", required=True, help="Directory for benchmark suite artifacts.")
    parser.add_argument("--image-root", help="Root used to resolve relative figure/table image refs.")
    parser.add_argument(
        "--retrieval-policy",
        default="paper_visual_rag_tuned",
        help="Retrieval policy, for example paper_visual_rag_tuned or paper_blind_semantic_rag_v1.",
    )
    parser.add_argument("--max-pairs-per-type", type=int, default=100)
    parser.add_argument("--min-papers", type=int, default=20)
    parser.add_argument("--target-min-per-type", type=int, default=50)
    parser.add_argument("--split-seed", default="paper-rag-benchmark-v1")
    parser.add_argument(
        "--question-profile",
        choices=("template", "blind_detemplated", "blind_semantic"),
        default="template",
        help="Question generation profile. Use blind_semantic for blind prompts with natural anchors.",
    )
    parser.add_argument("--gold-audit-sample-size", type=int, default=30)
    parser.add_argument(
        "--gold-judge",
        choices=("none", "llm"),
        default="none",
        help="Optional gold evidence audit mode. 'llm' uses OPENAI_*-compatible chat settings.",
    )
    parser.add_argument(
        "--gold-judge-sample-size",
        type=int,
        help="Maximum sampled audit items to send to --gold-judge.",
    )
    parser.add_argument("--gold-judge-max-evidence-chars", type=int, default=1600)
    parser.add_argument("--answer-eval", action="store_true", help="Generate answers for the test split and run answer-level evaluation.")
    parser.add_argument("--generation-track", choices=("end_to_end", "oracle_context"), default="end_to_end", help="Answer generation track; oracle_context may inject gold context for diagnostic comparison.")
    parser.add_argument("--answer-eval-sample-size", type=int, help="Maximum deterministic test samples for answer evaluation.")
    parser.add_argument("--answer-max-context-chunks", type=int, default=3)
    parser.add_argument("--answer-max-chars-per-chunk", type=int, default=1000)
    parser.add_argument(
        "--answer-judge",
        choices=("none", "llm"),
        default="none",
        help="Optional LLM faithfulness/relevancy/context-precision judge for generated answers.",
    )
    parser.add_argument("--answer-judge-sample-size", type=int, help="Maximum generated answers sent to --answer-judge.")
    parser.add_argument("--spot-check-sample-size", type=int, default=0, help="Export answer samples for manual spot checking.")
    parser.add_argument("--spot-check-annotations", help="Optional JSONL manual annotations to summarize in the benchmark report.")
    parser.add_argument(
        "--quality-threshold",
        action="append",
        default=[],
        metavar="METRIC=VALUE",
        help="Quality gate threshold, for example answer.success_rate=0.85 or generation.faithfulness=0.9.",
    )
    parser.add_argument("--fixed-window-tokens", type=int, default=220)
    parser.add_argument("--fixed-window-overlap-tokens", type=int)
    parser.add_argument(
        "--with-fixed-window-baseline",
        action="store_true",
        help="Also run the legacy fixed-window baseline for explicit A/B comparison.",
    )
    parser.add_argument("--no-negative", action="store_true")
    parser.add_argument("--no-visual", action="store_true")
    parser.add_argument("--no-page-visual", action="store_true")
    parser.add_argument("--no-render-page-visual", action="store_true")
    parser.add_argument(
        "--lightweight-reranker",
        action="store_true",
        help="Enable deterministic structured field reranking for the candidate retriever.",
    )
    return parser


def _parse_thresholds(values: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError(f"threshold must be METRIC=VALUE: {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"threshold metric is empty: {raw!r}")
        out[key] = float(value)
    return out


if __name__ == "__main__":
    raise SystemExit(main())
