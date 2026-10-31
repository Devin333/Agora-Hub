"""Many AgentLoop runs with multi-call turns over faulty dependencies (marker ``tool_stress``)."""

from __future__ import annotations

import random
import threading

import pytest

from tests.tools.harness.fault_stores import Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.meter import StressMeter
from tests.tools.harness.registry import ToolEnvironment, build_tool_environment
from tests.tools.harness.scripted_llm import (
    final_turn,
    observations_in_prompt,
    run_graph_agent,
    tool_agent,
    tool_turn,
)

pytestmark = pytest.mark.tool_stress

SEED = 20261031
REPORT = {"title": "Agent memory weekly", "sections": [{"title": "Findings", "content": "Memory matters."}]}
TOOLS = [
    "memory.recall",
    "report.search",
    "report.publish",
    "control.delegate_to_subagent",
    "local_json.save",
    "quality.duplicate_check",
]
FAULTS = [Pass(), Pass(), Raise(ConnectionError("store down")), RaiseAfter(TimeoutError("ack lost")), Stall(seconds=0.02)]


def _arm_faults(environment: ToolEnvironment, rng: random.Random) -> None:
    environment.memory.plan.script("recall", rng.choice(FAULTS))
    environment.reports.plan.script("search_reports", rng.choice(FAULTS))
    environment.reports.plan.script("save_report", rng.choice(FAULTS))
    environment.tasks.plan.script("enqueue", rng.choice(FAULTS))


def _one_run(environment: ToolEnvironment, run_index: int) -> list[str]:
    """Run one agent with a 6-call turn; return the observation statuses the model saw."""

    result, llm = run_graph_agent(
        environment.registry,
        tool_agent(TOOLS),
        [
            tool_turn(
                ("c1", "memory.recall", {"query": f"q{run_index}"}),
                ("c2", "report.search", {"query": "memory"}),
                ("c3", "report.publish", {"report": REPORT, "report_id": f"r{run_index % 4}"}),
                ("c4", "control.delegate_to_subagent", {"task_type": "summarize", "payload": {"n": run_index % 4}}),
                ("c5", "local_json.save", {"collection": "briefs", "record_id": f"r{run_index % 4}", "value": {"i": run_index}}),
                ("c6", "quality.duplicate_check", {"items": [{"url": "https://a.com/x"}] * 3}),
            ),
            final_turn(f"run {run_index} reported what came back"),
        ],
        identity=environment.identity,
    )
    assert result.success is True, result.to_dict()["termination_reason"]
    observations = observations_in_prompt(llm, 1)
    assert [item["tool_name"] for item in observations] == TOOLS
    return [item["status"] for item in observations]


@pytest.mark.leak_grace(3.0)
def test_sequential_agent_runs_under_seeded_faults_stay_correct_and_flat(tool_env: ToolEnvironment) -> None:
    rng = random.Random(SEED)
    seen: set[str] = set()

    with StressMeter("60 sequential AgentLoop runs, 6 calls each") as meter:
        for run_index in range(1, 61):
            _arm_faults(tool_env, rng)
            statuses = _one_run(tool_env, run_index)
            # Pure and local tools never depend on the faulted stores.
            assert statuses[4:] == ["succeeded", "succeeded"], statuses
            seen.update(statuses)
            if run_index % 10 == 0:
                meter.sample(run_index * len(TOOLS))

    assert {"succeeded", "failed"} <= seen
    assert meter.memory_growth_bytes() < 4 * 1024 * 1024, meter.report()


@pytest.mark.leak_grace(3.0)
def test_concurrent_agent_runs_in_separate_activities_do_not_interfere(tmp_path) -> None:
    environments = [build_tool_environment(tmp_path / f"activity-{index}") for index in range(8)]
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(environments))

    def worker(index: int) -> None:
        environment = environments[index]
        rng = random.Random(SEED + index)
        try:
            barrier.wait()
            for run_index in range(1, 11):
                _arm_faults(environment, rng)
                _one_run(environment, run_index)
        except BaseException as exc:  # surfaced below with the worker's index
            errors.append(exc)

    with StressMeter("8 activities x 10 AgentLoop runs in parallel", units=80 * len(TOOLS)):
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(len(environments))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

    assert not errors, repr(errors[:3])
    for environment in environments:
        # Each activity wrote only its own records under its own root.
        saved = sorted(path.name for path in (environment.local_json_root / "briefs").glob("*.json"))
        assert saved == ["r0.json", "r1.json", "r2.json", "r3.json"]
