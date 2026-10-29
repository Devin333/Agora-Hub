from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from framework.tool.governance.approval import is_tool_approval
from framework.workers.approval import (
    ApprovalDecision,
    ApprovalDecisionType,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
)
from infrastructure.storage.local_json import LocalJsonApprovalStore


DEFAULT_APPROVAL_STORE_PATH = ".newsroom/approvals/approvals.json"


@dataclass(frozen=True)
class ApprovalListResult:
    approvals: tuple[ApprovalRequest, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_count": len(self.approvals),
            "approvals": [approval.to_dict() for approval in self.approvals],
        }


@dataclass(frozen=True)
class ApprovalGraphResumeOutcome:
    """What deciding a Graph-bound tool approval did to its Graph run."""

    run_id: str
    approval_id: str
    approved: bool
    resumed: bool
    run_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "approval_id": self.approval_id,
            "approved": self.approved,
            "resumed": self.resumed,
            "run_status": self.run_status,
        }


@runtime_checkable
class ToolApprovalGraphResumePort(Protocol):
    """Drive the Graph run parked on a decided tool approval.

    Implementations must be idempotent: resuming an approval whose Wait was
    already resolved reports ``resumed=False`` instead of driving the run a
    second time.
    """

    def resume_tool_approval(
        self,
        approval: ApprovalRequest,
    ) -> ApprovalGraphResumeOutcome: ...


class ApprovalGraphResumeError(RuntimeError):
    """A decided approval could not be applied to its Graph run.

    The decision itself is durable; ``ApprovalApplicationService.resume_graph``
    retries the Graph resume without deciding again.
    """

    def __init__(self, message: str, *, code: str, approval_id: str) -> None:
        super().__init__(message)
        self.code = code
        self.approval_id = approval_id


@dataclass(frozen=True)
class ApprovalDetailResult:
    approval: ApprovalRequest
    graph_resume: ApprovalGraphResumeOutcome | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "approval_id": self.approval.approval_id,
            "approval": self.approval.to_dict(),
        }
        if self.graph_resume is not None:
            payload["graph_resume"] = self.graph_resume.to_dict()
        return payload


class ApprovalApplicationService:
    def __init__(
        self,
        *,
        store: ApprovalStore | None = None,
        store_path: str | Path = DEFAULT_APPROVAL_STORE_PATH,
        graph_resume: ToolApprovalGraphResumePort | None = None,
    ) -> None:
        if graph_resume is not None and not isinstance(
            graph_resume,
            ToolApprovalGraphResumePort,
        ):
            raise TypeError("graph_resume must implement ToolApprovalGraphResumePort")
        self.store = store or LocalJsonApprovalStore(store_path)
        self._graph_resume = graph_resume

    def submit_request(
        self,
        *,
        requested_action: str,
        risk_level: str = "medium",
        reason: str | None = None,
        payload: dict[str, Any] | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        requested_by: str | None = None,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ApprovalDetailResult:
        request = ApprovalRequest(
            requested_action=requested_action,
            risk_level=risk_level,
            reason=reason,
            payload=payload or {},
            task_id=task_id,
            run_id=run_id,
            requested_by=requested_by,
            expires_at=expires_at,
            metadata=metadata or {},
        )
        return ApprovalDetailResult(self.store.upsert_approval(request))

    def list_approvals(self, *, status: ApprovalStatus | str | None = None) -> ApprovalListResult:
        return ApprovalListResult(tuple(self.store.list_approvals(status=status)))

    def get_approval(self, approval_id: str) -> ApprovalDetailResult:
        return ApprovalDetailResult(self.store.get_approval(approval_id))

    def approve(
        self,
        approval_id: str,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> ApprovalDetailResult:
        return self._record_decision(
            approval_id,
            decision_type=ApprovalDecisionType.APPROVE,
            decided_by=decided_by,
            reason=reason,
        )

    def reject(
        self,
        approval_id: str,
        *,
        decided_by: str,
        reason: str | None = None,
    ) -> ApprovalDetailResult:
        return self._record_decision(
            approval_id,
            decision_type=ApprovalDecisionType.REJECT,
            decided_by=decided_by,
            reason=reason,
        )

    def modify(
        self,
        approval_id: str,
        *,
        decided_by: str,
        modifications: dict[str, Any],
        reason: str | None = None,
    ) -> ApprovalDetailResult:
        return self._record_decision(
            approval_id,
            decision_type=ApprovalDecisionType.MODIFY,
            decided_by=decided_by,
            reason=reason,
            modifications=modifications,
        )

    def resume_graph(self, approval_id: str) -> ApprovalDetailResult:
        """Re-apply an already decided Graph-bound tool approval to its run.

        Used after a decision was recorded but driving the Graph failed; the
        resume port is idempotent, so a Wait resolved meanwhile is not driven
        again.
        """

        approval = self.store.get_approval(approval_id)
        if not _resumes_graph(approval):
            raise ApprovalGraphResumeError(
                "approval is not bound to a Graph run",
                code="approval_graph_unbound",
                approval_id=approval.approval_id,
            )
        if approval.decision is None:
            raise ApprovalGraphResumeError(
                "approval has no decision to apply",
                code="approval_graph_resume_undecided",
                approval_id=approval.approval_id,
            )
        return ApprovalDetailResult(approval, self._resume(approval))

    def _record_decision(
        self,
        approval_id: str,
        *,
        decision_type: ApprovalDecisionType,
        decided_by: str,
        reason: str | None = None,
        modifications: dict[str, Any] | None = None,
    ) -> ApprovalDetailResult:
        decision = ApprovalDecision(
            decision_type=decision_type,
            decided_by=decided_by,
            reason=reason,
            modifications=modifications or {},
        )
        decided = self.store.record_decision(
            approval_id,
            decision=decision,
        )
        if not _resumes_graph(decided):
            return ApprovalDetailResult(decided)
        return ApprovalDetailResult(decided, self._resume(decided))

    def _resume(self, approval: ApprovalRequest) -> ApprovalGraphResumeOutcome:
        if self._graph_resume is None:
            raise ApprovalGraphResumeError(
                "Graph-bound tool approval has no Graph resume runtime",
                code="approval_graph_resume_unavailable",
                approval_id=approval.approval_id,
            )
        outcome = self._graph_resume.resume_tool_approval(approval)
        if (
            not isinstance(outcome, ApprovalGraphResumeOutcome)
            or outcome.approval_id != approval.approval_id
        ):
            raise ApprovalGraphResumeError(
                "Graph resume runtime returned an invalid outcome",
                code="approval_graph_resume_invalid",
                approval_id=approval.approval_id,
            )
        return outcome


def _resumes_graph(approval: ApprovalRequest) -> bool:
    """Only tool approvals requested inside a Graph run park a Graph Wait."""

    return is_tool_approval(approval) and approval.graph_identity is not None
