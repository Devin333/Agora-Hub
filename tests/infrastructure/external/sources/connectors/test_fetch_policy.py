from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from infrastructure.external.sources.feed import FeedConnector
from infrastructure.external.sources.fetch_policy import (
    DomainRateLimiter,
    RateLimitDecision,
    RobotsDisallowedError,
    SourceRateLimitExceededError,
    SourceFetchPolicy,
    TooManyRedirectsError,
    decide_source_fetch_retry,
    effective_fetch_policy,
    ensure_robots_allowed,
    fetch_attempts,
    fetch_retry_decision,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
    source_domain_key,
)
from infrastructure.external.sources.models import SourceDefinition


UTC = timezone.utc
RSS_FIXTURE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example RSS</title>
    <item>
      <title>Policy update</title>
      <link>https://example.com/items/1</link>
    </item>
  </channel>
</rss>
"""


def _http_error(status_code: int) -> HTTPError:
    return HTTPError(
        f"https://example.com/status/{status_code}",
        status_code,
        "HTTP error",
        hdrs=None,
        fp=BytesIO(b""),
    )


def test_open_request_with_fetch_policy_enforces_redirect_limit() -> None:
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
        request = Request(f"http://127.0.0.1:{server.server_port}/start")

        with pytest.raises(TooManyRedirectsError) as exc_info:
            open_request_with_fetch_policy(request, SourceFetchPolicy(max_redirects=1))

        assert exc_info.value.max_redirects == 1
        assert exc_info.value.url.endswith("/done")

        with open_request_with_fetch_policy(
            Request(f"http://127.0.0.1:{server.server_port}/start"),
            SourceFetchPolicy(max_redirects=2),
        ) as response:
            assert response.read() == b"done"
    finally:
        server.shutdown()
        server.server_close()


def test_effective_fetch_policy_applies_source_user_agent_and_robots_policy() -> None:
    source = SourceDefinition(
        source_id="source",
        name="Source",
        source_type="rss",
        url="https://example.com/rss.xml",
        respect_robots=False,
        user_agent="SourceAgent/1.0",
    )

    policy = effective_fetch_policy(SourceFetchPolicy(user_agent="DefaultAgent/1.0"), source)

    assert policy.user_agent == "SourceAgent/1.0"
    assert policy.respect_robots is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Example.COM/path?query=1#fragment", "example.com"),
        ("http://user:password@EXAMPLE.com:8080/path", "example.com"),
        ("https://example.com:443/other", "example.com"),
        ("https://[2001:DB8::1]:8443/path", "2001:db8::1"),
        ("https://export.arxiv.org/api/query", "arxiv.org"),
        ("https://arxiv.org/pdf/2607.00001", "arxiv.org"),
    ],
)
def test_source_domain_key_uses_canonical_provider_hostname(url: str, expected: str) -> None:
    assert source_domain_key(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "example.com/path",
        "ftp://example.com/file",
        "https:///missing-host",
        "https://example.com:not-a-port/path",
        "https://[2001:db8::1/path",
    ],
)
def test_source_domain_key_rejects_missing_or_malformed_http_hostname(url: str) -> None:
    with pytest.raises(ValueError, match="source.*URL|source rate-limit URL"):
        source_domain_key(url)


def test_domain_rate_limiter_reservations_are_atomic_under_concurrency() -> None:
    limit = 7
    request_count = 32
    barrier = threading.Barrier(request_count)
    limiter = DomainRateLimiter(now=lambda: datetime(2026, 7, 19, tzinfo=UTC))

    def reserve(index: int) -> RateLimitDecision:
        barrier.wait()
        return limiter.reserve(
            f"https://user:secret@EXAMPLE.com:8443/items/{index}?request={index}",
            limit_per_minute=limit,
        )

    with ThreadPoolExecutor(max_workers=request_count) as executor:
        decisions = list(executor.map(reserve, range(request_count)))

    allowed = [decision for decision in decisions if decision.allowed]
    denied = [decision for decision in decisions if not decision.allowed]
    assert len(allowed) == limit
    assert len(denied) == request_count - limit
    assert {decision.domain for decision in decisions} == {"example.com"}
    assert {decision.retry_after_seconds for decision in denied} == {60}


@pytest.mark.parametrize(
    (
        "exception",
        "retry_statuses",
        "retry_times",
        "attempts",
        "expected_retryable",
        "expected_should_retry",
        "expected_status",
        "expected_remaining",
    ),
    [
        (_http_error(404), (404,), 2, 1, True, True, 404, 2),
        (_http_error(503), (429,), 2, 1, False, False, 503, 2),
        (TimeoutError("timed out"), (), 1, 1, True, True, None, 1),
        (URLError(TimeoutError("timed out")), (), 1, 1, True, True, None, 1),
        (URLError("connection reset"), (), 1, 1, True, True, None, 1),
        (ValueError("invalid policy input"), (), 3, 1, False, False, None, 3),
        (RuntimeError("connection reset"), (), 2, 1, True, True, None, 2),
        (RuntimeError("no retry budget"), (), 0, 1, True, False, None, 0),
        (RuntimeError("budget exhausted"), (), 2, 3, True, False, None, 0),
        (
            RobotsDisallowedError(
                "https://example.com/private",
                "https://example.com/robots.txt",
                "NewsRoomTest/1.0",
            ),
            (),
            2,
            1,
            False,
            False,
            None,
            2,
        ),
    ],
)
def test_source_fetch_retry_decision_matrix(
    exception: Exception,
    retry_statuses: tuple[int, ...],
    retry_times: int,
    attempts: int,
    expected_retryable: bool,
    expected_should_retry: bool,
    expected_status: int | None,
    expected_remaining: int,
) -> None:
    policy = SourceFetchPolicy(
        retry_times=retry_times,
        retry_on_status_codes=retry_statuses,
    )

    decision = decide_source_fetch_retry(exception, policy, attempts=attempts)

    assert decision.attempts == attempts
    assert decision.max_attempts == retry_times + 1
    assert decision.retryable is expected_retryable
    assert decision.should_retry is expected_should_retry
    assert decision.status_code == expected_status
    assert decision.remaining_attempts == expected_remaining


def test_run_with_fetch_retries_honors_configured_404() -> None:
    calls = 0

    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _http_error(404)
        return "ok"

    result = run_with_fetch_retries(
        operation,
        SourceFetchPolicy(retry_times=1, retry_on_status_codes=(404,)),
    )

    assert result == "ok"
    assert calls == 2


def test_run_with_fetch_retries_stops_on_disabled_503() -> None:
    calls = 0
    error = _http_error(503)

    def operation() -> str:
        nonlocal calls
        calls += 1
        raise error

    policy = SourceFetchPolicy(retry_times=3, retry_on_status_codes=(429,))
    with pytest.raises(HTTPError) as exc_info:
        run_with_fetch_retries(operation, policy)

    assert exc_info.value is error
    assert calls == 1
    assert fetch_attempts(error) == 1
    assert fetch_retry_decision(error) == decide_source_fetch_retry(error, policy)


def test_run_with_fetch_retries_attaches_exhausted_budget_decision() -> None:
    calls = 0
    error = RuntimeError("temporary connection failure")

    def operation() -> None:
        nonlocal calls
        calls += 1
        raise error

    with pytest.raises(RuntimeError) as exc_info:
        run_with_fetch_retries(operation, SourceFetchPolicy(retry_times=2))

    decision = fetch_retry_decision(exc_info.value)
    assert calls == 3
    assert fetch_attempts(exc_info.value) == 3
    assert decision is not None
    assert decision.attempts == 3
    assert decision.max_attempts == 3
    assert decision.retryable is True
    assert decision.should_retry is False
    assert decision.remaining_attempts == 0


def test_invalid_policy_validation_invokes_no_operation_or_synthetic_attempt() -> None:
    calls = 0

    def operation() -> None:
        nonlocal calls
        calls += 1

    with pytest.raises(ValueError) as exc_info:
        policy = SourceFetchPolicy(retry_times=-1)
        run_with_fetch_retries(operation, policy)

    assert calls == 0
    assert fetch_attempts(exc_info.value) is None
    assert fetch_retry_decision(exc_info.value) is None


def test_feed_parse_failure_is_outside_fetch_retry_boundary() -> None:
    calls = 0

    def fetch_text(url: str) -> str:
        nonlocal calls
        calls += 1
        return "<rss"

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(retry_times=3),
    )
    source = SourceDefinition(
        source_id="feed",
        name="Feed",
        source_type="rss",
        url="https://example.com/feed.xml",
    )

    items, errors = connector.fetch(source)

    assert items == []
    assert calls == 1
    assert errors[0].error_type == "invalid_feed"
    assert "attempts" not in errors[0].metadata


def test_logical_fetch_reserves_quota_once_across_retries() -> None:
    class CountingRateLimiter:
        def __init__(self) -> None:
            self.delegate = DomainRateLimiter(now=lambda: datetime(2026, 7, 19, tzinfo=UTC))
            self.calls = 0

        def reserve(self, url: str, *, limit_per_minute: int | None) -> RateLimitDecision:
            self.calls += 1
            return self.delegate.reserve(url, limit_per_minute=limit_per_minute)

    limiter = CountingRateLimiter()
    fetch_calls = 0

    def fetch_text(url: str) -> str:
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls < 3:
            raise _http_error(503)
        return RSS_FIXTURE

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(
            retry_times=2,
            rate_limit_per_domain_per_minute=1,
        ),
        rate_limiter=limiter,
    )
    source = SourceDefinition(
        source_id="feed",
        name="Feed",
        source_type="rss",
        url="https://example.com/feed.xml",
    )

    items, errors = connector.fetch(source)

    assert len(items) == 1
    assert errors == []
    assert limiter.calls == 1
    assert fetch_calls == 3


def test_failed_logical_fetch_keeps_one_reservation_across_retries() -> None:
    class CountingRateLimiter:
        def __init__(self) -> None:
            self.delegate = DomainRateLimiter(
                now=lambda: datetime(2026, 7, 19, tzinfo=UTC)
            )
            self.calls = 0

        def reserve(
            self,
            url: str,
            *,
            limit_per_minute: int | None,
        ) -> RateLimitDecision:
            self.calls += 1
            return self.delegate.reserve(
                url,
                limit_per_minute=limit_per_minute,
            )

    limiter = CountingRateLimiter()
    fetch_calls = 0

    def fetch_text(_url: str) -> str:
        nonlocal fetch_calls
        fetch_calls += 1
        raise _http_error(503)

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(
            retry_times=2,
            rate_limit_per_domain_per_minute=1,
        ),
        rate_limiter=limiter,
    )
    first = SourceDefinition(
        source_id="first",
        name="First",
        source_type="rss",
        url="https://example.com/first.xml",
    )
    second = SourceDefinition(
        source_id="second",
        name="Second",
        source_type="rss",
        url="https://example.com/second.xml",
    )

    first_items, first_errors = connector.fetch(first)
    second_items, second_errors = connector.fetch(second)

    assert first_items == []
    assert first_errors[0].error_type == "fetch_http_5xx"
    assert first_errors[0].metadata["attempts"] == 3
    assert second_items == []
    assert second_errors[0].error_type == "rate_limited"
    assert limiter.calls == 2
    assert fetch_calls == 3


def test_rate_limit_denial_does_not_fetch_or_consume_retry_budget() -> None:
    fetch_calls = 0

    def fetch_text(url: str) -> str:
        nonlocal fetch_calls
        fetch_calls += 1
        return RSS_FIXTURE

    connector = FeedConnector(
        fetch_text=fetch_text,
        fetch_policy=SourceFetchPolicy(
            retry_times=3,
            rate_limit_per_domain_per_minute=1,
        ),
        rate_limiter=DomainRateLimiter(now=lambda: datetime(2026, 7, 19, tzinfo=UTC)),
    )
    first = SourceDefinition(
        source_id="first",
        name="First",
        source_type="rss",
        url="https://example.com/first.xml",
    )
    second = SourceDefinition(
        source_id="second",
        name="Second",
        source_type="rss",
        url="https://example.com/second.xml",
    )

    first_items, first_errors = connector.fetch(first)
    denied_items, denied_errors = connector.fetch(second)

    assert len(first_items) == 1
    assert first_errors == []
    assert denied_items == []
    assert denied_errors[0].error_type == "rate_limited"
    assert "attempts" not in denied_errors[0].metadata
    assert fetch_calls == 1


def test_robots_denial_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def open_robots(request: Request, policy: SourceFetchPolicy) -> _RobotsResponse:
        nonlocal calls
        calls += 1
        return _RobotsResponse(b"User-agent: *\nDisallow: /private\n")

    monkeypatch.setattr(
        "infrastructure.external.sources.fetch_policy.open_request_with_fetch_policy",
        open_robots,
    )
    policy = SourceFetchPolicy(retry_times=2, user_agent="NewsRoomTest/1.0")

    with pytest.raises(RobotsDisallowedError) as exc_info:
        run_with_fetch_retries(
            lambda: ensure_robots_allowed("https://example.com/private/report", policy),
            policy,
        )

    decision = fetch_retry_decision(exc_info.value)
    assert calls == 1
    assert decision is not None
    assert decision.retryable is False
    assert decision.should_retry is False


def test_robots_transport_failure_uses_fetch_retry_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def open_robots(request: Request, policy: SourceFetchPolicy) -> _RobotsResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise URLError("connection reset")
        return _RobotsResponse(b"User-agent: *\nAllow: /\n")

    monkeypatch.setattr(
        "infrastructure.external.sources.fetch_policy.open_request_with_fetch_policy",
        open_robots,
    )
    policy = SourceFetchPolicy(retry_times=1, user_agent="NewsRoomTest/1.0")

    run_with_fetch_retries(
        lambda: ensure_robots_allowed("https://example.com/public/report", policy),
        policy,
    )

    assert calls == 2


def test_robots_check_uses_injected_open_request() -> None:
    requests = []

    def open_robots(request: Request, policy: SourceFetchPolicy) -> _RobotsResponse:
        requests.append((request.full_url, request.headers["User-agent"], policy.respect_robots))
        return _RobotsResponse(b"User-agent: *\nDisallow: /private\n")

    policy = SourceFetchPolicy(user_agent="NewsRoomTest/1.0")

    ensure_robots_allowed("https://example.com/public/report", policy, open_request=open_robots)
    with pytest.raises(RobotsDisallowedError):
        ensure_robots_allowed("https://example.com/private/report", policy, open_request=open_robots)

    assert requests == [
        ("https://example.com/robots.txt", "NewsRoomTest/1.0", False),
        ("https://example.com/robots.txt", "NewsRoomTest/1.0", False),
    ]


def test_typed_rate_limit_denial_preserves_canonical_decision() -> None:
    decision = RateLimitDecision(
        allowed=False,
        domain="example.com",
        limit_per_minute=2,
        retry_after_seconds=17,
    )

    error = SourceRateLimitExceededError("https://example.com/feed.xml", decision)

    assert error.url == "https://example.com/feed.xml"
    assert error.decision is decision
    assert error.domain == "example.com"
    assert error.limit_per_minute == 2
    assert error.window_seconds == 60
    assert error.retry_after_seconds == 17


class _RobotsResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _RobotsResponse:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def read(self, size: int) -> bytes:
        return self._body[:size]
