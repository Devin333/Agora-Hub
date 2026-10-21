"""Deterministic, fail-closed acceptance for paired parsing evaluation manifests."""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, model_validator

from backend.research.rag.evaluation.document_parsing_protocol import (
    AggregationUnit,
    CacheState,
    ExecutionKind,
    Exposure,
    MetricStatus,
    PageStatus,
    ParsingEvaluationManifest,
    ParsingTrack,
    RunStatus,
)

from .document_parsing_protocol import ProtocolModel


ACCEPTANCE_PROFILE_SCHEMA = "research-document-parsing-acceptance-profile/v1"


class MetricDirection(StrEnum):
    HIGHER_IS_BETTER = "higher_is_better"
    LOWER_IS_BETTER = "lower_is_better"


class AcceptanceMetricRule(ProtocolModel):
    metric: str = Field(min_length=1)
    aggregation_unit: AggregationUnit
    direction: MetricDirection
    improvement_mode: Literal["absolute", "relative"] = "absolute"
    minimum_improvement: float | None = None
    maximum_regression: float = 0.0
    minimum_documents: int = Field(ge=2)
    confidence_level: float = Field(gt=0.5, lt=1.0)
    bootstrap_replicates: int = Field(ge=1000, le=100_000)

    @model_validator(mode="after")
    def _thresholds_are_non_negative(self):
        if (self.minimum_improvement is not None and self.minimum_improvement < 0) or self.maximum_regression < 0:
            raise ValueError("acceptance deltas must be non-negative")
        if self.minimum_improvement is not None and self.maximum_regression != 0:
            raise ValueError("specify minimum_improvement or maximum_regression, not both")
        return self


class PerformanceAcceptanceRule(ProtocolModel):
    """Preregistered candidate/baseline ratios over page wall-clock seconds."""

    max_median_elapsed_ratio: float = Field(gt=0, allow_inf_nan=False)
    max_p95_elapsed_ratio: float = Field(gt=0, allow_inf_nan=False)


class ParsingAcceptanceProfile(ProtocolModel):
    schema_version: Literal["research-document-parsing-acceptance-profile/v1"] = ACCEPTANCE_PROFILE_SCHEMA
    profile_id: str = Field(min_length=1)
    track: ParsingTrack
    require_frozen_heldout: bool = True
    require_production_execution: bool = True
    require_complete_outcomes: bool = True
    metrics: tuple[AcceptanceMetricRule, ...] = Field(min_length=1)
    performance: PerformanceAcceptanceRule | None = None

    @model_validator(mode="after")
    def _unique_metrics(self):
        keys = [(item.metric, item.aggregation_unit) for item in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("acceptance metric rules must be unique")
        return self


def acceptance_profile_fingerprint(profile: ParsingAcceptanceProfile) -> str:
    canonical = json.dumps(
        profile.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def write_acceptance_profile(profile: ParsingAcceptanceProfile, path: str) -> None:
    """Write an immutable profile; conflicting replacement is rejected."""
    target = Path(path)
    payload = {
        "profile": profile.model_dump(mode="json"),
        "profile_sha256": acceptance_profile_fingerprint(profile),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
    except FileExistsError:
        if target.read_text(encoding="utf-8") != text:
            raise FileExistsError("acceptance profile is immutable once registered")


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate acceptance profile JSON key: {key}")
        result[key] = value
    return result


def load_acceptance_profile(path: str) -> ParsingAcceptanceProfile:
    text = Path(path).read_text(encoding="utf-8")
    payload = json.loads(text, object_pairs_hook=_unique_json_object)
    if not isinstance(payload, dict) or set(payload) != {"profile", "profile_sha256"}:
        raise ValueError("acceptance profile envelope is invalid")
    profile = ParsingAcceptanceProfile.model_validate_json(
        json.dumps(payload["profile"], ensure_ascii=False), strict=True
    )
    if payload["profile_sha256"] != acceptance_profile_fingerprint(profile):
        raise ValueError("acceptance profile fingerprint mismatch")
    return profile


def evaluate_document_parsing_acceptance(
    baseline: ParsingEvaluationManifest | None,
    candidate: ParsingEvaluationManifest | None,
    profile: ParsingAcceptanceProfile | None,
) -> dict[str, Any]:
    """Return pass/fail/blocked with actuals, thresholds, and deterministic reasons.

    This function evaluates recorded evidence only. It never runs parsers,
    publishes profiles, or changes runtime configuration.
    """
    if profile is None:
        return _blocked("acceptance_profile_missing")
    if baseline is None or candidate is None:
        return _blocked("baseline_or_candidate_evidence_missing", profile=profile)
    reasons = _validate_runs(baseline, candidate, profile)
    if reasons:
        return _blocked(*reasons, profile=profile)
    if profile.require_complete_outcomes:
        incomplete = {
            "baseline": sum(outcome.status is not PageStatus.SUCCEEDED for outcome in baseline.outcomes),
            "candidate": sum(outcome.status is not PageStatus.SUCCEEDED for outcome in candidate.outcomes),
        }
        if any(incomplete.values()):
            requested = len(candidate.requested_pages)
            return {
                "schema_version": "research-document-parsing-acceptance-result/v1",
                "profile_id": profile.profile_id,
                "status": "fail",
                "reasons": ["requested_page_coverage_below_100_percent"],
                "metrics": [],
                "actual": {
                    "requested_pages": requested,
                    "baseline_non_succeeded_pages": incomplete["baseline"],
                    "candidate_non_succeeded_pages": incomplete["candidate"],
                    "candidate_succeeded_pages": requested - incomplete["candidate"],
                },
                "threshold": {"requested_page_success_ratio": 1.0},
                "quality_gate_decision": "evaluated",
            }

    metric_results = []
    for rule in profile.metrics:
        paired, owners, missing = _paired_values(baseline, candidate, rule.metric, rule.aggregation_unit)
        if missing:
            return _blocked(
                f"metric_outcomes_incomplete:{rule.metric}:{rule.aggregation_unit.value}",
                profile=profile,
                metrics=metric_results,
                details={"missing_or_invalid_supports": missing},
            )
        document_deltas, document_groups, invalid_baselines = _per_document_deltas(
            paired, owners, rule.direction, baseline, relative=rule.improvement_mode == "relative"
        )
        if invalid_baselines:
            return _blocked(
                f"relative_improvement_baseline_zero:{rule.metric}",
                profile=profile,
                metrics=metric_results,
                details={"documents": invalid_baselines},
            )
        if len(document_deltas) < rule.minimum_documents:
            return _blocked(
                f"minimum_documents_not_met:{rule.metric}",
                profile=profile,
                metrics=metric_results,
                details={"observed_documents": len(document_deltas), "minimum_documents": rule.minimum_documents},
            )
        grouped_deltas: dict[str, list[float]] = defaultdict(list)
        for document_id, delta in document_deltas.items():
            grouped_deltas[document_groups[document_id]].append(delta)
        if len(grouped_deltas) < 2:
            return _blocked(
                f"minimum_source_groups_not_met:{rule.metric}",
                profile=profile,
                metrics=metric_results,
                details={"observed_source_groups": len(grouped_deltas), "minimum_source_groups": 2},
            )
        mean_delta = sum(document_deltas.values()) / len(document_deltas)
        lower, upper = _cluster_bootstrap_interval(
            list(grouped_deltas.values()),
            confidence_level=rule.confidence_level,
            replicates=rule.bootstrap_replicates,
            seed=_seed(profile.profile_id, rule.metric, rule.aggregation_unit.value),
        )
        required_lower_bound = (
            rule.minimum_improvement
            if rule.minimum_improvement is not None
            else -rule.maximum_regression
        )
        passed = lower >= required_lower_bound
        metric_results.append({
            "metric": rule.metric,
            "aggregation_unit": rule.aggregation_unit.value,
            "status": "pass" if passed else "fail",
            "actual": {
                "paired_support_count": len(paired),
                "document_count": len(document_deltas),
                "source_group_count": len(grouped_deltas),
                "mean_improvement": mean_delta,
                "confidence_interval": [lower, upper],
                "confidence_level": rule.confidence_level,
            },
            "threshold": {
                "improvement_mode": rule.improvement_mode,
                "minimum_improvement": rule.minimum_improvement,
                "maximum_regression": rule.maximum_regression,
                "required_lower_confidence_bound": required_lower_bound,
                "minimum_documents": rule.minimum_documents,
            },
            "reason": None if passed else "confidence_bound_did_not_meet_preregistered_threshold",
        })
    performance_result = _evaluate_performance(baseline, candidate, profile.performance)
    quality_failed = any(item["status"] == "fail" for item in metric_results)
    reasons = [item["reason"] for item in metric_results if item["reason"]]
    if profile.performance is None:
        reasons.append("performance_profile_missing")
    elif performance_result["status"] == "blocked":
        reasons.extend(performance_result["reasons"])
    elif performance_result["status"] == "fail":
        reasons.extend(performance_result["reasons"])
    overall_status = (
        "blocked" if profile.performance is None or performance_result["status"] == "blocked"
        else "fail" if quality_failed or performance_result["status"] == "fail"
        else "pass"
    )
    return {
        "schema_version": "research-document-parsing-acceptance-result/v1",
        "profile_id": profile.profile_id,
        "status": overall_status,
        "reasons": reasons,
        "metrics": metric_results,
        "performance": performance_result,
        "quality_gate_decision": "evaluated" if overall_status != "blocked" else "not_evaluated",
    }


def _evaluate_performance(baseline, candidate, rule):
    coverage = "page_elapsed_only_not_gpu_resource_acceptance"
    if rule is None:
        return {
            "status": "not_evaluated",
            "reasons": ["performance_profile_missing"],
            "coverage": coverage,
        }

    baseline_conditions = baseline.performance_conditions
    candidate_conditions = candidate.performance_conditions
    if baseline_conditions is None or candidate_conditions is None:
        return {"status": "blocked", "reasons": ["performance_conditions_missing"], "coverage": coverage}
    conditions = (baseline_conditions, candidate_conditions)
    if any(item.concurrency is None for item in conditions):
        return {"status": "blocked", "reasons": ["performance_concurrency_unknown"]}
    if any(item.concurrency != manifest.budget.max_concurrency for item, manifest in zip(conditions, (baseline, candidate), strict=True)):
        return {"status": "blocked", "reasons": ["performance_concurrency_does_not_match_run"]}
    if any(item.cache_state is CacheState.UNKNOWN for item in conditions):
        return {"status": "blocked", "reasons": ["performance_cache_state_unknown"]}
    if any(item.timing_scope_version is None or item.timing_scope_version.value is None for item in conditions):
        return {"status": "blocked", "reasons": ["performance_timing_scope_unknown"]}
    if baseline_conditions.concurrency != candidate_conditions.concurrency:
        return {"status": "blocked", "reasons": ["performance_concurrency_mismatch"]}
    if baseline_conditions.cache_state is not candidate_conditions.cache_state:
        return {"status": "blocked", "reasons": ["performance_cache_state_mismatch"]}
    if baseline_conditions.timing_scope_version != candidate_conditions.timing_scope_version:
        return {"status": "blocked", "reasons": ["performance_timing_scope_mismatch"]}
    if baseline.fingerprints.hardware.value is None or candidate.fingerprints.hardware.value is None:
        return {"status": "blocked", "reasons": ["performance_hardware_unknown"]}
    if baseline.fingerprints.hardware != candidate.fingerprints.hardware:
        return {"status": "blocked", "reasons": ["performance_hardware_mismatch"]}

    baseline_elapsed = [outcome.elapsed_seconds for outcome in baseline.outcomes]
    candidate_elapsed = [outcome.elapsed_seconds for outcome in candidate.outcomes]
    if not baseline_elapsed or any(value is None for value in baseline_elapsed + candidate_elapsed):
        return {"status": "blocked", "reasons": ["performance_elapsed_seconds_missing"]}
    baseline_values = [float(value) for value in baseline_elapsed]
    candidate_values = [float(value) for value in candidate_elapsed]
    baseline_median = _median(baseline_values)
    candidate_median = _median(candidate_values)
    baseline_p95 = _nearest_rank_percentile(baseline_values, 0.95)
    candidate_p95 = _nearest_rank_percentile(candidate_values, 0.95)
    if baseline_median == 0:
        return {"status": "blocked", "reasons": ["performance_baseline_median_zero"]}
    if baseline_p95 == 0:
        return {"status": "blocked", "reasons": ["performance_baseline_p95_zero"]}

    actual = {
        "measurement": "PageOutcome.elapsed_seconds; per-page wall-clock seconds",
        "sample_count": len(baseline_values),
        "baseline_median_elapsed_seconds": baseline_median,
        "candidate_median_elapsed_seconds": candidate_median,
        "median_elapsed_ratio": candidate_median / baseline_median,
        "baseline_p95_elapsed_seconds": baseline_p95,
        "candidate_p95_elapsed_seconds": candidate_p95,
        "p95_elapsed_ratio": candidate_p95 / baseline_p95,
        "concurrency": baseline_conditions.concurrency,
        "cache_state": baseline_conditions.cache_state.value,
        "timing_scope_version": baseline_conditions.timing_scope_version.value,
        "hardware": baseline.fingerprints.hardware.value,
    }
    violations = []
    if actual["median_elapsed_ratio"] > rule.max_median_elapsed_ratio:
        violations.append("median_elapsed_ratio_exceeded")
    if actual["p95_elapsed_ratio"] > rule.max_p95_elapsed_ratio:
        violations.append("p95_elapsed_ratio_exceeded")
    return {
        "status": "fail" if violations else "pass",
        "reasons": violations,
        "coverage": coverage,
        "actual": actual,
        "threshold": {
            "max_median_elapsed_ratio": rule.max_median_elapsed_ratio,
            "max_p95_elapsed_ratio": rule.max_p95_elapsed_ratio,
            "scope": "page wall-clock elapsed_seconds; excludes any unrecorded setup or aggregate runtime",
        },
    }


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _nearest_rank_percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _validate_runs(baseline, candidate, profile) -> list[str]:
    reasons: list[str] = []
    for name, manifest in (("baseline", baseline), ("candidate", candidate)):
        if manifest.track is not profile.track:
            reasons.append(f"{name}_track_mismatch")
        if manifest.status is not RunStatus.COMPLETED:
            reasons.append(f"{name}_run_not_completed")
        if profile.require_production_execution and manifest.execution.kind is not ExecutionKind.PRODUCTION:
            reasons.append(f"{name}_execution_not_production")
        if profile.require_frozen_heldout:
            if any(page.exposure is not Exposure.FROZEN_HELDOUT for page in manifest.requested_pages):
                reasons.append(f"{name}_not_frozen_heldout")
            if any(page.source_document_id is None or page.source_group_id is None for page in manifest.requested_pages):
                reasons.append(f"{name}_document_identity_unknown")
    if reasons:
        return reasons
    base_pages = {page.page_id: page for page in baseline.requested_pages}
    candidate_pages = {page.page_id: page for page in candidate.requested_pages}
    if base_pages != candidate_pages:
        reasons.append("requested_input_identity_mismatch")
    comparable_fields = (
        "dataset",
        "annotations",
        "evaluator_commit",
        "evaluator_config_sha256",
        "exporter",
        "hardware",
    )
    for field in comparable_fields:
        baseline_identity = getattr(baseline.fingerprints, field)
        candidate_identity = getattr(candidate.fingerprints, field)
        if baseline_identity.value is None or candidate_identity.value is None:
            reasons.append(f"paired_fingerprint_unknown:{field}")
        elif baseline_identity != candidate_identity:
            reasons.append(f"paired_fingerprint_mismatch:{field}")
    return reasons


def _paired_values(baseline, candidate, metric, unit):
    def collect(manifest):
        rows_by_support: dict[str, Any] = {}
        owners: dict[str, str] = {}
        bad: set[str] = set()
        for outcome in manifest.outcomes:
            rows = [row for row in outcome.metrics if row.metric == metric and row.aggregation_unit is unit]
            if unit is AggregationUnit.PAGE and len(rows) != 1:
                bad.add(outcome.page_id)
                continue
            if unit is not AggregationUnit.PAGE and not rows:
                bad.add(outcome.page_id)
                continue
            for row in rows:
                if unit is AggregationUnit.PAGE:
                    key = outcome.page_id
                else:
                    if row.support_id is None:
                        bad.add(outcome.page_id)
                        continue
                    # Object/sample identifiers are scoped to their source page.
                    key = json.dumps((outcome.page_id, row.support_id), ensure_ascii=False, separators=(",", ":"))
                if key in owners and owners[key] != outcome.page_id:
                    bad.add(str(key))
                    continue
                owners[key] = outcome.page_id
                rows_by_support[key] = row
                if row.status not in {MetricStatus.SUCCEEDED, MetricStatus.NOT_APPLICABLE}:
                    bad.add(str(key))
        return rows_by_support, owners, bad

    base, base_owners, base_bad = collect(baseline)
    cand, cand_owners, cand_bad = collect(candidate)
    supports = set(base) | set(cand)
    bad = base_bad | cand_bad | (set(base) ^ set(cand))
    if base_owners != cand_owners:
        bad.update(
            support for support in supports
            if base_owners.get(support) != cand_owners.get(support)
        )
    paired: dict[str, tuple[float, float]] = {}
    owners: dict[str, str] = {}
    for support in supports - bad:
        before = base[support]
        after = cand[support]
        if before.status is MetricStatus.SUCCEEDED and after.status is MetricStatus.SUCCEEDED:
            paired[support] = (before.value, after.value)
            owners[support] = base_owners[support]
        elif before.status is MetricStatus.NOT_APPLICABLE and after.status is MetricStatus.NOT_APPLICABLE:
            # The protocol has no gold-applicability evidence field. Even identical
            # free-text reasons cannot prove that the gold lacks this category.
            bad.add(f"not_applicable_semantics_unverifiable:{support}")
        else:
            bad.add(str(support))
    if bad:
        return {}, {}, sorted(bad)
    page_ids = set(base_owners.values())
    if not page_ids.issubset({page.page_id for page in baseline.requested_pages}):
        return {}, {}, ["metric_support_page_identity_unknown"]
    return paired, owners, []


def _per_document_deltas(paired, owners, direction, baseline, *, relative: bool = False):
    page_to_source = {
        page.page_id: (page.source_document_id, page.source_group_id)
        for page in baseline.requested_pages
    }
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    document_groups: dict[str, str] = {}
    for support, (before, after) in paired.items():
        page_id = owners[support]
        document_id, source_group_id = page_to_source.get(page_id, (None, None))
        if document_id is None or source_group_id is None:
            raise ValueError(f"metric support cannot be mapped to a source document group: {support}")
        if document_id in document_groups and document_groups[document_id] != source_group_id:
            raise ValueError(f"source document maps to multiple source groups: {document_id}")
        document_groups[document_id] = source_group_id
        grouped[document_id].append((before, after))
    deltas: dict[str, float] = {}
    invalid_baselines: list[str] = []
    for document_id, pairs in grouped.items():
        before_mean = sum(before for before, _ in pairs) / len(pairs)
        after_mean = sum(after for _, after in pairs) / len(pairs)
        if relative:
            if before_mean == 0:
                invalid_baselines.append(document_id)
                continue
            delta = (after_mean - before_mean) / abs(before_mean)
            if direction is MetricDirection.LOWER_IS_BETTER:
                delta = -delta
        else:
            delta = (after_mean - before_mean) if direction is MetricDirection.HIGHER_IS_BETTER else (before_mean - after_mean)
        deltas[document_id] = delta
    return deltas, document_groups, invalid_baselines


def _cluster_bootstrap_interval(groups: list[list[float]], *, confidence_level: float, replicates: int, seed: int):
    rng = random.Random(seed)
    size = len(groups)
    means: list[float] = []
    for _ in range(replicates):
        sampled_groups = [groups[rng.randrange(size)] for _ in range(size)]
        sampled_values = [value for group in sampled_groups for value in group]
        means.append(sum(sampled_values) / len(sampled_values))
    means.sort()
    alpha = (1.0 - confidence_level) / 2.0
    lower = means[min(replicates - 1, int(alpha * replicates))]
    upper = means[min(replicates - 1, int((1.0 - alpha) * replicates))]
    return lower, upper


def _seed(*parts: str) -> int:
    return int.from_bytes(hashlib.sha256("|".join(parts).encode("utf-8")).digest()[:8], "big")


def _blocked(*reasons: str, profile=None, metrics=None, details=None):
    resolved_reasons = list(reasons)
    performance = None
    if profile is not None and profile.performance is None:
        if "performance_profile_missing" not in resolved_reasons:
            resolved_reasons.append("performance_profile_missing")
        performance = {
            "status": "not_evaluated",
            "reasons": ["performance_profile_missing"],
            "coverage": "page_elapsed_only_not_gpu_resource_acceptance",
        }
    return {
        "schema_version": "research-document-parsing-acceptance-result/v1",
        "profile_id": profile.profile_id if profile is not None else None,
        "status": "blocked",
        "reasons": resolved_reasons,
        "metrics": list(metrics or []),
        "performance": performance,
        "details": dict(details or {}),
        "quality_gate_decision": "not_evaluated",
    }


__all__ = [
    "ACCEPTANCE_PROFILE_SCHEMA",
    "AcceptanceMetricRule",
    "MetricDirection",
    "PerformanceAcceptanceRule",
    "ParsingAcceptanceProfile",
    "acceptance_profile_fingerprint",
    "evaluate_document_parsing_acceptance",
    "load_acceptance_profile",
    "write_acceptance_profile",
]
