from __future__ import annotations

import numpy as np
import pytest

from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache, QasperOfflineCacheError


def test_offline_cache_loads_fixture_npz_and_hashes_text(tmp_path) -> None:
    text = "frozen question"
    key = OfflineEmbeddingCache.key(text)
    path = tmp_path / "embeddings.npz"
    np.savez_compressed(path, keys=np.asarray([key]), vectors=np.asarray([[0.25, 0.75]], dtype=np.float32))

    cache = OfflineEmbeddingCache.load(path)

    assert cache.vector_for(text) == pytest.approx((0.25, 0.75))
    assert len(cache.cache_id) == 64


def test_missing_embedding_key_fails_explicitly_without_provider_fallback() -> None:
    cache = OfflineEmbeddingCache.from_vectors({}, cache_id="fixture-cache-v1")

    with pytest.raises(QasperOfflineCacheError, match="never calls an embedding provider"):
        cache.vector_for("uncached query")


def test_cache_rejects_inconsistent_or_nonfinite_vectors() -> None:
    with pytest.raises(ValueError, match="same dimension"):
        OfflineEmbeddingCache.from_vectors({"a": [1.0], "b": [1.0, 2.0]}, cache_id="fixture")
    with pytest.raises(ValueError, match="finite"):
        OfflineEmbeddingCache.from_vectors({"a": [float("nan")]}, cache_id="fixture")


def test_cached_fixture_store_drives_real_dense_channel_without_encoder_fallback() -> None:
    from types import SimpleNamespace

    from backend.research.document.models import PaperChunk
    from backend.research.rag.evaluation.qasper_experiments import PROFILES, SweepParameters, retrieve_profile
    from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureChunkStore, RetrievalChannelInputs

    chunk = PaperChunk(chunk_id="c1", paper_id="p1", parse_source="latex", chunk_type="paragraph",
                       content="cached evidence", section_index=0, metadata={})
    question = "cached question"
    cache = OfflineEmbeddingCache.from_vectors(
        {OfflineEmbeddingCache.key(chunk.content): [1.0, 0.0], OfflineEmbeddingCache.key(question): [1.0, 0.0]},
        cache_id="fixture-only",
    )
    store = CachedFixtureChunkStore([chunk], cache)
    result = retrieve_profile(profile=PROFILES["semantic_only"], inputs=RetrievalChannelInputs(store),
                              paper_id="p1", question=question,
                              policy=SimpleNamespace(multi_query_enabled=False),
                              parameters=SweepParameters(candidate_limit=1))
    assert [item.chunk_id for item in result["ranked_chunks"]] == ["c1"]

    with pytest.raises(QasperOfflineCacheError, match="never calls an embedding provider"):
        store.search_with_scores("p1", "uncached query")


def test_cached_fixture_field_index_requires_all_real_extracted_field_vectors() -> None:
    from backend.research.document.models import PaperChunk
    from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureFieldIndex

    chunk = PaperChunk(chunk_id="c1", paper_id="p1", parse_source="latex", chunk_type="paragraph",
                       section_title="Method", content="cached field body", section_index=0, metadata={})
    field_text = "cached field body"
    cache = OfflineEmbeddingCache.from_vectors(
        {OfflineEmbeddingCache.key(field_text): [0.0, 1.0]}, cache_id="field-fixture"
    )
    with pytest.raises(QasperOfflineCacheError, match="missing"):
        CachedFixtureFieldIndex([chunk], cache)


def test_cached_fixture_field_index_searches_with_cached_vectors_only() -> None:
    from backend.research.document.models import PaperChunk
    from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureFieldIndex

    chunk = PaperChunk(chunk_id="c1", paper_id="p1", parse_source="latex", chunk_type="paragraph",
                       section_title="Method", content="cached field body", section_index=0, metadata={})
    text = "cached field body"
    cache = OfflineEmbeddingCache.from_vectors(
        {OfflineEmbeddingCache.key(text): [0.0, 1.0], OfflineEmbeddingCache.key("Method"): [0.0, 1.0]}, cache_id="field-fixture"
    )
    index = CachedFixtureFieldIndex([chunk], cache)
    assert index.search_field_vectors("p1", text)[0].chunk_id == "c1"

