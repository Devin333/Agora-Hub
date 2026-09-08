from __future__ import annotations

from typing import Any, Protocol

from framework.memory.models import MemoryQuery, MemoryRecord, MemorySearchResult, MemoryWriteResult


class MemorySearchPort(Protocol):
    def search(self, query: MemoryQuery) -> list[MemorySearchResult]:
        ...


class MemoryStore(MemorySearchPort, Protocol):
    def write(self, record: MemoryRecord) -> MemoryWriteResult:
        ...

    def write_many(self, records: list[MemoryRecord]) -> MemoryWriteResult:
        ...

    def get(self, memory_id: str) -> MemoryRecord | None:
        ...

    def update(self, memory_id: str, patch: dict[str, Any]) -> MemoryRecord:
        ...

    def delete(self, memory_id: str) -> None:
        ...
