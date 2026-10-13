from __future__ import annotations

import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from types import SimpleNamespace

import pytest

from framework.events import default_event_schema_catalog
from framework.events.runtime.models import (
    DeadLetterPage,
    DeadLetterRecord,
    ConsumerEffectContract,
    DeliveryRecord,
    DurableSubscription,
    EventPage,
    EffectIdempotencyStrategy,
    PendingDeliveryStats,
    QuarantinePage,
    QuarantineReason,
    QuarantineRecord,
    ReplayMode,
    ReplayReport,
    ReplayReportPage,
    ReplayStatus,
    RetirementCancellationReport,
    SubscriptionKey,
    SubscriptionStatus,
)
from interfaces.models.actor import ActorContext
from interfaces.services.event_operator_factory import (
    build_event_operator_service,
    event_operator_service_from_actor,
    event_operator_service_from_env,
)
from interfaces.services.event_operator_service import (
    EventOperationCapabilityUnavailableError,
    EventOperatorApplicationService,
)
from interfaces.services.event_reader_service import EventAuthorizationError


NOW = datetime(2026, 7, 17, 2, 3, 4, tzinfo=UTC)


class _Store:
    def __init__(self) -> None:
        self.quarantine = QuarantineRecord(
            quarantine_id="quarantine-1",
            source=r"C:\private\legacy-events.jsonl",
            reason=QuarantineReason.CORRUPT_RECORD,
            created_at=NOW,
            tenant_id="tenant-a",
            redacted_diagnostic="checksum mismatch",
        )
        self.replay = ReplayReport(
            replay_id="replay-1",
            mode=ReplayMode.VERIFY_HISTORY,
            source_stream_id="run:run-1",
            high_watermark=1,
            status=ReplayStatus.SUCCEEDED,
            started_at=NOW,
            to_sequence=1,
            checkpoint_ref=r"C:\private\checkpoint.json",
            quarantine_refs=(r"C:\private\quarantine.json",),
            result_checksum="sha256:" + "a" * 64,
            finished_at=NOW,
            tenant_id="tenant-a",
            operator_id="operator-1",
            operator_reason="verified history",
        )
        self.dead_letter = _dead_letter()
        self.retirement_cancellation = None
        self.retirement_cancellation_lock = Lock()
        self.subscription = DurableSubscription(
            subscription_id="subscription-1",
            subscription_version=1,
            consumer_id="consumer-1",
            effect=ConsumerEffectContract(
                performs_external_effects=True,
                consumer_effect_id="effect-1",
                idempotency_strategy=(
                    EffectIdempotencyStrategy.TARGET_IDEMPOTENCY_KEY
                ),
            ),
            status=SubscriptionStatus.RETIRED,
            tenant_id="tenant-a",
            created_at=NOW,
            updated_at=NOW,
        )

    def get_quarantine(self, quarantine_id, *, tenant_id=None):
        if quarantine_id == "quarantine-1" and tenant_id == "tenant-a":
            return self.quarantine
        return None

    def list_quarantine(self, query):
        return QuarantinePage(
            records=(self.quarantine,) if query.tenant_id == "tenant-a" else ()
        )

    def get_replay_report(self, replay_id, *, tenant_id=None):
        if replay_id == "replay-1" and tenant_id == "tenant-a":
            return self.replay
        return None

    def list_replay_reports(self, query):
        return ReplayReportPage(
            reports=(self.replay,) if query.tenant_id == "tenant-a" else ()
        )

    def get_dead_letter(self, dead_letter_id, *, tenant_id=None):
        if dead_letter_id == "dead-letter-1" and tenant_id == "tenant-a":
            return self.dead_letter
        return None

    def list_dead_letters(self, query):
        return DeadLetterPage(
            records=(self.dead_letter,) if query.tenant_id == "tenant-a" else ()
        )

    def resolve_dead_letter(self, action):
        self.dead_letter = replace(
            self.dead_letter,
            disposition="resolved",
            operator_id=action.operator_id,
            operator_reason=action.reason,
            updated_at=action.requested_at,
        )
        return self.dead_letter

    def get_subscription(self, key):
        return self.subscription if key == SubscriptionKey("subscription-1", 1) else None

    def pending_delivery_stats(self, key, *, stream_id):
        return PendingDeliveryStats(pending_count=0, lag=0)

    def get_checkpoint(self, key, *, tenant_id=None):
        return None

    def get_stream_high_watermark(self, stream_id, *, tenant_id=None):
        return None

    def get_event(self, event_id, *, tenant_id=None):
        return None

    def read_stream(self, request):
        return EventPage(
            stream_id=request.stream_id,
            events=(),
            high_watermark=None,
            tenant_id=request.tenant_id,
        )

    def get_retirement_cancellation_report(
        self,
        cancellation_id,
        *,
        tenant_id=None,
    ):
        report = self.retirement_cancellation
        if (
            report is not None
            and report.cancellation_id == cancellation_id
            and report.tenant_id == tenant_id
        ):
            return report
        return None

    def cancel_retired_subscription(self, request):
        with self.retirement_cancellation_lock:
            if self.retirement_cancellation is None:
                self.retirement_cancellation = RetirementCancellationReport(
                    cancellation_id=request.cancellation_id,
                    subscription=request.subscription,
                    requested_at=request.requested_at,
                    cancelled_at=request.requested_at,
                    operator_id=request.operator_id,
                    operator_reason=request.operator_reason,
                    authorization_evidence_ref=request.authorization_evidence_ref,
                    item_limit=request.limit,
                    remaining_nonterminal_count=0,
                    items=(),
                    tenant_id=request.tenant_id,
                )
            return self.retirement_cancellation


class _DeliveryRuntime:
    def __init__(self, store) -> None:
        self.consumers = SimpleNamespace(store=store)
        self.actions = []

    def requeue_dead_letter(self, subscription, action):
        self.actions.append(action)
        return DeliveryRecord(
            delivery_id="delivery-requeue-1",
            event_id="event-1",
            stream_id="run:run-1",
            stream_sequence=1,
            subscription_id=subscription.subscription_id,
            subscription_version=subscription.subscription_version,
            consumer_id="consumer-1",
            consumer_effect_id="effect-1",
            tenant_id="tenant-a",
            delivery_generation=2,
            created_at=action.requested_at,
            updated_at=action.requested_at,
        )

    def begin_redelivery(self, request):
        raise AssertionError("redelivery is outside this facade")

    def cancel_retired_subscription(self, request):
        report = RetirementCancellationReport(
            cancellation_id=request.cancellation_id,
            subscription=request.subscription,
            requested_at=request.requested_at,
            cancelled_at=request.requested_at,
            operator_id=request.operator_id,
            operator_reason=request.operator_reason,
            authorization_evidence_ref=request.authorization_evidence_ref,
            item_limit=request.limit,
            remaining_nonterminal_count=0,
            items=(),
            tenant_id=request.tenant_id,
        )
        self.consumers.store.retirement_cancellation = report
        return report


def test_facade_returns_json_safe_allowlists_without_absolute_paths(tmp_path) -> None:
    store = _Store()
    runtime = _DeliveryRuntime(store)
    service = build_event_operator_service(
        _actor("events:read", "events:operate"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        delivery_runtime=runtime,
        clock=lambda: NOW,
    )

    payloads = (
        service.list_quarantine(),
        service.get_quarantine("quarantine-1"),
        service.list_replay_reports(),
        service.get_replay_report("replay-1"),
        service.list_dead_letters(),
        service.get_dead_letter("dead-letter-1"),
        service.get_consumer_status(
            subscription_id="subscription-1",
            subscription_version=1,
            stream_id="run:run-1",
        ),
    )

    encoded = json.dumps(payloads)
    assert r"C:\private" not in encoded
    assert payloads[0]["items"][0]["source"] == (
        "redacted://operator-reference/absolute-path"
    )
    assert payloads[2]["items"][0]["checkpoint_ref"] == (
        "redacted://operator-reference/absolute-path"
    )
    assert payloads[2]["items"][0]["quarantine_refs"] == [
        "redacted://operator-reference/absolute-path"
    ]
    assert payloads[0]["items"][0]["created_at"].endswith("Z")
    assert "payload" not in encoded


@pytest.mark.parametrize(
    "source",
    [
        r"C:private\legacy-events.jsonl",
        r"..\private\legacy-events.jsonl",
        "../private/legacy-events.jsonl",
        "private/legacy-events.jsonl",
    ],
)
def test_facade_redacts_drive_relative_and_local_source_paths(
    tmp_path,
    source: str,
) -> None:
    store = _Store()
    store.quarantine = replace(store.quarantine, source=source)
    service = build_event_operator_service(
        _actor("events:read"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        clock=lambda: NOW,
    )

    payload = service.get_quarantine("quarantine-1")

    assert payload["quarantine"]["source"] == (
        "redacted://operator-reference/local-path"
    )
    assert source not in json.dumps(payload)


def test_requeue_maps_acknowledgement_but_runtime_retains_capability_authority(
    tmp_path,
) -> None:
    store = _Store()
    runtime = _DeliveryRuntime(store)
    service = build_event_operator_service(
        _actor("events:operate"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        delivery_runtime=runtime,
        clock=lambda: NOW,
    )

    result = service.requeue_dead_letter(
        "dead-letter-1",
        subscription_id="subscription-1",
        subscription_version=1,
        operator_reason="repair capability verified",
        idempotency_acknowledged=True,
    )

    assert result["delivery"]["state"] == "pending"
    assert runtime.actions[0].idempotency_ready is True
    assert runtime.actions[0].operator_id == "operator-1"
    assert runtime.actions[0].requested_at == NOW


def test_retirement_cancellation_facade_returns_bounded_audit_dto(tmp_path) -> None:
    store = _Store()
    service = build_event_operator_service(
        _actor("events:operate", "events:read"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        clock=lambda: NOW,
    )

    cancelled = service.cancel_retired_subscription(
        "retirement-cancel-facade",
        subscription_id="subscription-1",
        subscription_version=1,
        operator_reason="retired worker removed from service",
        limit=10,
    )
    fetched = service.get_retirement_cancellation_report(
        "retirement-cancel-facade"
    )

    audit = cancelled["retirement_cancellation"]
    assert audit["completed"] is True
    assert audit["cancelled_count"] == 0
    assert audit["item_limit"] == 10
    assert audit["authorization_evidence_ref"].startswith("authz://")
    assert fetched["found"] is True
    assert fetched["retirement_cancellation"] == audit
    assert json.loads(json.dumps(cancelled)) == cancelled


def test_retirement_cancellation_retry_reauthorizes_across_actor_requests(
    tmp_path,
) -> None:
    store = _Store()
    first = build_event_operator_service(
        _actor("events:operate", "events:read", request_id="request-1"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        clock=lambda: NOW,
    )
    retry = build_event_operator_service(
        _actor("events:operate", "events:read", request_id="request-2"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        clock=lambda: NOW + timedelta(seconds=1),
    )

    original = first.cancel_retired_subscription(
        "retirement-cancel-across-requests",
        subscription_id="subscription-1",
        subscription_version=1,
        operator_reason="retired worker removed from service",
        limit=10,
    )
    retried = retry.cancel_retired_subscription(
        "retirement-cancel-across-requests",
        subscription_id="subscription-1",
        subscription_version=1,
        operator_reason="retired worker removed from service",
        limit=10,
    )

    assert retried == original


def test_concurrent_application_retry_has_no_read_before_write_dependency(
    tmp_path,
) -> None:
    store = _Store()
    event_storage = _storage(store)
    barrier = Barrier(8)

    def cancel(index: int):
        service = build_event_operator_service(
            _actor(
                "events:operate",
                "events:read",
                request_id=f"request-{index}",
            ),
            tenant_id="tenant-a",
            artifact_root=tmp_path,
            event_storage=event_storage,
            clock=lambda: NOW + timedelta(seconds=index),
        )
        barrier.wait(timeout=15)
        return service.cancel_retired_subscription(
            "retirement-cancel-concurrent-application",
            subscription_id="subscription-1",
            subscription_version=1,
            operator_reason="retired worker removed from service",
            limit=10,
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        reports = tuple(executor.map(cancel, range(8)))

    assert all(report == reports[0] for report in reports)


def test_build_rejects_delivery_runtime_bound_to_another_store(tmp_path) -> None:
    store = _Store()

    with pytest.raises(ValueError, match="must share event storage"):
        build_event_operator_service(
            _actor("events:operate"),
            tenant_id="tenant-a",
            artifact_root=tmp_path,
            event_storage=_storage(store),
            delivery_runtime=_DeliveryRuntime(_Store()),
        )


def test_runtime_absence_is_explicit_but_store_owned_resolve_remains_available(
    tmp_path,
) -> None:
    store = _Store()
    service = build_event_operator_service(
        _actor("events:operate"),
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        clock=lambda: NOW,
    )

    resolved = service.resolve_dead_letter(
        "dead-letter-1",
        operator_reason="terminally reviewed",
    )
    assert resolved["dead_letter"]["disposition"] == "resolved"
    store.dead_letter = _dead_letter()
    with pytest.raises(EventOperationCapabilityUnavailableError):
        service.requeue_dead_letter(
            "dead-letter-1",
            subscription_id="subscription-1",
            subscription_version=1,
            operator_reason="retry requested",
            idempotency_acknowledged=True,
        )


def test_factory_snapshots_verified_actor_permissions(tmp_path) -> None:
    store = _Store()
    actor = _actor("events:read")
    service = build_event_operator_service(
        actor,
        tenant_id="tenant-a",
        artifact_root=tmp_path,
        event_storage=_storage(store),
        clock=lambda: NOW,
    )
    actor.permissions.clear()
    actor.permissions.append("events:operate")

    assert service.list_dead_letters()["items"]
    with pytest.raises(EventAuthorizationError):
        service.resolve_dead_letter(
            "dead-letter-1",
            operator_reason="late permission mutation",
        )


def test_from_actor_composes_storage_once_and_maps_exact_permissions(
    tmp_path,
    monkeypatch,
) -> None:
    import infrastructure.storage.events.factory as storage_factory

    store = _Store()
    calls = []

    def compose(**kwargs):
        calls.append(kwargs)
        return _storage(store)

    monkeypatch.setattr(storage_factory, "durable_event_storage_from_env", compose)
    service = event_operator_service_from_actor(
        _actor("events:read"),
        env={
            "NEWS_TENANT_ID": "tenant-a",
            "NEWS_ARTIFACT_ROOT": str(tmp_path),
        },
        clock=lambda: NOW,
    )

    assert service.list_dead_letters()["items"][0]["dead_letter_id"] == (
        "dead-letter-1"
    )
    with pytest.raises(EventAuthorizationError):
        service.resolve_dead_letter(
            "dead-letter-1",
            operator_reason="must be denied",
        )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"NEWS_TENANT_ID": "tenant-a"},
        {"NEWS_EVENT_OPERATOR_PRINCIPAL_ID": "deployment-operator"},
    ],
)
def test_env_factory_fails_closed_when_identity_scope_is_missing(env) -> None:
    with pytest.raises(ValueError, match="NEWS_"):
        event_operator_service_from_env(env=env)


def test_public_facade_methods_cannot_accept_identity_or_time_fields() -> None:
    method_names = (
        "list_quarantine",
        "get_quarantine",
        "list_replay_reports",
        "get_replay_report",
        "list_dead_letters",
        "get_dead_letter",
        "resolve_dead_letter",
        "requeue_dead_letter",
        "get_consumer_status",
        "get_projection_status",
    )
    forbidden = {"tenant_id", "operator_id", "requested_at", "resolved_at"}

    for method_name in method_names:
        parameters = inspect.signature(
            getattr(EventOperatorApplicationService, method_name)
        ).parameters
        assert forbidden.isdisjoint(parameters)


def _actor(*permissions: str, request_id: str = "request-1") -> ActorContext:
    return ActorContext(
        actor_id="operator-1",
        actor_type="user",
        roles=[],
        permissions=list(permissions),
        request_id=request_id,
    )


def _storage(store):
    return SimpleNamespace(
        event_store=store,
        schema_catalog=default_event_schema_catalog(),
    )


def _dead_letter() -> DeadLetterRecord:
    return DeadLetterRecord(
        dead_letter_id="dead-letter-1",
        delivery_id="delivery-1",
        event_id="event-1",
        stream_id="run:run-1",
        stream_sequence=1,
        subscription_id="subscription-1",
        subscription_version=1,
        consumer_id="consumer-1",
        consumer_effect_id="effect-1",
        delivery_generation=1,
        attempt_count=3,
        first_failure_at=NOW,
        last_failure_at=NOW,
        reason_class="permanent_failure",
        redacted_diagnostic="bounded diagnostic",
        tenant_id="tenant-a",
    )
