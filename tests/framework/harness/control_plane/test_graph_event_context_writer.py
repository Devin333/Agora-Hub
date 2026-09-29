from __future__ import annotations

from datetime import UTC, datetime

import pytest

from framework.events import (
    EventRuntime,
    GraphEventContext,
    GraphEventExecutionVersion,
    default_event_schema_catalog,
    graph_event_context,
)
from framework.events.canonical import checksum_for
from framework.events.runtime.models import StreamReadRequest
from framework.events.graph_phase import (
    GraphExecutionPhase,
    GraphPhaseBoundary,
    GraphPhaseTransitionRecord,
)
from framework.harness.control_plane import (
    DurableHarnessEventPort,
    DurableHarnessTransitionPort,
    HarnessEventCanonicalAdapter,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.event import HarnessEvent, HarnessEventType
from framework.harness.control_plane.graph_application import (
    HarnessGraphControlPlaneRuntime,
)
from framework.harness.graph import HarnessGraphCompiler
from framework.harness.graph.validation import HarnessGraphPreflightPolicy
from framework.harness.control_plane.state import run_spec_checksum
from framework.shared.graph_identity import GraphRunIdentity, GraphStageIdentity
from infrastructure.storage.events import SQLiteEventStore
from backend.research.graphs import build_paper_analysis_graph_definition
from framework.harness.control_plane.state import HarnessRunSpec


NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
GRAPH_CHECKSUM = checksum_for({"graph": "writer-test"})


def _context() -> GraphEventContext:
    identity = GraphRunIdentity(
        run_id="run-writer-test",
        graph_id="writer.graph",
        graph_version="1",
        graph_ref="writer.graph@1",
        graph_checksum=GRAPH_CHECKSUM,
    )
    return GraphEventContext(
        identity=identity,
        execution_version=GraphEventExecutionVersion(
            graph_schema_version="newsroom.normalized-harness-graph/v3",
            compiler_version="3",
            normalized_graph_checksum=GRAPH_CHECKSUM,
        ),
        stage_identity=GraphStageIdentity(
            run_id=identity.run_id,
            graph_id=identity.graph_id,
            graph_version=identity.graph_version,
            graph_ref=identity.graph_ref,
            graph_checksum=identity.graph_checksum,
            node_id="analyze",
            node_instance_id="analyze:1",
        ),
    )


def _record(sequence: int, *, phase: GraphExecutionPhase = GraphExecutionPhase.PLAN):
    return GraphPhaseTransitionRecord(
        context=_context(),
        phase=phase,
        boundary=GraphPhaseBoundary.ENTRY,
        attempt=0,
        event_sequence=sequence,
        occurred_at=NOW,
    )


def _runtime(tmp_path):
    store = SQLiteEventStore(tmp_path / "events.sqlite3", clock=lambda: NOW)
    runtime = EventRuntime(
        store=store,
        schema_catalog=default_event_schema_catalog(),
        monotonic=lambda: 1.0,
    )
    return store, runtime


def test_phase_writer_requires_durable_graph_transition_owner(tmp_path) -> None:
    _, runtime = _runtime(tmp_path)
    port = DurableHarnessEventPort(
        runtime,
        adapter=HarnessEventCanonicalAdapter(tenant_id="tenant-a"),
    )

    with pytest.raises(HarnessValidationError) as captured:
        port.record_graph_phase_transition(_record(1), expected_last_sequence=0)

    assert captured.value.code == "graph_phase_transition_owner_required"


def test_generic_durable_record_cannot_bypass_phase_writer(tmp_path) -> None:
    _, runtime = _runtime(tmp_path)
    port = DurableHarnessEventPort(
        runtime,
        adapter=HarnessEventCanonicalAdapter(tenant_id="tenant-a"),
    )
    record = _record(1)
    event = HarnessEvent(
        event_type=HarnessEventType.GRAPH_PHASE_TRANSITION_RECORDED,
        run_id=record.context.identity.run_id,
        node_id=record.context.node_id,
        payload=record.to_dict(),
        metadata={"graph_context": record.context.to_dict()},
        occurred_at=NOW,
    )

    with pytest.raises(HarnessValidationError, match="record_graph_phase_transition"):
        port.record(event)


def test_durable_graph_transition_port_commits_and_recovers_graph_identity(tmp_path) -> None:
    store, runtime = _runtime(tmp_path)
    adapter = HarnessEventCanonicalAdapter(tenant_id="tenant-a")
    port = DurableHarnessTransitionPort(runtime, store, adapter=adapter)
    run_spec = HarnessRunSpec(
        run_id="run-transition-writer",
        graph=build_paper_analysis_graph_definition(),
        created_at=NOW,
    )
    graph = HarnessGraphCompiler().compile(run_spec.graph).graph
    control_runtime = HarnessGraphControlPlaneRuntime(port)

    state = control_runtime.initialize(
        run_spec,
        graph,
        HarnessGraphPreflightPolicy(),
        run_spec_checksum=run_spec_checksum(run_spec),
    )
    recovery = port.recover_graph(run_spec.run_id)

    assert state.projection_checksum == recovery.state.projection_checksum
    assert recovery.expected_last_sequence == 1
    stored = store.read_stream(
        StreamReadRequest(
            stream_id=f"run:{run_spec.run_id}",
            limit=10,
            tenant_id="tenant-a",
        )
    )
    assert len(stored.events) == 1
    assert graph_event_context(stored.events[0]).identity.graph_id == graph.graph_id
