from __future__ import annotations

from dataclasses import replace
from typing import Any

from framework.memory.exceptions import MemoryNotFound
from framework.memory.models import MemoryQuery, MemoryRecord, MemorySearchResult, MemoryWriteResult
from framework.memory.stores.keyword_search import search_memory_records


class InMemoryMemoryStore:
    def __init__(self, records: list[MemoryRecord] | None = None) -> None:
        self._records: dict[str, MemoryRecord] = {}
        if records:
            self.write_many(records)

    def write(self, record: MemoryRecord) -> MemoryWriteResult:
        return self.write_many([record])

    def write_many(self, records: list[MemoryRecord]) -> MemoryWriteResult:
        for record in records:
            self._records[record.memory_id] = record
        return MemoryWriteResult(
            accepted_count=len(records),
            written_count=len(records),
            memory_ids=[record.memory_id for record in records],
        )

    def get(self, memory_id: str) -> MemoryRecord | None:
        return self._records.get(memory_id)

    def search(self, query: MemoryQuery) -> list[MemorySearchResult]:
        return search_memory_records(self._records.values(), query)

    def update(self, memory_id: str, patch: dict[str, Any]) -> MemoryRecord:
        record = self._records.get(memory_id)
        if record is None:
            raise MemoryNotFound(memory_id)
        existing_refs = record.refs if isinstance(record.refs, dict) else {}
        updated = replace(
            record,
            kind=patch.get("kind", record.kind),
            scope=patch.get("scope", record.scope),
            summary=patch.get("summary", record.summary),
            content=str(patch.get("content", record.content)),
            metadata={**record.metadata, **dict(patch.get("metadata") or {})},
            refs={**existing_refs, **dict(patch.get("refs") or {})},
            tags=[str(tag) for tag in patch.get("tags", record.tags)],
            confidence=patch.get("confidence", record.confidence),
            importance=patch.get("importance", record.importance),
            score=patch.get("score", record.score),
            embedding=patch.get("embedding", record.embedding),
            actor=patch.get("actor", record.actor),
            namespace=patch.get("namespace", record.namespace),
            tenant_id=patch.get("tenant_id", record.tenant_id),
            updated_at=patch.get("updated_at", record.updated_at),
            expires_at=patch.get("expires_at", record.expires_at),
            invalidated_at=patch.get("invalidated_at", record.invalidated_at),
            invalidation_reason=patch.get("invalidation_reason", record.invalidation_reason),
            version=patch.get("version", record.version),
        )
        self._records[memory_id] = updated
        return updated

    def delete(self, memory_id: str) -> None:
        self._records.pop(memory_id, None)

    def delete_many(self, memory_ids: list[str]) -> list[str]:
        deleted: list[str] = []
        for memory_id in memory_ids:
            if memory_id in self._records:
                self.delete(memory_id)
                deleted.append(memory_id)
        return deleted

    def delete_by_query(self, query: MemoryQuery) -> list[str]:
        matches = [result.memory_id for result in self.search(query)]
        return self.delete_many(matches)

    def records(self) -> list[MemoryRecord]:
        return list(self._records.values())
