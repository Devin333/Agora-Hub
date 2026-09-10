from __future__ import annotations

from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.models import TaskAdmissionOwner
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler, task_instance_for_attempt
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


def _stores():
    candidate, plan = _graph_only_candidate_and_plan()
    memory = InMemoryTaskPlanStore()
    memory.append_candidate(candidate)
    memory.accept_plan(plan)
    events = _EventStore()
    durable = _store(events, _ArtifactStore())
    durable.append_candidate(candidate)
    durable.accept_plan(plan)
    return plan, memory, durable, events


def _transition_batch(store, plan):
    initial = store.load_projection(plan.run_id, plan.stage_id)
    instance = task_instance_for_attempt(plan, plan.tasks[0].task_id, 1)
    scheduler = TaskPlanScheduler()
    ready = replace(
        scheduler.reserve_ready_tasks(
            initial,
            TaskPlanReadyDecision(logical_ready_task_ids=(instance.task_id,)),
        ),
        last_sequence=initial.last_sequence + 1,
    )
    admitted = replace(
        scheduler.mark_admitted(
            ready,
            instance,
            admission_owner=TaskAdmissionOwner.QUEUE,
        ),
        last_sequence=initial.last_sequence + 2,
    )
    dispatched = replace(
        scheduler.mark_dispatched(admitted, instance),
        last_sequence=initial.last_sequence + 3,
    )
    readiness = LogicalTaskReadiness(
        task_id=instance.task_id,
        task_definition_checksum=instance.task_definition_checksum,
        logical_ready_order=(instance.task_id,),
    )
    admission = TaskQueueAdmissionEvidence(
        task_instance=instance,
        budget_before_checksum=TaskPlanBudgetLedger.from_snapshot(
            ready.consumed_budget
        ).to_dict()["ledger_checksum"],
        budget_after_checksum=TaskPlanBudgetLedger.from_snapshot(
            admitted.consumed_budget
        ).to_dict()["ledger_checksum"],
    )
    events = (
        TaskPlanEvent.for_plan(
            "TASK_READY",
            plan,
            task_id=instance.task_id,
            input_checksum=instance.task_definition_checksum,
            payload={"logical_readiness": readiness.to_dict()},
            sequence=initial.last_sequence + 1,
        ),
        TaskPlanEvent.for_plan(
            "TASK_QUEUE_ADMITTED",
            plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            input_checksum=instance.task_definition_checksum,
            payload={"queue_admission": admission.to_dict()},
            sequence=initial.last_sequence + 2,
        ),
        TaskPlanEvent.for_plan(
            "TASK_DISPATCHED",
            plan,
            task_id=instance.task_id,
            task_instance_id=instance.task_instance_id,
            attempt=instance.attempt,
            input_checksum=instance.task_definition_checksum,
            sequence=initial.last_sequence + 3,
        ),
    )
    return initial, instance, events, (ready, admitted, dispatched)


@pytest.mark.parametrize("store_name", ("memory", "durable"))
def test_commit_events_is_exactly_idempotent_after_a_later_transition(store_name):
    plan, memory, durable, _events = _stores()
    store = memory if store_name == "memory" else durable
    initial, instance, batch, projections = _transition_batch(store, plan)

    committed = store.commit_events(
        batch,
        projections,
        expected_projection_checksum=initial.projection_checksum,
    )
    started = replace(
        TaskPlanScheduler.mark_started(projections[-1], instance),
        last_sequence=projections[-1].last_sequence + 1,
    )
    started_event = TaskPlanEvent.for_plan(
        "TASK_STARTED",
        plan,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        input_checksum=instance.task_definition_checksum,
        sequence=started.last_sequence,
    )
    store.commit_events(
        (started_event,),
        (started,),
        expected_projection_checksum=projections[-1].projection_checksum,
    )
    history = store.read_events(plan.run_id, plan.stage_id)

    assert store.commit_events(
        batch,
        projections,
        expected_projection_checksum=initial.projection_checksum,
    ) == committed
    assert store.read_events(plan.run_id, plan.stage_id) == history


@pytest.mark.parametrize("store_name", ("memory", "durable"))
def test_commit_events_rejects_different_projection_and_partial_history(store_name):
    plan, memory, durable, _events = _stores()
    store = memory if store_name == "memory" else durable
    initial, _instance, batch, projections = _transition_batch(store, plan)
    store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)

    conflicting = (
        projections[0],
        replace(projections[0], last_sequence=projections[1].last_sequence),
        projections[2],
    )
    with pytest.raises(HarnessValidationError):
        store.commit_events(batch, conflicting, expected_projection_checksum=initial.projection_checksum)

    plan, memory, durable, _events = _stores()
    store = memory if store_name == "memory" else durable
    initial, _instance, batch, projections = _transition_batch(store, plan)
    store.append_event(batch[0])
    with pytest.raises(HarnessValidationError, match="partially present"):
        store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)


def test_durable_commit_events_keeps_projection_unreachable_when_batch_publish_fails():
    plan, _memory, durable, events = _stores()
    initial, _instance, batch, projections = _transition_batch(durable, plan)
    events.fail_on_event_type = "TASK_DISPATCHED"

    with pytest.raises(RuntimeError, match="injected batch failure"):
        durable.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)

    assert durable.load_projection(plan.run_id, plan.stage_id) == initial
    assert all(
        event.event_type not in {
            "TASK_READY",
            "TASK_QUEUE_ADMITTED",
            "TASK_DISPATCHED",
        }
        for event in events._events
    )


def test_memory_commit_events_rolls_back_event_projection_and_idempotency_index():
    class _FailOnceDict(dict):
        def __init__(self):
            super().__init__()
            self.remaining = 1

        def __setitem__(self, key, value):
            if self.remaining == 0:
                raise RuntimeError("injected projection-index failure")
            self.remaining -= 1
            super().__setitem__(key, value)

    plan, memory, _durable, _events = _stores()
    initial, _instance, batch, projections = _transition_batch(memory, plan)
    memory._transition_projections = _FailOnceDict()

    with pytest.raises(RuntimeError, match="projection-index failure"):
        memory.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)

    assert memory.read_events(plan.run_id, plan.stage_id)[-1].event_type == "PLAN_ACCEPTED"
    assert memory.load_projection(plan.run_id, plan.stage_id) == initial
    assert memory._transition_projections == {}


@pytest.mark.parametrize("missing_index", (0, 1, 2))
def test_durable_batch_retry_never_recreates_missing_historical_projection(missing_index):
    candidate, plan = _graph_only_candidate_and_plan()
    artifacts = _ArtifactStore()
    durable = _store(_EventStore(), artifacts)
    durable.append_candidate(candidate)
    durable.accept_plan(plan)
    initial, _instance, batch, projections = _transition_batch(durable, plan)
    durable.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    digest = projections[missing_index].projection_checksum.removeprefix("sha256:")
    key = next(key for key in artifacts._content if key[1].endswith(f"/projection/{digest}.json"))
    del artifacts._content[key]
    before = dict(artifacts._content)
    with pytest.raises(HarnessValidationError, match="artifact"):
        durable.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    assert artifacts._content == before


@pytest.mark.parametrize("failed_index", (0, 1, 2))
def test_durable_projection_write_failure_preserves_entire_prior_state(monkeypatch, failed_index):
    plan, _memory, durable, events = _stores()
    initial, _instance, batch, projections = _transition_batch(durable, plan)
    original = durable._put_projection
    writes = 0

    def write(projection):
        nonlocal writes
        index = writes
        writes += 1
        if index == failed_index:
            raise RuntimeError("injected artifact failure")
        return original(projection)

    monkeypatch.setattr(durable, "_put_projection", write)
    before = tuple(events._events)
    with pytest.raises(RuntimeError, match="artifact failure"):
        durable.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    assert tuple(events._events) == before
    assert durable.load_projection(plan.run_id, plan.stage_id) == initial
    monkeypatch.setattr(durable, "_put_projection", original)
    durable.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    assert durable.load_projection(plan.run_id, plan.stage_id) == projections[-1]
