from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

from backend.research.rag.evaluation.paper_evidence_eval import EvidenceQAPair
from framework.rag.evaluation import AnswerMetricCase, evidence_support_coverage, score_answer_case
from framework.rag.evaluation.answer_metrics import looks_like_abstention


_ABSTAIN_MARKERS = (
    "cannot determine",
    "cannot answer",
    "does not contain",
    "does not discuss",
    "does not disclose",
    "does not report",
    "does not address",
    "do not contain",
    "do not report",
    "do not address",
    "insufficient to answer",
    "contains no",
    "contain no",
    "contains nothing about",
    "contain nothing about",
    "insufficient evidence",
    "not enough evidence",
    "not stated",
    "not discussed",
    "not mentioned",
    "does not mention",
    "does not include",
    "does not state",
    "do not mention",
    "do not discuss",
    "do not include",
    "do not state",
    "not in the provided context",
    "not available from the passages",
    "provided context does not mention",
    "context does not mention",
    "contains no mention",
    "contain nothing about",
    "contains nothing about",
    "no mention of",
    "no evidence",
    "无法确定",
    "无法回答",
    "没有足够证据",
    "证据不足",
    "未提到",
    "没有提到",
    "论文中没有",
)
OVERCONSERVATIVE_ABSTENTION_REASON = "abstained_over_conservative"
WRONG_ABSTENTION_REASON = "abstention_wrong"


def _looks_like_eval_abstention(answer: str) -> bool:
    text = " ".join(str(answer or "").casefold().split())
    if looks_like_abstention(text, abstain_markers=_ABSTAIN_MARKERS):
        return True
    has_context_anchor = any(
        anchor in text
        for anchor in ("provided context", "provided passage", "provided passages", "the passages")
    )
    return has_context_anchor and bool(re.search(r"\bnot\s+(?:a|an|the|any)\b", text))


def abstention_failure_reason(expected_behavior: str, answer: str) -> str:
    expected = str(expected_behavior or "answer").strip().casefold()
    abstained = _looks_like_eval_abstention(str(answer or ""))
    if expected == "answer" and abstained:
        return OVERCONSERVATIVE_ABSTENTION_REASON
    if expected == "abstain" and not abstained:
        return WRONG_ABSTENTION_REASON
    return ""


@dataclass
class EvidenceAnswerSample:
    pair: EvidenceQAPair
    answer: str
    cited_chunk_ids: list[str] = field(default_factory=list)
    cited_source_locators: list[str] = field(default_factory=list)
    context_chunk_ids: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


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

    def answer_fact_coverage(self) -> float:
        return _average_optional(score.fact_coverage for score in self.scores)

    def citation_grounding_score(self) -> float:
        return _average_optional(score.citation_grounding for score in self.scores)

    def source_locator_grounding_score(self) -> float:
        return _average_optional(score.source_locator_grounding for score in self.scores)

    def retrieval_context_coverage_score(self) -> float:
        return _average_optional(score.retrieval_context_coverage for score in self.scores)

    def citation_gold_coverage_score(self) -> float:
        return _average_optional(score.citation_gold_coverage for score in self.scores)

    def strict_retrieval_context_coverage_score(self) -> float:
        return _average_optional(score.strict_retrieval_context_coverage for score in self.scores)

    def equivalent_retrieval_context_coverage_score(self) -> float:
        return _average_optional(score.equivalent_retrieval_context_coverage for score in self.scores)

    def strict_citation_gold_coverage_score(self) -> float:
        return _average_optional(score.strict_citation_gold_coverage for score in self.scores)

    def equivalent_citation_gold_coverage_score(self) -> float:
        return _average_optional(score.equivalent_citation_gold_coverage for score in self.scores)

    def equivalent_supported_rate(self) -> float:
        return sum(1 for score in self.scores if score.equivalent_gold_supported) / self.total if self.total else 0.0

    def claim_support_coverage_score(self) -> float:
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

    def abstention_accuracy(self) -> float:
        return _average_optional(score.abstention_correct for score in self.scores)

    def success_rate(self) -> float:
        return sum(1 for score in self.scores if score.answer_success) / self.total if self.total else 0.0

    def failure_reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for score in self.scores:
            reason = score.failure_reason
            if not reason:
                continue
            counts[reason] = counts.get(reason, 0) + 1
        return dict(sorted(counts.items()))

    def report(self) -> str:
        lines = [
            f"=== Evidence Answer Eval (n={self.total}) ===",
            f"  AnswerFactCoverage       = {self.answer_fact_coverage():.3f}",
            f"  RetrievalContextCoverage = {self.retrieval_context_coverage_score():.3f}",
            f"  CitationGroundingScore   = {self.citation_grounding_score():.3f}",
            f"  CitationGoldCoverage     = {self.citation_gold_coverage_score():.3f}",
            f"  StrictContextCoverage    = {self.strict_retrieval_context_coverage_score():.3f}",
            f"  EquivalentContextCoverage = {self.equivalent_retrieval_context_coverage_score():.3f}",
            f"  StrictCitationCoverage   = {self.strict_citation_gold_coverage_score():.3f}",
            f"  EquivalentCitationCoverage = {self.equivalent_citation_gold_coverage_score():.3f}",
            f"  EquivalentSupportedRate  = {self.equivalent_supported_rate():.3f}",
            f"  ClaimSupportCoverage     = {self.claim_support_coverage_score():.3f}",
            f"  TrueMissingGoldRate      = {self.true_missing_gold_rate():.3f}",
            f"  SourceLocatorGrounding   = {self.source_locator_grounding_score():.3f}",
            f"  AbstentionAccuracy       = {self.abstention_accuracy():.3f}",
            f"  AnswerSuccessRate        = {self.success_rate():.1%}",
        ]
        if self.by_qa_type:
            lines.append("  -- by qa_type --")
            for qa_type in sorted(self.by_qa_type):
                sub = self.by_qa_type[qa_type]
                lines.append(
                    f"     {qa_type:<18} n={sub.total:<3} "
                    f"facts={sub.answer_fact_coverage():.3f} "
                    f"context={sub.retrieval_context_coverage_score():.3f} "
                    f"citations={sub.citation_grounding_score():.3f} "
                    f"success={sub.success_rate():.1%}"
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
            abstain_markers=_ABSTAIN_MARKERS,
        )
        failure_reason = abstention_failure_reason(pair.expected_behavior, sample.answer) or score.failure_reason
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


def _average_optional(values: Any) -> float:
    usable = [float(value) for value in values if value is not None]
    return sum(usable) / len(usable) if usable else 0.0


__all__ = [
    "EvidenceAnswerEvalResult",
    "EvidenceAnswerEvaluator",
    "EvidenceAnswerSample",
    "EvidenceAnswerScores",
]
