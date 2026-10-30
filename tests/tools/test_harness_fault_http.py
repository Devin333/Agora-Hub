"""The fault HTTP server reproduces each failure the way a real client sees it."""

from __future__ import annotations

import http.client
import threading
import time
import urllib.error
import urllib.request

import pytest

from tests.tools.harness.fault_http import (
    Delay,
    Drop,
    FaultHTTPServer,
    Hang,
    Redirect,
    Reset,
    Respond,
    Trickle,
    Truncate,
)


def _get(url: str, *, timeout: float = 2.0, data: bytes | None = None) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout, data=data) as response:
        return response.status, response.read()


def test_route_script_advances_then_repeats_its_last_step(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/flaky", Respond(503, "busy"), Respond(200, "ok"))

    with pytest.raises(urllib.error.HTTPError) as first:
        _get(fault_http.url("/flaky"))
    assert first.value.code == 503
    assert _get(fault_http.url("/flaky")) == (200, b"ok")
    assert _get(fault_http.url("/flaky")) == (200, b"ok")
    assert len(fault_http.requests_for("/flaky")) == 3


def test_requests_are_recorded_with_method_headers_and_body(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/hook", Respond(202, ""))
    request = urllib.request.Request(
        fault_http.url("/hook?token=abc"),
        data=b'{"event": "published"}',
        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2.0) as response:
        assert response.status == 202

    [recorded] = fault_http.requests_for("/hook")
    assert recorded.method == "POST"
    assert recorded.query == "token=abc"
    assert recorded.header("authorization") == "Bearer secret"
    assert recorded.body == b'{"event": "published"}'


def test_unscripted_path_answers_404(fault_http: FaultHTTPServer) -> None:
    with pytest.raises(urllib.error.HTTPError) as error:
        _get(fault_http.url("/missing"))
    assert error.value.code == 404


def test_hang_outlasts_the_client_timeout(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/stuck", Hang())
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _get(fault_http.url("/stuck"), timeout=0.3)
    assert time.monotonic() - started < 1.5


def test_delay_then_answers(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/slow", Delay(0.3, Respond(200, "late")))
    started = time.monotonic()
    assert _get(fault_http.url("/slow")) == (200, b"late")
    assert time.monotonic() - started >= 0.25


def test_trickle_delivers_the_whole_body_slowly(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/drip", Trickle("abcdef", chunk_size=2, interval_seconds=0.1))
    started = time.monotonic()
    assert _get(fault_http.url("/drip")) == (200, b"abcdef")
    assert time.monotonic() - started >= 0.2


def test_truncated_body_is_an_incomplete_read(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/cut", Truncate("0123456789", sent_bytes=4))
    with pytest.raises(http.client.IncompleteRead) as error:
        _get(fault_http.url("/cut"))
    assert error.value.partial == b"0123"


def test_reset_surfaces_as_a_connection_error(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/rst", Reset())
    with pytest.raises((ConnectionError, urllib.error.URLError, http.client.RemoteDisconnected)):
        _get(fault_http.url("/rst"))


def test_drop_closes_without_a_status_line(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/gone", Drop())
    with pytest.raises(http.client.RemoteDisconnected):
        _get(fault_http.url("/gone"))


def test_redirects_point_wherever_the_script_says(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/start", Redirect(fault_http.url("/end")))
    fault_http.route("/end", Respond(200, "arrived"))
    assert _get(fault_http.url("/start")) == (200, b"arrived")
    assert [request.path for request in fault_http.requests] == ["/start", "/end"]


def test_hanging_handler_ends_once_the_client_gives_up(fault_http: FaultHTTPServer) -> None:
    fault_http.route("/stuck", Hang())
    before = threading.active_count()
    for _ in range(5):
        with pytest.raises(TimeoutError):
            _get(fault_http.url("/stuck"), timeout=0.2)

    deadline = time.monotonic() + 1.0
    while threading.active_count() > before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert threading.active_count() <= before


def test_stop_releases_hanging_handlers_and_joins_them() -> None:
    server = FaultHTTPServer().start()
    server.route("/stuck", Hang())
    with pytest.raises(TimeoutError):
        _get(server.url("/stuck"), timeout=0.2)
    started = time.monotonic()
    server.stop()
    assert time.monotonic() - started < 2.0
