from __future__ import annotations

import json
from dataclasses import replace

import pytest

from framework.agent.loop import AgentActionParser, AgentActionParserError, AgentLoop
from framework.agent.models import AgentLoopPolicy, AgentLoopStopReason, AgentSpec
from framework.agent.models.orchestration import (
    AgentOrchestrationRequest,
    AgentOrchestrationResult,
    ParentObservation,
    ParentObservationLimits,
    ParentTaskSummary,
    ParentWaveSummary,
)
from framework.events.canonical import checksum_for
from framework.llm import FakeLLMClient
from framework.tool import ToolExecutor, ToolRegistry


class _RecordingOrchestrationPort:
    def __init__(self, result: AgentOrchestrationResult) -> None:
        self.result = result
        self.requests: list[AgentOrchestrationRequest] = []

    def dispatch(self, request: AgentOrchestrationRequest) -> AgentOrchestrationResult:
        self.requests.append(request)
        return self.result


def _agent(*, max_tasks: int = 3) -> AgentSpec:
    return AgentSpec(
        agent_id="parent",
        name="Parent",
        instructions="delegate safely",
        loop_policy=AgentLoopPolicy(max_iterations=2, allow_subagents=True),
        metadata={
            "agent_orchestration": {
                "policy_ref": "parent-policy@1",
                "max_tasks_per_group": max_tasks,
                "max_task_summaries": 1,
                "max_summary_bytes": 24,
                "max_diagnostics": 1,
                "max_refs": 1,
                "max_observation_bytes": 1024,
            }
        },
    )


def _batch_action(*, extra: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {
        "action_type": "delegate_batch",
        "schema_version": "newsroom.agent.delegate-batch/v1",
        "correlation_id": "turn-1",
        "tasks": [
            {
                "logical_task_id": "structure",
                "objective": "Analyze structure",
                "capability_hint": "research.structure@1",
                "input_refs": ["document", "evidence_pack"],
                "output_role": "structure",
                "depends_on": [],
            },
            {
                "logical_task_id": "contribution",
                "objective": "Analyze contribution",
                "capability_hint": "research.contribution@1",
                "input_refs": ["document", "evidence_pack"],
                "output_role": "contribution",
                "depends_on": [],
            },
        ],
    }
    payload.update(extra or {})
    return json.dumps(payload)


def _result(*, status: str = "succeeded") -> AgentOrchestrationResult:
    return AgentOrchestrationResult(
        status=status,
        reason_code=(None if status == "succeeded" else "REQUIRED_ROLE_MISSING"),
        observation=ParentObservation(
            group_id="group-1",
            group_status=status,
            plan_version="1",
            task_summaries=(
                ParentTaskSummary(
                    logical_task_id="structure",
                    status="succeeded",
                    summary="secret=sk-abcdefghijklmnopqrstuvwxyz",
                    result_ref="artifact://structure",
                    result_checksum="checksum-structure",
                ),
                ParentTaskSummary(
                    logical_task_id="contribution",
                    status="failed",
                    summary="must be truncated",
                    result_ref="artifact://contribution",
                    result_checksum="checksum-contribution",
                ),
            ),
            diagnostics=("private transcript: sk-abcdefghijklmnopqrstuvwxyz", "extra diagnostic"),
            result_refs=("artifact://structure", "artifact://contribution"),
        ),
    )


def test_parser_accepts_strict_versioned_delegate_batch() -> None:
    action = AgentActionParser().parse(_batch_action())

    assert action.delegate_batch is not None
    assert action.delegate_batch.correlation_id == "turn-1"
    assert [item.logical_task_id for item in action.delegate_batch.tasks] == [
        "structure",
        "contribution",
    ]


@pytest.mark.parametrize(
    "field",
    ["worker_ref", "queue", "tools", "policy", "quality_verdict", "publication", "memory_promotion", "next_step"],
)
def test_parser_rejects_delegate_batch_control_fields(field: str) -> None:
    with pytest.raises(AgentActionParserError, match="forbidden control|unsupported fields"):
        AgentActionParser().parse(_batch_action(extra={field: "forbidden"}))


@pytest.mark.parametrize(
    "field",
    ["worker_ref", "tools", "hidden_context", "next_step", "publication", "memory_promotion"],
)
def test_parser_rejects_child_proposal_control_fields(field: str) -> None:
    payload = json.loads(_batch_action())
    payload["tasks"][0][field] = "forbidden"

    with pytest.raises(AgentActionParserError, match="unsupported fields"):
        AgentActionParser().parse(json.dumps(payload))


def test_agent_loop_submits_one_candidate_and_uses_limited_redacted_observation() -> None:
    port = _RecordingOrchestrationPort(_result())
    loop = AgentLoop(
        llm_client=FakeLLMClient([
            _batch_action(),
            json.dumps({"action_type": "final_output", "output": {"output": "done"}}),
        ]),
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )

    result = loop.run(_agent(), {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is True
    assert len(port.requests) == 1
    assert port.requests[0].policy_ref == "parent-policy@1"
    assert len(port.requests[0].candidate.tasks) == 2
    second_request = loop._llm_client.requests[1]
    assert "group-1" in str(second_request.messages)
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in str(second_request.messages)
    assert "***REDACTED***" in str(second_request.messages)
    assert "contribution" not in str(second_request.messages)


def test_agent_loop_returns_deferred_when_orchestration_is_unavailable() -> None:
    loop = AgentLoop(
        llm_client=FakeLLMClient([_batch_action()]),
        tool_executor=ToolExecutor(ToolRegistry()),
    )

    result = loop.run(_agent(), {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert any("agent_orchestration_unavailable" in str(event) for event in result.events)


def test_parent_turn_identity_is_stable_and_distinct_from_model_correlation() -> None:
    recorded = []
    for _ in range(2):
        port = _RecordingOrchestrationPort(_result())
        loop = AgentLoop(
            llm_client=FakeLLMClient([_batch_action(), _batch_action()]),
            tool_executor=ToolExecutor(ToolRegistry()),
            orchestration_port=port,
            orchestration_enabled=True,
        )
        loop.run(_agent(), {"run_id": "run-1"}, [], run_id="run-1", standalone=True)
        assert len(port.requests) == 2
        assert len({request.candidate.correlation_id for request in port.requests}) == 1
        turns = tuple(request.parent_turn_id for request in port.requests)
        assert turns[0] != turns[1]
        recorded.append(turns)
    assert recorded[0] == recorded[1]


@pytest.mark.parametrize("change", ["missing", "blank", "untrimmed", "too_long", "v1", "unknown"])
def test_orchestration_request_requires_strict_versioned_parent_turn(change: str) -> None:
    request = AgentOrchestrationRequest(
        parent_agent_id="parent", parent_turn_id="parent-turn-1", run_id="run-1",
        execution_identity=None, graph_checkpoint_ref=None, policy_ref="parent-policy@1",
        max_tasks_per_group=3, parent_observation_limits=ParentObservationLimits(),
        candidate=AgentActionParser().parse(_batch_action()).delegate_batch,
    )
    payload = request.to_dict()
    assert AgentOrchestrationRequest.from_dict(payload) == request
    assert payload["schema_version"] == "newsroom.agent-orchestration-request/v2"
    if change == "missing":
        del payload["parent_turn_id"]
    elif change == "v1":
        payload["schema_version"] = "newsroom.agent-orchestration-request/v1"
    elif change == "unknown":
        payload["turn_alias"] = "parent-turn-1"
    else:
        payload["parent_turn_id"] = {"blank": "", "untrimmed": " turn ", "too_long": "t" * 513}[change]
    with pytest.raises(ValueError):
        AgentOrchestrationRequest.from_dict(payload)


@pytest.mark.parametrize("field", ["max_total_bytes", "max_observaton_bytes"])
def test_agent_loop_rejects_noncanonical_observation_policy_before_dispatch(field: str) -> None:
    port = _RecordingOrchestrationPort(_result())
    client = FakeLLMClient([_batch_action()])
    loop = AgentLoop(
        llm_client=client,
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )
    agent = replace(
        _agent(),
        loop_policy=AgentLoopPolicy(max_iterations=1, allow_subagents=True),
        metadata={"agent_orchestration": {"policy_ref": "parent-policy@1", field: 512}},
    )

    result = loop.run(agent, {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert port.requests == []
    assert len(client.requests) == 1
    judge = result.trace["iterations"][0]["judge"]
    assert judge["validation_errors"] == ["delegate_batch_candidate_rejected"]
    assert field in judge["feedback"]


def test_agent_loop_uses_canonical_observation_defaults_when_limits_are_absent() -> None:
    port = _RecordingOrchestrationPort(_result(status="partial_failure"))
    loop = AgentLoop(
        llm_client=FakeLLMClient([_batch_action()]),
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )
    agent = replace(
        _agent(),
        metadata={"agent_orchestration": {"policy_ref": "parent-policy@1"}},
    )

    loop.run(agent, {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert len(port.requests) == 1
    assert port.requests[0].parent_observation_limits == ParentObservationLimits()


def test_agent_loop_exposes_partial_failure_as_observation_not_success() -> None:
    port = _RecordingOrchestrationPort(_result(status="partial_failure"))
    loop = AgentLoop(
        llm_client=FakeLLMClient([_batch_action()]),
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )

    result = loop.run(_agent(), {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert any("partial_failure" in str(event) for event in result.events)
    assert len(port.requests) == 1


@pytest.mark.parametrize("status", ["partial_failure", "cancelled", "indeterminate", "halted"])
def test_failed_group_cannot_be_followed_by_successful_final_output(status: str) -> None:
    port = _RecordingOrchestrationPort(_result(status=status))
    client = FakeLLMClient(
        [
            _batch_action(),
            json.dumps({"action_type": "final_output", "output": {"output": "unsafe"}}),
        ]
    )
    loop = AgentLoop(
        llm_client=client,
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )

    result = loop.run(_agent(), {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert result.output["delegate_batch_observation"]["group_status"] == status
    assert len(client.requests) == 1
    assert len(port.requests) == 1


def test_joined_group_does_not_extend_parent_turn_budget() -> None:
    port = _RecordingOrchestrationPort(_result())
    client = FakeLLMClient([_batch_action(), _batch_action()])
    loop = AgentLoop(
        llm_client=client,
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )
    agent = replace(_agent(), loop_policy=AgentLoopPolicy(max_iterations=1, allow_subagents=True))

    result = loop.run(agent, {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert result.diagnostics is not None
    assert result.diagnostics.stop_reason is AgentLoopStopReason.MAX_ITERATIONS_EXCEEDED
    assert result.max_steps_reached is True
    assert len(client.requests) == 1
    assert len(port.requests) == 1


def test_agent_loop_does_not_submit_batch_over_its_trusted_limit() -> None:
    port = _RecordingOrchestrationPort(_result())
    loop = AgentLoop(
        llm_client=FakeLLMClient([_batch_action()]),
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )

    result = loop.run(_agent(max_tasks=1), {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert port.requests == []
    assert any("exceeds max_tasks_per_group" in str(event) for event in result.events)


def test_parent_observation_round_trip_is_strict_and_checksum_bound() -> None:
    observation = _result().observation
    restored = ParentObservation.from_dict(observation.to_dict())

    assert restored == observation
    with pytest.raises(ValueError, match="unknown fields"):
        ParentObservation.from_dict({**observation.to_dict(), "control": "forbidden"})
    with pytest.raises(ValueError, match="checksum-bound"):
        ParentObservation(
            group_id="group-1",
            group_status="succeeded",
            plan_version="1",
            result_refs=("artifact://unbound",),
        )


def test_parent_observation_projection_respects_total_byte_limit() -> None:
    observation = ParentObservation(
        group_id="group-1",
        group_status="succeeded",
        plan_version="1",
        task_summaries=tuple(
            ParentTaskSummary(
                logical_task_id=f"task-{index}",
                status="succeeded",
                summary="very long safe summary " * 80,
                result_ref=f"artifact://task-{index}",
                result_checksum=f"checksum-{index}",
            )
            for index in range(8)
        ),
        wave_summaries=tuple(
            ParentWaveSummary(wave_id=f"wave-{index}", ordinal=index + 1, status="succeeded")
            for index in range(16)
        ),
        diagnostics=tuple("diagnostic " * 80 for _ in range(16)),
        result_refs=tuple(f"artifact://task-{index}" for index in range(8)),
    )

    projected = observation.project(
        ParentObservationLimits(max_observation_bytes=512)
    )

    assert len(json.dumps(projected, sort_keys=True, separators=(",", ":")).encode("utf-8")) <= 512
    assert projected["observation_checksum"] == checksum_for(
        {key: value for key, value in projected.items() if key != "observation_checksum"}
    )
    assert projected["truncated"] is True


def test_parent_observation_projection_checksum_binds_final_redacted_view() -> None:
    observation = ParentObservation(
        group_id="group-1",
        group_status="succeeded",
        plan_version="1",
        task_summaries=(
            ParentTaskSummary(
                logical_task_id="task-1",
                status="succeeded",
                summary="secret=sk-abcdefghijklmnopqrstuvwxyz",
            ),
        ),
        diagnostics=("private transcript: sk-abcdefghijklmnopqrstuvwxyz",),
    )

    projected = observation.project(ParentObservationLimits(max_summary_bytes=24))
    assert projected["observation_checksum"] == checksum_for(
        {key: value for key, value in projected.items() if key != "observation_checksum"}
    )
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(projected)
    assert projected["observation_checksum"] != "sha256:" + "0" * 64

    forged = dict(projected)
    forged["truncated"] = not forged["truncated"]
    assert forged["observation_checksum"] != checksum_for(
        {key: value for key, value in forged.items() if key != "observation_checksum"}
    )


@pytest.mark.parametrize("max_bytes", [1, 2, 3, 4, 5, 8])
def test_parent_summary_and_diagnostics_respect_small_utf8_byte_limits(max_bytes: int) -> None:
    observation = ParentObservation(
        group_id="group-1",
        group_status="succeeded",
        plan_version="1",
        task_summaries=(
            ParentTaskSummary(
                logical_task_id="task-1",
                status="succeeded",
                summary="\u4e2d\u6587" * 8,
            ),
        ),
        diagnostics=("\u8bca\u65ad" * 8,),
    )

    projected = observation.project(ParentObservationLimits(max_summary_bytes=max_bytes))

    assert len(projected["tasks"][0]["summary"].encode("utf-8")) <= max_bytes
    assert len(projected["diagnostics"][0].encode("utf-8")) <= max_bytes
    assert projected["truncated"] is True


def test_legacy_delegate_uses_single_task_orchestration_adapter() -> None:
    port = _RecordingOrchestrationPort(_result())
    agent = AgentSpec(
        agent_id="parent",
        name="Parent",
        instructions="delegate safely",
        loop_policy=AgentLoopPolicy(max_iterations=2, allow_subagents=True),
        allowed_subagents=["critic"],
        metadata={
            "agent_orchestration": {
                "policy_ref": "parent-policy@1",
                "max_tasks_per_group": 2,
                "legacy_delegate_capabilities": {
                    "critic": {
                        "capability_hint": "research.critic@1",
                        "input_refs": ["document"],
                        "output_role": "critique",
                    }
                },
            }
        },
    )
    loop = AgentLoop(
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "action_type": "delegate",
                        "subagent_id": "critic",
                        "subagent_task": "Review the evidence",
                    }
                ),
                json.dumps({"action_type": "final_output", "output": {"output": "done"}}),
            ]
        ),
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )

    result = loop.run(agent, {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is True
    assert len(port.requests) == 1
    request = port.requests[0]
    assert len(request.candidate.tasks) == 1
    assert request.candidate.tasks[0].logical_task_id == "critic"


def test_enabled_legacy_delegate_without_mapping_is_deferred() -> None:
    port = _RecordingOrchestrationPort(_result())
    agent = AgentSpec(
        agent_id="parent",
        name="Parent",
        instructions="delegate safely",
        loop_policy=AgentLoopPolicy(max_iterations=1, allow_subagents=True),
        allowed_subagents=["critic"],
        metadata={"agent_orchestration": {"max_tasks_per_group": 2}},
    )
    loop = AgentLoop(
        llm_client=FakeLLMClient(
            [json.dumps({"action_type": "delegate", "subagent_id": "critic"})]
        ),
        tool_executor=ToolExecutor(ToolRegistry()),
        orchestration_port=port,
        orchestration_enabled=True,
    )

    result = loop.run(agent, {"run_id": "run-1"}, [], run_id="run-1", standalone=True)

    assert result.success is False
    assert port.requests == []
    assert any("legacy_delegate_mapping_unavailable" in str(event) for event in result.events)
