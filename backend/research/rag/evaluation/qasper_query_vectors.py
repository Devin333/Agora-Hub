"""Resolve QASPER query vectors: base cache, then supplementary cache, then live embedding.

Chunk and field vectors stay strictly offline. Query texts rewritten by the retrieval
planner are absent from the frozen cache, so they are embedded once with the same
model, normalized like the cache rows, and persisted for reuse.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from backend.research.ports.qasper_query_embedding import (
    QueryEmbeddingPort,
    QueryVectorSnapshot,
    QueryVectorStorePort,
)
from backend.research.rag.evaluation.qasper_retrieval import OfflineEmbeddingCache


class QueryVectorError(RuntimeError):
    """A query vector cannot be resolved without breaking cache identity."""


@dataclass
class QueryVectorStats:
    base_hits: int = 0
    supplementary_hits: int = 0
    live_calls: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "base_hits": self.base_hits,
            "supplementary_hits": self.supplementary_hits,
            "live_calls": self.live_calls,
        }


class QueryVectorResolver:
    """Callable ``query_text -> vector`` for the cached fixture store and field index."""

    def __init__(
        self,
        cache: OfflineEmbeddingCache,
        *,
        embedder: QueryEmbeddingPort,
        store: QueryVectorStorePort,
        model_id: str,
    ) -> None:
        dimension = int(cache.matrix.shape[1])
        if int(embedder.dimension) != dimension:
            raise QueryVectorError(
                f"embedder dimension {embedder.dimension} does not match cache dimension {dimension}"
            )
        if embedder.model_id != model_id:
            raise QueryVectorError(f"embedder model {embedder.model_id!r} does not match cache model {model_id!r}")
        self._cache = cache
        self._embedder = embedder
        self._store = store
        self._model_id = model_id
        self._dimension = dimension
        self._supplementary: dict[str, np.ndarray] = {}
        self._dirty = False
        self.stats = QueryVectorStats()
        snapshot = store.load()
        if snapshot is not None:
            self._adopt(snapshot)

    def __call__(self, query_text: str) -> np.ndarray:
        key = OfflineEmbeddingCache.key(query_text)
        if key in self._cache.key_to_row:
            self.stats.base_hits += 1
            return self._cache.vector_for(query_text)
        cached = self._supplementary.get(key)
        if cached is not None:
            self.stats.supplementary_hits += 1
            return cached
        vectors = self._embedder.embed_queries([query_text])
        if len(vectors) != 1:
            raise QueryVectorError(f"embedder returned {len(vectors)} vectors for one query")
        vector = self._normalized(vectors[0])
        self._supplementary[key] = vector
        self._dirty = True
        self.stats.live_calls += 1
        return vector

    @property
    def supplementary_size(self) -> int:
        return len(self._supplementary)

    def flush(self) -> None:
        """Persist newly embedded query vectors; a no-op when nothing changed."""
        if not self._dirty:
            return
        self._store.save(
            QueryVectorSnapshot(
                base_cache_sha256=self._cache.cache_id,
                model_id=self._model_id,
                dimension=self._dimension,
                vectors={key: tuple(float(x) for x in vector) for key, vector in self._supplementary.items()},
            )
        )
        self._dirty = False

    def _adopt(self, snapshot: QueryVectorSnapshot) -> None:
        if snapshot.base_cache_sha256 != self._cache.cache_id:
            raise QueryVectorError("supplementary query vectors were built for a different base cache")
        if snapshot.model_id != self._model_id or int(snapshot.dimension) != self._dimension:
            raise QueryVectorError("supplementary query vectors were built with a different embedding model")
        for key, values in snapshot.vectors.items():
            vector = np.asarray(values, dtype=np.float32)
            if vector.shape != (self._dimension,) or not np.isfinite(vector).all():
                raise QueryVectorError(f"supplementary query vector {key} is malformed")
            vector.setflags(write=False)
            self._supplementary[key] = vector

    def _normalized(self, values: list[float]) -> np.ndarray:
        vector = np.asarray(values, dtype=np.float32)
        if vector.shape != (self._dimension,):
            raise QueryVectorError(f"live query vector has shape {vector.shape}, expected ({self._dimension},)")
        if not np.isfinite(vector).all():
            raise QueryVectorError("live query vector contains non-finite values")
        norm = float(np.linalg.norm(vector))
        if norm == 0.0 or not math.isfinite(norm):
            raise QueryVectorError("live query vector has zero norm")
        vector = (vector / norm).astype(np.float32)
        vector.setflags(write=False)
        return vector


__all__ = ["QueryVectorError", "QueryVectorResolver", "QueryVectorStats"]
