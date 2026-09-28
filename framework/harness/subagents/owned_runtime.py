"""Own a durable child admission scope for the lifetime of a composition."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Event, RLock, Thread
from typing import Any
from uuid import uuid4

from framework.harness.subagents.supervisor import (
    ChildAgentSupervisor,
    ChildAgentSupervisorError,
)
from framework.harness.subagents.supervisor_store import DurableChildAgentEventLog


_DEFAULT_RENEWAL = object()


class HarnessOwnedChildAgentRuntime:
    """Acquire, recover, renew, and close one fenced supervisor resource.

    Lease takeover only changes the controller. Recovered unconfirmed workers
    still occupy capacity, and recovery never invokes them.
    """

    def __init__(
        self,
        *,
        event_log: DurableChildAgentEventLog,
        max_children: int,
        owner_id: str | None = None,
        clock: Callable[[], datetime] | None = None,
        renewal_interval_seconds: float | None | object = _DEFAULT_RENEWAL,
        runtime_event_sink: Any | None = None,
    ) -> None:
        if (
            isinstance(max_children, bool)
            or not isinstance(max_children, int)
            or max_children < 1
        ):
            raise ValueError("max_children must be a positive integer")
        if owner_id is not None and (
            not isinstance(owner_id, str) or not owner_id.strip()
        ):
            raise ValueError("owner_id must be nonempty text")
        interval = (
            event_log.owner_lease_seconds / 3
            if renewal_interval_seconds is _DEFAULT_RENEWAL
            else renewal_interval_seconds
        )
        if interval is not None and (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0 < interval < event_log.owner_lease_seconds
        ):
            raise ValueError(
                "renewal interval must be positive and shorter than the owner lease"
            )
        if runtime_event_sink is not None and not callable(runtime_event_sink) and not any(
            hasattr(runtime_event_sink, name) for name in ("append", "publish")
        ):
            raise TypeError(
                "runtime_event_sink must be callable or expose append/publish"
            )
        self._event_log = event_log
        self._max_children = max_children
        self._owner_id = owner_id or f"child-runtime-{uuid4().hex}"
        self._clock = clock
        self._renewal_interval = interval
        self._runtime_event_sink = runtime_event_sink
        self._lock = RLock()
        self._stop = Event()
        self._renewer: Thread | None = None
        self._renewal_error: Exception | None = None
        self._supervisor: ChildAgentSupervisor | None = None
        self._closed = False

    @property
    def supervisor(self) -> ChildAgentSupervisor:
        with self._lock:
            if self._closed or self._supervisor is None:
                raise ChildAgentSupervisorError(
                    "child runtime is not started", code="child_owner_not_started"
                )
            self._assert_owner()
            return self._supervisor

    def start(self) -> None:
        with self._lock:
            if self._closed:
                raise ChildAgentSupervisorError(
                    "child runtime is closed", code="child_admission_closed"
                )
            if self._supervisor is not None:
                self._assert_owner()
                return
            self._event_log.acquire_owner(self._owner_id)
            supervisor = ChildAgentSupervisor(
                event_sink=self._event_log,
                event_reader=self._event_log,
                owner_guard=self._assert_owner,
                max_children=self._max_children,
                clock=self._clock,
                runtime_event_sink=self._runtime_event_sink,
            )
            try:
                supervisor.recover()
                self._assert_owner()
            except BaseException:
                supervisor.shutdown(wait=False)
                try:
                    self._event_log.release_owner()
                except ChildAgentSupervisorError:
                    pass
                raise
            self._supervisor = supervisor
            if self._renewal_interval is not None:
                self._renewer = Thread(
                    target=self._renew, name="newsroom-child-owner", daemon=True
                )
                self._renewer.start()

    def register_run_scope(self, run_id: str, tenant_id: str) -> None:
        with self._lock:
            if self._closed or self._supervisor is None:
                raise ChildAgentSupervisorError(
                    "child runtime is not started", code="child_owner_not_started"
                )
            self._assert_owner()
            self._event_log.register_run_scope(run_id, tenant_id)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            supervisor = self._supervisor
        # Do not hold the runtime lock while cancelling or joining: worker
        # settlement uses the supervisor lock and the durable owner guard.
        error: Exception | None = None
        try:
            if supervisor is not None:
                supervisor.stop_admission()
                try:
                    supervisor.cancel_active()
                except ChildAgentSupervisorError as exc:
                    if exc.code != "child_owner_lost":
                        error = exc
                finally:
                    supervisor.shutdown(wait=False)
        finally:
            self._stop.set()
            if self._renewer is not None:
                self._renewer.join()
        if supervisor is not None and error is None:
            try:
                self._event_log.release_owner()
            except ChildAgentSupervisorError as exc:
                if exc.code != "child_owner_lost":
                    error = exc
        if error is not None:
            raise error

    def _assert_owner(self) -> None:
        if self._renewal_error is not None:
            raise ChildAgentSupervisorError(
                "child owner renewal failed", code="child_owner_lost"
            ) from self._renewal_error
        self._event_log.assert_owner()

    def _renew(self) -> None:
        while not self._stop.wait(self._renewal_interval):
            try:
                self._event_log.renew_owner()
            except Exception as exc:
                self._renewal_error = exc
                return


__all__ = ["HarnessOwnedChildAgentRuntime"]
