from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from framework.events.errors import (
    EventStoreContentionError,
    EventStoreError,
    EventStoreUnavailableError,
)

psycopg = pytest.importorskip("psycopg")


class _LifecycleConnection:
    def __init__(self, *, rollback_error: BaseException | None = None) -> None:
        self.autocommit = False
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0
        self.rollback_error = rollback_error

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self) -> None:
        self.closes += 1


class _LifecycleFactory:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        return self.connection


class _AcquireFailureConnection(_LifecycleConnection):
    @property
    def autocommit(self) -> bool:
        raise psycopg.OperationalError("connection initialization failed")

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        pass


class _TransactionalStateCursor:
    def __init__(self, connection: _TransactionalStateConnection) -> None:
        self.connection = connection
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._result: tuple[object, ...] | None = None
        self.rowcount = -1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, query: object, params: tuple[object, ...]) -> None:
        sql = str(query)
        self.executed.append((sql, params))
        self.connection.executed.append((sql, params))
        self.rowcount = -1
        if "SELECT namespace, state_key, revision, checksum, payload" in sql:
            self._result = self.connection.state_row
        elif "INSERT INTO event_transactional_states" in sql:
            self.connection.state_row = (
                params[0],
                params[1],
                params[2],
                params[3],
                params[4],
            )
            self.rowcount = 1
            self._result = None
        elif "UPDATE event_transactional_states" in sql:
            current = self.connection.state_row
            if current is not None and current[2:4] == params[5:7]:
                self.connection.state_row = (
                    params[3],
                    params[4],
                    params[0],
                    params[1],
                    params[2],
                )
                self.rowcount = 1
            else:
                self.rowcount = 0
            self._result = None
        else:
            self._result = None

    def fetchone(self):
        result = self._result
        self._result = None
        return result


class _TransactionalStateConnection(_LifecycleConnection):
    def __init__(self) -> None:
        super().__init__()
        self.state_row: tuple[object, ...] | None = None
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def cursor(self) -> _TransactionalStateCursor:
        return _TransactionalStateCursor(self)


class _AdvisoryCursor:
    def __init__(self, *try_results: bool) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self._try_results = list(try_results)
        self._current_result: tuple[bool] | None = None

    def execute(self, query: object, params: tuple[object, ...]) -> None:
        sql = str(query)
        self.executed.append((sql, params))
        if "pg_try_advisory" in sql:
            self._current_result = (self._try_results.pop(0),)

    def fetchone(self) -> tuple[bool] | None:
        result = self._current_result
        self._current_result = None
        return result


def _store(factory: _LifecycleFactory) -> Any:
    from infrastructure.storage.events.postgres import PostgresDurableEventStore

    return PostgresDurableEventStore(
        "postgresql://example/test",
        connection_factory=factory,
    )


def test_initial_retired_subscription_is_rejected_before_connection_acquisition() -> None:
    from framework.events.runtime import DurableSubscription, SubscriptionStatus

    connection = _LifecycleConnection()
    factory = _LifecycleFactory(connection)
    store = _store(factory)

    with pytest.raises(ValueError, match="initial RETIRED"):
        store.register_subscription(
            DurableSubscription(
                subscription_id="retired-subscription",
                subscription_version=1,
                consumer_id="retired-consumer",
                status=SubscriptionStatus.RETIRED,
            )
        )

    assert factory.calls == 0
    assert connection.commits == 0
    assert connection.rollbacks == 0
    assert connection.closes == 0


def test_subscription_definition_excludes_lifecycle_state_and_timestamps() -> None:
    from framework.events.runtime import DurableSubscription, SubscriptionStatus
    from infrastructure.storage.events.postgres import _subscription_definition

    subscription = DurableSubscription(
        subscription_id="subscription-a",
        subscription_version=1,
        consumer_id="consumer-a",
    )
    created_at = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)
    lifecycle_update = replace(
        subscription,
        status=SubscriptionStatus.PAUSED,
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=5),
    )

    assert _subscription_definition(lifecycle_update) == _subscription_definition(
        subscription
    )
    assert _subscription_definition(
        replace(subscription, consumer_id="consumer-b")
    ) != _subscription_definition(subscription)


def test_unit_of_work_acquires_only_on_enter_and_unentered_operations_fail_typed() -> None:
    connection = _LifecycleConnection()
    factory = _LifecycleFactory(connection)
    unit_of_work = _store(factory).unit_of_work()

    assert factory.calls == 0
    for operation in (
        lambda: unit_of_work.connection,
        lambda: unit_of_work.append_event(None),  # type: ignore[arg-type]
        lambda: unit_of_work.settle_delivery(None),  # type: ignore[arg-type]
        unit_of_work.commit,
        unit_of_work.rollback,
    ):
        with pytest.raises(EventStoreError, match="has not been entered"):
            operation()
    assert factory.calls == 0
    assert connection.closes == 0


def test_unit_of_work_commit_closes_exactly_once_and_exit_is_idempotent() -> None:
    connection = _LifecycleConnection()
    factory = _LifecycleFactory(connection)
    unit_of_work = _store(factory).unit_of_work()

    with unit_of_work:
        assert unit_of_work.connection is connection
        unit_of_work.commit()

    assert factory.calls == 1
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closes == 1
    with pytest.raises(EventStoreError, match="already closed"):
        unit_of_work.commit()


def test_postgres_transactional_state_ddl_is_idempotent_and_uow_uses_row_lock() -> None:
    from framework.events.runtime import TransactionalStateSnapshot
    from infrastructure.storage.events.postgres import (
        POSTGRES_TRANSACTIONAL_STATE_DDL,
    )

    assert "CREATE TABLE IF NOT EXISTS event_transactional_states" in (
        POSTGRES_TRANSACTIONAL_STATE_DDL
    )
    connection = _TransactionalStateConnection()
    unit_of_work = _store(_LifecycleFactory(connection)).unit_of_work()
    initial = TransactionalStateSnapshot.create(
        namespace="harness.capacity",
        key="tenant-a:pool-a",
        revision=1,
        payload={"available": 2},
    )

    with unit_of_work:
        persisted = unit_of_work.cas_transactional_state(
            namespace=initial.namespace,
            key=initial.key,
            expected_revision=None,
            expected_checksum=None,
            next_snapshot=initial,
        )
        assert unit_of_work.load_transactional_state(
            initial.namespace,
            initial.key,
        ) == initial
        unit_of_work.commit()

    assert persisted == initial
    assert connection.commits == 1
    assert any("pg_advisory_xact_lock" in sql for sql, _ in connection.executed)
    assert any("FOR UPDATE" in sql for sql, _ in connection.executed)


def test_store_close_does_not_close_caller_owned_connection_factory() -> None:
    connection = _LifecycleConnection()
    factory = _LifecycleFactory(connection)
    store = _store(factory)

    store.close()

    assert factory.calls == 0
    assert connection.closes == 0


def test_default_stores_share_pool_until_last_reference_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from infrastructure.storage.events import postgres as postgres_module

    class FakePool:
        def __init__(self, *_args, **_kwargs) -> None:
            self.closed = False
            self.close_calls = 0

        def getconn(self):
            raise AssertionError("connection acquisition is not part of this test")

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True

    created: list[FakePool] = []

    def create_pool(*args, **kwargs):
        pool = FakePool(*args, **kwargs)
        created.append(pool)
        return pool

    postgres_module._close_all_shared_pools()
    monkeypatch.setattr(postgres_module, "ConnectionPool", create_pool)

    first = postgres_module.PostgresDurableEventStore(
        "postgresql://example/shared-test"
    )
    second = postgres_module.PostgresDurableEventStore(
        "postgresql://example/shared-test"
    )

    assert len(created) == 1
    assert first._pool is second._pool
    first.close()
    first.close()
    assert created[0].close_calls == 0
    second.close()
    assert created[0].close_calls == 1
    assert postgres_module._POOL_REGISTRY == {}


@pytest.mark.parametrize(
    ("minimum", "maximum", "timeout", "message"),
    (
        (-1, 1, 1.0, "pool_min_size"),
        (2, 1, 1.0, "pool_min_size"),
        (0, 0, 1.0, "pool_max_size"),
        (0, 1, 0.0, "pool_timeout_seconds"),
    ),
)
def test_pool_configuration_is_validated_before_open(
    minimum: int,
    maximum: int,
    timeout: float,
    message: str,
) -> None:
    from infrastructure.storage.events.postgres import PostgresDurableEventStore

    with pytest.raises(ValueError, match=message):
        PostgresDurableEventStore(
            "postgresql://example/test",
            pool_min_size=minimum,
            pool_max_size=maximum,
            pool_timeout_seconds=timeout,
        )


def test_unit_of_work_rollback_and_implicit_exit_each_close_exactly_once() -> None:
    explicit_connection = _LifecycleConnection()
    explicit = _store(_LifecycleFactory(explicit_connection)).unit_of_work()
    with explicit:
        explicit.rollback()
    assert explicit_connection.commits == 0
    assert explicit_connection.rollbacks == 1
    assert explicit_connection.closes == 1

    implicit_connection = _LifecycleConnection()
    implicit = _store(_LifecycleFactory(implicit_connection)).unit_of_work()
    with implicit:
        pass
    assert implicit_connection.commits == 0
    assert implicit_connection.rollbacks == 1
    assert implicit_connection.closes == 1


def test_unit_of_work_acquire_failure_closes_partially_acquired_connection() -> None:
    connection = _AcquireFailureConnection()
    factory = _LifecycleFactory(connection)
    unit_of_work = _store(factory).unit_of_work()

    with pytest.raises(EventStoreUnavailableError):
        unit_of_work.__enter__()

    assert factory.calls == 1
    assert connection.closes == 1


def test_unit_of_work_rollback_failure_is_typed_closed_and_redacted() -> None:
    connection = _LifecycleConnection(
        rollback_error=psycopg.OperationalError(
            "rollback failed for postgresql://admin:raw-secret@example/test"
        )
    )
    unit_of_work = _store(_LifecycleFactory(connection)).unit_of_work()

    unit_of_work.__enter__()
    with pytest.raises(EventStoreUnavailableError) as failure:
        unit_of_work.rollback()

    assert "raw-secret" not in str(failure.value)
    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_unit_of_work_body_failure_is_not_masked_by_rollback_failure() -> None:
    connection = _LifecycleConnection(
        rollback_error=psycopg.OperationalError("rollback unavailable")
    )
    unit_of_work = _store(_LifecycleFactory(connection)).unit_of_work()

    with pytest.raises(RuntimeError, match="body failed"):
        with unit_of_work:
            raise RuntimeError("body failed")

    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_failed_mutation_marks_unit_of_work_rollback_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _LifecycleConnection()
    store = _store(_LifecycleFactory(connection))
    unit_of_work = store.unit_of_work()

    def fail_append(
        _connection: object,
        _event: object,
        *,
        expected_last_sequence: int | None,
        lock_scope: object,
    ) -> object:
        assert expected_last_sequence is None
        assert lock_scope is not None
        raise ValueError("candidate rejected after transaction work")

    monkeypatch.setattr(store, "_append_event_in_transaction", fail_append)

    unit_of_work.__enter__()
    with pytest.raises(ValueError, match="candidate rejected"):
        unit_of_work.append_event(None)  # type: ignore[arg-type]
    with pytest.raises(EventStoreError, match="rollback-only"):
        unit_of_work.commit()

    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closes == 1


def test_transaction_lock_tracker_is_reentrant_and_upgrades_without_waiting() -> None:
    from infrastructure.storage.events.postgres import (
        _PostgresTransactionLockTracker,
    )

    cursor = _AdvisoryCursor(True, False)
    tracker = _PostgresTransactionLockTracker()
    first = tracker.new_operation()

    first.acquire(cursor, "logical-a", exclusive=False)
    first.acquire(cursor, "logical-a", exclusive=False)
    first.acquire(cursor, "logical-a", exclusive=True)
    second = tracker.new_operation()
    second.acquire(cursor, "logical-a", exclusive=True)

    assert len(cursor.executed) == 2
    assert "pg_advisory_xact_lock_shared" in cursor.executed[0][0]
    assert "pg_try_advisory_xact_lock" in cursor.executed[1][0]

    with pytest.raises(EventStoreContentionError, match="retry"):
        second.acquire(cursor, "logical-b", exclusive=True)
    assert len(cursor.executed) == 3
    assert "pg_try_advisory_xact_lock" in cursor.executed[2][0]


def test_transaction_lock_tracker_rejects_reverse_capacity_before_sql() -> None:
    from infrastructure.storage.events.postgres import (
        _PostgresTransactionLockTracker,
    )

    tracker = _PostgresTransactionLockTracker()
    first = tracker.new_operation()
    high_key = ("tenant", "subscription-b", 1)
    assert first.new_capacity_keys((high_key,)) == (high_key,)
    first.capacity_key_acquired(high_key)

    with pytest.raises(EventStoreContentionError, match="canonical order"):
        tracker.new_operation().new_capacity_keys(
            (("tenant", "subscription-a", 1),)
        )


@pytest.mark.parametrize(
    "error_type",
    (
        psycopg.errors.SerializationFailure,
        psycopg.errors.DeadlockDetected,
        psycopg.errors.LockNotAvailable,
    ),
)
def test_retryable_postgres_sqlstates_map_to_typed_contention(
    error_type: type[psycopg.Error],
) -> None:
    from infrastructure.storage.events.postgres import _reraise_store_exception

    with pytest.raises(EventStoreContentionError, match="retryable contention"):
        _reraise_store_exception(error_type("concurrent transaction conflict"))


def test_deployed_advisory_lock_names_remain_rolling_upgrade_compatible() -> None:
    from infrastructure.storage.events.postgres import (
        _lock_event_identity,
        _lock_subscription_registry,
        _subscription_identity_lock_name,
    )

    cursor = _AdvisoryCursor()
    _lock_subscription_registry(cursor, "tenant-a", exclusive=False)
    _lock_event_identity(cursor, "event-a")

    assert cursor.executed[0][1] == ("event-subscription-registry:tenant-a",)
    assert cursor.executed[1][1] == ("event:event-a",)
    assert _subscription_identity_lock_name(
        SimpleNamespace(subscription_id="subscription-a", subscription_version=2)
    ) == "subscription:subscription-a:2"
