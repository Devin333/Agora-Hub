from __future__ import annotations

import asyncio
import json
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from backend.research.application.paper_answer_model import LLMPaperAnswerModel
from backend.research.ports.paper_answer_model import PaperAnswerModelError, PaperAnswerModelRequest
from backend.research.rag.evaluation.qasper_answer_judge import JudgeRequest, judge_answer
from framework.llm.clients.openai_compatible import (
    LLMProviderError,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from framework.llm.models import LLMRequest


def _config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        provider="diagnostic-provider",
        base_url="https://provider.example/v1",
        model="answer-model",
        api_key_env="TEST_DIAGNOSTIC_API_KEY",
    )


def _request() -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": "answer"}])


def _http_error(status: int, payload: dict) -> HTTPError:
    return HTTPError(
        "https://provider.example/v1/chat/completions",
        status,
        "provider error",
        hdrs=None,
        fp=BytesIO(json.dumps(payload).encode("utf-8")),
    )


def test_http_503_preserves_bounded_redacted_provider_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DIAGNOSTIC_API_KEY", "request-secret")
    secret = "plain-provider-secret"
    long_message = "gateway overloaded " + ("x" * 3000)

    def transport(_request, _timeout):  # type: ignore[no-untyped-def]
        raise _http_error(
            503,
            {
                "error": {
                    "type": "gateway_error",
                    "code": "upstream_busy",
                    "message": long_message,
                    "api_key": secret,
                }
            },
        )

    client = OpenAICompatibleClient(_config(), transport=transport)
    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request())

    error = raised.value
    assert error.error_type == "server_error"
    assert error.retryable is True
    assert error.status_code == 503
    diagnostic = error.to_dict(redact=False)["diagnostics"][0]
    assert diagnostic["stage"] == "http_error_response"
    assert diagnostic["provider_error_type"] == "gateway_error"
    assert diagnostic["provider_error_code"] == "upstream_busy"
    assert diagnostic["provider_message"].startswith("gateway overloaded")
    assert diagnostic["provider_message_truncated"] is True
    assert diagnostic["raw_excerpt_truncated"] is True
    assert len(diagnostic["raw_excerpt"]) <= diagnostic["raw_excerpt_limit"] == 2000
    assert secret not in json.dumps(diagnostic)
    assert "redacted" in diagnostic["raw_excerpt"].lower()


def test_http_200_gateway_payload_retains_error_fields_without_changing_schema_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DIAGNOSTIC_API_KEY", "request-secret")
    body = {
        "error": {
            "type": "gateway_error",
            "code": "gateway_timeout",
            "message": "upstream returned no completion",
        }
    }
    client = OpenAICompatibleClient(
        _config(),
        transport=lambda _request, _timeout: json.dumps(body).encode("utf-8"),
    )

    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request())

    assert raised.value.error_type == "schema_error"
    assert raised.value.retryable is False
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic["provider_error_type"] == "gateway_error"
    assert diagnostic["provider_error_code"] == "gateway_timeout"
    assert diagnostic["provider_message"] == "upstream returned no completion"
    assert "gateway_timeout" in diagnostic["raw_excerpt"]


@pytest.mark.parametrize("status", [503, 200])
def test_structured_error_excerpt_recursively_redacts_text_credentials(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    monkeypatch.setenv("TEST_DIAGNOSTIC_API_KEY", "request-secret")
    secrets = {
        "message": "plain-message-password",
        "detail": "plain-detail-token",
        "list": "plain-list-secret",
        "key": "plain-key-secret",
        "pem": "private-key-material",
    }
    payload = {
        "error": {
            "type": "gateway_error",
            "code": "upstream_busy",
            "message": f"gateway failed password={secrets['message']}",
            "details": {
                "note": f"token={secrets['detail']}",
                "items": [
                    f"client_secret={secrets['list']}",
                    {"api_key": secrets["key"]},
                    (
                        "-----BEGIN PRIVATE KEY-----\n"
                        f"{secrets['pem']}\n"
                        "-----END PRIVATE KEY-----"
                    ),
                ],
            },
        }
    }

    def transport(_request, _timeout):  # type: ignore[no-untyped-def]
        if status == 503:
            raise _http_error(status, payload)
        return json.dumps(payload).encode("utf-8")

    client = OpenAICompatibleClient(_config(), transport=transport)
    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request())

    projected = raised.value.to_dict(redact=True)
    serialized = json.dumps(projected)
    for secret in secrets.values():
        assert secret not in serialized
    diagnostic = projected["diagnostics"][0]
    assert diagnostic["provider_error_type"] == "gateway_error"
    assert diagnostic["provider_error_code"] == "upstream_busy"
    assert diagnostic["provider_message"].startswith("gateway failed")
    assert "redacted" in diagnostic["raw_excerpt"].lower()


def test_malformed_json_retains_redacted_truncated_body_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DIAGNOSTIC_API_KEY", "request-secret")
    credential = "abcdefghijklmnop"
    body = (f"not-json Bearer {credential} " + ("z" * 3000)).encode()
    client = OpenAICompatibleClient(_config(), transport=lambda _request, _timeout: body)

    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request())

    assert raised.value.error_type == "schema_error"
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic["stage"] == "response_json_decode"
    assert diagnostic["raw_excerpt_truncated"] is True
    assert credential not in diagnostic["raw_excerpt"]
    assert "redacted" in diagnostic["raw_excerpt"].lower()


def test_network_reason_is_preserved_as_redacted_summary_without_changing_retry_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DIAGNOSTIC_API_KEY", "request-secret")
    credential = "network-password"

    def transport(_request, _timeout):  # type: ignore[no-untyped-def]
        raise URLError(f"connect failed https://user:{credential}@gateway.example/path")

    client = OpenAICompatibleClient(_config(), transport=transport)
    with pytest.raises(LLMProviderError) as raised:
        client.complete(_request())

    assert raised.value.error_type == "transient_network"
    assert raised.value.retryable is True
    diagnostic = raised.value.diagnostics[0]
    assert diagnostic["stage"] == "network_transport"
    assert diagnostic["exception_type"].endswith("URLError")
    assert credential not in diagnostic["network_reason"]
    assert "redacted" in diagnostic["network_reason"].lower()


def test_answer_model_and_judge_receive_provider_diagnostic_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_DIAGNOSTIC_API_KEY", "request-secret")

    def failing_transport(_request, _timeout):  # type: ignore[no-untyped-def]
        raise _http_error(
            503,
            {
                "error": {
                    "type": "gateway_error",
                    "code": "upstream_busy",
                    "message": "provider unavailable",
                }
            },
        )

    client = OpenAICompatibleClient(_config(), transport=failing_transport)
    answer_model = LLMPaperAnswerModel(client, max_output_tokens=64)
    with pytest.raises(PaperAnswerModelError) as answer_error:
        asyncio.run(
            answer_model.complete(
                PaperAnswerModelRequest(system_prompt="system", user_prompt="user")
            )
        )
    answer_diagnostic = answer_error.value.details["diagnostics"][0]
    assert answer_diagnostic["provider_error_type"] == "gateway_error"
    assert answer_diagnostic["provider_error_code"] == "upstream_busy"

    async def judge_call(_prompt: str) -> str:
        client.complete(_request())
        raise AssertionError("unreachable")

    verdict = asyncio.run(
        judge_answer(
            judge_call,
            JudgeRequest(
                question="Which model?",
                references=("BERT",),
                answer="BERT [1]",
                cited_passages=((1, "The model is BERT."),),
            ),
        )
    )
    assert verdict.judge_error is True
    assert verdict.error_detail["provider_error_type"] == "gateway_error"
    assert verdict.error_detail["provider_error_code"] == "upstream_busy"
    assert verdict.error_detail["provider_message"] == "provider unavailable"
