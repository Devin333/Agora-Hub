"""Per-case records and aggregate metrics for the QASPER end-to-end answer evaluation.

Every rate carries its denominator. Cases with no defined value for a metric (for
example citation coverage on a mapping that is not ``valid``, or a judge error) are
excluded from that metric's denominator instead of being counted as zero.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.research.rag.evaluation.qasper_answer_judge import JudgeVerdict
from backend.research.rag.evaluation.qasper_answer_metrics import ANSWER_TYPES, AnswerType
from framework.rag.evaluation.answer_metrics import AnswerOutcome


@dataclass(frozen=True)
class QasperAnswerCase:
    case_id: str
    paper_id: str
    question: str
    stratum: AnswerType
    references: tuple[str, ...]
    reference_types: tuple[str, ...]
    mapping_disposition: str
    outcome: AnswerOutcome
    answer: str
    prediction: str
    answer_f1: float
    matched_type: AnswerType
    cited_chunk_ids: tuple[str, ...]
    context_chunk_ids: tuple[str, ...]
    strict_citation_coverage: float | None
    equivalent_citation_coverage: float | None
    strict_context_coverage: float | None
    equivalent_context_coverage: float | None
    judge: JudgeVerdict | None
    decision_reason: str
    abstention_reason: str
    end_to_end_ms: float
    generation_ms: float | None
    prompt_chars: int | None
    model: str
    model_usage: dict[str, Any] | None
    evidence_type_gap_accepted: bool = False
    generation_error: str = ""

    @property
    def expects_abstention(self) -> bool:
        return all(kind == "none" for kind in self.reference_types)

    @property
    def expects_answer(self) -> bool:
        return all(kind != "none" for kind in self.reference_types)

    def end_to_end_correct(self) -> bool | None:
        """Headline correctness; None only when a judge error leaves an answer ungraded."""
        if self.outcome.is_system_failure:
            return False
        if self.outcome.is_refusal:
            # A refusal is right exactly when some annotator found the question unanswerable.
            return "none" in self.reference_types
        if self.judge is None or self.judge.judge_error:
            return None
        return bool(self.judge.correct)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "paper_id": self.paper_id,
            "question": self.question,
            "stratum": self.stratum,
            "references": list(self.references),
            "reference_types": list(self.reference_types),
            "mapping_disposition": self.mapping_disposition,
            "outcome": self.outcome.value,
            "answer": self.answer,
            "prediction": self.prediction,
            "answer_f1": self.answer_f1,
            "matched_type": self.matched_type,
            "cited_chunk_ids": list(self.cited_chunk_ids),
            "context_chunk_ids": list(self.context_chunk_ids),
            "strict_citation_coverage": self.strict_citation_coverage,
            "equivalent_citation_coverage": self.equivalent_citation_coverage,
            "strict_context_coverage": self.strict_context_coverage,
            "equivalent_context_coverage": self.equivalent_context_coverage,
            "judge": self.judge.to_dict() if self.judge is not None else None,
            "end_to_end_correct": self.end_to_end_correct(),
            "decision_reason": self.decision_reason,
            "abstention_reason": self.abstention_reason,
            "end_to_end_ms": self.end_to_end_ms,
            "generation_ms": self.generation_ms,
            "prompt_chars": self.prompt_chars,
            "model": self.model,
            "model_usage": self.model_usage,
            "evidence_type_gap_accepted": self.evidence_type_gap_accepted,
            "generation_error": self.generation_error,
        }


@dataclass(frozen=True)
class Rate:
    value: float | None
    numerator: float
    denominator: int

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "numerator": self.numerator, "denominator": self.denominator}


def _rate(numerator: float, denominator: int) -> Rate:
    return Rate(numerator / denominator if denominator else None, numerator, denominator)


def _mean(values: Sequence[float]) -> Rate:
    return _rate(sum(values), len(values))


@dataclass(frozen=True)
class QasperAnswerSummary:
    cases: tuple[QasperAnswerCase, ...]
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        cases = self.cases
        answered = [case for case in cases if case.outcome is AnswerOutcome.ANSWERED]
        judged = [case for case in answered if case.judge is not None and not case.judge.judge_error]
        expect_abstain = [case for case in cases if case.expects_abstention and not case.outcome.is_system_failure]
        expect_answer = [case for case in cases if case.expects_answer and not case.outcome.is_system_failure]
        graded = [value for case in cases if (value := case.end_to_end_correct()) is not None]
        citation_support = [
            rate for case in judged if (rate := case.judge.citation_support_rate()) is not None
        ]
        return {
            "cases": len(cases),
            "outcome_counts": _counts(case.outcome.value for case in cases),
            "answer_f1": _mean([case.answer_f1 for case in cases]).to_dict(),
            "answer_f1_by_stratum": {
                kind: _mean([case.answer_f1 for case in cases if case.stratum == kind]).to_dict()
                for kind in ANSWER_TYPES
            },
            "answer_f1_by_matched_type": {
                kind: _mean([case.answer_f1 for case in cases if case.matched_type == kind]).to_dict()
                for kind in ANSWER_TYPES
            },
            "end_to_end_correct": _rate(sum(1 for value in graded if value), len(graded)).to_dict(),
            "judge_correct_on_answered": _rate(
                sum(1 for case in judged if case.judge.correct), len(judged)
            ).to_dict(),
            "judge_citation_support": _mean(citation_support).to_dict(),
            "judge_errors": sum(1 for case in answered if case.judge is not None and case.judge.judge_error),
            "correct_abstention": _rate(
                sum(1 for case in expect_abstain if case.outcome.is_refusal), len(expect_abstain)
            ).to_dict(),
            "over_abstention": _rate(
                sum(1 for case in expect_answer if case.outcome.is_refusal), len(expect_answer)
            ).to_dict(),
            "system_failure": _rate(
                sum(1 for case in cases if case.outcome.is_system_failure), len(cases)
            ).to_dict(),
            "strict_citation_coverage": _defined_mean(case.strict_citation_coverage for case in answered),
            "equivalent_citation_coverage": _defined_mean(case.equivalent_citation_coverage for case in answered),
            "strict_context_coverage": _defined_mean(case.strict_context_coverage for case in cases),
            "equivalent_context_coverage": _defined_mean(case.equivalent_context_coverage for case in cases),
            "mapping_dispositions": _counts(case.mapping_disposition for case in cases),
            "evidence_type_gap_accepted": sum(1 for case in cases if case.evidence_type_gap_accepted),
            "efficiency": _efficiency(cases),
            **self.extra,
        }


def _defined_mean(values) -> dict[str, Any]:
    return _mean([value for value in values if value is not None]).to_dict()


def _counts(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _efficiency(cases: Sequence[QasperAnswerCase]) -> dict[str, Any]:
    usages = [case.model_usage for case in cases if isinstance(case.model_usage, dict)]
    prompt_chars = [case.prompt_chars for case in cases if case.prompt_chars is not None]
    return {
        "end_to_end_ms": _latency([case.end_to_end_ms for case in cases]),
        "generation_ms": _latency([case.generation_ms for case in cases if case.generation_ms is not None]),
        "model_calls_recorded": len(usages),
        "tokens": {
            key: sum(int(usage.get(key) or 0) for usage in usages)
            for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
        },
        # Gateway token counts can exceed what was sent; compare with characters actually sent.
        "prompt_chars": {"n": len(prompt_chars), "total": sum(prompt_chars), "max": max(prompt_chars, default=None)},
        "judge_calls": sum(1 for case in cases if case.judge is not None),
        "estimated_cost_usd": None,
    }


def _latency(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    ordered = sorted(values)

    def rank(q: float) -> float:
        return ordered[max(0, math.ceil(q * len(ordered)) - 1)]

    return {"n": len(ordered), "p50": rank(0.50), "p95": rank(0.95), "max": ordered[-1]}


__all__ = ["QasperAnswerCase", "QasperAnswerSummary", "Rate"]
