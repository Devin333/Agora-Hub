"""Lazy, composition-owned child lifecycle resources for dynamic Research."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any

from framework.harness.subagents import (
    ChildAgentSupervisor,
    DurableChildAgentEventLog,
    HarnessOwnedChildAgentRuntime,
)
from framework.harness.task_plan.parallel import ParallelAgentCoordinator


RESEARCH_DYNAMIC_CHILD_RESOURCE_SCOPE = "research.dynamic-analysis.children"


class ResearchChildRuntimeUnavailableError(RuntimeError):
    """The canonical durable state ports cannot host the child lifecycle."""


@dataclass(frozen=True, slots=True)
class ResearchChildRuntimeBinding:
    supervisor: ChildAgentSupervisor
    parallel_coordinator: ParallelAgentCoordinator


class LazyResearchChildRuntime:
    """Start one durable child owner only when dynamic Research is requested."""

    def __init__(
        self,
        *,
        state_runtime: Any,
        state_reader: Any,
        max_children: int,
        state_key: str = RESEARCH_DYNAMIC_CHILD_RESOURCE_SCOPE,
        runtime_event_sink: Any | None = None,
    ) -> None:
        if isinstance(max_children, bool) or not isinstance(max_children, int):
            raise TypeError("max_children must be an integer")
        if max_children < 1:
            raise ValueError("max_children must be positive")
        if not isinstance(state_key, str) or not state_key.strip():
            raise ValueError("state_key is required")
        if runtime_event_sink is not None and not callable(runtime_event_sink) and not any(
            hasattr(runtime_event_sink, name) for name in ("append", "publish")
        ):
            raise TypeError(
                "runtime_event_sink must be callable or expose append/publish"
            )
        normalized_state_key = state_key.strip()
        self._state_runtime = state_runtime
        self._state_reader = state_reader
        self._max_children = max_children
        self._state_key = normalized_state_key
        self._runtime_event_sink = runtime_event_sink
        self._lock = RLock()
        self._owned_runtime: HarnessOwnedChildAgentRuntime | None = None
        self._binding: ResearchChildRuntimeBinding | None = None
        self._closed = False

    @property
    def started(self) -> bool:
        with self._lock:
            return self._binding is not None

    def start_for_run(
        self,
        *,
        run_id: str,
        tenant_id: str,
    ) -> ResearchChildRuntimeBinding:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id is required")
        normalized_run_id = run_id.strip()
        normalized_tenant_id = tenant_id.strip()

        with self._lock:
            if self._closed:
                raise RuntimeError("Research child runtime is closed")
            if self._binding is None:
                self._start()
            assert self._owned_runtime is not None
            assert self._binding is not None
            self._owned_runtime.register_run_scope(
                normalized_run_id,
                normalized_tenant_id,
            )
            return self._binding

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            owned_runtime = self._owned_runtime
            self._owned_runtime = None
            self._binding = None
        if owned_runtime is not None:
            owned_runtime.close()

    def _start(self) -> None:
        try:
            event_log = DurableChildAgentEventLog(
                state_runtime=self._state_runtime,
                state_reader=self._state_reader,
                state_key=self._state_key,
            )
        except (TypeError, ValueError) as exc:
            raise ResearchChildRuntimeUnavailableError(
                "Dynamic Research requires canonical durable lifecycle state ports"
            ) from exc

        owned_runtime = HarnessOwnedChildAgentRuntime(
            event_log=event_log,
            max_children=self._max_children,
            runtime_event_sink=self._runtime_event_sink,
        )
        try:
            owned_runtime.start()
            supervisor = owned_runtime.supervisor
            binding = ResearchChildRuntimeBinding(
                supervisor=supervisor,
                parallel_coordinator=ParallelAgentCoordinator(
                    max_workers=supervisor.capacity,
                    child_supervisor=supervisor,
                ),
            )
        except BaseException:
            try:
                owned_runtime.close()
            except BaseException:
                # Preserve the owner/recovery diagnostic that caused startup
                # to fail; cleanup is best effort at this boundary.
                pass
            raise
        self._owned_runtime = owned_runtime
        self._binding = binding


__all__ = [
    "LazyResearchChildRuntime",
    "RESEARCH_DYNAMIC_CHILD_RESOURCE_SCOPE",
    "ResearchChildRuntimeBinding",
    "ResearchChildRuntimeUnavailableError",
]
