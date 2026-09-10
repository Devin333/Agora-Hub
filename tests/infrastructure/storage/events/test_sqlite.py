from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from framework.events.canonical import (
    BusinessContext,
    EventCandidate,
    ProducerIdentity,
)
from framework.events.errors import (
    EventIdentityCollisionError,
    EventStaleLeaseError,
    EventStoreCapacityError,
    EventStoreContentionError,
    EventStoreCorruptionError,
    EventStreamVersionConflictError,
)
from framework.events.ports import EventStorePort, TransactionalStateReaderPort
from framework.events.runtime.models import (
    DeadLetterAction,
    DeadLetterDisposition,
    DeadLetterQuery,
    DeliveryClaimRequest,
    DeliveryLimits,
    DeliveryQuery,
    DeliverySettlement,
    DeliveryState,
    DurableSubscription,
    MAX_REDELIVERY_ITEMS,
    QuarantineDisposition,
    QuarantineReason,
    QuarantineRecord,
    RedeliveryRequest,
    RetirementCancellationRequest,
    ReplayMode,
    ReplayReportQuery,
    ReplayStartRequest,
    ReplayStatus,
    RetryPolicy,
    StreamReadRequest,
    SubscriptionStart,
    SubscriptionStartPolicy,
    SubscriptionKey,
    SubscriptionStatus,
    TransactionalStateSnapshot,
)
from framework.events.schema import SecurityClassification
from infrastructure.storage.events.sqlite import SQLiteEventStore


NOW = datetime(2026, 7, 15, 1, 0, tzinfo=UTC)


def _candidate(
    number: int,
    *,
    stream_id: str = "run:one",
    tenant_id: str | None = "tenant-a",
    event_type: str = "workflow_started",
) -> EventCandidate:
    return EventCandidate(
        event_id=f"evt-{number}",
        event_type=event_type,
        data_schema="newsroom.workflow-event/v1",
        source="tests.sqlite",
        occurred_at=NOW + timedelta(seconds=number),
        stream_id=stream_id,
        business_context=BusinessContext(run_id="one"),
        producer=ProducerIdentity(component="sqlite-tests", version="1"),
        tenant_id=tenant_id,
        security_classification=SecurityClassification.INTERNAL,
        payload={"number": number},
    )


def _store(tmp_path: Path, **kwargs: object) -> SQLiteEventStore:
    return SQLiteEventStore(tmp_path / "events.sqlite3", clock=lambda: NOW, **kwargs)


class _MutableClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


def test_sqlite_store_is_file_backed_wal_and_implements_port(tmp_path: Path) -> None:
    store = _store(tmp_path)

    assert isinstance(store, EventStorePort)
    assert isinstance(store, TransactionalStateReaderPort)
    assert store.durability_policy == {
        "journal_mode": "WAL",
        "synchronous": "FULL",
        "busy_timeout_ms": 5000,
        "host_scope": "single-host",
    }
    with sqlite3.connect(store.database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "durable_events",
        "event_stream_sequences",
        "event_transactional_states",
        "event_subscriptions",
        "event_subscription_stream_states",
        "event_deliveries",
        "event_inbox",
        "event_consumer_checkpoints",
        "event_dead_letters",
        "event_quarantine",
        "event_replay_reports",
        "event_redelivery_reports",
        "event_redelivery_items",
        "event_retirement_cancellation_reports",
        "event_retirement_cancellation_items",
    } <= table_names

    with pytest.raises(ValueError, match="file-backed"):
        SQLiteEventStore(":memory:")


def test_append_is_atomic_idempotent_ordered_and_tenant_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    subscription = DurableSubscription(
        "projection",
        1,
        "projection-consumer",
        tenant_id="tenant-a",
    )
    store.register_subscription(subscription)

    first = store.append_event(_candidate(1))
    duplicate = store.append_event(_candidate(1))
    second = store.append_event(_candidate(2))
    other_tenant = store.append_event(_candidate(3, tenant_id="tenant-b"))

    assert (first.event.stream_sequence, second.event.stream_sequence) == (1, 2)
    assert other_tenant.event.stream_sequence == 1
    assert first.created and first.pending_delivery_count == 1
    assert not duplicate.created and duplicate.pending_delivery_count == 0
    assert store.get_event("evt-1", tenant_id="tenant-b") is None
    assert [
        event.event_id
        for event in store.read_stream(
            StreamReadRequest("run:one", tenant_id="tenant-a")
        ).events
    ] == ["evt-1", "evt-2"]

    with pytest.raises(EventIdentityCollisionError):
        store.append_event(_candidate(1, event_type="workflow_failed"))


def test_conditional_append_resolves_duplicate_before_stream_conflict(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = store.append_event(_candidate(1), expected_last_sequence=0)

    duplicate = store.append_event(_candidate(1), expected_last_sequence=0)

    assert duplicate.event == first.event
    assert duplicate.created is False
    with pytest.raises(EventStreamVersionConflictError) as failure:
        store.append_event(_candidate(2), expected_last_sequence=0)
    assert failure.value.stream_id == "run:one"
    assert failure.value.expected_last_sequence == 0
    assert failure.value.actual_last_sequence == 1
    assert store.get_event("evt-2", tenant_id="tenant-a") is None
    assert store.get_stream_high_watermark("run:one", tenant_id="tenant-a") == 1

    second = store.append_event(_candidate(2), expected_last_sequence=1)
    assert second.event.stream_sequence == 2


def test_concurrent_conditional_appends_allow_one_writer_per_stream_version(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    barrier = Barrier(2)

    def append(number: int):
        barrier.wait()
        try:
            return store.append_event(
                _candidate(number),
                expected_last_sequence=0,
            )
        except EventStreamVersionConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(append, (1, 2)))

    committed = [result for result in results if not isinstance(result, Exception)]
    conflicts = [
        result
        for result in results
        if isinstance(result, EventStreamVersionConflictError)
    ]
    assert len(committed) == 1
    assert committed[0].event.stream_sequence == 1
    assert len(conflicts) == 1
    assert conflicts[0].expected_last_sequence == 0
    assert conflicts[0].actual_last_sequence == 1
    page = store.read_stream(StreamReadRequest("run:one", tenant_id="tenant-a"))
    assert len(page.events) == 1
    assert page.high_watermark == 1


def test_unit_of_work_rolls_back_sequence_event_and_outbox(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_subscription(
        DurableSubscription("projection", 1, "consumer", tenant_id="tenant-a")
    )

    with store.unit_of_work() as transaction:
        transaction.append_event(_candidate(1))

    assert store.get_event("evt-1", tenant_id="tenant-a") is None
    assert store.get_stream_high_watermark("run:one", tenant_id="tenant-a") is None
    assert store.list_deliveries(DeliveryQuery(tenant_id="tenant-a")).records == ()

    committed = store.append_event(_candidate(2))
    assert committed.event.stream_sequence == 1


def test_transactional_state_cas_is_monotonic_and_exact_redelivery_is_idempotent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    initial = TransactionalStateSnapshot.create(
        namespace="harness.capacity",
        key="tenant-a:pool-a",
        revision=1,
        payload={"available": 2},
    )
    updated = TransactionalStateSnapshot.create(
        namespace=initial.namespace,
        key=initial.key,
        revision=2,
        payload={"available": 1},
    )

    with store.unit_of_work() as transaction:
        installed = transaction.cas_transactional_state(
            namespace=initial.namespace,
            key=initial.key,
            expected_revision=None,
            expected_checksum=None,
            next_snapshot=initial,
        )
        transaction.commit()
    assert installed == initial
    assert store.load_transactional_state(initial.namespace, initial.key) == initial

    with store.unit_of_work() as transaction:
        persisted = transaction.cas_transactional_state(
            namespace=updated.namespace,
            key=updated.key,
            expected_revision=initial.revision,
            expected_checksum=initial.checksum,
            next_snapshot=updated,
        )
        transaction.commit()
    assert persisted == updated

    with store.unit_of_work() as transaction:
        redelivered = transaction.cas_transactional_state(
            namespace=updated.namespace,
            key=updated.key,
            expected_revision=initial.revision,
            expected_checksum=initial.checksum,
            next_snapshot=updated,
        )
        transaction.commit()
    assert redelivered == updated

    conflicting = TransactionalStateSnapshot.create(
        namespace=updated.namespace,
        key=updated.key,
        revision=3,
        payload={"available": 0},
    )
    with pytest.raises(EventStoreContentionError, match="revision/checksum conflict"):
        with store.unit_of_work() as transaction:
            transaction.cas_transactional_state(
                namespace=conflicting.namespace,
                key=conflicting.key,
                expected_revision=initial.revision,
                expected_checksum=initial.checksum,
                next_snapshot=conflicting,
            )
    assert store.load_transactional_state(updated.namespace, updated.key) == updated


def test_transactional_state_and_event_roll_back_as_one_unit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    initial = TransactionalStateSnapshot.create(
        namespace="harness.capacity",
        key="tenant-a:pool-a",
        revision=1,
        payload={"available": 2},
    )

    with store.unit_of_work() as transaction:
        transaction.cas_transactional_state(
            namespace=initial.namespace,
            key=initial.key,
            expected_revision=None,
            expected_checksum=None,
            next_snapshot=initial,
        )
        transaction.append_event(_candidate(41), expected_last_sequence=0)

    assert store.load_transactional_state(initial.namespace, initial.key) is None
    assert store.get_event("evt-41", tenant_id="tenant-a") is None
    assert store.get_stream_high_watermark("run:one", tenant_id="tenant-a") is None


def test_stream_pagination_fixes_snapshot_and_filters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_event(_candidate(1))
    store.append_event(_candidate(2, event_type="workflow_failed"))
    store.append_event(_candidate(3))

    first = store.read_stream(
        StreamReadRequest(
            "run:one",
            tenant_id="tenant-a",
            event_types=frozenset({"workflow_started"}),
            limit=1,
        )
    )
    assert [event.event_id for event in first.events] == ["evt-1"]
    assert first.high_watermark == 3
    assert first.next_cursor is not None

    store.append_event(_candidate(4))
    second = store.read_stream(
        StreamReadRequest(
            "run:one",
            tenant_id="tenant-a",
            event_types=frozenset({"workflow_started"}),
            cursor=first.next_cursor,
            limit=10,
        )
    )
    assert [event.event_id for event in second.events] == ["evt-3"]
    assert second.high_watermark == 3


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        (SubscriptionStart(SubscriptionStartPolicy.EARLIEST), ["evt-1", "evt-2"]),
        (SubscriptionStart(SubscriptionStartPolicy.LATEST), []),
        (
            SubscriptionStart(SubscriptionStartPolicy.AT_SEQUENCE, start_sequence=2),
            ["evt-2"],
        ),
    ],
)
def test_subscription_registration_materializes_exact_start_boundary(
    tmp_path: Path,
    start: SubscriptionStart,
    expected: list[str],
) -> None:
    store = _store(tmp_path)
    store.append_event(_candidate(1))
    store.append_event(_candidate(2))

    store.register_subscription(
        DurableSubscription(
            "projection",
            1,
            "consumer",
            start=start,
            tenant_id="tenant-a",
        )
    )

    assert [
        delivery.event_id
        for delivery in store.list_deliveries(
            DeliveryQuery(tenant_id="tenant-a")
        ).records
    ] == expected


def test_registration_backfill_capacity_failure_rolls_back_all_streams(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for number, stream_id in enumerate(
        ("run:one", "run:one", "run:two", "run:two"),
        1,
    ):
        store.append_event(_candidate(number, stream_id=stream_id))
    subscription = DurableSubscription(
        "bounded-backfill",
        1,
        "consumer",
        limits=DeliveryLimits(
            batch_size=1,
            max_in_flight=1,
            max_concurrency=1,
            pending_warning_threshold=2,
            pending_hard_limit=3,
        ),
        tenant_id="tenant-a",
    )

    with pytest.raises(EventStoreCapacityError, match="backfill"):
        store.register_subscription(subscription)

    assert store.get_subscription(subscription.key) is None
    assert store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=subscription.subscription_version,
            tenant_id=subscription.tenant_id,
        )
    ).records == ()
    for stream_id in ("run:one", "run:two"):
        assert store.get_subscription_stream_state(
            subscription.key,
            stream_id,
            tenant_id=subscription.tenant_id,
        ) is None


def test_initial_retired_registration_is_rejected_without_side_effects(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.append_event(_candidate(1))
    subscription = DurableSubscription(
        "initially-retired",
        1,
        "consumer",
        status=SubscriptionStatus.RETIRED,
        tenant_id="tenant-a",
    )

    with pytest.raises(ValueError, match="initially RETIRED"):
        store.register_subscription(subscription)

    assert store.get_subscription(subscription.key) is None
    assert store.get_subscription_stream_state(
        subscription.key,
        "run:one",
        tenant_id="tenant-a",
    ) is None
    assert store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            tenant_id="tenant-a",
        )
    ).records == ()


def test_duplicate_registration_ignores_dynamic_status_and_timestamps(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    definition = DurableSubscription("projection", 1, "consumer", tenant_id="tenant-a")
    registered = store.register_subscription(definition)
    paused = store.set_subscription_status(
        registered.key,
        SubscriptionStatus.PAUSED,
        changed_at=NOW + timedelta(seconds=1),
        reason="maintenance",
    )

    duplicate = store.register_subscription(
        replace(
            definition,
            created_at=NOW + timedelta(days=1),
            updated_at=NOW + timedelta(days=2),
        )
    )

    assert duplicate == paused
    assert duplicate.status is SubscriptionStatus.PAUSED


def test_paused_subscription_materializes_but_does_not_claim_and_retired_drains(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription("projection", 1, "consumer", tenant_id="tenant-a")
    )
    store.set_subscription_status(
        subscription.key,
        SubscriptionStatus.PAUSED,
        changed_at=NOW,
        reason="maintenance",
    )
    store.append_event(_candidate(1))
    request = DeliveryClaimRequest("projection", 1, "worker", NOW)
    assert store.claim_deliveries(request) == ()

    store.set_subscription_status(
        subscription.key,
        SubscriptionStatus.RETIRED,
        changed_at=NOW + timedelta(seconds=1),
        reason="replace consumer",
    )
    claims = store.claim_deliveries(
        replace(request, requested_at=NOW + timedelta(seconds=2))
    )
    assert len(claims) == 1
    store.append_event(_candidate(2))
    deliveries = store.list_deliveries(DeliveryQuery(tenant_id="tenant-a")).records
    assert [delivery.event_id for delivery in deliveries] == ["evt-1"]

    with pytest.raises(ValueError, match="cannot be reactivated"):
        store.set_subscription_status(
            subscription.key,
            SubscriptionStatus.ACTIVE,
            changed_at=NOW + timedelta(seconds=3),
            reason="unsafe",
        )


def test_claim_fencing_retry_exhaustion_checkpoint_and_late_repair(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_subscription(
        DurableSubscription(
            "projection",
            1,
            "consumer",
            retry_policy=RetryPolicy(max_attempts=1),
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    store.append_event(_candidate(2))
    first_claim = store.claim_deliveries(
        DeliveryClaimRequest("projection", 1, "worker-a", NOW)
    )
    assert len(first_claim) == 1
    assert first_claim[0].event.event_id == "evt-1"

    with pytest.raises(EventStaleLeaseError):
        store.renew_delivery_lease(
            replace(first_claim[0].lease, lease_owner="stale-worker"),
            renewed_at=NOW + timedelta(seconds=1),
            lease_duration_seconds=30,
        )

    settlement = store.settle_delivery(
        DeliverySettlement(
            first_claim[0].lease,
            DeliveryState.RETRY_WAIT,
            NOW + timedelta(seconds=1),
            reason_class="poison",
            retry_available_at=NOW + timedelta(seconds=2),
        )
    )
    assert settlement.delivery.state is DeliveryState.DEAD_LETTER
    assert settlement.dead_letter_id is not None
    assert settlement.checkpoint is not None
    assert settlement.checkpoint.highest_contiguous_terminal_sequence == 1

    next_claim = store.claim_deliveries(
        DeliveryClaimRequest(
            "projection",
            1,
            "worker-b",
            NOW + timedelta(seconds=2),
        )
    )
    assert [claim.event.event_id for claim in next_claim] == ["evt-2"]
    repaired = store.requeue_dead_letter(
        DeadLetterAction(
            settlement.dead_letter_id,
            "operator",
            "repair poison input",
            NOW + timedelta(seconds=3),
            idempotency_ready=True,
        )
    )
    assert repaired.delivery_generation == 2
    assert repaired.state is DeliveryState.PENDING
    assert store.list_dead_letters(
        DeadLetterQuery(tenant_id="tenant-a")
    ).records[0].disposition is DeadLetterDisposition.REQUEUED


def test_requeue_capacity_failure_keeps_dead_letter_open(tmp_path: Path) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription(
            "bounded-repair",
            1,
            "consumer",
            retry_policy=RetryPolicy(max_attempts=1),
            limits=DeliveryLimits(
                batch_size=1,
                max_in_flight=1,
                max_concurrency=1,
                pending_warning_threshold=1,
                pending_hard_limit=2,
            ),
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1, stream_id="run:dead"))
    claim = store.claim_deliveries(
        DeliveryClaimRequest("bounded-repair", 1, "worker", NOW, limit=1)
    )[0]
    dead = store.settle_delivery(
        DeliverySettlement(
            claim.lease,
            DeliveryState.DEAD_LETTER,
            NOW + timedelta(seconds=1),
            reason_class="poison",
        )
    )
    assert dead.dead_letter_id is not None
    store.append_event(_candidate(2, stream_id="run:two"))
    store.append_event(_candidate(3, stream_id="run:three"))

    with pytest.raises(EventStoreCapacityError, match="hard limit"):
        store.requeue_dead_letter(
            DeadLetterAction(
                dead.dead_letter_id,
                "operator",
                "repair poison input",
                NOW + timedelta(seconds=2),
                idempotency_ready=True,
            )
        )

    dead_letter = store.list_dead_letters(
        DeadLetterQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=subscription.subscription_version,
            tenant_id=subscription.tenant_id,
        )
    ).records[0]
    assert dead_letter.disposition is DeadLetterDisposition.OPEN
    deliveries = store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=subscription.subscription_version,
            tenant_id=subscription.tenant_id,
        )
    ).records
    assert len(deliveries) == 3
    assert max(delivery.delivery_generation for delivery in deliveries) == 1
    assert store.pending_delivery_stats(subscription.key).pending_count == 2


def test_max_concurrency_is_a_durable_lease_owner_slot(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    store = SQLiteEventStore(tmp_path / "events.sqlite3", clock=clock)
    store.register_subscription(
        DurableSubscription(
            "concurrency",
            1,
            "consumer",
            limits=DeliveryLimits(
                batch_size=1,
                max_in_flight=3,
                max_concurrency=1,
                pending_warning_threshold=3,
                pending_hard_limit=6,
            ),
            tenant_id="tenant-a",
        )
    )
    for number in range(1, 4):
        store.append_event(_candidate(number, stream_id=f"run:{number}"))

    first = store.claim_deliveries(
        DeliveryClaimRequest(
            "concurrency",
            1,
            "worker-a",
            NOW - timedelta(days=1),
            lease_duration_seconds=5,
            limit=1,
        )
    )
    assert len(first) == 1
    assert first[0].lease.lease_expires_at == NOW + timedelta(seconds=5)
    assert store.claim_deliveries(
        DeliveryClaimRequest("concurrency", 1, "worker-b", NOW, limit=1)
    ) == ()
    assert len(
        store.claim_deliveries(
            DeliveryClaimRequest(
                "concurrency",
                1,
                "worker-a",
                NOW,
                lease_duration_seconds=5,
                limit=1,
            )
        )
    ) == 1

    clock.advance(seconds=6)
    replacement = store.claim_deliveries(
        DeliveryClaimRequest(
            "concurrency",
            1,
            "worker-b",
            NOW - timedelta(days=1),
            lease_duration_seconds=5,
            limit=1,
        )
    )
    assert len(replacement) == 1
    assert replacement[0].lease.lease_owner == "worker-b"
    assert replacement[0].lease.lease_expires_at == clock.current + timedelta(seconds=5)


def test_store_clock_fences_backdated_renew_and_settlement(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    store = SQLiteEventStore(tmp_path / "events.sqlite3", clock=clock)
    store.register_subscription(
        DurableSubscription("lease-clock", 1, "consumer", tenant_id="tenant-a")
    )
    store.append_event(_candidate(1))
    first = store.claim_deliveries(
        DeliveryClaimRequest(
            "lease-clock",
            1,
            "worker-a",
            NOW - timedelta(days=1),
            lease_duration_seconds=5,
            limit=1,
        )
    )[0]
    assert first.lease.lease_expires_at == NOW + timedelta(seconds=5)

    clock.advance(seconds=4.9999)
    assert store.claim_deliveries(
        DeliveryClaimRequest(
            "lease-clock",
            1,
            "worker-too-early",
            NOW + timedelta(days=1),
            lease_duration_seconds=5,
            limit=1,
        )
    ) == ()
    clock.advance(seconds=0.0002)
    with pytest.raises(EventStaleLeaseError):
        store.renew_delivery_lease(
            first.lease,
            renewed_at=NOW - timedelta(days=1),
            lease_duration_seconds=5,
        )
    with pytest.raises(EventStaleLeaseError):
        store.settle_delivery(
            DeliverySettlement(
                first.lease,
                DeliveryState.ACKED,
                NOW - timedelta(days=1),
            )
        )

    recovered = store.claim_deliveries(
        DeliveryClaimRequest(
            "lease-clock",
            1,
            "worker-b",
            NOW - timedelta(days=1),
            lease_duration_seconds=5,
            limit=1,
        )
    )[0]
    renewed = store.renew_delivery_lease(
        recovered.lease,
        renewed_at=NOW - timedelta(days=1),
        lease_duration_seconds=10,
    )
    assert renewed.lease_expires_at == clock.current + timedelta(seconds=10)


def test_pending_delivery_stats_separate_lag_and_late_repair(tmp_path: Path) -> None:
    clock = _MutableClock(NOW)
    store = SQLiteEventStore(tmp_path / "events.sqlite3", clock=clock)
    subscription = store.register_subscription(
        DurableSubscription(
            "repair-stats",
            1,
            "consumer",
            retry_policy=RetryPolicy(max_attempts=1),
            limits=DeliveryLimits(
                batch_size=1,
                max_in_flight=1,
                max_concurrency=1,
                pending_warning_threshold=1,
                pending_hard_limit=3,
            ),
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    claim = store.claim_deliveries(
        DeliveryClaimRequest("repair-stats", 1, "worker", NOW, limit=1)
    )[0]
    dead = store.settle_delivery(
        DeliverySettlement(
            claim.lease,
            DeliveryState.DEAD_LETTER,
            NOW,
            reason_class="poison",
        )
    )
    assert dead.dead_letter_id is not None
    store.append_event(_candidate(2, stream_id="run:two"))
    clock.advance(seconds=7)
    store.requeue_dead_letter(
        DeadLetterAction(
            dead.dead_letter_id,
            "operator",
            "repair poison input",
            clock.current,
            idempotency_ready=True,
        )
    )

    stats = store.pending_delivery_stats(subscription.key)

    assert stats.pending_count == 2
    assert stats.lag == 1
    assert stats.late_repair_pending_count == 1
    assert stats.warning_threshold_reached
    assert stats.capacity_remaining == 1
    assert stats.oldest_pending_at == NOW
    assert stats.oldest_pending_age_seconds == 7


def test_pending_stats_order_mixed_rfc3339_precision_chronologically(
    tmp_path: Path,
) -> None:
    clock = _MutableClock(NOW)
    store = SQLiteEventStore(tmp_path / "events.sqlite3", clock=clock)
    subscription = store.register_subscription(
        DurableSubscription("mixed-time", 1, "consumer", tenant_id="tenant-a")
    )
    store.append_event(_candidate(1, stream_id="run:first"))
    clock.advance(seconds=0.1)
    store.append_event(_candidate(2, stream_id="run:second"))

    stats = store.pending_delivery_stats(subscription.key)

    assert stats.oldest_pending_at == NOW
    assert stats.oldest_pending_age_seconds == 0.1


def test_authorized_redelivery_is_finite_idempotent_and_preserves_history(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription(
            "redelivery",
            1,
            "redelivery-consumer",
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    for number in (1, 2, 3):
        store.append_event(_candidate(number))
        claim = store.claim_deliveries(
            DeliveryClaimRequest(
                subscription.subscription_id,
                subscription.subscription_version,
                f"worker-{number}",
                NOW + timedelta(seconds=number),
                limit=1,
            )
        )[0]
        store.settle_delivery(
            DeliverySettlement(
                claim.lease,
                DeliveryState.ACKED,
                NOW + timedelta(seconds=number, milliseconds=1),
            )
        )

    original_deliveries = store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=1,
            tenant_id="tenant-a",
        )
    ).records
    request = RedeliveryRequest(
        redelivery_id="redelivery-request-1",
        subscription=subscription.key,
        source_stream_id="run:one",
        from_sequence=1,
        through_sequence=2,
        requested_at=NOW + timedelta(seconds=10),
        operator_id="operator-1",
        operator_reason="rebuild projection after deterministic repair",
        authorization_evidence_ref="authz-decision:tenant-a:redelivery-1",
        tenant_id="tenant-a",
    )

    first = store.begin_redelivery(request)
    store.append_event(_candidate(4))
    duplicate = store.begin_redelivery(request)

    assert duplicate == first
    assert first.captured_high_watermark == 3
    assert first.through_sequence == 2
    assert [item.stream_sequence for item in first.items] == [1, 2]
    assert [item.delivery_generation for item in first.items] == [2, 2]
    assert store.get_redelivery_report(
        request.redelivery_id,
        tenant_id="tenant-a",
    ) == first
    assert store.get_redelivery_report(request.redelivery_id) is None
    all_deliveries = store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=1,
            tenant_id="tenant-a",
            limit=20,
        )
    ).records
    assert len(all_deliveries) == len(original_deliveries) + 3
    assert sorted(
        (
            item.event_id,
            item.delivery_generation,
            item.state,
        )
        for item in all_deliveries
        if item.event_id in {"evt-1", "evt-2"}
    ) == [
        ("evt-1", 1, DeliveryState.ACKED),
        ("evt-1", 2, DeliveryState.PENDING),
        ("evt-2", 1, DeliveryState.ACKED),
        ("evt-2", 2, DeliveryState.PENDING),
    ]
    assert store.list_dead_letters(DeadLetterQuery(tenant_id="tenant-a")).records == ()


def test_open_ended_redelivery_rejects_an_oversized_captured_range_atomically(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription(
            "bounded-redelivery",
            1,
            "bounded-consumer",
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    deliveries_before = store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=subscription.subscription_version,
            tenant_id="tenant-a",
        )
    ).records
    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "UPDATE event_stream_sequences SET last_sequence = ? "
            "WHERE tenant_scope = ? AND stream_id = ?",
            (MAX_REDELIVERY_ITEMS + 1, "tenant-a", "run:one"),
        )
        schema_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' "
            "AND name = 'event_redelivery_reports'"
        ).fetchone()[0]
    assert "through_sequence - from_sequence < 1000" in schema_sql
    request = RedeliveryRequest(
        redelivery_id="oversized-open-range",
        subscription=subscription.key,
        source_stream_id="run:one",
        from_sequence=1,
        requested_at=NOW,
        operator_id="operator",
        operator_reason="bounded repair",
        authorization_evidence_ref="authz:oversized-open-range",
        tenant_id="tenant-a",
    )

    with pytest.raises(EventStoreCapacityError, match="stream-position"):
        store.begin_redelivery(request)

    assert store.get_redelivery_report(
        request.redelivery_id,
        tenant_id="tenant-a",
    ) is None
    assert store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=subscription.subscription_version,
            tenant_id="tenant-a",
        )
    ).records == deliveries_before


def test_concurrent_authorized_redelivery_materializes_one_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "concurrent-redelivery.sqlite3"
    store = SQLiteEventStore(database, clock=lambda: NOW)
    subscription = store.register_subscription(
        DurableSubscription(
            "concurrent-redelivery",
            1,
            "consumer",
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    claim = store.claim_deliveries(
        DeliveryClaimRequest(subscription.subscription_id, 1, "worker", NOW)
    )[0]
    store.settle_delivery(
        DeliverySettlement(
            claim.lease,
            DeliveryState.ACKED,
            NOW + timedelta(seconds=1),
        )
    )
    request = RedeliveryRequest(
        redelivery_id="concurrent-redelivery-request",
        subscription=SubscriptionKey(subscription.subscription_id, 1),
        source_stream_id="run:one",
        from_sequence=1,
        through_sequence=1,
        requested_at=NOW + timedelta(seconds=2),
        operator_id="operator",
        operator_reason="authorized exact retry",
        authorization_evidence_ref="authz:concurrent-redelivery",
        tenant_id="tenant-a",
    )
    barrier = Barrier(2)

    def begin() -> object:
        concurrent = SQLiteEventStore(database, initialize=False, clock=lambda: NOW)
        barrier.wait()
        return concurrent.begin_redelivery(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        reports = list(executor.map(lambda _index: begin(), range(2)))

    assert reports[0] == reports[1]
    deliveries = store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=1,
            tenant_id="tenant-a",
            limit=10,
        )
    ).records
    assert [(item.delivery_generation, item.state) for item in deliveries] == [
        (1, DeliveryState.ACKED),
        (2, DeliveryState.PENDING),
    ]


def test_authorized_redelivery_rejects_nonterminal_or_changed_request(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription(
            "guarded-redelivery",
            1,
            "consumer",
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    request = RedeliveryRequest(
        redelivery_id="guarded-redelivery-request",
        subscription=subscription.key,
        source_stream_id="run:one",
        from_sequence=1,
        through_sequence=1,
        requested_at=NOW,
        operator_id="operator",
        operator_reason="repair",
        authorization_evidence_ref="authz:guarded-redelivery",
        tenant_id="tenant-a",
    )
    with pytest.raises(Exception, match="nonterminal"):
        store.begin_redelivery(request)

    claim = store.claim_deliveries(
        DeliveryClaimRequest(subscription.subscription_id, 1, "worker", NOW)
    )[0]
    store.settle_delivery(
        DeliverySettlement(
            claim.lease,
            DeliveryState.ACKED,
            NOW + timedelta(seconds=1),
        )
    )
    store.begin_redelivery(request)
    with pytest.raises(Exception, match="different request"):
        store.begin_redelivery(replace(request, operator_reason="changed"))


def test_authorized_redelivery_identity_is_tenant_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reports = {}
    for index, tenant_id in enumerate(("tenant-a", "tenant-b"), start=1):
        subscription = store.register_subscription(
            DurableSubscription(
                f"tenant-redelivery-{index}",
                1,
                f"tenant-consumer-{index}",
                supports_out_of_order_repair=True,
                tenant_id=tenant_id,
            )
        )
        store.append_event(_candidate(10 + index, tenant_id=tenant_id))
        claim = store.claim_deliveries(
            DeliveryClaimRequest(
                subscription.subscription_id,
                1,
                f"tenant-worker-{index}",
                NOW,
            )
        )[0]
        store.settle_delivery(
            DeliverySettlement(
                claim.lease,
                DeliveryState.ACKED,
                NOW + timedelta(seconds=1),
            )
        )
        request = RedeliveryRequest(
            redelivery_id="shared-redelivery-id",
            subscription=subscription.key,
            source_stream_id="run:one",
            from_sequence=1,
            requested_at=NOW + timedelta(seconds=2),
            operator_id=f"operator-{index}",
            operator_reason="tenant-scoped repair",
            authorization_evidence_ref=f"authz:{tenant_id}:redelivery",
            tenant_id=tenant_id,
        )
        reports[tenant_id] = store.begin_redelivery(request)

    assert reports["tenant-a"].redelivery_id == reports["tenant-b"].redelivery_id
    assert reports["tenant-a"].tenant_id == "tenant-a"
    assert reports["tenant-b"].tenant_id == "tenant-b"
    assert store.get_redelivery_report(
        "shared-redelivery-id",
        tenant_id="tenant-a",
    ) == reports["tenant-a"]
    assert store.get_redelivery_report(
        "shared-redelivery-id",
        tenant_id="tenant-b",
    ) == reports["tenant-b"]


def test_poison_event_redelivery_preserves_open_dead_letter(tmp_path: Path) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription(
            "poison-redelivery",
            1,
            "poison-consumer",
            retry_policy=RetryPolicy(max_attempts=1),
            supports_out_of_order_repair=True,
            tenant_id="tenant-a",
        )
    )
    original_event = store.append_event(_candidate(1)).event
    claim = store.claim_deliveries(
        DeliveryClaimRequest(subscription.subscription_id, 1, "poison-worker", NOW)
    )[0]
    settlement = store.settle_delivery(
        DeliverySettlement(
            claim.lease,
            DeliveryState.RETRY_WAIT,
            NOW + timedelta(seconds=1),
            reason_class="poison_event",
            retry_available_at=NOW + timedelta(seconds=2),
        )
    )
    assert settlement.dead_letter_id is not None
    dead_letter_before = store.get_dead_letter(
        settlement.dead_letter_id,
        tenant_id="tenant-a",
    )
    assert dead_letter_before is not None
    assert dead_letter_before.disposition is DeadLetterDisposition.OPEN

    report = store.begin_redelivery(
        RedeliveryRequest(
            redelivery_id="poison-redelivery-request",
            subscription=subscription.key,
            source_stream_id=original_event.stream_id,
            from_sequence=original_event.stream_sequence,
            through_sequence=original_event.stream_sequence,
            requested_at=NOW + timedelta(seconds=3),
            operator_id="operator",
            operator_reason="retry after poison input repair",
            authorization_evidence_ref="authz:poison-redelivery",
            tenant_id="tenant-a",
        )
    )

    assert report.items[0].delivery_generation == 2
    assert store.get_dead_letter(
        settlement.dead_letter_id,
        tenant_id="tenant-a",
    ) == dead_letter_before
    deliveries = store.list_deliveries(
        DeliveryQuery(
            subscription_id=subscription.subscription_id,
            subscription_version=1,
            tenant_id="tenant-a",
            limit=10,
        )
    ).records
    assert sorted((item.delivery_generation, item.state) for item in deliveries) == [
        (1, DeliveryState.DEAD_LETTER),
        (2, DeliveryState.PENDING),
    ]
    assert store.get_event(original_event.event_id, tenant_id="tenant-a") == original_event


def test_quarantine_replay_backup_and_restore_are_durable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_event(_candidate(1))
    quarantine = QuarantineRecord(
        "quarantine-1",
        "legacy-jsonl",
        QuarantineReason.CORRUPT_RECORD,
        NOW,
        tenant_id="tenant-a",
        redacted_diagnostic="checksum mismatch",
    )
    store.save_quarantine(quarantine)
    resolved = store.resolve_quarantine(
        quarantine.quarantine_id,
        QuarantineDisposition.REJECTED,
        operator_id="operator",
        reason="cannot recover",
        resolved_at=NOW + timedelta(seconds=1),
    )
    assert resolved.disposition is QuarantineDisposition.REJECTED

    replay = store.begin_replay(
        ReplayStartRequest(
            "replay-1",
            ReplayMode.REBUILD_STATE,
            "run:one",
            NOW,
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(2))
    running = store.update_replay_report(
        replace(replay, status=ReplayStatus.RUNNING)
    )
    assert running.high_watermark == 1
    assert store.list_replay_reports(
        ReplayReportQuery(tenant_id="tenant-a")
    ).reports == (running,)

    backup = store.backup_to(tmp_path / "backup.sqlite3")
    restored = SQLiteEventStore.restore_backup(
        backup,
        tmp_path / "restored.sqlite3",
    )
    restored.verify_integrity(full=True)
    assert restored.get_event("evt-2", tenant_id="tenant-a") is not None
    assert (
        restored.get_replay_report("replay-1", tenant_id="tenant-a").high_watermark
        == 1
    )


def test_replay_watermark_is_atomic_with_concurrent_live_append(tmp_path: Path) -> None:
    database = tmp_path / "replay-watermark.sqlite3"
    store = SQLiteEventStore(database, clock=lambda: NOW)
    store.append_event(_candidate(1))
    barrier = Barrier(2)

    def capture() -> object:
        concurrent = SQLiteEventStore(database, initialize=False, clock=lambda: NOW)
        barrier.wait()
        return concurrent.begin_replay(
            ReplayStartRequest(
                "atomic-watermark-replay",
                ReplayMode.VERIFY_HISTORY,
                "run:one",
                NOW,
                tenant_id="tenant-a",
            )
        )

    def append() -> object:
        concurrent = SQLiteEventStore(database, initialize=False, clock=lambda: NOW)
        barrier.wait()
        return concurrent.append_event(_candidate(2))

    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_future = executor.submit(capture)
        append_future = executor.submit(append)
        report = capture_future.result()
        appended = append_future.result()

    assert report.high_watermark in {1, 2}
    assert appended.event.stream_sequence == 2
    page = store.read_stream(
        StreamReadRequest(
            stream_id="run:one",
            through_sequence=report.high_watermark,
            tenant_id="tenant-a",
        )
    )
    assert [event.stream_sequence for event in page.events] == list(
        range(1, report.high_watermark + 1)
    )
    assert store.get_stream_high_watermark("run:one", tenant_id="tenant-a") == 2


def test_replay_identity_retry_includes_checkpoint_and_range(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.append_event(_candidate(1))
    request = ReplayStartRequest(
        "strict-replay-identity",
        ReplayMode.VERIFY_HISTORY,
        "run:one",
        NOW,
        from_sequence=1,
        checkpoint_ref="checkpoint-one",
        tenant_id="tenant-a",
    )
    assert store.begin_replay(request) == store.begin_replay(request)
    with pytest.raises(EventIdentityCollisionError):
        store.begin_replay(replace(request, checkpoint_ref="checkpoint-two"))


def test_pending_hard_limit_fails_append_before_event_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.register_subscription(
        DurableSubscription(
            "projection",
            1,
            "consumer",
            limits=DeliveryLimits(
                batch_size=1,
                max_in_flight=1,
                max_concurrency=1,
                pending_warning_threshold=1,
                pending_hard_limit=2,
            ),
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    store.append_event(_candidate(2))

    with pytest.raises(EventStoreCapacityError):
        store.append_event(_candidate(3))

    assert store.get_event("evt-3", tenant_id="tenant-a") is None
    assert store.get_stream_high_watermark("run:one", tenant_id="tenant-a") == 2


def test_retirement_cancellation_report_rejects_delivery_audit_drift(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    subscription = store.register_subscription(
        DurableSubscription(
            "retirement-audit",
            1,
            "retirement-audit-consumer",
            tenant_id="tenant-a",
        )
    )
    store.append_event(_candidate(1))
    retired_at = (subscription.updated_at or NOW) + timedelta(seconds=1)
    store.set_subscription_status(
        subscription.key,
        SubscriptionStatus.RETIRED,
        changed_at=retired_at,
        reason="retire audit fixture",
    )
    request = RetirementCancellationRequest(
        cancellation_id="retirement-audit-cancel",
        subscription=subscription.key,
        requested_at=retired_at,
        operator_id="operator-1",
        operator_reason="cancel retired audit fixture",
        authorization_evidence_ref="authz:retirement-audit-cancel",
        tenant_id="tenant-a",
    )
    report = store.cancel_retired_subscription(request)
    assert report.completed

    with sqlite3.connect(store.database) as connection:
        connection.execute(
            "UPDATE event_deliveries SET reason_class = 'manual_drop' "
            "WHERE delivery_id = ?",
            (report.items[0].delivery_id,),
        )
        connection.commit()

    with pytest.raises(
        EventStoreCorruptionError,
        match="disagrees with its delivery disposition",
    ):
        store.get_retirement_cancellation_report(
            request.cancellation_id,
            tenant_id="tenant-a",
        )


def test_concurrent_retirement_cancellation_exact_retry_writes_one_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "retirement-cancel-concurrent.sqlite3"
    store = SQLiteEventStore(database, clock=lambda: NOW)
    subscription = store.register_subscription(
        DurableSubscription(
            "retirement-cancel-concurrent",
            1,
            "retirement-cancel-concurrent-consumer",
            tenant_id="tenant-a",
        )
    )
    for index in range(1, 9):
        store.append_event(_candidate(index))
    retired_at = (subscription.updated_at or NOW) + timedelta(seconds=1)
    store.set_subscription_status(
        subscription.key,
        SubscriptionStatus.RETIRED,
        changed_at=retired_at,
        reason="retire before exact retry race",
    )
    request = RetirementCancellationRequest(
        cancellation_id="retirement-cancel-concurrent-command",
        subscription=subscription.key,
        requested_at=retired_at,
        operator_id="operator-1",
        operator_reason="cancel retired backlog exactly once",
        authorization_evidence_ref="authz:retirement-cancel-concurrent",
        tenant_id="tenant-a",
        limit=8,
    )
    barrier = Barrier(8)

    def cancel(_index: int):
        concurrent = SQLiteEventStore(
            database,
            initialize=False,
            clock=lambda: NOW,
        )
        barrier.wait(timeout=15)
        return concurrent.cancel_retired_subscription(request)

    with ThreadPoolExecutor(max_workers=8) as executor:
        reports = tuple(executor.map(cancel, range(8)))

    assert all(report == reports[0] for report in reports)
    assert reports[0].cancelled_count == 8
    assert reports[0].completed
    with sqlite3.connect(database) as connection:
        report_count = connection.execute(
            "SELECT COUNT(*) FROM event_retirement_cancellation_reports "
            "WHERE cancellation_id = ?",
            (request.cancellation_id,),
        ).fetchone()[0]
        item_count = connection.execute(
            "SELECT COUNT(*) FROM event_retirement_cancellation_items "
            "WHERE cancellation_id = ?",
            (request.cancellation_id,),
        ).fetchone()[0]
    assert (report_count, item_count) == (1, 8)
