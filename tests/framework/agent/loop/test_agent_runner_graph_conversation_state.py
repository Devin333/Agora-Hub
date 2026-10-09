from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from framework.agent.loop.prompt import PreparedInputProjection
from framework.agent.loop.runner import AgentRunner
from framework.agent.models import (
    AgentLoopMetrics,
    AgentLoopResult,
    AgentLoopStatus,
    AgentSpec,
)
from framework.agent.models.action import DelegateBatchCandidate, DelegateBatchProposal
from framework.agent.models.orchestration import (
    AgentOrchestrationPort,
    AgentOrchestrationRequest,
    AgentOrchestrationResult,
    ParentObservation,
    ParentObservationLimits,
)
from framework.agent.messages import AgentMessageRecord, CONVERSATION_SCOPE_GRAPH
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolRegistry
from infrastructure.storage.conversation import LocalJsonConversationStore


runner_module = import_module("framework.agent.loop.runner")


class _AcceptedLoop:
    observed_inputs: list[dict[str, Any]] = []

    def __init__(self, **_: Any) -> None:
        pass

    def run(
        self,
        agent: AgentSpec,
        inputs: dict[str, Any],
        tools: list[dict[str, Any]],
        *,
        run_id: str | None = None,
        execution_identity: Any | None = None,
        graph_checkpoint_ref: str | None = None,
        standalone: bool = False,
    ) -> AgentLoopResult:
        _ = (
            agent,
            tools,
            run_id,
            execution_identity,
            graph_checkpoint_ref,
            standalone,
        )
        self.observed_inputs.append(dict(inputs))
        return AgentLoopResult(
            success=True,
            status=AgentLoopStatus.ACCEPTED,
            output={"answer": "accepted"},
            iterations=2,
            metrics=AgentLoopMetrics(iterations=2),
            trace={"summary": {"iteration_count": 2}},
            termination_reason="final_output_accepted",
        )


def test_agent_runner_passes_execution_environment_to_tool_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingToolExecutor:
        def __init__(self, *_args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(runner_module, "ToolExecutor", _CapturingToolExecutor)
    monkeypatch.setattr(runner_module, "AgentLoop", _AcceptedLoop)
    environment = object()
    runner = AgentRunner(
        llm_client=object(),
        tool_registry=ToolRegistry(),
        execution_environment=environment,
    )
    agent = AgentSpec(
        agent_id="analyst",
        name="Analyst",
        instructions="Return a bounded answer.",
    )

    runner.run(agent, {"query": "inspect"}, standalone=True)

    assert captured["execution_environment"] is environment


def test_agent_runner_passes_opted_in_prepared_projection_to_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _ProjectionAwareLoop(_AcceptedLoop):
        def run(
            self,
            agent: AgentSpec,
            inputs: dict[str, Any],
            tools: list[dict[str, Any]],
            *,
            run_id: str | None = None,
            execution_identity: Any | None = None,
            graph_checkpoint_ref: str | None = None,
            standalone: bool = False,
            prepared_input_projection: PreparedInputProjection | None = None,
        ) -> AgentLoopResult:
            captured["prepared_input_projection"] = prepared_input_projection
            return super().run(
                agent,
                inputs,
                tools,
                run_id=run_id,
                execution_identity=execution_identity,
                graph_checkpoint_ref=graph_checkpoint_ref,
                standalone=standalone,
            )

    monkeypatch.setattr(runner_module, "AgentLoop", _ProjectionAwareLoop)
    projection = PreparedInputProjection(
        projection_id="test.prepared-input",
        projection_revision="1",
        payload={"objective": "Inspect the accepted source"},
    )
    runner = AgentRunner(llm_client=object(), tool_registry=ToolRegistry())
    agent = AgentSpec(
        agent_id="analyst",
        name="Analyst",
        instructions="Return a bounded answer.",
    )

    runner.run(
        agent,
        {"query": "inspect"},
        standalone=True,
        prepared_input_projection=projection,
    )

    assert captured["prepared_input_projection"] is projection


def test_agent_runner_rejects_invalid_prepared_projection_before_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "AgentLoop", _AcceptedLoop)
    runner = AgentRunner(llm_client=object(), tool_registry=ToolRegistry())
    agent = AgentSpec(
        agent_id="analyst",
        name="Analyst",
        instructions="Return a bounded answer.",
    )

    with pytest.raises(TypeError, match="PreparedInputProjection"):
        runner.run(
            agent,
            {"query": "inspect"},
            standalone=True,
            prepared_input_projection=object(),  # type: ignore[arg-type]
        )


def test_agent_runner_injects_orchestration_port_into_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _CapturingLoop(_AcceptedLoop):
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(runner_module, "AgentLoop", _CapturingLoop)
    port = object()
    runner = AgentRunner(
        llm_client=object(),
        tool_registry=ToolRegistry(),
        orchestration_port=port,
        orchestration_enabled=True,
    )
    agent = AgentSpec(
        agent_id="analyst",
        name="Analyst",
        instructions="Return a bounded answer.",
    )

    runner.run(agent, {"query": "inspect"}, standalone=True)

    assert captured["orchestration_port"] is port
    assert captured["orchestration_enabled"] is True


@pytest.fixture
def graph_runner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> tuple[AgentRunner, LocalJsonConversationStore, AgentSpec]:
    _AcceptedLoop.observed_inputs = []
    monkeypatch.setattr(runner_module, "AgentLoop", _AcceptedLoop)
    store = LocalJsonConversationStore(tmp_path)
    runner = AgentRunner(
        llm_client=object(),
        tool_registry=ToolRegistry(),
        conversation_store=store,
    )
    agent = AgentSpec(
        agent_id="analyst",
        name="Analyst",
        instructions="Return a bounded answer.",
    )
    return runner, store, agent


def test_agent_runner_persists_versioned_graph_outer_identity(
    graph_runner,
) -> None:
    runner, store, agent = graph_runner

    runner.run(
        agent,
        {"query": "inspect"},
        conversation_id="conversation-1",
        run_id="run-1",
        graph_id="test.graph",
        graph_version="1",
        graph_ref="test.graph@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        graph_checkpoint_ref="checkpoint://run-1/7",
        activity_id="activity-1",
        attempt=1,
    )

    cursor = store.read_cursor("conversation-1")
    checkpoint = store.read_iteration_checkpoint("conversation-1")
    assert cursor is not None
    assert checkpoint is not None
    assert (
        cursor.run_id,
        cursor.node_instance_id,
        cursor.graph_checkpoint_ref,
    ) == ("run-1", "analyze:1", "checkpoint://run-1/7")
    assert (
        checkpoint.run_id,
        checkpoint.node_instance_id,
        checkpoint.graph_checkpoint_ref,
    ) == ("run-1", "analyze:1", "checkpoint://run-1/7")
    assert cursor.schema_version == "newsroom.graph-conversation-cursor/v2"
    assert (
        checkpoint.schema_version
        == "newsroom.graph-agent-iteration-checkpoint/v2"
    )
    assert "workflow_checkpoint_id" not in cursor.to_dict()
    assert "step_id" not in checkpoint.to_dict()


def test_agent_runner_does_not_masquerade_standalone_run_as_graph_identity(
    graph_runner,
) -> None:
    runner, store, agent = graph_runner

    runner.run(
        agent,
        {"query": "inspect"},
        conversation_id="conversation-standalone",
        run_id="standalone-run",
        standalone=True,
    )

    cursor = store.read_cursor("conversation-standalone")
    checkpoint = store.read_iteration_checkpoint("conversation-standalone")
    assert cursor is not None
    assert checkpoint is not None
    assert (cursor.run_id, cursor.node_instance_id, cursor.graph_checkpoint_ref) == (
        None,
        None,
        None,
    )
    assert (
        checkpoint.run_id,
        checkpoint.node_instance_id,
        checkpoint.graph_checkpoint_ref,
    ) == (None, None, None)


def test_agent_runner_rejects_graph_identity_declared_as_standalone(
    graph_runner,
) -> None:
    runner, _, agent = graph_runner

    with pytest.raises(ValueError, match="standalone.*cannot carry Graph identity"):
        runner.run(
            agent,
            {"query": "inspect"},
            conversation_id="conversation-invalid-scope",
            run_id="run-1",
            graph_id="test.graph",
            graph_version="1",
            graph_ref="test.graph@1",
            graph_checksum="sha256:" + "a" * 64,
            node_id="analyze",
            node_instance_id="analyze:1",
            graph_checkpoint_ref="checkpoint://run-1/7",
            activity_id="activity-1",
            attempt=1,
            standalone=True,
        )


def test_agent_runner_injects_only_matching_graph_resume_context(
    graph_runner,
) -> None:
    runner, _, agent = graph_runner
    identity = {
        "run_id": "run-1",
        "graph_id": "test.graph",
        "graph_version": "1",
        "graph_ref": "test.graph@1",
        "graph_checksum": "sha256:" + "a" * 64,
        "node_id": "analyze",
        "node_instance_id": "analyze:1",
        "graph_checkpoint_ref": "checkpoint://run-1/7",
        "activity_id": "activity-1",
        "attempt": 1,
    }
    runner.run(
        agent,
        {"query": "first"},
        conversation_id="conversation-resume",
        **identity,
    )

    runner.run(
        agent,
        {"query": "resume"},
        conversation_id="conversation-resume",
        resume_from_cursor=True,
        **identity,
    )

    resumed_inputs = _AcceptedLoop.observed_inputs[-1]
    assert resumed_inputs["conversation_cursor"]["node_instance_id"] == "analyze:1"
    assert (
        resumed_inputs["agent_iteration_checkpoint"]["graph_checkpoint_ref"]
        == "checkpoint://run-1/7"
    )

    with pytest.raises(ValueError, match="Graph identity mismatch"):
        runner.run(
            agent,
            {"query": "forged resume"},
            conversation_id="conversation-resume",
            run_id="run-1",
            graph_id="test.graph",
            graph_version="1",
            graph_ref="test.graph@1",
            graph_checksum="sha256:" + "a" * 64,
            node_id="analyze",
            node_instance_id="other:1",
            graph_checkpoint_ref="checkpoint://run-1/7",
            activity_id="activity-1",
            attempt=1,
            resume_from_cursor=True,
        )


def test_agent_runner_rejects_partial_graph_identity(graph_runner) -> None:
    runner, _, agent = graph_runner

    with pytest.raises(ValueError, match="requires run_id, node_instance_id"):
        runner.run(
            agent,
            {"query": "inspect"},
            conversation_id="conversation-invalid",
            run_id="run-1",
            node_instance_id="analyze:1",
        )

    with pytest.raises(ValueError, match="graph_checkpoint_ref"):
        runner.run(
            agent,
            {"query": "inspect"},
            conversation_id="conversation-invalid",
            run_id="run-1",
            node_instance_id="analyze:1",
            graph_checkpoint_ref="checkpoint://run-1/1\nforged",
        )


@pytest.mark.parametrize(
    "recovery_only",
    (False, True),
    ids=("enabled-new-groups", "disabled-new-groups-recovery-only"),
)
def test_agent_runner_resumes_pending_parent_turn_without_llm_or_child_rerun(
    tmp_path,
    recovery_only: bool,
) -> None:
    class _Port:
        def __init__(self) -> None:
            self.dispatch_calls = 0
            self.recovery_calls = 0

        @staticmethod
        def _result(
            request: AgentOrchestrationRequest,
        ) -> AgentOrchestrationResult:
            assert request.parent_turn_id == "parent-turn-1"
            return AgentOrchestrationResult(
                status="succeeded",
                observation=ParentObservation(
                    group_id="group-1",
                    group_status="succeeded",
                    plan_version="1",
                    task_summaries=(),
                ),
            )

        def dispatch(
            self,
            request: AgentOrchestrationRequest,
        ) -> AgentOrchestrationResult:
            self.dispatch_calls += 1
            return self._result(request)

    class _RecoveryPort(_Port):
        def dispatch(
            self,
            _request: AgentOrchestrationRequest,
        ) -> AgentOrchestrationResult:  # pragma: no cover - must stay fenced
            self.dispatch_calls += 1
            raise AssertionError("disabled rollout must not admit a new group")

        def recover_submission(
            self,
            request: AgentOrchestrationRequest,
        ) -> AgentOrchestrationResult:
            self.recovery_calls += 1
            return self._result(request)

    class _NoLLM:
        def complete(self, _request):  # pragma: no cover - should never execute
            raise AssertionError("pending continuation must not call the model")

    identity = GraphExecutionIdentity(
        run_id="run-1",
        graph_id="test.graph",
        graph_version="1",
        graph_ref="test.graph@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        activity_id="activity-1",
        attempt=1,
    )
    candidate = DelegateBatchCandidate(
        correlation_id="turn-1",
        tasks=(
            DelegateBatchProposal(
                logical_task_id="task-1",
                objective="inspect",
                capability_hint="research.inspect@1",
                input_refs=("document",),
                output_role="analysis",
            ),
        ),
    )
    request = AgentOrchestrationRequest(
        parent_agent_id="analyst",
        parent_turn_id="parent-turn-1",
        run_id="run-1",
        execution_identity=identity,
        graph_checkpoint_ref="checkpoint://run-1/7",
        policy_ref="parent-policy@1",
        max_tasks_per_group=1,
        parent_observation_limits=ParentObservationLimits(),
        candidate=candidate,
    )
    store = LocalJsonConversationStore(tmp_path / "conversations")
    store.append_message(
        "conversation-1",
        AgentMessageRecord(
            conversation_id="conversation-1",
            role="assistant",
            content={
                "success": False,
                "status": "pending",
                "output": {
                    "orchestration_request": request.to_dict(),
                    "delegate_batch_candidate": candidate.to_dict(),
                },
            },
            scope_kind=CONVERSATION_SCOPE_GRAPH,
            agent_id="analyst",
            run_id=identity.run_id,
            graph_id=identity.graph_id,
            graph_version=identity.graph_version,
            graph_ref=identity.graph_ref,
            graph_checksum=identity.graph_checksum,
            node_id=identity.node_id,
            node_instance_id=identity.node_instance_id,
            graph_checkpoint_ref="checkpoint://run-1/7",
            activity_id=identity.activity_id,
            attempt=identity.attempt,
        ),
    )
    port = _RecoveryPort() if recovery_only else _Port()
    assert isinstance(port, AgentOrchestrationPort)
    runner = AgentRunner(
        llm_client=_NoLLM(),
        tool_registry=ToolRegistry(),
        conversation_store=store,
        orchestration_port=None if recovery_only else port,
        orchestration_enabled=not recovery_only,
        orchestration_recovery_port=port if recovery_only else None,
    )
    agent = AgentSpec(agent_id="analyst", name="Analyst", instructions="inspect")

    result = runner.run(
        agent,
        {"query": "resume"},
        conversation_id="conversation-1",
        resume_from_cursor=True,
        run_id=identity.run_id,
        graph_id=identity.graph_id,
        graph_version=identity.graph_version,
        graph_ref=identity.graph_ref,
        graph_checksum=identity.graph_checksum,
        node_id=identity.node_id,
        node_instance_id=identity.node_instance_id,
        graph_checkpoint_ref="checkpoint://run-1/7",
        activity_id=identity.activity_id,
        attempt=identity.attempt,
    )

    assert result.success is True
    assert result.output["delegate_batch_observation"]["observation_checksum"].startswith("sha256:")
    assert port.dispatch_calls == (0 if recovery_only else 1)
    assert port.recovery_calls == (1 if recovery_only else 0)
