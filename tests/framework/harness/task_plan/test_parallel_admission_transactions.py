from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from framework.events.errors import EventContractError
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.task_plan.budget_ledger import TaskPlanBudgetLedger
from framework.harness.task_plan.canonical import thaw_mapping
from framework.harness.task_plan.capacity import (
    CapacityPool,
    CapacityScopeSnapshot,
    FirstFitPacking,
    TaskCapacityDemand,
    pack_first_fit,
)
from framework.harness.task_plan.attempt_history import (
    TaskAttemptHistoryRecord,
    TaskAttemptOutcome,
)
from framework.harness.task_plan.identity import TaskPlanStageIdentity
from framework.harness.task_plan.parallel_lifecycle import ReservationState
from framework.harness.task_plan.models import (
    PlanCandidate,
    TaskAdmissionOwner,
    TaskLifecycle,
)
from framework.harness.task_plan.parallel import (
    DispatchGroup, DispatchWave, ParallelAgentCoordinator, SerialTaskExecutorAdapter, TaskReservation,
)
from framework.harness.task_plan.replay import TaskPlanReplayReducer
from framework.harness.task_plan.scheduler import (
    TaskPlanReadyDecision,
    TaskPlanScheduler,
    task_instance_for_attempt,
)
from framework.harness.task_plan.store import (
    InMemoryTaskPlanStore,
    LogicalTaskReadiness,
    TaskPlanEvent,
)
from framework.harness.task_plan.validation import (
    TaskPlanValidationContext,
    TaskPlanValidator,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore
from tests.fixtures.task_plan import build_task_plan_stage_binding
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
    state = next(item for item in initial.tasks if item.task_id == instance.task_id)
    if state.status is TaskLifecycle.PENDING:
        target_order = tuple((*initial.logical_ready_order, instance.task_id))
        ready = replace(
            TaskPlanScheduler().reserve_ready_tasks(
                initial,
                TaskPlanReadyDecision(logical_ready_task_ids=target_order),
            ),
            last_sequence=initial.last_sequence + 1,
        )
        readiness = LogicalTaskReadiness(
            task_id=instance.task_id,
            task_definition_checksum=instance.task_definition_checksum,
            logical_ready_order=target_order,
        )
        ready_event = TaskPlanEvent.for_plan(
            "TASK_READY",
            plan,
            task_id=instance.task_id,
            input_checksum=instance.task_definition_checksum,
            payload={"logical_readiness": readiness.to_dict()},
            sequence=ready.last_sequence,
        )
        store.commit_event(ready_event, ready)
        initial = ready
    ledger_before = TaskPlanBudgetLedger.from_snapshot(initial.consumed_budget)
    admitted_projection = TaskPlanScheduler.admit_ready_tasks(
        initial,
        (instance,),
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    ledger_after = TaskPlanBudgetLedger.from_snapshot(
        admitted_projection.consumed_budget
    )
    overflow = tuple(
        task_id
        for task_id in initial.logical_ready_order
        if task_id != instance.task_id
    )
    packing = FirstFitPacking(
        ready_order=initial.logical_ready_order,
        selected=(instance.task_id,),
        overflow=overflow,
        reservations=(),
        reasons={task_id: "CAPACITY_NOT_AVAILABLE" for task_id in overflow},
        budget_before_checksum=ledger_before.to_dict()["ledger_checksum"],
        budget_after_checksum=ledger_after.to_dict()["ledger_checksum"],
        admitted_budget_snapshot=admitted_projection.consumed_budget,
    )
    wave = DispatchWave(
        group_id=group.group_id, ordinal=ordinal, task_ids=(instance.task_id,),
        effective_parallelism=1, execution_mode="SERIAL", state="ADMITTED",
        reservations=(TaskReservation(instance.task_id, instance.idempotency_key, instance.budget_snapshot.to_dict()),),
        packing=packing,
    )
    events = (
        _parallel_event(plan, "TASK_WAVE_ADMITTED", initial.last_sequence + 1, {
            "group": group.to_dict(), "wave": wave.to_dict(),
            "budget_before_checksum": ledger_before.to_dict()["ledger_checksum"],
            "budget_after_checksum": ledger_after.to_dict()["ledger_checksum"],
            "packing_checksum": packing.packing_checksum,
        }),
    )
    return initial, wave, events, (
        replace(admitted_projection, last_sequence=events[-1].sequence),
    )


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
        "terminal_outcome": "INDETERMINATE",
        "reservation_states": {
            task_id: ReservationState.RESERVED.value for task_id in wave.task_ids
        },
        "child_states": {
            task_id: TaskLifecycle.ADMITTED.value for task_id in wave.task_ids
        },
    })
    store.commit_event(completed, replace(current, last_sequence=completed.sequence))
    current, _, next_batch, next_projections = _wave_batch(store, plan, group, 1, 2)
    store.commit_events(next_batch, next_projections, expected_projection_checksum=current.projection_checksum)
    assert len([event for event in store.read_events(plan.run_id, plan.stage_id) if event.event_type == "TASK_WAVE_ADMITTED"]) == 2


def test_replay_accepts_canonical_frozen_wave_task_order(admitted):
    store, plan, group = admitted
    initial, wave, batch, projections = _wave_batch(store, plan, group, 0, 1)
    store.commit_events(
        batch,
        projections,
        expected_projection_checksum=initial.projection_checksum,
    )
    history = store.read_events(plan.run_id, plan.stage_id)
    admitted_event = next(
        event for event in history if event.event_type == "TASK_WAVE_ADMITTED"
    )

    # TaskPlanEvent canonicalization freezes JSON arrays as tuples.  Replay
    # must parse the typed wave rather than requiring the pre-freeze list type.
    assert isinstance(admitted_event.payload["wave"]["task_ids"], tuple)
    report = TaskPlanReplayReducer().replay(
        (plan,),
        history,
        require_terminal_events=False,
    )

    assert tuple(report.parallel_waves[wave.wave_id]["task_ids"]) == wave.task_ids


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
        "terminal_outcome": "INDETERMINATE",
        "reservation_states": {"unknown-task": ReservationState.RESERVED.value},
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

    def publish_batch_with_state_cas(self, requests, **kwargs):
        self._arrive()
        return self.delegate.publish_batch_with_state_cas(requests, **kwargs)

    def compare_and_swap_transactional_state(self, next_snapshot, **kwargs):
        return self.delegate.compare_and_swap_transactional_state(
            next_snapshot,
            **kwargs,
        )


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
        # Install the complete logical READY prefix first so both competing
        # wave proposals are built from the same projection CAS predecessor.
        # _wave_batch records a missing READY fact as part of fixture setup.
        _wave_batch(store, plan, group, 0, 1)
        _wave_batch(store, plan, group, 1, 1)
        initial, _, batch, projections = _wave_batch(store, plan, group, 0, 1)
        other_initial, _, other_batch, other_projections = _wave_batch(
            store,
            plan,
            group,
            1,
            1,
        )
        assert other_initial == initial
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
        "terminal_outcome": "INDETERMINATE",
        "reservation_states": {
            task_id: ReservationState.RESERVED.value for task_id in wave.task_ids
        },
        "child_states": {
            task_id: TaskLifecycle.ADMITTED.value for task_id in wave.task_ids
        },
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


def _capacity_admission_case(store, plan, group):
    initial, _, _, _ = _wave_batch(store, plan, group, 0, 1)
    instance = _request(plan).task_instances[0]
    pool = CapacityPool(
        "worker",
        1,
        owner_scope="shared-capacity",
        reservation_key="worker-pool",
        reservation_version=1,
        expires_at_ms=9_999_999_999_999,
    )
    baseline = CapacityScopeSnapshot(
        owner_scope=pool.owner_scope,
        pools=(pool,),
        revision=1,
        expires_at_ms=pool.expires_at_ms,
    )
    store.install_capacity_snapshot(baseline)
    packing = pack_first_fit(
        (instance.task_id,),
        {instance.task_id: TaskCapacityDemand(instance.task_id, {pool.pool_id: 1})},
        {pool.pool_id: pool},
        max_tasks=1,
        owner_scope=pool.owner_scope,
        reservation_keys={instance.task_id: instance.idempotency_key},
        task_instances={instance.task_id: instance},
        budget_snapshot=initial.consumed_budget,
        capacity_snapshot=baseline,
        now_ms=1,
    )
    admitted_projection = TaskPlanScheduler.admit_ready_tasks(
        initial,
        (instance,),
        admission_owner=TaskAdmissionOwner.GROUP_WAVE,
    )
    wave = DispatchWave(
        group_id=group.group_id,
        ordinal=1,
        task_ids=(instance.task_id,),
        effective_parallelism=1,
        execution_mode="SERIAL",
        state="ADMITTED",
        reservations=(TaskReservation(
            instance.task_id,
            instance.idempotency_key,
            instance.budget_snapshot.to_dict(),
            capacity_allocations=packing.reservations[0].allocations,
            capacity_policy_checksums=packing.reservations[0].policy_checksums,
            capacity_reservation=packing.reservations[0],
        ),),
        packing=packing,
    )
    admission = _parallel_event(
        plan,
        "TASK_WAVE_ADMITTED",
        initial.last_sequence + 1,
        {
            "group": group.to_dict(),
            "wave": wave.to_dict(),
            "budget_before_checksum": packing.budget_before_checksum,
            "budget_after_checksum": packing.budget_after_checksum,
            "packing_checksum": packing.packing_checksum,
        },
    )
    admitted_projection = replace(
        admitted_projection,
        last_sequence=admission.sequence,
    )
    return admission, admitted_projection, baseline, packing, wave, instance, initial


def _capacity_completion_case(store, plan, group):
    (
        admission,
        admitted_projection,
        baseline,
        packing,
        wave,
        instance,
        initial,
    ) = _capacity_admission_case(store, plan, group)
    store.commit_wave_admission(
        (admission,),
        (admitted_projection,),
        expected_projection_checksum=initial.projection_checksum,
        expected_capacity_revision=baseline.revision,
        capacity_scope=baseline.owner_scope,
        capacity_before_checksum=baseline.snapshot_checksum,
        capacity_after=packing.capacity_after,
        pool_reservations=packing.reservations,
    )
    definition = next(item for item in plan.tasks if item.task_id == instance.task_id)
    history_record = TaskAttemptHistoryRecord(
        instance,
        definition.binding_checksum,
        TaskAttemptOutcome.RECLAIMED,
        group=group,
        wave=wave,
        reason_code="reclaimed",
    )
    history_event = _parallel_event(
        plan,
        "TASK_ATTEMPT_RECORDED",
        admission.sequence + 1,
        {
            "group_id": group.group_id,
            "wave_id": wave.wave_id,
            "task_id": instance.task_id,
            "task_instance_id": instance.task_instance_id,
            "attempt": instance.attempt,
            "history_record": history_record.to_dict(),
        },
    )
    after_history = replace(admitted_projection, last_sequence=history_event.sequence)
    store.commit_event(history_event, after_history)
    settled = packing.reservations[0].settled(
        ReservationState.RELEASED,
        reservation_key=instance.idempotency_key,
        expected_version=1,
    )
    before_release = packing.capacity_after
    released_pool = replace(
        before_release.pools[0],
        reserved=0,
        reservation_version=before_release.pools[0].reservation_version + 1,
    )
    after_release = before_release.with_reserved_pools((released_pool,))
    completion = _parallel_event(
        plan,
        "TASK_WAVE_COMPLETED",
        history_event.sequence + 1,
        {
            "group_id": group.group_id,
            "wave_id": wave.wave_id,
            "task_ids": list(wave.task_ids),
            "reservation_states": {instance.task_id: "RELEASED"},
            "child_states": {instance.task_id: TaskLifecycle.FAILED.value},
            "terminal_outcome": "RECLAIMED",
            "capacity_before": before_release.to_dict(),
            "capacity_after": after_release.to_dict(),
        },
    )
    completion_projection = replace(after_history, last_sequence=completion.sequence)
    return (
        completion,
        completion_projection,
        before_release,
        after_release,
        settled,
    )


def test_public_commit_events_cannot_bypass_capacity_admission_cas(admitted):
    store, plan, group = admitted
    admission, projection, before, packing, _wave, _instance, initial = (
        _capacity_admission_case(store, plan, group)
    )
    history = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(HarnessValidationError, match="atomic CAS entry point"):
        store.commit_events(
            (admission,),
            (projection,),
            expected_projection_checksum=initial.projection_checksum,
        )

    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_capacity_snapshot(before.owner_scope) == before
    committed = store.commit_wave_admission(
        (admission,),
        (projection,),
        expected_projection_checksum=initial.projection_checksum,
        expected_capacity_revision=before.revision,
        capacity_scope=before.owner_scope,
        capacity_before_checksum=before.snapshot_checksum,
        capacity_after=packing.capacity_after,
        pool_reservations=packing.reservations,
    )
    # Exact historical redelivery is evidence validation, not a new mutation,
    # and therefore remains valid through the ordinary projection API.
    assert store.commit_events(
        (admission,),
        (projection,),
        expected_projection_checksum=projection.projection_checksum,
    ) == committed
    assert store.load_capacity_snapshot(before.owner_scope) == packing.capacity_after


def test_sqlite_public_commit_cannot_bypass_capacity_admission_cas(tmp_path):
    store, events, artifacts, plan, group, _initial, group_event, group_projection = (
        _durable_admission_setup(tmp_path, backend="sqlite")
    )
    store.commit_event(group_event, group_projection)
    admission, projection, before, packing, _wave, _instance, initial = (
        _capacity_admission_case(store, plan, group)
    )
    history = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(HarnessValidationError, match="atomic CAS entry point"):
        store.commit_events(
            (admission,),
            (projection,),
            expected_projection_checksum=initial.projection_checksum,
        )
    committed = store.commit_wave_admission(
        (admission,),
        (projection,),
        expected_projection_checksum=initial.projection_checksum,
        expected_capacity_revision=before.revision,
        capacity_scope=before.owner_scope,
        capacity_before_checksum=before.snapshot_checksum,
        capacity_after=packing.capacity_after,
        pool_reservations=packing.reservations,
    )

    reopened = _store(
        SQLiteEventStore(events.database, clock=lambda: FIXED_NOW),
        artifacts,
    )
    assert reopened.read_events(plan.run_id, plan.stage_id) == (*history, admission)
    assert reopened.load_capacity_snapshot(before.owner_scope) == packing.capacity_after
    assert reopened.commit_events(
        (admission,),
        (projection,),
        expected_projection_checksum=projection.projection_checksum,
    ) == committed


@pytest.mark.parametrize("writer", ("append", "batch"))
def test_raw_append_cannot_bypass_capacity_admission_cas(admitted, writer):
    store, plan, group = admitted
    admission, _projection, before, _packing, _wave, _instance, _initial = (
        _capacity_admission_case(store, plan, group)
    )
    history = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(HarnessValidationError, match="atomic CAS entry point"):
        if writer == "append":
            store.append_event(admission)
        else:
            store.append_events((admission,))

    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_capacity_snapshot(before.owner_scope) == before


def test_reserved_capacity_completion_is_a_public_noop_transition(admitted):
    store, plan, group = admitted
    admission, projection, before, packing, wave, _instance, initial = (
        _capacity_admission_case(store, plan, group)
    )
    store.commit_wave_admission(
        (admission,),
        (projection,),
        expected_projection_checksum=initial.projection_checksum,
        expected_capacity_revision=before.revision,
        capacity_scope=before.owner_scope,
        capacity_before_checksum=before.snapshot_checksum,
        capacity_after=packing.capacity_after,
        pool_reservations=packing.reservations,
    )
    capacity = packing.capacity_after
    completion = _parallel_event(
        plan,
        "TASK_WAVE_COMPLETED",
        projection.last_sequence + 1,
        {
            "group_id": group.group_id,
            "wave_id": wave.wave_id,
            "task_ids": list(wave.task_ids),
            "reservation_states": {
                task_id: ReservationState.RESERVED.value
                for task_id in wave.task_ids
            },
            "child_states": {
                task_id: TaskLifecycle.ADMITTED.value
                for task_id in wave.task_ids
            },
            "terminal_outcome": "INDETERMINATE",
            "capacity_before": capacity.to_dict(),
            "capacity_after": capacity.to_dict(),
        },
    )
    completion_projection = replace(
        projection,
        last_sequence=completion.sequence,
    )

    committed = store.commit_events(
        (completion,),
        (completion_projection,),
        expected_projection_checksum=projection.projection_checksum,
    )

    assert committed == (completion.event_checksum,)
    assert store.load_capacity_snapshot(before.owner_scope) == capacity
    assert store.commit_events(
        (completion,),
        (completion_projection,),
        expected_projection_checksum=completion_projection.projection_checksum,
    ) == committed
    assert store.load_capacity_snapshot(before.owner_scope) == capacity


def test_public_commit_events_cannot_hide_capacity_release_evidence(admitted):
    store, plan, group = admitted
    completion, projection, before, _after, _settled = _capacity_completion_case(
        store,
        plan,
        group,
    )
    current = store.load_projection(plan.run_id, plan.stage_id)
    history = store.read_events(plan.run_id, plan.stage_id)

    with pytest.raises(HarnessValidationError, match="atomic CAS entry point"):
        store.commit_events(
            (completion,),
            (projection,),
            expected_projection_checksum=current.projection_checksum,
        )
    stripped_payload = {
        key: value
        for key, value in completion.payload.items()
        if key not in {"capacity_before", "capacity_after"}
    }
    stripped = replace(completion, payload=stripped_payload)
    with pytest.raises(HarnessValidationError, match="atomic CAS entry point"):
        store.commit_events(
            (stripped,),
            (projection,),
            expected_projection_checksum=current.projection_checksum,
        )

    assert store.read_events(plan.run_id, plan.stage_id) == history
    assert store.load_capacity_snapshot(before.owner_scope) == before


def test_wave_completion_releases_capacity_once_and_redelivers_original_transition(admitted):
    store, plan, group = admitted
    completion, projection, before, after, settled = _capacity_completion_case(
        store, plan, group
    )
    committed = store.commit_wave_completion(
        (completion,),
        (projection,),
        expected_projection_checksum=store.load_projection(
            plan.run_id, plan.stage_id
        ).projection_checksum,
        expected_capacity_revision=before.revision,
        capacity_scope=before.owner_scope,
        capacity_before_checksum=before.snapshot_checksum,
        capacity_after=after,
        settled_pool_reservations=(settled,),
    )
    assert committed == (completion.event_checksum,)
    assert store.load_capacity_snapshot(before.owner_scope) == after
    assert store.commit_wave_completion(
        (completion,),
        (projection,),
        expected_projection_checksum=projection.projection_checksum,
        expected_capacity_revision=before.revision,
        capacity_scope=before.owner_scope,
        capacity_before_checksum=before.snapshot_checksum,
        capacity_after=after,
        settled_pool_reservations=(settled,),
    ) == committed
    assert store.load_capacity_snapshot(before.owner_scope) == after
    assert store.commit_events(
        (completion,),
        (projection,),
        expected_projection_checksum=projection.projection_checksum,
    ) == committed
    assert store.load_capacity_snapshot(before.owner_scope) == after

    corrupted_history = tuple(
        replace(event, sequence=event.sequence - 1)
        if event.event_checksum == completion.event_checksum
        else event
        for event in store.read_events(plan.run_id, plan.stage_id)
        if event.event_type != "TASK_ATTEMPT_RECORDED"
    )
    with pytest.raises(
        HarnessValidationError,
        match="release lacks terminal attempt evidence",
    ):
        TaskPlanReplayReducer().replay(
            (plan,),
            corrupted_history,
            require_terminal_events=False,
        )


def test_wave_completion_rejects_invalid_release_without_mutating_capacity(admitted):
    store, plan, group = admitted
    completion, projection, before, after, settled = _capacity_completion_case(
        store, plan, group
    )
    over_release_pool = replace(after.pools[0], reserved=1)
    tampered = CapacityScopeSnapshot(
        owner_scope=after.owner_scope,
        pools=(over_release_pool,),
        revision=after.revision,
        expires_at_ms=after.expires_at_ms,
    )
    tampered_event = replace(
        completion,
        payload={**completion.payload, "capacity_after": tampered.to_dict()},
    )
    with pytest.raises(HarnessValidationError):
        store.commit_wave_completion(
            (tampered_event,),
            (projection,),
            expected_projection_checksum=store.load_projection(
                plan.run_id, plan.stage_id
            ).projection_checksum,
            expected_capacity_revision=before.revision,
            capacity_scope=before.owner_scope,
            capacity_before_checksum=before.snapshot_checksum,
            capacity_after=tampered,
            settled_pool_reservations=(settled,),
        )
    with pytest.raises(HarnessValidationError, match="at least one confirmed settlement"):
        store.commit_wave_completion(
            (completion,),
            (projection,),
            expected_projection_checksum=store.load_projection(
                plan.run_id, plan.stage_id
            ).projection_checksum,
            expected_capacity_revision=before.revision,
            capacity_scope=before.owner_scope,
            capacity_before_checksum=before.snapshot_checksum,
            capacity_after=after,
            settled_pool_reservations=(),
        )

    reserved_after = CapacityScopeSnapshot(
        owner_scope=before.owner_scope,
        pools=before.pools,
        revision=before.revision + 1,
        expires_at_ms=before.expires_at_ms,
    )
    reserved_completion = replace(
        completion,
        payload={
            **completion.payload,
            "reservation_states": {
                settled.task_id: ReservationState.RESERVED.value,
            },
            "capacity_after": reserved_after.to_dict(),
        },
    )
    with pytest.raises(
        HarnessValidationError,
        match="exactly cover every released reservation",
    ):
        store.commit_wave_completion(
            (reserved_completion,),
            (projection,),
            expected_projection_checksum=store.load_projection(
                plan.run_id, plan.stage_id
            ).projection_checksum,
            expected_capacity_revision=before.revision,
            capacity_scope=before.owner_scope,
            capacity_before_checksum=before.snapshot_checksum,
            capacity_after=reserved_after,
            settled_pool_reservations=(settled,),
        )

    current = store.load_projection(plan.run_id, plan.stage_id)
    admitted_instance = task_instance_for_attempt(plan, settled.task_id, 1)
    smuggled_projection = replace(
        TaskPlanScheduler.mark_dispatched(current, admitted_instance),
        last_sequence=completion.sequence,
    )
    with pytest.raises(
        HarnessValidationError,
        match="projection differs from current authoritative state",
    ):
        store.commit_wave_completion(
            (completion,),
            (smuggled_projection,),
            expected_projection_checksum=current.projection_checksum,
            expected_capacity_revision=before.revision,
            capacity_scope=before.owner_scope,
            capacity_before_checksum=before.snapshot_checksum,
            capacity_after=after,
            settled_pool_reservations=(settled,),
        )
    assert store.load_capacity_snapshot(before.owner_scope) == before


def _shared_capacity_plan(run_id):
    base_candidate, _base_plan, policy, registry = _accepted_plan(
        (_task("a"),),
        explicit_execution_budget=True,
    )
    stage_binding = build_task_plan_stage_binding(
        graph_id="research.dynamic",
        stage_id=policy.stage_id,
        policy_ref=policy.exact_ref,
        required_output_roles=policy.required_output_roles,
        input_keys=("document",),
    )
    candidate = PlanCandidate.for_stage(
        stage_identity=TaskPlanStageIdentity(
            run_id=run_id,
            stage_binding=stage_binding,
        ),
        candidate_id=f"candidate-{run_id}",
        input_context_refs=base_candidate.input_context_refs,
        tasks=base_candidate.tasks,
        required_output_roles=base_candidate.required_output_roles,
        generated_by=base_candidate.generated_by,
        requested_plan_budget=base_candidate.requested_plan_budget,
    )
    plan = TaskPlanValidator().accept(
        candidate,
        policy,
        registry,
        plan_id=f"plan-{run_id}",
        accepted_at="2026-08-02T00:00:00Z",
        context=TaskPlanValidationContext(
            run_id=run_id,
            stage_binding=stage_binding,
            available_input_refs=("document",),
            registered_gate_refs=policy.allowed_gate_refs,
        ),
    )
    return candidate, plan


def _prepare_shared_capacity_proposal(store, run_id):
    candidate, plan = _shared_capacity_plan(run_id)
    store.append_candidate(candidate)
    store.accept_plan(plan)
    coordinator = ParallelAgentCoordinator(
        max_workers=1,
        serial_executor=SerialTaskExecutorAdapter(),
    )
    group = coordinator.create_group(
        replace(_request(plan), serial_fallback=True),
    )
    initial = store.load_projection(plan.run_id, plan.stage_id)
    group_event = _parallel_event(
        plan,
        "TASK_GROUP_ADMITTED",
        initial.last_sequence + 1,
        {
            "group": group.to_dict(),
            "requested_parallelism": 1,
            "effective_parallelism": 1,
        },
    )
    store.commit_event(
        group_event,
        replace(initial, last_sequence=group_event.sequence),
    )
    admission, projection, before, packing, wave, _instance, ready = (
        _capacity_admission_case(store, plan, group)
    )
    return plan, admission, projection, before, packing, wave, ready


def test_two_plans_compete_atomically_for_one_sqlite_capacity_revision(tmp_path):
    database = tmp_path / "shared-plan-capacity.sqlite3"
    artifacts = _ArtifactStore()
    setup_stores = tuple(
        _store(
            SQLiteEventStore(database, clock=lambda: FIXED_NOW),
            artifacts,
        )
        for _ in range(2)
    )
    proposals = tuple(
        _prepare_shared_capacity_proposal(store, run_id)
        for store, run_id in zip(
            setup_stores,
            ("shared-plan-a", "shared-plan-b"),
            strict=True,
        )
    )
    assert proposals[0][0].plan_id != proposals[1][0].plan_id
    assert proposals[0][0].run_id != proposals[1][0].run_id
    assert proposals[0][3] == proposals[1][3]
    assert proposals[0][4].capacity_after == proposals[1][4].capacity_after
    prefixes = tuple(
        store.read_events(plan.run_id, plan.stage_id)
        for store, (plan, *_rest) in zip(setup_stores, proposals, strict=True)
    )

    barrier = Barrier(2)
    backends = tuple(
        SQLiteEventStore(database, clock=lambda: FIXED_NOW)
        for _ in range(2)
    )
    runtimes = tuple(
        _RacingAdmissionRuntime(backend, barrier)
        for backend in backends
    )
    writers = tuple(
        _store(backend, artifacts, runtime=runtime)
        for backend, runtime in zip(backends, runtimes, strict=True)
    )

    def commit(index):
        plan, admission, projection, before, packing, _wave, ready = proposals[index]
        try:
            return writers[index].commit_wave_admission(
                (admission,),
                (projection,),
                expected_projection_checksum=ready.projection_checksum,
                expected_capacity_revision=before.revision,
                capacity_scope=before.owner_scope,
                capacity_before_checksum=before.snapshot_checksum,
                capacity_after=packing.capacity_after,
                pool_reservations=packing.reservations,
            )
        except EventContractError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(commit, range(2)))

    winners = tuple(
        index for index, outcome in enumerate(outcomes)
        if not isinstance(outcome, EventContractError)
    )
    losers = tuple(
        index for index, outcome in enumerate(outcomes)
        if isinstance(outcome, EventContractError)
    )
    assert len(winners) == len(losers) == 1
    winner = winners[0]
    loser = losers[0]
    assert isinstance(outcomes[loser], EventContractError)
    assert str(outcomes[loser]) == (
        "state redelivery cannot commit previously unseen events"
    )
    assert all(runtime.publications == 1 for runtime in runtimes)

    reopened = _store(
        SQLiteEventStore(database, clock=lambda: FIXED_NOW),
        artifacts,
    )
    winner_plan, winner_event, winner_projection, _before, winner_packing, winner_wave, _ready = proposals[winner]
    loser_plan, _loser_event, _loser_projection, _before, _loser_packing, _loser_wave, loser_ready = proposals[loser]
    assert reopened.load_capacity_snapshot("shared-capacity") == winner_packing.capacity_after
    assert winner_packing.capacity_after.pools[0].reserved == 1

    winner_history = reopened.read_events(winner_plan.run_id, winner_plan.stage_id)
    assert winner_history == (*prefixes[winner], winner_event)
    committed_wave = DispatchWave.from_dict(
        thaw_mapping(winner_history[-1].payload["wave"])
    )
    assert committed_wave.wave_id == winner_wave.wave_id
    assert committed_wave.packing.capacity_after == winner_packing.capacity_after
    assert committed_wave.packing.reservations == winner_packing.reservations
    assert reopened.load_projection(
        winner_plan.run_id,
        winner_plan.stage_id,
    ) == winner_projection

    assert reopened.read_events(loser_plan.run_id, loser_plan.stage_id) == prefixes[loser]
    loser_projection = reopened.load_projection(
        loser_plan.run_id,
        loser_plan.stage_id,
    )
    assert loser_projection == loser_ready
    assert loser_projection.logical_ready_order == ("a",)
    assert all(
        state.status is TaskLifecycle.READY
        and state.attempts == 0
        and state.active_instance_id is None
        and state.admission_owner is None
        for state in loser_projection.tasks
    )
    assert TaskPlanBudgetLedger.from_snapshot(
        loser_projection.consumed_budget
    ).records == {}
