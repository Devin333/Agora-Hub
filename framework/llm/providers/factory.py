"""Build the provider client for one user-configured model connection."""
from __future__ import annotations

from dataclasses import dataclass, field

from framework.llm.clients.anthropic_messages import (
    ANTHROPIC_RETRYABLE_STATUS_CODES,
    AnthropicMessagesClient,
    AnthropicMessagesConfig,
)
from framework.llm.clients.openai_compatible import OpenAICompatibleClient, OpenAICompatibleConfig
from framework.llm.clients.transport import LLMRetryPolicy, StreamTransport, Transport
from framework.llm.context.anthropic import (
    DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS,
    AnthropicModelOptions,
    AnthropicThinkingOff,
)
from framework.llm.models.client import LLMClient
from framework.llm.models.reasoning import ReasoningEffort, ReasoningParameterStyle
from framework.llm.providers.catalog import ProviderKind, ProviderSpec, WireProtocol, provider_spec
from framework.llm.structured_output import ProviderStructuredOutputCapability
from framework.shared.secret_text import SecretText

DEFAULT_CONNECTION_TIMEOUT_SECONDS = 120.0
DEFAULT_CONNECTION_MAX_ATTEMPTS = 2
_RETRY_DELAYS_SECONDS = (1.0, 2.0)


@dataclass(frozen=True)
class ProviderClientSettings:
    provider: ProviderKind
    # Stable per connection revision; scopes structured-output projections and caches.
    deployment_id: str
    base_url: str
    model: str
    api_key: SecretText = field(repr=False)
    reasoning_effort: ReasoningEffort | None = None
    # Only consulted for custom OpenAI-compatible endpoints.
    reasoning_parameter: ReasoningParameterStyle | None = None
    # Model facts from discovery; the known-model table fills them when absent.
    max_output_tokens: int | None = None
    thinking_off: AnthropicThinkingOff | None = None
    timeout_seconds: float = DEFAULT_CONNECTION_TIMEOUT_SECONDS
    max_attempts: int = DEFAULT_CONNECTION_MAX_ATTEMPTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", ProviderKind.parse(self.provider))
        if not isinstance(self.api_key, SecretText):
            object.__setattr__(self, "api_key", SecretText(self.api_key))
        if not self.deployment_id.strip() or not self.base_url.strip() or not self.model.strip():
            raise ValueError("deployment_id, base_url and model are required")
        if self.reasoning_effort is not None:
            object.__setattr__(self, "reasoning_effort", ReasoningEffort.parse(self.reasoning_effort))
        if self.thinking_off is not None:
            object.__setattr__(self, "thinking_off", AnthropicThinkingOff(self.thinking_off))
        if self.max_output_tokens is not None and self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


def json_object_capability(*, provider: str, deployment_id: str) -> ProviderStructuredOutputCapability:
    """JSON mode with the schema checked locally after decoding.

    Native strict schemas need a per-deployment release record, which user
    connections do not have, so every Chat Completions vendor is asked for plain
    JSON and the contract is enforced by the local validation gate.
    """
    return ProviderStructuredOutputCapability(
        provider=provider,
        deployment=deployment_id,
        mode="json_object",
        supports_stream_terminal_validation=True,
        revision=f"{provider}-json-object-v1",
    )


def build_provider_client(
    settings: ProviderClientSettings,
    *,
    transport: Transport | None = None,
    stream_transport: StreamTransport | None = None,
) -> LLMClient:
    spec = provider_spec(settings.provider)
    retry_policy = LLMRetryPolicy(
        max_attempts=settings.max_attempts,
        retry_delay_seconds=_RETRY_DELAYS_SECONDS,
    )
    if spec.protocol is WireProtocol.OPENAI_CHAT:
        return OpenAICompatibleClient(
            OpenAICompatibleConfig(
                provider=spec.kind.value,
                base_url=settings.base_url,
                model=settings.model,
                api_key=settings.api_key,
                timeout_seconds=settings.timeout_seconds,
                dialect=spec.chat_dialect(
                    settings.model,
                    reasoning_parameter=settings.reasoning_parameter,
                ),
                reasoning_effort=settings.reasoning_effort,
            ),
            transport=transport,
            stream_transport=stream_transport,
            retry_policy=retry_policy,
            structured_output_capability=json_object_capability(
                provider=spec.kind.value,
                deployment_id=settings.deployment_id,
            ),
        )
    if spec.protocol is WireProtocol.ANTHROPIC_MESSAGES:
        return AnthropicMessagesClient(
            AnthropicMessagesConfig(
                provider=spec.kind.value,
                base_url=settings.base_url,
                model=settings.model,
                api_key=settings.api_key,
                timeout_seconds=settings.timeout_seconds,
                options=_anthropic_options(spec, settings),
            ),
            transport=transport,
            stream_transport=stream_transport,
            retry_policy=LLMRetryPolicy(
                max_attempts=settings.max_attempts,
                retry_delay_seconds=_RETRY_DELAYS_SECONDS,
                retryable_status_codes=ANTHROPIC_RETRYABLE_STATUS_CODES,
            ),
            structured_output_capability=json_object_capability(
                provider=spec.kind.value,
                deployment_id=settings.deployment_id,
            ),
        )
    raise ValueError(f"no client is registered for protocol {spec.protocol.value}")


def _anthropic_options(spec: ProviderSpec, settings: ProviderClientSettings) -> AnthropicModelOptions:
    known = spec.known_model(settings.model)
    thinking_off = settings.thinking_off or (known.thinking_off if known is not None else None)
    max_output = settings.max_output_tokens or (known.max_output_tokens if known is not None else None)
    return AnthropicModelOptions(
        reasoning_effort=settings.reasoning_effort,
        thinking_off=thinking_off,
        # Messages requires an explicit cap; never ask for more than the model can emit.
        default_max_output_tokens=min(DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS, max_output)
        if max_output is not None
        else DEFAULT_ANTHROPIC_MAX_OUTPUT_TOKENS,
    )


__all__ = [
    "DEFAULT_CONNECTION_MAX_ATTEMPTS",
    "DEFAULT_CONNECTION_TIMEOUT_SECONDS",
    "ProviderClientSettings",
    "build_provider_client",
    "json_object_capability",
]
