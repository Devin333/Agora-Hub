from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from framework.harness.control_plane.compensation_runtime import (
    compensation_binding_versions,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.decision import (
    HarnessGraphDecision,
    HarnessGraphDecisionType,
)
from framework.harness.control_plane.graph_evaluator import (
    GraphEvaluation,
    HarnessGraphCandidate,
    HarnessGraphCandidateType,
    HarnessGraphEvaluationContext,
    HarnessGraphEvaluator,
)
from framework.harness.graph.operations import HarnessGraphRunOperation
from framework.harness.control_plane.graph_state import (
    HarnessEvidenceKind,
    HarnessGraphState,
    HarnessNodeInstanceState,
    HarnessNodeInstanceStatus,
    HarnessPendingSideEffectState,
    HarnessPendingSideEffectStatus,
    RunLifecycle,
)
from framework.harness.control_plane.policy import HarnessBudgetSnapshot
from framework.harness.control_plane.state import HarnessStepStatus
from framework.harness.control_plane.step_lifecycle import (
    StepLifecycleBindingMode,
    StepLifecycleBudget,
    StepLifecycleObservations,
    StepLifecycleStateMachine,
    StepLifecycleTransition,
    StepLifecycleTransitionType,
)
from framework.harness.graph.canonical import (
    canonical_checksum,
    freeze_json,
    required_text,
    thaw_json,
)
from framework.harness.graph.model import (
    HarnessContractKind,
    HarnessContractReference,
    HarnessControlNode,
    HarnessExecutableNode,
    HarnessGraphEdgeKind,
    HarnessGraphNode,
    HarnessGraphNodeKind,
    NormalizedHarnessGraph,
)
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.graph.activity import (
    HarnessRetryPolicy,
    HarnessStepSpec,
)
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_WORKER_ACTIVITY_SCHEMA,
)


_STEP_GRAPH_DECISION_TYPES = MappingProxyType(
    {
        StepLifecycleTransitionType.PLAN_STEP: HarnessGraphDecisionType.ENTER_STEP_PHASE,
        StepLifecycleTransitionType.EXECUTE_STEP: HarnessGraphDecisionType.DISPATCH_ACTIVITY,
        StepLifecycleTransitionType.VERIFY_STEP: HarnessGraphDecisionType.VERIFY_ACTIVITY_RESULT,
        StepLifecycleTransitionType.COMPLETE_STEP: HarnessGraphDecisionType.COMPLETE_NODE,
        StepLifecycleTransitionType.RETRY_STEP: HarnessGraphDecisionType.RETRY_NODE,
        StepLifecycleTransitionType.REPLAN_STEP: HarnessGraphDecisionType.REPLAN_NODE,
        StepLifecycleTransitionType.ROUTE_TO_REPAIR: HarnessGraphDecisionType.ROUTE_TO_REPAIR,
        StepLifecycleTransitionType.WAIT_FOR_APPROVAL: HarnessGraphDecisionType.WAIT_NODE,
        # A worker-reported block has no Graph Wait registration. Treat it as a
        # terminal safety halt; Graph waits are represented only by Wait nodes.
        StepLifecycleTransitionType.BLOCK_STEP: HarnessGraphDecisionType.HALT_RUN,
        StepLifecycleTransitionType.FAIL_STEP: HarnessGraphDecisionType.FAIL_NODE,
        StepLifecycleTransitionType.HALT_STEP: HarnessGraphDecisionType.HALT_RUN,
    }
)
_GRAPH_RECONCILIATION_TYPES = frozenset(
    {
        HarnessGraphCandidateType.SELECT_PARALLEL_WINNER,
        HarnessGraphCandidateType.SCHEDULE_COMPENSATION,
    }
)
_CANDIDATE_ALLOWED_NODE_KINDS = MappingProxyType(
    {
        HarnessGraphCandidateType.COMPLETE_CONTROL_NODE: frozenset(
            {
                HarnessGraphNodeKind.CHOICE_JOIN,
                HarnessGraphNodeKind.LOOP_JOIN,
                HarnessGraphNodeKind.TERMINAL,
            }
        ),
        HarnessGraphCandidateType.SELECT_CHOICE: frozenset(
            {HarnessGraphNodeKind.CHOICE}
        ),
        HarnessGraphCandidateType.OPEN_FORK: frozenset(
            {HarnessGraphNodeKind.FORK_ALL, HarnessGraphNodeKind.FORK_ANY}
        ),
        HarnessGraphCandidateType.SATISFY_JOIN: frozenset(
            {HarnessGraphNodeKind.JOIN_ALL, HarnessGraphNodeKind.JOIN_ANY}
        ),
        HarnessGraphCandidateType.FAIL_JOIN: frozenset(
            {HarnessGraphNodeKind.JOIN_ALL, HarnessGraphNodeKind.JOIN_ANY}
        ),
        HarnessGraphCandidateType.SELECT_PARALLEL_WINNER: frozenset(
            {HarnessGraphNodeKind.JOIN_ANY}
        ),
        HarnessGraphCandidateType.REQUEST_BRANCH_CANCEL: frozenset(
            HarnessGraphNodeKind
        ),
        HarnessGraphCandidateType.START_LOOP_ITERATION: frozenset(
            {HarnessGraphNodeKind.LOOP_GUARD}
        ),
        HarnessGraphCandidateType.EXIT_LOOP: frozenset(
            {HarnessGraphNodeKind.LOOP_GUARD}
        ),
        HarnessGraphCandidateType.EXHAUST_LOOP: frozenset(
            {HarnessGraphNodeKind.LOOP_GUARD}
        ),
        HarnessGraphCandidateType.REGISTER_WAIT: frozenset({HarnessGraphNodeKind.WAIT}),
        HarnessGraphCandidateType.RESUME_WAIT: frozenset({HarnessGraphNodeKind.WAIT}),
        HarnessGraphCandidateType.APPLY_MERGE: frozenset({HarnessGraphNodeKind.MERGE}),
        HarnessGraphCandidateType.SCHEDULE_COMPENSATION: frozenset(
            {HarnessGraphNodeKind.EXECUTABLE}
        ),
    }
)
_GLOBAL_GRAPH_CANDIDATE_TYPES = frozenset(
    {
        HarnessGraphCandidateType.PROJECT_RUN_WAITING,
        HarnessGraphCandidateType.COMPLETE_RUN,
    }
)
_ARBITRATION_RECONCILIATION = 0
_ARBITRATION_SAFETY = 1
_ARBITRATION_CONTROL_ACTIVATION = 2
_ARBITRATION_STEP = 3
_ARBITRATION_GRAPH_CONTROL = 4
_ARBITRATION_ACTIVATION = 5
_ARBITRATION_WAITING = 6
_ARBITRATION_COMPLETION = 7
_STEP_LIFECYCLE_NODE_STATUSES = frozenset(
    {
        HarnessNodeInstanceStatus.READY,
        HarnessNodeInstanceStatus.RUNNING,
        HarnessNodeInstanceStatus.COMPENSATING,
    }
)


@dataclass(frozen=True, slots=True)
class HarnessGraphStepSchedulingInput:
    """Immutable, canonical input for one active executable node instance."""

    node_instance_id: str
    step: HarnessStepSpec = field(compare=False, repr=False)
    observations: StepLifecycleObservations
    budget: StepLifecycleBudget | HarnessBudgetSnapshot
    step_projection: Mapping[str, Any] = field(init=False, repr=False)
    control_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        node_instance_id = required_text(
            self.node_instance_id,
            "graph_step_scheduling_input.node_instance_id",
        )
        if not isinstance(self.step, HarnessStepSpec):
            raise TypeError("step must be HarnessStepSpec")
        if not isinstance(self.observations, StepLifecycleObservations):
            raise TypeError("observations must be StepLifecycleObservations")
        if self.observations.binding_mode is not StepLifecycleBindingMode.GRAPH_BOUND:
            raise HarnessValidationError(
                "Graph scheduling requires graph-bound Step observations",
                code="step_lifecycle_binding_mode_mismatch",
            )
        if self.observations.node_instance_id != node_instance_id:
            raise HarnessValidationError(
                "Step scheduling input belongs to another node instance",
                code="cross_node_step_observation_rejected",
            )
        if isinstance(self.budget, HarnessBudgetSnapshot):
            budget = StepLifecycleBudget.from_snapshot(self.budget)
        elif isinstance(self.budget, StepLifecycleBudget):
            budget = self.budget
        else:
            raise TypeError(
                "budget must be StepLifecycleBudget or HarnessBudgetSnapshot"
            )
        step_projection = freeze_json(
            self.step.to_dict(),
            "graph_step_scheduling_input.step",
        )
        if not isinstance(step_projection, Mapping):
            raise HarnessValidationError(
                "Step scheduling input must have a canonical object projection",
                code="invalid_graph_step_scheduling_input",
            )
        normalized_step = _step_from_projection(step_projection)
        control_projection = {
            "node_instance_id": node_instance_id,
            "step": thaw_json(step_projection),
            "observations": self.observations.control_projection(),
            "budget": budget.to_dict(),
        }
        object.__setattr__(self, "node_instance_id", node_instance_id)
        object.__setattr__(self, "step", normalized_step)
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "step_projection", step_projection)
        object.__setattr__(
            self,
            "control_checksum",
            canonical_checksum(control_projection),
        )

    def control_projection(self) -> dict[str, Any]:
        return {
            "node_instance_id": self.node_instance_id,
            "step": thaw_json(self.step_projection),
            "observations": self.observations.control_projection(),
            "budget": self.budget.to_dict(),
            "control_checksum": self.control_checksum,
        }


@dataclass(frozen=True, slots=True)
class _SchedulingOption:
    arbitration_rank: int
    stable_key: tuple[Any, ...]
    decision: HarnessGraphDecision


class HarnessScheduler:
    __slots__ = ("_graph_evaluator", "_step_state_machine", "_task_plan_scheduler")

    def __init__(
        self,
        *,
        graph_evaluator: HarnessGraphEvaluator | None = None,
        step_state_machine: StepLifecycleStateMachine | None = None,
        task_plan_scheduler: Any | None = None,
    ) -> None:
        from framework.harness.task_plan.scheduler import TaskPlanScheduler

        self._graph_evaluator = graph_evaluator or HarnessGraphEvaluator()
        self._step_state_machine = step_state_machine or StepLifecycleStateMachine()
        if task_plan_scheduler is not None and not isinstance(
            task_plan_scheduler,
            TaskPlanScheduler,
        ):
            raise TypeError("task_plan_scheduler must be TaskPlanScheduler")
        self._task_plan_scheduler = task_plan_scheduler or TaskPlanScheduler()

    def next_task_plan_decision(
        self,
        projection: Any,
        max_count: int,
        *,
        plan: Any,
        policy: Any | None = None,
        worker_capacity: int | None = None,
        available_input_refs: tuple[str, ...] | Mapping[str, Any] = (),
    ) -> Any:
        """Return the only Control Plane-visible TaskPlan scheduling decision."""

        return self._task_plan_scheduler.next_ready_tasks(
            projection,
            max_count,
            plan=plan,
            policy=policy,
            worker_capacity=worker_capacity,
            available_input_refs=available_input_refs,
        )

    def reserve_task_plan_tasks(self, projection: Any, decision: Any) -> Any:
        return self._task_plan_scheduler.reserve_ready_tasks(projection, decision)

    def mark_task_plan_dispatched(self, projection: Any, instance: Any) -> Any:
        return self._task_plan_scheduler.mark_dispatched(projection, instance)

    def mark_task_plan_admitted(self, projection: Any, instance: Any) -> Any:
        return self._task_plan_scheduler.mark_admitted(projection, instance)

    def mark_task_plan_started(self, projection: Any, instance: Any) -> Any:
        return self._task_plan_scheduler.mark_started(projection, instance)

    def reclaim_task_plan_task(
        self,
        projection: Any,
        task_id: str,
        *,
        task_instance_id: str | None = None,
    ) -> Any:
        return self._task_plan_scheduler.reclaim_stale(
            projection,
            task_id,
            task_instance_id=task_instance_id,
        )

    def next_decision(
        self,
        state: HarnessGraphState,
        *,
        graph: NormalizedHarnessGraph | None = None,
        graph_context: HarnessGraphEvaluationContext | None = None,
        step_inputs: tuple[HarnessGraphStepSchedulingInput, ...] = (),
    ) -> HarnessGraphDecision | None:
        if not isinstance(state, HarnessGraphState):
            raise TypeError("state must be HarnessGraphState")
        if graph is None:
            raise HarnessValidationError(
                "Graph scheduling requires the pinned normalized graph",
                code="graph_scheduler_graph_missing",
            )
        return self._next_graph_decision(
            state,
            graph,
            graph_context=graph_context,
            step_inputs=step_inputs,
        )

    def _next_graph_decision(
        self,
        state: HarnessGraphState,
        graph: NormalizedHarnessGraph,
        *,
        graph_context: HarnessGraphEvaluationContext | None,
        step_inputs: tuple[HarnessGraphStepSchedulingInput, ...],
    ) -> HarnessGraphDecision | None:
        if not isinstance(graph, NormalizedHarnessGraph):
            raise TypeError("graph must be NormalizedHarnessGraph")
        context = graph_context or HarnessGraphEvaluationContext()
        if not isinstance(context, HarnessGraphEvaluationContext):
            raise TypeError("graph_context must be HarnessGraphEvaluationContext")
        normalized_inputs = _normalize_graph_step_inputs(step_inputs)
        inputs_by_instance = {item.node_instance_id: item for item in normalized_inputs}
        observation_checksum = canonical_checksum(
            {
                "graph_context": context.to_dict(),
                "step_inputs": [
                    item.control_projection() for item in normalized_inputs
                ],
            }
        )
        graph_ref = _graph_reference(graph)
        if state.graph_ref != graph_ref:
            raise HarnessValidationError(
                "Graph scheduler state is pinned to another graph identity",
                code="graph_scheduler_graph_mismatch",
                details={
                    "expected": graph_ref.to_dict(),
                    "actual": state.graph_ref.to_dict(),
                },
            )
        definitions = {node.node_id: node for node in graph.nodes}
        instances = {item.instance_id: item for item in state.node_instances}
        _validate_graph_state_bindings(state, definitions)
        if state.lifecycle in {RunLifecycle.COMPLETED, RunLifecycle.HALTED}:
            if normalized_inputs:
                raise HarnessValidationError(
                    "Terminal graph state cannot accept Step scheduling inputs",
                    code="unexpected_graph_step_scheduling_input",
                )
            return None
        evaluation = self._graph_evaluator.evaluate(
            graph,
            state,
            context=context,
        )
        _validate_graph_evaluation(
            evaluation,
            graph=graph,
            state=state,
            context=context,
            definitions=definitions,
            instances=instances,
        )
        options: list[_SchedulingOption] = []
        for candidate in evaluation.candidates:
            option = _graph_candidate_option(
                candidate,
                graph=graph,
                state=state,
                graph_ref=graph_ref,
                observation_checksum=observation_checksum,
                definitions=definitions,
                instances=instances,
            )
            if option is not None:
                options.append(option)
        run_operation_override = state.metadata.get("pending_run_operation") is not None
        terminal_observation_override = run_operation_override or any(
            candidate.candidate_type is HarnessGraphCandidateType.COMPLETE_RUN
            and candidate.reason_code
            in {"approval_cancelled", "side_effect_retry_exhausted"}
            for candidate in evaluation.candidates
        )
        compensation_mode = state.metadata.get("execution_mode") == "compensating"

        step_runnable_ids: set[str] = set()
        for node_state in state.node_instances:
            definition = definitions[node_state.identity.node_id]
            if not isinstance(definition, HarnessExecutableNode):
                continue
            if not _is_step_lifecycle_runnable(node_state):
                continue
            if (
                compensation_mode
                and node_state.status is not HarnessNodeInstanceStatus.COMPENSATING
            ):
                continue
            step_runnable_ids.add(node_state.instance_id)
            step_input = inputs_by_instance.get(node_state.instance_id)
            if terminal_observation_override:
                if step_input is not None:
                    _validate_graph_step_input(
                        graph,
                        definition,
                        node_state,
                        step_input,
                    )
                continue
            if step_input is None:
                options.append(
                    _missing_step_input_option(
                        graph_ref=graph_ref,
                        state=state,
                        node_state=node_state,
                        definition=definition,
                        observation_checksum=observation_checksum,
                    )
                )
                continue
            _validate_graph_step_input(
                graph,
                definition,
                node_state,
                step_input,
            )
            transition = self._step_state_machine.next_transition(
                step_input.step,
                node_state,
                step_input.observations,
                step_input.budget,
            )
            if transition is None or _dispatch_already_active(
                transition,
                node_state,
                state,
            ):
                continue
            if (
                transition.transition_type is StepLifecycleTransitionType.EXECUTE_STEP
                and not _physical_dispatch_capacity_available(state)
            ):
                continue
            option = _step_transition_option(
                transition,
                graph=graph,
                state=state,
                graph_ref=graph_ref,
                node_state=node_state,
                definition=definition,
                observation_checksum=observation_checksum,
            )
            if option is not None:
                options.append(option)

        extra_inputs = sorted(set(inputs_by_instance).difference(step_runnable_ids))
        if extra_inputs:
            raise HarnessValidationError(
                "Step scheduling inputs include non-runnable node instances",
                code="unexpected_graph_step_scheduling_input",
                details={"node_instance_ids": extra_inputs},
            )
        if not options:
            return None
        return min(
            options,
            key=lambda item: (
                item.arbitration_rank,
                item.stable_key,
                item.decision.decision_checksum,
            ),
        ).decision



def _is_step_lifecycle_runnable(node: HarnessNodeInstanceState) -> bool:
    if node.status in _STEP_LIFECYCLE_NODE_STATUSES:
        return True
    return (
        node.status is HarnessNodeInstanceStatus.WAITING
        and node.step_status is HarnessStepStatus.WAITING_APPROVAL
        and node.metadata.get("approval_granted") is True
        and any(
            evidence.kind is HarnessEvidenceKind.APPROVAL
            and evidence.attempt == node.attempt
            for evidence in node.evidence_refs
        )
    )


def _normalize_graph_step_inputs(
    values: Sequence[HarnessGraphStepSchedulingInput],
) -> tuple[HarnessGraphStepSchedulingInput, ...]:
    if not isinstance(values, tuple):
        raise TypeError("step_inputs must be an immutable tuple")
    if not all(isinstance(item, HarnessGraphStepSchedulingInput) for item in values):
        raise TypeError(
            "step_inputs must contain HarnessGraphStepSchedulingInput values"
        )
    normalized = tuple(
        sorted(
            values,
            key=lambda item: (item.node_instance_id, item.control_checksum),
        )
    )
    identities = [item.node_instance_id for item in normalized]
    if len(identities) != len(set(identities)):
        raise HarnessValidationError(
            "Step scheduling inputs must be unique per node instance",
            code="duplicate_graph_step_scheduling_input",
        )
    return normalized


def _graph_reference(graph: NormalizedHarnessGraph) -> HarnessGraphReference:
    return HarnessGraphReference.from_graph(graph)


def _validate_graph_state_bindings(
    state: HarnessGraphState,
    definitions: Mapping[str, HarnessGraphNode],
) -> None:
    for node_state in state.node_instances:
        node_id = node_state.identity.node_id
        definition = definitions.get(node_id)
        if definition is None:
            raise HarnessValidationError(
                "Graph state references an unknown node definition",
                code="graph_scheduler_state_identity_mismatch",
                details={
                    "node_id": node_id,
                    "node_instance_id": node_state.instance_id,
                },
            )
        mismatches: list[str] = []
        if node_state.node_kind is not definition.node_kind:
            mismatches.append("node_kind")
        if isinstance(definition, HarnessExecutableNode):
            if node_state.step_id != definition.step_id:
                mismatches.append("step_id")
            if node_state.step_ref != definition.step_ref:
                mismatches.append("step_ref")
        if mismatches:
            raise HarnessValidationError(
                "Graph state node binding does not match its pinned definition",
                code="graph_scheduler_state_identity_mismatch",
                details={
                    "node_id": node_id,
                    "node_instance_id": node_state.instance_id,
                    "mismatches": mismatches,
                },
            )


def _validate_graph_evaluation(
    evaluation: GraphEvaluation,
    *,
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    context: HarnessGraphEvaluationContext,
    definitions: Mapping[str, HarnessGraphNode],
    instances: Mapping[str, HarnessNodeInstanceState],
) -> None:
    if not isinstance(evaluation, GraphEvaluation):
        raise TypeError("graph evaluator must return GraphEvaluation")
    checksum_mismatches = {
        name: {"expected": expected, "actual": actual}
        for name, expected, actual in (
            ("graph", graph.checksum, evaluation.graph_checksum),
            ("state", state.projection_checksum, evaluation.state_checksum),
            ("context", context.checksum, evaluation.context_checksum),
        )
        if expected != actual
    }
    projection_mismatches = {
        name: {"expected": list(expected), "actual": list(actual)}
        for name, expected, actual in (
            (
                "ready",
                tuple(sorted(state.ready_node_ids)),
                evaluation.ready_node_instance_ids,
            ),
            (
                "running",
                tuple(sorted(state.running_node_ids)),
                evaluation.running_node_instance_ids,
            ),
            (
                "waiting",
                tuple(sorted(state.waiting_node_ids)),
                evaluation.waiting_node_instance_ids,
            ),
            (
                "terminal",
                tuple(sorted(state.terminal_node_ids)),
                evaluation.terminal_node_instance_ids,
            ),
        )
        if expected != actual
    }
    if checksum_mismatches or projection_mismatches:
        raise HarnessValidationError(
            "Graph evaluator output does not match the accepted scheduler input",
            code="graph_scheduler_evaluation_mismatch",
            details={
                "checksums": checksum_mismatches,
                "projections": projection_mismatches,
            },
        )
    accepted_evidence_refs = _accepted_graph_candidate_evidence_refs(state, context)
    graph_branch_ids = {
        branch_id
        for branch_id in (
            *(
                branch.branch_id
                for node in graph.nodes
                if isinstance(node, HarnessControlNode)
                for branch in node.branches
            ),
            *(edge.branch_id for edge in graph.edges if edge.branch_id is not None),
            *(
                branch_id
                for node in graph.nodes
                if isinstance(node, HarnessControlNode) and node.join is not None
                for branch_id in node.join.required_branch_ids
            ),
        )
    }
    for candidate in evaluation.candidates:
        unknown_evidence_refs = sorted(
            set(candidate.evidence_refs).difference(accepted_evidence_refs)
        )
        if unknown_evidence_refs:
            raise HarnessValidationError(
                "Graph candidate references evidence outside the accepted scheduler input",
                code="graph_scheduler_candidate_evidence_mismatch",
                details={
                    "candidate_checksum": candidate.candidate_checksum,
                    "candidate_type": candidate.candidate_type.value,
                    "reason_code": candidate.reason_code,
                    "node_id": candidate.node_id,
                    "node_instance_id": candidate.node_instance_id,
                    "unaccepted_evidence_refs": unknown_evidence_refs,
                },
            )
        definition = (
            None if candidate.node_id is None else definitions.get(candidate.node_id)
        )
        if candidate.node_id is not None and definition is None:
            raise HarnessValidationError(
                "Graph candidate references an unknown node definition",
                code="graph_scheduler_candidate_identity_mismatch",
                details={"node_id": candidate.node_id},
            )
        allowed_node_kinds = _CANDIDATE_ALLOWED_NODE_KINDS.get(candidate.candidate_type)
        if allowed_node_kinds is not None and (
            definition is None or definition.node_kind not in allowed_node_kinds
        ):
            raise HarnessValidationError(
                "Graph candidate type does not match its pinned node kind",
                code="graph_scheduler_candidate_kind_mismatch",
                details={
                    "candidate_type": candidate.candidate_type.value,
                    "node_id": candidate.node_id,
                    "node_kind": (
                        None if definition is None else definition.node_kind.value
                    ),
                    "allowed_node_kinds": sorted(
                        item.value for item in allowed_node_kinds
                    ),
                },
            )
        if candidate.candidate_type in _GLOBAL_GRAPH_CANDIDATE_TYPES and (
            candidate.node_id is not None
            or candidate.node_instance_id is not None
            or candidate.target_node_ids
            or candidate.branch_id is not None
        ):
            raise HarnessValidationError(
                "Run-level graph candidate cannot carry node routing identity",
                code="graph_scheduler_candidate_kind_mismatch",
                details={"candidate_type": candidate.candidate_type.value},
            )
        if candidate.node_instance_id is not None:
            instance = instances.get(candidate.node_instance_id)
            if instance is None or (
                candidate.node_id is not None
                and instance.identity.node_id != candidate.node_id
            ):
                raise HarnessValidationError(
                    "Graph candidate references an unknown or mismatched node instance",
                    code="graph_scheduler_candidate_identity_mismatch",
                    details={
                        "node_id": candidate.node_id,
                        "node_instance_id": candidate.node_instance_id,
                    },
                )
        unknown_targets = sorted(set(candidate.target_node_ids).difference(definitions))
        if unknown_targets:
            raise HarnessValidationError(
                "Graph candidate references unknown target definitions",
                code="graph_scheduler_candidate_identity_mismatch",
                details={"target_node_ids": unknown_targets},
            )
        if (
            candidate.branch_id is not None
            and candidate.branch_id not in graph_branch_ids
        ):
            raise HarnessValidationError(
                "Graph candidate references an unknown branch",
                code="graph_scheduler_candidate_identity_mismatch",
                details={"branch_id": candidate.branch_id},
            )


def _accepted_graph_candidate_evidence_refs(
    state: HarnessGraphState,
    context: HarnessGraphEvaluationContext,
) -> frozenset[str]:
    references = {
        canonical_checksum(node_state.to_dict()) for node_state in state.node_instances
    }
    for node_state in state.node_instances:
        for evidence in node_state.evidence_refs:
            references.add(evidence.evidence_ref)
        references.update(_terminal_metadata_evidence_refs(node_state))
    for join_state in state.join_states:
        references.update(join_state.terminal_event_refs.values())
    references.update(
        registration.resolution_event_ref
        for registration in state.wait_registrations
        if registration.resolution_event_ref is not None
    )
    for signal in state.signal_inbox:
        references.add(signal.signal.signal_ref)
        if signal.match is not None:
            references.add(signal.match.match_ref)
    for entry in state.compensation_stack:
        references.add(entry.effect_outcome_ref)
        if entry.outcome_ref is not None:
            references.add(entry.outcome_ref)
    if state.terminal_evidence_ref is not None:
        references.add(state.terminal_evidence_ref)
    pending_operation = state.metadata.get("pending_run_operation")
    if isinstance(pending_operation, Mapping):
        references.add(
            HarnessGraphRunOperation.from_dict(pending_operation).operation_ref
        )
    for observation in context.observations:
        references.add(observation.evidence_ref)
        references.add(observation.payload_ref)
    return frozenset(references)


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
        and _is_checksum_reference(value)
    )


def _is_checksum_reference(value: str) -> bool:
    if not value.startswith("sha256:"):
        return False
    digest = value.removeprefix("sha256:")
    return len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest
    )


def _graph_candidate_option(
    candidate: HarnessGraphCandidate,
    *,
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    graph_ref: HarnessGraphReference,
    observation_checksum: str,
    definitions: Mapping[str, HarnessGraphNode],
    instances: Mapping[str, HarnessNodeInstanceState],
) -> _SchedulingOption | None:
    rank = _graph_candidate_rank(candidate)
    definition = (
        None if candidate.node_id is None else definitions.get(candidate.node_id)
    )
    payload = thaw_json(candidate.payload)
    if not isinstance(payload, dict):
        raise HarnessValidationError(
            "Graph candidate payload must thaw to an object",
            code="invalid_graph_candidate_payload",
        )
    payload["graph_candidate"] = {
        "checksum": candidate.candidate_checksum,
        "priority": candidate.priority,
        "branch_id": candidate.branch_id,
    }
    decision_type = HarnessGraphDecisionType(candidate.candidate_type.value)
    reason_code = candidate.reason_code
    evidence_refs = candidate.evidence_refs
    if (
        candidate.candidate_type is HarnessGraphCandidateType.COMPLETE_RUN
        and candidate.payload.get("outcome") == "succeeded"
        and graph.terminal_policy_ref is not None
    ):
        pending = _pending_terminal_side_effect(state)
        if pending is None:
            decision_type = HarnessGraphDecisionType.PREPARE_SIDE_EFFECT
            reason_code = "terminal_side_effect_authorization_required"
            payload["side_effect_scope"] = "terminal_run"
            payload["completion_reason_code"] = candidate.reason_code
        elif pending.status is HarnessPendingSideEffectStatus.PREPARED:
            return None
        elif pending.status is HarnessPendingSideEffectStatus.FAILED:
            return None
        else:
            assert pending.outcome_ref is not None
            evidence_refs = (*evidence_refs, pending.outcome_ref)
            payload["side_effect_prepare_decision_ref"] = (
                pending.prepare_decision_ref
            )
            payload["side_effect_outcome_ref"] = pending.outcome_ref
    decision = HarnessGraphDecision(
        decision_type=decision_type,
        run_id=state.run_id,
        graph_ref=graph_ref,
        input_projection_checksum=state.projection_checksum,
        observation_checksum=observation_checksum,
        reason_code=reason_code,
        node_id=candidate.node_id,
        node_instance_id=candidate.node_instance_id,
        target_node_ids=candidate.target_node_ids,
        evidence_refs=evidence_refs,
        binding_versions=_candidate_binding_versions(
            graph,
            candidate,
            definition,
        ),
        payload=payload,
    )
    stable_key = _graph_candidate_stable_key(
        candidate,
        rank=rank,
        definition=definition,
        state=state,
        instances=instances,
    )
    return _SchedulingOption(
        rank,
        (stable_key[0], 1, stable_key[1:]),
        decision,
    )


def _graph_candidate_rank(candidate: HarnessGraphCandidate) -> int:
    if candidate.candidate_type in _GRAPH_RECONCILIATION_TYPES:
        return _ARBITRATION_RECONCILIATION
    if candidate.candidate_type is HarnessGraphCandidateType.HALT_RUN:
        return _ARBITRATION_SAFETY
    if candidate.candidate_type is HarnessGraphCandidateType.REQUEST_BRANCH_CANCEL:
        return _ARBITRATION_SAFETY
    if (
        candidate.candidate_type is HarnessGraphCandidateType.COMPLETE_RUN
        and candidate.reason_code
        in {"approval_cancelled", "side_effect_retry_exhausted"}
    ):
        return _ARBITRATION_SAFETY
    if (
        candidate.candidate_type is HarnessGraphCandidateType.COMPLETE_RUN
        and isinstance(candidate.payload.get("run_operation"), Mapping)
    ):
        return _ARBITRATION_SAFETY
    if candidate.candidate_type is HarnessGraphCandidateType.ACTIVATE_NODE:
        if candidate.reason_code == "committed_control_selection_ready":
            return _ARBITRATION_CONTROL_ACTIVATION
        return _ARBITRATION_ACTIVATION
    if candidate.candidate_type is HarnessGraphCandidateType.PROJECT_RUN_WAITING:
        return _ARBITRATION_WAITING
    if candidate.candidate_type is HarnessGraphCandidateType.COMPLETE_RUN:
        return _ARBITRATION_COMPLETION
    return _ARBITRATION_GRAPH_CONTROL


def _graph_candidate_stable_key(
    candidate: HarnessGraphCandidate,
    *,
    rank: int,
    definition: HarnessGraphNode | None,
    state: HarnessGraphState,
    instances: Mapping[str, HarnessNodeInstanceState],
) -> tuple[Any, ...]:
    instance = (
        None
        if candidate.node_instance_id is None
        else instances.get(candidate.node_instance_id)
    )
    activation_ordinal = (
        2**63 - 1 if instance is None else instance.identity.activation_ordinal
    )
    declaration_order = (
        2**63 - 1 if definition is None else definition.declaration_order
    )
    if rank == _ARBITRATION_RECONCILIATION:
        return (
            _graph_candidate_causal_sequence(candidate, state, instances),
            candidate.priority,
            activation_ordinal,
            declaration_order,
            candidate.sort_key,
        )
    if rank in {_ARBITRATION_CONTROL_ACTIVATION, _ARBITRATION_ACTIVATION}:
        return (declaration_order, candidate.sort_key)
    return (
        candidate.priority,
        activation_ordinal,
        declaration_order,
        candidate.sort_key,
    )


def _graph_candidate_causal_sequence(
    candidate: HarnessGraphCandidate,
    state: HarnessGraphState,
    instances: Mapping[str, HarnessNodeInstanceState],
) -> int:
    candidate_ids = (
        candidate.node_instance_id,
        candidate.payload.get("winner_node_instance_id"),
        candidate.payload.get("origin_node_instance_id"),
    )
    sequences = tuple(
        instances[item].last_event_sequence
        for item in candidate_ids
        if isinstance(item, str) and item in instances
    )
    return max(sequences) if sequences else state.last_event_sequence


def _candidate_binding_versions(
    graph: NormalizedHarnessGraph,
    candidate: HarnessGraphCandidate,
    definition: HarnessGraphNode | None,
) -> dict[str, str]:
    bindings: dict[str, str] = {}
    if isinstance(definition, HarnessExecutableNode):
        bindings.update(compensation_binding_versions(definition))
    elif isinstance(definition, HarnessControlNode):
        if definition.wait is not None and candidate.candidate_type in {
            HarnessGraphCandidateType.REGISTER_WAIT,
            HarnessGraphCandidateType.RESUME_WAIT,
        }:
            bindings["wait"] = (
                f"{definition.wait.signal_type}@{definition.wait.signal_version}"
            )
        merge_ref = None
        if definition.join is not None:
            merge_ref = definition.join.merge_ref
        if definition.merge is not None:
            merge_ref = definition.merge.merge_ref
        if merge_ref is not None:
            bindings["merge"] = merge_ref.exact_ref
    if candidate.candidate_type is HarnessGraphCandidateType.SCHEDULE_COMPENSATION:
        handler_payload = candidate.payload.get("handler_ref")
        activity_payload = candidate.payload.get("activity_ref")
        if not isinstance(handler_payload, Mapping) or not isinstance(
            activity_payload,
            Mapping,
        ):
            raise HarnessValidationError(
                "Compensation candidate is missing exact runtime bindings",
                code="graph_decision_binding_missing",
            )
        handler_ref = HarnessContractReference.from_dict(handler_payload)
        activity_ref = HarnessContractReference.from_dict(activity_payload)
        if (
            handler_ref.contract_kind is not HarnessContractKind.COMPENSATION
            or activity_ref.contract_kind is not HarnessContractKind.ACTIVITY
        ):
            raise HarnessValidationError(
                "Compensation candidate runtime binding kinds do not match",
                code="graph_decision_binding_mismatch",
            )
        bindings["compensation"] = handler_ref.exact_ref
        bindings["activity"] = activity_ref.exact_ref
    if (
        candidate.candidate_type is HarnessGraphCandidateType.COMPLETE_RUN
        and graph.terminal_policy_ref is not None
    ):
        bindings["terminal_policy"] = graph.terminal_policy_ref.exact_ref
    return bindings


def _step_transition_option(
    transition: StepLifecycleTransition,
    *,
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    graph_ref: HarnessGraphReference,
    node_state: HarnessNodeInstanceState,
    definition: HarnessExecutableNode,
    observation_checksum: str,
) -> _SchedulingOption | None:
    if transition.evidence_refs:
        rank = _ARBITRATION_RECONCILIATION
    elif transition.transition_type is StepLifecycleTransitionType.HALT_STEP:
        rank = _ARBITRATION_SAFETY
    else:
        rank = _ARBITRATION_STEP
    target_node_ids = _repair_target_node_ids(graph, definition, transition)
    decision_attempt = _step_decision_attempt(transition, node_state)
    decision_type = _STEP_GRAPH_DECISION_TYPES[transition.transition_type]
    reason_code = transition.reason_code
    evidence_refs = tuple(item.evidence_ref for item in transition.evidence_refs)
    payload = _step_transition_payload(
        transition,
        source_attempt=node_state.attempt,
        decision_attempt=decision_attempt,
    )
    pending: HarnessPendingSideEffectState | None = None
    if (
        transition.transition_type is StepLifecycleTransitionType.COMPLETE_STEP
        and definition.side_effect_ref is not None
        and node_state.status is not HarnessNodeInstanceStatus.COMPENSATING
    ):
        pending = _pending_node_side_effect(node_state)
        if pending is None:
            decision_type = HarnessGraphDecisionType.PREPARE_SIDE_EFFECT
            reason_code = "side_effect_authorization_required"
            payload["side_effect_scope"] = "node_instance"
            payload["completion_reason_code"] = transition.reason_code
        elif pending.status is HarnessPendingSideEffectStatus.PREPARED:
            return None
        elif pending.status is HarnessPendingSideEffectStatus.FAILED:
            return None
        else:
            assert pending.outcome_ref is not None
            evidence_refs = (*evidence_refs, pending.outcome_ref)
            payload["side_effect_prepare_decision_ref"] = (
                pending.prepare_decision_ref
            )
            payload["side_effect_outcome_ref"] = pending.outcome_ref
    decision = HarnessGraphDecision(
        decision_type=decision_type,
        run_id=state.run_id,
        graph_ref=graph_ref,
        input_projection_checksum=state.projection_checksum,
        observation_checksum=observation_checksum,
        reason_code=reason_code,
        node_id=definition.node_id,
        node_instance_id=node_state.instance_id,
        step_ref=definition.step_ref,
        attempt=decision_attempt,
        target_node_ids=target_node_ids,
        evidence_refs=evidence_refs,
        binding_versions=compensation_binding_versions(
            definition,
            state=state,
            node=node_state,
        ),
        payload=payload,
    )
    causal_sequence = (
        pending.observation_sequence
        if pending is not None and pending.observation_sequence is not None
        else max(item.event_sequence for item in transition.evidence_refs)
        if transition.evidence_refs
        else node_state.last_event_sequence
    )
    if rank == _ARBITRATION_RECONCILIATION:
        stable_key = (
            causal_sequence,
            0,
            (
                node_state.identity.activation_ordinal,
                node_state.instance_id,
                transition.transition_type.value,
                transition.reason_code,
            ),
        )
    else:
        stable_key = (
            node_state.identity.activation_ordinal,
            0,
            (
                node_state.instance_id,
                transition.transition_type.value,
                transition.reason_code,
                node_state.last_event_sequence,
            ),
        )
    return _SchedulingOption(rank, stable_key, decision)


def _pending_node_side_effect(
    node: HarnessNodeInstanceState,
) -> HarnessPendingSideEffectState | None:
    value = node.metadata.get("pending_side_effect")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            "pending node side effect is not a canonical object",
            code="invalid_pending_side_effect_state",
        )
    return HarnessPendingSideEffectState.from_dict(value)


def _pending_terminal_side_effect(
    state: HarnessGraphState,
) -> HarnessPendingSideEffectState | None:
    value = state.metadata.get("pending_terminal_side_effect")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise HarnessValidationError(
            "pending terminal side effect is not a canonical object",
            code="invalid_pending_side_effect_state",
        )
    return HarnessPendingSideEffectState.from_dict(value)


def _missing_step_input_option(
    *,
    graph_ref: HarnessGraphReference,
    state: HarnessGraphState,
    node_state: HarnessNodeInstanceState,
    definition: HarnessExecutableNode,
    observation_checksum: str,
) -> _SchedulingOption:
    decision = HarnessGraphDecision(
        decision_type=HarnessGraphDecisionType.HALT_RUN,
        run_id=state.run_id,
        graph_ref=graph_ref,
        input_projection_checksum=state.projection_checksum,
        observation_checksum=observation_checksum,
        reason_code="step_lifecycle_input_missing",
        node_id=definition.node_id,
        node_instance_id=node_state.instance_id,
        step_ref=definition.step_ref,
        attempt=node_state.attempt,
        binding_versions=compensation_binding_versions(
            definition,
            state=state,
            node=node_state,
        ),
        payload={"step_id": definition.step_id},
    )
    return _SchedulingOption(
        _ARBITRATION_SAFETY,
        (
            node_state.identity.activation_ordinal,
            0,
            (
                node_state.instance_id,
                "missing_step_input",
                decision.reason_code,
                node_state.last_event_sequence,
            ),
        ),
        decision,
    )


def _repair_target_node_ids(
    graph: NormalizedHarnessGraph,
    definition: HarnessExecutableNode,
    transition: StepLifecycleTransition,
) -> tuple[str, ...]:
    if transition.transition_type is not StepLifecycleTransitionType.ROUTE_TO_REPAIR:
        return ()
    definitions = {node.node_id: node for node in graph.nodes}
    targets = tuple(
        sorted(
            edge.target_id
            for edge in graph.edges
            if edge.source_id == definition.node_id
            and edge.edge_kind is HarnessGraphEdgeKind.REPAIR
            and isinstance(definitions.get(edge.target_id), HarnessExecutableNode)
            and definitions[edge.target_id].step_id == transition.target_step_id
        )
    )
    if len(targets) != 1:
        raise HarnessValidationError(
            "Step repair transition must resolve one exact graph target",
            code="graph_repair_target_mismatch",
            details={
                "node_id": definition.node_id,
                "target_step_id": transition.target_step_id,
                "target_node_ids": list(targets),
            },
        )
    return targets


def _step_transition_payload(
    transition: StepLifecycleTransition,
    *,
    source_attempt: int,
    decision_attempt: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step_transition_type": transition.transition_type.value,
        "reason": transition.reason,
        "source_attempt": source_attempt,
    }
    phase = {
        StepLifecycleTransitionType.PLAN_STEP: "plan",
        StepLifecycleTransitionType.EXECUTE_STEP: "execute",
        StepLifecycleTransitionType.VERIFY_STEP: "verify",
    }.get(transition.transition_type)
    if phase is not None:
        payload["phase"] = phase
    if transition.transition_type is StepLifecycleTransitionType.EXECUTE_STEP:
        payload["activity_attempt"] = decision_attempt
    if transition.target_step_id is not None:
        payload["target_step_id"] = transition.target_step_id
    for key in (
        "backoff_seconds",
        "budget_exhausted",
        "error",
        "error_type",
        "status",
    ):
        if key in transition.payload:
            payload[key] = transition.payload[key]
    return payload


def _step_decision_attempt(
    transition: StepLifecycleTransition,
    node_state: HarnessNodeInstanceState,
) -> int:
    if (
        transition.transition_type is StepLifecycleTransitionType.EXECUTE_STEP
        and node_state.step_status
        in {
            HarnessStepStatus.PLANNING,
            HarnessStepStatus.PLAN_VERIFIED,
            HarnessStepStatus.RETRYING,
        }
    ):
        return node_state.attempt + 1
    return node_state.attempt


def _dispatch_already_active(
    transition: StepLifecycleTransition,
    node_state: HarnessNodeInstanceState,
    state: HarnessGraphState,
) -> bool:
    return (
        transition.transition_type is StepLifecycleTransitionType.EXECUTE_STEP
        and any(
            item.node_instance_id == node_state.instance_id
            and item.attempt == node_state.attempt
            for item in state.active_activities
        )
    )


def _physical_dispatch_capacity_available(state: HarnessGraphState) -> bool:
    counter = state.budgets.get("max_parallelism")
    if counter is None:
        raise HarnessValidationError(
            "Graph scheduling requires a physical parallelism counter",
            code="graph_budget_counter_missing",
            details={"name": "max_parallelism"},
        )
    return len(state.active_activities) < counter.limit


def _validate_graph_step_input(
    graph: NormalizedHarnessGraph,
    definition: HarnessExecutableNode,
    node_state: HarnessNodeInstanceState,
    step_input: HarnessGraphStepSchedulingInput,
) -> None:
    mismatches: list[str] = []
    step = step_input.step
    if step_input.node_instance_id != node_state.instance_id:
        mismatches.append("node_instance_id")
    if node_state.step_id != definition.step_id or step.step_id != definition.step_id:
        mismatches.append("step_id")
    if node_state.step_ref != definition.step_ref:
        mismatches.append("state.step_ref")
    expected_step_ref = HarnessContractReference(
        HarnessContractKind.STEP,
        f"{graph.identity_ref.contract_id}:{step.step_id}",
        (
            graph.identity_version
            if graph.graph_ref is not None
            else str(step.metadata.get("step_version", "1"))
        ),
    )
    if expected_step_ref != definition.step_ref:
        mismatches.append("input.step_ref")
    graph_only = graph.schema_version == GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA
    expected_worker_ref = (
        definition.worker_ref
        if graph_only
        else HarnessContractReference(
            HarnessContractKind.WORKER,
            str(step.metadata.get("worker_id", step.step_id)),
            str(step.metadata.get("worker_version", "1")),
        )
    )
    if expected_worker_ref != definition.worker_ref:
        mismatches.append("worker_ref")
    expected_activity_ref = (
        definition.activity_ref if graph_only else _activity_reference(step)
    )
    if expected_activity_ref != definition.activity_ref:
        mismatches.append("activity_ref")
    if _gate_references(step) != definition.gate_refs:
        mismatches.append("gate_refs")
    expected_side_effect_ref = (
        None
        if step.side_effect_ref is None
        else HarnessContractReference(
            HarnessContractKind.SIDE_EFFECT,
            step.side_effect_ref.handler_id,
            step.side_effect_ref.version,
        )
    )
    if expected_side_effect_ref != definition.side_effect_ref:
        mismatches.append("side_effect_ref")
    if tuple(step.input_keys) != definition.input_keys:
        mismatches.append("input_keys")
    expected_outputs = () if step.output_key is None else (step.output_key,)
    if expected_outputs != definition.output_keys:
        mismatches.append("output_keys")
    if thaw_json(definition.metadata.get("step_metadata", {})) != thaw_json(
        step_input.step_projection.get("metadata", {})
    ):
        mismatches.append("step_metadata")
    if thaw_json(definition.metadata.get("retry_policy", {})) != thaw_json(
        step_input.step_projection.get("retry_policy", {})
    ):
        mismatches.append("retry_policy")
    if mismatches:
        raise HarnessValidationError(
            "Step scheduling input does not match its pinned graph definition",
            code="graph_step_scheduling_binding_mismatch",
            details={
                "node_id": definition.node_id,
                "node_instance_id": node_state.instance_id,
                "mismatches": sorted(set(mismatches)),
            },
        )


def _activity_reference(step: HarnessStepSpec) -> HarnessContractReference:
    value = str(
        step.metadata.get(
            "activity_contract_version",
            HARNESS_WORKER_ACTIVITY_SCHEMA,
        )
    ).strip()
    if value.count("@") == 1:
        contract_id, version = value.rsplit("@", maxsplit=1)
        return HarnessContractReference(
            HarnessContractKind.ACTIVITY,
            contract_id,
            version,
        )
    if "/v" in value:
        contract_id, version_number = value.rsplit("/v", maxsplit=1)
        if contract_id and version_number.isdigit() and int(version_number) > 0:
            return HarnessContractReference(
                HarnessContractKind.ACTIVITY,
                contract_id,
                f"v{version_number}",
            )
    raise HarnessValidationError(
        "Step activity reference must be exact",
        code="graph_step_scheduling_binding_mismatch",
    )


def _gate_references(
    step: HarnessStepSpec,
) -> tuple[HarnessContractReference, ...]:
    if step.quality_gate is None:
        return ()
    value = step.quality_gate.strip()
    if value.count("@") == 1:
        gate_id, version = value.rsplit("@", maxsplit=1)
    else:
        gate_id, version = value, "1"
    return (
        HarnessContractReference(
            HarnessContractKind.GATE,
            gate_id,
            version,
        ),
    )


def _step_from_projection(value: Mapping[str, Any]) -> HarnessStepSpec:
    retry_value = value.get("retry_policy")
    if not isinstance(retry_value, Mapping):
        raise HarnessValidationError(
            "Step scheduling input retry policy must be an object",
            code="invalid_graph_step_scheduling_input",
        )
    retry_policy = HarnessRetryPolicy(
        max_retries=retry_value.get("max_retries", 0),
        max_attempts=retry_value.get("max_attempts"),
        retry_on_statuses=tuple(retry_value.get("retry_on_statuses", ())),
        backoff_seconds=retry_value.get("backoff_seconds", 0.0),
        repair_step_id=retry_value.get("repair_step_id"),
        fail_fast_error_types=tuple(retry_value.get("fail_fast_error_types", ())),
    )
    metadata = thaw_json(value.get("metadata", {}))
    if not isinstance(metadata, dict):
        raise HarnessValidationError(
            "Step scheduling input metadata must be an object",
            code="invalid_graph_step_scheduling_input",
        )
    step = HarnessStepSpec(
        step_id=value.get("step_id"),
        worker_type=value.get("worker_type"),
        input_keys=tuple(value.get("input_keys", ())),
        output_key=value.get("output_key"),
        retry_policy=retry_policy,
        quality_gate=value.get("quality_gate"),
        metadata=metadata,
        side_effect_handler=value.get("side_effect_handler"),
    )
    object.__setattr__(
        step,
        "metadata",
        freeze_json(metadata, "graph_step_scheduling_input.step.metadata"),
    )
    return step




__all__ = ["HarnessGraphStepSchedulingInput", "HarnessScheduler"]
