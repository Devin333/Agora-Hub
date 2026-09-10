from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
import re
from time import perf_counter
from typing import TYPE_CHECKING, Any

from framework.events.canonical import (
    BusinessContext,
    EventCandidate,
    PayloadReference,
    ProducerIdentity,
    StoredEvent,
    TraceBlock,
    assert_same_event_identity,
    normalize_canonical_json,
)
from framework.events.errors import (
    EventContractError,
    EventIdentityCollisionError,
    EventStoreContentionError,
    EventStreamVersionConflictError,
)
from framework.events.runtime.fallback import (
    LocalRuntimeDiagnosticFallback,
    RuntimeDiagnosticCategory,
    RuntimeDiagnosticComponent,
    RuntimeDiagnosticOperation,
)
from framework.events.runtime.models import AppendResult, TransactionalStateSnapshot
from framework.events.schema.catalog import EventSchemaCatalog
from framework.events.schema.security import (
    EventSecurityProjector,
    SecurityClassification,
)
from framework.events.telemetry import (
    EventTelemetry,
    TelemetryInstrumentationScope,
    TelemetryResource,
    default_event_telemetry,
)

if TYPE_CHECKING:
    from framework.events.ports import EventStorePort, EventUnitOfWorkPort


MAX_ATOMIC_PUBLISH_BATCH_SIZE = 64
_CHECKSUM_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class EventPublishRequest:
    """Immutable producer input before schema and security acceptance.

    The request deliberately has no store-assigned fields or checksums.  Its
    payload and extensions are detached from caller-owned objects immediately,
    but only ``EventRuntime`` may turn it into a post-security
    :class:`EventCandidate`.
    """

    event_id: str
    event_type: str
    data_schema: str
    source: str
    occurred_at: datetime
    stream_id: str
    business_context: BusinessContext
    producer: ProducerIdentity
    subject: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    trace: TraceBlock | None = None
    tenant_id: str | None = None
    security_classification: SecurityClassification | str = SecurityClassification.INTERNAL
    content_type: str = "application/json"
    payload: Mapping[str, Any] | None = None
    payload_ref: PayloadReference | Mapping[str, Any] | None = None
    extensions: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "event_type",
            "data_schema",
            "source",
            "stream_id",
            "content_type",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "occurred_at", _required_utc(self.occurred_at))
        for field_name in ("subject", "correlation_id", "causation_id", "tenant_id"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "security_classification",
            SecurityClassification(self.security_classification),
        )

        if not isinstance(self.business_context, BusinessContext):
            object.__setattr__(
                self,
                "business_context",
                BusinessContext.from_dict(self.business_context),
            )
        if not isinstance(self.producer, ProducerIdentity):
            object.__setattr__(
                self,
                "producer",
                ProducerIdentity.from_dict(self.producer),
            )
        if self.trace is not None and not isinstance(self.trace, TraceBlock):
            object.__setattr__(self, "trace", TraceBlock.from_dict(self.trace))

        if self.payload is not None and self.payload_ref is not None:
            raise ValueError("payload and payload_ref are mutually exclusive")
        if self.payload is not None:
            normalized_payload = normalize_canonical_json(self.payload, path="$.payload")
            if not isinstance(normalized_payload, Mapping):
                raise TypeError("payload must be an object")
            object.__setattr__(self, "payload", normalized_payload)
        if self.payload_ref is not None and not isinstance(
            self.payload_ref,
            PayloadReference,
        ):
            if not isinstance(self.payload_ref, Mapping):
                raise TypeError("payload_ref must be a PayloadReference or object")
            object.__setattr__(
                self,
                "payload_ref",
                PayloadReference.from_dict(self.payload_ref),
            )

        normalized_extensions = normalize_canonical_json(
            self.extensions,
            path="$.extensions",
        )
        if not isinstance(normalized_extensions, Mapping):
            raise TypeError("extensions must be an object")
        object.__setattr__(self, "extensions", normalized_extensions)


class EventRuntime:
    """The sole live boundary that can construct a durable event candidate."""

    def __init__(
        self,
        *,
        store: EventStorePort,
        schema_catalog: EventSchemaCatalog,
        security_projector: EventSecurityProjector | None = None,
        diagnostic_fallback: LocalRuntimeDiagnosticFallback | None = None,
        telemetry: EventTelemetry | None = None,
        backend: str = "unknown",
        monotonic: Callable[[], float] = perf_counter,
    ) -> None:
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._store = store
        self._schema_catalog = schema_catalog
        self._security_projector = security_projector or EventSecurityProjector()
        self._diagnostic_fallback = (
            diagnostic_fallback
            if diagnostic_fallback is not None
            else LocalRuntimeDiagnosticFallback()
        )
        self._telemetry = telemetry or default_event_telemetry(
            resource=TelemetryResource(service_name="newsroom-event-runtime"),
            scope=TelemetryInstrumentationScope(
                name="framework.events.publisher",
                version="1",
            ),
        )
        self._backend = _backend_metric_bucket(backend)
        self._monotonic = monotonic

    @property
    def diagnostic_fallback(self) -> LocalRuntimeDiagnosticFallback:
        return self._diagnostic_fallback

    def publish(
        self,
        event: EventPublishRequest,
        *,
        expected_last_sequence: int | None = None,
        unit_of_work: EventUnitOfWorkPort | None = None,
    ) -> StoredEvent:
        if not isinstance(event, EventPublishRequest):
            raise TypeError("event must be EventPublishRequest")
        expected_last_sequence = _expected_last_sequence(expected_last_sequence)
        candidate = self._candidate_from_request(event)

        # Only the transaction owner can observe whether an append became durable.
        owns_commit = unit_of_work is None
        append_started = _safe_monotonic(self._monotonic) if owns_commit else None
        try:
            if unit_of_work is not None:
                result = _append_verified(
                    unit_of_work,
                    candidate,
                    expected_last_sequence=expected_last_sequence,
                )
            else:
                with self._store.unit_of_work() as owned_unit_of_work:
                    result = _append_verified(
                        owned_unit_of_work,
                        candidate,
                        expected_last_sequence=expected_last_sequence,
                    )
                    owned_unit_of_work.commit()
        except Exception as error:
            if owns_commit:
                self._record_append_metrics(
                    result="failed",
                    started_at=append_started,
                )
            store_health_failure = _is_store_health_failure(error)
            if store_health_failure:
                self._telemetry.record_gauge(
                    "event_store_health",
                    0,
                    labels={"backend": self._backend},
                )
            if isinstance(error, EventIdentityCollisionError):
                self._telemetry.add_counter(
                    "event_identity_collision_total",
                    labels={"source": _source_metric_bucket(event.source)},
                )
            if store_health_failure:
                self._diagnostic_fallback.record(
                    category=RuntimeDiagnosticCategory.EVENT_STORE_FAILURE,
                    component=RuntimeDiagnosticComponent.EVENT_PUBLISHER,
                    operation=RuntimeDiagnosticOperation.PUBLISH,
                    error=error,
                )
            raise
        if owns_commit:
            self._record_append_metrics(
                result="accepted" if result.created else "duplicate",
                started_at=append_started,
            )
            self._telemetry.record_gauge(
                "event_store_health",
                1,
                labels={"backend": self._backend},
            )
        return result.event

    def publish_batch(
        self,
        events: Sequence[EventPublishRequest],
        *,
        expected_last_sequence: int | None = None,
    ) -> tuple[StoredEvent, ...]:
        """Commit a same-stream batch as one authoritative visibility boundary."""

        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise TypeError("events must be a Sequence of EventPublishRequest")
        batch_size = len(events)
        if batch_size == 0:
            raise ValueError("events must contain at least one publish request")
        if batch_size > MAX_ATOMIC_PUBLISH_BATCH_SIZE:
            raise ValueError(
                "events exceeds MAX_ATOMIC_PUBLISH_BATCH_SIZE="
                f"{MAX_ATOMIC_PUBLISH_BATCH_SIZE}"
            )
        requests = tuple(events)
        for request in requests:
            if not isinstance(request, EventPublishRequest):
                raise TypeError("every batch item must be an EventPublishRequest")
        expected_last_sequence = _expected_last_sequence(expected_last_sequence)
        stream_scope = (requests[0].tenant_id, requests[0].stream_id)
        if any(
            (request.tenant_id, request.stream_id) != stream_scope
            for request in requests[1:]
        ):
            raise EventContractError(
                "atomic publish batch requires one tenant and stream scope"
            )

        # Validate, project, and freeze every candidate before opening the
        # transaction so a schema failure cannot allocate a stream sequence.
        candidates = tuple(
            self._candidate_from_request(request) for request in requests
        )
        append_started = _safe_monotonic(self._monotonic)
        results: list[AppendResult] = []
        try:
            with self._store.unit_of_work() as unit_of_work:
                next_expected = expected_last_sequence
                for candidate in candidates:
                    result = _append_verified(
                        unit_of_work,
                        candidate,
                        expected_last_sequence=next_expected,
                    )
                    if results and (
                        result.event.stream_sequence
                        != results[-1].event.stream_sequence + 1
                    ):
                        raise EventContractError(
                            "atomic publish batch did not receive contiguous stream sequences"
                        )
                    if (
                        not results
                        and expected_last_sequence is not None
                        and result.event.stream_sequence != expected_last_sequence + 1
                    ):
                        raise EventContractError(
                            "atomic publish batch did not start after expected_last_sequence"
                        )
                    results.append(result)
                    next_expected = result.event.stream_sequence
                unit_of_work.commit()
        except Exception as error:
            self._record_append_metrics(
                result="failed",
                started_at=append_started,
            )
            store_health_failure = _is_store_health_failure(error)
            if store_health_failure:
                self._telemetry.record_gauge(
                    "event_store_health",
                    0,
                    labels={"backend": self._backend},
                )
            if isinstance(error, EventIdentityCollisionError):
                self._telemetry.add_counter(
                    "event_identity_collision_total",
                    labels={"source": _source_metric_bucket(requests[0].source)},
                )
            if store_health_failure:
                self._diagnostic_fallback.record(
                    category=RuntimeDiagnosticCategory.EVENT_STORE_FAILURE,
                    component=RuntimeDiagnosticComponent.EVENT_PUBLISHER,
                    operation=RuntimeDiagnosticOperation.PUBLISH,
                    error=error,
                )
            raise

        for result in results:
            self._telemetry.add_counter(
                "event_append_total",
                labels={
                    "backend": self._backend,
                    "result": "accepted" if result.created else "duplicate",
                },
            )
        completed_at = _safe_monotonic(self._monotonic)
        if append_started is not None and completed_at is not None:
            self._telemetry.record_histogram(
                "event_append_latency_seconds",
                max(0.0, completed_at - append_started),
                labels={"backend": self._backend},
            )
        self._telemetry.record_gauge(
            "event_store_health",
            1,
            labels={"backend": self._backend},
        )
        return tuple(result.event for result in results)

    def publish_batch_with_state_cas(
        self,
        events: Sequence[EventPublishRequest],
        *,
        expected_last_sequence: int | None,
        state_namespace: str,
        state_key: str,
        expected_state_revision: int | None,
        expected_state_checksum: str | None,
        next_state: TransactionalStateSnapshot,
    ) -> tuple[tuple[StoredEvent, ...], TransactionalStateSnapshot]:
        """Commit one canonical event batch and one monotonic state CAS."""

        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            raise TypeError("events must be a Sequence of EventPublishRequest")
        batch_size = len(events)
        if batch_size == 0:
            raise ValueError("events must contain at least one publish request")
        if batch_size > MAX_ATOMIC_PUBLISH_BATCH_SIZE:
            raise ValueError(
                "events exceeds MAX_ATOMIC_PUBLISH_BATCH_SIZE="
                f"{MAX_ATOMIC_PUBLISH_BATCH_SIZE}"
            )
        requests = tuple(events)
        for request in requests:
            if not isinstance(request, EventPublishRequest):
                raise TypeError("every batch item must be an EventPublishRequest")
        expected_last_sequence = _expected_last_sequence(expected_last_sequence)
        stream_scope = (requests[0].tenant_id, requests[0].stream_id)
        if any(
            (request.tenant_id, request.stream_id) != stream_scope
            for request in requests[1:]
        ):
            raise EventContractError(
                "atomic publish batch requires one tenant and stream scope"
            )

        namespace = _required_text(state_namespace, "state_namespace")
        key = _required_text(state_key, "state_key")
        if not isinstance(next_state, TransactionalStateSnapshot):
            raise TypeError("next_state must be TransactionalStateSnapshot")
        if (next_state.namespace, next_state.key) != (namespace, key):
            raise ValueError("next_state namespace/key must match the CAS target")
        expected_revision, expected_checksum = _state_cas_expectation(
            expected_state_revision,
            expected_state_checksum,
        )

        # Candidate/schema/security validation occurs before opening a database
        # transaction.  The state row itself is nevertheless validated and
        # compared only by the adapter through the transaction connection.
        candidates = tuple(
            self._candidate_from_request(request) for request in requests
        )
        append_started = _safe_monotonic(self._monotonic)
        results: list[AppendResult] = []
        persisted_state: TransactionalStateSnapshot | None = None
        try:
            with self._store.unit_of_work() as unit_of_work:
                state_before = unit_of_work.load_transactional_state(namespace, key)
                persisted_state = unit_of_work.cas_transactional_state(
                    namespace=namespace,
                    key=key,
                    expected_revision=expected_revision,
                    expected_checksum=expected_checksum,
                    next_snapshot=next_state,
                )
                if persisted_state != next_state:
                    raise EventContractError(
                        "transactional state CAS returned a different snapshot"
                    )
                exact_state_redelivery = state_before == persisted_state == next_state
                next_expected = expected_last_sequence
                for candidate in candidates:
                    result = _append_verified(
                        unit_of_work,
                        candidate,
                        expected_last_sequence=next_expected,
                    )
                    if results and (
                        result.event.stream_sequence
                        != results[-1].event.stream_sequence + 1
                    ):
                        raise EventContractError(
                            "atomic publish batch did not receive contiguous stream sequences"
                        )
                    if (
                        not results
                        and expected_last_sequence is not None
                        and result.event.stream_sequence != expected_last_sequence + 1
                    ):
                        raise EventContractError(
                            "atomic publish batch did not start after expected_last_sequence"
                        )
                    results.append(result)
                    next_expected = result.event.stream_sequence

                if exact_state_redelivery:
                    if any(result.created for result in results):
                        raise EventContractError(
                            "state redelivery cannot commit previously unseen events"
                        )
                elif any(not result.created for result in results):
                    raise EventContractError(
                        "new state transition cannot reuse a previously committed event"
                    )
                unit_of_work.commit()
        except Exception as error:
            self._record_append_metrics(result="failed", started_at=append_started)
            store_health_failure = _is_store_health_failure(error)
            if store_health_failure:
                self._telemetry.record_gauge(
                    "event_store_health",
                    0,
                    labels={"backend": self._backend},
                )
            if isinstance(error, EventIdentityCollisionError):
                self._telemetry.add_counter(
                    "event_identity_collision_total",
                    labels={"source": _source_metric_bucket(requests[0].source)},
                )
            if store_health_failure:
                self._diagnostic_fallback.record(
                    category=RuntimeDiagnosticCategory.EVENT_STORE_FAILURE,
                    component=RuntimeDiagnosticComponent.EVENT_PUBLISHER,
                    operation=RuntimeDiagnosticOperation.PUBLISH,
                    error=error,
                )
            raise

        for result in results:
            self._telemetry.add_counter(
                "event_append_total",
                labels={
                    "backend": self._backend,
                    "result": "accepted" if result.created else "duplicate",
                },
            )
        completed_at = _safe_monotonic(self._monotonic)
        if append_started is not None and completed_at is not None:
            self._telemetry.record_histogram(
                "event_append_latency_seconds",
                max(0.0, completed_at - append_started),
                labels={"backend": self._backend},
            )
        self._telemetry.record_gauge(
            "event_store_health",
            1,
            labels={"backend": self._backend},
        )
        if persisted_state is None:  # pragma: no cover - guarded by successful CAS
            raise EventContractError("transactional state CAS returned no snapshot")
        return tuple(result.event for result in results), persisted_state

    def compare_and_swap_transactional_state(
        self,
        next_snapshot: TransactionalStateSnapshot,
        *,
        expected_revision: int | None,
        expected_checksum: str | None,
    ) -> TransactionalStateSnapshot:
        """Commit one state CAS without exposing an adapter transaction handle."""

        if not isinstance(next_snapshot, TransactionalStateSnapshot):
            raise TypeError("next_snapshot must be TransactionalStateSnapshot")
        expected_revision, expected_checksum = _state_cas_expectation(
            expected_revision,
            expected_checksum,
        )
        try:
            with self._store.unit_of_work() as unit_of_work:
                persisted = unit_of_work.cas_transactional_state(
                    namespace=next_snapshot.namespace,
                    key=next_snapshot.key,
                    expected_revision=expected_revision,
                    expected_checksum=expected_checksum,
                    next_snapshot=next_snapshot,
                )
                if not isinstance(persisted, TransactionalStateSnapshot):
                    raise EventContractError(
                        "transactional state CAS returned an invalid snapshot"
                    )
                if persisted != next_snapshot:
                    raise EventContractError(
                        "transactional state CAS returned a different snapshot"
                    )
                unit_of_work.commit()
        except Exception as error:
            if _is_store_health_failure(error):
                self._telemetry.record_gauge(
                    "event_store_health",
                    0,
                    labels={"backend": self._backend},
                )
                self._diagnostic_fallback.record(
                    category=RuntimeDiagnosticCategory.EVENT_STORE_FAILURE,
                    component=RuntimeDiagnosticComponent.EVENT_PUBLISHER,
                    operation=RuntimeDiagnosticOperation.PUBLISH,
                    error=error,
                )
            raise
        self._telemetry.record_gauge(
            "event_store_health",
            1,
            labels={"backend": self._backend},
        )
        return persisted

    def _candidate_from_request(self, event: EventPublishRequest) -> EventCandidate:
        registration = self._schema_catalog.get(event.event_type, event.data_schema)
        policy = registration.sensitivity_policy

        validated_payload: Mapping[str, Any] | None
        if event.payload_ref is None:
            validated_payload = self._schema_catalog.prepare_publish_payload(
                event.event_type,
                event.data_schema,
                event.payload or {},
                business_context=event.business_context,
            )
        else:
            # Referenced bytes are not fetched through the event runtime.  The
            # shared projector owns the schema reference disposition and proves
            # the ordinary or secure integrity boundary before append.
            validated_payload = None

        projection = self._security_projector.project(
            payload=validated_payload,
            payload_ref=event.payload_ref,
            extensions=event.extensions,
            policy=policy,
            classification=event.security_classification,
            tenant_id=event.tenant_id,
        )
        payload_ref = (
            None
            if projection.payload_ref is None
            else PayloadReference.from_dict(projection.payload_ref)
        )
        return EventCandidate(
            event_id=event.event_id,
            event_type=event.event_type,
            data_schema=event.data_schema,
            source=event.source,
            occurred_at=event.occurred_at,
            stream_id=event.stream_id,
            business_context=event.business_context,
            producer=event.producer,
            subject=event.subject,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            trace=event.trace,
            tenant_id=projection.tenant_id,
            security_classification=projection.classification,
            content_type=event.content_type,
            payload=projection.payload,
            payload_ref=payload_ref,
            extensions=projection.extensions,
            max_inline_payload_bytes=policy.max_inline_payload_bytes,
        )

    def _record_append_metrics(
        self,
        *,
        result: str,
        started_at: float | None,
    ) -> None:
        self._telemetry.add_counter(
            "event_append_total",
            labels={"backend": self._backend, "result": result},
        )
        completed_at = _safe_monotonic(self._monotonic)
        if started_at is not None and completed_at is not None:
            self._telemetry.record_histogram(
                "event_append_latency_seconds",
                max(0.0, completed_at - started_at),
                labels={"backend": self._backend},
            )


def _append_verified(
    unit_of_work: EventUnitOfWorkPort,
    candidate: EventCandidate,
    *,
    expected_last_sequence: int | None,
) -> AppendResult:
    result = unit_of_work.append_event(
        candidate,
        expected_last_sequence=expected_last_sequence,
    )
    if not isinstance(result, AppendResult):
        raise EventContractError("durable event store returned an invalid append result")
    assert_same_event_identity(result.event, candidate)
    result.event.verify_integrity()
    return result


def _expected_last_sequence(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("expected_last_sequence must be an integer or None")
    if value < 0:
        raise ValueError("expected_last_sequence must not be negative")
    return value


def _state_cas_expectation(
    revision: int | None,
    checksum: str | None,
) -> tuple[int | None, str | None]:
    if (revision is None) != (checksum is None):
        raise ValueError(
            "expected_state_revision and expected_state_checksum must both be "
            "set or both be None"
        )
    if revision is None:
        return None, None
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise TypeError("expected_state_revision must be an integer or None")
    if revision < 1:
        raise ValueError("expected_state_revision must be positive")
    if (
        not isinstance(checksum, str)
        or _CHECKSUM_PATTERN.fullmatch(checksum) is None
    ):
        raise ValueError(
            "expected_state_checksum must be sha256:<64 lowercase hex> or None"
        )
    return revision, checksum


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _required_utc(value: Any) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("occurred_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


def _backend_metric_bucket(value: Any) -> str:
    normalized = str(value).strip().lower() if isinstance(value, str) else "unknown"
    aliases = {"postgres": "postgresql", "postgresql": "postgresql", "sqlite": "sqlite"}
    return aliases.get(normalized, normalized if normalized in {"memory", "unknown"} else "unknown")


def _source_metric_bucket(value: Any) -> str:
    normalized = str(value).strip().lower() if isinstance(value, str) else ""
    for bucket in ("graph", "harness", "migration", "tool", "api"):
        if normalized == bucket or normalized.startswith(f"{bucket}.") or normalized.startswith(
            f"{bucket}:"
        ):
            return bucket
    if normalized.startswith("interfaces.api"):
        return "api"
    return "unknown"


def _safe_monotonic(clock: Callable[[], float]) -> float | None:
    try:
        value = float(clock())
    except Exception:
        return None
    return value if isfinite(value) else None


def _is_store_health_failure(error: BaseException) -> bool:
    return not isinstance(
        error,
        (
            EventIdentityCollisionError,
            EventStoreContentionError,
            EventStreamVersionConflictError,
        ),
    )


__all__ = ["EventPublishRequest", "EventRuntime"]
