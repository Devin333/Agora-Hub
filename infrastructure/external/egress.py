"""The single guarded path for outbound HTTP(S) fetches.

Every hop of a fetch (the first request, each redirect, robots.txt) is
checked here, at the moment it is about to connect:

* only ``http`` and ``https``; no credentials in the URL; no https->http
  downgrade on redirect;
* the host must match ``allowed_domains`` when that list is configured;
* the host is resolved once and every returned address must be public or
  inside ``allowed_private_networks``; a mixed answer is refused outright;
* the connection goes to the vetted address, never to a fresh lookup, so a
  DNS answer cannot change between the check and the connect;
* ``Authorization``, ``Cookie`` and ``Proxy-Authorization`` are dropped when
  a redirect leaves the original origin;
* the whole fetch runs inside one ``fetch_deadline`` budget, enforced by a
  watchdog that closes the socket when the budget runs out.

Environment proxy variables are deliberately ignored: through a proxy the
guard would vet the proxy's address instead of the real destination.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPException, HTTPResponse, HTTPSConnection, IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from infrastructure.external.fetch_deadline import (
    FetchDeadlineExceeded,
    bounded_timeout,
    deadline_budget_seconds,
    deadline_expires_at,
    deadline_passed,
    fetch_deadline,
    spent_by_timeout,
    SocketWatchdog,
)

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], list[tuple[Any, ...]]]

ALLOWED_SCHEMES = frozenset({"http", "https"})
_CREDENTIAL_HEADERS = ("Authorization", "Proxy-authorization", "Cookie")
_NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")


def system_resolver(host: str, port: int) -> list[tuple[Any, ...]]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


class EgressDenied(ValueError):
    """The destination is not allowed. Policy refusals are never retried."""

    def __init__(self, reason: str, *, url: str | None = None, host: str | None = None) -> None:
        self.reason = reason
        self.url = url
        self.host = host
        target = display_origin(url) if url else host
        super().__init__(f"outbound fetch refused: {reason}" + (f" ({target})" if target else ""))


class TooManyRedirectsError(ValueError):
    def __init__(self, url: str, max_redirects: int) -> None:
        self.url = url
        self.max_redirects = max_redirects
        super().__init__(
            f"fetch exceeded max_redirects={max_redirects} at a redirect to {display_origin(url)}"
        )


class EgressConnectError(URLError):
    """No connection was established, so nothing was sent.

    Raised for DNS failures and refused or unreachable addresses. A URLError
    so retry and error classification treat it like any connect failure.
    """


@dataclass(frozen=True)
class EgressPolicy:
    """Where outbound fetches may go.

    ``allowed_domains`` empty means any public host. Non-public addresses
    (loopback, private, link-local, cloud metadata, CGNAT, multicast, ...)
    are refused unless covered by ``allowed_private_networks``, for example
    ``("10.20.0.0/16",)`` for an intranet source.
    """

    allowed_domains: tuple[str, ...] = ()
    allowed_private_networks: tuple[str, ...] = ()
    max_redirects: int = 3
    _networks: tuple[IPNetwork, ...] = field(init=False, repr=False, compare=False, default=())

    def __post_init__(self) -> None:
        if type(self.max_redirects) is not int or self.max_redirects < 0:
            raise ValueError("max_redirects must be a non-negative integer")
        domains: list[str] = []
        for value in self.allowed_domains:
            try:
                domains.append(normalize_host(str(value)))
            except EgressDenied as exc:
                raise ValueError(f"allowed_domains entry is not a host name: {value!r}") from exc
        networks = tuple(_parse_network(value) for value in self.allowed_private_networks)
        object.__setattr__(self, "allowed_domains", tuple(dict.fromkeys(domains)))
        object.__setattr__(self, "allowed_private_networks", tuple(str(network) for network in networks))
        object.__setattr__(self, "_networks", networks)

    def check_url(self, url: str, *, previous_url: str | None = None) -> str:
        """Validate a URL before it is requested; return its normalized host."""

        parts = urlsplit(url)
        scheme = parts.scheme.casefold()
        if scheme not in ALLOWED_SCHEMES:
            raise EgressDenied("only http and https URLs may be fetched", url=url)
        if "@" in parts.netloc:
            raise EgressDenied("URLs with embedded credentials are not fetched", url=url)
        if previous_url is not None and urlsplit(previous_url).scheme.casefold() == "https" and scheme == "http":
            raise EgressDenied("redirect would downgrade https to http", url=url)
        try:
            hostname = parts.hostname
            parts.port
        except ValueError as exc:
            raise EgressDenied("URL is malformed", url=url) from exc
        return self.check_host(hostname or "")

    def check_host(self, host: str) -> str:
        normalized = normalize_host(host)
        if self.allowed_domains and not any(
            _domain_matches(normalized, domain) for domain in self.allowed_domains
        ):
            raise EgressDenied("host is not in allowed_domains", host=normalized)
        literal = _ip_literal(normalized)
        if literal is not None:
            self.check_address(literal, host=normalized)
        return normalized

    def check_address(self, address: IPAddress, *, host: str) -> None:
        if is_public_address(address) or self._allows_private(address):
            return
        raise EgressDenied(f"{host} resolves to non-public address {address}", host=host)

    def resolve(self, host: str, port: int, resolver: Resolver) -> list[tuple[Any, ...]]:
        """Resolve once and vet every answer; a single bad answer refuses all."""

        infos = resolver(host, port)
        if not infos:
            raise OSError(f"{host} resolved to no addresses")
        candidates: list[tuple[Any, ...]] = []
        for family, socktype, proto, _canonname, sockaddr in infos:
            address = _ip_literal(str(sockaddr[0]))
            if address is None:
                raise EgressDenied(f"{host} resolved to an unrecognised address {sockaddr!r}", host=host)
            self.check_address(address, host=host)
            candidate = (family, socktype, proto, sockaddr)
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _allows_private(self, address: IPAddress) -> bool:
        return any(view in network for view in _address_views(address) for network in self._networks)


def is_public_address(address: IPAddress) -> bool:
    """Globally routable unicast, including every IPv4 address embedded in IPv6."""

    return all(view.is_global and not view.is_multicast for view in _address_views(address))


def normalize_host(host: str) -> str:
    text = str(host or "").strip().rstrip(".").casefold()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if not text:
        raise EgressDenied("URL has no host")
    literal = _ip_literal(text)
    if literal is not None:
        return str(literal)
    try:
        return text.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise EgressDenied("host is not a valid domain name", host=text) from exc


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def _ip_literal(text: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None


def _address_views(address: IPAddress) -> Iterator[IPAddress]:
    yield address
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            yield address.ipv4_mapped
        if address.sixtofour is not None:
            yield address.sixtofour
        if address.teredo is not None:
            yield address.teredo[1]
        if address in _NAT64_PREFIX:
            yield ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)


def _parse_network(value: Any) -> IPNetwork:
    try:
        return ipaddress.ip_network(str(value).strip(), strict=True)
    except ValueError as exc:
        raise ValueError(f"allowed_private_networks entry is not a network: {value!r}") from exc


def display_origin(url: str) -> str:
    """Scheme, host and port only.

    Paths, query strings and userinfo can all carry secrets (incoming-webhook
    URLs keep their token in the path), so error text never includes them.
    """

    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, "", "", ""))


class GuardedHTTPResponse(HTTPResponse):
    """Reads past the fetch budget fail as ``FetchDeadlineExceeded``."""

    _watchdog: SocketWatchdog | None = None

    # http.client returns a short read(amt)/readinto when the peer closes
    # before the declared Content-Length, so a cut connection would look like
    # a complete body. A short read with bytes still owed is a truncation.

    def read(self, amt: int | None = None) -> bytes:
        def read_body() -> bytes:
            owed = self._owed(amt)
            data = super(GuardedHTTPResponse, self).read(amt)
            if owed is not None and len(data) < owed:
                raise IncompleteRead(data, owed - len(data))
            return data

        return self._guarded(read_body)

    def readinto(self, b: Any) -> int:
        def read_body() -> int:
            owed = self._owed(len(memoryview(b)))
            received = super(GuardedHTTPResponse, self).readinto(b)
            if owed is not None and received < owed:
                raise IncompleteRead(bytes(memoryview(b)[:received]), owed - received)
            return received

        return self._guarded(read_body)

    def _owed(self, amt: int | None) -> int | None:
        """Bytes this read must return, or None when the length is unknown."""

        if amt is None or self.fp is None or self.length is None:
            return None
        return min(int(amt), self.length)

    def close(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
        super().close()

    def release_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()

    def _guarded(self, read: Callable[[], Any]) -> Any:
        watchdog = self._watchdog
        if watchdog is not None:
            watchdog.raise_if_spent()
        try:
            value = read()
        except (OSError, HTTPException, ValueError) as exc:
            if watchdog is not None:
                watchdog.raise_if_spent(exc)
            raise
        if watchdog is not None:
            watchdog.raise_if_spent()
        return value


class _GuardedConnectionMixin:
    response_class = GuardedHTTPResponse
    _egress: "_Egress"
    _watchdog: SocketWatchdog | None = None
    _response_owns_watchdog = False

    def _guarded_connect(self) -> None:
        connection: Any = self
        if connection._tunnel_host:
            # A CONNECT tunnel would make the guard vet the proxy, not the destination.
            raise EgressDenied("proxy tunnels are not used for guarded fetches", host=connection.host)
        sock = self._egress.open_connection(connection.host, connection.port, connection.source_address)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        connection.sock = sock
        self._watchdog = self._egress.new_watchdog()
        if self._watchdog is not None:
            self._watchdog.watch(sock)

    def getresponse(self) -> HTTPResponse:
        watchdog = self._watchdog
        # HTTPConnection.getresponse closes the connection itself when the
        # response will close it (HTTP/1.0, "Connection: close"); the response
        # keeps the socket, so it must keep the watchdog through that close.
        self._response_owns_watchdog = True
        try:
            response = super().getresponse()  # type: ignore[misc]
        except (OSError, HTTPException) as exc:
            if watchdog is not None:
                watchdog.cancel()
                watchdog.raise_if_spent(exc)
            raise
        except BaseException:
            if watchdog is not None:
                watchdog.cancel()
            raise
        if watchdog is not None:
            if isinstance(response, GuardedHTTPResponse):
                response._watchdog = watchdog
            else:
                watchdog.cancel()
        return response

    def close(self) -> None:
        if self._watchdog is not None and not self._response_owns_watchdog:
            self._watchdog.cancel()
        super().close()  # type: ignore[misc]


class _GuardedHTTPConnection(_GuardedConnectionMixin, HTTPConnection):
    def connect(self) -> None:
        self._guarded_connect()


class _GuardedHTTPSConnection(_GuardedConnectionMixin, HTTPSConnection):
    def connect(self) -> None:
        # Same steps as HTTPSConnection.connect, with the raw socket opened by
        # the guard. The certificate is checked against the requested host name.
        self._guarded_connect()
        try:
            self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)
        except OSError as exc:
            # A failed handshake (untrusted certificate, wrong host, timeout)
            # happens before any request byte is sent.
            self.sock.close()
            if deadline_passed() or spent_by_timeout(exc, deadline_expires_at()):
                raise FetchDeadlineExceeded(deadline_budget_seconds(), before_send=True) from exc
            raise EgressConnectError(exc) from exc
        if self._watchdog is not None:
            self._watchdog.watch(self.sock)


class _Egress:
    """Per-fetch state shared by every hop's connection."""

    def __init__(self, policy: EgressPolicy, resolver: Resolver, socket_timeout: float | None) -> None:
        self.policy = policy
        self.resolver = resolver
        self.socket_timeout = socket_timeout

    def new_watchdog(self) -> SocketWatchdog | None:
        return SocketWatchdog.for_current_deadline()

    def open_connection(self, host: str, port: int, source_address: Any = None) -> socket.socket:
        """Connect to a vetted address. Every failure here happens before a byte is sent."""

        normalized = self.policy.check_host(host)
        try:
            resolved = self.policy.resolve(normalized, port, self.resolver)
        except OSError as exc:
            if deadline_passed():
                raise FetchDeadlineExceeded(deadline_budget_seconds(), before_send=True) from exc
            raise EgressConnectError(exc) from exc
        candidates = _interleave_families(resolved)
        last_error: OSError | None = None
        for index, (family, socktype, proto, sockaddr) in enumerate(candidates):
            sock = socket.socket(family, socktype, proto)
            try:
                # Each remaining address gets an equal share of the budget to
                # connect, so a black-holed first address (typically a broken
                # IPv6 route) cannot spend the whole fetch before the next is tried.
                timeout = bounded_timeout(self.socket_timeout)
                if timeout is not None:
                    timeout = timeout / (len(candidates) - index)
                sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                sock.settimeout(bounded_timeout(self.socket_timeout))
                return sock
            except OSError as exc:
                sock.close()
                last_error = exc
                if deadline_passed() or spent_by_timeout(exc, deadline_expires_at()):
                    raise FetchDeadlineExceeded(deadline_budget_seconds(), before_send=True) from exc
            except BaseException:
                sock.close()
                raise
        assert last_error is not None
        raise EgressConnectError(last_error) from last_error

    def http_connection(self, host: str, **kwargs: Any) -> HTTPConnection:
        connection = _GuardedHTTPConnection(host, **kwargs)
        connection._egress = self
        return connection

    def https_connection(self, host: str, **kwargs: Any) -> HTTPSConnection:
        connection = _GuardedHTTPSConnection(host, **kwargs)
        connection._egress = self
        return connection


class _GuardedHTTPHandler(HTTPHandler):
    def __init__(self, egress: _Egress) -> None:
        super().__init__()
        self._egress = egress

    def http_open(self, req: Request) -> Any:
        return self.do_open(self._egress.http_connection, req)


class _GuardedHTTPSHandler(HTTPSHandler):
    def __init__(self, egress: _Egress, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._egress = egress
        self._ssl_context = context

    def https_open(self, req: Request) -> Any:
        return self.do_open(self._egress.https_connection, req, context=self._ssl_context)


class _GuardedRedirectHandler(HTTPRedirectHandler):
    """Re-vet every hop and drop credentials when the origin changes."""

    def __init__(self, policy: EgressPolicy) -> None:
        super().__init__()
        self._policy = policy
        self._redirects = 0

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        target = urljoin(req.full_url, newurl)
        self._redirects += 1
        try:
            if self._redirects > self._policy.max_redirects:
                raise TooManyRedirectsError(target, self._policy.max_redirects)
            self._policy.check_url(target, previous_url=req.full_url)
        except (TooManyRedirectsError, EgressDenied):
            fp.close()
            raise
        new_request = super().redirect_request(req, fp, code, msg, headers, target)
        if new_request is not None and url_origin(req.full_url) != url_origin(target):
            for header in _CREDENTIAL_HEADERS:
                new_request.headers.pop(header, None)
                new_request.unredirected_hdrs.pop(header, None)
        return new_request


def open_guarded(
    request: Request,
    policy: EgressPolicy,
    *,
    timeout_seconds: float,
    resolver: Resolver = system_resolver,
    ssl_context: ssl.SSLContext | None = None,
) -> HTTPResponse:
    """Open ``request`` through the guard inside a ``timeout_seconds`` budget.

    The budget joins any enclosing ``fetch_deadline`` (the shorter wins) and
    keeps running while the caller reads the returned response.
    """

    policy.check_url(request.full_url)
    with fetch_deadline(timeout_seconds):
        egress = _Egress(policy, resolver, timeout_seconds)
        opener = build_opener(
            ProxyHandler({}),
            _GuardedHTTPHandler(egress),
            _GuardedHTTPSHandler(egress, ssl_context or ssl.create_default_context()),
            _GuardedRedirectHandler(policy),
        )
        try:
            return opener.open(request, timeout=bounded_timeout(timeout_seconds))
        except HTTPError as exc:
            # The caller may still read the error body; it is small and the
            # socket timeout bounds it, so the watchdog thread can go now.
            if isinstance(exc.fp, GuardedHTTPResponse):
                exc.fp.release_watchdog()
            raise
        except EgressConnectError:
            raise
        except URLError as exc:
            if isinstance(
                exc.reason,
                (EgressDenied, EgressConnectError, FetchDeadlineExceeded, TooManyRedirectsError),
            ):
                raise exc.reason from exc
            reason = exc.reason if isinstance(exc.reason, BaseException) else exc
            if deadline_passed() or spent_by_timeout(reason, deadline_expires_at()):
                raise FetchDeadlineExceeded(deadline_budget_seconds()) from exc
            raise


def _interleave_families(candidates: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
    """Alternate address families in resolver order (RFC 8305 section 4).

    A host with several AAAA records and a broken IPv6 path then reaches an
    IPv4 address after one failed attempt rather than after all of them.
    """

    by_family: dict[Any, list[tuple[Any, ...]]] = {}
    for candidate in candidates:
        by_family.setdefault(candidate[0], []).append(candidate)
    queues = list(by_family.values())
    ordered: list[tuple[Any, ...]] = []
    while any(queues):
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
    return ordered


def url_origin(url: str) -> tuple[str, str, int | None]:
    """(scheme, normalized host, effective port): the unit credentials are bound to."""

    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    port = parts.port or {"http": 80, "https": 443}.get(scheme)
    return scheme, normalize_host(parts.hostname or ""), port
