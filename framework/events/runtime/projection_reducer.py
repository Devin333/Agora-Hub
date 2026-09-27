"""Deterministic runtime read model over the canonical replay stream."""

from __future__ import annotations

from collections.abc import Mapping
from re import fullmatch
from typing import Any

from framework.events.runtime.replay_engine import (
    ReplayReducerRegistration,
    ReplayReducerRegistry,
)


RUNTIME_PROJECTION_REDUCER_ID = "runtime-event-projection"
RUNTIME_PROJECTION_REDUCER_VERSION = "1"
RUNTIME_PROJECTION_STATE_SCHEMA = "newsroom.runtime-projection-state/v1"
RUNTIME_EVENT_DATA_SCHEMA = "newsroom.runtime-event/v1"


def runtime_projection_reducer(state: dict[str, Any], event: Any) -> dict[str, Any]:
    """Fold one verified canonical record into bounded-per-entity status state.

    The replay engine owns event ID uniqueness, integrity, scope, and sequence
    continuity. A checkpoint retains only the latest status per identity and
    the last consumed canonical cursor, never the raw event history.
    """

    if (
        sorted(state) != ["cursors", "state_schema", "statuses"]
        or state["state_schema"] != "newsroom.runtime-projection-state/v1"
    ):
        raise ValueError("runtime projection state schema is invalid")
    cursors = dict(state["cursors"])
    statuses = dict(state["statuses"])
    stream_id = event.stream_id
    if cursors and (len(cursors) != 1 or stream_id not in cursors):
        raise ValueError("runtime projection stream scope changed")
    previous_cursor = cursors[stream_id] if stream_id in cursors else None
    previous_sequence = previous_cursor["sequence"] if previous_cursor is not None else 0
    if event.stream_sequence != previous_sequence + 1:
        raise ValueError("runtime projection sequence is not contiguous")
    cursors[stream_id] = {
        "stream_id": stream_id,
        "sequence": event.stream_sequence,
        "checksum": event.record_checksum,
    }

    if event.data_schema != "newsroom.runtime-event/v1":
        return {"state_schema": state["state_schema"], "cursors": cursors, "statuses": statuses}

    payload = event.payload
    if (
        ("event_id" in payload and payload["event_id"] != event.event_id)
        or ("event_type" in payload and payload["event_type"] != event.event_type)
        or (
            "stream_id" in payload
            and payload["stream_id"] is not None
            and payload["stream_id"] != stream_id
        )
    ):
        raise ValueError("runtime payload conflicts with canonical envelope")
    identity = dict(payload["identity"])
    graph_identity = dict(identity["graph_identity"] or {})
    for field in ("activity_id", "node_id", "node_instance_id"):
        if field in graph_identity and identity[field] not in (None, graph_identity[field]):
            raise ValueError("runtime identity conflicts with Graph identity")
        if field in graph_identity:
            identity[field] = graph_identity[field]
    status = payload["status"] if "status" in payload else None
    reason_code = payload["reason_code"] if "reason_code" in payload else None
    if status is not None or reason_code is not None:
        # Include the full Graph identity: run ID alone does not identify a
        # graph version or an activity. Input maps were canonicalized by the
        # schema catalog before arriving at this audited reducer.
        identity_key = str([
            graph_identity["run_id"] if graph_identity else None,
            graph_identity["graph_id"] if graph_identity else None,
            graph_identity["graph_version"] if graph_identity else None,
            graph_identity["graph_ref"] if graph_identity else None,
            graph_identity["graph_checksum"] if graph_identity else None,
            graph_identity["node_id"] if graph_identity else None,
            graph_identity["node_instance_id"] if graph_identity else None,
            graph_identity["activity_id"] if graph_identity else None,
            graph_identity["attempt"] if graph_identity else None,
            identity["activity_id"],
            identity["attempt_id"],
            identity["node_id"],
            identity["node_instance_id"],
        ])
        statuses[identity_key] = {
            "identity": identity,
            "status": status if status is not None else event.event_type,
            "reason_code": reason_code,
            "last_event_id": event.event_id,
            "sequence": event.stream_sequence,
            "updated_at": event.occurred_at,
            "refs": payload["refs"],
        }
    return {"state_schema": state["state_schema"], "cursors": cursors, "statuses": statuses}


def validate_runtime_projection_checkpoint_state(
    state: Any, *, stream_id: str, last_sequence: int
) -> None:
    """Reject an incompatible runtime checkpoint even if no events remain to fold."""

    if (
        not isinstance(state, Mapping)
        or set(state) != {"state_schema", "cursors", "statuses"}
        or state["state_schema"] != RUNTIME_PROJECTION_STATE_SCHEMA
        or not isinstance(state["cursors"], Mapping)
        or not isinstance(state["statuses"], Mapping)
    ):
        raise ValueError("runtime projection checkpoint state schema is invalid")
    if (
        isinstance(last_sequence, bool)
        or not isinstance(last_sequence, int)
        or last_sequence < 0
    ):
        raise ValueError("runtime projection checkpoint sequence is invalid")
    cursors = state["cursors"]
    statuses = state["statuses"]
    if last_sequence == 0:
        if cursors or statuses:
            raise ValueError("empty runtime projection checkpoint has state")
        return
    if set(cursors) != {stream_id}:
        raise ValueError("runtime projection checkpoint stream scope changed")
    cursor = cursors[stream_id]
    if (
        not isinstance(cursor, Mapping)
        or set(cursor) != {"stream_id", "sequence", "checksum"}
        or cursor["stream_id"] != stream_id
        or isinstance(cursor["sequence"], bool)
        or cursor["sequence"] != last_sequence
        or not isinstance(cursor["checksum"], str)
        or fullmatch(r"sha256:[0-9a-f]{64}", cursor["checksum"]) is None
    ):
        raise ValueError("runtime projection checkpoint cursor is invalid")
    expected_status_fields = {
        "identity",
        "status",
        "reason_code",
        "last_event_id",
        "sequence",
        "updated_at",
        "refs",
    }
    expected_identity_fields = {
        "graph_identity",
        "activity_id",
        "attempt_id",
        "node_id",
        "node_instance_id",
    }
    expected_graph_fields = {
        "run_id",
        "graph_id",
        "graph_version",
        "graph_ref",
        "graph_checksum",
        "node_id",
        "node_instance_id",
        "activity_id",
        "attempt",
    }
    for key, status in statuses.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(status, Mapping)
            or set(status) != expected_status_fields
            or not isinstance(status["identity"], Mapping)
            or set(status["identity"]) != expected_identity_fields
            or not isinstance(status["status"], str)
            or not status["status"]
            or (
                status["reason_code"] is not None
                and not isinstance(status["reason_code"], str)
            )
            or not isinstance(status["last_event_id"], str)
            or not status["last_event_id"]
            or isinstance(status["sequence"], bool)
            or not isinstance(status["sequence"], int)
            or not 1 <= status["sequence"] <= last_sequence
            or not isinstance(status["updated_at"], str)
            or not status["updated_at"]
            or not isinstance(status["refs"], (tuple, list))
            or any(not isinstance(ref, str) for ref in status["refs"])
        ):
            raise ValueError("runtime projection checkpoint status is invalid")
        identity = status["identity"]
        graph_identity = identity["graph_identity"]
        if graph_identity is not None:
            if (
                not isinstance(graph_identity, Mapping)
                or set(graph_identity) != expected_graph_fields
                or isinstance(graph_identity["attempt"], bool)
                or not isinstance(graph_identity["attempt"], int)
                or graph_identity["attempt"] < 1
                or any(
                    not isinstance(graph_identity[field], str)
                    or not graph_identity[field]
                    for field in expected_graph_fields - {"attempt"}
                )
                or fullmatch(r"sha256:[0-9a-f]{64}", graph_identity["graph_checksum"]) is None
                or any(
                    identity[field] != graph_identity[field]
                    for field in ("activity_id", "node_id", "node_instance_id")
                )
            ):
                raise ValueError("runtime projection checkpoint identity is invalid")
        if any(
            identity[field] is not None and not isinstance(identity[field], str)
            for field in ("activity_id", "attempt_id", "node_id", "node_instance_id")
        ):
            raise ValueError("runtime projection checkpoint identity is invalid")
        expected_key = str([
            graph_identity["run_id"] if graph_identity else None,
            graph_identity["graph_id"] if graph_identity else None,
            graph_identity["graph_version"] if graph_identity else None,
            graph_identity["graph_ref"] if graph_identity else None,
            graph_identity["graph_checksum"] if graph_identity else None,
            graph_identity["node_id"] if graph_identity else None,
            graph_identity["node_instance_id"] if graph_identity else None,
            graph_identity["activity_id"] if graph_identity else None,
            graph_identity["attempt"] if graph_identity else None,
            identity["activity_id"],
            identity["attempt_id"],
            identity["node_id"],
            identity["node_instance_id"],
        ])
        if key != expected_key:
            raise ValueError("runtime projection checkpoint identity key is invalid")


def register_runtime_projection_reducer(registry: ReplayReducerRegistry) -> None:
    """Register the pinned runtime projection reducer exactly once."""

    registry.register(
        ReplayReducerRegistration(
            reducer_id=RUNTIME_PROJECTION_REDUCER_ID,
            version=RUNTIME_PROJECTION_REDUCER_VERSION,
            reducer=runtime_projection_reducer,
            initial_state={
                "state_schema": RUNTIME_PROJECTION_STATE_SCHEMA,
                "cursors": {},
                "statuses": {},
            },
        )
    )


__all__ = [
    "RUNTIME_EVENT_DATA_SCHEMA",
    "RUNTIME_PROJECTION_REDUCER_ID",
    "RUNTIME_PROJECTION_REDUCER_VERSION",
    "RUNTIME_PROJECTION_STATE_SCHEMA",
    "register_runtime_projection_reducer",
    "validate_runtime_projection_checkpoint_state",
    "runtime_projection_reducer",
]
