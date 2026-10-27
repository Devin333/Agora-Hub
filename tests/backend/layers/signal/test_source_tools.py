import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from framework.tool import (
    ToolCall,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolStatus,
)
from backend.foundation.models.source import SourceDefinition, SourceError, SourceFetchPolicy
from backend.layers.signal.tools import register_source_tools
from backend.foundation.registry.source_registry import SourceRegistry
from backend.layers.signal.source_health import BasicSourceHealthManager
from backend.layers.signal.source_processing.error_metadata import SOURCE_ERROR_RUNTIME_METADATA_KEY
from backend.layers.signal.source_processing.error_policy import SOURCE_ERROR_POLICY_METADATA_KEY
from infrastructure.external.sources import DomainRateLimiter
from interfaces.services.source_tool_runtime import default_source_tool_runtime


RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example RSS</title>
    <item>
      <title>Chip Export Update</title>
      <link>https://example.com/news/chips?utm_source=newsletter</link>
      <description>New export controls were announced.</description>
      <pubDate>Mon, 11 May 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


ATOM_FIXTURE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>Model Release Notes</title>
    <link href="https://example.com/releases/model?utm_source=x" />
    <summary>Release summary.</summary>
    <updated>2026-05-11T02:00:00Z</updated>
  </entry>
</feed>
"""


JSON_FEED_FIXTURE = """{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "Example JSON Feed",
  "items": [
    {
      "id": "json-item-1",
      "url": "https://example.com/json-feed-item",
      "title": "JSON feed item",
      "summary": "JSON feed summary.",
      "date_published": "2026-05-11T02:00:00Z",
      "authors": [{"name": "Alice Example"}],
      "tags": ["ai"]
    }
  ]
}
"""


HTML_FIXTURE = """<!doctype html>
<html lang="en">
  <head>
    <title>Official Launch Notes</title>
    <link rel="canonical" href="https://example.com/blog/launch" />
    <meta name="description" content="Official launch summary." />
    <meta property="article:published_time" content="2026-05-11T10:30:00Z" />
    <meta name="author" content="Alice Example" />
  </head>
  <body>
    <article>
      <h1>Official Launch Notes</h1>
      <p>The official blog describes the source pipeline HTML fallback.</p>
    </article>
  </body>
</html>
"""


def register_test_source_tools(registry: ToolRegistry, **kwargs) -> None:
    fetch_text = kwargs.pop("fetch_text", None)
    kwargs.setdefault("source_runtime", default_source_tool_runtime(fetch_text=fetch_text))
    fetch_policy = kwargs.get("fetch_policy")
    if getattr(fetch_policy, "rate_limit_per_domain_per_minute", None) is not None:
        kwargs.setdefault("rate_limiter", DomainRateLimiter())
    register_source_tools(registry, **kwargs)


def test_source_fetch_url_tool_fetches_configured_source_through_executor() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or "source content",
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml?utm_source=x&b=2&a=1",
                    "source_type": "rss",
                    "reliability": "high",
                },
                "max_bytes": 100,
                "timeout_seconds": 2,
                "user_agent": "NewsRoomTest/1.0",
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://example.com/feed.xml?utm_source=x&b=2&a=1"]
    assert observation.result.output["source_id"] == "rss-example"
    assert observation.result.output["content"] == "source content"
    assert observation.result.output["content_bytes"] == len("source content")
    assert observation.result.output["canonical_url"] == "https://example.com/feed.xml?a=1&b=2"
    assert observation.result.output["fetch_policy"] == {
        "timeout_seconds": 2.0,
        "max_bytes": 100,
        "max_redirects": 3,
        "respect_robots": True,
        "user_agent": "NewsRoomTest/1.0",
    }


def test_source_fetch_url_tool_retries_transient_fetch_error() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []

    def fetch_text(url: str) -> str:
        seen_urls.append(url)
        if len(seen_urls) == 1:
            raise RuntimeError("temporary fetch failure")
        return "source content"

    register_test_source_tools(
        registry,
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(retry_times=1),
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["content"] == "source content"
    assert seen_urls == ["https://example.com/feed.xml", "https://example.com/feed.xml"]


def test_source_fetch_url_tool_reserves_once_across_runtime_retries() -> None:
    class CountingRateLimiter:
        def __init__(self) -> None:
            self.delegate = DomainRateLimiter()
            self.calls = 0

        def reserve(self, url: str, *, limit_per_minute: int | None):
            self.calls += 1
            return self.delegate.reserve(
                url,
                limit_per_minute=limit_per_minute,
            )

    registry = ToolRegistry()
    fetch_calls = 0
    limiter = CountingRateLimiter()

    def fetch_text(_url: str) -> str:
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls < 3:
            raise RuntimeError("temporary fetch failure")
        return "source content"

    register_source_tools(
        registry,
        fetch_policy=SourceFetchPolicy(
            retry_times=2,
            rate_limit_per_domain_per_minute=1,
        ),
        source_runtime=default_source_tool_runtime(fetch_text=fetch_text),
        rate_limiter=limiter,
    )

    observation = ToolExecutor(registry).execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["content"] == "source content"
    assert limiter.calls == 1
    assert fetch_calls == 3


def test_source_fetch_url_tool_prefers_runtime_over_legacy_fetch_hook() -> None:
    registry = ToolRegistry()
    runtime_calls: list[str] = []
    legacy_calls: list[str] = []
    register_source_tools(
        registry,
        fetch_text=lambda url: legacy_calls.append(url) or "legacy content",
        source_runtime=default_source_tool_runtime(
            fetch_text=lambda url: runtime_calls.append(url) or "runtime content"
        ),
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["content"] == "runtime content"
    assert runtime_calls == ["https://example.com/feed.xml"]
    assert legacy_calls == []


def test_source_fetch_url_legacy_hook_is_not_wrapped_in_business_retry() -> None:
    registry = ToolRegistry()
    calls = 0

    def failing_fetch(url: str) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary fetch failure")

    register_source_tools(
        registry,
        fetch_text=failing_fetch,
        fetch_policy=SourceFetchPolicy(retry_times=3),
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert calls == 1


def test_source_fetch_url_fails_closed_without_rate_limiter_adapter() -> None:
    registry = ToolRegistry()
    calls = 0

    def fetch_text(url: str) -> str:
        nonlocal calls
        calls += 1
        return "content"

    register_source_tools(
        registry,
        source_runtime=default_source_tool_runtime(fetch_text=fetch_text),
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert "rate limiter adapter is required" in (observation.result.error_message or "")
    assert calls == 0


def test_source_fetch_url_tool_rejects_non_http_urls_before_fetch() -> None:
    registry = ToolRegistry()
    calls = {"count": 0}
    register_test_source_tools(
        registry,
        fetch_text=lambda url: calls.__setitem__("count", calls["count"] + 1) or "content",
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "local",
                    "name": "Local",
                    "url": "file:///etc/passwd",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert calls["count"] == 0
    assert "only supports http and https" in (observation.result.error_message or "")


def test_source_fetch_url_tool_allows_configured_domain_and_subdomain() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or "source content",
        allowed_domains=["example.com"],
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://news.example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://news.example.com/feed.xml"]


def test_source_fetch_url_tool_rejects_domains_outside_allowlist_before_fetch() -> None:
    registry = ToolRegistry()
    calls = {"count": 0}
    register_test_source_tools(
        registry,
        fetch_text=lambda url: calls.__setitem__("count", calls["count"] + 1) or "content",
        allowed_domains=["example.com"],
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-evil",
                    "name": "Evil RSS",
                    "url": "https://evil.test/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert calls["count"] == 0
    assert "allowed domains" in (observation.result.error_message or "")


def test_source_fetch_url_tool_rate_limits_shared_domain_before_fetch() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or "source content",
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
    )
    executor = ToolExecutor(registry)

    first = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "first",
                    "name": "First",
                    "url": "https://example.com/first.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )
    second = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "second",
                    "name": "Second",
                    "url": "https://example.com/second.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert first.status == ToolStatus.SUCCEEDED
    assert second.status == ToolStatus.FAILED
    assert seen_urls == ["https://example.com/first.xml"]
    assert "rate_limited" in (second.result.error_message or "")


def test_source_fetch_url_tool_skips_cooling_source_before_fetch() -> None:
    registry = ToolRegistry()
    health_manager = BasicSourceHealthManager(failure_threshold=1, cooldown_seconds=300)
    health_manager.record_failure(
        "cooling",
        SourceError(source_id="cooling", error_type="fetch_timeout", error_message="timeout"),
    )
    calls = {"count": 0}
    register_test_source_tools(
        registry,
        fetch_text=lambda url: calls.__setitem__("count", calls["count"] + 1) or "content",
        health_manager=health_manager,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "cooling",
                    "name": "Cooling",
                    "url": "https://example.com/cooling.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert calls["count"] == 0
    assert "cooldown" in (observation.result.error_message or "")


def test_source_check_health_tool_reads_health_manager_state() -> None:
    registry = ToolRegistry()
    health_manager = BasicSourceHealthManager()
    health_manager.record_failure(
        "rss-example",
        SourceError(
            source_id="rss-example",
            source_name="Example RSS",
            error_type="TimeoutError",
            error_message="timed out",
            url="https://example.com/feed.xml",
        ),
    )
    register_test_source_tools(registry, health_manager=health_manager)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.check_health",
            arguments={"source_id": "rss-example"},
        ),
        ToolPolicy(allowed_tools=["source.check_health"]),
    )

    health = observation.result.output["health"]

    assert observation.status == ToolStatus.SUCCEEDED
    assert health["source_id"] == "rss-example"
    assert health["source_name"] == "Example RSS"
    assert health["url"] == "https://example.com/feed.xml"
    assert health["status"] == "healthy"
    assert health["consecutive_failures"] == 1
    assert health["last_error"]["error_type"] == "TimeoutError"


def test_source_check_health_tool_uses_source_argument_context() -> None:
    registry = ToolRegistry()
    health_manager = BasicSourceHealthManager()
    register_test_source_tools(registry, health_manager=health_manager)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.check_health",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.check_health"]),
    )

    health = observation.result.output["health"]

    assert observation.status == ToolStatus.SUCCEEDED
    assert health["source_id"] == "rss-example"
    assert health["source_name"] == "Example RSS"
    assert health["url"] == "https://example.com/feed.xml"
    assert health_manager.get("rss-example").source_name == "Example RSS"


def test_source_probe_tool_records_success_without_returning_content() -> None:
    registry = ToolRegistry()
    health_manager = BasicSourceHealthManager()
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or "probe body",
        health_manager=health_manager,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.probe",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.probe"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://example.com/feed.xml"]
    assert observation.result.output["ok"] is True
    assert observation.result.output["content_bytes"] == len("probe body")
    assert "content" not in observation.result.output
    assert observation.result.output["health"]["status"] == "healthy"
    assert observation.result.output["health"]["source_name"] == "Example RSS"
    assert observation.result.output["health"]["url"] == "https://example.com/feed.xml"
    assert health_manager.get("rss-example").last_success_at is not None


def test_source_probe_tool_records_fetch_failure_as_health_failure() -> None:
    registry = ToolRegistry()
    health_manager = BasicSourceHealthManager(failure_threshold=1)

    def failing_fetch(url: str) -> str:
        raise RuntimeError(f"cannot reach {url}")

    register_test_source_tools(
        registry,
        fetch_text=failing_fetch,
        health_manager=health_manager,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.probe",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.probe"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["ok"] is False
    error = observation.result.output["error"]
    assert error["error_type"] == "fetch_connection_error"
    assert error["source_name"] == "Example RSS"
    assert error["retryable"] is True
    assert error["metadata"]["phase"] == "probe"
    assert error["metadata"]["tool"] == "source.probe"
    assert error["metadata"]["original_exception_type"] == "RuntimeError"
    assert error["metadata"][SOURCE_ERROR_RUNTIME_METADATA_KEY] == {
        "phase": "probe",
        "retryable": True,
        "source_health_affecting": True,
    }
    assert error["metadata"][SOURCE_ERROR_POLICY_METADATA_KEY] == {
        "source_health_affecting": True,
        "workflow_blocking": False,
        "operator_action_required": False,
    }
    assert observation.result.output["health"]["status"] == "down"
    assert observation.result.output["health"]["source_name"] == "Example RSS"
    assert observation.result.output["health"]["url"] == "https://example.com/feed.xml"
    assert health_manager.get("rss-example").consecutive_failures == 1


def test_source_probe_tool_projects_formal_metadata_for_cooling_source_skip() -> None:
    registry = ToolRegistry()
    health_manager = BasicSourceHealthManager(failure_threshold=1, cooldown_seconds=300)
    health_manager.record_failure(
        "cooling",
        SourceError(source_id="cooling", error_type="fetch_timeout", error_message="timeout"),
    )
    calls = {"count": 0}
    register_test_source_tools(
        registry,
        fetch_text=lambda url: calls.__setitem__("count", calls["count"] + 1) or "content",
        health_manager=health_manager,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.probe",
            arguments={
                "source": {
                    "source_id": "cooling",
                    "name": "Cooling",
                    "url": "https://example.com/cooling.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.probe"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert calls["count"] == 0
    assert observation.result.output["ok"] is False
    error = observation.result.output["error"]
    assert error["error_type"] == "source_fetch_skipped"
    assert error["retryable"] is True
    assert error["metadata"]["phase"] == "probe"
    assert error["metadata"]["tool"] == "source.probe"
    assert error["metadata"]["skip_reason"] == "cooldown"
    assert error["metadata"]["source_health_affecting"] is False
    assert error["metadata"][SOURCE_ERROR_RUNTIME_METADATA_KEY] == {
        "phase": "probe",
        "retryable": True,
        "source_health_affecting": False,
    }
    assert error["metadata"][SOURCE_ERROR_POLICY_METADATA_KEY] == {
        "source_health_affecting": False,
        "workflow_blocking": False,
        "operator_action_required": False,
    }
    assert observation.result.output["health"]["status"] == "down"
    assert observation.result.output["health"]["cooldown_until"] is not None


def test_source_probe_tool_returns_rate_limit_error_without_fetching() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []
    health_manager = BasicSourceHealthManager(failure_threshold=1)
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or "content",
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
        health_manager=health_manager,
    )
    executor = ToolExecutor(registry)

    first = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "first",
                    "name": "First",
                    "url": "https://example.com/first.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )
    probe = executor.execute(
        ToolCall(
            tool_name="source.probe",
            arguments={
                "source": {
                    "source_id": "probe",
                    "name": "Probe",
                    "url": "https://example.com/probe.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.probe"]),
    )

    assert first.status == ToolStatus.SUCCEEDED
    assert probe.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://example.com/first.xml"]
    assert probe.result.output["ok"] is False
    assert probe.result.output["error"]["error_type"] == "rate_limited"
    assert health_manager.get("probe").consecutive_failures == 0


def test_source_fetch_official_blog_tool_fetches_marked_feed() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or RSS_FIXTURE,
        allowed_domains=["example.com"],
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={
                "source": {
                    "source_id": "official-blog",
                    "name": "Official Blog",
                    "url": "https://example.com/blog.xml",
                    "source_type": "rss",
                    "reliability": "high",
                    "metadata": {"official_blog": True},
                },
                "limit": 1,
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://example.com/blog.xml"]
    assert observation.result.output["source"]["source_id"] == "official-blog"
    assert observation.result.output["item_count"] == 1
    assert observation.result.output["error_count"] == 0
    assert item["title"] == "Chip Export Update"


def test_source_fetch_official_blog_tool_uses_shared_rate_limiter() -> None:
    registry = ToolRegistry()
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or RSS_FIXTURE,
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
    )
    executor = ToolExecutor(registry)

    first = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={
                "source": {
                    "source_id": "official-one",
                    "name": "Official One",
                    "url": "https://example.com/one.xml",
                    "source_type": "rss",
                    "metadata": {"official_blog": True},
                },
                "limit": 1,
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )
    second = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={
                "source": {
                    "source_id": "official-two",
                    "name": "Official Two",
                    "url": "https://example.com/two.xml",
                    "source_type": "rss",
                    "metadata": {"official_blog": True},
                },
                "limit": 1,
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )

    assert first.status == ToolStatus.SUCCEEDED
    assert first.result.output["item_count"] == 1
    assert second.status == ToolStatus.SUCCEEDED
    assert second.result.output["item_count"] == 0
    assert second.result.output["errors"][0]["error_type"] == "rate_limited"
    assert seen_urls == ["https://example.com/one.xml"]


def test_source_fetch_official_blog_tool_selects_registry_source_by_topic() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="community",
                name="Community Feed",
                source_type="rss",
                url="https://example.com/community.xml",
                topics=["ai"],
                authority_score=0.9,
            ),
            SourceDefinition(
                source_id="official-ai",
                name="Official AI Blog",
                source_type="atom",
                url="https://example.com/ai.atom",
                topics=["ai", "policy"],
                reliability="high",
                authority_score=0.8,
                metadata={"source_kind": "official_blog"},
            ),
        ]
    )
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        fetch_text=lambda url: seen_urls.append(url) or ATOM_FIXTURE,
        allowed_domains=["example.com"],
        source_registry=source_registry,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={"topic": "AI policy", "limit": 1},
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://example.com/ai.atom"]
    assert observation.result.output["source"]["source_id"] == "official-ai"
    assert item["title"] == "Model Release Notes"
    assert item["source_type"] == "atom"


def test_source_fetch_official_blog_tool_rejects_unmarked_source_before_fetch() -> None:
    registry = ToolRegistry()
    calls = {"count": 0}
    register_test_source_tools(
        registry,
        fetch_text=lambda url: calls.__setitem__("count", calls["count"] + 1) or RSS_FIXTURE,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={
                "source": {
                    "source_id": "community",
                    "name": "Community Feed",
                    "url": "https://example.com/community.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert calls["count"] == 0
    assert "official blog" in (observation.result.error_message or "")


def test_source_fetch_official_blog_tool_fetches_marked_html_source() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="official-html",
                name="Official HTML Blog",
                source_type="html",
                url="https://example.com/blog/launch",
                reliability="high",
                metadata={"official_blog": True},
            )
        ]
    )
    seen_urls: list[str] = []
    register_test_source_tools(
        registry,
        source_registry=source_registry,
        fetch_text=lambda url: seen_urls.append(url) or HTML_FIXTURE,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={"source_id": "official-html", "limit": 1},
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert seen_urls == ["https://example.com/blog/launch"]
    assert observation.result.output["item_count"] == 1
    assert item["source_type"] == "html"
    assert item["title"] == "Official Launch Notes"
    assert item["metadata"]["extractor_name"] == "stdlib_html_extractor"


def test_source_fetch_official_blog_tool_accepts_official_blog_source_type() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="official-blog",
                name="Official Blog",
                source_type="official_blog",
                url="https://example.com/blog/launch",
                reliability="high",
            )
        ]
    )
    register_test_source_tools(
        registry,
        source_registry=source_registry,
        fetch_text=lambda url: HTML_FIXTURE,
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_official_blog",
            arguments={"source_id": "official-blog", "limit": 1},
        ),
        ToolPolicy(allowed_tools=["source.fetch_official_blog"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert item["source_type"] == "official_blog"
    assert item["metadata"]["official_blog"] is True


def test_source_search_tool_selects_sources_by_topic_filters_and_limit() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="ai-us",
                name="AI US",
                source_type="rss",
                url="https://example.com/ai-us.xml",
                topics=["ai", "policy"],
                reliability="high",
                authority_score=0.7,
                language="en",
                region="us",
            ),
            SourceDefinition(
                source_id="ai-cn",
                name="AI CN",
                source_type="rss",
                url="https://example.com/ai-cn.xml",
                topics=["ai"],
                language="zh",
                region="cn",
            ),
            SourceDefinition(
                source_id="disabled",
                name="Disabled",
                source_type="rss",
                url="https://example.com/disabled.xml",
                topics=["ai"],
                enabled=False,
            ),
        ]
    )
    register_test_source_tools(registry, source_registry=source_registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.search",
            arguments={
                "query": "AI policy",
                "language": "en",
                "region": "us",
                "limit": 1,
            },
        ),
        ToolPolicy(allowed_tools=["source.search"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["query"] == "AI policy"
    assert observation.result.output["source_count"] == 1
    assert observation.result.output["sources"][0]["source_id"] == "ai-us"
    assert observation.result.output["sources"][0]["reliability"] == "high"


def test_source_search_tool_lists_configured_sources_when_query_is_omitted() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="enabled",
                name="Enabled",
                source_type="rss",
                url="https://example.com/enabled.xml",
            ),
            SourceDefinition(
                source_id="disabled",
                name="Disabled",
                source_type="rss",
                url="https://example.com/disabled.xml",
                enabled=False,
            ),
        ]
    )
    register_test_source_tools(registry, source_registry=source_registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.search",
            arguments={"enabled_only": False},
        ),
        ToolPolicy(allowed_tools=["source.search"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["query"] is None
    assert [source["source_id"] for source in observation.result.output["sources"]] == [
        "disabled",
        "enabled",
    ]


def test_source_search_tool_filters_by_source_type() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="rss",
                name="RSS",
                source_type="rss",
                url="https://example.com/rss.xml",
                topics=["ai"],
            ),
            SourceDefinition(
                source_id="html",
                name="HTML",
                source_type="html",
                url="https://example.com/blog",
                topics=["ai"],
            ),
        ]
    )
    register_test_source_tools(registry, source_registry=source_registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.search",
            arguments={"query": "ai", "source_type": "html"},
        ),
        ToolPolicy(allowed_tools=["source.search"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert [source["source_id"] for source in observation.result.output["sources"]] == ["html"]


def test_source_search_tool_filters_by_reliability() -> None:
    registry = ToolRegistry()
    source_registry = SourceRegistry(
        [
            SourceDefinition(
                source_id="high",
                name="High",
                source_type="rss",
                url="https://example.com/high.xml",
                topics=["ai"],
                reliability="high",
            ),
            SourceDefinition(
                source_id="low",
                name="Low",
                source_type="rss",
                url="https://example.com/low.xml",
                topics=["ai"],
                reliability="low",
            ),
        ]
    )
    register_test_source_tools(registry, source_registry=source_registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.search",
            arguments={"query": "ai", "reliability": "high"},
        ),
        ToolPolicy(allowed_tools=["source.search"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert [source["source_id"] for source in observation.result.output["sources"]] == ["high"]


def test_source_fetch_url_tool_applies_max_bytes_to_injected_fetcher() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry, fetch_text=lambda url: "abcdef")
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                },
                "max_bytes": 5,
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.FAILED
    assert "max_bytes" in (observation.result.error_message or "")


def test_source_fetch_url_tool_default_fetch_uses_source_fetch_policy(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200

        class Headers:
            def get_content_type(self):
                return "application/rss+xml"

        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            captured["read_size"] = size
            return b"abc"

    def fake_open_request(request, policy):
        captured["user_agent"] = request.headers["User-agent"]
        captured["timeout"] = policy.timeout_seconds
        captured["max_redirects"] = policy.max_redirects
        return Response()

    def fake_robots_check(url, policy):
        captured["robots_url"] = url
        captured["robots_user_agent"] = policy.user_agent

    monkeypatch.setattr(
        "interfaces.services.source_tool_runtime.open_request_with_fetch_policy",
        fake_open_request,
    )
    # The robots.txt lookup is its own network request; keep the test offline.
    monkeypatch.setattr(
        "interfaces.services.source_tool_runtime.ensure_robots_allowed",
        fake_robots_check,
    )
    registry = ToolRegistry()
    register_test_source_tools(
        registry,
        fetch_policy=SourceFetchPolicy(
            timeout_seconds=3,
            max_bytes=5,
            max_redirects=2,
            user_agent="NewsRoomTest/1.0",
        ),
    )
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.fetch_url",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                }
            },
        ),
        ToolPolicy(allowed_tools=["source.fetch_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["content"] == "abc"
    assert observation.result.output["status_code"] == 200
    assert observation.result.output["content_type"] == "application/rss+xml"
    assert captured == {
        "robots_url": "https://example.com/feed.xml",
        "robots_user_agent": "NewsRoomTest/1.0",
        "user_agent": "NewsRoomTest/1.0",
        "timeout": 3,
        "max_redirects": 2,
        "read_size": 6,
    }


def test_source_fetch_url_tool_enforces_redirect_limit() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", "/one")
                self.end_headers()
                return
            if self.path == "/one":
                self.send_response(302)
                self.send_header("Location", "/done")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"done")

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registry = ToolRegistry()
        register_test_source_tools(
            registry,
            fetch_policy=SourceFetchPolicy(max_redirects=1),
        )
        executor = ToolExecutor(registry)
        url = f"http://127.0.0.1:{server.server_port}/start"

        observation = executor.execute(
            ToolCall(
                tool_name="source.fetch_url",
                arguments={
                    "source": {
                        "source_id": "rss-example",
                        "name": "Example RSS",
                        "url": url,
                        "source_type": "rss",
                    }
                },
            ),
            ToolPolicy(allowed_tools=["source.fetch_url"]),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert observation.status == ToolStatus.FAILED
    assert "max_redirects=1" in (observation.result.error_message or "")


def test_source_fetch_url_tool_respects_robots_txt() -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/robots.txt":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"User-agent: *\nDisallow: /blocked\n")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"blocked content")

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        registry = ToolRegistry()
        register_test_source_tools(registry)
        executor = ToolExecutor(registry)
        url = f"http://127.0.0.1:{server.server_port}/blocked/feed.xml"

        observation = executor.execute(
            ToolCall(
                tool_name="source.fetch_url",
                arguments={
                    "source": {
                        "source_id": "rss-example",
                        "name": "Example RSS",
                        "url": url,
                        "source_type": "rss",
                    }
                },
            ),
            ToolPolicy(allowed_tools=["source.fetch_url"]),
        )
    finally:
        server.shutdown()
        server.server_close()

    assert observation.status == ToolStatus.FAILED
    assert "robots.txt disallows" in (observation.result.error_message or "")
    assert requests == ["/robots.txt"]


def test_source_parse_rss_tool_uses_feed_connector_through_executor() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.parse_rss",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                    "reliability": "high",
                    "authority_score": 0.9,
                },
                "xml": RSS_FIXTURE,
                "limit": 1,
            },
        ),
        ToolPolicy(allowed_tools=["source.parse_rss"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert item["title"] == "Chip Export Update"
    assert item["source_id"] == "rss-example"
    assert item["lineage"]["source_id"] == "rss-example"
    assert item["lineage"]["source_item_id"] == item["source_item_id"]
    assert item["lineage"]["raw_url"] == item["url"]
    assert item["metadata"]["source_reliability"] == "high"


def test_source_extract_items_tool_extracts_rss_content() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_items",
            arguments={
                "source": {
                    "source_id": "rss-example",
                    "name": "Example RSS",
                    "url": "https://example.com/feed.xml",
                    "source_type": "rss",
                    "reliability": "high",
                },
                "content": RSS_FIXTURE,
                "limit": 1,
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_items"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert item["title"] == "Chip Export Update"
    assert item["source_id"] == "rss-example"


def test_source_extract_items_tool_extracts_atom_content() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_items",
            arguments={
                "source": {
                    "source_id": "atom-example",
                    "name": "Example Atom",
                    "url": "https://example.com/atom.xml",
                    "source_type": "atom",
                },
                "content": ATOM_FIXTURE,
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_items"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert item["title"] == "Model Release Notes"
    assert item["source_type"] == "atom"


def test_source_extract_html_tool_extracts_html_content() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_html",
            arguments={
                "source": {
                    "source_id": "html-example",
                    "name": "HTML Example",
                    "url": "https://example.com/blog/launch",
                    "source_type": "html",
                    "reliability": "high",
                },
                "html": HTML_FIXTURE,
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_html"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert item["source_type"] == "html"
    assert item["title"] == "Official Launch Notes"
    assert item["metadata"]["extraction_confidence"] > 0


def test_source_extract_html_tool_accepts_web_page_source_type() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_html",
            arguments={
                "source": {
                    "source_id": "web-page",
                    "name": "Web Page",
                    "url": "https://example.com/blog/launch",
                    "source_type": "web_page",
                },
                "html": HTML_FIXTURE,
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_html"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert item["source_type"] == "web_page"
    assert item["metadata"]["source_kind"] == "web_page"


def test_source_extract_items_tool_dispatches_html_content() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_items",
            arguments={
                "source": {
                    "source_id": "html-example",
                    "name": "HTML Example",
                    "url": "https://example.com/blog/launch",
                    "source_type": "html",
                },
                "content": HTML_FIXTURE,
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_items"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert item["source_type"] == "html"
    assert item["url"] == "https://example.com/blog/launch"


def test_source_extract_items_tool_parses_json_feed_content() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_items",
            arguments={
                "source": {
                    "source_id": "json-feed",
                    "name": "JSON Feed",
                    "url": "https://example.com/feed.json",
                    "source_type": "rss",
                },
                "content": JSON_FEED_FIXTURE,
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_items"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert item["title"] == "JSON feed item"
    assert item["metadata"]["feed_format"] == "json_feed"


def test_source_extract_manual_tool_extracts_curated_records() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_manual",
            arguments={
                "source": {
                    "source_id": "manual-example",
                    "name": "Manual Example",
                    "url": "manual://operator",
                    "source_type": "manual",
                    "reliability": "high",
                },
                "records": [
                    {
                        "title": "Curated article",
                        "url": "https://example.com/curated",
                        "summary": "Reviewed by an operator.",
                        "submitted_by": "operator-1",
                    }
                ],
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_manual"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert observation.result.output["error_count"] == 0
    assert item["source_type"] == "manual"
    assert item["metadata"]["submitted_by"] == "operator-1"


def test_source_extract_items_tool_dispatches_manual_records() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.extract_items",
            arguments={
                "source": {
                    "source_id": "manual-example",
                    "name": "Manual Example",
                    "url": "manual://operator",
                    "source_type": "manual",
                },
                "content": [{"title": "Curated article", "url": "https://example.com/curated"}],
            },
        ),
        ToolPolicy(allowed_tools=["source.extract_items"]),
    )

    item = observation.result.output["items"][0]

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["item_count"] == 1
    assert item["source_type"] == "manual"


def test_source_normalize_url_tool_removes_tracking_parameters() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.normalize_url",
            arguments={"url": "HTTPS://Example.com/News/?utm_source=x&b=2&a=1#g"},
        ),
        ToolPolicy(allowed_tools=["source.normalize_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["canonical_url"] == "https://example.com/News?a=1&b=2"


def test_source_normalize_url_tool_resolves_relative_url_with_base_url() -> None:
    registry = ToolRegistry()
    register_test_source_tools(registry)
    executor = ToolExecutor(registry)

    observation = executor.execute(
        ToolCall(
            tool_name="source.normalize_url",
            arguments={
                "url": "../News/?utm_source=x&b=2&a=1#g",
                "base_url": "https://Example.com/blog/index.html",
            },
        ),
        ToolPolicy(allowed_tools=["source.normalize_url"]),
    )

    assert observation.status == ToolStatus.SUCCEEDED
    assert observation.result.output["canonical_url"] == "https://example.com/News?a=1&b=2"
