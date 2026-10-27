from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from backend.research.domain import ResearchPaper
from backend.research.domain.catalog import ResearchPaperIdentity, ResearchSourceSnapshot, actor_scope_ref
from backend.research.domain.common import SourceLineage
from backend.research.document.chunk_manifest import ChunkManifestManager
from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from infrastructure.research.catalog_store import (
    CATALOG_STORE_SCHEMA_VERSION,
    FilesystemResearchCatalogStore,
    FilesystemResearchEventSink,
    ResearchCatalogArtifactNotFoundError,
    ResearchCatalogArtifactScopeError,
    ResearchCatalogStoreCorruptionError,
    ResearchCatalogStoreError,
    _checksum,
    persist_restore_provenance,
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


def test_catalog_store_concurrent_stale_identity_writers_preserve_snapshot_provenance(tmp_path) -> None:
    scope = {"user_id": "alice", "memory_namespace": "research:user:alice"}
    stores = [FilesystemResearchCatalogStore(tmp_path), FilesystemResearchCatalogStore(tmp_path)]
    identities = [
        ResearchPaperIdentity(
            paper_id="paper-shared",
            title="Shared local paper",
            source_snapshot_refs=[f"snapshot-{index}"],
            field_provenance={"title": [f"snapshot-{index}"]},
            metadata={"actor_scope": scope},
        )
        for index in range(2)
    ]
    ready = Barrier(2)

    def save(index: int) -> None:
        ready.wait()
        stores[index].save(identities[index])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, range(2)))

    stored = FilesystemResearchCatalogStore(tmp_path).get_identity(
        "paper-shared",
        actor_scope=scope,
    )
    assert stored is not None
    assert set(stored.source_snapshot_refs) == {"snapshot-0", "snapshot-1"}
    assert set(stored.source_snapshot_ids) == {"snapshot-0", "snapshot-1"}
    assert set(stored.field_provenance["title"]) == {"snapshot-0", "snapshot-1"}


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


@pytest.fixture
def long_event_sink_root(tmp_path: Path) -> Iterator[Path]:
    # Keep pytest's real root; only this fixture's owned tree is cleaned up.
    owned_root = resolve_storage_path(tmp_path / "event-sink-owned")
    parent = owned_root
    while len(os.fspath(parent)) <= 300:
        parent /= "long-event-storage-abcdefghijklmnopqrstuvwxyz"
    try:
        yield parent / "paper-catalog"
    finally:
        io_root = storage_io_path(owned_root)
        if io_root.exists():
            shutil.rmtree(io_root)


@pytest.fixture
def event_sink_scope() -> dict[str, str]:
    return {
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "memory_namespace": "research:tenant:tenant-a:user:user-a",
    }


@pytest.mark.parametrize("scoped", [False, True], ids=["public", "owner-scoped"])
def test_research_event_sink_long_path_finalize_reopens_and_replays(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
    scoped: bool,
) -> None:
    root = long_event_sink_root
    scope = event_sink_scope if scoped else {}
    run_id = "research-parse-" + "0123456789abcdef" * 2
    fingerprint = "request-fingerprint"
    sink = FilesystemResearchEventSink(root)
    digest = hashlib.sha256(actor_scope_ref(scope).encode("utf-8")).hexdigest()[:32]
    intent_path = root / "runs" / digest / f"{run_id}.intent.json"
    final_path = intent_path.with_name(f"{run_id}.final.json")
    events_root = root / "events" / digest if scoped else root / "events"
    events_path = events_root / f"{run_id}.jsonl"
    assert len(os.fspath(root)) > 260
    assert sink.root == root
    assert sink._run_record_path(run_id, scope, "intent") == intent_path
    assert not os.fspath(intent_path).startswith("\\\\?\\")
    assert sink.acquire_run_lease(
        run_id, request_fingerprint=fingerprint, actor_scope=scope
    ) == {"state": "owner", "run_id": run_id}
    intent_bytes = storage_io_path(intent_path).read_bytes()
    sink.append(run_id, {"event_id": "resolving", "to_status": "resolving", "actor_scope": scope})

    reopened = FilesystemResearchEventSink(root)
    lease = reopened.acquire_run_lease(
        run_id, request_fingerprint=fingerprint, actor_scope=scope
    )
    assert lease["state"] == "in_progress"
    assert lease["last_committed_phase"] == "resolving"
    reopened.append(
        run_id,
        {"event_id": "parsed", "to_status": "parsed", "terminal": True,
         "paper_id": "paper-long", "actor_scope": scope},
    )
    assert [event["sequence"] for event in reopened.read_history(run_id, actor_scope=scope)] == [1, 2]
    assert [intent["run_id"] for intent in reopened.scan_incomplete_runs()] == [run_id]
    payload = {
        "status": "parsed", "paper_id": "paper-long", "actor_scope": scope,
        "request_fingerprint": fingerprint, "source_snapshot_refs": ["snapshot-long"],
        "artifact_refs": ["artifact://research/research-document/" + "a" * 64],
    }
    reopened.finalize(run_id, payload)
    final_bytes = storage_io_path(final_path).read_bytes()
    events_bytes = storage_io_path(events_path).read_bytes()
    reopened.finalize(run_id, payload)

    restarted = FilesystemResearchEventSink(root)
    final = restarted.load_final(run_id, actor_scope=scope)
    assert final is not None
    assert final["run_id"] == run_id
    assert final["actor_scope"] == scope
    assert final["request_fingerprint"] == fingerprint
    assert final["paper_id"] == "paper-long"
    assert final["artifact_refs"] == payload["artifact_refs"]
    assert restarted.acquire_run_lease(
        run_id, request_fingerprint=fingerprint, actor_scope=scope
    ) == {"state": "completed", "final": final}
    assert restarted.scan_incomplete_runs() == ()
    assert restarted.recover_incomplete_runs() == ()
    assert storage_io_path(intent_path).read_bytes() == intent_bytes
    assert storage_io_path(final_path).read_bytes() == final_bytes
    assert storage_io_path(events_path).read_bytes() == events_bytes
    assert list(storage_io_path(root).rglob("*.tmp")) == []


@pytest.mark.parametrize("scope_field", ["tenant_id", "user_id", "memory_namespace"])
def test_research_event_sink_long_path_preserves_owner_and_conflict_admission(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
    scope_field: str,
) -> None:
    sink = FilesystemResearchEventSink(long_event_sink_root)
    scope = event_sink_scope
    other_scope = {**scope, scope_field: scope[scope_field] + "-other"}
    run_id = "run-owner-conflict"
    sink.create_run_intent(run_id, request_fingerprint="fp-a", actor_scope=scope)
    intent_path = sink._run_record_path(run_id, scope, "intent")
    intent_bytes = storage_io_path(intent_path).read_bytes()
    with pytest.raises(ResearchCatalogStoreError, match="intent conflicts"):
        sink.acquire_run_lease(run_id, request_fingerprint="fp-changed", actor_scope=scope)
    with pytest.raises(ResearchCatalogStoreError, match="conflicts with run intent"):
        sink.finalize(run_id, {"actor_scope": scope, "request_fingerprint": "fp-changed"})
    assert sink.load_final(run_id, actor_scope=other_scope) is None
    assert sink.read_history(run_id, actor_scope=other_scope) == ()
    with pytest.raises(ResearchCatalogStoreError, match="no run intent"):
        sink.finalize(run_id, {"actor_scope": other_scope, "request_fingerprint": "fp-a"})
    final_path = sink._run_record_path(run_id, scope, "final")
    assert not storage_io_path(final_path).exists()

    payload = {"status": "parsed", "paper_id": "paper-a", "actor_scope": scope,
               "request_fingerprint": "fp-a"}
    sink.finalize(run_id, payload)
    final_bytes = storage_io_path(final_path).read_bytes()
    with pytest.raises(ResearchCatalogStoreError, match="conflicts with existing run"):
        sink.finalize(run_id, {**payload, "paper_id": "paper-changed"})
    sink.create_run_intent(run_id, request_fingerprint="fp-b", actor_scope=other_scope)
    sink.finalize(run_id, {**payload, "actor_scope": other_scope,
                           "request_fingerprint": "fp-b", "paper_id": "paper-b"})

    reopened = FilesystemResearchEventSink(long_event_sink_root)
    for owner_scope, fingerprint, paper_id in [(scope, "fp-a", "paper-a"), (other_scope, "fp-b", "paper-b")]:
        lease = reopened.acquire_run_lease(run_id, request_fingerprint=fingerprint, actor_scope=owner_scope)
        assert lease["state"] == "completed"
        assert lease["final"]["actor_scope"] == owner_scope
        assert lease["final"]["paper_id"] == paper_id
    assert storage_io_path(intent_path).read_bytes() == intent_bytes
    assert storage_io_path(final_path).read_bytes() == final_bytes


@pytest.mark.parametrize("field", ["run_id", "actor_scope"])
def test_research_event_sink_long_path_rejects_tampered_intent_identity(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
    field: str,
) -> None:
    sink = FilesystemResearchEventSink(long_event_sink_root)
    run_id = "run-intent-identity"
    sink.create_run_intent(run_id, request_fingerprint="fp", actor_scope=event_sink_scope)
    path = storage_io_path(sink._run_record_path(run_id, event_sink_scope, "intent"))
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = "other-run" if field == "run_id" else {"user_id": "other-user"}
    path.write_text(json.dumps(record), encoding="utf-8")
    tampered_bytes = path.read_bytes()

    with pytest.raises(ResearchCatalogStoreError, match="intent conflicts"):
        FilesystemResearchEventSink(long_event_sink_root).acquire_run_lease(
            run_id, request_fingerprint="fp", actor_scope=event_sink_scope
        )
    assert path.read_bytes() == tampered_bytes
    assert not storage_io_path(sink._run_record_path(run_id, event_sink_scope, "final")).exists()


@pytest.mark.parametrize("record_kind", ["final", "recovery"])
@pytest.mark.parametrize("field", ["run_id", "actor_scope"])
def test_research_event_sink_long_path_rejects_tampered_result_identity(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
    record_kind: str,
    field: str,
) -> None:
    sink = FilesystemResearchEventSink(long_event_sink_root)
    run_id = "run-result-identity"
    sink.create_run_intent(run_id, request_fingerprint="fp", actor_scope=event_sink_scope)
    if record_kind == "final":
        sink.finalize(run_id, {"status": "parsed", "actor_scope": event_sink_scope})
    else:
        assert sink.recover_incomplete_runs()[0]["status"] == "quarantined"
    path = storage_io_path(sink._run_record_path(run_id, event_sink_scope, record_kind))
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = "other-run" if field == "run_id" else {"user_id": "other-user"}
    path.write_text(json.dumps(record), encoding="utf-8")
    tampered_bytes = path.read_bytes()

    reopened = FilesystemResearchEventSink(long_event_sink_root)
    if record_kind == "final":
        with pytest.raises(ResearchCatalogStoreCorruptionError, match="invalid identity"):
            reopened.load_final(run_id, actor_scope=event_sink_scope)
    with pytest.raises(ResearchCatalogStoreCorruptionError, match="invalid identity"):
        reopened.acquire_run_lease(run_id, request_fingerprint="fp", actor_scope=event_sink_scope)
    assert path.read_bytes() == tampered_bytes



def test_research_event_sink_long_path_recovers_terminal_history_after_restart(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
) -> None:
    sink = FilesystemResearchEventSink(long_event_sink_root)
    run_id = "run-terminal-replay"
    sink.create_run_intent(run_id, request_fingerprint="fp", actor_scope=event_sink_scope)
    sink.append(
        run_id,
        {"event_id": "terminal-event", "to_status": "parsed", "terminal": True,
         "paper_id": "paper-recovered", "source_snapshot_id": "snapshot-recovered",
         "actor_scope": event_sink_scope},
    )
    reopened = FilesystemResearchEventSink(long_event_sink_root)
    assert [record["run_id"] for record in reopened.scan_incomplete_runs()] == [run_id]
    outcomes = reopened.recover_incomplete_runs()
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "recovered"
    final = reopened.load_final(run_id, actor_scope=event_sink_scope)
    assert final is not None
    assert final["paper_id"] == "paper-recovered"
    assert final["source_snapshot_refs"] == ["snapshot-recovered"]
    assert final["recovered_from_event_id"] == "terminal-event"
    history = reopened.read_history(run_id, actor_scope=event_sink_scope)
    assert [event["sequence"] for event in history] == [1, 2]
    assert history[-1]["event_type"] == "research_parse_recovery"
    final_path = storage_io_path(reopened._run_record_path(run_id, event_sink_scope, "final"))
    final_bytes = final_path.read_bytes()

    restarted = FilesystemResearchEventSink(long_event_sink_root)
    assert restarted.acquire_run_lease(
        run_id, request_fingerprint="fp", actor_scope=event_sink_scope
    ) == {"state": "completed", "final": final}
    assert restarted.recover_incomplete_runs() == ()
    assert restarted.read_history(run_id, actor_scope=event_sink_scope) == history
    assert final_path.read_bytes() == final_bytes


@pytest.mark.parametrize(
    "corrupt_sequence", [False, True], ids=["missing-terminal", "sequence-gap"]
)
def test_research_event_sink_long_path_quarantine_remains_durable_after_restart(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
    corrupt_sequence: bool,
) -> None:
    sink = FilesystemResearchEventSink(long_event_sink_root)
    run_id = "run-quarantine"
    sink.create_run_intent(run_id, request_fingerprint="fp", actor_scope=event_sink_scope)
    sink.append(run_id, {"event_id": "resolving", "to_status": "resolving", "actor_scope": event_sink_scope})
    digest = hashlib.sha256(actor_scope_ref(event_sink_scope).encode("utf-8")).hexdigest()[:32]
    events_path = storage_io_path(long_event_sink_root / "events" / digest / f"{run_id}.jsonl")
    if corrupt_sequence:
        event = json.loads(events_path.read_text(encoding="utf-8"))
        event["sequence"] = 4
        events_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        with pytest.raises(ResearchCatalogStoreCorruptionError, match="sequence gap"):
            sink.read_history(run_id, actor_scope=event_sink_scope)
    outcomes = FilesystemResearchEventSink(long_event_sink_root).recover_incomplete_runs()
    assert len(outcomes) == 1
    recovery = outcomes[0]
    assert recovery["status"] == "quarantined"
    assert recovery["reason_code"] == (
        "event_transcript_corrupt" if corrupt_sequence else "terminal_event_missing"
    )
    recovery_path = storage_io_path(sink._run_record_path(run_id, event_sink_scope, "recovery"))
    recovery_bytes = recovery_path.read_bytes()
    events_bytes = events_path.read_bytes()

    reopened = FilesystemResearchEventSink(long_event_sink_root)
    assert reopened.load_final(run_id, actor_scope=event_sink_scope) is None
    assert reopened.acquire_run_lease(
        run_id, request_fingerprint="fp", actor_scope=event_sink_scope
    ) == {"state": "recovery_required", "recovery": recovery}
    assert reopened.recover_incomplete_runs() == (recovery,)
    assert recovery_path.read_bytes() == recovery_bytes
    assert events_path.read_bytes() == events_bytes


@pytest.mark.skipif(os.name != "nt", reason="Win32 extended-path input")
def test_research_event_sink_extended_root_exposes_only_logical_paths(
    long_event_sink_root: Path,
    event_sink_scope: dict[str, str],
) -> None:
    io_root = storage_io_path(long_event_sink_root)
    assert os.fspath(io_root).startswith("\\\\?\\")
    sink = FilesystemResearchEventSink(io_root)
    assert sink.root == long_event_sink_root
    assert not os.fspath(sink.root).startswith("\\\\?\\")
    run_id = "run-logical-root"
    target = sink._run_record_path(run_id, event_sink_scope, "intent")
    assert target.is_relative_to(long_event_sink_root)
    assert not os.fspath(target).startswith("\\\\?\\")
    sink.create_run_intent(run_id, request_fingerprint="fp", actor_scope=event_sink_scope)
    sink.finalize(run_id, {"status": "parsed", "actor_scope": event_sink_scope})
    final = FilesystemResearchEventSink(sink.root).load_final(run_id, actor_scope=event_sink_scope)
    assert final is not None
    assert final["actor_scope"] == event_sink_scope


def _chunk_manifest_fixture(root: Path, *, paper_id: str, actor_scope: dict[str, str]) -> Path:
    manager = ChunkManifestManager(root / "chunk-manifests")
    path = manager.path_for(paper_id, actor_scope=actor_scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"manifest_version":2}', encoding="utf-8")
    return path


def _legacy_artifact(store: FilesystemResearchCatalogStore, ref: str, legacy_path: str | Path) -> tuple[str, Path]:
    digest = ref.rsplit("/", 1)[-1]
    current = store.artifact_root / f"research-chunk-manifest-{digest[:32]}.json"
    envelope = json.loads(current.read_text(encoding="utf-8"))
    envelope["payload"]["manifest_path"] = str(legacy_path)
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    legacy_digest = hashlib.sha256(encoded).hexdigest()
    legacy_ref = f"artifact://research/research-chunk-manifest/{legacy_digest}"
    legacy_file = store.artifact_root / f"research-chunk-manifest-{legacy_digest[:32]}.json"
    legacy_file.write_text(json.dumps(envelope), encoding="utf-8")
    legacy_file.with_suffix(".commit.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "record_type": "research_artifact_commit_marker",
                "status": "committed",
                "artifact_ref": legacy_ref,
                "artifact_type": "research-chunk-manifest",
                "run_id": None,
                "content_checksum": legacy_digest,
                "created_at": "2026-10-13T00:00:00+00:00",
                "actor_scope": envelope["metadata"].get("actor_scope", {}),
            }
        ),
        encoding="utf-8",
    )
    return legacy_ref, legacy_file


def _noncanonical_absolute_path(path: Path, variant: str) -> str:
    if variant == "dot":
        return str(path.parent) + "\\.\\" + path.name
    if variant == "parent":
        return str(path.parent) + "\\other\\..\\" + path.name
    if variant == "duplicate_separator":
        return str(path.parent) + "\\\\" + path.name
    raise ValueError(f"unknown noncanonical path variant: {variant}")


def test_chunk_manifest_artifact_persists_portable_path_before_digest(tmp_path) -> None:
    scope = {"tenant_id": "tenant-a", "user_id": "user-a", "memory_namespace": "research:user-a"}
    store = FilesystemResearchCatalogStore(tmp_path)
    manifest = _chunk_manifest_fixture(tmp_path, paper_id="paper-1", actor_scope=scope)

    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )

    digest = ref.rsplit("/", 1)[-1]
    artifact = store.artifact_root / f"research-chunk-manifest-{digest[:32]}.json"
    persisted = json.loads(artifact.read_text(encoding="utf-8"))
    assert persisted["payload"]["manifest_path"].startswith("chunk-manifests/")
    assert not Path(persisted["payload"]["manifest_path"]).is_absolute()
    loaded = store.read(ref, actor_scope=scope, include_payload=True)
    assert loaded["payload"]["manifest_path"] == str(manifest.resolve())


def test_existing_absolute_chunk_manifest_under_current_owner_is_trusted(tmp_path) -> None:
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(tmp_path)
    manifest = _chunk_manifest_fixture(tmp_path, paper_id="paper-1", actor_scope=scope)
    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )
    legacy_ref, _ = _legacy_artifact(store, ref, manifest.resolve())

    loaded = store.read(legacy_ref, actor_scope=scope, include_payload=True)

    assert loaded["payload"]["manifest_path"] == str(manifest.resolve())


@pytest.mark.parametrize("variant", ["dot", "parent", "duplicate_separator"])
def test_existing_absolute_chunk_manifest_reader_rejects_noncanonical_path(
    tmp_path_factory: pytest.TempPathFactory,
    variant: str,
) -> None:
    root = tmp_path_factory.mktemp("legacyabs")
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(root)
    manifest = _chunk_manifest_fixture(root, paper_id="paper-1", actor_scope=scope)
    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )
    raw_path = _noncanonical_absolute_path(manifest, variant)
    legacy_ref, legacy_file = _legacy_artifact(store, ref, raw_path)
    legacy_marker = legacy_file.with_suffix(".commit.json")
    original_artifact_bytes = legacy_file.read_bytes()
    original_marker_bytes = legacy_marker.read_bytes()

    persisted = json.loads(legacy_file.read_text(encoding="utf-8"))
    assert persisted["payload"]["manifest_path"] == raw_path
    with pytest.raises(ResearchCatalogStoreCorruptionError, match="not canonical"):
        store.read(legacy_ref, actor_scope=scope, include_payload=True)
    assert legacy_file.read_bytes() == original_artifact_bytes
    assert legacy_marker.read_bytes() == original_marker_bytes


@pytest.mark.parametrize("variant", ["dot", "parent", "duplicate_separator"])
def test_chunk_manifest_publication_rejects_noncanonical_path(tmp_path, variant) -> None:
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(tmp_path)
    manifest = _chunk_manifest_fixture(tmp_path, paper_id="paper-1", actor_scope=scope)
    if variant == "dot":
        raw_path = str(manifest.parent) + "\\.\\" + manifest.name
    elif variant == "parent":
        raw_path = str(manifest.parent) + "\\other\\..\\" + manifest.name
    else:
        raw_path = str(manifest.parent) + "\\\\" + manifest.name

    with pytest.raises(ResearchCatalogStoreError, match="not canonical"):
        store.publish(
            artifact_type="research-chunk-manifest",
            payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": raw_path},
            metadata={"paper_id": "paper-1", "actor_scope": scope},
        )


def test_chunk_manifest_artifact_supports_relative_catalog_root(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    scope = {"user_id": "user-a"}
    root = Path("catalog")
    store = FilesystemResearchCatalogStore(root)
    manifest = _chunk_manifest_fixture(root, paper_id="paper-1", actor_scope=scope)

    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )

    loaded = store.read(ref, actor_scope=scope, include_payload=True)
    assert loaded["payload"]["manifest_path"] == str(manifest.resolve())


def test_store_scoped_artifact_is_never_visible_to_an_actor(tmp_path) -> None:
    store = FilesystemResearchCatalogStore(tmp_path)
    ref = store.publish(
        artifact_type="parser-execution-receipt",
        payload={"backend": "pymupdf"},
        metadata={"paper_id": "paper-1"},
    )

    assert store.read(ref, actor_scope={})["artifactRef"] == ref
    with pytest.raises(ResearchCatalogArtifactScopeError):
        store.read(ref, actor_scope={"user_id": "user-a", "memory_namespace": "research:user:user-a"})


def test_chunk_manifest_artifact_rejects_path_outside_bound_paper_scope(tmp_path) -> None:
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(tmp_path / "catalog")
    wrong = _chunk_manifest_fixture(tmp_path / "catalog", paper_id="paper-2", actor_scope=scope)

    with pytest.raises(ResearchCatalogStoreError, match="paper or actor scope"):
        store.publish(
            artifact_type="research-chunk-manifest",
            payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(wrong)},
            metadata={"paper_id": "paper-1", "actor_scope": scope},
        )


def test_chunk_manifest_artifact_without_external_manifest_remains_readable(tmp_path) -> None:
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(tmp_path)

    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "chunks": []},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )

    loaded = store.read(ref, actor_scope=scope, include_payload=True)
    assert "manifest_path" not in loaded["payload"]


def test_chunk_manifest_reader_rejects_signed_noncanonical_relative_path(tmp_path) -> None:
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(tmp_path)
    manifest = _chunk_manifest_fixture(tmp_path, paper_id="paper-1", actor_scope=scope)
    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )
    digest = ref.rsplit("/", 1)[-1]
    current = store.artifact_root / f"research-chunk-manifest-{digest[:32]}.json"
    envelope = json.loads(current.read_text(encoding="utf-8"))
    envelope["payload"]["manifest_path"] = envelope["payload"]["manifest_path"].replace("/", "//", 1)
    encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    invalid_digest = hashlib.sha256(encoded).hexdigest()
    invalid_ref = f"artifact://research/research-chunk-manifest/{invalid_digest}"
    invalid = store.artifact_root / f"research-chunk-manifest-{invalid_digest[:32]}.json"
    invalid.write_text(json.dumps(envelope), encoding="utf-8")
    invalid.with_suffix(".commit.json").write_text(
        json.dumps({"status": "committed", "artifact_ref": invalid_ref, "artifact_type": "research-chunk-manifest", "content_checksum": invalid_digest}),
        encoding="utf-8",
    )

    with pytest.raises(ResearchCatalogStoreCorruptionError, match="escapes the catalog owner"):
        store.read(invalid_ref, actor_scope=scope, include_payload=True)


def _resign_restore_provenance(root: Path, mutate) -> None:
    provenance_path = root / "_restore-provenance" / "manifest.json"
    marker_path = root / "_restore-provenance" / "manifest.commit.json"
    document = json.loads(provenance_path.read_text(encoding="utf-8"))
    mutate(document)
    unsigned = {key: value for key, value in document.items() if key != "content_checksum"}
    encoded = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    document["content_checksum"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["content_checksum"] = document["content_checksum"]
    provenance_path.write_text(json.dumps(document), encoding="utf-8")
    marker_path.write_text(json.dumps(marker), encoding="utf-8")


def _legacy_restore_provenance_fixture(tmp_path: Path):
    current_root = tmp_path / "restored-catalog"
    legacy_root = tmp_path / "offline-original-catalog"
    scope = {"tenant_id": "tenant-a", "user_id": "user-a", "memory_namespace": "research:user-a"}
    writer = FilesystemResearchCatalogStore(current_root)
    manifest = _chunk_manifest_fixture(current_root, paper_id="paper-1", actor_scope=scope)
    ref = writer.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )
    relative = manifest.relative_to(current_root)
    legacy_ref, legacy_file = _legacy_artifact(writer, ref, legacy_root / relative)
    portable_digest = ref.rsplit("/", 1)[-1]
    portable_file = writer.artifact_root / f"research-chunk-manifest-{portable_digest[:32]}.json"
    portable_file.unlink()
    portable_file.with_suffix(".commit.json").unlink()
    receipt = persist_restore_provenance(
        current_root,
        backup_id="backup-1",
        owner_id="research_paper_catalog",
        owner_schema="research-paper-catalog/v2",
        trusted_backup_origins=(legacy_root,),
    )
    return current_root, legacy_root, scope, manifest, legacy_ref, legacy_file, receipt


def test_restore_provenance_is_not_written_for_portable_catalog(tmp_path) -> None:
    root = tmp_path / "portable-catalog"
    scope = {"user_id": "user-a"}
    store = FilesystemResearchCatalogStore(root)
    manifest = _chunk_manifest_fixture(root, paper_id="paper-1", actor_scope=scope)
    store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )

    receipt = persist_restore_provenance(
        root,
        backup_id="backup-1",
        owner_id="research_paper_catalog",
        owner_schema="research-paper-catalog/v2",
        trusted_backup_origins=(tmp_path / "unused-origin",),
    )

    assert receipt == {
        "schema": "newsroom.research-catalog-restore-provenance/v1",
        "status": "not_required",
        "entry_count": 0,
    }
    assert not (root / "_restore-provenance").exists()


@pytest.mark.parametrize("variant", ["dot", "parent", "duplicate_separator"])
def test_restore_provenance_rejects_noncanonical_absolute_legacy_path(
    tmp_path_factory: pytest.TempPathFactory,
    variant: str,
) -> None:
    base = tmp_path_factory.mktemp("legacyabs")
    current_root = base / "current"
    legacy_root = base / "legacy"
    scope = {"tenant_id": "tenant-a", "user_id": "user-a", "memory_namespace": "research:user-a"}
    store = FilesystemResearchCatalogStore(current_root)
    manifest = _chunk_manifest_fixture(current_root, paper_id="paper-1", actor_scope=scope)
    ref = store.publish(
        artifact_type="research-chunk-manifest",
        payload={"paper_id": "paper-1", "document_id": "document-1", "actor_scope": scope, "manifest_path": str(manifest)},
        metadata={"paper_id": "paper-1", "actor_scope": scope},
    )
    legacy_manifest = legacy_root / manifest.relative_to(current_root)
    raw_path = _noncanonical_absolute_path(legacy_manifest, variant)
    _legacy_ref, legacy_file = _legacy_artifact(store, ref, raw_path)
    legacy_marker = legacy_file.with_suffix(".commit.json")
    original_artifact_bytes = legacy_file.read_bytes()
    original_marker_bytes = legacy_marker.read_bytes()

    persisted = json.loads(legacy_file.read_text(encoding="utf-8"))
    assert persisted["payload"]["manifest_path"] == raw_path
    with pytest.raises(ResearchCatalogStoreCorruptionError, match="not canonical"):
        persist_restore_provenance(
            current_root,
            backup_id="backup-1",
            owner_id="research_paper_catalog",
            owner_schema="research-paper-catalog/v2",
            trusted_backup_origins=(legacy_root,),
        )
    assert legacy_file.read_bytes() == original_artifact_bytes
    assert legacy_marker.read_bytes() == original_marker_bytes
    assert not (current_root / "_restore-provenance").exists()


def test_legacy_chunk_manifest_read_consumes_persisted_restore_provenance(tmp_path) -> None:
    current_root, legacy_root, scope, manifest, legacy_ref, legacy_file, receipt = _legacy_restore_provenance_fixture(tmp_path)
    original_bytes = legacy_file.read_bytes()

    loaded = FilesystemResearchCatalogStore(current_root).read(
        legacy_ref,
        actor_scope=scope,
        include_payload=True,
    )

    assert receipt["entry_count"] == 1
    assert loaded["payload"]["manifest_path"] == str(manifest.resolve())
    assert legacy_file.read_bytes() == original_bytes
    assert not legacy_root.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda entry: entry.__setitem__("target_relative_path", "../foreign/chunk_manifest.json"),
        lambda entry: entry.__setitem__("paper_id", "paper-2"),
        lambda entry: entry.__setitem__("actor_scope", {"user_id": "user-b"}),
        lambda entry: entry.__setitem__("content_checksum", "sha256:" + "0" * 64),
        lambda entry: entry.__setitem__("artifact_file_sha256", "sha256:" + "0" * 64),
        lambda entry: entry.__setitem__("marker_file_sha256", "sha256:" + "0" * 64),
        lambda entry: entry.__setitem__("legacy_manifest_path_sha256", "sha256:" + "0" * 64),
    ],
    ids=("foreign-target", "wrong-paper", "wrong-scope", "wrong-digest", "artifact-tamper", "marker-tamper", "wrong-legacy-root"),
)
def test_legacy_chunk_manifest_restore_provenance_semantic_tamper_fails_closed(tmp_path, mutation) -> None:
    current_root, _legacy_root, scope, _manifest, legacy_ref, _legacy_file, _receipt = _legacy_restore_provenance_fixture(tmp_path)

    def mutate(document: dict) -> None:
        mutation(document["entries"][legacy_ref])

    _resign_restore_provenance(current_root, mutate)

    with pytest.raises(ResearchCatalogStoreCorruptionError):
        FilesystemResearchCatalogStore(current_root).read(
            legacy_ref,
            actor_scope=scope,
            include_payload=True,
        )
