from __future__ import annotations

import base64
import binascii
import os
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TypeVar

from framework.events.canonical import (
    EventCandidate,
    StoredEvent,
    assert_same_event_identity,
    checksum_for,
    thaw_canonical_json,
)
from framework.events.errors import (
    EventConsumerIdempotencyError,
    EventContractError,
    EventIdentityCollisionError,
    EventRetirementCancellationCollisionError,
    EventRetirementCancellationError,
    EventStaleLeaseError,
    EventStoreCapacityError,
    EventStoreContentionError,
    EventStoreCorruptionError,
    EventStoreError,
    EventStoreUnavailableError,
    EventStreamVersionConflictError,
    EventSubscriptionPositionError,
)
from framework.events.runtime.models import (
    AppendResult,
    CheckpointKey,
    CheckpointPage,
    CheckpointQuery,
    ClaimedDelivery,
    ConsumerCheckpoint,
    ConsumerEffectContract,
    DeadLetterAction,
    DeadLetterDisposition,
    DeadLetterPage,
    DeadLetterQuery,
    DeadLetterRecord,
    DeliveryClaimRequest,
    DeliveryLeaseToken,
    DeliveryLimits,
    DeliveryPage,
    DeliveryQuery,
    DeliveryRecord,
    DeliverySettlement,
    DeliverySettlementResult,
    DeliveryState,
    DurableSubscription,
    EffectIdempotencyStrategy,
    InboxEntry,
    InboxKey,
    LeasePolicy,
    MAX_REDELIVERY_ITEMS,
    MAX_RETIREMENT_CANCELLATION_ITEMS,
    PendingDeliveryStats,
    QuarantineDisposition,
    QuarantinePage,
    QuarantineQuery,
    QuarantineReason,
    QuarantineRecord,
    RedeliveryItem,
    RedeliveryReport,
    RedeliveryRequest,
    RetirementCancellationItem,
    RetirementCancellationReport,
    RetirementCancellationRequest,
    ReplayMode,
    ReplayReport,
    ReplayReportPage,
    ReplayReportQuery,
    ReplayStartRequest,
    ReplayStatus,
    ReplayVersion,
    RetryPolicy,
    EventPage,
    StreamReadRequest,
    StreamSequenceCursor,
    SubscriptionFilter,
    SubscriptionKey,
    SubscriptionPage,
    SubscriptionQuery,
    SubscriptionStart,
    SubscriptionStartPolicy,
    SubscriptionStatus,
    SubscriptionStreamState,
    SubscriptionStreamStatePage,
    SubscriptionStreamStateQuery,
    TransactionalStateSnapshot,
)
from framework.events.runtime.identity import dead_letter_id_for, delivery_id_for
from framework.shared.json import json_loads, stable_json_dumps
from framework.shared.time import format_datetime, parse_datetime, utc_now


ConnectionFactory = Callable[[], sqlite3.Connection]
Clock = Callable[[], datetime]
_T = TypeVar("_T")

SQLITE_SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0
DEFAULT_SYNCHRONOUS = "FULL"
_SYNCHRONOUS_POLICIES = frozenset({"OFF", "NORMAL", "FULL", "EXTRA"})
_TERMINAL_STATES = frozenset(
    {DeliveryState.ACKED.value, DeliveryState.DROPPED.value, DeliveryState.DEAD_LETTER.value}
)
_NONTERMINAL_STATES = frozenset(
    {DeliveryState.PENDING.value, DeliveryState.CLAIMED.value, DeliveryState.RETRY_WAIT.value}
)


_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS event_store_metadata (
    schema_version INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    CHECK (schema_version >= 1)
);

CREATE TABLE IF NOT EXISTS event_stream_sequences (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    stream_id TEXT NOT NULL,
    last_sequence INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, stream_id),
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (tenant_id IS NULL OR trim(tenant_id) <> ''),
    CHECK (trim(stream_id) <> ''),
    CHECK (last_sequence >= 0),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS event_transactional_states (
    namespace TEXT NOT NULL,
    state_key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    checksum TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (namespace, state_key),
    CHECK (trim(namespace) <> ''),
    CHECK (trim(state_key) <> ''),
    CHECK (revision >= 1),
    CHECK (checksum GLOB 'sha256:[0-9a-f]*' AND length(checksum) = 71),
    CHECK (json_valid(payload_json) AND json_type(payload_json) = 'object')
);

CREATE TABLE IF NOT EXISTS durable_events (
    event_id TEXT PRIMARY KEY,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    stream_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    data_schema TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    content_checksum TEXT NOT NULL,
    record_checksum TEXT NOT NULL,
    event_json TEXT NOT NULL,
    UNIQUE (event_id, tenant_scope),
    UNIQUE (tenant_scope, stream_id, stream_sequence),
    UNIQUE (event_id, tenant_scope, stream_id, stream_sequence),
    FOREIGN KEY (tenant_scope, stream_id)
        REFERENCES event_stream_sequences (tenant_scope, stream_id)
        ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (tenant_id IS NULL OR trim(tenant_id) <> ''),
    CHECK (trim(event_id) <> '' AND trim(stream_id) <> ''),
    CHECK (trim(event_type) <> '' AND trim(data_schema) <> ''),
    CHECK (stream_sequence >= 1),
    CHECK (content_checksum GLOB 'sha256:[0-9a-f]*' AND length(content_checksum) = 71),
    CHECK (record_checksum GLOB 'sha256:[0-9a-f]*' AND length(record_checksum) = 71),
    CHECK (json_valid(event_json))
);

CREATE INDEX IF NOT EXISTS idx_durable_events_stream
    ON durable_events (tenant_scope, stream_id, stream_sequence);
CREATE INDEX IF NOT EXISTS idx_durable_events_type
    ON durable_events (tenant_scope, event_type, stream_id, stream_sequence);
CREATE INDEX IF NOT EXISTS idx_durable_events_schema
    ON durable_events (tenant_scope, data_schema, stream_id, stream_sequence);

CREATE TABLE IF NOT EXISTS event_subscriptions (
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    consumer_id TEXT NOT NULL,
    consumer_effect_scope TEXT NOT NULL,
    consumer_effect_id TEXT,
    status TEXT NOT NULL,
    pending_hard_limit INTEGER NOT NULL,
    subscription_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subscription_id, subscription_version),
    UNIQUE (subscription_id, subscription_version, tenant_scope),
    UNIQUE (
        subscription_id,
        subscription_version,
        tenant_scope,
        consumer_id,
        consumer_effect_scope
    ),
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (consumer_effect_scope = COALESCE(consumer_effect_id, '')),
    CHECK (subscription_version >= 1),
    CHECK (trim(subscription_id) <> '' AND trim(consumer_id) <> ''),
    CHECK (status IN ('active', 'paused', 'retired')),
    CHECK (pending_hard_limit >= 1),
    CHECK (updated_at >= created_at),
    CHECK (json_valid(subscription_json))
);

CREATE INDEX IF NOT EXISTS idx_event_subscriptions_scope_status
    ON event_subscriptions (tenant_scope, status, subscription_id, subscription_version);

CREATE TABLE IF NOT EXISTS event_subscription_stream_states (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    stream_id TEXT NOT NULL,
    start_sequence INTEGER NOT NULL,
    registration_watermark INTEGER NOT NULL,
    retirement_watermark INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, subscription_id, subscription_version, stream_id),
    FOREIGN KEY (subscription_id, subscription_version, tenant_scope)
        REFERENCES event_subscriptions (subscription_id, subscription_version, tenant_scope)
        ON DELETE RESTRICT,
    FOREIGN KEY (tenant_scope, stream_id)
        REFERENCES event_stream_sequences (tenant_scope, stream_id)
        ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (subscription_version >= 1 AND trim(stream_id) <> ''),
    CHECK (start_sequence >= 1 AND registration_watermark >= 0),
    CHECK (start_sequence <= registration_watermark + 1),
    CHECK (retirement_watermark IS NULL OR retirement_watermark >= registration_watermark),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_event_subscription_states_stream
    ON event_subscription_stream_states (
        tenant_scope, stream_id, subscription_id, subscription_version
    );

CREATE TABLE IF NOT EXISTS event_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    stream_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    consumer_id TEXT NOT NULL,
    consumer_effect_scope TEXT NOT NULL,
    consumer_effect_id TEXT,
    delivery_generation INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    available_at TEXT,
    lease_owner TEXT,
    lease_generation INTEGER,
    lease_expires_at TEXT,
    first_failure_at TEXT,
    last_failure_at TEXT,
    reason_class TEXT,
    redacted_diagnostic TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (event_id, subscription_id, subscription_version, delivery_generation),
    UNIQUE (
        delivery_id, event_id, tenant_scope, stream_id, stream_sequence,
        subscription_id, subscription_version, consumer_id,
        consumer_effect_scope, delivery_generation
    ),
    UNIQUE (delivery_id, event_id, tenant_scope, consumer_effect_scope),
    FOREIGN KEY (event_id, tenant_scope, stream_id, stream_sequence)
        REFERENCES durable_events (event_id, tenant_scope, stream_id, stream_sequence)
        ON DELETE RESTRICT,
    FOREIGN KEY (
        subscription_id, subscription_version, tenant_scope,
        consumer_id, consumer_effect_scope
    ) REFERENCES event_subscriptions (
        subscription_id, subscription_version, tenant_scope,
        consumer_id, consumer_effect_scope
    ) ON DELETE RESTRICT,
    FOREIGN KEY (tenant_scope, subscription_id, subscription_version, stream_id)
        REFERENCES event_subscription_stream_states (
            tenant_scope, subscription_id, subscription_version, stream_id
        ) ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (consumer_effect_scope = COALESCE(consumer_effect_id, '')),
    CHECK (stream_sequence >= 1 AND subscription_version >= 1),
    CHECK (delivery_generation >= 1 AND attempt_count >= 0),
    CHECK (state IN ('pending', 'claimed', 'retry_wait', 'acked', 'dropped', 'dead_letter')),
    CHECK (
        (state = 'pending' AND attempt_count = 0)
        OR (state <> 'pending' AND attempt_count >= 1)
    ),
    CHECK (
        (lease_owner IS NULL AND lease_generation IS NULL AND lease_expires_at IS NULL)
        OR (
            lease_owner IS NOT NULL AND trim(lease_owner) <> ''
            AND lease_generation >= 1 AND lease_expires_at IS NOT NULL
        )
    ),
    CHECK (state = 'claimed' OR lease_owner IS NULL),
    CHECK (state <> 'claimed' OR lease_owner IS NOT NULL),
    CHECK (
        (first_failure_at IS NULL AND last_failure_at IS NULL)
        OR (first_failure_at IS NOT NULL AND last_failure_at >= first_failure_at)
    ),
    CHECK (
        state NOT IN ('retry_wait', 'dead_letter')
        OR (first_failure_at IS NOT NULL AND reason_class IS NOT NULL AND trim(reason_class) <> '')
    ),
    CHECK (redacted_diagnostic IS NULL OR length(redacted_diagnostic) <= 2048),
    CHECK (updated_at >= created_at)
);

CREATE INDEX IF NOT EXISTS idx_event_deliveries_claimable
    ON event_deliveries (
        subscription_id, subscription_version, state, available_at,
        tenant_scope, stream_id, stream_sequence
    );
CREATE INDEX IF NOT EXISTS idx_event_deliveries_lease
    ON event_deliveries (lease_expires_at, subscription_id, subscription_version);
CREATE INDEX IF NOT EXISTS idx_event_deliveries_stream
    ON event_deliveries (
        tenant_scope, subscription_id, subscription_version,
        stream_id, stream_sequence, delivery_generation
    );

CREATE TABLE IF NOT EXISTS event_inbox (
    event_id TEXT NOT NULL,
    consumer_effect_id TEXT NOT NULL,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    completed_at TEXT NOT NULL,
    delivery_id TEXT,
    result_checksum TEXT,
    PRIMARY KEY (event_id, consumer_effect_id),
    FOREIGN KEY (event_id, tenant_scope)
        REFERENCES durable_events (event_id, tenant_scope)
        ON DELETE RESTRICT,
    FOREIGN KEY (delivery_id, event_id, tenant_scope, consumer_effect_id)
        REFERENCES event_deliveries (
            delivery_id, event_id, tenant_scope, consumer_effect_scope
        ) ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(event_id) <> '' AND trim(consumer_effect_id) <> ''),
    CHECK (result_checksum IS NULL OR length(result_checksum) = 71)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_inbox_delivery
    ON event_inbox (delivery_id) WHERE delivery_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS event_consumer_checkpoints (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    stream_id TEXT NOT NULL,
    highest_contiguous_terminal_sequence INTEGER NOT NULL,
    last_event_id TEXT NOT NULL,
    terminal_disposition TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    checksum TEXT NOT NULL,
    checkpoint_version INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_scope, subscription_id, subscription_version, stream_id),
    FOREIGN KEY (tenant_scope, subscription_id, subscription_version, stream_id)
        REFERENCES event_subscription_stream_states (
            tenant_scope, subscription_id, subscription_version, stream_id
        ) ON DELETE RESTRICT,
    FOREIGN KEY (
        last_event_id, tenant_scope, stream_id,
        highest_contiguous_terminal_sequence
    ) REFERENCES durable_events (
        event_id, tenant_scope, stream_id, stream_sequence
    ) ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (highest_contiguous_terminal_sequence >= 1),
    CHECK (terminal_disposition IN ('acked', 'dropped', 'dead_letter')),
    CHECK (checkpoint_version >= 1),
    CHECK (length(checksum) = 71)
);

CREATE TABLE IF NOT EXISTS event_dead_letters (
    dead_letter_id TEXT PRIMARY KEY,
    delivery_id TEXT NOT NULL UNIQUE,
    event_id TEXT NOT NULL,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    stream_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    consumer_id TEXT NOT NULL,
    consumer_effect_scope TEXT NOT NULL,
    consumer_effect_id TEXT,
    delivery_generation INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    first_failure_at TEXT NOT NULL,
    last_failure_at TEXT NOT NULL,
    reason_class TEXT NOT NULL,
    redacted_diagnostic TEXT,
    disposition TEXT NOT NULL DEFAULT 'open',
    operator_id TEXT,
    operator_reason TEXT,
    updated_at TEXT,
    FOREIGN KEY (
        delivery_id, event_id, tenant_scope, stream_id, stream_sequence,
        subscription_id, subscription_version, consumer_id,
        consumer_effect_scope, delivery_generation
    ) REFERENCES event_deliveries (
        delivery_id, event_id, tenant_scope, stream_id, stream_sequence,
        subscription_id, subscription_version, consumer_id,
        consumer_effect_scope, delivery_generation
    ) ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (consumer_effect_scope = COALESCE(consumer_effect_id, '')),
    CHECK (stream_sequence >= 1 AND subscription_version >= 1),
    CHECK (delivery_generation >= 1 AND attempt_count >= 1),
    CHECK (last_failure_at >= first_failure_at),
    CHECK (trim(reason_class) <> ''),
    CHECK (redacted_diagnostic IS NULL OR length(redacted_diagnostic) <= 2048),
    CHECK (disposition IN ('open', 'requeued', 'resolved')),
    CHECK (
        (disposition = 'open' AND operator_id IS NULL AND operator_reason IS NULL AND updated_at IS NULL)
        OR (
            disposition IN ('requeued', 'resolved')
            AND operator_id IS NOT NULL AND operator_reason IS NOT NULL AND updated_at IS NOT NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_event_dead_letters_query
    ON event_dead_letters (
        tenant_scope, subscription_id, subscription_version,
        disposition, last_failure_at, dead_letter_id
    );

CREATE TABLE IF NOT EXISTS event_quarantine (
    quarantine_id TEXT PRIMARY KEY,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    envelope_schema TEXT,
    event_type TEXT,
    data_schema TEXT,
    redacted_diagnostic TEXT,
    disposition TEXT NOT NULL DEFAULT 'pending',
    operator_id TEXT,
    operator_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(quarantine_id) <> '' AND trim(source) <> ''),
    CHECK (reason IN (
        'unknown_envelope_schema', 'unknown_data_schema', 'schema_validation_failed',
        'missing_occurred_at', 'invalid_occurred_at', 'context_conflict',
        'identity_collision', 'corrupt_record', 'unsupported_legacy_mapping',
        'upcast_failed', 'security_scope_ambiguous'
    )),
    CHECK (redacted_diagnostic IS NULL OR length(redacted_diagnostic) <= 2048),
    CHECK (disposition IN ('pending', 'released', 'rejected')),
    CHECK (
        (disposition = 'pending' AND operator_id IS NULL AND operator_reason IS NULL AND updated_at IS NULL)
        OR (
            disposition IN ('released', 'rejected')
            AND operator_id IS NOT NULL AND operator_reason IS NOT NULL
            AND updated_at IS NOT NULL AND updated_at >= created_at
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_event_quarantine_query
    ON event_quarantine (tenant_scope, disposition, reason, created_at, quarantine_id);

CREATE TABLE IF NOT EXISTS event_replay_reports (
    replay_id TEXT PRIMARY KEY,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    mode TEXT NOT NULL,
    source_stream_id TEXT NOT NULL,
    high_watermark INTEGER NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    replay_json TEXT NOT NULL,
    FOREIGN KEY (tenant_scope, source_stream_id)
        REFERENCES event_stream_sequences (tenant_scope, stream_id)
        ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (mode IN ('rebuild_state', 'verify_history', 'redeliver')),
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed')),
    CHECK (high_watermark >= 0),
    CHECK (json_valid(replay_json))
);

CREATE INDEX IF NOT EXISTS idx_event_replay_reports_query
    ON event_replay_reports (
        tenant_scope, source_stream_id, mode, status, started_at, replay_id
    );

CREATE TABLE IF NOT EXISTS event_redelivery_reports (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    redelivery_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    source_stream_id TEXT NOT NULL,
    from_sequence INTEGER NOT NULL,
    requested_through_sequence INTEGER,
    through_sequence INTEGER NOT NULL,
    captured_high_watermark INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    operator_reason TEXT NOT NULL,
    authorization_evidence_ref TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, redelivery_id),
    FOREIGN KEY (subscription_id, subscription_version, tenant_scope)
        REFERENCES event_subscriptions (subscription_id, subscription_version, tenant_scope)
        ON DELETE RESTRICT,
    FOREIGN KEY (tenant_scope, source_stream_id)
        REFERENCES event_stream_sequences (tenant_scope, stream_id)
        ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(redelivery_id) <> '' AND trim(source_stream_id) <> ''),
    CHECK (subscription_version >= 1),
    CHECK (
        from_sequence >= 1
        AND through_sequence >= from_sequence
        AND through_sequence - from_sequence < 1000
        AND captured_high_watermark >= through_sequence
        AND (
            requested_through_sequence IS NULL
            OR requested_through_sequence = through_sequence
        )
    ),
    CHECK (trim(operator_id) <> '' AND trim(operator_reason) <> ''),
    CHECK (
        trim(authorization_evidence_ref) <> ''
        AND length(authorization_evidence_ref) <= 512
    ),
    CHECK (scheduled_at >= requested_at)
);

CREATE INDEX IF NOT EXISTS idx_event_redelivery_reports_scope_stream
    ON event_redelivery_reports (
        tenant_scope, source_stream_id, subscription_id,
        subscription_version, requested_at, redelivery_id
    );

CREATE TABLE IF NOT EXISTS event_redelivery_items (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    redelivery_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    delivery_id TEXT NOT NULL,
    delivery_generation INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (
        tenant_scope, redelivery_id, event_id,
        subscription_id, subscription_version
    ),
    UNIQUE (delivery_id),
    FOREIGN KEY (tenant_scope, redelivery_id)
        REFERENCES event_redelivery_reports (tenant_scope, redelivery_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (delivery_id)
        REFERENCES event_deliveries (delivery_id)
        ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(event_id) <> '' AND trim(stream_id) <> ''),
    CHECK (stream_sequence >= 1 AND subscription_version >= 1),
    CHECK (delivery_generation >= 2)
);

CREATE INDEX IF NOT EXISTS idx_event_redelivery_items_report_sequence
    ON event_redelivery_items (
        tenant_scope, redelivery_id, stream_sequence, event_id
    );

CREATE TABLE IF NOT EXISTS event_subscription_status_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    FOREIGN KEY (subscription_id, subscription_version)
        REFERENCES event_subscriptions (subscription_id, subscription_version)
        ON DELETE RESTRICT,
    CHECK (trim(reason) <> '')
);

CREATE TABLE IF NOT EXISTS event_retirement_cancellation_reports (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    cancellation_id TEXT NOT NULL,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    cancelled_at TEXT NOT NULL,
    operator_id TEXT NOT NULL,
    operator_reason TEXT NOT NULL,
    authorization_evidence_ref TEXT NOT NULL,
    item_limit INTEGER NOT NULL,
    cancelled_count INTEGER NOT NULL,
    remaining_nonterminal_count INTEGER NOT NULL,
    remaining_nonterminal_count_truncated INTEGER NOT NULL,
    PRIMARY KEY (tenant_scope, cancellation_id),
    UNIQUE (
        tenant_scope, cancellation_id, subscription_id, subscription_version
    ),
    FOREIGN KEY (subscription_id, subscription_version, tenant_scope)
        REFERENCES event_subscriptions (
            subscription_id, subscription_version, tenant_scope
        ) ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(cancellation_id) <> ''),
    CHECK (subscription_version >= 1),
    CHECK (cancelled_at >= requested_at),
    CHECK (trim(operator_id) <> '' AND trim(operator_reason) <> ''),
    CHECK (
        trim(authorization_evidence_ref) <> ''
        AND length(authorization_evidence_ref) <= 512
    ),
    CHECK (item_limit BETWEEN 1 AND 1000),
    CHECK (cancelled_count BETWEEN 0 AND item_limit),
    CHECK (remaining_nonterminal_count >= 0),
    CHECK (remaining_nonterminal_count_truncated IN (0, 1)),
    CHECK (
        (remaining_nonterminal_count_truncated = 0
            AND remaining_nonterminal_count <= item_limit)
        OR (remaining_nonterminal_count_truncated = 1
            AND remaining_nonterminal_count = item_limit + 1)
    )
);

CREATE INDEX IF NOT EXISTS idx_event_retirement_cancellation_reports_subscription
    ON event_retirement_cancellation_reports (
        tenant_scope, subscription_id, subscription_version,
        requested_at, cancellation_id
    );

CREATE TABLE IF NOT EXISTS event_retirement_cancellation_items (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    cancellation_id TEXT NOT NULL,
    delivery_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    stream_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    subscription_id TEXT NOT NULL,
    subscription_version INTEGER NOT NULL,
    delivery_generation INTEGER NOT NULL,
    previous_state TEXT NOT NULL,
    previous_attempt_count INTEGER NOT NULL,
    previous_reason_class TEXT,
    terminal_state TEXT NOT NULL,
    cancelled_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, cancellation_id, delivery_id),
    UNIQUE (delivery_id),
    FOREIGN KEY (
        tenant_scope, cancellation_id, subscription_id, subscription_version
    )
        REFERENCES event_retirement_cancellation_reports (
            tenant_scope, cancellation_id, subscription_id, subscription_version
        ) ON DELETE RESTRICT,
    FOREIGN KEY (delivery_id)
        REFERENCES event_deliveries (delivery_id)
        ON DELETE RESTRICT,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(delivery_id) <> '' AND trim(event_id) <> '' AND trim(stream_id) <> ''),
    CHECK (stream_sequence >= 1 AND subscription_version >= 1),
    CHECK (delivery_generation >= 1 AND previous_attempt_count >= 0),
    CHECK (
        (previous_state = 'pending' AND previous_attempt_count = 0)
        OR (previous_state IN ('claimed', 'retry_wait')
            AND previous_attempt_count >= 1)
    ),
    CHECK (terminal_state = 'dropped')
);

CREATE INDEX IF NOT EXISTS idx_event_retirement_cancellation_items_report
    ON event_retirement_cancellation_items (
        tenant_scope, cancellation_id, stream_id, stream_sequence,
        delivery_generation, delivery_id
    );
"""


class SQLiteEventStore:
    """Single-host durable event store backed by SQLite WAL transactions.

    The adapter deliberately opens a short-lived connection per operation.  A
    write unit of work owns one ``BEGIN IMMEDIATE`` transaction, so stream
    sequence allocation, immutable event insertion, and outbox materialization
    become visible together.  SQLite is suitable for one host; shared or
    multi-host deployments must use the PostgreSQL adapter.
    """

    def __init__(
        self,
        database: str | Path,
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
        synchronous: str = DEFAULT_SYNCHRONOUS,
        read_only: bool = False,
        connection_factory: ConnectionFactory | None = None,
        clock: Clock = utc_now,
        initialize: bool = True,
    ) -> None:
        timeout = float(busy_timeout_seconds)
        if timeout < 0:
            raise ValueError("busy_timeout_seconds must be non-negative")
        policy = str(synchronous).strip().upper()
        if policy not in _SYNCHRONOUS_POLICIES:
            raise ValueError(
                "synchronous must be one of OFF, NORMAL, FULL, or EXTRA"
            )
        self.database = str(database)
        self.busy_timeout_seconds = timeout
        self.synchronous = policy
        self.read_only = bool(read_only)
        self._connection_factory = connection_factory
        self._clock = clock
        self._uri = self.database.startswith("file:")

        if self.database == ":memory:" and self._connection_factory is None:
            raise ValueError(
                "durable SQLite event storage requires a file-backed database"
            )

        if self._connection_factory is None and not self._uri and self.database != ":memory:":
            path = Path(self.database)
            if not self.read_only:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise EventStoreUnavailableError(
                        "create SQLite event-store directory failed"
                    ) from exc
        if initialize:
            if self.read_only:
                self._verify_existing_schema()
            else:
                self._initialize_schema()

    @property
    def durability_policy(self) -> Mapping[str, str | int]:
        return {
            "journal_mode": "WAL",
            "synchronous": self.synchronous,
            "busy_timeout_ms": int(self.busy_timeout_seconds * 1000),
            "host_scope": "single-host",
        }

    def unit_of_work(self) -> SQLiteEventUnitOfWork:
        return SQLiteEventUnitOfWork(self)

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        unit_of_work = self.unit_of_work()
        try:
            unit_of_work.__enter__()
            result = unit_of_work.append_event(
                event,
                expected_last_sequence=expected_last_sequence,
            )
            unit_of_work.commit()
            return result
        finally:
            unit_of_work.__exit__(None, None, None)

    def settle_delivery(
        self,
        settlement: DeliverySettlement,
    ) -> DeliverySettlementResult:
        unit_of_work = self.unit_of_work()
        try:
            unit_of_work.__enter__()
            result = unit_of_work.settle_delivery(settlement)
            unit_of_work.commit()
            return result
        finally:
            unit_of_work.__exit__(None, None, None)

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            try:
                journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
                if self.database != ":memory:" and journal_mode.lower() != "wal":
                    raise EventStoreUnavailableError(
                        f"SQLite durable store requires WAL mode; got {journal_mode}"
                    )
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO event_store_metadata (schema_version, created_at) "
                    "VALUES (?, ?)",
                    (SQLITE_SCHEMA_VERSION, _time_text(self._clock())),
                )
                connection.commit()
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, operation="initialize SQLite event store") from exc
        self.verify_integrity()

    def _verify_existing_schema(self) -> None:
        with self._connection() as connection:
            try:
                row = connection.execute(
                    "SELECT schema_version FROM event_store_metadata "
                    "ORDER BY schema_version DESC LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, operation="open read-only SQLite event store") from exc
            if row is None or int(row[0]) != SQLITE_SCHEMA_VERSION:
                raise EventStoreCorruptionError(
                    "SQLite event store schema metadata is missing or unsupported"
                )
        self.verify_integrity()

    def _open_connection(self) -> sqlite3.Connection:
        try:
            if self._connection_factory is not None:
                connection = self._connection_factory()
            else:
                database = self.database
                uri = self._uri
                if self.read_only and not uri:
                    database = f"{Path(database).resolve().as_uri()}?mode=ro"
                    uri = True
                connection = sqlite3.connect(
                    database,
                    timeout=self.busy_timeout_seconds,
                    isolation_level=None,
                    uri=uri,
                    check_same_thread=False,
                )
            connection.row_factory = sqlite3.Row
            connection.create_function(
                "newsroom_epoch_us",
                1,
                _epoch_microseconds,
                deterministic=True,
            )
            connection.execute(f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}")
            connection.execute("PRAGMA foreign_keys=ON")
            if self.read_only:
                connection.execute("PRAGMA query_only=ON")
            else:
                connection.execute(f"PRAGMA synchronous={self.synchronous}")
            return connection
        except sqlite3.Error as exc:
            raise _map_sqlite_error(exc, operation="open SQLite event store") from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        try:
            with self._connection() as connection:
                yield connection
        except (EventStoreError, EventContractError, ValueError):
            raise
        except sqlite3.Error as exc:
            raise _map_sqlite_error(exc, operation="read SQLite event store") from exc

    def verify_integrity(self, *, full: bool = False) -> None:
        pragma = "integrity_check" if full else "quick_check"
        with self._connection() as connection:
            try:
                rows = connection.execute(f"PRAGMA {pragma}").fetchall()
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, operation=f"SQLite {pragma}") from exc
        findings = tuple(str(row[0]) for row in rows)
        if findings != ("ok",):
            raise EventStoreCorruptionError(
                f"SQLite {pragma} failed: {'; '.join(findings[:3])}"
            )

    def checkpoint_wal(self, *, mode: str = "FULL") -> tuple[int, int, int]:
        normalized = str(mode).strip().upper()
        if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("invalid WAL checkpoint mode")
        with self._read() as connection:
            try:
                row = connection.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, operation="checkpoint SQLite WAL") from exc
        if row is None:
            raise EventStoreCorruptionError("SQLite WAL checkpoint returned no result")
        return int(row[0]), int(row[1]), int(row[2])

    def backup_to(self, destination: str | Path) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.resolve() == Path(self.database).resolve():
            raise ValueError("backup destination must differ from the live database")
        try:
            with self._connection() as source:
                backup = sqlite3.connect(target)
                try:
                    source.backup(backup)
                    backup.commit()
                finally:
                    backup.close()
            SQLiteEventStore(target, read_only=True, initialize=True).verify_integrity(full=True)
        except (EventStoreError, ValueError):
            raise
        except sqlite3.Error as exc:
            raise _map_sqlite_error(exc, operation="backup SQLite event store") from exc
        return target

    @classmethod
    def restore_backup(
        cls,
        backup: str | Path,
        destination: str | Path,
        **store_options: Any,
    ) -> SQLiteEventStore:
        source_path = Path(backup)
        destination_path = Path(destination)
        if source_path.resolve() == destination_path.resolve():
            raise ValueError("backup and restore destination must differ")
        cls(source_path, read_only=True).verify_integrity(full=True)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination_path.with_name(f".{destination_path.name}.restore.tmp")
        try:
            if temporary.exists():
                temporary.unlink()
            source = sqlite3.connect(source_path)
            restored = sqlite3.connect(temporary)
            try:
                source.backup(restored)
                restored.commit()
            finally:
                restored.close()
                source.close()
            cls(temporary, read_only=True).verify_integrity(full=True)
            os.replace(temporary, destination_path)
        except (EventStoreError, ValueError):
            if temporary.exists():
                temporary.unlink()
            raise
        except (OSError, sqlite3.Error) as exc:
            if temporary.exists():
                temporary.unlink()
            if isinstance(exc, sqlite3.Error):
                raise _map_sqlite_error(exc, operation="restore SQLite event store") from exc
            raise EventStoreUnavailableError("restore SQLite event store failed") from exc
        return cls(destination_path, **store_options)

    def get_event(
        self,
        event_id: str,
        *,
        tenant_id: str | None = None,
    ) -> StoredEvent | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM durable_events WHERE event_id = ? AND tenant_scope = ?",
                (event_id, _tenant_scope(tenant_id)),
            ).fetchone()
        return None if row is None else _stored_event_from_row(row)

    def read_stream(self, request: StreamReadRequest) -> EventPage:
        tenant_scope = _tenant_scope(request.tenant_id)
        with self._read() as connection:
            current = _stream_watermark(
                connection,
                tenant_scope=tenant_scope,
                stream_id=request.stream_id,
            )
            if current == 0:
                return EventPage(
                    stream_id=request.stream_id,
                    events=(),
                    high_watermark=None,
                    tenant_id=request.tenant_id,
                )
            high_watermark = min(request.through_sequence or current, current)
            after_sequence = request.cursor.after_sequence if request.cursor is not None else 0
            clauses = [
                "tenant_scope = ?",
                "stream_id = ?",
                "stream_sequence > ?",
                "stream_sequence <= ?",
            ]
            params: list[Any] = [
                tenant_scope,
                request.stream_id,
                after_sequence,
                high_watermark,
            ]
            _append_in_filter(clauses, params, "event_type", request.event_types)
            _append_in_filter(clauses, params, "data_schema", request.data_schemas)
            rows = connection.execute(
                "SELECT * FROM durable_events WHERE "
                + " AND ".join(clauses)
                + " ORDER BY stream_sequence ASC LIMIT ?",
                (*params, request.limit + 1),
            ).fetchall()
        has_more = len(rows) > request.limit
        selected = rows[: request.limit]
        events = tuple(_stored_event_from_row(row) for row in selected)
        next_cursor = None
        if has_more and events and events[-1].stream_sequence < high_watermark:
            next_cursor = StreamSequenceCursor(
                stream_id=request.stream_id,
                after_sequence=events[-1].stream_sequence,
                high_watermark=high_watermark,
                tenant_id=request.tenant_id,
            )
        return EventPage(
            stream_id=request.stream_id,
            events=events,
            high_watermark=high_watermark,
            next_cursor=next_cursor,
            tenant_id=request.tenant_id,
        )

    def get_stream_high_watermark(
        self,
        stream_id: str,
        *,
        tenant_id: str | None = None,
    ) -> int | None:
        with self._read() as connection:
            value = _stream_watermark(
                connection,
                tenant_scope=_tenant_scope(tenant_id),
                stream_id=stream_id,
            )
        return value or None

    def load_transactional_state(
        self,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        with self._read() as connection:
            return _load_transactional_state(connection, namespace, key)

    def register_subscription(
        self,
        subscription: DurableSubscription,
    ) -> DurableSubscription:
        with self._write() as connection:
            existing = _select_subscription(connection, subscription.key)
            if existing is not None:
                if _subscription_definition(existing) != _subscription_definition(subscription):
                    raise ValueError(
                        "subscription version already exists with a different definition"
                    )
                return existing
            if subscription.status is SubscriptionStatus.RETIRED:
                raise ValueError("subscription cannot be initially RETIRED")
            now = self._clock()
            created_at = subscription.created_at or now
            updated_at = subscription.updated_at or created_at
            persisted = replace(
                subscription,
                created_at=created_at,
                updated_at=updated_at,
            )
            _insert_subscription(connection, persisted)
            streams = connection.execute(
                "SELECT stream_id, last_sequence FROM event_stream_sequences "
                "WHERE tenant_scope = ? ORDER BY stream_id",
                (_tenant_scope(persisted.tenant_id),),
            ).fetchall()
            backfilled_count = 0
            for stream in streams:
                watermark = int(stream["last_sequence"])
                start_sequence = _subscription_start_sequence(persisted.start, watermark)
                if start_sequence > watermark + 1:
                    raise EventSubscriptionPositionError(
                        subscription_id=persisted.subscription_id,
                        subscription_version=persisted.subscription_version,
                        stream_id=str(stream["stream_id"]),
                        requested_sequence=start_sequence,
                        maximum_sequence=watermark + 1,
                    )
                _insert_subscription_stream_state(
                    connection,
                    persisted,
                    stream_id=str(stream["stream_id"]),
                    start_sequence=start_sequence,
                    registration_watermark=watermark,
                    created_at=created_at,
                )
                if start_sequence <= watermark:
                    rows = connection.execute(
                        "SELECT * FROM durable_events WHERE tenant_scope = ? "
                        "AND stream_id = ? AND stream_sequence BETWEEN ? AND ? "
                        "ORDER BY stream_sequence",
                        (
                            _tenant_scope(persisted.tenant_id),
                            str(stream["stream_id"]),
                            start_sequence,
                            watermark,
                        ),
                    ).fetchall()
                    for row in rows:
                        if _event_matches_subscription_row(row, persisted):
                            if backfilled_count >= persisted.limits.pending_hard_limit:
                                raise EventStoreCapacityError(
                                    "subscription backfill exceeds its durable pending hard limit"
                                )
                            backfilled_count += _insert_delivery(
                                connection,
                                persisted,
                                row,
                                created_at=now,
                            )
            return persisted

    def get_subscription(self, key: SubscriptionKey) -> DurableSubscription | None:
        with self._read() as connection:
            return _select_subscription(connection, key)

    def list_subscriptions(self, query: SubscriptionQuery) -> SubscriptionPage:
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.status is not None:
            clauses.append("status = ?")
            params.append(query.status.value)
        cursor = _decode_cursor(query.cursor, kind="subscriptions", filters={
            "tenant_scope": _tenant_scope(query.tenant_id),
            "status": query.status.value if query.status is not None else None,
        })
        if cursor is not None:
            clauses.append("(subscription_id, subscription_version) > (?, ?)")
            params.extend([cursor[0], cursor[1]])
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_subscriptions WHERE "
                + " AND ".join(clauses)
                + " ORDER BY subscription_id, subscription_version LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "subscriptions",
                [str(last["subscription_id"]), int(last["subscription_version"])],
                {"tenant_scope": _tenant_scope(query.tenant_id), "status": query.status.value if query.status is not None else None},
            )
        return SubscriptionPage(
            subscriptions=tuple(_subscription_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    def get_subscription_stream_state(
        self,
        key: SubscriptionKey,
        stream_id: str,
        *,
        tenant_id: str | None = None,
    ) -> SubscriptionStreamState | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_subscription_stream_states WHERE tenant_scope = ? "
                "AND subscription_id = ? AND subscription_version = ? AND stream_id = ?",
                (_tenant_scope(tenant_id), key.subscription_id, key.subscription_version, stream_id),
            ).fetchone()
        return None if row is None else _subscription_state_from_row(row)

    def list_subscription_stream_states(
        self,
        query: SubscriptionStreamStateQuery,
    ) -> SubscriptionStreamStatePage:
        filters = {
            "tenant_scope": _tenant_scope(query.tenant_id),
            "subscription_id": query.subscription_id,
            "subscription_version": query.subscription_version,
            "stream_id": query.stream_id,
        }
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [filters["tenant_scope"]]
        for field in ("subscription_id", "subscription_version", "stream_id"):
            value = filters[field]
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        cursor = _decode_cursor(query.cursor, kind="subscription_states", filters=filters)
        if cursor is not None:
            clauses.append("(subscription_id, subscription_version, stream_id) > (?, ?, ?)")
            params.extend(cursor)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_subscription_stream_states WHERE "
                + " AND ".join(clauses)
                + " ORDER BY subscription_id, subscription_version, stream_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "subscription_states",
                [str(last["subscription_id"]), int(last["subscription_version"]), str(last["stream_id"])],
                filters,
            )
        return SubscriptionStreamStatePage(
            states=tuple(_subscription_state_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    def set_subscription_status(
        self,
        key: SubscriptionKey,
        status: SubscriptionStatus,
        *,
        changed_at: datetime,
        reason: str,
    ) -> DurableSubscription:
        normalized_status = SubscriptionStatus(status)
        if not str(reason).strip():
            raise ValueError("subscription status reason is required")
        changed_text = _time_text(changed_at)
        with self._write() as connection:
            current = _select_subscription(connection, key)
            if current is None:
                raise KeyError(f"unknown subscription: {key.subscription_id}@{key.subscription_version}")
            if current.updated_at is not None and changed_at < current.updated_at:
                raise ValueError("changed_at cannot precede subscription updated_at")
            if current.status is SubscriptionStatus.RETIRED and normalized_status is not SubscriptionStatus.RETIRED:
                raise ValueError("retired subscription cannot be reactivated")
            if normalized_status is SubscriptionStatus.RETIRED and current.status is not SubscriptionStatus.RETIRED:
                connection.execute(
                    "UPDATE event_subscription_stream_states AS state SET "
                    "retirement_watermark = (SELECT last_sequence FROM event_stream_sequences AS stream "
                    "WHERE stream.tenant_scope = state.tenant_scope AND stream.stream_id = state.stream_id), "
                    "updated_at = ? WHERE subscription_id = ? AND subscription_version = ?",
                    (changed_text, key.subscription_id, key.subscription_version),
                )
            updated = replace(current, status=normalized_status, updated_at=changed_at)
            connection.execute(
                "UPDATE event_subscriptions SET status = ?, updated_at = ?, subscription_json = ? "
                "WHERE subscription_id = ? AND subscription_version = ?",
                (
                    normalized_status.value,
                    changed_text,
                    _json(updated),
                    key.subscription_id,
                    key.subscription_version,
                ),
            )
            connection.execute(
                "INSERT INTO event_subscription_status_audit ("
                "subscription_id, subscription_version, previous_status, new_status, changed_at, reason"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    key.subscription_id,
                    key.subscription_version,
                    current.status.value,
                    normalized_status.value,
                    changed_text,
                    str(reason).strip(),
                ),
            )
            return updated

    def get_retirement_cancellation_report(
        self,
        cancellation_id: str,
        *,
        tenant_id: str | None = None,
    ) -> RetirementCancellationReport | None:
        normalized_id = _required_text(cancellation_id, "cancellation_id")
        tenant_scope = _tenant_scope(tenant_id)
        with self._read() as connection:
            return _select_retirement_cancellation_report(
                connection,
                normalized_id,
                tenant_scope=tenant_scope,
            )

    def cancel_retired_subscription(
        self,
        request: RetirementCancellationRequest,
    ) -> RetirementCancellationReport:
        if not isinstance(request, RetirementCancellationRequest):
            raise TypeError("request must be RetirementCancellationRequest")
        tenant_scope = _tenant_scope(request.tenant_id)
        with self._write() as connection:
            existing = _select_retirement_cancellation_report(
                connection,
                request.cancellation_id,
                tenant_scope=tenant_scope,
            )
            if existing is not None:
                _assert_retirement_cancellation_retry(existing, request)
                return existing

            subscription = _select_subscription(connection, request.subscription)
            if subscription is None or subscription.tenant_id != request.tenant_id:
                raise EventRetirementCancellationError(
                    "retirement cancellation subscription is not available in scope"
                )
            if subscription.status is not SubscriptionStatus.RETIRED:
                raise EventRetirementCancellationError(
                    "retirement cancellation requires a retired subscription"
                )
            invalid = connection.execute(
                "SELECT 1 FROM event_deliveries AS delivery "
                "LEFT JOIN event_subscription_stream_states AS state "
                "ON state.tenant_scope = delivery.tenant_scope "
                "AND state.subscription_id = delivery.subscription_id "
                "AND state.subscription_version = delivery.subscription_version "
                "AND state.stream_id = delivery.stream_id "
                "WHERE delivery.tenant_scope = ? "
                "AND delivery.subscription_id = ? "
                "AND delivery.subscription_version = ? "
                "AND delivery.state IN ('pending', 'claimed', 'retry_wait') "
                "AND (state.retirement_watermark IS NULL "
                "OR delivery.stream_sequence > state.retirement_watermark) LIMIT 1",
                (
                    tenant_scope,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                ),
            ).fetchone()
            if invalid is not None:
                raise EventStoreCorruptionError(
                    "retired subscription has work outside its retirement watermark"
                )
            rows = connection.execute(
                "SELECT delivery.* FROM event_deliveries AS delivery "
                "JOIN event_subscription_stream_states AS state "
                "ON state.tenant_scope = delivery.tenant_scope "
                "AND state.subscription_id = delivery.subscription_id "
                "AND state.subscription_version = delivery.subscription_version "
                "AND state.stream_id = delivery.stream_id "
                "WHERE delivery.tenant_scope = ? "
                "AND delivery.subscription_id = ? "
                "AND delivery.subscription_version = ? "
                "AND delivery.state IN ('pending', 'claimed', 'retry_wait') "
                "AND state.retirement_watermark IS NOT NULL "
                "AND delivery.stream_sequence <= state.retirement_watermark "
                "ORDER BY delivery.stream_id COLLATE BINARY, delivery.stream_sequence, "
                "delivery.delivery_generation, delivery.delivery_id LIMIT ?",
                (
                    tenant_scope,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                    request.limit,
                ),
            ).fetchall()
            time_floor = [self._clock(), request.requested_at]
            if subscription.updated_at is not None:
                time_floor.append(subscription.updated_at)
            time_floor.extend(_time_value(row["updated_at"]) for row in rows)
            cancelled_at = max(time_floor)
            cancelled_text = _time_text(cancelled_at)
            connection.execute(
                "INSERT INTO event_retirement_cancellation_reports ("
                "tenant_scope, tenant_id, cancellation_id, subscription_id, "
                "subscription_version, requested_at, cancelled_at, operator_id, "
                "operator_reason, authorization_evidence_ref, item_limit, "
                "cancelled_count, remaining_nonterminal_count, "
                "remaining_nonterminal_count_truncated"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (
                    tenant_scope,
                    request.tenant_id,
                    request.cancellation_id,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                    _time_text(request.requested_at),
                    cancelled_text,
                    request.operator_id,
                    request.operator_reason,
                    request.authorization_evidence_ref,
                    request.limit,
                    len(rows),
                ),
            )
            affected_streams: set[str] = set()
            for row in rows:
                previous = _delivery_from_row(row)
                connection.execute(
                    "INSERT INTO event_retirement_cancellation_items ("
                    "tenant_scope, tenant_id, cancellation_id, delivery_id, event_id, "
                    "stream_id, stream_sequence, subscription_id, subscription_version, "
                    "delivery_generation, previous_state, previous_attempt_count, "
                    "previous_reason_class, terminal_state, cancelled_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'dropped', ?)",
                    (
                        tenant_scope,
                        request.tenant_id,
                        request.cancellation_id,
                        previous.delivery_id,
                        previous.event_id,
                        previous.stream_id,
                        previous.stream_sequence,
                        previous.subscription_id,
                        previous.subscription_version,
                        previous.delivery_generation,
                        previous.state.value,
                        previous.attempt_count,
                        previous.reason_class,
                        cancelled_text,
                    ),
                )
                updated = connection.execute(
                    "UPDATE event_deliveries SET state = 'dropped', "
                    "attempt_count = CASE WHEN attempt_count < 1 THEN 1 ELSE attempt_count END, "
                    "available_at = NULL, lease_owner = NULL, lease_generation = NULL, "
                    "lease_expires_at = NULL, reason_class = 'subscription_retired', "
                    "redacted_diagnostic = NULL, updated_at = ? "
                    "WHERE delivery_id = ? AND state = ?",
                    (cancelled_text, previous.delivery_id, previous.state.value),
                )
                if updated.rowcount != 1:
                    raise EventRetirementCancellationError(
                        "retirement cancellation delivery changed concurrently"
                    )
                if previous.delivery_generation == 1:
                    affected_streams.add(previous.stream_id)
            for stream_id in sorted(affected_streams):
                _advance_checkpoint(
                    connection,
                    subscription,
                    stream_id,
                    settled_at=cancelled_at,
                )
            remaining_rows = connection.execute(
                "SELECT 1 FROM event_deliveries WHERE tenant_scope = ? "
                "AND subscription_id = ? AND subscription_version = ? "
                "AND state IN ('pending', 'claimed', 'retry_wait') LIMIT ?",
                (
                    tenant_scope,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                    request.limit + 1,
                ),
            ).fetchall()
            remaining = len(remaining_rows)
            remaining_truncated = remaining > request.limit
            connection.execute(
                "UPDATE event_retirement_cancellation_reports "
                "SET remaining_nonterminal_count = ?, "
                "remaining_nonterminal_count_truncated = ? "
                "WHERE tenant_scope = ? AND cancellation_id = ?",
                (
                    remaining,
                    int(remaining_truncated),
                    tenant_scope,
                    request.cancellation_id,
                ),
            )
            report = _select_retirement_cancellation_report(
                connection,
                request.cancellation_id,
                tenant_scope=tenant_scope,
            )
            if report is None:
                raise EventStoreCorruptionError(
                    "retirement cancellation report disappeared during commit"
                )
            return report

    def get_delivery(
        self,
        delivery_id: str,
        *,
        tenant_id: str | None = None,
    ) -> DeliveryRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_deliveries WHERE delivery_id = ? AND tenant_scope = ?",
                (delivery_id, _tenant_scope(tenant_id)),
            ).fetchone()
        return None if row is None else _delivery_from_row(row)

    def list_deliveries(self, query: DeliveryQuery) -> DeliveryPage:
        filters = {
            "tenant_scope": _tenant_scope(query.tenant_id),
            "subscription_id": query.subscription_id,
            "subscription_version": query.subscription_version,
            "stream_id": query.stream_id,
            "state": query.state.value if query.state is not None else None,
            "after_sequence": query.after_sequence,
        }
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [filters["tenant_scope"]]
        for field in ("subscription_id", "subscription_version", "stream_id", "state"):
            value = filters[field]
            if value is not None:
                clauses.append(f"{field} = ?")
                params.append(value)
        if query.after_sequence is not None:
            clauses.append("stream_sequence > ?")
            params.append(query.after_sequence)
        cursor = _decode_cursor(query.cursor, kind="deliveries", filters=filters)
        if cursor is not None:
            clauses.append("(stream_sequence, delivery_id) > (?, ?)")
            params.extend(cursor)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_deliveries WHERE " + " AND ".join(clauses)
                + " ORDER BY stream_sequence, delivery_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "deliveries",
                [int(last["stream_sequence"]), str(last["delivery_id"])],
                filters,
            )
        return DeliveryPage(
            records=tuple(_delivery_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    def claim_deliveries(
        self,
        request: DeliveryClaimRequest,
    ) -> tuple[ClaimedDelivery, ...]:
        with self._write() as connection:
            authoritative_now = self._clock()
            subscription = _select_subscription(
                connection,
                SubscriptionKey(request.subscription_id, request.subscription_version),
            )
            if subscription is None:
                raise KeyError(
                    f"unknown subscription: {request.subscription_id}@{request.subscription_version}"
                )
            if subscription.status is SubscriptionStatus.PAUSED:
                return ()
            tenant_scope = _tenant_scope(subscription.tenant_id)
            _recover_expired_sqlite_claims(
                connection,
                subscription,
                requested_at=authoritative_now,
            )
            active_claims = connection.execute(
                "SELECT COUNT(*) AS in_flight, "
                "COUNT(DISTINCT lease_owner) AS owner_count, "
                "MAX(CASE WHEN lease_owner = ? THEN 1 ELSE 0 END) AS owner_active "
                "FROM event_deliveries WHERE tenant_scope = ? AND subscription_id = ? "
                "AND subscription_version = ? AND state = 'claimed' "
                "AND newsroom_epoch_us(lease_expires_at) > newsroom_epoch_us(?)",
                (
                    request.lease_owner,
                    tenant_scope,
                    request.subscription_id,
                    request.subscription_version,
                    _time_text(authoritative_now),
                ),
            ).fetchone()
            current_in_flight = int(active_claims["in_flight"])
            owner_count = int(active_claims["owner_count"])
            owner_active = bool(active_claims["owner_active"])
            if not owner_active and owner_count >= subscription.limits.max_concurrency:
                return ()
            capacity = max(0, subscription.limits.max_in_flight - current_in_flight)
            limit = min(request.limit, subscription.limits.batch_size, capacity)
            if limit == 0:
                return ()
            clauses = [
                "d.subscription_id = ?",
                "d.subscription_version = ?",
                "d.tenant_scope = ?",
                "("
                "d.state = 'pending' OR "
                "(d.state = 'retry_wait' "
                "AND newsroom_epoch_us(d.available_at) <= newsroom_epoch_us(?)) OR "
                "(d.state = 'claimed' "
                "AND newsroom_epoch_us(d.lease_expires_at) "
                "<= newsroom_epoch_us(?))"
                ")",
            ]
            params: list[Any] = [
                request.subscription_id,
                request.subscription_version,
                tenant_scope,
                _time_text(authoritative_now),
                _time_text(authoritative_now),
            ]
            if request.stream_id is not None:
                clauses.append("d.stream_id = ?")
                params.append(request.stream_id)
            if subscription.status is SubscriptionStatus.RETIRED:
                clauses.append(
                    "d.stream_sequence <= COALESCE(s.retirement_watermark, -1)"
                )
            rows = connection.execute(
                "SELECT d.* FROM event_deliveries AS d "
                "JOIN event_subscription_stream_states AS s ON "
                "s.tenant_scope = d.tenant_scope AND s.subscription_id = d.subscription_id "
                "AND s.subscription_version = d.subscription_version AND s.stream_id = d.stream_id "
                "WHERE " + " AND ".join(clauses)
                + " AND (d.delivery_generation > 1 OR NOT EXISTS ("
                "SELECT 1 FROM event_deliveries AS earlier WHERE "
                "earlier.tenant_scope = d.tenant_scope AND earlier.subscription_id = d.subscription_id "
                "AND earlier.subscription_version = d.subscription_version "
                "AND earlier.stream_id = d.stream_id AND earlier.delivery_generation = 1 "
                "AND earlier.stream_sequence < d.stream_sequence "
                "AND earlier.state IN ('pending', 'claimed', 'retry_wait')"
                ")) ORDER BY d.stream_id, d.stream_sequence, d.delivery_generation LIMIT ?",
                (*params, limit),
            ).fetchall()
            claimed: list[ClaimedDelivery] = []
            for row in rows:
                attempt_count = int(row["attempt_count"]) + 1
                lease_generation = max(int(row["lease_generation"] or 0) + 1, attempt_count)
                lease_expires_at = authoritative_now + timedelta(
                    seconds=request.lease_duration_seconds
                )
                updated = connection.execute(
                    "UPDATE event_deliveries SET state = 'claimed', attempt_count = ?, "
                    "available_at = NULL, lease_owner = ?, lease_generation = ?, "
                    "lease_expires_at = ?, updated_at = ? WHERE delivery_id = ? AND "
                    "(state = 'pending' OR (state = 'retry_wait' "
                    "AND newsroom_epoch_us(available_at) <= newsroom_epoch_us(?)) "
                    "OR (state = 'claimed' "
                    "AND newsroom_epoch_us(lease_expires_at) "
                    "<= newsroom_epoch_us(?))) RETURNING *",
                    (
                        attempt_count,
                        request.lease_owner,
                        lease_generation,
                        _time_text(lease_expires_at),
                        _time_text(authoritative_now),
                        str(row["delivery_id"]),
                        _time_text(authoritative_now),
                        _time_text(authoritative_now),
                    ),
                ).fetchone()
                if updated is None:
                    continue
                delivery = _delivery_from_row(updated)
                event_row = connection.execute(
                    "SELECT * FROM durable_events WHERE event_id = ?",
                    (delivery.event_id,),
                ).fetchone()
                if event_row is None:
                    raise EventStoreCorruptionError("delivery references a missing event")
                lease = DeliveryLeaseToken(
                    delivery_id=delivery.delivery_id,
                    delivery_generation=delivery.delivery_generation,
                    lease_owner=request.lease_owner,
                    lease_generation=lease_generation,
                    lease_expires_at=lease_expires_at,
                    lease_started_at=authoritative_now,
                )
                claimed.append(
                    ClaimedDelivery(
                        delivery=delivery,
                        event=_stored_event_from_row(event_row),
                        lease=lease,
                    )
                )
            return tuple(claimed)

    def renew_delivery_lease(
        self,
        lease: DeliveryLeaseToken,
        *,
        renewed_at: datetime,
        lease_duration_seconds: float,
    ) -> DeliveryLeaseToken:
        _time_text(renewed_at)
        LeasePolicy(lease_duration_seconds)
        with self._write() as connection:
            authoritative_now = self._clock()
            new_expiry = authoritative_now + timedelta(seconds=lease_duration_seconds)
            row = connection.execute(
                "UPDATE event_deliveries SET lease_expires_at = ?, updated_at = ? "
                "WHERE delivery_id = ? AND delivery_generation = ? AND state = 'claimed' "
                "AND lease_owner = ? AND lease_generation = ? AND lease_expires_at = ? "
                "AND newsroom_epoch_us(lease_expires_at) > newsroom_epoch_us(?) "
                "RETURNING delivery_id",
                (
                    _time_text(new_expiry),
                    _time_text(authoritative_now),
                    lease.delivery_id,
                    lease.delivery_generation,
                    lease.lease_owner,
                    lease.lease_generation,
                    _time_text(lease.lease_expires_at),
                    _time_text(authoritative_now),
                ),
            ).fetchone()
            if row is None:
                raise EventStaleLeaseError("delivery lease is stale or expired")
        return replace(
            lease,
            lease_expires_at=new_expiry,
            lease_started_at=authoritative_now,
        )

    def pending_delivery_stats(
        self,
        key: SubscriptionKey,
        *,
        stream_id: str | None = None,
    ) -> PendingDeliveryStats:
        authoritative_now = self._clock()
        with self._read() as connection:
            subscription = _select_subscription(connection, key)
            if subscription is None:
                raise KeyError(
                    f"unknown subscription: {key.subscription_id}@{key.subscription_version}"
                )
            clauses = [
                "subscription_id = ?",
                "subscription_version = ?",
                "tenant_scope = ?",
                "state IN ('pending', 'claimed', 'retry_wait')",
            ]
            params: list[Any] = [
                key.subscription_id,
                key.subscription_version,
                _tenant_scope(subscription.tenant_id),
            ]
            if stream_id is not None:
                clauses.append("stream_id = ?")
                params.append(stream_id)
            row = connection.execute(
                "WITH pending AS ("
                "SELECT delivery_generation, created_at FROM event_deliveries WHERE "
                + " AND ".join(clauses)
                + ") SELECT COUNT(*) AS pending_count, "
                "SUM(CASE WHEN delivery_generation = 1 THEN 1 ELSE 0 END) AS lag, "
                "SUM(CASE WHEN delivery_generation > 1 THEN 1 ELSE 0 END) "
                "AS late_repair_pending_count, "
                "(SELECT created_at FROM pending "
                "ORDER BY newsroom_epoch_us(created_at), created_at LIMIT 1) "
                "AS oldest_pending_at FROM pending",
                params,
            ).fetchone()
        count = int(row["pending_count"])
        oldest_pending_at = _time_value(row["oldest_pending_at"]) if count else None
        return PendingDeliveryStats(
            pending_count=count,
            lag=int(row["lag"] or 0),
            oldest_pending_at=oldest_pending_at,
            oldest_pending_age_seconds=(
                max(0.0, (authoritative_now - oldest_pending_at).total_seconds())
                if oldest_pending_at is not None
                else None
            ),
            late_repair_pending_count=int(row["late_repair_pending_count"] or 0),
            warning_threshold_reached=(
                count >= subscription.limits.pending_warning_threshold
            ),
            capacity_remaining=max(
                0,
                subscription.limits.pending_hard_limit - count,
            ),
        )

    def get_inbox_entry(
        self,
        key: InboxKey,
        *,
        tenant_id: str | None = None,
    ) -> InboxEntry | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_inbox WHERE event_id = ? AND consumer_effect_id = ? "
                "AND tenant_scope = ?",
                (key.event_id, key.consumer_effect_id, _tenant_scope(tenant_id)),
            ).fetchone()
        return None if row is None else _inbox_from_row(row)

    def get_checkpoint(
        self,
        key: CheckpointKey,
        *,
        tenant_id: str | None = None,
    ) -> ConsumerCheckpoint | None:
        scope_tenant = key.tenant_id if getattr(key, "tenant_id", None) is not None else tenant_id
        if getattr(key, "tenant_id", None) is not None and tenant_id is not None and key.tenant_id != tenant_id:
            raise ValueError("checkpoint tenant scopes conflict")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_consumer_checkpoints WHERE tenant_scope = ? "
                "AND subscription_id = ? AND subscription_version = ? AND stream_id = ?",
                (
                    _tenant_scope(scope_tenant),
                    key.subscription_id,
                    key.subscription_version,
                    key.stream_id,
                ),
            ).fetchone()
        return None if row is None else _checkpoint_from_row(row)

    def list_checkpoints(self, query: CheckpointQuery) -> CheckpointPage:
        filters = {
            "tenant_scope": _tenant_scope(query.tenant_id),
            "subscription_id": query.subscription_id,
            "subscription_version": query.subscription_version,
            "stream_id": query.stream_id,
        }
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [filters["tenant_scope"]]
        for field in ("subscription_id", "subscription_version", "stream_id"):
            if filters[field] is not None:
                clauses.append(f"{field} = ?")
                params.append(filters[field])
        cursor = _decode_cursor(query.cursor, kind="checkpoints", filters=filters)
        if cursor is not None:
            clauses.append("(subscription_id, subscription_version, stream_id) > (?, ?, ?)")
            params.extend(cursor)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_consumer_checkpoints WHERE " + " AND ".join(clauses)
                + " ORDER BY subscription_id, subscription_version, stream_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "checkpoints",
                [str(last["subscription_id"]), int(last["subscription_version"]), str(last["stream_id"])],
                filters,
            )
        return CheckpointPage(
            checkpoints=tuple(_checkpoint_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    def get_dead_letter(
        self,
        dead_letter_id: str,
        *,
        tenant_id: str | None = None,
    ) -> DeadLetterRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_dead_letters WHERE dead_letter_id = ? AND tenant_scope = ?",
                (dead_letter_id, _tenant_scope(tenant_id)),
            ).fetchone()
        return None if row is None else _dead_letter_from_row(row)

    def list_dead_letters(self, query: DeadLetterQuery) -> DeadLetterPage:
        filters = {
            "tenant_scope": _tenant_scope(query.tenant_id),
            "subscription_id": query.subscription_id,
            "subscription_version": query.subscription_version,
            "disposition": query.disposition.value if query.disposition is not None else None,
        }
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [filters["tenant_scope"]]
        for field in ("subscription_id", "subscription_version", "disposition"):
            if filters[field] is not None:
                clauses.append(f"{field} = ?")
                params.append(filters[field])
        cursor = _decode_cursor(query.cursor, kind="dead_letters", filters=filters)
        if cursor is not None:
            clauses.append("(last_failure_at, dead_letter_id) > (?, ?)")
            params.extend(cursor)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_dead_letters WHERE " + " AND ".join(clauses)
                + " ORDER BY last_failure_at, dead_letter_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "dead_letters",
                [str(last["last_failure_at"]), str(last["dead_letter_id"])],
                filters,
            )
        return DeadLetterPage(
            records=tuple(_dead_letter_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    def requeue_dead_letter(self, action: DeadLetterAction) -> DeliveryRecord:
        if not action.idempotency_ready:
            raise EventConsumerIdempotencyError(
                "dead-letter requeue requires an idempotency-ready consumer"
            )
        with self._write() as connection:
            authoritative_now = self._clock()
            row = connection.execute(
                "SELECT * FROM event_dead_letters WHERE dead_letter_id = ?",
                (action.dead_letter_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown dead letter: {action.dead_letter_id}")
            record = _dead_letter_from_row(row)
            if record.disposition is not DeadLetterDisposition.OPEN:
                raise ValueError("dead letter is no longer open")
            subscription = _select_subscription(
                connection,
                SubscriptionKey(record.subscription_id, record.subscription_version),
            )
            if subscription is None:
                raise EventStoreCorruptionError("dead letter references a missing subscription")
            if subscription.status is SubscriptionStatus.RETIRED:
                raise EventRetirementCancellationError(
                    "retired subscription cannot accept dead-letter requeue"
                )
            if not subscription.supports_out_of_order_repair:
                raise EventConsumerIdempotencyError(
                    "subscription does not support idempotent out-of-order repair"
                )
            event_row = connection.execute(
                "SELECT * FROM durable_events WHERE event_id = ?",
                (record.event_id,),
            ).fetchone()
            if event_row is None:
                raise EventStoreCorruptionError("dead letter references a missing event")
            if _subscription_pending_capacity_exhausted(connection, subscription):
                raise EventStoreCapacityError(
                    "subscription durable pending hard limit is exhausted"
                )
            next_generation = int(
                connection.execute(
                    "SELECT COALESCE(MAX(delivery_generation), 0) + 1 FROM event_deliveries "
                    "WHERE event_id = ? AND subscription_id = ? AND subscription_version = ?",
                    (record.event_id, record.subscription_id, record.subscription_version),
                ).fetchone()[0]
            )
            delivery_id = _delivery_id(
                record.event_id,
                record.subscription_id,
                record.subscription_version,
                next_generation,
            )
            connection.execute(
                "INSERT INTO event_deliveries ("
                "delivery_id, event_id, tenant_scope, tenant_id, stream_id, stream_sequence, "
                "subscription_id, subscription_version, consumer_id, consumer_effect_scope, "
                "consumer_effect_id, delivery_generation, state, attempt_count, available_at, "
                "lease_owner, lease_generation, lease_expires_at, first_failure_at, last_failure_at, "
                "reason_class, redacted_diagnostic, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, "
                "NULL, NULL, NULL, NULL, ?, ?)",
                (
                    delivery_id,
                    record.event_id,
                    str(row["tenant_scope"]),
                    record.tenant_id,
                    record.stream_id,
                    record.stream_sequence,
                    record.subscription_id,
                    record.subscription_version,
                    record.consumer_id,
                    _effect_scope(record.consumer_effect_id),
                    record.consumer_effect_id,
                    next_generation,
                    _time_text(authoritative_now),
                    _time_text(authoritative_now),
                    _time_text(authoritative_now),
                ),
            )
            operator_updated_at = max(
                authoritative_now,
                action.requested_at,
                record.updated_at or authoritative_now,
            )
            updated = connection.execute(
                "UPDATE event_dead_letters SET disposition = 'requeued', operator_id = ?, "
                "operator_reason = ?, updated_at = ? WHERE dead_letter_id = ? AND disposition = 'open'",
                (
                    action.operator_id,
                    action.reason,
                    _time_text(operator_updated_at),
                    action.dead_letter_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("dead letter changed concurrently")
            delivery_row = connection.execute(
                "SELECT * FROM event_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            return _delivery_from_row(delivery_row)

    def resolve_dead_letter(self, action: DeadLetterAction) -> DeadLetterRecord:
        with self._write() as connection:
            updated = connection.execute(
                "UPDATE event_dead_letters SET disposition = 'resolved', operator_id = ?, "
                "operator_reason = ?, updated_at = ? WHERE dead_letter_id = ? "
                "AND disposition = 'open' RETURNING *",
                (
                    action.operator_id,
                    action.reason,
                    _time_text(action.requested_at),
                    action.dead_letter_id,
                ),
            ).fetchone()
            if updated is None:
                exists = connection.execute(
                    "SELECT disposition FROM event_dead_letters WHERE dead_letter_id = ?",
                    (action.dead_letter_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError(f"unknown dead letter: {action.dead_letter_id}")
                raise ValueError("dead letter is no longer open")
            return _dead_letter_from_row(updated)

    def begin_redelivery(self, request: RedeliveryRequest) -> RedeliveryReport:
        if not isinstance(request, RedeliveryRequest):
            raise TypeError("request must be RedeliveryRequest")
        tenant_scope = _tenant_scope(request.tenant_id)
        with self._write() as connection:
            existing_row = connection.execute(
                "SELECT * FROM event_redelivery_reports "
                "WHERE tenant_scope = ? AND redelivery_id = ?",
                (tenant_scope, request.redelivery_id),
            ).fetchone()
            if existing_row is not None:
                existing = _redelivery_report_from_sqlite(connection, existing_row)
                if _redelivery_request_identity(existing) != _redelivery_request_identity(
                    request
                ):
                    raise EventStoreError(
                        "redelivery identity already has a different request"
                    )
                return existing

            subscription = _select_subscription(connection, request.subscription)
            if subscription is None or subscription.tenant_id != request.tenant_id:
                raise EventStoreError("redelivery subscription is unavailable in tenant scope")
            if subscription.status is SubscriptionStatus.RETIRED:
                raise EventStoreError("retired subscription cannot accept redelivery work")
            if not subscription.supports_out_of_order_repair:
                raise EventConsumerIdempotencyError(
                    "subscription does not support idempotent out-of-order repair"
                )

            captured_high_watermark = _stream_watermark(
                connection,
                tenant_scope=tenant_scope,
                stream_id=request.source_stream_id,
            )
            if captured_high_watermark == 0:
                raise EventStoreError("redelivery source stream does not exist")
            through_sequence = request.through_sequence or captured_high_watermark
            if request.from_sequence > captured_high_watermark:
                raise EventStoreError("redelivery range starts above the captured watermark")
            if through_sequence > captured_high_watermark:
                raise EventStoreError("redelivery range exceeds the captured watermark")
            if through_sequence - request.from_sequence + 1 > MAX_REDELIVERY_ITEMS:
                raise EventStoreCapacityError(
                    "redelivery range exceeds the bounded stream-position limit"
                )

            nonterminal = connection.execute(
                "SELECT 1 FROM event_deliveries WHERE tenant_scope = ? "
                "AND subscription_id = ? AND subscription_version = ? "
                "AND stream_id = ? AND stream_sequence BETWEEN ? AND ? "
                "AND state IN ('pending', 'claimed', 'retry_wait') LIMIT 1",
                (
                    tenant_scope,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                    request.source_stream_id,
                    request.from_sequence,
                    through_sequence,
                ),
            ).fetchone()
            if nonterminal is not None:
                raise EventStoreError(
                    "redelivery target contains nonterminal delivery work"
                )

            event_rows = connection.execute(
                "SELECT events.* FROM durable_events AS events "
                "JOIN event_deliveries AS deliveries "
                "ON deliveries.event_id = events.event_id "
                "AND deliveries.tenant_scope = events.tenant_scope "
                "AND deliveries.stream_id = events.stream_id "
                "AND deliveries.stream_sequence = events.stream_sequence "
                "WHERE deliveries.tenant_scope = ? "
                "AND deliveries.subscription_id = ? "
                "AND deliveries.subscription_version = ? "
                "AND deliveries.delivery_generation = 1 "
                "AND deliveries.stream_id = ? "
                "AND deliveries.stream_sequence BETWEEN ? AND ? "
                "ORDER BY deliveries.stream_sequence, deliveries.event_id "
                "LIMIT ?",
                (
                    tenant_scope,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                    request.source_stream_id,
                    request.from_sequence,
                    through_sequence,
                    MAX_REDELIVERY_ITEMS + 1,
                ),
            ).fetchall()
            if not event_rows:
                raise EventStoreError(
                    "redelivery range contains no existing event-consumer pair"
                )
            if len(event_rows) > MAX_REDELIVERY_ITEMS:
                raise EventStoreCapacityError(
                    "redelivery selection exceeds the "
                    f"{MAX_REDELIVERY_ITEMS}-item limit"
                )

            pending_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM event_deliveries WHERE tenant_scope = ? "
                    "AND subscription_id = ? AND subscription_version = ? "
                    "AND state IN ('pending', 'claimed', 'retry_wait')",
                    (
                        tenant_scope,
                        subscription.subscription_id,
                        subscription.subscription_version,
                    ),
                ).fetchone()[0]
            )
            if pending_count + len(event_rows) > subscription.limits.pending_hard_limit:
                raise EventStoreCapacityError(
                    "redelivery exceeds the subscription durable pending hard limit"
                )

            scheduled_at = max(self._clock(), request.requested_at)
            connection.execute(
                "INSERT INTO event_redelivery_reports ("
                "tenant_scope, tenant_id, redelivery_id, subscription_id, "
                "subscription_version, source_stream_id, from_sequence, "
                "requested_through_sequence, through_sequence, captured_high_watermark, "
                "requested_at, scheduled_at, operator_id, operator_reason, "
                "authorization_evidence_ref"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tenant_scope,
                    request.tenant_id,
                    request.redelivery_id,
                    request.subscription.subscription_id,
                    request.subscription.subscription_version,
                    request.source_stream_id,
                    request.from_sequence,
                    request.through_sequence,
                    through_sequence,
                    captured_high_watermark,
                    _time_text(request.requested_at),
                    _time_text(scheduled_at),
                    request.operator_id,
                    request.operator_reason,
                    request.authorization_evidence_ref,
                ),
            )

            for event_row in event_rows:
                event_id = str(event_row["event_id"])
                next_generation = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(delivery_generation), 0) + 1 "
                        "FROM event_deliveries WHERE event_id = ? "
                        "AND subscription_id = ? AND subscription_version = ?",
                        (
                            event_id,
                            subscription.subscription_id,
                            subscription.subscription_version,
                        ),
                    ).fetchone()[0]
                )
                if next_generation < 2:
                    raise EventStoreCorruptionError(
                        "redelivery target has no original delivery generation"
                    )
                inserted = _insert_delivery(
                    connection,
                    subscription,
                    event_row,
                    created_at=scheduled_at,
                    delivery_generation=next_generation,
                )
                if inserted != 1:
                    raise EventStoreCorruptionError(
                        "redelivery delivery generation was not inserted"
                    )
                delivery_id = _delivery_id(
                    event_id,
                    subscription.subscription_id,
                    subscription.subscription_version,
                    next_generation,
                )
                connection.execute(
                    "INSERT INTO event_redelivery_items ("
                    "tenant_scope, tenant_id, redelivery_id, event_id, stream_id, "
                    "stream_sequence, subscription_id, subscription_version, "
                    "delivery_id, delivery_generation, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tenant_scope,
                        request.tenant_id,
                        request.redelivery_id,
                        event_id,
                        request.source_stream_id,
                        int(event_row["stream_sequence"]),
                        subscription.subscription_id,
                        subscription.subscription_version,
                        delivery_id,
                        next_generation,
                        _time_text(scheduled_at),
                    ),
                )

            report_row = connection.execute(
                "SELECT * FROM event_redelivery_reports "
                "WHERE tenant_scope = ? AND redelivery_id = ?",
                (tenant_scope, request.redelivery_id),
            ).fetchone()
            if report_row is None:
                raise EventStoreCorruptionError(
                    "redelivery report disappeared before transaction commit"
                )
            return _redelivery_report_from_sqlite(connection, report_row)

    def get_redelivery_report(
        self,
        redelivery_id: str,
        *,
        tenant_id: str | None = None,
    ) -> RedeliveryReport | None:
        normalized_id = str(redelivery_id).strip()
        if not normalized_id:
            raise ValueError("redelivery_id is required")
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_redelivery_reports "
                "WHERE tenant_scope = ? AND redelivery_id = ?",
                (_tenant_scope(tenant_id), normalized_id),
            ).fetchone()
            return (
                None
                if row is None
                else _redelivery_report_from_sqlite(connection, row)
            )

    def save_quarantine(self, record: QuarantineRecord) -> QuarantineRecord:
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM event_quarantine WHERE quarantine_id = ?",
                (record.quarantine_id,),
            ).fetchone()
            if existing is not None:
                current = _quarantine_from_row(existing)
                if current != record:
                    raise EventIdentityCollisionError(record.quarantine_id)
                return current
            connection.execute(
                "INSERT INTO event_quarantine ("
                "quarantine_id, tenant_scope, tenant_id, source, reason, envelope_schema, "
                "event_type, data_schema, redacted_diagnostic, disposition, operator_id, "
                "operator_reason, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.quarantine_id,
                    _tenant_scope(record.tenant_id),
                    record.tenant_id,
                    record.source,
                    record.reason.value,
                    record.envelope_schema,
                    record.event_type,
                    record.data_schema,
                    record.redacted_diagnostic,
                    record.disposition.value,
                    record.operator_id,
                    record.operator_reason,
                    _time_text(record.created_at),
                    _optional_time_text(record.updated_at),
                ),
            )
            return record

    def get_quarantine(
        self,
        quarantine_id: str,
        *,
        tenant_id: str | None = None,
    ) -> QuarantineRecord | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_quarantine WHERE quarantine_id = ? AND tenant_scope = ?",
                (quarantine_id, _tenant_scope(tenant_id)),
            ).fetchone()
        return None if row is None else _quarantine_from_row(row)

    def list_quarantine(self, query: QuarantineQuery) -> QuarantinePage:
        filters = {
            "tenant_scope": _tenant_scope(query.tenant_id),
            "reason": query.reason.value if query.reason is not None else None,
            "disposition": query.disposition.value if query.disposition is not None else None,
        }
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [filters["tenant_scope"]]
        for field in ("reason", "disposition"):
            if filters[field] is not None:
                clauses.append(f"{field} = ?")
                params.append(filters[field])
        cursor = _decode_cursor(query.cursor, kind="quarantine", filters=filters)
        if cursor is not None:
            clauses.append("(created_at, quarantine_id) > (?, ?)")
            params.extend(cursor)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_quarantine WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at, quarantine_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "quarantine",
                [str(last["created_at"]), str(last["quarantine_id"])],
                filters,
            )
        return QuarantinePage(
            records=tuple(_quarantine_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    def resolve_quarantine(
        self,
        quarantine_id: str,
        disposition: QuarantineDisposition,
        *,
        operator_id: str,
        reason: str,
        resolved_at: datetime,
    ) -> QuarantineRecord:
        normalized = QuarantineDisposition(disposition)
        if normalized is QuarantineDisposition.PENDING:
            raise ValueError("quarantine resolution must be RELEASED or REJECTED")
        if not str(operator_id).strip() or not str(reason).strip():
            raise ValueError("quarantine resolution requires operator and reason")
        with self._write() as connection:
            row = connection.execute(
                "UPDATE event_quarantine SET disposition = ?, operator_id = ?, operator_reason = ?, "
                "updated_at = ? WHERE quarantine_id = ? AND disposition = 'pending' RETURNING *",
                (
                    normalized.value,
                    str(operator_id).strip(),
                    str(reason).strip(),
                    _time_text(resolved_at),
                    quarantine_id,
                ),
            ).fetchone()
            if row is None:
                exists = connection.execute(
                    "SELECT disposition FROM event_quarantine WHERE quarantine_id = ?",
                    (quarantine_id,),
                ).fetchone()
                if exists is None:
                    raise KeyError(f"unknown quarantine: {quarantine_id}")
                raise ValueError("quarantine is already resolved")
            return _quarantine_from_row(row)

    def begin_replay(self, request: ReplayStartRequest) -> ReplayReport:
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM event_replay_reports WHERE replay_id = ?",
                (request.replay_id,),
            ).fetchone()
            if existing is not None:
                current = _replay_from_row(existing)
                if (
                    current.mode is request.mode
                    and current.source_stream_id == request.source_stream_id
                    and current.tenant_id == request.tenant_id
                    and current.started_at == request.requested_at
                    and current.from_sequence == request.from_sequence
                    and current.checkpoint_ref == request.checkpoint_ref
                    and current.operator_id == request.operator_id
                    and current.operator_reason == request.operator_reason
                ):
                    return current
                raise EventIdentityCollisionError(request.replay_id)
            high_watermark = _stream_watermark(
                connection,
                tenant_scope=_tenant_scope(request.tenant_id),
                stream_id=request.source_stream_id,
            )
            if high_watermark == 0:
                raise ValueError("replay source stream does not exist or is empty")
            if request.from_sequence is not None and request.from_sequence > high_watermark:
                raise ValueError("replay from_sequence exceeds source high watermark")
            report = ReplayReport(
                replay_id=request.replay_id,
                mode=request.mode,
                source_stream_id=request.source_stream_id,
                high_watermark=high_watermark,
                status=ReplayStatus.PENDING,
                started_at=request.requested_at,
                from_sequence=request.from_sequence,
                checkpoint_ref=request.checkpoint_ref,
                tenant_id=request.tenant_id,
                operator_id=request.operator_id,
                operator_reason=request.operator_reason,
            )
            _insert_replay(connection, report)
            return report

    def update_replay_report(self, report: ReplayReport) -> ReplayReport:
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM event_replay_reports WHERE replay_id = ?",
                (report.replay_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown replay report: {report.replay_id}")
            current = _replay_from_row(row)
            _validate_replay_update(current, report)
            connection.execute(
                "UPDATE event_replay_reports SET status = ?, replay_json = ? WHERE replay_id = ?",
                (report.status.value, _json(report), report.replay_id),
            )
            return report

    def get_replay_report(
        self,
        replay_id: str,
        *,
        tenant_id: str | None = None,
    ) -> ReplayReport | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM event_replay_reports WHERE replay_id = ? AND tenant_scope = ?",
                (replay_id, _tenant_scope(tenant_id)),
            ).fetchone()
        return None if row is None else _replay_from_row(row)

    def list_replay_reports(self, query: ReplayReportQuery) -> ReplayReportPage:
        filters = {
            "tenant_scope": _tenant_scope(query.tenant_id),
            "source_stream_id": query.source_stream_id,
            "mode": query.mode.value if query.mode is not None else None,
            "status": query.status.value if query.status is not None else None,
        }
        clauses = ["tenant_scope = ?"]
        params: list[Any] = [filters["tenant_scope"]]
        for field in ("source_stream_id", "mode", "status"):
            if filters[field] is not None:
                clauses.append(f"{field} = ?")
                params.append(filters[field])
        cursor = _decode_cursor(query.cursor, kind="replays", filters=filters)
        if cursor is not None:
            clauses.append("(started_at, replay_id) > (?, ?)")
            params.extend(cursor)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM event_replay_reports WHERE " + " AND ".join(clauses)
                + " ORDER BY started_at, replay_id LIMIT ?",
                (*params, query.limit + 1),
            ).fetchall()
        selected = rows[: query.limit]
        next_cursor = None
        if len(rows) > query.limit:
            last = selected[-1]
            next_cursor = _encode_cursor(
                "replays",
                [str(last["started_at"]), str(last["replay_id"])],
                filters,
            )
        return ReplayReportPage(
            reports=tuple(_replay_from_row(row) for row in selected),
            next_cursor=next_cursor,
        )

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise _map_sqlite_error(exc, operation="write SQLite event store") from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _append_event(
        self,
        connection: sqlite3.Connection,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        if not isinstance(event, EventCandidate):
            raise TypeError("event must be EventCandidate")
        expected_last_sequence = _expected_last_sequence(expected_last_sequence)
        existing = connection.execute(
            "SELECT * FROM durable_events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()
        if existing is not None:
            stored = _stored_event_from_row(existing)
            assert_same_event_identity(stored, event)
            return AppendResult(event=stored, created=False, pending_delivery_count=0)

        tenant_scope = _tenant_scope(event.tenant_id)
        now = self._clock()
        now_text = _time_text(now)
        connection.execute(
            "INSERT INTO event_stream_sequences ("
            "tenant_scope, tenant_id, stream_id, last_sequence, created_at, updated_at"
            ") VALUES (?, ?, ?, 0, ?, ?) ON CONFLICT (tenant_scope, stream_id) DO NOTHING",
            (tenant_scope, event.tenant_id, event.stream_id, now_text, now_text),
        )
        if expected_last_sequence is not None:
            current_row = connection.execute(
                "SELECT last_sequence FROM event_stream_sequences "
                "WHERE tenant_scope = ? AND stream_id = ?",
                (tenant_scope, event.stream_id),
            ).fetchone()
            if current_row is None:
                raise EventStoreCorruptionError(
                    "stream sequence row disappeared before conditional append"
                )
            actual_last_sequence = int(current_row[0])
            if actual_last_sequence != expected_last_sequence:
                raise EventStreamVersionConflictError(
                    stream_id=event.stream_id,
                    expected_last_sequence=expected_last_sequence,
                    actual_last_sequence=actual_last_sequence,
                )
        sequence_row = connection.execute(
            "UPDATE event_stream_sequences SET last_sequence = last_sequence + 1, updated_at = ? "
            "WHERE tenant_scope = ? AND stream_id = ? RETURNING last_sequence",
            (now_text, tenant_scope, event.stream_id),
        ).fetchone()
        if sequence_row is None:
            raise EventStoreCorruptionError("stream sequence row disappeared during append")
        sequence = int(sequence_row[0])
        stored = StoredEvent(event, observed_at=now, stream_sequence=sequence)
        connection.execute(
            "INSERT INTO durable_events ("
            "event_id, tenant_scope, tenant_id, stream_id, stream_sequence, event_type, "
            "data_schema, observed_at, content_checksum, record_checksum, event_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stored.event_id,
                tenant_scope,
                stored.tenant_id,
                stored.stream_id,
                stored.stream_sequence,
                stored.event_type,
                stored.data_schema,
                _time_text(stored.observed_at),
                stored.content_checksum,
                stored.record_checksum,
                _json(stored),
            ),
        )
        subscriptions = connection.execute(
            "SELECT * FROM event_subscriptions WHERE tenant_scope = ? AND status <> 'retired' "
            "ORDER BY subscription_id, subscription_version",
            (tenant_scope,),
        ).fetchall()
        pending_count = 0
        event_row = connection.execute(
            "SELECT * FROM durable_events WHERE event_id = ?",
            (stored.event_id,),
        ).fetchone()
        for subscription_row in subscriptions:
            subscription = _subscription_from_row(subscription_row)
            state = connection.execute(
                "SELECT * FROM event_subscription_stream_states WHERE tenant_scope = ? "
                "AND subscription_id = ? AND subscription_version = ? AND stream_id = ?",
                (
                    tenant_scope,
                    subscription.subscription_id,
                    subscription.subscription_version,
                    stored.stream_id,
                ),
            ).fetchone()
            if state is None:
                start_sequence = _subscription_start_sequence(
                    subscription.start,
                    sequence - 1,
                )
                if start_sequence > sequence:
                    continue
                _insert_subscription_stream_state(
                    connection,
                    subscription,
                    stream_id=stored.stream_id,
                    start_sequence=start_sequence,
                    registration_watermark=sequence - 1,
                    created_at=now,
                )
                state = connection.execute(
                    "SELECT * FROM event_subscription_stream_states WHERE tenant_scope = ? "
                    "AND subscription_id = ? AND subscription_version = ? AND stream_id = ?",
                    (
                        tenant_scope,
                        subscription.subscription_id,
                        subscription.subscription_version,
                        stored.stream_id,
                    ),
                ).fetchone()
            if int(state["start_sequence"]) > sequence:
                continue
            retirement = state["retirement_watermark"]
            if retirement is not None and sequence > int(retirement):
                continue
            if not _event_matches_subscription_row(event_row, subscription):
                continue
            if _subscription_pending_capacity_exhausted(connection, subscription):
                raise EventStoreCapacityError(
                    "subscription pending delivery hard limit is exhausted"
                )
            pending_count += _insert_delivery(
                connection,
                subscription,
                event_row,
                created_at=now,
            )
        return AppendResult(
            event=stored,
            created=True,
            pending_delivery_count=pending_count,
        )

    def _settle_delivery(
        self,
        connection: sqlite3.Connection,
        settlement: DeliverySettlement,
    ) -> DeliverySettlementResult:
        lease = settlement.lease
        authoritative_now = self._clock()
        row = connection.execute(
            "SELECT * FROM event_deliveries WHERE delivery_id = ?",
            (lease.delivery_id,),
        ).fetchone()
        if (
            row is None
            or not _lease_matches(row, lease, at=authoritative_now)
        ):
            raise EventStaleLeaseError("delivery lease is stale or expired")
        delivery = _delivery_from_row(row)
        subscription = _select_subscription(
            connection,
            SubscriptionKey(delivery.subscription_id, delivery.subscription_version),
        )
        if subscription is None:
            raise EventStoreCorruptionError("delivery references a missing subscription")

        if (
            target := settlement.target_state
        ) is DeliveryState.ACKED and (
            subscription.effect.idempotency_strategy
            is EffectIdempotencyStrategy.INBOX_TRANSACTION
        ) and settlement.inbox_entry is None:
            raise EventConsumerIdempotencyError(
                "INBOX_TRANSACTION acknowledgement requires an inbox entry"
            )

        if target is DeliveryState.RETRY_WAIT and not subscription.retry_policy.can_retry(
            delivery.attempt_count
        ):
            target = DeliveryState.DEAD_LETTER
        first_failure_at = delivery.first_failure_at
        last_failure_at = delivery.last_failure_at
        reason_class = settlement.reason_class
        diagnostic = settlement.redacted_diagnostic
        delivery_time_floor = delivery.updated_at or authoritative_now
        delivery_updated_at = max(delivery_time_floor, authoritative_now)
        if target in {DeliveryState.RETRY_WAIT, DeliveryState.DEAD_LETTER}:
            failure_occurrence = max(
                delivery_time_floor,
                min(settlement.settled_at, authoritative_now),
            )
            first_failure_at = first_failure_at or failure_occurrence
            last_failure_at = (
                failure_occurrence
                if last_failure_at is None
                else max(last_failure_at, failure_occurrence)
            )
            if reason_class is None:
                raise ValueError(f"{target.value} settlement requires reason_class")

        inbox_recorded = False
        if settlement.inbox_entry is not None:
            inbox_recorded = _record_inbox(
                connection,
                delivery,
                settlement.inbox_entry,
            )

        updated = connection.execute(
            "UPDATE event_deliveries SET state = ?, available_at = ?, lease_owner = NULL, "
            "lease_generation = NULL, lease_expires_at = NULL, first_failure_at = ?, "
            "last_failure_at = ?, reason_class = ?, redacted_diagnostic = ?, updated_at = ? "
            "WHERE delivery_id = ? AND state = 'claimed' AND lease_owner = ? "
            "AND lease_generation = ? AND lease_expires_at = ? RETURNING *",
            (
                target.value,
                _optional_time_text(settlement.retry_available_at) if target is DeliveryState.RETRY_WAIT else None,
                _optional_time_text(first_failure_at),
                _optional_time_text(last_failure_at),
                reason_class,
                diagnostic,
                _time_text(delivery_updated_at),
                lease.delivery_id,
                lease.lease_owner,
                lease.lease_generation,
                _time_text(lease.lease_expires_at),
            ),
        ).fetchone()
        if updated is None:
            raise EventStaleLeaseError("delivery lease changed during settlement")
        dead_letter_id = None
        if target is DeliveryState.DEAD_LETTER:
            if first_failure_at is None or last_failure_at is None:
                raise EventStoreCorruptionError(
                    "dead-letter settlement is missing failure timestamps"
                )
            dead_letter_id = _dead_letter_id(
                delivery.delivery_id,
                delivery.delivery_generation,
            )
            connection.execute(
                "INSERT INTO event_dead_letters ("
                "dead_letter_id, delivery_id, event_id, tenant_scope, tenant_id, stream_id, "
                "stream_sequence, subscription_id, subscription_version, consumer_id, "
                "consumer_effect_scope, consumer_effect_id, delivery_generation, attempt_count, "
                "first_failure_at, last_failure_at, reason_class, redacted_diagnostic, disposition, "
                "operator_id, operator_reason, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, NULL)",
                (
                    dead_letter_id,
                    delivery.delivery_id,
                    delivery.event_id,
                    str(row["tenant_scope"]),
                    delivery.tenant_id,
                    delivery.stream_id,
                    delivery.stream_sequence,
                    delivery.subscription_id,
                    delivery.subscription_version,
                    delivery.consumer_id,
                    _effect_scope(delivery.consumer_effect_id),
                    delivery.consumer_effect_id,
                    delivery.delivery_generation,
                    delivery.attempt_count,
                    _time_text(first_failure_at),
                    _time_text(last_failure_at),
                    reason_class,
                    diagnostic,
                ),
            )
        checkpoint = None
        if target.is_terminal and delivery.delivery_generation == 1:
            checkpoint = _advance_checkpoint(
                connection,
                subscription,
                delivery.stream_id,
                settled_at=delivery_updated_at,
            )
        return DeliverySettlementResult(
            delivery=_delivery_from_row(updated),
            checkpoint=checkpoint,
            dead_letter_id=dead_letter_id,
            inbox_recorded=inbox_recorded,
        )


class SQLiteEventUnitOfWork:
    def __init__(self, store: SQLiteEventStore) -> None:
        self._store = store
        self._connection: sqlite3.Connection | None = None
        self._finished = False
        self._rollback_only = False

    def __enter__(self) -> SQLiteEventUnitOfWork:
        if self._connection is not None:
            raise RuntimeError("SQLite event unit of work is already active")
        connection = self._store._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            connection.close()
            raise _map_sqlite_error(exc, operation="begin SQLite event transaction") from exc
        self._connection = connection
        self._finished = False
        self._rollback_only = False
        return self

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        try:
            return self._store._append_event(
                self._require_connection(),
                event,
                expected_last_sequence=expected_last_sequence,
            )
        except (EventStoreError, EventContractError, ValueError, TypeError):
            self._rollback_only = True
            raise
        except sqlite3.Error as exc:
            self._rollback_only = True
            raise _map_sqlite_error(exc, operation="append SQLite event") from exc

    def settle_delivery(
        self,
        settlement: DeliverySettlement,
    ) -> DeliverySettlementResult:
        try:
            return self._store._settle_delivery(
                self._require_connection(),
                settlement,
            )
        except (EventStoreError, EventContractError, EventStaleLeaseError, ValueError):
            self._rollback_only = True
            raise
        except sqlite3.Error as exc:
            self._rollback_only = True
            raise _map_sqlite_error(exc, operation="settle SQLite delivery") from exc

    def load_transactional_state(
        self,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        try:
            return _load_transactional_state(
                self._require_connection(),
                namespace,
                key,
            )
        except (EventStoreError, ValueError, TypeError):
            self._rollback_only = True
            raise
        except sqlite3.Error as exc:
            self._rollback_only = True
            raise _map_sqlite_error(
                exc,
                operation="load SQLite transactional state",
            ) from exc

    def cas_transactional_state(
        self,
        *,
        namespace: str,
        key: str,
        expected_revision: int | None,
        expected_checksum: str | None,
        next_snapshot: TransactionalStateSnapshot,
    ) -> TransactionalStateSnapshot:
        try:
            return _cas_transactional_state(
                self._require_connection(),
                namespace=namespace,
                key=key,
                expected_revision=expected_revision,
                expected_checksum=expected_checksum,
                next_snapshot=next_snapshot,
            )
        except (EventStoreError, ValueError, TypeError):
            self._rollback_only = True
            raise
        except sqlite3.Error as exc:
            self._rollback_only = True
            raise _map_sqlite_error(
                exc,
                operation="CAS SQLite transactional state",
            ) from exc

    def commit(self) -> None:
        connection = self._require_connection()
        if self._finished:
            raise RuntimeError("SQLite event unit of work is already finished")
        if self._rollback_only:
            try:
                connection.rollback()
            except sqlite3.Error as exc:
                self._finished = True
                raise _map_sqlite_error(
                    exc,
                    operation="rollback failed SQLite event transaction",
                ) from exc
            self._finished = True
            raise EventStoreError(
                "event unit of work is rollback-only after a failed operation"
            )
        try:
            connection.commit()
        except sqlite3.Error as exc:
            try:
                connection.rollback()
            finally:
                self._finished = True
            raise _map_sqlite_error(exc, operation="commit SQLite event transaction") from exc
        self._finished = True

    def rollback(self) -> None:
        connection = self._require_connection()
        if not self._finished:
            try:
                connection.rollback()
            except sqlite3.Error as exc:
                self._finished = True
                raise _map_sqlite_error(exc, operation="rollback SQLite event transaction") from exc
            self._finished = True

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        connection = self._connection
        if connection is not None:
            try:
                if not self._finished:
                    connection.rollback()
            finally:
                connection.close()
                self._connection = None
                self._finished = True
        return False

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("SQLite event unit of work is not active")
        return self._connection


SqliteEventStore = SQLiteEventStore


def _tenant_scope(tenant_id: str | None) -> str:
    return tenant_id or ""


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _expected_last_sequence(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected_last_sequence must be an integer or None")
    if value < 0:
        raise ValueError("expected_last_sequence must not be negative")
    return value


def _load_transactional_state(
    connection: sqlite3.Connection,
    namespace: str,
    key: str,
) -> TransactionalStateSnapshot | None:
    normalized_namespace = _required_text(namespace, "namespace")
    normalized_key = _required_text(key, "key")
    row = connection.execute(
        "SELECT namespace, state_key, revision, checksum, payload_json "
        "FROM event_transactional_states WHERE namespace = ? AND state_key = ?",
        (normalized_namespace, normalized_key),
    ).fetchone()
    if row is None:
        return None
    try:
        return TransactionalStateSnapshot.from_dict(
            {
                "namespace": row["namespace"],
                "key": row["state_key"],
                "revision": row["revision"],
                "checksum": row["checksum"],
                "payload": _json_object(
                    row["payload_json"],
                    field_name="transactional_state.payload_json",
                ),
            }
        )
    except EventStoreCorruptionError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "stored transactional state cannot be decoded"
        ) from exc


def _cas_transactional_state(
    connection: sqlite3.Connection,
    *,
    namespace: str,
    key: str,
    expected_revision: int | None,
    expected_checksum: str | None,
    next_snapshot: TransactionalStateSnapshot,
) -> TransactionalStateSnapshot:
    normalized_namespace = _required_text(namespace, "namespace")
    normalized_key = _required_text(key, "key")
    expected_revision, expected_checksum = _transactional_state_expectation(
        expected_revision,
        expected_checksum,
    )
    if not isinstance(next_snapshot, TransactionalStateSnapshot):
        raise TypeError("next_snapshot must be TransactionalStateSnapshot")
    if (next_snapshot.namespace, next_snapshot.key) != (
        normalized_namespace,
        normalized_key,
    ):
        raise ValueError("next_snapshot namespace/key must match the CAS target")

    current = _load_transactional_state(
        connection,
        normalized_namespace,
        normalized_key,
    )
    if current == next_snapshot:
        return current
    if current is None:
        if expected_revision is not None or next_snapshot.revision != 1:
            raise EventStoreContentionError(
                "transactional state creation requires empty expected state "
                "and revision 1"
            )
        connection.execute(
            "INSERT INTO event_transactional_states "
            "(namespace, state_key, revision, checksum, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                next_snapshot.namespace,
                next_snapshot.key,
                next_snapshot.revision,
                next_snapshot.checksum,
                stable_json_dumps(thaw_canonical_json(next_snapshot.payload)),
            ),
        )
        return next_snapshot

    if expected_revision is None:
        raise EventStoreContentionError(
            "transactional state already exists; revision/checksum are required"
        )
    if (
        current.revision != expected_revision
        or current.checksum != expected_checksum
    ):
        raise EventStoreContentionError(
            "transactional state revision/checksum conflict"
        )
    if next_snapshot.revision != current.revision + 1:
        raise ValueError(
            "next_snapshot revision must increment current revision by one"
        )
    updated = connection.execute(
        "UPDATE event_transactional_states "
        "SET revision = ?, checksum = ?, payload_json = ? "
        "WHERE namespace = ? AND state_key = ? AND revision = ? AND checksum = ?",
        (
            next_snapshot.revision,
            next_snapshot.checksum,
            stable_json_dumps(thaw_canonical_json(next_snapshot.payload)),
            normalized_namespace,
            normalized_key,
            current.revision,
            current.checksum,
        ),
    )
    if updated.rowcount != 1:
        raise EventStoreContentionError(
            "transactional state changed during compare-and-swap"
        )
    return next_snapshot


def _transactional_state_expectation(
    revision: int | None,
    checksum: str | None,
) -> tuple[int | None, str | None]:
    if (revision is None) != (checksum is None):
        raise ValueError(
            "expected_revision and expected_checksum must both be set or both "
            "be None"
        )
    if revision is None:
        return None, None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("expected_revision must be a positive integer or None")
    if (
        not isinstance(checksum, str)
        or not checksum.startswith("sha256:")
        or len(checksum) != 71
        or any(character not in "0123456789abcdef" for character in checksum[7:])
    ):
        raise ValueError(
            "expected_checksum must be sha256:<64 lowercase hex> or None"
        )
    return revision, checksum


def _effect_scope(consumer_effect_id: str | None) -> str:
    return consumer_effect_id or ""


def _time_text(value: datetime) -> str:
    text = format_datetime(value)
    if text is None:
        raise ValueError("datetime is required")
    return text


def _optional_time_text(value: datetime | None) -> str | None:
    return None if value is None else _time_text(value)


def _time_value(value: Any) -> datetime:
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise EventStoreCorruptionError("stored datetime is missing")
    return parsed


def _epoch_microseconds(value: Any) -> int | None:
    if value is None:
        return None
    parsed = parse_datetime(str(value))
    if parsed is None:
        raise ValueError("stored datetime is missing or invalid")
    delta = parsed - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _optional_time_value(value: Any) -> datetime | None:
    return None if value is None else _time_value(value)


def _json(value: Any) -> str:
    return stable_json_dumps(value)


def _json_object(value: Any, *, field_name: str) -> Mapping[str, Any]:
    try:
        decoded = json_loads(str(value))
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(f"stored {field_name} is invalid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise EventStoreCorruptionError(f"stored {field_name} must be a JSON object")
    return decoded


def _json_list(value: Any, *, field_name: str) -> list[Any]:
    try:
        decoded = json_loads(str(value))
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(f"stored {field_name} is invalid JSON") from exc
    if not isinstance(decoded, list):
        raise EventStoreCorruptionError(f"stored {field_name} must be a JSON array")
    return decoded


def _map_sqlite_error(exc: sqlite3.Error, *, operation: str) -> EventStoreError:
    message = str(exc).lower()
    code = getattr(exc, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    if primary_code == sqlite3.SQLITE_FULL or "database or disk is full" in message:
        return EventStoreCapacityError(f"{operation}: durable capacity is exhausted")
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "database corruption",
        )
    ):
        return EventStoreCorruptionError(f"{operation}: SQLite store is corrupt")
    if primary_code in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_AUTH,
    } or any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "readonly database",
            "read-only database",
            "unable to open database file",
            "disk i/o error",
            "permission denied",
        )
    ):
        return EventStoreUnavailableError(f"{operation}: durable store is unavailable")
    return EventStoreCorruptionError(f"{operation}: SQLite operation failed")


def _stream_watermark(
    connection: sqlite3.Connection,
    *,
    tenant_scope: str,
    stream_id: str,
) -> int:
    row = connection.execute(
        "SELECT last_sequence FROM event_stream_sequences WHERE tenant_scope = ? AND stream_id = ?",
        (tenant_scope, stream_id),
    ).fetchone()
    return 0 if row is None else int(row[0])


def _stored_event_from_row(row: sqlite3.Row) -> StoredEvent:
    try:
        event = StoredEvent.from_dict(
            _json_object(row["event_json"], field_name="event_json"),
            verify_checksum=True,
        )
    except EventContractError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("stored event cannot be decoded") from exc
    indexed = (
        str(row["event_id"]),
        row["tenant_id"],
        str(row["tenant_scope"]),
        str(row["stream_id"]),
        int(row["stream_sequence"]),
        str(row["event_type"]),
        str(row["data_schema"]),
        str(row["content_checksum"]),
        str(row["record_checksum"]),
    )
    expected = (
        event.event_id,
        event.tenant_id,
        _tenant_scope(event.tenant_id),
        event.stream_id,
        event.stream_sequence,
        event.event_type,
        event.data_schema,
        event.content_checksum,
        event.record_checksum,
    )
    if indexed != expected:
        raise EventStoreCorruptionError("stored event indexes disagree with canonical event JSON")
    return event


def _subscription_definition(subscription: DurableSubscription) -> tuple[Any, ...]:
    effect = subscription.effect
    retry = subscription.retry_policy
    limits = subscription.limits
    return (
        subscription.subscription_id,
        subscription.subscription_version,
        subscription.consumer_id,
        tuple(sorted(subscription.event_filter.event_types)),
        tuple(sorted(subscription.event_filter.data_schemas)),
        subscription.start.policy.value,
        subscription.start.start_sequence,
        effect.performs_external_effects,
        effect.consumer_effect_id,
        effect.idempotency_strategy.value if effect.idempotency_strategy else None,
        retry.max_attempts,
        retry.initial_delay_seconds,
        retry.multiplier,
        retry.max_delay_seconds,
        retry.jitter_ratio,
        subscription.lease_policy.duration_seconds,
        limits.batch_size,
        limits.max_in_flight,
        limits.max_concurrency,
        limits.pending_warning_threshold,
        limits.pending_hard_limit,
        subscription.supports_out_of_order_repair,
        subscription.tenant_id,
    )


def _subscription_from_row(row: sqlite3.Row) -> DurableSubscription:
    payload = _json_object(row["subscription_json"], field_name="subscription_json")
    try:
        effect_payload = dict(payload.get("effect") or {})
        start_payload = dict(payload.get("start") or {})
        filter_payload = dict(payload.get("event_filter") or {})
        retry_payload = dict(payload.get("retry_policy") or {})
        lease_payload = dict(payload.get("lease_policy") or {})
        limits_payload = dict(payload.get("limits") or {})
        subscription = DurableSubscription(
            subscription_id=str(payload["subscription_id"]),
            subscription_version=int(payload["subscription_version"]),
            consumer_id=str(payload["consumer_id"]),
            event_filter=SubscriptionFilter(
                event_types=frozenset(filter_payload.get("event_types") or ()),
                data_schemas=frozenset(filter_payload.get("data_schemas") or ()),
            ),
            start=SubscriptionStart(
                policy=SubscriptionStartPolicy(start_payload.get("policy", "earliest")),
                start_sequence=start_payload.get("start_sequence"),
            ),
            effect=ConsumerEffectContract(
                performs_external_effects=bool(effect_payload.get("performs_external_effects", False)),
                consumer_effect_id=effect_payload.get("consumer_effect_id"),
                idempotency_strategy=(
                    EffectIdempotencyStrategy(effect_payload["idempotency_strategy"])
                    if effect_payload.get("idempotency_strategy") is not None
                    else None
                ),
            ),
            retry_policy=RetryPolicy(**retry_payload),
            lease_policy=LeasePolicy(**lease_payload),
            limits=DeliveryLimits(**limits_payload),
            status=SubscriptionStatus(payload.get("status", "active")),
            supports_out_of_order_repair=bool(payload.get("supports_out_of_order_repair", False)),
            tenant_id=payload.get("tenant_id"),
            created_at=_optional_time_value(payload.get("created_at")),
            updated_at=_optional_time_value(payload.get("updated_at")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("stored subscription cannot be decoded") from exc
    expected = (
        subscription.subscription_id,
        subscription.subscription_version,
        _tenant_scope(subscription.tenant_id),
        subscription.consumer_id,
        _effect_scope(subscription.effect.consumer_effect_id),
        subscription.status.value,
        subscription.limits.pending_hard_limit,
    )
    actual = (
        str(row["subscription_id"]),
        int(row["subscription_version"]),
        str(row["tenant_scope"]),
        str(row["consumer_id"]),
        str(row["consumer_effect_scope"]),
        str(row["status"]),
        int(row["pending_hard_limit"]),
    )
    if expected != actual:
        raise EventStoreCorruptionError(
            "stored subscription indexes disagree with subscription JSON"
        )
    return subscription


def _select_subscription(
    connection: sqlite3.Connection,
    key: SubscriptionKey,
) -> DurableSubscription | None:
    row = connection.execute(
        "SELECT * FROM event_subscriptions WHERE subscription_id = ? AND subscription_version = ?",
        (key.subscription_id, key.subscription_version),
    ).fetchone()
    return None if row is None else _subscription_from_row(row)


def _insert_subscription(
    connection: sqlite3.Connection,
    subscription: DurableSubscription,
) -> None:
    if subscription.created_at is None or subscription.updated_at is None:
        raise ValueError("persisted subscription requires timestamps")
    connection.execute(
        "INSERT INTO event_subscriptions ("
        "subscription_id, subscription_version, tenant_scope, tenant_id, consumer_id, "
        "consumer_effect_scope, consumer_effect_id, status, pending_hard_limit, "
        "subscription_json, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            subscription.subscription_id,
            subscription.subscription_version,
            _tenant_scope(subscription.tenant_id),
            subscription.tenant_id,
            subscription.consumer_id,
            _effect_scope(subscription.effect.consumer_effect_id),
            subscription.effect.consumer_effect_id,
            subscription.status.value,
            subscription.limits.pending_hard_limit,
            _json(subscription),
            _time_text(subscription.created_at),
            _time_text(subscription.updated_at),
        ),
    )


def _subscription_start_sequence(start: SubscriptionStart, watermark: int) -> int:
    if start.policy is SubscriptionStartPolicy.EARLIEST:
        return 1
    if start.policy is SubscriptionStartPolicy.LATEST:
        return watermark + 1
    if start.start_sequence is None:
        raise ValueError("AT_SEQUENCE requires start_sequence")
    return start.start_sequence


def _insert_subscription_stream_state(
    connection: sqlite3.Connection,
    subscription: DurableSubscription,
    *,
    stream_id: str,
    start_sequence: int,
    registration_watermark: int,
    created_at: datetime,
) -> None:
    timestamp = _time_text(created_at)
    connection.execute(
        "INSERT INTO event_subscription_stream_states ("
        "tenant_scope, tenant_id, subscription_id, subscription_version, stream_id, "
        "start_sequence, registration_watermark, retirement_watermark, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (
            _tenant_scope(subscription.tenant_id),
            subscription.tenant_id,
            subscription.subscription_id,
            subscription.subscription_version,
            stream_id,
            start_sequence,
            registration_watermark,
            timestamp,
            timestamp,
        ),
    )


def _subscription_state_from_row(row: sqlite3.Row) -> SubscriptionStreamState:
    return SubscriptionStreamState(
        subscription_id=str(row["subscription_id"]),
        subscription_version=int(row["subscription_version"]),
        stream_id=str(row["stream_id"]),
        start_sequence=int(row["start_sequence"]),
        registration_watermark=int(row["registration_watermark"]),
        tenant_id=row["tenant_id"],
        retirement_watermark=(
            None if row["retirement_watermark"] is None else int(row["retirement_watermark"])
        ),
        created_at=_time_value(row["created_at"]),
        updated_at=_time_value(row["updated_at"]),
    )


def _event_matches_subscription_row(
    event_row: sqlite3.Row,
    subscription: DurableSubscription,
) -> bool:
    event_types = subscription.event_filter.event_types
    data_schemas = subscription.event_filter.data_schemas
    return (
        (not event_types or str(event_row["event_type"]) in event_types)
        and (not data_schemas or str(event_row["data_schema"]) in data_schemas)
        and str(event_row["tenant_scope"]) == _tenant_scope(subscription.tenant_id)
    )


def _delivery_id(
    event_id: str,
    subscription_id: str,
    subscription_version: int,
    generation: int,
) -> str:
    return delivery_id_for(event_id, subscription_id, subscription_version, generation)


def _dead_letter_id(delivery_id: str, delivery_generation: int = 1) -> str:
    return dead_letter_id_for(delivery_id, delivery_generation)


def _insert_delivery(
    connection: sqlite3.Connection,
    subscription: DurableSubscription,
    event_row: sqlite3.Row,
    *,
    created_at: datetime,
    delivery_generation: int = 1,
) -> int:
    delivery_id = _delivery_id(
        str(event_row["event_id"]),
        subscription.subscription_id,
        subscription.subscription_version,
        delivery_generation,
    )
    timestamp = _time_text(created_at)
    cursor = connection.execute(
        "INSERT OR IGNORE INTO event_deliveries ("
        "delivery_id, event_id, tenant_scope, tenant_id, stream_id, stream_sequence, "
        "subscription_id, subscription_version, consumer_id, consumer_effect_scope, "
        "consumer_effect_id, delivery_generation, state, attempt_count, available_at, "
        "lease_owner, lease_generation, lease_expires_at, first_failure_at, last_failure_at, "
        "reason_class, redacted_diagnostic, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, NULL, NULL, NULL, "
        "NULL, NULL, NULL, NULL, ?, ?)",
        (
            delivery_id,
            str(event_row["event_id"]),
            str(event_row["tenant_scope"]),
            event_row["tenant_id"],
            str(event_row["stream_id"]),
            int(event_row["stream_sequence"]),
            subscription.subscription_id,
            subscription.subscription_version,
            subscription.consumer_id,
            _effect_scope(subscription.effect.consumer_effect_id),
            subscription.effect.consumer_effect_id,
            delivery_generation,
            timestamp,
            timestamp,
            timestamp,
        ),
    )
    return 1 if cursor.rowcount == 1 else 0


def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
    try:
        delivery = DeliveryRecord(
            delivery_id=str(row["delivery_id"]),
            event_id=str(row["event_id"]),
            stream_id=str(row["stream_id"]),
            stream_sequence=int(row["stream_sequence"]),
            subscription_id=str(row["subscription_id"]),
            subscription_version=int(row["subscription_version"]),
            consumer_id=str(row["consumer_id"]),
            consumer_effect_id=row["consumer_effect_id"],
            tenant_id=row["tenant_id"],
            delivery_generation=int(row["delivery_generation"]),
            state=DeliveryState(str(row["state"])),
            attempt_count=int(row["attempt_count"]),
            available_at=_optional_time_value(row["available_at"]),
            lease_owner=row["lease_owner"],
            lease_generation=(
                None if row["lease_generation"] is None else int(row["lease_generation"])
            ),
            lease_expires_at=_optional_time_value(row["lease_expires_at"]),
            first_failure_at=_optional_time_value(row["first_failure_at"]),
            last_failure_at=_optional_time_value(row["last_failure_at"]),
            reason_class=row["reason_class"],
            redacted_diagnostic=row["redacted_diagnostic"],
            created_at=_time_value(row["created_at"]),
            updated_at=_time_value(row["updated_at"]),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("stored delivery cannot be decoded") from exc
    if str(row["tenant_scope"]) != _tenant_scope(delivery.tenant_id):
        raise EventStoreCorruptionError("delivery tenant scope is corrupt")
    if str(row["consumer_effect_scope"]) != _effect_scope(delivery.consumer_effect_id):
        raise EventStoreCorruptionError("delivery effect scope is corrupt")
    return delivery


def _lease_matches(
    row: sqlite3.Row,
    lease: DeliveryLeaseToken,
    *,
    at: datetime,
) -> bool:
    return (
        str(row["delivery_id"]) == lease.delivery_id
        and int(row["delivery_generation"]) == lease.delivery_generation
        and str(row["state"]) == DeliveryState.CLAIMED.value
        and row["lease_owner"] == lease.lease_owner
        and int(row["lease_generation"] or 0) == lease.lease_generation
        and row["lease_expires_at"] == _time_text(lease.lease_expires_at)
        and _time_value(row["lease_expires_at"]) > at
    )


def _subscription_pending_capacity_exhausted(
    connection: sqlite3.Connection,
    subscription: DurableSubscription,
) -> bool:
    row = connection.execute(
        "SELECT 1 FROM event_deliveries WHERE tenant_scope = ? "
        "AND subscription_id = ? AND subscription_version = ? "
        "AND state IN ('pending', 'claimed', 'retry_wait') LIMIT 1 OFFSET ?",
        (
            _tenant_scope(subscription.tenant_id),
            subscription.subscription_id,
            subscription.subscription_version,
            subscription.limits.pending_hard_limit - 1,
        ),
    ).fetchone()
    return row is not None


def _recover_expired_sqlite_claims(
    connection: sqlite3.Connection,
    subscription: DurableSubscription,
    *,
    requested_at: datetime,
) -> None:
    expired = connection.execute(
        "SELECT * FROM event_deliveries WHERE subscription_id = ? "
        "AND subscription_version = ? AND tenant_scope = ? AND state = 'claimed' "
        "AND newsroom_epoch_us(lease_expires_at) <= newsroom_epoch_us(?) "
        "ORDER BY stream_id, stream_sequence, delivery_generation",
        (
            subscription.subscription_id,
            subscription.subscription_version,
            _tenant_scope(subscription.tenant_id),
            _time_text(requested_at),
        ),
    ).fetchall()
    for row in expired:
        delivery = _delivery_from_row(row)
        if delivery.attempt_count < subscription.retry_policy.max_attempts:
            connection.execute(
                "UPDATE event_deliveries SET state = 'retry_wait', available_at = ?, "
                "lease_owner = NULL, lease_generation = NULL, lease_expires_at = NULL, "
                "first_failure_at = COALESCE(first_failure_at, ?), last_failure_at = ?, "
                "reason_class = 'lease_expired', "
                "redacted_diagnostic = 'delivery lease expired before settlement', "
                "updated_at = ? WHERE delivery_id = ? AND state = 'claimed'",
                (
                    _time_text(requested_at),
                    _time_text(requested_at),
                    _time_text(requested_at),
                    _time_text(requested_at),
                    delivery.delivery_id,
                ),
            )
            continue

        settled_row = connection.execute(
            "UPDATE event_deliveries SET state = 'dead_letter', available_at = NULL, "
            "lease_owner = NULL, lease_generation = NULL, lease_expires_at = NULL, "
            "first_failure_at = COALESCE(first_failure_at, ?), last_failure_at = ?, "
            "reason_class = 'lease_expired', "
            "redacted_diagnostic = 'delivery lease expired after retry budget exhaustion', "
            "updated_at = ? WHERE delivery_id = ? AND state = 'claimed' RETURNING *",
            (
                _time_text(requested_at),
                _time_text(requested_at),
                _time_text(requested_at),
                delivery.delivery_id,
            ),
        ).fetchone()
        if settled_row is None:
            raise EventStoreCorruptionError(
                "expired delivery disappeared during terminal recovery"
            )
        settled = _delivery_from_row(settled_row)
        if settled.first_failure_at is None or settled.last_failure_at is None:
            raise EventStoreCorruptionError(
                "terminal lease recovery is missing failure timestamps"
            )
        connection.execute(
            "INSERT INTO event_dead_letters ("
            "dead_letter_id, delivery_id, event_id, tenant_scope, tenant_id, stream_id, "
            "stream_sequence, subscription_id, subscription_version, consumer_id, "
            "consumer_effect_scope, consumer_effect_id, delivery_generation, attempt_count, "
            "first_failure_at, last_failure_at, reason_class, redacted_diagnostic, disposition, "
            "operator_id, operator_reason, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, NULL)",
            (
                _dead_letter_id(
                    settled.delivery_id,
                    settled.delivery_generation,
                ),
                settled.delivery_id,
                settled.event_id,
                _tenant_scope(settled.tenant_id),
                settled.tenant_id,
                settled.stream_id,
                settled.stream_sequence,
                settled.subscription_id,
                settled.subscription_version,
                settled.consumer_id,
                _effect_scope(settled.consumer_effect_id),
                settled.consumer_effect_id,
                settled.delivery_generation,
                settled.attempt_count,
                _time_text(settled.first_failure_at),
                _time_text(settled.last_failure_at),
                settled.reason_class,
                settled.redacted_diagnostic,
            ),
        )
        if settled.delivery_generation == 1:
            _advance_checkpoint(
                connection,
                subscription,
                settled.stream_id,
                settled_at=requested_at,
            )


def _record_inbox(
    connection: sqlite3.Connection,
    delivery: DeliveryRecord,
    entry: InboxEntry,
) -> bool:
    if (
        entry.event_id != delivery.event_id
        or entry.consumer_effect_id != delivery.consumer_effect_id
        or (entry.delivery_id is not None and entry.delivery_id != delivery.delivery_id)
    ):
        raise EventConsumerIdempotencyError(
            "inbox entry does not match the claimed delivery effect identity"
        )
    existing = connection.execute(
        "SELECT * FROM event_inbox WHERE event_id = ? AND consumer_effect_id = ?",
        (entry.event_id, entry.consumer_effect_id),
    ).fetchone()
    if existing is not None:
        current = _inbox_from_row(existing)
        if current.delivery_id is None:
            raise EventStoreCorruptionError(
                "stored inbox entry is missing its completing delivery"
            )
        if current.result_checksum != entry.result_checksum:
            raise EventConsumerIdempotencyError(
                "inbox identity was already completed with a different result"
            )
        return False
    connection.execute(
        "INSERT INTO event_inbox ("
        "event_id, consumer_effect_id, tenant_scope, tenant_id, completed_at, delivery_id, result_checksum"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            entry.event_id,
            entry.consumer_effect_id,
            _tenant_scope(delivery.tenant_id),
            delivery.tenant_id,
            _time_text(entry.completed_at),
            entry.delivery_id or delivery.delivery_id,
            entry.result_checksum,
        ),
    )
    return True


def _inbox_from_row(row: sqlite3.Row) -> InboxEntry:
    return InboxEntry(
        event_id=str(row["event_id"]),
        consumer_effect_id=str(row["consumer_effect_id"]),
        completed_at=_time_value(row["completed_at"]),
        delivery_id=row["delivery_id"],
        result_checksum=row["result_checksum"],
    )


def _checkpoint_projection(
    *,
    tenant_id: str | None,
    subscription_id: str,
    subscription_version: int,
    stream_id: str,
    sequence: int,
    last_event_id: str,
    terminal_disposition: DeliveryState,
    updated_at: datetime,
    checkpoint_version: int = 1,
) -> Mapping[str, Any]:
    return {
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        "subscription_version": subscription_version,
        "stream_id": stream_id,
        "highest_contiguous_terminal_sequence": sequence,
        "last_event_id": last_event_id,
        "terminal_disposition": terminal_disposition.value,
        "updated_at": _time_text(updated_at),
        "checkpoint_version": checkpoint_version,
    }


def _advance_checkpoint(
    connection: sqlite3.Connection,
    subscription: DurableSubscription,
    stream_id: str,
    *,
    settled_at: datetime,
) -> ConsumerCheckpoint | None:
    tenant_scope = _tenant_scope(subscription.tenant_id)
    existing = connection.execute(
        "SELECT * FROM event_consumer_checkpoints WHERE tenant_scope = ? "
        "AND subscription_id = ? AND subscription_version = ? AND stream_id = ?",
        (
            tenant_scope,
            subscription.subscription_id,
            subscription.subscription_version,
            stream_id,
        ),
    ).fetchone()
    frontier = int(existing["highest_contiguous_terminal_sequence"]) if existing else 0
    rows = connection.execute(
        "SELECT event_id, stream_sequence, state FROM event_deliveries WHERE tenant_scope = ? "
        "AND subscription_id = ? AND subscription_version = ? AND stream_id = ? "
        "AND delivery_generation = 1 AND stream_sequence > ? ORDER BY stream_sequence",
        (
            tenant_scope,
            subscription.subscription_id,
            subscription.subscription_version,
            stream_id,
            frontier,
        ),
    ).fetchall()
    last_row: sqlite3.Row | None = None
    for row in rows:
        if str(row["state"]) not in _TERMINAL_STATES:
            break
        frontier = int(row["stream_sequence"])
        last_row = row
    if last_row is None:
        return None if existing is None else _checkpoint_from_row(existing)
    terminal_disposition = DeliveryState(str(last_row["state"]))
    projection = _checkpoint_projection(
        tenant_id=subscription.tenant_id,
        subscription_id=subscription.subscription_id,
        subscription_version=subscription.subscription_version,
        stream_id=stream_id,
        sequence=frontier,
        last_event_id=str(last_row["event_id"]),
        terminal_disposition=terminal_disposition,
        updated_at=settled_at,
    )
    checksum = checksum_for(projection)
    connection.execute(
        "INSERT INTO event_consumer_checkpoints ("
        "tenant_scope, tenant_id, subscription_id, subscription_version, stream_id, "
        "highest_contiguous_terminal_sequence, last_event_id, terminal_disposition, "
        "updated_at, checksum, checkpoint_version"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1) "
        "ON CONFLICT (tenant_scope, subscription_id, subscription_version, stream_id) "
        "DO UPDATE SET highest_contiguous_terminal_sequence = excluded.highest_contiguous_terminal_sequence, "
        "last_event_id = excluded.last_event_id, terminal_disposition = excluded.terminal_disposition, "
        "updated_at = excluded.updated_at, checksum = excluded.checksum",
        (
            tenant_scope,
            subscription.tenant_id,
            subscription.subscription_id,
            subscription.subscription_version,
            stream_id,
            frontier,
            str(last_row["event_id"]),
            terminal_disposition.value,
            _time_text(settled_at),
            checksum,
        ),
    )
    row = connection.execute(
        "SELECT * FROM event_consumer_checkpoints WHERE tenant_scope = ? "
        "AND subscription_id = ? AND subscription_version = ? AND stream_id = ?",
        (
            tenant_scope,
            subscription.subscription_id,
            subscription.subscription_version,
            stream_id,
        ),
    ).fetchone()
    return _checkpoint_from_row(row)


def _checkpoint_from_row(row: sqlite3.Row) -> ConsumerCheckpoint:
    checkpoint = ConsumerCheckpoint(
        subscription_id=str(row["subscription_id"]),
        subscription_version=int(row["subscription_version"]),
        stream_id=str(row["stream_id"]),
        highest_contiguous_terminal_sequence=int(
            row["highest_contiguous_terminal_sequence"]
        ),
        last_event_id=str(row["last_event_id"]),
        terminal_disposition=DeliveryState(str(row["terminal_disposition"])),
        updated_at=_time_value(row["updated_at"]),
        checksum=str(row["checksum"]),
        checkpoint_version=int(row["checkpoint_version"]),
        tenant_id=row["tenant_id"],
    )
    if (
        checkpoint.highest_contiguous_terminal_sequence is None
        or checkpoint.last_event_id is None
        or checkpoint.terminal_disposition is None
    ):
        raise EventStoreCorruptionError("persisted checkpoint frontier is incomplete")
    expected = checksum_for(
        _checkpoint_projection(
            tenant_id=checkpoint.tenant_id,
            subscription_id=checkpoint.subscription_id,
            subscription_version=checkpoint.subscription_version,
            stream_id=checkpoint.stream_id,
            sequence=checkpoint.highest_contiguous_terminal_sequence,
            last_event_id=checkpoint.last_event_id,
            terminal_disposition=checkpoint.terminal_disposition,
            updated_at=checkpoint.updated_at,
            checkpoint_version=checkpoint.checkpoint_version,
        )
    )
    if expected != checkpoint.checksum:
        raise EventStoreCorruptionError("consumer checkpoint checksum does not match")
    return checkpoint


def _dead_letter_from_row(row: sqlite3.Row) -> DeadLetterRecord:
    try:
        return DeadLetterRecord(
            dead_letter_id=str(row["dead_letter_id"]),
            delivery_id=str(row["delivery_id"]),
            event_id=str(row["event_id"]),
            stream_id=str(row["stream_id"]),
            stream_sequence=int(row["stream_sequence"]),
            subscription_id=str(row["subscription_id"]),
            subscription_version=int(row["subscription_version"]),
            consumer_id=str(row["consumer_id"]),
            consumer_effect_id=row["consumer_effect_id"],
            delivery_generation=int(row["delivery_generation"]),
            attempt_count=int(row["attempt_count"]),
            first_failure_at=_time_value(row["first_failure_at"]),
            last_failure_at=_time_value(row["last_failure_at"]),
            reason_class=str(row["reason_class"]),
            redacted_diagnostic=row["redacted_diagnostic"],
            tenant_id=row["tenant_id"],
            disposition=DeadLetterDisposition(str(row["disposition"])),
            operator_id=row["operator_id"],
            operator_reason=row["operator_reason"],
            updated_at=_optional_time_value(row["updated_at"]),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("stored dead letter cannot be decoded") from exc


def _quarantine_from_row(row: sqlite3.Row) -> QuarantineRecord:
    try:
        return QuarantineRecord(
            quarantine_id=str(row["quarantine_id"]),
            source=str(row["source"]),
            reason=QuarantineReason(str(row["reason"])),
            created_at=_time_value(row["created_at"]),
            envelope_schema=row["envelope_schema"],
            event_type=row["event_type"],
            data_schema=row["data_schema"],
            tenant_id=row["tenant_id"],
            redacted_diagnostic=row["redacted_diagnostic"],
            disposition=QuarantineDisposition(str(row["disposition"])),
            operator_id=row["operator_id"],
            operator_reason=row["operator_reason"],
            updated_at=_optional_time_value(row["updated_at"]),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("stored quarantine record cannot be decoded") from exc


def _select_retirement_cancellation_report(
    connection: sqlite3.Connection,
    cancellation_id: str,
    *,
    tenant_scope: str,
) -> RetirementCancellationReport | None:
    row = connection.execute(
        "SELECT * FROM event_retirement_cancellation_reports "
        "WHERE tenant_scope = ? AND cancellation_id = ?",
        (tenant_scope, cancellation_id),
    ).fetchone()
    if row is None:
        return None
    item_limit = int(row["item_limit"])
    if not 1 <= item_limit <= MAX_RETIREMENT_CANCELLATION_ITEMS:
        raise EventStoreCorruptionError(
            "stored retirement cancellation item limit is invalid"
        )
    cancelled_count = int(row["cancelled_count"])
    if not 0 <= cancelled_count <= item_limit:
        raise EventStoreCorruptionError(
            "stored retirement cancellation item count is invalid"
        )
    item_rows = connection.execute(
        "SELECT item.*, delivery.event_id AS linked_event_id, "
        "delivery.stream_id AS linked_stream_id, "
        "delivery.stream_sequence AS linked_stream_sequence, "
        "delivery.subscription_id AS linked_subscription_id, "
        "delivery.subscription_version AS linked_subscription_version, "
        "delivery.delivery_generation AS linked_delivery_generation, "
        "delivery.state AS linked_state, "
        "delivery.attempt_count AS linked_attempt_count, "
        "delivery.tenant_scope AS linked_tenant_scope, "
        "delivery.reason_class AS linked_reason_class, "
        "delivery.lease_owner AS linked_lease_owner, "
        "delivery.lease_generation AS linked_lease_generation, "
        "delivery.lease_expires_at AS linked_lease_expires_at, "
        "delivery.updated_at AS linked_updated_at "
        "FROM event_retirement_cancellation_items AS item "
        "JOIN event_deliveries AS delivery ON delivery.delivery_id = item.delivery_id "
        "WHERE item.tenant_scope = ? AND item.cancellation_id = ? "
        "ORDER BY item.stream_id COLLATE BINARY, item.stream_sequence, "
        "item.delivery_generation, item.delivery_id COLLATE BINARY LIMIT ?",
        (tenant_scope, cancellation_id, item_limit + 1),
    ).fetchall()
    if len(item_rows) > item_limit:
        raise EventStoreCorruptionError(
            "stored retirement cancellation report exceeds its item limit"
        )
    if len(item_rows) != cancelled_count:
        raise EventStoreCorruptionError(
            "stored retirement cancellation report is missing audit items"
        )
    try:
        subscription = SubscriptionKey(
            str(row["subscription_id"]),
            int(row["subscription_version"]),
        )
        cancelled_at = _time_value(row["cancelled_at"])
        items: list[RetirementCancellationItem] = []
        for item_row in item_rows:
            item_subscription = SubscriptionKey(
                str(item_row["subscription_id"]),
                int(item_row["subscription_version"]),
            )
            if item_subscription != subscription:
                raise EventStoreCorruptionError(
                    "retirement cancellation item crossed subscription scope"
                )
            indexed = (
                str(item_row["event_id"]),
                str(item_row["stream_id"]),
                int(item_row["stream_sequence"]),
                str(item_row["subscription_id"]),
                int(item_row["subscription_version"]),
                int(item_row["delivery_generation"]),
                str(item_row["terminal_state"]),
                str(item_row["tenant_scope"]),
            )
            linked = (
                str(item_row["linked_event_id"]),
                str(item_row["linked_stream_id"]),
                int(item_row["linked_stream_sequence"]),
                str(item_row["linked_subscription_id"]),
                int(item_row["linked_subscription_version"]),
                int(item_row["linked_delivery_generation"]),
                str(item_row["linked_state"]),
                str(item_row["linked_tenant_scope"]),
            )
            expected_attempt_count = max(
                1,
                int(item_row["previous_attempt_count"]),
            )
            if (
                indexed != linked
                or int(item_row["linked_attempt_count"])
                != expected_attempt_count
                or str(item_row["linked_reason_class"])
                != "subscription_retired"
                or item_row["linked_lease_owner"] is not None
                or item_row["linked_lease_generation"] is not None
                or item_row["linked_lease_expires_at"] is not None
                or _time_value(item_row["linked_updated_at"])
                != _time_value(item_row["cancelled_at"])
            ):
                raise EventStoreCorruptionError(
                    "retirement cancellation item disagrees with its delivery disposition"
                )
            items.append(
                RetirementCancellationItem(
                    cancellation_id=str(item_row["cancellation_id"]),
                    delivery_id=str(item_row["delivery_id"]),
                    event_id=str(item_row["event_id"]),
                    stream_id=str(item_row["stream_id"]),
                    stream_sequence=int(item_row["stream_sequence"]),
                    subscription=subscription,
                    delivery_generation=int(item_row["delivery_generation"]),
                    previous_state=DeliveryState(str(item_row["previous_state"])),
                    previous_attempt_count=int(item_row["previous_attempt_count"]),
                    previous_reason_class=item_row["previous_reason_class"],
                    terminal_state=DeliveryState(str(item_row["terminal_state"])),
                    cancelled_at=_time_value(item_row["cancelled_at"]),
                    tenant_id=item_row["tenant_id"],
                )
            )
        truncated_raw = int(row["remaining_nonterminal_count_truncated"])
        if truncated_raw not in (0, 1):
            raise EventStoreCorruptionError(
                "stored retirement cancellation remaining-count bound is invalid"
            )
        report = RetirementCancellationReport(
            cancellation_id=str(row["cancellation_id"]),
            subscription=subscription,
            requested_at=_time_value(row["requested_at"]),
            cancelled_at=cancelled_at,
            operator_id=str(row["operator_id"]),
            operator_reason=str(row["operator_reason"]),
            authorization_evidence_ref=str(row["authorization_evidence_ref"]),
            item_limit=item_limit,
            remaining_nonterminal_count=int(row["remaining_nonterminal_count"]),
            remaining_nonterminal_count_truncated=bool(truncated_raw),
            items=tuple(items),
            tenant_id=row["tenant_id"],
        )
    except EventStoreCorruptionError:
        raise
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "stored retirement cancellation report cannot be decoded"
        ) from exc
    if _tenant_scope(report.tenant_id) != str(row["tenant_scope"]):
        raise EventStoreCorruptionError(
            "retirement cancellation tenant index is corrupt"
        )
    return report


def _retirement_cancellation_identity(
    value: RetirementCancellationRequest | RetirementCancellationReport,
) -> tuple[Any, ...]:
    if isinstance(value, RetirementCancellationRequest):
        return (
            value.cancellation_id,
            value.subscription,
            value.operator_id,
            value.operator_reason,
            value.tenant_id,
            value.limit,
        )
    return (
        value.cancellation_id,
        value.subscription,
        value.operator_id,
        value.operator_reason,
        value.tenant_id,
        value.item_limit,
    )


def _assert_retirement_cancellation_retry(
    report: RetirementCancellationReport,
    request: RetirementCancellationRequest,
) -> None:
    if _retirement_cancellation_identity(report) != _retirement_cancellation_identity(
        request
    ):
        raise EventRetirementCancellationCollisionError(
            "retirement cancellation id was reused for another operator command"
        )


def _redelivery_report_from_sqlite(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> RedeliveryReport:
    item_rows = connection.execute(
        "SELECT items.*, deliveries.event_id AS linked_event_id, "
        "deliveries.stream_id AS linked_stream_id, "
        "deliveries.stream_sequence AS linked_stream_sequence, "
        "deliveries.subscription_id AS linked_subscription_id, "
        "deliveries.subscription_version AS linked_subscription_version, "
        "deliveries.delivery_generation AS linked_delivery_generation, "
        "deliveries.tenant_scope AS linked_tenant_scope "
        "FROM event_redelivery_items AS items "
        "JOIN event_deliveries AS deliveries ON deliveries.delivery_id = items.delivery_id "
        "WHERE items.tenant_scope = ? AND items.redelivery_id = ? "
        "ORDER BY items.stream_sequence, items.event_id LIMIT ?",
        (
            str(row["tenant_scope"]),
            str(row["redelivery_id"]),
            MAX_REDELIVERY_ITEMS + 1,
        ),
    ).fetchall()
    if len(item_rows) > MAX_REDELIVERY_ITEMS:
        raise EventStoreCorruptionError(
            "stored redelivery report exceeds the bounded item limit"
        )
    try:
        subscription = SubscriptionKey(
            str(row["subscription_id"]),
            int(row["subscription_version"]),
        )
        items: list[RedeliveryItem] = []
        for item_row in item_rows:
            indexed = (
                str(item_row["event_id"]),
                str(item_row["stream_id"]),
                int(item_row["stream_sequence"]),
                str(item_row["subscription_id"]),
                int(item_row["subscription_version"]),
                int(item_row["delivery_generation"]),
                str(item_row["tenant_scope"]),
            )
            linked = (
                str(item_row["linked_event_id"]),
                str(item_row["linked_stream_id"]),
                int(item_row["linked_stream_sequence"]),
                str(item_row["linked_subscription_id"]),
                int(item_row["linked_subscription_version"]),
                int(item_row["linked_delivery_generation"]),
                str(item_row["linked_tenant_scope"]),
            )
            if indexed != linked:
                raise EventStoreCorruptionError(
                    "redelivery item disagrees with its delivery generation"
                )
            items.append(
                RedeliveryItem(
                    redelivery_id=str(item_row["redelivery_id"]),
                    event_id=str(item_row["event_id"]),
                    stream_id=str(item_row["stream_id"]),
                    stream_sequence=int(item_row["stream_sequence"]),
                    subscription=subscription,
                    delivery_id=str(item_row["delivery_id"]),
                    delivery_generation=int(item_row["delivery_generation"]),
                    created_at=_time_value(item_row["created_at"]),
                    tenant_id=item_row["tenant_id"],
                )
            )
        report = RedeliveryReport(
            redelivery_id=str(row["redelivery_id"]),
            subscription=subscription,
            source_stream_id=str(row["source_stream_id"]),
            from_sequence=int(row["from_sequence"]),
            requested_through_sequence=(
                int(row["requested_through_sequence"])
                if row["requested_through_sequence"] is not None
                else None
            ),
            through_sequence=int(row["through_sequence"]),
            captured_high_watermark=int(row["captured_high_watermark"]),
            requested_at=_time_value(row["requested_at"]),
            scheduled_at=_time_value(row["scheduled_at"]),
            operator_id=str(row["operator_id"]),
            operator_reason=str(row["operator_reason"]),
            authorization_evidence_ref=str(row["authorization_evidence_ref"]),
            items=tuple(items),
            tenant_id=row["tenant_id"],
        )
    except EventStoreCorruptionError:
        raise
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "stored redelivery report cannot be decoded"
        ) from exc
    if _tenant_scope(report.tenant_id) != str(row["tenant_scope"]):
        raise EventStoreCorruptionError("redelivery report tenant index is corrupt")
    return report


def _redelivery_request_identity(
    value: RedeliveryRequest | RedeliveryReport,
) -> tuple[Any, ...]:
    if isinstance(value, RedeliveryRequest):
        return (
            value.redelivery_id,
            value.subscription,
            value.source_stream_id,
            value.from_sequence,
            value.through_sequence,
            value.requested_at,
            value.operator_id,
            value.operator_reason,
            value.authorization_evidence_ref,
            value.tenant_id,
        )
    return (
        value.redelivery_id,
        value.subscription,
        value.source_stream_id,
        value.from_sequence,
        value.requested_through_sequence,
        value.requested_at,
        value.operator_id,
        value.operator_reason,
        value.authorization_evidence_ref,
        value.tenant_id,
    )


def _replay_from_row(row: sqlite3.Row) -> ReplayReport:
    payload = _json_object(row["replay_json"], field_name="replay_json")
    try:
        report = ReplayReport(
            replay_id=str(payload["replay_id"]),
            mode=ReplayMode(str(payload["mode"])),
            source_stream_id=str(payload["source_stream_id"]),
            high_watermark=int(payload["high_watermark"]),
            status=ReplayStatus(str(payload["status"])),
            started_at=_time_value(payload["started_at"]),
            from_sequence=payload.get("from_sequence"),
            to_sequence=payload.get("to_sequence"),
            checkpoint_ref=payload.get("checkpoint_ref"),
            versions=tuple(
                ReplayVersion(component=str(item["component"]), version=str(item["version"]))
                for item in payload.get("versions") or ()
            ),
            applied_upcasters=tuple(payload.get("applied_upcasters") or ()),
            quarantine_refs=tuple(payload.get("quarantine_refs") or ()),
            mismatch_sequence=payload.get("mismatch_sequence"),
            reason_class=payload.get("reason_class"),
            result_checksum=payload.get("result_checksum"),
            finished_at=_optional_time_value(payload.get("finished_at")),
            tenant_id=payload.get("tenant_id"),
            operator_id=payload.get("operator_id"),
            operator_reason=payload.get("operator_reason"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("stored replay report cannot be decoded") from exc
    if (
        report.replay_id != str(row["replay_id"])
        or _tenant_scope(report.tenant_id) != str(row["tenant_scope"])
        or report.mode.value != str(row["mode"])
        or report.source_stream_id != str(row["source_stream_id"])
        or report.high_watermark != int(row["high_watermark"])
        or report.status.value != str(row["status"])
        or _time_text(report.started_at) != str(row["started_at"])
    ):
        raise EventStoreCorruptionError("replay report indexes disagree with replay JSON")
    return report


def _insert_replay(connection: sqlite3.Connection, report: ReplayReport) -> None:
    connection.execute(
        "INSERT INTO event_replay_reports ("
        "replay_id, tenant_scope, tenant_id, mode, source_stream_id, high_watermark, "
        "status, started_at, replay_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report.replay_id,
            _tenant_scope(report.tenant_id),
            report.tenant_id,
            report.mode.value,
            report.source_stream_id,
            report.high_watermark,
            report.status.value,
            _time_text(report.started_at),
            _json(report),
        ),
    )


def _validate_replay_update(current: ReplayReport, updated: ReplayReport) -> None:
    immutable = (
        "replay_id",
        "mode",
        "source_stream_id",
        "high_watermark",
        "started_at",
        "from_sequence",
        "checkpoint_ref",
        "tenant_id",
        "operator_id",
        "operator_reason",
    )
    for field_name in immutable:
        if getattr(current, field_name) != getattr(updated, field_name):
            raise EventStoreError(f"replay report cannot change {field_name}")
    transitions = {
        ReplayStatus.PENDING: {
            ReplayStatus.PENDING,
            ReplayStatus.RUNNING,
            ReplayStatus.FAILED,
        },
        ReplayStatus.RUNNING: {
            ReplayStatus.RUNNING,
            ReplayStatus.SUCCEEDED,
            ReplayStatus.FAILED,
        },
        ReplayStatus.SUCCEEDED: {ReplayStatus.SUCCEEDED},
        ReplayStatus.FAILED: {ReplayStatus.FAILED},
    }
    if updated.status not in transitions[current.status]:
        raise EventStoreError(
            f"invalid replay status transition: {current.status.value} -> {updated.status.value}"
        )
    if current.status in {ReplayStatus.SUCCEEDED, ReplayStatus.FAILED} and current != updated:
        raise EventStoreError("terminal replay report is immutable")


def _append_in_filter(
    clauses: list[str],
    params: list[Any],
    column: str,
    values: frozenset[str],
) -> None:
    if not values:
        return
    ordered = sorted(values)
    clauses.append(f"{column} IN ({','.join('?' for _ in ordered)})")
    params.extend(ordered)


def _encode_cursor(kind: str, key: Sequence[Any], filters: Mapping[str, Any]) -> str:
    payload = _json({"v": 1, "kind": kind, "key": list(key), "filters": dict(filters)})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(
    cursor: str | None,
    *,
    kind: str,
    filters: Mapping[str, Any],
) -> list[Any] | None:
    if cursor is None:
        return None
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        ).decode("utf-8")
        payload = json_loads(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid SQLite event-store cursor") from exc
    if (
        not isinstance(payload, Mapping)
        or payload.get("v") != 1
        or payload.get("kind") != kind
        or payload.get("filters") != dict(filters)
        or not isinstance(payload.get("key"), list)
    ):
        raise ValueError("cursor does not match the requested query scope")
    return list(payload["key"])


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_SECONDS",
    "DEFAULT_SYNCHRONOUS",
    "SQLITE_SCHEMA_VERSION",
    "SQLiteEventStore",
    "SQLiteEventUnitOfWork",
    "SqliteEventStore",
]
