from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from backend.research.rag.evaluation.qasper_protocol import (
    QasperMappingResult,
    QasperMetric,
    QasperMetricDefinition,
    RankedResult,
    RetrievalStage,
)
from backend.research.rag.evaluation.qasper_mapping import FrozenQasperCase


def _path_ranks(
    paths_by_anchor: Mapping[str, Sequence[tuple[str, ...]]],
    ranked_results: Sequence[RankedResult],
    k: int,
) -> dict[str, int]:
    rank_by_chunk: dict[str, int] = {}
    for row in ranked_results:
        if row.rank <= k:
            rank_by_chunk.setdefault(row.chunk_id, row.rank)
    found: dict[str, int] = {}
    for anchor_id, paths in paths_by_anchor.items():
        complete_ranks = [
            max(rank_by_chunk[chunk_id] for chunk_id in path)
            for path in paths
            if path and all(chunk_id in rank_by_chunk for chunk_id in path)
        ]
        if complete_ranks:
            found[anchor_id] = min(complete_ranks)
    return found


def _group_coverage(mapping: QasperMappingResult, found: set[str]) -> tuple[int, int]:
    assert mapping.mapping is not None
    groups = mapping.mapping.required_evidence_groups
    covered = sum(
        any(set(alternative).issubset(found) for alternative in group.alternatives)
        for group in groups
    )
    return covered, len(groups)


def _ndcg(found_ranks: Mapping[str, int], gold_anchor_count: int, k: int) -> float:
    # Binary per-rank gain, with each anchor credited once at path completion.
    actual_by_rank: dict[int, float] = {}
    for rank in found_ranks.values():
        actual_by_rank[rank] = 1.0
    dcg = sum(
        gain / math.log2(rank + 1)
        for rank, gain in actual_by_rank.items()
        if rank <= k
    )
    ideal_count = min(k, gold_anchor_count)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return min(1.0, dcg / ideal) if ideal else 0.0


def _non_measured(
    definition: QasperMetricDefinition, result: QasperMappingResult
) -> QasperMetric:
    status = result.disposition
    if status == "valid":
        status = "system_failure"
    return QasperMetric(
        metric_id=definition.metric_id,
        version=definition.version,
        k=definition.k,
        value=None,
        status=status,
        numerator=None,
        denominator=None,
        reason=result.reason
        or "Frozen mapping is unavailable for deterministic scoring.",
    )


def score_qasper_metrics(
    frozen_case: FrozenQasperCase | QasperMappingResult,
    ranked_results: Sequence[RankedResult],
    metric_definitions: Sequence[QasperMetricDefinition],
    *,
    final_stage: RetrievalStage,
    strict_anchor_ids: Mapping[str, Sequence[str]] | None = None,
) -> tuple[QasperMetric, ...]:
    """Score the explicitly declared final stage over complete frozen paths."""
    mapping_result = (
        frozen_case.mapping_result
        if isinstance(frozen_case, FrozenQasperCase)
        else frozen_case
    )
    ranked_results = tuple(row for row in ranked_results if row.stage == final_stage)
    if mapping_result.disposition != "valid" or mapping_result.mapping is None:
        return tuple(
            _non_measured(definition, mapping_result)
            for definition in metric_definitions
        )
    mapping = mapping_result.mapping
    mapping.verify_digest()
    if not mapping.required_evidence_groups:
        return tuple(
            _non_measured(definition, mapping_result)
            for definition in metric_definitions
        )
    if len({row.rank for row in ranked_results}) != len(ranked_results):
        raise ValueError("ranked results must have unique original ranks")

    equivalent_paths: dict[str, tuple[tuple[str, ...], ...]] = {
        anchor.anchor_id: tuple(tuple(path.chunk_ids) for path in anchor.chunk_sets)
        for anchor in mapping.anchor_mappings
        if anchor.chunk_sets
    }
    if isinstance(frozen_case, FrozenQasperCase):
        # Strict scoring consumes only the separately frozen exact-child paths.
        # It must never reuse parent/adjacent/normalized-span equivalent closure.
        exact_paths = {
            item.anchor_id: item.paths
            for item in frozen_case.strict_anchors
            if item.paths
        }
    else:
        exact_paths = {
            anchor.anchor_id: tuple(
                tuple(path.chunk_ids) for path in anchor.chunk_sets
            )
            for anchor in mapping.anchor_mappings
            if anchor.status == "exact" and anchor.chunk_sets
        }
    if strict_anchor_ids is not None:
        exact_paths = {
            anchor_id: (tuple(dict.fromkeys(chunk_ids)),)
            for anchor_id, chunk_ids in strict_anchor_ids.items()
            if chunk_ids
        }

    definitions = tuple(metric_definitions)
    keys = [(item.metric_id, item.k) for item in definitions]
    if len(keys) != len(set(keys)):
        raise ValueError("metric definitions must have unique metric/K pairs")
    declared = set(keys)
    for metric_id, cutoff in keys:
        partner = (
            "equivalent_" + metric_id.removeprefix("strict_")
            if metric_id.startswith("strict_")
            else "strict_" + metric_id.removeprefix("equivalent_")
        ), cutoff
        if partner not in declared:
            raise ValueError(
                "Strict and Equivalent metric definitions must be paired"
            )

    results: list[QasperMetric] = []
    for definition in definitions:
        is_strict = definition.metric_id.startswith("strict_")
        name = definition.metric_id.removeprefix("strict_").removeprefix(
            "equivalent_"
        )
        max_rank = max((row.rank for row in ranked_results), default=1)
        cutoff = definition.k if definition.k is not None else max_rank
        paths = exact_paths if is_strict else equivalent_paths
        completed = _path_ranks(paths, ranked_results, cutoff)
        if name == "hit":
            numerator, denominator = float(bool(completed)), 1
        elif name == "coverage":
            numerator_int, denominator = _group_coverage(
                mapping_result, set(completed)
            )
            numerator = float(numerator_int)
        elif name == "mrr":
            numerator, denominator = (
                1.0 / min(completed.values()) if completed else 0.0
            ), 1
        elif name == "ndcg":
            numerator, denominator = _ndcg(completed, len(paths), cutoff), 1
        else:
            raise ValueError(
                f"unsupported QASPER metric {definition.metric_id!r}"
            )
        results.append(
            QasperMetric(
                metric_id=definition.metric_id,
                version=definition.version,
                k=definition.k,
                mapping_version=mapping_result.mapping_version,
                value=numerator / denominator,
                status="ok",
                numerator=numerator,
                denominator=denominator,
                reason=None,
            )
        )
    return tuple(results)
