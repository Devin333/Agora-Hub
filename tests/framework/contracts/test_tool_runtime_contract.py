from __future__ import annotations

import time

from framework.events.runtime.projection import RuntimeEventType
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolCall, ToolDefinition, ToolExecutor, ToolPolicy, ToolRegistry, ToolStatus
from framework.agent.artifacts import ArtifactManager


def test_tool_runtime_contract_standard_result_paths(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(name="contract.echo", input_schema={"required": ["message"]}),
        lambda args: {"message": args["message"], "token": "sk-1234567890abcdef"},
    )
    registry.register(
        ToolDefinition(name="contract.large", input_schema={}),
        lambda args: {"text": "x" * 128},
    )
    registry.register(
        ToolDefinition(name="contract.slow", input_schema={}, timeout_seconds=0.01),
        lambda args: time.sleep(0.05) or {"ok": True},
    )

    success = ToolExecutor(registry).execute(
        ToolCall(tool_name="contract.echo", arguments={"message": "hi"}, call_id="call-ok"),
        ToolPolicy(allowed_tools=["contract.echo"]),
    )
    denied = ToolExecutor(registry).execute(
        ToolCall(tool_name="contract.echo", arguments={"message": "hi"}, call_id="call-denied"),
        ToolPolicy(allowed_tools=[]),
    )
    spilled = ToolExecutor(
        registry,
        artifact_manager=ArtifactManager(tmp_path),
        run_id="run-tool-contract",
    ).execute(
        ToolCall(tool_name="contract.large", call_id="call-large"),
        ToolPolicy(allowed_tools=["contract.large"], spill_large_results_to_artifact=True, max_result_chars_inline=8),
    )
    timeout = ToolExecutor(registry).execute(
        ToolCall(tool_name="contract.slow", call_id="call-timeout"),
        ToolPolicy(allowed_tools=["contract.slow"]),
    )

    assert success.status == ToolStatus.SUCCEEDED
    assert success.result.policy_trace.allowed is True
    assert "sk-1234567890abcdef" not in str(success.result.to_dict()["redacted_output"])
    assert denied.status == ToolStatus.BLOCKED
    assert denied.result.gate_result["decision"] == "block"
    assert spilled.status == ToolStatus.SUCCEEDED
    assert spilled.result.artifact_refs
    assert spilled.result.policy_trace.checks[-1]["check_id"] == "tool.artifact_spill"
    assert timeout.status == ToolStatus.TIMEOUT
    assert timeout.result.timeout is True
    assert timeout.result.error_envelope["error_type"] == "ToolTimeoutError"


def test_tool_executor_runtime_events_use_canonical_projection_contract() -> None:
    emitted = []
    identity = GraphExecutionIdentity(
        run_id="run-tool-events",
        graph_id="research",
        graph_version="2026.09",
        graph_ref="research@2026.09",
        graph_checksum="sha256:" + "a" * 64,
        node_id="collect",
        node_instance_id="collect-1",
        activity_id="activity-1",
        attempt=1,
    )
    executor = ToolExecutor(ToolRegistry(), runtime_event_sink=emitted.append)
    call = ToolCall(
        tool_name="research.fetch",
        call_id="call-events",
        graph_identity=identity,
    )
    checksum = "sha256:" + "b" * 64
    payload = {
        "attempt_id": "attempt-1",
        "status": "succeeded",
        "reason_code": "completed",
        "artifact_ref": "artifact://evidence/1",
        "execution_receipt_checksum": checksum,
        "execution_provider_id": {
            "provider": "local",
            "api_key": "must-not-be-persisted",
        },
    }

    executor._emit("tool_succeeded", call, payload)
    executor._emit("tool_succeeded", call, payload)

    assert len(emitted) == 2
    first, replay = emitted
    assert first.event_type is RuntimeEventType.EXECUTION_TERMINAL
    assert first.event_id == replay.event_id
    assert first.identity.graph_identity == identity
    assert first.identity.activity_id == identity.activity_id
    assert first.identity.attempt_id == "attempt-1"
    assert first.stream_id == identity.run_id
    assert first.refs == ("artifact://evidence/1",)
    assert first.checksums == {"execution_receipt_checksum": checksum}
    assert first.metadata["execution_provider_id"] == {
        "provider": "local",
        "api_key": "[redacted]",
    }
