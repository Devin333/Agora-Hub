"""Compose durable reference admission with a trusted namespace revision store."""

from pathlib import Path

from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_snapshot import RefAuthoritySnapshotStorePort
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore


def build_harness_ref_admission_service(
    *, snapshot_store: RefAuthoritySnapshotStorePort,
    namespace_root: str | Path,
    namespace_refs: tuple[str, ...] = (),
) -> HarnessRefAdmissionService:
    """Exact refs come from trusted composition, never candidate namespace hints.

    The default contains no memory grant. Publishing a new immutable namespace
    does not make it readable until composition explicitly binds that revision
    to a newly admitted execution.
    """
    if (
        not isinstance(snapshot_store, RefAuthoritySnapshotStorePort)
        or getattr(snapshot_store, "is_durable", False) is not True
    ):
        raise TypeError("production reference admission requires durable snapshots")
    return HarnessRefAdmissionService(
        snapshot_store, memory_namespaces=FilesystemMemoryNamespaceStore(namespace_root),
        memory_namespace_refs=namespace_refs,
    )
