from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

import numpy as np

from backend.research.document.models import PaperChunk


class QasperOfflineCacheError(RuntimeError):
    """Raised when a fixture run would need an embedding absent from its cache."""


@dataclass(frozen=True)
class OfflineEmbeddingCache:
    """Read-only float32 matrix plus compact text-key-to-row mapping."""

    key_to_row: Mapping[str, int]
    matrix: np.ndarray
    cache_id: str

    def __post_init__(self) -> None:
        if self.matrix.dtype != np.float32 or self.matrix.ndim != 2:
            raise ValueError("embedding cache matrix must be a two-dimensional float32 array")
        if len(self.key_to_row) != self.matrix.shape[0]:
            raise ValueError("embedding cache keys/vectors have inconsistent shapes")
        if not np.isfinite(self.matrix).all():
            raise ValueError("embedding cache contains non-finite values")
        self.matrix.setflags(write=False)
        object.__setattr__(self, "key_to_row", MappingProxyType(dict(self.key_to_row)))

    @classmethod
    def load(cls, path: Path) -> "OfflineEmbeddingCache":
        path = Path(path)
        if not path.is_file():
            raise QasperOfflineCacheError(f"embedding cache does not exist: {path}")
        try:
            with np.load(path, allow_pickle=False) as data:
                keys = [str(key) for key in data["keys"].tolist()]
                matrix = np.asarray(data["vectors"], dtype=np.float32)
        except (OSError, ValueError, KeyError) as exc:
            raise QasperOfflineCacheError(f"invalid embedding cache: {path}") from exc
        if matrix.ndim != 2 or len(keys) != matrix.shape[0] or matrix.shape[1] == 0:
            raise QasperOfflineCacheError("embedding cache keys/vectors have inconsistent shapes")
        if any(not key for key in keys) or len(set(keys)) != len(keys):
            raise QasperOfflineCacheError("embedding cache contains an empty or duplicate key")
        if not np.isfinite(matrix).all():
            raise QasperOfflineCacheError("embedding cache contains non-finite values")
        hasher = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(block)
        return cls(key_to_row={key: row for row, key in enumerate(keys)}, matrix=matrix,
                   cache_id=hasher.hexdigest())

    @classmethod
    def from_vectors(cls, vectors: Mapping[str, Sequence[float]], *, cache_id: str) -> "OfflineEmbeddingCache":
        if not cache_id.strip():
            raise ValueError("cache_id must be non-empty")
        keys = list(vectors)
        if any(not key for key in keys):
            raise ValueError("cache keys must be non-empty")
        try:
            matrix = np.asarray([vectors[key] for key in keys], dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("cache vectors must be finite, non-empty and have the same dimension") from exc
        if not keys:
            matrix = np.empty((0, 0), dtype=np.float32)
        if matrix.ndim != 2 or (keys and matrix.shape[1] == 0) or not np.isfinite(matrix).all():
            raise ValueError("cache vectors must be finite, non-empty and have the same dimension")
        return cls(key_to_row={key: row for row, key in enumerate(keys)}, matrix=matrix, cache_id=cache_id)

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    def row_for(self, text: str) -> int:
        key = self.key(text)
        try:
            return self.key_to_row[key]
        except KeyError as exc:
            raise QasperOfflineCacheError(
                f"embedding cache key missing for text digest {key}; offline evaluation never calls an embedding provider"
            ) from exc

    def vector_for(self, text: str) -> np.ndarray:
        """Return a zero-copy read-only row view from the cache matrix."""
        vector = self.matrix[self.row_for(text)]
        vector.flags.writeable = False
        return vector

    def require_texts(self, texts: Sequence[str]) -> None:
        missing = sorted({self.key(text) for text in texts} - self.key_to_row.keys())
        if missing:
            raise QasperOfflineCacheError(
                f"embedding cache is missing {len(missing)} required key(s): {', '.join(missing[:5])}"
            )


class CachedFixtureChunkStore:
    """Read-only NumPy fixture store compatible with production DenseTextChannel."""

    def __init__(self, chunks: Sequence[PaperChunk], cache: OfflineEmbeddingCache) -> None:
        self._chunks: dict[str, PaperChunk] = {}
        row_ids: list[int] = []
        for chunk in chunks:
            if not isinstance(chunk, PaperChunk):
                raise TypeError("fixture store accepts PaperChunk instances only")
            if chunk.chunk_id in self._chunks:
                raise ValueError(f"duplicate fixture chunk id: {chunk.chunk_id}")
            self._chunks[chunk.chunk_id] = chunk
            row_ids.append(cache.row_for(chunk.content))
        self._vector_rows = np.asarray(row_ids, dtype=np.intp)
        self._ordered_ids = tuple(self._chunks)
        self._cache = cache

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        return self._chunks.get(chunk_id)

    def list_chunks(self, paper_id: str) -> list[PaperChunk]:
        return [chunk for chunk in self._chunks.values() if chunk.paper_id == paper_id]

    def search_with_scores(self, paper_id: str, query_text: str, *, filters=None, limit=10):
        from backend.research.rag.retrieval.channels.sparse_lexical import chunk_matches_filters

        if limit <= 0:
            return []
        query = self._cache.vector_for(query_text)
        selected = [
            index for index, chunk_id in enumerate(self._ordered_ids)
            if self._chunks[chunk_id].paper_id == paper_id
            and chunk_matches_filters(self._chunks[chunk_id], filters or {})
        ]
        if not selected:
            return []
        vector_rows = self._vector_rows[selected]
        if query.shape[0] != self._cache.matrix.shape[1]:
            raise QasperOfflineCacheError("query and chunk embedding dimensions differ")
        scores = self._cache.matrix[vector_rows] @ query
        order = np.argsort(-scores, kind="stable")[: min(limit, len(selected))]
        return [(self._chunks[self._ordered_ids[selected[int(i)]]], float(scores[int(i)])) for i in order]


class CachedFixtureFieldIndex:
    """Read-only field vectors backed by the same compact offline cache matrix."""

    def __init__(self, chunks: Sequence[PaperChunk], cache: OfflineEmbeddingCache) -> None:
        from backend.research.rag.adapters.paper_field_text import extract_field_texts

        self._rows: list[tuple[PaperChunk, str, str, int]] = []
        self._cache = cache
        for chunk in chunks:
            for name, text in extract_field_texts(chunk).non_empty().items():
                text = str(text)
                self._rows.append((chunk, name, text, cache.row_for(text)))

    def search_field_vectors(self, paper_id: str, query_text: str, *, field_names=None, filters=None, limit=10):
        from backend.research.ports.field_embedding_index import FieldEmbeddingHit
        from backend.research.rag.retrieval.channels.sparse_lexical import chunk_matches_filters

        if limit <= 0:
            return []
        query = self._cache.vector_for(query_text)
        allowed = set(field_names) if field_names is not None else None
        eligible = [(chunk, name, text, row) for chunk, name, text, row in self._rows
                    if chunk.paper_id == paper_id and (allowed is None or name in allowed)
                    and chunk_matches_filters(chunk, filters or {})]
        if not eligible:
            return []
        row_ids = np.fromiter((row for _, _, _, row in eligible), dtype=np.intp)
        if query.shape[0] != self._cache.matrix.shape[1]:
            raise QasperOfflineCacheError("query and field embedding dimensions differ")
        scores = self._cache.matrix[row_ids] @ query
        order = np.argsort(-scores, kind="stable")[: min(limit, len(eligible))]
        return [
            FieldEmbeddingHit(eligible[int(i)][0].chunk_id, eligible[int(i)][1], float(scores[int(i)]),
                              eligible[int(i)][2], dict(eligible[int(i)][0].metadata))
            for i in order
        ]


@dataclass(frozen=True)
class RetrievalChannelInputs:
    """Fixture-owned ports; retrieval channel behavior remains in production code."""

    chunk_store: object
    field_index: object | None = None


__all__ = [
    "OfflineEmbeddingCache", "QasperOfflineCacheError", "CachedFixtureChunkStore",
    "CachedFixtureFieldIndex", "RetrievalChannelInputs",
]
