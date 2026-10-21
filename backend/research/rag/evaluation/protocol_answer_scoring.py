"""Answer, citation, and abstention scoring.

Observations come from deterministic fact comparison first. The module never
invokes a semantic judge and never substitutes lexical similarity for
correctness or faithfulness: pairs it cannot decide stay pending and make their
metrics needs_review. A separate consensus workflow may resolve exactly those
pending pairs; observations they influence are attributed to the semantic judge
source.
"""
from __future__ import annotations

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
    AUTOMATED_REVIEW_POLICY,
    EvaluationCase,
    ReferenceLabel,
    ReviewAcceptancePolicy,
)
from backend.research.rag.evaluation.protocol_claim_assessment import (
    ClaimFactAssessment,
    ClaimVerdict,
    ComparisonTargetKind,
    PassageDecision,
    PendingJudgment,
    ResolvedDimension,
    SemanticDecision,
    SemanticResolution,
    TargetComparison,
    assess_claim,
    index_resolutions,
)
from backend.research.rag.evaluation.protocol_fact_projections import (
    CandidateFactProjection,
    EvidenceFactProjection,
    ExactSourceExcerpt,
    ReferenceFactProjection,
    evidence_source_identity,
    index_candidate_facts,
    index_evidence_facts,
    index_reference_facts,
    rule_key,
    source_identity_mismatch,
)
from backend.research.rag.evaluation.protocol_semantic_facts import (
    DETERMINISTIC_SEMANTIC_RULE_VERSION,
    EXPERIMENTAL_CONDITION_DIMENSIONS,
    DimensionOutcome,
    FactDimension,
    NumericUnit,
    SemanticCondition,
    SemanticFact,
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
    GroundedAnswerCandidate,
    RAGContextPack,
)

ANSWER_METRIC_VERSION = "2.0"
CITATION_BINDING_RULE_VERSION = "citation-binding/v1"
DEFAULT_ABSTENTION_RUBRIC_VERSION = "abstention/v1"

AnswerMetricId = Literal[
    "answer.fact_precision",
    "answer.fact_recall",
    "answer.fact_f1",
    "answer.contradicted_fact_rate",
    "answer.unsupported_inference_rate",
    "answer.unresolved_claim_rate",
    "answer.numeric_accuracy",
    "answer.unit_accuracy",
    "answer.condition_accuracy",
    "answer.faithfulness",
    "answer.citation_support",
    "answer.citation_coverage",
    "answer.abstention_recall",
    "answer.false_abstention_rate",
]


def _unique(values: Sequence[str], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")


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
    source: ScoreSource = ScoreSource.RULE

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
    review_policy_id: NonEmptyStr


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


class _ScoreResultBase(ProtocolModel):
    case_id: NonEmptyStr
    candidate_answer_id: NonEmptyStr | None
    context_pack_id: NonEmptyStr | None
    citation_bindings: tuple[CitationBinding, ...]
    observations: tuple[AnswerMetricObservation, ...]
    claim_assessments: tuple[ClaimFactAssessment, ...] = ()

    @property
    def fact_precision(self) -> AnswerMetricObservation:
        return self.observation("answer.fact_precision")

    @property
    def fact_completeness(self) -> AnswerMetricObservation:
        return self.observation("answer.fact_recall")

    @property
    def contradicted_fact_rate(self) -> AnswerMetricObservation:
        return self.observation("answer.contradicted_fact_rate")

    @property
    def unsupported_inference_rate(self) -> AnswerMetricObservation:
        return self.observation("answer.unsupported_inference_rate")

    @property
    def unresolved_claim_rate(self) -> AnswerMetricObservation:
        return self.observation("answer.unresolved_claim_rate")

    @property
    def numeric_accuracy(self) -> AnswerMetricObservation:
        return self.observation("answer.numeric_accuracy")

    @property
    def unit_accuracy(self) -> AnswerMetricObservation:
        return self.observation("answer.unit_accuracy")

    @property
    def condition_accuracy(self) -> AnswerMetricObservation:
        return self.observation("answer.condition_accuracy")

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

    def pending_judgments(self) -> tuple[PendingJudgment, ...]:
        """Every (claim, target) pair a semantic judge must resolve."""

        return tuple(item for assessment in self.claim_assessments for item in assessment.pending_judgments())

    def to_evaluation_scores(self, identity: AnswerScoreIdentity) -> tuple[EvaluationScore, ...]:
        if identity.case_id != self.case_id:
            raise ValueError("score identity case_id must match the scored case")
        return _observations_to_scores(self.observations, identity)


class AnswerScoreResult(_ScoreResultBase):
    """Scores of one answer against a reviewed reference."""

    abstention: AbstentionCaseObservation


class GroundedScoreResult(_ScoreResultBase):
    """Reference-free scores: only grounding in the material actually given."""

    context_kind: NonEmptyStr


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
            review_reason = source_identity_mismatch(actual, source)
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
    review_policy: ReviewAcceptancePolicy = AUTOMATED_REVIEW_POLICY,
) -> AbstentionCaseObservation:
    """Classify refusal behavior without inferring the frozen answerability label."""

    reviewed_class = _reviewed_class(reference, review_policy)
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
        review_policy_id=_policy_key(review_policy),
    )


def classify_case_abstention(
    *,
    case: EvaluationCase,
    candidate: GroundedAnswerCandidate | None,
    runtime_error: str | None = None,
    rubric: AbstentionRubric = AbstentionRubric(),
    review_policy: ReviewAcceptancePolicy = AUTOMATED_REVIEW_POLICY,
) -> AbstentionCaseObservation:
    """Classify one native frozen case without constructing a parallel QA model."""

    case.verify_digest()
    return classify_abstention(
        case_id=case.case_id,
        reference=case.reference_projection(),
        candidate=candidate,
        runtime_error=runtime_error,
        rubric=rubric,
        review_policy=review_policy,
    )


def aggregate_planned_abstention(
    *,
    cases: Sequence[EvaluationCase],
    candidates: Mapping[str, GroundedAnswerCandidate],
    runtime_errors: Mapping[str, str] | None = None,
    rubric: AbstentionRubric = AbstentionRubric(),
    review_policy: ReviewAcceptancePolicy = AUTOMATED_REVIEW_POLICY,
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
        review_policy=review_policy,
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
    policies = sorted({item.review_policy_id for item in observations})
    neg_metric = _aggregate_ratio_observation(
        metric_id="answer.abstention_recall",
        event_count=neg_success,
        non_event_count=neg_failure,
        unknown_count=neg_unknown,
        provenance={
            "reviewed_class": "negative",
            "rubric_versions": rubrics,
            "review_policies": policies,
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
            "review_policies": policies,
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
    ("answer.contradicted_fact_rate", "reference_facts+actual_context+answer_claims"),
    ("answer.unsupported_inference_rate", "reference_facts+actual_context+answer_claims"),
    ("answer.unresolved_claim_rate", "reference_facts+actual_context+answer_claims"),
    ("answer.numeric_accuracy", "aligned_reference_facts+answer_claims"),
    ("answer.unit_accuracy", "aligned_reference_facts+answer_claims"),
    ("answer.condition_accuracy", "aligned_reference_facts+answer_claims"),
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
    review_policy: ReviewAcceptancePolicy = AUTOMATED_REVIEW_POLICY,
    resolutions: Sequence[SemanticResolution] = (),
) -> AnswerScoreResult:
    """Score one answer from deterministic comparisons plus attributed resolutions.

    ``resolutions`` may only decide pairs the deterministic rules left pending
    (see ``AnswerScoreResult.pending_judgments``); any other resolution is a
    contract error. Observations influenced by a resolution are attributed to
    the semantic judge source.
    """

    abstention = classify_abstention(
        case_id=case_id,
        reference=reference,
        candidate=candidate,
        runtime_error=runtime_error,
        rubric=rubric,
        review_policy=review_policy,
    )
    answer_ref = f"answer://{candidate.answer_id}" if candidate else f"answer://{case_id}/missing"
    reference_ref = f"reference://{case_id}/{content_digest(reference)}"
    base_evidence = (answer_ref, reference_ref)
    resolution_index = index_resolutions(resolutions)
    if runtime_error is not None or candidate is None:
        if resolution_index:
            raise ValueError("resolutions cannot apply to a runtime failure")
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
    reviewed_class = _reviewed_class(reference, review_policy)
    assessments: tuple[ClaimFactAssessment, ...] = ()
    answerable = not (
        reference.answerability != "answerable" or reviewed_class is ReviewedClass.NEEDS_REVIEW
    )
    if resolution_index and not answerable:
        raise ValueError("resolutions only apply to accepted answerable cases")
    if reference.answerability == "needs_review" or reviewed_class is ReviewedClass.NEEDS_REVIEW:
        observations = tuple(_state_observation(
            metric_id=metric_id,
            status=ScoreStatus.NEEDS_REVIEW,
            reason=(
                "answerability is not frozen" if reference.answerability == "needs_review"
                else review_policy.rejection_reason(reference.review)
                or "reference review is not accepted"
            ),
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
        observations, assessments = _score_answerable_case(
            reference=reference,
            candidate=candidate,
            actual_context=actual_context,
            reference_facts=reference_facts,
            candidate_facts=candidate_facts,
            evidence_facts=evidence_facts,
            bindings=bindings,
            base_evidence=base_evidence,
            resolutions=resolution_index,
        )
    observations = _with_provenance(observations, _review_provenance(reference, review_policy))
    return AnswerScoreResult(
        case_id=case_id,
        candidate_answer_id=candidate.answer_id,
        context_pack_id=actual_context.pack_id if actual_context else None,
        citation_bindings=bindings,
        abstention=abstention,
        observations=observations,
        claim_assessments=assessments,
    )


score_answer_candidate = score_answer

_REFERENCE_ONLY_METRICS: frozenset[str] = frozenset({
    "answer.fact_precision",
    "answer.fact_recall",
    "answer.fact_f1",
    "answer.numeric_accuracy",
    "answer.unit_accuracy",
    "answer.condition_accuracy",
})


def score_grounded_answer(
    *,
    case_id: str,
    candidate: GroundedAnswerCandidate,
    actual_context: RAGContextPack,
    context_kind: str,
    candidate_facts: Sequence[CandidateFactProjection] = (),
    evidence_facts: Sequence[EvidenceFactProjection] = (),
    resolutions: Sequence[SemanticResolution] = (),
) -> GroundedScoreResult:
    """Score an answer that has no reference: grounding, contradiction, citations.

    ``context_kind`` names what the context is (for example ``model_context`` or
    ``cited_quotes``) so a report can never present quote-level grounding as
    support by the full sources. Reference-only metrics are not applicable.
    """

    resolution_index = index_resolutions(resolutions)
    if any(kind is ComparisonTargetKind.REFERENCE for _, kind, _ in resolution_index):
        raise ValueError("reference resolutions cannot apply without a reference")
    claims = tuple(candidate.claims)
    if len({item.claim_id for item in claims}) != len(claims):
        raise ValueError("candidate claim_id values must be unique")
    candidate_by_id: dict[str, CandidateFactProjection] = {}
    claim_ids = {item.claim_id for item in claims}
    for projection in candidate_facts:
        if projection.answer_claim_id not in claim_ids or projection.answer_claim_id in candidate_by_id:
            raise ValueError("candidate facts must map one-to-one onto known answer claims")
        candidate_by_id[projection.answer_claim_id] = projection
    evidence_by_id = index_evidence_facts(evidence_facts, actual_context)
    assessments = tuple(
        assess_claim(
            claim,
            projection=candidate_by_id.get(claim.claim_id),
            reference_by_id={},
            reference_claim_ids=(),
            actual_context=actual_context,
            evidence_by_id=evidence_by_id,
            resolutions=resolution_index,
            reference_available=False,
        )
        for claim in claims
    )
    applied = [item for assessment in assessments for item in assessment.resolutions]
    unused = set(resolution_index) - {item.key for item in applied}
    if unused:
        raise ValueError("resolutions target pairs that are not pending")
    resolvers = sorted({item.resolver for item in applied})
    bindings = parse_citation_bindings(
        candidate=candidate, actual_context=actual_context, evidence_facts=evidence_facts,
    )
    evidence = (f"answer://{candidate.answer_id}", f"context://{actual_context.pack_id}")
    provenance = {
        "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        "context_kind": context_kind,
        "reference": "absent",
        "candidate_projection_rules": sorted({
            rule_key(item.rule_id, item.rule_version) for item in candidate_by_id.values()
        }),
        "evidence_projection_rules": sorted({
            rule_key(item.rule_id, item.rule_version)
            for values in evidence_by_id.values() for item in values
        }),
        "evidence_resolutions": len(applied),
        "evidence_resolvers": resolvers,
    }
    contradicted, over_inference, unresolved = _score_claim_verdicts(
        assessments=assessments, provenance=provenance, evidence=evidence,
    )
    faithfulness = _score_faithfulness(
        assessments=assessments, actual_context=actual_context, provenance=provenance,
        evidence=(evidence[0],),
    )
    support = _score_citation_support(
        claims=claims, assessments=assessments, bindings=bindings, provenance=provenance, evidence=evidence,
    )
    coverage = _score_citation_coverage(
        claims=claims, assessments=assessments, bindings=bindings, provenance=provenance, evidence=evidence,
    )
    measured = {
        item.metric_id: item
        for item in _attributed(
            (contradicted, over_inference, unresolved, faithfulness, support, coverage), bool(resolvers),
        )
    }
    observations = tuple(
        measured.get(metric_id) or _na_observation(
            metric_id=metric_id,
            reason="metric requires a reference answer, which this case does not have",
            provenance={"input_boundary": boundary, "reference": "absent", "context_kind": context_kind},
            evidence=evidence,
        )
        for metric_id, boundary in _CASE_METRIC_BOUNDARIES
    )
    assert all(
        item.status is ScoreStatus.NOT_APPLICABLE
        for item in observations if item.metric_id in _REFERENCE_ONLY_METRICS
    )
    return GroundedScoreResult(
        case_id=case_id,
        candidate_answer_id=candidate.answer_id,
        context_pack_id=actual_context.pack_id,
        citation_bindings=bindings,
        observations=observations,
        claim_assessments=assessments,
        context_kind=context_kind,
    )


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
    resolutions: Mapping[tuple[str, ComparisonTargetKind, str], SemanticResolution],
) -> tuple[tuple[AnswerMetricObservation, ...], tuple[ClaimFactAssessment, ...]]:
    claims = tuple(candidate.claims)
    claim_ids = {item.claim_id for item in claims}
    if len(claim_ids) != len(claims):
        raise ValueError("candidate claim_id values must be unique")
    reference_by_id = index_reference_facts(reference_facts, reference)
    candidate_by_id = index_candidate_facts(candidate_facts, claims, reference)
    evidence_by_id = index_evidence_facts(evidence_facts, actual_context)
    reference_claim_ids = tuple(item.claim_id for item in reference.reference_claims)
    assessments = tuple(
        assess_claim(
            claim,
            projection=candidate_by_id.get(claim.claim_id),
            reference_by_id=reference_by_id,
            reference_claim_ids=reference_claim_ids,
            actual_context=actual_context,
            evidence_by_id=evidence_by_id,
            resolutions=resolutions,
        )
        for claim in claims
    )
    applied = [item for assessment in assessments for item in assessment.resolutions]
    unused = set(resolutions) - {item.key for item in applied}
    if unused:
        raise ValueError(
            "resolutions target pairs that are not pending: "
            + ", ".join(f"{claim}/{kind.value}/{target}" for claim, kind, target in sorted(unused))
        )
    reference_resolvers = sorted({
        item.resolver for item in applied if item.target_kind is ComparisonTargetKind.REFERENCE
    })
    evidence_resolvers = sorted({
        item.resolver for item in applied if item.target_kind is ComparisonTargetKind.EVIDENCE
    })
    fact_provenance = {
        "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        "reference_projection_rules": sorted({
            rule_key(item.rule_id, item.rule_version) for item in reference_by_id.values()
        }),
        "candidate_projection_rules": sorted({
            rule_key(item.rule_id, item.rule_version) for item in candidate_by_id.values()
        }),
        "reference_resolutions": sum(
            item.target_kind is ComparisonTargetKind.REFERENCE for item in applied
        ),
        "reference_resolvers": reference_resolvers,
    }
    context_provenance = {
        "rule_version": DETERMINISTIC_SEMANTIC_RULE_VERSION,
        "candidate_projection_rules": fact_provenance["candidate_projection_rules"],
        "evidence_projection_rules": sorted({
            rule_key(item.rule_id, item.rule_version)
            for values in evidence_by_id.values() for item in values
        }),
        "evidence_resolutions": sum(
            item.target_kind is ComparisonTargetKind.EVIDENCE for item in applied
        ),
        "evidence_resolvers": evidence_resolvers,
    }
    precision, recall, f1 = _score_fact_correctness(
        reference=reference,
        assessments=assessments,
        provenance=fact_provenance,
        evidence=base_evidence,
    )
    contradicted, over_inference, unresolved = _score_claim_verdicts(
        assessments=assessments,
        provenance={**fact_provenance, **context_provenance},
        evidence=base_evidence,
    )
    numeric, unit, condition = _score_dimension_accuracy(
        assessments=assessments,
        provenance=fact_provenance,
        evidence=base_evidence,
    )
    faithfulness = _score_faithfulness(
        assessments=assessments,
        actual_context=actual_context,
        provenance=context_provenance,
        evidence=(base_evidence[0],),
    )
    support = _score_citation_support(
        claims=claims,
        assessments=assessments,
        bindings=bindings,
        provenance=context_provenance,
        evidence=(base_evidence[0],),
    )
    coverage = _score_citation_coverage(
        claims=claims,
        assessments=assessments,
        bindings=bindings,
        provenance=context_provenance,
        evidence=(base_evidence[0],),
    )
    judged_reference = bool(reference_resolvers)
    judged_context = bool(evidence_resolvers)
    observations = (
        *_attributed((precision, recall, f1, numeric, unit, condition), judged_reference),
        *_attributed((contradicted, over_inference, unresolved), judged_reference or judged_context),
        *_attributed((faithfulness, support, coverage), judged_context),
    )
    return observations, assessments


def _score_fact_correctness(
    *,
    reference: ReferenceLabel,
    assessments: Sequence[ClaimFactAssessment],
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> tuple[AnswerMetricObservation, AnswerMetricObservation, AnswerMetricObservation]:
    boundary = {"input_boundary": "reference_facts+answer_claims", **provenance}
    if not assessments:
        precision = _na_observation(
            metric_id="answer.fact_precision",
            reason="answer has no verifiable claims; precision cannot be vacuously one",
            provenance=boundary,
            evidence=evidence,
        )
        recall = _ok_ratio(
            metric_id="answer.fact_recall",
            numerator=0,
            denominator=len(reference.reference_claims),
            provenance=boundary,
            evidence=evidence,
        )
        f1 = _na_observation(
            metric_id="answer.fact_f1",
            reason="fact F1 is undefined when claim precision is not applicable",
            provenance=boundary,
            evidence=evidence,
        )
        return precision, recall, f1

    precision = _decisions_ratio(
        metric_id="answer.fact_precision",
        decisions=[item.reference_decision for item in assessments],
        unknown_reason="answer claims require semantic judge resolution against the reference",
        provenance=boundary,
        evidence=evidence,
    )

    # A reference claim is covered when any claim supports it, and unknown while
    # some claim's judgment about it is still pending.
    matched = {target for item in assessments for target in item.supported_reference_ids}
    pending = {
        target
        for item in assessments
        for target in (*item.pending_reference_ids, *item.recall_pending_reference_ids)
    }
    all_reference_ids = {item.claim_id for item in reference.reference_claims}
    unknown_reference = (pending - matched) & all_reference_ids
    if unknown_reference:
        recall = _review_observation(
            metric_id="answer.fact_recall",
            reason="reference claim completeness requires semantic judge resolution",
            provenance={
                **boundary,
                "matched_reference_claims": len(matched),
                "planned_reference_claims": len(all_reference_ids),
                "unknown_reference_claim_ids": sorted(unknown_reference),
            },
            evidence=evidence,
        )
    else:
        recall = _ok_ratio(
            metric_id="answer.fact_recall",
            numerator=len(matched & all_reference_ids),
            denominator=len(all_reference_ids),
            provenance=boundary,
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
            provenance={**boundary, "derived_from": ["answer.fact_precision", "answer.fact_recall"]},
            evidence=evidence,
        )
    elif recall.status is ScoreStatus.NOT_APPLICABLE:
        f1 = _na_observation(
            metric_id="answer.fact_f1",
            reason="fact F1 is undefined without reference claims",
            provenance=boundary,
            evidence=evidence,
        )
    else:
        f1 = _review_observation(
            metric_id="answer.fact_f1",
            reason="fact F1 remains unknown until precision and completeness are both valid",
            provenance={
                **boundary,
                "precision_status": precision.status.value,
                "recall_status": recall.status.value,
            },
            evidence=evidence,
        )
    return precision, recall, f1


_VERDICT_METRICS: tuple[tuple[AnswerMetricId, ClaimVerdict, str], ...] = (
    ("answer.contradicted_fact_rate", ClaimVerdict.CONTRADICTED,
     "contradiction status of some claims requires semantic judge resolution"),
    ("answer.unsupported_inference_rate", ClaimVerdict.OVER_INFERENCE,
     "over-inference status of some claims requires semantic judge resolution"),
)


def _score_claim_verdicts(
    *,
    assessments: Sequence[ClaimFactAssessment],
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> tuple[AnswerMetricObservation, AnswerMetricObservation, AnswerMetricObservation]:
    boundary = {"input_boundary": "reference_facts+actual_context+answer_claims", **provenance}
    counts = {verdict: sum(item.verdict is verdict for item in assessments) for verdict in ClaimVerdict}
    unresolved = counts[ClaimVerdict.UNRESOLVED]
    verdict_counts = {verdict.value: count for verdict, count in counts.items()}
    result: list[AnswerMetricObservation] = []
    for metric_id, verdict, unknown_reason in _VERDICT_METRICS:
        result.append(_aggregate_ratio_observation(
            metric_id=metric_id,
            event_count=counts[verdict],
            non_event_count=len(assessments) - counts[verdict] - unresolved,
            unknown_count=unresolved,
            provenance={**boundary, "verdict_counts": verdict_counts},
            evidence=evidence,
            zero_reason="answer has no verifiable claims",
            unknown_reason=unknown_reason,
        ))
    # The unresolved rate measures the unknowns themselves, so it is always known.
    result.append(_aggregate_ratio_observation(
        metric_id="answer.unresolved_claim_rate",
        event_count=unresolved,
        non_event_count=len(assessments) - unresolved,
        unknown_count=0,
        provenance={**boundary, "verdict_counts": verdict_counts},
        evidence=evidence,
        zero_reason="answer has no verifiable claims",
        unknown_reason="unreachable",
    ))
    return result[0], result[1], result[2]


_DIMENSION_METRICS: tuple[tuple[AnswerMetricId, frozenset[FactDimension], str], ...] = (
    ("answer.numeric_accuracy", frozenset({FactDimension.NUMERIC_VALUE}), "numeric value"),
    ("answer.unit_accuracy", frozenset({FactDimension.UNIT}), "unit"),
    ("answer.condition_accuracy", EXPERIMENTAL_CONDITION_DIMENSIONS, "experimental condition"),
)


def _asserted_dimension_count(fact: SemanticFact | None, dimensions: frozenset[FactDimension]) -> int:
    """How many items of a dimension group an unresolved claim asserts."""

    if fact is None:
        return 0
    count = 0
    if FactDimension.NUMERIC_VALUE in dimensions and fact.numeric_value is not None:
        count += 1
    if FactDimension.UNIT in dimensions and fact.unit is not None:
        count += 1
    if FactDimension.DATASET in dimensions and fact.dataset_id is not None:
        count += 1
    if FactDimension.SPLIT in dimensions and fact.split_id is not None:
        count += 1
    if FactDimension.CONDITION in dimensions:
        count += len(fact.conditions)
    return count


def _score_dimension_accuracy(
    *,
    assessments: Sequence[ClaimFactAssessment],
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> tuple[AnswerMetricObservation, AnswerMetricObservation, AnswerMetricObservation]:
    """Per-dimension accuracy over claims aligned to exactly one reference fact.

    Only dimensions the claim explicitly states are counted, so omitting a
    condition is not scored as stating a wrong one. Claims that are about facts
    outside the reference (unsupported) have no aligned target and are excluded.
    """

    boundary = {"input_boundary": "aligned_reference_facts+answer_claims", **provenance}
    result: list[AnswerMetricObservation] = []
    for metric_id, dimensions, label in _DIMENSION_METRICS:
        correct = wrong = unknown = 0
        for item in assessments:
            if item.reference_decision is SemanticDecision.UNSUPPORTED:
                continue
            outcomes = (
                None if item.reference_decision is SemanticDecision.NEEDS_REVIEW
                else item.aligned_dimension_outcomes()
            )
            if outcomes is None:
                unknown += _asserted_dimension_count(item.candidate_fact, dimensions)
                continue
            for dimension in outcomes:
                if dimension.dimension not in dimensions:
                    continue
                if dimension.outcome is DimensionOutcome.MATCH:
                    correct += 1
                elif dimension.outcome is DimensionOutcome.MISMATCH:
                    wrong += 1
                else:
                    unknown += 1
        result.append(_aggregate_ratio_observation(
            metric_id=metric_id,
            event_count=correct,
            non_event_count=wrong,
            unknown_count=unknown,
            provenance={**boundary, "dimension_group": label},
            evidence=evidence,
            zero_reason=f"no aligned answer claim states a comparable {label}",
            unknown_reason=f"{label} correctness of some aligned claims requires semantic judge resolution",
        ))
    return result[0], result[1], result[2]


def _score_faithfulness(
    *,
    assessments: Sequence[ClaimFactAssessment],
    actual_context: RAGContextPack | None,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    boundary = {"input_boundary": "actual_context+answer_claims", **provenance}
    if not assessments:
        return _na_observation(
            metric_id="answer.faithfulness",
            reason="answer has no verifiable claims; faithfulness cannot be vacuously one",
            provenance=boundary,
            evidence=evidence,
        )
    if actual_context is None:
        return _review_observation(
            metric_id="answer.faithfulness",
            reason="actual model context was not supplied",
            provenance=boundary,
            evidence=evidence,
        )
    return _decisions_ratio(
        metric_id="answer.faithfulness",
        decisions=[item.context_decision for item in assessments],
        unknown_reason="actual-context support requires semantic judge resolution",
        provenance={**boundary, "context_pack_id": actual_context.pack_id},
        evidence=(*evidence, f"context://{actual_context.pack_id}"),
    )


def _cited_claim_decision(
    claim: AnswerClaim,
    assessment: ClaimFactAssessment,
    binding: CitationBinding | None,
) -> SemanticDecision:
    if binding is None or binding.state is CitationParseState.INVALID:
        return SemanticDecision.UNSUPPORTED
    if binding.state is CitationParseState.NEEDS_REVIEW:
        return SemanticDecision.NEEDS_REVIEW
    return assessment.decision_over_evidence(claim.evidence_ids)


def _score_citation_support(
    *,
    claims: Sequence[AnswerClaim],
    assessments: Sequence[ClaimFactAssessment],
    bindings: Sequence[CitationBinding],
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    boundary = {
        "input_boundary": "exact_cited_source+bound_answer_claim",
        "citation_binding_rule_version": CITATION_BINDING_RULE_VERSION,
        **provenance,
    }
    assessment_by_id = {item.claim_id: item for item in assessments}
    binding_by_id = {item.claim_id: item for item in bindings}
    cited = [claim for claim in claims if claim.evidence_ids]
    if not cited:
        return _na_observation(
            metric_id="answer.citation_support",
            reason="no claim-bound citations exist; citation support cannot be vacuously one",
            provenance=boundary,
            evidence=evidence,
        )
    decisions: list[SemanticDecision] = []
    refs: list[str] = []
    for claim in cited:
        binding = binding_by_id.get(claim.claim_id)
        decision = _cited_claim_decision(claim, assessment_by_id[claim.claim_id], binding)
        decisions.append(decision)
        if binding is not None and binding.state is CitationParseState.PARSED:
            refs.extend(source.evidence_reference for source in binding.sources)
    return _decisions_ratio(
        metric_id="answer.citation_support",
        decisions=decisions,
        unknown_reason="citation semantics or exact source identity requires review",
        provenance=boundary,
        evidence=tuple(dict.fromkeys((*evidence, *refs))),
    )


def _score_citation_coverage(
    *,
    claims: Sequence[AnswerClaim],
    assessments: Sequence[ClaimFactAssessment],
    bindings: Sequence[CitationBinding],
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    boundary = {
        "input_boundary": "answer_claims+exact_citation_bindings",
        "citation_binding_rule_version": CITATION_BINDING_RULE_VERSION,
        **provenance,
    }
    if not claims:
        return _na_observation(
            metric_id="answer.citation_coverage",
            reason="answer has no factual claim denominator; coverage cannot be vacuously one",
            provenance=boundary,
            evidence=evidence,
        )
    assessment_by_id = {item.claim_id: item for item in assessments}
    binding_by_id = {item.claim_id: item for item in bindings}
    decisions: list[SemanticDecision] = []
    for claim in claims:
        # Missing citations are an observed zero, independent of whether fact
        # correctness or context support is known.
        if not claim.evidence_ids:
            decisions.append(SemanticDecision.UNSUPPORTED)
            continue
        binding = binding_by_id.get(claim.claim_id)
        if binding is None:
            decisions.append(SemanticDecision.NEEDS_REVIEW)
            continue
        decisions.append(_cited_claim_decision(claim, assessment_by_id[claim.claim_id], binding))
    return _decisions_ratio(
        metric_id="answer.citation_coverage",
        decisions=decisions,
        unknown_reason="sufficient citation coverage requires exact-source semantic review",
        provenance=boundary,
        evidence=evidence,
    )


def _policy_key(policy: ReviewAcceptancePolicy) -> str:
    return f"{policy.policy_id}@{policy.policy_version}"


def _reviewed_class(reference: ReferenceLabel, policy: ReviewAcceptancePolicy) -> ReviewedClass:
    if reference.answerability == "needs_review" or not policy.accepts(reference.review):
        return ReviewedClass.NEEDS_REVIEW
    return ReviewedClass.POSITIVE if reference.answerability == "answerable" else ReviewedClass.NEGATIVE


def _review_provenance(reference: ReferenceLabel, policy: ReviewAcceptancePolicy) -> dict[str, Any]:
    return {
        "review_authority": reference.review.authority,
        "review_policy": _policy_key(policy),
    }


def _attributed(
    observations: Sequence[AnswerMetricObservation],
    judged: bool,
) -> tuple[AnswerMetricObservation, ...]:
    if not judged:
        return tuple(observations)
    return tuple(
        AnswerMetricObservation.model_validate({
            **item.model_dump(),
            "source": ScoreSource.SEMANTIC_JUDGE,
        })
        for item in observations
    )


def _with_provenance(
    observations: Sequence[AnswerMetricObservation],
    extra: Mapping[str, Any],
) -> tuple[AnswerMetricObservation, ...]:
    return tuple(
        AnswerMetricObservation.model_validate({
            **item.model_dump(),
            "provenance": {**item.provenance, **extra},
        })
        for item in observations
    )


def _claim_span_overrides(candidate: GroundedAnswerCandidate) -> Mapping[str, Any]:
    raw = candidate.metadata.get("claim_spans", {})
    return raw if isinstance(raw, Mapping) else {}


def _resolve_claim_span(
    answer_text: str,
    claim: AnswerClaim,
    overrides: Mapping[str, Any],
) -> tuple[ClaimAnswerSpan | None, str]:
    """Locate the verbatim answer span a claim is grounded on.

    A declared ``claim_spans`` entry binds a decontextualized atomic claim to the
    exact answer text that asserts it; its ``text`` must equal that slice so the
    binding cannot silently drift when the answer text changes. Without a
    declaration the claim text itself must occur exactly once in the answer.
    """

    raw = overrides.get(claim.claim_id)
    if isinstance(raw, Mapping):
        start, end, text = raw.get("start"), raw.get("end"), raw.get("text")
        if (
            isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start < end <= len(answer_text)
            and isinstance(text, str)
            and answer_text[start:end] == text
        ):
            return ClaimAnswerSpan(
                claim_id=claim.claim_id,
                start=start,
                end=end,
                text=text,
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


def _decisions_ratio(
    *,
    metric_id: AnswerMetricId,
    decisions: Sequence[SemanticDecision],
    unknown_reason: str,
    provenance: dict[str, Any],
    evidence: tuple[str, ...],
) -> AnswerMetricObservation:
    supported = sum(item is SemanticDecision.SUPPORTED for item in decisions)
    contradicted = sum(item is SemanticDecision.CONTRADICTED for item in decisions)
    unsupported = sum(item is SemanticDecision.UNSUPPORTED for item in decisions)
    unknown = sum(item is SemanticDecision.NEEDS_REVIEW for item in decisions)
    counts = {
        "known_supported": supported,
        "known_contradicted": contradicted,
        "known_unsupported": unsupported,
    }
    if unknown:
        return _review_observation(
            metric_id=metric_id,
            reason=unknown_reason,
            provenance={
                **provenance,
                **counts,
                "unknown": unknown,
                "planned_denominator": len(decisions),
            },
            evidence=evidence,
        )
    return _ok_ratio(
        metric_id=metric_id,
        numerator=supported,
        denominator=len(decisions),
        provenance={**provenance, **counts},
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
            source=observation.source,
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
        definition(
            "answer.contradicted_fact_rate",
            required_inputs=frozenset({"answer_claims", "reference_claims", "actual_context"}),
            direction=MetricDirection.LOWER_IS_BETTER,
        ),
        definition(
            "answer.unsupported_inference_rate",
            required_inputs=frozenset({"answer_claims", "reference_claims", "actual_context"}),
            direction=MetricDirection.LOWER_IS_BETTER,
        ),
        definition(
            "answer.unresolved_claim_rate",
            required_inputs=frozenset({"answer_claims", "reference_claims", "actual_context"}),
            direction=MetricDirection.LOWER_IS_BETTER,
        ),
        definition("answer.numeric_accuracy", required_inputs=frozenset({"answer_claims", "reference_claims"})),
        definition("answer.unit_accuracy", required_inputs=frozenset({"answer_claims", "reference_claims"})),
        definition("answer.condition_accuracy", required_inputs=frozenset({"answer_claims", "reference_claims"})),
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
    "ClaimFactAssessment",
    "ClaimVerdict",
    "ComparisonTargetKind",
    "EvidenceFactProjection",
    "GroundedScoreResult",
    "PassageDecision",
    "PendingJudgment",
    "ResolvedDimension",
    "SemanticResolution",
    "ExactSourceExcerpt",
    "NumericUnit",
    "ReferenceFactProjection",
    "ReviewedClass",
    "SemanticCondition",
    "SemanticDecision",
    "SemanticFact",
    "TargetComparison",
    "aggregate_abstention_metrics",
    "aggregate_planned_abstention",
    "answer_metric_definitions",
    "build_answer_metric_registry",
    "evidence_source_identity",
    "classify_abstention",
    "classify_case_abstention",
    "parse_citation_bindings",
    "register_answer_metrics",
    "score_answer",
    "score_answer_candidate",
    "score_grounded_answer",
]
