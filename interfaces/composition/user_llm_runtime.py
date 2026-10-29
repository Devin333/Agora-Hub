"""Build and cache provider clients for user model connections.

A client is built once per connection revision: the key is decrypted, the
vendor client is constructed over the pinned outbound transport, and the result
is kept until that connection changes. Each call still asks storage which
connection is assigned to the purpose, so an edit, a reassignment or a delete
takes effect on the next call.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from backend.foundation.llm_connections import (
    ConnectionOwner,
    CredentialBinding,
    CredentialCipher,
    CredentialCipherError,
    LLMPurpose,
    ModelConnection,
    ModelConnectionNotFoundError,
    ModelConnectionRepository,
    ModelConnectionStoreError,
    ModelConnectionUnavailable,
    ModelConnectionUnavailableReason,
)
from backend.foundation.llm_connections.routing import PurposeRoutedLLMClient, ResolvedModelConnection
from framework.llm.models import LLMClient
from framework.llm.providers import ProviderClientSettings, build_provider_client
from framework.shared.secret_text import SecretText
from infrastructure.security.outbound_target import OutboundTargetPolicy
from infrastructure.security.pinned_transport import PinnedHTTPTransport

DEFAULT_CLIENT_CACHE_SIZE = 256

ClientBuilder = Callable[[ModelConnection, SecretText], LLMClient]


@dataclass(frozen=True)
class ConnectionClientOptions:
    timeout_seconds: float = 120.0
    max_attempts: int = 2


def pinned_client_builder(
    policy: OutboundTargetPolicy,
    *,
    options: ConnectionClientOptions | None = None,
) -> ClientBuilder:
    """Vendor clients whose every request passes the outbound policy and connects to checked addresses."""
    options = options or ConnectionClientOptions()
    transport = PinnedHTTPTransport(policy)

    def build(connection: ModelConnection, api_key: SecretText) -> LLMClient:
        return build_provider_client(
            client_settings(connection, api_key, options=options),
            transport=transport,
            stream_transport=transport.stream,
        )

    return build


def client_settings(
    connection: ModelConnection,
    api_key: SecretText,
    *,
    options: ConnectionClientOptions,
) -> ProviderClientSettings:
    return ProviderClientSettings(
        provider=connection.provider,
        deployment_id=connection.deployment_id,
        base_url=connection.base_url,
        model=connection.model,
        api_key=api_key,
        reasoning_effort=connection.reasoning_effort,
        reasoning_parameter=connection.reasoning_parameter,
        max_output_tokens=connection.max_output_tokens,
        thinking_off=connection.capabilities.thinking_off,
        timeout_seconds=options.timeout_seconds,
        max_attempts=options.max_attempts,
    )


class UserLLMClientResolver:
    def __init__(
        self,
        repository: ModelConnectionRepository,
        cipher: CredentialCipher,
        *,
        client_builder: ClientBuilder,
        cache_size: int = DEFAULT_CLIENT_CACHE_SIZE,
    ) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self._repository = repository
        self._cipher = cipher
        self._build = client_builder
        self._cache_size = cache_size
        # (owner, connection_id) -> client for the newest revision seen.
        self._clients: OrderedDict[tuple[ConnectionOwner, str], ResolvedModelConnection] = OrderedDict()
        self._lock = threading.Lock()

    def resolve(self, owner: ConnectionOwner, purpose: LLMPurpose) -> ResolvedModelConnection:
        purpose = LLMPurpose(purpose)
        try:
            connection = self._repository.resolve_purpose(owner, purpose)
        except ModelConnectionStoreError as exc:
            raise _unavailable(purpose, exc) from exc
        if connection is None:
            raise ModelConnectionUnavailable(purpose, ModelConnectionUnavailableReason.NOT_ASSIGNED)
        cached = self._cached(owner, connection)
        if cached is not None:
            return cached
        return self._load(owner, connection, purpose)

    def routed_client(self, purpose: LLMPurpose) -> PurposeRoutedLLMClient:
        return PurposeRoutedLLMClient(purpose, self)

    def forget(self, owner: ConnectionOwner, connection_id: str) -> None:
        """Drop a cached client right away, e.g. after its connection was deleted."""
        with self._lock:
            self._clients.pop((owner, connection_id), None)

    def _cached(self, owner: ConnectionOwner, connection: ModelConnection) -> ResolvedModelConnection | None:
        key = (owner, connection.connection_id)
        with self._lock:
            cached = self._clients.get(key)
            if cached is None or cached.connection.revision != connection.revision:
                return None
            self._clients.move_to_end(key)
            return cached

    def _load(
        self,
        owner: ConnectionOwner,
        connection: ModelConnection,
        purpose: LLMPurpose,
    ) -> ResolvedModelConnection:
        try:
            stored = self._repository.load_with_credential(owner, connection.connection_id)
        except ModelConnectionNotFoundError as exc:
            # Deleted between the assignment lookup and this read.
            raise ModelConnectionUnavailable(purpose, ModelConnectionUnavailableReason.NOT_ASSIGNED) from exc
        except ModelConnectionStoreError as exc:
            raise _unavailable(purpose, exc) from exc
        try:
            api_key = self._cipher.decrypt(
                stored.credential,
                binding=CredentialBinding(owner, stored.connection.connection_id),
            )
        except CredentialCipherError as exc:
            raise ModelConnectionUnavailable(
                purpose, ModelConnectionUnavailableReason.CREDENTIAL_UNREADABLE
            ) from exc
        resolved = ResolvedModelConnection(connection=stored.connection, client=self._build(stored.connection, api_key))
        key = (owner, stored.connection.connection_id)
        with self._lock:
            current = self._clients.get(key)
            # A concurrent load may already hold a newer revision; keep the newest.
            if current is None or current.connection.revision <= resolved.connection.revision:
                self._clients[key] = resolved
                self._clients.move_to_end(key)
            while len(self._clients) > self._cache_size:
                self._clients.popitem(last=False)
        return resolved


def _unavailable(purpose: LLMPurpose, exc: ModelConnectionStoreError) -> ModelConnectionUnavailable:
    reason = (
        ModelConnectionUnavailableReason.STORE_UNAVAILABLE
        if exc.retryable
        else ModelConnectionUnavailableReason.CONNECTION_INVALID
    )
    return ModelConnectionUnavailable(purpose, reason)


__all__ = [
    "DEFAULT_CLIENT_CACHE_SIZE",
    "ClientBuilder",
    "ConnectionClientOptions",
    "UserLLMClientResolver",
    "client_settings",
    "pinned_client_builder",
]
