from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Event
from threading import Barrier
import pytest

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.execution_control import (
    ChildExecutionControl,
    bind_child_execution_control,
    current_child_execution_control,
    require_current_child_execution_control,
)
from framework.harness.subagents.supervisor import (
    ChildAgentHandle,
    ChildAgentLease,
    ChildAgentState,
    ChildAgentSupervisor,
)
from framework.harness.task_plan.parallel import (
    DispatchWave,
    DispatchWaveState,
    ParallelAgentCoordinator,
    ReservationState,
    TaskReservation,
    _SupervisorTaskWorker,
)
from framework.shared.attempts import current_attempt_context
from framework.shared.graph_identity import GraphExecutionIdentity
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _admitted_request,
    _packing,
    _parent_identity,
    _result,
    _request,
)


def _control(
    *,
    clock=lambda: 100.0,
    lease_seconds: float = 30.0,
    deadline_ms: int | None = None,
):
    plan = _accepted_parallel_plan(("task-1",))
    request = _admitted_request(_request(plan))
    instance = request.task_instances[0]
    wave = DispatchWave(
        "group-1",
        1,
        (instance.task_id,),
        1,
        (
            TaskReservation(
                instance.task_id,
                instance.idempotency_key,
                instance.budget_snapshot.to_dict(),
                ReservationState.RESERVED,
            ),
        ),
        DispatchWaveState.ADMITTED,
        packing=_packing(instance.task_id),
    )
    spawn = ParallelAgentCoordinator._spawn_request(request, wave, instance)
    now = datetime(2026, 9, 11, tzinfo=UTC)
    deadline_ms = deadline_ms or int(now.timestamp() * 1000) + 100_000
    handle = ChildAgentHandle(
        child_id=spawn.child_id or "child-1",
        parent_graph_identity=_parent_identity(plan),
        child_graph_identity=None,
        stage_id=spawn.stage_id,
        task_id=instance.task_id,
        task_instance_id=instance.task_instance_id,
        attempt=instance.attempt,
        allowed_tools=spawn.allowed_tools,
        allowed_memory_namespaces=spawn.allowed_memory_namespaces,
        budget=spawn.budget,
        transcript_ref=None,
        operation_id=spawn.operation_id,
        state=ChildAgentState.RUNNING,
        lease=ChildAgentLease("lease-1", now, now + timedelta(seconds=lease_seconds)),
        created_at=now,
        updated_at=now,
    )
    return (
        ChildExecutionControl.create(
            handle=handle,
            spawn_request=spawn,
            task_instance=instance,
            reservation=spawn.budget,
            group_absolute_deadline_ms=deadline_ms,
            clock=clock,
            utc_clock=lambda: now,
        ),
        instance,
        _parent_identity(plan),
    )


def test_control_binds_exact_handle_attempt_and_root_context() -> None:
    control, instance, parent = _control()

    assert current_child_execution_control() is None
    with bind_child_execution_control(control):
        current = require_current_child_execution_control(
            task_instance=instance,
            parent_graph_identity=parent,
        )
        assert current is control
        assert current_attempt_context() is control.attempt_context
    assert current_child_execution_control() is None


def test_control_rejects_missing_or_mismatched_execution_identity() -> None:
    control, instance, parent = _control()
    with pytest.raises(HarnessValidationError) as missing:
        require_current_child_execution_control(
            task_instance=instance,
            parent_graph_identity=parent,
        )
    assert missing.value.code == "CHILD_EXECUTION_CONTROL_REQUIRED"

    wrong = GraphExecutionIdentity(
        run_id=parent.run_id,
        graph_id=parent.graph_id,
        graph_version=parent.graph_version,
        graph_ref=parent.graph_ref,
        graph_checksum=parent.graph_checksum,
        node_id=parent.node_id,
        node_instance_id="another-node",
        activity_id=parent.activity_id,
        attempt=parent.attempt,
    )
    with bind_child_execution_control(control), pytest.raises(HarnessValidationError) as mismatch:
        require_current_child_execution_control(
            task_instance=instance,
            parent_graph_identity=wrong,
        )
    assert mismatch.value.code == "CHILD_EXECUTION_CONTROL_IDENTITY_MISMATCH"


def test_overlapping_control_bindings_are_isolated() -> None:
    first, first_instance, first_parent = _control()
    second, second_instance, second_parent = _control()

    with bind_child_execution_control(first):
        assert require_current_child_execution_control(
            task_instance=first_instance,
            parent_graph_identity=first_parent,
        ) is first
        with bind_child_execution_control(second):
            assert require_current_child_execution_control(
                task_instance=second_instance,
                parent_graph_identity=second_parent,
            ) is second
        assert require_current_child_execution_control(
            task_instance=first_instance,
            parent_graph_identity=first_parent,
        ) is first


def test_cancel_and_deadline_stop_new_child_operations() -> None:
    clock = [100.0]
    control, _instance, _parent = _control(clock=lambda: clock[0])
    control.request_cancel()
    with pytest.raises(HarnessValidationError) as cancelled:
        control.raise_if_active()
    assert cancelled.value.code == "CHILD_EXECUTION_CANCELLED"

    clock[0] = 100.0
    deadline, _instance, _parent = _control(clock=lambda: clock[0])
    clock[0] = 131.0
    with pytest.raises(HarnessValidationError) as elapsed:
        deadline.raise_if_active()
    assert elapsed.value.code == "CHILD_EXECUTION_DEADLINE_EXCEEDED"


def test_effective_deadline_is_the_earliest_immutable_admission_bound() -> None:
    clock = [100.0]
    control, _instance, _parent = _control(
        clock=lambda: clock[0],
        lease_seconds=30.0,
    )

    # The admitted lease (30 seconds) is tighter than the group (100 seconds)
    # and the task's versioned time allocation (900 seconds).
    assert control.effective_deadline == 130.0


def test_unconfirmed_descendant_is_preserved_as_indeterminate() -> None:
    control, _instance, _parent = _control()
    control.mark_descendant_unconfirmed()
    with pytest.raises(Exception) as raised:
        control.raise_if_active()
    assert getattr(raised.value, "code", None) == "attempt_indeterminate"


def test_real_supervisor_handle_binds_control_before_worker_body() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _admitted_request(_request(plan))
    instance = request.task_instances[0]
    wave = DispatchWave(
        "group-1", 1, (instance.task_id,), 1,
        (TaskReservation(instance.task_id, instance.idempotency_key, instance.budget_snapshot.to_dict()),),
        DispatchWaveState.ADMITTED, packing=_packing(instance.task_id),
    )
    spawn = ParallelAgentCoordinator._spawn_request(request, wave, instance)
    observed = []

    def invoke(item):
        control = require_current_child_execution_control(
            task_instance=item,
            parent_graph_identity=_parent_identity(plan),
        )
        observed.append(control)
        assert current_attempt_context() is control.attempt_context
        control.raise_if_active()
        return _result(plan, item)

    worker = _SupervisorTaskWorker(
        invoke, instance, spawn_request=spawn,
        group_absolute_deadline_ms=request.group_absolute_deadline_ms,
    )
    supervisor = ChildAgentSupervisor(max_children=1)
    try:
        handle = supervisor.spawn(spawn, worker=worker)
        terminal = supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
        assert terminal.receipt is not None
        assert terminal.receipt.status is ChildAgentState.SUCCEEDED
        assert len(observed) == 1
        assert observed[0].handle == handle
    finally:
        supervisor.shutdown()


def test_identity_mismatch_rejects_before_supervised_worker_body() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _admitted_request(_request(plan))
    first, second = request.task_instances
    wave = DispatchWave(
        "group-1", 1, (first.task_id,), 1,
        (TaskReservation(first.task_id, first.idempotency_key, first.budget_snapshot.to_dict()),),
        DispatchWaveState.ADMITTED, packing=_packing(first.task_id),
    )
    spawn = ParallelAgentCoordinator._spawn_request(request, wave, first)
    body_called = False

    def invoke(_item):
        nonlocal body_called
        body_called = True
        return _result(plan, first)

    worker = _SupervisorTaskWorker(
        invoke, second, spawn_request=spawn,
        group_absolute_deadline_ms=request.group_absolute_deadline_ms,
    )
    supervisor = ChildAgentSupervisor(max_children=1)
    try:
        handle = supervisor.spawn(spawn, worker=worker)
        terminal = supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
        assert terminal.receipt is not None
        assert terminal.receipt.status is ChildAgentState.FAILED
        assert body_called is False
    finally:
        supervisor.shutdown()


def test_two_overlapping_supervised_children_keep_controls_isolated() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _admitted_request(_request(plan))
    first, second = request.task_instances
    wave = DispatchWave(
        "group-1", 1, (first.task_id, second.task_id), 2,
        (
            TaskReservation(first.task_id, first.idempotency_key, first.budget_snapshot.to_dict()),
            TaskReservation(second.task_id, second.idempotency_key, second.budget_snapshot.to_dict()),
        ),
        DispatchWaveState.ADMITTED, packing=_packing(first.task_id, second.task_id),
    )
    spawns = tuple(
        ParallelAgentCoordinator._spawn_request(request, wave, instance)
        for instance in (first, second)
    )
    barrier = Barrier(2)
    observed = {}

    def worker_for(instance, spawn):
        def invoke(item):
            control = require_current_child_execution_control(
                task_instance=item, parent_graph_identity=_parent_identity(plan),
            )
            barrier.wait(timeout=1)
            observed[item.task_id] = control
            assert current_attempt_context() is control.attempt_context
            return _result(plan, item)

        return _SupervisorTaskWorker(
            invoke, instance, spawn_request=spawn,
            group_absolute_deadline_ms=request.group_absolute_deadline_ms,
        )

    supervisor = ChildAgentSupervisor(max_children=2)
    try:
        handles = supervisor.spawn_batch(
            spawns,
            workers=tuple(worker_for(instance, spawn) for instance, spawn in zip((first, second), spawns, strict=True)),
        )
        for handle in handles:
            terminal = supervisor.wait(handle.child_id, operation_id=handle.operation_id, timeout_seconds=1)
            assert terminal.receipt is not None
            assert terminal.receipt.status is ChildAgentState.SUCCEEDED
        assert set(observed) == {"task-1", "task-2"}
        assert observed["task-1"] is not observed["task-2"]
        assert observed["task-1"].handle.task_instance_id == first.task_instance_id
        assert observed["task-2"].handle.task_instance_id == second.task_instance_id
    finally:
        supervisor.shutdown()


def test_cancellation_reaches_started_worker_control_without_claiming_termination() -> None:
    plan = _accepted_parallel_plan(("task-1",))
    request = _admitted_request(_request(plan))
    instance = request.task_instances[0]
    wave = DispatchWave(
        "group-1", 1, (instance.task_id,), 1,
        (TaskReservation(instance.task_id, instance.idempotency_key, instance.budget_snapshot.to_dict()),),
        DispatchWaveState.ADMITTED, packing=_packing(instance.task_id),
    )
    spawn = ParallelAgentCoordinator._spawn_request(request, wave, instance)
    started = Event()
    release = Event()
    cancellation_seen = Event()

    def invoke(item):
        control = require_current_child_execution_control(
            task_instance=item, parent_graph_identity=_parent_identity(plan),
        )
        started.set()
        assert release.wait(timeout=1)
        if control.cancel_event.is_set():
            cancellation_seen.set()
        return _result(plan, item)

    worker = _SupervisorTaskWorker(
        invoke, instance, spawn_request=spawn,
        group_absolute_deadline_ms=request.group_absolute_deadline_ms,
    )
    supervisor = ChildAgentSupervisor(max_children=1)
    try:
        handle = supervisor.spawn(spawn, worker=worker)
        assert started.wait(timeout=1)
        cancelled = supervisor.cancel(handle.child_id, operation_id=handle.operation_id)
        assert cancelled.receipt is not None
        assert cancelled.receipt.termination_confirmed is False
        release.set()
        assert cancellation_seen.wait(timeout=1)
    finally:
        release.set()
        supervisor.shutdown()


def test_recovered_worker_placeholder_cannot_acquire_live_execution_rights() -> None:
    control, instance, _parent = _control()
    worker = _SupervisorTaskWorker(lambda _item: pytest.fail("must not execute"), instance)
    with pytest.raises(HarnessValidationError) as raised:
        worker.run(control.handle)
    assert raised.value.code == "CHILD_EXECUTION_CONTROL_REQUIRED"
