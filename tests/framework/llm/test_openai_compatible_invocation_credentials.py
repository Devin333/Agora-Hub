from __future__ import annotations

import contextvars
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from framework.llm import LLMRequest
from framework.llm.clients import (
    CREDENTIAL_MODE_ENV,
    InvocationLLMClient,
    LLMCredentialMode,
    LLMCredentialUnavailableError,
    LLMEndpoint,
    LLMWireProtocol,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    bind_llm_endpoint_source,
    llm_credential_mode,
    require_invocation_endpoints,
    resolve_invocation_endpoint,
    validate_endpoint_base_url,
)
from framework.llm.models.reasoning import ReasoningEffort, ReasoningSetting, ReasoningWire


def _config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        provider="test-provider",
        base_url="https://operator.example/v1",
        model="operator-model",
        api_key_env="TEST_OPERATOR_API_KEY",
    )


def _body(text: str = "ok") -> bytes:
    return json.dumps(
        {
            "id": "response-1",
            "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    ).encode("utf-8")


class _RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.payloads: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, request, _timeout):
        payload = json.loads(request.data.decode("utf-8"))
        with self._lock:
            self.calls.append(
                (request.full_url, request.get_header("Authorization"), payload["model"])
            )
            self.payloads.append(payload)
        return _body()

    def stream(self, request, _timeout):
        self(request, _timeout)
        yield 'data: {"id":"s1","choices":[{"delta":{"content":"hi"}}]}'
        yield 'data: {"id":"s1","choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield "data: [DONE]"


def _request(model: str | None = None, *, max_tokens: int | None = None) -> LLMRequest:
    return LLMRequest(
        messages=[{"role": "user", "content": "hello"}],
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
    )


def _endpoint(name: str, *, revision: int = 1, **overrides) -> LLMEndpoint:
    values = dict(
        base_url=f"https://{name}.example/v1/",
        model=f"{name}-model",
        api_key=f"sk-{name}-secret",
        provider="test-provider",
        connection_ref=f"{name}-connection@{revision}",
    )
    values.update(overrides)
    return LLMEndpoint(**values)


def _invocation_client(transport: _RecordingTransport, purpose: str = "answer", built=None):
    def factory(endpoint: LLMEndpoint) -> OpenAICompatibleClient:
        if built is not None:
            built.append(endpoint.connection_ref)
        return OpenAICompatibleClient.for_endpoint(
            endpoint,
            timeout_seconds=30,
            transport=transport,
            stream_transport=transport.stream,
        )

    return InvocationLLMClient(purpose, factory)


def _source(**by_purpose: LLMEndpoint):
    def resolve(purpose: str) -> LLMEndpoint:
        try:
            return by_purpose[purpose]
        except KeyError:
            raise LLMCredentialUnavailableError(
                "model_connection_required", "no connection", purpose=purpose
            ) from None

    return resolve


def test_endpoint_client_uses_the_endpoint_and_ignores_the_request_model(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_API_KEY", "sk-operator")
    transport = _RecordingTransport()
    client = _invocation_client(transport)

    with bind_llm_endpoint_source(_source(answer=_endpoint("alice"))):
        client.complete(_request(model="route-model"))

    assert transport.calls == [
        ("https://alice.example/v1/chat/completions", "Bearer sk-alice-secret", "alice-model")
    ]


def test_each_purpose_resolves_its_own_assigned_endpoint():
    transport = _RecordingTransport()
    answer = _invocation_client(transport, "answer")
    summary = _invocation_client(transport, "evidence_summary")

    with bind_llm_endpoint_source(
        _source(answer=_endpoint("alice"), evidence_summary=_endpoint("summarizer"))
    ):
        answer.complete(_request())
        summary.complete(_request())

    assert [call[2] for call in transport.calls] == ["alice-model", "summarizer-model"]


def test_list_models_reads_the_endpoint_model_list_with_its_key():
    seen = []

    def transport(request, _timeout):
        seen.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        return json.dumps(
            {"object": "list", "data": [{"id": "alice-model"}, {"id": "other"}, {"object": "model"}]}
        ).encode("utf-8")

    client = OpenAICompatibleClient.for_endpoint(
        _endpoint("alice"), timeout_seconds=10, transport=transport
    )

    assert client.list_models() == ("alice-model", "other")
    assert seen == [("GET", "https://alice.example/v1/models", "Bearer sk-alice-secret")]


def test_without_binding_or_assignment_the_call_fails_closed(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_API_KEY", "sk-operator")
    transport = _RecordingTransport()
    client = _invocation_client(transport, "section_navigation")

    with pytest.raises(LLMCredentialUnavailableError) as unbound:
        client.complete(_request())
    with bind_llm_endpoint_source(_source(answer=_endpoint("alice"))):
        with pytest.raises(LLMCredentialUnavailableError) as unassigned:
            client.stream(_request())

    assert unbound.value.code == "model_connection_required"
    assert unbound.value.purpose == "section_navigation"
    assert unassigned.value.purpose == "section_navigation"
    assert transport.calls == []


def test_endpoint_is_resolved_once_per_call_and_clients_are_reused_per_revision():
    resolved: list[str] = []
    built: list[str] = []
    transport = _RecordingTransport()
    client = _invocation_client(transport, built=built)
    current = {"endpoint": _endpoint("alice", revision=1)}

    def source(purpose: str) -> LLMEndpoint:
        resolved.append(purpose)
        return current["endpoint"]

    with bind_llm_endpoint_source(source):
        assert resolved == []
        client.complete(_request())
        client.complete(_request())
        current["endpoint"] = _endpoint("alice", revision=2)
        client.complete(_request())
        # Same revision label, rotated key: never reuse the old key's client.
        current["endpoint"] = _endpoint("alice", revision=2, api_key="sk-alice-rotated")
        client.complete(_request())

    assert resolved == ["answer"] * 4
    assert built == ["alice-connection@1", "alice-connection@2", "alice-connection@2"]
    assert transport.calls[-1][1] == "Bearer sk-alice-rotated"


def test_concurrent_accounts_on_one_shared_client_stay_isolated():
    transport = _RecordingTransport()
    client = _invocation_client(transport)
    barrier = threading.Barrier(2)

    def run(name: str) -> None:
        with bind_llm_endpoint_source(_source(answer=_endpoint(name))):
            barrier.wait(timeout=5)
            for _ in range(20):
                client.complete(_request())

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(run, "alice"), pool.submit(run, "bob")]:
            future.result()

    assert len(transport.calls) == 40
    for url, auth, model in transport.calls:
        name = url.removeprefix("https://").split(".", 1)[0]
        assert auth == f"Bearer sk-{name}-secret"
        assert model == f"{name}-model"


def test_stream_binds_endpoint_from_the_calling_context():
    transport = _RecordingTransport()
    client = _invocation_client(transport)

    with bind_llm_endpoint_source(_source(answer=_endpoint("alice"))):
        events = client.stream(_request())
    collected = list(events)

    assert transport.calls[0][1] == "Bearer sk-alice-secret"
    assert collected[0].metadata["model"] == "alice-model"


def test_environment_client_keeps_operator_key_and_request_model(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_API_KEY", "sk-operator")
    transport = _RecordingTransport()
    client = OpenAICompatibleClient(_config(), transport=transport)

    with bind_llm_endpoint_source(_source(answer=_endpoint("alice"))):
        client.complete(_request(model="route-model"))

    assert transport.calls == [
        ("https://operator.example/v1/chat/completions", "Bearer sk-operator", "route-model")
    ]
    assert not client.endpoint_bound


def test_endpoint_reasoning_is_sent_and_widens_the_output_limit():
    transport = _RecordingTransport()
    client = _invocation_client(transport)
    endpoint = _endpoint(
        "alice",
        reasoning=ReasoningSetting(ReasoningWire.DEEPSEEK_THINKING, ReasoningEffort.HIGH),
        max_output_tokens=10_000,
    )

    with bind_llm_endpoint_source(_source(answer=endpoint)):
        client.complete(_request(max_tokens=900))

    payload = transport.payloads[0]
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    # 900 visible + 16384 reasoning allowance, capped by the model ceiling.
    assert payload["max_tokens"] == 10_000


def test_endpoint_for_another_protocol_is_refused():
    with pytest.raises(ValueError):
        OpenAICompatibleClient.for_endpoint(
            _endpoint("alice", protocol=LLMWireProtocol.ANTHROPIC_MESSAGES), timeout_seconds=10
        )


def test_credential_mode_parses_from_environment(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_MODE_ENV, "invocation")
    assert llm_credential_mode() is LLMCredentialMode.INVOCATION
    monkeypatch.delenv(CREDENTIAL_MODE_ENV)
    assert llm_credential_mode() is LLMCredentialMode.ENVIRONMENT
    with pytest.raises(ValueError):
        llm_credential_mode({CREDENTIAL_MODE_ENV: "shared"})


def test_require_invocation_endpoints_checks_every_purpose_only_in_invocation_mode():
    invocation = {CREDENTIAL_MODE_ENV: "invocation"}
    environment = {CREDENTIAL_MODE_ENV: "environment"}

    require_invocation_endpoints(["answer", "evidence_summary"], environment)
    with bind_llm_endpoint_source(_source(answer=_endpoint("alice"))):
        require_invocation_endpoints(["answer"], invocation)
        with pytest.raises(LLMCredentialUnavailableError) as raised:
            require_invocation_endpoints(["answer", "evidence_summary"], invocation)

    assert raised.value.purpose == "evidence_summary"


def test_binding_is_visible_only_in_copied_thread_contexts():
    seen: dict[str, object] = {}

    def probe(key: str) -> None:
        try:
            seen[key] = resolve_invocation_endpoint("answer").model
        except LLMCredentialUnavailableError as exc:
            seen[key] = exc.code

    with bind_llm_endpoint_source(_source(answer=_endpoint("alice"))):
        context = contextvars.copy_context()
        plain = threading.Thread(target=probe, args=("plain",))
        copied = threading.Thread(target=lambda: context.run(probe, "copied"))
        for thread in (plain, copied):
            thread.start()
            thread.join()

    assert seen == {"plain": "model_connection_required", "copied": "alice-model"}


@pytest.mark.parametrize(
    "value",
    [
        "ftp://api.example.com",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?key=1",
        "https://api.example.com/v1#frag",
        "api.example.com/v1",
    ],
)
def test_endpoint_base_url_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        validate_endpoint_base_url(value)


def test_endpoint_base_url_normalizes_http_and_https():
    # Which hosts may be called is the network policy's decision, not the URL shape's.
    assert validate_endpoint_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    assert validate_endpoint_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"


def test_endpoint_rejects_invalid_key_and_hides_it_from_repr():
    with pytest.raises(ValueError):
        LLMEndpoint(base_url="https://a.example", model="m", api_key="has space", provider="p")
    endpoint = _endpoint("alice")
    assert "secret" not in repr(endpoint)
