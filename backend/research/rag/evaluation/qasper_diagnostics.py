"""Store-free, deterministic diagnostics over frozen QASPER execution artifacts.

Ranks refer to completed anchor paths, never membership of a gold chunk union.
Channel contributions are marginal path completions in explicit channel order;
channel ranks pool query variants using each chunk's earliest channel rank.
Latency percentiles use linear interpolation of supplied millisecond samples.
No clocks, external calls, prices, retrieval, or metric judgements run here.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from backend.research.rag.evaluation.protocol_common import (
    ExecutionProfile,
    NonEmptyStr,
    ProtocolModel,
)
from backend.research.rag.evaluation.protocol_execution import PlannedEvaluationUnit
from backend.research.rag.evaluation.qasper_mapping import (
    FrozenQasperCase,
    FrozenQasperDataset,
)
from backend.research.rag.evaluation.qasper_protocol import (
    NonnegativeNumber,
    QasperCaseResult,
    QasperDenominators,
    QasperFailureRecord,
    QasperMetric,
    QasperMetricDefinition,
    QasperReportArtifact,
    RankedResult,
    RetrievalStage,
)

Stage = RetrievalStage
Channel = Literal["dense", "bm25", "field"]
Policy = Literal["strict", "equivalent"]
Phase = Literal["index", "candidate", "fusion", "rerank", "request", "judge"]
Attribution = Literal[
    "hit",
    "partial_coverage",
    "candidate_recall_miss",
    "fusion_miss",
    "rerank_miss",
    "top_k_miss",
    "mapping_failure",
    "source_index_failure",
    "system_failure",
    "not_applicable",
    "telemetry_unavailable",
]
_PHASES: tuple[Phase, ...] = (
    "index",
    "candidate",
    "fusion",
    "rerank",
    "request",
    "judge",
)
_STAGES: tuple[Stage, ...] = ("candidate", "fused", "reranked")
QASPER_QUESTION_TYPE_DERIVATION_ID = "qasper-question-type-rules/v1"
Paths = Mapping[str, tuple[tuple[str, ...], ...]]


class QasperLatencyObservation(ProtocolModel):
    phase: Phase
    milliseconds: NonnegativeNumber


class QasperUnitTelemetry(ProtocolModel):
    """Explicit stage presence distinguishes an empty stage from an absent one.

    If fusion emits a larger ranking than the actual reranker input, supply the
    latter in rerank_input_chunk_ids. Otherwise all fused rows are that input.
    Each latency is one measured boundary per unit, not a synthetic sample.
    """

    case_id: NonEmptyStr
    repetition: int = Field(default=1, gt=0, strict=True)
    attempt_id: NonEmptyStr
    executed_stages: tuple[Stage, ...]
    latencies: tuple[QasperLatencyObservation, ...] = ()
    rerank_input_chunk_ids: tuple[NonEmptyStr, ...] | None = None

    @property
    def key(self) -> tuple[str, int, str]:
        return self.case_id, self.repetition, self.attempt_id

    @model_validator(mode="after")
    def _boundaries(self) -> Self:
        if self.executed_stages != tuple(
            stage for stage in _STAGES if stage in self.executed_stages
        ):
            raise ValueError("executed_stages must be unique and in execution order")
        phases = [item.phase for item in self.latencies]
        if len(phases) != len(set(phases)):
            raise ValueError("one latency observation per phase and unit is required")
        if self.rerank_input_chunk_ids is not None:
            if (
                "reranked" not in self.executed_stages
                or "fused" not in self.executed_stages
            ):
                raise ValueError(
                    "rerank input requires observed fused and reranked stages"
                )
            if len(self.rerank_input_chunk_ids) != len(
                set(self.rerank_input_chunk_ids)
            ):
                raise ValueError("rerank input chunk IDs must be unique")
        return self


class QasperExecutionTelemetry(ProtocolModel):
    run_id: NonEmptyStr
    execution_profile: ExecutionProfile
    # Include enabled channels only. Order defines marginal contribution credit.
    channel_order: tuple[Channel, ...]
    units: tuple[QasperUnitTelemetry, ...]
    index_cost_usd: NonnegativeNumber | None = None
    request_cost_usd: NonnegativeNumber | None = None
    judge_cost_usd: NonnegativeNumber | None = None

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if len(self.channel_order) != len(set(self.channel_order)):
            raise ValueError("channel_order must contain unique channels")
        if len({unit.key for unit in self.units}) != len(self.units):
            raise ValueError("execution telemetry must contain unique unit keys")
        if self.execution_profile is ExecutionProfile.FIXTURE and any(
            cost not in (None, 0.0)
            for cost in (
                self.index_cost_usd,
                self.request_cost_usd,
                self.judge_cost_usd,
            )
        ):
            raise ValueError("fixture external costs must be zero or not applicable")
        return self


class QasperStageDiagnostic(ProtocolModel):
    stage: Stage
    candidate_count: int
    completed_anchor_ids: tuple[str, ...]
    top_k_anchor_ids: tuple[str, ...]
    first_hit_rank: int | None


class QasperChannelDiagnostic(ProtocolModel):
    channel: Channel
    candidate_count: int | None
    first_gold_rank: int | None
    completed_anchor_ids: tuple[str, ...]
    new_anchor_ids: tuple[str, ...]
    final_first_hit_rank: int | None


class QasperAnchorMiss(ProtocolModel):
    anchor_id: str
    attribution: Attribution


class QasperCaseDiagnostic(ProtocolModel):
    case_id: str
    repetition: int
    attempt_id: str
    status: str
    mapping_status: str
    question_type: str
    question_type_source: Literal["dataset", "derived_not_gold", "unknown"]
    question_type_derivation_id: str | None
    paper: str
    sections: tuple[str, ...]
    # Slice cardinality is deduplicated text gold, not annotation/float rows.
    evidence_count: int
    raw_evidence_count: int
    gold_anchor_count: int
    measured: bool
    first_hit_rank: int | None
    hit_at_k: bool | None
    covered_groups: int | None
    required_groups: int | None
    attribution: Attribution
    reason: str | None
    stages: tuple[QasperStageDiagnostic, ...]
    channels: tuple[QasperChannelDiagnostic, ...]
    # Missing alternatives remain visible but do not turn a sufficient hit into
    # a case failure. Case coverage follows required groups, not all anchors.
    anchor_misses: tuple[QasperAnchorMiss, ...]


class QasperSliceMetric(ProtocolModel):
    metric_id: str
    version: str
    k: int | None
    value: float | None
    denominator: int


class QasperDiagnosticSlice(ProtocolModel):
    dimension: Literal[
        "question_type",
        "paper",
        "section",
        "field",
        "evidence_count",
        "mapping_status",
        "first_hit_rank",
    ]
    value: str
    unit_keys: tuple[tuple[str, int, str], ...]
    denominators: QasperDenominators
    diagnosed_count: int
    hit_count: int
    metrics: tuple[QasperSliceMetric, ...]
    attributions: tuple[tuple[Attribution, int], ...]


class QasperLatencySummary(ProtocolModel):
    phase: Phase
    sample_count: int
    missing_unit_count: int
    p50_ms: float | None
    p95_ms: float | None


class QasperExternalCost(ProtocolModel):
    scope: Literal["index", "request", "judge"]
    status: Literal["reported", "not_applicable", "not_reported"]
    usd: float | None


class QasperDiagnostics(ProtocolModel):
    run_id: str
    frozen_dataset_digest: str
    execution_profile: ExecutionProfile
    policy: Policy
    k: int
    qualification: Literal["diagnostic_only"] = "diagnostic_only"
    represents_production_latency: bool
    latency_note: str
    channel_order: tuple[Channel, ...]
    question_type_derivation_id: str
    question_type_note: str
    denominators: QasperDenominators
    cases: tuple[QasperCaseDiagnostic, ...]
    slices: tuple[QasperDiagnosticSlice, ...]
    latency: tuple[QasperLatencySummary, ...]
    external_costs: tuple[QasperExternalCost, ...]


def derive_qasper_question_type(case: FrozenQasperCase) -> tuple[str, str, str | None]:
    explicit = case.question_type.strip()
    if explicit and explicit.casefold() not in {"unknown", "other", "n/a", "na"}:
        return explicit, "dataset", None
    query = re.sub(r"\s+", " ", case.query.strip().casefold())
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "comparison",
            (
                r"\bcompar(?:e|ed|ing|ison)\b",
                r"\bdiffer(?:ence|ent|ently)?\b",
                r"\bversus\b|\bvs\.?\b",
                r"\bbetter than\b|\bworse than\b",
            ),
        ),
        (
            "numeric",
            (
                r"\bhow many\b|\bhow much\b",
                r"\bwhat (?:is|was|were) the (?:number|percentage|proportion|rate)\b",
                r"\bnumber of\b|\bpercentage of\b|\bproportion of\b",
            ),
        ),
        (
            "dataset",
            (
                r"\bdataset\b|\bdata set\b|\bcorpus\b|\bbenchmark\b",
                r"\btraining data\b|\btest data\b|\bevaluation data\b",
            ),
        ),
        (
            "result",
            (
                r"\bresult(?:s)?\b|\bfinding(?:s)?\b|\bconclu(?:de|ded|sion|sions)\b",
                r"\bperformance\b|\boutcome(?:s)?\b|\bachiev(?:e|ed|ement)\b",
            ),
        ),
        ("why_how", (r"^(?:why|how)\b",)),
        (
            "method",
            (
                r"\bmethod(?:ology)?\b|\bapproach\b|\btechnique\b",
                r"\balgorithm\b|\barchitecture\b|\bmodel\b|\bprocedure\b",
            ),
        ),
    )
    for label, patterns in rules:
        if any(re.search(pattern, query) for pattern in patterns):
            return label, "derived_not_gold", QASPER_QUESTION_TYPE_DERIVATION_ID
    return "unknown", "unknown", QASPER_QUESTION_TYPE_DERIVATION_ID


def _paths(
    case: FrozenQasperCase, policy: Policy
) -> dict[str, tuple[tuple[str, ...], ...]]:
    if policy == "strict":
        return {anchor.anchor_id: anchor.paths for anchor in case.strict_anchors}
    mapping = case.mapping_result.mapping
    return (
        {}
        if mapping is None
        else {
            anchor.anchor_id: tuple(tuple(path.chunk_ids) for path in anchor.chunk_sets)
            for anchor in mapping.anchor_mappings
        }
    )


def _completed(
    paths: Paths, ranks: Mapping[str, int], k: int | None = None
) -> dict[str, int]:
    found: dict[str, int] = {}
    for anchor_id, alternatives in paths.items():
        completions = [
            max(ranks[chunk] for chunk in path)
            for path in alternatives
            if path
            and all(
                chunk in ranks and (k is None or ranks[chunk] <= k) for chunk in path
            )
        ]
        if completions:
            found[anchor_id] = min(completions)
    return found


def _ranks(rows: Sequence[RankedResult], *, stage: Stage) -> dict[str, int]:
    ranks: dict[str, int] = {}
    for row in rows:
        if row.stage == stage:
            ranks[row.chunk_id] = min(row.rank, ranks.get(row.chunk_id, row.rank))
    return ranks


def _coverage(case: FrozenQasperCase, found: Mapping[str, int]) -> tuple[int, int]:
    mapping = case.mapping_result.mapping
    if mapping is None:
        return 0, 0
    groups = mapping.required_evidence_groups
    return sum(
        any(set(option).issubset(found) for option in group.alternatives)
        for group in groups
    ), len(groups)


def _nonmeasured_attribution(
    case: FrozenQasperCase, result: QasperCaseResult
) -> Attribution:
    categories = {failure.category for failure in result.failures}
    if "source_index_failure" in categories:
        return "source_index_failure"
    if (
        case.mapping_result.disposition == "not_applicable"
        or result.status == "not_applicable"
    ):
        return "not_applicable"
    if case.mapping_result.disposition != "valid" or "mapping_failure" in categories:
        return "mapping_failure"
    return "system_failure"


def _miss_cause(
    paths: Paths,
    stages: tuple[Stage, ...],
    ranks: Mapping[Stage, Mapping[str, int]],
    k: int,
    rerank_input: tuple[str, ...] | None,
) -> Attribution:
    if not stages:
        return "telemetry_unavailable"
    if "candidate" in stages and not _completed(paths, ranks["candidate"]):
        return "candidate_recall_miss"
    if "fused" in stages:
        # If reranking ran, diagnose loss at its actual input pool, not fused@K.
        pool = (
            ranks["fused"]
            if rerank_input is None
            else {chunk: 1 for chunk in rerank_input}
        )
        fused_hit = _completed(paths, pool, None if "reranked" in stages else k)
        if not fused_hit:
            return "fusion_miss" if "candidate" in stages else "telemetry_unavailable"
        if stages[-1] == "reranked":
            return "rerank_miss"
    if stages[-1] == "candidate":
        return "candidate_recall_miss"
    return "telemetry_unavailable"


def _diagnose_case(
    case: FrozenQasperCase,
    result: QasperCaseResult,
    telemetry: QasperUnitTelemetry,
    channels: tuple[Channel, ...],
    policy: Policy,
    k: int,
) -> QasperCaseDiagnostic:
    stages = telemetry.executed_stages
    rows = result.raw_ranked_results
    if any(row.stage not in stages for row in rows):
        raise ValueError(
            "report rows belong to a stage absent from execution telemetry"
        )
    if stages and result.final_stage != stages[-1]:
        raise ValueError("case final_stage differs from execution telemetry")
    ranks = {stage: _ranks(rows, stage=stage) for stage in stages}
    for previous, current in zip(stages, stages[1:]):
        if not ranks[current].keys() <= ranks[previous].keys():
            raise ValueError("a downstream stage cannot invent candidate chunks")
    if telemetry.rerank_input_chunk_ids is not None:
        pool = set(telemetry.rerank_input_chunk_ids)
        if not pool <= ranks["fused"].keys() or not ranks["reranked"].keys() <= pool:
            raise ValueError(
                "rerank input must match fused candidates and contain reranked rows"
            )
    paths = _paths(case, policy)
    valid = case.mapping_result.disposition == "valid" and result.status == "ok"
    measured = valid and bool(stages)
    final_all = _completed(paths, ranks[stages[-1]]) if measured else {}
    final_ranks = _completed(paths, ranks[stages[-1]], k) if measured else {}
    stage_diagnostics = tuple(
        QasperStageDiagnostic(
            stage=stage,
            candidate_count=len(ranks[stage]),
            completed_anchor_ids=tuple(sorted(_completed(paths, ranks[stage])))
            if valid
            else (),
            top_k_anchor_ids=tuple(sorted(_completed(paths, ranks[stage], k)))
            if valid
            else (),
            first_hit_rank=min(_completed(paths, ranks[stage]).values(), default=None)
            if valid
            else None,
        )
        for stage in stages
    )

    channel_ranks: dict[Channel, dict[str, int]] = {channel: {} for channel in channels}
    for row in rows:
        if row.stage != "candidate":
            continue
        for provenance in row.provenance:
            if provenance.channel not in channel_ranks:
                raise ValueError(
                    "candidate provenance channel is absent from channel_order"
                )
            bucket = channel_ranks[provenance.channel]
            bucket[row.chunk_id] = min(
                provenance.channel_rank,
                bucket.get(row.chunk_id, provenance.channel_rank),
            )
    seen: dict[str, int] = {}
    previous_anchors: set[str] = set()
    channel_diagnostics = []
    for channel in channels:
        own = _completed(paths, channel_ranks[channel]) if valid else {}
        seen.update(channel_ranks[channel])
        cumulative = _completed(paths, seen) if valid else {}
        channel_diagnostics.append(
            QasperChannelDiagnostic(
                channel=channel,
                candidate_count=len(channel_ranks[channel])
                if "candidate" in stages
                else None,
                first_gold_rank=min(own.values(), default=None),
                completed_anchor_ids=tuple(sorted(own)),
                new_anchor_ids=tuple(sorted(cumulative.keys() - previous_anchors)),
                final_first_hit_rank=min(
                    (rank for anchor, rank in final_all.items() if anchor in own),
                    default=None,
                ),
            )
        )
        previous_anchors = set(cumulative)

    covered, required = _coverage(case, final_ranks) if measured else (None, None)
    if not valid:
        attribution = _nonmeasured_attribution(case, result)
    elif not measured:
        attribution = "telemetry_unavailable"
    elif final_ranks:
        attribution = "hit" if covered == required else "partial_coverage"
    elif final_all:
        attribution = "top_k_miss"
    else:
        attribution = _miss_cause(
            paths, stages, ranks, k, telemetry.rerank_input_chunk_ids
        )
    misses = (
        tuple(
            QasperAnchorMiss(
                anchor_id=anchor,
                attribution=(
                    "top_k_miss"
                    if anchor in final_all
                    else _miss_cause(
                        {anchor: alternatives},
                        stages,
                        ranks,
                        k,
                        telemetry.rerank_input_chunk_ids,
                    )
                ),
            )
            for anchor, alternatives in sorted(paths.items())
            if anchor not in final_ranks
        )
        if measured
        else ()
    )
    question_type, question_type_source, derivation_id = derive_qasper_question_type(case)
    return QasperCaseDiagnostic(
        case_id=case.case_id,
        repetition=result.unit.repetition,
        attempt_id=result.attempt_id,
        status=result.status,
        mapping_status=case.mapping_result.disposition,
        question_type=question_type,
        question_type_source=question_type_source,
        question_type_derivation_id=derivation_id,
        paper=case.paper_id,
        sections=case.sections,
        evidence_count=len(paths),
        raw_evidence_count=case.raw_evidence_count,
        gold_anchor_count=len(paths),
        measured=measured,
        first_hit_rank=min(final_all.values(), default=None),
        hit_at_k=bool(final_ranks) if measured else None,
        covered_groups=covered,
        required_groups=required,
        attribution=attribution,
        reason=result.reason or case.mapping_result.reason,
        stages=stage_diagnostics,
        channels=tuple(channel_diagnostics),
        anchor_misses=misses,
    )


def attribute_qasper_retrieval_failures(
    case: FrozenQasperCase,
    unit: PlannedEvaluationUnit,
    rows: Sequence[RankedResult],
    final_stage: RetrievalStage,
    metrics: Sequence[QasperMetric],
) -> tuple[QasperFailureRecord, ...]:
    """Attribute measured misses using complete frozen paths, never ID overlap."""
    ranks = {stage: _ranks(rows, stage=stage) for stage in _STAGES}
    grouped: dict[tuple[str, str, str], list[QasperMetricDefinition]] = {}
    for metric in metrics:
        if metric.status != "ok" or metric.value is None or metric.value > 0:
            if (
                metric.status == "ok"
                and metric.metric_id.endswith("_coverage")
                and metric.value is not None
                and 0 < metric.value < 1
            ):
                key = (
                    "partial_coverage",
                    "scoring",
                    "Only part of the frozen required evidence groups was retrieved.",
                )
            else:
                continue
        else:
            policy: Policy = (
                "strict" if metric.metric_id.startswith("strict_") else "equivalent"
            )
            paths = _paths(case, policy)
            candidate = _completed(paths, ranks["candidate"])
            final_all = _completed(paths, ranks[final_stage])
            final_at_k = (
                final_all
                if metric.k is None
                else _completed(paths, ranks[final_stage], metric.k)
            )
            if (
                metric.metric_id.endswith("_coverage")
                and final_at_k
                and metric.numerator == 0
            ):
                key = (
                    "partial_coverage",
                    "scoring",
                    "Complete anchors were retrieved but no required evidence group was complete.",
                )
            elif not candidate:
                key = (
                    "candidate_recall_miss",
                    "candidate",
                    "No complete frozen evidence path appeared in the candidate stage.",
                )
            elif final_all and not final_at_k:
                key = (
                    "top_k_miss",
                    "scoring",
                    "A complete frozen evidence path appeared below the metric Top-K cutoff.",
                )
            elif not final_all:
                stage = "rerank" if final_stage == "reranked" else "fusion"
                key = (
                    "fusion_rerank_miss",
                    stage,
                    "A complete candidate evidence path was lost before the declared final stage.",
                )
            else:
                continue
        grouped.setdefault(key, []).append(
            QasperMetricDefinition(
                metric_id=metric.metric_id, version=metric.version, k=metric.k
            )
        )
    return tuple(
        QasperFailureRecord(
            unit=unit,
            category=category,
            status="ok",
            reason=reason,
            stage=stage,
            affected_metrics=tuple(definitions),
        )
        for (category, stage, reason), definitions in grouped.items()
    )


def _slice_metrics(
    results: Sequence[QasperCaseResult], definitions: Sequence[QasperMetricDefinition]
) -> tuple[QasperSliceMetric, ...]:
    output = []
    for definition in definitions:
        values = [
            metric.value
            for result in results
            for metric in result.metrics
            if metric.key == definition.key
            and metric.status == "ok"
            and metric.value is not None
        ]
        output.append(
            QasperSliceMetric(
                metric_id=definition.metric_id,
                version=definition.version,
                k=definition.k,
                value=math.fsum(values) / len(values) if values else None,
                denominator=len(values),
            )
        )
    return tuple(output)


def _slices(
    cases: tuple[QasperCaseDiagnostic, ...], report: QasperReportArtifact
) -> tuple[QasperDiagnosticSlice, ...]:
    buckets: dict[tuple[str, str], list[int]] = {}
    for index, case in enumerate(cases):
        first_hit = (
            str(case.first_hit_rank)
            if case.first_hit_rank is not None
            else "miss"
            if case.measured
            else "not_measured"
        )
        dimensions = {
            "question_type": (case.question_type,),
            "paper": (case.paper,),
            "section": tuple(sorted(set(case.sections))) or ("not_available",),
            "field": tuple(sorted(set(case.sections))) or ("not_available",),
            "evidence_count": (str(case.evidence_count),),
            "mapping_status": (case.mapping_status,),
            "first_hit_rank": (first_hit,),
        }
        for dimension, values in dimensions.items():
            for value in values:
                buckets.setdefault((dimension, value), []).append(index)
    output = []
    for (dimension, value), indices in sorted(buckets.items()):
        selected = [cases[index] for index in indices]
        counts = {
            status: sum(case.status == status for case in selected)
            for status in ("ok", "needs_review", "system_failure", "not_applicable")
        }
        output.append(
            QasperDiagnosticSlice(
                dimension=dimension,
                value=value,
                unit_keys=tuple(
                    (case.case_id, case.repetition, case.attempt_id)
                    for case in selected
                ),
                denominators=QasperDenominators(
                    planned=len(selected), valid=counts.pop("ok"), **counts
                ),
                diagnosed_count=sum(case.measured for case in selected),
                hit_count=sum(case.hit_at_k is True for case in selected),
                metrics=_slice_metrics(
                    [report.case_results[index] for index in indices],
                    report.manifest.metric_definitions,
                ),
                attributions=tuple(
                    (cause, sum(case.attribution == cause for case in selected))
                    for cause in sorted({case.attribution for case in selected})
                ),
            )
        )
    return tuple(output)


def _percentile(samples: Sequence[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    weight = position - low
    return (1.0 - weight) * ordered[low] + weight * ordered[high]


def diagnose_qasper_report(
    dataset: FrozenQasperDataset,
    report: QasperReportArtifact,
    telemetry: QasperExecutionTelemetry,
    *,
    k: int,
    policy: Policy = "equivalent",
) -> QasperDiagnostics:
    """Project one selected variant's planned units without reading any store.

    All three inputs must describe the same frozen data/run. Non-ok cases stay
    in slice denominators, never becoming retrieval negatives or latency zeros.
    K is explicit and affects stage hits/attribution; slice metric values retain
    their own original metric/K identities from the report. Equivalent output
    remains diagnostic-only under the existing pending-review mapping policy.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("k must be a positive integer")
    if policy not in ("strict", "equivalent"):
        raise ValueError("policy must be strict or equivalent")
    dataset.verify_digest()
    # Revalidate nested immutable contracts too (model_copy can bypass guards).
    report = QasperReportArtifact.model_validate_json(report.model_dump_json())
    telemetry = QasperExecutionTelemetry.model_validate_json(
        telemetry.model_dump_json()
    )
    data = report.manifest.data_identity
    if (
        (dataset.dataset_id, dataset.dataset_version, dataset.dataset_digest)
        != (data.dataset_id, data.dataset_version, data.dataset_digest)
        or dataset.binding != report.manifest.index_binding
        or dataset.corpus_sha256 != data.corpus_sha256
        or dataset.index_sha256 != data.chunk_sha256
    ):
        raise ValueError("frozen dataset and report dataset/index identities differ")
    if (
        telemetry.run_id != report.run_id
        or telemetry.execution_profile
        is not report.manifest.execution_identity.execution_profile
    ):
        raise ValueError("telemetry run/profile differs from report")
    frozen_cases = {case.case_id: case for case in dataset.cases}
    if len(frozen_cases) != len(dataset.cases):
        raise ValueError("frozen dataset case IDs must be unique")
    units = {unit.key: unit for unit in telemetry.units}
    expected = {
        (case.unit.case_id, case.unit.repetition, case.attempt_id)
        for case in report.case_results
    }
    if set(units) != expected:
        raise ValueError(
            "telemetry must match every report case/repetition/attempt exactly"
        )
    diagnostics = []
    for result in report.case_results:
        case = frozen_cases.get(result.unit.case_id)
        if (
            case is None
            or case.mapping_result != result.mapping_result
            or case.query != result.query
            or case.paper_id not in result.paper_ids
        ):
            raise ValueError("report case does not match frozen case/mapping")
        diagnostics.append(
            _diagnose_case(
                case,
                result,
                units[(case.case_id, result.unit.repetition, result.attempt_id)],
                telemetry.channel_order,
                policy,
                k,
            )
        )
    cases = tuple(diagnostics)
    latencies = []
    for phase in _PHASES:
        samples = [
            item.milliseconds
            for unit in telemetry.units
            for item in unit.latencies
            if item.phase == phase
        ]
        latencies.append(
            QasperLatencySummary(
                phase=phase,
                sample_count=len(samples),
                missing_unit_count=len(telemetry.units) - len(samples),
                p50_ms=_percentile(samples, 0.5),
                p95_ms=_percentile(samples, 0.95),
            )
        )
    fixture = telemetry.execution_profile is ExecutionProfile.FIXTURE
    costs = tuple(
        QasperExternalCost(
            scope=scope,
            usd=getattr(telemetry, f"{scope}_cost_usd"),
            status="reported"
            if getattr(telemetry, f"{scope}_cost_usd") is not None
            else "not_applicable"
            if fixture
            else "not_reported",
        )
        for scope in ("index", "request", "judge")
    )
    return QasperDiagnostics(
        run_id=report.run_id,
        frozen_dataset_digest=dataset.digest,
        execution_profile=telemetry.execution_profile,
        policy=policy,
        k=k,
        represents_production_latency=not fixture,
        latency_note=(
            "Fixture/in-memory measurements only; these are not Qdrant production latencies."
            if fixture
            else "Caller-supplied production observations only; missing samples and costs are not estimated."
        ),
        channel_order=telemetry.channel_order,
        question_type_derivation_id=QASPER_QUESTION_TYPE_DERIVATION_ID,
        question_type_note=(
            "Explicit dataset labels are preserved. Unknown labels use deterministic "
            "derived-not-gold text rules; unresolved questions remain unknown."
        ),
        denominators=report.denominators,
        cases=cases,
        slices=_slices(cases, report),
        latency=tuple(latencies),
        external_costs=costs,
    )


__all__ = [
    "QASPER_QUESTION_TYPE_DERIVATION_ID",
    "QasperLatencyObservation",
    "QasperUnitTelemetry",
    "QasperExecutionTelemetry",
    "QasperStageDiagnostic",
    "QasperChannelDiagnostic",
    "QasperAnchorMiss",
    "QasperCaseDiagnostic",
    "QasperSliceMetric",
    "QasperDiagnosticSlice",
    "QasperLatencySummary",
    "QasperExternalCost",
    "QasperDiagnostics",
    "derive_qasper_question_type",
    "attribute_qasper_retrieval_failures",
    "diagnose_qasper_report",
]
