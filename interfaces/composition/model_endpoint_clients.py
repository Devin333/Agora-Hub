"""Provider clients built for one account endpoint.

Every model client that serves a signed-in account is built here from the
endpoint the account assigned to a purpose, so the wire protocol, timeouts and
retry policy are chosen in one place for every consumer.
"""

from __future__ import annotations

from dataclasses import dataclass

from framework.llm.clients.endpoint import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.invocation import InvocationLLMClient
from framework.llm.clients.openai_compatible import LLMRetryPolicy, OpenAICompatibleClient
from framework.llm.models import LLMClient


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

    def __init__(self, policy: EndpointClientPolicy) -> None:
        self._policy = policy

    def __call__(self, endpoint: LLMEndpoint) -> LLMClient:
        retry_policy = LLMRetryPolicy(max_attempts=self._policy.max_attempts)
        if endpoint.protocol is LLMWireProtocol.OPENAI_CHAT:
            return OpenAICompatibleClient.for_endpoint(
                endpoint,
                timeout_seconds=self._policy.timeout_seconds,
                retry_policy=retry_policy,
            )
        raise ValueError(f"no client speaks the {endpoint.protocol.value} protocol")


def invocation_client(purpose: str, policy: EndpointClientPolicy) -> InvocationLLMClient:
    """Client for one purpose that serves whichever account is bound to the call."""
    return InvocationLLMClient(purpose, EndpointClientFactory(policy))


__all__ = ["EndpointClientFactory", "EndpointClientPolicy", "invocation_client"]
