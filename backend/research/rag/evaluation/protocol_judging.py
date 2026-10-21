"""Protocol integration for independent semantic judge observations.

This module maps Research-owned judge observations into the existing strict
``EvaluationScore`` and ``MetricRegistry`` contracts. It does not own provider
clients, retries, routing, quality gates, publication, or run persistence.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import Field, model_validator

from backend.research.ports.evaluation_judge import (
    JudgeMetric,
    JudgeObservationStatus,
    JudgeRuntimeProfile,
    SemanticEvaluationJudgePort,
    SemanticJudgeObservation,
    SemanticJudgeRequest,
)
from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    EvaluationTrack,
    ExecutionProfile,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
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

RAGAS_SEMANTIC_METRIC_VERSION = "ragas-0.4.3-research-v1"
CITATION_SUPPORT_METRIC_VERSION = "research-citation-support-v1"


class JudgeScoreIdentity(ProtocolModel):
    """Frozen score identity supplied by the owning evaluation run."""

    dataset_id: NonEmptyStr
    dataset_version: NonEmptyStr
    dataset_digest: Sha256Digest
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    attempt_id: NonEmptyStr
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    variant_fingerprint: Sha256Digest
    repetition: int = Field(ge=1, strict=True)
    scope: NonEmptyStr
    scoring_revision: NonEmptyStr

    @model_validator(mode="after")
    def _semantic_scope_is_explicit(self) -> "JudgeScoreIdentity":
        if self.scope not in {"text_subset", "reference_comparison"}:
            raise ValueError(
                "Ragas semantic scores require scope=text_subset or reference_comparison"
            )
        if self.track is EvaluationTrack.RETRIEVAL:
            raise ValueError("answer semantic judge metrics do not apply to retrieval-only runs")
        return self


def semantic_judge_metric_definitions() -> tuple[MetricDefinition, ...]:
    """Return definitions for the supported, independently observed metrics."""

    common = {
        "applicable_tracks": frozenset(
            {EvaluationTrack.ORACLE_GENERATION, EvaluationTrack.END_TO_END}
        ),
        "applicable_execution_profiles": frozenset(
            {ExecutionProfile.FIXTURE, ExecutionProfile.PRODUCTION}
        ),
        "granularity": MetricGranularity.ATTEMPT,
        "direction": MetricDirection.HIGHER_IS_BETTER,
        "domain": NumericDomain(minimum=0.0, maximum=1.0),
        "missing_policy": MissingValuePolicy.ERROR,
    }
    ratio = {
        **common,
        "value_kind": MetricValueKind.RATIO,
        "aggregation": MetricAggregation.RATIO,
    }
    scalar = {
        **common,
        "value_kind": MetricValueKind.SCALAR,
        "aggregation": MetricAggregation.MEAN,
    }
    return (
        MetricDefinition(
            metric_id=JudgeMetric.FACT_PRECISION.value,
            version=RAGAS_SEMANTIC_METRIC_VERSION,
            required_inputs=frozenset({"response", "reference"}),
            applicable_scopes=frozenset({"reference_comparison"}),
            **ratio,
        ),
        MetricDefinition(
            metric_id=JudgeMetric.FACT_RECALL.value,
            version=RAGAS_SEMANTIC_METRIC_VERSION,
            required_inputs=frozenset({"response", "reference"}),
            applicable_scopes=frozenset({"reference_comparison"}),
            **ratio,
        ),
        MetricDefinition(
            metric_id=JudgeMetric.FACT_F1.value,
            version=RAGAS_SEMANTIC_METRIC_VERSION,
            required_inputs=frozenset({"response", "reference"}),
            applicable_scopes=frozenset({"reference_comparison"}),
            **scalar,
        ),
        MetricDefinition(
            metric_id=JudgeMetric.FAITHFULNESS.value,
            version=RAGAS_SEMANTIC_METRIC_VERSION,
            required_inputs=frozenset(
                {"user_input", "response", "actual_contexts"}
            ),
            applicable_scopes=frozenset({"text_subset"}),
            **ratio,
        ),
        MetricDefinition(
            metric_id=JudgeMetric.CITATION_SUPPORT.value,
            version=CITATION_SUPPORT_METRIC_VERSION,
            required_inputs=frozenset(
                {
                    "claim_id",
                    "claim_text",
                    "answer_span",
                    "citation_location_text",
                }
            ),
            applicable_scopes=frozenset({"text_subset"}),
            **ratio,
        ),
    )


def register_semantic_judge_metrics(registry: MetricRegistry) -> None:
    """Register semantic metrics in the existing shared Research registry."""

    for definition in semantic_judge_metric_definitions():
        registry.register(definition)


def build_semantic_judge_registry() -> MetricRegistry:
    """Convenience registry for isolated contract tests and optional adapters."""

    return MetricRegistry(semantic_judge_metric_definitions())


async def collect_independent_judge_observations(
    judge: SemanticEvaluationJudgePort,
    requests: Iterable[SemanticJudgeRequest],
) -> tuple[SemanticJudgeObservation, ...]:
    """Judge each frozen metric input independently without score substitution.

    Calls are intentionally serial here. The owning Harness/runner may schedule
    cases, but each adapter observation has its own bounded request/deadline
    budget and must retain its own errors. No result is copied into another
    metric and no judge outcome is interpreted as a quality gate decision.
    """

    observations: list[SemanticJudgeObservation] = []
    seen: set[str] = set()
    for request in requests:
        if request.observation_id in seen:
            raise ValueError(f"duplicate judge observation id: {request.observation_id}")
        seen.add(request.observation_id)
        observation = await judge.judge(request)
        if observation.observation_id != request.observation_id:
            raise ValueError("judge observation identity does not match request")
        if observation.metric is not request.metric:
            raise ValueError("judge observation metric does not match request")
        if observation.metric_version != request.metric_version:
            raise ValueError("judge observation metric version does not match request")
        if observation.execution_profile is not request.execution_profile:
            raise ValueError("judge observation execution profile does not match request")
        observations.append(observation)
    return tuple(observations)


def observation_to_evaluation_score(
    *,
    identity: JudgeScoreIdentity,
    observation: SemanticJudgeObservation,
    registry: MetricRegistry,
) -> EvaluationScore:
    """Project one validated semantic observation into ``EvaluationScore``."""

    expected_profile = {
        ExecutionProfile.FIXTURE: JudgeRuntimeProfile.FIXTURE,
        ExecutionProfile.PRODUCTION: JudgeRuntimeProfile.PRODUCTION,
    }[identity.execution_profile]
    if observation.execution_profile is not expected_profile:
        raise ValueError("judge execution profile does not match score identity")
    if observation.scope.value != identity.scope:
        raise ValueError("judge scope does not match score identity")

    status = {
        JudgeObservationStatus.OK: ScoreStatus.OK,
        JudgeObservationStatus.ERROR: ScoreStatus.ERROR,
        JudgeObservationStatus.NOT_APPLICABLE: ScoreStatus.NOT_APPLICABLE,
        JudgeObservationStatus.NEEDS_REVIEW: ScoreStatus.NEEDS_REVIEW,
    }[observation.status]
    definition = registry.definition(observation.metric.value, observation.metric_version)
    is_ratio = definition.value_kind is MetricValueKind.RATIO
    if observation.status is JudgeObservationStatus.OK and is_ratio:
        if observation.numerator is None or observation.denominator is None:
            raise ValueError("registered ratio observation requires actual counts")
        numerator = observation.numerator
        denominator = observation.denominator
    else:
        numerator = denominator = None

    provenance: dict[str, Any] = {
        "semantic_judge": observation.model_dump(mode="json"),
        "observation_digest": content_digest(observation),
        "qualification_limit": (
            "text_subset does not satisfy unmeasured visual/formula/table requirements; "
            "reference_comparison does not establish the truth of claims absent from reference"
        ),
    }
    reason = None if status is ScoreStatus.OK else "; ".join(observation.reasons)
    score = EvaluationScore(
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
        metric_id=observation.metric.value,
        version=observation.metric_version,
        value=observation.value,
        status=status,
        numerator=numerator,
        denominator=denominator,
        source=ScoreSource.SEMANTIC_JUDGE,
        provenance=provenance,
        provenance_digest=content_digest(provenance),
        evidence=observation.evidence_references,
        reason=reason,
    )
    registry.validate_score(score)
    return score


__all__ = [
    "CITATION_SUPPORT_METRIC_VERSION",
    "JudgeScoreIdentity",
    "RAGAS_SEMANTIC_METRIC_VERSION",
    "build_semantic_judge_registry",
    "collect_independent_judge_observations",
    "observation_to_evaluation_score",
    "register_semantic_judge_metrics",
    "semantic_judge_metric_definitions",
]