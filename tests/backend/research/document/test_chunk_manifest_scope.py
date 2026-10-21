from __future__ import annotations

import json

from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.models import PaperChunk


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
    path = manager.write(
        "paper-1",
        chunks,
        document_metadata={**common, "parse_cache_fingerprint": "pipeline-a"},
        actor_scope=common["actor_scope"],
        observed_at="2026-10-21T00:00:00Z",
    )
    first_id = json.loads(path.read_text(encoding="utf-8"))["manifest_id"]
    assert manager.is_current(
        "paper-1", **common, pipeline_fingerprint="pipeline-a"
    )
    assert not manager.is_current(
        "paper-1", **common, pipeline_fingerprint="pipeline-b"
    )

    manager.write(
        "paper-1",
        chunks,
        document_metadata={**common, "parse_cache_fingerprint": "pipeline-b"},
        actor_scope=common["actor_scope"],
        observed_at="2026-10-21T00:00:00Z",
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["manifest_id"] != first_id
    assert payload["history"][0]["manifest_id"] == first_id
    assert payload["history"][0]["pipeline_fingerprint"] == "pipeline-a"
    assert manager.is_current(
        "paper-1", **common, pipeline_fingerprint="pipeline-b"
    )
