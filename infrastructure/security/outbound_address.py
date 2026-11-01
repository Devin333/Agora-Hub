"""Which network addresses the server may call on a user's behalf.

User-supplied URLs (model connection base URLs, remote paper sources) must not
reach the server's own network. A target passes only when it uses HTTPS and
every address its hostname resolves to is publicly routable; a name that does
not resolve is refused rather than waved through. Hosts or networks an
administrator allowlists are exempt, which is how an in-house model gateway or
a local Ollama behind ``host.docker.internal`` is reached; those may also use
plain HTTP.

The check returns the addresses that passed so a transport can connect to
exactly those, closing the gap a second DNS lookup would leave open.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from framework.shared.errors import BoundaryViolationError

LLM_ALLOWED_PRIVATE_HOSTS_ENV = "NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Iterable[str]]

_DEFAULT_PORTS = {"https": 443, "http": 80}


class OutboundRejection(StrEnum):
    INVALID_URL = "invalid_url"
    SCHEME_NOT_ALLOWED = "scheme_not_allowed"
    CREDENTIALS_IN_URL = "credentials_in_url"
    UNRESOLVABLE = "unresolvable"
    NON_PUBLIC_ADDRESS = "non_public_address"


class OutboundAddressRejected(BoundaryViolationError, ValueError):
    """The target may not be called. ``reason`` is safe to show; the URL is never echoed.

    A ``BoundaryViolationError`` so framework clients can tell a refused target
    (a configuration problem, never retried) from a network failure without
    importing this layer; a ``ValueError`` so fetch retry policies treat it as
    final too.
    """

    def __init__(self, reason: OutboundRejection) -> None:
        self.reason = OutboundRejection(reason)
        super().__init__(f"outbound target rejected: {self.reason.value}", code=self.reason.value)


@dataclass(frozen=True, slots=True)
class OutboundTarget:
    scheme: str
    host: str
    port: int
    # Addresses that passed the check; a transport connects only to these.
    addresses: tuple[str, ...]
    allowlisted: bool


@dataclass(frozen=True)
class OutboundAddressPolicy:
    """Public HTTPS by default; ``allowed_hosts``/``allowed_networks`` admit private targets."""

    allowed_hosts: frozenset[str] = frozenset()
    allowed_networks: tuple[IPNetwork, ...] = ()
    resolver: Resolver | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_hosts",
            frozenset(_normalized_host(host) for host in self.allowed_hosts),
        )
        object.__setattr__(self, "allowed_networks", tuple(self.allowed_networks))

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        variable: str = LLM_ALLOWED_PRIVATE_HOSTS_ENV,
    ) -> OutboundAddressPolicy:
        """Parse a comma list of hostnames, IP addresses and CIDR networks.

        A malformed entry fails startup: silently dropping it would leave the
        administrator debugging a refusal they believe they allowed.
        """
        environ = os.environ if environ is None else environ
        hosts: set[str] = set()
        networks: list[IPNetwork] = []
        for raw in (environ.get(variable) or "").split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
                continue
            except ValueError:
                pass
            if not _is_hostname(entry):
                raise ValueError(
                    f"{variable} entry is not a hostname, IP address or CIDR network: {entry!r}"
                )
            hosts.add(entry)
        return cls(allowed_hosts=frozenset(hosts), allowed_networks=tuple(networks))

    def check_url(self, url: str) -> OutboundTarget:
        """Validate the URL and resolve its host; raise ``OutboundAddressRejected`` if refused."""
        scheme, host, port = self.check_url_shape(url)
        return self.check_host(host, port, scheme=scheme)

    def check_url_shape(self, url: str) -> tuple[str, str, int]:
        """Everything that can be refused without a DNS lookup: scheme, userinfo, host."""
        parsed = urlsplit(str(url or "").strip())
        scheme = parsed.scheme.casefold()
        try:
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise OutboundAddressRejected(OutboundRejection.INVALID_URL) from exc
        if not host:
            raise OutboundAddressRejected(OutboundRejection.INVALID_URL)
        if scheme not in _DEFAULT_PORTS:
            raise OutboundAddressRejected(OutboundRejection.SCHEME_NOT_ALLOWED)
        if parsed.username is not None or parsed.password is not None:
            raise OutboundAddressRejected(OutboundRejection.CREDENTIALS_IN_URL)
        host = _normalized_host(host)
        if scheme != "https" and not self._may_allowlist(host):
            # Plain HTTP only ever passes through the allowlist.
            raise OutboundAddressRejected(OutboundRejection.SCHEME_NOT_ALLOWED)
        return scheme, host, port or _DEFAULT_PORTS[scheme]

    def check_host(self, host: str, port: int, *, scheme: str = "https") -> OutboundTarget:
        """Resolve ``host`` once and admit it only if every address may be called."""
        scheme = scheme.casefold()
        host = _normalized_host(host)
        literal = _ip_literal(host)
        addresses = (str(literal),) if literal is not None else self._resolve(host, port)
        if not addresses:
            raise OutboundAddressRejected(OutboundRejection.UNRESOLVABLE)
        allowlisted = host in self.allowed_hosts or (
            bool(self.allowed_networks)
            and all(
                any(ipaddress.ip_address(item) in network for network in self.allowed_networks)
                for item in addresses
            )
        )
        if not allowlisted:
            if scheme != "https":
                raise OutboundAddressRejected(OutboundRejection.SCHEME_NOT_ALLOWED)
            if not all(is_public_address(ipaddress.ip_address(item)) for item in addresses):
                raise OutboundAddressRejected(OutboundRejection.NON_PUBLIC_ADDRESS)
        return OutboundTarget(scheme, host, port, addresses, allowlisted=allowlisted)

    def _may_allowlist(self, host: str) -> bool:
        # Network entries are judged after resolution; a hostname cannot be
        # ruled out before its addresses are known.
        return host in self.allowed_hosts or bool(self.allowed_networks)

    def _resolve(self, host: str, port: int) -> tuple[str, ...]:
        resolver = self.resolver or _system_resolver
        try:
            resolved = tuple(resolver(host, port))
        except (OSError, UnicodeError):
            return ()
        addresses: list[str] = []
        for value in resolved:
            address = _ip_literal(str(value))
            if address is None:
                continue
            text = str(address)
            if text not in addresses:
                addresses.append(text)
        return tuple(addresses)


# Well-known NAT64 prefix (RFC 6052): the low 32 bits are an IPv4 address.
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def is_public_address(address: IPAddress) -> bool:
    """True only for globally routable unicast addresses.

    The one predicate for every outbound guard (model and research endpoints
    here, source fetches in ``infrastructure.external.egress``). An IPv6
    address that carries an IPv4 address (IPv4-mapped, 6to4, NAT64) is public
    only if that IPv4 address is too; Teredo tunnels are never public.
    """

    if isinstance(address, ipaddress.IPv6Address):
        if address.teredo is not None:
            return False
        embedded = [view for view in address_views(address) if view is not address]
        if embedded:
            return all(is_public_address(view) for view in embedded)
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or address.is_reserved
        or address.is_multicast
        or not address.is_global
    )


def address_views(address: IPAddress) -> list[IPAddress]:
    """The address itself and every IPv4 address an IPv6 address carries.

    IPv4-mapped (``::ffff:a.b.c.d``), 6to4 (``2002::/16``), Teredo's client
    address and NAT64 (``64:ff9b::/96``). Allowlists match on any view, so an
    administrator who admits ``10.0.0.0/8`` also admits its mapped forms.
    """

    views: list[IPAddress] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            views.append(address.ipv4_mapped)
        if address.sixtofour is not None:
            views.append(address.sixtofour)
        if address.teredo is not None:
            views.append(address.teredo[1])
        if address in _NAT64_PREFIX:
            views.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return views


def _normalized_host(host: str) -> str:
    return str(host).strip().strip("[]").casefold().rstrip(".")


def _is_hostname(entry: str) -> bool:
    labels = entry.rstrip(".").split(".")
    return bool(labels) and all(
        label and len(label) <= 63 and label.replace("-", "").isalnum() and label.isascii()
        and not label.startswith("-") and not label.endswith("-")
        for label in labels
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
    "LLM_ALLOWED_PRIVATE_HOSTS_ENV",
    "OutboundAddressPolicy",
    "OutboundAddressRejected",
    "OutboundRejection",
    "OutboundTarget",
    "address_views",
    "is_public_address",
]
