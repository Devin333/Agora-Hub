from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.agent.models import AgentLoopEventType
from framework.events.trace import TraceContext, trace_fields
from framework.llm.structured_output.observability import StructuredOutputEvent
from framework.shared.graph_identity import GraphExecutionIdentity


@dataclass(frozen=True)
class AgentLoopEvent:
    event_type: AgentLoopEventType
    agent_id: str
    iteration: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    trace_context: TraceContext | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "event_type": self.event_type.value,
            "agent_id": self.agent_id,
        }
        if self.iteration is not None:
            result["iteration"] = self.iteration
        result.update(trace_fields(self.trace_context))
        result.update(dict(self.payload))
        return result


class AgentLoopEventRecorder:
    def __init__(
        self,
        *,
        agent_id: str,
        trace_context: TraceContext | None = None,
        run_id: str | None = None,
        execution_identity: GraphExecutionIdentity | None = None,
        runtime_event_sink: Any | None = None,
    ) -> None:
        self.agent_id = agent_id
        if execution_identity is not None and not isinstance(
            execution_identity, GraphExecutionIdentity
        ):
            execution_identity = GraphExecutionIdentity.from_dict(execution_identity)
        if execution_identity is not None:
            if run_id is not None and run_id != execution_identity.run_id:
                raise ValueError("run_id must match execution_identity.run_id")
            run_id = execution_identity.run_id
        self.run_id = run_id
        self.execution_identity = execution_identity
        self.trace_context = (
            trace_context.child(agent_id=agent_id)
            if trace_context is not None
            else None
        )
        self._runtime_event_sink = runtime_event_sink
        self._events: list[AgentLoopEvent] = []

    def emit(
        self,
        event_type: AgentLoopEventType,
        *,
        iteration: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = AgentLoopEvent(
            event_type=event_type,
            agent_id=self.agent_id,
            iteration=iteration,
            payload=dict(payload or {}),
            trace_context=self.trace_context,
        )
        if self._runtime_event_sink is not None:
            from framework.events.runtime.projection import RuntimeEventEmitter, RuntimeEventIdentity

            runtime_source = "agent-loop"
            runtime_refs: tuple[str, ...] = ()
            runtime_reason_code = (
                str(event.payload.get("stop_reason"))
                if event.payload.get("stop_reason") is not None
                else None
            )
            # ToolExecutor owns tool approval requests. AgentLoop owns the
            # approval request created by control.request_human_review and
            # control.escalate, which otherwise only remained in the local
            # transcript because the canonical agent-loop map intentionally
            # excludes approval facts.
            if (
                event_type is AgentLoopEventType.AGENT_WAITING_FOR_APPROVAL
                and event.payload.get("approval_kind") != "tool_approval"
            ):
                runtime_source = "agent-loop-approval"
                approval_id = event.payload.get("approval_id")
                if isinstance(approval_id, str) and approval_id.strip():
                    runtime_refs = (approval_id.strip(),)
                runtime_reason_code = "approval_requested"

            RuntimeEventEmitter(
                self._runtime_event_sink,
                identity=RuntimeEventIdentity(
                    graph_identity=self.execution_identity,
                    activity_id=(self.execution_identity.activity_id if self.execution_identity else None),
                    node_id=(self.execution_identity.node_id if self.execution_identity else None),
                    node_instance_id=(self.execution_identity.node_instance_id if self.execution_identity else None),
                    attempt_id=(str(self.execution_identity.attempt) if self.execution_identity else None),
                ),
                source=runtime_source,
                stream_id=self.run_id,
            ).emit(
                event_type.value,
                status=(
                    str(event.payload.get("status"))
                    if event.payload.get("status") is not None
                    else None
                ),
                reason_code=runtime_reason_code,
                refs=runtime_refs,
                metadata={
                    "agent_id": self.agent_id,
                    "iteration": iteration,
                    "payload": event.payload,
                },
            )
        self._events.append(event)
        return event.to_dict()

    def started(self) -> None:
        self.emit(AgentLoopEventType.AGENT_STARTED)

    def iteration_started(
        self,
        *,
        iteration: int,
        feedback: str | None,
        tool_observation_count: int,
        tools_available: list[str],
    ) -> None:
        self.emit(
            AgentLoopEventType.ITERATION_STARTED,
            iteration=iteration,
            payload={
                "feedback": feedback,
                "tool_observation_count": tool_observation_count,
                "tools_available": list(tools_available),
            },
        )

    def llm_call(
        self,
        *,
        iteration: int,
        token_usage: dict[str, Any],
        response_chars: int,
        provider: str | None = None,
        model: str | None = None,
        route_id: str | None = None,
        deployment_id: str | None = None,
        fallback_used: bool | None = None,
        fallback_count: int | None = None,
        router_event_count: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "token_usage": dict(token_usage),
            "response_chars": response_chars,
        }
        if provider:
            payload["provider"] = provider
        if model:
            payload["model"] = model
        if route_id:
            payload["route_id"] = route_id
        if deployment_id:
            payload["deployment_id"] = deployment_id
        if fallback_used is not None:
            payload["fallback_used"] = fallback_used
        if fallback_count is not None:
            payload["fallback_count"] = fallback_count
        if router_event_count is not None:
            payload["router_event_count"] = router_event_count
        self.emit(AgentLoopEventType.LLM_CALL, iteration=iteration, payload=payload)

    def llm_stream_event(
        self,
        *,
        iteration: int,
        stream_event: dict[str, Any],
        sequence: int,
    ) -> None:
        event_type = str(stream_event.get("event_type") or "")
        payload: dict[str, Any] = {
            "sequence": sequence,
            "stream_event_type": event_type,
            "stream_event": dict(stream_event),
        }
        text_delta = stream_event.get("text_delta")
        if isinstance(text_delta, str):
            payload["text_delta_chars"] = len(text_delta)
        self.emit(AgentLoopEventType.LLM_STREAM_EVENT, iteration=iteration, payload=payload)

    def llm_failed(self, *, iteration: int, exc: Exception) -> None:
        self.emit(
            AgentLoopEventType.LLM_CALL_FAILED,
            iteration=iteration,
            payload={
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            },
        )

    def action_parsed(
        self,
        *,
        iteration: int,
        action_type: str,
        tool_name: str | None = None,
        output_keys: list[str] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "action_type": action_type,
            "output_keys": list(output_keys or []),
        }
        if tool_name:
            payload["tool_name"] = tool_name
        self.emit(AgentLoopEventType.ACTION_PARSED, iteration=iteration, payload=payload)

    def parser_error(
        self,
        *,
        iteration: int,
        error_type: str,
        error_message: str,
        parser_errors: int,
        max_parser_errors: int,
    ) -> None:
        self.emit(
            AgentLoopEventType.PARSER_ERROR,
            iteration=iteration,
            payload={
                "error_type": error_type,
                "error_message": error_message,
                "parser_errors": parser_errors,
                "max_parser_errors": max_parser_errors,
            },
        )

    def tool_call(self, *, iteration: int, tool_name: str, call_id: str) -> None:
        self.emit(
            AgentLoopEventType.TOOL_CALL,
            iteration=iteration,
            payload={
                "tool_name": tool_name,
                "call_id": call_id,
            },
        )

    def tool_observation(
        self,
        *,
        iteration: int,
        observation: dict[str, Any],
    ) -> None:
        self.emit(
            AgentLoopEventType.TOOL_OBSERVATION,
            iteration=iteration,
            payload={"observation": dict(observation)},
        )

    def tool_budget_blocked(
        self,
        *,
        iteration: int,
        tool_name: str,
        max_tool_calls: int,
    ) -> None:
        self.emit(
            AgentLoopEventType.TOOL_BUDGET_BLOCKED,
            iteration=iteration,
            payload={
                "tool_name": tool_name,
                "max_tool_calls_per_agent": max_tool_calls,
            },
        )

    def tool_approval_required(
        self,
        *,
        iteration: int,
        tool_name: str,
        approval_id: str | None,
    ) -> None:
        self.emit(
            AgentLoopEventType.TOOL_APPROVAL_REQUIRED,
            iteration=iteration,
            payload={
                "tool_name": tool_name,
                "approval_id": approval_id,
            },
        )

    def repeated_tool_call_detected(
        self,
        *,
        iteration: int,
        tool_name: str,
        signature: str,
        count: int,
        limit: int,
    ) -> None:
        self.emit(
            AgentLoopEventType.REPEATED_TOOL_CALL_DETECTED,
            iteration=iteration,
            payload={
                "tool_name": tool_name,
                "signature": signature,
                "count": count,
                "limit": limit,
            },
        )

    def subagent_delegation_requested(
        self,
        *,
        iteration: int,
        parent_agent_id: str,
        child_agent_id: str,
        handoff_reason: str,
        task: str,
    ) -> None:
        self.emit(
            AgentLoopEventType.SUBAGENT_DELEGATION_REQUESTED,
            iteration=iteration,
            payload={
                "parent_agent_id": parent_agent_id,
                "child_agent_id": child_agent_id,
                "handoff_reason": handoff_reason,
                "task": task,
            },
        )

    def subagent_completed(
        self,
        *,
        iteration: int,
        child_agent_id: str,
        output_keys: list[str],
        summary: str | None,
    ) -> None:
        self.emit(
            AgentLoopEventType.SUBAGENT_COMPLETED,
            iteration=iteration,
            payload={
                "child_agent_id": child_agent_id,
                "output_keys": sorted(output_keys),
                "summary": summary,
            },
        )

    def subagent_failed(
        self,
        *,
        iteration: int,
        child_agent_id: str,
        status: str,
        error: str | None,
    ) -> None:
        self.emit(
            AgentLoopEventType.SUBAGENT_FAILED,
            iteration=iteration,
            payload={
                "child_agent_id": child_agent_id,
                "status": status,
                "error": error,
            },
        )

    def judge_accept(self, *, iteration: int, verdict: dict[str, Any]) -> None:
        self.emit(
            AgentLoopEventType.JUDGE_ACCEPT,
            iteration=iteration,
            payload={"verdict": dict(verdict)},
        )

    def structured_output_validation_accepted(
        self,
        *,
        iteration: int,
        verdict: dict[str, Any],
        repair_count: int = 0,
    ) -> None:
        self.emit(
            AgentLoopEventType.STRUCTURED_OUTPUT_VALIDATION_ACCEPTED,
            iteration=iteration,
            payload={
                **_structured_output_event_payload(
                    verdict,
                    event_type="structured_output_validation_accepted",
                    attempt_ref=str(iteration),
                    budget_disposition="accepted_for_domain_gates",
                    run_id=self.run_id,
                    execution_identity=self.execution_identity,
                ),
                "repair_count": max(0, int(repair_count)),
            },
        )

    def structured_output_repair_requested(
        self,
        *,
        iteration: int,
        verdict: dict[str, Any],
        repair_attempt: int,
        max_repairs: int,
    ) -> None:
        self.emit(
            AgentLoopEventType.STRUCTURED_OUTPUT_REPAIR_REQUESTED,
            iteration=iteration,
            payload={
                **_structured_output_event_payload(
                    verdict,
                    event_type="structured_output_repair_requested",
                    attempt_ref=str(iteration),
                    budget_disposition="repair_authorized",
                    run_id=self.run_id,
                    execution_identity=self.execution_identity,
                ),
                "repair_attempt": repair_attempt,
                "max_repairs": max_repairs,
                "remaining_repairs": max(0, max_repairs - repair_attempt),
            },
        )

    def structured_output_repair_budget_exhausted(
        self,
        *,
        iteration: int,
        verdict: dict[str, Any],
        stop_reason: str,
    ) -> None:
        self.emit(
            AgentLoopEventType.STRUCTURED_OUTPUT_REPAIR_BUDGET_EXHAUSTED,
            iteration=iteration,
            payload={
                **_structured_output_event_payload(
                    verdict,
                    event_type="structured_output_repair_budget_exhausted",
                    attempt_ref=str(iteration),
                    budget_disposition="halt",
                    run_id=self.run_id,
                    execution_identity=self.execution_identity,
                ),
                "stop_reason": stop_reason,
            },
        )

    def judge_retry(
        self,
        *,
        iteration: int,
        feedback: str,
        verdict: dict[str, Any],
        via_tool: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "feedback": feedback,
            "verdict": dict(verdict),
        }
        if via_tool:
            payload["via_tool"] = via_tool
        self.emit(AgentLoopEventType.JUDGE_RETRY, iteration=iteration, payload=payload)

    def judge_block(
        self,
        *,
        iteration: int,
        verdict: dict[str, Any],
        via_tool: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"verdict": dict(verdict)}
        if via_tool:
            payload["via_tool"] = via_tool
        self.emit(AgentLoopEventType.JUDGE_BLOCK, iteration=iteration, payload=payload)

    def final_output(
        self,
        *,
        iteration: int,
        output_keys: list[str],
        via_tool: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"output_keys": sorted(output_keys)}
        if via_tool:
            payload["via_tool"] = via_tool
        self.emit(AgentLoopEventType.FINAL_OUTPUT, iteration=iteration, payload=payload)

    def completed(
        self,
        *,
        iteration: int,
        status: str,
        stop_reason: str,
    ) -> None:
        self.emit(
            AgentLoopEventType.AGENT_COMPLETED,
            iteration=iteration,
            payload={"status": status, "stop_reason": stop_reason},
        )

    def waiting_for_approval(
        self,
        *,
        iteration: int,
        stop_reason: str,
        approval_id: str | None,
        approval_kind: str = "tool_approval",
        tool_name: str | None = None,
        control_action: str | None = None,
        escalation_type: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "stop_reason": stop_reason,
            "approval_id": approval_id,
            "approval_kind": approval_kind,
        }
        if tool_name:
            payload["tool_name"] = tool_name
        if control_action:
            payload["control_action"] = control_action
        if escalation_type:
            payload["escalation_type"] = escalation_type
        self.emit(
            AgentLoopEventType.AGENT_WAITING_FOR_APPROVAL,
            iteration=iteration,
            payload=payload,
        )

    def blocked(self, *, iteration: int, stop_reason: str, verdict: dict[str, Any] | None) -> None:
        self.emit(
            AgentLoopEventType.AGENT_BLOCKED,
            iteration=iteration,
            payload={"stop_reason": stop_reason, "verdict": verdict},
        )

    def stalled(self, *, iteration: int, stop_reason: str, summary: str) -> None:
        self.emit(
            AgentLoopEventType.AGENT_STALLED,
            iteration=iteration,
            payload={"stop_reason": stop_reason, "summary": summary},
        )

    def retry_exhausted(
        self,
        *,
        iteration: int,
        stop_reason: str,
        verdict: dict[str, Any] | None,
    ) -> None:
        self.emit(
            AgentLoopEventType.AGENT_RETRY_EXHAUSTED,
            iteration=iteration,
            payload={"stop_reason": stop_reason, "verdict": verdict},
        )

    def failed(self, *, iteration: int, stop_reason: str, error: str) -> None:
        self.emit(
            AgentLoopEventType.AGENT_FAILED,
            iteration=iteration,
            payload={"stop_reason": stop_reason, "error": error},
        )

    def to_dicts(self) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self._events]


def event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _structured_output_event_payload(
    verdict: dict[str, Any],
    *,
    event_type: str,
    attempt_ref: str,
    budget_disposition: str,
    run_id: str | None,
    execution_identity: GraphExecutionIdentity | None = None,
) -> dict[str, Any]:
    diagnostics = verdict.get("structured_output_diagnostics")
    safe_diagnostics = []
    if isinstance(diagnostics, list):
        for item in diagnostics[:20]:
            if not isinstance(item, dict):
                continue
            safe_diagnostics.append(
                {
                    "code": item.get("code"),
                    "instance_path": list(item.get("instance_path") or []),
                    "schema_path": list(item.get("schema_path") or []),
                    "validator": item.get("validator"),
                }
            )
    contract = verdict.get("structured_output_contract")
    safe_contract = dict(contract) if isinstance(contract, dict) else None
    if safe_contract is not None:
        safe_contract.pop("schema_name", None)
    first_diagnostic = safe_diagnostics[0] if safe_diagnostics else {}
    envelope = StructuredOutputEvent(
        event_type=event_type,
        run_id=run_id,
        execution_identity=execution_identity,
        attempt_ref=attempt_ref,
        schema_digest=(safe_contract or {}).get("schema_digest"),
        schema_revision=(safe_contract or {}).get("schema_revision"),
        schema_dialect=(safe_contract or {}).get("dialect"),
        typed_adapter_revision=(safe_contract or {}).get(
            "typed_adapter_revision"
        ),
        provider_capability_revision=(safe_contract or {}).get(
            "provider_capability_revision"
        ),
        projection_digest=(safe_contract or {}).get("projection_digest"),
        projection_mode=(safe_contract or {}).get("projection_mode"),
        issue_code=first_diagnostic.get("code"),
        instance_path=tuple(first_diagnostic.get("instance_path") or ()),
        schema_path=tuple(first_diagnostic.get("schema_path") or ()),
        issue_count=min(len(safe_diagnostics), 20),
        response_fingerprint=verdict.get("response_fingerprint"),
        budget_disposition=budget_disposition,
    )
    return {
        **envelope.to_payload(),
        "contract": safe_contract,
        "diagnostics": safe_diagnostics,
    }
