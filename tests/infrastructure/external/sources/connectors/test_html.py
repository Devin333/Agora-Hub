from datetime import UTC, datetime
from io import BytesIO
from urllib.error import HTTPError

from backend.foundation.models.source import SourceDefinition
from infrastructure.external.sources import HtmlConnector, SourceFetchPolicy, extract_html


HTML_FIXTURE = """<!doctype html>
<html lang="en">
  <head>
    <title> Product Launch Notes </title>
    <link rel="canonical" href="https://example.com/blog/product-launch" />
    <meta name="description" content="Launch summary for the new product." />
    <meta property="article:published_time" content="2026-05-11T10:30:00Z" />
    <meta name="author" content="Alice Example; Bob Example" />
  </head>
  <body>
    <nav>Ignore navigation links</nav>
    <article>
      <h1>Product Launch Notes</h1>
      <p>The product launch introduces a production workflow for source processing.</p>
      <p>It includes extraction confidence, canonical URLs, and metadata capture.</p>
    </article>
    <footer>Ignore footer</footer>
  </body>
</html>
"""


def test_extract_html_returns_metadata_and_visible_text() -> None:
    result = extract_html(HTML_FIXTURE)

    assert result.title == "Product Launch Notes"
    assert result.canonical_url == "https://example.com/blog/product-launch"
    assert result.summary == "Launch summary for the new product."
    assert result.published_at == datetime(2026, 5, 11, 10, 30, tzinfo=UTC)
    assert result.authors == ["Alice Example", "Bob Example"]
    assert result.language == "en"
    assert "production workflow" in (result.text or "")
    assert "Ignore navigation" not in (result.text or "")
    assert result.confidence >= 0.9
    assert result.attempted_extractors == ("stdlib_html_extractor",)


def test_html_connector_parses_html_into_raw_source_item() -> None:
    source = _source()

    items = HtmlConnector().parse(source, HTML_FIXTURE)

    assert len(items) == 1
    item = items[0]
    assert item.source_type.value == "html"
    assert item.title == "Product Launch Notes"
    assert item.url == "https://example.com/blog/product-launch"
    assert item.published_at == datetime(2026, 5, 11, 10, 30, tzinfo=UTC)
    assert item.authors == ["Alice Example", "Bob Example"]
    assert item.language == "en"
    assert "canonical URLs" in (item.raw_content or "")
    assert item.metadata["extractor_name"] == "stdlib_html_extractor"
    assert item.metadata["attempted_extractors"] == ["stdlib_html_extractor"]
    assert item.metadata["extraction_confidence"] >= 0.9
    assert item.metadata["source_reliability"] == "high"


def test_html_connector_preserves_official_blog_source_type_and_metadata() -> None:
    source = SourceDefinition(
        source_id="official-blog",
        name="Official Blog",
        source_type="official_blog",
        url="https://example.com/blog/product-launch",
        reliability="high",
    )

    items = HtmlConnector().parse(source, HTML_FIXTURE)

    assert len(items) == 1
    assert items[0].source_type.value == "official_blog"
    assert items[0].metadata["official_blog"] is True
    assert items[0].metadata["source_kind"] == "official_blog"


def test_html_connector_falls_back_to_source_name_and_url_for_minimal_html() -> None:
    source = _source()
    html = "<html><body><p>Only visible body text is available here.</p></body></html>"

    items = HtmlConnector().parse(source, html)

    assert len(items) == 1
    assert items[0].title == "Example Blog"
    assert items[0].url == "https://example.com/blog/product-launch"
    assert items[0].summary == "Only visible body text is available here."
    assert items[0].metadata["extraction_confidence"] > 0


def test_html_connector_normalizes_relative_canonical_url() -> None:
    source = SourceDefinition(
        source_id="blog",
        name="Example Blog",
        source_type="html",
        url="https://Example.com/blog/index.html",
    )
    html = """<html><head>
      <title>Relative Canonical</title>
      <link rel="canonical" href="/blog/post?utm_source=x" />
    </head><body><p>Body text for a relative canonical page.</p></body></html>"""

    items = HtmlConnector().parse(source, html)

    assert len(items) == 1
    assert items[0].url == "https://example.com/blog/post"
    assert items[0].metadata["canonical_url"] == "https://example.com/blog/post"


def test_html_connector_rejects_malformed_canonical_as_structured_parse_error() -> None:
    source = _source()
    html = """
    <html><head>
      <title>Malformed canonical</title>
      <link rel="canonical" href="https://example.com:not-a-port/article" />
    </head><body><p>Body text remains parseable.</p></body></html>
    """
    connector = HtmlConnector(fetch_text=lambda _: html)

    items, errors = connector.fetch(source)

    assert items == []
    assert len(errors) == 1
    assert errors[0].error_type == "parse_error"
    assert errors[0].retryable is False
    assert errors[0].metadata["phase"] == "parse"
    assert errors[0].metadata["original_exception_type"] == "ValueError"


def test_html_connector_fetch_retries_transient_failure() -> None:
    calls = []

    def fetch_text(url: str) -> str:
        calls.append(url)
        if len(calls) == 1:
            raise HTTPError(url, 503, "unavailable", hdrs=None, fp=BytesIO(b""))
        return HTML_FIXTURE

    items, errors = HtmlConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(retry_times=1),
    ).fetch(_source())

    assert len(items) == 1
    assert errors == []
    assert calls == [
        "https://example.com/blog/product-launch",
        "https://example.com/blog/product-launch",
    ]


def test_html_connector_fetch_returns_empty_response_error() -> None:
    items, errors = HtmlConnector(fetch_text=lambda url: "  \n").fetch(_source())

    assert items == []
    assert errors[0].error_type == "empty_source_response"
    assert errors[0].metadata["phase"] == "fetch"


def test_html_connector_default_fetch_rejects_unsupported_content_type(monkeypatch) -> None:
    class Headers:
        def get_content_type(self):
            return "application/json"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size):
            return HTML_FIXTURE.encode("utf-8")

    def fake_open_request(request, policy):
        return Response()

    monkeypatch.setattr("infrastructure.external.sources.html.open_request_with_fetch_policy", fake_open_request)

    items, errors = HtmlConnector().fetch(_source(respect_robots=False))

    assert items == []
    assert errors[0].error_type == "unsupported_content_type"
    assert errors[0].metadata["phase"] == "fetch"
    assert errors[0].metadata["content_type"] == "application/json"
    assert errors[0].metadata["retryable"] is False
    assert errors[0].metadata["source_health_affecting"] is False
    assert "text/html" in errors[0].metadata["supported_content_types"]


def test_html_connector_default_fetch_routes_robots_and_page_through_injected_open_request() -> None:
    requested_urls = []

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
        requested_urls.append(request.full_url)
        if request.full_url.endswith("/robots.txt"):
            return Response(b"User-agent: *\nAllow: /\n", "text/plain")
        return Response(HTML_FIXTURE.encode("utf-8"), "text/html")

    items, errors = HtmlConnector(open_request=fake_open_request).fetch(_source())

    assert errors == []
    assert [item.title for item in items] == ["Product Launch Notes"]
    assert requested_urls == [
        "https://example.com/robots.txt",
        "https://example.com/blog/product-launch",
    ]


def _source(*, respect_robots: bool = True) -> SourceDefinition:
    return SourceDefinition(
        source_id="blog",
        name="Example Blog",
        source_type="html",
        url="https://example.com/blog/product-launch",
        reliability="high",
        authority_score=0.8,
        respect_robots=respect_robots,
    )
