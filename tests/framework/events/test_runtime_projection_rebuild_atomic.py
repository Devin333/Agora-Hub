from __future__ import annotations

from datetime import UTC, datetime, timedelta
import threading

import pytest

from framework.events.runtime.projection import (
    RuntimeEventEnvelope,
    RuntimeEventIdentityConflict,
    RuntimeEventProjection,
)


NOW = datetime(2026, 9, 27, 1, 2, 3, tzinfo=UTC)


class _ObservableRLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.contended = threading.Event()

    def __enter__(self):
        if not self._lock.acquire(blocking=False):
            self.contended.set()
            self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._lock.release()


def test_failed_rebuild_preserves_published_runtime_projection() -> None:
    published = RuntimeEventEnvelope(
        event_id="runtime-1",
        event_type="worker_status",
        occurred_at=NOW,
        status="running",
        sequence=1,
        stream_id="runtime:run-1",
    )
    projection = RuntimeEventProjection()
    projection.apply(published)
    original_status = projection.status()
    original_cursor = projection.cursor("runtime:run-1")
    conflict = RuntimeEventEnvelope(
        event_id="runtime-1",
        event_type="worker_status",
        occurred_at=NOW + timedelta(seconds=1),
        status="failed",
        sequence=2,
        stream_id="runtime:run-1",
    )

    with pytest.raises(RuntimeEventIdentityConflict):
        projection.rebuild((published, conflict))

    assert projection.status() == original_status
    assert projection.cursor("runtime:run-1") == original_cursor


def test_rebuild_serializes_live_apply_without_losing_delivery() -> None:
    projection = RuntimeEventProjection()
    observable_lock = _ObservableRLock()
    projection._lock = observable_lock
    fold_started = threading.Event()
    release_fold = threading.Event()
    historical = RuntimeEventEnvelope(
        event_id="runtime-history-1",
        event_type="worker_status",
        occurred_at=NOW,
        status="running",
        sequence=1,
        stream_id="runtime:run-1",
    )
    live = RuntimeEventEnvelope(
        event_id="runtime-live-2",
        event_type="worker_status",
        occurred_at=NOW + timedelta(seconds=1),
        status="succeeded",
        sequence=2,
        stream_id="runtime:run-1",
    )

    def blocked_history():
        fold_started.set()
        assert release_fold.wait(timeout=5)
        yield historical

    rebuild_thread = threading.Thread(
        target=projection.rebuild,
        args=(blocked_history(),),
    )
    rebuild_thread.start()
    assert fold_started.wait(timeout=5)

    apply_thread = threading.Thread(target=projection.apply, args=(live,))
    apply_thread.start()
    assert observable_lock.contended.wait(timeout=5)
    release_fold.set()
    rebuild_thread.join(timeout=5)
    apply_thread.join(timeout=5)

    assert not rebuild_thread.is_alive()
    assert not apply_thread.is_alive()
    assert projection.cursor("runtime:run-1").sequence == 2
    assert projection.status()[0].last_event_id == "runtime-live-2"
    assert projection.status()[0].status == "succeeded"
