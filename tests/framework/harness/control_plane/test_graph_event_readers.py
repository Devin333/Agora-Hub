from __future__ import annotations

from datetime import UTC, datetime

import pytest

from framework.events.canonical import (
    BusinessContext,
    EventCandidate,
    PayloadReference,
    ProducerIdentity,
    StoredEvent,
    checksum_for,
)
from framework.events.errors import EventStoreCorruptionError
from framework.events.projection import GraphEventContext, GraphEventExecutionVersion
from framework.shared.graph_identity import GraphExecutionIdentity, GraphRunIdentity
from framework.events.schema.security import SecurityClassification
from framework.harness.control_plane.event import HarnessEvent, HarnessEventType
from framework.harness.control_plane.event_log import (
    event_log_entry_from_stored_event,
)
from framework.harness.control_plane.transcript import (
    transcript_entry_from_stored_event,
)
from framework.harness.graph.activity import graph_activity_input_checksum
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.reference import HarnessGraphReference
from framework.harness.control_plane.graph_runtime import HarnessGraphActivity
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)


_NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


def _activity() -> HarnessGraphActivity:
    task = {"inputs": {"source": "paper"}}
    graph_ref = HarnessGraphReference(
        graph_id="reader.graph",
        schema_version=GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        compiler_version=HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        condition_policy_version=HARNESS_CONDITION_POLICY_VERSION,
        checksum=checksum_for({"graph": "reader"}),
        graph_ref=HarnessContractReference(HarnessContractKind.GRAPH, "reader.graph", "1"),
    )
    return HarnessGraphActivity(
        run_id="run-reader",
        graph_ref=graph_ref,
        node_id="analyze",
        node_instance_id="hni-analyze-1",
        step_ref=HarnessContractReference(HarnessContractKind.STEP, "reader.analyze", "1"),
        worker_ref=HarnessContractReference(HarnessContractKind.WORKER, "reader.worker", "1"),
        activity_ref=HarnessContractReference(HarnessContractKind.ACTIVITY, "reader.activity", "1"),
        attempt=1,
        input_ref=graph_activity_input_checksum(task),
        causal_decision_checksum=checksum_for({"decision": "dispatch"}),
        causal_decision_sequence=3,
        fencing_generation=1,
    )


def _stored(*, event_type: str, extension_key: str, activity: HarnessGraphActivity) -> StoredEvent:
    identity = GraphRunIdentity(
        run_id=activity.run_id,
        graph_id=activity.graph_ref.graph_id,
        graph_version=activity.graph_ref.identity_version,
        graph_ref=f"{activity.graph_ref.graph_id}@{activity.graph_ref.identity_version}",
        graph_checksum=activity.graph_ref.checksum,
    )
    context = GraphEventContext(
        identity=identity,
        execution_version=GraphEventExecutionVersion(
            graph_schema_version=activity.graph_ref.schema_version,
            compiler_version=activity.graph_ref.compiler_version,
            normalized_graph_checksum=activity.graph_ref.checksum,
        ),
        execution_identity=GraphExecutionIdentity(
            run_id=activity.run_id,
            graph_id=identity.graph_id,
            graph_version=identity.graph_version,
            graph_ref=identity.graph_ref,
            graph_checksum=identity.graph_checksum,
            node_id=activity.node_id,
            node_instance_id=activity.node_instance_id,
            activity_id=activity.activity_id,
            attempt=activity.attempt,
        ),
    )
    payload_ref = PayloadReference(
        uri="secure-activity://tenant-a/reader-result",
        expected_checksum=checksum_for({"result": "ok"}),
        content_type="application/vnd.newsroom.recorded-activity+json",
        size_bytes=1,
    )
    candidate = EventCandidate(
        event_id="evt-reader-1",
        event_type=event_type,
        data_schema="newsroom.harness-graph-event/v1",
        source="io.newsroom.harness.control-plane",
        occurred_at=_NOW,
        stream_id=f"run:{activity.run_id}",
        correlation_id=activity.run_id,
        business_context=BusinessContext(
            run_id=activity.run_id,
            graph_id=identity.graph_id,
            graph_version=identity.graph_version,
            graph_ref=f"{identity.graph_id}@{identity.graph_version}",
            graph_checksum=identity.graph_checksum,
            stage_id=activity.node_id,
        ),
        producer=ProducerIdentity(component="framework.harness.control_plane", version="1"),
        tenant_id="tenant-a",
        security_classification=SecurityClassification.CONFIDENTIAL,
        content_type=payload_ref.content_type,
        payload=None,
        payload_ref=payload_ref,
        extensions={
            "graph_context": context.to_dict(),
            extension_key: {
                "activity": activity.to_dict(),
                "status": "succeeded",
            },
        },
    )
    return StoredEvent(candidate, observed_at=_NOW, stream_sequence=1)


def test_live_readers_project_graph_activity_extension() -> None:
    activity = _activity()
    event = _stored(
        event_type="graph_worker_result_recorded",
        extension_key="harness_graph_activity",
        activity=activity,
    )

    log = event_log_entry_from_stored_event(event)
    transcript = transcript_entry_from_stored_event(event)

    assert log.metadata["activity_id"] == activity.activity_id
    assert log.metadata["graph_ref"] == activity.graph_ref.to_dict()
    assert transcript.metadata["event_payload"]["activity_checksum"] == activity.activity_checksum
    assert transcript.metadata["event_payload"]["node_instance_id"] == activity.node_instance_id


def test_live_readers_reject_flat_activity_extension() -> None:
    activity = _activity()
    event = _stored(
        event_type="worker_result_recorded",
        extension_key="harness_activity",
        activity=activity,
    )

    with pytest.raises(EventStoreCorruptionError, match="Graph activity result"):
        event_log_entry_from_stored_event(event)
    with pytest.raises(EventStoreCorruptionError, match="Graph activity result"):
        transcript_entry_from_stored_event(event)


def test_live_event_contract_rejects_flat_worker_event() -> None:
    with pytest.raises(ValueError, match="not a valid HarnessEventType"):
        HarnessEvent(
            event_type="worker_result_recorded",
            run_id="run-reader",
            payload={"status": "succeeded"},
        )

    # Compare values: `"text" in EnumClass` raises TypeError on Python 3.11.
    event_types = {member.value for member in HarnessEventType}
    assert "worker_called" not in event_types
    assert "worker_result_recorded" not in event_types
