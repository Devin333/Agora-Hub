from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from framework.events.errors import EventContractError, EventStoreUnavailableError
from framework.events.ports import ReplayReportStorePort
from framework.events.runtime.models import (
    ReplayMode,
    ReplayReport,
    ReplayReportPage,
    ReplayReportQuery,
    ReplayStartRequest,
    ReplayStatus,
)
from framework.events.runtime.projection_reducer import (
    RUNTIME_PROJECTION_REDUCER_ID,
    RUNTIME_PROJECTION_REDUCER_VERSION,
    validate_runtime_projection_checkpoint_state,
)
from framework.shared.time import utc_now
from interfaces.services.event_delivery_operations_service import (
    EventOperationCapabilityUnavailableError,
    validate_operator_reason,
)
from framework.events.runtime.replay_engine import (
    ReplayCheckpoint,
    ReplayExecutionResult,
)
from interfaces.services.event_reader_service import (
    EventAuthorizationContext,
    EventAuthorizerPort,
    EventPermission,
    EventServiceAvailability,
    authorize_event_operation,
)


class EventReplayRuntimePort(Protocol):
    def rebuild_state(
        self,
        request: ReplayStartRequest,
        *,
        reducer_id: str,
        reducer_version: str,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult: ...

    def verify_history(
        self,
        request: ReplayStartRequest,
        *,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult: ...


class ReplayCheckpointReaderPort(Protocol):
    def get_checkpoint(
        self,
        checkpoint_id: str,
        *,
        tenant_id: str | None = None,
    ) -> ReplayCheckpoint | None: ...


@dataclass(frozen=True, slots=True)
class ReplayReportLookupResult:
    availability: EventServiceAvailability
    tenant_id: str | None
    report: ReplayReport | None = None
    unavailable_reason_class: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "availability", EventServiceAvailability(self.availability))
        if self.availability is EventServiceAvailability.AVAILABLE:
            if self.unavailable_reason_class is not None:
                raise ValueError("available replay lookup cannot have a failure reason")
        elif self.report is not None:
            raise ValueError("unavailable replay lookup cannot contain a report")
        elif self.unavailable_reason_class is None:
            raise ValueError("unavailable replay lookup requires a reason class")


@dataclass(frozen=True, slots=True)
class ReplayReportListResult:
    availability: EventServiceAvailability
    tenant_id: str | None
    page: ReplayReportPage | None = None
    unavailable_reason_class: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "availability", EventServiceAvailability(self.availability))
        if self.availability is EventServiceAvailability.AVAILABLE:
            if self.page is None:
                raise ValueError("available replay list requires a page")
            if self.unavailable_reason_class is not None:
                raise ValueError("available replay list cannot have a failure reason")
        elif self.page is not None:
            raise ValueError("unavailable replay list cannot contain a page")
        elif self.unavailable_reason_class is None:
            raise ValueError("unavailable replay list requires a reason class")


class EventReplayService:
    """Authorized application entrypoints for deterministic replay and reports."""

    def __init__(
        self,
        *,
        engine: EventReplayRuntimePort | None = None,
        checkpoint_reader: ReplayCheckpointReaderPort | None = None,
        report_store: ReplayReportStorePort,
        authorizer: EventAuthorizerPort,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        if report_store is None:
            raise ValueError("replay report store is required")
        if authorizer is None:
            raise ValueError("event authorizer is required")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._engine = engine
        self._checkpoint_reader = checkpoint_reader
        self._reports = report_store
        self._authorizer = authorizer
        self._clock = clock

    def rebuild_state(
        self,
        *,
        replay_id: str,
        source_stream_id: str,
        operator_reason: str,
        reducer_id: str,
        reducer_version: str,
        authorization: EventAuthorizationContext,
        from_sequence: int | None = None,
        checkpoint_ref: str | None = None,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult:
        checkpoint = self._resolve_checkpoint(
            mode=ReplayMode.REBUILD_STATE,
            source_stream_id=source_stream_id,
            reducer_id=reducer_id,
            reducer_version=reducer_version,
            authorization=authorization,
            checkpoint_ref=checkpoint_ref,
            checkpoint=checkpoint,
        )
        checkpoint_target = _checkpoint_target(
            mode=ReplayMode.REBUILD_STATE,
            source_stream_id=source_stream_id,
            tenant_id=authorization.tenant_id,
            checkpoint_ref=checkpoint_ref,
            checkpoint=checkpoint,
        )
        request = self._request(
            replay_id=replay_id,
            mode=ReplayMode.REBUILD_STATE,
            source_stream_id=source_stream_id,
            operator_reason=operator_reason,
            authorization=authorization,
            from_sequence=from_sequence,
            checkpoint_ref=checkpoint_ref,
            after_sequence=after_sequence,
            reducer_id=reducer_id,
            reducer_version=reducer_version,
            checkpoint_target=checkpoint_target,
        )
        engine = self._require_engine()
        result = engine.rebuild_state(
            request,
            reducer_id=reducer_id,
            reducer_version=reducer_version,
            checkpoint=checkpoint,
            after_sequence=after_sequence,
        )
        return _validated_execution_result(result, request)

    def _resolve_checkpoint(
        self,
        *,
        mode: ReplayMode,
        source_stream_id: str,
        reducer_id: str | None,
        reducer_version: str | None,
        authorization: EventAuthorizationContext,
        checkpoint_ref: str | None,
        checkpoint: ReplayCheckpoint | None,
    ) -> ReplayCheckpoint | None:
        if checkpoint is not None:
            _validate_runtime_checkpoint_state(
                checkpoint,
                reducer_id=reducer_id,
                reducer_version=reducer_version,
            )
            return checkpoint
        if checkpoint_ref is None:
            return None
        normalized_ref = _required_text(checkpoint_ref, "checkpoint_ref")
        authorize_event_operation(
            self._authorizer,
            authorization,
            EventPermission.REPLAY_START,
            target={
                "mode": mode.value,
                "source_stream_id": source_stream_id,
                "checkpoint_ref": normalized_ref,
                "reducer_id": reducer_id,
                "reducer_version": reducer_version,
                "operation": "checkpoint_lookup",
            },
        )
        if self._checkpoint_reader is None:
            raise EventOperationCapabilityUnavailableError(
                "durable replay checkpoint lookup is unavailable"
            )
        loaded = self._checkpoint_reader.get_checkpoint(
            normalized_ref,
            tenant_id=authorization.tenant_id,
        )
        if loaded is None:
            raise ValueError("replay checkpoint is unavailable in tenant scope")
        if not isinstance(loaded, ReplayCheckpoint):
            raise EventContractError("checkpoint reader returned an invalid checkpoint")
        loaded.verify_integrity()
        if (
            loaded.mode is not mode
            or loaded.source_stream_id != source_stream_id
            or loaded.tenant_id != authorization.tenant_id
            or loaded.reducer_id != reducer_id
            or loaded.reducer_version != reducer_version
        ):
            raise ValueError("replay checkpoint does not match the requested scope")
        _validate_runtime_checkpoint_state(
            loaded,
            reducer_id=reducer_id,
            reducer_version=reducer_version,
        )
        return loaded

    def verify_history(
        self,
        *,
        replay_id: str,
        source_stream_id: str,
        operator_reason: str,
        authorization: EventAuthorizationContext,
        from_sequence: int | None = None,
        checkpoint_ref: str | None = None,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult:
        checkpoint = self._resolve_checkpoint(
            mode=ReplayMode.VERIFY_HISTORY,
            source_stream_id=source_stream_id,
            reducer_id=None,
            reducer_version=None,
            authorization=authorization,
            checkpoint_ref=checkpoint_ref,
            checkpoint=checkpoint,
        )
        checkpoint_target = _checkpoint_target(
            mode=ReplayMode.VERIFY_HISTORY,
            source_stream_id=source_stream_id,
            tenant_id=authorization.tenant_id,
            checkpoint_ref=checkpoint_ref,
            checkpoint=checkpoint,
        )
        request = self._request(
            replay_id=replay_id,
            mode=ReplayMode.VERIFY_HISTORY,
            source_stream_id=source_stream_id,
            operator_reason=operator_reason,
            authorization=authorization,
            from_sequence=from_sequence,
            checkpoint_ref=checkpoint_ref,
            after_sequence=after_sequence,
            checkpoint_target=checkpoint_target,
        )
        engine = self._require_engine()
        result = engine.verify_history(
            request,
            checkpoint=checkpoint,
            after_sequence=after_sequence,
        )
        return _validated_execution_result(result, request)

    def get_report(
        self,
        replay_id: str,
        *,
        authorization: EventAuthorizationContext,
    ) -> ReplayReportLookupResult:
        normalized_replay_id = _required_text(replay_id, "replay_id")
        authorize_event_operation(
            self._authorizer,
            authorization,
            EventPermission.REPLAY_READ,
            target={"replay_id": normalized_replay_id},
        )
        try:
            report = self._reports.get_replay_report(
                normalized_replay_id,
                tenant_id=authorization.tenant_id,
            )
        except EventStoreUnavailableError as error:
            return ReplayReportLookupResult(
                availability=EventServiceAvailability.UNAVAILABLE,
                tenant_id=authorization.tenant_id,
                unavailable_reason_class=type(error).__name__,
            )
        if report is not None and (
            not isinstance(report, ReplayReport)
            or report.replay_id != normalized_replay_id
            or report.tenant_id != authorization.tenant_id
        ):
            raise EventContractError("replay report store returned another target")
        return ReplayReportLookupResult(
            availability=EventServiceAvailability.AVAILABLE,
            tenant_id=authorization.tenant_id,
            report=report,
        )

    def list_reports(
        self,
        *,
        authorization: EventAuthorizationContext,
        source_stream_id: str | None = None,
        mode: ReplayMode | None = None,
        status: ReplayStatus | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ReplayReportListResult:
        query = ReplayReportQuery(
            source_stream_id=source_stream_id,
            mode=mode,
            status=status,
            tenant_id=authorization.tenant_id,
            cursor=cursor,
            limit=limit,
        )
        authorize_event_operation(
            self._authorizer,
            authorization,
            EventPermission.REPLAY_READ,
            target={
                "source_stream_id": query.source_stream_id,
                "mode": None if query.mode is None else query.mode.value,
                "status": None if query.status is None else query.status.value,
                "cursor": query.cursor,
                "limit": query.limit,
            },
        )
        try:
            page = self._reports.list_replay_reports(query)
        except EventStoreUnavailableError as error:
            return ReplayReportListResult(
                availability=EventServiceAvailability.UNAVAILABLE,
                tenant_id=authorization.tenant_id,
                unavailable_reason_class=type(error).__name__,
            )
        if not isinstance(page, ReplayReportPage):
            raise EventContractError("replay report store returned an invalid page")
        if len(page.reports) > query.limit:
            raise EventContractError("replay report store exceeded the requested limit")
        if page.next_cursor is not None and page.next_cursor == query.cursor:
            raise EventContractError("replay report cursor did not advance")
        if len({report.replay_id for report in page.reports}) != len(page.reports):
            raise EventContractError("replay report store returned duplicate reports")
        if any(
            report.tenant_id != authorization.tenant_id
            or (
                query.source_stream_id is not None
                and report.source_stream_id != query.source_stream_id
            )
            or (query.mode is not None and report.mode is not query.mode)
            or (query.status is not None and report.status is not query.status)
            for report in page.reports
        ):
            raise EventContractError("replay report store violated the query filters")
        return ReplayReportListResult(
            availability=EventServiceAvailability.AVAILABLE,
            tenant_id=authorization.tenant_id,
            page=page,
        )

    def _request(
        self,
        *,
        replay_id: str,
        mode: ReplayMode,
        source_stream_id: str,
        operator_reason: str,
        authorization: EventAuthorizationContext,
        from_sequence: int | None,
        checkpoint_ref: str | None,
        after_sequence: int | None,
        reducer_id: str | None = None,
        reducer_version: str | None = None,
        checkpoint_target: dict[str, Any],
    ) -> ReplayStartRequest:
        request = ReplayStartRequest(
            replay_id=replay_id,
            mode=mode,
            source_stream_id=source_stream_id,
            requested_at=_clock_value(self._clock),
            from_sequence=from_sequence,
            checkpoint_ref=checkpoint_ref,
            tenant_id=authorization.tenant_id,
            operator_id=authorization.principal_id,
            operator_reason=validate_operator_reason(operator_reason),
        )
        authorize_event_operation(
            self._authorizer,
            authorization,
            EventPermission.REPLAY_START,
            target={
                "replay_id": request.replay_id,
                "mode": request.mode.value,
                "source_stream_id": request.source_stream_id,
                "from_sequence": request.from_sequence,
                "checkpoint_ref": request.checkpoint_ref,
                "checkpoint": checkpoint_target,
                "after_sequence": after_sequence,
                "reducer_id": reducer_id,
                "reducer_version": reducer_version,
                "requested_at": request.requested_at.isoformat(),
                "operator_reason": request.operator_reason,
            },
        )
        return request

    def _require_engine(self) -> EventReplayRuntimePort:
        if self._engine is None:
            raise EventOperationCapabilityUnavailableError(
                "deterministic replay capability is unavailable"
            )
        return self._engine


def _checkpoint_target(
    *,
    mode: ReplayMode,
    source_stream_id: str,
    tenant_id: str | None,
    checkpoint_ref: str | None,
    checkpoint: ReplayCheckpoint | None,
) -> dict[str, Any]:
    if (checkpoint_ref is None) != (checkpoint is None):
        raise ValueError("checkpoint_ref and checkpoint must be supplied together")
    if checkpoint is None:
        return {
            "checkpoint_id": None,
            "checkpoint_checksum": None,
            "mode": None,
            "source_stream_id": None,
            "tenant_id": None,
            "last_sequence": None,
            "source_high_watermark": None,
        }
    if not isinstance(checkpoint, ReplayCheckpoint):
        raise TypeError("checkpoint must be ReplayCheckpoint")
    checkpoint.verify_integrity()
    if checkpoint_ref != checkpoint.checkpoint_id:
        raise ValueError("checkpoint_ref must match checkpoint.checkpoint_id")
    if (
        checkpoint.mode is not mode
        or checkpoint.source_stream_id != source_stream_id
        or checkpoint.tenant_id != tenant_id
    ):
        raise ValueError("checkpoint does not match replay mode or source scope")
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_checksum": checkpoint.checkpoint_checksum,
        "mode": checkpoint.mode.value,
        "source_stream_id": checkpoint.source_stream_id,
        "tenant_id": checkpoint.tenant_id,
        "last_sequence": checkpoint.last_sequence,
        "source_high_watermark": checkpoint.source_high_watermark,
    }


def _validate_runtime_checkpoint_state(
    checkpoint: ReplayCheckpoint,
    *,
    reducer_id: str | None,
    reducer_version: str | None,
) -> None:
    if not isinstance(checkpoint, ReplayCheckpoint):
        return
    if (
        reducer_id != RUNTIME_PROJECTION_REDUCER_ID
        or reducer_version != RUNTIME_PROJECTION_REDUCER_VERSION
    ):
        return
    validate_runtime_projection_checkpoint_state(
        checkpoint.state,
        stream_id=checkpoint.source_stream_id,
        last_sequence=checkpoint.last_sequence,
    )


def _validated_execution_result(
    result: ReplayExecutionResult,
    request: ReplayStartRequest,
) -> ReplayExecutionResult:
    if not isinstance(result, ReplayExecutionResult):
        raise EventContractError("replay runtime returned an invalid execution result")
    report = result.report
    checkpoint = result.checkpoint
    checkpoint.verify_integrity()
    if (
        report.replay_id != request.replay_id
        or report.mode is not request.mode
        or report.source_stream_id != request.source_stream_id
        or report.tenant_id != request.tenant_id
        or report.operator_id != request.operator_id
        or report.operator_reason != request.operator_reason
        or checkpoint.mode is not request.mode
        or checkpoint.source_stream_id != request.source_stream_id
        or checkpoint.tenant_id != request.tenant_id
        or checkpoint.source_high_watermark != report.high_watermark
    ):
        raise EventContractError("replay runtime returned another execution scope")
    return result


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


__all__ = [
    "EventReplayRuntimePort",
    "EventReplayService",
    "ReplayCheckpointReaderPort",
    "ReplayReportListResult",
    "ReplayReportLookupResult",
]
