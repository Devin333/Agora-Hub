from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from framework.events import (
    TransactionalStateReaderPort,
    TransactionalStateRuntimePort,
    ports,
)
from framework.events.runtime import EventRuntime, models
from framework.events.runtime.models import (
    CheckpointKey,
    ConsumerCheckpoint,
    ConsumerEffectContract,
    DeliveryLeaseToken,
    DeliveryRecord,
    DeliverySettlement,
    DeliveryKey,
    DeliveryLimits,
    DeliveryState,
    DurableSubscription,
    EffectIdempotencyStrategy,
    LeasePolicy,
    LegacyEventOffset,
    PendingDeliveryStats,
    ReplayMode,
    ReplayStartRequest,
    RetirementCancellationItem,
    RetirementCancellationReport,
    RetirementCancellationRequest,
    RetryPolicy,
    StreamReadRequest,
    StreamSequenceCursor,
    SubscriptionKey,
    SubscriptionFilter,
    SubscriptionStart,
    SubscriptionStartPolicy,
    SubscriptionStreamState,
)


ROOT = Path(__file__).resolve().parents[3]
CHECKSUM_ZERO = "sha256:" + "0" * 64
CHECKSUM_ONE = "sha256:" + "1" * 64


class _ReadOnlyEventReader:
    """Minimal ordinary reader with no transactional-state capability."""

    def get_event(self, event_id, *, tenant_id=None):  # type: ignore[no-untyped-def]
        return None

    def read_stream(self, request):  # type: ignore[no-untyped-def]
        return models.EventPage(
            stream_id=request.stream_id,
            events=(),
            high_watermark=None,
            tenant_id=request.tenant_id,
        )

    def get_stream_high_watermark(  # type: ignore[no-untyped-def]
        self,
        stream_id,
        *,
        tenant_id=None,
    ):
        return None


class _PublishOnlyRuntime:
    """Ordinary canonical publisher with no shared-state mutation authority."""

    def publish(self, event, **kwargs):  # type: ignore[no-untyped-def]
        return event

    def publish_batch(self, events, **kwargs):  # type: ignore[no-untyped-def]
        return tuple(events)


class _TransactionalRuntime(_PublishOnlyRuntime):
    def publish_batch_with_state_cas(self, events, **kwargs):  # type: ignore[no-untyped-def]
        return tuple(events), kwargs["next_state"]

    def compare_and_swap_transactional_state(  # type: ignore[no-untyped-def]
        self,
        next_snapshot,
        **kwargs,
    ):
        return next_snapshot


def test_plain_event_reader_does_not_require_transactional_state_access() -> None:
    reader = _ReadOnlyEventReader()

    assert isinstance(reader, ports.EventReaderPort)
    assert not isinstance(reader, ports.TransactionalStateReaderPort)
    assert TransactionalStateReaderPort is ports.TransactionalStateReaderPort


def test_plain_event_runtime_does_not_grant_transactional_state_mutation() -> None:
    runtime = _PublishOnlyRuntime()

    assert isinstance(runtime, ports.EventRuntimePort)
    assert not isinstance(runtime, ports.TransactionalStateRuntimePort)
    assert TransactionalStateRuntimePort is ports.TransactionalStateRuntimePort


def test_transactional_state_runtime_composes_the_publish_capability() -> None:
    runtime = _TransactionalRuntime()

    assert isinstance(runtime, ports.EventRuntimePort)
    assert isinstance(runtime, ports.TransactionalStateRuntimePort)


def test_canonical_event_runtime_implements_transactional_state_capability() -> None:
    # Protocol conformance is structural and does not require opening a real
    # adapter merely to inspect the concrete runtime surface.  SQLite/Postgres
    # transaction semantics are covered by their state-batch integration tests.
    runtime = object.__new__(EventRuntime)

    assert isinstance(runtime, ports.EventRuntimePort)
    assert isinstance(runtime, ports.TransactionalStateRuntimePort)


def test_framework_event_runtime_has_no_infrastructure_import_boundary() -> None:
    targets = [
        ROOT / "framework" / "events" / "ports.py",
        ROOT / "framework" / "events" / "runtime" / "models.py",
        ROOT / "framework" / "events" / "runtime" / "__init__.py",
    ]

    for target in targets:
        tree = ast.parse(target.read_text(encoding="utf-8"), filename=str(target))
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not {
            module_name
            for module_name in imported_modules
            if module_name == "infrastructure" or module_name.startswith("infrastructure.")
        }, target


def test_store_port_exposes_one_backend_neutral_conformance_surface() -> None:
    required_methods = {
        "unit_of_work",
        "get_event",
        "read_stream",
        "get_stream_high_watermark",
        "load_transactional_state",
        "register_subscription",
        "list_subscriptions",
        "get_subscription_stream_state",
        "list_subscription_stream_states",
        "claim_deliveries",
        "list_deliveries",
        "settle_delivery",
        "get_inbox_entry",
        "get_checkpoint",
        "list_checkpoints",
        "list_dead_letters",
        "list_quarantine",
        "begin_replay",
        "update_replay_report",
        "list_replay_reports",
        "cancel_retired_subscription",
        "get_retirement_cancellation_report",
    }

    assert required_methods <= set(dir(ports.EventStorePort))
    assert issubclass(ports.EventStorePort, ports.TransactionalStateReaderPort)
    source = (ROOT / "framework" / "events" / "ports.py").read_text(encoding="utf-8")
    assert "TYPE_CHECKING" in source
    assert "from framework.events.canonical import EventCandidate, StoredEvent" in source


def test_runtime_models_are_frozen_value_objects() -> None:
    dataclass_types = [
        getattr(models, exported_name)
        for exported_name in models.__all__
        if isinstance(getattr(models, exported_name), type)
        and is_dataclass(getattr(models, exported_name))
    ]

    assert dataclass_types
    assert all(model.__dataclass_params__.frozen for model in dataclass_types)

    policy = RetryPolicy()
    with pytest.raises(FrozenInstanceError):
        policy.max_attempts = 2  # type: ignore[misc]


def test_delivery_and_subscription_enums_preserve_durable_state_vocabulary() -> None:
    assert {value.value for value in DeliveryState} == {
        "pending",
        "claimed",
        "retry_wait",
        "acked",
        "dropped",
        "dead_letter",
    }
    assert {value.value for value in SubscriptionStartPolicy} == {
        "earliest",
        "latest",
        "at_sequence",
    }
    assert not DeliveryState.RETRY_WAIT.is_terminal
    assert all(
        state.is_terminal
        for state in (
            DeliveryState.ACKED,
            DeliveryState.DROPPED,
            DeliveryState.DEAD_LETTER,
        )
    )


def test_stream_cursor_is_1_based_while_legacy_offset_is_explicitly_0_based() -> None:
    assert LegacyEventOffset(0).value == 0
    assert StreamSequenceCursor("run:one", 1, 2).after_sequence == 1

    with pytest.raises(ValueError, match="after_sequence"):
        StreamSequenceCursor("run:one", 0, 2)
    with pytest.raises(ValueError, match="legacy_event_offset"):
        LegacyEventOffset(-1)


def test_sequence_cursor_pagination_validates_stream_and_limit() -> None:
    cursor = StreamSequenceCursor("run:one", 7, 9)
    request = StreamReadRequest("run:one", cursor=cursor, through_sequence=9)

    assert request.limit == models.DEFAULT_PAGE_LIMIT
    assert request.cursor is cursor
    assert StreamReadRequest("run:one", cursor=cursor).through_sequence == 9

    with pytest.raises(ValueError, match="cursor stream_id"):
        StreamReadRequest("run:two", cursor=cursor)
    with pytest.raises(ValueError, match="limit"):
        StreamReadRequest("run:one", limit=0)
    with pytest.raises(ValueError, match="limit"):
        StreamReadRequest("run:one", limit=models.MAX_PAGE_LIMIT + 1)
    with pytest.raises(ValueError, match="through_sequence"):
        StreamReadRequest("run:one", cursor=cursor, through_sequence=6)
    tenant_cursor = StreamSequenceCursor("run:one", 7, 9, tenant_id="tenant-a")
    with pytest.raises(ValueError, match="cursor tenant_id"):
        StreamReadRequest("run:one", cursor=tenant_cursor, tenant_id="tenant-b")


def test_subscription_version_delivery_generation_and_checkpoint_are_distinct() -> None:
    subscription = SubscriptionKey("projection", 2)
    delivery = DeliveryKey(
        event_id="evt-1",
        subscription_id=subscription.subscription_id,
        subscription_version=subscription.subscription_version,
        delivery_generation=3,
    )
    checkpoint = CheckpointKey(
        subscription_id=subscription.subscription_id,
        subscription_version=subscription.subscription_version,
        stream_id="run:one",
        tenant_id="tenant-a",
    )

    assert (delivery.subscription_version, delivery.delivery_generation) == (2, 3)
    assert checkpoint.subscription_version == 2
    assert checkpoint.tenant_id == "tenant-a"
    assert not hasattr(checkpoint, "offset")

    with pytest.raises(ValueError, match="subscription_version"):
        SubscriptionKey("projection", 0)
    with pytest.raises(ValueError, match="delivery_generation"):
        DeliveryKey("evt-1", "projection", 1, 0)


def test_subscription_watermarks_are_scoped_per_tenant_stream() -> None:
    first = SubscriptionStreamState(
        subscription_id="projection",
        subscription_version=1,
        stream_id="run:one",
        start_sequence=1,
        registration_watermark=4,
        tenant_id="tenant-a",
    )
    second = SubscriptionStreamState(
        subscription_id="projection",
        subscription_version=1,
        stream_id="run:two",
        start_sequence=5,
        registration_watermark=8,
        retirement_watermark=10,
        tenant_id="tenant-a",
    )

    assert first.registration_watermark == 4
    assert second.retirement_watermark == 10
    assert not hasattr(DurableSubscription, "registration_watermark")
    with pytest.raises(ValueError, match="cannot precede"):
        SubscriptionStreamState(
            subscription_id="projection",
            subscription_version=1,
            stream_id="run:two",
            start_sequence=1,
            registration_watermark=8,
            retirement_watermark=7,
        )
    with pytest.raises(ValueError, match="registration_watermark plus one"):
        SubscriptionStreamState(
            subscription_id="projection",
            subscription_version=1,
            stream_id="run:future",
            start_sequence=10,
            registration_watermark=4,
        )


def test_checkpoint_frontier_accepts_none_or_a_1_based_terminal_sequence() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    empty = ConsumerCheckpoint(
        subscription_id="projection",
        subscription_version=1,
        stream_id="run:one",
        highest_contiguous_terminal_sequence=None,
        last_event_id=None,
        terminal_disposition=None,
        updated_at=now,
        checksum=CHECKSUM_ZERO,
    )
    advanced = ConsumerCheckpoint(
        subscription_id="projection",
        subscription_version=1,
        stream_id="run:one",
        highest_contiguous_terminal_sequence=1,
        last_event_id="evt-1",
        terminal_disposition=DeliveryState.ACKED,
        updated_at=now,
        checksum=CHECKSUM_ONE,
        tenant_id="tenant-a",
    )

    assert empty.highest_contiguous_terminal_sequence is None
    assert advanced.highest_contiguous_terminal_sequence == 1
    assert advanced.key.tenant_id == "tenant-a"

    with pytest.raises(ValueError, match="highest_contiguous_terminal_sequence"):
        ConsumerCheckpoint(
            subscription_id="projection",
            subscription_version=1,
            stream_id="run:one",
            highest_contiguous_terminal_sequence=0,
            last_event_id="evt-zero",
            terminal_disposition=DeliveryState.ACKED,
            updated_at=now,
            checksum=CHECKSUM_ZERO,
        )


def test_retirement_cancellation_models_are_bounded_and_audit_prior_state() -> None:
    now = datetime(2026, 7, 17, tzinfo=UTC)
    subscription = SubscriptionKey("retired-projection", 2)
    request = RetirementCancellationRequest(
        cancellation_id="retirement-cancel-model",
        subscription=subscription,
        requested_at=now,
        operator_id="operator-1",
        operator_reason="retired projection is decommissioned",
        authorization_evidence_ref="authz:model-retirement-cancel",
        tenant_id="tenant-a",
        limit=10,
    )
    item = RetirementCancellationItem(
        cancellation_id=request.cancellation_id,
        delivery_id="delivery-1",
        event_id="event-1",
        stream_id="run:one",
        stream_sequence=1,
        subscription=subscription,
        delivery_generation=1,
        previous_state=DeliveryState.CLAIMED,
        previous_attempt_count=1,
        cancelled_at=now,
        tenant_id="tenant-a",
    )
    report = RetirementCancellationReport(
        cancellation_id=request.cancellation_id,
        subscription=subscription,
        requested_at=now,
        cancelled_at=now,
        operator_id=request.operator_id,
        operator_reason=request.operator_reason,
        authorization_evidence_ref=request.authorization_evidence_ref,
        item_limit=request.limit,
        remaining_nonterminal_count=2,
        items=(item,),
        tenant_id="tenant-a",
    )

    assert report.cancelled_count == 1
    assert report.completed is False
    assert report.items[0].previous_state is DeliveryState.CLAIMED
    assert report.items[0].terminal_state is DeliveryState.DROPPED

    with pytest.raises(ValueError, match="cannot exceed"):
        RetirementCancellationRequest(
            cancellation_id="retirement-cancel-oversized",
            subscription=subscription,
            requested_at=now,
            operator_id="operator-1",
            operator_reason="oversized request",
            authorization_evidence_ref="authz:model-retirement-cancel",
            limit=models.MAX_RETIREMENT_CANCELLATION_ITEMS + 1,
        )
    with pytest.raises(ValueError, match="nonterminal prior state"):
        RetirementCancellationItem(
            cancellation_id=request.cancellation_id,
            delivery_id="delivery-terminal",
            event_id="event-terminal",
            stream_id="run:one",
            stream_sequence=1,
            subscription=subscription,
            delivery_generation=1,
            previous_state=DeliveryState.ACKED,
            previous_attempt_count=1,
            cancelled_at=now,
        )


def test_retry_policy_is_finite_bounded_and_uses_the_documented_defaults() -> None:
    policy = RetryPolicy()

    assert policy.max_attempts == 5
    assert policy.jitter_ratio == pytest.approx(0.2)
    assert [policy.base_delay_seconds(attempt) for attempt in range(1, 6)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
    ]
    assert policy.can_retry(4)
    assert not policy.can_retry(5)

    for kwargs in (
        {"max_attempts": 0},
        {"max_attempts": models.MAX_CONFIGURED_ATTEMPTS + 1},
        {"initial_delay_seconds": 0},
        {"multiplier": 0.5},
        {"initial_delay_seconds": 2, "max_delay_seconds": 1},
        {"jitter_ratio": 1},
        {"jitter_ratio": "0.2"},
    ):
        with pytest.raises(ValueError):
            RetryPolicy(**kwargs)


def test_lease_and_backpressure_limits_have_hard_bounds() -> None:
    assert LeasePolicy().duration_seconds == 30
    assert LeasePolicy(models.MIN_LEASE_SECONDS).duration_seconds == 5
    assert LeasePolicy(models.MAX_LEASE_SECONDS).duration_seconds == 300

    with pytest.raises(ValueError, match="duration_seconds"):
        LeasePolicy(models.MIN_LEASE_SECONDS - 0.01)
    with pytest.raises(ValueError, match="duration_seconds"):
        LeasePolicy(models.MAX_LEASE_SECONDS + 0.01)
    with pytest.raises(ValueError, match="batch_size"):
        DeliveryLimits(batch_size=101, max_in_flight=100)
    with pytest.raises(ValueError, match="pending_warning_threshold"):
        DeliveryLimits(pending_warning_threshold=100, pending_hard_limit=100)


def test_pending_delivery_stats_separate_frontier_lag_and_late_repair() -> None:
    oldest = datetime(2026, 7, 15, tzinfo=UTC)
    stats = PendingDeliveryStats(
        pending_count=3,
        lag=2,
        oldest_pending_at=oldest,
        oldest_pending_age_seconds=12.5,
        late_repair_pending_count=1,
        warning_threshold_reached=True,
        capacity_remaining=7,
    )

    assert stats.lag == 2
    assert stats.late_repair_pending_count == 1
    assert stats.oldest_pending_age_seconds == 12.5
    assert stats.warning_threshold_reached

    with pytest.raises(ValueError, match="cannot exceed pending_count"):
        PendingDeliveryStats(
            pending_count=1,
            lag=0,
            late_repair_pending_count=2,
        )
    with pytest.raises(ValueError, match="requires oldest_pending_at"):
        PendingDeliveryStats(
            pending_count=1,
            lag=1,
            oldest_pending_age_seconds=1,
        )


def test_at_sequence_start_is_inclusive_and_requires_a_1_based_sequence() -> None:
    start = SubscriptionStart(SubscriptionStartPolicy.AT_SEQUENCE, start_sequence=1)

    assert start.start_sequence == 1
    with pytest.raises(ValueError, match="AT_SEQUENCE"):
        SubscriptionStart(SubscriptionStartPolicy.AT_SEQUENCE)
    with pytest.raises(ValueError, match="only valid"):
        SubscriptionStart(SubscriptionStartPolicy.LATEST, start_sequence=1)


def test_external_effect_subscription_requires_an_idempotency_declaration() -> None:
    with pytest.raises(ValueError, match="external-effect consumers require"):
        ConsumerEffectContract(performs_external_effects=True)
    with pytest.raises(ValueError, match="external-effect consumers require"):
        ConsumerEffectContract(
            performs_external_effects=True,
            consumer_effect_id="email-send",
        )

    contract = ConsumerEffectContract(
        performs_external_effects=True,
        consumer_effect_id="email-send",
        idempotency_strategy=EffectIdempotencyStrategy.TARGET_IDEMPOTENCY_KEY,
    )
    subscription = DurableSubscription(
        subscription_id="email-projection",
        subscription_version=1,
        consumer_id="email-consumer",
        effect=contract,
    )

    assert subscription.effect.consumer_effect_id == "email-send"
    assert subscription.effect.idempotency_strategy is EffectIdempotencyStrategy.TARGET_IDEMPOTENCY_KEY


def test_delivery_lease_and_retry_settlement_keep_fencing_and_time_invariants() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    expires_at = now + timedelta(seconds=30)
    token = DeliveryLeaseToken(
        delivery_id="delivery-1",
        delivery_generation=1,
        lease_owner="dispatcher-1",
        lease_generation=1,
        lease_expires_at=expires_at,
    )
    assert token.lease_started_at is None
    with pytest.raises(ValueError, match="lease_started_at"):
        DeliveryLeaseToken(
            delivery_id="delivery-invalid-time",
            delivery_generation=1,
            lease_owner="dispatcher-1",
            lease_generation=1,
            lease_expires_at=expires_at,
            lease_started_at=expires_at,
        )
    claimed = DeliveryRecord(
        delivery_id="delivery-1",
        event_id="evt-1",
        stream_id="run:one",
        stream_sequence=1,
        subscription_id="projection",
        subscription_version=1,
        consumer_id="projection-consumer",
        state=DeliveryState.CLAIMED,
        attempt_count=1,
        lease_owner=token.lease_owner,
        lease_generation=token.lease_generation,
        lease_expires_at=token.lease_expires_at,
    )
    settlement = DeliverySettlement(
        lease=token,
        target_state=DeliveryState.RETRY_WAIT,
        settled_at=now,
        reason_class="transient",
        retry_available_at=now + timedelta(seconds=1),
    )

    assert claimed.lease_expires_at == token.lease_expires_at
    assert settlement.retry_available_at > settlement.settled_at

    with pytest.raises(ValueError, match="only CLAIMED"):
        DeliveryRecord(
            delivery_id="delivery-1",
            event_id="evt-1",
            stream_id="run:one",
            stream_sequence=1,
            subscription_id="projection",
            subscription_version=1,
            consumer_id="projection-consumer",
            state=DeliveryState.ACKED,
            attempt_count=1,
            lease_owner=token.lease_owner,
            lease_generation=token.lease_generation,
            lease_expires_at=token.lease_expires_at,
        )
    with pytest.raises(ValueError, match="after settled_at"):
        DeliverySettlement(
            lease=token,
            target_state=DeliveryState.RETRY_WAIT,
            settled_at=now,
            reason_class="transient",
            retry_available_at=now,
        )
    with pytest.raises(ValueError, match="redacted_diagnostic"):
        DeliverySettlement(
            lease=token,
            target_state=DeliveryState.ACKED,
            settled_at=now,
            redacted_diagnostic=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="reason_class exceeds"):
        DeliverySettlement(
            lease=token,
            target_state=DeliveryState.DEAD_LETTER,
            settled_at=now,
            reason_class="x" * (models.MAX_REASON_CLASS_LENGTH + 1),
        )


def test_effect_identity_is_independent_of_subscription_version_and_generation() -> None:
    effect = ConsumerEffectContract(
        performs_external_effects=True,
        consumer_effect_id="publish-report",
        idempotency_strategy=EffectIdempotencyStrategy.INBOX_TRANSACTION,
    )
    first = DurableSubscription("publisher", 1, "publisher-consumer", effect=effect)
    second = DurableSubscription("publisher", 2, "publisher-consumer", effect=effect)
    first_delivery = DeliveryKey("evt-1", "publisher", 1, delivery_generation=1)
    repair_delivery = DeliveryKey("evt-1", "publisher", 2, delivery_generation=4)

    assert first.effect.consumer_effect_id == second.effect.consumer_effect_id
    assert first_delivery != repair_delivery


def test_nested_subscription_values_are_deeply_immutable_and_strictly_typed() -> None:
    event_types = {"graph_started"}
    event_filter = SubscriptionFilter(event_types=event_types)
    event_types.add("graph_finished")

    assert event_filter.event_types == frozenset({"graph_started"})
    with pytest.raises(ValueError, match="event_filter"):
        DurableSubscription("projection", 1, "consumer", event_filter={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="subscription_id"):
        SubscriptionKey(123, 1)  # type: ignore[arg-type]


def test_replay_start_request_requires_audited_redelivery_context() -> None:
    now = datetime(2026, 7, 15, tzinfo=UTC)
    request = ReplayStartRequest(
        replay_id="replay-1",
        mode=ReplayMode.REBUILD_STATE,
        source_stream_id="run:one",
        requested_at=now,
    )

    assert request.requested_at is now
    with pytest.raises(ValueError, match="operator context"):
        ReplayStartRequest(
            replay_id="replay-2",
            mode=ReplayMode.REDELIVER,
            source_stream_id="run:one",
            requested_at=now,
        )
