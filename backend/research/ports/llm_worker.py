from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.research.ports.reader_repair_candidate import (
    READER_REPAIR_APPLICATION_OBSERVATION_TASK,
    READER_REPAIR_PATCH_CANDIDATE_TASK,
)
from framework.shared.graph_identity import GraphExecutionIdentity


@runtime_checkable
class ResearchCandidateWorkerPort(Protocol):
    def generate_candidate(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        execution_identity: GraphExecutionIdentity | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
    ) -> dict[str, Any]:
        ...


__all__ = [
    "READER_REPAIR_APPLICATION_OBSERVATION_TASK",
    "READER_REPAIR_PATCH_CANDIDATE_TASK",
    "ResearchCandidateWorkerPort",
]
