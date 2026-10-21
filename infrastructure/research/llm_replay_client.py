"""File-backed record/replay for evaluation LLM calls.

Evaluation must be reproducible: the same request must yield the same response
on a rerun, and a CI replay must never reach a provider. Each request is keyed
by a digest of everything that determines the output (deployment identity,
messages, schema, sampling parameters); only the response content, structured
output, model name, and token usage are stored, never credentials.

Modes:
* ``record``: replay a stored response when present, otherwise call and store;
* ``replay``: replay only; a missing response is an error, not a provider call.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from framework.llm.models import LLMClient, LLMRequest, LLMResponse
from framework.llm.models.usage import TokenUsage

ReplayMode = Literal["record", "replay"]
_REPLAY_FORMAT = "llm-replay/v1"


class LLMReplayMissError(RuntimeError):
    """A replay-only run met a request that was never recorded."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def replay_key(*, provider: str, model: str, request: LLMRequest) -> str:
    payload = {
        "format": _REPLAY_FORMAT,
        "provider": provider,
        "model": model,
        "messages": request.messages,
        "output_schema": request.output_schema,
        "output_schema_name": request.output_schema_name if request.output_schema is not None else None,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "response_format": request.response_format,
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class FilesystemLLMReplayClient:
    def __init__(
        self,
        client: LLMClient | None,
        *,
        provider: str,
        model: str,
        directory: Path,
        mode: ReplayMode,
    ) -> None:
        if mode == "record" and client is None:
            raise ValueError("record mode requires a provider client")
        if not directory.is_absolute():
            raise ValueError("replay directory must be absolute")
        self._client = client
        self._provider = provider
        self._model = model
        self._directory = directory
        self._mode = mode

    def _path(self, key: str) -> Path:
        return self._directory / self._provider / key[:2] / f"{key}.json"

    def complete(self, request: LLMRequest) -> LLMResponse:
        key = replay_key(provider=self._provider, model=self._model, request=request)
        path = self._path(key)
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if stored.get("format") != _REPLAY_FORMAT or stored.get("key") != key:
                raise ValueError(f"replay entry {path.name} is not a valid {_REPLAY_FORMAT} record")
            return LLMResponse(
                content=stored["content"],
                structured_output=stored["structured_output"],
                model=stored["model"],
                usage=TokenUsage(**stored["usage"]),
                metadata={"llm_replay": "hit", "llm_replay_key": key},
            )
        if self._mode == "replay" or self._client is None:
            raise LLMReplayMissError(f"no recorded response for request {key}")
        response = LLMResponse.from_any(self._client.complete(request))
        record = {
            "format": _REPLAY_FORMAT,
            "key": key,
            "provider": self._provider,
            "model": response.model or self._model,
            "content": response.content,
            "structured_output": response.structured_output,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "reasoning_tokens": response.usage.reasoning_tokens,
                "cached_input_tokens": response.usage.cached_input_tokens,
                "estimated_cost_usd": response.usage.estimated_cost_usd,
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crashed run never leaves a truncated record.
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(_canonical(record))
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
        return LLMResponse(
            content=response.content,
            structured_output=response.structured_output,
            model=response.model,
            usage=response.usage,
            metadata={**response.metadata, "llm_replay": "recorded", "llm_replay_key": key},
        )

    def stream(self, request: LLMRequest):  # pragma: no cover - evaluation never streams
        raise NotImplementedError("evaluation replay does not support streaming")


__all__ = [
    "FilesystemLLMReplayClient",
    "LLMReplayMissError",
    "ReplayMode",
    "replay_key",
]
