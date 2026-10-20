"""Versioned contracts for auditable PDF parsing evaluation runs.

These records describe evidence and outcomes; they do not execute parsers,
score gold data, or decide release promotion.
"""

from __future__ import annotations

import math
from collections import Counter
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    NonEmptyStr,
    ProtocolModel,
    Sha256Digest,
)

PARSING_EVALUATION_PROTOCOL = "research-document-parsing-eval/v1"


class ParsingTrack(StrEnum):
    BACKEND_RAW = "backend_raw"
    PRODUCTION_DOCUMENT = "production_document"
    DOWNSTREAM_PROJECTION = "downstream_projection"


class ExecutionKind(StrEnum):
    DIAGNOSTIC = "diagnostic"
    PRODUCTION = "production"


class InputMode(StrEnum):
    NATIVE_PDF = "native_pdf"
    RASTER_PDF = "raster_pdf"
    PAGE_IMAGE = "page_image"
    UNKNOWN = "unknown"


class Exposure(StrEnum):
    EXPOSED_DEV = "exposed_dev"
    FROZEN_HELDOUT = "frozen_heldout"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PageStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    INVALID = "invalid"


class MetricStatus(StrEnum):
    SUCCEEDED = "succeeded"
    NOT_APPLICABLE = "not_applicable"
    UNMATCHED = "unmatched"
    MISSING_OUTPUT = "missing_output"
    ERROR = "error"
    TIMEOUT = "timeout"
    INVALID = "invalid"


class AggregationUnit(StrEnum):
    PAGE = "page"
    OBJECT = "object"
    SAMPLE = "sample"


class IdentityValue(ProtocolModel):
    """A known immutable identity or an explicit, reasoned unknown."""

    value: NonEmptyStr | None = None
    unknown_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _consistent_identity(self) -> Self:
        has_value = self.value is not None
        has_unknown_reason = self.unknown_reason is not None
        if has_value == has_unknown_reason:
            raise ValueError("provide exactly one of value or unknown_reason")
        if self.value is not None:
            terminal = self.value.casefold().rstrip("/\\").replace(":", "/").split("/")[-1]
            if terminal in {"latest", "current", "head"}:
                raise ValueError("identity value must be immutable, not a floating alias")
        return self


class ParsingFingerprints(ProtocolModel):
    dataset: IdentityValue
    annotations: IdentityValue
    evaluator_commit: IdentityValue
    evaluator_config_sha256: IdentityValue
    parser: IdentityValue
    parser_image_digest: IdentityValue
    parser_weights_digest: IdentityValue
    parser_config_sha256: IdentityValue
    code_revision: IdentityValue
    dirty_content_sha256: IdentityValue
    document_schema: IdentityValue
    normalizer: IdentityValue
    exporter: IdentityValue
    chunker: IdentityValue
    hardware: IdentityValue

    @model_validator(mode="after")
    def _validate_sha256_fields(self) -> Self:
        for name in ("evaluator_config_sha256", "parser_config_sha256", "dirty_content_sha256"):
            identity = getattr(self, name)
            if identity.value is not None and (
                len(identity.value) != 71
                or not identity.value.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in identity.value[7:])
            ):
                raise ValueError(f"{name} must be a sha256:<64 lowercase hex> identity")
        if self.parser_image_digest.value is not None and (
            len(self.parser_image_digest.value) != 71
            or not self.parser_image_digest.value.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in self.parser_image_digest.value[7:])
        ):
            raise ValueError("parser_image_digest must be a sha256:<64 lowercase hex> identity")
        return self


class ParsingExecutionIdentity(ProtocolModel):
    kind: ExecutionKind
    composition: IdentityValue
    execution_receipt_refs: tuple[NonEmptyStr, ...] = ()
    parser_invocation: IdentityValue

    @model_validator(mode="after")
    def _production_requires_receipt(self) -> Self:
        if len(self.execution_receipt_refs) != len(set(self.execution_receipt_refs)):
            raise ValueError("execution receipt references must be unique")
        if self.kind is ExecutionKind.PRODUCTION:
            if self.composition.value is None:
                raise ValueError("production execution requires a known composition identity")
            if not self.execution_receipt_refs:
                raise ValueError("production execution requires execution receipt references")
        return self


class ParsingBudget(ProtocolModel):
    max_pages: int = Field(gt=0)
    timeout_seconds_per_page: float = Field(gt=0, allow_inf_nan=False)
    max_total_seconds: float = Field(gt=0, allow_inf_nan=False)
    max_concurrency: int = Field(gt=0)
    max_retries: int = Field(ge=0)
    max_roi_attempts: int = Field(ge=0)


class RequestedPage(ProtocolModel):
    page_id: NonEmptyStr
    input_sha256: Sha256Digest
    input_mode: InputMode
    input_mode_reason: NonEmptyStr | None = None
    source_document_id: NonEmptyStr | None
    source_group_id: NonEmptyStr | None
    source_mapping_reason: NonEmptyStr | None = None
    exposure: Exposure
    batch_id: NonEmptyStr | None = None
    batch_page_index: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _source_mapping_is_explicit(self) -> Self:
        if self.input_mode is InputMode.UNKNOWN and self.input_mode_reason is None:
            raise ValueError("unknown input mode requires a reason")
        if self.input_mode is not InputMode.UNKNOWN and self.input_mode_reason is not None:
            raise ValueError("known input mode must not include an unknown reason")
        if self.exposure is Exposure.FROZEN_HELDOUT and (
            self.source_document_id is None or self.source_group_id is None
        ):
            raise ValueError("heldout pages require known document and version-family identities")
        if self.source_document_id is None and self.source_group_id is None:
            if self.source_mapping_reason is None:
                raise ValueError("unknown document mapping requires a reason")
        elif self.source_document_id is None or self.source_group_id is None:
            raise ValueError("document and version-family identities must be both known or both unknown")
        elif self.source_mapping_reason is not None:
            raise ValueError("known source mapping must not include an unknown reason")
        return self


class MetricSpec(ProtocolModel):
    metric: NonEmptyStr
    aggregation_unit: AggregationUnit


class MetricOutcome(ProtocolModel):
    metric: NonEmptyStr
    status: MetricStatus
    aggregation_unit: AggregationUnit
    value: float | None = None
    support_id: NonEmptyStr | None = None
    reason: NonEmptyStr | None = None

    @field_validator("value")
    @classmethod
    def _finite_metric_value(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("metric value must be finite")
        return value

    @model_validator(mode="after")
    def _status_value_consistency(self) -> Self:
        if self.aggregation_unit is AggregationUnit.OBJECT and self.support_id is None:
            raise ValueError("object-level metric outcomes require a support_id")
        if self.status is MetricStatus.SUCCEEDED:
            if self.value is None:
                raise ValueError("successful metric outcome requires a value")
            if self.reason is not None:
                raise ValueError("successful metric outcome must not include a failure reason")
        else:
            if self.value is not None:
                raise ValueError("non-successful metric outcome must not include a value")
            if self.reason is None:
                raise ValueError("non-successful metric outcome requires a reason")
        return self


class PageOutcome(ProtocolModel):
    page_id: NonEmptyStr
    status: PageStatus
    reason: NonEmptyStr | None = None
    metrics: tuple[MetricOutcome, ...] = ()

    @model_validator(mode="after")
    def _consistent_outcome(self) -> Self:
        if self.status is PageStatus.SUCCEEDED and self.reason is not None:
            raise ValueError("successful page outcome must not include a failure reason")
        if self.status in {PageStatus.FAILED, PageStatus.SKIPPED, PageStatus.INVALID} and self.reason is None:
            raise ValueError("failed, skipped, and invalid page outcomes require a reason")
        metric_keys = [(metric.metric, metric.aggregation_unit, metric.support_id) for metric in self.metrics]
        if len(metric_keys) != len(set(metric_keys)):
            raise ValueError("page metric outcomes must have unique metric/unit/support identities")
        if self.status is not PageStatus.SUCCEEDED and any(
            metric.status is MetricStatus.SUCCEEDED for metric in self.metrics
        ):
            raise ValueError("a metric cannot succeed when its page parse did not succeed")
        return self


class ParsingEvaluationManifest(ProtocolModel):
    protocol: str = PARSING_EVALUATION_PROTOCOL
    run_id: NonEmptyStr
    status: RunStatus
    track: ParsingTrack
    execution: ParsingExecutionIdentity
    fingerprints: ParsingFingerprints
    budget: ParsingBudget
    metrics: tuple[MetricSpec, ...]
    requested_pages: tuple[RequestedPage, ...]
    outcomes: tuple[PageOutcome, ...]

    @model_validator(mode="after")
    def _complete_page_accounting(self) -> Self:
        if self.protocol != PARSING_EVALUATION_PROTOCOL:
            raise ValueError(f"unsupported parsing evaluation protocol: {self.protocol}")
        if not self.requested_pages:
            raise ValueError("evaluation must request at least one page")
        if not self.metrics:
            raise ValueError("evaluation must declare its planned metrics")
        metric_specs = [(item.metric, item.aggregation_unit) for item in self.metrics]
        if len(metric_specs) != len(set(metric_specs)):
            raise ValueError("planned metric identities must be unique")
        requested_ids = [page.page_id for page in self.requested_pages]
        outcome_ids = [outcome.page_id for outcome in self.outcomes]
        if len(requested_ids) != len(set(requested_ids)):
            raise ValueError("requested page ids must be unique")
        batch_locations = [
            (page.batch_id, page.batch_page_index)
            for page in self.requested_pages
            if page.batch_id is not None or page.batch_page_index is not None
        ]
        if any((page.batch_id is None) != (page.batch_page_index is None) for page in self.requested_pages):
            raise ValueError("batch mapping requires both batch_id and batch_page_index")
        if len(batch_locations) != len(set(batch_locations)):
            raise ValueError("batch page mappings must be unique and reversible")
        if self.budget.max_pages < len(self.requested_pages):
            raise ValueError("max_pages budget is smaller than requested page count")
        if len(outcome_ids) != len(set(outcome_ids)):
            raise ValueError("page outcome ids must be unique")
        planned_metric_set = set(metric_specs)
        terminal_page_statuses = {PageStatus.SUCCEEDED, PageStatus.FAILED, PageStatus.SKIPPED, PageStatus.INVALID}
        page_statuses = {outcome.status for outcome in self.outcomes}
        if self.status is RunStatus.PLANNED and page_statuses != {PageStatus.PLANNED}:
            raise ValueError("planned run must contain only planned page outcomes")
        if self.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED} and not page_statuses.issubset(terminal_page_statuses):
            raise ValueError("terminal run must not contain planned or running page outcomes")
        if set(requested_ids) != set(outcome_ids):
            missing = sorted(set(requested_ids) - set(outcome_ids))
            unexpected = sorted(set(outcome_ids) - set(requested_ids))
            raise ValueError(f"page outcomes must cover requested pages exactly; missing={missing}, unexpected={unexpected}")
        for outcome in self.outcomes:
            outcome_metric_set = {(item.metric, item.aggregation_unit) for item in outcome.metrics}
            if not outcome_metric_set.issubset(planned_metric_set):
                raise ValueError(f"page {outcome.page_id} contains an unplanned metric")
            if self.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED} and outcome_metric_set != planned_metric_set:
                raise ValueError(f"terminal page {outcome.page_id} must report every planned metric")
        if self.execution.kind is ExecutionKind.DIAGNOSTIC and self.track is ParsingTrack.PRODUCTION_DOCUMENT:
            # Diagnostic runs may inspect production-shaped artifacts, but cannot claim a production track.
            raise ValueError("diagnostic execution cannot claim the production_document track")
        if self.execution.kind is ExecutionKind.PRODUCTION:
            required_identities = {
                "parser": self.fingerprints.parser,
                "code_revision": self.fingerprints.code_revision,
                "parser_config_sha256": self.fingerprints.parser_config_sha256,
                "document_schema": self.fingerprints.document_schema,
                "normalizer": self.fingerprints.normalizer,
            }
            unknown = [name for name, identity in required_identities.items() if identity.value is None]
            if unknown:
                raise ValueError(f"production execution requires known identities: {', '.join(unknown)}")
        if self.track is ParsingTrack.DOWNSTREAM_PROJECTION:
            if self.execution.kind is not ExecutionKind.PRODUCTION:
                raise ValueError("downstream_projection requires production execution identity")
            if self.fingerprints.chunker.value is None:
                raise ValueError("downstream_projection requires a known chunker identity")
        return self


def metric_denominators(manifest: ParsingEvaluationManifest) -> dict[str, dict[str, Any]]:
    """Summarize metric support without dropping failed or not-applicable rows."""

    grouped: dict[tuple[str, AggregationUnit], list[MetricOutcome]] = {}
    for page in manifest.outcomes:
        for result in page.metrics:
            grouped.setdefault((result.metric, result.aggregation_unit), []).append(result)
    summary: dict[str, dict[str, Any]] = {}
    for (name, unit), results in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1].value)):
        counts = Counter(result.status.value for result in results)
        key = f"{name}:{unit.value}"
        summary[key] = {
            "requested_outcomes": len(results),
            "scored_denominator": counts[MetricStatus.SUCCEEDED.value],
            "status_counts": dict(sorted(counts.items())),
            "support_ids": sorted(result.support_id for result in results if result.support_id is not None),
        }
    return summary


__all__ = [
    "AggregationUnit",
    "ExecutionKind",
    "Exposure",
    "IdentityValue",
    "InputMode",
    "MetricOutcome",
    "MetricSpec",
    "MetricStatus",
    "PARSING_EVALUATION_PROTOCOL",
    "PageOutcome",
    "PageStatus",
    "ParsingBudget",
    "ParsingEvaluationManifest",
    "ParsingExecutionIdentity",
    "ParsingFingerprints",
    "ParsingTrack",
    "RequestedPage",
    "RunStatus",
    "metric_denominators",
]
