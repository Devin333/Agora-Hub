from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from framework.harness.ref_results import HarnessResultRefAuthority

from framework.events.canonical import checksum_for, thaw_canonical_json
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_runtime import HarnessGraphActivity
from framework.harness.control_plane.graph_state import HarnessGraphState
from framework.harness.runtime.graph_result_runtime import HarnessGraphResultRuntime
from framework.harness.runtime.materializer import MaterializationResult, ResultMaterializer
from framework.harness.runtime.result_models import (
    ArtifactClass,
    BoundedSummary,
    ContextPolicy,
    NodeResultBinding,
    NodeResultEnvelope,
    NodeResultStatus,
    ResultProvenance,
    ResultSensitivity,
    RetentionClass,
)
from framework.harness.runtime.result_policy import (
    NodeResultRequest,
    PersistenceBudgetSnapshot,
)
from framework.harness.subagents.gates import SubAgentTranscriptGate
from framework.harness.subagents.handoff import verify_handoff
from framework.harness.subagents.models import (
    SubAgentHandoff,
    SubAgentInvocation,
    SubAgentResult,
    SubAgentStatus,
)
from framework.harness.subagents.runtime import (
    SubAgentRuntime,
    subagent_attempt_identity,
)
from framework.harness.subagents.transcript import (
    SubAgentAttemptIdentity,
    SubAgentContextEvidence,
    SubAgentOutputDocument,
    SubAgentTranscript,
    SubAgentTranscriptReceipt,
    SubAgentTranscriptStorePort,
    sanitize_subagent_payload,
    subagent_evidence_schemas,
)
from framework.harness.graph.model import NormalizedHarnessGraph
from framework.shared.json import stable_json_dumps
from framework.shared.time import ensure_utc, utc_now


SUBAGENT_NODE_RESULT_SCHEMA_V3 = "newsroom.subagent-node-result@3"
SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V3 = "newsroom.subagent-materialized-bundle@3"
SUBAGENT_NODE_RESULT_SCHEMA = SUBAGENT_NODE_RESULT_SCHEMA_V3
SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA = SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V3
SUBAGENT_HANDOFF_SCHEMA = "newsroom.subagent-handoff@1"
SUBAGENT_RESULT_ADAPTER_REVISION_V3 = "harness-subagent-result-adapter@3"
SUBAGENT_RESULT_ADAPTER_REVISION = SUBAGENT_RESULT_ADAPTER_REVISION_V3
_MAX_RESULT_SUMMARY_BYTES = 2 * 1024


@dataclass(frozen=True, slots=True)
class VerifiedSubAgentMaterializedBundle:
    binding: NodeResultBinding
    identity: SubAgentAttemptIdentity
    receipt: SubAgentTranscriptReceipt
    context: SubAgentContextEvidence
    output: SubAgentOutputDocument
    transcript: SubAgentTranscript
    handoff: SubAgentHandoff | None = None
    bundle_schema: str = SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA
    bundle_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.binding, NodeResultBinding):
            raise TypeError("binding must be NodeResultBinding")
        if not isinstance(self.identity, SubAgentAttemptIdentity):
            raise TypeError("identity must be SubAgentAttemptIdentity")
        if not isinstance(self.receipt, SubAgentTranscriptReceipt):
            raise TypeError("receipt must be SubAgentTranscriptReceipt")
        if not isinstance(self.context, SubAgentContextEvidence):
            raise TypeError("context must be SubAgentContextEvidence")
        if not isinstance(self.output, SubAgentOutputDocument):
            raise TypeError("output must be SubAgentOutputDocument")
        if not isinstance(self.transcript, SubAgentTranscript):
            raise TypeError("transcript must be SubAgentTranscript")
        if self.handoff is not None and not isinstance(
            self.handoff,
            SubAgentHandoff,
        ):
            raise TypeError("handoff must be SubAgentHandoff or None")
        if self.bundle_schema != SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V3:
            raise HarnessValidationError(
                "unsupported SubAgent materialized bundle schema",
                code="subagent_result_schema_unsupported",
            )
        _verify_bundle_scope_and_identity(self)
        object.__setattr__(
            self,
            "bundle_checksum",
            checksum_for(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        handoff_gate = _verified_handoff_gate(self.handoff)
        return {
            "bundle_schema": self.bundle_schema,
            "graph_binding": self.binding.to_dict(),
            "subagent_identity": self.identity.to_dict(),
            "transcript_receipt": self.receipt.to_dict(),
            "context_evidence": self.context.to_dict(),
            "output_document": self.output.to_dict(),
            "transcript_body": self.transcript.to_dict(),
            "handoff_document": (
                self.handoff.to_dict() if self.handoff is not None else None
            ),
            "handoff_gate_evidence": handoff_gate,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "bundle_checksum": self.bundle_checksum,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Self:
        if not isinstance(value, Mapping):
            raise HarnessValidationError(
                "SubAgent materialized bundle must be an object",
                code="subagent_result_bundle_invalid",
            )
        payload = dict(value)
        expected = {
            "bundle_schema",
            "graph_binding",
            "subagent_identity",
            "transcript_receipt",
            "context_evidence",
            "output_document",
            "transcript_body",
            "handoff_document",
            "handoff_gate_evidence",
            "bundle_checksum",
        }
        if set(payload) != expected:
            raise HarnessValidationError(
                "SubAgent materialized bundle fields are invalid",
                code="subagent_result_bundle_invalid",
            )
        supplied_checksum = payload.pop("bundle_checksum")
        supplied_handoff_gate = payload.pop("handoff_gate_evidence")
        handoff_payload = payload.pop("handoff_document")
        for field_name in (
            "graph_binding",
            "subagent_identity",
            "transcript_receipt",
            "context_evidence",
            "output_document",
            "transcript_body",
        ):
            if not isinstance(payload[field_name], Mapping):
                raise HarnessValidationError(
                    f"{field_name} must be an object",
                    code="subagent_result_bundle_invalid",
                )
        handoff = (
            None
            if handoff_payload is None
            else SubAgentHandoff.from_dict(_required_mapping(
                handoff_payload,
                "handoff_document",
            ))
        )
        result = cls(
            bundle_schema=payload["bundle_schema"],
            binding=NodeResultBinding.from_dict(payload["graph_binding"]),
            identity=SubAgentAttemptIdentity.from_dict(
                payload["subagent_identity"]
            ),
            receipt=SubAgentTranscriptReceipt.from_dict(
                payload["transcript_receipt"]
            ),
            context=SubAgentContextEvidence.from_dict(
                payload["context_evidence"]
            ),
            output=SubAgentOutputDocument.from_dict(
                payload["output_document"]
            ),
            transcript=SubAgentTranscript.from_dict(
                payload["transcript_body"]
            ),
            handoff=handoff,
        )
        if supplied_handoff_gate != _verified_handoff_gate(handoff):
            raise HarnessValidationError(
                "SubAgent handoff gate evidence does not match",
                code="subagent_handoff_evidence_mismatch",
            )
        if supplied_checksum != result.bundle_checksum:
            raise HarnessValidationError(
                "SubAgent materialized bundle checksum does not match",
                code="subagent_result_checksum_mismatch",
            )
        return result


@dataclass(frozen=True, slots=True)
class HarnessSubAgentMaterializationResult:
    result: SubAgentResult
    bundle: VerifiedSubAgentMaterializedBundle
    materialization: MaterializationResult
    recovered: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.result, SubAgentResult):
            raise TypeError("result must be SubAgentResult")
        if not isinstance(self.bundle, VerifiedSubAgentMaterializedBundle):
            raise TypeError("bundle must be VerifiedSubAgentMaterializedBundle")
        if not isinstance(self.materialization, MaterializationResult):
            raise TypeError("materialization must be MaterializationResult")
        if self.bundle.binding != self.materialization.envelope.binding:
            raise HarnessValidationError(
                "SubAgent materialization binding changed",
                code="subagent_result_scope_mismatch",
            )
        if (
            checksum_for(self.bundle.to_dict())
            != self.materialization.envelope.candidate_checksum
        ):
            raise HarnessValidationError(
                "SubAgent materialization candidate changed",
                code="subagent_result_checksum_mismatch",
            )
        if not isinstance(self.recovered, bool):
            raise TypeError("recovered must be boolean")


@dataclass(frozen=True, slots=True)
class HarnessSubAgentActivityResult:
    outcome: HarnessSubAgentMaterializationResult
    graph_state: HarnessGraphState

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, HarnessSubAgentMaterializationResult):
            raise TypeError(
                "outcome must be HarnessSubAgentMaterializationResult"
            )
        if not isinstance(self.graph_state, HarnessGraphState):
            raise TypeError("graph_state must be HarnessGraphState")


class HarnessSubAgentResultAdapter:
    """Materialize only transcript-store-verified SubAgent attempts."""

    def __init__(
        self,
        *,
        materializer: ResultMaterializer,
        graph_result_runtime: HarnessGraphResultRuntime,
        transcript_store: SubAgentTranscriptStorePort,
        clock=utc_now,
        result_ref_authority: HarnessResultRefAuthority | None = None,
    ) -> None:
        if not isinstance(materializer, ResultMaterializer):
            raise TypeError("materializer must be ResultMaterializer")
        if not isinstance(graph_result_runtime, HarnessGraphResultRuntime):
            raise TypeError(
                "graph_result_runtime must be HarnessGraphResultRuntime"
            )
        if not isinstance(transcript_store, SubAgentTranscriptStorePort):
            raise TypeError(
                "transcript_store must implement SubAgentTranscriptStorePort"
            )
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._materializer = materializer
        self._graph_result_runtime = graph_result_runtime
        self._transcript_store = transcript_store
        self._clock = clock
        if result_ref_authority is not None:
            from framework.harness.ref_results import HarnessResultRefAuthority

            if not isinstance(result_ref_authority, HarnessResultRefAuthority) or result_ref_authority.transcript_store is not transcript_store:
                raise TypeError("result_ref_authority must own this transcript store")
        self.result_ref_authority = result_ref_authority

    def _store_for(self, identity: SubAgentAttemptIdentity) -> SubAgentTranscriptStorePort:
        if self.result_ref_authority is None:
            return self._transcript_store
        return self.result_ref_authority.for_attempt(identity)

    def _admit_materialization(self, identity: SubAgentAttemptIdentity, envelope: NodeResultEnvelope) -> None:
        if self.result_ref_authority is not None:
            self.result_ref_authority.admit_materialized_result(identity, envelope)

    def binding_for_activity(
        self,
        *,
        activity: HarnessGraphActivity,
        graph: NormalizedHarnessGraph,
        tenant_id: str,
        tenant_scope_ref: str,
        run_spec_checksum: str,
        invocation: SubAgentInvocation,
    ) -> NodeResultBinding:
        _require_activity(activity)
        identity = subagent_attempt_identity(invocation)
        _validate_activity_identity(activity, identity, graph)
        if checksum_for(tenant_id) != tenant_scope_ref:
            raise HarnessValidationError(
                "SubAgent tenant id does not match its Graph tenant scope",
                code="graph_result_lineage_scope_mismatch",
                details={"mismatches": ["tenant_id"]},
            )
        return self._graph_result_runtime.binding_for_activity(
            activity_id=activity.activity_id,
            graph=graph,
            tenant_id=tenant_id,
            tenant_scope_ref=tenant_scope_ref,
            attempt_id=subagent_result_attempt_id(identity),
            run_spec_checksum=run_spec_checksum,
        )

    def request_from_verified_result(
        self,
        result: SubAgentResult,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        handoff: SubAgentHandoff | None = None,
        created_at: datetime | None = None,
    ) -> tuple[NodeResultRequest, VerifiedSubAgentMaterializedBundle]:
        if not isinstance(result, SubAgentResult):
            raise TypeError("result must be SubAgentResult")
        if not isinstance(invocation, SubAgentInvocation):
            raise TypeError("invocation must be SubAgentInvocation")
        if not isinstance(binding, NodeResultBinding):
            raise TypeError("binding must be NodeResultBinding")
        identity = subagent_attempt_identity(invocation)
        evidence_schemas = subagent_evidence_schemas(identity)
        node_result_schema = SUBAGENT_NODE_RESULT_SCHEMA_V3
        materialized_bundle_schema = SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V3
        adapter_revision = SUBAGENT_RESULT_ADAPTER_REVISION_V3
        bundle = self._verified_bundle(
            result,
            identity=identity,
            binding=binding,
            handoff=handoff,
            bundle_schema=materialized_bundle_schema,
        )
        projection, result_summary, summary_complete = _control_projection(
            bundle
        )
        timestamp = ensure_utc(created_at or self._clock())
        request = NodeResultRequest(
            binding=binding,
            status=_node_status(result.status),
            output_schema_ref=node_result_schema,
            output_schema_digest=checksum_for(
                {
                    "schema": node_result_schema,
                    "bundle_schema": materialized_bundle_schema,
                    "context_schema": evidence_schemas.context,
                    "output_document_schema": evidence_schemas.output,
                    "transcript_schema": evidence_schemas.transcript,
                    "receipt_schema": evidence_schemas.receipt,
                    "handoff_schema": (
                        SUBAGENT_HANDOFF_SCHEMA
                        if handoff is not None
                        else None
                    ),
                    "subagent_output_schema": (
                        invocation.subagent_spec.output_schema
                    ),
                }
            ),
            candidate=bundle.to_dict(),
            media_type="application/json",
            summary=BoundedSummary.from_text(
                _summary_text(bundle, result_summary),
                complete=(
                    result.status is SubAgentStatus.SUCCEEDED
                    and summary_complete
                ),
            ),
            inline_projection=projection,
            inline_allowed_fields=tuple(sorted(projection)),
            provenance=ResultProvenance(
                producer_ref=adapter_revision,
                producer_revision=adapter_revision,
                source_refs=tuple(
                    sorted(
                        {
                            bundle.receipt.transcript_ref,
                            bundle.receipt.context_ref,
                            bundle.receipt.output_ref,
                        }
                    )
                ),
            ),
            artifact_class=ArtifactClass.TRANSCRIPT,
            retention_class=RetentionClass.EVIDENCE,
            sensitivity=ResultSensitivity.INTERNAL,
            required_for_replay=True,
            required_for_publication=False,
            reusable=False,
            side_effect_free=False,
            dependency_digest=None,
            context_policy=ContextPolicy.REF_LOAD_ALLOWED,
            created_at=timestamp,
        )
        if request.candidate_checksum != checksum_for(bundle.to_dict()):
            raise HarnessValidationError(
                "SubAgent bundle checksum changed during canonicalization",
                code="subagent_result_checksum_mismatch",
            )
        return request, bundle

    def materialize(
        self,
        result: SubAgentResult,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        handoff: SubAgentHandoff | None = None,
        created_at: datetime | None = None,
        budget: PersistenceBudgetSnapshot | None = None,
        recovered: bool = False,
    ) -> HarnessSubAgentMaterializationResult:
        request, bundle = self.request_from_verified_result(
            result,
            invocation=invocation,
            binding=binding,
            handoff=handoff,
            created_at=created_at,
        )
        materialization = self._materializer.materialize(
            request,
            budget=budget,
        )
        self._admit_materialization(bundle.identity, materialization.envelope)
        return HarnessSubAgentMaterializationResult(
            result=result,
            bundle=bundle,
            materialization=materialization,
            recovered=recovered,
        )

    def materialize_and_accept(
        self,
        result: SubAgentResult,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        activity: HarnessGraphActivity,
        graph: NormalizedHarnessGraph,
        run_spec_checksum: str,
        occurred_at: datetime,
        handoff: SubAgentHandoff | None = None,
        budget: PersistenceBudgetSnapshot | None = None,
        context_fingerprint: str | None = None,
        recovered: bool = False,
    ) -> HarnessSubAgentActivityResult:
        expected_binding = self.binding_for_activity(
            activity=activity,
            graph=graph,
            tenant_id=binding.tenant_id,
            tenant_scope_ref=binding.tenant_scope_ref,
            run_spec_checksum=run_spec_checksum,
            invocation=invocation,
        )
        if binding != expected_binding:
            raise HarnessValidationError(
                "SubAgent result binding does not match its Graph activity",
                code="subagent_result_scope_mismatch",
            )
        outcome = self.materialize(
            result,
            invocation=invocation,
            binding=binding,
            handoff=handoff,
            created_at=occurred_at,
            budget=budget,
            recovered=recovered,
        )
        graph_state = self._graph_result_runtime.accept_materialized_result(
            outcome.materialization.envelope,
            expected_binding=binding,
            activity_id=activity.activity_id,
            graph=graph,
            run_spec_checksum=run_spec_checksum,
            occurred_at=occurred_at,
            context_fingerprint=context_fingerprint,
        )
        return HarnessSubAgentActivityResult(
            outcome=outcome,
            graph_state=graph_state,
        )

    def recover_materialization(
        self,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        handoff: SubAgentHandoff | None = None,
        created_at: datetime | None = None,
        budget: PersistenceBudgetSnapshot | None = None,
    ) -> HarnessSubAgentMaterializationResult:
        request, bundle, result = self.request_from_committed_attempt(
            invocation=invocation,
            binding=binding,
            handoff=handoff,
            created_at=created_at,
        )
        materialization = self._materializer.materialize(
            request,
            budget=budget,
        )
        self._admit_materialization(bundle.identity, materialization.envelope)
        return HarnessSubAgentMaterializationResult(
            result=result,
            bundle=bundle,
            materialization=materialization,
            recovered=True,
        )

    def request_from_committed_attempt(
        self,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        handoff: SubAgentHandoff | None = None,
        created_at: datetime | None = None,
    ) -> tuple[
        NodeResultRequest,
        VerifiedSubAgentMaterializedBundle,
        SubAgentResult,
    ]:
        """Build a result request from the transcript store without live work."""

        identity = subagent_attempt_identity(invocation)
        receipt = self._store_for(identity).find_by_identity(identity)
        if receipt is None:
            raise HarnessValidationError(
                "SubAgent result recovery requires a committed transcript",
                code="subagent_result_recovery_missing",
            )
        result = self._result_from_receipt(receipt, identity=identity)
        request, bundle = self.request_from_verified_result(
            result,
            invocation=invocation,
            binding=binding,
            handoff=handoff,
            created_at=created_at,
        )
        return request, bundle, result

    def require_existing_materialization(
        self,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        handoff: SubAgentHandoff | None = None,
        created_at: datetime | None = None,
    ) -> NodeResultEnvelope:
        """Read a compatible common result envelope without writing it."""

        request, _, _ = self.request_from_committed_attempt(
            invocation=invocation,
            binding=binding,
            handoff=handoff,
            created_at=created_at,
        )
        return self._materializer.require_existing(request)

    def recover_and_accept(
        self,
        *,
        invocation: SubAgentInvocation,
        binding: NodeResultBinding,
        activity: HarnessGraphActivity,
        graph: NormalizedHarnessGraph,
        run_spec_checksum: str,
        occurred_at: datetime,
        handoff: SubAgentHandoff | None = None,
        budget: PersistenceBudgetSnapshot | None = None,
        context_fingerprint: str | None = None,
    ) -> HarnessSubAgentActivityResult:
        expected_binding = self.binding_for_activity(
            activity=activity,
            graph=graph,
            tenant_id=binding.tenant_id,
            tenant_scope_ref=binding.tenant_scope_ref,
            run_spec_checksum=run_spec_checksum,
            invocation=invocation,
        )
        if binding != expected_binding:
            raise HarnessValidationError(
                "SubAgent result binding does not match its Graph activity",
                code="subagent_result_scope_mismatch",
            )
        recovered = self.recover_materialization(
            invocation=invocation,
            binding=binding,
            handoff=handoff,
            created_at=occurred_at,
            budget=budget,
        )
        graph_state = self._graph_result_runtime.accept_materialized_result(
            recovered.materialization.envelope,
            expected_binding=binding,
            activity_id=activity.activity_id,
            graph=graph,
            run_spec_checksum=run_spec_checksum,
            occurred_at=occurred_at,
            context_fingerprint=context_fingerprint,
        )
        return HarnessSubAgentActivityResult(
            outcome=recovered,
            graph_state=graph_state,
        )

    def _verified_bundle(
        self,
        result: SubAgentResult,
        *,
        identity: SubAgentAttemptIdentity,
        binding: NodeResultBinding,
        handoff: SubAgentHandoff | None,
        bundle_schema: str,
    ) -> VerifiedSubAgentMaterializedBundle:
        store = self._store_for(identity)
        gate = SubAgentTranscriptGate().evaluate(
            result,
            store=store,
            identity=identity,
        )
        if not gate.passed or result.transcript_receipt is None:
            raise HarnessValidationError(
                "SubAgent transcript must pass its owner gate before materialization",
                code=gate.reason_code,
                details={"gate": gate.gate_name},
            )
        receipt = store.verify(result.transcript_receipt)
        context = store.read_context(receipt.context_ref)
        output = store.read_output(receipt.output_ref)
        transcript = store.read(receipt.transcript_ref)
        _verify_source_result(
            result,
            identity=identity,
            output=output,
            transcript=transcript,
        )
        return VerifiedSubAgentMaterializedBundle(
            binding=binding,
            identity=identity,
            receipt=receipt,
            context=context,
            output=output,
            transcript=transcript,
            handoff=handoff,
            bundle_schema=bundle_schema,
        )

    def _result_from_receipt(
        self,
        receipt: SubAgentTranscriptReceipt,
        *,
        identity: SubAgentAttemptIdentity,
    ) -> SubAgentResult:
        store = self._store_for(identity)
        verified = store.verify(receipt)
        output = store.read_output(verified.output_ref)
        transcript = store.read(verified.transcript_ref)
        if output.identity != identity or transcript.identity != identity:
            raise HarnessValidationError(
                "recovered SubAgent transcript belongs to another attempt",
                code="subagent_result_scope_mismatch",
            )
        return SubAgentResult(
            invocation_id=identity.invocation_id,
            child_run_id=identity.child_run_id,
            subagent_id=identity.subagent_id,
            status=SubAgentStatus(output.status),
            output=dict(output.output),
            artifact_refs=output.artifact_refs,
            tool_call_refs=transcript.tool_call_refs,
            warnings=transcript.warnings,
            errors=transcript.errors,
            transcript_receipt=verified,
            metadata={"recovered": True},
        )


class HarnessSubAgentActivityRuntime:
    """Compose SubAgent execution, common persistence, and Graph acceptance."""

    def __init__(
        self,
        *,
        runtime: SubAgentRuntime,
        adapter: HarnessSubAgentResultAdapter,
    ) -> None:
        if not isinstance(runtime, SubAgentRuntime):
            raise TypeError("runtime must be SubAgentRuntime")
        if not isinstance(adapter, HarnessSubAgentResultAdapter):
            raise TypeError("adapter must be HarnessSubAgentResultAdapter")
        self._runtime = runtime
        self._adapter = adapter

    def execute_and_accept(
        self,
        *,
        invocation: SubAgentInvocation,
        activity: HarnessGraphActivity,
        graph: NormalizedHarnessGraph,
        tenant_id: str,
        tenant_scope_ref: str,
        run_spec_checksum: str,
        occurred_at: datetime,
        handoff: SubAgentHandoff | None = None,
        budget: PersistenceBudgetSnapshot | None = None,
        context_fingerprint: str | None = None,
    ) -> HarnessSubAgentActivityResult:
        binding = self._adapter.binding_for_activity(
            activity=activity,
            graph=graph,
            tenant_id=tenant_id,
            tenant_scope_ref=tenant_scope_ref,
            run_spec_checksum=run_spec_checksum,
            invocation=invocation,
        )
        result = self._runtime.invoke(invocation)
        return self._adapter.materialize_and_accept(
            result,
            invocation=invocation,
            binding=binding,
            activity=activity,
            graph=graph,
            run_spec_checksum=run_spec_checksum,
            occurred_at=occurred_at,
            handoff=handoff,
            budget=budget,
            context_fingerprint=context_fingerprint,
            recovered=bool(result.metadata.get("recovered", False)),
        )

    def recover_and_accept(
        self,
        *,
        invocation: SubAgentInvocation,
        activity: HarnessGraphActivity,
        graph: NormalizedHarnessGraph,
        tenant_id: str,
        tenant_scope_ref: str,
        run_spec_checksum: str,
        occurred_at: datetime,
        handoff: SubAgentHandoff | None = None,
        budget: PersistenceBudgetSnapshot | None = None,
        context_fingerprint: str | None = None,
    ) -> HarnessSubAgentActivityResult:
        binding = self._adapter.binding_for_activity(
            activity=activity,
            graph=graph,
            tenant_id=tenant_id,
            tenant_scope_ref=tenant_scope_ref,
            run_spec_checksum=run_spec_checksum,
            invocation=invocation,
        )
        return self._adapter.recover_and_accept(
            invocation=invocation,
            binding=binding,
            activity=activity,
            graph=graph,
            run_spec_checksum=run_spec_checksum,
            occurred_at=occurred_at,
            handoff=handoff,
            budget=budget,
            context_fingerprint=context_fingerprint,
        )


def verify_subagent_materialized_bundle(
    value: Mapping[str, Any],
    *,
    expected_binding: NodeResultBinding | None = None,
) -> VerifiedSubAgentMaterializedBundle:
    thawed = thaw_canonical_json(value)
    if not isinstance(thawed, Mapping):
        raise HarnessValidationError(
            "SubAgent materialized bundle must be an object",
            code="subagent_result_bundle_invalid",
        )
    bundle = VerifiedSubAgentMaterializedBundle.from_dict(thawed)
    if expected_binding is not None and bundle.binding != expected_binding:
        raise HarnessValidationError(
            "SubAgent materialized bundle binding does not match",
            code="subagent_result_scope_mismatch",
        )
    return bundle


def _verify_bundle_scope_and_identity(
    bundle: VerifiedSubAgentMaterializedBundle,
) -> None:
    identity = bundle.identity
    receipt = bundle.receipt
    context = bundle.context
    output = bundle.output
    transcript = bundle.transcript
    evidence_schemas = subagent_evidence_schemas(identity)
    mismatches: list[str] = []
    if bundle.bundle_schema != SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V3:
        mismatches.append("bundle_schema")
    if context.schema_version != evidence_schemas.context:
        mismatches.append("context.schema_version")
    if output.schema_version != evidence_schemas.output:
        mismatches.append("output.schema_version")
    if transcript.schema_version != evidence_schemas.transcript:
        mismatches.append("transcript.schema_version")
    if receipt.schema_version != evidence_schemas.receipt:
        mismatches.append("receipt.schema_version")
    if receipt.identity_checksum != identity.identity_checksum:
        mismatches.append("receipt.identity_checksum")
    if bundle.binding.run_id != identity.parent_run_id:
        mismatches.append("run_id")
    if checksum_for(bundle.binding.tenant_id) != bundle.binding.tenant_scope_ref:
        mismatches.append("tenant_scope_ref")
    if bundle.binding.node_id != identity.node_id:
        mismatches.append("node_id")
    if bundle.binding.attempt_id != subagent_result_attempt_id(identity):
        mismatches.append("attempt_id")
    if context.identity != identity:
        mismatches.append("context.identity")
    if output.identity != identity:
        mismatches.append("output.identity")
    if transcript.identity != identity:
        mismatches.append("transcript.identity")
    if receipt.transcript_id != identity.transcript_id:
        mismatches.append("receipt.transcript_id")
    if receipt.invocation_id != identity.invocation_id:
        mismatches.append("receipt.invocation_id")
    if receipt.parent_run_id != identity.parent_run_id:
        mismatches.append("receipt.parent_run_id")
    if receipt.child_run_id != identity.child_run_id:
        mismatches.append("receipt.child_run_id")
    if receipt.task_instance_id != identity.task_instance_id:
        mismatches.append("receipt.task_instance_id")
    if receipt.attempt != identity.attempt:
        mismatches.append("receipt.attempt")
    if receipt.transcript_ref != transcript.ref:
        mismatches.append("receipt.transcript_ref")
    if receipt.transcript_checksum != transcript.transcript_checksum:
        mismatches.append("receipt.transcript_checksum")
    if receipt.context_checksum != context.context_checksum:
        mismatches.append("receipt.context_checksum")
    if receipt.output_ref != output.ref or transcript.output_ref != output.ref:
        mismatches.append("output_ref")
    if (
        receipt.output_checksum != output.output_checksum
        or transcript.output_checksum != output.output_checksum
    ):
        mismatches.append("output_checksum")
    if transcript.artifact_refs != output.artifact_refs:
        mismatches.append("artifact_refs")
    if bundle.handoff is not None:
        if bundle.handoff.parent_run_id != identity.parent_run_id:
            mismatches.append("handoff.parent_run_id")
        if bundle.handoff.from_subagent_id != identity.subagent_id:
            mismatches.append("handoff.from_subagent_id")
        sanitized = sanitize_subagent_payload(
            bundle.handoff.to_dict(),
            field_name="handoff",
        )
        if sanitized != bundle.handoff.to_dict():
            mismatches.append("handoff.sanitization")
    if mismatches:
        raise HarnessValidationError(
            "SubAgent materialized bundle identity does not match",
            code="subagent_result_scope_mismatch",
            details={"mismatches": mismatches},
        )


def _verify_source_result(
    result: SubAgentResult,
    *,
    identity: SubAgentAttemptIdentity,
    output: SubAgentOutputDocument,
    transcript: SubAgentTranscript,
) -> None:
    mismatches = tuple(
        field_name
        for field_name, expected, actual in (
            ("invocation_id", identity.invocation_id, result.invocation_id),
            ("child_run_id", identity.child_run_id, result.child_run_id),
            ("subagent_id", identity.subagent_id, result.subagent_id),
            ("status", output.status, result.status.value),
            ("output", checksum_for(output.output), checksum_for(result.output)),
            ("artifact_refs", output.artifact_refs, result.artifact_refs),
            ("tool_call_refs", transcript.tool_call_refs, result.tool_call_refs),
            ("warnings", transcript.warnings, result.warnings),
            ("errors", transcript.errors, result.errors),
        )
        if expected != actual
    )
    if mismatches:
        raise HarnessValidationError(
            "SubAgent result does not match its verified transcript",
            code="subagent_result_identity_mismatch",
            details={"mismatches": list(mismatches)},
        )


def _verified_handoff_gate(
    handoff: SubAgentHandoff | None,
) -> dict[str, Any] | None:
    if handoff is None:
        return None
    gate = verify_handoff(handoff)
    if not gate.passed:
        raise HarnessValidationError(
            "SubAgent handoff failed its deterministic schema gate",
            code=gate.reason_code,
            details=gate.details,
        )
    return {
        **gate.evidence_projection(),
        "evidence_checksum": gate.evidence_checksum,
    }


def _control_projection(
    bundle: VerifiedSubAgentMaterializedBundle,
) -> tuple[dict[str, Any], str | None, bool]:
    transcript = bundle.transcript
    result_summary, summary_complete = _result_summary(bundle.output.output)
    failed_gate_count = sum(
        1 for item in transcript.gate_results if item["passed"] is False
    )
    projection: dict[str, Any] = {
        "subagent_id": bundle.identity.subagent_id,
        "invocation_id": bundle.identity.invocation_id,
        "child_run_id": bundle.identity.child_run_id,
        "task_id": bundle.identity.task_id,
        "task_instance_id": bundle.identity.task_instance_id,
        "subagent_attempt": bundle.identity.attempt,
        "subagent_status": bundle.output.status,
        "transcript_ref": bundle.receipt.transcript_ref,
        "transcript_checksum": bundle.receipt.transcript_checksum,
        "context_ref": bundle.receipt.context_ref,
        "context_checksum": bundle.receipt.context_checksum,
        "output_ref": bundle.receipt.output_ref,
        "output_checksum": bundle.receipt.output_checksum,
        "receipt_checksum": bundle.receipt.receipt_checksum,
        "bundle_checksum": bundle.bundle_checksum,
        "gate_count": len(transcript.gate_results),
        "failed_gate_count": failed_gate_count,
        "artifact_ref_count": len(transcript.artifact_refs),
        "tool_call_ref_count": len(transcript.tool_call_refs),
        "warning_count": len(transcript.warnings),
        "error_count": len(transcript.errors),
    }
    if result_summary is not None:
        projection["result_summary"] = result_summary
    if bundle.handoff is not None:
        gate = _verified_handoff_gate(bundle.handoff)
        projection["handoff"] = {
            "handoff_id": bundle.handoff.handoff_id,
            "from_subagent_id": bundle.handoff.from_subagent_id,
            "to_subagent_id": bundle.handoff.to_subagent_id,
            "payload_checksum": checksum_for(bundle.handoff.payload),
            "gate_checksum": gate["evidence_checksum"] if gate else None,
            "input_ref_count": len(bundle.handoff.input_refs),
            "artifact_ref_count": len(bundle.handoff.artifact_refs),
        }
    return projection, result_summary, summary_complete


def _result_summary(output: Mapping[str, Any]) -> tuple[str | None, bool]:
    for key in ("summary", "result"):
        if key not in output:
            continue
        value = output[key]
        if isinstance(value, str):
            text = value.strip()
        elif value is None or isinstance(value, (bool, int, float)):
            text = stable_json_dumps(value)
        else:
            continue
        if not text:
            continue
        encoded = text.encode("utf-8")
        if len(encoded) <= _MAX_RESULT_SUMMARY_BYTES:
            return text, True
        truncated = encoded[:_MAX_RESULT_SUMMARY_BYTES]
        while True:
            try:
                return truncated.decode("utf-8"), False
            except UnicodeDecodeError:
                truncated = truncated[:-1]
    return None, not bool(output)


def _summary_text(
    bundle: VerifiedSubAgentMaterializedBundle,
    result_summary: str | None,
) -> str:
    prefix = f"SubAgent {bundle.identity.subagent_id} {bundle.output.status}"
    return prefix if result_summary is None else f"{prefix}: {result_summary}"


def _node_status(status: SubAgentStatus) -> NodeResultStatus:
    if status is SubAgentStatus.SUCCEEDED:
        return NodeResultStatus.SUCCEEDED
    if status is SubAgentStatus.FAILED:
        return NodeResultStatus.FAILED
    return NodeResultStatus.HALTED


def subagent_result_attempt_id(identity: SubAgentAttemptIdentity) -> str:
    if not isinstance(identity, SubAgentAttemptIdentity):
        raise TypeError("identity must be SubAgentAttemptIdentity")
    return identity.result_attempt_id


def _validate_activity_identity(
    activity: HarnessGraphActivity,
    identity: SubAgentAttemptIdentity,
    graph: NormalizedHarnessGraph,
) -> None:
    mismatches = [
        field_name
        for field_name, expected, actual in (
            ("run_id", activity.run_id, identity.parent_run_id),
            ("node_id", activity.node_id, identity.node_id),
            (
                "node_instance_id",
                activity.node_instance_id,
                identity.node_instance_id,
            ),
            ("activity_id", activity.activity_id, identity.activity_id),
            ("activity_attempt", activity.attempt, identity.activity_attempt),
        )
        if expected != actual
    ]
    if activity.graph_ref.graph_id != graph.graph_id:
        mismatches.append("activity.graph_id")
    if activity.graph_ref.schema_version != graph.schema_version:
        mismatches.append("activity.graph_schema_version")
    if activity.graph_ref.compiler_version != graph.compiler_version:
        mismatches.append("activity.compiler_version")
    if (
        activity.graph_ref.condition_policy_version
        != graph.condition_policy_version
    ):
        mismatches.append("activity.condition_policy_version")
    if activity.graph_ref.checksum != graph.checksum:
        mismatches.append("activity.graph_checksum")
    graph_ref = graph.graph_ref
    activity_graph_ref = activity.graph_ref.graph_ref
    for field_name, expected, actual in (
        ("graph_id", graph.graph_id, identity.graph_id),
        ("graph_version", graph.graph_version, identity.graph_version),
        (
            "graph_ref",
            None if graph_ref is None else graph_ref.exact_ref,
            identity.graph_ref,
        ),
        ("graph_schema_version", graph.schema_version, identity.graph_schema_version),
        ("compiler_version", graph.compiler_version, identity.compiler_version),
        (
            "condition_policy_version",
            graph.condition_policy_version,
            identity.condition_policy_version,
        ),
        ("graph_checksum", graph.checksum, identity.graph_checksum),
        (
            "activity.graph_ref",
            None if graph_ref is None else graph_ref.exact_ref,
            None if activity_graph_ref is None else activity_graph_ref.exact_ref,
        ),
    ):
        if expected != actual:
            mismatches.append(field_name)
    if mismatches:
        raise HarnessValidationError(
            "SubAgent invocation does not belong to its Graph activity",
            code="subagent_result_scope_mismatch",
            details={"mismatches": sorted(set(mismatches))},
        )


def _required_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="subagent_result_bundle_invalid",
        )
    return value


def _require_activity(activity: Any) -> HarnessGraphActivity:
    if not isinstance(activity, HarnessGraphActivity):
        raise TypeError("activity must be HarnessGraphActivity")
    return activity


__all__ = [
    "HarnessSubAgentActivityResult",
    "HarnessSubAgentActivityRuntime",
    "HarnessSubAgentMaterializationResult",
    "HarnessSubAgentResultAdapter",
    "SUBAGENT_HANDOFF_SCHEMA",
    "SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA",
    "SUBAGENT_MATERIALIZED_BUNDLE_SCHEMA_V3",
    "SUBAGENT_NODE_RESULT_SCHEMA",
    "SUBAGENT_NODE_RESULT_SCHEMA_V3",
    "VerifiedSubAgentMaterializedBundle",
    "subagent_result_attempt_id",
    "verify_subagent_materialized_bundle",
]
