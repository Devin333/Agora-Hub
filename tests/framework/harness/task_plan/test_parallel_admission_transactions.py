from dataclasses import replace

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.parallel import (
    DispatchWave, ParallelAgentCoordinator, SerialTaskExecutorAdapter, TaskReservation,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from framework.harness.task_plan.store import InMemoryTaskPlanStore, TaskPlanEvent
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    _accepted_plan, _ArtifactStore, _EventStore, _store, _task,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import _request


def _parallel_event(plan, kind, sequence, payload):
    return TaskPlanEvent.for_plan(kind, plan, sequence=sequence, payload={
        "event_type": kind, "parallel_event_idempotency_key": f"{kind}:{sequence}",
        "idempotency_key": str(sequence), **payload,
    })


@pytest.fixture(params=("memory", "durable"))
def admitted(request):
    candidate, plan, _, _ = _accepted_plan((
        _task("a"), _task("b", capability="research.helper", role="analysis.helper"),
    ), two_tasks=True)
    store = InMemoryTaskPlanStore() if request.param == "memory" else _store(_EventStore(), _ArtifactStore())
    store.append_candidate(candidate)
    store.accept_plan(plan)
    coordinator = ParallelAgentCoordinator(max_workers=2, serial_executor=SerialTaskExecutorAdapter())
    group = coordinator.create_group(replace(_request(plan), serial_fallback=True))
    current = store.load_projection(plan.run_id, plan.stage_id)
    event = _parallel_event(plan, "TASK_GROUP_ADMITTED", current.last_sequence + 1, {
        "group": group.to_dict(), "requested_parallelism": 2, "effective_parallelism": 1,
    })
    store.commit_events((event,), (replace(current, last_sequence=event.sequence),), expected_projection_checksum=current.projection_checksum)
    return store, plan, group


def _wave_batch(store, plan, group, index, ordinal):
    initial = store.load_projection(plan.run_id, plan.stage_id)
    instance = _request(plan).task_instances[index]
    ready = replace(TaskPlanScheduler().reserve_ready_tasks(initial, TaskPlanReadyDecision((instance,))), last_sequence=initial.last_sequence + 1)
    wave = DispatchWave(
        group_id=group.group_id, ordinal=ordinal, task_ids=(instance.task_id,),
        effective_parallelism=1, execution_mode="SERIAL", state="ADMITTED",
        reservations=(TaskReservation(instance.task_id, instance.idempotency_key, instance.budget_snapshot.to_dict()),),
    )
    events = (
        TaskPlanEvent.for_plan("TASK_READY", plan, task_id=instance.task_id, task_instance_id=instance.task_instance_id,
                               attempt=1, input_checksum=instance.task_definition_checksum, sequence=ready.last_sequence),
        _parallel_event(plan, "TASK_WAVE_ADMITTED", ready.last_sequence + 1, {
            "group": group.to_dict(), "wave": wave.to_dict(),
            "budget_before_checksum": TaskPlanBudgetLedger.from_snapshot(initial.consumed_budget).to_dict()["ledger_checksum"],
            "budget_after_checksum": TaskPlanBudgetLedger.from_snapshot(ready.consumed_budget).to_dict()["ledger_checksum"],
        }),
    )
    admitted_projection = TaskPlanScheduler.mark_admitted(ready, instance)
    return initial, wave, events, (ready, replace(admitted_projection, last_sequence=events[-1].sequence))


def test_second_active_wave_cannot_commit_even_from_latest_projection(admitted):
    store, plan, group = admitted
    initial, wave, batch, projections = _wave_batch(store, plan, group, 0, 1)
    store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    current, _, competing, proposed = _wave_batch(store, plan, group, 1, 2)
    before = store.read_events(plan.run_id, plan.stage_id)
    with pytest.raises(HarnessValidationError, match="active wave"):
        store.commit_events(competing, proposed, expected_projection_checksum=current.projection_checksum)
    assert store.read_events(plan.run_id, plan.stage_id) == before
    assert store.load_projection(plan.run_id, plan.stage_id) == current
    assert len(TaskPlanBudgetLedger.from_snapshot(current.consumed_budget).records) == 1
    with pytest.raises(HarnessValidationError, match="active wave"):
        TaskPlanReplayReducer().replay((plan,), (*before, *competing), require_terminal_events=False)

    completed = _parallel_event(plan, "TASK_WAVE_COMPLETED", current.last_sequence + 1, {
        "group_id": group.group_id, "wave_id": wave.wave_id, "task_ids": list(wave.task_ids),
        "terminal_outcome": "INDETERMINATE", "reservation_state": "RESERVED",
    })
    store.commit_event(completed, replace(current, last_sequence=completed.sequence))
    current, _, next_batch, next_projections = _wave_batch(store, plan, group, 1, 2)
    store.commit_events(next_batch, next_projections, expected_projection_checksum=current.projection_checksum)
    assert len([event for event in store.read_events(plan.run_id, plan.stage_id) if event.event_type == "TASK_WAVE_ADMITTED"]) == 2


@pytest.mark.parametrize("ordinal", (2, 17))
def test_admission_requires_contiguous_bounded_wave_ordinal(admitted, ordinal):
    store, plan, group = admitted
    initial, _, batch, projections = _wave_batch(store, plan, group, 0, ordinal)
    with pytest.raises(HarnessValidationError, match="ordinal"):
        store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    assert store.load_projection(plan.run_id, plan.stage_id) == initial


def test_alternate_correlation_cannot_admit_second_group_for_accepted_plan(admitted):
    store, plan, group = admitted
    other = replace(group, correlation_id="another-parent-turn")
    assert other.group_id != group.group_id
    current = store.load_projection(plan.run_id, plan.stage_id)
    event = TaskPlanEvent.for_plan("TASK_GROUP_ADMITTED", plan, sequence=current.last_sequence + 1, payload={"group": other.to_dict()})
    with pytest.raises(HarnessValidationError, match="already owns"):
        store.commit_event(event, replace(current, last_sequence=event.sequence))
    assert store.load_projection(plan.run_id, plan.stage_id) == current


def test_group_binding_drift_rejected_before_any_durable_change(admitted):
    store, plan, group = admitted
    altered = replace(group, task_ids=(group.task_ids[0],))
    current = store.load_projection(plan.run_id, plan.stage_id)
    event = TaskPlanEvent.for_plan("TASK_GROUP_ADMITTED", plan, sequence=current.last_sequence + 1, payload={"group": altered.to_dict()})
    with pytest.raises(HarnessValidationError, match="complete accepted plan"):
        store.commit_event(event, replace(current, last_sequence=event.sequence))
    assert store.load_projection(plan.run_id, plan.stage_id) == current


def test_closed_group_cannot_admit_a_new_wave(admitted):
    store, plan, group = admitted
    current = store.load_projection(plan.run_id, plan.stage_id)
    closed = _parallel_event(plan, "TASK_GROUP_FAILED", current.last_sequence + 1, {
        "group": replace(group, state="FAILED").to_dict(), "reason_code": "TASK_FAILED",
    })
    store.commit_event(closed, replace(current, last_sequence=closed.sequence))
    initial, _, batch, projections = _wave_batch(store, plan, group, 0, 1)
    with pytest.raises(HarnessValidationError, match="closed to admission"):
        store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    assert store.load_projection(plan.run_id, plan.stage_id) == initial


def test_direct_append_cannot_bypass_single_active_wave(admitted):
    store, plan, group = admitted
    initial, _, batch, projections = _wave_batch(store, plan, group, 0, 1)
    store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    current, _, second, _ = _wave_batch(store, plan, group, 1, 2)
    admission = replace(second[-1], sequence=current.last_sequence + 1)
    with pytest.raises(HarnessValidationError, match="active wave"):
        store.append_event(admission)
    assert store.load_projection(plan.run_id, plan.stage_id) == current


@pytest.mark.parametrize("writer", ("commit", "append", "batch"))
def test_invalid_wave_completion_is_rejected_before_artifact_or_history_changes(admitted, monkeypatch, writer):
    store, plan, group = admitted
    initial, wave, batch, projections = _wave_batch(store, plan, group, 0, 1)
    store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    current = store.load_projection(plan.run_id, plan.stage_id)
    history = store.read_events(plan.run_id, plan.stage_id)
    event = _parallel_event(plan, "TASK_WAVE_COMPLETED", current.last_sequence + 1, {
        "group_id": group.group_id, "wave_id": wave.wave_id, "task_ids": ["unknown-task"],
        "terminal_outcome": "INDETERMINATE", "reservation_state": "RESERVED",
    })
    if hasattr(store, "_put_projection"):
        monkeypatch.setattr(store, "_put_projection", lambda _projection: pytest.fail("invalid terminal artifact written"))
    with pytest.raises(HarnessValidationError, match="terminal evidence"):
        if writer == "commit":
            store.commit_event(event, replace(current, last_sequence=event.sequence))
        elif writer == "append":
            store.append_event(event)
        else:
            store.append_events((event,))
    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_projection(plan.run_id, plan.stage_id) == current
