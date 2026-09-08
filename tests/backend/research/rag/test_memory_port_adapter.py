from __future__ import annotations

from dataclasses import replace

import pytest

from backend.research.rag.adapters import ResearchRAGMemoryPort
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.memory import MemoryWriteCandidate, MemoryWriteStatus
from framework.memory import InMemoryMemoryStore, MemoryRuntime
from tests.fixtures.admitted_memory import admitted_memory


def _request(fixture, **overrides):
    return {
        "query": "ablation evidence", "namespace": fixture.metadata.namespace, "limit": 5,
        "execution_identity": fixture.graph.to_graph_execution_identity().to_dict(), **overrides,
    }


def test_rag_memory_returns_only_admitted_episodic_records_with_exact_lineage(tmp_path):
    fixture = admitted_memory(tmp_path)
    port = ResearchRAGMemoryPort(fixture.recall)
    hits = port.recall(_request(fixture, limit=100))
    assert [hit["memory_id"] for hit in hits] == ["mem/1"]
    assert hits[0]["memory_ref"] == f"{fixture.metadata.exact_ref}#record=mem%2F1"
    assert hits[0]["namespace_checksum"] == fixture.metadata.source_checksum
    assert hits[0]["input_snapshot_ref"] == fixture.root.snapshot_ref
    assert hits[0]["execution_identity"] == fixture.root.execution_identity.to_dict()
    assert hits[0]["content"].startswith("Prior paper ask")
    assert hits[0]["relevance"] > 0
    fixture.publisher.publish((replace(fixture.records[0], content="New current ablation evidence"),))
    assert port.recall(_request(fixture)) == hits


@pytest.mark.parametrize("overrides,code", [
    ({"execution_identity": None}, "REF_SNAPSHOT_BINDING_MISMATCH"),
    ({"execution_identity": {}}, "REF_SNAPSHOT_BINDING_MISMATCH"),
    ({"namespace": "research.private"}, "REF_UNAUTHORIZED"),
    ({"revision": "latest"}, "REF_UNAUTHORIZED"),
    ({"tenant_id": "another-tenant"}, "REF_UNAUTHORIZED"),
    ({"owner_id": "another-owner"}, "REF_UNAUTHORIZED"),
])
def test_rag_memory_rejects_ungranted_reads_before_namespace_io(tmp_path, monkeypatch, overrides, code):
    fixture = admitted_memory(tmp_path)
    monkeypatch.setattr(fixture.namespaces, "describe", lambda ref: pytest.fail("unauthorized metadata read"))
    monkeypatch.setattr(fixture.namespaces, "read", lambda ref: pytest.fail("unauthorized payload read"))
    with pytest.raises(HarnessValidationError) as error:
        ResearchRAGMemoryPort(fixture.recall).recall(_request(fixture, **overrides))
    assert error.value.code == code


def test_rag_memory_rejects_uncommitted_grant_and_mutable_runtime(tmp_path, monkeypatch):
    fixture = admitted_memory(tmp_path, commit=False)
    monkeypatch.setattr(fixture.namespaces, "read", lambda ref: pytest.fail("read without committed grant"))
    with pytest.raises(HarnessValidationError):
        ResearchRAGMemoryPort(fixture.recall).recall(_request(fixture))
    with pytest.raises(TypeError, match="execution-bound"):
        ResearchRAGMemoryPort(MemoryRuntime(InMemoryMemoryStore()))


def test_rag_memory_keeps_candidate_only_write_surface(tmp_path):
    fixture = admitted_memory(tmp_path)
    port = ResearchRAGMemoryPort(fixture.recall)
    candidate = MemoryWriteCandidate(
        candidate_id="candidate-1", namespace=fixture.metadata.namespace,
        content={"memory": "do not write from normal RAG recall"},
    )
    proposed = port.propose_write(candidate)
    assert proposed.status == MemoryWriteStatus.PROPOSED
    assert not any(hasattr(port, name) for name in ("commit_write", "store", "promote", "write"))
    assert fixture.namespaces.read(fixture.metadata.exact_ref).records() == tuple(sorted(fixture.records, key=lambda record: record.memory_id))


@pytest.mark.parametrize("field,value", [
    ("execution_identity", {}),
    ("input_snapshot_ref", None),
    ("record_namespace_refs", {}),
    ("namespace_checksums", {}),
    ("namespace_refs", []),
])
def test_rag_memory_refuses_results_with_lost_admission_lineage(tmp_path, monkeypatch, field, value):
    fixture = admitted_memory(tmp_path)
    result = fixture.recall.recall("ablation evidence")
    monkeypatch.setattr(fixture.recall, "recall", lambda *args, **kwargs: replace(
        result, diagnostics={**result.diagnostics, field: value},
    ))
    with pytest.raises(HarnessValidationError):
        ResearchRAGMemoryPort(fixture.recall).recall(_request(fixture))


def test_owner_selector_excludes_other_granted_owner_before_namespace_io(tmp_path, monkeypatch):
    fixture = admitted_memory(tmp_path, shared_owner=True)
    for name in ("describe", "read"):
        original = getattr(fixture.namespaces, name)

        def owner_only(ref, _original=original):
            assert ref != fixture.shared_metadata.exact_ref, "other owner namespace opened"
            return _original(ref)

        monkeypatch.setattr(fixture.namespaces, name, owner_only)
    hits = ResearchRAGMemoryPort(fixture.recall).recall(_request(fixture, namespace=None, owner_id="owner-1"))
    assert [hit["memory_id"] for hit in hits] == ["mem/1"]
