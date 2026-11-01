"""Detect threads and attempt capacity a tool run failed to give back.

A tool that times out is abandoned by the supervisor, but its worker thread
keeps running and keeps one slot of the shared attempt capacity until the
handler returns. A handler with no internal timeout therefore leaks a slot
for as long as its upstream stalls; enough of them and every tool call is
refused. The guard waits a short grace period for stragglers to finish once
their fault sources are released, then fails with the names of what remains.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from framework.shared.attempts import DEFAULT_ATTEMPT_EXECUTION_CAPACITY

DEFAULT_GRACE_SECONDS = 2.0
_POLL_SECONDS = 0.02


@dataclass(frozen=True)
class ResourceSnapshot:
    threads: frozenset[threading.Thread]
    attempt_capacity_active: int


def snapshot_resources() -> ResourceSnapshot:
    return ResourceSnapshot(
        threads=frozenset(threading.enumerate()),
        attempt_capacity_active=DEFAULT_ATTEMPT_EXECUTION_CAPACITY.active,
    )


def leaked_resources(baseline: ResourceSnapshot) -> list[str]:
    leaks = [
        f"thread {thread.name!r} is still alive"
        for thread in threading.enumerate()
        if thread not in baseline.threads and thread.is_alive()
    ]
    active = DEFAULT_ATTEMPT_EXECUTION_CAPACITY.active
    if active != baseline.attempt_capacity_active:
        leaks.append(
            "attempt capacity holds "
            f"{active - baseline.attempt_capacity_active} slot(s) more than before the test"
        )
    return leaks


def assert_no_leaks(baseline: ResourceSnapshot, *, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
    deadline = time.monotonic() + grace_seconds
    while True:
        leaks = leaked_resources(baseline)
        if not leaks or time.monotonic() >= deadline:
            break
        time.sleep(_POLL_SECONDS)
    assert not leaks, (
        f"resources still held {grace_seconds:.1f}s after the test: " + "; ".join(leaks)
    )
