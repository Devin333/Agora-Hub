from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from infrastructure.external.visual_embeddings import visual_embedding_model_from_env
from infrastructure.storage.vector.embeddings import DeterministicEmbeddingModel
from infrastructure.storage.vector.models import VectorDocument, VectorSearchQuery
from infrastructure.storage.vector.qdrant_store import DEFAULT_QDRANT_URL, QdrantVectorStore

PAPER_VISUAL_CHUNKS_COLLECTION = "paper_visual_chunks"

_PAYLOAD_INDEXES: dict[str, str] = {
    "paper_id": "keyword",
    "chunk_id": "keyword",
    "chunk_type": "keyword",
    "image_ref": "keyword",
    "figure_id": "keyword",
    "section_index": "integer",
}


@runtime_checkable
class VisualEmbeddingPort(Protocol):
    dimension: int

    def embed_text(self, text: str) -> list[float]: ...

    def embed_image(self, image_path: str) -> list[float]: ...

    def embed_images(self, image_paths: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class VisualChunkHit:
    chunk_id: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class PaperVisualChunkStore:
    """
    Vector store for visual paper chunks.

    The collection stores image embeddings only. Search uses a multimodal text
    embedding from the same provider, then the business retriever resolves the
    returned chunk_id against the main chunk store.
    """

    def __init__(
        self,
        vector_store: Any,
        visual_embedding_model: VisualEmbeddingPort,
        *,
        image_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self._store = vector_store
        self._visual_embedding_model = visual_embedding_model
        self._image_root = Path(image_root) if image_root else None

    def ensure_collection(self) -> None:
        self._store.ensure_collections([PAPER_VISUAL_CHUNKS_COLLECTION])
        self._store.ensure_payload_indexes([PAPER_VISUAL_CHUNKS_COLLECTION], _PAYLOAD_INDEXES)

    def index_chunks(self, chunks: list[Any]) -> None:
        visual_chunks = [chunk for chunk in chunks if _should_index_visual_chunk(chunk)]
        if not visual_chunks:
            return

        docs: list[VectorDocument] = []
        for chunk in visual_chunks:
            image_ref = str(chunk.metadata.get("image_ref") or "")
            image_path = self._resolve_image_path(image_ref, paper_id=chunk.paper_id)
            if not image_path.exists():
                logging.getLogger(__name__).warning(
                    "visual chunk image missing, skipped: %s", image_path
                )
                continue
            vector = self._visual_embedding_model.embed_image(str(image_path))
            docs.append(_chunk_to_visual_doc(chunk, vector=vector, image_ref=image_ref))

        if docs:
            self._store.upsert_documents(docs)

    def delete_paper_chunks(self, paper_id: str) -> None:
        if hasattr(self._store, "delete_by_filter"):
            self._store.delete_by_filter(
                PAPER_VISUAL_CHUNKS_COLLECTION,
                {"paper_id": paper_id},
            )
            return

        from qdrant_client import models as qmodels

        self._store.client.delete(
            collection_name=PAPER_VISUAL_CHUNKS_COLLECTION,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="paper_id",
                            match=qmodels.MatchValue(value=paper_id),
                        )
                    ]
                )
            ),
        )

    def search_visual_chunks(
        self,
        paper_id: str,
        query_text: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
    ) -> list[VisualChunkHit]:
        query_vector = self._visual_embedding_model.embed_text(query_text)
        combined = {"paper_id": paper_id, **(filters or {})}
        results = self._store.search(
            VectorSearchQuery(
                collection=PAPER_VISUAL_CHUNKS_COLLECTION,
                text=query_text,
                vector=query_vector,
                filters=combined,
                limit=limit,
            )
        )
        hits: list[VisualChunkHit] = []
        for result in results:
            chunk_id = result.payload.get("chunk_id")
            if chunk_id:
                hits.append(
                    VisualChunkHit(
                        chunk_id=str(chunk_id),
                        score=float(result.score),
                        metadata=dict(result.payload),
                    )
                )
        return hits

    def _resolve_image_path(self, image_ref: str, *, paper_id: str = "") -> Path:
        normalized = image_ref.replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute():
            return path

        candidates = []
        if self._image_root is not None:
            candidates.extend([
                self._image_root / path,
                self._image_root / paper_id / path,
            ])
            if path.parts and path.parts[0] == ".newsroom":
                candidates.append(Path.cwd() / path)
            newsroom_index = normalized.find(".newsroom/")
            if newsroom_index > 0:
                candidates.append(Path.cwd() / normalized[newsroom_index:])
        candidates.append(Path.cwd() / path)

        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0]


def paper_visual_chunk_store_from_env(
    *,
    env: Mapping[str, str] | None = None,
) -> PaperVisualChunkStore | None:
    values = env if env is not None else os.environ
    visual_model = visual_embedding_model_from_env(env=values)
    if visual_model is None:
        return None

    from infrastructure.storage.vector.connection import connect_qdrant

    url = values.get("NEWS_VISUAL_QDRANT_URL") or values.get("NEWS_QDRANT_URL") or DEFAULT_QDRANT_URL
    vector_store = QdrantVectorStore(
        connect_qdrant(url),
        embedding_model=DeterministicEmbeddingModel(dimension=visual_model.dimension),
        vector_size=visual_model.dimension,
    )
    return PaperVisualChunkStore(
        vector_store,
        visual_model,
        image_root=values.get("NEWS_VISUAL_IMAGE_ROOT") or None,
    )


def _should_index_visual_chunk(chunk: Any) -> bool:
    return chunk.chunk_type == "figure" and bool(chunk.metadata.get("image_ref"))


def _chunk_to_visual_doc(
    chunk: Any,
    *,
    vector: list[float],
    image_ref: str,
) -> VectorDocument:
    payload = {
        "chunk_id": chunk.chunk_id,
        "paper_id": chunk.paper_id,
        "chunk_type": chunk.chunk_type,
        "content": chunk.content,
        "section_title": chunk.section_title,
        "section_index": chunk.section_index,
        "figure_id": chunk.figure_id,
        "image_ref": image_ref,
        "source_locator": chunk.metadata.get("source_locator", ""),
        "caption_source_locator": chunk.metadata.get("caption_source_locator", ""),
        "pdf_rect": chunk.metadata.get("pdf_rect"),
        "caption_pdf_rect": chunk.metadata.get("caption_pdf_rect"),
        "content_sources": chunk.metadata.get("content_sources", []),
        "visual_indexed": True,
    }
    return VectorDocument(
        document_id=chunk.chunk_id,
        collection=PAPER_VISUAL_CHUNKS_COLLECTION,
        text=chunk.content,
        payload=payload,
        source_type="paper_visual_chunk",
        vector=vector,
        topic=chunk.paper_id,
        section_id=chunk.chunk_id,
    )


__all__ = [
    "PAPER_VISUAL_CHUNKS_COLLECTION",
    "PaperVisualChunkStore",
    "paper_visual_chunk_store_from_env",
]
