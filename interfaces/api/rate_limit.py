from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable


Clock = Callable[[], float]
_SWEEP_INTERVAL = 1024


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class InMemoryRateLimiter:
    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int = 60,
        clock: Clock | None = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock or time.monotonic
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._checks_since_sweep = 0

    def check(self, key: str) -> RateLimitDecision:
        now = self._clock()
        cutoff = now - self.window_seconds
        self._sweep_idle_keys(cutoff)
        requests = self._requests[key]
        while requests and requests[0] <= cutoff:
            requests.popleft()

        if len(requests) >= self.limit:
            retry_after = max(1, math.ceil(self.window_seconds - (now - requests[0])))
            return RateLimitDecision(
                allowed=False,
                limit=self.limit,
                remaining=0,
                retry_after_seconds=retry_after,
            )

        requests.append(now)
        return RateLimitDecision(
            allowed=True,
            limit=self.limit,
            remaining=max(0, self.limit - len(requests)),
            retry_after_seconds=0,
        )

    def _sweep_idle_keys(self, cutoff: float) -> None:
        # Keys are client addresses or accounts; without a sweep, every address
        # that ever called once would stay in memory for the process lifetime.
        self._checks_since_sweep += 1
        if self._checks_since_sweep < _SWEEP_INTERVAL:
            return
        self._checks_since_sweep = 0
        for key in [key for key, stamps in self._requests.items() if not stamps or stamps[-1] <= cutoff]:
            del self._requests[key]
