from dataclasses import replace

import pytest

from framework.memory.exceptions import MemoryPolicyDenied
from framework.memory.models import MemoryRecord
from framework.memory.namespace import MemoryNamespaceDescriptor, MemoryNamespaceError, MemoryNamespacePublisher, MemoryNamespaceRevision
from framework.memory.policy import MemoryPolicy
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore


def _record():
    return MemoryRecord(memory_id="note", content="verified note", namespace="research.public",
                        tenant_id="tenant-1", actor="owner-1", refs={"evidence_id": "evidence-1"})


def _revision(records):
    return MemoryNamespaceRevision.create(namespace="research.public", tenant_id="tenant-1",
                                          owner_id="owner-1", shared_read_only=True, records=records)


def test_full_record_content_and_scope_determine_revision_not_record_version():
    record = _record()
    first = _revision((record,))
    second = _revision((replace(record, content="different note", version=record.version),))
    assert first.descriptor.revision != second.descriptor.revision
    assert MemoryNamespaceDescriptor.from_dict(first.descriptor.to_dict()) == first.descriptor
    assert len(first.descriptor.revision) == 64
    restored = first.records()[0]
    restored.metadata["external mutation"] = True
    assert first.records()[0].metadata == {}
    assert "content" not in first.descriptor.to_dict()
    assert "verified note" not in str(first.descriptor.to_dict())


@pytest.mark.parametrize("field,value", (("actor", "sibling"), ("tenant_id", "other"), ("namespace", "private")))
def test_publication_rejects_records_outside_trusted_scope_before_store_write(tmp_path, field, value):
    store = FilesystemMemoryNamespaceStore(tmp_path)
    publisher = MemoryNamespacePublisher(store, namespace="research.public", tenant_id="tenant-1",
                                          owner_id="owner-1", shared_read_only=True, policy=MemoryPolicy())
    with pytest.raises(MemoryNamespaceError):
        publisher.publish((replace(_record(), **{field: value}),))
    assert not tuple(tmp_path.iterdir())


def test_publication_retains_existing_memory_policy_and_does_not_write_partial_batch(tmp_path):
    store = FilesystemMemoryNamespaceStore(tmp_path)
    publisher = MemoryNamespacePublisher(store, namespace="research.public", tenant_id="tenant-1",
                                          owner_id="owner-1", shared_read_only=False,
                                          policy=MemoryPolicy(min_confidence_to_write=0.8))
    with pytest.raises(MemoryPolicyDenied):
        publisher.publish((_record(), replace(_record(), memory_id="rejected", confidence=0.2)))
    assert not tuple(tmp_path.iterdir())
    disabled = MemoryNamespacePublisher(store, namespace="research.public", tenant_id="tenant-1",
                                         owner_id="owner-1", shared_read_only=False, policy=MemoryPolicy(allow_write=False))
    with pytest.raises(MemoryPolicyDenied):
        disabled.publish(())


def test_duplicate_records_and_descriptor_field_tampering_are_rejected():
    record = _record()
    with pytest.raises(MemoryNamespaceError):
        _revision((record, record))
    descriptor = _revision((record,)).descriptor
    for change in ({"owner_id": "sibling"}, {"extra": "field"}, {"revision": "latest"}):
        with pytest.raises(MemoryNamespaceError):
            MemoryNamespaceDescriptor.from_dict({**descriptor.to_dict(), **change})
