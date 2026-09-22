"""Invocation-local control for one admitted, supervised child execution.

The supervisor remains the lifecycle owner.  This module only projects its
already-admitted handle, lease and reservation into the cooperative attempt
context consumed by the child execution path.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from threading import Event
from time import monotonic
from typing import Callable, Iterator, Mapping

from framework.harness.control_plane.budget_reservation import BudgetReservation
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.subagents.supervisor import (
    ChildAgentHandle,
    ChildAgentSpawnRequest,
    ChildAgentState,
)
from framework.harness.task_plan.models import TaskInstance
from framework.shared.attempts import AttemptContext, ExecutionLimits
from framework.shared.graph_identity import GraphExecutionIdentity


_CURRENT_CHILD_EXECUTION_CONTROL: contextvars.ContextVar[ChildExecutionControl | None] = (
    contextvars.ContextVar("harness_child_execution_control", default=None)
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _require_ms(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HarnessValidationError(
            f"{name} must be a positive integer",
            code="CHILD_EXECUTION_CONTROL_INVALID",
        )
    return value


@dataclass(frozen=True, slots=True)
class ChildExecutionControl:
    """The live authority for exactly one supervisor worker invocation."""

    handle: ChildAgentHandle
    spawn_request: ChildAgentSpawnRequest
    task_instance: TaskInstance
    reservation: BudgetReservation
    group_absolute_deadline_ms: int
    effective_deadline: float
    attempt_context: AttemptContext
    cancel_event: Event
    _clock: Callable[[], float]

    @classmethod
    def create(
        cls,
        *,
        handle: ChildAgentHandle,
        spawn_request: ChildAgentSpawnRequest,
        task_instance: TaskInstance,
        reservation: BudgetReservation | Mapping[str, object],
        group_absolute_deadline_ms: int,
        clock: Callable[[], float] = monotonic,
        utc_clock: Callable[[], datetime] = _utc_now,
    ) -> "ChildExecutionControl":
        if not isinstance(handle, ChildAgentHandle):
            raise TypeError("handle must be ChildAgentHandle")
        if not isinstance(spawn_request, ChildAgentSpawnRequest):
            raise TypeError("spawn_request must be ChildAgentSpawnRequest")
        if not isinstance(task_instance, TaskInstance):
            raise TypeError("task_instance must be TaskInstance")
        if not callable(clock) or not callable(utc_clock):
            raise TypeError("clock and utc_clock must be callable")
        deadline_ms = _require_ms(group_absolute_deadline_ms, "group_absolute_deadline_ms")
        parsed = (
            reservation
            if isinstance(reservation, BudgetReservation)
            else BudgetReservation.from_dict(reservation)
        )
        cls._validate_identity(handle, spawn_request, task_instance, parsed)
        now_utc = utc_clock()
        if not isinstance(now_utc, datetime) or now_utc.tzinfo is None:
            raise TypeError("utc_clock must return an aware datetime")
        now_utc = now_utc.astimezone(UTC)
        if handle.state is not ChildAgentState.RUNNING:
            raise HarnessValidationError(
                "child execution control requires a running supervisor handle",
                code="CHILD_EXECUTION_HANDLE_NOT_RUNNING",
            )
        if handle.lease.is_expired(now_utc):
            raise HarnessValidationError(
                "child lease expired before execution began",
                code="CHILD_EXECUTION_LEASE_EXPIRED",
            )
        now = float(clock())
        if not isfinite(now):
            raise TypeError("clock must return a finite monotonic value")
        # Convert durable UTC deadlines once at invocation start.  This does
        # not renew a lease and preserves the earliest already-admitted bound.
        lease_deadline = now + (handle.lease.expires_at - now_utc).total_seconds()
        group_deadline = now + (deadline_ms / 1000.0 - now_utc.timestamp())
        attempt_deadline = now + parsed.attempt_allocation["time_limit_ms"] / 1000.0
        effective = min(lease_deadline, group_deadline, attempt_deadline)
        if not isfinite(effective):
            raise HarnessValidationError(
                "child execution requires a finite effective deadline",
                code="CHILD_EXECUTION_CONTROL_INVALID",
            )
        cancellation = Event()
        limits = ExecutionLimits(
            execution_id=handle.operation_id,
            hard_deadline=effective,
            cancel_event=cancellation,
        )
        context = AttemptContext.create(
            attempt_id=task_instance.task_instance_id,
            idempotency_key=task_instance.idempotency_key,
            operation_id=handle.operation_id,
            operation_kind="supervised_child",
            local_attempt_no=task_instance.attempt,
            execution_limits=limits,
            deadline=effective,
            cancel_event=cancellation,
            clock=clock,
        )
        return cls(
            handle,
            spawn_request,
            task_instance,
            parsed,
            deadline_ms,
            effective,
            context,
            cancellation,
            clock,
        )

    @staticmethod
    def _validate_identity(
        handle: ChildAgentHandle,
        spawn: ChildAgentSpawnRequest,
        instance: TaskInstance,
        reservation: BudgetReservation,
    ) -> None:
        mismatches = {
            name: (getattr(handle, name), getattr(instance, name))
            for name in ("stage_id", "task_id", "task_instance_id", "attempt")
            if getattr(handle, name) != getattr(instance, name)
        }
        parent = handle.parent_graph_identity
        for name in (
            "run_id",
            "graph_id",
            "graph_version",
            "graph_ref",
            "graph_checksum",
        ):
            if getattr(parent, name) != getattr(instance, name):
                mismatches[name] = (getattr(parent, name), getattr(instance, name))
        spawn_fields = (
            "child_id",
            "parent_graph_identity",
            "stage_id",
            "task_id",
            "task_instance_id",
            "attempt",
            "allowed_tools",
            "allowed_memory_namespaces",
            "budget",
            "transcript_ref",
            "operation_id",
        )
        for name in spawn_fields:
            expected = getattr(spawn, name)
            actual = getattr(handle, name)
            if name == "child_id" and expected is None:
                continue
            if name == "budget":
                expected, actual = dict(expected), dict(actual)
            if expected != actual:
                mismatches[f"spawn.{name}"] = (expected, actual)
        if reservation.reservation_key != handle.operation_id:
            mismatches["reservation_key"] = (reservation.reservation_key, handle.operation_id)
        if reservation.to_dict() != dict(handle.budget):
            mismatches["reservation"] = ("reservation differs from handle budget", "handle budget")
        if mismatches:
            raise HarnessValidationError(
                "child execution control identity differs from admitted handle",
                code="CHILD_EXECUTION_CONTROL_IDENTITY_MISMATCH",
                details={"fields": sorted(mismatches)},
            )

    def matches(
        self,
        *,
        task_instance: TaskInstance,
        parent_graph_identity: GraphExecutionIdentity,
    ) -> bool:
        return (
            isinstance(task_instance, TaskInstance)
            and isinstance(parent_graph_identity, GraphExecutionIdentity)
            and task_instance.to_dict() == self.task_instance.to_dict()
            and parent_graph_identity == self.handle.parent_graph_identity
        )

    def request_cancel(self) -> None:
        """Request cooperative cancellation; this never confirms termination."""
        self.cancel_event.set()

    def mark_descendant_indeterminate(self) -> None:
        self.attempt_context.mark_descendant_indeterminate()

    def mark_descendant_unconfirmed(self) -> None:
        self.attempt_context.mark_descendant_unconfirmed()

    def raise_if_active(self) -> None:
        if self.cancel_event.is_set() or self.attempt_context.cancelled:
            raise HarnessValidationError(
                "child execution is cancelled",
                code="CHILD_EXECUTION_CANCELLED",
            )
        if self._clock() >= self.effective_deadline:
            self.cancel_event.set()
            raise HarnessValidationError(
                "child execution deadline has elapsed",
                code="CHILD_EXECUTION_DEADLINE_EXCEEDED",
            )
        self.attempt_context.raise_if_indeterminate()


@contextmanager
def bind_child_execution_control(
    control: ChildExecutionControl,
) -> Iterator[ChildExecutionControl]:
    if not isinstance(control, ChildExecutionControl):
        raise TypeError("control must be ChildExecutionControl")
    from framework.shared.attempts import bind_attempt_context

    token = _CURRENT_CHILD_EXECUTION_CONTROL.set(control)
    try:
        with bind_attempt_context(control.attempt_context):
            yield control
    finally:
        _CURRENT_CHILD_EXECUTION_CONTROL.reset(token)


def current_child_execution_control() -> ChildExecutionControl | None:
    return _CURRENT_CHILD_EXECUTION_CONTROL.get()


def require_current_child_execution_control(
    *,
    task_instance: TaskInstance,
    parent_graph_identity: GraphExecutionIdentity,
) -> ChildExecutionControl:
    control = current_child_execution_control()
    if control is None:
        raise HarnessValidationError(
            "production child invocation has no live execution control",
            code="CHILD_EXECUTION_CONTROL_REQUIRED",
        )
    if not control.matches(
        task_instance=task_instance,
        parent_graph_identity=parent_graph_identity,
    ):
        raise HarnessValidationError(
            "live child execution control identity mismatch",
            code="CHILD_EXECUTION_CONTROL_IDENTITY_MISMATCH",
        )
    return control


__all__ = [
    "ChildExecutionControl",
    "bind_child_execution_control",
    "current_child_execution_control",
    "require_current_child_execution_control",
]
