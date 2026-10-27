from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from framework.events.errors import (
    EventIncompleteHistoryError,
    EventReplayMismatchError,
    EventStoreCorruptionError,
)
from framework.events.canonical import checksum_for
from framework.harness.control_plane.compensation_runtime import (
    compensation_binding_versions,
    compensation_entry_for_node,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.decision import (
    HarnessGraphDecision,
    HarnessGraphDecisionType,
)
from framework.harness.control_plane.graph_evaluator import (
    HarnessAcceptedGraphObservation,
    HarnessGraphObservationType,
)
from framework.harness.graph.operations import HarnessGraphRunOperation
from framework.harness.control_plane.graph_runtime import (
    HarnessGraphActivity,
    HarnessGraphActivityResult,
    HarnessGraphActivityResultCommit,
    HarnessGraphActivityResultStatus,
    HarnessGraphCommitKind,
    HarnessGraphDecisionCommit,
    HarnessGraphObservationCommit,
    HarnessGraphProjectionCommit,
    HarnessGraphRecovery,
    HarnessGraphTransitionPort,
    graph_reference,
    initial_graph_state,
    validate_graph_activity_result,
)
from framework.harness.control_plane.graph_state import (
    HarnessActiveActivityState,
    HarnessAttemptEvidenceReference,
    HarnessBranchOutputReference,
    HarnessCompensationEntry,
    HarnessCompensationStatus,
    HarnessEvidenceKind,
    HarnessGraphBudgetState,
    HarnessGraphState,
    HarnessJoinKind,
    HarnessJoinState,
    HarnessJoinStatus,
    HarnessLoopCounterState,
    HarnessLoopIteration,
    HarnessLoopStatus,
    HarnessNodeInstanceIdentity,
    HarnessNodeInstanceState,
    HarnessNodeInstanceStatus,
    HarnessPendingSideEffectScope,
    HarnessPendingSideEffectState,
    HarnessPendingSideEffectStatus,
    HarnessWaitRegistration,
    HarnessWaitStatus,
    RunLifecycle,
    RunOutcome,
)
from framework.harness.control_plane.state import (
    HarnessRunSpec,
    HarnessStepStatus,
)
from framework.harness.side_effects.models import (
    HarnessSideEffectDecision,
    HarnessSideEffectDecisionStatus,
    HarnessSideEffectOrigin,
    HarnessSideEffectOutcome,
    HarnessSideEffectOutcomeStatus,
)
from framework.harness.side_effects.ports import HarnessSideEffectStorePort
from framework.harness.graph.bindings import HarnessActivityCapabilities
from framework.harness.graph.canonical import (
    canonical_checksum,
    freeze_json,
    required_text,
    thaw_json,
)
from framework.harness.graph.model import (
    HarnessControlNode,
    HarnessContractKind,
    HarnessContractReference,
    HarnessExecutableNode,
    HarnessGraphEdgeKind,
    HarnessGraphNode,
    HarnessGraphNodeKind,
    HarnessMergeKind,
    NormalizedHarnessGraph,
)
from framework.harness.graph.dsl import WaitKind
from framework.harness.graph.validation import HarnessGraphPreflightPolicy
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
)
from framework.harness.waits.models import (
    HarnessSignalInboxEntry,
    HarnessSignalInboxEntryStatus,
    HarnessWaitApprovalEvidenceRecord,
    HarnessWaitCancellationRecord,
    HarnessWaitCauseKind,
    HarnessEarlySignalRetentionPolicy,
    HarnessWaitRegistrationRecord,
    HarnessWaitScope,
    HarnessWaitSignal,
    HarnessWaitSignalMatch,
    HarnessWaitTimeoutRecord,
    HarnessWaitTimerWakeRecord,
)
from framework.harness.waits.ports import HarnessTimerWakePort


_WAIT_SIGNAL_RETENTION_POLICY = HarnessEarlySignalRetentionPolicy()


_CONTROL_SUCCESS_TYPES = frozenset(
    {
        HarnessGraphDecisionType.COMPLETE_CONTROL_NODE,
        HarnessGraphDecisionType.OPEN_FORK,
        HarnessGraphDecisionType.SELECT_CHOICE,
        HarnessGraphDecisionType.SATISFY_JOIN,
        HarnessGraphDecisionType.START_LOOP_ITERATION,
        HarnessGraphDecisionType.EXIT_LOOP,
        HarnessGraphDecisionType.EXHAUST_LOOP,
        HarnessGraphDecisionType.APPLY_MERGE,
        HarnessGraphDecisionType.SELECT_PARALLEL_WINNER,
    }
)
_CONTROL_FAILURE_TYPES = frozenset({HarnessGraphDecisionType.FAIL_JOIN})
_STEP_NODE_DECISION_TYPES = frozenset(
    {
        HarnessGraphDecisionType.ENTER_STEP_PHASE,
        HarnessGraphDecisionType.DISPATCH_ACTIVITY,
        HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT,
        HarnessGraphDecisionType.COMPLETE_NODE,
        HarnessGraphDecisionType.FAIL_NODE,
        HarnessGraphDecisionType.RETRY_NODE,
        HarnessGraphDecisionType.REPLAN_NODE,
        HarnessGraphDecisionType.ROUTE_TO_REPAIR,
        HarnessGraphDecisionType.WAIT_NODE,
    }
)
_RUN_DECISION_TYPES = frozenset(
    {
        HarnessGraphDecisionType.PROJECT_RUN_WAITING,
        HarnessGraphDecisionType.COMPLETE_RUN,
        HarnessGraphDecisionType.HALT_RUN,
    }
)
_CONTROL_DECISION_NODE_KINDS = MappingProxyType(
    {
        HarnessGraphDecisionType.COMPLETE_CONTROL_NODE: frozenset(
            {
                HarnessGraphNodeKind.CHOICE_JOIN,
                HarnessGraphNodeKind.LOOP_JOIN,
                HarnessGraphNodeKind.TERMINAL,
            }
        ),
        HarnessGraphDecisionType.OPEN_FORK: frozenset(
            {HarnessGraphNodeKind.FORK_ALL, HarnessGraphNodeKind.FORK_ANY}
        ),
        HarnessGraphDecisionType.SELECT_CHOICE: frozenset(
            {HarnessGraphNodeKind.CHOICE}
        ),
        HarnessGraphDecisionType.SATISFY_JOIN: frozenset(
            {HarnessGraphNodeKind.JOIN_ALL, HarnessGraphNodeKind.JOIN_ANY}
        ),
        HarnessGraphDecisionType.FAIL_JOIN: frozenset(
            {HarnessGraphNodeKind.JOIN_ALL, HarnessGraphNodeKind.JOIN_ANY}
        ),
        HarnessGraphDecisionType.SELECT_PARALLEL_WINNER: frozenset(
            {HarnessGraphNodeKind.JOIN_ANY}
        ),
        HarnessGraphDecisionType.START_LOOP_ITERATION: frozenset(
            {HarnessGraphNodeKind.LOOP_GUARD}
        ),
        HarnessGraphDecisionType.EXIT_LOOP: frozenset(
            {HarnessGraphNodeKind.LOOP_GUARD}
        ),
        HarnessGraphDecisionType.EXHAUST_LOOP: frozenset(
            {HarnessGraphNodeKind.LOOP_GUARD}
        ),
        HarnessGraphDecisionType.APPLY_MERGE: frozenset({HarnessGraphNodeKind.MERGE}),
    }
)
_RUNNING_CONTROL_DECISIONS = frozenset(
    {
        HarnessGraphDecisionType.SATISFY_JOIN,
        HarnessGraphDecisionType.FAIL_JOIN,
        HarnessGraphDecisionType.SELECT_PARALLEL_WINNER,
    }
)
_BUDGET_CONSUMPTIONS = MappingProxyType(
    {
        HarnessGraphDecisionType.ACTIVATE_NODE: MappingProxyType(
            {"node_activations": 1}
        ),
        HarnessGraphDecisionType.OPEN_FORK: MappingProxyType({"node_activations": 1}),
        HarnessGraphDecisionType.SCHEDULE_COMPENSATION: MappingProxyType(
            {"node_activations": 1, "compensations": 1}
        ),
        HarnessGraphDecisionType.ENTER_STEP_PHASE: MappingProxyType({"turns": 1}),
        HarnessGraphDecisionType.DISPATCH_ACTIVITY: MappingProxyType(
            {"turns": 1, "worker_calls": 1}
        ),
        HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT: MappingProxyType({"turns": 1}),
        HarnessGraphDecisionType.RETRY_NODE: MappingProxyType({"retries": 1}),
        HarnessGraphDecisionType.REPLAN_NODE: MappingProxyType({"replans": 1}),
    }
)


@runtime_checkable
class HarnessGraphActivityDispatcherPort(Protocol):
    def dispatch(self, activity: HarnessGraphActivity) -> None:
        """Dispatch one already committed activity descriptor."""
        ...


@runtime_checkable
class HarnessGraphActivityReconciliationPort(Protocol):
    def reconcile(self, activity: HarnessGraphActivity) -> bool:
        """Return whether recovery may redeliver an active descriptor."""
        ...


@dataclass(frozen=True, slots=True)
class HarnessGraphActivityCancellationRequest:
    run_id: str
    activity_id: str
    node_instance_id: str
    attempt: int
    idempotency_key: str
    fencing_generation: int
    causal_decision_checksum: str
    reason_code: str
    request_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "activity_id",
            "node_instance_id",
            "idempotency_key",
            "reason_code",
        ):
            object.__setattr__(
                self,
                field_name,
                required_text(getattr(self, field_name), field_name),
            )
        _positive_int(self.attempt, "attempt")
        _positive_int(self.fencing_generation, "fencing_generation")
        if not _is_checksum_ref(self.causal_decision_checksum):
            raise HarnessValidationError(
                "cancellation request requires an exact causal decision",
                code="graph_cancellation_decision_ref_invalid",
            )
        object.__setattr__(
            self,
            "request_checksum",
            canonical_checksum(
                {
                    "run_id": self.run_id,
                    "activity_id": self.activity_id,
                    "node_instance_id": self.node_instance_id,
                    "attempt": self.attempt,
                    "idempotency_key": self.idempotency_key,
                    "fencing_generation": self.fencing_generation,
                    "causal_decision_checksum": self.causal_decision_checksum,
                    "reason_code": self.reason_code,
                }
            ),
        )


@runtime_checkable
class HarnessGraphActivityCancellationDispatcherPort(Protocol):
    def request_cancellation(
        self,
        request: HarnessGraphActivityCancellationRequest,
    ) -> None:
        """Request cooperative cancellation after its decision is durable."""
        ...


@runtime_checkable
class HarnessGraphConcurrentActivityDispatcherPort(
    HarnessGraphActivityDispatcherPort,
    HarnessGraphActivityCancellationDispatcherPort,
    Protocol,
):
    def concurrency_capabilities_for(
        self,
        activity_ref: HarnessContractReference,
    ) -> HarnessActivityCapabilities | None:
        """Return attempt-safety evidence for one exact activity contract."""
        ...


@dataclass(frozen=True, slots=True)
class HarnessGraphAppliedDecision:
    state: HarnessGraphState
    budget_reservations: Mapping[str, Any] = field(default_factory=dict)
    budget_consumptions: Mapping[str, Any] = field(default_factory=dict)
    activity: HarnessGraphActivity | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, HarnessGraphState):
            raise TypeError("state must be HarnessGraphState")
        reservations = _freeze_counter_delta(
            self.budget_reservations,
            "budget_reservations",
        )
        consumptions = _freeze_counter_delta(
            self.budget_consumptions,
            "budget_consumptions",
        )
        if reservations != consumptions:
            raise HarnessValidationError(
                "atomic graph budget reservation must be consumed by the same transition",
                code="graph_budget_reservation_mismatch",
            )
        if self.activity is not None and not isinstance(
            self.activity,
            HarnessGraphActivity,
        ):
            raise TypeError("activity must be HarnessGraphActivity")
        object.__setattr__(self, "budget_reservations", reservations)
        object.__setattr__(self, "budget_consumptions", consumptions)


class HarnessGraphDecisionApplier:
    __slots__ = ()

    def apply(
        self,
        state: HarnessGraphState,
        graph: NormalizedHarnessGraph,
        decision: HarnessGraphDecision,
        *,
        decision_sequence: int,
        projection_sequence: int,
        activity_input_ref: str | None = None,
        accepted_evidence_refs: tuple[str, ...] = (),
        side_effect_outcome_ref: str | None = None,
    ) -> HarnessGraphAppliedDecision:
        _validate_graph_decision(
            state,
            graph,
            decision,
            accepted_evidence_refs=accepted_evidence_refs,
            side_effect_outcome_ref=side_effect_outcome_ref,
        )
        _positive_int(decision_sequence, "decision_sequence")
        _positive_int(projection_sequence, "projection_sequence")
        if projection_sequence != decision_sequence + 1:
            raise HarnessValidationError(
                "graph projection must immediately follow its causal decision",
                code="graph_decision_projection_sequence_mismatch",
            )
        if decision_sequence <= state.last_event_sequence:
            raise EventReplayMismatchError(
                sequence=state.last_event_sequence,
                reason="graph decision sequence does not follow the projection",
            )
        consumptions = dict(_BUDGET_CONSUMPTIONS.get(decision.decision_type, {}))
        budgets = _consume_budgets(state.budgets, consumptions)
        applied = replace(state, budgets=budgets, projection_checksum=None)
        activity: HarnessGraphActivity | None = None
        if decision.decision_type is HarnessGraphDecisionType.SCHEDULE_COMPENSATION:
            applied = _schedule_compensation(
                applied,
                graph,
                decision,
                projection_sequence=projection_sequence,
            )
        elif decision.decision_type is HarnessGraphDecisionType.ACTIVATE_NODE:
            applied = _activate_node(
                applied,
                graph,
                decision,
                projection_sequence=projection_sequence,
            )
        elif decision.decision_type is HarnessGraphDecisionType.PREPARE_SIDE_EFFECT:
            applied = _apply_prepare_side_effect(
                applied,
                graph,
                decision,
                decision_sequence=decision_sequence,
                projection_sequence=projection_sequence,
            )
        elif decision.decision_type in _STEP_NODE_DECISION_TYPES:
            applied, activity = _apply_step_decision(
                applied,
                graph,
                decision,
                decision_sequence=decision_sequence,
                projection_sequence=projection_sequence,
                activity_input_ref=activity_input_ref,
                side_effect_outcome_ref=side_effect_outcome_ref,
            )
        elif decision.decision_type in _CONTROL_SUCCESS_TYPES:
            if decision.decision_type is HarnessGraphDecisionType.APPLY_MERGE:
                applied = _apply_merge_decision(
                    applied,
                    graph,
                    decision,
                    projection_sequence=projection_sequence,
                )
            else:
                applied = _apply_control_decision(
                    applied,
                    decision,
                    projection_sequence=projection_sequence,
                    succeeded=True,
                )
            if decision.decision_type is HarnessGraphDecisionType.OPEN_FORK:
                applied = _open_parallel_join(
                    applied,
                    graph,
                    decision,
                    projection_sequence=projection_sequence,
                )
            elif decision.decision_type in {
                HarnessGraphDecisionType.START_LOOP_ITERATION,
                HarnessGraphDecisionType.EXIT_LOOP,
                HarnessGraphDecisionType.EXHAUST_LOOP,
            }:
                applied = _apply_loop_counter_transition(
                    applied,
                    graph,
                    decision,
                    projection_sequence=projection_sequence,
                )
        elif decision.decision_type in _CONTROL_FAILURE_TYPES:
            applied = _apply_control_decision(
                applied,
                decision,
                projection_sequence=projection_sequence,
                succeeded=False,
            )
        elif decision.decision_type is HarnessGraphDecisionType.REQUEST_BRANCH_CANCEL:
            applied = _request_branch_cancel(
                applied,
                decision,
                projection_sequence=projection_sequence,
            )
        elif decision.decision_type is HarnessGraphDecisionType.REGISTER_WAIT:
            applied = _register_wait(
                applied,
                graph,
                decision,
                decision_sequence=decision_sequence,
                projection_sequence=projection_sequence,
            )
        elif decision.decision_type is HarnessGraphDecisionType.RESUME_WAIT:
            applied = _resume_wait(
                applied,
                decision,
                projection_sequence=projection_sequence,
            )
        elif decision.decision_type in _RUN_DECISION_TYPES:
            applied = _apply_run_decision(
                applied,
                graph,
                decision,
                decision_sequence=decision_sequence,
                projection_sequence=projection_sequence,
                side_effect_outcome_ref=side_effect_outcome_ref,
            )
        else:  # pragma: no cover - enum exhaustiveness guard
            raise HarnessValidationError(
                "graph decision type has no registered Control Plane handler",
                code="unregistered_graph_decision_handler",
                details={"decision_type": decision.decision_type.value},
            )
        applied = _record_loop_body_terminal(
            applied,
            graph,
            decision,
            projection_sequence=projection_sequence,
        )
        applied = _record_parallel_branch_terminal(
            applied,
            graph,
            decision,
            projection_sequence=projection_sequence,
        )
        if applied.last_event_sequence != projection_sequence:
            applied = replace(
                applied,
                last_event_sequence=projection_sequence,
                projection_checksum=None,
            )
        return HarnessGraphAppliedDecision(
            state=applied,
            budget_reservations=consumptions,
            budget_consumptions=consumptions,
            activity=activity,
        )

    def apply_activity_result(
        self,
        state: HarnessGraphState,
        activity: HarnessGraphActivity,
        result: HarnessGraphActivityResult,
        *,
        result_sequence: int,
        projection_sequence: int,
    ) -> HarnessGraphState:
        validate_graph_activity_result(activity, result)
        _positive_int(result_sequence, "result_sequence")
        _positive_int(projection_sequence, "projection_sequence")
        if projection_sequence != result_sequence + 1:
            raise HarnessValidationError(
                "activity result projection must immediately follow its result",
                code="graph_activity_projection_sequence_mismatch",
            )
        if result_sequence <= state.last_event_sequence:
            raise EventReplayMismatchError(
                sequence=state.last_event_sequence,
                reason="graph activity result does not follow the projection",
            )
        active = next(
            (
                item
                for item in state.active_activities
                if item.activity_id == activity.activity_id
            ),
            None,
        )
        node = _node_instance(state, activity.node_instance_id)
        compensation_entry = (
            compensation_entry_for_node(state, node)
            if node.status is HarnessNodeInstanceStatus.COMPENSATING
            else None
        )
        mismatches: list[str] = []
        if active is None:
            mismatches.append("active_activity")
        else:
            if active.node_instance_id != node.instance_id:
                mismatches.append("node_instance_id")
            if active.attempt != result.attempt or node.attempt != result.attempt:
                mismatches.append("attempt")
            if active.idempotency_key != result.idempotency_key:
                mismatches.append("idempotency_key")
            if active.fencing_generation != result.fencing_generation:
                mismatches.append("fencing_generation")
            if active.activity_ref != result.activity_ref:
                mismatches.append("activity_ref")
        if mismatches:
            raise HarnessValidationError(
                "activity result does not match the active graph attempt",
                code="graph_activity_result_state_mismatch",
                details={"mismatches": sorted(set(mismatches))},
            )
        evidence = HarnessAttemptEvidenceReference(
            result.evidence_ref,
            HarnessEvidenceKind.ACTIVITY_RESULT,
            node.instance_id,
            result.attempt,
            result_sequence,
            contract_ref=result.activity_ref,
            payload_ref=result.payload_ref,
        )
        metadata = thaw_json(node.metadata)
        metadata.update(
            {
                "activity_result_ref": result.evidence_ref,
                "activity_result_status": result.status.value,
                "activity_payload_ref": result.payload_ref,
            }
        )
        output_refs = thaw_json(node.output_refs)
        output_refs["activity_result"] = result.payload_ref
        lineage = result.result_lineage
        if lineage is not None:
            metadata.update(
                {
                    "activity_result_attempt_id": lineage.attempt_id,
                    "activity_result_lineage_ref": lineage.lineage_checksum,
                    "activity_result_policy_version": lineage.policy_version,
                }
            )
            lineage_refs = output_refs.get("activity_result_lineage_refs", {})
            if not isinstance(lineage_refs, Mapping):
                raise HarnessValidationError(
                    "graph result lineage history must be an object",
                    code="graph_result_lineage_state_mismatch",
                )
            lineage_refs = dict(lineage_refs)
            reference_projection = lineage.reference_projection()
            existing_lineage = lineage_refs.get(lineage.attempt_id)
            if existing_lineage not in (None, reference_projection):
                raise HarnessValidationError(
                    "graph result attempt conflicts with projected lineage",
                    code="graph_result_lineage_state_mismatch",
                )
            lineage_refs[lineage.attempt_id] = reference_projection
            output_refs["activity_result_lineage"] = lineage.control_projection()
            output_refs["activity_result_lineage_refs"] = lineage_refs
        updated_node = replace(
            node,
            output_refs=output_refs,
            evidence_refs=(*node.evidence_refs, evidence),
            last_event_sequence=projection_sequence,
            metadata=metadata,
        )
        lifecycle = state.lifecycle
        outcome = state.outcome
        terminal_reason = state.terminal_reason_code
        terminal_evidence = state.terminal_evidence_ref
        state_metadata = thaw_json(state.metadata)
        uncertain = result.status is HarnessGraphActivityResultStatus.INDETERMINATE or (
            result.status
            in {
                HarnessGraphActivityResultStatus.FAILED,
                HarnessGraphActivityResultStatus.TIMEOUT,
                HarnessGraphActivityResultStatus.CANCELLED,
            }
            and not result.termination_confirmed
        )
        cancellation_was_requested = (
            node.status is HarnessNodeInstanceStatus.CANCEL_REQUESTED
        )
        retain_active = uncertain and not result.termination_confirmed
        compensation_stack = state.compensation_stack
        if uncertain:
            updated_node = replace(
                updated_node,
                status=HarnessNodeInstanceStatus.HALTED,
                step_status=HarnessStepStatus.HALTED,
                error_code="activity_outcome_indeterminate",
                terminal_reason="activity_outcome_indeterminate",
            )
            lifecycle = RunLifecycle.HALTED
            outcome = RunOutcome.INDETERMINATE
            terminal_reason = "activity_outcome_indeterminate"
            terminal_evidence = result.evidence_ref
            state_metadata["manual_intervention"] = {
                "required": True,
                "reason_code": "activity_outcome_indeterminate",
                "evidence_ref": result.evidence_ref,
                "node_instance_id": node.instance_id,
                "activity_id": activity.activity_id,
            }
            if compensation_entry is not None:
                compensation_entry = replace(
                    compensation_entry,
                    status=HarnessCompensationStatus.INDETERMINATE,
                    outcome_ref=result.evidence_ref,
                    last_event_sequence=projection_sequence,
                )
                compensation_stack = _replace_compensation_entry(
                    compensation_stack,
                    compensation_entry,
                )
        elif (
            result.status is HarnessGraphActivityResultStatus.CANCELLED
            and result.termination_confirmed
        ):
            if compensation_entry is None:
                updated_node = replace(
                    updated_node,
                    status=HarnessNodeInstanceStatus.CANCELLED,
                    step_status=HarnessStepStatus.HALTED,
                    error_code="branch_cancelled",
                    terminal_reason="branch_cancelled",
                )
            else:
                updated_node = replace(
                    updated_node,
                    status=HarnessNodeInstanceStatus.FAILED,
                    step_status=HarnessStepStatus.FAILED,
                    error_code="compensation_activity_cancelled",
                    terminal_reason="compensation_activity_cancelled",
                )
                compensation_entry = replace(
                    compensation_entry,
                    status=HarnessCompensationStatus.FAILED,
                    outcome_ref=result.evidence_ref,
                    last_event_sequence=projection_sequence,
                )
                compensation_stack = _replace_compensation_entry(
                    compensation_stack,
                    compensation_entry,
                )
        elif cancellation_was_requested and result.status in {
            HarnessGraphActivityResultStatus.SUCCEEDED,
            HarnessGraphActivityResultStatus.FAILED,
            HarnessGraphActivityResultStatus.TIMEOUT,
        }:
            reconciled_metadata = thaw_json(updated_node.metadata)
            reconciled_metadata["cancel_reconciliation_status"] = result.status.value
            updated_node = replace(
                updated_node,
                status=HarnessNodeInstanceStatus.CANCELLED,
                step_status=HarnessStepStatus.HALTED,
                error_code="branch_cancelled_after_activity_completion",
                terminal_reason="branch_cancelled_after_activity_completion",
                metadata=reconciled_metadata,
            )
        projected = replace(
            state,
            node_instances=_replace_node(state.node_instances, updated_node),
            active_activities=(
                state.active_activities
                if retain_active
                else tuple(
                    item
                    for item in state.active_activities
                    if item.activity_id != activity.activity_id
                )
            ),
            lifecycle=lifecycle,
            outcome=outcome,
            compensation_stack=compensation_stack,
            last_event_sequence=projection_sequence,
            terminal_reason_code=terminal_reason,
            terminal_evidence_ref=terminal_evidence,
            metadata=state_metadata,
            projection_checksum=None,
        )
        if updated_node.status is HarnessNodeInstanceStatus.CANCELLED:
            projected = _record_cancelled_branch_result(
                projected,
                updated_node,
                result.evidence_ref,
                projection_sequence=projection_sequence,
            )
        return projected

    def apply_observation(
        self,
        state: HarnessGraphState,
        graph: NormalizedHarnessGraph,
        observation: HarnessAcceptedGraphObservation,
        *,
        observation_sequence: int,
        projection_sequence: int,
    ) -> HarnessGraphState:
        if not isinstance(observation, HarnessAcceptedGraphObservation):
            raise TypeError("observation must be HarnessAcceptedGraphObservation")
        _positive_int(observation_sequence, "observation_sequence")
        _positive_int(projection_sequence, "projection_sequence")
        if projection_sequence != observation_sequence + 1:
            raise HarnessValidationError(
                "graph observation projection must immediately follow its cause",
                code="graph_observation_projection_sequence_mismatch",
            )
        if observation_sequence <= state.last_event_sequence:
            raise EventReplayMismatchError(
                sequence=state.last_event_sequence,
                reason="graph observation does not follow the projection",
            )
        if observation.event_sequence != observation_sequence:
            raise HarnessValidationError(
                "graph observation sequence does not match its causal commit",
                code="graph_observation_sequence_mismatch",
            )
        if observation.observation_type is HarnessGraphObservationType.RUN_OPERATION:
            return _apply_run_operation_observation(
                state,
                observation,
                projection_sequence=projection_sequence,
            )
        node = _node_instance(state, observation.node_instance_id)
        definition = _definition(graph, observation.node_id)
        is_merge_result = (
            observation.observation_type is HarnessGraphObservationType.MERGE_RESULT
            and isinstance(definition, HarnessControlNode)
            and definition.merge is not None
        )
        is_wait_cause = (
            observation.observation_type is HarnessGraphObservationType.WAIT_CAUSE
            and isinstance(definition, HarnessControlNode)
            and definition.node_kind is HarnessGraphNodeKind.WAIT
            and definition.wait is not None
        )
        if (
            not isinstance(definition, HarnessExecutableNode)
            and not is_merge_result
            and not is_wait_cause
        ):
            raise HarnessValidationError(
                "graph observation targets an incompatible node kind",
                code="graph_observation_node_kind_mismatch",
            )
        if (
            node.identity.node_id != observation.node_id
            or node.attempt != observation.attempt
        ):
            raise HarnessValidationError(
                "graph observation belongs to another node attempt",
                code="graph_observation_identity_mismatch",
            )
        if is_wait_cause:
            expected_contracts = _graph_observation_contracts(
                observation,
                definition,
                graph,
            )
            if observation.contract_ref not in expected_contracts:
                raise HarnessValidationError(
                    "Wait cause does not match its pinned signal schema",
                    code="graph_observation_contract_mismatch",
                )
            return _apply_wait_cause_observation(
                state,
                node,
                definition,
                observation,
                projection_sequence=projection_sequence,
            )
        expected_contracts = _graph_observation_contracts(
            observation,
            definition,
            graph,
        )
        if observation.contract_ref not in expected_contracts:
            raise HarnessValidationError(
                "graph observation does not match its pinned contract",
                code="graph_observation_contract_mismatch",
            )
        logical_identity = _graph_observation_logical_identity(observation)
        evidence_kind = (
            HarnessEvidenceKind.ACTIVITY_RESULT
            if observation.observation_type
            in {
                HarnessGraphObservationType.VERIFIED_OUTPUT,
                HarnessGraphObservationType.WORKER_STATUS,
            }
            else HarnessEvidenceKind.APPROVAL
            if observation.observation_type is HarnessGraphObservationType.APPROVAL
            else HarnessEvidenceKind.SIDE_EFFECT_OUTCOME
            if observation.observation_type
            in {
                HarnessGraphObservationType.SIDE_EFFECT_OUTCOME,
                HarnessGraphObservationType.SIDE_EFFECT_FAILURE,
            }
            else HarnessEvidenceKind.MERGE_RESULT
            if observation.observation_type is HarnessGraphObservationType.MERGE_RESULT
            else HarnessEvidenceKind.GATE_RESULT
        )
        evidence = HarnessAttemptEvidenceReference(
            observation.evidence_ref,
            evidence_kind,
            node.instance_id,
            observation.attempt,
            observation_sequence,
            contract_ref=observation.contract_ref,
            payload_ref=observation.payload_ref,
        )
        metadata = thaw_json(node.metadata)
        accepted = list(metadata.get("accepted_observations", ()))
        accepted.append(
            {
                "logical_identity": logical_identity,
                "observation_checksum": observation.observation_checksum,
            }
        )
        metadata["accepted_observations"] = accepted
        if observation.observation_type is HarnessGraphObservationType.APPROVAL:
            metadata["approval_granted"] = observation.payload["approved"]
            if observation.payload["approved"]:
                metadata["approval_evidence_ref"] = observation.payload["approval_ref"]
            else:
                metadata["approval_reason_ref"] = observation.payload["reason_ref"]
        elif observation.observation_type in {
            HarnessGraphObservationType.SIDE_EFFECT_OUTCOME,
            HarnessGraphObservationType.SIDE_EFFECT_FAILURE,
        }:
            return _apply_side_effect_observation(
                state,
                node,
                observation,
                evidence,
                metadata=metadata,
                projection_sequence=projection_sequence,
            )
        elif observation.observation_type is HarnessGraphObservationType.MERGE_RESULT:
            return _apply_merge_result_observation(
                state,
                node,
                definition,
                observation,
                evidence,
                metadata=metadata,
                projection_sequence=projection_sequence,
            )
        updated = replace(
            node,
            evidence_refs=(*node.evidence_refs, evidence),
            last_event_sequence=projection_sequence,
            metadata=metadata,
        )
        return replace(
            state,
            node_instances=_replace_node(state.node_instances, updated),
            last_event_sequence=projection_sequence,
            projection_checksum=None,
        )


class HarnessGraphControlPlaneRuntime:
    __slots__ = (
        "_applier",
        "_dispatcher",
        "_port",
        "_side_effect_store",
        "_timer_wake_port",
    )

    def __init__(
        self,
        transition_port: HarnessGraphTransitionPort,
        *,
        activity_dispatcher: HarnessGraphActivityDispatcherPort | None = None,
        side_effect_store: HarnessSideEffectStorePort | None = None,
        timer_wake_port: HarnessTimerWakePort | None = None,
        applier: HarnessGraphDecisionApplier | None = None,
    ) -> None:
        if not isinstance(transition_port, HarnessGraphTransitionPort):
            raise TypeError("transition_port must implement HarnessGraphTransitionPort")
        if activity_dispatcher is not None and not isinstance(
            activity_dispatcher,
            HarnessGraphActivityDispatcherPort,
        ):
            raise TypeError(
                "activity_dispatcher must implement HarnessGraphActivityDispatcherPort"
            )
        if timer_wake_port is not None and not isinstance(
            timer_wake_port,
            HarnessTimerWakePort,
        ):
            raise TypeError("timer_wake_port must implement HarnessTimerWakePort")
        if side_effect_store is not None and not isinstance(
            side_effect_store,
            HarnessSideEffectStorePort,
        ):
            raise TypeError(
                "side_effect_store must implement HarnessSideEffectStorePort"
            )
        self._port = transition_port
        self._dispatcher = activity_dispatcher
        self._side_effect_store = side_effect_store
        self._timer_wake_port = timer_wake_port
        self._applier = applier or HarnessGraphDecisionApplier()

    @property
    def transition_port(self) -> HarnessGraphTransitionPort:
        return self._port

    def initialize(
        self,
        run_spec: HarnessRunSpec,
        graph: NormalizedHarnessGraph,
        policy: HarnessGraphPreflightPolicy,
        *,
        run_spec_checksum: str,
    ) -> HarnessGraphState:
        _require_graph_only_runtime_graph(graph)
        recovery = self._port.recover_graph(run_spec.run_id)
        if recovery.state is not None:
            self._validate_recovery_identity(
                recovery,
                graph,
                expected_run_id=run_spec.run_id,
                run_spec_checksum=run_spec_checksum,
            )
            return self.recover(
                run_spec.run_id,
                graph,
                run_spec_checksum=run_spec_checksum,
            )
        if recovery.expected_last_sequence != 0:
            raise EventIncompleteHistoryError(
                "graph run stream contains history without an initial projection"
            )
        state = initial_graph_state(
            run_spec,
            graph,
            policy,
            run_spec_checksum=run_spec_checksum,
            event_sequence=1,
            runtime_scope_metadata=_runtime_scope_metadata(self._port),
        )
        commit = self._port.initialize_graph(
            graph,
            state,
            run_spec_checksum=run_spec_checksum,
            occurred_at=run_spec.created_at,
            expected_last_sequence=0,
        )
        if commit.state.projection_checksum != state.projection_checksum:
            raise EventReplayMismatchError(
                sequence=commit.sequence,
                reason="graph initialization port returned a conflicting projection",
            )
        return commit.state

    def apply_decision(
        self,
        state: HarnessGraphState,
        graph: NormalizedHarnessGraph,
        decision: HarnessGraphDecision,
        *,
        run_spec_checksum: str,
        occurred_at: datetime,
        activity_input_ref: str | None = None,
        accepted_evidence_refs: tuple[str, ...] = (),
        side_effect_outcome_ref: str | None = None,
    ) -> HarnessGraphState:
        if _run_spec_checksum_from_state(state) != run_spec_checksum:
            raise EventReplayMismatchError(
                sequence=state.last_event_sequence,
                reason="graph state run spec checksum does not match the current request",
            )
        recovery = self._port.recover_graph(state.run_id)
        self._validate_recovery_identity(
            recovery,
            graph,
            expected_run_id=state.run_id,
            run_spec_checksum=run_spec_checksum,
        )
        existing = next(
            (
                item
                for item in recovery.decision_commits
                if item.decision.decision_checksum == decision.decision_checksum
            ),
            None,
        )
        if existing is not None:
            replayed = HarnessGraphDecisionCommit(
                decision,
                existing.sequence,
                existing.occurred_at,
                activity_input_ref=activity_input_ref,
                accepted_evidence_refs=accepted_evidence_refs,
                side_effect_outcome_ref=side_effect_outcome_ref,
            )
            if (
                replayed.decision != existing.decision
                or replayed.activity_input_ref != existing.activity_input_ref
                or replayed.accepted_evidence_refs != existing.accepted_evidence_refs
                or replayed.side_effect_outcome_ref
                != existing.side_effect_outcome_ref
            ):
                raise EventStoreCorruptionError(
                    "graph decision checksum resolves conflicting request content"
                )
            projected = _projection_for_cause(
                recovery,
                decision.decision_checksum,
            )
            if projected is not None:
                if recovery.state is None:  # pragma: no cover - recovery invariant
                    raise EventIncompleteHistoryError(
                        "graph decision recovery is missing current state"
                    )
                return recovery.state
            return self._project_decision(
                recovery,
                graph,
                existing,
            )
        if (
            recovery.pending_decisions
            or recovery.pending_activity_results
            or recovery.pending_observations
        ):
            raise HarnessValidationError(
                "graph recovery must reconcile committed work before a new decision",
                code="graph_recovery_required",
            )
        if recovery.state is None or (
            recovery.state.projection_checksum != state.projection_checksum
        ):
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="graph decision attempted from a stale in-memory projection",
            )
        if decision.decision_type is HarnessGraphDecisionType.DISPATCH_ACTIVITY and (
            self._dispatcher is None or activity_input_ref is None
        ):
            raise HarnessValidationError(
                "graph activity dispatch requires a dispatcher and durable input reference",
                code=(
                    "graph_activity_dispatcher_missing"
                    if self._dispatcher is None
                    else "graph_activity_input_ref_missing"
                ),
            )
        self._applier.apply(
            state,
            graph,
            decision,
            decision_sequence=recovery.expected_last_sequence + 1,
            projection_sequence=recovery.expected_last_sequence + 2,
            activity_input_ref=activity_input_ref,
            accepted_evidence_refs=accepted_evidence_refs,
            side_effect_outcome_ref=side_effect_outcome_ref,
        )
        if side_effect_outcome_ref is not None:
            _validate_durable_side_effect_outcome(
                self._side_effect_store,
                state,
                graph,
                decision,
                side_effect_outcome_ref,
            )
        commit = self._port.commit_graph_decision(
            decision,
            occurred_at=occurred_at,
            expected_last_sequence=recovery.expected_last_sequence,
            activity_input_ref=activity_input_ref,
            accepted_evidence_refs=accepted_evidence_refs,
            side_effect_outcome_ref=side_effect_outcome_ref,
        )
        refreshed = self._port.recover_graph(state.run_id)
        return self._project_decision(
            refreshed,
            graph,
            commit,
        )

    def accept_activity_result(
        self,
        result: HarnessGraphActivityResult,
        *,
        graph: NormalizedHarnessGraph,
        run_spec_checksum: str,
        occurred_at: datetime,
    ) -> HarnessGraphState:
        if not isinstance(result, HarnessGraphActivityResult):
            raise TypeError("result must be HarnessGraphActivityResult")
        activity = self._port.activity_for(result.activity_id)
        if activity is None:
            raise HarnessValidationError(
                "graph activity result references an unknown activity",
                code="graph_activity_identity_mismatch",
            )
        recovery = self._port.recover_graph(activity.run_id)
        self._validate_recovery_identity(
            recovery,
            graph,
            expected_run_id=activity.run_id,
            run_spec_checksum=run_spec_checksum,
        )
        existing = next(
            (
                item
                for item in recovery.activity_result_commits
                if item.result.activity_id == result.activity_id
            ),
            None,
        )
        if existing is not None:
            if existing.result != result:
                raise EventReplayMismatchError(
                    sequence=existing.sequence,
                    reason="graph activity produced a conflicting duplicate result",
                )
            projected = _projection_for_cause(recovery, result.result_checksum)
            if projected is not None:
                if recovery.state is None:  # pragma: no cover - recovery invariant
                    raise EventIncompleteHistoryError(
                        "graph activity result recovery is missing current state"
                    )
                return recovery.state
            return self._project_activity_result(recovery, activity, existing)
        if (
            recovery.pending_decisions
            or recovery.pending_activity_results
            or recovery.pending_observations
        ):
            raise HarnessValidationError(
                "graph recovery must reconcile committed work before accepting a result",
                code="graph_recovery_required",
            )
        validate_graph_activity_result(activity, result)
        if recovery.state is None:
            raise EventIncompleteHistoryError("graph activity result is missing state")
        self._applier.apply_activity_result(
            recovery.state,
            activity,
            result,
            result_sequence=recovery.expected_last_sequence + 1,
            projection_sequence=recovery.expected_last_sequence + 2,
        )
        commit = self._port.commit_graph_activity_result(
            result,
            occurred_at=occurred_at,
            expected_last_sequence=recovery.expected_last_sequence,
        )
        refreshed = self._port.recover_graph(activity.run_id)
        return self._project_activity_result(refreshed, activity, commit)

    def accept_observation(
        self,
        observation: HarnessAcceptedGraphObservation,
        *,
        run_id: str,
        graph: NormalizedHarnessGraph,
        run_spec_checksum: str,
        occurred_at: datetime,
    ) -> HarnessGraphState:
        if not isinstance(observation, HarnessAcceptedGraphObservation):
            raise TypeError("observation must be HarnessAcceptedGraphObservation")
        run_id = required_text(run_id, "run_id")
        recovery = self._port.recover_graph(run_id)
        if recovery.state is None:
            raise HarnessValidationError(
                "graph observation belongs to an unknown run",
                code="graph_observation_run_mismatch",
            )
        run_id = recovery.state.run_id
        self._validate_recovery_identity(
            recovery,
            graph,
            expected_run_id=run_id,
            run_spec_checksum=run_spec_checksum,
        )
        existing = next(
            (
                item
                for item in recovery.observation_commits
                if item.observation.observation_checksum
                == observation.observation_checksum
            ),
            None,
        )
        if existing is not None:
            projected = _projection_for_cause(
                recovery,
                observation.observation_checksum,
            )
            if projected is not None:
                if recovery.state is None:  # pragma: no cover - recovery invariant
                    raise EventIncompleteHistoryError(
                        "graph observation recovery is missing current state"
                    )
                return recovery.state
            return self._project_observation(recovery, graph, existing)
        if (
            recovery.pending_decisions
            or recovery.pending_activity_results
            or recovery.pending_observations
        ):
            raise HarnessValidationError(
                "graph recovery must reconcile committed work before accepting an observation",
                code="graph_recovery_required",
            )
        if recovery.state is None:
            raise EventIncompleteHistoryError(
                "graph observation is missing its durable state"
            )
        if observation.observation_type is HarnessGraphObservationType.SIDE_EFFECT_OUTCOME:
            _validate_durable_side_effect_observation(
                self._side_effect_store,
                recovery.state,
                graph,
                observation,
            )
        elif (
            observation.observation_type
            is HarnessGraphObservationType.SIDE_EFFECT_FAILURE
        ):
            _validate_durable_side_effect_failure_observation(
                self._side_effect_store,
                recovery.state,
                graph,
                observation,
            )
        expected_sequence = recovery.expected_last_sequence + 1
        if observation.event_sequence != expected_sequence:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="graph observation was built from a stale projection",
            )
        self._applier.apply_observation(
            recovery.state,
            graph,
            observation,
            observation_sequence=expected_sequence,
            projection_sequence=expected_sequence + 1,
        )
        commit = self._port.commit_graph_observation(
            observation,
            occurred_at=occurred_at,
            expected_last_sequence=recovery.expected_last_sequence,
        )
        refreshed = self._port.recover_graph(run_id)
        return self._project_observation(refreshed, graph, commit)

    def recover(
        self,
        run_id: str,
        graph: NormalizedHarnessGraph,
        *,
        run_spec_checksum: str,
    ) -> HarnessGraphState:
        run_id = required_text(run_id, "run_id")
        _require_graph_only_runtime_graph(graph)
        while True:
            recovery = self._port.recover_graph(run_id)
            self._validate_recovery_identity(
                recovery,
                graph,
                expected_run_id=run_id,
                run_spec_checksum=run_spec_checksum,
            )
            pending = tuple(
                sorted(
                    (
                        *(
                            ("decision", item.sequence, item)
                            for item in recovery.pending_decisions
                        ),
                        *(
                            ("result", item.sequence, item)
                            for item in recovery.pending_activity_results
                        ),
                        *(
                            ("observation", item.sequence, item)
                            for item in recovery.pending_observations
                        ),
                    ),
                    key=lambda item: item[1],
                )
            )
            if not pending:
                break
            if len(pending) != 1:
                raise EventIncompleteHistoryError(
                    "graph run contains multiple unprojected causal records"
                )
            kind, _, commit = pending[0]
            if kind == "decision":
                if not isinstance(commit, HarnessGraphDecisionCommit):
                    raise EventIncompleteHistoryError(
                        "graph pending decision record is invalid"
                    )
                self._project_decision(
                    recovery,
                    graph,
                    commit,
                )
            elif kind == "result":
                if not isinstance(commit, HarnessGraphActivityResultCommit):
                    raise EventIncompleteHistoryError(
                        "graph pending activity result record is invalid"
                    )
                activity = self._port.activity_for(commit.result.activity_id)
                if activity is None:
                    raise EventIncompleteHistoryError(
                        "graph activity result is missing its durable descriptor"
                    )
                self._project_activity_result(recovery, activity, commit)
            else:
                if not isinstance(commit, HarnessGraphObservationCommit):
                    raise EventIncompleteHistoryError(
                        "graph pending observation record is invalid"
                    )
                self._project_observation(recovery, graph, commit)
        recovery = self._port.recover_graph(run_id)
        state = recovery.state
        if state is None:
            raise EventIncompleteHistoryError("graph run has no durable state")
        result_activity_ids = {
            item.result.activity_id for item in recovery.activity_result_commits
        }
        active_activity_ids = {item.activity_id for item in state.active_activities}
        # Cancellation intent is durable and must be installed before a
        # recovery dispatch can create another physical attempt.
        self._recover_cancellation_requests(recovery)
        for activity in recovery.activities:
            marked_dispatched = activity.activity_id in recovery.dispatched_activity_ids
            if (
                activity.activity_id not in active_activity_ids
                or activity.activity_id in result_activity_ids
            ):
                continue
            # ``dispatched_activity_ids`` is an audit marker, not a completion
            # receipt.  A crash can occur after the marker write and before
            # the Graph result commit, so every active activity without a
            # result must be reconciled.  Only an in-process physical owner
            # can suppress a duplicate dispatch while its call is still live.
            if marked_dispatched:
                reconcile = (
                    None
                    if self._dispatcher is None
                    else getattr(self._dispatcher, "reconcile", None)
                )
                if not callable(reconcile):
                    # External dispatchers without the optional observer keep
                    # the durable marker as their in-flight ownership signal.
                    # The production physical dispatcher implements reconcile
                    # and therefore enables restart re-dispatch safely.
                    continue
                if not reconcile(activity):
                    continue
            self._dispatch_after_commit(activity)
        self._sync_timer_registrations(state)
        return self._port.recover_graph(run_id).state or state

    def _project_decision(
        self,
        recovery: HarnessGraphRecovery,
        graph: NormalizedHarnessGraph,
        commit: HarnessGraphDecisionCommit,
    ) -> HarnessGraphState:
        state = recovery.state
        if state is None:
            raise EventIncompleteHistoryError(
                "graph decision cannot project without an initialized state"
            )
        if recovery.expected_last_sequence != commit.sequence:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="graph decision projection does not follow its causal commit",
            )
        cancellation_request = self._cancellation_request_for_decision(
            state,
            commit.decision,
        )
        applied = self._applier.apply(
            state,
            graph,
            commit.decision,
            decision_sequence=commit.sequence,
            projection_sequence=commit.sequence + 1,
            activity_input_ref=commit.activity_input_ref,
            accepted_evidence_refs=commit.accepted_evidence_refs,
            side_effect_outcome_ref=commit.side_effect_outcome_ref,
        )
        projection = HarnessGraphProjectionCommit(
            HarnessGraphCommitKind.DECISION_PROJECTION,
            commit.decision.decision_checksum,
            state.projection_checksum,
            applied.state,
            commit.sequence + 1,
            commit.occurred_at,
            budget_reservations=applied.budget_reservations,
            budget_consumptions=applied.budget_consumptions,
            activity=applied.activity,
        )
        committed = self._port.commit_graph_projection(
            projection,
            expected_last_sequence=commit.sequence,
        )
        if committed != projection:
            raise EventReplayMismatchError(
                sequence=committed.sequence,
                reason="graph transition port returned a conflicting projection",
            )
        if applied.activity is not None:
            self._dispatch_after_commit(applied.activity)
        if cancellation_request is not None:
            self._dispatch_cancellation_after_commit(cancellation_request)
        self._sync_timer_registrations(committed.state)
        return committed.state

    def _project_activity_result(
        self,
        recovery: HarnessGraphRecovery,
        activity: HarnessGraphActivity,
        commit: HarnessGraphActivityResultCommit,
    ) -> HarnessGraphState:
        state = recovery.state
        if state is None:
            raise EventIncompleteHistoryError(
                "graph activity result cannot project without state"
            )
        if recovery.expected_last_sequence != commit.sequence:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="activity result projection does not follow its causal commit",
            )
        projected = self._applier.apply_activity_result(
            state,
            activity,
            commit.result,
            result_sequence=commit.sequence,
            projection_sequence=commit.sequence + 1,
        )
        projection = HarnessGraphProjectionCommit(
            HarnessGraphCommitKind.ACTIVITY_RESULT_PROJECTION,
            commit.result.result_checksum,
            state.projection_checksum,
            projected,
            commit.sequence + 1,
            commit.occurred_at,
        )
        committed = self._port.commit_graph_projection(
            projection,
            expected_last_sequence=commit.sequence,
        )
        if committed != projection:
            raise EventReplayMismatchError(
                sequence=committed.sequence,
                reason="graph transition port returned a conflicting result projection",
            )
        return committed.state

    def _project_observation(
        self,
        recovery: HarnessGraphRecovery,
        graph: NormalizedHarnessGraph,
        commit: HarnessGraphObservationCommit,
    ) -> HarnessGraphState:
        state = recovery.state
        if state is None:
            raise EventIncompleteHistoryError(
                "graph observation cannot project without an initialized state"
            )
        if recovery.expected_last_sequence != commit.sequence:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="graph observation projection does not follow its causal commit",
            )
        applied = self._applier.apply_observation(
            state,
            graph,
            commit.observation,
            observation_sequence=commit.sequence,
            projection_sequence=commit.sequence + 1,
        )
        projection = HarnessGraphProjectionCommit(
            HarnessGraphCommitKind.OBSERVATION_PROJECTION,
            commit.observation.observation_checksum,
            state.projection_checksum,
            applied,
            commit.sequence + 1,
            commit.occurred_at,
        )
        committed = self._port.commit_graph_projection(
            projection,
            expected_last_sequence=commit.sequence,
        )
        if committed != projection:
            raise EventReplayMismatchError(
                sequence=committed.sequence,
                reason="graph transition port returned a conflicting observation projection",
            )
        self._sync_timer_registrations(committed.state)
        return committed.state

    def _dispatch_after_commit(self, activity: HarnessGraphActivity) -> None:
        if self._dispatcher is None:
            raise HarnessValidationError(
                "graph activity dispatch requires an injected dispatcher",
                code="graph_activity_dispatcher_missing",
            )
        # Persist physical ownership before entering the external call.  A
        # process crash while the worker is in flight must leave a durable
        # marker that recovery can reconcile or quarantine instead of treating
        # the activity as never dispatched.
        self._port.mark_activity_dispatched(activity.activity_id)
        self._dispatcher.dispatch(activity)

    def _dispatch_cancellation_after_commit(
        self,
        request: HarnessGraphActivityCancellationRequest,
    ) -> None:
        handler = (
            None
            if self._dispatcher is None
            else getattr(self._dispatcher, "request_cancellation", None)
        )
        if not callable(handler):
            raise HarnessValidationError(
                "active branch cancellation requires a cancellation-capable dispatcher",
                code="graph_activity_cancellation_dispatcher_missing",
                details={"activity_id": request.activity_id},
            )
        handler(request)

    def _cancellation_request_for_decision(
        self,
        state: HarnessGraphState,
        decision: HarnessGraphDecision,
    ) -> HarnessGraphActivityCancellationRequest | None:
        if (
            decision.decision_type is not HarnessGraphDecisionType.REQUEST_BRANCH_CANCEL
            or decision.node_instance_id is None
        ):
            return None
        active = next(
            (
                item
                for item in state.active_activities
                if item.node_instance_id == decision.node_instance_id
            ),
            None,
        )
        if active is None:
            return None
        activity = self._port.activity_for(active.activity_id)
        if activity is None:
            raise EventIncompleteHistoryError(
                "branch cancellation is missing its durable activity descriptor"
            )
        if (
            activity.node_instance_id != active.node_instance_id
            or activity.attempt != active.attempt
            or activity.idempotency_key != active.idempotency_key
            or activity.fencing_generation != active.fencing_generation
        ):
            raise EventStoreCorruptionError(
                "branch cancellation activity conflicts with active attempt state"
            )
        return HarnessGraphActivityCancellationRequest(
            run_id=state.run_id,
            activity_id=activity.activity_id,
            node_instance_id=activity.node_instance_id,
            attempt=activity.attempt,
            idempotency_key=activity.idempotency_key,
            fencing_generation=activity.fencing_generation,
            causal_decision_checksum=decision.decision_checksum,
            reason_code=decision.reason_code,
        )

    def _recover_cancellation_requests(
        self,
        recovery: HarnessGraphRecovery,
    ) -> None:
        state = recovery.state
        if state is None:
            return
        decisions = {
            item.decision.decision_checksum: item.decision
            for item in recovery.decision_commits
        }
        for node in state.node_instances:
            if node.status is not HarnessNodeInstanceStatus.CANCEL_REQUESTED:
                continue
            decision_ref = node.metadata.get("last_decision_checksum")
            decision = (
                None
                if not isinstance(decision_ref, str)
                else decisions.get(decision_ref)
            )
            if (
                decision is None
                or decision.decision_type
                is not HarnessGraphDecisionType.REQUEST_BRANCH_CANCEL
            ):
                raise EventIncompleteHistoryError(
                    "cancel-requested node lacks its durable causal decision"
                )
            request = self._cancellation_request_for_decision(state, decision)
            if request is None:
                raise EventIncompleteHistoryError(
                    "cancel-requested node lacks one active activity"
                )
            self._dispatch_cancellation_after_commit(request)

    def _sync_timer_registrations(self, state: HarnessGraphState) -> None:
        """Reconcile live timer registrations from the durable projection.

        The graph projection is authoritative.  Registering or cancelling a
        live timer is therefore a post-commit adapter action and may be safely
        retried during recovery when the adapter call fails.
        """

        if self._timer_wake_port is None:
            return
        registrations = tuple(
            sorted(
                (
                    item
                    for item in state.wait_registrations
                    if item.kind is WaitKind.TIMER or item.deadline_ref is not None
                ),
                key=lambda item: (
                    item.registered_sequence,
                    item.node_instance_id,
                    item.wait_id,
                ),
            )
        )
        for item in registrations:
            scope = HarnessWaitScope(
                wait_id=item.wait_id,
                run_id=state.run_id,
                node_instance_id=item.node_instance_id,
                tenant_scope_ref=item.tenant_scope_ref,
                identity_scope_ref=item.identity_scope_ref,
                signal_schema_ref=item.signal_schema_ref,
                correlation_ref=item.correlation_ref,
            )
            record = HarnessWaitRegistrationRecord(
                scope=scope,
                kind=item.kind,
                registered_sequence=item.registered_sequence,
                deadline_ref=item.deadline_ref,
            )
            if item.unresolved:
                self._timer_wake_port.register_timer(record)
            else:
                self._timer_wake_port.cancel_timer(record)

    @staticmethod
    def _validate_recovery_identity(
        recovery: HarnessGraphRecovery,
        graph: NormalizedHarnessGraph,
        *,
        expected_run_id: str,
        run_spec_checksum: str,
    ) -> None:
        if recovery.run_id != expected_run_id:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="recovered graph run id does not match the requested run",
            )
        if recovery.graph != graph:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="recovered graph does not match the pinned graph",
            )
        if recovery.run_spec_checksum != run_spec_checksum:
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="recovered graph run spec checksum does not match",
            )
        if recovery.state is None:
            raise EventIncompleteHistoryError("graph recovery is missing its state")
        if recovery.state.graph_ref != graph_reference(graph):
            raise EventReplayMismatchError(
                sequence=recovery.expected_last_sequence,
                reason="recovered graph state reference does not match",
            )


def _require_graph_only_runtime_graph(graph: NormalizedHarnessGraph) -> None:
    if not isinstance(graph, NormalizedHarnessGraph):
        raise TypeError("graph must be NormalizedHarnessGraph")
    if graph.schema_version != GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA:
        raise HarnessValidationError(
            "live Graph runtime accepts only the Graph-only normalized schema",
            code="legacy_graph_admission_rejected",
            details={"schema_version": graph.schema_version},
        )


def _validate_graph_decision(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    accepted_evidence_refs: tuple[str, ...],
    side_effect_outcome_ref: str | None,
) -> None:
    if not isinstance(state, HarnessGraphState):
        raise TypeError("state must be HarnessGraphState")
    if not isinstance(graph, NormalizedHarnessGraph):
        raise TypeError("graph must be NormalizedHarnessGraph")
    _require_graph_only_runtime_graph(graph)
    if not isinstance(decision, HarnessGraphDecision):
        raise TypeError("decision must be HarnessGraphDecision")
    if state.lifecycle in {RunLifecycle.COMPLETED, RunLifecycle.HALTED}:
        raise HarnessValidationError(
            "terminal graph state cannot accept another decision",
            code="graph_control_terminal_state",
        )
    mismatches: list[str] = []
    if decision.run_id != state.run_id:
        mismatches.append("run_id")
    if decision.graph_ref != state.graph_ref or decision.graph_ref != graph_reference(
        graph
    ):
        mismatches.append("graph_ref")
    if decision.input_projection_checksum != state.projection_checksum:
        mismatches.append("input_projection_checksum")
    definitions = {item.node_id: item for item in graph.nodes}
    instances = {item.instance_id: item for item in state.node_instances}
    definition = None if decision.node_id is None else definitions.get(decision.node_id)
    if decision.node_id is not None and definition is None:
        mismatches.append("node_id")
    instance = (
        None
        if decision.node_instance_id is None
        else instances.get(decision.node_instance_id)
    )
    if decision.node_instance_id is not None and (
        instance is None
        or decision.node_id is None
        or instance.identity.node_id != decision.node_id
    ):
        mismatches.append("node_instance_id")
    if set(decision.target_node_ids).difference(definitions):
        mismatches.append("target_node_ids")
    if (
        isinstance(definition, HarnessExecutableNode)
        and decision.decision_type is not HarnessGraphDecisionType.SCHEDULE_COMPENSATION
    ):
        expected_bindings = compensation_binding_versions(
            definition,
            state=state,
            node=instance,
        )
        if dict(decision.binding_versions) != expected_bindings:
            mismatches.append("binding_versions")
        if decision.step_ref is not None and decision.step_ref != definition.step_ref:
            mismatches.append("step_ref")
    elif (
        isinstance(definition, HarnessControlNode)
        and definition.merge is not None
        and decision.decision_type is HarnessGraphDecisionType.APPLY_MERGE
    ):
        expected_bindings = (
            {}
            if definition.merge.merge_ref is None
            else {"merge": definition.merge.merge_ref.exact_ref}
        )
        if dict(decision.binding_versions) != expected_bindings:
            mismatches.append("binding_versions")
    elif (
        decision.decision_type is HarnessGraphDecisionType.PREPARE_SIDE_EFFECT
        and definition is None
    ):
        expected_bindings = (
            {}
            if graph.terminal_policy_ref is None
            else {"terminal_policy": graph.terminal_policy_ref.exact_ref}
        )
        if dict(decision.binding_versions) != expected_bindings:
            mismatches.append("binding_versions")
    elif (
        isinstance(definition, HarnessControlNode)
        and definition.wait is not None
        and decision.decision_type
        in {
            HarnessGraphDecisionType.REGISTER_WAIT,
            HarnessGraphDecisionType.RESUME_WAIT,
        }
    ):
        expected_bindings = {
            "wait": f"{definition.wait.signal_type}@{definition.wait.signal_version}"
        }
        if dict(decision.binding_versions) != expected_bindings:
            mismatches.append("binding_versions")
    if mismatches:
        raise HarnessValidationError(
            "graph decision does not match the current pinned projection",
            code="graph_control_decision_mismatch",
            details={"mismatches": sorted(set(mismatches))},
        )
    accepted_evidence = _checksum_tuple(
        accepted_evidence_refs,
        "accepted_evidence_refs",
    )
    _validate_side_effect_outcome_reference_contract(
        graph,
        decision,
        definition=definition,
        instance=instance,
        accepted_evidence_refs=accepted_evidence,
        side_effect_outcome_ref=side_effect_outcome_ref,
    )
    allowed_evidence = _state_evidence_refs(state).union(accepted_evidence)
    unaccepted = tuple(
        ref for ref in decision.evidence_refs if ref not in allowed_evidence
    )
    if unaccepted:
        raise HarnessValidationError(
            "graph decision references evidence outside accepted Control Plane inputs",
            code="graph_control_decision_evidence_mismatch",
            details={"unaccepted_evidence_refs": list(unaccepted)},
        )


def _validate_side_effect_outcome_reference_contract(
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    definition: HarnessGraphNode | None,
    instance: HarnessNodeInstanceState | None,
    accepted_evidence_refs: tuple[str, ...],
    side_effect_outcome_ref: str | None,
) -> None:
    node_effect_expected = (
        decision.decision_type is HarnessGraphDecisionType.COMPLETE_NODE
        and isinstance(definition, HarnessExecutableNode)
        and definition.side_effect_ref is not None
        and (
            instance is None
            or instance.status is not HarnessNodeInstanceStatus.COMPENSATING
        )
    )
    terminal_effect_expected = (
        decision.decision_type is HarnessGraphDecisionType.COMPLETE_RUN
        and decision.payload.get("outcome") == RunOutcome.SUCCEEDED.value
        and graph.terminal_policy is not None
    )
    outcome_expected = node_effect_expected or terminal_effect_expected
    if outcome_expected and side_effect_outcome_ref is None:
        raise HarnessValidationError(
            "effectful completion requires a durable outcome reference",
            code="graph_side_effect_outcome_missing",
        )
    if not outcome_expected and side_effect_outcome_ref is not None:
        raise HarnessValidationError(
            "side-effect outcome does not match a pinned completion effect",
            code="graph_decision_side_effect_outcome_mismatch",
        )
    if side_effect_outcome_ref is None:
        return
    if node_effect_expected and (
        decision.reason_code != "verification_passed"
        or decision.payload.get("step_transition_type") != "complete_step"
    ):
        raise HarnessValidationError(
            "effectful node completion requires a successful VERIFY decision",
            code="graph_side_effect_verify_evidence_mismatch",
        )
    if not _is_checksum_ref(side_effect_outcome_ref):
        raise HarnessValidationError(
            "side-effect outcome reference must be a checksum",
            code="graph_decision_side_effect_outcome_mismatch",
        )
    if side_effect_outcome_ref not in accepted_evidence_refs:
        raise HarnessValidationError(
            "side-effect outcome must be accepted before completion commits",
            code="graph_decision_side_effect_evidence_missing",
        )


def _validate_durable_side_effect_outcome(
    store: HarnessSideEffectStorePort | None,
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    outcome_ref: str,
) -> None:
    if store is None:
        raise HarnessValidationError(
            "effectful graph completion requires a durable side-effect store",
            code="graph_side_effect_store_missing",
        )
    decisions = store.list_decisions(run_id=state.run_id)
    if not all(isinstance(item, HarnessSideEffectDecision) for item in decisions):
        raise EventStoreCorruptionError(
            "durable side-effect store returned an invalid authorization"
        )

    expected_origin: HarnessSideEffectOrigin
    expected_handler_ref: str
    if decision.decision_type is HarnessGraphDecisionType.COMPLETE_NODE:
        if decision.node_id is None or decision.node_instance_id is None:
            raise HarnessValidationError(
                "effectful node completion requires exact node identity",
                code="graph_step_decision_identity_missing",
            )
        definition = _definition(graph, decision.node_id)
        if (
            not isinstance(definition, HarnessExecutableNode)
            or definition.side_effect_ref is None
        ):
            raise HarnessValidationError(
                "side-effect outcome targets a node without a pinned effect",
                code="graph_decision_side_effect_outcome_mismatch",
            )
        instance = _node_instance(state, decision.node_instance_id)
        expected_origin = HarnessSideEffectOrigin.WORKER
        expected_handler_ref = definition.side_effect_ref.exact_ref
        raw_pending = instance.metadata.get("pending_side_effect")
        if not isinstance(raw_pending, Mapping):
            raise HarnessValidationError(
                "effectful graph completion has no durable preparation",
                code="graph_side_effect_preparation_missing",
            )
        pending = HarnessPendingSideEffectState.from_dict(raw_pending)
        candidates = tuple(
            item
            for item in decisions
            if item.origin is expected_origin
            and item.node_id == decision.node_id
            and item.node_instance_id == decision.node_instance_id
            and item.attempt == decision.attempt
            and item.causation_id == pending.prepare_decision_ref
        )
    elif decision.decision_type is HarnessGraphDecisionType.COMPLETE_RUN:
        policy = graph.terminal_policy
        if policy is None:
            raise HarnessValidationError(
                "side-effect outcome targets a run without a pinned terminal policy",
                code="graph_decision_side_effect_outcome_mismatch",
            )
        expected_origin = HarnessSideEffectOrigin.CONTROLLER_TERMINAL
        expected_handler_ref = str(policy.handler)
        raw_pending = state.metadata.get("pending_terminal_side_effect")
        if not isinstance(raw_pending, Mapping):
            raise HarnessValidationError(
                "terminal graph completion has no durable preparation",
                code="graph_side_effect_preparation_missing",
            )
        pending = HarnessPendingSideEffectState.from_dict(raw_pending)
        candidates = tuple(
            item
            for item in decisions
            if item.origin is expected_origin
            and item.terminal_action == "complete_run"
            and item.causation_id == pending.prepare_decision_ref
        )
    else:  # pragma: no cover - pure contract validation rejects this first
        raise HarnessValidationError(
            "side-effect outcome is not bound to a completion decision",
            code="graph_decision_side_effect_outcome_mismatch",
        )

    if not candidates:
        raise HarnessValidationError(
            "graph side-effect outcome has no durable authorization",
            code="graph_side_effect_authorization_missing",
        )
    if len(candidates) != 1:
        raise EventStoreCorruptionError(
            "multiple durable side-effect authorizations match one graph completion"
        )
    authorization = candidates[0]
    if (
        pending.status is not HarnessPendingSideEffectStatus.OUTCOME_RECORDED
        or pending.authorization_ref != authorization.checksum
        or pending.outcome_ref != outcome_ref
    ):
        raise HarnessValidationError(
            "graph completion conflicts with its recorded side-effect observation",
            code="graph_side_effect_preparation_mismatch",
        )
    expected_identity_scope = state.metadata.get("identity_scope_ref")
    expected_subject_scope = state.metadata.get("subject_scope_ref")
    if not _is_checksum_ref(expected_identity_scope) or not _is_checksum_ref(
        expected_subject_scope
    ):
        raise HarnessValidationError(
            "effectful graph completion is missing authoritative scope references",
            code="graph_side_effect_scope_missing",
        )
    authorization_ref = authorization.checksum
    authorization_mismatch = (
        authorization.status is not HarnessSideEffectDecisionStatus.AUTHORIZED
        or not _is_checksum_ref(authorization_ref)
        or authorization.run_id != state.run_id
        or authorization.graph_id != state.graph_ref.graph_id
        or authorization.graph_version != state.graph_ref.identity_version
        or authorization.graph_ref != state.graph_ref.identity_ref.exact_ref
        or authorization.graph_checksum != state.graph_ref.checksum
        or str(authorization.handler) != expected_handler_ref
        or authorization.identity_scope_ref != expected_identity_scope
        or authorization.subject_scope_ref != expected_subject_scope
    )
    if authorization_mismatch:
        raise EventStoreCorruptionError(
            "durable side-effect authorization conflicts with graph completion"
        )
    if decision.decision_type is HarnessGraphDecisionType.COMPLETE_NODE:
        node = _node_instance(state, decision.node_instance_id or "")
        gate_evidence = {
            (item.contract_ref.exact_ref, item.evidence_ref): item.evidence_ref
            for item in node.evidence_refs
            if item.kind is HarnessEvidenceKind.GATE_RESULT
            and item.attempt == node.attempt
            and item.contract_ref is not None
        }
        if (
            not authorization.gate_refs
            or not _is_checksum_ref(authorization.aggregate_verdict_ref)
            or len(authorization.gate_refs) != len(authorization.gate_result_refs)
            or any(
                gate_evidence.get(pair) not in decision.evidence_refs
                for pair in zip(
                    authorization.gate_refs,
                    authorization.gate_result_refs,
                    strict=True,
                )
            )
        ):
            raise EventStoreCorruptionError(
                "durable side-effect authorization lacks accepted VERIFY evidence"
            )

    try:
        outcome = store.get_outcome(
            effect_id=authorization.effect_id,
            identity_scope_ref=authorization.identity_scope_ref,
            subject_scope_ref=authorization.subject_scope_ref,
            idempotency_key=authorization.idempotency_key,
        )
    except HarnessValidationError as exc:
        raise EventStoreCorruptionError(
            "durable side-effect outcome lookup conflicts with its authorization"
        ) from exc
    if outcome is None:
        raise HarnessValidationError(
            "graph side-effect outcome is not durably readable",
            code="graph_side_effect_outcome_missing",
        )
    if not isinstance(outcome, HarnessSideEffectOutcome):
        raise EventStoreCorruptionError(
            "durable side-effect store returned an invalid outcome"
        )
    durable_pair_mismatch = (
        outcome.status is not HarnessSideEffectOutcomeStatus.COMMITTED
        or outcome.effect_id != authorization.effect_id
        or outcome.decision_ref != authorization_ref
        or outcome.run_id != authorization.run_id
        or outcome.graph_id != authorization.graph_id
        or outcome.graph_version != authorization.graph_version
        or outcome.graph_ref != authorization.graph_ref
        or outcome.graph_checksum != authorization.graph_checksum
        or outcome.origin is not authorization.origin
        or outcome.kind != authorization.kind
        or outcome.handler != authorization.handler
        or outcome.idempotency_key != authorization.idempotency_key
        or outcome.identity_scope_ref != authorization.identity_scope_ref
        or outcome.subject_scope_ref != authorization.subject_scope_ref
        or outcome.atomic_group != authorization.atomic_group
        or outcome.node_id != authorization.node_id
        or outcome.node_instance_id != authorization.node_instance_id
        or outcome.activity_id != authorization.activity_id
        or outcome.attempt != authorization.attempt
        or outcome.terminal_action != authorization.terminal_action
        or outcome.disposition is not authorization.disposition
        or not _is_checksum_ref(outcome.checksum)
    )
    if durable_pair_mismatch:
        raise EventStoreCorruptionError(
            "durable side-effect outcome conflicts with its authorization"
        )
    if outcome.checksum != outcome_ref:
        raise HarnessValidationError(
            "graph completion supplied a conflicting side-effect outcome reference",
            code="graph_side_effect_outcome_mismatch",
        )


def _expected_side_effect_activity_id(
    state: HarnessGraphState,
    pending: HarnessPendingSideEffectState,
) -> str:
    """Recover the exact physical activity pinned on the node projection."""

    node = _node_instance(state, pending.node_instance_id)
    activity_id = node.metadata.get("activity_id")
    activity_attempt = node.metadata.get("activity_attempt")
    if (
        not isinstance(activity_id, str)
        or not activity_id.strip()
        or activity_attempt != pending.attempt
    ):
        raise EventStoreCorruptionError(
            "graph side-effect preparation lacks its physical activity identity"
        )
    return activity_id.strip()


def _validate_durable_side_effect_observation(
    store: HarnessSideEffectStorePort | None,
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    observation: HarnessAcceptedGraphObservation,
) -> None:
    if store is None:
        raise HarnessValidationError(
            "side-effect outcome observation requires a durable store",
            code="graph_side_effect_store_missing",
        )
    scope = HarnessPendingSideEffectScope(observation.payload["scope"])
    raw_pending = (
        _node_instance(state, observation.node_instance_id).metadata.get(
            "pending_side_effect"
        )
        if scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else state.metadata.get("pending_terminal_side_effect")
    )
    if not isinstance(raw_pending, Mapping):
        raise HarnessValidationError(
            "side-effect outcome has no durable graph preparation",
            code="graph_side_effect_preparation_missing",
        )
    pending = HarnessPendingSideEffectState.from_dict(raw_pending)
    if (
        pending.scope is not scope
        or pending.status is not HarnessPendingSideEffectStatus.PREPARED
        or pending.prepare_decision_ref
        != observation.payload["prepare_decision_ref"]
        or pending.node_id != observation.node_id
        or pending.node_instance_id != observation.node_instance_id
        or pending.attempt != observation.attempt
        or pending.handler_ref != observation.contract_ref
    ):
        raise HarnessValidationError(
            "side-effect outcome conflicts with its durable graph preparation",
            code="graph_side_effect_preparation_mismatch",
        )
    decision_ref = observation.payload["decision_ref"]
    authorization = store.get_decision(decision_ref)
    if authorization is None:
        raise HarnessValidationError(
            "side-effect outcome has no durable authorization",
            code="graph_side_effect_authorization_missing",
        )
    if not isinstance(authorization, HarnessSideEffectDecision):
        raise EventStoreCorruptionError(
            "durable side-effect store returned an invalid authorization"
        )
    expected_origin = (
        HarnessSideEffectOrigin.WORKER
        if scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else HarnessSideEffectOrigin.CONTROLLER_TERMINAL
    )
    definition = _definition(graph, pending.node_id)
    expected_activity_id = (
        _expected_side_effect_activity_id(state, pending)
        if scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else None
    )
    expected_node_id = (
        pending.node_id
        if scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else None
    )
    expected_node_instance_id = (
        pending.node_instance_id
        if scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else None
    )
    authorization_mismatch = (
        authorization.checksum != decision_ref
        or authorization.status is not HarnessSideEffectDecisionStatus.AUTHORIZED
        or authorization.causation_id != pending.prepare_decision_ref
        or authorization.command_ordinal != pending.prepare_sequence
        or authorization.run_id != state.run_id
        or authorization.graph_id != state.graph_ref.graph_id
        or authorization.graph_version != state.graph_ref.identity_version
        or authorization.graph_ref != state.graph_ref.identity_ref.exact_ref
        or authorization.graph_checksum != state.graph_ref.checksum
        or authorization.origin is not expected_origin
        or str(authorization.handler) != pending.handler_ref.exact_ref
        or authorization.identity_scope_ref
        != state.metadata.get("identity_scope_ref")
        or authorization.subject_scope_ref
        != state.metadata.get("subject_scope_ref")
        or authorization.node_id != expected_node_id
        or authorization.node_instance_id != expected_node_instance_id
        or authorization.activity_id != expected_activity_id
        or (
            scope is HarnessPendingSideEffectScope.NODE_INSTANCE
            and authorization.attempt != pending.attempt
        )
        or (
            scope is HarnessPendingSideEffectScope.TERMINAL_RUN
            and authorization.terminal_action != "complete_run"
        )
    )
    if authorization_mismatch:
        raise EventStoreCorruptionError(
            "durable side-effect authorization conflicts with its graph preparation"
        )
    outcome = store.get_outcome(
        effect_id=authorization.effect_id,
        identity_scope_ref=authorization.identity_scope_ref,
        subject_scope_ref=authorization.subject_scope_ref,
        idempotency_key=authorization.idempotency_key,
    )
    if outcome is None:
        raise HarnessValidationError(
            "side-effect outcome observation is not durably readable",
            code="graph_side_effect_outcome_missing",
        )
    if not isinstance(outcome, HarnessSideEffectOutcome):
        raise EventStoreCorruptionError(
            "durable side-effect store returned an invalid outcome"
        )
    outcome_mismatch = (
        outcome.status is not HarnessSideEffectOutcomeStatus.COMMITTED
        or outcome.checksum != observation.evidence_ref
        or outcome.checksum != observation.payload["outcome_ref"]
        or outcome.decision_ref != decision_ref
        or outcome.effect_id != authorization.effect_id
        or outcome.run_id != authorization.run_id
        or outcome.graph_id != authorization.graph_id
        or outcome.graph_version != authorization.graph_version
        or outcome.graph_ref != authorization.graph_ref
        or outcome.graph_checksum != authorization.graph_checksum
        or outcome.origin is not authorization.origin
        or outcome.kind != authorization.kind
        or outcome.handler != authorization.handler
        or outcome.idempotency_key != authorization.idempotency_key
        or outcome.identity_scope_ref != authorization.identity_scope_ref
        or outcome.subject_scope_ref != authorization.subject_scope_ref
        or outcome.atomic_group != authorization.atomic_group
        or outcome.node_id != authorization.node_id
        or outcome.node_instance_id != authorization.node_instance_id
        or outcome.activity_id != authorization.activity_id
        or outcome.attempt != authorization.attempt
        or outcome.terminal_action != authorization.terminal_action
        or outcome.disposition is not authorization.disposition
        or outcome.disposition.value != observation.payload["disposition"]
        or checksum_for(outcome.effect_id) != observation.payload["effect_ref"]
    )
    if outcome_mismatch:
        raise EventStoreCorruptionError(
            "durable side-effect outcome conflicts with its graph observation"
        )


def _validate_durable_side_effect_failure_observation(
    store: HarnessSideEffectStorePort | None,
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    observation: HarnessAcceptedGraphObservation,
) -> None:
    if store is None:
        raise HarnessValidationError(
            "side-effect failure observation requires a durable store",
            code="graph_side_effect_store_missing",
        )
    prepare_ref = observation.payload["causal_graph_decision_checksum"]
    candidates: list[HarnessPendingSideEffectState] = []
    node = _node_instance(state, observation.node_instance_id)
    raw_node_pending = node.metadata.get("pending_side_effect")
    if isinstance(raw_node_pending, Mapping):
        pending = HarnessPendingSideEffectState.from_dict(raw_node_pending)
        if pending.prepare_decision_ref == prepare_ref:
            candidates.append(pending)
    raw_terminal_pending = state.metadata.get("pending_terminal_side_effect")
    if isinstance(raw_terminal_pending, Mapping):
        pending = HarnessPendingSideEffectState.from_dict(raw_terminal_pending)
        if pending.prepare_decision_ref == prepare_ref:
            candidates.append(pending)
    if len(candidates) != 1:
        raise HarnessValidationError(
            "side-effect failure has no unique durable graph preparation",
            code="graph_side_effect_preparation_missing",
        )
    pending = candidates[0]
    if (
        pending.status is not HarnessPendingSideEffectStatus.PREPARED
        or pending.node_id != observation.node_id
        or pending.node_instance_id != observation.node_instance_id
        or pending.attempt != observation.attempt
        or pending.handler_ref != observation.contract_ref
    ):
        raise HarnessValidationError(
            "side-effect failure conflicts with its durable graph preparation",
            code="graph_side_effect_preparation_mismatch",
        )
    decision_ref = observation.payload["decision_ref"]
    authorization = store.get_decision(decision_ref)
    if authorization is None:
        raise HarnessValidationError(
            "side-effect failure has no durable authorization",
            code="graph_side_effect_authorization_missing",
        )
    if not isinstance(authorization, HarnessSideEffectDecision):
        raise EventStoreCorruptionError(
            "durable side-effect store returned an invalid authorization"
        )
    definition = _definition(graph, pending.node_id)
    expected_origin = (
        HarnessSideEffectOrigin.WORKER
        if pending.scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else HarnessSideEffectOrigin.CONTROLLER_TERMINAL
    )
    expected_activity_id = (
        _expected_side_effect_activity_id(state, pending)
        if pending.scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else None
    )
    expected_node_id = (
        pending.node_id
        if pending.scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else None
    )
    expected_node_instance_id = (
        pending.node_instance_id
        if pending.scope is HarnessPendingSideEffectScope.NODE_INSTANCE
        else None
    )
    if (
        authorization.checksum != decision_ref
        or authorization.status is not HarnessSideEffectDecisionStatus.AUTHORIZED
        or authorization.causation_id != pending.prepare_decision_ref
        or authorization.command_ordinal != pending.prepare_sequence
        or authorization.run_id != state.run_id
        or authorization.graph_id != state.graph_ref.graph_id
        or authorization.graph_version != state.graph_ref.identity_version
        or authorization.graph_ref != state.graph_ref.identity_ref.exact_ref
        or authorization.graph_checksum != state.graph_ref.checksum
        or authorization.origin is not expected_origin
        or str(authorization.handler) != pending.handler_ref.exact_ref
        or authorization.identity_scope_ref
        != state.metadata.get("identity_scope_ref")
        or authorization.subject_scope_ref
        != state.metadata.get("subject_scope_ref")
        or authorization.node_id != expected_node_id
        or authorization.node_instance_id != expected_node_instance_id
        or authorization.activity_id != expected_activity_id
        or (
            pending.scope is HarnessPendingSideEffectScope.NODE_INSTANCE
            and authorization.attempt != pending.attempt
        )
        or (
            pending.scope is HarnessPendingSideEffectScope.TERMINAL_RUN
            and authorization.terminal_action != "complete_run"
        )
    ):
        raise EventStoreCorruptionError(
            "durable side-effect authorization conflicts with its failure observation"
        )
    expected_failure_ref = checksum_for(
        {
            "code": observation.payload["reason_code"],
            "effect_ref": checksum_for(authorization.effect_id),
            "decision_ref": authorization.checksum,
        }
    )
    if (
        observation.payload["failure_ref"] != expected_failure_ref
        or observation.evidence_ref != expected_failure_ref
    ):
        raise HarnessValidationError(
            "side-effect failure evidence does not match its authorization",
            code="graph_side_effect_failure_evidence_mismatch",
        )
    outcome = store.get_outcome(
        effect_id=authorization.effect_id,
        identity_scope_ref=authorization.identity_scope_ref,
        subject_scope_ref=authorization.subject_scope_ref,
        idempotency_key=authorization.idempotency_key,
    )
    if outcome is not None:
        raise EventStoreCorruptionError(
            "side-effect failure conflicts with a committed durable outcome"
        )


def _activate_node(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_id is None:
        raise HarnessValidationError(
            "node activation requires a node definition",
            code="graph_activation_identity_missing",
        )
    definition = _definition(graph, decision.node_id)
    max_active_nodes = state.budgets.require("max_active_nodes").limit
    active_node_count = sum(not item.is_terminal for item in state.node_instances)
    if active_node_count >= max_active_nodes:
        raise HarnessValidationError(
            "graph node activation exceeds active-node capacity",
            code="graph_active_node_limit_exceeded",
            details={"active": active_node_count, "limit": max_active_nodes},
        )
    branch_path = tuple(decision.payload.get("branch_path", ()))
    raw_vector = decision.payload.get("iteration_vector", ())
    if not isinstance(raw_vector, tuple | list):
        raise HarnessValidationError(
            "activation iteration_vector must be an array",
            code="invalid_graph_activation_scope",
        )
    iteration_vector = tuple(
        HarnessLoopIteration.from_dict(item) for item in raw_vector
    )
    prior_scope_instances = tuple(
        item.identity.node_id == definition.node_id
        and item.identity.branch_path == branch_path
        and item.identity.iteration_vector == iteration_vector
        for item in state.node_instances
    )
    duplicate_scope = any(prior_scope_instances)
    repeated_loop_guard = (
        definition.node_kind is HarnessGraphNodeKind.LOOP_GUARD
        and decision.reason_code == "loop_iteration_completed"
        and all(
            item.is_terminal
            for item in state.node_instances
            if item.identity.node_id == definition.node_id
            and item.identity.branch_path == branch_path
            and item.identity.iteration_vector == iteration_vector
        )
    )
    if duplicate_scope and not repeated_loop_guard:
        raise HarnessValidationError(
            "graph node scope is already activated",
            code="duplicate_graph_node_activation",
        )
    ordinal = (
        max(
            (item.identity.activation_ordinal for item in state.node_instances),
            default=0,
        )
        + 1
    )
    identity = HarnessNodeInstanceIdentity(
        state.run_id,
        graph.checksum,
        definition.node_id,
        branch_path=branch_path,
        iteration_vector=iteration_vector,
        activation_ordinal=ordinal,
    )
    common = {
        "identity": identity,
        "node_kind": definition.node_kind,
        "status": HarnessNodeInstanceStatus.READY,
        "activation_sequence": projection_sequence,
        "last_event_sequence": projection_sequence,
        "metadata": _decision_metadata(decision),
    }
    if isinstance(definition, HarnessExecutableNode):
        node = HarnessNodeInstanceState(
            **common,
            step_id=definition.step_id,
            step_ref=definition.step_ref,
            step_status=HarnessStepStatus.PENDING,
        )
    else:
        node = HarnessNodeInstanceState(**common)
    lifecycle = (
        RunLifecycle.RUNNING
        if state.lifecycle is RunLifecycle.CREATED
        else state.lifecycle
    )
    return replace(
        state,
        lifecycle=lifecycle,
        node_instances=(*state.node_instances, node),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _schedule_compensation(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if state.metadata.get("execution_mode") != "compensating":
        raise HarnessValidationError(
            "compensation scheduling requires dedicated compensation mode",
            code="graph_compensation_mode_required",
        )
    if decision.node_id is None or decision.node_instance_id is not None:
        raise HarnessValidationError(
            "compensation scheduling requires one target definition",
            code="graph_compensation_identity_mismatch",
        )
    definition = _definition(graph, decision.node_id)
    if not isinstance(definition, HarnessExecutableNode):
        raise HarnessValidationError(
            "compensation scheduling requires an executable definition",
            code="graph_compensation_node_kind_mismatch",
        )
    if state.active_activities:
        raise HarnessValidationError(
            "compensation cannot start before forward activity termination",
            code="graph_activity_termination_unconfirmed",
        )
    if any(
        item.status is HarnessCompensationStatus.RUNNING
        for item in state.compensation_stack
    ):
        raise HarnessValidationError(
            "only one compensation entry may run at a time",
            code="graph_compensation_already_running",
        )
    pending = tuple(
        item
        for item in state.compensation_stack
        if item.status is HarnessCompensationStatus.PENDING
    )
    if not pending:
        raise HarnessValidationError(
            "compensation scheduling requires a pending entry",
            code="graph_compensation_entry_missing",
        )
    selected = max(
        pending,
        key=lambda item: (item.effect_commit_sequence, item.entry_id),
    )
    if decision.payload.get("entry_id") != selected.entry_id:
        raise HarnessValidationError(
            "compensation entry is not next in durable effect order",
            code="graph_compensation_order_mismatch",
        )
    origin = _node_instance(state, selected.origin_node_instance_id)
    binding = next(
        (
            item
            for item in graph.compensation_refs
            if item.for_node_id == origin.identity.node_id
            and item.compensation_node_id == definition.node_id
        ),
        None,
    )
    if binding is None:
        raise HarnessValidationError(
            "compensation entry has no exact pinned binding",
            code="graph_compensation_binding_mismatch",
        )
    handler_payload = decision.payload.get("handler_ref")
    activity_payload = decision.payload.get("activity_ref")
    if not isinstance(handler_payload, Mapping) or not isinstance(
        activity_payload,
        Mapping,
    ):
        raise HarnessValidationError(
            "compensation decision is missing exact runtime references",
            code="graph_compensation_binding_mismatch",
        )
    handler_ref = HarnessContractReference.from_dict(handler_payload)
    activity_ref = HarnessContractReference.from_dict(activity_payload)
    expected_bindings = compensation_binding_versions(definition)
    expected_bindings.update(
        {
            "compensation": binding.handler_ref.exact_ref,
            "activity": binding.activity_ref.exact_ref,
        }
    )
    if (
        selected.handler_ref != binding.handler_ref
        or selected.activity_ref != binding.activity_ref
        or handler_ref != binding.handler_ref
        or activity_ref != binding.activity_ref
        or dict(decision.binding_versions) != expected_bindings
        or decision.target_node_ids != (definition.node_id,)
        or decision.payload.get("origin_node_instance_id") != origin.instance_id
        or decision.payload.get("idempotency_key") != selected.idempotency_key
        or decision.payload.get("fencing_generation")
        != selected.fencing_generation
        or selected.effect_outcome_ref not in decision.evidence_refs
    ):
        raise HarnessValidationError(
            "compensation decision conflicts with its durable entry",
            code="graph_compensation_binding_mismatch",
        )
    max_active_nodes = state.budgets.require("max_active_nodes").limit
    active_node_count = sum(not item.is_terminal for item in state.node_instances)
    if active_node_count >= max_active_nodes:
        raise HarnessValidationError(
            "compensation activation exceeds active-node capacity",
            code="graph_active_node_limit_exceeded",
            details={"active": active_node_count, "limit": max_active_nodes},
        )
    ordinal = (
        max(
            (item.identity.activation_ordinal for item in state.node_instances),
            default=0,
        )
        + 1
    )
    identity = HarnessNodeInstanceIdentity(
        state.run_id,
        graph.checksum,
        definition.node_id,
        branch_path=origin.identity.branch_path,
        iteration_vector=origin.identity.iteration_vector,
        activation_ordinal=ordinal,
    )
    metadata = _decision_metadata(decision)
    metadata.update(
        {
            "compensation_entry_id": selected.entry_id,
            "origin_node_instance_id": origin.instance_id,
            "effect_outcome_ref": selected.effect_outcome_ref,
            "compensation_handler_ref": binding.handler_ref.exact_ref,
            "compensation_activity_ref": binding.activity_ref.exact_ref,
            "compensation_idempotency_key": selected.idempotency_key,
            "compensation_fencing_generation": selected.fencing_generation,
        }
    )
    node = HarnessNodeInstanceState(
        identity=identity,
        node_kind=definition.node_kind,
        status=HarnessNodeInstanceStatus.COMPENSATING,
        activation_sequence=projection_sequence,
        last_event_sequence=projection_sequence,
        step_id=definition.step_id,
        step_ref=definition.step_ref,
        step_status=HarnessStepStatus.PENDING,
        metadata=metadata,
    )
    running_entry = replace(
        selected,
        status=HarnessCompensationStatus.RUNNING,
        compensation_node_instance_id=node.instance_id,
        last_event_sequence=projection_sequence,
    )
    return replace(
        state,
        lifecycle=RunLifecycle.RUNNING,
        node_instances=(*state.node_instances, node),
        compensation_stack=tuple(
            running_entry if item.entry_id == selected.entry_id else item
            for item in state.compensation_stack
        ),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_prepare_side_effect(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    decision_sequence: int,
    projection_sequence: int,
) -> HarnessGraphState:
    scope = decision.payload.get("side_effect_scope")
    if scope == HarnessPendingSideEffectScope.NODE_INSTANCE.value:
        if decision.node_id is None or decision.node_instance_id is None:
            raise HarnessValidationError(
                "node side-effect preparation requires exact node identity",
                code="graph_step_decision_identity_missing",
            )
        definition = _definition(graph, decision.node_id)
        if (
            not isinstance(definition, HarnessExecutableNode)
            or definition.side_effect_ref is None
        ):
            raise HarnessValidationError(
                "side-effect preparation targets a node without a pinned handler",
                code="graph_decision_side_effect_outcome_mismatch",
            )
        node = _node_instance(state, decision.node_instance_id)
        if node.status is HarnessNodeInstanceStatus.COMPENSATING:
            raise HarnessValidationError(
                "compensation completion cannot invoke the forward side effect",
                code="graph_side_effect_scope_mismatch",
            )
        _validate_step_decision_source(
            node,
            HarnessGraphDecisionType.PREPARE_SIDE_EFFECT,
        )
        if decision.attempt != node.attempt:
            raise HarnessValidationError(
                "side-effect preparation belongs to another node attempt",
                code="graph_step_decision_attempt_mismatch",
            )
        if decision.payload.get("step_transition_type") != "complete_step":
            raise HarnessValidationError(
                "side-effect preparation requires successful Step completion evidence",
                code="graph_side_effect_verify_evidence_mismatch",
            )
        if node.metadata.get("pending_side_effect") is not None:
            raise HarnessValidationError(
                "node already has a durable side-effect preparation",
                code="duplicate_graph_side_effect_preparation",
            )
        pending = HarnessPendingSideEffectState(
            scope=HarnessPendingSideEffectScope.NODE_INSTANCE,
            prepare_decision_ref=decision.decision_checksum,
            prepare_sequence=decision_sequence,
            handler_ref=definition.side_effect_ref,
            node_id=definition.node_id,
            node_instance_id=node.instance_id,
            attempt=node.attempt,
        )
        metadata = _merged_node_metadata(node, decision)
        metadata["pending_side_effect"] = pending.to_dict()
        updated = replace(
            node,
            last_event_sequence=projection_sequence,
            metadata=metadata,
        )
        return replace(
            state,
            node_instances=_replace_node(state.node_instances, updated),
            last_event_sequence=projection_sequence,
            projection_checksum=None,
        )

    if scope != HarnessPendingSideEffectScope.TERMINAL_RUN.value:
        raise HarnessValidationError(
            "side-effect preparation scope is unsupported",
            code="graph_side_effect_scope_mismatch",
        )
    if any(
        value is not None
        for value in (
            decision.node_id,
            decision.node_instance_id,
            decision.step_ref,
            decision.attempt,
        )
    ):
        raise HarnessValidationError(
            "terminal side-effect preparation must use run scope",
            code="graph_decision_identity_mismatch",
        )
    if (
        graph.terminal_policy is None
        or graph.terminal_policy_ref is None
        or decision.payload.get("outcome") != RunOutcome.SUCCEEDED.value
    ):
        raise HarnessValidationError(
            "terminal side-effect preparation requires one successful pinned policy",
            code="terminal_side_effect_policy_missing",
        )
    if state.metadata.get("pending_terminal_side_effect") is not None:
        raise HarnessValidationError(
            "run already has a durable terminal side-effect preparation",
            code="duplicate_graph_side_effect_preparation",
        )
    if state.active_activities or any(
        not item.is_terminal for item in state.node_instances
    ):
        raise HarnessValidationError(
            "terminal side-effect preparation requires terminal graph work",
            code="terminal_side_effect_steps_incomplete",
        )
    terminal_bindings = tuple(
        item for item in graph.compensation_refs if item.scope == "terminal_run"
    )
    anchor_node_id = (
        terminal_bindings[0].for_node_id if len(terminal_bindings) == 1 else None
    )
    anchor_candidates = tuple(
        item
        for item in state.node_instances
        if item.node_kind is HarnessGraphNodeKind.EXECUTABLE
        and item.status is HarnessNodeInstanceStatus.SUCCEEDED
        and (
            anchor_node_id is None
            or item.identity.node_id == anchor_node_id
        )
    )
    if not anchor_candidates:
        raise HarnessValidationError(
            "terminal side effect has no verified executable observation anchor",
            code="terminal_side_effect_anchor_missing",
        )
    anchor = max(
        anchor_candidates,
        key=lambda item: (
            item.last_event_sequence,
            item.identity.activation_ordinal,
            item.instance_id,
        ),
    )
    handler_ref = HarnessContractReference(
        HarnessContractKind.SIDE_EFFECT,
        graph.terminal_policy.handler.handler_id,
        graph.terminal_policy.handler.version,
    )
    pending = HarnessPendingSideEffectState(
        scope=HarnessPendingSideEffectScope.TERMINAL_RUN,
        prepare_decision_ref=decision.decision_checksum,
        prepare_sequence=decision_sequence,
        handler_ref=handler_ref,
        node_id=anchor.identity.node_id,
        node_instance_id=anchor.instance_id,
        attempt=anchor.attempt,
    )
    metadata = thaw_json(state.metadata)
    metadata["pending_terminal_side_effect"] = pending.to_dict()
    metadata["last_run_decision_checksum"] = decision.decision_checksum
    metadata["last_run_decision_type"] = decision.decision_type.value
    return replace(
        state,
        metadata=metadata,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_step_decision(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    decision_sequence: int,
    projection_sequence: int,
    activity_input_ref: str | None,
    side_effect_outcome_ref: str | None,
) -> tuple[HarnessGraphState, HarnessGraphActivity | None]:
    if decision.node_instance_id is None or decision.node_id is None:
        raise HarnessValidationError(
            "Step decision requires node identity",
            code="graph_step_decision_identity_missing",
        )
    node = _node_instance(state, decision.node_instance_id)
    definition = _definition(graph, decision.node_id)
    if not isinstance(definition, HarnessExecutableNode):
        raise HarnessValidationError(
            "Step decision requires an executable definition",
            code="graph_step_decision_node_kind_mismatch",
        )
    compensation_entry = (
        compensation_entry_for_node(state, node)
        if node.status is HarnessNodeInstanceStatus.COMPENSATING
        else None
    )
    decision_type = decision.decision_type
    _validate_step_decision_source(node, decision_type)
    expected_attempt = (
        node.attempt + 1
        if decision_type is HarnessGraphDecisionType.DISPATCH_ACTIVITY
        and node.step_status
        in {
            HarnessStepStatus.PLANNING,
            HarnessStepStatus.PLAN_VERIFIED,
            HarnessStepStatus.RETRYING,
        }
        else node.attempt
    )
    if decision.attempt != expected_attempt:
        raise HarnessValidationError(
            "graph Step decision does not match the current attempt",
            code="graph_step_decision_attempt_mismatch",
            details={
                "decision_type": decision_type.value,
                "expected_attempt": expected_attempt,
                "actual_attempt": decision.attempt,
            },
        )
    activity: HarnessGraphActivity | None = None
    metadata = _merged_node_metadata(node, decision)
    status = (
        HarnessNodeInstanceStatus.COMPENSATING
        if node.status is HarnessNodeInstanceStatus.COMPENSATING
        else HarnessNodeInstanceStatus.RUNNING
    )
    step_status = node.step_status
    attempt = node.attempt
    replans = node.replans
    error_code = node.error_code
    terminal_reason = node.terminal_reason
    output_refs = thaw_json(node.output_refs)
    evidence_refs = node.evidence_refs
    compensation_stack = state.compensation_stack
    active_activities = state.active_activities
    if decision_type is HarnessGraphDecisionType.ENTER_STEP_PHASE:
        step_status = HarnessStepStatus.PLANNING
    elif decision_type is HarnessGraphDecisionType.DISPATCH_ACTIVITY:
        if activity_input_ref is None:
            raise HarnessValidationError(
                "activity dispatch requires a durable input reference",
                code="graph_activity_input_ref_missing",
            )
        if decision.attempt is None:
            raise HarnessValidationError(
                "activity dispatch requires an attempt",
                code="graph_step_decision_identity_missing",
            )
        max_parallelism = state.budgets.require("max_parallelism")
        if len(state.active_activities) >= max_parallelism.limit:
            raise HarnessValidationError(
                "graph activity dispatch exceeds physical parallelism",
                code="graph_parallelism_exceeded",
                details={"limit": max_parallelism.limit},
            )
        if any(item.node_instance_id == node.instance_id for item in active_activities):
            raise HarnessValidationError(
                "node instance already has an active activity",
                code="duplicate_active_activity_attempt",
            )
        attempt = decision.attempt
        if attempt != node.attempt:
            metadata.pop("approval_granted", None)
            metadata.pop("approval_evidence_ref", None)
            metadata.pop("approval_reason_ref", None)
        fencing_generation = (
            int(metadata.get("fencing_generation", 0)) + 1
            if compensation_entry is None
            else compensation_entry.fencing_generation
            + (1 if node.attempt > 0 else 0)
        )
        metadata["fencing_generation"] = fencing_generation
        if compensation_entry is not None:
            metadata["compensation_fencing_generation"] = fencing_generation
            compensation_entry = replace(
                compensation_entry,
                fencing_generation=fencing_generation,
                last_event_sequence=projection_sequence,
            )
            compensation_stack = _replace_compensation_entry(
                compensation_stack,
                compensation_entry,
            )
        activity = HarnessGraphActivity(
            run_id=state.run_id,
            graph_ref=state.graph_ref,
            node_id=definition.node_id,
            node_instance_id=node.instance_id,
            step_ref=definition.step_ref,
            worker_ref=definition.worker_ref,
            activity_ref=(
                definition.activity_ref
                if compensation_entry is None
                else compensation_entry.activity_ref
            ),
            attempt=attempt,
            input_ref=activity_input_ref,
            causal_decision_checksum=decision.decision_checksum,
            causal_decision_sequence=decision_sequence,
            fencing_generation=fencing_generation,
            tenant_scope_ref=state.metadata.get("tenant_scope_ref"),
            identity_scope_ref=state.metadata.get("identity_scope_ref"),
            subject_scope_ref=state.metadata.get("subject_scope_ref"),
        )
        # Preserve the exact physical activity identity on the node projection.
        # Side-effect authorization happens after the activity leaves the active
        # set, so reconstructing it from the definition/step label would lose
        # retry and loop-instance identity.
        metadata["activity_id"] = activity.activity_id
        metadata["activity_attempt"] = activity.attempt
        active_activities = (
            *active_activities,
            HarnessActiveActivityState(
                activity.activity_id,
                activity.activity_ref,
                activity.node_instance_id,
                activity.attempt,
                activity.idempotency_key,
                activity.fencing_generation,
                decision_sequence,
            ),
        )
        step_status = HarnessStepStatus.RUNNING
    elif decision_type is HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT:
        if status is HarnessNodeInstanceStatus.WAITING:
            status = HarnessNodeInstanceStatus.RUNNING
        step_status = HarnessStepStatus.VERIFYING
    elif decision_type is HarnessGraphDecisionType.COMPLETE_NODE:
        status = (
            HarnessNodeInstanceStatus.SUCCEEDED
            if compensation_entry is None
            else HarnessNodeInstanceStatus.COMPENSATED
        )
        step_status = HarnessStepStatus.SUCCEEDED
        if compensation_entry is not None:
            compensation_entry = replace(
                compensation_entry,
                status=HarnessCompensationStatus.SUCCEEDED,
                outcome_ref=decision.decision_checksum,
                last_event_sequence=projection_sequence,
            )
            compensation_stack = _replace_compensation_entry(
                compensation_stack,
                compensation_entry,
            )
        else:
            activity_payload_ref = metadata.get("activity_payload_ref")
            if isinstance(activity_payload_ref, str):
                for output_key in definition.output_keys:
                    output_refs[output_key] = activity_payload_ref
        if compensation_entry is None and definition.side_effect_ref is not None:
            if side_effect_outcome_ref is None:
                raise HarnessValidationError(
                    "effectful node completion requires a durable outcome reference",
                    code="graph_side_effect_outcome_missing",
                )
            raw_pending = node.metadata.get("pending_side_effect")
            if not isinstance(raw_pending, Mapping):
                raise HarnessValidationError(
                    "effectful node completion has no durable preparation",
                    code="graph_side_effect_preparation_missing",
                )
            pending = HarnessPendingSideEffectState.from_dict(raw_pending)
            if (
                pending.scope is not HarnessPendingSideEffectScope.NODE_INSTANCE
                or pending.status
                is not HarnessPendingSideEffectStatus.OUTCOME_RECORDED
                or pending.outcome_ref != side_effect_outcome_ref
                or pending.prepare_decision_ref
                != decision.payload.get("side_effect_prepare_decision_ref")
                or pending.outcome_ref
                != decision.payload.get("side_effect_outcome_ref")
                or pending.authorization_ref is None
                or pending.observation_sequence is None
            ):
                raise HarnessValidationError(
                    "effectful node completion conflicts with its recorded outcome",
                    code="graph_side_effect_preparation_mismatch",
                )
            effect_evidence = next(
                (
                    item
                    for item in evidence_refs
                    if item.kind is HarnessEvidenceKind.SIDE_EFFECT_OUTCOME
                    and item.evidence_ref == side_effect_outcome_ref
                    and item.event_sequence == pending.observation_sequence
                    and item.contract_ref == definition.side_effect_ref
                ),
                None,
            )
            if effect_evidence is None:
                raise HarnessValidationError(
                    "effectful node completion lacks accepted outcome observation",
                    code="graph_decision_side_effect_evidence_missing",
                )
            metadata["side_effect_outcome_ref"] = side_effect_outcome_ref
            metadata["side_effect_decision_ref"] = pending.authorization_ref
            metadata.pop("pending_side_effect", None)
            binding = next(
                (
                    item
                    for item in graph.compensation_refs
                    if item.for_node_id == definition.node_id
                    and item.scope == "node_instance"
                ),
                None,
            )
            if binding is not None:
                entry_id = canonical_checksum(
                    {
                        "run_id": state.run_id,
                        "origin_node_instance_id": node.instance_id,
                        "effect_outcome_ref": side_effect_outcome_ref,
                        "binding_id": binding.binding_id,
                    }
                )
                if any(item.entry_id == entry_id for item in compensation_stack):
                    raise HarnessValidationError(
                        "compensation entry already exists for the effect outcome",
                        code="duplicate_compensation_entry",
                    )
                compensation_stack = (
                    *compensation_stack,
                    HarnessCompensationEntry(
                        entry_id=entry_id,
                        origin_node_instance_id=node.instance_id,
                        effect_outcome_ref=side_effect_outcome_ref,
                        effect_commit_sequence=pending.observation_sequence,
                        handler_ref=binding.handler_ref,
                        activity_ref=binding.activity_ref,
                        idempotency_key=canonical_checksum(
                            {
                                "operation": "compensate",
                                "entry_id": entry_id,
                                "handler_ref": binding.handler_ref.exact_ref,
                            }
                        ),
                        fencing_generation=1,
                        last_event_sequence=projection_sequence,
                    ),
                )
        elif side_effect_outcome_ref is not None:
            raise HarnessValidationError(
                "side-effect outcome targets a node without a pinned effect binding",
                code="graph_decision_side_effect_outcome_mismatch",
            )
    elif decision_type is HarnessGraphDecisionType.FAIL_NODE:
        status = HarnessNodeInstanceStatus.FAILED
        step_status = HarnessStepStatus.FAILED
        error_code = decision.reason_code
        terminal_reason = decision.reason_code
        if compensation_entry is not None:
            compensation_entry = replace(
                compensation_entry,
                status=HarnessCompensationStatus.FAILED,
                outcome_ref=decision.decision_checksum,
                last_event_sequence=projection_sequence,
            )
            compensation_stack = _replace_compensation_entry(
                compensation_stack,
                compensation_entry,
            )
    elif decision_type is HarnessGraphDecisionType.RETRY_NODE:
        step_status = HarnessStepStatus.RETRYING
    elif decision_type is HarnessGraphDecisionType.REPLAN_NODE:
        step_status = HarnessStepStatus.REPLANNING
        replans += 1
    elif decision_type is HarnessGraphDecisionType.ROUTE_TO_REPAIR:
        status = HarnessNodeInstanceStatus.FAILED
        step_status = HarnessStepStatus.FAILED
        error_code = decision.reason_code
        terminal_reason = decision.reason_code
    elif decision_type is HarnessGraphDecisionType.WAIT_NODE:
        status = HarnessNodeInstanceStatus.WAITING
        step_status = HarnessStepStatus.WAITING_APPROVAL
        if decision.payload.get("step_transition_type") == "block_step":
            metadata["worker_blocked"] = True
            error_code = decision.reason_code
            terminal_reason = decision.reason_code
    else:  # pragma: no cover - caller dispatches only Step decisions
        raise AssertionError(f"unexpected Step decision: {decision_type.value}")
    updated = replace(
        node,
        status=status,
        step_status=step_status,
        attempt=attempt,
        replans=replans,
        output_refs=output_refs,
        evidence_refs=evidence_refs,
        error_code=error_code,
        terminal_reason=terminal_reason,
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    projected = replace(
        state,
        lifecycle=(
            RunLifecycle.RUNNING
            if state.lifecycle in {RunLifecycle.CREATED, RunLifecycle.WAITING}
            and status
            in {
                HarnessNodeInstanceStatus.RUNNING,
                HarnessNodeInstanceStatus.COMPENSATING,
            }
            else state.lifecycle
        ),
        node_instances=_replace_node(state.node_instances, updated),
        active_activities=active_activities,
        compensation_stack=compensation_stack,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )
    return projected, activity


def _apply_control_decision(
    state: HarnessGraphState,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
    succeeded: bool,
) -> HarnessGraphState:
    if decision.node_instance_id is None:
        raise HarnessValidationError(
            "control decision requires a node instance",
            code="graph_control_node_identity_missing",
        )
    node = _node_instance(state, decision.node_instance_id)
    allowed_kinds = _CONTROL_DECISION_NODE_KINDS[decision.decision_type]
    if node.node_kind not in allowed_kinds:
        raise HarnessValidationError(
            "control decision targets an incompatible node kind",
            code="graph_control_node_kind_mismatch",
            details={
                "decision_type": decision.decision_type.value,
                "node_kind": node.node_kind.value,
                "allowed_node_kinds": sorted(item.value for item in allowed_kinds),
            },
        )
    expected_status = (
        HarnessNodeInstanceStatus.RUNNING
        if decision.decision_type in _RUNNING_CONTROL_DECISIONS
        else HarnessNodeInstanceStatus.READY
    )
    if node.status is not expected_status:
        raise HarnessValidationError(
            "control decision is incompatible with the current node status",
            code="graph_control_node_state_mismatch",
            details={
                "decision_type": decision.decision_type.value,
                "node_status": node.status.value,
                "expected_status": expected_status.value,
            },
        )
    status = (
        HarnessNodeInstanceStatus.SUCCEEDED
        if succeeded
        else HarnessNodeInstanceStatus.FAILED
    )
    updated = replace(
        node,
        status=status,
        error_code=None if succeeded else decision.reason_code,
        terminal_reason=None if succeeded else decision.reason_code,
        last_event_sequence=projection_sequence,
        metadata=_merged_node_metadata(node, decision),
    )
    joins = state.join_states
    state_metadata = thaw_json(state.metadata)
    if decision.decision_type in {
        HarnessGraphDecisionType.SATISFY_JOIN,
        HarnessGraphDecisionType.FAIL_JOIN,
        HarnessGraphDecisionType.SELECT_PARALLEL_WINNER,
    }:
        join = next(
            (
                item
                for item in joins
                if item.join_instance_id == decision.node_instance_id
            ),
            None,
        )
        if join is not None:
            winner = join.winner_branch_id
            if (
                decision.decision_type
                is HarnessGraphDecisionType.SELECT_PARALLEL_WINNER
            ):
                graph_candidate = decision.payload.get("graph_candidate", {})
                if isinstance(graph_candidate, Mapping):
                    winner = graph_candidate.get("branch_id")
            join_status = (
                HarnessJoinStatus.FAILED
                if decision.decision_type is HarnessGraphDecisionType.FAIL_JOIN
                else HarnessJoinStatus.SATISFIED
            )
            updated_join = replace(
                join,
                status=join_status,
                winner_branch_id=winner,
                last_event_sequence=projection_sequence,
            )
            joins = tuple(
                updated_join if item.join_instance_id == join.join_instance_id else item
                for item in joins
            )
        if (
            decision.decision_type is HarnessGraphDecisionType.FAIL_JOIN
            and decision.payload.get("failure_policy") == "compensate"
        ):
            state_metadata.update(
                {
                    "execution_mode": "compensating",
                    "compensation_trigger_ref": decision.decision_checksum,
                    "compensation_trigger_node_instance_id": node.instance_id,
                }
            )
    return replace(
        state,
        node_instances=_replace_node(state.node_instances, updated),
        join_states=joins,
        metadata=state_metadata,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_merge_decision(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None or decision.node_id is None:
        raise HarnessValidationError(
            "Merge decision requires one node instance",
            code="graph_control_node_identity_missing",
        )
    node = _node_instance(state, decision.node_instance_id)
    definition = _definition(graph, decision.node_id)
    if (
        not isinstance(definition, HarnessControlNode)
        or definition.merge is None
        or node.node_kind is not HarnessGraphNodeKind.MERGE
        or node.status is not HarnessNodeInstanceStatus.READY
    ):
        raise HarnessValidationError(
            "Merge decision targets an incompatible node",
            code="graph_merge_node_state_mismatch",
        )
    raw_inputs = decision.payload.get("input_refs")
    if not isinstance(raw_inputs, tuple):
        raise HarnessValidationError(
            "Merge decision is missing exact branch output references",
            code="graph_merge_input_missing",
        )
    inputs = tuple(HarnessBranchOutputReference.from_dict(item) for item in raw_inputs)
    ordered = tuple(sorted(inputs, key=lambda item: item.stable_order_key))
    if inputs != ordered:
        raise HarnessValidationError(
            "Merge decision branch references are not canonically ordered",
            code="graph_merge_input_order_mismatch",
        )
    input_checksum = canonical_checksum([item.to_dict() for item in ordered])
    if decision.payload.get("input_checksum") != input_checksum:
        raise HarnessValidationError(
            "Merge decision input checksum does not match its exact references",
            code="graph_merge_input_checksum_mismatch",
        )
    merge_ref = (
        None
        if definition.merge.merge_ref is None
        else definition.merge.merge_ref.exact_ref
    )
    operation_id = canonical_checksum(
        {
            "run_id": state.run_id,
            "graph_checksum": graph.checksum,
            "merge_node_instance_id": node.instance_id,
            "merge_ref": merge_ref,
            "input_checksum": input_checksum,
        }
    )
    if decision.payload.get("operation_id") != operation_id:
        raise HarnessValidationError(
            "Merge decision operation identity does not match its pinned inputs",
            code="graph_merge_operation_identity_mismatch",
        )
    metadata = thaw_json(node.metadata)
    metadata.update(
        {
            "merge_operation_id": operation_id,
            "merge_input_checksum": input_checksum,
            "merge_input_refs": [item.to_dict() for item in ordered],
            "merge_decision_ref": decision.decision_checksum,
        }
    )
    if definition.merge.merge_kind is HarnessMergeKind.PURE:
        updated = replace(
            node,
            status=HarnessNodeInstanceStatus.RUNNING,
            last_event_sequence=projection_sequence,
            metadata=metadata,
        )
    else:
        aggregation_id = decision.payload.get("aggregation_node_instance_id")
        aggregation = (
            None
            if not isinstance(aggregation_id, str)
            else next(
                (
                    item
                    for item in state.node_instances
                    if item.instance_id == aggregation_id
                ),
                None,
            )
        )
        if (
            aggregation is None
            or aggregation.identity.node_id != definition.merge.aggregation_node_id
            or aggregation.status is not HarnessNodeInstanceStatus.SUCCEEDED
        ):
            raise HarnessValidationError(
                "Merge marker requires one verified aggregation node instance",
                code="graph_merge_aggregation_evidence_missing",
            )
        if (
            decision.payload.get("aggregation_terminal_ref")
            != aggregation.metadata.get("last_decision_checksum")
            or aggregation.metadata.get("last_decision_type") != "complete_node"
        ):
            raise HarnessValidationError(
                "Merge marker lacks exact aggregation terminal evidence",
                code="graph_merge_aggregation_evidence_missing",
            )
        all_output_refs = thaw_json(aggregation.output_refs)
        output_refs = {
            key: all_output_refs[key]
            for key in definition.merge.output_keys
            if key in all_output_refs
        }
        if set(output_refs) != set(definition.merge.output_keys) or not all(
            _is_checksum_ref(value) for value in output_refs.values()
        ):
            raise HarnessValidationError(
                "verified aggregation outputs do not match the Merge contract",
                code="graph_merge_output_contract_mismatch",
            )
        if decision.payload.get("aggregation_output_refs") != output_refs:
            raise HarnessValidationError(
                "Merge decision aggregation outputs differ from durable state",
                code="graph_merge_output_contract_mismatch",
            )
        metadata["aggregation_node_instance_id"] = aggregation.instance_id
        metadata["aggregation_terminal_ref"] = decision.payload[
            "aggregation_terminal_ref"
        ]
        updated = replace(
            node,
            status=HarnessNodeInstanceStatus.SUCCEEDED,
            output_refs=output_refs,
            last_event_sequence=projection_sequence,
            metadata=metadata,
        )
    return replace(
        state,
        node_instances=_replace_node(state.node_instances, updated),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_side_effect_observation(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
    observation: HarnessAcceptedGraphObservation,
    evidence: HarnessAttemptEvidenceReference,
    *,
    metadata: dict[str, Any],
    projection_sequence: int,
) -> HarnessGraphState:
    if observation.observation_type is HarnessGraphObservationType.SIDE_EFFECT_OUTCOME:
        prepare_ref = observation.payload["prepare_decision_ref"]
        authorization_ref = observation.payload["decision_ref"]
        outcome_ref = observation.payload["outcome_ref"]
        failure_ref = None
        reason_code = None
        status = HarnessPendingSideEffectStatus.OUTCOME_RECORDED
    else:
        prepare_ref = observation.payload["causal_graph_decision_checksum"]
        authorization_ref = observation.payload["decision_ref"]
        outcome_ref = None
        failure_ref = observation.payload["failure_ref"]
        reason_code = observation.payload["reason_code"]
        status = HarnessPendingSideEffectStatus.FAILED

    candidates: list[tuple[str, HarnessPendingSideEffectState]] = []
    raw_node_pending = node.metadata.get("pending_side_effect")
    if isinstance(raw_node_pending, Mapping):
        pending = HarnessPendingSideEffectState.from_dict(raw_node_pending)
        if pending.prepare_decision_ref == prepare_ref:
            candidates.append(("node", pending))
    raw_terminal_pending = state.metadata.get("pending_terminal_side_effect")
    if isinstance(raw_terminal_pending, Mapping):
        pending = HarnessPendingSideEffectState.from_dict(raw_terminal_pending)
        if pending.prepare_decision_ref == prepare_ref:
            candidates.append(("terminal", pending))
    if len(candidates) != 1:
        raise HarnessValidationError(
            "side-effect observation has no unique durable preparation",
            code="graph_side_effect_preparation_missing",
        )
    location, pending = candidates[0]
    if (
        pending.status is not HarnessPendingSideEffectStatus.PREPARED
        or pending.node_id != observation.node_id
        or pending.node_instance_id != observation.node_instance_id
        or pending.attempt != observation.attempt
        or pending.handler_ref != observation.contract_ref
    ):
        raise HarnessValidationError(
            "side-effect observation conflicts with its durable preparation",
            code="graph_side_effect_preparation_mismatch",
        )
    if (
        observation.observation_type is HarnessGraphObservationType.SIDE_EFFECT_OUTCOME
        and observation.payload["scope"] != pending.scope.value
    ):
        raise HarnessValidationError(
            "side-effect outcome scope conflicts with its durable preparation",
            code="graph_side_effect_scope_mismatch",
        )
    recorded = replace(
        pending,
        status=status,
        authorization_ref=authorization_ref,
        outcome_ref=outcome_ref,
        failure_ref=failure_ref,
        reason_code=reason_code,
        observation_sequence=observation.event_sequence,
    )
    if status is HarnessPendingSideEffectStatus.OUTCOME_RECORDED:
        metadata["side_effect_outcome_ref"] = outcome_ref
        if location == "terminal":
            metadata["terminal_side_effect_outcome_ref"] = outcome_ref
    else:
        metadata["side_effect_failure_ref"] = failure_ref
        metadata["side_effect_failure_reason"] = reason_code
    if location == "node":
        metadata["pending_side_effect"] = recorded.to_dict()
    updated_node = replace(
        node,
        evidence_refs=(*node.evidence_refs, evidence),
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    state_metadata = thaw_json(state.metadata)
    if location == "terminal":
        state_metadata["pending_terminal_side_effect"] = recorded.to_dict()
    return replace(
        state,
        node_instances=_replace_node(state.node_instances, updated_node),
        metadata=state_metadata,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_wait_cause_observation(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
    definition: HarnessGraphNode,
    observation: HarnessAcceptedGraphObservation,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if not isinstance(definition, HarnessControlNode) or definition.wait is None:
        raise HarnessValidationError(
            "Wait cause targets a node without a Wait contract",
            code="graph_wait_contract_missing",
        )
    payload = thaw_json(observation.payload)
    if not isinstance(payload, Mapping):
        raise HarnessValidationError(
            "Wait cause payload must be an object",
            code="invalid_graph_observation_payload",
        )
    cause_kind = HarnessWaitCauseKind(payload["cause_kind"])
    raw_record = payload["record"]
    if not isinstance(raw_record, Mapping):
        raise HarnessValidationError(
            "Wait cause record must be an object",
            code="invalid_graph_observation_payload",
        )
    record: Any
    cause_ref: str
    scope: HarnessWaitScope
    if cause_kind is HarnessWaitCauseKind.SIGNAL:
        record = HarnessWaitSignal.from_dict(raw_record)
        cause_ref = record.signal_ref
        scope = record.scope
        record_sequence = record.received_sequence
    elif cause_kind is HarnessWaitCauseKind.TIMER:
        record = HarnessWaitTimerWakeRecord.from_dict(raw_record)
        cause_ref = record.wake_ref
        scope = record.scope
        record_sequence = record.recorded_sequence
    elif cause_kind is HarnessWaitCauseKind.TIMEOUT:
        record = HarnessWaitTimeoutRecord.from_dict(raw_record)
        cause_ref = record.timeout_ref
        scope = record.scope
        record_sequence = record.timed_out_sequence
    elif cause_kind is HarnessWaitCauseKind.APPROVAL:
        record = HarnessWaitApprovalEvidenceRecord.from_dict(raw_record)
        cause_ref = record.approval_ref
        scope = record.scope
        record_sequence = record.recorded_sequence
    elif cause_kind is HarnessWaitCauseKind.CANCELLATION:
        record = HarnessWaitCancellationRecord.from_dict(raw_record)
        cause_ref = record.cancellation_ref
        scope = record.scope
        record_sequence = record.cancelled_sequence
    else:  # pragma: no cover - enum exhaustiveness guard
        raise AssertionError(f"unsupported Wait cause kind: {cause_kind.value}")
    if observation.evidence_ref != cause_ref:
        raise HarnessValidationError(
            "Wait cause evidence does not match its canonical record",
            code="graph_wait_cause_evidence_mismatch",
        )
    if record_sequence != observation.event_sequence:
        raise HarnessValidationError(
            "Wait cause sequence does not match its canonical stream event",
            code="graph_wait_cause_sequence_mismatch",
        )
    if (
        scope.run_id != state.run_id
        or scope.node_instance_id != node.instance_id
        or scope.wait_id != definition.wait.wait_id
        or scope.signal_schema_ref
        != f"{definition.wait.signal_type}@{definition.wait.signal_version}"
    ):
        raise HarnessValidationError(
            "Wait cause is outside the pinned run/node scope",
            code="graph_wait_cause_scope_mismatch",
        )
    if cause_kind is HarnessWaitCauseKind.SIGNAL and definition.wait.kind is not WaitKind.SIGNAL:
        raise HarnessValidationError(
            "signal cause targets a non-signal Wait",
            code="graph_wait_cause_kind_mismatch",
        )
    if cause_kind is HarnessWaitCauseKind.TIMER and definition.wait.kind is not WaitKind.TIMER:
        raise HarnessValidationError(
            "timer cause targets a non-timer Wait",
            code="graph_wait_cause_kind_mismatch",
        )
    if (
        cause_kind is HarnessWaitCauseKind.TIMEOUT
        and definition.wait.timeout_policy is None
    ):
        raise HarnessValidationError(
            "timeout cause targets a Wait without a timeout policy",
            code="graph_wait_cause_kind_mismatch",
        )
    if cause_kind is HarnessWaitCauseKind.APPROVAL and definition.wait.kind is not WaitKind.APPROVAL:
        raise HarnessValidationError(
            "approval cause targets a non-approval Wait",
            code="graph_wait_cause_kind_mismatch",
        )
    registrations = tuple(
        item
        for item in state.wait_registrations
        if item.node_instance_id == node.instance_id
    )
    registration = (
        None
        if not registrations
        else max(
            registrations,
            key=lambda item: (item.registered_sequence, item.wait_id),
        )
    )
    if registration is None:
        if cause_kind is not HarnessWaitCauseKind.SIGNAL or node.status is not HarnessNodeInstanceStatus.READY:
            raise HarnessValidationError(
                "Wait cause requires a durable registration",
                code="graph_wait_registration_missing",
            )
        existing = next(
            (
                item
                for item in state.signal_inbox
                if item.signal.signal_ref == record.signal_ref
            ),
            None,
        )
        if existing is not None:
            if existing.signal.idempotency_projection() != record.idempotency_projection():
                raise HarnessValidationError(
                    "Wait signal identity was reused with conflicting content",
                    code="wait_signal_identity_conflict",
                )
            return replace(
                state,
                last_event_sequence=projection_sequence,
                projection_checksum=None,
            )
        inbox = _append_signal_with_retention(
            state.signal_inbox,
            HarnessSignalInboxEntry(signal=record),
            through_sequence=projection_sequence,
        )
        return replace(
            state,
            signal_inbox=inbox,
            last_event_sequence=projection_sequence,
            projection_checksum=None,
        )
    expected_scope = HarnessWaitScope(
        wait_id=registration.wait_id,
        run_id=state.run_id,
        node_instance_id=node.instance_id,
        tenant_scope_ref=registration.tenant_scope_ref,
        identity_scope_ref=registration.identity_scope_ref,
        signal_schema_ref=registration.signal_schema_ref,
        correlation_ref=registration.correlation_ref,
    )
    if scope != expected_scope:
        raise HarnessValidationError(
            "Wait cause does not match its durable registration scope",
            code="graph_wait_cause_scope_mismatch",
        )
    if not registration.unresolved:
        if registration.resolution_event_ref == cause_ref:
            return replace(
                state,
                last_event_sequence=projection_sequence,
                projection_checksum=None,
            )
        raise HarnessValidationError(
            "resolved Wait cannot accept a second cause",
            code="graph_wait_already_resolved",
        )

    updated_registration = registration
    inbox = list(state.signal_inbox)
    if cause_kind is HarnessWaitCauseKind.SIGNAL:
        registration_record = HarnessWaitRegistrationRecord(
            scope=expected_scope,
            kind=WaitKind.SIGNAL,
            registered_sequence=registration.registered_sequence,
            deadline_ref=registration.deadline_ref,
        )
        match = HarnessWaitSignalMatch(
            scope=expected_scope,
            registration_ref=registration_record.registration_ref,
            signal_ref=record.signal_ref,
            matched_sequence=projection_sequence,
        )
        existing = next(
            (item for item in inbox if item.signal.signal_ref == record.signal_ref),
            None,
        )
        if existing is not None and (
            existing.signal.idempotency_projection()
            != record.idempotency_projection()
        ):
            raise HarnessValidationError(
                "Wait signal identity was reused with conflicting content",
                code="wait_signal_identity_conflict",
            )
        if existing is None:
            inbox = list(
                _append_signal_with_retention(
                    tuple(inbox),
                    HarnessSignalInboxEntry(
                        signal=record,
                        status=HarnessSignalInboxEntryStatus.MATCHED,
                        match=match,
                    ),
                    through_sequence=projection_sequence,
                )
            )
        else:
            inbox = [
                replace(
                    item,
                    status=HarnessSignalInboxEntryStatus.MATCHED,
                    match=match,
                )
                if item.signal.signal_ref == record.signal_ref
                else item
                for item in inbox
            ]
        updated_registration = replace(
            registration,
            status=HarnessWaitStatus.RESUMED,
            resolution_event_ref=record.signal_ref,
            last_event_sequence=projection_sequence,
        )
    elif cause_kind is HarnessWaitCauseKind.TIMER:
        if record.deadline_ref != registration.deadline_ref:
            raise HarnessValidationError(
                "timer wake does not match the registered deadline",
                code="graph_wait_deadline_mismatch",
            )
        updated_registration = replace(
            registration,
            status=HarnessWaitStatus.RESUMED,
            resolution_event_ref=record.wake_ref,
            last_event_sequence=projection_sequence,
        )
    elif cause_kind is HarnessWaitCauseKind.TIMEOUT:
        if record.deadline_ref != registration.deadline_ref:
            raise HarnessValidationError(
                "Wait timeout does not match the registered deadline",
                code="graph_wait_deadline_mismatch",
            )
        updated_registration = replace(
            registration,
            status=HarnessWaitStatus.TIMED_OUT,
            resolution_event_ref=record.timeout_ref,
            last_event_sequence=projection_sequence,
        )
    elif cause_kind is HarnessWaitCauseKind.APPROVAL:
        # A re-entrant Wait hands its grant to the resumed node, so the
        # decision evidence must name exactly that grant (fail closed).
        if registration.reentrant and record.approval_id != registration.approval_id:
            raise HarnessValidationError(
                "approval evidence targets another grant than the durable Wait",
                code="graph_wait_approval_id_mismatch",
            )
        updated_registration = replace(
            registration,
            status=(
                HarnessWaitStatus.RESUMED
                if record.approved
                else HarnessWaitStatus.CANCELLED
            ),
            resolution_event_ref=record.approval_ref,
            last_event_sequence=projection_sequence,
        )
    else:
        updated_registration = replace(
            registration,
            status=HarnessWaitStatus.CANCELLED,
            resolution_event_ref=record.cancellation_ref,
            last_event_sequence=projection_sequence,
        )
    metadata = thaw_json(node.metadata)
    metadata.update(
        {
            "wait_cause_ref": cause_ref,
            "wait_cause_kind": cause_kind.value,
        }
    )
    if cause_kind is HarnessWaitCauseKind.APPROVAL:
        metadata["approval_granted"] = record.approved
        metadata["wait_resolution_ref"] = record.approval_ref
        metadata["approval_evidence_ref"] = record.approval_event_ref
    updated_node = replace(
        node,
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    return replace(
        state,
        lifecycle=(
            RunLifecycle.RUNNING
            if state.lifecycle is RunLifecycle.WAITING
            else state.lifecycle
        ),
        node_instances=_replace_node(state.node_instances, updated_node),
        wait_registrations=tuple(
            updated_registration
            if item.node_instance_id == registration.node_instance_id
            else item
            for item in state.wait_registrations
        ),
        signal_inbox=tuple(inbox),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _append_signal_with_retention(
    current: tuple[HarnessSignalInboxEntry, ...],
    entry: HarnessSignalInboxEntry,
    *,
    through_sequence: int,
) -> tuple[HarnessSignalInboxEntry, ...]:
    policy = _WAIT_SIGNAL_RETENTION_POLICY
    if entry.signal.received_sequence > through_sequence:
        raise HarnessValidationError(
            "Wait signal receipt sequence is ahead of its durable cause",
            code="wait_signal_sequence_regression",
        )
    cutoff = through_sequence - policy.sequence_window
    retained = tuple(
        item
        for item in current
        if item.signal.received_sequence > cutoff
    )
    if entry.signal.received_sequence <= cutoff:
        raise HarnessValidationError(
            "Wait signal is outside the bounded early-signal window",
            code="early_signal_retention_expired",
        )
    if len(retained) >= policy.max_signals:
        raise HarnessValidationError(
            "Wait signal inbox reached its total retention bound",
            code="early_signal_retention_exhausted",
            details={"max_signals": policy.max_signals},
        )
    scope_count = sum(item.signal.scope == entry.signal.scope for item in retained)
    if scope_count >= policy.max_signals_per_scope:
        raise HarnessValidationError(
            "Wait signal inbox reached its per-scope retention bound",
            code="early_signal_scope_retention_exhausted",
            details={"max_signals_per_scope": policy.max_signals_per_scope},
        )
    return (*retained, entry)


def _apply_merge_result_observation(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
    definition: HarnessGraphNode,
    observation: HarnessAcceptedGraphObservation,
    evidence: HarnessAttemptEvidenceReference,
    *,
    metadata: dict[str, Any],
    projection_sequence: int,
) -> HarnessGraphState:
    if (
        not isinstance(definition, HarnessControlNode)
        or definition.merge is None
        or definition.merge.merge_kind is not HarnessMergeKind.PURE
        or node.status is not HarnessNodeInstanceStatus.RUNNING
    ):
        raise HarnessValidationError(
            "Merge result targets an incompatible node state",
            code="graph_merge_node_state_mismatch",
        )
    payload = thaw_json(observation.payload)
    if (
        payload["operation_id"] != metadata.get("merge_operation_id")
        or payload["input_checksum"] != metadata.get("merge_input_checksum")
        or payload["input_refs"] != metadata.get("merge_input_refs")
    ):
        raise HarnessValidationError(
            "Merge result does not match the committed Merge operation",
            code="graph_merge_result_identity_mismatch",
        )
    inputs = tuple(
        HarnessBranchOutputReference.from_dict(item) for item in payload["input_refs"]
    )
    if (
        canonical_checksum([item.to_dict() for item in inputs])
        != payload["input_checksum"]
    ):
        raise HarnessValidationError(
            "Merge result input checksum is invalid",
            code="graph_merge_input_checksum_mismatch",
        )
    succeeded = bool(payload["succeeded"])
    output_refs = dict(payload["output_refs"])
    outputs = dict(payload["outputs"])
    if succeeded:
        if set(outputs) != set(definition.merge.output_keys):
            raise HarnessValidationError(
                "Merge result keys do not match the pinned output contract",
                code="graph_merge_output_contract_mismatch",
            )
        allowed_refs = {
            value
            for item in inputs
            for value in (item.payload_ref, item.producer_terminal_ref)
        }
        for output_key, output in outputs.items():
            _validate_pure_merge_manifest(output, allowed_refs=allowed_refs)
            if output_refs.get(output_key) != canonical_checksum(output):
                raise HarnessValidationError(
                    "Merge output reference does not match its manifest",
                    code="graph_merge_output_checksum_mismatch",
                )
        status = HarnessNodeInstanceStatus.SUCCEEDED
        error_code = None
        terminal_reason = None
    else:
        if output_refs or outputs:
            raise HarnessValidationError(
                "failed Merge result cannot expose outputs",
                code="graph_merge_output_contract_mismatch",
            )
        status = HarnessNodeInstanceStatus.FAILED
        error_code = str(payload["reason_code"])
        terminal_reason = str(payload["reason_code"])
    metadata["merge_result_ref"] = observation.evidence_ref
    metadata["merge_outputs"] = outputs
    updated = replace(
        node,
        status=status,
        output_refs=output_refs,
        evidence_refs=(*node.evidence_refs, evidence),
        error_code=error_code,
        terminal_reason=terminal_reason,
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    return replace(
        state,
        node_instances=_replace_node(state.node_instances, updated),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _validate_pure_merge_manifest(
    value: Any,
    *,
    allowed_refs: set[str],
) -> None:
    if isinstance(value, str):
        if value not in allowed_refs:
            raise HarnessValidationError(
                "pure Merge output references data outside its exact inputs",
                code="graph_merge_output_reference_forged",
                details={"reference": value},
            )
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            required_text(key, "merge_output.key")
            _validate_pure_merge_manifest(item, allowed_refs=allowed_refs)
        return
    if isinstance(value, tuple | list):
        for item in value:
            _validate_pure_merge_manifest(item, allowed_refs=allowed_refs)
        return
    raise HarnessValidationError(
        "pure Merge output may contain only exact input references",
        code="graph_merge_output_reference_forged",
    )


def _apply_run_operation_observation(
    state: HarnessGraphState,
    observation: HarnessAcceptedGraphObservation,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    payload = thaw_json(observation.payload)
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("record"),
        Mapping,
    ):
        raise HarnessValidationError(
            "run operation observation is missing its typed record",
            code="invalid_graph_observation_payload",
        )
    try:
        operation = HarnessGraphRunOperation.from_dict(payload["record"])
    except (HarnessValidationError, TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "run operation observation violates its typed contract",
            code="invalid_graph_observation_payload",
        ) from exc
    if (
        operation.run_id != state.run_id
        or operation.accepted_sequence != observation.event_sequence
        or operation.operation_ref != observation.evidence_ref
    ):
        raise HarnessValidationError(
            "run operation observation does not match its Graph stream",
            code="graph_run_operation_identity_mismatch",
        )
    if state.lifecycle in {RunLifecycle.COMPLETED, RunLifecycle.HALTED}:
        raise HarnessValidationError(
            "terminal Graph run cannot accept a new operation",
            code="graph_run_operation_terminal",
        )
    metadata = thaw_json(state.metadata)
    existing_value = metadata.get("pending_run_operation")
    if existing_value is not None:
        if not isinstance(existing_value, Mapping):
            raise HarnessValidationError(
                "pending Graph run operation is invalid",
                code="invalid_pending_graph_run_operation",
            )
        existing = HarnessGraphRunOperation.from_dict(existing_value)
        if (
            existing.operation_identity_ref != operation.operation_identity_ref
            or existing.idempotency_projection() != operation.idempotency_projection()
        ):
            raise HarnessValidationError(
                "Graph run already has another pending operation",
                code="graph_run_operation_conflict",
            )
        raise HarnessValidationError(
            "Graph run operation was committed more than once",
            code="duplicate_graph_run_operation",
        )
    metadata["pending_run_operation"] = operation.to_dict()
    return replace(
        state,
        lifecycle=(
            RunLifecycle.RUNNING
            if state.lifecycle is RunLifecycle.CREATED
            else state.lifecycle
        ),
        metadata=metadata,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _request_branch_cancel(
    state: HarnessGraphState,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None:
        raise HarnessValidationError(
            "branch cancellation requires a node instance",
            code="graph_control_node_identity_missing",
        )
    node = _node_instance(state, decision.node_instance_id)
    if node.is_terminal:
        raise HarnessValidationError(
            "branch cancellation cannot target a terminal node",
            code="graph_control_node_state_mismatch",
        )
    active = any(
        item.node_instance_id == node.instance_id for item in state.active_activities
    )
    updated = replace(
        node,
        status=(
            HarnessNodeInstanceStatus.CANCEL_REQUESTED
            if active
            else HarnessNodeInstanceStatus.CANCELLED
        ),
        step_status=(
            node.step_status
            if active or node.step_status is None
            else HarnessStepStatus.HALTED
        ),
        error_code="branch_cancelled" if not active else None,
        terminal_reason="branch_cancelled" if not active else None,
        last_event_sequence=projection_sequence,
        metadata=_merged_node_metadata(node, decision),
    )
    return replace(
        state,
        node_instances=_replace_node(state.node_instances, updated),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _record_cancelled_branch_result(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
    evidence_ref: str,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    join = _owning_join_for_branch_result(state, node)
    if join is None:
        return state
    fork = _node_instance(state, join.fork_instance_id)
    parent_path = fork.identity.branch_path
    if (
        len(node.identity.branch_path) <= len(parent_path)
        or node.identity.branch_path[: len(parent_path)] != parent_path
    ):
        raise HarnessValidationError(
            "confirmed branch cancellation belongs to another join scope",
            code="join_branch_scope_mismatch",
        )
    branch_id = node.identity.branch_path[len(parent_path)]
    if branch_id not in join.required_branch_ids:
        raise HarnessValidationError(
            "confirmed cancellation references an unknown join branch",
            code="join_branch_identity_mismatch",
        )
    completed = thaw_json(join.completed_branch_instances)
    terminal_refs = thaw_json(join.terminal_event_refs)
    existing_instance_id = completed.get(branch_id)
    existing_ref = terminal_refs.get(branch_id)
    if existing_instance_id is not None:
        if existing_instance_id == node.instance_id and existing_ref == evidence_ref:
            return state
        # A composite branch may already be terminal because an earlier node
        # failed. A later cancellation confirmation in the same branch cannot
        # replace that first authoritative branch outcome.
        return state
    completed[branch_id] = node.instance_id
    terminal_refs[branch_id] = evidence_ref
    updated_join = replace(
        join,
        completed_branch_instances=completed,
        terminal_event_refs=terminal_refs,
        last_event_sequence=projection_sequence,
    )
    return replace(
        state,
        join_states=tuple(
            updated_join if item.join_instance_id == join.join_instance_id else item
            for item in state.join_states
        ),
        projection_checksum=None,
    )


def _owning_join_for_branch_result(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
) -> HarnessJoinState | None:
    decision_payload = node.metadata.get("decision_payload", {})
    requested_join_id = (
        decision_payload.get("join_instance_id")
        if isinstance(decision_payload, Mapping)
        else None
    )
    candidates: list[tuple[int, HarnessJoinState]] = []
    for join in state.join_states:
        if join.status is not HarnessJoinStatus.OPEN and not (
            join.join_kind is HarnessJoinKind.ANY
            and join.status is HarnessJoinStatus.SATISFIED
        ):
            continue
        fork = _node_instance(state, join.fork_instance_id)
        parent_path = fork.identity.branch_path
        if (
            len(node.identity.branch_path) <= len(parent_path)
            or node.identity.branch_path[: len(parent_path)] != parent_path
            or node.identity.iteration_vector[: len(fork.identity.iteration_vector)]
            != fork.identity.iteration_vector
        ):
            continue
        branch_id = node.identity.branch_path[len(parent_path)]
        if branch_id not in join.required_branch_ids:
            continue
        candidates.append((len(parent_path), join))
    if isinstance(requested_join_id, str):
        requested = tuple(
            item for _, item in candidates if item.join_instance_id == requested_join_id
        )
        if len(requested) != 1:
            raise HarnessValidationError(
                "branch cancellation decision references another Join scope",
                code="join_branch_scope_mismatch",
            )
        return requested[0]
    if not candidates:
        return None
    _, selected = max(
        candidates,
        key=lambda item: (item[0], item[1].join_instance_id),
    )
    return selected


def _open_parallel_join(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None or decision.node_id is None:
        raise HarnessValidationError(
            "fork opening requires a node instance",
            code="graph_control_node_identity_missing",
        )
    fork_instance = _node_instance(state, decision.node_instance_id)
    fork_definition = _definition(graph, decision.node_id)
    if not isinstance(fork_definition, HarnessControlNode):
        raise HarnessValidationError(
            "fork opening requires a control-node definition",
            code="graph_control_node_kind_mismatch",
        )
    expected_targets = tuple(
        target
        for branch in fork_definition.branches
        for target in branch.entry_node_ids
    )
    if decision.target_node_ids != expected_targets:
        raise HarnessValidationError(
            "fork decision targets conflict with the pinned branch definitions",
            code="graph_control_decision_mismatch",
            details={
                "expected_target_node_ids": list(expected_targets),
                "actual_target_node_ids": list(decision.target_node_ids),
            },
        )
    join_definitions = tuple(
        node
        for node in graph.nodes
        if isinstance(node, HarnessControlNode)
        and node.join is not None
        and node.join.fork_node_id == fork_definition.node_id
    )
    if len(join_definitions) != 1:
        raise HarnessValidationError(
            "fork opening cannot resolve one pinned join definition",
            code="graph_control_decision_mismatch",
            details={"fork_node_id": fork_definition.node_id},
        )
    join_definition = join_definitions[0]
    payload_join_id = decision.payload.get("join_node_id")
    if payload_join_id != join_definition.node_id:
        raise HarnessValidationError(
            "fork decision join identity conflicts with the pinned graph",
            code="graph_control_decision_mismatch",
            details={
                "expected_join_node_id": join_definition.node_id,
                "actual_join_node_id": payload_join_id,
            },
        )
    if any(
        item.identity.node_id == join_definition.node_id
        and item.identity.branch_path == fork_instance.identity.branch_path
        and item.identity.iteration_vector == fork_instance.identity.iteration_vector
        for item in state.node_instances
    ):
        raise HarnessValidationError(
            "parallel join scope is already open",
            code="duplicate_graph_node_activation",
        )
    active_node_count = sum(not item.is_terminal for item in state.node_instances)
    max_active_nodes = state.budgets.require("max_active_nodes").limit
    if active_node_count >= max_active_nodes:
        raise HarnessValidationError(
            "parallel join opening exceeds active-node capacity",
            code="graph_active_node_limit_exceeded",
            details={"active": active_node_count, "limit": max_active_nodes},
        )
    ordinal = (
        max(
            (item.identity.activation_ordinal for item in state.node_instances),
            default=0,
        )
        + 1
    )
    join_identity = HarnessNodeInstanceIdentity(
        state.run_id,
        graph.checksum,
        join_definition.node_id,
        branch_path=fork_instance.identity.branch_path,
        iteration_vector=fork_instance.identity.iteration_vector,
        activation_ordinal=ordinal,
    )
    join_node = HarnessNodeInstanceState(
        identity=join_identity,
        node_kind=join_definition.node_kind,
        status=HarnessNodeInstanceStatus.RUNNING,
        activation_sequence=projection_sequence,
        last_event_sequence=projection_sequence,
        metadata={
            **_decision_metadata(decision),
            "fork_instance_id": fork_instance.instance_id,
        },
    )
    join_kind = (
        HarnessJoinKind.ALL
        if join_definition.node_kind is HarnessGraphNodeKind.JOIN_ALL
        else HarnessJoinKind.ANY
    )
    join_state = HarnessJoinState(
        join_instance_id=join_node.instance_id,
        fork_instance_id=fork_instance.instance_id,
        join_kind=join_kind,
        status=HarnessJoinStatus.OPEN,
        required_branch_ids=join_definition.join.required_branch_ids,
        last_event_sequence=projection_sequence,
    )
    return replace(
        state,
        node_instances=(*state.node_instances, join_node),
        join_states=(*state.join_states, join_state),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_loop_counter_transition(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None or decision.node_id is None:
        raise HarnessValidationError(
            "loop transition requires a guard node instance",
            code="graph_control_node_identity_missing",
        )
    guard = _node_instance(state, decision.node_instance_id)
    definition = _definition(graph, decision.node_id)
    if (
        not isinstance(definition, HarnessControlNode)
        or definition.node_kind is not HarnessGraphNodeKind.LOOP_GUARD
        or definition.loop is None
    ):
        raise HarnessValidationError(
            "loop transition requires a pinned loop guard",
            code="graph_control_node_kind_mismatch",
        )
    counter = next(
        (
            item
            for item in state.loop_counters
            if item.loop_id == definition.node_id
            and item.branch_path == guard.identity.branch_path
            and item.parent_iteration_vector == guard.identity.iteration_vector
        ),
        None,
    )
    if counter is None:
        counter = HarnessLoopCounterState(
            loop_id=definition.node_id,
            branch_path=guard.identity.branch_path,
            parent_iteration_vector=guard.identity.iteration_vector,
            completed_iterations=0,
            max_iterations=definition.loop.max_iterations,
            status=HarnessLoopStatus.PENDING,
            last_event_sequence=guard.activation_sequence,
        )
        counters = (*state.loop_counters, counter)
    else:
        counters = state.loop_counters
    if counter.max_iterations != definition.loop.max_iterations:
        raise HarnessValidationError(
            "loop counter conflicts with the pinned maximum",
            code="invalid_loop_counter_state",
        )
    expected_targets = {
        HarnessGraphDecisionType.START_LOOP_ITERATION: definition.loop.body_entry_node_ids,
        HarnessGraphDecisionType.EXIT_LOOP: definition.loop.exit_node_ids,
        HarnessGraphDecisionType.EXHAUST_LOOP: definition.loop.exhaustion_node_ids,
    }[decision.decision_type]
    if decision.target_node_ids != expected_targets:
        raise HarnessValidationError(
            "loop transition targets conflict with the pinned loop contract",
            code="graph_control_decision_mismatch",
            details={
                "expected_target_node_ids": list(expected_targets),
                "actual_target_node_ids": list(decision.target_node_ids),
            },
        )
    if (
        decision.payload.get("max_iterations", counter.max_iterations)
        != counter.max_iterations
    ):
        raise HarnessValidationError(
            "loop transition maximum conflicts with the durable counter",
            code="loop_iteration_counter_mismatch",
        )
    if decision.decision_type is HarnessGraphDecisionType.START_LOOP_ITERATION:
        expected_iteration = counter.completed_iterations
        if decision.payload.get("iteration") != expected_iteration:
            raise HarnessValidationError(
                "loop start decision conflicts with the durable counter",
                code="loop_iteration_counter_mismatch",
                details={
                    "expected_iteration": expected_iteration,
                    "actual_iteration": decision.payload.get("iteration"),
                },
            )
        if counter.completed_iterations >= counter.max_iterations:
            raise HarnessValidationError(
                "loop start exceeds its pinned iteration bound",
                code="loop_iteration_bound_exceeded",
            )
        updated = replace(
            counter,
            status=HarnessLoopStatus.ACTIVE,
            last_event_sequence=projection_sequence,
        )
    elif decision.decision_type is HarnessGraphDecisionType.EXIT_LOOP:
        if decision.payload.get("completed_iterations") != counter.completed_iterations:
            raise HarnessValidationError(
                "loop exit conflicts with the durable iteration counter",
                code="loop_iteration_counter_mismatch",
            )
        updated = replace(
            counter,
            status=HarnessLoopStatus.EXITED,
            last_event_sequence=projection_sequence,
        )
    else:
        if (
            counter.completed_iterations != counter.max_iterations
            or decision.payload.get("completed_iterations")
            != counter.completed_iterations
        ):
            raise HarnessValidationError(
                "loop exhaustion requires the exact durable iteration bound",
                code="loop_iteration_counter_mismatch",
            )
        updated = replace(
            counter,
            status=HarnessLoopStatus.EXHAUSTED,
            last_event_sequence=projection_sequence,
        )
    return replace(
        state,
        loop_counters=tuple(
            updated if item.counter_id == counter.counter_id else item
            for item in counters
        ),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _record_loop_body_terminal(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None:
        return state
    node = _node_instance(state, decision.node_instance_id)
    if (
        node.status
        not in {
            HarnessNodeInstanceStatus.SUCCEEDED,
            HarnessNodeInstanceStatus.COMPENSATED,
        }
        or not node.identity.iteration_vector
    ):
        return state
    back_edges = tuple(
        edge
        for edge in graph.edges
        if edge.source_id == node.identity.node_id
        and edge.edge_kind is HarnessGraphEdgeKind.LOOP_BACK
    )
    if not back_edges:
        return state
    counters = state.loop_counters
    for edge in sorted(back_edges, key=lambda item: item.edge_id):
        loop_id = edge.loop_id
        iteration = node.identity.iteration_vector[-1]
        if loop_id is None or iteration.loop_id != loop_id:
            raise HarnessValidationError(
                "loop body terminal scope conflicts with its back edge",
                code="loop_iteration_counter_mismatch",
            )
        parent_vector = node.identity.iteration_vector[:-1]
        counter = next(
            (
                item
                for item in counters
                if item.loop_id == loop_id
                and item.branch_path == node.identity.branch_path
                and item.parent_iteration_vector == parent_vector
            ),
            None,
        )
        if counter is None:
            raise EventIncompleteHistoryError(
                "loop body completed without its durable counter"
            )
        completed = iteration.iteration + 1
        if completed != counter.completed_iterations + 1:
            raise HarnessValidationError(
                "loop body terminal must advance the durable counter by one",
                code="loop_iteration_counter_mismatch",
            )
        updated = replace(
            counter,
            completed_iterations=completed,
            status=(
                HarnessLoopStatus.EXHAUSTED
                if completed == counter.max_iterations
                else HarnessLoopStatus.ACTIVE
            ),
            last_event_sequence=projection_sequence,
        )
        counters = tuple(
            updated if item.counter_id == counter.counter_id else item
            for item in counters
        )
    return replace(
        state,
        loop_counters=counters,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _record_parallel_branch_terminal(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None:
        return state
    node = _node_instance(state, decision.node_instance_id)
    if not node.is_terminal:
        return state
    join_targets = [
        (edge.branch_id, edge.target_id, node.identity.branch_path[:-1], True)
        for edge in graph.edges
        if edge.source_id == node.identity.node_id
        and edge.edge_kind is HarnessGraphEdgeKind.JOIN
    ]
    if not join_targets and node.status not in {
        HarnessNodeInstanceStatus.SUCCEEDED,
        HarnessNodeInstanceStatus.COMPENSATED,
    }:
        owning = _nearest_open_join_for_failed_node(state, node)
        if owning is not None:
            join, branch_id = owning
            join_node = _node_instance(state, join.join_instance_id)
            fork = _node_instance(state, join.fork_instance_id)
            join_targets.append(
                (
                    branch_id,
                    join_node.identity.node_id,
                    fork.identity.branch_path,
                    False,
                )
            )
    if not join_targets:
        return state
    joins = state.join_states
    for branch_id, target_node_id, parent_path, strict_evidence in sorted(
        join_targets,
        key=lambda item: ("" if item[0] is None else item[0], item[1]),
    ):
        if branch_id is None or not node.identity.branch_path:
            raise HarnessValidationError(
                "parallel branch terminal edge has no branch scope",
                code="join_branch_scope_mismatch",
            )
        if (
            len(node.identity.branch_path) <= len(parent_path)
            or node.identity.branch_path[: len(parent_path)] != parent_path
            or node.identity.branch_path[len(parent_path)] != branch_id
        ):
            raise HarnessValidationError(
                "parallel branch terminal belongs to another branch scope",
                code="join_branch_scope_mismatch",
                details={"branch_id": branch_id},
            )
        join_node = next(
            (
                item
                for item in state.node_instances
                if item.identity.node_id == target_node_id
                and item.identity.branch_path == parent_path
                and item.identity.iteration_vector == node.identity.iteration_vector
            ),
            None,
        )
        if join_node is None:
            raise EventIncompleteHistoryError(
                "parallel branch completed without its durable join instance"
            )
        join = next(
            (item for item in joins if item.join_instance_id == join_node.instance_id),
            None,
        )
        accepts_late_loser = (
            join is not None
            and join.join_kind is HarnessJoinKind.ANY
            and join.status is HarnessJoinStatus.SATISFIED
        )
        if join is None or (
            join.status is not HarnessJoinStatus.OPEN and not accepts_late_loser
        ):
            raise EventIncompleteHistoryError(
                "parallel branch completed without one open durable join"
            )
        completed = thaw_json(join.completed_branch_instances)
        terminal_refs = thaw_json(join.terminal_event_refs)
        existing_instance_id = completed.get(branch_id)
        existing_ref = terminal_refs.get(branch_id)
        if existing_instance_id is not None and (
            existing_instance_id != node.instance_id
            or existing_ref != decision.decision_checksum
        ):
            if not strict_evidence:
                continue
            raise HarnessValidationError(
                "parallel branch completion conflicts with durable join evidence",
                code="join_evidence_identity_mismatch",
                details={"branch_id": branch_id},
            )
        completed[branch_id] = node.instance_id
        terminal_refs[branch_id] = decision.decision_checksum
        updated_join = replace(
            join,
            completed_branch_instances=completed,
            terminal_event_refs=terminal_refs,
            last_event_sequence=projection_sequence,
        )
        joins = tuple(
            updated_join if item.join_instance_id == join.join_instance_id else item
            for item in joins
        )
    return replace(
        state,
        join_states=joins,
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _nearest_open_join_for_failed_node(
    state: HarnessGraphState,
    node: HarnessNodeInstanceState,
) -> tuple[HarnessJoinState, str] | None:
    candidates: list[tuple[int, HarnessJoinState, str]] = []
    for join in state.join_states:
        if join.status is not HarnessJoinStatus.OPEN:
            continue
        fork = _node_instance(state, join.fork_instance_id)
        parent_path = fork.identity.branch_path
        if (
            node.identity.iteration_vector != fork.identity.iteration_vector
            or len(node.identity.branch_path) <= len(parent_path)
            or node.identity.branch_path[: len(parent_path)] != parent_path
        ):
            continue
        branch_id = node.identity.branch_path[len(parent_path)]
        if branch_id not in join.required_branch_ids:
            continue
        candidates.append((len(parent_path), join, branch_id))
    if not candidates:
        return None
    _, join, branch_id = max(
        candidates,
        key=lambda item: (item[0], item[1].join_instance_id, item[2]),
    )
    return join, branch_id


def _register_wait(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    decision_sequence: int,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None or decision.node_id is None:
        raise HarnessValidationError(
            "Wait registration requires exact node identity",
            code="graph_control_node_identity_missing",
        )
    node = _node_instance(state, decision.node_instance_id)
    definition = _definition(graph, decision.node_id)
    if (
        node.identity.node_id != decision.node_id
        or node.node_kind is not HarnessGraphNodeKind.WAIT
        or node.status is not HarnessNodeInstanceStatus.READY
        or not isinstance(definition, HarnessControlNode)
        or definition.wait is None
    ):
        raise HarnessValidationError(
            "Wait registration targets an incompatible node state",
            code="graph_wait_node_state_mismatch",
        )
    raw_registration = decision.payload.get("registration")
    if not isinstance(raw_registration, Mapping):
        raise HarnessValidationError(
            "Wait decision lacks its resolved registration",
            code="graph_wait_registration_missing",
        )
    expected_fields = {
        "wait_id",
        "kind",
        "correlation_ref",
        "tenant_scope_ref",
        "identity_scope_ref",
        "signal_schema_ref",
        "deadline_ref",
        "resolved",
    }
    if definition.wait.resume_node_id is not None:
        expected_fields.add("approval_id")
    if set(raw_registration) != expected_fields:
        raise HarnessValidationError(
            "Wait registration fields do not match the durable contract",
            code="graph_wait_registration_invalid",
            details={
                "missing": sorted(expected_fields.difference(raw_registration)),
                "unknown": sorted(set(raw_registration).difference(expected_fields)),
            },
        )
    if raw_registration["resolved"] is not True:
        raise HarnessValidationError(
            "Wait registration sources were not resolved from accepted graph data",
            code="wait_registration_source_missing",
        )
    scope = HarnessWaitScope(
        wait_id=raw_registration["wait_id"],
        run_id=state.run_id,
        node_instance_id=node.instance_id,
        tenant_scope_ref=raw_registration["tenant_scope_ref"],
        identity_scope_ref=raw_registration["identity_scope_ref"],
        signal_schema_ref=raw_registration["signal_schema_ref"],
        correlation_ref=raw_registration["correlation_ref"],
    )
    record = HarnessWaitRegistrationRecord(
        scope=scope,
        kind=raw_registration["kind"],
        registered_sequence=decision_sequence,
        deadline_ref=raw_registration["deadline_ref"],
    )
    if (
        scope.wait_id != definition.wait.wait_id
        or record.kind is not definition.wait.kind
        or scope.signal_schema_ref
        != f"{definition.wait.signal_type}@{definition.wait.signal_version}"
    ):
        raise HarnessValidationError(
            "Wait registration does not match the pinned normalized contract",
            code="graph_wait_registration_contract_mismatch",
        )
    for field_name in ("tenant_scope_ref", "identity_scope_ref"):
        pinned_scope = state.metadata.get(field_name)
        if pinned_scope is not None and pinned_scope != getattr(scope, field_name):
            raise HarnessValidationError(
                "Wait registration conflicts with the authoritative run scope",
                code="graph_wait_registration_scope_mismatch",
                details={"field": field_name},
            )
    if any(item.node_instance_id == node.instance_id for item in state.wait_registrations):
        raise HarnessValidationError(
            "Wait node instance already has a registration",
            code="duplicate_wait_node_registration",
        )

    inbox = list(state.signal_inbox)
    wait_status = HarnessWaitStatus.REGISTERED
    resolution_ref = None
    if record.kind is WaitKind.SIGNAL:
        matching = tuple(
            entry
            for entry in inbox
            if entry.status is HarnessSignalInboxEntryStatus.EARLY
            and entry.signal.scope == scope
        )
        if matching:
            selected = min(
                matching,
                key=lambda item: (
                    item.signal.received_sequence,
                    item.signal.signal_id,
                    item.signal.signal_ref,
                ),
            )
            match = HarnessWaitSignalMatch(
                scope=scope,
                registration_ref=record.registration_ref,
                signal_ref=selected.signal.signal_ref,
                matched_sequence=projection_sequence,
            )
            inbox = [
                replace(
                    entry,
                    status=HarnessSignalInboxEntryStatus.MATCHED,
                    match=match,
                )
                if entry.signal.signal_ref == selected.signal.signal_ref
                else entry
                for entry in inbox
            ]
            wait_status = HarnessWaitStatus.RESUMED
            resolution_ref = selected.signal.signal_ref

    registration = HarnessWaitRegistration(
        wait_id=scope.wait_id,
        node_instance_id=node.instance_id,
        kind=record.kind,
        correlation_ref=scope.correlation_ref,
        tenant_scope_ref=scope.tenant_scope_ref,
        identity_scope_ref=scope.identity_scope_ref,
        signal_schema_ref=scope.signal_schema_ref,
        registered_sequence=decision_sequence,
        status=wait_status,
        deadline_ref=record.deadline_ref,
        resolution_event_ref=resolution_ref,
        last_event_sequence=projection_sequence,
        approval_id=raw_registration.get("approval_id"),
        resume_node_id=definition.wait.resume_node_id,
    )
    metadata = _merged_node_metadata(node, decision)
    metadata.update(
        {
            "wait_registration_ref": record.registration_ref,
            "wait_scope_ref": scope.scope_ref,
        }
    )
    updated = replace(
        node,
        status=HarnessNodeInstanceStatus.WAITING,
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    return replace(
        state,
        lifecycle=(
            RunLifecycle.RUNNING
            if state.lifecycle in {RunLifecycle.CREATED, RunLifecycle.WAITING}
            else state.lifecycle
        ),
        node_instances=_replace_node(state.node_instances, updated),
        wait_registrations=(*state.wait_registrations, registration),
        signal_inbox=tuple(inbox),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _resume_wait(
    state: HarnessGraphState,
    decision: HarnessGraphDecision,
    *,
    projection_sequence: int,
) -> HarnessGraphState:
    if decision.node_instance_id is None:
        raise HarnessValidationError(
            "Wait resume requires a node instance",
            code="graph_control_node_identity_missing",
        )
    node = _node_instance(state, decision.node_instance_id)
    if (
        node.node_kind is not HarnessGraphNodeKind.WAIT
        or node.status is not HarnessNodeInstanceStatus.WAITING
    ):
        raise HarnessValidationError(
            "Wait resume targets an incompatible node state",
            code="graph_wait_node_state_mismatch",
        )
    registration = next(
        (
            item
            for item in state.wait_registrations
            if item.node_instance_id == node.instance_id
        ),
        None,
    )
    if registration is None or registration.status is HarnessWaitStatus.REGISTERED:
        raise HarnessValidationError(
            "Wait resume requires durable resolution evidence",
            code="graph_wait_resolution_missing",
        )
    if registration.status is HarnessWaitStatus.TIMED_OUT:
        status = (
            HarnessNodeInstanceStatus.SUCCEEDED
            if decision.target_node_ids
            else HarnessNodeInstanceStatus.FAILED
        )
    elif registration.status is HarnessWaitStatus.RESUMED:
        status = HarnessNodeInstanceStatus.SUCCEEDED
    else:
        raise HarnessValidationError(
            "cancelled Wait requires a run halt decision",
            code="graph_wait_cancelled_requires_halt",
        )
    metadata = _merged_node_metadata(node, decision)
    resolution = decision.payload.get("resolution")
    if resolution not in {"resumed", "timed_out"}:
        raise HarnessValidationError(
            "Wait resume decision lacks a supported resolution",
            code="graph_wait_resolution_invalid",
        )
    metadata["wait_resolution"] = resolution
    updated = replace(
        node,
        status=status,
        error_code=(
            "wait_timed_out" if status is HarnessNodeInstanceStatus.FAILED else None
        ),
        terminal_reason=(
            "wait_timed_out" if status is HarnessNodeInstanceStatus.FAILED else None
        ),
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    return replace(
        state,
        lifecycle=RunLifecycle.RUNNING,
        node_instances=_replace_node(state.node_instances, updated),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _push_terminal_compensation_entry(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    *,
    decision_sequence: int,
    projection_sequence: int,
    side_effect_outcome_ref: str | None,
) -> HarnessGraphState:
    bindings = tuple(
        item for item in graph.compensation_refs if item.scope == "terminal_run"
    )
    if not bindings:
        return state
    if len(bindings) != 1:
        raise HarnessValidationError(
            "terminal side effect has ambiguous compensation bindings",
            code="ambiguous_terminal_compensation_binding",
        )
    if side_effect_outcome_ref is None:
        raise HarnessValidationError(
            "terminal compensation requires a durable side-effect outcome",
            code="graph_side_effect_outcome_missing",
        )
    raw_pending = state.metadata.get("pending_terminal_side_effect")
    if not isinstance(raw_pending, Mapping):
        raise HarnessValidationError(
            "terminal completion has no durable side-effect preparation",
            code="graph_side_effect_preparation_missing",
        )
    pending = HarnessPendingSideEffectState.from_dict(raw_pending)
    if (
        pending.scope is not HarnessPendingSideEffectScope.TERMINAL_RUN
        or pending.status
        is not HarnessPendingSideEffectStatus.OUTCOME_RECORDED
        or pending.outcome_ref != side_effect_outcome_ref
        or pending.observation_sequence is None
    ):
        raise HarnessValidationError(
            "terminal completion conflicts with its recorded side-effect outcome",
            code="graph_side_effect_preparation_mismatch",
        )
    binding = bindings[0]
    definition = _definition(graph, binding.for_node_id)
    policy = graph.terminal_policy
    if (
        not isinstance(definition, HarnessExecutableNode)
        or definition.side_effect_ref is None
        or policy is None
        or definition.side_effect_ref.exact_ref
        != f"{policy.handler.handler_id}@{policy.handler.version}"
    ):
        raise HarnessValidationError(
            "terminal compensation does not match its pinned forward handler",
            code="terminal_compensation_handler_mismatch",
        )
    origins = tuple(
        item
        for item in state.node_instances
        if item.instance_id == pending.node_instance_id
        and item.identity.node_id == binding.for_node_id
        and item.status is HarnessNodeInstanceStatus.SUCCEEDED
    )
    if not origins:
        raise HarnessValidationError(
            "terminal compensation has no verified origin node instance",
            code="terminal_compensation_origin_missing",
        )
    origin = max(
        origins,
        key=lambda item: (
            item.last_event_sequence,
            item.identity.activation_ordinal,
            item.instance_id,
        ),
    )
    entry_id = canonical_checksum(
        {
            "run_id": state.run_id,
            "scope": "terminal_run",
            "origin_node_instance_id": origin.instance_id,
            "effect_outcome_ref": side_effect_outcome_ref,
            "binding_id": binding.binding_id,
        }
    )
    if any(item.entry_id == entry_id for item in state.compensation_stack):
        raise HarnessValidationError(
            "terminal compensation entry already exists",
            code="duplicate_compensation_entry",
        )
    evidence = next(
        (
            item
            for item in origin.evidence_refs
            if item.kind is HarnessEvidenceKind.SIDE_EFFECT_OUTCOME
            and item.evidence_ref == side_effect_outcome_ref
            and item.event_sequence == pending.observation_sequence
            and item.contract_ref == definition.side_effect_ref
        ),
        None,
    )
    if evidence is None:
        raise HarnessValidationError(
            "terminal completion lacks accepted side-effect outcome observation",
            code="graph_decision_side_effect_evidence_missing",
        )
    metadata = thaw_json(origin.metadata)
    metadata["terminal_side_effect_outcome_ref"] = side_effect_outcome_ref
    metadata["terminal_compensation_binding_id"] = binding.binding_id
    updated_origin = replace(
        origin,
        evidence_refs=origin.evidence_refs,
        last_event_sequence=projection_sequence,
        metadata=metadata,
    )
    entry = HarnessCompensationEntry(
        entry_id=entry_id,
        origin_node_instance_id=origin.instance_id,
        effect_outcome_ref=side_effect_outcome_ref,
        effect_commit_sequence=pending.observation_sequence,
        handler_ref=binding.handler_ref,
        activity_ref=binding.activity_ref,
        idempotency_key=canonical_checksum(
            {
                "operation": "compensate",
                "scope": "terminal_run",
                "entry_id": entry_id,
                "handler_ref": binding.handler_ref.exact_ref,
            }
        ),
        fencing_generation=1,
        last_event_sequence=projection_sequence,
    )
    return replace(
        state,
        node_instances=_replace_node(state.node_instances, updated_origin),
        compensation_stack=(*state.compensation_stack, entry),
        last_event_sequence=projection_sequence,
        projection_checksum=None,
    )


def _apply_run_decision(
    state: HarnessGraphState,
    graph: NormalizedHarnessGraph,
    decision: HarnessGraphDecision,
    *,
    decision_sequence: int,
    projection_sequence: int,
    side_effect_outcome_ref: str | None,
) -> HarnessGraphState:
    if decision.decision_type is HarnessGraphDecisionType.PROJECT_RUN_WAITING:
        return replace(
            state,
            lifecycle=RunLifecycle.WAITING,
            last_event_sequence=projection_sequence,
            projection_checksum=None,
        )
    if decision.decision_type is HarnessGraphDecisionType.COMPLETE_RUN:
        outcome_value = decision.payload.get("outcome", RunOutcome.SUCCEEDED.value)
        outcome = RunOutcome(outcome_value)
        if outcome is RunOutcome.SUCCEEDED:
            state = _push_terminal_compensation_entry(
                state,
                graph,
                decision_sequence=decision_sequence,
                projection_sequence=projection_sequence,
                side_effect_outcome_ref=side_effect_outcome_ref,
            )
        state_metadata = thaw_json(state.metadata)
        raw_run_operation = state_metadata.get("pending_run_operation")
        state_metadata.pop("pending_terminal_side_effect", None)
        state_metadata.pop("pending_run_operation", None)
        node_instances = state.node_instances
        loop_counters = state.loop_counters
        wait_registrations = state.wait_registrations
        if outcome in {RunOutcome.CANCELLED, RunOutcome.FAILED} and any(
            not item.is_terminal for item in state.node_instances
        ):
            if state.active_activities:
                raise HarnessValidationError(
                    "run completion requires activity termination confirmation",
                    code="graph_activity_termination_unconfirmed",
                )
            node_instances = tuple(
                item
                if item.is_terminal
                else replace(
                    item,
                    status=(
                        HarnessNodeInstanceStatus.CANCELLED
                        if outcome is RunOutcome.CANCELLED
                        else HarnessNodeInstanceStatus.FAILED
                        if item.node_kind is HarnessGraphNodeKind.EXECUTABLE
                        else HarnessNodeInstanceStatus.HALTED
                    ),
                    step_status=(
                        (
                            HarnessStepStatus.HALTED
                            if outcome is RunOutcome.CANCELLED
                            else HarnessStepStatus.FAILED
                        )
                        if item.node_kind is HarnessGraphNodeKind.EXECUTABLE
                        else item.step_status
                    ),
                    error_code=decision.reason_code,
                    terminal_reason=decision.reason_code,
                    last_event_sequence=projection_sequence,
                )
                for item in state.node_instances
            )
        if outcome in {RunOutcome.CANCELLED, RunOutcome.FAILED}:
            loop_counters = tuple(
                item
                if item.status
                in {HarnessLoopStatus.EXITED, HarnessLoopStatus.EXHAUSTED}
                else replace(
                    item,
                    status=HarnessLoopStatus.EXITED,
                    last_event_sequence=projection_sequence,
                )
                for item in state.loop_counters
            )
        if outcome is RunOutcome.CANCELLED and any(
            item.unresolved for item in state.wait_registrations
        ):
            if not isinstance(raw_run_operation, Mapping):
                raise HarnessValidationError(
                    "run cancellation requires durable Wait resolution evidence",
                    code="graph_run_cancellation_wait_resolution_missing",
                )
            operation = HarnessGraphRunOperation.from_dict(raw_run_operation)
            if operation.operation_ref not in decision.evidence_refs:
                raise HarnessValidationError(
                    "run cancellation decision does not reference its operation",
                    code="graph_run_cancellation_wait_evidence_mismatch",
                )
            wait_registrations = tuple(
                replace(
                    item,
                    status=HarnessWaitStatus.CANCELLED,
                    resolution_event_ref=operation.operation_ref,
                    last_event_sequence=projection_sequence,
                )
                if item.unresolved
                else item
                for item in state.wait_registrations
            )
        return replace(
            state,
            lifecycle=RunLifecycle.COMPLETED,
            outcome=outcome,
            node_instances=node_instances,
            loop_counters=loop_counters,
            wait_registrations=wait_registrations,
            metadata=state_metadata,
            last_event_sequence=projection_sequence,
            terminal_reason_code=(
                decision.reason_code
                if outcome is not RunOutcome.SUCCEEDED
                else state.terminal_reason_code
            ),
            projection_checksum=None,
        )
    node_instances = state.node_instances
    compensation_stack = state.compensation_stack
    halted_compensation = False
    if decision.node_instance_id is not None:
        node = _node_instance(state, decision.node_instance_id)
        compensation_entry = (
            compensation_entry_for_node(state, node)
            if node.status is HarnessNodeInstanceStatus.COMPENSATING
            else None
        )
        has_active_activity = any(
            item.node_instance_id == node.instance_id
            for item in state.active_activities
        )
        if compensation_entry is not None:
            halted_compensation = True
            node = replace(
                node,
                status=HarnessNodeInstanceStatus.HALTED,
                step_status=HarnessStepStatus.HALTED,
                error_code=decision.reason_code,
                terminal_reason=decision.reason_code,
                last_event_sequence=projection_sequence,
                metadata=_merged_node_metadata(node, decision),
            )
            compensation_entry = replace(
                compensation_entry,
                status=HarnessCompensationStatus.INDETERMINATE,
                outcome_ref=decision.decision_checksum,
                last_event_sequence=projection_sequence,
            )
            compensation_stack = _replace_compensation_entry(
                compensation_stack,
                compensation_entry,
            )
        elif has_active_activity:
            node = replace(
                node,
                status=HarnessNodeInstanceStatus.CANCEL_REQUESTED,
                error_code=decision.reason_code,
                terminal_reason=decision.reason_code,
                last_event_sequence=projection_sequence,
            )
        elif node.node_kind is HarnessGraphNodeKind.EXECUTABLE:
            node = replace(
                node,
                status=HarnessNodeInstanceStatus.HALTED,
                step_status=HarnessStepStatus.HALTED,
                error_code=decision.reason_code,
                terminal_reason=decision.reason_code,
                last_event_sequence=projection_sequence,
            )
        else:
            node = replace(
                node,
                status=HarnessNodeInstanceStatus.HALTED,
                error_code=decision.reason_code,
                terminal_reason=decision.reason_code,
                last_event_sequence=projection_sequence,
            )
        node_instances = _replace_node(node_instances, node)
    requested_outcome = decision.payload.get("outcome")
    outcome = (
        RunOutcome.INDETERMINATE
        if halted_compensation
        else RunOutcome.NONE
        if requested_outcome is None
        else RunOutcome(requested_outcome)
    )
    evidence_ref = (
        decision.decision_checksum
        if halted_compensation
        else decision.evidence_refs[0]
        if outcome is RunOutcome.INDETERMINATE and decision.evidence_refs
        else None
    )
    metadata = thaw_json(state.metadata)
    if decision.payload.get("manual_intervention_required") is True:
        evidence_ref = decision.decision_checksum
        metadata["manual_intervention"] = {
            "required": True,
            "reason_code": decision.reason_code,
            "decision_ref": decision.decision_checksum,
            "evidence_refs": list(decision.evidence_refs),
        }
    elif halted_compensation:
        metadata["manual_intervention"] = {
            "required": True,
            "reason_code": decision.reason_code,
            "decision_ref": decision.decision_checksum,
            "evidence_refs": list(decision.evidence_refs),
        }
    return replace(
        state,
        lifecycle=RunLifecycle.HALTED,
        outcome=outcome,
        node_instances=node_instances,
        compensation_stack=compensation_stack,
        last_event_sequence=projection_sequence,
        terminal_reason_code=decision.reason_code,
        terminal_evidence_ref=evidence_ref,
        metadata=metadata,
        projection_checksum=None,
    )


def _consume_budgets(
    budgets: HarnessGraphBudgetState,
    consumptions: Mapping[str, int],
) -> HarnessGraphBudgetState:
    if not consumptions:
        return budgets
    updated = []
    for counter in budgets.counters:
        amount = consumptions.get(counter.name, 0)
        if amount:
            if counter.remaining < amount:
                raise HarnessValidationError(
                    "graph budget is exhausted",
                    code="graph_budget_exhausted",
                    details={
                        "name": counter.name,
                        "limit": counter.limit,
                        "used": counter.used,
                        "reserved": counter.reserved,
                        "requested": amount,
                    },
                )
            counter = replace(counter, used=counter.used + amount)
        updated.append(counter)
    missing = sorted(
        set(consumptions).difference(item.name for item in budgets.counters)
    )
    if missing:
        raise HarnessValidationError(
            "graph budget counter is missing",
            code="graph_budget_counter_missing",
            details={"names": missing},
        )
    return HarnessGraphBudgetState(tuple(updated))


def _validate_step_decision_source(
    node: HarnessNodeInstanceState,
    decision_type: HarnessGraphDecisionType,
) -> None:
    allowed = {
        HarnessGraphDecisionType.ENTER_STEP_PHASE: frozenset(
            {
                (HarnessNodeInstanceStatus.READY, HarnessStepStatus.PENDING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.REPLANNING),
                (
                    HarnessNodeInstanceStatus.COMPENSATING,
                    HarnessStepStatus.PENDING,
                ),
            }
        ),
        HarnessGraphDecisionType.DISPATCH_ACTIVITY: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.PLANNING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.PLAN_VERIFIED),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RETRYING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.PLANNING),
                (
                    HarnessNodeInstanceStatus.COMPENSATING,
                    HarnessStepStatus.PLAN_VERIFIED,
                ),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.RETRYING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.RUNNING),
            }
        ),
        HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RUNNING),
                (
                    HarnessNodeInstanceStatus.WAITING,
                    HarnessStepStatus.WAITING_APPROVAL,
                ),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.RUNNING),
            }
        ),
        HarnessGraphDecisionType.COMPLETE_NODE: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.VERIFYING),
            }
        ),
        HarnessGraphDecisionType.PREPARE_SIDE_EFFECT: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
            }
        ),
        HarnessGraphDecisionType.FAIL_NODE: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.VERIFYING),
            }
        ),
        HarnessGraphDecisionType.RETRY_NODE: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.VERIFYING),
            }
        ),
        HarnessGraphDecisionType.REPLAN_NODE: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.PLANNING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.PLANNING),
                (HarnessNodeInstanceStatus.COMPENSATING, HarnessStepStatus.VERIFYING),
            }
        ),
        HarnessGraphDecisionType.ROUTE_TO_REPAIR: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
            }
        ),
        HarnessGraphDecisionType.WAIT_NODE: frozenset(
            {
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.RUNNING),
                (HarnessNodeInstanceStatus.RUNNING, HarnessStepStatus.VERIFYING),
            }
        ),
    }[decision_type]
    current = (node.status, node.step_status)
    if current not in allowed:
        raise HarnessValidationError(
            "graph Step decision is incompatible with the current node phase",
            code="graph_step_decision_state_mismatch",
            details={
                "decision_type": decision_type.value,
                "node_status": node.status.value,
                "step_status": None
                if node.step_status is None
                else node.step_status.value,
            },
        )


def _decision_metadata(decision: HarnessGraphDecision) -> dict[str, Any]:
    metadata = {
        "last_decision_checksum": decision.decision_checksum,
        "last_reason_code": decision.reason_code,
        "last_decision_type": decision.decision_type.value,
        "decision_payload": thaw_json(decision.payload),
        "target_node_ids": list(decision.target_node_ids),
    }
    if decision.decision_type is HarnessGraphDecisionType.OPEN_FORK:
        branches = decision.payload.get("branches", ())
        if isinstance(branches, tuple):
            metadata["opened_branch_ids"] = [
                item.get("branch_id")
                for item in branches
                if isinstance(item, Mapping) and isinstance(item.get("branch_id"), str)
            ]
    if decision.decision_type is HarnessGraphDecisionType.SELECT_CHOICE:
        graph_candidate = decision.payload.get("graph_candidate", {})
        if isinstance(graph_candidate, Mapping):
            metadata["selected_branch_id"] = graph_candidate.get("branch_id")
    if decision.decision_type is HarnessGraphDecisionType.START_LOOP_ITERATION:
        metadata["selected_loop_route_id"] = "continue"
    elif decision.decision_type is HarnessGraphDecisionType.EXIT_LOOP:
        metadata["selected_loop_route_id"] = "exit"
    elif decision.decision_type is HarnessGraphDecisionType.EXHAUST_LOOP:
        metadata["selected_loop_route_id"] = "exhaustion"
    return metadata


def _merged_node_metadata(
    node: HarnessNodeInstanceState,
    decision: HarnessGraphDecision,
) -> dict[str, Any]:
    metadata = thaw_json(node.metadata)
    metadata.update(_decision_metadata(decision))
    return metadata


def _state_evidence_refs(state: HarnessGraphState) -> set[str]:
    refs = {canonical_checksum(item.to_dict()) for item in state.node_instances}
    for node in state.node_instances:
        refs.update(item.evidence_ref for item in node.evidence_refs)
        refs.update(_terminal_metadata_evidence_refs(node))
    for join in state.join_states:
        refs.update(join.terminal_event_refs.values())
    for wait in state.wait_registrations:
        if wait.resolution_event_ref is not None:
            refs.add(wait.resolution_event_ref)
    for signal in state.signal_inbox:
        refs.add(signal.signal.signal_ref)
        if signal.match is not None:
            refs.add(signal.match.match_ref)
    for entry in state.compensation_stack:
        refs.add(entry.effect_outcome_ref)
        if entry.outcome_ref is not None:
            refs.add(entry.outcome_ref)
    if state.terminal_evidence_ref is not None:
        refs.add(state.terminal_evidence_ref)
    return refs


def _terminal_metadata_evidence_refs(
    node: HarnessNodeInstanceState,
) -> tuple[str, ...]:
    if node.status is not HarnessNodeInstanceStatus.SUCCEEDED:
        return ()
    keys: tuple[str, ...] = ()
    if (
        node.node_kind is HarnessGraphNodeKind.EXECUTABLE
        and node.metadata.get("last_decision_type") == "complete_node"
    ):
        keys = ("last_decision_checksum", "side_effect_decision_ref")
    elif node.node_kind is HarnessGraphNodeKind.MERGE:
        keys = ("merge_result_ref", "merge_decision_ref")
    return tuple(
        value
        for key in keys
        if isinstance((value := node.metadata.get(key)), str)
        and _is_checksum_ref(value)
    )


def _definition(
    graph: NormalizedHarnessGraph,
    node_id: str,
) -> HarnessGraphNode:
    definition = next((item for item in graph.nodes if item.node_id == node_id), None)
    if definition is None:
        raise HarnessValidationError(
            "graph node definition is missing",
            code="graph_control_decision_mismatch",
            details={"node_id": node_id},
        )
    return definition


def _graph_observation_contracts(
    observation: HarnessAcceptedGraphObservation,
    definition: HarnessGraphNode,
    graph: NormalizedHarnessGraph,
) -> tuple:
    if observation.observation_type is HarnessGraphObservationType.WAIT_CAUSE:
        if (
            isinstance(definition, HarnessControlNode)
            and definition.wait is not None
        ):
            return (
                HarnessContractReference(
                    HarnessContractKind.WAIT,
                    definition.wait.signal_type,
                    definition.wait.signal_version,
                ),
            )
        if isinstance(definition, HarnessExecutableNode):
            return (
                HarnessContractReference(
                    HarnessContractKind.WAIT,
                    "approval",
                    "1",
                ),
            )
        return ()
    if observation.observation_type is HarnessGraphObservationType.MERGE_RESULT:
        if (
            isinstance(definition, HarnessControlNode)
            and definition.merge is not None
            and definition.merge.merge_ref is not None
        ):
            return (definition.merge.merge_ref,)
        return ()
    if not isinstance(definition, HarnessExecutableNode):
        return ()
    if observation.observation_type is HarnessGraphObservationType.VERIFIED_OUTPUT:
        return (definition.step_ref,)
    if observation.observation_type is HarnessGraphObservationType.WORKER_STATUS:
        return (definition.worker_ref,)
    if observation.observation_type is HarnessGraphObservationType.APPROVAL:
        return (definition.step_ref,)
    if observation.observation_type in {
        HarnessGraphObservationType.SIDE_EFFECT_OUTCOME,
        HarnessGraphObservationType.SIDE_EFFECT_FAILURE,
    }:
        references = []
        if definition.side_effect_ref is not None:
            references.append(definition.side_effect_ref)
        if graph.terminal_policy is not None:
            references.append(
                HarnessContractReference(
                    HarnessContractKind.SIDE_EFFECT,
                    graph.terminal_policy.handler.handler_id,
                    graph.terminal_policy.handler.version,
                )
            )
        return tuple(references)
    if observation.contract_ref.contract_kind is not HarnessContractKind.GATE:
        return ()
    # Framework mandatory plan/verify Gates are pinned by the owning
    # HarnessControlPlane even though they are not authored as domain Gate
    # refs on the executable node. The application layer still requires an
    # exact Gate reference and never evaluates or selects it itself.
    return (*definition.gate_refs, observation.contract_ref)


def _graph_observation_logical_identity(
    observation: HarnessAcceptedGraphObservation,
) -> tuple[str, int, str, str]:
    return (
        observation.node_instance_id,
        observation.attempt,
        observation.observation_type.value,
        (
            observation.contract_ref.exact_ref
            if observation.observation_type
            in {
                HarnessGraphObservationType.GATE_RESULT,
                HarnessGraphObservationType.MERGE_RESULT,
            }
            else ""
        ),
    )


def _node_instance(
    state: HarnessGraphState,
    node_instance_id: str,
) -> HarnessNodeInstanceState:
    node = next(
        (item for item in state.node_instances if item.instance_id == node_instance_id),
        None,
    )
    if node is None:
        raise HarnessValidationError(
            "graph node instance is missing",
            code="graph_control_decision_mismatch",
            details={"node_instance_id": node_instance_id},
        )
    return node


def _replace_node(
    nodes: tuple[HarnessNodeInstanceState, ...],
    updated: HarnessNodeInstanceState,
) -> tuple[HarnessNodeInstanceState, ...]:
    return tuple(
        updated if item.instance_id == updated.instance_id else item for item in nodes
    )


def _replace_compensation_entry(
    entries: tuple[HarnessCompensationEntry, ...],
    updated: HarnessCompensationEntry,
) -> tuple[HarnessCompensationEntry, ...]:
    return tuple(
        updated if item.entry_id == updated.entry_id else item for item in entries
    )


def _projection_for_cause(
    recovery: HarnessGraphRecovery,
    cause_checksum: str,
) -> HarnessGraphProjectionCommit | None:
    return next(
        (
            item
            for item in recovery.projection_commits
            if item.cause_checksum == cause_checksum
        ),
        None,
    )


def _run_spec_checksum_from_state(state: HarnessGraphState) -> str:
    value = state.metadata.get("run_spec_checksum")
    if not isinstance(value, str):
        raise EventIncompleteHistoryError(
            "graph state is missing its run spec checksum"
        )
    return value


def _runtime_scope_metadata(port: HarnessGraphTransitionPort) -> Mapping[str, Any]:
    provider = getattr(port, "graph_scope_metadata", None)
    if provider is None:
        return {}
    if not callable(provider):
        raise HarnessValidationError(
            "graph transition scope provider is invalid",
            code="graph_runtime_scope_mismatch",
        )
    value = provider()
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            "graph transition scope provider returned invalid metadata",
            code="graph_runtime_scope_mismatch",
        )
    return value


def _freeze_counter_delta(
    value: Mapping[str, Any], field_name: str
) -> Mapping[str, Any]:
    frozen = freeze_json(value, field_name)
    if not isinstance(frozen, Mapping):
        raise HarnessValidationError(
            f"{field_name} must be an object",
            code="invalid_graph_budget_delta",
        )
    return frozen


def _checksum_tuple(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(required_text(item, field_name) for item in values)
    if len(normalized) != len(set(normalized)):
        raise HarnessValidationError(
            f"{field_name} must not contain duplicates",
            code="duplicate_graph_control_evidence",
        )
    return normalized


def _is_checksum_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise HarnessValidationError(
            f"{field_name} must be a positive integer",
            code="invalid_graph_control_sequence",
        )
    return value


__all__ = [
    "HarnessGraphActivityCancellationDispatcherPort",
    "HarnessGraphActivityCancellationRequest",
    "HarnessGraphConcurrentActivityDispatcherPort",
    "HarnessGraphActivityDispatcherPort",
    "HarnessGraphAppliedDecision",
    "HarnessGraphControlPlaneRuntime",
    "HarnessGraphDecisionApplier",
]
