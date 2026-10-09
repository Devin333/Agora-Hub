from __future__ import annotations

import asyncio
import os
from urllib.request import Request, urlopen

from framework.llm.clients.openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
    LLMRetryPolicy,
)
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


def build_unity_llm_call(*, max_tokens: int = 1024, temperature: float | None = None):
    """Async LLM callable backed by the OPENAI_*-configured endpoint (unity2.ai).

    Uses a browser User-Agent transport to pass Cloudflare. Reads:
      OPENAI_BASE_URL, OPENAI_API_KEY, OPENAI_MODEL.
    """
    load_root_env()
    config = OpenAICompatibleConfig(
        provider="openai-compatible",
        base_url=os.environ["OPENAI_BASE_URL"],
        model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        api_key_env="OPENAI_API_KEY",
    )
    client = OpenAICompatibleClient(
        config,
        transport=_browser_ua_transport,
        retry_policy=LLMRetryPolicy(max_attempts=3, retry_delay_seconds=(1.5, 3.0)),
    )

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


__all__ = ["build_unity_llm_call"]
