from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from framework.events.canonical import (
    PayloadReference,
    checksum_for,
    normalize_canonical_json,
    thaw_canonical_json,
)
from framework.events.errors import EventStoreCorruptionError
from framework.events.runtime.activities import (
    RecordedActivityResolver,
    RecordedActivityStorePort,
    ReplayActivityCorruptionError,
    ReplayActivityDescriptor,
    ReplayActivityHandlerVersion,
    ReplayActivityKind,
    ReplayActivityMissingError,
    ReplayActivityRegistry,
    ReplayActivityResolverPort,
    ResolvedReplayActivity,
)
from framework.events.runtime.history import (
    DeterministicCommand,
    DeterministicHistoryRecord,
    ExactVersionRegistration,
    ExactVersionRegistry,
    HistoryEventPolicy,
    HistoryVerifier,
)
from framework.harness.control_plane.event import HarnessEvent
from framework.harness.graph.decision import HarnessGraphDecision
from framework.harness.control_plane.graph_state import HarnessGraphState
from framework.harness.graph.model import NormalizedHarnessGraph
from framework.harness.control_plane.graph_runtime import (
    HARNESS_GRAPH_COMMIT_SCHEMA,
    HARNESS_GRAPH_PROJECTION_RECORD_SCHEMA,
)
from framework.harness.control_plane.transition import (
    HARNESS_GRAPH_CONTROL_POLICY_VERSION,
    HARNESS_GRAPH_HISTORY_SCHEMA,
    HARNESS_GRAPH_PROJECTION_HISTORY_SCHEMA,
    HARNESS_GRAPH_REDUCER_VERSION,
)
from framework.harness.quality.verdict import gate_result_evidence


HARNESS_HISTORY_HANDLER_ID = "harness-control-plane"
HARNESS_HISTORY_POLICY_ID = "harness-control-policy"
HARNESS_HISTORY_SCHEMA_ID = "newsroom.harness-graph-event"
HARNESS_GRAPH_HISTORY_SCHEMA_ID = HARNESS_GRAPH_HISTORY_SCHEMA.removesuffix("/v1")
HARNESS_GRAPH_PROJECTION_HISTORY_SCHEMA_ID = HARNESS_GRAPH_PROJECTION_HISTORY_SCHEMA.removesuffix("/v1")
HARNESS_EVENT_HISTORY_GRAPH_ID = "harness-events"
HARNESS_EVENT_HISTORY_GRAPH_VERSION = "1"
HARNESS_GRAPH_ACTIVITY_HISTORY_GRAPH_ID = "harness-graph-activity"
HARNESS_DECISION_INPUT_SCHEMA = "newsroom.harness-graph-decision-input/v3"

_ACTIVITY_KIND_BY_TYPE = {
    "artifact": ReplayActivityKind.PUBLICATION,
    "function": ReplayActivityKind.TOOL,
    "clock": ReplayActivityKind.CLOCK,
    "email": ReplayActivityKind.EMAIL,
    "external_database": ReplayActivityKind.EXTERNAL_DATABASE,
    "http": ReplayActivityKind.HTTP,
    "llm": ReplayActivityKind.LLM,
    # AgentLoop is a Harness-controlled LLM worker whose durable activity
    # lineage uses the same replay family as direct LLM execution.
    "agent_loop": ReplayActivityKind.LLM,
    # TaskPlan is a composite Harness worker. Its child LLM and subagent calls
    # retain their own durable lineage, while the outer Graph activity replays
    # through the same tool family as other composite worker boundaries.
    "task_plan": ReplayActivityKind.TOOL,
    "mcp": ReplayActivityKind.MCP,
    "memory": ReplayActivityKind.MEMORY_WRITE,
    "publication": ReplayActivityKind.PUBLICATION,
    "random": ReplayActivityKind.RANDOM,
    "retrieval": ReplayActivityKind.RETRIEVAL,
    "script": ReplayActivityKind.TOOL,
    "skill": ReplayActivityKind.TOOL,
    "skill_evolution": ReplayActivityKind.TOOL,
    "subagent": ReplayActivityKind.TOOL,
    "tool": ReplayActivityKind.TOOL,
    "quality_gate": ReplayActivityKind.TOOL,
}


def harness_decision_history(
    *,
    graph_id: str,
    graph_version: str,
    command_ordinal: int,
    decision_input: Mapping[str, Any],
    decision: HarnessGraphDecision,
    causation_id: str,
    expected_activity: ReplayActivityDescriptor | None = None,
    recorded_activity_ref: PayloadReference | None = None,
) -> DeterministicHistoryRecord:
    normalized = _decision_input(decision_input)
    if not isinstance(decision, HarnessGraphDecision):
        raise TypeError("decision must be HarnessGraphDecision")
    snapshot_activity = normalized["expected_activity"]
    policy_activity = (
        None if expected_activity is None else expected_activity.to_dict()
    )
    if snapshot_activity != policy_activity:
        raise ValueError(
            "Harness decision input activity conflicts with history policy"
        )
    if (expected_activity is None) != (recorded_activity_ref is None):
        raise ValueError(
            "Harness decision activity and recorded reference must be supplied together"
        )
    return DeterministicHistoryRecord(
        policy=HistoryEventPolicy(
            handler_id=HARNESS_HISTORY_HANDLER_ID,
            handler_version=HARNESS_GRAPH_REDUCER_VERSION,
            graph_id=graph_id,
            graph_version=graph_version,
            policy_id=HARNESS_HISTORY_POLICY_ID,
            policy_version=HARNESS_GRAPH_CONTROL_POLICY_VERSION,
            schema_id=HARNESS_HISTORY_SCHEMA_ID,
            schema_version="newsroom.harness-graph-event/v1",
            expected_activity=expected_activity,
            recorded_activity_ref=recorded_activity_ref,
        ),
        handler_input=normalized,
        commands=(
            _decision_command(
                command_ordinal=command_ordinal,
                decision_input=normalized,
                decision_projection=_decision_projection(decision),
                graph_version=graph_version,
                causation_id=causation_id,
            ),
        ),
    )


def harness_decision_input_snapshot(
    *,
    state: Any,
    graph: NormalizedHarnessGraph | None = None,
    command_ordinal: int,
    causation_id: str,
    gate_results: Sequence[Any] = (),
    quality_verdict: Any | None = None,
    expected_activity: ReplayActivityDescriptor | None = None,
    approval_outcome: str | None = None,
    side_effect_authorization: Mapping[str, Any] | None = None,
    side_effect_failure: Mapping[str, Any] | None = None,
    side_effect_state_refs: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if not isinstance(state, HarnessGraphState):
        raise TypeError("state must be HarnessGraphState")
    if graph is None:
        graph = getattr(state, "graph", None)
    if not isinstance(graph, NormalizedHarnessGraph):
        raise TypeError("graph must be NormalizedHarnessGraph")
    if state.graph_ref.checksum != graph.checksum:
        raise ValueError("state and graph checksums do not match")
    graph_id = graph.graph_id
    graph_version = graph.graph_version
    graph_checksum = graph.checksum
    current_node_instance_id = _current_node_instance_id(state)
    current_node = _node_for_instance(state, current_node_instance_id)
    current_node_id = None if current_node is None else current_node.get("node_id")
    current_definition = _graph_node_for_instance(graph, current_node)
    current_policy = (
        current_definition.to_dict()
        if current_definition is not None
        else {}
    )
    if current_node_id is not None:
        current_policy["repair_node_ids"] = _repair_node_ids_for_source(
            graph,
            source_node_id=str(current_node_id),
        )
    _reject_legacy_repair_step_authority(current_policy)
    if side_effect_authorization is not None:
        current_policy["side_effect_authorization"] = dict(side_effect_authorization)
    if side_effect_failure is not None:
        current_policy["side_effect_failure"] = dict(side_effect_failure)
    routing_values: dict[str, Any] = {
        "quality_verdict.passed": (
            None if quality_verdict is None else quality_verdict.passed
        ),
        "quality_verdict.score": (
            None if quality_verdict is None else quality_verdict.score
        ),
        "run.lifecycle": state.lifecycle.value,
        "run.outcome": state.outcome.value,
        "node.outcome": None if current_node is None else current_node.get("status"),
    }
    if current_node is not None:
        for output_key, output_ref in current_node.get("output_refs", {}).items():
            routing_values[f"node.outputs.{output_key}"] = output_ref
    graph_outputs = state.metadata.get("outputs", {})
    if isinstance(graph_outputs, Mapping):
        for output_key, output_ref in graph_outputs.items():
            routing_values[f"graph.outputs.{output_key}"] = output_ref
    snapshot = {
        "schema": HARNESS_DECISION_INPUT_SCHEMA,
        "command_ordinal": command_ordinal,
        "causation_id": causation_id,
        "run_id": state.run_id,
        "graph_id": graph_id,
        "graph_version": graph_version,
        "graph_checksum": graph_checksum,
        "entry_node_ids": tuple(graph.entry_node_ids),
        "graph_nodes": tuple(node.to_dict() for node in graph.nodes),
        "graph_edges": tuple(edge.to_dict() for edge in graph.edges),
        "current_node_policy": current_policy,
        # Decision replay must bind to the same canonical state projection
        # that is persisted in Harness transitions.  The full in-memory state
        # may contain raw worker/effect metadata which the safe durable
        # projection intentionally omits; hashing it here would make a
        # restarted run produce a different completion input.
        "before_state_checksum": getattr(state, "projection_checksum", None)
        or checksum_for(state.to_dict()),
        "run_status": state.lifecycle.value,
        "current_node_instance_id": current_node_instance_id,
        "current_node_id": current_node_id,
        "turn_count": _graph_budget_used(state, "turns"),
        "replan_count": _graph_budget_used(state, "replans"),
        "worker_call_count": _graph_budget_used(state, "worker_calls"),
        "node_instances": tuple(
            _decision_node_instance_projection(
                node,
                side_effect_state_refs=side_effect_state_refs,
            )
            for node in state.node_instances
        ),
        "budget": _graph_budget_projection(state),
        "gate_results": tuple(
            _gate_decision_evidence(
                result,
                bounded=graph.terminal_policy is not None,
            )
            for result in gate_results
        ),
        "quality_verdict": (
            None if quality_verdict is None else quality_verdict.to_dict()
        ),
        "approval_outcome": approval_outcome,
        "routing_values": routing_values,
        "expected_activity": (
            None
            if expected_activity is None
            else expected_activity.to_dict()
        ),
    }
    return _decision_input(snapshot)


def _current_node_instance_id(state: HarnessGraphState) -> str | None:
    active = tuple(state.active_activities)
    if active:
        return active[0].node_instance_id
    active_nodes = tuple(node for node in state.node_instances if node.is_running or node.is_waiting or node.is_ready)
    if active_nodes:
        return active_nodes[0].instance_id
    return None


def _graph_budget_used(state: HarnessGraphState, name: str) -> int:
    counter = state.budgets.get(name)
    return 0 if counter is None else counter.used


def _graph_budget_projection(state: HarnessGraphState) -> Mapping[str, Any]:
    counters = state.budgets.to_dict()
    limits = {
        item["name"]: item["limit"]
        for item in counters["counters"]
    }
    return {
        **counters,
        "max_turns": limits.get("turns", 0),
        "max_replans": limits.get("replans", 0),
        "max_worker_calls": limits.get("worker_calls", 0),
        "max_retries_per_step": limits.get("retries", 0),
    }


def _node_for_instance(
    state: HarnessGraphState,
    node_instance_id: str | None,
) -> Mapping[str, Any] | None:
    if node_instance_id is None:
        return None
    for node in state.node_instances:
        if node.instance_id == node_instance_id:
            return node.to_dict()
    return None


def _graph_node_for_instance(
    graph: NormalizedHarnessGraph,
    node: Mapping[str, Any] | None,
) -> Any | None:
    if node is None:
        return None
    node_id = node.get("node_id")
    return next((item for item in graph.nodes if item.node_id == node_id), None)


def _repair_node_ids_for_source(
    graph: NormalizedHarnessGraph,
    *,
    source_node_id: str,
) -> dict[str, str]:
    """Project checksum-bound Graph repair routes for one source node."""

    routes: dict[str, str] = {}
    for reference in graph.repair_refs:
        if reference.source_node_id != source_node_id:
            continue
        for trigger in reference.triggers:
            existing = routes.get(trigger)
            if existing is not None and existing != reference.repair_node_id:
                raise ValueError("Graph repair route is ambiguous")
            routes[trigger] = reference.repair_node_id
    return dict(sorted(routes.items()))


def _reject_legacy_repair_step_authority(policy: Mapping[str, Any]) -> None:
    """Legacy leaf repair targets cannot enter the Graph replay contract."""

    if policy.get("repair_step_id") is not None:
        raise ValueError("Graph replay rejects legacy repair_step_id")
    metadata = policy.get("metadata")
    if not isinstance(metadata, Mapping):
        return
    if metadata.get("repair_step_id") is not None:
        raise ValueError("Graph replay rejects legacy repair_step_id")
    retry_policy = metadata.get("retry_policy")
    if isinstance(retry_policy, Mapping) and retry_policy.get("repair_step_id") is not None:
        raise ValueError("Graph replay rejects legacy repair_step_id")


def _repair_node_id(
    policy: Mapping[str, Any],
    *,
    trigger: str,
) -> str | None:
    _reject_legacy_repair_step_authority(policy)
    routes = policy.get("repair_node_ids", {})
    if not isinstance(routes, Mapping):
        raise ValueError("Graph replay repair_node_ids must be an object")
    target = routes.get(trigger)
    if target is None:
        return None
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Graph replay repair node id is invalid")
    return target


def _decision_node_instance_projection(
    node: Any,
    *,
    side_effect_state_refs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw = node.to_dict()
    step_status = raw.get("step_status")
    projection = {
        "node_instance_id": node.instance_id,
        "node_id": node.identity.node_id,
        "step_id": node.step_id,
        "status": node.status.value,
        "step_status": step_status,
        "attempts": node.attempt,
        "replans": node.replans,
        "approval_granted": bool(node.metadata.get("approval_granted")),
    }
    for key in (
        "approval_evidence_ref",
        "side_effect_effect_ref",
        "side_effect_intent_ref",
        "side_effect_decision_ref",
        "side_effect_outcome_ref",
        "side_effect_disposition",
    ):
        value = (
            side_effect_state_refs.get(key)
            if side_effect_state_refs is not None and key in side_effect_state_refs
            else node.metadata.get(key)
        )
        if value is not None:
            projection[key] = value
    return projection


def _gate_decision_evidence(
    result: Any,
    *,
    bounded: bool,
) -> Mapping[str, Any]:
    evidence = gate_result_evidence(result)
    if not bounded:
        return evidence
    # The canonical GATE_EVALUATED event retains the complete reason/score
    # evidence.  Scheduler replay needs only the exact input/result identity
    # and verdict; keeping those fields here avoids duplicating verbose gate
    # diagnostics in every decision extension.
    return {
        key: evidence[key]
        for key in (
            "gate",
            "passed",
            "reference",
            "input_ref",
            "result_ref",
        )
    }


def harness_event_history(event: HarnessEvent, *, data_schema: str) -> DeterministicHistoryRecord:
    if not isinstance(event, HarnessEvent):
        raise TypeError("event must be HarnessEvent")
    return DeterministicHistoryRecord(
        policy=HistoryEventPolicy(
            handler_id=HARNESS_HISTORY_HANDLER_ID,
            handler_version=HARNESS_GRAPH_REDUCER_VERSION,
            graph_id=HARNESS_EVENT_HISTORY_GRAPH_ID,
            graph_version=HARNESS_EVENT_HISTORY_GRAPH_VERSION,
            policy_id=HARNESS_HISTORY_POLICY_ID,
            policy_version=HARNESS_GRAPH_CONTROL_POLICY_VERSION,
            schema_id=HARNESS_HISTORY_SCHEMA_ID,
            schema_version=data_schema,
        ),
        commands=(),
    )


def harness_graph_history(*, data_schema: str) -> DeterministicHistoryRecord:
    if data_schema not in {
        HARNESS_GRAPH_COMMIT_SCHEMA,
        HARNESS_GRAPH_PROJECTION_RECORD_SCHEMA,
    }:
        raise ValueError("unsupported Harness Graph history schema")
    schema_id = (
        HARNESS_GRAPH_PROJECTION_HISTORY_SCHEMA_ID
        if data_schema == HARNESS_GRAPH_PROJECTION_RECORD_SCHEMA
        else HARNESS_GRAPH_HISTORY_SCHEMA_ID
    )
    return DeterministicHistoryRecord(
        policy=HistoryEventPolicy(
            handler_id=HARNESS_HISTORY_HANDLER_ID,
            handler_version=HARNESS_GRAPH_REDUCER_VERSION,
            graph_id=HARNESS_EVENT_HISTORY_GRAPH_ID,
            graph_version=HARNESS_EVENT_HISTORY_GRAPH_VERSION,
            policy_id=HARNESS_HISTORY_POLICY_ID,
            policy_version=HARNESS_GRAPH_CONTROL_POLICY_VERSION,
            schema_id=schema_id,
            schema_version=data_schema,
        ),
        commands=(),
    )


def harness_transition_history(
    transition: Any,
) -> DeterministicHistoryRecord:
    """Build a history policy for a Graph commit; legacy transitions are rejected."""
    schema = getattr(transition, "schema_version", None)
    if schema != HARNESS_GRAPH_COMMIT_SCHEMA:
        raise ValueError("legacy Harness transition history is not supported")
    return DeterministicHistoryRecord(
        policy=HistoryEventPolicy(
            handler_id=HARNESS_HISTORY_HANDLER_ID,
            handler_version=HARNESS_GRAPH_REDUCER_VERSION,
            graph_id=HARNESS_EVENT_HISTORY_GRAPH_ID,
            graph_version=HARNESS_EVENT_HISTORY_GRAPH_VERSION,
            policy_id=HARNESS_HISTORY_POLICY_ID,
            policy_version=HARNESS_GRAPH_CONTROL_POLICY_VERSION,
            schema_id=HARNESS_GRAPH_HISTORY_SCHEMA_ID,
            schema_version=schema,
        ),
        commands=(),
    )


def harness_graph_activity_history(
    record: Any,
    *,
    graph_activity: Any,
) -> DeterministicHistoryRecord:
    """Build replay history for a Graph-native activity result.

    The secure payload store remains shared with generic replay activities, but
    the history policy is explicitly Graph-owned and never carries a flat
    ``step_id`` activity descriptor.
    """

    from framework.events.runtime.activities import RecordedActivityWrite
    from framework.harness.control_plane.graph_runtime import HarnessGraphActivity

    if not isinstance(record, RecordedActivityWrite):
        raise TypeError("record must be RecordedActivityWrite")
    if not isinstance(graph_activity, HarnessGraphActivity):
        raise TypeError("graph_activity must be HarnessGraphActivity")
    descriptor = record.record.activity
    if descriptor.activity_id != graph_activity.activity_id:
        raise ValueError("record and graph activity identities must match")
    return DeterministicHistoryRecord(
        policy=HistoryEventPolicy(
            handler_id=HARNESS_HISTORY_HANDLER_ID,
            handler_version=HARNESS_GRAPH_REDUCER_VERSION,
            graph_id=HARNESS_GRAPH_ACTIVITY_HISTORY_GRAPH_ID,
            graph_version=graph_activity.activity_ref.version,
            policy_id=HARNESS_HISTORY_POLICY_ID,
            policy_version=HARNESS_GRAPH_CONTROL_POLICY_VERSION,
            schema_id=HARNESS_GRAPH_HISTORY_SCHEMA_ID,
            schema_version="newsroom.harness-graph-event/v1",
            expected_activity=descriptor,
            recorded_activity_ref=record.recorded_ref,
        ),
        commands=(),
    )


def harness_expected_commands(
    event: Any,
    handler_input: Mapping[str, Any],
    activity_result: ResolvedReplayActivity | None,
) -> tuple[DeterministicCommand, ...]:
    """Rebuild Harness decisions from accepted deterministic inputs."""

    if not handler_input:
        return ()
    decision_input = _decision_input(handler_input)
    semantic = harness_decision_kernel(decision_input, activity_result)
    return (
        _decision_command(
            command_ordinal=int(decision_input["command_ordinal"]),
            decision_input=decision_input,
            decision_projection=semantic,
            graph_version=str(decision_input["graph_version"]),
            causation_id=str(decision_input["causation_id"]),
        ),
    )


def harness_decision_kernel(
    decision_input: Mapping[str, Any],
    activity_result: ResolvedReplayActivity | None,
) -> Mapping[str, Any]:
    value = _decision_input(decision_input)
    status = str(value["run_status"])
    node_instance_id = _optional_string(value.get("current_node_instance_id"))
    current_node_id = _optional_string(value.get("current_node_id"))
    steps = tuple(value["node_instances"])
    current = None
    for node in steps:
        if isinstance(node, Mapping) and node.get("node_instance_id") == node_instance_id:
            current = node
            break
    step_status = None if current is None else str(current.get("step_status") or current["status"])
    budget = value["budget"]
    policy = value["current_node_policy"]
    _reject_legacy_repair_step_authority(policy)
    gates = tuple(value["gate_results"])
    verdict = value.get("quality_verdict")
    worker = _resolved_worker_semantics(activity_result)
    approval_outcome = value["approval_outcome"]

    side_effect_failure = policy.get("side_effect_failure")
    if isinstance(side_effect_failure, Mapping):
        code = str(side_effect_failure.get("code") or "side_effect_failure")
        failure = {"code": code}
        effect_ref = side_effect_failure.get("effect_ref")
        if effect_ref is not None:
            failure["effect_ref"] = effect_ref
        return {
            "decision_type": "fail_run",
            "reason": f"side-effect failure: {code}",
            "payload": {"side_effect_failure": failure},
        }

    decision_type: str
    target: str | None = None
    reason: str | None = None
    payload: dict[str, Any] = {}
    if status in {"succeeded", "failed", "halted", "cancelled"}:
        decision_type = "halt_run"
        reason = "run is already terminal: " + status
    elif status == "created":
        decision_type = "start_step"
        target_node = str(value["entry_node_ids"][0])
        node_instance_id = None
        target = target_node
        reason = "start entry step"
    elif status == "waiting_approval" or step_status == "waiting_approval":
        if approval_outcome == "approved":
            decision_type = "resume_after_approval"
            reason = "Harness approval granted"
            payload = {"approval_outcome": "approved"}
        elif approval_outcome == "cancelled":
            decision_type = "cancel_run"
            reason = "Harness approval was cancelled"
            payload = {"approval_outcome": "cancelled"}
        else:
            decision_type = "wait_for_approval"
            reason = "run is waiting for Harness approval"
    elif step_status == "pending":
        decision_type = _turn_budget_decision(value) or "plan_step"
        if decision_type == "halt_run":
            reason = "turn budget is exhausted"
            payload = {"budget_exhausted": "turns"}
    elif step_status == "planning":
        failed = False
        for item in gates:
            if not bool(item["passed"]):
                failed = True
                break
        if not gates or not failed:
            decision_type = _execution_decision(value)
        elif _can_replan(value, current):
            decision_type = "replan_step"
            reason = "plan gate failed"
            payload = {"gate_results": [dict(item) for item in gates]}
        else:
            decision_type = "halt_run"
            reason = "plan gate failed and replan budget is exhausted"
            payload = {"budget_exhausted": "replans"}
    elif step_status in {"plan_verified", "retrying"}:
        decision_type = _execution_decision(value)
        if step_status == "retrying":
            reason = "retry current step"
    elif step_status == "running":
        if worker is None:
            decision_type = "execute_step"
        elif worker["status"] == "succeeded":
            if bool(policy.get("approval_required")) and not bool(
                current.get("approval_granted")
            ):
                decision_type = "wait_for_approval"
                reason = "step requires Harness approval"
            else:
                decision_type = (
                    _turn_budget_decision(value)
                    or "verify_step"
                )
        elif worker["status"] == "waiting_approval":
            decision_type = "wait_for_approval"
            reason = "worker is waiting for approval"
        elif worker["status"] == "blocked":
            decision_type = "block_run"
            reason = worker.get("error") or "worker blocked"
        elif _can_retry_worker(value, current, policy, worker):
            decision_type = "retry_step"
            reason = worker.get("error") or "worker failed with retryable status"
            payload = {"backoff_seconds": policy.get("backoff_seconds", 0.0)}
        elif (repair_node_id := _repair_node_id(
            policy,
            trigger="worker_failure_after_retry_exhaustion",
        )) is not None:
            decision_type = "route_to_repair"
            target = repair_node_id
            reason = worker.get("error") or "worker failed; route to configured repair node"
        else:
            decision_type = "fail_run"
            reason = worker.get("error") or "worker failed and retry budget is exhausted"
    elif step_status == "verifying":
        if not gates and verdict is None:
            decision_type = (
                _turn_budget_decision(value)
                or "verify_step"
            )
        elif gates and _all_gates_passed(gates) and not (
            isinstance(verdict, Mapping) and not bool(verdict["passed"])
        ):
            decision_type = "complete_step"
        elif (repair_node_id := _repair_node_id(
            policy,
            trigger="verification_failure",
        )) is not None:
            decision_type = "route_to_repair"
            target = repair_node_id
            reason = "verification failed; route to repair node"
        elif _can_replan(value, current):
            decision_type = "replan_step"
            reason = "verification failed"
        else:
            decision_type = "halt_run"
            reason = "verification failed and replan budget is exhausted"
            payload = {"budget_exhausted": "replans"}
    elif step_status == "replanning":
        decision_type = (
            _turn_budget_decision(value) or "plan_step"
        )
        reason = "controlled replan"
    elif step_status == "succeeded":
        target = _selected_route(value, worker)
        if target is None:
            decision_type = "complete_run"
            reason = "Graph has no next activity"
        else:
            decision_type = "route_to_step"
            reason = "explicit Graph edge selected next node"
    elif step_status == "halted":
        decision_type = "halt_run"
    elif step_status == "failed":
        decision_type = "fail_run"
    else:
        decision_type = "fail_run"
        reason = "unsupported step status: " + str(step_status)
    side_effect_authorization = policy.get("side_effect_authorization")
    if side_effect_authorization is not None:
        if decision_type not in {"complete_step", "complete_run"} or not isinstance(
            side_effect_authorization,
            Mapping,
        ):
            raise ValueError(
                "side-effect authorization is only valid for completion decisions"
            )
        payload = {
            **payload,
            "side_effect_authorization": dict(side_effect_authorization),
        }
    return {
        "decision_type": decision_type,
        "node_instance_id": node_instance_id,
        "target_node_id": target,
        "reason": reason,
        "payload": payload,
    }


def _decision_input(value: Mapping[str, Any]) -> Mapping[str, Any]:
    normalized = normalize_canonical_json(
        value,
        path="$.harness_decision_input",
    )
    if not isinstance(normalized, Mapping):
        raise TypeError("Harness decision input must be an object")
    required = {
        "schema",
        "command_ordinal",
        "causation_id",
        "run_id",
        "graph_id",
        "graph_version",
        "graph_checksum",
        "entry_node_ids",
        "graph_nodes",
        "graph_edges",
        "current_node_policy",
        "before_state_checksum",
        "run_status",
        "current_node_instance_id",
        "current_node_id",
        "turn_count",
        "replan_count",
        "worker_call_count",
        "node_instances",
        "budget",
        "gate_results",
        "quality_verdict",
        "approval_outcome",
        "routing_values",
        "expected_activity",
    }
    if set(normalized) != required:
        raise ValueError("Harness decision input fields are incomplete")
    if normalized["schema"] != HARNESS_DECISION_INPUT_SCHEMA:
        raise ValueError("unsupported Harness decision input schema")
    approval_outcome = normalized["approval_outcome"]
    if approval_outcome not in {None, "approved", "cancelled"}:
        raise ValueError("unsupported Harness approval outcome")
    if approval_outcome is not None:
        current_node_instance_id = normalized["current_node_instance_id"]
        current_step_status = None
        for node in normalized["node_instances"]:
            if (
                isinstance(node, Mapping)
                and node.get("node_instance_id") == current_node_instance_id
            ):
                current_step_status = node.get("step_status") or node.get("status")
                break
        if (
            normalized["run_status"] != "waiting_approval"
            and current_step_status != "waiting_approval"
        ):
            raise ValueError(
                "Harness approval outcome requires an approval-waiting state"
            )
    return normalized


def _decision_command(
    *,
    command_ordinal: int,
    decision_input: Mapping[str, Any],
    decision_projection: Mapping[str, Any],
    graph_version: str,
    causation_id: str,
) -> DeterministicCommand:
    target = (
        decision_projection.get("target_node_id")
        or decision_projection.get("node_id")
        or "run"
    )
    return DeterministicCommand(
        ordinal=command_ordinal,
        kind=str(decision_projection["decision_type"]),
        target=target,
        handler_version=HARNESS_GRAPH_REDUCER_VERSION,
        graph_version=graph_version,
        policy_version=HARNESS_GRAPH_CONTROL_POLICY_VERSION,
        input_refs=(
            "urn:newsroom:harness-decision-input:"
            + str(command_ordinal),
        ),
        input_checksums=(checksum_for(decision_input),),
        budget_ref=checksum_for(decision_input["budget"]),
        gate_ref=checksum_for(decision_input["gate_results"]),
        decision_ref=checksum_for(
            _semantic_decision_projection(decision_projection)
        ),
        causation_id=causation_id,
    )


def _decision_projection(decision: HarnessGraphDecision) -> dict[str, Any]:
    if not isinstance(decision, HarnessGraphDecision):
        raise TypeError("decision must be HarnessGraphDecision")
    decision_type = {
        "activate_node": "start_step",
        "enter_step_phase": "plan_step",
        "dispatch_activity": "execute_step",
        "verify_activity_result": "verify_step",
        "complete_node": "complete_step",
        "retry_node": "retry_step",
        "replan_node": "replan_step",
        "route_to_repair": "route_to_repair",
        "wait_node": "wait_for_approval",
        "complete_run": "complete_run",
        "halt_run": "halt_run",
    }.get(decision.decision_type.value, decision.decision_type.value)
    return _semantic_decision_projection(
        {
        "decision_type": decision_type,
        "node_id": decision.node_id,
        "node_instance_id": decision.node_instance_id,
        "target_node_id": decision.target_node_ids[0] if decision.target_node_ids else None,
        "payload": decision.payload,
        }
    )


def _semantic_decision_projection(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    payload = value.get("payload", {})
    safe_payload: dict[str, Any] = {}
    if isinstance(payload, Mapping):
        for key in (
            "approval_outcome",
            "backoff_seconds",
            "budget_exhausted",
            "side_effect_failure",
        ):
            if key in payload:
                safe_payload[key] = payload[key]
    return {
        "decision_type": str(value["decision_type"]),
        "target_node_id": value.get("target_node_id"),
        "node_id": value.get("node_id"),
        "node_instance_id": value.get("node_instance_id"),
        "payload": safe_payload,
    }


def _resolved_worker_semantics(
    activity_result: ResolvedReplayActivity | None,
) -> dict[str, Any] | None:
    if activity_result is None:
        return None
    context = thaw_canonical_json(activity_result.activity.context)
    if not isinstance(context, Mapping):
        raise ValueError("Harness replay activity context must be an object")
    outcome = activity_result.outcome
    error_class = outcome.error_class
    status = "succeeded"
    if outcome.status.value != "succeeded":
        prefix = "harness_worker_"
        status = (
            error_class.removeprefix(prefix)
            if isinstance(error_class, str) and error_class.startswith(prefix)
            else "failed"
        )
    return {
        "status": status,
        "error": None,
        "error_type": _optional_string(
            context.get("worker_error_type", error_class)
        ),
    }


def _turn_budget_decision(
    value: Mapping[str, Any],
) -> str | None:
    budget = value["budget"]
    if int(value["turn_count"]) >= int(budget["max_turns"]):
        return "halt_run"
    return None


def _all_gates_passed(gates: tuple[Any, ...]) -> bool:
    for item in gates:
        if not bool(item["passed"]):
            return False
    return True


def _execution_decision(value: Mapping[str, Any]) -> str:
    if _turn_budget_decision(value) is not None:
        return "halt_run"
    budget = value["budget"]
    if int(value["worker_call_count"]) >= int(budget["max_worker_calls"]):
        return "halt_run"
    return "execute_step"


def _can_replan(value: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    budget = value["budget"]
    return int(value["replan_count"]) < int(budget["max_replans"]) and int(
        current["replans"]
    ) < int(budget["max_replans"])


def _can_retry_worker(
    value: Mapping[str, Any],
    current: Mapping[str, Any],
    policy: Mapping[str, Any],
    worker: Mapping[str, Any],
) -> bool:
    if worker["status"] not in tuple(policy["retry_on_statuses"]):
        return False
    error_type = worker.get("error_type")
    if error_type is not None and error_type in tuple(
        policy["fail_fast_error_types"]
    ):
        return False
    allowed = min(
        int(policy["effective_max_attempts"]),
        int(value["budget"]["max_retries_per_step"]) + 1,
    )
    return int(current["attempts"]) < allowed


def _selected_route(
    value: Mapping[str, Any],
    worker: Mapping[str, Any] | None,
) -> str | None:
    current_node = _optional_string(value.get("current_node_id"))
    if current_node is None:
        current_instance = _optional_string(value.get("current_node_instance_id"))
        for node in value["node_instances"]:
            if isinstance(node, Mapping) and node.get("node_instance_id") == current_instance:
                current_node = _optional_string(node.get("node_id"))
                break
    routing_values = dict(value["routing_values"])
    routing_values["worker_result.status"] = (
        None if worker is None else worker.get("status")
    )
    for edge in value["graph_edges"]:
        if edge.get("source_id") != current_node:
            continue
        condition = edge.get("condition")
        if condition is None or _graph_edge_condition_matches(condition, routing_values):
            return str(edge["target_id"])
    return None


def _graph_edge_condition_matches(
    condition: Mapping[str, Any],
    values: Mapping[str, Any],
) -> bool:
    if not condition:
        return True
    kind = condition.get("kind")
    if kind == "all":
        return all(
            isinstance(item, Mapping) and _graph_edge_condition_matches(item, values)
            for item in condition["conditions"]
        )
    if kind == "any":
        return any(
            isinstance(item, Mapping) and _graph_edge_condition_matches(item, values)
            for item in condition["conditions"]
        )
    if kind != "predicate":
        return False
    path = condition.get("path")
    if not isinstance(path, str):
        return False
    actual = values.get(path)
    operator = condition.get("operator")
    expected = condition.get("expected")
    if operator == "exists":
        return (actual is not None) is bool(expected)
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "in":
        return actual in expected
    if operator == "not_in":
        return actual not in expected
    if operator in {"gte", "gt", "lte", "lt"}:
        return _numeric_compare(actual, expected, operator)
    return False


def _numeric_compare(actual: Any, expected: Any, operation: str) -> bool:
    if not isinstance(actual, int | float) or not isinstance(expected, int | float):
        return False
    if operation == "gte":
        return actual >= expected
    if operation == "gt":
        return actual > expected
    if operation == "lte":
        return actual <= expected
    return actual < expected


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


class HarnessReplayActivityResolver(ReplayActivityResolverPort):
    """Resolve Harness worker outcomes from the generic recorded-activity store."""

    def __init__(
        self,
        store: RecordedActivityStorePort,
        registry: ReplayActivityRegistry,
    ) -> None:
        if store is None:
            raise TypeError("store is required")
        if not isinstance(registry, ReplayActivityRegistry):
            raise TypeError("registry must be ReplayActivityRegistry")
        self._resolver = RecordedActivityResolver(store, registry)

    def resolve(
        self,
        expected_activity: ReplayActivityDescriptor,
        recorded_ref: PayloadReference,
    ) -> ResolvedReplayActivity:
        if not isinstance(expected_activity, ReplayActivityDescriptor):
            raise TypeError("expected_activity must be ReplayActivityDescriptor")
        if not isinstance(recorded_ref, PayloadReference):
            raise TypeError("recorded_ref must be PayloadReference")
        try:
            return self._resolver.resolve(expected_activity, recorded_ref)
        except ReplayActivityCorruptionError:
            raise
        except Exception as exc:
            if isinstance(exc, ReplayActivityMissingError):
                raise
            raise ReplayActivityCorruptionError(
                "Harness recorded activity cannot be resolved"
            ) from exc


def build_harness_history_verifier(
    *,
    graph_id: str,
    graph_version: str,
    activity_store: RecordedActivityStorePort | None = None,
    secure_activity_store: RecordedActivityStorePort | None = None,
    activity_versions: Sequence[ReplayActivityHandlerVersion] = (),
) -> HistoryVerifier:
    versions = ExactVersionRegistry()
    registrations = {
        ("graph", str(graph_id), str(graph_version)),
        (
            "graph",
            HARNESS_EVENT_HISTORY_GRAPH_ID,
            HARNESS_EVENT_HISTORY_GRAPH_VERSION,
        ),
        (
            "policy",
            HARNESS_HISTORY_POLICY_ID,
            HARNESS_GRAPH_CONTROL_POLICY_VERSION,
        ),
        (
            "schema",
            HARNESS_HISTORY_SCHEMA_ID,
            "newsroom.harness-graph-event/v1",
        ),
        (
            "schema",
            HARNESS_GRAPH_HISTORY_SCHEMA_ID,
            HARNESS_GRAPH_COMMIT_SCHEMA,
        ),
        (
            "schema",
            HARNESS_GRAPH_PROJECTION_HISTORY_SCHEMA_ID,
            HARNESS_GRAPH_PROJECTION_RECORD_SCHEMA,
        ),
    }
    for kind, component_id, version in sorted(registrations):
        versions.register(
            ExactVersionRegistration(kind, component_id, version, "pinned")
        )
    versions.register(
        ExactVersionRegistration(
            "reducer",
            HARNESS_HISTORY_HANDLER_ID,
            HARNESS_GRAPH_REDUCER_VERSION,
            harness_expected_commands,
        )
    )
    if (
        activity_store is not None
        and secure_activity_store is not None
        and activity_store is not secure_activity_store
    ):
        raise ValueError("activity_store and secure_activity_store must identify one store")
    recorded_store = activity_store or secure_activity_store
    resolver = None
    if recorded_store is not None:
        activity_versions_registry = ReplayActivityRegistry()
        for version in activity_versions:
            if not isinstance(version, ReplayActivityHandlerVersion):
                raise TypeError(
                    "activity_versions must contain ReplayActivityHandlerVersion values"
                )
            activity_versions_registry.register(version)
        resolver = HarnessReplayActivityResolver(
            recorded_store,
            activity_versions_registry,
        )
    return HistoryVerifier(versions=versions, activity_resolver=resolver)


def harness_activity_kind(activity_type: str) -> ReplayActivityKind:
    """Map a Harness worker type to its durable nondeterministic activity kind."""

    try:
        return _ACTIVITY_KIND_BY_TYPE[str(activity_type).strip()]
    except KeyError as exc:
        raise EventStoreCorruptionError(
            f"unsupported Harness replay activity type: {activity_type}"
        ) from exc


__all__ = [
    "HARNESS_GRAPH_ACTIVITY_HISTORY_GRAPH_ID",
    "HARNESS_EVENT_HISTORY_GRAPH_ID",
    "HARNESS_HISTORY_HANDLER_ID",
    "HARNESS_HISTORY_POLICY_ID",
    "HARNESS_GRAPH_HISTORY_SCHEMA_ID",
    "HarnessReplayActivityResolver",
    "build_harness_history_verifier",
    "harness_decision_history",
    "harness_decision_input_snapshot",
    "harness_decision_kernel",
    "harness_graph_activity_history",
    "harness_activity_kind",
    "harness_event_history",
    "harness_graph_history",
    "harness_expected_commands",
    "harness_transition_history",
]
