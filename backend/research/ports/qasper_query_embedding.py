"""Ports for embedding QASPER evaluation queries that the offline cache lacks.

Chunk and field vectors always come from the frozen offline cache. Only query texts
the retrieval planner rewrites may be embedded live, and every live vector is
persisted so a rerun never pays for the same query twice. Only stdlib types cross
this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable


@runtime_checkable
class QueryEmbeddingPort(Protocol):
    """Embeds query texts with the same model and dimension as the offline cache."""

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one vector per text, in order; raise on any provider failure."""
        ...


@dataclass(frozen=True)
class QueryVectorSnapshot:
    """Supplementary query vectors bound to the base cache they extend."""

    base_cache_sha256: str
    model_id: str
    dimension: int
    vectors: Mapping[str, tuple[float, ...]] = field(default_factory=dict)


@runtime_checkable
class QueryVectorStorePort(Protocol):
    """Durable storage for supplementary query vectors."""

    def load(self) -> QueryVectorSnapshot | None:
        """Return the stored snapshot, or None when nothing has been stored yet."""
        ...

    def save(self, snapshot: QueryVectorSnapshot) -> None:
        """Atomically replace the stored snapshot."""
        ...


__all__ = ["QueryEmbeddingPort", "QueryVectorSnapshot", "QueryVectorStorePort"]
