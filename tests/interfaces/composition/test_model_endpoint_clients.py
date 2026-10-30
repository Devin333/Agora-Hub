from __future__ import annotations

import pytest

from framework.llm.clients import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.anthropic_messages import AnthropicMessagesClient
from framework.llm.clients.openai_compatible import OpenAICompatibleClient
from framework.llm.models import LLMRequest
from interfaces.composition.model_endpoint_clients import (
    EndpointClientFactory,
    EndpointClientPolicy,
    invocation_client,
)


def _endpoint(protocol: LLMWireProtocol) -> LLMEndpoint:
    return LLMEndpoint(
        base_url="https://api.example.com",
        model="model-1",
        api_key="sk-account-1",
        provider="example",
        protocol=protocol,
    )


def test_factory_builds_the_client_that_speaks_the_endpoint_protocol():
    factory = EndpointClientFactory(EndpointClientPolicy(timeout_seconds=10, max_attempts=2))

    openai = factory(_endpoint(LLMWireProtocol.OPENAI_CHAT))
    anthropic = factory(_endpoint(LLMWireProtocol.ANTHROPIC_MESSAGES))

    assert isinstance(openai, OpenAICompatibleClient)
    assert openai.endpoint_bound
    assert isinstance(anthropic, AnthropicMessagesClient)
    assert anthropic.model == "model-1"
    anthropic.close()


def test_invocation_client_keeps_its_purpose():
    client = invocation_client("evidence_summary", EndpointClientPolicy(timeout_seconds=5))

    assert client.purpose == "evidence_summary"


@pytest.mark.parametrize(("timeout", "attempts"), [(0, 1), (5, 0)])
def test_policy_rejects_unusable_limits(timeout, attempts):
    with pytest.raises(ValueError):
        EndpointClientPolicy(timeout_seconds=timeout, max_attempts=attempts)


def test_factory_clients_send_through_the_outbound_policy():
    from framework.llm.clients.openai_compatible import LLMProviderError
    from infrastructure.security.outbound_address import OutboundAddressPolicy

    # The endpoint's host resolves only to a private address; nothing is sent.
    outbound = OutboundAddressPolicy(resolver=lambda _host, _port: ["10.0.0.3"])
    factory = EndpointClientFactory(EndpointClientPolicy(timeout_seconds=5, max_attempts=3), outbound=outbound)
    request = LLMRequest(messages=[{"role": "user", "content": "hi"}])

    for protocol in LLMWireProtocol:
        client = factory(_endpoint(protocol))
        with pytest.raises(LLMProviderError) as raised:
            client.complete(request)
        assert raised.value.error_type == "endpoint_not_allowed"
        assert raised.value.retryable is False
        close = getattr(client, "close", None)
        if callable(close):
            close()


def test_factory_reads_the_allowlist_from_the_environment(monkeypatch):
    monkeypatch.setenv("NEWSROOM_LLM_ALLOWED_PRIVATE_HOSTS", "host.docker.internal")

    factory = EndpointClientFactory(EndpointClientPolicy(timeout_seconds=5))

    assert factory.outbound.allowed_hosts == frozenset({"host.docker.internal"})
