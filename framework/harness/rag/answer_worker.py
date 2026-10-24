from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from framework.harness.control_plane.errors import HarnessValidationError
from framework.shared.redaction import redact_sensitive_values
from framework.harness.rag.models import GroundedAnswerCandidate, RAGContextPack
from framework.shared.graph_identity import GraphExecutionIdentity


class AnswerWorkerError(RuntimeError):
    """The worker could not produce a candidate; the controller records a failed run.

    Raised for model/provider failures and worker contract violations. It is never a
    model abstention: abstentions are returned as candidates with ``abstained=True``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, object] | None = None,
    ) -> None:
        normalized = str(code or "").strip()
        if not normalized:
            raise HarnessValidationError("answer worker error code is required")
        super().__init__(message)
        self.code = normalized
        self.retryable = bool(retryable)
        self.details = (
            dict(redact_sensitive_values(dict(details)))
            if isinstance(details, Mapping)
            else {}
        )

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code, "message": str(self), "retryable": self.retryable}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


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
