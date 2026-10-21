"""Research-owned contracts for optional semantic evaluation judges.

The contracts deliberately keep third-party metric libraries and provider clients
outside ``backend.research``. A judge may only obtain model candidates through
an injected bounded port, and every subrequest must be emitted to the audit
callback. These observations are evidence; they never decide routing, gates,
publication, or skill promotion.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
    content_digest,
)


class JudgeMetric(StrEnum):
    FACT_PRECISION = "fact_precision"
    FACT_RECALL = "fact_recall"
    FACT_F1 = "fact_f1"
    FAITHFULNESS = "faithfulness"
    CITATION_SUPPORT = "citation_support"


class JudgeScope(StrEnum):
    TEXT_SUBSET = "text_subset"
    REFERENCE_COMPARISON = "reference_comparison"


class JudgeModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    TABLE = "table"
    FORMULA = "formula"


class JudgeRuntimeProfile(StrEnum):
    FIXTURE = "fixture"
    PRODUCTION = "production"


class JudgeObservationStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    NEEDS_REVIEW = "needs_review"


class JudgeErrorCode(StrEnum):
    JUDGE_UNAVAILABLE = "judge_unavailable"
    DEPENDENCY_VERSION_MISMATCH = "dependency_version_mismatch"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    INVALID_OUTPUT = "invalid_output"
    TIMEOUT = "timeout"
    RETRY_EXHAUSTED = "retry_exhausted"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BUDGET_HOOK_REJECTED = "shared_budget_hook_rejected"
    CANDIDATE_CALL_FAILED = "candidate_call_failed"
    USAGE_UNAVAILABLE = "usage_unavailable"
    NO_VERIFIABLE_CLAIMS = "no_verifiable_claims"


class JudgeCapability(ProtocolModel):
    modalities: frozenset[JudgeModality]
    scopes: frozenset[JudgeScope]

    @field_validator("modalities", "scopes")
    @classmethod
    def _non_empty(cls, value: frozenset[Any]) -> frozenset[Any]:
        if not value:
            raise ValueError("judge capability sets must not be empty")
        return value


class JudgeBudget(ProtocolModel):
    """Budget shared by every Ragas subcall for one semantic observation."""

    max_requests: int = Field(gt=0, strict=True)
    max_total_tokens: int = Field(gt=0, strict=True)
    max_output_tokens_per_request: int = Field(gt=0, strict=True)
    max_cost: float | None
    currency: NonEmptyStr | None
    unknown_price_reason: NonEmptyStr | None
    request_timeout_seconds: float = Field(gt=0.0, strict=True)
    deadline_seconds: float = Field(gt=0.0, strict=True)
    max_retries_per_subrequest: int = Field(ge=0, strict=True)

    @field_validator("max_cost", mode="before")
    @classmethod
    def _valid_cost(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("max_cost must be a non-negative finite number or null")
        if value == float("inf") or value != value:
            raise ValueError("max_cost must be a non-negative finite number or null")
        return float(value)

    @model_validator(mode="after")
    def _known_or_unknown_price(self) -> "JudgeBudget":
        if self.max_cost is None:
            if self.currency is not None or self.unknown_price_reason is None:
                raise ValueError(
                    "unknown judge pricing requires currency=null and unknown_price_reason"
                )
        elif self.currency is None or self.unknown_price_reason is not None:
            raise ValueError(
                "known judge pricing requires currency and unknown_price_reason=null"
            )
        if self.request_timeout_seconds > self.deadline_seconds:
            raise ValueError("request timeout must not exceed the observation deadline")
        return self


class CandidateUsage(ProtocolModel):
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)
    cost: float | None
    currency: NonEmptyStr | None
    cost_unknown_reason: NonEmptyStr | None

    @field_validator("cost", mode="before")
    @classmethod
    def _finite_cost(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("cost must be a non-negative finite number or null")
        if value == float("inf") or value != value:
            raise ValueError("cost must be a non-negative finite number or null")
        return float(value)

    @model_validator(mode="after")
    def _consistent_usage(self) -> "CandidateUsage":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if self.cost is None:
            if self.currency is not None or self.cost_unknown_reason is None:
                raise ValueError(
                    "unknown call cost requires currency=null and cost_unknown_reason"
                )
        elif self.currency is None or self.cost_unknown_reason is not None:
            raise ValueError(
                "known call cost requires currency and cost_unknown_reason=null"
            )
        return self


class JudgeUsageSummary(ProtocolModel):
    requests: int = Field(ge=0, strict=True)
    input_tokens: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)
    cost: float | None
    currency: NonEmptyStr | None
    cost_unknown_reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _consistent_summary(self) -> "JudgeUsageSummary":
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if self.cost is None:
            if self.currency is not None or self.cost_unknown_reason is None:
                raise ValueError(
                    "unknown aggregate cost requires currency=null and cost_unknown_reason"
                )
        elif self.currency is None or self.cost_unknown_reason is not None:
            raise ValueError(
                "known aggregate cost requires currency and cost_unknown_reason=null"
            )
        return self


class JudgeCandidateRequest(ProtocolModel):
    request_id: NonEmptyStr
    observation_id: NonEmptyStr
    metric: JudgeMetric
    task: NonEmptyStr
    prompt: NonEmptyStr
    prompt_digest: Sha256Digest
    response_schema: dict[NonEmptyStr, JsonValue]
    response_schema_digest: Sha256Digest
    schema_name: NonEmptyStr
    schema_version: ImmutableVersionStr
    generation_parameters: dict[NonEmptyStr, JsonValue]
    timeout_seconds: float = Field(gt=0.0, strict=True)
    max_transport_attempts: Literal[1]
    max_output_tokens: int = Field(gt=0, strict=True)
    remaining_total_tokens: int = Field(gt=0, strict=True)
    remaining_cost: float | None
    currency: NonEmptyStr | None

    @model_validator(mode="after")
    def _verify_content_identity(self) -> "JudgeCandidateRequest":
        if content_digest(self.prompt) != self.prompt_digest:
            raise ValueError("prompt_digest does not match prompt")
        if content_digest(self.response_schema) != self.response_schema_digest:
            raise ValueError("response_schema_digest does not match response_schema")
        if self.remaining_cost is None and self.currency is not None:
            raise ValueError("unknown remaining cost requires currency=null")
        if self.remaining_cost is not None and self.currency is None:
            raise ValueError("known remaining cost requires currency")
        return self


class JudgeCandidateResponse(ProtocolModel):
    raw_output: NonEmptyStr
    provider: NonEmptyStr
    model_id: NonEmptyStr
    model_revision: NonEmptyStr | None
    model_revision_unavailable_reason: NonEmptyStr | None
    runtime_profile: JudgeRuntimeProfile
    usage: CandidateUsage
    finish_reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _revision_is_explicit(self) -> "JudgeCandidateResponse":
        unavailable = self.model_revision is None
        if unavailable != (self.model_revision_unavailable_reason is not None):
            raise ValueError(
                "model_revision_unavailable_reason is required exactly when revision is null"
            )
        return self


class JudgeBudgetHookRequest(ProtocolModel):
    budget_key: NonEmptyStr
    observation_id: NonEmptyStr
    request_id: NonEmptyStr
    metric: JudgeMetric
    attempt: int = Field(ge=1, strict=True)
    requested_token_limit: int = Field(gt=0, strict=True)
    requested_cost_limit: float | None
    currency: NonEmptyStr | None
    deadline_seconds_remaining: float = Field(gt=0.0, strict=True)


class JudgeBudgetHookDecision(ProtocolModel):
    allowed: bool
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _reason_for_rejection(self) -> "JudgeBudgetHookDecision":
        if self.allowed == (self.reason is not None):
            raise ValueError("budget hook rejection requires a reason and approval forbids one")
        return self


class JudgeBudgetHookRecord(ProtocolModel):
    budget_key: NonEmptyStr
    observation_id: NonEmptyStr
    request_id: NonEmptyStr
    metric: JudgeMetric
    attempt: int = Field(ge=1, strict=True)
    usage: CandidateUsage | None
    error_code: JudgeErrorCode | None
    error_message: NonEmptyStr | None

    @model_validator(mode="after")
    def _outcome_is_explicit(self) -> "JudgeBudgetHookRecord":
        failure = self.error_code is not None or self.error_message is not None
        if failure and (self.error_code is None or self.error_message is None):
            raise ValueError("budget hook failure requires both error_code and error_message")
        if not failure and self.usage is None:
            raise ValueError("successful budget hook record requires usage")
        return self


class JudgeSubrequestRecord(ProtocolModel):
    request_id: NonEmptyStr
    metric: JudgeMetric
    task: NonEmptyStr
    attempt: int = Field(ge=1, strict=True)
    prompt: NonEmptyStr
    prompt_digest: Sha256Digest
    response_schema: dict[NonEmptyStr, JsonValue]
    response_schema_digest: Sha256Digest
    schema_name: NonEmptyStr
    schema_version: ImmutableVersionStr
    raw_output: str | None
    parsed_output: dict[str, JsonValue] | None
    provider: NonEmptyStr | None
    model_id: NonEmptyStr | None
    model_revision: NonEmptyStr | None
    model_revision_unavailable_reason: NonEmptyStr | None
    runtime_profile: JudgeRuntimeProfile | None
    usage: CandidateUsage | None
    duration_ms: float = Field(ge=0.0, strict=True)
    error_code: JudgeErrorCode | None
    error_message: NonEmptyStr | None
    retryable: bool | None

    @model_validator(mode="after")
    def _success_or_error(self) -> "JudgeSubrequestRecord":
        success = self.parsed_output is not None and self.usage is not None
        failure = self.error_code is not None and self.error_message is not None
        if success == failure:
            raise ValueError("subrequest record requires exactly one success or error")
        if success:
            required = (self.raw_output, self.provider, self.model_id, self.runtime_profile)
            if any(value is None for value in required):
                raise ValueError("successful subrequest requires raw output and runtime identity")
            unavailable = self.model_revision is None
            if unavailable != (self.model_revision_unavailable_reason is not None):
                raise ValueError("successful subrequest requires explicit revision availability")
            if self.retryable is not None:
                raise ValueError("successful subrequest must use retryable=null")
        elif self.retryable is None:
            raise ValueError("failed subrequest requires retryable")
        return self


class JudgeError(ProtocolModel):
    code: JudgeErrorCode
    stage: NonEmptyStr
    message: NonEmptyStr
    attempts: int = Field(ge=0, strict=True)
    retryable: bool
    budget_exhausted: bool


class TextContext(ProtocolModel):
    context_id: NonEmptyStr
    text: NonEmptyStr
    text_digest: Sha256Digest

    @model_validator(mode="after")
    def _verify_text(self) -> "TextContext":
        if content_digest(self.text) != self.text_digest:
            raise ValueError("text_digest does not match context text")
        return self


class AnswerSpan(ProtocolModel):
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    text: NonEmptyStr

    @model_validator(mode="after")
    def _ordered(self) -> "AnswerSpan":
        if self.end <= self.start:
            raise ValueError("answer span end must be greater than start")
        return self


class CitationLocation(ProtocolModel):
    source_id: NonEmptyStr
    anchor_id: NonEmptyStr
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    quoted_text: NonEmptyStr
    quoted_text_digest: Sha256Digest

    @model_validator(mode="after")
    def _exact_location(self) -> "CitationLocation":
        if self.end <= self.start:
            raise ValueError("citation location end must be greater than start")
        if content_digest(self.quoted_text) != self.quoted_text_digest:
            raise ValueError("quoted_text_digest does not match exact citation text")
        return self


class CitationBinding(ProtocolModel):
    claim_id: NonEmptyStr
    claim_text: NonEmptyStr
    answer_span: AnswerSpan
    citation: CitationLocation


class SemanticJudgeRequestBase(ProtocolModel):
    observation_id: NonEmptyStr
    metric: JudgeMetric
    metric_version: ImmutableVersionStr
    execution_profile: JudgeRuntimeProfile
    scope: JudgeScope
    required_modalities: frozenset[JudgeModality]
    budget: JudgeBudget
    shared_budget_key: NonEmptyStr | None
    generation_parameters: dict[NonEmptyStr, JsonValue]
    evidence_references: tuple[NonEmptyStr, ...]

    @field_validator("required_modalities")
    @classmethod
    def _modalities_required(cls, value: frozenset[JudgeModality]) -> frozenset[JudgeModality]:
        if not value:
            raise ValueError("required_modalities must not be empty")
        return value

    @field_validator("evidence_references")
    @classmethod
    def _evidence_required(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("evidence_references must be non-empty and unique")
        return value


class FactualJudgeRequest(SemanticJudgeRequestBase):
    scope: Literal[JudgeScope.REFERENCE_COMPARISON]
    metric: Literal[
        JudgeMetric.FACT_PRECISION,
        JudgeMetric.FACT_RECALL,
        JudgeMetric.FACT_F1,
    ]
    response: NonEmptyStr
    reference: NonEmptyStr
    response_digest: Sha256Digest
    reference_digest: Sha256Digest
    atomicity: Literal["low", "high"] = "high"
    coverage: Literal["low", "high"] = "high"

    @model_validator(mode="after")
    def _isolated_fact_input(self) -> "FactualJudgeRequest":
        if content_digest(self.response) != self.response_digest:
            raise ValueError("response_digest does not match response")
        if content_digest(self.reference) != self.reference_digest:
            raise ValueError("reference_digest does not match reference")
        return self


class FaithfulnessJudgeRequest(SemanticJudgeRequestBase):
    scope: Literal[JudgeScope.TEXT_SUBSET]
    metric: Literal[JudgeMetric.FAITHFULNESS]
    user_input: NonEmptyStr
    response: NonEmptyStr
    actual_contexts: tuple[TextContext, ...]
    user_input_digest: Sha256Digest
    response_digest: Sha256Digest

    @model_validator(mode="after")
    def _actual_context_only(self) -> "FaithfulnessJudgeRequest":
        if not self.actual_contexts:
            raise ValueError("faithfulness requires actual model contexts")
        if len({item.context_id for item in self.actual_contexts}) != len(self.actual_contexts):
            raise ValueError("actual context ids must be unique")
        if content_digest(self.user_input) != self.user_input_digest:
            raise ValueError("user_input_digest does not match user_input")
        if content_digest(self.response) != self.response_digest:
            raise ValueError("response_digest does not match response")
        return self


class CitationSupportJudgeRequest(SemanticJudgeRequestBase):
    scope: Literal[JudgeScope.TEXT_SUBSET]
    metric: Literal[JudgeMetric.CITATION_SUPPORT]
    binding: CitationBinding
    binding_digest: Sha256Digest
    rubric: NonEmptyStr
    rubric_version: ImmutableVersionStr
    rubric_digest: Sha256Digest

    @model_validator(mode="after")
    def _exact_binding_only(self) -> "CitationSupportJudgeRequest":
        if content_digest(self.binding) != self.binding_digest:
            raise ValueError("binding_digest does not match claim/citation binding")
        if content_digest(self.rubric) != self.rubric_digest:
            raise ValueError("rubric_digest does not match citation rubric")
        return self


SemanticJudgeRequest = Annotated[
    FactualJudgeRequest | FaithfulnessJudgeRequest | CitationSupportJudgeRequest,
    Field(discriminator="metric"),
]


class CitationSupportCandidate(ProtocolModel):
    claim_id: NonEmptyStr
    supported: bool
    reason: NonEmptyStr
    cited_text: NonEmptyStr
    conditions_considered: tuple[NonEmptyStr, ...]

    @field_validator("conditions_considered", mode="before")
    @classmethod
    def _json_array_to_tuple(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


class SemanticJudgeObservation(ProtocolModel):
    observation_id: NonEmptyStr
    metric: JudgeMetric
    metric_version: ImmutableVersionStr
    status: JudgeObservationStatus
    value: float | None
    numerator: int | None
    denominator: int | None
    scope: JudgeScope
    capability: JudgeCapability
    execution_profile: JudgeRuntimeProfile
    provider: NonEmptyStr | None
    model_id: NonEmptyStr | None
    model_revision: NonEmptyStr | None
    model_revision_unavailable_reason: NonEmptyStr | None
    adapter_id: NonEmptyStr
    adapter_version: ImmutableVersionStr
    library_name: NonEmptyStr
    library_version: ImmutableVersionStr | None
    library_unavailable_reason: NonEmptyStr | None
    prompt_version: ImmutableVersionStr
    rubric_version: ImmutableVersionStr | None
    rubric_digest: Sha256Digest | None
    generation_parameters: dict[NonEmptyStr, JsonValue]
    input_digests: dict[NonEmptyStr, Sha256Digest]
    subrequests: tuple[JudgeSubrequestRecord, ...]
    raw_judgment: dict[NonEmptyStr, JsonValue]
    reasons: tuple[NonEmptyStr, ...]
    usage: JudgeUsageSummary
    evidence_references: tuple[NonEmptyStr, ...]
    error: JudgeError | None
    shared_budget_hook_connected: bool
    scope_limitation: NonEmptyStr

    @field_validator("value", mode="before")
    @classmethod
    def _finite_score(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("judge value must be a finite number or null")
        value = float(value)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("judge value must be a finite number or null")
        if not 0.0 <= value <= 1.0:
            raise ValueError("judge value must be within [0, 1]")
        return value

    @field_validator("numerator", "denominator", mode="before")
    @classmethod
    def _counts(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("judge counts must be non-negative integers or null")
        return value

    @model_validator(mode="after")
    def _consistent_observation(self) -> "SemanticJudgeObservation":
        if not self.evidence_references or len(set(self.evidence_references)) != len(
            self.evidence_references
        ):
            raise ValueError("observation evidence references must be non-empty and unique")
        if self.status is JudgeObservationStatus.OK:
            if self.value is None or self.error is not None:
                raise ValueError("ok observation requires value and forbids error")
            if self.denominator is not None:
                if self.denominator <= 0 or self.numerator is None:
                    raise ValueError("measured ratio requires positive denominator and numerator")
                if self.numerator > self.denominator:
                    raise ValueError("numerator must not exceed denominator")
                if self.metric is not JudgeMetric.FACT_F1:
                    expected = self.numerator / self.denominator
                    if abs(self.value - expected) > 1e-12:
                        raise ValueError("ratio value must equal numerator / denominator")
            elif self.numerator is not None:
                raise ValueError("numerator requires denominator")
        else:
            if self.value is not None or self.numerator is not None or self.denominator is not None:
                raise ValueError("non-ok observation requires null value and counts")
            if not self.reasons:
                raise ValueError("non-ok observation requires a reason")
            if self.status is JudgeObservationStatus.ERROR and self.error is None:
                raise ValueError("error observation requires structured error")
            if self.status is not JudgeObservationStatus.ERROR and self.error is not None:
                raise ValueError("only error observation may carry structured error")
        runtime_known = self.provider is not None and self.model_id is not None
        if runtime_known:
            unavailable = self.model_revision is None
            if unavailable != (self.model_revision_unavailable_reason is not None):
                raise ValueError("runtime identity requires explicit revision availability")
        elif any(
            value is not None
            for value in (
                self.provider,
                self.model_id,
                self.model_revision,
                self.model_revision_unavailable_reason,
            )
        ):
            raise ValueError("provider and model identity must be present together")
        library_known = self.library_version is not None
        if library_known == (self.library_unavailable_reason is not None):
            raise ValueError("library version availability must be explicit")
        return self


@runtime_checkable
class BoundedJudgeCandidatePort(Protocol):
    """Project/Harness-owned candidate call; implementations must honor limits."""

    def generate_candidate(self, request: JudgeCandidateRequest) -> JudgeCandidateResponse:
        ...


@runtime_checkable
class JudgeSubrequestCallbackPort(Protocol):
    def on_subrequest(self, record: JudgeSubrequestRecord) -> None:
        ...


@runtime_checkable
class JudgeBudgetHookPort(Protocol):
    """Per-subrequest hook that an owning runner may adapt to its shared budget.

    This is not a ledger, journal, run store, or accounting authority.
    """

    def authorize(self, reservation: JudgeBudgetHookRequest) -> JudgeBudgetHookDecision:
        ...

    def record(self, entry: JudgeBudgetHookRecord) -> None:
        ...


@runtime_checkable
class SemanticEvaluationJudgePort(Protocol):
    @property
    def capability(self) -> JudgeCapability:
        ...

    async def judge(self, request: SemanticJudgeRequest) -> SemanticJudgeObservation:
        ...


__all__ = [
    "AnswerSpan",
    "BoundedJudgeCandidatePort",
    "CandidateUsage",
    "CitationBinding",
    "CitationLocation",
    "CitationSupportCandidate",
    "CitationSupportJudgeRequest",
    "FactualJudgeRequest",
    "FaithfulnessJudgeRequest",
    "JudgeBudget",
    "JudgeCandidateRequest",
    "JudgeCandidateResponse",
    "JudgeCapability",
    "JudgeError",
    "JudgeErrorCode",
    "JudgeBudgetHookRecord",
    "JudgeBudgetHookDecision",
    "JudgeBudgetHookRequest",
    "JudgeMetric",
    "JudgeModality",
    "JudgeObservationStatus",
    "JudgeRuntimeProfile",
    "JudgeScope",
    "JudgeSubrequestCallbackPort",
    "JudgeSubrequestRecord",
    "JudgeUsageSummary",
    "SemanticEvaluationJudgePort",
    "SemanticJudgeObservation",
    "SemanticJudgeRequest",
    "JudgeBudgetHookPort",
    "TextContext",
    "content_digest",
]