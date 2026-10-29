"""Exclusive lock files guarding read-modify-write cycles on local JSON stores."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class LocalJsonLockTimeoutError(TimeoutError):
    """Raised when a local JSON store lock cannot be acquired in time."""


class LocalJsonFileLock:
    """Cross-process mutual exclusion via an ``O_EXCL`` sidecar lock file.

    A thread lock serialises callers inside one process; the sidecar file
    serialises processes sharing the same store path. A lock file older than
    ``stale_after_seconds`` is treated as abandoned by a crashed holder.
    """

    def __init__(
        self,
        target: str | Path,
        *,
        timeout_seconds: float = 10.0,
        stale_after_seconds: float = 60.0,
        poll_interval_seconds: float = 0.01,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        target_path = Path(target)
        self.lock_path = target_path.with_name(f"{target_path.name}.lock")
        self.timeout_seconds = timeout_seconds
        self.stale_after_seconds = stale_after_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._thread_lock = threading.RLock()
        self._depth = 0

    @contextmanager
    def hold(self) -> Iterator[None]:
        with self._thread_lock:
            if self._depth == 0:
                self._acquire_file()
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._release_file()

    def _acquire_file(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                )
            except FileExistsError:
                if self._break_stale_lock():
                    continue
                if time.monotonic() >= deadline:
                    raise LocalJsonLockTimeoutError(
                        f"timed out acquiring local JSON lock: {self.lock_path}"
                    ) from None
                time.sleep(self.poll_interval_seconds)
                continue
            try:
                os.write(descriptor, str(os.getpid()).encode("ascii"))
            finally:
                os.close(descriptor)
            return

    def _break_stale_lock(self) -> bool:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return True
        if age < self.stale_after_seconds:
            return False
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
        return True

    def _release_file(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass
