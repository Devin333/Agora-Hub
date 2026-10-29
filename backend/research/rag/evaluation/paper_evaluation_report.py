from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from framework.rag.evaluation import RAGEvaluationReport, summarize_score_breakdowns

from backend.research.rag.adapters.evaluation_scorecard_adapter import evidence_results_to_rag_report
from backend.research.rag.evaluation.paper_answer_eval import EvidenceAnswerEvalResult, EvidenceAnswerScores
from backend.research.rag.evaluation.paper_evaluation_compare import EvidenceABResult
from backend.research.rag.adapters.paper_field_text import FIELD_NAMES
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceEvalResult, iter_ranked_score_breakdowns
from backend.research.rag.evaluation.paper_generation_eval import GenerationEvalResult


@dataclass
class EvidenceRegressionReport:
    retrieval: EvidenceEvalResult | None = None
    answer: EvidenceAnswerEvalResult | None = None
    generation: GenerationEvalResult | None = None
    ab: EvidenceABResult | None = None
    thresholds: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "metadata": dict(self.metadata),
            "thresholds": dict(self.thresholds),
            "passed": self.passed(),
            "issues": self.issues(),
            "rag_evaluation_report": self.to_rag_evaluation_report().to_dict(),
        }
        if self.retrieval is not None:
            payload["retrieval"] = _retrieval_result_to_dict(self.retrieval)
        if self.answer is not None:
            payload["answer"] = _answer_result_to_dict(self.answer)
        if self.generation is not None:
            payload["generation"] = _generation_result_to_dict(self.generation)
        if self.ab is not None:
            payload["ab"] = _ab_result_to_dict(self.ab)
        return payload

    def to_markdown(self) -> str:
        lines = ["# Paper RAG Evidence Regression Report", ""]
        lines.extend([
            f"**Status:** {'PASS' if self.passed() else 'FAIL'}",
            "",
        ])
        if self.thresholds:
            lines.extend(["## Thresholds", ""])
            for key in sorted(self.thresholds):
                lines.append(f"- `{key}` >= {self.thresholds[key]:.3f}")
            lines.append("")
        issues = self.issues()
        if issues:
            lines.extend(["## Issues", ""])
            for issue in issues:
                lines.append(f"- {issue}")
            lines.append("")
        if self.metadata:
            lines.extend(["## Metadata", ""])
            for key in sorted(self.metadata):
                lines.append(f"- `{key}`: {self.metadata[key]}")
            lines.append("")
        lines.extend(["## RAG Scorecard", "", _code_block(self.to_rag_evaluation_report().to_markdown()), ""])
        if self.retrieval is not None:
            distribution = _field_embedding_distribution(self.retrieval)
            if distribution["matched_evidence_count"]:
                lines.extend(_field_embedding_distribution_markdown(distribution))
            rerank_distribution = _rerank_distribution(self.retrieval)
            if rerank_distribution["reranked_evidence_count"]:
                lines.extend(_rerank_distribution_markdown(rerank_distribution))
            lines.extend(["## Retrieval", "", _code_block(self.retrieval.report()), ""])
        if self.answer is not None:
            lines.extend(["## Answer", "", _code_block(self.answer.report()), ""])
        if self.generation is not None:
            lines.extend(["## Generation", "", _code_block(self.generation.report()), ""])
        if self.ab is not None:
            lines.extend(["## A/B", "", _code_block(self.ab.report()), ""])
        return "\n".join(lines).rstrip() + "\n"

    def write(self, output_dir: str | Path) -> None:
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "evidence_regression_report.json").write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (target / "evidence_regression_report.md").write_text(
            self.to_markdown(),
            encoding="utf-8",
        )
        if self.answer is not None:
            _write_answer_failure_details(self.answer, target / "answer_failure_details.jsonl")

    def to_rag_evaluation_report(self) -> RAGEvaluationReport:
        return evidence_results_to_rag_report(
            retrieval=self.retrieval,
            answer=self.answer,
            generation=self.generation,
            thresholds=self.thresholds,
            metadata=self.metadata,
        )

    def passed(self) -> bool:
        return not self.issues()

    def issues(self) -> list[str]:
        issues: list[str] = []
        values = _threshold_values(self)
        for metric, threshold in self.thresholds.items():
            actual = values.get(metric)
            if actual is None:
                issues.append(f"threshold metric {metric!r} is unavailable")
                continue
            if actual < threshold:
                issues.append(f"{metric}={actual:.3f} is below threshold {threshold:.3f}")
        return issues


def _retrieval_result_to_dict(result: EvidenceEvalResult) -> dict[str, Any]:
    return {
        "total": result.total,
        "answerable_total": result.answerable_total,
        "abstain_total": result.abstain_total,
        "mrr": result.mrr(),
        "strict_mrr": result.mrr(),
        "equivalent_mrr": result.equivalent_mrr(),
        "by_k": {
            str(k): {
                "hit_rate": result.hit_rate(k),
                "strict_hit_rate": result.hit_rate(k),
                "equivalent_hit_rate": result.equivalent_hit_rate(k),
                "evidence_coverage": result.evidence_coverage(k),
                "strict_evidence_coverage": result.evidence_coverage(k),
                "equivalent_evidence_coverage": result.equivalent_evidence_coverage(k),
                "required_type_coverage": result.required_type_coverage(k),
                "source_locator_coverage": result.source_locator_coverage(k),
                "citation_accuracy": result.citation_accuracy(k),
                "image_recall": result.image_recall(k),
                "visual_evidence_coverage": result.visual_evidence_coverage(k),
                "over_retrieval_rate": result.over_retrieval_rate(k),
                "ndcg": result.ndcg(k),
            }
            for k in result.ks
        },
        "score_breakdown_summary": _score_breakdown_summary(result),
        "field_embedding_distribution": _field_embedding_distribution(result),
        "rerank_distribution": _rerank_distribution(result),
        "intent_distribution": dict(result.intent_distribution),
        "route_distribution": dict(result.route_distribution),
        "intent_confusion": {
            qa_type: dict(counts)
            for qa_type, counts in sorted(result.intent_confusion.items())
        },
        "by_qa_type": {
            qa_type: _retrieval_result_to_dict(sub)
            for qa_type, sub in result.by_qa_type.items()
        },
    }


def _score_breakdown_summary(result: EvidenceEvalResult) -> dict[str, Any]:
    top_k = max(result.ks) if result.ks else None
    summary = summarize_score_breakdowns(iter_ranked_score_breakdowns(result, top_k=top_k))
    summary["top_k"] = top_k
    return summary


def _field_embedding_distribution(result: EvidenceEvalResult) -> dict[str, Any]:
    top_k = max(result.ks) if result.ks else None
    field_scores: dict[str, list[float]] = {}
    evidence_count = 0
    for breakdown in iter_ranked_score_breakdowns(result, top_k=top_k):
        evidence_count += 1
        best_field, best_score = _best_embedding_field_from_breakdown(breakdown)
        if not best_field or best_score <= 0.0:
            continue
        field_scores.setdefault(best_field, []).append(best_score)

    search_hits: dict[str, int] = {}
    for sample in result.samples:
        raw = sample.retrieval_metadata.get("field_hits_by_name")
        if not isinstance(raw, dict):
            continue
        for field_name, count in raw.items():
            normalized = str(field_name).strip().casefold()
            if normalized not in FIELD_NAMES:
                continue
            search_hits[normalized] = search_hits.get(normalized, 0) + _safe_int(count)

    by_field = {
        field_name: {
            "count": len(scores),
            "avg_score": sum(scores) / len(scores),
            "max_score": max(scores),
        }
        for field_name, scores in sorted(field_scores.items())
        if scores
    }
    return {
        "top_k": top_k,
        "evidence_count": evidence_count,
        "matched_evidence_count": sum(item["count"] for item in by_field.values()),
        "by_field": by_field,
        "search_hits_by_field": dict(sorted(search_hits.items())),
    }


def _rerank_distribution(result: EvidenceEvalResult) -> dict[str, Any]:
    top_k = max(result.ks) if result.ks else None
    scores: list[float] = []
    evidence_count = 0
    for breakdown in iter_ranked_score_breakdowns(result, top_k=top_k):
        evidence_count += 1
        raw = breakdown.get("rerank_score")
        if raw is None:
            continue
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        scores.append(score)

    enabled_count = 0
    intent_counts: dict[str, int] = {}
    for sample in result.samples:
        metadata = sample.retrieval_metadata
        enabled = bool(
            metadata.get("reranker_enabled_for_intent")
            or metadata.get("field_reranker_enabled_for_intent")
        )
        if enabled:
            enabled_count += 1
            intent = str(sample.retrieval_intent or metadata.get("intent") or "")
            if intent:
                intent_counts[intent] = intent_counts.get(intent, 0) + 1

    return {
        "top_k": top_k,
        "evidence_count": evidence_count,
        "reranker_enabled_sample_count": enabled_count,
        "reranked_evidence_count": len(scores),
        "avg_score": sum(scores) / len(scores) if scores else 0.0,
        "min_score": min(scores) if scores else 0.0,
        "max_score": max(scores) if scores else 0.0,
        "enabled_intents": dict(sorted(intent_counts.items())),
    }


def _best_embedding_field_from_breakdown(breakdown: dict[str, float]) -> tuple[str, float]:
    best_field = ""
    best_score = 0.0
    for field_name in FIELD_NAMES:
        raw = breakdown.get(f"{field_name}_embedding_score")
        if raw is None:
            continue
        try:
            score = float(raw)
        except (TypeError, ValueError):
            continue
        if score > best_score:
            best_field = field_name
            best_score = score
    return best_field, best_score


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _field_embedding_distribution_markdown(distribution: dict[str, Any]) -> list[str]:
    lines = [
        "## Field Embedding Distribution",
        "",
        f"- top_k: `{distribution.get('top_k')}`",
        f"- evidence_count: `{distribution.get('evidence_count', 0)}`",
        f"- matched_evidence_count: `{distribution.get('matched_evidence_count', 0)}`",
        "",
    ]
    by_field = distribution.get("by_field") or {}
    if by_field:
        lines.extend([
            "| Field | count | avg | max |",
            "| --- | ---: | ---: | ---: |",
        ])
        for field_name, stats in sorted(by_field.items()):
            lines.append(
                f"| `{field_name}` | `{stats.get('count', 0)}` | "
                f"`{stats.get('avg_score', 0.0):.3f}` | `{stats.get('max_score', 0.0):.3f}` |"
            )
    search_hits = distribution.get("search_hits_by_field") or {}
    if search_hits:
        lines.extend(["", "| Search hit field | count |", "| --- | ---: |"])
        for field_name, count in sorted(search_hits.items()):
            lines.append(f"| `{field_name}` | `{count}` |")
    lines.append("")
    return lines


def _rerank_distribution_markdown(distribution: dict[str, Any]) -> list[str]:
    lines = [
        "## Rerank Distribution",
        "",
        f"- top_k: `{distribution.get('top_k')}`",
        f"- reranker_enabled_sample_count: `{distribution.get('reranker_enabled_sample_count', 0)}`",
        f"- reranked_evidence_count: `{distribution.get('reranked_evidence_count', 0)}`",
        f"- avg_score: `{distribution.get('avg_score', 0.0):.3f}`",
        f"- min_score: `{distribution.get('min_score', 0.0):.3f}`",
        f"- max_score: `{distribution.get('max_score', 0.0):.3f}`",
        "",
    ]
    intents = distribution.get("enabled_intents") or {}
    if intents:
        lines.extend(["| Intent | enabled samples |", "| --- | ---: |"])
        for intent, count in sorted(intents.items()):
            lines.append(f"| `{intent}` | `{count}` |")
        lines.append("")
    return lines


def _answer_result_to_dict(result: EvidenceAnswerEvalResult) -> dict[str, Any]:
    return {
        "total": result.total,
        "answer_fact_coverage": result.answer_fact_coverage(),
        "fact_match_coverage": result.answer_fact_coverage(),
        "retrieval_context_coverage": result.retrieval_context_coverage_score(),
        "citation_grounding_score": result.citation_grounding_score(),
        "citation_gold_coverage": result.citation_gold_coverage_score(),
        "strict_retrieval_context_coverage": result.strict_retrieval_context_coverage_score(),
        "equivalent_retrieval_context_coverage": result.equivalent_retrieval_context_coverage_score(),
        "strict_citation_gold_coverage": result.strict_citation_gold_coverage_score(),
        "equivalent_citation_gold_coverage": result.equivalent_citation_gold_coverage_score(),
        "equivalent_supported_rate": result.equivalent_supported_rate(),
        "claim_support_coverage": result.claim_support_coverage_score(),
        "true_missing_gold_rate": result.true_missing_gold_rate(),
        "source_locator_grounding_score": result.source_locator_grounding_score(),
        "abstention_accuracy": result.abstention_accuracy(),
        "success_rate": result.success_rate(),
        "outcome_counts": result.outcome_counts(),
        "abstention_basis_counts": result.abstention_basis_counts(),
        "system_failure_rate": result.system_failure_rate(),
        "over_abstention_rate": result.over_abstention_rate(),
        "failure_reason_counts": result.failure_reason_counts(),
        "diagnostic_tag_counts": result.diagnostic_tag_counts(),
        "by_qa_type": {
            qa_type: _answer_result_to_dict(sub)
            for qa_type, sub in result.by_qa_type.items()
        },
    }


def _write_answer_failure_details(result: EvidenceAnswerEvalResult, path: Path) -> None:
    rows = [
        _answer_failure_detail(score)
        for score in result.scores
        if not score.answer_success or score.failure_reason or score.diagnostic_tags
    ]
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _answer_failure_detail(score: EvidenceAnswerScores) -> dict[str, Any]:
    sample = score.sample
    pair = sample.pair
    return {
        "paper_id": pair.paper_id,
        "qa_type": pair.qa_type,
        "expected_behavior": pair.expected_behavior,
        "question": pair.question,
        "failure_reason": score.failure_reason,
        "diagnostic_tags": list(score.diagnostic_tags),
        "answer_success": score.answer_success,
        "abstention_correct": score.abstention_correct,
        "fact_coverage": score.fact_coverage,
        "retrieval_context_coverage": score.retrieval_context_coverage,
        "citation_grounding": score.citation_grounding,
        "citation_gold_coverage": score.citation_gold_coverage,
        "source_locator_grounding": score.source_locator_grounding,
        "gold_chunk_ids": list(pair.gold_chunk_ids),
        "equivalent_gold_chunk_ids": list(pair.equivalent_gold_chunk_ids),
        "required_primary_evidence_ids": list(pair.required_primary_evidence_ids),
        "acceptable_support_evidence_ids": list(pair.acceptable_support_evidence_ids),
        "gold_source_locators": list(pair.gold_source_locators),
        "context_chunk_ids": list(sample.context_chunk_ids),
        "cited_chunk_ids": list(sample.cited_chunk_ids),
        "cited_source_locators": list(sample.cited_source_locators),
        "matched_facts": list(score.matched_facts),
        "missing_facts": list(score.missing_facts),
        "answer": _truncate_text(sample.answer, limit=1200),
        "status": str(sample.metadata.get("status") or ""),
        "generation_mode": str(sample.metadata.get("generation_mode") or ""),
        "transcript_id": str(sample.metadata.get("transcript_id") or ""),
        "gate_results": list(sample.metadata.get("gate_results") or []),
        "decision": dict(sample.metadata.get("decision") or {}),
    }


def _truncate_text(text: str, *, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _generation_result_to_dict(result: GenerationEvalResult) -> dict[str, Any]:
    return {
        "total": len(result.per_sample),
        "faithfulness": result.faithfulness_score(),
        "answer_relevancy": result.answer_relevancy_score(),
        "context_precision": result.context_precision_score(),
        "claim_support_rate": result.claim_support_rate_score(),
        "contradiction_rate": result.contradiction_rate_score(),
        "unsupported_claim_rate": result.unsupported_claim_rate_score(),
        "citation_grounding_rate": result.citation_grounding_rate_score(),
        "citation_claim_support_rate": result.citation_claim_support_rate_score(),
        "wrong_citation_rate": result.wrong_citation_rate_score(),
        "missing_citation_rate": result.missing_citation_rate_score(),
        "grounded_answer_rate": result.grounded_answer_rate_score(),
        "judge_error_rate": result.judge_error_rate(),
        "per_sample": [score.to_dict() for score in result.per_sample],
        "sample_judgments": [judgment.to_dict() for judgment in result.sample_judgments],
    }


def _ab_result_to_dict(result: EvidenceABResult) -> dict[str, Any]:
    return {
        "baseline_name": result.baseline_name,
        "candidate_name": result.candidate_name,
        "deltas": [
            {
                "metric": delta.metric,
                "k": delta.k,
                "baseline_value": delta.baseline_value,
                "candidate_value": delta.candidate_value,
                "delta": delta.delta,
            }
            for delta in result.deltas
        ],
    }


def _threshold_values(report: EvidenceRegressionReport) -> dict[str, float]:
    values: dict[str, float] = {}
    if report.retrieval is not None:
        best_k = max(report.retrieval.ks) if report.retrieval.ks else 0
        if best_k:
            values.update({
                "retrieval.hit_rate": report.retrieval.hit_rate(best_k),
                "retrieval.equivalent_hit_rate": report.retrieval.equivalent_hit_rate(best_k),
                "retrieval.evidence_coverage": report.retrieval.evidence_coverage(best_k),
                "retrieval.equivalent_evidence_coverage": report.retrieval.equivalent_evidence_coverage(best_k),
                "retrieval.required_type_coverage": report.retrieval.required_type_coverage(best_k),
                "retrieval.source_locator_coverage": report.retrieval.source_locator_coverage(best_k),
                "retrieval.citation_accuracy": report.retrieval.citation_accuracy(best_k),
                "retrieval.image_recall": report.retrieval.image_recall(best_k),
            })
        values["retrieval.mrr"] = report.retrieval.mrr()
        values["retrieval.equivalent_mrr"] = report.retrieval.equivalent_mrr()
    if report.answer is not None:
        values.update({
            "answer.fact_coverage": report.answer.answer_fact_coverage(),
            "answer.fact_match_coverage": report.answer.answer_fact_coverage(),
            "answer.retrieval_context_coverage": report.answer.retrieval_context_coverage_score(),
            "answer.citation_grounding": report.answer.citation_grounding_score(),
            "answer.citation_gold_coverage": report.answer.citation_gold_coverage_score(),
            "answer.strict_retrieval_context_coverage": report.answer.strict_retrieval_context_coverage_score(),
            "answer.equivalent_retrieval_context_coverage": report.answer.equivalent_retrieval_context_coverage_score(),
            "answer.strict_citation_gold_coverage": report.answer.strict_citation_gold_coverage_score(),
            "answer.equivalent_citation_gold_coverage": report.answer.equivalent_citation_gold_coverage_score(),
            "answer.equivalent_supported_rate": report.answer.equivalent_supported_rate(),
            "answer.claim_support_coverage": report.answer.claim_support_coverage_score(),
            "answer.source_locator_grounding": report.answer.source_locator_grounding_score(),
            "answer.abstention_accuracy": report.answer.abstention_accuracy(),
            "answer.success_rate": report.answer.success_rate(),
        })
    if report.generation is not None:
        values.update({
            "generation.faithfulness": report.generation.faithfulness_score(),
            "generation.answer_relevancy": report.generation.answer_relevancy_score(),
            "generation.context_precision": report.generation.context_precision_score(),
            "generation.claim_support_rate": report.generation.claim_support_rate_score(),
            "generation.contradiction_rate": report.generation.contradiction_rate_score(),
            "generation.unsupported_claim_rate": report.generation.unsupported_claim_rate_score(),
            "generation.citation_grounding_rate": report.generation.citation_grounding_rate_score(),
            "generation.citation_claim_support_rate": report.generation.citation_claim_support_rate_score(),
            "generation.wrong_citation_rate": report.generation.wrong_citation_rate_score(),
            "generation.missing_citation_rate": report.generation.missing_citation_rate_score(),
            "generation.grounded_answer_rate": report.generation.grounded_answer_rate_score(),
            "generation.judge_error_rate": report.generation.judge_error_rate(),
        })
    if report.ab is not None:
        for delta in report.ab.deltas:
            suffix = f"@{delta.k}" if delta.k is not None else ""
            values[f"ab.{delta.metric}{suffix}.delta"] = delta.delta
    return {key: value for key, value in values.items() if value is not None}


def _code_block(text: str) -> str:
    return f"```text\n{text}\n```"


__all__ = ["EvidenceRegressionReport"]
