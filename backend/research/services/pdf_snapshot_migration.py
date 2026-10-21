"""Isolated PDF migration candidates; never promote a partial parse/chunk/index tuple.

The current ChunkIndexerPort has no isolated index snapshot or atomic activation
operation. This service deliberately refuses promotion and rollback until that
storage capability exists. Candidates use the existing immutable artifact store,
not the active document repository, chunk manifest, or vector namespace.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any, Mapping

from backend.research.document.chunker import PaperDocumentChunker
from backend.research.domain.catalog import actor_scope_matches
from backend.research.domain.document import ResearchDocument
from backend.research.ports.artifact_store import ResearchArtifactReadPort, ResearchArtifactStorePort
from backend.research.ports.paper_ingest import ResearchDocumentRepository


CANDIDATE_SCHEMA = "research-pdf-snapshot-candidate.v1"
CANDIDATE_ARTIFACT_TYPE = "research-pdf-snapshot-candidate"
INDEX_BLOCKER = "index_snapshot_atomic_activation_unavailable"


def content_checksum(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def document_content_checksum(document: ResearchDocument) -> str:
    """Bind parse content, excluding only publication bookkeeping and this digest."""
    payload = document.model_dump(mode="json", exclude_none=True)
    metadata = dict(payload.get("metadata") or {})
    for key in ("published_artifacts", "parse_content_checksum"):
        metadata.pop(key, None)
    payload["metadata"] = metadata
    return content_checksum(payload)


class PdfSnapshotMigrationBlocked(RuntimeError):
    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = dict(details or {})


class PdfSnapshotMigrationService:
    """Scoped, fail-closed migration boundary over existing Research persistence.

    A prepared candidate is diagnostic/staged, NOT a verified active snapshot.
    There is intentionally no parser-setting-only rollback or implicit fallback
    to an older source. A caller must obtain fresh source bytes before rollback.
    """

    def __init__(
        self,
        *,
        documents: ResearchDocumentRepository,
        artifacts: ResearchArtifactStorePort | None,
        chunker: PaperDocumentChunker | None,
        artifact_reader: ResearchArtifactReadPort | None = None,
    ) -> None:
        self._documents = documents
        self._artifacts = artifacts
        self._chunker = chunker
        self._reader = artifact_reader

    def prepare_candidate(
        self,
        *,
        document: ResearchDocument,
        previous: ResearchDocument,
        actor_scope: Mapping[str, str],
        run_id: str,
    ) -> str:
        self._validate_scope(document, actor_scope)
        self._validate_scope(previous, actor_scope)
        if document.paper_id != previous.paper_id:
            raise PdfSnapshotMigrationBlocked("snapshot_paper_mismatch")
        if self._artifacts is None or self._chunker is None:
            raise PdfSnapshotMigrationBlocked("snapshot_staging_unavailable")
        # No resolve_chunk_ids/write: those operate on the active manifest and
        # can reuse ids from an incompatible projection. Namespace candidate ids.
        chunks = self._chunker.chunk(document, str(document.metadata.get("parse_source") or "pdf"))
        if not chunks or any(chunk.paper_id != document.paper_id for chunk in chunks):
            raise PdfSnapshotMigrationBlocked("snapshot_chunk_projection_invalid")
        old_ids = [chunk.chunk_id for chunk in chunks]
        if len(old_ids) != len(set(old_ids)):
            raise PdfSnapshotMigrationBlocked("snapshot_chunk_ids_not_unique")
        parse_checksum = document_content_checksum(document)
        ids = {value: f"pdf-candidate-{content_checksum([parse_checksum, value])}" for value in old_ids}
        payloads = []
        for chunk in chunks:
            payload = chunk.model_dump(mode="json", exclude_none=True)
            payload["chunk_id"] = ids[chunk.chunk_id]
            if chunk.parent_chunk_id:
                # Section-parent locators are not necessarily materialized chunks.
                payload["parent_chunk_id"] = ids.get(chunk.parent_chunk_id, chunk.parent_chunk_id)
            payload["references"] = [ids.get(ref, ref) for ref in chunk.references]
            payload["metadata"] = {
                **dict(payload.get("metadata") or {}),
                "actor_scope": dict(actor_scope),
                "document_id": document.document_id,
                "source_hash": document.source_hash,
                "parse_content_checksum": parse_checksum,
                "candidate_only": True,
            }
            payloads.append(payload)
        versions = dict(document.metadata.get("parse_version_fingerprints") or {})
        payload = {
            "schema_version": CANDIDATE_SCHEMA,
            "paper_id": document.paper_id,
            "actor_scope": dict(actor_scope),
            "source_hash": document.source_hash,
            "run_id": run_id,
            "document": document.model_dump(mode="json", exclude_none=True),
            "document_checksum": parse_checksum,
            "chunks": payloads,
            "chunks_checksum": content_checksum(payloads),
            "version_fingerprints": versions,
            "previous_document_checksum": document_content_checksum(previous),
            "previous_source_hash": previous.source_hash,
            "previous_snapshot_verified": False,
            "index_snapshot": None,
            "activation_status": "blocked",
            "blockers": [INDEX_BLOCKER],
        }
        return str(self._artifacts.publish(
            artifact_type=CANDIDATE_ARTIFACT_TYPE,
            payload=payload,
            metadata={
                "paper_id": document.paper_id,
                "actor_scope": dict(actor_scope),
                "source_hash": document.source_hash,
                "candidate_checksum": content_checksum(payload),
                "candidate_schema": CANDIDATE_SCHEMA,
                "activation_status": "blocked",
                "run_id": run_id,
            },
        ))

    def activate_candidate(
        self,
        candidate_ref: str,
        *,
        paper_id: str,
        current_source_hash: str,
        actor_scope: Mapping[str, str],
    ) -> None:
        self._check_reference(candidate_ref, paper_id=paper_id,
                              current_source_hash=current_source_hash, actor_scope=actor_scope)
        raise PdfSnapshotMigrationBlocked(INDEX_BLOCKER, details={"candidate_ref": candidate_ref})

    def rollback_to_snapshot(
        self,
        snapshot_ref: str,
        *,
        paper_id: str,
        current_source_hash: str,
        actor_scope: Mapping[str, str],
    ) -> None:
        """Reject changed sources and partial tuples without touching active state."""
        self._check_reference(snapshot_ref, paper_id=paper_id,
                              current_source_hash=current_source_hash, actor_scope=actor_scope)
        raise PdfSnapshotMigrationBlocked(INDEX_BLOCKER, details={"snapshot_ref": snapshot_ref})

    def _check_reference(
        self, ref: str, *, paper_id: str, current_source_hash: str,
        actor_scope: Mapping[str, str],
    ) -> None:
        if not current_source_hash or not actor_scope:
            raise PdfSnapshotMigrationBlocked("snapshot_identity_required")
        if self._reader is None:
            raise PdfSnapshotMigrationBlocked("snapshot_reader_unavailable")
        record = self._reader.read(ref, actor_scope=actor_scope)
        metadata = record.get("metadata") or {}
        if (record.get("artifactType") != CANDIDATE_ARTIFACT_TYPE
                or metadata.get("candidate_schema") != CANDIDATE_SCHEMA
                or metadata.get("paper_id") != paper_id):
            raise PdfSnapshotMigrationBlocked("snapshot_identity_mismatch")
        if metadata.get("source_hash") != current_source_hash:
            raise PdfSnapshotMigrationBlocked("snapshot_source_changed")
        current = self._documents.get(paper_id, actor_scope=actor_scope)
        if current is None:
            raise PdfSnapshotMigrationBlocked("snapshot_active_document_missing")
        self._validate_scope(current, actor_scope)

    @staticmethod
    def _validate_scope(document: ResearchDocument, actor_scope: Mapping[str, str]) -> None:
        if not actor_scope or not actor_scope_matches(document.actor_scope, actor_scope):
            raise PdfSnapshotMigrationBlocked("snapshot_scope_mismatch")
