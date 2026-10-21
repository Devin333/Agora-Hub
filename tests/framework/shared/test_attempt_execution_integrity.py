from __future__ import annotations

import threading
import time
from contextvars import ContextVar

import pytest

from framework.shared.attempts import (
    AdmissionResult,
    AttemptCapacityExhaustedError,
    AttemptContext,
    AttemptFinalization,
    AttemptState,
    AttemptExecutionCapacity,
    AttemptLifecycleEmissionError,
    AttemptOutcome,
    AttemptSupervisor,
    CompositeAttemptLifecycleSink,
    DeadlineAdmissionPolicy,
    ExecutionLimits,
    LocalRetryBudget,
    RetryCreditExhaustedError,
    RetryCreditLedger,
    ParentCancelledBeforeStartError,
    current_attempt_context,
)


class _RecordingAttemptSink:
    def __init__(
        self,
        *,
        fail_started: bool = False,
        fail_terminal: bool = False,
        required: bool = True,
    ) -> None:
        self.events: list[tuple[str, object]] = []
        self.fail_started = fail_started
        self.fail_terminal = fail_terminal
        self.required = required

    def rejected(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        idempotency_key: str,
        admission: AdmissionResult,
    ) -> None:
        self.events.append(("rejected", admission))

    def started(self, *, context: AttemptContext) -> None:
        if self.fail_started:
            raise OSError("event store unavailable")
        self.events.append(("started", context.attempt_id))

    def terminal(self, *, outcome: AttemptOutcome[object]) -> None:
        self.events.append(("terminal", outcome.state))
        if self.fail_terminal:
            raise OSError("event store unavailable")


def test_supervised_attempt_propagates_caller_context_without_leaking_mutations() -> None:
    run_binding: ContextVar[str | None] = ContextVar(
        "attempt_test_run_binding",
        default=None,
    )
    token = run_binding.set("run-1")

    def invoke() -> str | None:
        assert run_binding.get() == "run-1"
        run_binding.set("worker-local")
        return run_binding.get()

    try:
        outcome = AttemptSupervisor().run(
            invoke,
            timeout_seconds=1.0,
            idempotency_key="context:run-1",
        )

        assert outcome.state is AttemptState.SUCCEEDED
        assert outcome.value == "worker-local"
        assert run_binding.get() == "run-1"
    finally:
        run_binding.reset(token)

    assert run_binding.get() is None


def test_non_cooperative_attempts_are_admitted_by_a_hard_capacity_limit() -> None:
    release = threading.Event()
    capacity = AttemptExecutionCapacity(max_active=2)
    supervisor = AttemptSupervisor(
        cancellation_grace_seconds=0.001,
        capacity=capacity,
    )

    def block() -> None:
        release.wait(1)

    first = supervisor.run(
        block,
        timeout_seconds=0.001,
        idempotency_key="capacity:first",
    )
    second = supervisor.run(
        block,
        timeout_seconds=0.001,
        idempotency_key="capacity:second",
    )
    third = supervisor.run(
        block,
        timeout_seconds=0.001,
        idempotency_key="capacity:third",
    )

    assert first.state is AttemptState.TIMED_OUT
    assert second.state is AttemptState.TIMED_OUT
    assert isinstance(third.error, AttemptCapacityExhaustedError)
    assert third.state is AttemptState.REJECTED
    assert third.context is None
    assert third.started is False
    assert capacity.active == 2

    release.set()
    deadline = time.monotonic() + 1
    while capacity.active and time.monotonic() < deadline:
        time.sleep(0.005)
    assert capacity.active == 0


def test_standalone_local_timeout_is_not_rejected_by_cancellation_grace() -> None:
    started = threading.Event()

    def clock() -> float:
        # Advance the deadline only after the worker proves it was scheduled.
        # Admission is the behavior under test; OS thread latency is not.
        return 0.02 if started.is_set() else 0.0

    supervisor = AttemptSupervisor(
        cancellation_grace_seconds=0.05,
        clock=clock,
    )
    local_budget = LocalRetryBudget(max_attempts=1)

    def block() -> None:
        started.set()
        context = current_attempt_context()
        assert context is not None
        assert context.cancel_event.wait(1)
        context.raise_if_cancelled()

    outcome = supervisor.run(
        block,
        timeout_seconds=0.01,
        idempotency_key="deadline:local-timeout",
        local_budget=local_budget,
        admission_policy=DeadlineAdmissionPolicy(
            timeout_seconds=0.01,
            cancellation_grace_seconds=0.05,
        ),
    )

    assert started.is_set()
    assert outcome.state is AttemptState.TIMED_OUT
    assert outcome.started is True
    assert outcome.timed_out is True
    assert outcome.termination_confirmed is True
    assert outcome.indeterminate is False
    assert outcome.context is not None
    assert outcome.context.local_attempt_no == 1
    assert local_budget.used == 1


def test_root_deadline_reserves_child_grace_and_rejects_without_consumption() -> None:
    now = 100.0
    capacity = AttemptExecutionCapacity(max_active=1)
    limits = ExecutionLimits(
        execution_id="deadline:root",
        hard_deadline=102.0,
        retry_credits=RetryCreditLedger(max_total_retries=2),
        verify_reserve_seconds=0.2,
        commit_reserve_seconds=0.1,
    )
    local_budget = LocalRetryBudget(max_attempts=2)
    called = False

    def fail_if_called() -> None:
        nonlocal called
        called = True

    outcome = AttemptSupervisor(
        clock=lambda: now,
        capacity=capacity,
    ).run(
        fail_if_called,
        timeout_seconds=5.0,
        idempotency_key="deadline:insufficient",
        local_budget=local_budget,
        execution_limits=limits,
        admission_policy=DeadlineAdmissionPolicy(
            timeout_seconds=5.0,
            min_start_window_seconds=2.0,
            cancellation_grace_seconds=0.3,
        ),
    )

    assert outcome.state is AttemptState.REJECTED
    assert outcome.reason_code == "attempt_deadline_admission_rejected"
    assert outcome.context is None
    assert called is False
    assert local_budget.used == 0
    assert limits.retry_credits.used_retries == 0
    assert capacity.active == 0


def test_parent_cancellation_rejects_before_local_or_root_claim() -> None:
    parent_cancel = threading.Event()
    parent_cancel.set()
    local_budget = LocalRetryBudget(max_attempts=2)
    ledger = RetryCreditLedger(max_total_retries=1)

    outcome = AttemptSupervisor().run(
        lambda: pytest.fail("cancelled child must not invoke callable"),
        timeout_seconds=None,
        idempotency_key="cancelled:child",
        local_budget=local_budget,
        retry_ledger=ledger,
        parent_cancel_event=parent_cancel,
    )

    assert outcome.state is AttemptState.REJECTED
    assert isinstance(outcome.error, ParentCancelledBeforeStartError)
    assert local_budget.used == 0
    assert ledger.used_retries == 0


def test_explicit_cancel_event_is_supervised_without_a_deadline() -> None:
    cancel_event = threading.Event()

    def cancel_before_return() -> str:
        cancel_event.set()
        return "must-not-succeed"

    outcome = AttemptSupervisor().run(
        cancel_before_return,
        timeout_seconds=None,
        idempotency_key="cancelled:explicit-event",
        cancel_event=cancel_event,
    )

    assert outcome.state is AttemptState.TIMED_OUT
    assert outcome.value is None


def test_root_retry_credit_exhaustion_keeps_other_local_budget_unchanged() -> None:
    ledger = RetryCreditLedger(max_total_retries=1)
    first_budget = LocalRetryBudget(max_attempts=2)
    second_budget = LocalRetryBudget(max_attempts=2)
    supervisor = AttemptSupervisor()

    first = supervisor.run(
        lambda: (_ for _ in ()).throw(RuntimeError("first failure")),
        timeout_seconds=None,
        idempotency_key="retry:first",
        local_budget=first_budget,
        retry_ledger=ledger,
    )
    retry = supervisor.run(
        lambda: (_ for _ in ()).throw(RuntimeError("retry failure")),
        timeout_seconds=None,
        idempotency_key="retry:first",
        local_budget=first_budget,
        retry_ledger=ledger,
    )
    blocked = supervisor.run(
        lambda: None,
        timeout_seconds=None,
        idempotency_key="retry:second",
        local_budget=second_budget,
        retry_ledger=ledger,
    )

    assert first.state is AttemptState.FAILED
    assert retry.state is AttemptState.FAILED
    assert ledger.used_retries == 1
    assert blocked.state is AttemptState.SUCCEEDED
    # A first attempt does not consume a retry credit.
    blocked_retry = supervisor.run(
        lambda: pytest.fail("retry credit should be exhausted"),
        timeout_seconds=None,
        idempotency_key="retry:second",
        local_budget=second_budget,
        retry_ledger=ledger,
    )
    assert blocked_retry.state is AttemptState.REJECTED
    assert isinstance(blocked_retry.error, RetryCreditExhaustedError)
    assert second_budget.used == 1


def test_parent_executable_deadline_does_not_deduct_completion_reserve_twice() -> None:
    parent = AttemptContext.create(
        attempt_id="parent-attempt",
        idempotency_key="root:step:parent",
        operation_id="root:step:parent",
        operation_kind="graph_step",
        deadline=5.0,
        completion_reserve_seconds=1.0,
        clock=lambda: 0.0,
    )

    outcome = AttemptSupervisor(clock=lambda: 0.0).run(
        lambda: "ok",
        timeout_seconds=10.0,
        idempotency_key="root:step:parent:tool:child",
        operation_id="root:step:parent:tool:child",
        parent_context=parent,
        admission_policy=DeadlineAdmissionPolicy(
            timeout_seconds=10.0,
            min_start_window_seconds=4.5,
        ),
    )

    assert outcome.state is AttemptState.SUCCEEDED
    assert outcome.context is not None
    assert outcome.context.deadline == 5.0
    assert outcome.context.admission_details["parent_available_until"] == 5.0


def test_deadline_rejection_emits_only_rejection_and_never_prepares() -> None:
    sink = _RecordingAttemptSink()
    prepared = False
    called = False
    local_budget = LocalRetryBudget(max_attempts=1)
    capacity = AttemptExecutionCapacity(max_active=1)

    def prepare(_identity: object) -> None:
        nonlocal prepared
        prepared = True

    def invoke() -> None:
        nonlocal called
        called = True

    outcome = AttemptSupervisor(clock=lambda: 10.0, capacity=capacity).run(
        invoke,
        timeout_seconds=1.0,
        idempotency_key="deadline:rejected",
        local_budget=local_budget,
        parent_available_deadline=10.5,
        admission_policy=DeadlineAdmissionPolicy(
            timeout_seconds=1.0,
            min_start_window_seconds=1.0,
        ),
        prepare=prepare,
        event_sink=sink,
    )

    assert outcome.state is AttemptState.REJECTED
    assert [event_type for event_type, _ in sink.events] == ["rejected"]
    assert prepared is False
    assert called is False
    assert local_budget.used == 0
    assert capacity.active == 0


def test_prepare_crossing_deadline_is_started_timeout_not_rejection() -> None:
    now = [0.0]
    sink = _RecordingAttemptSink()
    prepared = 0
    called = False
    local_budget = LocalRetryBudget(max_attempts=1)

    def prepare(_identity: object) -> None:
        nonlocal prepared
        prepared += 1
        now[0] = 2.0

    def invoke() -> None:
        nonlocal called
        called = True

    outcome = AttemptSupervisor(clock=lambda: now[0]).run(
        invoke,
        timeout_seconds=1.0,
        idempotency_key="deadline:crossed-during-prepare",
        local_budget=local_budget,
        admission_policy=DeadlineAdmissionPolicy(timeout_seconds=1.0),
        prepare=prepare,
        event_sink=sink,
    )

    assert outcome.state is AttemptState.TIMED_OUT
    assert outcome.started is True
    assert prepared == 1
    assert called is False
    assert local_budget.used == 1
    assert [event_type for event_type, _ in sink.events] == [
        "started",
        "terminal",
    ]


def test_completion_observed_after_deadline_cannot_be_reported_as_success() -> None:
    now = [0.0]

    def finish_late() -> str:
        now[0] = 2.0
        return "late"

    outcome = AttemptSupervisor(clock=lambda: now[0]).run(
        finish_late,
        timeout_seconds=1.0,
        idempotency_key="deadline:late-completion",
    )

    assert outcome.state is AttemptState.TIMED_OUT
    assert outcome.value is None
    assert outcome.timed_out is True


def test_started_event_failure_rolls_back_before_callable_or_prepare() -> None:
    sink = _RecordingAttemptSink(fail_started=True)
    local_budget = LocalRetryBudget(max_attempts=1)
    capacity = AttemptExecutionCapacity(max_active=1)
    prepared = False
    called = False

    def prepare(_identity: object) -> None:
        nonlocal prepared
        prepared = True

    def invoke() -> None:
        nonlocal called
        called = True

    with pytest.raises(AttemptLifecycleEmissionError):
        AttemptSupervisor(capacity=capacity).run(
            invoke,
            timeout_seconds=1.0,
            idempotency_key="events:start-failure",
            local_budget=local_budget,
            prepare=prepare,
            event_sink=sink,
        )

    assert prepared is False
    assert called is False
    assert local_budget.used == 0
    assert capacity.active == 0


@pytest.mark.parametrize(
    ("mode", "expected_state"),
    [
        ("success", AttemptState.SUCCEEDED),
        ("failure", AttemptState.FAILED),
        ("timeout", AttemptState.TIMED_OUT),
    ],
)
def test_started_attempt_finalizes_and_cleans_up_before_terminal(
    mode: str,
    expected_state: AttemptState,
) -> None:
    order: list[str] = []
    cleanup_calls = 0

    class _OrderSink(_RecordingAttemptSink):
        def terminal(self, *, outcome: AttemptOutcome[object]) -> None:
            assert cleanup_calls == 1
            order.append("terminal")
            super().terminal(outcome=outcome)

    def prepare(_identity: object):
        def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            order.append("cleanup")

        return cleanup

    def invoke() -> str:
        if mode == "failure":
            raise RuntimeError("expected failure")
        if mode == "timeout":
            context = current_attempt_context()
            assert context is not None
            while not context.cancelled:
                time.sleep(0.001)
            context.raise_if_cancelled()
        return "ok"

    def finalize(outcome: AttemptOutcome[str]) -> AttemptOutcome[str]:
        order.append("finalize")
        return outcome

    outcome = AttemptSupervisor(cancellation_grace_seconds=0.1).run(
        invoke,
        timeout_seconds=0.01 if mode == "timeout" else None,
        idempotency_key=f"cleanup:{mode}",
        prepare=prepare,
        finalize=finalize,
        event_sink=_OrderSink(),
    )

    assert outcome.state is expected_state
    assert cleanup_calls == 1
    assert order == ["finalize", "cleanup", "terminal"]


def test_cleanup_failure_is_terminal_indeterminate_not_success() -> None:
    sink = _RecordingAttemptSink()
    cleanup_calls = 0

    def prepare(_identity: object):
        def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1
            raise OSError("resource release failed")

        return cleanup

    outcome = AttemptSupervisor().run(
        lambda: "ok",
        timeout_seconds=None,
        idempotency_key="cleanup:failure",
        prepare=prepare,
        event_sink=sink,
    )

    assert cleanup_calls == 1
    assert outcome.state is AttemptState.INDETERMINATE
    assert outcome.indeterminate is True
    assert outcome.reason_code == "attempt_cleanup_failed"
    assert sink.events[-1] == ("terminal", AttemptState.INDETERMINATE)


def test_soft_lifecycle_sink_failure_never_blocks_authoritative_lifecycle() -> None:
    authoritative = _RecordingAttemptSink()
    soft = _RecordingAttemptSink(
        fail_started=True,
        fail_terminal=True,
        required=False,
    )

    outcome = AttemptSupervisor().run(
        lambda: "ok",
        timeout_seconds=None,
        idempotency_key="events:soft-failure",
        event_sink=CompositeAttemptLifecycleSink((authoritative, soft)),
    )

    assert outcome.state is AttemptState.SUCCEEDED
    assert [event_type for event_type, _ in authoritative.events] == [
        "started",
        "terminal",
    ]


def test_composite_rejects_multiple_authoritative_sinks() -> None:
    first = _RecordingAttemptSink()
    second = _RecordingAttemptSink(fail_started=True)

    with pytest.raises(ValueError, match="only one authoritative sink"):
        CompositeAttemptLifecycleSink((first, second))


def test_terminal_sink_failure_runs_cleanup_exactly_once() -> None:
    sink = _RecordingAttemptSink(fail_terminal=True)
    cleanup_calls = 0

    def prepare(_identity: object):
        def cleanup() -> None:
            nonlocal cleanup_calls
            cleanup_calls += 1

        return cleanup

    with pytest.raises(AttemptLifecycleEmissionError) as captured:
        AttemptSupervisor().run(
            lambda: "ok",
            timeout_seconds=None,
            idempotency_key="events:terminal-failure",
            prepare=prepare,
            event_sink=sink,
        )

    assert captured.value.details["phase"] == "terminal"
    assert cleanup_calls == 1


def test_terminal_sink_failure_rolls_back_reversible_finalization() -> None:
    sink = _RecordingAttemptSink(fail_terminal=True)
    visible = {"value": "before"}
    callbacks: list[str] = []

    def finalize(outcome: AttemptOutcome[str]) -> AttemptFinalization[str]:
        visible["value"] = "published"

        def rollback() -> None:
            callbacks.append("rollback")
            visible["value"] = "before"

        def complete() -> None:
            callbacks.append("complete")

        return AttemptFinalization(
            outcome=outcome,
            rollback=rollback,
            complete=complete,
        )

    with pytest.raises(AttemptLifecycleEmissionError):
        AttemptSupervisor().run(
            lambda: "ok",
            timeout_seconds=None,
            idempotency_key="events:terminal-rollback",
            finalize=finalize,
            event_sink=sink,
        )

    assert visible == {"value": "before"}
    assert callbacks == ["rollback"]
