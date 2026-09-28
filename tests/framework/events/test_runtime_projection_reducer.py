from __future__ import annotations

import pytest

from framework.events.runtime.projection import RUNTIME_EVENT_SCHEMA_V1
from framework.events.runtime.projection_reducer import (
    RUNTIME_EVENT_DATA_SCHEMA,
    RUNTIME_PROJECTION_REDUCER_ID,
    RUNTIME_PROJECTION_REDUCER_VERSION,
    register_runtime_projection_reducer,
    runtime_projection_reducer,
    validate_runtime_projection_checkpoint_state,
)
from framework.events.runtime.replay_engine import ReplayEvent, ReplayReducerRegistry


def _event(
    *,
    event_id: str,
    sequence: int,
    schema: str = RUNTIME_EVENT_SCHEMA_V1,
    event_type: str = "turn_started",
    payload: dict | None = None,
) -> ReplayEvent:
    return ReplayEvent(
        event_id=event_id,
        event_type=event_type,
        source_data_schema=schema,
        data_schema=schema,
        stream_id="run-1",
        stream_sequence=sequence,
        occurred_at="2026-09-27T00:00:00Z",
        payload=payload or {
            "identity": {
                "graph_identity": None,
                "activity_id": "activity-1",
                "attempt_id": "attempt-1",
                "node_id": None,
                "node_instance_id": None,
            },
            "status": "running",
            "refs": [],
        },
        record_checksum="sha256:" + str(sequence).zfill(64),
    )


def _registration():
    registry = ReplayReducerRegistry()
    register_runtime_projection_reducer(registry)
    return registry.get(RUNTIME_PROJECTION_REDUCER_ID, RUNTIME_PROJECTION_REDUCER_VERSION)


def test_runtime_reducer_uses_runtime_event_owner_schema() -> None:
    assert RUNTIME_EVENT_DATA_SCHEMA == RUNTIME_EVENT_SCHEMA_V1
    state = runtime_projection_reducer(
        _registration().initial_state,
        _event(event_id="event-1", sequence=1),
    )
    assert len(state["statuses"]) == 1


def test_runtime_reducer_does_not_promote_worker_routing_candidates() -> None:
    state = runtime_projection_reducer(
        _registration().initial_state,
        _event(
            event_id="worker-status",
            sequence=1,
            event_type="worker_status",
            payload={
                "identity": {
                    "graph_identity": None,
                    "activity_id": "activity-1",
                    "attempt_id": "attempt-1",
                    "node_id": None,
                    "node_instance_id": None,
                },
                "status": "running",
                "refs": [],
                "routing_decision": {"target": "unapproved-node"},
                "next_action": "dispatch_tool",
                "tool_dispatch": {"tool": "unapproved-tool", "args": {"x": 1}},
            },
        ),
    )

    assert set(state) == {"state_schema", "cursors", "statuses"}
    status = next(iter(state["statuses"].values()))
    assert set(status) == {
        "identity",
        "status",
        "reason_code",
        "last_event_id",
        "sequence",
        "updated_at",
        "refs",
    }
    assert status["status"] == "running"
    assert status["last_event_id"] == "worker-status"


def test_runtime_reducer_uses_canonical_sequence_and_bounds_checkpoint_state() -> None:
    registration = _registration()
    state = registration.initial_state
    for sequence in range(1, 8):
        state = registration.reducer(
            state,
            _event(event_id=f"event-{sequence}", sequence=sequence),
        )

    assert state["cursors"]["run-1"]["sequence"] == 7
    assert len(state["statuses"]) == 1
    assert next(iter(state["statuses"].values()))["sequence"] == 7
    assert "seen_event_ids" not in state


def test_runtime_reducer_mixed_stream_advances_frontier_not_payload_sequence() -> None:
    state = _registration().initial_state
    state = runtime_projection_reducer(
        state,
        _event(event_id="runtime", sequence=1, payload={
            "identity": {
                "graph_identity": None,
                "activity_id": "activity-1",
                "attempt_id": "attempt-1",
                "node_id": None,
                "node_instance_id": None,
            },
            "sequence": 44,
            "status": "running",
            "refs": [],
        }),
    )
    state = runtime_projection_reducer(
        state,
        _event(event_id="foreign", sequence=2, schema="newsroom.other/v1"),
    )
    assert state["cursors"]["run-1"]["sequence"] == 2
    assert next(iter(state["statuses"].values()))["sequence"] == 1


def test_runtime_reducer_rejects_duplicate_sequence_and_schema_conflict() -> None:
    state = runtime_projection_reducer(
        _registration().initial_state,
        _event(event_id="event-1", sequence=1),
    )
    with pytest.raises(ValueError, match="not contiguous"):
        runtime_projection_reducer(state, _event(event_id="event-2", sequence=1))
    with pytest.raises(ValueError, match="state schema"):
        runtime_projection_reducer({**state, "unexpected": True}, _event(event_id="event-2", sequence=2))
    with pytest.raises(ValueError, match="canonical envelope"):
        runtime_projection_reducer(
            state,
            _event(event_id="event-2", sequence=2, payload={
                "identity": {"graph_identity": None, "activity_id": None, "attempt_id": None, "node_id": None, "node_instance_id": None},
                "event_id": "forged-event",
                "status": "failed",
                "refs": [],
            }),
        )


def test_runtime_reducer_uses_identity_fields_in_canonical_order() -> None:
    graph_identity = {
        "run_id": "run-1",
        "graph_id": "graph-1",
        "graph_version": "1",
        "graph_ref": "graph-ref",
        "graph_checksum": "sha256:" + "a" * 64,
        "node_id": "node-1",
        "node_instance_id": "instance-1",
        "activity_id": "activity-1",
        "attempt": 1,
    }
    state = _registration().initial_state
    for sequence, identity_fields in enumerate((graph_identity, dict(reversed(list(graph_identity.items())))), start=1):
        state = runtime_projection_reducer(
            state,
            _event(
                event_id=f"event-{sequence}",
                sequence=sequence,
                payload={
                    "identity": {
                        "graph_identity": identity_fields,
                        "activity_id": "activity-1",
                        "attempt_id": "attempt-1",
                        "node_id": "node-1",
                        "node_instance_id": "instance-1",
                    },
                    "status": "running",
                    "refs": [],
                },
            ),
        )
    assert len(state["statuses"]) == 1
    assert next(iter(state["statuses"].values()))["sequence"] == 2


def test_runtime_checkpoint_state_validates_when_no_events_remain() -> None:
    state = runtime_projection_reducer(
        _registration().initial_state,
        _event(event_id="event-1", sequence=1),
    )
    validate_runtime_projection_checkpoint_state(state, stream_id="run-1", last_sequence=1)
    with pytest.raises(ValueError, match="cursor"):
        validate_runtime_projection_checkpoint_state(state, stream_id="run-1", last_sequence=2)
    with pytest.raises(ValueError, match="cursor"):
        validate_runtime_projection_checkpoint_state(
            {
                **state,
                "cursors": {
                    "run-1": {
                        **state["cursors"]["run-1"],
                        "sequence": True,
                    }
                },
            },
            stream_id="run-1",
            last_sequence=1,
        )
    with pytest.raises(ValueError, match="scope"):
        validate_runtime_projection_checkpoint_state(state, stream_id="other-run", last_sequence=1)
    with pytest.raises(ValueError, match="state schema"):
        validate_runtime_projection_checkpoint_state(
            {**state, "state_schema": "unsupported"}, stream_id="run-1", last_sequence=1
        )
    with pytest.raises(ValueError, match="status"):
        validate_runtime_projection_checkpoint_state(
            {**state, "statuses": {"forged": {"status": "running"}}},
            stream_id="run-1", last_sequence=1,
        )
    with pytest.raises(ValueError, match="identity key"):
        validate_runtime_projection_checkpoint_state(
            {**state, "statuses": {"forged": next(iter(state["statuses"].values()))}},
            stream_id="run-1", last_sequence=1,
        )
