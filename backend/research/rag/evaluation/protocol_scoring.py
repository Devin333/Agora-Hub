"""Versioned score observations, metric registration, and completeness accounting.

The module intentionally owns Research-specific nullable/status semantics.  The
framework ``MetricValue`` type is only used as a projection of an already
validated, numeric observation.
"""

from __future__ import annotations

import json
import math
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Iterable, Literal

from pydantic import (
    Field,
    JsonValue,
    PrivateAttr,
    field_serializer,
    field_validator,
    model_validator,
)

from backend.research.rag.evaluation.protocol_common import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    EvaluationTrack,
    ExecutionProfile,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    canonical_json_bytes,
    content_digest,
)
from framework.rag.evaluation.report import MetricValue


_FLOATING_VERSION_ALIAS = "latest"


def _reject_floating_version(value: str) -> str:
    if value.casefold() == _FLOATING_VERSION_ALIAS:
        raise ValueError("identity and version fields must not use floating aliases")
    return value


def _deep_json_copy(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {key: _deep_json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deep_json_copy(item) for item in value]
    return value


class ScoreStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_REVIEW = "needs_review"


class ScoreSource(StrEnum):
    RULE = "rule"
    SEMANTIC_JUDGE = "semantic_judge"
    HUMAN = "human"
    REAL_USAGE = "real_usage"


class MetricGranularity(StrEnum):
    ATTEMPT = "attempt"
    CASE = "case"
    RUN = "run"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"
    NEUTRAL = "neutral"


class MetricValueKind(StrEnum):
    SCALAR = "scalar"
    RATIO = "ratio"


class MetricAggregation(StrEnum):
    MEAN = "mean"
    SUM = "sum"
    COUNT = "count"
    RATIO = "ratio"
    MEDIAN = "median"
    PERCENTILE = "percentile"


class MissingValuePolicy(StrEnum):
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_REVIEW = "needs_review"


class NumericDomain(ProtocolModel):
    """Closed/open finite numeric bounds for one metric semantic version."""

    minimum: float
    maximum: float
    minimum_inclusive: bool = True
    maximum_inclusive: bool = True

    @field_validator("minimum", "maximum", mode="before")
    @classmethod
    def _finite_non_bool_bound(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("domain bounds must be finite non-boolean numbers")
        if not math.isfinite(value):
            raise ValueError("domain bounds must be finite non-boolean numbers")
        return value

    @model_validator(mode="after")
    def _ordered_bounds(self) -> "NumericDomain":
        if self.minimum > self.maximum:
            raise ValueError("domain minimum must not exceed maximum")
        if self.minimum == self.maximum and not (
            self.minimum_inclusive and self.maximum_inclusive
        ):
            raise ValueError("an equal-bound domain must include both bounds")
        return self

    def contains(self, value: float) -> bool:
        lower = value >= self.minimum if self.minimum_inclusive else value > self.minimum
        upper = value <= self.maximum if self.maximum_inclusive else value < self.maximum
        return lower and upper


class MetricDefinition(ProtocolModel):
    """Immutable registry entry defining one metric semantic version."""

    metric_id: NonEmptyStr
    version: NonEmptyStr
    required_inputs: frozenset[NonEmptyStr]
    applicable_tracks: frozenset[EvaluationTrack]
    applicable_execution_profiles: frozenset[ExecutionProfile]
    applicable_scopes: frozenset[NonEmptyStr]
    granularity: MetricGranularity
    direction: MetricDirection
    domain: NumericDomain
    value_kind: MetricValueKind
    aggregation: MetricAggregation
    missing_policy: MissingValuePolicy

    @field_validator("version")
    @classmethod
    def _stable_metric_version(cls, value: str) -> str:
        return _reject_floating_version(value)

    @field_validator("granularity", mode="before")
    @classmethod
    def _parse_granularity(cls, value: Any) -> Any:
        return MetricGranularity(value) if isinstance(value, str) else value

    @field_validator("direction", mode="before")
    @classmethod
    def _parse_direction(cls, value: Any) -> Any:
        return MetricDirection(value) if isinstance(value, str) else value

    @field_validator("value_kind", mode="before")
    @classmethod
    def _parse_value_kind(cls, value: Any) -> Any:
        return MetricValueKind(value) if isinstance(value, str) else value

    @field_validator("aggregation", mode="before")
    @classmethod
    def _parse_aggregation(cls, value: Any) -> Any:
        return MetricAggregation(value) if isinstance(value, str) else value

    @field_validator("missing_policy", mode="before")
    @classmethod
    def _parse_missing_policy(cls, value: Any) -> Any:
        return MissingValuePolicy(value) if isinstance(value, str) else value

    @field_validator("applicable_tracks", mode="before")
    @classmethod
    def _parse_tracks(cls, value: Any) -> Any:
        if isinstance(value, frozenset):
            return frozenset(
                EvaluationTrack(item) if isinstance(item, str) else item
                for item in value
            )
        return value

    @field_validator("applicable_execution_profiles", mode="before")
    @classmethod
    def _parse_execution_profiles(cls, value: Any) -> Any:
        if isinstance(value, frozenset):
            return frozenset(
                ExecutionProfile(item) if isinstance(item, str) else item
                for item in value
            )
        return value

    @field_validator(
        "required_inputs",
        "applicable_tracks",
        "applicable_execution_profiles",
        "applicable_scopes",
    )
    @classmethod
    def _non_empty_sets(cls, value: frozenset[Any]) -> frozenset[Any]:
        if not value:
            raise ValueError("metric input and applicability sets must not be empty")
        return value

    @model_validator(mode="after")
    def _ratio_contract(self) -> "MetricDefinition":
        if self.value_kind is MetricValueKind.RATIO:
            if not self.domain.contains(0.0) or not self.domain.contains(1.0):
                raise ValueError("ratio metric domain must contain both 0 and 1")
        return self

    def applies_to(
        self,
        *,
        track: EvaluationTrack,
        execution_profile: ExecutionProfile,
        scope: str,
    ) -> bool:
        return (
            track in self.applicable_tracks
            and execution_profile in self.applicable_execution_profiles
            and scope in self.applicable_scopes
        )


class EvaluationScore(ProtocolModel):
    """A single auditable score observation.

    Every field is required at construction, including all protocol and metric
    versions.  A non-``ok`` observation never carries a numeric value, while a
    valid zero remains an ordinary measurement and is not treated as missing.
    Metric-specific bounds and ratio/scalar count rules are checked by
    :class:`MetricRegistry`.
    """

    record_type: Literal["score"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
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
    metric_id: NonEmptyStr
    version: NonEmptyStr
    value: float | None
    status: ScoreStatus
    numerator: int | None
    denominator: int | None
    source: ScoreSource
    provenance: dict[NonEmptyStr, JsonValue]
    provenance_digest: Sha256Digest
    evidence: tuple[NonEmptyStr, ...]
    reason: NonEmptyStr | None

    _provenance_canonical: bytes = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        object.__setattr__(
            self,
            "_provenance_canonical",
            canonical_json_bytes(self.provenance),
        )

    @field_validator(
        "dataset_version",
        "scoring_revision",
        "version",
    )
    @classmethod
    def _stable_versions(cls, value: str) -> str:
        return _reject_floating_version(value)

    @field_validator("track", mode="before")
    @classmethod
    def _parse_track(cls, value: Any) -> Any:
        return EvaluationTrack(value) if isinstance(value, str) else value

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _parse_execution_profile(cls, value: Any) -> Any:
        return ExecutionProfile(value) if isinstance(value, str) else value

    @field_validator("status", mode="before")
    @classmethod
    def _parse_status(cls, value: Any) -> Any:
        return ScoreStatus(value) if isinstance(value, str) else value

    @field_validator("source", mode="before")
    @classmethod
    def _parse_source(cls, value: Any) -> Any:
        return ScoreSource(value) if isinstance(value, str) else value

    @field_validator("value", mode="before")
    @classmethod
    def _finite_non_bool_value(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("score value must be a finite non-boolean number or null")
        if not math.isfinite(value):
            raise ValueError("score value must be a finite non-boolean number or null")
        return value

    @field_validator("numerator", "denominator", mode="before")
    @classmethod
    def _non_negative_count(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("score counts must be non-negative integers or null")
        return value

    @field_validator("provenance")
    @classmethod
    def _provenance_is_auditable(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        if not value:
            raise ValueError("score provenance must not be empty")
        return _deep_json_copy(value)  # detach the caller's mutable input

    @field_serializer("provenance")
    def _serialize_provenance(self, _value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return self.verified_provenance()

    def verified_provenance(self) -> dict[str, JsonValue]:
        snapshot = json.loads(self._provenance_canonical)
        if content_digest(snapshot) != self.provenance_digest:
            raise ValueError("provenance content does not match provenance_digest")
        if canonical_json_bytes(self.provenance) != self._provenance_canonical:
            raise ValueError("score provenance was mutated after validation")
        return snapshot

    @field_validator("evidence")
    @classmethod
    def _evidence_is_auditable(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("score evidence must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("score evidence references must be unique")
        return value

    @model_validator(mode="after")
    def _status_and_nullable_consistency(self) -> "EvaluationScore":
        self.verified_provenance()
        if self.status is ScoreStatus.OK:
            if self.value is None:
                raise ValueError("status=ok requires a numeric value")
        else:
            if self.value is not None:
                raise ValueError("non-ok score status requires value=null")
            if self.reason is None:
                raise ValueError("non-ok score status requires a reason")
        return self


class ScoreSlot(ProtocolModel):
    """One frozen, planned metric observation slot."""

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
    metric_id: NonEmptyStr
    version: NonEmptyStr
    applicable: bool

    @field_validator("dataset_version", "scoring_revision", "version")
    @classmethod
    def _stable_versions(cls, value: str) -> str:
        return _reject_floating_version(value)

    @field_validator("track", mode="before")
    @classmethod
    def _parse_track(cls, value: Any) -> Any:
        return EvaluationTrack(value) if isinstance(value, str) else value

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _parse_execution_profile(cls, value: Any) -> Any:
        return ExecutionProfile(value) if isinstance(value, str) else value

    @classmethod
    def from_score(cls, score: EvaluationScore, *, applicable: bool) -> "ScoreSlot":
        return cls(
            **{
                name: getattr(score, name)
                for name in cls.model_fields
                if name != "applicable"
            },
            applicable=applicable,
        )

    @property
    def identity(self) -> tuple[object, ...]:
        return tuple(getattr(self, name) for name in type(self).model_fields if name != "applicable")


class CompletenessSlotState(StrEnum):
    VALID = "valid"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    PENDING_REVIEW = "pending_review"
    MISSING = "missing"


class ScoreSlotResult(ProtocolModel):
    slot: ScoreSlot
    state: CompletenessSlotState
    score: EvaluationScore | None

    @model_validator(mode="after")
    def _consistent_slot(self) -> "ScoreSlotResult":
        if self.score is not None:
            self.score._status_and_nullable_consistency()
            identity = ScoreSlot.from_score(
                self.score, applicable=self.slot.applicable
            ).identity
            if identity != self.slot.identity:
                raise ValueError("score observation identity must match its planned slot")
            if self.slot.applicable and self.score.status is ScoreStatus.NOT_APPLICABLE:
                raise ValueError("applicable planned slot cannot be observed as not_applicable")
            if not self.slot.applicable and self.score.status is not ScoreStatus.NOT_APPLICABLE:
                raise ValueError("non-applicable planned slot requires not_applicable observation")

        if not self.slot.applicable:
            expected = CompletenessSlotState.NOT_APPLICABLE
        elif self.score is None:
            expected = CompletenessSlotState.MISSING
        else:
            expected = {
                ScoreStatus.OK: CompletenessSlotState.VALID,
                ScoreStatus.ERROR: CompletenessSlotState.ERROR,
                ScoreStatus.NEEDS_REVIEW: CompletenessSlotState.PENDING_REVIEW,
            }[self.score.status]
        if self.state is not expected:
            raise ValueError("score slot state must agree with planned applicability and observation")
        return self


class ScoreCompleteness(ProtocolModel):
    planned: int = Field(ge=0, strict=True)
    applicable: int = Field(ge=0, strict=True)
    valid: int = Field(ge=0, strict=True)
    error: int = Field(ge=0, strict=True)
    not_applicable: int = Field(ge=0, strict=True)
    pending_review: int = Field(ge=0, strict=True)
    missing: int = Field(ge=0, strict=True)
    slots: tuple[ScoreSlotResult, ...]

    @model_validator(mode="after")
    def _consistent_counts(self) -> "ScoreCompleteness":
        counts = {
            "planned": len(self.slots),
            "applicable": 0,
            "valid": 0,
            "error": 0,
            "not_applicable": 0,
            "pending_review": 0,
            "missing": 0,
        }
        identities: set[tuple[object, ...]] = set()
        for result in self.slots:
            result._consistent_slot()
            if result.slot.identity in identities:
                raise ValueError("duplicate planned score slot in completeness")
            identities.add(result.slot.identity)
            counts["applicable"] += int(result.slot.applicable)
            if result.state is CompletenessSlotState.MISSING:
                counts["missing"] += 1
                counts["error"] += 1
            else:
                counts[result.state.value] += 1
        for name, expected in counts.items():
            if getattr(self, name) != expected:
                raise ValueError(
                    f"{name} count must match retained score slots: expected {expected}"
                )
        return self

    @property
    def coverage(self) -> float | None:
        if self.applicable == 0:
            return None
        return self.valid / self.applicable


class EndToEndOutcome(StrEnum):
    KNOWN_SUCCESS = "known_success"
    KNOWN_FAILURE = "known_failure"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class EndToEndExecutionState(StrEnum):
    """Typed model-path states; classification never rewrites recorded values."""

    PLANNED = "planned"
    PREFLIGHT_REJECTED = "preflight_rejected"
    RUNNING = "running"
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MODEL_PATH_COMPLETED = "model_path_completed"
    RUNTIME_FAILED = "runtime_failed"
    RUNTIME_TIMEOUT = "runtime_timeout"
    PENDING = "pending"
    UNRUN = "unrun"

    @property
    def classification(self) -> "EndToEndExecutionState":
        if self is EndToEndExecutionState.MODEL_PATH_COMPLETED:
            return EndToEndExecutionState.COMPLETED
        if self in {
            EndToEndExecutionState.RUNTIME_FAILED,
            EndToEndExecutionState.RUNTIME_TIMEOUT,
        }:
            return EndToEndExecutionState.FAILED
        if self in {EndToEndExecutionState.PENDING, EndToEndExecutionState.UNRUN}:
            return EndToEndExecutionState.PLANNED
        return self


class EndToEndSlot(ProtocolModel):
    """One frozen end-to-end experiment unit, independent of judge metrics."""

    dataset_id: NonEmptyStr
    dataset_version: NonEmptyStr
    dataset_digest: Sha256Digest
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    attempt_id: NonEmptyStr
    track: Literal[EvaluationTrack.END_TO_END]
    execution_profile: ExecutionProfile
    variant_fingerprint: Sha256Digest
    repetition: int = Field(ge=1, strict=True)
    scope: NonEmptyStr
    applicable: bool

    @field_validator("dataset_version")
    @classmethod
    def _stable_dataset_version(cls, value: str) -> str:
        return _reject_floating_version(value)

    @field_validator("track", mode="before")
    @classmethod
    def _parse_track(cls, value: Any) -> Any:
        return EvaluationTrack(value) if isinstance(value, str) else value

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _parse_execution_profile(cls, value: Any) -> Any:
        return ExecutionProfile(value) if isinstance(value, str) else value

    @property
    def identity(self) -> tuple[object, ...]:
        return tuple(getattr(self, name) for name in type(self).model_fields if name != "applicable")


class EndToEndObservation(ProtocolModel):
    dataset_id: NonEmptyStr
    dataset_version: NonEmptyStr
    dataset_digest: Sha256Digest
    run_id: NonEmptyStr
    case_id: NonEmptyStr
    attempt_id: NonEmptyStr
    track: Literal[EvaluationTrack.END_TO_END]
    execution_profile: ExecutionProfile
    variant_fingerprint: Sha256Digest
    repetition: int = Field(ge=1, strict=True)
    scope: NonEmptyStr
    outcome: EndToEndOutcome
    execution_state: EndToEndExecutionState
    required_judge_error: bool
    failure_type: NonEmptyStr | None
    reason: NonEmptyStr | None

    @field_validator("dataset_version")
    @classmethod
    def _stable_dataset_version(cls, value: str) -> str:
        return _reject_floating_version(value)

    @field_validator("track", mode="before")
    @classmethod
    def _parse_track(cls, value: Any) -> Any:
        return EvaluationTrack(value) if isinstance(value, str) else value

    @field_validator("execution_profile", mode="before")
    @classmethod
    def _parse_execution_profile(cls, value: Any) -> Any:
        return ExecutionProfile(value) if isinstance(value, str) else value

    @field_validator("execution_state", mode="before")
    @classmethod
    def _parse_execution_state(cls, value: Any) -> Any:
        return EndToEndExecutionState(value) if isinstance(value, str) else value

    @field_validator("outcome", mode="before")
    @classmethod
    def _parse_outcome(cls, value: Any) -> Any:
        return EndToEndOutcome(value) if isinstance(value, str) else value

    @property
    def identity(self) -> tuple[object, ...]:
        return tuple(
            getattr(self, name)
            for name in EndToEndSlot.model_fields
            if name != "applicable"
        )

    @model_validator(mode="after")
    def _preserve_failure_state(self) -> "EndToEndObservation":
        execution = self.execution_state.classification
        if execution is EndToEndExecutionState.FAILED:
            if self.outcome is not EndToEndOutcome.KNOWN_FAILURE:
                raise ValueError("confirmed runtime failure must remain known_failure")
            if self.required_judge_error:
                raise ValueError("confirmed runtime failure is not a required judge error")
        elif execution is not EndToEndExecutionState.COMPLETED:
            if self.outcome not in {
                EndToEndOutcome.UNKNOWN,
                EndToEndOutcome.NOT_APPLICABLE,
            }:
                raise ValueError("pending or unrun model paths must remain unknown")

        if self.required_judge_error:
            if execution is not EndToEndExecutionState.COMPLETED:
                raise ValueError("required judge errors require completed model-path execution")
            if self.outcome is not EndToEndOutcome.UNKNOWN:
                raise ValueError("required judge errors must remain unknown")
        # The existing judge failure-type namespace is a recorded machine
        # code, never a guess from the human-readable reason.
        judge_failure = (
            self.failure_type is not None
            and self.failure_type.casefold().startswith("judge")
        )
        if self.required_judge_error != judge_failure:
            raise ValueError("required_judge_error must agree with the typed judge failure")

        if self.outcome is EndToEndOutcome.KNOWN_SUCCESS:
            if execution is not EndToEndExecutionState.COMPLETED:
                raise ValueError("known success requires completed model-path execution")
            if self.failure_type is not None or self.reason is not None:
                raise ValueError("known success cannot carry failure information")
        elif self.outcome is EndToEndOutcome.NOT_APPLICABLE:
            if self.reason is None:
                raise ValueError("not_applicable outcome requires a reason")
            if self.failure_type is not None:
                raise ValueError("not_applicable outcome cannot carry failure information")
        elif self.failure_type is None or self.reason is None:
            raise ValueError("known failure and unknown outcomes require failure_type and reason")
        return self


class EndToEndSlotResult(ProtocolModel):
    slot: EndToEndSlot
    observation: EndToEndObservation | None
    outcome: EndToEndOutcome

    @model_validator(mode="after")
    def _consistent_slot(self) -> "EndToEndSlotResult":
        if self.observation is not None:
            self.observation._preserve_failure_state()
            if self.observation.identity != self.slot.identity:
                raise ValueError("end-to-end observation identity must match its planned slot")
            if (
                self.slot.applicable
                and self.observation.outcome is EndToEndOutcome.NOT_APPLICABLE
            ):
                raise ValueError("applicable end-to-end slot cannot be not_applicable")
            if (
                not self.slot.applicable
                and self.observation.outcome is not EndToEndOutcome.NOT_APPLICABLE
            ):
                raise ValueError("non-applicable end-to-end slot requires not_applicable outcome")
            expected = self.observation.outcome
        else:
            expected = (
                EndToEndOutcome.UNKNOWN
                if self.slot.applicable
                else EndToEndOutcome.NOT_APPLICABLE
            )
        if self.outcome is not expected:
            raise ValueError("end-to-end slot outcome must agree with its observation and plan")
        return self


class EndToEndSummary(ProtocolModel):
    planned: int = Field(ge=0, strict=True)
    applicable: int = Field(ge=0, strict=True)
    known_success: int = Field(ge=0, strict=True)
    known_failure: int = Field(ge=0, strict=True)
    unknown: int = Field(ge=0, strict=True)
    not_applicable: int = Field(ge=0, strict=True)
    slots: tuple[EndToEndSlotResult, ...]

    @model_validator(mode="after")
    def _consistent_counts(self) -> "EndToEndSummary":
        counts = {
            "planned": len(self.slots),
            "applicable": 0,
            "known_success": 0,
            "known_failure": 0,
            "unknown": 0,
            "not_applicable": 0,
        }
        identities: set[tuple[object, ...]] = set()
        for result in self.slots:
            result._consistent_slot()
            if result.slot.identity in identities:
                raise ValueError("duplicate planned end-to-end slot in summary")
            identities.add(result.slot.identity)
            counts["applicable"] += int(result.slot.applicable)
            counts[result.outcome.value] += 1
        for name, expected in counts.items():
            if getattr(self, name) != expected:
                raise ValueError(
                    f"{name} count must match retained end-to-end slots: expected {expected}"
                )
        return self

    @property
    def is_complete(self) -> bool:
        return self.unknown == 0

    @property
    def success_rate(self) -> float | None:
        if self.applicable == 0 or not self.is_complete:
            return None
        return self.known_success / self.applicable

    @property
    def verified_success_lower_bound(self) -> float | None:
        if self.applicable == 0:
            return None
        return self.known_success / self.applicable


class MetricRegistry:
    """Explicit registry and the only projection path to ``MetricValue``."""

    def __init__(self, definitions: Iterable[MetricDefinition]) -> None:
        entries: dict[tuple[str, str], MetricDefinition] = {}
        for definition in definitions:
            key = (definition.metric_id, definition.version)
            if key in entries:
                raise ValueError(f"duplicate metric definition: {key[0]}@{key[1]}")
            entries[key] = definition
        if not entries:
            raise ValueError("metric registry must contain at least one definition")
        self._definitions = MappingProxyType(entries)

    @property
    def definitions(self) -> tuple[MetricDefinition, ...]:
        return tuple(self._definitions.values())

    def register(self, definition: MetricDefinition) -> None:
        key = (definition.metric_id, definition.version)
        if key in self._definitions:
            raise ValueError(f"duplicate metric definition: {key[0]}@{key[1]}")
        entries = dict(self._definitions)
        entries[key] = definition
        self._definitions = MappingProxyType(entries)

    def definition(self, metric_id: str, version: str) -> MetricDefinition:
        try:
            return self._definitions[(metric_id, version)]
        except KeyError as exc:
            raise ValueError(f"unknown metric definition: {metric_id}@{version}") from exc

    def validate_score(self, score: EvaluationScore) -> MetricDefinition:
        score._status_and_nullable_consistency()
        definition = self.definition(score.metric_id, score.version)
        applies = definition.applies_to(
            track=score.track,
            execution_profile=score.execution_profile,
            scope=score.scope,
        )
        if not applies and score.status is not ScoreStatus.NOT_APPLICABLE:
            raise ValueError(
                f"metric {score.metric_id}@{score.version} is not applicable to "
                f"{score.track}/{score.execution_profile}/{score.scope}"
            )

        if score.status is ScoreStatus.OK:
            assert score.value is not None
            if not definition.domain.contains(score.value):
                raise ValueError(
                    f"metric value {score.value} is outside the registered domain for "
                    f"{score.metric_id}@{score.version}"
                )

        if definition.value_kind is MetricValueKind.RATIO:
            self._validate_ratio_counts(score)
        elif score.numerator is not None or score.denominator is not None:
            raise ValueError("non-ratio metrics must not carry numerator or denominator")
        return definition

    @staticmethod
    def _validate_ratio_counts(score: EvaluationScore) -> None:
        numerator, denominator = score.numerator, score.denominator
        if denominator is None:
            if numerator is not None:
                raise ValueError("a ratio numerator requires a known denominator")
            if score.status is ScoreStatus.OK:
                raise ValueError("ok ratio metrics require actual numerator and denominator")
            # Unknown semantic counts stay null.  The planned experiment
            # denominator is accounted for separately by ScoreCompleteness.
            return

        if denominator == 0:
            if numerator != 0:
                raise ValueError("a zero denominator requires a zero numerator")
            if score.status is not ScoreStatus.NOT_APPLICABLE:
                raise ValueError(
                    "a zero-denominator ratio must be not_applicable with value=null"
                )
            return

        if numerator is not None and numerator > denominator:
            raise ValueError("ratio numerator must not exceed denominator")
        if score.status is ScoreStatus.OK:
            if numerator is None:
                raise ValueError("ok ratio metrics require actual numerator and denominator")
            assert score.value is not None
            expected = numerator / denominator
            if not math.isclose(score.value, expected, rel_tol=1e-12, abs_tol=1e-12):
                raise ValueError("ratio value must equal numerator / denominator")

    def to_metric_value(self, score: EvaluationScore) -> MetricValue | None:
        self.validate_score(score)
        if score.status is not ScoreStatus.OK:
            return None
        assert score.value is not None
        return MetricValue(
            score.metric_id,
            score.value,
            {
                "metric_version": score.version,
                "scoring_revision": score.scoring_revision,
                "run_id": score.run_id,
                "case_id": score.case_id,
                "attempt_id": score.attempt_id,
                "track": score.track.value,
                "execution_profile": score.execution_profile.value,
                "scope": score.scope,
                "source": score.source.value,
                "numerator": score.numerator,
                "denominator": score.denominator,
            },
        )

    def ensure_comparable(self, scores: Iterable[EvaluationScore]) -> None:
        versions: dict[str, set[str]] = {}
        for score in scores:
            self.validate_score(score)
            versions.setdefault(score.metric_id, set()).add(score.version)
        incompatible = {
            metric_id: metric_versions
            for metric_id, metric_versions in versions.items()
            if len(metric_versions) > 1
        }
        if incompatible:
            details = ", ".join(
                f"{metric_id}={sorted(metric_versions)}"
                for metric_id, metric_versions in sorted(incompatible.items())
            )
            raise ValueError(f"incomparable metric versions: {details}")

    def summarize_completeness(
        self,
        planned_slots: Iterable[ScoreSlot],
        scores: Iterable[EvaluationScore],
    ) -> ScoreCompleteness:
        slots = tuple(planned_slots)
        planned_versions: dict[str, set[str]] = {}
        slot_by_identity: dict[tuple[object, ...], ScoreSlot] = {}
        for slot in slots:
            if slot.identity in slot_by_identity:
                raise ValueError("duplicate planned score slot")
            definition = self.definition(slot.metric_id, slot.version)
            definition_applies = definition.applies_to(
                track=slot.track,
                execution_profile=slot.execution_profile,
                scope=slot.scope,
            )
            if slot.applicable and not definition_applies:
                raise ValueError("dangling planned slot outside registered applicability")
            planned_versions.setdefault(slot.metric_id, set()).add(slot.version)
            slot_by_identity[slot.identity] = slot
        if any(len(versions) > 1 for versions in planned_versions.values()):
            raise ValueError("incomparable metric versions in planned score slots")

        score_by_identity: dict[tuple[object, ...], EvaluationScore] = {}
        score_list = tuple(scores)
        self.ensure_comparable(score_list)
        for score in score_list:
            identity = ScoreSlot.from_score(score, applicable=True).identity
            if identity not in slot_by_identity:
                raise ValueError("score references an unknown planned slot")
            if identity in score_by_identity:
                raise ValueError("duplicate score observation for planned slot")
            slot = slot_by_identity[identity]
            if slot.applicable and score.status is ScoreStatus.NOT_APPLICABLE:
                raise ValueError("applicable planned slot cannot be observed as not_applicable")
            if not slot.applicable and score.status is not ScoreStatus.NOT_APPLICABLE:
                raise ValueError("non-applicable planned slot requires not_applicable observation")
            score_by_identity[identity] = score

        results: list[ScoreSlotResult] = []
        valid = observed_error = pending = missing = 0
        for slot in slots:
            score = score_by_identity.get(slot.identity)
            if not slot.applicable:
                results.append(
                    ScoreSlotResult(
                        slot=slot,
                        state=CompletenessSlotState.NOT_APPLICABLE,
                        score=score,
                    )
                )
            elif score is None:
                missing += 1
                results.append(
                    ScoreSlotResult(
                        slot=slot,
                        state=CompletenessSlotState.MISSING,
                        score=None,
                    )
                )
            elif score.status is ScoreStatus.OK:
                valid += 1
                results.append(
                    ScoreSlotResult(slot=slot, state=CompletenessSlotState.VALID, score=score)
                )
            elif score.status is ScoreStatus.ERROR:
                observed_error += 1
                results.append(
                    ScoreSlotResult(slot=slot, state=CompletenessSlotState.ERROR, score=score)
                )
            elif score.status is ScoreStatus.NEEDS_REVIEW:
                pending += 1
                results.append(
                    ScoreSlotResult(
                        slot=slot,
                        state=CompletenessSlotState.PENDING_REVIEW,
                        score=score,
                    )
                )
            else:  # guarded above, retained as a defensive invariant
                raise ValueError("unexpected not_applicable score for applicable slot")

        applicable = sum(slot.applicable for slot in slots)
        not_applicable = len(slots) - applicable
        return ScoreCompleteness(
            planned=len(slots),
            applicable=applicable,
            valid=valid,
            error=observed_error + missing,
            not_applicable=not_applicable,
            pending_review=pending,
            missing=missing,
            slots=tuple(results),
        )


def summarize_end_to_end(
    planned_slots: Iterable[EndToEndSlot],
    observations: Iterable[EndToEndObservation],
) -> EndToEndSummary:
    """Aggregate model-path outcomes without conflating judge unknowns.

    A missing applicable observation is retained as ``unknown``.  A caller must
    explicitly classify a runtime failure as ``known_failure`` and a judge
    failure as ``unknown``; their ``failure_type`` and ``execution_state`` stay
    attached to the corresponding slot result.
    """

    slots = tuple(planned_slots)
    slot_by_identity: dict[tuple[object, ...], EndToEndSlot] = {}
    for slot in slots:
        if slot.identity in slot_by_identity:
            raise ValueError("duplicate planned end-to-end slot")
        slot_by_identity[slot.identity] = slot

    observation_by_identity: dict[tuple[object, ...], EndToEndObservation] = {}
    for observation in observations:
        if observation.identity not in slot_by_identity:
            raise ValueError("end-to-end observation references an unknown planned slot")
        if observation.identity in observation_by_identity:
            raise ValueError("duplicate end-to-end observation for planned slot")
        observation._preserve_failure_state()
        slot = slot_by_identity[observation.identity]
        if slot.applicable and observation.outcome is EndToEndOutcome.NOT_APPLICABLE:
            raise ValueError("applicable end-to-end slot cannot be not_applicable")
        if not slot.applicable and observation.outcome is not EndToEndOutcome.NOT_APPLICABLE:
            raise ValueError("non-applicable end-to-end slot requires not_applicable outcome")
        observation_by_identity[observation.identity] = observation

    results: list[EndToEndSlotResult] = []
    known_success = known_failure = unknown = 0
    for slot in slots:
        observation = observation_by_identity.get(slot.identity)
        if not slot.applicable:
            outcome = EndToEndOutcome.NOT_APPLICABLE
        elif observation is None:
            outcome = EndToEndOutcome.UNKNOWN
            unknown += 1
        else:
            outcome = observation.outcome
            if outcome is EndToEndOutcome.KNOWN_SUCCESS:
                known_success += 1
            elif outcome is EndToEndOutcome.KNOWN_FAILURE:
                known_failure += 1
            elif outcome is EndToEndOutcome.UNKNOWN:
                unknown += 1
            else:  # guarded above
                raise ValueError("unexpected not_applicable applicable outcome")
        results.append(EndToEndSlotResult(slot=slot, observation=observation, outcome=outcome))

    applicable = sum(slot.applicable for slot in slots)
    return EndToEndSummary(
        planned=len(slots),
        applicable=applicable,
        known_success=known_success,
        known_failure=known_failure,
        unknown=unknown,
        not_applicable=len(slots) - applicable,
        slots=tuple(results),
    )


__all__ = [
    "CompletenessSlotState",
    "EndToEndExecutionState",
    "EndToEndObservation",
    "EndToEndOutcome",
    "EndToEndSlot",
    "EndToEndSlotResult",
    "EndToEndSummary",
    "EvaluationScore",
    "EvaluationTrack",
    "ExecutionProfile",
    "MetricAggregation",
    "MetricDefinition",
    "MetricDirection",
    "MetricGranularity",
    "MetricRegistry",
    "MetricValueKind",
    "MissingValuePolicy",
    "NumericDomain",
    "ScoreCompleteness",
    "ScoreSlot",
    "ScoreSlotResult",
    "ScoreSource",
    "ScoreStatus",
    "summarize_end_to_end",
]
