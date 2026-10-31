"""The guarded opener against a live local fault server."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from infrastructure.external.egress import EgressDenied, EgressPolicy, TooManyRedirectsError, open_guarded
from infrastructure.external.fetch_deadline import FetchDeadlineExceeded
from infrastructure.external.sources.fetch_policy import (
    SourceFetchPolicy,
    ensure_robots_allowed,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
)
from tests.tools.harness.fault_http import Delay, FaultHTTPServer, Hang, Redirect, Respond, Trickle

LOOPBACK = ("127.0.0.0/8", "::1/128")


@pytest.fixture
def server() -> Iterator[FaultHTTPServer]:
    with FaultHTTPServer() as fault:
        yield fault


class _Resolver:
    """Map names to fixed answers and record every lookup."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers
        self.lookups: list[str] = []

    def __call__(self, host: str, port: int) -> list[tuple]:
        self.lookups.append(host)
        address = self.answers[host]
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]


def _open(url: str, policy: EgressPolicy, resolver=None, *, timeout: float = 2.0, **request):
    options = {"timeout_seconds": timeout}
    if resolver is not None:
        options["resolver"] = resolver
    return open_guarded(Request(url, **request), policy, **options)


def test_connection_goes_to_the_vetted_address_and_keeps_the_host_header(server: FaultHTTPServer) -> None:
    server.route("/feed", Respond(200, "ok"))
    resolver = _Resolver({"news.example": "127.0.0.1"})

    with _open(server.url("/feed", host="news.example"), EgressPolicy(allowed_private_networks=LOOPBACK), resolver) as response:
        assert response.read() == b"ok"

    assert resolver.lookups == ["news.example"]
    assert server.requests_for("/feed")[0].header("Host") == f"news.example:{server.port}"


def test_loopback_is_refused_unless_explicitly_allowed(server: FaultHTTPServer) -> None:
    server.route("/feed", Respond(200, "secret"))
    with pytest.raises(EgressDenied, match="non-public"):
        _open(server.url("/feed"), EgressPolicy())
    assert server.requests == []


def test_redirect_to_cloud_metadata_is_refused_before_it_is_requested(server: FaultHTTPServer) -> None:
    server.route("/start", Redirect("http://169.254.169.254/latest/meta-data/"))
    with pytest.raises(EgressDenied, match="169.254.169.254"):
        _open(server.url("/start"), EgressPolicy(allowed_private_networks=LOOPBACK))
    assert [request.path for request in server.requests] == ["/start"]


def test_every_hop_is_resolved_and_vetted_again(server: FaultHTTPServer) -> None:
    server.route("/start", Redirect(server.url("/next", host="rebind.example")))
    resolver = _Resolver({"news.example": "127.0.0.1", "rebind.example": "169.254.169.254"})

    with pytest.raises(EgressDenied, match="169.254.169.254"):
        _open(server.url("/start", host="news.example"), EgressPolicy(allowed_private_networks=LOOPBACK), resolver)
    assert resolver.lookups == ["news.example", "rebind.example"]


def test_redirect_cannot_leave_allowed_domains(server: FaultHTTPServer) -> None:
    server.route("/start", Redirect(server.url("/landing", host="evil.example")))
    resolver = _Resolver({"news.example": "127.0.0.1", "evil.example": "127.0.0.1"})
    policy = EgressPolicy(allowed_domains=("news.example",), allowed_private_networks=LOOPBACK)

    with pytest.raises(EgressDenied, match="allowed_domains"):
        _open(server.url("/start", host="news.example"), policy, resolver)
    assert server.requests_for("/landing") == []


def test_cross_origin_redirect_drops_credentials_same_origin_keeps_them(server: FaultHTTPServer) -> None:
    server.route("/cross", Redirect(server.url("/landing", host="cdn.example")))
    server.route("/same", Redirect("/landing"))
    server.route("/landing", Respond(200, "ok"))
    resolver = _Resolver({"api.example": "127.0.0.1", "cdn.example": "127.0.0.1"})
    policy = EgressPolicy(allowed_private_networks=LOOPBACK)
    headers = {"Authorization": "Bearer ghp_secret", "Cookie": "session=1", "X-Trace": "t-1"}

    for path in ("/cross", "/same"):
        with _open(server.url(path, host="api.example"), policy, resolver, headers=headers) as response:
            response.read()

    cross, same = server.requests_for("/landing")
    assert cross.header("Authorization") is None and cross.header("Cookie") is None
    assert cross.header("X-Trace") == "t-1"
    assert same.header("Authorization") == "Bearer ghp_secret"


def test_redirect_limit_counts_every_hop(server: FaultHTTPServer) -> None:
    server.route("/a", Redirect("/b"))
    server.route("/b", Redirect("/a"))
    with pytest.raises(TooManyRedirectsError) as error:
        _open(server.url("/a"), EgressPolicy(allowed_private_networks=LOOPBACK, max_redirects=3))
    assert error.value.max_redirects == 3
    assert len(server.requests) == 4


def test_post_is_never_replayed_to_a_307_target(server: FaultHTTPServer) -> None:
    server.route("/hook", Redirect(server.url("/elsewhere"), status=307))
    with pytest.raises(HTTPError) as error:
        _open(server.url("/hook"), EgressPolicy(allowed_private_networks=LOOPBACK), data=b'{"x":1}', method="POST")
    assert error.value.code == 307
    error.value.close()
    assert server.requests_for("/elsewhere") == []


def test_black_holed_first_address_leaves_time_for_the_next(server: FaultHTTPServer) -> None:
    # 192.0.2.1 (TEST-NET-1) never answers, like a broken IPv6 route; the
    # working address must still get its share of the budget.
    server.route("/feed", Respond(200, "ok"))

    def resolve(host: str, port: int) -> list[tuple]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
        ]

    policy = EgressPolicy(allowed_private_networks=(*LOOPBACK, "192.0.2.0/24"))
    started = time.monotonic()
    with _open(server.url("/feed", host="dual.example"), policy, resolve, timeout=2.0) as response:
        assert response.read() == b"ok"
    assert time.monotonic() - started < 1.5


def test_hanging_upstream_ends_at_the_budget(server: FaultHTTPServer) -> None:
    server.route("/hang", Hang())
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        _open(server.url("/hang"), EgressPolicy(allowed_private_networks=LOOPBACK), timeout=0.5)
    assert time.monotonic() - started < 1.2


def test_slow_drip_that_beats_every_socket_timeout_still_ends_at_the_budget(server: FaultHTTPServer) -> None:
    # Each byte arrives within the socket timeout; only the whole-fetch budget stops it.
    server.route("/drip", Trickle("x" * 40, chunk_size=1, interval_seconds=0.15))
    started = time.monotonic()
    with pytest.raises(FetchDeadlineExceeded):
        with _open(server.url("/drip"), EgressPolicy(allowed_private_networks=LOOPBACK), timeout=1.0) as response:
            response.read()
    assert 0.8 <= time.monotonic() - started < 1.8


def _watchdogs() -> int:
    return sum(thread.name == "egress-deadline-watchdog" for thread in threading.enumerate())


def test_http_error_and_closed_responses_release_their_watchdog(server: FaultHTTPServer) -> None:
    server.route("/missing", Respond(404, "nope"))
    server.route("/ok", Respond(200, "ok"))
    before = _watchdogs()
    policy = EgressPolicy(allowed_private_networks=LOOPBACK)

    with pytest.raises(HTTPError) as error:
        _open(server.url("/missing"), policy, timeout=30.0)
    assert error.value.read() == b"nope"
    with _open(server.url("/ok"), policy, timeout=30.0) as response:
        response.read()

    deadline = time.monotonic() + 1.0
    while _watchdogs() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _watchdogs() == before


def test_environment_proxies_are_not_used(server: FaultHTTPServer, monkeypatch: pytest.MonkeyPatch) -> None:
    server.route("/feed", Respond(200, "direct"))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    with _open(server.url("/feed"), EgressPolicy(allowed_private_networks=LOOPBACK)) as response:
        assert response.read() == b"direct"


def _fetch(url: str, policy: SourceFetchPolicy) -> bytes:
    with open_request_with_fetch_policy(Request(url), policy) as response:
        return response.read()


def test_retries_stop_when_the_whole_fetch_budget_is_spent(server: FaultHTTPServer) -> None:
    server.route("/busy", Delay(0.4, Respond(503, "busy")))
    policy = SourceFetchPolicy(
        timeout_seconds=1.0,
        retry_times=5,
        respect_robots=False,
        allowed_private_networks=LOOPBACK,
    )
    started = time.monotonic()
    with pytest.raises((HTTPError, FetchDeadlineExceeded)):
        run_with_fetch_retries(lambda: _fetch(server.url("/busy"), policy), policy)

    assert time.monotonic() - started < 1.6
    assert 2 <= len(server.requests_for("/busy")) <= 3


def test_retry_recovers_inside_the_budget(server: FaultHTTPServer) -> None:
    server.route("/flaky", Respond(503, "busy"), Respond(200, "ok"))
    policy = SourceFetchPolicy(timeout_seconds=2.0, retry_times=1, respect_robots=False, allowed_private_networks=LOOPBACK)

    assert run_with_fetch_retries(lambda: _fetch(server.url("/flaky"), policy), policy) == b"ok"
    assert len(server.requests_for("/flaky")) == 2


def test_robots_lookup_goes_through_the_same_guard(server: FaultHTTPServer) -> None:
    server.route("/robots.txt", Redirect("http://169.254.169.254/robots.txt"))
    policy = SourceFetchPolicy(timeout_seconds=2.0, respect_robots=True, allowed_private_networks=LOOPBACK)

    with pytest.raises(EgressDenied, match="169.254.169.254"):
        ensure_robots_allowed(server.url("/feed"), policy)
