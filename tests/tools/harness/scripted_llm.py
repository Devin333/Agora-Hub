"""Drive ``AgentLoop`` with a scripted model so a turn issues real tool calls."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from framework.agent.loop import AgentLoop
from framework.agent.models import AgentLoopPolicy, AgentLoopResult, AgentSpec
from framework.llm import FakeLLMClient, LLMResponse
from framework.llm.models import LLMToolCall
from framework.tool import ToolExecutor, ToolPolicy, ToolRegistry


def tool_turn(*calls: tuple[str, str, dict[str, Any]]) -> LLMResponse:
    """One model turn that requests every ``(call_id, tool_name, arguments)``."""

    return LLMResponse(
        content="",
        tool_calls=[
            LLMToolCall(tool_call_id=call_id, tool_name=name, arguments=dict(arguments))
            for call_id, name, arguments in calls
        ],
    )


def final_turn(summary: str) -> str:
    return json.dumps({"action_type": "final_output", "output": {"output": {"summary": summary}}})


def tool_agent(
    tool_names: Sequence[str],
    *,
    allow_parallel: bool = True,
    max_iterations: int = 4,
) -> AgentSpec:
    names = list(tool_names)
    return AgentSpec(
        agent_id="tool-stability-agent",
        name="Tool Stability Agent",
        instructions="Call the requested tools, then report what came back.",
        allowed_tools=names,
        loop_policy=AgentLoopPolicy(
            max_iterations=max_iterations,
            allow_parallel_tool_calls=allow_parallel,
        ),
        tool_policy=ToolPolicy(
            allowed_tools=names,
            require_explicit_allowlist=True,
            allow_dangerous_tools=True,
            require_approval_for_side_effects=False,
            max_tool_calls_per_iteration=max(len(names), 1),
            max_tool_calls_per_agent=max(len(names), 1) * max_iterations,
        ),
    )


def run_agent(
    registry: ToolRegistry,
    agent: AgentSpec,
    responses: Sequence[LLMResponse | str],
    *,
    run_id: str = "run-tool-stability-agent",
) -> tuple[AgentLoopResult, FakeLLMClient]:
    llm = FakeLLMClient(list(responses))
    result = AgentLoop(llm_client=llm, tool_executor=ToolExecutor(registry)).run(
        agent,
        {"task": "exercise tools"},
        registry.export_schema_for_llm(agent.agent_id, agent.resolved_tool_policy()),
        run_id=run_id,
        standalone=True,
    )
    return result, llm


def observations_in_prompt(llm: FakeLLMClient, request_index: int) -> list[dict[str, Any]]:
    """Tool observations the model was shown in its ``request_index``-th request."""

    user = llm.requests[request_index].messages[-1]["content"]
    marker = "\nTool observations: "
    if marker not in user:
        return []
    payload = user.split(marker, 1)[1].split("\nJudge feedback: ", 1)[0]
    return json.loads(payload)
