"""Live query embedding for QASPER evaluation, backed by the configured embedding provider."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from infrastructure.storage.vector.embeddings import (
    OpenAICompatibleEmbeddingConfig,
    OpenAICompatibleEmbeddingModel,
)


class ProviderQueryEmbedder:
    """Implements ``QueryEmbeddingPort`` with the same model that built the offline cache."""

    def __init__(self, config: OpenAICompatibleEmbeddingConfig, *, env: Mapping[str, str] | None = None) -> None:
        self._config = config
        self._model = OpenAICompatibleEmbeddingModel(config, env=env)

    @classmethod
    def from_env(cls, *, vector_size: int, env: Mapping[str, str] | None = None) -> "ProviderQueryEmbedder":
        return cls(OpenAICompatibleEmbeddingConfig.from_env(env=env, vector_size=vector_size), env=env)

    @property
    def model_id(self) -> str:
        return f"{self._config.provider}/{self._config.model}"

    @property
    def dimension(self) -> int:
        return int(self._config.vector_size)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self._model.embed_texts(list(texts))


__all__ = ["ProviderQueryEmbedder"]
