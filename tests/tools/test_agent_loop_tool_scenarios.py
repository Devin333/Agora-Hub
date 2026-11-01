"""End-to-end: one AgentLoop turn issues several real tool calls, some against faulty dependencies.

The loop runs as a Graph activity, wired like ``AgentRunner`` wires it, over
the full business registry. What matters is what the model is shown next:
every call answered in request order, failures as observations with the
right status, nothing leaked, and siblings unaffected by a stalled call.
"""

from __future__ import annotations

import json
import time

import pytest

from framework.agent.models import AgentLoopStatus
from tests.tools.harness.fault_http import FaultHTTPServer, Hang, Respond
from tests.tools.harness.fault_stores import Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.network import source_payload, source_policy, source_tool_environment
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.harness.scripted_llm import (
    final_turn,
    observations_in_prompt,
    run_agent,
    run_graph_agent,
    tool_agent,
    tool_turn,
    trajectory_statuses,
)

REPORT = {"title": "Agent memory weekly", "sections": [{"title": "Findings", "content": "Memory matters."}]}


def _statuses(observations: list[dict]) -> list[tuple[str, str]]:
    return [(item["tool_name"], item["status"]) for item in observations]


def _by_tool(observations: list[dict]) -> dict[str, dict]:
    return {item["tool_name"]: item for item in observations}


def test_mixed_batch_answers_every_call_in_order_with_its_own_status(tool_env: ToolEnvironment) -> None:
    tool_env.reports.plan.script("save_report", RaiseAfter(TimeoutError("ack lost")))
    tool_env.memory.plan.script("recall", Raise(ConnectionError("memory store down")))
    names = ["artifact.write", "report.publish", "memory.recall", "control.delegate_to_subagent", "quality.duplicate_check"]

    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(names),
        [
            tool_turn(
                ("c1", "artifact.write", {"path": "notes/a.json", "content": {"a": 1}}),
                ("c2", "report.publish", {"report": REPORT}),
                ("c3", "memory.recall", {"query": "agent memory"}),
                ("c4", "control.delegate_to_subagent", {"task_type": "summarize"}),
                ("c5", "quality.duplicate_check", {"items": [{"url": "https://a.com/x"}] * 3}),
            ),
            final_turn("reported what came back"),
        ],
        identity=tool_env.identity,
    )

    assert result.success is True
    observations = observations_in_prompt(llm, 1)
    assert _statuses(observations) == [
        ("artifact.write", "succeeded"),
        ("report.publish", "failed"),
        ("memory.recall", "failed"),
        ("control.delegate_to_subagent", "succeeded"),
        ("quality.duplicate_check", "succeeded"),
    ]
    by_tool = _by_tool(observations)
    # The model must be able to tell "maybe published" from "definitely not read".
    assert by_tool["report.publish"]["result"]["indeterminate"] is True
    assert by_tool["memory.recall"]["result"]["indeterminate"] is False
    assert [item["metadata"]["model_tool_call_id"] for item in (o["call"] for o in observations)] == [
        "c1", "c2", "c3", "c4", "c5"
    ]


def test_stalled_store_read_times_out_without_holding_back_its_siblings(
    tool_env_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from framework.tool.builtin import memory as memory_tools

    monkeypatch.setattr(memory_tools, "MEMORY_RECALL_TIMEOUT_SECONDS", 0.5)
    environment = tool_env_factory()
    environment.memory.plan.script("recall", Stall())
    names = ["memory.recall", "report.search", "quality.duplicate_check", "report.validate"]
    try:
        started = time.monotonic()
        result, llm = run_graph_agent(
            environment.registry,
            tool_agent(names),
            [
                tool_turn(
                    ("c1", "memory.recall", {"query": "agent memory"}),
                    ("c2", "report.search", {"query": "memory"}),
                    ("c3", "quality.duplicate_check", {"items": []}),
                    ("c4", "report.validate", {"report": REPORT}),
                ),
                final_turn("recall timed out"),
            ],
            identity=environment.identity,
        )
        elapsed = time.monotonic() - started
    finally:
        environment.memory.plan.release()

    assert result.success is True
    assert _statuses(observations_in_prompt(llm, 1)) == [
        ("memory.recall", "timeout"),
        ("report.search", "succeeded"),
        ("quality.duplicate_check", "succeeded"),
        ("report.validate", "succeeded"),
    ]
    assert elapsed < 5.0


def test_hung_upstream_fetch_times_out_and_the_rest_of_the_turn_completes(
    tool_env_factory, fault_http: FaultHTTPServer
) -> None:
    fault_http.route("/feed", Hang())
    fault_http.route("/ok", Respond(200, "<html><title>fine</title><p>ok</p></html>", content_type="text/html"))
    environment = source_tool_environment(tool_env_factory, policy=source_policy(timeout_seconds=0.5))
    names = ["source.fetch_url", "source.normalize_url", "report.validate"]

    try:
        started = time.monotonic()
        result, llm = run_graph_agent(
            environment.registry,
            tool_agent(names),
            [
                tool_turn(
                    ("c1", "source.fetch_url", {"source": source_payload(fault_http.url("/feed"))}),
                    ("c2", "source.normalize_url", {"url": "https://Example.com/a?utm_source=x"}),
                    ("c3", "report.validate", {"report": REPORT}),
                ),
                final_turn("feed unreachable"),
            ],
            identity=environment.identity,
        )
        elapsed = time.monotonic() - started
    finally:
        fault_http.release()

    assert result.success is True
    statuses = _statuses(observations_in_prompt(llm, 1))
    assert statuses[0][0] == "source.fetch_url" and statuses[0][1] in {"failed", "timeout"}
    assert statuses[1:] == [("source.normalize_url", "succeeded"), ("report.validate", "succeeded")]
    assert elapsed < 6.0


def test_oversized_result_reaches_the_model_as_a_bounded_pointer(tool_env: ToolEnvironment) -> None:
    run_dir = tool_env.artifact_manager.run_dir(tool_env.identity.run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    for index in range(300):
        (run_dir / f"doc-{index:03d}.txt").write_text("needle", encoding="utf-8")

    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["artifact.search"]),
        [tool_turn(("c1", "artifact.search", {"query": "needle", "max_results": 100})), final_turn("found them")],
        identity=tool_env.identity,
    )

    assert result.success is True
    [observation] = observations_in_prompt(llm, 1)
    assert observation["status"] == "succeeded"
    assert observation["artifact_ref"] is not None
    assert len(json.dumps(observation)) < 20_000


def test_two_writes_to_one_record_in_a_turn_apply_in_request_order(tool_env: ToolEnvironment) -> None:
    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["local_json.save"], max_tool_calls_per_iteration=2),
        [
            tool_turn(
                ("c1", "local_json.save", {"collection": "briefs", "record_id": "r1", "value": {"version": 1}}),
                ("c2", "local_json.save", {"collection": "briefs", "record_id": "r1", "value": {"version": 2}}),
            ),
            final_turn("saved twice"),
        ],
        identity=tool_env.identity,
    )

    assert result.success is True
    assert _statuses(observations_in_prompt(llm, 1)) == [("local_json.save", "succeeded")] * 2
    stored = json.loads((tool_env.local_json_root / "briefs" / "r1.json").read_text(encoding="utf-8"))
    assert stored["value"] == {"version": 2}


def test_model_retry_after_a_lost_acknowledgement_does_not_queue_the_work_twice(tool_env: ToolEnvironment) -> None:
    tool_env.tasks.plan.script("enqueue", RaiseAfter(TimeoutError("ack lost")), Pass())
    delegation = {"task_type": "summarize", "payload": {"report_id": "r1"}}

    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["control.delegate_to_subagent"]),
        [
            tool_turn(("c1", "control.delegate_to_subagent", delegation)),
            tool_turn(("c2", "control.delegate_to_subagent", delegation)),
            final_turn("delegated once"),
        ],
        identity=tool_env.identity,
    )

    assert result.success is True
    first = observations_in_prompt(llm, 1)[-1]
    retried = observations_in_prompt(llm, 2)[-1]
    assert first["status"] == "failed" and first["result"]["indeterminate"] is True
    assert retried["status"] == "succeeded"
    assert tool_env.tasks.inner.queue_status("framework:queue:default").pending_count == 1


def test_approval_gate_pauses_mid_batch_and_runs_nothing_after_it(tool_env: ToolEnvironment) -> None:
    names = ["report.validate", "control.delegate_to_subagent", "local_json.save"]

    result, _llm = run_graph_agent(
        tool_env.registry,
        tool_agent(names, require_approval_for=["control.delegate_to_subagent"]),
        [
            tool_turn(
                ("c1", "report.validate", {"report": REPORT}),
                ("c2", "control.delegate_to_subagent", {"task_type": "summarize"}),
                ("c3", "local_json.save", {"collection": "briefs", "record_id": "r1", "value": {}}),
            ),
        ],
        identity=tool_env.identity,
    )

    assert result.status == AgentLoopStatus.WAITING_FOR_APPROVAL
    assert trajectory_statuses(result) == [
        ("report.validate", "succeeded"),
        ("control.delegate_to_subagent", "approval_required"),
        ("local_json.save", "blocked"),
    ]
    assert tool_env.tasks.plan.calls("enqueue") == 0
    assert not (tool_env.local_json_root / "briefs" / "r1.json").exists()


def test_disallowed_call_is_blocked_while_its_siblings_run(tool_env: ToolEnvironment) -> None:
    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["report.validate", "quality.duplicate_check"], max_tool_calls_per_iteration=3),
        [
            tool_turn(
                ("c1", "report.validate", {"report": REPORT}),
                ("c2", "local_json.save", {"collection": "briefs", "record_id": "r1", "value": {}}),
                ("c3", "quality.duplicate_check", {"items": []}),
            ),
            final_turn("save was not allowed"),
        ],
        identity=tool_env.identity,
    )

    # The judge refuses an answer from a turn that tried a tool outside the
    # allowlist; what matters here is that the siblings still ran.
    assert result.success is False
    assert _statuses(observations_in_prompt(llm, 1)) == [
        ("report.validate", "succeeded"),
        ("local_json.save", "blocked"),
        ("quality.duplicate_check", "succeeded"),
    ]
    assert not (tool_env.local_json_root / "briefs" / "r1.json").exists()


def test_over_budget_batch_is_refused_whole_and_the_model_can_retry_smaller(tool_env: ToolEnvironment) -> None:
    save = {"collection": "briefs", "record_id": "r1", "value": {"v": 1}}

    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["report.validate", "local_json.save"], max_tool_calls_per_iteration=2),
        [
            tool_turn(
                ("c1", "local_json.save", save),
                ("c2", "report.validate", {"report": REPORT}),
                ("c3", "report.validate", {"report": REPORT}),
            ),
            tool_turn(("c4", "local_json.save", save), ("c5", "report.validate", {"report": REPORT})),
            final_turn("saved on the second try"),
        ],
        identity=tool_env.identity,
    )

    assert result.success is True
    # Nothing of a refused batch runs, so the model never has to guess which part took effect.
    first = observations_in_prompt(llm, 1)
    assert [item["status"] for item in first] == ["blocked"] * 3
    assert "max_tool_calls_per_iteration of 2" in first[0]["result"]["error_message"]
    assert _statuses(observations_in_prompt(llm, 2)[3:]) == [
        ("local_json.save", "succeeded"),
        ("report.validate", "succeeded"),
    ]
    assert (tool_env.local_json_root / "briefs" / "r1.json").exists()


def test_a_turn_whose_calls_all_fail_does_not_stop_the_run_by_itself(tool_env: ToolEnvironment) -> None:
    """Five parallel failures are one failed turn: the model still gets to react."""

    down = Raise(ConnectionError("memory store down"))
    tool_env.memory.plan.script("recall", down, down, down, down, down, Pass())
    calls = [(f"c{index}", "memory.recall", {"query": f"q{index}"}) for index in range(5)]

    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["memory.recall"], max_tool_calls_per_iteration=5),
        [
            tool_turn(*calls),
            tool_turn(("c9", "memory.recall", {"query": "again"})),
            final_turn("recall recovered"),
        ],
        identity=tool_env.identity,
    )

    assert result.success is True, result.to_dict()["termination_reason"]
    assert [item["status"] for item in observations_in_prompt(llm, 1)] == ["failed"] * 5
    assert observations_in_prompt(llm, 2)[-1]["status"] == "succeeded"


def test_repeated_all_failing_turns_still_stop_the_run(tool_env: ToolEnvironment) -> None:
    tool_env.memory.plan.script("recall", Raise(ConnectionError("memory store down")))
    turns = [
        tool_turn((f"t{turn}a", "memory.recall", {"query": f"a{turn}"}), (f"t{turn}b", "memory.recall", {"query": f"b{turn}"}))
        for turn in range(6)
    ]

    result, _llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["memory.recall"], max_iterations=6, max_tool_calls_per_iteration=2),
        turns,
        identity=tool_env.identity,
    )

    assert result.status == AgentLoopStatus.STALLED
    assert result.to_dict()["termination_reason"] == "tool_failed"
    # The default limit is 3 failed turns; the 4th stops the run.
    assert len(result.to_dict()["trajectory"]) == 4


def test_graph_bound_tools_refuse_a_standalone_loop(tool_env: ToolEnvironment) -> None:
    result, llm = run_agent(
        tool_env.registry,
        tool_agent(["artifact.write", "report.validate"]),
        [
            tool_turn(
                ("c1", "artifact.write", {"path": "notes/a.json", "content": {"a": 1}}),
                ("c2", "report.validate", {"report": REPORT}),
            ),
            final_turn("artifact write refused"),
        ],
    )

    assert result.success is True
    statuses = _statuses(observations_in_prompt(llm, 1))
    assert statuses[0][0] == "artifact.write" and statuses[0][1] != "succeeded"
    assert statuses[1] == ("report.validate", "succeeded")
    assert not (tool_env.artifact_manager.run_dir(tool_env.identity.run_id) / "notes" / "a.json").exists()


def test_store_errors_carrying_credentials_never_reach_the_model(tool_env: ToolEnvironment) -> None:
    tool_env.reports.plan.script(
        "search_reports",
        Raise(ConnectionError("could not connect to postgresql://newsroom:hunter2@db.internal:5432/news")),
    )

    result, llm = run_graph_agent(
        tool_env.registry,
        tool_agent(["report.search"]),
        [tool_turn(("c1", "report.search", {"query": "memory"})), final_turn("store down")],
        identity=tool_env.identity,
    )

    assert result.success is True
    prompt = llm.requests[1].messages[-1]["content"]
    assert "hunter2" not in prompt
    assert _statuses(observations_in_prompt(llm, 1)) == [("report.search", "failed")]
