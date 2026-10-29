"""Per-case records and aggregate metrics for the QASPER end-to-end answer evaluation.

Every rate carries its denominator. Cases with no defined value for a metric (for
example citation coverage on a mapping that is not ``valid``, or a judge error) are
excluded from that metric's denominator instead of being counted as zero.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.research.rag.evaluation.qasper_answer_judge import JudgeRequest, JudgeVerdict
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
    # Redacted provider/model diagnostics for a failed generation; empty otherwise.
    generation_error_detail: dict[str, Any] | None = None
    judge_request: JudgeRequest | None = None
    rejudge_attempt: dict[str, Any] | None = None
    # Answer-side evidence selection, when enabled: the scored pool, per-passage
    # relevance, the Adaptive-k decision, summarizer usage, and how much gold
    # evidence the pool held before selection.
    evidence_selection: dict[str, Any] | None = None
    # Retrieval-side section navigation, when enabled: the sections read and chosen,
    # the passages added and displaced, navigator usage, and whether the added
    # passages hold gold evidence.
    section_navigation: dict[str, Any] | None = None

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
            "generation_error_detail": self.generation_error_detail,
            "judge_request": self.judge_request.to_dict() if self.judge_request is not None else None,
            "rejudge_attempt": self.rejudge_attempt,
            "evidence_selection": self.evidence_selection,
            "section_navigation": self.section_navigation,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "QasperAnswerCase":
        """Rebuild a record from ``answers.jsonl`` so runs can be merged and re-summarized."""
        if not isinstance(payload, Mapping):
            raise TypeError("answer case must be an object")
        judge = payload.get("judge")
        judge_request = payload.get("judge_request")
        rejudge_attempt = payload.get("rejudge_attempt")
        if judge is not None and not isinstance(judge, Mapping):
            raise ValueError("case.judge must be an object or null")
        if judge_request is not None and not isinstance(judge_request, Mapping):
            raise ValueError("case.judge_request must be an object or null")
        if rejudge_attempt is not None and not isinstance(rejudge_attempt, Mapping):
            raise ValueError("case.rejudge_attempt must be an object or null")
        evidence_type_gap_accepted = payload.get("evidence_type_gap_accepted", False)
        if not isinstance(evidence_type_gap_accepted, bool):
            raise ValueError("case.evidence_type_gap_accepted must be a boolean")
        stratum = _answer_type(payload.get("stratum"), "case.stratum")
        matched_type = _answer_type(payload.get("matched_type"), "case.matched_type")
        return cls(
            case_id=_string(payload.get("case_id"), "case.case_id"),
            paper_id=_string(payload.get("paper_id"), "case.paper_id"),
            question=_string(payload.get("question"), "case.question"),
            stratum=stratum,
            references=_strings(payload.get("references"), "case.references"),
            reference_types=_strings(payload.get("reference_types"), "case.reference_types"),
            mapping_disposition=_string(payload.get("mapping_disposition"), "case.mapping_disposition"),
            outcome=AnswerOutcome(_string(payload.get("outcome"), "case.outcome")),
            answer=_string(payload.get("answer"), "case.answer"),
            prediction=_string(payload.get("prediction"), "case.prediction"),
            answer_f1=_number(payload.get("answer_f1"), "case.answer_f1"),
            matched_type=matched_type,
            cited_chunk_ids=_strings(payload.get("cited_chunk_ids"), "case.cited_chunk_ids"),
            context_chunk_ids=_strings(payload.get("context_chunk_ids"), "case.context_chunk_ids"),
            strict_citation_coverage=_optional_number(
                payload.get("strict_citation_coverage"), "case.strict_citation_coverage",
            ),
            equivalent_citation_coverage=_optional_number(
                payload.get("equivalent_citation_coverage"), "case.equivalent_citation_coverage",
            ),
            strict_context_coverage=_optional_number(
                payload.get("strict_context_coverage"), "case.strict_context_coverage",
            ),
            equivalent_context_coverage=_optional_number(
                payload.get("equivalent_context_coverage"), "case.equivalent_context_coverage",
            ),
            judge=JudgeVerdict.from_dict(judge) if judge is not None else None,
            decision_reason=_string(payload.get("decision_reason"), "case.decision_reason"),
            abstention_reason=_string(payload.get("abstention_reason"), "case.abstention_reason"),
            end_to_end_ms=_number(payload.get("end_to_end_ms"), "case.end_to_end_ms"),
            generation_ms=_optional_number(payload.get("generation_ms"), "case.generation_ms"),
            prompt_chars=_optional_int(payload.get("prompt_chars"), "case.prompt_chars"),
            model=_string(payload.get("model"), "case.model"),
            model_usage=_optional_mapping(payload.get("model_usage"), "case.model_usage"),
            evidence_type_gap_accepted=evidence_type_gap_accepted,
            generation_error=_string(payload.get("generation_error", ""), "case.generation_error"),
            generation_error_detail=_optional_mapping(
                payload.get("generation_error_detail"), "case.generation_error_detail",
            ),
            judge_request=JudgeRequest.from_dict(judge_request) if judge_request is not None else None,
            rejudge_attempt=dict(rejudge_attempt) if rejudge_attempt is not None else None,
            evidence_selection=_optional_mapping(payload.get("evidence_selection"), "case.evidence_selection"),
            section_navigation=_optional_mapping(payload.get("section_navigation"), "case.section_navigation"),
        )

    @property
    def needs_generation_rerun(self) -> bool:
        return self.outcome.is_system_failure

    @property
    def needs_judge_rerun(self) -> bool:
        return self.outcome is AnswerOutcome.ANSWERED and self.judge is not None and self.judge.judge_error


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
            **_evidence_selection_metrics(cases),
            **_section_navigation_metrics(cases),
            "mapping_dispositions": _counts(case.mapping_disposition for case in cases),
            "evidence_type_gap_accepted": sum(1 for case in cases if case.evidence_type_gap_accepted),
            "efficiency": _efficiency(cases),
            **self.extra,
        }


def _evidence_selection_metrics(cases: Sequence[QasperAnswerCase]) -> dict[str, Any]:
    selected = [case.evidence_selection for case in cases if case.evidence_selection is not None]
    if not selected:
        return {}
    usages = [item.get("usage") for item in selected if isinstance(item.get("usage"), Mapping)]
    diagnostics = [
        item.get("summarizer_diagnostics")
        for item in selected
        if isinstance(item.get("summarizer_diagnostics"), Mapping)
    ]
    passages = [len(item.get("selected_ids") or ()) for item in selected]
    return {
        "evidence_selection": {
            "cases": len(selected),
            "pool_equivalent_context_coverage": _defined_mean(
                _optional_float(item.get("pool_equivalent_context_coverage")) for item in selected
            ),
            "pool_strict_context_coverage": _defined_mean(
                _optional_float(item.get("pool_strict_context_coverage")) for item in selected
            ),
            "selected_passages": {
                "mean": sum(passages) / len(passages),
                "min": min(passages),
                "max": max(passages),
            },
            "gap_applied": sum(
                1 for item in selected
                if isinstance(item.get("adaptive_k"), Mapping) and item["adaptive_k"].get("gap_applied")
            ),
            "summarizer_calls": sum(int(item.get("model_calls") or 0) for item in diagnostics),
            "malformed_replies": sum(int(item.get("malformed_replies") or 0) for item in diagnostics),
            "unassessed_passages": sum(int(item.get("unassessed_passages") or 0) for item in diagnostics),
            "rejected_quotes": sum(int(item.get("rejected_quotes") or 0) for item in diagnostics),
            "tokens": {
                key: sum(int(usage.get(key) or 0) for usage in usages)
                for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
            },
        },
    }


def _section_navigation_metrics(cases: Sequence[QasperAnswerCase]) -> dict[str, Any]:
    navigated = [case.section_navigation for case in cases if case.section_navigation is not None]
    if not navigated:
        return {}
    steps = [
        step
        for item in navigated
        for step in (item.get("steps") or ())
        if isinstance(step, Mapping)
    ]
    usages = [step.get("usage") for step in steps if isinstance(step.get("usage"), Mapping)]
    diagnostics = [step.get("diagnostics") for step in steps if isinstance(step.get("diagnostics"), Mapping)]
    added = [len(item.get("added_chunk_ids") or ()) for item in navigated]
    return {
        "section_navigation": {
            "cases": len(navigated),
            "cases_with_added_passages": sum(1 for count in added if count),
            "added_passages": sum(added),
            "displaced_passages": sum(len(item.get("displaced_chunk_ids") or ()) for item in navigated),
            # Cases where a navigated passage holds gold evidence and survived into context.
            "added_gold_in_context": sum(1 for item in navigated if item.get("added_gold_in_context")),
            # Skips decided by the expander (no structure) or the navigator (all read).
            "skipped": _counts(
                str(reason)
                for step in steps
                if (reason := step.get("skipped") or (step.get("diagnostics") or {}).get("skipped"))
            ),
            "errors": _counts(str(step.get("error")) for step in steps if step.get("error")),
            "navigator_calls": sum(int(item.get("model_calls") or 0) for item in diagnostics),
            "malformed_replies": sum(int(item.get("malformed_replies") or 0) for item in diagnostics),
            "tokens": {
                key: sum(int(usage.get(key) or 0) for usage in usages)
                for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")
            },
        },
    }


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _defined_mean(values) -> dict[str, Any]:
    return _mean([value for value in values if value is not None]).to_dict()


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be an array of strings")
    return tuple(value)


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def _optional_number(value: Any, name: str) -> float | None:
    return None if value is None else _number(value, name)


def _optional_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer or null")
    return value


def _optional_mapping(value: Any, name: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object or null")
    return dict(value)


def _answer_type(value: Any, name: str) -> AnswerType:
    if value not in ANSWER_TYPES:
        raise ValueError(f"{name} must be a known answer type")
    return value


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
