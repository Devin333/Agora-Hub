"""Deterministic stand-in for the desktop background executor in tests."""

from __future__ import annotations

import contextvars
from collections.abc import Callable


class ManualDesktopExecutor:
    """Queues tasks with their submitting context; tests run them explicitly."""

    def __init__(self) -> None:
        self._pending: list[tuple[contextvars.Context, Callable[[], None], Callable[[], None] | None]] = []

    @property
    def pending(self) -> int:
        return len(self._pending)

    def submit(
        self,
        task: Callable[[], None],
        *,
        on_abandon: Callable[[], None] | None = None,
    ) -> None:
        self._pending.append((contextvars.copy_context(), task, on_abandon))

    def drain(self) -> int:
        """Run every queued task in submission order; returns how many ran."""

        ran = 0
        while self._pending:
            context, task, _ = self._pending.pop(0)
            context.run(task)
            ran += 1
        return ran

    def abandon(self) -> int:
        """Drop queued tasks the way a stopping executor does."""

        dropped = 0
        while self._pending:
            context, _, on_abandon = self._pending.pop(0)
            if on_abandon is not None:
                context.run(on_abandon)
            dropped += 1
        return dropped
