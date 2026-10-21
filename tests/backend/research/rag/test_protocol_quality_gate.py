from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION, SCHEMA_VERSION, EvaluationTrack, ExecutionProfile, content_digest,
)
from backend.research.rag.evaluation.protocol_execution import (
    ComparisonIdentity, QualificationDecision, RunExecutionStatus,
)
from backend.research.rag.evaluation.protocol_quality_gate import (
    CasePaperFamily, ComparisonEvidence, ComparisonField, ComparisonStatus,
    HumanApprovalReference, MetricEvidenceSource, MetricThresholdRule,
    QualificationScope, QualityEvidence, QualityPolicy, ReviewCoverageEvidence, ReviewCoverageRule,
    StageEvidence, StageEvidenceStatus, StageRequirementRule, ThresholdBasis,
    evaluate_quality_policy,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EndToEndExecutionState, EndToEndObservation, EndToEndOutcome, EndToEndSlot,
    EvaluationScore, MetricAggregation, MetricDefinition, MetricDirection,
    MetricGranularity, MetricRegistry, MetricValueKind, MissingValuePolicy,
    NumericDomain, ScoreSlot, ScoreSource, ScoreStatus, summarize_end_to_end,
)

D1 = "1" * 64
D2 = "2" * 64
D3 = "3" * 64
D4 = "4" * 64
D5 = "5" * 64
D6 = "6" * 64
D7 = "7" * 64
D8 = "8" * 64
NOW = datetime(2026, 10, 14, tzinfo=UTC)


def _definition(
    metric_id: str = "answer.quality",
    *,
    direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER,
    aggregation: MetricAggregation = MetricAggregation.MEAN,
    kind: MetricValueKind = MetricValueKind.SCALAR,
    maximum: float = 1.0,
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        version="1.0",
        required_inputs=frozenset({"answer"}),
        applicable_tracks=frozenset({EvaluationTrack.END_TO_END}),
        applicable_execution_profiles=frozenset(
            {ExecutionProfile.PRODUCTION, ExecutionProfile.FIXTURE}
        ),
        applicable_scopes=frozenset({"full_task"}),
        granularity=MetricGranularity.ATTEMPT,
        direction=direction,
        domain=NumericDomain(minimum=0.0, maximum=maximum),
        value_kind=kind,
        aggregation=aggregation,
        missing_policy=MissingValuePolicy.ERROR,
    )


def _registry(*definitions: MetricDefinition) -> MetricRegistry:
    return MetricRegistry(definitions or (_definition(),))


def _identity(
    *,
    profile: ExecutionProfile = ExecutionProfile.PRODUCTION,
    variant: str = D2,
    corpus: str = D3,
    code: str = D5,
) -> ComparisonIdentity:
    return ComparisonIdentity.create(
        dataset_digest=D1,
        case_scope_fingerprint=D4,
        corpus_content_sha256=corpus,
        track=EvaluationTrack.END_TO_END,
        execution_profile=profile,
        variant_fingerprint=variant,
        code_fingerprint=code,
        environment_fingerprint=D6,
        budget_fingerprint=D7,
        initial_state_fingerprint=D8,
        namespace_fingerprint="9" * 64,
        repetition_plan=(1,),
        timing_boundary="request plus retries; scoring excluded",
    )


def _score(
    case_number: int,
    *,
    metric_id: str = "answer.quality",
    value: float | None = 0.8,
    status: ScoreStatus = ScoreStatus.OK,
    profile: ExecutionProfile = ExecutionProfile.PRODUCTION,
    variant: str = D2,
    reason: str | None = None,
) -> EvaluationScore:
    provenance: dict[str, Any] = {
        "source": "deterministic-test-fixture",
        "input_fingerprint": D6,
    }
    return EvaluationScore(
        record_type="score",
        schema_version=SCHEMA_VERSION,
        protocol_version=PROTOCOL_VERSION,
        dataset_id="research-dev",
        dataset_version="dataset-v1",
        dataset_digest=D1,
        run_id=f"run-{variant[:4]}",
        case_id=f"case-{case_number}",
        attempt_id=f"attempt-{case_number}",
        track=EvaluationTrack.END_TO_END,
        execution_profile=profile,
        variant_fingerprint=variant,
        repetition=1,
        scope="full_task",
        scoring_revision="score-v1",
        metric_id=metric_id,
        version="1.0",
        value=value,
        status=status,
        numerator=None,
        denominator=None,
        source=ScoreSource.RULE,
        provenance=provenance,
        provenance_digest=content_digest(provenance),
        evidence=(f"artifact://scores/{metric_id}/{case_number}",),
        reason=reason,
    )


def _slot(score: EvaluationScore, *, applicable: bool = True) -> ScoreSlot:
    return ScoreSlot.from_score(score, applicable=applicable)


def _families(*case_numbers: int) -> tuple[CasePaperFamily, ...]:
    return tuple(
        CasePaperFamily(case_id=f"case-{number}", paper_family_id=f"family-{number}")
        for number in case_numbers
    )


def _evidence(
    *,
    scores: tuple[EvaluationScore, ...] = (),
    slots: tuple[ScoreSlot, ...] = (),
    profile: ExecutionProfile = ExecutionProfile.PRODUCTION,
    variant: str = D2,
    corpus: str = D3,
    execution_status: RunExecutionStatus = RunExecutionStatus.COMPLETED,
    end_to_end_summary=None,
    stages: tuple[StageEvidence, ...] = (),
    review_coverage: tuple[ReviewCoverageEvidence, ...] = (),
    families: tuple[CasePaperFamily, ...] = (),
) -> QualityEvidence:
    return QualityEvidence(
        execution_status=execution_status,
        comparison_identity=_identity(profile=profile, variant=variant, corpus=corpus),
        planned_score_slots=slots,
        scores=scores,
        end_to_end_summary=end_to_end_summary,
        stages=stages,
        review_coverage=review_coverage,
        case_paper_families=families,
        uncertainty=(),
    )


def _approval(policy_digest: str) -> HumanApprovalReference:
    return HumanApprovalReference(
        policy_id="release-quality",
        policy_version="2026-10-14",
        scoring_revision="score-v1",
        reviewer_id="quality-owner-17",
        reviewer_kind="human",
        approved_at=NOW,
        approval_reference="artifact://approvals/release-quality/2026-10-14",
        approval_content_sha256=D8,
        approved_policy_digest=policy_digest,
    )


def _policy(
    *rules,
    approval: bool = True,
    allowed: frozenset[ComparisonField] = frozenset(),
) -> QualityPolicy:
    content = {
        "record_type": "quality_policy",
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "policy_id": "release-quality",
        "policy_version": "2026-10-14",
        "scoring_revision": "score-v1",
        "allowed_comparison_differences": allowed,
        "rules": tuple(rules),
    }
    policy_digest = QualityPolicy.calculate_policy_digest(**content)
    return QualityPolicy.create(
        **content, approval=_approval(policy_digest) if approval else None
    )


def _metric_rule(
    *,
    metric_id: str = "answer.quality",
    profile: ExecutionProfile = ExecutionProfile.PRODUCTION,
    direction: MetricDirection = MetricDirection.HIGHER_IS_BETTER,
    aggregation: MetricAggregation = MetricAggregation.MEAN,
    source: MetricEvidenceSource = MetricEvidenceSource.SCORE,
    applicable: bool = True,
    minimum: float | None = 0.7,
    maximum: float | None = None,
    delta_min: float | None = None,
    delta_max: float | None = None,
    min_coverage: float | None = 1.0,
    min_cases: int | None = 1,
    min_families: int | None = 1,
) -> MetricThresholdRule:
    return MetricThresholdRule(
        rule_id=f"{metric_id}-rule",
        rule_version="1.0",
        applicable=applicable,
        metric_id=metric_id,
        metric_version="1.0",
        scoring_revision="score-v1",
        track=EvaluationTrack.END_TO_END,
        execution_profile=profile,
        scope="full_task",
        evidence_source=source,
        aggregation=aggregation,
        direction=direction,
        threshold_basis=ThresholdBasis.POINT_ESTIMATE,
        min=minimum,
        max=maximum,
        delta_min=delta_min,
        delta_max=delta_max,
        min_coverage=min_coverage,
        min_independent_cases=min_cases,
        min_paper_families=min_families,
    )


def _end_to_end_summary(
    outcomes: tuple[EndToEndOutcome, ...],
    *,
    profile: ExecutionProfile = ExecutionProfile.PRODUCTION,
    variant: str = D2,
):
    slots = tuple(
        EndToEndSlot(
            dataset_id="research-dev",
            dataset_version="dataset-v1",
            dataset_digest=D1,
            run_id="run-e2e",
            case_id=f"case-{index}",
            attempt_id=f"attempt-{index}",
            track=EvaluationTrack.END_TO_END,
            execution_profile=profile,
            variant_fingerprint=variant,
            repetition=1,
            scope="full_task",
            applicable=True,
        )
        for index in range(1, len(outcomes) + 1)
    )
    observations = []
    for slot, outcome in zip(slots, outcomes, strict=True):
        if outcome is EndToEndOutcome.KNOWN_SUCCESS:
            state = EndToEndExecutionState.MODEL_PATH_COMPLETED
            judge_error = False
            failure_type = reason = None
        elif outcome is EndToEndOutcome.KNOWN_FAILURE:
            state = EndToEndExecutionState.RUNTIME_FAILED
            judge_error = False
            failure_type = "generation_timeout"
            reason = "the model path timed out"
        else:
            state = EndToEndExecutionState.MODEL_PATH_COMPLETED
            judge_error = True
            failure_type = "judge_timeout"
            reason = "required judge timed out"
        observations.append(
            EndToEndObservation(
                **{
                    name: getattr(slot, name)
                    for name in EndToEndSlot.model_fields
                    if name != "applicable"
                },
                outcome=outcome,
                execution_state=state,
                required_judge_error=judge_error,
                failure_type=failure_type,
                reason=reason,
            )
        )
    return summarize_end_to_end(slots, tuple(observations))


def test_versioned_policy_requires_exact_real_human_approval_reference() -> None:
    policy = _policy(_metric_rule())
    assert policy.policy_digest == QualityPolicy.calculate_policy_digest(
        **policy.model_dump(mode="json")
    )
    assert policy.approval is not None
    assert policy.approval.approved_policy_digest == policy.policy_digest
    assert QualityPolicy.model_validate_json(policy.model_dump_json()) == policy

    with pytest.raises(ValidationError, match="exact policy content"):
        QualityPolicy.create(
            **{
                **policy.model_dump(exclude={"policy_digest", "approval"}),
                "approval": policy.approval.model_copy(update={"policy_version": "other-version"}),
            }
        )
    assert policy.approval is not None
    changed_rule = policy.rules[0].model_copy(update={"min": 0.95})
    with pytest.raises(ValidationError, match="exact policy content"):
        QualityPolicy.create(
            **{
                **policy.model_dump(exclude={"policy_digest", "approval", "rules"}),
                "rules": (changed_rule,),
                "approval": policy.approval,
            }
        )

    with pytest.raises(ValidationError, match="timezone-aware"):
        HumanApprovalReference(
            **{
                **policy.approval.model_dump(),
                "approved_at": datetime(2026, 10, 14),
            }
        )


def test_empty_policy_and_missing_human_approval_cannot_pass() -> None:
    evidence = _evidence()
    empty = evaluate_quality_policy(
        policy=_policy(approval=False), registry=_registry(), candidate=evidence
    )
    assert empty.decision is QualificationDecision.INCONCLUSIVE
    assert set(empty.missing_requirements) == {
        "human_policy_approval", "quality_policy_rules"
    }

    score = _score(1)
    no_approval = evaluate_quality_policy(
        policy=_policy(_metric_rule(), approval=False),
        registry=_registry(),
        candidate=_evidence(scores=(score,), slots=(_slot(score),), families=_families(1)),
    )
    assert no_approval.decision is QualificationDecision.INCONCLUSIVE
    assert "human_policy_approval" in no_approval.missing_requirements


def test_complete_metric_stage_and_review_rules_pass_with_rule_level_evidence() -> None:
    scores = (_score(1, value=0.8), _score(2, value=0.9))
    stage = StageEvidence(
        stage_id="live-answer",
        status=StageEvidenceStatus.COMPLETED,
        evidence_references=("artifact://runs/live-answer",),
        reason=None,
    )
    review = ReviewCoverageEvidence(
        review_plan_id="human-review",
        review_plan_version="1.0",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.PRODUCTION,
        scope="full_task",
        planned_case_ids=("case-1", "case-2"),
        reviewed_case_ids=("case-1", "case-2"),
        not_reviewed_case_ids=(),
        pending_case_ids=(),
        error_case_ids=(),
        reviewer_ids=("reviewer-1",),
        reviewer_kind="human",
        evidence_references=("artifact://reviews/human-review",),
    )
    policy = _policy(
        _metric_rule(minimum=0.8, min_cases=2, min_families=2),
        StageRequirementRule(
            rule_id="live-stage", rule_version="1.0", applicable=True,
            stage_id="live-answer", track=EvaluationTrack.END_TO_END,
            execution_profile=ExecutionProfile.PRODUCTION,
        ),
        ReviewCoverageRule(
            rule_id="review-coverage", rule_version="1.0", applicable=True,
            review_plan_id="human-review", review_plan_version="1.0",
            track=EvaluationTrack.END_TO_END,
            execution_profile=ExecutionProfile.PRODUCTION,
            scope="full_task", min_coverage=1.0,
            min_reviewed_cases=2, min_paper_families=2,
        ),
    )
    result = evaluate_quality_policy(
        policy=policy,
        registry=_registry(),
        candidate=_evidence(
            scores=scores,
            slots=tuple(_slot(score) for score in scores),
            stages=(stage,), review_coverage=(review,), families=_families(1, 2),
        ),
    )
    assert result.decision is QualificationDecision.PASS
    assert result.execution_status is RunExecutionStatus.COMPLETED
    assert result.production_qualification_eligible is False
    assert result.qualification_scope is QualificationScope.LOCAL_POLICY
    assert set(result.production_qualification_missing_requirements) == {
        "production_evaluation_options", "production_evaluation_run",
    }
    assert result.side_effects_performed is False
    assert {item.decision for item in result.rule_evaluations} == {QualificationDecision.PASS}
    assert result.rule_evaluations[0].observed_value == pytest.approx(0.85)
    assert result.rule_evaluations[0].evidence_references


def test_fixture_can_pass_contract_gate_but_never_production_qualification() -> None:
    score = _score(1, profile=ExecutionProfile.FIXTURE)
    result = evaluate_quality_policy(
        policy=_policy(_metric_rule(profile=ExecutionProfile.FIXTURE)),
        registry=_registry(),
        candidate=_evidence(
            scores=(score,), slots=(_slot(score),),
            profile=ExecutionProfile.FIXTURE, families=_families(1),
        ),
    )
    assert result.decision is QualificationDecision.PASS
    assert result.production_qualification_eligible is False
    assert result.side_effects_performed is False


def test_required_metric_error_or_missing_planned_slot_is_inconclusive() -> None:
    valid = _score(1, value=0.8)
    error = _score(
        2, value=None, status=ScoreStatus.ERROR,
        reason="judge output did not satisfy the schema",
    )
    result = evaluate_quality_policy(
        policy=_policy(_metric_rule(minimum=0.7, min_cases=2, min_families=2)),
        registry=_registry(),
        candidate=_evidence(
            scores=(valid, error), slots=(_slot(valid), _slot(error)),
            families=_families(1, 2),
        ),
    )
    rule = result.rule_evaluations[0]
    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert rule.decision is QualificationDecision.INCONCLUSIVE
    assert rule.coverage == 0.5
    assert "required_metric_error_or_missing" in rule.missing_requirements

    missing_result = evaluate_quality_policy(
        policy=_policy(_metric_rule(minimum=0.7, min_cases=2, min_families=2)),
        registry=_registry(),
        candidate=_evidence(
            scores=(valid,), slots=(_slot(valid), _slot(error)),
            families=_families(1, 2),
        ),
    )
    assert missing_result.decision is QualificationDecision.INCONCLUSIVE
    assert missing_result.rule_evaluations[0].coverage == 0.5


def test_known_runtime_failures_remain_complete_denominator_and_fail_quality() -> None:
    summary = _end_to_end_summary(
        (EndToEndOutcome.KNOWN_FAILURE, EndToEndOutcome.KNOWN_FAILURE)
    )
    registry = _registry(
        _definition(
            "end_to_end_success_rate",
            aggregation=MetricAggregation.RATIO,
            kind=MetricValueKind.RATIO,
        )
    )
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(
                metric_id="end_to_end_success_rate",
                source=MetricEvidenceSource.END_TO_END,
                aggregation=MetricAggregation.RATIO,
                minimum=0.5,
                min_cases=2,
                min_families=2,
            )
        ),
        registry=registry,
        candidate=_evidence(
            execution_status=RunExecutionStatus.FAILED,
            end_to_end_summary=summary,
            families=_families(1, 2),
        ),
    )
    rule = result.rule_evaluations[0]
    assert result.execution_status is RunExecutionStatus.FAILED
    assert result.decision is QualificationDecision.FAIL
    assert rule.coverage == 1.0
    assert rule.observed_value == 0.0
    assert rule.independent_cases == 2


def test_judge_unknown_stays_inconclusive_when_no_deterministic_failure_exists() -> None:
    summary = _end_to_end_summary(
        (EndToEndOutcome.KNOWN_SUCCESS, EndToEndOutcome.UNKNOWN)
    )
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(
                metric_id="end_to_end_success_rate",
                source=MetricEvidenceSource.END_TO_END,
                aggregation=MetricAggregation.RATIO,
                minimum=0.4,
                min_cases=2,
                min_families=2,
            )
        ),
        registry=_registry(
            _definition(
                "end_to_end_success_rate",
                aggregation=MetricAggregation.RATIO,
                kind=MetricValueKind.RATIO,
            )
        ),
        candidate=_evidence(end_to_end_summary=summary, families=_families(1, 2)),
    )
    rule = result.rule_evaluations[0]
    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert rule.possible_min == 0.5
    assert rule.possible_max == 1.0
    assert "required_end_to_end_outcome_unknown" in rule.missing_requirements


def test_known_cost_upper_bound_failure_dominates_another_unknown_metric() -> None:
    cost = _score(1, metric_id="usage.cost", value=120.0)
    unknown = _score(
        1, metric_id="answer.quality", value=None, status=ScoreStatus.ERROR,
        reason="required judge unavailable",
    )
    cost_definition = _definition(
        "usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
        aggregation=MetricAggregation.SUM, maximum=1000.0,
    )
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(
                metric_id="usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
                aggregation=MetricAggregation.SUM, minimum=None, maximum=100.0,
            ),
        ),
        registry=_registry(cost_definition, _definition()),
        candidate=_evidence(
            scores=(cost, unknown), slots=(_slot(cost), _slot(unknown)),
            families=_families(1),
        ),
    )
    assert result.decision is QualificationDecision.FAIL
    assert result.rule_evaluations[0].decision is QualificationDecision.FAIL
    assert "exceeds max" in result.rule_evaluations[0].reason
    assert any(
        item.startswith("required_threshold_rule:answer.quality@1.0")
        for item in result.missing_requirements
    )


def test_uncovered_applicable_planned_metric_is_explicitly_inconclusive() -> None:
    cost = _score(1, metric_id="usage.cost", value=80.0)
    unknown = _score(
        1, metric_id="answer.quality", value=None, status=ScoreStatus.ERROR,
        reason="required judge unavailable",
    )
    cost_definition = _definition(
        "usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
        aggregation=MetricAggregation.SUM, maximum=1000.0,
    )
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(
                metric_id="usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
                aggregation=MetricAggregation.SUM, minimum=None, maximum=100.0,
            ),
        ),
        registry=_registry(cost_definition, _definition()),
        candidate=_evidence(
            scores=(cost, unknown), slots=(_slot(cost), _slot(unknown)),
            families=_families(1),
        ),
    )

    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert result.rule_evaluations[0].observed_value == 80.0
    assert any(
        item.startswith("required_threshold_rule:answer.quality@1.0")
        for item in result.missing_requirements
    )
    assert unknown.value is None


def test_explicit_frozen_not_applicable_slot_does_not_require_a_threshold_rule() -> None:
    cost = _score(1, metric_id="usage.cost", value=80.0)
    not_applicable = _score(
        1, metric_id="answer.quality", value=None, status=ScoreStatus.NOT_APPLICABLE,
        reason="the frozen case plan excludes answer judging",
    )
    cost_definition = _definition(
        "usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
        aggregation=MetricAggregation.SUM, maximum=1000.0,
    )
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(
                metric_id="usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
                aggregation=MetricAggregation.SUM, minimum=None, maximum=100.0,
            ),
        ),
        registry=_registry(cost_definition, _definition()),
        candidate=_evidence(
            scores=(cost, not_applicable),
            slots=(_slot(cost), _slot(not_applicable, applicable=False)),
            families=_families(1),
        ),
    )

    assert result.decision is QualificationDecision.PASS
    assert not any(
        item.startswith("required_threshold_rule:answer.quality@1.0")
        for item in result.missing_requirements
    )


def test_runtime_not_applicable_cannot_downgrade_an_applicable_frozen_slot() -> None:
    score = _score(
        1, value=None, status=ScoreStatus.NOT_APPLICABLE,
        reason="runtime attempted to remove the judge denominator",
    )
    with pytest.raises(ValidationError, match="applicable planned score slot"):
        _evidence(scores=(score,), slots=(_slot(score, applicable=True),), families=_families(1))


def test_delta_requires_typed_comparable_paired_evidence_and_respects_lower_better_direction() -> None:
    baseline_score = _score(1, metric_id="usage.cost", value=100.0, variant=D3)
    candidate_score = _score(1, metric_id="usage.cost", value=105.0, variant=D2)
    definition = _definition(
        "usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
        aggregation=MetricAggregation.SUM, maximum=1000.0,
    )
    rule = _metric_rule(
        metric_id="usage.cost", direction=MetricDirection.LOWER_IS_BETTER,
        aggregation=MetricAggregation.SUM, minimum=None, maximum=200.0,
        delta_max=10.0,
    )
    policy = _policy(rule, allowed=frozenset({ComparisonField.VARIANT_FINGERPRINT}))
    candidate = _evidence(
        scores=(candidate_score,), slots=(_slot(candidate_score),),
        variant=D2, families=_families(1),
    )
    baseline = _evidence(
        scores=(baseline_score,), slots=(_slot(baseline_score),),
        variant=D3, families=_families(1),
    )
    comparison = ComparisonEvidence(
        baseline_identity=baseline.comparison_identity,
        candidate_identity=candidate.comparison_identity,
        allowed_differences=frozenset({ComparisonField.VARIANT_FINGERPRINT}),
        paired_case_ids=("case-1",),
        evidence_references=("artifact://comparisons/cost",),
    )
    passed = evaluate_quality_policy(
        policy=policy, registry=_registry(definition), candidate=candidate,
        baseline=baseline, comparison=comparison,
    )
    assert passed.decision is QualificationDecision.INCONCLUSIVE
    assert passed.comparison_status is ComparisonStatus.MISSING
    assert passed.rule_evaluations[0].delta is None
    assert "typed_evaluation_options" in passed.errors

    missing = evaluate_quality_policy(
        policy=policy, registry=_registry(definition), candidate=candidate
    )
    assert missing.decision is QualificationDecision.INCONCLUSIVE
    assert missing.comparison_status is ComparisonStatus.MISSING

    regressed_score = _score(1, metric_id="usage.cost", value=115.0, variant=D2)
    regressed_candidate = _evidence(
        scores=(regressed_score,), slots=(_slot(regressed_score),),
        variant=D2, families=_families(1),
    )
    regressed_comparison = comparison.model_copy(
        update={"candidate_identity": regressed_candidate.comparison_identity}
    )
    failed = evaluate_quality_policy(
        policy=policy, registry=_registry(definition), candidate=regressed_candidate,
        baseline=baseline, comparison=regressed_comparison,
    )
    assert failed.decision is QualificationDecision.INCONCLUSIVE
    assert failed.comparison_status is ComparisonStatus.MISSING


def test_incomparable_core_identity_blocks_delta_and_pass() -> None:
    baseline_score = _score(1, variant=D3)
    candidate_score = _score(1, variant=D2)
    policy = _policy(
        _metric_rule(delta_min=-0.1),
        allowed=frozenset({ComparisonField.VARIANT_FINGERPRINT}),
    )
    candidate = _evidence(
        scores=(candidate_score,), slots=(_slot(candidate_score),),
        variant=D2, families=_families(1),
    )
    baseline = _evidence(
        scores=(baseline_score,), slots=(_slot(baseline_score),),
        variant=D3, corpus=D7, families=_families(1),
    )
    comparison = ComparisonEvidence(
        baseline_identity=baseline.comparison_identity,
        candidate_identity=candidate.comparison_identity,
        allowed_differences=frozenset({ComparisonField.VARIANT_FINGERPRINT}),
        paired_case_ids=("case-1",),
        evidence_references=("artifact://comparisons/incomparable",),
    )
    result = evaluate_quality_policy(
        policy=policy, registry=_registry(), candidate=candidate,
        baseline=baseline, comparison=comparison,
    )
    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert result.comparison_status is ComparisonStatus.MISSING
    assert "typed_evaluation_options" in result.errors
    assert result.production_qualification_eligible is False


def test_unknown_threshold_or_coverage_configuration_is_not_a_pass() -> None:
    score = _score(1, value=0.99)
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(
                minimum=None, maximum=None, delta_min=None, delta_max=None,
                min_coverage=None, min_cases=None, min_families=None,
            )
        ),
        registry=_registry(),
        candidate=_evidence(
            scores=(score,), slots=(_slot(score),), families=_families(1),
        ),
    )
    rule = result.rule_evaluations[0]
    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert {
        "quality_threshold", "min_coverage", "min_independent_cases", "min_paper_families"
    }.issubset(rule.missing_requirements)


def test_frozen_not_applicable_rule_is_distinct_but_na_cannot_hide_mandatory_metric() -> None:
    na_score = _score(
        1, value=None, status=ScoreStatus.NOT_APPLICABLE,
        reason="frozen case applicability excludes this metric",
    )
    mandatory = evaluate_quality_policy(
        policy=_policy(_metric_rule(applicable=True)),
        registry=_registry(),
        candidate=_evidence(
            scores=(na_score,), slots=(_slot(na_score, applicable=False),),
            families=_families(1),
        ),
    )
    assert mandatory.decision is QualificationDecision.INCONCLUSIVE
    assert "mandatory_metric_has_no_applicable_slot" in mandatory.rule_evaluations[0].missing_requirements

    truly_na = evaluate_quality_policy(
        policy=_policy(_metric_rule(applicable=False)),
        registry=_registry(),
        candidate=_evidence(),
    )
    assert truly_na.decision is QualificationDecision.NOT_APPLICABLE
    assert truly_na.production_qualification_eligible is False


def test_skipped_stage_and_pending_review_cannot_pass() -> None:
    score = _score(1)
    stage = StageEvidence(
        stage_id="live-answer",
        status=StageEvidenceStatus.SKIPPED,
        evidence_references=(),
        reason="credentials were unavailable",
    )
    review = ReviewCoverageEvidence(
        review_plan_id="human-review",
        review_plan_version="1.0",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.PRODUCTION,
        scope="full_task",
        planned_case_ids=("case-1",),
        reviewed_case_ids=(),
        not_reviewed_case_ids=(),
        pending_case_ids=("case-1",),
        error_case_ids=(),
        reviewer_ids=(),
        reviewer_kind="human",
        evidence_references=(),
    )
    result = evaluate_quality_policy(
        policy=_policy(
            _metric_rule(),
            StageRequirementRule(
                rule_id="live-stage", rule_version="1.0", applicable=True,
                stage_id="live-answer", track=EvaluationTrack.END_TO_END,
                execution_profile=ExecutionProfile.PRODUCTION,
            ),
            ReviewCoverageRule(
                rule_id="review", rule_version="1.0", applicable=True,
                review_plan_id="human-review", review_plan_version="1.0",
                track=EvaluationTrack.END_TO_END,
                execution_profile=ExecutionProfile.PRODUCTION,
                scope="full_task", min_coverage=1.0,
                min_reviewed_cases=1, min_paper_families=1,
            ),
        ),
        registry=_registry(),
        candidate=_evidence(
            scores=(score,), slots=(_slot(score),), stages=(stage,),
            review_coverage=(review,), families=_families(1),
        ),
    )
    assert result.decision is QualificationDecision.INCONCLUSIVE
    assert result.rule_evaluations[1].decision is QualificationDecision.INCONCLUSIVE
    assert result.rule_evaluations[2].decision is QualificationDecision.INCONCLUSIVE
    assert "review_pending_or_error" in result.rule_evaluations[2].missing_requirements


def test_quality_evidence_rejects_scores_not_backed_by_actual_planned_slots() -> None:
    score = _score(1)
    with pytest.raises(ValidationError, match="actual planned_score_slot"):
        _evidence(scores=(score,), slots=(), families=_families(1))


def test_higher_is_better_delta_min_uses_candidate_minus_baseline() -> None:
    baseline_score = _score(1, value=0.8, variant=D3)
    candidate_score = _score(1, value=0.75, variant=D2)
    policy = _policy(
        _metric_rule(minimum=0.7, delta_min=-0.1),
        allowed=frozenset({ComparisonField.VARIANT_FINGERPRINT}),
    )
    candidate = _evidence(
        scores=(candidate_score,), slots=(_slot(candidate_score),),
        variant=D2, families=_families(1),
    )
    baseline = _evidence(
        scores=(baseline_score,), slots=(_slot(baseline_score),),
        variant=D3, families=_families(1),
    )
    comparison = ComparisonEvidence(
        baseline_identity=baseline.comparison_identity,
        candidate_identity=candidate.comparison_identity,
        allowed_differences=frozenset({ComparisonField.VARIANT_FINGERPRINT}),
        paired_case_ids=("case-1",),
        evidence_references=("artifact://comparisons/quality",),
    )
    passed = evaluate_quality_policy(
        policy=policy, registry=_registry(), candidate=candidate,
        baseline=baseline, comparison=comparison,
    )
    assert passed.decision is QualificationDecision.INCONCLUSIVE
    assert passed.comparison_status is ComparisonStatus.MISSING
    assert passed.rule_evaluations[0].delta is None

    regressed_score = _score(1, value=0.65, variant=D2)
    regressed = _evidence(
        scores=(regressed_score,), slots=(_slot(regressed_score),),
        variant=D2, families=_families(1),
    )
    failed = evaluate_quality_policy(
        policy=policy, registry=_registry(), candidate=regressed,
        baseline=baseline,
        comparison=comparison.model_copy(update={"candidate_identity": regressed.comparison_identity}),
    )
    assert failed.decision is QualificationDecision.FAIL
    assert failed.comparison_status is ComparisonStatus.MISSING
    assert "below min" in failed.rule_evaluations[0].reason

