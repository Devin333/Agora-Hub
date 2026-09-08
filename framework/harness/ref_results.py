"""Execution-bound result grants and authorized SubAgent evidence access."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from framework.harness.runtime.result_models import NodeResultEnvelope

from framework.events.canonical import checksum_for
from framework.harness.artifacts.ports import ArtifactReferenceDescriptorPort
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_authority import (
    REF_KIND_RESULT, RefAccessMode, RefAccessPolicy, RefAuthority, RefDescriptor,
)
from framework.harness.ref_snapshot import (
    RefAuthoritySnapshot, RefAuthoritySnapshotStorePort, RefSnapshotPhase,
    SnapshotRefResolutionPort,
)
from framework.harness.subagents.transcript import (
    MAX_PARENT_QUERY,
    SubAgentAttemptIdentity, SubAgentContextEvidence, SubAgentOutputDocument,
    SubAgentTranscript, SubAgentTranscriptReceipt,
    SubAgentTranscriptStorePort,
)
from framework.harness.subagents.transcript_metadata import (
    SubAgentAttemptDescriptor, SubAgentTranscriptDescriptorPort,
)
from framework.shared.graph_identity import GraphExecutionIdentity


def _denied(message: str, code: str = "REF_UNAUTHORIZED") -> HarnessValidationError:
    return HarnessValidationError(message, code=code)


class HarnessResultRefAuthority:
    """Issue result grants from trusted writer metadata, never worker authority."""

    def __init__(
        self, store: RefAuthoritySnapshotStorePort, *,
        transcript_store: SubAgentTranscriptStorePort,
        artifact_descriptors: ArtifactReferenceDescriptorPort | None = None,
        tenant_id: str,
    ) -> None:
        if not isinstance(store, RefAuthoritySnapshotStorePort):
            raise TypeError("store must implement RefAuthoritySnapshotStorePort")
        if not isinstance(transcript_store, SubAgentTranscriptStorePort) or not isinstance(
            transcript_store, SubAgentTranscriptDescriptorPort,
        ):
            raise TypeError("result authority requires transcript storage and metadata ports")
        if artifact_descriptors is not None and not isinstance(artifact_descriptors, ArtifactReferenceDescriptorPort):
            raise TypeError("artifact_descriptors must implement ArtifactReferenceDescriptorPort")
        if not isinstance(tenant_id, str) or not tenant_id or tenant_id != tenant_id.strip():
            raise ValueError("tenant_id must be a nonblank trimmed string")
        self.store = store
        self.transcript_store = transcript_store
        self._artifacts = artifact_descriptors
        self._tenant_id = tenant_id

    @property
    def is_durable(self) -> bool:
        return (
            getattr(self.store, "is_durable", False) is True
            and getattr(self.transcript_store, "is_durable", False) is True
        )

    def accepted_attempt(
        self, *, execution: GraphExecutionIdentity, stage_id: str,
        stage_binding_checksum: str, task_instance_id: str, attempt: int,
        task_policy_checksum: str,
    ) -> SubAgentAttemptIdentity:
        grant = self.store.find(
            run_id=execution.run_id,
            binding_key=RefAuthoritySnapshot.task_binding_key(
                execution, stage_id, stage_binding_checksum, task_instance_id,
                attempt, RefSnapshotPhase.CHILD_INPUT,
            ),
        )
        if grant is None or grant.task_policy_checksum != task_policy_checksum:
            raise _denied("result access requires its admitted child grant", "REF_SNAPSHOT_MISSING")
        self._require_tenant(grant)
        if grant.attempt_identity is None:
            raise _denied("child grant has no accepted attempt", "REF_SNAPSHOT_BINDING_MISMATCH")
        return grant.attempt_identity

    def for_attempt(
        self, identity: SubAgentAttemptIdentity, *, allow_registration: bool = False,
    ) -> "AttemptResultStore":
        return AttemptResultStore(self, identity, allow_registration=allow_registration)

    def _require_tenant(self, grant: RefAuthoritySnapshot) -> None:
        if grant.policy.tenant_id != checksum_for(self._tenant_id):
            raise _denied("result authority is outside the admitted tenant", "REF_POLICY_SCOPE_MISMATCH")

    def _grant(self, identity: SubAgentAttemptIdentity, phase: RefSnapshotPhase) -> RefAuthoritySnapshot | None:
        grant = self.store.find(
            run_id=identity.parent_run_id,
            binding_key=RefAuthoritySnapshot.attempt_binding_key(identity, phase),
        )
        if grant is not None:
            self._require_tenant(grant)
            if grant.attempt_identity != identity:
                raise _denied("result grant belongs to another accepted attempt", "REF_SNAPSHOT_BINDING_MISMATCH")
        return grant

    def _descriptor(self, parent: RefAuthoritySnapshot, ref: str, digest: str, artifact_type: str) -> RefDescriptor:
        return RefDescriptor(
            ref=ref, run_id=parent.run_id, stage_id=parent.stage_id,
            tenant_id=parent.policy.tenant_id, owner_id=parent.policy.owner_id,
            access_mode=RefAccessMode.READ_ONLY, artifact_type=artifact_type,
            source_checksum=digest, ref_kind=REF_KIND_RESULT,
        )

    def _artifact_descriptor(self, parent: RefAuthoritySnapshot, ref: str) -> RefDescriptor:
        if self._artifacts is None:
            raise _denied("result artifact has no trusted metadata owner")
        identity = parent.attempt_identity
        assert identity is not None
        metadata = self._artifacts.describe_artifact_ref(
            ref, expected_run_id=parent.run_id, expected_tenant_id=self._tenant_id,
        )
        if (
            metadata.ref != ref or metadata.run_id != parent.run_id
            or metadata.tenant_id != self._tenant_id
            or metadata.graph_id != identity.graph_id or metadata.node_id != identity.node_id
            or metadata.attempt_id != identity.result_attempt_id
        ):
            raise _denied("result artifact metadata belongs to another attempt", "REF_OWNER_MISMATCH")
        return self._descriptor(parent, ref, metadata.checksum, metadata.artifact_type)

    def _snapshot(
        self, parent: RefAuthoritySnapshot, *, phase: RefSnapshotPhase,
        source_checksum: str, descriptors: tuple[RefDescriptor, ...],
    ) -> RefAuthoritySnapshot:
        snapshot = RefAuthoritySnapshot(
            execution_identity=parent.execution_identity, stage_id=parent.stage_id,
            stage_binding_checksum=parent.stage_binding_checksum,
            task_policy_checksum=parent.task_policy_checksum, source_checksum=source_checksum,
            policy=RefAccessPolicy(
                policy_id=phase.value.lower() + ":" + parent.attempt_identity.task_instance_id,
                version="1", run_id=parent.run_id, stage_id=parent.stage_id,
                tenant_id=parent.policy.tenant_id, owner_id=parent.policy.owner_id,
                allowed_refs=tuple(item.ref for item in descriptors),
                allowed_artifact_types=tuple(sorted({item.artifact_type for item in descriptors})),
                allowed_ref_kinds=(REF_KIND_RESULT,),
                pinned_checksums={item.ref: item.source_checksum for item in descriptors},
            ),
            descriptors=descriptors, phase=phase, parent_snapshot_ref=parent.snapshot_ref,
            attempt_identity=parent.attempt_identity,
        )
        snapshot.validate_parent(parent)
        return snapshot

    def _commit(self, snapshot: RefAuthoritySnapshot) -> RefAuthoritySnapshot:
        ref = self.store.commit(snapshot)
        recorded = self.store.get(run_id=snapshot.run_id, snapshot_ref=ref)
        if recorded != snapshot:
            raise _denied("result grant commit changed its contents", "REF_SNAPSHOT_CONFLICT")
        return recorded

    def result_grant(
        self, identity: SubAgentAttemptIdentity, *, allow_registration: bool = False,
    ) -> tuple[RefAuthoritySnapshot, SubAgentAttemptDescriptor] | None:
        if not isinstance(allow_registration, bool):
            raise TypeError("allow_registration must be bool")
        parent = self._grant(identity, RefSnapshotPhase.CHILD_INPUT)
        if parent is None:
            raise _denied("result attempt has no admitted input grant", "REF_SNAPSHOT_MISSING")
        grant = self._grant(identity, RefSnapshotPhase.RESULT_ACCEPTANCE)
        metadata = self.transcript_store.describe_attempt(identity)
        if metadata is None:
            if grant is not None:
                raise _denied("committed result evidence is missing", "REF_SNAPSHOT_SOURCE_MISSING")
            return None
        if metadata.identity != identity:
            raise _denied("result metadata changed accepted identity", "REF_SNAPSHOT_BINDING_MISMATCH")
        receipt = metadata.receipt
        descriptors = tuple(
            self._descriptor(parent, ref, digest, kind)
            for ref, digest, kind in (
                (receipt.context_ref, receipt.context_checksum, "subagent_context"),
                (receipt.output_ref, receipt.output_checksum, "subagent_output"),
                (receipt.transcript_ref, receipt.transcript_checksum, "subagent_transcript"),
            )
        ) + tuple(self._artifact_descriptor(parent, ref) for ref in metadata.artifact_refs)
        expected = self._snapshot(
            parent, phase=RefSnapshotPhase.RESULT_ACCEPTANCE,
            source_checksum=metadata.descriptor_checksum, descriptors=descriptors,
        )
        if grant is None:
            if not allow_registration:
                raise _denied("result grant is not committed", "REF_SNAPSHOT_MISSING")
            grant = self._commit(expected)
        if grant != expected:
            raise _denied("result grant differs from its trusted source authority", "REF_SNAPSHOT_CONFLICT")
        return grant, metadata

    def admit_materialized_result(
        self, identity: SubAgentAttemptIdentity, envelope: NodeResultEnvelope,
    ) -> RefAuthoritySnapshot:
        from framework.harness.runtime.result_models import NodeResultEnvelope

        if not isinstance(envelope, NodeResultEnvelope):
            raise TypeError("envelope must be NodeResultEnvelope")
        bound = self.result_grant(identity)
        if bound is None:
            raise _denied("materialized result requires a committed result grant", "REF_SNAPSHOT_MISSING")
        parent, metadata = bound
        binding = envelope.binding
        if (
            binding.run_id != identity.parent_run_id or binding.graph_id != identity.graph_id
            or binding.graph_version != identity.graph_ref or binding.node_id != identity.node_id
            or binding.attempt_id != identity.result_attempt_id
            or binding.tenant_id != self._tenant_id or binding.tenant_scope_ref != parent.policy.tenant_id
            or set(envelope.provenance.source_refs) != {
                metadata.receipt.context_ref, metadata.receipt.output_ref, metadata.receipt.transcript_ref,
            }
        ):
            raise _denied("materialized result is outside its accepted attempt", "REF_SNAPSHOT_BINDING_MISMATCH")
        # Replay-required SubAgent evidence must have durable artifacts, not cache-only authority.
        if envelope.cache_refs or not envelope.materialized_refs:
            raise _denied("SubAgent materialization requires durable artifact metadata")
        descriptors = tuple(self._artifact_descriptor(parent, item.ref) for item in envelope.materialized_refs)
        if any(item.source_checksum != record.content_checksum for item, record in zip(descriptors, envelope.materialized_refs)):
            raise _denied("materialized artifact checksum differs from its envelope", "REF_CHECKSUM_MISMATCH")
        return self._commit(self._snapshot(
            parent, phase=RefSnapshotPhase.MATERIALIZED_RESULT,
            source_checksum=checksum_for(envelope.to_dict()), descriptors=descriptors,
        ))


@dataclass(frozen=True, slots=True, init=False)
class AttemptResultStore:
    """One accepted attempt's store view; a reference cannot select its caller."""

    _authority: HarnessResultRefAuthority = field(repr=False)
    identity: SubAgentAttemptIdentity
    _allow_registration: bool

    def __init__(self, authority: HarnessResultRefAuthority, identity: SubAgentAttemptIdentity, *, allow_registration: bool) -> None:
        if not isinstance(authority, HarnessResultRefAuthority):
            raise TypeError("authority must be HarnessResultRefAuthority")
        if not isinstance(identity, SubAgentAttemptIdentity):
            raise TypeError("identity must be SubAgentAttemptIdentity")
        if not isinstance(allow_registration, bool):
            raise TypeError("allow_registration must be bool")
        object.__setattr__(self, "_authority", authority)
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "_allow_registration", allow_registration)

    @property
    def is_durable(self) -> bool:
        return self._authority.is_durable

    def _bound(self) -> tuple[RefAuthoritySnapshot, SubAgentAttemptDescriptor]:
        bound = self._authority.result_grant(self.identity, allow_registration=self._allow_registration)
        if bound is None:
            raise _denied("result evidence is unavailable", "REF_SNAPSHOT_SOURCE_MISSING")
        return bound

    def _authorize(
        self, grant: RefAuthoritySnapshot, ref: str, *, expected_checksum: str | None = None,
    ) -> RefDescriptor:
        return RefAuthority().authorize_ref(
            ref, grant.policy, resolver=SnapshotRefResolutionPort(grant),
            expected_kind=REF_KIND_RESULT, expected_checksum=expected_checksum,
        )

    def authorize_artifacts(self, refs: tuple[str, ...], *, include_materialized: bool = False) -> None:
        grant, metadata = self._bound()
        original = set(metadata.artifact_refs)
        materialized = self._authority._grant(self.identity, RefSnapshotPhase.MATERIALIZED_RESULT) if include_materialized else None
        allowed = original | (set(materialized.policy.allowed_refs) if materialized is not None else set())
        if set(refs) != allowed:
            raise _denied("result artifacts differ from committed attempt grants")
        for ref in refs:
            owner = grant if ref in original else materialized
            descriptor = self._authorize(owner, ref)
            if self._authority._artifact_descriptor(owner, ref) != descriptor:
                raise _denied("result artifact metadata changed its original grant", "REF_CHECKSUM_MISMATCH")

    def authorize_original_artifacts(self) -> None:
        _, metadata = self._bound()
        self.authorize_artifacts(metadata.artifact_refs)

    def write(self, context: SubAgentContextEvidence, output: SubAgentOutputDocument, transcript: SubAgentTranscript) -> SubAgentTranscriptReceipt:
        if not self._allow_registration:
            raise _denied("result consumer cannot write evidence", "REF_ACCESS_MODE_DENIED")
        if any(item.identity != self.identity for item in (context, output, transcript)):
            raise _denied("result writer changed accepted attempt", "REF_SNAPSHOT_BINDING_MISMATCH")
        if self._authority._grant(self.identity, RefSnapshotPhase.CHILD_INPUT) is None:
            raise _denied("result writer has no admitted child grant", "REF_SNAPSHOT_MISSING")
        receipt = self._authority.transcript_store.write(context, output, transcript)
        return self.verify(receipt)

    def _read(self, ref: str, method: str, checksum_field: str, artifact_type: str):
        grant, _ = self._bound()
        descriptor = self._authorize(grant, ref)
        if descriptor.artifact_type != artifact_type:
            raise _denied("result read method differs from its descriptor type", "REF_ARTIFACT_TYPE_DENIED")
        document = getattr(self._authority.transcript_store, method)(ref)
        if document.identity != self.identity or getattr(document, checksum_field) != descriptor.source_checksum:
            raise _denied("result payload differs from its pinned descriptor", "REF_CHECKSUM_MISMATCH")
        return document

    def read(self, transcript_ref: str) -> SubAgentTranscript:
        return self._read(transcript_ref, "read", "transcript_checksum", "subagent_transcript")

    def read_output(self, output_ref: str) -> SubAgentOutputDocument:
        return self._read(output_ref, "read_output", "output_checksum", "subagent_output")

    def read_context(self, context_ref: str) -> SubAgentContextEvidence:
        return self._read(context_ref, "read_context", "context_checksum", "subagent_context")

    def verify(self, receipt: SubAgentTranscriptReceipt) -> SubAgentTranscriptReceipt:
        grant, metadata = self._bound()
        if receipt != metadata.receipt:
            raise _denied("receipt differs from accepted result metadata", "REF_SNAPSHOT_BINDING_MISMATCH")
        for ref, digest in (
            (receipt.context_ref, receipt.context_checksum),
            (receipt.output_ref, receipt.output_checksum),
            (receipt.transcript_ref, receipt.transcript_checksum),
        ):
            self._authorize(grant, ref, expected_checksum=digest)
        verified = self._authority.transcript_store.verify(receipt)
        if verified != receipt:
            raise _denied("result verification returned another receipt", "REF_CHECKSUM_MISMATCH")
        return verified

    def find_by_identity(self, identity: SubAgentAttemptIdentity) -> SubAgentTranscriptReceipt | None:
        if identity != self.identity:
            raise _denied("result lookup cannot select another accepted attempt", "REF_OWNER_MISMATCH")
        bound = self._authority.result_grant(identity, allow_registration=self._allow_registration)
        return None if bound is None else self.verify(bound[1].receipt)

    def refs_for_parent(self, parent_run_id: str, *, limit: int = MAX_PARENT_QUERY) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= MAX_PARENT_QUERY:
            raise HarnessValidationError("transcript parent query limit is invalid", code="subagent_transcript_query_limit_invalid")
        if parent_run_id != self.identity.parent_run_id:
            raise _denied("result listing cannot select another parent run")
        receipt = self.find_by_identity(self.identity)
        return () if receipt is None else (receipt.transcript_ref,)
