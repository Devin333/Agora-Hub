from __future__ import annotations

from typing import Any

from framework.agent.loop.extensions import OutputNormalizer
from framework.agent.loop.loop import AgentLoop
from framework.agent.models import AgentLoopResult, AgentSpec
from framework.agent.loop.parser import AgentActionParser
from framework.agent.loop.prompt import PromptBuilder
from framework.agent.loop.judge import OutputJudge
from framework.agent.subagents import SubAgentExecutor
from framework.llm.budget import GlobalBudgetTracker
from framework.llm.models import LLMClient
from framework.memory import MemoryPolicy, MemoryRuntime
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.tool import ToolExecutor
from framework.tool.runtime.evidence import ToolExecutionEvidencePort
from framework.tool import ToolRegistry, register_memory_tools
from framework.agent.messages import (
    AgentIterationCheckpoint,
    AgentMessageRecord,
    CONVERSATION_SCOPE_GRAPH,
    CONVERSATION_SCOPE_STANDALONE,
    ConversationCursor,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.agent.models.orchestration import AgentOrchestrationPort


class AgentRunner:
    def __init__(
        self,
        *,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        conversation_store: Any | None = None,
        global_budget_tracker: GlobalBudgetTracker | None = None,
        subagent_executor: SubAgentExecutor | None = None,
        orchestration_port: AgentOrchestrationPort | None = None,
        orchestration_enabled: bool = False,
        memory_runtime: MemoryRuntime | None = None,
        memory_policy: MemoryPolicy | None = None,
        output_judge: OutputJudge | None = None,
        output_normalizer: OutputNormalizer | None = None,
        runtime_event_sink: Any | None = None,
        execution_environment: Any | None = None,
        require_explicit_execution_profile: bool = False,
    ) -> None:
        self._llm_client = llm_client
        self._tool_registry = tool_registry
        self._conversation_store = conversation_store
        self._global_budget_tracker = global_budget_tracker
        self._subagent_executor = subagent_executor
        self._orchestration_port = orchestration_port
        self._orchestration_enabled = orchestration_enabled
        self._memory_runtime = memory_runtime
        self._memory_policy = memory_policy
        self._output_judge = output_judge or OutputJudge()
        self._output_normalizer = output_normalizer
        self._runtime_event_sink = runtime_event_sink
        self._execution_environment = execution_environment
        self._require_explicit_execution_profile = require_explicit_execution_profile

    @property
    def execution_environment(self) -> Any | None:
        """Return the provider registry bound to this runner, if any."""

        return self._execution_environment

    @property
    def orchestration_enabled(self) -> bool:
        """Whether the Harness-owned multi-child capability is enabled."""

        return self._orchestration_enabled

    @property
    def orchestration_port(self) -> AgentOrchestrationPort | None:
        """Expose the configured Harness boundary for composition integrity checks."""

        return self._orchestration_port

    def bind_orchestration(
        self,
        *,
        orchestration_port: AgentOrchestrationPort | None,
        orchestration_enabled: bool,
    ) -> None:
        """Bind the production-owned orchestration boundary before execution.

        Rebinding a runner to a different port is prohibited so a reused graph
        worker cannot drift to an ad hoc child executor between runs.
        """

        if orchestration_port is not None and not isinstance(
            orchestration_port, AgentOrchestrationPort
        ):
            raise TypeError("orchestration_port must implement AgentOrchestrationPort")
        if not isinstance(orchestration_enabled, bool):
            raise TypeError("orchestration_enabled must be boolean")
        if (
            self._orchestration_port is not None
            and orchestration_port is not None
            and self._orchestration_port is not orchestration_port
        ):
            raise ValueError("AgentRunner orchestration port is already bound")
        if self._orchestration_enabled and not orchestration_enabled:
            raise ValueError("AgentRunner orchestration feature cannot be disabled after binding")
        self._orchestration_port = orchestration_port or self._orchestration_port
        self._orchestration_enabled = orchestration_enabled

    def run(
        self,
        agent: AgentSpec,
        inputs: dict[str, Any],
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
        graph_id: str | None = None,
        graph_version: str | None = None,
        graph_ref: str | None = None,
        graph_checksum: str | None = None,
        node_id: str | None = None,
        node_instance_id: str | None = None,
        graph_checkpoint_ref: str | None = None,
        activity_id: str | None = None,
        attempt: int | None = None,
        resume_from_cursor: bool = False,
        global_budget_tracker: GlobalBudgetTracker | None = None,
        standalone: bool = False,
        memory_recall: ExecutionMemoryRecallPort | None = None,
        tool_execution_evidence: ToolExecutionEvidencePort | None = None,
    ) -> AgentLoopResult:
        if not isinstance(standalone, bool):
            raise TypeError("standalone must be boolean")
        _validate_graph_identity_arguments(
            run_id=run_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
        )
        graph_identity = _graph_execution_identity(
            run_id=run_id,
            graph_id=graph_id,
            graph_version=graph_version,
            graph_ref=graph_ref,
            graph_checksum=graph_checksum,
            node_id=node_id,
            node_instance_id=node_instance_id,
            activity_id=activity_id,
            attempt=attempt,
        )
        if standalone and (
            graph_identity is not None
            or node_instance_id is not None
            or graph_checkpoint_ref is not None
        ):
            raise ValueError(
                "standalone AgentRunner execution cannot carry Graph identity"
            )
        if graph_identity is None and not standalone:
            raise ValueError(
                "AgentRunner requires an exact GraphExecutionIdentity; "
                "use standalone=True for an explicitly isolated run"
            )
        if memory_recall is not None and not isinstance(
            memory_recall, ExecutionMemoryRecallPort
        ):
            raise TypeError(
                "memory_recall must implement ExecutionMemoryRecallPort"
            )
        if standalone and memory_recall is not None:
            raise ValueError(
                "standalone AgentRunner cannot use execution-bound memory recall"
            )
        if tool_execution_evidence is not None and (
            standalone
            or not isinstance(tool_execution_evidence, ToolExecutionEvidencePort)
            or getattr(tool_execution_evidence, "is_durable", False) is not True
        ):
            raise ValueError(
                "tool_execution_evidence requires a durable Graph-bound capability"
            )
        if memory_recall is not None:
            memory_recall.validate_execution(graph_identity)
        elif graph_identity is not None and (
            self._orchestration_enabled
            or self._memory_runtime is not None
            or _has_registered_memory_tools(self._tool_registry)
        ):
            raise ValueError(
                "Graph-bound AgentRunner memory recall requires an "
                "ExecutionMemoryRecallPort"
            )
        execution_tool_registry = _execution_tool_registry(
            self._tool_registry,
            memory_recall=memory_recall,
            execution_identity=graph_identity,
            memory_policy=self._memory_policy,
            memory_enabled=agent.memory_enabled,
        )
        message_scope_kind = (
            CONVERSATION_SCOPE_STANDALONE
            if standalone
            else CONVERSATION_SCOPE_GRAPH
        )
        message_identity = _message_identity(
            scope_kind=message_scope_kind,
            run_id=run_id,
            graph_id=graph_id,
            graph_version=graph_version,
            graph_ref=graph_ref,
            graph_checksum=graph_checksum,
            node_id=node_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
            activity_id=activity_id,
            attempt=attempt,
        )
        loop_inputs = self._inputs_with_resume_context(
            inputs,
            conversation_id,
            agent_id=agent.agent_id,
            resume_from_cursor=resume_from_cursor,
            run_id=run_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
        )
        self._append_conversation_message(
            conversation_id,
            AgentMessageRecord(
                conversation_id=conversation_id or "",
                role="user",
                content=inputs,
                agent_id=agent.agent_id,
                **message_identity,
                metadata={"message_type": "agent_inputs"},
            ),
        )
        tools = execution_tool_registry.export_schema_for_llm(
            agent.agent_id,
            agent.resolved_tool_policy(),
        )
        effective_budget_tracker = global_budget_tracker or self._global_budget_tracker
        if graph_identity is not None and effective_budget_tracker is not None:
            effective_budget_tracker = effective_budget_tracker.for_execution_identity(
                graph_identity
            )
        loop = AgentLoop(
            llm_client=self._llm_client,
            tool_executor=ToolExecutor(
                execution_tool_registry,
                graph_identity=graph_identity,
                execution_environment=self._execution_environment,
                runtime_event_sink=self._runtime_event_sink,
                require_explicit_execution_profile=self._require_explicit_execution_profile,
                execution_evidence=tool_execution_evidence,
            ),
            prompt_builder=PromptBuilder(),
            action_parser=AgentActionParser(),
            output_judge=self._output_judge,
            output_normalizer=self._output_normalizer,
            global_budget_tracker=effective_budget_tracker,
            subagent_executor=self._subagent_executor,
            orchestration_port=self._orchestration_port,
            orchestration_enabled=self._orchestration_enabled,
            memory_runtime=self._memory_runtime,
            memory_recall=memory_recall,
            memory_policy=self._memory_policy,
            runtime_event_sink=self._runtime_event_sink,
            tool_execution_evidence=tool_execution_evidence,
        )
        result = loop.run(
            agent,
            loop_inputs,
            tools,
            run_id=run_id,
            execution_identity=graph_identity,
            graph_checkpoint_ref=graph_checkpoint_ref,
            standalone=standalone,
        )
        self._append_conversation_events(
            conversation_id,
            agent,
            result.events,
            run_id=run_id,
            graph_id=graph_id,
            graph_version=graph_version,
            graph_ref=graph_ref,
            graph_checksum=graph_checksum,
            node_id=node_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
            activity_id=activity_id,
            attempt=attempt,
            scope_kind=message_scope_kind,
        )
        self._append_conversation_diagnostics(
            conversation_id,
            agent,
            result,
            run_id=run_id,
            graph_id=graph_id,
            graph_version=graph_version,
            graph_ref=graph_ref,
            graph_checksum=graph_checksum,
            node_id=node_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
            activity_id=activity_id,
            attempt=attempt,
            scope_kind=message_scope_kind,
        )
        self._append_conversation_message(
            conversation_id,
            AgentMessageRecord(
                conversation_id=conversation_id or "",
                role="assistant",
                content=_conversation_result_payload(result),
                agent_id=agent.agent_id,
                **message_identity,
                metadata={
                    "message_type": "agent_result",
                    "status": result.status.value,
                    "iterations": result.iterations,
                },
            ),
        )
        if self._conversation_store is not None and conversation_id:
            self._conversation_store.write_summary(
                conversation_id,
                (
                    f"agent_id={agent.agent_id} status={result.status.value} "
                    f"iterations={result.iterations} "
                    f"stop_reason={_result_stop_reason(result)}"
                ),
            )
            self._compact_conversation_if_needed(conversation_id, agent)
            self._write_conversation_cursor(
                conversation_id,
                agent=agent,
                result=result,
                run_id=run_id,
                node_instance_id=node_instance_id,
                graph_checkpoint_ref=graph_checkpoint_ref,
            )
            self._write_iteration_checkpoint(
                conversation_id,
                agent=agent,
                result=result,
                run_id=run_id,
                node_instance_id=node_instance_id,
                graph_checkpoint_ref=graph_checkpoint_ref,
            )
        return result

    def run_spec(
        self,
        spec: AgentSpec,
        input_text: str | dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> AgentLoopResult:
        inputs = input_text if isinstance(input_text, dict) else {"input_text": input_text}
        return self.run(spec, dict(inputs), run_id=run_id, standalone=True)

    def _append_conversation_message(
        self,
        conversation_id: str | None,
        message: AgentMessageRecord,
    ) -> None:
        if self._conversation_store is None or not conversation_id:
            return
        self._conversation_store.append_message(conversation_id, message)

    def _append_conversation_events(
        self,
        conversation_id: str | None,
        agent: AgentSpec,
        events: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        graph_id: str | None = None,
        graph_version: str | None = None,
        graph_ref: str | None = None,
        graph_checksum: str | None = None,
        node_id: str | None = None,
        node_instance_id: str | None = None,
        graph_checkpoint_ref: str | None = None,
        activity_id: str | None = None,
        attempt: int | None = None,
        scope_kind: str,
    ) -> None:
        if self._conversation_store is None or not conversation_id:
            return
        for event in events:
            message = _conversation_message_from_event(
                conversation_id,
                agent,
                event,
                run_id=run_id,
                graph_id=graph_id,
                graph_version=graph_version,
                graph_ref=graph_ref,
                graph_checksum=graph_checksum,
                node_id=node_id,
                node_instance_id=node_instance_id,
                graph_checkpoint_ref=graph_checkpoint_ref,
                activity_id=activity_id,
                attempt=attempt,
                scope_kind=scope_kind,
            )
            if message is not None:
                self._conversation_store.append_message(conversation_id, message)

    def _append_conversation_diagnostics(
        self,
        conversation_id: str | None,
        agent: AgentSpec,
        result: AgentLoopResult,
        *,
        run_id: str | None = None,
        graph_id: str | None = None,
        graph_version: str | None = None,
        graph_ref: str | None = None,
        graph_checksum: str | None = None,
        node_id: str | None = None,
        node_instance_id: str | None = None,
        graph_checkpoint_ref: str | None = None,
        activity_id: str | None = None,
        attempt: int | None = None,
        scope_kind: str,
    ) -> None:
        if self._conversation_store is None or not conversation_id:
            return
        if result.diagnostics is None:
            return
        diagnostics = result.diagnostics.to_dict()
        trace_summary = diagnostics.get("trace_summary") or {}
        self._conversation_store.append_message(
            conversation_id,
            AgentMessageRecord(
                conversation_id=conversation_id,
                role="diagnostic",
                content={
                    "diagnostics": diagnostics,
                    "trace_summary": trace_summary,
                },
                agent_id=agent.agent_id,
                **_message_identity(
                    scope_kind=scope_kind,
                    run_id=run_id,
                    graph_id=graph_id,
                    graph_version=graph_version,
                    graph_ref=graph_ref,
                    graph_checksum=graph_checksum,
                    node_id=node_id,
                    node_instance_id=node_instance_id,
                    graph_checkpoint_ref=graph_checkpoint_ref,
                    activity_id=activity_id,
                    attempt=attempt,
                ),
                metadata={
                    "message_type": "agent_loop_diagnostics",
                    "status": result.status.value,
                    "stop_reason": diagnostics.get("stop_reason"),
                    "healthy": diagnostics.get("healthy"),
                    "severity": diagnostics.get("severity"),
                },
            ),
        )

    def _compact_conversation_if_needed(self, conversation_id: str, agent: AgentSpec) -> None:
        if self._conversation_store is None:
            return
        policy = agent.loop_policy
        if not policy.conversation_compaction_enabled:
            return
        messages = self._conversation_store.read_messages(conversation_id)
        if len(messages) <= policy.conversation_compaction_max_messages:
            return
        self._conversation_store.compact_messages(
            conversation_id,
            keep_last=policy.conversation_compaction_keep_last,
        )

    def _inputs_with_resume_context(
        self,
        inputs: dict[str, Any],
        conversation_id: str | None,
        *,
        agent_id: str,
        resume_from_cursor: bool,
        run_id: str | None,
        node_instance_id: str | None,
        graph_checkpoint_ref: str | None,
    ) -> dict[str, Any]:
        loop_inputs = dict(inputs)
        if not resume_from_cursor or self._conversation_store is None or not conversation_id:
            return loop_inputs
        cursor = self._conversation_store.read_cursor(conversation_id)
        if cursor is None:
            return loop_inputs
        cursor_agent_id = cursor.metadata.get("agent_id")
        if cursor_agent_id is not None and str(cursor_agent_id) != agent_id:
            raise ValueError(
                "conversation cursor agent_id mismatch: "
                f"{cursor_agent_id} != {agent_id}"
            )
        _assert_resume_graph_identity(
            cursor,
            run_id=run_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
            label="conversation cursor",
        )
        loop_inputs["conversation_cursor"] = cursor.to_dict()
        summary = self._conversation_store.get_summary(conversation_id)
        if summary is not None:
            loop_inputs["conversation_summary"] = summary
        checkpoint = self._conversation_store.read_iteration_checkpoint(conversation_id)
        if checkpoint is not None:
            checkpoint_agent_id = checkpoint.agent_id
            if checkpoint_agent_id != agent_id:
                raise ValueError(
                    "agent iteration checkpoint agent_id mismatch: "
                    f"{checkpoint_agent_id} != {agent_id}"
                )
            _assert_resume_graph_identity(
                checkpoint,
                run_id=run_id,
                node_instance_id=node_instance_id,
                graph_checkpoint_ref=graph_checkpoint_ref,
                label="agent iteration checkpoint",
            )
            if _graph_identity(checkpoint) != _graph_identity(cursor):
                raise ValueError(
                    "conversation cursor and agent iteration checkpoint Graph "
                    "identity mismatch"
                )
            loop_inputs["agent_iteration_checkpoint"] = checkpoint.to_dict()
        return loop_inputs

    def _write_conversation_cursor(
        self,
        conversation_id: str,
        *,
        agent: AgentSpec,
        result: AgentLoopResult,
        run_id: str | None,
        node_instance_id: str | None,
        graph_checkpoint_ref: str | None,
    ) -> None:
        if self._conversation_store is None:
            return
        messages = self._conversation_store.read_messages(conversation_id)
        if not messages:
            return
        latest = messages[-1]
        persisted_identity = _persisted_graph_identity(
            run_id=run_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
        )
        self._conversation_store.write_cursor(
            ConversationCursor(
                conversation_id=conversation_id,
                message_offset=len(messages) - 1,
                message_id=latest.message_id,
                run_id=persisted_identity[0],
                node_instance_id=persisted_identity[1],
                graph_checkpoint_ref=persisted_identity[2],
                metadata={
                    "agent_id": agent.agent_id,
                    "status": result.status.value,
                    "success": result.success,
                    "stop_reason": _result_stop_reason(result),
                    "iterations": result.iterations,
                    "message_type": latest.metadata.get("message_type"),
                },
            )
        )

    def _write_iteration_checkpoint(
        self,
        conversation_id: str,
        *,
        agent: AgentSpec,
        result: AgentLoopResult,
        run_id: str | None,
        node_instance_id: str | None,
        graph_checkpoint_ref: str | None,
    ) -> None:
        if self._conversation_store is None:
            return
        messages = self._conversation_store.read_messages(conversation_id)
        latest_message_id = messages[-1].message_id if messages else None
        persisted_identity = _persisted_graph_identity(
            run_id=run_id,
            node_instance_id=node_instance_id,
            graph_checkpoint_ref=graph_checkpoint_ref,
        )
        self._conversation_store.write_iteration_checkpoint(
            AgentIterationCheckpoint(
                conversation_id=conversation_id,
                agent_id=agent.agent_id,
                iteration=result.iterations,
                status=result.status.value,
                stop_reason=_result_stop_reason(result),
                run_id=persisted_identity[0],
                node_instance_id=persisted_identity[1],
                graph_checkpoint_ref=persisted_identity[2],
                message_id=latest_message_id,
                trace_summary=_trace_summary(result),
                diagnostics_summary=_diagnostics_summary(result),
                last_tool_observation=_last_tool_observation(result.events),
                llm_call_artifact_ids=[
                    artifact.artifact_id for artifact in result.llm_call_artifacts
                ],
                metadata=_iteration_checkpoint_metadata(result),
            )
        )


_MEMORY_TOOL_NAMES = frozenset({"memory.recall", "memory.explain"})


def _has_registered_memory_tools(registry: ToolRegistry) -> bool:
    return any(
        item.definition.name in _MEMORY_TOOL_NAMES
        for item in registry.list_registered_tools()
    )


def _execution_tool_registry(
    registry: ToolRegistry,
    *,
    memory_recall: ExecutionMemoryRecallPort | None,
    execution_identity: GraphExecutionIdentity | None,
    memory_policy: MemoryPolicy | None,
    memory_enabled: bool,
) -> ToolRegistry:
    if memory_recall is None and memory_enabled:
        return registry
    if memory_recall is not None and execution_identity is None:
        raise ValueError(
            "execution-bound memory recall requires GraphExecutionIdentity"
        )
    overlay = ToolRegistry()
    for registered in registry.list_registered_tools():
        if registered.definition.name in _MEMORY_TOOL_NAMES:
            continue
        overlay.register(
            registered.definition,
            registered.executor,
            graph_identity=registered.graph_identity,
        )
    if memory_recall is not None and memory_enabled:
        register_memory_tools(
            overlay,
            memory_recall=memory_recall,
            memory_policy=memory_policy,
            execution_identity=execution_identity,
        )
    return overlay


def _validate_graph_identity_arguments(
    *,
    run_id: str | None,
    node_instance_id: str | None,
    graph_checkpoint_ref: str | None,
) -> None:
    if node_instance_id is None and graph_checkpoint_ref is None:
        return
    if run_id is None or node_instance_id is None or graph_checkpoint_ref is None:
        raise ValueError(
            "Graph-bound AgentRunner execution requires run_id, node_instance_id, "
            "and graph_checkpoint_ref together"
        )
    for field_name, value in (
        ("run_id", run_id),
        ("node_instance_id", node_instance_id),
        ("graph_checkpoint_ref", graph_checkpoint_ref),
    ):
        if (
            not isinstance(value, str)
            or not value
            or value.strip() != value
            or len(value) > 2048
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in value
            )
        ):
            raise ValueError(f"{field_name} must be a valid non-empty string")


def _graph_execution_identity(
    *,
    run_id: str | None,
    graph_id: str | None,
    graph_version: str | None,
    graph_ref: str | None,
    graph_checksum: str | None,
    node_id: str | None,
    node_instance_id: str | None,
    activity_id: str | None,
    attempt: int | None,
) -> GraphExecutionIdentity | None:
    values = {
        "run_id": run_id,
        "graph_id": graph_id,
        "graph_version": graph_version,
        "graph_ref": graph_ref,
        "graph_checksum": graph_checksum,
        "node_id": node_id,
        "node_instance_id": node_instance_id,
        "activity_id": activity_id,
        "attempt": attempt,
    }
    if all(values[name] is None for name in values if name != "run_id"):
        return None
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise ValueError(
            "Graph-bound AgentRunner tool execution requires complete identity; "
            f"missing {', '.join(missing)}"
        )
    return GraphExecutionIdentity(**values)  # type: ignore[arg-type]


def _persisted_graph_identity(
    *,
    run_id: str | None,
    node_instance_id: str | None,
    graph_checkpoint_ref: str | None,
) -> tuple[str | None, str | None, str | None]:
    if node_instance_id is None and graph_checkpoint_ref is None:
        return None, None, None
    return run_id, node_instance_id, graph_checkpoint_ref


def _graph_identity(value: Any) -> tuple[str | None, str | None, str | None]:
    return (
        value.run_id,
        value.node_instance_id,
        value.graph_checkpoint_ref,
    )


def _assert_resume_graph_identity(
    value: Any,
    *,
    run_id: str | None,
    node_instance_id: str | None,
    graph_checkpoint_ref: str | None,
    label: str,
) -> None:
    actual = _graph_identity(value)
    expected = _persisted_graph_identity(
        run_id=run_id,
        node_instance_id=node_instance_id,
        graph_checkpoint_ref=graph_checkpoint_ref,
    )
    if actual != expected:
        raise ValueError(
            f"{label} Graph identity mismatch: stored={actual!r}, requested={expected!r}"
        )


def _conversation_result_payload(result: AgentLoopResult) -> dict[str, Any]:
    if result.success:
        return {
            "success": True,
            "status": result.status.value,
            "output": result.output,
            "diagnostics": result.diagnostics.to_dict() if result.diagnostics else None,
        }
    return {
        "success": False,
        "status": result.status.value,
        "error": result.error,
        "verdict": result.verdict.to_dict() if result.verdict else None,
        "diagnostics": result.diagnostics.to_dict() if result.diagnostics else None,
    }


def _conversation_message_from_event(
    conversation_id: str,
    agent: AgentSpec,
    event: dict[str, Any],
    *,
    run_id: str | None = None,
    graph_id: str | None = None,
    graph_version: str | None = None,
    graph_ref: str | None = None,
    graph_checksum: str | None = None,
    node_id: str | None = None,
    node_instance_id: str | None = None,
    graph_checkpoint_ref: str | None = None,
    activity_id: str | None = None,
    attempt: int | None = None,
    scope_kind: str,
) -> AgentMessageRecord | None:
    message_identity = _message_identity(
        scope_kind=scope_kind,
        run_id=run_id,
        graph_id=graph_id,
        graph_version=graph_version,
        graph_ref=graph_ref,
        graph_checksum=graph_checksum,
        node_id=node_id,
        node_instance_id=node_instance_id,
        graph_checkpoint_ref=graph_checkpoint_ref,
        activity_id=activity_id,
        attempt=attempt,
    )
    event_type = str(event.get("event_type") or "")
    if event_type == "tool_observation":
        observation = event.get("observation")
        if not isinstance(observation, dict):
            return None
        return AgentMessageRecord(
            conversation_id=conversation_id,
            role="tool",
            content=dict(observation),
            agent_id=agent.agent_id,
            **message_identity,
            metadata={
                "message_type": "agent_tool_observation",
                "event_type": event_type,
                "tool_name": observation.get("tool_name"),
                "tool_call_id": observation.get("tool_call_id"),
                "status": observation.get("status"),
            },
        )
    if event_type == "judge_retry":
        return AgentMessageRecord(
            conversation_id=conversation_id,
            role="judge",
            content={
                "feedback": event.get("feedback"),
                "verdict": event.get("verdict"),
                "via_tool": event.get("via_tool"),
            },
            agent_id=agent.agent_id,
            **message_identity,
            metadata={
                "message_type": "agent_judge_retry",
                "event_type": event_type,
                "status": "retry",
            },
        )
    if event_type in {
        "agent_stalled",
        "agent_retry_exhausted",
        "agent_waiting_for_approval",
        "agent_blocked",
        "agent_failed",
    }:
        return AgentMessageRecord(
            conversation_id=conversation_id,
            role="diagnostic",
            content=dict(event),
            agent_id=agent.agent_id,
            **message_identity,
            metadata={
                "message_type": "agent_loop_stop_event",
                "event_type": event_type,
                "status": event_type.removeprefix("agent_"),
            },
        )
    return None


def _message_identity(
    *,
    scope_kind: str,
    run_id: str | None,
    graph_id: str | None,
    graph_version: str | None,
    graph_ref: str | None,
    graph_checksum: str | None,
    node_id: str | None,
    node_instance_id: str | None,
    graph_checkpoint_ref: str | None,
    activity_id: str | None,
    attempt: int | None,
) -> dict[str, Any]:
    if scope_kind == CONVERSATION_SCOPE_STANDALONE:
        return {"scope_kind": CONVERSATION_SCOPE_STANDALONE}
    return {
        "scope_kind": CONVERSATION_SCOPE_GRAPH,
        "run_id": run_id,
        "graph_id": graph_id,
        "graph_version": graph_version,
        "graph_ref": graph_ref,
        "graph_checksum": graph_checksum,
        "node_id": node_id,
        "node_instance_id": node_instance_id,
        "graph_checkpoint_ref": graph_checkpoint_ref,
        "activity_id": activity_id,
        "attempt": attempt,
    }


def _result_stop_reason(result: AgentLoopResult) -> str:
    if result.diagnostics is None:
        return "unknown"
    return result.diagnostics.stop_reason.value


def _trace_summary(result: AgentLoopResult) -> dict[str, Any]:
    summary = result.trace.get("summary")
    if isinstance(summary, dict):
        return dict(summary)
    if result.diagnostics is not None:
        return dict(result.diagnostics.trace_summary)
    return {}


def _diagnostics_summary(result: AgentLoopResult) -> dict[str, Any]:
    if result.diagnostics is None:
        return {
            "status": result.status.value,
            "stop_reason": "unknown",
            "summary": result.error,
            "healthy": result.success,
        }
    return {
        "status": result.diagnostics.status.value,
        "stop_reason": result.diagnostics.stop_reason.value,
        "summary": result.diagnostics.summary,
        "healthy": result.diagnostics.healthy,
        "severity": result.diagnostics.severity.value,
        "issues": [issue.to_dict() for issue in result.diagnostics.issues],
        "suggestions": list(result.diagnostics.suggestions),
    }


def _last_tool_observation(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("event_type") != "tool_observation":
            continue
        observation = event.get("observation")
        if not isinstance(observation, dict):
            return None
        result = observation.get("result")
        call = observation.get("call")
        output = result.get("output") if isinstance(result, dict) else None
        output_approval_id = output.get("approval_id") if isinstance(output, dict) else None
        return {
            "iteration": event.get("iteration"),
            "tool_name": observation.get("tool_name")
            or (call.get("tool_name") if isinstance(call, dict) else None),
            "tool_call_id": observation.get("tool_call_id")
            or (call.get("call_id") if isinstance(call, dict) else None),
            "status": observation.get("status"),
            "approval_id": (
                result.get("approval_id") if isinstance(result, dict) else None
            )
            or output_approval_id,
            "error_type": result.get("error_type") if isinstance(result, dict) else None,
        }
    return None


def _iteration_checkpoint_metadata(result: AgentLoopResult) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "success": result.success,
        "metrics": result.metrics.to_dict(),
        "event_count": len(result.events),
    }
    if result.diagnostics is not None:
        metadata["approval_ids"] = _approval_ids_from_diagnostics(result.diagnostics.to_dict())
    if result.error:
        metadata["error"] = result.error
    return metadata


def _approval_ids_from_diagnostics(diagnostics: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for issue in diagnostics.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        metadata = issue.get("metadata")
        if not isinstance(metadata, dict):
            continue
        approval_id = metadata.get("approval_id")
        if approval_id is not None:
            ids.append(str(approval_id))
    return ids
