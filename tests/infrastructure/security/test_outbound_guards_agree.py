"""The two outbound guards decide the same way.

Source fetches go through ``infrastructure.external.egress``; model and
research endpoints through ``infrastructure.security``. Both judge an
address with one predicate and refuse with a ``BoundaryViolationError``, so
neither can drift into admitting what the other refuses, and every retry
policy treats a refusal from either as final.
"""

from __future__ import annotations

import ipaddress
from urllib.error import URLError

import pytest

from framework.shared.errors import BoundaryViolationError
from infrastructure.external import egress
from infrastructure.external.egress import EgressDenied, EgressPolicy
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy, decide_source_fetch_retry
from infrastructure.security import outbound_address
from infrastructure.security.outbound_address import (
    OutboundAddressPolicy,
    OutboundAddressRejected,
    OutboundRejection,
    address_views,
)

NON_PUBLIC = [
    "127.0.0.1", "10.1.2.3", "100.64.0.1", "169.254.169.254", "198.18.0.1", "240.0.0.1", "224.0.0.1",
    "::1", "fd00::1", "fe80::1", "ff02::1", "2001::1",
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "2002:7f00:1::",  # 6to4 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of the cloud metadata address
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
]
PUBLIC = ["8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "::ffff:8.8.8.8", "2002:808:808::", "64:ff9b::808:808"]


def test_both_guards_use_the_same_public_address_rule() -> None:
    assert egress.is_public_address is outbound_address.is_public_address


@pytest.mark.parametrize("address", NON_PUBLIC)
def test_a_non_public_address_is_refused_by_both_guards(address: str) -> None:
    with pytest.raises(EgressDenied):
        EgressPolicy().check_address(ipaddress.ip_address(address), host="target")
    with pytest.raises(OutboundAddressRejected) as rejected:
        OutboundAddressPolicy().check_host(address, 443)
    assert rejected.value.reason is OutboundRejection.NON_PUBLIC_ADDRESS


@pytest.mark.parametrize("address", PUBLIC)
def test_a_public_address_passes_both_guards(address: str) -> None:
    EgressPolicy().check_address(ipaddress.ip_address(address), host="target")
    assert OutboundAddressPolicy().check_host(address, 443).addresses == (str(ipaddress.ip_address(address)),)


def test_an_allowlisted_network_also_admits_its_embedded_forms() -> None:
    views = {str(view) for view in address_views(ipaddress.ip_address("64:ff9b::a0a:a0a"))}
    assert views == {"64:ff9b::a0a:a0a", "10.10.10.10"}
    EgressPolicy(allowed_private_networks=("10.0.0.0/8",)).check_address(ipaddress.ip_address("::ffff:10.1.1.1"), host="intranet")


@pytest.mark.parametrize(
    "refusal",
    [
        EgressDenied("host is not in allowed_domains", host="elsewhere.example"),
        OutboundAddressRejected(OutboundRejection.NON_PUBLIC_ADDRESS),
    ],
    ids=["egress", "pinned-transport"],
)
def test_a_refusal_from_either_guard_is_never_retried(refusal: BoundaryViolationError) -> None:
    policy = SourceFetchPolicy(retry_times=3)
    wrapped = URLError(refusal)
    wrapped.__cause__ = refusal

    for error in (refusal, wrapped):
        decision = decide_source_fetch_retry(error, policy)
        assert decision.retryable is False and decision.should_retry is False
    assert isinstance(refusal, BoundaryViolationError) and isinstance(refusal, ValueError)
