from __future__ import annotations

import os
from collections import OrderedDict
from collections import defaultdict
from threading import RLock
from typing import Any, Mapping, cast
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models

from infrastructure.storage.vector.embeddings import EmbeddingModel, embedding_model_from_env
from infrastructure.storage.vector.models import (
    VectorCollectionStatus,
    VectorDocument,
    VectorPayloadIndexStatus,
    VectorSearchQuery,
    VectorSearchResult,
)


DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_VECTOR_SIZE = 64
DEFAULT_PAYLOAD_INDEXES = {
    "run_id": "keyword",
    "report_id": "keyword",
    "topic": "keyword",
    "source_type": "keyword",
    "category": "keyword",
    "published_at": "datetime",
    "confidence": "float",
    "language": "keyword",
}


class QdrantVectorStore:
    def __init__(
        self,
        client: QdrantClient,
        *,
        embedding_model: EmbeddingModel | None = None,
        vector_size: int = DEFAULT_VECTOR_SIZE,
        query_embedding_cache_size: int = 512,
    ) -> None:
        self.client = client
        self.embedding_model = embedding_model or embedding_model_from_env(vector_size=vector_size)
        self.vector_size = vector_size
        self._query_embedding_cache_size = max(0, int(query_embedding_cache_size))
        self._query_embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._query_embedding_cache_lock = RLock()

    def upsert_documents(self, docs: list[VectorDocument]) -> None:
        grouped: dict[str, list[VectorDocument]] = defaultdict(list)
        for doc in docs:
            grouped[doc.collection].append(doc)

        for collection, collection_docs in grouped.items():
            self._ensure_collection(collection)
            points = []
            embedded_vectors = iter(
                self.embedding_model.embed_texts([doc.text for doc in collection_docs if doc.vector is None])
            )
            for doc in collection_docs:
                vector = doc.vector or next(embedded_vectors)
                points.append(
                    models.PointStruct(
                        id=str(uuid5(NAMESPACE_URL, f"{collection}:{doc.document_id}")),
                        vector=vector,
                        payload=doc.to_payload(),
                    )
                )
            self.client.upsert(collection_name=collection, points=points, wait=True)

    def search(self, query: VectorSearchQuery) -> list[VectorSearchResult]:
        query_vector = query.vector or self._query_embedding(query.text)
        response = self.client.query_points(
            collection_name=query.collection,
            query=query_vector,
            query_filter=_qdrant_filter(query.filters),
            limit=query.limit,
            offset=query.offset,
            with_payload=True,
            score_threshold=query.score_threshold,
        )
        raw_points = getattr(response, "points", response)
        points = cast(Any, raw_points[0] if isinstance(raw_points, tuple) else raw_points)
        return [
            VectorSearchResult.from_payload(score=point.score, payload=dict(point.payload or {}))
            for point in points
        ]

    def clear_query_embedding_cache(self) -> None:
        """Clear cached query vectors after an embedding configuration change."""
        with self._query_embedding_cache_lock:
            self._query_embedding_cache.clear()

    def _query_embedding(self, text: str) -> list[float]:
        normalized_text = str(text or "").strip()
        if self._query_embedding_cache_size <= 0:
            return list(self.embedding_model.embed_text(normalized_text))
        with self._query_embedding_cache_lock:
            cached = self._query_embedding_cache.get(normalized_text)
            if cached is not None:
                self._query_embedding_cache.move_to_end(normalized_text)
                return list(cached)

        vector = list(self.embedding_model.embed_text(normalized_text))
        with self._query_embedding_cache_lock:
            self._query_embedding_cache[normalized_text] = vector
            self._query_embedding_cache.move_to_end(normalized_text)
            while len(self._query_embedding_cache) > self._query_embedding_cache_size:
                self._query_embedding_cache.popitem(last=False)
        return list(vector)

    def get_document(self, collection: str, document_id: str) -> VectorSearchResult | None:
        response = self.client.scroll(
            collection_name=collection,
            scroll_filter=_qdrant_filter({"document_id": document_id}),
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        points = response[0] if isinstance(response, tuple) else getattr(response, "points", response)
        if not points:
            return None
        return VectorSearchResult.from_payload(score=1.0, payload=dict(points[0].payload or {}))

    def list_payloads(
        self,
        collection: str,
        *,
        filters: dict[str, Any] | None = None,
        batch_size: int = 256,
    ) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        offset: Any = None
        while True:
            response = self.client.scroll(
                collection_name=collection,
                scroll_filter=_qdrant_filter(filters or {}),
                limit=batch_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            if isinstance(response, tuple):
                points, offset = response
            else:
                points = getattr(response, "points", response)
                offset = getattr(response, "next_page_offset", None)
            payloads.extend(dict(point.payload or {}) for point in points)
            if offset is None or not points:
                return payloads

    def ensure_collections(self, collections: list[str]) -> list[VectorCollectionStatus]:
        statuses = []
        for collection in collections:
            existed_before = self.client.collection_exists(collection)
            created = False
            if not existed_before:
                self._create_collection(collection)
                created = True
            statuses.append(
                VectorCollectionStatus(
                    collection=collection,
                    vector_size=self.vector_size,
                    existed_before=existed_before,
                    created=created,
                )
            )
        return statuses

    def ensure_payload_indexes(
        self,
        collections: list[str],
        payload_indexes: Mapping[str, str] | None = None,
    ) -> list[VectorPayloadIndexStatus]:
        index_schema = dict(payload_indexes or DEFAULT_PAYLOAD_INDEXES)
        statuses: list[VectorPayloadIndexStatus] = []
        for collection in collections:
            self._ensure_collection(collection)
            existing = _payload_schema_fields(self.client, collection)
            for field_name, field_schema in index_schema.items():
                existed_before = field_name in existing
                created = False
                if not existed_before:
                    self.client.create_payload_index(
                        collection_name=collection,
                        field_name=field_name,
                        field_schema=_payload_schema_type(field_schema),
                        wait=True,
                    )
                    created = True
                statuses.append(
                    VectorPayloadIndexStatus(
                        collection=collection,
                        field_name=field_name,
                        field_schema=field_schema,
                        existed_before=existed_before,
                        created=created,
                    )
                )
        return statuses

    def _ensure_collection(self, collection: str) -> None:
        if self.client.collection_exists(collection):
            return
        self._create_collection(collection)

    def _create_collection(self, collection: str) -> None:
        self.client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=self.vector_size, distance=models.Distance.COSINE),
        )


def qdrant_store_from_env(
    *,
    embedding_model: EmbeddingModel | None = None,
    env: Mapping[str, str] | None = None,
) -> QdrantVectorStore:
    values = env if env is not None else os.environ
    vector_size = _vector_size_from_env(values)
    url = values.get("NEWS_QDRANT_URL", DEFAULT_QDRANT_URL)
    client = QdrantClient(url=url)
    resolved_embedding_model = embedding_model or embedding_model_from_env(env=values, vector_size=vector_size)
    return QdrantVectorStore(client, embedding_model=resolved_embedding_model, vector_size=vector_size)


def _qdrant_filter(filters: dict[str, Any]) -> models.Filter | None:
    if not filters:
        return None
    return models.Filter(
        must=[_qdrant_field_condition(key, value) for key, value in filters.items()]
    )


def _qdrant_field_condition(key: str, value: Any) -> models.FieldCondition:
    if isinstance(value, (list, tuple, set, frozenset)):
        return models.FieldCondition(
            key=key,
            match=models.MatchAny(any=list(value)),
        )
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _vector_size_from_env(values: Mapping[str, str]) -> int:
    if values.get("NEWS_VECTOR_SIZE"):
        return int(values["NEWS_VECTOR_SIZE"])
    if values.get("NEWS_EMBEDDING_DIMENSIONS"):
        return int(values["NEWS_EMBEDDING_DIMENSIONS"])
    return DEFAULT_VECTOR_SIZE


def _payload_schema_fields(client: QdrantClient, collection: str) -> set[str]:
    try:
        info = client.get_collection(collection)
    except Exception:
        return set()
    payload_schema = getattr(info, "payload_schema", None)
    if isinstance(payload_schema, Mapping):
        return {str(key) for key in payload_schema}
    return set()


def _payload_schema_type(value: str) -> models.PayloadSchemaType:
    normalized = value.strip().lower()
    mapping = {
        "bool": models.PayloadSchemaType.BOOL,
        "datetime": models.PayloadSchemaType.DATETIME,
        "float": models.PayloadSchemaType.FLOAT,
        "integer": models.PayloadSchemaType.INTEGER,
        "keyword": models.PayloadSchemaType.KEYWORD,
        "text": models.PayloadSchemaType.TEXT,
        "uuid": models.PayloadSchemaType.UUID,
    }
    if normalized not in mapping:
        raise ValueError(f"unsupported Qdrant payload index schema: {value}")
    return mapping[normalized]
