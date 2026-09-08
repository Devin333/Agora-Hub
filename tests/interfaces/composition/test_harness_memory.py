from types import SimpleNamespace

import pytest

from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.memory.namespace import MemoryNamespaceError
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from interfaces.composition.harness_memory import build_harness_ref_admission_service
from tests.framework.harness.test_ref_snapshot_store import _store


def test_composition_uses_real_durable_namespace_store_and_no_implicit_memory_grants(tmp_path):
    snapshots, _ = _store(tmp_path)
    admission = build_harness_ref_admission_service(
        snapshot_store=snapshots, namespace_root=tmp_path / "namespaces",
    )
    assert admission.store is snapshots
    assert isinstance(admission.memory_namespaces, FilesystemMemoryNamespaceStore)
    assert admission.memory_namespace_refs == ()


def test_production_composition_rejects_missing_or_volatile_authority(tmp_path):
    snapshots, _ = _store(tmp_path)
    volatile = SimpleNamespace(commit=snapshots.commit, find=snapshots.find, get=snapshots.get)
    for invalid in (None, volatile):
        with pytest.raises(TypeError, match="durable"):
            build_harness_ref_admission_service(snapshot_store=invalid, namespace_root=tmp_path / "namespaces")
    with pytest.raises(TypeError):
        HarnessRefAdmissionService(snapshots, memory_namespace_refs=("memory-namespace://" + "a" * 64,))
    with pytest.raises(TypeError):
        HarnessRefAdmissionService(snapshots, memory_namespaces=SimpleNamespace(describe=lambda ref: None))
    with pytest.raises(MemoryNamespaceError):
        build_harness_ref_admission_service(snapshot_store=snapshots, namespace_root=tmp_path / "namespaces", namespace_refs=("research.analysis",))
