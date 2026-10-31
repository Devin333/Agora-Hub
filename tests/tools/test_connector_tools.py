"""arxiv.search_papers and github.* through the real connectors, against fault servers.

Connector tools report upstream failures inside their payload (``errors``) so
the model still gets partial results; a refused argument fails the call.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import pytest

from framework.tool import ToolStatus
from tests.tools.harness.fault_http import Delay, FaultHTTPServer, Hang, Redirect, Reset, Respond, Step, Truncate
from tests.tools.harness.invoke import ToolRun, assert_message_is_safe, expect_succeeded, run_batch, run_tool
from tests.tools.harness.network import arxiv_connector, github_connector
from tests.tools.harness.registry import ToolEnvironment

Build = Callable[..., ToolEnvironment]
TOKEN = "ghp_FAULTHARNESS0000000000000000000000"
JSON = "application/json"
ATOM = "application/atom+xml"
ARXIV_FEED = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
    "<entry><id>http://arxiv.org/abs/2601.00001v1</id><title>Agent memory</title>"
    "<summary>About memory.</summary><published>2026-01-01T00:00:00Z</published>"
    '<link rel="alternate" href="https://arxiv.org/abs/2601.00001v1"/></entry></feed>'
)
RELEASES = json.dumps(
    [{"html_url": "https://github.com/o/r/releases/tag/v1", "tag_name": "v1", "name": "v1", "body": "notes"}]
)
SEARCH = json.dumps({"items": [{"id": 1, "full_name": "o/r", "html_url": "https://github.com/o/r"}]})


def _github_env(build: Build, server: FaultHTTPServer, **policy: Any) -> ToolEnvironment:
    return build(github_connector=github_connector(server.url("/"), **policy))


def _arxiv_env(build: Build, **policy: Any) -> ToolEnvironment:
    return build(arxiv_connector=arxiv_connector(**policy))


# tool -> (route on the fault server, arguments, good body, content type)
def _calls(server: FaultHTTPServer) -> dict[str, tuple[str, dict[str, Any], str, str]]:
    return {
        "arxiv.search_papers": ("/api/query", {"query": "agent memory", "source": {"url": server.url("/api/query")}}, ARXIV_FEED, ATOM),
        "github.fetch_releases": ("/repos/o/r/releases", {"repository": "o/r", "source": {"url": server.url("/")}}, RELEASES, JSON),
        "github.search_repositories": ("/search/repositories", {"query": "agents", "source": {"url": server.url("/")}}, SEARCH, JSON),
    }


def _env_for(tool: str, build: Build, server: FaultHTTPServer, **policy: Any) -> ToolEnvironment:
    return _arxiv_env(build, **policy) if tool.startswith("arxiv") else _github_env(build, server, **policy)


def _reported_error(run: ToolRun) -> dict[str, Any]:
    output = expect_succeeded(run)
    assert output["error_count"] >= 1 and output["errors"], f"no error reported: {output}"
    error = output["errors"][0]
    assert_message_is_safe(str(error.get("error_message") or ""), secrets=[TOKEN])
    return error


def _items(tool: str, output: dict[str, Any]) -> int:
    return int(output.get("repository_count", output.get("item_count", 0)))


TOOLS = ("arxiv.search_papers", "github.fetch_releases", "github.search_repositories")


@pytest.mark.tool_scenario("arxiv.search_papers", "A")
@pytest.mark.tool_scenario("github.search_repositories", "A")
@pytest.mark.parametrize("tool", ("arxiv.search_papers", "github.search_repositories"))
@pytest.mark.parametrize("query", ["", "   ", None])
def test_blank_query_is_refused(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str, query: Any) -> None:
    run = run_tool(_env_for(tool, tool_env_factory, fault_http).registry, tool, {"query": query})
    assert run.status is ToolStatus.FAILED, run.describe()
    assert fault_http.requests == []


@pytest.mark.tool_scenario("github.fetch_releases", "A")
@pytest.mark.parametrize("repository", ["", "no-slash", "a/b/c", "../../etc/passwd", "o/r?x=1", "o /r"])
def test_malformed_repository_never_reaches_the_api(
    tool_env_factory: Build, fault_http: FaultHTTPServer, repository: str
) -> None:
    run = run_tool(
        _github_env(tool_env_factory, fault_http).registry,
        "github.fetch_releases",
        {"repository": repository, "source": {"url": fault_http.url("/")}},
    )
    if run.status is ToolStatus.SUCCEEDED:
        _reported_error(run)
    else:
        assert_message_is_safe(run.error_message)
    assert [request.path for request in fault_http.requests if ".." in request.path] == []


@pytest.mark.tool_scenario("arxiv.search_papers", "A")
@pytest.mark.tool_scenario("github.fetch_releases", "A")
@pytest.mark.tool_scenario("github.search_repositories", "A")
@pytest.mark.parametrize("tool", TOOLS)
def test_limit_is_clamped_before_the_request(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    route, args, body, content_type = _calls(fault_http)[tool]
    fault_http.route(route, Respond(200, body, content_type=content_type))
    run = run_tool(_env_for(tool, tool_env_factory, fault_http).registry, tool, {**args, "limit": 10_000})

    expect_succeeded(run)
    params = parse_qs(fault_http.requests_for(route)[0].query)
    requested = int((params.get("per_page") or params.get("max_results"))[0])
    assert requested <= 100


@pytest.mark.tool_scenario("arxiv.search_papers", "B")
@pytest.mark.tool_scenario("github.fetch_releases", "B")
@pytest.mark.tool_scenario("github.search_repositories", "B")
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "fault",
    ["500", "404", "reset", "truncated", "not-json-or-xml", "wrong-content-type", "empty"],
)
def test_upstream_failures_are_reported_in_the_payload(
    tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str, fault: str
) -> None:
    route, args, body, content_type = _calls(fault_http)[tool]
    steps: dict[str, Step] = {
        "500": Respond(500, "boom", content_type=content_type),
        "404": Respond(404, "{}", content_type=content_type),
        "reset": Reset(),
        "truncated": Truncate(body, sent_bytes=10, content_type=content_type),
        "not-json-or-xml": Respond(200, "<<<garbage", content_type=content_type),
        "wrong-content-type": Respond(200, body, content_type="text/html"),
        "empty": Respond(200, "", content_type=content_type),
    }
    fault_http.route(route, steps[fault])

    _reported_error(run_tool(_env_for(tool, tool_env_factory, fault_http).registry, tool, args))


@pytest.mark.tool_scenario("arxiv.search_papers", "C")
@pytest.mark.tool_scenario("github.fetch_releases", "C")
@pytest.mark.tool_scenario("github.search_repositories", "C")
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("fault", ["hang", "always-busy"])
def test_connector_fetch_ends_on_its_own_budget(
    tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str, fault: str
) -> None:
    route, args, _body, content_type = _calls(fault_http)[tool]
    fault_http.route(route, Hang() if fault == "hang" else Delay(0.4, Respond(503, "busy", content_type=content_type)))
    environment = _env_for(tool, tool_env_factory, fault_http, timeout_seconds=1.0, retry_times=10)

    run = run_tool(environment.registry, tool, args)

    _reported_error(run)
    assert run.elapsed_seconds < 1.6, run.describe()
    assert len(fault_http.requests_for(route)) <= 3
    assert environment.registry.require(tool).definition.timeout_seconds == pytest.approx(2.0)


@pytest.mark.tool_scenario("arxiv.search_papers", "C")
@pytest.mark.tool_scenario("github.fetch_releases", "C")
@pytest.mark.tool_scenario("github.search_repositories", "C")
@pytest.mark.parametrize("tool", TOOLS)
def test_transient_upstream_error_recovers(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    route, args, body, content_type = _calls(fault_http)[tool]
    fault_http.route(route, Respond(502, "bad gateway", content_type=content_type), Respond(200, body, content_type=content_type))

    run = run_tool(_env_for(tool, tool_env_factory, fault_http, retry_times=1).registry, tool, args)

    assert _items(tool, expect_succeeded(run)) == 1
    assert len(fault_http.requests_for(route)) == 2


@pytest.mark.tool_scenario("github.search_repositories", "C")
def test_rate_limit_with_a_long_retry_after_is_not_hammered(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    route, args, _body, _type = _calls(fault_http)["github.search_repositories"]
    fault_http.route(route, Respond(429, '{"message":"rate limited"}', content_type=JSON, headers={"Retry-After": "30"}))

    run = run_tool(_github_env(tool_env_factory, fault_http, retry_times=3).registry, "github.search_repositories", args)

    _reported_error(run)
    # Waiting 30s does not fit a 2s budget, so retrying now would only hit the limit again.
    assert len(fault_http.requests_for(route)) == 1


@pytest.mark.tool_scenario("github.search_repositories", "C")
def test_short_retry_after_is_waited_for_before_retrying(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    route, args, body, _type = _calls(fault_http)["github.search_repositories"]
    fault_http.route(
        route,
        Respond(503, "busy", content_type=JSON, headers={"Retry-After": "1"}),
        Respond(200, body, content_type=JSON),
    )

    run = run_tool(
        _github_env(tool_env_factory, fault_http, retry_times=1, timeout_seconds=3.0).registry,
        "github.search_repositories",
        args,
    )

    assert _items("github.search_repositories", expect_succeeded(run)) == 1
    first, second = fault_http.requests_for(route)
    assert second.received_at - first.received_at >= 0.9


@pytest.mark.tool_scenario("arxiv.search_papers", "D")
@pytest.mark.tool_scenario("github.fetch_releases", "D")
@pytest.mark.tool_scenario("github.search_repositories", "D")
@pytest.mark.parametrize("tool", TOOLS)
def test_oversized_api_response_is_refused(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    route, args, body, content_type = _calls(fault_http)[tool]
    fault_http.route(route, Respond(200, body + " " * 5000, content_type=content_type))

    error = _reported_error(run_tool(_env_for(tool, tool_env_factory, fault_http, max_bytes=1000).registry, tool, args))
    assert "max_bytes" in str(error.get("error_message"))


@pytest.mark.tool_scenario("arxiv.search_papers", "E")
@pytest.mark.tool_scenario("github.fetch_releases", "E")
@pytest.mark.tool_scenario("github.search_repositories", "E")
def test_parallel_connector_calls_keep_their_own_results(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    calls = _calls(fault_http)
    for route, _args, body, content_type in calls.values():
        fault_http.route(route, Delay(0.2, Respond(200, body, content_type=content_type)))
    environment = tool_env_factory(
        arxiv_connector=arxiv_connector(),
        github_connector=github_connector(fault_http.url("/")),
    )
    batch = [(tool, calls[tool][1]) for tool in TOOLS for _ in range(4)]

    observations, elapsed = run_batch(environment.registry, batch)

    assert [_items(name, observation.result.output) for (name, _), observation in zip(batch, observations)] == [1] * 12
    assert elapsed < 1.5  # serial would be 2.4s


@pytest.mark.tool_scenario("github.fetch_releases", "G")
@pytest.mark.tool_scenario("github.search_repositories", "G")
@pytest.mark.parametrize("tool", ("github.fetch_releases", "github.search_repositories"))
def test_token_goes_only_to_the_configured_api_origin(
    tool_env_factory: Build, fault_http: FaultHTTPServer, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    route, args, body, content_type = _calls(fault_http)[tool]
    fault_http.route(route, Respond(200, body, content_type=content_type))

    configured = tool_env_factory(github_connector=github_connector(fault_http.url("/")))
    expect_succeeded(run_tool(configured.registry, tool, args))
    elsewhere = tool_env_factory(github_connector=github_connector("https://api.github.com"))
    run = run_tool(elsewhere.registry, tool, args)

    sent = [request.header("Authorization") for request in fault_http.requests_for(route)]
    assert sent == [f"Bearer {TOKEN}", None]
    assert TOKEN not in json.dumps(run.output, default=str)


@pytest.mark.tool_scenario("github.fetch_releases", "G")
@pytest.mark.tool_scenario("github.search_repositories", "G")
@pytest.mark.parametrize("tool", ("github.fetch_releases", "github.search_repositories"))
def test_token_is_dropped_when_a_redirect_leaves_the_api_origin(
    tool_env_factory: Build, fault_http: FaultHTTPServer, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    route, args, body, content_type = _calls(fault_http)[tool]
    with FaultHTTPServer() as other_origin:
        fault_http.route(route, Redirect(other_origin.url("/landing")))
        other_origin.route("/landing", Respond(200, body, content_type=content_type))

        expect_succeeded(run_tool(_github_env(tool_env_factory, fault_http).registry, tool, args))

        assert fault_http.requests_for(route)[0].header("Authorization") == f"Bearer {TOKEN}"
        assert [request.header("Authorization") for request in other_origin.requests_for("/landing")] == [None]


@pytest.mark.tool_scenario("arxiv.search_papers", "G")
@pytest.mark.tool_scenario("github.fetch_releases", "G")
@pytest.mark.tool_scenario("github.search_repositories", "G")
@pytest.mark.parametrize("tool", TOOLS)
def test_model_chosen_internal_api_url_is_refused(tool_env_factory: Build, fault_http: FaultHTTPServer, tool: str) -> None:
    route, args, body, content_type = _calls(fault_http)[tool]
    fault_http.route(route, Respond(200, body, content_type=content_type))
    environment = _env_for(tool, tool_env_factory, fault_http, allowed_private_networks=())

    for url in (fault_http.url(route), "http://169.254.169.254/latest/"):
        error = _reported_error(run_tool(environment.registry, tool, {**args, "source": {"url": url}}))
        assert "non-public" in str(error.get("error_message"))
    assert fault_http.requests == []
