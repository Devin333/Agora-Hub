"""Deterministic answer, citation, and abstention scoring.

The module only emits independently verifiable observations. It never invokes a
semantic judge and never substitutes lexical similarity for correctness or
faithfulness. Ambiguous semantics become needs_review for the separate judge
workflow.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    EvaluationTrack,
    ExecutionProfile,
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import (
    EvaluationCase, EvidenceLocation, ReferenceLabel,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EvaluationScore,
    MetricAggregation,
    MetricDefinition,
    MetricDirection,
    MetricGranularity,
    MetricRegistry,
    MetricValueKind,
    MissingValuePolicy,
    NumericDomain,
    ScoreSource,
    ScoreStatus,
)
from framework.harness.rag.models import (
    AnswerClaim,
    EvidenceCandidate,
    GroundedAnswerCandidate,
    RAGContextPack,
)

ANSWER_METRIC_VERSION = "1.0"
CITATION_BINDING_RULE_VERSION = "citation-binding/v1"
DETERMINISTIC_SEMANTIC_RULE_VERSION = "deterministic-semantic/v1"
DEFAULT_ABSTENTION_RUBRIC_VERSION = "abstention/v1"

AnswerMetricId = Literal[
    "answer.fact_precision",
    "answer.fact_recall",
    "answer.fact_f1",
    "answer.faithfulness",
    "answer.citation_support",
    "answer.citation_coverage",
    "answer.abstention_recall",
    "answer.false_abstention_rate",
]
ContentModality = Literal["text", "table", "figure", "equation"]


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_exact_text(value: str) -> str:
    return " ".join(value.split())


def _unique(values: Sequence[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")


class NumericUnit(StrEnum):
    FRACTION = "fraction"
    PERCENT = "percent"
    PERCENTAGE_POINT = "percentage_point"
    RELATIVE_PERCENT = "relative_percent"
    SCALAR = "scalar"


class SemanticDecision(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    NEEDS_REVIEW = "needs_review"


class CitationParseState(StrEnum):
    PARSED = "parsed"
    INVALID = "invalid"
    NEEDS_REVIEW = "needs_review"


class AbstentionClassification(StrEnum):
    ABSTAINED = "abstained"
    ANSWERED = "answered"
    EMPTY_OUTPUT = "empty_output"
    RUNTIME_ERROR = "runtime_error"
    NEEDS_REVIEW = "needs_review"


class ReviewedClass(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEEDS_REVIEW = "needs_review"


class SemanticCondition(ProtocolModel):
    name: NonEmptyStr
    value: NonEmptyStr


class SemanticFact(ProtocolModel):
    """Immutable deterministic fact projection, not a replacement QA record."""

    exact_quote: NonEmptyStr | None = None
    numeric_value: float | None = None
    unit: NumericUnit | None = None
    tolerance: float = Field(default=0.0, ge=0.0)
    object_id: NonEmptyStr | None = None
    dataset_id: NonEmptyStr | None = None
    split_id: NonEmptyStr | None = None
    statistic: NonEmptyStr | None = None
    conditions: tuple[SemanticCondition, ...] = ()
    modality: ContentModality = "text"

    @field_validator("numeric_value", "tolerance", mode="before")
    @classmethod
    def _finite_number(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("semantic numeric fields must be finite numbers")
        if not math.isfinite(float(value)):
            raise ValueError("semantic numeric fields must be finite numbers")
        return float(value)

    @field_validator("unit", mode="before")
    @classmethod
    def _parse_unit(cls, value: Any) -> Any:
        return NumericUnit(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _complete_fact(self) -> "SemanticFact":
        if self.exact_quote is None and self.numeric_value is None:
            raise ValueError("semantic fact requires an exact quote or numeric value")
        if (self.numeric_value is None) != (self.unit is None):
            raise ValueError("numeric_value and unit must be supplied together")
        _unique([item.name for item in self.conditions], "semantic condition names")
        return self


class ExactSourceExcerpt(ProtocolModel):
    """Exact source text bound to actual context and reproducible identity."""

    context_pack_id: NonEmptyStr
    evidence_id: NonEmptyStr
    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    source_locator: NonEmptyStr
    location: EvidenceLocation | None = None
    content_type: ContentModality
    source_text: NonEmptyStr
    source_text_sha256: Sha256Digest
    quote_start: int = Field(ge=0, strict=True)
    quote_end: int = Field(gt=0, strict=True)
    quote_text: NonEmptyStr
    quote_sha256: Sha256Digest

    @model_validator(mode="after")
    def _verify_exact_text(self) -> "ExactSourceExcerpt":
        if self.quote_end <= self.quote_start or self.quote_end > len(self.source_text):
            raise ValueError("invalid exact quote range")
        if self.source_text[self.quote_start : self.quote_end] != self.quote_text:
            raise ValueError("quote_text must equal the exact source_text slice")
        if self.source_text_sha256 != _text_sha256(self.source_text):
            raise ValueError("source_text_sha256 does not match source_text")
        if self.quote_sha256 != _text_sha256(self.quote_text):
            raise ValueError("quote_sha256 does not match quote_text")
        return self

    @property
    def evidence_reference(self) -> str:
        locator = self.location.model_dump(mode="json") if self.location else self.source_locator
        return (
            f"source://{self.paper_id}/{self.document_version}/{self.source_sha256}/"
            f"{content_digest(locator)}#quote={self.quote_sha256}"
        )


class ReferenceFactProjection(ProtocolModel):
    reference_claim_id: NonEmptyStr
    fact: SemanticFact
    evidence_anchor_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr

    @model_validator(mode="after")
    def _unique_anchors(self) -> "ReferenceFactProjection":
        _unique(self.evidence_anchor_ids, "evidence_anchor_ids")
        return self


class CandidateFactProjection(ProtocolModel):
    answer_claim_id: NonEmptyStr
    reference_claim_id: NonEmptyStr | None = None
    fact: SemanticFact
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr


class EvidenceFactProjection(ProtocolModel):
    evidence_id: NonEmptyStr
    fact: SemanticFact
    source: ExactSourceExcerpt
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr

    @model_validator(mode="after")
    def _source_matches_evidence(self) -> "EvidenceFactProjection":
        if self.source.evidence_id != self.evidence_id:
            raise ValueError("evidence fact and exact source must use the same evidence_id")
        if self.fact.exact_quote is not None and (
            _normalized_exact_text(self.fact.exact_quote)
            != _normalized_exact_text(self.source.quote_text)
        ):
            raise ValueError("exact-quote fact must equal its verified source quote")
        return self


class ClaimAnswerSpan(ProtocolModel):
    claim_id: NonEmptyStr
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    text: NonEmptyStr

    @model_validator(mode="after")
    def _valid_range(self) -> "ClaimAnswerSpan":
        if self.end <= self.start:
            raise ValueError("claim span end must exceed start")
        return self


class CitationBinding(ProtocolModel):
    claim_id: NonEmptyStr
    answer_span: ClaimAnswerSpan | None
    cited_evidence_ids: tuple[NonEmptyStr, ...]
    sources: tuple[ExactSourceExcerpt, ...]
    state: CitationParseState
    reason: NonEmptyStr | None
    rule_version: Literal[CITATION_BINDING_RULE_VERSION] = CITATION_BINDING_RULE_VERSION

    @model_validator(mode="after")
    def _state_contract(self) -> "CitationBinding":
        if self.state is CitationParseState.PARSED:
            if self.answer_span is None or self.reason is not None:
                raise ValueError("parsed binding requires a span and no reason")
        elif self.reason is None:
            raise ValueError("non-parsed binding requires a reason")
        return self


class AnswerMetricObservation(ProtocolModel):
    metric_id: AnswerMetricId
    version: Literal[ANSWER_METRIC_VERSION] = ANSWER_METRIC_VERSION
    value: float | None
    status: ScoreStatus
    numerator: int | None
    denominator: int | None
    reason: NonEmptyStr | None
    provenance: dict[NonEmptyStr, Any]
    evidence: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("status", mode="before")
    @classmethod
    def _parse_status(cls, value: Any) -> Any:
        return ScoreStatus(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _status_contract(self) -> "AnswerMetricObservation":
        if not self.provenance:
            raise ValueError("answer metric provenance must not be empty")
        _unique(self.evidence, "answer metric evidence")
        if (self.numerator is None) != (self.denominator is None):
            raise ValueError("metric counts must both be null or both be present")
        if self.numerator is not None:
            if self.numerator < 0 or self.denominator is None or self.denominator < 0:
                raise ValueError("metric counts must be non-negative")
            if self.numerator > self.denominator:
                raise ValueError("metric numerator must not exceed denominator")
        if self.status is ScoreStatus.OK:
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("ok observations require a finite value")
        elif self.value is not None or self.reason is None:
            raise ValueError("non-ok observations require value=null and a reason")
        if self.denominator == 0 and self.status is not ScoreStatus.NOT_APPLICABLE:
            raise ValueError("zero denominator is only valid for not_applicable")
        return self


class AnswerScoreIdentity(ProtocolModel):
    """Caller-supplied identity; this scorer never invents run provenance."""

    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    attempt_id: NonEmptyStr
    track: Literal[EvaluationTrack.ORACLE_GENERATION, EvaluationTrack.END_TO_END]
    execution_profile: ExecutionProfile
    variant_fingerprint: Sha256Digest
    repetition: int = Field(ge=1, strict=True)
    scope: NonEmptyStr
    scoring_revision: ImmutableVersionStr

    @field_validator("track", mode="before")
    @classmethod
    def _parse_track(cls, value: Any) -> Any:
        return EvaluationTrack(value) if isinstance(value, str) else value

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _parse_profile(cls, value: Any) -> Any:
        return ExecutionProfile(value) if isinstance(value, str) else value


class AbstentionRubric(ProtocolModel):
    version: ImmutableVersionStr = DEFAULT_ABSTENTION_RUBRIC_VERSION
    full_response_patterns: tuple[NonEmptyStr, ...] = (
        r"(?i)I (?:cannot|can't) answer (?:this|the question) based on the provided (?:context|evidence)\.?",
        r"(?i)The provided (?:context|evidence) is insufficient to answer (?:this|the question)\.?",
        r"(?i)There is insufficient (?:context|evidence) to answer (?:this|the question)\.?",
    )
    allow_text_only_abstention: bool = True

    @model_validator(mode="after")
    def _valid_patterns(self) -> "AbstentionRubric":
        for pattern in self.full_response_patterns:
            re.compile(pattern)
        return self

    def matches_complete_refusal(self, text: str) -> bool:
        normalized = " ".join(text.split())
        return any(re.fullmatch(pattern, normalized) for pattern in self.full_response_patterns)

    def starts_with_refusal_clause(self, text: str) -> bool:
        normalized = " ".join(text.split())
        return any(re.match(pattern, normalized) for pattern in self.full_response_patterns)


class AbstentionCaseObservation(ProtocolModel):
    case_id: NonEmptyStr
    reviewed_class: ReviewedClass
    classification: AbstentionClassification
    abstained: bool | None
    correct_for_negative: bool | None
    false_for_positive: bool | None
    runtime_failure: bool
    reason: NonEmptyStr
    rubric_version: ImmutableVersionStr


class AbstentionAggregateResult(ProtocolModel):
    planned: int = Field(ge=0, strict=True)
    negative_applicable: int = Field(ge=0, strict=True)
    positive_applicable: int = Field(ge=0, strict=True)
    pending_answerability: int = Field(ge=0, strict=True)
    negative_known_success: int = Field(ge=0, strict=True)
    negative_known_failure: int = Field(ge=0, strict=True)
    negative_unknown: int = Field(ge=0, strict=True)
    positive_false_abstention: int = Field(ge=0, strict=True)
    positive_non_abstention: int = Field(ge=0, strict=True)
    positive_unknown: int = Field(ge=0, strict=True)
    runtime_failures: int = Field(ge=0, strict=True)
    observations: tuple[AnswerMetricObservation, ...]

    @model_validator(mode="after")
    def _counts_complete(self) -> "AbstentionAggregateResult":
        if self.negative_applicable != self.negative_known_success + self.negative_known_failure + self.negative_unknown:
            raise ValueError("negative counts must equal the applicable denominator")
        if self.positive_applicable != self.positive_false_abstention + self.positive_non_abstention + self.positive_unknown:
            raise ValueError("positive counts must equal the applicable denominator")
        if self.planned != self.negative_applicable + self.positive_applicable + self.pending_answerability:
            raise ValueError("all planned abstention cases must remain accounted for")
        return self

    def to_evaluation_scores(self, identity: "AnswerScoreIdentity") -> tuple[EvaluationScore, ...]:
        return _observations_to_scores(self.observations, identity)


class AnswerScoreResult(ProtocolModel):
    case_id: NonEmptyStr
    candidate_answer_id: NonEmptyStr | None
    context_pack_id: NonEmptyStr | None
    citation_bindings: tuple[CitationBinding, ...]
    abstention: AbstentionCaseObservation
    observations: tuple[AnswerMetricObservation, ...]

    @property
    def fact_precision(self) -> AnswerMetricObservation:
        return self.observation("answer.fact_precision")

    @property
    def fact_completeness(self) -> AnswerMetricObservation:
        return self.observation("answer.fact_recall")

    @property
    def faithfulness(self) -> AnswerMetricObservation:
        return self.observation("answer.faithfulness")

    @property
    def citation_support(self) -> AnswerMetricObservation:
        return self.observation("answer.citation_support")

    @property
    def citation_coverage(self) -> AnswerMetricObservation:
        return self.observation("answer.citation_coverage")

    def observation(self, metric_id: AnswerMetricId) -> AnswerMetricObservation:
        return next(item for item in self.observations if item.metric_id == metric_id)

    def to_evaluation_scores(self, identity: AnswerScoreIdentity) -> tuple[EvaluationScore, ...]:
        if identity.case_id != self.case_id:
            raise ValueError("score identity case_id must match the scored case")
        return _observations_to_scores(self.observations, identity)


def parse_citation_bindings(
    *,
    candidate: GroundedAnswerCandidate,
    actual_context: RAGContextPack,
    evidence_facts: Sequence[EvidenceFactProjection],
) -> tuple[CitationBinding, ...]:
    """Bind native claims to exact answer spans and actual cited sources."""

    sources_by_id: dict[str, ExactSourceExcerpt] = {}
    for projection in evidence_facts:
        previous = sources_by_id.setdefault(projection.evidence_id, projection.source)
        if previous != projection.source:
            raise ValueError("one evidence_id cannot resolve to different exact source excerpts")
    context_by_id = {item.evidence_id: item for item in actual_context.accepted_evidence}
    top_level = set(candidate.cited_evidence_ids)
    overrides = _claim_span_overrides(candidate)
    bindings: list[CitationBinding] = []
    seen: set[str] = set()
    for claim in candidate.claims:
        if claim.claim_id in seen:
            bindings.append(CitationBinding(
                claim_id=claim.claim_id,
                answer_span=None,
                cited_evidence_ids=claim.evidence_ids,
                sources=(),
                state=CitationParseState.INVALID,
                reason="duplicate stable claim_id",
            ))
            continue
        seen.add(claim.claim_id)
        span, span_reason = _resolve_claim_span(candidate.answer_text, claim, overrides)
        if span is None:
            bindings.append(CitationBinding(
                claim_id=claim.claim_id,
                answer_span=None,
                cited_evidence_ids=claim.evidence_ids,
                sources=(),
                state=CitationParseState.NEEDS_REVIEW,
                reason=span_reason,
            ))
            continue
        resolved: list[ExactSourceExcerpt] = []
        invalid_reason: str | None = None
        review_reason: str | None = None
        for evidence_id in claim.evidence_ids:
            if evidence_id not in top_level:
                invalid_reason = "claim citation is absent from candidate.cited_evidence_ids"
                break
            actual = context_by_id.get(evidence_id)
            if actual is None:
                invalid_reason = "claim citation does not resolve in actual accepted context"
                break
            source = sources_by_id.get(evidence_id)
            if source is None:
                review_reason = "citation lacks a verified exact source excerpt"
                break
            if source.context_pack_id != actual_context.pack_id:
                invalid_reason = "citation source belongs to a different context pack"
                break
            review_reason = _source_identity_mismatch(actual, source)
            if review_reason:
                break
            resolved.append(source)
        state = CitationParseState.PARSED
        reason = None
        if invalid_reason:
            state, reason = CitationParseState.INVALID, invalid_reason
        elif review_reason:
            state, reason = CitationParseState.NEEDS_REVIEW, review_reason
        bindings.append(CitationBinding(
            claim_id=claim.claim_id,
            answer_span=span,
            cited_evidence_ids=claim.evidence_ids,
            sources=tuple(resolved),
            state=state,
            reason=reason,
        ))
    return tuple(bindings)


def classify_abstention(
    *,
    case_id: str,
    reference: ReferenceLabel,
    candidate: GroundedAnswerCandidate | None,
    runtime_error: str | None = None,
    rubric: AbstentionRubric = AbstentionRubric(),
) -> AbstentionCaseObservation:
    """Classify refusal behavior without inferring the frozen answerability label."""

    reviewed_class = _reviewed_class(reference)
    if runtime_error is not None or candidate is None:
        classification = AbstentionClassification.RUNTIME_ERROR
        abstained: bool | None = False
        reason = f"runtime model failure: {runtime_error or 'no candidate output'}"
    else:
        text = candidate.answer_text.strip()
        if not text:
            classification, abstained = AbstentionClassification.EMPTY_OUTPUT, False
            reason = "empty output is not a protocol-confirmed refusal"
        else:
            full_refusal = rubric.matches_complete_refusal(text)
            refusal_clause = rubric.starts_with_refusal_clause(text)
            has_claims = bool(candidate.claims)
            if has_claims and (candidate.abstained or refusal_clause):
                classification, abstained = AbstentionClassification.NEEDS_REVIEW, None
                reason = "refusal signal is mixed with factual claims or a partial answer"
            elif candidate.abstained:
                if not full_refusal:
                    classification, abstained = AbstentionClassification.NEEDS_REVIEW, None
                    reason = "explicit abstention text does not satisfy the versioned full-response rubric"
                else:
                    classification, abstained = AbstentionClassification.ABSTAINED, True
                    reason = "native abstained signal and complete refusal satisfy the rubric"
            elif full_refusal and rubric.allow_text_only_abstention and not has_claims:
                classification, abstained = AbstentionClassification.ABSTAINED, True
                reason = "complete refusal satisfies the versioned text-only rubric"
            elif has_claims:
                classification, abstained = AbstentionClassification.ANSWERED, False
                reason = "candidate contains factual claims without a complete refusal"
            else:
                classification, abstained = AbstentionClassification.NEEDS_REVIEW, None
                reason = "non-empty claimless natural language is not classified from keywords alone"
    correct_for_negative: bool | None = None
    false_for_positive: bool | None = None
    if reviewed_class is ReviewedClass.NEGATIVE:
        correct_for_negative = (
            True if classification is AbstentionClassification.ABSTAINED
            else None if classification is AbstentionClassification.NEEDS_REVIEW
            else False
        )
    elif reviewed_class is ReviewedClass.POSITIVE:
        false_for_positive = (
            True if classification is AbstentionClassification.ABSTAINED
            else None if classification is AbstentionClassification.NEEDS_REVIEW
            else False
        )
    return AbstentionCaseObservation(
        case_id=case_id,
        reviewed_class=reviewed_class,
        classification=classification,
        abstained=abstained,
        correct_for_negative=correct_for_negative,
        false_for_positive=false_for_positive,
        runtime_failure=classification is AbstentionClassification.RUNTIME_ERROR,
        reason=reason,
        rubric_version=rubric.version,
    )


def classify_case_abstention(
    *,
    case: EvaluationCase,
    candidate: GroundedAnswerCandidate | None,
    runtime_error: str | None = None,
    rubric: AbstentionRubric = AbstentionRubric(),
) -> AbstentionCaseObservation:
    """Classify one native frozen case without constructing a parallel QA model."""

    case.verify_digest()
    return classify_abstention(
        case_id=case.case_id,
        reference=case.reference_projection(),
        candidate=candidate,
        runtime_error=runtime_error,
        rubric=rubric,
    )


def aggregate_planned_abstention(
    *,
    cases: Sequence[EvaluationCase],
    candidates: Mapping[str, GroundedAnswerCandidate],
    runtime_errors: Mapping[str, str] | None = None,
    rubric: AbstentionRubric = AbstentionRubric(),
) -> AbstentionAggregateResult:
    """Keep every planned native case, including failures and unresolved labels."""

    case_ids = [case.case_id for case in cases]
    _unique(case_ids, "planned abstention case_id values")
    runtime_error_map = runtime_errors or {}
    unknown_candidates = set(candidates) - set(case_ids)
    unknown_errors = set(runtime_error_map) - set(case_ids)
    if unknown_candidates or unknown_errors:
        raise ValueError("candidate/runtime maps contain case IDs outside the frozen plan")
    observations = tuple(
        classify_case_abstention(
            case=case,
            candidate=candidates.get(case.case_id),
            runtime_error=runtime_error_map.get(case.case_id),
            rubric=rubric,
        )
        for case in cases
    )
    return aggregate_abstention_metrics(observations)


def aggregate_abstention_metrics(
    observations: Sequence[AbstentionCaseObservation],
) -> AbstentionAggregateResult:
    """Aggregate every planned case without shrinking reviewed denominators."""

    negative = [item for item in observations if item.reviewed_class is ReviewedClass.NEGATIVE]
    positive = [item for item in observations if item.reviewed_class is ReviewedClass.POSITIVE]
    pending = [item for item in observations if item.reviewed_class is ReviewedClass.NEEDS_REVIEW]
    neg_success = sum(item.correct_for_negative is True for item in negative)
    neg_failure = sum(item.correct_for_negative is False for item in negative)
    neg_unknown = sum(item.correct_for_negative is None for item in negative)
    pos_false = sum(item.false_for_positive is True for item in positive)
    pos_non = sum(item.false_for_positive is False for item in positive)
    pos_unknown = sum(item.false_for_positive is None for item in positive)
    evidence = tuple(f"abstention-case://{item.case_id}" for item in observations) or (
        "abstention-plan://empty",
    )
    rubrics = sorted({item.rubric_version for item in observations})
    neg_metric = _aggregate_ratio_observation(
        metric_id="answer.abstention_recall",
        event_count=neg_success,
        non_event_count=neg_failure,
        unknown_count=neg_unknown,
        provenance={
            "reviewed_class": "negative",
            "rubric_versions": rubrics,
            "runtime_failures": sum(item.runtime_failure for item in negative),
            "planned_denominator": len(negative),
        },
        evidence=evidence,
        zero_reason="no reviewed negative cases are applicable",
        unknown_reason="negative abstention semantics remain unresolved",
    )
    pos_metric = _aggregate_ratio_observation(
        metric_id="answer.false_abstention_rate",
        event_count=pos_false,
        non_event_count=pos_non,
        unknown_count=pos_unknown,
        provenance={
            "reviewed_class": "positive",
            "rubric_versions": rubrics,
            "runtime_failures": sum(item.runtime_failure for item in positive),
            "planned_denominator": len(positive),
        },
        evidence=evidence,
        zero_reason="no reviewed positive cases are applicable",
        unknown_reason="positive abstention semantics remain unresolved",
    )
    return AbstentionAggregateResult(
        planned=len(observations),
        negative_applicable=len(negative),
        positive_applicable=len(positive),
        pending_answerability=len(pending),
        negative_known_success=neg_success,
        negative_known_failure=neg_failure,
        negative_unknown=neg_unknown,
        positive_false_abstention=pos_false,
        positive_non_abstention=pos_non,
        positive_unknown=pos_unknown,
        runtime_failures=sum(item.runtime_failure for item in observations),
        observations=(neg_metric, pos_metric),
    )


_CASE_METRIC_BOUNDARIES: tuple[tuple[AnswerMetricId, str], ...] = (
    ("answer.fact_precision", "reference_facts+answer_claims"),
    ("answer.fact_recall", "reference_facts+answer_claims"),
    ("answer.fact_f1", "reference_facts+answer_claims"),
    ("answer.faithfulness", "actual_context+answer_claims"),
    ("answer.citation_support", "exact_cited_source+bound_answer_claim"),
    ("answer.citation_coverage", "answer_claims+citation_bindings"),
)


def score_answer(
    *,
    case_id: str,
    reference: ReferenceLabel,
    candidate: GroundedAnswerCandidate | None,
    actual_context: RAGContextPack | None,
    reference_facts: Sequence[ReferenceFactProjection] = (),
    candidate_facts: Sequence[CandidateFactProjection] = (),
    evidence_facts: Sequence[EvidenceFactProjection] = (),
    runtime_error: str | None = None,
    rubric: AbstentionRubric = AbstentionRubric(),
) -> AnswerScoreResult:
    """Produce independent deterministic observations with isolated inputs."""

    abstention = classify_abstention(
        case_id=case_id,
        reference=reference,
        candidate=candidate,
        runtime_error=runtime_error,
        rubric=rubric,
    )
    answer_ref = f"answer://{candidate.answer_id}" if candidate else f"answer://{case_id}/missing"
    reference_ref = f"reference://{case_id}/{content_digest(reference)}"
    base_evidence = (answer_ref, reference_ref)
    if runtime_error is not None or candidate is None:
        observations = tuple(_state_observation(
            metric_id=metric_id,
            status=ScoreStatus.ERROR,
            reason=f"runtime model failure: {runtime_error or 'no candidate output'}",
            provenance={"input_boundary": boundary, "failure_kind": "runtime_model_failure"},
            evidence=base_evidence,
        ) for metric_id, boundary in _CASE_METRIC_BOUNDARIES)
        return AnswerScoreResult(
            case_id=case_id,
            candidate_answer_id=None,
            context_pack_id=actual_context.pack_id if actual_context else None,
            citation_bindings=(),
            abstention=abstention,
            observations=observations,
        )
    bindings = () if actual_context is None else parse_citation_bindings(
        candidate=candidate,
        actual_context=actual_context,
        evidence_facts=evidence_facts,
    )
    reviewed_class = _reviewed_class(reference)
    if reference.answerability == "needs_review" or reviewed_class is ReviewedClass.NEEDS_REVIEW:
        observations = tuple(_state_observation(
            metric_id=metric_id,
            status=ScoreStatus.NEEDS_REVIEW,
            reason="answerability or human review is not frozen and approved",
            provenance={"input_boundary": boundary},
            evidence=base_evidence,
        ) for metric_id, boundary in _CASE_METRIC_BOUNDARIES)
    elif reference.answerability == "unanswerable":
        observations = tuple(_na_observation(
            metric_id=metric_id,
            reason="answer-content metric has no verifiable claim denominator on a reviewed negative case",
            provenance={"input_boundary": boundary},
            evidence=base_evidence,
        ) for metric_id, boundary in _CASE_METRIC_BOUNDARIES)
    else:
        observations = _score_answerable_case(
            reference=reference,
            candidate=candidate,
            actual_context=actual_context,
            reference_facts=reference_facts,
            candidate_facts=candidate_facts,
            evidence_facts=evidence_facts,
            bindings=bindings,
            base_evidence=base_evidence,
        )
    return AnswerScoreResult(
        case_id=case_id,
        candidate_answer_id=candidate.answer_id,
        context_pack_id=actual_context.pack_id if actual_context else None,
        citation_bindings=bindings,
        abstention=abstention,
        observations=observations,
    )


score_answer_candidate = score_answer


def _score_answerable_case(
    *,
    reference: ReferenceLabel,
    candidate: GroundedAnswerCandidate,
    actual_context: RAGContextPack | None,
    reference_facts: Sequence[ReferenceFactProjection],
    candidate_facts: Sequence[CandidateFactProjection],
    evidence_facts: Sequence[EvidenceFactProjection],
    bindings: Sequence[CitationBinding],
    base_evidence: tuple[str, ...],
) -> tuple[AnswerMetricObservation, ...]:
    claims = tuple(candidate.claims)
    claim_ids = {item.claim_id for item in claims}
    if len(claim_ids) != len(claims):
        raise ValueError("candidate claim_id values must be unique")
    reference_by_id = _index_reference_facts(reference_facts, reference)
    candidate_by_id = _index_candidate_facts(candidate_facts, claims)
    evidence_by_id = _index_evidence_facts(evidence_facts, actual_context)
    precision, recall, f1 = _score_facts(
        reference=reference,
        claims=claims,
        reference_by_id=reference_by_id,
        candidate_by_id=candidate_by_id,
        evidence=base_evidence,
    )
    faithfulness = _score_faithfulness(
        claims=claims,
        actual_context=actual_context,
        candidate_by_id=candidate_by_id,
        evidence_by_id=evidence_by_id,
        evidence=(base_evidence[0],),
    )
    support = _score_citation_support(
        claims=claims,
        candidate_by_id=candidate_by_id,
        evidence_by_id=evidence_by_id,
        bindings=bindings,
        evidence=(base_evidence[0],),
    )
    coverage = _score_citation_coverage(
        claims=claims,
        bindings=bindings,
        candidate_by_id=candidate_by_id,
        evidence_by_id=evidence_by_id,
        evidence=(base_evidence[0],),
    )
    return precision, recall, f1, faithfulness, support, coverage


def _score_facts(
    *,
    reference: ReferenceLabel,
    claims: Sequence[AnswerClaim],
    reference_by_id: Mapping[str, ReferenceFactProjection],
    candidate_by_id: Mapping[str, CandidateFactProjection],
    evidence: tuple[str, ...],
) -> tuple[AnswerMetricObservation, AnswerMetricObservation, AnswerMetricObservation]:
    if not claims:
        precision = _na_observation(
            metric_id="answer.fact_precision",
            reason="answer has no verifiable claims; precision cannot be vacuously one",
            provenance={"input_boundary": "reference_facts+answer_claims"},
            evidence=evidence,
        )
        recall = _ok_ratio(
            metric_id="answer.fact_recall",
            numerator=0,
            denominator=len(reference.reference_claims),
            provenance={
                "input_boundary": "reference_facts+answer_claims",
                "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
            },
            evidence=evidence,
        )
        f1 = _na_observation(
            metric_id="answer.fact_f1",
            reason="fact F1 is undefined when claim precision is not applicable",
            provenance={"input_boundary": "reference_facts+answer_claims"},
            evidence=evidence,
        )
        return precision, recall, f1

    decisions: list[SemanticDecision] = []
    matched: set[str] = set()
    for claim in claims:
        projection = candidate_by_id.get(claim.claim_id)
        if projection is None:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        if projection.reference_claim_id is not None:
            target = reference_by_id.get(projection.reference_claim_id)
            targets = (target,) if target is not None else ()
        else:
            targets = tuple(reference_by_id.values())
        if not targets:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        target_decisions = [
            _compare_semantic_facts(projection.fact, item.fact)[0] for item in targets
        ]
        supported = [
            index for index, item in enumerate(target_decisions)
            if item is SemanticDecision.SUPPORTED
        ]
        if len(supported) == 1:
            matched.add(targets[supported[0]].reference_claim_id)
            decisions.append(SemanticDecision.SUPPORTED)
        elif len(supported) > 1 or any(
            item is SemanticDecision.NEEDS_REVIEW for item in target_decisions
        ):
            decisions.append(SemanticDecision.NEEDS_REVIEW)
        elif len(targets) == 1:
            decisions.append(SemanticDecision.UNSUPPORTED)
        else:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
    precision = _decisions_ratio(
        metric_id="answer.fact_precision",
        decisions=decisions,
        unknown_reason="answer claims require semantic judge or human review",
        provenance={
            "input_boundary": "reference_facts+answer_claims",
            "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        },
        evidence=evidence,
    )

    unknown_reference = {
        item.claim_id for item in reference.reference_claims
    } - set(reference_by_id)
    for claim_id, expected in reference_by_id.items():
        if claim_id in matched:
            continue
        for actual in candidate_by_id.values():
            if actual.reference_claim_id not in (None, claim_id):
                continue
            if _compare_semantic_facts(actual.fact, expected.fact)[0] is SemanticDecision.NEEDS_REVIEW:
                unknown_reference.add(claim_id)
    if unknown_reference:
        recall = _review_observation(
            metric_id="answer.fact_recall",
            reason="reference claim completeness requires semantic review",
            provenance={
                "matched_reference_claims": len(matched),
                "planned_reference_claims": len(reference.reference_claims),
                "unknown_reference_claim_ids": sorted(unknown_reference),
                "input_boundary": "reference_facts+answer_claims",
            },
            evidence=evidence,
        )
    else:
        recall = _ok_ratio(
            metric_id="answer.fact_recall",
            numerator=len(matched),
            denominator=len(reference.reference_claims),
            provenance={
                "input_boundary": "reference_facts+answer_claims",
                "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
            },
            evidence=evidence,
        )
    if precision.status is ScoreStatus.OK and recall.status is ScoreStatus.OK:
        assert precision.value is not None and recall.value is not None
        value = 0.0 if precision.value + recall.value == 0 else (
            2.0 * precision.value * recall.value / (precision.value + recall.value)
        )
        f1 = _ok_scalar(
            metric_id="answer.fact_f1",
            value=value,
            provenance={
                "input_boundary": "reference_facts+answer_claims",
                "derived_from": ["answer.fact_precision", "answer.fact_recall"],
                "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
            },
            evidence=evidence,
        )
    else:
        f1 = _review_observation(
            metric_id="answer.fact_f1",
            reason="fact F1 remains unknown until precision and completeness are both valid",
            provenance={
                "precision_status": precision.status.value,
                "recall_status": recall.status.value,
                "input_boundary": "reference_facts+answer_claims",
            },
            evidence=evidence,
        )
    return precision, recall, f1


def _score_faithfulness(
    *,
    claims: Sequence[AnswerClaim],
    actual_context: RAGContextPack | None,
    candidate_by_id: Mapping[str, CandidateFactProjection],
    evidence_by_id: Mapping[str, tuple[EvidenceFactProjection, ...]],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    if not claims:
        return _na_observation(
            metric_id="answer.faithfulness",
            reason="answer has no verifiable claims; faithfulness cannot be vacuously one",
            provenance={"input_boundary": "actual_context+answer_claims"},
            evidence=evidence,
        )
    if actual_context is None:
        return _review_observation(
            metric_id="answer.faithfulness",
            reason="actual model context was not supplied",
            provenance={"input_boundary": "actual_context+answer_claims"},
            evidence=evidence,
        )
    if not actual_context.accepted_evidence:
        return _ok_ratio(
            metric_id="answer.faithfulness",
            numerator=0,
            denominator=len(claims),
            provenance={
                "input_boundary": "actual_context+answer_claims",
                "context_pack_id": actual_context.pack_id,
                "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
            },
            evidence=(*evidence, f"context://{actual_context.pack_id}"),
        )
    context_facts = tuple(item for values in evidence_by_id.values() for item in values)
    decisions = []
    for claim in claims:
        projection = candidate_by_id.get(claim.claim_id)
        decisions.append(
            SemanticDecision.NEEDS_REVIEW
            if projection is None or not context_facts
            else _best_semantic_decision(projection.fact, context_facts)
        )
    return _decisions_ratio(
        metric_id="answer.faithfulness",
        decisions=decisions,
        unknown_reason="actual-context support requires semantic judge or human review",
        provenance={
            "input_boundary": "actual_context+answer_claims",
            "context_pack_id": actual_context.pack_id,
            "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        },
        evidence=(*evidence, f"context://{actual_context.pack_id}"),
    )


def _score_citation_support(
    *,
    claims: Sequence[AnswerClaim],
    candidate_by_id: Mapping[str, CandidateFactProjection],
    evidence_by_id: Mapping[str, tuple[EvidenceFactProjection, ...]],
    bindings: Sequence[CitationBinding],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    cited = [claim for claim in claims if claim.evidence_ids]
    if not cited:
        return _na_observation(
            metric_id="answer.citation_support",
            reason="no claim-bound citations exist; citation support cannot be vacuously one",
            provenance={"input_boundary": "exact_cited_source+bound_answer_claim"},
            evidence=evidence,
        )
    binding_by_id = {item.claim_id: item for item in bindings}
    decisions: list[SemanticDecision] = []
    refs: list[str] = []
    for claim in cited:
        binding = binding_by_id.get(claim.claim_id)
        if binding is None or binding.state is CitationParseState.INVALID:
            decisions.append(SemanticDecision.UNSUPPORTED)
            continue
        if binding.state is CitationParseState.NEEDS_REVIEW:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        projection = candidate_by_id.get(claim.claim_id)
        facts = tuple(
            item
            for evidence_id in claim.evidence_ids
            for item in evidence_by_id.get(evidence_id, ())
        )
        if projection is None or not facts:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        decisions.append(_best_semantic_decision(projection.fact, facts))
        refs.extend(source.evidence_reference for source in binding.sources)
    return _decisions_ratio(
        metric_id="answer.citation_support",
        decisions=decisions,
        unknown_reason="citation semantics or exact source identity requires review",
        provenance={
            "input_boundary": "exact_cited_source+bound_answer_claim",
            "citation_binding_rule_version": CITATION_BINDING_RULE_VERSION,
            "semantic_rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        },
        evidence=tuple(dict.fromkeys((*evidence, *refs))),
    )


def _score_citation_coverage(
    *,
    claims: Sequence[AnswerClaim],
    bindings: Sequence[CitationBinding],
    candidate_by_id: Mapping[str, CandidateFactProjection],
    evidence_by_id: Mapping[str, tuple[EvidenceFactProjection, ...]],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    if not claims:
        return _na_observation(
            metric_id="answer.citation_coverage",
            reason="answer has no factual claim denominator; coverage cannot be vacuously one",
            provenance={"input_boundary": "answer_claims+citation_bindings"},
            evidence=evidence,
        )
    binding_by_id = {item.claim_id: item for item in bindings}
    decisions: list[SemanticDecision] = []
    for claim in claims:
        # Missing citations are an observed zero, independent of whether fact
        # correctness or context support is known.
        if not claim.evidence_ids:
            decisions.append(SemanticDecision.UNSUPPORTED)
            continue
        binding = binding_by_id.get(claim.claim_id)
        if binding is None or binding.state is CitationParseState.NEEDS_REVIEW:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        if binding.state is CitationParseState.INVALID:
            decisions.append(SemanticDecision.UNSUPPORTED)
            continue
        projection = candidate_by_id.get(claim.claim_id)
        facts = tuple(
            item for evidence_id in claim.evidence_ids
            for item in evidence_by_id.get(evidence_id, ())
        )
        if projection is None or not facts:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        decisions.append(_best_semantic_decision(projection.fact, facts))
    return _decisions_ratio(
        metric_id="answer.citation_coverage",
        decisions=decisions,
        unknown_reason="sufficient citation coverage requires exact-source semantic review",
        provenance={
            "input_boundary": "answer_claims+exact_citation_bindings",
            "rule_version": CITATION_BINDING_RULE_VERSION,
            "semantic_rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        },
        evidence=evidence,
    )


def _best_semantic_decision(
    candidate: SemanticFact,
    evidence_facts: Sequence[EvidenceFactProjection],
) -> SemanticDecision:
    decisions = [_compare_semantic_facts(candidate, item.fact)[0] for item in evidence_facts]
    if any(item is SemanticDecision.SUPPORTED for item in decisions):
        return SemanticDecision.SUPPORTED
    if decisions and all(item is SemanticDecision.UNSUPPORTED for item in decisions):
        return SemanticDecision.UNSUPPORTED
    return SemanticDecision.NEEDS_REVIEW


def _compare_semantic_facts(
    actual: SemanticFact,
    expected: SemanticFact,
) -> tuple[SemanticDecision, str]:
    if expected.modality != actual.modality and "figure" in {expected.modality, actual.modality}:
        return SemanticDecision.NEEDS_REVIEW, "text/table evidence cannot establish full figure scope"
    if expected.modality != actual.modality:
        return SemanticDecision.UNSUPPORTED, "evidence modality differs from explicit claim scope"
    for field_name in ("object_id", "dataset_id", "split_id", "statistic"):
        expected_value = getattr(expected, field_name)
        actual_value = getattr(actual, field_name)
        if expected_value is None:
            if actual_value is not None:
                return SemanticDecision.NEEDS_REVIEW, f"extra {field_name} changes semantic scope"
            continue
        if actual_value is None:
            return SemanticDecision.NEEDS_REVIEW, f"missing explicit {field_name}"
        if actual_value.casefold() != expected_value.casefold():
            return SemanticDecision.UNSUPPORTED, f"explicit {field_name} differs"
    expected_conditions = {
        item.name.casefold(): item.value.casefold() for item in expected.conditions
    }
    actual_conditions = {
        item.name.casefold(): item.value.casefold() for item in actual.conditions
    }
    for name, value in expected_conditions.items():
        if name not in actual_conditions:
            return SemanticDecision.NEEDS_REVIEW, f"missing explicit experimental condition {name}"
        if actual_conditions[name] != value:
            return SemanticDecision.UNSUPPORTED, f"experimental condition {name} differs"
    if set(actual_conditions) - set(expected_conditions):
        return SemanticDecision.NEEDS_REVIEW, "additional experimental conditions change semantic scope"
    if expected.exact_quote is not None:
        if actual.exact_quote is None:
            return SemanticDecision.NEEDS_REVIEW, "exact-quote semantic input is absent"
        if _normalized_exact_text(actual.exact_quote) != _normalized_exact_text(expected.exact_quote):
            return SemanticDecision.NEEDS_REVIEW, "non-identical wording is not a deterministic semantic contradiction"
    if expected.numeric_value is not None:
        if any(getattr(expected, name) is None or getattr(actual, name) is None
               for name in ("object_id", "dataset_id", "split_id", "statistic")):
            return SemanticDecision.NEEDS_REVIEW, "numeric facts lack explicit object/dataset/split/statistic"
        if actual.numeric_value is None or actual.unit is None or expected.unit is None:
            return SemanticDecision.NEEDS_REVIEW, "numeric semantic input is incomplete"
        actual_dimension, actual_value = _normalize_numeric(actual.numeric_value, actual.unit)
        expected_dimension, expected_value = _normalize_numeric(expected.numeric_value, expected.unit)
        if actual_dimension != expected_dimension:
            return SemanticDecision.UNSUPPORTED, "percentage points, relative gain, and absolute values are distinct"
        # The observed answer cannot choose a larger acceptance tolerance.
        tolerance = expected.tolerance
        if not math.isclose(actual_value, expected_value, rel_tol=0.0, abs_tol=tolerance):
            return SemanticDecision.UNSUPPORTED, "normalized numeric value exceeds tolerance"
    return SemanticDecision.SUPPORTED, "all deterministic semantic dimensions match"


def _normalize_numeric(value: float, unit: NumericUnit) -> tuple[str, float]:
    if unit is NumericUnit.FRACTION:
        return "absolute_fraction", value
    if unit is NumericUnit.PERCENT:
        return "absolute_fraction", value / 100.0
    if unit is NumericUnit.PERCENTAGE_POINT:
        return "percentage_point", value
    if unit is NumericUnit.RELATIVE_PERCENT:
        return "relative_gain", value / 100.0
    return "scalar", value


def _reviewed_class(reference: ReferenceLabel) -> ReviewedClass:
    review = reference.review
    approved = (
        review.status == "approved"
        and review.reviewer_kind == "human"
        and review.reviewer_id is not None
        and review.reviewed_at is not None
        and review.source_completeness == "complete"
        and review.checked_scope is not None
        and review.evidence_reference is not None
    )
    if not approved or reference.answerability == "needs_review":
        return ReviewedClass.NEEDS_REVIEW
    return ReviewedClass.POSITIVE if reference.answerability == "answerable" else ReviewedClass.NEGATIVE


def _claim_span_overrides(candidate: GroundedAnswerCandidate) -> Mapping[str, Any]:
    raw = candidate.metadata.get("claim_spans", {})
    return raw if isinstance(raw, Mapping) else {}


def _resolve_claim_span(
    answer_text: str,
    claim: AnswerClaim,
    overrides: Mapping[str, Any],
) -> tuple[ClaimAnswerSpan | None, str]:
    raw = overrides.get(claim.claim_id)
    if isinstance(raw, Mapping):
        start, end = raw.get("start"), raw.get("end")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(answer_text)
            and answer_text[start:end] == claim.text
        ):
            return ClaimAnswerSpan(
                claim_id=claim.claim_id,
                start=start,
                end=end,
                text=claim.text,
            ), ""
        return None, "declared claim span does not exactly match answer text"
    positions: list[int] = []
    start = answer_text.find(claim.text)
    while start >= 0:
        positions.append(start)
        start = answer_text.find(claim.text, start + 1)
    if len(positions) != 1:
        return None, "claim text must occur exactly once or provide a verified claim span"
    start = positions[0]
    return ClaimAnswerSpan(
        claim_id=claim.claim_id,
        start=start,
        end=start + len(claim.text),
        text=claim.text,
    ), ""


def _source_identity_mismatch(
    evidence: EvidenceCandidate,
    source: ExactSourceExcerpt,
) -> str | None:
    metadata = evidence.metadata
    raw_chunk = metadata.get("paper_chunk")
    chunk: Mapping[str, Any] = raw_chunk if isinstance(raw_chunk, Mapping) else {}
    raw_chunk_metadata = chunk.get("metadata")
    chunk_metadata: Mapping[str, Any] = (
        raw_chunk_metadata if isinstance(raw_chunk_metadata, Mapping) else {}
    )

    def pick(name: str) -> str | None:
        value = metadata.get(name)
        if value in (None, ""):
            value = chunk.get(name)
        if value in (None, ""):
            value = chunk_metadata.get(name)
        return str(value) if value not in (None, "") else None

    expected = {
        "paper_id": pick("paper_id"),
        "document_version": pick("document_version"),
        "source_sha256": pick("source_sha256"),
        "source_locator": pick("source_locator") or evidence.source_ref,
    }
    actual = {
        "paper_id": source.paper_id,
        "document_version": source.document_version,
        "source_sha256": source.source_sha256,
        "source_locator": source.source_locator,
    }
    missing = [name for name, value in expected.items() if value is None]
    if missing:
        return f"actual context lacks immutable source identity fields: {', '.join(missing)}"
    mismatch = [name for name in expected if expected[name] != actual[name]]
    if mismatch:
        return f"exact citation source identity differs for: {', '.join(mismatch)}"
    # Only the exact text actually exposed to the model may support faithfulness.
    # A complete source document or arbitrary retrieved chunk cannot repair a
    # truncated or compressed accepted context.
    if source.quote_text not in evidence.summary:
        return "verified source quote is absent from actual model context text"
    return None


def _index_reference_facts(
    projections: Sequence[ReferenceFactProjection],
    reference: ReferenceLabel,
) -> dict[str, ReferenceFactProjection]:
    allowed_claim_ids = {item.claim_id for item in reference.reference_claims}
    anchors = {item.anchor_id: item for item in reference.evidence_anchors}
    result: dict[str, ReferenceFactProjection] = {}
    for projection in projections:
        if projection.reference_claim_id not in allowed_claim_ids:
            raise ValueError("reference fact projection targets an unknown reference claim")
        if projection.reference_claim_id in result:
            raise ValueError("reference claims may have only one deterministic projection")
        for anchor_id in projection.evidence_anchor_ids:
            anchor = anchors.get(anchor_id)
            if anchor is None or projection.reference_claim_id not in anchor.claim_ids:
                raise ValueError("reference fact projection must use native anchors for its claim")
        result[projection.reference_claim_id] = projection
    return result


def _index_candidate_facts(
    projections: Sequence[CandidateFactProjection],
    claims: Sequence[AnswerClaim],
) -> dict[str, CandidateFactProjection]:
    claim_by_id = {item.claim_id: item for item in claims}
    result: dict[str, CandidateFactProjection] = {}
    for projection in projections:
        claim = claim_by_id.get(projection.answer_claim_id)
        if claim is None:
            raise ValueError("candidate fact projection targets an unknown answer claim")
        if projection.answer_claim_id in result:
            raise ValueError("answer claims may have only one deterministic projection")
        if projection.fact.exact_quote is not None and (
            _normalized_exact_text(projection.fact.exact_quote)
            != _normalized_exact_text(claim.text)
        ):
            raise ValueError("exact-quote candidate projection must equal its native AnswerClaim text")
        result[projection.answer_claim_id] = projection
    return result


def _index_evidence_facts(
    projections: Sequence[EvidenceFactProjection],
    actual_context: RAGContextPack | None,
) -> dict[str, tuple[EvidenceFactProjection, ...]]:
    actual_by_id = (
        {item.evidence_id: item for item in actual_context.accepted_evidence}
        if actual_context is not None
        else {}
    )
    grouped: dict[str, list[EvidenceFactProjection]] = {}
    for projection in projections:
        if actual_context is None or projection.evidence_id not in actual_by_id:
            raise ValueError("evidence facts must belong to actual_context.accepted_evidence")
        if projection.source.context_pack_id != actual_context.pack_id:
            raise ValueError("evidence fact source belongs to a different actual context pack")
        if _source_identity_mismatch(actual_by_id[projection.evidence_id], projection.source):
            continue
        grouped.setdefault(projection.evidence_id, []).append(projection)
    return {key: tuple(value) for key, value in grouped.items()}


def _decisions_ratio(
    *,
    metric_id: AnswerMetricId,
    decisions: Sequence[SemanticDecision],
    unknown_reason: str,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    supported = sum(item is SemanticDecision.SUPPORTED for item in decisions)
    unsupported = sum(item is SemanticDecision.UNSUPPORTED for item in decisions)
    unknown = sum(item is SemanticDecision.NEEDS_REVIEW for item in decisions)
    if unknown:
        return _review_observation(
            metric_id=metric_id,
            reason=unknown_reason,
            provenance={
                **provenance,
                "known_supported": supported,
                "known_unsupported": unsupported,
                "unknown": unknown,
                "planned_denominator": len(decisions),
            },
            evidence=evidence,
        )
    return _ok_ratio(
        metric_id=metric_id,
        numerator=supported,
        denominator=len(decisions),
        provenance=provenance,
        evidence=evidence,
    )


def _aggregate_ratio_observation(
    *,
    metric_id: AnswerMetricId,
    event_count: int,
    non_event_count: int,
    unknown_count: int,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
    zero_reason: str,
    unknown_reason: str,
) -> AnswerMetricObservation:
    denominator = event_count + non_event_count + unknown_count
    if denominator == 0:
        return _na_observation(
            metric_id=metric_id,
            reason=zero_reason,
            provenance=provenance,
            evidence=evidence,
        )
    if unknown_count:
        return _review_observation(
            metric_id=metric_id,
            reason=unknown_reason,
            provenance={
                **provenance,
                "known_event": event_count,
                "known_non_event": non_event_count,
                "unknown": unknown_count,
            },
            evidence=evidence,
        )
    return _ok_ratio(
        metric_id=metric_id,
        numerator=event_count,
        denominator=denominator,
        provenance=provenance,
        evidence=evidence,
    )


def _ok_ratio(
    *,
    metric_id: AnswerMetricId,
    numerator: int,
    denominator: int,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    if denominator == 0:
        return _na_observation(
            metric_id=metric_id,
            reason="metric has no applicable denominator",
            provenance=provenance,
            evidence=evidence,
        )
    return AnswerMetricObservation(
        metric_id=metric_id,
        value=numerator / denominator,
        status=ScoreStatus.OK,
        numerator=numerator,
        denominator=denominator,
        reason=None,
        provenance=provenance,
        evidence=evidence,
    )


def _ok_scalar(
    *,
    metric_id: AnswerMetricId,
    value: float,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    return AnswerMetricObservation(
        metric_id=metric_id,
        value=value,
        status=ScoreStatus.OK,
        numerator=None,
        denominator=None,
        reason=None,
        provenance=provenance,
        evidence=evidence,
    )


def _state_observation(
    *,
    metric_id: AnswerMetricId,
    status: ScoreStatus,
    reason: str,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    return AnswerMetricObservation(
        metric_id=metric_id,
        value=None,
        status=status,
        numerator=None,
        denominator=None,
        reason=reason,
        provenance=provenance,
        evidence=evidence,
    )


def _review_observation(
    *,
    metric_id: AnswerMetricId,
    reason: str,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    return _state_observation(
        metric_id=metric_id,
        status=ScoreStatus.NEEDS_REVIEW,
        reason=reason,
        provenance=provenance,
        evidence=evidence,
    )


def _na_observation(
    *,
    metric_id: AnswerMetricId,
    reason: str,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    is_scalar = metric_id == "answer.fact_f1"
    return AnswerMetricObservation(
        metric_id=metric_id,
        value=None,
        status=ScoreStatus.NOT_APPLICABLE,
        numerator=None if is_scalar else 0,
        denominator=None if is_scalar else 0,
        reason=reason,
        provenance=provenance,
        evidence=evidence,
    )


def _observations_to_scores(
    observations: Sequence[AnswerMetricObservation],
    identity: AnswerScoreIdentity,
) -> tuple[EvaluationScore, ...]:
    scores: list[EvaluationScore] = []
    for observation in observations:
        provenance = {
            **observation.provenance,
            "answer_scoring_module": "protocol_answer_scoring",
        }
        scores.append(EvaluationScore(
            record_type="score",
            schema_version=SCHEMA_VERSION,
            protocol_version=PROTOCOL_VERSION,
            dataset_id=identity.dataset_id,
            dataset_version=identity.dataset_version,
            dataset_digest=identity.dataset_digest,
            run_id=identity.run_id,
            case_id=identity.case_id,
            attempt_id=identity.attempt_id,
            track=identity.track,
            execution_profile=identity.execution_profile,
            variant_fingerprint=identity.variant_fingerprint,
            repetition=identity.repetition,
            scope=identity.scope,
            scoring_revision=identity.scoring_revision,
            metric_id=observation.metric_id,
            version=observation.version,
            value=observation.value,
            status=observation.status,
            numerator=observation.numerator,
            denominator=observation.denominator,
            source=ScoreSource.RULE,
            provenance=provenance,
            provenance_digest=content_digest(provenance),
            evidence=observation.evidence,
            reason=observation.reason,
        ))
    return tuple(scores)


def answer_metric_definitions(
    *, scopes: frozenset[str] = frozenset({"full_task", "text_subset"}),
) -> tuple[MetricDefinition, ...]:
    tracks = frozenset({EvaluationTrack.ORACLE_GENERATION, EvaluationTrack.END_TO_END})
    profiles = frozenset({ExecutionProfile.FIXTURE, ExecutionProfile.PRODUCTION})
    domain = NumericDomain(minimum=0.0, maximum=1.0)

    def definition(
        metric_id: AnswerMetricId,
        *,
        required_inputs: frozenset[str],
        granularity: MetricGranularity = MetricGranularity.CASE,
        direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER,
        value_kind: MetricValueKind = MetricValueKind.RATIO,
        aggregation: MetricAggregation = MetricAggregation.RATIO,
    ) -> MetricDefinition:
        return MetricDefinition(
            metric_id=metric_id,
            version=ANSWER_METRIC_VERSION,
            required_inputs=required_inputs,
            applicable_tracks=tracks,
            applicable_execution_profiles=profiles,
            applicable_scopes=scopes,
            granularity=granularity,
            direction=direction,
            domain=domain,
            value_kind=value_kind,
            aggregation=aggregation,
            missing_policy=MissingValuePolicy.NEEDS_REVIEW,
        )

    return (
        definition("answer.fact_precision", required_inputs=frozenset({"answer_claims", "reference_claims"})),
        definition("answer.fact_recall", required_inputs=frozenset({"answer_claims", "reference_claims"})),
        definition(
            "answer.fact_f1",
            required_inputs=frozenset({"answer_claims", "reference_claims"}),
            value_kind=MetricValueKind.SCALAR,
            aggregation=MetricAggregation.MEAN,
        ),
        definition("answer.faithfulness", required_inputs=frozenset({"answer_claims", "actual_context"})),
        definition("answer.citation_support", required_inputs=frozenset({"answer_claims", "exact_cited_source"})),
        definition("answer.citation_coverage", required_inputs=frozenset({"answer_claims", "citation_bindings"})),
        definition(
            "answer.abstention_recall",
            required_inputs=frozenset({"reviewed_answerability", "abstention_classification"}),
            granularity=MetricGranularity.RUN,
        ),
        definition(
            "answer.false_abstention_rate",
            required_inputs=frozenset({"reviewed_answerability", "abstention_classification"}),
            granularity=MetricGranularity.RUN,
            direction=MetricDirection.LOWER_IS_BETTER,
        ),
    )


def build_answer_metric_registry(
    *, scopes: frozenset[str] = frozenset({"full_task", "text_subset"}),
) -> MetricRegistry:
    return MetricRegistry(answer_metric_definitions(scopes=scopes))


def register_answer_metrics(
    registry: MetricRegistry,
    *, scopes: frozenset[str] = frozenset({"full_task", "text_subset"}),
) -> None:
    for definition in answer_metric_definitions(scopes=scopes):
        registry.register(definition)


__all__ = [
    "ANSWER_METRIC_VERSION",
    "CITATION_BINDING_RULE_VERSION",
    "DEFAULT_ABSTENTION_RUBRIC_VERSION",
    "DETERMINISTIC_SEMANTIC_RULE_VERSION",
    "AbstentionAggregateResult",
    "AbstentionCaseObservation",
    "AbstentionClassification",
    "AbstentionRubric",
    "AnswerMetricObservation",
    "AnswerScoreIdentity",
    "AnswerScoreResult",
    "CandidateFactProjection",
    "CitationBinding",
    "CitationParseState",
    "EvidenceFactProjection",
    "ExactSourceExcerpt",
    "NumericUnit",
    "ReferenceFactProjection",
    "ReviewedClass",
    "SemanticCondition",
    "SemanticDecision",
    "SemanticFact",
    "aggregate_abstention_metrics",
    "aggregate_planned_abstention",
    "answer_metric_definitions",
    "build_answer_metric_registry",
    "classify_abstention",
    "classify_case_abstention",
    "parse_citation_bindings",
    "register_answer_metrics",
    "score_answer",
    "score_answer_candidate",
]
