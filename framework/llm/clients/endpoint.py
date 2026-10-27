from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from ipaddress import ip_address
from urllib.parse import urlsplit

CREDENTIAL_MODE_ENV = "NEWSROOM_LLM_CREDENTIAL_MODE"


class LLMCredentialMode(StrEnum):
    """Where an OpenAI-compatible client takes its endpoint and key from.

    ``environment`` keeps the deployment-wide configuration: the client reads
    the key named by its config from the process environment on every call.
    ``invocation`` serves many accounts from one process: every call must run
    inside :func:`bind_llm_endpoint_source`, and the process environment is
    never consulted, so a call that escapes the binding fails instead of
    spending the operator's key.
    """

    ENVIRONMENT = "environment"
    INVOCATION = "invocation"


class LLMCredentialUnavailableError(RuntimeError):
    """No usable credential is bound for the current invocation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class LLMEndpoint:
    """One caller's provider endpoint, resolved for a single invocation."""

    base_url: str
    model: str
    api_key: str = field(repr=False)

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


LLMEndpointSource = Callable[[], LLMEndpoint]

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


def resolve_invocation_endpoint() -> LLMEndpoint:
    source = _CURRENT_ENDPOINT_SOURCE.get()
    if source is None:
        raise LLMCredentialUnavailableError(
            "model_credential_required",
            "No model credential is bound to this request",
        )
    endpoint = source()
    if not isinstance(endpoint, LLMEndpoint):
        raise TypeError("endpoint source must return LLMEndpoint")
    return endpoint


def require_invocation_credential(env: Mapping[str, str] | None = None) -> None:
    """Fail before work starts when this invocation would have no credential.

    In environment mode the process key is checked by the client itself, so
    this is a no-op. In invocation mode the bound source is resolved once,
    which surfaces a missing or unreadable account credential before a run is
    admitted rather than after part of it has executed.
    """

    if llm_credential_mode(env) is LLMCredentialMode.INVOCATION:
        resolve_invocation_endpoint()


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
    """Accept an HTTPS base URL, or plain HTTP only for a loopback address."""

    text = str(value or "").strip().rstrip("/")
    parsed = urlsplit(text)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("base_url must not contain credentials, query or fragment")
    if not parsed.hostname:
        raise ValueError("base_url must be an absolute URL")
    if parsed.scheme == "https":
        return text
    if parsed.scheme == "http" and _is_loopback(parsed.hostname):
        return text
    raise ValueError("base_url must use HTTPS")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


__all__ = [
    "CREDENTIAL_MODE_ENV",
    "credential_unavailable_cause",
    "require_invocation_credential",
    "LLMCredentialMode",
    "LLMCredentialUnavailableError",
    "LLMEndpoint",
    "LLMEndpointSource",
    "bind_llm_endpoint_source",
    "llm_credential_mode",
    "resolve_invocation_endpoint",
    "validate_endpoint_base_url",
]
