from __future__ import annotations

import asyncio
import os
from urllib.request import Request, urlopen

from framework.llm.clients.openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    LLMRetryPolicy,
)
from backend.research.application.llm_selectors import (
    RESEARCH_ANSWER_API_KEY_ENV_KEYS,
    RESEARCH_ANSWER_BASE_URL_KEYS,
    RESEARCH_ANSWER_MODEL_KEYS,
    first_configured,
)
from backend.research.application.paper_answer_model import OpenAICompatiblePaperAnswerModel
from framework.llm.models.request import LLMRequest
from framework.shared.env import load_root_env
from framework.shared.graph_identity import GraphExecutionIdentity

# unity2.ai sits behind Cloudflare, which 403s the default Python-urllib UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _browser_ua_transport(request: Request, timeout_seconds: float) -> bytes:
    request.add_header("User-Agent", _BROWSER_UA)
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def _build_unity_client(*, base_url: str, model: str, api_key_env: str) -> OpenAICompatibleClient:
    """Client for an OpenAI-compatible gateway behind Cloudflare (browser User-Agent transport)."""
    config = OpenAICompatibleConfig(
        provider="openai-compatible",
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
    )
    return OpenAICompatibleClient(
        config,
        transport=_browser_ua_transport,
        retry_policy=LLMRetryPolicy(max_attempts=3, retry_delay_seconds=(1.5, 3.0)),
    )


def _build_generic_unity_client() -> OpenAICompatibleClient:
    """Judges and auxiliary workers: OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL."""
    load_root_env()
    return _build_unity_client(
        base_url=os.environ["OPENAI_BASE_URL"],
        model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        api_key_env="OPENAI_API_KEY",
    )


def build_unity_paper_answer_model(*, max_tokens: int = 600) -> OpenAICompatiblePaperAnswerModel:
    """Paper answer model resolved with the same selectors as the desktop Research route.

    Evaluation must generate with the production answer model, so this reads
    ``NEWS_RESEARCH_LLM_*`` before the generic ``NEWS_LLM_*`` and ``OPENAI_*`` keys.
    """
    load_root_env()
    base_url = first_configured(os.environ, RESEARCH_ANSWER_BASE_URL_KEYS)
    model = first_configured(os.environ, RESEARCH_ANSWER_MODEL_KEYS)
    missing = [
        name
        for name, value in (("base URL", base_url), ("model", model))
        if value is None
    ]
    if missing:
        raise ValueError(
            "paper answer model is not configured; missing "
            + " and ".join(missing)
            + f" (checked {', '.join(RESEARCH_ANSWER_BASE_URL_KEYS + RESEARCH_ANSWER_MODEL_KEYS)})"
        )
    client = _build_unity_client(
        base_url=str(base_url),
        model=str(model),
        api_key_env=first_configured(os.environ, RESEARCH_ANSWER_API_KEY_ENV_KEYS) or "OPENAI_API_KEY",
    )
    return OpenAICompatiblePaperAnswerModel(
        client,
        max_output_tokens=max_tokens,
        temperature=0.0,
    )


def build_unity_llm_call(*, max_tokens: int = 1024, temperature: float | None = None):
    """Async prompt-to-text callable for judges and other single-prompt workers."""
    client = _build_generic_unity_client()

    async def llm_call(
        prompt: str,
        *,
        execution_identity: GraphExecutionIdentity | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
    ) -> str:
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            execution_identity=execution_identity,
            timeout_seconds=timeout_seconds,
            max_transport_attempts=max_transport_attempts,
        )
        response = await asyncio.to_thread(client.complete, request)
        return response.content or ""

    return llm_call


__all__ = ["build_unity_llm_call", "build_unity_paper_answer_model"]
