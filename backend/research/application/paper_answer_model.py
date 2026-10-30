"""Paper answer model port served by any framework ``LLMClient``."""

from __future__ import annotations

import asyncio
import hashlib

from backend.research.ports.paper_answer_model import (
    PaperAnswerModelError,
    PaperAnswerModelRequest,
    PaperAnswerModelResponse,
)
from framework.llm.clients.endpoint import LLMCredentialUnavailableError
from framework.llm.clients.openai_compatible import (
    LLMConfigurationError,
    LLMProviderError,
)
from framework.llm.models.client import LLMClient
from framework.llm.models.request import LLMRequest
from framework.llm.models.response import LLMResponse

# Provider failures caused by our own request or credentials, not by availability.
# ``schema_error`` is deliberately absent: answer requests send no tools, so it only
# reports a malformed provider response (invalid JSON, missing choices), which is an
# availability fault of the provider or gateway.
_CONFIGURATION_ERROR_TYPES = frozenset({
    "auth_error",
    "invalid_request",
    "unsupported_model",
    "context_length",
})


class LLMPaperAnswerModel:
    """Sends one paper-answer prompt and returns raw text with token usage."""

    def __init__(
        self,
        client: LLMClient,
        *,
        max_output_tokens: int,
        temperature: float | None = 0.0,
    ) -> None:
        if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens < 1:
            raise ValueError("max_output_tokens must be a positive integer")
        self._client = client
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature

    @property
    def measurement_identity(self) -> dict[str, object]:
        """Stable non-secret identity for reproducible answer evaluations.

        Read when asked, so a client whose endpoint is chosen per call reports
        the endpoint bound to the current call rather than one fixed at startup.
        """
        return {
            **client_endpoint_identity(self._client),
            "max_tokens": self._max_output_tokens,
            "temperature": self._temperature,
        }

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
            response = LLMResponse.from_any(raw)
        except LLMCredentialUnavailableError:
            # The account has no usable connection for this purpose: an
            # account-level outcome the interface reports, not a model fault.
            raise
        except LLMConfigurationError as exc:
            raise PaperAnswerModelError(
                "configuration",
                str(exc),
                details=_unexpected_error_details(exc, error_type="configuration_error"),
            ) from exc
        except LLMProviderError as exc:
            raise _model_error_from_provider(exc) from exc
        except TimeoutError as exc:
            raise PaperAnswerModelError(
                "timeout",
                "paper answer model timed out",
                retryable=True,
                details=_unexpected_error_details(exc, error_type="timeout", retryable=True),
            ) from exc
        except Exception as exc:
            raise PaperAnswerModelError(
                "provider_unavailable",
                str(exc) or "unexpected paper answer model failure",
                details=_unexpected_error_details(exc),
            ) from exc
        text = str(response.content or "")
        if not text.strip():
            raise PaperAnswerModelError("empty_output", "paper answer model returned no text")
        return PaperAnswerModelResponse(
            text=text,
            model=str(response.model or client_endpoint_identity(self._client).get("model") or ""),
            usage=response.usage,
        )


def client_endpoint_identity(client: object) -> dict[str, object]:
    """Provider, model and hashed endpoint of ``client``; empty when none is bound."""
    resolve = getattr(client, "endpoint_identity", None)
    if callable(resolve):
        try:
            identity = resolve()
        except LLMCredentialUnavailableError:
            return {"purpose": str(getattr(client, "purpose", "") or "")}
        provider, model, base_url = identity.provider, identity.model, identity.base_url
    else:
        config = getattr(client, "config", None)
        if config is None:
            return {}
        provider, model, base_url = config.provider, config.model, config.base_url
    endpoint = str(base_url or "").rstrip("/")
    return {
        "provider": str(provider or ""),
        "model": str(model or ""),
        "endpoint_sha256": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
    }


def _model_error_from_provider(exc: LLMProviderError) -> PaperAnswerModelError:
    error_type = str(exc.error_type or "")
    details = exc.to_dict(redact=True)
    error_category = str(details.get("error_category") or error_type)
    if error_category == "timeout":
        return PaperAnswerModelError("timeout", str(exc), retryable=bool(exc.retryable), details=details)
    if error_category in _CONFIGURATION_ERROR_TYPES:
        return PaperAnswerModelError(
            "configuration", str(exc), retryable=bool(exc.retryable), details=details,
        )
    return PaperAnswerModelError(
        "provider_unavailable", str(exc), retryable=bool(exc.retryable), details=details,
    )


def _unexpected_error_details(
    exc: Exception,
    *,
    error_type: str | None = None,
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "error_type": error_type or type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
        "retryable": bool(retryable),
    }


__all__ = ["LLMPaperAnswerModel", "client_endpoint_identity"]
