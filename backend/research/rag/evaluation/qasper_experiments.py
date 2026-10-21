from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass
from numbers import Real
from typing import Callable, Iterable, Mapping, Sequence, TypedDict

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.qasper_retrieval import RetrievalChannelInputs
from backend.research.rag.retrieval.channels.dense_text import DenseTextChannel
from backend.research.rag.retrieval.channels.field_embedding import FieldEmbeddingChannel
from backend.research.rag.retrieval.channels.sparse_lexical import SparseLexicalChannel
from backend.research.rag.retrieval.fusion import fuse_chunk_rankings
from backend.research.rag.retrieval.recall_stage import recall_queries_for_policy


class QueryVariant(TypedDict):
    query_id: str
    query_hash: str
    query: str


class RawChannelResult(TypedDict):
    channel: str
    query_id: str
    query_hash: str
    rank: int
    chunk_id: str
    score: float
    field_tag: str | None
    provenance: dict[str, object]


class ChannelDiagnostic(TypedDict):
    candidate_count: int
    first_gold_rank: int | None
    new_gold_evidence_ids: tuple[str, ...]


class QasperRetrievalResult(TypedDict):
    execution_profile: str
    profile: str
    profile_fingerprint: str
    parameters: dict[str, object]
    reranker_id: str | None
    channel_enabled: dict[str, bool]
    queries: tuple[QueryVariant, ...]
    raw_channel_results: tuple[RawChannelResult, ...]
    channel_candidates: dict[str, tuple[tuple[PaperChunk, float], ...]]
    channel_diagnostics: dict[str, ChannelDiagnostic]
    fused_results: tuple[tuple[PaperChunk, float], ...]
    candidate_count_before_rerank: int
    candidate_count: int
    final_results: tuple[tuple[PaperChunk, float], ...]
    ranked_chunks: tuple[PaperChunk, ...]
    ranked_scores: tuple[float, ...]


@dataclass(frozen=True)
class RetrievalProfile:
    name: str
    semantic: bool
    bm25: bool
    field: bool
    multi_query: bool = False


PROFILES: Mapping[str, RetrievalProfile] = {
    profile.name: profile for profile in (
        RetrievalProfile("semantic_only", True, False, False),
        RetrievalProfile("bm25_only", False, True, False),
        RetrievalProfile("field_only", False, False, True),
        RetrievalProfile("semantic_bm25", True, True, False),
        RetrievalProfile("semantic_field", True, False, True),
        RetrievalProfile("full_hybrid", True, True, True),
        RetrievalProfile("full_hybrid_multi_query", True, True, True, True),
    )
}

_SUPPORTED_CHANNELS = frozenset(("semantic", "bm25", "field"))


def _positive_integer(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _finite_real(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} must be a finite real number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite real number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite real number")
    return 0.0 if number == 0 else number


@dataclass(frozen=True)
class SweepParameters:
    candidate_limit: int = 100
    overfetch_multiplier: int = 3
    rrf_k: int = 60
    rerank_score_threshold: float | None = None
    channel_weights: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("candidate_limit", self.candidate_limit),
            ("overfetch_multiplier", self.overfetch_multiplier),
            ("rrf_k", self.rrf_k),
        ):
            _positive_integer(value, label)
        if self.rerank_score_threshold is not None:
            object.__setattr__(
                self,
                "rerank_score_threshold",
                _finite_real(self.rerank_score_threshold, "rerank_score_threshold"),
            )

        normalized_weights: list[tuple[str, float]] = []
        for item in self.channel_weights:
            if not isinstance(item, tuple) or len(item) != 2:
                raise ValueError("channel_weights entries must be (channel, weight) pairs")
            name, weight = item
            if not isinstance(name, str) or name not in _SUPPORTED_CHANNELS:
                raise ValueError("channel weights may only name semantic, bm25, or field")
            normalized_weight = _finite_real(weight, f"channel weight {name!r}")
            if normalized_weight <= 0:
                raise ValueError("channel weights must be positive")
            normalized_weights.append((name, normalized_weight))
        names = [name for name, _ in normalized_weights]
        if len(names) != len(set(names)):
            raise ValueError("channel weight names must be unique")
        object.__setattr__(self, "channel_weights", tuple(sorted(normalized_weights)))

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_limit": self.candidate_limit,
            "overfetch_multiplier": self.overfetch_multiplier,
            "rrf_k": self.rrf_k,
            "rerank_score_threshold": self.rerank_score_threshold,
            "channel_weights": dict(sorted(self.channel_weights)),
        }


@dataclass(frozen=True)
class ExperimentCase:
    profile: RetrievalProfile
    parameters: SweepParameters
    fingerprint: str


def _enabled_channels(profile: RetrievalProfile) -> frozenset[str]:
    return frozenset(name for name in _SUPPORTED_CHANNELS if getattr(profile, name))


def _validate_profile_weights(profile: RetrievalProfile, parameters: SweepParameters) -> None:
    disabled = {name for name, _ in parameters.channel_weights} - _enabled_channels(profile)
    if disabled:
        raise ValueError(
            "channel weights may only name channels enabled by the selected profile; "
            f"disabled={sorted(disabled)}"
        )


def build_sweep(
    profiles: Sequence[str], *, candidate_limits: Sequence[int],
    overfetch_multipliers: Sequence[int], rrf_ks: Sequence[int],
    rerank_score_thresholds: Sequence[float | None],
    channel_weights: Sequence[Mapping[str, float]], identity: Mapping[str, str],
) -> tuple[ExperimentCase, ...]:
    """Build a deterministic Cartesian sweep; every explicit combination is retained."""
    profile_names = tuple(profiles)
    if not profile_names or any(not isinstance(name, str) or name not in PROFILES for name in profile_names):
        raise ValueError("profiles must name one or more supported QASPER profiles")
    if len(profile_names) != len(set(profile_names)):
        raise ValueError("profiles must not contain duplicates")
    required_identity = {"code", "dataset", "index", "embedding", "artifact"}
    missing_identity = required_identity - identity.keys()
    if missing_identity or any(not str(identity[key]).strip() for key in required_identity if key in identity):
        raise ValueError(f"sweep identity must bind non-empty code/dataset/index/embedding/artifact; missing={sorted(missing_identity)}")
    if not all((candidate_limits, overfetch_multipliers, rrf_ks, rerank_score_thresholds, channel_weights)):
        raise ValueError("every sweep dimension must contain at least one explicit value")
    cases: list[ExperimentCase] = []
    fingerprints: set[str] = set()
    for profile_name, limit, overfetch, rrf_k, threshold, weights in itertools.product(
        profile_names, candidate_limits, overfetch_multipliers, rrf_ks, rerank_score_thresholds, channel_weights
    ):
        if not isinstance(weights, Mapping):
            raise ValueError("channel_weights entries must be mappings")
        params = SweepParameters(
            candidate_limit=limit, overfetch_multiplier=overfetch, rrf_k=rrf_k,
            rerank_score_threshold=threshold,
            channel_weights=tuple(weights.items()),
        )
        profile = PROFILES[profile_name]
        _validate_profile_weights(profile, params)
        payload = {"profile": profile.__dict__, "parameters": params.as_dict(), "identity": dict(sorted(identity.items()))}
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        if fingerprint in fingerprints:
            raise ValueError("sweep contains duplicate parameter combinations")
        fingerprints.add(fingerprint)
        cases.append(ExperimentCase(profile, params, fingerprint))
    return tuple(cases)


Reranker = Callable[[str, PaperChunk], float]


class _InMemorySparseLexicalChannel(SparseLexicalChannel):
    """Production BM25 channel with a snapshot-local, read-only index only."""

    def __init__(self, chunk_store: object) -> None:
        super().__init__(chunk_store)
        self._snapshot_indexes = {}

    def _load_bm25_index(self, paper_id: str, trace):
        from backend.research.rag.retrieval.bm25_index import PaperBM25Index

        cached = self._snapshot_indexes.get(paper_id)
        if cached is None:
            chunks = self._store.list_chunks(paper_id)
            cached = (PaperBM25Index.build(paper_id, chunks), chunks)
            self._snapshot_indexes[paper_id] = cached
        index, chunks = cached
        return index, "fixture_memory_snapshot", chunks


def retrieve_profile(
    *, profile: RetrievalProfile, inputs: RetrievalChannelInputs, paper_id: str,
    question: str, policy: object, parameters: SweepParameters,
    reranker: Reranker | None = None, reranker_id: str | None = None,
    frozen_gold_evidence_ids: Sequence[str] = (),
) -> QasperRetrievalResult:
    """Retrieve from fixture ports and retain raw, fused, and final ranked outputs."""
    _validate_profile_weights(profile, parameters)
    if parameters.rerank_score_threshold is not None and (reranker is None or not reranker_id or not reranker_id.strip()):
        raise ValueError("rerank threshold requires an explicit reranker and stable reranker_id")
    if reranker is not None and not reranker_id:
        raise ValueError("a supplied reranker requires a stable reranker_id")
    if profile.multi_query:
        class _MultiQueryPolicy:
            multi_query_enabled = True

            def __getattr__(self, name: str):
                return getattr(policy, name)

        from backend.research.rag.retrieval.paper_policy import classify_query_intent

        queries = recall_queries_for_policy(question, classify_query_intent(question), _MultiQueryPolicy())
    else:
        queries = [question]
    query_records: tuple[QueryVariant, ...] = tuple(
        {"query_id": f"q{index:03d}", "query_hash": hashlib.sha256(query.encode("utf-8")).hexdigest(), "query": query}
        for index, query in enumerate(dict.fromkeys(str(item).strip() for item in queries if str(item).strip()))
    )
    recall_limit = parameters.candidate_limit * parameters.overfetch_multiplier
    raw_channel_results: list[dict[str, object]] = []
    channel_rankings: dict[str, dict[str, list[tuple[PaperChunk, float]]]] = {}

    dense_channel = DenseTextChannel(inputs.chunk_store) if profile.semantic else None
    bm25_channel = _InMemorySparseLexicalChannel(inputs.chunk_store) if profile.bm25 else None
    field_channel = None
    if profile.field:
        if inputs.field_index is None:
            raise ValueError("field profile requires a field embedding index")
        field_channel = FieldEmbeddingChannel(inputs.chunk_store, inputs.field_index)

    for query_record in query_records:
        query_id = str(query_record["query_id"])
        query = str(query_record["query"])
        query_hash = str(query_record["query_hash"])
        if dense_channel is not None:
            rows = dense_channel.recall_chunks(paper_id=paper_id, query_text=query, filters={}, limit=recall_limit)
            channel_rankings.setdefault("semantic", {})[query_id] = rows
            _append_raw_rows(raw_channel_results, "semantic", query_id, query_hash, rows)
        if bm25_channel is not None:
            rows = bm25_channel.recall_chunks(paper_id=paper_id, query_text=query, filters={}, limit=recall_limit)
            channel_rankings.setdefault("bm25", {})[query_id] = rows
            _append_raw_rows(raw_channel_results, "bm25", query_id, query_hash, rows)
        if field_channel is not None:
            hits = field_channel.search_hits(paper_id=paper_id, query_text=query, field_names=None,
                                             candidate_filters=[{}], limit=recall_limit)
            for rank, hit in enumerate(hits, start=1):
                raw_channel_results.append({
                    "channel": "field", "query_id": query_id, "query_hash": query_hash,
                    "rank": rank, "chunk_id": hit.chunk_id, "score": float(hit.score),
                    "field_tag": hit.field_name, "provenance": dict(hit.metadata),
                })
            channel_rankings.setdefault("field", {})[query_id] = field_channel.ranked_chunks(hits, paper_id)

    rankings: list[tuple[str, list[tuple[PaperChunk, float]]]] = []
    for channel_name, per_query in channel_rankings.items():
        for query_id, rows in per_query.items():
            rankings.append((f"{channel_name}@{query_id}", rows))
    if len(rankings) > 1:
        union_ids = {chunk.chunk_id for _, rows in rankings for chunk, _ in rows}
        fused_results = fuse_chunk_rankings(rankings, limit=len(union_ids), rrf_k=parameters.rrf_k,
                                            metadata_prefix="qasper")
    elif rankings:
        fused_results = list(rankings[0][1])
    else:
        fused_results = []

    weights = dict(parameters.channel_weights)
    if weights and fused_results and len(rankings) > 1:
        weighted: list[tuple[PaperChunk, float]] = []
        for chunk, _ in fused_results:
            contributions = chunk.metadata.get("qasper_rrf_channel_contributions", {})
            score = sum(
                float(value) * weights.get(channel_query.split("@", 1)[0], 1.0)
                for channel_query, value in contributions.items()
            )
            weighted.append((chunk, score))
        weighted.sort(key=lambda item: (-item[1], item[0].section_index, item[0].chunk_id))
        fused_results = weighted

    candidate_pool = fused_results[:parameters.candidate_limit]
    candidate_count_before_rerank = len(candidate_pool)
    if reranker is not None:
        rescored = [(chunk, float(reranker(question, chunk))) for chunk, _ in candidate_pool]
        if any(not math.isfinite(score) for _, score in rescored):
            raise ValueError("reranker returned a non-finite score")
        if parameters.rerank_score_threshold is not None:
            rescored = [(chunk, score) for chunk, score in rescored if score >= parameters.rerank_score_threshold]
        rescored.sort(key=lambda item: (-item[1], item[0].section_index, item[0].chunk_id))
        final_results = rescored
    else:
        final_results = candidate_pool

    channel_candidates = {
        channel: _best_channel_candidates(per_query)
        for channel, per_query in channel_rankings.items()
    }
    diagnostics: dict[str, dict[str, object]] = {}
    seen_gold: set[str] = set()
    frozen_gold = set(frozen_gold_evidence_ids)
    for channel_name in channel_rankings:
        rows = [item for item in raw_channel_results if item["channel"] == channel_name]
        unique_candidates = {str(item["chunk_id"]) for item in rows}
        hit_rows = [item for item in rows if item["chunk_id"] in frozen_gold]
        first_hit = min((int(item["rank"]) for item in hit_rows), default=None)
        channel_gold = {str(item["chunk_id"]) for item in hit_rows}
        diagnostics[channel_name] = {
            "candidate_count": len(unique_candidates), "first_gold_rank": first_hit,
            "new_gold_evidence_ids": tuple(sorted(channel_gold - seen_gold)),
        }
        seen_gold.update(channel_gold)

    return {
        "execution_profile": "fixture", "profile": profile.name,
        "profile_fingerprint": hashlib.sha256(json.dumps(profile.__dict__, sort_keys=True).encode()).hexdigest(),
        "parameters": parameters.as_dict(), "reranker_id": reranker_id,
        "channel_enabled": {"semantic": profile.semantic, "bm25": profile.bm25, "field": profile.field},
        "queries": query_records,
        "raw_channel_results": tuple(raw_channel_results),
        "channel_candidates": channel_candidates,
        "channel_diagnostics": diagnostics,
        "fused_results": tuple(fused_results),
        "candidate_count_before_rerank": candidate_count_before_rerank,
        "candidate_count": len(final_results),
        "final_results": tuple(final_results),
        # Stable aliases retained for callers already consuming the earlier API.
        "ranked_chunks": tuple(chunk for chunk, _ in final_results),
        "ranked_scores": tuple(score for _, score in final_results),
    }


def _append_raw_rows(
    target: list[dict[str, object]], channel: str, query_id: str, query_hash: str,
    rows: Sequence[tuple[PaperChunk, float]],
) -> None:
    for rank, (chunk, score) in enumerate(rows, start=1):
        target.append({
            "channel": channel, "query_id": query_id, "query_hash": query_hash,
            "rank": rank, "chunk_id": chunk.chunk_id, "score": float(score),
            "field_tag": None, "provenance": dict(chunk.metadata),
        })


def _best_channel_candidates(
    per_query: Mapping[str, Sequence[tuple[PaperChunk, float]]],
) -> tuple[tuple[PaperChunk, float], ...]:
    best: dict[str, tuple[PaperChunk, float]] = {}
    for rows in per_query.values():
        for chunk, score in rows:
            current = best.get(chunk.chunk_id)
            if current is None or score > current[1]:
                best[chunk.chunk_id] = (chunk, float(score))
    result = list(best.values())
    result.sort(key=lambda item: (-item[1], item[0].section_index, item[0].chunk_id))
    return tuple(result)


@dataclass(frozen=True)
class CostReport:
    external_calls: int = 0
    credential_present: bool = False
    external_cost_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.external_calls < 0 or not math.isfinite(self.external_cost_usd) or self.external_cost_usd < 0:
            raise ValueError("invalid cost report")
        if self.external_calls == 0 and not self.credential_present and self.external_cost_usd != 0.0:
            raise ValueError("no external calls and no credentials must report zero external cost")


def report_cost(*, external_calls: int, credential_present: bool, measured_cost_usd: float | None = None) -> CostReport:
    if external_calls == 0:
        return CostReport(external_calls=0, credential_present=credential_present, external_cost_usd=0.0)
    if measured_cost_usd is None:
        raise ValueError("external usage requires a measured cost")
    return CostReport(external_calls, credential_present, float(measured_cost_usd))


__all__ = [
    "PROFILES", "RetrievalProfile", "SweepParameters", "ExperimentCase", "build_sweep",
    "QueryVariant", "RawChannelResult", "ChannelDiagnostic", "QasperRetrievalResult",
    "retrieve_profile", "CostReport", "report_cost",
]
