"""Provider clients built for one account endpoint.

Every model client that serves a signed-in account is built here from the
endpoint the account assigned to a purpose, so the wire protocol, timeouts,
retry policy and network policy are chosen in one place for every consumer.

Account endpoints carry user-supplied base URLs, so every client built here
sends through the pinned transports: public HTTPS only unless the operator
allowlisted the host in ``NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS``, connections
pinned to the checked addresses, redirects never followed.
"""

from __future__ import annotations

from dataclasses import dataclass

from framework.llm.clients.anthropic_messages import (
    ANTHROPIC_RETRYABLE_STATUS_CODES,
    AnthropicMessagesClient,
)
from framework.llm.clients.endpoint import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.invocation import InvocationLLMClient
from framework.llm.clients.openai_compatible import LLMRetryPolicy, OpenAICompatibleClient
from framework.llm.models import LLMClient
from infrastructure.security.outbound_address import OutboundAddressPolicy
from infrastructure.security.pinned_https import PinnedConnector, PinnedUrllibTransport
from infrastructure.security.pinned_httpx import pinned_httpx_client


@dataclass(frozen=True, slots=True)
class EndpointClientPolicy:
    timeout_seconds: float
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


class EndpointClientFactory:
    """Build the provider client that speaks an endpoint's wire protocol."""

    def __init__(
        self,
        policy: EndpointClientPolicy,
        *,
        outbound: OutboundAddressPolicy | None = None,
    ) -> None:
        self._policy = policy
        self._connector = PinnedConnector(
            outbound if outbound is not None else OutboundAddressPolicy.from_environment()
        )

    @property
    def outbound(self) -> OutboundAddressPolicy:
        return self._connector.policy

    def __call__(self, endpoint: LLMEndpoint) -> LLMClient:
        if endpoint.protocol is LLMWireProtocol.OPENAI_CHAT:
            transport = PinnedUrllibTransport(self._connector)
            return OpenAICompatibleClient.for_endpoint(
                endpoint,
                timeout_seconds=self._policy.timeout_seconds,
                transport=transport,
                stream_transport=transport.stream,
                retry_policy=LLMRetryPolicy(max_attempts=self._policy.max_attempts),
            )
        if endpoint.protocol is LLMWireProtocol.ANTHROPIC_MESSAGES:
            return AnthropicMessagesClient.for_endpoint(
                endpoint,
                timeout_seconds=self._policy.timeout_seconds,
                http_client=pinned_httpx_client(self.outbound, connector=self._connector),
                retry_policy=LLMRetryPolicy(
                    max_attempts=self._policy.max_attempts,
                    retryable_status_codes=ANTHROPIC_RETRYABLE_STATUS_CODES,
                ),
            )
        raise ValueError(f"no client speaks the {endpoint.protocol.value} protocol")


def invocation_client(
    purpose: str,
    policy: EndpointClientPolicy,
    *,
    outbound: OutboundAddressPolicy | None = None,
) -> InvocationLLMClient:
    """Client for one purpose that serves whichever account is bound to the call."""
    return InvocationLLMClient(purpose, EndpointClientFactory(policy, outbound=outbound))


__all__ = ["EndpointClientFactory", "EndpointClientPolicy", "invocation_client"]
