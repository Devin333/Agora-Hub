"""Redacted, replay-safe projection of runtime execution facts.

This projection is deliberately downstream of the canonical durable event
stream.  It is safe to rebuild and query, but has no methods that can mutate a
Graph, approve a tool, publish an artifact, or invoke a worker.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
import hashlib
import re
import threading
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import stable_json_dumps

if TYPE_CHECKING:  # pragma: no cover - typing-only imports kept out of runtime cycles
    from framework.events.canonical import StoredEvent
    from framework.events.runtime.publisher import EventRuntime, EventPublishRequest


class RuntimeProjectionError(RuntimeError):
    pass


class RuntimeEventIdentityConflict(RuntimeProjectionError):
    pass


class RuntimeCursorConflict(RuntimeProjectionError):
    pass


class ProtectedPayloadRejected(RuntimeProjectionError):
    pass


class RuntimeEventType(StrEnum):
    TURN_STARTED = "turn_started"
    TURN_STOPPED = "turn_stopped"
    TURN_ABORTED = "turn_aborted"
    TOOL_REQUESTED = "tool_requested"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECIDED = "approval_decided"
    EXECUTION_STARTED = "execution_started"
    EXECUTION_TERMINAL = "execution_terminal"
    CHILD_SPAWNED = "child_spawned"
    CHILD_STATUS = "child_status"
    CHILD_HEARTBEAT = "child_heartbeat"
    CHILD_TERMINAL = "child_terminal"
    CONTEXT_COMPACTION_PLANNED = "context_compaction_planned"
    CONTEXT_COMPACTION_COMMITTED = "context_compaction_committed"
    CONTEXT_COMPACTION_REJECTED = "context_compaction_rejected"
    WORKER_HEARTBEAT = "worker_heartbeat"
    WORKER_STATUS = "worker_status"
    TIMEOUT = "timeout"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLATION_CONFIRMED = "cancellation_confirmed"
    INDETERMINATE = "indeterminate"
    RUNTIME_ERROR = "runtime_error"


RUNTIME_EVENT_SCHEMA_V1 = "newsroom.runtime-event/v1"
RUNTIME_EVENT_DATA_SCHEMA = RUNTIME_EVENT_SCHEMA_V1
_LEGACY_HARNESS_RUNTIME_TYPES = frozenset(
    {"context_compaction_planned", "context_compaction_rejected"}
)
MAX_RUNTIME_METADATA_BYTES = 16 * 1024
MAX_RUNTIME_REFS = 64
MAX_RUNTIME_REF_LENGTH = 2048
MAX_CURSOR_LENGTH = 512

_SECRET_KEY = re.compile(
    r"(?:secret|token|password|credential|private[_-]?key|api[_-]?key|authorization|cookie|prompt|raw_payload|file_content)",
    re.IGNORECASE,
)
_PROTECTED_PAYLOAD_KEY = re.compile(
    r"^(?:args?|arguments?|call|payload|output|observation|prompt|task|feedback|"
    r"verdict|stream_event|text_delta|content|file(?:_content)?|raw)$",
    re.IGNORECASE,
)
_CHECKSUM_KEY = re.compile(r"(?:checksum|digest)$", re.IGNORECASE)
_REF_KEY = re.compile(r"(?:ref|uri|id)$", re.IGNORECASE)
_CHECKSUM = re.compile(r"sha256:[0-9a-f]{64}\Z", re.IGNORECASE)


def _utc(value: datetime, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: Any, field_name: str, *, max_length: int = 512) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} exceeds its bounded length")
    return normalized


def _optional_text(value: Any, field_name: str, *, max_length: int = 2048) -> str | None:
    if value is None:
        return None
    return _text(value, field_name, max_length=max_length)


def _identity(value: GraphExecutionIdentity | Mapping[str, Any] | None) -> GraphExecutionIdentity | None:
    if value is None:
        return None
    if isinstance(value, GraphExecutionIdentity):
        return value
    return GraphExecutionIdentity.from_dict(value)


def _checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def redact_runtime_value(value: Any, *, key: str = "", max_depth: int = 8) -> Any:
    """Return bounded JSON-safe metadata with secret-bearing fields removed."""
    if max_depth <= 0:
        return "[redacted-depth]"
    if _SECRET_KEY.search(key) or _PROTECTED_PAYLOAD_KEY.fullmatch(key):
        return "[redacted]"
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            name = str(raw_key)
            if _SECRET_KEY.search(name) or _PROTECTED_PAYLOAD_KEY.fullmatch(name):
                output[name] = "[redacted]"
            else:
                output[name] = redact_runtime_value(raw_value, key=name, max_depth=max_depth - 1)
        return output
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact_runtime_value(item, max_depth=max_depth - 1) for item in value[:128]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 2048:
            return "[truncated]"
        return value
    return str(value)[:2048]


def _bounded_refs(values: Iterable[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    refs = tuple(_text(value, "ref", max_length=MAX_RUNTIME_REF_LENGTH) for value in values)
    if len(refs) > MAX_RUNTIME_REFS:
        raise ValueError("refs exceed bounded limit")
    return tuple(dict.fromkeys(refs))


def _bounded_checksums(values: Mapping[str, str] | None) -> dict[str, str]:
    if values is None:
        return {}
    checksums = {}
    for key, value in dict(values).items():
        checksum = _text(value, "checksum", max_length=80).lower()
        if _CHECKSUM.fullmatch(checksum) is None:
            raise ValueError("checksums must use sha256 format")
        checksums[str(key)] = checksum
    if len(checksums) > MAX_RUNTIME_REFS:
        raise ValueError("checksums exceed bounded limit")
    return dict(sorted(checksums.items()))


@dataclass(frozen=True, slots=True)
class RuntimeEventIdentity:
    graph_identity: GraphExecutionIdentity | Mapping[str, Any] | None = None
    activity_id: str | None = None
    attempt_id: str | None = None
    node_id: str | None = None
    node_instance_id: str | None = None

    def __post_init__(self) -> None:
        identity = _identity(self.graph_identity)
        for name in ("activity_id", "attempt_id", "node_id", "node_instance_id"):
            value = _optional_text(getattr(self, name), name)
            object.__setattr__(self, name, value)
        if identity is not None:
            expected = {
                "activity_id": identity.activity_id,
                "node_id": identity.node_id,
                "node_instance_id": identity.node_instance_id,
            }
            for name, value in expected.items():
                supplied = getattr(self, name)
                if supplied is not None and supplied != value:
                    raise RuntimeEventIdentityConflict(
                        f"runtime identity field {name} conflicts with Graph identity"
                    )
                object.__setattr__(self, name, value)
        object.__setattr__(self, "graph_identity", identity)

    @property
    def run_id(self) -> str | None:
        return self.graph_identity.run_id if self.graph_identity else None

    def matches(self, other: "RuntimeEventIdentity") -> bool:
        return self == other

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_identity": self.graph_identity.to_dict() if self.graph_identity else None,
            "activity_id": self.activity_id,
            "attempt_id": self.attempt_id,
            "node_id": self.node_id,
            "node_instance_id": self.node_instance_id,
        }


@dataclass(frozen=True, slots=True)
class RuntimeEventEnvelope:
    event_id: str
    event_type: RuntimeEventType | str
    occurred_at: datetime
    identity: RuntimeEventIdentity | Mapping[str, Any] = field(default_factory=RuntimeEventIdentity)
    status: str | None = None
    reason_code: str | None = None
    sequence: int | None = None
    stream_id: str | None = None
    schema_version: str = RUNTIME_EVENT_SCHEMA_V1
    refs: tuple[str, ...] = ()
    checksums: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    source: str = "harness"

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, "event_id"))
        object.__setattr__(self, "event_type", RuntimeEventType(self.event_type))
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        identity = self.identity if isinstance(self.identity, RuntimeEventIdentity) else RuntimeEventIdentity(**dict(self.identity))
        object.__setattr__(self, "identity", identity)
        if self.schema_version != RUNTIME_EVENT_SCHEMA_V1:
            raise ValueError("unsupported runtime event schema")
        if self.sequence is not None and (isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1):
            raise ValueError("sequence must be a positive integer or None")
        object.__setattr__(self, "status", _optional_text(self.status, "status"))
        object.__setattr__(self, "reason_code", _optional_text(self.reason_code, "reason_code"))
        object.__setattr__(self, "stream_id", _optional_text(self.stream_id, "stream_id"))
        object.__setattr__(self, "source", _text(self.source, "source"))
        refs = _bounded_refs(self.refs)
        checksums = _bounded_checksums(self.checksums)
        metadata = redact_runtime_value(dict(self.metadata))
        if len(stable_json_dumps(metadata).encode("utf-8")) > MAX_RUNTIME_METADATA_BYTES:
            raise ValueError("runtime metadata exceeds bounded size")
        object.__setattr__(self, "refs", refs)
        object.__setattr__(self, "checksums", dict(sorted(checksums.items())))
        object.__setattr__(self, "metadata", metadata)

    @property
    def event_identity(self) -> str:
        return self.event_id

    @property
    def run_id(self) -> str | None:
        return self.identity.run_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "occurred_at": self.occurred_at.isoformat(),
            "identity": self.identity.to_dict(),
            "status": self.status,
            "reason_code": self.reason_code,
            "sequence": self.sequence,
            "stream_id": self.stream_id,
            "refs": list(self.refs),
            "checksums": dict(self.checksums),
            "metadata": dict(self.metadata),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeEventEnvelope":
        payload = dict(value)
        payload["occurred_at"] = datetime.fromisoformat(payload["occurred_at"]) if isinstance(payload.get("occurred_at"), str) else payload.get("occurred_at")
        payload["identity"] = RuntimeEventIdentity(**dict(payload.get("identity") or {}))
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class RuntimeEventCursor:
    stream_id: str
    sequence: int
    checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream_id", _text(self.stream_id, "stream_id"))
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("cursor sequence must be non-negative")
        checksum = _text(self.checksum, "checksum", max_length=80).lower()
        if _CHECKSUM.fullmatch(checksum) is None:
            raise ValueError("cursor checksum must use sha256 format")
        object.__setattr__(self, "checksum", checksum)

    def encode(self) -> str:
        return f"{self.stream_id}:{self.sequence}:{self.checksum}"

    @classmethod
    def decode(cls, value: str) -> "RuntimeEventCursor":
        raw = _text(value, "cursor", max_length=MAX_CURSOR_LENGTH)
        parts = raw.rsplit(":", 2)
        if len(parts) != 3:
            raise RuntimeCursorConflict("cursor is malformed")
        try:
            sequence = int(parts[1])
        except ValueError as exc:
            raise RuntimeCursorConflict("cursor sequence is invalid") from exc
        return cls(parts[0], sequence, parts[2])


@dataclass(frozen=True, slots=True)
class RuntimeEventPage:
    events: tuple[RuntimeEventEnvelope, ...]
    cursor: RuntimeEventCursor | None
    has_more: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [event.to_dict() for event in self.events],
            "cursor": self.cursor.encode() if self.cursor is not None else None,
            "has_more": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    identity: RuntimeEventIdentity
    status: str
    reason_code: str | None
    last_event_id: str
    sequence: int
    updated_at: datetime
    refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "status": self.status,
            "reason_code": self.reason_code,
            "last_event_id": self.last_event_id,
            "sequence": self.sequence,
            "updated_at": self.updated_at.isoformat(),
            "refs": list(self.refs),
        }


@runtime_checkable
class RuntimeEventStorePort(Protocol):
    def append(self, event: RuntimeEventEnvelope) -> RuntimeEventEnvelope: ...

    def read(self, *, stream_id: str, after_sequence: int = 0, limit: int = 100) -> RuntimeEventPage: ...


class InMemoryRuntimeEventStore:
    """Canonical append-only store with identity and sequence conflict checks."""

    def __init__(self, events: Sequence[RuntimeEventEnvelope | Mapping[str, Any]] | None = None) -> None:
        self._events: dict[str, list[RuntimeEventEnvelope]] = {}
        self._by_id: dict[str, RuntimeEventEnvelope] = {}
        self._lock = threading.RLock()
        for event in events or ():
            self.append(event if isinstance(event, RuntimeEventEnvelope) else RuntimeEventEnvelope.from_dict(event))

    def append(self, event: RuntimeEventEnvelope) -> RuntimeEventEnvelope:
        if not isinstance(event, RuntimeEventEnvelope):
            raise TypeError("event must be RuntimeEventEnvelope")
        with self._lock:
            existing = self._by_id.get(event.event_id)
            if existing is not None:
                comparable = replace(
                    event,
                    stream_id=existing.stream_id,
                    sequence=existing.sequence,
                )
                if existing != comparable:
                    raise RuntimeEventIdentityConflict("event identity already has different content")
                return existing
            stream_id = event.stream_id or event.run_id or "runtime"
            stream = self._events.setdefault(stream_id, [])
            sequence = len(stream) + 1
            accepted = replace(event, stream_id=stream_id, sequence=sequence)
            stream.append(accepted)
            self._by_id[accepted.event_id] = accepted
            return accepted

    def read(self, *, stream_id: str, after_sequence: int = 0, limit: int = 100) -> RuntimeEventPage:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        with self._lock:
            stream = self._events.get(stream_id, [])
            page = tuple(item for item in stream if (item.sequence or 0) > after_sequence)[:limit]
            cursor = None
            if page:
                last = page[-1]
                cursor = RuntimeEventCursor(stream_id, last.sequence or 0, _checksum(last.to_dict()))
            has_more = len(stream) > after_sequence + len(page)
            return RuntimeEventPage(page, cursor, has_more)

    def all_events(self, *, stream_id: str | None = None) -> tuple[RuntimeEventEnvelope, ...]:
        with self._lock:
            if stream_id is not None:
                return tuple(self._events.get(stream_id, ()))
            return tuple(event for stream in self._events.values() for event in stream)


def _validate_runtime_cursor(store: RuntimeEventStorePort, cursor: RuntimeEventCursor) -> None:
    """Verify that a resume cursor still identifies its canonical event.

    The store port intentionally exposes only paged reads, so look up the
    single event immediately at the cursor position.  This keeps validation
    independent of a particular store implementation while rejecting gaps,
    sequence rewrites, stream mismatches, and checksum tampering.
    """
    if cursor.sequence == 0:
        return
    page = store.read(
        stream_id=cursor.stream_id,
        after_sequence=cursor.sequence - 1,
        limit=1,
    )
    if not page.events:
        raise RuntimeCursorConflict("cursor no longer matches canonical history")
    event = page.events[0]
    if (
        event.stream_id != cursor.stream_id
        or event.sequence != cursor.sequence
        or _checksum(event.to_dict()) != cursor.checksum
    ):
        raise RuntimeCursorConflict("cursor no longer matches canonical history")


class RuntimeEventProjection:
    """Idempotent read model built exclusively from canonical events."""

    def __init__(self, *, store: RuntimeEventStorePort | None = None) -> None:
        self._store = store or InMemoryRuntimeEventStore()
        self._seen: dict[str, RuntimeEventEnvelope] = {}
        self._sequence_seen: dict[tuple[str, int], RuntimeEventEnvelope] = {}
        self._status: dict[tuple[Any, ...], RuntimeStatus] = {}
        self._cursor: dict[str, RuntimeEventCursor] = {}
        self._lock = threading.RLock()
        # A projection attached to an already-populated local canonical store
        # must expose that committed history immediately.  Without this
        # rebuild, a restarted operator process would return an empty status
        # view until an unrelated new event arrived.
        if isinstance(self._store, InMemoryRuntimeEventStore):
            existing = self._store.all_events()
            if existing:
                self.rebuild(existing)

    @property
    def store(self) -> RuntimeEventStorePort:
        return self._store

    def append(self, event: RuntimeEventEnvelope | Mapping[str, Any]) -> RuntimeEventEnvelope:
        normalized = event if isinstance(event, RuntimeEventEnvelope) else RuntimeEventEnvelope.from_dict(event)
        accepted = self._store.append(normalized)
        self.apply(accepted)
        return accepted

    def apply(self, event: RuntimeEventEnvelope | Mapping[str, Any]) -> bool:
        normalized = event if isinstance(event, RuntimeEventEnvelope) else RuntimeEventEnvelope.from_dict(event)
        with self._lock:
            previous = self._seen.get(normalized.event_id)
            if previous is not None:
                if previous != normalized:
                    raise RuntimeEventIdentityConflict("duplicate event id has different content")
                return False
            stream = normalized.stream_id or normalized.run_id or "runtime"
            if normalized.sequence is not None:
                sequence_key = (stream, normalized.sequence)
                sequence_previous = self._sequence_seen.get(sequence_key)
                if sequence_previous is not None:
                    if sequence_previous != normalized:
                        raise RuntimeEventIdentityConflict(
                            "runtime stream sequence has conflicting event identity"
                        )
                    # The event id was not seen only when a caller supplied a
                    # semantically identical envelope after a projection
                    # rebuild; retain the first identity and treat it as a
                    # harmless at-least-once delivery.
                    return False
                self._sequence_seen[sequence_key] = normalized
                current = self._cursor.get(stream)
                current_sequence = current.sequence if current is not None else 0
                # Delivery may be out of order. Keep the checkpoint at the
                # highest contiguous prefix so a restart never skips a gap.
                next_sequence = current_sequence + 1
                last_contiguous = current
                while (stream, next_sequence) in self._sequence_seen:
                    contiguous = self._sequence_seen[(stream, next_sequence)]
                    last_contiguous = RuntimeEventCursor(
                        stream,
                        next_sequence,
                        _checksum(contiguous.to_dict()),
                    )
                    next_sequence += 1
                if last_contiguous is not None and (
                    current is None or last_contiguous.sequence > current.sequence
                ):
                    self._cursor[stream] = last_contiguous
            # Only commit the event id after all sequence/identity checks have
            # passed; a rejected delivery must remain retryable.
            self._seen[normalized.event_id] = normalized
            key = _status_key(normalized.identity)
            if normalized.status is not None or normalized.reason_code is not None:
                candidate = RuntimeStatus(
                    identity=normalized.identity,
                    status=normalized.status or normalized.event_type.value,
                    reason_code=normalized.reason_code,
                    last_event_id=normalized.event_id,
                    sequence=normalized.sequence or 0,
                    updated_at=normalized.occurred_at,
                    refs=normalized.refs,
                )
                previous_status = self._status.get(key)
                if previous_status is None or _status_order(candidate) >= _status_order(previous_status):
                    self._status[key] = candidate
            return True

    def consume(self, *, stream_id: str, limit: int = 100) -> RuntimeEventPage:
        with self._lock:
            cursor = self._cursor.get(stream_id)
            after = cursor.sequence if cursor else 0
        page = self._store.read(stream_id=stream_id, after_sequence=after, limit=limit)
        for event in page.events:
            self.apply(event)
        return page

    def rebuild(self, events: Iterable[RuntimeEventEnvelope | Mapping[str, Any]] | None = None) -> None:
        if events is None:
            if not isinstance(self._store, InMemoryRuntimeEventStore):
                raise RuntimeProjectionError("rebuild requires an explicit event iterable for this store")
            events = self._store.all_events()
        with self._lock:
            # Keep live applies behind the entire candidate fold. Once the
            # candidate is published, blocked deliveries continue against it
            # instead of being overwritten by the final swap.
            candidate = object.__new__(RuntimeEventProjection)
            candidate._store = self._store
            candidate._seen = {}
            candidate._sequence_seen = {}
            candidate._status = {}
            candidate._cursor = {}
            candidate._lock = threading.RLock()
            ordered = sorted(
                (
                    item
                    if isinstance(item, RuntimeEventEnvelope)
                    else RuntimeEventEnvelope.from_dict(item)
                    for item in events
                ),
                key=lambda item: (
                    item.stream_id or item.run_id or "runtime",
                    item.sequence or 0,
                    item.occurred_at,
                    item.event_id,
                ),
            )
            for event in ordered:
                candidate.apply(event)
            self._seen = candidate._seen
            self._sequence_seen = candidate._sequence_seen
            self._status = candidate._status
            self._cursor = candidate._cursor

    def status(
        self,
        *,
        identity: RuntimeEventIdentity | Mapping[str, Any] | None = None,
        run_id: str | None = None,
        node_id: str | None = None,
        node_instance_id: str | None = None,
        activity_id: str | None = None,
        attempt_id: str | None = None,
        child_id: str | None = None,
    ) -> tuple[RuntimeStatus, ...]:
        if identity is not None:
            target = identity if isinstance(identity, RuntimeEventIdentity) else RuntimeEventIdentity(**dict(identity))
            return tuple(status for key, status in self._status.items() if key == _status_key(target))
        result = tuple(self._status.values())
        if run_id is not None:
            result = tuple(item for item in result if item.identity.run_id == run_id)
        if node_id is not None:
            result = tuple(item for item in result if item.identity.node_id == node_id)
        if node_instance_id is not None:
            result = tuple(item for item in result if item.identity.node_instance_id == node_instance_id)
        if activity_id is not None:
            result = tuple(item for item in result if item.identity.activity_id == activity_id)
        if attempt_id is not None:
            result = tuple(item for item in result if item.identity.attempt_id == attempt_id)
        if child_id is not None:
            result = tuple(item for item in result if child_id in item.refs or item.identity.attempt_id == child_id)
        return tuple(sorted(result, key=lambda item: (item.updated_at, item.last_event_id)))

    def timeline(self, *, stream_id: str, after: RuntimeEventCursor | str | None = None, limit: int = 100) -> RuntimeEventPage:
        after_sequence = 0
        if after is not None:
            cursor = RuntimeEventCursor.decode(after) if isinstance(after, str) else after
            if cursor.stream_id != stream_id:
                raise RuntimeCursorConflict("cursor stream does not match query stream")
            # Validate that the cursor still refers to the canonical event.
            _validate_runtime_cursor(self._store, cursor)
            after_sequence = cursor.sequence
        return self._store.read(stream_id=stream_id, after_sequence=after_sequence, limit=limit)

    def cursor(self, stream_id: str) -> RuntimeEventCursor | None:
        return self._cursor.get(stream_id)


class RuntimeEventEmitter:
    """Small adapter for local runtime recorders.

    Local components may keep their native event model, but when this adapter
    is configured every fact is normalized before it reaches the canonical
    sink.  It contains no workflow mutation methods and therefore cannot grant
    routing, approval, publication, or memory authority to a worker.
    """

    _TYPE_MAP = {
        "agent_started": RuntimeEventType.TURN_STARTED,
        "agent_completed": RuntimeEventType.TURN_STOPPED,
        "agent_failed": RuntimeEventType.TURN_ABORTED,
        "agent_blocked": RuntimeEventType.TURN_ABORTED,
        "agent_stalled": RuntimeEventType.TURN_ABORTED,
        "agent_waiting_for_approval": RuntimeEventType.APPROVAL_REQUESTED,
        "tool_call": RuntimeEventType.TOOL_REQUESTED,
        "tool_call_requested": RuntimeEventType.TOOL_REQUESTED,
        "tool_approval_required": RuntimeEventType.APPROVAL_REQUESTED,
        "tool_approval_decided": RuntimeEventType.APPROVAL_DECIDED,
        "tool_started": RuntimeEventType.EXECUTION_STARTED,
        "attempt_started": RuntimeEventType.EXECUTION_STARTED,
        "tool_succeeded": RuntimeEventType.EXECUTION_TERMINAL,
        "tool_failed": RuntimeEventType.EXECUTION_TERMINAL,
        "tool_timeout": RuntimeEventType.TIMEOUT,
        "attempt_terminal": RuntimeEventType.EXECUTION_TERMINAL,
        "child_spawned": RuntimeEventType.CHILD_SPAWNED,
        "child_status": RuntimeEventType.CHILD_STATUS,
        "child_heartbeat": RuntimeEventType.CHILD_HEARTBEAT,
        "child_terminal": RuntimeEventType.CHILD_TERMINAL,
        "child_cancel_requested": RuntimeEventType.CANCEL_REQUESTED,
        "child_closed": RuntimeEventType.CHILD_TERMINAL,
        "worker_heartbeat": RuntimeEventType.WORKER_HEARTBEAT,
        "worker_status": RuntimeEventType.WORKER_STATUS,
        "context_compaction_planned": RuntimeEventType.CONTEXT_COMPACTION_PLANNED,
        "context_compaction_committed": RuntimeEventType.CONTEXT_COMPACTION_COMMITTED,
        "context_compaction_rejected": RuntimeEventType.CONTEXT_COMPACTION_REJECTED,
        "cancel_requested": RuntimeEventType.CANCEL_REQUESTED,
        "cancellation_confirmed": RuntimeEventType.CANCELLATION_CONFIRMED,
        "indeterminate": RuntimeEventType.INDETERMINATE,
    }

    def __init__(
        self,
        sink: Any,
        *,
        identity: RuntimeEventIdentity | Mapping[str, Any] | None = None,
        source: str = "harness",
        stream_id: str | None = None,
    ) -> None:
        if not callable(sink) and not any(hasattr(sink, name) for name in ("append", "publish")):
            raise TypeError("runtime event sink must be callable or expose append/publish")
        self._sink = sink
        self._identity = (
            identity
            if isinstance(identity, RuntimeEventIdentity)
            else RuntimeEventIdentity(**dict(identity or {}))
        )
        self._source = _text(source, "source")
        self._stream_id = stream_id

    def emit(
        self,
        event_type: RuntimeEventType | str,
        *,
        event_id: str | None = None,
        occurred_at: datetime | None = None,
        identity: RuntimeEventIdentity | Mapping[str, Any] | None = None,
        status: str | None = None,
        reason_code: str | None = None,
        refs: Iterable[str] | None = None,
        checksums: Mapping[str, str] | None = None,
        metadata: Mapping[str, Any] | None = None,
        stream_id: str | None = None,
        source: str | None = None,
    ) -> RuntimeEventEnvelope:
        local_type = str(event_type)
        canonical_type = self._TYPE_MAP.get(local_type, RuntimeEventType.RUNTIME_ERROR)
        resolved_identity = (
            identity
            if isinstance(identity, RuntimeEventIdentity)
            else RuntimeEventIdentity(**dict(identity))
            if identity is not None
            else self._identity
        )
        # Normalize one-shot inputs before deriving identity so the exact same
        # values reach both the event-id hash and the canonical envelope.
        resolved_refs = _bounded_refs(refs)
        resolved_checksums = _bounded_checksums(checksums)
        safe_metadata = redact_runtime_value(dict(metadata or {}))
        resolved_stream = stream_id or self._stream_id or resolved_identity.run_id
        if event_id is None:
            event_id = "runtime:" + hashlib.sha256(
                stable_json_dumps(
                    {
                        "stream_id": resolved_stream,
                        "event_type": canonical_type.value,
                        "identity": resolved_identity.to_dict(),
                        "status": status,
                        "reason_code": reason_code,
                        "refs": list(resolved_refs),
                        "checksums": resolved_checksums,
                        "metadata": safe_metadata,
                    }
                ).encode("utf-8")
            ).hexdigest()
        envelope = RuntimeEventEnvelope(
            event_id=event_id,
            event_type=canonical_type,
            occurred_at=occurred_at or datetime.now(UTC),
            identity=resolved_identity,
            status=status,
            reason_code=reason_code,
            stream_id=resolved_stream,
            refs=resolved_refs,
            checksums=resolved_checksums,
            metadata=safe_metadata,
            source=source or self._source,
        )
        result: Any = None
        if callable(self._sink):
            result = self._sink(envelope)
        elif hasattr(self._sink, "append"):
            result = self._sink.append(envelope)
        else:
            result = self._sink.publish(envelope)
        return result if isinstance(result, RuntimeEventEnvelope) else envelope


class RuntimeOperatorStatusService:
    """Read-only operator facade over :class:`RuntimeEventProjection`."""

    def __init__(self, projection: RuntimeEventProjection) -> None:
        self._projection = projection

    def get_status(
        self,
        *,
        run_id: str | None = None,
        identity: RuntimeEventIdentity | Mapping[str, Any] | None = None,
        node_id: str | None = None,
        node_instance_id: str | None = None,
        activity_id: str | None = None,
        attempt_id: str | None = None,
        child_id: str | None = None,
    ) -> tuple[RuntimeStatus, ...]:
        return self._projection.status(
            run_id=run_id,
            identity=identity,
            node_id=node_id,
            node_instance_id=node_instance_id,
            activity_id=activity_id,
            attempt_id=attempt_id,
            child_id=child_id,
        )

    def get_timeline(self, *, stream_id: str, cursor: RuntimeEventCursor | str | None = None, limit: int = 100) -> RuntimeEventPage:
        return self._projection.timeline(stream_id=stream_id, after=cursor, limit=limit)

    # Deliberately no approve/cancel/route methods.  Mutations must use the
    # existing application-service ports.


def runtime_event_publish_request(
    event: RuntimeEventEnvelope,
    *,
    producer_component: str = "harness-runtime",
    producer_version: str = "1",
) -> Any:
    """Build an :class:`EventPublishRequest` for the canonical durable port."""
    from framework.events.canonical import BusinessContext, ProducerIdentity
    from framework.events.runtime.publisher import EventPublishRequest

    identity = event.identity.graph_identity
    context = BusinessContext(
        run_id=event.run_id,
        execution_identity=identity,
        stage_id=event.identity.node_id,
        node_instance_id=event.identity.node_instance_id,
        request_id=event.event_id,
    )
    if event.event_type.value in _LEGACY_HARNESS_RUNTIME_TYPES:
        # Context-compaction facts retain their established graph-event
        # schemas during migration.  The legacy schemas accept only the
        # reference-only fact body, so do not pass the unified envelope fields
        # as unknown payload properties.
        payload = {
            "projection_schema": "harness-safe-summary/v1",
            **dict(event.metadata),
        }
    else:
        payload = {
            key: value
            for key, value in event.to_dict().items()
            if key not in {"event_id", "event_type", "occurred_at", "stream_id", "source"}
        }
    return EventPublishRequest(
        event_id=event.event_id,
        event_type=event.event_type.value,
        data_schema=(
            "newsroom.harness-graph-event/v1"
            if event.event_type.value in _LEGACY_HARNESS_RUNTIME_TYPES
            else RUNTIME_EVENT_DATA_SCHEMA
        ),
        source=event.source,
        occurred_at=event.occurred_at,
        stream_id=event.stream_id or event.run_id or "runtime",
        business_context=context,
        producer=ProducerIdentity(component=producer_component, version=producer_version),
        subject=event.identity.activity_id or event.identity.attempt_id,
        payload=payload,
    )


class CanonicalRuntimeEventPublisher:
    """Adapter that appends runtime facts through the existing EventRuntime."""

    def __init__(self, runtime: Any) -> None:
        if not hasattr(runtime, "publish"):
            raise TypeError("runtime must expose the canonical publish port")
        self._runtime = runtime

    def publish(self, event: RuntimeEventEnvelope) -> Any:
        return self._runtime.publish(runtime_event_publish_request(event))

    def append(self, event: RuntimeEventEnvelope) -> Any:
        return self.publish(event)


def _status_key(identity: RuntimeEventIdentity) -> tuple[Any, ...]:
    return (
        identity.graph_identity,
        identity.activity_id,
        identity.attempt_id,
        identity.node_id,
        identity.node_instance_id,
    )


def _status_order(status: RuntimeStatus) -> tuple[int, datetime, str]:
    """Order status transitions by canonical stream position, then time."""
    return (status.sequence, status.updated_at, status.last_event_id)


# Compatibility aliases make the contract discoverable from both the events
# and Harness runtime namespaces without introducing another implementation.
RuntimeEvent = RuntimeEventEnvelope
RuntimeEventProjectionService = RuntimeOperatorStatusService
RuntimeEventProjector = RuntimeEventProjection
InMemoryRuntimeEventLog = InMemoryRuntimeEventStore


__all__ = [
    "InMemoryRuntimeEventLog",
    "InMemoryRuntimeEventStore",
    "CanonicalRuntimeEventPublisher",
    "MAX_RUNTIME_METADATA_BYTES",
    "ProtectedPayloadRejected",
    "RUNTIME_EVENT_SCHEMA_V1",
    "RUNTIME_EVENT_DATA_SCHEMA",
    "RuntimeCursorConflict",
    "RuntimeEvent",
    "RuntimeEventEnvelope",
    "RuntimeEventIdentity",
    "RuntimeEventIdentityConflict",
    "RuntimeEventPage",
    "RuntimeEventProjection",
    "RuntimeEventProjectionService",
    "RuntimeEventProjector",
    "RuntimeEventEmitter",
    "RuntimeEventStorePort",
    "RuntimeEventType",
    "RuntimeOperatorStatusService",
    "RuntimeProjectionError",
    "RuntimeStatus",
    "redact_runtime_value",
    "runtime_event_publish_request",
]
