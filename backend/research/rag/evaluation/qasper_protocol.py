"""Immutable QASPER evaluation contracts, not a runner, mapper or scorer.

The v1 Equivalent policy is deliberately pending human review. Version identity
is exact and closed: accepting a future revision requires a code/schema change.
Legacy summaries never become measurements by being reconstructed today.
"""
from __future__ import annotations

import math
from typing import Annotated, Any, Literal, Self

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from backend.research.rag.evaluation.protocol_common import (
    EvaluationTrack, ExecutionProfile, ImmutableVersionStr, NonEmptyStr,
    ProtocolModel, Sha256Digest, content_digest,
)
from backend.research.rag.evaluation.protocol_comparison import ComparisonQualification
from backend.research.rag.evaluation.protocol_evidence_mapping import IndexBinding, IndexEvidenceMapping
from backend.research.rag.evaluation.protocol_execution import (
    ArtifactReference, CodeIdentity, ExperimentManifest, PlannedEvaluationUnit,
)

QASPER_PROTOCOL_VERSION = "qasper-retrieval-evaluation/v1"
QASPER_SCHEMA_VERSION = "1.0"
QASPER_MAPPING_VERSION = "qasper-evidence-mapping/v1"
STRICT_METRIC_VERSION = "qasper-strict-retrieval/v1"
EQUIVALENT_METRIC_VERSION = "qasper-equivalent-retrieval/v1"
FIXTURE_EXECUTION_VERSION = "qasper-fixture-execution/v1"
PRODUCTION_EXECUTION_VERSION = "qasper-production-execution/v1"
EQUIVALENT_MAPPING_REVIEW = "pending_human_review"

MetricStatus = Literal["ok", "needs_review", "system_failure", "not_applicable"]
MappingDisposition = Literal["valid", "needs_review", "system_failure", "not_applicable"]
RetrievalStage = Literal["candidate", "fused", "reranked"]
MetricAggregation = Literal["per_case", "macro_average"]
QasperMetricId = Literal[
    "strict_hit", "strict_coverage", "strict_mrr", "strict_ndcg",
    "equivalent_hit", "equivalent_coverage", "equivalent_mrr", "equivalent_ndcg",
]


def _number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("number must be finite and non-boolean")
    return value


FiniteNumber = Annotated[float, BeforeValidator(_number), Field(allow_inf_nan=False)]
UnitNumber = Annotated[FiniteNumber, Field(ge=0, le=1)]
NonnegativeNumber = Annotated[FiniteNumber, Field(ge=0)]


class _QasperModel(ProtocolModel):
    # Frozen models can still be corrupted via model_copy/model_construct.
    model_config = ConfigDict(revalidate_instances="always")


class _VersionedModel(_QasperModel):
    schema_version: Literal[QASPER_SCHEMA_VERSION] = QASPER_SCHEMA_VERSION
    protocol_version: Literal[QASPER_PROTOCOL_VERSION] = QASPER_PROTOCOL_VERSION


def _revalidate(value: ProtocolModel) -> None:
    """Reconstruct shared contracts too; shared frozen instances may be forged."""
    type(value).model_validate_json(value.model_dump_json())


def _verify(model: ProtocolModel, field: str = "digest") -> None:
    if getattr(model, field) != content_digest(model.model_dump(mode="json", exclude={field})):
        raise ValueError(f"{field} does not match content")


def _create(cls: type, content_type: type, content: dict[str, Any]) -> Any:
    if "digest" in content:
        raise ValueError("digest is calculated; do not provide it to create()")
    normalized = content_type.model_validate(content)
    values = {name: getattr(normalized, name) for name in content_type.model_fields}
    return cls(**values, digest=content_digest(normalized))


def _unique(values: tuple, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


class HistoricalCodeClaim(_QasperModel):
    """Unverified declaration, never an observed historical CodeIdentity."""
    git_revision: ImmutableVersionStr
    source: NonEmptyStr
    claim: NonEmptyStr
    verification: Literal["unverified_handoff_claim"] = "unverified_handoff_claim"


class LegacyMetricSummary(_QasperModel):
    """Raw legacy label/value; no new Strict/Equivalent metric version is implied."""
    metric_id: Annotated[str, Field(strict=True, pattern=r"^legacy_[a-z0-9_]+$")]
    value: UnitNumber
    denominator: int | None = Field(default=None, gt=0, strict=True)
    provenance: Literal["legacy_summary_only"] = "legacy_summary_only"


class QasperEmbeddingCacheIdentity(_QasperModel):
    """Typed offline cache metadata; it is not an embedding provider contract."""
    rows: int = Field(ge=0, strict=True)
    dimension: int = Field(gt=0, strict=True)
    dtype: NonEmptyStr
    keys_sha256: Sha256Digest


class QasperBaselineSource(_QasperModel):
    role: Literal["dataset", "corpus", "chunks", "embedding_cache", "raw_result", "reconstruction_script"]
    artifact: ArtifactReference

    @model_validator(mode="after")
    def _artifact(self) -> Self:
        _revalidate(self.artifact)
        return self


class _BaselineContent(_VersionedModel):
    baseline_id: NonEmptyStr
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_sha256: Sha256Digest
    corpus_id: NonEmptyStr
    corpus_version: ImmutableVersionStr
    corpus_sha256: Sha256Digest
    chunking_version: ImmutableVersionStr
    chunk_count: int = Field(ge=0, strict=True)
    dataset_case_count: int = Field(ge=0, strict=True)
    evaluable_case_count: int = Field(ge=0, strict=True)
    embedding_cache_id: NonEmptyStr
    embedding_cache_sha256: Sha256Digest
    embedding_model_id: NonEmptyStr
    embedding_model_version: ImmutableVersionStr
    embedding_dimension: int = Field(gt=0, strict=True)
    embedding_cache_identity: QasperEmbeddingCacheIdentity | None = None
    raw_result_sha256: Sha256Digest
    historical_code_claim: HistoricalCodeClaim
    observed_reconstruction_code_identity: CodeIdentity
    legacy_summary: tuple[LegacyMetricSummary, ...]
    legacy_count_policy: Literal["aligned_with_snapshot", "explicit_diagnostic_mismatch"] = (
        "aligned_with_snapshot"
    )
    chunk_sha256: Sha256Digest | None = None
    paper_count: int = Field(ge=0, strict=True)
    float_evidence_count: int | None = Field(default=None, ge=0, strict=True)
    unanswerable_annotation_count: int | None = Field(default=None, ge=0, strict=True)
    sources: tuple[QasperBaselineSource, ...] = ()
    execution_profile: ExecutionProfile
    historical_runtime_evidence: Literal["unavailable"] = "unavailable"
    qualification: Literal["diagnostic_only"] = "diagnostic_only"
    qualification_reason: Literal["Legacy summary has no per-case runtime/code proof and no versioned metric identity."] = (
        "Legacy summary has no per-case runtime/code proof and no versioned metric identity."
    )

    @model_validator(mode="after")
    def _content(self) -> Self:
        _revalidate(self.observed_reconstruction_code_identity)
        if self.execution_profile is None:
            raise ValueError("baseline execution_profile must be explicit")
        if self.embedding_cache_identity is not None:
            if self.embedding_cache_identity.dimension != self.embedding_dimension:
                raise ValueError("embedding cache metadata does not match baseline embedding dimension")
        if self.evaluable_case_count > self.dataset_case_count:
            raise ValueError("evaluable_case_count cannot exceed dataset_case_count")
        _unique(tuple(x.metric_id for x in self.legacy_summary), "legacy metric ids")
        has_count_mismatch = any(
            item.denominator is not None and item.denominator != self.evaluable_case_count
            for item in self.legacy_summary
        )
        if has_count_mismatch and self.legacy_count_policy != "explicit_diagnostic_mismatch":
            raise ValueError(
                "legacy metric denominators differ from evaluable_case_count; "
                "set allow_legacy_count_mismatch=True for diagnostic-only capture"
            )
        if not has_count_mismatch and self.legacy_count_policy == "explicit_diagnostic_mismatch":
            raise ValueError("legacy count mismatch override requires an actual denominator mismatch")
        _unique(tuple(x.role for x in self.sources), "baseline source roles")
        expected = {
            "dataset": self.dataset_sha256, "corpus": self.corpus_sha256,
            "chunks": self.chunk_sha256, "embedding_cache": self.embedding_cache_sha256,
            "raw_result": self.raw_result_sha256,
        }
        for source in self.sources:
            if source.role in expected and source.artifact.content_sha256 != expected[source.role]:
                raise ValueError(f"baseline source digest mismatch: {source.role}")
        return self


class QasperBaselineSnapshot(_BaselineContent):
    """Frozen reconstruction identity; its legacy values are diagnostic-only."""
    digest: Sha256Digest

    @classmethod
    def create(cls, *, allow_legacy_count_mismatch: bool = False, **content: Any) -> Self:
        if not isinstance(allow_legacy_count_mismatch, bool):
            raise TypeError("allow_legacy_count_mismatch must be a boolean")
        requested_policy = content.get("legacy_count_policy", "aligned_with_snapshot")
        if allow_legacy_count_mismatch:
            if requested_policy != "aligned_with_snapshot":
                raise ValueError("legacy count policy must be selected through allow_legacy_count_mismatch")
            content["legacy_count_policy"] = "explicit_diagnostic_mismatch"
        elif requested_policy == "explicit_diagnostic_mismatch":
            raise ValueError("legacy count mismatch requires allow_legacy_count_mismatch=True")
        return _create(cls, _BaselineContent, content)

    def digest_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"digest"})

    def verify_digest(self) -> None:
        _revalidate(self.observed_reconstruction_code_identity)
        _verify(self)

    @model_validator(mode="after")
    def _digest(self) -> Self:
        self.verify_digest()
        return self

    @property
    def unmappable_case_count(self) -> int:
        return self.dataset_case_count - self.evaluable_case_count


class QasperDataIdentity(_QasperModel):
    """Protocol dataset digest is separate from the raw dataset file SHA."""
    dataset_id: NonEmptyStr
    dataset_version: ImmutableVersionStr
    dataset_digest: Sha256Digest
    dataset_sha256: Sha256Digest
    dataset_case_count: int = Field(ge=0, strict=True)
    evaluable_case_count: int = Field(ge=0, strict=True)
    corpus_snapshot_id: NonEmptyStr
    corpus_sha256: Sha256Digest
    chunk_snapshot_id: NonEmptyStr
    chunk_sha256: Sha256Digest
    chunking_version: ImmutableVersionStr
    chunk_count: int = Field(ge=0, strict=True)
    paper_count: int = Field(ge=0, strict=True)
    embedding_cache_id: NonEmptyStr
    embedding_cache_sha256: Sha256Digest
    embedding_model_id: NonEmptyStr
    embedding_model_version: ImmutableVersionStr
    embedding_dimension: int = Field(gt=0, strict=True)


class QasperMetricVersions(_QasperModel):
    # The pair (metric_id, version) is the independent immutable metric identity.
    strict_hit: Literal[STRICT_METRIC_VERSION] = STRICT_METRIC_VERSION
    strict_coverage: Literal[STRICT_METRIC_VERSION] = STRICT_METRIC_VERSION
    strict_mrr: Literal[STRICT_METRIC_VERSION] = STRICT_METRIC_VERSION
    strict_ndcg: Literal[STRICT_METRIC_VERSION] = STRICT_METRIC_VERSION
    equivalent_hit: Literal[EQUIVALENT_METRIC_VERSION] = EQUIVALENT_METRIC_VERSION
    equivalent_coverage: Literal[EQUIVALENT_METRIC_VERSION] = EQUIVALENT_METRIC_VERSION
    equivalent_mrr: Literal[EQUIVALENT_METRIC_VERSION] = EQUIVALENT_METRIC_VERSION
    equivalent_ndcg: Literal[EQUIVALENT_METRIC_VERSION] = EQUIVALENT_METRIC_VERSION


class QasperMappingPolicy(_QasperModel):
    mapping_version: Literal[QASPER_MAPPING_VERSION] = QASPER_MAPPING_VERSION
    review_status: Literal["pending_human_review"] = EQUIVALENT_MAPPING_REVIEW
    # v1 assigns a contract identity, not an approved overlap threshold.
    frozen_overlap_threshold: None = None
    review_reason: NonEmptyStr = "Equivalent overlap threshold awaits human review; no approved policy is claimed."


class QasperExecutionIdentity(_QasperModel):
    execution_profile: ExecutionProfile
    version: Literal[FIXTURE_EXECUTION_VERSION, PRODUCTION_EXECUTION_VERSION]
    implementation_id: NonEmptyStr
    configuration_sha256: Sha256Digest

    @model_validator(mode="after")
    def _version(self) -> Self:
        expected = FIXTURE_EXECUTION_VERSION if self.execution_profile is ExecutionProfile.FIXTURE else PRODUCTION_EXECUTION_VERSION
        if self.version != expected:
            raise ValueError("execution profile version does not match fixture/production identity")
        return self


class QasperMetricDefinition(_QasperModel):
    metric_id: QasperMetricId
    version: Literal[STRICT_METRIC_VERSION, EQUIVALENT_METRIC_VERSION]
    k: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def _metric_identity(self) -> Self:
        expected = STRICT_METRIC_VERSION if self.metric_id.startswith("strict_") else EQUIVALENT_METRIC_VERSION
        if self.version != expected:
            raise ValueError("unknown or incompatible QASPER metric version")
        if not self.metric_id.endswith("_mrr") and self.k is None:
            raise ValueError("Hit/Coverage/nDCG require an explicit K")
        return self

    @property
    def key(self) -> tuple[str, int | None]:
        return self.metric_id, self.k


def _dual_metrics(metrics: tuple[QasperMetricDefinition, ...]) -> None:
    keys = tuple(m.key for m in metrics)
    _unique(keys, "metric id/K pairs")
    for metric_id, k in keys:
        paired = ("equivalent_" + metric_id.removeprefix("strict_"), k) if metric_id.startswith("strict_") else ("strict_" + metric_id.removeprefix("equivalent_"), k)
        if paired not in keys:
            raise ValueError("Strict and Equivalent metrics must retain the same metric/K cohort")


class _ManifestContent(_VersionedModel):
    experiment_manifest: ExperimentManifest
    data_identity: QasperDataIdentity
    index_binding: IndexBinding
    mapping_artifact: ArtifactReference
    mapping_policy: QasperMappingPolicy = Field(default_factory=QasperMappingPolicy)
    metric_versions: QasperMetricVersions = Field(default_factory=QasperMetricVersions)
    metric_definitions: tuple[QasperMetricDefinition, ...] = Field(min_length=1)
    execution_identity: QasperExecutionIdentity
    code_identity: CodeIdentity

    @model_validator(mode="after")
    def _links(self) -> Self:
        for item in (self.experiment_manifest, self.index_binding, self.mapping_artifact, self.code_identity):
            _revalidate(item)
        shared = self.experiment_manifest
        data = self.data_identity
        if (shared.dataset_id, shared.dataset_version, shared.dataset_digest) != (data.dataset_id, data.dataset_version, data.dataset_digest):
            raise ValueError("dataset identity differs from wrapped ExperimentManifest")
        if (self.index_binding.corpus_snapshot_id != data.corpus_snapshot_id
                or self.index_binding.index_content_sha256 != data.chunk_sha256):
            raise ValueError("corpus/index identity differs from IndexBinding")
        _dual_metrics(self.metric_definitions)
        if {m.metric_id.split("_", 1)[1] for m in self.metric_definitions} != {"hit", "coverage", "mrr", "ndcg"}:
            raise ValueError("manifest must declare all four Strict/Equivalent metric families")
        if any(v.track is not EvaluationTrack.RETRIEVAL for v in shared.variants):
            raise ValueError("QASPER retrieval manifest cannot wrap generation/end-to-end variants")
        if not shared.comparison_identities:
            raise ValueError("wrapped manifest must declare comparison identities")
        for variant in shared.variants:
            identities = [i for i in shared.comparison_identities if i.variant_fingerprint == variant.variant_fingerprint]
            if len(identities) != 1:
                raise ValueError("each retrieval variant requires one execution comparison identity")
            identity = identities[0]
            if (identity.dataset_digest != data.dataset_digest
                    or identity.corpus_content_sha256 != data.corpus_sha256
                    or identity.execution_profile is not self.execution_identity.execution_profile
                    or identity.code_fingerprint != self.code_identity.code_fingerprint
                    or identity.track is not EvaluationTrack.RETRIEVAL):
                raise ValueError("wrapped comparison dataset/corpus/execution/code identity mismatch")
            if variant.retrieval is None or (
                variant.retrieval.parser_config_hash != self.index_binding.parser_config_sha256
                or variant.retrieval.chunker_config_hash != self.index_binding.chunker_config_sha256
                or variant.retrieval.index_config_hash != self.index_binding.index_config_sha256
            ):
                raise ValueError("variant parser/chunker/index differs from mapping IndexBinding")
        return self


class QasperExperimentManifest(_ManifestContent):
    """QASPER resource/metric bindings around the existing authoritative plan."""
    digest: Sha256Digest

    @classmethod
    def create(cls, **content: Any) -> Self:
        return _create(cls, _ManifestContent, content)

    def verify_digest(self) -> None:
        _revalidate(self.experiment_manifest)
        _verify(self)

    @model_validator(mode="after")
    def _digest(self) -> Self:
        self.verify_digest()
        return self


class QasperMetric(QasperMetricDefinition):
    """A supplied observation, not a calculation. Non-ok never means zero."""
    mapping_version: Literal[QASPER_MAPPING_VERSION] = QASPER_MAPPING_VERSION
    value: UnitNumber | None
    status: MetricStatus
    numerator: NonnegativeNumber | None
    denominator: int | None = Field(default=None, ge=0, strict=True)
    reason: NonEmptyStr | None
    aggregation: MetricAggregation = "per_case"

    @model_validator(mode="after")
    def _observation(self) -> Self:
        if self.status == "ok":
            if self.value is None or self.numerator is None or self.denominator is None or self.denominator <= 0 or self.reason is not None:
                raise ValueError("ok metric requires value and nonzero counts, without failure reason")
            if self.metric_id.endswith("_hit") and not float(self.numerator).is_integer():
                raise ValueError("Hit numerator must be an integer count")
            if self.numerator > self.denominator or not math.isclose(self.value, self.numerator / self.denominator, rel_tol=1e-9, abs_tol=1e-12):
                raise ValueError("metric value must match numerator/denominator")
        elif self.value is not None or self.numerator is not None or self.denominator is not None or self.reason is None:
            raise ValueError("non-ok metric requires null value/counts and an explicit reason")
        return self


class QasperMappingResult(_VersionedModel):
    case_id: NonEmptyStr
    disposition: MappingDisposition
    mapping: IndexEvidenceMapping | None
    mapping_version: Literal[QASPER_MAPPING_VERSION] = QASPER_MAPPING_VERSION
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _mapping(self) -> Self:
        if self.mapping is not None:
            _revalidate(self.mapping)
            if self.mapping.case_id != self.case_id or self.mapping.mapping_version != self.mapping_version:
                raise ValueError("mapping result case/version differs from IndexEvidenceMapping")
        if self.disposition == "valid":
            if self.mapping is None or self.reason is not None:
                raise ValueError("valid mapping requires frozen IndexEvidenceMapping without failure reason")
            if not self.mapping.anchor_mappings or any(x.disposition != "valid" for x in self.mapping.anchor_mappings):
                raise ValueError("valid mapping cannot hide missing or non-valid anchor mappings")
        elif self.reason is None:
            raise ValueError("non-valid mapping requires an explicit reason")
        return self

    @property
    def mapping_digest(self) -> str | None:
        return self.mapping.mapping_digest if self.mapping else None


class QasperChannelProvenance(_QasperModel):
    channel: Literal["dense", "bm25", "field"]
    query_id: NonEmptyStr
    query_sha256: Sha256Digest
    channel_rank: int = Field(gt=0, strict=True)
    raw_score: FiniteNumber | None
    field: NonEmptyStr | None = None


class RankedResult(_QasperModel):
    """Original rows remain ordered, including duplicate chunk IDs."""
    chunk_id: NonEmptyStr
    rank: int = Field(gt=0, strict=True)
    stage: RetrievalStage
    raw_score: FiniteNumber | None
    provenance: tuple[QasperChannelProvenance, ...] = Field(min_length=1)


class QasperFailureRecord(_VersionedModel):
    unit: PlannedEvaluationUnit
    category: Literal["candidate_recall_miss", "fusion_rerank_miss", "top_k_miss", "partial_coverage", "mapping_failure", "source_index_failure", "system_failure", "float_evidence", "unanswerable", "not_applicable"]
    status: MetricStatus
    reason: NonEmptyStr
    stage: Literal["source", "index", "mapping", "candidate", "fusion", "rerank", "scoring"]
    affected_metrics: tuple[QasperMetricDefinition, ...] = Field(min_length=1)
    evidence: tuple[ArtifactReference, ...] = ()

    @model_validator(mode="after")
    def _failure(self) -> Self:
        _revalidate(self.unit)
        for artifact in self.evidence:
            _revalidate(artifact)
        if self.status == "ok" and self.category not in {"candidate_recall_miss", "fusion_rerank_miss", "top_k_miss", "partial_coverage"}:
            raise ValueError("only measured retrieval misses/partial coverage may have ok status")
        _unique(tuple(x.key for x in self.affected_metrics), "affected metrics")
        return self


class QasperCaseResult(_VersionedModel):
    manifest_digest: Sha256Digest
    run_id: NonEmptyStr
    attempt_id: NonEmptyStr
    unit: PlannedEvaluationUnit
    query: NonEmptyStr
    paper_ids: tuple[NonEmptyStr, ...] = Field(min_length=1)
    status: MetricStatus
    mapping_result: QasperMappingResult
    final_stage: RetrievalStage
    raw_ranked_results: tuple[RankedResult, ...]
    metrics: tuple[QasperMetric, ...] = Field(min_length=1)
    failures: tuple[QasperFailureRecord, ...] = ()
    reason: NonEmptyStr | None

    @model_validator(mode="after")
    def _case(self) -> Self:
        _revalidate(self.unit)
        if self.unit.track is not EvaluationTrack.RETRIEVAL:
            raise ValueError("case must use a retrieval planned unit")
        if self.unit.case_id != self.mapping_result.case_id:
            raise ValueError("case unit differs from mapping case")
        _unique(self.paper_ids, "paper ids")
        _dual_metrics(self.metrics)
        stage_order = ("candidate", "fused", "reranked")
        for stage in stage_order:
            ranks = tuple(x.rank for x in self.raw_ranked_results if x.stage == stage)
            if ranks != tuple(range(1, len(ranks) + 1)):
                raise ValueError("raw ranks must remain contiguous and ordered within each stage")
        final_index = stage_order.index(self.final_stage)
        if any(stage_order.index(row.stage) > final_index for row in self.raw_ranked_results):
            raise ValueError("raw results cannot contain a stage after final_stage")
        if any(metric.aggregation != "per_case" for metric in self.metrics):
            raise ValueError("case metrics must retain per-case aggregation semantics")
        if self.status == "ok":
            if self.reason is not None or self.mapping_result.disposition != "valid" or any(m.status != "ok" for m in self.metrics):
                raise ValueError("ok case requires valid mapping and ok dual metrics without failure reason")
        elif self.reason is None or any(m.status == "ok" for m in self.metrics) or not self.failures:
            raise ValueError("non-ok case requires null metrics, explicit reason and failure records")
        keys = {m.key for m in self.metrics}
        for failure in self.failures:
            if failure.unit != self.unit or not {m.key for m in failure.affected_metrics}.issubset(keys):
                raise ValueError("failure unit/metric identity differs from case")
        return self


class QasperDenominators(_QasperModel):
    planned: int = Field(ge=0, strict=True)
    valid: int = Field(ge=0, strict=True)
    needs_review: int = Field(ge=0, strict=True)
    system_failure: int = Field(ge=0, strict=True)
    not_applicable: int = Field(ge=0, strict=True)

    @property
    def non_ok(self) -> int:
        return self.needs_review + self.system_failure + self.not_applicable

    @model_validator(mode="after")
    def _partition(self) -> Self:
        if self.valid + self.non_ok != self.planned:
            raise ValueError("valid and non-ok counts must partition planned denominator")
        return self


class QasperReportFiles(_QasperModel):
    manifest: ArtifactReference
    cases: ArtifactReference
    metrics: ArtifactReference
    failures: ArtifactReference
    report: ArtifactReference

    @model_validator(mode="after")
    def _references(self) -> Self:
        refs = tuple(getattr(self, name) for name in type(self).model_fields)
        for ref in refs:
            _revalidate(ref)
        _unique(tuple(r.reference_id for r in refs), "report artifact reference ids")
        _unique(tuple(r.uri for r in refs), "report artifact URIs")
        return self


class QasperReportArtifact(_VersionedModel):
    """A validated report projection, not a second run/event/score store."""
    manifest: QasperExperimentManifest
    run_id: NonEmptyStr
    selected_variant_fingerprint: Sha256Digest
    case_results: tuple[QasperCaseResult, ...]
    denominators: QasperDenominators
    metrics: tuple[QasperMetric, ...] = Field(min_length=1)
    artifacts: QasperReportFiles
    qualification: Literal["diagnostic_only"] = "diagnostic_only"
    qualification_reason: NonEmptyStr = "Equivalent policy awaits human review; this artifact does not authorize release or production ranking."

    @model_validator(mode="after")
    def _report(self) -> Self:
        _revalidate(self.manifest)
        shared = self.manifest.experiment_manifest
        plan = tuple(u for u in shared.planned_units if u.variant_fingerprint == self.selected_variant_fingerprint)
        if not plan:
            raise ValueError("report selected variant is not planned in ExperimentManifest")
        actual = tuple(c.unit.primary_key for c in self.case_results)
        _unique(actual, "case unit primary keys")
        if set(actual) != {u.primary_key for u in plan} or len(actual) != self.denominators.planned:
            raise ValueError("report must retain every selected planned unit, including non-ok units")
        statuses = {"ok": self.denominators.valid, "needs_review": self.denominators.needs_review,
                    "system_failure": self.denominators.system_failure, "not_applicable": self.denominators.not_applicable}
        if any(sum(c.status == status for c in self.case_results) != count for status, count in statuses.items()):
            raise ValueError("denominator status counts differ from case results")
        definitions = self.manifest.metric_definitions
        expected = {m.key for m in definitions}
        _dual_metrics(self.metrics)
        if {m.key for m in self.metrics} != expected:
            raise ValueError("report metrics must match frozen manifest definitions")
        for case in self.case_results:
            if case.manifest_digest != self.manifest.digest or case.run_id != self.run_id or {m.key for m in case.metrics} != expected:
                raise ValueError("case run/manifest/metric identity mismatch")
            mapping = case.mapping_result.mapping
            if mapping is not None:
                data = self.manifest.data_identity
                if ((mapping.dataset_id, mapping.dataset_version, mapping.dataset_digest) != (data.dataset_id, data.dataset_version, data.dataset_digest)
                        or mapping.binding != self.manifest.index_binding):
                    raise ValueError("case mapping dataset/index binding mismatch")
        for metric in self.metrics:
            if metric.aggregation != "macro_average":
                raise ValueError("report metrics must declare macro_average aggregation")
            if self.denominators.valid == 0:
                if metric.status == "ok":
                    raise ValueError("zero valid denominator cannot produce an ok metric")
            elif metric.status != "ok" or metric.denominator != self.denominators.valid:
                raise ValueError("aggregate metrics must use the aligned valid case denominator; planned/non-ok remain explicit")
        return self


def qualify_qasper_comparison(
    baseline: QasperExperimentManifest | QasperBaselineSnapshot,
    candidate: QasperExperimentManifest | QasperBaselineSnapshot,
) -> ComparisonQualification:
    """Resource/version compatibility only, never a statistical ranking gate.

    A matching legacy snapshot is still diagnostic-only. A future reviewed
    protocol must also use the shared runtime comparison/preflight contracts.
    """
    try:
        _revalidate(baseline)
        _revalidate(candidate)
    except (ValueError, TypeError) as exc:
        return ComparisonQualification(status="incomparable", ranking_eligible=False,
                                       reasons=(f"invalid QASPER identity/digest: {exc}",))
    reasons: list[str] = []
    if isinstance(baseline, QasperExperimentManifest) and isinstance(candidate, QasperExperimentManifest):
        for name in ("schema_version", "protocol_version", "data_identity", "index_binding", "mapping_artifact", "mapping_policy", "metric_versions", "metric_definitions", "execution_identity", "code_identity"):
            if getattr(baseline, name) != getattr(candidate, name):
                reasons.append(f"fixed QASPER comparison identity changed: {name}")
        if baseline.experiment_manifest.manifest_digest != candidate.experiment_manifest.manifest_digest:
            reasons.append("comparison requires the same predeclared ExperimentManifest")
    else:
        def projection(value: QasperExperimentManifest | QasperBaselineSnapshot) -> tuple:
            if isinstance(value, QasperExperimentManifest):
                d = value.data_identity
                return (d.dataset_id, d.dataset_version, d.dataset_sha256, d.dataset_case_count,
                        d.evaluable_case_count, d.corpus_snapshot_id, d.corpus_sha256,
                        d.chunk_snapshot_id, d.chunk_sha256, d.chunking_version, d.chunk_count,
                        d.paper_count, d.embedding_cache_id,
                        d.embedding_cache_sha256, d.embedding_model_id, d.embedding_model_version,
                        d.embedding_dimension, value.execution_identity.execution_profile)
            return (value.dataset_id, value.dataset_version, value.dataset_sha256,
                    value.dataset_case_count, value.evaluable_case_count, value.corpus_id,
                    value.corpus_sha256, value.chunk_sha256, value.chunking_version,
                    value.chunk_count, value.paper_count, value.embedding_cache_id,
                    value.embedding_cache_sha256, value.embedding_model_id, value.embedding_model_version,
                    value.embedding_dimension, value.execution_profile)
        if projection(baseline) != projection(candidate):
            reasons.append("legacy/current dataset/corpus/chunk/cache/embedding/execution identity mismatch")
        if isinstance(baseline, QasperBaselineSnapshot) and isinstance(candidate, QasperBaselineSnapshot):
            if baseline.chunking_version != candidate.chunking_version or baseline.raw_result_sha256 != candidate.raw_result_sha256:
                reasons.append("legacy chunking/raw result identity mismatch")
    if reasons:
        return ComparisonQualification(status="incomparable", ranking_eligible=False, reasons=tuple(reasons))
    return ComparisonQualification(status="diagnostic_only", ranking_eligible=False,
                                   reasons=("Legacy summaries lack runtime proof; Equivalent v1 policy is pending human review. Compatibility does not confer formal measurement comparability.",))


__all__ = [
    "QASPER_PROTOCOL_VERSION", "QASPER_SCHEMA_VERSION", "QASPER_MAPPING_VERSION",
    "STRICT_METRIC_VERSION", "EQUIVALENT_METRIC_VERSION", "FIXTURE_EXECUTION_VERSION",
    "PRODUCTION_EXECUTION_VERSION", "EQUIVALENT_MAPPING_REVIEW", "MetricStatus",
    "MappingDisposition", "RetrievalStage", "MetricAggregation", "QasperMetricId", "HistoricalCodeClaim", "LegacyMetricSummary",
    "QasperEmbeddingCacheIdentity", "QasperBaselineSource", "QasperBaselineSnapshot", "QasperDataIdentity",
    "QasperMetricVersions", "QasperMappingPolicy", "QasperExecutionIdentity",
    "QasperMetricDefinition", "QasperExperimentManifest", "QasperMetric",
    "QasperMappingResult", "QasperChannelProvenance", "RankedResult", "QasperFailureRecord",
    "QasperCaseResult", "QasperDenominators", "QasperReportFiles", "QasperReportArtifact",
    "qualify_qasper_comparison",
]
