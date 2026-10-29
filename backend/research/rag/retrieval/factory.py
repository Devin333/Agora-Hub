from __future__ import annotations

from typing import TYPE_CHECKING

from backend.research.ports.chunk_store import ChunkStorePort
from backend.research.ports.field_embedding_index import FieldEmbeddingSearchPort
from backend.research.ports.section_navigator import SectionNavigatorPort
from backend.research.ports.visual_chunk_index import VisualChunkSearchPort
from backend.research.rag.retrieval.channels.claim_index import ClaimIndexChannel
from backend.research.rag.retrieval.channels.dense_text import DenseTextChannel
from backend.research.rag.retrieval.channels.field_embedding import FieldEmbeddingChannel
from backend.research.rag.retrieval.channels.sparse_lexical import SparseLexicalChannel
from backend.research.rag.retrieval.channels.visual import VisualRecallChannel
from backend.research.rag.retrieval.contracts import RetrievalRequest, RetrievalResult
from backend.research.rag.retrieval.expanders.cross_ref import CrossRefContextExpander
from backend.research.rag.retrieval.expanders.parent import ParentContextExpander
from backend.research.rag.retrieval.expanders.section_navigation import (
    SectionNavigationConfig,
    SectionNavigationExpander,
)
from backend.research.rag.retrieval.expanders.structural import StructuralContextExpander
from backend.research.rag.retrieval.expanders.supplemental_table import SupplementalTableHitExpander
from backend.research.rag.retrieval.expanders.table_context import TableContextExpander
from backend.research.rag.retrieval.paper_claim_index import PaperClaimSearchPort
from backend.research.rag.retrieval.pipeline import RetrievalPipeline
from backend.research.rag.retrieval.planner import QueryPlanner
from backend.research.rag.retrieval.policies import RetrievalPolicy
from backend.research.rag.retrieval.ranking_stage import ChildRankingStage
from backend.research.rag.retrieval.recall_stage import CandidateRecallStage
from backend.research.rag.retrieval.rerank import RerankCascade
from backend.research.rag.retrieval.scoring import ChildCandidateScorer

if TYPE_CHECKING:
    from backend.research.ports.reranker import RerankerPort


def build_retrieval_pipeline(
    chunk_store: ChunkStorePort,
    *,
    policy: RetrievalPolicy,
    reranker: "RerankerPort | None" = None,
    field_index: FieldEmbeddingSearchPort | None = None,
    field_reranker: "RerankerPort | None" = None,
    visual_store: VisualChunkSearchPort | None = None,
    claim_index: PaperClaimSearchPort | None = None,
    section_navigator: SectionNavigatorPort | None = None,
    section_navigation: SectionNavigationConfig | None = None,
) -> RetrievalPipeline:
    if section_navigation is not None and section_navigator is None:
        raise ValueError("section_navigation config requires a section_navigator")
    dense_channel = DenseTextChannel(chunk_store)
    sparse_channel = SparseLexicalChannel(chunk_store)
    field_channel = FieldEmbeddingChannel(chunk_store, field_index)
    claim_channel = ClaimIndexChannel(chunk_store, claim_index)
    visual_channel = VisualRecallChannel(chunk_store, visual_store)
    recall_stage = CandidateRecallStage(
        dense_channel=dense_channel,
        sparse_channel=sparse_channel,
        field_channel=field_channel,
        claim_channel=claim_channel,
        visual_channel=visual_channel,
        field_index=field_index,
        claim_index=claim_index,
        visual_store=visual_store,
        policy=policy,
    )
    planner = QueryPlanner(policy)
    rerank_cascade = RerankCascade(
        policy,
        reranker=reranker,
        field_reranker=field_reranker,
    )
    parent_expander = ParentContextExpander(
        chunk_store,
        policy,
        reranker=reranker,
    )
    ranking_stage = ChildRankingStage(
        policy=policy,
        rerank_cascade=rerank_cascade,
        child_scorer=ChildCandidateScorer(policy),
        visual_channel=visual_channel,
        request_factory=RetrievalRequest,
    )
    return RetrievalPipeline(
        policy=policy,
        planner=planner,
        recall_stage=recall_stage,
        ranking_stage=ranking_stage,
        parent_expander=parent_expander,
        cross_ref_expander=CrossRefContextExpander(chunk_store),
        table_context_expander=TableContextExpander(
            chunk_store,
            policy,
            reranker=reranker,
        ),
        structural_expander=StructuralContextExpander(chunk_store, policy),
        supplemental_table_expander=SupplementalTableHitExpander(chunk_store, policy),
        result_factory=RetrievalResult,
        reranker_available=reranker is not None,
        field_index_available=field_index is not None,
        field_reranker_available=field_reranker is not None,
        visual_store_available=visual_store is not None,
        claim_index_available=claim_index is not None,
        section_navigation_expander=(
            SectionNavigationExpander(chunk_store, section_navigator, section_navigation)
            if section_navigator is not None
            else None
        ),
    )


__all__ = ["build_retrieval_pipeline"]
