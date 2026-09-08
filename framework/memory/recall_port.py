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
    ) -> MemoryRecallResult:
        """Return verified records with admission lineage in diagnostics.

        input_snapshot_ref and execution_identity identify the grant;
        record_namespace_refs maps returned record IDs to exact revisions,
        whose checksums are in namespace_checksums.
        """
        ...
