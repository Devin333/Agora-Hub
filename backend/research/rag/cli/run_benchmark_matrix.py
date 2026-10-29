from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from backend.research.rag.evaluation.paper_benchmark_matrix import (
    BenchmarkMatrixConfig,
    BenchmarkMatrixDataset,
    load_benchmark_matrix_datasets,
    run_benchmark_matrix,
)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_utf8_output()
    args = _build_parser().parse_args(argv)
    datasets = _datasets_from_args(args)
    result = run_benchmark_matrix(BenchmarkMatrixConfig(
        datasets=datasets,
        output_dir=Path(args.output_dir),
        retrieval_policy=args.retrieval_policy,
        question_profile=args.question_profile,
        max_pairs_per_type=args.max_pairs_per_type,
        min_papers=args.min_papers,
        target_min_per_type=args.target_min_per_type,
        split_seed=args.split_seed,
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
        answer_judge_mode=args.answer_judge,
        answer_judge_sample_size=args.answer_judge_sample_size,
        spot_check_sample_size=args.spot_check_sample_size,
        spot_check_annotations_path=Path(args.spot_check_annotations) if args.spot_check_annotations else None,
        quality_thresholds=_parse_thresholds(args.quality_threshold),
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
        prog="python -m backend.research.rag.cli.run_benchmark_matrix",
        description="Run the Paper RAG benchmark suite across multiple held-out paper datasets.",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="Dataset name and papers directory. Repeat for historical_38, new50_20260629, etc.",
    )
    parser.add_argument(
        "--dataset-manifest",
        help="JSON manifest with a datasets list. Entries use name, papers_dir, and optional image_root.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--retrieval-policy", default="paper_blind_semantic_rag_v1")
    parser.add_argument(
        "--question-profile",
        choices=("template", "blind_detemplated", "blind_semantic"),
        default="blind_semantic",
    )
    parser.add_argument("--max-pairs-per-type", type=int, default=100)
    parser.add_argument("--min-papers", type=int, default=20)
    parser.add_argument("--target-min-per-type", type=int, default=50)
    parser.add_argument("--split-seed", default="paper-rag-benchmark-v1")
    parser.add_argument("--gold-audit-sample-size", type=int, default=30)
    parser.add_argument(
        "--gold-judge",
        choices=("none", "llm"),
        default="none",
        help="Optional gold evidence audit mode for each matrix dataset.",
    )
    parser.add_argument("--gold-judge-sample-size", type=int)
    parser.add_argument("--gold-judge-max-evidence-chars", type=int, default=1600)
    parser.add_argument("--answer-eval", action="store_true")
    parser.add_argument("--generation-track", choices=("end_to_end", "oracle_context"), default="end_to_end")
    parser.add_argument("--answer-eval-sample-size", type=int)
    parser.add_argument(
        "--answer-judge",
        choices=("none", "llm"),
        default="none",
        help="Optional answer generation judge for each matrix dataset.",
    )
    parser.add_argument("--answer-judge-sample-size", type=int)
    parser.add_argument("--spot-check-sample-size", type=int, default=0)
    parser.add_argument(
        "--spot-check-annotations",
        help="Optional JSONL file or directory. Directories resolve to <dataset>.jsonl per dataset.",
    )
    parser.add_argument("--quality-threshold", action="append", default=[], metavar="METRIC=VALUE")
    parser.add_argument("--no-visual", action="store_true")
    parser.add_argument("--no-page-visual", action="store_true")
    parser.add_argument("--no-render-page-visual", action="store_true")
    parser.add_argument("--lightweight-reranker", action="store_true")
    return parser


def _datasets_from_args(args: argparse.Namespace) -> tuple[BenchmarkMatrixDataset, ...]:
    datasets: list[BenchmarkMatrixDataset] = []
    if args.dataset_manifest:
        datasets.extend(load_benchmark_matrix_datasets(Path(args.dataset_manifest)))
    datasets.extend(_parse_dataset(value) for value in args.dataset)
    if not datasets:
        raise ValueError("provide at least one --dataset or --dataset-manifest")
    return tuple(datasets)


def _parse_dataset(value: str) -> BenchmarkMatrixDataset:
    if "=" not in value:
        raise ValueError(f"dataset must use NAME=PATH form: {value!r}")
    name, raw_path = value.split("=", 1)
    name = name.strip()
    raw_path = raw_path.strip()
    if not name or not raw_path:
        raise ValueError(f"dataset must include both name and path: {value!r}")
    return BenchmarkMatrixDataset(name=name, papers_dir=Path(raw_path))


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
