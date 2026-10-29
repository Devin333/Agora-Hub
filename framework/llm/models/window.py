"""Per-call token windows for clients whose model is chosen at call time."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMCallWindow:
    """What one call may send and generate on the model it will reach."""

    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    def capped(self, *, max_input_tokens: int | None = None, max_output_tokens: int | None = None) -> LLMCallWindow:
        """Narrow the window to a caller's own limits; never widens it."""
        return LLMCallWindow(
            max_input_tokens=min(self.max_input_tokens, max_input_tokens or self.max_input_tokens),
            max_output_tokens=min(self.max_output_tokens, max_output_tokens or self.max_output_tokens),
        )


@runtime_checkable
class WindowedLLMClient(Protocol):
    """A client that can say, before a call, which window that call will get.

    Callers that size prompts (context assembly, candidate projection) ask for
    the window right before building the request instead of fixing it at
    construction time.
    """

    def call_window(self, *, requested_output_tokens: int) -> LLMCallWindow: ...


__all__ = ["LLMCallWindow", "WindowedLLMClient"]
