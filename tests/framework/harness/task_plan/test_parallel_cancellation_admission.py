from __future__ import annotations

from concurrent.futures import wait
from threading import Event, Thread

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import ChildAgentState, ChildAgentSupervisor
from framework.harness.task_plan.parallel import (
    DispatchGroupState,
    ParallelAgentCoordinator,
    ParallelEventSink,
)
from tests.framework.harness.task_plan.test_parallel_orchestration import (
    _accepted_parallel_plan,
    _request,
    _result,
)


def test_indeterminate_cohort_fences_queued_sibling_before_parent_returns() -> None:
    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    occupied = Event()
    release_occupied = Event()
    started = Event()
    release_running = Event()
    dispatched = Event()
    returned = Event()
    calls: list[str] = []
    errors: list[BaseException] = []
    events: list[dict] = []

    def emit(event):
        events.append(event)
        if event["event_type"] == "TASK_WAVE_DISPATCHED":
            dispatched.set()

    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(emit, lambda batch: [emit(event) for event in batch]),
    )

    def occupy_executor():
        occupied.set()
        assert release_occupied.wait(timeout=5)

    # A busy executor thread makes the second admitted child genuinely queued.
    # No scheduling delay or polling is used to arrange the interleaving.
    occupier = supervisor._executor.submit(occupy_executor)

    def invoke(instance):
        calls.append(instance.task_id)
        if instance.task_id == "task-1":
            started.set()
            assert release_running.wait(timeout=5)
        return _result(plan, instance)

    def dispatch():
        try:
            coordinator.dispatch(request, invoke)
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    thread = Thread(target=dispatch)
    try:
        assert occupied.wait(timeout=5)
        thread.start()
        assert started.wait(timeout=5)
        assert dispatched.wait(timeout=5)
        session = next(iter(coordinator._sessions.values()))
        running, _ = session.active_children["task-1"]
        queued, _ = session.active_children["task-2"]
        cancelled = supervisor.cancel(running.child_id, operation_id=running.operation_id)
        assert cancelled.receipt.status is ChildAgentState.LOST
        assert cancelled.receipt.termination_confirmed is False
        assert returned.wait(timeout=5)
        assert len(errors) == 1
        assert isinstance(errors[0], HarnessValidationError)
        assert errors[0].code == "TASK_GROUP_INDETERMINATE"
        calls_at_return = tuple(calls)

        # Drain all physical work after the parent has returned. The queued
        # sibling must not acquire callback admission during this interval.
        release_running.set()
        release_occupied.set()
        occupier.result(timeout=5)
        assert not wait(tuple(supervisor._futures.values()), timeout=5).not_done
        supervisor.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert tuple(calls) == calls_at_return == ("task-1",)
        queued_terminal = supervisor.wait(queued.child_id, operation_id=queued.operation_id)
        assert queued_terminal.receipt.status is ChildAgentState.CANCELLED
        assert queued_terminal.receipt.termination_confirmed is True
        assert queued_terminal.handle.state is ChildAgentState.CLOSED
        assert "task-2" not in session.active_children
        assert "task-1" in session.active_children
        running_terminal = supervisor.wait(running.child_id, operation_id=running.operation_id)
        assert running_terminal.receipt == cancelled.receipt
        joined = coordinator.join(request)
        assert joined.group.state is DispatchGroupState.INDETERMINATE
        history = {record.task_id: record for record in joined.attempt_history}
        assert history["task-1"].outcome.value == "INDETERMINATE"
        assert history["task-1"].terminal_receipt["termination_confirmed"] is False
        assert history["task-2"].outcome.value == "CANCELLED"
        assert history["task-2"].terminal_receipt["termination_confirmed"] is True
    finally:
        release_running.set()
        release_occupied.set()
        if thread.ident is not None:
            thread.join(timeout=5)
        supervisor.shutdown()


def test_indeterminate_cohort_revokes_admitted_sibling_before_subagent_dispatch(monkeypatch) -> None:
    from framework.harness import FakeSubAgentRuntime, FakeSubAgentWorker, SubAgentStatus, fake_subagent_spec
    from framework.harness.subagents import runtime as runtime_module

    plan = _accepted_parallel_plan(("task-1", "task-2"))
    request = _request(plan)
    started = Event()
    release_running = Event()
    dispatch_ready = Event()
    release_dispatch = Event()
    dispatched = Event()
    returned = Event()
    errors = []
    worker = FakeSubAgentWorker()
    runtime = FakeSubAgentRuntime(fake_subagent_spec(), worker)
    invocation = runtime.build_invocation()
    inspect_identity = runtime_module._accepts_execution_identity

    def pause_dispatch(callback):
        dispatch_ready.set()
        assert release_dispatch.wait(timeout=5)
        return inspect_identity(callback)

    monkeypatch.setattr(runtime_module, "_accepts_execution_identity", pause_dispatch)

    def emit(event):
        if event["event_type"] == "TASK_WAVE_DISPATCHED":
            dispatched.set()

    supervisor = ChildAgentSupervisor(max_children=2)
    coordinator = ParallelAgentCoordinator(
        max_workers=2,
        child_supervisor=supervisor,
        event_sink=ParallelEventSink(emit, lambda batch: [emit(event) for event in batch]),
    )

    def invoke(instance):
        if instance.task_id == "task-1":
            started.set()
            assert release_running.wait(timeout=5)
        else:
            result = runtime.invoke(invocation)
            assert result.status is SubAgentStatus.HALTED
        return _result(plan, instance)

    def dispatch():
        try:
            coordinator.dispatch(request, invoke)
        except BaseException as exc:
            errors.append(exc)
        finally:
            returned.set()

    thread = Thread(target=dispatch)
    try:
        thread.start()
        assert started.wait(timeout=5)
        assert dispatch_ready.wait(timeout=5)
        assert dispatched.wait(timeout=5)
        session = next(iter(coordinator._sessions.values()))
        running, _ = session.active_children["task-1"]
        sibling, _ = session.active_children["task-2"]
        cancelled = supervisor.cancel(running.child_id, operation_id=running.operation_id)
        assert cancelled.receipt.status is ChildAgentState.LOST
        assert cancelled.receipt.termination_confirmed is False
        assert returned.wait(timeout=5)
        assert len(errors) == 1
        assert isinstance(errors[0], HarnessValidationError)
        assert errors[0].code == "TASK_GROUP_INDETERMINATE"
        assert worker.calls == []
        release_dispatch.set()
        release_running.set()
        assert not wait(tuple(supervisor._futures.values()), timeout=5).not_done
        supervisor.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert worker.calls == []
        sibling_terminal = supervisor.wait(sibling.child_id, operation_id=sibling.operation_id)
        assert sibling_terminal.receipt.status is ChildAgentState.LOST
        assert sibling_terminal.receipt.termination_confirmed is False
        history = coordinator.join(request).attempt_history
        assert {record.task_id for record in history} == {"task-1", "task-2"}
        assert all(record.outcome.value == "INDETERMINATE" for record in history)
        assert all(record.terminal_receipt["termination_confirmed"] is False for record in history)
    finally:
        release_dispatch.set()
        release_running.set()
        thread.join(timeout=5)
        supervisor.shutdown()
