"""OpenAI-compatible implementation of the paper answer model port."""

from __future__ import annotations

import asyncio

from backend.research.ports.paper_answer_model import (
    PaperAnswerModelError,
    PaperAnswerModelRequest,
    PaperAnswerModelResponse,
)
from framework.llm.clients.openai_compatible import (
    LLMConfigurationError,
    LLMProviderError,
    OpenAICompatibleClient,
)
from framework.llm.models.request import LLMRequest
from framework.llm.models.response import LLMResponse

# Provider failures caused by our own request or credentials, not by availability.
_CONFIGURATION_ERROR_TYPES = frozenset({
    "auth_error",
    "invalid_request",
    "unsupported_model",
    "context_length",
    "schema_error",
})


class OpenAICompatiblePaperAnswerModel:
    """Sends one paper-answer prompt and returns raw text with token usage."""

    def __init__(
        self,
        client: OpenAICompatibleClient,
        *,
        max_output_tokens: int,
        temperature: float | None = 0.0,
    ) -> None:
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        self._client = client
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature

    async def complete(self, request: PaperAnswerModelRequest) -> PaperAnswerModelResponse:
        llm_request = LLMRequest(
            messages=[
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            max_tokens=self._max_output_tokens,
            temperature=self._temperature,
            metadata={"component": "paper_answer_model"},
            execution_identity=request.execution_identity,
            max_transport_attempts=request.max_transport_attempts,
        )
        try:
            raw = await asyncio.to_thread(self._client.complete, llm_request)
        except LLMConfigurationError as exc:
            raise PaperAnswerModelError("configuration", str(exc)) from exc
        except LLMProviderError as exc:
            raise _model_error_from_provider(exc) from exc
        except TimeoutError as exc:
            raise PaperAnswerModelError("timeout", "paper answer model timed out", retryable=True) from exc
        response = LLMResponse.from_any(raw)
        text = str(response.content or "")
        if not text.strip():
            raise PaperAnswerModelError("empty_output", "paper answer model returned no text")
        return PaperAnswerModelResponse(
            text=text,
            model=str(response.model or self._client.config.model),
            usage=response.usage,
        )


def _model_error_from_provider(exc: LLMProviderError) -> PaperAnswerModelError:
    error_type = str(exc.error_type or "")
    if error_type == "timeout":
        return PaperAnswerModelError("timeout", str(exc), retryable=bool(exc.retryable))
    if error_type in _CONFIGURATION_ERROR_TYPES:
        return PaperAnswerModelError("configuration", str(exc))
    return PaperAnswerModelError("provider_unavailable", str(exc), retryable=bool(exc.retryable))


__all__ = ["OpenAICompatiblePaperAnswerModel"]
