from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskAdmissionOwner, TaskPlanProjection
from framework.harness.task_plan.scheduler import (
    TaskPlanReadyDecision,
    TaskPlanScheduler,
    task_instance_for_attempt,
)
from framework.harness.task_plan.store import (
    InMemoryTaskPlanStore,
    LogicalTaskReadiness,
    TaskPlanEvent,
    TaskQueueAdmissionEvidence,
)
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _ArtifactStore,
    _EventStore,
    _graph_only_candidate_and_plan,
    _store,
)


_BATCH_EVENT_TYPES = frozenset(
    {"TASK_READY", "TASK_QUEUE_ADMITTED", "TASK_DISPATCHED", "TASK_STARTED"}
)


def _batch(store, plan, *, sequence: int) -> tuple[TaskPlanEvent, ...]:
    scheduler = TaskPlanScheduler()
    projection = store.load_projection(plan.run_id, plan.stage_id)
    task = plan.tasks[0]
    ready_order = (task.task_id,)
    ready_projection = scheduler.reserve_ready_tasks(
        projection,
        TaskPlanReadyDecision(logical_ready_task_ids=ready_order),
    )
    state = next(item for item in projection.tasks if item.task_id == task.task_id)
    instance = task_instance_for_attempt(plan, task.task_id, state.attempts + 1)
    admitted_projection = scheduler.mark_admitted(
        ready_projection,
        instance,
        admission_owner=TaskAdmissionOwner.QUEUE,
    )
    readiness = LogicalTaskReadiness(
        task_id=task.task_id,
        task_definition_checksum=task.task_definition_checksum,
        logical_ready_order=ready_order,
    )
    admission = TaskQueueAdmissionEvidence(
        task_instance=instance,
        budget_before_checksum=TaskPlanBudgetLedger.from_snapshot(
            ready_projection.consumed_budget
        ).to_dict()["ledger_checksum"],
        budget_after_checksum=TaskPlanBudgetLedger.from_snapshot(
            admitted_projection.consumed_budget
        ).to_dict()["ledger_checksum"],
    )
    return (
        TaskPlanEvent.for_plan(
            "TASK_READY",
            plan,
            task_id=task.task_id,
            input_checksum=task.task_definition_checksum,
            payload={"logical_readiness": readiness.to_dict()},
            sequence=sequence,
        ),
        TaskPlanEvent.for_plan(
            "TASK_QUEUE_ADMITTED",
            plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            input_checksum=instance.task_definition_checksum,
            payload={"queue_admission": admission.to_dict()},
            sequence=sequence + 1,
        ),
        *(
            TaskPlanEvent.for_plan(
                event_type,
                plan,
                task_id=instance.task_id,
                task_instance_id=instance.task_instance_id,
                attempt=instance.attempt,
                input_checksum=instance.task_definition_checksum,
                sequence=sequence + offset,
            )
            for offset, event_type in enumerate(
                ("TASK_DISPATCHED", "TASK_STARTED"),
                start=2,
            )
        ),
    )


def _in_memory_plan_store():
    candidate, plan = _graph_only_candidate_and_plan()
    store = InMemoryTaskPlanStore()
    store.append_candidate(candidate)
    store.accept_plan(plan)
    return store, plan


def _durable_plan_store(*, fail_on_event_type: str | None = None):
    candidate, plan = _graph_only_candidate_and_plan()
    event_store = _EventStore(fail_on_event_type=fail_on_event_type)
    store = _store(event_store, _ArtifactStore())
    store.append_candidate(candidate)
    store.accept_plan(plan)
    return store, plan, event_store


def test_in_memory_batch_is_atomic_and_full_redelivery_is_idempotent() -> None:
    store, plan = _in_memory_plan_store()
    batch = _batch(
        store,
        plan,
        sequence=len(store.read_events(plan.run_id, plan.stage_id)) + 1,
    )

    checksums = store.append_events(batch)
    before = store.read_events(plan.run_id, plan.stage_id)

    assert checksums == tuple(event.event_checksum for event in batch)
    assert store.append_events(batch) == checksums
    assert store.read_events(plan.run_id, plan.stage_id) == before
    assert store.load_projection(plan.run_id, plan.stage_id).last_sequence == batch[-1].sequence


@pytest.mark.parametrize("failure_index", (1, 2, 3))
def test_durable_batch_failure_never_exposes_partial_events_or_projection(
    failure_index: int,
) -> None:
    event_types = (
        "TASK_READY",
        "TASK_QUEUE_ADMITTED",
        "TASK_DISPATCHED",
        "TASK_STARTED",
    )
    store, plan, event_store = _durable_plan_store(
        fail_on_event_type=event_types[failure_index]
    )
    batch = _batch(
        store,
        plan,
        sequence=len(store.read_events(plan.run_id, plan.stage_id)) + 1,
    )
    before = store.read_events(plan.run_id, plan.stage_id)
    visible_projection = store.load_projection(plan.run_id, plan.stage_id)

    with pytest.raises(RuntimeError, match="injected batch failure"):
        store.append_events(batch)

    assert store.read_events(plan.run_id, plan.stage_id) == before
    assert store.load_projection(plan.run_id, plan.stage_id) == visible_projection
    assert event_store.get_stream_high_watermark(f"run:{plan.run_id}") == before[-1].sequence


def test_durable_batch_pins_a_projection_for_every_committed_prefix() -> None:
    store, plan, event_store = _durable_plan_store()
    batch = _batch(
        store,
        plan,
        sequence=len(store.read_events(plan.run_id, plan.stage_id)) + 1,
    )

    checksums = store.append_events(batch)

    durable_batch = tuple(
        item
        for item in event_store._events
        if item.event_type in _BATCH_EVENT_TYPES
    )
    assert checksums == tuple(event.event_checksum for event in batch)
    assert len(durable_batch) == len(batch)
    for stored, event in zip(durable_batch, batch, strict=True):
        reference = store._reference_from_event(stored, "projection")
        assert reference is not None
        projection = store._read_reference(reference, TaskPlanProjection)
        assert projection.last_sequence == event.sequence
    assert store.load_projection(plan.run_id, plan.stage_id).last_sequence == batch[-1].sequence
    assert store.append_events(batch) == checksums
    assert len(
        [item for item in event_store._events if item.event_type in _BATCH_EVENT_TYPES]
    ) == len(batch)


@pytest.mark.parametrize("factory", (_in_memory_plan_store,))
def test_in_memory_batch_rejects_cross_scope_bad_order_and_partial_history(factory) -> None:
    store, plan = factory()
    batch = _batch(
        store,
        plan,
        sequence=len(store.read_events(plan.run_id, plan.stage_id)) + 1,
    )
    before = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(HarnessValidationError) as scope_error:
        store.append_events(
            (
                batch[0],
                batch[1],
                replace(batch[2], plan_version=plan.version + 1),
            )
        )
    assert scope_error.value.code == "task_plan_event_scope_mismatch"

    with pytest.raises(HarnessValidationError) as sequence_error:
        store.append_events((batch[1], batch[0]))
    assert sequence_error.value.code == "task_plan_sequence_conflict"

    store.append_event(batch[0])
    with pytest.raises(HarnessValidationError) as history_error:
        store.append_events(batch)
    assert history_error.value.code == "task_plan_event_history_conflict"
    assert store.read_events(plan.run_id, plan.stage_id) == (*before, batch[0])


def test_durable_batch_rejects_partial_history_before_visible_mutation() -> None:
    store, plan, _event_store = _durable_plan_store()
    batch = _batch(
        store,
        plan,
        sequence=len(store.read_events(plan.run_id, plan.stage_id)) + 1,
    )
    store.append_event(batch[0])
    before = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(HarnessValidationError) as error:
        store.append_events(batch)

    assert error.value.code == "task_plan_event_history_conflict"
    assert store.read_events(plan.run_id, plan.stage_id) == before


def test_durable_batch_rejects_cross_scope_and_bad_order_before_publish() -> None:
    store, plan, event_store = _durable_plan_store()
    batch = _batch(
        store,
        plan,
        sequence=len(store.read_events(plan.run_id, plan.stage_id)) + 1,
    )
    before = tuple(event_store._events)

    with pytest.raises(HarnessValidationError) as scope_error:
        store.append_events(
            (
                batch[0],
                batch[1],
                replace(batch[2], plan_version=plan.version + 1),
            )
        )
    assert scope_error.value.code == "task_plan_event_scope_mismatch"

    with pytest.raises(HarnessValidationError) as sequence_error:
        store.append_events((batch[1], batch[0]))
    assert sequence_error.value.code == "task_plan_sequence_conflict"
    assert tuple(event_store._events) == before
