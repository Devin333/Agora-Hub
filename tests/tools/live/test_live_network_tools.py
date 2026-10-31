"""arxiv.*, github.*, source.* and web.search against the real services.

Read-only: these only fetch public data. Assertions check the shape and the
contract (status, counts agree with items, URLs point where they should), not
specific content, since live data changes.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from framework.tool import ToolStatus
from infrastructure.external.sources.arxiv import ArxivConnector
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy as InfraFetchPolicy
from infrastructure.external.sources.github import GithubConnector
from interfaces.services.source_tool_runtime import default_source_tool_runtime
from backend.foundation.models.source import SourceFetchPolicy as BusinessFetchPolicy
from tests.tools.harness.invoke import ToolRun, assert_message_is_safe, expect_succeeded
from tests.tools.harness.live import LiveSettings, skip_if_upstream_unavailable
from tests.tools.harness.registry import ToolEnvironment
from tests.tools.harness.scripted_llm import final_turn, observations_in_prompt, run_graph_agent, tool_agent, tool_turn

pytestmark = pytest.mark.tool_live

PUBLIC_FEED = "https://github.blog/feed/"
KNOWN_REPOSITORY = "python/cpython"


def _connector_policy() -> InfraFetchPolicy:
    return InfraFetchPolicy(timeout_seconds=15.0, retry_times=1)


def _connectors_env(tool_env_factory, live_settings: LiveSettings) -> ToolEnvironment:
    return tool_env_factory(
        arxiv_connector=ArxivConnector(fetch_policy=_connector_policy()),
        github_connector=GithubConnector(fetch_policy=_connector_policy()),
    )


def _source_env(tool_env_factory, **policy: Any) -> ToolEnvironment:
    options: dict[str, Any] = {"timeout_seconds": 15.0, "retry_times": 1}
    options.update(policy)
    return tool_env_factory(
        source_tool_runtime=default_source_tool_runtime(),
        source_fetch_policy=BusinessFetchPolicy(**options),
    )


def _clean_payload(run: ToolRun) -> dict[str, Any]:
    output = expect_succeeded(run)
    skip_if_upstream_unavailable(output["errors"])
    assert output["error_count"] == 0, output["errors"]
    return output


def test_arxiv_search_returns_real_papers(tool_env_factory, live_settings: LiveSettings) -> None:
    environment = _connectors_env(tool_env_factory, live_settings)

    run = environment.run("arxiv.search_papers", {"query": "large language model agents", "limit": 5})

    output = _clean_payload(run)
    assert 1 <= output["item_count"] <= 5
    assert output["item_count"] == len(output["items"])
    for item in output["items"]:
        assert item["title"].strip()
        assert item["url"].startswith(("https://arxiv.org/abs/", "http://arxiv.org/abs/"))
    assert run.elapsed_seconds < 30.0


def test_arxiv_query_with_no_matches_reports_an_empty_feed(tool_env_factory, live_settings: LiveSettings) -> None:
    # arXiv ORs bare terms, so the query is one fielded nonsense token.
    environment = _connectors_env(tool_env_factory, live_settings)

    run = environment.run("arxiv.search_papers", {"query": "ti:zzqxjvkwplmnbq8471", "limit": 3})

    output = expect_succeeded(run)
    skip_if_upstream_unavailable(output["errors"])
    assert output["item_count"] == 0 and output["items"] == []
    [error] = output["errors"]
    assert error["error_type"] == "empty_arxiv_feed"
    assert error["retryable"] is False and error["metadata"].get("source_health_affecting") is False


@pytest.mark.live_service("github")
def test_github_repository_search_uses_the_token_and_returns_real_repositories(tool_env_factory, live_settings: LiveSettings) -> None:
    environment = _connectors_env(tool_env_factory, live_settings)

    run = environment.run("github.search_repositories", {"query": "agent framework language:python", "limit": 5, "sort": "stars"})

    output = _clean_payload(run)
    assert 1 <= output["repository_count"] <= 5
    for repository in output["repositories"]:
        assert repository["html_url"].startswith("https://github.com/")
        assert "/" in repository["full_name"]
    stars = [repository["stargazers_count"] for repository in output["repositories"]]
    assert stars == sorted(stars, reverse=True)
    assert_message_is_safe(str(output), secrets=[str(live_settings.github_token)])


@pytest.mark.live_service("github")
def test_github_releases_of_a_known_repository(tool_env_factory, live_settings: LiveSettings) -> None:
    environment = _connectors_env(tool_env_factory, live_settings)

    run = environment.run("github.fetch_releases", {"repository": KNOWN_REPOSITORY, "limit": 3})

    output = expect_succeeded(run)
    # cpython tags releases without always publishing GitHub releases; either
    # way the call must succeed and report consistently.
    assert output["item_count"] == len(output["items"]) <= 3
    for item in output["items"]:
        assert item["url"].startswith(f"https://github.com/{KNOWN_REPOSITORY}/releases/")


@pytest.mark.live_service("github")
def test_github_missing_repository_is_reported_not_raised(tool_env_factory, live_settings: LiveSettings) -> None:
    environment = _connectors_env(tool_env_factory, live_settings)

    run = environment.run("github.fetch_releases", {"repository": "newsroom-tool-live/does-not-exist-8471"})

    output = expect_succeeded(run)
    assert output["item_count"] == 0 and output["error_count"] >= 1
    message = str(output["errors"][0].get("error_message") or "")
    assert_message_is_safe(message, secrets=[str(live_settings.github_token)])


def test_public_feed_is_fetched_and_parsed(tool_env_factory) -> None:
    environment = _source_env(tool_env_factory, allowed_domains=("github.blog",))

    source = {"source_id": "github-blog", "name": "GitHub Blog", "url": PUBLIC_FEED, "source_type": "rss"}

    fetched = expect_succeeded(environment.run("source.fetch_url", {"source": source}))
    assert fetched["status_code"] == 200 and fetched["content_bytes"] > 0
    extracted = expect_succeeded(environment.run("source.extract_items", {"source": source, "content": fetched["content"], "limit": 5}))

    assert 1 <= extracted["item_count"] == len(extracted["items"]) <= 5
    assert all(item["url"].startswith("https://github.blog/") for item in extracted["items"])


def test_feed_host_outside_allowed_domains_is_refused_before_fetching(tool_env_factory) -> None:
    environment = _source_env(tool_env_factory, allowed_domains=("example.org",))

    run = environment.run("source.fetch_url",
        {"source": {"source_id": "github-blog", "name": "GitHub Blog", "url": PUBLIC_FEED, "source_type": "rss"}},
    )

    assert run.status is ToolStatus.FAILED
    assert "allowed domains" in run.error_message
    assert run.elapsed_seconds < 2.0


def test_unresolvable_host_fails_fast_with_a_clear_reason(tool_env_factory) -> None:
    environment = _source_env(tool_env_factory)
    url = "https://newsroom-tool-live-8471.invalid/feed.xml"

    started = time.monotonic()
    run = environment.run("source.probe", {"source": {"source_id": "nowhere", "name": "Nowhere", "url": url}})
    elapsed = time.monotonic() - started

    output = expect_succeeded(run)
    assert output["ok"] is False
    assert_message_is_safe(str(output["error"]["error_message"]))
    assert elapsed < 20.0


def test_web_search_answers_or_reports_without_crashing(tool_env_factory) -> None:
    """DuckDuckGo may serve a bot challenge to automated clients; the known
    false-success on that page is pinned in test_web_search.py. Here the
    call must finish inside its budget with a well-formed payload."""

    environment = tool_env_factory()

    run = environment.run("web.search", {"query": "python asyncio documentation", "limit": 5})

    assert run.status in {ToolStatus.SUCCEEDED, ToolStatus.FAILED}, run.describe()
    if run.status is ToolStatus.SUCCEEDED:
        results = run.output.get("results", [])
        assert len(results) <= 5
        assert all(str(result.get("url", "")).startswith(("http://", "https://")) for result in results)
    else:
        assert_message_is_safe(run.error_message)
    assert run.elapsed_seconds < 30.0


@pytest.mark.live_service("github")
def test_parallel_live_reads_in_one_batch_keep_order(tool_env_factory, live_settings: LiveSettings) -> None:
    environment = _connectors_env(tool_env_factory, live_settings)
    calls = [
        ("arxiv.search_papers", {"query": "retrieval augmented generation", "limit": 2}),
        ("github.search_repositories", {"query": "retrieval augmented generation", "limit": 2}),
        ("arxiv.search_papers", {"query": "agent memory", "limit": 2}),
        ("github.search_repositories", {"query": "agent memory", "limit": 2}),
    ]

    observations, elapsed = environment.run_batch(calls)

    assert [observation.call.tool_name for observation in observations] == [name for name, _ in calls]
    assert all(observation.status is ToolStatus.SUCCEEDED for observation in observations)
    assert elapsed < 45.0


@pytest.mark.live_service("github")
def test_agent_turn_with_live_tools_returns_every_observation(tool_env_factory, live_settings: LiveSettings) -> None:
    environment = tool_env_factory(
        arxiv_connector=ArxivConnector(fetch_policy=_connector_policy()),
        github_connector=GithubConnector(fetch_policy=_connector_policy()),
    )
    names = ["arxiv.search_papers", "github.search_repositories", "github.fetch_releases"]

    result, llm = run_graph_agent(
        environment.registry,
        tool_agent(names),
        [
            tool_turn(
                ("c1", "arxiv.search_papers", {"query": "tool use language models", "limit": 2}),
                ("c2", "github.search_repositories", {"query": "tool use language models", "limit": 2}),
                ("c3", "github.fetch_releases", {"repository": "newsroom-tool-live/does-not-exist-8471"}),
            ),
            final_turn("summarized live results"),
        ],
        identity=environment.identity,
    )

    assert result.success is True
    observations = observations_in_prompt(llm, 1)
    assert [(item["tool_name"], item["status"]) for item in observations] == [(name, "succeeded") for name in names]
    assert str(live_settings.github_token) not in llm.requests[1].messages[-1]["content"]
