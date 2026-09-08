from __future__ import annotations

import json
from dataclasses import replace

import pytest

from framework.agent.loop import AgentLoop, AgentRunner
from framework.agent.models import AgentLoopPolicy, AgentSpec
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_memory import HarnessMemoryRecallRuntime
from framework.llm import FakeLLMClient
from framework.memory import MemoryKind, MemoryPolicy, MemoryQuery, MemoryRecallResult, MemoryScope
from framework.memory.integrations.agent import AgentMemoryAdapter
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolDefinition, ToolExecutor, ToolPolicy, ToolRegistry
from framework.tool.builtin.memory import register_memory_tools
from tests.framework.harness.test_ref_memory import _setup


def test_runner_injects_authorized_immutable_memory_into_first_llm_request(
    tmp_path,
) -> None:
    root, metadata, reader, _, _, _ = _setup(tmp_path)
    llm = FakeLLMClient([_final_output("used admitted memory")])
    shared_registry = ToolRegistry()
    runtime = HarnessMemoryRecallRuntime(reader)

    result = _runner(llm, shared_registry).run(
        _agent(),
        {"topic": "evidence"},
        memory_recall=runtime,
        **_run_kwargs(root.execution_identity),
    )

    assert result.success is True
    prompt = llm.requests[0].estimated_prompt_text()
    assert "evidence-bound note" in prompt
    assert runtime.execution_identity == root.execution_identity
    assert metadata.exact_ref in runtime.recall("evidence").diagnostics[
        "namespace_refs"
    ]
    assert shared_registry.list_registered_tools() == []


def test_runner_overlay_replaces_mutable_memory_tools_without_mutating_registry(
    tmp_path,
) -> None:
    root, _, reader, _, _, _ = _setup(tmp_path)
    mutable = _MutableRecall()
    shared_registry = ToolRegistry()
    register_memory_tools(
        shared_registry,
        memory_runtime=mutable,
        standalone=True,
    )
    shared_registry.register(
        ToolDefinition(
            name="test.keep",
            description="kept in the execution overlay",
            input_schema={"properties": {}, "additionalProperties": False},
        ),
        lambda _: {"kept": True},
    )
    original_memory_executor = shared_registry.require("memory.recall").executor
    llm = FakeLLMClient(
        [
            json.dumps(
                {
                    "action_type": "tool_call",
                    "tool_name": "memory.recall",
                    "tool_args": {"query": "evidence"},
                }
            ),
            _final_output("done"),
        ]
    )

    result = _runner(llm, shared_registry).run(
        _agent(allowed_tools=["memory.recall"]),
        {"topic": "evidence"},
        memory_recall=HarnessMemoryRecallRuntime(reader),
        **_run_kwargs(root.execution_identity),
    )

    assert result.success is True
    assert mutable.queries == []
    assert shared_registry.require("memory.recall").executor is original_memory_executor
    assert shared_registry.require("memory.recall").graph_identity is None
    assert shared_registry.require("test.keep").executor({}) == {"kept": True}
    assert "evidence-bound note" in llm.requests[1].estimated_prompt_text()


def test_missing_grant_propagates_before_any_llm_request(tmp_path) -> None:
    root, _, reader, _, _, _ = _setup(tmp_path, commit_grant=False)
    llm = FakeLLMClient([_final_output("must not run")])

    with pytest.raises(HarnessValidationError):
        _runner(llm, ToolRegistry()).run(
            _agent(),
            {"topic": "evidence"},
            memory_recall=HarnessMemoryRecallRuntime(reader),
            **_run_kwargs(root.execution_identity),
        )

    assert llm.requests == []


def test_runner_rejects_recall_bound_to_another_attempt_before_llm(tmp_path) -> None:
    root, _, reader, _, _, _ = _setup(tmp_path)
    llm = FakeLLMClient([_final_output("must not run")])
    other_identity = replace(root.execution_identity, attempt=2)

    with pytest.raises(HarnessValidationError):
        _runner(llm, ToolRegistry()).run(
            _agent(),
            {"topic": "evidence"},
            memory_recall=HarnessMemoryRecallRuntime(reader),
            **_run_kwargs(other_identity),
        )

    assert llm.requests == []


def test_orchestration_enabled_runner_requires_even_an_empty_memory_capability() -> None:
    identity = _identity()
    llm = FakeLLMClient([_final_output("must not run")])
    runner = AgentRunner(
        llm_client=llm,
        tool_registry=ToolRegistry(),
        orchestration_enabled=True,
    )

    with pytest.raises(ValueError, match="ExecutionMemoryRecallPort"):
        runner.run(
            _agent(),
            {"topic": "no memory refs"},
            **_run_kwargs(identity),
        )

    assert llm.requests == []


def test_agent_adapter_does_not_add_caller_identity_filters_to_authorized_query() -> None:
    identity = _identity()
    recall = _RecordingRecall(identity)

    AgentMemoryAdapter().before_llm_call(
        agent_id="caller-agent",
        input_text="shared evidence",
        memory_recall=recall,
        execution_identity=identity,
    )

    assert len(recall.queries) == 1
    assert recall.queries[0].filters == {}
    assert recall.queries[0].query == "shared evidence"


def test_reused_runner_does_not_retain_previous_invocation_memory(tmp_path) -> None:
    root, _, reader, _, _, _ = _setup(tmp_path)
    llm = FakeLLMClient([_final_output("first"), _final_output("second")])
    registry = ToolRegistry()
    runner = _runner(llm, registry)
    first = runner.run(
        _agent(), {"topic": "evidence"}, memory_recall=HarnessMemoryRecallRuntime(reader),
        **_run_kwargs(root.execution_identity),
    )
    next_identity = replace(root.execution_identity, activity_id="next-activity", attempt=2)
    second = runner.run(_agent(), {"topic": "different input"}, **_run_kwargs(next_identity))
    assert first.success and second.success
    assert "evidence-bound note" in llm.requests[0].estimated_prompt_text()
    assert "evidence-bound note" not in llm.requests[1].estimated_prompt_text()
    assert llm.requests[1].execution_identity == next_identity
    assert registry.list_registered_tools() == []


def test_recall_integrity_failure_is_not_swallowed_before_llm(tmp_path, monkeypatch) -> None:
    root, _, reader, namespaces, _, _ = _setup(tmp_path)
    llm = FakeLLMClient([_final_output("must not run")])

    def corrupt_payload(ref):
        raise HarnessValidationError("memory checksum corrupt", code="REF_CHECKSUM_MISMATCH")

    monkeypatch.setattr(namespaces, "read", corrupt_payload)
    with pytest.raises(HarnessValidationError) as error:
        _runner(llm, ToolRegistry()).run(
            _agent(), {"topic": "evidence"}, memory_recall=HarnessMemoryRecallRuntime(reader),
            **_run_kwargs(root.execution_identity),
        )
    assert error.value.code == "REF_CHECKSUM_MISMATCH"
    assert llm.requests == []


def test_automatic_recall_respects_narrower_agent_memory_policy(tmp_path) -> None:
    root, _, reader, _, _, _ = _setup(tmp_path)
    result = AgentMemoryAdapter().before_llm_call(
        agent_id="narrow-agent", input_text="evidence",
        memory_recall=HarnessMemoryRecallRuntime(reader), execution_identity=root.execution_identity,
        policy=MemoryPolicy(allowed_scopes=[MemoryScope.SESSION], allowed_kinds=[MemoryKind.SEMANTIC],
                            max_recall_results=1, max_context_tokens=20),
    )
    assert result.result_count == 1
    assert result.query.kinds == [MemoryKind.SEMANTIC]
    assert result.context_block.token_estimate <= 20


@pytest.mark.parametrize("flag", ["memory_enabled", "memory_recall_enabled"])
def test_disabled_automatic_recall_reads_no_namespace_and_adds_no_context(tmp_path, monkeypatch, flag):
    root, _, reader, namespaces, _, _ = _setup(tmp_path)
    llm = FakeLLMClient([_final_output("no recall")])
    agent = _agent()
    if flag == "memory_enabled":
        agent = replace(agent, memory_enabled=False)
    else:
        agent = replace(agent, loop_policy=replace(agent.loop_policy, memory_recall_enabled=False))
    monkeypatch.setattr(namespaces, "describe", lambda ref: pytest.fail("disabled recall read metadata"))
    monkeypatch.setattr(namespaces, "read", lambda ref: pytest.fail("disabled recall read payload"))
    result = _runner(llm, ToolRegistry()).run(
        agent, {"topic": "no recall"}, memory_recall=HarnessMemoryRecallRuntime(reader),
        **_run_kwargs(root.execution_identity),
    )
    assert result.success
    assert "evidence-bound note" not in llm.requests[0].estimated_prompt_text()
    if flag == "memory_enabled":
        assert result.memory_candidates == []


def test_direct_graph_loop_cannot_reuse_standalone_memory_tool():
    mutable = _MutableRecall()
    registry = ToolRegistry()
    register_memory_tools(registry, memory_runtime=mutable, standalone=True)
    llm = FakeLLMClient([json.dumps({"action_type": "tool_call", "tool_name": "memory.recall", "tool_args": {"query": "secret"}})])
    identity = _identity()
    agent = _agent(allowed_tools=["memory.recall"])
    agent = replace(agent, loop_policy=replace(agent.loop_policy, max_iterations=1, stop_on_tool_error=True))
    result = AgentLoop(llm_client=llm, tool_executor=ToolExecutor(registry, graph_identity=identity)).run(
        agent, {"topic": "read"}, registry.export_schema_for_llm(agent.agent_id, agent.resolved_tool_policy()),
        run_id=identity.run_id, execution_identity=identity, graph_checkpoint_ref="checkpoint://direct-memory",
    )
    assert not result.success
    assert mutable.queries == []
    assert llm.call_count == 1


def test_runner_tool_inherits_memory_policy_and_can_run_with_auto_recall_disabled(tmp_path, monkeypatch):
    root, _, reader, _, _, _ = _setup(tmp_path)
    policy = MemoryPolicy(allowed_scopes=[MemoryScope.SESSION], allowed_kinds=[MemoryKind.SEMANTIC],
                          max_recall_results=1, max_context_tokens=16)
    runtime = HarnessMemoryRecallRuntime(reader)
    results = []
    original = runtime.recall

    def recall(query, *, policy=None):
        result = original(query, policy=policy)
        results.append((policy, result))
        return result

    monkeypatch.setattr(runtime, "recall", recall)
    llm = FakeLLMClient([
        json.dumps({"action_type": "tool_call", "tool_name": "memory.recall", "tool_args": {
            "query": "evidence", "limit": 100, "max_context_tokens": 100_000,
        }}), _final_output("bounded tool memory"),
    ])
    agent = _agent(allowed_tools=["memory.recall"])
    agent = replace(agent, loop_policy=replace(agent.loop_policy, memory_recall_enabled=False))
    result = AgentRunner(llm_client=llm, tool_registry=ToolRegistry(), memory_policy=policy).run(
        agent, {"topic": "evidence"}, memory_recall=runtime, **_run_kwargs(root.execution_identity),
    )
    assert result.success
    assert len(results) == 1
    assert results[0][0] is policy
    assert results[0][1].query.limit == 1
    assert results[0][1].query.kinds == [MemoryKind.SEMANTIC]
    assert results[0][1].context_block.token_estimate <= 16


def test_memory_enabled_false_hides_and_blocks_memory_tools(tmp_path, monkeypatch):
    root, _, reader, namespaces, _, _ = _setup(tmp_path)
    monkeypatch.setattr(namespaces, "read", lambda ref: pytest.fail("disabled memory tool read payload"))
    llm = FakeLLMClient([json.dumps({"action_type": "tool_call", "tool_name": "memory.recall", "tool_args": {"query": "secret"}})])
    agent = _agent(allowed_tools=["memory.recall"])
    agent = replace(agent, memory_enabled=False, loop_policy=replace(agent.loop_policy, max_iterations=1, stop_on_tool_error=True))
    result = _runner(llm, ToolRegistry()).run(
        agent, {"topic": "no memory"}, memory_recall=HarnessMemoryRecallRuntime(reader),
        **_run_kwargs(root.execution_identity),
    )
    assert not result.success
    assert "memory.recall" not in str(llm.requests[0].tools)
    assert result.memory_candidates == []


def test_standalone_memory_enabled_false_hides_and_blocks_mutable_memory_tools():
    mutable = _MutableRecall()
    registry = ToolRegistry()
    register_memory_tools(registry, memory_runtime=mutable, standalone=True)
    llm = FakeLLMClient([json.dumps({
        "action_type": "tool_call",
        "tool_name": "memory.recall",
        "tool_args": {"query": "secret"},
    })])
    agent = _agent(allowed_tools=["memory.recall"])
    agent = replace(
        agent,
        memory_enabled=False,
        loop_policy=replace(
            agent.loop_policy,
            max_iterations=1,
            stop_on_tool_error=True,
        ),
    )

    result = AgentRunner(llm_client=llm, tool_registry=registry).run(
        agent,
        {"topic": "no memory"},
        standalone=True,
    )

    assert not result.success
    assert "memory.recall" not in str(llm.requests[0].tools)
    assert mutable.queries == []
    assert registry.require("memory.recall").graph_identity is None


def test_empty_policy_allowlists_share_capability_baseline_for_auto_and_tool_recall(
    tmp_path,
    monkeypatch,
):
    root, _, reader, _, _, _ = _setup(tmp_path)
    policy = MemoryPolicy(max_recall_results=1, max_context_tokens=20)
    runtime = HarnessMemoryRecallRuntime(reader)
    calls = []
    original = runtime.recall

    def recall(query, *, policy=None):
        result = original(query, policy=policy)
        calls.append((policy, result))
        return result

    monkeypatch.setattr(runtime, "recall", recall)
    llm = FakeLLMClient([
        json.dumps({
            "action_type": "tool_call",
            "tool_name": "memory.recall",
            "tool_args": {"query": "evidence", "limit": 100},
        }),
        _final_output("bounded memory"),
    ])

    result = AgentRunner(
        llm_client=llm,
        tool_registry=ToolRegistry(),
        memory_policy=policy,
    ).run(
        _agent(allowed_tools=["memory.recall"]),
        {"topic": "evidence"},
        memory_recall=runtime,
        **_run_kwargs(root.execution_identity),
    )

    assert result.success
    assert len(calls) == 3
    assert all(item[0] is policy for item in calls)
    assert all(MemoryScope.GLOBAL not in item[1].query.scopes for item in calls)
    assert all(item[1].query.limit == 1 for item in calls)
    assert all(item[1].context_block.token_estimate <= 20 for item in calls)


class _MutableRecall:
    def __init__(self) -> None:
        self.queries: list[object] = []

    def recall(self, query):
        self.queries.append(query)
        return {"results": [], "result_count": 0}


class _RecordingRecall:
    def __init__(self, identity: GraphExecutionIdentity) -> None:
        self._identity = identity
        self.queries: list[MemoryQuery] = []

    @property
    def execution_identity(self) -> GraphExecutionIdentity:
        return self._identity

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None:
        if execution_identity != self._identity:
            raise ValueError("execution mismatch")

    def recall(self, query, *, policy=None) -> MemoryRecallResult:
        assert isinstance(query, MemoryQuery)
        self.queries.append(query)
        return MemoryRecallResult(query=query)


def _runner(llm: FakeLLMClient, registry: ToolRegistry) -> AgentRunner:
    return AgentRunner(llm_client=llm, tool_registry=registry)


def _agent(*, allowed_tools: list[str] | None = None) -> AgentSpec:
    tools = list(allowed_tools or [])
    return AgentSpec(
        agent_id="execution-memory-agent",
        name="Execution Memory Agent",
        instructions="Use only admitted memory and answer.",
        allowed_tools=tools,
        loop_policy=AgentLoopPolicy(max_iterations=3),
        tool_policy=ToolPolicy(
            allowed_tools=tools,
            require_explicit_allowlist=True,
            require_approval_for_side_effects=False,
        ),
    )


def _run_kwargs(identity: GraphExecutionIdentity) -> dict[str, object]:
    return {
        **identity.to_dict(),
        "graph_checkpoint_ref": f"checkpoint://{identity.run_id}/memory",
    }


def _final_output(summary: str) -> str:
    return json.dumps(
        {
            "action_type": "final_output",
            "output": {"output": {"summary": summary}},
        }
    )


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-recording-memory",
        graph_id="research.agent",
        graph_version="1",
        graph_ref="research.agent@1",
        graph_checksum="sha256:" + "b" * 64,
        node_id="analyze",
        node_instance_id="analyze:1",
        activity_id="activity-memory",
        attempt=1,
    )
