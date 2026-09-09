from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import thaw_mapping
from framework.harness.task_plan.parallel import (
    DispatchGroup, DispatchWave, ParallelAgentCoordinator, SerialTaskExecutorAdapter, TaskReservation,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.scheduler import TaskPlanReadyDecision, TaskPlanScheduler
from framework.harness.task_plan.store import InMemoryTaskPlanStore, TaskPlanEvent
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.framework.harness.task_plan.test_durable_task_plan_store import (
    FIXED_NOW, _accepted_plan, _ArtifactStore, _EventStore, _runtime, _store, _task,
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
    ), two_tasks=True, explicit_execution_budget=True)
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


class _RacingAdmissionRuntime:
    """Hold both writers at publication after validating the same prefix."""

    def __init__(self, event_store, barrier):
        self.delegate = _runtime(event_store)
        self.barrier = barrier
        self.publications = 0

    def _arrive(self):
        self.publications += 1
        if self.publications == 1:
            self.barrier.wait(timeout=10)

    def publish(self, request, **kwargs):
        self._arrive()
        return self.delegate.publish(request, **kwargs)

    def publish_batch(self, requests, **kwargs):
        self._arrive()
        return self.delegate.publish_batch(requests, **kwargs)


def _durable_admission_setup(tmp_path, *, backend="sqlite"):
    candidate, plan, _, _ = _accepted_plan((
        _task("a"), _task("b", capability="research.helper", role="analysis.helper"),
    ), two_tasks=True, explicit_execution_budget=True)
    events = SQLiteEventStore(tmp_path / "admission.sqlite3", clock=lambda: FIXED_NOW) if backend == "sqlite" else _EventStore()
    artifacts = _ArtifactStore()
    store = _store(events, artifacts)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    coordinator = ParallelAgentCoordinator(max_workers=2, serial_executor=SerialTaskExecutorAdapter())
    group = coordinator.create_group(replace(_request(plan), serial_fallback=True))
    initial = store.load_projection(plan.run_id, plan.stage_id)
    event = _parallel_event(plan, "TASK_GROUP_ADMITTED", initial.last_sequence + 1, {
        "group": group.to_dict(), "requested_parallelism": 2, "effective_parallelism": 1,
    })
    projection = replace(initial, last_sequence=event.sequence)
    return store, events, artifacts, plan, group, initial, event, projection


@pytest.mark.parametrize("admission_kind", ("group", "wave"))
@pytest.mark.parametrize("identical", (True, False))
@pytest.mark.parametrize("backend", ("sqlite", "fixture"))
def test_durable_concurrent_admissions_commit_once(tmp_path, admission_kind, identical, backend):
    store, events, artifacts, plan, group, initial, event, projection = _durable_admission_setup(tmp_path, backend=backend)
    if admission_kind == "group":
        other = event if identical else replace(event, payload={
            **event.payload, "group": replace(group, correlation_id="competing-parent").to_dict(),
        })
        proposals = (((event,), (projection,)), ((other,), (projection,)))
    else:
        store.commit_event(event, projection)
        initial, _, batch, projections = _wave_batch(store, plan, group, 0, 1)
        _, _, other_batch, other_projections = _wave_batch(store, plan, group, 1, 1)
        proposals = ((batch, projections), (batch, projections) if identical else (other_batch, other_projections))
    before = store.read_events(plan.run_id, plan.stage_id)
    barrier = Barrier(2)
    backends = [SQLiteEventStore(events.database, clock=lambda: FIXED_NOW) if backend == "sqlite" else events for _ in range(2)]
    runtimes = [_RacingAdmissionRuntime(backend, barrier) for backend in backends]
    writers = [_store(backend, artifacts, runtime=runtime) for backend, runtime in zip(backends, runtimes, strict=True)]

    def commit(index):
        batch, projections = proposals[index]
        try:
            return writers[index].commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
        except HarnessValidationError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(commit, range(2)))
    assert all(runtime.publications >= 1 for runtime in runtimes)
    failures = [item for item in outcomes if isinstance(item, HarnessValidationError)]
    assert len(failures) == (0 if identical else 1)
    assert all(item.code == "task_plan_sequence_conflict" for item in failures)
    winner = next(index for index, item in enumerate(outcomes) if not isinstance(item, HarnessValidationError))
    winning_batch, winning_projections = proposals[winner]
    reopened_backend = SQLiteEventStore(events.database, clock=lambda: FIXED_NOW) if backend == "sqlite" else events
    reopened = _store(reopened_backend, artifacts)
    assert reopened.read_events(plan.run_id, plan.stage_id) == (*before, *winning_batch)
    current = reopened.load_projection(plan.run_id, plan.stage_id)
    assert current == winning_projections[-1]
    ledger = TaskPlanBudgetLedger.from_snapshot(current.consumed_budget)
    assert len(ledger.records) == (0 if admission_kind == "group" else 1)
    if admission_kind == "wave":
        winner_instance = _request(plan).task_instances[0 if identical else winner]
        assert set(ledger.records) == {winner_instance.idempotency_key}


def test_reopened_store_redelivers_group_and_wave_without_rewriting_later_state(tmp_path):
    store, events, artifacts, plan, group, initial, event, projection = _durable_admission_setup(tmp_path)
    store.commit_event(event, projection)
    wave_initial, wave, batch, projections = _wave_batch(store, plan, group, 0, 1)
    store.commit_events(batch, projections, expected_projection_checksum=wave_initial.projection_checksum)
    current = store.load_projection(plan.run_id, plan.stage_id)
    completed = _parallel_event(plan, "TASK_WAVE_COMPLETED", current.last_sequence + 1, {
        "group_id": group.group_id, "wave_id": wave.wave_id, "task_ids": list(wave.task_ids),
        "terminal_outcome": "INDETERMINATE", "reservation_state": "RESERVED",
    })
    store.commit_event(completed, replace(current, last_sequence=completed.sequence))
    before = store.read_events(plan.run_id, plan.stage_id)
    current = store.load_projection(plan.run_id, plan.stage_id)
    immutable_artifacts = dict(artifacts._content)
    reopened = _store(SQLiteEventStore(events.database, clock=lambda: FIXED_NOW), artifacts)
    assert reopened.commit_events((event,), (projection,), expected_projection_checksum=initial.projection_checksum) == (event.event_checksum,)
    assert reopened.commit_events(batch, projections, expected_projection_checksum=wave_initial.projection_checksum) == tuple(item.event_checksum for item in batch)
    assert reopened.read_events(plan.run_id, plan.stage_id) == before
    assert reopened.load_projection(plan.run_id, plan.stage_id) == current
    assert artifacts._content == immutable_artifacts
    assert len(TaskPlanBudgetLedger.from_snapshot(current.consumed_budget).records) == 1


@pytest.mark.parametrize("drift", ("membership", "policy", "budget", "join", "parallelism", "waves", "parent"))
def test_wave_cannot_repin_admitted_group_even_with_recomputed_checksums(admitted, drift):
    store, plan, group = admitted
    changes = {
        "membership": {"task_ids": (group.task_ids[0],)},
        "policy": {"admission_policy_checksum": "sha256:" + "a" * 64},
        "budget": {"budget_envelope": {**group.budget_envelope, "max_tool_calls": group.budget_envelope["max_tool_calls"] + 1}},
        "join": {"join_policy": "fail_fast"},
        "parallelism": {"max_parallelism": group.max_parallelism + 1},
        "waves": {"max_waves": group.max_waves + 1},
        "parent": {"parent_graph_identity": replace(group.parent_graph_identity, activity_id="different-parent")},
    }
    altered = replace(group, **changes[drift])
    assert altered.group_checksum != group.group_checksum
    initial, _, batch, projections = _wave_batch(store, plan, altered, 0, 1)
    history = store.read_events(plan.run_id, plan.stage_id)
    with pytest.raises(HarnessValidationError) as exc:
        store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    assert exc.value.code in {"TASK_GROUP_SCOPE_MISMATCH", "TASK_GROUP_ADMISSION_CONFLICT"}
    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_projection(plan.run_id, plan.stage_id) == initial
    assert not TaskPlanBudgetLedger.from_snapshot(initial.consumed_budget).records


def test_coordinator_restores_reopened_admission_and_reservations_without_live_calls(tmp_path):
    store, events, artifacts, plan, group, _, event, projection = _durable_admission_setup(tmp_path)
    store.commit_event(event, projection)
    initial, wave, batch, projections = _wave_batch(store, plan, group, 0, 1)
    store.commit_events(batch, projections, expected_projection_checksum=initial.projection_checksum)
    reopened = _store(SQLiteEventStore(events.database, clock=lambda: FIXED_NOW), artifacts)
    history = reopened.read_events(plan.run_id, plan.stage_id)
    report = TaskPlanReplayReducer().replay((reopened.plan(plan.run_id, plan.stage_id),), history, require_terminal_events=False)
    recorded_group = DispatchGroup.from_dict(report.parallel_groups[group.group_id])
    recorded_waves = tuple(DispatchWave.from_dict(thaw_mapping(item)) for item in report.parallel_waves.values())
    assert recorded_group.group_checksum == group.group_checksum
    assert recorded_waves == (wave,)
    assert report.projection == reopened.load_projection(plan.run_id, plan.stage_id)

    def unexpected_event(_event):
        pytest.fail("restoring admission must not publish or start execution")

    class NoExecution:
        calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            pytest.fail("restoring admission must not execute a child")

    executor = NoExecution()
    coordinator = ParallelAgentCoordinator(max_workers=2, event_sink=unexpected_event, serial_executor=executor)
    request = replace(_request(plan), serial_fallback=True, task_instances=(),
                      available_concurrency_reservations=0, budget_snapshot=report.projection.consumed_budget)
    assert coordinator.restore_group(request, recorded_group, recorded_waves) == recorded_group
    assert coordinator.create_group(request) == recorded_group
    assert coordinator.restore_group(request, recorded_group, recorded_waves) == recorded_group
    session = coordinator._sessions[group.group_id]
    assert session.next_wave_ordinal == 2
    assert session.reserved == set(wave.task_ids)
    assert tuple(item.idempotency_key for item in session.wave_instances[wave.wave_id]) == tuple(item.idempotency_key for item in wave.reservations)
    assert reopened.read_events(plan.run_id, plan.stage_id) == history
    assert reopened.load_projection(plan.run_id, plan.stage_id) == report.projection
    assert executor.calls == 0
