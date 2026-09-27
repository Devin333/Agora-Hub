from __future__ import annotations

import dis
import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import CodeType, FunctionType, ModuleType
from typing import TYPE_CHECKING, Any, Never, Protocol, TypeAlias, TypeVar

from framework.events.canonical import (
    CanonicalValue,
    StoredEvent,
    checksum_for,
    normalize_canonical_json,
    thaw_canonical_json,
)
from framework.events.errors import EventIntegrityError, EventQuarantineError, EventReplayError
from framework.events.runtime.fallback import (
    LocalRuntimeDiagnosticFallback,
    RuntimeDiagnosticCategory,
    RuntimeDiagnosticComponent,
    RuntimeDiagnosticOperation,
)
from framework.events.runtime.history import (
    DETERMINISTIC_HISTORY_EXTENSION,
    DeterministicHistoryRecord,
    HistoryVerificationError,
    HistoryVerificationState,
    HistoryVerifier,
)
from framework.events.runtime.models import (
    MAX_PAGE_LIMIT,
    EventPage,
    QuarantineReason,
    QuarantineRecord,
    ReplayMode,
    ReplayReport,
    ReplayStartRequest,
    ReplayStatus,
    ReplayVersion,
    StreamReadRequest,
    StreamSequenceCursor,
)
from framework.events.schema.catalog import EventSchemaCatalog, HistoricalSchemaResolution
from framework.events.telemetry import (
    EventTelemetry,
    TelemetryInstrumentationScope,
    TelemetryResource,
    default_event_telemetry,
)

if TYPE_CHECKING:
    from framework.events.ports import EventStorePort


ReplayReducer: TypeAlias = Callable[[CanonicalValue, "ReplayEvent"], Any]
ReplayClock: TypeAlias = Callable[[], datetime]
_T = TypeVar("_T")


class ReplayCheckpointStorePort(Protocol):
    """Durable checkpoint slot separate from immutable source history.

    A save replaces only the same replay-owned checkpoint slot with a later
    checksum-verified sequence.  Implementations must make the checkpoint
    durable before returning; an in-memory implementation is test-only.
    """

    def save_checkpoint(self, checkpoint: ReplayCheckpoint) -> ReplayCheckpoint: ...

    def get_checkpoint(
        self,
        checkpoint_id: str,
        *,
        tenant_id: str | None = None,
    ) -> ReplayCheckpoint | None: ...

_CHECKSUM_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
_FORBIDDEN_REDUCER_OPCODES = frozenset(
    {
        "BUILD_SET",
        "DELETE_DEREF",
        "DELETE_GLOBAL",
        "IMPORT_FROM",
        "IMPORT_NAME",
        "IMPORT_STAR",
        "LOAD_BUILD_CLASS",
        "MAKE_FUNCTION",
        "STORE_DEREF",
        "STORE_GLOBAL",
        "SET_ADD",
        "SET_UPDATE",
    }
)
_ALLOWED_REDUCER_BUILTINS = frozenset(
    {
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "float",
        "int",
        "len",
        "list",
        "max",
        "min",
        "range",
        "round",
        "sorted",
        "str",
        "sum",
        "tuple",
        "ValueError",
    }
)
_ALLOWED_REPLAY_EVENT_ATTRIBUTES = frozenset(
    {
        "applied_upcasters",
        "data_schema",
        "event_id",
        "event_type",
        "occurred_at",
        "payload",
        "record_checksum",
        "source_data_schema",
        "stream_id",
        "stream_sequence",
    }
)
_ALLOWED_CANONICAL_METHODS = frozenset(
    {
        "count",
        "endswith",
        "get",
        "index",
        "items",
        "join",
        "keys",
        "lower",
        "lstrip",
        "replace",
        "rsplit",
        "rstrip",
        "split",
        "startswith",
        "strip",
        "upper",
        "values",
    }
)
_FORBIDDEN_REFLECTION_CONSTANTS = frozenset(
    {
        "__builtins__",
        "__class__",
        "__closure__",
        "__code__",
        "__dict__",
        "__func__",
        "__getattribute__",
        "__globals__",
        "__mro__",
        "__subclasses__",
        "cr_frame",
        "f_builtins",
        "f_globals",
        "f_locals",
        "func_globals",
        "gi_frame",
        "mro",
    }
)


class ReplayCoreError(EventReplayError):
    """Base class for deterministic replay-core failures."""


class ReplayModeError(ReplayCoreError, ValueError):
    """Raised when an entrypoint is used with a different replay mode."""


class ReplayRedeliveryDelegationRequired(ReplayCoreError):
    """REDELIVER belongs to the normal durable delivery ledger, not this core."""


class ReplayReducerRegistrationError(ReplayCoreError, ValueError):
    """Raised when a reducer cannot satisfy the capability-free pure contract."""


class ReplayExecutionFailure(ReplayCoreError):
    """A typed replay halt whose durable report has already been updated."""

    def __init__(
        self,
        *,
        reason_class: str,
        sequence: int | None,
        report: ReplayReport | None,
        checkpoint: ReplayCheckpoint | None,
    ) -> None:
        self.reason_class = reason_class
        self.sequence = sequence
        self.report = report
        self.checkpoint = checkpoint
        location = "" if sequence is None else f" at sequence {sequence}"
        super().__init__(f"deterministic replay failed{location}: {reason_class}")


class ReplayCheckpointError(ReplayExecutionFailure):
    """The supplied resume checkpoint or exclusive offset is incompatible."""


class ReplayHistoryOrderError(ReplayExecutionFailure):
    """The durable reader returned a gap, duplicate, or out-of-order record."""


class ReplayHistoryIntegrityError(ReplayExecutionFailure):
    """A stored source event failed canonical integrity verification."""


class ReplayHistorySchemaError(ReplayExecutionFailure):
    """A source event could not be validated or deterministically upcast."""


class ReplayReducerExecutionError(ReplayExecutionFailure):
    """A registered reducer failed or produced a nondeterministic value."""


class ReplaySourceReadError(ReplayExecutionFailure):
    """The authoritative replay source failed while reading a fixed prefix."""


class ReplayHistoryVerificationError(ReplayExecutionFailure):
    """Deterministic commands, activities, or handler versions did not replay."""


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """Schema-resolved immutable input exposed to a pure reducer."""

    event_id: str
    event_type: str
    source_data_schema: str
    data_schema: str
    stream_id: str
    stream_sequence: int
    occurred_at: str
    payload: Mapping[str, Any]
    record_checksum: str
    history: Mapping[str, Any] | None = None
    applied_upcasters: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "event_type",
            "source_data_schema",
            "data_schema",
            "stream_id",
            "occurred_at",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "stream_sequence",
            _positive_int(self.stream_sequence, "stream_sequence"),
        )
        object.__setattr__(
            self,
            "record_checksum",
            _required_checksum(self.record_checksum, "record_checksum"),
        )
        normalized = _normalize_replay_json(self.payload, path="$.replay.payload")
        if not isinstance(normalized, Mapping):
            raise TypeError("replay payload must be an object")
        object.__setattr__(self, "payload", normalized)
        history = self.history
        if history is not None:
            history = _normalize_replay_json(history, path="$.replay.history")
            if not isinstance(history, Mapping):
                raise TypeError("replay history must be an object")
        object.__setattr__(self, "history", history)
        object.__setattr__(
            self,
            "applied_upcasters",
            tuple(
                _required_text(value, "applied_upcaster")
                for value in self.applied_upcasters
            ),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source_data_schema": self.source_data_schema,
            "data_schema": self.data_schema,
            "stream_id": self.stream_id,
            "stream_sequence": self.stream_sequence,
            "occurred_at": self.occurred_at,
            "payload": thaw_canonical_json(self.payload),
            "record_checksum": self.record_checksum,
            "history": thaw_canonical_json(self.history),
            "applied_upcasters": list(self.applied_upcasters),
        }


@dataclass(frozen=True, slots=True)
class ReplayReducerRegistration:
    reducer_id: str
    version: str
    reducer: ReplayReducer
    initial_state: Any = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reducer_id", _required_text(self.reducer_id, "reducer_id"))
        object.__setattr__(self, "version", _required_text(self.version, "version"))
        _audit_reducer(self.reducer)
        object.__setattr__(
            self,
            "initial_state",
            _normalize_replay_json(self.initial_state, path="$.replay.initial_state"),
        )


class ReplayReducerRegistry:
    """Versioned registry whose reducers receive no runtime capabilities.

    Registration applies a fail-closed bytecode contract for canonical data
    access, reflection, imports, and unordered containers.  It is not a Python
    sandbox: only trusted, reviewed release code may be registered.
    """

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], ReplayReducerRegistration] = {}

    def register(self, registration: ReplayReducerRegistration) -> None:
        if not isinstance(registration, ReplayReducerRegistration):
            raise TypeError("registration must be ReplayReducerRegistration")
        key = (registration.reducer_id, registration.version)
        if key in self._registrations:
            raise ReplayReducerRegistrationError(
                f"duplicate replay reducer registration: {key[0]} ({key[1]})"
            )
        self._registrations[key] = registration

    def get(self, reducer_id: str, version: str) -> ReplayReducerRegistration:
        key = (
            _required_text(reducer_id, "reducer_id"),
            _required_text(version, "reducer_version"),
        )
        try:
            return self._registrations[key]
        except KeyError as exc:
            raise ReplayReducerRegistrationError(
                f"unregistered replay reducer: {key[0]} ({key[1]})"
            ) from exc

    def registrations(self) -> tuple[ReplayReducerRegistration, ...]:
        return tuple(self._registrations[key] for key in sorted(self._registrations))


@dataclass(frozen=True, slots=True)
class ReplayCheckpoint:
    """Portable, checksum-verified reducer/history resume state."""

    checkpoint_id: str
    mode: ReplayMode
    source_stream_id: str
    last_sequence: int
    source_high_watermark: int
    runtime_version: str
    schema_catalog_version: str
    history_checksum: str
    last_event_id: str | None = None
    state: Any = None
    reducer_id: str | None = None
    reducer_version: str | None = None
    parent_checkpoint_id: str | None = None
    tenant_id: str | None = None
    applied_upcasters: tuple[str, ...] = ()
    versions: tuple[ReplayVersion, ...] = ()
    verification_state: Mapping[str, Any] | None = None
    checkpoint_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checkpoint_id",
            _required_text(self.checkpoint_id, "checkpoint_id"),
        )
        mode = ReplayMode(self.mode)
        if mode is ReplayMode.REDELIVER:
            raise ValueError("REDELIVER cannot use a deterministic replay checkpoint")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self,
            "source_stream_id",
            _required_text(self.source_stream_id, "source_stream_id"),
        )
        last_sequence = _nonnegative_int(self.last_sequence, "last_sequence")
        source_high_watermark = _nonnegative_int(
            self.source_high_watermark,
            "source_high_watermark",
        )
        if last_sequence > source_high_watermark:
            raise ValueError("last_sequence cannot exceed source_high_watermark")
        object.__setattr__(self, "last_sequence", last_sequence)
        object.__setattr__(self, "source_high_watermark", source_high_watermark)
        last_event_id = _optional_text(self.last_event_id, "last_event_id")
        if last_sequence == 0 and last_event_id is not None:
            raise ValueError("sequence-zero replay checkpoint cannot reference an event")
        object.__setattr__(self, "last_event_id", last_event_id)
        object.__setattr__(
            self,
            "runtime_version",
            _required_text(self.runtime_version, "runtime_version"),
        )
        object.__setattr__(
            self,
            "schema_catalog_version",
            _required_text(self.schema_catalog_version, "schema_catalog_version"),
        )
        object.__setattr__(
            self,
            "history_checksum",
            _required_checksum(self.history_checksum, "history_checksum"),
        )
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        reducer_id = _optional_text(self.reducer_id, "reducer_id")
        reducer_version = _optional_text(self.reducer_version, "reducer_version")
        if mode is ReplayMode.REBUILD_STATE and (
            reducer_id is None or reducer_version is None
        ):
            raise ValueError("REBUILD_STATE checkpoint requires a reducer version")
        if mode is ReplayMode.VERIFY_HISTORY and (
            reducer_id is not None or reducer_version is not None
        ):
            raise ValueError("VERIFY_HISTORY checkpoint cannot contain reducer state")
        object.__setattr__(self, "reducer_id", reducer_id)
        object.__setattr__(self, "reducer_version", reducer_version)
        object.__setattr__(
            self,
            "parent_checkpoint_id",
            _optional_text(self.parent_checkpoint_id, "parent_checkpoint_id"),
        )
        if self.parent_checkpoint_id == self.checkpoint_id:
            raise ValueError("checkpoint cannot be its own parent")
        state = _normalize_replay_json(self.state, path="$.replay.checkpoint.state")
        if mode is ReplayMode.VERIFY_HISTORY and state is not None:
            raise ValueError("VERIFY_HISTORY checkpoint state must be null")
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "applied_upcasters",
            tuple(
                _required_text(value, "applied_upcaster")
                for value in self.applied_upcasters
            ),
        )
        versions = tuple(self.versions)
        if any(not isinstance(value, ReplayVersion) for value in versions):
            raise TypeError("versions must contain ReplayVersion values")
        if not versions:
            derived = [
                ReplayVersion("replay_runtime", self.runtime_version),
                ReplayVersion("schema_catalog", self.schema_catalog_version),
            ]
            if reducer_id is not None and reducer_version is not None:
                derived.append(
                    ReplayVersion(f"reducer:{reducer_id}", reducer_version)
                )
            versions = tuple(derived)
        components = tuple(value.component for value in versions)
        if len(set(components)) != len(components):
            raise ValueError("replay checkpoint versions must have unique components")
        required_versions = {
            "replay_runtime": self.runtime_version,
            "schema_catalog": self.schema_catalog_version,
        }
        if reducer_id is not None and reducer_version is not None:
            required_versions[f"reducer:{reducer_id}"] = reducer_version
        actual_versions = {value.component: value.version for value in versions}
        if any(
            actual_versions.get(key) != value
            for key, value in required_versions.items()
        ):
            raise ValueError("replay checkpoint versions conflict with pinned handlers")
        object.__setattr__(self, "versions", versions)
        verification_state = self.verification_state
        if verification_state is not None:
            if mode is not ReplayMode.VERIFY_HISTORY:
                raise ValueError(
                    "verification_state is valid only for VERIFY_HISTORY checkpoints"
                )
            parsed_verification_state = HistoryVerificationState.from_checkpoint(
                verification_state
            )
            _validate_verification_pinned_versions(
                versions,
                parsed_verification_state,
            )
            verification_state = parsed_verification_state.to_checkpoint()
        object.__setattr__(self, "verification_state", verification_state)
        object.__setattr__(
            self,
            "checkpoint_checksum",
            checksum_for(self.checksum_projection()),
        )

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "mode": self.mode.value,
            "source_stream_id": self.source_stream_id,
            "last_sequence": self.last_sequence,
            "source_high_watermark": self.source_high_watermark,
            "last_event_id": self.last_event_id,
            "runtime_version": self.runtime_version,
            "schema_catalog_version": self.schema_catalog_version,
            "history_checksum": self.history_checksum,
            "state": thaw_canonical_json(self.state),
            "reducer_id": self.reducer_id,
            "reducer_version": self.reducer_version,
            "parent_checkpoint_id": self.parent_checkpoint_id,
            "tenant_id": self.tenant_id,
            "applied_upcasters": list(self.applied_upcasters),
            "versions": [
                {"component": value.component, "version": value.version}
                for value in self.versions
            ],
            "verification_state": thaw_canonical_json(self.verification_state),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.checksum_projection(),
            "checkpoint_checksum": self.checkpoint_checksum,
        }

    def verify_integrity(self) -> None:
        if checksum_for(self.checksum_projection()) != self.checkpoint_checksum:
            raise EventIntegrityError("replay checkpoint checksum does not match")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReplayCheckpoint:
        allowed = {
            "checkpoint_id",
            "mode",
            "source_stream_id",
            "last_sequence",
            "source_high_watermark",
            "last_event_id",
            "runtime_version",
            "schema_catalog_version",
            "history_checksum",
            "state",
            "reducer_id",
            "reducer_version",
            "parent_checkpoint_id",
            "tenant_id",
            "applied_upcasters",
            "versions",
            "verification_state",
            "checkpoint_checksum",
        }
        unknown = sorted(str(key) for key in value if key not in allowed)
        if unknown:
            raise ValueError(
                f"unknown replay checkpoint field(s): {', '.join(unknown)}"
            )
        checkpoint = cls(
            checkpoint_id=value.get("checkpoint_id"),
            mode=value.get("mode"),
            source_stream_id=value.get("source_stream_id"),
            last_sequence=value.get("last_sequence"),
            source_high_watermark=value.get("source_high_watermark"),
            last_event_id=value.get("last_event_id"),
            runtime_version=value.get("runtime_version"),
            schema_catalog_version=value.get("schema_catalog_version"),
            history_checksum=value.get("history_checksum"),
            state=value.get("state"),
            reducer_id=value.get("reducer_id"),
            reducer_version=value.get("reducer_version"),
            parent_checkpoint_id=value.get("parent_checkpoint_id"),
            tenant_id=value.get("tenant_id"),
            applied_upcasters=tuple(value.get("applied_upcasters") or ()),
            versions=tuple(
                item
                if isinstance(item, ReplayVersion)
                else ReplayVersion(
                    component=item.get("component"),
                    version=item.get("version"),
                )
                for item in (value.get("versions") or ())
            ),
            verification_state=value.get("verification_state"),
        )
        supplied = str(value.get("checkpoint_checksum") or "").lower()
        if supplied != checkpoint.checkpoint_checksum:
            raise EventIntegrityError("replay checkpoint checksum does not match")
        return checkpoint


@dataclass(frozen=True, slots=True)
class ReplayExecutionResult:
    report: ReplayReport
    checkpoint: ReplayCheckpoint
    state: Any = None

    def __post_init__(self) -> None:
        if self.report.status is not ReplayStatus.SUCCEEDED:
            raise ValueError("replay result requires a successful report")
        if self.checkpoint.last_sequence != self.report.high_watermark:
            raise ValueError("replay result checkpoint must reach the report watermark")
        if self.report.checkpoint_ref != self.checkpoint.checkpoint_id:
            raise ValueError("replay report must reference its generated checkpoint")
        object.__setattr__(
            self,
            "state",
            _normalize_replay_json(self.state, path="$.replay.result.state"),
        )


@dataclass(slots=True)
class _ReplayProgress:
    state: CanonicalValue
    history_checksum: str
    last_sequence: int
    last_event_id: str | None
    verification_state: HistoryVerificationState | None
    applied_upcasters: list[str]
    durable_report: ReplayReport
    parent_checkpoint_id: str | None = None
    durable_checkpoint: ReplayCheckpoint | None = None


@dataclass(frozen=True, slots=True)
class _ReplayIssue(Exception):
    reason_class: str
    sequence: int | None
    error_type: type[ReplayExecutionFailure]
    quarantine_reason: QuarantineReason | None = None


class DeterministicReplayEngine:
    """Finite, source-read-only replay for reducers and history validation.

    Command comparison and recorded nondeterministic activity resolution are
    intentionally separate capabilities.  This core never owns a bus or a
    delivery handler, so ``REDELIVER`` cannot accidentally become live replay.
    At this engine boundary, the caller's ``request.checkpoint_ref`` selects an
    input checkpoint.  The durable report receives a distinct replay-owned
    output slot derived from ``replay_id``; the generated checkpoint records
    input lineage through ``parent_checkpoint_id``.
    """

    def __init__(
        self,
        store: EventStorePort,
        catalog: EventSchemaCatalog,
        reducers: ReplayReducerRegistry,
        checkpoint_store: ReplayCheckpointStorePort,
        *,
        runtime_version: str,
        schema_catalog_version: str,
        clock: ReplayClock,
        page_size: int = 100,
        history_verifier: HistoryVerifier | None = None,
        diagnostic_fallback: LocalRuntimeDiagnosticFallback | None = None,
        telemetry: EventTelemetry | None = None,
    ) -> None:
        if not isinstance(catalog, EventSchemaCatalog):
            raise TypeError("catalog must be EventSchemaCatalog")
        if not isinstance(reducers, ReplayReducerRegistry):
            raise TypeError("reducers must be ReplayReducerRegistry")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if isinstance(page_size, bool) or not isinstance(page_size, int):
            raise TypeError("page_size must be an integer")
        if page_size < 1 or page_size > MAX_PAGE_LIMIT:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_LIMIT}")
        self._store = store
        self._catalog = catalog
        self._reducers = reducers
        self._checkpoints = checkpoint_store
        self._runtime_version = _required_text(runtime_version, "runtime_version")
        self._schema_catalog_version = _required_text(
            schema_catalog_version,
            "schema_catalog_version",
        )
        self._clock = clock
        self._page_size = page_size
        if history_verifier is not None and not isinstance(
            history_verifier,
            HistoryVerifier,
        ):
            raise TypeError("history_verifier must be HistoryVerifier")
        self._history_verifier = history_verifier
        self._diagnostic_fallback = (
            diagnostic_fallback
            if diagnostic_fallback is not None
            else LocalRuntimeDiagnosticFallback()
        )
        self._telemetry = telemetry or default_event_telemetry(
            resource=TelemetryResource(service_name="newsroom-event-runtime"),
            scope=TelemetryInstrumentationScope(
                name="framework.events.replay",
                version="1",
            ),
        )

    @property
    def diagnostic_fallback(self) -> LocalRuntimeDiagnosticFallback:
        return self._diagnostic_fallback

    def execute(
        self,
        request: ReplayStartRequest,
        *,
        reducer_id: str | None = None,
        reducer_version: str | None = None,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult:
        if request.mode is ReplayMode.REBUILD_STATE:
            if reducer_id is None or reducer_version is None:
                raise ReplayReducerRegistrationError(
                    "REBUILD_STATE requires reducer_id and reducer_version"
                )
            return self.rebuild_state(
                request,
                reducer_id=reducer_id,
                reducer_version=reducer_version,
                checkpoint=checkpoint,
                after_sequence=after_sequence,
            )
        if request.mode is ReplayMode.VERIFY_HISTORY:
            if reducer_id is not None or reducer_version is not None:
                raise ReplayModeError("VERIFY_HISTORY does not execute reducers")
            return self.verify_history(
                request,
                checkpoint=checkpoint,
                after_sequence=after_sequence,
            )
        return self.redeliver(request)

    def rebuild_state(
        self,
        request: ReplayStartRequest,
        *,
        reducer_id: str,
        reducer_version: str,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult:
        _require_mode(request, ReplayMode.REBUILD_STATE)
        registration = self._reducers.get(reducer_id, reducer_version)
        return self._execute_finite(
            request,
            registration=registration,
            checkpoint=checkpoint,
            after_sequence=after_sequence,
        )

    def verify_history(
        self,
        request: ReplayStartRequest,
        *,
        checkpoint: ReplayCheckpoint | None = None,
        after_sequence: int | None = None,
    ) -> ReplayExecutionResult:
        _require_mode(request, ReplayMode.VERIFY_HISTORY)
        if self._history_verifier is None:
            raise ReplayModeError(
                "VERIFY_HISTORY requires a deterministic history verifier"
            )
        return self._execute_finite(
            request,
            registration=None,
            checkpoint=checkpoint,
            after_sequence=after_sequence,
        )

    def redeliver(self, request: ReplayStartRequest) -> Never:
        _require_mode(request, ReplayMode.REDELIVER)
        raise ReplayRedeliveryDelegationRequired(
            "REDELIVER must be authorized and scheduled through the durable delivery ledger"
        )

    def _execute_finite(
        self,
        request: ReplayStartRequest,
        *,
        registration: ReplayReducerRegistration | None,
        checkpoint: ReplayCheckpoint | None,
        after_sequence: int | None,
    ) -> ReplayExecutionResult:
        try:
            result = self._execute_finite_impl(
                request,
                registration=registration,
                checkpoint=checkpoint,
                after_sequence=after_sequence,
            )
        except Exception as error:
            self._record_replay_metrics(request.mode, result="failed", error=error)
            raise
        self._record_replay_metrics(request.mode, result="success")
        return result

    def _execute_finite_impl(
        self,
        request: ReplayStartRequest,
        *,
        registration: ReplayReducerRegistration | None,
        checkpoint: ReplayCheckpoint | None,
        after_sequence: int | None,
    ) -> ReplayExecutionResult:
        input_checkpoint_ref = request.checkpoint_ref
        effective_request = _with_output_checkpoint_ref(request)
        pending = self._store_call(
            lambda: self._store.begin_replay(effective_request),
            failure_operation=RuntimeDiagnosticOperation.REPLAY_BEGIN_FAILED,
        )
        try:
            self._verify_started_report(effective_request, pending)
        except Exception as error:
            self._record_store_failure(
                RuntimeDiagnosticOperation.REPLAY_BEGIN_REPORT_INVALID,
                error,
            )
            raise
        base_versions = self._versions(registration)
        if pending.status is ReplayStatus.FAILED:
            raise ReplayCoreError("terminal replay reports cannot be executed again")
        if pending.status is ReplayStatus.SUCCEEDED:
            if pending.to_sequence != pending.high_watermark:
                raise ReplayCoreError("terminal replay report watermark is incomplete")
            output_ref = pending.checkpoint_ref
            output_checkpoint = self._store_call(
                lambda: self._checkpoints.get_checkpoint(
                    output_ref or "",
                    tenant_id=pending.tenant_id,
                ),
                failure_operation=RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_FAILED,
                report=pending,
            )
            if output_checkpoint is None:
                raise ReplaySourceReadError(
                    reason_class="missing_durable_checkpoint",
                    sequence=pending.to_sequence,
                    report=pending,
                    checkpoint=None,
                )
            output_checkpoint = self._validated_store_checkpoint(
                lambda: self._validate_loaded_output_checkpoint(
                    pending,
                    output_checkpoint,
                    expected_parent_id=input_checkpoint_ref,
                )
            )
            try:
                output_checkpoint.verify_integrity()
            except EventIntegrityError:
                raise ReplaySourceReadError(
                    reason_class="corrupt_checkpoint",
                    sequence=pending.to_sequence,
                    report=pending,
                    checkpoint=None,
                ) from None
            if output_checkpoint.last_sequence != pending.high_watermark:
                raise ReplayCoreError("terminal replay checkpoint watermark is incomplete")
            try:
                self._validate_checkpoint(
                    effective_request,
                    pending,
                    output_checkpoint,
                    registration=registration,
                )
            except _ReplayIssue as issue:
                raise issue.error_type(
                    reason_class=issue.reason_class,
                    sequence=issue.sequence,
                    report=pending,
                    checkpoint=output_checkpoint,
                ) from None
            if checkpoint is not None:
                try:
                    checkpoint.verify_integrity()
                except EventIntegrityError:
                    raise ReplaySourceReadError(
                        reason_class="corrupt_checkpoint",
                        sequence=pending.to_sequence,
                        report=pending,
                        checkpoint=None,
                    ) from None
                try:
                    self._validate_checkpoint(
                        effective_request,
                        pending,
                        checkpoint,
                        registration=registration,
                    )
                except _ReplayIssue as issue:
                    raise issue.error_type(
                        reason_class=issue.reason_class,
                        sequence=issue.sequence,
                        report=pending,
                        checkpoint=output_checkpoint,
                    ) from None
                if checkpoint.checkpoint_id != input_checkpoint_ref:
                    raise ReplayCheckpointError(
                        reason_class="checkpoint_reference_mismatch",
                        sequence=None,
                        report=pending,
                        checkpoint=output_checkpoint,
                    )
            expected_after_sequence = checkpoint.last_sequence if checkpoint is not None else 0
            if after_sequence not in (None, expected_after_sequence):
                raise ReplayCheckpointError(
                    reason_class="after_sequence_mismatch",
                    sequence=after_sequence,
                    report=pending,
                    checkpoint=output_checkpoint,
                )
            expected_checksum = _replay_result_checksum(
                output_checkpoint,
                registration=registration,
            )
            if pending.result_checksum != expected_checksum:
                raise ReplaySourceReadError(
                    reason_class="replay_result_checksum_mismatch",
                    sequence=pending.to_sequence,
                    report=pending,
                    checkpoint=output_checkpoint,
                )
            return ReplayExecutionResult(
                report=pending,
                checkpoint=output_checkpoint,
                state=(
                    output_checkpoint.state
                    if registration is not None
                    else None
                ),
            )
        if pending.status is ReplayStatus.RUNNING and not _versions_extend(
            base_versions,
            pending.versions,
        ):
            raise ReplayCoreError(
                "running replay report pins incompatible component versions"
            )

        progress: _ReplayProgress | None = None
        current_report = pending
        try:
            if pending.status is ReplayStatus.PENDING:
                resume_checkpoint = self._resolve_pending_checkpoint(
                    pending,
                    input_checkpoint_ref=input_checkpoint_ref,
                    supplied_checkpoint=checkpoint,
                )
                progress = self._prepare_progress(
                    effective_request,
                    pending,
                    registration=registration,
                    checkpoint=resume_checkpoint,
                    after_sequence=after_sequence,
                )
                initial_checkpoint = self._persist_progress_checkpoint(
                    effective_request,
                    progress,
                    registration=registration,
                )
                running_candidate = replace(
                    pending,
                    status=ReplayStatus.RUNNING,
                    versions=self._progress_versions(registration, progress),
                )
                current_report = self._store_call(
                    lambda: self._store.update_replay_report(running_candidate),
                    failure_operation=(
                        RuntimeDiagnosticOperation.REPLAY_RUNNING_REPORT_UPDATE_FAILED
                    ),
                    report=pending,
                    checkpoint=initial_checkpoint,
                )
                current_report = self._verify_updated_report(
                    running_candidate,
                    current_report,
                    report=pending,
                    checkpoint=initial_checkpoint,
                )
                progress.durable_report = current_report
            else:
                output_ref = pending.checkpoint_ref
                output_checkpoint = self._store_call(
                    lambda: self._checkpoints.get_checkpoint(
                        output_ref or "",
                        tenant_id=pending.tenant_id,
                    ),
                    failure_operation=(
                        RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_FAILED
                    ),
                    report=pending,
                )
                if output_checkpoint is None:
                    failure = ReplaySourceReadError(
                        reason_class="missing_durable_checkpoint",
                        sequence=pending.to_sequence,
                        report=pending,
                        checkpoint=None,
                    )
                    raise failure from None
                output_checkpoint = self._validated_store_checkpoint(
                    lambda: self._validate_loaded_output_checkpoint(
                        pending,
                        output_checkpoint,
                        expected_parent_id=input_checkpoint_ref,
                    )
                )
                if checkpoint is not None and (
                    checkpoint.checkpoint_id != output_checkpoint.checkpoint_id
                ):
                    raise _ReplayIssue(
                        "checkpoint_reference_mismatch",
                        None,
                        ReplayCheckpointError,
                    )
                progress = self._prepare_progress(
                    effective_request,
                    pending,
                    registration=registration,
                    checkpoint=output_checkpoint,
                    after_sequence=after_sequence,
                )
                if pending.versions == ():
                    running_candidate = replace(
                        pending,
                        versions=self._progress_versions(registration, progress),
                    )
                    current_report = self._store_call(
                        lambda: self._store.update_replay_report(running_candidate),
                        failure_operation=(
                            RuntimeDiagnosticOperation.REPLAY_RUNNING_REPORT_UPDATE_FAILED
                        ),
                        report=pending,
                        checkpoint=output_checkpoint,
                    )
                    current_report = self._verify_updated_report(
                        running_candidate,
                        current_report,
                        report=pending,
                        checkpoint=output_checkpoint,
                    )
                    progress.durable_report = current_report
            self._read_and_apply(
                effective_request,
                progress,
                registration=registration,
            )
        except _ReplayIssue as issue:
            self._raise_durable_failure(
                effective_request,
                issue,
                fallback_report=current_report,
                progress=progress,
                registration=registration,
            )

        if progress is None:  # pragma: no cover - guarded by the control flow above
            raise AssertionError("replay progress was not initialized")
        checkpoint_result = self._persist_progress_checkpoint(
            effective_request,
            progress,
            registration=registration,
        )
        if registration is not None:
            result_checksum = checksum_for(thaw_canonical_json(progress.state))
        elif progress.verification_state is not None:
            result_checksum = checksum_for(
                {
                    "source_history_checksum": progress.history_checksum,
                    "command_history_checksum": (
                        progress.verification_state.history_checksum
                    ),
                }
            )
        else:  # pragma: no cover - VERIFY_HISTORY requires a verifier
            result_checksum = progress.history_checksum
        current_report = progress.durable_report
        finished_at = self._safe_finished_at(
            current_report.started_at,
            report=current_report,
            checkpoint=checkpoint_result,
        )
        success_candidate = replace(
            current_report,
            status=ReplayStatus.SUCCEEDED,
            to_sequence=current_report.high_watermark,
            applied_upcasters=tuple(progress.applied_upcasters),
            result_checksum=result_checksum,
            finished_at=finished_at,
        )
        completed = self._store_call(
            lambda: self._store.update_replay_report(success_candidate),
            failure_operation=(
                RuntimeDiagnosticOperation.REPLAY_SUCCESS_REPORT_UPDATE_FAILED
            ),
            report=current_report,
            checkpoint=checkpoint_result,
        )
        completed = self._verify_updated_report(
            success_candidate,
            completed,
            report=current_report,
            checkpoint=checkpoint_result,
        )
        return ReplayExecutionResult(
            report=completed,
            checkpoint=checkpoint_result,
            state=progress.state if registration is not None else None,
        )

    def _record_replay_metrics(
        self,
        mode: ReplayMode,
        *,
        result: str,
        error: Exception | None = None,
    ) -> None:
        self._telemetry.add_counter(
            "event_replay_total",
            labels={"mode": mode.value, "result": result},
        )
        if error is None:
            return
        reason = getattr(error, "reason_class", type(error).__name__)
        self._telemetry.add_counter(
            "event_replay_mismatch_total",
            labels={"reason": _replay_reason_metric_bucket(str(reason))},
        )
        if isinstance(error, (ReplayHistoryIntegrityError, ReplayHistorySchemaError)):
            self._telemetry.add_counter(
                "event_quarantine_total",
                labels={
                    "reason": (
                        "integrity"
                        if isinstance(error, ReplayHistoryIntegrityError)
                        else "schema"
                    )
                },
            )

    def _resolve_pending_checkpoint(
        self,
        report: ReplayReport,
        *,
        input_checkpoint_ref: str | None,
        supplied_checkpoint: ReplayCheckpoint | None,
    ) -> ReplayCheckpoint | None:
        output_checkpoint = self._store_call(
            lambda: self._checkpoints.get_checkpoint(
                report.checkpoint_ref or "",
                tenant_id=report.tenant_id,
            ),
            failure_operation=(
                RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_FAILED
            ),
            report=report,
        )
        if output_checkpoint is not None:
            return self._validated_store_checkpoint(
                lambda: self._validate_loaded_output_checkpoint(
                    report,
                    output_checkpoint,
                    expected_parent_id=input_checkpoint_ref,
                )
            )
        if supplied_checkpoint is not None:
            if supplied_checkpoint.checkpoint_id != input_checkpoint_ref:
                raise _ReplayIssue(
                    "checkpoint_reference_mismatch",
                    None,
                    ReplayCheckpointError,
                )
            return supplied_checkpoint
        if input_checkpoint_ref is None:
            return None
        parent_checkpoint = self._store_call(
            lambda: self._checkpoints.get_checkpoint(
                input_checkpoint_ref,
                tenant_id=report.tenant_id,
            ),
            failure_operation=(
                RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_FAILED
            ),
            report=report,
        )
        if parent_checkpoint is None:
            raise _ReplayIssue(
                "missing_durable_checkpoint",
                None,
                ReplayCheckpointError,
            )
        return self._validated_store_checkpoint(
            lambda: self._validate_loaded_parent_checkpoint(
                report,
                parent_checkpoint,
                expected_checkpoint_id=input_checkpoint_ref,
            )
        )

    @staticmethod
    def _validate_loaded_output_checkpoint(
        report: ReplayReport,
        checkpoint: ReplayCheckpoint,
        *,
        expected_parent_id: str | None,
    ) -> ReplayCheckpoint:
        if (
            not isinstance(checkpoint, ReplayCheckpoint)
            or checkpoint.checkpoint_id != report.checkpoint_ref
            or checkpoint.parent_checkpoint_id != expected_parent_id
        ):
            failure = ReplaySourceReadError(
                reason_class="replay_checkpoint_identity_mismatch",
                sequence=report.to_sequence,
                report=report,
                checkpoint=None,
            )
            raise failure from None
        return checkpoint

    @staticmethod
    def _validate_loaded_parent_checkpoint(
        report: ReplayReport,
        checkpoint: ReplayCheckpoint,
        *,
        expected_checkpoint_id: str,
    ) -> ReplayCheckpoint:
        if (
            not isinstance(checkpoint, ReplayCheckpoint)
            or checkpoint.checkpoint_id != expected_checkpoint_id
        ):
            failure = ReplaySourceReadError(
                reason_class="replay_checkpoint_identity_mismatch",
                sequence=report.to_sequence,
                report=report,
                checkpoint=None,
            )
            raise failure from None
        return checkpoint

    def _validated_store_checkpoint(
        self,
        validation: Callable[[], ReplayCheckpoint],
    ) -> ReplayCheckpoint:
        try:
            checkpoint = validation()
        except Exception as error:
            self._record_store_failure(
                RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_INVALID,
                error,
            )
            raise
        try:
            checkpoint.verify_integrity()
        except EventIntegrityError as error:
            # Preserve the existing typed checkpoint-validation path in
            # _prepare_progress while still surfacing the invalid store response.
            self._record_store_failure(
                RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_INVALID,
                error,
            )
        return checkpoint

    def _prepare_progress(
        self,
        request: ReplayStartRequest,
        report: ReplayReport,
        *,
        registration: ReplayReducerRegistration | None,
        checkpoint: ReplayCheckpoint | None,
        after_sequence: int | None,
    ) -> _ReplayProgress:
        normalized_after = _optional_nonnegative_int(after_sequence, "after_sequence")
        if checkpoint is None:
            if normalized_after not in (None, 0):
                raise _ReplayIssue("checkpoint_required", normalized_after, ReplayCheckpointError)
            if request.from_sequence not in (None, 1):
                raise _ReplayIssue(
                    "checkpoint_required",
                    request.from_sequence,
                    ReplayCheckpointError,
                )
            progress = _ReplayProgress(
                state=registration.initial_state if registration is not None else None,
                history_checksum=_initial_history_checksum(
                    request.source_stream_id,
                    request.tenant_id,
                ),
                last_sequence=0,
                last_event_id=None,
                verification_state=(
                    self._history_verifier.start(first_sequence=1).state
                    if registration is None and self._history_verifier is not None
                    else None
                ),
                applied_upcasters=[],
                durable_report=report,
            )
        else:
            checkpoint_is_corrupt = False
            try:
                checkpoint.verify_integrity()
            except EventIntegrityError:
                checkpoint_is_corrupt = True
            if checkpoint_is_corrupt:
                raise _ReplayIssue(
                    "corrupt_checkpoint",
                    None,
                    ReplayCheckpointError,
                ) from None
            self._validate_checkpoint(
                request,
                report,
                checkpoint,
                registration=registration,
            )
            if normalized_after is not None and normalized_after != checkpoint.last_sequence:
                raise _ReplayIssue(
                    "after_sequence_mismatch",
                    normalized_after,
                    ReplayCheckpointError,
                )
            progress = _ReplayProgress(
                state=checkpoint.state,
                history_checksum=checkpoint.history_checksum,
                last_sequence=checkpoint.last_sequence,
                last_event_id=checkpoint.last_event_id,
                verification_state=(
                    None
                    if checkpoint.verification_state is None
                    else HistoryVerificationState.from_checkpoint(
                        checkpoint.verification_state
                    )
                ),
                applied_upcasters=list(checkpoint.applied_upcasters),
                durable_report=report,
                parent_checkpoint_id=(
                    checkpoint.parent_checkpoint_id
                    if checkpoint.checkpoint_id == report.checkpoint_ref
                    else checkpoint.checkpoint_id
                ),
                durable_checkpoint=checkpoint,
            )

        if progress.last_sequence > report.high_watermark:
            raise _ReplayIssue(
                "checkpoint_beyond_high_watermark",
                None,
                ReplayCheckpointError,
            )
        same_replay_checkpoint = (
            checkpoint is not None and checkpoint.checkpoint_id == report.checkpoint_ref
        )
        expected_from = progress.last_sequence + 1
        if (
            request.from_sequence is not None
            and not same_replay_checkpoint
            and request.from_sequence != expected_from
        ):
            raise _ReplayIssue(
                "from_sequence_mismatch",
                request.from_sequence,
                ReplayCheckpointError,
            )
        if report.to_sequence is not None and report.to_sequence > progress.last_sequence:
            raise _ReplayIssue(
                "running_report_checkpoint_mismatch",
                report.to_sequence,
                ReplayCheckpointError,
            )
        if not tuple(progress.applied_upcasters[: len(report.applied_upcasters)]) == (
            report.applied_upcasters
        ):
            raise _ReplayIssue(
                "running_report_checkpoint_mismatch",
                report.to_sequence,
                ReplayCheckpointError,
            )
        return progress

    def _validate_checkpoint(
        self,
        request: ReplayStartRequest,
        report: ReplayReport,
        checkpoint: ReplayCheckpoint,
        *,
        registration: ReplayReducerRegistration | None,
    ) -> None:
        same_replay_checkpoint = checkpoint.checkpoint_id == report.checkpoint_ref
        watermark_is_compatible = (
            checkpoint.source_high_watermark == report.high_watermark
            if same_replay_checkpoint
            else checkpoint.source_high_watermark <= report.high_watermark
        )
        checks = (
            (checkpoint.mode is request.mode, "checkpoint_mode_mismatch"),
            (
                checkpoint.source_stream_id == request.source_stream_id,
                "checkpoint_stream_mismatch",
            ),
            (checkpoint.tenant_id == request.tenant_id, "checkpoint_tenant_mismatch"),
            (
                checkpoint.runtime_version == self._runtime_version,
                "checkpoint_runtime_version_mismatch",
            ),
            (
                checkpoint.schema_catalog_version == self._schema_catalog_version,
                "checkpoint_schema_version_mismatch",
            ),
            (
                _versions_extend(
                    self._versions(registration),
                    checkpoint.versions,
                ),
                "checkpoint_handler_version_mismatch",
            ),
            (
                watermark_is_compatible,
                "checkpoint_high_watermark_mismatch",
            ),
        )
        for passed, reason in checks:
            if not passed:
                raise _ReplayIssue(reason, None, ReplayCheckpointError)
        if checkpoint.last_sequence > 0 and checkpoint.last_event_id is None:
            raise _ReplayIssue(
                "checkpoint_event_identity_missing",
                checkpoint.last_sequence,
                ReplayCheckpointError,
            )
        if registration is None:
            if checkpoint.reducer_id is not None or checkpoint.reducer_version is not None:
                raise _ReplayIssue(
                    "checkpoint_reducer_mode_mismatch",
                    None,
                    ReplayCheckpointError,
                )
            if self._history_verifier is not None:
                if checkpoint.verification_state is None:
                    raise _ReplayIssue(
                        "checkpoint_verification_state_missing",
                        checkpoint.last_sequence or None,
                        ReplayCheckpointError,
                    )
                try:
                    verification_state = HistoryVerificationState.from_checkpoint(
                        checkpoint.verification_state
                    )
                    _validate_verification_pinned_versions(
                        checkpoint.versions,
                        verification_state,
                    )
                except (HistoryVerificationError, TypeError, ValueError):
                    raise _ReplayIssue(
                        "checkpoint_verification_version_mismatch",
                        checkpoint.last_sequence or None,
                        ReplayCheckpointError,
                    ) from None
                if (
                    verification_state.next_sequence
                    != checkpoint.last_sequence + 1
                ):
                    raise _ReplayIssue(
                        "checkpoint_verification_sequence_mismatch",
                        checkpoint.last_sequence or None,
                        ReplayCheckpointError,
                    )
        elif (
            checkpoint.reducer_id != registration.reducer_id
            or checkpoint.reducer_version != registration.version
        ):
            raise _ReplayIssue(
                "checkpoint_reducer_version_mismatch",
                None,
                ReplayCheckpointError,
            )

    def _read_and_apply(
        self,
        request: ReplayStartRequest,
        progress: _ReplayProgress,
        *,
        registration: ReplayReducerRegistration | None,
    ) -> None:
        report = progress.durable_report
        if progress.last_sequence == report.high_watermark:
            return
        cursor = (
            None
            if progress.last_sequence == 0
            else StreamSequenceCursor(
                stream_id=request.source_stream_id,
                after_sequence=progress.last_sequence,
                high_watermark=report.high_watermark,
                tenant_id=request.tenant_id,
            )
        )
        expected_sequence = progress.last_sequence + 1

        while progress.last_sequence < report.high_watermark:
            read_request = StreamReadRequest(
                stream_id=request.source_stream_id,
                cursor=cursor,
                limit=self._page_size,
                through_sequence=report.high_watermark,
                tenant_id=request.tenant_id,
            )
            page = self._store_call(
                lambda: self._store.read_stream(read_request),
                failure_operation=RuntimeDiagnosticOperation.SOURCE_READ_FAILED,
                sequence=expected_sequence,
                report=progress.durable_report,
                checkpoint=progress.durable_checkpoint,
            )
            try:
                self._validate_page(page, read_request, expected_sequence)
            except Exception as error:
                self._record_store_failure(
                    RuntimeDiagnosticOperation.SOURCE_READ_INVALID,
                    error,
                )
                raise

            for event in page.events:
                if (
                    event.stream_id != request.source_stream_id
                    or event.tenant_id != request.tenant_id
                ):
                    issue = _ReplayIssue(
                        "source_scope_mismatch",
                        event.stream_sequence,
                        ReplayHistoryOrderError,
                    )
                    self._record_store_failure(
                        RuntimeDiagnosticOperation.SOURCE_READ_INVALID,
                        issue,
                    )
                    raise issue
                if event.stream_sequence != expected_sequence:
                    reason = (
                        "unsorted_history"
                        if event.stream_sequence < expected_sequence
                        else "history_gap"
                    )
                    issue = _ReplayIssue(
                        reason,
                        event.stream_sequence,
                        ReplayHistoryOrderError,
                    )
                    self._record_store_failure(
                        RuntimeDiagnosticOperation.SOURCE_READ_INVALID,
                        issue,
                    )
                    raise issue
                replay_event = self._resolve_event(
                    event,
                    allow_reference_only=(registration is None),
                )
                next_history_checksum = checksum_for(
                    {
                        "previous": progress.history_checksum,
                        "event": replay_event.checksum_projection(),
                    }
                )
                next_state = progress.state
                if registration is not None:
                    reducer_failed = False
                    try:
                        next_state = _apply_reducer(
                            registration.reducer,
                            progress.state,
                            replay_event,
                        )
                    except Exception:
                        reducer_failed = True
                    if reducer_failed:
                        raise _ReplayIssue(
                            "reducer_failed",
                            event.stream_sequence,
                            ReplayReducerExecutionError,
                        ) from None
                elif self._history_verifier is not None:
                    if progress.verification_state is None:
                        raise _ReplayIssue(
                            "verification_state_missing",
                            event.stream_sequence,
                            ReplayHistoryVerificationError,
                        )
                    try:
                        verification = self._history_verifier.verify_event(
                            progress.verification_state,
                            replay_event,
                        )
                    except HistoryVerificationError as error:
                        raise _ReplayIssue(
                            error.reason_class,
                            error.sequence,
                            ReplayHistoryVerificationError,
                        ) from None
                    progress.verification_state = verification.state
                progress.state = next_state
                progress.history_checksum = next_history_checksum
                progress.last_sequence = event.stream_sequence
                progress.last_event_id = event.event_id
                progress.applied_upcasters.extend(
                    f"{event.stream_sequence}:{value}"
                    for value in replay_event.applied_upcasters
                )
                expected_sequence += 1

            checkpoint = self._persist_progress_checkpoint(
                request,
                progress,
                registration=registration,
            )
            current_report = progress.durable_report
            progress_candidate = replace(
                current_report,
                status=ReplayStatus.RUNNING,
                to_sequence=progress.last_sequence,
                applied_upcasters=tuple(progress.applied_upcasters),
                versions=self._progress_versions(registration, progress),
            )
            progress.durable_report = self._store_call(
                lambda: self._store.update_replay_report(progress_candidate),
                failure_operation=(
                    RuntimeDiagnosticOperation.REPLAY_PROGRESS_REPORT_UPDATE_FAILED
                ),
                report=current_report,
                checkpoint=checkpoint,
                sequence=progress.last_sequence,
            )
            progress.durable_report = self._verify_updated_report(
                progress_candidate,
                progress.durable_report,
                report=current_report,
                checkpoint=checkpoint,
                sequence=progress.last_sequence,
            )
            if progress.last_sequence == report.high_watermark:
                if page.next_cursor is not None:
                    issue = _ReplayIssue(
                        "cursor_exceeds_high_watermark",
                        progress.last_sequence,
                        ReplayHistoryOrderError,
                    )
                    self._record_store_failure(
                        RuntimeDiagnosticOperation.SOURCE_READ_INVALID,
                        issue,
                    )
                    raise issue
                break
            if page.next_cursor is None:
                issue = _ReplayIssue(
                    "history_gap",
                    expected_sequence,
                    ReplayHistoryOrderError,
                )
                self._record_store_failure(
                    RuntimeDiagnosticOperation.SOURCE_READ_INVALID,
                    issue,
                )
                raise issue
            cursor = page.next_cursor

    def _validate_page(
        self,
        page: EventPage,
        request: StreamReadRequest,
        expected_sequence: int,
    ) -> None:
        if page.stream_id != request.stream_id or page.tenant_id != request.tenant_id:
            raise _ReplayIssue(
                "source_scope_mismatch",
                expected_sequence,
                ReplayHistoryOrderError,
            )
        if page.high_watermark != request.through_sequence:
            raise _ReplayIssue(
                "source_watermark_mismatch",
                expected_sequence,
                ReplayHistoryOrderError,
            )
        if not page.events:
            raise _ReplayIssue(
                "history_gap",
                expected_sequence,
                ReplayHistoryOrderError,
            )
        event_above_watermark = next(
            (
                event
                for event in page.events
                if event.stream_sequence > request.through_sequence
            ),
            None,
        )
        if event_above_watermark is not None:
            raise _ReplayIssue(
                "event_exceeds_high_watermark",
                event_above_watermark.stream_sequence,
                ReplayHistoryOrderError,
            )
        if page.next_cursor is not None:
            if (
                page.next_cursor.stream_id != request.stream_id
                or page.next_cursor.tenant_id != request.tenant_id
                or page.next_cursor.high_watermark != request.through_sequence
                or page.next_cursor.after_sequence != page.events[-1].stream_sequence
            ):
                raise _ReplayIssue(
                    "invalid_source_cursor",
                    page.events[-1].stream_sequence,
                    ReplayHistoryOrderError,
                )

    def _resolve_event(
        self,
        event: StoredEvent,
        *,
        allow_reference_only: bool = False,
    ) -> ReplayEvent:
        integrity_failed = False
        try:
            event.verify_integrity()
        except EventIntegrityError:
            integrity_failed = True
        if integrity_failed:
            raise _ReplayIssue(
                "corrupt_record",
                event.stream_sequence,
                ReplayHistoryIntegrityError,
                QuarantineReason.CORRUPT_RECORD,
            ) from None
        history_value = thaw_canonical_json(
            event.extensions.get(DETERMINISTIC_HISTORY_EXTENSION)
        )
        if allow_reference_only and not isinstance(history_value, Mapping):
            raise _ReplayIssue(
                "corrupt_history",
                event.stream_sequence,
                ReplayHistoryVerificationError,
            ) from None
        if event.payload is None and not allow_reference_only:
            raise _ReplayIssue(
                "payload_unavailable",
                event.stream_sequence,
                ReplayHistorySchemaError,
            )
        if event.payload is None:
            history = event.extensions.get(DETERMINISTIC_HISTORY_EXTENSION)
            try:
                registration = self._catalog.get(
                    event.event_type,
                    event.data_schema,
                )
                history_record = DeterministicHistoryRecord.from_dict(history)
            except Exception:
                raise _ReplayIssue(
                    "payload_unavailable",
                    event.stream_sequence,
                    ReplayHistorySchemaError,
                ) from None
            if (
                event.payload_ref is None
                or registration.sensitivity_policy.whole_document_reference.value
                == "deny"
                or history_record.policy.expected_activity is None
                or history_record.policy.recorded_activity_ref != event.payload_ref
            ):
                raise _ReplayIssue(
                    "payload_unavailable",
                    event.stream_sequence,
                    ReplayHistorySchemaError,
                ) from None
            return _replay_event_for_reference(event, history_record)
        schema_failure_reason: QuarantineReason | None = None
        resolved: HistoricalSchemaResolution | None = None
        try:
            resolved = self._catalog.resolve_historical(
                event.event_type,
                event.data_schema,
                event.payload,
                occurred_at=event.occurred_at,
                envelope_schema=event.envelope_schema,
                source=f"{event.stream_id}:{event.stream_sequence}",
            )
        except EventQuarantineError as error:
            schema_failure_reason = _quarantine_reason(error.reason)
        if schema_failure_reason is not None:
            raise _ReplayIssue(
                schema_failure_reason.value,
                event.stream_sequence,
                ReplayHistorySchemaError,
                schema_failure_reason,
            ) from None
        if resolved is None:  # pragma: no cover - catalog returns or raises
            raise AssertionError("schema catalog returned no historical resolution")
        return _replay_event(event, resolved)

    def _raise_durable_failure(
        self,
        request: ReplayStartRequest,
        issue: _ReplayIssue,
        *,
        fallback_report: ReplayReport,
        progress: _ReplayProgress | None,
        registration: ReplayReducerRegistration | None,
    ) -> Never:
        report = progress.durable_report if progress is not None else fallback_report
        checkpoint_result = None
        if progress is not None:
            checkpoint_result = self._persist_progress_checkpoint(
                request,
                progress,
                registration=registration,
            )
        finished_at = self._safe_finished_at(
            report.started_at,
            report=report,
            checkpoint=checkpoint_result,
        )
        quarantine_refs = list(report.quarantine_refs)
        if issue.quarantine_reason is not None:
            sequence = issue.sequence or 0
            quarantine_write_failed = False
            quarantine: QuarantineRecord | None = None
            quarantine_candidate = QuarantineRecord(
                quarantine_id=(
                    f"replay:{request.replay_id}:{sequence}:"
                    f"{issue.quarantine_reason.value}"
                ),
                source=f"{request.source_stream_id}:{sequence}",
                reason=issue.quarantine_reason,
                created_at=finished_at,
                tenant_id=request.tenant_id,
                redacted_diagnostic=issue.quarantine_reason.value,
            )
            try:
                quarantine = self._store.save_quarantine(quarantine_candidate)
            except Exception as error:
                self._record_store_failure(
                    RuntimeDiagnosticOperation.QUARANTINE_WRITE,
                    error,
                )
                quarantine_write_failed = True
            if quarantine_write_failed:
                failure = ReplaySourceReadError(
                    reason_class="quarantine_persistence_failed",
                    sequence=issue.sequence,
                    report=report,
                    checkpoint=checkpoint_result,
                )
                raise failure from None
            if quarantine is None:  # pragma: no cover - store returns or fails
                failure = AssertionError("quarantine store returned no record")
                self._record_store_failure(
                    RuntimeDiagnosticOperation.QUARANTINE_WRITE,
                    failure,
                )
                raise failure
            try:
                quarantine_id = quarantine.quarantine_id
            except Exception as error:
                self._record_store_failure(
                    RuntimeDiagnosticOperation.QUARANTINE_WRITE,
                    error,
                )
                raise
            if not isinstance(quarantine, QuarantineRecord) or quarantine != quarantine_candidate:
                failure = ReplaySourceReadError(
                    reason_class="quarantine_persistence_failed",
                    sequence=issue.sequence,
                    report=report,
                    checkpoint=checkpoint_result,
                )
                self._record_store_failure(
                    RuntimeDiagnosticOperation.QUARANTINE_WRITE,
                    failure,
                )
                raise failure from None
            quarantine_refs.append(quarantine_id)
        mismatch_sequence = issue.sequence
        if mismatch_sequence is not None and not (
            1 <= mismatch_sequence <= report.high_watermark
        ):
            mismatch_sequence = None
        applied_upcasters = (
            tuple(progress.applied_upcasters) if progress is not None else report.applied_upcasters
        )
        to_sequence = report.to_sequence
        if progress is not None:
            minimum = report.from_sequence or 1
            if progress.last_sequence >= minimum:
                to_sequence = progress.last_sequence
        failed_candidate = replace(
            report,
            status=ReplayStatus.FAILED,
            to_sequence=to_sequence,
            applied_upcasters=applied_upcasters,
            quarantine_refs=tuple(quarantine_refs),
            mismatch_sequence=mismatch_sequence,
            reason_class=issue.reason_class,
            finished_at=finished_at,
        )
        failed = self._store_call(
            lambda: self._store.update_replay_report(failed_candidate),
            failure_operation=(
                RuntimeDiagnosticOperation.REPLAY_FAILURE_REPORT_UPDATE_FAILED
            ),
            report=report,
            checkpoint=checkpoint_result,
            sequence=issue.sequence,
        )
        failed = self._verify_updated_report(
            failed_candidate,
            failed,
            report=report,
            checkpoint=checkpoint_result,
            sequence=issue.sequence,
        )
        failure = issue.error_type(
            reason_class=issue.reason_class,
            sequence=issue.sequence,
            report=failed,
            checkpoint=checkpoint_result,
        )
        raise failure from None

    def _persist_progress_checkpoint(
        self,
        request: ReplayStartRequest,
        progress: _ReplayProgress,
        *,
        registration: ReplayReducerRegistration | None,
    ) -> ReplayCheckpoint:
        checkpoint = self._checkpoint(
            request,
            progress,
            registration=registration,
        )
        persisted = self._store_call(
            lambda: self._checkpoints.save_checkpoint(checkpoint),
            failure_operation=(
                RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_WRITE_FAILED
            ),
            report=progress.durable_report,
            checkpoint=progress.durable_checkpoint,
            sequence=progress.last_sequence or None,
        )
        checkpoint_response_invalid = not isinstance(persisted, ReplayCheckpoint)
        validation_error: Exception | None = None
        if isinstance(persisted, ReplayCheckpoint):
            try:
                persisted.verify_integrity()
                if (
                    persisted != checkpoint
                    or persisted.tenant_id != checkpoint.tenant_id
                ):
                    checkpoint_response_invalid = True
            except Exception as error:
                validation_error = error
                checkpoint_response_invalid = True
        if checkpoint_response_invalid:
            failure = ReplaySourceReadError(
                reason_class="replay_checkpoint_write_invalid",
                sequence=progress.last_sequence or None,
                report=progress.durable_report,
                checkpoint=progress.durable_checkpoint,
            )
            self._record_store_failure(
                RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_WRITE_INVALID,
                validation_error or failure,
            )
            raise failure from None
        progress.durable_checkpoint = persisted
        return persisted

    def _checkpoint(
        self,
        request: ReplayStartRequest,
        progress: _ReplayProgress,
        *,
        registration: ReplayReducerRegistration | None,
    ) -> ReplayCheckpoint:
        report = progress.durable_report
        checkpoint_id = report.checkpoint_ref
        if checkpoint_id is None:  # pragma: no cover - effective requests always set it
            raise ReplayCoreError("replay report is missing its output checkpoint reference")
        return ReplayCheckpoint(
            checkpoint_id=checkpoint_id,
            mode=request.mode,
            source_stream_id=request.source_stream_id,
            last_sequence=progress.last_sequence,
            source_high_watermark=report.high_watermark,
            last_event_id=progress.last_event_id,
            runtime_version=self._runtime_version,
            schema_catalog_version=self._schema_catalog_version,
            history_checksum=progress.history_checksum,
            state=progress.state,
            reducer_id=registration.reducer_id if registration is not None else None,
            reducer_version=registration.version if registration is not None else None,
            parent_checkpoint_id=progress.parent_checkpoint_id,
            tenant_id=request.tenant_id,
            applied_upcasters=tuple(progress.applied_upcasters),
            versions=self._progress_versions(registration, progress),
            verification_state=(
                None
                if progress.verification_state is None
                else progress.verification_state.to_checkpoint()
            ),
        )

    def _store_call(
        self,
        operation: Callable[[], _T],
        *,
        failure_operation: RuntimeDiagnosticOperation,
        report: ReplayReport | None = None,
        checkpoint: ReplayCheckpoint | None = None,
        sequence: int | None = None,
    ) -> _T:
        failure: ReplaySourceReadError | None = None
        try:
            return operation()
        except Exception as error:
            self._record_store_failure(failure_operation, error)
            failure = ReplaySourceReadError(
                reason_class=failure_operation.value,
                sequence=sequence,
                report=report,
                checkpoint=checkpoint,
            )
        if failure is None:  # pragma: no cover - operation either returns or fails
            raise AssertionError("store failure wrapper lost its error")
        raise failure from None

    def _verify_updated_report(
        self,
        candidate: ReplayReport,
        persisted: object,
        *,
        report: ReplayReport,
        checkpoint: ReplayCheckpoint | None,
        sequence: int | None = None,
    ) -> ReplayReport:
        if isinstance(persisted, ReplayReport) and persisted == candidate:
            return persisted
        failure = ReplaySourceReadError(
            reason_class=RuntimeDiagnosticOperation.REPLAY_REPORT_UPDATE_INVALID.value,
            sequence=sequence,
            report=report,
            checkpoint=checkpoint,
        )
        self._record_store_failure(
            RuntimeDiagnosticOperation.REPLAY_REPORT_UPDATE_INVALID,
            failure,
        )
        raise failure from None

    def _record_store_failure(
        self,
        operation: RuntimeDiagnosticOperation,
        error: Exception,
    ) -> None:
        self._diagnostic_fallback.record(
            category=_replay_failure_category(operation),
            component=RuntimeDiagnosticComponent.REPLAY_ENGINE,
            operation=operation,
            error=error,
        )

    def _versions(
        self,
        registration: ReplayReducerRegistration | None,
    ) -> tuple[ReplayVersion, ...]:
        versions = [
            ReplayVersion(component="replay_runtime", version=self._runtime_version),
            ReplayVersion(
                component="schema_catalog",
                version=self._schema_catalog_version,
            ),
        ]
        if registration is not None:
            versions.append(
                ReplayVersion(
                    component=f"reducer:{registration.reducer_id}",
                    version=registration.version,
                )
            )
        return tuple(versions)

    def _progress_versions(
        self,
        registration: ReplayReducerRegistration | None,
        progress: _ReplayProgress,
    ) -> tuple[ReplayVersion, ...]:
        base_versions = self._versions(registration)
        versions = list(base_versions)
        if progress.verification_state is not None:
            versions.extend(progress.verification_state.pinned_versions)
        by_component: dict[str, ReplayVersion] = {}
        for value in versions:
            existing = by_component.get(value.component)
            if existing is not None and existing.version != value.version:
                raise ReplayCoreError(
                    "replay progress pins conflicting component versions"
                )
            by_component[value.component] = value
        base_components = {value.component for value in base_versions}
        return (*base_versions, *(
            by_component[key]
            for key in sorted(by_component)
            if key not in base_components
        ))

    @staticmethod
    def _verify_started_report(
        request: ReplayStartRequest,
        report: ReplayReport,
    ) -> None:
        if not isinstance(report, ReplayReport):
            raise ReplayCoreError("durable replay store returned an invalid report")
        identity_pairs = (
            (report.replay_id, request.replay_id),
            (report.mode, request.mode),
            (report.source_stream_id, request.source_stream_id),
            (report.started_at, request.requested_at),
            (report.from_sequence, request.from_sequence),
            (report.checkpoint_ref, request.checkpoint_ref),
            (report.tenant_id, request.tenant_id),
            (report.operator_id, request.operator_id),
            (report.operator_reason, request.operator_reason),
        )
        if any(actual != expected for actual, expected in identity_pairs):
            raise ReplayCoreError("durable replay report does not match its start request")
        if report.high_watermark < 1:
            raise ReplayCoreError("durable replay source high watermark must be positive")

    def _finished_at(self, started_at: datetime) -> datetime:
        finished_at = self._clock()
        if not isinstance(finished_at, datetime):
            raise TypeError("replay clock must return a datetime")
        if finished_at.tzinfo is None or finished_at.utcoffset() is None:
            raise ValueError("replay clock must return a timezone-aware datetime")
        normalized = finished_at.astimezone(UTC)
        if normalized < started_at:
            raise ValueError("replay clock cannot precede started_at")
        return normalized

    def _safe_finished_at(
        self,
        started_at: datetime,
        *,
        report: ReplayReport,
        checkpoint: ReplayCheckpoint | None,
    ) -> datetime:
        clock_failed = False
        finished_at: datetime | None = None
        try:
            finished_at = self._finished_at(started_at)
        except Exception:
            clock_failed = True
        if clock_failed:
            failure = ReplaySourceReadError(
                reason_class="replay_clock_failed",
                sequence=checkpoint.last_sequence if checkpoint is not None else None,
                report=report,
                checkpoint=checkpoint,
            )
            raise failure from None
        if finished_at is None:  # pragma: no cover - guarded by clock_failed
            raise AssertionError("replay clock returned no timestamp")
        return finished_at


def _replay_event(event: StoredEvent, resolved: HistoricalSchemaResolution) -> ReplayEvent:
    history = thaw_canonical_json(
        event.extensions.get(DETERMINISTIC_HISTORY_EXTENSION)
    )
    if history is not None and not isinstance(history, Mapping):
        history = None
    return ReplayEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        source_data_schema=resolved.source_data_schema,
        data_schema=resolved.data_schema,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        occurred_at=resolved.occurred_at.isoformat().replace("+00:00", "Z"),
        payload=resolved.payload,
        record_checksum=event.record_checksum,
        history=history,
        applied_upcasters=resolved.applied_upcasters,
    )


def _replay_event_for_reference(
    event: StoredEvent,
    history: DeterministicHistoryRecord,
) -> ReplayEvent:
    return ReplayEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        source_data_schema=event.data_schema,
        data_schema=event.data_schema,
        stream_id=event.stream_id,
        stream_sequence=event.stream_sequence,
        occurred_at=event.occurred_at.isoformat().replace("+00:00", "Z"),
        payload={},
        record_checksum=event.record_checksum,
        history=history.to_dict(),
    )


def _apply_reducer(
    reducer: ReplayReducer,
    state: CanonicalValue,
    event: ReplayEvent,
) -> CanonicalValue:
    reducer_state = _normalize_replay_json(
        thaw_canonical_json(state),
        path="$.replay.reducer.state",
    )
    return _normalize_replay_json(
        reducer(reducer_state, event),
        path="$.replay.reducer.result",
    )


def _audit_reducer(reducer: ReplayReducer) -> None:
    if not isinstance(reducer, FunctionType):
        raise ReplayReducerRegistrationError("replay reducer must be a plain function")
    if inspect.iscoroutinefunction(reducer) or inspect.isgeneratorfunction(reducer):
        raise ReplayReducerRegistrationError("replay reducer must be synchronous")
    parameters = tuple(inspect.signature(reducer).parameters.values())
    if len(parameters) != 2 or any(
        parameter.kind
        not in {parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD}
        for parameter in parameters
    ):
        raise ReplayReducerRegistrationError(
            "replay reducer must accept exactly (state, event)"
        )
    _audit_function_dependencies(reducer, seen=set())


def _audit_function_dependencies(function: FunctionType, *, seen: set[int]) -> None:
    identity = id(function)
    if identity in seen:
        return
    seen.add(identity)
    closure = inspect.getclosurevars(function)
    global_reads = {
        instruction.argval
        for instruction in dis.get_instructions(function)
        if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}
    }
    for name in closure.builtins:
        if name not in _ALLOWED_REDUCER_BUILTINS:
            raise ReplayReducerRegistrationError(
                f"replay reducer uses forbidden builtin: {name}"
            )
    # getclosurevars also reports import/attribute names that match globals.
    dependencies = {
        name: value for name, value in closure.globals.items() if name in global_reads
    }
    for name, value in {**dependencies, **closure.nonlocals}.items():
        if isinstance(value, ModuleType) or not _is_immutable_constant(value):
            raise ReplayReducerRegistrationError(
                f"replay reducer captures forbidden dependency: {name}"
            )
    for value in function.__defaults__ or ():
        if not _is_immutable_constant(value):
            raise ReplayReducerRegistrationError("replay reducer has a mutable default")
    for value in (function.__kwdefaults__ or {}).values():
        if not _is_immutable_constant(value):
            raise ReplayReducerRegistrationError("replay reducer has a mutable default")
    _audit_reducer_code(
        function.__code__,
        function=function,
        function_seen=seen,
        code_seen=set(),
    )


def _audit_reducer_code(
    code: CodeType,
    *,
    function: FunctionType,
    function_seen: set[int],
    code_seen: set[int],
) -> None:
    identity = id(code)
    if identity in code_seen:
        return
    code_seen.add(identity)
    for instruction in dis.get_instructions(code):
        operation = instruction.opname
        name = str(instruction.argval) if instruction.argval is not None else ""
        if operation in _FORBIDDEN_REDUCER_OPCODES:
            raise ReplayReducerRegistrationError(
                f"replay reducer uses forbidden operation: {operation}"
            )
        if operation == "LOAD_ATTR" and name not in _ALLOWED_REPLAY_EVENT_ATTRIBUTES:
            raise ReplayReducerRegistrationError(
                f"replay reducer uses forbidden attribute: {name}"
            )
        if operation == "LOAD_METHOD" and name not in _ALLOWED_CANONICAL_METHODS:
            raise ReplayReducerRegistrationError(
                f"replay reducer uses forbidden method: {name}"
            )
        if operation == "LOAD_GLOBAL":
            if name in _ALLOWED_REDUCER_BUILTINS:
                continue
            raise ReplayReducerRegistrationError(
                f"replay reducer uses forbidden global: {name}"
            )
        if operation == "LOAD_CONST":
            _audit_reducer_constant(
                instruction.argval,
                function=function,
                function_seen=function_seen,
                code_seen=code_seen,
            )


def _audit_reducer_constant(
    value: Any,
    *,
    function: FunctionType,
    function_seen: set[int],
    code_seen: set[int],
) -> None:
    if isinstance(value, CodeType):
        _audit_reducer_code(
            value,
            function=function,
            function_seen=function_seen,
            code_seen=code_seen,
        )
        return
    if isinstance(value, str):
        if (
            value in _FORBIDDEN_REFLECTION_CONSTANTS
            or (value.startswith("__") and value.endswith("__"))
        ):
            raise ReplayReducerRegistrationError(
                f"replay reducer contains forbidden reflection constant: {value}"
            )
        return
    if value is None or isinstance(value, (bool, int, float, bytes)):
        return
    if isinstance(value, tuple):
        for item in value:
            _audit_reducer_constant(
                item,
                function=function,
                function_seen=function_seen,
                code_seen=code_seen,
            )
        return
    raise ReplayReducerRegistrationError(
        f"replay reducer contains unsupported constant: {type(value).__name__}"
    )


def _is_immutable_constant(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return True
    if isinstance(value, tuple):
        return all(_is_immutable_constant(item) for item in value)
    return False


def _normalize_replay_json(value: Any, *, path: str) -> CanonicalValue:
    """Freeze JSON after recursively sorting mappings for process-stable iteration."""

    return normalize_canonical_json(_sort_replay_json(value), path=path)


def _sort_replay_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            return value
        return {
            key: _sort_replay_json(value[key])
            for key in sorted(value)
        }
    if isinstance(value, (tuple, list)):
        return [_sort_replay_json(item) for item in value]
    return value


def _initial_history_checksum(stream_id: str, tenant_id: str | None) -> str:
    return checksum_for(
        {
            "replay_history": "v1",
            "source_stream_id": stream_id,
            "tenant_id": tenant_id,
        }
    )


def _replay_result_checksum(
    checkpoint: ReplayCheckpoint,
    *,
    registration: ReplayReducerRegistration | None,
) -> str:
    if registration is not None:
        return checksum_for(thaw_canonical_json(checkpoint.state))
    if checkpoint.verification_state is not None:
        verification = HistoryVerificationState.from_checkpoint(
            checkpoint.verification_state
        )
        return checksum_for(
            {
                "source_history_checksum": checkpoint.history_checksum,
                "command_history_checksum": verification.history_checksum,
            }
        )
    return checkpoint.history_checksum


def _with_output_checkpoint_ref(request: ReplayStartRequest) -> ReplayStartRequest:
    if request.checkpoint_ref is None:
        suffix = "root"
    else:
        parent_digest = checksum_for({"parent_checkpoint_ref": request.checkpoint_ref})
        suffix = f"parent:{parent_digest.removeprefix('sha256:')}"
    return replace(request, checkpoint_ref=f"{request.replay_id}:checkpoint:{suffix}")


def _quarantine_reason(value: str) -> QuarantineReason:
    try:
        return QuarantineReason(value)
    except ValueError as exc:
        raise ReplayCoreError("schema catalog returned an unknown quarantine reason") from exc


def _replay_failure_category(
    operation: RuntimeDiagnosticOperation,
) -> RuntimeDiagnosticCategory:
    if operation in {
        RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_FAILED,
        RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_WRITE_FAILED,
        RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_READ_INVALID,
        RuntimeDiagnosticOperation.REPLAY_CHECKPOINT_WRITE_INVALID,
    }:
        return RuntimeDiagnosticCategory.REPLAY_CHECKPOINT_FAILURE
    if operation in {
        RuntimeDiagnosticOperation.REPLAY_BEGIN_FAILED,
        RuntimeDiagnosticOperation.REPLAY_RUNNING_REPORT_UPDATE_FAILED,
        RuntimeDiagnosticOperation.REPLAY_SUCCESS_REPORT_UPDATE_FAILED,
        RuntimeDiagnosticOperation.REPLAY_PROGRESS_REPORT_UPDATE_FAILED,
        RuntimeDiagnosticOperation.REPLAY_FAILURE_REPORT_UPDATE_FAILED,
        RuntimeDiagnosticOperation.REPLAY_BEGIN_REPORT_INVALID,
        RuntimeDiagnosticOperation.REPLAY_REPORT_UPDATE_INVALID,
    }:
        return RuntimeDiagnosticCategory.REPLAY_REPORT_FAILURE
    if operation is RuntimeDiagnosticOperation.QUARANTINE_WRITE:
        return RuntimeDiagnosticCategory.REPLAY_QUARANTINE_FAILURE
    return RuntimeDiagnosticCategory.REPLAY_STORE_FAILURE


def _replay_reason_metric_bucket(reason: str) -> str:
    normalized = reason.casefold()
    if "activity" in normalized:
        return "activity"
    if "command" in normalized or "nondetermin" in normalized:
        return "command"
    if "schema" in normalized or "upcast" in normalized:
        return "schema"
    if "version" in normalized:
        return "version"
    if any(value in normalized for value in ("checksum", "corrupt", "integrity")):
        return "integrity"
    return "unknown"


def _require_mode(request: ReplayStartRequest, expected: ReplayMode) -> None:
    if not isinstance(request, ReplayStartRequest):
        raise TypeError("request must be ReplayStartRequest")
    if request.mode is not expected:
        raise ReplayModeError(
            f"{expected.value} entrypoint cannot execute {request.mode.value}"
        )


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


def _optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(value, field_name)


def _required_checksum(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name).lower()
    if _CHECKSUM_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")
    return normalized


def _versions_extend(
    required: tuple[ReplayVersion, ...],
    actual: tuple[ReplayVersion, ...],
) -> bool:
    required_by_component = {value.component: value.version for value in required}
    actual_by_component = {value.component: value.version for value in actual}
    return len(actual_by_component) == len(actual) and all(
        actual_by_component.get(component) == version
        for component, version in required_by_component.items()
    )


def _validate_verification_pinned_versions(
    versions: tuple[ReplayVersion, ...],
    verification_state: HistoryVerificationState,
) -> None:
    checkpoint_versions = {
        value.component: value.version
        for value in versions
    }
    for pinned in verification_state.pinned_versions:
        if checkpoint_versions.get(pinned.component) != pinned.version:
            raise ValueError(
                "replay checkpoint versions conflict with verification pinned versions"
            )


__all__ = [
    "DeterministicReplayEngine",
    "ReplayCheckpoint",
    "ReplayCheckpointError",
    "ReplayCheckpointStorePort",
    "ReplayCoreError",
    "ReplayEvent",
    "ReplayExecutionFailure",
    "ReplayExecutionResult",
    "ReplayHistoryIntegrityError",
    "ReplayHistoryOrderError",
    "ReplayHistorySchemaError",
    "ReplayHistoryVerificationError",
    "ReplayModeError",
    "ReplayRedeliveryDelegationRequired",
    "ReplayReducerExecutionError",
    "ReplayReducerRegistration",
    "ReplayReducerRegistrationError",
    "ReplayReducerRegistry",
    "ReplaySourceReadError",
]
