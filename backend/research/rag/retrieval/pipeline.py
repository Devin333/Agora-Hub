from __future__ import annotations

import logging
import time
from typing import Any

from framework.rag.retrieval import dedupe_by_key

from backend.research.document.models import PaperChunk
from backend.research.rag.retrieval.filtering import (
    filter_chunks_for_request,
    filter_scored_chunks_for_request,
)
from backend.research.rag.retrieval.metrics import RetrievalMetricsBuilder
from backend.research.rag.retrieval.policy_config import policy_config_hash
from backend.research.rag.retrieval.trace import RetrievalTrace


class RetrievalPipeline:
    def __init__(
        self,
        *,
        policy: Any,
        planner: Any,
        recall_stage: Any,
        ranking_stage: Any,
        parent_expander: Any,
        cross_ref_expander: Any,
        table_context_expander: Any,
        structural_expander: Any,
        supplemental_table_expander: Any,
        result_factory: Callable[..., Any],
        reranker_available: bool,
        field_index_available: bool,
        field_reranker_available: bool,
        visual_store_available: bool,
        claim_index_available: bool,
    ) -> None:
        self._policy = policy
        self._planner = planner
        self._recall_stage = recall_stage
        self._ranking_stage = ranking_stage
        self._parent_expander = parent_expander
        self._cross_ref_expander = cross_ref_expander
        self._table_context_expander = table_context_expander
        self._structural_expander = structural_expander
        self._supplemental_table_expander = supplemental_table_expander
        self._result_factory = result_factory
        self._metrics_builder = RetrievalMetricsBuilder(
            policy=policy,
            reranker_available=reranker_available,
            field_index_available=field_index_available,
            field_reranker_available=field_reranker_available,
            visual_store_available=visual_store_available,
            claim_index_available=claim_index_available,
        )

    def retrieve(self, request: Any) -> Any:
        t0 = time.perf_counter()
        plan = self._planner.build(request)
        route = plan.route
        candidate_filters = [dict(item) for item in plan.candidate_filters]
        active_policy_hash = policy_config_hash(self._policy)
        retrieval_trace = RetrievalTrace(
            policy_name=self._policy.name,
            policy_hash=active_policy_hash,
            route=plan.route_dict(),
        )

        candidate_limit = plan.candidate_limit
        recall_result = self._recall_stage.recall(
            request,
            route,
            candidate_filters,
            candidate_limit,
            trace=retrieval_trace,
        )
        candidates = filter_scored_chunks_for_request(recall_result.candidates, request)
        field_hits = recall_result.field_hits
        claim_hits = recall_result.claim_hits
        visual_hits = recall_result.visual_hits
        n_recalled = recall_result.n_recalled
        n_visual_recalled = recall_result.n_visual_recalled

        ranking_result = self._ranking_stage.rank(
            request,
            route,
            candidates,
            visual_hits,
            limit=request.limit,
        )
        scored = filter_scored_chunks_for_request(ranking_result.scored, request)
        child_chunks = filter_chunks_for_request(ranking_result.child_chunks, request)
        child_chunks = self._structural_expander.expand(child_chunks, request, route)
        child_chunks = filter_chunks_for_request(child_chunks, request)
        # Table queries should remain table-first when a real table chunk exists.
        # Paragraph candidates are still queried as a fallback for parsers that
        # only preserve the table reference in surrounding prose. Apply this
        # preference after supplemental table lookup because the table may have
        # been recovered there when a paragraph ranked above it.
        supplemental_table_chunks = self._supplemental_table_expander.expand(child_chunks, request, route)
        supplemental_table_chunks = filter_chunks_for_request(supplemental_table_chunks, request)
        child_chunks.extend(supplemental_table_chunks)
        if route.intent == "table_query":
            table_anchors = [
                chunk for chunk in child_chunks
                if chunk.chunk_type == "table"
                or chunk.has_table
                or bool(chunk.metadata.get("table_id"))
                or bool(chunk.metadata.get("parent_table_chunk_id"))
            ]
            if table_anchors:
                table_ids = {chunk.chunk_id for chunk in table_anchors}
                child_chunks = [
                    chunk for chunk in child_chunks
                    if chunk.chunk_id in table_ids or bool(chunk.metadata.get("expansion_reason"))
                ]
        top_score = scored[0][1] if scored else 0.0

        parent_chunks, parent_metrics = self._parent_expander.expand(child_chunks, request, route)
        parent_chunks = filter_chunks_for_request(parent_chunks, request)
        cross_ref_chunks = self._cross_ref_expander.expand(
            child_chunks,
            request.paper_id,
            filters=getattr(request, "filters", {}) or {},
        )
        table_context_chunks = self._table_context_expander.expand(child_chunks, request, route)
        ref_chunks = filter_chunks_for_request(
            _dedupe_chunks([*cross_ref_chunks, *table_context_chunks]),
            request,
        )

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        metrics = self._metrics_builder.build(
            active_policy_hash=active_policy_hash,
            plan=plan,
            route=route,
            candidate_filters=candidate_filters,
            candidate_limit=candidate_limit,
            recall_result=recall_result,
            candidates=candidates,
            field_hits=field_hits,
            claim_hits=claim_hits,
            n_recalled=n_recalled,
            n_visual_recalled=n_visual_recalled,
            base_reranker_enabled=ranking_result.base_reranker_enabled,
            field_reranker_enabled=ranking_result.field_reranker_enabled,
            n_filtered=ranking_result.n_filtered,
            scored=scored,
            child_chunks=child_chunks,
            parent_chunks=parent_chunks,
            ref_chunks=ref_chunks,
            supplemental_table_chunks=supplemental_table_chunks,
            table_context_chunks=table_context_chunks,
            top_score=top_score,
            elapsed_ms=elapsed_ms,
            retrieval_trace=retrieval_trace,
            parent_metrics=parent_metrics,
        )
        logging.getLogger(__name__).info("retrieval %s", metrics)
        return self._result_factory(
            parent_chunks=parent_chunks,
            child_chunks=child_chunks,
            ref_chunks=ref_chunks,
            intent=route.intent,
            metadata=metrics,
        )


def _dedupe_chunks(chunks: list[PaperChunk]) -> list[PaperChunk]:
    return dedupe_by_key(chunks, key=lambda chunk: chunk.chunk_id)


__all__ = ["RetrievalPipeline"]
