from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from framework.workers.approval import (
    ApprovalDecision,
    ApprovalExpiredError,
    ApprovalNotConsumableError,
    ApprovalNotFoundError,
    ApprovalRequest,
    ApprovalStatus,
)
from infrastructure.storage.local_json import LocalJsonApprovalStore
from infrastructure.storage.local_json.file_lock import (
    LocalJsonFileLock,
    LocalJsonLockTimeoutError,
)

_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def _approved_store(path, **overrides: object) -> LocalJsonApprovalStore:
    store = LocalJsonApprovalStore(path, now_fn=lambda: _CREATED + timedelta(seconds=1))
    store.upsert_approval(
        ApprovalRequest(
            approval_id="appr-1",
            requested_action="tool:sample.publish",
            risk_level="high",
            created_at=_CREATED,
            **overrides,  # type: ignore[arg-type]
        )
    )
    store.record_decision(
        "appr-1",
        decision=ApprovalDecision(decision_type="approve", decided_by="reviewer"),
    )
    return store


def test_consume_persists_single_use_redemption(tmp_path) -> None:
    path = tmp_path / "approvals.json"
    store = _approved_store(path)

    store.consume_approval("appr-1", consumed_by="call-1")

    restored = LocalJsonApprovalStore(path).get_approval("appr-1")
    assert restored.status == ApprovalStatus.CONSUMED
    assert restored.consumed_by == "call-1"
    assert restored.consumed_at == _CREATED + timedelta(seconds=1)
    with pytest.raises(ApprovalNotConsumableError):
        LocalJsonApprovalStore(path).consume_approval("appr-1", consumed_by="call-2")
    assert not (tmp_path / "approvals.json.lock").exists()


def test_consume_refuses_expired_and_missing_approvals(tmp_path) -> None:
    store = _approved_store(
        tmp_path / "approvals.json",
        expires_at=_CREATED + timedelta(seconds=5),
    )

    with pytest.raises(ApprovalExpiredError):
        store.consume_approval(
            "appr-1",
            consumed_by="call-1",
            consumed_at=_CREATED + timedelta(seconds=6),
        )
    with pytest.raises(ApprovalNotFoundError):
        store.consume_approval("missing", consumed_by="call-1")
    assert store.get_approval("appr-1").status == ApprovalStatus.APPROVED


def test_independent_store_instances_redeem_exactly_once(tmp_path) -> None:
    path = tmp_path / "approvals.json"
    _approved_store(path)
    # Separate instances share only the file, as separate processes would.
    stores = [LocalJsonApprovalStore(path) for _ in range(6)]
    barrier = threading.Barrier(len(stores))
    winners: list[str] = []
    losers: list[str] = []

    def consume(index: int) -> None:
        barrier.wait()
        try:
            stores[index].consume_approval("appr-1", consumed_by=f"call-{index}")
        except ApprovalNotConsumableError:
            losers.append(f"call-{index}")
        else:
            winners.append(f"call-{index}")

    threads = [threading.Thread(target=consume, args=(index,)) for index in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert len(losers) == len(stores) - 1
    assert LocalJsonApprovalStore(path).get_approval("appr-1").consumed_by == winners[0]


def test_file_lock_is_reentrant_and_removed_on_release(tmp_path) -> None:
    lock = LocalJsonFileLock(tmp_path / "store.json")

    with lock.hold():
        assert lock.lock_path.exists()
        with lock.hold():
            assert lock.lock_path.exists()
        assert lock.lock_path.exists()

    assert not lock.lock_path.exists()


def test_file_lock_times_out_while_another_holder_is_active(tmp_path) -> None:
    holder = LocalJsonFileLock(tmp_path / "store.json")
    contender = LocalJsonFileLock(tmp_path / "store.json", timeout_seconds=0.05)

    with holder.hold():
        with pytest.raises(LocalJsonLockTimeoutError):
            with contender.hold():
                pass

    with contender.hold():
        assert contender.lock_path.exists()


def test_file_lock_breaks_stale_lock_left_by_crashed_holder(tmp_path) -> None:
    lock = LocalJsonFileLock(
        tmp_path / "store.json",
        timeout_seconds=0.05,
        stale_after_seconds=1,
    )
    lock.lock_path.write_text("99999", encoding="ascii")
    stale = time.time() - 5
    os.utime(lock.lock_path, (stale, stale))

    with lock.hold():
        assert lock.lock_path.read_text(encoding="ascii") == str(os.getpid())


def test_file_lock_rejects_invalid_configuration(tmp_path) -> None:
    with pytest.raises(ValueError):
        LocalJsonFileLock(tmp_path / "store.json", timeout_seconds=0)
    with pytest.raises(ValueError):
        LocalJsonFileLock(tmp_path / "store.json", stale_after_seconds=0)
