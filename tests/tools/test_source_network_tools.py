"""source.fetch_url / probe / fetch_official_blog / check_health under real upstream faults.

All fetches run through the production ``InfrastructureSourceToolRuntime`` and
the egress-guarded opener, against a local fault server.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from backend.layers.signal import tools as source_tools
from backend.layers.signal.source_health.manager import BasicSourceHealthManager
from framework.tool import ToolStatus
from tests.tools.harness.fault_http import (
    Delay,
    Drop,
    FaultHTTPServer,
    Hang,
    Redirect,
    Reset,
    Respond,
    Step,
    Trickle,
    Truncate,
)
from tests.tools.harness.fault_stores import FaultPlan, FaultySourceHealthStore, Raise, Stall
from tests.tools.harness.invoke import ToolRun, assert_message_is_safe, expect_succeeded, run_batch, run_tool
from tests.tools.harness.network import source_payload, source_policy, source_tool_environment
from tests.tools.harness.registry import ToolEnvironment

FETCH_TOOLS = ("source.fetch_url", "source.probe", "source.fetch_official_blog")
RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>Fault feed</title>'
    "<item><title>Release notes</title><link>https://example.com/notes</link></item>"
    "</channel></rss>"
)
# Feed connectors refuse other content types before reading the body.
RSS_TYPE = "application/rss+xml"
Build = Callable[..., ToolEnvironment]


def _env(build: Build, **policy: Any) -> ToolEnvironment:
    return source_tool_environment(build, policy=source_policy(**policy))


def _args(tool: str, url: str, **extra: Any) -> dict[str, Any]:
    fields = {"source_type": "rss", "metadata": {"official_blog": True}} if tool == "source.fetch_official_blog" else {}
    return {"source": source_payload(url, **fields), **extra}


def _refusal(run: ToolRun) -> str:
    """The failure message, wherever the tool reports it.

    fetch_url fails the call; probe and fetch_official_blog succeed and carry
    the failure in their payload so callers can record source health.
    """

    if run.status is ToolStatus.FAILED:
        message = run.error_message
    else:
        assert run.status is ToolStatus.SUCCEEDED, run.describe()
        output = run.output or {}
        if "ok" in output:
            assert output["ok"] is False, f"probe unexpectedly succeeded: {output}"
            message = str(output["error"]["error_message"])
        else:
            assert output.get("item_count") == 0 and output.get("errors"), f"fetch unexpectedly succeeded: {output}"
            message = str(output["errors"][0]["error_message"])
        assert run.status is not ToolStatus.TIMEOUT
    assert message, run.describe()
    assert_message_is_safe(message)
    return message


def _dead_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.mark.tool_scenario("source.fetch_url", "A")
@pytest.mark.tool_scenario("source.probe", "A")
@pytest.mark.tool_scenario("source.fetch_official_blog", "A")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("ftp://example.com/feed.xml", "http"),
        ("file:///etc/passwd", "http"),
        ("javascript:alert(1)", "http"),
        ("http://user:secret@example.com/feed.xml", "credentials"),
        ("http://example.com:99999/feed.xml", ""),
        ("", ""),
    ],
)
def test_unusable_source_urls_are_refused(tool_env_factory: Build, tool: str, url: str, reason: str) -> None:
    run = run_tool(_env(tool_env_factory).registry, tool, _args(tool, url))

    message = _refusal(run)
    assert reason in message.casefold()
    assert "secret" not in message


@pytest.mark.tool_scenario("source.fetch_url", "A")
@pytest.mark.tool_scenario("source.probe", "A")
@pytest.mark.parametrize("tool", ("source.fetch_url", "source.probe"))
@pytest.mark.parametrize("source", ["https://example.com/feed.xml", ["https://example.com"], 7, None])
def test_source_argument_must_be_an_object(tool_env_factory: Build, tool: str, source: Any) -> None:
    run = run_tool(_env(tool_env_factory).registry, tool, {"source": source})
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.fetch_url", "B")
@pytest.mark.tool_scenario("source.probe", "B")
@pytest.mark.tool_scenario("source.fetch_official_blog", "B")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
@pytest.mark.parametrize(
    "step",
    [Respond(503, "busy"), Respond(404, "gone"), Reset(), Truncate(RSS, sent_bytes=20, content_type=RSS_TYPE), Drop()],
    ids=["503", "404", "reset", "truncated", "dropped"],
)
def test_upstream_failures_end_as_reported_failures(
    tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str, step: Step
) -> None:
    fault_http.route("/feed", step)
    run = run_tool(_env(tool_env_factory).registry, tool, _args(tool, fault_http.url("/feed")))

    _refusal(run)
    assert len(fault_http.requests_for("/feed")) == 1


@pytest.mark.tool_scenario("source.fetch_url", "B")
@pytest.mark.tool_scenario("source.probe", "B")
@pytest.mark.tool_scenario("source.fetch_official_blog", "B")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
def test_refused_connection_is_a_reported_failure(tool_env_factory: Build, tool: str) -> None:
    run = run_tool(_env(tool_env_factory).registry, tool, _args(tool, f"http://127.0.0.1:{_dead_port()}/feed"))
    _refusal(run)


@pytest.mark.tool_scenario("source.fetch_url", "C")
@pytest.mark.tool_scenario("source.probe", "C")
@pytest.mark.tool_scenario("source.fetch_official_blog", "C")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
@pytest.mark.parametrize(
    "step",
    [Hang(), Trickle(RSS * 4, chunk_size=1, interval_seconds=0.1, content_type=RSS_TYPE), Delay(0.4, Respond(503, "busy"))],
    ids=["hang", "slow-drip", "always-busy"],
)
def test_one_budget_bounds_the_whole_fetch_including_retries(
    tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str, step: Step
) -> None:
    fault_http.route("/feed", step)
    environment = _env(tool_env_factory, timeout_seconds=1.0, retry_times=10)
    run = run_tool(environment.registry, tool, _args(tool, fault_http.url("/feed")))

    # The fetch ends on its own budget, before the executor's outer timeout.
    message = _refusal(run)
    assert run.elapsed_seconds < 1.6, run.describe()
    assert len(fault_http.requests_for("/feed")) <= 3
    if tool == "source.fetch_url":
        assert "time budget" in message or "503" in message


def _fetched(tool: str, run: ToolRun) -> bool:
    output = expect_succeeded(run)
    if tool == "source.fetch_url":
        return output["content"] == RSS
    if tool == "source.probe":
        return output["ok"] is True
    return output["item_count"] == 1


@pytest.mark.tool_scenario("source.fetch_url", "C")
@pytest.mark.tool_scenario("source.probe", "C")
@pytest.mark.tool_scenario("source.fetch_official_blog", "C")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
def test_transient_failure_recovers_on_retry(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    fault_http.route("/feed", Respond(503, "busy"), Respond(200, RSS, content_type=RSS_TYPE))
    run = run_tool(_env(tool_env_factory, retry_times=1).registry, tool, _args(tool, fault_http.url("/feed")))

    assert _fetched(tool, run), run.describe()
    assert len(fault_http.requests_for("/feed")) == 2


@pytest.mark.tool_scenario("source.fetch_url", "C")
def test_model_cannot_extend_the_operator_budget(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/feed", Respond(200, RSS, content_type=RSS_TYPE))
    environment = _env(tool_env_factory, timeout_seconds=1.0)
    definition = environment.registry.require("source.fetch_url").definition

    run = run_tool(
        environment.registry,
        "source.fetch_url",
        _args("source.fetch_url", fault_http.url("/feed"), timeout_seconds=300, max_redirects=50),
    )

    policy = expect_succeeded(run)["fetch_policy"]
    assert policy["timeout_seconds"] == 1.0
    assert policy["max_redirects"] == 3
    assert definition.timeout_seconds == 1.0 + source_tools.FETCH_TOOL_TIMEOUT_MARGIN_SECONDS


@pytest.mark.tool_scenario("source.fetch_url", "D")
@pytest.mark.tool_scenario("source.probe", "D")
@pytest.mark.tool_scenario("source.fetch_official_blog", "D")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
def test_oversized_body_is_refused_at_max_bytes(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    fault_http.route("/feed", Respond(200, "x" * 5000, content_type=RSS_TYPE))
    run = run_tool(_env(tool_env_factory, max_bytes=1000).registry, tool, _args(tool, fault_http.url("/feed")))

    assert "max_bytes" in _refusal(run)


@pytest.mark.tool_scenario("source.fetch_url", "D")
@pytest.mark.tool_scenario("source.probe", "D")
@pytest.mark.parametrize("tool", ("source.fetch_url", "source.probe"))
def test_model_cannot_raise_the_operator_byte_limit(
    tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str
) -> None:
    fault_http.route("/feed", Respond(200, "x" * 150))
    environment = _env(tool_env_factory, max_bytes=100)
    run = run_tool(environment.registry, tool, _args(tool, fault_http.url("/feed"), max_bytes=10_000))

    assert "max_bytes" in _refusal(run)


@pytest.mark.tool_scenario("source.fetch_url", "E")
def test_parallel_fetches_each_get_their_own_result(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    for index in range(16):
        fault_http.route(f"/feed/{index}", Delay(0.2, Respond(200, f"feed-{index}")))
    environment = _env(tool_env_factory)

    observations, elapsed = run_batch(
        environment.registry,
        [("source.fetch_url", _args("source.fetch_url", fault_http.url(f"/feed/{index}"))) for index in range(16)],
    )

    assert [observation.result.output["content"] for observation in observations] == [
        f"feed-{index}" for index in range(16)
    ]
    assert elapsed < 2.0  # serial would be 3.2s


@pytest.mark.tool_scenario("source.probe", "E")
def test_concurrent_probe_failures_are_all_counted(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/feed", Respond(503, "busy"))
    environment = source_tool_environment(
        tool_env_factory,
        policy=source_policy(),
        source_health_manager=BasicSourceHealthManager(failure_threshold=1000, degraded_threshold=1000),
    )
    args = _args("source.probe", fault_http.url("/feed"))

    with ThreadPoolExecutor(max_workers=12) as pool:
        runs = list(pool.map(lambda _: run_tool(environment.registry, "source.probe", args), range(12)))

    assert all(run.output["ok"] is False for run in runs)
    health = expect_succeeded(run_tool(environment.registry, "source.check_health", {"source_id": "fault-source"}))
    assert health["health"]["consecutive_failures"] == 12


@pytest.mark.tool_scenario("source.fetch_official_blog", "E")
def test_parallel_official_blog_fetches_stay_independent(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    environment = _env(tool_env_factory)
    calls = []
    for index in range(8):
        fault_http.route(f"/blog/{index}", Delay(0.1, Respond(200, RSS, content_type=RSS_TYPE)))
        args = _args("source.fetch_official_blog", fault_http.url(f"/blog/{index}"))
        args["source"]["source_id"] = f"blog-{index}"
        calls.append(("source.fetch_official_blog", args))

    observations, _ = run_batch(environment.registry, calls)

    assert [observation.result.output["item_count"] for observation in observations] == [1] * 8


@pytest.mark.tool_scenario("source.fetch_url", "G")
@pytest.mark.tool_scenario("source.probe", "G")
@pytest.mark.tool_scenario("source.fetch_official_blog", "G")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
def test_internal_addresses_are_refused_by_default(
    tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str
) -> None:
    fault_http.route("/feed", Respond(200, "INTERNAL"))
    environment = _env(tool_env_factory, allowed_private_networks=())

    for url in (fault_http.url("/feed"), "http://169.254.169.254/latest/meta-data/"):
        assert "non-public" in _refusal(run_tool(environment.registry, tool, _args(tool, url)))
    assert fault_http.requests == []


@pytest.mark.tool_scenario("source.fetch_url", "G")
@pytest.mark.tool_scenario("source.probe", "G")
@pytest.mark.tool_scenario("source.fetch_official_blog", "G")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
def test_redirect_into_cloud_metadata_is_refused(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    fault_http.route("/feed", Redirect("http://169.254.169.254/latest/meta-data/iam/"))
    run = run_tool(_env(tool_env_factory).registry, tool, _args(tool, fault_http.url("/feed")))

    assert "169.254.169.254" in _refusal(run)
    assert [request.path for request in fault_http.requests] == ["/feed"]


@pytest.mark.tool_scenario("source.fetch_url", "G")
@pytest.mark.tool_scenario("source.probe", "G")
@pytest.mark.tool_scenario("source.fetch_official_blog", "G")
@pytest.mark.parametrize("tool", FETCH_TOOLS)
def test_redirect_cannot_leave_allowed_domains(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    fault_http.route("/feed", Redirect(fault_http.url("/elsewhere", host="localhost")))
    fault_http.route("/elsewhere", Respond(200, RSS, content_type=RSS_TYPE))
    environment = _env(tool_env_factory, allowed_domains=("127.0.0.1",))

    assert "allowed_domains" in _refusal(run_tool(environment.registry, tool, _args(tool, fault_http.url("/feed"))))
    assert fault_http.requests_for("/elsewhere") == []


@pytest.mark.tool_scenario("source.fetch_url", "G")
def test_robots_lookup_cannot_be_used_to_reach_internal_addresses(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    fault_http.route("/robots.txt", Redirect("http://169.254.169.254/robots.txt"))
    fault_http.route("/feed", Respond(200, RSS, content_type=RSS_TYPE))
    run = run_tool(
        _env(tool_env_factory, respect_robots=True).registry,
        "source.fetch_url",
        _args("source.fetch_url", fault_http.url("/feed")),
    )

    assert "169.254.169.254" in _refusal(run)
    assert fault_http.requests_for("/feed") == []


def _health_env(build: Build, plan: FaultPlan) -> ToolEnvironment:
    return source_tool_environment(build, health_store=FaultySourceHealthStore(plan))


@pytest.mark.tool_scenario("source.check_health", "A")
@pytest.mark.parametrize("args", [{"source": {}}, {"source": {"source_id": ""}}, {"source": "fault-source"}, {}])
def test_check_health_needs_a_source_id(tool_env_factory: Build, args: dict[str, Any]) -> None:
    run = run_tool(_health_env(tool_env_factory, FaultPlan()).registry, "source.check_health", args)
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("source.check_health", "B")
def test_check_health_reports_a_failing_health_store(tool_env_factory: Build) -> None:
    plan = FaultPlan().script("get_source_health", Raise(ConnectionError("health store unavailable")))
    run = run_tool(_health_env(tool_env_factory, plan).registry, "source.check_health", {"source_id": "s1"})

    assert run.status is ToolStatus.FAILED, run.describe()
    assert "health store unavailable" in run.error_message


@pytest.mark.tool_scenario("source.check_health", "C")
def test_check_health_times_out_on_a_stalled_store(
    tool_env_factory: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_tools, "SOURCE_HEALTH_READ_TIMEOUT_SECONDS", 0.5)
    plan = FaultPlan().script("get_source_health", Stall())
    try:
        run = run_tool(_health_env(tool_env_factory, plan).registry, "source.check_health", {"source_id": "s1"})
        assert run.status is ToolStatus.TIMEOUT, run.describe()
        assert run.elapsed_seconds < 1.2
    finally:
        plan.release()


@pytest.mark.tool_scenario("source.check_health", "E")
def test_slow_health_store_reads_for_different_sources_overlap(tool_env_factory: Build) -> None:
    plan = FaultPlan().script("get_source_health", Stall(seconds=0.1))
    environment = _health_env(tool_env_factory, plan)

    observations, elapsed = run_batch(
        environment.registry,
        [("source.check_health", {"source_id": f"s{index}"}) for index in range(16)],
    )

    assert all(observation.status is ToolStatus.SUCCEEDED for observation in observations)
    assert elapsed < 0.8  # one global lock would serialize this to 1.6s


@pytest.mark.tool_scenario("source.check_health", "E")
@pytest.mark.tool_scenario("source.fetch_url", "E")
def test_stalled_health_read_for_one_source_does_not_block_others(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    fault_http.route("/feed", Respond(200, RSS, content_type=RSS_TYPE))
    plan = FaultPlan().script("get_source_health:stuck", Stall())
    environment = _health_env(tool_env_factory, plan)
    stuck = threading.Thread(
        target=run_tool, args=(environment.registry, "source.check_health", {"source_id": "stuck"}), daemon=True
    )
    try:
        stuck.start()
        time.sleep(0.2)
        started = time.monotonic()
        expect_succeeded(run_tool(environment.registry, "source.check_health", {"source_id": "other"}))
        expect_succeeded(run_tool(environment.registry, "source.fetch_url", _args("source.fetch_url", fault_http.url("/feed"))))
        assert time.monotonic() - started < 1.0
    finally:
        plan.release()
        stuck.join(timeout=5)
