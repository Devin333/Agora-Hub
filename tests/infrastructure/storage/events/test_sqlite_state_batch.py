from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from framework.events import (
    BusinessContext,
    DeliveryQuery,
    DurableSubscription,
    EventContractError,
    EventIdentityCollisionError,
    EventPublishRequest,
    EventRuntime,
    EventSchemaCatalog,
    EventSchemaRegistration,
    EventStoreContentionError,
    ProducerIdentity,
    SensitivityPolicy,
    StreamReadRequest,
    TransactionalStateSnapshot,
)
from infrastructure.storage.events.sqlite import SQLiteEventStore


OCCURRED_AT = datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
OBSERVED_AT = OCCURRED_AT + timedelta(seconds=1)
TENANT_ID = "tenant-a"
STATE_NAMESPACE = "harness.capacity"
STATE_KEY = "tenant-a:shared-pool"
SUBSCRIPTION_ID = "state-batch-projection"


def _catalog() -> EventSchemaCatalog:
    catalog = EventSchemaCatalog()
    catalog.register(
        EventSchemaRegistration(
            event_type="io.newsroom.test.state-batch",
            data_schema="io.newsroom.test.state-batch/v1",
            json_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            sensitivity_policy=SensitivityPolicy(),
            current=True,
        )
    )
    return catalog


def _store(database: Path, *, initialize: bool = True) -> SQLiteEventStore:
    return SQLiteEventStore(
        database,
        initialize=initialize,
        clock=lambda: OBSERVED_AT,
    )


def _runtime(store: SQLiteEventStore) -> EventRuntime:
    return EventRuntime(store=store, schema_catalog=_catalog(), backend="sqlite")


def _request(
    event_id: str,
    *,
    stream_id: str,
    message: str,
    offset_seconds: int = 0,
) -> EventPublishRequest:
    return EventPublishRequest(
        event_id=event_id,
        event_type="io.newsroom.test.state-batch",
        data_schema="io.newsroom.test.state-batch/v1",
        source="tests.sqlite-state-batch",
        occurred_at=OCCURRED_AT + timedelta(seconds=offset_seconds),
        stream_id=stream_id,
        business_context=BusinessContext(run_id=stream_id.removeprefix("run:")),
        producer=ProducerIdentity(component="sqlite-state-batch-tests", version="1"),
        tenant_id=TENANT_ID,
        payload={"message": message},
    )


def _state(revision: int, owner: str) -> TransactionalStateSnapshot:
    return TransactionalStateSnapshot.create(
        namespace=STATE_NAMESPACE,
        key=STATE_KEY,
        revision=revision,
        payload={"owner": owner},
    )


def _register_projection(store: SQLiteEventStore) -> None:
    store.register_subscription(
        DurableSubscription(
            SUBSCRIPTION_ID,
            1,
            "state-batch-consumer",
            tenant_id=TENANT_ID,
        )
    )


def _deliveries(store: SQLiteEventStore):
    return store.list_deliveries(
        DeliveryQuery(
            subscription_id=SUBSCRIPTION_ID,
            subscription_version=1,
            tenant_id=TENANT_ID,
            limit=100,
        )
    ).records


def test_independent_runtimes_competing_for_one_state_revision_commit_one_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state-race.sqlite3"
    observer = _store(database)
    _register_projection(observer)
    initial = _state(1, "unreserved")
    _runtime(observer).compare_and_swap_transactional_state(
        initial,
        expected_revision=None,
        expected_checksum=None,
    )

    stores = {
        label: _store(database, initialize=False) for label in ("alpha", "beta")
    }
    runtimes = {label: _runtime(store) for label, store in stores.items()}
    transitions = {label: _state(2, label) for label in ("alpha", "beta")}
    requests = {
        label: _request(
            f"evt-state-race-{label}",
            stream_id=f"run:state-race-{label}",
            message=f"admitted by {label}",
        )
        for label in ("alpha", "beta")
    }
    barrier = Barrier(2)

    def compete(label: str):
        barrier.wait(timeout=10)
        try:
            return (
                label,
                runtimes[label].publish_batch_with_state_cas(
                    [requests[label]],
                    expected_last_sequence=0,
                    state_namespace=STATE_NAMESPACE,
                    state_key=STATE_KEY,
                    expected_state_revision=initial.revision,
                    expected_state_checksum=initial.checksum,
                    next_state=transitions[label],
                ),
            )
        except EventStoreContentionError as error:
            return label, error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(compete, ("alpha", "beta")))

    committed = tuple(item for item in results if not isinstance(item[1], Exception))
    rejected = tuple(item for item in results if isinstance(item[1], Exception))
    assert len(committed) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0][1], EventStoreContentionError)

    winner = committed[0][0]
    loser = rejected[0][0]
    stored_events, persisted_state = committed[0][1]
    assert persisted_state == transitions[winner]
    assert observer.load_transactional_state(STATE_NAMESPACE, STATE_KEY) == transitions[
        winner
    ]
    assert [event.event_id for event in stored_events] == [requests[winner].event_id]
    assert observer.get_event(requests[winner].event_id, tenant_id=TENANT_ID) == (
        stored_events[0]
    )
    assert observer.get_event(requests[loser].event_id, tenant_id=TENANT_ID) is None
    assert observer.get_stream_high_watermark(
        requests[winner].stream_id,
        tenant_id=TENANT_ID,
    ) == 1
    assert observer.get_stream_high_watermark(
        requests[loser].stream_id,
        tenant_id=TENANT_ID,
    ) is None
    assert [
        (delivery.event_id, delivery.stream_id, delivery.stream_sequence)
        for delivery in _deliveries(observer)
    ] == [(requests[winner].event_id, requests[winner].stream_id, 1)]


def test_second_append_identity_collision_rolls_back_state_event_sequence_and_delivery(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state-batch-rollback.sqlite3"
    store = _store(database)
    _register_projection(store)
    runtime = _runtime(store)
    initial = _state(1, "unreserved")
    runtime.compare_and_swap_transactional_state(
        initial,
        expected_revision=None,
        expected_checksum=None,
    )

    existing_request = _request(
        "evt-existing-collision",
        stream_id="run:existing-collision",
        message="original identity",
    )
    existing_event = runtime.publish(existing_request, expected_last_sequence=0)
    deliveries_before = _deliveries(store)
    next_state = _state(2, "batch-owner")
    first_request = _request(
        "evt-rollback-first",
        stream_id="run:rollback-batch",
        message="must roll back",
        offset_seconds=1,
    )
    colliding_request = _request(
        existing_request.event_id,
        stream_id=first_request.stream_id,
        message="conflicting identity",
        offset_seconds=2,
    )

    with pytest.raises(EventIdentityCollisionError):
        runtime.publish_batch_with_state_cas(
            [first_request, colliding_request],
            expected_last_sequence=0,
            state_namespace=STATE_NAMESPACE,
            state_key=STATE_KEY,
            expected_state_revision=initial.revision,
            expected_state_checksum=initial.checksum,
            next_state=next_state,
        )

    assert store.load_transactional_state(STATE_NAMESPACE, STATE_KEY) == initial
    assert store.get_event(first_request.event_id, tenant_id=TENANT_ID) is None
    assert (
        store.get_event(existing_request.event_id, tenant_id=TENANT_ID)
        == existing_event
    )
    assert store.get_stream_high_watermark(
        first_request.stream_id,
        tenant_id=TENANT_ID,
    ) is None
    assert _deliveries(store) == deliveries_before

    after_rollback = runtime.publish(
        _request(
            "evt-after-rollback",
            stream_id=first_request.stream_id,
            message="first durable sequence",
            offset_seconds=3,
        ),
        expected_last_sequence=0,
    )
    assert after_rollback.stream_sequence == 1
    assert all(
        delivery.event_id != first_request.event_id for delivery in _deliveries(store)
    )


def test_exact_state_and_event_batch_redelivery_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "state-batch-redelivery.sqlite3"
    store = _store(database)
    _register_projection(store)
    runtime = _runtime(store)
    initial = _state(1, "unreserved")
    runtime.compare_and_swap_transactional_state(
        initial,
        expected_revision=None,
        expected_checksum=None,
    )
    next_state = _state(2, "wave-one")
    requests = (
        _request(
            "evt-redelivery-one",
            stream_id="run:state-redelivery",
            message="wave admitted",
        ),
        _request(
            "evt-redelivery-two",
            stream_id="run:state-redelivery",
            message="spawn intent",
            offset_seconds=1,
        ),
    )

    first = runtime.publish_batch_with_state_cas(
        requests,
        expected_last_sequence=0,
        state_namespace=STATE_NAMESPACE,
        state_key=STATE_KEY,
        expected_state_revision=initial.revision,
        expected_state_checksum=initial.checksum,
        next_state=next_state,
    )
    deliveries_after_first = _deliveries(store)
    redelivered = runtime.publish_batch_with_state_cas(
        requests,
        expected_last_sequence=0,
        state_namespace=STATE_NAMESPACE,
        state_key=STATE_KEY,
        expected_state_revision=initial.revision,
        expected_state_checksum=initial.checksum,
        next_state=next_state,
    )

    assert redelivered == first
    assert store.load_transactional_state(STATE_NAMESPACE, STATE_KEY) == next_state
    assert store.get_stream_high_watermark(
        requests[0].stream_id,
        tenant_id=TENANT_ID,
    ) == 2
    assert store.read_stream(
        StreamReadRequest(requests[0].stream_id, tenant_id=TENANT_ID)
    ).events == first[0]
    assert _deliveries(store) == deliveries_after_first
    assert [delivery.event_id for delivery in deliveries_after_first] == [
        request.event_id for request in requests
    ]


def test_state_transition_and_event_batch_cannot_be_mixed_across_redeliveries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state-batch-mixed-redelivery.sqlite3"
    store = _store(database)
    _register_projection(store)
    runtime = _runtime(store)
    initial = _state(1, "unreserved")
    runtime.compare_and_swap_transactional_state(
        initial,
        expected_revision=None,
        expected_checksum=None,
    )
    admitted = _state(2, "wave-one")
    original_request = _request(
        "evt-original-transition",
        stream_id="run:mixed-redelivery",
        message="original admission",
    )
    original_events, _ = runtime.publish_batch_with_state_cas(
        [original_request],
        expected_last_sequence=0,
        state_namespace=STATE_NAMESPACE,
        state_key=STATE_KEY,
        expected_state_revision=initial.revision,
        expected_state_checksum=initial.checksum,
        next_state=admitted,
    )
    deliveries_before_rejections = _deliveries(store)

    unseen_request = _request(
        "evt-unseen-on-redelivery",
        stream_id=original_request.stream_id,
        message="must not attach to consumed transition",
        offset_seconds=1,
    )
    with pytest.raises(
        EventContractError,
        match="state redelivery cannot commit previously unseen events",
    ):
        runtime.publish_batch_with_state_cas(
            [unseen_request],
            expected_last_sequence=1,
            state_namespace=STATE_NAMESPACE,
            state_key=STATE_KEY,
            expected_state_revision=initial.revision,
            expected_state_checksum=initial.checksum,
            next_state=admitted,
        )

    assert store.load_transactional_state(STATE_NAMESPACE, STATE_KEY) == admitted
    assert store.get_event(unseen_request.event_id, tenant_id=TENANT_ID) is None
    assert store.get_stream_high_watermark(
        original_request.stream_id,
        tenant_id=TENANT_ID,
    ) == 1
    assert _deliveries(store) == deliveries_before_rejections

    released = _state(3, "released")
    with pytest.raises(
        EventContractError,
        match="new state transition cannot reuse a previously committed event",
    ):
        runtime.publish_batch_with_state_cas(
            [original_request],
            expected_last_sequence=0,
            state_namespace=STATE_NAMESPACE,
            state_key=STATE_KEY,
            expected_state_revision=admitted.revision,
            expected_state_checksum=admitted.checksum,
            next_state=released,
        )

    assert store.load_transactional_state(STATE_NAMESPACE, STATE_KEY) == admitted
    assert store.read_stream(
        StreamReadRequest(original_request.stream_id, tenant_id=TENANT_ID)
    ).events == original_events
    assert store.get_stream_high_watermark(
        original_request.stream_id,
        tenant_id=TENANT_ID,
    ) == 1
    assert _deliveries(store) == deliveries_before_rejections
