"""HTTP(S) connections that reach only the addresses an outbound policy admitted.

``urllib`` and ``httpx`` resolve the hostname again when they connect, so a DNS
answer that changes between the check and the connection (DNS rebinding) could
reach an internal address. ``PinnedConnector`` checks the URL with an
``OutboundAddressPolicy`` on every request, opens the TCP connection to one of
the addresses that passed, and still verifies the TLS certificate for the
original hostname. Redirects are never followed: a 3xx is returned to the
caller like any other status.

When the process environment configures a proxy for the scheme (and the host
is not bypassed or allowlisted), the request is tunnelled through it with
CONNECT; the proxy resolves the name, but the policy check still runs first.

``PinnedUrllibTransport`` adapts the connector to the ``(Request, timeout)``
transport seam of the OpenAI-compatible client; ``pinned_httpx`` adapts it to
the httpx transport the Anthropic SDK uses.
"""

from __future__ import annotations

import base64
import http.client
import socket
import ssl
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, getproxies, proxy_bypass

from infrastructure.security.outbound_address import OutboundAddressPolicy, OutboundTarget

DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_ERROR_BODY_BYTES = 64 * 1024

Header = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _ProxyRoute:
    host: str
    port: int
    authorization: str | None


class PinnedResponse:
    """An open response whose connection closes with it."""

    def __init__(self, connection: http.client.HTTPConnection, response: http.client.HTTPResponse) -> None:
        self._connection = connection
        self._response = response
        self.status: int = response.status
        self.reason: str = response.reason or ""

    @property
    def headers(self) -> http.client.HTTPMessage:
        return self._response.headers

    def header_items(self) -> list[Header]:
        # ``items()`` keeps repeated headers (Set-Cookie and the like) apart.
        return list(self._response.headers.items())

    def read(self, amount: int | None = None) -> bytes:
        return self._response.read(amount)

    def read1(self, amount: int) -> bytes:
        return self._response.read1(amount)

    def readline(self, limit: int) -> bytes:
        return self._response.readline(limit)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


class PinnedConnector:
    """Send one request per connection to an address the policy admitted."""

    def __init__(
        self,
        policy: OutboundAddressPolicy,
        *,
        ssl_context: ssl.SSLContext | None = None,
        use_environment_proxies: bool = True,
    ) -> None:
        self.policy = policy
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._use_environment_proxies = use_environment_proxies

    def open(
        self,
        method: str,
        url: str,
        headers: Iterable[Header],
        body: bytes | None,
        *,
        connect_timeout: float | None,
        read_timeout: float | None,
    ) -> PinnedResponse:
        """Send the request and return the response once its headers arrive.

        A refused target raises ``OutboundAddressRejected`` before any socket
        is opened. Network failures raise ``OSError`` (``TimeoutError`` for
        timeouts) or ``http.client.HTTPException``.
        """
        target = self.policy.check_url(url)
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        connection = self._connection(target, connect_timeout, read_timeout)
        try:
            connection.putrequest(method.upper(), path, skip_host=True, skip_accept_encoding=True)
            names: set[str] = set()
            for name, value in headers:
                names.add(name.casefold())
                connection.putheader(name, value)
            if "host" not in names:
                connection.putheader("Host", parsed.netloc)
            if body is not None and "content-length" not in names and "transfer-encoding" not in names:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(message_body=body)
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        return PinnedResponse(connection, response)

    def _connection(
        self, target: OutboundTarget, connect_timeout: float | None, read_timeout: float | None
    ) -> http.client.HTTPConnection:
        proxy = self._proxy_for(target)
        if proxy is None:
            if target.scheme == "https":
                return _PinnedHTTPSConnection(target, connect_timeout, read_timeout, self._ssl_context)
            return _PinnedHTTPConnection(target, connect_timeout, read_timeout)
        tunnel_headers = {"Proxy-Authorization": proxy.authorization} if proxy.authorization else None
        connection: http.client.HTTPConnection
        if target.scheme == "https":
            connection = http.client.HTTPSConnection(
                proxy.host, proxy.port, timeout=read_timeout, context=self._ssl_context
            )
        else:
            connection = http.client.HTTPConnection(proxy.host, proxy.port, timeout=read_timeout)
        connection.set_tunnel(target.host, target.port, headers=tunnel_headers)
        return connection

    def _proxy_for(self, target: OutboundTarget) -> _ProxyRoute | None:
        # Allowlisted private hosts are inside the network; never send them to a proxy.
        if not self._use_environment_proxies or target.allowlisted:
            return None
        proxy_url = getproxies().get(target.scheme)
        if not proxy_url or proxy_bypass(target.host):
            return None
        parsed = urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        if not parsed.hostname:
            return None
        authorization = None
        if parsed.username is not None:
            credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}"
            authorization = "Basic " + base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return _ProxyRoute(
            host=parsed.hostname,
            port=parsed.port or (443 if parsed.scheme == "https" else 80),
            authorization=authorization,
        )


class PinnedUrllibTransport:
    """``Transport``/``StreamTransport`` for clients built on ``urllib.request.Request``.

    Status codes from 300 up raise ``HTTPError`` like ``urlopen`` does for
    errors; a redirect is reported, never followed.
    """

    def __init__(
        self,
        connector: PinnedConnector,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self._connector = connector
        self._max_response_bytes = max_response_bytes

    @classmethod
    def for_policy(cls, policy: OutboundAddressPolicy, **kwargs: object) -> PinnedUrllibTransport:
        return cls(PinnedConnector(policy), **kwargs)  # type: ignore[arg-type]

    def __call__(self, request: Request, timeout_seconds: float) -> bytes:
        """Blocking transport: the whole body, bounded."""
        response = self._open(request, timeout_seconds)
        try:
            body = _guarded_read(lambda: response.read(self._max_response_bytes + 1))
            if len(body) > self._max_response_bytes:
                raise URLError("response exceeds the configured size limit")
            return body
        finally:
            response.close()

    def stream(self, request: Request, timeout_seconds: float) -> Iterator[bytes]:
        """Streaming transport: response lines as they arrive."""
        response = self._open(request, timeout_seconds)
        received = 0
        try:
            while True:
                line = _guarded_read(lambda: response.readline(self._max_response_bytes - received + 1))
                if not line:
                    return
                received += len(line)
                if received > self._max_response_bytes:
                    raise URLError("response exceeds the configured size limit")
                yield line
        finally:
            response.close()

    def _open(self, request: Request, timeout_seconds: float) -> PinnedResponse:
        url = request.full_url
        try:
            response = self._connector.open(
                request.get_method(),
                url,
                request.header_items(),
                request.data if isinstance(request.data, bytes) or request.data is None else bytes(request.data),
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
            )
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise URLError(exc) from exc
        if response.status >= 300:
            # 3xx included: a redirect could point anywhere, so it is reported, not followed.
            raise _http_error(url, response)
        return response


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: OutboundTarget, connect_timeout: float | None, read_timeout: float | None) -> None:
        super().__init__(target.host, target.port, timeout=read_timeout)
        self._target = target
        self._connect_timeout = connect_timeout

    def connect(self) -> None:
        self.sock = connect_to_checked_address(self._target, self._connect_timeout, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        target: OutboundTarget,
        connect_timeout: float | None,
        read_timeout: float | None,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(target.host, target.port, timeout=read_timeout, context=context)
        self._target = target
        self._connect_timeout = connect_timeout
        self._tls_context = context

    def connect(self) -> None:
        raw = connect_to_checked_address(self._target, self._connect_timeout, self.timeout)
        try:
            # SNI and certificate verification use the hostname, not the pinned IP.
            self.sock = self._tls_context.wrap_socket(raw, server_hostname=self._target.host)
        except BaseException:
            raw.close()
            raise


def connect_to_checked_address(
    target: OutboundTarget, connect_timeout: float | None, read_timeout: float | None
) -> socket.socket:
    """Open a TCP connection to the first admitted address that answers."""
    last_error: OSError | None = None
    for address in target.addresses:
        try:
            sock = socket.create_connection((address, target.port), timeout=connect_timeout)
        except OSError as exc:
            last_error = exc
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        sock.settimeout(read_timeout)
        return sock
    raise last_error or OSError("no admitted address to connect to")


class RedirectLimitExceeded(ValueError):
    def __init__(self, url: str, max_redirects: int) -> None:
        self.url = url
        self.max_redirects = max_redirects
        super().__init__(f"exceeded max_redirects={max_redirects}")


class PinnedOpenedResponse:
    """What ``urlopen`` returns, for callers that read ``url``, ``headers`` and the body."""

    def __init__(self, url: str, response: PinnedResponse) -> None:
        self.url = url
        self.status = response.status
        self._response = response

    @property
    def headers(self) -> http.client.HTTPMessage:
        return self._response.headers

    def geturl(self) -> str:
        return self.url

    def read(self, amount: int | None = None) -> bytes:
        return _guarded_read(lambda: self._response.read(amount))

    def __iter__(self) -> Iterator[bytes]:
        while True:
            line = _guarded_read(lambda: self._response.readline(_MAX_LINE_BYTES))
            if not line:
                return
            yield line

    def close(self) -> None:
        self._response.close()

    def __enter__(self) -> PinnedOpenedResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


_MAX_LINE_BYTES = 1024 * 1024
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Credentials a redirect must not carry to another origin.
_ORIGIN_BOUND_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


class PinnedRedirectingOpener:
    """``urlopen`` replacement that re-checks the policy on every redirect hop.

    For fetchers whose targets legitimately redirect (DOI resolvers, publisher
    CDNs). Each hop is a new pinned request, so a redirect to an internal
    address is refused like a direct request would be.
    """

    def __init__(self, connector: PinnedConnector) -> None:
        self._connector = connector

    def open(self, request: Request, *, timeout_seconds: float, max_redirects: int) -> PinnedOpenedResponse:
        method = request.get_method()
        url = request.full_url
        headers = list(request.header_items())
        body = request.data if isinstance(request.data, bytes) or request.data is None else bytes(request.data)
        origin = _origin(url)
        redirects = 0
        while True:
            try:
                response = self._connector.open(
                    method, url, headers, body,
                    connect_timeout=timeout_seconds, read_timeout=timeout_seconds,
                )
            except TimeoutError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise URLError(exc) from exc
            location = response.headers.get("Location") if response.status in _REDIRECT_STATUSES else None
            if location is None:
                if response.status >= 400:
                    raise _http_error(url, response)
                return PinnedOpenedResponse(url, response)
            response.close()
            redirects += 1
            if redirects > max_redirects:
                raise RedirectLimitExceeded(url, max_redirects)
            url = urljoin(url, location)
            if response.status == 303 or (response.status in {301, 302} and method == "POST"):
                method, body = "GET", None
                headers = [(name, value) for name, value in headers if name.casefold() not in {"content-type", "content-length"}]
            if _origin(url) != origin:
                headers = [(name, value) for name, value in headers if name.casefold() not in _ORIGIN_BOUND_HEADERS]
                origin = _origin(url)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.casefold(), (parsed.hostname or "").casefold(), parsed.port


def _http_error(url: str, response: PinnedResponse) -> HTTPError:
    try:
        body = response.read(_MAX_ERROR_BODY_BYTES)
    except (OSError, http.client.HTTPException):
        body = b""
    finally:
        response.close()
    return HTTPError(url, response.status, response.reason, response.headers, BytesIO(body))


def _guarded_read(read):
    try:
        return read()
    except TimeoutError:
        raise
    except (OSError, http.client.HTTPException) as exc:
        raise URLError(exc) from exc


__all__ = [
    "DEFAULT_MAX_RESPONSE_BYTES",
    "PinnedConnector",
    "PinnedOpenedResponse",
    "PinnedRedirectingOpener",
    "PinnedResponse",
    "PinnedUrllibTransport",
    "RedirectLimitExceeded",
    "connect_to_checked_address",
]
