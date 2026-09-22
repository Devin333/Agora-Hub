from __future__ import annotations

import json
from threading import Lock
from typing import Any

import pytest

from framework.agent.loop import AgentLoop, AgentRunner
from framework.agent.models import AgentLoopPolicy, AgentSpec
from framework.llm import FakeLLMClient
from framework.shared.attempts import AttemptIdentity, AttemptOutcome
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import (
    ToolBatchExecutor,
    ToolCall,
    ToolDefinition,
    ToolEvidencePersistenceError,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolRunnerTurnEvidence,
    ToolSideEffect,
    ToolStatus,
)


class _RecordingEvidence:
    is_durable = True

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.events: list[tuple[str, Any]] = []
        self._lock = Lock()

    def _record(self, name: str, value: Any) -> None:
        with self._lock:
            self.events.append((name, value))
        if self.fail == name:
            raise OSError(f"injected {name} failure")

    def commit_runner_turn(self, turn) -> ToolRunnerTurnEvidence:
        self._record("runner_turn", dict(turn))
        iteration = int(turn["iteration"])
        return ToolRunnerTurnEvidence(
            ref=f"tool-runner-turn://attempt/turn-{iteration}",
            checksum="sha256:" + f"{iteration:064x}",
        )

    def register_call(self, call: ToolCall) -> None:
        self._record("register", call)

    def bind_tool(self, call: ToolCall, definition: ToolDefinition) -> None:
        self._record("bind", (call, definition))

    def admit_attempt(self, call: ToolCall, identity: AttemptIdentity) -> None:
        self._record("admit", (call, identity))

    def record_attempt_terminal(
        self,
        call: ToolCall,
        outcome: AttemptOutcome[Any],
    ) -> None:
        self._record("terminal", (call, outcome))

    def commit_observation(
        self,
        observation,
        definition: ToolDefinition | None,
    ) -> str:
        self._record("observation", (observation, definition))
        return f"tool-evidence://{observation.call.call_id}"


def _registry(handler, *, attempts: int = 1) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="sample.read",
            input_schema={
                "properties": {"value": {}},
                "additionalProperties": False,
            },
            side_effect=ToolSideEffect.READ_ONLY,
            concurrency_safe=True,
            max_attempts=attempts,
        ),
        handler,
    )
    return registry


def _policy(**overrides) -> ToolPolicy:
    values = {
        "allowed_tools": ["sample.read"],
        "require_explicit_allowlist": True,
        "require_approval_for_side_effects": False,
    }
    values.update(overrides)
    return ToolPolicy(**values)


def test_physical_intent_write_failure_prevents_execution_and_retry() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    evidence = _RecordingEvidence(fail="admit")
    executor = ToolExecutor(
        _registry(handler, attempts=3),
        execution_evidence=evidence,
    )

    with pytest.raises(ToolEvidencePersistenceError, match="physical intent"):
        executor.execute(ToolCall(tool_name="sample.read"), _policy())

    assert calls == 0
    assert [name for name, _ in evidence.events] == ["register", "bind", "admit"]
    assert executor.list_records() == []
    assert "tool_started" not in {event.event_type for event in executor.list_events()}


@pytest.mark.parametrize(
    ("failed_write", "expected_events"),
    [
        ("register", ["register"]),
        ("bind", ["register", "bind"]),
    ],
)
def test_pre_attempt_evidence_failure_prevents_physical_execution(
    failed_write: str,
    expected_events: list[str],
) -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    evidence = _RecordingEvidence(fail=failed_write)
    executor = ToolExecutor(_registry(handler), execution_evidence=evidence)

    with pytest.raises(ToolEvidencePersistenceError, match=failed_write):
        executor.execute(ToolCall(tool_name="sample.read"), _policy())

    assert calls == 0
    assert [name for name, _ in evidence.events] == expected_events
    assert executor.list_records() == []


def test_each_physical_retry_is_admitted_and_terminated_before_final_receipt() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient read failure")
        return {"ok": True}

    evidence = _RecordingEvidence()
    observation = ToolExecutor(
        _registry(handler, attempts=2),
        execution_evidence=evidence,
    ).execute(ToolCall(tool_name="sample.read", call_id="retry-call"), _policy())

    names = [name for name, _ in evidence.events]
    assert observation.status is ToolStatus.SUCCEEDED
    assert observation.result.metadata["tool_evidence_ref"] == "tool-evidence://retry-call"
    assert calls == 2
    assert names == [
        "register",
        "bind",
        "admit",
        "terminal",
        "admit",
        "terminal",
        "observation",
    ]
    attempts = [
        value[1].local_attempt_no
        for name, value in evidence.events
        if name == "admit"
    ]
    assert attempts == [1, 2]


def test_terminal_evidence_failure_never_returns_success_or_retries() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    evidence = _RecordingEvidence(fail="terminal")
    observation = ToolExecutor(
        _registry(handler, attempts=3),
        execution_evidence=evidence,
    ).execute(ToolCall(tool_name="sample.read", call_id="terminal-failure"), _policy())

    assert calls == 1
    assert observation.status is ToolStatus.FAILED
    assert observation.result.indeterminate is True
    assert observation.result.metadata["tool_evidence_ref"] == (
        "tool-evidence://terminal-failure"
    )
    assert [name for name, _ in evidence.events].count("admit") == 1
    assert [name for name, _ in evidence.events].count("terminal") == 1


def test_final_receipt_failure_never_returns_unpersisted_success() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    evidence = _RecordingEvidence(fail="observation")
    executor = ToolExecutor(_registry(handler), execution_evidence=evidence)

    with pytest.raises(ToolEvidencePersistenceError, match="commit_observation"):
        executor.execute(
            ToolCall(tool_name="sample.read", call_id="receipt-failure"),
            _policy(),
        )

    assert calls == 1
    assert [name for name, _ in evidence.events] == [
        "register",
        "bind",
        "admit",
        "terminal",
        "observation",
    ]
    assert executor.list_records() == []


def test_policy_rejection_has_receipt_without_physical_attempt() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    evidence = _RecordingEvidence()
    observation = ToolExecutor(
        _registry(handler),
        execution_evidence=evidence,
    ).execute(
        ToolCall(tool_name="sample.read", call_id="blocked-call"),
        ToolPolicy(allowed_tools=[], require_explicit_allowlist=True),
    )

    assert observation.status is ToolStatus.BLOCKED
    assert observation.result.metadata["tool_evidence_ref"] == (
        "tool-evidence://blocked-call"
    )
    assert calls == 0
    assert [name for name, _ in evidence.events] == [
        "register",
        "bind",
        "observation",
    ]


def test_batch_budget_rejection_records_every_call_without_execution() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    evidence = _RecordingEvidence()
    observations = ToolBatchExecutor(
        _registry(handler),
        execution_evidence=evidence,
    ).execute_batch(
        [
            ToolCall(tool_name="sample.read", call_id="batch-1"),
            ToolCall(tool_name="sample.read", call_id="batch-2"),
        ],
        _policy(max_tool_calls_per_iteration=1),
    )

    assert calls == 0
    assert [item.status for item in observations] == [
        ToolStatus.BLOCKED,
        ToolStatus.BLOCKED,
    ]
    assert [
        item.result.metadata["tool_evidence_ref"] for item in observations
    ] == ["tool-evidence://batch-1", "tool-evidence://batch-2"]
    assert [name for name, _ in evidence.events].count("observation") == 2
    assert [name for name, _ in evidence.events].count("admit") == 0


def test_parallel_batch_uses_one_capability_with_distinct_physical_calls() -> None:
    evidence = _RecordingEvidence()
    observations = ToolBatchExecutor(
        _registry(lambda arguments: {"value": arguments["value"]}),
        execution_evidence=evidence,
        max_workers=2,
    ).execute_batch(
        [
            ToolCall(
                tool_name="sample.read",
                call_id="parallel-1",
                arguments={"value": 1},
            ),
            ToolCall(
                tool_name="sample.read",
                call_id="parallel-2",
                arguments={"value": 2},
            ),
        ],
        _policy(max_tool_calls_per_iteration=2),
    )

    assert [item.result.output for item in observations] == [
        {"value": 1},
        {"value": 2},
    ]
    admitted_ids = {
        value[0].call_id
        for name, value in evidence.events
        if name == "admit"
    }
    assert admitted_ids == {"parallel-1", "parallel-2"}
    assert [name for name, _ in evidence.events].count("terminal") == 2
    assert [name for name, _ in evidence.events].count("observation") == 2


def test_quorum_failure_preserves_batch_semantics_and_source_receipts() -> None:
    def handler(arguments):
        if arguments["value"] == "fail":
            raise RuntimeError("expected tool failure")
        return {"value": arguments["value"]}

    evidence = _RecordingEvidence()
    observations = ToolBatchExecutor(
        _registry(handler),
        execution_evidence=evidence,
    ).execute_batch(
        [
            ToolCall(
                tool_name="sample.read",
                call_id="quorum-ok",
                arguments={"value": "ok"},
            ),
            ToolCall(
                tool_name="sample.read",
                call_id="quorum-failed",
                arguments={"value": "fail"},
            ),
        ],
        _policy(max_tool_calls_per_iteration=2),
        mode="quorum",
        quorum=2,
    )

    assert [item.status for item in observations] == [
        ToolStatus.FAILED,
        ToolStatus.FAILED,
    ]
    assert [item.result.error_type for item in observations] == [
        "BatchAggregationError",
        "BatchAggregationError",
    ]
    assert [
        item.result.metadata["source_tool_status"] for item in observations
    ] == [ToolStatus.SUCCEEDED.value, ToolStatus.FAILED.value]
    assert [
        item.result.metadata["source_tool_evidence_ref"] for item in observations
    ] == ["tool-evidence://quorum-ok", "tool-evidence://quorum-failed"]
    assert [item.raw_result.status for item in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
    ]
    assert [
        item.raw_result.metadata["tool_evidence_ref"] for item in observations
    ] == ["tool-evidence://quorum-ok", "tool-evidence://quorum-failed"]
    assert [name for name, _ in evidence.events].count("admit") == 2
    assert [name for name, _ in evidence.events].count("observation") == 2


def test_strict_batch_closes_unexecuted_requests_without_invoking_them() -> None:
    physical_values: list[str] = []

    def handler(arguments):
        value = str(arguments["value"])
        physical_values.append(value)
        if value == "fail":
            raise RuntimeError("expected strict failure")
        return {"value": value}

    evidence = _RecordingEvidence()
    observations = ToolBatchExecutor(
        _registry(handler),
        execution_evidence=evidence,
    ).execute_batch(
        [
            ToolCall(
                tool_name="sample.read",
                call_id="strict-ok",
                arguments={"value": "ok"},
            ),
            ToolCall(
                tool_name="sample.read",
                call_id="strict-failed",
                arguments={"value": "fail"},
            ),
            ToolCall(
                tool_name="sample.read",
                call_id="strict-skipped",
                arguments={"value": "must-not-run"},
            ),
        ],
        _policy(max_tool_calls_per_iteration=3),
        mode="strict",
    )

    assert physical_values == ["ok", "fail"]
    assert [item.status for item in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
        ToolStatus.BLOCKED,
    ]
    skipped = observations[-1]
    assert skipped.result.metadata["tool_evidence_ref"] == (
        "tool-evidence://strict-skipped"
    )
    assert skipped.result.error_type == "ToolBatchNotInvoked"
    assert skipped.result.metadata["execution_disposition"] == "not_invoked"
    assert "stopped before" in (skipped.result.error_message or "")
    assert [name for name, _ in evidence.events].count("register") == 3
    assert [name for name, _ in evidence.events].count("admit") == 2
    assert [name for name, _ in evidence.events].count("observation") == 3


def test_controlled_batch_rejects_untracked_compensating_effects() -> None:
    evidence = _RecordingEvidence()

    with pytest.raises(ValueError, match="compensating effects"):
        ToolBatchExecutor(
            _registry(lambda _arguments: {"ok": True}),
            execution_evidence=evidence,
            compensating_actions={"sample.read": lambda _observation: None},
        )

    assert evidence.events == []


def test_executor_without_evidence_preserves_default_execution_path() -> None:
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    observation = ToolExecutor(_registry(handler)).execute(
        ToolCall(tool_name="sample.read", call_id="ordinary-call"),
        _policy(),
    )

    assert calls == 1
    assert observation.status is ToolStatus.SUCCEEDED
    assert "tool_evidence_ref" not in observation.result.metadata


def test_agent_loop_requires_the_executor_evidence_capability_pair() -> None:
    evidence = _RecordingEvidence()

    with pytest.raises(ValueError, match="same tool evidence capability"):
        AgentLoop(
            llm_client=FakeLLMClient([]),
            tool_executor=ToolExecutor(
                _registry(lambda _arguments: {"ok": True}),
                execution_evidence=evidence,
            ),
        )


@pytest.mark.parametrize(
    ("ref", "checksum", "message"),
    [
        (" turn://leading-space", "sha256:" + "a" * 64, "reference"),
        ("turn://ok", "a" * 64, "canonical sha256"),
        ("turn://ok", "sha256:" + "A" * 64, "canonical sha256"),
    ],
)
def test_runner_turn_evidence_requires_canonical_identity(
    ref: str,
    checksum: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ToolRunnerTurnEvidence(ref=ref, checksum=checksum)


def test_agent_runner_commits_turn_before_registering_tool_call() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-tool-evidence",
        graph_id="graph-tool-evidence",
        graph_version="1",
        graph_ref="graph-tool-evidence@1",
        graph_checksum="sha256:" + "a" * 64,
        node_id="node-tool-evidence",
        node_instance_id="node-tool-evidence:1",
        activity_id="activity-tool-evidence",
        attempt=1,
    )
    llm = FakeLLMClient(
        [
            json.dumps(
                {
                    "action_type": "tool_call",
                    "tool_name": "sample.read",
                    "tool_args": {},
                }
            ),
            json.dumps({"action_type": "final", "content": "done"}),
        ]
    )
    evidence = _RecordingEvidence()
    agent = AgentSpec(
        agent_id="evidence-agent",
        name="Evidence Agent",
        instructions="Use the tool, then finish.",
        allowed_tools=["sample.read"],
        memory_enabled=False,
        loop_policy=AgentLoopPolicy(max_iterations=2),
    )

    result = AgentRunner(
        llm_client=llm,
        tool_registry=_registry(lambda _arguments: {"ok": True}),
    ).run(
        agent,
        {"run_id": identity.run_id},
        run_id=identity.run_id,
        graph_id=identity.graph_id,
        graph_version=identity.graph_version,
        graph_ref=identity.graph_ref,
        graph_checksum=identity.graph_checksum,
        node_id=identity.node_id,
        node_instance_id=identity.node_instance_id,
        graph_checkpoint_ref="graph-checkpoint://tool-evidence",
        activity_id=identity.activity_id,
        attempt=identity.attempt,
        tool_execution_evidence=evidence,
    )

    assert result.success is True
    assert [name for name, _ in evidence.events][:2] == [
        "runner_turn",
        "register",
    ]
    registered = next(value for name, value in evidence.events if name == "register")
    assert registered.metadata["runner_turn_ref"] == (
        "tool-runner-turn://attempt/turn-1"
    )
    assert registered.metadata["runner_turn_checksum"] == "sha256:" + f"{1:064x}"
    assert registered.metadata["runner_turn_ref"] != registered.metadata[
        "runner_turn_checksum"
    ]
    assert registered.metadata["call_ordinal"] == 0


def test_agent_runner_turn_write_failure_prevents_tool_execution() -> None:
    identity = GraphExecutionIdentity(
        run_id="run-turn-write-failure",
        graph_id="graph-tool-evidence",
        graph_version="1",
        graph_ref="graph-tool-evidence@1",
        graph_checksum="sha256:" + "b" * 64,
        node_id="node-tool-evidence",
        node_instance_id="node-tool-evidence:1",
        activity_id="activity-tool-evidence",
        attempt=1,
    )
    llm = FakeLLMClient(
        [
            json.dumps(
                {
                    "action_type": "tool_call",
                    "tool_name": "sample.read",
                    "tool_args": {},
                }
            )
        ]
    )
    evidence = _RecordingEvidence(fail="runner_turn")
    calls = 0

    def handler(_arguments):
        nonlocal calls
        calls += 1
        return {"ok": True}

    agent = AgentSpec(
        agent_id="evidence-agent",
        name="Evidence Agent",
        instructions="Use the tool.",
        allowed_tools=["sample.read"],
        memory_enabled=False,
        loop_policy=AgentLoopPolicy(max_iterations=1),
    )

    with pytest.raises(ToolEvidencePersistenceError, match="runner turn"):
        AgentRunner(
            llm_client=llm,
            tool_registry=_registry(handler),
        ).run(
            agent,
            {"run_id": identity.run_id},
            run_id=identity.run_id,
            graph_id=identity.graph_id,
            graph_version=identity.graph_version,
            graph_ref=identity.graph_ref,
            graph_checksum=identity.graph_checksum,
            node_id=identity.node_id,
            node_instance_id=identity.node_instance_id,
            graph_checkpoint_ref="graph-checkpoint://tool-evidence",
            activity_id=identity.activity_id,
            attempt=identity.attempt,
            tool_execution_evidence=evidence,
        )

    assert calls == 0
    assert [name for name, _ in evidence.events] == ["runner_turn"]
