from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from infrastructure.security.outbound_target import (
    ALLOWED_PRIVATE_HOSTS_ENV,
    OutboundRejection,
    OutboundTargetPolicy,
    OutboundTargetRejected,
    is_public_address,
)
from infrastructure.security.pinned_transport import PinnedHTTPTransport


def _resolver(mapping):
    def resolve(host, _port):
        if host not in mapping:
            raise socket.gaierror("no such host")
        return mapping[host]

    return resolve


PUBLIC = "93.184.215.14"


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.example.com/v1", OutboundRejection.SCHEME_NOT_ALLOWED),
        ("ftp://api.example.com", OutboundRejection.SCHEME_NOT_ALLOWED),
        ("https:///v1", OutboundRejection.INVALID_URL),
        ("https://user:pw@api.example.com", OutboundRejection.CREDENTIALS_IN_URL),
        ("https://api.example.com:99999", OutboundRejection.INVALID_URL),
        ("https://127.0.0.1/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://10.0.0.8/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://169.254.169.254/latest", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://[::1]/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://[::ffff:10.0.0.1]/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://[fe80::1%25eth0]/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://[fd00::1]/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://100.64.0.1/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://internal.example.com/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://mixed.example.com/v1", OutboundRejection.NON_PUBLIC_ADDRESS),
        ("https://missing.example.com/v1", OutboundRejection.UNRESOLVABLE),
    ],
)
def test_non_public_or_malformed_targets_are_rejected(url, reason) -> None:
    policy = OutboundTargetPolicy(resolver=_resolver({
        "internal.example.com": ["192.168.1.5"],
        # One private answer is enough to refuse: the connection could pick it.
        "mixed.example.com": [PUBLIC, "10.1.1.1"],
    }))
    with pytest.raises(OutboundTargetRejected) as caught:
        policy.check(url)
    assert caught.value.reason is reason
    assert "example" not in str(caught.value)


def test_public_https_target_passes_with_its_checked_addresses() -> None:
    policy = OutboundTargetPolicy(resolver=_resolver({"api.example.com": [PUBLIC, PUBLIC]}))
    target = policy.check("https://API.example.com./v1")
    assert (target.host, target.port, target.addresses, target.allowlisted) == (
        "api.example.com", 443, (PUBLIC,), False,
    )


def test_allowlist_admits_named_hosts_and_networks_including_http() -> None:
    policy = OutboundTargetPolicy.from_environment(
        {ALLOWED_PRIVATE_HOSTS_ENV: "llm-gateway.corp, 10.20.0.0/16 ,127.0.0.1"}
    )
    policy = OutboundTargetPolicy(
        allowed_hosts=policy.allowed_hosts,
        allowed_networks=policy.allowed_networks,
        resolver=_resolver({"llm-gateway.corp": ["192.168.7.7"], "other.corp": ["192.168.7.8"]}),
    )
    assert policy.check("http://llm-gateway.corp:8080/v1").allowlisted
    assert policy.check("https://10.20.3.4/v1").allowlisted
    assert policy.check("http://127.0.0.1:11434/v1").allowlisted
    with pytest.raises(OutboundTargetRejected):
        policy.check("https://other.corp/v1")
    with pytest.raises(OutboundTargetRejected):
        policy.check("https://10.21.0.1/v1")


def test_allowlist_rejects_entries_that_are_not_hosts() -> None:
    with pytest.raises(ValueError):
        OutboundTargetPolicy.from_environment({ALLOWED_PRIVATE_HOSTS_ENV: "https://gateway/v1"})


def test_unresolved_hosts_can_be_deferred_to_the_fetcher() -> None:
    policy = OutboundTargetPolicy(resolver=_resolver({}))
    assert policy.check("https://later.example.com", allow_unresolved=True).addresses == ()


@pytest.mark.parametrize(
    ("address", "public"),
    [("8.8.8.8", True), ("2606:4700::1111", True), ("2002:0a00:0001::1", False),
     ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False), ("0.0.0.0", False), ("224.0.0.1", False)],
)
def test_public_address_classification(address, public) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public


# --- pinned transport --------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/")
            self.end_headers()
            return
        body = f"host={self.headers['Host']}\nline2\n".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def test_transport_connects_to_the_checked_address_not_a_fresh_lookup(local_server) -> None:
    answers = iter([["127.0.0.1"]])

    def rebinding_resolver(host, _port):
        # A second lookup would return a different address; the transport must not make one.
        return next(answers)

    policy = OutboundTargetPolicy(allowed_networks=(ipaddress.ip_network("127.0.0.0/8"),),
                                  resolver=rebinding_resolver)
    transport = PinnedHTTPTransport(policy, use_environment_proxies=False)
    body = transport(Request(f"http://gateway.test:{local_server}/models"), 5)
    assert body.startswith(f"host=gateway.test:{local_server}".encode())


def test_transport_refuses_rejected_targets_before_connecting() -> None:
    transport = PinnedHTTPTransport(OutboundTargetPolicy(), use_environment_proxies=False)
    with pytest.raises(OutboundTargetRejected):
        transport(Request("https://127.0.0.1:1/v1/messages"), 5)


def test_transport_reports_redirects_instead_of_following(local_server) -> None:
    policy = OutboundTargetPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
    transport = PinnedHTTPTransport(policy, use_environment_proxies=False)
    with pytest.raises(HTTPError) as caught:
        transport(Request(f"http://127.0.0.1:{local_server}/redirect"), 5)
    assert caught.value.code == 302


def test_stream_yields_lines_and_enforces_the_size_bound(local_server) -> None:
    policy = OutboundTargetPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
    lines = list(PinnedHTTPTransport(policy, use_environment_proxies=False).stream(
        Request(f"http://127.0.0.1:{local_server}/s"), 5
    ))
    assert lines[1] == b"line2\n"
    with pytest.raises(Exception, match="size limit"):
        PinnedHTTPTransport(policy, max_response_bytes=4, use_environment_proxies=False)(
            Request(f"http://127.0.0.1:{local_server}/s"), 5
        )


def test_https_verifies_the_original_hostname(local_server) -> None:
    # The plain-HTTP test server cannot complete a TLS handshake; the failure must
    # come from TLS on the pinned socket, proving the SNI path is taken.
    policy = OutboundTargetPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
    transport = PinnedHTTPTransport(policy, ssl_context=ssl.create_default_context(),
                                    use_environment_proxies=False)
    with pytest.raises(Exception):
        transport(Request(f"https://127.0.0.1:{local_server}/"), 5)
