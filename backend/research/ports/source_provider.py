from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

from backend.research.domain.document import ResearchDocument
from backend.research.domain.paper import PaperSourceRecord, ResearchPaper


@dataclass(frozen=True)
class BoundPaperSource:
    paper: ResearchPaper
    source_record: PaperSourceRecord
    document: ResearchDocument


@runtime_checkable
class PaperSourceProvider(Protocol):
    def fetch_paper(self, source_url: str) -> ResearchPaper:
        ...

    def fetch_source_record(self, paper_id: str) -> PaperSourceRecord:
        ...


@runtime_checkable
class BoundPaperSourceProvider(PaperSourceProvider, Protocol):
    def fetch_bound_source(
        self,
        *,
        source_ref: str,
        paper_id: str,
        source_revision: str,
        actor_scope: Mapping[str, str],
    ) -> BoundPaperSource | None:
        ...


__all__ = ["BoundPaperSource", "BoundPaperSourceProvider", "PaperSourceProvider"]
