"""Fault stores, the leak guard, executor helpers and the scripted Agent turn."""

from __future__ import annotations

import threading
import time
import urllib.request

import pytest

from framework.shared.attempts import DEFAULT_ATTEMPT_EXECUTION_CAPACITY
from framework.tool import ToolDefinition, ToolRegistry, ToolStatus
from infrastructure.storage.persistence.records import ReportRecord
from tests.tools.harness.fault_http import FaultHTTPServer, Hang
from tests.tools.harness.fault_stores import FaultPlan, FaultyReportRepository, Pass, Raise, RaiseAfter, Stall
from tests.tools.harness.invoke import expect_failed, expect_succeeded, run_batch, run_tool
from tests.tools.harness.leaks import assert_no_leaks, snapshot_resources
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.harness.scripted_llm import final_turn, observations_in_prompt, run_agent, tool_agent, tool_turn


def _report(report_id: str, title: str = "Agent memory weekly") -> ReportRecord:
    return ReportRecord(report_id=report_id, run_id="run-1", status="completed", title=title)


def test_fault_plan_scripts_by_call_order_and_repeats_the_last_entry() -> None:
    repository = FaultyReportRepository(FaultPlan().script("save_report", Raise(ConnectionError("db down")), Pass()))

    with pytest.raises(ConnectionError):
        repository.save_report(_report("r1"))
    repository.save_report(_report("r1"))
    repository.save_report(_report("r2"))

    assert repository.plan.calls("save_report") == 3
    assert repository.write_log == [("report", "r1"), ("report", "r2")]


def test_raise_after_applies_the_write_and_loses_the_acknowledgement() -> None:
    repository = FaultyReportRepository(FaultPlan().script("save_report", RaiseAfter(TimeoutError("ack lost"))))

    with pytest.raises(TimeoutError):
        repository.save_report(_report("r1"))
    assert set(repository.reports) == {"r1"}


def test_upsert_semantics_overwrite_by_key_and_search_by_title() -> None:
    repository = FaultyReportRepository()
    repository.save_report(_report("r1", "First draft"))
    repository.save_report(_report("r1", "Agent memory final"))

    assert repository.reports["r1"].title == "Agent memory final"
    assert [summary.report_id for summary in repository.search_reports("memory")] == ["r1"]
    assert repository.write_log == [("report", "r1"), ("report", "r1")]


def test_stall_blocks_until_released() -> None:
    plan = FaultPlan().script("save_report", Stall())
    repository = FaultyReportRepository(plan)
    worker = threading.Thread(target=repository.save_report, args=(_report("r1"),))
    worker.start()
    time.sleep(0.1)
    assert worker.is_alive()
    plan.release()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert set(repository.reports) == {"r1"}


def test_leak_guard_reports_a_live_thread_until_it_exits() -> None:
    baseline = snapshot_resources()
    stop = threading.Event()
    worker = threading.Thread(target=stop.wait, name="deliberate-leak", daemon=True)
    worker.start()
    with pytest.raises(AssertionError, match="deliberate-leak"):
        assert_no_leaks(baseline, grace_seconds=0.1)
    stop.set()
    worker.join(timeout=2)
    assert_no_leaks(baseline, grace_seconds=0.1)


def test_leak_guard_reports_held_attempt_capacity() -> None:
    baseline = snapshot_resources()
    assert DEFAULT_ATTEMPT_EXECUTION_CAPACITY.acquire()
    try:
        with pytest.raises(AssertionError, match="attempt capacity"):
            assert_no_leaks(baseline, grace_seconds=0.05)
    finally:
        DEFAULT_ATTEMPT_EXECUTION_CAPACITY.release()
    assert_no_leaks(baseline, grace_seconds=0.05)


def _hanging_fetch_registry(fault_http: FaultHTTPServer) -> ToolRegistry:
    fault_http.route("/stuck", Hang())
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="harness.fetch",
            description="Fetch a URL from the fault server.",
            input_schema={"properties": {}, "additionalProperties": False},
            side_effect="read_only",
            concurrency_safe=True,
            timeout_seconds=0.3,
        ),
        lambda _args: urllib.request.urlopen(fault_http.url("/stuck"), timeout=1.0).read(),
    )
    return registry


def test_executor_timeout_is_reported_and_the_abandoned_attempt_is_reclaimed(
    fault_http: FaultHTTPServer,
) -> None:
    run = run_tool(_hanging_fetch_registry(fault_http), "harness.fetch")

    expect_failed(run, status=ToolStatus.TIMEOUT, error_type="ToolTimeoutError", within_seconds=1.0)
    # The worker is still blocked in the socket read; the autouse guard waits
    # for it after fault_http releases the handler at teardown.


def test_batch_returns_one_observation_per_call_in_request_order(tool_env: ToolEnvironment) -> None:
    observations, _ = run_batch(
        tool_env.registry,
        [
            ("quality.duplicate_check", {"items": [{"url": "https://example.com/a", "title": "A"}]}),
            ("quality.duplicate_check", {"items": "not-a-list"}),
            ("source.normalize_url", {"url": "HTTPS://Example.com/a/?b=1"}),
        ],
    )

    assert [observation.status for observation in observations] == [
        ToolStatus.SUCCEEDED,
        ToolStatus.FAILED,
        ToolStatus.SUCCEEDED,
    ]


def test_scripted_agent_turn_runs_real_tools_and_returns_the_failure(tool_env: ToolEnvironment) -> None:
    agent = tool_agent(["quality.duplicate_check", "source.normalize_url"])
    result, llm = run_agent(
        tool_env.registry,
        agent,
        [
            tool_turn(
                ("call_dupes", "quality.duplicate_check", {"items": "not-a-list"}),
                ("call_url", "source.normalize_url", {"url": "https://example.com/a"}),
            ),
            final_turn("normalized the URL; duplicate check rejected its input"),
        ],
    )

    assert result.success is True
    observations = observations_in_prompt(llm, 1)
    assert [(item["tool_name"], item["status"]) for item in observations] == [
        ("quality.duplicate_check", "failed"),
        ("source.normalize_url", "succeeded"),
    ]


def test_full_environment_runs_a_read_only_tool(tool_env: ToolEnvironment) -> None:
    output = expect_succeeded(
        run_tool(tool_env.registry, "source.normalize_url", {"url": "https://example.com/a"})
    )
    assert output
