from __future__ import annotations

from typing import TYPE_CHECKING

from backend.research.document.models import PaperChunk
from backend.research.ports.chunk_store import ChunkStorePort
from backend.research.ports.field_embedding_index import FieldEmbeddingSearchPort
from backend.research.ports.section_navigator import SectionNavigatorPort
from backend.research.ports.visual_chunk_index import VisualChunkSearchPort
from backend.research.rag.retrieval.contracts import RetrievalRequest, RetrievalResult
from backend.research.rag.retrieval.expanders.section_navigation import SectionNavigationConfig
from backend.research.rag.retrieval.factory import build_retrieval_pipeline
from backend.research.rag.retrieval.paper_claim_index import PaperClaimSearchPort
from backend.research.rag.retrieval.policies import (
    DEFAULT_RETRIEVAL_POLICY,
    HIGH_VALUE_VISUAL_RESULT_INTENTS,
    LIGHTWEIGHT_FIELD_RERANK_INTENTS,
    NEWS_PAPER_RAG_POLICY_CONFIG_ENV,
    NEWS_PAPER_RAG_POLICY_ENV,
    PAPER_BLIND_SEMANTIC_RAG_V1_POLICY,
    PAPER_FORMULA_RAG_V1_POLICY,
    PAPER_HYBRID_RRF_RAG_V1_POLICY,
    PAPER_VISUAL_RAG_TUNED_POLICY,
    RetrievalPolicy,
    build_retrieval_policy,
    build_retrieval_policy_from_config,
    build_retrieval_policy_from_config_file,
    build_retrieval_policy_from_env,
    retrieval_policy_enables_lightweight_reranker,
)

if TYPE_CHECKING:
    from backend.research.ports.reranker import RerankerPort


class ResearchRetriever:
    """
    Agent-callable retrieval tool.

    Flow: intent classification -> vector search -> rerank -> context expansion.
    """

    def __init__(
        self,
        chunk_store: ChunkStorePort,
        *,
        policy: RetrievalPolicy | None = None,
        reranker: "RerankerPort | None" = None,
        field_index: FieldEmbeddingSearchPort | None = None,
        field_reranker: "RerankerPort | None" = None,
        visual_store: VisualChunkSearchPort | None = None,
        claim_index: PaperClaimSearchPort | None = None,
        section_navigator: SectionNavigatorPort | None = None,
        section_navigation: SectionNavigationConfig | None = None,
    ) -> None:
        self._store = chunk_store
        self._policy = policy or RetrievalPolicy()
        self._pipeline = build_retrieval_pipeline(
            chunk_store,
            policy=self._policy,
            reranker=reranker,
            field_index=field_index,
            field_reranker=field_reranker,
            visual_store=visual_store,
            claim_index=claim_index,
            section_navigator=section_navigator,
            section_navigation=section_navigation,
        )

    @property
    def policy(self) -> RetrievalPolicy:
        return self._policy

    def get_chunk(self, chunk_id: str) -> PaperChunk | None:
        """Expose deterministic chunk lookup for context assembly helpers."""
        return self._store.get_chunk(chunk_id)

    def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        return self._pipeline.retrieve(request)


__all__ = [
    "DEFAULT_RETRIEVAL_POLICY",
    "HIGH_VALUE_VISUAL_RESULT_INTENTS",
    "LIGHTWEIGHT_FIELD_RERANK_INTENTS",
    "NEWS_PAPER_RAG_POLICY_CONFIG_ENV",
    "NEWS_PAPER_RAG_POLICY_ENV",
    "PAPER_BLIND_SEMANTIC_RAG_V1_POLICY",
    "PAPER_FORMULA_RAG_V1_POLICY",
    "PAPER_HYBRID_RRF_RAG_V1_POLICY",
    "PAPER_VISUAL_RAG_TUNED_POLICY",
    "ResearchRetriever",
    "RetrievalPolicy",
    "RetrievalRequest",
    "RetrievalResult",
    "build_retrieval_policy",
    "build_retrieval_policy_from_config",
    "build_retrieval_policy_from_config_file",
    "build_retrieval_policy_from_env",
    "retrieval_policy_enables_lightweight_reranker",
]
