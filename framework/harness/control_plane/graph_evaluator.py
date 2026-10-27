from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_state import (
    HarnessBranchOutputReference,
    HarnessCompensationStatus,
    HarnessEvidenceKind,
    HarnessGraphState,
    HarnessJoinKind,
    HarnessJoinState,
    HarnessJoinStatus,
    HarnessLoopIteration,
    HarnessLoopCounterState,
    HarnessNodeInstanceState,
    HarnessNodeInstanceStatus,
    HarnessWaitStatus,
    RunLifecycle,
)
from framework.harness.graph.operations import (
    HARNESS_GRAPH_RUN_OPERATION_CONTRACT_ID,
    HARNESS_GRAPH_RUN_OPERATION_CONTRACT_VERSION,
    HARNESS_GRAPH_RUN_OPERATION_NODE_ID,
    HarnessGraphRunOperation,
    HarnessGraphRunOperationType,
)
from framework.harness.control_plane.state import HarnessStepStatus
from framework.harness.graph.canonical import (
    canonical_checksum,
    exact_reference,
    freeze_json,
    required_text,
    thaw_json,
)
from framework.harness.graph.conditions import (
    ConditionAll,
    ConditionAny,
    ConditionPredicate,
    HarnessCondition,
    evaluate_condition,
    resolve_condition_path,
)
from framework.harness.graph.dsl import WaitKind, WaitTimeoutAction
from framework.harness.graph.model import (
    HarnessControlNode,
    HarnessContractKind,
    HarnessContractReference,
    HarnessGraphEdge,
    HarnessGraphEdgeKind,
    HarnessExecutableNode,
    HarnessGraphNode,
    HarnessGraphNodeKind,
    NormalizedHarnessGraph,
)
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.graph.versioning import HARNESS_GRAPH_EVALUATOR_VERSION
from framework.harness.waits.models import (
    HarnessWaitApprovalEvidenceRecord,
    HarnessWaitCancellationRecord,
    HarnessWaitCauseKind,
    HarnessWaitSignal,
    HarnessWaitTimeoutRecord,
    HarnessWaitTimerWakeRecord,
)


_CHECKSUM_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUCCESSFUL_NODE_STATUSES = frozenset(
    {
        HarnessNodeInstanceStatus.SUCCEEDED,
        HarnessNodeInstanceStatus.COMPENSATED,
    }
)
_FORWARD_DEPENDENCY_EDGE_KINDS = frozenset(
    {
        HarnessGraphEdgeKind.DEPENDENCY,
        HarnessGraphEdgeKind.WAIT_RESUME,
        HarnessGraphEdgeKind.WAIT_TIMEOUT,
    }
)


class HarnessGraphCandidateType(StrEnum):
    ACTIVATE_NODE = "activate_node"
    COMPLETE_CONTROL_NODE = "complete_control_node"
    SELECT_CHOICE = "select_choice"
    OPEN_FORK = "open_fork"
    SATISFY_JOIN = "satisfy_join"
    FAIL_JOIN = "fail_join"
    SELECT_PARALLEL_WINNER = "select_parallel_winner"
    REQUEST_BRANCH_CANCEL = "request_branch_cancel"
    START_LOOP_ITERATION = "start_loop_iteration"
    EXIT_LOOP = "exit_loop"
    EXHAUST_LOOP = "exhaust_loop"
    REGISTER_WAIT = "register_wait"
    RESUME_WAIT = "resume_wait"
    APPLY_MERGE = "apply_merge"
    SCHEDULE_COMPENSATION = "schedule_compensation"
    PROJECT_RUN_WAITING = "project_run_waiting"
    COMPLETE_RUN = "complete_run"
    HALT_RUN = "halt_run"


class HarnessGraphObservationType(StrEnum):
    VERIFIED_OUTPUT = "verified_output"
    WORKER_STATUS = "worker_status"
    QUALITY_VERDICT = "quality_verdict"
    GATE_RESULT = "gate_result"
    APPROVAL = "approval"
    SIDE_EFFECT_OUTCOME = "side_effect_outcome"
    SIDE_EFFECT_FAILURE = "side_effect_failure"
    MERGE_RESULT = "merge_result"
    WAIT_CAUSE = "wait_cause"
    RUN_OPERATION = "run_operation"


@dataclass(frozen=True, slots=True)
class HarnessAcceptedGraphObservation:
    observation_type: HarnessGraphObservationType | str
    node_id: str
    node_instance_id: str
    attempt: int
    event_sequence: int
    contract_ref: HarnessContractReference
    evidence_ref: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    control_fact_paths: tuple[str, ...] = ()
    payload_ref: str = field(init=False)
    observation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        observation_type = HarnessGraphObservationType(self.observation_type)
        node_id = required_text(self.node_id, "graph_observation.node_id")
        node_instance_id = required_text(
            self.node_instance_id,
            "graph_observation.node_instance_id",
        )
        minimum_attempt = (
            0
            if observation_type
            in {
                HarnessGraphObservationType.GATE_RESULT,
                HarnessGraphObservationType.MERGE_RESULT,
                HarnessGraphObservationType.WAIT_CAUSE,
                HarnessGraphObservationType.RUN_OPERATION,
            }
            else 1
        )
        if (
            not isinstance(self.attempt, int)
            or isinstance(self.attempt, bool)
            or self.attempt < minimum_attempt
        ):
            raise HarnessValidationError(
                "graph observation attempt is outside its allowed phase range",
                code="invalid_graph_observation_attempt",
            )
        if (
            not isinstance(self.event_sequence, int)
            or isinstance(self.event_sequence, bool)
            or self.event_sequence < 1
        ):
            raise HarnessValidationError(
                "graph observation event sequence must be positive",
                code="invalid_graph_observation_sequence",
            )
        if not isinstance(self.contract_ref, HarnessContractReference):
            raise TypeError(
                "graph observation contract_ref must be HarnessContractReference"
            )
        evidence_ref = _checksum(
            self.evidence_ref,
            "graph_observation.evidence_ref",
        )
        payload = freeze_json(self.payload, "graph_observation.payload")
        if not isinstance(payload, Mapping):
            raise HarnessValidationError(
                "graph observation payload must be an object",
                code="invalid_graph_observation_payload",
            )
        control_fact_paths = _control_fact_paths(self.control_fact_paths)
        if observation_type is HarnessGraphObservationType.VERIFIED_OUTPUT:
            if self.contract_ref.contract_kind is not HarnessContractKind.STEP:
                raise HarnessValidationError(
                    "verified output observation requires an exact Step reference",
                    code="graph_observation_contract_mismatch",
                )
            projected_payload = _project_control_fact_payload(
                thaw_json(payload),
                control_fact_paths,
            )
            if projected_payload != thaw_json(payload):
                raise HarnessValidationError(
                    "verified output observation contains undeclared control data",
                    code="undeclared_graph_control_fact",
                )
            payload = freeze_json(projected_payload, "graph_observation.payload")
        elif observation_type is HarnessGraphObservationType.WORKER_STATUS:
            if self.contract_ref.contract_kind is not HarnessContractKind.WORKER:
                raise HarnessValidationError(
                    "worker status observation requires an exact Worker reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_status_payload(payload)
        elif observation_type is HarnessGraphObservationType.QUALITY_VERDICT:
            if self.contract_ref.contract_kind is not HarnessContractKind.GATE:
                raise HarnessValidationError(
                    "quality verdict observation requires an exact Gate reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_quality_payload(payload)
        elif observation_type is HarnessGraphObservationType.GATE_RESULT:
            if self.contract_ref.contract_kind is not HarnessContractKind.GATE:
                raise HarnessValidationError(
                    "Gate result observation requires an exact Gate reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_gate_payload(payload)
        elif observation_type is HarnessGraphObservationType.APPROVAL:
            if self.contract_ref.contract_kind is not HarnessContractKind.STEP:
                raise HarnessValidationError(
                    "approval observation requires an exact Step reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_approval_payload(payload)
        elif observation_type is HarnessGraphObservationType.SIDE_EFFECT_OUTCOME:
            if self.contract_ref.contract_kind is not HarnessContractKind.SIDE_EFFECT:
                raise HarnessValidationError(
                    "side-effect outcome requires an exact handler reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_side_effect_outcome_payload(payload)
            if evidence_ref != payload["outcome_ref"]:
                raise HarnessValidationError(
                    "side-effect outcome evidence must equal its durable outcome reference",
                    code="graph_side_effect_outcome_evidence_mismatch",
                )
        elif observation_type is HarnessGraphObservationType.SIDE_EFFECT_FAILURE:
            if self.contract_ref.contract_kind is not HarnessContractKind.SIDE_EFFECT:
                raise HarnessValidationError(
                    "side-effect failure requires an exact handler reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_side_effect_failure_payload(payload)
        elif observation_type is HarnessGraphObservationType.MERGE_RESULT:
            if self.contract_ref.contract_kind is not HarnessContractKind.MERGE:
                raise HarnessValidationError(
                    "merge result requires an exact merge binding reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_merge_result_payload(payload)
        elif observation_type is HarnessGraphObservationType.WAIT_CAUSE:
            if self.contract_ref.contract_kind is not HarnessContractKind.WAIT:
                raise HarnessValidationError(
                    "Wait cause requires an exact Wait signal schema reference",
                    code="graph_observation_contract_mismatch",
                )
            _validate_wait_cause_payload(payload)
        elif observation_type is HarnessGraphObservationType.RUN_OPERATION:
            if (
                self.contract_ref.contract_kind
                is not HarnessContractKind.RUN_OPERATION
                or self.contract_ref.contract_id
                != HARNESS_GRAPH_RUN_OPERATION_CONTRACT_ID
                or self.contract_ref.version
                != HARNESS_GRAPH_RUN_OPERATION_CONTRACT_VERSION
            ):
                raise HarnessValidationError(
                    "run operation requires the exact Graph operation contract",
                    code="graph_observation_contract_mismatch",
                )
            operation = _validate_run_operation_payload(payload)
            if (
                node_id != HARNESS_GRAPH_RUN_OPERATION_NODE_ID
                or node_instance_id != operation.run_id
                or self.attempt != 0
            ):
                raise HarnessValidationError(
                    "run operation observation does not match its Graph run",
                    code="graph_run_operation_identity_mismatch",
                )
            if evidence_ref != operation.operation_ref:
                raise HarnessValidationError(
                    "run operation evidence does not match its canonical record",
                    code="graph_run_operation_evidence_mismatch",
                )
        if (
            observation_type is not HarnessGraphObservationType.VERIFIED_OUTPUT
            and control_fact_paths
        ):
            raise HarnessValidationError(
                "only verified output observations may declare control facts",
                code="graph_observation_control_fact_mismatch",
            )
        object.__setattr__(self, "observation_type", observation_type)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "node_instance_id", node_instance_id)
        object.__setattr__(self, "evidence_ref", evidence_ref)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(self, "control_fact_paths", control_fact_paths)
        object.__setattr__(
            self,
            "payload_ref",
            canonical_checksum(thaw_json(payload)),
        )
        object.__setattr__(
            self,
            "observation_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "observation_type": self.observation_type.value,
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
            "attempt": self.attempt,
            "event_sequence": self.event_sequence,
            "contract_ref": self.contract_ref.to_dict(),
            "evidence_ref": self.evidence_ref,
            "payload": thaw_json(self.payload),
            "control_fact_paths": list(self.control_fact_paths),
            "payload_ref": self.payload_ref,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "observation_checksum": self.observation_checksum,
        }


@dataclass(frozen=True, slots=True)
class HarnessGraphEvaluationContext:
    inputs: Mapping[str, Any] = field(default_factory=dict)
    observations: tuple[HarnessAcceptedGraphObservation, ...] = ()

    def __post_init__(self) -> None:
        inputs = freeze_json(self.inputs, "graph_context.inputs")
        if not isinstance(inputs, Mapping):
            raise HarnessValidationError(
                "graph evaluation context inputs must be an object",
                code="invalid_graph_evaluation_context",
            )
        observations = tuple(self.observations)
        if not all(
            isinstance(item, HarnessAcceptedGraphObservation) for item in observations
        ):
            raise TypeError(
                "graph evaluation observations must contain HarnessAcceptedGraphObservation values"
            )
        observations = tuple(
            sorted(
                observations,
                key=lambda item: (
                    item.event_sequence,
                    item.node_instance_id,
                    item.attempt,
                    item.observation_type.value,
                    item.contract_ref.exact_ref,
                    item.observation_checksum,
                ),
            )
        )
        if len(observations) != len(
            {item.observation_checksum for item in observations}
        ):
            raise HarnessValidationError(
                "graph observations must have unique checksums",
                code="duplicate_graph_observation",
            )
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "observations", observations)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": thaw_json(self.inputs),
            "observations": [item.to_dict() for item in self.observations],
        }

    @property
    def checksum(self) -> str:
        return canonical_checksum(self.to_dict())

    def for_node_instance(
        self,
        node_instance_id: str,
    ) -> tuple[HarnessAcceptedGraphObservation, ...]:
        return tuple(
            item
            for item in self.observations
            if item.node_instance_id == node_instance_id
        )


@dataclass(frozen=True, slots=True)
class HarnessGraphCandidate:
    candidate_type: HarnessGraphCandidateType | str
    reason_code: str
    priority: int
    node_id: str | None = None
    node_instance_id: str | None = None
    target_node_ids: tuple[str, ...] = ()
    branch_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    candidate_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        candidate_type = HarnessGraphCandidateType(self.candidate_type)
        reason_code = required_text(self.reason_code, "graph_candidate.reason_code")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise HarnessValidationError(
                "graph candidate priority must be an integer",
                code="invalid_graph_candidate_priority",
            )
        node_id = _optional_text(self.node_id, "graph_candidate.node_id")
        node_instance_id = _optional_text(
            self.node_instance_id,
            "graph_candidate.node_instance_id",
        )
        target_node_ids = _ordered_unique_text_tuple(
            self.target_node_ids,
            "graph_candidate.target_node_ids",
        )
        branch_id = _optional_text(self.branch_id, "graph_candidate.branch_id")
        evidence_refs = tuple(
            sorted(
                _checksum(item, "graph_candidate.evidence_refs")
                for item in self.evidence_refs
            )
        )
        if len(evidence_refs) != len(set(evidence_refs)):
            raise HarnessValidationError(
                "graph candidate evidence references must be unique",
                code="duplicate_graph_candidate_evidence",
            )
        payload = freeze_json(self.payload, "graph_candidate.payload")
        if not isinstance(payload, Mapping):
            raise HarnessValidationError(
                "graph candidate payload must be an object",
                code="invalid_graph_candidate_payload",
            )
        object.__setattr__(self, "candidate_type", candidate_type)
        object.__setattr__(self, "reason_code", reason_code)
        object.__setattr__(self, "node_id", node_id)
        object.__setattr__(self, "node_instance_id", node_instance_id)
        object.__setattr__(self, "target_node_ids", target_node_ids)
        object.__setattr__(self, "branch_id", branch_id)
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "payload", payload)
        object.__setattr__(
            self,
            "candidate_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.priority,
            "" if self.node_id is None else self.node_id,
            "" if self.node_instance_id is None else self.node_instance_id,
            self.candidate_type.value,
            "" if self.branch_id is None else self.branch_id,
            self.target_node_ids,
            self.candidate_checksum,
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "candidate_type": self.candidate_type.value,
            "reason_code": self.reason_code,
            "priority": self.priority,
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
            "target_node_ids": list(self.target_node_ids),
            "branch_id": self.branch_id,
            "evidence_refs": list(self.evidence_refs),
            "payload": thaw_json(self.payload),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "candidate_checksum": self.candidate_checksum,
        }


@dataclass(frozen=True, slots=True)
class GraphEvaluation:
    graph_checksum: str
    state_checksum: str
    context_checksum: str
    candidates: tuple[HarnessGraphCandidate, ...]
    ready_node_instance_ids: tuple[str, ...]
    running_node_instance_ids: tuple[str, ...]
    waiting_node_instance_ids: tuple[str, ...]
    terminal_node_instance_ids: tuple[str, ...]
    blocked_node_ids: tuple[str, ...]
    evaluator_version: str = HARNESS_GRAPH_EVALUATOR_VERSION
    evaluation_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "graph_checksum",
            _checksum(self.graph_checksum, "graph_evaluation.graph_checksum"),
        )
        object.__setattr__(
            self,
            "state_checksum",
            _checksum(self.state_checksum, "graph_evaluation.state_checksum"),
        )
        object.__setattr__(
            self,
            "context_checksum",
            _checksum(self.context_checksum, "graph_evaluation.context_checksum"),
        )
        raw_candidates = tuple(self.candidates)
        if not all(isinstance(item, HarnessGraphCandidate) for item in raw_candidates):
            raise TypeError("candidates must contain HarnessGraphCandidate values")
        candidates = tuple(sorted(raw_candidates, key=lambda item: item.sort_key))
        candidate_ids = [item.candidate_checksum for item in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise HarnessValidationError(
                "graph evaluation candidates must be unique",
                code="duplicate_graph_evaluation_candidate",
            )
        for field_name in (
            "ready_node_instance_ids",
            "running_node_instance_ids",
            "waiting_node_instance_ids",
            "terminal_node_instance_ids",
            "blocked_node_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _stable_text_tuple(
                    getattr(self, field_name), f"graph_evaluation.{field_name}"
                ),
            )
        if self.evaluator_version != HARNESS_GRAPH_EVALUATOR_VERSION:
            raise HarnessValidationError(
                "unsupported graph evaluator version",
                code="unsupported_graph_evaluator_version",
            )
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(
            self,
            "evaluation_checksum",
            canonical_checksum(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "evaluator_version": self.evaluator_version,
            "graph_checksum": self.graph_checksum,
            "state_checksum": self.state_checksum,
            "context_checksum": self.context_checksum,
            "candidates": [item.to_dict() for item in self.candidates],
            "ready_node_instance_ids": list(self.ready_node_instance_ids),
            "running_node_instance_ids": list(self.running_node_instance_ids),
            "waiting_node_instance_ids": list(self.waiting_node_instance_ids),
            "terminal_node_instance_ids": list(self.terminal_node_instance_ids),
            "blocked_node_ids": list(self.blocked_node_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "evaluation_checksum": self.evaluation_checksum,
        }


class HarnessGraphEvaluator:
    evaluator_version = HARNESS_GRAPH_EVALUATOR_VERSION

    def evaluate(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        *,
        context: HarnessGraphEvaluationContext | None = None,
    ) -> GraphEvaluation:
        if not isinstance(graph, NormalizedHarnessGraph):
            raise TypeError("graph must be NormalizedHarnessGraph")
        if not isinstance(state, HarnessGraphState):
            raise TypeError("state must be HarnessGraphState")
        accepted_context = context or HarnessGraphEvaluationContext()
        if not isinstance(accepted_context, HarnessGraphEvaluationContext):
            raise TypeError("context must be HarnessGraphEvaluationContext")
        expected_graph_ref = HarnessGraphReference.from_graph(graph)
        if state.graph_ref != expected_graph_ref:
            raise HarnessValidationError(
                "graph evaluator state is pinned to another graph identity",
                code="graph_evaluator_graph_mismatch",
                details={
                    "expected": expected_graph_ref.to_dict(),
                    "actual": state.graph_ref.to_dict(),
                },
            )

        nodes_by_id = {node.node_id: node for node in graph.nodes}
        _validate_accepted_observations(
            graph,
            state,
            accepted_context,
            nodes_by_id,
        )
        instances_by_definition = _instances_by_definition(state.node_instances)
        instances_by_scope = _instances_by_definition_scope(state.node_instances)
        candidates: list[HarnessGraphCandidate] = []

        pending_run_operation = _pending_run_operation(state)
        if pending_run_operation is not None:
            candidates.extend(_run_operation_candidates(state, pending_run_operation))
        else:
            terminal_observation = _terminal_observation_candidate(
                state,
                accepted_context,
            )
            if terminal_observation is not None:
                candidates.append(terminal_observation)

            if terminal_observation is None:
                candidates.extend(
                    self._compensation_candidates(
                        graph,
                        state,
                        nodes_by_id,
                    )
                )
            compensation_mode = _compensation_mode(state)
            if terminal_observation is None and not compensation_mode:
                candidates.extend(self._join_candidates(graph, state, nodes_by_id))
                candidates.extend(
                    self._control_candidates(
                        graph,
                        state,
                        accepted_context,
                        nodes_by_id,
                    )
                )
                candidates.extend(
                    self._activation_candidates(
                        graph,
                        state,
                        nodes_by_id,
                        instances_by_definition,
                        instances_by_scope,
                    )
                )

        if not candidates and pending_run_operation is None:
            completion = self._run_projection_candidate(graph, state)
            if completion is not None:
                candidates.append(completion)

        activated_definitions = {
            candidate.node_id
            for candidate in candidates
            if candidate.candidate_type is HarnessGraphCandidateType.ACTIVATE_NODE
            and candidate.node_id is not None
        }
        blocked = tuple(
            node_id
            for node_id in sorted(nodes_by_id)
            if node_id not in instances_by_definition
            and node_id not in activated_definitions
        )
        return GraphEvaluation(
            graph_checksum=graph.checksum,
            state_checksum=state.projection_checksum,
            context_checksum=accepted_context.checksum,
            candidates=tuple(candidates),
            ready_node_instance_ids=state.ready_node_ids,
            running_node_instance_ids=state.running_node_ids,
            waiting_node_instance_ids=state.waiting_node_ids,
            terminal_node_instance_ids=state.terminal_node_ids,
            blocked_node_ids=blocked,
        )

    def _control_candidates(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        context: HarnessGraphEvaluationContext,
        nodes_by_id: Mapping[str, HarnessGraphNode],
    ) -> tuple[HarnessGraphCandidate, ...]:
        candidates: list[HarnessGraphCandidate] = []
        waits_by_node = {
            node_instance_id: max(
                registrations,
                key=lambda item: (item.registered_sequence, item.wait_id),
            )
            for node_instance_id in {
                item.node_instance_id for item in state.wait_registrations
            }
            for registrations in [
                tuple(
                    item
                    for item in state.wait_registrations
                    if item.node_instance_id == node_instance_id
                )
            ]
        }
        for instance in state.node_instances:
            definition = nodes_by_id[instance.identity.node_id]
            if definition.node_kind is HarnessGraphNodeKind.EXECUTABLE:
                registration = waits_by_node.get(instance.instance_id)
                if (
                    registration is not None
                    and registration.kind is WaitKind.APPROVAL
                    and registration.status is HarnessWaitStatus.CANCELLED
                    and instance.status is HarnessNodeInstanceStatus.WAITING
                    and instance.step_status is HarnessStepStatus.WAITING_APPROVAL
                ):
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.COMPLETE_RUN,
                            "approval_cancelled",
                            50,
                            evidence_refs=(registration.resolution_event_ref,),
                            payload={
                                "outcome": "cancelled",
                                "wait_id": registration.wait_id,
                            },
                        )
                    )
                continue
            if definition.node_kind in {
                HarnessGraphNodeKind.JOIN_ALL,
                HarnessGraphNodeKind.JOIN_ANY,
            }:
                continue
            if definition.node_kind is HarnessGraphNodeKind.WAIT:
                candidate = self._wait_candidate(
                    graph,
                    state,
                    definition,
                    instance,
                    waits_by_node.get(instance.instance_id),
                    context,
                )
                if candidate is not None:
                    candidates.append(candidate)
                continue
            if instance.status is not HarnessNodeInstanceStatus.READY:
                continue
            if not isinstance(definition, HarnessControlNode):
                raise TypeError("control node definition must be HarnessControlNode")
            if definition.node_kind is HarnessGraphNodeKind.CHOICE:
                candidates.append(
                    self._choice_candidate(graph, state, definition, instance, context)
                )
            elif definition.node_kind is HarnessGraphNodeKind.CHOICE_JOIN:
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.COMPLETE_CONTROL_NODE,
                        "selected_choice_branch_join_ready",
                        30,
                        node_id=definition.node_id,
                        node_instance_id=instance.instance_id,
                    )
                )
            elif definition.node_kind is HarnessGraphNodeKind.LOOP_JOIN:
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.COMPLETE_CONTROL_NODE,
                        "selected_loop_route_join_ready",
                        30,
                        node_id=definition.node_id,
                        node_instance_id=instance.instance_id,
                    )
                )
            elif definition.node_kind in {
                HarnessGraphNodeKind.FORK_ALL,
                HarnessGraphNodeKind.FORK_ANY,
            }:
                candidates.append(self._fork_candidate(graph, definition, instance))
            elif definition.node_kind is HarnessGraphNodeKind.LOOP_GUARD:
                candidates.append(
                    self._loop_candidate(
                        graph,
                        definition,
                        instance,
                        state,
                        context,
                    )
                )
            elif definition.node_kind is HarnessGraphNodeKind.MERGE:
                candidates.append(
                    self._merge_candidate(
                        graph,
                        state,
                        definition,
                        instance,
                    )
                )
            elif definition.node_kind is HarnessGraphNodeKind.TERMINAL:
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.COMPLETE_CONTROL_NODE,
                        "terminal_control_node_ready",
                        30,
                        node_id=definition.node_id,
                        node_instance_id=instance.instance_id,
                    )
                )
        return tuple(candidates)

    def _merge_candidate(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        node: HarnessControlNode,
        instance: HarnessNodeInstanceState,
    ) -> HarnessGraphCandidate:
        if node.merge is None:
            raise HarnessValidationError(
                "Merge node is missing its normalized contract",
                code="graph_evaluator_merge_contract_missing",
            )
        join_instance, join_state, ordered = merge_branch_output_references(
            graph,
            state,
            node,
            branch_path=instance.identity.branch_path,
            iteration_vector=instance.identity.iteration_vector,
        )
        input_projection = [item.to_dict() for item in ordered]
        input_checksum = canonical_checksum(input_projection)
        merge_ref = (
            None if node.merge.merge_ref is None else node.merge.merge_ref.exact_ref
        )
        operation_id = canonical_checksum(
            {
                "run_id": state.run_id,
                "graph_checksum": graph.checksum,
                "merge_node_instance_id": instance.instance_id,
                "merge_ref": merge_ref,
                "input_checksum": input_checksum,
            }
        )
        payload: dict[str, Any] = {
            "merge": node.merge.to_dict(),
            "join_node_instance_id": join_instance.instance_id,
            "input_refs": input_projection,
            "input_checksum": input_checksum,
            "operation_id": operation_id,
        }
        aggregation_terminal_ref = None
        if node.merge.aggregation_node_id is not None:
            aggregation_instance = next(
                (
                    item
                    for item in state.node_instances
                    if item.identity.node_id == node.merge.aggregation_node_id
                    and item.identity.branch_path == instance.identity.branch_path
                    and item.identity.iteration_vector
                    == instance.identity.iteration_vector
                    and item.status is HarnessNodeInstanceStatus.SUCCEEDED
                ),
                None,
            )
            if aggregation_instance is None:
                raise HarnessValidationError(
                    "Merge marker lacks verified aggregation output",
                    code="graph_merge_aggregation_evidence_missing",
                )
            payload["aggregation_node_instance_id"] = aggregation_instance.instance_id
            aggregation_terminal_ref = _producer_completion_decision_ref(
                aggregation_instance,
                next(
                    item
                    for item in graph.nodes
                    if item.node_id == aggregation_instance.identity.node_id
                ),
            )
            payload["aggregation_terminal_ref"] = aggregation_terminal_ref
            all_output_refs = thaw_json(aggregation_instance.output_refs)
            payload["aggregation_output_refs"] = {
                key: all_output_refs[key]
                for key in node.merge.output_keys
                if key in all_output_refs
            }
        return HarnessGraphCandidate(
            HarnessGraphCandidateType.APPLY_MERGE,
            "merge_ready",
            30,
            node_id=node.node_id,
            node_instance_id=instance.instance_id,
            evidence_refs=tuple(
                sorted(
                    {
                        *(
                            join_state.terminal_event_refs[branch_id]
                            for branch_id in node.merge.input_branch_ids
                        ),
                        *(item.producer_terminal_ref for item in ordered),
                        *(
                            ()
                            if aggregation_terminal_ref is None
                            else (aggregation_terminal_ref,)
                        ),
                    }
                )
            ),
            payload=payload,
        )

    def _choice_candidate(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        node: HarnessControlNode,
        instance: HarnessNodeInstanceState,
        context: HarnessGraphEvaluationContext,
    ) -> HarnessGraphCandidate:
        condition_context = _condition_context(
            graph,
            state,
            node,
            instance,
            context,
        )
        default_branch = None
        selected = None
        for branch in node.branches:
            if branch.is_default:
                default_branch = branch
                continue
            if branch.condition is not None and evaluate_condition(
                branch.condition,
                condition_context,
            ):
                selected = branch
                break
        selected = selected or default_branch
        if selected is None:
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.HALT_RUN,
                "no_matching_route",
                0,
                node_id=node.node_id,
                node_instance_id=instance.instance_id,
                payload={"condition_policy_version": graph.condition_policy_version},
            )
        return HarnessGraphCandidate(
            HarnessGraphCandidateType.SELECT_CHOICE,
            "choice_branch_selected",
            20,
            node_id=node.node_id,
            node_instance_id=instance.instance_id,
            target_node_ids=selected.entry_node_ids,
            branch_id=selected.branch_id,
            payload={
                "condition_policy_version": graph.condition_policy_version,
                "branch_priority": selected.priority,
                "is_default": selected.is_default,
            },
        )

    def _fork_candidate(
        self,
        graph: NormalizedHarnessGraph,
        node: HarnessControlNode,
        instance: HarnessNodeInstanceState,
    ) -> HarnessGraphCandidate:
        branches = tuple(
            {
                "branch_id": branch.branch_id,
                "entry_node_ids": list(branch.entry_node_ids),
                "output_namespace": branch.output_namespace,
                "priority": branch.priority,
            }
            for branch in node.branches
        )
        targets = tuple(
            target for branch in node.branches for target in branch.entry_node_ids
        )
        join = _join_for_fork(graph, node.node_id)
        return HarnessGraphCandidate(
            HarnessGraphCandidateType.OPEN_FORK,
            "parallel_fork_ready",
            20,
            node_id=node.node_id,
            node_instance_id=instance.instance_id,
            target_node_ids=targets,
            payload={
                "fork_kind": node.node_kind.value,
                "branches": branches,
                "join_node_id": None if join is None else join.node_id,
                "failure_policy": (
                    None
                    if join is None or join.join is None
                    else join.join.failure_policy
                ),
            },
        )

    def _join_candidates(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        nodes_by_id: Mapping[str, HarnessGraphNode],
    ) -> tuple[HarnessGraphCandidate, ...]:
        candidates: list[HarnessGraphCandidate] = []
        instances_by_id = {item.instance_id: item for item in state.node_instances}
        for join_state in state.join_states:
            join_instance = instances_by_id[join_state.join_instance_id]
            definition = nodes_by_id[join_instance.identity.node_id]
            if (
                not isinstance(definition, HarnessControlNode)
                or definition.join is None
            ):
                raise HarnessValidationError(
                    "join state cannot resolve its normalized definition",
                    code="graph_evaluator_join_definition_mismatch",
                )
            if (
                join_state.join_kind is HarnessJoinKind.ANY
                and join_state.status is HarnessJoinStatus.SATISFIED
                and join_state.winner_branch_id is not None
            ):
                fork = nodes_by_id.get(definition.join.fork_node_id)
                cancellation_policy = (
                    fork.metadata.get("cancellation_policy")
                    if isinstance(fork, HarnessControlNode)
                    else None
                )
                if cancellation_policy == "cancel_losers":
                    parent_path = join_instance.identity.branch_path
                    losers = tuple(
                        sorted(
                            (
                                item
                                for item in state.node_instances
                                if not item.is_terminal
                                and item.instance_id != join_instance.instance_id
                                and item.status
                                is not HarnessNodeInstanceStatus.CANCEL_REQUESTED
                                and len(item.identity.branch_path) > len(parent_path)
                                and item.identity.branch_path[: len(parent_path)]
                                == parent_path
                                and item.identity.branch_path[len(parent_path)]
                                in join_state.required_branch_ids
                                and item.identity.branch_path[len(parent_path)]
                                != join_state.winner_branch_id
                            ),
                            key=lambda item: (
                                item.identity.activation_ordinal,
                                item.instance_id,
                            ),
                        )
                    )
                    winner_ref = join_state.terminal_event_refs[
                        join_state.winner_branch_id
                    ]
                    candidates.extend(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.REQUEST_BRANCH_CANCEL,
                            "parallel_any_cancel_loser",
                            5,
                            node_id=item.identity.node_id,
                            node_instance_id=item.instance_id,
                            branch_id=item.identity.branch_path[len(parent_path)],
                            evidence_refs=(winner_ref,),
                            payload={
                                "join_instance_id": join_instance.instance_id,
                                "cancellation_policy": "cancel_losers",
                                "winner_branch_id": join_state.winner_branch_id,
                            },
                        )
                        for item in losers
                    )
                continue
            if join_state.status is not HarnessJoinStatus.OPEN:
                continue
            if join_state.join_kind is HarnessJoinKind.ALL:
                failed_branches = tuple(
                    sorted(
                        branch_id
                        for branch_id, instance_id in join_state.completed_branch_instances.items()
                        if instances_by_id[instance_id].status
                        not in _SUCCESSFUL_NODE_STATUSES
                    )
                )
                evidence = tuple(join_state.terminal_event_refs.values())
                if failed_branches and definition.join.failure_policy == "fail_fast":
                    unfinished = _active_join_scope_instances(
                        state,
                        join_instance,
                        join_state,
                    )
                    cancel_targets = tuple(
                        item
                        for item in unfinished
                        if item.status is not HarnessNodeInstanceStatus.CANCEL_REQUESTED
                    )
                    if cancel_targets:
                        parent_path = join_instance.identity.branch_path
                        candidates.extend(
                            HarnessGraphCandidate(
                                HarnessGraphCandidateType.REQUEST_BRANCH_CANCEL,
                                "parallel_all_fail_fast_cancel_sibling",
                                5,
                                node_id=item.identity.node_id,
                                node_instance_id=item.instance_id,
                                branch_id=item.identity.branch_path[len(parent_path)],
                                evidence_refs=evidence,
                                payload={
                                    "join_instance_id": join_instance.instance_id,
                                    "failure_policy": "fail_fast",
                                    "failed_branch_ids": list(failed_branches),
                                },
                            )
                            for item in cancel_targets
                        )
                        continue
                    if unfinished:
                        # A cancellation request is not a terminal outcome. The
                        # Join remains open until every active attempt confirms
                        # termination through a durable result.
                        continue
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.FAIL_JOIN,
                            "parallel_all_fail_fast_failure",
                            10,
                            node_id=definition.node_id,
                            node_instance_id=join_state.join_instance_id,
                            evidence_refs=evidence,
                            payload={
                                "failure_policy": "fail_fast",
                                "failed_branch_ids": list(failed_branches),
                            },
                        )
                    )
                    continue
                if set(join_state.completed_branch_instances) != set(
                    join_state.required_branch_ids
                ):
                    continue
                if _active_join_scope_instances(state, join_instance, join_state):
                    continue
                if failed_branches:
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.FAIL_JOIN,
                            f"parallel_all_{definition.join.failure_policy}_failure",
                            10,
                            node_id=definition.node_id,
                            node_instance_id=join_state.join_instance_id,
                            evidence_refs=evidence,
                            payload={
                                "failure_policy": definition.join.failure_policy,
                                "failed_branch_ids": list(failed_branches),
                            },
                        )
                    )
                else:
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.SATISFY_JOIN,
                            "parallel_all_join_satisfied",
                            10,
                            node_id=definition.node_id,
                            node_instance_id=join_state.join_instance_id,
                            evidence_refs=evidence,
                            payload={
                                "branch_instance_ids": thaw_json(
                                    join_state.completed_branch_instances
                                )
                            },
                        )
                    )
                continue

            successful = tuple(
                (
                    instances_by_id[instance_id].last_event_sequence,
                    instances_by_id[instance_id].identity.activation_ordinal,
                    branch_id,
                    instance_id,
                )
                for branch_id, instance_id in join_state.completed_branch_instances.items()
                if instances_by_id[instance_id].status in _SUCCESSFUL_NODE_STATUSES
            )
            if successful:
                _, _, branch_id, instance_id = min(successful)
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.SELECT_PARALLEL_WINNER,
                        "parallel_any_verified_winner",
                        5,
                        node_id=definition.node_id,
                        node_instance_id=join_state.join_instance_id,
                        branch_id=branch_id,
                        evidence_refs=(join_state.terminal_event_refs[branch_id],),
                        payload={"winner_node_instance_id": instance_id},
                    )
                )
            elif set(join_state.completed_branch_instances) == set(
                join_state.required_branch_ids
            ):
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.FAIL_JOIN,
                        "parallel_any_all_branches_failed",
                        10,
                        node_id=definition.node_id,
                        node_instance_id=join_state.join_instance_id,
                        evidence_refs=tuple(join_state.terminal_event_refs.values()),
                        payload={"failure_policy": definition.join.failure_policy},
                    )
                )
        return tuple(candidates)

    def _loop_candidate(
        self,
        graph: NormalizedHarnessGraph,
        node: HarnessControlNode,
        instance: HarnessNodeInstanceState,
        state: HarnessGraphState,
        context: HarnessGraphEvaluationContext,
    ) -> HarnessGraphCandidate:
        if node.loop is None:
            raise HarnessValidationError(
                "loop guard is missing its normalized contract",
                code="graph_evaluator_loop_contract_missing",
            )
        counter = _loop_counter_for(instance, state.loop_counters)
        completed = 0 if counter is None else counter.completed_iterations
        condition_context = _condition_context(
            graph,
            state,
            node,
            instance,
            context,
        )
        should_continue = evaluate_condition(node.loop.condition, condition_context)
        if should_continue and completed < node.loop.max_iterations:
            iteration_scope = (
                instance.identity.branch_path,
                (
                    *instance.identity.iteration_vector,
                    HarnessLoopIteration(node.node_id, completed),
                ),
            )
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.START_LOOP_ITERATION,
                "loop_condition_matched",
                20,
                node_id=node.node_id,
                node_instance_id=instance.instance_id,
                target_node_ids=node.loop.body_entry_node_ids,
                payload={
                    "iteration": completed,
                    "max_iterations": node.loop.max_iterations,
                    **_scope_payload(iteration_scope),
                },
            )
        if should_continue:
            if not node.loop.exhaustion_node_ids:
                return HarnessGraphCandidate(
                    HarnessGraphCandidateType.HALT_RUN,
                    "loop_budget_exhausted",
                    0,
                    node_id=node.node_id,
                    node_instance_id=instance.instance_id,
                    payload={
                        "completed_iterations": completed,
                        "max_iterations": node.loop.max_iterations,
                        **_scope_payload(_instance_scope(instance)),
                    },
                )
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.EXHAUST_LOOP,
                "loop_exhaustion_route_selected",
                20,
                node_id=node.node_id,
                node_instance_id=instance.instance_id,
                target_node_ids=node.loop.exhaustion_node_ids,
                payload={
                    "completed_iterations": completed,
                    "max_iterations": node.loop.max_iterations,
                    **_scope_payload(_instance_scope(instance)),
                },
            )
        return HarnessGraphCandidate(
            HarnessGraphCandidateType.EXIT_LOOP,
            "loop_condition_not_matched",
            20,
            node_id=node.node_id,
            node_instance_id=instance.instance_id,
            target_node_ids=node.loop.exit_node_ids,
            payload={
                "completed_iterations": completed,
                **_scope_payload(_instance_scope(instance)),
            },
        )

    def _wait_candidate(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        node: HarnessGraphNode,
        instance: HarnessNodeInstanceState,
        registration,
        context: HarnessGraphEvaluationContext,
    ) -> HarnessGraphCandidate | None:
        if not isinstance(node, HarnessControlNode) or node.wait is None:
            raise HarnessValidationError(
                "Wait node is missing its normalized contract",
                code="graph_evaluator_wait_contract_missing",
            )
        if instance.status is HarnessNodeInstanceStatus.READY and registration is None:
            registration_projection = _wait_registration_projection(
                graph,
                state,
                node,
                instance,
                context,
            )
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.REGISTER_WAIT,
                "wait_registration_required",
                20,
                node_id=node.node_id,
                node_instance_id=instance.instance_id,
                payload={
                    "wait_contract": node.wait.to_dict(),
                    "registration": registration_projection,
                },
            )
        if (
            instance.status is not HarnessNodeInstanceStatus.WAITING
            or registration is None
        ):
            return None
        if registration.status is HarnessWaitStatus.RESUMED:
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.RESUME_WAIT,
                "wait_signal_accepted",
                10,
                node_id=node.node_id,
                node_instance_id=instance.instance_id,
                evidence_refs=(registration.resolution_event_ref,),
                payload={"wait_id": registration.wait_id, "resolution": "resumed"},
            )
        if registration.status is HarnessWaitStatus.TIMED_OUT:
            timeout_policy = node.wait.timeout_policy
            if (
                timeout_policy is not None
                and timeout_policy.action is WaitTimeoutAction.HALT
            ):
                return HarnessGraphCandidate(
                    HarnessGraphCandidateType.HALT_RUN,
                    "wait_timeout_halt",
                    0,
                    node_id=node.node_id,
                    node_instance_id=instance.instance_id,
                    evidence_refs=(registration.resolution_event_ref,),
                    payload={"wait_id": registration.wait_id},
                )
            targets = ()
            if (
                timeout_policy is not None
                and timeout_policy.action is WaitTimeoutAction.ROUTE
                and timeout_policy.target_node_id is not None
            ):
                targets = (timeout_policy.target_node_id,)
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.RESUME_WAIT,
                "wait_timeout_accepted",
                10,
                node_id=node.node_id,
                node_instance_id=instance.instance_id,
                target_node_ids=targets,
                evidence_refs=(registration.resolution_event_ref,),
                payload={"wait_id": registration.wait_id, "resolution": "timed_out"},
            )
        if registration.status is HarnessWaitStatus.CANCELLED:
            approval_rejected = (
                instance.metadata.get("wait_cause_kind")
                == HarnessWaitCauseKind.APPROVAL.value
                and instance.metadata.get("approval_granted") is False
            )
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.COMPLETE_RUN,
                "approval_cancelled" if approval_rejected else "wait_cancelled",
                50,
                evidence_refs=(registration.resolution_event_ref,),
                payload={
                    "wait_id": registration.wait_id,
                    "outcome": "cancelled",
                },
            )
        return None

    def _activation_candidates(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        nodes_by_id: Mapping[str, HarnessGraphNode],
        instances_by_definition: Mapping[str, tuple[HarnessNodeInstanceState, ...]],
        instances_by_scope: Mapping[
            tuple[
                str,
                tuple[str, ...],
                tuple[HarnessLoopIteration, ...],
            ],
            tuple[HarnessNodeInstanceState, ...],
        ],
    ) -> tuple[HarnessGraphCandidate, ...]:
        incoming: dict[str, list[HarnessGraphEdge]] = defaultdict(list)
        outgoing: dict[str, list[HarnessGraphEdge]] = defaultdict(list)
        for edge in graph.edges:
            incoming[edge.target_id].append(edge)
            outgoing[edge.source_id].append(edge)
        candidates: list[HarnessGraphCandidate] = list(
            _selected_control_activation_candidates(
                state,
                nodes_by_id,
                instances_by_scope,
                outgoing,
            )
        )
        candidates.extend(
            _loop_back_activation_candidates(
                state,
                instances_by_scope,
                outgoing,
            )
        )
        already_selected = {
            (
                item.node_id,
                tuple(item.payload.get("branch_path", ())),
                _iteration_vector_from_payload(item.payload),
            )
            for item in candidates
            if item.node_id is not None
        }
        for node_id in sorted(nodes_by_id):
            if node_id in graph.entry_node_ids:
                root_key = (node_id, (), ())
                if (
                    root_key not in instances_by_scope
                    and root_key not in already_selected
                ):
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.ACTIVATE_NODE,
                            "graph_entry_ready",
                            40,
                            node_id=node_id,
                            payload=_scope_payload(((), ())),
                        )
                    )
                continue
            definition = nodes_by_id[node_id]
            if (
                isinstance(definition, HarnessControlNode)
                and definition.node_kind is HarnessGraphNodeKind.CHOICE_JOIN
            ):
                for scope, evidence_refs, payload in _choice_join_ready_scopes(
                    definition,
                    nodes_by_id,
                    instances_by_definition,
                ):
                    scoped_key = (node_id, *scope)
                    if (
                        scoped_key in instances_by_scope
                        or scoped_key in already_selected
                    ):
                        continue
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.ACTIVATE_NODE,
                            "selected_choice_branch_succeeded",
                            40,
                            node_id=node_id,
                            evidence_refs=evidence_refs,
                            payload={**payload, **_scope_payload(scope)},
                        )
                    )
                continue
            if (
                isinstance(definition, HarnessControlNode)
                and definition.node_kind is HarnessGraphNodeKind.LOOP_JOIN
            ):
                for scope, evidence_refs, payload in _loop_join_ready_scopes(
                    definition,
                    nodes_by_id,
                    instances_by_definition,
                ):
                    scoped_key = (node_id, *scope)
                    if (
                        scoped_key in instances_by_scope
                        or scoped_key in already_selected
                    ):
                        continue
                    candidates.append(
                        HarnessGraphCandidate(
                            HarnessGraphCandidateType.ACTIVATE_NODE,
                            "selected_loop_route_succeeded",
                            40,
                            node_id=node_id,
                            evidence_refs=evidence_refs,
                            payload={**payload, **_scope_payload(scope)},
                        )
                    )
                continue
            dependency_edges = tuple(
                edge
                for edge in incoming.get(node_id, ())
                if edge.edge_kind in _FORWARD_DEPENDENCY_EDGE_KINDS
            )
            if not dependency_edges:
                continue
            ready_edges_by_scope = _ready_dependency_edges_by_scope(
                dependency_edges,
                instances_by_definition,
            )
            for scope in sorted(ready_edges_by_scope, key=_scope_sort_key):
                scoped_key = (node_id, *scope)
                if scoped_key in instances_by_scope or scoped_key in already_selected:
                    continue
                causal_edges = ready_edges_by_scope[scope]
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.ACTIVATE_NODE,
                        "predecessors_satisfied",
                        40,
                        node_id=node_id,
                        evidence_refs=tuple(
                            _terminal_evidence_for_definition(
                                edge.source_id,
                                instances_by_definition,
                                scope=scope,
                            )
                            for edge in causal_edges
                        ),
                        payload=_scope_payload(scope),
                    )
                )
        return _admit_activation_candidates(state, tuple(candidates))

    def _compensation_candidates(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
        nodes_by_id: Mapping[str, HarnessGraphNode],
    ) -> tuple[HarnessGraphCandidate, ...]:
        if not _compensation_mode(state):
            return ()
        entries = state.compensation_stack
        if any(item.status is HarnessCompensationStatus.RUNNING for item in entries):
            return ()
        pending = tuple(
            item for item in entries if item.status is HarnessCompensationStatus.PENDING
        )
        if pending:
            budget = state.budgets.get("compensations")
            if budget is None:
                return (
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.HALT_RUN,
                        "graph_budget_counter_missing",
                        0,
                        payload={
                            "missing_counters": ["compensations"],
                            "outcome": "compensation_failed",
                            "manual_intervention_required": True,
                        },
                    ),
                )
            if budget.remaining <= 0:
                return (
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.HALT_RUN,
                        "compensation_budget_exhausted",
                        0,
                        evidence_refs=tuple(
                            item.effect_outcome_ref for item in pending
                        ),
                        payload={
                            "limit": budget.limit,
                            "used": budget.used,
                            "reserved": budget.reserved,
                            "outcome": "compensation_failed",
                            "manual_intervention_required": True,
                        },
                    ),
                )
            entry = max(
                pending,
                key=lambda item: (item.effect_commit_sequence, item.entry_id),
            )
            origin = next(
                item
                for item in state.node_instances
                if item.instance_id == entry.origin_node_instance_id
            )
            binding = next(
                (
                    item
                    for item in graph.compensation_refs
                    if item.for_node_id == origin.identity.node_id
                ),
                None,
            )
            if binding is None:
                return (
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.HALT_RUN,
                        "compensation_binding_missing",
                        0,
                        node_id=origin.identity.node_id,
                        evidence_refs=(entry.effect_outcome_ref,),
                        payload={
                            "outcome": "indeterminate",
                            "manual_intervention_required": True,
                        },
                    ),
                )
            return (
                HarnessGraphCandidate(
                    HarnessGraphCandidateType.SCHEDULE_COMPENSATION,
                    "next_compensation_in_reverse_effect_order",
                    10,
                    node_id=binding.compensation_node_id,
                    target_node_ids=(binding.compensation_node_id,),
                    evidence_refs=(entry.effect_outcome_ref,),
                    payload={
                        "entry_id": entry.entry_id,
                        "origin_node_instance_id": origin.instance_id,
                        "handler_ref": binding.handler_ref.to_dict(),
                        "activity_ref": binding.activity_ref.to_dict(),
                        "idempotency_key": entry.idempotency_key,
                        "fencing_generation": entry.fencing_generation,
                    },
                ),
            )
        indeterminate = tuple(
            item
            for item in entries
            if item.status is HarnessCompensationStatus.INDETERMINATE
        )
        if indeterminate:
            return (
                HarnessGraphCandidate(
                    HarnessGraphCandidateType.HALT_RUN,
                    "compensation_outcome_indeterminate",
                    0,
                    evidence_refs=tuple(
                        item.outcome_ref
                        for item in indeterminate
                        if item.outcome_ref is not None
                    ),
                    payload={
                        "outcome": "indeterminate",
                        "manual_intervention_required": True,
                    },
                ),
            )
        failed = tuple(
            item
            for item in entries
            if item.status is HarnessCompensationStatus.FAILED
        )
        if failed:
            return (
                HarnessGraphCandidate(
                    HarnessGraphCandidateType.HALT_RUN,
                    "compensation_failed",
                    0,
                    evidence_refs=tuple(
                        item.outcome_ref
                        for item in failed
                        if item.outcome_ref is not None
                    ),
                    payload={
                        "outcome": "compensation_failed",
                        "manual_intervention_required": True,
                    },
                ),
            )
        if entries and all(
            item.status is HarnessCompensationStatus.SUCCEEDED for item in entries
        ):
            return (
                HarnessGraphCandidate(
                    HarnessGraphCandidateType.COMPLETE_RUN,
                    "all_compensations_succeeded",
                    50,
                    evidence_refs=tuple(
                        item.outcome_ref
                        for item in entries
                        if item.outcome_ref is not None
                    ),
                    payload={"outcome": "compensated"},
                ),
            )
        return ()

    def _run_projection_candidate(
        self,
        graph: NormalizedHarnessGraph,
        state: HarnessGraphState,
    ) -> HarnessGraphCandidate | None:
        if state.lifecycle is RunLifecycle.WAITING:
            return None
        active = tuple(item for item in state.node_instances if not item.is_terminal)
        if active:
            if (
                not _has_runnable_or_running_work(state, active)
                and any(item.is_waiting for item in active)
                and (
                    any(item.unresolved for item in state.wait_registrations)
                    or any(
                        item.node_kind is HarnessGraphNodeKind.EXECUTABLE
                        and item.step_status is HarnessStepStatus.WAITING_APPROVAL
                        for item in active
                    )
                )
            ):
                return HarnessGraphCandidate(
                    HarnessGraphCandidateType.PROJECT_RUN_WAITING,
                    "only_unresolved_waits_remain",
                    60,
                )
            return None
        instances_by_definition = _instances_by_definition(state.node_instances)
        if all(
            _definition_succeeded(node_id, instances_by_definition)
            for node_id in graph.terminal_node_ids
        ):
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.COMPLETE_RUN,
                "graph_terminal_nodes_succeeded",
                60,
                evidence_refs=tuple(
                    _terminal_evidence_for_definition(
                        node_id,
                        instances_by_definition,
                    )
                    for node_id in graph.terminal_node_ids
                ),
                payload={"outcome": "succeeded"},
            )
        failed = tuple(
            item
            for item in state.node_instances
            if item.status
            in {
                HarnessNodeInstanceStatus.FAILED,
                HarnessNodeInstanceStatus.CANCELLED,
                HarnessNodeInstanceStatus.HALTED,
            }
        )
        if failed:
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.COMPLETE_RUN,
                "graph_terminal_failure",
                60,
                evidence_refs=tuple(
                    canonical_checksum(item.to_dict()) for item in failed
                ),
                payload={"outcome": "failed"},
            )
        return None


def _has_runnable_or_running_work(
    state: HarnessGraphState,
    active: tuple[HarnessNodeInstanceState, ...],
) -> bool:
    passive_join_ids = {
        item.join_instance_id
        for item in state.join_states
        if item.status is HarnessJoinStatus.OPEN
    }
    return any(
        item.is_ready
        or (item.is_running and item.instance_id not in passive_join_ids)
        for item in active
    )


def merge_branch_output_references(
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    merge_node: HarnessControlNode,
    *,
    branch_path: tuple[str, ...],
    iteration_vector: tuple[HarnessLoopIteration, ...],
) -> tuple[
    HarnessNodeInstanceState,
    HarnessJoinState,
    tuple[HarnessBranchOutputReference, ...],
]:
    if merge_node.merge is None:
        raise HarnessValidationError(
            "Merge node is missing its normalized contract",
            code="graph_evaluator_merge_contract_missing",
        )
    join_node_id = merge_node.metadata.get("join_node_id")
    if not isinstance(join_node_id, str):
        raise HarnessValidationError(
            "Merge node is missing its paired Join identity",
            code="graph_evaluator_merge_contract_missing",
        )
    join_instance = next(
        (
            item
            for item in state.node_instances
            if item.identity.node_id == join_node_id
            and item.identity.branch_path == branch_path
            and item.identity.iteration_vector == iteration_vector
            and item.status is HarnessNodeInstanceStatus.SUCCEEDED
        ),
        None,
    )
    if join_instance is None:
        raise HarnessValidationError(
            "Merge activation lacks one successful paired Join instance",
            code="graph_merge_join_evidence_missing",
        )
    join_state = next(
        (
            item
            for item in state.join_states
            if item.join_instance_id == join_instance.instance_id
            and item.status is HarnessJoinStatus.SATISFIED
        ),
        None,
    )
    if join_state is None:
        raise HarnessValidationError(
            "Merge activation lacks satisfied Join evidence",
            code="graph_merge_join_evidence_missing",
        )
    fork_instance = next(
        (
            item
            for item in state.node_instances
            if item.instance_id == join_state.fork_instance_id
        ),
        None,
    )
    if fork_instance is None:
        raise HarnessValidationError(
            "Merge activation lacks its durable Fork instance",
            code="graph_merge_join_evidence_missing",
        )
    if (
        fork_instance.status is not HarnessNodeInstanceStatus.SUCCEEDED
        or fork_instance.identity.branch_path != branch_path
        or fork_instance.identity.iteration_vector != iteration_vector
    ):
        raise HarnessValidationError(
            "Merge activation does not match its paired Fork scope",
            code="graph_merge_join_evidence_missing",
        )
    fork_definition = next(
        (
            item
            for item in graph.nodes
            if item.node_id == fork_instance.identity.node_id
            and isinstance(item, HarnessControlNode)
        ),
        None,
    )
    if fork_definition is None:
        raise HarnessValidationError(
            "Merge activation cannot resolve its pinned Fork definition",
            code="graph_merge_join_evidence_missing",
        )
    branches = {item.branch_id: item for item in fork_definition.branches}
    definitions = {item.node_id: item for item in graph.nodes}
    instances = {item.instance_id: item for item in state.node_instances}
    references: list[HarnessBranchOutputReference] = []
    for branch_id in merge_node.merge.input_branch_ids:
        branch = branches.get(branch_id)
        terminal_instance_id = join_state.completed_branch_instances.get(branch_id)
        branch_terminal_ref = join_state.terminal_event_refs.get(branch_id)
        branch_terminal = (
            None
            if not isinstance(terminal_instance_id, str)
            else instances.get(terminal_instance_id)
        )
        branch_scope = (*fork_instance.identity.branch_path, branch_id)
        if (
            branch is None
            or branch_terminal is None
            or not isinstance(branch_terminal_ref, str)
            or branch_terminal.status not in _SUCCESSFUL_NODE_STATUSES
            or not _scope_has_prefix(
                branch_terminal.identity.branch_path,
                branch_scope,
            )
            or not _scope_has_prefix(
                branch_terminal.identity.iteration_vector,
                fork_instance.identity.iteration_vector,
            )
        ):
            raise HarnessValidationError(
                "Merge input cannot resolve one exact branch terminal",
                code="graph_merge_input_missing",
                details={"branch_id": branch_id},
            )
        branch_terminal_definition = definitions.get(branch_terminal.identity.node_id)
        if branch_terminal_definition is None or (
            _producer_completion_decision_ref(
                branch_terminal,
                branch_terminal_definition,
            )
            != branch_terminal_ref
        ):
            raise HarnessValidationError(
                "Merge branch terminal does not match its durable decision",
                code="graph_merge_input_reference_invalid",
                details={"branch_id": branch_id},
            )

        producers = tuple(
            item
            for item in state.node_instances
            if item.status in _SUCCESSFUL_NODE_STATUSES
            and _scope_has_prefix(item.identity.branch_path, branch_scope)
            and _scope_has_prefix(
                item.identity.iteration_vector,
                fork_instance.identity.iteration_vector,
            )
            and item.activation_sequence > fork_instance.last_event_sequence
            and item.last_event_sequence <= join_state.last_event_sequence
            and _declared_business_output_keys(definitions.get(item.identity.node_id))
        )
        for producer in producers:
            definition = definitions.get(producer.identity.node_id)
            if definition is None:  # pragma: no cover - filtered above
                raise AssertionError("Merge producer definition disappeared")
            producer_terminal_ref = _producer_terminal_or_effect_decision_ref(
                producer,
                definition,
            )
            for output_key in _declared_business_output_keys(definition):
                payload_ref = producer.output_refs.get(output_key)
                if not isinstance(payload_ref, str):
                    raise HarnessValidationError(
                        "Merge input requires exact payload references",
                        code="graph_merge_input_reference_invalid",
                        details={
                            "branch_id": branch_id,
                            "output_key": output_key,
                            "producer_node_instance_id": producer.instance_id,
                        },
                    )
                references.append(
                    HarnessBranchOutputReference(
                        branch_id=branch_id,
                        output_namespace=branch.output_namespace,
                        branch_priority=branch.priority,
                        producer_node_id=producer.identity.node_id,
                        producer_declaration_order=definition.declaration_order,
                        producer_node_instance_id=producer.instance_id,
                        producer_activation_ordinal=(
                            producer.identity.activation_ordinal
                        ),
                        attempt=producer.attempt,
                        iteration_vector=producer.identity.iteration_vector,
                        output_key=output_key,
                        payload_ref=payload_ref,
                        producer_terminal_ref=producer_terminal_ref,
                    )
                )
    return (
        join_instance,
        join_state,
        tuple(sorted(references, key=lambda item: item.stable_order_key)),
    )


def _declared_business_output_keys(
    definition: HarnessGraphNode | None,
) -> tuple[str, ...]:
    if isinstance(definition, HarnessExecutableNode):
        output_keys = definition.output_keys
    elif isinstance(definition, HarnessControlNode) and definition.merge is not None:
        output_keys = definition.merge.output_keys
    else:
        return ()
    return tuple(key for key in output_keys if key != "activity_result")


def _producer_terminal_or_effect_decision_ref(
    producer: HarnessNodeInstanceState,
    definition: HarnessGraphNode,
) -> str:
    if isinstance(definition, HarnessExecutableNode):
        if definition.side_effect_ref is not None:
            effect_decision_ref = producer.metadata.get("side_effect_decision_ref")
            if effect_decision_ref is not None:
                return _checksum(
                    effect_decision_ref,
                    "merge_input.side_effect_decision_ref",
                )
        return _producer_completion_decision_ref(producer, definition)
    if isinstance(definition, HarnessControlNode) and definition.merge is not None:
        merge_result_ref = producer.metadata.get("merge_result_ref")
        if merge_result_ref is not None:
            return _checksum(merge_result_ref, "merge_input.merge_result_ref")
        merge_decision_ref = producer.metadata.get("merge_decision_ref")
        if merge_decision_ref is not None:
            return _checksum(merge_decision_ref, "merge_input.merge_decision_ref")
    raise HarnessValidationError(
        "Merge input producer lacks a durable terminal or effect decision reference",
        code="graph_merge_input_reference_invalid",
        details={"producer_node_instance_id": producer.instance_id},
    )


def _producer_completion_decision_ref(
    producer: HarnessNodeInstanceState,
    definition: HarnessGraphNode,
) -> str:
    if isinstance(definition, HarnessExecutableNode):
        if producer.metadata.get("last_decision_type") != "complete_node":
            raise HarnessValidationError(
                "Merge executable producer lacks its terminal decision",
                code="graph_merge_input_reference_invalid",
                details={"producer_node_instance_id": producer.instance_id},
            )
        reference = producer.metadata.get("last_decision_checksum")
    elif isinstance(definition, HarnessControlNode) and definition.merge is not None:
        reference = producer.metadata.get("merge_decision_ref")
    else:
        reference = producer.metadata.get("last_decision_checksum")
    if reference is None:
        raise HarnessValidationError(
            "Merge input producer lacks its terminal decision reference",
            code="graph_merge_input_reference_invalid",
            details={"producer_node_instance_id": producer.instance_id},
        )
    return _checksum(reference, "merge_input.producer_terminal_ref")


def _scope_has_prefix(
    value: tuple[Any, ...],
    prefix: tuple[Any, ...],
) -> bool:
    return len(value) >= len(prefix) and value[: len(prefix)] == prefix


def _active_join_scope_instances(
    state: HarnessGraphState,
    join_instance: HarnessNodeInstanceState,
    join_state: HarnessJoinState,
) -> tuple[HarnessNodeInstanceState, ...]:
    parent_path = join_instance.identity.branch_path
    return tuple(
        sorted(
            (
                item
                for item in state.node_instances
                if not item.is_terminal
                and item.instance_id != join_instance.instance_id
                and len(item.identity.branch_path) > len(parent_path)
                and item.identity.branch_path[: len(parent_path)] == parent_path
                and item.identity.branch_path[len(parent_path)]
                in join_state.required_branch_ids
                and item.identity.iteration_vector
                == join_instance.identity.iteration_vector
            ),
            key=lambda item: (
                item.identity.activation_ordinal,
                item.instance_id,
            ),
        )
    )


def _instances_by_definition(
    instances: tuple[HarnessNodeInstanceState, ...],
) -> dict[str, tuple[HarnessNodeInstanceState, ...]]:
    grouped: dict[str, list[HarnessNodeInstanceState]] = defaultdict(list)
    for instance in instances:
        grouped[instance.identity.node_id].append(instance)
    return {
        node_id: tuple(
            sorted(
                values,
                key=lambda item: (
                    item.identity.activation_ordinal,
                    item.instance_id,
                ),
            )
        )
        for node_id, values in grouped.items()
    }


def _instances_by_definition_scope(
    instances: tuple[HarnessNodeInstanceState, ...],
) -> dict[
    tuple[str, tuple[str, ...], tuple[HarnessLoopIteration, ...]],
    tuple[HarnessNodeInstanceState, ...],
]:
    grouped: dict[
        tuple[str, tuple[str, ...], tuple[HarnessLoopIteration, ...]],
        list[HarnessNodeInstanceState],
    ] = defaultdict(list)
    for instance in instances:
        grouped[(instance.identity.node_id, *_instance_scope(instance))].append(
            instance
        )
    return {
        key: tuple(
            sorted(
                values,
                key=lambda item: (
                    item.identity.activation_ordinal,
                    item.instance_id,
                ),
            )
        )
        for key, values in grouped.items()
    }


def _pending_run_operation(
    state: HarnessGraphState,
) -> HarnessGraphRunOperation | None:
    raw_operation = state.metadata.get("pending_run_operation")
    if raw_operation is None:
        return None
    if not isinstance(raw_operation, Mapping):
        raise HarnessValidationError(
            "pending Graph run operation is not a typed record",
            code="invalid_pending_graph_run_operation",
        )
    try:
        operation = HarnessGraphRunOperation.from_dict(raw_operation)
    except (HarnessValidationError, TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "pending Graph run operation is invalid",
            code="invalid_pending_graph_run_operation",
        ) from exc
    if operation.run_id != state.run_id:
        raise HarnessValidationError(
            "pending Graph run operation belongs to another run",
            code="graph_run_operation_run_mismatch",
        )
    return operation


def _run_operation_candidates(
    state: HarnessGraphState,
    operation: HarnessGraphRunOperation,
) -> tuple[HarnessGraphCandidate, ...]:
    if operation.operation_type is not HarnessGraphRunOperationType.CANCEL:
        raise HarnessValidationError(
            "unsupported pending Graph run operation",
            code="unsupported_graph_run_operation",
        )
    nodes = {item.instance_id: item for item in state.node_instances}
    cancel_targets = tuple(
        sorted(
            (
                nodes[item.node_instance_id]
                for item in state.active_activities
                if nodes[item.node_instance_id].status
                is not HarnessNodeInstanceStatus.CANCEL_REQUESTED
            ),
            key=lambda item: (
                item.identity.activation_ordinal,
                item.instance_id,
            ),
        )
    )
    payload = {
        "run_operation": operation.to_dict(),
        "outcome": "cancelled",
    }
    if cancel_targets:
        return tuple(
            HarnessGraphCandidate(
                HarnessGraphCandidateType.REQUEST_BRANCH_CANCEL,
                operation.reason_code,
                0,
                node_id=item.identity.node_id,
                node_instance_id=item.instance_id,
                evidence_refs=(operation.operation_ref,),
                payload=payload,
            )
            for item in cancel_targets
        )
    if state.active_activities:
        return ()
    return (
        HarnessGraphCandidate(
            HarnessGraphCandidateType.COMPLETE_RUN,
            operation.reason_code,
            0,
            evidence_refs=(operation.operation_ref,),
            payload=payload,
        ),
    )


def _validate_accepted_observations(
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    context: HarnessGraphEvaluationContext,
    nodes_by_id: Mapping[str, HarnessGraphNode],
) -> None:
    instances_by_id = {item.instance_id: item for item in state.node_instances}
    logical_identities: set[tuple[str, int, str, str]] = set()
    for observation in context.observations:
        if observation.observation_type is HarnessGraphObservationType.RUN_OPERATION:
            raise HarnessValidationError(
                "run operations are reduced from durable Graph state, not node context",
                code="graph_run_operation_context_rejected",
            )
        instance = instances_by_id.get(observation.node_instance_id)
        if instance is None:
            raise HarnessValidationError(
                "graph observation belongs to an unknown node instance",
                code="cross_node_graph_observation_rejected",
            )
        definition = nodes_by_id.get(instance.identity.node_id)
        if not isinstance(definition, HarnessExecutableNode):
            raise HarnessValidationError(
                "graph observation requires an executable node instance",
                code="graph_observation_node_kind_mismatch",
            )
        if observation.node_id != instance.identity.node_id:
            raise HarnessValidationError(
                "graph observation node definition does not match its instance",
                code="cross_node_graph_observation_rejected",
            )
        if observation.attempt != instance.attempt:
            raise HarnessValidationError(
                "graph observation belongs to another attempt",
                code="cross_attempt_graph_observation_rejected",
            )
        if observation.event_sequence > instance.last_event_sequence:
            raise HarnessValidationError(
                "graph observation has not been accepted by the current projection",
                code="uncommitted_graph_observation_rejected",
            )
        expected_contracts = _observation_contracts(
            observation.observation_type,
            definition,
            graph=graph,
        )
        if observation.contract_ref not in expected_contracts:
            raise HarnessValidationError(
                "graph observation contract does not match the pinned node binding",
                code="graph_observation_contract_mismatch",
                details={
                    "actual": observation.contract_ref.exact_ref,
                    "expected": sorted(item.exact_ref for item in expected_contracts),
                },
            )
        expected_evidence_kind = (
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
            else HarnessEvidenceKind.GATE_RESULT
        )
        if not any(
            evidence.evidence_ref == observation.evidence_ref
            and evidence.kind is expected_evidence_kind
            and evidence.node_instance_id == observation.node_instance_id
            and evidence.attempt == observation.attempt
            and evidence.event_sequence == observation.event_sequence
            and evidence.contract_ref == observation.contract_ref
            and evidence.payload_ref == observation.payload_ref
            for evidence in instance.evidence_refs
        ):
            raise HarnessValidationError(
                "graph observation evidence is absent from the accepted node projection",
                code="unaccepted_graph_observation_rejected",
            )
        logical_identity = (
            observation.node_instance_id,
            observation.attempt,
            observation.observation_type.value,
            (
                observation.contract_ref.exact_ref
                if observation.observation_type
                is HarnessGraphObservationType.GATE_RESULT
                else ""
            ),
        )
        if logical_identity in logical_identities:
            raise HarnessValidationError(
                "graph control context contains ambiguous accepted observations",
                code="ambiguous_graph_observation",
            )
        logical_identities.add(logical_identity)

    for observation in context.observations:
        if (
            observation.observation_type
            is not HarnessGraphObservationType.VERIFIED_OUTPUT
        ):
            continue
        instance = instances_by_id[observation.node_instance_id]
        definition = nodes_by_id[instance.identity.node_id]
        if not isinstance(definition, HarnessExecutableNode):
            raise AssertionError("verified output definition was validated above")
        declared_paths = _declared_control_fact_paths(definition)
        if observation.control_fact_paths != declared_paths:
            raise HarnessValidationError(
                "verified output control facts do not match the pinned Step declaration",
                code="graph_observation_control_fact_mismatch",
                details={
                    "actual": list(observation.control_fact_paths),
                    "expected": list(declared_paths),
                },
            )
        if instance.status not in _SUCCESSFUL_NODE_STATUSES:
            raise HarnessValidationError(
                "verified output cannot control routing before the node succeeds",
                code="unverified_graph_control_fact",
            )
        if not definition.gate_refs:
            raise HarnessValidationError(
                "routing control facts require an exact deterministic Gate binding",
                code="graph_control_fact_gate_missing",
            )
        gate_results = {
            item.contract_ref: item
            for item in context.for_node_instance(instance.instance_id)
            if item.attempt == observation.attempt
            and item.observation_type is HarnessGraphObservationType.GATE_RESULT
        }
        missing_gates = tuple(
            reference
            for reference in definition.gate_refs
            if reference not in gate_results
        )
        failed_gates = tuple(
            reference
            for reference in definition.gate_refs
            if reference in gate_results
            and gate_results[reference].payload.get("passed") is not True
        )
        if missing_gates or failed_gates:
            raise HarnessValidationError(
                "routing control facts lack successful deterministic Gate evidence",
                code="unverified_graph_control_fact",
                details={
                    "missing_gates": [item.exact_ref for item in missing_gates],
                    "failed_gates": [item.exact_ref for item in failed_gates],
                },
            )


def _observation_contracts(
    observation_type: HarnessGraphObservationType,
    definition: HarnessExecutableNode,
    *,
    graph: NormalizedHarnessGraph | None = None,
) -> tuple[HarnessContractReference, ...]:
    if observation_type is HarnessGraphObservationType.VERIFIED_OUTPUT:
        return (definition.step_ref,)
    if observation_type is HarnessGraphObservationType.WORKER_STATUS:
        return (definition.worker_ref,)
    if observation_type is HarnessGraphObservationType.APPROVAL:
        return (definition.step_ref,)
    if observation_type in {
        HarnessGraphObservationType.SIDE_EFFECT_OUTCOME,
        HarnessGraphObservationType.SIDE_EFFECT_FAILURE,
    }:
        references = []
        if definition.side_effect_ref is not None:
            references.append(definition.side_effect_ref)
        if graph is not None and graph.terminal_policy is not None:
            references.append(
                HarnessContractReference(
                    HarnessContractKind.SIDE_EFFECT,
                    graph.terminal_policy.handler.handler_id,
                    graph.terminal_policy.handler.version,
                )
            )
        return tuple(references)
    return definition.gate_refs


def _definition_succeeded(
    node_id: str,
    instances_by_definition: Mapping[str, tuple[HarnessNodeInstanceState, ...]],
) -> bool:
    instances = instances_by_definition.get(node_id, ())
    return bool(instances) and any(
        item.status in _SUCCESSFUL_NODE_STATUSES for item in instances
    )


def _terminal_evidence_for_definition(
    node_id: str,
    instances_by_definition: Mapping[str, tuple[HarnessNodeInstanceState, ...]],
    *,
    scope: tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]] | None = None,
) -> str:
    candidates = tuple(
        item
        for item in instances_by_definition.get(node_id, ())
        if item.status in _SUCCESSFUL_NODE_STATUSES
        and (scope is None or _instance_scope(item) == scope)
    )
    if not candidates:
        raise HarnessValidationError(
            "terminal evidence requested for an incomplete node definition",
            code="graph_evaluator_terminal_evidence_missing",
        )
    selected = max(
        candidates,
        key=lambda item: (item.last_event_sequence, item.identity.activation_ordinal),
    )
    evidence = tuple(
        item.evidence_ref
        for item in selected.evidence_refs
        if item.event_sequence == selected.last_event_sequence
    )
    return evidence[-1] if evidence else canonical_checksum(selected.to_dict())


def _edge_selected_for_instance(
    edge: HarnessGraphEdge,
    source: HarnessNodeInstanceState,
) -> bool:
    if edge.edge_kind not in {
        HarnessGraphEdgeKind.WAIT_RESUME,
        HarnessGraphEdgeKind.WAIT_TIMEOUT,
    }:
        return True
    resolution = source.metadata.get("wait_resolution")
    expected = (
        "resumed" if edge.edge_kind is HarnessGraphEdgeKind.WAIT_RESUME else "timed_out"
    )
    return resolution == expected


def _ready_dependency_edges_by_scope(
    edges: tuple[HarnessGraphEdge, ...],
    instances_by_definition: Mapping[
        str,
        tuple[HarnessNodeInstanceState, ...],
    ],
) -> Mapping[
    tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
    tuple[HarnessGraphEdge, ...],
]:
    wait_edges = tuple(
        edge
        for edge in edges
        if edge.edge_kind
        in {
            HarnessGraphEdgeKind.WAIT_RESUME,
            HarnessGraphEdgeKind.WAIT_TIMEOUT,
        }
    )
    selected_wait_edges: dict[
        tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
        list[HarnessGraphEdge],
    ] = defaultdict(list)
    for edge in wait_edges:
        for source in instances_by_definition.get(edge.source_id, ()):
            if (
                source.status in _SUCCESSFUL_NODE_STATUSES
                and _edge_selected_for_instance(edge, source)
            ):
                selected_wait_edges[_instance_scope(source)].append(edge)

    ordinary_edges = tuple(edge for edge in edges if edge not in wait_edges)
    ordinary_scopes: (
        set[tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]]] | None
    ) = None
    for edge in ordinary_edges:
        source_scopes = {
            _instance_scope(item)
            for item in instances_by_definition.get(edge.source_id, ())
            if item.status in _SUCCESSFUL_NODE_STATUSES
        }
        ordinary_scopes = (
            source_scopes
            if ordinary_scopes is None
            else ordinary_scopes.intersection(source_scopes)
        )

    ready: dict[
        tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
        tuple[HarnessGraphEdge, ...],
    ] = {}
    for scope in ordinary_scopes or ():
        ready[scope] = ordinary_edges
    for scope, selected in selected_wait_edges.items():
        ready[scope] = tuple(
            sorted(selected, key=lambda edge: (edge.priority, edge.edge_id))
        )
    return ready


def _instance_scope(
    instance: HarnessNodeInstanceState,
) -> tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]]:
    return (
        instance.identity.branch_path,
        instance.identity.iteration_vector,
    )


def _scope_payload(
    scope: tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
) -> dict[str, Any]:
    branch_path, iteration_vector = scope
    return {
        "branch_path": list(branch_path),
        "iteration_vector": [item.to_dict() for item in iteration_vector],
    }


def _iteration_vector_from_payload(
    payload: Mapping[str, Any],
) -> tuple[HarnessLoopIteration, ...]:
    raw = payload.get("iteration_vector", ())
    if not isinstance(raw, tuple | list):
        return ()
    values: list[HarnessLoopIteration] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return ()
        loop_id = item.get("loop_id")
        iteration = item.get("iteration")
        if not isinstance(loop_id, str) or not isinstance(iteration, int):
            return ()
        values.append(HarnessLoopIteration(loop_id, iteration))
    return tuple(values)


def _scope_sort_key(
    scope: tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
) -> tuple[Any, ...]:
    branch_path, iteration_vector = scope
    return (
        branch_path,
        tuple((item.loop_id, item.iteration) for item in iteration_vector),
    )


def _admit_activation_candidates(
    state: HarnessGraphState,
    candidates: tuple[HarnessGraphCandidate, ...],
) -> tuple[HarnessGraphCandidate, ...]:
    if not candidates:
        return ()
    activation_budget = state.budgets.get("node_activations")
    active_budget = state.budgets.get("max_active_nodes")
    missing = tuple(
        name
        for name, counter in (
            ("node_activations", activation_budget),
            ("max_active_nodes", active_budget),
        )
        if counter is None
    )
    if missing:
        return (
            HarnessGraphCandidate(
                HarnessGraphCandidateType.HALT_RUN,
                "graph_budget_counter_missing",
                0,
                payload={"missing_counters": list(missing)},
            ),
        )
    if activation_budget is None or active_budget is None:
        raise AssertionError("Graph budget counters were validated above")
    if activation_budget.remaining <= 0:
        return (
            HarnessGraphCandidate(
                HarnessGraphCandidateType.HALT_RUN,
                "node_activation_budget_exhausted",
                0,
                payload={
                    "limit": activation_budget.limit,
                    "used": activation_budget.used,
                    "reserved": activation_budget.reserved,
                },
            ),
        )
    active_count = sum(1 for item in state.node_instances if not item.is_terminal)
    active_capacity = min(
        active_budget.remaining,
        max(0, active_budget.limit - active_count),
    )
    admitted = min(activation_budget.remaining, active_capacity)
    if admitted <= 0:
        return ()
    return tuple(sorted(candidates, key=lambda item: item.sort_key)[:admitted])


def _selected_control_activation_candidates(
    state: HarnessGraphState,
    nodes_by_id: Mapping[str, HarnessGraphNode],
    instances_by_scope: Mapping[
        tuple[
            str,
            tuple[str, ...],
            tuple[HarnessLoopIteration, ...],
        ],
        tuple[HarnessNodeInstanceState, ...],
    ],
    outgoing: Mapping[str, list[HarnessGraphEdge]],
) -> tuple[HarnessGraphCandidate, ...]:
    candidates: list[HarnessGraphCandidate] = []
    for instance in state.node_instances:
        if (
            instance.status is HarnessNodeInstanceStatus.FAILED
            and instance.metadata.get("last_decision_type") == "route_to_repair"
        ):
            raw_targets = instance.metadata.get("target_node_ids", ())
            target_ids = (
                tuple(raw_targets)
                if isinstance(raw_targets, tuple | list)
                and all(isinstance(item, str) for item in raw_targets)
                else ()
            )
            declared_targets = {
                edge.target_id
                for edge in outgoing.get(instance.identity.node_id, ())
                if edge.edge_kind is HarnessGraphEdgeKind.REPAIR
            }
            for target_id in sorted(set(target_ids).intersection(declared_targets)):
                scope = _instance_scope(instance)
                if (target_id, *scope) in instances_by_scope:
                    continue
                candidates.append(
                    HarnessGraphCandidate(
                        HarnessGraphCandidateType.ACTIVATE_NODE,
                        "committed_repair_route_ready",
                        40,
                        node_id=target_id,
                        evidence_refs=(canonical_checksum(instance.to_dict()),),
                        payload={
                            "repair_source_node_instance_id": instance.instance_id,
                            "repair_source_node_id": instance.identity.node_id,
                            **_scope_payload(scope),
                        },
                    )
                )
            continue
        if instance.status not in _SUCCESSFUL_NODE_STATUSES:
            continue
        definition = nodes_by_id[instance.identity.node_id]
        if not isinstance(definition, HarnessControlNode):
            continue
        selected_edges: tuple[HarnessGraphEdge, ...] = ()
        if definition.node_kind is HarnessGraphNodeKind.CHOICE:
            selected_branch_id = instance.metadata.get("selected_branch_id")
            if isinstance(selected_branch_id, str):
                selected_edges = tuple(
                    edge
                    for edge in outgoing.get(definition.node_id, ())
                    if edge.edge_kind
                    in {HarnessGraphEdgeKind.CHOICE, HarnessGraphEdgeKind.DEFAULT}
                    and edge.branch_id == selected_branch_id
                )
        elif definition.node_kind in {
            HarnessGraphNodeKind.FORK_ALL,
            HarnessGraphNodeKind.FORK_ANY,
        }:
            opened = instance.metadata.get("opened_branch_ids", ())
            if isinstance(opened, tuple) and all(
                isinstance(item, str) for item in opened
            ):
                opened_ids = frozenset(opened)
                selected_edges = tuple(
                    edge
                    for edge in outgoing.get(definition.node_id, ())
                    if edge.edge_kind is HarnessGraphEdgeKind.FORK_BRANCH
                    and edge.branch_id in opened_ids
                )
        elif definition.node_kind is HarnessGraphNodeKind.LOOP_GUARD:
            route = instance.metadata.get("selected_loop_route_id")
            edge_kind = {
                "continue": HarnessGraphEdgeKind.LOOP_BODY,
                "exit": HarnessGraphEdgeKind.LOOP_EXIT,
                "exhaustion": HarnessGraphEdgeKind.LOOP_EXHAUSTED,
            }.get(route)
            if edge_kind is not None:
                selected_edges = tuple(
                    edge
                    for edge in outgoing.get(definition.node_id, ())
                    if edge.edge_kind is edge_kind
                )
        for edge in sorted(
            selected_edges,
            key=lambda item: (
                item.priority,
                "" if item.branch_id is None else item.branch_id,
                item.target_id,
                item.edge_id,
            ),
        ):
            if definition.node_kind is HarnessGraphNodeKind.LOOP_GUARD:
                decision_payload = instance.metadata.get("decision_payload", {})
                if not isinstance(decision_payload, Mapping):
                    raise HarnessValidationError(
                        "loop guard decision payload is invalid",
                        code="graph_evaluator_loop_counter_mismatch",
                    )
                raw_branch_path = decision_payload.get("branch_path", ())
                if not isinstance(raw_branch_path, tuple | list) or not all(
                    isinstance(item, str) for item in raw_branch_path
                ):
                    raise HarnessValidationError(
                        "loop guard branch scope is invalid",
                        code="graph_evaluator_loop_counter_mismatch",
                    )
                scope = (
                    tuple(raw_branch_path),
                    _iteration_vector_from_payload(decision_payload),
                )
            else:
                branch_path = instance.identity.branch_path + (
                    () if edge.branch_id is None else (edge.branch_id,)
                )
                scope = (branch_path, instance.identity.iteration_vector)
            if (edge.target_id, *scope) in instances_by_scope:
                continue
            candidates.append(
                HarnessGraphCandidate(
                    HarnessGraphCandidateType.ACTIVATE_NODE,
                    "committed_control_selection_ready",
                    40,
                    node_id=edge.target_id,
                    branch_id=edge.branch_id,
                    evidence_refs=(canonical_checksum(instance.to_dict()),),
                    payload={
                        "source_node_instance_id": instance.instance_id,
                        "source_node_id": definition.node_id,
                        **_scope_payload(scope),
                    },
                )
            )
    return tuple(candidates)


def _loop_back_activation_candidates(
    state: HarnessGraphState,
    instances_by_scope: Mapping[
        tuple[
            str,
            tuple[str, ...],
            tuple[HarnessLoopIteration, ...],
        ],
        tuple[HarnessNodeInstanceState, ...],
    ],
    outgoing: Mapping[str, list[HarnessGraphEdge]],
) -> tuple[HarnessGraphCandidate, ...]:
    candidates: list[HarnessGraphCandidate] = []
    for instance in state.node_instances:
        if (
            instance.status not in _SUCCESSFUL_NODE_STATUSES
            or not instance.identity.iteration_vector
        ):
            continue
        for edge in outgoing.get(instance.identity.node_id, ()):
            if edge.edge_kind is not HarnessGraphEdgeKind.LOOP_BACK:
                continue
            iteration = instance.identity.iteration_vector[-1]
            if edge.loop_id is None or iteration.loop_id != edge.loop_id:
                raise HarnessValidationError(
                    "loop-back edge conflicts with the body iteration scope",
                    code="graph_evaluator_loop_counter_mismatch",
                )
            parent_vector = instance.identity.iteration_vector[:-1]
            scope = (instance.identity.branch_path, parent_vector)
            counter = next(
                (
                    item
                    for item in state.loop_counters
                    if item.loop_id == edge.loop_id
                    and item.branch_path == scope[0]
                    and item.parent_iteration_vector == scope[1]
                ),
                None,
            )
            if (
                counter is None
                or counter.completed_iterations != iteration.iteration + 1
            ):
                continue
            next_iteration_vector = (
                *parent_vector,
                HarnessLoopIteration(edge.loop_id, counter.completed_iterations),
            )
            if any(
                item.identity.branch_path == instance.identity.branch_path
                and item.identity.iteration_vector == next_iteration_vector
                for item in state.node_instances
            ):
                continue
            existing = instances_by_scope.get((edge.target_id, *scope), ())
            if any(
                item.activation_sequence > instance.last_event_sequence
                for item in existing
            ):
                continue
            candidates.append(
                HarnessGraphCandidate(
                    HarnessGraphCandidateType.ACTIVATE_NODE,
                    "loop_iteration_completed",
                    35,
                    node_id=edge.target_id,
                    evidence_refs=(canonical_checksum(instance.to_dict()),),
                    payload={
                        "loop_back_source_instance_id": instance.instance_id,
                        "completed_iterations": counter.completed_iterations,
                        **_scope_payload(scope),
                    },
                )
            )
    return tuple(candidates)


def _choice_join_ready_scopes(
    definition: HarnessControlNode,
    nodes_by_id: Mapping[str, HarnessGraphNode],
    instances_by_definition: Mapping[str, tuple[HarnessNodeInstanceState, ...]],
) -> tuple[
    tuple[
        tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
        tuple[str, ...],
        dict[str, Any],
    ],
    ...,
]:
    choice_node_id = definition.metadata.get("choice_node_id")
    choice = (
        nodes_by_id.get(choice_node_id) if isinstance(choice_node_id, str) else None
    )
    if (
        not isinstance(choice, HarnessControlNode)
        or choice.node_kind is not HarnessGraphNodeKind.CHOICE
    ):
        raise HarnessValidationError(
            "Choice join cannot resolve its selector node",
            code="graph_evaluator_choice_join_mismatch",
            details={
                "choice_join_node_id": definition.node_id,
                "choice_node_id": choice_node_id,
            },
        )
    branches = {branch.branch_id: branch for branch in definition.branches}
    if tuple(branches) != tuple(branch.branch_id for branch in choice.branches):
        raise HarnessValidationError(
            "Choice join branches do not match their selector",
            code="graph_evaluator_choice_join_mismatch",
            details={"choice_join_node_id": definition.node_id},
        )

    ready: list[
        tuple[
            tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
            tuple[str, ...],
            dict[str, Any],
        ]
    ] = []
    for choice_instance in instances_by_definition.get(choice.node_id, ()):
        if choice_instance.status not in _SUCCESSFUL_NODE_STATUSES:
            continue
        selected_branch_id = choice_instance.metadata.get("selected_branch_id")
        branch = (
            branches.get(selected_branch_id)
            if isinstance(selected_branch_id, str)
            else None
        )
        if branch is None:
            raise HarnessValidationError(
                "completed Choice instance is missing its declared branch selection",
                code="graph_evaluator_choice_selection_missing",
                details={
                    "choice_node_id": choice.node_id,
                    "node_instance_id": choice_instance.instance_id,
                },
            )
        branch_scope = (
            (*choice_instance.identity.branch_path, selected_branch_id),
            choice_instance.identity.iteration_vector,
        )
        terminal_instances = tuple(
            sorted(
                (
                    item
                    for terminal_id in branch.terminal_node_ids
                    for item in instances_by_definition.get(terminal_id, ())
                    if item.status in _SUCCESSFUL_NODE_STATUSES
                    and _instance_scope(item) == branch_scope
                ),
                key=lambda item: (
                    item.last_event_sequence,
                    item.identity.activation_ordinal,
                    item.instance_id,
                ),
            )
        )
        if not terminal_instances:
            continue
        parent_scope = _instance_scope(choice_instance)
        evidence_refs = tuple(
            sorted(
                {
                    canonical_checksum(choice_instance.to_dict()),
                    *(
                        canonical_checksum(item.to_dict())
                        for item in terminal_instances
                    ),
                }
            )
        )
        ready.append(
            (
                parent_scope,
                evidence_refs,
                {
                    "choice_node_id": choice.node_id,
                    "choice_node_instance_id": choice_instance.instance_id,
                    "selected_branch_id": selected_branch_id,
                    "terminal_node_instance_ids": [
                        item.instance_id for item in terminal_instances
                    ],
                },
            )
        )
    return tuple(sorted(ready, key=lambda item: _scope_sort_key(item[0])))


def _loop_join_ready_scopes(
    definition: HarnessControlNode,
    nodes_by_id: Mapping[str, HarnessGraphNode],
    instances_by_definition: Mapping[str, tuple[HarnessNodeInstanceState, ...]],
) -> tuple[
    tuple[
        tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
        tuple[str, ...],
        dict[str, Any],
    ],
    ...,
]:
    loop_node_id = definition.metadata.get("loop_node_id")
    loop = nodes_by_id.get(loop_node_id) if isinstance(loop_node_id, str) else None
    if (
        not isinstance(loop, HarnessControlNode)
        or loop.node_kind is not HarnessGraphNodeKind.LOOP_GUARD
        or loop.loop is None
    ):
        raise HarnessValidationError(
            "Loop join cannot resolve its bounded Loop guard",
            code="graph_evaluator_loop_join_mismatch",
            details={
                "loop_join_node_id": definition.node_id,
                "loop_node_id": loop_node_id,
            },
        )

    declared_route_ids = {branch.branch_id for branch in definition.branches}
    selected_by_scope: dict[
        tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
        tuple[HarnessNodeInstanceState, str],
    ] = {}
    for loop_instance in instances_by_definition.get(loop.node_id, ()):
        if loop_instance.status not in _SUCCESSFUL_NODE_STATUSES:
            continue
        selected_route_id = loop_instance.metadata.get("selected_loop_route_id")
        if selected_route_id in {None, "continue"}:
            continue
        if (
            not isinstance(selected_route_id, str)
            or selected_route_id not in declared_route_ids
        ):
            raise HarnessValidationError(
                "completed Loop guard has an invalid durable route selection",
                code="graph_evaluator_loop_route_invalid",
                details={
                    "loop_node_id": loop.node_id,
                    "node_instance_id": loop_instance.instance_id,
                    "selected_route_id": selected_route_id,
                },
            )
        scope = _instance_scope(loop_instance)
        if scope in selected_by_scope:
            raise HarnessValidationError(
                "bounded Loop has multiple terminal route selections in one scope",
                code="graph_evaluator_loop_route_ambiguous",
                details={
                    "loop_node_id": loop.node_id,
                    "node_instance_ids": sorted(
                        {
                            selected_by_scope[scope][0].instance_id,
                            loop_instance.instance_id,
                        }
                    ),
                },
            )
        selected_by_scope[scope] = (loop_instance, selected_route_id)

    ready: list[
        tuple[
            tuple[tuple[str, ...], tuple[HarnessLoopIteration, ...]],
            tuple[str, ...],
            dict[str, Any],
        ]
    ] = []
    for scope, (loop_instance, selected_route_id) in sorted(
        selected_by_scope.items(),
        key=lambda item: _scope_sort_key(item[0]),
    ):
        completed_routes: list[tuple[str, tuple[HarnessNodeInstanceState, ...]]] = []
        for branch in definition.branches:
            terminal_instances = tuple(
                sorted(
                    (
                        item
                        for terminal_id in branch.terminal_node_ids
                        for item in instances_by_definition.get(terminal_id, ())
                        if item.status in _SUCCESSFUL_NODE_STATUSES
                        and _instance_scope(item) == scope
                    ),
                    key=lambda item: (
                        item.last_event_sequence,
                        item.identity.activation_ordinal,
                        item.instance_id,
                    ),
                )
            )
            if terminal_instances:
                completed_routes.append((branch.branch_id, terminal_instances))
        if not completed_routes:
            continue
        if len(completed_routes) > 1:
            raise HarnessValidationError(
                "bounded Loop has multiple successful exit routes in one scope",
                code="graph_evaluator_loop_route_ambiguous",
                details={
                    "loop_node_id": loop.node_id,
                    "node_instance_id": loop_instance.instance_id,
                    "route_ids": sorted(item[0] for item in completed_routes),
                },
            )
        route_id, terminal_instances = completed_routes[0]
        if route_id != selected_route_id:
            raise HarnessValidationError(
                "Loop terminal evidence does not match its durable route selection",
                code="graph_evaluator_loop_route_mismatch",
                details={
                    "loop_node_id": loop.node_id,
                    "selected_route_id": selected_route_id,
                    "completed_route_id": route_id,
                },
            )
        evidence_refs = tuple(
            sorted(
                {
                    canonical_checksum(loop_instance.to_dict()),
                    *(
                        canonical_checksum(item.to_dict())
                        for item in terminal_instances
                    ),
                }
            )
        )
        ready.append(
            (
                scope,
                evidence_refs,
                {
                    "loop_node_id": loop.node_id,
                    "loop_node_instance_id": loop_instance.instance_id,
                    "selected_route_id": route_id,
                    "terminal_node_instance_ids": [
                        item.instance_id for item in terminal_instances
                    ],
                },
            )
        )
    return tuple(ready)


def _condition_context(
    graph: NormalizedHarnessGraph | None,
    state: HarnessGraphState,
    node: HarnessControlNode,
    instance: HarnessNodeInstanceState,
    context: HarnessGraphEvaluationContext,
) -> dict[str, Any]:
    source_instance = (
        _loop_condition_source(state, node, instance)
        if node.loop is not None and _condition_requires_source(node.loop.condition)
        else None
    )
    if source_instance is None:
        source_node_id = _single_dependency_source(graph, node.node_id)
        source_instance = _source_instance_for_control(
            state,
            source_node_id,
            instance,
        )
    source_observations = (
        ()
        if source_instance is None
        else context.for_node_instance(source_instance.instance_id)
    )
    verified_output = _single_observation_payload(
        source_observations,
        HarnessGraphObservationType.VERIFIED_OUTPUT,
    )
    worker_status = _single_observation_payload(
        source_observations,
        HarnessGraphObservationType.WORKER_STATUS,
    )
    quality_verdict = _single_observation_payload(
        source_observations,
        HarnessGraphObservationType.QUALITY_VERDICT,
    )
    gate_results = {
        item.contract_ref.contract_id: thaw_json(item.payload)
        for item in source_observations
        if item.observation_type is HarnessGraphObservationType.GATE_RESULT
    }
    return {
        "graph": {
            "inputs": thaw_json(context.inputs),
            "outputs": _graph_verified_outputs(context),
        },
        "node": {
            "outputs": verified_output,
            "outcome": None
            if source_instance is None
            else source_instance.status.value,
        },
        "worker_result": worker_status,
        "quality_verdict": quality_verdict,
        "gate_results": gate_results,
        "run": {
            "lifecycle": state.lifecycle.value,
            "outcome": state.outcome.value,
        },
    }


def _wait_registration_projection(
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    node: HarnessControlNode,
    instance: HarnessNodeInstanceState,
    context: HarnessGraphEvaluationContext,
) -> dict[str, Any]:
    if node.wait is None:
        raise HarnessValidationError(
            "Wait node is missing its normalized contract",
            code="graph_evaluator_wait_contract_missing",
        )
    runtime_context = _wait_runtime_context(
        graph,
        state,
        node,
        instance,
        context,
    )
    signal_schema_ref = exact_reference(
        f"{node.wait.signal_type}@{node.wait.signal_version}",
        "wait.signal_schema_ref",
    )
    try:
        correlation = _resolve_wait_template(
            thaw_json(node.wait.correlation),
            runtime_context,
            field_name="correlation",
        )
        tenant_scope = _resolve_wait_path(
            node.wait.tenant_scope_path,
            runtime_context,
            field_name="tenant_scope",
        )
        identity_scope = _resolve_wait_path(
            node.wait.identity_scope_path,
            runtime_context,
            field_name="identity_scope",
        )
        deadline_ref = None
        if node.wait.deadline_input_path is not None:
            deadline = _resolve_wait_path(
                node.wait.deadline_input_path,
                runtime_context,
                field_name="deadline",
            )
            deadline_ref = _reference_or_checksum(deadline)
        projection = {
            "wait_id": node.wait.wait_id,
            "kind": node.wait.kind.value,
            "correlation_ref": canonical_checksum(correlation),
            "tenant_scope_ref": _reference_or_checksum(tenant_scope),
            "identity_scope_ref": _reference_or_checksum(identity_scope),
            "signal_schema_ref": signal_schema_ref,
            "deadline_ref": deadline_ref,
            "resolved": True,
        }
        if node.wait.resume_node_id is not None:
            # A re-entrant approval Wait records the exact grant it waits on so
            # the resumed node can redeem it without trusting caller input.
            projection["approval_id"] = _wait_approval_id(correlation, node)
        return projection
    except HarnessValidationError as exc:
        if exc.code != "wait_registration_source_missing":
            raise
        # Unit-level evaluator calls may omit live input observations. Keep the
        # candidate deterministic, but the Control Plane rejects this marker
        # before it can create an unsafe registration.
        def unresolved(path: str) -> str:
            return canonical_checksum({"unresolved_path": path})
        return {
            "wait_id": node.wait.wait_id,
            "kind": node.wait.kind.value,
            "correlation_ref": canonical_checksum(
                {"unresolved_correlation": thaw_json(node.wait.correlation)}
            ),
            "tenant_scope_ref": unresolved(node.wait.tenant_scope_path),
            "identity_scope_ref": unresolved(node.wait.identity_scope_path),
            "signal_schema_ref": signal_schema_ref,
            "deadline_ref": (
                None
                if node.wait.deadline_input_path is None
                else unresolved(node.wait.deadline_input_path)
            ),
            "resolved": False,
        }


def _wait_approval_id(
    correlation: Mapping[str, Any],
    node: HarnessControlNode,
) -> str:
    approval_id = correlation.get("approval_id")
    if not isinstance(approval_id, str) or not approval_id.strip():
        raise HarnessValidationError(
            "re-entrant approval Wait resolved no approval_id",
            code="wait_resume_approval_id_missing",
            details={"wait_node_id": node.node_id},
        )
    return approval_id.strip()


def _wait_runtime_context(
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    node: HarnessControlNode,
    instance: HarnessNodeInstanceState,
    context: HarnessGraphEvaluationContext,
) -> dict[str, Any]:
    visible = _wait_visible_verified_outputs(
        graph,
        state,
        node,
        instance,
        context,
    )
    by_definition: dict[str, tuple[HarnessNodeInstanceState, Mapping[str, Any], int]] = {}
    for producer, payload, distance in visible:
        previous = by_definition.get(producer.identity.node_id)
        if previous is None or (
            producer.identity.activation_ordinal,
            producer.last_event_sequence,
            producer.instance_id,
        ) > (
            previous[0].identity.activation_ordinal,
            previous[0].last_event_sequence,
            previous[0].instance_id,
        ):
            by_definition[producer.identity.node_id] = (
                producer,
                payload,
                distance,
            )

    graph_outputs: dict[str, Any] = {}
    output_candidates: dict[
        str,
        list[tuple[HarnessNodeInstanceState, Mapping[str, Any], int]],
    ] = defaultdict(list)
    for candidate in by_definition.values():
        for key in candidate[1]:
            output_candidates[str(key)].append(candidate)
    for key, candidates in sorted(output_candidates.items()):
        nearest = min(item[2] for item in candidates)
        selected = tuple(item for item in candidates if item[2] == nearest)
        if len(selected) != 1:
            raise HarnessValidationError(
                "Wait graph output has no unique scope-visible producer",
                code="wait_output_source_ambiguous",
                details={
                    "wait_node_id": node.node_id,
                    "output_key": key,
                    "producer_node_instance_ids": sorted(
                        item[0].instance_id for item in selected
                    ),
                },
            )
        graph_outputs[key] = thaw_json(selected[0][1][key])

    node_outputs = {
        node_id: thaw_json(payload)
        for node_id, (_, payload, _) in sorted(by_definition.items())
    }
    return {
        "graph": {
            "inputs": thaw_json(context.inputs),
            "outputs": graph_outputs,
        },
        "node": {"outputs": node_outputs},
    }


def _wait_visible_verified_outputs(
    graph: NormalizedHarnessGraph,
    state: HarnessGraphState,
    node: HarnessControlNode,
    instance: HarnessNodeInstanceState,
    context: HarnessGraphEvaluationContext,
) -> tuple[tuple[HarnessNodeInstanceState, Mapping[str, Any], int], ...]:
    instances = {item.instance_id: item for item in state.node_instances}
    definitions = {item.node_id: item for item in graph.nodes}
    visible: list[tuple[HarnessNodeInstanceState, Mapping[str, Any], int]] = []
    for observation in context.observations:
        if (
            observation.observation_type
            is not HarnessGraphObservationType.VERIFIED_OUTPUT
        ):
            continue
        producer = instances.get(observation.node_instance_id)
        if (
            producer is None
            or producer.status not in _SUCCESSFUL_NODE_STATUSES
            or producer.attempt != observation.attempt
            or producer.identity.activation_ordinal
            >= instance.identity.activation_ordinal
            or not _wait_scope_compatible(
                producer.identity.branch_path,
                instance.identity.branch_path,
            )
            or not _wait_scope_compatible(
                producer.identity.iteration_vector,
                instance.identity.iteration_vector,
            )
        ):
            continue
        distance = _wait_graph_path_distance(
            graph,
            producer.identity.node_id,
            node.node_id,
        )
        if distance is None:
            continue
        payload = thaw_json(observation.payload)
        if not isinstance(payload, Mapping):
            raise HarnessValidationError(
                "Wait source observation payload must be an object",
                code="invalid_graph_observation_payload",
            )
        definition = definitions.get(producer.identity.node_id)
        if not isinstance(definition, HarnessExecutableNode):
            raise HarnessValidationError(
                "Wait verified output must belong to an executable producer",
                code="wait_output_source_invalid",
            )
        visible.append(
            (
                producer,
                _wait_output_projection(definition, payload),
                distance,
            )
        )
    return tuple(
        sorted(
            visible,
            key=lambda item: (
                item[2],
                item[0].identity.activation_ordinal,
                item[0].instance_id,
            ),
        )
    )


def _wait_output_projection(
    definition: HarnessExecutableNode,
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    if len(definition.output_keys) == 1:
        output_key = definition.output_keys[0]
        # Physical Graph workers return an exact output-key mapping. Legacy
        # workers may still return the value directly; both shapes project to
        # the same normalized node output without duplicating the key.
        if set(payload) == {output_key}:
            return {output_key: thaw_json(payload[output_key])}
        return {output_key: thaw_json(payload)}
    projected = {
        output_key: thaw_json(payload[output_key])
        for output_key in definition.output_keys
        if output_key in payload
    }
    if not projected:
        raise HarnessValidationError(
            "Wait verified output does not expose a declared output key",
            code="wait_output_source_missing",
            details={"producer_node_id": definition.node_id},
        )
    return projected


def _resolve_wait_template(
    value: Any,
    context: Mapping[str, Any],
    *,
    field_name: str,
) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _resolve_wait_template(
                child,
                context,
                field_name=f"{field_name}.{key}",
            )
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, str):
        return _resolve_wait_path(value, context, field_name=field_name)
    raise HarnessValidationError(
        "Wait correlation contains a non-structural runtime source",
        code="invalid_wait_correlation_source",
        details={"field": field_name},
    )


def _resolve_wait_path(
    path: str,
    context: Mapping[str, Any],
    *,
    field_name: str,
) -> Any:
    value = resolve_condition_path(path, context)
    if value is None:
        raise HarnessValidationError(
            "Wait registration source is absent from accepted graph data",
            code="wait_registration_source_missing",
            details={"field": field_name, "path": path},
        )
    return value


def _reference_or_checksum(value: Any) -> str:
    if isinstance(value, str) and _CHECKSUM_PATTERN.fullmatch(value) is not None:
        return value
    return canonical_checksum(value)


def _wait_graph_path_distance(
    graph: NormalizedHarnessGraph,
    source_id: str,
    target_id: str,
) -> int | None:
    if source_id == target_id:
        return 0
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source_id].append(edge.target_id)
    frontier: list[tuple[str, int]] = [(source_id, 0)]
    visited = {source_id}
    while frontier:
        current, distance = frontier.pop(0)
        for target in sorted(adjacency.get(current, ())):
            if target == target_id:
                return distance + 1
            if target in visited:
                continue
            visited.add(target)
            frontier.append((target, distance + 1))
    return None


def _wait_scope_compatible(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    common = min(len(left), len(right))
    return left[:common] == right[:common]


def _condition_requires_source(condition: HarnessCondition) -> bool:
    if isinstance(condition, ConditionPredicate):
        return condition.path.startswith(
            (
                "node.",
                "worker_result.",
                "quality_verdict.",
                "gate_results.",
            )
        )
    if isinstance(condition, ConditionAll | ConditionAny):
        return any(_condition_requires_source(child) for child in condition.conditions)
    raise TypeError("condition must be a HarnessCondition")


def _loop_condition_source(
    state: HarnessGraphState,
    node: HarnessControlNode,
    control_instance: HarnessNodeInstanceState,
) -> HarnessNodeInstanceState | None:
    if node.node_kind is not HarnessGraphNodeKind.LOOP_GUARD or node.loop is None:
        return None
    counter = _loop_counter_for(control_instance, state.loop_counters)
    if counter is None or counter.completed_iterations == 0:
        return None
    previous_iteration = (
        *control_instance.identity.iteration_vector,
        HarnessLoopIteration(node.node_id, counter.completed_iterations - 1),
    )
    matches = tuple(
        item
        for item in state.node_instances
        if item.identity.node_id in node.loop.body_terminal_node_ids
        and item.identity.branch_path == control_instance.identity.branch_path
        and item.identity.iteration_vector == previous_iteration
        and item.status in _SUCCESSFUL_NODE_STATUSES
    )
    if len(matches) != 1:
        raise HarnessValidationError(
            "Loop continuation requires one exact prior-iteration terminal source",
            code="graph_evaluator_loop_condition_source_ambiguous",
            details={
                "loop_node_id": node.node_id,
                "completed_iterations": counter.completed_iterations,
                "source_node_instance_ids": sorted(
                    item.instance_id for item in matches
                ),
            },
        )
    return matches[0]


def _source_instance_for_control(
    state: HarnessGraphState,
    source_node_id: str | None,
    control_instance: HarnessNodeInstanceState,
) -> HarnessNodeInstanceState | None:
    if source_node_id is None:
        return None
    matches = tuple(
        item
        for item in state.node_instances
        if item.identity.node_id == source_node_id
        and item.identity.branch_path == control_instance.identity.branch_path
        and item.identity.iteration_vector == control_instance.identity.iteration_vector
    )
    if not matches:
        return None
    return max(
        matches,
        key=lambda item: (
            item.last_event_sequence,
            item.identity.activation_ordinal,
            item.instance_id,
        ),
    )


def _single_observation_payload(
    observations: tuple[HarnessAcceptedGraphObservation, ...],
    observation_type: HarnessGraphObservationType,
) -> Any:
    matches = tuple(
        item for item in observations if item.observation_type is observation_type
    )
    if not matches:
        return (
            None
            if observation_type is not HarnessGraphObservationType.VERIFIED_OUTPUT
            else {}
        )
    if len(matches) > 1:
        raise HarnessValidationError(
            "graph control context contains duplicate observation kinds",
            code="ambiguous_graph_observation",
            details={"observation_type": observation_type.value},
        )
    return thaw_json(matches[0].payload)


def _graph_verified_outputs(
    context: HarnessGraphEvaluationContext,
) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for observation in context.observations:
        if (
            observation.observation_type
            is not HarnessGraphObservationType.VERIFIED_OUTPUT
        ):
            continue
        payload = thaw_json(observation.payload)
        if not isinstance(payload, Mapping):
            raise HarnessValidationError(
                "verified output observation payload must be an object",
                code="invalid_graph_observation_payload",
            )
        for key in sorted(payload):
            outputs[str(key)] = payload[key]
    return outputs


def _single_dependency_source(
    graph: NormalizedHarnessGraph | None,
    node_id: str,
) -> str | None:
    if graph is None:
        return None
    sources = tuple(
        sorted(
            {
                edge.source_id
                for edge in graph.edges
                if edge.target_id == node_id
                and edge.edge_kind is HarnessGraphEdgeKind.DEPENDENCY
            }
        )
    )
    return sources[0] if len(sources) == 1 else None


def _join_for_fork(
    graph: NormalizedHarnessGraph,
    fork_node_id: str,
) -> HarnessControlNode | None:
    matches = tuple(
        node
        for node in graph.nodes
        if isinstance(node, HarnessControlNode)
        and node.join is not None
        and node.join.fork_node_id == fork_node_id
    )
    return matches[0] if matches else None


def _loop_counter_for(
    instance: HarnessNodeInstanceState,
    counters: tuple[HarnessLoopCounterState, ...],
) -> HarnessLoopCounterState | None:
    matches = tuple(
        item
        for item in counters
        if item.loop_id == instance.identity.node_id
        and item.branch_path == instance.identity.branch_path
        and item.parent_iteration_vector == instance.identity.iteration_vector
    )
    if len(matches) > 1:
        raise HarnessValidationError(
            "loop guard scope resolves multiple counters",
            code="graph_evaluator_loop_counter_ambiguous",
        )
    return matches[0] if matches else None


def _compensation_mode(state: HarnessGraphState) -> bool:
    return state.metadata.get("execution_mode") == "compensating"


def _terminal_observation_candidate(
    state: HarnessGraphState,
    context: HarnessGraphEvaluationContext,
) -> HarnessGraphCandidate | None:
    instances = {item.instance_id: item for item in state.node_instances}
    failures = tuple(
        observation
        for observation in context.observations
        if observation.observation_type
        is HarnessGraphObservationType.SIDE_EFFECT_FAILURE
        and observation.node_instance_id in instances
        and instances[observation.node_instance_id].attempt == observation.attempt
    )
    if failures:
        selected_failure = min(
            failures,
            key=lambda item: (
                item.event_sequence,
                item.node_instance_id,
                item.observation_checksum,
            ),
        )
        if (
            selected_failure.payload.get("reason_code")
            == "side_effect_attempt_indeterminate"
        ):
            return HarnessGraphCandidate(
                HarnessGraphCandidateType.HALT_RUN,
                "side_effect_attempt_indeterminate",
                0,
                node_id=selected_failure.node_id,
                node_instance_id=selected_failure.node_instance_id,
                evidence_refs=(selected_failure.evidence_ref,),
                payload={
                    "outcome": "indeterminate",
                    "manual_intervention_required": True,
                },
            )
        return HarnessGraphCandidate(
            HarnessGraphCandidateType.COMPLETE_RUN,
            "side_effect_retry_exhausted",
            0,
            evidence_refs=(selected_failure.evidence_ref,),
            payload={"outcome": "failed"},
        )
    cancellations = tuple(
        observation
        for observation in context.observations
        if observation.observation_type is HarnessGraphObservationType.APPROVAL
        and observation.payload.get("approved") is False
        and observation.node_instance_id in instances
        and instances[observation.node_instance_id].status
        is HarnessNodeInstanceStatus.WAITING
        and instances[observation.node_instance_id].attempt == observation.attempt
    )
    if not cancellations:
        return None
    selected = min(
        cancellations,
        key=lambda item: (
            item.event_sequence,
            item.node_instance_id,
            item.observation_checksum,
        ),
    )
    return HarnessGraphCandidate(
        HarnessGraphCandidateType.COMPLETE_RUN,
        "approval_cancelled",
        0,
        evidence_refs=(selected.evidence_ref,),
        payload={"outcome": "cancelled"},
    )


def _validate_status_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(payload, {"status"}, "worker status")
    required_text(payload["status"], "graph_observation.payload.status")


def _validate_quality_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(payload, {"passed", "score"}, "quality verdict")
    if not isinstance(payload["passed"], bool):
        raise HarnessValidationError(
            "quality verdict observation passed value must be boolean",
            code="invalid_graph_observation_payload",
        )
    score = payload["score"]
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, int | float)
        or not math.isfinite(float(score))
        or not 0 <= float(score) <= 1
    ):
        raise HarnessValidationError(
            "quality verdict observation score must be between zero and one",
            code="invalid_graph_observation_payload",
        )


def _validate_gate_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(
        payload,
        {"passed", "input_ref", "result_ref", "reason_code"},
        "Gate result",
    )
    if not isinstance(payload["passed"], bool):
        raise HarnessValidationError(
            "Gate result observation requires a boolean passed value",
            code="invalid_graph_observation_payload",
        )
    _checksum(payload["input_ref"], "graph_observation.payload.input_ref")
    _checksum(payload["result_ref"], "graph_observation.payload.result_ref")
    required_text(payload["reason_code"], "graph_observation.payload.reason_code")


def _validate_approval_payload(payload: Mapping[str, Any]) -> None:
    approved = payload.get("approved")
    if not isinstance(approved, bool):
        raise HarnessValidationError(
            "approval observation requires a boolean approved value",
            code="invalid_graph_observation_payload",
        )
    if approved:
        _exact_payload_keys(payload, {"approved", "approval_ref"}, "approval")
        _checksum(payload["approval_ref"], "graph_observation.payload.approval_ref")
    else:
        _exact_payload_keys(payload, {"approved", "reason_ref"}, "approval")
        _checksum(payload["reason_ref"], "graph_observation.payload.reason_ref")


def _validate_wait_cause_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(payload, {"cause_kind", "record"}, "Wait cause")
    cause_kind = HarnessWaitCauseKind(payload["cause_kind"])
    record = payload["record"]
    if not isinstance(record, Mapping):
        raise HarnessValidationError(
            "Wait cause record must be an object",
            code="invalid_graph_observation_payload",
        )
    if cause_kind is HarnessWaitCauseKind.SIGNAL:
        HarnessWaitSignal.from_dict(record)
    elif cause_kind is HarnessWaitCauseKind.TIMER:
        HarnessWaitTimerWakeRecord.from_dict(record)
    elif cause_kind is HarnessWaitCauseKind.TIMEOUT:
        HarnessWaitTimeoutRecord.from_dict(record)
    elif cause_kind is HarnessWaitCauseKind.APPROVAL:
        HarnessWaitApprovalEvidenceRecord.from_dict(record)
    elif cause_kind is HarnessWaitCauseKind.CANCELLATION:
        HarnessWaitCancellationRecord.from_dict(record)
    else:  # pragma: no cover - enum exhaustiveness guard
        raise AssertionError(f"unsupported Wait cause kind: {cause_kind.value}")


def _validate_run_operation_payload(
    payload: Mapping[str, Any],
) -> HarnessGraphRunOperation:
    _exact_payload_keys(payload, {"record"}, "run operation")
    record = payload["record"]
    if not isinstance(record, Mapping):
        raise HarnessValidationError(
            "run operation record must be an object",
            code="invalid_graph_observation_payload",
        )
    try:
        return HarnessGraphRunOperation.from_dict(record)
    except (HarnessValidationError, TypeError, ValueError) as exc:
        raise HarnessValidationError(
            "run operation record violates its typed contract",
            code="invalid_graph_observation_payload",
        ) from exc


def _validate_side_effect_failure_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(
        payload,
        {
            "decision_ref",
            "failure_ref",
            "reason_code",
            "causal_graph_decision_checksum",
        },
        "side-effect failure",
    )
    _checksum(payload["decision_ref"], "graph_observation.payload.decision_ref")
    _checksum(payload["failure_ref"], "graph_observation.payload.failure_ref")
    _checksum(
        payload["causal_graph_decision_checksum"],
        "graph_observation.payload.causal_graph_decision_checksum",
    )
    required_text(
        payload["reason_code"],
        "graph_observation.payload.reason_code",
    )


def _validate_side_effect_outcome_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(
        payload,
        {
            "scope",
            "prepare_decision_ref",
            "decision_ref",
            "outcome_ref",
            "effect_ref",
            "disposition",
        },
        "side-effect outcome",
    )
    scope = required_text(payload["scope"], "graph_observation.payload.scope")
    if scope not in {"node_instance", "terminal_run"}:
        raise HarnessValidationError(
            "side-effect outcome scope is unsupported",
            code="invalid_graph_observation_payload",
        )
    for field_name in (
        "prepare_decision_ref",
        "decision_ref",
        "outcome_ref",
        "effect_ref",
    ):
        _checksum(
            payload[field_name],
            f"graph_observation.payload.{field_name}",
        )
    disposition = required_text(
        payload["disposition"],
        "graph_observation.payload.disposition",
    )
    if disposition not in {"prepared", "accepted"}:
        raise HarnessValidationError(
            "side-effect outcome disposition is not a successful forward disposition",
            code="invalid_graph_observation_payload",
        )


def _validate_merge_result_payload(payload: Mapping[str, Any]) -> None:
    _exact_payload_keys(
        payload,
        {
            "operation_id",
            "input_checksum",
            "input_refs",
            "succeeded",
            "output_refs",
            "outputs",
            "reason_code",
        },
        "merge result",
    )
    _checksum(payload["operation_id"], "graph_observation.payload.operation_id")
    _checksum(
        payload["input_checksum"],
        "graph_observation.payload.input_checksum",
    )
    if not isinstance(payload["succeeded"], bool):
        raise HarnessValidationError(
            "merge result succeeded value must be boolean",
            code="invalid_graph_observation_payload",
        )
    input_refs = payload["input_refs"]
    if not isinstance(input_refs, tuple):
        raise HarnessValidationError(
            "merge result input_refs must be an array",
            code="invalid_graph_observation_payload",
        )
    if not all(isinstance(item, Mapping) for item in input_refs):
        raise HarnessValidationError(
            "merge result input reference must be an object",
            code="invalid_graph_observation_payload",
        )
    output_refs = payload["output_refs"]
    outputs = payload["outputs"]
    if not isinstance(output_refs, Mapping) or not isinstance(outputs, Mapping):
        raise HarnessValidationError(
            "merge result outputs must be objects",
            code="invalid_graph_observation_payload",
        )
    for key, reference in output_refs.items():
        required_text(key, "graph_observation.payload.output_key")
        _checksum(reference, "graph_observation.payload.output_ref")
    if set(output_refs) != set(outputs):
        raise HarnessValidationError(
            "merge result output values and references must align",
            code="invalid_graph_observation_payload",
        )
    required_text(payload["reason_code"], "graph_observation.payload.reason_code")


def _declared_control_fact_paths(
    definition: HarnessExecutableNode,
) -> tuple[str, ...]:
    step_metadata = definition.metadata.get("step_metadata", {})
    if not isinstance(step_metadata, Mapping):
        raise HarnessValidationError(
            "normalized Step metadata must be an object",
            code="invalid_graph_control_fact_contract",
        )
    raw_paths = step_metadata.get("control_fact_paths", ())
    if not isinstance(raw_paths, tuple | list):
        raise HarnessValidationError(
            "control_fact_paths must be an array",
            code="invalid_graph_control_fact_contract",
        )
    return _control_fact_paths(raw_paths)


def _control_fact_paths(values: Any) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, tuple | list):
        raise HarnessValidationError(
            "control fact paths must be an array",
            code="invalid_graph_control_fact_contract",
        )
    paths = tuple(
        sorted(
            required_text(item, "graph_observation.control_fact_paths")
            for item in values
        )
    )
    if len(paths) != len(set(paths)):
        raise HarnessValidationError(
            "control fact paths must be unique",
            code="invalid_graph_control_fact_contract",
        )
    for path in paths:
        segments = path.split(".")
        if any(not segment for segment in segments):
            raise HarnessValidationError(
                "control fact path contains an empty segment",
                code="invalid_graph_control_fact_contract",
                details={"path": path},
            )
    for index, path in enumerate(paths):
        if any(other.startswith(f"{path}.") for other in paths[index + 1 :]):
            raise HarnessValidationError(
                "control fact paths cannot overlap",
                code="invalid_graph_control_fact_contract",
                details={"path": path},
            )
    return paths


def _project_control_fact_payload(
    payload: Mapping[str, Any],
    paths: tuple[str, ...],
) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    missing = object()
    for path in paths:
        value: Any = payload
        for segment in path.split("."):
            if not isinstance(value, Mapping) or segment not in value:
                value = missing
                break
            value = value[segment]
        if value is missing:
            raise HarnessValidationError(
                "verified control fact is absent from its payload",
                code="graph_observation_control_fact_missing",
                details={"path": path},
            )
        target = projected
        segments = path.split(".")
        for segment in segments[:-1]:
            child = target.setdefault(segment, {})
            if not isinstance(child, dict):
                raise HarnessValidationError(
                    "control fact paths overlap",
                    code="invalid_graph_control_fact_contract",
                )
            target = child
        target[segments[-1]] = value
    return projected


def _exact_payload_keys(
    payload: Mapping[str, Any],
    expected: set[str],
    field_name: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        raise HarnessValidationError(
            f"{field_name} observation fields do not match its schema",
            code="invalid_graph_observation_payload",
            details={
                "missing": sorted(expected.difference(actual)),
                "unknown": sorted(str(item) for item in actual.difference(expected)),
            },
        )


def _stable_text_tuple(values, field_name: str) -> tuple[str, ...]:
    normalized = tuple(required_text(item, field_name) for item in values)
    if len(normalized) != len(set(normalized)):
        raise HarnessValidationError(
            f"{field_name} must not contain duplicates",
            code="duplicate_graph_evaluation_identity",
        )
    return tuple(sorted(normalized))


def _ordered_unique_text_tuple(values, field_name: str) -> tuple[str, ...]:
    normalized = tuple(required_text(item, field_name) for item in values)
    if len(normalized) != len(set(normalized)):
        raise HarnessValidationError(
            f"{field_name} must not contain duplicates",
            code="duplicate_graph_evaluation_identity",
        )
    return normalized


def _optional_text(value: Any, field_name: str) -> str | None:
    return None if value is None else required_text(value, field_name)


def _checksum(value: Any, field_name: str) -> str:
    normalized = required_text(value, field_name)
    if _CHECKSUM_PATTERN.fullmatch(normalized) is None:
        raise HarnessValidationError(
            f"{field_name} must be a canonical sha256 reference",
            code="invalid_graph_evaluation_checksum",
        )
    return normalized


__all__ = [
    "GraphEvaluation",
    "HarnessAcceptedGraphObservation",
    "HarnessGraphCandidate",
    "HarnessGraphCandidateType",
    "HarnessGraphEvaluationContext",
    "HarnessGraphObservationType",
    "HarnessGraphEvaluator",
    "merge_branch_output_references",
]
