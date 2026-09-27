from __future__ import annotations

import ast
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from framework.events.canonical import (
    BusinessContext,
    EventCandidate,
    PayloadReference,
    ProducerIdentity,
    StoredEvent,
    checksum_for,
)
from framework.events.errors import EventIntegrityError
from framework.events.runtime.models import (
    EventPage,
    QuarantineRecord,
    ReplayMode,
    ReplayReport,
    ReplayStartRequest,
    ReplayStatus,
    ReplayVersion,
    StreamReadRequest,
    StreamSequenceCursor,
)
from framework.events.runtime.history import (
    DETERMINISTIC_HISTORY_EXTENSION,
    DeterministicHistoryRecord,
    DeterministicCommand,
    ExactVersionRegistration,
    ExactVersionRegistry,
    HistoryEventPolicy,
    HistoryVerifier,
)
from framework.events.runtime.activities import (
    ReplayActivityDescriptor,
    ReplayActivityKind,
)
from framework.events.runtime.replay_engine import (
    DeterministicReplayEngine,
    ReplayCheckpoint,
    ReplayCheckpointError,
    ReplayCoreError,
    ReplayHistoryIntegrityError,
    ReplayHistoryOrderError,
    ReplayHistorySchemaError,
    ReplayHistoryVerificationError,
    ReplayModeError,
    ReplayRedeliveryDelegationRequired,
    ReplayReducerRegistration,
    ReplayReducerRegistrationError,
    ReplayReducerRegistry,
    ReplaySourceReadError,
)
from framework.events.schema import EventSchemaCatalog, EventSchemaRegistration
from framework.events.telemetry import EventTelemetry


ROOT = Path(__file__).resolve().parents[3]
STARTED_AT = datetime(2026, 7, 15, 2, 0, tzinfo=UTC)
FINISHED_AT = STARTED_AT + timedelta(seconds=5)
STREAM_ID = "run:replay-1"


class _MetricBackend:
    def __init__(self) -> None:
        self.counters: list[tuple[str, int, dict[str, str]]] = []

    def add_counter(self, name, value, *, attributes) -> None:
        self.counters.append((name, value, dict(attributes)))

    def record_histogram(self, name, value, *, attributes) -> None:
        return None

    def record_gauge(self, name, value, *, attributes) -> None:
        return None

    def start_span(self, name, *, attributes, links):  # pragma: no cover
        raise AssertionError("replay metrics do not start spans")
TENANT_ID = "tenant-replay"


def _upcast_value(payload: Any) -> dict[str, Any]:
    return {"value": payload["value"], "label": f"value-{payload['value']}"}


def _sum_reducer(state: Any, event: Any) -> dict[str, Any]:
    return {
        "total": state["total"] + event.payload["value"],
        "seen": [*state["seen"], event.stream_sequence],
    }


def _impure_reducer(state: Any, event: Any) -> Any:
    with open("should-never-open", encoding="utf-8") as stream:  # pragma: no cover
        return {"state": state, "event": event, "value": stream.read()}


def _reflection_escape_reducer(state: Any, event: Any) -> Any:
    builtins = event.checksum_projection.__globals__["__builtins__"]
    opener = builtins["open"]
    with opener(state["path"], "a", encoding="utf-8") as stream:
        stream.write("escaped\n")
    return state


def _import_reducer(state: Any, event: Any) -> Any:
    import os as imported_os

    return {"state": state, "event": event, "cwd": imported_os.getcwd()}


def _getattr_reducer(state: Any, event: Any) -> Any:
    return {"state": state, "payload": getattr(event, "payload")}


def _unordered_reducer(state: Any, event: Any) -> Any:
    return {"items": list(frozenset(event.payload["items"])), "state": state}


def _set_literal_reducer(state: Any, event: Any) -> Any:
    return {"items": list({*event.payload["items"]}), "state": state}


def _catalog(*, with_upcast: bool = True) -> EventSchemaCatalog:
    catalog = EventSchemaCatalog()
    catalog.register(
        EventSchemaRegistration(
            event_type="io.newsroom.counter",
            data_schema="io.newsroom.counter/v1",
            json_schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            },
            upcast_to="io.newsroom.counter/v2" if with_upcast else None,
            upcaster=_upcast_value if with_upcast else None,
        )
    )
    catalog.register(
        EventSchemaRegistration(
            event_type="io.newsroom.counter",
            data_schema="io.newsroom.counter/v2",
            json_schema={
                "type": "object",
                "required": ["value", "label"],
                "properties": {
                    "value": {"type": "integer"},
                    "label": {"type": "string"},
                },
                "additionalProperties": False,
            },
            current=True,
        )
    )
    return catalog


def _event(
    sequence: int,
    *,
    data_schema: str = "io.newsroom.counter/v2",
    stream_id: str = STREAM_ID,
    tenant_id: str | None = TENANT_ID,
    recorded_commands: tuple[DeterministicCommand, ...] = (),
    history_policy: HistoryEventPolicy | None = None,
    malformed_history: bool = False,
) -> StoredEvent:
    payload: dict[str, Any] = {"value": sequence}
    if data_schema.endswith("/v2"):
        payload["label"] = f"value-{sequence}"
    candidate = EventCandidate(
        event_id=f"evt-replay-{sequence}",
        event_type="io.newsroom.counter",
        data_schema=data_schema,
        source="tests.replay",
        occurred_at=STARTED_AT + timedelta(seconds=sequence),
        stream_id=stream_id,
        business_context=BusinessContext(run_id="replay-1"),
        producer=ProducerIdentity(component="replay-tests", version="1"),
        tenant_id=tenant_id,
        payload=payload,
        extensions={
            DETERMINISTIC_HISTORY_EXTENSION: (
                "not-an-object"
                if malformed_history
                else DeterministicHistoryRecord(
                    policy=history_policy or _history_policy(),
                    commands=recorded_commands,
                ).to_dict()
            )
        },
    )
    return StoredEvent(
        candidate,
        observed_at=STARTED_AT + timedelta(seconds=sequence, milliseconds=1),
        stream_sequence=sequence,
    )


def _request(
    replay_id: str,
    mode: ReplayMode,
    *,
    stream_id: str = STREAM_ID,
    checkpoint_ref: str | None = None,
    from_sequence: int | None = None,
) -> ReplayStartRequest:
    return ReplayStartRequest(
        replay_id=replay_id,
        mode=mode,
        source_stream_id=stream_id,
        requested_at=STARTED_AT,
        from_sequence=from_sequence,
        checkpoint_ref=checkpoint_ref,
        tenant_id=TENANT_ID,
        operator_id="operator-1" if mode is ReplayMode.REDELIVER else None,
        operator_reason="authorized repair" if mode is ReplayMode.REDELIVER else None,
    )


def _registry() -> ReplayReducerRegistry:
    registry = ReplayReducerRegistry()
    registry.register(
        ReplayReducerRegistration(
            reducer_id="counter",
            version="2.1.0",
            reducer=_sum_reducer,
            initial_state={"total": 0, "seen": []},
        )
    )
    return registry


def _history_policy() -> HistoryEventPolicy:
    return HistoryEventPolicy(
        handler_id="counter-history",
        handler_version="1",
        graph_id="counter-graph",
        graph_version="1",
        policy_id="counter-policy",
        policy_version="1",
        schema_id="counter-schema",
        schema_version="2",
    )


def _history_handler_for(commands: tuple[DeterministicCommand, ...]):
    def handler(
        _event: Any,
        _handler_input: Any,
        _activity_result: Any,
    ) -> tuple[DeterministicCommand, ...]:
        return commands

    return handler


def _history_verifier(
    *,
    expected: tuple[DeterministicCommand, ...] = (),
) -> HistoryVerifier:
    handler = _history_handler_for(expected)
    versions = ExactVersionRegistry()
    for kind, component_id, version, value in (
        ("graph", "counter-graph", "1", "pinned"),
        ("policy", "counter-policy", "1", "pinned"),
        ("schema", "counter-schema", "2", "pinned"),
        ("reducer", "counter-history", "1", handler),
    ):
        versions.register(
            ExactVersionRegistration(kind, component_id, version, value)
        )
    return HistoryVerifier(versions=versions)


class _ProcessCrash(BaseException):
    pass


class _FakeCheckpointStore:
    def __init__(self) -> None:
        self.records: dict[str, ReplayCheckpoint] = {}
        self.read_overrides: dict[str, ReplayCheckpoint] = {}
        self.allow_unscoped_override = False
        self.save_calls = 0
        self.fail_reads = False
        self.fail_writes = False
        self.secret = "checkpoint-store-secret-token"

    def save_checkpoint(self, checkpoint: ReplayCheckpoint) -> ReplayCheckpoint:
        self.save_calls += 1
        if self.fail_writes:
            raise RuntimeError(self.secret)
        existing = self.records.get(checkpoint.checkpoint_id)
        if existing is not None:
            if checkpoint.last_sequence < existing.last_sequence:
                raise ValueError("checkpoint cannot move backwards")
            if (
                checkpoint.source_stream_id != existing.source_stream_id
                or checkpoint.source_high_watermark != existing.source_high_watermark
            ):
                raise ValueError("checkpoint slot identity changed")
        self.records[checkpoint.checkpoint_id] = checkpoint
        return checkpoint

    def get_checkpoint(
        self,
        checkpoint_id: str,
        *,
        tenant_id: str | None = None,
    ) -> ReplayCheckpoint | None:
        if self.fail_reads:
            raise RuntimeError(self.secret)
        if checkpoint_id in self.read_overrides:
            checkpoint = self.read_overrides[checkpoint_id]
            return (
                checkpoint
                if self.allow_unscoped_override or checkpoint.tenant_id == tenant_id
                else None
            )
        checkpoint = self.records.get(checkpoint_id)
        if checkpoint is None or checkpoint.tenant_id != tenant_id:
            return None
        return checkpoint


class _FakeReplayStore:
    def __init__(self, events: list[StoredEvent]) -> None:
        self.events = list(events)
        self.checkpoints = _FakeCheckpointStore()
        self.reports: dict[str, ReplayReport] = {}
        self.report_history: list[ReplayReport] = []
        self.read_requests: list[StreamReadRequest] = []
        self.quarantine: list[QuarantineRecord] = []
        self.fail_after_reads: int | None = None
        self.crash_after_reads: int | None = None
        self.fail_begin = False
        self.fail_update_calls: set[int] = set()
        self.crash_update_calls: set[int] = set()
        self.fail_quarantine = False
        self.update_calls = 0
        self.secret = "event-store-secret-token"
        self.live_append: StoredEvent | None = None
        self.forced_page: Any | None = None
        self._live_appended = False

    def begin_replay(self, request: ReplayStartRequest) -> ReplayReport:
        if self.fail_begin:
            raise RuntimeError(self.secret)
        existing = self.reports.get(request.replay_id)
        if existing is not None:
            return existing
        matching = [
            event
            for event in self.events
            if event.stream_id == request.source_stream_id
            and event.tenant_id == request.tenant_id
        ]
        high_watermark = max((event.stream_sequence for event in matching), default=0)
        if high_watermark < 1:
            raise ValueError("replay source stream is empty")
        if request.from_sequence is not None and request.from_sequence > high_watermark:
            raise ValueError("from_sequence exceeds source high watermark")
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
        self.reports[report.replay_id] = report
        self.report_history.append(report)
        return report

    def update_replay_report(self, report: ReplayReport) -> ReplayReport:
        self.update_calls += 1
        if self.update_calls in self.crash_update_calls:
            raise _ProcessCrash("simulated process death before report transition")
        if self.update_calls in self.fail_update_calls:
            raise RuntimeError(self.secret)
        current = self.reports[report.replay_id]
        allowed = {
            ReplayStatus.PENDING: {ReplayStatus.RUNNING, ReplayStatus.FAILED},
            ReplayStatus.RUNNING: {
                ReplayStatus.RUNNING,
                ReplayStatus.SUCCEEDED,
                ReplayStatus.FAILED,
            },
            ReplayStatus.SUCCEEDED: {ReplayStatus.SUCCEEDED},
            ReplayStatus.FAILED: {ReplayStatus.FAILED},
        }
        if report.status not in allowed[current.status]:
            raise ValueError("invalid replay report transition")
        self.reports[report.replay_id] = report
        self.report_history.append(report)
        return report

    def read_stream(self, request: StreamReadRequest) -> EventPage:
        self.read_requests.append(request)
        if (
            self.crash_after_reads is not None
            and len(self.read_requests) > self.crash_after_reads
        ):
            raise _ProcessCrash("simulated process death")
        if (
            self.fail_after_reads is not None
            and len(self.read_requests) > self.fail_after_reads
        ):
            raise RuntimeError(self.secret)
        if self.live_append is not None and not self._live_appended:
            self.events.append(self.live_append)
            self._live_appended = True
        if self.forced_page is not None:
            page = self.forced_page
            self.forced_page = None
            return page
        after_sequence = request.cursor.after_sequence if request.cursor is not None else 0
        current = max(
            (
                event.stream_sequence
                for event in self.events
                if event.stream_id == request.stream_id
                and event.tenant_id == request.tenant_id
            ),
            default=0,
        )
        high_watermark = min(request.through_sequence or current, current)
        available = sorted(
            (
                event
                for event in self.events
                if event.stream_id == request.stream_id
                and event.tenant_id == request.tenant_id
                and after_sequence < event.stream_sequence <= high_watermark
            ),
            key=lambda event: event.stream_sequence,
        )
        selected = tuple(available[: request.limit])
        next_cursor = None
        if len(available) > request.limit and selected:
            next_cursor = StreamSequenceCursor(
                stream_id=request.stream_id,
                after_sequence=selected[-1].stream_sequence,
                high_watermark=high_watermark,
                tenant_id=request.tenant_id,
            )
        return EventPage(
            stream_id=request.stream_id,
            events=selected,
            high_watermark=high_watermark,
            next_cursor=next_cursor,
            tenant_id=request.tenant_id,
        )

    def save_quarantine(self, record: QuarantineRecord) -> QuarantineRecord:
        if self.fail_quarantine:
            raise RuntimeError(self.secret)
        existing = next(
            (
                item
                for item in self.quarantine
                if item.quarantine_id == record.quarantine_id
            ),
            None,
        )
        if existing is not None:
            return existing
        self.quarantine.append(record)
        return record


def _engine(
    store: _FakeReplayStore,
    *,
    catalog: EventSchemaCatalog | None = None,
    page_size: int = 1,
    clock: Any = None,
    history_verifier: HistoryVerifier | None = None,
    telemetry: EventTelemetry | None = None,
) -> DeterministicReplayEngine:
    return DeterministicReplayEngine(
        store,  # type: ignore[arg-type]
        catalog or _catalog(),
        _registry(),
        store.checkpoints,
        runtime_version="19.0.0",
        schema_catalog_version="counter-catalog/2",
        clock=clock or (lambda: FINISHED_AT),
        page_size=page_size,
        history_verifier=history_verifier or _history_verifier(),
        telemetry=telemetry,
    )


def test_rebuild_state_pins_versions_upcasts_and_never_mutates_source() -> None:
    store = _FakeReplayStore(
        [_event(1, data_schema="io.newsroom.counter/v1"), _event(2)]
    )
    source_before = [event.to_dict() for event in store.events]

    result = _engine(store).rebuild_state(
        _request("rebuild-1", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    )

    assert result.state == {"total": 3, "seen": (1, 2)}
    assert result.report.status is ReplayStatus.SUCCEEDED
    assert result.report.high_watermark == 2
    assert result.report.to_sequence == 2
    assert result.report.applied_upcasters == (
        "1:io.newsroom.counter/v1->io.newsroom.counter/v2",
    )
    assert [(item.component, item.version) for item in result.report.versions] == [
        ("replay_runtime", "19.0.0"),
        ("schema_catalog", "counter-catalog/2"),
        ("reducer:counter", "2.1.0"),
    ]
    assert result.report.result_checksum == checksum_for(result.state)
    assert result.report.checkpoint_ref == result.checkpoint.checkpoint_id
    assert store.checkpoints.records[result.checkpoint.checkpoint_id] == result.checkpoint
    assert [event.to_dict() for event in store.events] == source_before
    assert all(request.through_sequence == 2 for request in store.read_requests)
    assert store.read_requests[1].cursor is not None
    assert store.read_requests[1].cursor.after_sequence == 1
    statuses = [report.status for report in store.report_history]
    assert statuses[0] is ReplayStatus.PENDING
    assert ReplayStatus.RUNNING in statuses
    assert statuses[-1] is ReplayStatus.SUCCEEDED


def test_exact_terminal_retry_reuses_success_without_source_read_or_write() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    request = _request("rebuild-exact-retry", ReplayMode.REBUILD_STATE)
    first = _engine(store, page_size=2).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    read_count = len(store.read_requests)
    update_count = store.update_calls
    checkpoint_save_count = store.checkpoints.save_calls
    history_count = len(store.report_history)

    retried = _engine(store, page_size=2).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )

    assert retried.report == first.report
    assert retried.checkpoint == first.checkpoint
    assert retried.state == first.state
    assert len(store.read_requests) == read_count
    assert store.update_calls == update_count
    assert store.checkpoints.save_calls == checkpoint_save_count
    assert len(store.report_history) == history_count

    with pytest.raises(ReplayCheckpointError, match="after_sequence_mismatch"):
        _engine(store, page_size=2).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
            after_sequence=1,
        )
    assert len(store.read_requests) == read_count


@pytest.mark.parametrize(
    "drift_request",
    [
        _request("rebuild-terminal-drift", ReplayMode.REBUILD_STATE, from_sequence=1),
        replace(
            _request("rebuild-terminal-reason", ReplayMode.REBUILD_STATE),
            operator_reason="different reason",
        ),
    ],
)
def test_terminal_retry_rejects_request_identity_drift(
    drift_request: ReplayStartRequest,
) -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    original = _request(
        drift_request.replay_id,
        ReplayMode.REBUILD_STATE,
    )
    _engine(store).rebuild_state(
        original,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    with pytest.raises(ReplayCoreError):
        _engine(store).rebuild_state(
            drift_request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )


def test_terminal_retry_rejects_corrupt_output_checkpoint() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    request = _request("rebuild-corrupt-terminal", ReplayMode.REBUILD_STATE)
    first = _engine(store).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    corrupt = first.checkpoint
    object.__setattr__(corrupt, "checkpoint_checksum", "sha256:" + "0" * 64)
    store.checkpoints.records[corrupt.checkpoint_id] = corrupt

    with pytest.raises(ReplaySourceReadError, match="corrupt_checkpoint"):
        _engine(store).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )


@pytest.mark.parametrize(
    ("field", "replacement", "reason_class"),
    [
        ("source_stream_id", "run:other-stream", "checkpoint_stream_mismatch"),
        ("tenant_id", "tenant-other", "checkpoint_tenant_mismatch"),
    ],
)
def test_terminal_retry_rejects_output_checkpoint_scope_drift(
    field: str,
    replacement: str,
    reason_class: str,
) -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    request = _request("rebuild-terminal-scope-drift", ReplayMode.REBUILD_STATE)
    first = _engine(store).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    tampered = replace(first.checkpoint, **{field: replacement})
    store.checkpoints.records[tampered.checkpoint_id] = tampered
    if field == "tenant_id":
        store.checkpoints.read_overrides[tampered.checkpoint_id] = tampered
        store.checkpoints.allow_unscoped_override = True

    with pytest.raises(ReplayCheckpointError) as caught:
        _engine(store).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )
    assert caught.value.reason_class == reason_class


def test_terminal_retry_rejects_output_checkpoint_version_and_result_drift() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    request = _request("rebuild-terminal-version-drift", ReplayMode.REBUILD_STATE)
    first = _engine(store).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    versions = tuple(
        ReplayVersion(item.component, "other-runtime")
        if item.component == "replay_runtime"
        else item
        for item in first.checkpoint.versions
    )
    version_drift = replace(
        first.checkpoint,
        runtime_version="other-runtime",
        versions=versions,
    )
    store.checkpoints.records[version_drift.checkpoint_id] = version_drift
    with pytest.raises(ReplayCheckpointError) as caught:
        _engine(store).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )
    assert caught.value.reason_class == "checkpoint_runtime_version_mismatch"

    result_drift = replace(first.checkpoint, state={"total": 999, "seen": [1, 2]})
    store.checkpoints.records[result_drift.checkpoint_id] = result_drift
    with pytest.raises(ReplaySourceReadError) as caught:
        _engine(store).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )
    assert caught.value.reason_class == "replay_result_checksum_mismatch"


def test_terminal_retry_rejects_output_parent_checkpoint_drift() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    parent = _engine(store, page_size=2).rebuild_state(
        _request("rebuild-terminal-parent", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    ).checkpoint
    store.events.append(_event(3))
    request = _request(
        "rebuild-terminal-child",
        ReplayMode.REBUILD_STATE,
        checkpoint_ref=parent.checkpoint_id,
        from_sequence=3,
    )
    first = _engine(store).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
        checkpoint=parent,
        after_sequence=parent.last_sequence,
    )
    tampered = replace(first.checkpoint, parent_checkpoint_id="other-parent")
    store.checkpoints.records[tampered.checkpoint_id] = tampered

    with pytest.raises(ReplaySourceReadError) as caught:
        _engine(store).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
            checkpoint=parent,
            after_sequence=parent.last_sequence,
        )
    assert caught.value.reason_class == "replay_checkpoint_identity_mismatch"


def test_exact_terminal_retry_with_parent_checkpoint_uses_parent_after_sequence() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    parent = _engine(store, page_size=2).rebuild_state(
        _request("rebuild-continuation-parent", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    ).checkpoint
    store.events.append(_event(3))
    request = _request(
        "rebuild-continuation-child",
        ReplayMode.REBUILD_STATE,
        checkpoint_ref=parent.checkpoint_id,
        from_sequence=3,
    )

    first = _engine(store, page_size=2).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
        checkpoint=parent,
        after_sequence=parent.last_sequence,
    )
    read_count = len(store.read_requests)
    update_count = store.update_calls
    checkpoint_save_count = store.checkpoints.save_calls
    history_count = len(store.report_history)

    retried = _engine(store, page_size=2).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
        checkpoint=parent,
        after_sequence=parent.last_sequence,
    )

    assert retried.report == first.report
    assert retried.checkpoint == first.checkpoint
    assert retried.state == first.state == {"total": 6, "seen": (1, 2, 3)}
    assert retried.report.high_watermark == 3
    assert retried.checkpoint.parent_checkpoint_id == parent.checkpoint_id
    assert len(store.read_requests) == read_count
    assert store.update_calls == update_count
    assert store.checkpoints.save_calls == checkpoint_save_count
    assert len(store.report_history) == history_count

    with pytest.raises(ReplayCheckpointError, match="after_sequence_mismatch"):
        _engine(store, page_size=2).rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
            checkpoint=parent,
            after_sequence=first.checkpoint.last_sequence,
        )
    assert len(store.read_requests) == read_count
    assert store.update_calls == update_count
    assert store.checkpoints.save_calls == checkpoint_save_count


def test_verify_history_compares_deterministic_commands_and_rejects_reducer_arguments() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    engine = _engine(store, page_size=2)

    result = engine.verify_history(
        _request("verify-1", ReplayMode.VERIFY_HISTORY)
    )

    assert result.state is None
    assert result.report.mode is ReplayMode.VERIFY_HISTORY
    assert result.checkpoint.verification_state is not None
    assert result.report.result_checksum == checksum_for(
        {
            "source_history_checksum": result.checkpoint.history_checksum,
            "command_history_checksum": result.checkpoint.verification_state[
                "history_checksum"
            ],
        }
    )
    assert ("reducer:counter-history", "1") in {
        (item.component, item.version) for item in result.report.versions
    }

    with pytest.raises(ReplayModeError, match="does not execute reducers"):
        engine.execute(
            _request("verify-2", ReplayMode.VERIFY_HISTORY),
            reducer_id="counter",
            reducer_version="2.1.0",
        )
    assert "verify-2" not in store.reports


def _command(*, target: str) -> DeterministicCommand:
    return DeterministicCommand(
        ordinal=0,
        kind="route",
        target=target,
        handler_version="1",
        graph_version="1",
        policy_version="1",
        input_refs=("event://counter/1",),
        input_checksums=(checksum_for("counter-input"),),
    )


def test_verify_history_command_mismatch_is_durable_and_source_is_immutable() -> None:
    expected = (_command(target="step:expected"),)
    recorded = (_command(target="step:recorded"),)
    store = _FakeReplayStore([_event(1, recorded_commands=recorded)])
    source_before = [event.to_dict() for event in store.events]
    verifier = _history_verifier(expected=expected)

    with pytest.raises(ReplayHistoryVerificationError) as caught:
        _engine(store, history_verifier=verifier).verify_history(
            _request("verify-command-mismatch", ReplayMode.VERIFY_HISTORY)
        )

    assert caught.value.reason_class == "command_nondeterminism"
    assert caught.value.sequence == 1
    assert caught.value.report is not None
    assert caught.value.report.status is ReplayStatus.FAILED
    assert caught.value.report.mismatch_sequence == 1
    assert [event.to_dict() for event in store.events] == source_before


def test_replay_records_success_and_bounded_mismatch_metrics() -> None:
    backend = _MetricBackend()
    telemetry = EventTelemetry(backend)
    _engine(
        _FakeReplayStore([_event(1)]),
        telemetry=telemetry,
    ).rebuild_state(
        _request("rebuild-metric", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    expected = (_command(target="step:expected"),)
    recorded = (_command(target="step:recorded"),)
    with pytest.raises(ReplayHistoryVerificationError):
        _engine(
            _FakeReplayStore([_event(1, recorded_commands=recorded)]),
            history_verifier=_history_verifier(expected=expected),
            telemetry=telemetry,
        ).verify_history(
            _request("verify-metric", ReplayMode.VERIFY_HISTORY)
        )

    assert backend.counters == [
        (
            "event_replay_total",
            1,
            {"mode": "rebuild_state", "result": "success"},
        ),
        (
            "event_replay_total",
            1,
            {"mode": "verify_history", "result": "failed"},
        ),
        ("event_replay_mismatch_total", 1, {"reason": "command"}),
    ]
    assert "verify-metric" not in repr(backend.counters)


def test_verify_history_missing_activity_and_version_fail_closed() -> None:
    input_ref = PayloadReference(
        uri="secure-activity://tenant-replay/input/1",
        expected_checksum=checksum_for("accepted-input"),
        content_type="application/json",
        size_bytes=32,
    )
    activity = ReplayActivityDescriptor(
        activity_id="activity-missing-1",
        activity_kind=ReplayActivityKind.LLM,
        input_ref=input_ref,
        input_checksum=input_ref.expected_checksum,
        idempotency_key="activity-missing-1",
        attempt=1,
        contract_version="newsroom.activity/v1",
        handler_version="provider/v1",
        accepted_at=STARTED_AT,
        tenant_id=TENANT_ID,
    )
    record_ref = PayloadReference(
        uri="secure-activity://tenant-replay/record/1",
        expected_checksum=checksum_for("missing-record"),
        content_type="application/vnd.newsroom.replay-activity-record+json",
        size_bytes=64,
    )

    activity_policy = replace(
        _history_policy(),
        expected_activity=activity,
        recorded_activity_ref=record_ref,
    )

    missing_store = _FakeReplayStore([_event(1, history_policy=activity_policy)])
    with pytest.raises(ReplayHistoryVerificationError) as missing:
        _engine(
            missing_store,
            history_verifier=_history_verifier(),
        ).verify_history(
            _request("verify-missing-activity", ReplayMode.VERIFY_HISTORY)
        )
    assert missing.value.reason_class == "missing_activity_result"
    assert missing.value.report is not None
    assert missing.value.report.status is ReplayStatus.FAILED

    incompatible_store = _FakeReplayStore(
        [
            _event(
                1,
                history_policy=replace(
                    _history_policy(),
                    graph_version="missing-version",
                ),
            )
        ]
    )
    with pytest.raises(ReplayHistoryVerificationError) as incompatible:
        _engine(
            incompatible_store,
            history_verifier=_history_verifier(),
        ).verify_history(
            _request("verify-incompatible-version", ReplayMode.VERIFY_HISTORY)
        )
    assert incompatible.value.reason_class == "incompatible_version"
    assert incompatible.value.report is not None
    assert incompatible.value.report.mismatch_sequence == 1


def test_redeliver_is_explicitly_delegated_without_reading_or_creating_report() -> None:
    store = _FakeReplayStore([_event(1)])
    engine = _engine(store)

    with pytest.raises(
        ReplayRedeliveryDelegationRequired,
        match="durable delivery ledger",
    ):
        engine.redeliver(_request("redeliver-1", ReplayMode.REDELIVER))

    assert store.reports == {}
    assert store.read_requests == []
    with pytest.raises(ReplayModeError):
        engine.verify_history(_request("wrong-mode", ReplayMode.REBUILD_STATE))


def test_reducer_registry_rejects_capability_escape_without_side_effect(
    tmp_path: Path,
) -> None:
    escaped = tmp_path / "escaped.txt"
    with pytest.raises(ReplayReducerRegistrationError, match="forbidden attribute"):
        ReplayReducerRegistration(
            "reflection",
            "1",
            _reflection_escape_reducer,
            initial_state={"path": str(escaped)},
        )
    assert not escaped.exists()

    with pytest.raises(ReplayReducerRegistrationError, match="forbidden builtin: open"):
        ReplayReducerRegistration("impure", "1", _impure_reducer)
    with pytest.raises(ReplayReducerRegistrationError, match="forbidden operation: IMPORT"):
        ReplayReducerRegistration("import", "1", _import_reducer)
    with pytest.raises(ReplayReducerRegistrationError, match="forbidden builtin: getattr"):
        ReplayReducerRegistration("getattr", "1", _getattr_reducer)
    with pytest.raises(ReplayReducerRegistrationError, match="forbidden builtin: frozenset"):
        ReplayReducerRegistration("unordered", "1", _unordered_reducer)
    with pytest.raises(ReplayReducerRegistrationError, match="forbidden operation: BUILD_SET"):
        ReplayReducerRegistration("set-literal", "1", _set_literal_reducer)

    captured: list[int] = []

    def mutable_capture(state: Any, event: Any) -> Any:
        captured.append(event.stream_sequence)
        return state

    with pytest.raises(ReplayReducerRegistrationError, match="forbidden dependency"):
        ReplayReducerRegistration("capture", "1", mutable_capture)

    registry = _registry()
    with pytest.raises(ReplayReducerRegistrationError, match="duplicate"):
        registry.register(registry.get("counter", "2.1.0"))


def test_unordered_reducer_is_rejected_across_python_hash_seeds() -> None:
    script = """
from framework.events.runtime.replay_engine import (
    ReplayReducerRegistration,
    ReplayReducerRegistrationError,
)

def reducer(state, event):
    return {"items": list(frozenset(event.payload["items"]))}

try:
    ReplayReducerRegistration("unordered", "1", reducer)
except ReplayReducerRegistrationError as exc:
    print(type(exc).__name__ + ":" + str(exc))
else:
    print("accepted")
"""
    outputs: list[str] = []
    for seed in ("1", "2", "3"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout.strip())

    assert outputs == [
        "ReplayReducerRegistrationError:replay reducer uses forbidden builtin: frozenset"
    ] * 3


def test_process_death_resumes_from_durable_checkpoint_without_gap_or_duplicate() -> None:
    events = [_event(1), _event(2), _event(3)]
    store = _FakeReplayStore(events)
    store.crash_after_reads = 1
    engine = _engine(store)
    request = _request("rebuild-process-death", ReplayMode.REBUILD_STATE)

    with pytest.raises(_ProcessCrash):
        engine.rebuild_state(
            request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )

    running = store.reports[request.replay_id]
    assert running.status is ReplayStatus.RUNNING
    assert running.to_sequence == 1
    assert running.checkpoint_ref is not None
    checkpoint = store.checkpoints.records[running.checkpoint_ref]
    assert checkpoint.last_sequence == 1
    assert checkpoint.state == {"total": 1, "seen": (1,)}
    assert checkpoint.source_high_watermark == running.high_watermark

    store.events.append(_event(4))
    store.crash_after_reads = None
    store.read_requests.clear()
    resumed = _engine(store).rebuild_state(
        request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    one_shot = _engine(_FakeReplayStore(events), page_size=3).rebuild_state(
        _request("rebuild-one-shot", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    )

    assert resumed.state == {"total": 6, "seen": (1, 2, 3)}
    assert resumed.report.high_watermark == 3
    assert resumed.report.result_checksum == one_shot.report.result_checksum
    assert resumed.checkpoint.history_checksum == one_shot.checkpoint.history_checksum
    assert resumed.report.checkpoint_ref == checkpoint.checkpoint_id
    assert store.read_requests[0].cursor is not None
    assert store.read_requests[0].cursor.after_sequence == 1


def test_live_append_above_transactional_watermark_is_excluded() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    store.live_append = _event(3)

    result = _engine(store).rebuild_state(
        _request("rebuild-live", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    )

    assert result.report.high_watermark == 2
    assert result.state == {"total": 3, "seen": (1, 2)}
    assert [event.stream_sequence for event in store.events] == [1, 2, 3]
    assert all(request.through_sequence == 2 for request in store.read_requests)


def test_page_event_above_captured_watermark_fails_before_apply() -> None:
    first = _event(1)
    second = _event(2)
    store = _FakeReplayStore([first, second])
    store.forced_page = SimpleNamespace(
        stream_id=STREAM_ID,
        tenant_id=TENANT_ID,
        high_watermark=2,
        events=(first, second, _event(3)),
        next_cursor=None,
    )

    with pytest.raises(ReplayHistoryOrderError) as caught:
        _engine(store, page_size=3).rebuild_state(
            _request("rebuild-page-above-watermark", ReplayMode.REBUILD_STATE),
            reducer_id="counter",
            reducer_version="2.1.0",
        )

    failure = caught.value
    assert failure.reason_class == "event_exceeds_high_watermark"
    assert failure.sequence == 3
    assert failure.report is not None
    assert failure.report.status is ReplayStatus.FAILED
    assert store.reports["rebuild-page-above-watermark"].status is ReplayStatus.FAILED
    assert failure.checkpoint is not None
    assert failure.checkpoint.last_sequence == 0
    assert failure.checkpoint.state == {"total": 0, "seen": ()}
    assert store.report_history[-1].status is ReplayStatus.FAILED


def test_new_replay_extends_parent_checkpoint_without_overwriting_input_slot() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    first = _engine(store, page_size=2).rebuild_state(
        _request("rebuild-parent", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    parent_snapshot = first.checkpoint.to_dict()
    store.events.append(_event(3))

    extended = _engine(store).rebuild_state(
        _request(
            "rebuild-child",
            ReplayMode.REBUILD_STATE,
            checkpoint_ref=first.checkpoint.checkpoint_id,
            from_sequence=3,
        ),
        reducer_id="counter",
        reducer_version="2.1.0",
    )

    assert extended.state == {"total": 6, "seen": (1, 2, 3)}
    assert extended.report.high_watermark == 3
    assert extended.report.checkpoint_ref is not None
    assert extended.report.checkpoint_ref.startswith(
        "rebuild-child:checkpoint:parent:"
    )
    assert extended.checkpoint.checkpoint_id != first.checkpoint.checkpoint_id
    assert extended.checkpoint.parent_checkpoint_id == first.checkpoint.checkpoint_id
    assert store.checkpoints.records[first.checkpoint.checkpoint_id].to_dict() == (
        parent_snapshot
    )


def test_parent_checkpoint_survives_crash_before_running_transition() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    parent = _engine(store, page_size=2).rebuild_state(
        _request("pre-running-parent", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    ).checkpoint
    store.events.append(_event(3))
    child_request = _request(
        "pre-running-child",
        ReplayMode.REBUILD_STATE,
        checkpoint_ref=parent.checkpoint_id,
        from_sequence=3,
    )
    crashing_update = store.update_calls + 1
    store.crash_update_calls = {crashing_update}
    reads_before = len(store.read_requests)

    with pytest.raises(_ProcessCrash):
        _engine(store).rebuild_state(
            child_request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )

    pending = store.reports[child_request.replay_id]
    assert pending.status is ReplayStatus.PENDING
    assert pending.checkpoint_ref is not None
    assert pending.checkpoint_ref != parent.checkpoint_id
    initial_output = store.checkpoints.records[pending.checkpoint_ref]
    assert initial_output.last_sequence == 2
    assert initial_output.parent_checkpoint_id == parent.checkpoint_id
    assert len(store.read_requests) == reads_before

    changed_parent_request = _request(
        child_request.replay_id,
        ReplayMode.REBUILD_STATE,
        checkpoint_ref="different-parent",
        from_sequence=3,
    )
    with pytest.raises(ReplayCoreError, match="does not match"):
        _engine(store).rebuild_state(
            changed_parent_request,
            reducer_id="counter",
            reducer_version="2.1.0",
        )
    assert len(store.read_requests) == reads_before

    store.crash_update_calls.clear()
    resumed = _engine(store).rebuild_state(
        child_request,
        reducer_id="counter",
        reducer_version="2.1.0",
    )
    assert resumed.state == {"total": 6, "seen": (1, 2, 3)}
    assert resumed.checkpoint.parent_checkpoint_id == parent.checkpoint_id


def test_checkpoint_store_wrong_parent_and_slot_fail_before_source_read() -> None:
    pending_store = _FakeReplayStore([_event(1)])
    pending_request = _request("wrong-parent-output", ReplayMode.VERIFY_HISTORY)
    pending_store.crash_update_calls = {1}
    with pytest.raises(_ProcessCrash):
        _engine(pending_store).verify_history(pending_request)
    pending_report = pending_store.reports[pending_request.replay_id]
    assert pending_report.checkpoint_ref is not None
    correct_output = pending_store.checkpoints.records[pending_report.checkpoint_ref]
    pending_store.checkpoints.records[pending_report.checkpoint_ref] = replace(
        correct_output,
        parent_checkpoint_id="wrong-parent-with-valid-checksum",
    )
    pending_store.crash_update_calls.clear()
    reads_before = len(pending_store.read_requests)

    with pytest.raises(ReplaySourceReadError) as wrong_parent:
        _engine(pending_store).verify_history(pending_request)
    assert wrong_parent.value.reason_class == "replay_checkpoint_identity_mismatch"
    assert wrong_parent.value.report is not None
    assert wrong_parent.value.report.status is ReplayStatus.PENDING
    assert len(pending_store.read_requests) == reads_before

    running_store = _FakeReplayStore([_event(1), _event(2)])
    running_request = _request("wrong-running-slot", ReplayMode.VERIFY_HISTORY)
    running_store.crash_after_reads = 1
    with pytest.raises(_ProcessCrash):
        _engine(running_store).verify_history(running_request)
    running_report = running_store.reports[running_request.replay_id]
    assert running_report.checkpoint_ref is not None
    running_output = running_store.checkpoints.records[running_report.checkpoint_ref]
    running_store.checkpoints.read_overrides[running_report.checkpoint_ref] = replace(
        running_output,
        checkpoint_id="valid-checksum-wrong-output-slot",
    )
    running_store.crash_after_reads = None
    running_store.read_requests.clear()

    with pytest.raises(ReplaySourceReadError) as wrong_slot:
        _engine(running_store).verify_history(running_request)
    assert wrong_slot.value.reason_class == "replay_checkpoint_identity_mismatch"
    assert wrong_slot.value.report is not None
    assert wrong_slot.value.report.status is ReplayStatus.RUNNING
    assert running_store.read_requests == []


def test_verify_history_process_death_resumes_verifier_checkpoint() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    request = _request("verify-resume", ReplayMode.VERIFY_HISTORY)
    store.crash_after_reads = 1

    with pytest.raises(_ProcessCrash):
        _engine(store, page_size=1).verify_history(request)

    running = store.reports[request.replay_id]
    assert running.checkpoint_ref is not None
    checkpoint = store.checkpoints.records[running.checkpoint_ref]
    assert checkpoint.last_sequence == 1
    assert checkpoint.last_event_id == "evt-replay-1"
    assert checkpoint.verification_state is not None
    assert checkpoint.verification_state["next_sequence"] == 2

    store.crash_after_reads = None
    store.read_requests.clear()
    resumed = _engine(store, page_size=1).verify_history(request)

    assert resumed.report.status is ReplayStatus.SUCCEEDED
    assert resumed.checkpoint.last_sequence == 2
    assert resumed.checkpoint.verification_state is not None
    assert resumed.checkpoint.verification_state["next_sequence"] == 3
    assert store.read_requests[0].cursor is not None
    assert store.read_requests[0].cursor.after_sequence == 1


def test_parent_checkpoint_wrong_id_and_tenant_fail_before_source_read() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])
    parent = _engine(store, page_size=2).verify_history(
        _request("strict-parent-slot", ReplayMode.VERIFY_HISTORY)
    ).checkpoint
    store.events.append(_event(3))
    store.read_requests.clear()
    wrong_id = replace(parent, checkpoint_id="valid-checksum-wrong-parent-slot")
    store.checkpoints.read_overrides[parent.checkpoint_id] = wrong_id

    with pytest.raises(ReplaySourceReadError) as mismatched:
        _engine(store).verify_history(
            _request(
                "strict-child-slot",
                ReplayMode.VERIFY_HISTORY,
                checkpoint_ref=parent.checkpoint_id,
            )
        )
    assert mismatched.value.reason_class == "replay_checkpoint_identity_mismatch"
    assert store.read_requests == []

    store.checkpoints.read_overrides[parent.checkpoint_id] = replace(
        parent,
        tenant_id="another-tenant",
    )
    store.read_requests.clear()
    with pytest.raises(ReplayCheckpointError) as wrong_tenant:
        _engine(store).verify_history(
            _request(
                "strict-child-tenant",
                ReplayMode.VERIFY_HISTORY,
                checkpoint_ref=parent.checkpoint_id,
            )
        )
    assert wrong_tenant.value.reason_class == "missing_durable_checkpoint"
    assert store.read_requests == []


def test_missing_input_checkpoint_ref_fails_before_event_source_read() -> None:
    store = _FakeReplayStore([_event(1), _event(2)])

    with pytest.raises(ReplayCheckpointError) as caught:
        _engine(store).rebuild_state(
            _request(
                "rebuild-missing-parent",
                ReplayMode.REBUILD_STATE,
                checkpoint_ref="missing-parent-checkpoint",
            ),
            reducer_id="counter",
            reducer_version="2.1.0",
        )

    assert store.read_requests == []
    assert caught.value.report is not None
    assert caught.value.report.status is ReplayStatus.FAILED
    assert caught.value.report.checkpoint_ref is not None
    assert caught.value.report.checkpoint_ref.startswith(
        "rebuild-missing-parent:checkpoint:parent:"
    )


@pytest.mark.parametrize(
    ("case", "error_type", "reason", "sequence", "quarantined"),
    [
        (
            "unsorted",
            ReplayHistoryOrderError,
            "unsorted_history",
            1,
            False,
        ),
        (
            "corrupt",
            ReplayHistoryIntegrityError,
            "corrupt_record",
            1,
            True,
        ),
        (
            "unknown_schema",
            ReplayHistorySchemaError,
            "unknown_data_schema",
            1,
            True,
        ),
        (
            "upcast_failure",
            ReplayHistorySchemaError,
            "upcast_failed",
            1,
            True,
        ),
        (
            "malformed_history",
            ReplayHistoryVerificationError,
            "corrupt_history",
            1,
            False,
        ),
    ],
)
def test_invalid_history_fails_with_typed_durable_report(
    case: str,
    error_type: type[Exception],
    reason: str,
    sequence: int,
    quarantined: bool,
) -> None:
    first = _event(1)
    second = _event(2)
    catalog = _catalog()
    store = _FakeReplayStore([first, second])
    if case == "unsorted":
        store.forced_page = SimpleNamespace(
            stream_id=STREAM_ID,
            tenant_id=TENANT_ID,
            high_watermark=2,
            events=(first, first),
            next_cursor=None,
        )
    elif case == "corrupt":
        object.__setattr__(first, "record_checksum", "sha256:" + "0" * 64)
        store = _FakeReplayStore([first])
    elif case == "unknown_schema":
        store = _FakeReplayStore([_event(1, data_schema="io.newsroom.counter/v99")])
    elif case == "upcast_failure":
        store = _FakeReplayStore(
            [_event(1, data_schema="io.newsroom.counter/v1")]
        )
        catalog = _catalog(with_upcast=False)
    elif case == "malformed_history":
        store = _FakeReplayStore([_event(1, malformed_history=True)])

    source_before = [event.to_dict() for event in store.events]
    engine = _engine(store, catalog=catalog, page_size=2)
    with pytest.raises(error_type) as caught:
        engine.verify_history(_request(f"verify-{case}", ReplayMode.VERIFY_HISTORY))

    failure = caught.value
    assert failure.report.status is ReplayStatus.FAILED
    assert failure.report.reason_class == reason
    assert failure.report.mismatch_sequence == sequence
    assert failure.checkpoint is not None
    assert failure.report.checkpoint_ref == failure.checkpoint.checkpoint_id
    assert store.checkpoints.records[failure.checkpoint.checkpoint_id] == (
        failure.checkpoint
    )
    assert bool(failure.report.quarantine_refs) is quarantined
    assert bool(store.quarantine) is quarantined
    assert [event.to_dict() for event in store.events] == source_before
    statuses = [item.status for item in store.report_history]
    assert statuses[:2] == [ReplayStatus.PENDING, ReplayStatus.RUNNING]
    assert statuses[-1] is ReplayStatus.FAILED


def test_checkpoint_mismatch_fails_before_source_read_and_tampering_is_rejected() -> None:
    source = [_event(1), _event(2)]
    first_store = _FakeReplayStore(source)
    checkpoint = _engine(first_store, page_size=2).rebuild_state(
        _request("checkpoint-source", ReplayMode.REBUILD_STATE),
        reducer_id="counter",
        reducer_version="2.1.0",
    ).checkpoint

    serialized = checkpoint.to_dict()
    serialized["state"] = {"total": 999, "seen": [1, 2]}
    with pytest.raises(EventIntegrityError, match="checkpoint checksum"):
        ReplayCheckpoint.from_dict(serialized)

    incompatible = replace(
        checkpoint,
        runtime_version="different-runtime",
        versions=tuple(
            ReplayVersion(item.component, "different-runtime")
            if item.component == "replay_runtime"
            else item
            for item in checkpoint.versions
        ),
    )
    resume_store = _FakeReplayStore(source)
    with pytest.raises(ReplayCheckpointError) as caught:
        _engine(resume_store).rebuild_state(
            _request(
                "checkpoint-mismatch",
                ReplayMode.REBUILD_STATE,
                checkpoint_ref=incompatible.checkpoint_id,
            ),
            reducer_id="counter",
            reducer_version="2.1.0",
            checkpoint=incompatible,
            after_sequence=2,
        )

    assert resume_store.read_requests == []
    assert caught.value.report.status is ReplayStatus.FAILED
    assert caught.value.report.reason_class == "checkpoint_runtime_version_mismatch"


def test_verify_checkpoint_rejects_conflicting_pinned_versions_on_build_and_restore() -> None:
    source_store = _FakeReplayStore([_event(1), _event(2)])
    checkpoint = _engine(source_store, page_size=2).verify_history(
        _request("verify-pinned-source", ReplayMode.VERIFY_HISTORY)
    ).checkpoint
    conflicting_versions = tuple(
        ReplayVersion(item.component, "conflicting-version")
            if item.component == "graph:counter-graph"
        else item
        for item in checkpoint.versions
    )

    with pytest.raises(ValueError, match="verification pinned versions"):
        replace(checkpoint, versions=conflicting_versions)

    restored = ReplayCheckpoint.from_dict(checkpoint.to_dict())
    object.__setattr__(restored, "versions", conflicting_versions)
    object.__setattr__(
        restored,
        "checkpoint_checksum",
        checksum_for(restored.checksum_projection()),
    )
    resume_store = _FakeReplayStore([_event(1), _event(2)])

    with pytest.raises(ReplayCheckpointError) as caught:
        _engine(resume_store).verify_history(
            _request(
                "verify-pinned-restore",
                ReplayMode.VERIFY_HISTORY,
                checkpoint_ref=restored.checkpoint_id,
            ),
            checkpoint=restored,
            after_sequence=restored.last_sequence,
        )

    assert resume_store.read_requests == []
    assert caught.value.reason_class == "checkpoint_verification_version_mismatch"
    assert caught.value.report is not None
    assert caught.value.report.status is ReplayStatus.FAILED


def _assert_no_secret_in_exception_chain(error: BaseException, secret: str) -> None:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        assert secret not in str(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def test_begin_and_initial_report_store_failures_are_safe_and_typed() -> None:
    begin_store = _FakeReplayStore([_event(1)])
    begin_store.fail_begin = True
    with pytest.raises(ReplaySourceReadError) as begin_failure:
        _engine(begin_store).verify_history(
            _request("begin-store-failure", ReplayMode.VERIFY_HISTORY)
        )
    assert begin_failure.value.reason_class == "replay_begin_failed"
    assert begin_failure.value.report is None
    _assert_no_secret_in_exception_chain(begin_failure.value, begin_store.secret)

    update_store = _FakeReplayStore([_event(1)])
    update_store.fail_update_calls = {1}
    with pytest.raises(ReplaySourceReadError) as update_failure:
        _engine(update_store).verify_history(
            _request("initial-update-failure", ReplayMode.VERIFY_HISTORY)
        )
    assert update_failure.value.reason_class == "replay_running_report_update_failed"
    assert update_failure.value.report is not None
    assert update_failure.value.report.status is ReplayStatus.PENDING
    assert update_store.update_calls == 1
    _assert_no_secret_in_exception_chain(update_failure.value, update_store.secret)


@pytest.mark.parametrize(
    ("failed_update_call", "expected_reason", "expected_to_sequence"),
    [
        (2, "replay_progress_report_update_failed", None),
        (3, "replay_success_report_update_failed", 1),
    ],
)
def test_progress_and_success_report_failures_do_not_retry_same_store(
    failed_update_call: int,
    expected_reason: str,
    expected_to_sequence: int | None,
) -> None:
    store = _FakeReplayStore([_event(1)])
    store.fail_update_calls = {failed_update_call}

    with pytest.raises(ReplaySourceReadError) as caught:
        _engine(store).verify_history(
            _request(f"report-failure-{failed_update_call}", ReplayMode.VERIFY_HISTORY)
        )

    failure = caught.value
    assert failure.reason_class == expected_reason
    assert failure.report is not None
    assert failure.report.status is ReplayStatus.RUNNING
    assert failure.report.to_sequence == expected_to_sequence
    assert failure.checkpoint is not None
    assert failure.checkpoint.last_sequence == 1
    assert store.update_calls == failed_update_call
    _assert_no_secret_in_exception_chain(failure, store.secret)


def test_failure_report_update_failure_leaves_recoverable_running_report() -> None:
    store = _FakeReplayStore([_event(1, data_schema="io.newsroom.counter/v99")])
    store.fail_update_calls = {2}

    with pytest.raises(ReplaySourceReadError) as caught:
        _engine(store).verify_history(
            _request("failure-report-store-error", ReplayMode.VERIFY_HISTORY)
        )

    failure = caught.value
    assert failure.reason_class == "replay_failure_report_update_failed"
    assert failure.report is not None
    assert failure.report.status is ReplayStatus.RUNNING
    assert failure.checkpoint is not None
    assert failure.checkpoint.last_sequence == 0
    assert store.update_calls == 2
    assert store.reports["failure-report-store-error"].status is ReplayStatus.RUNNING
    _assert_no_secret_in_exception_chain(failure, store.secret)


def test_read_and_quarantine_store_failures_have_redacted_durable_reports() -> None:
    read_store = _FakeReplayStore([_event(1)])
    read_store.fail_after_reads = 0
    read_request = _request("safe-read-failure", ReplayMode.VERIFY_HISTORY)
    with pytest.raises(ReplaySourceReadError) as read_failure:
        _engine(read_store).verify_history(read_request)
    assert read_failure.value.reason_class == "source_read_failed"
    assert read_failure.value.report is not None
    assert read_failure.value.report.status is ReplayStatus.RUNNING
    assert read_failure.value.checkpoint is not None
    assert read_failure.value.checkpoint.last_sequence == 0
    _assert_no_secret_in_exception_chain(read_failure.value, read_store.secret)
    read_store.events.append(_event(2))
    read_store.fail_after_reads = None
    read_store.read_requests.clear()
    resumed = _engine(read_store).verify_history(read_request)
    assert resumed.report.high_watermark == 1
    assert resumed.checkpoint.last_sequence == 1
    assert all(item.through_sequence == 1 for item in read_store.read_requests)

    quarantine_store = _FakeReplayStore(
        [_event(1, data_schema="io.newsroom.counter/v99")]
    )
    quarantine_store.fail_quarantine = True
    with pytest.raises(ReplaySourceReadError) as quarantine_failure:
        _engine(quarantine_store).verify_history(
            _request("safe-quarantine-failure", ReplayMode.VERIFY_HISTORY)
        )
    assert quarantine_failure.value.reason_class == "quarantine_persistence_failed"
    assert quarantine_failure.value.report is not None
    assert quarantine_failure.value.report.status is ReplayStatus.RUNNING
    _assert_no_secret_in_exception_chain(
        quarantine_failure.value,
        quarantine_store.secret,
    )


def test_checkpoint_store_failure_is_safe_and_does_not_write_failure_report() -> None:
    store = _FakeReplayStore([_event(1)])
    store.checkpoints.fail_writes = True
    request = _request("checkpoint-write-failure", ReplayMode.VERIFY_HISTORY)

    with pytest.raises(ReplaySourceReadError) as caught:
        _engine(store).verify_history(request)

    failure = caught.value
    assert failure.reason_class == "replay_checkpoint_write_failed"
    assert failure.report is not None
    assert failure.report.status is ReplayStatus.PENDING
    assert failure.checkpoint is None
    assert store.update_calls == 0
    _assert_no_secret_in_exception_chain(failure, store.checkpoints.secret)

    # The PENDING report already fixed H=1.  Recovery after storage returns must
    # not widen that replay when a live append commits during the outage.
    store.events.append(_event(2))
    store.checkpoints.fail_writes = False
    store.read_requests.clear()
    resumed = _engine(store).verify_history(request)

    assert resumed.report.high_watermark == 1
    assert resumed.checkpoint.last_sequence == 1
    assert resumed.checkpoint.source_high_watermark == 1
    assert store.read_requests
    assert all(read.through_sequence == 1 for read in store.read_requests)

    readable_store = _FakeReplayStore([_event(1)])
    parent = _engine(readable_store).verify_history(
        _request("checkpoint-read-parent", ReplayMode.VERIFY_HISTORY)
    ).checkpoint
    readable_store.checkpoints.fail_reads = True
    with pytest.raises(ReplaySourceReadError) as read_failure:
        _engine(readable_store).verify_history(
            _request(
                "checkpoint-read-failure",
                ReplayMode.VERIFY_HISTORY,
                checkpoint_ref=parent.checkpoint_id,
            )
        )
    assert read_failure.value.reason_class == "replay_checkpoint_read_failed"
    assert read_failure.value.report is not None
    assert read_failure.value.report.status is ReplayStatus.PENDING
    _assert_no_secret_in_exception_chain(
        read_failure.value,
        readable_store.checkpoints.secret,
    )


@pytest.mark.parametrize("deterministic_failure", [False, True])
def test_clock_failure_is_safe_and_keeps_replay_recoverable(
    deterministic_failure: bool,
) -> None:
    secret = "clock-secret-token"

    def failing_clock() -> datetime:
        raise RuntimeError(secret)

    event = (
        _event(1, data_schema="io.newsroom.counter/v99")
        if deterministic_failure
        else _event(1)
    )
    store = _FakeReplayStore([event])
    with pytest.raises(ReplaySourceReadError) as caught:
        _engine(store, clock=failing_clock).verify_history(
            _request(
                f"clock-failure-{deterministic_failure}",
                ReplayMode.VERIFY_HISTORY,
            )
        )

    failure = caught.value
    assert failure.reason_class == "replay_clock_failed"
    assert failure.report is not None
    assert failure.report.status is ReplayStatus.RUNNING
    assert failure.checkpoint is not None
    assert failure.checkpoint.checkpoint_id == failure.report.checkpoint_ref
    _assert_no_secret_in_exception_chain(failure, secret)


def test_replay_core_has_no_bus_infrastructure_or_side_effect_adapter_imports() -> None:
    target = ROOT / "framework" / "events" / "runtime" / "replay_engine.py"
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(target))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    forbidden_roots = (
        "backend",
        "infrastructure",
        "interfaces",
        "framework.events.bus",
        "framework.tool",
        "framework.memory",
    )
    assert not {
        module
        for module in imports
        if any(module == root or module.startswith(f"{root}.") for root in forbidden_roots)
    }
    assert "replay_to_bus" not in source
    assert "EventBus" not in source
