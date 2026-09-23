from __future__ import annotations

import pytest

from framework.harness.subagents import ChildAgentSupervisorError
from infrastructure.storage.events.factory import durable_event_storage_from_env
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
