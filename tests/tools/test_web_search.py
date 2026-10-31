"""web.search over the real DuckDuckGo HTML provider, pointed at a fault server.

web.search is not modified in this round (see the tool stability plan). Its
known gaps are pinned as strict xfails: each one fails while the gap exists
and turns red the day it is fixed, so the marker cannot outlive the bug.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from framework.tool import ToolStatus
from infrastructure.tools.web_search_tools import DuckDuckGoHtmlSearchProvider
from tests.tools.harness.fault_http import FaultHTTPServer, Hang, Redirect, Reset, Respond, Trickle
from tests.tools.harness.invoke import assert_message_is_safe, expect_succeeded, run_batch, run_tool
from tests.tools.harness.registry import ToolEnvironment

Build = Callable[..., ToolEnvironment]
HTML = "text/html; charset=utf-8"


def _result(index: int) -> str:
    target = f"https%3A%2F%2Fexample.com%2Fpaper-{index}"
    return (
        f'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg={target}&amp;rut=x">'
        f"Paper {index} &amp; notes</a>"
        f'<a class="result__snippet" href="#">Snippet <b>{index}</b></a></div>'
    )


def _results_page(count: int) -> str:
    return "<html><body>" + "".join(_result(index) for index in range(count)) + "</body></html>"


CHALLENGE_PAGE = (
    "<html><head><title>DuckDuckGo</title></head><body>"
    '<div class="anomaly-modal__title">Unfortunately, bots use DuckDuckGo too.</div>'
    "<form id=\"challenge-form\">Please complete the following challenge</form></body></html>"
)


def _env(build: Build, server: FaultHTTPServer, path: str = "/html/") -> ToolEnvironment:
    provider = DuckDuckGoHtmlSearchProvider(endpoint=server.url(path), allowed_domains=["127.0.0.1"])
    return build(web_search_provider=provider)


def _search(environment: ToolEnvironment, **args: Any):
    return run_tool(environment.registry, "web.search", {"query": "agent memory", **args})


@pytest.mark.tool_scenario("web.search", "A")
@pytest.mark.parametrize("args", [{"query": ""}, {"query": "   "}, {"limit": "many"}, {"timeout_seconds": "soon"}])
def test_unusable_arguments_fail_before_any_request(
    tool_env_factory: Build, fault_http: FaultHTTPServer, args: dict[str, Any]
) -> None:
    run = run_tool(_env(tool_env_factory, fault_http).registry, "web.search", {"query": "agent memory", **args})
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)
    assert fault_http.requests == []


@pytest.mark.tool_scenario("web.search", "A")
def test_results_are_parsed_and_redirect_links_unwrapped(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/html/", Respond(200, _results_page(3), content_type=HTML))

    output = expect_succeeded(_search(_env(tool_env_factory, fault_http)))

    assert output["result_count"] == 3
    first = output["results"][0]
    assert first == {
        "title": "Paper 0 & notes",
        "url": "https://example.com/paper-0",
        "snippet": "Snippet 0",
        "source": "duckduckgo",
    }
    assert fault_http.requests_for("/html/")[0].query == "q=agent+memory"


@pytest.mark.tool_scenario("web.search", "B")
@pytest.mark.parametrize("step", [Respond(503, "busy"), Respond(403, "forbidden"), Reset()], ids=["503", "403", "reset"])
def test_upstream_failures_fail_the_call(tool_env_factory: Build, fault_http: FaultHTTPServer, step) -> None:
    fault_http.route("/html/", step)
    run = _search(_env(tool_env_factory, fault_http))
    assert run.status is ToolStatus.FAILED, run.describe()
    assert_message_is_safe(run.error_message)


@pytest.mark.tool_scenario("web.search", "B")
@pytest.mark.xfail(strict=True, reason="known gap: a bot-challenge page is reported as a successful empty search")
def test_bot_challenge_page_is_not_reported_as_an_empty_result(
    tool_env_factory: Build, fault_http: FaultHTTPServer
) -> None:
    fault_http.route("/html/", Respond(200, CHALLENGE_PAGE, content_type=HTML))
    assert _search(_env(tool_env_factory, fault_http)).status is ToolStatus.FAILED


@pytest.mark.tool_scenario("web.search", "C")
def test_hanging_endpoint_fails_on_the_requested_timeout(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/html/", Hang())
    run = _search(_env(tool_env_factory, fault_http), timeout_seconds=0.5)
    assert run.status is ToolStatus.FAILED, run.describe()
    assert run.elapsed_seconds < 1.5


@pytest.mark.tool_scenario("web.search", "C")
@pytest.mark.xfail(strict=True, reason="known gap: timeout_seconds accepts up to 30s but the tool is cut off at 20s")
def test_requested_timeout_never_exceeds_the_tool_timeout(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    environment = _env(tool_env_factory, fault_http)
    declared = environment.registry.require("web.search").definition.timeout_seconds
    properties = environment.registry.require("web.search").definition.input_schema["properties"]
    assert properties["timeout_seconds"].get("maximum", 30.0) <= declared


@pytest.mark.tool_scenario("web.search", "C")
@pytest.mark.xfail(strict=True, reason="known gap: the timeout bounds each recv, not the whole download")
def test_slow_drip_response_ends_on_the_requested_timeout(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/html/", Trickle(_results_page(1), chunk_size=8, interval_seconds=0.15, content_type=HTML))
    run = _search(_env(tool_env_factory, fault_http), timeout_seconds=0.5)
    assert run.elapsed_seconds < 1.5, run.describe()


@pytest.mark.tool_scenario("web.search", "D")
def test_huge_results_page_is_capped(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/html/", Respond(200, _results_page(5_000), content_type=HTML))

    run = _search(_env(tool_env_factory, fault_http), limit=500)

    output = expect_succeeded(run)
    assert output["result_count"] == 20  # limit is clamped to 20
    assert run.elapsed_seconds < 5.0


@pytest.mark.tool_scenario("web.search", "E")
def test_parallel_searches_keep_their_own_results(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    fault_http.route("/html/", Respond(200, _results_page(4), content_type=HTML))
    environment = _env(tool_env_factory, fault_http)

    observations, _ = run_batch(environment.registry, [("web.search", {"query": f"topic {i}"}) for i in range(12)])

    assert [observation.result.output["query"] for observation in observations] == [f"topic {i}" for i in range(12)]
    assert sorted(request.query for request in fault_http.requests_for("/html/")) == sorted(
        f"q=topic+{i}" for i in range(12)
    )


@pytest.mark.tool_scenario("web.search", "G")
def test_endpoint_outside_its_allowed_domains_is_refused(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    provider = DuckDuckGoHtmlSearchProvider(endpoint=fault_http.url("/html/"), allowed_domains=["duckduckgo.com"])
    run = run_tool(tool_env_factory(web_search_provider=provider).registry, "web.search", {"query": "x"})
    assert run.status is ToolStatus.FAILED
    assert "allowed domains" in run.error_message
    assert fault_http.requests == []


@pytest.mark.tool_scenario("web.search", "G")
@pytest.mark.xfail(strict=True, reason="known gap: redirects from the endpoint are followed without re-checking the host")
def test_redirect_away_from_the_endpoint_is_not_followed(tool_env_factory: Build, fault_http: FaultHTTPServer) -> None:
    with FaultHTTPServer() as elsewhere:
        fault_http.route("/html/", Redirect(elsewhere.url("/internal", host="localhost")))
        elsewhere.route("/internal", Respond(200, _results_page(1), content_type=HTML))
        _search(_env(tool_env_factory, fault_http), timeout_seconds=3)
        assert elsewhere.requests == []
