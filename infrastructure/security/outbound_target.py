"""Which network targets the server may call on a user's behalf.

User-supplied URLs (model endpoints, remote paper sources) must not reach the
server's own network. A target passes only when it uses HTTPS and every address
its hostname resolves to is publicly routable. Hosts an administrator lists in
the allowlist are exempt, which is how an in-house model gateway is reached;
those may also use plain HTTP.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

ALLOWED_PRIVATE_HOSTS_ENV = "NEWS_LLM_ALLOWED_PRIVATE_HOSTS"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Iterable[str]]


class OutboundRejection(StrEnum):
    INVALID_URL = "invalid_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    CREDENTIALS_IN_URL = "credentials_in_url"
    UNRESOLVABLE = "unresolvable"
    NON_PUBLIC_ADDRESS = "non_public_address"


class OutboundTargetRejected(ValueError):
    """The URL may not be called. ``reason`` is safe to show; the URL is not echoed."""

    def __init__(self, reason: OutboundRejection) -> None:
        self.reason = OutboundRejection(reason)
        super().__init__(f"outbound target rejected: {self.reason.value}")


@dataclass(frozen=True)
class OutboundTarget:
    scheme: str
    host: str
    port: int
    # Addresses that passed the check; a transport connects only to these.
    addresses: tuple[str, ...]
    allowlisted: bool


@dataclass(frozen=True)
class OutboundTargetPolicy:
    allowed_hosts: frozenset[str] = frozenset()
    allowed_networks: tuple[IPNetwork, ...] = ()
    resolver: Resolver | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_hosts", frozenset(host.casefold().rstrip(".") for host in self.allowed_hosts))
        object.__setattr__(self, "allowed_networks", tuple(self.allowed_networks))

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        variable: str = ALLOWED_PRIVATE_HOSTS_ENV,
    ) -> OutboundTargetPolicy:
        """Parse a comma list of hostnames, IP addresses and CIDR networks."""
        environ = os.environ if environ is None else environ
        hosts: set[str] = set()
        networks: list[IPNetwork] = []
        for entry in (environ.get(variable) or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
            except ValueError:
                if any(char in entry for char in "/:@ ") or not entry.replace("-", "").replace(".", "").isalnum():
                    raise ValueError(f"{variable} entry is not a hostname, IP address or CIDR network: {entry!r}")
                hosts.add(entry)
        return cls(allowed_hosts=frozenset(hosts), allowed_networks=tuple(networks))

    def check(self, url: str, *, allow_unresolved: bool = False) -> OutboundTarget:
        parsed = urlsplit(str(url or "").strip())
        scheme = parsed.scheme.casefold()
        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise OutboundTargetRejected(OutboundRejection.INVALID_URL) from exc
        if scheme not in {"https", "http"} or not host:
            raise OutboundTargetRejected(
                OutboundRejection.SCHEME_NOT_ALLOWED if host else OutboundRejection.INVALID_URL
            )
        if parsed.username is not None or parsed.password is not None:
            raise OutboundTargetRejected(OutboundRejection.CREDENTIALS_IN_URL)
        host = host.casefold().rstrip(".")
        port = port or (443 if scheme == "https" else 80)

        if scheme != "https" and host not in self.allowed_hosts and not self.allowed_networks:
            # Plain HTTP can only ever pass through the allowlist; no lookup needed to refuse it.
            raise OutboundTargetRejected(OutboundRejection.SCHEME_NOT_ALLOWED)

        literal = _ip_literal(host)
        addresses = (str(literal),) if literal is not None else self._resolve(host, port)
        if not addresses:
            if allow_unresolved:
                return OutboundTarget(scheme, host, port, (), allowlisted=False)
            raise OutboundTargetRejected(OutboundRejection.UNRESOLVABLE)

        allowlisted = host in self.allowed_hosts or (
            bool(self.allowed_networks)
            and all(any(ipaddress.ip_address(item) in network for network in self.allowed_networks)
                    for item in addresses)
        )
        if not allowlisted:
            if scheme != "https":
                raise OutboundTargetRejected(OutboundRejection.SCHEME_NOT_ALLOWED)
            if not all(is_public_address(ipaddress.ip_address(item)) for item in addresses):
                raise OutboundTargetRejected(OutboundRejection.NON_PUBLIC_ADDRESS)
        return OutboundTarget(scheme, host, port, addresses, allowlisted=allowlisted)

    def _resolve(self, host: str, port: int) -> tuple[str, ...]:
        resolver = self.resolver or _system_resolver
        try:
            resolved = resolver(host, port)
        except OSError:
            return ()
        addresses: list[str] = []
        for value in resolved:
            address = _ip_literal(value)
            if address is None:
                continue
            text = str(address)
            if text not in addresses:
                addresses.append(text)
        return tuple(addresses)


def is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped or address.sixtofour
        if mapped is not None:
            return is_public_address(mapped)
        if address.teredo is not None:
            return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
        or not address.is_global
    )


def _ip_literal(value: str) -> IPAddress | None:
    try:
        # Zone ids (fe80::1%eth0) are link-local by definition; strip and judge the address.
        return ipaddress.ip_address(value.split("%", 1)[0].strip("[]"))
    except ValueError:
        return None


def _system_resolver(host: str, port: int) -> list[str]:
    return [str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


__all__ = [
    "ALLOWED_PRIVATE_HOSTS_ENV",
    "OutboundRejection",
    "OutboundTarget",
    "OutboundTargetPolicy",
    "OutboundTargetRejected",
    "is_public_address",
]
