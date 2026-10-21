from __future__ import annotations

import json

from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.parse_cache_identity import (
    PARSE_CACHE_IDENTITY_SCHEMA,
    PARSE_NORMALIZER_VERSION,
    parse_cache_identity_fingerprint,
)
from backend.research.document.models import PaperChunk


def _cache_identity(source_hash: str, profile: str = "metadata") -> tuple[dict, str]:
    identity = {
        "schema": PARSE_CACHE_IDENTITY_SCHEMA,
        "source": {"hash": source_hash, "content_type": "application/pdf"},
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


def test_nested_actor_scope_uses_the_same_private_manifest_path(tmp_path) -> None:
    manager = ChunkManifestManager(tmp_path / "manifests")
    flat = {"tenant_id": "tenant-a", "user_id": "user-a"}
    nested = {"actor_scope": flat}
    assert manager.path_for("paper-1", actor_scope=flat) == manager.path_for("paper-1", actor_scope=nested)

    manager.write(
        "paper-1",
        [PaperChunk(
            chunk_id="chunk-1",
            paper_id="paper-1",
            parse_source="latex",
            content="method text",
            metadata={"source_locator": "paper://paper-1/method"},
        )],
        actor_scope=nested,
        document_id="document-1",
        source_hash="sha256:paper",
    )
    payload = json.loads(manager.path_for("paper-1", actor_scope=flat).read_text(encoding="utf-8"))
    assert payload["actor_scope"] == flat
    assert payload["chunks"][0]["actor_scope"] == flat


def test_chunk_manifest_rejects_stale_pipeline_snapshot(tmp_path) -> None:
    manager = ChunkManifestManager(tmp_path / "manifests")
    chunks = [PaperChunk(
        chunk_id="chunk-1",
        paper_id="paper-1",
        parse_source="latex",
        content="method text",
        metadata={"source_locator": "paper://paper-1/method"},
    )]
    common = {
        "document_id": "document-1",
        "source_hash": "sha256:paper",
        "actor_scope": {"tenant_id": "tenant-a"},
    }
    identity_a, pipeline_a = _cache_identity(common["source_hash"], "metadata")
    identity_b, pipeline_b = _cache_identity(common["source_hash"], "reading")
    path = manager.write(
        "paper-1",
        chunks,
        document_metadata={
            **common,
            "parse_cache_fingerprint": pipeline_a,
            "parse_cache_identity": identity_a,
        },
        actor_scope=common["actor_scope"],
        observed_at="2026-10-21T00:00:00Z",
    )
    first_id = json.loads(path.read_text(encoding="utf-8"))["manifest_id"]
    assert manager.is_current(
        "paper-1", **common, pipeline_fingerprint=pipeline_a
    )
    assert not manager.is_current(
        "paper-1", **common, pipeline_fingerprint=pipeline_b
    )

    manager.write(
        "paper-1",
        chunks,
        document_metadata={
            **common,
            "parse_cache_fingerprint": pipeline_b,
            "parse_cache_identity": identity_b,
        },
        actor_scope=common["actor_scope"],
        observed_at="2026-10-21T00:00:00Z",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["manifest_id"] != first_id
    assert payload["history"][0]["manifest_id"] == first_id
    assert payload["history"][0]["pipeline_fingerprint"] == pipeline_a
    assert manager.is_current(
        "paper-1", **common, pipeline_fingerprint=pipeline_b
    )


def test_chunk_id_reuse_requires_matching_complete_cache_identity(tmp_path) -> None:
    manager = ChunkManifestManager(tmp_path / "manifests")
    source_hash = "sha256:paper"
    identity_a, pipeline_a = _cache_identity(source_hash, "metadata")
    _identity_b, pipeline_b = _cache_identity(source_hash, "reading")
    scope = {"tenant_id": "tenant-a"}
    original = [PaperChunk(
        chunk_id="chunk-old",
        paper_id="paper-1",
        parse_source="latex",
        content="method text",
        metadata={"source_locator": "paper://paper-1/method"},
    )]
    original = manager.resolve_chunk_ids(
        "paper-1",
        original,
        document_id="document-1",
        source_hash=source_hash,
        pipeline_fingerprint=pipeline_a,
        actor_scope=scope,
    )
    manager.write(
        "paper-1",
        original,
        document_id="document-1",
        source_hash=source_hash,
        actor_scope=scope,
        document_metadata={
            "parse_cache_identity": identity_a,
            "parse_cache_fingerprint": pipeline_a,
        },
    )
    candidate = [PaperChunk(
        chunk_id="chunk-new",
        paper_id="paper-1",
        parse_source="latex",
        content="method text",
        metadata={"source_locator": "paper://paper-1/method"},
    )]

    matching = manager.resolve_chunk_ids(
        "paper-1",
        candidate,
        document_id="document-1",
        source_hash=source_hash,
        pipeline_fingerprint=pipeline_a,
        actor_scope=scope,
    )
    mismatched = manager.resolve_chunk_ids(
        "paper-1",
        candidate,
        document_id="document-1",
        source_hash=source_hash,
        pipeline_fingerprint=pipeline_b,
        actor_scope=scope,
    )

    assert matching[0].chunk_id == "chunk-old"
    assert mismatched[0].chunk_id == "chunk-new"
