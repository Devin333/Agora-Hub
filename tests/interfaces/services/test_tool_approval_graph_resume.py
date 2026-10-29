"""End-to-end: Graph tool-approval Wait -> approve -> node re-entry -> replay.

The AgentLoop node requests an approval-gated tool, the Graph parks on a
re-entrant approval Wait that records the approval id, the approval service
decides it and drives the Graph, and the re-entered node redeems exactly
that grant through ``AgentRunner.run(resume_approval_id=...)``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from framework.agent.loop.runner import AgentRunner
from framework.agent.models import AgentLoopPolicy, AgentSpec
from framework.events.canonical import checksum_for
from framework.harness.agent_loop import AgentLoopGraphApprovalWaitBinding
from framework.harness.control_plane import HarnessRunSpec
from framework.harness.control_plane.graph_wait_approval import (
    authoritative_wait_approval_id,
)
from framework.harness.graph import (
    HarnessGraphDefinition,
    HarnessGraphLeafBinding,
    HarnessGraphSpec,
    HarnessLeafActivityKind,
    HarnessStepSpec,
    HarnessWorkerType,
)
from framework.harness.side_effects import HarnessTerminalSideEffectPolicy
from framework.llm import FakeLLMClient
from framework.tool import (
    ToolDefinition,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
)
from framework.workers.approval import ApprovalStatus, InMemoryApprovalStore
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.storage.conversation import LocalJsonConversationStore
from infrastructure.storage.harness import SQLiteHarnessNodeOutputResource
from interfaces.composition.agent_loop_graph import (
    build_agent_loop_graph_runtime_composition,
)
from interfaces.composition.tool_approval_resume import (
    HarnessGraphToolApprovalResumer,
)
from interfaces.services.approval_service import (
    ApprovalApplicationService,
    ApprovalGraphResumeError,
)
from tests.interfaces.services.test_agent_loop_graph_service import (
    ACTIVITY_REF,
    WORKER_REF,
    _durable_event_port,
    _terminal_side_effects_for,
)

_TOOL_NAME = "newsroom.publish"
_ARGUMENTS = {"channel": "news", "title": "Daily brief"}
_NODE_ID = "run-agent-loop"
_OUTPUT_KEY = "agent_loop_result"


class _PublishTool:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(arguments))
        return {"story_ref": f"{arguments['channel']}/{arguments['title']}"}


def _registry(tool: _PublishTool) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=_TOOL_NAME,
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


def _agent() -> AgentSpec:
    return AgentSpec(
        agent_id="publisher-agent",
        name="Publisher",
        role="Publisher",
        goal="Publish the brief and report it",
        instructions="Publish the brief, then report.",
        output_key="analysis_result",
        allowed_tools=[_TOOL_NAME],
        loop_policy=AgentLoopPolicy(max_iterations=4),
        tool_policy=ToolPolicy(
            allowed_tools=[_TOOL_NAME],
            require_explicit_allowlist=True,
            require_approval_for_side_effects=True,
        ),
    )


def _llm_script() -> list[str]:
    """First turn requests the tool; the re-entered turn reports completion."""

    return [
        json.dumps(
            {
                "action_type": "tool_call",
                "tool_name": _TOOL_NAME,
                "tool_args": dict(_ARGUMENTS),
            }
        ),
        json.dumps(
            {
                "action_type": "final_output",
                "output": {"analysis_result": {"story_ref": "news/Daily brief"}},
            }
        ),
    ]


def _wait_binding() -> AgentLoopGraphApprovalWaitBinding:
    return AgentLoopGraphApprovalWaitBinding(
        source_node_id=_NODE_ID,
        result_output_key=_OUTPUT_KEY,
        wait_id="agent-loop-tool-approval",
    )


def _run_spec(run_id: str) -> HarnessRunSpec:
    binding = _wait_binding()
    step = HarnessStepSpec(
        _NODE_ID,
        HarnessWorkerType.AGENT_LOOP,
        input_keys=("inputs", "conversation_id", "resume_from_cursor"),
        output_key=_OUTPUT_KEY,
        quality_gate=binding.gate_ref,
        metadata={
            "control_fact_paths": binding.control_fact_paths,
            "tool_allowlist": [_TOOL_NAME],
        },
    )
    binding.assert_step_contract(step)
    graph_id = "production.agent-loop.tool-approval"
    definition = HarnessGraphDefinition(
        graph_id=graph_id,
        graph_version="1",
        root=HarnessGraphSpec(
            graph_id=graph_id,
            root=binding.approval_loop(
                loop_id="agent-loop-approval-loop",
                max_approval_rounds=2,
            ),
            input_keys=(
                "inputs",
                "conversation_id",
                "resume_from_cursor",
                "tenant_scope_ref",
                "identity_scope_ref",
            ),
        ),
        activities=(step,),
        leaf_activity_bindings=(
            HarnessGraphLeafBinding(
                activity_id=_NODE_ID,
                leaf_activity_kind=HarnessLeafActivityKind.AGENT_LOOP,
                worker_ref=WORKER_REF,
                activity_ref=ACTIVITY_REF,
            ),
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="production.agent-loop-terminal",
            version="1",
            handler="production.agent-loop-terminal@1",
            kind="artifact",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=checksum_for(
                {"terminal": "production.agent-loop"}
            ),
        ),
    )
    tenant_scope_ref = checksum_for({"tenant": "production"})
    # The durable transition port is bound to the "production" identity scope.
    identity_scope_ref = checksum_for("production")
    return HarnessRunSpec(
        run_id=run_id,
        graph=definition,
        inputs={
            "inputs": {"topic": "daily-brief"},
            "conversation_id": f"{run_id}-conversation",
            "resume_from_cursor": False,
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
        },
        metadata={
            "tenant_scope_ref": tenant_scope_ref,
            "identity_scope_ref": identity_scope_ref,
            "subject_scope_ref": checksum_for({"subject": run_id}),
        },
    )


class _Harness:
    """One process: durable stores, the Graph runtime and the approval service."""

    def __init__(self, root: Path, *, llm: FakeLLMClient | None = None) -> None:
        self.tool = _PublishTool()
        self.llm = llm or FakeLLMClient(_llm_script())
        self.approval_store = InMemoryApprovalStore()
        self.resumer = HarnessGraphToolApprovalResumer()
        registry, side_effect_store, self.terminal_handler = _terminal_side_effects_for(
            root / "side-effects.sqlite3"
        )
        self.runner = AgentRunner(
            llm_client=self.llm,
            tool_registry=_registry(self.tool),
            conversation_store=LocalJsonConversationStore(root / "conversation"),
        )
        self.composition = build_agent_loop_graph_runtime_composition(
            agent_runner=self.runner,
            agent=_agent(),
            artifact_port=FilesystemHarnessArtifactPort(root),
            node_output_resource=SQLiteHarnessNodeOutputResource(root / "node.sqlite3"),
            event_port=_durable_event_port(root),
            worker_ref=WORKER_REF,
            activity_ref=ACTIVITY_REF,
            side_effect_registry=registry,
            side_effect_store=side_effect_store,
            approval_store=self.approval_store,
            approval_resumer=self.resumer,
        )
        self.approvals = ApprovalApplicationService(
            store=self.approval_store,
            graph_resume=self.resumer,
        )

    def pending_approval_id(self) -> str:
        pending = self.approval_store.list_approvals(status=ApprovalStatus.PENDING)
        assert len(pending) == 1
        return pending[0].approval_id


def test_approved_tool_wait_reenters_node_and_executes_tool_once(tmp_path: Path) -> None:
    harness = _Harness(tmp_path / "approve")
    run_spec = _run_spec("tool-approval-e2e")

    waiting = harness.composition.run(run_spec)

    assert waiting.status.value == "waiting_approval"
    assert harness.tool.calls == []
    approval_id = harness.pending_approval_id()
    approval = harness.approval_store.get_approval(approval_id)
    assert approval.graph_identity is not None
    assert approval.graph_identity.node_id == _NODE_ID
    state = harness.composition.recover_graph(run_spec)
    (registration,) = (
        item for item in state.wait_registrations if item.unresolved
    )
    assert registration.reentrant is True
    assert registration.resume_node_id == _NODE_ID
    assert registration.approval_id == approval_id
    assert (
        authoritative_wait_approval_id(
            state,
            registration.node_instance_id,
            graph_inputs=run_spec.inputs,
        )
        == approval_id
    )

    decided = harness.approvals.approve(approval_id, decided_by="editor", reason="ok")

    assert decided.graph_resume is not None
    assert decided.graph_resume.resumed is True
    assert decided.graph_resume.approved is True
    assert decided.graph_resume.run_status == "succeeded"
    assert harness.tool.calls == [_ARGUMENTS]
    assert harness.approval_store.get_approval(approval_id).status == ApprovalStatus.CONSUMED
    # The re-entered node replayed the approved call without asking the model
    # to request it again: one turn to request, one turn to report.
    assert len(harness.llm.requests) == 2
    resumed_state = harness.composition.recover_graph(run_spec)
    agent_instances = [
        node for node in resumed_state.node_instances if node.identity.node_id == _NODE_ID
    ]
    assert len(agent_instances) == 2
    assert all(not item.unresolved for item in resumed_state.wait_registrations)

    # Re-applying the decision finds the Wait already resolved and does not
    # drive the Graph or the tool again.
    again = harness.approvals.resume_graph(approval_id)

    assert again.graph_resume is not None
    assert again.graph_resume.resumed is False
    assert harness.tool.calls == [_ARGUMENTS]
    assert harness.composition.recover_graph(run_spec).last_event_sequence == (
        resumed_state.last_event_sequence
    )


def test_consumed_grant_is_denied_on_second_redeem(tmp_path: Path) -> None:
    harness = _Harness(tmp_path / "second-redeem")
    run_spec = _run_spec("tool-approval-second-redeem")
    harness.composition.run(run_spec)
    approval_id = harness.pending_approval_id()
    harness.approvals.approve(approval_id, decided_by="editor")
    assert harness.tool.calls == [_ARGUMENTS]

    approval = harness.approval_store.get_approval(approval_id)
    identity = approval.graph_identity
    assert identity is not None
    replay_llm = FakeLLMClient(
        [
            json.dumps(
                {
                    "action_type": "final_output",
                    "output": {"analysis_result": {"story_ref": "refused"}},
                }
            )
        ]
    )
    replay_runner = AgentRunner(
        llm_client=replay_llm,
        tool_registry=_registry(harness.tool),
        approval_store=harness.approval_store,
    )

    replay_runner.run(
        _agent(),
        {"topic": "daily-brief"},
        resume_approval_id=approval_id,
        **identity.to_dict(),
        graph_checkpoint_ref=f"checkpoint://{identity.run_id}/{identity.activity_id}",
    )

    assert harness.tool.calls == [_ARGUMENTS]
    assert harness.approval_store.get_approval(approval_id).status == ApprovalStatus.CONSUMED
    assert "approval_consumed" in replay_llm.requests[0].estimated_prompt_text()


def test_rejected_tool_approval_cancels_the_graph_without_executing(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path / "reject")
    run_spec = _run_spec("tool-approval-reject")
    harness.composition.run(run_spec)
    approval_id = harness.pending_approval_id()

    decided = harness.approvals.reject(approval_id, decided_by="editor", reason="no")

    assert decided.graph_resume is not None
    assert decided.graph_resume.approved is False
    assert decided.graph_resume.resumed is True
    assert decided.graph_resume.run_status != "succeeded"
    assert harness.tool.calls == []
    assert harness.approval_store.get_approval(approval_id).status == ApprovalStatus.REJECTED


def test_graph_bound_approval_without_resume_runtime_fails_after_durable_decision(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path / "unbound")
    run_spec = _run_spec("tool-approval-unbound")
    harness.composition.run(run_spec)
    approval_id = harness.pending_approval_id()
    service = ApprovalApplicationService(store=harness.approval_store)

    with pytest.raises(ApprovalGraphResumeError) as raised:
        service.approve(approval_id, decided_by="editor")

    assert raised.value.code == "approval_graph_resume_unavailable"
    # The decision is durable; the bound runtime can apply it afterwards.
    assert harness.approval_store.get_approval(approval_id).status == ApprovalStatus.APPROVED
    resumed = harness.approvals.resume_graph(approval_id)
    assert resumed.graph_resume is not None
    assert resumed.graph_resume.resumed is True
    assert harness.tool.calls == [_ARGUMENTS]


def test_resumer_rejects_approval_whose_run_has_no_live_runtime(tmp_path: Path) -> None:
    harness = _Harness(tmp_path / "foreign")
    run_spec = _run_spec("tool-approval-foreign")
    harness.composition.run(run_spec)
    approval_id = harness.pending_approval_id()
    other_process = ApprovalApplicationService(
        store=harness.approval_store,
        graph_resume=HarnessGraphToolApprovalResumer(),
    )

    with pytest.raises(ApprovalGraphResumeError) as raised:
        other_process.approve(approval_id, decided_by="editor")

    assert raised.value.code == "approval_graph_run_unavailable"
    assert harness.tool.calls == []
