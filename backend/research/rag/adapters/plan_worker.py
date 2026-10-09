from __future__ import annotations

import math
from typing import Any

from framework.harness.workers.result import HarnessWorkerResult, HarnessWorkerStatus
from framework.shared.graph_identity import GraphExecutionIdentity

from backend.research.ports.llm_worker import ResearchCandidateWorkerPort


class ResearchRAGPlanWorker:
    """Adapts Research candidate workers to the Harness RAG planner worker shape."""

    def __init__(self, worker: ResearchCandidateWorkerPort) -> None:
        self._worker = worker

    def generate(
        self,
        request: dict[str, Any],
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> HarnessWorkerResult:
        kwargs: dict[str, Any] = {}
        try:
            timeout_seconds = _optional_timeout(request.get("timeout_seconds"))
            max_transport_attempts = _optional_attempts(
                request.get("max_transport_attempts")
            )
        except (TypeError, ValueError):
            return _failed("worker_failed")
        if execution_identity is not None:
            kwargs["execution_identity"] = execution_identity
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = timeout_seconds
        if max_transport_attempts is not None:
            kwargs["max_transport_attempts"] = max_transport_attempts
        try:
            output = self._worker.generate_candidate(
                task="rag_plan_candidate",
                payload=request,
                **kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - outbound adapter boundary
            return _failed(_worker_failure_code(exc))
        if not isinstance(output, dict):
            return HarnessWorkerResult(
                status=HarnessWorkerStatus.FAILED,
                output={},
                error="Research candidate worker returned a non-dict planner payload",
            )
        return HarnessWorkerResult(
            status=HarnessWorkerStatus.SUCCEEDED,
            output=output,
        )


def _optional_attempts(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("max_transport_attempts must be an integer")
    if value <= 0:
        raise ValueError("max_transport_attempts must be greater than zero")
    return value


def _optional_timeout(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("timeout_seconds must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    return parsed


def _worker_failure_code(exc: BaseException) -> str:
    visited: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, TimeoutError):
            return "worker_timeout"
        if getattr(current, "failure_code", None) == "worker_timeout":
            return "worker_timeout"
        if getattr(current, "error_type", None) == "timeout":
            return "worker_timeout"
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return "worker_failed"


def _failed(failure_code: str) -> HarnessWorkerResult:
    return HarnessWorkerResult(
        status=HarnessWorkerStatus.FAILED,
        output={},
        diagnostics={"failure_code": failure_code},
    )


__all__ = ["ResearchRAGPlanWorker"]
