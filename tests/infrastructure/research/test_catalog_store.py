from __future__ import annotations

import json

import pytest

from backend.research.domain import ResearchPaper
from backend.research.domain.catalog import ResearchSourceSnapshot
from backend.research.domain.common import SourceLineage
from infrastructure.research.catalog_store import (
    CATALOG_STORE_SCHEMA_VERSION,
    FilesystemResearchCatalogStore,
    FilesystemResearchEventSink,
    ResearchCatalogArtifactNotFoundError,
    ResearchCatalogArtifactScopeError,
    ResearchCatalogStoreError,
    _checksum,
)


def test_catalog_store_migrates_legacy_state_and_persists_schema(tmp_path) -> None:
    store = FilesystemResearchCatalogStore(tmp_path)
    store.save(
        ResearchPaper(
            paper_id="paper-1",
            title="A paper",
            metadata={"actor_scope": {"tenant_id": "tenant-a"}},
        )
    )

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["schema_version"] = 1
    payload.pop("sota_claims", None)
    unsigned = {key: value for key, value in payload.items() if key != "checksum"}
    payload["checksum"] = _checksum(unsigned)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.get_paper("paper-1", actor_scope={"tenant_id": "tenant-a"}) is not None
    migrated = json.loads(store.path.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == CATALOG_STORE_SCHEMA_VERSION
    assert migrated["sota_claims"] == {}
    assert migrated["checksum"] == _checksum({key: value for key, value in migrated.items() if key != "checksum"})


def test_catalog_store_keeps_actor_scopes_isolated(tmp_path) -> None:
    store = FilesystemResearchCatalogStore(tmp_path)
    store.save(ResearchPaper(paper_id="paper-a", title="A", metadata={"actor_scope": {"tenant_id": "a"}}))
    store.save(ResearchPaper(paper_id="paper-b", title="B", metadata={"actor_scope": {"tenant_id": "b"}}))

    assert store.get_paper("paper-a", actor_scope={"tenant_id": "b"}) is None
    assert store.get_paper("paper-a", actor_scope={"tenant_id": "a"}) is not None


def test_snapshot_roundtrip_preserves_immutable_content_with_flat_actor_scope(tmp_path) -> None:
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    snapshot = ResearchSourceSnapshot(snapshot_id="source-1", paper_id="paper-1", external_id="paper.pdf", source_type="local", source_hash="abc", lineage=SourceLineage(source_refs=["paper://paper-1/pdf"], source_hash="abc"))
    snapshot = snapshot.model_copy(update={"actor_scope": scope, "metadata": dict(scope), "lineage": snapshot.lineage.model_copy(update={"metadata": dict(scope)})})
    store = FilesystemResearchCatalogStore(tmp_path)
    store.save(snapshot)
    original = store.path.read_bytes()
    reopened = FilesystemResearchCatalogStore(tmp_path)
    restored = reopened.get_snapshot("source-1", actor_scope=scope)
    assert restored is not None
    reopened.save(restored)
    assert store.path.read_bytes() == original
    assert reopened.get_snapshot("source-1", actor_scope={"user_id": "bob"}) is None
    with pytest.raises(ResearchCatalogStoreError, match="immutable"):
        reopened.save(restored.model_copy(update={"source_hash": "different", "checksum": "different"}))


def test_existing_flat_snapshot_normalization_does_not_rewrite_observation() -> None:
    from copy import deepcopy
    from infrastructure.research.catalog_store import _put_immutable_snapshot

    raw = ResearchSourceSnapshot(snapshot_id="source-1", paper_id="paper-1", external_id="paper.pdf", lineage=SourceLineage(source_refs=["paper://paper-1/pdf"])).model_dump(mode="json", exclude_none=True)
    raw["actor_scope"] = {"user_id": "alice"}
    raw["metadata"] = {"user_id": "alice"}
    raw["lineage"]["metadata"] = {"user_id": "alice"}
    state = {"snapshots": {"source-1": deepcopy(raw)}}
    restored = ResearchSourceSnapshot.model_validate(raw)
    _put_immutable_snapshot(state, "source-1", restored)
    assert state["snapshots"]["source-1"] == raw
    with pytest.raises(ResearchCatalogStoreError, match="immutable"):
        _put_immutable_snapshot(state, "source-1", restored.model_copy(update={"metadata": {**restored.metadata, "new_claim": "changed"}}))


def test_catalog_artifact_read_requires_scope_and_commit_marker(tmp_path) -> None:
    store = FilesystemResearchCatalogStore(tmp_path)
    ref = store.publish(
        artifact_type="research-document",
        payload={"text": "paper body"},
        metadata={
            "paper_id": "paper-1",
            "actor_scope": {
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "memory_namespace": "research:tenant:tenant-a:user:user-a",
            },
        },
    )

    summary = store.read(
        ref,
        actor_scope={
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "memory_namespace": "research:tenant:tenant-a:user:user-a",
        },
    )
    assert summary["artifactRef"] == ref
    assert "payload" not in summary
    assert store.read(
        ref,
        actor_scope={
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "memory_namespace": "research:tenant:tenant-a:user:user-a",
        },
        include_payload=True,
    )["payload"] == {"text": "paper body"}

    try:
        store.read(ref, actor_scope={"tenant_id": "tenant-b"})
    except ResearchCatalogArtifactScopeError:
        pass
    else:
        raise AssertionError("cross-tenant artifact read must be denied")

    try:
        store.read("artifact://research/research-document/" + "0" * 64, actor_scope={"tenant_id": "tenant-a"})
    except ResearchCatalogArtifactNotFoundError:
        pass
    else:
        raise AssertionError("unknown artifact must be hidden")


def test_research_event_sink_assigns_sequences_and_scans_incomplete_runs(tmp_path) -> None:
    sink = FilesystemResearchEventSink(tmp_path)
    scope = {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "memory_namespace": "research:tenant:tenant-a:user:user-a",
    }
    sink.create_run_intent("run-1", request_fingerprint="fp", actor_scope=scope)
    sink.append("run-1", {"event_id": "run-1:e1", "event_type": "research_parse_phase", "actor_scope": scope})
    sink.append("run-1", {"event_id": "run-1:e2", "event_type": "research_parse_phase", "actor_scope": scope})

    history = sink.read_history("run-1", actor_scope=scope)
    assert [item["sequence"] for item in history] == [1, 2]
    assert sink.scan_incomplete_runs()[0]["run_id"] == "run-1"

    sink.finalize("run-1", {"status": "parsed", "actor_scope": scope})
    assert sink.scan_incomplete_runs() == ()


def test_research_event_sink_lease_replays_completion_without_new_owner(tmp_path) -> None:
    sink = FilesystemResearchEventSink(tmp_path)
    scope = {"tenant_id": "tenant-a"}

    assert sink.acquire_run_lease("run-lease", request_fingerprint="fp-a", actor_scope=scope)["state"] == "owner"
    assert sink.acquire_run_lease("run-lease", request_fingerprint="fp-a", actor_scope=scope)["state"] == "in_progress"
    with pytest.raises(ResearchCatalogStoreError, match="conflicts"):
        sink.acquire_run_lease("run-lease", request_fingerprint="fp-b", actor_scope=scope)

    sink.finalize(
        "run-lease",
        {
            "status": "metadata_only",
            "paper_id": "paper-lease",
            "actor_scope": scope,
            "request_fingerprint": "fp-a",
        },
    )

    replay = sink.acquire_run_lease("run-lease", request_fingerprint="fp-a", actor_scope=scope)
    assert replay["state"] == "completed"
    assert replay["final"]["paper_id"] == "paper-lease"


def test_research_event_sink_replays_terminal_event_without_reexecuting_run(tmp_path) -> None:
    sink = FilesystemResearchEventSink(tmp_path)
    scope = {"tenant_id": "tenant-a"}
    sink.acquire_run_lease("run-recovery", request_fingerprint="fp", actor_scope=scope)
    sink.append(
        "run-recovery",
        {
            "event_id": "run-recovery:parsed",
            "event_type": "research_parse_phase",
            "status": "parsed",
            "to_status": "parsed",
            "paper_id": "paper-recovery",
            "source_snapshot_id": "snapshot-recovery",
            "artifact_refs": ["artifact://research/research-document/" + "a" * 64],
            "terminal": True,
            "actor_scope": scope,
        },
    )

    outcomes = sink.recover_incomplete_runs()

    assert outcomes[0]["status"] == "recovered"
    final = sink.load_final("run-recovery", actor_scope=scope)
    assert final is not None
    assert final["paper_id"] == "paper-recovery"
    assert sink.acquire_run_lease("run-recovery", request_fingerprint="fp", actor_scope=scope)["state"] == "completed"


def test_research_event_sink_quarantines_run_without_terminal_event(tmp_path) -> None:
    sink = FilesystemResearchEventSink(tmp_path)
    scope = {"tenant_id": "tenant-a"}
    sink.acquire_run_lease("run-orphan", request_fingerprint="fp", actor_scope=scope)
    sink.append(
        "run-orphan",
        {
            "event_id": "run-orphan:resolving",
            "event_type": "research_parse_phase",
            "status": "resolving",
            "to_status": "resolving",
            "actor_scope": scope,
        },
    )

    outcomes = sink.recover_incomplete_runs()

    assert outcomes[0]["status"] == "quarantined"
    lease = sink.acquire_run_lease("run-orphan", request_fingerprint="fp", actor_scope=scope)
    assert lease["state"] == "recovery_required"


def test_research_event_sink_does_not_treat_intermediate_parsed_event_as_terminal(tmp_path) -> None:
    sink = FilesystemResearchEventSink(tmp_path)
    scope = {"tenant_id": "tenant-a"}
    sink.acquire_run_lease("run-intermediate", request_fingerprint="fp", actor_scope=scope)
    sink.append(
        "run-intermediate",
        {
            "event_id": "run-intermediate:parsed",
            "event_type": "research_parse_phase",
            "status": "parsed",
            "to_status": "parsed",
            "paper_id": "paper-intermediate",
            "source_snapshot_id": "snapshot-intermediate",
            "actor_scope": scope,
        },
    )

    outcomes = sink.recover_incomplete_runs()

    assert outcomes[0]["status"] == "quarantined"
    assert outcomes[0]["reason_code"] == "terminal_event_missing"
