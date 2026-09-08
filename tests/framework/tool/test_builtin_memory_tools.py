from __future__ import annotations

import pytest

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolCall, ToolExecutor, ToolPolicy, ToolRegistry, ToolStatus
from framework.tool.builtin.memory import register_memory_tools


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-memory",
        graph_id="research.graph",
        graph_version="1",
        graph_ref="research.graph@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="recall",
        node_instance_id="recall:1",
        activity_id="activity-memory",
        attempt=1,
    )


class _Runtime:
    def __init__(self) -> None:
        self.queries: list[dict[str, object]] = []

    def recall(self, query: dict[str, object]) -> dict[str, object]:
        self.queries.append(dict(query))
        return {"results": [], "result_count": 0}


class _RecallPort(_Runtime):
    execution_identity = _identity()

    def validate_execution(self, execution_identity):
        if execution_identity != self.execution_identity:
            raise ValueError("memory caller conflicts with Graph identity")


def test_memory_tools_require_graph_identity_or_explicit_standalone() -> None:
    with pytest.raises(ValueError, match="GraphExecutionIdentity"):
        register_memory_tools(ToolRegistry(), memory_runtime=_Runtime())

    registry = ToolRegistry()
    register_memory_tools(registry, memory_runtime=_Runtime(), standalone=True)
    assert {tool.name for tool in registry.list_tools()} == {
        "memory.recall",
        "memory.explain",
    }


def test_graph_memory_recall_is_bound_to_exact_identity() -> None:
    runtime = _RecallPort()
    identity = _identity()
    registry = ToolRegistry()
    register_memory_tools(
        registry,
        memory_recall=runtime,
        execution_identity=identity,
    )
    assert registry.require("memory.recall").graph_identity == identity
    executor = ToolExecutor(registry, graph_identity=identity)
    policy = ToolPolicy(allowed_tools=["memory.recall"])

    observation = executor.execute(
        ToolCall(
            tool_name="memory.recall",
            arguments={
                "query": "graph-bound",
                "filters": {"topic": "research"},
            },
        ),
        policy,
    )

    assert observation.status is ToolStatus.SUCCEEDED
    filters = runtime.queries[-1]["filters"]
    assert isinstance(filters, dict)
    assert "run_id" not in filters
    assert "graph_checksum" not in filters
    assert filters["topic"] == "research"

    conflicting = executor.execute(
        ToolCall(
            tool_name="memory.recall",
            arguments={
                "query": "cross-run",
                "filters": {"run_id": "other-run"},
            },
        ),
        policy,
    )
    assert conflicting.status is ToolStatus.FAILED
    assert "conflicts with Graph identity" in (
        conflicting.result.error_message or ""
    )


@pytest.mark.parametrize("argument", ["memory_runtime", "vector_store"])
def test_graph_memory_rejects_mutable_runtime_without_authority(argument):
    with pytest.raises(ValueError, match="execution-bound recall"):
        register_memory_tools(ToolRegistry(), execution_identity=_identity(), **{argument: _Runtime()})


def test_bound_memory_rejects_fallback_and_standalone_identity():
    with pytest.raises(ValueError, match="mutable fallback"):
        register_memory_tools(ToolRegistry(), memory_runtime=_Runtime(), memory_recall=_RecallPort(), execution_identity=_identity())
    with pytest.raises(ValueError, match="standalone"):
        register_memory_tools(ToolRegistry(), memory_runtime=_Runtime(), execution_identity=_identity(), standalone=True)
    with pytest.raises(ValueError, match="collection"):
        register_memory_tools(ToolRegistry(), memory_recall=_RecallPort(), execution_identity=_identity(), default_collection="other")


def test_graph_tool_executor_cannot_use_standalone_memory_registration():
    mutable = _Runtime()
    registry = ToolRegistry()
    register_memory_tools(registry, memory_runtime=mutable, standalone=True)
    observation = ToolExecutor(registry, graph_identity=_identity()).execute(
        ToolCall(tool_name="memory.recall", arguments={"query": "secret"}),
        ToolPolicy(allowed_tools=["memory.recall"]),
    )
    assert observation.status is not ToolStatus.SUCCEEDED
    assert "execution-bound registration" in observation.result.error_message
    assert mutable.queries == []
