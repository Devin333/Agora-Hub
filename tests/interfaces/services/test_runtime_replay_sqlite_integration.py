from __future__ import annotations

import dis
from dataclasses import replace
from datetime import UTC, datetime

from framework.events.canonical import BusinessContext, ProducerIdentity
from framework.events.runtime.projection import (
    RuntimeEventEnvelope,
    RuntimeEventIdentity,
    runtime_event_publish_request,
)
from framework.events.runtime.projection_reducer import (
    runtime_projection_reducer,
)
from framework.events.runtime.publisher import EventPublishRequest
from infrastructure.storage.events.factory import durable_event_storage_from_env
from interfaces.models.actor import ActorContext
from interfaces.services.event_operator_factory import build_event_operator_service


TENANT_ID = "tenant-runtime-replay"
OTHER_TENANT_ID = "tenant-other"
STREAM_ID = "runtime:run-1"
NOW = datetime(2026, 9, 27, 1, 2, 3, tzinfo=UTC)


def test_sqlite_runtime_replay_resumes_after_reopen_and_matches_full_rebuild(
    tmp_path,
    monkeypatch,
) -> None:
    first_storage = durable_event_storage_from_env(artifact_root=tmp_path, env={})
    first_service = _operator(first_storage, tmp_path)
    first_storage.event_runtime.publish(_runtime_request("runtime-1", payload_sequence=101))
    first_storage.event_runtime.publish(_runtime_request("runtime-2", payload_sequence=202))
    first_storage.event_runtime.publish(
        _runtime_request(
            "other-tenant-event",
            payload_sequence=1,
            tenant_id=OTHER_TENANT_ID,
        )
    )

    prefix = first_service.rebuild_runtime_projection(
        replay_id="runtime-prefix",
        source_stream_id=STREAM_ID,
        operator_reason="build durable runtime prefix",
    )
    assert prefix["replay_report"]["high_watermark"] == 2
    assert prefix["checkpoint"]["last_sequence"] == 2
    prefix_checkpoint_id = prefix["checkpoint"]["checkpoint_id"]
    prefix_checkpoint = first_storage.replay_checkpoint_store.get_checkpoint(
        prefix_checkpoint_id,
        tenant_id=TENANT_ID,
    )
    assert prefix_checkpoint is not None
    assert prefix_checkpoint.state["cursors"][STREAM_ID]["sequence"] == 2
    assert next(iter(prefix_checkpoint.state["statuses"].values()))["sequence"] == 2

    # Recompose every production adapter over the same SQLite file. This is
    # the process-restart boundary: no in-memory engine or checkpoint survives.
    del first_service
    del first_storage
    reopened_storage = durable_event_storage_from_env(artifact_root=tmp_path, env={})
    reopened_service = _operator(reopened_storage, tmp_path)
    third = reopened_storage.event_runtime.publish(
        _runtime_request("runtime-3", payload_sequence=303)
    )
    foreign_tail = reopened_storage.event_runtime.publish(_foreign_request("foreign-4"))
    assert third.stream_sequence == 3
    assert foreign_tail.stream_sequence == 4

    def unexpected_publish(*args, **kwargs):
        raise AssertionError("offline replay must not publish live events")

    monkeypatch.setattr(
        reopened_storage.event_runtime,
        "publish",
        unexpected_publish,
    )

    prefix_checkpoint = reopened_storage.replay_checkpoint_store.get_checkpoint(
        prefix_checkpoint_id,
        tenant_id=TENANT_ID,
    )
    assert prefix_checkpoint is not None
    resumed = reopened_service.rebuild_runtime_projection(
        replay_id="runtime-resumed",
        source_stream_id=STREAM_ID,
        operator_reason="resume durable runtime projection",
        from_sequence=3,
        checkpoint_ref=prefix_checkpoint.checkpoint_id,
    )
    resumed_retry = reopened_service.rebuild_runtime_projection(
        replay_id="runtime-resumed",
        source_stream_id=STREAM_ID,
        operator_reason="resume durable runtime projection",
        from_sequence=3,
        checkpoint_ref=prefix_checkpoint.checkpoint_id,
    )
    full = reopened_service.rebuild_runtime_projection(
        replay_id="runtime-full",
        source_stream_id=STREAM_ID,
        operator_reason="verify full runtime projection",
    )
    assert "state" not in resumed
    assert "state" not in resumed["checkpoint"]

    resumed_checkpoint = reopened_storage.replay_checkpoint_store.get_checkpoint(
        resumed["checkpoint"]["checkpoint_id"],
        tenant_id=TENANT_ID,
    )
    full_checkpoint = reopened_storage.replay_checkpoint_store.get_checkpoint(
        full["checkpoint"]["checkpoint_id"],
        tenant_id=TENANT_ID,
    )
    assert resumed_checkpoint is not None
    assert full_checkpoint is not None
    assert resumed_retry["replay_report"] == resumed["replay_report"]
    assert resumed_retry["checkpoint"] == resumed["checkpoint"]
    assert resumed["replay_report"]["high_watermark"] == 4
    assert resumed["checkpoint"]["last_sequence"] == 4
    assert resumed["checkpoint"]["parent_checkpoint_id"] == prefix_checkpoint.checkpoint_id
    assert resumed_checkpoint.state == full_checkpoint.state
    assert resumed["replay_report"]["result_checksum"] == full["replay_report"]["result_checksum"]
    assert resumed["checkpoint"]["history_checksum"] == full["checkpoint"]["history_checksum"]
    assert resumed_checkpoint.state["cursors"][STREAM_ID]["sequence"] == 4
    assert resumed_checkpoint.state["cursors"][STREAM_ID]["checksum"] == foreign_tail.record_checksum
    assert len(resumed_checkpoint.state["statuses"]) == 1
    projected_status = next(iter(resumed_checkpoint.state["statuses"].values()))
    assert projected_status["last_event_id"] == "runtime-3"
    assert projected_status["sequence"] == 3

    reports = reopened_service.list_replay_reports(source_stream_id=STREAM_ID)
    assert reports["tenant_id"] == TENANT_ID
    assert {item["replay_id"] for item in reports["items"]} >= {
        "runtime-prefix",
        "runtime-resumed",
        "runtime-full",
    }


def test_runtime_projection_reducer_has_no_live_execution_dependency() -> None:
    referenced_names = {
        str(instruction.argval).casefold()
        for instruction in dis.get_instructions(runtime_projection_reducer)
        if instruction.argval is not None
    }
    assert not any(
        marker in name
        for name in referenced_names
        for marker in ("worker", "tool", "llm", "scheduler", "executor")
    )


def _operator(storage, artifact_root):
    actor = ActorContext(
        actor_id="runtime-replay-operator",
        actor_type="service",
        roles=["service"],
        permissions=["events:read", "events:operate"],
        request_id="runtime-replay-integration",
    )
    return build_event_operator_service(
        actor,
        tenant_id=TENANT_ID,
        artifact_root=artifact_root,
        event_storage=storage,
        clock=lambda: NOW,
    )


def _runtime_request(
    event_id: str,
    *,
    payload_sequence: int,
    tenant_id: str = TENANT_ID,
) -> EventPublishRequest:
    envelope = RuntimeEventEnvelope(
        event_id=event_id,
        event_type="worker_status",
        occurred_at=NOW,
        identity=RuntimeEventIdentity(),
        status="running",
        sequence=payload_sequence,
        stream_id=STREAM_ID,
        refs=(),
    )
    return replace(runtime_event_publish_request(envelope), tenant_id=tenant_id)


def _foreign_request(event_id: str) -> EventPublishRequest:
    return EventPublishRequest(
        event_id=event_id,
        event_type="run_created",
        data_schema="newsroom.harness-graph-event/v1",
        source="runtime-replay-test",
        occurred_at=NOW,
        stream_id=STREAM_ID,
        tenant_id=TENANT_ID,
        business_context=BusinessContext(run_id="run-1"),
        producer=ProducerIdentity(component="runtime-replay-test", version="1"),
        payload={"projection_schema": "harness-safe-summary/v1"},
    )
