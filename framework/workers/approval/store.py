from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from typing import Protocol

from framework.workers.approval.model import (
    ApprovalDecision,
    ApprovalNotFoundError,
    ApprovalRequest,
    ApprovalStatus,
)


class ApprovalStore(Protocol):
    def list_approvals(self, *, status: ApprovalStatus | str | None = None) -> list[ApprovalRequest]: ...

    def get_approval(self, approval_id: str) -> ApprovalRequest: ...

    def upsert_approval(self, request: ApprovalRequest) -> ApprovalRequest: ...

    def record_decision(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
    ) -> ApprovalRequest: ...

    def consume_approval(
        self,
        approval_id: str,
        *,
        consumed_by: str,
        consumed_at: datetime | None = None,
    ) -> ApprovalRequest:
        """Atomically redeem an approved request as a single-use execution grant.

        Raises ApprovalNotConsumableError (or ApprovalExpiredError) when the
        request is not in a consumable state at the moment of redemption.
        """
        ...


class InMemoryApprovalStore:
    def __init__(
        self,
        requests: list[ApprovalRequest] | None = None,
        *,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._requests = {request.approval_id: request for request in requests or []}
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    def create(self, request: ApprovalRequest) -> ApprovalRequest:
        return self.upsert_approval(request)

    def get(self, approval_id: str) -> ApprovalRequest:
        return self.get_approval(approval_id)

    def decide(self, approval_id: str, decision: ApprovalDecision) -> ApprovalRequest:
        return self.record_decision(approval_id, decision=decision)

    def list_approvals(self, *, status: ApprovalStatus | str | None = None) -> list[ApprovalRequest]:
        records = sorted(self._requests.values(), key=lambda request: request.created_at)
        if status is None:
            return records
        actual_status = ApprovalStatus(status)
        return [request for request in records if request.status == actual_status]

    def get_approval(self, approval_id: str) -> ApprovalRequest:
        try:
            return self._requests[approval_id]
        except KeyError as exc:
            raise ApprovalNotFoundError(approval_id) from exc

    def upsert_approval(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._lock:
            self._requests[request.approval_id] = request
        return request

    def record_decision(
        self,
        approval_id: str,
        *,
        decision: ApprovalDecision,
    ) -> ApprovalRequest:
        with self._lock:
            request = self.get_approval(approval_id)
            decided = request.with_decision(decision)
            self._requests[approval_id] = decided
        return decided

    def consume_approval(
        self,
        approval_id: str,
        *,
        consumed_by: str,
        consumed_at: datetime | None = None,
    ) -> ApprovalRequest:
        with self._lock:
            request = self.get_approval(approval_id)
            consumed = request.with_consumption(
                consumed_by=consumed_by,
                consumed_at=consumed_at or self.now_fn(),
            )
            self._requests[approval_id] = consumed
        return consumed
