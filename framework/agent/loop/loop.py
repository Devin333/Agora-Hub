from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
import json
from typing import Any

from framework.agent.diagnostics import (
    AgentLoopDiagnosticsBuilder,
    AgentLoopStallDetector,
    StallDetection,
    max_iterations_detection,
)
from framework.agent.loop.events import AgentLoopEventRecorder
from framework.agent.loop.extensions import (
    OutputNormalizer,
    identity_output_normalizer,
)
from framework.agent.loop.judge import OutputJudge
from framework.agent.loop.output_budget import (
    output_budget_judge_verdict,
    resolve_agent_output_budget,
    validate_agent_output_budget,
)
from framework.agent.models import (
    AgentAction,
    AgentLoopMetrics,
    AgentLoopResult,
    AgentLoopStatus,
    AgentLoopStopReason,
    AgentSpec,
    DelegateBatchCandidate,
    DelegateBatchProposal,
    JudgeDecision,
    JudgeVerdict,
    LLMCallArtifact,
)
from framework.agent.models.orchestration import (
    AgentOrchestrationPort,
    AgentOrchestrationRequest,
    AgentOrchestrationResult,
    ParentObservationLimits,
)
from framework.agent.skill_call import SkillCall
from framework.agent.skill_context import (
    AgentSkillRuntime,
    SkillRunnerProtocol,
)
from framework.agent.skill_observation import SkillObservation
from framework.agent.skill_selection import SkillSelectionPolicy
from framework.agent.loop.parser import AgentActionParser
from framework.agent.loop.prompt import PromptBuilder
from framework.agent.subagents import (
    SubAgentExecutor,
    SubAgentResult,
    SubAgentTask,
)
from framework.agent.models.trace import AgentLoopTrace, IterationTrace
from framework.events import TraceContext, W3CTracePropagator
from framework.events.canonical import checksum_for
from framework.llm.budget import (
    GlobalBudgetCheck,
    GlobalBudgetExceededError,
    GlobalBudgetTracker,
    LLMBudgetOperation,
)
from framework.llm.context import estimate_request_tokens
from framework.llm.models import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamEvent,
    LLMStreamAccumulator,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.llm.structured_output import (
    ManagedStructuredOutputError,
    compile_structured_output_contract,
    require_managed_structured_output_for_contract,
)
from framework.memory import (
    AgentMemoryAdapter,
    DEFAULT_AGENT_MEMORY_POLICY,
    MemoryPolicy,
    MemoryRuntime,
)
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.tool import ToolExecutor
from framework.tool import (
    ArtifactRef,
    ToolCall,
    ToolObservation,
    ToolPolicy,
    ToolResult,
    ToolStatus,
)


class AgentLoop:
    def __init__(
        self,
        *,
        llm_client: LLMClient,
        tool_executor: ToolExecutor,
        prompt_builder: PromptBuilder | None = None,
        action_parser: AgentActionParser | None = None,
        output_judge: OutputJudge | None = None,
        output_normalizer: OutputNormalizer | None = None,
        global_budget_tracker: GlobalBudgetTracker | None = None,
        subagent_executor: SubAgentExecutor | None = None,
        orchestration_port: AgentOrchestrationPort | None = None,
        orchestration_enabled: bool = False,
        memory_runtime: MemoryRuntime | None = None,
        memory_recall: ExecutionMemoryRecallPort | None = None,
        memory_policy: MemoryPolicy | None = None,
        memory_adapter: AgentMemoryAdapter | None = None,
        skill_registry: Any | None = None,
        skill_runner: SkillRunnerProtocol | None = None,
        skill_selection_policy: SkillSelectionPolicy | None = None,
        agent_skill_runtime: AgentSkillRuntime | None = None,
        runtime_event_sink: Any | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._tool_executor = tool_executor
        self._prompt_builder = prompt_builder or PromptBuilder()
        self._action_parser = action_parser or AgentActionParser()
        self._output_judge = output_judge or OutputJudge()
        self._output_normalizer = output_normalizer or identity_output_normalizer
        self._global_budget_tracker = global_budget_tracker
        self._active_budget_operation: LLMBudgetOperation | None = None
        self._active_budget_tracker: GlobalBudgetTracker | None = None
        self._subagent_executor = subagent_executor
        if orchestration_port is not None and not isinstance(
            orchestration_port, AgentOrchestrationPort
        ):
            raise TypeError("orchestration_port must implement AgentOrchestrationPort")
        if not isinstance(orchestration_enabled, bool):
            raise TypeError("orchestration_enabled must be boolean")
        self._orchestration_port = orchestration_port
        self._orchestration_enabled = orchestration_enabled
        if memory_recall is not None and not isinstance(
            memory_recall, ExecutionMemoryRecallPort
        ):
            raise TypeError(
                "memory_recall must implement ExecutionMemoryRecallPort"
            )
        self._memory_runtime = memory_runtime
        self._memory_recall = memory_recall
        self._memory_policy = memory_policy or DEFAULT_AGENT_MEMORY_POLICY
        self._memory_adapter = memory_adapter or AgentMemoryAdapter()
        if agent_skill_runtime is not None:
            self._skill_runtime = agent_skill_runtime
        elif skill_registry is not None and skill_runner is not None:
            self._skill_runtime = AgentSkillRuntime(
                registry=skill_registry,
                runner=skill_runner,
                selection_policy=skill_selection_policy,
            )
        else:
            self._skill_runtime = None
        self._runtime_event_sink = runtime_event_sink

    def run(
        self,
        agent: AgentSpec,
        inputs: dict[str, Any],
        tools: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        execution_identity: GraphExecutionIdentity | None = None,
        graph_checkpoint_ref: str | None = None,
        standalone: bool = False,
    ) -> AgentLoopResult:
        if not isinstance(standalone, bool):
            raise TypeError("standalone must be boolean")
        if execution_identity is not None and not isinstance(
            execution_identity,
            GraphExecutionIdentity,
        ):
            raise TypeError(
                "AgentLoop execution_identity must be GraphExecutionIdentity"
            )
        if standalone and (
            execution_identity is not None or graph_checkpoint_ref is not None
        ):
            raise ValueError("standalone AgentLoop cannot carry Graph identity")
        if standalone and self._memory_recall is not None:
            raise ValueError(
                "standalone AgentLoop cannot use execution-bound memory recall"
            )
        if execution_identity is None and not standalone:
            raise ValueError(
                "AgentLoop requires an exact GraphExecutionIdentity; "
                "use standalone=True for an explicitly isolated run"
            )
        if (
            execution_identity is not None
            and self._memory_recall is None
            and self._memory_runtime is not None
        ):
            raise ValueError(
                "Graph-bound AgentLoop memory recall requires an "
                "ExecutionMemoryRecallPort"
            )
        if self._memory_recall is not None:
            self._memory_recall.validate_execution(execution_identity)
        if execution_identity is not None and (
            not isinstance(graph_checkpoint_ref, str)
            or not graph_checkpoint_ref
            or graph_checkpoint_ref.strip() != graph_checkpoint_ref
        ):
            raise ValueError(
                "Graph-bound AgentLoop requires a canonical graph_checkpoint_ref"
            )
        effective_run_id = run_id or _run_id_from_inputs(inputs)
        if execution_identity is not None and execution_identity.run_id != effective_run_id:
            raise ValueError("AgentLoop execution identity does not match run_id")
        metrics = AgentLoopMetrics()
        self._active_budget_operation = None
        self._active_budget_tracker = None
        events = AgentLoopEventRecorder(
            agent_id=agent.agent_id,
            run_id=effective_run_id,
            execution_identity=execution_identity,
            runtime_event_sink=self._runtime_event_sink,
        )
        trace = AgentLoopTrace(
            agent_id=agent.agent_id,
            execution_identity=execution_identity,
        )
        diagnostics = AgentLoopDiagnosticsBuilder(agent_id=agent.agent_id, trace=trace)
        stall_detector = AgentLoopStallDetector(agent.loop_policy)

        events.started()
        feedback: str | None = None
        tool_observations: list[ToolObservation] = []
        skill_observations: list[SkillObservation] = []
        called_tools: list[str] = []
        last_verdict: JudgeVerdict | None = None
        llm_call_artifacts: list[LLMCallArtifact] = []
        judge_retries = 0
        parser_errors = 0
        empty_output_retries = 0

        for iteration in range(1, agent.loop_policy.max_iterations + 1):
            metrics.iterations = iteration
            iteration_trace = trace.start_iteration(
                iteration,
                feedback=feedback,
                tool_observation_count_before=len(tool_observations),
                tools_available=_tool_names(tools),
            )
            events.iteration_started(
                iteration=iteration,
                feedback=feedback,
                tool_observation_count=len(tool_observations),
                tools_available=_tool_names(tools),
            )
            memory_context = self._memory_context_for_llm(
                agent=agent,
                inputs=inputs,
                execution_identity=execution_identity,
            )
            prompt_inputs = dict(inputs)
            skill_prompt_section = self._skill_prompt_section(agent=agent, inputs=inputs)

            request = self._prompt_builder.build(
                agent,
                prompt_inputs,
                feedback=feedback,
                tool_observations=[
                    _prompt_safe_observation(observation, agent.resolved_tool_policy()).to_dict()
                    for observation in tool_observations
                ]
                + [observation.to_agent_message() for observation in skill_observations],
                tools=tools,
                memory_context=memory_context,
                skill_prompt_section=skill_prompt_section,
            )
            if execution_identity is not None:
                request = request.clone(execution_identity=execution_identity)
            trace.record_prompt(iteration_trace, request)
            try:
                self._reserve_global_budget_before_llm_call(
                    metrics,
                    request=request,
                    agent=agent,
                    iteration=iteration,
                )
            except GlobalBudgetExceededError as exc:
                trace.mark_stop_candidate(
                    iteration_trace,
                    AgentLoopStopReason.GLOBAL_BUDGET_EXCEEDED.value,
                )
                return self._global_budget_exceeded_result(
                    agent=agent,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iterations=iteration,
                    exc=exc,
                    llm_call_artifacts=llm_call_artifacts,
                )
            except Exception as exc:
                if not _is_global_budget_exception(exc):
                    raise
                trace.mark_stop_candidate(
                    iteration_trace,
                    AgentLoopStopReason.GLOBAL_BUDGET_EXCEEDED.value,
                )
                return self._global_budget_exceeded_result(
                    agent=agent,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iterations=iteration,
                    exc=exc,
                    llm_call_artifacts=llm_call_artifacts,
                )
            try:
                response = self._complete_llm_request(
                    request,
                    agent=agent,
                    iteration=iteration,
                    metrics=metrics,
                    events=events,
                )
            except GlobalBudgetExceededError as exc:
                metrics.llm_error_count += 1
                trace.record_llm_error(iteration_trace, exc)
                events.llm_failed(iteration=iteration, exc=exc)
                return self._global_budget_exceeded_result(
                    agent=agent,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iterations=iteration,
                    exc=exc,
                    llm_call_artifacts=llm_call_artifacts,
                )
            except Exception as exc:
                self._mark_active_budget_indeterminate(
                    metrics,
                    reason="agent_direct_llm_dispatch_indeterminate"
                )
                if _is_global_budget_exception(exc):
                    metrics.llm_error_count += 1
                    trace.record_llm_error(iteration_trace, exc)
                    events.llm_failed(iteration=iteration, exc=exc)
                    return self._global_budget_exceeded_result(
                        agent=agent,
                        metrics=metrics,
                        events=events,
                        trace=trace,
                        diagnostics=diagnostics,
                        iterations=iteration,
                        exc=exc,
                        llm_call_artifacts=llm_call_artifacts,
                    )
                structured_verdict = _structured_output_failure_verdict(
                    exc,
                    request=request,
                )
                if structured_verdict is not None:
                    metrics.llm_error_count += 1
                    safe_failure = _ManagedStructuredOutputAttemptError()
                    trace.record_llm_error(iteration_trace, safe_failure)
                    events.llm_failed(iteration=iteration, exc=safe_failure)
                    last_verdict = structured_verdict
                    trace.record_judge(iteration_trace, structured_verdict)
                    verdict_result = self._handle_verdict(
                        agent=agent,
                        action=AgentAction(
                            action_type="final_output",
                            output={"structured_output_rejected": True},
                        ),
                        run_id=effective_run_id,
                        metrics=metrics,
                        events=events,
                        trace=trace,
                        diagnostics=diagnostics,
                        stall_detector=stall_detector,
                        iteration_trace=iteration_trace,
                        verdict=structured_verdict,
                        judge_retries=judge_retries,
                        empty_output_retries=empty_output_retries,
                        via_tool=None,
                        llm_call_artifacts=llm_call_artifacts,
                    )
                    if isinstance(verdict_result, AgentLoopResult):
                        return verdict_result
                    judge_retries, empty_output_retries, feedback = verdict_result
                    trace.finish_iteration(iteration_trace)
                    continue
                metrics.llm_error_count += 1
                trace.record_llm_error(iteration_trace, exc)
                events.llm_failed(iteration=iteration, exc=exc)
                return self._failed_result(
                    agent=agent,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iterations=iteration,
                    stop_reason=AgentLoopStopReason.LLM_FAILED,
                    error=str(exc),
                    llm_call_artifacts=llm_call_artifacts,
                )

            llm_artifact = _llm_call_artifact(
                agent=agent,
                iteration=iteration,
                request=request,
                response=response,
            )
            llm_trace = trace.record_llm_call(iteration_trace, response)
            trace.record_llm_artifact(iteration_trace, llm_artifact.artifact_id)
            llm_call_artifacts.append(llm_artifact)
            metrics.llm_calls += 1
            metrics.add_usage(response.usage)
            try:
                self._record_global_budget_call(metrics, response)
            except GlobalBudgetExceededError as exc:
                trace.mark_stop_candidate(
                    iteration_trace,
                    AgentLoopStopReason.GLOBAL_BUDGET_EXCEEDED.value,
                )
                events.llm_call(
                    iteration=iteration,
                    token_usage=response.usage.to_dict(),
                    response_chars=llm_trace.response_chars,
                    provider=llm_trace.provider,
                    model=llm_trace.model,
                    route_id=llm_trace.route_id,
                    deployment_id=llm_trace.deployment_id,
                    fallback_used=llm_trace.fallback_used,
                    fallback_count=llm_trace.fallback_count,
                    router_event_count=llm_trace.router_event_count,
                )
                return self._global_budget_exceeded_result(
                    agent=agent,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iterations=iteration,
                    exc=exc,
                    llm_call_artifacts=llm_call_artifacts,
                )
            events.llm_call(
                iteration=iteration,
                token_usage=response.usage.to_dict(),
                response_chars=llm_trace.response_chars,
                provider=llm_trace.provider,
                model=llm_trace.model,
                route_id=llm_trace.route_id,
                deployment_id=llm_trace.deployment_id,
                fallback_used=llm_trace.fallback_used,
                fallback_count=llm_trace.fallback_count,
                router_event_count=llm_trace.router_event_count,
            )

            response_content = response.content or ""
            try:
                if response.structured_output is not None:
                    managed_contract = compile_structured_output_contract(
                        request.structured_output_schema_source(),
                        schema_name=request.output_schema_name,
                    )
                    try:
                        require_managed_structured_output_for_contract(
                            response=response,
                            contract=managed_contract,
                        )
                    except ManagedStructuredOutputError as exc:
                        raise ValueError(
                            "LLM structured response failed managed envelope validation"
                        ) from exc
                    action = AgentAction(
                        action_type="final_output",
                        output=dict(response.structured_output),
                        metadata={
                            "structured_output_validation": _mapping_or_empty(
                                response.metadata.get("structured_output_validation")
                            )
                        },
                    )
                elif request.output_schema is not None:
                    raise ValueError(
                        "LLM structured response is missing a managed terminal object"
                    )
                else:
                    action = self._action_parser.parse(response_content)
            except Exception as exc:
                parser_errors += 1
                metrics.parser_errors += 1
                feedback = f"parser error: {exc}"
                parser_error = trace.record_parser_error(
                    iteration_trace,
                    exc=exc,
                    content=response_content,
                    max_preview_chars=agent.loop_policy.max_trace_preview_chars,
                )
                last_verdict = JudgeVerdict(
                    decision=JudgeDecision.RETRY,
                    confidence=0.0,
                    feedback=feedback,
                    schema_errors=[str(exc)],
                )
                events.parser_error(
                    iteration=iteration,
                    error_type=parser_error.error_type,
                    error_message=parser_error.error_message,
                    parser_errors=parser_errors,
                    max_parser_errors=agent.loop_policy.max_parser_errors,
                )
                detection = stall_detector.after_parser_error(
                    trace=trace,
                    iteration=iteration,
                    parser_errors=parser_errors,
                )
                if detection.stalled:
                    trace.mark_stop_candidate(
                        iteration_trace,
                        detection.stop_reason.value if detection.stop_reason else "parser_error",
                    )
                    return self._retry_exhausted_result(
                        agent=agent,
                        metrics=metrics,
                        events=events,
                        trace=trace,
                        diagnostics=diagnostics,
                        iterations=iteration,
                        verdict=last_verdict,
                        stop_reason=AgentLoopStopReason.PARSER_RETRY_EXHAUSTED,
                        detection=detection,
                        llm_call_artifacts=llm_call_artifacts,
                )
                trace.finish_iteration(iteration_trace)
                continue

            parser_errors = 0
            action_trace = trace.record_action(
                iteration_trace,
                action_type=_parsed_action_type(action),
                tool_name=_parsed_tool_name(action),
                output=_parsed_output(action),
            )
            events.action_parsed(
                iteration=iteration,
                action_type=action_trace.action_type,
                tool_name=action_trace.tool_name,
                output_keys=action_trace.output_keys,
            )

            if isinstance(action, SkillCall):
                feedback = self._handle_skill_action(
                    action=action,
                    agent_run_id=effective_run_id or agent.agent_id,
                    execution_identity=execution_identity,
                    skill_observations=skill_observations,
                )
                trace.finish_iteration(iteration_trace)
                continue

            if action.is_tool_call():
                tool_result = self._handle_tool_action(
                    agent=agent,
                    inputs=inputs,
                    action=action,
                    run_id=effective_run_id,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    stall_detector=stall_detector,
                    iteration_trace=iteration_trace,
                    tool_observations=tool_observations,
                    called_tools=called_tools,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(tool_result, AgentLoopResult):
                    return tool_result
                feedback = tool_result
                trace.finish_iteration(iteration_trace)
                continue

            if _action_type_value(action.action_type) in {"delegate", "delegate_to_subagent"}:
                result = self._handle_delegate_action(
                    agent=agent,
                    inputs=inputs,
                    action=action,
                    run_id=effective_run_id,
                    execution_identity=execution_identity,
                    graph_checkpoint_ref=graph_checkpoint_ref,
                    standalone=standalone,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iteration_trace=iteration_trace,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(result, AgentLoopResult):
                    return result
                feedback = result
                trace.finish_iteration(iteration_trace)
                continue

            if _action_type_value(action.action_type) == "delegate_batch":
                result = self._handle_delegate_batch_action(
                    agent=agent,
                    action=action,
                    run_id=effective_run_id,
                    execution_identity=execution_identity,
                    graph_checkpoint_ref=graph_checkpoint_ref,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    iteration_trace=iteration_trace,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(result, AgentLoopResult):
                    return result
                feedback = result
                trace.finish_iteration(iteration_trace)
                continue

            raw_output = action.output or {}
            budget_verdict = self._output_budget_verdict(agent=agent, output=raw_output)
            if budget_verdict is not None:
                last_verdict = budget_verdict
                trace.record_judge(iteration_trace, budget_verdict)
                verdict_result = self._handle_verdict(
                    agent=agent,
                    action=AgentAction(
                        action_type=action.action_type,
                        content=action.content,
                        output=raw_output,
                    ),
                    run_id=effective_run_id,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    stall_detector=stall_detector,
                    iteration_trace=iteration_trace,
                    verdict=budget_verdict,
                    judge_retries=judge_retries,
                    empty_output_retries=empty_output_retries,
                    via_tool=None,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(verdict_result, AgentLoopResult):
                    return verdict_result
                judge_retries, empty_output_retries, feedback = verdict_result
                trace.finish_iteration(iteration_trace)
                continue

            normalized_output = self._normalize_output(
                agent=agent,
                output=raw_output,
                inputs=inputs,
            )
            action = AgentAction(action_type=action.action_type, content=action.content, output=normalized_output)
            budget_verdict = self._output_budget_verdict(agent=agent, output=normalized_output)
            if budget_verdict is not None:
                last_verdict = budget_verdict
                trace.record_judge(iteration_trace, budget_verdict)
                verdict_result = self._handle_verdict(
                    agent=agent,
                    action=action,
                    run_id=effective_run_id,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    stall_detector=stall_detector,
                    iteration_trace=iteration_trace,
                    verdict=budget_verdict,
                    judge_retries=judge_retries,
                    empty_output_retries=empty_output_retries,
                    via_tool=None,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(verdict_result, AgentLoopResult):
                    return verdict_result
                judge_retries, empty_output_retries, feedback = verdict_result
                trace.finish_iteration(iteration_trace)
                continue
            verdict = self._output_judge.judge(
                agent=agent,
                action=action,
                called_tools=called_tools,
                inputs=inputs,
            )
            last_verdict = verdict
            trace.record_judge(iteration_trace, verdict)
            verdict_result = self._handle_verdict(
                agent=agent,
                action=action,
                run_id=effective_run_id,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                stall_detector=stall_detector,
                iteration_trace=iteration_trace,
                verdict=verdict,
                judge_retries=judge_retries,
                empty_output_retries=empty_output_retries,
                via_tool=None,
                llm_call_artifacts=llm_call_artifacts,
            )
            if isinstance(verdict_result, AgentLoopResult):
                return verdict_result
            judge_retries, empty_output_retries, feedback = verdict_result
            trace.finish_iteration(iteration_trace)

        detection = max_iterations_detection(metrics.iterations, agent.loop_policy)
        return self._stalled_result(
            agent=agent,
            metrics=metrics,
            events=events,
            trace=trace,
            diagnostics=diagnostics,
            iterations=metrics.iterations,
            verdict=last_verdict,
            detection=detection,
            llm_call_artifacts=llm_call_artifacts,
        )

    def _handle_tool_action(
        self,
        *,
        agent: AgentSpec,
        inputs: dict[str, Any],
        action: AgentAction,
        run_id: str | None,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        stall_detector: AgentLoopStallDetector,
        iteration_trace: IterationTrace,
        tool_observations: list[ToolObservation],
        called_tools: list[str],
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> str | AgentLoopResult | None:
        iteration = iteration_trace.iteration
        tool_policy = agent.resolved_tool_policy()
        if not agent.memory_enabled:
            tool_policy = replace(tool_policy, blocked_tools=[
                *tool_policy.blocked_tools, "memory.recall", "memory.explain",
            ])
        tool_call = ToolCall(
            tool_name=action.tool_name or "",
            arguments=action.tool_args,
            requested_by_agent_id=agent.agent_id,
        )
        budget_blocked = metrics.tool_calls >= _max_tool_calls_per_agent(tool_policy)
        if budget_blocked:
            observation = _blocked_tool_budget_observation(tool_call, tool_policy)
            events.tool_budget_blocked(
                iteration=iteration,
                tool_name=tool_call.tool_name,
                max_tool_calls=_max_tool_calls_per_agent(tool_policy),
            )
        else:
            observation = self._execute_tool(tool_call, _execution_tool_policy(tool_policy))
        observation = _prompt_safe_observation(observation, tool_policy)
        self._record_tool_observation_memory_candidate(
            agent=agent,
            observation=observation,
            trace=trace,
        )

        tool_observations.append(observation)
        called_tools.append(observation.call.tool_name)
        metrics.tool_calls += 1
        metrics.record_tool_status(observation.status, elapsed_ms=observation.elapsed_ms)
        events.tool_call(
            iteration=iteration,
            tool_name=observation.call.tool_name,
            call_id=observation.call.call_id,
        )
        events.tool_observation(iteration=iteration, observation=observation.to_dict())
        tool_trace = trace.record_tool_call(
            iteration_trace,
            observation,
            max_preview_chars=agent.loop_policy.max_trace_preview_chars,
        )

        repeated_count = trace.count_tool_signature(tool_trace.signature.key)
        if repeated_count > 1:
            metrics.repeated_tool_calls += 1
            events.repeated_tool_call_detected(
                iteration=iteration,
                tool_name=tool_trace.tool_name,
                signature=tool_trace.signature.key,
                count=repeated_count,
                limit=agent.loop_policy.max_repeated_tool_calls,
            )
        detection = stall_detector.after_tool_call(trace, tool_trace)
        if detection.stalled:
            metrics.stalled_iterations += 1
            trace.mark_stop_candidate(
                iteration_trace,
                detection.stop_reason.value if detection.stop_reason else "stalled",
            )
            return self._stalled_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration,
                verdict=None,
                detection=detection,
                llm_call_artifacts=llm_call_artifacts,
            )

        if observation.status == ToolStatus.APPROVAL_REQUIRED:
            events.tool_approval_required(
                iteration=iteration,
                tool_name=observation.call.tool_name,
                approval_id=observation.result.approval_id,
            )
            return self._waiting_for_approval_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration,
                tool_name=observation.call.tool_name,
                approval_id=observation.result.approval_id,
                llm_call_artifacts=llm_call_artifacts,
            )

        if observation.status == ToolStatus.SUCCEEDED and _is_control_approval_request(observation):
            control_metadata = _control_approval_metadata(observation)
            metrics.tool_approval_requests += 1
            return self._waiting_for_approval_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration,
                tool_name=observation.call.tool_name,
                approval_id=str(control_metadata["approval_id"]),
                approval_kind=str(control_metadata["approval_kind"]),
                control_action=str(control_metadata["control_action"]),
                escalation_type=control_metadata.get("escalation_type"),
                llm_call_artifacts=llm_call_artifacts,
            )

        if observation.status == ToolStatus.SUCCEEDED and _is_control_set_output(observation):
            raw_control_output = _control_output(observation)
            budget_verdict = self._output_budget_verdict(
                agent=agent,
                output=raw_control_output,
            )
            if budget_verdict is not None:
                trace.record_judge(iteration_trace, budget_verdict)
                result = self._handle_verdict(
                    agent=agent,
                    action=AgentAction(action_type="final_output", output=raw_control_output),
                    run_id=run_id,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    stall_detector=stall_detector,
                    iteration_trace=iteration_trace,
                    verdict=budget_verdict,
                    judge_retries=metrics.judge_retries,
                    empty_output_retries=0,
                    via_tool=observation.call.tool_name,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(result, AgentLoopResult):
                    return result
                return result[2]
            control_output = self._normalize_output(
                agent=agent,
                output=raw_control_output,
                inputs=inputs,
            )
            budget_verdict = self._output_budget_verdict(agent=agent, output=control_output)
            if budget_verdict is not None:
                trace.record_judge(iteration_trace, budget_verdict)
                result = self._handle_verdict(
                    agent=agent,
                    action=AgentAction(action_type="final_output", output=control_output),
                    run_id=run_id,
                    metrics=metrics,
                    events=events,
                    trace=trace,
                    diagnostics=diagnostics,
                    stall_detector=stall_detector,
                    iteration_trace=iteration_trace,
                    verdict=budget_verdict,
                    judge_retries=metrics.judge_retries,
                    empty_output_retries=0,
                    via_tool=observation.call.tool_name,
                    llm_call_artifacts=llm_call_artifacts,
                )
                if isinstance(result, AgentLoopResult):
                    return result
                return result[2]
            verdict = self._output_judge.judge(
                agent=agent,
                action=AgentAction(
                    action_type="final_output",
                    output=control_output,
                ),
                called_tools=called_tools,
                inputs=inputs,
            )
            trace.record_judge(iteration_trace, verdict)
            result = self._handle_verdict(
                agent=agent,
                action=AgentAction(action_type="final_output", output=control_output),
                run_id=run_id,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                stall_detector=stall_detector,
                iteration_trace=iteration_trace,
                verdict=verdict,
                judge_retries=metrics.judge_retries,
                empty_output_retries=0,
                via_tool=observation.call.tool_name,
                llm_call_artifacts=llm_call_artifacts,
            )
            if isinstance(result, AgentLoopResult):
                return result
            return result[2]

        if observation.status == ToolStatus.BLOCKED:
            return observation.result.error_message or "tool call blocked"
        if observation.status == ToolStatus.FAILED:
            return observation.result.error_message or "tool call failed"
        if observation.status == ToolStatus.TIMEOUT:
            return observation.result.error_message or "tool call timed out"
        return None

    def _handle_skill_action(
        self,
        *,
        action: SkillCall,
        agent_run_id: str,
        execution_identity: GraphExecutionIdentity | None,
        skill_observations: list[SkillObservation],
    ) -> str:
        if self._skill_runtime is None:
            observation = SkillObservation(
                call_id=action.ensure_call_id().call_id or "",
                skill_name=action.skill_name,
                status="failed",
                errors=["Skill runtime is not configured"],
            )
        else:
            observation = self._skill_runtime.execute_call(
                action,
                agent_run_id=agent_run_id,
                execution_identity=execution_identity,
            )
        skill_observations.append(observation)
        if observation.errors:
            return f"skill observation: {observation.skill_name} {observation.status}: {observation.errors[0]}"
        return f"skill observation: {observation.skill_name} {observation.status}: {observation.output_summary}"

    def _handle_delegate_batch_action(
        self,
        *,
        agent: AgentSpec,
        action: AgentAction,
        run_id: str | None,
        execution_identity: GraphExecutionIdentity | None,
        graph_checkpoint_ref: str | None,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iteration_trace: IterationTrace,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> str | AgentLoopResult:
        candidate = action.delegate_batch
        if candidate is None:
            verdict = JudgeVerdict(
                decision=JudgeDecision.RETRY,
                confidence=1.0,
                feedback="delegate_batch candidate is missing",
                validation_errors=["delegate_batch candidate is missing"],
            )
            trace.record_judge(iteration_trace, verdict)
            return verdict.feedback or "delegate_batch candidate is missing"
        if not agent.allow_subagents:
            verdict = JudgeVerdict(
                decision=JudgeDecision.BLOCK,
                confidence=1.0,
                feedback="delegate_batch is not allowed by the parent Agent policy",
                policy_violations=["delegate_batch not allowed"],
            )
            trace.record_judge(iteration_trace, verdict)
            events.judge_block(iteration=iteration_trace.iteration, verdict=verdict.to_dict())
            return self._blocked_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration_trace.iteration,
                verdict=verdict,
                stop_reason=AgentLoopStopReason.AGENT_POLICY_BLOCKED,
                via_tool=None,
                llm_call_artifacts=llm_call_artifacts,
            )
        if not self._orchestration_enabled or self._orchestration_port is None:
            verdict = JudgeVerdict(
                decision=JudgeDecision.ESCALATE,
                confidence=1.0,
                feedback="delegate_batch orchestration is unavailable",
                validation_errors=["agent_orchestration_unavailable"],
            )
            trace.record_judge(iteration_trace, verdict)
            return "delegate_batch deferred: agent_orchestration_unavailable"
        try:
            policy_ref, max_tasks, limits = _orchestration_policy(agent)
            request = AgentOrchestrationRequest(
                parent_agent_id=agent.agent_id,
                parent_turn_id=_orchestration_parent_turn_id(
                    agent.agent_id, execution_identity, iteration_trace.iteration
                ),
                run_id=run_id,
                execution_identity=execution_identity,
                graph_checkpoint_ref=graph_checkpoint_ref,
                policy_ref=policy_ref,
                max_tasks_per_group=max_tasks,
                parent_observation_limits=limits,
                candidate=candidate,
            )
        except (TypeError, ValueError) as exc:
            verdict = JudgeVerdict(
                decision=JudgeDecision.RETRY,
                confidence=1.0,
                feedback=f"delegate_batch rejected: {exc}",
                validation_errors=["delegate_batch_candidate_rejected"],
            )
            trace.record_judge(iteration_trace, verdict)
            return verdict.feedback or "delegate_batch rejected"
        events.subagent_delegation_requested(
            iteration=iteration_trace.iteration,
            parent_agent_id=agent.agent_id,
            child_agent_id="delegate_batch",
            handoff_reason="Harness-owned multi-child delegation",
            task=candidate.correlation_id,
        )
        try:
            result = self._orchestration_port.dispatch(request)
        except Exception as exc:
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id="delegate_batch",
                status="unavailable",
                error=type(exc).__name__,
            )
            verdict = JudgeVerdict(
                decision=JudgeDecision.ESCALATE,
                confidence=1.0,
                feedback="delegate_batch orchestration is unavailable",
                validation_errors=["agent_orchestration_unavailable"],
            )
            trace.record_judge(iteration_trace, verdict)
            return "delegate_batch deferred: agent_orchestration_unavailable"
        if not isinstance(result, AgentOrchestrationResult):
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id="delegate_batch",
                status="unavailable",
                error="invalid_orchestration_result",
            )
            return "delegate_batch deferred: agent_orchestration_unavailable"
        observation = result.observation.project(limits)
        if result.status.casefold() != "succeeded":
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id="delegate_batch",
                status=result.status,
                error=result.reason_code or "agent_orchestration_group_not_succeeded",
            )
            return self._orchestration_group_failed_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration_trace.iteration,
                observation=observation,
                result=result,
                llm_call_artifacts=llm_call_artifacts,
            )
        events.subagent_completed(
            iteration=iteration_trace.iteration,
            child_agent_id="delegate_batch",
            output_keys=sorted(observation),
            summary=f"group={observation['group_id']} status={result.status}",
        )
        return "delegate_batch joined observation: " + json.dumps(
            observation,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _orchestration_group_failed_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        observation: dict[str, Any],
        result: AgentOrchestrationResult,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        reason = result.reason_code or "agent_orchestration_group_not_succeeded"
        result_diagnostics = diagnostics.failed(
            metrics=metrics,
            iterations=iterations,
            stop_reason=AgentLoopStopReason.UNKNOWN_FAILED,
            error=reason,
        )
        events.failed(
            iteration=iterations,
            stop_reason=AgentLoopStopReason.UNKNOWN_FAILED.value,
            error=reason,
        )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=AgentLoopStopReason.UNKNOWN_FAILED,
            success=False,
            status=AgentLoopStatus.FAILED,
            output={"delegate_batch_observation": observation},
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=reason,
        )

    def _handle_legacy_delegate_orchestration(
        self,
        *,
        agent: AgentSpec,
        child_agent_id: str,
        subagent_task: str,
        run_id: str | None,
        execution_identity: GraphExecutionIdentity | None,
        graph_checkpoint_ref: str | None,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iteration_trace: IterationTrace,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> str | AgentLoopResult:
        if self._orchestration_port is None:
            return _legacy_orchestration_unavailable_feedback(
                child_agent_id=child_agent_id,
                events=events,
                iteration=iteration_trace.iteration,
                trace=trace,
                iteration_trace=iteration_trace,
            )
        try:
            policy_ref, max_tasks, limits = _orchestration_policy(agent)
            capability_hint, input_refs, output_role = _legacy_delegate_mapping(
                agent,
                child_agent_id,
            )
            candidate = DelegateBatchCandidate(
                correlation_id=(
                    f"legacy:{agent.agent_id}:{iteration_trace.iteration}:{child_agent_id}"
                ),
                tasks=(
                    DelegateBatchProposal(
                        logical_task_id=child_agent_id,
                        objective=subagent_task,
                        capability_hint=capability_hint,
                        input_refs=input_refs,
                        output_role=output_role,
                    ),
                ),
            )
            request = AgentOrchestrationRequest(
                parent_agent_id=agent.agent_id,
                parent_turn_id=_orchestration_parent_turn_id(
                    agent.agent_id, execution_identity, iteration_trace.iteration
                ),
                run_id=run_id,
                execution_identity=execution_identity,
                graph_checkpoint_ref=graph_checkpoint_ref,
                policy_ref=policy_ref,
                max_tasks_per_group=max_tasks,
                parent_observation_limits=limits,
                candidate=candidate,
            )
        except (TypeError, ValueError) as exc:
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id=child_agent_id,
                status="deferred",
                error=f"legacy_delegate_mapping_unavailable: {exc}",
            )
            verdict = JudgeVerdict(
                decision=JudgeDecision.ESCALATE,
                confidence=1.0,
                feedback="legacy delegate orchestration is unavailable",
                validation_errors=["legacy_delegate_mapping_unavailable"],
            )
            trace.record_judge(iteration_trace, verdict)
            return "delegate deferred: legacy_delegate_mapping_unavailable"
        try:
            result = self._orchestration_port.dispatch(request)
        except Exception as exc:
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id=child_agent_id,
                status="unavailable",
                error=type(exc).__name__,
            )
            return "delegate deferred: agent_orchestration_unavailable"
        if not isinstance(result, AgentOrchestrationResult):
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id=child_agent_id,
                status="unavailable",
                error="invalid_orchestration_result",
            )
            return "delegate deferred: agent_orchestration_unavailable"
        observation = result.observation.project(limits)
        if result.status.casefold() != "succeeded":
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id=child_agent_id,
                status=result.status,
                error=result.reason_code or "agent_orchestration_group_not_succeeded",
            )
            return self._orchestration_group_failed_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration_trace.iteration,
                observation=observation,
                result=result,
                llm_call_artifacts=llm_call_artifacts,
            )
        events.subagent_completed(
            iteration=iteration_trace.iteration,
            child_agent_id=child_agent_id,
            output_keys=sorted(observation),
            summary=f"group={observation['group_id']} status={result.status}",
        )
        return "subagent delegation observation: " + json.dumps(
            observation,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _handle_delegate_action(
        self,
        *,
        agent: AgentSpec,
        inputs: dict[str, Any],
        action: AgentAction,
        run_id: str | None,
        execution_identity: GraphExecutionIdentity | None,
        graph_checkpoint_ref: str | None,
        standalone: bool,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iteration_trace: IterationTrace,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> str | AgentLoopResult:
        child_agent_id = action.subagent_id or ""
        if not agent.allows_subagent(child_agent_id):
            verdict = JudgeVerdict(
                decision=JudgeDecision.BLOCK,
                confidence=1.0,
                feedback=f"subagent delegation is not allowed: {child_agent_id}",
                policy_violations=["subagent delegation not allowed"],
            )
            trace.record_judge(iteration_trace, verdict)
            events.judge_block(iteration=iteration_trace.iteration, verdict=verdict.to_dict())
            return self._blocked_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration_trace.iteration,
                verdict=verdict,
                stop_reason=AgentLoopStopReason.AGENT_POLICY_BLOCKED,
                via_tool=None,
                llm_call_artifacts=llm_call_artifacts,
            )

        handoff_reason = action.handoff_reason or "subagent delegation requested"
        subagent_task = action.subagent_task or handoff_reason
        metadata = {
            "parent_agent_id": agent.agent_id,
            "child_agent_id": child_agent_id,
            "handoff_reason": handoff_reason,
        }
        events.subagent_delegation_requested(
            iteration=iteration_trace.iteration,
            parent_agent_id=agent.agent_id,
            child_agent_id=child_agent_id,
            handoff_reason=handoff_reason,
            task=subagent_task,
        )
        if self._orchestration_enabled:
            return self._handle_legacy_delegate_orchestration(
                agent=agent,
                child_agent_id=child_agent_id,
                subagent_task=subagent_task,
                run_id=run_id,
                execution_identity=execution_identity,
                graph_checkpoint_ref=graph_checkpoint_ref,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iteration_trace=iteration_trace,
                llm_call_artifacts=llm_call_artifacts,
            )
        if self._subagent_executor is not None:
            try:
                subagent_result = self._subagent_executor.run(
                    SubAgentTask(
                        parent_agent_id=agent.agent_id,
                        child_agent_id=child_agent_id,
                        task=subagent_task,
                        inputs=_subagent_inputs_snapshot(inputs=inputs, action=action),
                        handoff_reason=handoff_reason,
                        metadata=metadata,
                        run_id=run_id,
                        execution_identity=execution_identity,
                        graph_checkpoint_ref=graph_checkpoint_ref,
                        standalone=standalone,
                        trace_carrier=_subagent_trace_carrier(
                            events.trace_context
                        ),
                    )
                )
            except Exception as exc:
                events.subagent_failed(
                    iteration=iteration_trace.iteration,
                    child_agent_id=child_agent_id,
                    status="failed",
                    error=str(exc),
                )
                verdict = JudgeVerdict(
                    decision=JudgeDecision.RETRY,
                    confidence=0.2,
                    feedback=f"subagent delegation failed: {exc}",
                    validation_errors=[f"subagent delegation failed: {child_agent_id}"],
                )
                trace.record_judge(iteration_trace, verdict)
                return verdict.feedback or "subagent delegation failed"
            result_payload = subagent_result.to_dict()
            if subagent_result.success:
                events.subagent_completed(
                    iteration=iteration_trace.iteration,
                    child_agent_id=child_agent_id,
                    output_keys=sorted(subagent_result.output.keys()),
                    summary=subagent_result.summary,
                )
                return _subagent_feedback(subagent_result)
            events.subagent_failed(
                iteration=iteration_trace.iteration,
                child_agent_id=child_agent_id,
                status=str(result_payload["status"]),
                error=subagent_result.error,
            )
            verdict = JudgeVerdict(
                decision=JudgeDecision.RETRY,
                confidence=0.2,
                feedback=_failed_subagent_feedback(result_payload),
                validation_errors=[f"subagent delegation failed: {result_payload}"],
            )
            trace.record_judge(iteration_trace, verdict)
            return verdict.feedback or "subagent delegation failed"
        verdict = JudgeVerdict(
            decision=JudgeDecision.ESCALATE,
            confidence=1.0,
            feedback="subagent delegation accepted by policy but orchestration is deferred",
            policy_violations=[],
            validation_errors=[f"delegation handoff: {metadata}"],
        )
        trace.record_judge(iteration_trace, verdict)
        return (
            "subagent delegation contract recorded; "
            f"parent_agent_id={agent.agent_id}; "
            f"child_agent_id={child_agent_id}; "
            f"handoff_reason={handoff_reason}"
        )

    def _handle_verdict(
        self,
        *,
        agent: AgentSpec,
        action: AgentAction,
        run_id: str | None,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        stall_detector: AgentLoopStallDetector,
        iteration_trace: IterationTrace,
        verdict: JudgeVerdict,
        judge_retries: int,
        via_tool: str | None,
        llm_call_artifacts: list[LLMCallArtifact],
        empty_output_retries: int,
    ) -> tuple[int, int, str] | AgentLoopResult:
        iteration = iteration_trace.iteration
        verdict_payload = verdict.to_dict()
        if verdict.decision == JudgeDecision.ACCEPT:
            metrics.judge_accepts += 1
            events.judge_accept(iteration=iteration, verdict=verdict_payload)
            if verdict.structured_output_contract is not None:
                metrics.structured_output_validation_accepts += 1
                events.structured_output_validation_accepted(
                    iteration=iteration,
                    verdict=verdict_payload,
                    repair_count=judge_retries,
                )
            return self._accepted_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration,
                output=action.output or {},
                run_id=run_id,
                verdict=verdict,
                stop_reason=(
                    AgentLoopStopReason.CONTROL_OUTPUT_ACCEPTED
                    if via_tool
                    else AgentLoopStopReason.FINAL_OUTPUT_ACCEPTED
                ),
                via_tool=via_tool,
                llm_call_artifacts=llm_call_artifacts,
            )

        if verdict.decision == JudgeDecision.BLOCK:
            metrics.judge_blocks += 1
            events.judge_block(iteration=iteration, verdict=verdict_payload, via_tool=via_tool)
            return self._blocked_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration,
                verdict=verdict,
                stop_reason=_blocked_stop_reason(verdict),
                via_tool=via_tool,
                llm_call_artifacts=llm_call_artifacts,
            )

        next_judge_retries = judge_retries + 1
        next_empty_output_retries = (
            empty_output_retries + 1 if not (action.output or {}) else 0
        )
        metrics.judge_retries += 1
        feedback = verdict.feedback or "output rejected by judge"
        events.judge_retry(
            iteration=iteration,
            feedback=feedback,
            verdict=verdict_payload,
            via_tool=via_tool,
        )
        detection = stall_detector.after_judge_retry(
            trace=trace,
            iteration=iteration,
            judge_retries=next_judge_retries,
            empty_output_retries=next_empty_output_retries,
        )
        if detection.stalled:
            stop_reason = detection.stop_reason or AgentLoopStopReason.JUDGE_RETRY_EXHAUSTED
            trace.mark_stop_candidate(iteration_trace, stop_reason.value)
            return self._retry_exhausted_result(
                agent=agent,
                metrics=metrics,
                events=events,
                trace=trace,
                diagnostics=diagnostics,
                iterations=iteration,
                verdict=verdict,
                stop_reason=stop_reason,
                detection=detection,
                llm_call_artifacts=llm_call_artifacts,
            )
        if verdict.structured_output_diagnostics:
            metrics.structured_output_repairs += 1
            events.structured_output_repair_requested(
                iteration=iteration,
                verdict=verdict_payload,
                repair_attempt=next_judge_retries,
                max_repairs=agent.loop_policy.max_judge_retries,
            )
        return next_judge_retries, next_empty_output_retries, feedback

    def _execute_tool(self, tool_call: ToolCall, tool_policy: ToolPolicy) -> ToolObservation:
        return self._tool_executor.execute(tool_call, tool_policy)

    def _complete_llm_request(
        self,
        request: LLMRequest,
        *,
        agent: AgentSpec,
        iteration: int,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
    ) -> LLMResponse:
        if not agent.loop_policy.llm_streaming_enabled:
            return _bind_llm_response_identity(
                LLMResponse.from_any(self._llm_client.complete(request)),
                request.execution_identity,
            )
        stream = getattr(self._llm_client, "stream", None)
        if not callable(stream):
            return _bind_llm_response_identity(
                LLMResponse.from_any(self._llm_client.complete(request)),
                request.execution_identity,
            )

        accumulator = LLMStreamAccumulator(
            expected_execution_identity=request.execution_identity,
            require_expected_identity=request.execution_identity is not None,
        )
        stream_event_count = 0
        raw_stream = stream(request)
        stream_events = raw_stream if isinstance(raw_stream, Iterable) else []
        for stream_event_count, stream_event in enumerate(stream_events, start=1):
            normalized_event = LLMStreamEvent.from_any(stream_event)
            accumulator.add_event(normalized_event)
            metrics.llm_stream_event_count += 1
            events.llm_stream_event(
                iteration=iteration,
                stream_event=normalized_event.to_dict(),
                sequence=stream_event_count,
            )
        response = accumulator.to_response()
        metadata = dict(response.metadata)
        metadata["llm_streamed"] = True
        metadata["llm_stream_event_count"] = stream_event_count
        return LLMResponse(
            content=response.content,
            usage=response.usage,
            metadata=metadata,
            execution_identity=request.execution_identity,
            structured_output=response.structured_output,
            tool_calls=list(response.tool_calls),
        )

    def _reserve_global_budget_before_llm_call(
        self,
        metrics: AgentLoopMetrics,
        *,
        request: LLMRequest,
        agent: AgentSpec,
        iteration: int,
    ) -> None:
        if self._global_budget_tracker is None:
            return
        if _client_manages_global_budget(self._llm_client):
            return
        tracker = self._global_budget_tracker
        if request.execution_identity is not None:
            tracker = tracker.for_execution_identity(request.execution_identity)
        self._active_budget_tracker = tracker
        operation_id = tracker.next_operation_identity(
            f"agent:{agent.agent_id}:iteration:{iteration}"
        )
        operation = tracker.reserve_direct_operation(
            operation_id=operation_id,
            idempotency_key=f"{operation_id}:reservation",
            input_tokens=estimate_request_tokens(request),
            output_tokens=(
                max(0, int(request.max_tokens))
                if request.max_tokens is not None
                else None
            ),
        )
        if isinstance(operation, GlobalBudgetCheck):
            raise GlobalBudgetExceededError(operation)
        self._active_budget_operation = operation
        check = tracker.check_for_operation(operation)
        metrics.global_budget_check = check.to_dict()
        metrics.global_budget_usage = check.usage.to_dict()

    def _record_global_budget_call(
        self,
        metrics: AgentLoopMetrics,
        response,
    ) -> None:
        metadata = dict(response.metadata)
        router_budget_check = metadata.get("llm_global_budget_check")
        router_budget_usage = metadata.get("llm_global_budget_usage")
        if isinstance(router_budget_check, dict):
            self._active_budget_operation = None
            self._active_budget_tracker = None
            metrics.global_budget_check = dict(router_budget_check)
            metrics.global_budget_usage = (
                dict(router_budget_usage) if isinstance(router_budget_usage, dict) else None
            )
            return
        tracker = self._active_budget_tracker
        if tracker is None:
            return
        operation = self._active_budget_operation
        self._active_budget_operation = None
        if operation is None:
            return
        check = tracker.settle_operation(
            operation,
            response.usage,
            estimated_cost_usd=_optional_float(metadata.get("llm_estimated_cost_usd")),
        )
        metrics.global_budget_check = check.to_dict()
        metrics.global_budget_usage = check.usage.to_dict()

    def _mark_active_budget_indeterminate(
        self,
        metrics: AgentLoopMetrics,
        *,
        reason: str,
    ) -> None:
        operation = self._active_budget_operation
        self._active_budget_operation = None
        tracker = self._active_budget_tracker
        if tracker is None or operation is None:
            return
        check = tracker.mark_operation_indeterminate(
            operation,
            reason=reason,
        )
        metrics.global_budget_check = check.to_dict()
        metrics.global_budget_usage = check.usage.to_dict()

    def _memory_context_for_llm(
        self,
        *,
        agent: AgentSpec,
        inputs: dict[str, Any],
        execution_identity: GraphExecutionIdentity | None,
    ) -> str | None:
        if not agent.memory_enabled or not agent.loop_policy.memory_recall_enabled:
            return None
        if self._memory_recall is not None:
            recall = self._memory_adapter.before_llm_call(
                agent_id=agent.agent_id,
                input_text=str(inputs),
                memory_recall=self._memory_recall,
                execution_identity=execution_identity,
                policy=self._memory_policy,
            )
            return recall.context_block.content or None
        if self._memory_runtime is None:
            return None
        try:
            recall = self._memory_adapter.before_llm_call(
                agent_id=agent.agent_id,
                input_text=str(inputs),
                runtime=self._memory_runtime,
                execution_identity=execution_identity,
                policy=self._memory_policy,
            )
        except Exception:
            return None
        return recall.context_block.content or None

    def _skill_prompt_section(
        self,
        *,
        agent: AgentSpec,
        inputs: dict[str, Any],
    ) -> str | None:
        if self._skill_runtime is None:
            return None
        task = _skill_selection_task(agent=agent, inputs=inputs)
        return self._skill_runtime.build_prompt_section(
            task,
            context={"agent_id": agent.agent_id, "inputs": inputs},
        )

    def _normalize_output(
        self,
        *,
        agent: AgentSpec,
        output: dict[str, Any],
        inputs: dict[str, Any],
    ) -> dict[str, Any]:
        return self._output_normalizer(agent=agent, output=output, inputs=inputs)

    def _output_budget_verdict(
        self,
        *,
        agent: AgentSpec,
        output: dict[str, Any],
    ) -> JudgeVerdict | None:
        budget = resolve_agent_output_budget(agent.validation_policy)
        if budget is None:
            return None
        check = validate_agent_output_budget(output, budget=budget)
        if not check.has_violations:
            return None
        return output_budget_judge_verdict(check)

    def _record_tool_observation_memory_candidate(
        self,
        *,
        agent: AgentSpec,
        observation: ToolObservation,
        trace: AgentLoopTrace,
    ) -> None:
        if not agent.memory_enabled or not agent.loop_policy.memory_write_enabled:
            return
        if observation.status != ToolStatus.SUCCEEDED:
            return
        try:
            candidate = self._memory_adapter.propose_tool_observation(
                agent_id=agent.agent_id,
                tool_name=observation.tool_name,
                observation=observation.to_dict(),
                execution_identity=trace.execution_identity,
            )
        except Exception:
            return
        if candidate is not None:
            trace.record_memory_candidate(candidate.to_dict())

    def _record_final_output_memory_candidate(
        self,
        *,
        agent: AgentSpec,
        output: dict[str, Any],
        trace: AgentLoopTrace,
    ) -> None:
        if not agent.memory_enabled or not agent.loop_policy.memory_write_enabled:
            return
        try:
            candidate = self._memory_adapter.propose_final_output(
                agent_id=agent.agent_id,
                output=output,
                execution_identity=trace.execution_identity,
            )
        except Exception:
            return
        if candidate is not None:
            trace.record_memory_candidate(candidate.to_dict())

    def _accepted_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        output: dict[str, Any],
        run_id: str | None,
        verdict: JudgeVerdict,
        stop_reason: AgentLoopStopReason,
        via_tool: str | None,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        self._record_final_output_memory_candidate(
            agent=agent,
            output=output,
            trace=trace,
        )
        events.final_output(iteration=iterations, output_keys=sorted(output.keys()), via_tool=via_tool)
        result_diagnostics = diagnostics.accepted(
            metrics=metrics,
            iterations=iterations,
            stop_reason=stop_reason,
            verdict=verdict,
        )
        events.completed(
            iteration=iterations,
            status=AgentLoopStatus.ACCEPTED.value,
            stop_reason=stop_reason.value,
        )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=stop_reason,
            success=True,
            status=AgentLoopStatus.ACCEPTED,
            output=output,
            verdict=verdict,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
        )

    def _blocked_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        verdict: JudgeVerdict,
        stop_reason: AgentLoopStopReason,
        via_tool: str | None,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        result_diagnostics = diagnostics.blocked(
            metrics=metrics,
            iterations=iterations,
            stop_reason=stop_reason,
            verdict=verdict,
        )
        events.blocked(
            iteration=iterations,
            stop_reason=stop_reason.value,
            verdict=verdict.to_dict(),
        )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=stop_reason,
            success=False,
            status=AgentLoopStatus.BLOCKED,
            verdict=verdict,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=verdict.feedback,
        )

    def _global_budget_exceeded_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        exc: Exception,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        budget_check = _budget_check_from_exception(exc)
        if budget_check is not None:
            metrics.global_budget_check = budget_check
            usage = budget_check.get("usage")
            metrics.global_budget_usage = dict(usage) if isinstance(usage, dict) else None
        verdict = JudgeVerdict(
            decision=JudgeDecision.BLOCK,
            confidence=1.0,
            feedback=str(exc),
            policy_violations=["global budget exceeded"],
        )
        result_diagnostics = diagnostics.blocked(
            metrics=metrics,
            iterations=iterations,
            stop_reason=AgentLoopStopReason.GLOBAL_BUDGET_EXCEEDED,
            verdict=verdict,
        )
        events.blocked(
            iteration=iterations,
            stop_reason=AgentLoopStopReason.GLOBAL_BUDGET_EXCEEDED.value,
            verdict=verdict.to_dict(),
        )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=AgentLoopStopReason.GLOBAL_BUDGET_EXCEEDED,
            success=False,
            status=AgentLoopStatus.BLOCKED,
            verdict=verdict,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=str(exc),
        )

    def _waiting_for_approval_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        tool_name: str,
        approval_id: str | None,
        llm_call_artifacts: list[LLMCallArtifact],
        approval_kind: str = "tool_approval",
        control_action: str | None = None,
        escalation_type: str | None = None,
    ) -> AgentLoopResult:
        result_diagnostics = diagnostics.waiting_for_approval(
            metrics=metrics,
            iterations=iterations,
            tool_name=tool_name,
            approval_id=approval_id,
            approval_kind=approval_kind,
            control_action=control_action,
            escalation_type=escalation_type,
        )
        events.waiting_for_approval(
            iteration=iterations,
            stop_reason=AgentLoopStopReason.TOOL_APPROVAL_REQUIRED.value,
            approval_id=approval_id,
            approval_kind=approval_kind,
            tool_name=tool_name,
            control_action=control_action,
            escalation_type=escalation_type,
        )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=AgentLoopStopReason.TOOL_APPROVAL_REQUIRED,
            success=False,
            status=AgentLoopStatus.WAITING_FOR_APPROVAL,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=result_diagnostics.summary,
        )

    def _retry_exhausted_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        verdict: JudgeVerdict | None,
        stop_reason: AgentLoopStopReason,
        detection: StallDetection | None = None,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        result_diagnostics = diagnostics.retry_exhausted(
            metrics=metrics,
            iterations=iterations,
            stop_reason=stop_reason,
            verdict=verdict,
            issue=detection.issue if detection else None,
        )
        events.retry_exhausted(
            iteration=iterations,
            stop_reason=stop_reason.value,
            verdict=verdict.to_dict() if verdict else None,
        )
        if verdict is not None and verdict.structured_output_diagnostics:
            metrics.structured_output_repair_budget_exhausted += 1
            events.structured_output_repair_budget_exhausted(
                iteration=iterations,
                verdict=verdict.to_dict(),
                stop_reason=stop_reason.value,
            )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=stop_reason,
            success=False,
            status=AgentLoopStatus.RETRY_EXHAUSTED,
            verdict=verdict,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=result_diagnostics.summary,
        )

    def _stalled_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        verdict: JudgeVerdict | None,
        detection: StallDetection,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        result_diagnostics = diagnostics.stalled(
            metrics=metrics,
            iterations=iterations,
            detection=detection,
            verdict=verdict,
        )
        events.stalled(
            iteration=iterations,
            stop_reason=result_diagnostics.stop_reason.value,
            summary=result_diagnostics.summary,
        )
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=result_diagnostics.stop_reason,
            success=False,
            status=AgentLoopStatus.STALLED,
            verdict=verdict,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=result_diagnostics.summary,
        )

    def _failed_result(
        self,
        *,
        agent: AgentSpec,
        metrics: AgentLoopMetrics,
        events: AgentLoopEventRecorder,
        trace: AgentLoopTrace,
        diagnostics: AgentLoopDiagnosticsBuilder,
        iterations: int,
        stop_reason: AgentLoopStopReason,
        error: str,
        llm_call_artifacts: list[LLMCallArtifact],
    ) -> AgentLoopResult:
        result_diagnostics = diagnostics.failed(
            metrics=metrics,
            iterations=iterations,
            stop_reason=stop_reason,
            error=error,
        )
        events.failed(iteration=iterations, stop_reason=stop_reason.value, error=error)
        return _agent_loop_result(
            loop_trace=trace,
            stop_reason=stop_reason,
            success=False,
            status=AgentLoopStatus.FAILED,
            iterations=iterations,
            metrics=metrics,
            events=events.to_dicts(),
            trace=_trace_payload(trace, agent),
            diagnostics=result_diagnostics,
            llm_call_artifacts=list(llm_call_artifacts),
            error=error,
        )


def _trace_payload(trace: AgentLoopTrace, agent: AgentSpec) -> dict[str, Any]:
    trace.finish_open_iterations()
    if not agent.loop_policy.trace_enabled:
        return {"agent_id": agent.agent_id, "summary": trace.summary()}
    return trace.to_dict()


def _agent_loop_result(
    *,
    loop_trace: AgentLoopTrace,
    stop_reason: AgentLoopStopReason,
    **kwargs: Any,
) -> AgentLoopResult:
    loop_trace.finish_open_iterations()
    trajectory = [item.to_dict() for item in loop_trace.trajectory()]
    return AgentLoopResult(
        **kwargs,
        trajectory=trajectory,
        tool_calls=[item.to_dict() for item in loop_trace.tool_calls],
        memory_ops=[],
        memory_candidates=[
            dict(item) for item in loop_trace.memory_candidates
        ],
        termination_reason=stop_reason.value,
        max_steps_reached=stop_reason
        in {
            AgentLoopStopReason.MAX_ITERATIONS,
            AgentLoopStopReason.MAX_ITERATIONS_EXCEEDED,
        },
        trace_id=loop_trace.trace_id,
        trace_ref="agent_loop_trace",
    )


def _prompt_safe_observation(
    observation: ToolObservation,
    policy: ToolPolicy,
) -> ToolObservation:
    if observation.status != ToolStatus.SUCCEEDED:
        return observation
    result = observation.result
    output = result.output
    if output is None:
        return observation
    output_bytes = result.output_bytes
    if output_bytes is None:
        output_bytes = len(str(output).encode("utf-8"))
    if output_bytes <= policy.max_result_chars_inline:
        return observation

    artifact_refs = list(result.artifact_refs)
    if not artifact_refs:
        artifact_refs.append(
            ArtifactRef(
                artifact_id=f"tool_result:{observation.call.call_id}",
                relative_path=f"tool_results/{observation.call.call_id}.json",
                size_bytes=output_bytes,
            )
        )
    pointer_output = {
        "artifact_ref": artifact_refs[0].to_dict(),
        "summary": _large_result_summary(output),
        "count": _large_result_count(output),
        "sample": _large_result_sample(output),
    }
    pointer_result = replace(
        result,
        output=pointer_output,
        output_summary=(
            result.output_summary
            or f"Tool result stored as artifact pointer: {artifact_refs[0].relative_path}"
        ),
        artifact_refs=artifact_refs,
        output_bytes=output_bytes,
        redacted=True,
    )
    return ToolObservation(
        call=observation.call,
        result=pointer_result,
        elapsed_ms=observation.elapsed_ms,
    )


def _execution_tool_policy(policy: ToolPolicy) -> ToolPolicy:
    if not policy.spill_large_results_to_artifact:
        return policy
    return replace(policy, spill_large_results_to_artifact=False)


def _large_result_summary(value: Any) -> str:
    if isinstance(value, list):
        return f"large list result with {len(value)} item(s)"
    if isinstance(value, dict):
        return f"large object result with {len(value)} top-level field(s)"
    return "large scalar tool result"


def _large_result_count(value: Any) -> int | None:
    if isinstance(value, (list, dict, str)):
        return len(value)
    return None


def _large_result_sample(value: Any) -> Any:
    if isinstance(value, list):
        return value[:3]
    if isinstance(value, dict):
        return {
            key: value[key]
            for key in list(value)[:3]
        }
    text = str(value)
    return text[:500]


def _llm_call_artifact(
    *,
    agent: AgentSpec,
    iteration: int,
    request: LLMRequest,
    response: LLMResponse,
) -> LLMCallArtifact:
    metadata = dict(response.metadata)
    return LLMCallArtifact(
        artifact_id=f"{agent.agent_id}:llm_call:{iteration}",
        iteration=iteration,
        request=request.to_dict(redact=True),
        response=response.to_dict(redact=True),
        metadata={
            "agent_id": agent.agent_id,
            "provider": metadata.get("provider") or metadata.get("llm_provider"),
            "model": metadata.get("model") or metadata.get("llm_model"),
            "route_id": metadata.get("llm_route_id"),
            "deployment_id": metadata.get("llm_deployment_id"),
            "fallback_used": metadata.get("llm_fallback_used"),
            "streamed": metadata.get("llm_streamed"),
        },
    )


def _bind_llm_response_identity(
    response: LLMResponse,
    execution_identity: GraphExecutionIdentity | None,
) -> LLMResponse:
    if execution_identity is None:
        return response
    if (
        response.execution_identity != execution_identity
    ):
        raise ValueError("LLM response Graph identity conflicts with its request")
    return response


def _tool_names(tools: list[dict[str, Any]]) -> list[str]:
    names = []
    for tool in tools:
        name = tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return sorted(names)


def _skill_selection_task(*, agent: AgentSpec, inputs: dict[str, Any]) -> str:
    return "\n".join(
        [
            agent.goal or "",
            agent.instructions or "",
            str(inputs),
        ]
    )


def _max_tool_calls_per_agent(policy: ToolPolicy) -> int:
    return max(0, int(policy.max_tool_calls_per_agent))


def _blocked_tool_budget_observation(
    tool_call: ToolCall,
    policy: ToolPolicy,
) -> ToolObservation:
    return ToolObservation(
        call=tool_call,
        result=ToolResult(
            status=ToolStatus.BLOCKED,
            error_type="ToolPermissionError",
            error_message=(
                "agent exceeded max_tool_calls_per_agent "
                f"of {_max_tool_calls_per_agent(policy)}"
            ),
        ),
        elapsed_ms=0.0,
    )


def _is_control_set_output(observation: ToolObservation) -> bool:
    output = observation.result.output
    return (
        observation.call.tool_name == "control.set_output"
        and isinstance(output, dict)
        and output.get("control_action") == "set_output"
        and isinstance(output.get("output"), dict)
    )


def _control_output(observation: ToolObservation) -> dict[str, Any]:
    output = observation.result.output
    if isinstance(output, dict) and isinstance(output.get("output"), dict):
        return dict(output["output"])
    return {}


def _is_control_approval_request(observation: ToolObservation) -> bool:
    output = observation.result.output
    if not isinstance(output, dict):
        return False
    return (
        observation.call.tool_name in {"control.request_human_review", "control.escalate"}
        and output.get("control_action") in {"request_human_review", "escalate"}
        and isinstance(output.get("approval_id"), str)
        and bool(str(output.get("approval_id")).strip())
    )


def _control_approval_metadata(observation: ToolObservation) -> dict[str, Any]:
    output = observation.result.output if isinstance(observation.result.output, dict) else {}
    control_action = str(output.get("control_action") or "")
    payload: dict[str, Any] = {
        "approval_id": str(output["approval_id"]),
        "control_action": control_action,
        "approval_kind": (
            "escalation" if control_action == "escalate" else "human_review"
        ),
    }
    escalation_type = output.get("escalation_type")
    if isinstance(escalation_type, str) and escalation_type:
        payload["escalation_type"] = escalation_type
    return payload


def _subagent_trace_carrier(context: TraceContext | None) -> dict[str, str]:
    if context is None:
        return {}
    return W3CTracePropagator().inject(context)


def _subagent_inputs_snapshot(
    *,
    inputs: dict[str, Any],
    action: AgentAction,
) -> dict[str, Any]:
    return {
        "parent_inputs": dict(inputs),
        "subagent_task": action.subagent_task,
        "handoff_reason": action.handoff_reason,
    }


def _subagent_feedback(result: SubAgentResult) -> str:
    payload = result.to_dict()
    return (
        "subagent delegation completed; "
        f"child_agent_id={result.child_agent_id}; "
        f"status={payload['status']}; "
        f"summary={result.summary or ''}; "
        f"output={payload['output']}"
    )


def _failed_subagent_feedback(result_payload: dict[str, Any]) -> str:
    details: list[str] = [
        "subagent delegation returned non-success status: "
        f"{result_payload.get('status')}"
    ]
    for key in ("error", "summary"):
        value = result_payload.get(key)
        if value:
            details.append(f"{key}={value}")
    metadata = result_payload.get("metadata")
    if isinstance(metadata, dict):
        stop_reason = metadata.get("stop_reason")
        if stop_reason:
            details.append(f"stop_reason={stop_reason}")
    child_agent_id = result_payload.get("child_agent_id")
    if child_agent_id:
        details.append(f"child_agent_id={child_agent_id}")
    return "; ".join(details)


def _blocked_stop_reason(verdict: JudgeVerdict) -> AgentLoopStopReason:
    if any("subagent delegation" in violation for violation in verdict.policy_violations):
        return AgentLoopStopReason.AGENT_POLICY_BLOCKED
    if any("secret" in violation for violation in verdict.policy_violations):
        return AgentLoopStopReason.SECRET_BLOCKED
    return AgentLoopStopReason.JUDGE_BLOCKED


def _is_global_budget_exception(exc: Exception) -> bool:
    if getattr(exc, "error_type", None) == "global_budget_exceeded":
        return True
    if exc.__class__.__name__ == "GlobalBudgetExceededError":
        return True
    to_dict = getattr(exc, "to_dict", None)
    if callable(to_dict):
        try:
            payload = to_dict()
        except Exception:
            return False
        return isinstance(payload, dict) and payload.get("error_type") == "global_budget_exceeded"
    return False


def _client_manages_global_budget(client: Any) -> bool:
    capability = getattr(client, "manages_global_budget", None)
    return bool(capability() if callable(capability) else capability)


def _structured_output_failure_verdict(
    exc: Exception,
    *,
    request: LLMRequest,
) -> JudgeVerdict | None:
    if request.output_schema is None:
        return None
    error_payload: dict[str, Any] | None = None
    error_type = str(getattr(exc, "error_type", "") or "")
    if error_type in {
        "structured_output_parse_error",
        "structured_output_validation_error",
        "structured_output_typed_validation_error",
    }:
        error_payload = {
            "error_type": error_type,
            "diagnostics": list(getattr(exc, "diagnostics", ()) or ()),
            "response_fingerprint": getattr(exc, "response_fingerprint", None),
        }
    else:
        for item in reversed(tuple(getattr(exc, "errors", ()) or ())):
            if not isinstance(item, dict):
                continue
            nested_type = str(item.get("error_type") or "")
            if nested_type in {
                "structured_output_parse_error",
                "structured_output_validation_error",
                "structured_output_typed_validation_error",
            }:
                error_payload = dict(item)
                break
    if error_payload is None:
        return None
    try:
        contract = compile_structured_output_contract(
            request.structured_output_schema_source(),
            schema_name=request.output_schema_name,
        )
    except (TypeError, ValueError):
        return None

    raw_diagnostics = error_payload.get("diagnostics")
    diagnostics: list[dict[str, Any]] = []
    if isinstance(raw_diagnostics, (list, tuple)):
        for item in raw_diagnostics[: contract.limits.max_diagnostics]:
            if not isinstance(item, dict):
                continue
            code = str(
                item.get("code")
                or error_payload.get("error_type")
                or "structured_output_validation_error"
            )
            instance_path = [
                value
                for value in item.get("instance_path", [])
                if isinstance(value, (str, int)) and not isinstance(value, bool)
            ]
            schema_path = [
                value
                for value in item.get("schema_path", [])
                if isinstance(value, (str, int)) and not isinstance(value, bool)
            ]
            diagnostics.append(
                {
                    "code": code,
                    "message": f"{code} at {_json_pointer(instance_path)}",
                    "instance_path": instance_path,
                    "schema_path": schema_path,
                    "validator": (
                        str(item["validator"])[:96]
                        if item.get("validator") is not None
                        else None
                    ),
                    "contract_digest": contract.schema_digest,
                }
            )
    if not diagnostics:
        code = str(
            error_payload.get("error_type")
            or "structured_output_validation_error"
        )
        diagnostics.append(
            {
                "code": code,
                "message": f"{code} at $",
                "instance_path": [],
                "schema_path": [],
                "validator": None,
                "contract_digest": contract.schema_digest,
            }
        )
    fingerprint = error_payload.get("response_fingerprint")
    return JudgeVerdict(
        decision=JudgeDecision.RETRY,
        confidence=0.0,
        feedback="structured output rejected: "
        + "; ".join(item["message"] for item in diagnostics),
        schema_errors=[item["message"] for item in diagnostics],
        structured_output_diagnostics=diagnostics,
        structured_output_contract=contract.to_dict(),
        response_fingerprint=(fingerprint if isinstance(fingerprint, str) else None),
    )


class _ManagedStructuredOutputAttemptError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("structured output failed managed validation")


def _json_pointer(path: list[str | int]) -> str:
    if not path:
        return "$"
    encoded = "/".join(
        str(item).replace("~", "~0").replace("/", "~1") for item in path
    )
    return f"$/{encoded}"


def _budget_check_from_exception(exc: Exception) -> dict[str, Any] | None:
    check = getattr(exc, "check", None)
    if check is not None and hasattr(check, "to_dict"):
        payload = check.to_dict()
        return dict(payload) if isinstance(payload, dict) else None
    manifest = getattr(exc, "manifest", None)
    if isinstance(manifest, dict):
        check_payload = manifest.get("global_budget_check")
        if isinstance(check_payload, dict):
            return dict(check_payload)
    return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _orchestration_policy(agent: AgentSpec) -> tuple[str, int, ParentObservationLimits]:
    raw_policy = agent.metadata.get("agent_orchestration", {})
    if not isinstance(raw_policy, Mapping):
        raise ValueError("agent_orchestration metadata must be an object")
    policy_ref = raw_policy.get("policy_ref", "agent-orchestration@1")
    max_tasks = raw_policy.get("max_tasks_per_group", 8)
    limit_values = ParentObservationLimits().to_dict()
    limit_values.update(
        {
            key: value
            for key, value in raw_policy.items()
            if key not in {"policy_ref", "max_tasks_per_group", "legacy_delegate_capabilities"}
        }
    )
    limits = ParentObservationLimits.from_dict(limit_values)
    return str(policy_ref), max_tasks, limits


def _orchestration_parent_turn_id(
    agent_id: str,
    identity: GraphExecutionIdentity | None,
    iteration: int,
) -> str:
    return "parent-turn:" + checksum_for({
        "agent_id": agent_id,
        "run_id": identity.run_id if identity is not None else None,
        "activity_id": identity.activity_id if identity is not None else None,
        "iteration": iteration,
    }).removeprefix("sha256:")


def _legacy_delegate_mapping(
    agent: AgentSpec,
    child_agent_id: str,
) -> tuple[str, tuple[str, ...], str]:
    raw_policy = agent.metadata.get("agent_orchestration", {})
    if not isinstance(raw_policy, Mapping):
        raise ValueError("agent_orchestration metadata must be an object")
    raw_mappings = raw_policy.get("legacy_delegate_capabilities")
    if not isinstance(raw_mappings, Mapping):
        raise ValueError("legacy_delegate_capabilities is not configured")
    raw_mapping = raw_mappings.get(child_agent_id)
    if not isinstance(raw_mapping, Mapping):
        raise ValueError(f"legacy child {child_agent_id!r} has no pinned capability")
    expected = {"capability_hint", "input_refs", "output_role"}
    if set(raw_mapping) != expected:
        raise ValueError("legacy delegate mapping contains unsupported fields")
    capability_hint = raw_mapping["capability_hint"]
    output_role = raw_mapping["output_role"]
    input_refs = raw_mapping["input_refs"]
    if not isinstance(input_refs, (list, tuple)):
        raise ValueError("legacy delegate input_refs must be an array")
    return str(capability_hint), tuple(input_refs), str(output_role)


def _legacy_orchestration_unavailable_feedback(
    *,
    child_agent_id: str,
    events: AgentLoopEventRecorder,
    iteration: int,
    trace: AgentLoopTrace,
    iteration_trace: IterationTrace,
) -> str:
    events.subagent_failed(
        iteration=iteration,
        child_agent_id=child_agent_id,
        status="unavailable",
        error="agent_orchestration_unavailable",
    )
    trace.record_judge(
        iteration_trace,
        JudgeVerdict(
            decision=JudgeDecision.ESCALATE,
            confidence=1.0,
            feedback="legacy delegate orchestration is unavailable",
            validation_errors=["agent_orchestration_unavailable"],
        ),
    )
    return "delegate deferred: agent_orchestration_unavailable"


def _action_type_value(action_type: Any) -> str:
    return action_type.value if hasattr(action_type, "value") else str(action_type)


def _parsed_action_type(action: AgentAction | SkillCall) -> str:
    if isinstance(action, SkillCall):
        return action.type
    return _action_type_value(action.action_type)


def _parsed_tool_name(action: AgentAction | SkillCall) -> str | None:
    if isinstance(action, SkillCall):
        return action.skill_name
    return action.tool_name


def _parsed_output(action: AgentAction | SkillCall) -> dict[str, Any] | None:
    if isinstance(action, SkillCall):
        return {"skill_name": action.skill_name}
    return action.output


def _run_id_from_inputs(inputs: dict[str, Any]) -> str | None:
    value = inputs.get("run_id")
    if value:
        return str(value)
    request = inputs.get("request")
    if isinstance(request, dict) and request.get("run_id"):
        return str(request.get("run_id"))
    return None
