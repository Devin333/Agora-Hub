"""Typed document view over the multi-model research catalog store."""
from collections.abc import Mapping

from backend.research.domain.document import ResearchDocument
from infrastructure.research.catalog_store import FilesystemResearchCatalogStore


class FilesystemResearchDocumentRepository:
    def __init__(self, catalog: FilesystemResearchCatalogStore) -> None:
        self._catalog = catalog

    def get(self, paper_id: str, *, actor_scope: Mapping[str, str] | None = None) -> ResearchDocument | None:
        return self._catalog.get_document(paper_id, actor_scope=actor_scope)

    def save(self, document: ResearchDocument) -> None:
        self._catalog.save(document)
