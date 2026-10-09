from __future__ import annotations

import re
from typing import Any

from framework.rag.core import intent_allowed

from backend.research.rag.retrieval.plan import (
    ChannelSpec,
    ExpanderSpec,
    FusionSpec,
    RerankSpec,
    RetrievalPlan,
)
from backend.research.rag.retrieval.filtering import merge_request_filters
from backend.research.rag.retrieval.paper_policy import RetrievalRoute, build_retrieval_route
_HYBRID_RRF_INTENTS = (
    "figure_query",
    "table_query",
    "numerical_result",
    "comparison",
    "formula_query",
    "citation_query",
)


class QueryPlanner:
    def __init__(self, policy: Any) -> None:
        self._policy = policy

    def build(self, request: Any) -> RetrievalPlan:
        route = build_retrieval_route(str(request.question or ""))
        filters = self._build_filters(route, request)
        candidate_filters = tuple(self._candidate_filters(route, filters))
        element_labels = tuple(sorted(element_query_labels(request.question, route.intent)))
        candidate_limit = self._candidate_limit(
            request_limit=int(request.limit),
            route=route,
            element_query_labels=element_labels,
        )
        return RetrievalPlan(
            route=route,
            filters=filters,
            candidate_filters=candidate_filters,
            candidate_limit=candidate_limit,
            element_query_labels=element_labels,
            channels=self._channels(route, candidate_filters, candidate_limit),
            fusion=FusionSpec(
                algorithm="rrf" if self._policy.hybrid_rrf_enabled else "weighted",
                rrf_k=self._policy.rrf_k,
            ),
            rerank=RerankSpec(
                lightweight_enabled=self._policy.reranker_enabled_for(route.intent),
                field_enabled=self._policy.field_reranker_enabled_for(route.intent),
                score_threshold=self._policy.rerank_score_threshold,
                options={
                    "lightweight_intents": tuple(self._policy.reranking_intents),
                    "field_intents": tuple(self._policy.field_reranking_intents),
                },
            ),
            metadata=dict(getattr(request, "metadata", {}) or {}),
            expanders=(
                ExpanderSpec("parent", options={
                    "max_chunks": self._policy.max_parent_chunks,
                    "max_tokens": self._policy.max_parent_tokens,
                }),
                ExpanderSpec("cross_ref"),
                ExpanderSpec("table_context", enabled=route.intent in {
                    "table_query",
                    "numerical_result",
                    "comparison",
                }),
                ExpanderSpec("formula_context", enabled=route.intent == "formula_query"),
            ),
        )

    def _build_filters(self, route: RetrievalRoute, request: Any) -> dict[str, Any]:
        return merge_request_filters(request, route.extra_filters)

    def _candidate_filters(
        self,
        route: RetrievalRoute,
        base_filters: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if route.intent == "formula_query" and self._policy.formula_sparse_enabled:
            formula_filters = dict(base_filters)
            formula_filters.pop("has_formula", None)
            formula_filters["chunk_type"] = "formula"
            return [formula_filters]
        if route.candidate_filter_groups:
            return dedupe_filters([
                {**base_filters, **dict(filters)}
                for filters in route.candidate_filter_groups
            ])
        if "chunk_type" in base_filters or not route.chunk_type_filter:
            return [dict(base_filters)]
        return [
            {**base_filters, "chunk_type": chunk_type}
            for chunk_type in route.chunk_type_filter
        ]

    def _candidate_limit(
        self,
        *,
        request_limit: int,
        route: RetrievalRoute,
        element_query_labels: tuple[str, ...],
    ) -> int:
        candidate_limit = request_limit * self._policy.overfetch_multiplier
        if element_query_labels:
            candidate_limit = max(
                candidate_limit,
                request_limit * self._policy.element_label_overfetch_multiplier,
            )
        if route.intent == "citation_query":
            candidate_limit = max(
                candidate_limit,
                request_limit * self._policy.citation_claim_overfetch_multiplier,
            )
        return candidate_limit

    def _channels(
        self,
        route: RetrievalRoute,
        candidate_filters: tuple[dict[str, Any], ...],
        candidate_limit: int,
    ) -> tuple[ChannelSpec, ...]:
        return (
            ChannelSpec(
                "dense_text",
                enabled=True,
                limit=candidate_limit,
                filters=candidate_filters,
                options={"multi_query": self._policy.multi_query_enabled},
            ),
            ChannelSpec(
                "sparse_lexical",
                enabled=(
                    self._policy.sparse_lexical_enabled
                    and intent_allowed(route.intent, _HYBRID_RRF_INTENTS)
                ),
                limit=candidate_limit,
                filters=candidate_filters,
                options={
                    "formula_sparse": (
                        self._policy.formula_sparse_enabled
                        and route.intent == "formula_query"
                    )
                },
            ),
            ChannelSpec(
                "field_embedding",
                enabled=self._policy.field_embedding_enabled,
                filters=candidate_filters,
                options={"field_names": self._policy.field_search_fields_for(route.intent)},
            ),
            ChannelSpec(
                "claim_index",
                enabled=route.intent == "citation_query",
                limit=candidate_limit,
            ),
            ChannelSpec("visual", enabled=True, filters=candidate_filters),
        )


def element_query_labels(query_text: str, intent: str) -> set[str]:
    prefixes_by_intent: dict[str, tuple[str, ...]] = {
        "formula_query": ("equation", "formula", "eq"),
        "table_query": ("table", "tab"),
        "figure_query": ("figure", "fig"),
        "numerical_result": ("table", "tab", "figure", "fig"),
    }
    prefixes = prefixes_by_intent.get(intent, ())
    if not prefixes:
        return set()
    normalized = str(query_text or "").casefold()
    labels: set[str] = set()
    for prefix in prefixes:
        pattern = rf"\b{re.escape(prefix)}(?:\.|\s+)([a-z0-9][a-z0-9._-]*)"
        for match in re.finditer(pattern, normalized):
            labels.add(normalize_element_label(match.group(1)))
    return {label for label in labels if label}


def normalize_element_label(value: str) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(r"^(?:equation|formula|eq|table|tab|figure|fig)\.?\s*", "", text)
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def dedupe_filters(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[tuple[str, str], ...]] = set()
    out: list[dict[str, Any]] = []
    for item in filters:
        normalized = tuple(sorted((str(key), repr(value)) for key, value in item.items()))
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(item)
    return out or [{}]


__all__ = [
    "QueryPlanner",
    "dedupe_filters",
    "element_query_labels",
    "normalize_element_label",
]
