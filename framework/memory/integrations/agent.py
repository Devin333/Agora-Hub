from __future__ import annotations

from typing import Any

from framework.memory.models import MemoryKind, MemoryQuery, MemoryRecord, MemoryRecallResult, MemoryScope
from framework.memory.policy import DEFAULT_AGENT_MEMORY_POLICY, MemoryPolicy
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.memory.runtime import MemoryRuntime
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.hashing import short_hash
from framework.shared.redaction import redact_sensitive_values


class AgentMemoryAdapter:
    def before_llm_call(
        self,
        *,
        agent_id: str,
        input_text: str,
        runtime: MemoryRuntime | None = None,
        memory_recall: ExecutionMemoryRecallPort | None = None,
        execution_identity: GraphExecutionIdentity | None = None,
        policy: MemoryPolicy = DEFAULT_AGENT_MEMORY_POLICY,
    ) -> MemoryRecallResult:
        if memory_recall is not None:
            if not isinstance(memory_recall, ExecutionMemoryRecallPort):
                raise TypeError(
                    "memory_recall must implement ExecutionMemoryRecallPort"
                )
            execution_identity = _require_execution_identity(execution_identity)
            memory_recall.validate_execution(execution_identity)
            kinds = [
                kind for kind in (MemoryKind.CORE, MemoryKind.SEMANTIC, MemoryKind.EPISODIC)
                if not policy.allowed_kinds or kind in policy.allowed_kinds
            ]
            if not kinds:
                raise ValueError("Agent memory policy permits no automatic recall kinds")
            return memory_recall.recall(
                MemoryQuery(
                    query=input_text,
                    scopes=_allowed_scopes(
                        _allowed_scopes(
                            [
                                MemoryScope.SESSION,
                                MemoryScope.AGENT,
                                MemoryScope.GRAPH,
                                MemoryScope.GLOBAL,
                            ],
                            policy=DEFAULT_AGENT_MEMORY_POLICY,
                        ),
                        policy=policy,
                    ),
                    kinds=kinds,
                    limit=policy.max_recall_results,
                    max_context_tokens=policy.max_context_tokens,
                ),
                policy=policy,
            )
        if execution_identity is not None:
            raise ValueError("Graph memory recall requires ExecutionMemoryRecallPort")
        if runtime is None:
            raise ValueError("runtime or memory_recall is required")
        desired_scopes = [
            MemoryScope.SESSION,
            MemoryScope.AGENT,
            MemoryScope.GLOBAL,
        ]
        filters: dict[str, Any] = {"agent_id": agent_id}
        return runtime.recall(
            MemoryQuery(
                query=input_text,
                scopes=_allowed_scopes(desired_scopes, policy=policy),
                kinds=[MemoryKind.CORE, MemoryKind.SEMANTIC, MemoryKind.EPISODIC],
                filters=filters,
                limit=policy.max_recall_results,
                max_context_tokens=policy.max_context_tokens,
            ),
            policy=policy,
        )

    def propose_tool_observation(
        self,
        *,
        agent_id: str,
        tool_name: str,
        observation: dict[str, Any],
        execution_identity: GraphExecutionIdentity | None,
    ) -> MemoryRecord | None:
        if execution_identity is None:
            return None
        execution_identity = _require_execution_identity(execution_identity)
        return _candidate_record(
            kind=MemoryKind.OBSERVATION,
            summary=f"Tool observation from {tool_name}",
            content=_bounded_content(observation),
            agent_id=agent_id,
            execution_identity=execution_identity,
            event_type="tool_observation",
            metadata={"tool_name": tool_name},
        )

    def propose_final_output(
        self,
        *,
        agent_id: str,
        output: dict[str, Any],
        execution_identity: GraphExecutionIdentity | None,
    ) -> MemoryRecord | None:
        if execution_identity is None:
            return None
        execution_identity = _require_execution_identity(execution_identity)
        return _candidate_record(
            kind=MemoryKind.EPISODIC,
            summary=f"Final output from {agent_id}",
            content=_bounded_content(output),
            agent_id=agent_id,
            execution_identity=execution_identity,
            event_type="final_output",
        )


def _candidate_record(
    *,
    kind: MemoryKind,
    summary: str,
    content: str,
    agent_id: str,
    execution_identity: GraphExecutionIdentity,
    event_type: str,
    metadata: dict[str, Any] | None = None,
) -> MemoryRecord:
    execution_identity = _require_execution_identity(execution_identity)
    identity = execution_identity.to_dict()
    candidate_projection = {
        "kind": kind.value,
        "summary": summary,
        "content": content,
        "agent_id": agent_id,
        "event_type": event_type,
        "execution_identity": identity,
        "metadata": dict(metadata or {}),
    }
    return MemoryRecord(
        memory_id=f"agent-loop:{short_hash(candidate_projection, length=32)}",
        kind=kind,
        scope=MemoryScope.GRAPH,
        summary=summary,
        content=content,
        metadata={
            "candidate_only": True,
            "source": "agent_loop",
            "event_type": event_type,
            "agent_id": agent_id,
            "graph_identity": identity,
            **dict(metadata or {}),
        },
        refs={**identity, "agent_id": agent_id, "source": "agent_loop"},
        actor=agent_id,
        namespace="agent.loop",
    )


def _require_execution_identity(
    value: GraphExecutionIdentity,
) -> GraphExecutionIdentity:
    if not isinstance(value, GraphExecutionIdentity):
        raise TypeError(
            "AgentMemoryAdapter requires GraphExecutionIdentity"
        )
    return value


def _bounded_content(value: Any, *, limit: int = 16_384) -> str:
    text = str(redact_sensitive_values(value))
    if len(text) <= limit:
        return text
    return f"{text[:limit - 32]}...[truncated:{len(text) - limit + 32}]"


def _allowed_scopes(
    desired_scopes: list[MemoryScope],
    *,
    policy: MemoryPolicy,
) -> list[MemoryScope]:
    if not policy.allowed_scopes:
        return list(desired_scopes)
    allowed = set(policy.allowed_scopes)
    return [scope for scope in desired_scopes if scope in allowed]
