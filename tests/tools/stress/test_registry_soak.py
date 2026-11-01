"""Soak and concurrency runs over the real business registry (marker ``tool_stress``).

Faults are drawn from a fixed seed, so a failure reproduces exactly. Each run
prints a ``[stress]`` line with wall time, traced memory curve and threads
(``pytest -s`` shows it).
"""

from __future__ import annotations

import random
import threading
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from framework.tool import ToolStatus
from framework.tool.builtin import memory as memory_tools
from tests.tools.harness.fault_stores import Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.meter import StressMeter
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.test_notification_webhook_tools import outcome

pytestmark = pytest.mark.tool_stress

SEED = 20261030
REPORT = {"title": "Agent memory weekly", "sections": [{"title": "Findings", "content": "Memory matters."}]}
EVIDENCE = {
    "items": [
        {
            "evidence_id": "e1",
            "source_url": "https://example.com/memory",
            "title": "Agent memory",
            "summary": "Memory matters.",
            "confidence": 0.8,
        }
    ]
}


@dataclass(frozen=True)
class Scenario:
    """One kind of call: which tool, which fault, and what the caller must learn."""

    name: str
    tool: str
    arguments: Callable[[int], dict[str, Any]]
    expected: str
    store: str | None = None
    method: str | None = None
    fault: Any = None


def _scenarios() -> list[Scenario]:
    down = ConnectionError("store down")
    return [
        Scenario("publish", "report.publish", lambda i: {"report": REPORT, "report_id": f"r{i % 8}"}, "applied", "reports", "save_report", Pass()),
        Scenario("publish-down", "report.publish", lambda i: {"report": REPORT}, "indeterminate", "reports", "save_report", Raise(down)),
        Scenario("publish-ack-lost", "report.publish", lambda i: {"report": REPORT}, "indeterminate", "reports", "save_report", RaiseAfter(TimeoutError("ack lost"))),
        Scenario("search", "report.search", lambda i: {"query": "memory"}, "applied", "reports", "search_reports", Pass()),
        Scenario("search-down", "report.search", lambda i: {"query": "memory"}, "not_applied", "reports", "search_reports", Raise(down)),
        Scenario("recall", "memory.recall", lambda i: {"query": f"q{i % 5}"}, "applied", "memory", "recall", Pass()),
        Scenario("recall-slow", "memory.recall", lambda i: {"query": "slow"}, "applied", "memory", "recall", Stall(seconds=0.02)),
        Scenario("recall-down", "memory.recall", lambda i: {"query": "q"}, "not_applied", "memory", "recall", Raise(down)),
        Scenario("review", "control.request_human_review", lambda i: {"requested_action": "publish", "reason": f"check {i % 6}"}, "applied", "approvals", "upsert_approval", Pass()),
        Scenario("review-down", "control.request_human_review", lambda i: {"requested_action": "publish", "reason": "down"}, "not_applied", "approvals", "upsert_approval", Raise(down)),
        Scenario("delegate", "control.delegate_to_subagent", lambda i: {"task_type": "summarize", "payload": {"n": i % 6}}, "applied", "tasks", "enqueue", Pass()),
        Scenario("delegate-down", "control.delegate_to_subagent", lambda i: {"task_type": "summarize", "payload": {"down": True}}, "indeterminate", "tasks", "enqueue", Raise(down)),
        Scenario("save", "local_json.save", lambda i: {"collection": "briefs", "record_id": f"r{i % 8}", "value": {"i": i}}, "applied"),
        Scenario("save-bad-name", "local_json.save", lambda i: {"collection": "briefs", "record_id": "CON", "value": {}}, "not_applied"),
        Scenario("artifact", "artifact.write", lambda i: {"path": f"soak/{i % 8}.json", "content": {"i": i}}, "applied"),
        Scenario("artifact-escape", "artifact.write", lambda i: {"path": "../escape.json", "content": {}}, "not_applied"),
        Scenario("editor", "quality.editor_score", lambda i: {"report": REPORT, "evidence_bundle": EVIDENCE}, "applied"),
        Scenario("dupes", "quality.duplicate_check", lambda i: {"items": [{"url": "https://a.com/x"}] * 4}, "applied"),
        Scenario("progress-nan", "control.report_progress", lambda i: {"message": "m", "percent": float("nan")}, "not_applied"),
    ]


def _store(environment: ToolEnvironment, name: str) -> Any:
    return getattr(environment, name)


@pytest.mark.leak_grace(3.0)
def test_seeded_mixed_fault_soak_reports_every_outcome_and_holds_no_memory(tool_env: ToolEnvironment) -> None:
    rng = random.Random(SEED)
    scenarios = _scenarios()
    tally: Counter[str] = Counter()
    mismatches: list[str] = []

    with StressMeter("registry mixed-fault soak") as meter:
        for index in range(1, 601):
            scenario = rng.choice(scenarios)
            if scenario.store is not None:
                _store(tool_env, scenario.store).plan.script(scenario.method, scenario.fault)
            run = tool_env.run(scenario.tool, scenario.arguments(index))
            seen = outcome(run)
            tally[scenario.name] += 1
            if seen != scenario.expected:
                mismatches.append(f"#{index} {scenario.name}: expected {scenario.expected}, got {seen} ({run.describe()})")
            if index % 100 == 0:
                meter.sample(index)

    assert not mismatches, "\n".join(mismatches[:10])
    assert set(tally) == {scenario.name for scenario in scenarios}
    # Stored state is bounded by the key space above; anything beyond is a leak.
    assert meter.memory_growth_bytes() < 3 * 1024 * 1024, meter.report()
    assert len(tool_env.approvals.inner.list_approvals()) <= 6


@pytest.mark.leak_grace(5.0)
def test_sustained_store_timeouts_give_back_every_thread_and_slot(tool_env_factory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memory_tools, "MEMORY_RECALL_TIMEOUT_SECONDS", 0.2)
    environment = tool_env_factory()
    # Each stalled read outlives its 0.2s budget, then finishes on its own at 0.5s.
    environment.memory.plan.script("recall", Stall(seconds=0.5))
    baseline_threads = threading.active_count()

    with StressMeter("sustained recall timeouts") as meter:
        for index in range(1, 31):
            run = environment.run("memory.recall", {"query": f"q{index}"})
            assert run.status is ToolStatus.TIMEOUT, run.describe()
            if index % 10 == 0:
                meter.sample(index)

    # Abandoned workers finish within their stall; at most a few overlap.
    assert meter.max_threads() <= baseline_threads + 4, meter.report()


def _race(count: int, call: Callable[[int], Any]) -> list[Any]:
    barrier = threading.Barrier(count)
    results: list[Any] = [None] * count

    def worker(index: int) -> None:
        barrier.wait()
        results[index] = call(index)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_concurrent_identical_review_requests_leave_one_approval(tool_env: ToolEnvironment) -> None:
    args = {"requested_action": "publish", "reason": "check citations"}

    with StressMeter("32 identical review requests", units=32):
        runs = _race(32, lambda _index: tool_env.run("control.request_human_review", args))

    assert all(run.status is ToolStatus.SUCCEEDED for run in runs), [run.describe() for run in runs if run.status is not ToolStatus.SUCCEEDED][:3]
    assert len({run.output["approval_id"] for run in runs}) == 1
    assert len(tool_env.approvals.inner.list_approvals()) == 1


def test_concurrent_identical_delegations_queue_the_work_once(tool_env: ToolEnvironment) -> None:
    args = {"task_type": "summarize", "payload": {"report_id": "r1"}}

    with StressMeter("32 identical delegations", units=32):
        runs = _race(32, lambda _index: tool_env.run("control.delegate_to_subagent", args))

    assert all(run.status is ToolStatus.SUCCEEDED for run in runs), [run.describe() for run in runs if run.status is not ToolStatus.SUCCEEDED][:3]
    assert sum(1 for run in runs if not run.output["deduplicated"]) == 1
    assert tool_env.tasks.inner.queue_status("framework:queue:default").pending_count == 1


def test_concurrent_distinct_delegations_are_all_queued(tool_env: ToolEnvironment) -> None:
    with StressMeter("64 distinct delegations", units=64):
        runs = _race(64, lambda index: tool_env.run("control.delegate_to_subagent", {"task_type": "summarize", "payload": {"n": index}}))

    assert all(run.status is ToolStatus.SUCCEEDED for run in runs)
    assert len({run.output["task_id"] for run in runs}) == 64
    assert tool_env.tasks.inner.queue_status("framework:queue:default").pending_count == 64


def test_parallel_batch_of_64_real_reads_keeps_order_and_statuses(tool_env: ToolEnvironment) -> None:
    tool_env.memory.plan.script("recall", Stall(seconds=0.05))
    tool_env.reports.plan.script("search_reports", Raise(ConnectionError("report store down")))
    kinds = [
        ("memory.recall", {"query": "agent memory"}, ToolStatus.SUCCEEDED),
        ("report.search", {"query": "memory"}, ToolStatus.FAILED),
        ("quality.duplicate_check", {"items": [{"url": "https://a.com/x"}] * 3}, ToolStatus.SUCCEEDED),
        ("report.validate", {"report": REPORT}, ToolStatus.SUCCEEDED),
    ]
    calls = [kinds[index % len(kinds)] for index in range(64)]

    with StressMeter("parallel batch of 64 reads", units=64):
        observations, elapsed = tool_env.run_batch([(name, args) for name, args, _ in calls], max_workers=16)

    assert [observation.status for observation in observations] == [status for _, _, status in calls]
    assert [observation.call.tool_name for observation in observations] == [name for name, _, _ in calls]
    # 16 recalls of 0.05s each, 16 at a time: serial would be 0.8s on recalls alone.
    assert elapsed < 3.0
