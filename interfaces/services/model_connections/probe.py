"""Checks that reach a provider on the user's behalf: key verification and model discovery.

Exactly one listing (and at most one tiny completion, for custom addresses
that do not list models) is sent per check and nothing is retried. Provider
failures become public connection errors; the provider's own text and the
key never reach the caller.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from typing import Protocol

from framework.llm.clients import LLMEndpoint
from framework.llm.clients.openai_compatible import LLMProviderError
from framework.llm.models import LLMRequest
from framework.llm.models.reasoning import ReasoningEffort
from interfaces.services.model_connections.errors import (
    ModelConnectionError,
    credential_rejected,
    discovery_unsupported,
    endpoint_not_allowed,
    model_unavailable,
    provider_unreachable,
)

MAX_DISCOVERED_MODELS = 1000
_REJECTED_STATUS_CODES = frozenset({401, 403})
# Answers of an address that has no model list endpoint.
_NO_LISTING_STATUS_CODES = frozenset({404, 405, 501})
# Answers to the probe completion that name the model, not the key or the network.
_MODEL_REFUSED_STATUS_CODES = frozenset({400, 404, 422})
_PROBE_MAX_TOKENS = 16


class ModelProbeClient(Protocol):
    def list_models(self) -> tuple[str, ...]: ...
    def complete(self, request: LLMRequest): ...


ProbeClientFactory = Callable[[LLMEndpoint], ModelProbeClient]


def verify_endpoint(factory: ProbeClientFactory, endpoint: LLMEndpoint, *, custom: bool) -> None:
    """Raise a ``ModelConnectionError`` unless the key may call the endpoint's model."""

    with _client(factory, _probe_endpoint(endpoint)) as client:
        try:
            available = client.list_models()
        except LLMProviderError as exc:
            if custom and exc.status_code in _NO_LISTING_STATUS_CODES:
                _probe_completion(client)
                return
            raise _listing_error(exc) from None
        except OSError:
            raise provider_unreachable() from None
    if endpoint.model not in available:
        raise model_unavailable()


def discover_models(factory: ProbeClientFactory, endpoint: LLMEndpoint) -> tuple[str, ...]:
    with _client(factory, endpoint) as client:
        try:
            available = client.list_models()
        except LLMProviderError as exc:
            if exc.status_code in _NO_LISTING_STATUS_CODES:
                raise discovery_unsupported() from None
            raise _listing_error(exc) from None
        except OSError:
            raise provider_unreachable() from None
    return tuple(sorted({str(item) for item in available if str(item).strip()}))[:MAX_DISCOVERED_MODELS]


def _probe_completion(client: ModelProbeClient) -> None:
    request = LLMRequest(
        messages=[{"role": "user", "content": "Reply with OK."}],
        max_tokens=_PROBE_MAX_TOKENS,
        metadata={"component": "model_connection_probe"},
        max_transport_attempts=1,
    )
    try:
        client.complete(request)
    except LLMProviderError as exc:
        if exc.error_type == "endpoint_not_allowed":
            raise endpoint_not_allowed() from None
        if exc.status_code in _REJECTED_STATUS_CODES:
            raise credential_rejected() from None
        if exc.status_code in _MODEL_REFUSED_STATUS_CODES:
            raise model_unavailable() from None
        raise provider_unreachable() from None
    except OSError:
        raise provider_unreachable() from None


def _listing_error(exc: LLMProviderError) -> ModelConnectionError:
    if exc.error_type == "endpoint_not_allowed":
        return endpoint_not_allowed()
    if exc.status_code in _REJECTED_STATUS_CODES:
        return credential_rejected()
    return provider_unreachable()


def _probe_endpoint(endpoint: LLMEndpoint) -> LLMEndpoint:
    # The probe completion only checks that the model answers; it switches
    # reasoning off where the provider allows so it costs a few tokens and
    # returns within the verification timeout.
    reasoning = endpoint.reasoning
    effort = ReasoningEffort.OFF if ReasoningEffort.OFF in reasoning.wire.offered_efforts() else None
    return replace(endpoint, reasoning=replace(reasoning, effort=effort), connection_ref=None)


@contextmanager
def _client(factory: ProbeClientFactory, endpoint: LLMEndpoint) -> Iterator[ModelProbeClient]:
    client = factory(endpoint)
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


__all__ = [
    "MAX_DISCOVERED_MODELS",
    "ModelProbeClient",
    "ProbeClientFactory",
    "discover_models",
    "verify_endpoint",
]
