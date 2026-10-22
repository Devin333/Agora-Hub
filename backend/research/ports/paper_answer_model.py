"""Port for the language model that drafts grounded paper answers.

The answer generator owns the prompt and the abstention protocol; this port only
transports one request to a model and returns its raw text plus usage. Every way the
model can fail to return text is a typed ``PaperAnswerModelError``; callers never
receive an empty or fabricated response in place of a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from framework.llm.models.usage import TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity

PaperAnswerModelErrorCode = Literal[
    "provider_unavailable",
    "timeout",
    "configuration",
    "empty_output",
    "malformed_output",
]
_ERROR_CODES: frozenset[str] = frozenset(PaperAnswerModelErrorCode.__args__)  # type: ignore[attr-defined]


class PaperAnswerModelError(RuntimeError):
    """The answer model did not produce a usable response."""

    def __init__(
        self,
        code: PaperAnswerModelErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        if code not in _ERROR_CODES:
            raise ValueError(f"unknown paper answer model error code: {code!r}")
        super().__init__(message)
        self.code: PaperAnswerModelErrorCode = code
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class PaperAnswerModelRequest:
    system_prompt: str
    user_prompt: str
    execution_identity: GraphExecutionIdentity | None = None
    # None defers to the adapter's configured transport retry policy.
    max_transport_attempts: int | None = None

    def __post_init__(self) -> None:
        if not str(self.system_prompt).strip():
            raise ValueError("system_prompt is required")
        if not str(self.user_prompt).strip():
            raise ValueError("user_prompt is required")
        if self.max_transport_attempts is not None and (
            isinstance(self.max_transport_attempts, bool)
            or not isinstance(self.max_transport_attempts, int)
            or self.max_transport_attempts < 1
        ):
            raise ValueError("max_transport_attempts must be a positive integer")


@dataclass(frozen=True)
class PaperAnswerModelResponse:
    text: str
    model: str
    usage: TokenUsage = field(default_factory=TokenUsage)


@runtime_checkable
class PaperAnswerModelPort(Protocol):
    async def complete(self, request: PaperAnswerModelRequest) -> PaperAnswerModelResponse:
        """Return the model's raw text or raise ``PaperAnswerModelError``."""
        ...


__all__ = [
    "PaperAnswerModelError",
    "PaperAnswerModelErrorCode",
    "PaperAnswerModelPort",
    "PaperAnswerModelRequest",
    "PaperAnswerModelResponse",
]
