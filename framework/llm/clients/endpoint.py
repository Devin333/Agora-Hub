from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from framework.llm.models.reasoning import ReasoningSetting
from framework.llm.structured_output.projection import ProviderStructuredOutputCapability

CREDENTIAL_MODE_ENV = "NEWSROOM_LLM_CREDENTIAL_MODE"


class LLMCredentialMode(StrEnum):
    """Where a deployment's model calls take their endpoint and key from.

    ``environment`` keeps the deployment-wide configuration: clients read the
    key named by their config from the process environment. ``invocation``
    serves many accounts from one process: model clients are
    :class:`~framework.llm.clients.invocation.InvocationLLMClient` instances,
    every call must run inside :func:`bind_llm_endpoint_source`, and the
    process environment is never consulted, so a call that escapes the
    binding fails instead of spending the operator's key.
    """

    ENVIRONMENT = "environment"
    INVOCATION = "invocation"


class LLMCredentialUnavailableError(RuntimeError):
    """No usable endpoint is bound for the current invocation and purpose."""

    def __init__(self, code: str, message: str, *, purpose: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.purpose = purpose


class LLMWireProtocol(StrEnum):
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"


@dataclass(frozen=True, slots=True)
class LLMEndpointIdentity:
    """Non-secret identity of the endpoint that serves (or would serve) a call."""

    provider: str
    model: str
    base_url: str


@dataclass(frozen=True, slots=True)
class LLMEndpoint:
    """One caller's provider endpoint, resolved for a single invocation."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    provider: str
    protocol: LLMWireProtocol = LLMWireProtocol.OPENAI_CHAT
    # What this provider/model can enforce for structured output. ``None``
    # means nothing is declared and structured requests are refused.
    structured_output_capability: ProviderStructuredOutputCapability | None = None
    reasoning: ReasoningSetting = field(default_factory=ReasoningSetting)
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    # Stable non-secret reference to the stored connection and its revision;
    # clients built for an endpoint are cached under it.
    connection_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", validate_endpoint_base_url(self.base_url))
        model = str(self.model or "").strip()
        if not model or len(model) > 256 or any(ord(char) < 0x20 for char in model):
            raise ValueError("model must be 1-256 printable characters")
        object.__setattr__(self, "model", model)
        api_key = str(self.api_key or "").strip()
        if not api_key or len(api_key) > 512 or any(ord(char) < 0x21 or ord(char) > 0x7E for char in api_key):
            raise ValueError("api_key must be 1-512 visible ASCII characters")
        object.__setattr__(self, "api_key", api_key)
        provider = str(self.provider or "").strip()
        if not provider:
            raise ValueError("provider must be non-empty")
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "protocol", LLMWireProtocol(self.protocol))
        if not isinstance(self.reasoning, ReasoningSetting):
            raise TypeError("reasoning must be ReasoningSetting")
        for name in ("context_window_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
                raise ValueError(f"{name} must be a positive integer")
        capability = self.structured_output_capability
        if capability is not None:
            if not isinstance(capability, ProviderStructuredOutputCapability):
                raise TypeError(
                    "structured_output_capability must be ProviderStructuredOutputCapability"
                )
            if capability.provider != provider:
                raise ValueError(
                    "structured_output_capability must belong to the endpoint provider"
                )

    @property
    def identity(self) -> LLMEndpointIdentity:
        return LLMEndpointIdentity(provider=self.provider, model=self.model, base_url=self.base_url)


# Called with the purpose of the model call (an opaque label owned by the
# composition, for example ``answer``); returns the endpoint assigned to it.
LLMEndpointSource = Callable[[str], LLMEndpoint]

_CURRENT_ENDPOINT_SOURCE: ContextVar[LLMEndpointSource | None] = ContextVar(
    "newsroom_llm_endpoint_source", default=None
)


@contextmanager
def bind_llm_endpoint_source(source: LLMEndpointSource) -> Iterator[None]:
    """Bind the endpoint resolver for the dynamic extent of one invocation.

    The source is called lazily, once per provider call, so callers that never
    reach a model never decrypt a credential. Worker threads see the binding
    only when they run inside a copied context (``contextvars.copy_context``).
    """

    if not callable(source):
        raise TypeError("endpoint source must be callable")
    token = _CURRENT_ENDPOINT_SOURCE.set(source)
    try:
        yield
    finally:
        _CURRENT_ENDPOINT_SOURCE.reset(token)


def resolve_invocation_endpoint(purpose: str) -> LLMEndpoint:
    source = _CURRENT_ENDPOINT_SOURCE.get()
    if source is None:
        raise LLMCredentialUnavailableError(
            "model_connection_required",
            "No model connection is bound to this request",
            purpose=purpose,
        )
    endpoint = source(purpose)
    if not isinstance(endpoint, LLMEndpoint):
        raise TypeError("endpoint source must return LLMEndpoint")
    return endpoint


def require_invocation_endpoints(
    purposes: Iterable[str],
    env: Mapping[str, str] | None = None,
) -> None:
    """Fail before work starts when a purpose of this invocation has no endpoint.

    In environment mode the process key is checked by the client itself, so
    this is a no-op. In invocation mode each purpose is resolved once, which
    surfaces a missing or unreadable connection before a run is admitted
    rather than after part of it has executed.
    """

    if llm_credential_mode(env) is LLMCredentialMode.INVOCATION:
        for purpose in purposes:
            resolve_invocation_endpoint(purpose)


def credential_unavailable_cause(error: BaseException) -> LLMCredentialUnavailableError | None:
    """Return the credential failure behind ``error``, if one caused it.

    Harness layers wrap provider failures; walking ``__cause__`` and
    ``__context__`` lets the interface report the account-level reason
    instead of a generic run failure.
    """

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, LLMCredentialUnavailableError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def llm_credential_mode(env: Mapping[str, str] | None = None) -> LLMCredentialMode:
    values = env if env is not None else os.environ
    raw = str(values.get(CREDENTIAL_MODE_ENV) or LLMCredentialMode.ENVIRONMENT.value).strip()
    try:
        return LLMCredentialMode(raw)
    except ValueError as exc:
        raise ValueError(f"{CREDENTIAL_MODE_ENV} must be environment or invocation") from exc


def validate_endpoint_base_url(value: str) -> str:
    """Normalize an absolute HTTP(S) base URL without credentials, query or fragment.

    This checks only the shape. Which hosts and schemes a deployment may call
    is decided by the network policy of the transport that sends the request.
    """

    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query or fragment")
    if not parsed.hostname:
        raise ValueError("base_url must be an absolute URL")
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("base_url must use HTTPS")
    return text


__all__ = [
    "CREDENTIAL_MODE_ENV",
    "LLMCredentialMode",
    "LLMCredentialUnavailableError",
    "LLMEndpoint",
    "LLMEndpointIdentity",
    "LLMEndpointSource",
    "LLMWireProtocol",
    "bind_llm_endpoint_source",
    "credential_unavailable_cause",
    "llm_credential_mode",
    "require_invocation_endpoints",
    "resolve_invocation_endpoint",
    "validate_endpoint_base_url",
]
