from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread

_LOGGER = logging.getLogger(__name__)


class BackgroundWarmup:
    """Run an optional cache warm-up next to the server instead of before it.

    A startup hook delays the listening socket until it returns, so a warm-up
    there (loading model weights, opening vector store collections) makes the
    whole API unavailable for its duration. Requests that need the warmed
    resource synchronise on that resource's own lock, so serving them while the
    warm-up is still running is safe; they just pay the remaining cold cost.

    The thread is a daemon: a warm-up never holds the process open at shutdown,
    and its failure is logged rather than raised because the resource is
    initialised again on first use.
    """

    def __init__(self, name: str, task: Callable[[], None]) -> None:
        self._name = name
        self._task = task
        self._done = Event()
        self._thread: Thread | None = None

    @property
    def started(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = Thread(target=self._run, name=f"api-warmup-{self._name}", daemon=True)
        self._thread.start()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the warm-up finished; ``True`` when it did within ``timeout``."""

        if self._thread is None:
            return True
        return self._done.wait(timeout)

    def _run(self) -> None:
        try:
            self._task()
        except Exception:
            _LOGGER.warning("%s warm-up skipped", self._name, exc_info=True)
        finally:
            self._done.set()


__all__ = ["BackgroundWarmup"]
