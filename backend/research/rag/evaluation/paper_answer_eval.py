from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair
from framework.rag.evaluation import AnswerMetricCase, AnswerOutcome, evidence_support_coverage, score_answer_case
from framework.rag.evaluation.failure_reason import RAGFailureReason


OVERCONSERVATIVE_ABSTENTION_REASON = RAGFailureReason.ABSTAINED_OVER_CONSERVATIVE.value
WRONG_ABSTENTION_REASON = RAGFailureReason.ABSTENTION_WRONG.value


@dataclass
class EvidenceAnswerSample:
    pair: EvidenceQAPair
    answer: str
    outcome: AnswerOutcome
    cited_chunk_ids: list[str] = field(default_factory=list)
    cited_source_locators: list[str] = field(default_factory=list)
    context_chunk_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.outcome = AnswerOutcome(self.outcome)
        if self.outcome is not AnswerOutcome.ANSWERED and str(self.answer or "").strip():
            raise ValueError("only answered samples may carry answer text")


@dataclass(frozen=True)
class EvidenceAnswerScores:
    sample: EvidenceAnswerSample
    fact_coverage: float | None
    citation_grounding: float | None
    source_locator_grounding: float | None
    abstention_correct: float | None
    answer_success: bool
    retrieval_context_coverage: float | None = None
    citation_gold_coverage: float | None = None
    strict_retrieval_context_coverage: float | None = None
    equivalent_retrieval_context_coverage: float | None = None
    strict_citation_gold_coverage: float | None = None
    equivalent_citation_gold_coverage: float | None = None
    equivalent_gold_supported: bool = False
    claim_support_coverage: float | None = None
    diagnostic_tags: tuple[str, ...] = ()
    failure_reason: str = ""
    matched_facts: tuple[str, ...] = ()
    missing_facts: tuple[str, ...] = ()

    @property
    def qa_type(self) -> str:
        return self.sample.pair.qa_type


@dataclass
class EvidenceAnswerEvalResult:
    scores: list[EvidenceAnswerScores] = field(default_factory=list)
    by_qa_type: dict[str, "EvidenceAnswerEvalResult"] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.scores)

    def answer_fact_coverage(self) -> float | None:
        return _average_optional(score.fact_coverage for score in self.scores)

    def citation_grounding_score(self) -> float | None:
        return _average_optional(score.citation_grounding for score in self.scores)

    def source_locator_grounding_score(self) -> float | None:
        return _average_optional(score.source_locator_grounding for score in self.scores)

    def retrieval_context_coverage_score(self) -> float | None:
        return _average_optional(score.retrieval_context_coverage for score in self.scores)

    def citation_gold_coverage_score(self) -> float | None:
        return _average_optional(score.citation_gold_coverage for score in self.scores)

    def strict_retrieval_context_coverage_score(self) -> float | None:
        return _average_optional(score.strict_retrieval_context_coverage for score in self.scores)

    def equivalent_retrieval_context_coverage_score(self) -> float | None:
        return _average_optional(score.equivalent_retrieval_context_coverage for score in self.scores)

    def strict_citation_gold_coverage_score(self) -> float | None:
        return _average_optional(score.strict_citation_gold_coverage for score in self.scores)

    def equivalent_citation_gold_coverage_score(self) -> float | None:
        return _average_optional(score.equivalent_citation_gold_coverage for score in self.scores)

    def equivalent_supported_rate(self) -> float:
        return sum(1 for score in self.scores if score.equivalent_gold_supported) / self.total if self.total else 0.0

    def claim_support_coverage_score(self) -> float | None:
        return _average_optional(score.claim_support_coverage for score in self.scores)

    def true_missing_gold_rate(self) -> float:
        return (
            sum(1 for score in self.scores if "true_missing_gold_in_retrieval" in score.diagnostic_tags) / self.total
            if self.total
            else 0.0
        )

    def diagnostic_tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for score in self.scores:
            for tag in score.diagnostic_tags:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    def abstention_accuracy(self) -> float | None:
        return _average_optional(score.abstention_correct for score in self.scores)

    def success_rate(self) -> float:
        return sum(1 for score in self.scores if score.answer_success) / self.total if self.total else 0.0

    def outcome_counts(self) -> dict[str, int]:
        counts = {outcome.value: 0 for outcome in AnswerOutcome}
        for score in self.scores:
            counts[score.sample.outcome.value] += 1
        return {key: value for key, value in counts.items() if value}

    def system_failure_rate(self) -> float | None:
        if not self.total:
            return None
        return sum(score.sample.outcome.is_system_failure for score in self.scores) / self.total

    def over_abstention_rate(self) -> float | None:
        eligible = [score for score in self.scores if score.sample.pair.expected_behavior == "answer" and not score.sample.outcome.is_system_failure]
        if not eligible:
            return None
        return sum(score.sample.outcome.is_refusal for score in eligible) / len(eligible)

    def failure_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for score in self.scores:
            reason = score.failure_reason
            if not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    def _metric_denominator(self, getter) -> int:
        return sum(getter(score) is not None for score in self.scores)

    def report(self) -> str:
        lines = [
            f"=== Evidence Answer Eval (n={self.total}) ===",
            f"  AnswerFactCoverage       = {_format_metric(self.answer_fact_coverage(), self._metric_denominator(lambda score: score.fact_coverage))}",
            f"  RetrievalContextCoverage = {_format_metric(self.retrieval_context_coverage_score(), self._metric_denominator(lambda score: score.retrieval_context_coverage))}",
            f"  CitationGroundingScore   = {_format_metric(self.citation_grounding_score(), self._metric_denominator(lambda score: score.citation_grounding))}",
            f"  CitationGoldCoverage     = {_format_metric(self.citation_gold_coverage_score(), self._metric_denominator(lambda score: score.citation_gold_coverage))}",
            f"  StrictContextCoverage    = {_format_metric(self.strict_retrieval_context_coverage_score(), self._metric_denominator(lambda score: score.strict_retrieval_context_coverage))}",
            f"  EquivalentContextCoverage = {_format_metric(self.equivalent_retrieval_context_coverage_score(), self._metric_denominator(lambda score: score.equivalent_retrieval_context_coverage))}",
            f"  StrictCitationCoverage   = {_format_metric(self.strict_citation_gold_coverage_score(), self._metric_denominator(lambda score: score.strict_citation_gold_coverage))}",
            f"  EquivalentCitationCoverage = {_format_metric(self.equivalent_citation_gold_coverage_score(), self._metric_denominator(lambda score: score.equivalent_citation_gold_coverage))}",
            f"  EquivalentSupportedRate  = {_format_metric(self.equivalent_supported_rate(), self.total)}",
            f"  ClaimSupportCoverage     = {_format_metric(self.claim_support_coverage_score(), self._metric_denominator(lambda score: score.claim_support_coverage))}",
            f"  TrueMissingGoldRate      = {_format_metric(self.true_missing_gold_rate(), self.total)}",
            f"  SourceLocatorGrounding   = {_format_metric(self.source_locator_grounding_score(), self._metric_denominator(lambda score: score.source_locator_grounding))}",
            f"  AbstentionAccuracy       = {_format_metric(self.abstention_accuracy(), self._metric_denominator(lambda score: score.abstention_correct))}",
            f"  AnswerSuccessRate        = {_format_metric(self.success_rate(), self.total, percent=True)}",
            f"  SystemFailureRate        = {_format_metric(self.system_failure_rate(), self.total)}",
            f"  OverAbstentionRate       = {_format_metric(self.over_abstention_rate(), sum(1 for score in self.scores if score.sample.pair.expected_behavior == 'answer' and not score.sample.outcome.is_system_failure))}",
            f"  OutcomeCounts            = {self.outcome_counts()}",
        ]
        if self.by_qa_type:
            lines.append("  -- by qa_type --")
            for qa_type in sorted(self.by_qa_type):
                sub = self.by_qa_type[qa_type]
                lines.append(
                    f"     {qa_type:<18} n={sub.total:<3} "
                    f"facts={_format_metric(sub.answer_fact_coverage(), sub._metric_denominator(lambda score: score.fact_coverage))} "
                    f"context={_format_metric(sub.retrieval_context_coverage_score(), sub._metric_denominator(lambda score: score.retrieval_context_coverage))} "
                    f"citations={_format_metric(sub.citation_grounding_score(), sub._metric_denominator(lambda score: score.citation_grounding))} "
                    f"success={_format_metric(sub.success_rate(), sub.total, percent=True)}"
                )
        failure_counts = self.failure_reason_counts()
        if failure_counts:
            lines.append("  -- failure reasons --")
            for reason, count in failure_counts.items():
                lines.append(f"     {reason:<32} n={count}")
        diagnostic_counts = self.diagnostic_tag_counts()
        if diagnostic_counts:
            lines.append("  -- diagnostic tags --")
            for tag, count in diagnostic_counts.items():
                lines.append(f"     {tag:<40} n={count}")
        return "\n".join(lines)


class EvidenceAnswerEvaluator:
    """Deterministic answer-level evaluation for evidence QA benchmarks."""

    def __init__(
        self,
        *,
        fact_match_threshold: float = 0.75,
        success_fact_threshold: float = 0.8,
        success_citation_threshold: float = 0.5,
    ) -> None:
        self._fact_match_threshold = fact_match_threshold
        self._success_fact_threshold = success_fact_threshold
        self._success_citation_threshold = success_citation_threshold

    def evaluate(self, samples: list[EvidenceAnswerSample]) -> EvidenceAnswerEvalResult:
        scores = [self.score(sample) for sample in samples]
        result = EvidenceAnswerEvalResult(scores=scores)
        by_type: dict[str, list[EvidenceAnswerScores]] = {}
        for score in scores:
            by_type.setdefault(score.qa_type, []).append(score)
        result.by_qa_type = {
            qa_type: EvidenceAnswerEvalResult(scores=type_scores)
            for qa_type, type_scores in by_type.items()
        }
        return result

    def score(self, sample: EvidenceAnswerSample) -> EvidenceAnswerScores:
        pair = sample.pair
        thresholds = _thresholds_for_pair(
            pair,
            fact_match_threshold=self._fact_match_threshold,
            success_fact_threshold=self._success_fact_threshold,
        )
        score = score_answer_case(
            _metric_case_from_sample(sample),
            fact_match_threshold=thresholds["fact_match_threshold"],
            success_fact_threshold=thresholds["success_fact_threshold"],
            success_citation_threshold=self._success_citation_threshold,
        )
        failure_reason = score.failure_reason
        claim_support_coverage = _claim_support_coverage(sample)
        diagnostic_tags = _diagnostic_tags(
            sample,
            strict_context_coverage=score.strict_retrieval_context_coverage,
            equivalent_context_coverage=score.equivalent_retrieval_context_coverage,
            equivalent_gold_supported=score.equivalent_gold_supported,
            claim_support_coverage=claim_support_coverage,
            failure_reason=failure_reason,
        )
        return EvidenceAnswerScores(
            sample=sample,
            fact_coverage=score.fact_coverage,
            citation_grounding=score.citation_grounding,
            source_locator_grounding=score.source_locator_grounding,
            abstention_correct=score.abstention_correct,
            answer_success=score.answer_success,
            retrieval_context_coverage=score.retrieval_context_coverage,
            citation_gold_coverage=score.citation_gold_coverage,
            strict_retrieval_context_coverage=score.strict_retrieval_context_coverage,
            equivalent_retrieval_context_coverage=score.equivalent_retrieval_context_coverage,
            strict_citation_gold_coverage=score.strict_citation_gold_coverage,
            equivalent_citation_gold_coverage=score.equivalent_citation_gold_coverage,
            equivalent_gold_supported=score.equivalent_gold_supported,
            claim_support_coverage=claim_support_coverage,
            diagnostic_tags=diagnostic_tags,
            failure_reason=failure_reason,
            matched_facts=score.matched_facts,
            missing_facts=score.missing_facts,
        )


def _metadata_ids(metadata: dict[str, Any], key: str) -> list[str]:
    raw = metadata.get(key)
    if not isinstance(raw, list):
        return []
    return _unique_texts(raw)


def _claim_support_coverage(sample: EvidenceAnswerSample) -> float | None:
    pair = sample.pair
    if str(pair.qa_type or "").casefold() != "citation_qa":
        return None
    gold_claim_ids = set(_unique_texts(pair.gold_claim_ids))
    if not gold_claim_ids:
        return None
    context_claim_ids = set(_claim_ids_from_metadata(sample.metadata))
    return 1.0 if gold_claim_ids.intersection(context_claim_ids) else 0.0


def _claim_ids_from_metadata(metadata: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in metadata.get("context_relationships") or []:
        if isinstance(item, dict):
            out.append(str(item.get("claim_id") or ""))
    for item in metadata.get("locator_context") or []:
        if isinstance(item, dict):
            out.append(str(item.get("claim_id") or ""))
    return _unique_texts(out)


def _diagnostic_tags(
    sample: EvidenceAnswerSample,
    *,
    strict_context_coverage: float | None,
    equivalent_context_coverage: float | None,
    equivalent_gold_supported: bool,
    claim_support_coverage: float | None,
    failure_reason: str,
) -> tuple[str, ...]:
    pair = sample.pair
    tags: list[str] = []
    if _retrieval_equivalent_support(pair, sample.metadata) != 1.0 and pair.gold_chunk_ids:
        tags.append("true_missing_gold_in_retrieval")
    if equivalent_gold_supported:
        tags.append("gold_id_missed_but_equivalent_supported")
    if _missing_any(sample.context_chunk_ids, _primary_evidence_ids(pair)):
        tags.append("context_missing_primary_evidence")
    if _missing_interpretation_context(pair, sample.context_chunk_ids):
        tags.append("context_missing_interpretation_evidence")
    if claim_support_coverage == 0.0:
        tags.append("claim_not_supported")
    if failure_reason == "fact_match_low":
        tags.append("fact_match_low")
    if (strict_context_coverage or 0.0) < 1.0 and (equivalent_context_coverage or 0.0) >= 1.0:
        tags.append("strict_context_missed_but_equivalent_supported")
    return tuple(_unique_texts(tags))


def _retrieval_equivalent_support(pair: EvidenceQAPair, metadata: dict[str, Any]) -> float | None:
    retrieved_ids = _metadata_ids(metadata, "retrieved_chunk_ids")
    if not retrieved_ids:
        return 0.0 if pair.gold_chunk_ids else None
    return evidence_support_coverage(
        retrieved_ids,
        pair.gold_chunk_ids,
        pair.equivalent_gold_chunk_ids or pair.gold_chunk_ids,
    )


def _primary_evidence_ids(pair: EvidenceQAPair) -> list[str]:
    group = dict(pair.supporting_evidence_group or {})
    return _unique_texts([
        *pair.required_primary_evidence_ids,
        *list(group.get("primary_evidence_ids") or []),
        *pair.gold_chunk_ids,
    ])


def _interpretation_context_ids(pair: EvidenceQAPair) -> list[str]:
    group = dict(pair.supporting_evidence_group or {})
    return _unique_texts([
        *pair.acceptable_support_evidence_ids,
        *list(group.get("interpretation_context_ids") or []),
    ])


def _missing_any(candidate_ids: list[str], required_ids: list[str]) -> bool:
    if not required_ids:
        return False
    candidate_set = set(_unique_texts(candidate_ids))
    return bool(set(required_ids) - candidate_set)


def _missing_interpretation_context(pair: EvidenceQAPair, context_chunk_ids: list[str]) -> bool:
    interpretation_ids = _interpretation_context_ids(pair)
    if not interpretation_ids:
        return False
    qa_type = str(pair.qa_type or "").casefold()
    if qa_type == "citation_qa":
        return False
    return not set(_unique_texts(context_chunk_ids)).intersection(interpretation_ids)


def _metric_case_from_sample(sample: EvidenceAnswerSample) -> AnswerMetricCase:
    pair = sample.pair
    return AnswerMetricCase(
        case_id=f"{pair.paper_id}:{pair.qa_type}:{pair.question}",
        question=pair.question,
        answer=sample.answer,
        outcome=sample.outcome,
        expected_facts=tuple(pair.answer_facts),
        cited_evidence_ids=tuple(sample.cited_chunk_ids),
        context_evidence_ids=tuple(sample.context_chunk_ids),
        gold_evidence_ids=tuple(pair.gold_chunk_ids),
        equivalent_gold_evidence_ids=tuple(pair.equivalent_gold_chunk_ids or pair.gold_chunk_ids),
        expected_abstain=pair.expected_behavior == "abstain",
        cited_source_locators=tuple(sample.cited_source_locators),
        gold_source_locators=tuple(pair.gold_source_locators),
        retrieved_evidence_ids=tuple(_metadata_ids(sample.metadata, "retrieved_chunk_ids")),
        metadata={
            "paper_id": pair.paper_id,
            "qa_type": pair.qa_type,
            **dict(sample.metadata),
        },
    )


def _thresholds_for_pair(
    pair: EvidenceQAPair,
    *,
    fact_match_threshold: float,
    success_fact_threshold: float,
) -> dict[str, float]:
    qa_type = str(pair.qa_type or "").casefold()
    if qa_type == "citation_qa":
        return {
            "fact_match_threshold": fact_match_threshold,
            "success_fact_threshold": 0.0,
        }
    if qa_type in {"formula_qa", "formula_explanation_qa"}:
        return {
            "fact_match_threshold": min(fact_match_threshold, 0.65),
            "success_fact_threshold": success_fact_threshold,
        }
    return {
        "fact_match_threshold": fact_match_threshold,
        "success_fact_threshold": success_fact_threshold,
    }


def _unique_texts(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _average_optional(values: Any) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return sum(usable) / len(usable) if usable else None


def _format_metric(value: float | None, denominator: int, *, percent: bool = False) -> str:
    if value is None:
        return f"N/A (n={denominator})"
    return f"{value:.1%} (n={denominator})" if percent else f"{value:.3f} (n={denominator})"


__all__ = [
    "EvidenceAnswerEvalResult",
    "EvidenceAnswerEvaluator",
    "EvidenceAnswerSample",
    "EvidenceAnswerScores",
]
