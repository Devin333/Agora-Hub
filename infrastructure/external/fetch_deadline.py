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

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


class FetchDeadlineExceeded(TimeoutError):
    """The fetch used its whole budget; retrying cannot help.

    ``before_send`` is True when the budget ran out before a connection was
    established, so the request was never sent.
    """

    def __init__(self, budget_seconds: float | None, *, before_send: bool = False) -> None:
        self.budget_seconds = budget_seconds
        self.before_send = before_send
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


class SocketWatchdog:
    """Shut a socket down when the budget ends.

    Socket timeouts bound one recv; the watchdog bounds the whole exchange,
    including slow-dripping replies. Shutting the raw descriptor wakes any
    thread blocked on it, plain or TLS. ``watch`` again after a TLS upgrade
    so the watchdog follows the socket that is actually in use.
    """

    def __init__(self, expires_at: float, budget_seconds: float | None) -> None:
        self.expires_at = expires_at
        self.budget_seconds = budget_seconds
        self.fired = False
        self._cancelled = False
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @classmethod
    def for_current_deadline(cls) -> "SocketWatchdog | None":
        current = _CURRENT.get()
        if current is None:
            return None
        return cls(current.expires_at, current.budget_seconds)

    def watch(self, sock: socket.socket) -> None:
        with self._lock:
            self._sock = sock
            if self._timer is not None or self._cancelled:
                return
            delay = max(0.0, self.expires_at - time.monotonic())
            self._timer = threading.Timer(delay, self._fire)
            self._timer.daemon = True
            self._timer.name = "egress-deadline-watchdog"
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def raise_if_spent(self, error: BaseException | None = None) -> None:
        """Raise ``FetchDeadlineExceeded`` when the budget ended this exchange.

        Either the watchdog fired, or ``error`` is the socket timeout (set to
        the remaining budget) that fired just before it.
        """

        if self.fired or (error is not None and spent_by_timeout(error, self.expires_at)):
            raise FetchDeadlineExceeded(self.budget_seconds) from error

    def _fire(self) -> None:
        with self._lock:
            if self._cancelled:
                return
            self.fired = True
            sock = self._sock
        if sock is None:
            return
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            pass
