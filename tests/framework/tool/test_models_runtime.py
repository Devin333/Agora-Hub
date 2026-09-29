from __future__ import annotations

import pytest

from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolStatus,
)
from framework.shared.graph_identity import GraphExecutionIdentity


def _graph_identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-tool",
        graph_id="research.graph",
        graph_version="v1",
        graph_ref="research.graph@v1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="tool-node",
        node_instance_id="tool-node:1",
        activity_id="tool-activity",
        attempt=1,
    )


def test_tool_definition_and_call_prd_aliases() -> None:
    definition = ToolDefinition.from_dict(
        {
            "name": "sample.echo",
            "description": "Echo",
            "input_schema": {"required": ["message"]},
            "side_effect": "read_only",
        }
    )
    call = ToolCall.new("sample.echo", {"message": "hi"}, requested_by="agent-1")

    assert definition.namespace == "sample"
    assert definition.short_name() == "echo"
    assert call.requested_by == "agent-1"
    assert call.requested_by_agent_id == "agent-1"


def test_tool_executor_returns_success_result() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="sample.echo", input_schema={"required": ["message"]}),
        lambda args: {"message": args["message"]},
    )

    observation = ToolExecutor(registry).execute(
        ToolCall(tool_name="sample.echo", arguments={"message": "hello"}),
        ToolPolicy(allowed_tools=["sample.echo"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.ok is True
    assert observation.result.output == {"message": "hello"}


def test_tool_policy_can_disable_default_approval_for_authorized_side_effect() -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.publish",
            input_schema={},
            side_effect="publishing",
            requires_approval=True,
        ),
        lambda args: {"published": True},
    )

    observation = ToolExecutor(registry).execute(
        ToolCall(tool_name="sample.publish", arguments={}),
        ToolPolicy(
            allowed_tools=["sample.publish"],
            require_approval_for_side_effects=False,
        ),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output == {"published": True}


def test_tool_policy_per_tool_approval_overrides_side_effect_setting() -> None:
    definition = ToolDefinition(
        name="sample.publish",
        input_schema={},
        side_effect="publishing",
        requires_approval=True,
    )

    assert ToolPolicy(
        require_approval_for=[definition.name],
        require_approval_for_side_effects=False,
    ).requires_approval(definition)


def test_tool_executor_binds_and_rejects_graph_execution_identity() -> None:
    identity = _graph_identity()
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="sample.echo", input_schema={"required": ["message"]}),
        lambda args: {"message": args["message"]},
        graph_identity=identity,
    )
    executor = ToolExecutor(registry, graph_identity=identity)

    observation = executor.execute(
        ToolCall(tool_name="sample.echo", arguments={"message": "bound"}),
        ToolPolicy(allowed_tools=["sample.echo"]),
    )

    assert observation.result.graph_identity == identity
    assert observation.result.call_id == observation.call.call_id
    assert observation.call.to_dict()["graph_identity"] == identity.to_dict()

    conflicting = GraphExecutionIdentity(
        run_id=identity.run_id,
        graph_id=identity.graph_id,
        graph_version=identity.graph_version,
        graph_ref=identity.graph_ref,
        graph_checksum=identity.graph_checksum,
        node_id=identity.node_id,
        node_instance_id=identity.node_instance_id,
        activity_id=identity.activity_id,
        attempt=2,
    )
    with pytest.raises(ValueError, match="conflicts with executor identity"):
        executor.execute(
            ToolCall(
                tool_name="sample.echo",
                arguments={"message": "forged"},
                graph_identity=conflicting,
            ),
            ToolPolicy(allowed_tools=["sample.echo"]),
        )
