from __future__ import annotations

import json
from threading import Barrier, Lock
from typing import Any

from framework.agent.loop import AgentLoop
from framework.agent.models import AgentLoopPolicy, AgentLoopStatus, AgentSpec
from framework.llm import FakeLLMClient, LLMResponse
from framework.llm.models import LLMToolCall
from framework.shared.attempts import AttemptIdentity, AttemptOutcome
from framework.tool import (
    ToolCall,
    ToolDefinition,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolSideEffect,
)
from framework.tool.runtime.evidence import ToolRunnerTurnEvidence

_SCHEMA = {
    "type": "object",
    "properties": {"q": {"type": "string"}},
    "required": ["q"],
    "additionalProperties": False,
}


class _Tools:
    def __init__(self, *, barrier: Barrier | None = None) -> None:
        self.invoked: list[tuple[str, str]] = []
        self._lock = Lock()
        self._barrier = barrier

    def handler(self, name: str, *, fail: bool = False):
        def run(arguments: dict[str, Any]) -> dict[str, Any]:
            if self._barrier is not None:
                self._barrier.wait()
            with self._lock:
                self.invoked.append((name, arguments["q"]))
            if fail:
                raise RuntimeError(f"{name} upstream unavailable")
            return {"hits": [f"{name}:{arguments['q']}"]}

        return run


def _registry(tools: _Tools, *, failing: set[str] = frozenset(), writes: set[str] = frozenset()):
    registry = ToolRegistry()
    for name in ("search.papers", "search.news", "search.projects", "report.publish"):
        registry.register(
            ToolDefinition(
                name=name,
                description=name,
                input_schema=_SCHEMA,
                side_effect=(
                    ToolSideEffect.WRITES_EXTERNAL_STATE
                    if name in writes
                    else ToolSideEffect.READ_ONLY
                ),
                concurrency_safe=name not in writes,
            ),
            tools.handler(name, fail=name in failing),
        )
    return registry


def _agent(
    *,
    allow_parallel: bool = False,
    max_tool_calls_per_agent: int = 20,
    require_approval_for: list[str] | None = None,
) -> AgentSpec:
    names = ["search.papers", "search.news", "search.projects", "report.publish"]
    return AgentSpec(
        agent_id="research-agent",
        name="Research Agent",
        instructions="Search every source, then answer.",
        allowed_tools=names,
        loop_policy=AgentLoopPolicy(max_iterations=4, allow_parallel_tool_calls=allow_parallel),
        tool_policy=ToolPolicy(
            allowed_tools=names,
            require_explicit_allowlist=True,
            require_approval_for_side_effects=False,
            require_approval_for=list(require_approval_for or []),
            max_tool_calls_per_iteration=5,
            max_tool_calls_per_agent=max_tool_calls_per_agent,
        ),
    )


def _tool_turn(*calls: tuple[str, str, str | dict[str, Any]]) -> LLMResponse:
    tool_calls = []
    for call_id, name, arguments in calls:
        if isinstance(arguments, str):
            tool_calls.append(
                LLMToolCall.with_invalid_arguments(
                    tool_call_id=call_id,
                    tool_name=name,
                    raw_arguments=arguments,
                    error="tool call arguments are not valid JSON: Unterminated string",
                )
            )
        else:
            tool_calls.append(LLMToolCall(tool_call_id=call_id, tool_name=name, arguments=arguments))
    return LLMResponse(content="", tool_calls=tool_calls)


def _final(summary: str) -> str:
    return json.dumps({"action_type": "final_output", "output": {"output": {"summary": summary}}})


def _run(llm: FakeLLMClient, registry: ToolRegistry, agent: AgentSpec, **loop_options):
    executor = loop_options.pop("tool_executor", None) or ToolExecutor(registry)
    return AgentLoop(llm_client=llm, tool_executor=executor, **loop_options).run(
        agent,
        {"topic": "agent memory"},
        registry.export_schema_for_llm(agent.agent_id, agent.resolved_tool_policy()),
        run_id="run-native-tools",
        standalone=True,
    )


def _observations_in_prompt(llm: FakeLLMClient, index: int) -> list[dict[str, Any]]:
    user = llm.requests[index].messages[-1]["content"]
    marker = "\nTool observations: "
    payload = user.split(marker, 1)[1].split("\nJudge feedback: ", 1)[0]
    return json.loads(payload)


def test_one_failed_native_tool_call_is_returned_to_the_model_with_its_siblings() -> None:
    tools = _Tools()
    registry = _registry(tools, failing={"search.news"})
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("call_papers", "search.papers", {"q": "memory"}),
                ("call_news", "search.news", {"q": "memory"}),
                ("call_projects", "search.projects", {"q": "memory"}),
            ),
            _final("papers and projects found; news unavailable"),
        ]
    )

    result = _run(llm, registry, _agent())

    assert result.success is True
    assert sorted(tools.invoked) == [
        ("search.news", "memory"),
        ("search.papers", "memory"),
        ("search.projects", "memory"),
    ]
    observations = _observations_in_prompt(llm, 1)
    assert [item["tool_name"] for item in observations] == [
        "search.papers",
        "search.news",
        "search.projects",
    ]
    assert [item["status"] for item in observations] == ["succeeded", "failed", "succeeded"]
    assert [item["call"]["metadata"]["model_tool_call_id"] for item in observations] == [
        "call_papers",
        "call_news",
        "call_projects",
    ]
    feedback = llm.requests[1].messages[-1]["content"].split("\nJudge feedback: ", 1)[1]
    assert feedback.startswith("1 of 3 tool calls did not succeed")
    assert "search.news [call_news] failed: search.news upstream unavailable" in feedback
    first = result.to_dict()["trajectory"][0]
    assert first["parsed_action"]["tool_names"] == [
        "search.papers",
        "search.news",
        "search.projects",
    ]
    assert [item["status"] for item in first["observations"]] == [
        "succeeded",
        "failed",
        "succeeded",
    ]
    assert result.metrics.tool_calls == 3


def test_malformed_native_arguments_reject_only_that_call() -> None:
    tools = _Tools()
    registry = _registry(tools)
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("call_papers", "search.papers", {"q": "memory"}),
                ("call_news", "search.news", '{"q": "mem'),
            ),
            _final("papers only"),
        ]
    )

    result = _run(llm, registry, _agent())

    assert result.success is True
    assert tools.invoked == [("search.papers", "memory")]
    observations = _observations_in_prompt(llm, 1)
    assert [item["status"] for item in observations] == ["succeeded", "blocked"]
    assert observations[1]["error_type"] == "ToolArgumentsInvalid"
    assert observations[1]["result"]["metadata"]["execution_disposition"] == "not_invoked"


def test_parallel_native_calls_run_concurrently_when_the_agent_allows_it() -> None:
    # The barrier only releases when all three handlers are running at once.
    tools = _Tools(barrier=Barrier(3, timeout=5))
    registry = _registry(tools)
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("a", "search.papers", {"q": "x"}),
                ("b", "search.news", {"q": "x"}),
                ("c", "search.projects", {"q": "x"}),
            ),
            _final("done"),
        ]
    )

    result = _run(llm, registry, _agent(allow_parallel=True), tool_batch_max_workers=3)

    assert result.success is True
    assert len(tools.invoked) == 3
    assert [item["status"] for item in _observations_in_prompt(llm, 1)] == ["succeeded"] * 3


def test_agent_without_parallel_permission_runs_native_calls_in_request_order() -> None:
    tools = _Tools()
    registry = _registry(tools)
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("a", "search.projects", {"q": "1"}),
                ("b", "search.papers", {"q": "2"}),
                ("c", "search.news", {"q": "3"}),
            ),
            _final("done"),
        ]
    )

    _run(llm, registry, _agent(allow_parallel=False))

    assert tools.invoked == [
        ("search.projects", "1"),
        ("search.papers", "2"),
        ("search.news", "3"),
    ]


def test_pending_approval_pauses_the_turn_and_leaves_later_calls_uninvoked() -> None:
    tools = _Tools()
    registry = _registry(tools, writes={"report.publish"})
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("a", "search.papers", {"q": "x"}),
                ("b", "report.publish", {"q": "x"}),
                ("c", "search.news", {"q": "x"}),
            ),
        ]
    )

    result = _run(llm, registry, _agent(require_approval_for=["report.publish"]))

    assert result.status == AgentLoopStatus.WAITING_FOR_APPROVAL
    assert tools.invoked == [("search.papers", "x")]
    statuses = [item["status"] for item in result.to_dict()["trajectory"][0]["observations"]]
    assert statuses == ["succeeded", "approval_required", "blocked"]


def test_agent_tool_budget_blocks_the_calls_past_the_limit_in_order() -> None:
    tools = _Tools()
    registry = _registry(tools)
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("a", "search.papers", {"q": "x"}),
                ("b", "search.news", {"q": "x"}),
                ("c", "search.projects", {"q": "x"}),
            ),
            _final("partial"),
        ]
    )

    result = _run(llm, registry, _agent(max_tool_calls_per_agent=2))

    assert result.success is True
    assert sorted(name for name, _ in tools.invoked) == ["search.news", "search.papers"]
    observations = _observations_in_prompt(llm, 1)
    assert [item["status"] for item in observations] == ["succeeded", "succeeded", "blocked"]
    assert "max_tool_calls_per_agent of 2" in observations[2]["result"]["error_message"]


class _RecordingEvidence:
    is_durable = True

    def __init__(self) -> None:
        self.runner_turns: list[dict[str, Any]] = []
        self.registered: list[ToolCall] = []
        self._lock = Lock()

    def commit_runner_turn(self, turn) -> ToolRunnerTurnEvidence:
        with self._lock:
            self.runner_turns.append(dict(turn))
        return ToolRunnerTurnEvidence(
            ref=f"tool-runner-turn://turn-{turn['iteration']}",
            checksum="sha256:" + f"{int(turn['iteration']):064x}",
        )

    def register_call(self, call: ToolCall) -> None:
        with self._lock:
            self.registered.append(call)

    def bind_tool(self, call, definition) -> None:
        return None

    def admit_attempt(self, call, identity: AttemptIdentity) -> None:
        return None

    def record_attempt_terminal(self, call, outcome: AttemptOutcome[Any]) -> None:
        return None

    def commit_observation(self, observation, definition) -> str:
        return f"tool-evidence://{observation.call.call_id}"


def test_native_calls_of_one_turn_share_one_runner_turn_with_distinct_ordinals() -> None:
    tools = _Tools()
    registry = _registry(tools, failing={"search.news"})
    evidence = _RecordingEvidence()
    llm = FakeLLMClient(
        [
            _tool_turn(
                ("a", "search.papers", {"q": "x"}),
                ("b", "search.news", {"q": "x"}),
                ("c", "search.projects", '{"q": '),
            ),
            _final("done"),
        ]
    )

    result = _run(
        llm,
        registry,
        _agent(),
        tool_executor=ToolExecutor(registry, execution_evidence=evidence),
        tool_execution_evidence=evidence,
    )

    assert result.success is True
    assert len(evidence.runner_turns) == 1
    # Evidence identity comes from the ordinal, not from registration order.
    assert sorted(
        (call.metadata["call_ordinal"], call.tool_name) for call in evidence.registered
    ) == [(0, "search.papers"), (1, "search.news"), (2, "search.projects")]
    assert {call.metadata["runner_turn_ref"] for call in evidence.registered} == {
        "tool-runner-turn://turn-1"
    }
