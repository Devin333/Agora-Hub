from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import RefAuthority, RefDescriptor
from framework.harness.ref_memory import HarnessMemoryNamespaceReader
from framework.memory.models import MemoryRecord
from framework.memory.namespace import MemoryNamespacePublisher, MemoryNamespaceError
from framework.memory.policy import MemoryPolicy
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from tests.framework.harness.test_ref_snapshot_store import _snapshot, _store


def _setup(tmp_path, *, commit_grant=True):
    snapshots, events = _store(tmp_path)
    namespaces = FilesystemMemoryNamespaceStore(tmp_path / "namespaces")
    root = _snapshot()
    publisher = MemoryNamespacePublisher(
        namespaces, namespace="research.analysis", tenant_id=root.policy.tenant_id,
        owner_id=root.policy.owner_id, shared_read_only=False, policy=MemoryPolicy(),
    )
    metadata = publisher.publish((MemoryRecord(
        content="evidence-bound note", memory_id="note-1", namespace="research.analysis",
        tenant_id=root.policy.tenant_id, actor=root.policy.owner_id, refs={"evidence_id": "evidence-1"},
    ),))
    descriptor = RefDescriptor.memory(
        namespace=metadata.namespace, ref=metadata.exact_ref,
        run_id=root.run_id, stage_id=root.stage_id, tenant_id=metadata.tenant_id,
        owner_id=metadata.owner_id, source_checksum=metadata.source_checksum,
    )
    descriptors = (*root.descriptors, descriptor)
    root = replace(root, descriptors=descriptors, policy=replace(
        root.policy, allowed_refs=tuple(item.ref for item in descriptors),
        allowed_ref_kinds=("input", "memory"), allowed_artifact_types=("graph_input", "memory_namespace"),
        allowed_memory_namespaces=(metadata.namespace,), pinned_checksums={item.ref: item.source_checksum for item in descriptors},
    ))
    if commit_grant:
        snapshots.commit(root)
    reader = HarnessMemoryNamespaceReader(
        snapshot=root, snapshot_store=snapshots, namespace_store=namespaces,
        execution_identity=root.execution_identity,
    )
    return root, metadata, reader, namespaces, snapshots, events


def test_memory_reads_require_committed_authority_before_metadata_or_payload(tmp_path, monkeypatch):
    root, metadata, reader, namespaces, snapshots, events = _setup(tmp_path, commit_grant=False)
    reads = []
    original_describe = namespaces.describe
    original_read = namespaces.read

    def describe(ref):
        reads.append("metadata")
        assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
        return original_describe(ref)

    def read(ref):
        reads.append("payload")
        assert reads[0] == "metadata"
        return original_read(ref)

    monkeypatch.setattr(namespaces, "describe", describe)
    monkeypatch.setattr(namespaces, "read", read)
    with pytest.raises(HarnessValidationError):
        reader.read(metadata.exact_ref)
    assert reads == []
    snapshots.commit(root)
    assert reader.read(metadata.exact_ref).records()[0].content == "evidence-bound note"
    assert reads[:2] == ["metadata", "payload"]
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
    with pytest.raises(HarnessValidationError):
        RefAuthority().authorize_memory_namespace_ref(
            metadata.exact_ref, root.policy,
            descriptors={item.ref: item for item in root.descriptors}, requested_access="READ_WRITE",
        )


@pytest.mark.parametrize("field,value", (("tenant_id", "other-tenant"), ("owner_id", "sibling-owner"),
                                        ("namespace", "research.private"), ("shared_read_only", True)))
def test_metadata_scope_replacement_fails_before_memory_payload(tmp_path, monkeypatch, field, value):
    _, metadata, reader, namespaces, _, _ = _setup(tmp_path)
    monkeypatch.setattr(namespaces, "describe", lambda ref: replace(metadata, **{field: value}))
    monkeypatch.setattr(namespaces, "read", lambda ref: pytest.fail("unauthorized payload read"))
    with pytest.raises(HarnessValidationError) as error:
        reader.read(metadata.exact_ref)
    assert error.value.code == "REF_CHECKSUM_MISMATCH"


@pytest.mark.parametrize("field,value", (("run_id", "other-run"), ("activity_id", "other-activity"),
                                        ("node_instance_id", "other-node"), ("attempt", 2)))
def test_reader_requires_callers_exact_physical_execution(tmp_path, monkeypatch, field, value):
    root, _, _, namespaces, snapshots, _ = _setup(tmp_path)
    monkeypatch.setattr(namespaces, "describe", lambda ref: pytest.fail("cross-execution metadata read"))
    with pytest.raises(HarnessValidationError) as error:
        HarnessMemoryNamespaceReader(
            snapshot=root, snapshot_store=snapshots, namespace_store=namespaces,
            execution_identity=replace(root.execution_identity, **{field: value}),
        )
    assert error.value.code == "REF_SNAPSHOT_BINDING_MISMATCH"


def test_unpinned_new_revision_and_logical_name_cannot_redirect_reader(tmp_path, monkeypatch):
    _, metadata, reader, namespaces, _, _ = _setup(tmp_path)
    monkeypatch.setattr(namespaces, "describe", lambda ref: pytest.fail("unadmitted namespace read"))
    with pytest.raises(HarnessValidationError):
        reader.read("memory-namespace://" + "f" * 64)
    with pytest.raises(MemoryNamespaceError):
        reader.read(metadata.namespace)


def test_reader_restores_exact_revision_with_no_new_events(tmp_path):
    root, metadata, _, _, _, events = _setup(tmp_path)
    reopened, _ = _store(tmp_path)
    reader = HarnessMemoryNamespaceReader(
        snapshot=root, snapshot_store=reopened,
        namespace_store=FilesystemMemoryNamespaceStore(tmp_path / "namespaces"),
        execution_identity=root.execution_identity,
    )
    assert reader.read(metadata.exact_ref).records()[0].memory_id == "note-1"
    assert events.get_stream_high_watermark("run:ref-run", tenant_id="control") == 1
