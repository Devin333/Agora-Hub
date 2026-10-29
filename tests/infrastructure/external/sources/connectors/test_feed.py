from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from backend.foundation.models.source import SourceDefinition
from infrastructure.external.sources import DomainRateLimiter, FeedConnector, SourceFetchPolicy, TooManyRedirectsError


RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example RSS</title>
    <item>
      <title>AI chip export update</title>
      <link>https://example.com/articles/chips?utm_source=x</link>
      <description>Policy update summary.</description>
      <pubDate>Mon, 11 May 2026 02:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""


ATOM_FIXTURE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <title>Model release notes</title>
    <link href="https://example.com/releases/model" />
    <summary>Release summary.</summary>
    <updated>2026-05-11T02:00:00Z</updated>
  </entry>
</feed>
"""


JSON_FEED_FIXTURE = """{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "Example JSON Feed",
  "home_page_url": "https://example.com/",
  "feed_url": "https://example.com/feed.json",
  "language": "en",
  "items": [
    {
      "id": "json-item-1",
      "url": "https://example.com/json-feed-item?utm_source=x",
      "title": "JSON feed item",
      "summary": "JSON feed summary.",
      "date_published": "2026-05-11T02:00:00Z",
      "authors": [{"name": "Alice Example"}],
      "tags": ["ai", "release"]
    }
  ]
}
"""


def test_feed_connector_parses_rss_fixture() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
        reliability="high",
    )

    items = FeedConnector().parse(source, RSS_FIXTURE)

    assert len(items) == 1
    assert items[0].title == "AI chip export update"
    assert items[0].url == "https://example.com/articles/chips?utm_source=x"
    assert items[0].metadata["source_reliability"] == "high"
    assert items[0].metadata["source_authority_score"] == 0.5


def test_feed_connector_propagates_allowlisted_governance_metadata() -> None:
    source = SourceDefinition(
        source_id="official-rss",
        name="Official RSS",
        source_type="rss",
        url="https://example.com/rss.xml",
        category="official",
        metadata={
            "official_blog": True,
            "category": "official_blog",
            "api_key": "should-not-propagate",
        },
    )

    items = FeedConnector().parse(source, RSS_FIXTURE)

    assert len(items) == 1
    assert items[0].metadata["official_blog"] is True
    assert items[0].metadata["source_category"] == "official"
    assert items[0].metadata["category"] == "official_blog"
    assert "api_key" not in items[0].metadata


def test_feed_connector_parses_atom_fixture() -> None:
    source = SourceDefinition(
        source_id="atom-source",
        name="Atom Source",
        source_type="atom",
        url="https://example.com/atom.xml",
    )

    items = FeedConnector().parse(source, ATOM_FIXTURE)

    assert len(items) == 1
    assert items[0].title == "Model release notes"
    assert items[0].url == "https://example.com/releases/model"


def test_feed_connector_parses_json_feed_fixture() -> None:
    source = SourceDefinition(
        source_id="json-feed",
        name="JSON Feed",
        source_type="rss",
        url="https://example.com/feed.json",
        reliability="high",
    )

    items = FeedConnector().parse(source, JSON_FEED_FIXTURE)

    assert len(items) == 1
    item = items[0]
    assert item.title == "JSON feed item"
    assert item.url == "https://example.com/json-feed-item?utm_source=x"
    assert item.published_at == datetime(2026, 5, 11, 2, 0, tzinfo=UTC)
    assert item.summary == "JSON feed summary."
    assert item.authors == ["Alice Example"]
    assert item.tags == ["ai", "release"]
    assert item.language == "en"
    assert item.metadata["feed_format"] == "json_feed"
    assert item.metadata["json_feed_item_id"] == "json-item-1"


def test_feed_connector_fetch_returns_structured_error() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(fetch_text=lambda url: (_ for _ in ()).throw(RuntimeError("boom")))

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].source_id == "rss-source"
    assert errors[0].source_name == "RSS Source"
    assert errors[0].error_type == "fetch_connection_error"
    assert errors[0].retryable is True
    assert errors[0].metadata["original_exception_type"] == "RuntimeError"
    assert errors[0].metadata["retryable"] is True


def test_feed_connector_fetch_returns_empty_response_error() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(fetch_text=lambda url: "  \n")

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "empty_source_response"
    assert errors[0].url == "https://example.com/rss.xml"
    assert errors[0].metadata["phase"] == "fetch"


def test_feed_connector_fetch_returns_empty_feed_error() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(
        fetch_text=lambda url: """<?xml version="1.0"?><rss version="2.0"><channel /></rss>"""
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "empty_feed"
    assert errors[0].metadata["phase"] == "parse"


def test_feed_connector_fetch_maps_parse_error() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(fetch_text=lambda url: "<rss")

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "invalid_feed"
    assert errors[0].metadata["phase"] == "parse"
    assert errors[0].metadata["retryable"] is False
    assert errors[0].retryable is False


def test_feed_connector_fetch_maps_timeout_error() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(fetch_text=lambda url: (_ for _ in ()).throw(URLError(TimeoutError("timed out"))))

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "fetch_timeout"
    assert errors[0].metadata["original_exception_type"] == "URLError"
    assert errors[0].metadata["retryable"] is True


@pytest.mark.parametrize(
    ("status_code", "expected_error_type", "expected_retryable"),
    [(404, "fetch_http_4xx", False), (429, "fetch_http_4xx", True), (503, "fetch_http_5xx", True)],
)
def test_feed_connector_fetch_maps_http_errors(
    status_code: int,
    expected_error_type: str,
    expected_retryable: bool,
) -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(
        fetch_text=lambda url: (_ for _ in ()).throw(
            HTTPError(url, status_code, "http error", hdrs=None, fp=BytesIO(b""))
        )
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == expected_error_type
    assert errors[0].metadata["status_code"] == status_code
    assert errors[0].metadata["retryable"] is expected_retryable


def test_feed_connector_default_fetch_applies_policy(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            captured["read_size"] = size
            return b"abcdef"

    def fake_open_request(request, policy):
        captured["user_agent"] = request.headers["User-agent"]
        captured["timeout"] = policy.timeout_seconds
        return Response()

    monkeypatch.setattr("infrastructure.external.sources.feed.open_request_with_fetch_policy", fake_open_request)
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
        respect_robots=False,
        user_agent="SourceAgent/1.0",
    )
    connector = FeedConnector(
        fetch_policy=SourceFetchPolicy(timeout_seconds=3, max_bytes=5, user_agent="NewsRoomTest/1.0")
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "max_bytes_exceeded"
    assert "max_bytes" in errors[0].error_message
    assert captured == {
        "user_agent": "SourceAgent/1.0",
        "timeout": 3,
        "read_size": 6,
    }


def test_feed_connector_default_fetch_routes_robots_and_feed_through_injected_open_request() -> None:
    requests = []

    class Headers:
        def __init__(self, content_type):
            self._content_type = content_type

        def get_content_type(self):
            return self._content_type

        def get(self, name, default=None):
            return self._content_type if name.casefold() == "content-type" else default

    class Response:
        status = 200

        def __init__(self, body, content_type):
            self._body = body
            self.headers = Headers(content_type)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            return self._body[:size]

    def fake_open_request(request, policy):
        requests.append((request.full_url, request.headers["User-agent"], policy.max_redirects))
        if request.full_url.endswith("/robots.txt"):
            return Response(b"User-agent: *\nAllow: /\n", "text/plain")
        return Response(RSS_FIXTURE.encode("utf-8"), "application/rss+xml")

    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(
        fetch_policy=SourceFetchPolicy(max_redirects=2, user_agent="NewsRoomTest/1.0"),
        open_request=fake_open_request,
    )

    items, errors = connector.fetch(source)

    assert errors == []
    assert [item.title for item in items] == ["AI chip export update"]
    assert requests == [
        ("https://example.com/robots.txt", "NewsRoomTest/1.0", 2),
        ("https://example.com/rss.xml", "NewsRoomTest/1.0", 2),
    ]


def test_feed_connector_default_fetch_attaches_response_metadata(monkeypatch) -> None:
    class Headers:
        def get_content_type(self):
            return "application/rss+xml"

        def items(self):
            return [
                ("Content-Type", "application/rss+xml"),
                ("Cache-Control", "max-age=60"),
            ]

    class Response:
        status = 200
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def geturl(self):
            return "https://example.com/rss.xml"

        def read(self, size):
            return RSS_FIXTURE.encode("utf-8")

    def fake_open_request(request, policy):
        return Response()

    monkeypatch.setattr("infrastructure.external.sources.feed.open_request_with_fetch_policy", fake_open_request)
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
        respect_robots=False,
    )

    items, errors = FeedConnector().fetch(source, limit=1)

    assert errors == []
    assert len(items) == 1
    response = items[0].metadata["fetch_response"]
    assert response["status_code"] == 200
    assert response["content_type"] == "application/rss+xml"
    assert response["url"] == "https://example.com/rss.xml"
    assert response["headers"]["Content-Type"] == "application/rss+xml"
    assert response["headers"]["Cache-Control"] == "max-age=60"


def test_feed_connector_keeps_response_metadata_request_scoped(monkeypatch) -> None:
    second_parse_started = threading.Event()

    class Headers:
        def get_content_type(self):
            return "application/rss+xml"

        def items(self):
            return [("Content-Type", "application/rss+xml")]

    class Response:
        status = 200
        headers = Headers()

        def __init__(self, url: str) -> None:
            self._url = url

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def geturl(self):
            return self._url

        def read(self, size):
            if "a.example" in self._url:
                assert second_parse_started.wait(timeout=2)
            return RSS_FIXTURE.encode("utf-8")

    def fake_open_request(request, policy):
        return Response(request.full_url)

    connector = FeedConnector()
    original_parse = connector.parse

    def interleaved_parse(source, xml_text, *, limit=None):
        if "b.example" in source.url:
            second_parse_started.set()
        return original_parse(source, xml_text, limit=limit)

    monkeypatch.setattr(
        "infrastructure.external.sources.feed.open_request_with_fetch_policy",
        fake_open_request,
    )
    monkeypatch.setattr(connector, "parse", interleaved_parse)
    sources = [
        SourceDefinition(
            source_id="source-a",
            name="Source A",
            source_type="rss",
            url="https://a.example/rss.xml",
            respect_robots=False,
        ),
        SourceDefinition(
            source_id="source-b",
            name="Source B",
            source_type="rss",
            url="https://b.example/rss.xml",
            respect_robots=False,
        ),
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(connector.fetch, sources))

    for source, (items, errors) in zip(sources, results, strict=True):
        assert errors == []
        assert items[0].metadata["fetch_response"]["url"] == source.url


def test_feed_connector_default_fetch_rejects_unsupported_content_type(monkeypatch) -> None:
    class Headers:
        def get_content_type(self):
            return "text/html; charset=utf-8"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            return RSS_FIXTURE.encode("utf-8")

    def fake_open_request(request, policy):
        return Response()

    monkeypatch.setattr("infrastructure.external.sources.feed.open_request_with_fetch_policy", fake_open_request)
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
        respect_robots=False,
    )

    items, errors = FeedConnector().fetch(source)

    assert items == []
    assert errors[0].error_type == "unsupported_content_type"
    assert errors[0].metadata["phase"] == "fetch"
    assert errors[0].metadata["content_type"] == "text/html"
    assert errors[0].metadata["retryable"] is False
    assert errors[0].retryable is False
    assert errors[0].metadata["source_health_affecting"] is False
    assert "application/rss+xml" in errors[0].metadata["supported_content_types"]


def test_feed_connector_fetch_maps_redirect_limit_error() -> None:
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(
        fetch_text=lambda url: (_ for _ in ()).throw(
            TooManyRedirectsError("https://example.com/loop", max_redirects=1)
        )
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "too_many_redirects"
    assert errors[0].metadata["phase"] == "fetch"
    assert errors[0].metadata["redirect_url"] == "https://example.com/loop"
    assert errors[0].metadata["max_redirects"] == 1
    assert errors[0].metadata["retryable"] is False
    assert errors[0].metadata["source_health_affecting"] is False


def test_feed_connector_default_fetch_respects_robots_txt() -> None:
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
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(RSS_FIXTURE.encode("utf-8"))

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = SourceDefinition(
            source_id="rss-source",
            name="RSS Source",
            source_type="rss",
            url=f"http://127.0.0.1:{server.server_port}/blocked/rss.xml",
        )

        items, errors = FeedConnector().fetch(source)
    finally:
        server.shutdown()
        server.server_close()

    assert items == []
    assert errors[0].error_type == "robots_disallowed"
    assert errors[0].metadata["phase"] == "fetch"
    assert errors[0].metadata["robots_url"].endswith("/robots.txt")
    assert errors[0].metadata["retryable"] is False
    assert errors[0].metadata["source_health_affecting"] is False
    assert requests == ["/robots.txt"]


def test_feed_connector_skips_robots_when_source_disables_it() -> None:
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
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(RSS_FIXTURE.encode("utf-8"))

        def log_message(self, format, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        source = SourceDefinition(
            source_id="rss-source",
            name="RSS Source",
            source_type="rss",
            url=f"http://127.0.0.1:{server.server_port}/blocked/rss.xml",
            respect_robots=False,
        )

        items, errors = FeedConnector().fetch(source)
    finally:
        server.shutdown()
        server.server_close()

    assert len(items) == 1
    assert errors == []
    assert requests == ["/blocked/rss.xml"]


def test_feed_connector_retries_transient_fetch_error() -> None:
    calls = []
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )

    def fetch_text(url: str) -> str:
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 503, "unavailable", hdrs=None, fp=BytesIO(b""))
        return RSS_FIXTURE

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(retry_times=1),
    )

    items, errors = connector.fetch(source)

    assert len(items) == 1
    assert errors == []
    assert calls == ["https://example.com/rss.xml", "https://example.com/rss.xml"]


def test_feed_connector_does_not_retry_unconfigured_http_4xx() -> None:
    calls = []
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )

    def fetch_text(url: str) -> str:
        calls.append(url)
        raise HTTPError(url, 404, "missing", hdrs=None, fp=BytesIO(b""))

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(retry_times=2),
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "fetch_http_4xx"
    assert errors[0].metadata["attempts"] == 1
    assert calls == ["https://example.com/rss.xml"]


def test_feed_connector_reports_attempts_after_retry_exhaustion() -> None:
    calls = []
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )

    def fetch_text(url: str) -> str:
        calls.append(url)
        raise HTTPError(url, 503, "unavailable", hdrs=None, fp=BytesIO(b""))

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(retry_times=2),
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert errors[0].error_type == "fetch_http_5xx"
    assert errors[0].metadata["attempts"] == 3
    assert errors[0].metadata["retryable"] is True
    assert calls == [
        "https://example.com/rss.xml",
        "https://example.com/rss.xml",
        "https://example.com/rss.xml",
    ]


def test_feed_connector_rate_limits_same_domain_before_fetch() -> None:
    calls = []
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    same_domain = SourceDefinition(
        source_id="rss-source-2",
        name="RSS Source 2",
        source_type="rss",
        url="https://example.com/other.xml",
    )
    connector = FeedConnector(
        fetch_text=lambda url: calls.append(url) or RSS_FIXTURE,
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
        rate_limiter=DomainRateLimiter(now=lambda: datetime(2026, 5, 11, tzinfo=UTC)),
    )

    first_items, first_errors = connector.fetch(source)
    second_items, second_errors = connector.fetch(same_domain)

    assert len(first_items) == 1
    assert first_errors == []
    assert second_items == []
    assert second_errors[0].error_type == "rate_limited"
    assert second_errors[0].url == "https://example.com/other.xml"
    assert second_errors[0].source_name == "RSS Source 2"
    assert second_errors[0].retryable is True
    assert second_errors[0].metadata["domain"] == "example.com"
    assert second_errors[0].metadata["retryable"] is True
    assert second_errors[0].metadata["source_health_affecting"] is False
    assert calls == ["https://example.com/rss.xml"]


def test_feed_connector_rate_limit_is_per_domain() -> None:
    calls = []
    now = datetime(2026, 5, 11, tzinfo=UTC)
    connector = FeedConnector(
        fetch_text=lambda url: calls.append(url) or RSS_FIXTURE,
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
        rate_limiter=DomainRateLimiter(now=lambda: now),
    )
    first = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    other = SourceDefinition(
        source_id="rss-other",
        name="Other RSS",
        source_type="rss",
        url="https://other.example/rss.xml",
    )

    first_items, first_errors = connector.fetch(first)
    other_items, other_errors = connector.fetch(other)

    assert len(first_items) == 1
    assert first_errors == []
    assert len(other_items) == 1
    assert other_errors == []
    assert calls == ["https://example.com/rss.xml", "https://other.example/rss.xml"]


def test_feed_connector_rate_limit_window_resets() -> None:
    calls = []
    clock = {"now": datetime(2026, 5, 11, tzinfo=UTC)}
    source = SourceDefinition(
        source_id="rss-source",
        name="RSS Source",
        source_type="rss",
        url="https://example.com/rss.xml",
    )
    connector = FeedConnector(
        fetch_text=lambda url: calls.append(url) or RSS_FIXTURE,
        fetch_policy=SourceFetchPolicy(rate_limit_per_domain_per_minute=1),
        rate_limiter=DomainRateLimiter(now=lambda: clock["now"]),
    )

    first_items, first_errors = connector.fetch(source)
    blocked_items, blocked_errors = connector.fetch(source)
    clock["now"] = clock["now"] + timedelta(seconds=61)
    reset_items, reset_errors = connector.fetch(source)

    assert len(first_items) == 1
    assert first_errors == []
    assert blocked_items == []
    assert blocked_errors[0].error_type == "rate_limited"
    assert len(reset_items) == 1
    assert reset_errors == []
    assert calls == ["https://example.com/rss.xml", "https://example.com/rss.xml"]


def test_source_fetch_policy_rejects_invalid_values() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        SourceFetchPolicy(timeout_seconds=0)
    with pytest.raises(ValueError, match="max_bytes"):
        SourceFetchPolicy(max_bytes=0)
    with pytest.raises(ValueError, match="max_redirects"):
        SourceFetchPolicy(max_redirects=-1)
    with pytest.raises(ValueError, match="user_agent"):
        SourceFetchPolicy(user_agent="")
    with pytest.raises(ValueError, match="rate_limit_per_domain_per_minute"):
        SourceFetchPolicy(rate_limit_per_domain_per_minute=0)
    with pytest.raises(ValueError, match="retry_times"):
        SourceFetchPolicy(retry_times=-1)
    with pytest.raises(ValueError, match="retry_on_status_codes"):
        SourceFetchPolicy(retry_on_status_codes=(99,))
