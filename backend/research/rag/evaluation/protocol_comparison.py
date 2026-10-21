"""Pure typed-config qualification and paired statistics over saved scores.

No retrieval, model, judge, runtime, persistence, or publication is invoked here.
The returned comparison qualification is not a quality-gate pass decision.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

from pydantic import Field, field_validator, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ImmutableVersionStr,
    NonEmptyStr,
    ProtocolModel,
    content_digest,
)
from backend.research.rag.evaluation.protocol_dataset import DatasetManifest
from backend.research.rag.evaluation.protocol_execution import (
    ComparisonIdentity,
    ComponentClassification,
    ComponentRole,
    EvaluationOptions,
    EvaluationTrack,
    ExecutionProfile,
    ExperimentManifest,
    VariantConfiguration,
)
from backend.research.rag.evaluation.protocol_scoring import (
    EvaluationScore, MetricDirection, MetricGranularity, MetricRegistry, ScoreStatus,
)

BOOTSTRAP_METHOD_VERSION = "paper-family-cluster-percentile/v1"
# Paths are exact typed fields, not names, wildcards, arbitrary metadata, or
# permissions to change models, budgets, context policy, sources or initial state.
_VARIABLE_FIELDS = frozenset(
    {
        "chunker_config_hash",
        "embedding_config_hash",
        "retriever_config_hash",
        "reranker_config_hash",
    }
)
_ALLOWED_PATHS = frozenset(f"variant.retrieval.{name}" for name in _VARIABLE_FIELDS)
_INDEX_INPUTS = frozenset({"chunker_config_hash", "embedding_config_hash"})
_ROLE_FIELDS = {
    ComponentRole.PARSER: "parser_config_hash",
    ComponentRole.CHUNKER: "chunker_config_hash",
    ComponentRole.EMBEDDING: "embedding_config_hash",
    ComponentRole.INDEX: "index_config_hash",
    ComponentRole.RETRIEVER: "retriever_config_hash",
    ComponentRole.RERANKER: "reranker_config_hash",
    ComponentRole.GENERATOR: "generator_config_hash",
}


class PairedComparisonConfig(ProtocolModel):
    """All evidence requirements are explicit, not industry-default thresholds."""
    metric_id: NonEmptyStr
    metric_version: ImmutableVersionStr
    scoring_revision: ImmutableVersionStr
    scope: NonEmptyStr
    minimum_cases: int = Field(ge=1, strict=True)
    minimum_groups: int = Field(ge=1, strict=True)
    minimum_repetitions: int = Field(ge=1, strict=True)
    require_complete_pairs: bool
    bootstrap_resamples: int = Field(ge=1, strict=True)
    confidence_level: float
    seed: int = Field(strict=True)
    method_version: Literal[BOOTSTRAP_METHOD_VERSION]

    @field_validator("confidence_level", mode="before")
    @classmethod
    def _confidence(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (float, int)):
            raise ValueError("confidence_level must be a finite number in (0, 1)")
        if not math.isfinite(value) or not 0 < value < 1:
            raise ValueError("confidence_level must be a finite number in (0, 1)")
        return float(value)


class ComparisonQualification(ProtocolModel):
    status: Literal["comparable", "incomparable", "inconclusive", "diagnostic_only"]
    reasons: tuple[NonEmptyStr, ...]
    ranking_eligible: bool

    @model_validator(mode="after")
    def _ranking_boundary(self) -> "ComparisonQualification":
        if self.ranking_eligible and self.status != "comparable":
            raise ValueError("only a comparable result may be ranking eligible")
        return self


class ConfigurationComparison(ProtocolModel):
    comparison_qualification: ComparisonQualification
    baseline_identity: ComparisonIdentity | None
    candidate_identity: ComparisonIdentity | None
    changed_paths: tuple[str, ...]
    derived_paths: tuple[str, ...]


class ConfidenceInterval(ProtocolModel):
    lower: float
    upper: float
    confidence_level: float

    @model_validator(mode="after")
    def _ordered_finite_bounds(self) -> "ConfidenceInterval":
        if not all(math.isfinite(value) for value in (self.lower, self.upper, self.confidence_level)):
            raise ValueError("confidence interval values must be finite")
        if self.lower > self.upper:
            raise ValueError("confidence interval lower bound must not exceed upper bound")
        return self


class PairedCaseDifference(ProtocolModel):
    case_id: NonEmptyStr
    group_id: NonEmptyStr
    planned_repetitions: tuple[int, ...]
    paired_repetitions: tuple[int, ...]
    baseline_mean: float | None
    candidate_mean: float | None
    candidate_minus_baseline: float | None
    directed_improvement: float | None
    confidence: ConfidenceInterval | None
    missing_repetitions: tuple[int, ...]
    reasons: tuple[str, ...]


class PairedComparisonResult(ProtocolModel):
    """Derived report, not a second run/score record or an execution authority."""
    baseline_name: NonEmptyStr
    candidate_name: NonEmptyStr
    configuration: ConfigurationComparison
    comparison_qualification: ComparisonQualification
    config: PairedComparisonConfig
    direction: MetricDirection
    planned_cases: int = Field(ge=0, strict=True)
    planned_groups: int = Field(ge=0, strict=True)
    planned_repetition_pairs: int = Field(ge=0, strict=True)
    valid_pairs: int = Field(ge=0, strict=True)
    missing_pairs: int = Field(ge=0, strict=True)
    valid_repetition_pairs: int = Field(ge=0, strict=True)
    missing_repetition_pairs: int = Field(ge=0, strict=True)
    independent_groups: int = Field(ge=0, strict=True)
    case_differences: tuple[PairedCaseDifference, ...]
    candidate_minus_baseline: float | None
    directed_improvement: float | None
    confidence: ConfidenceInterval | None
    directed_confidence: ConfidenceInterval | None
    bootstrap_completed: int = Field(ge=0, strict=True)
    conclusion: Literal["improvement", "regression", "no_clear_difference", "inconclusive", "diagnostic_only"]

    @model_validator(mode="after")
    def _count_integrity(self) -> "PairedComparisonResult":
        if self.valid_pairs + self.missing_pairs != self.planned_cases:
            raise ValueError("case pair counts must match planned_cases")
        if self.valid_repetition_pairs + self.missing_repetition_pairs != self.planned_repetition_pairs:
            raise ValueError("repetition pair counts must match planned_repetition_pairs")
        if len(self.case_differences) != self.planned_cases:
            raise ValueError("case_differences must retain every planned case")
        if self.bootstrap_completed not in {0, self.config.bootstrap_resamples}:
            raise ValueError("bootstrap must complete the frozen plan or retain zero completed draws")
        if self.conclusion in {"improvement", "regression"} and not self.comparison_qualification.ranking_eligible:
            raise ValueError("directional conclusions require ranking-eligible comparison evidence")
        return self

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")

    def report(self) -> str:
        return (
            f"Saved-score A/B ({self.baseline_name} -> {self.candidate_name})\n"
            f"comparison_qualification={self.comparison_qualification.status} "
            f"valid_pairs={self.valid_pairs}/{self.planned_cases} "
            f"paper_groups={self.independent_groups}/{self.planned_groups}\n"
            f"candidate-baseline={self.candidate_minus_baseline} confidence={self.confidence} "
            f"conclusion={self.conclusion}"
        )


def _revalidate(model: ProtocolModel):
    # model_validate(instance) trusts frozen instances and model_copy bypasses
    # validation. JSON reconstruction verifies all nested digests and typed links.
    return type(model).model_validate_json(model.model_dump_json())


def _identity(options: EvaluationOptions) -> ComparisonIdentity:
    return next(item for item in options.experiment_manifest.comparison_identities
                if item.variant_fingerprint == options.selected_variant_fingerprint
                and item.track == options.selected_variant.track
                and item.execution_profile == options.execution_profile)


def qualify_comparison(
    baseline: EvaluationOptions, candidate: EvaluationOptions,
) -> ConfigurationComparison:
    """Recompute typed fingerprints and enforce the frozen declared experiment."""
    try:
        baseline = _revalidate(baseline)
        candidate = _revalidate(candidate)
    except (ValueError, TypeError) as exc:
        return ConfigurationComparison(
            comparison_qualification=ComparisonQualification(
                status="incomparable", reasons=(f"invalid typed configuration: {exc}",),
                ranking_eligible=False),
            baseline_identity=None, candidate_identity=None, changed_paths=(), derived_paths=())
    left: ExperimentManifest = baseline.experiment_manifest
    right: ExperimentManifest = candidate.experiment_manifest
    a: VariantConfiguration = baseline.selected_variant
    b: VariantConfiguration = candidate.selected_variant
    ia, ib = _identity(baseline), _identity(candidate)
    reasons: list[str] = []
    changed: list[str] = []
    derived: list[str] = []
    if left.manifest_digest != right.manifest_digest:
        reasons.append("comparison requires the same predeclared experiment manifest")
    declared = frozenset(left.allowed_differences)
    if declared - _ALLOWED_PATHS:
        reasons.append(f"forbidden or unknown allowed_differences: {sorted(declared - _ALLOWED_PATHS)}")
    for path in declared:
        if a.retrieval is None or b.retrieval is None:
            reasons.append(f"declared typed path does not apply to this track: {path}")
    # Variant fingerprint is validated against real config, but is the one field
    # that is expected to differ; names never confer authorization.
    for name in ComparisonIdentity.model_fields:
        if name not in {"variant_fingerprint", "comparison_fingerprint"}:
            if getattr(ia, name) != getattr(ib, name):
                reasons.append(f"fixed comparison identity changed: {name}")
    for name in ("execution_profile", "execution_path", "source_access_grants", "namespaces",
                 "initial_state", "budget_limits", "code_identity", "environment"):
        if getattr(baseline, name) != getattr(candidate, name):
            reasons.append(f"fixed configuration changed: {name}")
    for name in ("track", "generation", "harness"):
        if getattr(a, name) != getattr(b, name):
            reasons.append(f"fixed variant configuration changed: {name}")
    retrieval_changes: set[str] = set()
    if (a.retrieval is None) != (b.retrieval is None):
        reasons.append("retrieval applicability changed")
    elif a.retrieval is not None and b.retrieval is not None:
        for name in type(a.retrieval).model_fields:
            if getattr(a.retrieval, name) != getattr(b.retrieval, name):
                retrieval_changes.add(name)
                path = f"variant.retrieval.{name}"
                if name == "reranker_not_applicable_reason":
                    # The reason is descriptive; its nullability is tied to the
                    # typed reranker hash by execution contract validation.
                    continue
                if name == "index_config_hash":
                    derived.append(path)
                else:
                    changed.append(path)
                    if path not in declared:
                        reasons.append(f"undeclared typed change: {path}")
    index_build_changed = bool(retrieval_changes & _INDEX_INPUTS)
    if "index_config_hash" in retrieval_changes and not index_build_changed:
        reasons.append("index config changed without declared parser/chunking/embedding input change")
    index_changes = []
    for name in type(baseline.corpus).model_fields:
        if getattr(baseline.corpus, name) != getattr(candidate.corpus, name):
            path = f"corpus.{name}"
            if name in {"index_snapshot_id", "index_content_sha256"}:
                index_changes.append(name)
                derived.append(path)
            elif name != "evidence_references":
                reasons.append(f"fixed corpus/source configuration changed: {name}")
    if index_changes and not index_build_changed:
        reasons.append("index snapshot changed without an index-building typed change")
    if index_build_changed:
        if "index_config_hash" not in retrieval_changes:
            reasons.append("index-building changes require a changed derived index config")
        if set(index_changes) != {"index_snapshot_id", "index_content_sha256"}:
            reasons.append("index-building changes require a changed index snapshot and content hash")
    reasons.extend(_component_checks(baseline, candidate, retrieval_changes))
    if not retrieval_changes and a.track is not EvaluationTrack.ORACLE_GENERATION:
        reasons.append("variant names are display-only; typed configurations contain no actual change")
    qualification = ComparisonQualification(
        status="incomparable" if reasons else "comparable",
        reasons=tuple(dict.fromkeys(reasons)), ranking_eligible=not reasons
            and a.track is not EvaluationTrack.ORACLE_GENERATION)
    if not reasons and a.track is EvaluationTrack.ORACLE_GENERATION:
        qualification = ComparisonQualification(status="diagnostic_only",
            reasons=("oracle_generation is diagnostic; production ranking is forbidden",), ranking_eligible=False)
    return ConfigurationComparison(comparison_qualification=qualification,
        baseline_identity=ia, candidate_identity=ib, changed_paths=tuple(sorted(changed)),
        derived_paths=tuple(sorted(derived)))


def _component_checks(a: EvaluationOptions, b: EvaluationOptions, changes: set[str]) -> list[str]:
    reasons: list[str] = []
    left, right = ({c.role: c for c in options.components} for options in (a, b))
    if left.keys() != right.keys():
        reasons.append("observed component role set changed")
    for role, field in _ROLE_FIELDS.items():
        factor_a = a.selected_variant.generation if role is ComponentRole.GENERATOR else a.selected_variant.retrieval
        factor_b = b.selected_variant.generation if role is ComponentRole.GENERATOR else b.selected_variant.retrieval
        applicable = (factor_a is not None and factor_b is not None
                      and getattr(factor_a, field, None) is not None
                      and getattr(factor_b, field, None) is not None)
        if applicable and (role not in left or role not in right):
            reasons.append(f"missing observed component proof for typed factor: {role.value}")
    for role in left.keys() | right.keys():
        ca, cb = left.get(role), right.get(role)
        if ca is None or cb is None:
            continue
        field = _ROLE_FIELDS.get(role)
        factor_a = a.selected_variant.generation if role is ComponentRole.GENERATOR else a.selected_variant.retrieval
        factor_b = b.selected_variant.generation if role is ComponentRole.GENERATOR else b.selected_variant.retrieval
        if field and factor_a is not None and factor_b is not None:
            if ca.config_hash != getattr(factor_a, field) or cb.config_hash != getattr(factor_b, field):
                reasons.append(f"observed component config does not match typed factors: {role.value}")
        mutable = field in changes
        if (a.execution_profile is ExecutionProfile.PRODUCTION and mutable
                and (ca.classification is not ComponentClassification.PRODUCTION
                     or cb.classification is not ComponentClassification.PRODUCTION)):
            reasons.append(f"production comparison variable lacks production component proof: {role.value}")
        # Runtime proof digests and evidence refs are per-run observations. Their
        # presence is validated, not their equality. Classification/code remain
        # fixed even for an authorized variable.
        fixed = () if mutable else ("classification", "fixture_kind", "code_revision",
            "adapter", "provider", "model", "revision", "config_hash", "identity_unavailable_reason")
        for name in fixed:
            if getattr(ca, name) != getattr(cb, name):
                reasons.append(f"unrelated observed component change: {role.value}.{name}")
    return reasons


@dataclass(frozen=True)
class _RepetitionScore:
    value: float | None
    reasons: tuple[str, ...]


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values)


def _interval(values: Sequence[float], level: float) -> ConfidenceInterval:
    ordered = sorted(values)
    def percentile(p: float) -> float:
        location = p * (len(ordered) - 1)
        lo, hi = math.floor(location), math.ceil(location)
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (location - lo)
    alpha = (1 - level) / 2
    return ConfidenceInterval(lower=percentile(alpha), upper=percentile(1 - alpha), confidence_level=level)


def _direct(value: float, direction: MetricDirection) -> float | None:
    if direction is MetricDirection.NEUTRAL:
        return None
    return -value if direction is MetricDirection.LOWER_IS_BETTER else value


def _planned(options: EvaluationOptions) -> dict[str, tuple[int, ...]]:
    repetitions: dict[str, list[int]] = defaultdict(list)
    for unit in options.experiment_manifest.planned_units:
        if (
            unit.variant_fingerprint == options.selected_variant_fingerprint
            and unit.track == options.selected_variant.track
        ):
            repetitions[unit.case_id].append(unit.repetition)
    return {case: tuple(sorted(values)) for case, values in repetitions.items()}


def _scores(
    observations: Iterable[EvaluationScore], options: EvaluationOptions,
    config: PairedComparisonConfig, registry: MetricRegistry,
) -> dict[tuple[str, int], _RepetitionScore]:
    plan = _planned(options)
    manifest = options.experiment_manifest
    grouped: dict[tuple[str, int], list[EvaluationScore]] = defaultdict(list)
    run_ids: set[str] = set()
    for observation in observations:
        score = _revalidate(observation)
        if score.metric_id != config.metric_id or score.scope != config.scope:
            continue
        if score.version != config.metric_version or score.scoring_revision != config.scoring_revision:
            raise ValueError("incomparable score metric version or scoring revision; rescore both variants explicitly")
        registry.validate_score(score)
        if (score.dataset_id, score.dataset_version, score.dataset_digest) != (
                manifest.dataset_id, manifest.dataset_version, manifest.dataset_digest):
            raise ValueError("saved score dataset identity differs from experiment manifest")
        if (score.track, score.execution_profile, score.variant_fingerprint) != (
                options.selected_variant.track, options.execution_profile, options.selected_variant_fingerprint):
            raise ValueError("saved score track/profile/variant identity differs from selected configuration")
        if score.case_id not in plan or score.repetition not in plan[score.case_id]:
            raise ValueError("saved score case/repetition is outside the frozen plan")
        run_ids.add(score.run_id)
        grouped[score.case_id, score.repetition].append(score)
    if len(run_ids) > 1:
        raise ValueError("multiple saved runs cannot silently expand a variant's repetition plan")
    result: dict[tuple[str, int], _RepetitionScore] = {}
    for key, scores in grouped.items():
        # Exact duplicate rows and multiple attempts cannot enlarge n. Conflicting
        # attempt scores require explicit selection upstream; do not cherry-pick
        # the successful retry or average arbitrary retry artifacts.
        unique = {content_digest(score): score for score in scores}.values()
        values = {score.value for score in unique if score.status is ScoreStatus.OK}
        unknown = tuple(sorted({f"{score.status.value}: {score.reason}" for score in unique
                                if score.status is not ScoreStatus.OK}))
        if unknown or len(values) != 1:
            result[key] = _RepetitionScore(
                None,
                unknown or ("conflicting raw-attempt scores require explicit resolution",),
            )
        else:
            result[key] = _RepetitionScore(next(iter(values)), ())
    return result


def compare_saved_scores(
    *, baseline_options: EvaluationOptions, candidate_options: EvaluationOptions,
    baseline_scores: Iterable[EvaluationScore], candidate_scores: Iterable[EvaluationScore],
    dataset_manifest: DatasetManifest, registry: MetricRegistry, config: PairedComparisonConfig,
    baseline_name: str = "baseline", candidate_name: str = "candidate",
) -> PairedComparisonResult:
    """Same-case/same-repetition pairing, then independent paper-family bootstrap."""
    config = _revalidate(config)
    definition = registry.definition(config.metric_id, config.metric_version)
    if definition.granularity is MetricGranularity.RUN:
        raise ValueError("run-granularity metrics cannot be used as same-case paired scores")
    configuration = qualify_comparison(baseline_options, candidate_options)
    if configuration.baseline_identity is None or configuration.candidate_identity is None:
        # Fingerprints cannot be trusted; never derive counts from forged records.
        return _result(baseline_name, candidate_name, configuration, config, definition.direction,
                       (), {}, (), ("invalid typed configuration",), None, (), 0)
    baseline, candidate = _revalidate(baseline_options), _revalidate(candidate_options)
    dataset = _revalidate(dataset_manifest)
    plan, other_plan = _planned(baseline), _planned(candidate)
    groups = {item.case_id: item.paper_group_id for item in dataset.case_manifest}
    reasons = (
        list(configuration.comparison_qualification.reasons)
        if configuration.comparison_qualification.status == "incomparable"
        else []
    )
    if (dataset.dataset_id, dataset.dataset_version, dataset.dataset_digest) != (
            baseline.experiment_manifest.dataset_id, baseline.experiment_manifest.dataset_version,
            baseline.experiment_manifest.dataset_digest):
        reasons.append("dataset manifest identity differs from frozen experiment")
    if dataset.corpus_snapshot_digest != baseline.corpus.corpus_content_sha256:
        reasons.append("dataset corpus identity differs from configured source corpus")
    if set(plan) != set(other_plan) or plan != other_plan:
        reasons.append("same-case paired repetition plans differ")
    if set(plan) != {item.case_id for item in baseline.experiment_manifest.case_scopes}:
        reasons.append("selected variant does not plan every frozen case scope")
    group_sources = {group.paper_group_id: {(s.paper_id, s.document_version, s.source_sha256)
                    for s in group.sources} for group in dataset.split_manifest.groups}
    for case_scope in baseline.experiment_manifest.case_scopes:
        group = groups.get(case_scope.case_id)
        if group is None or not {(s.paper_id, s.document_version, s.source_sha256)
                                 for s in case_scope.scope.sources}.issubset(group_sources.get(group, set())):
            reasons.append(f"case scope is not bound to its manifest paper family: {case_scope.case_id}")
    a_scores, b_scores = {}, {}
    try:
        a_scores = _scores(baseline_scores, baseline, config, registry)
        b_scores = _scores(candidate_scores, candidate, config, registry)
    except (ValueError, TypeError) as exc:
        reasons.append(str(exc))
    if reasons:
        configuration = configuration.model_copy(update={"comparison_qualification": ComparisonQualification(
            status="incomparable", reasons=tuple(dict.fromkeys(reasons)), ranking_eligible=False)})
    pairs: list[PairedCaseDifference] = []
    for case_id, repetitions in sorted(plan.items()):
        paired: list[int] = []
        values_a: list[float] = []
        values_b: list[float] = []
        missing_reasons: list[str] = []
        for repetition in repetitions:
            va = a_scores.get((case_id, repetition), _RepetitionScore(None, ("missing score/stage",)))
            vb = b_scores.get((case_id, repetition), _RepetitionScore(None, ("missing score/stage",)))
            if va.value is None or vb.value is None or reasons:
                missing_reasons.extend(f"baseline repetition {repetition}: {r}" for r in va.reasons)
                missing_reasons.extend(f"candidate repetition {repetition}: {r}" for r in vb.reasons)
                continue
            paired.append(repetition)
            values_a.append(va.value)
            values_b.append(vb.value)
        deltas = [b - a for a, b in zip(values_a, values_b, strict=True)]
        valid = len(paired) >= config.minimum_repetitions and (
            not config.require_complete_pairs or len(paired) == len(repetitions)
        )
        delta = _mean(deltas) if valid else None
        confidence = None
        if valid:
            rng = random.Random(content_digest((config.seed, config.method_version, case_id)))
            confidence = _interval([_mean(rng.choices(deltas, k=len(deltas)))
                                    for _ in range(config.bootstrap_resamples)], config.confidence_level)
        if not valid:
            missing_reasons.append("insufficient configured paired repetitions or incomplete mandatory scores")
        pairs.append(PairedCaseDifference(case_id=case_id, group_id=groups.get(case_id, "unknown"),
            planned_repetitions=repetitions, paired_repetitions=tuple(paired),
            baseline_mean=_mean(values_a) if valid else None, candidate_mean=_mean(values_b) if valid else None,
            candidate_minus_baseline=delta,
            directed_improvement=(
                _direct(delta, definition.direction) if delta is not None else None
            ),
            confidence=confidence, missing_repetitions=tuple(r for r in repetitions if r not in paired),
            reasons=tuple(dict.fromkeys(missing_reasons))))
    valid_pairs = [pair for pair in pairs if pair.candidate_minus_baseline is not None]
    clusters: dict[str, list[float]] = defaultdict(list)
    for pair in valid_pairs:
        clusters[pair.group_id].append(pair.candidate_minus_baseline)
    insufficiency: list[str] = []
    if len(valid_pairs) < config.minimum_cases:
        insufficiency.append("valid paired cases below explicit minimum_cases")
    if len(clusters) < config.minimum_groups:
        insufficiency.append("independent paper families below explicit minimum_groups")
    if config.require_complete_pairs and len(valid_pairs) != len(plan):
        insufficiency.append("mandatory paired scores/stages are missing or unknown")
    draws: list[float] = []
    if clusters and not reasons and len(clusters) >= config.minimum_groups:
        rng = random.Random(content_digest((config.seed, config.method_version)))
        ids = sorted(clusters)
        for _ in range(config.bootstrap_resamples):
            sampled = rng.choices(ids, k=len(ids))
            draws.append(_mean([value for group in sampled for value in clusters[group]]))
    confidence = _interval(draws, config.confidence_level) if draws else None
    return _result(baseline_name, candidate_name, configuration, config, definition.direction,
                   pairs, groups, tuple(plan), tuple(insufficiency), confidence, draws, len(clusters))


def _result(
    baseline_name: str,
    candidate_name: str,
    configuration: ConfigurationComparison,
    config: PairedComparisonConfig,
    direction: MetricDirection,
    pairs: Sequence[PairedCaseDifference],
    groups: dict[str, str],
    case_ids: Sequence[str],
    insufficiency: Sequence[str],
    confidence: ConfidenceInterval | None,
    draws: Sequence[float],
    cluster_count: int,
) -> PairedComparisonResult:
    valid = [pair for pair in pairs if pair.candidate_minus_baseline is not None]
    qualification = configuration.comparison_qualification
    conclusion = "inconclusive"
    directed_confidence = None
    if draws and direction is not MetricDirection.NEUTRAL:
        directed_values = [_direct(value, direction) for value in draws]
        directed_confidence = _interval(
            [value for value in directed_values if value is not None],
            config.confidence_level,
        )
    if qualification.status == "diagnostic_only" or direction is MetricDirection.NEUTRAL:
        neutral_reason = (
            ("neutral metric has no ranking direction",)
            if direction is MetricDirection.NEUTRAL
            else ()
        )
        qualification = ComparisonQualification(
            status="diagnostic_only",
            ranking_eligible=False,
            reasons=qualification.reasons + tuple(insufficiency) + neutral_reason,
        )
        conclusion = "diagnostic_only"
    elif qualification.status != "incomparable" and insufficiency:
        qualification = ComparisonQualification(
            status="inconclusive",
            reasons=tuple(insufficiency),
            ranking_eligible=False,
        )
    elif qualification.status != "incomparable" and directed_confidence is not None:
        if directed_confidence.lower > 0:
            conclusion = "improvement"
        elif directed_confidence.upper < 0:
            conclusion = "regression"
        else:
            conclusion = "no_clear_difference"
            qualification = ComparisonQualification(
                status="inconclusive",
                reasons=("confidence interval does not establish a directional difference",),
                ranking_eligible=False,
            )
    mean = _mean([pair.candidate_minus_baseline for pair in valid]) if valid else None
    planned_repetitions = sum(len(pair.planned_repetitions) for pair in pairs)
    paired_repetitions = sum(len(pair.paired_repetitions) for pair in pairs)
    return PairedComparisonResult(
        baseline_name=baseline_name,
        candidate_name=candidate_name,
        configuration=configuration,
        comparison_qualification=qualification,
        config=config,
        direction=direction,
        planned_cases=len(case_ids),
        planned_groups=len({groups[case] for case in case_ids if case in groups}),
        planned_repetition_pairs=planned_repetitions,
        valid_pairs=len(valid),
        missing_pairs=len(case_ids) - len(valid),
        valid_repetition_pairs=paired_repetitions,
        missing_repetition_pairs=planned_repetitions - paired_repetitions,
        independent_groups=cluster_count,
        case_differences=tuple(pairs),
        candidate_minus_baseline=mean,
        directed_improvement=_direct(mean, direction) if mean is not None else None,
        confidence=confidence,
        directed_confidence=directed_confidence,
        bootstrap_completed=len(draws),
        conclusion=conclusion,
    )


__all__ = [
    "BOOTSTRAP_METHOD_VERSION",
    "PairedComparisonConfig",
    "ComparisonQualification",
    "ConfigurationComparison",
    "ConfidenceInterval",
    "PairedCaseDifference",
    "PairedComparisonResult",
    "qualify_comparison",
    "compare_saved_scores",
]
