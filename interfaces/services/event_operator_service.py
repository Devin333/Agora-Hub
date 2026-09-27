from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit

from framework.events.runtime.models import (
    MAX_RETIREMENT_CANCELLATION_ITEMS,
    ConsumerCheckpoint,
    DeadLetterDisposition,
    DeadLetterRecord,
    DeliveryRecord,
    PendingDeliveryStats,
    QuarantineDisposition,
    QuarantineReason,
    QuarantineRecord,
    ReplayMode,
    ReplayReport,
    ReplayStatus,
    RetirementCancellationItem,
    RetirementCancellationReport,
    SubscriptionKey,
)
from framework.events.runtime.projection_reducer import (
    RUNTIME_PROJECTION_REDUCER_ID,
    RUNTIME_PROJECTION_REDUCER_VERSION,
)
from framework.events.runtime.replay_engine import ReplayCheckpoint
from interfaces.services.event_delivery_operations_service import (
    ConsumerDeliveryStatusResult,
    EventDeliveryOperationsService,
    EventOperationCapabilityUnavailableError,
    EventOperationNotFoundError,
)
from interfaces.services.event_projection_service import (
    EventProjectionNotFoundError,
    EventProjectionService,
    EventProjectionStatusResult,
)
from interfaces.services.event_quarantine_service import EventQuarantineService
from interfaces.services.event_reader_service import (
    EventAuthorizationContext,
    EventServiceAvailability,
)
from interfaces.services.event_replay_service import EventReplayService


class EventOperatorApplicationService:
    """Request-scoped, tenant-bound operator facade with public DTO allowlists."""

    def __init__(
        self,
        *,
        authorization: EventAuthorizationContext,
        quarantine: EventQuarantineService,
        replay: EventReplayService,
        delivery: EventDeliveryOperationsService,
        projection: EventProjectionService,
    ) -> None:
        if not isinstance(authorization, EventAuthorizationContext):
            raise TypeError("authorization must be EventAuthorizationContext")
        if authorization.tenant_id is None:
            raise ValueError("event operator service requires a tenant scope")
        for value, expected, field_name in (
            (quarantine, EventQuarantineService, "quarantine"),
            (replay, EventReplayService, "replay"),
            (delivery, EventDeliveryOperationsService, "delivery"),
            (projection, EventProjectionService, "projection"),
        ):
            if not isinstance(value, expected):
                raise TypeError(f"{field_name} service is invalid")
        self._authorization = authorization
        self._quarantine = quarantine
        self._replay = replay
        self._delivery = delivery
        self._projection = projection

    def list_quarantine(
        self,
        *,
        reason: QuarantineReason | str | None = None,
        disposition: QuarantineDisposition | str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        result = self._quarantine.list(
            authorization=self._authorization,
            reason=_optional_enum(QuarantineReason, reason),
            disposition=_optional_enum(QuarantineDisposition, disposition),
            cursor=cursor,
            limit=limit,
        )
        page = result.page
        return _list_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            items=() if page is None else page.records,
            serializer=_quarantine_record,
            next_cursor=None if page is None else page.next_cursor,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def get_quarantine(self, quarantine_id: str) -> dict[str, Any]:
        result = self._quarantine.get(
            quarantine_id,
            authorization=self._authorization,
        )
        return _lookup_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            key="quarantine",
            value=result.record,
            serializer=_quarantine_record,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def list_replay_reports(
        self,
        *,
        source_stream_id: str | None = None,
        mode: ReplayMode | str | None = None,
        status: ReplayStatus | str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        result = self._replay.list_reports(
            authorization=self._authorization,
            source_stream_id=source_stream_id,
            mode=_optional_enum(ReplayMode, mode),
            status=_optional_enum(ReplayStatus, status),
            cursor=cursor,
            limit=limit,
        )
        page = result.page
        return _list_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            items=() if page is None else page.reports,
            serializer=_replay_report,
            next_cursor=None if page is None else page.next_cursor,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def get_replay_report(self, replay_id: str) -> dict[str, Any]:
        result = self._replay.get_report(
            replay_id,
            authorization=self._authorization,
        )
        return _lookup_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            key="replay_report",
            value=result.report,
            serializer=_replay_report,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def rebuild_runtime_projection(
        self,
        *,
        replay_id: str,
        source_stream_id: str,
        operator_reason: str,
        from_sequence: int | None = None,
        checkpoint_ref: str | None = None,
    ) -> dict[str, Any]:
        result = self._replay.rebuild_state(
            replay_id=replay_id,
            source_stream_id=source_stream_id,
            operator_reason=operator_reason,
            reducer_id=RUNTIME_PROJECTION_REDUCER_ID,
            reducer_version=RUNTIME_PROJECTION_REDUCER_VERSION,
            authorization=self._authorization,
            from_sequence=from_sequence,
            checkpoint_ref=checkpoint_ref,
        )
        return {
            "availability": EventServiceAvailability.AVAILABLE.value,
            "tenant_id": self._authorization.tenant_id,
            "replay_report": _replay_report(result.report),
            "checkpoint": _replay_checkpoint(result.checkpoint),
        }

    def list_dead_letters(
        self,
        *,
        subscription_id: str | None = None,
        subscription_version: int | None = None,
        disposition: DeadLetterDisposition | str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        result = self._delivery.list_dead_letters(
            authorization=self._authorization,
            subscription_id=subscription_id,
            subscription_version=subscription_version,
            disposition=_optional_enum(DeadLetterDisposition, disposition),
            cursor=cursor,
            limit=limit,
        )
        page = result.page
        return _list_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            items=() if page is None else page.records,
            serializer=_dead_letter_record,
            next_cursor=None if page is None else page.next_cursor,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def get_dead_letter(self, dead_letter_id: str) -> dict[str, Any]:
        result = self._delivery.get_dead_letter(
            dead_letter_id,
            authorization=self._authorization,
        )
        return _lookup_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            key="dead_letter",
            value=result.record,
            serializer=_dead_letter_record,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def resolve_dead_letter(
        self,
        dead_letter_id: str,
        *,
        operator_reason: str,
    ) -> dict[str, Any]:
        record = self._delivery.resolve_dead_letter(
            dead_letter_id,
            operator_reason=operator_reason,
            authorization=self._authorization,
        )
        return {
            "availability": EventServiceAvailability.AVAILABLE.value,
            "tenant_id": self._authorization.tenant_id,
            "dead_letter": _dead_letter_record(record),
        }

    def requeue_dead_letter(
        self,
        dead_letter_id: str,
        *,
        subscription_id: str,
        subscription_version: int,
        operator_reason: str,
        idempotency_acknowledged: bool,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_acknowledged, bool):
            raise TypeError("idempotency_acknowledged must be a boolean")
        delivery = self._delivery.requeue_dead_letter(
            SubscriptionKey(subscription_id, subscription_version),
            dead_letter_id,
            operator_reason=operator_reason,
            idempotency_ready=idempotency_acknowledged,
            authorization=self._authorization,
        )
        return {
            "availability": EventServiceAvailability.AVAILABLE.value,
            "tenant_id": self._authorization.tenant_id,
            "delivery": _delivery_record(delivery),
        }

    def cancel_retired_subscription(
        self,
        cancellation_id: str,
        *,
        subscription_id: str,
        subscription_version: int,
        operator_reason: str,
        limit: int = MAX_RETIREMENT_CANCELLATION_ITEMS,
    ) -> dict[str, Any]:
        report = self._delivery.cancel_retired_subscription(
            cancellation_id=cancellation_id,
            subscription=SubscriptionKey(subscription_id, subscription_version),
            operator_reason=operator_reason,
            authorization=self._authorization,
            limit=limit,
        )
        return {
            "availability": EventServiceAvailability.AVAILABLE.value,
            "tenant_id": self._authorization.tenant_id,
            "retirement_cancellation": _retirement_cancellation_report(report),
        }

    def get_retirement_cancellation_report(
        self,
        cancellation_id: str,
    ) -> dict[str, Any]:
        result = self._delivery.get_retirement_cancellation_report(
            cancellation_id,
            authorization=self._authorization,
        )
        return _lookup_response(
            availability=result.availability,
            tenant_id=result.tenant_id,
            key="retirement_cancellation",
            value=result.report,
            serializer=_retirement_cancellation_report,
            unavailable_reason_class=result.unavailable_reason_class,
        )

    def get_consumer_status(
        self,
        subscription_id: str,
        *,
        subscription_version: int,
        stream_id: str,
    ) -> dict[str, Any]:
        result = self._delivery.get_consumer_status(
            SubscriptionKey(subscription_id, subscription_version),
            stream_id=stream_id,
            authorization=self._authorization,
        )
        return _consumer_status(result)

    def get_projection_status(self, run_id: str) -> dict[str, Any]:
        try:
            result = self._projection.get_run_projection_status_from_manifest(
                run_id,
                authorization=self._authorization,
            )
        except EventProjectionNotFoundError:
            raise EventOperationNotFoundError(
                "run projection is not available in tenant scope"
            ) from None
        return _projection_status(run_id, result)


def _list_response(
    *,
    availability: EventServiceAvailability,
    tenant_id: str | None,
    items: tuple[Any, ...],
    serializer,
    next_cursor: str | None,
    unavailable_reason_class: str | None,
) -> dict[str, Any]:
    return {
        "availability": availability.value,
        "tenant_id": tenant_id,
        "items": [serializer(item) for item in items],
        "next_cursor": next_cursor,
        "unavailable_reason_class": unavailable_reason_class,
    }


def _lookup_response(
    *,
    availability: EventServiceAvailability,
    tenant_id: str | None,
    key: str,
    value: Any,
    serializer,
    unavailable_reason_class: str | None,
) -> dict[str, Any]:
    return {
        "availability": availability.value,
        "tenant_id": tenant_id,
        "found": value is not None,
        key: None if value is None else serializer(value),
        "unavailable_reason_class": unavailable_reason_class,
    }


def _quarantine_record(record: QuarantineRecord) -> dict[str, Any]:
    return {
        "quarantine_id": record.quarantine_id,
        "source": _public_reference(record.source),
        "reason": record.reason.value,
        "envelope_schema": record.envelope_schema,
        "event_type": record.event_type,
        "data_schema": record.data_schema,
        "redacted_diagnostic": record.redacted_diagnostic,
        "disposition": record.disposition.value,
        "created_at": _utc_z(record.created_at),
        "updated_at": _utc_z(record.updated_at),
        "operator_id": record.operator_id,
        "operator_reason": record.operator_reason,
    }


def _replay_report(report: ReplayReport) -> dict[str, Any]:
    return {
        "replay_id": report.replay_id,
        "mode": report.mode.value,
        "source_stream_id": report.source_stream_id,
        "high_watermark": report.high_watermark,
        "status": report.status.value,
        "from_sequence": report.from_sequence,
        "to_sequence": report.to_sequence,
        "checkpoint_ref": _public_reference(report.checkpoint_ref),
        "versions": [
            {"component": version.component, "version": version.version}
            for version in report.versions
        ],
        "applied_upcasters": list(report.applied_upcasters),
        "quarantine_refs": [
            _public_reference(reference) for reference in report.quarantine_refs
        ],
        "mismatch_sequence": report.mismatch_sequence,
        "reason_class": report.reason_class,
        "result_checksum": report.result_checksum,
        "started_at": _utc_z(report.started_at),
        "finished_at": _utc_z(report.finished_at),
        "operator_id": report.operator_id,
        "operator_reason": report.operator_reason,
    }


def _replay_checkpoint(checkpoint: ReplayCheckpoint) -> dict[str, Any]:
    return {
        "checkpoint_id": _public_reference(checkpoint.checkpoint_id),
        "checkpoint_checksum": checkpoint.checkpoint_checksum,
        "mode": checkpoint.mode.value,
        "source_stream_id": checkpoint.source_stream_id,
        "last_sequence": checkpoint.last_sequence,
        "source_high_watermark": checkpoint.source_high_watermark,
        "last_event_id": checkpoint.last_event_id,
        "reducer_id": checkpoint.reducer_id,
        "reducer_version": checkpoint.reducer_version,
        "parent_checkpoint_id": _public_reference(checkpoint.parent_checkpoint_id),
        "tenant_id": checkpoint.tenant_id,
        "history_checksum": checkpoint.history_checksum,
    }


def _dead_letter_record(record: DeadLetterRecord) -> dict[str, Any]:
    return {
        "dead_letter_id": record.dead_letter_id,
        "delivery_id": record.delivery_id,
        "event_id": record.event_id,
        "stream_id": record.stream_id,
        "stream_sequence": record.stream_sequence,
        "subscription_id": record.subscription_id,
        "subscription_version": record.subscription_version,
        "consumer_id": record.consumer_id,
        "consumer_effect_id": record.consumer_effect_id,
        "delivery_generation": record.delivery_generation,
        "attempt_count": record.attempt_count,
        "first_failure_at": _utc_z(record.first_failure_at),
        "last_failure_at": _utc_z(record.last_failure_at),
        "reason_class": record.reason_class,
        "redacted_diagnostic": record.redacted_diagnostic,
        "disposition": record.disposition.value,
        "operator_id": record.operator_id,
        "operator_reason": record.operator_reason,
        "updated_at": _utc_z(record.updated_at),
    }


def _delivery_record(record: DeliveryRecord) -> dict[str, Any]:
    return {
        "delivery_id": record.delivery_id,
        "event_id": record.event_id,
        "stream_id": record.stream_id,
        "stream_sequence": record.stream_sequence,
        "subscription_id": record.subscription_id,
        "subscription_version": record.subscription_version,
        "consumer_id": record.consumer_id,
        "consumer_effect_id": record.consumer_effect_id,
        "delivery_generation": record.delivery_generation,
        "state": record.state.value,
        "attempt_count": record.attempt_count,
        "available_at": _utc_z(record.available_at),
        "reason_class": record.reason_class,
        "redacted_diagnostic": record.redacted_diagnostic,
        "created_at": _utc_z(record.created_at),
        "updated_at": _utc_z(record.updated_at),
    }


def _retirement_cancellation_report(
    report: RetirementCancellationReport,
) -> dict[str, Any]:
    return {
        "cancellation_id": report.cancellation_id,
        "subscription": {
            "subscription_id": report.subscription.subscription_id,
            "subscription_version": report.subscription.subscription_version,
        },
        "requested_at": _utc_z(report.requested_at),
        "cancelled_at": _utc_z(report.cancelled_at),
        "operator_id": report.operator_id,
        "operator_reason": report.operator_reason,
        "authorization_evidence_ref": _public_reference(
            report.authorization_evidence_ref
        ),
        "item_limit": report.item_limit,
        "cancelled_count": report.cancelled_count,
        "remaining_nonterminal_count": report.remaining_nonterminal_count,
        "remaining_nonterminal_count_truncated": (
            report.remaining_nonterminal_count_truncated
        ),
        "completed": report.completed,
        "items": [_retirement_cancellation_item(item) for item in report.items],
    }


def _retirement_cancellation_item(
    item: RetirementCancellationItem,
) -> dict[str, Any]:
    return {
        "delivery_id": item.delivery_id,
        "event_id": item.event_id,
        "stream_id": item.stream_id,
        "stream_sequence": item.stream_sequence,
        "delivery_generation": item.delivery_generation,
        "previous_state": item.previous_state.value,
        "previous_attempt_count": item.previous_attempt_count,
        "previous_reason_class": item.previous_reason_class,
        "terminal_state": item.terminal_state.value,
        "cancelled_at": _utc_z(item.cancelled_at),
    }


def _consumer_status(result: ConsumerDeliveryStatusResult) -> dict[str, Any]:
    return {
        "availability": result.availability.value,
        "tenant_id": result.tenant_id,
        "found": result.found,
        "subscription": {
            "subscription_id": result.subscription.subscription_id,
            "subscription_version": result.subscription.subscription_version,
        },
        "stream_id": result.stream_id,
        "stats": None if result.stats is None else _pending_stats(result.stats),
        "checkpoint": (
            None if result.checkpoint is None else _consumer_checkpoint(result.checkpoint)
        ),
        "unavailable_reason_class": result.unavailable_reason_class,
    }


def _pending_stats(stats: PendingDeliveryStats) -> dict[str, Any]:
    return {
        "pending_count": stats.pending_count,
        "lag": stats.lag,
        "oldest_pending_at": _utc_z(stats.oldest_pending_at),
        "oldest_pending_age_seconds": stats.oldest_pending_age_seconds,
        "late_repair_pending_count": stats.late_repair_pending_count,
        "warning_threshold_reached": stats.warning_threshold_reached,
        "capacity_remaining": stats.capacity_remaining,
    }


def _consumer_checkpoint(checkpoint: ConsumerCheckpoint) -> dict[str, Any]:
    return {
        "highest_contiguous_terminal_sequence": (
            checkpoint.highest_contiguous_terminal_sequence
        ),
        "last_event_id": checkpoint.last_event_id,
        "terminal_disposition": (
            None
            if checkpoint.terminal_disposition is None
            else checkpoint.terminal_disposition.value
        ),
        "updated_at": _utc_z(checkpoint.updated_at),
        "checkpoint_version": checkpoint.checkpoint_version,
        "checksum": checkpoint.checksum,
    }


def _projection_status(
    run_id: str,
    result: EventProjectionStatusResult,
) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "tenant_id": result.tenant_id,
        "run_id": run_id,
        "stream_id": result.stream_id,
        "durable_high_watermark": result.durable_high_watermark,
        "projection_high_watermark": result.projection_high_watermark,
        "projection_event_count": result.projection_event_count,
        "projection_checksum": result.projection_checksum,
        "unavailable_reason_class": result.unavailable_reason_class,
    }


def _optional_enum(enum_type, value):
    return None if value is None else enum_type(value)


def _utc_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _public_reference(value: str | None) -> str | None:
    if value is None:
        return None
    if len(value) > 512 or any(ord(character) < 32 for character in value):
        return "redacted://operator-reference/invalid"
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute() or PurePosixPath(value).is_absolute():
        return "redacted://operator-reference/absolute-path"
    if windows_path.drive:
        return "redacted://operator-reference/local-path"
    parsed = urlsplit(value)
    if parsed.scheme.casefold() == "file":
        return "redacted://operator-reference/absolute-path"
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return "redacted://operator-reference/sensitive"
    normalized_path = parsed.path.replace("\\", "/")
    if ".." in PurePosixPath(normalized_path).parts:
        return "redacted://operator-reference/local-path"
    if not parsed.scheme and ("/" in value or "\\" in value):
        return "redacted://operator-reference/local-path"
    return value


__all__ = [
    "EventOperationCapabilityUnavailableError",
    "EventOperationNotFoundError",
    "EventOperatorApplicationService",
]
