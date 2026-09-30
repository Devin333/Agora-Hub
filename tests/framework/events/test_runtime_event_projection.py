from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from framework.events.canonical import thaw_canonical_json
from framework.events.runtime.models import StreamReadRequest
from framework.events.runtime.projection import (
    CanonicalRuntimeEventPublisher,
    InMemoryRuntimeEventStore,
    RuntimeCursorConflict,
    RuntimeEventCursor,
    RuntimeEventEnvelope,
    RuntimeEventIdentity,
    RuntimeEventIdentityConflict,
    RuntimeEventEmitter,
    RuntimeEventProjection,
    RuntimeEventType,
    RuntimeOperatorStatusService,
    runtime_event_publish_request,
)
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import RUNTIME_EVENT_DATA_SCHEMA, default_event_schema_catalog
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.storage.events.sqlite import SQLiteEventStore


def _identity() -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id="run-1",
        graph_id="graph",
        graph_version="1.0.0",
        graph_ref="graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="node",
        node_instance_id="node-1",
        activity_id="activity",
        attempt=1,
    )


def _event(event_id: str, *, sequence: int | None = None, status: str = "running") -> RuntimeEventEnvelope:
    return RuntimeEventEnvelope(
        event_id=event_id,
        event_type=RuntimeEventType.WORKER_STATUS,
        occurred_at=datetime.now(UTC),
        identity=RuntimeEventIdentity(graph_identity=_identity(), activity_id="activity", attempt_id="attempt-1"),
        status=status,
        reason_code="test",
        sequence=sequence,
        stream_id="run-1",
        metadata={"token": "do-not-store", "safe": "ok"},
    )


class _DelegatingRuntimeEventStore:
    """RuntimeEventStorePort wrapper used to exercise the generic path."""

    def __init__(self, inner: InMemoryRuntimeEventStore) -> None:
        self.inner = inner

    def append(self, event: RuntimeEventEnvelope) -> RuntimeEventEnvelope:
        return self.inner.append(event)

    def read(self, *, stream_id: str, after_sequence: int = 0, limit: int = 100):
        return self.inner.read(stream_id=stream_id, after_sequence=after_sequence, limit=limit)


class _SequenceMismatchRuntimeEventStore(_DelegatingRuntimeEventStore):
    def read(self, *, stream_id: str, after_sequence: int = 0, limit: int = 100):
        page = super().read(stream_id=stream_id, after_sequence=after_sequence, limit=limit)
        if not page.events:
            return page
        return replace(page, events=(replace(page.events[0], sequence=(page.events[0].sequence or 0) + 1),))


def test_projection_redacts_and_deduplicates_events() -> None:
    projection = RuntimeEventProjection()
    accepted = projection.append(_event("event-1"))
    assert accepted.sequence == 1
    assert accepted.metadata["token"] == "[redacted]"
    assert projection.apply(accepted) is False
    assert len(projection.status(run_id="run-1")) == 1


def test_projection_rebuild_does_not_run_effects_and_cursor_is_checked() -> None:
    store = InMemoryRuntimeEventStore()
    projection = RuntimeEventProjection(store=store)
    projection.append(_event("event-1"))
    projection.append(_event("event-2", status="succeeded"))
    service = RuntimeOperatorStatusService(projection)
    status = service.get_status(run_id="run-1", node_id="node")
    assert len(status) == 1
    assert status[0].to_dict()["status"] == "succeeded"
    page = service.get_timeline(stream_id="run-1")
    assert page.to_dict()["cursor"] == page.cursor.encode()
    assert len(page.events) == 2
    assert page.cursor is not None
    projection.rebuild()
    assert len(projection.status(run_id="run-1")) == 1
    assert projection.timeline(stream_id="run-1", after=page.cursor).events == ()
    tampered = type(page.cursor)(page.cursor.stream_id, page.cursor.sequence, "sha256:" + "f" * 64)
    with pytest.raises(RuntimeCursorConflict):
        projection.timeline(stream_id="run-1", after=tampered)


def test_projection_cursor_validation_uses_runtime_store_port_wrapper() -> None:
    store = _DelegatingRuntimeEventStore(InMemoryRuntimeEventStore())
    projection = RuntimeEventProjection(store=store)
    projection.append(_event("event-1"))
    page = projection.timeline(stream_id="run-1")
    assert page.cursor is not None
    tampered = RuntimeEventCursor(page.cursor.stream_id, page.cursor.sequence, "sha256:" + "f" * 64)
    with pytest.raises(RuntimeCursorConflict):
        projection.timeline(stream_id="run-1", after=tampered)


def test_projection_cursor_validation_rejects_missing_and_mismatched_canonical_events() -> None:
    inner = InMemoryRuntimeEventStore()
    projection = RuntimeEventProjection(store=_DelegatingRuntimeEventStore(inner))
    projection.append(_event("event-1"))
    event = inner.read(stream_id="run-1").events[0]
    assert event.sequence == 1

    missing = RuntimeEventCursor("run-1", 2, "sha256:" + "a" * 64)
    with pytest.raises(RuntimeCursorConflict):
        projection.timeline(stream_id="run-1", after=missing)

    mismatch = RuntimeEventProjection(store=_SequenceMismatchRuntimeEventStore(inner))
    mismatch_cursor = RuntimeEventCursor("run-1", 1, "sha256:" + "a" * 64)
    with pytest.raises(RuntimeCursorConflict):
        mismatch.timeline(stream_id="run-1", after=mismatch_cursor)


def test_projection_cursor_sequence_zero_is_initial_position_for_wrapped_store() -> None:
    projection = RuntimeEventProjection(store=_DelegatingRuntimeEventStore(InMemoryRuntimeEventStore()))
    page = projection.timeline(
        stream_id="run-1",
        after=RuntimeEventCursor("run-1", 0, "sha256:" + "0" * 64),
    )
    assert page.events == ()


def test_projection_attached_to_existing_store_replays_committed_history() -> None:
    store = InMemoryRuntimeEventStore()
    store.append(_event("preexisting-event"))
    projection = RuntimeEventProjection(store=store)
    assert projection.status(run_id="run-1")[0].last_event_id == "preexisting-event"
    assert projection.cursor("run-1") is not None


def test_identity_collision_is_rejected() -> None:
    store = InMemoryRuntimeEventStore()
    store.append(_event("event-1"))
    with pytest.raises(RuntimeEventIdentityConflict):
        store.append(_event("event-1", status="failed"))


@pytest.mark.parametrize(
    ("conflicting_field", "conflicting_value"),
    (("stream_id", "another-stream"), ("sequence", 2)),
)
def test_identity_collision_rejects_explicit_stream_and_sequence_conflicts(
    conflicting_field: str,
    conflicting_value: str | int,
) -> None:
    store = InMemoryRuntimeEventStore()
    store.append(_event("event-1"))
    conflicting = replace(
        _event("event-1"),
        **{conflicting_field: conflicting_value},
    )

    with pytest.raises(RuntimeEventIdentityConflict):
        store.append(conflicting)


def test_identity_fields_must_match_graph_identity() -> None:
    with pytest.raises(RuntimeEventIdentityConflict):
        RuntimeEventIdentity(graph_identity=_identity(), activity_id="different")


def test_out_of_order_delivery_remains_retry_safe() -> None:
    projection = RuntimeEventProjection()
    first = projection.apply(_event("event-3", sequence=3))
    event_one = _event("event-1", sequence=1)
    second = projection.apply(event_one)
    assert first is True
    assert second is True
    assert projection.apply(event_one) is False
    assert projection.cursor("run-1") is not None
    assert projection.cursor("run-1").sequence == 1
    projection.apply(_event("event-2", sequence=2))
    assert projection.cursor("run-1").sequence == 3


def test_runtime_emitter_is_stable_and_redacts_protected_payload() -> None:
    projection = RuntimeEventProjection()
    emitter = RuntimeEventEmitter(
        projection,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        stream_id="run-1",
    )
    event = emitter.emit(
        "tool_call_requested",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={
            "arguments": {"file_content": "secret", "safe": "ok"},
            "diagnostic": {"api_key": "nested-secret", "safe": "ok"},
        },
    )
    assert event.metadata["arguments"] == "[redacted]"
    assert event.metadata["diagnostic"] == {
        "api_key": "[redacted]",
        "safe": "ok",
    }
    request = runtime_event_publish_request(event)
    default_event_schema_catalog().validate(
        request.event_type,
        request.data_schema,
        request.payload,
    )
    assert event.event_id == emitter.emit(
        "tool_call_requested",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        metadata={
            "arguments": {"file_content": "secret", "safe": "ok"},
            "diagnostic": {"api_key": "nested-secret", "safe": "ok"},
        },
    ).event_id


def test_runtime_emitter_normalizes_iterable_inputs_before_identity_and_envelope() -> None:
    projection = RuntimeEventProjection()
    emitter = RuntimeEventEmitter(
        projection,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        stream_id="run-1",
    )
    checksum = "sha256:" + "a" * 64
    event = emitter.emit(
        "tool_call_requested",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        refs=(ref for ref in ("artifact://one", "artifact://two")),
        checksums={"artifact://one": checksum},
        metadata={"safe": "ok"},
    )

    assert event.refs == ("artifact://one", "artifact://two")
    assert event.checksums == {"artifact://one": checksum}
    replay = emitter.emit(
        "tool_call_requested",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        refs=(ref for ref in ("artifact://one", "artifact://two")),
        checksums={"artifact://one": checksum},
        metadata={"safe": "ok"},
    )
    assert replay.event_id == event.event_id


def test_runtime_emitter_checksum_changes_change_automatic_identity() -> None:
    emitter = RuntimeEventEmitter(
        lambda _event: None,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        stream_id="run-1",
    )
    first = emitter.emit(
        "tool_call_requested",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        checksums={"artifact": "sha256:" + "a" * 64},
    )
    second = emitter.emit(
        "tool_call_requested",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        checksums={"artifact": "sha256:" + "b" * 64},
    )
    assert first.event_id != second.event_id


def test_runtime_emitter_maps_wait_timeout_to_canonical_timeout() -> None:
    projection = RuntimeEventProjection()
    emitter = RuntimeEventEmitter(
        projection,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        source="harness-wait-service",
        stream_id="run-1",
    )

    event = emitter.emit(
        "timeout",
        status="timed_out",
        reason_code="wait_timeout",
        refs=("wait-1",),
        metadata={"operation": "timeout", "wait_id": "wait-1"},
    )

    assert event is not None
    assert event.event_type is RuntimeEventType.TIMEOUT
    assert event.event_type is not RuntimeEventType.RUNTIME_ERROR
    assert projection.store.all_events(stream_id="run-1") == (event,)


def test_agent_loop_emitter_forwards_only_turn_lifecycle_facts() -> None:
    projection = RuntimeEventProjection()
    emitter = RuntimeEventEmitter(
        projection,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        source="agent-loop",
        stream_id="run-1",
    )

    started = emitter.emit("agent_started", metadata={"agent_id": "agent-1"})
    assert started is not None
    assert started.event_type is RuntimeEventType.TURN_STARTED

    for local_type in (
        "llm_call",
        "llm_stream_event",
        "parser_error",
        "tool_call",
        "tool_observation",
        "tool_approval_required",
        "agent_waiting_for_approval",
    ):
        assert emitter.emit(
            local_type,
            metadata={
                "payload": {
                    "raw_model_output": "must-not-be-persisted",
                    "tool_result": "must-not-be-persisted",
                }
            },
        ) is None

    completed = emitter.emit(
        "agent_completed",
        status="accepted",
        reason_code="final_output",
        metadata={"agent_id": "agent-1", "iteration": 1},
    )
    assert completed is not None
    assert completed.event_type is RuntimeEventType.TURN_STOPPED

    events = projection.store.all_events(stream_id="run-1")
    assert [event.event_type for event in events] == [
        RuntimeEventType.TURN_STARTED,
        RuntimeEventType.TURN_STOPPED,
    ]
    assert all(event.event_type is not RuntimeEventType.RUNTIME_ERROR for event in events)
    assert "must-not-be-persisted" not in repr([event.to_dict() for event in events])


def test_agent_loop_emitter_fails_closed_for_missing_or_mismatched_identity() -> None:
    missing_identity = RuntimeEventEmitter(
        lambda _event: None,
        source="agent-loop",
        stream_id="run-1",
    )
    with pytest.raises(RuntimeEventIdentityConflict):
        missing_identity.emit("agent_started")

    mismatched_stream = RuntimeEventEmitter(
        lambda _event: None,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        source="agent-loop",
        stream_id="different-run",
    )
    with pytest.raises(RuntimeEventIdentityConflict):
        mismatched_stream.emit("agent_started")


def test_agent_loop_safe_projection_is_the_only_payload_written_to_sqlite(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "agent-loop-runtime.sqlite3")
    publisher = CanonicalRuntimeEventPublisher(
        EventRuntime(store=store, schema_catalog=default_event_schema_catalog())
    )
    emitter = RuntimeEventEmitter(
        publisher,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        source="agent-loop",
        stream_id="run-1",
    )

    emitter.emit("agent_started", metadata={"agent_id": "agent-1"})
    assert emitter.emit(
        "llm_stream_event",
        metadata={"payload": {"text_delta": "sensitive-model-output"}},
    ) is None
    assert emitter.emit(
        "tool_call",
        metadata={"payload": {"arguments": "sensitive-tool-input"}},
    ) is None
    assert emitter.emit(
        "tool_approval_required",
        metadata={"payload": {"approval_id": "approval-secret"}},
    ) is None
    emitter.emit(
        "agent_completed",
        status="accepted",
        reason_code="final_output",
        metadata={"agent_id": "agent-1", "iteration": 1},
    )

    page = store.read_stream(StreamReadRequest(stream_id="run-1"))
    assert [event.event_type for event in page.events] == [
        RuntimeEventType.TURN_STARTED.value,
        RuntimeEventType.TURN_STOPPED.value,
    ]
    payloads = [thaw_canonical_json(event.payload) for event in page.events]
    assert all(
        payload["identity"]["graph_identity"] == _identity().to_dict()
        for payload in payloads
    )
    durable_content = repr(payloads)
    assert "sensitive-model-output" not in durable_content
    assert "sensitive-tool-input" not in durable_content
    assert "approval-secret" not in durable_content
    assert "runtime_error" not in durable_content


@pytest.mark.parametrize(
    ("source", "allowed", "blocked"),
    [
        (
            "context-compaction-runtime",
            "context_compaction_planned",
            "context_compaction_action_applied",
        ),
        ("worker-heartbeat", "worker_heartbeat", "unknown_worker_event"),
    ],
)
def test_scoped_runtime_emitters_reject_unowned_event_types(
    source: str,
    allowed: str,
    blocked: str,
) -> None:
    projection = RuntimeEventProjection()
    emitter = RuntimeEventEmitter(
        projection,
        identity=RuntimeEventIdentity(graph_identity=_identity()),
        source=source,
        stream_id="run-1",
    )
    accepted = emitter.emit(
        allowed,
        reason_code="lifecycle_fact",
        refs=("artifact://bounded-ref",),
        metadata={
            "safe": "ok",
            "output": "must-not-be-persisted",
            "prompt": "must-not-be-persisted",
        },
    )
    assert accepted is not None
    assert accepted.event_type is not RuntimeEventType.RUNTIME_ERROR
    assert emitter.emit(blocked, metadata={"secret": "must-not-be-persisted"}) is None
    events = projection.store.all_events(stream_id="run-1")
    assert len(events) == 1
    assert events[0].refs == ("artifact://bounded-ref",)
    assert events[0].metadata["output"] == "[redacted]"
    assert events[0].metadata["prompt"] == "[redacted]"


def test_runtime_schema_rejects_raw_payload_and_unknown_envelope_fields() -> None:
    catalog = default_event_schema_catalog()
    payload = _event("schema-event").to_dict()
    payload["metadata"] = {"arguments": {"secret": "value"}}
    with pytest.raises(Exception):
        catalog.validate("tool_requested", RUNTIME_EVENT_DATA_SCHEMA, payload)
    payload = _event("schema-event-2").to_dict()
    payload["raw_payload"] = "should-not-be-inline"
    with pytest.raises(Exception):
        catalog.validate("tool_requested", RUNTIME_EVENT_DATA_SCHEMA, payload)

    payload = _event("schema-event-3").to_dict()
    payload["metadata"] = {"diagnostic": {"api_key": "still-secret"}}
    with pytest.raises(Exception):
        catalog.validate("tool_requested", RUNTIME_EVENT_DATA_SCHEMA, payload)


def test_runtime_publish_request_keeps_source_in_canonical_envelope() -> None:
    catalog = default_event_schema_catalog()
    event = replace(_event("publish-event"), metadata={"safe": "ok"})
    request = runtime_event_publish_request(event)

    assert request.source == event.source
    assert "source" not in request.payload
    catalog.validate(request.event_type, request.data_schema, request.payload)
    # Previously stored v1 payloads may contain source; replay still accepts them.
    catalog.validate(
        request.event_type,
        request.data_schema,
        {**request.payload, "source": event.source},
    )


def test_canonical_runtime_event_publisher_binds_the_composed_tenant_scope(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "tenant-scoped-runtime.sqlite3")
    runtime = EventRuntime(store=store, schema_catalog=default_event_schema_catalog())
    publisher = CanonicalRuntimeEventPublisher(runtime, tenant_id="tenant-runtime")

    publisher.publish(_event("tenant-scoped-event"))

    stored = store.read_stream(
        StreamReadRequest(stream_id="run-1", tenant_id="tenant-runtime")
    )
    assert len(stored.events) == 1
    assert stored.events[0].tenant_id == "tenant-runtime"
    assert publisher.tenant_id == "tenant-runtime"


@pytest.mark.parametrize("tenant_id", ["", "   ", 42])
def test_canonical_runtime_event_publisher_rejects_invalid_tenant_scope(tenant_id) -> None:
    runtime = type("Runtime", (), {"publish": lambda self, event: None})()
    with pytest.raises(ValueError):
        CanonicalRuntimeEventPublisher(runtime, tenant_id=tenant_id)
