from backend.research.domain.document import ResearchDocument, ResearchSection
from backend.research.domain.common import SourceLineage
from infrastructure.research.catalog_store import FilesystemResearchCatalogStore
from infrastructure.research.document_repository import FilesystemResearchDocumentRepository


def test_document_view_reads_document_instead_of_catalog_entry_and_keeps_owner_scope(tmp_path):
    catalog = FilesystemResearchCatalogStore(tmp_path)
    documents = FilesystemResearchDocumentRepository(catalog)
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    document = ResearchDocument(paper_id="paper", source_hash="source", sections=[ResearchSection(section_id="methods", title="Methods", text="Full paper body", source_ref="source://paper/methods")], lineage=SourceLineage(source_refs=["source://paper"], metadata=scope), actor_scope=scope)
    documents.save(document)
    assert catalog.get("paper", actor_scope=scope) is None
    assert documents.get("paper", actor_scope=scope) == document
    assert documents.get("paper", actor_scope={"user_id": "bob", "memory_namespace": "research:user:bob"}) is None
