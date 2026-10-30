"""A model client whose provider endpoint is chosen per call by the caller's binding."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable

from framework.llm.clients.endpoint import (
    LLMEndpoint,
    LLMEndpointIdentity,
    resolve_invocation_endpoint,
)
from framework.llm.models import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent

LLMEndpointClientFactory = Callable[[LLMEndpoint], LLMClient]

DEFAULT_CLIENT_CACHE_SIZE = 256


class InvocationLLMClient:
    """Serve one purpose (answer, evidence summary, ...) for whichever account is bound.

    Each call resolves the endpoint the bound account assigned to
    ``purpose`` and delegates to a provider client built for exactly that
    endpoint. Built clients are reused while the endpoint is unchanged: the
    cache key includes the connection revision and a digest of the key, so a
    changed key or setting always gets a new client.
    """

    def __init__(
        self,
        purpose: str,
        client_factory: LLMEndpointClientFactory,
        *,
        cache_size: int = DEFAULT_CLIENT_CACHE_SIZE,
    ) -> None:
        purpose = str(purpose or "").strip()
        if not purpose:
            raise ValueError("purpose must be non-empty")
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self.purpose = purpose
        self._factory = client_factory
        self._cache_size = cache_size
        self._clients: OrderedDict[tuple[str, ...], LLMClient] = OrderedDict()
        self._lock = threading.Lock()

    def complete(self, request: LLMRequest) -> LLMResponse:
        return self._client_for(self._endpoint()).complete(request)

    def stream(self, request: LLMRequest) -> Iterable[LLMStreamEvent]:
        # Resolve eagerly so the endpoint comes from the caller's context, not
        # from whichever context first advances the returned iterator.
        return self._client_for(self._endpoint()).stream(request)

    def endpoint_identity(self) -> LLMEndpointIdentity:
        return self._endpoint().identity

    def _endpoint(self) -> LLMEndpoint:
        return resolve_invocation_endpoint(self.purpose)

    def _client_for(self, endpoint: LLMEndpoint) -> LLMClient:
        if endpoint.connection_ref is None:
            return self._factory(endpoint)
        key = (
            endpoint.connection_ref,
            hashlib.sha256(endpoint.api_key.encode("utf-8")).hexdigest(),
        )
        with self._lock:
            client = self._clients.get(key)
            if client is not None:
                self._clients.move_to_end(key)
                return client
        client = self._factory(endpoint)
        with self._lock:
            self._clients[key] = client
            self._clients.move_to_end(key)
            while len(self._clients) > self._cache_size:
                _evicted_key, evicted = self._clients.popitem(last=False)
                _close_quietly(evicted)
        return client

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            _close_quietly(client)


def _close_quietly(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


__all__ = ["DEFAULT_CLIENT_CACHE_SIZE", "InvocationLLMClient", "LLMEndpointClientFactory"]
