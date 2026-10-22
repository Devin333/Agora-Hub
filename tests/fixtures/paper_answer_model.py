"""Deterministic fake for ``PaperAnswerModelPort`` used across Research tests."""

from __future__ import annotations

from collections.abc import Callable
from typing import Union

from backend.research.ports.paper_answer_model import (
    PaperAnswerModelError,
    PaperAnswerModelRequest,
    PaperAnswerModelResponse,
)
from framework.llm.models.usage import TokenUsage

Reply = Union[str, PaperAnswerModelError, Callable[[PaperAnswerModelRequest], str]]


class FakePaperAnswerModel:
    """Returns scripted replies in order; the last reply repeats once exhausted.

    A reply may be raw text, a ``PaperAnswerModelError`` to raise, or a callable
    that builds text from the request (to assert on, or echo, prompt contents).
    """

    def __init__(self, *replies: Reply, model: str = "fake-paper-answer-model") -> None:
        if not replies:
            raise ValueError("FakePaperAnswerModel needs at least one reply")
        self._replies = list(replies)
        self.model = model
        self.requests: list[PaperAnswerModelRequest] = []

    async def complete(self, request: PaperAnswerModelRequest) -> PaperAnswerModelResponse:
        self.requests.append(request)
        reply = self._replies[min(len(self.requests) - 1, len(self._replies) - 1)]
        if isinstance(reply, PaperAnswerModelError):
            raise reply
        text = reply(request) if callable(reply) else reply
        return PaperAnswerModelResponse(
            text=text,
            model=self.model,
            usage=TokenUsage(input_tokens=len(request.user_prompt) // 4, output_tokens=len(text) // 4),
        )

    @property
    def last_request(self) -> PaperAnswerModelRequest:
        return self.requests[-1]


__all__ = ["FakePaperAnswerModel"]
