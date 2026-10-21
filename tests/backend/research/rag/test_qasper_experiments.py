from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from backend.research.document.models import PaperChunk
from backend.research.rag.evaluation.qasper_experiments import (
    PROFILES, SweepParameters, build_sweep, report_cost, retrieve_profile,
)
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache, RetrievalChannelInputs


def _chunk(cid: str, index: int) -> PaperChunk:
    return PaperChunk(chunk_id=cid, paper_id="p1", parse_source="latex", chunk_type="paragraph",
                      content=f"evidence {cid}", section_index=index, metadata={})


class _Store:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.chunks = [_chunk("c1", 0), _chunk("c2", 1)]

    def search_with_scores(self, paper_id, query_text, *, filters=None, limit=10):
        self.calls.append("semantic")
        return [(self.chunks[0], 0.9)]

    def list_chunks(self, paper_id):
        self.calls.append("bm25")
        return self.chunks

    def get_chunk(self, chunk_id):
        return next((chunk for chunk in self.chunks if chunk.chunk_id == chunk_id), None)


class _FieldIndex:
    def search_field_vectors(self, paper_id, query_text, *, field_names=None, filters=None, limit=10):
        from backend.research.ports.field_embedding_index import FieldEmbeddingHit
        return [FieldEmbeddingHit("c2", "content", 0.8, "evidence c2")]


def test_seven_profiles_are_explicit_and_disabled_channels_are_not_called() -> None:
    store = _Store()
    result = retrieve_profile(profile=PROFILES["semantic_only"], inputs=RetrievalChannelInputs(store),
                              paper_id="p1", question="question", policy=SimpleNamespace(multi_query_enabled=False),
                              parameters=SweepParameters(candidate_limit=5), frozen_gold_evidence_ids=("c1",))
    assert len(PROFILES) == 7
    assert store.calls == ["semantic"]
    assert result["execution_profile"] == "fixture"
    assert result["channel_diagnostics"]["semantic"]["first_gold_rank"] == 1
    assert result["channel_diagnostics"]["semantic"]["new_gold_evidence_ids"] == ("c1",)
    assert result["channel_enabled"] == {"semantic": True, "bm25": False, "field": False}


def test_field_only_uses_field_channel_and_requires_field_index() -> None:
    store = _Store()
    with pytest.raises(ValueError, match="requires a field"):
        retrieve_profile(profile=PROFILES["field_only"], inputs=RetrievalChannelInputs(store),
                         paper_id="p1", question="question", policy=SimpleNamespace(multi_query_enabled=False),
                         parameters=SweepParameters())
    result = retrieve_profile(profile=PROFILES["field_only"], inputs=RetrievalChannelInputs(store, _FieldIndex()),
                              paper_id="p1", question="question", policy=SimpleNamespace(multi_query_enabled=False),
                              parameters=SweepParameters())
    assert [chunk.chunk_id for chunk in result["ranked_chunks"]] == ["c2"]
    assert store.calls == []


def test_sweep_fingerprints_are_unique_and_bind_identity_and_every_dimension() -> None:
    cases = build_sweep(["semantic_only", "full_hybrid"], candidate_limits=[5, 10],
                        overfetch_multipliers=[2], rrf_ks=[20], rerank_score_thresholds=[None, 0.15],
                        channel_weights=[{"semantic": 1.0}], identity={"code": "abc", "dataset": "fixture-v1", "index": "fixture-index", "embedding": "cache-v1", "artifact": "run-fixture"})
    assert len(cases) == 8
    assert len({case.fingerprint for case in cases}) == len(cases)
    assert {case.parameters.candidate_limit for case in cases} == {5, 10}


@pytest.mark.parametrize("field", ["candidate_limit", "overfetch_multiplier", "rrf_k"])
@pytest.mark.parametrize("value", [True, 1.5, 0, -1])
def test_integer_budgets_require_true_positive_integers(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        SweepParameters(**{field: value})


@pytest.mark.parametrize("value", [True, "0.1", float("nan"), float("inf"), float("-inf")])
def test_threshold_requires_a_finite_non_boolean_number(value: object) -> None:
    with pytest.raises(ValueError, match="finite real number"):
        SweepParameters(rerank_score_threshold=value)

    assert SweepParameters(rerank_score_threshold=-1).rerank_score_threshold == -1.0


@pytest.mark.parametrize("weight", [True, "2", 0, -1, float("nan"), float("inf")])
def test_channel_weights_require_positive_finite_non_boolean_numbers(weight: object) -> None:
    with pytest.raises(ValueError, match="channel weight|positive"):
        SweepParameters(channel_weights=(("semantic", weight),))


def test_channel_weight_names_are_supported_and_unique() -> None:
    with pytest.raises(ValueError, match="semantic, bm25, or field"):
        SweepParameters(channel_weights=(("dense", 1.0),))
    with pytest.raises(ValueError, match="unique"):
        SweepParameters(channel_weights=(("semantic", 1.0), ("semantic", 2.0)))


def test_build_sweep_rejects_coercible_weight_values_and_duplicate_inputs() -> None:
    identity = {"code": "abc", "dataset": "fixture-v1", "index": "fixture-index",
                "embedding": "cache-v1", "artifact": "run-fixture"}
    kwargs = {"candidate_limits": [5], "overfetch_multipliers": [2], "rrf_ks": [20],
              "rerank_score_thresholds": [None], "identity": identity}

    for invalid in (True, "1.0"):
        with pytest.raises(ValueError, match="finite real number"):
            build_sweep(["semantic_only"], channel_weights=[{"semantic": invalid}], **kwargs)
    with pytest.raises(ValueError, match="profiles must not contain duplicates"):
        build_sweep(["semantic_only", "semantic_only"], channel_weights=[{}], **kwargs)
    with pytest.raises(ValueError, match="duplicate parameter combinations"):
        build_sweep(["semantic_only"], channel_weights=[{}, {}], **kwargs)


def test_sweep_fingerprint_is_deterministic_for_mapping_order_and_numeric_normalization() -> None:
    identity = {"code": "abc", "dataset": "fixture-v1", "index": "fixture-index",
                "embedding": "cache-v1", "artifact": "run-fixture"}
    first = build_sweep(["semantic_bm25"], candidate_limits=[5], overfetch_multipliers=[2],
                        rrf_ks=[20], rerank_score_thresholds=[0],
                        channel_weights=[{"semantic": 2, "bm25": 1.0}], identity=identity)[0]
    second = build_sweep(["semantic_bm25"], candidate_limits=[5], overfetch_multipliers=[2],
                         rrf_ks=[20], rerank_score_thresholds=[-0.0],
                         channel_weights=[{"bm25": 1, "semantic": 2.0}],
                         identity=dict(reversed(tuple(identity.items()))))[0]

    assert first.fingerprint == second.fingerprint
    assert first.parameters.rerank_score_threshold == 0.0
    assert first.parameters.channel_weights == (("bm25", 1.0), ("semantic", 2.0))


def test_disabled_channel_weight_fails_before_any_channel_call() -> None:
    store = _Store()
    with pytest.raises(ValueError, match="channels enabled by the selected profile"):
        retrieve_profile(
            profile=PROFILES["semantic_only"], inputs=RetrievalChannelInputs(store),
            paper_id="p1", question="question", policy=SimpleNamespace(multi_query_enabled=False),
            parameters=SweepParameters(channel_weights=(("bm25", 1.0),)),
        )
    assert store.calls == []


def test_build_sweep_rejects_disabled_channel_weights() -> None:
    with pytest.raises(ValueError, match="channels enabled by the selected profile"):
        build_sweep(["semantic_only"], candidate_limits=[5], overfetch_multipliers=[2], rrf_ks=[20],
                    rerank_score_thresholds=[None], channel_weights=[{"bm25": 1.0}],
                    identity={"code": "abc", "dataset": "fixture-v1", "index": "fixture-index",
                              "embedding": "cache-v1", "artifact": "run-fixture"})


def test_rerank_threshold_requires_declared_real_scorer() -> None:
    with pytest.raises(ValueError, match="explicit reranker"):
        retrieve_profile(profile=PROFILES["semantic_only"], inputs=RetrievalChannelInputs(_Store()),
                         paper_id="p1", question="q", policy=SimpleNamespace(multi_query_enabled=False),
                         parameters=SweepParameters(rerank_score_threshold=0.15))


def test_fixture_cost_is_zero_without_external_calls_or_credentials() -> None:
    assert report_cost(external_calls=0, credential_present=False).external_cost_usd == 0.0
    with pytest.raises(ValueError, match="measured cost"):
        report_cost(external_calls=1, credential_present=True)



def test_bm25_and_hybrid_profiles_use_real_sparse_and_fusion_implementations() -> None:
    store = _Store()
    policy = SimpleNamespace(multi_query_enabled=False)
    bm25 = retrieve_profile(profile=PROFILES["bm25_only"], inputs=RetrievalChannelInputs(store),
                            paper_id="p1", question="evidence", policy=policy,
                            parameters=SweepParameters(candidate_limit=2))
    assert store.calls == ["bm25"]
    assert bm25["channel_enabled"] == {"semantic": False, "bm25": True, "field": False}

    store.calls.clear()
    hybrid = retrieve_profile(profile=PROFILES["semantic_bm25"], inputs=RetrievalChannelInputs(store),
                              paper_id="p1", question="evidence", policy=policy,
                              parameters=SweepParameters(candidate_limit=2, rrf_k=20))
    assert store.calls == ["semantic", "bm25"]
    assert hybrid["channel_enabled"] == {"semantic": True, "bm25": True, "field": False}
    assert hybrid["ranked_chunks"]
    assert len(hybrid["fused_results"]) == 2  # complete union remains available beyond final candidate limit
    assert {item["channel"] for item in hybrid["raw_channel_results"]} == {"semantic", "bm25"}
    assert all({"query_id", "query_hash", "rank", "score", "field_tag"} <= item.keys()
               for item in hybrid["raw_channel_results"])


def test_weighted_rrf_orders_complete_union_before_candidate_limit(monkeypatch) -> None:
    from backend.research.rag.retrieval import bm25_index

    class _WeightedStore(_Store):
        def search_with_scores(self, paper_id, query_text, *, filters=None, limit=10):
            self.calls.append("semantic")
            return [(self.chunks[0], 0.9), (self.chunks[1], 0.8)]

        def list_chunks(self, paper_id):
            self.calls.append("bm25")
            # BM25 sees only c2's supporting term in this frozen fixture snapshot.
            return [self.chunks[1].model_copy(update={"content": "unrelated method"}), _chunk("c3", 2)]

    monkeypatch.setattr(bm25_index, "default_bm25_index_path",
                        lambda _paper_id: (_ for _ in ()).throw(AssertionError("production BM25 path touched")))
    store = _WeightedStore()
    result = retrieve_profile(
        profile=PROFILES["semantic_bm25"], inputs=RetrievalChannelInputs(store), paper_id="p1",
        question="evidence", policy=SimpleNamespace(multi_query_enabled=False),
        parameters=SweepParameters(candidate_limit=1, rrf_k=60, channel_weights=(("semantic", 0.01), ("bm25", 100.0))),
    )
    assert result["fused_results"][0][0].chunk_id == "c3"
    assert {chunk.chunk_id for chunk, _score in result["fused_results"]} == {"c1", "c2", "c3"}
    assert [chunk.chunk_id for chunk, _score in result["final_results"]] == ["c3"]


def test_offline_embedding_cache_keeps_numpy_matrix_and_compact_key_map(tmp_path) -> None:
    vectors = np.arange(40, dtype=np.float32).reshape(10, 4)
    keys = [f"key-{index}" for index in range(10)]
    path = tmp_path / "large-shaped-cache.npz"
    np.savez_compressed(path, keys=np.asarray(keys), vectors=vectors)

    cache = OfflineEmbeddingCache.load(path)

    assert cache.matrix.dtype == np.float32
    assert cache.matrix.shape == (10, 4)
    assert cache.key_to_row == {key: index for index, key in enumerate(keys)}
    assert not cache.matrix.flags.writeable




def test_multi_query_preserves_each_query_raw_rank_and_hash() -> None:
    store = _Store()
    result = retrieve_profile(
        profile=PROFILES["full_hybrid_multi_query"],
        inputs=RetrievalChannelInputs(store, _FieldIndex()), paper_id="p1",
        question="compare model accuracy", policy=SimpleNamespace(multi_query_enabled=False),
        parameters=SweepParameters(candidate_limit=5),
    )
    query_ids = {query["query_id"] for query in result["queries"]}
    raw_query_ids = {row["query_id"] for row in result["raw_channel_results"]}
    assert len(query_ids) > 1
    assert raw_query_ids == query_ids
    assert all(len(row["query_hash"]) == 64 for row in result["raw_channel_results"])
    assert all(row["rank"] > 0 for row in result["raw_channel_results"])
    assert any(row["channel"] == "field" and row["field_tag"] == "content"
               for row in result["raw_channel_results"])
