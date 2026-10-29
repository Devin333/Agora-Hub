"""User-owned model connections and the purposes they serve.

A connection names one vendor endpoint, one model on it and how that model is
called. The API key is never part of these objects: storage keeps it encrypted
and the domain only knows whether one is configured.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from framework.llm.models.reasoning import ReasoningEffort, ReasoningParameterStyle, ordered_efforts
from framework.llm.providers.catalog import AnthropicThinkingOff, ProviderKind

MAX_OWNER_ID_LENGTH = 128
MAX_CONNECTION_ID_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 80
MAX_MODEL_ID_LENGTH = 200
MAX_BASE_URL_LENGTH = 2048
MIN_CONTEXT_WINDOW_TOKENS = 1_024
MAX_CONTEXT_WINDOW_TOKENS = 20_000_000
MAX_CONNECTIONS_PER_OWNER = 32

_OWNER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]*")
_CONNECTION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class LLMPurpose(StrEnum):
    """What a model call is for; each purpose is routed to one connection."""

    ANSWER = "answer"
    EVIDENCE_SUMMARY = "evidence_summary"
    SECTION_NAVIGATION = "section_navigation"
    CANDIDATE_GENERATION = "candidate_generation"


@dataclass(frozen=True, slots=True)
class ConnectionOwner:
    """Authenticated identity that owns connections; every lookup is scoped by it."""

    user_id: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "user_id", _identifier(self.user_id, "user_id", MAX_OWNER_ID_LENGTH, _OWNER_PATTERN))
        if self.tenant_id is not None:
            object.__setattr__(
                self, "tenant_id", _identifier(self.tenant_id, "tenant_id", MAX_OWNER_ID_LENGTH, _OWNER_PATTERN)
            )

    @property
    def tenant_scope(self) -> str:
        return self.tenant_id or ""

    def to_dict(self) -> dict[str, str | None]:
        return {"user_id": self.user_id, "tenant_id": self.tenant_id}


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """Model facts captured when the connection was saved.

    Saving them keeps request-time validation local: a stored effort is always
    one the model offered, without asking the vendor again.
    """

    reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    thinking_off: AnthropicThinkingOff | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasoning_efforts", ordered_efforts(self.reasoning_efforts))
        if self.thinking_off is not None:
            object.__setattr__(self, "thinking_off", AnthropicThinkingOff(self.thinking_off))

    def accepts(self, effort: ReasoningEffort | None) -> bool:
        return effort is None or effort in self.reasoning_efforts

    def to_dict(self) -> dict[str, Any]:
        return {
            "reasoning_efforts": [effort.value for effort in self.reasoning_efforts],
            "thinking_off": self.thinking_off.value if self.thinking_off is not None else None,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ModelCapabilities:
        return cls(
            reasoning_efforts=tuple(ReasoningEffort.parse(item) for item in payload.get("reasoning_efforts") or ()),
            thinking_off=payload.get("thinking_off"),
        )


@dataclass(frozen=True, slots=True)
class ModelConnection:
    connection_id: str
    owner: ConnectionOwner
    provider: ProviderKind
    display_name: str
    base_url: str
    model: str
    context_window_tokens: int
    capabilities: ModelCapabilities = field(default_factory=ModelCapabilities)
    max_output_tokens: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    # Only meaningful for custom OpenAI-compatible endpoints.
    reasoning_parameter: ReasoningParameterStyle | None = None
    credential_configured: bool = False
    revision: int = 1
    verified_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner, ConnectionOwner):
            raise TypeError("owner must be a ConnectionOwner")
        object.__setattr__(
            self,
            "connection_id",
            _identifier(self.connection_id, "connection_id", MAX_CONNECTION_ID_LENGTH, _CONNECTION_ID_PATTERN),
        )
        object.__setattr__(self, "provider", ProviderKind.parse(self.provider))
        object.__setattr__(self, "display_name", _text(self.display_name, "display_name", MAX_DISPLAY_NAME_LENGTH))
        object.__setattr__(self, "base_url", _base_url(self.base_url))
        object.__setattr__(self, "model", _text(self.model, "model", MAX_MODEL_ID_LENGTH))
        object.__setattr__(
            self,
            "context_window_tokens",
            _bounded_int(self.context_window_tokens, "context_window_tokens",
                         MIN_CONTEXT_WINDOW_TOKENS, MAX_CONTEXT_WINDOW_TOKENS),
        )
        if self.max_output_tokens is not None:
            object.__setattr__(
                self,
                "max_output_tokens",
                _bounded_int(self.max_output_tokens, "max_output_tokens", 1, self.context_window_tokens),
            )
        if not isinstance(self.capabilities, ModelCapabilities):
            raise TypeError("capabilities must be ModelCapabilities")
        if self.reasoning_effort is not None:
            object.__setattr__(self, "reasoning_effort", ReasoningEffort.parse(self.reasoning_effort))
        if not self.capabilities.accepts(self.reasoning_effort):
            raise ValueError(f"{self.model} does not offer reasoning effort {self.reasoning_effort.value}")
        if self.reasoning_parameter is not None:
            object.__setattr__(self, "reasoning_parameter", ReasoningParameterStyle(self.reasoning_parameter))
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("revision must be a positive integer")

    @property
    def deployment_id(self) -> str:
        """Identity of this exact configuration; caches and projections are scoped by it."""
        return f"{self.connection_id}@{self.revision}"

    def revised(self, **changes: Any) -> ModelConnection:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class PurposeAssignment:
    purpose: LLMPurpose
    connection_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", LLMPurpose(self.purpose))
        object.__setattr__(
            self,
            "connection_id",
            _identifier(self.connection_id, "connection_id", MAX_CONNECTION_ID_LENGTH, _CONNECTION_ID_PATTERN),
        )


def _identifier(value: Any, field_name: str, maximum: int, pattern: re.Pattern[str]) -> str:
    value = _text(value, field_name, maximum)
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} contains unsupported characters")
    return value


def _text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be text")
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be blank")
    if "\x00" in value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{field_name} must not contain control characters")
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return value


def _base_url(value: Any) -> str:
    # Which hosts and schemes may be reached is outbound policy, checked before
    # every request; the domain only insists on an absolute web URL.
    value = _text(value, "base_url", MAX_BASE_URL_LENGTH).rstrip("/")
    if not value.casefold().startswith(("https://", "http://")):
        raise ValueError("base_url must be an absolute http(s) URL")
    return value


def _bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{field_name} must be an integer between {minimum} and {maximum}")
    return value


__all__ = [
    "MAX_CONNECTIONS_PER_OWNER",
    "ConnectionOwner",
    "LLMPurpose",
    "ModelCapabilities",
    "ModelConnection",
    "PurposeAssignment",
]
