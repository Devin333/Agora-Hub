"""HTTP transports for user-supplied endpoints that connect only to checked addresses.

``urllib`` resolves the hostname again when it connects, so a DNS answer that
changes between the check and the connection (DNS rebinding) could reach an
internal address. These transports check the URL with an
``OutboundTargetPolicy`` on every request and open the TCP connection to one
of the addresses that passed, while TLS still verifies the certificate for the
original hostname. Redirects are never followed.

When an HTTPS proxy is configured for the host, the request is tunnelled
through it with CONNECT and the proxy resolves the name; the policy check still
runs first.
"""
from __future__ import annotations

import http.client
import socket
import ssl
from collections.abc import Iterator
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass

from infrastructure.security.outbound_target import OutboundTarget, OutboundTargetPolicy

DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024
_MAX_ERROR_BODY_BYTES = 64 * 1024


class PinnedHTTPTransport:
    def __init__(
        self,
        policy: OutboundTargetPolicy,
        *,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        ssl_context: ssl.SSLContext | None = None,
        use_environment_proxies: bool = True,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self._policy = policy
        self._max_response_bytes = max_response_bytes
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._use_environment_proxies = use_environment_proxies

    def __call__(self, request: Request, timeout_seconds: float) -> bytes:
        """Blocking transport: the whole body, bounded."""
        connection, response = self._open(request, timeout_seconds)
        try:
            body = response.read(self._max_response_bytes + 1)
            if len(body) > self._max_response_bytes:
                raise URLError("response exceeds the configured size limit")
            return body
        finally:
            connection.close()

    def stream(self, request: Request, timeout_seconds: float) -> Iterator[bytes]:
        """Streaming transport: response lines as they arrive."""
        connection, response = self._open(request, timeout_seconds)
        received = 0
        try:
            while True:
                line = response.readline(self._max_response_bytes - received + 1)
                if not line:
                    return
                received += len(line)
                if received > self._max_response_bytes:
                    raise URLError("response exceeds the configured size limit")
                yield line
        finally:
            connection.close()

    def _open(self, request: Request, timeout_seconds: float):
        url = request.full_url
        # A rejection is a configuration problem, not a network blip: it is raised
        # as OutboundTargetRejected so clients do not retry it.
        target = self._policy.check(url)
        connection = self._connection(target, timeout_seconds)
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        headers = {key: value for key, value in request.header_items()}
        headers.setdefault("Host", parsed.netloc.rsplit("@", 1)[-1])
        try:
            connection.request(request.get_method(), path, body=request.data, headers=headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            if isinstance(exc, TimeoutError):
                raise TimeoutError("request timed out") from exc
            raise URLError(exc) from exc
        if response.status >= 300:
            # 3xx included: a redirect could point anywhere, so it is reported, not followed.
            body = response.read(_MAX_ERROR_BODY_BYTES)
            connection.close()
            raise HTTPError(url, response.status, response.reason, response.headers, BytesIO(body))
        return connection, response

    def _connection(self, target: OutboundTarget, timeout_seconds: float) -> http.client.HTTPConnection:
        proxy = self._proxy_for(target)
        if proxy is not None:
            proxy_host, proxy_port = proxy
            connection: http.client.HTTPConnection
            if target.scheme == "https":
                connection = http.client.HTTPSConnection(
                    proxy_host, proxy_port, timeout=timeout_seconds, context=self._ssl_context
                )
            else:
                connection = http.client.HTTPConnection(proxy_host, proxy_port, timeout=timeout_seconds)
            connection.set_tunnel(target.host, target.port)
            return connection
        if target.scheme == "https":
            return _PinnedHTTPSConnection(target, timeout_seconds, self._ssl_context)
        return _PinnedHTTPConnection(target, timeout_seconds)

    def _proxy_for(self, target: OutboundTarget) -> tuple[str, int] | None:
        # Allowlisted private hosts are inside the network; never send them to a proxy.
        if not self._use_environment_proxies or target.allowlisted:
            return None
        proxy_url = getproxies().get(target.scheme)
        if not proxy_url or proxy_bypass(target.host):
            return None
        parsed = urlsplit(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        if not parsed.hostname:
            return None
        return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, target: OutboundTarget, timeout: float) -> None:
        super().__init__(target.host, target.port, timeout=timeout)
        self._target = target

    def connect(self) -> None:
        self.sock = _connect_any(self._target, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, target: OutboundTarget, timeout: float, context: ssl.SSLContext) -> None:
        super().__init__(target.host, target.port, timeout=timeout, context=context)
        self._target = target
        self._tls_context = context

    def connect(self) -> None:
        raw = _connect_any(self._target, self.timeout)
        self.sock = self._tls_context.wrap_socket(raw, server_hostname=self._target.host)


def _connect_any(target: OutboundTarget, timeout: float | None) -> socket.socket:
    last_error: OSError | None = None
    for address in target.addresses:
        try:
            return socket.create_connection((address, target.port), timeout=timeout)
        except OSError as exc:
            last_error = exc
    raise last_error or OSError("no checked address to connect to")


__all__ = ["DEFAULT_MAX_RESPONSE_BYTES", "PinnedHTTPTransport"]
