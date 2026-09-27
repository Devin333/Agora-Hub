from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.events.errors import (
    EventContractError,
    EventIntegrityError,
    EventStoreUnavailableError,
)
from framework.events.runtime.models import (
    DeadLetterPage,
    DeadLetterRecord,
    DeliveryRecord,
    QuarantineDisposition,
    QuarantinePage,
    QuarantineReason,
    QuarantineRecord,
    ReplayMode,
    ReplayReport,
    ReplayReportPage,
    ReplayStatus,
    ReplayVersion,
    RedeliveryItem,
    RedeliveryReport,
    RetirementCancellationReport,
    SubscriptionKey,
    SubscriptionStatus,
)
from framework.events.runtime.replay_engine import (
    ReplayCheckpoint,
    ReplayExecutionResult,
)
from framework.events.runtime.projection_reducer import (
    RUNTIME_PROJECTION_REDUCER_ID,
    RUNTIME_PROJECTION_REDUCER_VERSION,
    RUNTIME_PROJECTION_STATE_SCHEMA,
)
from interfaces.services.event_delivery_operations_service import (
    EventDeliveryOperationsService,
    EventOperationCapabilityUnavailableError,
    EventOperationNotFoundError,
)
from interfaces.services.event_quarantine_service import EventQuarantineService
from interfaces.services.event_reader_service import (
    EventAuthorizationContext,
    EventAuthorizationDecision,
    EventAuthorizationError,
    EventServiceAvailability,
)
from interfaces.services.event_replay_service import EventReplayService


NOW = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


class _Authorizer:
    def __init__(self, *, authorized: bool = True) -> None:
        self.authorized = authorized
        self.requests = []

    def authorize(self, request):
        self.requests.append(request)
        return EventAuthorizationDecision(
            request=request,
            authorized=self.authorized,
            authorization_evidence_ref=(
                "authz://decision/operator-1" if self.authorized else None
            ),
            denial_reason_class=None if self.authorized else "policy_denied",
        )


class _ReplayEngine:
    def __init__(self) -> None:
        self.rebuild_calls = []
        self.verify_calls = []

    def rebuild_state(self, request, **kwargs):
        self.rebuild_calls.append((request, kwargs))
        return _replay_result(
            request,
            reducer_id=kwargs["reducer_id"],
            reducer_version=kwargs["reducer_version"],
            input_checkpoint=kwargs.get("checkpoint"),
        )

    def verify_history(self, request, **kwargs):
        self.verify_calls.append((request, kwargs))
        return _replay_result(
            request,
            input_checkpoint=kwargs.get("checkpoint"),
        )


class _ReportStore:
    def __init__(self) -> None:
        self.queries = []
        self.unavailable = False

    def get_replay_report(self, replay_id, *, tenant_id=None):
        if self.unavailable:
            raise EventStoreUnavailableError("report store unavailable")
        return None

    def list_replay_reports(self, query):
        self.queries.append(query)
        if self.unavailable:
            raise EventStoreUnavailableError("report store unavailable")
        return ReplayReportPage(reports=())


class _CheckpointReader:
    def __init__(self, checkpoint=None) -> None:
        self.checkpoint = checkpoint
        self.calls = []

    def get_checkpoint(self, checkpoint_id, *, tenant_id=None):
        self.calls.append((checkpoint_id, tenant_id))
        return self.checkpoint


class _DeliveryRuntime:
    def __init__(self) -> None:
        self.requeue_calls = []
        self.redelivery_calls = []
        self.retirement_cancellation_calls = []

    def requeue_dead_letter(self, subscription, action):
        self.requeue_calls.append((subscription, action))
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
        self.redelivery_calls.append(request)
        end = request.through_sequence or request.from_sequence
        items = tuple(
            RedeliveryItem(
                redelivery_id=request.redelivery_id,
                event_id=f"event-{sequence}",
                stream_id=request.source_stream_id,
                stream_sequence=sequence,
                subscription=request.subscription,
                delivery_id=f"delivery-{sequence}",
                delivery_generation=2,
                created_at=request.requested_at,
                tenant_id=request.tenant_id,
            )
            for sequence in range(request.from_sequence, end + 1)
        )
        return RedeliveryReport(
            redelivery_id=request.redelivery_id,
            subscription=request.subscription,
            source_stream_id=request.source_stream_id,
            from_sequence=request.from_sequence,
            through_sequence=end,
            captured_high_watermark=end,
            requested_at=request.requested_at,
            scheduled_at=request.requested_at,
            operator_id=request.operator_id,
            operator_reason=request.operator_reason,
            authorization_evidence_ref=request.authorization_evidence_ref,
            items=items,
            requested_through_sequence=request.through_sequence,
            tenant_id=request.tenant_id,
        )

    def cancel_retired_subscription(self, request):
        self.retirement_cancellation_calls.append(request)
        return RetirementCancellationReport(
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


class _DeliveryStore:
    def __init__(self, *, tenant_id: str = "tenant-a") -> None:
        self.dead_letter = _dead_letter(tenant_id=tenant_id)
        self.dead_letter_queries = []
        self.retirement_cancellation = None
        self.subscription = SimpleNamespace(
            subscription_id="subscription-1",
            subscription_version=1,
            tenant_id=tenant_id,
            consumer_id="consumer-1",
            effect=SimpleNamespace(consumer_effect_id="effect-1"),
            status=SubscriptionStatus.ACTIVE,
        )

    def get_subscription(self, key):
        if key == SubscriptionKey("subscription-1", 1):
            return self.subscription
        return None

    def get_dead_letter(self, dead_letter_id, *, tenant_id=None):
        if (
            dead_letter_id == self.dead_letter.dead_letter_id
            and tenant_id == self.dead_letter.tenant_id
        ):
            return self.dead_letter
        return None

    def list_dead_letters(self, query):
        self.dead_letter_queries.append(query)
        records = (
            (self.dead_letter,)
            if query.tenant_id == self.dead_letter.tenant_id
            else ()
        )
        return DeadLetterPage(records=records)

    def resolve_dead_letter(self, action):
        return replace(
            self.dead_letter,
            disposition="resolved",
            operator_id=action.operator_id,
            operator_reason=action.reason,
            updated_at=action.requested_at,
        )

    def get_redelivery_report(self, redelivery_id, *, tenant_id=None):
        return None

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


class _QuarantineStore:
    def __init__(self) -> None:
        self.record = QuarantineRecord(
            quarantine_id="quarantine-1",
            source="legacy://events/1",
            reason=QuarantineReason.UNKNOWN_DATA_SCHEMA,
            created_at=NOW,
            tenant_id="tenant-a",
            redacted_diagnostic="unknown schema",
        )
        self.queries = []
        self.resolve_calls = []

    def get_quarantine(self, quarantine_id, *, tenant_id=None):
        if quarantine_id == self.record.quarantine_id and tenant_id == "tenant-a":
            return self.record
        return None

    def list_quarantine(self, query):
        self.queries.append(query)
        records = (self.record,) if query.tenant_id == "tenant-a" else ()
        return QuarantinePage(records=records)

    def resolve_quarantine(
        self,
        quarantine_id,
        disposition,
        *,
        operator_id,
        reason,
        resolved_at,
    ):
        self.resolve_calls.append(
            (quarantine_id, disposition, operator_id, reason, resolved_at)
        )
        return replace(
            self.record,
            disposition=disposition,
            operator_id=operator_id,
            operator_reason=reason,
            updated_at=resolved_at,
        )


def test_replay_service_builds_tenant_scoped_operator_request() -> None:
    engine = _ReplayEngine()
    service = EventReplayService(
        engine=engine,
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )
    authorization = _authorization()

    result = service.rebuild_state(
        replay_id="replay-1",
        source_stream_id="run:run-1",
        operator_reason="repair verified state",
        reducer_id="run-state",
        reducer_version="1",
        authorization=authorization,
    )

    request, kwargs = engine.rebuild_calls[0]
    assert isinstance(result, ReplayExecutionResult)
    assert request.mode is ReplayMode.REBUILD_STATE
    assert request.tenant_id == "tenant-a"
    assert request.operator_id == "operator-1"
    assert request.operator_reason == "repair verified state"
    assert kwargs["reducer_id"] == "run-state"


def test_replay_report_unavailability_is_typed_and_secret_free() -> None:
    store = _ReportStore()
    store.unavailable = True
    service = EventReplayService(
        engine=_ReplayEngine(),
        report_store=store,
        authorizer=_Authorizer(),
    )

    result = service.list_reports(
        authorization=_authorization()
    )

    assert result.availability is EventServiceAvailability.UNAVAILABLE
    assert result.unavailable_reason_class == "EventStoreUnavailableError"
    assert result.page is None


def test_replay_authorization_binds_complete_checkpoint_identity() -> None:
    engine = _ReplayEngine()
    authorizer = _Authorizer()
    service = EventReplayService(
        engine=engine,
        report_store=_ReportStore(),
        authorizer=authorizer,
        clock=lambda: NOW,
    )
    checkpoint = _input_checkpoint()

    service.rebuild_state(
        replay_id="replay-checkpoint",
        source_stream_id="run:run-1",
        operator_reason="resume authorized replay",
        reducer_id="run-state",
        reducer_version="1",
        checkpoint_ref=checkpoint.checkpoint_id,
        checkpoint=checkpoint,
        after_sequence=checkpoint.last_sequence,
        authorization=_authorization(),
    )

    target = authorizer.requests[0].target["checkpoint"]
    assert target == {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_checksum": checkpoint.checkpoint_checksum,
        "mode": ReplayMode.REBUILD_STATE.value,
        "source_stream_id": "run:run-1",
        "tenant_id": "tenant-a",
        "last_sequence": 1,
        "source_high_watermark": 1,
    }


def test_replay_rejects_checkpoint_ref_or_scope_mismatch_before_runtime() -> None:
    engine = _ReplayEngine()
    service = EventReplayService(
        engine=engine,
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )
    checkpoint = _input_checkpoint()

    with pytest.raises(ValueError, match="checkpoint_ref must match"):
        service.rebuild_state(
            replay_id="replay-checkpoint",
            source_stream_id="run:run-1",
            operator_reason="mismatched checkpoint",
            reducer_id="run-state",
            reducer_version="1",
            checkpoint_ref="checkpoint-other",
            checkpoint=checkpoint,
            authorization=_authorization(),
        )
    with pytest.raises(ValueError, match="source scope"):
        service.rebuild_state(
            replay_id="replay-checkpoint",
            source_stream_id="run:another-run",
            operator_reason="cross-scope checkpoint",
            reducer_id="run-state",
            reducer_version="1",
            checkpoint_ref=checkpoint.checkpoint_id,
            checkpoint=checkpoint,
            authorization=_authorization(),
        )

    assert engine.rebuild_calls == []


def test_replay_checkpoint_lookup_requires_authorization_before_reader_access() -> None:
    reader = _CheckpointReader(_input_checkpoint())
    service = EventReplayService(
        engine=_ReplayEngine(),
        checkpoint_reader=reader,
        report_store=_ReportStore(),
        authorizer=_Authorizer(authorized=False),
        clock=lambda: NOW,
    )

    with pytest.raises(EventAuthorizationError):
        service.rebuild_state(
            replay_id="replay-denied",
            source_stream_id="run:run-1",
            operator_reason="must not disclose checkpoint",
            reducer_id="run-state",
            reducer_version="1",
            checkpoint_ref="checkpoint-input-1",
            from_sequence=2,
            authorization=_authorization(),
        )

    assert reader.calls == []


@pytest.mark.parametrize(
    ("checkpoint_kind", "match"),
    [
        ("missing", "unavailable in tenant scope"),
        ("cross_tenant", "requested scope"),
        ("wrong_version", "requested scope"),
    ],
)
def test_replay_checkpoint_lookup_fails_closed_for_invalid_scope_or_version(
    checkpoint_kind,
    match,
) -> None:
    checkpoint = None
    if checkpoint_kind == "cross_tenant":
        checkpoint = replace(_input_checkpoint(), tenant_id="tenant-b")
    elif checkpoint_kind == "wrong_version":
        checkpoint = replace(
            _input_checkpoint(),
            reducer_version="2",
            versions=(
                ReplayVersion("replay_runtime", "runtime-1"),
                ReplayVersion("schema_catalog", "schema-1"),
                ReplayVersion("reducer:run-state", "2"),
            ),
        )
    reader = _CheckpointReader(checkpoint)
    engine = _ReplayEngine()
    service = EventReplayService(
        engine=engine,
        checkpoint_reader=reader,
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match=match):
        service.rebuild_state(
            replay_id="replay-invalid-checkpoint",
            source_stream_id="run:run-1",
            operator_reason="reject incompatible checkpoint",
            reducer_id="run-state",
            reducer_version="1",
            checkpoint_ref="checkpoint-input-1",
            from_sequence=2,
            authorization=_authorization(),
        )

    assert reader.calls == [("checkpoint-input-1", "tenant-a")]
    assert engine.rebuild_calls == []


def test_replay_checkpoint_lookup_rejects_corrupt_checksum() -> None:
    checkpoint = _input_checkpoint()
    object.__setattr__(checkpoint, "checkpoint_checksum", "sha256:" + "0" * 64)
    reader = _CheckpointReader(checkpoint)
    engine = _ReplayEngine()
    service = EventReplayService(
        engine=engine,
        checkpoint_reader=reader,
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(EventIntegrityError):
        service.rebuild_state(
            replay_id="replay-corrupt-checkpoint",
            source_stream_id="run:run-1",
            operator_reason="reject corrupt checkpoint",
            reducer_id="run-state",
            reducer_version="1",
            checkpoint_ref=checkpoint.checkpoint_id,
            from_sequence=2,
            authorization=_authorization(),
        )

    assert engine.rebuild_calls == []


def test_runtime_replay_rejects_resigned_inconsistent_state_before_noop_resume() -> None:
    checkpoint = ReplayCheckpoint(
        checkpoint_id="runtime-checkpoint-invalid-state",
        mode=ReplayMode.REBUILD_STATE,
        source_stream_id="run:run-1",
        last_sequence=2,
        source_high_watermark=2,
        runtime_version="runtime-1",
        schema_catalog_version="schema-1",
        history_checksum="sha256:" + "b" * 64,
        last_event_id="event-2",
        state={
            "state_schema": RUNTIME_PROJECTION_STATE_SCHEMA,
            "cursors": {
                "run:run-1": {
                    "stream_id": "run:run-1",
                    "sequence": 1,
                    "checksum": "sha256:" + "a" * 64,
                }
            },
            "statuses": {},
        },
        reducer_id=RUNTIME_PROJECTION_REDUCER_ID,
        reducer_version=RUNTIME_PROJECTION_REDUCER_VERSION,
        tenant_id="tenant-a",
    )
    engine = _ReplayEngine()
    service = EventReplayService(
        engine=engine,
        checkpoint_reader=_CheckpointReader(checkpoint),
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="cursor"):
        service.rebuild_state(
            replay_id="runtime-noop-resume",
            source_stream_id="run:run-1",
            operator_reason="reject invalid signed state",
            reducer_id=RUNTIME_PROJECTION_REDUCER_ID,
            reducer_version=RUNTIME_PROJECTION_REDUCER_VERSION,
            checkpoint_ref=checkpoint.checkpoint_id,
            from_sequence=3,
            authorization=_authorization(),
        )

    assert checkpoint.last_sequence == checkpoint.source_high_watermark
    assert engine.rebuild_calls == []

    direct_engine = _ReplayEngine()
    direct_service = EventReplayService(
        engine=direct_engine,
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )
    with pytest.raises(ValueError, match="cursor"):
        direct_service.rebuild_state(
            replay_id="runtime-direct-noop-resume",
            source_stream_id="run:run-1",
            operator_reason="reject direct invalid signed state",
            reducer_id=RUNTIME_PROJECTION_REDUCER_ID,
            reducer_version=RUNTIME_PROJECTION_REDUCER_VERSION,
            checkpoint_ref=checkpoint.checkpoint_id,
            checkpoint=checkpoint,
            from_sequence=3,
            authorization=_authorization(),
        )
    assert direct_engine.rebuild_calls == []


def test_delivery_mutations_receive_principal_tenant_reason_and_evidence() -> None:
    store = _DeliveryStore()
    runtime = _DeliveryRuntime()
    service = EventDeliveryOperationsService(
        store=store,
        runtime=runtime,
        retirement_cancellation_runtime=runtime,
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )
    subscription = SubscriptionKey("subscription-1", 1)

    service.requeue_dead_letter(
        subscription,
        "dead-letter-1",
        operator_reason="retry after repair",
        idempotency_ready=True,
        authorization=_authorization(),
    )
    service.begin_redelivery(
        redelivery_id="redelivery-1",
        subscription=subscription,
        source_stream_id="run:run-1",
        from_sequence=1,
        through_sequence=2,
        operator_reason="reprocess audited range",
        authorization=_authorization(),
    )

    action = runtime.requeue_calls[0][1]
    redelivery = runtime.redelivery_calls[0]
    assert action.operator_id == "operator-1"
    assert action.reason == "retry after repair"
    assert action.requested_at == NOW
    assert action.idempotency_ready is True
    assert redelivery.tenant_id == "tenant-a"
    assert redelivery.operator_id == "operator-1"
    assert redelivery.authorization_evidence_ref == "authz://decision/operator-1"


def test_dead_letter_and_quarantine_queries_are_forced_to_authorized_tenant() -> None:
    delivery_store = _DeliveryStore()
    delivery_service = EventDeliveryOperationsService(
        store=delivery_store,
        runtime=_DeliveryRuntime(),
        authorizer=_Authorizer(),
    )
    quarantine_store = _QuarantineStore()
    quarantine_service = EventQuarantineService(
        quarantine_store,
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    dead_letters = delivery_service.list_dead_letters(
        authorization=_authorization()
    )
    quarantines = quarantine_service.list(
        authorization=_authorization()
    )
    resolved = quarantine_service.resolve(
        "quarantine-1",
        QuarantineDisposition.REJECTED,
        operator_reason="invalid legacy record",
        authorization=_authorization(),
    )

    assert dead_letters.page.records == (delivery_store.dead_letter,)
    assert delivery_store.dead_letter_queries[0].tenant_id == "tenant-a"
    assert quarantines.page.records == (quarantine_store.record,)
    assert quarantine_store.queries[0].tenant_id == "tenant-a"
    assert resolved.operator_id == "operator-1"
    assert quarantine_store.resolve_calls[0][2] == "operator-1"


def test_operator_permissions_are_checked_before_runtime_mutation() -> None:
    runtime = _DeliveryRuntime()
    service = EventDeliveryOperationsService(
        store=_DeliveryStore(),
        runtime=runtime,
        authorizer=_Authorizer(authorized=False),
        clock=lambda: NOW,
    )

    with pytest.raises(EventAuthorizationError, match="not authorized"):
        service.requeue_dead_letter(
            SubscriptionKey("subscription-1", 1),
            "dead-letter-1",
            operator_reason="not authorized",
            idempotency_ready=True,
            authorization=_authorization(),
        )

    assert runtime.requeue_calls == []


def test_retirement_cancellation_uses_authorizer_evidence_before_runtime_mutation() -> None:
    store = _DeliveryStore()
    store.subscription.status = SubscriptionStatus.RETIRED
    runtime = _DeliveryRuntime()
    authorizer = _Authorizer()
    service = EventDeliveryOperationsService(
        store=store,
        runtime=runtime,
        retirement_cancellation_runtime=runtime,
        authorizer=authorizer,
        clock=lambda: NOW,
    )

    report = service.cancel_retired_subscription(
        cancellation_id="retirement-cancel-1",
        subscription=SubscriptionKey("subscription-1", 1),
        operator_reason="retired consumer is being decommissioned",
        limit=25,
        authorization=_authorization(),
    )

    request = runtime.retirement_cancellation_calls[0]
    assert authorizer.requests[0].operation.value == (
        "event.subscription_retirement.cancel"
    )
    assert request.authorization_evidence_ref == "authz://decision/operator-1"
    assert request.operator_id == "operator-1"
    assert request.tenant_id == "tenant-a"
    assert request.limit == 25
    assert report.cancellation_id == request.cancellation_id


def test_retirement_cancellation_denial_and_scope_fail_before_runtime_mutation() -> None:
    denied_runtime = _DeliveryRuntime()
    denied = EventDeliveryOperationsService(
        store=_DeliveryStore(),
        runtime=denied_runtime,
        retirement_cancellation_runtime=denied_runtime,
        authorizer=_Authorizer(authorized=False),
        clock=lambda: NOW,
    )
    with pytest.raises(EventAuthorizationError, match="not authorized"):
        denied.cancel_retired_subscription(
            cancellation_id="retirement-cancel-denied",
            subscription=SubscriptionKey("subscription-1", 1),
            operator_reason="denied request",
            authorization=_authorization(),
        )
    assert denied_runtime.retirement_cancellation_calls == []

    wrong_scope_store = _DeliveryStore(tenant_id="tenant-b")
    wrong_scope_store.subscription.status = SubscriptionStatus.RETIRED
    wrong_scope_runtime = _DeliveryRuntime()
    scoped = EventDeliveryOperationsService(
        store=wrong_scope_store,
        runtime=wrong_scope_runtime,
        retirement_cancellation_runtime=wrong_scope_runtime,
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )
    with pytest.raises(EventOperationNotFoundError, match="tenant scope"):
        scoped.cancel_retired_subscription(
            cancellation_id="retirement-cancel-cross-tenant",
            subscription=SubscriptionKey("subscription-1", 1),
            operator_reason="must not cross tenant",
            authorization=_authorization(),
        )
    assert wrong_scope_runtime.retirement_cancellation_calls == []


def test_retirement_cancellation_retry_uses_fresh_authorization_metadata() -> None:
    store = _DeliveryStore()
    store.subscription.status = SubscriptionStatus.RETIRED
    runtime = _DeliveryRuntime()
    service = EventDeliveryOperationsService(
        store=store,
        runtime=runtime,
        retirement_cancellation_runtime=runtime,
        authorizer=_Authorizer(),
        clock=lambda: NOW + timedelta(days=1),
    )
    store.retirement_cancellation = RetirementCancellationReport(
        cancellation_id="retirement-cancel-retry",
        subscription=SubscriptionKey("subscription-1", 1),
        requested_at=NOW,
        cancelled_at=NOW,
        operator_id="operator-1",
        operator_reason="retry exact command",
        authorization_evidence_ref="authz://decision/operator-1",
        item_limit=1000,
        remaining_nonterminal_count=0,
        items=(),
        tenant_id="tenant-a",
    )

    service.cancel_retired_subscription(
        cancellation_id="retirement-cancel-retry",
        subscription=SubscriptionKey("subscription-1", 1),
        operator_reason="retry exact command",
        authorization=_authorization(),
    )

    request = runtime.retirement_cancellation_calls[0]
    assert request.requested_at == NOW + timedelta(days=1)
    assert request.authorization_evidence_ref == "authz://decision/operator-1"


def test_cross_tenant_requeue_is_rejected_before_runtime_mutation() -> None:
    runtime = _DeliveryRuntime()
    service = EventDeliveryOperationsService(
        store=_DeliveryStore(tenant_id="tenant-b"),
        runtime=runtime,
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(EventOperationNotFoundError, match="subscription scope"):
        service.requeue_dead_letter(
            SubscriptionKey("subscription-1", 1),
            "dead-letter-1",
            operator_reason="must not cross tenant",
            idempotency_ready=True,
            authorization=_authorization(),
        )

    assert runtime.requeue_calls == []


def test_cross_tenant_redelivery_is_rejected_before_runtime_mutation() -> None:
    runtime = _DeliveryRuntime()
    service = EventDeliveryOperationsService(
        store=_DeliveryStore(tenant_id="tenant-b"),
        runtime=runtime,
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(EventOperationNotFoundError, match="tenant scope"):
        service.begin_redelivery(
            redelivery_id="redelivery-cross-tenant",
            subscription=SubscriptionKey("subscription-1", 1),
            source_stream_id="run:run-1",
            from_sequence=1,
            operator_reason="must not cross tenant",
            authorization=_authorization(),
        )

    assert runtime.redelivery_calls == []


def test_operator_list_results_must_honor_exact_filters() -> None:
    delivery_service = EventDeliveryOperationsService(
        store=_DeliveryStore(),
        runtime=_DeliveryRuntime(),
        authorizer=_Authorizer(),
    )
    quarantine_service = EventQuarantineService(
        _QuarantineStore(),
        authorizer=_Authorizer(),
    )

    with pytest.raises(EventContractError, match="query filters"):
        delivery_service.list_dead_letters(
            subscription_id="another-subscription",
            authorization=_authorization(),
        )
    with pytest.raises(EventContractError, match="query filters"):
        quarantine_service.list(
            reason=QuarantineReason.CORRUPT_RECORD,
            authorization=_authorization(),
        )


def test_operator_get_results_must_match_requested_identity() -> None:
    class WrongDeadLetterStore(_DeliveryStore):
        def get_dead_letter(self, dead_letter_id, *, tenant_id=None):
            return self.dead_letter

    delivery_service = EventDeliveryOperationsService(
        store=WrongDeadLetterStore(),
        runtime=_DeliveryRuntime(),
        authorizer=_Authorizer(),
    )

    with pytest.raises(EventContractError, match="another target"):
        delivery_service.get_dead_letter(
            "dead-letter-requested",
            authorization=_authorization(),
        )


def test_read_only_operator_services_do_not_require_mutation_runtimes() -> None:
    replay = EventReplayService(
        report_store=_ReportStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )
    delivery = EventDeliveryOperationsService(
        store=_DeliveryStore(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    assert replay.list_reports(authorization=_authorization()).page.reports == ()
    assert (
        delivery.list_dead_letters(authorization=_authorization()).page.records
        == (_DeliveryStore().dead_letter,)
    )
    with pytest.raises(
        EventOperationCapabilityUnavailableError,
        match="requeue capability is unavailable",
    ):
        delivery.requeue_dead_letter(
            SubscriptionKey("subscription-1", 1),
            "dead-letter-1",
            operator_reason="repair is verified",
            idempotency_ready=True,
            authorization=_authorization(),
        )
    with pytest.raises(
        EventOperationCapabilityUnavailableError,
        match="replay capability is unavailable",
    ):
        replay.verify_history(
            replay_id="replay-unavailable",
            source_stream_id="run:run-1",
            operator_reason="verify deterministic history",
            authorization=_authorization(),
        )


@pytest.mark.parametrize(
    "operator_reason",
    ["control\ncharacter", "x" * 513],
)
def test_operator_mutation_reason_is_bounded_and_control_free(
    operator_reason: str,
) -> None:
    service = EventDeliveryOperationsService(
        store=_DeliveryStore(),
        runtime=_DeliveryRuntime(),
        authorizer=_Authorizer(),
        clock=lambda: NOW,
    )

    with pytest.raises(ValueError, match="operator_reason"):
        service.resolve_dead_letter(
            "dead-letter-1",
            operator_reason=operator_reason,
            authorization=_authorization(),
        )


def test_event_application_services_do_not_import_concrete_runtime_owners() -> None:
    service_files = (
        "event_reader_service.py",
        "event_projection_service.py",
        "event_replay_service.py",
        "event_delivery_operations_service.py",
        "event_quarantine_service.py",
    )
    for filename in service_files:
        path = Path("interfaces/services") / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = []
        imported_symbols = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
                imported_symbols.extend(alias.name for alias in node.names)
        assert not any(module.startswith("infrastructure") for module in imported)
        assert not any("dispatcher" in module for module in imported)
        assert not any("executor" in module for module in imported)
        assert "framework.events.runtime.delivery" not in imported
        assert "DeterministicReplayEngine" not in imported_symbols
        assert "DurableDeliveryRuntime" not in imported_symbols


def _authorization() -> EventAuthorizationContext:
    return EventAuthorizationContext(
        principal_id="operator-1",
        tenant_id="tenant-a",
        authentication_evidence_ref="authn://session/operator-1",
    )


def _dead_letter(*, tenant_id: str = "tenant-a") -> DeadLetterRecord:
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
        tenant_id=tenant_id,
    )


def _input_checkpoint() -> ReplayCheckpoint:
    return ReplayCheckpoint(
        checkpoint_id="checkpoint-input-1",
        mode=ReplayMode.REBUILD_STATE,
        source_stream_id="run:run-1",
        last_sequence=1,
        source_high_watermark=1,
        runtime_version="runtime-1",
        schema_catalog_version="schema-1",
        history_checksum="sha256:" + "b" * 64,
        last_event_id="event-1",
        state={"count": 1},
        reducer_id="run-state",
        reducer_version="1",
        tenant_id="tenant-a",
    )


def _replay_result(
    request,
    *,
    reducer_id: str | None = None,
    reducer_version: str | None = None,
    input_checkpoint: ReplayCheckpoint | None = None,
) -> ReplayExecutionResult:
    high_watermark = (
        input_checkpoint.source_high_watermark
        if input_checkpoint is not None
        else 0
    )
    output_checkpoint = ReplayCheckpoint(
        checkpoint_id=f"checkpoint-output-{request.replay_id}",
        mode=request.mode,
        source_stream_id=request.source_stream_id,
        last_sequence=high_watermark,
        source_high_watermark=high_watermark,
        runtime_version="runtime-1",
        schema_catalog_version="schema-1",
        history_checksum="sha256:" + "c" * 64,
        last_event_id=("event-1" if high_watermark else None),
        state={} if request.mode is ReplayMode.REBUILD_STATE else None,
        reducer_id=reducer_id,
        reducer_version=reducer_version,
        parent_checkpoint_id=(
            None if input_checkpoint is None else input_checkpoint.checkpoint_id
        ),
        tenant_id=request.tenant_id,
    )
    report = ReplayReport(
        replay_id=request.replay_id,
        mode=request.mode,
        source_stream_id=request.source_stream_id,
        high_watermark=high_watermark,
        status=ReplayStatus.SUCCEEDED,
        started_at=request.requested_at,
        to_sequence=high_watermark or None,
        checkpoint_ref=output_checkpoint.checkpoint_id,
        result_checksum="sha256:" + "d" * 64,
        finished_at=request.requested_at,
        tenant_id=request.tenant_id,
        operator_id=request.operator_id,
        operator_reason=request.operator_reason,
    )
    return ReplayExecutionResult(
        report=report,
        checkpoint=output_checkpoint,
        state={} if request.mode is ReplayMode.REBUILD_STATE else None,
    )
