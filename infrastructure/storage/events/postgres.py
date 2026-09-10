from __future__ import annotations

import atexit
import base64
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import TracebackType
from typing import Any, Literal, NoReturn
from weakref import finalize

import psycopg
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from framework.events.canonical import (
    EventCandidate,
    StoredEvent,
    checksum_for,
    thaw_canonical_json,
)
from framework.events.errors import (
    EventConsumerIdempotencyError,
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
    DeadLetterAction,
    DeadLetterDisposition,
    DeadLetterPage,
    DeadLetterQuery,
    DeadLetterRecord,
    DeliveryClaimRequest,
    DeliveryLeaseToken,
    DeliveryPage,
    DeliveryQuery,
    DeliveryRecord,
    DeliverySettlement,
    DeliverySettlementResult,
    DeliveryState,
    DurableSubscription,
    EventPage,
    InboxEntry,
    InboxKey,
    LeasePolicy,
    MAX_REDELIVERY_ITEMS,
    MAX_RETIREMENT_CANCELLATION_ITEMS,
    PendingDeliveryStats,
    QuarantineDisposition,
    QuarantinePage,
    QuarantineQuery,
    QuarantineRecord,
    RedeliveryItem,
    RedeliveryReport,
    RedeliveryRequest,
    RetirementCancellationItem,
    RetirementCancellationReport,
    RetirementCancellationRequest,
    ReplayReport,
    ReplayReportPage,
    ReplayReportQuery,
    ReplayStartRequest,
    ReplayStatus,
    ReplayVersion,
    StreamReadRequest,
    StreamSequenceCursor,
    SubscriptionKey,
    SubscriptionPage,
    SubscriptionQuery,
    SubscriptionStartPolicy,
    SubscriptionStatus,
    SubscriptionStreamState,
    SubscriptionStreamStatePage,
    SubscriptionStreamStateQuery,
    TransactionalStateSnapshot,
)
from framework.events.runtime.identity import dead_letter_id_for, delivery_id_for
from infrastructure.storage.postgres.dsn import normalize_dsn


ConnectionFactory = Callable[[], Any]

DEFAULT_POOL_MIN_SIZE = 1
DEFAULT_POOL_MAX_SIZE = 16
DEFAULT_POOL_TIMEOUT_SECONDS = 30.0

PoolKey = tuple[str, int, int, float]
_POOL_REGISTRY_LOCK = Lock()
_POOL_REGISTRY: dict[PoolKey, tuple[ConnectionPool[Any], int]] = {}


_EVENT_COLUMNS = """
    event_id,
    tenant_id,
    stream_id,
    stream_sequence,
    envelope_schema,
    event_type,
    data_schema,
    source,
    subject,
    occurred_at,
    observed_at,
    correlation_id,
    causation_id,
    business_context,
    producer,
    trace_context,
    security_classification,
    content_type,
    payload,
    payload_ref,
    extensions,
    content_checksum,
    record_checksum
"""


POSTGRES_TRANSACTIONAL_STATE_DDL = """
CREATE TABLE IF NOT EXISTS event_transactional_states (
    namespace TEXT NOT NULL,
    state_key TEXT NOT NULL,
    revision BIGINT NOT NULL,
    checksum TEXT NOT NULL,
    payload JSONB NOT NULL,
    PRIMARY KEY (namespace, state_key),
    CONSTRAINT ck_event_transactional_states_namespace
        CHECK (btrim(namespace) <> ''),
    CONSTRAINT ck_event_transactional_states_key
        CHECK (btrim(state_key) <> ''),
    CONSTRAINT ck_event_transactional_states_revision
        CHECK (revision >= 1),
    CONSTRAINT ck_event_transactional_states_checksum
        CHECK (checksum ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_event_transactional_states_payload
        CHECK (jsonb_typeof(payload) = 'object')
);
"""


class PostgresDurableEventStore:
    """Canonical PostgreSQL event store backed by event storage migrations.

    The adapter deliberately accepts only an already validated, security-
    projected :class:`EventCandidate`.  Schema validation and security
    projection belong to the application/runtime boundary and are never
    duplicated with a weaker database-specific policy here.
    """

    def __init__(
        self,
        dsn: str,
        *,
        connection_factory: ConnectionFactory | None = None,
        pool_min_size: int = DEFAULT_POOL_MIN_SIZE,
        pool_max_size: int = DEFAULT_POOL_MAX_SIZE,
        pool_timeout_seconds: float = DEFAULT_POOL_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = normalize_dsn(dsn.strip())
        self._pool: ConnectionPool[Any] | None = None
        self._pool_finalizer: Any | None = None
        if connection_factory is not None:
            self._connection_factory = connection_factory
        else:
            if isinstance(pool_min_size, bool) or pool_min_size < 0:
                raise ValueError("pool_min_size must be greater than or equal to zero")
            if isinstance(pool_max_size, bool) or pool_max_size < 1:
                raise ValueError("pool_max_size must be greater than zero")
            if pool_min_size > pool_max_size:
                raise ValueError("pool_min_size must not exceed pool_max_size")
            if (
                isinstance(pool_timeout_seconds, bool)
                or pool_timeout_seconds <= 0
            ):
                raise ValueError("pool_timeout_seconds must be greater than zero")
            pool_key = (
                self.dsn,
                pool_min_size,
                pool_max_size,
                float(pool_timeout_seconds),
            )
            self._pool = _acquire_shared_pool(pool_key)
            self._pool_finalizer = finalize(
                self,
                _release_shared_pool,
                pool_key,
                self._pool,
            )
            self._connection_factory = self._pool.getconn

    def close(self) -> None:
        """Close the owned connection pool; injected factories remain caller-owned."""

        pool_finalizer = self._pool_finalizer
        if pool_finalizer is not None and pool_finalizer.alive:
            pool_finalizer()

    def unit_of_work(self) -> PostgresEventUnitOfWork:
        return PostgresEventUnitOfWork(self)

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        """Append and commit one event when no wider UoW is needed."""

        with self.unit_of_work() as unit_of_work:
            result = unit_of_work.append_event(
                event,
                expected_last_sequence=expected_last_sequence,
            )
            unit_of_work.commit()
            return result

    def get_event(
        self,
        event_id: str,
        *,
        tenant_id: str | None = None,
    ) -> StoredEvent | None:
        event_id = _required_text(event_id, "event_id")
        tenant_scope = _tenant_scope(tenant_id)
        row = self._fetch_one(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM durable_events
            WHERE event_id = %s AND tenant_scope = %s
            """,
            (event_id, tenant_scope),
        )
        return _stored_event_from_row(row) if row is not None else None

    def read_stream(self, request: StreamReadRequest) -> EventPage:
        if not isinstance(request, StreamReadRequest):
            raise ValueError("request must be StreamReadRequest")
        tenant_scope = _tenant_scope(request.tenant_id)
        current_high = self.get_stream_high_watermark(
            request.stream_id,
            tenant_id=request.tenant_id,
        )
        if current_high is None:
            return EventPage(
                stream_id=request.stream_id,
                events=(),
                high_watermark=None,
                tenant_id=request.tenant_id,
            )

        if request.cursor is not None:
            high_watermark = request.cursor.high_watermark
            after_sequence = request.cursor.after_sequence
        else:
            requested_high = request.through_sequence
            high_watermark = (
                current_high
                if requested_high is None
                else min(current_high, requested_high)
            )
            after_sequence = 0

        where = [
            "tenant_scope = %s",
            "stream_id = %s",
            "stream_sequence > %s",
            "stream_sequence <= %s",
        ]
        params: list[Any] = [
            tenant_scope,
            request.stream_id,
            after_sequence,
            high_watermark,
        ]
        if request.event_types:
            where.append("event_type = ANY(%s)")
            params.append(sorted(request.event_types))
        if request.data_schemas:
            where.append("data_schema = ANY(%s)")
            params.append(sorted(request.data_schemas))
        params.append(request.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_EVENT_COLUMNS}
            FROM durable_events
            WHERE {' AND '.join(where)}
            ORDER BY stream_sequence ASC
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > request.limit
        selected = rows[: request.limit]
        events = tuple(_stored_event_from_row(row) for row in selected)
        next_cursor = None
        if has_more and events:
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
        stream_id = _required_text(stream_id, "stream_id")
        row = self._fetch_one(
            """
            SELECT last_sequence
            FROM event_stream_sequences
            WHERE tenant_scope = %s AND stream_id = %s
            """,
            (_tenant_scope(tenant_id), stream_id),
        )
        if row is None or int(row[0]) == 0:
            return None
        return int(row[0])

    def load_transactional_state(
        self,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        normalized_namespace = _required_text(namespace, "namespace")
        normalized_key = _required_text(key, "key")
        row = self._fetch_one(
            """
            SELECT namespace, state_key, revision, checksum, payload
            FROM event_transactional_states
            WHERE namespace = %s AND state_key = %s
            """,
            (normalized_namespace, normalized_key),
        )
        return _transactional_state_from_row(row) if row is not None else None

    def register_subscription(
        self,
        subscription: DurableSubscription,
    ) -> DurableSubscription:
        if not isinstance(subscription, DurableSubscription):
            raise ValueError("subscription must be DurableSubscription")
        if subscription.status is SubscriptionStatus.RETIRED:
            raise ValueError("initial RETIRED subscription registration is not allowed")
        with self._connection() as connection:
            try:
                result = self._register_subscription_in_transaction(
                    connection,
                    subscription,
                )
                connection.commit()
                return result
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def get_subscription(self, key: Any) -> DurableSubscription | None:
        subscription_id = _required_text(key.subscription_id, "subscription_id")
        version = _positive_int(key.subscription_version, "subscription_version")
        row = self._fetch_one(
            f"""
            SELECT {_subscription_columns()}
            FROM event_subscriptions
            WHERE subscription_id = %s AND subscription_version = %s
            """,
            (subscription_id, version),
        )
        return _subscription_from_row(row) if row is not None else None

    def list_subscriptions(self, query: SubscriptionQuery) -> SubscriptionPage:
        if not isinstance(query, SubscriptionQuery):
            raise ValueError("query must be SubscriptionQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.status is not None:
            where.append("status = %s")
            params.append(query.status.value)
        if query.cursor is not None:
            cursor_id, cursor_version = _decode_cursor(query.cursor, 2)
            where.append("(subscription_id, subscription_version) > (%s, %s)")
            params.extend((cursor_id, int(cursor_version)))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_subscription_columns()}
            FROM event_subscriptions
            WHERE {' AND '.join(where)}
            ORDER BY subscription_id, subscription_version
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        subscriptions = tuple(_subscription_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and subscriptions:
            last = subscriptions[-1]
            next_cursor = _encode_cursor(
                (last.subscription_id, last.subscription_version)
            )
        return SubscriptionPage(subscriptions=subscriptions, next_cursor=next_cursor)

    def get_subscription_stream_state(
        self,
        key: Any,
        stream_id: str,
        *,
        tenant_id: str | None = None,
    ) -> SubscriptionStreamState | None:
        row = self._fetch_one(
            f"""
            SELECT {_stream_state_columns()}
            FROM event_subscription_stream_states
            WHERE tenant_scope = %s
              AND subscription_id = %s
              AND subscription_version = %s
              AND stream_id = %s
            """,
            (
                _tenant_scope(tenant_id),
                _required_text(key.subscription_id, "subscription_id"),
                _positive_int(key.subscription_version, "subscription_version"),
                _required_text(stream_id, "stream_id"),
            ),
        )
        return _stream_state_from_row(row) if row is not None else None

    def list_subscription_stream_states(
        self,
        query: SubscriptionStreamStateQuery,
    ) -> SubscriptionStreamStatePage:
        if not isinstance(query, SubscriptionStreamStateQuery):
            raise ValueError("query must be SubscriptionStreamStateQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.subscription_id is not None:
            where.append("subscription_id = %s")
            params.append(query.subscription_id)
        if query.subscription_version is not None:
            where.append("subscription_version = %s")
            params.append(query.subscription_version)
        if query.stream_id is not None:
            where.append("stream_id = %s")
            params.append(query.stream_id)
        if query.cursor is not None:
            cursor = _decode_cursor(query.cursor, 3)
            where.append(
                "(subscription_id, subscription_version, stream_id) > (%s, %s, %s)"
            )
            params.extend((cursor[0], int(cursor[1]), cursor[2]))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_stream_state_columns()}
            FROM event_subscription_stream_states
            WHERE {' AND '.join(where)}
            ORDER BY subscription_id, subscription_version, stream_id
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        states = tuple(_stream_state_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and states:
            last = states[-1]
            next_cursor = _encode_cursor(
                (last.subscription_id, last.subscription_version, last.stream_id)
            )
        return SubscriptionStreamStatePage(states=states, next_cursor=next_cursor)

    def set_subscription_status(
        self,
        key: Any,
        status: SubscriptionStatus,
        *,
        changed_at: datetime,
        reason: str,
    ) -> DurableSubscription:
        status = SubscriptionStatus(status)
        changed_at = _required_utc(changed_at, "changed_at")
        reason_text = _required_text(reason, "reason")
        subscription_id = _required_text(key.subscription_id, "subscription_id")
        version = _positive_int(key.subscription_version, "subscription_version")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT tenant_scope
                        FROM event_subscriptions
                        WHERE subscription_id = %s AND subscription_version = %s
                        """,
                        (subscription_id, version),
                    )
                    scope_row = cursor.fetchone()
                    if scope_row is None:
                        raise EventStoreError("subscription does not exist")
                    if status is SubscriptionStatus.RETIRED:
                        _lock_subscription_registry(
                            cursor,
                            str(scope_row[0]),
                            exclusive=True,
                        )
                    cursor.execute(
                        """
                        SELECT status, updated_at
                        FROM event_subscriptions
                        WHERE subscription_id = %s AND subscription_version = %s
                        FOR UPDATE
                        """,
                        (subscription_id, version),
                    )
                    status_row = cursor.fetchone()
                    if status_row is None:
                        raise EventStoreError("subscription does not exist")
                    current_status = SubscriptionStatus(str(status_row[0]))
                    current_updated_at = _required_utc(
                        status_row[1],
                        "subscription updated_at",
                    )
                    if changed_at < current_updated_at:
                        raise ValueError(
                            "changed_at cannot precede subscription updated_at"
                        )
                    if (
                        current_status is SubscriptionStatus.RETIRED
                        and status is not SubscriptionStatus.RETIRED
                    ):
                        raise EventStoreError("retired subscription versions are immutable")
                    if status is SubscriptionStatus.RETIRED:
                        cursor.execute(
                            """
                            UPDATE event_subscription_stream_states AS state
                            SET retirement_watermark = stream.last_sequence,
                                updated_at = %s
                            FROM event_stream_sequences AS stream
                            WHERE state.tenant_scope = stream.tenant_scope
                              AND state.stream_id = stream.stream_id
                              AND state.subscription_id = %s
                              AND state.subscription_version = %s
                              AND state.retirement_watermark IS NULL
                            """,
                            (changed_at, subscription_id, version),
                        )
                    cursor.execute(
                        f"""
                        UPDATE event_subscriptions
                        SET status = %s, updated_at = %s
                        WHERE subscription_id = %s AND subscription_version = %s
                        RETURNING {_subscription_columns()}
                        """,
                        (status.value, changed_at, subscription_id, version),
                    )
                    row = cursor.fetchone()
                    cursor.execute(
                        """
                        INSERT INTO event_subscription_status_audit (
                            subscription_id,
                            subscription_version,
                            previous_status,
                            new_status,
                            changed_at,
                            reason
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            subscription_id,
                            version,
                            current_status.value,
                            status.value,
                            changed_at,
                            reason_text,
                        ),
                    )
                if row is None:
                    raise EventStoreError("subscription does not exist")
                connection.commit()
                return _subscription_from_row(row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def get_retirement_cancellation_report(
        self,
        cancellation_id: str,
        *,
        tenant_id: str | None = None,
    ) -> RetirementCancellationReport | None:
        normalized_id = _required_text(cancellation_id, "cancellation_id")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    report = _select_retirement_cancellation_report(
                        cursor,
                        normalized_id,
                        tenant_scope=_tenant_scope(tenant_id),
                    )
                connection.commit()
                return report
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def cancel_retired_subscription(
        self,
        request: RetirementCancellationRequest,
    ) -> RetirementCancellationReport:
        if not isinstance(request, RetirementCancellationRequest):
            raise TypeError("request must be RetirementCancellationRequest")
        tenant_scope = _tenant_scope(request.tenant_id)
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (
                            "event-retirement-cancellation:"
                            + _json([tenant_scope, request.cancellation_id]),
                        ),
                    )
                    existing = _select_retirement_cancellation_report(
                        cursor,
                        request.cancellation_id,
                        tenant_scope=tenant_scope,
                    )
                    if existing is not None:
                        _assert_retirement_cancellation_retry(existing, request)
                        connection.commit()
                        return existing
                    cursor.execute(
                        "SELECT tenant_scope FROM event_subscriptions "
                        "WHERE subscription_id = %s AND subscription_version = %s",
                        (
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                        ),
                    )
                    scope_row = cursor.fetchone()
                    if scope_row is None or str(scope_row[0]) != tenant_scope:
                        raise EventRetirementCancellationError(
                            "retirement cancellation subscription is not available in scope"
                        )
                    _lock_subscription_registry(cursor, tenant_scope, exclusive=True)
                    cursor.execute(
                        f"""
                        SELECT {_subscription_columns()}
                        FROM event_subscriptions
                        WHERE subscription_id = %s AND subscription_version = %s
                        FOR UPDATE
                        """,
                        (
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                        ),
                    )
                    subscription_row = cursor.fetchone()
                    if subscription_row is None:
                        raise EventRetirementCancellationError(
                            "retirement cancellation subscription disappeared"
                        )
                    subscription = _subscription_from_row(subscription_row)
                    if (
                        subscription.tenant_id != request.tenant_id
                        or subscription.status is not SubscriptionStatus.RETIRED
                    ):
                        raise EventRetirementCancellationError(
                            "retirement cancellation requires a retired subscription"
                        )
                    cursor.execute(
                        """
                        SELECT 1
                        FROM event_deliveries AS delivery
                        LEFT JOIN event_subscription_stream_states AS state
                          ON state.tenant_scope = delivery.tenant_scope
                         AND state.subscription_id = delivery.subscription_id
                         AND state.subscription_version = delivery.subscription_version
                         AND state.stream_id = delivery.stream_id
                        WHERE delivery.tenant_scope = %s
                          AND delivery.subscription_id = %s
                          AND delivery.subscription_version = %s
                          AND delivery.state IN ('pending', 'claimed', 'retry_wait')
                          AND (
                              state.retirement_watermark IS NULL
                              OR delivery.stream_sequence > state.retirement_watermark
                          )
                        LIMIT 1
                        """,
                        (
                            tenant_scope,
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                        ),
                    )
                    if cursor.fetchone() is not None:
                        raise EventStoreCorruptionError(
                            "retired subscription has work outside its retirement watermark"
                        )
                    cursor.execute(
                        f"""
                        SELECT {_delivery_columns('delivery')}
                        FROM event_deliveries AS delivery
                        JOIN event_subscription_stream_states AS state
                          ON state.tenant_scope = delivery.tenant_scope
                         AND state.subscription_id = delivery.subscription_id
                         AND state.subscription_version = delivery.subscription_version
                         AND state.stream_id = delivery.stream_id
                        WHERE delivery.tenant_scope = %s
                          AND delivery.subscription_id = %s
                          AND delivery.subscription_version = %s
                          AND delivery.state IN ('pending', 'claimed', 'retry_wait')
                          AND state.retirement_watermark IS NOT NULL
                          AND delivery.stream_sequence <= state.retirement_watermark
                        ORDER BY delivery.stream_id COLLATE "C",
                                 delivery.stream_sequence,
                                 delivery.delivery_generation,
                                 delivery.delivery_id COLLATE "C"
                        LIMIT %s
                        FOR UPDATE OF delivery
                        """,
                        (
                            tenant_scope,
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                            request.limit,
                        ),
                    )
                    rows = cursor.fetchall()
                    previous_deliveries = tuple(
                        _delivery_from_row(row) for row in rows
                    )
                    time_floor = [
                        _database_now(cursor),
                        request.requested_at,
                    ]
                    if subscription.updated_at is not None:
                        time_floor.append(subscription.updated_at)
                    time_floor.extend(
                        delivery.updated_at for delivery in previous_deliveries
                    )
                    cancelled_at = max(time_floor)
                    cursor.execute(
                        """
                        INSERT INTO event_retirement_cancellation_reports (
                            cancellation_id, tenant_id, subscription_id,
                            subscription_version, requested_at, cancelled_at,
                            operator_id, operator_reason,
                            authorization_evidence_ref, item_limit,
                            cancelled_count, remaining_nonterminal_count,
                            remaining_nonterminal_count_truncated
                        ) VALUES (
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, 0, FALSE
                        )
                        """,
                        (
                            request.cancellation_id,
                            request.tenant_id,
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                            request.requested_at,
                            cancelled_at,
                            request.operator_id,
                            request.operator_reason,
                            request.authorization_evidence_ref,
                            request.limit,
                            len(previous_deliveries),
                        ),
                    )
                    affected_streams: dict[str, DeliveryRecord] = {}
                    for previous in previous_deliveries:
                        cursor.execute(
                            """
                            INSERT INTO event_retirement_cancellation_items (
                                tenant_id, cancellation_id, delivery_id, event_id,
                                stream_id, stream_sequence, subscription_id,
                                subscription_version, delivery_generation,
                                previous_state, previous_attempt_count,
                                previous_reason_class, terminal_state, cancelled_at
                            ) VALUES (
                                %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s,
                                %s, %s,
                                %s, 'dropped', %s
                            )
                            """,
                            (
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
                                cancelled_at,
                            ),
                        )
                        cursor.execute(
                            f"""
                            UPDATE event_deliveries
                            SET state = 'dropped',
                                attempt_count = GREATEST(attempt_count, 1),
                                available_at = NULL,
                                lease_owner = NULL,
                                lease_generation = NULL,
                                lease_expires_at = NULL,
                                reason_class = 'subscription_retired',
                                redacted_diagnostic = NULL,
                                updated_at = %s
                            WHERE delivery_id = %s AND state = %s
                            RETURNING {_delivery_columns()}
                            """,
                            (
                                cancelled_at,
                                previous.delivery_id,
                                previous.state.value,
                            ),
                        )
                        updated_row = cursor.fetchone()
                        if updated_row is None:
                            raise EventRetirementCancellationError(
                                "retirement cancellation delivery changed concurrently"
                            )
                        updated = _delivery_from_row(updated_row)
                        if updated.delivery_generation == 1:
                            affected_streams[updated.stream_id] = updated
                    for stream_id in sorted(affected_streams):
                        self._advance_checkpoint(
                            cursor,
                            affected_streams[stream_id],
                            updated_at=cancelled_at,
                        )
                    cursor.execute(
                        """
                        SELECT 1
                        FROM event_deliveries
                        WHERE tenant_scope = %s
                          AND subscription_id = %s
                          AND subscription_version = %s
                          AND state IN ('pending', 'claimed', 'retry_wait')
                        LIMIT %s
                        """,
                        (
                            tenant_scope,
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                            request.limit + 1,
                        ),
                    )
                    remaining = len(cursor.fetchall())
                    remaining_truncated = remaining > request.limit
                    cursor.execute(
                        """
                        UPDATE event_retirement_cancellation_reports
                        SET remaining_nonterminal_count = %s,
                            remaining_nonterminal_count_truncated = %s
                        WHERE tenant_scope = %s AND cancellation_id = %s
                        """,
                        (
                            remaining,
                            remaining_truncated,
                            tenant_scope,
                            request.cancellation_id,
                        ),
                    )
                    report = _select_retirement_cancellation_report(
                        cursor,
                        request.cancellation_id,
                        tenant_scope=tenant_scope,
                    )
                    if report is None:
                        raise EventStoreCorruptionError(
                            "retirement cancellation report disappeared before commit"
                        )
                connection.commit()
                return report
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def get_delivery(
        self,
        delivery_id: str,
        *,
        tenant_id: str | None = None,
    ) -> DeliveryRecord | None:
        row = self._fetch_one(
            f"""
            SELECT {_delivery_columns()}
            FROM event_deliveries
            WHERE delivery_id = %s AND tenant_scope = %s
            """,
            (
                _required_text(delivery_id, "delivery_id"),
                _tenant_scope(tenant_id),
            ),
        )
        return _delivery_from_row(row) if row is not None else None

    def list_deliveries(self, query: DeliveryQuery) -> DeliveryPage:
        if not isinstance(query, DeliveryQuery):
            raise ValueError("query must be DeliveryQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.subscription_id is not None:
            where.append("subscription_id = %s")
            params.append(query.subscription_id)
        if query.subscription_version is not None:
            where.append("subscription_version = %s")
            params.append(query.subscription_version)
        if query.stream_id is not None:
            where.append("stream_id = %s")
            params.append(query.stream_id)
        if query.state is not None:
            where.append("state = %s")
            params.append(query.state.value)
        if query.after_sequence is not None:
            where.append("stream_sequence > %s")
            params.append(query.after_sequence)
        if query.cursor is not None:
            cursor = _decode_cursor(query.cursor, 4)
            where.append(
                "(stream_id, stream_sequence, delivery_generation, delivery_id) "
                "> (%s, %s, %s, %s)"
            )
            params.extend((cursor[0], int(cursor[1]), int(cursor[2]), cursor[3]))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_delivery_columns()}
            FROM event_deliveries
            WHERE {' AND '.join(where)}
            ORDER BY stream_id, stream_sequence, delivery_generation, delivery_id
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        records = tuple(_delivery_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and records:
            last = records[-1]
            next_cursor = _encode_cursor(
                (
                    last.stream_id,
                    last.stream_sequence,
                    last.delivery_generation,
                    last.delivery_id,
                )
            )
        return DeliveryPage(records=records, next_cursor=next_cursor)

    def claim_deliveries(
        self,
        request: DeliveryClaimRequest,
    ) -> tuple[ClaimedDelivery, ...]:
        if not isinstance(request, DeliveryClaimRequest):
            raise ValueError("request must be DeliveryClaimRequest")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT {_subscription_columns()}
                        FROM event_subscriptions
                        WHERE subscription_id = %s AND subscription_version = %s
                        FOR SHARE
                        """,
                        (request.subscription_id, request.subscription_version),
                    )
                    subscription_row = cursor.fetchone()
                    if subscription_row is None:
                        raise EventStoreError("subscription does not exist")
                    subscription = _subscription_from_row(subscription_row)
                    # Claims and append admission share one transaction-scoped
                    # subscription fence.  Capacity-controlled claim paths read
                    # the subscription definition before that fence; append
                    # acquires every needed fence before mutating stream or
                    # delivery rows.
                    _lock_subscription_delivery_capacity(cursor, subscription)
                    database_now = _database_now(cursor)
                    if subscription.status is SubscriptionStatus.PAUSED:
                        connection.commit()
                        return ()

                    # Expiry recovery consumes the attempt already recorded by
                    # the claim.  Exhausted deliveries become terminal before
                    # any later sequence can be selected.
                    cursor.execute(
                        """
                        UPDATE event_deliveries
                        SET state = 'retry_wait',
                            available_at = %s,
                            lease_owner = NULL,
                            lease_generation = NULL,
                            lease_expires_at = NULL,
                            first_failure_at = COALESCE(
                                first_failure_at,
                                GREATEST(updated_at, %s)
                            ),
                            last_failure_at = GREATEST(
                                COALESCE(last_failure_at, updated_at),
                                updated_at,
                                %s
                            ),
                            reason_class = 'lease_expired',
                            redacted_diagnostic = 'delivery lease expired before settlement',
                            updated_at = GREATEST(updated_at, %s)
                        WHERE subscription_id = %s
                          AND subscription_version = %s
                          AND state = 'claimed'
                          AND lease_expires_at <= %s
                          AND attempt_count < %s
                        """,
                        (
                            database_now,
                            database_now,
                            database_now,
                            database_now,
                            request.subscription_id,
                            request.subscription_version,
                            database_now,
                            subscription.retry_policy.max_attempts,
                        ),
                    )
                    cursor.execute(
                        f"""
                        UPDATE event_deliveries
                        SET state = 'dead_letter',
                            available_at = NULL,
                            lease_owner = NULL,
                            lease_generation = NULL,
                            lease_expires_at = NULL,
                            first_failure_at = COALESCE(
                                first_failure_at,
                                GREATEST(updated_at, %s)
                            ),
                            last_failure_at = GREATEST(
                                COALESCE(last_failure_at, updated_at),
                                updated_at,
                                %s
                            ),
                            reason_class = 'lease_expired',
                            redacted_diagnostic =
                                'delivery lease expired after retry budget exhaustion',
                            updated_at = GREATEST(updated_at, %s)
                        WHERE subscription_id = %s
                          AND subscription_version = %s
                          AND state = 'claimed'
                          AND lease_expires_at <= %s
                          AND attempt_count >= %s
                        RETURNING {_delivery_columns()}
                        """,
                        (
                            database_now,
                            database_now,
                            database_now,
                            request.subscription_id,
                            request.subscription_version,
                            database_now,
                            subscription.retry_policy.max_attempts,
                        ),
                    )
                    exhausted = tuple(
                        _delivery_from_row(row) for row in cursor.fetchall()
                    )
                    for delivery in exhausted:
                        dead_letter_id = _dead_letter_id(
                            delivery.delivery_id,
                            delivery.delivery_generation,
                        )
                        cursor.execute(
                            """
                            INSERT INTO event_dead_letters (
                                dead_letter_id, delivery_id, event_id, tenant_id,
                                stream_id, stream_sequence, subscription_id,
                                subscription_version, consumer_id, consumer_effect_id,
                                delivery_generation, attempt_count, first_failure_at,
                                last_failure_at, reason_class, redacted_diagnostic
                            ) VALUES (
                                %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s,
                                %s, %s, %s
                            )
                            """,
                            (
                                dead_letter_id,
                                delivery.delivery_id,
                                delivery.event_id,
                                delivery.tenant_id,
                                delivery.stream_id,
                                delivery.stream_sequence,
                                delivery.subscription_id,
                                delivery.subscription_version,
                                delivery.consumer_id,
                                delivery.consumer_effect_id,
                                delivery.delivery_generation,
                                delivery.attempt_count,
                                delivery.first_failure_at,
                                delivery.last_failure_at,
                                delivery.reason_class,
                                delivery.redacted_diagnostic,
                            ),
                        )
                        if delivery.delivery_generation == 1:
                            self._advance_checkpoint(
                                cursor,
                                delivery,
                                updated_at=database_now,
                            )
                    if not _subscription_lease_owner_slot_available(
                        cursor,
                        subscription,
                        lease_owner=request.lease_owner,
                        database_now=database_now,
                    ):
                        connection.commit()
                        return ()
                    in_flight = _bounded_subscription_in_flight(
                        cursor,
                        subscription,
                    )
                    available_capacity = max(
                        0,
                        subscription.limits.max_in_flight - in_flight,
                    )
                    claim_limit = min(
                        request.limit,
                        subscription.limits.batch_size,
                        available_capacity,
                    )
                    if claim_limit == 0:
                        connection.commit()
                        return ()

                    where = [
                        "d.subscription_id = %s",
                        "d.subscription_version = %s",
                        "d.state IN ('pending', 'retry_wait')",
                        "(d.available_at IS NULL OR d.available_at <= %s)",
                    ]
                    params: list[Any] = [
                        request.subscription_id,
                        request.subscription_version,
                        database_now,
                    ]
                    if request.stream_id is not None:
                        where.append("d.stream_id = %s")
                        params.append(request.stream_id)
                    where.append(
                        """
                        (
                            d.delivery_generation > 1
                            OR NOT EXISTS (
                                SELECT 1
                                FROM event_deliveries AS prior
                                WHERE prior.tenant_scope = d.tenant_scope
                                  AND prior.subscription_id = d.subscription_id
                                  AND prior.subscription_version = d.subscription_version
                                  AND prior.stream_id = d.stream_id
                                  AND prior.delivery_generation = 1
                                  AND prior.stream_sequence < d.stream_sequence
                                  AND prior.state IN ('pending', 'claimed', 'retry_wait')
                            )
                        )
                        """
                    )
                    params.append(claim_limit)
                    cursor.execute(
                        f"""
                        SELECT d.delivery_id
                        FROM event_deliveries AS d
                        WHERE {' AND '.join(where)}
                        ORDER BY
                            d.tenant_scope,
                            d.stream_id,
                            d.stream_sequence,
                            d.delivery_generation,
                            d.delivery_id
                        FOR UPDATE OF d SKIP LOCKED
                        LIMIT %s
                        """,
                        tuple(params),
                    )
                    delivery_ids = tuple(str(row[0]) for row in cursor.fetchall())
                    lease_expires_at = database_now + timedelta(
                        seconds=request.lease_duration_seconds
                    )
                    claimed: list[ClaimedDelivery] = []
                    for delivery_id in delivery_ids:
                        cursor.execute(
                            f"""
                            UPDATE event_deliveries
                            SET state = 'claimed',
                                attempt_count = attempt_count + 1,
                                lease_owner = %s,
                                lease_generation = attempt_count + 1,
                                lease_expires_at = %s,
                                available_at = NULL,
                                updated_at = GREATEST(updated_at, %s)
                            WHERE delivery_id = %s
                            RETURNING {_delivery_columns()}
                            """,
                            (
                                request.lease_owner,
                                lease_expires_at,
                                database_now,
                                delivery_id,
                            ),
                        )
                        delivery_row = cursor.fetchone()
                        if delivery_row is None:
                            raise EventStoreCorruptionError(
                                "locked delivery disappeared during claim"
                            )
                        delivery = _delivery_from_row(delivery_row)
                        cursor.execute(
                            f"""
                            SELECT {_EVENT_COLUMNS}
                            FROM durable_events
                            WHERE event_id = %s AND tenant_scope = %s
                            """,
                            (delivery.event_id, _tenant_scope(delivery.tenant_id)),
                        )
                        event_row = cursor.fetchone()
                        if event_row is None:
                            raise EventStoreCorruptionError(
                                "delivery references a missing durable event"
                            )
                        event = _stored_event_from_row(event_row)
                        lease = DeliveryLeaseToken(
                            delivery_id=delivery.delivery_id,
                            delivery_generation=delivery.delivery_generation,
                            lease_owner=request.lease_owner,
                            lease_generation=delivery.lease_generation,
                            lease_expires_at=delivery.lease_expires_at,
                            lease_started_at=database_now,
                        )
                        claimed.append(
                            ClaimedDelivery(
                                delivery=delivery,
                                event=event,
                                lease=lease,
                            )
                        )
                connection.commit()
                return tuple(claimed)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def renew_delivery_lease(
        self,
        lease: DeliveryLeaseToken,
        *,
        renewed_at: datetime,
        lease_duration_seconds: float,
    ) -> DeliveryLeaseToken:
        if not isinstance(lease, DeliveryLeaseToken):
            raise ValueError("lease must be DeliveryLeaseToken")
        _required_utc(renewed_at, "renewed_at")
        duration = LeasePolicy(lease_duration_seconds).duration_seconds
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH lease_clock AS (
                            SELECT clock_timestamp() AS database_now
                        )
                        UPDATE event_deliveries AS delivery
                        SET lease_expires_at =
                                lease_clock.database_now
                                + make_interval(secs => %s),
                            updated_at = GREATEST(
                                delivery.updated_at,
                                lease_clock.database_now
                            )
                        FROM lease_clock
                        WHERE delivery_id = %s
                          AND delivery_generation = %s
                          AND state = 'claimed'
                          AND lease_owner = %s
                          AND lease_generation = %s
                          AND lease_expires_at = %s
                          AND lease_expires_at > lease_clock.database_now
                        RETURNING delivery.lease_expires_at,
                                  lease_clock.database_now
                        """,
                        (
                            duration,
                            lease.delivery_id,
                            lease.delivery_generation,
                            lease.lease_owner,
                            lease.lease_generation,
                            lease.lease_expires_at,
                        ),
                    )
                    renewed_row = cursor.fetchone()
                    if renewed_row is None:
                        raise EventStaleLeaseError(
                            "delivery lease is stale or already expired"
                        )
                    new_expiry = _required_utc(
                        renewed_row[0],
                        "renewed lease_expires_at",
                    )
                    lease_started_at = _required_utc(
                        renewed_row[1],
                        "renewed lease_started_at",
                    )
                connection.commit()
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)
        return DeliveryLeaseToken(
            delivery_id=lease.delivery_id,
            delivery_generation=lease.delivery_generation,
            lease_owner=lease.lease_owner,
            lease_generation=lease.lease_generation,
            lease_expires_at=new_expiry,
            lease_started_at=lease_started_at,
        )

    def settle_delivery(
        self,
        settlement: DeliverySettlement,
    ) -> DeliverySettlementResult:
        with self.unit_of_work() as unit_of_work:
            result = unit_of_work.settle_delivery(settlement)
            unit_of_work.commit()
            return result

    def pending_delivery_stats(
        self,
        key: Any,
        *,
        stream_id: str | None = None,
    ) -> PendingDeliveryStats:
        subscription_id = _required_text(key.subscription_id, "subscription_id")
        version = _positive_int(key.subscription_version, "subscription_version")
        stream_filter = ""
        params: list[Any] = [subscription_id, version]
        if stream_id is not None:
            stream_filter = "AND delivery.stream_id = %s"
            params.append(_required_text(stream_id, "stream_id"))
        row = self._fetch_one(
            f"""
            WITH delivery_clock AS (
                SELECT clock_timestamp() AS database_now
            ), subscription AS (
                SELECT tenant_scope, subscription_id, subscription_version,
                       pending_warning_threshold, pending_hard_limit
                FROM event_subscriptions
                WHERE subscription_id = %s AND subscription_version = %s
            ), pending AS (
                SELECT count(*) AS pending_count,
                       count(*) FILTER (
                           WHERE delivery.delivery_generation = 1
                       ) AS lag,
                       count(*) FILTER (
                           WHERE delivery.delivery_generation > 1
                       ) AS late_repair_pending_count,
                       min(delivery.created_at) AS oldest_pending_at
                FROM event_deliveries AS delivery
                JOIN subscription
                  ON subscription.tenant_scope = delivery.tenant_scope
                 AND subscription.subscription_id = delivery.subscription_id
                 AND subscription.subscription_version =
                     delivery.subscription_version
                WHERE delivery.state IN ('pending', 'claimed', 'retry_wait')
                  {stream_filter}
            )
            SELECT pending.pending_count,
                   pending.lag,
                   pending.late_repair_pending_count,
                   pending.oldest_pending_at,
                   delivery_clock.database_now,
                   subscription.pending_warning_threshold,
                   subscription.pending_hard_limit
            FROM subscription
            CROSS JOIN pending
            CROSS JOIN delivery_clock
            """,
            tuple(params),
        )
        if row is None:
            raise EventStoreError("subscription does not exist")
        pending_count = int(row[0])
        oldest_pending_at = (
            _required_utc(row[3], "oldest_pending_at")
            if row[3] is not None
            else None
        )
        database_now = _required_utc(row[4], "database_now")
        return PendingDeliveryStats(
            pending_count=pending_count,
            lag=int(row[1]),
            oldest_pending_at=oldest_pending_at,
            oldest_pending_age_seconds=(
                max(0.0, (database_now - oldest_pending_at).total_seconds())
                if oldest_pending_at is not None
                else None
            ),
            late_repair_pending_count=int(row[2]),
            warning_threshold_reached=pending_count >= int(row[5]),
            capacity_remaining=max(0, int(row[6]) - pending_count),
        )

    def get_inbox_entry(
        self,
        key: InboxKey,
        *,
        tenant_id: str | None = None,
    ) -> InboxEntry | None:
        if not isinstance(key, InboxKey):
            raise ValueError("key must be InboxKey")
        row = self._fetch_one(
            """
            SELECT event_id, consumer_effect_id, completed_at,
                   delivery_id, result_checksum
            FROM event_inbox
            WHERE event_id = %s
              AND consumer_effect_id = %s
              AND tenant_scope = %s
            """,
            (key.event_id, key.consumer_effect_id, _tenant_scope(tenant_id)),
        )
        return _inbox_from_row(row) if row is not None else None

    def get_checkpoint(
        self,
        key: CheckpointKey,
        *,
        tenant_id: str | None = None,
    ) -> ConsumerCheckpoint | None:
        if not isinstance(key, CheckpointKey):
            raise ValueError("key must be CheckpointKey")
        if tenant_id is not None and tenant_id != key.tenant_id:
            raise ValueError("tenant_id must match checkpoint key scope")
        row = self._fetch_one(
            f"""
            SELECT {_checkpoint_columns()}
            FROM event_consumer_checkpoints
            WHERE tenant_scope = %s
              AND subscription_id = %s
              AND subscription_version = %s
              AND stream_id = %s
            """,
            (
                _tenant_scope(key.tenant_id),
                key.subscription_id,
                key.subscription_version,
                key.stream_id,
            ),
        )
        return _checkpoint_from_row(row) if row is not None else None

    def list_checkpoints(self, query: CheckpointQuery) -> CheckpointPage:
        if not isinstance(query, CheckpointQuery):
            raise ValueError("query must be CheckpointQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.subscription_id is not None:
            where.append("subscription_id = %s")
            params.append(query.subscription_id)
        if query.subscription_version is not None:
            where.append("subscription_version = %s")
            params.append(query.subscription_version)
        if query.stream_id is not None:
            where.append("stream_id = %s")
            params.append(query.stream_id)
        if query.cursor is not None:
            cursor = _decode_cursor(query.cursor, 3)
            where.append(
                "(subscription_id, subscription_version, stream_id) > (%s, %s, %s)"
            )
            params.extend((cursor[0], int(cursor[1]), cursor[2]))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_checkpoint_columns()}
            FROM event_consumer_checkpoints
            WHERE {' AND '.join(where)}
            ORDER BY subscription_id, subscription_version, stream_id
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        checkpoints = tuple(_checkpoint_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and checkpoints:
            last = checkpoints[-1]
            next_cursor = _encode_cursor(
                (last.subscription_id, last.subscription_version, last.stream_id)
            )
        return CheckpointPage(checkpoints=checkpoints, next_cursor=next_cursor)

    def get_dead_letter(
        self,
        dead_letter_id: str,
        *,
        tenant_id: str | None = None,
    ) -> DeadLetterRecord | None:
        row = self._fetch_one(
            f"""
            SELECT {_dead_letter_columns()}
            FROM event_dead_letters
            WHERE dead_letter_id = %s AND tenant_scope = %s
            """,
            (
                _required_text(dead_letter_id, "dead_letter_id"),
                _tenant_scope(tenant_id),
            ),
        )
        return _dead_letter_from_row(row) if row is not None else None

    def list_dead_letters(self, query: DeadLetterQuery) -> DeadLetterPage:
        if not isinstance(query, DeadLetterQuery):
            raise ValueError("query must be DeadLetterQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.subscription_id is not None:
            where.append("subscription_id = %s")
            params.append(query.subscription_id)
        if query.subscription_version is not None:
            where.append("subscription_version = %s")
            params.append(query.subscription_version)
        if query.disposition is not None:
            where.append("disposition = %s")
            params.append(query.disposition.value)
        if query.cursor is not None:
            cursor_time, cursor_id = _decode_cursor(query.cursor, 2)
            where.append("(last_failure_at, dead_letter_id) > (%s, %s)")
            params.extend((_parse_time(cursor_time), cursor_id))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_dead_letter_columns()}
            FROM event_dead_letters
            WHERE {' AND '.join(where)}
            ORDER BY last_failure_at, dead_letter_id
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        records = tuple(_dead_letter_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and records:
            last = records[-1]
            next_cursor = _encode_cursor(
                (_format_time(last.last_failure_at), last.dead_letter_id)
            )
        return DeadLetterPage(records=records, next_cursor=next_cursor)

    def requeue_dead_letter(self, action: DeadLetterAction) -> DeliveryRecord:
        if not isinstance(action, DeadLetterAction):
            raise ValueError("action must be DeadLetterAction")
        if not action.idempotency_ready:
            raise EventConsumerIdempotencyError(
                "dead-letter requeue requires an idempotency-ready effect boundary"
            )
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    # Resolve immutable ownership without taking the dead-letter
                    # row lock first.  Every path that can add or remove bounded
                    # delivery work must acquire capacity before a delivery/DLQ
                    # row lock, otherwise requeue and settlement can deadlock
                    # with append/claim transactions that use the opposite order.
                    cursor.execute(
                        f"""
                        SELECT {_dead_letter_columns()}
                        FROM event_dead_letters
                        WHERE dead_letter_id = %s
                        """,
                        (action.dead_letter_id,),
                    )
                    preliminary_row = cursor.fetchone()
                    if preliminary_row is None:
                        raise EventStoreError("dead letter does not exist")
                    preliminary = _dead_letter_from_row(preliminary_row)
                    cursor.execute(
                        f"""
                        SELECT {_subscription_columns()}
                        FROM event_subscriptions
                        WHERE subscription_id = %s AND subscription_version = %s
                        FOR SHARE
                        """,
                        (
                            preliminary.subscription_id,
                            preliminary.subscription_version,
                        ),
                    )
                    subscription_row = cursor.fetchone()
                    if subscription_row is None:
                        raise EventStoreCorruptionError(
                            "dead letter references a missing subscription"
                        )
                    subscription = _subscription_from_row(subscription_row)
                    if subscription.status is SubscriptionStatus.RETIRED:
                        raise EventRetirementCancellationError(
                            "retired subscription cannot accept dead-letter requeue"
                        )
                    _lock_subscription_delivery_capacity(cursor, subscription)
                    cursor.execute(
                        f"""
                        SELECT {_dead_letter_columns()}
                        FROM event_dead_letters
                        WHERE dead_letter_id = %s
                        FOR UPDATE
                        """,
                        (action.dead_letter_id,),
                    )
                    locked_row = cursor.fetchone()
                    if locked_row is None:
                        raise EventStoreCorruptionError(
                            "dead letter disappeared during requeue"
                        )
                    record = _dead_letter_from_row(locked_row)
                    if (
                        record.subscription_id != subscription.subscription_id
                        or record.subscription_version
                        != subscription.subscription_version
                    ):
                        raise EventStoreCorruptionError(
                            "dead letter subscription identity changed during requeue"
                        )
                    if record.disposition is not DeadLetterDisposition.OPEN:
                        raise EventStoreError(
                            "dead letter is already terminally operated"
                        )
                    if not subscription.supports_out_of_order_repair:
                        raise EventConsumerIdempotencyError(
                            "subscription does not permit out-of-order late repair"
                        )
                    if _subscription_pending_capacity_exhausted(
                        cursor,
                        subscription,
                    ):
                        raise EventStoreCapacityError(
                            "subscription durable pending hard limit is exhausted"
                        )
                    cursor.execute(
                        """
                        SELECT COALESCE(max(delivery_generation), 0) + 1
                        FROM event_deliveries
                        WHERE event_id = %s
                          AND subscription_id = %s
                          AND subscription_version = %s
                        """,
                        (
                            record.event_id,
                            record.subscription_id,
                            record.subscription_version,
                        ),
                    )
                    generation = int(cursor.fetchone()[0])
                    database_now = _database_now(cursor)
                    inserted = self._insert_delivery(
                        cursor,
                        subscription,
                        event_id=record.event_id,
                        stream_id=record.stream_id,
                        stream_sequence=record.stream_sequence,
                        created_at=database_now,
                        delivery_generation=generation,
                    )
                    if not inserted:
                        raise EventStoreCorruptionError(
                            "late-repair delivery generation was not inserted"
                        )
                    cursor.execute(
                        """
                        UPDATE event_dead_letters
                        SET disposition = 'requeued', operator_id = %s,
                            operator_reason = %s,
                            updated_at = GREATEST(updated_at, %s, %s)
                        WHERE dead_letter_id = %s
                        """,
                        (
                            action.operator_id,
                            action.reason,
                            database_now,
                            action.requested_at,
                            action.dead_letter_id,
                        ),
                    )
                    delivery_id = _delivery_id(
                        record.event_id,
                        record.subscription_id,
                        record.subscription_version,
                        generation,
                    )
                    cursor.execute(
                        f"""
                        SELECT {_delivery_columns()}
                        FROM event_deliveries
                        WHERE delivery_id = %s
                        """,
                        (delivery_id,),
                    )
                    delivery_row = cursor.fetchone()
                    if delivery_row is None:
                        raise EventStoreCorruptionError(
                            "late-repair delivery cannot be read after insert"
                        )
                connection.commit()
                return _delivery_from_row(delivery_row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def resolve_dead_letter(self, action: DeadLetterAction) -> DeadLetterRecord:
        if not isinstance(action, DeadLetterAction):
            raise ValueError("action must be DeadLetterAction")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE event_dead_letters
                        SET disposition = 'resolved', operator_id = %s,
                            operator_reason = %s, updated_at = %s
                        WHERE dead_letter_id = %s AND disposition = 'open'
                        RETURNING {_dead_letter_columns()}
                        """,
                        (
                            action.operator_id,
                            action.reason,
                            action.requested_at,
                            action.dead_letter_id,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise EventStoreError(
                            "dead letter does not exist or is already operated"
                        )
                connection.commit()
                return _dead_letter_from_row(row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def begin_redelivery(self, request: RedeliveryRequest) -> RedeliveryReport:
        if not isinstance(request, RedeliveryRequest):
            raise ValueError("request must be RedeliveryRequest")
        tenant_scope = _tenant_scope(request.tenant_id)
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (
                            "event-redelivery:"
                            + _json([tenant_scope, request.redelivery_id]),
                        ),
                    )
                    cursor.execute(
                        f"""
                        SELECT {_redelivery_report_columns()}
                        FROM event_redelivery_reports
                        WHERE tenant_scope = %s AND redelivery_id = %s
                        """,
                        (tenant_scope, request.redelivery_id),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        existing = _redelivery_report_from_postgres(
                            cursor,
                            existing_row,
                        )
                        if _redelivery_request_identity(existing) != (
                            _redelivery_request_identity(request)
                        ):
                            raise EventStoreError(
                                "redelivery identity already has a different request"
                            )
                        connection.commit()
                        return existing

                    cursor.execute(
                        f"""
                        SELECT {_subscription_columns()}
                        FROM event_subscriptions
                        WHERE subscription_id = %s
                          AND subscription_version = %s
                          AND tenant_scope = %s
                        FOR SHARE
                        """,
                        (
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                            tenant_scope,
                        ),
                    )
                    subscription_row = cursor.fetchone()
                    if subscription_row is None:
                        raise EventStoreError(
                            "redelivery subscription is unavailable in tenant scope"
                        )
                    subscription = _subscription_from_row(subscription_row)
                    if subscription.status is SubscriptionStatus.RETIRED:
                        raise EventStoreError(
                            "retired subscription cannot accept redelivery work"
                        )
                    if not subscription.supports_out_of_order_repair:
                        raise EventConsumerIdempotencyError(
                            "subscription does not support idempotent out-of-order repair"
                        )

                    # Capacity is the canonical first mutable-resource fence for
                    # append, claim, settlement, DLQ requeue, and redelivery.
                    _lock_subscription_delivery_capacity(cursor, subscription)
                    cursor.execute(
                        """
                        SELECT last_sequence
                        FROM event_stream_sequences
                        WHERE tenant_scope = %s AND stream_id = %s
                        FOR SHARE
                        """,
                        (tenant_scope, request.source_stream_id),
                    )
                    stream_row = cursor.fetchone()
                    if stream_row is None:
                        raise EventStoreError("redelivery source stream does not exist")
                    captured_high_watermark = int(stream_row[0])
                    through_sequence = (
                        request.through_sequence or captured_high_watermark
                    )
                    if request.from_sequence > captured_high_watermark:
                        raise EventStoreError(
                            "redelivery range starts above the captured watermark"
                        )
                    if through_sequence > captured_high_watermark:
                        raise EventStoreError(
                            "redelivery range exceeds the captured watermark"
                        )
                    if (
                        through_sequence - request.from_sequence + 1
                        > MAX_REDELIVERY_ITEMS
                    ):
                        raise EventStoreCapacityError(
                            "redelivery range exceeds the bounded "
                            "stream-position limit"
                        )

                    cursor.execute(
                        """
                        SELECT 1
                        FROM event_deliveries
                        WHERE tenant_scope = %s
                          AND subscription_id = %s
                          AND subscription_version = %s
                          AND stream_id = %s
                          AND stream_sequence BETWEEN %s AND %s
                          AND state IN ('pending', 'claimed', 'retry_wait')
                        LIMIT 1
                        """,
                        (
                            tenant_scope,
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                            request.source_stream_id,
                            request.from_sequence,
                            through_sequence,
                        ),
                    )
                    if cursor.fetchone() is not None:
                        raise EventStoreError(
                            "redelivery target contains nonterminal delivery work"
                        )

                    cursor.execute(
                        """
                        SELECT events.event_id, events.stream_id, events.stream_sequence
                        FROM durable_events AS events
                        JOIN event_deliveries AS deliveries
                          ON deliveries.event_id = events.event_id
                         AND deliveries.tenant_scope = events.tenant_scope
                         AND deliveries.stream_id = events.stream_id
                         AND deliveries.stream_sequence = events.stream_sequence
                        WHERE deliveries.tenant_scope = %s
                          AND deliveries.subscription_id = %s
                          AND deliveries.subscription_version = %s
                          AND deliveries.delivery_generation = 1
                          AND deliveries.stream_id = %s
                          AND deliveries.stream_sequence BETWEEN %s AND %s
                        ORDER BY deliveries.stream_sequence, deliveries.event_id
                        LIMIT %s
                        """,
                        (
                            tenant_scope,
                            request.subscription.subscription_id,
                            request.subscription.subscription_version,
                            request.source_stream_id,
                            request.from_sequence,
                            through_sequence,
                            MAX_REDELIVERY_ITEMS + 1,
                        ),
                    )
                    event_rows = tuple(cursor.fetchall())
                    if not event_rows:
                        raise EventStoreError(
                            "redelivery range contains no existing event-consumer pair"
                        )
                    if len(event_rows) > MAX_REDELIVERY_ITEMS:
                        raise EventStoreCapacityError(
                            "redelivery selection exceeds the "
                            f"{MAX_REDELIVERY_ITEMS}-item limit"
                        )

                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM event_deliveries
                        WHERE tenant_scope = %s
                          AND subscription_id = %s
                          AND subscription_version = %s
                          AND state IN ('pending', 'claimed', 'retry_wait')
                        """,
                        (
                            tenant_scope,
                            subscription.subscription_id,
                            subscription.subscription_version,
                        ),
                    )
                    pending_row = cursor.fetchone()
                    if pending_row is None:
                        raise EventStoreCorruptionError(
                            "redelivery capacity query returned no row"
                        )
                    if (
                        int(pending_row[0]) + len(event_rows)
                        > subscription.limits.pending_hard_limit
                    ):
                        raise EventStoreCapacityError(
                            "redelivery exceeds the subscription durable pending hard limit"
                        )

                    scheduled_at = max(_database_now(cursor), request.requested_at)
                    cursor.execute(
                        """
                        INSERT INTO event_redelivery_reports (
                            redelivery_id, tenant_id, subscription_id,
                            subscription_version, source_stream_id, from_sequence,
                            requested_through_sequence, through_sequence,
                            captured_high_watermark, requested_at, scheduled_at,
                            operator_id, operator_reason, authorization_evidence_ref
                        ) VALUES (
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s, %s
                        )
                        """,
                        (
                            request.redelivery_id,
                            request.tenant_id,
                            subscription.subscription_id,
                            subscription.subscription_version,
                            request.source_stream_id,
                            request.from_sequence,
                            request.through_sequence,
                            through_sequence,
                            captured_high_watermark,
                            request.requested_at,
                            scheduled_at,
                            request.operator_id,
                            request.operator_reason,
                            request.authorization_evidence_ref,
                        ),
                    )

                    for event_id_value, stream_id_value, sequence_value in event_rows:
                        event_id = str(event_id_value)
                        cursor.execute(
                            """
                            SELECT COALESCE(MAX(delivery_generation), 0) + 1
                            FROM event_deliveries
                            WHERE event_id = %s
                              AND subscription_id = %s
                              AND subscription_version = %s
                            """,
                            (
                                event_id,
                                subscription.subscription_id,
                                subscription.subscription_version,
                            ),
                        )
                        generation_row = cursor.fetchone()
                        if generation_row is None:
                            raise EventStoreCorruptionError(
                                "redelivery generation query returned no row"
                            )
                        generation = int(generation_row[0])
                        if generation < 2:
                            raise EventStoreCorruptionError(
                                "redelivery target has no original delivery generation"
                            )
                        inserted = self._insert_delivery(
                            cursor,
                            subscription,
                            event_id=event_id,
                            stream_id=str(stream_id_value),
                            stream_sequence=int(sequence_value),
                            created_at=scheduled_at,
                            delivery_generation=generation,
                        )
                        if not inserted:
                            raise EventStoreCorruptionError(
                                "redelivery delivery generation was not inserted"
                            )
                        delivery_id = _delivery_id(
                            event_id,
                            subscription.subscription_id,
                            subscription.subscription_version,
                            generation,
                        )
                        cursor.execute(
                            """
                            INSERT INTO event_redelivery_items (
                                tenant_id, redelivery_id, event_id, stream_id,
                                stream_sequence, subscription_id,
                                subscription_version, delivery_id,
                                delivery_generation, created_at
                            ) VALUES (
                                %s, %s, %s, %s,
                                %s, %s,
                                %s, %s,
                                %s, %s
                            )
                            """,
                            (
                                request.tenant_id,
                                request.redelivery_id,
                                event_id,
                                str(stream_id_value),
                                int(sequence_value),
                                subscription.subscription_id,
                                subscription.subscription_version,
                                delivery_id,
                                generation,
                                scheduled_at,
                            ),
                        )

                    cursor.execute(
                        f"""
                        SELECT {_redelivery_report_columns()}
                        FROM event_redelivery_reports
                        WHERE tenant_scope = %s AND redelivery_id = %s
                        """,
                        (tenant_scope, request.redelivery_id),
                    )
                    report_row = cursor.fetchone()
                    if report_row is None:
                        raise EventStoreCorruptionError(
                            "redelivery report disappeared before transaction commit"
                        )
                    report = _redelivery_report_from_postgres(cursor, report_row)
                connection.commit()
                return report
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def get_redelivery_report(
        self,
        redelivery_id: str,
        *,
        tenant_id: str | None = None,
    ) -> RedeliveryReport | None:
        normalized_id = _required_text(redelivery_id, "redelivery_id")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(
                        f"""
                        SELECT {_redelivery_report_columns()}
                        FROM event_redelivery_reports
                        WHERE tenant_scope = %s AND redelivery_id = %s
                        """,
                        (_tenant_scope(tenant_id), normalized_id),
                    )
                    row = cursor.fetchone()
                    report = (
                        None
                        if row is None
                        else _redelivery_report_from_postgres(cursor, row)
                    )
                connection.commit()
                return report
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def save_quarantine(self, record: QuarantineRecord) -> QuarantineRecord:
        if not isinstance(record, QuarantineRecord):
            raise ValueError("record must be QuarantineRecord")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"quarantine:{record.quarantine_id}",),
                    )
                    cursor.execute(
                        f"""
                        SELECT {_quarantine_columns()}
                        FROM event_quarantine
                        WHERE quarantine_id = %s
                        FOR UPDATE
                        """,
                        (record.quarantine_id,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        existing = _quarantine_from_row(existing_row)
                        if existing != record:
                            raise EventStoreError(
                                "quarantine identity already has different content"
                            )
                        connection.commit()
                        return existing
                    cursor.execute(
                        f"""
                        INSERT INTO event_quarantine (
                            quarantine_id, tenant_id, source, reason,
                            envelope_schema, event_type, data_schema,
                            redacted_diagnostic, disposition, operator_id,
                            operator_reason, created_at, updated_at
                        )
                        VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s
                        )
                        RETURNING {_quarantine_columns()}
                        """,
                        (
                            record.quarantine_id,
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
                            record.created_at,
                            record.updated_at,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise EventStoreCorruptionError(
                            "quarantine insert returned no durable row"
                        )
                connection.commit()
                return _quarantine_from_row(row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def get_quarantine(
        self,
        quarantine_id: str,
        *,
        tenant_id: str | None = None,
    ) -> QuarantineRecord | None:
        row = self._fetch_one(
            f"""
            SELECT {_quarantine_columns()}
            FROM event_quarantine
            WHERE quarantine_id = %s AND tenant_scope = %s
            """,
            (
                _required_text(quarantine_id, "quarantine_id"),
                _tenant_scope(tenant_id),
            ),
        )
        return _quarantine_from_row(row) if row is not None else None

    def list_quarantine(self, query: QuarantineQuery) -> QuarantinePage:
        if not isinstance(query, QuarantineQuery):
            raise ValueError("query must be QuarantineQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.reason is not None:
            where.append("reason = %s")
            params.append(query.reason.value)
        if query.disposition is not None:
            where.append("disposition = %s")
            params.append(query.disposition.value)
        if query.cursor is not None:
            cursor_time, cursor_id = _decode_cursor(query.cursor, 2)
            where.append("(created_at, quarantine_id) > (%s, %s)")
            params.extend((_parse_time(cursor_time), cursor_id))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_quarantine_columns()}
            FROM event_quarantine
            WHERE {' AND '.join(where)}
            ORDER BY created_at, quarantine_id
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        records = tuple(_quarantine_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and records:
            last = records[-1]
            next_cursor = _encode_cursor(
                (_format_time(last.created_at), last.quarantine_id)
            )
        return QuarantinePage(records=records, next_cursor=next_cursor)

    def resolve_quarantine(
        self,
        quarantine_id: str,
        disposition: QuarantineDisposition,
        *,
        operator_id: str,
        reason: str,
        resolved_at: datetime,
    ) -> QuarantineRecord:
        disposition = QuarantineDisposition(disposition)
        if disposition is QuarantineDisposition.PENDING:
            raise ValueError("quarantine resolution must be terminal")
        quarantine_id = _required_text(quarantine_id, "quarantine_id")
        operator_id = _required_text(operator_id, "operator_id")
        reason = _required_text(reason, "reason")
        resolved_at = _required_utc(resolved_at, "resolved_at")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        UPDATE event_quarantine
                        SET disposition = %s, operator_id = %s,
                            operator_reason = %s, updated_at = %s
                        WHERE quarantine_id = %s AND disposition = 'pending'
                        RETURNING {_quarantine_columns()}
                        """,
                        (
                            disposition.value,
                            operator_id,
                            reason,
                            resolved_at,
                            quarantine_id,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise EventStoreError(
                            "quarantine record does not exist or is already resolved"
                        )
                connection.commit()
                return _quarantine_from_row(row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def begin_replay(self, request: ReplayStartRequest) -> ReplayReport:
        if not isinstance(request, ReplayStartRequest):
            raise ValueError("request must be ReplayStartRequest")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"replay:{request.replay_id}",),
                    )
                    cursor.execute(
                        f"""
                        SELECT {_replay_columns()}
                        FROM event_replay_reports
                        WHERE replay_id = %s
                        FOR UPDATE
                        """,
                        (request.replay_id,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is not None:
                        existing = _replay_from_row(existing_row)
                        if _replay_request_identity(existing) != _replay_request_identity(
                            request
                        ):
                            raise EventStoreError(
                                "replay identity already has a different request"
                            )
                        connection.commit()
                        return existing
                    cursor.execute(
                        """
                        SELECT last_sequence
                        FROM event_stream_sequences
                        WHERE tenant_scope = %s AND stream_id = %s
                        FOR SHARE
                        """,
                        (
                            _tenant_scope(request.tenant_id),
                            request.source_stream_id,
                        ),
                    )
                    stream_row = cursor.fetchone()
                    if stream_row is None:
                        raise EventStoreError("replay source stream does not exist")
                    high_watermark = int(stream_row[0])
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
                    cursor.execute(
                        f"""
                        INSERT INTO event_replay_reports (
                            replay_id, tenant_id, mode, source_stream_id,
                            high_watermark, status, from_sequence, to_sequence,
                            checkpoint_ref, versions, applied_upcasters,
                            quarantine_refs, mismatch_sequence, reason_class,
                            result_checksum, started_at, finished_at,
                            operator_id, operator_reason
                        )
                        VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s::jsonb, %s::jsonb,
                            %s::jsonb, %s, %s,
                            %s, %s, %s,
                            %s, %s
                        )
                        RETURNING {_replay_columns()}
                        """,
                        _replay_params(report),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise EventStoreCorruptionError(
                            "replay report insert returned no durable row"
                        )
                connection.commit()
                return _replay_from_row(row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def update_replay_report(self, report: ReplayReport) -> ReplayReport:
        if not isinstance(report, ReplayReport):
            raise ValueError("report must be ReplayReport")
        with self._connection() as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        SELECT {_replay_columns()}
                        FROM event_replay_reports
                        WHERE replay_id = %s
                        FOR UPDATE
                        """,
                        (report.replay_id,),
                    )
                    existing_row = cursor.fetchone()
                    if existing_row is None:
                        raise EventStoreError("replay report does not exist")
                    existing = _replay_from_row(existing_row)
                    if _replay_immutable_identity(existing) != _replay_immutable_identity(
                        report
                    ):
                        raise EventStoreError(
                            "replay update attempted to change immutable source identity"
                        )
                    if existing.status in {
                        ReplayStatus.SUCCEEDED,
                        ReplayStatus.FAILED,
                    }:
                        if existing == report:
                            connection.commit()
                            return existing
                        raise EventStoreError("terminal replay report is immutable")
                    allowed = {
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
                    }
                    if report.status not in allowed[existing.status]:
                        raise EventStoreError("invalid replay status transition")
                    cursor.execute(
                        f"""
                        UPDATE event_replay_reports
                        SET status = %s, from_sequence = %s, to_sequence = %s,
                            checkpoint_ref = %s, versions = %s::jsonb,
                            applied_upcasters = %s::jsonb,
                            quarantine_refs = %s::jsonb,
                            mismatch_sequence = %s, reason_class = %s,
                            result_checksum = %s, finished_at = %s
                        WHERE replay_id = %s
                        RETURNING {_replay_columns()}
                        """,
                        (
                            report.status.value,
                            report.from_sequence,
                            report.to_sequence,
                            report.checkpoint_ref,
                            _json(
                                [
                                    {"component": item.component, "version": item.version}
                                    for item in report.versions
                                ]
                            ),
                            _json(list(report.applied_upcasters)),
                            _json(list(report.quarantine_refs)),
                            report.mismatch_sequence,
                            report.reason_class,
                            report.result_checksum,
                            report.finished_at,
                            report.replay_id,
                        ),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise EventStoreCorruptionError(
                            "replay report disappeared during update"
                        )
                connection.commit()
                return _replay_from_row(row)
            except BaseException as exc:
                connection.rollback()
                _reraise_store_exception(exc)

    def get_replay_report(
        self,
        replay_id: str,
        *,
        tenant_id: str | None = None,
    ) -> ReplayReport | None:
        row = self._fetch_one(
            f"""
            SELECT {_replay_columns()}
            FROM event_replay_reports
            WHERE replay_id = %s AND tenant_scope = %s
            """,
            (
                _required_text(replay_id, "replay_id"),
                _tenant_scope(tenant_id),
            ),
        )
        return _replay_from_row(row) if row is not None else None

    def list_replay_reports(self, query: ReplayReportQuery) -> ReplayReportPage:
        if not isinstance(query, ReplayReportQuery):
            raise ValueError("query must be ReplayReportQuery")
        where = ["tenant_scope = %s"]
        params: list[Any] = [_tenant_scope(query.tenant_id)]
        if query.source_stream_id is not None:
            where.append("source_stream_id = %s")
            params.append(query.source_stream_id)
        if query.mode is not None:
            where.append("mode = %s")
            params.append(query.mode.value)
        if query.status is not None:
            where.append("status = %s")
            params.append(query.status.value)
        if query.cursor is not None:
            cursor_time, cursor_id = _decode_cursor(query.cursor, 2)
            where.append("(started_at, replay_id) > (%s, %s)")
            params.extend((_parse_time(cursor_time), cursor_id))
        params.append(query.limit + 1)
        rows = self._fetch_all(
            f"""
            SELECT {_replay_columns()}
            FROM event_replay_reports
            WHERE {' AND '.join(where)}
            ORDER BY started_at, replay_id
            LIMIT %s
            """,
            tuple(params),
        )
        has_more = len(rows) > query.limit
        reports = tuple(_replay_from_row(row) for row in rows[: query.limit])
        next_cursor = None
        if has_more and reports:
            last = reports[-1]
            next_cursor = _encode_cursor(
                (_format_time(last.started_at), last.replay_id)
            )
        return ReplayReportPage(reports=reports, next_cursor=next_cursor)

    def _settle_delivery_in_transaction(
        self,
        connection: Any,
        settlement: DeliverySettlement,
        *,
        lock_scope: _PostgresTransactionLockScope | None = None,
    ) -> DeliverySettlementResult:
        if not isinstance(settlement, DeliverySettlement):
            raise ValueError("settlement must be DeliverySettlement")
        lease = settlement.lease
        with connection.cursor() as cursor:
            # Read immutable ownership first, then take the capacity fence and
            # finally the delivery row lock.  Claim, append, registration, and
            # requeue use the same capacity-before-delivery order.
            cursor.execute(
                """
                SELECT subscription_id, subscription_version
                FROM event_deliveries
                WHERE delivery_id = %s
                """,
                (lease.delivery_id,),
            )
            identity_row = cursor.fetchone()
            if identity_row is None:
                raise EventStaleLeaseError(
                    "delivery lease no longer identifies a row"
                )
            subscription_id = str(identity_row[0])
            subscription_version = int(identity_row[1])
            cursor.execute(
                f"""
                SELECT {_subscription_columns()}
                FROM event_subscriptions
                WHERE subscription_id = %s AND subscription_version = %s
                """,
                (subscription_id, subscription_version),
            )
            subscription_row = cursor.fetchone()
            if subscription_row is None:
                raise EventStoreCorruptionError(
                    "delivery references a missing durable subscription"
                )
            subscription = _subscription_from_row(subscription_row)
            _lock_subscription_delivery_capacity(
                cursor,
                subscription,
                lock_scope=lock_scope,
            )
            _lock_delivery_row(
                cursor,
                lease.delivery_id,
                lock_scope=lock_scope,
            )
            cursor.execute(
                f"""
                SELECT {_delivery_columns()}
                FROM event_deliveries
                WHERE delivery_id = %s
                FOR UPDATE
                """,
                (lease.delivery_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise EventStaleLeaseError("delivery lease no longer identifies a row")
            current = _delivery_from_row(row)
            if (
                current.subscription_id != subscription.subscription_id
                or current.subscription_version != subscription.subscription_version
            ):
                raise EventStoreCorruptionError(
                    "delivery subscription identity changed during settlement"
                )
            database_now = _database_now(cursor)
            if (
                current.state is not DeliveryState.CLAIMED
                or current.delivery_generation != lease.delivery_generation
                or current.lease_owner != lease.lease_owner
                or current.lease_generation != lease.lease_generation
                or current.lease_expires_at != lease.lease_expires_at
                or lease.lease_expires_at <= database_now
            ):
                raise EventStaleLeaseError(
                    "delivery settlement was rejected by the lease fence"
                )

            from framework.events.runtime.models import EffectIdempotencyStrategy

            if (
                settlement.target_state is DeliveryState.ACKED
                and subscription.effect.idempotency_strategy
                is EffectIdempotencyStrategy.INBOX_TRANSACTION
                and settlement.inbox_entry is None
            ):
                raise EventConsumerIdempotencyError(
                    "INBOX_TRANSACTION acknowledgement requires an inbox entry"
                )

            inbox_recorded = False
            if settlement.inbox_entry is not None:
                inbox = settlement.inbox_entry
                if current.consumer_effect_id is None:
                    raise EventConsumerIdempotencyError(
                        "delivery has no consumer_effect_id for inbox settlement"
                    )
                if (
                    inbox.event_id != current.event_id
                    or inbox.consumer_effect_id != current.consumer_effect_id
                    or (
                        inbox.delivery_id is not None
                        and inbox.delivery_id != current.delivery_id
                    )
                ):
                    raise EventConsumerIdempotencyError(
                        "inbox entry does not match the claimed consumer effect"
                    )
                _lock_inbox_row(
                    cursor,
                    inbox.event_id,
                    inbox.consumer_effect_id,
                    lock_scope=lock_scope,
                )
                cursor.execute(
                    """
                    INSERT INTO event_inbox (
                        event_id, consumer_effect_id, tenant_id,
                        completed_at, delivery_id, result_checksum
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id, consumer_effect_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        inbox.event_id,
                        inbox.consumer_effect_id,
                        current.tenant_id,
                        inbox.completed_at,
                        inbox.delivery_id or current.delivery_id,
                        inbox.result_checksum,
                    ),
                )
                inbox_recorded = cursor.fetchone() is not None
                if not inbox_recorded:
                    cursor.execute(
                        """
                        SELECT event_id, consumer_effect_id, completed_at,
                               delivery_id, result_checksum, tenant_scope
                        FROM event_inbox
                        WHERE event_id = %s AND consumer_effect_id = %s
                        """,
                        (inbox.event_id, inbox.consumer_effect_id),
                    )
                    existing_inbox = cursor.fetchone()
                    if (
                        existing_inbox is None
                        or str(existing_inbox[0]) != inbox.event_id
                        or str(existing_inbox[1]) != inbox.consumer_effect_id
                        or str(existing_inbox[5]) != _tenant_scope(current.tenant_id)
                    ):
                        raise EventStoreCorruptionError(
                            "inbox uniqueness conflict cannot be resolved safely"
                        )
                    existing_delivery_id = (
                        str(existing_inbox[3])
                        if existing_inbox[3] is not None
                        else None
                    )
                    existing_checksum = (
                        str(existing_inbox[4])
                        if existing_inbox[4] is not None
                        else None
                    )
                    if existing_checksum != inbox.result_checksum:
                        raise EventConsumerIdempotencyError(
                            "inbox identity was already completed with a different result"
                        )
                    if existing_delivery_id is None:
                        raise EventStoreCorruptionError(
                            "stored inbox entry is missing its completing delivery"
                        )

            target_state = settlement.target_state
            if (
                target_state is DeliveryState.RETRY_WAIT
                and not subscription.retry_policy.can_retry(current.attempt_count)
            ):
                target_state = DeliveryState.DEAD_LETTER
            failure_target = target_state in {
                DeliveryState.RETRY_WAIT,
                DeliveryState.DEAD_LETTER,
            }
            first_failure_at = current.first_failure_at
            last_failure_at = current.last_failure_at
            if failure_target:
                failure_occurrence = max(
                    current.updated_at,
                    min(settlement.settled_at, database_now),
                )
                first_failure_at = first_failure_at or failure_occurrence
                last_failure_at = (
                    failure_occurrence
                    if last_failure_at is None
                    else max(last_failure_at, failure_occurrence)
                )
            delivery_updated_at = max(current.updated_at, database_now)
            reason_class = settlement.reason_class or current.reason_class
            redacted_diagnostic = (
                settlement.redacted_diagnostic
                if settlement.redacted_diagnostic is not None
                else current.redacted_diagnostic
            )
            cursor.execute(
                f"""
                UPDATE event_deliveries
                SET state = %s,
                    available_at = %s,
                    lease_owner = NULL,
                    lease_generation = NULL,
                    lease_expires_at = NULL,
                    first_failure_at = %s,
                    last_failure_at = %s,
                    reason_class = %s,
                    redacted_diagnostic = %s,
                    updated_at = %s
                WHERE delivery_id = %s
                RETURNING {_delivery_columns()}
                """,
                (
                    target_state.value,
                    (
                        settlement.retry_available_at
                        if target_state is DeliveryState.RETRY_WAIT
                        else None
                    ),
                    first_failure_at,
                    last_failure_at,
                    reason_class,
                    redacted_diagnostic,
                    delivery_updated_at,
                    current.delivery_id,
                ),
            )
            settled_row = cursor.fetchone()
            if settled_row is None:
                raise EventStoreCorruptionError(
                    "claimed delivery disappeared during settlement"
                )
            settled = _delivery_from_row(settled_row)

            dead_letter_id = None
            if target_state is DeliveryState.DEAD_LETTER:
                dead_letter_id = _dead_letter_id(
                    current.delivery_id,
                    current.delivery_generation,
                )
                cursor.execute(
                    """
                    INSERT INTO event_dead_letters (
                        dead_letter_id, delivery_id, event_id, tenant_id,
                        stream_id, stream_sequence, subscription_id,
                        subscription_version, consumer_id, consumer_effect_id,
                        delivery_generation, attempt_count, first_failure_at,
                        last_failure_at, reason_class, redacted_diagnostic
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s
                    )
                    """,
                    (
                        dead_letter_id,
                        settled.delivery_id,
                        settled.event_id,
                        settled.tenant_id,
                        settled.stream_id,
                        settled.stream_sequence,
                        settled.subscription_id,
                        settled.subscription_version,
                        settled.consumer_id,
                        settled.consumer_effect_id,
                        settled.delivery_generation,
                        settled.attempt_count,
                        settled.first_failure_at,
                        settled.last_failure_at,
                        settled.reason_class,
                        settled.redacted_diagnostic,
                    ),
                )

            checkpoint = None
            if settled.state.is_terminal and settled.delivery_generation == 1:
                checkpoint = self._advance_checkpoint(
                    cursor,
                    settled,
                    updated_at=delivery_updated_at,
                )
            return DeliverySettlementResult(
                delivery=settled,
                checkpoint=checkpoint,
                dead_letter_id=dead_letter_id,
                inbox_recorded=inbox_recorded,
            )

    def _advance_checkpoint(
        self,
        cursor: Any,
        settled: DeliveryRecord,
        *,
        updated_at: datetime,
    ) -> ConsumerCheckpoint | None:
        tenant_scope = _tenant_scope(settled.tenant_id)
        checkpoint_params = (
            tenant_scope,
            settled.subscription_id,
            settled.subscription_version,
            settled.stream_id,
        )
        cursor.execute(
            """
            SELECT start_sequence
            FROM event_subscription_stream_states
            WHERE tenant_scope = %s
              AND subscription_id = %s
              AND subscription_version = %s
              AND stream_id = %s
            FOR SHARE
            """,
            checkpoint_params,
        )
        state_row = cursor.fetchone()
        if state_row is None:
            raise EventStoreCorruptionError(
                "delivery checkpoint has no subscription stream state"
            )
        start_sequence = int(state_row[0])
        cursor.execute(
            f"""
            SELECT {_checkpoint_columns()}
            FROM event_consumer_checkpoints
            WHERE tenant_scope = %s
              AND subscription_id = %s
              AND subscription_version = %s
              AND stream_id = %s
            FOR UPDATE
            """,
            checkpoint_params,
        )
        existing_row = cursor.fetchone()
        existing = (
            _checkpoint_from_row(existing_row) if existing_row is not None else None
        )
        scan_sequence = start_sequence
        if existing is not None:
            existing_sequence = existing.highest_contiguous_terminal_sequence
            if existing_sequence is None:
                raise EventStoreCorruptionError(
                    "persisted checkpoint frontier is incomplete"
                )
            scan_sequence = max(start_sequence, existing_sequence + 1)
        cursor.execute(
            f"""
            SELECT {_delivery_columns()}
            FROM event_deliveries
            WHERE tenant_scope = %s
              AND subscription_id = %s
              AND subscription_version = %s
              AND stream_id = %s
              AND delivery_generation = 1
              AND stream_sequence >= %s
            ORDER BY stream_sequence
            FOR SHARE
            """,
            (
                tenant_scope,
                settled.subscription_id,
                settled.subscription_version,
                settled.stream_id,
                scan_sequence,
            ),
        )
        frontier: DeliveryRecord | None = None
        for row in cursor.fetchall():
            delivery = _delivery_from_row(row)
            if not delivery.state.is_terminal:
                break
            frontier = delivery
        if frontier is None:
            return existing

        checksum = _checkpoint_checksum(
            subscription_id=frontier.subscription_id,
            subscription_version=frontier.subscription_version,
            stream_id=frontier.stream_id,
            tenant_id=frontier.tenant_id,
            sequence=frontier.stream_sequence,
            event_id=frontier.event_id,
            disposition=frontier.state,
            updated_at=updated_at,
            checkpoint_version=1,
        )
        cursor.execute(
            f"""
            INSERT INTO event_consumer_checkpoints (
                tenant_id, subscription_id, subscription_version, stream_id,
                highest_contiguous_terminal_sequence, last_event_id,
                terminal_disposition, updated_at, checksum, checkpoint_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT (
                tenant_scope, subscription_id, subscription_version, stream_id
            ) DO NOTHING
            RETURNING {_checkpoint_columns()}
            """,
            (
                frontier.tenant_id,
                frontier.subscription_id,
                frontier.subscription_version,
                frontier.stream_id,
                frontier.stream_sequence,
                frontier.event_id,
                frontier.state.value,
                updated_at,
                checksum,
            ),
        )
        checkpoint_row = cursor.fetchone()
        if checkpoint_row is None:
            cursor.execute(
                f"""
                UPDATE event_consumer_checkpoints
                SET highest_contiguous_terminal_sequence = %s,
                    last_event_id = %s,
                    terminal_disposition = %s,
                    updated_at = %s,
                    checksum = %s,
                    checkpoint_version = 1
                WHERE tenant_scope = %s
                  AND subscription_id = %s
                  AND subscription_version = %s
                  AND stream_id = %s
                  AND highest_contiguous_terminal_sequence < %s
                RETURNING {_checkpoint_columns()}
                """,
                (
                    frontier.stream_sequence,
                    frontier.event_id,
                    frontier.state.value,
                    updated_at,
                    checksum,
                    *checkpoint_params,
                    frontier.stream_sequence,
                ),
            )
            checkpoint_row = cursor.fetchone()
        if checkpoint_row is None:
            raise EventStoreCorruptionError("checkpoint frontier did not advance")
        return _checkpoint_from_row(checkpoint_row)

    def _register_subscription_in_transaction(
        self,
        connection: Any,
        subscription: DurableSubscription,
    ) -> DurableSubscription:
        tenant_scope = _tenant_scope(subscription.tenant_id)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (_subscription_identity_lock_name(subscription),),
            )
            _lock_subscription_registry(
                cursor,
                tenant_scope,
                exclusive=True,
            )
            cursor.execute(
                f"""
                SELECT {_subscription_columns()}
                FROM event_subscriptions
                WHERE subscription_id = %s AND subscription_version = %s
                FOR UPDATE
                """,
                (subscription.subscription_id, subscription.subscription_version),
            )
            existing_row = cursor.fetchone()
            if existing_row is not None:
                existing = _subscription_from_row(existing_row)
                if _subscription_definition(existing) != _subscription_definition(
                    subscription
                ):
                    raise EventStoreError(
                        "subscription identity already has a different definition"
                    )
                return existing

            database_now = _database_now(cursor)
            created_at = subscription.created_at or database_now
            updated_at = subscription.updated_at or created_at
            effect = subscription.effect
            retry = subscription.retry_policy
            lease = subscription.lease_policy
            limits = subscription.limits
            cursor.execute(
                f"""
                INSERT INTO event_subscriptions (
                    subscription_id, subscription_version, tenant_id, consumer_id,
                    event_types, data_schemas, start_policy, start_sequence,
                    performs_external_effects, consumer_effect_id,
                    idempotency_strategy, retry_max_attempts,
                    retry_initial_delay_seconds, retry_multiplier,
                    retry_max_delay_seconds, retry_jitter_ratio,
                    lease_duration_seconds, batch_size, max_in_flight,
                    max_concurrency, pending_warning_threshold,
                    pending_hard_limit, status, supports_out_of_order_repair,
                    created_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                RETURNING {_subscription_columns()}
                """,
                (
                    subscription.subscription_id,
                    subscription.subscription_version,
                    subscription.tenant_id,
                    subscription.consumer_id,
                    _json(sorted(subscription.event_filter.event_types)),
                    _json(sorted(subscription.event_filter.data_schemas)),
                    subscription.start.policy.value,
                    subscription.start.start_sequence,
                    effect.performs_external_effects,
                    effect.consumer_effect_id,
                    (
                        effect.idempotency_strategy.value
                        if effect.idempotency_strategy is not None
                        else None
                    ),
                    retry.max_attempts,
                    retry.initial_delay_seconds,
                    retry.multiplier,
                    retry.max_delay_seconds,
                    retry.jitter_ratio,
                    lease.duration_seconds,
                    limits.batch_size,
                    limits.max_in_flight,
                    limits.max_concurrency,
                    limits.pending_warning_threshold,
                    limits.pending_hard_limit,
                    subscription.status.value,
                    subscription.supports_out_of_order_repair,
                    created_at,
                    updated_at,
                ),
            )
            inserted_row = cursor.fetchone()
            if inserted_row is None:
                raise EventStoreCorruptionError(
                    "subscription insert returned no durable row"
                )

            # Registration can backfill many streams.  Fence the subscription
            # once and apply one aggregate hard limit across the whole
            # registration transaction rather than granting every stream its
            # own copy of the limit.
            _lock_subscription_delivery_capacity(cursor, subscription)

            cursor.execute(
                """
                SELECT stream_id, last_sequence
                FROM event_stream_sequences
                WHERE tenant_scope = %s
                ORDER BY stream_id
                FOR UPDATE
                """,
                (tenant_scope,),
            )
            streams = tuple((str(row[0]), int(row[1])) for row in cursor.fetchall())
            backfilled_count = 0
            for stream_id, registration_watermark in streams:
                start_sequence = _subscription_start_sequence(
                    subscription,
                    registration_watermark,
                )
                if start_sequence > registration_watermark + 1:
                    raise EventSubscriptionPositionError(
                        subscription_id=subscription.subscription_id,
                        subscription_version=subscription.subscription_version,
                        stream_id=stream_id,
                        requested_sequence=start_sequence,
                        maximum_sequence=registration_watermark + 1,
                    )
                self._insert_stream_state(
                    cursor,
                    subscription,
                    stream_id=stream_id,
                    start_sequence=start_sequence,
                    registration_watermark=registration_watermark,
                    created_at=created_at,
                )
                if subscription.status is not SubscriptionStatus.RETIRED:
                    backfilled_count += self._backfill_deliveries(
                        cursor,
                        subscription,
                        stream_id=stream_id,
                        start_sequence=start_sequence,
                        through_sequence=registration_watermark,
                        created_at=database_now,
                        remaining_capacity=(
                            subscription.limits.pending_hard_limit
                            - backfilled_count
                        ),
                    )
            return _subscription_from_row(inserted_row)

    def _insert_stream_state(
        self,
        cursor: Any,
        subscription: DurableSubscription,
        *,
        stream_id: str,
        start_sequence: int,
        registration_watermark: int,
        created_at: datetime,
    ) -> None:
        cursor.execute(
            """
            INSERT INTO event_subscription_stream_states (
                tenant_id, subscription_id, subscription_version,
                stream_id, start_sequence, registration_watermark,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (
                tenant_scope, subscription_id, subscription_version, stream_id
            ) DO NOTHING
            """,
            (
                subscription.tenant_id,
                subscription.subscription_id,
                subscription.subscription_version,
                stream_id,
                start_sequence,
                registration_watermark,
                created_at,
                created_at,
            ),
        )

    def _backfill_deliveries(
        self,
        cursor: Any,
        subscription: DurableSubscription,
        *,
        stream_id: str,
        start_sequence: int,
        through_sequence: int,
        created_at: datetime,
        remaining_capacity: int,
    ) -> int:
        if through_sequence < start_sequence:
            return 0
        if remaining_capacity < 0:
            raise EventStoreCapacityError(
                "subscription backfill exceeds its durable pending hard limit"
            )
        where = [
            "tenant_scope = %s",
            "stream_id = %s",
            "stream_sequence BETWEEN %s AND %s",
        ]
        params: list[Any] = [
            _tenant_scope(subscription.tenant_id),
            stream_id,
            start_sequence,
            through_sequence,
        ]
        if subscription.event_filter.event_types:
            where.append("event_type = ANY(%s)")
            params.append(sorted(subscription.event_filter.event_types))
        if subscription.event_filter.data_schemas:
            where.append("data_schema = ANY(%s)")
            params.append(sorted(subscription.event_filter.data_schemas))
        params.append(remaining_capacity + 1)
        cursor.execute(
            f"""
            SELECT event_id, stream_sequence
            FROM durable_events
            WHERE {' AND '.join(where)}
            ORDER BY stream_sequence, event_id
            LIMIT %s
            """,
            tuple(params),
        )
        matches = tuple((str(row[0]), int(row[1])) for row in cursor.fetchall())
        if len(matches) > remaining_capacity:
            raise EventStoreCapacityError(
                "subscription backfill exceeds its durable pending hard limit"
            )
        inserted_count = 0
        for event_id, stream_sequence in matches:
            inserted_count += int(
                self._insert_delivery(
                    cursor,
                    subscription,
                    event_id=event_id,
                    stream_id=stream_id,
                    stream_sequence=stream_sequence,
                    created_at=created_at,
                )
            )
        return inserted_count

    def _insert_delivery(
        self,
        cursor: Any,
        subscription: DurableSubscription,
        *,
        event_id: str,
        stream_id: str,
        stream_sequence: int,
        created_at: datetime,
        delivery_generation: int = 1,
    ) -> bool:
        delivery_id = _delivery_id(
            event_id,
            subscription.subscription_id,
            subscription.subscription_version,
            delivery_generation,
        )
        cursor.execute(
            """
            INSERT INTO event_deliveries (
                delivery_id, event_id, tenant_id, stream_id, stream_sequence,
                subscription_id, subscription_version, consumer_id,
                consumer_effect_id, delivery_generation, state, attempt_count,
                created_at, updated_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s,
                %s, %s, 'pending', 0,
                %s, %s
            )
            ON CONFLICT (
                event_id, subscription_id, subscription_version, delivery_generation
            ) DO NOTHING
            RETURNING delivery_id
            """,
            (
                delivery_id,
                event_id,
                subscription.tenant_id,
                stream_id,
                stream_sequence,
                subscription.subscription_id,
                subscription.subscription_version,
                subscription.consumer_id,
                subscription.effect.consumer_effect_id,
                delivery_generation,
                created_at,
                created_at,
            ),
        )
        return cursor.fetchone() is not None

    def _append_event_in_transaction(
        self,
        connection: Any,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
        lock_scope: _PostgresTransactionLockScope | None = None,
    ) -> AppendResult:
        if not isinstance(event, EventCandidate):
            raise ValueError("event must be an already projected EventCandidate")
        expected_last_sequence = _expected_last_sequence(expected_last_sequence)
        tenant_scope = _tenant_scope(event.tenant_id)
        with connection.cursor() as cursor:
            # Fast-path an already committed immutable identity before any
            # admission fence.  This keeps same-checksum retries idempotent even
            # when a subscription backlog is already at its hard limit.  A miss
            # is rechecked after the event identity fence below.
            cursor.execute(
                f"""
                SELECT {_EVENT_COLUMNS}
                FROM durable_events
                WHERE event_id = %s
                """,
                (event.event_id,),
            )
            existing_row = cursor.fetchone()
            if existing_row is not None:
                existing = _stored_event_from_row(existing_row)
                if existing.content_checksum != event.content_checksum:
                    raise EventIdentityCollisionError(event.event_id)
                return AppendResult(
                    event=existing,
                    created=False,
                    pending_delivery_count=0,
                )

            # Subscription rows are locked before the stream counter.  The
            # registration path uses the same order, so a concurrent register
            # either backfills this event or becomes visible here without a
            # subscription-row/counter-row deadlock.
            _lock_subscription_registry(
                cursor,
                tenant_scope,
                exclusive=False,
                lock_scope=lock_scope,
            )
            if lock_scope is not None:
                # A later operation in a wider UoW must reject a reverse
                # capacity-key request before it can wait on a subscription
                # row held by another transaction.  The shared registry fence
                # makes this nonlocking identity snapshot stable with respect
                # to registration and retirement; pause/resume does not change
                # materialization membership.
                preview_subscriptions = _matching_subscriptions_for_append(
                    cursor,
                    tenant_scope=tenant_scope,
                    event_type=event.event_type,
                    data_schema=event.data_schema,
                    lock_rows=False,
                )
                _preflight_subscription_delivery_capacities(
                    preview_subscriptions,
                    lock_scope=lock_scope,
                )
            else:
                preview_subscriptions = None

            matching_subscriptions = _matching_subscriptions_for_append(
                cursor,
                tenant_scope=tenant_scope,
                event_type=event.event_type,
                data_schema=event.data_schema,
                lock_rows=True,
            )
            if (
                preview_subscriptions is not None
                and _subscription_capacity_keys(preview_subscriptions)
                != _subscription_capacity_keys(matching_subscriptions)
            ):
                raise EventStoreContentionError(
                    "subscription admission snapshot changed while acquiring "
                    "row locks; retry the entire transaction"
                )
            # The tenant registry snapshot is already fixed.  Fence all
            # matching subscription capacities in canonical order before the
            # stream counter or any delivery row is changed.  A later capacity
            # failure therefore rolls back the event, sequence, and every
            # delivery reservation as one transaction.
            _lock_subscription_delivery_capacities(
                cursor,
                matching_subscriptions,
                lock_scope=lock_scope,
            )

            # Direct single-operation transactions use the fixed
            # registry -> capacity -> event -> stream order.  A wider UoW uses
            # the same logical locks, but newly encountered resources after its
            # first mutation are try-locked so cross-family cycles fail fast.
            _lock_event_identity(
                cursor,
                event.event_id,
                lock_scope=lock_scope,
            )
            cursor.execute(
                f"""
                SELECT {_EVENT_COLUMNS}
                FROM durable_events
                WHERE event_id = %s
                FOR SHARE
                """,
                (event.event_id,),
            )
            existing_row = cursor.fetchone()
            if existing_row is not None:
                existing = _stored_event_from_row(existing_row)
                if existing.content_checksum != event.content_checksum:
                    raise EventIdentityCollisionError(event.event_id)
                return AppendResult(
                    event=existing,
                    created=False,
                    pending_delivery_count=0,
                )

            _lock_stream_sequence(
                cursor,
                tenant_scope=tenant_scope,
                stream_id=event.stream_id,
                lock_scope=lock_scope,
            )

            cursor.execute(
                """
                INSERT INTO event_stream_sequences (tenant_id, stream_id)
                VALUES (%s, %s)
                ON CONFLICT (tenant_scope, stream_id) DO NOTHING
                """,
                (event.tenant_id, event.stream_id),
            )
            if expected_last_sequence is not None:
                cursor.execute(
                    """
                    SELECT last_sequence
                    FROM event_stream_sequences
                    WHERE tenant_scope = %s AND stream_id = %s
                    FOR UPDATE
                    """,
                    (tenant_scope, event.stream_id),
                )
                current_row = cursor.fetchone()
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
            cursor.execute(
                """
                UPDATE event_stream_sequences
                SET last_sequence = last_sequence + 1,
                    updated_at = clock_timestamp()
                WHERE tenant_scope = %s AND stream_id = %s
                RETURNING last_sequence, clock_timestamp()
                """,
                (tenant_scope, event.stream_id),
            )
            allocation = cursor.fetchone()
            if allocation is None:
                raise EventStoreCorruptionError(
                    "stream counter allocation returned no durable row"
                )
            stream_sequence = int(allocation[0])
            observed_at = _required_utc(allocation[1], "observed_at")
            stored = StoredEvent(
                candidate=event,
                observed_at=observed_at,
                stream_sequence=stream_sequence,
            )
            cursor.execute(
                """
                INSERT INTO durable_events (
                    event_id, tenant_id, stream_id, stream_sequence,
                    envelope_schema, event_type, data_schema, source, subject,
                    occurred_at, observed_at, correlation_id, causation_id,
                    business_context, producer, trace_context,
                    security_classification, content_type, payload, payload_ref,
                    extensions, content_checksum, record_checksum
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s
                )
                """,
                (
                    stored.event_id,
                    stored.tenant_id,
                    stored.stream_id,
                    stored.stream_sequence,
                    stored.envelope_schema,
                    stored.event_type,
                    stored.data_schema,
                    stored.source,
                    stored.subject,
                    stored.occurred_at,
                    stored.observed_at,
                    stored.correlation_id,
                    stored.causation_id,
                    _json(stored.business_context.to_dict()),
                    _json(stored.producer.to_dict()),
                    _json(stored.trace.to_dict()) if stored.trace is not None else None,
                    stored.security_classification.value,
                    stored.content_type,
                    (
                        _json(thaw_canonical_json(stored.payload))
                        if stored.payload is not None
                        else None
                    ),
                    _json(stored.payload_ref.to_dict()) if stored.payload_ref else None,
                    _json(thaw_canonical_json(stored.extensions)),
                    stored.content_checksum,
                    stored.record_checksum,
                ),
            )

            pending_count = self._materialize_append_deliveries(
                cursor,
                stored,
                matching_subscriptions,
            )
            return AppendResult(
                event=stored,
                created=True,
                pending_delivery_count=pending_count,
            )

    def _materialize_append_deliveries(
        self,
        cursor: Any,
        event: StoredEvent,
        subscriptions: Sequence[DurableSubscription],
    ) -> int:
        created = 0
        for subscription in subscriptions:
            cursor.execute(
                """
                SELECT start_sequence, retirement_watermark
                FROM event_subscription_stream_states
                WHERE tenant_scope = %s
                  AND subscription_id = %s
                  AND subscription_version = %s
                  AND stream_id = %s
                FOR UPDATE
                """,
                (
                    _tenant_scope(event.tenant_id),
                    subscription.subscription_id,
                    subscription.subscription_version,
                    event.stream_id,
                ),
            )
            state_row = cursor.fetchone()
            if state_row is None:
                start_sequence = _subscription_start_sequence(
                    subscription,
                    event.stream_sequence - 1,
                )
                if start_sequence > event.stream_sequence:
                    continue
                self._insert_stream_state(
                    cursor,
                    subscription,
                    stream_id=event.stream_id,
                    start_sequence=start_sequence,
                    registration_watermark=event.stream_sequence - 1,
                    created_at=event.observed_at,
                )
                retirement_watermark = None
            else:
                start_sequence = int(state_row[0])
                retirement_watermark = (
                    int(state_row[1]) if state_row[1] is not None else None
                )
            if event.stream_sequence < start_sequence:
                continue
            if (
                retirement_watermark is not None
                and event.stream_sequence > retirement_watermark
            ):
                continue
            if _subscription_pending_capacity_exhausted(cursor, subscription):
                raise EventStoreCapacityError(
                    "subscription durable pending hard limit is exhausted"
                )
            if self._insert_delivery(
                cursor,
                subscription,
                event_id=event.event_id,
                stream_id=event.stream_id,
                stream_sequence=event.stream_sequence,
                created_at=event.observed_at,
            ):
                created += 1
        return created

    def _load_transactional_state_in_transaction(
        self,
        connection: Any,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        normalized_namespace = _required_text(namespace, "namespace")
        normalized_key = _required_text(key, "key")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (
                    _transactional_state_lock_name(
                        normalized_namespace,
                        normalized_key,
                    ),
                ),
            )
            cursor.execute(
                """
                SELECT namespace, state_key, revision, checksum, payload
                FROM event_transactional_states
                WHERE namespace = %s AND state_key = %s
                FOR UPDATE
                """,
                (normalized_namespace, normalized_key),
            )
            row = cursor.fetchone()
        return _transactional_state_from_row(row) if row is not None else None

    def _cas_transactional_state_in_transaction(
        self,
        connection: Any,
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

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (
                    _transactional_state_lock_name(
                        normalized_namespace,
                        normalized_key,
                    ),
                ),
            )
            cursor.execute(
                """
                SELECT namespace, state_key, revision, checksum, payload
                FROM event_transactional_states
                WHERE namespace = %s AND state_key = %s
                FOR UPDATE
                """,
                (normalized_namespace, normalized_key),
            )
            row = cursor.fetchone()
            current = _transactional_state_from_row(row) if row is not None else None
            if current == next_snapshot:
                return current
            if current is None:
                if expected_revision is not None or next_snapshot.revision != 1:
                    raise EventStoreContentionError(
                        "transactional state creation requires empty expected "
                        "state and revision 1"
                    )
                cursor.execute(
                    """
                    INSERT INTO event_transactional_states (
                        namespace, state_key, revision, checksum, payload
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        next_snapshot.namespace,
                        next_snapshot.key,
                        next_snapshot.revision,
                        next_snapshot.checksum,
                        _json(thaw_canonical_json(next_snapshot.payload)),
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
            cursor.execute(
                """
                UPDATE event_transactional_states
                SET revision = %s, checksum = %s, payload = %s::jsonb
                WHERE namespace = %s
                  AND state_key = %s
                  AND revision = %s
                  AND checksum = %s
                """,
                (
                    next_snapshot.revision,
                    next_snapshot.checksum,
                    _json(thaw_canonical_json(next_snapshot.payload)),
                    normalized_namespace,
                    normalized_key,
                    current.revision,
                    current.checksum,
                ),
            )
            if cursor.rowcount != 1:
                raise EventStoreContentionError(
                    "transactional state changed during compare-and-swap"
                )
        return next_snapshot

    def _fetch_one(self, sql: str, params: tuple[Any, ...]) -> tuple[Any, ...] | None:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return cursor.fetchone()
        except BaseException as exc:
            _reraise_store_exception(exc)

    def _fetch_all(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        try:
            with self._connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(sql, params)
                    return list(cursor.fetchall())
        except BaseException as exc:
            _reraise_store_exception(exc)

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        connection = self._acquire_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _acquire_connection(self) -> Any:
        connection: Any | None = None
        try:
            connection = self._connection_factory()
            if getattr(connection, "autocommit", False):
                connection.autocommit = False
            return connection
        except (
            psycopg.OperationalError,
            psycopg.InterfaceError,
            PoolClosed,
            PoolTimeout,
        ) as exc:
            if connection is not None:
                _close_connection_after_acquire_failure(connection)
            raise EventStoreUnavailableError(
                "PostgreSQL durable event store is unavailable"
            ) from exc
        except BaseException:
            if connection is not None:
                _close_connection_after_acquire_failure(connection)
            raise


class PostgresEventUnitOfWork:
    """Explicit transaction; an uncommitted context always rolls back."""

    def __init__(self, store: PostgresDurableEventStore) -> None:
        self._store = store
        self._connection: Any | None = None
        self._finished = False
        self._transaction_locks = _PostgresTransactionLockTracker()
        self._rollback_only = False

    @property
    def connection(self) -> Any:
        """Expose the same connection for an explicit business-state UoW."""

        return self._active_connection()

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        connection = self._active_connection()
        self._require_committable()
        lock_scope = self._transaction_locks.new_operation()
        try:
            return self._store._append_event_in_transaction(
                connection,
                event,
                expected_last_sequence=expected_last_sequence,
                lock_scope=lock_scope,
            )
        except BaseException as exc:
            self._rollback_only = True
            _reraise_store_exception(exc)

    def settle_delivery(
        self,
        settlement: DeliverySettlement,
    ) -> DeliverySettlementResult:
        connection = self._active_connection()
        self._require_committable()
        lock_scope = self._transaction_locks.new_operation()
        try:
            return self._store._settle_delivery_in_transaction(
                connection,
                settlement,
                lock_scope=lock_scope,
            )
        except BaseException as exc:
            self._rollback_only = True
            _reraise_store_exception(exc)

    def load_transactional_state(
        self,
        namespace: str,
        key: str,
    ) -> TransactionalStateSnapshot | None:
        connection = self._active_connection()
        self._require_committable()
        try:
            return self._store._load_transactional_state_in_transaction(
                connection,
                namespace,
                key,
            )
        except BaseException as exc:
            self._rollback_only = True
            _reraise_store_exception(exc)

    def cas_transactional_state(
        self,
        *,
        namespace: str,
        key: str,
        expected_revision: int | None,
        expected_checksum: str | None,
        next_snapshot: TransactionalStateSnapshot,
    ) -> TransactionalStateSnapshot:
        connection = self._active_connection()
        self._require_committable()
        try:
            return self._store._cas_transactional_state_in_transaction(
                connection,
                namespace=namespace,
                key=key,
                expected_revision=expected_revision,
                expected_checksum=expected_checksum,
                next_snapshot=next_snapshot,
            )
        except BaseException as exc:
            self._rollback_only = True
            _reraise_store_exception(exc)

    def commit(self) -> None:
        connection = self._active_connection()
        if self._rollback_only:
            try:
                connection.rollback()
            except BaseException as exc:
                try:
                    self._close()
                except BaseException:
                    pass
                _reraise_store_exception(exc)
            self._close()
            raise EventStoreError(
                "event unit of work is rollback-only after a failed mutation"
            )
        try:
            connection.commit()
        except BaseException as exc:
            try:
                connection.rollback()
            except BaseException:
                pass
            finally:
                self._close()
            _reraise_store_exception(exc)
        self._close()

    def rollback(self) -> None:
        if self._finished:
            return
        connection = self._active_connection()
        try:
            connection.rollback()
        except BaseException as exc:
            try:
                self._close()
            except BaseException:
                pass
            _reraise_store_exception(exc)
        finally:
            if not self._finished:
                self._close()

    def __enter__(self) -> PostgresEventUnitOfWork:
        if self._finished:
            raise EventStoreError("event unit of work is already closed")
        if self._connection is not None:
            raise EventStoreError("event unit of work is already entered")
        self._connection = self._store._acquire_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if not self._finished and self._connection is not None:
            if exc is None:
                self.rollback()
            else:
                try:
                    self.rollback()
                except BaseException:
                    # A body failure remains authoritative.  Rollback still
                    # closes the connection, but cleanup failure must not mask
                    # the original application/store exception.
                    pass
        return False

    def _active_connection(self) -> Any:
        if self._finished:
            raise EventStoreError("event unit of work is already closed")
        if self._connection is None:
            raise EventStoreError("event unit of work has not been entered")
        return self._connection

    def _require_committable(self) -> None:
        if self._rollback_only:
            raise EventStoreError(
                "event unit of work is rollback-only after a failed mutation"
            )

    def _close(self) -> None:
        if not self._finished:
            self._finished = True
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()


def _close_connection_after_acquire_failure(connection: Any) -> None:
    try:
        connection.close()
    except BaseException:
        # Preserve the acquisition failure as the public cause.  The connection
        # has already rejected configuration and cannot be used safely.
        pass


def _acquire_shared_pool(key: PoolKey) -> ConnectionPool[Any]:
    with _POOL_REGISTRY_LOCK:
        entry = _POOL_REGISTRY.get(key)
        if entry is not None and not entry[0].closed:
            pool, references = entry
            _POOL_REGISTRY[key] = (pool, references + 1)
            return pool
        dsn, minimum, maximum, timeout = key
        pool = ConnectionPool(
            dsn,
            min_size=minimum,
            max_size=maximum,
            timeout=timeout,
            open=True,
            close_returns=True,
            name="newsroom-durable-events",
        )
        _POOL_REGISTRY[key] = (pool, 1)
        return pool


def _release_shared_pool(key: PoolKey, pool: ConnectionPool[Any]) -> None:
    should_close = False
    with _POOL_REGISTRY_LOCK:
        entry = _POOL_REGISTRY.get(key)
        if entry is None or entry[0] is not pool:
            return
        references = entry[1] - 1
        if references > 0:
            _POOL_REGISTRY[key] = (pool, references)
        else:
            del _POOL_REGISTRY[key]
            should_close = True
    if should_close:
        pool.close()


def _close_all_shared_pools() -> None:
    with _POOL_REGISTRY_LOCK:
        pools = tuple(pool for pool, _references in _POOL_REGISTRY.values())
        _POOL_REGISTRY.clear()
    for pool in pools:
        pool.close()


atexit.register(_close_all_shared_pools)


def _stored_event_from_row(row: Sequence[Any]) -> StoredEvent:
    if len(row) != 23:
        raise EventStoreCorruptionError("durable event row has an invalid shape")
    record = {
        "event_id": row[0],
        "tenant_id": row[1],
        "stream_id": row[2],
        "stream_sequence": row[3],
        "envelope_schema": row[4],
        "event_type": row[5],
        "data_schema": row[6],
        "source": row[7],
        "subject": row[8],
        "occurred_at": _format_time(row[9]),
        "observed_at": _format_time(row[10]),
        "correlation_id": row[11],
        "causation_id": row[12],
        "business_context": _json_object(row[13], "business_context"),
        "producer": _json_object(row[14], "producer"),
        "trace": (
            _json_object(row[15], "trace_context") if row[15] is not None else None
        ),
        "security_classification": row[16],
        "content_type": row[17],
        "payload": _json_object(row[18], "payload") if row[18] is not None else None,
        "payload_ref": (
            _json_object(row[19], "payload_ref") if row[19] is not None else None
        ),
        "extensions": _json_object(row[20], "extensions"),
        "content_checksum": row[21],
        "record_checksum": row[22],
    }
    try:
        event = StoredEvent.from_dict(record, verify_checksum=True)
        event.verify_integrity()
        return event
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "durable event row failed canonical integrity verification"
        ) from exc


def _subscription_columns() -> str:
    return """
        subscription_id, subscription_version, consumer_id,
        event_types, data_schemas, start_policy, start_sequence,
        performs_external_effects, consumer_effect_id, idempotency_strategy,
        retry_max_attempts, retry_initial_delay_seconds, retry_multiplier,
        retry_max_delay_seconds, retry_jitter_ratio, lease_duration_seconds,
        batch_size, max_in_flight, max_concurrency,
        pending_warning_threshold, pending_hard_limit, status,
        supports_out_of_order_repair, tenant_id, created_at, updated_at
    """


def _subscription_from_row(row: Sequence[Any]) -> DurableSubscription:
    if len(row) != 26:
        raise EventStoreCorruptionError("subscription row has an invalid shape")
    from framework.events.runtime.models import (
        ConsumerEffectContract,
        DeliveryLimits,
        EffectIdempotencyStrategy,
        LeasePolicy,
        RetryPolicy,
        SubscriptionFilter,
        SubscriptionStart,
    )

    try:
        strategy = (
            EffectIdempotencyStrategy(str(row[9])) if row[9] is not None else None
        )
        return DurableSubscription(
            subscription_id=str(row[0]),
            subscription_version=int(row[1]),
            consumer_id=str(row[2]),
            event_filter=SubscriptionFilter(
                event_types=frozenset(_json_array(row[3], "event_types")),
                data_schemas=frozenset(_json_array(row[4], "data_schemas")),
            ),
            start=SubscriptionStart(
                policy=str(row[5]),
                start_sequence=int(row[6]) if row[6] is not None else None,
            ),
            effect=ConsumerEffectContract(
                performs_external_effects=bool(row[7]),
                consumer_effect_id=str(row[8]) if row[8] is not None else None,
                idempotency_strategy=strategy,
            ),
            retry_policy=RetryPolicy(
                max_attempts=int(row[10]),
                initial_delay_seconds=float(row[11]),
                multiplier=float(row[12]),
                max_delay_seconds=float(row[13]),
                jitter_ratio=float(row[14]),
            ),
            lease_policy=LeasePolicy(duration_seconds=float(row[15])),
            limits=DeliveryLimits(
                batch_size=int(row[16]),
                max_in_flight=int(row[17]),
                max_concurrency=int(row[18]),
                pending_warning_threshold=int(row[19]),
                pending_hard_limit=int(row[20]),
            ),
            status=str(row[21]),
            supports_out_of_order_repair=bool(row[22]),
            tenant_id=str(row[23]) if row[23] is not None else None,
            created_at=_required_utc(row[24], "created_at"),
            updated_at=_required_utc(row[25], "updated_at"),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "subscription row failed contract validation"
        ) from exc


def _stream_state_columns() -> str:
    return """
        subscription_id, subscription_version, stream_id, start_sequence,
        registration_watermark, tenant_id, retirement_watermark,
        created_at, updated_at
    """


def _stream_state_from_row(row: Sequence[Any]) -> SubscriptionStreamState:
    if len(row) != 9:
        raise EventStoreCorruptionError("subscription stream state has an invalid shape")
    try:
        return SubscriptionStreamState(
            subscription_id=str(row[0]),
            subscription_version=int(row[1]),
            stream_id=str(row[2]),
            start_sequence=int(row[3]),
            registration_watermark=int(row[4]),
            tenant_id=str(row[5]) if row[5] is not None else None,
            retirement_watermark=int(row[6]) if row[6] is not None else None,
            created_at=_required_utc(row[7], "created_at"),
            updated_at=_required_utc(row[8], "updated_at"),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "subscription stream state failed contract validation"
        ) from exc


_DELIVERY_COLUMN_NAMES = (
    "delivery_id",
    "event_id",
    "stream_id",
    "stream_sequence",
    "subscription_id",
    "subscription_version",
    "consumer_id",
    "consumer_effect_id",
    "tenant_id",
    "delivery_generation",
    "state",
    "attempt_count",
    "available_at",
    "lease_owner",
    "lease_generation",
    "lease_expires_at",
    "first_failure_at",
    "last_failure_at",
    "reason_class",
    "redacted_diagnostic",
    "created_at",
    "updated_at",
)


def _delivery_columns(table_alias: str | None = None) -> str:
    prefix = f"{table_alias}." if table_alias is not None else ""
    return ", ".join(f"{prefix}{column}" for column in _DELIVERY_COLUMN_NAMES)


def _delivery_from_row(row: Sequence[Any]) -> DeliveryRecord:
    if len(row) != 22:
        raise EventStoreCorruptionError("delivery row has an invalid shape")
    try:
        return DeliveryRecord(
            delivery_id=str(row[0]),
            event_id=str(row[1]),
            stream_id=str(row[2]),
            stream_sequence=int(row[3]),
            subscription_id=str(row[4]),
            subscription_version=int(row[5]),
            consumer_id=str(row[6]),
            consumer_effect_id=str(row[7]) if row[7] is not None else None,
            tenant_id=str(row[8]) if row[8] is not None else None,
            delivery_generation=int(row[9]),
            state=str(row[10]),
            attempt_count=int(row[11]),
            available_at=(
                _required_utc(row[12], "available_at")
                if row[12] is not None
                else None
            ),
            lease_owner=str(row[13]) if row[13] is not None else None,
            lease_generation=int(row[14]) if row[14] is not None else None,
            lease_expires_at=(
                _required_utc(row[15], "lease_expires_at")
                if row[15] is not None
                else None
            ),
            first_failure_at=(
                _required_utc(row[16], "first_failure_at")
                if row[16] is not None
                else None
            ),
            last_failure_at=(
                _required_utc(row[17], "last_failure_at")
                if row[17] is not None
                else None
            ),
            reason_class=str(row[18]) if row[18] is not None else None,
            redacted_diagnostic=str(row[19]) if row[19] is not None else None,
            created_at=_required_utc(row[20], "created_at"),
            updated_at=_required_utc(row[21], "updated_at"),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "delivery row failed contract validation"
        ) from exc


def _inbox_from_row(row: Sequence[Any]) -> InboxEntry:
    if len(row) != 5:
        raise EventStoreCorruptionError("inbox row has an invalid shape")
    try:
        return InboxEntry(
            event_id=str(row[0]),
            consumer_effect_id=str(row[1]),
            completed_at=_required_utc(row[2], "completed_at"),
            delivery_id=str(row[3]) if row[3] is not None else None,
            result_checksum=str(row[4]) if row[4] is not None else None,
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError("inbox row failed contract validation") from exc


def _checkpoint_columns() -> str:
    return """
        subscription_id, subscription_version, stream_id,
        highest_contiguous_terminal_sequence, last_event_id,
        terminal_disposition, updated_at, checksum,
        checkpoint_version, tenant_id
    """


def _checkpoint_from_row(row: Sequence[Any]) -> ConsumerCheckpoint:
    if len(row) != 10:
        raise EventStoreCorruptionError("checkpoint row has an invalid shape")
    try:
        checkpoint = ConsumerCheckpoint(
            subscription_id=str(row[0]),
            subscription_version=int(row[1]),
            stream_id=str(row[2]),
            highest_contiguous_terminal_sequence=(
                int(row[3]) if row[3] is not None else None
            ),
            last_event_id=str(row[4]) if row[4] is not None else None,
            terminal_disposition=str(row[5]) if row[5] is not None else None,
            updated_at=_required_utc(row[6], "updated_at"),
            checksum=str(row[7]),
            checkpoint_version=int(row[8]),
            tenant_id=str(row[9]) if row[9] is not None else None,
        )
        expected = _checkpoint_checksum(
            subscription_id=checkpoint.subscription_id,
            subscription_version=checkpoint.subscription_version,
            stream_id=checkpoint.stream_id,
            tenant_id=checkpoint.tenant_id,
            sequence=checkpoint.highest_contiguous_terminal_sequence,
            event_id=checkpoint.last_event_id,
            disposition=checkpoint.terminal_disposition,
            updated_at=checkpoint.updated_at,
            checkpoint_version=checkpoint.checkpoint_version,
        )
        if checkpoint.checksum != expected:
            raise EventStoreCorruptionError(
                "consumer checkpoint checksum verification failed"
            )
        return checkpoint
    except EventStoreCorruptionError:
        raise
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "checkpoint row failed contract validation"
        ) from exc


def _checkpoint_checksum(
    *,
    subscription_id: str,
    subscription_version: int,
    stream_id: str,
    tenant_id: str | None,
    sequence: int | None,
    event_id: str | None,
    disposition: DeliveryState | None,
    updated_at: datetime,
    checkpoint_version: int,
) -> str:
    return checksum_for(
        {
            "subscription_id": subscription_id,
            "subscription_version": subscription_version,
            "stream_id": stream_id,
            "tenant_id": tenant_id,
            "highest_contiguous_terminal_sequence": sequence,
            "last_event_id": event_id,
            "terminal_disposition": disposition.value if disposition else None,
            "updated_at": _format_time(updated_at),
            "checkpoint_version": checkpoint_version,
        }
    )


def _dead_letter_columns() -> str:
    return """
        dead_letter_id, delivery_id, event_id, stream_id, stream_sequence,
        subscription_id, subscription_version, consumer_id,
        consumer_effect_id, delivery_generation, attempt_count,
        first_failure_at, last_failure_at, reason_class,
        redacted_diagnostic, tenant_id, disposition,
        operator_id, operator_reason, updated_at
    """


def _dead_letter_from_row(row: Sequence[Any]) -> DeadLetterRecord:
    if len(row) != 20:
        raise EventStoreCorruptionError("dead-letter row has an invalid shape")
    try:
        return DeadLetterRecord(
            dead_letter_id=str(row[0]),
            delivery_id=str(row[1]),
            event_id=str(row[2]),
            stream_id=str(row[3]),
            stream_sequence=int(row[4]),
            subscription_id=str(row[5]),
            subscription_version=int(row[6]),
            consumer_id=str(row[7]),
            consumer_effect_id=str(row[8]) if row[8] is not None else None,
            delivery_generation=int(row[9]),
            attempt_count=int(row[10]),
            first_failure_at=_required_utc(row[11], "first_failure_at"),
            last_failure_at=_required_utc(row[12], "last_failure_at"),
            reason_class=str(row[13]),
            redacted_diagnostic=str(row[14]) if row[14] is not None else None,
            tenant_id=str(row[15]) if row[15] is not None else None,
            disposition=str(row[16]),
            operator_id=str(row[17]) if row[17] is not None else None,
            operator_reason=str(row[18]) if row[18] is not None else None,
            updated_at=(
                _required_utc(row[19], "updated_at")
                if row[19] is not None
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "dead-letter row failed contract validation"
        ) from exc


def _quarantine_columns() -> str:
    return """
        quarantine_id, source, reason, created_at, envelope_schema,
        event_type, data_schema, tenant_id, redacted_diagnostic,
        disposition, operator_id, operator_reason, updated_at
    """


def _quarantine_from_row(row: Sequence[Any]) -> QuarantineRecord:
    if len(row) != 13:
        raise EventStoreCorruptionError("quarantine row has an invalid shape")
    try:
        return QuarantineRecord(
            quarantine_id=str(row[0]),
            source=str(row[1]),
            reason=str(row[2]),
            created_at=_required_utc(row[3], "created_at"),
            envelope_schema=str(row[4]) if row[4] is not None else None,
            event_type=str(row[5]) if row[5] is not None else None,
            data_schema=str(row[6]) if row[6] is not None else None,
            tenant_id=str(row[7]) if row[7] is not None else None,
            redacted_diagnostic=str(row[8]) if row[8] is not None else None,
            disposition=str(row[9]),
            operator_id=str(row[10]) if row[10] is not None else None,
            operator_reason=str(row[11]) if row[11] is not None else None,
            updated_at=(
                _required_utc(row[12], "updated_at")
                if row[12] is not None
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "quarantine row failed contract validation"
        ) from exc


def _retirement_cancellation_report_columns() -> str:
    return """
        cancellation_id, tenant_id, tenant_scope, subscription_id,
        subscription_version, requested_at, cancelled_at, operator_id,
        operator_reason, authorization_evidence_ref, item_limit,
        cancelled_count, remaining_nonterminal_count,
        remaining_nonterminal_count_truncated
    """


def _select_retirement_cancellation_report(
    cursor: Any,
    cancellation_id: str,
    *,
    tenant_scope: str,
) -> RetirementCancellationReport | None:
    cursor.execute(
        f"""
        SELECT {_retirement_cancellation_report_columns()}
        FROM event_retirement_cancellation_reports
        WHERE tenant_scope = %s AND cancellation_id = %s
        """,
        (tenant_scope, cancellation_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return _retirement_cancellation_report_from_postgres(cursor, row)


def _retirement_cancellation_report_from_postgres(
    cursor: Any,
    row: Sequence[Any],
) -> RetirementCancellationReport:
    if len(row) != 14:
        raise EventStoreCorruptionError(
            "retirement cancellation report row has an invalid shape"
        )
    try:
        item_limit = int(row[10])
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "stored retirement cancellation item limit is invalid"
        ) from exc
    if not 1 <= item_limit <= MAX_RETIREMENT_CANCELLATION_ITEMS:
        raise EventStoreCorruptionError(
            "stored retirement cancellation item limit is invalid"
        )
    try:
        cancelled_count = int(row[11])
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "stored retirement cancellation item count is invalid"
        ) from exc
    if not 0 <= cancelled_count <= item_limit:
        raise EventStoreCorruptionError(
            "stored retirement cancellation item count is invalid"
        )
    cursor.execute(
        """
        SELECT
            items.tenant_id,
            items.tenant_scope,
            items.cancellation_id,
            items.delivery_id,
            items.event_id,
            items.stream_id,
            items.stream_sequence,
            items.subscription_id,
            items.subscription_version,
            items.delivery_generation,
            items.previous_state,
            items.previous_attempt_count,
            items.previous_reason_class,
            items.terminal_state,
            items.cancelled_at,
            deliveries.event_id,
            deliveries.stream_id,
            deliveries.stream_sequence,
            deliveries.subscription_id,
            deliveries.subscription_version,
            deliveries.delivery_generation,
            deliveries.state,
            deliveries.attempt_count,
            deliveries.tenant_scope,
            deliveries.reason_class,
            deliveries.lease_owner,
            deliveries.lease_generation,
            deliveries.lease_expires_at,
            deliveries.updated_at
        FROM event_retirement_cancellation_items AS items
        JOIN event_deliveries AS deliveries
          ON deliveries.delivery_id = items.delivery_id
        WHERE items.tenant_scope = %s AND items.cancellation_id = %s
        ORDER BY items.stream_id COLLATE "C", items.stream_sequence,
                 items.delivery_generation, items.delivery_id COLLATE "C"
        LIMIT %s
        """,
        (str(row[2]), str(row[0]), item_limit + 1),
    )
    item_rows = tuple(cursor.fetchall())
    if len(item_rows) > item_limit:
        raise EventStoreCorruptionError(
            "stored retirement cancellation report exceeds its item limit"
        )
    if len(item_rows) != cancelled_count:
        raise EventStoreCorruptionError(
            "stored retirement cancellation report is missing audit items"
        )
    try:
        subscription = SubscriptionKey(str(row[3]), int(row[4]))
        cancelled_at = _required_utc(row[6], "cancelled_at")
        items: list[RetirementCancellationItem] = []
        for item_row in item_rows:
            tenant_id = str(item_row[0]) if item_row[0] is not None else None
            item_tenant_scope = str(item_row[1])
            if _tenant_scope(tenant_id) != item_tenant_scope:
                raise EventStoreCorruptionError(
                    "retirement cancellation item tenant index is corrupt"
                )
            item_subscription = SubscriptionKey(
                str(item_row[7]),
                int(item_row[8]),
            )
            if item_subscription != subscription:
                raise EventStoreCorruptionError(
                    "retirement cancellation item crossed subscription scope"
                )
            indexed = (
                str(item_row[4]),
                str(item_row[5]),
                int(item_row[6]),
                str(item_row[7]),
                int(item_row[8]),
                int(item_row[9]),
                str(item_row[13]),
                item_tenant_scope,
            )
            linked = (
                str(item_row[15]),
                str(item_row[16]),
                int(item_row[17]),
                str(item_row[18]),
                int(item_row[19]),
                int(item_row[20]),
                str(item_row[21]),
                str(item_row[23]),
            )
            expected_attempt_count = max(1, int(item_row[11]))
            if (
                indexed != linked
                or int(item_row[22]) != expected_attempt_count
                or str(item_row[24]) != "subscription_retired"
                or item_row[25] is not None
                or item_row[26] is not None
                or item_row[27] is not None
                or _required_utc(item_row[28], "delivery updated_at")
                != _required_utc(item_row[14], "cancelled_at")
            ):
                raise EventStoreCorruptionError(
                    "retirement cancellation item disagrees with its delivery disposition"
                )
            items.append(
                RetirementCancellationItem(
                    cancellation_id=str(item_row[2]),
                    delivery_id=str(item_row[3]),
                    event_id=str(item_row[4]),
                    stream_id=str(item_row[5]),
                    stream_sequence=int(item_row[6]),
                    subscription=subscription,
                    delivery_generation=int(item_row[9]),
                    previous_state=DeliveryState(str(item_row[10])),
                    previous_attempt_count=int(item_row[11]),
                    previous_reason_class=(
                        str(item_row[12]) if item_row[12] is not None else None
                    ),
                    terminal_state=DeliveryState(str(item_row[13])),
                    cancelled_at=_required_utc(item_row[14], "cancelled_at"),
                    tenant_id=tenant_id,
                )
            )
        report = RetirementCancellationReport(
            cancellation_id=str(row[0]),
            tenant_id=str(row[1]) if row[1] is not None else None,
            subscription=subscription,
            requested_at=_required_utc(row[5], "requested_at"),
            cancelled_at=cancelled_at,
            operator_id=str(row[7]),
            operator_reason=str(row[8]),
            authorization_evidence_ref=str(row[9]),
            item_limit=item_limit,
            remaining_nonterminal_count=int(row[12]),
            remaining_nonterminal_count_truncated=bool(row[13]),
            items=tuple(items),
        )
    except EventStoreCorruptionError:
        raise
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "retirement cancellation report row failed contract validation"
        ) from exc
    if _tenant_scope(report.tenant_id) != str(row[2]):
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
    if _retirement_cancellation_identity(report) != (
        _retirement_cancellation_identity(request)
    ):
        raise EventRetirementCancellationCollisionError(
            "retirement cancellation id was reused for another operator command"
        )


def _redelivery_report_columns() -> str:
    return """
        redelivery_id, tenant_id, subscription_id, subscription_version,
        source_stream_id, from_sequence, requested_through_sequence,
        through_sequence, captured_high_watermark, requested_at, scheduled_at,
        operator_id, operator_reason, authorization_evidence_ref
    """


def _redelivery_report_from_postgres(
    cursor: Any,
    row: Sequence[Any],
) -> RedeliveryReport:
    if len(row) != 14:
        raise EventStoreCorruptionError("redelivery report row has an invalid shape")
    cursor.execute(
        """
        SELECT
            items.tenant_id,
            items.redelivery_id,
            items.event_id,
            items.stream_id,
            items.stream_sequence,
            items.subscription_id,
            items.subscription_version,
            items.delivery_id,
            items.delivery_generation,
            items.created_at,
            deliveries.event_id,
            deliveries.stream_id,
            deliveries.stream_sequence,
            deliveries.subscription_id,
            deliveries.subscription_version,
            deliveries.delivery_generation,
            deliveries.tenant_scope
        FROM event_redelivery_items AS items
        JOIN event_deliveries AS deliveries
          ON deliveries.delivery_id = items.delivery_id
        WHERE items.tenant_scope = %s AND items.redelivery_id = %s
        ORDER BY items.stream_sequence, items.event_id
        LIMIT %s
        """,
        (
            _tenant_scope(str(row[1]) if row[1] is not None else None),
            str(row[0]),
            MAX_REDELIVERY_ITEMS + 1,
        ),
    )
    item_rows = tuple(cursor.fetchall())
    if len(item_rows) > MAX_REDELIVERY_ITEMS:
        raise EventStoreCorruptionError(
            "stored redelivery report exceeds the bounded item limit"
        )
    try:
        subscription = SubscriptionKey(str(row[2]), int(row[3]))
        items: list[RedeliveryItem] = []
        for item_row in item_rows:
            tenant_id = str(item_row[0]) if item_row[0] is not None else None
            indexed = (
                str(item_row[2]),
                str(item_row[3]),
                int(item_row[4]),
                str(item_row[5]),
                int(item_row[6]),
                int(item_row[8]),
                _tenant_scope(tenant_id),
            )
            linked = (
                str(item_row[10]),
                str(item_row[11]),
                int(item_row[12]),
                str(item_row[13]),
                int(item_row[14]),
                int(item_row[15]),
                str(item_row[16]),
            )
            if indexed != linked:
                raise EventStoreCorruptionError(
                    "redelivery item disagrees with its delivery generation"
                )
            items.append(
                RedeliveryItem(
                    redelivery_id=str(item_row[1]),
                    event_id=str(item_row[2]),
                    stream_id=str(item_row[3]),
                    stream_sequence=int(item_row[4]),
                    subscription=subscription,
                    delivery_id=str(item_row[7]),
                    delivery_generation=int(item_row[8]),
                    created_at=_required_utc(item_row[9], "created_at"),
                    tenant_id=tenant_id,
                )
            )
        return RedeliveryReport(
            redelivery_id=str(row[0]),
            tenant_id=str(row[1]) if row[1] is not None else None,
            subscription=subscription,
            source_stream_id=str(row[4]),
            from_sequence=int(row[5]),
            requested_through_sequence=(
                int(row[6]) if row[6] is not None else None
            ),
            through_sequence=int(row[7]),
            captured_high_watermark=int(row[8]),
            requested_at=_required_utc(row[9], "requested_at"),
            scheduled_at=_required_utc(row[10], "scheduled_at"),
            operator_id=str(row[11]),
            operator_reason=str(row[12]),
            authorization_evidence_ref=str(row[13]),
            items=tuple(items),
        )
    except EventStoreCorruptionError:
        raise
    except (TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "redelivery report row failed contract validation"
        ) from exc


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


def _replay_columns() -> str:
    return """
        replay_id, mode, source_stream_id, high_watermark, status,
        started_at, from_sequence, to_sequence, checkpoint_ref,
        versions, applied_upcasters, quarantine_refs, mismatch_sequence,
        reason_class, result_checksum, finished_at, tenant_id,
        operator_id, operator_reason
    """


def _replay_from_row(row: Sequence[Any]) -> ReplayReport:
    if len(row) != 19:
        raise EventStoreCorruptionError("replay report row has an invalid shape")
    try:
        versions_raw = _json_array(row[9], "versions")
        versions = tuple(
            ReplayVersion(
                component=str(_json_object(item, "replay version")["component"]),
                version=str(_json_object(item, "replay version")["version"]),
            )
            for item in versions_raw
        )
        return ReplayReport(
            replay_id=str(row[0]),
            mode=str(row[1]),
            source_stream_id=str(row[2]),
            high_watermark=int(row[3]),
            status=str(row[4]),
            started_at=_required_utc(row[5], "started_at"),
            from_sequence=int(row[6]) if row[6] is not None else None,
            to_sequence=int(row[7]) if row[7] is not None else None,
            checkpoint_ref=str(row[8]) if row[8] is not None else None,
            versions=versions,
            applied_upcasters=tuple(
                str(value) for value in _json_array(row[10], "applied_upcasters")
            ),
            quarantine_refs=tuple(
                str(value) for value in _json_array(row[11], "quarantine_refs")
            ),
            mismatch_sequence=int(row[12]) if row[12] is not None else None,
            reason_class=str(row[13]) if row[13] is not None else None,
            result_checksum=str(row[14]) if row[14] is not None else None,
            finished_at=(
                _required_utc(row[15], "finished_at")
                if row[15] is not None
                else None
            ),
            tenant_id=str(row[16]) if row[16] is not None else None,
            operator_id=str(row[17]) if row[17] is not None else None,
            operator_reason=str(row[18]) if row[18] is not None else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "replay report row failed contract validation"
        ) from exc


def _replay_params(report: ReplayReport) -> tuple[Any, ...]:
    return (
        report.replay_id,
        report.tenant_id,
        report.mode.value,
        report.source_stream_id,
        report.high_watermark,
        report.status.value,
        report.from_sequence,
        report.to_sequence,
        report.checkpoint_ref,
        _json(
            [
                {"component": item.component, "version": item.version}
                for item in report.versions
            ]
        ),
        _json(list(report.applied_upcasters)),
        _json(list(report.quarantine_refs)),
        report.mismatch_sequence,
        report.reason_class,
        report.result_checksum,
        report.started_at,
        report.finished_at,
        report.operator_id,
        report.operator_reason,
    )


def _replay_request_identity(value: ReplayReport | ReplayStartRequest) -> tuple[Any, ...]:
    if isinstance(value, ReplayStartRequest):
        started_at = value.requested_at
        return (
            value.replay_id,
            value.mode,
            value.source_stream_id,
            value.from_sequence,
            value.checkpoint_ref,
            value.tenant_id,
            value.operator_id,
            value.operator_reason,
            started_at,
        )
    return (
        value.replay_id,
        value.mode,
        value.source_stream_id,
        value.from_sequence,
        value.checkpoint_ref,
        value.tenant_id,
        value.operator_id,
        value.operator_reason,
        value.started_at,
    )


def _replay_immutable_identity(report: ReplayReport) -> tuple[Any, ...]:
    return (
        report.replay_id,
        report.mode,
        report.source_stream_id,
        report.high_watermark,
        report.started_at,
        report.from_sequence,
        report.checkpoint_ref,
        report.tenant_id,
        report.operator_id,
        report.operator_reason,
    )


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


def _subscription_start_sequence(
    subscription: DurableSubscription,
    registration_watermark: int,
) -> int:
    if subscription.start.policy is SubscriptionStartPolicy.EARLIEST:
        return 1
    if subscription.start.policy is SubscriptionStartPolicy.LATEST:
        return registration_watermark + 1
    if subscription.start.start_sequence is None:
        raise EventStoreCorruptionError("AT_SEQUENCE subscription has no start sequence")
    return subscription.start.start_sequence


def _delivery_id(
    event_id: str,
    subscription_id: str,
    subscription_version: int,
    delivery_generation: int,
) -> str:
    return delivery_id_for(
        event_id,
        subscription_id,
        subscription_version,
        delivery_generation,
    )


def _dead_letter_id(delivery_id: str, delivery_generation: int) -> str:
    return dead_letter_id_for(delivery_id, delivery_generation)


def _lock_subscription_registry(
    cursor: Any,
    tenant_scope: str,
    *,
    exclusive: bool,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    _acquire_transaction_advisory_lock(
        cursor,
        f"event-subscription-registry:{tenant_scope}",
        exclusive=exclusive,
        lock_scope=lock_scope,
    )


CapacityLockKey = tuple[str, str, int]


class _PostgresTransactionLockTracker:
    """Coordinate all logical event-store locks held by one wider UoW.

    The first mutation may block while acquiring its complete, fixed lock plan.
    Once that mutation has retained any transaction advisory lock, later
    mutations only try newly encountered logical resources.  This prevents a
    business UoW from waiting while it already holds event-store locks that a
    competing UoW may need to finish.  Callers must retry the whole UoW after a
    typed contention failure.

    Capacity keys additionally retain a transaction-wide ascending watermark.
    That preserves the deterministic subscription ordering used by direct
    operations while still allowing re-entrant append/settle combinations.
    """

    def __init__(self) -> None:
        self._held_modes: dict[str, Literal["shared", "exclusive"]] = {}
        self._held_capacity_keys: set[CapacityLockKey] = set()
        self._capacity_high_watermark: CapacityLockKey | None = None

    def new_operation(self) -> _PostgresTransactionLockScope:
        return _PostgresTransactionLockScope(
            self,
            allow_blocking=not self._held_modes,
        )

    def _mode_for(self, lock_name: str) -> Literal["shared", "exclusive"] | None:
        return self._held_modes.get(lock_name)

    def _record_mode(
        self,
        lock_name: str,
        mode: Literal["shared", "exclusive"],
    ) -> None:
        self._held_modes[lock_name] = mode

    def _new_capacity_keys(
        self,
        keys: Sequence[CapacityLockKey],
    ) -> tuple[CapacityLockKey, ...]:
        pending = tuple(key for key in keys if key not in self._held_capacity_keys)
        if (
            pending
            and self._capacity_high_watermark is not None
            and pending[0] < self._capacity_high_watermark
        ):
            raise EventStoreContentionError(
                "event unit of work requested subscription capacity locks "
                "out of canonical order; retry the entire transaction"
            )
        return pending

    def _record_capacity_key(self, key: CapacityLockKey) -> None:
        self._held_capacity_keys.add(key)
        if (
            self._capacity_high_watermark is None
            or key > self._capacity_high_watermark
        ):
            self._capacity_high_watermark = key


class _PostgresTransactionLockScope:
    """One mutation's view of transaction-wide advisory lock ownership."""

    def __init__(
        self,
        tracker: _PostgresTransactionLockTracker,
        *,
        allow_blocking: bool,
    ) -> None:
        self._tracker = tracker
        self._allow_blocking = allow_blocking

    def acquire(
        self,
        cursor: Any,
        lock_name: str,
        *,
        exclusive: bool,
    ) -> None:
        held_mode = self._tracker._mode_for(lock_name)
        requested_mode: Literal["shared", "exclusive"] = (
            "exclusive" if exclusive else "shared"
        )
        if held_mode == "exclusive" or held_mode == requested_mode:
            return

        # Shared-to-exclusive upgrades are always non-blocking.  Two sessions
        # upgrading a shared advisory lock while retaining their shared holds
        # would otherwise create an avoidable upgrade cycle.
        try_only = not self._allow_blocking or held_mode == "shared"
        if try_only:
            function = (
                "pg_try_advisory_xact_lock"
                if exclusive
                else "pg_try_advisory_xact_lock_shared"
            )
            cursor.execute(
                f"SELECT {function}(hashtextextended(%s, 0))",
                (lock_name,),
            )
            acquired_row = cursor.fetchone()
            if acquired_row is None:
                raise EventStoreCorruptionError(
                    "PostgreSQL advisory lock attempt returned no result"
                )
            if not bool(acquired_row[0]):
                raise EventStoreContentionError(
                    "event unit of work encountered concurrent event-store "
                    "lock contention; retry the entire transaction"
                )
        else:
            function = (
                "pg_advisory_xact_lock"
                if exclusive
                else "pg_advisory_xact_lock_shared"
            )
            cursor.execute(
                f"SELECT {function}(hashtextextended(%s, 0))",
                (lock_name,),
            )
        self._tracker._record_mode(lock_name, requested_mode)

    def new_capacity_keys(
        self,
        keys: Sequence[CapacityLockKey],
    ) -> tuple[CapacityLockKey, ...]:
        return self._tracker._new_capacity_keys(keys)

    def capacity_key_acquired(self, key: CapacityLockKey) -> None:
        self._tracker._record_capacity_key(key)


def _acquire_transaction_advisory_lock(
    cursor: Any,
    lock_name: str,
    *,
    exclusive: bool,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    if lock_scope is not None:
        lock_scope.acquire(cursor, lock_name, exclusive=exclusive)
        return
    function = (
        "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    )
    cursor.execute(
        f"SELECT {function}(hashtextextended(%s, 0))",
        (lock_name,),
    )


def _matching_subscriptions_for_append(
    cursor: Any,
    *,
    tenant_scope: str,
    event_type: str,
    data_schema: str,
    lock_rows: bool,
) -> tuple[DurableSubscription, ...]:
    row_lock = "FOR SHARE" if lock_rows else ""
    cursor.execute(
        f"""
        SELECT {_subscription_columns()}
        FROM event_subscriptions
        WHERE tenant_scope = %s
          AND status <> 'retired'
          AND (
                jsonb_array_length(event_types) = 0
                OR event_types ? %s
          )
          AND (
                jsonb_array_length(data_schemas) = 0
                OR data_schemas ? %s
          )
        ORDER BY subscription_id, subscription_version
        {row_lock}
        """,
        (tenant_scope, event_type, data_schema),
    )
    return tuple(_subscription_from_row(row) for row in cursor.fetchall())


def _subscription_capacity_keys(
    subscriptions: Sequence[DurableSubscription],
) -> tuple[CapacityLockKey, ...]:
    return tuple(
        sorted(
            {
                _subscription_delivery_capacity_lock_key(subscription)
                for subscription in subscriptions
            }
        )
    )


def _preflight_subscription_delivery_capacities(
    subscriptions: Sequence[DurableSubscription],
    *,
    lock_scope: _PostgresTransactionLockScope,
) -> None:
    # ``new_capacity_keys`` performs the transaction-wide monotonicity check
    # without recording ownership or issuing SQL.  The actual acquisition
    # repeats the check and records a key only after PostgreSQL confirms it.
    lock_scope.new_capacity_keys(_subscription_capacity_keys(subscriptions))


def _lock_subscription_delivery_capacities(
    cursor: Any,
    subscriptions: Sequence[DurableSubscription],
    *,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    """Fence append admission for several subscriptions in one fixed order."""

    by_key = {
        _subscription_delivery_capacity_lock_key(subscription): subscription
        for subscription in subscriptions
    }
    ordered_keys = _subscription_capacity_keys(subscriptions)
    acquisition_keys = (
        ordered_keys
        if lock_scope is None
        else lock_scope.new_capacity_keys(ordered_keys)
    )
    for key in acquisition_keys:
        subscription = by_key[key]
        _acquire_transaction_advisory_lock(
            cursor,
            _subscription_delivery_capacity_lock_name(subscription),
            exclusive=True,
            lock_scope=lock_scope,
        )
        if lock_scope is not None:
            lock_scope.capacity_key_acquired(key)


def _lock_subscription_delivery_capacity(
    cursor: Any,
    subscription: DurableSubscription,
    *,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    _lock_subscription_delivery_capacities(
        cursor,
        (subscription,),
        lock_scope=lock_scope,
    )


def _lock_event_identity(
    cursor: Any,
    event_id: str,
    *,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    _acquire_transaction_advisory_lock(
        cursor,
        f"event:{event_id}",
        exclusive=True,
        lock_scope=lock_scope,
    )


def _lock_stream_sequence(
    cursor: Any,
    *,
    tenant_scope: str,
    stream_id: str,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    _acquire_transaction_advisory_lock(
        cursor,
        f"event-stream-sequence:{_json([tenant_scope, stream_id])}",
        exclusive=True,
        lock_scope=lock_scope,
    )


def _lock_delivery_row(
    cursor: Any,
    delivery_id: str,
    *,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    _acquire_transaction_advisory_lock(
        cursor,
        f"event-delivery-row:{_json([delivery_id])}",
        exclusive=True,
        lock_scope=lock_scope,
    )


def _lock_inbox_row(
    cursor: Any,
    event_id: str,
    consumer_effect_id: str,
    *,
    lock_scope: _PostgresTransactionLockScope | None = None,
) -> None:
    _acquire_transaction_advisory_lock(
        cursor,
        f"event-inbox-row:{_json([event_id, consumer_effect_id])}",
        exclusive=True,
        lock_scope=lock_scope,
    )


def _subscription_identity_lock_name(
    subscription: DurableSubscription,
) -> str:
    return (
        "subscription:"
        f"{subscription.subscription_id}:{subscription.subscription_version}"
    )


def _subscription_delivery_capacity_lock_key(
    subscription: DurableSubscription,
) -> CapacityLockKey:
    return (
        _tenant_scope(subscription.tenant_id),
        subscription.subscription_id,
        subscription.subscription_version,
    )


def _subscription_delivery_capacity_lock_name(
    subscription: DurableSubscription,
) -> str:
    identity = _json(list(_subscription_delivery_capacity_lock_key(subscription)))
    return f"event-subscription-delivery-capacity:{identity}"


def _subscription_pending_capacity_exhausted(
    cursor: Any,
    subscription: DurableSubscription,
) -> bool:
    """Check the hard boundary while the subscription capacity fence is held.

    The query stops at the configured boundary instead of performing an
    unfenced aggregate over a concurrently changing delivery set.
    """

    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM event_deliveries
            WHERE tenant_scope = %s
              AND subscription_id = %s
              AND subscription_version = %s
              AND state IN ('pending', 'claimed', 'retry_wait')
            ORDER BY delivery_id
            OFFSET %s
            LIMIT 1
        )
        """,
        (
            _tenant_scope(subscription.tenant_id),
            subscription.subscription_id,
            subscription.subscription_version,
            subscription.limits.pending_hard_limit - 1,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise EventStoreCorruptionError(
            "subscription pending capacity query returned no row"
        )
    return bool(row[0])


def _bounded_subscription_in_flight(
    cursor: Any,
    subscription: DurableSubscription,
) -> int:
    """Return at most the configured in-flight bound under its capacity fence."""

    cursor.execute(
        """
        SELECT delivery_id
        FROM event_deliveries
        WHERE tenant_scope = %s
          AND subscription_id = %s
          AND subscription_version = %s
          AND state = 'claimed'
        ORDER BY delivery_id
        LIMIT %s
        """,
        (
            _tenant_scope(subscription.tenant_id),
            subscription.subscription_id,
            subscription.subscription_version,
            subscription.limits.max_in_flight,
        ),
    )
    return len(cursor.fetchall())


def _subscription_lease_owner_slot_available(
    cursor: Any,
    subscription: DurableSubscription,
    *,
    lease_owner: str,
    database_now: datetime,
) -> bool:
    """Reserve no row, but decide one owner slot under the capacity fence."""

    cursor.execute(
        """
        SELECT
            EXISTS (
                SELECT 1
                FROM event_deliveries
                WHERE tenant_scope = %s
                  AND subscription_id = %s
                  AND subscription_version = %s
                  AND state = 'claimed'
                  AND lease_expires_at > %s
                  AND lease_owner = %s
            ),
            (
                SELECT count(*)
                FROM (
                    SELECT lease_owner
                    FROM event_deliveries
                    WHERE tenant_scope = %s
                      AND subscription_id = %s
                      AND subscription_version = %s
                      AND state = 'claimed'
                      AND lease_expires_at > %s
                      AND lease_owner IS NOT NULL
                    GROUP BY lease_owner
                    ORDER BY lease_owner
                    LIMIT %s
                ) AS active_owners
            )
        """,
        (
            _tenant_scope(subscription.tenant_id),
            subscription.subscription_id,
            subscription.subscription_version,
            database_now,
            lease_owner,
            _tenant_scope(subscription.tenant_id),
            subscription.subscription_id,
            subscription.subscription_version,
            database_now,
            subscription.limits.max_concurrency,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise EventStoreCorruptionError(
            "subscription concurrency slot query returned no row"
        )
    return bool(row[0]) or int(row[1]) < subscription.limits.max_concurrency


def _database_now(cursor: Any) -> datetime:
    cursor.execute("SELECT clock_timestamp()")
    row = cursor.fetchone()
    if row is None:
        raise EventStoreCorruptionError("PostgreSQL clock query returned no row")
    return _required_utc(row[0], "database_now")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    parsed = _json_value(value, field_name)
    if not isinstance(parsed, Mapping):
        raise EventStoreCorruptionError(f"stored {field_name} must be a JSON object")
    return dict(parsed)


def _json_array(value: Any, field_name: str) -> list[Any]:
    parsed = _json_value(value, field_name)
    if not isinstance(parsed, list):
        raise EventStoreCorruptionError(f"stored {field_name} must be a JSON array")
    return parsed


def _json_value(value: Any, field_name: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise EventStoreCorruptionError(
                f"stored {field_name} contains invalid JSON"
            ) from exc
    return value


def _tenant_scope(tenant_id: str | None) -> str:
    if tenant_id is None:
        return ""
    return _required_text(tenant_id, "tenant_id")


def _expected_last_sequence(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected_last_sequence must be an integer or None")
    if value < 0:
        raise ValueError("expected_last_sequence must not be negative")
    return value


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _required_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_time(value: Any) -> str:
    return _required_utc(value, "stored timestamp").isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid timestamp cursor")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid timestamp cursor") from exc
    return _required_utc(parsed, "cursor timestamp")


def _encode_cursor(values: Sequence[Any]) -> str:
    raw = _json(list(values)).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str, length: int) -> tuple[Any, ...]:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid page cursor") from exc
    if not isinstance(parsed, list) or len(parsed) != length:
        raise ValueError("invalid page cursor")
    return tuple(parsed)


def _transactional_state_from_row(
    row: Sequence[Any],
) -> TransactionalStateSnapshot:
    try:
        payload = row[4]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        return TransactionalStateSnapshot.from_dict(
            {
                "namespace": row[0],
                "key": row[1],
                "revision": row[2],
                "checksum": row[3],
                "payload": payload,
            }
        )
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise EventStoreCorruptionError(
            "stored transactional state cannot be decoded"
        ) from exc


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


def _transactional_state_lock_name(namespace: str, key: str) -> str:
    return f"event-transactional-state:{len(namespace)}:{namespace}:{key}"


def _reraise_store_exception(exc: BaseException) -> NoReturn:
    if not isinstance(exc, psycopg.Error):
        raise exc
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    if sqlstate in {"40001", "40P01", "55P03"}:
        raise EventStoreContentionError(
            "PostgreSQL durable event operation encountered retryable contention"
        ) from exc
    if sqlstate in {"53100", "53200", "53400"}:
        raise EventStoreCapacityError(
            "PostgreSQL durable event capacity is exhausted"
        ) from exc
    if (
        isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))
        or sqlstate.startswith("08")
        or sqlstate in {"53300", "57P01", "57P02", "57P03"}
    ):
        raise EventStoreUnavailableError(
            "PostgreSQL durable event store is unavailable"
        ) from exc
    raise EventStoreError("PostgreSQL durable event operation failed") from exc


__all__ = [
    "POSTGRES_TRANSACTIONAL_STATE_DDL",
    "PostgresDurableEventStore",
    "PostgresEventUnitOfWork",
]
