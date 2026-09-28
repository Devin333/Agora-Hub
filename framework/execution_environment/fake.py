from __future__ import annotations

from collections.abc import Callable

from framework.execution_environment.models import (
    ExecutionCapabilityProfile,
    ExecutionOutcome,
    ExecutionRequest,
)
from framework.execution_environment.ports import ExecutionCancellationSignal


class FakeExecutionEnvironment:
    """Contract fake; it never claims physical isolation outside tests."""

    def __init__(
        self,
        capabilities: ExecutionCapabilityProfile,
        outcome_factory: Callable[[ExecutionRequest], ExecutionOutcome],
    ) -> None:
        self._capabilities = capabilities
        self._outcome_factory = outcome_factory
        self.requests: list[ExecutionRequest] = []
        self.cancellation_signals: list[ExecutionCancellationSignal | None] = []

    @property
    def capabilities(self) -> ExecutionCapabilityProfile:
        return self._capabilities

    def execute(
        self,
        request: ExecutionRequest,
        *,
        cancellation: ExecutionCancellationSignal | None = None,
    ) -> ExecutionOutcome:
        self.requests.append(request)
        self.cancellation_signals.append(cancellation)
        return self._outcome_factory(request)


__all__ = ["FakeExecutionEnvironment"]
