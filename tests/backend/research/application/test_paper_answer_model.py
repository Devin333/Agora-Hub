from __future__ import annotations

import asyncio
import json

import pytest

from backend.research.application.paper_answer_model import OpenAICompatiblePaperAnswerModel
from backend.research.ports.paper_answer_model import PaperAnswerModelError, PaperAnswerModelRequest
from framework.llm.clients.openai_compatible import (
    LLMProviderError,
    OpenAICompatibleConfig,
)


class _Client:
    def __init__(self, result: object) -> None:
        self.config = OpenAICompatibleConfig(
            provider="test-provider",
            base_url="https://provider.test/v1",
            model="test-model",
            api_key_env="TEST_PROVIDER_API_KEY",
        )
        self._result = result

    def complete(self, request):
        del request
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


@pytest.mark.parametrize(
    ("error_type", "expected_code"),
    [
        ("invalid_api_key", "configuration"),
        ("invalid_model", "configuration"),
        ("schema_error", "provider_unavailable"),
        ("server_error", "provider_unavailable"),
    ],
)
def test_provider_errors_preserve_redacted_diagnostics(
    error_type: str,
    expected_code: str,
) -> None:
    provider_error = LLMProviderError(
        "provider failed with api_key=super-secret-value",
        provider="test-provider",
        model="test-model",
        error_type=error_type,
        retryable=error_type == "server_error",
        status_code=503,
        attempts=2,
    )
    model = OpenAICompatiblePaperAnswerModel(_Client(provider_error), max_output_tokens=64)

    with pytest.raises(PaperAnswerModelError) as raised:
        asyncio.run(model.complete(PaperAnswerModelRequest(system_prompt="system", user_prompt="user")))

    error = raised.value
    assert error.code == expected_code
    assert error.retryable is (error_type == "server_error")
    assert error.details["error_type"] == error_type
    assert error.details["status_code"] == 503
    assert error.details["attempts"] == 2
    serialized = json.dumps({"message": str(error), "details": error.details})
    assert "super-secret-value" not in serialized
    assert "redacted" in serialized.lower()


def test_unexpected_client_error_keeps_sanitized_type_and_message() -> None:
    model = OpenAICompatiblePaperAnswerModel(
        _Client(RuntimeError("unexpected failure; Bearer abcdefghijklmnop")),
        max_output_tokens=64,
    )

    with pytest.raises(PaperAnswerModelError) as raised:
        asyncio.run(model.complete(PaperAnswerModelRequest(system_prompt="system", user_prompt="user")))

    error = raised.value
    assert error.code == "provider_unavailable"
    assert error.retryable is False
    assert error.details == {
        "error_type": "RuntimeError",
        "message": "unexpected failure; [redacted]",
        "message_truncated": False,
        "message_limit": 2048,
        "retryable": False,
    }
    assert "abcdefghijklmnop" not in str(error)


def test_error_records_message_truncation_without_caller_metadata() -> None:
    error = PaperAnswerModelError(
        "provider_unavailable",
        "m" * 3000,
        details={"output_excerpt": "x" * 3000},
    )

    assert len(str(error)) == 2048
    assert error.details["message_truncated"] is True
    assert error.details["message_limit"] == 2048
    assert len(error.details["output_excerpt"]) == 2048
    assert error.details["output_excerpt_truncated"] is True
    assert error.details["output_excerpt_limit"] == 2048
