"""Validation of what a user submits for a model connection.

A draft is checked against the catalog before anything is stored: catalog
providers accept only their own models and reasoning tiers and take no
address or limits from the user; custom providers require them. The custom
address also passes the outbound policy's URL checks here, so a refused
address is reported when it is saved rather than on the first model call.
"""

from __future__ import annotations

from dataclasses import dataclass

from framework.llm.clients import validate_endpoint_base_url
from framework.llm.models.reasoning import ReasoningEffort, ReasoningWire
from infrastructure.security.outbound_address import OutboundAddressPolicy, OutboundAddressRejected
from interfaces.services.model_connections.catalog import (
    MODEL_IDENTIFIER,
    CatalogProvider,
    CustomProvider,
    ModelProviderCatalog,
    Provider,
)
from interfaces.services.model_connections.errors import (
    endpoint_not_allowed,
    invalid,
    provider_unsupported,
)

MAX_LABEL_LENGTH = 64
MAX_KEY_LENGTH = 512
MIN_KEY_LENGTH = 8
# Custom windows outside this range are typing mistakes, not models.
MIN_CONTEXT_WINDOW_TOKENS = 1024
MAX_CONTEXT_WINDOW_TOKENS = 20_000_000


@dataclass(frozen=True, slots=True)
class ConnectionDraft:
    """The fields of a connection form; ``None`` means not given."""

    provider_id: str
    model: str
    label: str | None = None
    reasoning_effort: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    reasoning_parameter: str | None = None
    structured_output: str | None = None


@dataclass(frozen=True, slots=True)
class ValidatedDraft:
    provider: Provider
    model: str
    label: str
    reasoning_effort: ReasoningEffort | None
    # ``None`` keeps the stored key, when keeping it is allowed.
    api_key: str | None
    base_url: str | None
    context_window_tokens: int | None
    max_output_tokens: int | None
    reasoning_parameter: ReasoningWire | None
    structured_output: str | None

    @property
    def key_destination(self) -> tuple[str, str | None]:
        """Where the key is sent; a stored key is never reused for another one."""
        return self.provider.id, self.base_url


def validate_draft(
    draft: ConnectionDraft,
    *,
    catalog: ModelProviderCatalog,
    outbound: OutboundAddressPolicy,
) -> ValidatedDraft:
    provider = catalog.provider(str(draft.provider_id or "").strip())
    if provider is None:
        raise provider_unsupported()
    api_key = normalized_key(draft.api_key) if draft.api_key is not None else None
    if isinstance(provider, CatalogProvider):
        return _catalog_draft(draft, provider, api_key=api_key)
    return _custom_draft(draft, provider, api_key=api_key, outbound=outbound)


def validate_custom_address(base_url: str | None, *, outbound: OutboundAddressPolicy) -> str:
    """Normalize a custom base URL and refuse what the outbound policy can refuse without DNS."""

    if not isinstance(base_url, str) or not base_url.strip():
        raise invalid("A custom provider needs its base URL", field="baseUrl")
    try:
        normalized = validate_endpoint_base_url(base_url)
    except ValueError:
        raise invalid("The base URL must be an absolute http(s) URL without credentials", field="baseUrl") from None
    try:
        outbound.check_url_shape(normalized)
    except OutboundAddressRejected:
        raise endpoint_not_allowed() from None
    return normalized


def normalized_key(api_key: str) -> str:
    secret = str(api_key or "").strip()
    if (
        len(secret) < MIN_KEY_LENGTH
        or len(secret) > MAX_KEY_LENGTH
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in secret)
    ):
        raise invalid(
            f"The API key must be {MIN_KEY_LENGTH}-{MAX_KEY_LENGTH} visible ASCII characters", field="apiKey"
        )
    return secret


def _catalog_draft(draft: ConnectionDraft, provider: CatalogProvider, *, api_key: str | None) -> ValidatedDraft:
    for name, field in (
        ("base_url", "baseUrl"),
        ("context_window_tokens", "contextWindowTokens"),
        ("max_output_tokens", "maxOutputTokens"),
        ("reasoning_parameter", "reasoningParameter"),
        ("structured_output", "structuredOutput"),
    ):
        if getattr(draft, name) is not None:
            raise invalid(f"{provider.id} takes {field} from the catalog", field=field)
    model = provider.model(str(draft.model or "").strip())
    if model is None:
        raise provider_unsupported()
    effort = _effort(draft.reasoning_effort)
    if not model.reasoning.offers(effort):
        raise invalid(f"{model.id} does not offer this reasoning effort", field="reasoningEffort")
    return ValidatedDraft(
        provider=provider,
        model=model.id,
        label=_label(draft.label, default=f"{provider.label} · {model.label}"),
        reasoning_effort=effort,
        api_key=api_key,
        base_url=None,
        context_window_tokens=None,
        max_output_tokens=None,
        reasoning_parameter=None,
        structured_output=None,
    )


def _custom_draft(
    draft: ConnectionDraft,
    provider: CustomProvider,
    *,
    api_key: str | None,
    outbound: OutboundAddressPolicy,
) -> ValidatedDraft:
    base_url = validate_custom_address(draft.base_url, outbound=outbound)
    model = str(draft.model or "").strip()
    if not MODEL_IDENTIFIER.fullmatch(model):
        raise invalid("The model id must be 1-128 letters, digits or ._:/@-", field="model")
    window = _bounded_int(
        draft.context_window_tokens,
        field="contextWindowTokens",
        minimum=MIN_CONTEXT_WINDOW_TOKENS,
        maximum=MAX_CONTEXT_WINDOW_TOKENS,
    )
    max_output = None
    if draft.max_output_tokens is not None:
        max_output = _bounded_int(draft.max_output_tokens, field="maxOutputTokens", minimum=1, maximum=window - 1)
    wire = ReasoningWire.NONE
    if draft.reasoning_parameter is not None:
        try:
            wire = ReasoningWire.parse(draft.reasoning_parameter)
        except ValueError:
            raise invalid("Unknown reasoning parameter style", field="reasoningParameter") from None
        if wire is not ReasoningWire.NONE and not provider.offers_reasoning_parameter(wire):
            raise invalid(f"{provider.id} cannot send reasoning as {wire.value}", field="reasoningParameter")
    effort = _effort(draft.reasoning_effort)
    if effort is not None and effort not in wire.offered_efforts():
        raise invalid("This reasoning parameter style cannot express that effort", field="reasoningEffort")
    mode = str(draft.structured_output or "none").strip()
    if not provider.offers_structured_output(mode):
        raise invalid(f"{provider.id} does not accept structured output mode {mode}", field="structuredOutput")
    return ValidatedDraft(
        provider=provider,
        model=model,
        label=_label(draft.label, default=f"{provider.label} · {model}"),
        reasoning_effort=effort,
        api_key=api_key,
        base_url=base_url,
        context_window_tokens=window,
        max_output_tokens=max_output,
        reasoning_parameter=wire if wire is not ReasoningWire.NONE else None,
        structured_output=mode,
    )


def _effort(value: str | None) -> ReasoningEffort | None:
    if value is None:
        return None
    try:
        return ReasoningEffort.parse(value)
    except ValueError:
        raise invalid("Unknown reasoning effort", field="reasoningEffort") from None


def _label(value: str | None, *, default: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return default[:MAX_LABEL_LENGTH]
    if len(text) > MAX_LABEL_LENGTH or any(ord(char) < 0x20 for char in text):
        raise invalid(f"The name must be at most {MAX_LABEL_LENGTH} characters", field="label")
    return text


def _bounded_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise invalid(f"{field} must be an integer between {minimum} and {maximum}", field=field)
    return value


__all__ = [
    "ConnectionDraft",
    "MAX_CONTEXT_WINDOW_TOKENS",
    "MIN_CONTEXT_WINDOW_TOKENS",
    "ValidatedDraft",
    "normalized_key",
    "validate_custom_address",
    "validate_draft",
]
