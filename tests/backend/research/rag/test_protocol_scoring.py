from __future__ import annotations

import math
from typing import Any

import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    content_digest,
)
from backend.research.rag.evaluation.protocol_scoring import (
    CompletenessSlotState,
    EndToEndExecutionState,
    EndToEndObservation,
    EndToEndOutcome,
    EndToEndSlot,
    EndToEndSlotResult,
    EndToEndSummary,
    EvaluationScore,
    EvaluationTrack,
    ExecutionProfile,
    MetricAggregation,
    MetricDefinition,
    MetricDirection,
    MetricGranularity,
    MetricRegistry,
    MetricValueKind,
    MissingValuePolicy,
    NumericDomain,
    ScoreCompleteness,
    ScoreSlot,
    ScoreSlotResult,
    ScoreSource,
    ScoreStatus,
    summarize_end_to_end,
)
from framework.rag.evaluation.report import MetricValue

DIGEST = "a" * 64
VARIANT = "b" * 64


def _definition(
    *,
    metric_id: str = "answer.fact_recall",
    version: str = "1.0",
    kind: MetricValueKind = MetricValueKind.RATIO,
    domain: NumericDomain | None = None,
) -> MetricDefinition:
    return MetricDefinition(
        metric_id=metric_id,
        version=version,
        required_inputs=frozenset({"answer", "reference_claims"}),
        applicable_tracks=frozenset(
            {EvaluationTrack.ORACLE_GENERATION, EvaluationTrack.END_TO_END}
        ),
        applicable_execution_profiles=frozenset(
            {ExecutionProfile.FIXTURE, ExecutionProfile.PRODUCTION}
        ),
        applicable_scopes=frozenset({"full_task", "text_subset"}),
        granularity=MetricGranularity.ATTEMPT,
        direction=MetricDirection.HIGHER_IS_BETTER,
        domain=domain or NumericDomain(minimum=0.0, maximum=1.0),
        value_kind=kind,
        aggregation=(
            MetricAggregation.RATIO if kind is MetricValueKind.RATIO else MetricAggregation.MEAN
        ),
        missing_policy=MissingValuePolicy.ERROR,
    )


def _score(**overrides: Any) -> EvaluationScore:
    provenance: dict[str, Any] = {
        "judge_model": "judge-v1",
        "input": {
            "fingerprint": "c" * 64,
            "artifacts": ["artifact://answer", "artifact://context"],
        },
    }
    payload: dict[str, Any] = {
        "record_type": "score",
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "dataset_id": "research-dev",
        "dataset_version": "2026-10-13",
        "dataset_digest": DIGEST,
        "run_id": "run-1",
        "case_id": "case-1",
        "attempt_id": "attempt-1",
        "track": EvaluationTrack.END_TO_END,
        "execution_profile": ExecutionProfile.PRODUCTION,
        "variant_fingerprint": VARIANT,
        "repetition": 1,
        "scope": "full_task",
        "scoring_revision": "score-revision-1",
        "metric_id": "answer.fact_recall",
        "version": "1.0",
        "value": 0.6,
        "status": ScoreStatus.OK,
        "numerator": 3,
        "denominator": 5,
        "source": ScoreSource.SEMANTIC_JUDGE,
        "provenance": provenance,
        "provenance_digest": content_digest(provenance),
        "evidence": ("artifact://run-1/case-1/judgment",),
        "reason": None,
    }
    payload.update(overrides)
    return EvaluationScore(**payload)


def _slot(case_number: int, *, applicable: bool = True, **overrides: Any) -> ScoreSlot:
    score = _score(
        case_id=f"case-{case_number}",
        attempt_id=f"attempt-{case_number}",
        **overrides,
    )
    return ScoreSlot.from_score(score, applicable=applicable)


def _end_to_end_slot(case_number: int, *, applicable: bool = True) -> EndToEndSlot:
    return EndToEndSlot(
        dataset_id="research-dev",
        dataset_version="2026-10-13",
        dataset_digest=DIGEST,
        run_id="run-1",
        case_id=f"case-{case_number}",
        attempt_id=f"attempt-{case_number}",
        track=EvaluationTrack.END_TO_END,
        execution_profile=ExecutionProfile.PRODUCTION,
        variant_fingerprint=VARIANT,
        repetition=1,
        scope="full_task",
        applicable=applicable,
    )


def _end_to_end_observation(
    slot: EndToEndSlot,
    outcome: EndToEndOutcome,
    *,
    execution_state: EndToEndExecutionState | str = EndToEndExecutionState.COMPLETED,
    required_judge_error: bool = False,
    failure_type: str | None = None,
    reason: str | None = None,
) -> EndToEndObservation:
    return EndToEndObservation(
        **{
            name: getattr(slot, name)
            for name in EndToEndObservation.model_fields
            if name
            not in {
                "outcome",
                "execution_state",
                "required_judge_error",
                "failure_type",
                "reason",
            }
        },
        outcome=outcome,
        execution_state=execution_state,
        required_judge_error=required_judge_error,
        failure_type=failure_type,
        reason=reason,
    )


def test_evaluation_score_requires_explicit_identity_versions_and_scope() -> None:
    dumped = _score().model_dump(mode="json")

    assert dumped["record_type"] == "score"
    assert dumped["schema_version"] == SCHEMA_VERSION
    assert dumped["protocol_version"] == PROTOCOL_VERSION
    assert dumped["scope"] == "full_task"
    assert dumped["version"] == "1.0"
    assert dumped["scoring_revision"] == "score-revision-1"

    for required in (
        "schema_version",
        "protocol_version",
        "dataset_version",
        "version",
        "scoring_revision",
        "scope",
    ):
        payload = dumped.copy()
        del payload[required]
        with pytest.raises(ValidationError):
            EvaluationScore.model_validate(payload)


def test_score_rejects_floating_identity_versions() -> None:
    for field_name in ("dataset_version", "scoring_revision", "version"):
        with pytest.raises(ValidationError, match="floating aliases"):
            _score(**{field_name: "latest"})

    with pytest.raises(ValidationError, match="floating aliases"):
        _definition(version="latest")


def test_provenance_digest_detects_nested_mutation_at_consumption_and_serialization() -> None:
    registry = MetricRegistry((_definition(),))
    score = _score()
    wire = score.model_dump(mode="json")

    round_tripped = EvaluationScore.model_validate_json(score.model_dump_json())
    assert round_tripped.model_dump(mode="json") == wire
    assert round_tripped.verified_provenance() == wire["provenance"]

    nested = score.provenance["input"]
    assert isinstance(nested, dict)
    artifacts = nested["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.append("artifact://tampered")

    with pytest.raises(ValueError, match="mutated after validation"):
        registry.validate_score(score)
    with pytest.raises(ValueError, match="mutated after validation"):
        score.model_dump(mode="json")


def test_provenance_digest_must_bind_supplied_content() -> None:
    with pytest.raises(ValidationError, match="does not match provenance_digest"):
        _score(provenance_digest="d" * 64)


def test_score_rejects_non_finite_bool_wrong_status_and_nullable_values() -> None:
    for value in (True, False, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError, match="finite non-boolean"):
            _score(value=value)

    with pytest.raises(ValidationError, match="valid ScoreStatus"):
        _score(status="passed")
    with pytest.raises(ValidationError, match="requires a numeric value"):
        _score(status=ScoreStatus.OK, value=None, numerator=0, denominator=1)
    with pytest.raises(ValidationError, match="requires value=null"):
        _score(status=ScoreStatus.ERROR, value=0.0, reason="judge timeout")
    with pytest.raises(ValidationError, match="requires a reason"):
        _score(status=ScoreStatus.NEEDS_REVIEW, value=None, reason=None)


def test_registry_validates_ratio_counts_domain_and_zero_denominator() -> None:
    registry = MetricRegistry((_definition(),))

    registry.validate_score(_score(value=0.6, numerator=3, denominator=5))
    with pytest.raises(ValueError, match="must equal"):
        registry.validate_score(_score(value=0.7, numerator=3, denominator=5))
    with pytest.raises(ValueError, match="must not exceed"):
        registry.validate_score(
            _score(value=None, status="error", reason="bad", numerator=6, denominator=5)
        )
    with pytest.raises(ValueError, match="zero-denominator ratio"):
        registry.validate_score(
            _score(value=None, status="error", reason="empty", numerator=0, denominator=0)
        )

    zero_denominator = _score(
        value=None,
        status=ScoreStatus.NOT_APPLICABLE,
        numerator=0,
        denominator=0,
        reason="no applicable claims",
    )
    registry.validate_score(zero_denominator)
    assert registry.to_metric_value(zero_denominator) is None

    with pytest.raises(ValueError, match="outside the registered domain"):
        registry.validate_score(_score(value=1.2, numerator=6, denominator=5))


def test_scalar_metric_rejects_fabricated_counts_and_domain_bounds() -> None:
    definition = _definition(
        metric_id="latency.request_ms",
        kind=MetricValueKind.SCALAR,
        domain=NumericDomain(minimum=0.0, maximum=10_000.0),
    )
    registry = MetricRegistry((definition,))
    scalar = _score(
        metric_id="latency.request_ms",
        value=25.5,
        numerator=None,
        denominator=None,
        source=ScoreSource.REAL_USAGE,
    )

    registry.validate_score(scalar)
    with pytest.raises(ValueError, match="must not carry"):
        registry.validate_score(
            scalar.model_copy(update={"numerator": 1, "denominator": 1})
        )
    with pytest.raises(ValidationError, match="domain minimum"):
        NumericDomain(minimum=2.0, maximum=1.0)
    with pytest.raises(ValidationError, match="finite non-boolean"):
        NumericDomain(minimum=False, maximum=1.0)


def test_registry_supports_explicit_registration_and_rejects_duplicates() -> None:
    registry = MetricRegistry((_definition(),))
    latency = _definition(
        metric_id="latency.request_ms",
        kind=MetricValueKind.SCALAR,
        domain=NumericDomain(minimum=0.0, maximum=10_000.0),
    )

    registry.register(latency)

    assert registry.definition("latency.request_ms", "1.0") == latency
    with pytest.raises(ValueError, match="duplicate metric definition"):
        registry.register(latency)


def test_registry_rejects_unknown_inapplicable_and_incomparable_metrics() -> None:
    v1 = _definition(version="1.0")
    v2 = _definition(version="2.0")
    registry = MetricRegistry((v1, v2))

    with pytest.raises(ValueError, match="unknown metric definition"):
        registry.validate_score(_score(metric_id="unknown.metric"))
    with pytest.raises(ValueError, match="not applicable"):
        registry.validate_score(_score(track=EvaluationTrack.RETRIEVAL))
    with pytest.raises(ValueError, match="incomparable metric versions"):
        registry.ensure_comparable((_score(version="1.0"), _score(version="2.0")))


def test_only_ok_scores_project_to_framework_metric_value_and_zero_is_preserved() -> None:
    registry = MetricRegistry((_definition(),))
    zero = _score(value=0.0, numerator=0, denominator=4)

    projected = registry.to_metric_value(zero)

    assert isinstance(projected, MetricValue)
    assert projected.value == 0.0
    assert projected.metadata["metric_version"] == "1.0"
    assert registry.to_metric_value(
        _score(
            value=None,
            status=ScoreStatus.ERROR,
            numerator=0,
            denominator=4,
            reason="judge timeout",
        )
    ) is None
    assert registry.to_metric_value(
        _score(
            value=None,
            status=ScoreStatus.NEEDS_REVIEW,
            numerator=0,
            denominator=4,
            reason="human adjudication required",
        )
    ) is None


def test_completeness_retains_all_planned_slots_and_counts_na_pending_and_missing() -> None:
    registry = MetricRegistry((_definition(),))
    slots = tuple(_slot(i, applicable=i > 3) for i in range(1, 11))
    scores = [
        _score(
            case_id=f"case-{i}",
            attempt_id=f"attempt-{i}",
            value=None,
            status=ScoreStatus.NOT_APPLICABLE,
            numerator=0,
            denominator=0,
            reason="outside frozen metric applicability",
        )
        for i in range(1, 4)
    ]
    scores.extend(
        _score(case_id=f"case-{i}", attempt_id=f"attempt-{i}")
        for i in range(4, 8)
    )
    scores.extend(
        _score(
            case_id=f"case-{i}",
            attempt_id=f"attempt-{i}",
            value=None,
            status=ScoreStatus.ERROR,
            numerator=0,
            denominator=5,
            reason="judge error",
        )
        for i in range(8, 9)
    )
    scores.append(
        _score(
            case_id="case-9",
            attempt_id="attempt-9",
            value=None,
            status=ScoreStatus.NEEDS_REVIEW,
            numerator=0,
            denominator=5,
            reason="requires adjudication",
        )
    )
    # case-10 is deliberately absent: it remains a planned, applicable error.

    summary = registry.summarize_completeness(slots, scores)

    assert summary.planned == 10
    assert summary.applicable == 7
    assert summary.valid == 4
    assert summary.error == 2
    assert summary.not_applicable == 3
    assert summary.pending_review == 1
    assert summary.missing == 1
    assert summary.coverage == pytest.approx(4 / 7)
    assert len(summary.slots) == 10
    assert summary.slots[-1].state.value == "missing"


def test_completeness_rejects_duplicate_unknown_and_dangling_slots() -> None:
    registry = MetricRegistry((_definition(),))
    slot = _slot(1)
    score = _score()

    with pytest.raises(ValueError, match="duplicate planned"):
        registry.summarize_completeness((slot, slot), (score,))
    with pytest.raises(ValueError, match="duplicate score"):
        registry.summarize_completeness((slot,), (score, score))
    with pytest.raises(ValueError, match="unknown planned slot"):
        registry.summarize_completeness((slot,), (_score(case_id="case-99"),))

    dangling = _slot(1, track=EvaluationTrack.RETRIEVAL)
    with pytest.raises(ValueError, match="dangling planned slot"):
        registry.summarize_completeness((dangling,), ())


def test_runtime_failure_stays_known_failure_in_ten_case_denominator() -> None:
    slots = tuple(_end_to_end_slot(i) for i in range(1, 11))
    observations = [
        _end_to_end_observation(slot, EndToEndOutcome.KNOWN_SUCCESS)
        for slot in slots[:7]
    ]
    observations.extend(
        _end_to_end_observation(
            slot,
            EndToEndOutcome.KNOWN_FAILURE,
            failure_type="quality_failure",
            reason="required behavior was not satisfied",
        )
        for slot in slots[7:9]
    )
    observations.append(
        _end_to_end_observation(
            slots[9],
            EndToEndOutcome.KNOWN_FAILURE,
            execution_state="runtime_failed",
            failure_type="generation_timeout",
            reason="generation exceeded its timeout",
        )
    )

    summary = summarize_end_to_end(slots, observations)

    assert (summary.known_success, summary.known_failure, summary.unknown) == (7, 3, 0)
    assert summary.applicable == 10
    assert summary.success_rate == pytest.approx(0.7)
    runtime_result = summary.slots[-1]
    assert runtime_result.observation is not None
    assert runtime_result.observation.execution_state == "runtime_failed"
    assert runtime_result.observation.failure_type == "generation_timeout"


def test_judge_error_is_unknown_and_only_exposes_verified_lower_bound() -> None:
    slots = tuple(_end_to_end_slot(i) for i in range(1, 11))
    observations = [
        _end_to_end_observation(slot, EndToEndOutcome.KNOWN_SUCCESS)
        for slot in slots[:7]
    ]
    observations.extend(
        _end_to_end_observation(
            slot,
            EndToEndOutcome.KNOWN_FAILURE,
            failure_type="quality_failure",
            reason="answer failed the reviewed behavior",
        )
        for slot in slots[7:9]
    )
    observations.append(
        _end_to_end_observation(
            slots[9],
            EndToEndOutcome.UNKNOWN,
            execution_state="model_path_completed",
            required_judge_error=True,
            failure_type="judge_timeout",
            reason="required judge did not return a valid observation",
        )
    )

    summary = summarize_end_to_end(slots, observations)

    assert (summary.known_success, summary.known_failure, summary.unknown) == (7, 2, 1)
    assert summary.applicable == 10
    assert summary.is_complete is False
    assert summary.success_rate is None
    assert summary.verified_success_lower_bound == pytest.approx(0.7)


def test_required_judge_error_cannot_be_reclassified_as_known_failure() -> None:
    slot = _end_to_end_slot(1)

    with pytest.raises(ValidationError, match="required judge errors must remain unknown"):
        EndToEndObservation(
            dataset_id=slot.dataset_id,
            dataset_version=slot.dataset_version,
            dataset_digest=slot.dataset_digest,
            run_id=slot.run_id,
            case_id=slot.case_id,
            attempt_id=slot.attempt_id,
            track=slot.track,
            execution_profile=slot.execution_profile,
            variant_fingerprint=slot.variant_fingerprint,
            repetition=slot.repetition,
            scope=slot.scope,
            outcome=EndToEndOutcome.KNOWN_FAILURE,
            execution_state="model_path_completed",
            required_judge_error=True,
            failure_type="judge_timeout",
            reason="required judge timed out",
        )

    unknown = _end_to_end_observation(
        slot,
        EndToEndOutcome.UNKNOWN,
        execution_state="model_path_completed",
        required_judge_error=True,
        failure_type="judge_timeout",
        reason="required judge timed out",
    )
    summary = summarize_end_to_end((slot,), (unknown,))
    assert summary.known_failure == 0
    assert summary.unknown == 1
    assert summary.success_rate is None


def test_end_to_end_retains_na_and_missing_as_unknown_and_rejects_bad_references() -> None:
    applicable = _end_to_end_slot(1)
    not_applicable = _end_to_end_slot(2, applicable=False)
    na_observation = _end_to_end_observation(
        not_applicable,
        EndToEndOutcome.NOT_APPLICABLE,
        reason="oracle track excludes unanswerable case",
    )

    summary = summarize_end_to_end((applicable, not_applicable), (na_observation,))

    assert summary.planned == 2
    assert summary.applicable == 1
    assert summary.not_applicable == 1
    assert summary.unknown == 1
    assert len(summary.slots) == 2

    with pytest.raises(ValueError, match="duplicate planned"):
        summarize_end_to_end((applicable, applicable), ())
    with pytest.raises(ValueError, match="unknown planned slot"):
        summarize_end_to_end(
            (applicable,),
            (
                _end_to_end_observation(
                    _end_to_end_slot(99), EndToEndOutcome.KNOWN_SUCCESS
                ),
            ),
        )


@pytest.mark.parametrize(
    "outside_identity",
    (
        {"track": EvaluationTrack.RETRIEVAL},
        {"execution_profile": ExecutionProfile.FIXTURE},
        {"scope": "figure_subset"},
    ),
    ids=("track-a", "profile", "scope"),
)
def test_nonapplicable_metric_is_retained_against_its_frozen_na_slot(
    outside_identity: dict[str, Any],
) -> None:
    definition = MetricDefinition(
        **{
            **_definition().model_dump(),
            "applicable_execution_profiles": frozenset({ExecutionProfile.PRODUCTION}),
        }
    )
    registry = MetricRegistry((definition,))
    na = _score(
        **outside_identity,
        status=ScoreStatus.NOT_APPLICABLE,
        value=None,
        numerator=None,
        denominator=None,
        reason="outside the frozen metric track/profile/scope applicability",
    )
    slot = ScoreSlot.from_score(na, applicable=False)

    assert registry.validate_score(na) == definition
    assert registry.to_metric_value(na) is None
    summary = registry.summarize_completeness((slot,), (na,))
    assert (summary.planned, summary.applicable, summary.not_applicable) == (1, 0, 1)
    assert summary.slots[0].score is na
    assert summary.coverage is None
    assert ScoreCompleteness.model_validate_json(summary.model_dump_json()) == summary

    for status in (ScoreStatus.OK, ScoreStatus.ERROR, ScoreStatus.NEEDS_REVIEW):
        non_na = _score(
            **outside_identity,
            status=status,
            value=0.6 if status is ScoreStatus.OK else None,
            reason=None if status is ScoreStatus.OK else "metric unavailable",
        )
        with pytest.raises(ValueError, match="not applicable"):
            registry.validate_score(non_na)


def test_applicable_score_slot_cannot_be_relabelled_na_to_shrink_denominator() -> None:
    registry = MetricRegistry((_definition(),))
    slot = _slot(1)
    na = _score(
        status=ScoreStatus.NOT_APPLICABLE,
        value=None,
        numerator=0,
        denominator=0,
        reason="no claims returned after runtime failure",
    )

    with pytest.raises(ValueError, match="applicable planned slot cannot"):
        registry.summarize_completeness((slot,), (na,))
    missing = registry.summarize_completeness((slot,), ())
    assert (missing.planned, missing.applicable, missing.error, missing.missing) == (1, 1, 1, 1)
    assert missing.not_applicable == 0
    assert missing.coverage == 0.0

    with pytest.raises(ValueError, match="non-applicable planned slot requires"):
        registry.summarize_completeness((_slot(1, applicable=False),), (_score(),))


@pytest.mark.parametrize("status", (ScoreStatus.ERROR, ScoreStatus.NEEDS_REVIEW))
@pytest.mark.parametrize("denominator", (None, 5), ids=("unknown-denominator", "known-denominator"))
def test_unknown_ratio_counts_remain_null_without_changing_plan_denominator(
    status: ScoreStatus,
    denominator: int | None,
) -> None:
    registry = MetricRegistry((_definition(),))
    unknown = _score(
        status=status,
        value=None,
        numerator=None,
        denominator=denominator,
        reason="required judge unavailable; supported-claim count is unknown",
    )

    registry.validate_score(unknown)
    assert registry.to_metric_value(unknown) is None
    wire = unknown.model_dump(mode="json")
    assert wire["value"] is None
    assert wire["numerator"] is None
    assert wire["denominator"] == denominator
    round_trip = EvaluationScore.model_validate_json(unknown.model_dump_json())
    registry.validate_score(round_trip)
    assert round_trip.numerator is None
    assert round_trip.denominator == denominator

    summary = registry.summarize_completeness((_slot(1),), (unknown,))
    assert (summary.planned, summary.applicable, summary.valid) == (1, 1, 0)
    assert summary.error == int(status is ScoreStatus.ERROR)
    assert summary.pending_review == int(status is ScoreStatus.NEEDS_REVIEW)
    assert summary.not_applicable == 0
    assert summary.coverage == 0.0
    assert math.isfinite(summary.coverage)
    assert summary.slots[0].score is unknown


@pytest.mark.parametrize("numerator,denominator", ((None, None), (None, 5), (0, None)))
def test_ok_ratio_requires_real_counts(numerator: int | None, denominator: int | None) -> None:
    registry = MetricRegistry((_definition(),))
    with pytest.raises(ValueError, match="actual numerator and denominator|known denominator"):
        registry.validate_score(_score(numerator=numerator, denominator=denominator))


@pytest.mark.parametrize(
    "status,numerator,denominator",
    (
        (ScoreStatus.ERROR, 1, None),
        (ScoreStatus.ERROR, 6, 5),
        (ScoreStatus.NOT_APPLICABLE, None, 0),
        (ScoreStatus.NOT_APPLICABLE, 1, 0),
        (ScoreStatus.ERROR, 0, 0),
        (ScoreStatus.NEEDS_REVIEW, 0, 0),
    ),
)
def test_unknown_ratio_counts_do_not_permit_inconsistent_known_counts(
    status: ScoreStatus,
    numerator: int | None,
    denominator: int | None,
) -> None:
    registry = MetricRegistry((_definition(),))
    invalid = _score(
        status=status,
        value=None,
        numerator=numerator,
        denominator=denominator,
        reason="known counts do not justify an observation",
    )
    with pytest.raises(ValueError, match="denominator|numerator"):
        registry.validate_score(invalid)


@pytest.mark.parametrize("state", ("failed", "runtime_failed", "runtime_timeout"))
@pytest.mark.parametrize("outcome", (EndToEndOutcome.KNOWN_SUCCESS, EndToEndOutcome.UNKNOWN))
def test_ten_case_summary_rejects_runtime_failure_masquerading_as_success_or_unknown(
    state: str,
    outcome: EndToEndOutcome,
) -> None:
    slots = tuple(_end_to_end_slot(i) for i in range(1, 11))
    observations = [
        _end_to_end_observation(slot, EndToEndOutcome.KNOWN_SUCCESS)
        for slot in slots[:7]
    ]
    observations.extend(
        _end_to_end_observation(
            slot,
            EndToEndOutcome.KNOWN_FAILURE,
            failure_type="quality_failure",
            reason="required behavior was not satisfied",
        )
        for slot in slots[7:9]
    )
    runtime_failure = _end_to_end_observation(
        slots[9],
        EndToEndOutcome.KNOWN_FAILURE,
        execution_state=state,
        failure_type="generation_timeout",
        reason="generation exceeded its timeout",
    )

    # Both the construction boundary and aggregation boundary enforce the
    # same model-path fact, even when an unvalidated model_copy is supplied.
    with pytest.raises(ValidationError, match="confirmed runtime failure must remain"):
        _end_to_end_observation(
            slots[9],
            outcome,
            execution_state=state,
            failure_type="generation_timeout" if outcome is EndToEndOutcome.UNKNOWN else None,
            reason="generation timeout" if outcome is EndToEndOutcome.UNKNOWN else None,
        )
    corrupt = runtime_failure.model_copy(update={"outcome": outcome})
    with pytest.raises(ValueError, match="confirmed runtime failure must remain"):
        summarize_end_to_end(slots, (*observations, corrupt))

    summary = summarize_end_to_end(slots, (*observations, runtime_failure))
    assert (summary.known_success, summary.known_failure, summary.unknown) == (7, 3, 0)
    assert summary.applicable == 10
    assert summary.success_rate == pytest.approx(0.7)
    assert summary.slots[-1].observation is runtime_failure
    assert runtime_failure.execution_state.value == state
    assert runtime_failure.execution_state.classification is EndToEndExecutionState.FAILED
    wire = runtime_failure.model_dump(mode="json")
    assert wire["execution_state"] == state
    assert wire["failure_type"] == "generation_timeout"
    restored = EndToEndObservation.model_validate_json(runtime_failure.model_dump_json())
    assert restored.execution_state.value == state


@pytest.mark.parametrize(
    "state",
    ("planned", "preflight_rejected", "running", "incomplete", "cancelled", "pending", "unrun"),
)
def test_pending_or_unrun_execution_is_unknown_unless_frozen_plan_is_na(state: str) -> None:
    slot = _end_to_end_slot(1)
    unknown = _end_to_end_observation(
        slot,
        EndToEndOutcome.UNKNOWN,
        execution_state=state,
        failure_type="execution_not_completed",
        reason="model path has no confirmed result",
    )
    summary = summarize_end_to_end((slot,), (unknown,))
    assert (summary.applicable, summary.unknown) == (1, 1)
    assert summary.success_rate is None
    assert unknown.execution_state.value == state

    for outcome in (EndToEndOutcome.KNOWN_SUCCESS, EndToEndOutcome.KNOWN_FAILURE):
        with pytest.raises(ValidationError, match="pending or unrun model paths"):
            _end_to_end_observation(
                slot,
                outcome,
                execution_state=state,
                failure_type="execution_not_completed",
                reason="model path has no confirmed result",
            )

    na_slot = _end_to_end_slot(2, applicable=False)
    na = _end_to_end_observation(
        na_slot,
        EndToEndOutcome.NOT_APPLICABLE,
        execution_state=state,
        reason="reviewed gold scope excludes this experiment unit",
    )
    na_summary = summarize_end_to_end((na_slot,), (na,))
    assert (na_summary.planned, na_summary.applicable, na_summary.not_applicable) == (1, 0, 1)
    assert na_summary.success_rate is None
    assert na_summary.verified_success_lower_bound is None


@pytest.mark.parametrize("state", ("completed", "model_path_completed"))
def test_judge_error_requires_successful_execution_and_preserves_original_state(state: str) -> None:
    slot = _end_to_end_slot(1)
    unknown = _end_to_end_observation(
        slot,
        EndToEndOutcome.UNKNOWN,
        execution_state=state,
        required_judge_error=True,
        failure_type="judge_unavailable",
        reason="required judge produced no observation",
    )
    assert unknown.execution_state.classification is EndToEndExecutionState.COMPLETED
    assert unknown.model_dump(mode="json")["execution_state"] == state
    summary = summarize_end_to_end((slot,), (unknown,))
    assert (summary.known_success, summary.known_failure, summary.unknown) == (0, 0, 1)
    assert summary.success_rate is None

    for outcome in (EndToEndOutcome.KNOWN_SUCCESS, EndToEndOutcome.KNOWN_FAILURE):
        with pytest.raises(ValidationError, match="required judge errors must remain unknown"):
            _end_to_end_observation(
                slot,
                outcome,
                execution_state=state,
                required_judge_error=True,
                failure_type="judge_unavailable",
                reason="required judge produced no observation",
            )


@pytest.mark.parametrize("state", ("planned", "incomplete", "cancelled", "runtime_failed"))
def test_judge_error_cannot_replace_an_unrun_or_failed_model_path(state: str) -> None:
    with pytest.raises(ValidationError, match="confirmed runtime failure|require completed"):
        _end_to_end_observation(
            _end_to_end_slot(1),
            EndToEndOutcome.UNKNOWN,
            execution_state=state,
            required_judge_error=True,
            failure_type="judge_timeout",
            reason="judge was unavailable",
        )


def test_unknown_execution_state_is_rejected_and_reason_does_not_route_outcome() -> None:
    slot = _end_to_end_slot(1)
    with pytest.raises(ValidationError, match="valid EndToEndExecutionState"):
        _end_to_end_observation(slot, EndToEndOutcome.KNOWN_SUCCESS, execution_state="looks_good")

    runtime_failure = _end_to_end_observation(
        slot,
        EndToEndOutcome.KNOWN_FAILURE,
        execution_state="runtime_timeout",
        failure_type="generation_timeout",
        reason="this reason mentions a judge but cannot change the execution fact",
    )
    assert summarize_end_to_end((slot,), (runtime_failure,)).known_failure == 1
    with pytest.raises(ValidationError, match="confirmed runtime failure must remain"):
        _end_to_end_observation(
            slot,
            EndToEndOutcome.NOT_APPLICABLE,
            execution_state="runtime_timeout",
            reason="gold scope is non-applicable",
        )


def test_score_completeness_recomputes_counters_instead_of_accepting_valid_subset_claims() -> None:
    registry = MetricRegistry((_definition(),))
    slots = tuple(_slot(i) for i in range(1, 11))
    errors = tuple(
        _score(
            case_id=slot.case_id,
            attempt_id=slot.attempt_id,
            status=ScoreStatus.ERROR,
            value=None,
            numerator=None,
            denominator=None,
            reason="judge unavailable",
        )
        for slot in slots
    )
    actual = registry.summarize_completeness(slots, errors)
    assert (actual.valid, actual.error, actual.applicable) == (0, 10, 10)
    assert actual.coverage == 0.0
    assert math.isfinite(actual.coverage)
    forged_counts = {**actual.model_dump(), "valid": 10, "error": 0}
    with pytest.raises(ValidationError, match="valid count must match retained score slots"):
        ScoreCompleteness(**forged_counts)

    for name in (
        "planned", "applicable", "valid", "error", "not_applicable", "pending_review", "missing"
    ):
        counts = actual.model_dump()
        counts[name] += 1
        with pytest.raises(ValidationError, match=f"{name} count must match retained score slots"):
            ScoreCompleteness(**counts)


def test_score_slot_result_validates_observation_state_identity_and_applicability() -> None:
    slot = _slot(1)
    error = _score(
        status=ScoreStatus.ERROR,
        value=None,
        numerator=None,
        denominator=None,
        reason="judge unavailable",
    )
    with pytest.raises(ValidationError, match="score slot state must agree"):
        ScoreSlotResult(slot=slot, score=error, state=CompletenessSlotState.VALID)
    with pytest.raises(ValidationError, match="identity must match"):
        ScoreSlotResult(slot=_slot(2), score=error, state=CompletenessSlotState.ERROR)
    with pytest.raises(ValidationError, match="non-applicable planned slot requires"):
        ScoreSlotResult(slot=_slot(1, applicable=False), score=error, state=CompletenessSlotState.ERROR)

    result = ScoreSlotResult(slot=slot, score=error, state=CompletenessSlotState.ERROR)
    corrupt = result.model_copy(update={"state": CompletenessSlotState.VALID})
    with pytest.raises(ValidationError, match="score slot state must agree"):
        ScoreCompleteness(
            planned=1,
            applicable=1,
            valid=1,
            error=0,
            not_applicable=0,
            pending_review=0,
            missing=0,
            slots=(corrupt,),
        )


def test_end_to_end_summary_recomputes_all_counters_from_retained_slot_outcomes() -> None:
    slots = tuple(_end_to_end_slot(i) for i in range(1, 11))
    observations = [
        _end_to_end_observation(slot, EndToEndOutcome.KNOWN_SUCCESS)
        for slot in slots[:7]
    ]
    observations.extend(
        _end_to_end_observation(
            slot,
            EndToEndOutcome.KNOWN_FAILURE,
            failure_type="quality_failure",
            reason="required behavior was not satisfied",
        )
        for slot in slots[7:9]
    )
    observations.append(
        _end_to_end_observation(
            slots[9],
            EndToEndOutcome.UNKNOWN,
            required_judge_error=True,
            failure_type="judge_timeout",
            reason="required judge did not finish",
        )
    )
    actual = summarize_end_to_end(slots, observations)
    assert actual.success_rate is None
    assert actual.verified_success_lower_bound == pytest.approx(0.7)
    assert math.isfinite(actual.verified_success_lower_bound)
    # These counters still sum to ten, but falsely turn an unknown judge
    # observation into a model failure and claim a complete rate.
    forged_counts = {**actual.model_dump(), "known_failure": 3, "unknown": 0}
    with pytest.raises(
        ValidationError, match="known_failure count must match retained end-to-end slots"
    ):
        EndToEndSummary(**forged_counts)

    for name in ("planned", "applicable", "known_success", "known_failure", "unknown", "not_applicable"):
        counts = actual.model_dump()
        counts[name] += 1
        with pytest.raises(ValidationError, match=f"{name} count must match retained end-to-end slots"):
            EndToEndSummary(**counts)


def test_end_to_end_slot_result_cannot_override_failure_observation_or_invent_missing_success() -> None:
    slot = _end_to_end_slot(1)
    runtime_failure = _end_to_end_observation(
        slot,
        EndToEndOutcome.KNOWN_FAILURE,
        execution_state="runtime_failed",
        failure_type="generation_timeout",
        reason="generation timed out",
    )
    with pytest.raises(ValidationError, match="slot outcome must agree"):
        EndToEndSlotResult(slot=slot, observation=runtime_failure, outcome=EndToEndOutcome.KNOWN_SUCCESS)
    with pytest.raises(ValidationError, match="slot outcome must agree"):
        EndToEndSlotResult(slot=slot, observation=None, outcome=EndToEndOutcome.KNOWN_SUCCESS)
    with pytest.raises(ValidationError, match="identity must match"):
        EndToEndSlotResult(
            slot=_end_to_end_slot(2),
            observation=runtime_failure,
            outcome=runtime_failure.outcome,
        )

    result = EndToEndSlotResult(slot=slot, observation=runtime_failure, outcome=runtime_failure.outcome)
    corrupt = result.model_copy(update={"outcome": EndToEndOutcome.KNOWN_SUCCESS})
    with pytest.raises(ValidationError, match="slot outcome must agree"):
        EndToEndSummary(
            planned=1,
            applicable=1,
            known_success=1,
            known_failure=0,
            unknown=0,
            not_applicable=0,
            slots=(corrupt,),
        )


def test_summary_construction_rejects_duplicate_retained_slots() -> None:
    score_summary = MetricRegistry((_definition(),)).summarize_completeness((_slot(1),), ())
    with pytest.raises(ValidationError, match="duplicate planned score slot"):
        ScoreCompleteness(
            planned=2,
            applicable=2,
            valid=0,
            error=2,
            not_applicable=0,
            pending_review=0,
            missing=2,
            slots=score_summary.slots * 2,
        )

    end_to_end_summary = summarize_end_to_end((_end_to_end_slot(1),), ())
    with pytest.raises(ValidationError, match="duplicate planned end-to-end slot"):
        EndToEndSummary(
            planned=2,
            applicable=2,
            known_success=0,
            known_failure=0,
            unknown=2,
            not_applicable=0,
            slots=end_to_end_summary.slots * 2,
        )


def test_gold_na_experiment_cannot_hide_an_applicable_judge_unknown() -> None:
    applicable_slot = _end_to_end_slot(1)
    na_slot = _end_to_end_slot(2, applicable=False)
    judge_unknown = _end_to_end_observation(
        applicable_slot,
        EndToEndOutcome.UNKNOWN,
        execution_state="model_path_completed",
        required_judge_error=True,
        failure_type="judge_timeout",
        reason="required judge produced no measurement",
    )
    gold_na = _end_to_end_observation(
        na_slot,
        EndToEndOutcome.NOT_APPLICABLE,
        execution_state="planned",
        reason="reviewed gold excludes this experiment unit",
    )
    actual = summarize_end_to_end((applicable_slot, na_slot), (judge_unknown, gold_na))
    assert (actual.planned, actual.applicable, actual.unknown, actual.not_applicable) == (2, 1, 1, 1)
    assert actual.success_rate is None
    assert actual.verified_success_lower_bound == 0.0
    assert EndToEndSummary.model_validate_json(actual.model_dump_json()) == actual

    na_for_applicable = _end_to_end_observation(
        applicable_slot,
        EndToEndOutcome.NOT_APPLICABLE,
        execution_state="completed",
        reason="this after-the-fact NA would shrink the denominator",
    )
    with pytest.raises(ValueError, match="applicable end-to-end slot cannot"):
        summarize_end_to_end((applicable_slot, na_slot), (na_for_applicable, gold_na))

    forged_counts = {**actual.model_dump(), "applicable": 0, "unknown": 0, "not_applicable": 2}
    with pytest.raises(ValidationError, match="applicable count must match"):
        EndToEndSummary(**forged_counts)


def test_judge_failure_code_cannot_contradict_explicit_judge_error_flag() -> None:
    with pytest.raises(ValidationError, match="must agree with the typed judge failure"):
        _end_to_end_observation(
            _end_to_end_slot(1),
            EndToEndOutcome.KNOWN_FAILURE,
            execution_state="completed",
            required_judge_error=False,
            failure_type="judge_timeout",
            reason="a judge timeout is not a model quality failure",
        )
