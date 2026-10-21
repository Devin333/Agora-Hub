from __future__ import annotations

import json

import pytest

from framework.llm.clients.fake import FakeLLMClient
from framework.llm.models import LLMRequest
from infrastructure.research.llm_replay_client import (
    FilesystemLLMReplayClient,
    LLMReplayMissError,
    replay_key,
)


def _request(text: str = "hello") -> LLMRequest:
    return LLMRequest(messages=[{"role": "user", "content": text}], temperature=0, max_tokens=10)


def test_record_then_replay_never_calls_the_provider_twice(tmp_path) -> None:
    provider = FakeLLMClient(["first answer"])
    recorder = FilesystemLLMReplayClient(provider, provider="p", model="m", directory=tmp_path, mode="record")
    first = recorder.complete(_request())
    assert first.content == "first answer" and first.metadata["llm_replay"] == "recorded"
    again = recorder.complete(_request())
    assert again.content == "first answer" and again.metadata["llm_replay"] == "hit"
    assert provider.call_count == 1

    replayer = FilesystemLLMReplayClient(None, provider="p", model="m", directory=tmp_path, mode="replay")
    assert replayer.complete(_request()).content == "first answer"
    stored = next(tmp_path.rglob("*.json"))
    assert "api_key" not in stored.read_text(encoding="utf-8")
    assert json.loads(stored.read_text(encoding="utf-8"))["format"] == "llm-replay/v1"


def test_replay_miss_is_an_error_not_a_provider_call(tmp_path) -> None:
    replayer = FilesystemLLMReplayClient(None, provider="p", model="m", directory=tmp_path, mode="replay")
    with pytest.raises(LLMReplayMissError):
        replayer.complete(_request())


def test_key_depends_on_everything_that_determines_the_output() -> None:
    base = replay_key(provider="p", model="m", request=_request())
    assert base != replay_key(provider="p", model="m", request=_request("other"))
    assert base != replay_key(provider="p", model="m2", request=_request())
    assert base != replay_key(provider="p", model="m", request=LLMRequest(
        messages=[{"role": "user", "content": "hello"}], temperature=0.7, max_tokens=10,
    ))


def test_record_mode_requires_a_provider_and_absolute_directory(tmp_path) -> None:
    with pytest.raises(ValueError, match="provider client"):
        FilesystemLLMReplayClient(None, provider="p", model="m", directory=tmp_path, mode="record")
    with pytest.raises(ValueError, match="absolute"):
        FilesystemLLMReplayClient(FakeLLMClient([]), provider="p", model="m",
                                  directory=tmp_path.relative_to(tmp_path.anchor), mode="record")
