from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from framework.events import EventRuntime, default_event_schema_catalog
from framework.events.canonical import checksum_for, thaw_canonical_json
from framework.events.errors import (
    EventIdentityCollisionError,
    EventStoreCorruptionError,
)
from framework.events.graph_phase import (
    GraphExecutionPhase,
    GraphPhaseBoundary,
    GraphPhaseTransitionRecord,
)
from framework.events.projection import GraphEventContext, GraphEventExecutionVersion
from framework.events.runtime.models import StreamReadRequest
from framework.events.schema import EventSecurityProjector
from framework.harness.control_plane.durable_events import (
    DurableHarnessTransitionPort,
    HarnessEventCanonicalAdapter,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.event import HarnessEvent
from framework.harness.control_plane.harness import HarnessControlPlane
from framework.harness.graph.activity import HarnessRetryPolicy
from framework.harness.graph.decision import HarnessGraphDecisionType
from framework.harness.side_effects.fake import (
    CountingHarnessSideEffectHandler,
    InMemoryHarnessSideEffectStore,
)
from framework.harness.side_effects.registry import (
    HarnessSideEffectHandlerBinding,
    HarnessSideEffectRegistry,
)
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphRunIdentity, GraphStageIdentity
from infrastructure.storage.events import SQLiteEventStore
from infrastructure.storage.events.activity_store import SQLiteRecordedActivityStore
from tests.framework.harness.control_plane.test_graph_checkpoint_replay import (
    _FunctionWorker,
    _install_local_physical_dispatcher,
    _linear_authority,
    _linear_run_spec,
)


TENANT_ID = "tenant-phase-validation"
ACTIVITY_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@dataclass
class _DurableFixture:
    database: object
    store: SQLiteEventStore
    runtime: EventRuntime
    adapter: HarnessEventCanonicalAdapter
    port: DurableHarnessTransitionPort
    control_plane: HarnessControlPlane
    run_spec: object
    workers: tuple[object, ...]


@dataclass
class _FailOnceWorker:
    worker_id: str = "first"
    worker_version: str = "1"
    worker_type: str = "function"
    call_count: int = 0

    def execute(self, _task: dict) -> HarnessWorkerResult:
        self.call_count += 1
        if self.call_count == 1:
            return HarnessWorkerResult("failed", error="retryable failure")
        return HarnessWorkerResult("succeeded", output={"first": True})


class _StopBeforeExecutePort(DurableHarnessTransitionPort):
    def commit_graph_decision(self, decision, **kwargs):
        if decision.decision_type is HarnessGraphDecisionType.DISPATCH_ACTIVITY:
            raise RuntimeError("stop before execute")
        return super().commit_graph_decision(decision, **kwargs)


def test_sqlite_reopen_accepts_ordered_phase_history_and_idempotent_retry(
    tmp_path,
) -> None:
    fixture = _fixture(tmp_path, "run-phase-valid")

    result = fixture.control_plane.run(fixture.run_spec)
    recovery = fixture.port.recover_graph(fixture.run_spec.run_id)
    head = recovery.expected_last_sequence
    first = recovery.phase_transition_records[0]

    assert result.succeeded
    assert tuple(item.event_sequence for item in recovery.phase_transition_records) == tuple(
        sorted(item.event_sequence for item in recovery.phase_transition_records)
    )
    assert any(
        second.event_sequence > first_item.event_sequence + 1
        for first_item, second in zip(
            recovery.phase_transition_records,
            recovery.phase_transition_records[1:],
            strict=True,
        )
    )
    for node_instance_id in {
        item.context.node_instance_id for item in recovery.phase_transition_records
    }:
        records = tuple(
            item
            for item in recovery.phase_transition_records
            if item.context.node_instance_id == node_instance_id
        )
        assert tuple((item.phase.value, item.boundary.value) for item in records) == (
            ("plan", "entry"),
            ("plan", "exit"),
            ("execute", "entry"),
            ("execute", "exit"),
            ("verify", "entry"),
            ("verify", "exit"),
        )

    duplicate = fixture.port.record_graph_phase_transition(
        first,
        expected_last_sequence=first.event_sequence - 1,
    )
    assert thaw_canonical_json(
        duplicate.payload["graph_phase_transition"]
    ) == first.to_dict()
    assert fixture.store.get_stream_high_watermark(
        f"run:{fixture.run_spec.run_id}",
        tenant_id=TENANT_ID,
    ) == head

    reopened = _reopen(fixture)
    rebuilt = reopened.recover_graph(fixture.run_spec.run_id)
    assert rebuilt.state == recovery.state
    assert rebuilt.phase_transition_records == recovery.phase_transition_records


def test_sqlite_retry_keeps_phase_attempt_order_valid(tmp_path) -> None:
    worker = _FailOnceWorker()
    fixture = _fixture(
        tmp_path,
        "run-phase-retry",
        workers=(worker, _FunctionWorker("second", {"second": True})),
        retry_first=True,
    )

    result = fixture.control_plane.run(fixture.run_spec)
    recovery = _reopen(fixture).recover_graph(fixture.run_spec.run_id)
    first_node = next(
        item.context.node_instance_id
        for item in recovery.phase_transition_records
        if item.context.node_id == "first"
    )
    records = tuple(
        item
        for item in recovery.phase_transition_records
        if item.context.node_instance_id == first_node
    )

    assert result.succeeded
    assert worker.call_count == 2
    assert tuple(
        (item.phase.value, item.boundary.value, item.attempt) for item in records
    ) == (
        ("plan", "entry", 0),
        ("plan", "exit", 0),
        ("execute", "entry", 1),
        ("execute", "exit", 1),
        ("execute", "entry", 2),
        ("execute", "exit", 2),
        ("verify", "entry", 2),
        ("verify", "exit", 2),
    )


@pytest.mark.parametrize(
    "corruption",
    ("graph", "node_instance", "attempt", "envelope_sequence", "terminal_reentry"),
)
def test_sqlite_reopen_rejects_checksum_valid_phase_corruption_without_writes(
    tmp_path,
    corruption: str,
) -> None:
    fixture = _fixture(tmp_path, f"run-phase-corrupt-{corruption}")
    fixture.control_plane.run(fixture.run_spec)
    recovery = fixture.port.recover_graph(fixture.run_spec.run_id)
    head = recovery.expected_last_sequence
    source = recovery.phase_transition_records[-1]
    terminal_node = next(
        item
        for item in recovery.state.node_instances
        if item.instance_id == source.context.node_instance_id
    )
    record = GraphPhaseTransitionRecord(
        context=_corrupt_context(source.context, corruption),
        phase=GraphExecutionPhase.PLAN,
        boundary=GraphPhaseBoundary.ENTRY,
        attempt=(terminal_node.attempt + 1 if corruption == "attempt" else terminal_node.attempt),
        event_sequence=(head + 2 if corruption == "envelope_sequence" else head + 1),
        occurred_at=source.occurred_at,
    )
    _append_untrusted_phase(fixture, record, expected_last_sequence=head)
    corrupted_head = head + 1
    before_count = len(_stored_events(fixture))

    reopened = _reopen(fixture)
    publish_calls: list[object] = []

    def forbidden_publish(*args, **kwargs):
        publish_calls.append((args, kwargs))
        raise AssertionError("offline recovery attempted a live write")

    reopened._runtime.publish = forbidden_publish  # type: ignore[method-assign]
    with pytest.raises(EventStoreCorruptionError):
        reopened.recover_graph(fixture.run_spec.run_id)

    assert publish_calls == []
    assert fixture.store.get_stream_high_watermark(
        f"run:{fixture.run_spec.run_id}",
        tenant_id=TENANT_ID,
    ) == corrupted_head
    assert len(_stored_events(fixture)) == before_count


def test_live_and_reopened_reader_reject_verify_without_execute(tmp_path) -> None:
    first = _FunctionWorker("first", {"first": True})
    second = _FunctionWorker("second", {"second": True})
    fixture = _fixture(
        tmp_path,
        "run-phase-no-execute",
        workers=(first, second),
        port_type=_StopBeforeExecutePort,
    )
    with pytest.raises(RuntimeError, match="stop before execute"):
        fixture.control_plane.run(fixture.run_spec)

    recovery = fixture.port.recover_graph(fixture.run_spec.run_id)
    assert tuple(
        (item.phase.value, item.boundary.value)
        for item in recovery.phase_transition_records
    ) == (("plan", "entry"), ("plan", "exit"))
    head = recovery.expected_last_sequence
    source = recovery.phase_transition_records[-1]
    record = GraphPhaseTransitionRecord(
        context=source.context,
        phase=GraphExecutionPhase.VERIFY,
        boundary=GraphPhaseBoundary.ENTRY,
        attempt=source.attempt,
        event_sequence=head + 1,
        occurred_at=source.occurred_at,
        gate_evidence_refs=(checksum_for("false-success"),),
    )

    with pytest.raises(HarnessValidationError) as captured:
        fixture.port.record_graph_phase_transition(
            record,
            expected_last_sequence=head,
        )
    assert captured.value.code == "graph_phase_transition_invalid"
    assert fixture.store.get_stream_high_watermark(
        f"run:{fixture.run_spec.run_id}",
        tenant_id=TENANT_ID,
    ) == head

    _append_untrusted_phase(fixture, record, expected_last_sequence=head)
    with pytest.raises(EventStoreCorruptionError):
        _reopen(fixture).recover_graph(fixture.run_spec.run_id)


def test_phase_event_identity_conflict_is_rejected_without_advancing_stream(
    tmp_path,
) -> None:
    fixture = _fixture(tmp_path, "run-phase-identity-conflict")
    fixture.control_plane.run(fixture.run_spec)
    recovery = fixture.port.recover_graph(fixture.run_spec.run_id)
    source = recovery.phase_transition_records[0]
    stored_event = next(
        item
        for item in _stored_events(fixture)
        if item.stream_sequence == source.event_sequence
    )
    conflicting = replace(
        source,
        phase=GraphExecutionPhase.VERIFY,
    )
    event = HarnessEvent(
        event_id=stored_event.event_id,
        event_type="graph_phase_transition_recorded",
        run_id=fixture.run_spec.run_id,
        node_id=conflicting.context.node_id,
        payload=conflicting.to_dict(),
        metadata={"graph_context": conflicting.context.to_dict()},
        occurred_at=conflicting.occurred_at,
    )
    request = fixture.adapter.to_publish_request(
        event,
        graph_context=conflicting.context,
    )
    head = recovery.expected_last_sequence

    with pytest.raises(EventIdentityCollisionError):
        fixture.runtime.publish(request, expected_last_sequence=head)

    assert fixture.store.get_stream_high_watermark(
        f"run:{fixture.run_spec.run_id}",
        tenant_id=TENANT_ID,
    ) == head


def _fixture(
    tmp_path,
    run_id: str,
    *,
    workers: tuple[object, ...] | None = None,
    retry_first: bool = False,
    port_type=DurableHarnessTransitionPort,
) -> _DurableFixture:
    database = tmp_path / f"{run_id}.sqlite3"
    store = SQLiteEventStore(database)
    activity_store = SQLiteRecordedActivityStore(
        database,
        encryption_key=ACTIVITY_KEY,
    )
    runtime = EventRuntime(
        store=store,
        schema_catalog=default_event_schema_catalog(),
        security_projector=EventSecurityProjector(
            secure_payload_store=activity_store,
        ),
        monotonic=lambda: 1.0,
    )
    adapter = HarnessEventCanonicalAdapter(tenant_id=TENANT_ID)
    port = port_type(
        runtime,
        store,
        secure_activity_store=activity_store,
        adapter=adapter,
    )
    run_spec = _linear_run_spec(run_id)
    metadata = dict(run_spec.metadata)
    metadata["identity_scope_ref"] = adapter.identity_scope_ref
    if retry_first:
        activities = tuple(
            replace(
                item,
                retry_policy=HarnessRetryPolicy(max_retries=1, max_attempts=2),
            )
            if item.step_id == "first"
            else item
            for item in run_spec.graph.activities
        )
        run_spec = replace(
            run_spec,
            graph=replace(
                run_spec.graph,
                activities=activities,
                definition_checksum=None,
            ),
            budget=replace(
                run_spec.budget,
                max_retries_per_step=1,
                max_worker_calls=run_spec.budget.max_worker_calls + 1,
            ),
            metadata=metadata,
        )
    else:
        run_spec = replace(run_spec, metadata=metadata)
    selected_workers = workers or (
        _FunctionWorker("first", {"first": True}),
        _FunctionWorker("second", {"second": True}),
    )
    side_effect_store = InMemoryHarnessSideEffectStore()
    registry = HarnessSideEffectRegistry(
        (
            HarnessSideEffectHandlerBinding(
                "test.terminal@1",
                "artifact",
                CountingHarnessSideEffectHandler(
                    side_effect_store,
                    disposition="accepted",
                ),
            ),
        )
    )
    control_plane = HarnessControlPlane(
        event_port=port,
        side_effect_store=side_effect_store,
        runtime_binding_authority=_linear_authority(
            selected_workers,
            side_effect_registry=registry,
        ),
    )
    _install_local_physical_dispatcher(control_plane)
    return _DurableFixture(
        database,
        store,
        runtime,
        adapter,
        port,
        control_plane,
        run_spec,
        selected_workers,
    )


def _reopen(fixture: _DurableFixture) -> DurableHarnessTransitionPort:
    store = SQLiteEventStore(fixture.database)
    activity_store = SQLiteRecordedActivityStore(
        fixture.database,
        encryption_key=ACTIVITY_KEY,
    )
    runtime = EventRuntime(
        store=store,
        schema_catalog=default_event_schema_catalog(),
        security_projector=EventSecurityProjector(
            secure_payload_store=activity_store,
        ),
        monotonic=lambda: 1.0,
    )
    return DurableHarnessTransitionPort(
        runtime,
        store,
        secure_activity_store=activity_store,
        adapter=fixture.adapter,
    )


def _append_untrusted_phase(
    fixture: _DurableFixture,
    record: GraphPhaseTransitionRecord,
    *,
    expected_last_sequence: int,
) -> None:
    event = HarnessEvent(
        event_type="graph_phase_transition_recorded",
        run_id=record.context.identity.run_id,
        node_id=record.context.node_id,
        payload=record.to_dict(),
        metadata={"graph_context": record.context.to_dict()},
        occurred_at=record.occurred_at,
    )
    request = fixture.adapter.to_publish_request(
        event,
        graph_context=record.context,
    )
    fixture.runtime.publish(
        request,
        expected_last_sequence=expected_last_sequence,
    )


def _stored_events(fixture: _DurableFixture):
    return fixture.store.read_stream(
        StreamReadRequest(
            stream_id=f"run:{fixture.run_spec.run_id}",
            limit=500,
            tenant_id=TENANT_ID,
        )
    ).events


def _corrupt_context(
    context: GraphEventContext,
    corruption: str,
) -> GraphEventContext:
    if corruption == "node_instance":
        stage = replace(context.stage_identity, node_instance_id="wrong-node:99")
        return replace(context, stage_identity=stage)
    if corruption != "graph":
        return context
    checksum = checksum_for("wrong-graph")
    identity = GraphRunIdentity(
        run_id=context.identity.run_id,
        graph_id=context.identity.graph_id,
        graph_version=context.identity.graph_version,
        graph_ref=context.identity.graph_ref,
        graph_checksum=checksum,
    )
    stage = GraphStageIdentity(
        run_id=identity.run_id,
        graph_id=identity.graph_id,
        graph_version=identity.graph_version,
        graph_ref=identity.graph_ref,
        graph_checksum=checksum,
        node_id=context.node_id,
        node_instance_id=context.node_instance_id,
    )
    execution = GraphEventExecutionVersion(
        graph_schema_version=context.execution_version.graph_schema_version,
        compiler_version=context.execution_version.compiler_version,
        normalized_graph_checksum=checksum,
    )
    return GraphEventContext(
        identity=identity,
        execution_version=execution,
        stage_identity=stage,
    )
