"""Address, host and URL decisions of the egress guard (no network)."""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import replace

import pytest

from infrastructure.external.egress import EgressDenied, EgressPolicy, is_public_address, normalize_host
from infrastructure.external.fetch_deadline import (
    FetchDeadlineExceeded,
    bounded_timeout,
    fetch_deadline,
    remaining_seconds,
)
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy, decide_source_fetch_retry


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "172.16.4.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # CGNAT
        "0.0.0.0",
        "224.0.0.1",  # multicast is "global" to ipaddress
        "255.255.255.255",
        "::1",
        "fd00::1",
        "fe80::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "2002:7f00:1::",  # 6to4 of 127.0.0.1
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is False
    with pytest.raises(EgressDenied, match="non-public"):
        EgressPolicy().check_address(ipaddress.ip_address(address), host="target")


@pytest.mark.parametrize("address", ["8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8"])
def test_public_addresses_pass(address: str) -> None:
    EgressPolicy().check_address(ipaddress.ip_address(address), host="target")


def test_allowed_private_networks_open_exactly_their_range() -> None:
    policy = EgressPolicy(allowed_private_networks=("10.20.0.0/16",))

    policy.check_address(ipaddress.ip_address("10.20.3.4"), host="intranet")
    policy.check_address(ipaddress.ip_address("::ffff:10.20.3.4"), host="intranet")
    with pytest.raises(EgressDenied):
        policy.check_address(ipaddress.ip_address("10.21.0.1"), host="intranet")
    with pytest.raises(EgressDenied):
        policy.check_address(ipaddress.ip_address("169.254.169.254"), host="metadata")


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("openai.com", True),
        ("blog.openai.com", True),
        ("OPENAI.COM.", True),
        ("evil-openai.com", False),
        ("openai.com.evil.example", False),
        ("xn--penai-mxa.com", False),
    ],
)
def test_allowed_domains_match_host_or_subdomain_only(host: str, allowed: bool) -> None:
    policy = EgressPolicy(allowed_domains=("openai.com",))
    if allowed:
        assert policy.check_host(host).endswith("openai.com")
    else:
        with pytest.raises(EgressDenied, match="allowed_domains"):
            policy.check_host(host)


def test_unicode_hosts_compare_as_punycode() -> None:
    assert normalize_host("bücher.example") == "xn--bcher-kva.example"
    with pytest.raises(EgressDenied):
        EgressPolicy(allowed_domains=("example.com",)).check_host("еxample.com")  # Cyrillic е


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "only http and https"),
        ("ftp://example.com/feed", "only http and https"),
        ("gopher://example.com/", "only http and https"),
        ("https://user:pass@example.com/feed", "embedded credentials"),
        ("https://openai.com@evil.example/feed", "embedded credentials"),
        ("http://example.com:99999/", "malformed"),
        ("http:///nohost", "no host"),
        ("http://169.254.169.254/latest/meta-data/", "non-public"),
        ("http://[::1]:8080/", "non-public"),
    ],
)
def test_urls_are_refused_before_any_connection(url: str, reason: str) -> None:
    with pytest.raises(EgressDenied, match=reason):
        EgressPolicy().check_url(url)


def test_https_to_http_redirect_is_a_downgrade() -> None:
    with pytest.raises(EgressDenied, match="downgrade"):
        EgressPolicy().check_url("http://example.com/next", previous_url="https://example.com/start")


def _resolver(*addresses: str):
    def resolve(host: str, port: int):
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return resolve


def test_one_non_public_answer_refuses_the_whole_resolution() -> None:
    with pytest.raises(EgressDenied, match="169.254.169.254"):
        EgressPolicy().resolve("mixed.example", 80, _resolver("8.8.8.8", "169.254.169.254"))


def test_resolution_keeps_every_vetted_candidate_once() -> None:
    candidates = EgressPolicy().resolve("dual.example", 443, _resolver("8.8.8.8", "8.8.8.8", "2606:4700::1111"))
    assert [candidate[3][0] for candidate in candidates] == ["8.8.8.8", "2606:4700::1111"]


def test_empty_resolution_is_a_connection_error() -> None:
    with pytest.raises(OSError, match="no addresses"):
        EgressPolicy().resolve("void.example", 80, lambda host, port: [])


def test_policy_rejects_malformed_entries_at_configuration_time() -> None:
    with pytest.raises(ValueError, match="allowed_private_networks"):
        EgressPolicy(allowed_private_networks=("10.0.0.1/8",))
    with pytest.raises(ValueError, match="allowed_domains"):
        EgressPolicy(allowed_domains=("",))


def test_fetch_policy_carries_its_egress_rules_through_replace() -> None:
    policy = SourceFetchPolicy(allowed_domains=("Example.COM",), allowed_private_networks=("10.0.0.0/8",))
    replaced = replace(policy, timeout_seconds=3.0)

    assert replaced.allowed_domains == ("example.com",)
    assert replaced.egress_policy.allowed_private_networks == ("10.0.0.0/8",)
    assert replaced.egress_policy.max_redirects == policy.max_redirects


def test_nested_deadlines_keep_the_earlier_expiry() -> None:
    with fetch_deadline(5.0):
        with fetch_deadline(0.5):
            assert remaining_seconds() <= 0.5
        with fetch_deadline(60.0):
            assert remaining_seconds() <= 5.0
        assert bounded_timeout(30.0) <= 5.0
    assert remaining_seconds() is None


def test_spent_deadline_refuses_new_operations_and_is_never_retried() -> None:
    # time.monotonic ticks every ~15ms on Windows (Python 3.12); sleep well past it.
    with fetch_deadline(0.01):
        time.sleep(0.06)
        with pytest.raises(FetchDeadlineExceeded):
            bounded_timeout(10.0)
    decision = decide_source_fetch_retry(FetchDeadlineExceeded(1.0), SourceFetchPolicy(retry_times=5))
    assert decision.retryable is False
    assert decide_source_fetch_retry(EgressDenied("x"), SourceFetchPolicy(retry_times=5)).retryable is False
