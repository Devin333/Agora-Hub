"""An ``httpx2`` transport over ``PinnedConnector`` for SDK-based model clients.

The Anthropic SDK sends through an ``httpx2.Client``. Giving that client this
transport routes every SDK request through the same outbound policy and IP
pinning as the urllib clients. Each request opens its own connection (no
pool): model calls are long and few, and a pooled socket would outlive the
DNS answer it was checked against.
"""

from __future__ import annotations

import http.client
from collections.abc import Iterator

import httpx2

from infrastructure.security.outbound_address import OutboundAddressPolicy
from infrastructure.security.pinned_https import PinnedConnector, PinnedResponse

_CHUNK_BYTES = 64 * 1024
# Hop-by-hop headers http.client manages itself.
_SKIPPED_REQUEST_HEADERS = frozenset({"connection", "keep-alive", "transfer-encoding"})


class PinnedHTTPXTransport(httpx2.BaseTransport):
    def __init__(self, connector: PinnedConnector) -> None:
        self._connector = connector

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        body = request.read()
        timeouts = request.extensions.get("timeout") or {}
        headers = [
            (name, value)
            for name, value in request.headers.multi_items()
            if name.casefold() not in _SKIPPED_REQUEST_HEADERS
        ]
        try:
            response = self._connector.open(
                request.method,
                str(request.url),
                headers,
                body if body or request.method not in {"GET", "HEAD"} else None,
                connect_timeout=timeouts.get("connect"),
                read_timeout=timeouts.get("read"),
            )
        except TimeoutError as exc:
            raise httpx2.ConnectTimeout("connection timed out", request=request) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise httpx2.ConnectError(type(exc).__name__, request=request) from exc
        # http.client already removed the chunked framing; drop the header so
        # httpx2 does not try to decode it again.
        response_headers = [
            (name, value) for name, value in response.header_items() if name.casefold() != "transfer-encoding"
        ]
        return httpx2.Response(
            status_code=response.status,
            headers=response_headers,
            stream=_ResponseStream(response, request),
            extensions={"reason_phrase": response.reason.encode("ascii", "replace"), "http_version": b"HTTP/1.1"},
        )


class _ResponseStream(httpx2.SyncByteStream):
    def __init__(self, response: PinnedResponse, request: httpx2.Request) -> None:
        self._response = response
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        while True:
            try:
                chunk = self._response.read1(_CHUNK_BYTES)
            except TimeoutError as exc:
                raise httpx2.ReadTimeout("read timed out", request=self._request) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise httpx2.ReadError(type(exc).__name__, request=self._request) from exc
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._response.close()


def pinned_httpx_client(policy: OutboundAddressPolicy, *, connector: PinnedConnector | None = None) -> httpx2.Client:
    """An ``httpx2.Client`` that never follows redirects and never consults proxy env vars itself.

    Proxies from the environment are still honoured, by ``PinnedConnector``,
    after the policy check.
    """
    return httpx2.Client(
        transport=PinnedHTTPXTransport(connector or PinnedConnector(policy)),
        follow_redirects=False,
        trust_env=False,
    )


__all__ = ["PinnedHTTPXTransport", "pinned_httpx_client"]
