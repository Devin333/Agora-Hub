from __future__ import annotations

import ipaddress

import pytest

from framework.shared.errors import BoundaryViolationError
from infrastructure.security.outbound_address import (
    OutboundAddressPolicy,
    OutboundAddressRejected,
    OutboundRejection,
    is_public_address,
)

PUBLIC = "93.184.215.14"


def _resolver(mapping: dict[str, list[str]]):
    def resolve(host: str, _port: int):
        if host not in mapping:
            raise OSError("name not known")
        return mapping[host]

    return resolve


def _policy(mapping: dict[str, list[str]], **kwargs) -> OutboundAddressPolicy:
    return OutboundAddressPolicy(resolver=_resolver(mapping), **kwargs)


def _reason(policy: OutboundAddressPolicy, url: str) -> OutboundRejection:
    with pytest.raises(OutboundAddressRejected) as caught:
        policy.check_url(url)
    return caught.value.reason


def test_public_https_passes_with_the_checked_addresses():
    target = _policy({"api.example.com": [PUBLIC, PUBLIC]}).check_url("https://api.example.com/v1")

    assert (target.scheme, target.host, target.port) == ("https", "api.example.com", 443)
    assert target.addresses == (PUBLIC,)
    assert target.allowlisted is False


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "0.0.0.0",
     "100.64.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "2002:7f00:0001::1", "224.0.0.1"],
)
def test_non_public_resolution_is_refused(address):
    policy = _policy({"evil.example.com": [address]})

    assert _reason(policy, "https://evil.example.com") is OutboundRejection.NON_PUBLIC_ADDRESS


def test_one_private_answer_among_public_ones_is_refused():
    policy = _policy({"mixed.example.com": [PUBLIC, "10.1.2.3"]})

    assert _reason(policy, "https://mixed.example.com") is OutboundRejection.NON_PUBLIC_ADDRESS


@pytest.mark.parametrize("url", ["https://127.0.0.1/v1", "https://[::1]:8443/v1", "https://[fe80::1%25eth0]/"])
def test_literal_private_addresses_are_refused_without_lookup(url):
    policy = OutboundAddressPolicy(resolver=_resolver({}))

    assert _reason(policy, url) in {OutboundRejection.NON_PUBLIC_ADDRESS, OutboundRejection.INVALID_URL}


def test_unresolvable_host_is_refused_not_waved_through():
    assert _reason(_policy({}), "https://nowhere.example.com") is OutboundRejection.UNRESOLVABLE


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.example.com", OutboundRejection.SCHEME_NOT_ALLOWED),
        ("ftp://api.example.com", OutboundRejection.SCHEME_NOT_ALLOWED),
        ("https://user:pw@api.example.com", OutboundRejection.CREDENTIALS_IN_URL),
        ("https:///no-host", OutboundRejection.INVALID_URL),
        ("https://api.example.com:99999", OutboundRejection.INVALID_URL),
        ("not a url", OutboundRejection.INVALID_URL),
    ],
)
def test_malformed_or_unsafe_urls_are_refused(url, reason):
    assert _reason(_policy({"api.example.com": [PUBLIC]}), url) is reason


def test_allowlisted_hostname_may_be_private_and_plain_http():
    policy = _policy(
        {"host.docker.internal": ["192.168.65.254"]},
        allowed_hosts=frozenset({"Host.Docker.Internal."}),
    )

    target = policy.check_url("http://host.docker.internal:11434/v1")

    assert target.allowlisted is True
    assert target.addresses == ("192.168.65.254",)
    assert target.port == 11434


def test_allowlisted_network_admits_only_addresses_inside_it():
    policy = _policy(
        {"gateway.corp": ["10.20.0.7"], "other.corp": ["10.30.0.7"]},
        allowed_networks=(ipaddress.ip_network("10.20.0.0/16"),),
    )

    assert policy.check_url("http://gateway.corp").allowlisted is True
    assert _reason(policy, "http://other.corp") is OutboundRejection.SCHEME_NOT_ALLOWED
    assert _reason(policy, "https://other.corp") is OutboundRejection.NON_PUBLIC_ADDRESS


def test_plain_http_to_a_public_host_is_refused_even_with_a_network_allowlist():
    policy = _policy(
        {"api.example.com": [PUBLIC]},
        allowed_networks=(ipaddress.ip_network("10.0.0.0/8"),),
    )

    assert _reason(policy, "http://api.example.com") is OutboundRejection.SCHEME_NOT_ALLOWED


def test_environment_allowlist_parses_hosts_addresses_and_networks():
    policy = OutboundAddressPolicy.from_environment(
        {"NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS": " host.docker.internal , 10.0.0.0/8,192.168.1.10,fd00::/8 ,"}
    )

    assert policy.allowed_hosts == frozenset({"host.docker.internal"})
    assert ipaddress.ip_network("10.0.0.0/8") in policy.allowed_networks
    assert ipaddress.ip_network("192.168.1.10/32") in policy.allowed_networks
    assert ipaddress.ip_network("fd00::/8") in policy.allowed_networks


@pytest.mark.parametrize("entry", ["http://host", "host:8080", "user@host", "bad host", "-leading.example", "例子.测试"])
def test_environment_allowlist_rejects_malformed_entries(entry):
    with pytest.raises(ValueError, match="NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS"):
        OutboundAddressPolicy.from_environment({"NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS": entry})


def test_empty_environment_allows_nothing_private():
    policy = OutboundAddressPolicy.from_environment({})

    assert policy.allowed_hosts == frozenset()
    assert policy.allowed_networks == ()


def test_rejection_never_echoes_the_url_and_is_a_boundary_violation():
    with pytest.raises(OutboundAddressRejected) as caught:
        _policy({"secret-host.example.com": ["10.0.0.1"]}).check_url("https://secret-host.example.com/path?token=x")

    assert "secret-host" not in str(caught.value)
    assert "token" not in str(caught.value)
    assert isinstance(caught.value, BoundaryViolationError)
    assert isinstance(caught.value, ValueError)
    assert caught.value.code == "non_public_address"


@pytest.mark.parametrize(
    ("address", "public"),
    [("8.8.8.8", True), ("2606:4700:4700::1111", True), ("::ffff:8.8.8.8", True),
     ("::ffff:10.0.0.1", False), ("2001::1", False), ("198.18.0.1", False), ("240.0.0.1", False)],
)
def test_is_public_address_judges_embedded_ipv4_and_special_ranges(address, public):
    assert is_public_address(ipaddress.ip_address(address)) is public
