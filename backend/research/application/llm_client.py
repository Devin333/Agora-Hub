from __future__ import annotations

import asyncio
import hashlib
import os
from urllib.request import Request, urlopen

from framework.llm.clients.openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    LLMRetryPolicy,
)
from backend.research.application.llm_selectors import (
    EVAL_JUDGE_API_KEY_ENV_KEYS,
    EVAL_JUDGE_BASE_URL_KEYS,
    EVAL_JUDGE_MODEL_KEYS,
    RESEARCH_ANSWER_API_KEY_ENV_KEYS,
    RESEARCH_ANSWER_BASE_URL_KEYS,
    RESEARCH_ANSWER_MODEL_KEYS,
    RESEARCH_EVIDENCE_API_KEY_ENV_KEYS,
    RESEARCH_EVIDENCE_BASE_URL_KEYS,
    RESEARCH_EVIDENCE_MODEL_KEYS,
    first_configured,
)
from backend.research.application.paper_answer_model import OpenAICompatiblePaperAnswerModel
from backend.research.rag.retrieval.paper_evidence_summarizer import DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS
from framework.llm.models.request import LLMRequest
from framework.shared.env import load_root_env
from framework.shared.graph_identity import GraphExecutionIdentity

# unity2.ai sits behind Cloudflare, which 403s the default Python-urllib UA.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


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


def _resolve_unity_client(
    role: str,
    *,
    base_url_keys: tuple[str, ...],
    model_keys: tuple[str, ...],
    api_key_env_keys: tuple[str, ...],
) -> OpenAICompatibleClient:
    """Build a client from ordered env selectors; every role must resolve a base URL and model."""
    load_root_env()
    base_url = first_configured(os.environ, base_url_keys)
    model = first_configured(os.environ, model_keys)
    missing = [
        name
        for name, value in (("base URL", base_url), ("model", model))
        if value is None
    ]
    if missing:
        raise ValueError(
            f"{role} is not configured; missing "
            + " and ".join(missing)
            + f" (checked {', '.join(base_url_keys + model_keys)})"
        )
    return _build_unity_client(
        base_url=str(base_url),
        model=str(model),
        api_key_env=first_configured(os.environ, api_key_env_keys) or _DEFAULT_API_KEY_ENV,
    )


def _build_generic_unity_client() -> OpenAICompatibleClient:
    """Auxiliary workers such as the RAG planner: OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL."""
    load_root_env()
    return _build_unity_client(
        base_url=os.environ["OPENAI_BASE_URL"],
        model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        api_key_env=_DEFAULT_API_KEY_ENV,
    )


def build_unity_paper_answer_model(*, max_tokens: int = 600) -> OpenAICompatiblePaperAnswerModel:
    """Paper answer model resolved with the same selectors as the desktop Research route.

    Evaluation must generate with the production answer model, so this reads
    ``NEWS_RESEARCH_LLM_*`` before the generic ``NEWS_LLM_*`` and ``OPENAI_*`` keys.
    """
    client = _resolve_unity_client(
        "paper answer model",
        base_url_keys=RESEARCH_ANSWER_BASE_URL_KEYS,
        model_keys=RESEARCH_ANSWER_MODEL_KEYS,
        api_key_env_keys=RESEARCH_ANSWER_API_KEY_ENV_KEYS,
    )
    return OpenAICompatiblePaperAnswerModel(
        client,
        max_output_tokens=max_tokens,
        temperature=0.0,
    )


def build_unity_evidence_model(
    *, max_tokens: int = DEFAULT_SUMMARY_MAX_OUTPUT_TOKENS,
) -> OpenAICompatiblePaperAnswerModel:
    """Retrieval-side helper model (``NEWS_RESEARCH_EVIDENCE_LLM_*``, else the answer model keys).

    Serves the evidence summarizer and the section navigator; callers size the
    output room for their own reply format.
    """
    client = _resolve_unity_client(
        "evidence summarizer model",
        base_url_keys=RESEARCH_EVIDENCE_BASE_URL_KEYS,
        model_keys=RESEARCH_EVIDENCE_MODEL_KEYS,
        api_key_env_keys=RESEARCH_EVIDENCE_API_KEY_ENV_KEYS,
    )
    return OpenAICompatiblePaperAnswerModel(
        client,
        max_output_tokens=max_tokens,
        temperature=0.0,
    )


def build_unity_judge_llm_call(*, max_tokens: int = 1024, temperature: float | None = 0.0):
    """Prompt-to-text callable for evaluation judges (``NEWS_RAG_EVAL_JUDGE_*``).

    The judge model must be configured explicitly; it never falls back to the generic
    ``OPENAI_MODEL`` or to the answer model it is judging.
    """
    client = _resolve_unity_client(
        "evaluation judge",
        base_url_keys=EVAL_JUDGE_BASE_URL_KEYS,
        model_keys=EVAL_JUDGE_MODEL_KEYS,
        api_key_env_keys=EVAL_JUDGE_API_KEY_ENV_KEYS,
    )
    return _prompt_call(client, max_tokens=max_tokens, temperature=temperature)


def build_unity_llm_call(*, max_tokens: int = 1024, temperature: float | None = None):
    """Async prompt-to-text callable for auxiliary single-prompt workers."""
    return _prompt_call(_build_generic_unity_client(), max_tokens=max_tokens, temperature=temperature)


def _prompt_call(client: OpenAICompatibleClient, *, max_tokens: int, temperature: float | None):
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

    llm_call.measurement_identity = _measurement_identity(  # type: ignore[attr-defined]
        client, max_tokens=max_tokens, temperature=temperature,
    )
    return llm_call


def _measurement_identity(
    client: OpenAICompatibleClient,
    *,
    max_tokens: int,
    temperature: float | None,
) -> dict[str, object]:
    """Return only reproducibility fields; credentials and their env names stay private."""
    config = client.config
    endpoint = str(config.base_url or "").rstrip("/")
    return {
        "provider": str(config.provider or ""),
        "model": str(config.model or ""),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "endpoint_sha256": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
    }


__all__ = [
    "build_unity_evidence_model",
    "build_unity_judge_llm_call",
    "build_unity_llm_call",
    "build_unity_paper_answer_model",
]
