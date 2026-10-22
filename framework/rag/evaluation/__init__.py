from __future__ import annotations

from framework.rag.evaluation.answer_metrics import (
    AnswerMetricCase,
    AnswerMetricScore,
    AnswerOutcome,
    evidence_support_coverage,
    evaluate_answer_case,
    score_answer_case,
)
from framework.rag.evaluation.failure_reason import RAGFailureReason, normalize_failure_reason
from framework.rag.evaluation.report import MetricValue, RAGEvaluationReport, RAGScorecard
from framework.rag.evaluation.retrieval_metrics import (
    RetrievalMetricCase,
    evaluate_retrieval_case,
    evidence_coverage,
    hit_at_k,
    mean_reciprocal_rank,
    ndcg_at_k,
    reciprocal_rank,
    source_locator_coverage,
)
from framework.rag.evaluation.score_breakdown import SCORE_BREAKDOWN_COMPONENTS, summarize_score_breakdowns

__all__ = [
    "AnswerMetricCase",
    "AnswerMetricScore",
    "AnswerOutcome",
    "MetricValue",
    "RAGEvaluationReport",
    "RAGFailureReason",
    "RAGScorecard",
    "RetrievalMetricCase",
    "SCORE_BREAKDOWN_COMPONENTS",
    "evaluate_answer_case",
    "evidence_support_coverage",
    "evaluate_retrieval_case",
    "evidence_coverage",
    "hit_at_k",
    "mean_reciprocal_rank",
    "ndcg_at_k",
    "reciprocal_rank",
    "source_locator_coverage",
    "score_answer_case",
    "normalize_failure_reason",
    "summarize_score_breakdowns",
]
