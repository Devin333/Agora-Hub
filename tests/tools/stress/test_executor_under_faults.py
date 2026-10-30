"""The executor stays correct and gives every resource back under mixed upstream faults."""

from __future__ import annotations

import itertools
import threading
import urllib.request

import pytest

from framework.shared.attempts import DEFAULT_ATTEMPT_EXECUTION_CAPACITY
from framework.tool import ToolDefinition, ToolExecutor, ToolRegistry, ToolStatus
from tests.tools.harness.fault_http import Delay, FaultHTTPServer, Hang, Reset, Respond, Truncate
from tests.tools.harness.invoke import run_batch, run_tool, stability_policy

pytestmark = pytest.mark.tool_stress

# route -> status the tool must report; /stuck fails on the handler's own
# 0.4s socket timeout, well inside the 1s tool budget.
_ROUTES = {
    "/ok": ToolStatus.SUCCEEDED,
    "/slow": ToolStatus.SUCCEEDED,
    "/reset": ToolStatus.FAILED,
    "/cut": ToolStatus.FAILED,
    "/stuck": ToolStatus.FAILED,
}


def _route_fetch_registry(fault_http: FaultHTTPServer) -> ToolRegistry:
    fault_http.route("/ok", Respond(200, "fine"))
    fault_http.route("/slow", Delay(0.2, Respond(200, "late")))
    fault_http.route("/reset", Reset())
    fault_http.route("/cut", Truncate("0123456789", sent_bytes=3))
    fault_http.route("/stuck", Hang())
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="harness.fetch_route",
            description="Fetch one fault-server route.",
            input_schema={
                "required": ["path"],
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
            timeout_seconds=1.0,
        ),
        lambda args: urllib.request.urlopen(fault_http.url(args["path"]), timeout=0.4).read().decode(),
    )
    return registry


def test_parallel_batch_of_64_mixed_faults_keeps_every_result(fault_http: FaultHTTPServer) -> None:
    registry = _route_fetch_registry(fault_http)
    paths = list(itertools.islice(itertools.cycle(_ROUTES), 64))

    observations, elapsed = run_batch(
        registry,
        [("harness.fetch_route", {"path": path}) for path in paths],
        max_workers=16,
    )

    assert [observation.status for observation in observations] == [_ROUTES[path] for path in paths]
    assert [observation.call.arguments["path"] for observation in observations] == paths
    # 64 calls, 16 at a time, the slowest step 0.4s: serial would take ~10s.
    assert elapsed < 6.0


@pytest.mark.leak_grace(3.0)
def test_sustained_failures_do_not_accumulate_threads_or_capacity(fault_http: FaultHTTPServer) -> None:
    registry = _route_fetch_registry(fault_http)
    executor = ToolExecutor(registry)
    policy = stability_policy(registry)
    capacity_before = DEFAULT_ATTEMPT_EXECUTION_CAPACITY.active
    threads_before = threading.active_count()

    for path in itertools.islice(itertools.cycle(["/reset", "/cut", "/ok", "/stuck"]), 120):
        run = run_tool(registry, "harness.fetch_route", {"path": path}, policy=policy, executor=executor)
        assert run.status is _ROUTES[path], run.describe()

    assert DEFAULT_ATTEMPT_EXECUTION_CAPACITY.active == capacity_before
    # Fault-server handler threads finish with their requests; allow a few in flight.
    assert threading.active_count() <= threads_before + 4
