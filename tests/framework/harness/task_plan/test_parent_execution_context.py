from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
from framework.events.canonical import checksum_for, thaw_canonical_json
from framework.events.errors import EventStoreContentionError
from framework.events.runtime.models import TransactionalStateSnapshot
from framework.events.runtime.publisher import EventRuntime
from framework.events.schema import default_event_schema_catalog
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.canonical import canonical_payload_checksum
from framework.harness.task_plan.durable_store import (
    TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
    DurableTaskPlanStore,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.agent_loop.test_parent_ref_admission import _setup
from tests.interfaces.services.test_agent_loop_graph_service import _activity_and_input


STAGE_ID = "run-agent-loop"
STAGE_BINDING_CHECKSUM = checksum_for({"stage_binding": STAGE_ID})


def _execution_identity(context: HarnessGraphActivityTaskContext) -> GraphExecutionIdentity:
    activity = context.activity
    graph = activity.graph_ref
    return GraphExecutionIdentity(
        run_id=activity.run_id,
        graph_id=graph.graph_id,
        graph_version=graph.identity_version,
        graph_ref=graph.identity_ref.exact_ref,
        graph_checksum=graph.checksum,
        node_id=activity.node_id,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id,
        attempt=activity.attempt,
    )


def _state_key(identity: GraphExecutionIdentity) -> str:
    return canonical_payload_checksum(
        {
            "schema_version": TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
            "execution_identity": identity.to_dict(),
            "stage_id": STAGE_ID,
            "stage_binding_checksum": STAGE_BINDING_CHECKSUM,
        }
    )


def _fixture(root, *, run_id: str = "parent-context-run") -> SimpleNamespace:
    database = root / "task-plan.sqlite3"
    events = SQLiteEventStore(database)
    runtime = EventRuntime(
        store=events,
        schema_catalog=default_event_schema_catalog(),
    )
    store = DurableTaskPlanStore(
        runtime,
        events,
        artifact_store=FilesystemArtifactStore(root / "task-artifacts"),
    )
    activity, execution_input = _activity_and_input(run_id)
    context = HarnessGraphActivityTaskContext.for_execution_input(
        activity,
        execution_input,
    )
    return SimpleNamespace(
        database=database,
        events=events,
        runtime=runtime,
        store=store,
        context=context,
        identity=_execution_identity(context),
        artifact_root=root / "task-artifacts",
    )


def _register(fixture: SimpleNamespace, context=None):
    return fixture.store.register_parent_execution_context(
        context or fixture.context,
        execution_identity=fixture.identity,
        stage_id=STAGE_ID,
        stage_binding_checksum=STAGE_BINDING_CHECKSUM,
    )


def _load(
    fixture: SimpleNamespace,
    identity=None,
    *,
    stage_id: str = STAGE_ID,
    binding_checksum=None,
):
    return fixture.store.load_parent_execution_context(
        identity or fixture.identity,
        stage_id=stage_id,
        stage_binding_checksum=binding_checksum or STAGE_BINDING_CHECKSUM,
    )


def test_parent_execution_context_registration_is_idempotent_and_reopens(
    tmp_path,
) -> None:
    fixture = _fixture(tmp_path)

    assert _register(fixture) == fixture.context
    state_before = fixture.events.load_transactional_state(
        TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
        _state_key(fixture.identity),
    )
    assert _register(fixture) == fixture.context
    assert fixture.events.load_transactional_state(
        TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
        _state_key(fixture.identity),
    ) == state_before

    reopened_events = SQLiteEventStore(fixture.database)
    reopened = DurableTaskPlanStore(
        EventRuntime(
            store=reopened_events,
            schema_catalog=default_event_schema_catalog(),
        ),
        reopened_events,
        artifact_store=FilesystemArtifactStore(fixture.artifact_root),
    )
    assert reopened.load_parent_execution_context(
        fixture.identity,
        stage_id=STAGE_ID,
        stage_binding_checksum=STAGE_BINDING_CHECKSUM,
    ) == fixture.context


def test_parent_execution_context_rejects_a_different_checkpoint(
    tmp_path,
) -> None:
    fixture = _fixture(tmp_path)
    _register(fixture)
    conflicting = replace(
        fixture.context,
        graph_checkpoint_ref="graph-state://parent-context-run/different-checkpoint",
    )

    with pytest.raises(HarnessValidationError) as captured:
        _register(fixture, conflicting)

    assert captured.value.code == "task_plan_parent_execution_context_conflict"
    assert _load(fixture) == fixture.context


def test_parent_execution_context_rejects_wrong_graph_and_stage_binding(
    tmp_path,
) -> None:
    fixture = _fixture(tmp_path)
    _register(fixture)
    wrong_identity = replace(
        fixture.identity,
        graph_id="other-parent-graph",
        graph_ref=f"other-parent-graph@{fixture.identity.graph_version}",
    )

    with pytest.raises(HarnessValidationError) as registration_error:
        fixture.store.register_parent_execution_context(
            fixture.context,
            execution_identity=wrong_identity,
            stage_id=STAGE_ID,
            stage_binding_checksum=STAGE_BINDING_CHECKSUM,
        )
    assert (
        registration_error.value.code
        == "task_plan_parent_execution_context_mismatch"
    )

    for identity, stage_id, binding_checksum in (
        (wrong_identity, STAGE_ID, STAGE_BINDING_CHECKSUM),
        (fixture.identity, "other-stage", STAGE_BINDING_CHECKSUM),
        (
            fixture.identity,
            STAGE_ID,
            checksum_for({"stage_binding": "other"}),
        ),
    ):
        with pytest.raises(HarnessValidationError) as read_error:
            _load(
                fixture,
                identity,
                stage_id=stage_id,
                binding_checksum=binding_checksum,
            )
        assert read_error.value.code == "task_plan_parent_execution_context_missing"


def test_parent_execution_context_missing_is_a_typed_error(tmp_path) -> None:
    fixture = _fixture(tmp_path)

    with pytest.raises(HarnessValidationError) as captured:
        _load(fixture)

    assert captured.value.code == "task_plan_parent_execution_context_missing"


def test_parent_execution_context_rejects_corrupt_stored_payload(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _fixture(tmp_path)
    _register(fixture)
    state = fixture.events.load_transactional_state(
        TASK_PLAN_PARENT_CONTEXT_STATE_NAMESPACE,
        _state_key(fixture.identity),
    )
    assert state is not None
    payload = thaw_canonical_json(state.payload)
    payload["context"]["graph_checkpoint_ref"] = (
        "graph-state://parent-context-run/corrupt-checkpoint"
    )
    corrupt = TransactionalStateSnapshot.create(
        namespace=state.namespace,
        key=state.key,
        revision=state.revision,
        payload=payload,
    )
    monkeypatch.setattr(
        fixture.events,
        "load_transactional_state",
        lambda namespace, key: corrupt,
    )

    with pytest.raises(HarnessValidationError) as captured:
        _load(fixture)

    assert captured.value.code == "task_plan_parent_execution_context_corrupt"


def test_parent_execution_context_cas_loser_compares_without_overwrite(
    tmp_path,
    monkeypatch,
) -> None:
    fixture = _fixture(tmp_path)
    competing_context = replace(
        fixture.context,
        graph_checkpoint_ref="graph-state://parent-context-run/accepted-by-racer",
    )
    original_cas = fixture.runtime.compare_and_swap_transactional_state

    def lose_race(next_snapshot, *, expected_revision, expected_checksum):
        competing_payload = thaw_canonical_json(next_snapshot.payload)
        competing_payload["context"] = competing_context.to_dict()
        original_cas(
            TransactionalStateSnapshot.create(
                namespace=next_snapshot.namespace,
                key=next_snapshot.key,
                revision=next_snapshot.revision,
                payload=competing_payload,
            ),
            expected_revision=None,
            expected_checksum=None,
        )
        raise EventStoreContentionError("simulated parent context registration race")

    monkeypatch.setattr(
        fixture.runtime,
        "compare_and_swap_transactional_state",
        lose_race,
    )

    with pytest.raises(HarnessValidationError) as captured:
        _register(fixture)

    assert captured.value.code == "task_plan_parent_execution_context_conflict"
    assert _load(fixture) == competing_context


def test_parent_input_ingress_registers_the_original_graph_task_context(
    tmp_path,
) -> None:
    setup = _setup(tmp_path)

    result = setup.graph_runtime.run(setup.spec)

    assert result.succeeded, result
    snapshot = setup.admission.snapshot
    expected = HarnessGraphActivityTaskContext.from_dict(
        setup.admission.task[HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY]
    )
    assert setup.task_store.load_parent_execution_context(
        snapshot.execution_identity,
        stage_id=snapshot.stage_id,
        stage_binding_checksum=snapshot.stage_binding_checksum,
    ) == expected
    assert expected.graph_checkpoint_ref != ""
