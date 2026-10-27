from __future__ import annotations

import json
from typing import Any

import pytest

from framework.agent.loop import AgentRunner
from framework.agent.models import AgentLoopPolicy, AgentLoopStatus, AgentSpec
from framework.llm import FakeLLMClient
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolDefinition,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
)
from framework.workers.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)

_RUN_ID = "run-approval-replay"
_ARGUMENTS = {"channel": "news", "title": "Daily brief"}


class _PublishTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"published": arguments["title"]}


def _registry(tool: _PublishTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.publish",
            description="Publish a story to a channel.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["channel", "title"],
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.WRITES_EXTERNAL_STATE,
        ),
        tool,
    )
    return registry


def _agent(agent_id: str = "publisher-agent") -> AgentSpec:
    return AgentSpec(
        agent_id=agent_id,
        name="Publisher",
        instructions="Publish the brief, then report.",
        allowed_tools=["sample.publish"],
        loop_policy=AgentLoopPolicy(max_iterations=4),
        tool_policy=ToolPolicy(
            allowed_tools=["sample.publish"],
            require_explicit_allowlist=True,
            require_approval_for_side_effects=True,
        ),
    )


def _tool_call_response() -> str:
    return json.dumps(
        {
            "action_type": "tool_call",
            "tool_name": "sample.publish",
            "tool_args": dict(_ARGUMENTS),
        }
    )


def _final_output(summary: str) -> str:
    return json.dumps(
        {"action_type": "final_output", "output": {"output": {"summary": summary}}}
    )


def _runner(llm: FakeLLMClient, tool: _PublishTool, store: Any) -> AgentRunner:
    return AgentRunner(llm_client=llm, tool_registry=_registry(tool), approval_store=store)


def _first_run(store: InMemoryApprovalStore, tool: _PublishTool) -> str:
    llm = FakeLLMClient([_tool_call_response()])
    result = _runner(llm, tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
    )
    assert result.status == AgentLoopStatus.WAITING_FOR_APPROVAL
    approval_ids = {
        event.get("approval_id")
        for event in result.events
        if event["event_type"] == "tool_approval_required"
    }
    assert approval_ids == {_pending_id(store)}
    return approval_ids.pop()


def _pending_id(store: InMemoryApprovalStore) -> str:
    pending = store.list_approvals(status=ApprovalStatus.PENDING)
    assert len(pending) == 1
    return pending[0].approval_id


def _decide(store: InMemoryApprovalStore, approval_id: str, decision_type: str, **extra: Any) -> None:
    store.record_decision(
        approval_id,
        decision=ApprovalDecision(decision_type=decision_type, decided_by="editor", **extra),
    )


def test_approved_call_is_replayed_once_without_asking_the_model_again() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)
    assert tool.calls == []
    _decide(store, approval_id, "approve")

    llm = FakeLLMClient([_final_output("published daily brief")])
    result = _runner(llm, tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
        resume_approval_id=approval_id,
    )

    assert result.success is True
    assert result.status == AgentLoopStatus.ACCEPTED
    assert tool.calls == [_ARGUMENTS]
    # The replayed call consumed no LLM turn; the model only wrote the report.
    assert llm.call_count == 1
    assert "published" in llm.requests[0].estimated_prompt_text()
    consumed = store.get_approval(approval_id)
    assert consumed.status == ApprovalStatus.CONSUMED
    assert result.metrics.iterations == 2


def test_second_resume_with_consumed_approval_does_not_run_tool_again() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)
    _decide(store, approval_id, "approve")
    _runner(FakeLLMClient([_final_output("done")]), tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
        resume_approval_id=approval_id,
    )

    llm = FakeLLMClient([_final_output("could not publish twice")])
    result = _runner(llm, tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
        resume_approval_id=approval_id,
    )

    assert tool.calls == [_ARGUMENTS]
    assert "approval is consumed" in llm.requests[0].estimated_prompt_text()
    assert result.success is True


def test_rejected_approval_is_fed_back_to_the_model() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)
    _decide(store, approval_id, "reject", reason="embargoed")

    llm = FakeLLMClient([_final_output("publication was rejected")])
    result = _runner(llm, tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
        resume_approval_id=approval_id,
    )

    assert tool.calls == []
    assert result.success is True
    assert "approval is rejected" in llm.requests[0].estimated_prompt_text()


def test_resume_while_still_pending_waits_again_without_llm_call() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)

    llm = FakeLLMClient([])
    result = _runner(llm, tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
        resume_approval_id=approval_id,
    )

    assert result.status == AgentLoopStatus.WAITING_FOR_APPROVAL
    assert llm.call_count == 0
    assert tool.calls == []
    assert len(store.list_approvals()) == 1


def test_modified_approval_runs_reviewer_arguments() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)
    _decide(
        store,
        approval_id,
        "modify",
        modifications={"arguments": {"channel": "news", "title": "Edited brief"}},
    )

    _runner(FakeLLMClient([_final_output("done")]), tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id=_RUN_ID,
        standalone=True,
        resume_approval_id=approval_id,
    )

    assert tool.calls == [{"channel": "news", "title": "Edited brief"}]


def test_resume_is_refused_for_another_agent_or_unknown_kind() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)
    _decide(store, approval_id, "approve")
    foreign = store.upsert_approval(
        ApprovalRequest(requested_action="publish report", risk_level="high")
    )
    llm = FakeLLMClient([])
    runner = _runner(llm, tool, store)

    with pytest.raises(ValueError, match="different agent"):
        runner.run(
            _agent("other-agent"),
            {"topic": "brief"},
            run_id=_RUN_ID,
            standalone=True,
            resume_approval_id=approval_id,
        )
    with pytest.raises(ValueError, match="not a tool execution approval"):
        runner.run(
            _agent(),
            {"topic": "brief"},
            run_id=_RUN_ID,
            standalone=True,
            resume_approval_id=foreign.approval_id,
        )
    assert llm.call_count == 0
    assert tool.calls == []


def test_resume_requires_an_approval_store() -> None:
    runner = AgentRunner(llm_client=FakeLLMClient([]), tool_registry=_registry(_PublishTool()))

    with pytest.raises(ValueError, match="approval_store"):
        runner.run(
            _agent(),
            {"topic": "brief"},
            run_id=_RUN_ID,
            standalone=True,
            resume_approval_id="tool_appr_missing",
        )


def test_approval_from_another_run_cannot_be_redeemed() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    approval_id = _first_run(store, tool)
    _decide(store, approval_id, "approve")

    llm = FakeLLMClient([_final_output("refused")])
    _runner(llm, tool, store).run(
        _agent(),
        {"topic": "brief"},
        run_id="run-other",
        standalone=True,
        resume_approval_id=approval_id,
    )

    assert tool.calls == []
    assert store.get_approval(approval_id).status == ApprovalStatus.APPROVED
    assert "different tool call" in llm.requests[0].estimated_prompt_text()


def test_graph_bound_replay_survives_new_activity_attempt() -> None:
    store = InMemoryApprovalStore()
    tool = _PublishTool()
    first_identity = _identity(activity_id="activity-1", attempt=1)
    first = _runner(FakeLLMClient([_tool_call_response()]), tool, store).run(
        _agent(),
        {"topic": "brief"},
        **_graph_kwargs(first_identity),
    )
    assert first.status == AgentLoopStatus.WAITING_FOR_APPROVAL
    approval_id = _pending_id(store)
    assert store.get_approval(approval_id).graph_identity == first_identity
    _decide(store, approval_id, "approve")

    resumed_identity = _identity(activity_id="activity-2", attempt=2, node_instance_id="publish:2")
    result = _runner(FakeLLMClient([_final_output("done")]), tool, store).run(
        _agent(),
        {"topic": "brief"},
        resume_approval_id=approval_id,
        **_graph_kwargs(resumed_identity),
    )

    assert result.success is True
    assert tool.calls == [_ARGUMENTS]


def test_replay_is_refused_with_durable_tool_evidence() -> None:
    class _DurableEvidence:
        is_durable = True

    runner = _runner(FakeLLMClient([]), _PublishTool(), InMemoryApprovalStore())

    with pytest.raises(ValueError, match="durable tool execution evidence"):
        runner.run(
            _agent(),
            {"topic": "brief"},
            resume_approval_id="tool_appr_1",
            tool_execution_evidence=_DurableEvidence(),  # type: ignore[arg-type]
            **_graph_kwargs(_identity()),
        )


def _identity(**overrides: Any) -> GraphExecutionIdentity:
    values: dict[str, Any] = {
        "run_id": _RUN_ID,
        "graph_id": "newsroom.publish",
        "graph_version": "1",
        "graph_ref": "newsroom.publish@1",
        "graph_checksum": "sha256:" + "e" * 64,
        "node_id": "publish",
        "node_instance_id": "publish:1",
        "activity_id": "activity-1",
        "attempt": 1,
    }
    values.update(overrides)
    return GraphExecutionIdentity(**values)


def _graph_kwargs(identity: GraphExecutionIdentity) -> dict[str, Any]:
    return {
        **identity.to_dict(),
        "graph_checkpoint_ref": f"checkpoint://{identity.run_id}/{identity.activity_id}",
    }
