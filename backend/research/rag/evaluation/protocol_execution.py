"""Execution contracts and deterministic preflight for Research RAG evaluation.

This module defines the immutable run-side protocol boundary. It deliberately
contains no runner, provider calls, state machine, or persistence implementation.
Actual Research services are injected by outer composition after these contracts
pass deterministic preflight.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

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
from backend.research.rag.evaluation.protocol_dataset import QuestionScope

_LOCAL_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
_PRODUCTION_NAMESPACE_SEGMENTS = frozenset({"prod", "production"})
_FIXTURE_MARKERS = re.compile(
    r"(?:^|[^a-z0-9])(hash(?:ing)?|fixed(?:answer)?|fake|fixture|mock|stub|dummy)(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


def _aware(value: datetime | None, field_name: str) -> datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _portable_reference(value: str, field_name: str) -> str:
    if _LOCAL_PATH.match(value) or value.lower().startswith("file:"):
        raise ValueError(f"{field_name} must be a portable artifact reference, not a local path")
    return value


def _unique(values: tuple[str, ...], field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicate values")


def _finite_number(value: Any, field_name: str, *, positive: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{field_name} must be a finite non-boolean number")
    if value < 0 or (positive and value <= 0):
        comparator = "positive" if positive else "non-negative"
        raise ValueError(f"{field_name} must be {comparator}")
    return value


def _non_negative_integer(value: Any, field_name: str, *, positive: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a non-boolean integer")
    if value < 0 or (positive and value == 0):
        comparator = "positive" if positive else "non-negative"
        raise ValueError(f"{field_name} must be {comparator}")
    return value


def _namespace_segments(value: str) -> frozenset[str]:
    return frozenset(part for part in re.split(r"[^a-z0-9]+", value.lower()) if part)


def _reject_latest(value: str, field_name: str) -> str:
    if value.strip().lower() in {"latest", "current", "head"}:
        raise ValueError(f"{field_name} must be an immutable exact version, not a floating alias")
    return value


class ComponentRole(StrEnum):
    SOURCE_READER = "source_reader"
    PARSER = "parser"
    CHUNKER = "chunker"
    EMBEDDING = "embedding"
    INDEX = "index"
    RETRIEVER = "retriever"
    RERANKER = "reranker"
    GENERATOR = "generator"
    ANSWER_WORKER = "answer_worker"
    HARNESS = "harness"
    JUDGE = "judge"


class ComponentClassification(StrEnum):
    PRODUCTION = "production"
    FIXTURE = "fixture"
    NOT_APPLICABLE = "not_applicable"


class FixtureKind(StrEnum):
    HASH_EMBEDDING = "hash_embedding"
    FIXED_ANSWER = "fixed_answer"
    IN_MEMORY = "in_memory"
    FAKE = "fake"
    STUB = "stub"
    OTHER = "other"


class ExperimentPurpose(StrEnum):
    DEV_DIAGNOSTIC = "dev_diagnostic"
    HELDOUT_QUALIFICATION = "heldout_qualification"
    RELEASE_QUALIFICATION = "release_qualification"


class RunExecutionStatus(StrEnum):
    PLANNED = "planned"
    PREFLIGHT_REJECTED = "preflight_rejected"
    RUNNING = "running"
    INCOMPLETE = "incomplete"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QualificationDecision(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not_applicable"


class InitialStatePolicy(StrEnum):
    EMPTY = "empty"
    RESET_TO_SNAPSHOT = "reset_to_snapshot"


class CachePolicy(StrEnum):
    COLD = "cold"
    RESET_TO_SNAPSHOT = "reset_to_snapshot"


class ExecutionPathKind(StrEnum):
    RETRIEVAL_APPLICATION = "retrieval_application"
    ORACLE_ANSWER_WORKER = "oracle_answer_worker"
    RESEARCH_GATED_HARNESS = "research_gated_harness"


class ArtifactReference(ProtocolModel):
    reference_id: NonEmptyStr
    kind: NonEmptyStr
    uri: NonEmptyStr
    content_sha256: Sha256Digest

    @field_validator("uri")
    @classmethod
    def _portable_uri(cls, value: str) -> str:
        return _portable_reference(value, "uri")


class ComponentProvenance(ProtocolModel):
    """Observed component identity, never merely requested configuration."""

    role: ComponentRole
    classification: ComponentClassification
    adapter: NonEmptyStr | None
    provider: NonEmptyStr | None
    model: NonEmptyStr | None
    revision: ImmutableVersionStr | None
    identity_unavailable_reason: NonEmptyStr | None
    fixture_kind: FixtureKind | None
    not_applicable_reason: NonEmptyStr | None
    config_hash: Sha256Digest | None
    code_revision: ImmutableVersionStr | None
    evidence_references: tuple[NonEmptyStr, ...]
    runtime_provenance_digest: Sha256Digest | None

    @field_validator("revision", "code_revision")
    @classmethod
    def _exact_component_versions(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _reject_latest(value, info.field_name)

    @model_validator(mode="after")
    def _validate_component(self) -> Self:
        _unique(self.evidence_references, "evidence_references")
        for reference in self.evidence_references:
            _portable_reference(reference, "evidence_references")
        if self.classification is ComponentClassification.NOT_APPLICABLE:
            if self.not_applicable_reason is None:
                raise ValueError("not_applicable components require not_applicable_reason")
            forbidden = (
                self.adapter,
                self.provider,
                self.model,
                self.revision,
                self.identity_unavailable_reason,
                self.fixture_kind,
                self.config_hash,
                self.code_revision,
                self.runtime_provenance_digest,
            )
            if any(value is not None for value in forbidden) or self.evidence_references:
                raise ValueError("not_applicable components must not claim runtime identity or evidence")
            return self

        if self.not_applicable_reason is not None:
            raise ValueError("applicable components must use explicit null not_applicable_reason")
        if (
            self.adapter is None
            or self.config_hash is None
            or self.code_revision is None
            or self.runtime_provenance_digest is None
        ):
            raise ValueError(
                "applicable components require adapter, config_hash, code_revision, and externally observed runtime_provenance_digest"
            )
        if not self.evidence_references:
            raise ValueError("applicable components require runtime evidence_references")
        missing_identity = any(value is None for value in (self.provider, self.model, self.revision))
        if missing_identity != (self.identity_unavailable_reason is not None):
            raise ValueError(
                "identity_unavailable_reason is required exactly when provider/model/revision is unavailable"
            )
        if self.classification is ComponentClassification.FIXTURE and self.fixture_kind is None:
            raise ValueError("fixture components require fixture_kind")
        if self.classification is ComponentClassification.PRODUCTION and self.fixture_kind is not None:
            raise ValueError("production components must use explicit null fixture_kind")
        return self

    def has_fixture_marker(self) -> bool:
        values = (self.adapter, self.provider, self.model, self.revision)
        for value in values:
            if value is None:
                continue
            normalized = re.sub(r"[^a-z0-9]+", "", value.lower())
            if (
                "hashembed" in normalized
                or "fixedanswer" in normalized
                or any(marker in normalized for marker in ("fixture", "fake", "mock", "stub", "dummy"))
                or _FIXTURE_MARKERS.search(value)
            ):
                return True
        return False


class BudgetLimits(ProtocolModel):
    """Shared run ledger limits; retries, replans, and recovery do not reset it."""

    max_requests: int
    max_total_tokens: int
    max_cost: float | None
    currency: NonEmptyStr | None
    unknown_price: bool
    unknown_price_reason: NonEmptyStr | None
    price_basis_reference: NonEmptyStr | None
    request_timeout_seconds: float
    case_deadline_seconds: float
    run_deadline_seconds: float
    max_concurrency: int
    max_retries: int
    max_replans: int
    max_turns: int

    @field_validator("max_requests", "max_total_tokens", "max_retries", "max_replans", mode="before")
    @classmethod
    def _non_negative_ints(cls, value: Any, info: Any) -> Any:
        return _non_negative_integer(value, info.field_name)

    @field_validator("max_concurrency", "max_turns", mode="before")
    @classmethod
    def _positive_ints(cls, value: Any, info: Any) -> Any:
        return _non_negative_integer(value, info.field_name, positive=True)

    @field_validator(
        "request_timeout_seconds", "case_deadline_seconds", "run_deadline_seconds", mode="before"
    )
    @classmethod
    def _positive_times(cls, value: Any, info: Any) -> Any:
        return _finite_number(value, info.field_name, positive=True)

    @field_validator("max_cost", mode="before")
    @classmethod
    def _finite_cost(cls, value: Any) -> Any:
        if value is None:
            return value
        return _finite_number(value, "max_cost")

    @model_validator(mode="after")
    def _validate_price_boundary(self) -> Self:
        if self.unknown_price:
            if self.max_cost is not None:
                raise ValueError("unknown_price requires max_cost=null rather than a fabricated cost cap")
            if self.unknown_price_reason is None:
                raise ValueError("unknown_price requires unknown_price_reason")
            if self.currency is not None or self.price_basis_reference is not None:
                raise ValueError("unknown pricing must not claim currency or a price basis")
        else:
            if self.max_cost is None or self.currency is None or self.price_basis_reference is None:
                raise ValueError("known pricing requires max_cost, currency, and price_basis_reference")
            if self.unknown_price_reason is not None:
                raise ValueError("known pricing must use explicit null unknown_price_reason")
        if self.case_deadline_seconds > self.run_deadline_seconds:
            raise ValueError("case_deadline_seconds must not exceed run_deadline_seconds")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class CodeIdentity(ProtocolModel):
    git_revision: ImmutableVersionStr
    workspace_content_fingerprint: Sha256Digest
    mutable_patch_fingerprints: tuple[Sha256Digest, ...]
    dirty: bool
    code_fingerprint: Sha256Digest

    @field_validator("git_revision")
    @classmethod
    def _exact_git_revision(cls, value: str) -> str:
        return _reject_latest(value, "git_revision")

    @classmethod
    def create(
        cls,
        *,
        git_revision: str,
        workspace_content_fingerprint: str,
        mutable_patch_fingerprints: tuple[str, ...],
        dirty: bool,
    ) -> Self:
        payload = {
            "git_revision": git_revision,
            "workspace_content_fingerprint": workspace_content_fingerprint,
            "mutable_patch_fingerprints": mutable_patch_fingerprints,
            "dirty": dirty,
        }
        return cls(**payload, code_fingerprint=content_digest(payload))

    @model_validator(mode="after")
    def _validate_code_identity(self) -> Self:
        _unique(self.mutable_patch_fingerprints, "mutable_patch_fingerprints")
        if self.dirty != bool(self.mutable_patch_fingerprints):
            raise ValueError("dirty must exactly reflect whether mutable_patch_fingerprints are present")
        payload = self.model_dump(mode="json", exclude={"code_fingerprint"})
        if self.code_fingerprint != content_digest(payload):
            raise ValueError("code_fingerprint does not match git revision and workspace content")
        return self


class IsolatedNamespaces(ProtocolModel):
    tenant: NonEmptyStr
    index: NonEmptyStr
    memory: NonEmptyStr
    artifact: NonEmptyStr

    @model_validator(mode="after")
    def _reject_production_memory(self) -> Self:
        if _namespace_segments(self.memory) & _PRODUCTION_NAMESPACE_SEGMENTS:
            raise ValueError("memory namespace must not identify production business memory")
        if len({self.tenant, self.index, self.memory, self.artifact}) != 4:
            raise ValueError("tenant, index, memory, and artifact namespaces must be independently named")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class InitialState(ProtocolModel):
    memory_policy: InitialStatePolicy
    memory_snapshot_digest: Sha256Digest | None
    cache_policy: CachePolicy
    cache_snapshot_digest: Sha256Digest | None
    gold_labels_present: Literal[False]
    prior_run_answers_present: Literal[False]
    production_business_memory_mounted: Literal[False]
    source_fulltext_access: Literal["read_only"]

    @model_validator(mode="after")
    def _validate_snapshots(self) -> Self:
        if (self.memory_policy is InitialStatePolicy.RESET_TO_SNAPSHOT) != (
            self.memory_snapshot_digest is not None
        ):
            raise ValueError("memory snapshot digest must match memory_policy")
        if (self.cache_policy is CachePolicy.RESET_TO_SNAPSHOT) != (
            self.cache_snapshot_digest is not None
        ):
            raise ValueError("cache snapshot digest must match cache_policy")
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class SourceAccessGrant(ProtocolModel):
    """Minimal source authorization; no arbitrary metadata/filter channel exists."""

    paper_id: NonEmptyStr
    document_version: ImmutableVersionStr
    source_sha256: Sha256Digest
    authorization_reference: NonEmptyStr

    @field_validator("authorization_reference")
    @classmethod
    def _portable_authorization_reference(cls, value: str) -> str:
        return _portable_reference(value, "authorization_reference")

    @field_validator("document_version")
    @classmethod
    def _exact_document_version(cls, value: str) -> str:
        return _reject_latest(value, "document_version")


class CorpusProvenance(ProtocolModel):
    corpus_snapshot_id: NonEmptyStr
    corpus_content_sha256: Sha256Digest
    source_material: Literal["complete_fulltext", "reduced_fixture"]
    source_completeness: Literal["complete", "incomplete", "unknown"]
    index_snapshot_id: NonEmptyStr | None
    index_content_sha256: Sha256Digest | None
    index_not_applicable_reason: NonEmptyStr | None
    gold_labels_indexed: Literal[False]
    previous_answers_indexed: Literal[False]
    evidence_references: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @field_validator("corpus_snapshot_id", "index_snapshot_id")
    @classmethod
    def _exact_snapshot_identity(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _reject_latest(value, info.field_name)

    @model_validator(mode="after")
    def _validate_index_identity(self) -> Self:
        _unique(self.evidence_references, "evidence_references")
        for reference in self.evidence_references:
            _portable_reference(reference, "evidence_references")
        present = self.index_snapshot_id is not None and self.index_content_sha256 is not None
        absent = self.index_snapshot_id is None and self.index_content_sha256 is None
        if not (present or absent):
            raise ValueError("index_snapshot_id and index_content_sha256 must be provided together")
        if absent != (self.index_not_applicable_reason is not None):
            raise ValueError("index_not_applicable_reason is required exactly when no index applies")
        return self


class RetrievalFactors(ProtocolModel):
    parser_config_hash: Sha256Digest
    chunker_config_hash: Sha256Digest
    embedding_config_hash: Sha256Digest
    index_config_hash: Sha256Digest
    retriever_config_hash: Sha256Digest
    reranker_config_hash: Sha256Digest | None
    reranker_not_applicable_reason: NonEmptyStr | None
    context_policy_hash: Sha256Digest

    @model_validator(mode="after")
    def _validate_reranker(self) -> Self:
        if (self.reranker_config_hash is None) != (self.reranker_not_applicable_reason is not None):
            raise ValueError("reranker_not_applicable_reason is required exactly when reranking is disabled")
        return self


class GenerationFactors(ProtocolModel):
    generator_config_hash: Sha256Digest
    prompt_hash: Sha256Digest
    output_contract_hash: Sha256Digest
    decoding_config_hash: Sha256Digest
    context_policy_hash: Sha256Digest
    max_input_tokens: int
    max_output_tokens: int
    request_timeout_seconds: float
    max_retries: int

    @field_validator("max_input_tokens", "max_output_tokens", mode="before")
    @classmethod
    def _positive_tokens(cls, value: Any, info: Any) -> Any:
        return _non_negative_integer(value, info.field_name, positive=True)

    @field_validator("max_retries", mode="before")
    @classmethod
    def _retries(cls, value: Any) -> Any:
        return _non_negative_integer(value, "max_retries")

    @field_validator("request_timeout_seconds", mode="before")
    @classmethod
    def _timeout(cls, value: Any) -> Any:
        return _finite_number(value, "request_timeout_seconds", positive=True)


class HarnessFactors(ProtocolModel):
    application_service: NonEmptyStr
    harness_config_hash: Sha256Digest
    deterministic_gate_config_hash: Sha256Digest
    gated: Literal[True]
    plan_execute_verify: Literal[True]


class VariantConfiguration(ProtocolModel):
    variant_id: NonEmptyStr
    track: EvaluationTrack
    retrieval: RetrievalFactors | None
    retrieval_not_applicable_reason: NonEmptyStr | None
    generation: GenerationFactors | None
    generation_not_applicable_reason: NonEmptyStr | None
    harness: HarnessFactors | None
    harness_not_applicable_reason: NonEmptyStr | None
    variant_fingerprint: Sha256Digest

    @classmethod
    def create(
        cls,
        *,
        variant_id: str,
        track: EvaluationTrack,
        retrieval: RetrievalFactors | None,
        retrieval_not_applicable_reason: str | None,
        generation: GenerationFactors | None,
        generation_not_applicable_reason: str | None,
        harness: HarnessFactors | None,
        harness_not_applicable_reason: str | None,
    ) -> Self:
        payload = {"track": track, "retrieval": retrieval, "generation": generation, "harness": harness}
        return cls(
            variant_id=variant_id,
            track=track,
            retrieval=retrieval,
            retrieval_not_applicable_reason=retrieval_not_applicable_reason,
            generation=generation,
            generation_not_applicable_reason=generation_not_applicable_reason,
            harness=harness,
            harness_not_applicable_reason=harness_not_applicable_reason,
            variant_fingerprint=content_digest(payload),
        )

    @model_validator(mode="after")
    def _validate_track_factors(self) -> Self:
        pairs = (
            (self.retrieval, self.retrieval_not_applicable_reason, "retrieval"),
            (self.generation, self.generation_not_applicable_reason, "generation"),
            (self.harness, self.harness_not_applicable_reason, "harness"),
        )
        for value, reason, name in pairs:
            if (value is None) != (reason is not None):
                raise ValueError(f"{name}_not_applicable_reason is required exactly when {name} is absent")
        if self.track is EvaluationTrack.RETRIEVAL:
            if self.retrieval is None or self.generation is not None or self.harness is not None:
                raise ValueError("retrieval variants contain retrieval factors only")
        elif self.track is EvaluationTrack.ORACLE_GENERATION:
            if self.retrieval is not None or self.generation is None or self.harness is not None:
                raise ValueError(
                    "oracle_generation variants contain generation factors only; retrieval factors are forbidden"
                )
        elif self.track is EvaluationTrack.END_TO_END:
            if self.retrieval is None or self.generation is None or self.harness is None:
                raise ValueError("end_to_end variants require retrieval, generation, and gated harness factors")
        expected = content_digest(
            {"track": self.track, "retrieval": self.retrieval, "generation": self.generation, "harness": self.harness}
        )
        if self.variant_fingerprint != expected:
            raise ValueError("variant_fingerprint does not match track-relevant content")
        return self


class CaseScopeProjection(ProtocolModel):
    """Run-visible source projection; it contains no reference/gold material."""

    case_id: NonEmptyStr
    scope: QuestionScope
    scope_digest: Sha256Digest

    @classmethod
    def create(cls, *, case_id: str, scope: QuestionScope) -> Self:
        return cls(case_id=case_id, scope=scope, scope_digest=content_digest(scope))

    @model_validator(mode="after")
    def _validate_scope_digest(self) -> Self:
        for source in self.scope.sources:
            _reject_latest(source.document_version, "scope.sources.document_version")
        if self.scope_digest != content_digest(self.scope):
            raise ValueError("scope_digest does not match scope content")
        return self


class PlannedEvaluationUnit(ProtocolModel):
    dataset_version: ImmutableVersionStr
    case_id: NonEmptyStr
    track: EvaluationTrack
    variant_fingerprint: Sha256Digest
    repetition: int

    @field_validator("dataset_version")
    @classmethod
    def _reject_latest(cls, value: str) -> str:
        return _reject_latest(value, "dataset_version")

    @field_validator("repetition", mode="before")
    @classmethod
    def _positive_repetition(cls, value: Any) -> Any:
        return _non_negative_integer(value, "repetition", positive=True)

    @property
    def primary_key(self) -> tuple[str, str, EvaluationTrack, str, int]:
        return (self.dataset_version, self.case_id, self.track, self.variant_fingerprint, self.repetition)


class AttemptIdentity(ProtocolModel):
    attempt_id: NonEmptyStr
    unit: PlannedEvaluationUnit
    sequence: int
    external_request_id: NonEmptyStr | None

    @field_validator("sequence", mode="before")
    @classmethod
    def _positive_sequence(cls, value: Any) -> Any:
        return _non_negative_integer(value, "sequence", positive=True)


class ComparisonIdentity(ProtocolModel):
    dataset_digest: Sha256Digest
    case_scope_fingerprint: Sha256Digest
    corpus_content_sha256: Sha256Digest
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    variant_fingerprint: Sha256Digest
    code_fingerprint: Sha256Digest
    environment_fingerprint: Sha256Digest
    budget_fingerprint: Sha256Digest
    initial_state_fingerprint: Sha256Digest
    namespace_fingerprint: Sha256Digest
    repetition_plan: tuple[int, ...] = Field(min_length=1)
    timing_boundary: NonEmptyStr
    comparison_fingerprint: Sha256Digest

    @classmethod
    def create(cls, **content: Any) -> Self:
        if "comparison_fingerprint" in content:
            raise ValueError("comparison_fingerprint is calculated")
        return cls(**content, comparison_fingerprint=content_digest(content))

    @model_validator(mode="after")
    def _validate_comparison(self) -> Self:
        _unique(tuple(str(item) for item in self.repetition_plan), "repetition_plan")
        expected = content_digest(self.model_dump(mode="json", exclude={"comparison_fingerprint"}))
        if self.comparison_fingerprint != expected:
            raise ValueError("comparison_fingerprint does not match content identity")
        return self


class _ExperimentManifestContent(ProtocolModel):
    record_type: Literal["experiment_manifest"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    experiment_id: NonEmptyStr
    experiment_purpose: ExperimentPurpose
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest
    case_scopes: tuple[CaseScopeProjection, ...] = Field(min_length=1)
    variants: tuple[VariantConfiguration, ...] = Field(min_length=1)
    planned_units: tuple[PlannedEvaluationUnit, ...] = Field(min_length=1)
    comparison_identities: tuple[ComparisonIdentity, ...] = Field(min_length=1)
    allowed_differences: tuple[NonEmptyStr, ...]

    @field_validator("dataset_version")
    @classmethod
    def _immutable_dataset_version(cls, value: str) -> str:
        return _reject_latest(value, "dataset_version")

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        case_ids = tuple(item.case_id for item in self.case_scopes)
        _unique(case_ids, "case_scopes.case_id")
        variant_fingerprints = tuple(item.variant_fingerprint for item in self.variants)
        _unique(variant_fingerprints, "variants.variant_fingerprint")
        keys = tuple(item.primary_key for item in self.planned_units)
        if len(keys) != len(set(keys)):
            raise ValueError("planned_units must not contain duplicate formal unit primary keys")
        known_cases = set(case_ids)
        variants = {item.variant_fingerprint: item for item in self.variants}
        for unit in self.planned_units:
            if unit.dataset_version != self.dataset_version:
                raise ValueError("planned unit references another dataset_version")
            if unit.case_id not in known_cases:
                raise ValueError("planned unit references an unknown case_id")
            variant = variants.get(unit.variant_fingerprint)
            if variant is None or variant.track is not unit.track:
                raise ValueError("planned unit references an unknown or cross-track variant")
        comparison_keys = {(item.track, item.variant_fingerprint, item.comparison_fingerprint) for item in self.comparison_identities}
        if len(comparison_keys) != len(self.comparison_identities):
            raise ValueError("comparison_identities must not contain duplicates")
        _unique(self.allowed_differences, "allowed_differences")
        return self


class ExperimentManifest(_ExperimentManifestContent):
    manifest_digest: Sha256Digest

    @classmethod
    def create(cls, **content: Any) -> Self:
        if "manifest_digest" in content:
            raise ValueError("manifest_digest is calculated")
        normalized = _ExperimentManifestContent.model_validate(content)
        payload = normalized.model_dump(mode="json")
        return cls(**{name: getattr(normalized, name) for name in _ExperimentManifestContent.model_fields}, manifest_digest=content_digest(payload))

    @model_validator(mode="after")
    def _validate_digest(self) -> Self:
        expected = content_digest(self.model_dump(mode="json", exclude={"manifest_digest"}))
        if self.manifest_digest != expected:
            raise ValueError("manifest_digest does not match experiment content")
        return self


class ContextGraphRequestIdentity(ProtocolModel):
    """Requested exact Graph activity identity; runtime trace must prove execution separately."""

    run_id: NonEmptyStr
    graph_id: NonEmptyStr
    graph_version: ImmutableVersionStr
    graph_ref: NonEmptyStr
    graph_checksum: Sha256Digest
    stage_id: NonEmptyStr
    stage_binding_checksum: Sha256Digest
    node_id: NonEmptyStr
    node_instance_id: NonEmptyStr
    activity_id: NonEmptyStr
    activity_attempt: int

    @field_validator("graph_version")
    @classmethod
    def _exact_graph_version(cls, value: str) -> str:
        return _reject_latest(value, "graph_version")

    @field_validator("activity_attempt", mode="before")
    @classmethod
    def _positive_activity_attempt(cls, value: Any) -> Any:
        return _non_negative_integer(value, "activity_attempt", positive=True)

    @model_validator(mode="after")
    def _validate_graph_ref(self) -> Self:
        if self.graph_ref != f"{self.graph_id}@{self.graph_version}":
            raise ValueError("graph_ref must be an exact graph_id@graph_version reference")
        return self


class ExecutionPath(ProtocolModel):
    kind: ExecutionPathKind
    application_service: NonEmptyStr
    retrieval_enabled: bool
    answer_generation_enabled: bool
    gated_harness_requested: bool
    context_graph_identity: ContextGraphRequestIdentity | None


class ExecutionEnvironment(ProtocolModel):
    runtime: NonEmptyStr
    dependency_lock_digest: Sha256Digest
    hardware_class: NonEmptyStr | None
    hardware_identity_unavailable_reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _hardware_reason(self) -> Self:
        if (self.hardware_class is None) != (self.hardware_identity_unavailable_reason is not None):
            raise ValueError(
                "hardware_identity_unavailable_reason is required exactly when hardware_class is unavailable"
            )
        return self

    @property
    def fingerprint(self) -> str:
        return content_digest(self)


class EvaluationOptions(ProtocolModel):
    """Structured application input; callers never need to synthesize CLI argv."""

    record_type: Literal["evaluation_options"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    experiment_manifest: ExperimentManifest
    selected_variant_fingerprint: Sha256Digest
    execution_profile: ExecutionProfile
    execution_path: ExecutionPath
    components: tuple[ComponentProvenance, ...] = Field(min_length=1)
    source_access_grants: tuple[SourceAccessGrant, ...] = Field(min_length=1)
    corpus: CorpusProvenance
    namespaces: IsolatedNamespaces
    initial_state: InitialState
    budget_limits: BudgetLimits
    code_identity: CodeIdentity
    environment: ExecutionEnvironment

    @model_validator(mode="after")
    def _validate_links(self) -> Self:
        variants = {item.variant_fingerprint: item for item in self.experiment_manifest.variants}
        selected = variants.get(self.selected_variant_fingerprint)
        if selected is None:
            raise ValueError("selected_variant_fingerprint is not present in experiment_manifest")
        scope_sources = {
            (source.paper_id, source.document_version, source.source_sha256)
            for case_scope in self.experiment_manifest.case_scopes
            for source in case_scope.scope.sources
        }
        grants = tuple(
            (item.paper_id, item.document_version, item.source_sha256)
            for item in self.source_access_grants
        )
        if len(grants) != len(set(grants)):
            raise ValueError("source_access_grants must not contain duplicates")
        if set(grants) != scope_sources:
            raise ValueError("source_access_grants must exactly authorize the declared case scopes")
        roles = tuple(item.role for item in self.components)
        if len(roles) != len(set(roles)):
            raise ValueError("components must contain at most one observed component per role")
        comparisons = [
            item
            for item in self.experiment_manifest.comparison_identities
            if item.track is selected.track
            and item.execution_profile is self.execution_profile
            and item.variant_fingerprint == selected.variant_fingerprint
        ]
        if len(comparisons) != 1:
            raise ValueError("experiment_manifest must contain exactly one comparison identity for selected profile/variant")
        comparison = comparisons[0]
        repetitions = tuple(
            sorted({
                unit.repetition
                for unit in self.experiment_manifest.planned_units
                if unit.track is selected.track
                and unit.variant_fingerprint == selected.variant_fingerprint
            })
        )
        expected_links = {
            "dataset_digest": self.experiment_manifest.dataset_digest,
            "case_scope_fingerprint": content_digest(self.experiment_manifest.case_scopes),
            "corpus_content_sha256": self.corpus.corpus_content_sha256,
            "code_fingerprint": self.code_identity.code_fingerprint,
            "environment_fingerprint": self.environment.fingerprint,
            "budget_fingerprint": self.budget_limits.fingerprint,
            "initial_state_fingerprint": self.initial_state.fingerprint,
            "namespace_fingerprint": self.namespaces.fingerprint,
            "repetition_plan": repetitions,
        }
        for field_name, expected in expected_links.items():
            if getattr(comparison, field_name) != expected:
                raise ValueError(f"comparison identity {field_name} does not match structured options")
        return self

    @property
    def selected_variant(self) -> VariantConfiguration:
        return next(
            item for item in self.experiment_manifest.variants if item.variant_fingerprint == self.selected_variant_fingerprint
        )

    def preflight(self) -> "PreflightReport":
        return preflight_evaluation(self)

    def assert_preflight(self) -> "PreflightReport":
        report = self.preflight()
        if not report.accepted:
            raise EvaluationPreflightError(report)
        return report


class RunControl(ProtocolModel):
    """Clean per-attempt control plane, physically separate from reference labels."""

    record_type: Literal["run_control"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    run_id: NonEmptyStr
    unit: PlannedEvaluationUnit
    attempt_id: NonEmptyStr
    options_fingerprint: Sha256Digest
    budget_fingerprint: Sha256Digest
    namespace_fingerprint: Sha256Digest
    case_scope: CaseScopeProjection
    namespaces: IsolatedNamespaces
    budget_limits: BudgetLimits
    source_access_grants: tuple[SourceAccessGrant, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _link_case(self) -> Self:
        if self.budget_fingerprint != self.budget_limits.fingerprint:
            raise ValueError("budget_fingerprint does not match RunControl budget_limits")
        if self.namespace_fingerprint != self.namespaces.fingerprint:
            raise ValueError("namespace_fingerprint does not match RunControl namespaces")
        if self.case_scope.case_id != self.unit.case_id:
            raise ValueError("case_scope must describe the controlled unit case_id")
        expected = {
            (source.paper_id, source.document_version, source.source_sha256)
            for source in self.case_scope.scope.sources
        }
        actual = {
            (item.paper_id, item.document_version, item.source_sha256)
            for item in self.source_access_grants
        }
        if len(actual) != len(self.source_access_grants) or actual != expected:
            raise ValueError("source_access_grants must exactly match the controlled QuestionScope")
        return self


class EvaluationRun(ProtocolModel):
    record_type: Literal["run"]
    schema_version: Literal[SCHEMA_VERSION]
    protocol_version: Literal[PROTOCOL_VERSION]
    run_id: NonEmptyStr
    experiment_id: NonEmptyStr
    experiment_purpose: ExperimentPurpose
    experiment_manifest_digest: Sha256Digest
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest
    corpus_snapshot_id: NonEmptyStr
    corpus_content_sha256: Sha256Digest
    index_snapshot_id: NonEmptyStr | None
    index_not_applicable_reason: NonEmptyStr | None
    track: EvaluationTrack
    execution_profile: ExecutionProfile
    selected_variant_fingerprint: Sha256Digest
    options_fingerprint: Sha256Digest
    code_identity: CodeIdentity
    environment: ExecutionEnvironment
    execution_path: ExecutionPath
    variant_configuration: VariantConfiguration
    comparison_identity: ComparisonIdentity
    components: tuple[ComponentProvenance, ...] = Field(min_length=1)
    case_scopes: tuple[CaseScopeProjection, ...] = Field(min_length=1)
    source_access_grants: tuple[SourceAccessGrant, ...] = Field(min_length=1)
    budget_limits: BudgetLimits
    namespaces: IsolatedNamespaces
    initial_state: InitialState
    planned_units: tuple[PlannedEvaluationUnit, ...] = Field(min_length=1)
    attempts: tuple[AttemptIdentity, ...]
    artifact_references: tuple[ArtifactReference, ...]
    transcript_references: tuple[ArtifactReference, ...]
    started_at: datetime | None
    completed_at: datetime | None
    execution_status: RunExecutionStatus
    qualification: QualificationDecision
    qualification_reason: NonEmptyStr
    qualification_evidence_references: tuple[NonEmptyStr, ...]

    @field_validator("dataset_version")
    @classmethod
    def _version_not_latest(cls, value: str) -> str:
        if value.lower() == "latest":
            raise ValueError("dataset_version must not be latest")
        return value

    @field_validator("started_at", "completed_at")
    @classmethod
    def _aware_times(cls, value: datetime | None, info: Any) -> datetime | None:
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def _validate_run(self) -> Self:
        if self.variant_configuration.track is not self.track:
            raise ValueError("variant_configuration track does not match run track")
        if self.variant_configuration.variant_fingerprint != self.selected_variant_fingerprint:
            raise ValueError("variant_configuration fingerprint does not match selected variant")
        comparison = self.comparison_identity
        if (
            comparison.dataset_digest != self.dataset_digest
            or comparison.corpus_content_sha256 != self.corpus_content_sha256
            or comparison.track is not self.track
            or comparison.execution_profile is not self.execution_profile
            or comparison.variant_fingerprint != self.selected_variant_fingerprint
            or comparison.code_fingerprint != self.code_identity.code_fingerprint
            or comparison.environment_fingerprint != self.environment.fingerprint
            or comparison.budget_fingerprint != self.budget_limits.fingerprint
            or comparison.initial_state_fingerprint != self.initial_state.fingerprint
            or comparison.namespace_fingerprint != self.namespaces.fingerprint
            or comparison.case_scope_fingerprint != content_digest(self.case_scopes)
        ):
            raise ValueError("comparison_identity does not match the run content identity")
        if (self.index_snapshot_id is None) != (self.index_not_applicable_reason is not None):
            raise ValueError("index_not_applicable_reason is required exactly when index is absent")
        _reject_latest(self.corpus_snapshot_id, "corpus_snapshot_id")
        if self.index_snapshot_id is not None:
            _reject_latest(self.index_snapshot_id, "index_snapshot_id")
        scope_ids = tuple(item.case_id for item in self.case_scopes)
        _unique(scope_ids, "case_scopes.case_id")
        scope_sources = {
            (source.paper_id, source.document_version, source.source_sha256)
            for case_scope in self.case_scopes
            for source in case_scope.scope.sources
        }
        grants = tuple(
            (item.paper_id, item.document_version, item.source_sha256)
            for item in self.source_access_grants
        )
        if len(grants) != len(set(grants)) or set(grants) != scope_sources:
            raise ValueError("source_access_grants must exactly authorize run case scopes")
        keys = tuple(item.primary_key for item in self.planned_units)
        if len(keys) != len(set(keys)):
            raise ValueError("planned_units must not contain duplicate primary keys")
        attempts = tuple(item.attempt_id for item in self.attempts)
        _unique(attempts, "attempts.attempt_id")
        known_case_ids = set(scope_ids)
        for unit in self.planned_units:
            if (
                unit.dataset_version != self.dataset_version
                or unit.case_id not in known_case_ids
                or unit.track is not self.track
                or unit.variant_fingerprint != self.selected_variant_fingerprint
            ):
                raise ValueError("planned unit identity does not match the run dataset/cases/track/variant")
        unit_keys = set(keys)
        sequences: set[tuple[tuple[str, str, EvaluationTrack, str, int], int]] = set()
        for attempt in self.attempts:
            if attempt.unit.primary_key not in unit_keys:
                raise ValueError("attempt references a unit outside the run plan")
            sequence_key = (attempt.unit.primary_key, attempt.sequence)
            if sequence_key in sequences:
                raise ValueError("attempt sequence must be unique within a planned unit")
            sequences.add(sequence_key)
        references = self.artifact_references + self.transcript_references
        reference_ids = tuple(item.reference_id for item in references)
        _unique(reference_ids, "artifact/transcript reference_id")
        reference_content = tuple((item.uri, item.content_sha256) for item in references)
        if len(reference_content) != len(set(reference_content)):
            raise ValueError("artifact/transcript references must not duplicate the same content reference")
        if self.execution_status is RunExecutionStatus.COMPLETED and self.completed_at is None:
            raise ValueError("completed runs require completed_at")
        if self.execution_status is not RunExecutionStatus.COMPLETED and self.completed_at is not None:
            raise ValueError("only completed runs may set completed_at")
        if self.started_at is not None and self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        _unique(self.qualification_evidence_references, "qualification_evidence_references")
        for reference in self.qualification_evidence_references:
            _portable_reference(reference, "qualification_evidence_references")
        repetitions = tuple(sorted({unit.repetition for unit in self.planned_units}))
        if repetitions != self.comparison_identity.repetition_plan:
            raise ValueError("comparison repetition_plan does not match planned_units")
        if self.qualification is QualificationDecision.PASS:
            if self.execution_status is not RunExecutionStatus.COMPLETED:
                raise ValueError("pass qualification requires completed execution")
            if not self.qualification_evidence_references:
                raise ValueError("pass qualification requires independent scoring/review evidence")
        return self


class PreflightFinding(ProtocolModel):
    code: NonEmptyStr
    message: NonEmptyStr
    component_role: ComponentRole | None


class PreflightReport(ProtocolModel):
    accepted: bool
    paid_calls_allowed: bool
    release_evidence_eligible: bool
    findings: tuple[PreflightFinding, ...]
    options_fingerprint: Sha256Digest


class EvaluationPreflightError(ValueError):
    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        summary = "; ".join(f"{item.code}: {item.message}" for item in report.findings)
        super().__init__(f"evaluation preflight rejected: {summary}")


_RETRIEVAL_ROLES = frozenset(
    {ComponentRole.PARSER, ComponentRole.CHUNKER, ComponentRole.EMBEDDING, ComponentRole.INDEX, ComponentRole.RETRIEVER, ComponentRole.RERANKER}
)
_ANSWER_ROLES = frozenset({ComponentRole.GENERATOR, ComponentRole.ANSWER_WORKER, ComponentRole.HARNESS})
_CORE_ROLES = _RETRIEVAL_ROLES | _ANSWER_ROLES | {ComponentRole.SOURCE_READER}


def preflight_evaluation(options: EvaluationOptions) -> PreflightReport:
    """Inspect actual assembled provenance before any provider or paid call."""

    findings: list[PreflightFinding] = []

    def reject(code: str, message: str, role: ComponentRole | None = None) -> None:
        findings.append(PreflightFinding(code=code, message=message, component_role=role))

    components = {item.role: item for item in options.components}
    variant = options.selected_variant
    track = variant.track

    if options.execution_profile is ExecutionProfile.PRODUCTION:
        if options.corpus.source_material != "complete_fulltext" or options.corpus.source_completeness != "complete":
            reject("production_corpus_not_complete", "production requires a complete full-text corpus")
        if options.budget_limits.max_requests == 0 or options.budget_limits.max_total_tokens == 0:
            reject("production_budget_unbounded_or_empty", "production requires positive request and token bounds")
        if (
            options.code_identity.dirty
            and options.experiment_manifest.experiment_purpose
            in {ExperimentPurpose.HELDOUT_QUALIFICATION, ExperimentPurpose.RELEASE_QUALIFICATION}
        ):
            reject(
                "qualification_code_has_mutable_patches",
                "heldout/release qualification requires rebuildable committed code",
            )
        for component in options.components:
            if component.classification is ComponentClassification.NOT_APPLICABLE:
                continue
            if component.classification is not ComponentClassification.PRODUCTION:
                reject("fixture_component_in_production", "production profile contains an applicable fixture component", component.role)
            elif component.has_fixture_marker():
                reject(
                    "fixture_marker_in_production",
                    "observed adapter identity looks like hash/fixed/fake test behavior",
                    component.role,
                )

    required_roles: frozenset[ComponentRole]
    if track is EvaluationTrack.RETRIEVAL:
        required_roles = _CORE_ROLES
        if options.execution_path.kind is not ExecutionPathKind.RETRIEVAL_APPLICATION:
            reject("retrieval_wrong_path", "retrieval track must use the retrieval application path")
        if (
            options.execution_path.answer_generation_enabled
            or not options.execution_path.retrieval_enabled
            or options.execution_path.gated_harness_requested
            or options.execution_path.context_graph_identity is not None
        ):
            reject("retrieval_answer_call_configured", "retrieval track must retrieve without an answer or Harness activity")
        for role in _ANSWER_ROLES:
            component = components.get(role)
            if component is not None and component.classification is not ComponentClassification.NOT_APPLICABLE:
                reject("retrieval_answer_component_applicable", "retrieval track answer components must be not_applicable", role)
    elif track is EvaluationTrack.ORACLE_GENERATION:
        required_roles = _CORE_ROLES
        if options.execution_path.kind is not ExecutionPathKind.ORACLE_ANSWER_WORKER:
            reject("oracle_wrong_path", "oracle_generation must use the evidence-only answer worker path")
        if (
            options.execution_path.retrieval_enabled
            or not options.execution_path.answer_generation_enabled
            or options.execution_path.gated_harness_requested
            or options.execution_path.context_graph_identity is not None
        ):
            reject("oracle_retrieval_enabled", "oracle_generation must use only the evidence-only answer worker")
        for role in _RETRIEVAL_ROLES:
            component = components.get(role)
            if component is not None and component.classification is not ComponentClassification.NOT_APPLICABLE:
                reject(
                    "oracle_retrieval_component_applicable",
                    "oracle_generation retrieval components must be not_applicable with reasons",
                    role,
                )
    else:
        required_roles = _CORE_ROLES
        if options.execution_path.kind is not ExecutionPathKind.RESEARCH_GATED_HARNESS:
            reject("end_to_end_wrong_path", "end_to_end must use the Research application/Harness path")
        if not (
            options.execution_path.retrieval_enabled
            and options.execution_path.answer_generation_enabled
            and options.execution_path.gated_harness_requested
            and options.execution_path.context_graph_identity is not None
        ):
            reject("end_to_end_not_gated", "end_to_end requires retrieval, answer generation, and gated Harness")
        if variant.harness is None or not variant.harness.gated or not variant.harness.plan_execute_verify:
            reject("end_to_end_missing_harness_contract", "end_to_end requires gated PLAN/EXECUTE/VERIFY factors")

    for role in required_roles:
        if role not in components:
            reject("missing_component_provenance", "required component provenance is missing", role)

    for role, component in components.items():
        if role in required_roles and component.classification is ComponentClassification.NOT_APPLICABLE:
            allowed_na = (
                (track is EvaluationTrack.RETRIEVAL and role in _ANSWER_ROLES)
                or (track is EvaluationTrack.ORACLE_GENERATION and role in (_RETRIEVAL_ROLES | {ComponentRole.HARNESS}))
                or role is ComponentRole.RERANKER
            )
            if not allowed_na:
                reject("required_component_not_applicable", "required component is marked not_applicable", role)

    fingerprint = content_digest(options)
    release_evidence_eligible = (
        not findings
        and options.execution_profile is ExecutionProfile.PRODUCTION
        and options.experiment_manifest.experiment_purpose
        in {ExperimentPurpose.HELDOUT_QUALIFICATION, ExperimentPurpose.RELEASE_QUALIFICATION}
        and not options.code_identity.dirty
    )
    return PreflightReport(
        accepted=not findings,
        paid_calls_allowed=not findings,
        release_evidence_eligible=release_evidence_eligible,
        findings=tuple(findings),
        options_fingerprint=fingerprint,
    )


__all__ = [
    "ArtifactReference", "AttemptIdentity", "BudgetLimits", "CachePolicy", "CaseScopeProjection",
    "CodeIdentity", "ComparisonIdentity", "ComponentClassification", "ComponentProvenance", "ComponentRole",
    "CorpusProvenance", "EvaluationOptions", "EvaluationPreflightError", "EvaluationRun", "EvaluationTrack",
    "ContextGraphRequestIdentity", "ExecutionEnvironment", "ExecutionPath", "ExperimentPurpose", "ExecutionPathKind", "ExecutionProfile", "ExperimentManifest",
    "FixtureKind", "GenerationFactors", "HarnessFactors", "InitialState", "InitialStatePolicy",
    "IsolatedNamespaces", "PlannedEvaluationUnit", "PreflightFinding", "PreflightReport",
    "QualificationDecision", "RetrievalFactors", "SourceAccessGrant", "RunControl", "RunExecutionStatus", "VariantConfiguration",
    "preflight_evaluation",
]
