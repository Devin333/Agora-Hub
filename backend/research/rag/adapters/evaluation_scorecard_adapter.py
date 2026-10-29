from __future__ import annotations

from typing import Any

from framework.rag.evaluation import (
    MetricValue,
    RAGEvaluationReport,
    RAGFailureReason,
    RAGScorecard,
    normalize_failure_reason,
    summarize_score_breakdowns,
)

from backend.research.rag.evaluation.paper_answer_eval import EvidenceAnswerEvalResult
from backend.research.rag.evaluation.paper_evidence_eval import EvidenceEvalResult, iter_ranked_score_breakdowns
from backend.research.rag.evaluation.paper_generation_eval import GenerationEvalResult


def evidence_results_to_rag_report(
    *,
    retrieval: EvidenceEvalResult | None = None,
    answer: EvidenceAnswerEvalResult | None = None,
    generation: GenerationEvalResult | None = None,
    thresholds: dict[str, float] | None = None,
    metadata: dict[str, Any] | None = None,
    run_id: str = "paper-rag-evidence-regression",
) -> RAGEvaluationReport:
    return RAGEvaluationReport(
        title="Paper RAG Evidence Regression",
        scorecard=evidence_results_to_rag_scorecard(
            retrieval=retrieval,
            answer=answer,
            generation=generation,
            thresholds=thresholds or {},
            metadata=metadata or {},
            run_id=run_id,
        ),
    )


def evidence_results_to_rag_scorecard(
    *,
    retrieval: EvidenceEvalResult | None = None,
    answer: EvidenceAnswerEvalResult | None = None,
    generation: GenerationEvalResult | None = None,
    thresholds: dict[str, float] | None = None,
    metadata: dict[str, Any] | None = None,
    run_id: str = "paper-rag-evidence-regression",
) -> RAGScorecard:
    metrics: list[MetricValue] = []
    if retrieval is not None:
        metrics.extend(_retrieval_metrics(retrieval))
    if answer is not None:
        metrics.extend(_answer_metrics(answer))
    if generation is not None:
        metrics.extend(_generation_metrics(generation))
    scorecard_metadata = {
        **dict(metadata or {}),
        "thresholds": dict(thresholds or {}),
        "paper_specific_metrics": _paper_specific_metrics(retrieval),
        "score_breakdown_summary": _score_breakdown_summary(retrieval),
        "raw_failure_reason_counts": answer.failure_reason_counts() if answer is not None else {},
        "answer_diagnostic_tag_counts": answer.diagnostic_tag_counts() if answer is not None else {},
        "skipped_none_metrics": [],
    }
    if answer is not None:
        scorecard_metadata["skipped_none_metrics"] = _none_answer_metric_names(answer)
    metrics = [metric for metric in metrics if metric.value is not None]
    return RAGScorecard(
        run_id=run_id,
        metrics=tuple(metrics),
        failure_reasons=_mapped_failure_reasons(answer),
        metadata=scorecard_metadata,
    )


def _retrieval_metrics(result: EvidenceEvalResult) -> list[MetricValue]:
    metrics = [
        MetricValue("retrieval.mrr", result.mrr()),
        MetricValue("retrieval.equivalent_mrr", result.equivalent_mrr()),
        MetricValue("retrieval.total", result.total),
        MetricValue("retrieval.answerable_total", result.answerable_total),
    ]
    for k in result.ks:
        metrics.extend([
            MetricValue(f"retrieval.hit_at_{k}", result.hit_rate(k), {"k": k}),
            MetricValue(f"retrieval.equivalent_hit_at_{k}", result.equivalent_hit_rate(k), {"k": k}),
            MetricValue(f"retrieval.evidence_coverage_at_{k}", result.evidence_coverage(k), {"k": k}),
            MetricValue(
                f"retrieval.equivalent_evidence_coverage_at_{k}",
                result.equivalent_evidence_coverage(k),
                {"k": k},
            ),
            MetricValue(f"retrieval.source_locator_coverage_at_{k}", result.source_locator_coverage(k), {"k": k}),
            MetricValue(f"retrieval.ndcg_at_{k}", result.ndcg(k), {"k": k}),
        ])
    return metrics


def _answer_metrics(result: EvidenceAnswerEvalResult) -> list[MetricValue]:
    values = _answer_metric_values(result)
    return [MetricValue(name, value) for name, value in values.items() if value is not None]


def _answer_metric_values(result: EvidenceAnswerEvalResult) -> dict[str, float | None]:
    return {
        "answer.fact_coverage": result.answer_fact_coverage(),
        "answer.retrieval_context_coverage": result.retrieval_context_coverage_score(),
        "answer.citation_grounding": result.citation_grounding_score(),
        "answer.citation_gold_coverage": result.citation_gold_coverage_score(),
        "answer.strict_retrieval_context_coverage": result.strict_retrieval_context_coverage_score(),
        "answer.equivalent_retrieval_context_coverage": result.equivalent_retrieval_context_coverage_score(),
        "answer.strict_citation_gold_coverage": result.strict_citation_gold_coverage_score(),
        "answer.equivalent_citation_gold_coverage": result.equivalent_citation_gold_coverage_score(),
        "answer.equivalent_supported_rate": result.equivalent_supported_rate(),
        "answer.claim_support_coverage": result.claim_support_coverage_score(),
        "answer.true_missing_gold_rate": result.true_missing_gold_rate(),
        "answer.source_locator_grounding": result.source_locator_grounding_score(),
        "answer.abstention_accuracy": result.abstention_accuracy(),
        "answer.success_rate": result.success_rate(),
        "answer.system_failure_rate": result.system_failure_rate(),
        "answer.over_abstention_rate": result.over_abstention_rate(),
    }


def _none_answer_metric_names(result: EvidenceAnswerEvalResult) -> list[str]:
    return [name for name, value in _answer_metric_values(result).items() if value is None]


def _generation_metrics(result: GenerationEvalResult) -> list[MetricValue]:
    return [
        MetricValue("generation.faithfulness", result.faithfulness_score()),
        MetricValue("generation.answer_relevancy", result.answer_relevancy_score()),
        MetricValue("generation.context_precision", result.context_precision_score()),
        MetricValue("generation.claim_support_rate", result.claim_support_rate_score()),
        MetricValue("generation.contradiction_rate", result.contradiction_rate_score()),
        MetricValue("generation.unsupported_claim_rate", result.unsupported_claim_rate_score()),
        MetricValue("generation.citation_grounding_rate", result.citation_grounding_rate_score()),
        MetricValue("generation.citation_claim_support_rate", result.citation_claim_support_rate_score()),
        MetricValue("generation.wrong_citation_rate", result.wrong_citation_rate_score()),
        MetricValue("generation.missing_citation_rate", result.missing_citation_rate_score()),
        MetricValue("generation.grounded_answer_rate", result.grounded_answer_rate_score()),
        MetricValue("generation.judge_error_rate", result.judge_error_rate()),
    ]


def _paper_specific_metrics(result: EvidenceEvalResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    return {
        str(k): {
            "required_type_coverage": result.required_type_coverage(k),
            "equivalent_hit_rate": result.equivalent_hit_rate(k),
            "equivalent_evidence_coverage": result.equivalent_evidence_coverage(k),
            "image_recall": result.image_recall(k),
            "visual_evidence_coverage": result.visual_evidence_coverage(k),
            "citation_accuracy": result.citation_accuracy(k),
            "overlap_citation_accuracy": result.overlap_citation_accuracy(k),
            "over_retrieval_rate": result.over_retrieval_rate(k),
        }
        for k in result.ks
    }


def _score_breakdown_summary(result: EvidenceEvalResult | None) -> dict[str, Any]:
    if result is None:
        return {}
    top_k = max(result.ks) if result.ks else None
    summary = summarize_score_breakdowns(iter_ranked_score_breakdowns(result, top_k=top_k))
    summary["top_k"] = top_k
    return summary


def _mapped_failure_reasons(result: EvidenceAnswerEvalResult | None) -> tuple[RAGFailureReason, ...]:
    if result is None:
        return ()
    mapped: list[RAGFailureReason] = []
    for reason in result.failure_reason_counts():
        generic_reason = normalize_failure_reason(reason)
        if generic_reason not in mapped:
            mapped.append(generic_reason)
    return tuple(mapped)


__all__ = [
    "evidence_results_to_rag_report",
    "evidence_results_to_rag_scorecard",
]
