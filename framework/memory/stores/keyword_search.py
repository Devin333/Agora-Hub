"""Shared keyword retrieval over real memory records, without a mutable index."""

from __future__ import annotations

from collections.abc import Iterable

from framework.memory.models import MemoryQuery, MemoryRecord, MemorySearchResult


def search_memory_records(records: Iterable[MemoryRecord], query: MemoryQuery) -> list[MemorySearchResult]:
    results: list[MemorySearchResult] = []
    query_terms = _terms(query.query)
    for record in records:
        if not _record_matches_query(record, query):
            continue
        record_terms = _terms(f"{record.summary or ''} {record.content} {' '.join(record.tags)}")
        overlap = len(query_terms & record_terms)
        score = overlap / len(query_terms) if query_terms else 0.0
        if query.min_score is not None and score < query.min_score:
            continue
        results.append(MemorySearchResult(
            record=record, score=score, source="keyword",
            match_reasons=["text_match"] if overlap else [],
        ))
    results.sort(key=lambda result: result.score, reverse=True)
    return results[:query.limit]


def _record_matches_query(record: MemoryRecord, query: MemoryQuery) -> bool:
    if not query.include_invalidated and record.is_invalidated():
        return False
    if not query.include_expired and record.is_expired():
        return False
    if query.scopes and record.scope not in query.scopes:
        return False
    if query.kinds and record.kind not in query.kinds:
        return False
    if query.namespace is not None and record.namespace != query.namespace:
        return False
    if query.tenant_id is not None and record.tenant_id != query.tenant_id:
        return False
    if query.tags and not set(query.tags).issubset(set(record.tags)):
        return False
    if query.time_window is not None and not query.time_window.contains(record.created_at):
        return False
    return query.matches_metadata(record)


def _terms(text: str) -> set[str]:
    normalized = "".join(ch.lower() if ch.isalnum() else " " for ch in str(text))
    return {part for part in normalized.split() if part}
