from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Protocol

from framework.tool.models import ToolDefinition
from framework.tool.registry import ToolRegistry
from framework.tool.runtime.errors import effect_not_applied_on
from infrastructure.storage.vector import VectorDocument, VectorSearchQuery, VectorSearchResult

# Vector store calls get their own bound instead of the 30s policy default.
QDRANT_TOOL_TIMEOUT_SECONDS = 30.0
MAX_UPSERT_DOCUMENTS = 100


class QdrantSearchStore(Protocol):
    def search(self, query: VectorSearchQuery) -> list[VectorSearchResult]: ...


class QdrantDocumentStore(Protocol):
    def upsert_documents(self, docs: list[VectorDocument]) -> None: ...


def register_qdrant_tools(
    registry: ToolRegistry,
    *,
    vector_store: QdrantSearchStore,
    document_store: QdrantDocumentStore | None = None,
    allowed_collections: Sequence[str] = (),
) -> None:
    """Register Qdrant search and upsert, limited to ``allowed_collections``.

    The store serves every namespace, agent memory included. Without an
    allowlist a model could read any collection, or write into memory and skip
    memory admission, so both tools refuse every call until collections are
    configured.
    """

    collections = _allowed_collections(allowed_collections)
    registry.register(
        ToolDefinition(
            name="qdrant.search",
            description="Search a Qdrant vector collection through the configured vector store.",
            input_schema={
                "required": ["collection"],
                "properties": {
                    "collection": {"type": "string"},
                    "query": {"type": "string"},
                    "vector": {"type": "array"},
                    "filters": {"type": "object"},
                    "limit": {"type": "integer"},
                    "score_threshold": {"type": "number"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
            timeout_seconds=QDRANT_TOOL_TIMEOUT_SECONDS,
            max_result_bytes=500_000,
            metadata={"storage_backend": "qdrant"},
        ),
        lambda args: _search_qdrant(args, vector_store=vector_store, allowed_collections=collections),
    )
    if document_store is not None:
        registry.register(
            ToolDefinition(
                name="qdrant.upsert",
                description="Upsert structured vector documents through the configured Qdrant store.",
                input_schema={
                    "required": ["documents"],
                    "properties": {
                        "collection": {"type": "string"},
                        "documents": {"type": "array"},
                    },
                    "additionalProperties": False,
                },
                side_effect="writes_external_state",
                concurrency_safe=False,
                timeout_seconds=QDRANT_TOOL_TIMEOUT_SECONDS,
                max_result_bytes=100_000,
                metadata={"storage_backend": "qdrant", "writes_vector_memory": True},
            ),
            lambda args: _upsert_qdrant(args, document_store=document_store, allowed_collections=collections),
        )


def _search_qdrant(
    args: dict[str, Any],
    *,
    vector_store: QdrantSearchStore,
    allowed_collections: tuple[str, ...],
) -> dict[str, Any]:
    collection = _allowed_collection(args["collection"], allowed_collections)

    query_text = str(args.get("query") or "").strip()
    vector = _optional_vector(args.get("vector"))
    if not query_text and vector is None:
        raise ValueError("query or vector is required")

    filters = args.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")

    limit = _limit(args.get("limit"))
    score_threshold = args.get("score_threshold")
    vector_query = VectorSearchQuery(
        collection=collection,
        text=query_text,
        vector=vector,
        filters=dict(filters),
        limit=limit,
        score_threshold=float(score_threshold) if score_threshold is not None else None,
    )
    results = vector_store.search(vector_query)
    return {
        "collection": collection,
        "query": query_text or None,
        "vector_dimensions": len(vector) if vector is not None else None,
        "filters": dict(filters),
        "limit": limit,
        "result_count": len(results),
        "results": [result.to_dict() for result in results],
    }


def _upsert_qdrant(
    args: dict[str, Any],
    *,
    document_store: QdrantDocumentStore,
    allowed_collections: tuple[str, ...],
) -> dict[str, Any]:
    with effect_not_applied_on():
        default_collection = _optional_text(args.get("collection"))
        documents = args["documents"]
        if not isinstance(documents, list):
            raise ValueError("documents must be an array")
        if not documents:
            raise ValueError("documents must not be empty")
        if len(documents) > MAX_UPSERT_DOCUMENTS:
            raise ValueError(f"documents must not contain more than {MAX_UPSERT_DOCUMENTS} items")

        vector_documents = [
            _vector_document(document, default_collection=default_collection)
            for document in documents
        ]
        for document in vector_documents:
            _allowed_collection(document.collection, allowed_collections)
    document_store.upsert_documents(vector_documents)
    return {
        "documents_upserted": len(vector_documents),
        "collections": sorted({document.collection for document in vector_documents}),
        "document_ids": [document.document_id for document in vector_documents],
    }


def _vector_document(value: Any, *, default_collection: str | None) -> VectorDocument:
    if not isinstance(value, dict):
        raise ValueError("each document must be an object")
    document_id = _required_text(value.get("document_id"), "document_id")
    collection = _optional_text(value.get("collection")) or default_collection
    if not collection:
        raise ValueError("document collection is required")
    text = _required_text(value.get("text"), "text")
    source_type = _required_text(value.get("source_type"), "source_type")
    payload = value.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("document payload must be an object")
    refs = value.get("refs") or {}
    if not isinstance(refs, dict):
        raise ValueError("document refs must be an object")
    payload.setdefault("refs", dict(refs))
    for key, ref_value in refs.items():
        payload.setdefault(str(key), ref_value)
    vector = _optional_vector(value.get("vector"))
    return VectorDocument(
        document_id=document_id,
        collection=collection,
        text=text,
        payload=dict(payload),
        source_type=source_type,
        vector=vector,
        run_id=_optional_text(value.get("run_id") or refs.get("run_id")),
    )


def _optional_vector(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("vector must be an array")
    vector = [float(item) for item in value]
    if not vector:
        raise ValueError("vector must not be empty")
    if not all(math.isfinite(item) for item in vector):
        raise ValueError("vector values must be finite numbers")
    return vector


def _allowed_collections(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise TypeError("allowed_collections must be a sequence of collection names")
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _allowed_collection(value: Any, allowed_collections: tuple[str, ...]) -> str:
    collection = _required_text(value, "collection")
    if not allowed_collections:
        raise ValueError("qdrant tools have no allowed collections configured")
    if collection not in allowed_collections:
        raise ValueError(f"collection is not allowed for qdrant tools: {collection}")
    return collection


def _required_text(value: Any, field_name: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _limit(value: Any) -> int:
    if value is None:
        return 10
    return max(1, min(int(value), 100))

