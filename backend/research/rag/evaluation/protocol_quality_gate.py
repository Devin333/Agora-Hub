"""Deterministic quality policy and gate for the Research RAG protocol.

The gate consumes frozen score slots and typed protocol evidence. It does not
execute evaluations, manufacture thresholds, rank variants, or perform any
promotion/publication action.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum
from statistics import median
from typing import Annotated, Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION, SCHEMA_VERSION, EvaluationTrack, ExecutionProfile,
    ImmutableVersionStr, NonEmptyStr, ProtocolModel, Sha256Digest, content_digest,
)
from backend.research.rag.evaluation.protocol_comparison import qualify_comparison
from backend.research.rag.evaluation.protocol_execution import (
    ComparisonIdentity, EvaluationOptions, EvaluationRun, QualificationDecision,
    RunExecutionStatus,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EndToEndOutcome, EndToEndSummary, EvaluationScore, MetricAggregation,
    MetricDirection, MetricRegistry, ScoreCompleteness, ScoreSlot, ScoreStatus,
)


class MetricEvidenceSource(StrEnum):
    SCORE = "score"
    END_TO_END = "end_to_end"


class ThresholdBasis(StrEnum):
    POINT_ESTIMATE = "point_estimate"
    LOWER_CONFIDENCE_BOUND = "lower_confidence_bound"
    UPPER_CONFIDENCE_BOUND = "upper_confidence_bound"


class StageEvidenceStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"


class ComparisonStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    MISSING = "missing"
    COMPARABLE = "comparable"
    INCOMPARABLE = "incomparable"


class QualificationScope(StrEnum):
    LOCAL_POLICY = "local_policy"
    PRODUCTION_QUALIFICATION = "production_qualification"


class ComparisonField(StrEnum):
    VARIANT_FINGERPRINT = "variant_fingerprint"
    CODE_FINGERPRINT = "code_fingerprint"
    ENVIRONMENT_FINGERPRINT = "environment_fingerprint"
    BUDGET_FINGERPRINT = "budget_fingerprint"
    INITIAL_STATE_FINGERPRINT = "initial_state_fingerprint"
    NAMESPACE_FINGERPRINT = "namespace_fingerprint"


def _finite_number(value: Any, field_name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite non-boolean number or null")
    return value


def _digestable(value: Any) -> Any:
    if isinstance(value, ProtocolModel):
        return _digestable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _digestable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted((_digestable(item) for item in value), key=str)
    if isinstance(value, (tuple, list)):
        return [_digestable(item) for item in value]
    return value


def _policy_digest_content(value: Any) -> Any:
    payload = _digestable(value)
    if isinstance(payload, dict):
        payload.pop("approval", None)
        payload.pop("policy_digest", None)
        if "allowed_comparison_differences" in payload:
            payload["allowed_comparison_differences"] = sorted(
                payload["allowed_comparison_differences"]
            )
    return payload


def _portable_reference(value: str) -> str:
    if "://" not in value or value.startswith(("file://", "F:/", "C:/", "/")):
        raise ValueError("approval/evidence references must be portable artifact URIs")
    return value


def _unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


class HumanApprovalReference(ProtocolModel):
    """An externally auditable human approval of one exact policy version."""

    policy_id: NonEmptyStr
    policy_version: ImmutableVersionStr
    scoring_revision: ImmutableVersionStr
    reviewer_id: NonEmptyStr
    reviewer_kind: Literal["human"]
    approved_at: datetime
    approval_reference: NonEmptyStr
    approval_content_sha256: Sha256Digest
    approved_policy_digest: Sha256Digest

    @field_validator("approved_at")
    @classmethod
    def _aware_approval_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return value

    @field_validator("approval_reference")
    @classmethod
    def _portable_approval_reference(cls, value: str) -> str:
        return _portable_reference(value)


class MetricThresholdRule(ProtocolModel):
    rule_type: Literal["metric_threshold"] = "metric_threshold"
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr
    applicable: bool
    metric_id: NonEmptyStr
    metric_version: ImmutableVersionStr
    scoring_revision: ImmutableVersionStr
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    scope: NonEmptyStr
    evidence_source: MetricEvidenceSource
    aggregation: MetricAggregation
    direction: MetricDirection
    threshold_basis: ThresholdBasis
    min: float | None
    max: float | None
    delta_min: float | None
    delta_max: float | None
    min_coverage: float | None
    min_independent_cases: int | None
    min_paper_families: int | None

    @field_validator("min", "max", "delta_min", "delta_max", mode="before")
    @classmethod
    def _finite_thresholds(cls, value: Any, info: Any) -> Any:
        return _finite_number(value, info.field_name)

    @field_validator("min_coverage", mode="before")
    @classmethod
    def _coverage_range(cls, value: Any) -> Any:
        value = _finite_number(value, "min_coverage")
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("min_coverage must be between zero and one")
        return value

    @field_validator("min_independent_cases", "min_paper_families", mode="before")
    @classmethod
    def _sample_minimums(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{info.field_name} must be a non-negative integer or null")
        return value

    @model_validator(mode="after")
    def _ordered_thresholds(self) -> Self:
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min must not exceed max")
        if self.delta_min is not None and self.delta_max is not None and self.delta_min > self.delta_max:
            raise ValueError("delta_min must not exceed delta_max")
        if self.evidence_source is MetricEvidenceSource.END_TO_END and (
            self.metric_id != "end_to_end_success_rate"
            or self.track is not EvaluationTrack.END_TO_END
            or self.aggregation is not MetricAggregation.RATIO
        ):
            raise ValueError("end_to_end evidence requires the end_to_end_success_rate ratio")
        return self


class StageRequirementRule(ProtocolModel):
    rule_type: Literal["stage_requirement"] = "stage_requirement"
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr
    applicable: bool
    stage_id: NonEmptyStr
    track: EvaluationTrack
    execution_profile: ExecutionProfile


class ReviewCoverageRule(ProtocolModel):
    rule_type: Literal["review_coverage"] = "review_coverage"
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr
    applicable: bool
    review_plan_id: NonEmptyStr
    review_plan_version: ImmutableVersionStr
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    scope: NonEmptyStr
    min_coverage: float | None
    min_reviewed_cases: int | None
    min_paper_families: int | None

    @field_validator("min_coverage", mode="before")
    @classmethod
    def _coverage_range(cls, value: Any) -> Any:
        value = _finite_number(value, "min_coverage")
        if value is not None and not 0.0 <= value <= 1.0:
            raise ValueError("min_coverage must be between zero and one")
        return value

    @field_validator("min_reviewed_cases", "min_paper_families", mode="before")
    @classmethod
    def _sample_minimums(cls, value: Any, info: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{info.field_name} must be a non-negative integer or null")
        return value


QualityRule = Annotated[
    MetricThresholdRule | StageRequirementRule | ReviewCoverageRule,
    Field(discriminator="rule_type"),
]


class QualityPolicy(ProtocolModel):
    record_type: Literal["quality_policy"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    policy_id: NonEmptyStr
    policy_version: ImmutableVersionStr
    scoring_revision: ImmutableVersionStr
    approval: HumanApprovalReference | None
    allowed_comparison_differences: frozenset[ComparisonField]
    rules: tuple[QualityRule, ...]
    policy_digest: Sha256Digest

    @classmethod
    def calculate_policy_digest(cls, **content: Any) -> str:
        return content_digest(_policy_digest_content(content))

    @classmethod
    def create(cls, **content: Any) -> Self:
        if "policy_digest" in content:
            raise ValueError("policy_digest is calculated")
        return cls(**content, policy_digest=cls.calculate_policy_digest(**content))

    @model_validator(mode="after")
    def _validate_policy_identity(self) -> Self:
        keys = tuple((rule.rule_id, rule.rule_version) for rule in self.rules)
        if len(keys) != len(set(keys)):
            raise ValueError("quality policy rules must have unique id/version identities")
        if self.approval is not None and (
            self.approval.policy_id != self.policy_id
            or self.approval.policy_version != self.policy_version
            or self.approval.scoring_revision != self.scoring_revision
            or self.approval.approved_policy_digest != self.policy_digest
        ):
            raise ValueError(
                "human approval must reference the exact policy content and scoring revision"
            )
        if any(
            isinstance(rule, MetricThresholdRule)
            and rule.scoring_revision != self.scoring_revision
            for rule in self.rules
        ):
            raise ValueError("metric rules must use the policy scoring_revision")
        expected = self.calculate_policy_digest(**self.model_dump(mode="json"))
        if self.policy_digest != expected:
            raise ValueError("policy_digest does not match policy content")
        return self


class CasePaperFamily(ProtocolModel):
    case_id: NonEmptyStr
    paper_family_id: NonEmptyStr


class StageEvidence(ProtocolModel):
    stage_id: NonEmptyStr
    status: StageEvidenceStatus
    evidence_references: tuple[NonEmptyStr, ...]
    reason: NonEmptyStr | None

    @field_validator("evidence_references")
    @classmethod
    def _evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, "evidence_references")
        for item in value:
            _portable_reference(item)
        return value

    @model_validator(mode="after")
    def _status_evidence(self) -> Self:
        if self.status is StageEvidenceStatus.COMPLETED:
            if not self.evidence_references or self.reason is not None:
                raise ValueError("completed stages require evidence and no failure reason")
        elif self.reason is None:
            raise ValueError("non-completed stages require a reason")
        return self


class ReviewCoverageEvidence(ProtocolModel):
    review_plan_id: NonEmptyStr
    review_plan_version: ImmutableVersionStr
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    scope: NonEmptyStr
    planned_case_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    reviewed_case_ids: tuple[NonEmptyStr, ...]
    not_reviewed_case_ids: tuple[NonEmptyStr, ...]
    pending_case_ids: tuple[NonEmptyStr, ...]
    error_case_ids: tuple[NonEmptyStr, ...]
    reviewer_ids: tuple[NonEmptyStr, ...]
    reviewer_kind: Literal["human"]
    evidence_references: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _complete_partition(self) -> Self:
        groups = (
            self.reviewed_case_ids, self.not_reviewed_case_ids,
            self.pending_case_ids, self.error_case_ids,
        )
        _unique(self.planned_case_ids, "planned_case_ids")
        for name, values in zip(
            ("reviewed_case_ids", "not_reviewed_case_ids", "pending_case_ids", "error_case_ids"),
            groups,
            strict=True,
        ):
            _unique(values, name)
        flattened = tuple(item for group in groups for item in group)
        if len(flattened) != len(set(flattened)) or set(flattened) != set(self.planned_case_ids):
            raise ValueError("review case states must exactly partition planned_case_ids")
        _unique(self.reviewer_ids, "reviewer_ids")
        _unique(self.evidence_references, "evidence_references")
        if self.reviewed_case_ids and (not self.reviewer_ids or not self.evidence_references):
            raise ValueError("reviewed cases require named human reviewers and evidence references")
        for item in self.evidence_references:
            _portable_reference(item)
        return self

    @property
    def coverage(self) -> float:
        return len(self.reviewed_case_ids) / len(self.planned_case_ids)

    @property
    def has_unknowns(self) -> bool:
        return bool(self.pending_case_ids or self.error_case_ids)


class MetricUncertaintyEvidence(ProtocolModel):
    metric_id: NonEmptyStr
    metric_version: ImmutableVersionStr
    scoring_revision: ImmutableVersionStr
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    scope: NonEmptyStr
    lower_confidence_bound: float
    upper_confidence_bound: float
    method_id: NonEmptyStr
    method_version: ImmutableVersionStr
    evidence_references: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("lower_confidence_bound", "upper_confidence_bound", mode="before")
    @classmethod
    def _finite_bounds(cls, value: Any, info: Any) -> Any:
        return _finite_number(value, info.field_name)

    @field_validator("evidence_references")
    @classmethod
    def _evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, "evidence_references")
        for item in value:
            _portable_reference(item)
        return value

    @model_validator(mode="after")
    def _ordered_bounds(self) -> Self:
        if self.lower_confidence_bound > self.upper_confidence_bound:
            raise ValueError("lower_confidence_bound must not exceed upper_confidence_bound")
        return self

    @property
    def identity(self) -> tuple[object, ...]:
        return (
            self.metric_id, self.metric_version, self.scoring_revision,
            self.track, self.execution_profile, self.scope,
        )


class QualityEvidence(ProtocolModel):
    execution_status: RunExecutionStatus
    comparison_identity: ComparisonIdentity
    evaluation_options: EvaluationOptions | None = None
    evaluation_run: EvaluationRun | None = None
    planned_score_slots: tuple[ScoreSlot, ...]
    scores: tuple[EvaluationScore, ...]
    end_to_end_summary: EndToEndSummary | None
    stages: tuple[StageEvidence, ...]
    review_coverage: tuple[ReviewCoverageEvidence, ...]
    case_paper_families: tuple[CasePaperFamily, ...]
    uncertainty: tuple[MetricUncertaintyEvidence, ...]

    @model_validator(mode="after")
    def _link_typed_evidence(self) -> Self:
        slot_ids = tuple(slot.identity for slot in self.planned_score_slots)
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("planned_score_slots must not contain duplicates")
        known_slots = set(slot_ids)
        slots_by_identity = {slot.identity: slot for slot in self.planned_score_slots}
        score_ids: set[tuple[object, ...]] = set()
        for score in self.scores:
            identity = ScoreSlot.from_score(score, applicable=True).identity
            if identity not in known_slots:
                raise ValueError("every score must reference an actual planned_score_slot")
            if identity in score_ids:
                raise ValueError("scores must not duplicate a planned score slot")
            planned_slot = slots_by_identity[identity]
            if planned_slot.applicable and score.status is ScoreStatus.NOT_APPLICABLE:
                raise ValueError("an applicable planned score slot cannot be observed as not_applicable")
            if not planned_slot.applicable and score.status is not ScoreStatus.NOT_APPLICABLE:
                raise ValueError("a frozen non-applicable score slot requires a not_applicable observation")
            score_ids.add(identity)
        _unique(tuple(item.stage_id for item in self.stages), "stages.stage_id")
        review_keys = tuple(
            (item.review_plan_id, item.review_plan_version, item.track, item.execution_profile, item.scope)
            for item in self.review_coverage
        )
        if len(review_keys) != len(set(review_keys)):
            raise ValueError("review_coverage identities must be unique")
        _unique(tuple(item.case_id for item in self.case_paper_families), "case_paper_families.case_id")
        uncertainty_keys = tuple(item.identity for item in self.uncertainty)
        if len(uncertainty_keys) != len(set(uncertainty_keys)):
            raise ValueError("uncertainty identities must be unique")

        identity = self.comparison_identity
        typed_options = (
            EvaluationOptions.model_validate_json(self.evaluation_options.model_dump_json())
            if self.evaluation_options is not None else None
        )
        typed_run = (
            EvaluationRun.model_validate_json(self.evaluation_run.model_dump_json())
            if self.evaluation_run is not None else None
        )
        if typed_options is not None:
            selected_identities = tuple(
                item for item in typed_options.experiment_manifest.comparison_identities
                if item.variant_fingerprint == typed_options.selected_variant_fingerprint
                and item.track is typed_options.selected_variant.track
                and item.execution_profile is typed_options.execution_profile
            )
            if selected_identities != (identity,):
                raise ValueError("evaluation_options must contain the exact QualityEvidence comparison_identity")
        if typed_run is not None:
            run = typed_run
            if run.comparison_identity != identity or run.execution_status is not self.execution_status:
                raise ValueError("evaluation_run identity/status must match QualityEvidence")
            run_ids = {slot.run_id for slot in self.planned_score_slots}
            if self.end_to_end_summary is not None:
                run_ids.update(result.slot.run_id for result in self.end_to_end_summary.slots)
            if run_ids and run_ids != {run.run_id}:
                raise ValueError("all QualityEvidence observations must belong to evaluation_run.run_id")
            if typed_options is not None:
                options = typed_options
                manifest = options.experiment_manifest
                selected_units = tuple(
                    unit for unit in manifest.planned_units
                    if unit.track is options.selected_variant.track
                    and unit.variant_fingerprint == options.selected_variant_fingerprint
                )
                if (
                    run.experiment_id != manifest.experiment_id
                    or run.experiment_purpose is not manifest.experiment_purpose
                    or run.experiment_manifest_digest != manifest.manifest_digest
                    or run.dataset_id != manifest.dataset_id
                    or run.dataset_version != manifest.dataset_version
                    or run.dataset_digest != manifest.dataset_digest
                    or run.options_fingerprint != content_digest(options)
                    or run.selected_variant_fingerprint != options.selected_variant_fingerprint
                    or run.variant_configuration != options.selected_variant
                    or run.case_scopes != manifest.case_scopes
                    or run.planned_units != selected_units
                    or run.components != options.components
                    or run.source_access_grants != options.source_access_grants
                    or run.budget_limits != options.budget_limits
                    or run.namespaces != options.namespaces
                    or run.initial_state != options.initial_state
                    or run.code_identity != options.code_identity
                    or run.environment != options.environment
                    or run.execution_path != options.execution_path
                    or run.corpus_snapshot_id != options.corpus.corpus_snapshot_id
                    or run.corpus_content_sha256 != options.corpus.corpus_content_sha256
                    or run.index_snapshot_id != options.corpus.index_snapshot_id
                ):
                    raise ValueError("evaluation_run must be derived from the exact evaluation_options")
        for slot in self.planned_score_slots:
            if (
                slot.dataset_digest != identity.dataset_digest
                or slot.track is not identity.track
                or slot.execution_profile is not identity.execution_profile
                or slot.variant_fingerprint != identity.variant_fingerprint
            ):
                raise ValueError("planned score slot identity must match comparison_identity")
        if self.end_to_end_summary is not None:
            for result in self.end_to_end_summary.slots:
                slot = result.slot
                if (
                    slot.dataset_digest != identity.dataset_digest
                    or slot.track is not identity.track
                    or slot.execution_profile is not identity.execution_profile
                    or slot.variant_fingerprint != identity.variant_fingerprint
                ):
                    raise ValueError("end-to-end slot identity must match comparison_identity")
        return self


class ComparisonEvidence(ProtocolModel):
    baseline_identity: ComparisonIdentity
    candidate_identity: ComparisonIdentity
    allowed_differences: frozenset[ComparisonField]
    paired_case_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    evidence_references: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("paired_case_ids")
    @classmethod
    def _paired_cases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, "paired_case_ids")

    @field_validator("evidence_references")
    @classmethod
    def _evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _unique(value, "evidence_references")
        for item in value:
            _portable_reference(item)
        return value


class RuleEvaluation(ProtocolModel):
    rule_id: NonEmptyStr
    rule_version: ImmutableVersionStr
    rule_type: NonEmptyStr
    decision: QualificationDecision
    observed_value: float | None
    possible_min: float | None
    possible_max: float | None
    baseline_value: float | None
    delta: float | None
    coverage: float | None
    independent_cases: int | None
    paper_families: int | None
    evidence_references: tuple[NonEmptyStr, ...]
    missing_requirements: tuple[NonEmptyStr, ...]
    errors: tuple[NonEmptyStr, ...]
    reason: NonEmptyStr

    @field_validator(
        "observed_value", "possible_min", "possible_max", "baseline_value", "delta", "coverage",
        mode="before",
    )
    @classmethod
    def _finite_optional_values(cls, value: Any, info: Any) -> Any:
        return _finite_number(value, info.field_name)

    @model_validator(mode="after")
    def _pass_has_no_hidden_unknowns(self) -> Self:
        if self.decision is QualificationDecision.PASS and (self.missing_requirements or self.errors):
            raise ValueError("a passing rule cannot retain missing requirements or errors")
        return self


class QualityGateResult(ProtocolModel):
    record_type: Literal["quality_gate_result"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    policy_id: NonEmptyStr
    policy_version: ImmutableVersionStr
    policy_digest: Sha256Digest
    execution_status: RunExecutionStatus
    decision: QualificationDecision
    comparison_status: ComparisonStatus
    execution_profile: ExecutionProfile
    qualification_scope: QualificationScope
    production_qualification_eligible: bool
    production_qualification_missing_requirements: tuple[NonEmptyStr, ...]
    rule_evaluations: tuple[RuleEvaluation, ...]
    missing_requirements: tuple[NonEmptyStr, ...]
    errors: tuple[NonEmptyStr, ...]
    side_effects_performed: Literal[False]

    @model_validator(mode="after")
    def _qualification_boundary(self) -> Self:
        if self.production_qualification_eligible and (
            self.decision is not QualificationDecision.PASS
            or self.execution_profile is not ExecutionProfile.PRODUCTION
            or self.qualification_scope is not QualificationScope.PRODUCTION_QUALIFICATION
            or self.production_qualification_missing_requirements
        ):
            raise ValueError("production qualification evidence requires a complete production pass")
        if (
            self.qualification_scope is QualificationScope.PRODUCTION_QUALIFICATION
            and self.production_qualification_missing_requirements
        ):
            raise ValueError("production qualification scope cannot retain missing run/preflight evidence")
        if self.decision is QualificationDecision.PASS and (self.missing_requirements or self.errors):
            raise ValueError("a passing gate cannot retain missing requirements or errors")
        return self


@dataclass(frozen=True)
class _Aggregate:
    point: float | None
    possible_min: float | None
    possible_max: float | None
    coverage: float | None
    complete: bool
    independent_cases: int
    planned_cases: int
    paper_families: int | None
    planned_paper_families: int | None
    missing_requirements: tuple[str, ...]
    errors: tuple[str, ...]
    evidence_references: tuple[str, ...]


def _rule_matches_slot(rule: MetricThresholdRule, slot: ScoreSlot) -> bool:
    return (
        slot.metric_id == rule.metric_id
        and slot.version == rule.metric_version
        and slot.scoring_revision == rule.scoring_revision
        and slot.track is rule.track
        and slot.execution_profile is rule.execution_profile
        and slot.scope == rule.scope
    )


def _family_counts(
    evidence: QualityEvidence,
    *,
    valid_case_ids: set[str],
    planned_case_ids: set[str],
) -> tuple[int | None, int | None, tuple[str, ...]]:
    family_by_case = {item.case_id: item.paper_family_id for item in evidence.case_paper_families}
    missing = tuple(sorted(case_id for case_id in planned_case_ids if case_id not in family_by_case))
    if missing:
        return None, None, ("paper_family_mapping",)
    return (
        len({family_by_case[item] for item in valid_case_ids}),
        len({family_by_case[item] for item in planned_case_ids}),
        (),
    )


def _aggregate_score_values(
    *, registry: MetricRegistry, evidence: QualityEvidence, rule: MetricThresholdRule,
) -> _Aggregate:
    slots = tuple(slot for slot in evidence.planned_score_slots if _rule_matches_slot(rule, slot))
    if not slots:
        return _Aggregate(
            None, None, None, None, False, 0, 0, None, None,
            ("planned_score_slots",), (), (),
        )
    identities = {slot.identity for slot in slots}
    scores = tuple(
        score for score in evidence.scores
        if ScoreSlot.from_score(score, applicable=True).identity in identities
    )
    completeness: ScoreCompleteness = registry.summarize_completeness(slots, scores)
    definition = registry.definition(rule.metric_id, rule.metric_version)
    errors: list[str] = []
    if definition.aggregation is not rule.aggregation:
        errors.append("policy aggregation does not match MetricRegistry")
    if not definition.applies_to(
        track=rule.track, execution_profile=rule.execution_profile, scope=rule.scope,
    ):
        errors.append("policy identity is outside MetricRegistry applicability")
    if definition.direction is not rule.direction:
        errors.append("policy direction does not match MetricRegistry")

    valid_results = tuple(result for result in completeness.slots if result.state.value == "valid")
    valid_scores = tuple(result.score for result in valid_results if result.score is not None)
    values = tuple(score.value for score in valid_scores if score.value is not None)
    valid_case_ids = {result.slot.case_id for result in valid_results}
    planned_case_ids = {result.slot.case_id for result in completeness.slots if result.slot.applicable}
    families, planned_families, family_missing = _family_counts(
        evidence, valid_case_ids=valid_case_ids, planned_case_ids=planned_case_ids,
    )

    complete = completeness.error == 0 and completeness.pending_review == 0
    point: float | None = None
    possible_min: float | None = None
    possible_max: float | None = None
    applicable = completeness.applicable
    unknown = completeness.error + completeness.pending_review
    domain = definition.domain
    if applicable > 0:
        if rule.aggregation is MetricAggregation.MEAN:
            known_sum = sum(values)
            point = known_sum / applicable if complete else None
            possible_min = (known_sum + unknown * domain.minimum) / applicable
            possible_max = (known_sum + unknown * domain.maximum) / applicable
        elif rule.aggregation in {MetricAggregation.SUM, MetricAggregation.COUNT}:
            known_sum = sum(values)
            point = known_sum if complete else None
            possible_min = known_sum + unknown * domain.minimum
            possible_max = known_sum + unknown * domain.maximum
        elif rule.aggregation is MetricAggregation.RATIO:
            if complete:
                numerator = sum(score.numerator or 0 for score in valid_scores)
                denominator = sum(score.denominator or 0 for score in valid_scores)
                if denominator > 0:
                    point = numerator / denominator
                    possible_min = possible_max = point
            else:
                possible_min, possible_max = domain.minimum, domain.maximum
        elif rule.aggregation is MetricAggregation.MEDIAN:
            if complete and values:
                point = float(median(values))
                possible_min = possible_max = point
            else:
                possible_min, possible_max = domain.minimum, domain.maximum
        elif rule.aggregation is MetricAggregation.PERCENTILE:
            if complete and len(values) == 1:
                point = values[0]
                possible_min = possible_max = point
            else:
                errors.append("percentile thresholds require one pre-aggregated registered score")
                possible_min, possible_max = domain.minimum, domain.maximum

    references = tuple(sorted({item for score in valid_scores for item in score.evidence}))
    missing = list(family_missing)
    if completeness.applicable == 0:
        missing.append("mandatory_metric_has_no_applicable_slot")
    if completeness.error:
        missing.append("required_metric_error_or_missing")
    if completeness.pending_review:
        missing.append("required_metric_pending_review")
    return _Aggregate(
        point, possible_min, possible_max, completeness.coverage,
        complete and completeness.applicable > 0,
        len(valid_case_ids), len(planned_case_ids), families, planned_families,
        tuple(dict.fromkeys(missing)), tuple(errors), references,
    )


def _aggregate_end_to_end(
    *, registry: MetricRegistry, evidence: QualityEvidence, rule: MetricThresholdRule,
) -> _Aggregate:
    definition = registry.definition(rule.metric_id, rule.metric_version)
    errors: list[str] = []
    if definition.aggregation is not rule.aggregation:
        errors.append("policy aggregation does not match MetricRegistry")
    if not definition.applies_to(
        track=rule.track, execution_profile=rule.execution_profile, scope=rule.scope,
    ):
        errors.append("policy identity is outside MetricRegistry applicability")
    if definition.direction is not rule.direction:
        errors.append("policy direction does not match MetricRegistry")
    summary = evidence.end_to_end_summary
    if summary is None:
        return _Aggregate(None, None, None, None, False, 0, 0, None, None, ("end_to_end_summary",), tuple(errors), ())
    matching = tuple(
        result for result in summary.slots
        if result.slot.track is rule.track
        and result.slot.execution_profile is rule.execution_profile
        and result.slot.scope == rule.scope
    )
    if len(matching) != len(summary.slots):
        errors.append("end_to_end_summary contains slots outside the policy identity")
    if summary.applicable == 0:
        return _Aggregate(
            None, None, None, None, False, 0, 0, None, None,
            ("mandatory_metric_has_no_applicable_slot",), tuple(errors), (),
        )
    lower = summary.known_success / summary.applicable
    upper = (summary.known_success + summary.unknown) / summary.applicable
    known_case_ids = {
        result.slot.case_id for result in matching
        if result.outcome in {EndToEndOutcome.KNOWN_SUCCESS, EndToEndOutcome.KNOWN_FAILURE}
    }
    planned_case_ids = {result.slot.case_id for result in matching if result.slot.applicable}
    families, planned_families, family_missing = _family_counts(
        evidence, valid_case_ids=known_case_ids, planned_case_ids=planned_case_ids,
    )
    refs = tuple(sorted({
        f"artifact://run/{result.slot.run_id}/attempt/{result.slot.attempt_id}"
        for result in matching if result.observation is not None
    }))
    missing = list(family_missing)
    if summary.unknown:
        missing.append("required_end_to_end_outcome_unknown")
    return _Aggregate(
        summary.success_rate, lower, upper,
        (summary.known_success + summary.known_failure) / summary.applicable,
        summary.unknown == 0,
        len(known_case_ids), len(planned_case_ids), families, planned_families,
        tuple(missing), tuple(errors), refs,
    )


def _aggregate_metric(
    registry: MetricRegistry, evidence: QualityEvidence, rule: MetricThresholdRule,
) -> _Aggregate:
    if rule.evidence_source is MetricEvidenceSource.END_TO_END:
        return _aggregate_end_to_end(registry=registry, evidence=evidence, rule=rule)
    return _aggregate_score_values(registry=registry, evidence=evidence, rule=rule)


def _uncertainty_for(
    evidence: QualityEvidence, rule: MetricThresholdRule,
) -> MetricUncertaintyEvidence | None:
    identity = (
        rule.metric_id, rule.metric_version, rule.scoring_revision,
        rule.track, rule.execution_profile, rule.scope,
    )
    return next((item for item in evidence.uncertainty if item.identity == identity), None)


def _threshold_interval(
    aggregate: _Aggregate, evidence: QualityEvidence, rule: MetricThresholdRule,
) -> tuple[float | None, float | None, float | None, tuple[str, ...], tuple[str, ...]]:
    if rule.threshold_basis is ThresholdBasis.POINT_ESTIMATE:
        return aggregate.point, aggregate.possible_min, aggregate.possible_max, (), ()
    uncertainty = _uncertainty_for(evidence, rule)
    if uncertainty is None:
        return None, None, None, ("required_uncertainty_evidence",), ()
    if not aggregate.complete:
        return None, None, None, ("confidence_bound_requires_complete_metric",), ()
    selected = (
        uncertainty.lower_confidence_bound
        if rule.threshold_basis is ThresholdBasis.LOWER_CONFIDENCE_BOUND
        else uncertainty.upper_confidence_bound
    )
    return selected, selected, selected, (), uncertainty.evidence_references


def _comparison_case_ids(evidence: QualityEvidence) -> set[str]:
    case_ids = {slot.case_id for slot in evidence.planned_score_slots if slot.applicable}
    if evidence.end_to_end_summary is not None:
        case_ids.update(
            result.slot.case_id for result in evidence.end_to_end_summary.slots
            if result.slot.applicable
        )
    return case_ids


def _comparison_status(
    *, policy: QualityPolicy, candidate: QualityEvidence,
    baseline: QualityEvidence | None, comparison: ComparisonEvidence | None,
    required: bool,
) -> tuple[ComparisonStatus, tuple[str, ...]]:
    if baseline is None and comparison is None:
        return (
            (ComparisonStatus.MISSING, ("comparison_evidence",))
            if required
            else (ComparisonStatus.NOT_REQUESTED, ())
        )
    if baseline is None or comparison is None:
        return ComparisonStatus.MISSING, ("comparison_evidence",)
    if candidate.evaluation_options is None or baseline.evaluation_options is None:
        return ComparisonStatus.MISSING, ("typed_evaluation_options",)

    typed = qualify_comparison(baseline.evaluation_options, candidate.evaluation_options)
    qualification = typed.comparison_qualification
    reasons = list(qualification.reasons)
    if qualification.status != "comparable" or not qualification.ranking_eligible:
        reasons.append(
            "typed comparison qualification is not comparable and ranking eligible"
        )
    if comparison.allowed_differences != policy.allowed_comparison_differences:
        reasons.append("comparison metadata does not match the frozen policy")
    if typed.candidate_identity != candidate.comparison_identity:
        reasons.append("typed candidate identity does not match QualityEvidence")
    if typed.baseline_identity != baseline.comparison_identity:
        reasons.append("typed baseline identity does not match QualityEvidence")
    if comparison.candidate_identity != candidate.comparison_identity:
        reasons.append("candidate ComparisonIdentity does not match comparison evidence")
    if comparison.baseline_identity != baseline.comparison_identity:
        reasons.append("baseline ComparisonIdentity does not match comparison evidence")
    paired = set(comparison.paired_case_ids)
    if paired != _comparison_case_ids(candidate) or paired != _comparison_case_ids(baseline):
        reasons.append("paired_case_ids must exactly match both frozen applicable case sets")
    if reasons:
        return ComparisonStatus.INCOMPARABLE, tuple(dict.fromkeys(reasons))
    return ComparisonStatus.COMPARABLE, ()


def _metric_rule_identity(rule: MetricThresholdRule) -> tuple[object, ...]:
    return (
        rule.metric_id, rule.metric_version, rule.scoring_revision,
        rule.track, rule.execution_profile, rule.scope,
    )


def _slot_metric_identity(slot: ScoreSlot) -> tuple[object, ...]:
    return (
        slot.metric_id, slot.version, slot.scoring_revision,
        slot.track, slot.execution_profile, slot.scope,
    )


def _missing_planned_metric_rules(
    policy: QualityPolicy, evidence: QualityEvidence,
) -> tuple[str, ...]:
    covered = {
        _metric_rule_identity(rule)
        for rule in policy.rules
        if isinstance(rule, MetricThresholdRule)
        and rule.applicable
        and rule.evidence_source is MetricEvidenceSource.SCORE
    }
    missing = {
        _slot_metric_identity(slot)
        for slot in evidence.planned_score_slots
        if slot.applicable and _slot_metric_identity(slot) not in covered
    }
    return tuple(
        "required_threshold_rule:"
        f"{metric_id}@{version}:{scoring_revision}:"
        f"{track.value}:{profile.value}:{scope}"
        for metric_id, version, scoring_revision, track, profile, scope in sorted(
            missing, key=lambda item: tuple(str(part) for part in item)
        )
    )


def _production_qualification_evidence(
    evidence: QualityEvidence,
) -> tuple[QualificationScope, tuple[str, ...]]:
    if evidence.comparison_identity.execution_profile is not ExecutionProfile.PRODUCTION:
        return QualificationScope.LOCAL_POLICY, ()

    missing: list[str] = []
    options = evidence.evaluation_options
    run = evidence.evaluation_run
    if options is None:
        missing.append("production_evaluation_options")
    else:
        preflight = options.preflight()
        if not preflight.accepted:
            missing.append("accepted_production_preflight")
            missing.extend(f"production_preflight:{item.code}" for item in preflight.findings)
        if not preflight.release_evidence_eligible:
            missing.append("release_evidence_eligible_preflight")

    if run is None:
        missing.append("production_evaluation_run")
    else:
        if run.execution_status is not RunExecutionStatus.COMPLETED:
            missing.append("completed_production_run")
        if run.started_at is None or run.completed_at is None:
            missing.append("bounded_production_run_timestamps")
        if not run.artifact_references:
            missing.append("production_run_artifacts")
        if not run.transcript_references:
            missing.append("production_run_transcript")
        attempted_units = {item.unit.primary_key for item in run.attempts}
        if any(unit.primary_key not in attempted_units for unit in run.planned_units):
            missing.append("production_run_attempt_coverage")

    if missing:
        return QualificationScope.LOCAL_POLICY, tuple(dict.fromkeys(missing))
    return QualificationScope.PRODUCTION_QUALIFICATION, ()


def _metric_rule_evaluation(
    *, registry: MetricRegistry, candidate: QualityEvidence,
    baseline: QualityEvidence | None, comparison_status: ComparisonStatus,
    comparison_reasons: tuple[str, ...], comparison_references: tuple[str, ...],
    rule: MetricThresholdRule,
) -> RuleEvaluation:
    if not rule.applicable:
        return RuleEvaluation(
            rule_id=rule.rule_id, rule_version=rule.rule_version, rule_type=rule.rule_type,
            decision=QualificationDecision.NOT_APPLICABLE,
            observed_value=None, possible_min=None, possible_max=None,
            baseline_value=None, delta=None, coverage=None,
            independent_cases=None, paper_families=None, evidence_references=(),
            missing_requirements=(), errors=(),
            reason="rule applicability was frozen as not_applicable",
        )

    aggregate = _aggregate_metric(registry, candidate, rule)
    observed, lower, upper, basis_missing, basis_refs = _threshold_interval(aggregate, candidate, rule)
    missing = list(aggregate.missing_requirements) + list(basis_missing)
    errors = list(aggregate.errors)
    references = set(aggregate.evidence_references) | set(basis_refs)
    if all(value is None for value in (rule.min, rule.max, rule.delta_min, rule.delta_max)):
        missing.append("quality_threshold")
    if rule.min_coverage is None:
        missing.append("min_coverage")
    if rule.min_independent_cases is None:
        missing.append("min_independent_cases")
    if rule.min_paper_families is None:
        missing.append("min_paper_families")

    failures: list[str] = []
    if rule.min is not None and upper is not None and upper < rule.min:
        failures.append(f"maximum possible value {upper} is below min {rule.min}")
    if rule.max is not None and lower is not None and lower > rule.max:
        failures.append(f"minimum possible value {lower} exceeds max {rule.max}")
    if (
        rule.min_coverage is not None and aggregate.complete
        and aggregate.coverage is not None and aggregate.coverage < rule.min_coverage
    ):
        failures.append(f"coverage {aggregate.coverage} is below min_coverage {rule.min_coverage}")
    if (
        rule.min_independent_cases is not None
        and aggregate.planned_cases > 0
        and aggregate.planned_cases < rule.min_independent_cases
    ):
        failures.append(
            f"planned independent cases {aggregate.planned_cases} are below required {rule.min_independent_cases}"
        )
    elif (
        rule.min_independent_cases is not None and aggregate.complete
        and aggregate.independent_cases < rule.min_independent_cases
    ):
        failures.append(
            f"independent cases {aggregate.independent_cases} are below required {rule.min_independent_cases}"
        )
    if rule.min_paper_families is not None and aggregate.planned_paper_families is not None:
        if (
            aggregate.planned_cases > 0
            and aggregate.planned_paper_families < rule.min_paper_families
        ):
            failures.append(
                "planned paper families "
                f"{aggregate.planned_paper_families} are below required "
                f"{rule.min_paper_families}"
            )
        elif (
            aggregate.complete and aggregate.paper_families is not None
            and aggregate.paper_families < rule.min_paper_families
        ):
            failures.append(
                f"paper families {aggregate.paper_families} are below required {rule.min_paper_families}"
            )

    baseline_value: float | None = None
    delta: float | None = None
    if rule.delta_min is not None or rule.delta_max is not None:
        if comparison_status is not ComparisonStatus.COMPARABLE or baseline is None:
            missing.append("comparable_paired_delta")
            errors.extend(comparison_reasons)
        else:
            baseline_aggregate = _aggregate_metric(registry, baseline, rule)
            baseline_value, baseline_lower, baseline_upper, baseline_missing, baseline_refs = _threshold_interval(
                baseline_aggregate, baseline, rule
            )
            missing.extend(f"baseline:{item}" for item in baseline_aggregate.missing_requirements)
            missing.extend(f"baseline:{item}" for item in baseline_missing)
            errors.extend(f"baseline:{item}" for item in baseline_aggregate.errors)
            references.update(baseline_aggregate.evidence_references)
            references.update(baseline_refs)
            references.update(comparison_references)
            if (
                lower is not None and upper is not None
                and baseline_lower is not None and baseline_upper is not None
            ):
                delta_lower = lower - baseline_upper
                delta_upper = upper - baseline_lower
                if observed is not None and baseline_value is not None:
                    delta = observed - baseline_value
                if rule.delta_min is not None and delta_upper < rule.delta_min:
                    failures.append(
                        f"maximum possible candidate-baseline delta {delta_upper} is below delta_min {rule.delta_min}"
                    )
                if rule.delta_max is not None and delta_lower > rule.delta_max:
                    failures.append(
                        f"minimum possible candidate-baseline delta {delta_lower} exceeds delta_max {rule.delta_max}"
                    )
            else:
                missing.append("delta_measurement")

    if failures:
        decision = QualificationDecision.FAIL
        reason = "; ".join(failures)
    elif errors or missing or not aggregate.complete:
        decision = QualificationDecision.INCONCLUSIVE
        reason = "mandatory metric evidence or approved rule configuration is incomplete"
    else:
        decision = QualificationDecision.PASS
        reason = "all configured deterministic metric constraints are satisfied"
    return RuleEvaluation(
        rule_id=rule.rule_id, rule_version=rule.rule_version, rule_type=rule.rule_type,
        decision=decision, observed_value=observed, possible_min=lower, possible_max=upper,
        baseline_value=baseline_value, delta=delta, coverage=aggregate.coverage,
        independent_cases=aggregate.independent_cases, paper_families=aggregate.paper_families,
        evidence_references=tuple(sorted(references)),
        missing_requirements=tuple(dict.fromkeys(missing)),
        errors=tuple(dict.fromkeys(errors)), reason=reason,
    )


def _stage_rule_evaluation(evidence: QualityEvidence, rule: StageRequirementRule) -> RuleEvaluation:
    if not rule.applicable:
        decision = QualificationDecision.NOT_APPLICABLE
        reason = "stage applicability was frozen as not_applicable"
        missing: tuple[str, ...] = ()
        errors: tuple[str, ...] = ()
        references: tuple[str, ...] = ()
    elif (
        rule.track is not evidence.comparison_identity.track
        or rule.execution_profile is not evidence.comparison_identity.execution_profile
    ):
        decision = QualificationDecision.INCONCLUSIVE
        reason = "stage rule identity does not match the evaluated run"
        missing, errors, references = (), ("stage rule identity mismatch",), ()
    else:
        stage = next((item for item in evidence.stages if item.stage_id == rule.stage_id), None)
        if stage is None:
            decision = QualificationDecision.INCONCLUSIVE
            reason = "mandatory stage evidence is missing"
            missing, errors, references = ("required_stage_evidence",), (), ()
        elif stage.status is StageEvidenceStatus.COMPLETED:
            decision = QualificationDecision.PASS
            reason = "mandatory stage completed with retained evidence"
            missing, errors, references = (), (), stage.evidence_references
        elif stage.status is StageEvidenceStatus.FAILED:
            decision = QualificationDecision.FAIL
            reason = f"mandatory stage failed: {stage.reason}"
            missing, errors, references = (), (stage.reason or "mandatory stage failed",), stage.evidence_references
        else:
            decision = QualificationDecision.INCONCLUSIVE
            reason = f"mandatory stage did not execute: {stage.reason}"
            missing, errors, references = ("required_stage_not_completed",), (), stage.evidence_references
    return RuleEvaluation(
        rule_id=rule.rule_id, rule_version=rule.rule_version, rule_type=rule.rule_type,
        decision=decision, observed_value=None, possible_min=None, possible_max=None,
        baseline_value=None, delta=None, coverage=None, independent_cases=None,
        paper_families=None, evidence_references=references,
        missing_requirements=missing, errors=errors, reason=reason,
    )


def _review_rule_evaluation(evidence: QualityEvidence, rule: ReviewCoverageRule) -> RuleEvaluation:
    if not rule.applicable:
        return RuleEvaluation(
            rule_id=rule.rule_id, rule_version=rule.rule_version, rule_type=rule.rule_type,
            decision=QualificationDecision.NOT_APPLICABLE,
            observed_value=None, possible_min=None, possible_max=None, baseline_value=None,
            delta=None, coverage=None, independent_cases=None, paper_families=None,
            evidence_references=(), missing_requirements=(), errors=(),
            reason="review applicability was frozen as not_applicable",
        )
    review = next((
        item for item in evidence.review_coverage
        if item.review_plan_id == rule.review_plan_id
        and item.review_plan_version == rule.review_plan_version
        and item.track is rule.track
        and item.execution_profile is rule.execution_profile
        and item.scope == rule.scope
    ), None)
    if review is None:
        return RuleEvaluation(
            rule_id=rule.rule_id, rule_version=rule.rule_version, rule_type=rule.rule_type,
            decision=QualificationDecision.INCONCLUSIVE,
            observed_value=None, possible_min=None, possible_max=None, baseline_value=None,
            delta=None, coverage=None, independent_cases=None, paper_families=None,
            evidence_references=(), missing_requirements=("review_coverage_evidence",),
            errors=(), reason="mandatory review coverage evidence is missing",
        )
    missing: list[str] = []
    if rule.min_coverage is None:
        missing.append("review_min_coverage")
    if rule.min_reviewed_cases is None:
        missing.append("min_reviewed_cases")
    if rule.min_paper_families is None:
        missing.append("review_min_paper_families")
    if review.has_unknowns:
        missing.append("review_pending_or_error")

    family_by_case = {item.case_id: item.paper_family_id for item in evidence.case_paper_families}
    missing_family_cases = set(review.planned_case_ids) - set(family_by_case)
    families = None
    planned_families = None
    if missing_family_cases:
        missing.append("paper_family_mapping")
    else:
        families = len({family_by_case[item] for item in review.reviewed_case_ids})
        planned_families = len({family_by_case[item] for item in review.planned_case_ids})

    failures: list[str] = []
    if rule.min_coverage is not None and not review.has_unknowns and review.coverage < rule.min_coverage:
        failures.append(f"review coverage {review.coverage} is below required {rule.min_coverage}")
    if rule.min_reviewed_cases is not None:
        if len(review.planned_case_ids) < rule.min_reviewed_cases:
            failures.append("review plan contains fewer independent cases than required")
        elif not review.has_unknowns and len(review.reviewed_case_ids) < rule.min_reviewed_cases:
            failures.append("reviewed case count is below the approved minimum")
    if rule.min_paper_families is not None and planned_families is not None:
        if planned_families < rule.min_paper_families:
            failures.append("review plan contains fewer paper families than required")
        elif not review.has_unknowns and families is not None and families < rule.min_paper_families:
            failures.append("reviewed paper-family count is below the approved minimum")

    if failures:
        decision, reason = QualificationDecision.FAIL, "; ".join(failures)
    elif missing:
        decision = QualificationDecision.INCONCLUSIVE
        reason = "mandatory review coverage is incomplete or not fully configured"
    else:
        decision = QualificationDecision.PASS
        reason = "approved review coverage requirements are satisfied"
    return RuleEvaluation(
        rule_id=rule.rule_id, rule_version=rule.rule_version, rule_type=rule.rule_type,
        decision=decision, observed_value=review.coverage,
        possible_min=review.coverage, possible_max=review.coverage,
        baseline_value=None, delta=None, coverage=review.coverage,
        independent_cases=len(review.reviewed_case_ids), paper_families=families,
        evidence_references=review.evidence_references,
        missing_requirements=tuple(dict.fromkeys(missing)), errors=(), reason=reason,
    )


def evaluate_quality_policy(
    *, policy: QualityPolicy, registry: MetricRegistry, candidate: QualityEvidence,
    baseline: QualityEvidence | None = None, comparison: ComparisonEvidence | None = None,
) -> QualityGateResult:
    """Evaluate policy evidence without executing, ranking, or publishing anything."""
    delta_required = any(
        isinstance(rule, MetricThresholdRule) and rule.applicable
        and (rule.delta_min is not None or rule.delta_max is not None)
        for rule in policy.rules
    )
    comparison_status, comparison_reasons = _comparison_status(
        policy=policy, candidate=candidate, baseline=baseline,
        comparison=comparison, required=delta_required,
    )

    evaluations: list[RuleEvaluation] = []
    for rule in policy.rules:
        if isinstance(rule, MetricThresholdRule):
            evaluations.append(_metric_rule_evaluation(
                registry=registry, candidate=candidate, baseline=baseline,
                comparison_status=comparison_status,
                comparison_reasons=comparison_reasons,
                comparison_references=(comparison.evidence_references if comparison is not None else ()),
                rule=rule,
            ))
        elif isinstance(rule, StageRequirementRule):
            evaluations.append(_stage_rule_evaluation(candidate, rule))
        else:
            evaluations.append(_review_rule_evaluation(candidate, rule))

    missing: list[str] = []
    errors: list[str] = []
    if policy.approval is None:
        missing.append("human_policy_approval")
    if not policy.rules:
        missing.append("quality_policy_rules")
    missing.extend(_missing_planned_metric_rules(policy, candidate))
    if comparison_status in {ComparisonStatus.MISSING, ComparisonStatus.INCOMPARABLE}:
        missing.append("comparable_comparison_evidence")
        errors.extend(comparison_reasons)
    if candidate.execution_status is not RunExecutionStatus.COMPLETED:
        missing.append("completed_execution_status")

    decisions = tuple(item.decision for item in evaluations)
    if QualificationDecision.FAIL in decisions:
        decision = QualificationDecision.FAIL
    elif missing or errors or QualificationDecision.INCONCLUSIVE in decisions or not decisions:
        decision = QualificationDecision.INCONCLUSIVE
    elif all(item is QualificationDecision.NOT_APPLICABLE for item in decisions):
        decision = QualificationDecision.NOT_APPLICABLE
    else:
        decision = QualificationDecision.PASS

    qualification_scope, production_missing = _production_qualification_evidence(candidate)

    return QualityGateResult(
        record_type="quality_gate_result", schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION, policy_id=policy.policy_id,
        policy_version=policy.policy_version, policy_digest=policy.policy_digest,
        execution_status=candidate.execution_status, decision=decision,
        comparison_status=comparison_status,
        execution_profile=candidate.comparison_identity.execution_profile,
        qualification_scope=qualification_scope,
        production_qualification_eligible=(
            decision is QualificationDecision.PASS
            and qualification_scope is QualificationScope.PRODUCTION_QUALIFICATION
        ),
        production_qualification_missing_requirements=production_missing,
        rule_evaluations=tuple(evaluations),
        missing_requirements=tuple(dict.fromkeys(missing)),
        errors=tuple(dict.fromkeys(errors)), side_effects_performed=False,
    )


__all__ = [
    "CasePaperFamily", "ComparisonEvidence", "ComparisonField", "ComparisonStatus",
    "HumanApprovalReference", "MetricEvidenceSource", "MetricThresholdRule",
    "MetricUncertaintyEvidence", "QualificationScope", "QualityEvidence", "QualityGateResult", "QualityPolicy",
    "ReviewCoverageEvidence", "ReviewCoverageRule", "RuleEvaluation", "StageEvidence",
    "StageEvidenceStatus", "StageRequirementRule", "ThresholdBasis",
    "evaluate_quality_policy",
]


