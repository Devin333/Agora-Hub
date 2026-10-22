from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any, Sequence

from backend.research.rag.evaluation.evidence_eval_runner import (
    EvidenceEvalOptions,
    build_live_answer_samples,
    _matches_filters,
    run_evidence_eval_core,
)
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair


def main(
    argv: Sequence[str] | None = None,
    *,
    live_answer_ask: Callable[[EvidenceQAPair], dict[str, Any]] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    answer_model = None
    if args.live_answer_eval and live_answer_ask is None:
        from backend.research.application.llm_client import build_unity_paper_answer_model
        answer_model = build_unity_paper_answer_model(max_tokens=600)
    return run_evidence_eval_core(
        evidence_eval_options_from_args(args),
        live_answer_ask=live_answer_ask,
        answer_model=answer_model,
    )


def evidence_eval_options_from_args(args: argparse.Namespace) -> EvidenceEvalOptions:
    return EvidenceEvalOptions(
        golden_set=args.golden_set,
        output_dir=args.output_dir,
        papers_dir=args.papers_dir,
        build_golden_set=args.build_golden_set,
        max_pairs_per_type=args.max_pairs_per_type,
        no_negative=args.no_negative,
        domain=args.domain,
        live_retrieval=args.live_retrieval,
        visual=args.visual,
        page_visual=args.page_visual,
        no_render_page_visual=args.no_render_page_visual,
        vision_descriptions=args.vision_descriptions,
        image_root=args.image_root,
        retrieval_policy=args.retrieval_policy,
        lightweight_reranker=args.lightweight_reranker,
        thresholds=_parse_thresholds(args.threshold),
        deterministic_answer_eval=args.deterministic_answer_eval,
        live_answer_eval=args.live_answer_eval,
        answer_eval_limit=args.answer_eval_limit,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.research.rag.cli.run_evidence_eval",
        description="Write a paper RAG evidence benchmark summary/report from a golden set.",
    )
    parser.add_argument("--golden-set", help="Path to EvidenceQAPair JSON golden set.")
    parser.add_argument(
        "--output-dir",
        default=".newsroom/eval/evidence",
        help="Directory for evidence_regression_report.{json,md}.",
    )
    parser.add_argument(
        "--papers-dir",
        help="Directory containing per-paper research_document.json artifacts.",
    )
    parser.add_argument(
        "--build-golden-set",
        action="store_true",
        help="Build deterministic evidence QA pairs from --papers-dir before evaluating.",
    )
    parser.add_argument(
        "--max-pairs-per-type",
        type=int,
        default=20,
        help="Maximum deterministic QA pairs per type when --build-golden-set is used.",
    )
    parser.add_argument(
        "--no-negative",
        action="store_true",
        help="Skip negative QA pairs when --build-golden-set is used.",
    )
    parser.add_argument(
        "--domain",
        default="",
        help="Domain label written into generated QA pairs.",
    )
    parser.add_argument(
        "--live-retrieval",
        action="store_true",
        help="Index parsed paper chunks in memory and run EvidenceRetrievalEvaluator.",
    )
    parser.add_argument(
        "--visual",
        action="store_true",
        help="Enable in-memory visual indexing for figure chunks during --live-retrieval.",
    )
    parser.add_argument(
        "--page-visual",
        action="store_true",
        help="Add PDF page-level visual chunks to live retrieval/evaluation.",
    )
    parser.add_argument(
        "--no-render-page-visual",
        action="store_true",
        help="Do not render missing PDF page images when --page-visual is used.",
    )
    parser.add_argument(
        "--vision-descriptions",
        action="store_true",
        help="Use OPENAI_*-configured multimodal model to describe figure/table images before indexing.",
    )
    parser.add_argument(
        "--image-root",
        help="Root used to resolve relative figure image refs for --visual.",
    )
    parser.add_argument(
        "--retrieval-policy",
        default="",
        help=(
            "Named retrieval policy for --live-retrieval, for example "
            "paper_visual_rag_tuned, paper_blind_semantic_rag_v1, "
            "or paper_hybrid_rrf_rag_v1."
        ),
    )
    parser.add_argument(
        "--lightweight-reranker",
        action="store_true",
        help="Enable deterministic structured field reranking for --live-retrieval.",
    )
    parser.add_argument(
        "--threshold",
        action="append",
        default=[],
        metavar="METRIC=VALUE",
        help="Optional threshold, for example retrieval.evidence_coverage=0.8.",
    )
    parser.add_argument(
        "--deterministic-answer-eval",
        action="store_true",
        help=(
            "Add deterministic answer/abstention samples from the golden set so "
            "answer thresholds can run without an LLM or external services."
        ),
    )
    parser.add_argument(
        "--live-answer-eval",
        action="store_true",
        help=(
            "Evaluate answer metrics by running the gated Harness answer path. "
            "This may call the configured LLM and is intended for nightly/non-PR runs."
        ),
    )
    parser.add_argument(
        "--answer-eval-limit",
        type=int,
        default=8,
        help="Context passage limit passed to gated answer evaluation.",
    )
    return parser


def _parse_thresholds(values: list[str]) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"threshold must use METRIC=VALUE form: {value!r}")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("threshold metric is required")
        thresholds[key] = float(raw)
    return thresholds


if __name__ == "__main__":
    raise SystemExit(main())
