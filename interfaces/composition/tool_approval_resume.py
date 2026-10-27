"""Composition-owned bridge from decided tool approvals to Graph resume.

The approval application service records decisions; this module owns which
live Graph runtime a Graph-bound tool approval belongs to and drives exactly
that run's re-entrant approval Wait.  It never decides approvals itself and
never trusts caller input for the grant: the Wait to resume is located by the
approval id recorded durably on the Wait registration.
"""

from __future__ import annotations

from threading import RLock
from typing import Callable, Protocol, runtime_checkable

from framework.events.canonical import checksum_for
from framework.harness.control_plane.graph_state import HarnessGraphState
from framework.harness.control_plane.harness import HarnessRunResult
from framework.harness.control_plane.state import HarnessRunSpec
from framework.tool.governance.approval import is_tool_approval
from framework.workers.approval import ApprovalRequest, ApprovalStatus
from interfaces.services.approval_service import (
    ApprovalGraphResumeError,
    ApprovalGraphResumeOutcome,
    ToolApprovalGraphResumePort,
)

# A consumed grant was approved and then redeemed by the re-entered node.
_APPROVED_STATUSES = frozenset(
    {ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED, ApprovalStatus.CONSUMED}
)
_DECIDED_STATUSES = _APPROVED_STATUSES | {ApprovalStatus.REJECTED}


@runtime_checkable
class HarnessGraphApprovalResumeTarget(Protocol):
    """The live Graph runtime that owns one run's approval Waits."""

    def recover_graph(self, run_spec: HarnessRunSpec) -> HarnessGraphState: ...

    def resume_after_approval(
        self,
        run_spec: HarnessRunSpec,
        *,
        approved: bool,
        approval_ref: str,
        actor_identity_scope_ref: str,
        reason: str | None = None,
        approval_id: str | None = None,
    ) -> HarnessRunResult: ...


GraphApprovalRehydrator = Callable[
    [str],
    "tuple[HarnessRunSpec, HarnessGraphApprovalResumeTarget] | None",
]


class HarnessGraphToolApprovalResumer(ToolApprovalGraphResumePort):
    """Resume the Graph run whose re-entrant Wait is parked on a tool grant."""

    def __init__(self, *, rehydrator: GraphApprovalRehydrator | None = None) -> None:
        if rehydrator is not None and not callable(rehydrator):
            raise TypeError("rehydrator must be callable")
        self._lock = RLock()
        self._runs: dict[
            str,
            tuple[HarnessRunSpec, HarnessGraphApprovalResumeTarget],
        ] = {}
        self._rehydrator = rehydrator

    def register(
        self,
        run_spec: HarnessRunSpec,
        target: HarnessGraphApprovalResumeTarget,
    ) -> None:
        """Bind one run to the runtime that executes it in this process."""

        if not isinstance(run_spec, HarnessRunSpec):
            raise TypeError("run_spec must be HarnessRunSpec")
        if not isinstance(target, HarnessGraphApprovalResumeTarget):
            raise TypeError("target must implement HarnessGraphApprovalResumeTarget")
        with self._lock:
            existing = self._runs.get(run_spec.run_id)
            if existing is not None and (
                existing[0] != run_spec or existing[1] is not target
            ):
                raise ValueError(
                    "run id is already bound to another Graph runtime"
                )
            self._runs[run_spec.run_id] = (run_spec, target)

    def resume_tool_approval(
        self,
        approval: ApprovalRequest,
    ) -> ApprovalGraphResumeOutcome:
        if not isinstance(approval, ApprovalRequest):
            raise TypeError("approval must be ApprovalRequest")
        identity = approval.graph_identity
        if not is_tool_approval(approval) or identity is None:
            raise ApprovalGraphResumeError(
                "approval is not a Graph-bound tool approval",
                code="approval_graph_unbound",
                approval_id=approval.approval_id,
            )
        status = ApprovalStatus(approval.status)
        if status not in _DECIDED_STATUSES or approval.decision is None:
            raise ApprovalGraphResumeError(
                f"approval is {status.value}; only decided approvals resume a Graph",
                code="approval_graph_resume_undecided",
                approval_id=approval.approval_id,
            )
        approved = status in _APPROVED_STATUSES
        run_spec, target = self._resolve(identity.run_id, approval.approval_id)
        state = target.recover_graph(run_spec)
        registrations = tuple(
            item
            for item in state.wait_registrations
            if item.reentrant and item.approval_id == approval.approval_id
        )
        if not registrations:
            raise ApprovalGraphResumeError(
                "no re-entrant Graph Wait is parked on this approval",
                code="approval_graph_wait_not_found",
                approval_id=approval.approval_id,
            )
        if not any(item.unresolved for item in registrations):
            return ApprovalGraphResumeOutcome(
                run_id=run_spec.run_id,
                approval_id=approval.approval_id,
                approved=approved,
                resumed=False,
            )
        decision = approval.decision
        result = target.resume_after_approval(
            run_spec,
            approved=approved,
            reason=decision.reason,
            # Deterministic per decision, so a retried resume presents the
            # identical approval cause to the Harness.
            approval_ref=checksum_for(
                {
                    "approval_id": approval.approval_id,
                    "decision": decision.to_dict(),
                }
            ),
            actor_identity_scope_ref=checksum_for(
                {"approval_decided_by": decision.decided_by}
            ),
            approval_id=approval.approval_id,
        )
        return ApprovalGraphResumeOutcome(
            run_id=run_spec.run_id,
            approval_id=approval.approval_id,
            approved=approved,
            resumed=True,
            run_status=result.status.value,
        )

    def _resolve(
        self,
        run_id: str,
        approval_id: str,
    ) -> tuple[HarnessRunSpec, HarnessGraphApprovalResumeTarget]:
        with self._lock:
            bound = self._runs.get(run_id)
        if bound is None and self._rehydrator is not None:
            rehydrated = self._rehydrator(run_id)
            if rehydrated is not None:
                self.register(*rehydrated)
                bound = rehydrated
        if bound is None or bound[0].run_id != run_id:
            raise ApprovalGraphResumeError(
                "approval Graph run has no live runtime in this process",
                code="approval_graph_run_unavailable",
                approval_id=approval_id,
            )
        return bound


__all__ = [
    "GraphApprovalRehydrator",
    "HarnessGraphApprovalResumeTarget",
    "HarnessGraphToolApprovalResumer",
]
