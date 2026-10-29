"""Blocking HTTP transport seams and retry policy shared by provider clients."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Callable
from urllib.request import Request, urlopen


Transport = Callable[[Request, float], bytes]
StreamTransport = Callable[[Request, float], Iterable[bytes | str]]
Sleep = Callable[[float], None]


@dataclass(frozen=True)
class LLMRetryPolicy:
    max_attempts: int = 1
    retry_delay_seconds: tuple[float, ...] = (0.0,)
    retryable_status_codes: tuple[int, ...] = (408, 409, 429, 500, 502, 503, 504)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        object.__setattr__(self, "retry_delay_seconds", tuple(self.retry_delay_seconds))
        object.__setattr__(self, "retryable_status_codes", tuple(self.retryable_status_codes))
        if any(delay < 0 for delay in self.retry_delay_seconds):
            raise ValueError("retry delays must be non-negative")

    def delay_after_attempt(self, attempt: int) -> float:
        if not self.retry_delay_seconds:
            return 0.0
        index = min(max(attempt - 1, 0), len(self.retry_delay_seconds) - 1)
        return self.retry_delay_seconds[index]


def urlopen_transport(request: Request, timeout_seconds: float) -> bytes:
    with urlopen(request, timeout=timeout_seconds) as response:
        return response.read()


def urlopen_stream_transport(request: Request, timeout_seconds: float) -> Iterable[bytes]:
    with urlopen(request, timeout=timeout_seconds) as response:
        yield from response


__all__ = [
    "LLMRetryPolicy",
    "Sleep",
    "StreamTransport",
    "Transport",
    "urlopen_stream_transport",
    "urlopen_transport",
]
