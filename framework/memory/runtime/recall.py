from __future__ import annotations

from typing import Protocol

from framework.memory.models import MemoryContextBlock, MemoryQuery, MemoryRecallResult, MemorySearchResult
from framework.memory.policy import MemoryPolicy
from framework.memory.runtime.context_assembler import MemoryContextAssembler
from framework.memory.stores.base import MemorySearchPort


class MemoryRecallStrategy(Protocol):
    def recall(
        self,
        query: MemoryQuery,
        *,
        store: MemorySearchPort,
        policy: MemoryPolicy,
        assembler: MemoryContextAssembler,
    ) -> MemoryRecallResult:
        ...


class SimpleMemoryRecallStrategy:
    def recall(
        self,
        query: MemoryQuery,
        *,
        store: MemorySearchPort,
        policy: MemoryPolicy,
        assembler: MemoryContextAssembler,
    ) -> MemoryRecallResult:
        policy.validate_recall(query)
        effective_query = policy.filtered_query(query)
        results = store.search(effective_query)
        results = _rank_filter_and_limit(results, effective_query)
        context_budget = effective_query.max_context_tokens or policy.max_context_tokens
        context_block = assembler.assemble(
            results,
            max_context_tokens=context_budget,
        )
        return MemoryRecallResult(
            query=effective_query,
            results=results,
            context_block=context_block,
            diagnostics=_recall_diagnostics(
                requested_query=query,
                effective_query=effective_query,
                results=results,
                context_block=context_block,
                context_budget=context_budget,
            ),
        )


def _rank_filter_and_limit(
    results: list[MemorySearchResult],
    query: MemoryQuery,
) -> list[MemorySearchResult]:
    filtered = [
        result
        for result in results
        if query.min_score is None or result.score >= query.min_score
    ]
    filtered.sort(key=lambda result: result.score, reverse=True)
    return filtered[: query.limit]


def _recall_diagnostics(
    *,
    requested_query: MemoryQuery,
    effective_query: MemoryQuery,
    results: list[MemorySearchResult],
    context_block: MemoryContextBlock,
    context_budget: int,
) -> dict[str, object]:
    return {
        "requested_query": requested_query.to_dict(),
        "effective_query": effective_query.to_dict(),
        "result_count": len(results),
        "context_token_budget": context_budget,
        "context_token_estimate": context_block.token_estimate,
        "memory_ids": list(context_block.memory_ids),
    }
