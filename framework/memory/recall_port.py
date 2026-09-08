"""Execution-bound, read-only memory capability supplied by the flow controller."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from framework.memory.models import MemoryQuery, MemoryRecallResult
from framework.memory.policy import MemoryPolicy
from framework.shared.graph_identity import GraphExecutionIdentity


@runtime_checkable
class ExecutionMemoryRecallPort(Protocol):
    @property
    def execution_identity(self) -> GraphExecutionIdentity: ...

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None: ...

    def recall(
        self,
        query: MemoryQuery | dict[str, Any] | str,
        *,
        policy: MemoryPolicy | None = None,
    ) -> MemoryRecallResult: ...
