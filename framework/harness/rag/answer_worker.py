from __future__ import annotations

from typing import Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import GroundedAnswerCandidate, RAGContextPack
from framework.shared.graph_identity import GraphExecutionIdentity


class AnswerWorkerError(RuntimeError):
    """The worker could not produce a candidate; the controller records a failed run.

    Raised for model/provider failures and worker contract violations. It is never a
    model abstention: abstentions are returned as candidates with ``abstained=True``.
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        normalized = str(code or "").strip()
        if not normalized:
            raise HarnessValidationError("answer worker error code is required")
        super().__init__(message)
        self.code = normalized
        self.retryable = bool(retryable)

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": str(self), "retryable": self.retryable}


@runtime_checkable
class AnswerWorkerPort(Protocol):
    """Drafts grounded answer candidates from verified context packs."""

    def generate_answer(
        self,
        *,
        question: str,
        pack: RAGContextPack,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> GroundedAnswerCandidate:
        """Return an answer candidate; raise AnswerWorkerError when none can be produced.

        Controller/gates decide acceptance.
        """
        ...


__all__ = ["AnswerWorkerError", "AnswerWorkerPort"]
