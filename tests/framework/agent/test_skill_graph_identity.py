from __future__ import annotations

import pytest

from framework.agent.loop import AgentLoop
from framework.agent.skill_call import SkillCall
from framework.agent.skill_context import AgentSkillRuntime, _build_skill_run_context
from framework.shared.graph_identity import GraphExecutionIdentity, GraphStageIdentity
from framework.tool import ToolExecutor, ToolRegistry


IDENTITY = GraphExecutionIdentity(
    run_id="run-skill",
    graph_id="research.graph",
    graph_version="v1",
    graph_ref="research.graph@v1",
    graph_checksum="sha256:" + "a" * 64,
    node_id="analyze",
    node_instance_id="analyze-1",
    activity_id="activity-1",
    attempt=1,
)


class _Registry:
    def get(self, skill_name: str):
        return {"name": skill_name}


class _Runner:
    def __init__(self) -> None:
        self.context = None

    def run(self, skill_name: str, input_data: dict[str, object], context):
        self.context = context
        return {"status": "success", "summary": "ok"}


def test_skill_runtime_propagates_exact_graph_identity() -> None:
    runner = _Runner()
    observation = AgentSkillRuntime(_Registry(), runner).execute_call(
        SkillCall(skill_name="reader", call_id="call-1"),
        agent_run_id=IDENTITY.run_id,
        execution_identity=IDENTITY,
    )

    assert observation.status != "failed"
    assert runner.context.caller_type == "graph"
    assert runner.context.caller_id == IDENTITY.node_instance_id
    assert runner.context.metadata["graph_identity"] == IDENTITY.to_dict()
    assert runner.context.metadata["call_id"] == "call-1"


def test_skill_runtime_preserves_standalone_agent_context() -> None:
    runner = _Runner()
    AgentSkillRuntime(_Registry(), runner).execute_call(
        SkillCall(skill_name="reader", call_id="call-2"),
        agent_run_id="agent-run",
    )

    assert runner.context.caller_type == "agent"
    assert runner.context.metadata["agent_run_id"] == "agent-run"
    assert "graph_identity" not in runner.context.metadata


def test_skill_runtime_rejects_weak_or_cross_run_identity() -> None:
    runner = _Runner()
    stage_identity = GraphStageIdentity(
        run_id=IDENTITY.run_id,
        graph_id=IDENTITY.graph_id,
        graph_version=IDENTITY.graph_version,
        graph_ref=IDENTITY.graph_ref,
        graph_checksum=IDENTITY.graph_checksum,
        node_id=IDENTITY.node_id,
        node_instance_id=IDENTITY.node_instance_id,
    )

    with pytest.raises(TypeError, match="GraphExecutionIdentity"):
        AgentSkillRuntime(_Registry(), runner).execute_call(
            SkillCall(skill_name="reader"),
            agent_run_id=IDENTITY.run_id,
            execution_identity=stage_identity,  # type: ignore[arg-type]
        )

    other = GraphExecutionIdentity(
        **{**IDENTITY.to_dict(), "run_id": "other-run"}
    )
    with pytest.raises(ValueError, match="agent_run_id"):
        AgentSkillRuntime(_Registry(), runner).execute_call(
            SkillCall(skill_name="reader"),
            agent_run_id=IDENTITY.run_id,
            execution_identity=other,
        )


def test_skill_context_builder_rejects_identity_run_mismatch() -> None:
    with pytest.raises(ValueError, match="agent_run_id"):
        _build_skill_run_context(
            skill_name="reader",
            agent_run_id="other-run",
            call_id="call-3",
            reason=None,
            execution_identity=IDENTITY,
        )


def test_agent_loop_skill_handler_forwards_execution_identity() -> None:
    runner = _Runner()
    runtime = AgentSkillRuntime(_Registry(), runner)
    loop = AgentLoop(
        llm_client=object(),
        tool_executor=ToolExecutor(ToolRegistry()),
        agent_skill_runtime=runtime,
    )
    observations = []

    loop._handle_skill_action(
        action=SkillCall(skill_name="reader", call_id="call-4"),
        agent_run_id=IDENTITY.run_id,
        execution_identity=IDENTITY,
        skill_observations=observations,
    )

    assert runner.context.metadata["graph_identity"] == IDENTITY.to_dict()
