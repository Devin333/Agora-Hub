"""Background execution of admitted desktop Research operations.

Long-running model work (paper analysis) must not hold an HTTP request open:
the submitting request admits the operation, schedules it here and returns,
and the client polls the operation status. Each task runs inside a copy of
the submitting request's ``contextvars`` context, so request-scoped bindings
such as the account's model credential stay in effect off the request thread.

Worker threads are daemons. A task still running when the process exits is
not waited for indefinitely; its admission keeps a lease tied to this process,
so the request store marks it ``needs_review`` once the process is gone.
"""

from __future__ import annotations

import contextvars
import logging
import os
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_ANALYSIS_WORKERS = 2
MAX_ANALYSIS_WORKERS = 32
ANALYSIS_WORKERS_ENV = "NEWSROOM_RESEARCH_ANALYSIS_WORKERS"

BackgroundTask = Callable[[], None]


class ResearchDesktopExecutorUnavailableError(RuntimeError):
    """The executor no longer accepts work because the service is stopping."""

    code = "research_executor_unavailable"


@runtime_checkable
class ResearchDesktopTaskExecutor(Protocol):
    def submit(
        self,
        task: BackgroundTask,
        *,
        on_abandon: BackgroundTask | None = None,
    ) -> None:
        """Schedule ``task``; ``on_abandon`` runs if it is dropped unstarted."""
        ...


@dataclass(frozen=True, slots=True)
class _QueuedTask:
    context: contextvars.Context
    run: BackgroundTask
    on_abandon: BackgroundTask | None


class ResearchDesktopBackgroundExecutor:
    """Bounded worker pool for admitted desktop operations."""

    def __init__(
        self,
        *,
        max_workers: int = DEFAULT_ANALYSIS_WORKERS,
        thread_name_prefix: str = "research-desktop",
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int):
            raise TypeError("max_workers must be an integer")
        if not 1 <= max_workers <= MAX_ANALYSIS_WORKERS:
            raise ValueError(f"max_workers must be between 1 and {MAX_ANALYSIS_WORKERS}")
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._queue: queue.Queue[_QueuedTask | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def max_workers(self) -> int:
        return self._max_workers

    def submit(
        self,
        task: BackgroundTask,
        *,
        on_abandon: BackgroundTask | None = None,
    ) -> None:
        if not callable(task):
            raise TypeError("task must be callable")
        queued = _QueuedTask(
            context=contextvars.copy_context(),
            run=task,
            on_abandon=on_abandon,
        )
        with self._lock:
            if self._closed:
                raise ResearchDesktopExecutorUnavailableError(
                    "Research background executor is shutting down"
                )
            self._ensure_workers()
            self._queue.put(queued)

    def shutdown(self, *, timeout: float | None = None) -> None:
        """Stop accepting work, abandon queued tasks and join the workers.

        Running tasks get up to ``timeout`` seconds to finish; tasks that never
        started are reported through their ``on_abandon`` callback so their
        operations reach a reviewable state instead of waiting forever.
        """

        with self._lock:
            if self._closed:
                return
            self._closed = True
            threads = list(self._threads)
        abandoned: list[_QueuedTask] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                abandoned.append(item)
        for item in abandoned:
            if item.on_abandon is not None:
                self._run_guarded(item.context, item.on_abandon, phase="abandon")
        for _ in threads:
            self._queue.put(None)
        for thread in threads:
            thread.join(timeout)

    def _ensure_workers(self) -> None:
        # Workers start with the first task so composing the service, or a
        # process that never analyzes, does not hold idle threads.
        while len(self._threads) < self._max_workers:
            thread = threading.Thread(
                target=self._work,
                name=f"{self._thread_name_prefix}-{len(self._threads) + 1}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                self._run_guarded(item.context, item.run, phase="run")
            finally:
                self._queue.task_done()

    @staticmethod
    def _run_guarded(
        context: contextvars.Context,
        task: BackgroundTask,
        *,
        phase: str,
    ) -> None:
        try:
            context.run(task)
        except Exception:
            # A task records its own terminal state; anything escaping it is
            # logged so one failure never stops the worker.
            logger.exception("research desktop background task failed during %s", phase)


def analysis_worker_count_from_env() -> int:
    raw = os.getenv(ANALYSIS_WORKERS_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_ANALYSIS_WORKERS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{ANALYSIS_WORKERS_ENV} must be an integer") from exc
    if not 1 <= value <= MAX_ANALYSIS_WORKERS:
        raise ValueError(f"{ANALYSIS_WORKERS_ENV} must be between 1 and {MAX_ANALYSIS_WORKERS}")
    return value


def research_desktop_executor_from_env() -> ResearchDesktopBackgroundExecutor:
    return ResearchDesktopBackgroundExecutor(max_workers=analysis_worker_count_from_env())


_default_executor: ResearchDesktopBackgroundExecutor | None = None
_default_executor_lock = threading.Lock()


def default_research_desktop_executor() -> ResearchDesktopBackgroundExecutor:
    """Process-wide executor for services composed without an explicit one."""

    global _default_executor
    with _default_executor_lock:
        if _default_executor is None:
            _default_executor = research_desktop_executor_from_env()
        return _default_executor


__all__ = [
    "ANALYSIS_WORKERS_ENV",
    "BackgroundTask",
    "DEFAULT_ANALYSIS_WORKERS",
    "ResearchDesktopBackgroundExecutor",
    "ResearchDesktopExecutorUnavailableError",
    "ResearchDesktopTaskExecutor",
    "analysis_worker_count_from_env",
    "default_research_desktop_executor",
    "research_desktop_executor_from_env",
]
