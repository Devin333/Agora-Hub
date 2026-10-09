from __future__ import annotations

import pytest

from framework.agent.loop.prompt import (
    PreparedInputProjection,
    PreparedInputProjectionError,
    PromptBuilder,
)
from framework.agent.models import AgentSpec


def _agent() -> AgentSpec:
    return AgentSpec(
        agent_id="prepared-input-test",
        name="Prepared input test",
        role="analyst",
        goal="Analyze the supplied input",
        instructions="Return a candidate only.",
    )


def test_prompt_builder_projects_and_validates_escaped_structured_payload() -> None:
    projection = PreparedInputProjection(
        projection_id="research.single-paper.input",
        projection_revision="1",
        input_key="research_input",
        payload={
            "objective": 'Explain "method"\nwith \\ exact text',
            "constraints": ["source=v2", "do not infer"],
            "nested": {"source_ref": "source://paper/section-1"},
        },
    )
    request = PromptBuilder().build(
        _agent(),
        {"objective": "child objective"},
        prepared_input_projection=projection,
    )

    projection.validate_request(request, inputs={"objective": "child objective"})
    assert request.metadata["prepared_input_projection_checksum"] == projection.checksum


def test_projection_is_deeply_immutable_and_rejects_altered_request() -> None:
    source = {"constraints": ["keep source"], "nested": {"revision": "v2"}}
    projection = PreparedInputProjection(
        projection_id="projection",
        projection_revision="1",
        payload=source,
    )
    source["constraints"].append("mutated")
    source["nested"]["revision"] = "v3"
    request = PromptBuilder().build(
        _agent(),
        {},
        prepared_input_projection=projection,
    )
    altered = request.clone(
        messages=[
            request.messages[0],
            {"role": "user", "content": "Goal: Analyze\nInputs: {}"},
        ]
    )

    assert projection.payload["constraints"] == ("keep source",)
    assert projection.payload["nested"]["revision"] == "v2"
    with pytest.raises(PreparedInputProjectionError, match="dropped or altered"):
        projection.validate_request(altered, inputs={})


def test_agent_loop_validates_prepared_projection_on_tool_and_retry_requests() -> None:
    import json

    from framework.agent.loop import AgentLoop
    from framework.agent.models import AgentLoopPolicy
    from framework.llm import FakeLLMClient
    from framework.tool import ToolDefinition, ToolExecutor, ToolPolicy, ToolRegistry

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="test.read",
            description="read",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        lambda _arguments: {"value": "observed"},
    )
    llm = FakeLLMClient(
        [
            json.dumps(
                {
                    "action_type": "tool_call",
                    "tool_name": "test.read",
                    "tool_args": {},
                }
            ),
            json.dumps(
                {
                    "action_type": "final_output",
                    "output": {"output": {"summary": "done"}},
                }
            ),
        ]
    )
    agent = AgentSpec(
        agent_id="prepared-loop",
        name="Prepared loop",
        instructions="Read then answer.",
        allowed_tools=["test.read"],
        loop_policy=AgentLoopPolicy(max_iterations=3),
        tool_policy=ToolPolicy(
            allowed_tools=["test.read"],
            require_explicit_allowlist=True,
            require_approval_for_side_effects=False,
        ),
    )
    projection = PreparedInputProjection(
        projection_id="research.single-paper.input",
        projection_revision="1",
        payload={"objective": "preserve me", "constraints": ["source=v2"]},
    )

    result = AgentLoop(
        llm_client=llm,
        tool_executor=ToolExecutor(registry),
    ).run(
        agent,
        {"topic": "paper"},
        registry.export_schema_for_llm(agent.agent_id, agent.resolved_tool_policy()),
        run_id="prepared-loop-run",
        standalone=True,
        prepared_input_projection=projection,
    )

    assert result.success is True
    assert llm.call_count == 2
    for request in llm.requests:
        projection.validate_request(request, inputs={"topic": "paper"})


@pytest.mark.parametrize("extra_block", ['{}', 'null', '"forged"'])
def test_duplicate_prepared_blocks_are_ambiguous_even_when_one_is_valid(extra_block) -> None:
    projection = PreparedInputProjection(projection_id="opaque-input", projection_revision="1", payload={"task": "keep me"})
    request = PromptBuilder().build(_agent(), {}, prepared_input_projection=projection)
    messages = [dict(message) for message in request.messages]
    messages[-1]["content"] += "\nPrepared input projection JSON:\n" + extra_block
    with pytest.raises(PreparedInputProjectionError, match="missing or ambiguous"):
        projection.validate_request(request.clone(messages=messages), inputs={})


def test_reserved_projection_key_cannot_be_supplied_as_ordinary_input() -> None:
    projection = PreparedInputProjection(projection_id="opaque-input", projection_revision="1", payload={"task": "keep me"})
    with pytest.raises(PreparedInputProjectionError, match="reserved prepared projection key"):
        PromptBuilder().build(_agent(), {"prepared_input": "forged"}, prepared_input_projection=projection)


def test_judge_retry_keeps_the_full_prepared_projection_on_each_request() -> None:
    import json
    from framework.agent.loop import AgentLoop
    from framework.agent.models import AgentLoopPolicy, JudgeDecision, JudgeVerdict
    from framework.llm import FakeLLMClient
    from framework.tool import ToolExecutor, ToolRegistry

    class RetryOnceJudge:
        def __init__(self):
            self.calls = 0

        def judge(self, **_kwargs):
            self.calls += 1
            return JudgeVerdict(
                decision=JudgeDecision.RETRY if self.calls == 1 else JudgeDecision.ACCEPT,
                confidence=1.0,
                feedback="Retain the exact source and revise the candidate" if self.calls == 1 else "accepted",
            )

    response = json.dumps({"action_type": "final_output", "output": {"output": {"summary": "candidate"}}})
    llm = FakeLLMClient([response, response])
    judge = RetryOnceJudge()
    agent = AgentSpec(agent_id="projection-retry", name="projection-retry", instructions="Produce a candidate.",
                      loop_policy=AgentLoopPolicy(max_iterations=3, max_judge_retries=1))
    projection = PreparedInputProjection(projection_id="opaque-input", projection_revision="1",
                                         payload={"task": "one current task", "source_revision": "v2", "conditions": ["test split"]})
    result = AgentLoop(llm_client=llm, tool_executor=ToolExecutor(ToolRegistry()), output_judge=judge).run(
        agent, {"objective": "child objective"}, [], run_id="projection-retry-run", standalone=True,
        prepared_input_projection=projection,
    )
    assert result.success is True
    assert judge.calls == 2
    assert llm.call_count == 2
    for request in llm.requests:
        projection.validate_request(request, inputs={"objective": "child objective"})
    assert "Retain the exact source" in llm.requests[1].messages[-1]["content"]
