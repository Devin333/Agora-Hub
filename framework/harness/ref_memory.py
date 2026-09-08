"""Authorize exact immutable memory revisions before accessing their records."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

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
from framework.memory.models import MemoryQuery, MemoryRecallResult, MemoryRecord, MemorySearchResult
from framework.memory.policy import DEFAULT_AGENT_MEMORY_POLICY, MemoryPolicy
from framework.memory.runtime.context_assembler import MemoryContextAssembler
from framework.memory.runtime.recall import SimpleMemoryRecallStrategy
from framework.memory.stores.keyword_search import search_memory_records
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
        namespace_store: MemoryNamespaceStorePort | None,
        execution_identity: GraphExecutionIdentity,
        attempt_identity: SubAgentAttemptIdentity | None = None,
    ) -> None:
        if (
            not isinstance(snapshot, RefAuthoritySnapshot)
            or not isinstance(snapshot_store, RefAuthoritySnapshotStorePort)
        ):
            raise TypeError("memory reader requires an admitted reference snapshot")
        has_memory = any(item.ref_kind == "memory" for item in snapshot.descriptors)
        if (namespace_store is not None or has_memory) and (
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
        self._snapshot = snapshot
        self._snapshots = snapshot_store
        self._namespaces = namespace_store

    @property
    def snapshot(self) -> RefAuthoritySnapshot:
        return self._snapshot

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None:
        if execution_identity != self.snapshot.execution_identity:
            raise HarnessValidationError("memory caller differs from admitted execution", code="REF_SNAPSHOT_BINDING_MISMATCH")
        self._grant()

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


class HarnessMemoryRecallRuntime:
    """Recall exclusively from the complete records of an immutable input grant.

    Per-call retrieval uses verified namespace bytes, never the mutable
    publication source. No store or memory mutation API is exposed.
    """

    def __init__(self, reader: HarnessMemoryNamespaceReader) -> None:
        if not isinstance(reader, HarnessMemoryNamespaceReader):
            raise TypeError("memory recall requires a Harness namespace reader")
        self._reader = reader
        self._policy = replace(DEFAULT_AGENT_MEMORY_POLICY)

    @property
    def execution_identity(self) -> GraphExecutionIdentity:
        return self._reader.snapshot.execution_identity

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None:
        self._reader.validate_execution(execution_identity)

    def recall(
        self,
        query: MemoryQuery | dict[str, Any] | str,
        *,
        policy: MemoryPolicy | None = None,
    ) -> MemoryRecallResult:
        self.validate_execution(self.execution_identity)
        if isinstance(query, str):
            query = MemoryQuery(query=query)
        elif isinstance(query, dict):
            if set(query) - set(MemoryQuery.__dataclass_fields__):
                raise HarnessValidationError("memory query contains unsupported selectors", code="REF_UNAUTHORIZED")
            query = MemoryQuery.from_dict(query)
        if not isinstance(query, MemoryQuery):
            raise TypeError("memory query must be MemoryQuery, object or text")
        if set(query.filters) & {"namespace_ref", "revision", "exact_ref", "snapshot_ref", "access_mode"}:
            raise HarnessValidationError("memory filters cannot select authority", code="REF_UNAUTHORIZED")
        for key, expected in self.execution_identity.to_dict().items():
            if key in query.filters and query.filters[key] != expected:
                raise HarnessValidationError("memory filter conflicts with Graph identity", code="REF_UNAUTHORIZED")
        if query.filters.get("collection", "memories") != "memories":
            raise HarnessValidationError("collection cannot select namespace authority", code="REF_UNAUTHORIZED")
        if "owner_id" in query.filters and "actor" in query.filters and query.filters["owner_id"] != query.filters["actor"]:
            raise HarnessValidationError("memory owner selectors conflict", code="REF_UNAUTHORIZED")
        if "owner_id" in query.filters:
            filters = dict(query.filters)
            filters["actor"] = filters.pop("owner_id")
            query = replace(query, filters=filters)
        self._policy.validate_recall(query)
        requested_policy = policy or self._policy
        requested_policy.validate_recall(query)
        effective = requested_policy.filtered_query(query)
        if not query.scopes:
            effective = replace(effective, scopes=[
                scope for scope in self._policy.allowed_scopes
                if not requested_policy.allowed_scopes or scope in requested_policy.allowed_scopes
            ])
            if not effective.scopes:
                raise HarnessValidationError("memory policies have no permitted scope", code="REF_UNAUTHORIZED")
        if not query.kinds:
            effective = replace(effective, kinds=[
                kind for kind in self._policy.allowed_kinds
                if not requested_policy.allowed_kinds or kind in requested_policy.allowed_kinds
            ])
            if not effective.kinds:
                raise HarnessValidationError("memory policies have no permitted kind", code="REF_UNAUTHORIZED")
        effective = self._policy.filtered_query(effective)
        descriptors = tuple(item for item in self._reader.snapshot.descriptors if item.ref_kind == "memory")
        selectors = {
            "namespace": query.namespace,
            "tenant_id": query.tenant_id,
            "owner_id": query.filters.get("owner_id", query.filters.get("actor")),
        }
        for field in ("namespace", "tenant_id"):
            if field in query.filters:
                if selectors[field] is not None and selectors[field] != query.filters[field]:
                    raise HarnessValidationError("memory selectors conflict", code="REF_UNAUTHORIZED")
                selectors[field] = query.filters[field]
        for field, value in selectors.items():
            if value is not None:
                descriptors = tuple(item for item in descriptors if getattr(item, field) == value)
                if not descriptors:
                    raise HarnessValidationError("memory selector is outside the admitted namespaces", code="REF_UNAUTHORIZED")
        # Verify every selected descriptor before opening any selected payload.
        for descriptor in descriptors:
            self._reader.describe(descriptor.ref)
        records = []
        seen_ids = set()
        record_namespace_refs = {}
        for descriptor in descriptors:
            for record in self._reader.read(descriptor.ref).records():
                if record.memory_id in seen_ids:
                    raise HarnessValidationError("admitted memory revisions have ambiguous record identities", code="REF_SNAPSHOT_CONFLICT")
                seen_ids.add(record.memory_id)
                record_namespace_refs[record.memory_id] = descriptor.ref
                records.append(record)
        result = SimpleMemoryRecallStrategy().recall(
            effective, store=_RevisionSearch(tuple(records)), policy=requested_policy,
            assembler=MemoryContextAssembler(),
        )
        return replace(result, diagnostics={
            **result.diagnostics,
            "input_snapshot_ref": self._reader.snapshot.snapshot_ref,
            "namespace_refs": [item.ref for item in descriptors],
            "namespace_checksums": {item.ref: item.source_checksum for item in descriptors},
            "record_namespace_refs": {item.memory_id: record_namespace_refs[item.memory_id] for item in result.results},
            "execution_identity": self.execution_identity.to_dict(),
        })


class _RevisionSearch:
    def __init__(self, records: tuple[MemoryRecord, ...]) -> None:
        self._records = records

    def search(self, query: MemoryQuery) -> list[MemorySearchResult]:
        return search_memory_records(self._records, query)
