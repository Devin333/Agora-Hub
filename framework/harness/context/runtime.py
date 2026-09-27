from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable

from framework.harness.artifacts import ArtifactPort, ArtifactRef
from framework.harness.context.compaction_models import (
    ContextCompactionActionType,
    ContextCompactionOutcome,
    ContextCompactionPlan,
    ContextCompactionPolicy,
    ContextLossReport,
    ContextLossRisk,
)
from framework.harness.context.durable_store import (
    ContextDurableRefs,
    ContextVerifiedArtifactStore,
    ContextVerifiedStorePort,
)
from framework.harness.context.execution import (
    ContextCompactionActionExecutor,
    ContextCompactionExecutionResult,
    ContextCompactionExecutionStatus,
)
from framework.harness.context.planning import (
    ContextCompactionPlanner,
    ContextCompactionPlanningRequest,
)
from framework.harness.context.planning_models import (
    ContextCompactionPlanningResult,
    ContextCompactionPlanningStatus,
    ContextPhysicalAdmissionEvidence,
    ContextPlanningBudgetUsage,
)
from framework.harness.context.verified_records import (
    ContextCompressionRecordV2,
    ContextSemanticSnapshot,
)
from framework.harness.context.verification import (
    ContextAggregateVerificationResult,
    ContextAggregateVerifier,
    ContextPhysicalAdmissionVerifier,
    ContextPhysicalMaterialization,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.event import HarnessEvent, HarnessEventType
from framework.harness.ports import HarnessEventPort
from framework.shared.graph_identity import GraphExecutionIdentity


@runtime_checkable
class ContextPhysicalMaterializerPort(Protocol):
    def materialize(
        self,
        snapshot: ContextSemanticSnapshot,
        *,
        deployment_id: str,
    ) -> ContextPhysicalMaterialization:
        """Build the exact provider request represented by a semantic snapshot."""
        ...


class ContextCompactionRuntimeStatus(StrEnum):
    NO_COMPACTION_REQUIRED = "no_compaction_required"
    VERIFIED = "verified"
    PROTECTED_CONTEXT_EXCEEDS_WINDOW = "protected_context_exceeds_window"
    NO_ALLOWED_COMPACTION = "no_allowed_compaction"
    ACTION_BUDGET_EXHAUSTED = "action_budget_exhausted"
    SUMMARY_REJECTED = "summary_rejected"
    POST_COMPACTION_VERIFY_FAILED = "post_compaction_verify_failed"
    DURABLE_COMMIT_FAILED = "durable_commit_failed"


@dataclass(frozen=True)
class ContextCompactionRuntimeRequest:
    source_snapshot: ContextSemanticSnapshot
    policy: ContextCompactionPolicy
    deployment_id: str
    budget_usage: ContextPlanningBudgetUsage = field(
        default_factory=ContextPlanningBudgetUsage
    )

    def __post_init__(self) -> None:
        if not isinstance(self.source_snapshot, ContextSemanticSnapshot):
            raise HarnessValidationError("source_snapshot must be ContextSemanticSnapshot")
        if not isinstance(self.policy, ContextCompactionPolicy):
            raise HarnessValidationError("policy must be ContextCompactionPolicy")
        if not isinstance(self.deployment_id, str) or not self.deployment_id.strip():
            raise HarnessValidationError("deployment_id is required")
        if not isinstance(self.budget_usage, ContextPlanningBudgetUsage):
            raise HarnessValidationError("budget_usage must be ContextPlanningBudgetUsage")
        object.__setattr__(self, "deployment_id", self.deployment_id.strip())


@dataclass(frozen=True)
class ContextCompactionRuntimeResult:
    status: ContextCompactionRuntimeStatus | str
    source_snapshot: ContextSemanticSnapshot
    initial_admission: ContextPhysicalAdmissionEvidence | None
    planning: ContextCompactionPlanningResult | None
    execution: ContextCompactionExecutionResult | None
    result_snapshot: ContextSemanticSnapshot | None
    final_admission: ContextPhysicalAdmissionEvidence | None
    aggregate_verification: ContextAggregateVerificationResult | None
    durable_refs: ContextDurableRefs
    activation_event_id: str | None
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ContextCompactionRuntimeStatus(self.status))
        if not isinstance(self.source_snapshot, ContextSemanticSnapshot):
            raise HarnessValidationError("source_snapshot must be ContextSemanticSnapshot")
        for field_name, expected_type in (
            ("initial_admission", ContextPhysicalAdmissionEvidence),
            ("planning", ContextCompactionPlanningResult),
            ("execution", ContextCompactionExecutionResult),
            ("result_snapshot", ContextSemanticSnapshot),
            ("final_admission", ContextPhysicalAdmissionEvidence),
            ("aggregate_verification", ContextAggregateVerificationResult),
        ):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, expected_type):
                raise HarnessValidationError(f"{field_name} has an invalid type")
        if not isinstance(self.durable_refs, ContextDurableRefs):
            raise HarnessValidationError("durable_refs must be ContextDurableRefs")
        if self.activation_event_id is not None and not str(self.activation_event_id).strip():
            raise HarnessValidationError("activation_event_id must not be blank")
        if not isinstance(self.reason_code, str) or not self.reason_code.strip():
            raise HarnessValidationError("reason_code is required")

    @property
    def dispatch_authorized(self) -> bool:
        if self.activation_event_id is None or self.initial_admission is None:
            return False
        if not self.initial_admission.admitted and self.final_admission is None:
            return False
        if self.status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED:
            return self.initial_admission.admitted
        return (
            self.status is ContextCompactionRuntimeStatus.VERIFIED
            and self.aggregate_verification is not None
            and self.aggregate_verification.dispatch_authorized
            and self.final_admission is not None
            and self.final_admission.admitted
            and self.result_snapshot is not None
        )

    def authorizes_dispatch(self, prepared_fingerprint: str) -> bool:
        if not isinstance(prepared_fingerprint, str) or not prepared_fingerprint.strip():
            return False
        evidence = self.final_admission or self.initial_admission
        return self.dispatch_authorized and evidence is not None and (
            evidence.prepared_fingerprint == prepared_fingerprint
        )


class ContextCompactionRuntime:
    """Bounded PLAN -> EXECUTE -> VERIFY -> durable activation owner."""

    def __init__(
        self,
        *,
        materializer: ContextPhysicalMaterializerPort,
        admission_verifier: ContextPhysicalAdmissionVerifier,
        artifact_port: ArtifactPort,
        event_port: HarnessEventPort,
        planner: ContextCompactionPlanner | None = None,
        executor: ContextCompactionActionExecutor | None = None,
        aggregate_verifier: ContextAggregateVerifier | None = None,
        durable_store: ContextVerifiedStorePort | None = None,
        runtime_event_sink: Any | None = None,
        runtime_event_identity: GraphExecutionIdentity | None = None,
    ) -> None:
        if not isinstance(materializer, ContextPhysicalMaterializerPort):
            raise HarnessValidationError(
                "materializer must implement ContextPhysicalMaterializerPort"
            )
        if not isinstance(admission_verifier, ContextPhysicalAdmissionVerifier):
            raise HarnessValidationError(
                "admission_verifier must implement ContextPhysicalAdmissionVerifier"
            )
        if not isinstance(artifact_port, ArtifactPort):
            raise HarnessValidationError("artifact_port must implement ArtifactPort")
        if not isinstance(event_port, HarnessEventPort):
            raise HarnessValidationError("event_port must implement HarnessEventPort")
        self._materializer = materializer
        self._admission_verifier = admission_verifier
        self._event_port = event_port
        self._planner = planner or ContextCompactionPlanner()
        self._executor = executor or ContextCompactionActionExecutor(artifact_port)
        self._aggregate_verifier = aggregate_verifier or ContextAggregateVerifier()
        self._store = durable_store or ContextVerifiedArtifactStore(artifact_port)
        self._runtime_event_sink = runtime_event_sink
        self._runtime_event_identity = runtime_event_identity
        self._last_event_id: str | None = None

    def run(self, request: ContextCompactionRuntimeRequest) -> ContextCompactionRuntimeResult:
        if not isinstance(request, ContextCompactionRuntimeRequest):
            raise HarnessValidationError("request must be ContextCompactionRuntimeRequest")
        source = request.source_snapshot
        source_materialization = self._materializer.materialize(
            source,
            deployment_id=request.deployment_id,
        )
        self._assert_materialization(source, source_materialization)
        initial = self._admission_verifier.admit(source_materialization)
        self._assert_admission(initial, source)
        refs = replace(
            ContextDurableRefs(),
            source_snapshot=self._store.save_snapshot(source),
            initial_admission=self._store.save_admission(initial),
        )
        planning = self._planner.plan(
            ContextCompactionPlanningRequest(
                source_snapshot=source,
                initial_admission=initial,
                policy=request.policy,
                budget_usage=request.budget_usage,
            )
        )
        refs = replace(
            refs,
            planning_result=self._store.save_planning_result(planning),
            plan=(self._store.save_plan(planning.plan) if planning.plan is not None else None),
        )
        if not self._emit_planned(request, initial, planning, refs):
            return self._durable_failure(
                source=source,
                initial=initial,
                planning=planning,
                refs=refs,
                reason_code="canonical_event_commit_failed",
            )
        if planning.status is not ContextCompactionPlanningStatus.PLAN_READY:
            status = _runtime_status_for_planning(planning.status)
            if (
                status is not ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED
                and self._emit_planning_rejected(request, planning, refs) is None
            ):
                return self._durable_failure(
                    source=source,
                    initial=initial,
                    planning=planning,
                    refs=refs,
                    reason_code="canonical_event_commit_failed",
                )
            return ContextCompactionRuntimeResult(
                status=status,
                source_snapshot=source,
                initial_admission=initial,
                planning=planning,
                execution=None,
                result_snapshot=(source if status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED else None),
                final_admission=(initial if status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED else None),
                aggregate_verification=None,
                durable_refs=refs,
                activation_event_id=(
                    self._last_event_id if status is ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED else None
                ),
                reason_code=planning.reason_code,
            )

        assert planning.plan is not None
        execution = self._executor.execute(
            planning.plan,
            source_snapshot=source,
            initial_admission=initial,
            policy=request.policy,
            budget_usage=request.budget_usage,
        )
        action_refs: list[ArtifactRef] = []
        for action_result in execution.action_results:
            action_ref = self._store.save_action_result(action_result)
            action_refs.append(action_ref)
            refs = replace(refs, action_results=tuple(action_refs))
            if not self._emit_action(request, planning.plan, action_result, action_ref, refs):
                return self._durable_failure(
                    source=source,
                    initial=initial,
                    planning=planning,
                    execution=execution,
                    refs=refs,
                    reason_code="canonical_event_commit_failed",
                )
            if action_result.summary_candidate_ref:
                if not self._emit_summary_candidate(
                    request,
                    planning.plan,
                    action_result,
                    action_ref,
                ):
                    return self._durable_failure(
                        source=source,
                        initial=initial,
                        planning=planning,
                        execution=execution,
                        refs=refs,
                        reason_code="canonical_event_commit_failed",
                    )
        if execution.result_snapshot is None:
            if self._emit_rejected(
                request,
                planning,
                execution,
                refs,
                reason_code=execution.reason_code,
            ) is None:
                return self._durable_failure(
                    source=source,
                    initial=initial,
                    planning=planning,
                    execution=execution,
                    refs=refs,
                    reason_code="canonical_event_commit_failed",
                )
            return ContextCompactionRuntimeResult(
                status=_runtime_status_for_execution(execution.status),
                source_snapshot=source,
                initial_admission=initial,
                planning=planning,
                execution=execution,
                result_snapshot=None,
                final_admission=None,
                aggregate_verification=None,
                durable_refs=refs,
                activation_event_id=None,
                reason_code=execution.reason_code,
            )

        result_snapshot = execution.result_snapshot
        result_materialization = self._materializer.materialize(
            result_snapshot,
            deployment_id=request.deployment_id,
        )
        self._assert_materialization(result_snapshot, result_materialization)
        final_admission = self._admission_verifier.admit(result_materialization)
        self._assert_admission(final_admission, result_snapshot)
        aggregate = self._aggregate_verifier.verify(
            source_snapshot=source,
            result_snapshot=result_snapshot,
            plan=planning.plan,
            policy=request.policy,
            action_results=execution.action_results,
            usage=execution.usage,
            physical_admission=final_admission,
        )
        refs = replace(
            refs,
            result_snapshot=self._store.save_snapshot(result_snapshot),
            final_admission=self._store.save_admission(final_admission),
            aggregate_verification=self._store.save_aggregate_verification(aggregate),
        )
        record = self._build_record(
            request=request,
            source=source,
            result=result_snapshot,
            initial=initial,
            final=final_admission,
            plan=planning.plan,
            execution=execution,
            aggregate=aggregate,
        )
        refs = replace(refs, compression_record=self._store.save_compression_record(record))
        if not aggregate.dispatch_authorized:
            if self._emit_rejected(
                request,
                planning,
                execution,
                refs,
                reason_code=aggregate.reason_code,
                aggregate=aggregate,
            ) is None:
                return self._durable_failure(
                    source=source,
                    initial=initial,
                    planning=planning,
                    execution=execution,
                    result_snapshot=result_snapshot,
                    final_admission=final_admission,
                    aggregate=aggregate,
                    refs=refs,
                    reason_code="canonical_event_commit_failed",
                )
            return ContextCompactionRuntimeResult(
                status=ContextCompactionRuntimeStatus.POST_COMPACTION_VERIFY_FAILED,
                source_snapshot=source,
                initial_admission=initial,
                planning=planning,
                execution=execution,
                result_snapshot=result_snapshot,
                final_admission=final_admission,
                aggregate_verification=aggregate,
                durable_refs=refs,
                activation_event_id=None,
                reason_code=aggregate.reason_code,
            )
        verified_event = self._event(
            HarnessEventType.CONTEXT_COMPACTION_VERIFIED,
            request,
            {
                "source_snapshot_id": source.snapshot_id,
                "source_snapshot_checksum": source.checksum,
                "result_snapshot_id": result_snapshot.snapshot_id,
                "result_snapshot_checksum": result_snapshot.checksum,
                "plan_id": planning.plan.plan_id,
                "record_ref": refs.compression_record.ref if refs.compression_record else None,
                "aggregate_ref": refs.aggregate_verification.ref if refs.aggregate_verification else None,
                "initial_admission_ref": refs.initial_admission.ref if refs.initial_admission else None,
                "final_admission_ref": refs.final_admission.ref if refs.final_admission else None,
                "prepared_fingerprint": final_admission.prepared_fingerprint,
                "before_input_tokens": initial.input_tokens,
                "after_input_tokens": final_admission.input_tokens,
                "policy_revision": request.policy.policy_revision,
                "profile_revision": final_admission.physical_profile_revision,
                "gate_names": [gate.gate_name for gate in aggregate.gates],
            },
        )
        if verified_event is None:
            return self._durable_failure(
                source=source,
                initial=initial,
                planning=planning,
                execution=execution,
                refs=refs,
                result_snapshot=result_snapshot,
                final_admission=final_admission,
                aggregate=aggregate,
                reason_code="verified_activation_event_commit_failed",
            )
        return ContextCompactionRuntimeResult(
            status=ContextCompactionRuntimeStatus.VERIFIED,
            source_snapshot=source,
            initial_admission=initial,
            planning=planning,
            execution=execution,
            result_snapshot=result_snapshot,
            final_admission=final_admission,
            aggregate_verification=aggregate,
            durable_refs=refs,
            activation_event_id=verified_event.event_id,
            reason_code="verified_activation_committed",
        )

    def _emit_planned(
        self,
        request: ContextCompactionRuntimeRequest,
        initial: ContextPhysicalAdmissionEvidence,
        planning: ContextCompactionPlanningResult,
        refs: ContextDurableRefs,
    ) -> bool:
        return self._event(
            HarnessEventType.CONTEXT_COMPACTION_PLANNED,
            request,
            {
                "source_snapshot_id": request.source_snapshot.snapshot_id,
                "source_snapshot_checksum": request.source_snapshot.checksum,
                "initial_admission_id": initial.evidence_id,
                "initial_admission_ref": refs.initial_admission.ref if refs.initial_admission else None,
                "planning_result_ref": refs.planning_result.ref if refs.planning_result else None,
                "plan_ref": refs.plan.ref if refs.plan else None,
                "plan_id": planning.plan.plan_id if planning.plan else None,
                "status": planning.status.value,
                "reason_code": planning.reason_code,
                "protected_group_count": len(planning.protected_group_ids),
            },
        ) is not None

    def _emit_planning_rejected(
        self,
        request: ContextCompactionRuntimeRequest,
        planning: ContextCompactionPlanningResult,
        refs: ContextDurableRefs,
    ) -> HarnessEvent | None:
        return self._event(
            HarnessEventType.CONTEXT_COMPACTION_REJECTED,
            request,
            {
                "source_snapshot_id": request.source_snapshot.snapshot_id,
                "source_snapshot_checksum": request.source_snapshot.checksum,
                "plan_id": None,
                "planning_result_ref": (
                    refs.planning_result.ref if refs.planning_result else None
                ),
                "result_snapshot_id": None,
                "record_ref": None,
                "aggregate_ref": None,
                "reason_code": planning.reason_code,
                "aggregate_outcome": None,
                "planning_status": planning.status.value,
            },
        )

    def _emit_action(
        self,
        request: ContextCompactionRuntimeRequest,
        plan: ContextCompactionPlan,
        result: Any,
        action_ref: ArtifactRef,
        refs: ContextDurableRefs,
    ) -> bool:
        return self._event(
            HarnessEventType.CONTEXT_COMPACTION_ACTION_APPLIED,
            request,
            {
                "plan_id": plan.plan_id,
                "action_id": result.action.action_id,
                "action_type": result.action.action_type.value,
                "action_result_ref": action_ref.ref,
                "source_snapshot_id": result.source_snapshot_id,
                "result_group_count": len(result.result_group_ids),
                "applied": result.applied,
                "reason_code": result.reason_code,
            },
        ) is not None

    def _emit_summary_candidate(
        self,
        request: ContextCompactionRuntimeRequest,
        plan: ContextCompactionPlan,
        result: Any,
        action_ref: ArtifactRef,
    ) -> bool:
        return self._event(
            HarnessEventType.CONTEXT_SUMMARY_CANDIDATE_CREATED,
            request,
            {
                "plan_id": plan.plan_id,
                "action_id": result.action.action_id,
                "action_result_ref": action_ref.ref,
                "candidate_ref": result.summary_candidate_ref,
            },
        ) is not None

    def _emit_rejected(
        self,
        request: ContextCompactionRuntimeRequest,
        planning: ContextCompactionPlanningResult,
        execution: ContextCompactionExecutionResult,
        refs: ContextDurableRefs,
        *,
        reason_code: str,
        aggregate: ContextAggregateVerificationResult | None = None,
    ) -> HarnessEvent | None:
        return self._event(
            HarnessEventType.CONTEXT_COMPACTION_REJECTED,
            request,
            {
                "source_snapshot_id": request.source_snapshot.snapshot_id,
                "source_snapshot_checksum": request.source_snapshot.checksum,
                "plan_id": planning.plan.plan_id if planning.plan else None,
                "planning_result_ref": refs.planning_result.ref if refs.planning_result else None,
                "result_snapshot_id": execution.result_snapshot.snapshot_id if execution.result_snapshot else None,
                "record_ref": refs.compression_record.ref if refs.compression_record else None,
                "aggregate_ref": refs.aggregate_verification.ref if refs.aggregate_verification else None,
                "reason_code": reason_code,
                "aggregate_outcome": aggregate.outcome.value if aggregate else None,
            },
        )

    def _event(
        self,
        event_type: HarnessEventType,
        request: ContextCompactionRuntimeRequest,
        payload: Mapping[str, Any],
    ) -> HarnessEvent | None:
        try:
            stored = self._event_port.record(
                HarnessEvent(
                    event_type=event_type,
                    run_id=request.source_snapshot.run_id,
                    node_id=request.source_snapshot.stage_id,
                    payload=dict(payload),
                    metadata={
                        "schema_revision": "newsroom.context-runtime-event/v1",
                        "ref_only": True,
                    },
                )
            )
        except Exception:
            return None
        if not isinstance(stored, HarnessEvent) or stored.event_type is not event_type:
            return None
        if self._runtime_event_sink is not None:
            try:
                from framework.events.runtime.projection import RuntimeEventEmitter, RuntimeEventIdentity

                canonical_type = {
                    HarnessEventType.CONTEXT_COMPACTION_PLANNED: "context_compaction_planned",
                    HarnessEventType.CONTEXT_COMPACTION_REJECTED: "context_compaction_rejected",
                    HarnessEventType.CONTEXT_COMPACTION_VERIFIED: "context_compaction_committed",
                    HarnessEventType.CONTEXT_COMPACTION_ACTION_APPLIED: "context_compaction_committed",
                    HarnessEventType.CONTEXT_SUMMARY_CANDIDATE_CREATED: "context_compaction_committed",
                }.get(event_type, "runtime_error")
                RuntimeEventEmitter(
                    self._runtime_event_sink,
                    identity=RuntimeEventIdentity(
                        graph_identity=self._runtime_event_identity,
                        activity_id=(self._runtime_event_identity.activity_id if self._runtime_event_identity else None),
                        node_id=(self._runtime_event_identity.node_id if self._runtime_event_identity else request.source_snapshot.stage_id),
                        node_instance_id=(self._runtime_event_identity.node_instance_id if self._runtime_event_identity else None),
                    ),
                    source="context-compaction-runtime",
                    stream_id=request.source_snapshot.run_id,
                ).emit(
                    canonical_type,
                    # The Harness event and the runtime projection occupy the
                    # same canonical identity namespace in durable compositions.
                    # Keep an explicit stable correlation while preventing an
                    # identity collision on the second append.
                    event_id=f"context-runtime:{stored.event_id}",
                    status=str(payload.get("status") or "committed"),
                    reason_code=str(payload.get("reason_code") or "context_event"),
                    refs=tuple(str(value) for key, value in payload.items() if key.endswith("_ref") and isinstance(value, str)),
                    metadata=dict(payload),
                )
            except Exception:
                # The legacy event is already durable, but the canonical
                # projection is part of this runtime's required commit. A
                # failed projection must therefore surface as a typed durable
                # failure through the caller's existing event contract.
                return None
        self._last_event_id = stored.event_id
        return stored

    @staticmethod
    def _assert_materialization(
        snapshot: ContextSemanticSnapshot,
        materialization: ContextPhysicalMaterialization,
    ) -> None:
        if not isinstance(materialization, ContextPhysicalMaterialization):
            raise HarnessValidationError(
                "physical materializer must return ContextPhysicalMaterialization"
            )
        if materialization.result_snapshot.snapshot_id != snapshot.snapshot_id:
            raise HarnessValidationError("physical materialization snapshot is stale")
        if materialization.result_snapshot.checksum != snapshot.checksum:
            raise HarnessValidationError("physical materialization checksum is stale")

    @staticmethod
    def _assert_admission(
        evidence: ContextPhysicalAdmissionEvidence,
        snapshot: ContextSemanticSnapshot,
    ) -> None:
        if evidence.source_snapshot_id != snapshot.snapshot_id:
            raise HarnessValidationError("physical admission snapshot is stale")
        if evidence.source_snapshot_checksum != snapshot.checksum:
            raise HarnessValidationError("physical admission checksum is stale")

    def _durable_failure(
        self,
        *,
        source: ContextSemanticSnapshot,
        initial: ContextPhysicalAdmissionEvidence,
        planning: ContextCompactionPlanningResult,
        refs: ContextDurableRefs,
        reason_code: str,
        execution: ContextCompactionExecutionResult | None = None,
        result_snapshot: ContextSemanticSnapshot | None = None,
        final_admission: ContextPhysicalAdmissionEvidence | None = None,
        aggregate: ContextAggregateVerificationResult | None = None,
    ) -> ContextCompactionRuntimeResult:
        return ContextCompactionRuntimeResult(
            status=ContextCompactionRuntimeStatus.DURABLE_COMMIT_FAILED,
            source_snapshot=source,
            initial_admission=initial,
            planning=planning,
            execution=execution,
            result_snapshot=result_snapshot,
            final_admission=final_admission,
            aggregate_verification=aggregate,
            durable_refs=refs,
            activation_event_id=None,
            reason_code=reason_code,
        )

    def _build_record(
        self,
        *,
        request: ContextCompactionRuntimeRequest,
        source: ContextSemanticSnapshot,
        result: ContextSemanticSnapshot,
        initial: ContextPhysicalAdmissionEvidence,
        final: ContextPhysicalAdmissionEvidence,
        plan: ContextCompactionPlan,
        execution: ContextCompactionExecutionResult,
        aggregate: ContextAggregateVerificationResult,
    ) -> ContextCompressionRecordV2:
        removed = tuple(
            dict.fromkeys(
                group_id
                for action in execution.action_results
                for group_id in action.loss_report.removed_group_ids
            )
        )
        replaced = tuple(
            dict.fromkeys(
                group_id
                for action in execution.action_results
                for group_id in action.loss_report.replaced_group_ids
            )
        )
        retained = tuple(group.group_id for group in result.groups)
        source_refs = tuple(
            dict.fromkeys(
                ref
                for group in source.groups
                for ref in group.source_refs
            )
        )
        loss_reports = [action.loss_report for action in execution.action_results]
        loss_report = ContextLossReport(
            removed_group_ids=removed,
            replaced_group_ids=replaced,
            omitted_span_refs=tuple(
                dict.fromkeys(ref for item in loss_reports for ref in item.omitted_span_refs)
            ),
            omitted_topics=tuple(
                dict.fromkeys(topic for item in loss_reports for topic in item.omitted_topics)
            ),
            unresolved_questions=tuple(
                dict.fromkeys(question for item in loss_reports for question in item.unresolved_questions)
            ),
            loss_risk=_max_loss_risk(loss_reports),
        )
        return ContextCompressionRecordV2(
            run_id=source.run_id,
            stage_id=source.stage_id,
            source_snapshot_id=source.snapshot_id,
            source_snapshot_checksum=source.checksum,
            result_snapshot_id=result.snapshot_id,
            result_snapshot_checksum=result.checksum,
            plan_id=plan.plan_id,
            policy_revision=request.policy.policy_revision,
            action_results=execution.action_results,
            before_input_tokens=initial.input_tokens,
            after_input_tokens=final.input_tokens,
            retained_group_ids=retained,
            removed_group_ids=removed,
            replaced_group_ids=replaced,
            protected_group_ids=plan.protected_group_ids,
            reconstruction_refs=execution.reconstruction_refs,
            source_refs=source_refs,
            summary_refs=tuple(ref for ref in execution.summary_refs if ref),
            loss_report=loss_report,
            gate_results=tuple(gate.to_dict() for gate in aggregate.gates),
            aggregate_verdict=aggregate.outcome,
            reason_code=aggregate.reason_code,
            profile_revision=final.physical_profile_revision,
            tokenizer_revision=final.tokenizer_revision,
            normalizer_revision=final.normalizer_revision,
            prepared_fingerprint=final.prepared_fingerprint,
            initial_admission_evidence_id=initial.evidence_id,
            final_admission_evidence_id=final.evidence_id,
            materialization_revision=final.materialization_revision,
        )


def _runtime_status_for_planning(
    status: ContextCompactionPlanningStatus,
) -> ContextCompactionRuntimeStatus:
    return {
        ContextCompactionPlanningStatus.NO_COMPACTION_REQUIRED: ContextCompactionRuntimeStatus.NO_COMPACTION_REQUIRED,
        ContextCompactionPlanningStatus.PROTECTED_CONTEXT_EXCEEDS_WINDOW: ContextCompactionRuntimeStatus.PROTECTED_CONTEXT_EXCEEDS_WINDOW,
        ContextCompactionPlanningStatus.NO_ALLOWED_COMPACTION: ContextCompactionRuntimeStatus.NO_ALLOWED_COMPACTION,
        ContextCompactionPlanningStatus.ACTION_BUDGET_EXHAUSTED: ContextCompactionRuntimeStatus.ACTION_BUDGET_EXHAUSTED,
    }[status]


def _runtime_status_for_execution(
    status: ContextCompactionExecutionStatus,
) -> ContextCompactionRuntimeStatus:
    if status is ContextCompactionExecutionStatus.ACTION_BUDGET_EXHAUSTED:
        return ContextCompactionRuntimeStatus.ACTION_BUDGET_EXHAUSTED
    return ContextCompactionRuntimeStatus.SUMMARY_REJECTED


def _max_loss_risk(reports: list[ContextLossReport]) -> ContextLossRisk:
    if not reports:
        return ContextLossRisk.NONE
    order = {
        ContextLossRisk.NONE: 0,
        ContextLossRisk.LOW: 1,
        ContextLossRisk.MEDIUM: 2,
        ContextLossRisk.HIGH: 3,
        ContextLossRisk.UNKNOWN: 4,
    }
    return max((report.loss_risk for report in reports), key=lambda risk: order[risk])


__all__ = [
    "ContextCompactionRuntime",
    "ContextCompactionRuntimeRequest",
    "ContextCompactionRuntimeResult",
    "ContextCompactionRuntimeStatus",
    "ContextPhysicalMaterializerPort",
]
