from __future__ import annotations

from typing import Protocol, runtime_checkable

from framework.execution_environment.models import (
    ExecutionCapabilityProfile,
    ExecutionOutcome,
    ExecutionRequest,
)


@runtime_checkable
class ExecutionCancellationSignal(Protocol):
    """Process-local cancellation authority supplied by the Harness.

    The signal is deliberately separate from ``ExecutionRequest`` because it
    is mutable control-plane state, not versioned or replayable request data.
    """

    def is_set(self) -> bool: ...


@runtime_checkable
class ExecutionEnvironmentPort(Protocol):
    """Provider boundary for one physically isolated process invocation."""

    @property
    def capabilities(self) -> ExecutionCapabilityProfile: ...

    def execute(
        self,
        request: ExecutionRequest,
        *,
        cancellation: ExecutionCancellationSignal | None = None,
    ) -> ExecutionOutcome: ...


__all__ = ["ExecutionCancellationSignal", "ExecutionEnvironmentPort"]
