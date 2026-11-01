from __future__ import annotations

import pytest

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool.governance.approval import ToolApprovalRequest
from framework.tool.models.call import ToolCall
from framework.workers.approval.store import InMemoryApprovalStore
from framework.tool import (
    MappingSecretProvider,
    ToolDefinition,
    ToolJsonSchemaBuilder,
    ToolParameter,
    ToolRegistry,
    ToolResolver,
    register_control_tools,
)


def test_registry_resolver_and_schema_builder() -> None:
    registry = ToolRegistry()
    definition = ToolDefinition(name="sample.add")
    registry.register(definition, lambda args: args)

    schema = ToolJsonSchemaBuilder().build(
        [ToolParameter(name="value", type="integer", required=True)]
    )

    assert ToolResolver().resolve("sample.add", registry).definition == definition
    assert schema["required"] == ["value"]
    assert schema["properties"]["value"]["type"] == "integer"


def test_secret_provider_and_control_tool_success() -> None:
    assert MappingSecretProvider({"TOKEN": "secret"}).get_secret("TOKEN") == "secret"

    registry = ToolRegistry()
    register_control_tools(
        registry,
        approval_store=InMemoryApprovalStore(),
        execution_identity=_identity(),
    )
    result = registry.get("control.request_human_review").executor(
        {
            "requested_action": "review",
            "reason": "needs review",
            "payload": {"record_id": "r1"},
        }
    )

    assert result["control_action"] == "request_human_review"


def test_control_tool_rejects_sensitive_payload_keys() -> None:
    registry = ToolRegistry()
    register_control_tools(
        registry,
        approval_store=InMemoryApprovalStore(),
        execution_identity=_identity(),
    )

    with pytest.raises(ValueError, match="payload key is not allowed"):
        registry.get("control.request_human_review").executor(
            {
                "requested_action": "review",
                "reason": "needs review",
                "payload": {"api_key": "hidden"},
            }
        )


def test_tool_approval_wire_rejects_and_omits_step_id() -> None:
    request = ToolApprovalRequest(
        tool_call=ToolCall(tool_name="sample.publish"),
        tool_name="sample.publish",
        side_effect="write",
        reason="publish",
        risk_level="medium",
        graph_identity=_identity(),
    )
    assert "step_id" not in request.to_dict()
    with pytest.raises(ValueError, match="retired step_id"):
        ToolApprovalRequest(
            tool_call=request.tool_call,
            tool_name=request.tool_name,
            side_effect=request.side_effect,
            reason=request.reason,
            risk_level=request.risk_level,
            graph_identity=_identity(),
            step_id="publish",
        )


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="approval-run",
        graph_id="approval-graph",
        graph_version="1",
        graph_ref="approval-graph@1",
        graph_checksum="sha256:" + "2" * 64,
        node_id="approval",
        node_instance_id="approval-instance",
        activity_id="approval-activity",
        attempt=1,
    )
