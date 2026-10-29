from __future__ import annotations

import json

import pytest

from backend.research.application.qasper_query_vector_backfill import (
    QasperQueryVectorBackfillError,
    backfill_replay_query_vectors,
)
from backend.research.ports.qasper_query_embedding import QueryVectorSnapshot
from backend.research.rag.evaluation.qasper_query_vectors import QueryVectorResolver
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache

MODEL = "dashscope/text-embedding-v4"


class _Embedder:
    model_id = MODEL
    dimension = 2

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_queries(self, texts):
        self.calls.append(list(texts))
        return [[3.0, 4.0] for _ in texts]


class _Store:
    def __init__(self, snapshot: QueryVectorSnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.saves = 0

    def load(self):
        return self.snapshot

    def save(self, snapshot):
        self.snapshot = snapshot
        self.saves += 1


class _Artifacts:
    def __init__(self, files: dict[str, dict]) -> None:
        self.files = {name: json.dumps(value).encode() for name, value in files.items()}

    def read(self, directory: str, name: str) -> bytes:
        assert directory == "blocked"
        return self.files[name]

    def publish(self, directory, objects):  # pragma: no cover - backfill never publishes
        raise AssertionError("backfill must not publish replay artifacts")


def _cache() -> OfflineEmbeddingCache:
    return OfflineEmbeddingCache.from_vectors(
        {OfflineEmbeddingCache.key("chunk body"): [1.0, 0.0]}, cache_id="base",
    )


def _missing(text: str) -> dict:
    return {
        "query_digest": OfflineEmbeddingCache.key(text),
        "query_text": text,
        "query_kinds": ["controlled_replan_or_pipeline_rewrite"],
        "retrieval_channels": ["dense_chunk"],
        "affected_profiles": ["expanded_chunk"],
        "affected_case_ids": ["paper:q"],
        "occurrences": 1,
    }


def _blocked(*texts: str, code: str = "query_vectors_missing_during_replay", cache_sha: str = "base"):
    return _Artifacts({
        "report.json": {
            "protocol": "qasper_retrieval_only_replay_v1",
            "status": "blocked",
            "blocked": {"code": code, "reason": "missing"},
        },
        "manifest.json": {
            "identity": {"embedding_cache_sha256": cache_sha},
            "missing_query_vectors": [_missing(text) for text in texts],
        },
    })


def _resolver(embedder: _Embedder, store: _Store) -> QueryVectorResolver:
    return QueryVectorResolver(_cache(), embedder=embedder, store=store, model_id=MODEL)


def test_backfill_embeds_exactly_the_reported_queries_and_persists_them() -> None:
    embedder, store = _Embedder(), _Store()

    result = backfill_replay_query_vectors(
        _blocked("What is the performance of their method?", "Which datasets are used?"),
        blocked_replay_directory="blocked",
        resolver=_resolver(embedder, store),
        base_cache_sha256="base",
    )

    assert embedder.calls == [["What is the performance of their method?"], ["Which datasets are used?"]]
    assert [row["query_text"] for row in result["embedded"]] == [
        "What is the performance of their method?", "Which datasets are used?",
    ]
    assert result["already_present"] == 0 and result["requested"] == 2
    assert store.saves == 1
    assert set(store.snapshot.vectors) == {
        OfflineEmbeddingCache.key("What is the performance of their method?"),
        OfflineEmbeddingCache.key("Which datasets are used?"),
    }


def test_backfill_skips_queries_already_in_the_supplementary_cache() -> None:
    text = "What is the performance of their method?"
    snapshot = QueryVectorSnapshot(
        base_cache_sha256="base", model_id=MODEL, dimension=2,
        vectors={OfflineEmbeddingCache.key(text): (0.6, 0.8)},
    )
    embedder, store = _Embedder(), _Store(snapshot)

    result = backfill_replay_query_vectors(
        _blocked(text), blocked_replay_directory="blocked",
        resolver=_resolver(embedder, store), base_cache_sha256="base",
    )

    assert embedder.calls == []
    assert result["embedded"] == [] and result["already_present"] == 1
    assert store.saves == 0


@pytest.mark.parametrize(
    ("artifacts", "message"),
    [
        (_blocked("q", code="source_identity_mismatch"), "cannot fix"),
        (_blocked("q", cache_sha="other"), "different embedding cache"),
        (_blocked(), "lists no missing"),
    ],
)
def test_backfill_rejects_blocks_query_vectors_cannot_satisfy(artifacts, message) -> None:
    embedder = _Embedder()
    with pytest.raises(QasperQueryVectorBackfillError, match=message):
        backfill_replay_query_vectors(
            artifacts, blocked_replay_directory="blocked",
            resolver=_resolver(embedder, _Store()), base_cache_sha256="base",
        )
    assert embedder.calls == []


def test_backfill_rejects_a_query_text_that_does_not_match_its_digest() -> None:
    artifacts = _blocked("original question")
    manifest = json.loads(artifacts.files["manifest.json"])
    manifest["missing_query_vectors"][0]["query_text"] = "tampered question"
    artifacts.files["manifest.json"] = json.dumps(manifest).encode()
    embedder = _Embedder()

    with pytest.raises(QasperQueryVectorBackfillError, match="digest"):
        backfill_replay_query_vectors(
            artifacts, blocked_replay_directory="blocked",
            resolver=_resolver(embedder, _Store()), base_cache_sha256="base",
        )
    assert embedder.calls == []


def test_backfill_refuses_a_completed_replay() -> None:
    artifacts = _blocked("q")
    report = json.loads(artifacts.files["report.json"])
    report["status"] = "completed"
    artifacts.files["report.json"] = json.dumps(report).encode()

    with pytest.raises(QasperQueryVectorBackfillError, match="not a blocked"):
        backfill_replay_query_vectors(
            artifacts, blocked_replay_directory="blocked",
            resolver=_resolver(_Embedder(), _Store()), base_cache_sha256="base",
        )
