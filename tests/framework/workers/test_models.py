from __future__ import annotations

from datetime import UTC, datetime

from framework.workers import (
    DeadLetterRecord,
    Task,
    TaskResult,
    TaskRetryPolicy,
    TaskStatus,
    WorkerMetrics,
    WorkerStatus,
)
from framework.events.canonical import thaw_canonical_json
from framework.events.runtime.models import StreamReadRequest
from framework.events.runtime.projection import CanonicalRuntimeEventPublisher
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import default_event_schema_catalog
from infrastructure.storage.events.sqlite import SQLiteEventStore
from framework.workers.runtime.heartbeat import InMemoryWorkerHeartbeatStore, WorkerHeartbeat


def test_task_prd_aliases_round_trip() -> None:
    run_at = datetime(2026, 1, 1, tzinfo=UTC)
    task = Task(
        task_type="demo",
        payload={"x": 1},
        queue_name="q",
        scheduled_for=run_at,
        execution_scope="standalone",
    )

    assert task.queue == "q"
    assert task.run_at == run_at
    payload = task.to_dict()
    restored = Task.from_dict({**payload, "queue_name": None})

    assert restored.queue == "q"
    assert restored.run_at == run_at


def test_status_result_retry_metrics_and_dead_letter_helpers() -> None:
    task = Task(task_type="demo", payload={}, execution_scope="standalone")
    assert TaskStatus.SUCCEEDED.is_terminal()
    assert WorkerStatus.IDLE.value == "idle"
    assert TaskRetryPolicy(max_attempts=2).should_retry(1, "temporary")
    assert WorkerMetrics().record_success().succeeded_count == 1
    assert TaskResult.success("task-1", {"ok": True}).status == TaskStatus.SUCCEEDED
    assert TaskResult.failure("task-1", "nope").error_type == "TaskFailed"

    dead_letter = DeadLetterRecord.from_task(task, "failed")
    assert dead_letter.task.status == TaskStatus.DEAD_LETTER


def test_worker_heartbeat_routes_safe_fact_to_durable_runtime_stream(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "worker-events.sqlite3")
    runtime = EventRuntime(store=store, schema_catalog=default_event_schema_catalog())
    heartbeat_store = InMemoryWorkerHeartbeatStore(
        runtime_event_sink=CanonicalRuntimeEventPublisher(runtime)
    )

    heartbeat_store.save(
        WorkerHeartbeat(
            worker_id="worker-1",
            queue_names=["research"],
            current_task_id="task-1",
            metadata={"secret": "must-not-be-persisted"},
        )
    )

    page = store.read_stream(StreamReadRequest(stream_id="worker-1"))
    assert len(page.events) == 1
    event = page.events[0]
    assert event.event_type == "worker_heartbeat"
    payload = thaw_canonical_json(event.payload)
    assert payload["status"] == "running"
    assert payload["identity"]["attempt_id"] == "task-1"
    assert payload["refs"] == ["worker-1", "task-1"]
    assert "must-not-be-persisted" not in repr(payload)
