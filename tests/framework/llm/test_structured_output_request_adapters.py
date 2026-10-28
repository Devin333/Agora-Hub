from __future__ import annotations

import json

import pytest

from framework.llm import (
    LLMProviderError,
    LLMRequest,
    LLMRetryPolicy,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    OpenAICompatibleRequestNormalizer,
    ProviderStructuredOutputCapability,
    ProviderStructuredOutputPolicy,
    compile_structured_output_contract,
    project_structured_output_contract,
)
from framework.llm.clients import (
    LLMCredentialMode,
    LLMEndpoint,
    bind_llm_endpoint_source,
)
from framework.llm.context.openai import build_openai_chat_payload
from framework.llm.structured_output.request_adapters import (
    JSON_OBJECT_SCHEMA_INSTRUCTION,
    structured_output_request_adapter,
    supports_structured_output_projection,
)

_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {"answer": {"type": "string", "minLength": 1}},
    "additionalProperties": False,
}
_LOCAL_GATE = ProviderStructuredOutputPolicy(allow_json_object_local_gate=True)


def _json_object_capability(provider: str = "test-provider") -> ProviderStructuredOutputCapability:
    return ProviderStructuredOutputCapability(
        provider=provider,
        deployment=f"{provider}/model",
        mode="json_object",
        revision=f"{provider}-json-object-v1",
    )


def _config(provider: str = "test-provider") -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        provider=provider,
        base_url="https://operator.example/v1",
        model="operator-model",
        api_key_env="TEST_ADAPTER_API_KEY",
    )


def _request(**changes) -> LLMRequest:
    values = {
        "messages": [
            {"role": "system", "content": "You are a worker."},
            {"role": "user", "content": "Summarize."},
        ],
        "output_schema": _SCHEMA,
        "output_schema_name": "answer",
        "structured_output_policy": _LOCAL_GATE,
    }
    values.update(changes)
    return LLMRequest(**values)


def _projected(request: LLMRequest, capability: ProviderStructuredOutputCapability) -> LLMRequest:
    contract = compile_structured_output_contract(_SCHEMA, schema_name="answer")
    projection = project_structured_output_contract(
        contract, capability, policy=request.structured_output_policy
    )
    return request.with_structured_output_execution(contract=contract, projection=projection)


def _body(content: str) -> bytes:
    return json.dumps(
        {
            "id": "r1",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    ).encode("utf-8")


class _Transport:
    def __init__(self, *contents: str) -> None:
        self._contents = list(contents)
        self.payloads: list[dict] = []

    def __call__(self, request, _timeout):
        self.payloads.append(json.loads(request.data.decode("utf-8")))
        return _body(self._contents.pop(0))


def test_local_gate_sends_json_object_with_the_contract_schema_as_instruction() -> None:
    request = _projected(_request(), _json_object_capability())

    payload = build_openai_chat_payload(request, model="m")

    assert payload["response_format"] == {"type": "json_object"}
    roles = [message["role"] for message in payload["messages"]]
    assert roles == ["system", "system", "user"]
    assert payload["messages"][0]["content"] == "You are a worker."
    instruction = payload["messages"][1]["content"]
    assert instruction.startswith(JSON_OBJECT_SCHEMA_INSTRUCTION)
    # The provider rejects json_object requests unless a message says JSON.
    assert "json" in instruction.lower()
    schema = json.loads(instruction.removeprefix(JSON_OBJECT_SCHEMA_INSTRUCTION))
    assert schema == compile_structured_output_contract(_SCHEMA).canonical_schema
    # The caller's request is never mutated.
    assert len(request.messages) == 2


def test_local_gate_instruction_leads_when_there_is_no_system_prompt() -> None:
    request = _projected(
        _request(messages=[{"role": "user", "content": "Summarize."}]),
        _json_object_capability(),
    )

    messages = build_openai_chat_payload(request, model="m")["messages"]

    assert [message["role"] for message in messages] == ["system", "user"]


def test_preflight_normalizer_counts_the_same_payload_that_is_sent() -> None:
    request = _projected(_request(), _json_object_capability())

    normalized = OpenAICompatibleRequestNormalizer().normalize(
        request, provider="test-provider", model="m"
    )

    assert normalized.payload == build_openai_chat_payload(request, model="m")


def test_constrained_mode_has_no_adapter_and_is_refused() -> None:
    assert structured_output_request_adapter("constrained") is None
    assert structured_output_request_adapter("native_strict") is not None
    projection = _projected(_request(), _json_object_capability()).provider_schema_projection()
    assert supports_structured_output_projection(projection)


def test_local_gate_reply_is_validated_against_the_contract() -> None:
    transport = _Transport('{"answer": ""}')
    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        structured_output_capability=_json_object_capability(),
        credential_mode=LLMCredentialMode.ENVIRONMENT,
    )

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("TEST_ADAPTER_API_KEY", "sk-operator")
        with pytest.raises(LLMProviderError) as raised:
            client.complete(_request())

    assert raised.value.error_type == "structured_output_validation_error"


def test_empty_local_gate_reply_is_retried_then_accepted(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ADAPTER_API_KEY", "sk-operator")
    transport = _Transport("", '{"answer": "ok"}')
    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        retry_policy=LLMRetryPolicy(max_attempts=2),
        structured_output_capability=_json_object_capability(),
        sleep=lambda _seconds: None,
        credential_mode=LLMCredentialMode.ENVIRONMENT,
    )

    response = client.complete(_request())

    assert response.structured_output == {"answer": "ok"}
    assert len(transport.payloads) == 2
    validation = response.metadata["structured_output_validation"]
    assert validation["validated"] is True
    assert validation["projection_mode"] == "json_object_local_gate"


def test_empty_reply_is_not_retried_past_the_attempt_budget(monkeypatch) -> None:
    monkeypatch.setenv("TEST_ADAPTER_API_KEY", "sk-operator")
    transport = _Transport("")
    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        retry_policy=LLMRetryPolicy(max_attempts=3),
        structured_output_capability=_json_object_capability(),
        credential_mode=LLMCredentialMode.ENVIRONMENT,
    )

    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request(max_transport_attempts=1))

    assert raised.value.error_type == "structured_output_empty"
    assert len(transport.payloads) == 1


def test_invocation_uses_the_account_endpoint_capability_not_the_operator_one() -> None:
    transport = _Transport('{"answer": "ok"}')
    operator_capability = ProviderStructuredOutputCapability(
        provider="test-provider",
        deployment="operator",
        mode="none",
        revision="operator-none-v1",
    )
    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        structured_output_capability=operator_capability,
        credential_mode=LLMCredentialMode.INVOCATION,
    )
    endpoint = LLMEndpoint(
        base_url="https://dashscope.example/v1",
        model="deepseek-v4.1-flash",
        api_key="sk-alice-secret",
        provider="dashscope",
        structured_output_capability=_json_object_capability("dashscope"),
    )

    with bind_llm_endpoint_source(lambda: endpoint):
        response = client.complete(_request())

    assert response.structured_output == {"answer": "ok"}
    assert transport.payloads[0]["response_format"] == {"type": "json_object"}
    assert response.metadata["provider"] == "dashscope"
    assert client.config == _config()


def test_invocation_endpoint_without_capability_refuses_structured_output() -> None:
    transport = _Transport('{"answer": "ok"}')
    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        structured_output_capability=_json_object_capability(),
        credential_mode=LLMCredentialMode.INVOCATION,
    )
    endpoint = LLMEndpoint(base_url="https://a.example/v1", model="m", api_key="sk-alice-secret")

    with bind_llm_endpoint_source(lambda: endpoint):
        with pytest.raises(LLMProviderError) as raised:
            client.complete(_request())

    assert raised.value.error_type == "provider_schema_ineligible"
    assert transport.payloads == []


def test_endpoint_capability_must_belong_to_its_provider() -> None:
    with pytest.raises(ValueError):
        LLMEndpoint(
            base_url="https://a.example/v1",
            model="m",
            api_key="sk-alice-secret",
            provider="deepseek",
            structured_output_capability=_json_object_capability("dashscope"),
        )
    with pytest.raises(ValueError):
        LLMEndpoint(
            base_url="https://a.example/v1",
            model="m",
            api_key="sk-alice-secret",
            structured_output_capability=_json_object_capability("dashscope"),
        )


def test_invocation_refuses_a_projection_made_for_the_operator_deployment() -> None:
    transport = _Transport('{"answer": "ok"}')
    client = OpenAICompatibleClient(
        _config("dashscope"),
        transport=transport,
        credential_mode=LLMCredentialMode.INVOCATION,
    )
    operator = ProviderStructuredOutputCapability(
        provider="dashscope",
        deployment="operator",
        mode="json_object",
        revision="operator-json-object-v1",
    )
    endpoint = LLMEndpoint(
        base_url="https://dashscope.example/v1",
        model="deepseek-v4.1-flash",
        api_key="sk-alice-secret",
        provider="dashscope",
        structured_output_capability=_json_object_capability("dashscope"),
    )

    with bind_llm_endpoint_source(lambda: endpoint):
        with pytest.raises(LLMProviderError) as raised:
            client.complete(_projected(_request(), operator))

    assert raised.value.diagnostics[0]["validator"] == "provider_projection_endpoint_mismatch"
    assert transport.payloads == []
