from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.shared.json import to_jsonable
from framework.shared.graph_identity import GraphExecutionIdentity


class MemoryWriteStatus(StrEnum):
    PROPOSED = "proposed"


@dataclass(frozen=True)
class MemoryWriteCandidate:
    candidate_id: str
    namespace: str
    content: dict[str, Any]
    source_refs: tuple[str, ...] = ()
    status: MemoryWriteStatus | str = MemoryWriteStatus.PROPOSED
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.candidate_id).strip():
            raise HarnessValidationError("candidate_id is required")
        if not str(self.namespace).strip():
            raise HarnessValidationError("namespace is required")
        object.__setattr__(self, "content", dict(self.content))
        object.__setattr__(self, "source_refs", tuple(str(ref) for ref in self.source_refs))
        object.__setattr__(self, "status", MemoryWriteStatus(self.status))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "namespace": self.namespace,
            "content": to_jsonable(self.content),
            "source_refs": list(self.source_refs),
            "status": self.status.value,
            "metadata": to_jsonable(self.metadata),
        }


@runtime_checkable
class MemoryPort(Protocol):
    def recall(self, request: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        ...

    def propose_write(self, candidate: MemoryWriteCandidate) -> MemoryWriteCandidate:
        ...


@runtime_checkable
class ExecutionBoundMemoryPort(MemoryPort, Protocol):
    @property
    def execution_identity(self) -> GraphExecutionIdentity: ...

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None: ...


__all__ = ["ExecutionBoundMemoryPort", "MemoryPort", "MemoryWriteCandidate", "MemoryWriteStatus"]
