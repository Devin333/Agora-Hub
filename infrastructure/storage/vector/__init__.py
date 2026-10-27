"""Vector memory storage boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "DeterministicEmbeddingModel",
    "EmbeddingConfigurationError",
    "EmbeddingModel",
    "EmbeddingProviderError",
    "FakeEmbeddingAdapter",
    "InMemoryVectorStore",
    "OpenAICompatibleEmbeddingConfig",
    "OpenAICompatibleEmbeddingModel",
    "PaperFieldChunkStore",
    "QdrantVectorStore",
    "VectorCollectionStatus",
    "VectorDocument",
    "VectorPayloadIndexStatus",
    "VectorSearchQuery",
    "VectorSearchResult",
    "embedding_model_from_env",
    "qdrant_store_from_env",
]

# The Qdrant-backed exports pull in qdrant-client (about a second of imports), so
# they resolve on first use like every other export. qdrant-client is an optional
# extra: without it these names resolve to stand-ins that explain how to install it.
_QDRANT_EXPORTS = {
    "PaperFieldChunkStore": ("infrastructure.storage.vector.paper_field_chunk_store", "PaperFieldChunkStore"),
    "QdrantVectorStore": ("infrastructure.storage.vector.qdrant_store", "QdrantVectorStore"),
    "qdrant_store_from_env": ("infrastructure.storage.vector.qdrant_store", "qdrant_store_from_env"),
}

_LAZY = LazyExports(
    __name__,
    {
        "DeterministicEmbeddingModel": ("infrastructure.storage.vector.embeddings", "DeterministicEmbeddingModel"),
        "EmbeddingConfigurationError": ("infrastructure.storage.vector.embeddings", "EmbeddingConfigurationError"),
        "EmbeddingModel": ("infrastructure.storage.vector.embeddings", "EmbeddingModel"),
        "EmbeddingProviderError": ("infrastructure.storage.vector.embeddings", "EmbeddingProviderError"),
        "FakeEmbeddingAdapter": ("infrastructure.storage.vector.embeddings", "FakeEmbeddingAdapter"),
        "OpenAICompatibleEmbeddingConfig": ("infrastructure.storage.vector.embeddings", "OpenAICompatibleEmbeddingConfig"),
        "OpenAICompatibleEmbeddingModel": ("infrastructure.storage.vector.embeddings", "OpenAICompatibleEmbeddingModel"),
        "embedding_model_from_env": ("infrastructure.storage.vector.embeddings", "embedding_model_from_env"),
        "InMemoryVectorStore": ("infrastructure.storage.vector.fake_store", "InMemoryVectorStore"),
        "VectorCollectionStatus": ("infrastructure.storage.vector.models", "VectorCollectionStatus"),
        "VectorDocument": ("infrastructure.storage.vector.models", "VectorDocument"),
        "VectorPayloadIndexStatus": ("infrastructure.storage.vector.models", "VectorPayloadIndexStatus"),
        "VectorSearchQuery": ("infrastructure.storage.vector.models", "VectorSearchQuery"),
        "VectorSearchResult": ("infrastructure.storage.vector.models", "VectorSearchResult"),
        **_QDRANT_EXPORTS,
    },
)

_INSTALL_HINT = 'Install with `python -m pip install -e ".[qdrant]"` or `python -m pip install -e ".[dev]"`.'


def _qdrant_unavailable(name: str, error: ModuleNotFoundError) -> Any:
    if name == "qdrant_store_from_env":

        def qdrant_store_from_env(*args: Any, **kwargs: Any) -> Any:
            raise ModuleNotFoundError(f"qdrant-client is required for memory/vector features. {_INSTALL_HINT}") from error

        return qdrant_store_from_env

    def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
        raise ModuleNotFoundError(f"qdrant-client is required for {name}. {_INSTALL_HINT}") from error

    return type(name, (), {"__init__": __init__, "__module__": __name__})


def __getattr__(name: str) -> Any:
    if name in _QDRANT_EXPORTS:
        try:
            return _LAZY.getattr(name)
        except ModuleNotFoundError as exc:
            if not (exc.name or "").startswith("qdrant_client"):
                raise
            value = _qdrant_unavailable(name, exc)
            globals()[name] = value
            return value
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from infrastructure.storage.vector.embeddings import (
        DeterministicEmbeddingModel,
        EmbeddingConfigurationError,
        EmbeddingModel,
        EmbeddingProviderError,
        FakeEmbeddingAdapter,
        OpenAICompatibleEmbeddingConfig,
        OpenAICompatibleEmbeddingModel,
        embedding_model_from_env,
    )
    from infrastructure.storage.vector.fake_store import InMemoryVectorStore
    from infrastructure.storage.vector.models import (
        VectorCollectionStatus,
        VectorDocument,
        VectorPayloadIndexStatus,
        VectorSearchQuery,
        VectorSearchResult,
    )
    from infrastructure.storage.vector.paper_field_chunk_store import PaperFieldChunkStore
    from infrastructure.storage.vector.qdrant_store import QdrantVectorStore, qdrant_store_from_env
