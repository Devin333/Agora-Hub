from __future__ import annotations

import json
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from infrastructure.security.outbound_address import OutboundAddressPolicy, OutboundAddressRejected
from infrastructure.security.pinned_https import (
    PinnedConnector,
    PinnedRedirectingOpener,
    PinnedUrllibTransport,
    RedirectLimitExceeded,
)

ORIGIN_HOST = "models.example.com"
PUBLIC_ANSWER = "93.184.215.14"


def _policy(answers: dict[str, list[str]] | None = None) -> OutboundAddressPolicy:
    answers = answers if answers is not None else {ORIGIN_HOST: [PUBLIC_ANSWER]}

    def resolve(host: str, _port: int):
        if host not in answers:
            raise OSError("name not known")
        return answers[host]

    return OutboundAddressPolicy(resolver=resolve)


def _transport(origin, policy: OutboundAddressPolicy | None = None, **kwargs) -> PinnedUrllibTransport:
    connector = PinnedConnector(
        policy or _policy(), ssl_context=origin.client_context, use_environment_proxies=False
    )
    return PinnedUrllibTransport(connector, **kwargs)


def test_request_connects_to_the_checked_address_with_hostname_sni(tls_origin, dialed):
    body = _transport(tls_origin)(Request(tls_origin.url("/v1/models")), 5)

    assert json.loads(body) == {"ok": True, "path": "/v1/models"}
    assert dialed == [(PUBLIC_ANSWER, tls_origin.port)]
    assert tls_origin.sni_seen == [ORIGIN_HOST]
    assert tls_origin.requests[0]["headers"]["host"] == f"{ORIGIN_HOST}:{tls_origin.port}"


def test_post_sends_body_and_headers(tls_origin, dialed):
    request = Request(
        tls_origin.url("/v1/chat/completions"),
        data=b'{"q":1}',
        headers={"Authorization": "Bearer sk-test", "Content-Type": "application/json"},
        method="POST",
    )

    body = _transport(tls_origin)(request, 5)

    assert json.loads(body) == {"echo": '{"q":1}'}
    seen = tls_origin.requests[0]
    assert seen["method"] == "POST"
    assert seen["body"] == b'{"q":1}'
    assert seen["headers"]["authorization"] == "Bearer sk-test"
    assert seen["headers"]["content-length"] == "7"


def test_refused_target_raises_before_any_dial(tls_origin, dialed):
    policy = _policy({ORIGIN_HOST: ["10.0.0.8"]})

    with pytest.raises(OutboundAddressRejected):
        _transport(tls_origin, policy)(Request(tls_origin.url()), 5)

    assert dialed == []
    assert tls_origin.requests == []


def test_dns_rebinding_cannot_redirect_the_connection(tls_origin, dialed):
    # The first answer passes the check; a rebinding resolver would return a
    # private address on the second lookup. The transport never looks again.
    answers = iter([[PUBLIC_ANSWER], ["127.0.0.1"]])
    policy = OutboundAddressPolicy(resolver=lambda _host, _port: next(answers))

    _transport(tls_origin, policy)(Request(tls_origin.url()), 5)

    assert dialed == [(PUBLIC_ANSWER, tls_origin.port)]


def test_certificate_is_verified_for_the_hostname(tls_origin, dialed):
    connector = PinnedConnector(_policy(), ssl_context=ssl.create_default_context(), use_environment_proxies=False)

    with pytest.raises(URLError) as caught:
        PinnedUrllibTransport(connector)(Request(tls_origin.url()), 5)

    assert isinstance(caught.value.reason, ssl.SSLCertVerificationError)


def test_redirect_is_reported_not_followed(tls_origin, dialed):
    with pytest.raises(HTTPError) as caught:
        _transport(tls_origin)(Request(tls_origin.url("/redirect")), 5)

    assert caught.value.code == 302
    assert len(tls_origin.requests) == 1


def test_http_error_carries_status_and_bounded_body(tls_origin, dialed):
    with pytest.raises(HTTPError) as caught:
        _transport(tls_origin)(Request(tls_origin.url("/missing")), 5)

    assert caught.value.code == 404
    assert caught.value.read() == b'{"error":"nope"}'


def test_oversized_body_is_refused(tls_origin, dialed):
    with pytest.raises(URLError, match="size limit"):
        _transport(tls_origin, max_response_bytes=1024)(Request(tls_origin.url("/big")), 5)


def test_stream_yields_lines_as_they_arrive(tls_origin, dialed):
    lines = list(_transport(tls_origin).stream(Request(tls_origin.url("/stream")), 5))

    assert b"".join(lines) == b"data: one\n\ndata: two\n\n"
    assert lines[0] == b"data: one\n"


def test_redirecting_opener_rechecks_every_hop(tls_origin, dialed):
    opener = PinnedRedirectingOpener(
        PinnedConnector(
            _policy({ORIGIN_HOST: [PUBLIC_ANSWER], "internal.example.com": ["10.0.0.9"]}),
            ssl_context=tls_origin.client_context,
            use_environment_proxies=False,
        )
    )

    with pytest.raises(OutboundAddressRejected):
        opener.open(Request(tls_origin.url("/redirect")), timeout_seconds=5, max_redirects=3)

    assert dialed == [(PUBLIC_ANSWER, tls_origin.port)]


def test_redirecting_opener_enforces_the_redirect_limit(tls_origin, dialed):
    opener = PinnedRedirectingOpener(
        PinnedConnector(_policy(), ssl_context=tls_origin.client_context, use_environment_proxies=False)
    )

    with pytest.raises(RedirectLimitExceeded):
        opener.open(Request(tls_origin.url("/redirect")), timeout_seconds=5, max_redirects=0)


def test_redirecting_opener_returns_a_urlopen_like_response(tls_origin, dialed):
    opener = PinnedRedirectingOpener(
        PinnedConnector(_policy(), ssl_context=tls_origin.client_context, use_environment_proxies=False)
    )

    with opener.open(Request(tls_origin.url("/paper")), timeout_seconds=5, max_redirects=3) as response:
        assert response.url == tls_origin.url("/paper")
        assert response.headers.get("Content-Type") == "application/json"
        assert json.loads(response.read(1000)) == {"ok": True, "path": "/paper"}


def test_allowlisted_host_bypasses_environment_proxy(tls_origin, dialed, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    policy = OutboundAddressPolicy(
        allowed_hosts=frozenset({ORIGIN_HOST}), resolver=lambda _h, _p: [PUBLIC_ANSWER]
    )
    connector = PinnedConnector(policy, ssl_context=tls_origin.client_context)

    PinnedUrllibTransport(connector)(Request(tls_origin.url()), 5)

    assert dialed == [(PUBLIC_ANSWER, tls_origin.port)]


def test_public_host_goes_through_environment_proxy_with_connect(monkeypatch, dialed):
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:p%40ss@{PUBLIC_ANSWER}:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    connector = PinnedConnector(_policy())

    route = connector._proxy_for(_policy().check_url(f"https://{ORIGIN_HOST}/"))

    assert route is not None
    assert (route.host, route.port) == (PUBLIC_ANSWER, 3128)
    assert route.authorization == "Basic dXNlcjpwQHNz"
