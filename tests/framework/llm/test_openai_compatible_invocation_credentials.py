from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from framework.llm import LLMRequest
from framework.llm.clients import (
    CREDENTIAL_MODE_ENV,
    LLMCredentialMode,
    LLMCredentialUnavailableError,
    LLMEndpoint,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    bind_llm_endpoint_source,
    llm_credential_mode,
    validate_endpoint_base_url,
)


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
        self._lock = threading.Lock()

    def __call__(self, request, _timeout):
        payload = json.loads(request.data.decode("utf-8"))
        with self._lock:
            self.calls.append(
                (request.full_url, request.get_header("Authorization"), payload["model"])
            )
        return _body()

    def stream(self, request, _timeout):
        self(request, _timeout)
        yield 'data: {"id":"s1","choices":[{"delta":{"content":"hi"}}]}'
        yield 'data: {"id":"s1","choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield "data: [DONE]"


def _request(model: str | None = None) -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "hello"}], model=model)


def _endpoint(name: str) -> LLMEndpoint:
    return LLMEndpoint(
        base_url=f"https://{name}.example/v1/",
        model=f"{name}-model",
        api_key=f"sk-{name}-secret",
    )


def test_invocation_mode_uses_bound_endpoint_and_ignores_request_model(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_API_KEY", "sk-operator")
    transport = _RecordingTransport()
    client = OpenAICompatibleClient(
        _config(), transport=transport, credential_mode=LLMCredentialMode.INVOCATION
    )

    with bind_llm_endpoint_source(lambda: _endpoint("alice")):
        client.complete(_request(model="route-model"))

    assert transport.calls == [
        ("https://alice.example/v1/chat/completions", "Bearer sk-alice-secret", "alice-model")
    ]
    assert client.config == _config()


def test_invocation_mode_without_binding_fails_closed_and_never_reads_env(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_API_KEY", "sk-operator")
    transport = _RecordingTransport()
    client = OpenAICompatibleClient(
        _config(), transport=transport, credential_mode=LLMCredentialMode.INVOCATION
    )

    with pytest.raises(LLMCredentialUnavailableError) as raised:
        client.complete(_request())
    with pytest.raises(LLMCredentialUnavailableError):
        client.stream(_request())

    assert raised.value.code == "model_credential_required"
    assert transport.calls == []


def test_endpoint_source_is_resolved_lazily_once_per_call():
    resolved: list[str] = []
    transport = _RecordingTransport()
    client = OpenAICompatibleClient(
        _config(), transport=transport, credential_mode=LLMCredentialMode.INVOCATION
    )

    def source() -> LLMEndpoint:
        resolved.append("alice")
        return _endpoint("alice")

    with bind_llm_endpoint_source(source):
        assert resolved == []
        client.complete(_request())
        client.complete(_request())

    assert resolved == ["alice", "alice"]


def test_concurrent_bindings_on_one_shared_client_stay_isolated():
    transport = _RecordingTransport()
    client = OpenAICompatibleClient(
        _config(), transport=transport, credential_mode=LLMCredentialMode.INVOCATION
    )
    barrier = threading.Barrier(2)

    def run(name: str) -> None:
        with bind_llm_endpoint_source(lambda: _endpoint(name)):
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
    client = OpenAICompatibleClient(
        _config(),
        transport=transport,
        stream_transport=transport.stream,
        credential_mode=LLMCredentialMode.INVOCATION,
    )

    with bind_llm_endpoint_source(lambda: _endpoint("alice")):
        events = client.stream(_request())
    collected = list(events)

    assert transport.calls[0][1] == "Bearer sk-alice-secret"
    assert collected[0].metadata["model"] == "alice-model"


def test_environment_mode_keeps_operator_key_and_request_model(monkeypatch):
    monkeypatch.setenv("TEST_OPERATOR_API_KEY", "sk-operator")
    transport = _RecordingTransport()
    client = OpenAICompatibleClient(
        _config(), transport=transport, credential_mode=LLMCredentialMode.ENVIRONMENT
    )

    with bind_llm_endpoint_source(lambda: _endpoint("alice")):
        client.complete(_request(model="route-model"))

    assert transport.calls == [
        ("https://operator.example/v1/chat/completions", "Bearer sk-operator", "route-model")
    ]


def test_credential_mode_defaults_from_environment(monkeypatch):
    monkeypatch.setenv(CREDENTIAL_MODE_ENV, "invocation")
    assert OpenAICompatibleClient(_config()).credential_mode is LLMCredentialMode.INVOCATION
    monkeypatch.delenv(CREDENTIAL_MODE_ENV)
    assert llm_credential_mode() is LLMCredentialMode.ENVIRONMENT
    with pytest.raises(ValueError):
        llm_credential_mode({CREDENTIAL_MODE_ENV: "shared"})


def test_binding_is_visible_only_in_copied_thread_contexts():
    import contextvars

    from framework.llm.clients.endpoint import resolve_invocation_endpoint

    seen: dict[str, object] = {}

    def probe(key: str) -> None:
        try:
            seen[key] = resolve_invocation_endpoint().model
        except LLMCredentialUnavailableError as exc:
            seen[key] = exc.code

    with bind_llm_endpoint_source(lambda: _endpoint("alice")):
        context = contextvars.copy_context()
        plain = threading.Thread(target=probe, args=("plain",))
        copied = threading.Thread(target=lambda: context.run(probe, "copied"))
        for thread in (plain, copied):
            thread.start()
            thread.join()

    assert seen == {"plain": "model_credential_required", "copied": "alice-model"}


@pytest.mark.parametrize(
    "value",
    [
        "http://api.example.com/v1",
        "ftp://api.example.com",
        "https://user:pass@api.example.com/v1",
        "https://api.example.com/v1?key=1",
        "https://api.example.com/v1#frag",
        "api.example.com/v1",
    ],
)
def test_endpoint_base_url_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        validate_endpoint_base_url(value)


def test_endpoint_base_url_accepts_https_and_loopback_http():
    assert validate_endpoint_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    assert validate_endpoint_base_url("http://127.0.0.1:8000/v1") == "http://127.0.0.1:8000/v1"


def test_endpoint_rejects_invalid_key_and_hides_it_from_repr():
    with pytest.raises(ValueError):
        LLMEndpoint(base_url="https://a.example", model="m", api_key="has space")
    endpoint = _endpoint("alice")
    assert "secret" not in repr(endpoint)
