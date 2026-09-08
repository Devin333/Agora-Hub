"""Execution-bound authorization for durable planning observations."""

from __future__ import annotations

from dataclasses import replace

from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_PLANNING, REF_KIND_RESULT, RefAccessPolicy, RefAuthority,
    RefDescriptor, RefResolutionPort,
)
from framework.harness.ref_snapshot import (
    RefAuthoritySnapshot, RefAuthoritySnapshotStorePort, RefSnapshotPhase,
    SnapshotRefResolutionPort,
)
from framework.harness.task_plan.planning_observation import (
    PlanningObservationReceipt, PlanningObservationStorePort,
)
from framework.harness.task_plan.planning_metadata import (
    PlanningObservationDescriptor, PlanningObservationDescriptorPort,
)
from framework.harness.task_plan.canonical import identifier, positive_int
from framework.shared.graph_identity import GraphExecutionIdentity


def _error(message: str, code: str = "REF_UNAUTHORIZED") -> HarnessValidationError:
    return HarnessValidationError(message, code=code)


class HarnessPlanningRefAuthority:
    """Issue grants only from trusted metadata under an admitted Graph owner."""

    def __init__(
        self, store: RefAuthoritySnapshotStorePort, *,
        receipt_store: PlanningObservationStorePort,
        input_snapshot: RefAuthoritySnapshot,
        planner_turn: int,
        artifact_resolution: RefResolutionPort | None = None,
        allowed_artifact_types: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(store, RefAuthoritySnapshotStorePort):
            raise TypeError("store must implement RefAuthoritySnapshotStorePort")
        if not isinstance(receipt_store, PlanningObservationStorePort) or not isinstance(receipt_store, PlanningObservationDescriptorPort):
            raise TypeError("planning receipts require a metadata-only descriptor port")
        if not isinstance(input_snapshot, RefAuthoritySnapshot) or input_snapshot.phase is not RefSnapshotPhase.INPUT_ADMISSION:
            raise TypeError("planning authority requires input admission")
        if artifact_resolution is not None and not isinstance(artifact_resolution, RefResolutionPort):
            raise TypeError("artifact_resolution must implement RefResolutionPort")
        if receipt_store.input_snapshot.snapshot_ref != input_snapshot.snapshot_ref:
            raise _error("planning writer belongs to a different input grant", "REF_SNAPSHOT_BINDING_MISMATCH")
        self.store = store
        self.receipt_store = receipt_store
        self.input_snapshot = input_snapshot
        self.planner_turn_id = "planning_" + checksum_for({
            "input_snapshot_ref": input_snapshot.snapshot_ref,
            "planner_turn": positive_int(planner_turn, "planner_turn"),
        }).removeprefix("sha256:")
        self._artifact_resolution = artifact_resolution
        self._artifact_policy = replace(
            input_snapshot.policy, allowed_refs=(), pinned_checksums={},
            allowed_artifact_types=allowed_artifact_types,
            allowed_ref_kinds=(REF_KIND_RESULT,),
            allowed_memory_namespaces=(), writable_refs=(), shared_read_only_refs=(),
        )
        self._parent()

    def _parent(self) -> RefAuthoritySnapshot:
        parent = self.store.get(run_id=self.input_snapshot.run_id, snapshot_ref=self.input_snapshot.snapshot_ref)
        if parent != self.input_snapshot:
            raise _error("planning input grant differs from committed admission", "REF_SNAPSHOT_BINDING_MISMATCH")
        return parent

    def require_execution(self, execution: GraphExecutionIdentity, stage_binding_checksum: str, policy_checksum: str) -> None:
        parent = self._parent()
        if (execution, stage_binding_checksum, policy_checksum) != (
            parent.execution_identity, parent.stage_binding_checksum, parent.task_policy_checksum,
        ):
            raise _error("planning reference authority is outside the accepted Graph execution", "REF_SNAPSHOT_BINDING_MISMATCH")

    def require_scope(self, run_id: str, stage_id: str, policy_checksum: str | None = None, planner_turn_id: str | None = None) -> None:
        parent = self._parent()
        RefAuthority.require_scope(parent.policy, run_id=run_id, stage_id=stage_id)
        if policy_checksum is not None and policy_checksum != parent.task_policy_checksum:
            raise _error("planning request changed its admitted policy", "REF_POLICY_SCOPE_MISMATCH")
        if planner_turn_id is not None and identifier(planner_turn_id, "planner_turn_id") != self.planner_turn_id:
            raise _error("planning reference belongs to a different Harness turn", "REF_POLICY_SCOPE_MISMATCH")

    def _snapshot(self, metadata: PlanningObservationDescriptor) -> RefAuthoritySnapshot:
        parent = self._parent()
        self.require_scope(metadata.run_id, metadata.stage_id, metadata.policy_checksum, metadata.planner_turn_id)
        if metadata.input_snapshot_ref != parent.snapshot_ref:
            raise _error("planning receipt belongs to a different input grant", "REF_SNAPSHOT_BINDING_MISMATCH")
        descriptors = [RefDescriptor(
            ref=metadata.source_ref, run_id=parent.run_id, stage_id=parent.stage_id,
            tenant_id=parent.policy.tenant_id, owner_id=parent.policy.owner_id,
            access_mode="READ_ONLY", artifact_type="planning_observation",
            source_checksum=metadata.receipt_checksum, ref_kind=REF_KIND_PLANNING,
        )]
        for ref in metadata.artifact_refs:
            descriptor = None if self._artifact_resolution is None else self._artifact_resolution.resolve(ref)
            if not isinstance(descriptor, RefDescriptor) or descriptor.ref != ref:
                raise _error("planning artifact has no trusted exact descriptor")
            artifact_policy = replace(
                self._artifact_policy, allowed_refs=(ref,),
                pinned_checksums={ref: descriptor.source_checksum},
            )
            descriptors.append(RefAuthority().authorize(
                descriptor, artifact_policy,
                expected_kind=REF_KIND_RESULT,
            ))
        policy = RefAccessPolicy(
            policy_id="planning:" + metadata.request_checksum.removeprefix("sha256:"), version="1",
            run_id=parent.run_id, stage_id=parent.stage_id,
            tenant_id=parent.policy.tenant_id, owner_id=parent.policy.owner_id,
            allowed_refs=tuple(item.ref for item in descriptors),
            allowed_artifact_types=tuple(sorted({item.artifact_type for item in descriptors})),
            allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors})),
            pinned_checksums={item.ref: item.source_checksum for item in descriptors},
        )
        return RefAuthoritySnapshot(
            execution_identity=parent.execution_identity, stage_id=parent.stage_id,
            stage_binding_checksum=parent.stage_binding_checksum,
            task_policy_checksum=parent.task_policy_checksum,
            source_checksum=metadata.request_checksum, policy=policy,
            descriptors=tuple(descriptors), phase=RefSnapshotPhase.PLANNING_OBSERVATION,
            parent_snapshot_ref=parent.snapshot_ref,
        )

    def authorize(self, metadata: PlanningObservationDescriptor, *, allow_registration: bool) -> RefAuthoritySnapshot:
        expected = self._snapshot(metadata)
        grant = self.store.find(run_id=expected.run_id, binding_key=expected.binding_key)
        if grant is None:
            if not allow_registration:
                raise _error("planning reference grant is not committed", "REF_SNAPSHOT_MISSING")
            ref = self.store.commit(expected)
            if ref != expected.snapshot_ref:
                raise _error("planning grant commit returned different authority", "REF_SNAPSHOT_CONFLICT")
            grant = self.store.get(run_id=expected.run_id, snapshot_ref=ref)
        if grant != expected:
            raise _error("planning metadata differs from its pinned authority", "REF_SNAPSHOT_CONFLICT")
        RefAuthority().authorize_ref(
            metadata.source_ref, grant.policy, resolver=SnapshotRefResolutionPort(grant),
            expected_kind=REF_KIND_PLANNING, expected_checksum=metadata.receipt_checksum,
        )
        return grant

    def validation_ref_options(self, source_refs: tuple[str, ...]) -> dict:
        """Compose a read-only validation view of already committed grants."""

        parent = self._parent()
        descriptors = {item.ref: item for item in parent.descriptors}
        for ref in source_refs:
            metadata = self.receipt_store.describe_ref(ref)
            if metadata is None or metadata.source_ref != ref:
                raise _error("planning source metadata is missing", "REF_CHECKSUM_MISMATCH")
            grant = self.authorize(metadata, allow_registration=False)
            # Candidate validation only needs the receipt. Artifact access
            # remains confined to the receipt's own result grant.
            descriptor = next(item for item in grant.descriptors if item.ref == ref)
            if ref in descriptors and descriptors[ref] != descriptor:
                raise _error("planning reference conflicts with an admitted input", "REF_SNAPSHOT_CONFLICT")
            descriptors[ref] = descriptor
        return {
            "ref_authority": RefAuthority(),
            "ref_policy": replace(
                parent.policy, allowed_refs=tuple(descriptors), writable_refs=(),
                allowed_ref_kinds=tuple(sorted({item.ref_kind for item in descriptors.values()})),
                allowed_artifact_types=tuple(sorted({item.artifact_type for item in descriptors.values()})),
                pinned_checksums={ref: item.source_checksum for ref, item in descriptors.items()},
            ),
            "ref_descriptors": descriptors,
        }

    def reader(self, *, allow_registration: bool = False) -> AuthorizedPlanningObservationStore:
        return AuthorizedPlanningObservationStore(self, allow_registration=allow_registration)


class AuthorizedPlanningObservationStore:
    """Grant validation always precedes receipt payload reads."""

    def __init__(self, authority: HarnessPlanningRefAuthority, *, allow_registration: bool) -> None:
        self._authority = authority
        self._store = authority.receipt_store
        self._allow_registration = allow_registration

    @property
    def is_durable(self) -> bool:
        return getattr(self._store, "is_durable", False) is True and getattr(self._authority.store, "is_durable", False) is True

    def _read(self, metadata: PlanningObservationDescriptor | None) -> PlanningObservationReceipt | None:
        if metadata is None:
            return None
        self._authority.authorize(metadata, allow_registration=self._allow_registration)
        receipt = self._store.by_source_ref(metadata.source_ref)
        if receipt is None or PlanningObservationDescriptor.for_receipt(
            receipt, input_snapshot_ref=self._authority.input_snapshot.snapshot_ref,
            payload_checksum=metadata.payload_checksum,
            payload_size_bytes=metadata.payload_size_bytes,
        ) != metadata:
            raise _error("planning payload differs from authorized metadata", "REF_CHECKSUM_MISMATCH")
        return receipt

    def by_request(self, request_checksum: str) -> PlanningObservationReceipt | None:
        metadata = self._store.describe_request(request_checksum)
        if metadata is None:
            parent = self._authority._parent()
            if self._authority.store.find(
                run_id=parent.run_id,
                binding_key=RefAuthoritySnapshot.planning_binding_key(parent, request_checksum),
            ) is not None:
                raise _error("committed planning grant lost its receipt metadata", "REF_CHECKSUM_MISMATCH")
        if metadata is not None and metadata.request_checksum != request_checksum:
            raise _error("planning metadata differs from requested identity", "REF_CHECKSUM_MISMATCH")
        return self._read(metadata)

    def by_source_ref(self, source_ref: str) -> PlanningObservationReceipt | None:
        metadata = self._store.describe_ref(source_ref)
        if metadata is not None and metadata.source_ref != source_ref:
            raise _error("planning metadata differs from requested reference", "REF_CHECKSUM_MISMATCH")
        return self._read(metadata)

    def receipts_for_scope(self, run_id: str, stage_id: str, planner_turn_id: str) -> tuple[PlanningObservationReceipt, ...]:
        self._authority.require_scope(run_id, stage_id, planner_turn_id=planner_turn_id)
        receipts = []
        for metadata in self._store.descriptors_for_scope(run_id, stage_id, planner_turn_id):
            if (metadata.run_id, metadata.stage_id, metadata.planner_turn_id) != (run_id, stage_id, planner_turn_id):
                raise _error("planning scope index returned foreign metadata", "REF_POLICY_SCOPE_MISMATCH")
            receipts.append(self._read(metadata))
        return tuple(receipts)

    def save(self, receipt: PlanningObservationReceipt) -> str:
        if not self._allow_registration:
            raise _error("offline planning replay cannot persist receipts")
        request = receipt.request
        self._authority.require_scope(request.run_id, request.stage_id, request.policy_checksum, request.planner_turn_id)
        if self._store.save(receipt) != receipt.receipt_checksum:
            raise _error("planning writer returned a different receipt", "REF_CHECKSUM_MISMATCH")
        if self.by_request(request.request_checksum) != receipt:
            raise _error("planning writer did not preserve its receipt", "REF_CHECKSUM_MISMATCH")
        return receipt.receipt_checksum
