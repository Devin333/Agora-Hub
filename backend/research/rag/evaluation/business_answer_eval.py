"""Live answer evaluation over the business question bank.

Runs every scorable case through the production gated answer path
(``PaperRAGSession`` + ``PaperAnswerWorker`` + ``AnswerGenerator``) against the frozen
golden corpus snapshot, then records per-case outcomes, latency and token usage.

Retrieval here uses the in-memory fixture index, so the report is a diagnostic of the
answer path, not a production retrieval-quality qualification.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.research.ports.paper_answer_model import PaperAnswerModelPort
from backend.research.rag.evaluation.business_question_bank import load_business_question_bank
from backend.research.rag.evaluation.evidence_eval_runner import (
    build_in_memory_live_answer_ask,
    live_answer_sample,
)
from backend.research.rag.evaluation.golden_corpus_snapshot import load_golden_corpus_snapshot
from backend.research.rag.evaluation.paper_answer_eval import (
    EvidenceAnswerEvalResult,
    EvidenceAnswerEvaluator,
    EvidenceAnswerSample,
)
from backend.research.rag.evaluation.paper_evaluation_report import EvidenceRegressionReport

PROTOCOL = "business_question_bank_live_answer_v1"


@dataclass(frozen=True)
class BusinessAnswerEvalRun:
    output_dir: Path
    answer: EvidenceAnswerEvalResult
    efficiency: dict[str, Any]
    coverage: dict[str, Any]
    models: tuple[str, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "coverage": self.coverage,
            "models": list(self.models),
            "outcome_counts": self.answer.outcome_counts(),
            "system_failure_rate": self.answer.system_failure_rate(),
            "over_abstention_rate": self.answer.over_abstention_rate(),
            "abstention_accuracy": self.answer.abstention_accuracy(),
            "answer_success_rate": self.answer.success_rate(),
            "citation_gold_coverage": self.answer.citation_gold_coverage_score(),
            "retrieval_context_coverage": self.answer.retrieval_context_coverage_score(),
            "efficiency": self.efficiency,
        }


def run_business_answer_eval(
    *,
    question_bank_path: Path,
    corpus_snapshot_path: Path,
    golden_set_path: Path,
    output_dir: Path,
    answer_model: PaperAnswerModelPort,
    retrieval_policy: str,
    context_limit: int = 8,
    clock: Callable[[], float] = time.perf_counter,
) -> BusinessAnswerEvalRun:
    bank = load_business_question_bank(question_bank_path)
    snapshot = load_golden_corpus_snapshot(corpus_snapshot_path, golden_set_path=golden_set_path)
    chunks = list(snapshot.chunks)
    missing = sorted(bank.paper_ids() - {chunk.paper_id for chunk in chunks})
    if missing:
        # Retrieval would silently return nothing for these papers.
        raise ValueError(f"corpus snapshot lacks papers referenced by the question bank: {missing}")

    ask = build_in_memory_live_answer_ask(
        chunks,
        retrieval_policy=retrieval_policy,
        visual_enabled=False,
        image_root=None,
        lightweight_reranker=False,
        limit=context_limit,
        answer_model=answer_model,
    )
    samples: list[EvidenceAnswerSample] = []
    for pair in bank.pairs:
        started = clock()
        payload = ask(pair)
        elapsed_ms = round((clock() - started) * 1000, 1)
        sample = live_answer_sample(pair, payload)
        sample.metadata["end_to_end_elapsed_ms"] = elapsed_ms
        sample.metadata["generation_elapsed_ms"] = _generation_elapsed_ms(sample)
        samples.append(sample)

    answer = EvidenceAnswerEvaluator().evaluate(samples)
    efficiency = _efficiency(samples)
    models = tuple(sorted({model for sample in samples if (model := _sample_model(sample))}))
    report = EvidenceRegressionReport(
        answer=answer,
        metadata={
            "protocol": PROTOCOL,
            "track": "end_to_end",
            "execution_profile": "fixture",
            "retrieval_adapter": "in_memory_chunk_store_with_sha256_hash_field_index",
            "retrieval_policy": retrieval_policy,
            "production_qualification": {
                "eligible": False,
                "reason": "Retrieval uses the fixture hash index, not production embeddings.",
            },
            "question_bank": str(question_bank_path),
            "question_bank_coverage": bank.coverage(),
            "corpus_snapshot_checksum": snapshot.snapshot_checksum,
            "corpus_chunk_count": len(chunks),
            "answer_models": list(models),
            "efficiency": efficiency,
        },
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    report.write(output_dir)
    _write_case_rows(output_dir / "answers.jsonl", answer)
    return BusinessAnswerEvalRun(
        output_dir=output_dir,
        answer=answer,
        efficiency=efficiency,
        coverage=bank.coverage(),
        models=models,
    )


def _generation_elapsed_ms(sample: EvidenceAnswerSample) -> float | None:
    metadata = (sample.metadata.get("answer_candidate") or {}).get("metadata") or {}
    value = (metadata.get("generated_answer_metadata") or {}).get("generation_elapsed_ms")
    return float(value) if isinstance(value, (int, float)) else None


def _sample_model(sample: EvidenceAnswerSample) -> str:
    metadata = (sample.metadata.get("answer_candidate") or {}).get("metadata") or {}
    return str(metadata.get("model") or "")


def _efficiency(samples: list[EvidenceAnswerSample]) -> dict[str, Any]:
    end_to_end = [float(s.metadata["end_to_end_elapsed_ms"]) for s in samples]
    generation = [v for s in samples if (v := s.metadata.get("generation_elapsed_ms")) is not None]
    usages = [s.metadata["model_usage"] for s in samples if isinstance(s.metadata.get("model_usage"), dict)]
    totals = {
        key: sum(int(usage.get(key) or 0) for usage in usages)
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
    }
    return {
        "cases": len(samples),
        "model_calls_recorded": len(usages),
        "end_to_end_ms": _latency_stats(end_to_end),
        "generation_ms": _latency_stats(generation),
        "tokens": totals,
        "tokens_per_model_call": (
            {key: round(value / len(usages), 1) for key, value in totals.items()} if usages else None
        ),
        # No pricing table is configured for this gateway; cost is reported as unknown.
        "estimated_cost_usd": None,
    }


def _latency_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50": _nearest_rank(ordered, 0.50),
        "p95": _nearest_rank(ordered, 0.95),
        "max": ordered[-1],
    }


def _nearest_rank(ordered: list[float], quantile: float) -> float:
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _write_case_rows(path: Path, answer: EvidenceAnswerEvalResult) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for score in answer.scores:
            sample = score.sample
            pair = sample.pair
            decision = sample.metadata.get("decision") or {}
            row = {
                "case_id": pair.metadata.get("business_case_id"),
                "category": pair.metadata.get("business_category"),
                "paper_id": pair.paper_id,
                "question": pair.question,
                "expected_behavior": pair.expected_behavior,
                "outcome": sample.outcome.value,
                "answer_success": score.answer_success,
                "failure_reason": score.failure_reason,
                "answer": sample.answer,
                "abstention_reason": (
                    ((sample.metadata.get("answer_candidate") or {}).get("metadata") or {}).get("abstention_reason")
                ),
                "cited_chunk_ids": sample.cited_chunk_ids,
                "context_chunk_ids": sample.context_chunk_ids,
                "gold_chunk_ids": pair.gold_chunk_ids,
                "citation_gold_coverage": score.citation_gold_coverage,
                "retrieval_context_coverage": score.retrieval_context_coverage,
                "decision_type": decision.get("decision_type"),
                "decision_reason": decision.get("reason"),
                "end_to_end_elapsed_ms": sample.metadata.get("end_to_end_elapsed_ms"),
                "generation_elapsed_ms": sample.metadata.get("generation_elapsed_ms"),
                "model": _sample_model(sample),
                "model_usage": sample.metadata.get("model_usage"),
            }
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


__all__ = ["BusinessAnswerEvalRun", "PROTOCOL", "run_business_answer_eval"]
