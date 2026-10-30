from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from infrastructure.security.outbound_address import OutboundAddressPolicy, OutboundAddressRejected
from infrastructure.security.pinned_https import PinnedConnector
from infrastructure.security.pinned_httpx import PinnedHTTPXTransport, pinned_httpx_client

ORIGIN_HOST = "models.example.com"
PUBLIC_ANSWER = "93.184.215.14"


def _client(origin, answers: dict[str, list[str]] | None = None) -> httpx2.Client:
    answers = answers if answers is not None else {ORIGIN_HOST: [PUBLIC_ANSWER]}
    policy = OutboundAddressPolicy(resolver=lambda host, _port: answers[host])
    connector = PinnedConnector(policy, ssl_context=origin.client_context, use_environment_proxies=False)
    return pinned_httpx_client(policy, connector=connector)


def test_get_and_post_go_to_the_checked_address(tls_origin, dialed):
    with _client(tls_origin) as client:
        listing = client.get(tls_origin.url("/v1/models"), timeout=5)
        echoed = client.post(tls_origin.url("/v1/messages"), content=b'{"m":1}', timeout=5)

    assert listing.status_code == 200
    assert listing.json() == {"ok": True, "path": "/v1/models"}
    assert echoed.json() == {"echo": '{"m":1}'}
    assert dialed == [(PUBLIC_ANSWER, tls_origin.port)] * 2
    assert tls_origin.sni_seen == [ORIGIN_HOST] * 2


def test_chunked_stream_is_decoded_once(tls_origin, dialed):
    with _client(tls_origin) as client, client.stream("GET", tls_origin.url("/stream"), timeout=5) as response:
        body = b"".join(response.iter_bytes())

    assert body == b"data: one\n\ndata: two\n\n"


def test_redirect_is_returned_not_followed(tls_origin, dialed):
    with _client(tls_origin) as client:
        response = client.get(tls_origin.url("/redirect"), timeout=5)

    assert response.status_code == 302
    assert len(tls_origin.requests) == 1


def test_refused_target_surfaces_as_connect_error_caused_by_the_rejection(tls_origin, dialed):
    with _client(tls_origin, {ORIGIN_HOST: ["10.0.0.1"]}) as client, pytest.raises(OutboundAddressRejected):
        client.get(tls_origin.url(), timeout=5)

    assert dialed == []


def test_anthropic_sdk_sends_through_the_pinned_transport(tls_origin, dialed):
    sdk = anthropic.Anthropic(
        api_key="sk-ant-test",
        base_url=tls_origin.url(""),
        max_retries=0,
        http_client=_client(tls_origin),
    )

    # The test origin's body is not an Anthropic page and parses as an empty
    # one; what matters is that the request reached it through the pinned socket.
    assert list(sdk.models.list()) == []

    assert dialed and all(address == (PUBLIC_ANSWER, tls_origin.port) for address in dialed)
    assert tls_origin.requests[0]["path"].startswith("/v1/models")
    assert tls_origin.requests[0]["headers"]["authorization"] == "Bearer sk-ant-test"


def test_anthropic_sdk_reports_a_refused_target_as_connection_error_with_the_cause(tls_origin, dialed):
    sdk = anthropic.Anthropic(
        api_key="sk-ant-test",
        base_url=tls_origin.url(""),
        max_retries=0,
        http_client=_client(tls_origin, {ORIGIN_HOST: ["169.254.169.254"]}),
    )

    with pytest.raises(anthropic.APIConnectionError) as caught:
        sdk.models.list()

    assert isinstance(caught.value.__cause__, OutboundAddressRejected)
    assert dialed == []


def test_transport_is_an_httpx2_transport():
    assert issubclass(PinnedHTTPXTransport, httpx2.BaseTransport)
    client = pinned_httpx_client(OutboundAddressPolicy())
    assert client.follow_redirects is False
    client.close()


def test_json_body_round_trip(tls_origin, dialed):
    with _client(tls_origin) as client:
        response = client.post(tls_origin.url("/v1/messages"), json={"text": "你好"}, timeout=5)

    assert json.loads(response.json()["echo"]) == {"text": "你好"}
