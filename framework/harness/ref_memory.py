"""Authorize exact immutable memory revisions before accessing their records."""

from __future__ import annotations

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import RefAuthority, RefDescriptor, RefScope
from framework.harness.ref_snapshot import (
    RefAuthoritySnapshot,
    RefAuthoritySnapshotStorePort,
    RefSnapshotPhase,
    SnapshotRefResolutionPort,
)
from framework.harness.subagents.transcript import SubAgentAttemptIdentity
from framework.memory.namespace import (
    MemoryNamespaceDescriptor,
    MemoryNamespaceRevision,
    MemoryNamespaceStorePort,
    namespace_revision,
)
from framework.shared.graph_identity import GraphExecutionIdentity


class HarnessMemoryNamespaceReader:
    """Read-only authority bound to a caller's admitted execution/child attempt.

    Neither namespace lookup nor receipt replay issues new grants. The raw
    namespace store is accessible only after canonical grant and metadata checks.
    """

    def __init__(
        self,
        *,
        snapshot: RefAuthoritySnapshot,
        snapshot_store: RefAuthoritySnapshotStorePort,
        namespace_store: MemoryNamespaceStorePort,
        execution_identity: GraphExecutionIdentity,
        attempt_identity: SubAgentAttemptIdentity | None = None,
    ) -> None:
        if (
            not isinstance(snapshot, RefAuthoritySnapshot)
            or not isinstance(snapshot_store, RefAuthoritySnapshotStorePort)
        ):
            raise TypeError("memory reader requires an admitted reference snapshot")
        if (
            not isinstance(namespace_store, MemoryNamespaceStorePort)
            or getattr(namespace_store, "is_durable", False) is not True
        ):
            raise TypeError("memory reader requires durable namespace storage")
        if getattr(snapshot_store, "is_durable", False) is not True:
            raise TypeError("memory reader requires durable reference authority")
        if (
            snapshot.execution_identity != execution_identity
            or snapshot.attempt_identity != attempt_identity
            or snapshot.phase
            not in {RefSnapshotPhase.INPUT_ADMISSION, RefSnapshotPhase.CHILD_INPUT}
        ):
            raise HarnessValidationError(
                "memory reader is outside the accepted execution",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        self.snapshot = snapshot
        self._snapshots = snapshot_store
        self._namespaces = namespace_store

    def _grant(self) -> RefAuthoritySnapshot:
        grant = self._snapshots.get(run_id=self.snapshot.run_id, snapshot_ref=self.snapshot.snapshot_ref)
        if grant != self.snapshot:
            raise HarnessValidationError("memory reader grant differs from recorded authority", code="REF_SNAPSHOT_CONFLICT")
        return grant

    def describe(self, ref: str) -> MemoryNamespaceDescriptor:
        namespace_revision(ref)
        grant = self._grant()
        expected = RefAuthority().authorize_memory_namespace_ref(
            ref, grant.policy, resolver=SnapshotRefResolutionPort(grant),
        )
        metadata = self._namespaces.describe(ref)
        if not isinstance(metadata, MemoryNamespaceDescriptor) or metadata.exact_ref != ref:
            raise HarnessValidationError("memory namespace metadata is missing", code="REF_UNRESOLVED")
        actual = RefDescriptor.memory(
            namespace=metadata.namespace, ref=metadata.exact_ref,
            run_id=grant.run_id, stage_id=grant.stage_id,
            tenant_id=metadata.tenant_id, owner_id=metadata.owner_id,
            source_checksum=metadata.source_checksum,
            scope=RefScope.SHARED_READ_ONLY if metadata.shared_read_only else RefScope.PRIVATE,
        )
        if actual != expected:
            raise HarnessValidationError("memory metadata differs from its pinned descriptor", code="REF_CHECKSUM_MISMATCH")
        RefAuthority().authorize_memory_namespace(actual, grant.policy)
        return metadata

    def read(self, ref: str) -> MemoryNamespaceRevision:
        metadata = self.describe(ref)
        revision = self._namespaces.read(ref)
        if not isinstance(revision, MemoryNamespaceRevision) or revision.descriptor != metadata:
            raise HarnessValidationError("memory payload differs from its authorized revision", code="REF_CHECKSUM_MISMATCH")
        # The immutable value validates actual bytes, including all record fields.
        return MemoryNamespaceRevision(revision.descriptor, revision.payload)
