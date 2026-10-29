"""Route each model call to the connection its owner assigned to the call's purpose."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from backend.foundation.llm_connections.errors import (
    ModelConnectionUnavailable,
    ModelConnectionUnavailableReason,
)
from backend.foundation.llm_connections.models import ConnectionOwner, LLMPurpose, ModelConnection
from backend.foundation.llm_connections.scope import current_llm_owner
from framework.llm.models import LLMClient, LLMRequest, LLMResponse, LLMStreamEvent

# Input window cap and floor, matching what a context assembler can work with.
MAX_INPUT_WINDOW_TOKENS = 1_000_000
MIN_INPUT_WINDOW_TOKENS = 4_096


@dataclass(frozen=True, slots=True)
class ModelCallWindow:
    """Token budget for one call: what may be sent and what may be generated."""

    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


def connection_call_window(connection: ModelConnection, *, requested_output_tokens: int) -> ModelCallWindow:
    """The window a call through ``connection`` gets when the caller wants ``requested_output_tokens``.

    The output reservation is capped by what the model can emit; the input
    window is what remains of the model's context window.
    """
    if requested_output_tokens < 1:
        raise ValueError("requested_output_tokens must be positive")
    output = requested_output_tokens
    if connection.max_output_tokens is not None:
        output = min(output, connection.max_output_tokens)
    available = min(connection.context_window_tokens - output, MAX_INPUT_WINDOW_TOKENS)
    if available < MIN_INPUT_WINDOW_TOKENS:
        # A tiny model window cannot hold the reservation and a usable prompt; give
        # the prompt its floor and shrink the reservation instead.
        available = min(MIN_INPUT_WINDOW_TOKENS, connection.context_window_tokens - 1)
        output = connection.context_window_tokens - available
    return ModelCallWindow(max_input_tokens=available, max_output_tokens=output)


@dataclass(frozen=True)
class ResolvedModelConnection:
    connection: ModelConnection
    client: LLMClient

    def call_window(self, *, requested_output_tokens: int) -> ModelCallWindow:
        return connection_call_window(self.connection, requested_output_tokens=requested_output_tokens)


@runtime_checkable
class ModelConnectionResolver(Protocol):
    def resolve(self, owner: ConnectionOwner, purpose: LLMPurpose) -> ResolvedModelConnection:
        """Raises ``ModelConnectionUnavailable`` when the owner has no usable connection."""
        ...


OwnerProvider = Callable[[], ConnectionOwner | None]


class PurposeRoutedLLMClient:
    """An ``LLMClient`` whose vendor, model and key are chosen per call.

    The owner comes from the active ``llm_call_scope``. With no owner, or no
    connection assigned to this purpose, the call fails with
    ``ModelConnectionUnavailable``; there is no shared fallback key.
    """

    def __init__(
        self,
        purpose: LLMPurpose,
        resolver: ModelConnectionResolver,
        *,
        owner_provider: OwnerProvider = current_llm_owner,
    ) -> None:
        self.purpose = LLMPurpose(purpose)
        self._resolver = resolver
        self._owner_provider = owner_provider

    def resolve(self) -> ResolvedModelConnection:
        owner = self._owner_provider()
        if owner is None:
            raise ModelConnectionUnavailable(self.purpose, ModelConnectionUnavailableReason.NO_CALLER)
        return self._resolver.resolve(owner, self.purpose)

    def call_window(self, *, requested_output_tokens: int) -> ModelCallWindow:
        return self.resolve().call_window(requested_output_tokens=requested_output_tokens)

    def complete(self, request: LLMRequest) -> LLMResponse:
        resolved = self.resolve()
        return resolved.client.complete(_for_connection(request, resolved.connection))

    def stream(self, request: LLMRequest) -> Iterator[LLMStreamEvent]:
        resolved = self.resolve()
        yield from resolved.client.stream(_for_connection(request, resolved.connection))


def _for_connection(request: LLMRequest, connection: ModelConnection) -> LLMRequest:
    """Point the request at the connection's model and keep its output within the model's limit."""
    changes: dict = {}
    if request.model != connection.model:
        changes["model"] = connection.model
    if (
        request.max_tokens is not None
        and connection.max_output_tokens is not None
        and request.max_tokens > connection.max_output_tokens
    ):
        changes["max_tokens"] = connection.max_output_tokens
    return request.clone(**changes) if changes else request


__all__ = [
    "MAX_INPUT_WINDOW_TOKENS",
    "MIN_INPUT_WINDOW_TOKENS",
    "ModelCallWindow",
    "ModelConnectionResolver",
    "PurposeRoutedLLMClient",
    "ResolvedModelConnection",
    "connection_call_window",
]
