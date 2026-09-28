from __future__ import annotations

import pytest

from framework.harness.subagents import ChildAgentSupervisorError
from framework.harness.subagents import ChildAgentSpawnRequest, ChildAgentState
from framework.events.runtime.models import StreamReadRequest
from framework.events.runtime.projection import CanonicalRuntimeEventPublisher
from infrastructure.storage.events.factory import durable_event_storage_from_env
from framework.shared.graph_identity import GraphExecutionIdentity
from interfaces.composition.research_child_runtime import (
    LazyResearchChildRuntime,
    ResearchChildRuntimeUnavailableError,
)


def _close_storage(storage) -> None:
    for resource in (
        storage.activity_store,
        storage.replay_checkpoint_store,
        storage.event_store,
    ):
        close = getattr(resource, "close", None)
        if callable(close):
            close()


def _spawn_request() -> ChildAgentSpawnRequest:
    identity = GraphExecutionIdentity(
        run_id="research-runtime-events",
        graph_id="research",
        graph_version="1.0.0",
        graph_ref="research@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="dynamic-analysis",
        node_instance_id="dynamic-analysis-1",
        activity_id="dynamic-analysis-activity",
        attempt=1,
    )
    return ChildAgentSpawnRequest(
        parent_graph_identity=identity,
        stage_id="dynamic-analysis-stage",
        task_id="paper-analysis",
        task_instance_id="paper-analysis-1",
        attempt=1,
        allowed_tools=("tool.read",),
        allowed_memory_namespaces=("research",),
        budget={"turns": 2},
        operation_id="research-runtime-events-op-1",
        lease_seconds=60,
    )


def test_child_runtime_is_lazy_shared_and_reopens_canonical_sqlite(tmp_path) -> None:
    first_storage = durable_event_storage_from_env(
        artifact_root=tmp_path,
        env={},
    )
    first = LazyResearchChildRuntime(
        state_runtime=first_storage.event_runtime,
        state_reader=first_storage.event_store,
        max_children=3,
    )

    assert first.started is False
    first_binding = first.start_for_run(run_id="research-run-a", tenant_id="tenant-a")
    repeated_binding = first.start_for_run(
        run_id="research-run-b",
        tenant_id="tenant-b",
    )

    assert first.started is True
    assert repeated_binding.supervisor is first_binding.supervisor
    assert repeated_binding.parallel_coordinator is first_binding.parallel_coordinator
    first.close()
    _close_storage(first_storage)

    reopened_storage = durable_event_storage_from_env(
        artifact_root=tmp_path,
        env={},
    )
    reopened = LazyResearchChildRuntime(
        state_runtime=reopened_storage.event_runtime,
        state_reader=reopened_storage.event_store,
        max_children=3,
    )
    try:
        reopened_binding = reopened.start_for_run(
            run_id="research-run-a",
            tenant_id="tenant-a",
        )
        assert reopened_binding.supervisor.capacity == 3
        with pytest.raises(ChildAgentSupervisorError):
            reopened.start_for_run(
                run_id="research-run-a",
                tenant_id="tenant-drifted",
            )
    finally:
        reopened.close()
        _close_storage(reopened_storage)


def test_child_runtime_publishes_canonical_lifecycle_and_recovers_durable_state(
    tmp_path,
) -> None:
    first_storage = durable_event_storage_from_env(artifact_root=tmp_path, env={})
    first = LazyResearchChildRuntime(
        state_runtime=first_storage.event_runtime,
        state_reader=first_storage.event_store,
        max_children=1,
        runtime_event_sink=CanonicalRuntimeEventPublisher(
            first_storage.event_runtime
        ),
    )

    handle = None
    try:
        binding = first.start_for_run(
            run_id="research-runtime-events",
            tenant_id="tenant-runtime-events",
        )
        handle = binding.supervisor.spawn(
            _spawn_request(),
            worker={"candidate": "accepted"},
        )
        result = binding.supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
            timeout_seconds=2,
        )
        assert result.receipt is not None
        assert result.receipt.status is ChildAgentState.SUCCEEDED

        page = first_storage.event_store.read_stream(
            StreamReadRequest("research-runtime-events")
        )
        assert {event.event_type for event in page.events} >= {
            "child_spawned",
            "child_terminal",
        }
        assert all(event.data_schema == "newsroom.runtime-event/v1" for event in page.events)
    finally:
        first.close()
        _close_storage(first_storage)

    assert handle is not None
    reopened_storage = durable_event_storage_from_env(artifact_root=tmp_path, env={})
    reopened = LazyResearchChildRuntime(
        state_runtime=reopened_storage.event_runtime,
        state_reader=reopened_storage.event_store,
        max_children=1,
        runtime_event_sink=CanonicalRuntimeEventPublisher(
            reopened_storage.event_runtime
        ),
    )
    try:
        reopened_binding = reopened.start_for_run(
            run_id="research-runtime-events",
            tenant_id="tenant-runtime-events",
        )
        recovered = reopened_binding.supervisor.wait(
            handle.child_id,
            operation_id=handle.operation_id,
            timeout_seconds=0,
        )
        assert recovered.receipt is not None
        assert recovered.receipt.status is ChildAgentState.SUCCEEDED
        reopened_page = reopened_storage.event_store.read_stream(
            StreamReadRequest("research-runtime-events")
        )
        assert [event.event_type for event in reopened_page.events].count(
            "child_spawned"
        ) == 1
    finally:
        reopened.close()
        _close_storage(reopened_storage)


def test_static_runtimes_do_not_claim_owner_until_dynamic_admission(tmp_path) -> None:
    first_storage = durable_event_storage_from_env(artifact_root=tmp_path, env={})
    second_storage = durable_event_storage_from_env(artifact_root=tmp_path, env={})
    first = LazyResearchChildRuntime(
        state_runtime=first_storage.event_runtime,
        state_reader=first_storage.event_store,
        max_children=1,
    )
    second = LazyResearchChildRuntime(
        state_runtime=second_storage.event_runtime,
        state_reader=second_storage.event_store,
        max_children=1,
    )

    assert first.started is False
    assert second.started is False
    first.start_for_run(run_id="research-run-a", tenant_id="tenant-a")
    with pytest.raises(ChildAgentSupervisorError):
        second.start_for_run(run_id="research-run-b", tenant_id="tenant-b")
    assert second.started is False

    first.close()
    try:
        second.start_for_run(run_id="research-run-b", tenant_id="tenant-b")
        assert second.started is True
    finally:
        second.close()
        _close_storage(second_storage)
        _close_storage(first_storage)


def test_child_runtime_missing_canonical_state_ports_fails_only_on_dynamic_start() -> None:
    runtime = LazyResearchChildRuntime(
        state_runtime=object(),
        state_reader=object(),
        max_children=1,
    )

    assert runtime.started is False
    with pytest.raises(ResearchChildRuntimeUnavailableError):
        runtime.start_for_run(run_id="research-run", tenant_id="tenant-a")
    assert runtime.started is False
    runtime.close()


@pytest.mark.parametrize(
    ("run_id", "tenant_id"),
    (
        (None, "tenant-a"),
        (42, "tenant-a"),
        ("research-run", None),
        ("research-run", object()),
        (" ", "tenant-a"),
        ("research-run", " "),
    ),
)
def test_child_runtime_rejects_non_string_or_blank_scope_before_start(
    run_id,
    tenant_id,
) -> None:
    runtime = LazyResearchChildRuntime(
        state_runtime=object(),
        state_reader=object(),
        max_children=1,
    )

    with pytest.raises(ValueError):
        runtime.start_for_run(run_id=run_id, tenant_id=tenant_id)
    assert runtime.started is False
    runtime.close()
