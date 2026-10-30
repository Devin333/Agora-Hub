from __future__ import annotations

import pytest

from framework.llm.clients import LLMEndpoint, LLMWireProtocol
from framework.llm.clients.anthropic_messages import AnthropicMessagesClient
from framework.llm.clients.openai_compatible import OpenAICompatibleClient
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
