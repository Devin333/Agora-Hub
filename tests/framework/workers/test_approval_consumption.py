from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest

from framework.workers.approval import (
    CONSUMABLE_APPROVAL_STATUSES,
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalNotConsumableError,
    ApprovalRequest,
    ApprovalStatus,
    InMemoryApprovalStore,
)

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _request(**overrides: object) -> ApprovalRequest:
    values: dict[str, object] = {
        "approval_id": "appr-1",
        "requested_action": "tool:sample.publish",
        "risk_level": "high",
        "created_at": _CREATED,
    }
    values.update(overrides)
    return ApprovalRequest(**values)  # type: ignore[arg-type]


def _approved_store(**overrides: object) -> InMemoryApprovalStore:
    store = InMemoryApprovalStore(now_fn=lambda: _CREATED + timedelta(seconds=1))
    store.upsert_approval(_request(**overrides))
    store.record_decision(
        "appr-1",
        decision=ApprovalDecision(decision_type="approve", decided_by="reviewer"),
    )
    return store


def test_consumable_statuses_are_positive_decisions_only() -> None:
    assert CONSUMABLE_APPROVAL_STATUSES == {ApprovalStatus.APPROVED, ApprovalStatus.MODIFIED}


def test_consume_marks_approval_consumed_once() -> None:
    store = _approved_store()

    consumed = store.consume_approval("appr-1", consumed_by="call-1")

    assert consumed.status == ApprovalStatus.CONSUMED
    assert consumed.consumed_by == "call-1"
    assert consumed.consumed_at == _CREATED + timedelta(seconds=1)
    assert consumed.decision is not None
    with pytest.raises(ApprovalNotConsumableError):
        store.consume_approval("appr-1", consumed_by="call-2")
    assert store.get_approval("appr-1").consumed_by == "call-1"


def test_pending_and_rejected_approvals_cannot_be_consumed() -> None:
    store = InMemoryApprovalStore()
    store.upsert_approval(_request())
    with pytest.raises(ApprovalNotConsumableError):
        store.consume_approval("appr-1", consumed_by="call-1")

    store.record_decision(
        "appr-1",
        decision=ApprovalDecision(decision_type="reject", decided_by="reviewer"),
    )
    with pytest.raises(ApprovalNotConsumableError):
        store.consume_approval("appr-1", consumed_by="call-1")


def test_expired_approval_cannot_be_consumed() -> None:
    store = _approved_store(expires_at=_CREATED + timedelta(seconds=10))

    with pytest.raises(ApprovalExpiredError):
        store.consume_approval(
            "appr-1",
            consumed_by="call-1",
            consumed_at=_CREATED + timedelta(seconds=10),
        )
    assert store.get_approval("appr-1").status == ApprovalStatus.APPROVED


def test_concurrent_consumers_get_exactly_one_grant() -> None:
    store = _approved_store()
    barrier = threading.Barrier(8)
    winners: list[str] = []
    losers: list[str] = []

    def consume(index: int) -> None:
        barrier.wait()
        try:
            store.consume_approval("appr-1", consumed_by=f"call-{index}")
        except ApprovalNotConsumableError:
            losers.append(f"call-{index}")
        else:
            winners.append(f"call-{index}")

    threads = [threading.Thread(target=consume, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert len(losers) == 7
    assert store.get_approval("appr-1").consumed_by == winners[0]


def test_consumption_fields_are_validated_and_round_trip() -> None:
    consumed = _approved_store().consume_approval("appr-1", consumed_by="call-1")

    assert ApprovalRequest.from_dict(consumed.to_dict()) == consumed
    with pytest.raises(ValueError):
        _request(status=ApprovalStatus.CONSUMED)
    with pytest.raises(ValueError):
        _request(consumed_by="call-1", consumed_at=_CREATED)
