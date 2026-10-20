from __future__ import annotations

import math
from typing import Any

from infrastructure.storage.vector.embeddings import FakeEmbeddingAdapter
from infrastructure.storage.vector.models import (
    VectorCollectionStatus,
    VectorDocument,
    VectorSearchQuery,
    VectorSearchResult,
)


class InMemoryVectorStore:
    def __init__(self, embedding_model: FakeEmbeddingAdapter | None = None) -> None:
        self.embedding_model = embedding_model or FakeEmbeddingAdapter()
        self._collections: dict[str, dict[str, VectorDocument]] = {}

    def upsert_documents(self, docs: list[VectorDocument]) -> None:
        for doc in docs:
            stored = doc if doc.vector is not None else doc.with_vector(self.embedding_model.embed_text(doc.text))
            self._collections.setdefault(stored.collection, {})[stored.document_id] = stored

    def ensure_collections(self, collections: list[str]) -> list[VectorCollectionStatus]:
        statuses = []
        for collection in collections:
            existed_before = collection in self._collections
            if not existed_before:
                self._collections[collection] = {}
            statuses.append(
                VectorCollectionStatus(
                    collection=collection,
                    vector_size=self.embedding_model.dimension,
                    existed_before=existed_before,
                    created=not existed_before,
                )
            )
        return statuses

    def search(self, query: VectorSearchQuery) -> list[VectorSearchResult]:
        query_vector = query.vector or self.embedding_model.embed_text(query.text)
        scored: list[VectorSearchResult] = []
        for doc in self._collections.get(query.collection, {}).values():
            if doc.vector is None or not _matches_filters(doc, query.filters):
                continue
            score = _cosine_similarity(query_vector, doc.vector)
            if query.score_threshold is not None and score < query.score_threshold:
                continue
            scored.append(VectorSearchResult.from_payload(score=score, payload=doc.to_payload()))
        scored.sort(key=lambda result: result.score, reverse=True)
        return scored[query.offset : query.offset + query.limit]

    def ensure_payload_indexes(self, collections: list[str], payload_indexes=None) -> list:
        return []

    def get_document(self, collection: str, document_id: str) -> VectorSearchResult | None:
        doc = self._collections.get(collection, {}).get(document_id)
        if doc is None:
            return None
        return VectorSearchResult.from_payload(score=1.0, payload=doc.to_payload())

    def list_payloads(
        self,
        collection: str,
        *,
        filters: dict[str, Any] | None = None,
        batch_size: int = 256,
    ) -> list[dict[str, Any]]:
        return [
            doc.to_payload()
            for doc in self._collections.get(collection, {}).values()
            if _matches_filters(doc, filters or {})
        ]

    def delete_by_filter(self, collection: str, filters: dict[str, Any]) -> int:
        docs = self._collections.get(collection, {})
        matching_ids = [
            document_id for document_id, doc in docs.items() if _matches_filters(doc, filters)
        ]
        for document_id in matching_ids:
            del docs[document_id]
        return len(matching_ids)


def _matches_filters(doc: VectorDocument, filters: dict[str, Any]) -> bool:
    payload = doc.to_payload()
    for key, expected in filters.items():
        actual = payload.get(key)
        if isinstance(expected, (list, tuple, set, frozenset)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot_product = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot_product / (left_norm * right_norm)
