from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest

from backend.research.application.catalog import InMemoryResearchCatalogRepository
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.parse_cache_identity import (
    PARSE_CACHE_IDENTITY_SCHEMA,
    PARSE_NORMALIZER_VERSION,
    parse_cache_identity_fingerprint,
)
from backend.research.domain import ResearchDocument, ResearchSection, SourceLineage
from backend.research.services.pdf_snapshot_migration import (
    INDEX_BLOCKER,
    PdfSnapshotMigrationBlocked,
    PdfSnapshotMigrationService,
    content_checksum,
    document_content_checksum,
)


_SCOPE = {"tenant_id": "tenant-a", "user_id": "user-a"}
_SOURCE_HASH = sha256(b"paper").hexdigest()


def _cache_identity(profile: str) -> tuple[dict, str]:
    identity = {
        "schema": PARSE_CACHE_IDENTITY_SCHEMA,
        "source": {"hash": _SOURCE_HASH, "content_type": "application/pdf"},
        "parser": {"type": "test.Parser", "configuration": "fixture"},
        "compiler": {"type": None},
        "chunker": {"type": "test.Chunker", "configuration": "fixture"},
        "schemas": {
            "document": "research-document.v1",
            "document_sha256": "document-sha",
            "fidelity": "research-document-fidelity.v1",
            "fidelity_sha256": "fidelity-sha",
        },
        "profile": {"quality_profile": profile, "parser_backend": "fixture"},
        "normalizer": {"version": PARSE_NORMALIZER_VERSION, "module_sha256": "normalizer-sha"},
        "component_modules": {"test": "module-sha"},
        "runtime_config": {},
    }
    fingerprint = parse_cache_identity_fingerprint(identity)
    assert fingerprint is not None
    return identity, fingerprint


def _document(*, profile: str, text: str) -> ResearchDocument:
    identity, fingerprint = _cache_identity(profile)
    source_ref = "paper://paper-1/method"
    return ResearchDocument(
        paper_id="paper-1",
        source_hash=_SOURCE_HASH,
        actor_scope=_SCOPE,
        sections=[ResearchSection(
            section_id="method",
            title="Method",
            text=text,
            source_ref=source_ref,
        )],
        lineage=SourceLineage(
            source_refs=[source_ref],
            source_hash=_SOURCE_HASH,
            metadata={"actor_scope": _SCOPE},
        ),
        metadata={
            "actor_scope": _SCOPE,
            "parse_source": "marker",
            "parse_cache_identity": identity,
            "parse_cache_fingerprint": fingerprint,
        },
    )


class _Artifacts:
    def __init__(self) -> None:
        self.records: dict[str, dict] = {}

    def publish(self, *, artifact_type: str, payload: dict, metadata: dict | None = None) -> str:
        ref = f"artifact://candidate/{len(self.records) + 1}"
        self.records[ref] = {
            "artifactType": artifact_type,
            "payload": deepcopy(payload),
            "metadata": deepcopy(metadata or {}),
        }
        return ref

    def read(self, ref: str, *, actor_scope, include_payload: bool = False, max_chars: int = 200_000):
        record = deepcopy(self.records[ref])
        if not include_payload:
            record.pop("payload", None)
        return record


def test_snapshot_candidate_is_staged_but_activation_fails_closed_without_atomic_index() -> None:
    repository = InMemoryResearchCatalogRepository()
    previous = _document(profile="metadata", text="old content")
    candidate = _document(profile="reading", text="new content")
    repository.save(previous)
    artifacts = _Artifacts()
    service = PdfSnapshotMigrationService(
        documents=repository,
        artifacts=artifacts,
        chunker=PaperDocumentChunker(),
        artifact_reader=artifacts,
    )

    ref = service.prepare_candidate(
        document=candidate,
        previous=previous,
        actor_scope=_SCOPE,
        run_id="run-1",
    )
    payload = artifacts.records[ref]["payload"]

    assert payload["activation_status"] == "blocked"
    assert payload["index_snapshot"] is None
    assert payload["blockers"] == [INDEX_BLOCKER]
    assert payload["parse_cache_fingerprint"] == candidate.metadata["parse_cache_fingerprint"]
    assert artifacts.records[ref]["metadata"]["candidate_checksum"] == content_checksum(payload)
    with pytest.raises(PdfSnapshotMigrationBlocked) as blocked:
        service.activate_candidate(
            ref,
            paper_id="paper-1",
            current_source_hash=_SOURCE_HASH,
            actor_scope=_SCOPE,
        )
    assert blocked.value.code == INDEX_BLOCKER
    assert document_content_checksum(repository.get_document("paper-1", actor_scope=_SCOPE)) == document_content_checksum(previous)


def test_snapshot_candidate_rejects_incomplete_cache_identity() -> None:
    repository = InMemoryResearchCatalogRepository()
    previous = _document(profile="metadata", text="old content")
    candidate = _document(profile="reading", text="new content").model_copy(
        update={"metadata": {"actor_scope": _SCOPE, "parse_source": "marker"}}
    )
    repository.save(previous)
    artifacts = _Artifacts()
    service = PdfSnapshotMigrationService(
        documents=repository,
        artifacts=artifacts,
        chunker=PaperDocumentChunker(),
        artifact_reader=artifacts,
    )

    with pytest.raises(PdfSnapshotMigrationBlocked) as blocked:
        service.prepare_candidate(
            document=candidate,
            previous=previous,
            actor_scope=_SCOPE,
            run_id="run-1",
        )
    assert blocked.value.code == "snapshot_cache_identity_incomplete"
    assert artifacts.records == {}


def test_snapshot_activation_rejects_tampered_candidate_before_index_blocker() -> None:
    repository = InMemoryResearchCatalogRepository()
    previous = _document(profile="metadata", text="old content")
    candidate = _document(profile="reading", text="new content")
    repository.save(previous)
    artifacts = _Artifacts()
    service = PdfSnapshotMigrationService(
        documents=repository,
        artifacts=artifacts,
        chunker=PaperDocumentChunker(),
        artifact_reader=artifacts,
    )
    ref = service.prepare_candidate(
        document=candidate,
        previous=previous,
        actor_scope=_SCOPE,
        run_id="run-1",
    )
    artifacts.records[ref]["payload"]["chunks"][0]["content"] = "tampered"

    with pytest.raises(PdfSnapshotMigrationBlocked) as blocked:
        service.activate_candidate(
            ref,
            paper_id="paper-1",
            current_source_hash=_SOURCE_HASH,
            actor_scope=_SCOPE,
        )
    assert blocked.value.code == "snapshot_candidate_checksum_mismatch"
    assert document_content_checksum(repository.get_document("paper-1", actor_scope=_SCOPE)) == document_content_checksum(previous)
