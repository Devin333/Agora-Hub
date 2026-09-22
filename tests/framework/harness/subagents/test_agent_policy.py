from __future__ import annotations

from dataclasses import replace

import pytest

from framework.agent.models import AgentLoopPolicy, AgentSpec
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.agent_policy import project_child_agent_spec
from framework.harness.subagents.fake import FakeSubAgentRuntime, fake_subagent_spec
from framework.tool import ToolPolicy


OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["result"],
    "properties": {"result": {"type": "string"}},
}


def _invocation(**spec_overrides):
    values = {
        "output_schema": OUTPUT_SCHEMA,
        "allowed_tools": (
            "search.read",
            "blocked.tool",
            "memory.recall",
            "other.read",
        ),
        "budget": {"max_turns": 4, "max_tool_calls": 2, "max_memory_ops": 1},
    }
    values.update(spec_overrides)
    spec = fake_subagent_spec(**values)
    return FakeSubAgentRuntime(spec).build_invocation()


def _agent(**overrides) -> AgentSpec:
    values = {
        "agent_id": "critic",
        "name": "Evidence Critic",
        "instructions": "Review admitted evidence and return structured JSON.",
        "role": "Runtime evidence critic",
        "goal": "Review one admitted task.",
        "input_keys": ["evidence"],
        "output_key": "result",
        "output_schema": OUTPUT_SCHEMA,
        "allowed_tools": ["legacy.direct"],
        "tool_names": ["legacy.alias"],
        "loop_policy": AgentLoopPolicy(
            max_iterations=6,
            max_tool_calls=7,
            allow_parallel_tool_calls=True,
            memory_recall_enabled=True,
            memory_write_enabled=True,
            allow_subagents=True,
        ),
        "tool_policy": ToolPolicy(
            allowed_tools=["legacy.policy"],
            blocked_tools=["blocked.tool"],
            denied_tools=["denied.tool"],
            require_approval_for=["search.read"],
            allow_network_access=False,
            allow_mcp_tools=False,
            max_tool_calls_per_iteration=5,
            max_tool_calls_per_agent=6,
            require_explicit_allowlist=False,
            allow_dangerous_tools=False,
            require_approval_for_side_effects=True,
            max_result_bytes=512,
            max_result_chars_inline=128,
        ),
        "model_route": "child-model-route",
        "model_policy": {"route_id": "child-model-route", "nested": {"temperature": 0}},
        "validation_policy": {"strict": True, "gates": ["SchemaGate@1"]},
        "allowed_references": ["artifact://accepted/input"],
        "allowed_subagents": ["nested-child"],
        "metadata": {"owner": {"team": "research"}},
        "max_iterations": 7,
    }
    values.update(overrides)
    return AgentSpec(**values)


def test_projection_intersects_tools_and_preserves_parent_safety_policy():
    parent = _agent()
    invocation = _invocation()

    child = project_child_agent_spec(parent, invocation)

    assert child.allowed_tools == ["search.read", "memory.recall", "other.read"]
    assert child.tool_names == child.allowed_tools
    assert child.tool_policy is not None
    assert child.tool_policy.allowed_tools == child.allowed_tools
    assert child.tool_policy.require_explicit_allowlist is True
    assert child.tool_policy.blocked_tools == ["blocked.tool", "denied.tool"]
    assert child.tool_policy.denied_tools == ["blocked.tool", "denied.tool"]
    assert child.tool_policy.require_approval_for == ["search.read"]
    assert child.tool_policy.allow_network_access is False
    assert child.tool_policy.allow_mcp_tools is False
    assert child.tool_policy.allow_dangerous_tools is False
    assert child.tool_policy.require_approval_for_side_effects is True
    assert child.tool_policy.max_result_bytes == 512
    assert child.tool_policy.max_result_chars_inline == 128
    original_policy = parent.resolved_tool_policy().to_dict()
    child_policy = child.tool_policy.to_dict()
    narrowed_fields = {
        "allowed_tools",
        "max_tool_calls_per_agent",
        "max_tool_calls_per_iteration",
        "require_explicit_allowlist",
    }
    assert {
        key: value
        for key, value in child_policy.items()
        if key not in narrowed_fields
    } == {
        key: value
        for key, value in original_policy.items()
        if key not in narrowed_fields
    }
    assert child.max_iterations == child.loop_policy.max_iterations == 4
    assert child.loop_policy.max_tool_calls == 2
    assert child.tool_policy.max_tool_calls_per_agent == 2
    assert child.tool_policy.max_tool_calls_per_iteration == 2
    assert child.agent_id == invocation.subagent_spec.subagent_id == "critic"
    assert child.role == parent.role
    assert child.instructions == parent.instructions
    assert child.input_keys == parent.input_keys
    assert child.model_route == parent.model_route
    assert child.model_policy == parent.model_policy
    assert child.output_schema == invocation.subagent_spec.output_schema
    assert child.output_schema == parent.output_schema


def test_explicit_zero_tool_budget_is_not_replaced_by_a_default():
    parent = _agent()
    invocation = _invocation(
        budget={"max_turns": 3, "max_tool_calls": 0, "max_memory_ops": 0},
    )

    child = project_child_agent_spec(parent, invocation)

    assert child.max_iterations == child.loop_policy.max_iterations == 3
    assert child.loop_policy.max_tool_calls == 0
    assert child.tool_policy is not None
    assert child.tool_policy.max_tool_calls_per_agent == 0
    assert child.tool_policy.max_tool_calls_per_iteration == 0


def test_explicit_parent_allowlist_is_intersected_with_admitted_tools():
    baseline = _agent()
    parent = replace(
        baseline,
        tool_policy=replace(
            baseline.tool_policy,
            allowed_tools=["search.read", "parent.only"],
            require_explicit_allowlist=True,
        ),
    )

    child = project_child_agent_spec(parent, _invocation())

    assert child.allowed_tools == ["search.read"]
    assert child.tool_names == ["search.read"]
    assert child.tool_policy is not None
    assert child.tool_policy.allowed_tools == ["search.read"]


def test_explicit_zero_parent_tool_limits_remain_zero():
    baseline = _agent()
    parent = replace(
        baseline,
        loop_policy=replace(baseline.loop_policy, max_tool_calls=0),
        tool_policy=replace(
            baseline.tool_policy,
            max_tool_calls_per_agent=0,
            max_tool_calls_per_iteration=0,
        ),
    )

    child = project_child_agent_spec(parent, _invocation())

    assert child.loop_policy.max_tool_calls == 0
    assert child.tool_policy is not None
    assert child.tool_policy.max_tool_calls_per_agent == 0
    assert child.tool_policy.max_tool_calls_per_iteration == 0


def test_memory_disabled_removes_explicit_memory_tools_and_automatic_memory():
    child = project_child_agent_spec(
        _agent(memory_enabled=False),
        _invocation(),
    )

    assert child.memory_enabled is False
    assert child.allowed_tools == ["search.read", "other.read"]
    assert child.tool_names == child.allowed_tools
    assert child.tool_policy is not None
    assert child.tool_policy.allowed_tools == child.allowed_tools
    assert child.loop_policy.memory_recall_enabled is False
    assert child.loop_policy.memory_write_enabled is False


def test_disabling_automatic_recall_keeps_an_authorized_explicit_memory_tool():
    parent = _agent(
        loop_policy=replace(
            _agent().loop_policy,
            memory_recall_enabled=False,
            memory_write_enabled=True,
        )
    )

    child = project_child_agent_spec(parent, _invocation())

    assert "memory.recall" in child.allowed_tools
    assert child.loop_policy.memory_recall_enabled is False
    assert child.loop_policy.memory_write_enabled is True


def test_projection_disables_nested_delegation_and_does_not_alias_inputs():
    parent = _agent()
    invocation = _invocation()
    original_parent = parent.to_dict()
    original_invocation = invocation.subagent_spec.to_dict()

    child = project_child_agent_spec(parent, invocation)

    assert child.allow_subagents is False
    assert child.allowed_subagents == []
    child.allowed_tools.append("mutated.tool")
    child.tool_names.append("mutated.alias")
    assert child.tool_policy is not None
    child.tool_policy.allowed_tools.append("mutated.policy")
    child.output_schema["properties"]["result"]["type"] = "integer"
    child.model_policy["nested"]["temperature"] = 1
    child.validation_policy["gates"].append("OtherGate@1")
    child.metadata["owner"]["team"] = "other"
    child.allowed_references.append("artifact://mutated")

    assert parent.to_dict() == original_parent
    assert invocation.subagent_spec.to_dict() == original_invocation


@pytest.mark.parametrize(
    "budget",
    (
        {"max_tool_calls": 1},
        {"max_turns": 0, "max_tool_calls": 1},
        {"max_turns": 1, "max_tool_calls": True},
        {"max_turns": 1},
    ),
)
def test_missing_or_invalid_required_admission_budget_fails_closed(budget):
    with pytest.raises(HarnessValidationError) as raised:
        project_child_agent_spec(_agent(), _invocation(budget=budget))

    assert raised.value.code == "subagent_agent_budget_invalid"


def test_wildcard_admission_tool_fails_closed():
    with pytest.raises(HarnessValidationError) as raised:
        project_child_agent_spec(
            _agent(),
            _invocation(allowed_tools=("*",)),
        )

    assert raised.value.code == "subagent_agent_policy_invalid"


@pytest.mark.parametrize("field_name", ("input_schema", "output_schema"))
def test_missing_admitted_schema_fails_closed(field_name):
    with pytest.raises(HarnessValidationError) as raised:
        project_child_agent_spec(_agent(), _invocation(**{field_name: {}}))

    assert raised.value.code == "subagent_agent_policy_schema_mismatch"


def test_admitted_identity_conflict_fails_closed():
    with pytest.raises(HarnessValidationError) as raised:
        project_child_agent_spec(
            _agent(agent_id="different-child"),
            _invocation(),
        )

    assert raised.value.code == "subagent_agent_policy_identity_mismatch"


def test_output_schema_conflict_fails_closed():
    with pytest.raises(HarnessValidationError) as raised:
        project_child_agent_spec(
            _agent(output_schema={"type": "string"}),
            _invocation(),
        )

    assert raised.value.code == "subagent_agent_policy_schema_mismatch"
