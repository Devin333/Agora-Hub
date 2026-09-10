from __future__ import annotations

from datetime import UTC, datetime

import pytest

from framework.events import (
    AppendResult,
    BusinessContext,
    EventCandidate,
    EventContractError,
    EventContextConflictError,
    EventIdentityCollisionError,
    MAX_ATOMIC_PUBLISH_BATCH_SIZE,
    EventPublishRequest,
    EventRuntime,
    EventSchemaCatalog,
    EventSchemaRegistration,
    EventSchemaValidationError,
    EventSecurePayloadRequiredError,
    EventSecurityError,
    EventStoreContentionError,
    EventStoreUnavailableError,
    EventStreamVersionConflictError,
    LocalRuntimeDiagnosticFallback,
    PayloadReference,
    ProducerIdentity,
    SecurityClassification,
    SensitivityPolicy,
    StoredEvent,
    TransactionalStateSnapshot,
)
from framework.events.schema import WholeDocumentReferenceDisposition
from framework.events.telemetry import EventTelemetry


CHECKSUM = "sha256:" + "a" * 64
OCCURRED_AT = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 7, 15, 8, 0, 1, tzinfo=UTC)


class _UnitOfWork:
    def __init__(
        self,
        *,
        stored: StoredEvent | None = None,
        commit_error: Exception | None = None,
        append_error: Exception | None = None,
        transactional_state: TransactionalStateSnapshot | None = None,
        cas_error: Exception | None = None,
    ) -> None:
        self.stored = stored
        self.commit_error = commit_error
        self.append_error = append_error
        self.transactional_state = transactional_state
        self.cas_error = cas_error
        self.appended: list[EventCandidate] = []
        self.expected_last_sequences: list[int | None] = []
        self.commits = 0
        self.rollbacks = 0
        self.state_reads: list[tuple[str, str]] = []
        self.state_cas_calls: list[
            tuple[int | None, str | None, TransactionalStateSnapshot]
        ] = []

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        if self.append_error is not None:
            raise self.append_error
        self.appended.append(event)
        self.expected_last_sequences.append(expected_last_sequence)
        stored = self.stored or StoredEvent(
            candidate=event,
            observed_at=OBSERVED_AT,
            stream_sequence=3,
        )
        return AppendResult(
            event=stored,
            created=self.stored is None,
            pending_delivery_count=0,
        )

    def settle_delivery(self, settlement):  # pragma: no cover - protocol-only method
        raise NotImplementedError

    def load_transactional_state(self, namespace, key):
        self.state_reads.append((namespace, key))
        return self.transactional_state

    def cas_transactional_state(
        self,
        *,
        namespace,
        key,
        expected_revision,
        expected_checksum,
        next_snapshot,
    ):
        if self.cas_error is not None:
            raise self.cas_error
        assert (namespace, key) == (next_snapshot.namespace, next_snapshot.key)
        self.state_cas_calls.append(
            (expected_revision, expected_checksum, next_snapshot)
        )
        self.transactional_state = next_snapshot
        return next_snapshot

    def commit(self) -> None:
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self) -> None:
        self.rollbacks += 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None or self.commits == 0:
            self.rollback()
        return False


class _Store:
    def __init__(self, unit_of_work: _UnitOfWork) -> None:
        self.value = unit_of_work
        self.unit_of_work_calls = 0

    def unit_of_work(self) -> _UnitOfWork:
        self.unit_of_work_calls += 1
        return self.value


class _SequentialUnitOfWork(_UnitOfWork):
    def __init__(self, *, first_sequence: int = 3, commit_error: Exception | None = None):
        super().__init__(commit_error=commit_error)
        self.first_sequence = first_sequence

    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        self.appended.append(event)
        self.expected_last_sequences.append(expected_last_sequence)
        stored = StoredEvent(
            candidate=event,
            observed_at=OBSERVED_AT,
            stream_sequence=self.first_sequence + len(self.appended) - 1,
        )
        return AppendResult(
            event=stored,
            created=True,
            pending_delivery_count=0,
        )


class _DuplicateSequentialUnitOfWork(_SequentialUnitOfWork):
    def append_event(
        self,
        event: EventCandidate,
        *,
        expected_last_sequence: int | None = None,
    ) -> AppendResult:
        result = super().append_event(
            event,
            expected_last_sequence=expected_last_sequence,
        )
        return AppendResult(
            event=result.event,
            created=False,
            pending_delivery_count=0,
        )


class _MetricBackend:
    def __init__(self) -> None:
        self.counters: list[tuple[str, int, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []
        self.gauges: list[tuple[str, float, dict[str, str]]] = []

    def add_counter(self, name, value, *, attributes) -> None:
        self.counters.append((name, value, dict(attributes)))

    def record_histogram(self, name, value, *, attributes) -> None:
        self.histograms.append((name, value, dict(attributes)))

    def record_gauge(self, name, value, *, attributes) -> None:
        self.gauges.append((name, value, dict(attributes)))

    def start_span(self, name, *, attributes, links):  # pragma: no cover
        raise AssertionError("publisher metrics do not start spans")


def _catalog(
    *,
    policy: SensitivityPolicy | None = None,
) -> EventSchemaCatalog:
    catalog = EventSchemaCatalog()
    catalog.register(
        EventSchemaRegistration(
            event_type="io.newsroom.test.published",
            data_schema="io.newsroom.test.published/v1",
            json_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "token": {"type": "string"},
                },
                "required": ["message"],
                "additionalProperties": False,
            },
            sensitivity_policy=policy or SensitivityPolicy(),
            current=True,
        )
    )
    return catalog


def _request(**changes) -> EventPublishRequest:
    values = {
        "event_id": "evt-publish-1",
        "event_type": "io.newsroom.test.published",
        "data_schema": "io.newsroom.test.published/v1",
        "source": "tests.publisher",
        "occurred_at": OCCURRED_AT,
        "stream_id": "run:publish-1",
        "business_context": BusinessContext(run_id="publish-1"),
        "producer": ProducerIdentity(component="publisher-test", version="1"),
        "tenant_id": "tenant-a",
        "payload": {"message": "accepted"},
    }
    values.update(changes)
    return EventPublishRequest(**values)


def _state(revision: int, message: str) -> TransactionalStateSnapshot:
    return TransactionalStateSnapshot.create(
        namespace="harness.capacity",
        key="tenant-a:pool-a",
        revision=revision,
        payload={"message": message},
    )


def test_transactional_state_snapshot_is_strict_immutable_and_checksum_bound() -> None:
    snapshot = _state(1, "installed")

    assert TransactionalStateSnapshot.from_dict(snapshot.to_dict()) == snapshot
    with pytest.raises(TypeError):
        snapshot.payload["message"] = "mutated"  # type: ignore[index]

    unknown = snapshot.to_dict()
    unknown["extra"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        TransactionalStateSnapshot.from_dict(unknown)

    tampered = snapshot.to_dict()
    tampered["payload"] = {"message": "different"}
    with pytest.raises(ValueError, match="checksum does not match"):
        TransactionalStateSnapshot.from_dict(tampered)


def test_publish_validates_projects_appends_and_commits_before_return() -> None:
    source_payload = {"message": "accepted", "token": "must-not-persist"}
    request = _request(payload=source_payload)
    source_payload["message"] = "mutated-after-request"
    unit_of_work = _UnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(
        store=store,
        schema_catalog=_catalog(
            policy=SensitivityPolicy(
                field_rules={"/token": "sensitive"},
                redact_sensitive=True,
            )
        ),
    )

    stored = runtime.publish(request)

    assert store.unit_of_work_calls == 1
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0
    assert stored.stream_sequence == 3
    assert stored.observed_at == OBSERVED_AT
    assert stored.payload == {"message": "accepted", "token": "[REDACTED]"}
    assert "must-not-persist" not in str(stored.to_dict())
    assert unit_of_work.appended[0].content_checksum == stored.content_checksum


def test_validation_and_security_fail_before_store_or_sequence_allocation() -> None:
    unit_of_work = _UnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=_catalog())

    with pytest.raises(EventSchemaValidationError):
        runtime.publish(_request(payload={"message": 42}))
    with pytest.raises(EventSecurityError):
        runtime.publish(_request(extensions={"stream_sequence": 999}))

    assert store.unit_of_work_calls == 0
    assert unit_of_work.appended == []


def test_publish_forwards_expected_stream_position_to_unit_of_work() -> None:
    unit_of_work = _UnitOfWork()
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    stored = runtime.publish(_request(), expected_last_sequence=2)

    assert stored.stream_sequence == 3
    assert unit_of_work.expected_last_sequences == [2]


def test_publish_batch_commits_one_contiguous_same_stream_transaction() -> None:
    unit_of_work = _SequentialUnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=_catalog())

    stored = runtime.publish_batch(
        [
            _request(event_id="evt-batch-1"),
            _request(event_id="evt-batch-2", payload={"message": "second"}),
        ],
        expected_last_sequence=2,
    )

    assert [event.stream_sequence for event in stored] == [3, 4]
    assert unit_of_work.expected_last_sequences == [2, 3]
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0
    assert store.unit_of_work_calls == 1


def test_publish_batch_with_state_cas_commits_one_unit_of_work() -> None:
    current = _state(1, "before")
    next_state = _state(2, "after")
    unit_of_work = _SequentialUnitOfWork()
    unit_of_work.transactional_state = current
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=_catalog())

    stored, persisted = runtime.publish_batch_with_state_cas(
        [
            _request(event_id="evt-state-batch-1"),
            _request(event_id="evt-state-batch-2", payload={"message": "second"}),
        ],
        expected_last_sequence=2,
        state_namespace=next_state.namespace,
        state_key=next_state.key,
        expected_state_revision=current.revision,
        expected_state_checksum=current.checksum,
        next_state=next_state,
    )

    assert [event.stream_sequence for event in stored] == [3, 4]
    assert persisted == next_state
    assert unit_of_work.state_reads == [(next_state.namespace, next_state.key)]
    assert unit_of_work.state_cas_calls == [
        (current.revision, current.checksum, next_state)
    ]
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0
    assert store.unit_of_work_calls == 1


def test_publish_batch_with_state_cas_rolls_back_before_append_on_conflict() -> None:
    next_state = _state(2, "after")
    unit_of_work = _SequentialUnitOfWork()
    unit_of_work.cas_error = EventStoreContentionError("stale state")
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    with pytest.raises(EventStoreContentionError, match="stale state"):
        runtime.publish_batch_with_state_cas(
            [_request(event_id="evt-state-conflict")],
            expected_last_sequence=2,
            state_namespace=next_state.namespace,
            state_key=next_state.key,
            expected_state_revision=1,
            expected_state_checksum=CHECKSUM,
            next_state=next_state,
        )

    assert unit_of_work.appended == []
    assert unit_of_work.commits == 0
    assert unit_of_work.rollbacks == 1


def test_publish_batch_with_state_cas_reuses_exact_durable_redelivery() -> None:
    next_state = _state(2, "after")
    unit_of_work = _DuplicateSequentialUnitOfWork()
    unit_of_work.transactional_state = next_state
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    stored, persisted = runtime.publish_batch_with_state_cas(
        [
            _request(event_id="evt-state-redelivery-1"),
            _request(
                event_id="evt-state-redelivery-2",
                payload={"message": "second"},
            ),
        ],
        expected_last_sequence=2,
        state_namespace=next_state.namespace,
        state_key=next_state.key,
        expected_state_revision=1,
        expected_state_checksum=CHECKSUM,
        next_state=next_state,
    )

    assert [event.stream_sequence for event in stored] == [3, 4]
    assert persisted == next_state
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0


def test_compare_and_swap_transactional_state_installs_without_events() -> None:
    initial = _state(1, "installed")
    unit_of_work = _UnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=_catalog())

    persisted = runtime.compare_and_swap_transactional_state(
        initial,
        expected_revision=None,
        expected_checksum=None,
    )

    assert persisted == initial
    assert unit_of_work.appended == []
    assert unit_of_work.state_cas_calls == [(None, None, initial)]
    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 0


def test_publish_batch_rejects_cross_stream_input_before_opening_transaction() -> None:
    unit_of_work = _SequentialUnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=_catalog())

    with pytest.raises(EventContractError, match="one tenant and stream"):
        runtime.publish_batch(
            [
                _request(event_id="evt-batch-1"),
                _request(event_id="evt-batch-2", stream_id="run:other"),
            ]
        )

    assert store.unit_of_work_calls == 0


def test_publish_batch_rejects_oversized_sequence_before_opening_transaction() -> None:
    unit_of_work = _SequentialUnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=_catalog())
    requests = [
        _request(event_id=f"evt-batch-{index}")
        for index in range(MAX_ATOMIC_PUBLISH_BATCH_SIZE + 1)
    ]

    with pytest.raises(ValueError, match="MAX_ATOMIC_PUBLISH_BATCH_SIZE"):
        runtime.publish_batch(requests)

    assert store.unit_of_work_calls == 0
    assert unit_of_work.appended == []


def test_publish_batch_commit_failure_rolls_back_without_returning_events() -> None:
    unit_of_work = _SequentialUnitOfWork(
        commit_error=RuntimeError("batch commit unavailable")
    )
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    with pytest.raises(RuntimeError, match="batch commit unavailable"):
        runtime.publish_batch(
            [
                _request(event_id="evt-batch-1"),
                _request(event_id="evt-batch-2", payload={"message": "second"}),
            ]
        )

    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 1


def test_publish_batch_identity_collision_does_not_mark_store_unhealthy() -> None:
    unit_of_work = _UnitOfWork(
        append_error=EventIdentityCollisionError("evt-batch-1")
    )
    backend = _MetricBackend()
    runtime = EventRuntime(
        store=_Store(unit_of_work),
        schema_catalog=_catalog(),
        telemetry=EventTelemetry(backend),
        backend="sqlite",
        monotonic=iter((10.0, 10.25)).__next__,
    )

    with pytest.raises(EventIdentityCollisionError):
        runtime.publish_batch([_request(event_id="evt-batch-1")])

    assert backend.counters == [
        ("event_append_total", 1, {"backend": "sqlite", "result": "failed"}),
        ("event_identity_collision_total", 1, {"source": "unknown"}),
    ]
    assert backend.histograms == [
        ("event_append_latency_seconds", 0.25, {"backend": "sqlite"})
    ]
    assert backend.gauges == []
    assert len(runtime.diagnostic_fallback) == 0


@pytest.mark.parametrize("value", [-1, True, 1.5])
def test_publish_rejects_invalid_expected_stream_position_before_store(value) -> None:
    unit_of_work = _UnitOfWork()
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    with pytest.raises((TypeError, ValueError), match="expected_last_sequence"):
        runtime.publish(_request(), expected_last_sequence=value)

    assert unit_of_work.appended == []


def test_publish_strips_equal_context_duplicate_and_rejects_conflict_before_store() -> None:
    catalog = EventSchemaCatalog()
    catalog.register(
        EventSchemaRegistration(
            event_type="io.newsroom.test.context",
            data_schema="io.newsroom.test.context/v1",
            json_schema={
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
                "additionalProperties": False,
            },
            authoritative_context_fields=("run_id",),
            current=True,
        )
    )
    unit_of_work = _UnitOfWork()
    store = _Store(unit_of_work)
    runtime = EventRuntime(store=store, schema_catalog=catalog)
    request_values = {
        "event_id": "evt-context-1",
        "event_type": "io.newsroom.test.context",
        "data_schema": "io.newsroom.test.context/v1",
        "source": "tests.publisher",
        "occurred_at": OCCURRED_AT,
        "stream_id": "run:publish-1",
        "business_context": BusinessContext(run_id="publish-1"),
        "producer": ProducerIdentity(component="publisher-test", version="1"),
    }

    accepted = runtime.publish(
        EventPublishRequest(
            **request_values,
            payload={"message": "accepted", "run_id": "publish-1"},
        )
    )

    assert accepted.payload == {"message": "accepted"}
    assert store.unit_of_work_calls == 1
    with pytest.raises(EventContextConflictError, match="run_id"):
        runtime.publish(
            EventPublishRequest(
                **{**request_values, "event_id": "evt-context-2"},
                payload={"message": "rejected", "run_id": "another-run"},
            )
        )
    assert store.unit_of_work_calls == 1


def test_caller_owned_unit_of_work_is_not_committed_by_runtime() -> None:
    owned = _UnitOfWork()
    backend = _MetricBackend()
    runtime = EventRuntime(
        store=_Store(_UnitOfWork()),
        schema_catalog=_catalog(),
        telemetry=EventTelemetry(backend),
        backend="sqlite",
    )

    stored = runtime.publish(_request(), unit_of_work=owned)

    assert stored.stream_sequence == 3
    assert len(owned.appended) == 1
    assert owned.commits == 0
    assert owned.rollbacks == 0
    assert backend.counters == []
    assert backend.histograms == []
    assert backend.gauges == []


def test_caller_owned_unit_of_work_commit_failure_is_not_reported_as_accepted() -> None:
    owned = _UnitOfWork(commit_error=RuntimeError("business transaction rolled back"))
    backend = _MetricBackend()
    runtime = EventRuntime(
        store=_Store(_UnitOfWork()),
        schema_catalog=_catalog(),
        telemetry=EventTelemetry(backend),
        backend="postgresql",
    )

    runtime.publish(_request(), unit_of_work=owned)
    with pytest.raises(RuntimeError, match="business transaction rolled back"):
        owned.commit()

    assert owned.commits == 1
    assert backend.counters == []
    assert backend.histograms == []
    assert backend.gauges == []


def test_commit_failure_rolls_back_and_no_stored_event_is_returned() -> None:
    unit_of_work = _UnitOfWork(commit_error=RuntimeError("commit unavailable"))
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    with pytest.raises(RuntimeError, match="commit unavailable"):
        runtime.publish(_request())

    assert unit_of_work.commits == 1
    assert unit_of_work.rollbacks == 1


def test_identical_duplicate_returns_original_store_assignment() -> None:
    first_candidate = EventCandidate(
        event_id="evt-publish-1",
        event_type="io.newsroom.test.published",
        data_schema="io.newsroom.test.published/v1",
        source="tests.publisher",
        occurred_at=OCCURRED_AT,
        stream_id="run:publish-1",
        business_context=BusinessContext(run_id="publish-1"),
        producer=ProducerIdentity(component="publisher-test", version="1"),
        tenant_id="tenant-a",
        payload={"message": "accepted"},
    )
    existing = StoredEvent(first_candidate, OBSERVED_AT, 17)
    unit_of_work = _UnitOfWork(stored=existing)
    runtime = EventRuntime(store=_Store(unit_of_work), schema_catalog=_catalog())

    duplicate = runtime.publish(_request())

    assert duplicate == existing
    assert duplicate.stream_sequence == 17
    assert unit_of_work.commits == 1


def test_publish_records_committed_append_latency_result_and_store_health() -> None:
    backend = _MetricBackend()
    telemetry = EventTelemetry(backend)
    fallback = LocalRuntimeDiagnosticFallback()
    ticks = iter((10.0, 10.125, 20.0, 20.25))
    accepted = EventRuntime(
        store=_Store(_UnitOfWork()),
        schema_catalog=_catalog(),
        telemetry=telemetry,
        diagnostic_fallback=fallback,
        backend="sqlite",
        monotonic=lambda: next(ticks),
    )

    accepted.publish(_request(source="graph.runtime"))

    unavailable = EventRuntime(
        store=_Store(
            _UnitOfWork(append_error=EventStoreUnavailableError("store unavailable"))
        ),
        schema_catalog=_catalog(),
        telemetry=telemetry,
        diagnostic_fallback=fallback,
        backend="sqlite",
        monotonic=lambda: next(ticks),
    )
    with pytest.raises(EventStoreUnavailableError):
        unavailable.publish(_request(source="graph.runtime"))

    assert backend.counters == [
        ("event_append_total", 1, {"backend": "sqlite", "result": "accepted"}),
        ("event_append_total", 1, {"backend": "sqlite", "result": "failed"}),
    ]
    assert backend.histograms == [
        ("event_append_latency_seconds", 0.125, {"backend": "sqlite"}),
        ("event_append_latency_seconds", 0.25, {"backend": "sqlite"}),
    ]
    assert backend.gauges == [
        ("event_store_health", 1.0, {"backend": "sqlite"}),
        ("event_store_health", 0.0, {"backend": "sqlite"}),
    ]
    assert len(fallback) == 1


@pytest.mark.parametrize(
    "rejection",
    [
        EventIdentityCollisionError("evt-publish-1"),
        EventStreamVersionConflictError(
            stream_id="run:publish-1",
            expected_last_sequence=2,
            actual_last_sequence=3,
        ),
        EventStoreContentionError("retry wider transaction"),
    ],
    ids=("identity-collision", "stream-version-conflict", "contention"),
)
def test_healthy_append_rejection_does_not_mark_store_unhealthy(
    rejection: Exception,
) -> None:
    backend = _MetricBackend()
    fallback = LocalRuntimeDiagnosticFallback()
    runtime = EventRuntime(
        store=_Store(_UnitOfWork(append_error=rejection)),
        schema_catalog=_catalog(),
        telemetry=EventTelemetry(backend),
        diagnostic_fallback=fallback,
        backend="sqlite",
        monotonic=iter((10.0, 10.25)).__next__,
    )

    with pytest.raises(type(rejection)):
        runtime.publish(_request(source="graph.runtime"))

    assert backend.counters[0] == (
        "event_append_total",
        1,
        {"backend": "sqlite", "result": "failed"},
    )
    if isinstance(rejection, EventIdentityCollisionError):
        assert backend.counters[1:] == [
            ("event_identity_collision_total", 1, {"source": "graph"})
        ]
    else:
        assert backend.counters[1:] == []
    assert backend.histograms == [
        ("event_append_latency_seconds", 0.25, {"backend": "sqlite"})
    ]
    assert backend.gauges == []
    assert fallback.snapshot() == ()


def test_payload_reference_requires_schema_opt_in_and_matching_content_type() -> None:
    reference = PayloadReference(
        uri="artifact://tenant-a/run/large.json",
        expected_checksum=CHECKSUM,
        content_type="application/json",
        size_bytes=100,
    )
    denied = EventRuntime(store=_Store(_UnitOfWork()), schema_catalog=_catalog())
    with pytest.raises(EventSecurityError, match="does not permit"):
        denied.publish(_request(payload=None, payload_ref=reference))

    permitted = EventRuntime(
        store=_Store(_UnitOfWork()),
        schema_catalog=_catalog(
            policy=SensitivityPolicy(
                whole_document_reference=(
                    WholeDocumentReferenceDisposition.NON_SENSITIVE
                ),
                max_inline_payload_bytes=8,
            )
        ),
    )
    stored = permitted.publish(_request(payload=None, payload_ref=reference))
    assert stored.payload is None
    assert stored.payload_ref == reference

    with pytest.raises(ValueError, match="content_type"):
        permitted.publish(
            _request(
                content_type="application/octet-stream",
                payload=None,
                payload_ref=reference,
            )
        )


def test_protected_content_fails_closed_before_store_without_secure_composition() -> None:
    unit_of_work = _UnitOfWork()
    runtime = EventRuntime(
        store=_Store(unit_of_work),
        schema_catalog=_catalog(
            policy=SensitivityPolicy(
                whole_document_reference=(
                    WholeDocumentReferenceDisposition.SECURE_REQUIRED
                ),
                max_inline_payload_bytes=8,
            )
        ),
    )
    reference = PayloadReference(
        uri="secure://tenant-a/run/content",
        expected_checksum=CHECKSUM,
        content_type="application/json",
        size_bytes=100,
    )

    with pytest.raises(EventSecurePayloadRequiredError):
        runtime.publish(
            _request(
                payload=None,
                payload_ref=reference,
                security_classification=SecurityClassification.CONFIDENTIAL,
            )
        )

    assert unit_of_work.appended == []
