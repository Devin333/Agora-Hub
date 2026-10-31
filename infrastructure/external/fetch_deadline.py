"""One wall-clock budget for a whole logical fetch.

A fetch is more than one socket operation: robots.txt, the request itself,
each redirect hop and every retry all spend time. Per-socket timeouts only
bound a single recv, so a slow-dripping or repeatedly failing upstream can
keep a fetch alive far past its configured ``timeout_seconds``. When that
outlives the tool's declared timeout, the executor abandons the attempt and
its thread keeps holding an execution slot.

``fetch_deadline`` opens a scope; nested scopes can only shorten it. Network
code asks ``bounded_timeout`` for the timeout of each operation and checks
``remaining_seconds`` before starting another one.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class FetchDeadlineExceeded(TimeoutError):
    """The fetch used its whole budget; retrying cannot help."""

    def __init__(self, budget_seconds: float | None) -> None:
        self.budget_seconds = budget_seconds
        if budget_seconds is None:
            super().__init__("fetch deadline exceeded")
        else:
            super().__init__(f"fetch exceeded its {budget_seconds:g}s time budget")


@dataclass(frozen=True)
class _Deadline:
    expires_at: float
    budget_seconds: float


_CURRENT: ContextVar[_Deadline | None] = ContextVar("fetch_deadline", default=None)


@contextmanager
def fetch_deadline(budget_seconds: float) -> Iterator[None]:
    budget = float(budget_seconds)
    if not budget > 0:
        raise ValueError("fetch budget must be positive")
    proposed = _Deadline(expires_at=time.monotonic() + budget, budget_seconds=budget)
    current = _CURRENT.get()
    effective = current if current is not None and current.expires_at <= proposed.expires_at else proposed
    token = _CURRENT.set(effective)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def deadline_expires_at() -> float | None:
    current = _CURRENT.get()
    return current.expires_at if current is not None else None


def deadline_budget_seconds() -> float | None:
    current = _CURRENT.get()
    return current.budget_seconds if current is not None else None


def remaining_seconds() -> float | None:
    current = _CURRENT.get()
    if current is None:
        return None
    return current.expires_at - time.monotonic()


def ensure_time_remaining() -> None:
    remaining = remaining_seconds()
    if remaining is not None and remaining <= 0:
        raise FetchDeadlineExceeded(deadline_budget_seconds())


def bounded_timeout(timeout_seconds: float | None) -> float | None:
    """The socket timeout for the next operation: never past the deadline."""

    remaining = remaining_seconds()
    if remaining is None:
        return timeout_seconds
    if remaining <= 0:
        raise FetchDeadlineExceeded(deadline_budget_seconds())
    return remaining if timeout_seconds is None else min(float(timeout_seconds), remaining)


def deadline_passed() -> bool:
    remaining = remaining_seconds()
    return remaining is not None and remaining <= 0


# Socket timeouts are set to the remaining budget, so a timeout that fires at
# the budget may observe a clock a tick short of it (~15ms on Windows).
_BUDGET_TIMEOUT_TOLERANCE_SECONDS = 0.05


def spent_by_timeout(error: BaseException, expires_at: float | None) -> bool:
    """True when ``error`` is a timeout that fired because the budget ran out."""

    if expires_at is None or not isinstance(error, TimeoutError):
        return False
    return time.monotonic() >= expires_at - _BUDGET_TIMEOUT_TOLERANCE_SECONDS
