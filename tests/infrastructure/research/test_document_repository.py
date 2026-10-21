import json

import pytest

from backend.research.domain.document_fidelity import DocumentBlock, DocumentFidelity, DocumentPage
from backend.research.domain.document import ResearchDocument, ResearchSection
from backend.research.domain.common import SourceLineage
from infrastructure.research.catalog_store import (
    FilesystemResearchCatalogStore,
    ResearchCatalogStoreCorruptionError,
)
from infrastructure.research.document_repository import FilesystemResearchDocumentRepository


def _fidelity() -> DocumentFidelity:
    return DocumentFidelity(
        schema_version="research-document-fidelity.v1",
        normalization_version="normalizer/v3",
        source_hash="source",
        parser_fingerprint="sha256:parser",
        profile_fingerprint="sha256:profile",
        content_checksum="sha256:fidelity-content",
        artifact_refs=("artifact://research/raw/" + "a" * 64,),
        pages=(DocumentPage(
            pdf_page_number=1,
            width=612,
            height=792,
            dimension_unit="pt",
            rotation_degrees=0,
            backend_page_id="marker-page-0",
        ),),
        blocks=(DocumentBlock(
            block_id="body",
            page_number=1,
            kind="text",
            source_order=0,
            reading_order=0,
            order_status="preserved",
            source_locator="source://paper#page=1&block=body",
            bbox_unknown_reason="fixture does not define geometry",
            text="Body",
            content_owner_id="body",
        ),),
    )


def test_document_view_reads_document_instead_of_catalog_entry_and_keeps_owner_scope(tmp_path):
    catalog = FilesystemResearchCatalogStore(tmp_path)
    documents = FilesystemResearchDocumentRepository(catalog)
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    document = ResearchDocument(paper_id="paper", source_hash="source", sections=[ResearchSection(section_id="methods", title="Methods", text="Full paper body", source_ref="source://paper/methods")], lineage=SourceLineage(source_refs=["source://paper"], metadata=scope), actor_scope=scope)
    documents.save(document)
    assert catalog.get("paper", actor_scope=scope) is None
    assert documents.get("paper", actor_scope=scope) == document
    assert documents.get("paper", actor_scope={"user_id": "bob", "memory_namespace": "research:user:bob"}) is None


def test_document_fidelity_roundtrips_through_existing_repository_with_refs_and_version(tmp_path):
    catalog = FilesystemResearchCatalogStore(tmp_path)
    documents = FilesystemResearchDocumentRepository(catalog)
    fidelity = _fidelity()
    document = ResearchDocument(
        paper_id="paper-fidelity",
        source_hash="source",
        lineage=SourceLineage(source_refs=["source://paper"], source_hash="source", artifact_refs=["artifact://research/source/" + "b" * 64]),
        fidelity=fidelity,
    )

    documents.save(document)
    loaded = documents.get("paper-fidelity")

    assert loaded == document
    assert loaded is not None
    assert loaded.fidelity is not None
    assert loaded.fidelity.schema_version == "research-document-fidelity.v1"
    assert loaded.fidelity.model_dump(mode="json") == fidelity.model_dump(mode="json")
    assert loaded.fidelity.artifact_refs == fidelity.artifact_refs
    assert loaded.fidelity.blocks[0].source_locator == "source://paper#page=1&block=body"


def test_legacy_document_without_fidelity_remains_explicitly_unverified(tmp_path):
    catalog = FilesystemResearchCatalogStore(tmp_path)
    documents = FilesystemResearchDocumentRepository(catalog)
    legacy = ResearchDocument(
        paper_id="legacy-paper",
        source_hash="legacy-source",
        lineage=SourceLineage(source_refs=["source://legacy"], source_hash="legacy-source"),
    )

    documents.save(legacy)
    loaded = documents.get("legacy-paper")

    assert loaded == legacy
    assert loaded is not None and loaded.fidelity is None


def test_document_fidelity_tampering_is_rejected_by_catalog_checksum(tmp_path):
    catalog = FilesystemResearchCatalogStore(tmp_path)
    documents = FilesystemResearchDocumentRepository(catalog)
    document = ResearchDocument(
        paper_id="tamper-paper",
        source_hash="source",
        lineage=SourceLineage(source_refs=["source://paper"], source_hash="source"),
        fidelity=_fidelity(),
    )
    documents.save(document)

    payload = json.loads(catalog.path.read_text(encoding="utf-8"))
    next(iter(payload["documents"].values()))["fidelity"]["content_checksum"] = "sha256:tampered"
    catalog.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ResearchCatalogStoreCorruptionError, match="checksum mismatch"):
        documents.get("tamper-paper")
