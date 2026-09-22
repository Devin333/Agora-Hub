from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any

from framework.agent.models import AgentLoopPolicy, AgentSpec
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.models import SubAgentInvocation
from framework.tool import ToolPolicy


def project_child_agent_spec(
    parent: AgentSpec,
    invocation: SubAgentInvocation,
) -> AgentSpec:
    """Project one admitted SubAgent invocation into a stricter AgentSpec.

    This projection only narrows deterministic AgentLoop and tool-policy
    permissions. The invocation's token, time, and cost reservations remain
    runtime enforcement concerns and are deliberately not represented here.
    """

    if not isinstance(parent, AgentSpec):
        raise TypeError("parent must be AgentSpec")
    if not isinstance(invocation, SubAgentInvocation):
        raise TypeError("invocation must be SubAgentInvocation")
    if not isinstance(parent.loop_policy, AgentLoopPolicy):
        raise HarnessValidationError(
            "AgentSpec loop policy must be typed",
            code="subagent_agent_policy_invalid",
        )

    admitted = invocation.subagent_spec
    _required_schema(admitted.input_schema, "input_schema")
    output_schema = _required_schema(admitted.output_schema, "output_schema")
    if parent.agent_id != admitted.subagent_id:
        raise HarnessValidationError(
            "AgentSpec identity differs from the admitted SubAgentSpec",
            code="subagent_agent_policy_identity_mismatch",
        )
    if parent.output_schema != output_schema:
        raise HarnessValidationError(
            "AgentSpec output schema differs from the admitted SubAgentSpec",
            code="subagent_agent_policy_schema_mismatch",
        )

    admitted_turns = _required_budget(
        admitted.budget,
        "max_turns",
        minimum=1,
    )
    admitted_tool_calls = _required_budget(
        admitted.budget,
        "max_tool_calls",
        minimum=0,
    )
    parent_iterations = _bounded_integer(
        parent.max_iterations,
        "AgentSpec.max_iterations",
        minimum=1,
    )
    loop_iterations = _bounded_integer(
        parent.loop_policy.max_iterations,
        "AgentLoopPolicy.max_iterations",
        minimum=1,
    )
    loop_tool_calls = _bounded_integer(
        parent.loop_policy.max_tool_calls,
        "AgentLoopPolicy.max_tool_calls",
        minimum=0,
    )

    parent_tool_policy = parent.resolved_tool_policy()
    if not isinstance(parent_tool_policy, ToolPolicy):
        raise HarnessValidationError(
            "AgentSpec tool policy must be typed",
            code="subagent_agent_policy_invalid",
        )
    per_agent_tool_calls = _bounded_integer(
        parent_tool_policy.max_tool_calls_per_agent,
        "ToolPolicy.max_tool_calls_per_agent",
        minimum=0,
    )
    per_iteration_tool_calls = _bounded_integer(
        parent_tool_policy.max_tool_calls_per_iteration,
        "ToolPolicy.max_tool_calls_per_iteration",
        minimum=0,
    )
    admitted_tools = _admitted_tool_names(admitted.allowed_tools)
    allowed_tools = tuple(
        tool_name
        for tool_name in admitted_tools
        if parent_tool_policy.allows(tool_name)
    )

    if not isinstance(parent.memory_enabled, bool):
        raise HarnessValidationError(
            "AgentSpec.memory_enabled must be boolean",
            code="subagent_agent_policy_invalid",
        )
    for field_name in ("memory_recall_enabled", "memory_write_enabled"):
        if not isinstance(getattr(parent.loop_policy, field_name), bool):
            raise HarnessValidationError(
                f"AgentLoopPolicy.{field_name} must be boolean",
                code="subagent_agent_policy_invalid",
            )
    if not parent.memory_enabled:
        allowed_tools = tuple(
            tool_name
            for tool_name in allowed_tools
            if not _is_memory_tool(tool_name)
        )

    child_iterations = min(
        parent_iterations,
        loop_iterations,
        admitted_turns,
    )
    child_loop_tool_calls = min(
        loop_tool_calls,
        per_agent_tool_calls,
        admitted_tool_calls,
    )
    child_loop_policy = replace(
        deepcopy(parent.loop_policy),
        max_iterations=child_iterations,
        max_tool_calls=child_loop_tool_calls,
        memory_recall_enabled=(
            parent.loop_policy.memory_recall_enabled
            if parent.memory_enabled
            else False
        ),
        memory_write_enabled=(
            parent.loop_policy.memory_write_enabled
            if parent.memory_enabled
            else False
        ),
        allow_subagents=False,
    )
    child_tool_policy = replace(
        deepcopy(parent_tool_policy),
        allowed_tools=list(allowed_tools),
        blocked_tools=list(parent_tool_policy.blocked_tools),
        denied_tools=list(parent_tool_policy.denied_tools),
        require_approval_for=list(parent_tool_policy.require_approval_for),
        max_tool_calls_per_agent=min(
            per_agent_tool_calls,
            child_loop_tool_calls,
        ),
        max_tool_calls_per_iteration=min(
            per_iteration_tool_calls,
            child_loop_tool_calls,
        ),
        require_explicit_allowlist=True,
    )

    # Deep-copy the entire template first: AgentSpec is frozen, but several of
    # its policy and schema members intentionally remain mutable containers.
    child = deepcopy(parent)
    return replace(
        child,
        allowed_tools=list(allowed_tools),
        tool_names=list(allowed_tools),
        tool_policy=child_tool_policy,
        loop_policy=child_loop_policy,
        max_iterations=child_iterations,
        allowed_subagents=[],
    )


def _required_budget(
    budget: Any,
    field_name: str,
    *,
    minimum: int,
) -> int:
    if not isinstance(budget, dict) or field_name not in budget:
        raise HarnessValidationError(
            f"admitted SubAgentSpec budget requires {field_name}",
            code="subagent_agent_budget_invalid",
        )
    return _bounded_integer(
        budget[field_name],
        f"SubAgentSpec.budget.{field_name}",
        minimum=minimum,
    )


def _bounded_integer(value: Any, field_name: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise HarnessValidationError(
            f"{field_name} must be a {qualifier} integer",
            code="subagent_agent_budget_invalid",
        )
    return value


def _admitted_tool_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, tuple):
        raise HarnessValidationError(
            "admitted SubAgentSpec tools must be a typed tuple",
            code="subagent_agent_policy_invalid",
        )
    tools = tuple(value)
    if (
        not tools
        or any(
            not isinstance(item, str)
            or not item.strip()
            or item != item.strip()
            or item == "*"
            for item in tools
        )
        or len(set(tools)) != len(tools)
    ):
        raise HarnessValidationError(
            "admitted SubAgentSpec tools must be unique concrete names",
            code="subagent_agent_policy_invalid",
        )
    return tools


def _required_schema(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise HarnessValidationError(
            f"admitted SubAgentSpec {field_name} must be a non-empty object",
            code="subagent_agent_policy_schema_mismatch",
        )
    return deepcopy(value)


def _is_memory_tool(tool_name: str) -> bool:
    return tool_name == "memory" or tool_name.startswith("memory.")


__all__ = ["project_child_agent_spec"]
