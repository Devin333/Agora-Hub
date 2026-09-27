"""Harness-owned child-agent lifecycle supervision.

The existing :mod:`framework.harness.subagents.runtime` validates worker
inputs and persists transcript evidence.  This module adds the outer runtime
resource boundary: admission, leases, cancellation, recovery and idempotent
operations.  It intentionally never interprets a child result as a routing or
quality decision.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import threading
import hashlib
import inspect
import re
from typing import Any, ClassVar, Protocol, runtime_checkable
from uuid import uuid4

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.budget_allocation import USAGE_FIELDS
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.json import stable_json_dumps


class ChildAgentSupervisorError(RuntimeError):
    """Base error for typed supervisor failures."""

    def __init__(self, message: str, *, code: str = "child_agent_supervisor_error") -> None:
        super().__init__(message)
        self.code = code


class ChildAgentAdmissionError(ChildAgentSupervisorError):
    pass


class ChildAgentOperationConflict(ChildAgentSupervisorError):
    pass


class ChildAgentNotFoundError(ChildAgentSupervisorError):
    pass


class ChildAgentState(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    LOST = "LOST"
    CLOSED = "CLOSED"


TERMINAL_CHILD_STATES = frozenset(
    {
        ChildAgentState.SUCCEEDED,
        ChildAgentState.FAILED,
        ChildAgentState.CANCELLED,
        ChildAgentState.LOST,
        ChildAgentState.CLOSED,
    }
)
_FORBIDDEN_OUTPUT_KEYS = frozenset(
    {
        "routing",
        "next_step",
        "quality",
        "quality_passed",
        "publication",
        "publish_artifact",
        "memory_write",
        "write_memory",
        "promote_skill",
        "skill_promotion",
        "sibling_control",
        "cancel_sibling",
        "route_graph",
    }
)
_POSITIVE_BUDGET_KEYS = frozenset(
    {
        "turns",
        "tokens",
        "tool_calls",
        "memory_ops",
        "cpu_seconds",
        "remaining_turns",
        "remaining_tokens",
        "remaining_tool_calls",
        "remaining_memory_ops",
        "remaining_cpu_seconds",
        "max_turns",
        "max_tokens",
        "max_tool_calls",
        "max_memory_ops",
        "max_cpu_seconds",
    }
)
_BUDGET_DIMENSIONS = ("turns", "tokens", "tool_calls", "memory_ops", "cpu_seconds", "output_tokens", "time_ms", "cost_microusd")
_WORKER_NOT_SUPPLIED = object()
CHILD_AGENT_HANDLE_SCHEMA_VERSION = "newsroom.child-agent-handle/v1"


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    normalized = value.strip()
    if len(normalized) > 512:
        raise ValueError(f"{field_name} is too long")
    return normalized


def _utc(value: datetime, field_name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _identity(value: GraphExecutionIdentity | Mapping[str, Any], field_name: str) -> GraphExecutionIdentity:
    if isinstance(value, GraphExecutionIdentity):
        return value
    try:
        return GraphExecutionIdentity.from_dict(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a GraphExecutionIdentity") from exc


def _budget(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("budget must be an object")
    normalized = dict(value)
    if "schema_version" in normalized and normalized["schema_version"] != "agora.harness-budget-reservation/v2":
        raise ValueError("unsupported versioned child budget reservation")
    if normalized.get("schema_version") == "agora.harness-budget-reservation/v2":
        from framework.harness.control_plane.budget_reservation import BudgetReservation

        try:
            BudgetReservation.from_dict(normalized)
        except HarnessValidationError as exc:
            raise ValueError("invalid versioned child budget reservation") from exc
    stable_json_dumps(normalized)
    for key, raw in normalized.items():
        if key not in _POSITIVE_BUDGET_KEYS:
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw <= 0:
            raise ValueError(f"budget field {key} must have remaining capacity")
    # A child cannot reserve more of a dimension than the parent has left.
    # Callers may express the same contract as ``remaining_*`` or ``max_*``;
    # both forms are validated before the request reaches a worker.
    for dimension in _BUDGET_DIMENSIONS:
        requested = normalized.get(dimension)
        for available_key in (f"remaining_{dimension}", f"max_{dimension}"):
            available = normalized.get(available_key)
            if requested is not None and available is not None and requested > available:
                raise ValueError(
                    f"budget field {dimension} exceeds {available_key}"
                )
    return normalized


def _budget_amounts(value: Mapping[str, Any]) -> dict[str, int | float]:
    """Normalize the reservation charged to one child request."""
    if value.get("schema_version") == "agora.harness-budget-reservation/v2":
        return {USAGE_FIELDS[name]: amount for name, amount in value["attempt_allocation"].items()}
    amounts: dict[str, int | float] = {}
    for dimension in _BUDGET_DIMENSIONS:
        raw = value.get(dimension)
        if raw is None:
            raw = value.get(f"max_{dimension}")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            amounts[dimension] = raw
    return amounts


def _budget_limits(value: Mapping[str, Any]) -> dict[str, int | float]:
    """Read parent limits carried alongside a child admission request."""
    if value.get("schema_version") == "agora.harness-budget-reservation/v2":
        return {USAGE_FIELDS[name]: amount for name, amount in value["parent_allocation"].items()}
    limits: dict[str, int | float] = {}
    for dimension in _BUDGET_DIMENSIONS:
        raw = value.get(f"remaining_{dimension}")
        if raw is None:
            raw = value.get(f"max_{dimension}")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            limits[dimension] = raw
    return limits


def _budget_key(identity: GraphExecutionIdentity) -> str:
    return "budget:" + hashlib.sha256(
        stable_json_dumps(identity.to_dict()).encode("utf-8")
    ).hexdigest()


def _checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(stable_json_dumps(value).encode("utf-8")).hexdigest()


def _event_id_from_fields(fields: Mapping[str, Any]) -> str:
    return "child-event-" + hashlib.sha256(
        stable_json_dumps(dict(fields)).encode("utf-8")
    ).hexdigest()


def _derived_child_identity(parent: GraphExecutionIdentity, child_id: str) -> GraphExecutionIdentity:
    """Create a deterministic child activity identity when a caller omits one."""
    suffix = child_id.replace("/", "-")
    return GraphExecutionIdentity(
        run_id=parent.run_id,
        graph_id=parent.graph_id,
        graph_version=parent.graph_version,
        graph_ref=parent.graph_ref,
        graph_checksum=parent.graph_checksum,
        node_id=parent.node_id,
        node_instance_id=f"{parent.node_instance_id}:child:{suffix}",
        activity_id=f"{parent.activity_id}:child:{suffix}",
        attempt=parent.attempt,
    )


@dataclass(frozen=True, slots=True)
class ChildAgentLease:
    lease_id: str
    issued_at: datetime
    expires_at: datetime
    heartbeat_seq: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "lease_id", _required_text(self.lease_id, "lease_id"))
        issued = _utc(self.issued_at, "issued_at")
        expires = _utc(self.expires_at, "expires_at")
        if expires <= issued:
            raise ValueError("lease expires_at must be after issued_at")
        if isinstance(self.heartbeat_seq, bool) or not isinstance(self.heartbeat_seq, int) or self.heartbeat_seq < 0:
            raise ValueError("heartbeat_seq must be non-negative")
        object.__setattr__(self, "issued_at", issued)
        object.__setattr__(self, "expires_at", expires)

    def is_expired(self, now: datetime) -> bool:
        return _utc(now) >= self.expires_at


@dataclass(frozen=True, slots=True)
class ChildAgentHandle:
    """Immutable identity and admitted capabilities for one child runtime."""

    CONTRACT_SCHEMA_VERSION: ClassVar[str] = CHILD_AGENT_HANDLE_SCHEMA_VERSION
    SCHEMA_VERSION: ClassVar[str] = CHILD_AGENT_HANDLE_SCHEMA_VERSION

    child_id: str
    parent_graph_identity: GraphExecutionIdentity | Mapping[str, Any]
    child_graph_identity: GraphExecutionIdentity | Mapping[str, Any] | None
    stage_id: str
    task_id: str
    task_instance_id: str
    attempt: int
    allowed_tools: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    budget: Mapping[str, Any]
    transcript_ref: str | None
    operation_id: str
    state: ChildAgentState | str
    lease: ChildAgentLease
    created_at: datetime
    updated_at: datetime
    terminal_receipt_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("child_id", "stage_id", "task_id", "task_instance_id", "operation_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        parent = _identity(self.parent_graph_identity, "parent_graph_identity")
        child = None if self.child_graph_identity is None else _identity(self.child_graph_identity, "child_graph_identity")
        if child is not None:
            if child.run_id != parent.run_id:
                raise ValueError("child and parent Graph identities must share run_id")
            for field_name in ("graph_id", "graph_version", "graph_ref", "graph_checksum"):
                if getattr(child, field_name) != getattr(parent, field_name):
                    raise ValueError(
                        f"child and parent Graph identities must share {field_name}"
                    )
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt must be positive")
        tools = tuple(_required_text(item, "allowed_tool") for item in self.allowed_tools)
        namespaces = tuple(_required_text(item, "allowed_memory_namespace") for item in self.allowed_memory_namespaces)
        if not tools or not namespaces:
            raise ValueError("child capabilities must be explicitly declared")
        if len(set(tools)) != len(tools) or len(set(namespaces)) != len(namespaces):
            raise ValueError("child capabilities must be unique")
        budget = _budget(self.budget)
        if any(tool == "*" for tool in tools) or any(namespace == "*" for namespace in namespaces):
            raise ValueError("child capabilities cannot use wildcard admission")
        state = ChildAgentState(self.state)
        created = _utc(self.created_at, "created_at")
        updated = _utc(self.updated_at, "updated_at")
        if updated < created:
            raise ValueError("updated_at cannot precede created_at")
        object.__setattr__(self, "parent_graph_identity", parent)
        object.__setattr__(self, "child_graph_identity", child)
        object.__setattr__(self, "allowed_tools", tuple(sorted(tools)))
        object.__setattr__(self, "allowed_memory_namespaces", tuple(sorted(namespaces)))
        object.__setattr__(self, "budget", budget)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)
        if self.transcript_ref is not None:
            object.__setattr__(self, "transcript_ref", _required_text(self.transcript_ref, "transcript_ref"))
        if self.terminal_receipt_ref is not None:
            object.__setattr__(self, "terminal_receipt_ref", _required_text(self.terminal_receipt_ref, "terminal_receipt_ref"))

    @property
    def parent_identity(self) -> GraphExecutionIdentity:
        return self.parent_graph_identity

    @property
    def graph_identity(self) -> GraphExecutionIdentity:
        return self.child_graph_identity or self.parent_graph_identity

    @property
    def lease_expires_at(self) -> datetime:
        return self.lease.expires_at

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_CHILD_STATES

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ChildAgentHandle":
        """Restore a handle from its canonical contract payload."""
        if not isinstance(value, Mapping):
            raise ValueError("child handle contract payload must be an object")
        required = {
            "child_id",
            "parent_graph_identity",
            "child_graph_identity",
            "stage_id",
            "task_id",
            "task_instance_id",
            "attempt",
            "allowed_tools",
            "allowed_memory_namespaces",
            "budget",
            "transcript_ref",
            "operation_id",
            "state",
            "lease",
            "created_at",
            "updated_at",
            "terminal_receipt_ref",
        }
        unknown = sorted(set(value).difference(required))
        if unknown:
            raise ValueError(f"child handle contract payload has unknown fields: {unknown}")
        missing = sorted(required.difference(value))
        if missing:
            raise ValueError(f"child handle contract payload is missing fields: {missing}")
        lease = value["lease"]
        if not isinstance(lease, Mapping):
            raise ValueError("child handle contract lease must be an object")
        lease_required = {"lease_id", "issued_at", "expires_at", "heartbeat_seq"}
        lease_unknown = sorted(set(lease).difference(lease_required), key=str)
        if lease_unknown:
            raise ValueError(f"child handle contract lease has unknown fields: {lease_unknown}")
        lease_missing = sorted(lease_required.difference(lease))
        if lease_missing:
            raise ValueError(f"child handle contract lease is missing fields: {lease_missing}")
        allowed_tools = value["allowed_tools"]
        allowed_namespaces = value["allowed_memory_namespaces"]
        if (
            isinstance(allowed_tools, (str, bytes, bytearray))
            or not isinstance(allowed_tools, Sequence)
            or isinstance(allowed_namespaces, (str, bytes, bytearray))
            or not isinstance(allowed_namespaces, Sequence)
        ):
            raise ValueError("child handle contract capabilities must be arrays")
        budget = value["budget"]
        if not isinstance(budget, Mapping):
            raise ValueError("child handle contract budget must be an object")
        created_at = value["created_at"]
        updated_at = value["updated_at"]
        if not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise ValueError("child handle contract timestamps must be strings")
        issued_at = lease["issued_at"]
        expires_at = lease["expires_at"]
        if not isinstance(issued_at, str) or not isinstance(expires_at, str):
            raise ValueError("child handle contract lease timestamps must be strings")
        return cls(
            child_id=value["child_id"],
            parent_graph_identity=value["parent_graph_identity"],
            child_graph_identity=value["child_graph_identity"],
            stage_id=value["stage_id"],
            task_id=value["task_id"],
            task_instance_id=value["task_instance_id"],
            attempt=value["attempt"],
            allowed_tools=tuple(allowed_tools),
            allowed_memory_namespaces=tuple(allowed_namespaces),
            budget=budget,
            transcript_ref=value["transcript_ref"],
            operation_id=value["operation_id"],
            state=value["state"],
            lease=ChildAgentLease(
                lease_id=lease["lease_id"],
                issued_at=datetime.fromisoformat(issued_at),
                expires_at=datetime.fromisoformat(expires_at),
                heartbeat_seq=lease["heartbeat_seq"],
            ),
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
            terminal_receipt_ref=value["terminal_receipt_ref"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "parent_graph_identity": self.parent_graph_identity.to_dict(),
            "child_graph_identity": self.child_graph_identity.to_dict() if self.child_graph_identity else None,
            "stage_id": self.stage_id,
            "task_id": self.task_id,
            "task_instance_id": self.task_instance_id,
            "attempt": self.attempt,
            "allowed_tools": list(self.allowed_tools),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
            "budget": dict(self.budget),
            "transcript_ref": self.transcript_ref,
            "operation_id": self.operation_id,
            "state": self.state.value,
            "lease": {
                "lease_id": self.lease.lease_id,
                "issued_at": self.lease.issued_at.isoformat(),
                "expires_at": self.lease.expires_at.isoformat(),
                "heartbeat_seq": self.lease.heartbeat_seq,
            },
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "terminal_receipt_ref": self.terminal_receipt_ref,
        }

    def contract_payload(self) -> dict[str, Any]:
        """Return the canonical payload covered by the handle checksum."""
        return self.to_dict()

    @property
    def handle_checksum(self) -> str:
        return _checksum(self.contract_payload())

    def to_contract(self) -> dict[str, Any]:
        """Return the versioned, checksummed handle contract."""
        payload = self.contract_payload()
        return {
            "schema_version": self.CONTRACT_SCHEMA_VERSION,
            "canonical_payload": payload,
            "handle_checksum": self.handle_checksum,
        }

    def handle_contract(self) -> dict[str, Any]:
        """Backward-compatible alias for :meth:`to_contract`."""
        return self.to_contract()


@dataclass(frozen=True, slots=True)
class ChildAgentSpawnRequest:
    parent_graph_identity: GraphExecutionIdentity | Mapping[str, Any]
    stage_id: str
    task_id: str
    task_instance_id: str
    attempt: int
    allowed_tools: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    budget: Mapping[str, Any]
    operation_id: str
    child_graph_identity: GraphExecutionIdentity | Mapping[str, Any] | None = None
    transcript_ref: str | None = None
    lease_seconds: float = 30.0
    child_id: str | None = None

    def __post_init__(self) -> None:
        _identity(self.parent_graph_identity, "parent_graph_identity")
        if self.child_graph_identity is not None:
            _identity(self.child_graph_identity, "child_graph_identity")
        _budget(self.budget)
        if not self.allowed_tools or not self.allowed_memory_namespaces:
            raise ValueError("child capabilities must be explicitly declared")
        if any(not isinstance(item, str) or not item.strip() or item.strip() == "*" for item in (*self.allowed_tools, *self.allowed_memory_namespaces)):
            raise ValueError("child capabilities must be concrete, non-wildcard names")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, (int, float))
            or self.lease_seconds <= 0
            or self.lease_seconds > 3600
        ):
            raise ValueError("lease_seconds must be in (0, 3600]")
        object.__setattr__(self, "operation_id", _required_text(self.operation_id, "operation_id"))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ChildAgentSpawnRequest":
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class ChildAgentHeartbeat:
    child_id: str
    lease_id: str
    heartbeat_seq: int
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "child_id", _required_text(self.child_id, "child_id"))
        object.__setattr__(self, "lease_id", _required_text(self.lease_id, "lease_id"))
        if isinstance(self.heartbeat_seq, bool) or not isinstance(self.heartbeat_seq, int) or self.heartbeat_seq < 1:
            raise ValueError("heartbeat_seq must be positive")
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "observed_at"))


@dataclass(frozen=True, slots=True)
class ChildAgentTerminalReceipt:
    child_id: str
    operation_id: str
    parent_graph_identity: GraphExecutionIdentity | Mapping[str, Any]
    status: ChildAgentState | str
    reason_code: str
    result_ref: str | None
    result_checksum: str | None
    termination_confirmed: bool
    completed_at: datetime
    receipt_checksum: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "child_id", _required_text(self.child_id, "child_id"))
        object.__setattr__(self, "operation_id", _required_text(self.operation_id, "operation_id"))
        object.__setattr__(self, "parent_graph_identity", _identity(self.parent_graph_identity, "parent_graph_identity"))
        state = ChildAgentState(self.status)
        if state not in TERMINAL_CHILD_STATES - {ChildAgentState.CLOSED}:
            raise ValueError("terminal receipt requires a terminal child state")
        object.__setattr__(self, "status", state)
        object.__setattr__(self, "reason_code", _required_text(self.reason_code, "reason_code"))
        result_ref = None if self.result_ref is None else _required_text(self.result_ref, "result_ref")
        result_checksum = self.result_checksum
        if result_checksum is not None:
            result_checksum = str(result_checksum).strip().lower()
            if re.fullmatch(r"sha256:[0-9a-f]{64}", result_checksum) is None:
                raise ValueError("result_checksum must be a sha256 checksum")
        if result_checksum is not None and result_ref is None:
            raise ValueError("result_checksum requires result_ref")
        if state is ChildAgentState.SUCCEEDED and (result_ref is None or result_checksum is None):
            raise ValueError("succeeded terminal receipt requires result ref and checksum")
        if not isinstance(self.termination_confirmed, bool):
            raise ValueError("termination_confirmed must be boolean")
        if state in {
            ChildAgentState.SUCCEEDED,
            ChildAgentState.FAILED,
            ChildAgentState.CANCELLED,
        } and not self.termination_confirmed:
            raise ValueError(f"{state.value} terminal receipt requires confirmed termination")
        object.__setattr__(self, "result_ref", result_ref)
        object.__setattr__(self, "result_checksum", result_checksum)
        object.__setattr__(self, "completed_at", _utc(self.completed_at, "completed_at"))
        object.__setattr__(self, "receipt_checksum", _checksum(self.checksum_projection()))

    def checksum_projection(self) -> dict[str, Any]:
        return {
            "child_id": self.child_id,
            "operation_id": self.operation_id,
            "parent_graph_identity": self.parent_graph_identity.to_dict(),
            "status": self.status.value,
            "reason_code": self.reason_code,
            "result_ref": self.result_ref,
            "result_checksum": self.result_checksum,
            "termination_confirmed": self.termination_confirmed,
            "completed_at": self.completed_at.isoformat(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.checksum_projection(), "receipt_checksum": self.receipt_checksum}


@dataclass(frozen=True, slots=True)
class ChildAgentOperationResult:
    operation_id: str
    child_id: str
    handle: ChildAgentHandle
    receipt: ChildAgentTerminalReceipt | None = None
    result: Mapping[str, Any] | None = None


@runtime_checkable
class ChildAgentEventSink(Protocol):
    def record(self, event: Mapping[str, Any]) -> Any: ...


@runtime_checkable
class ChildAgentEventReader(Protocol):
    def read_events(self) -> Sequence[Mapping[str, Any]]: ...


@runtime_checkable
class ChildAgentWorker(Protocol):
    def run(self, handle: ChildAgentHandle) -> Mapping[str, Any] | Any: ...

    def cancel(self, handle: ChildAgentHandle) -> bool: ...


class InMemoryChildAgentEventLog:
    """Durable-shaped event log used by tests and local supervision."""

    def __init__(self, events: Sequence[Mapping[str, Any]] | None = None) -> None:
        self._lock = threading.RLock()
        self.events: list[dict[str, Any]] = []
        self._by_id: dict[str, dict[str, Any]] = {}
        for event in events or ():
            self.record(event)

    def record(self, event: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            item = dict(event)
            event_id = item.get("event_id")
            if isinstance(event_id, str):
                previous = self._by_id.get(event_id)
                if previous is not None:
                    if stable_json_dumps(previous) != stable_json_dumps(item):
                        raise ChildAgentOperationConflict(
                            "child lifecycle event identity has conflicting content",
                            code="event_identity_conflict",
                        )
                    return dict(previous)
                self._by_id[event_id] = item
            self.events.append(item)
            return item

    def for_child(self, child_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return tuple(item for item in self.events if item.get("child_id") == child_id)


class ChildAgentSupervisor:
    """Bounded, idempotent child-agent lifecycle controller.

    ``worker_factory`` may return a callable, an object with ``run`` or an
    object with ``invoke``.  Worker execution is deliberately treated as a
    candidate producer; all lifecycle and terminal state changes remain here.
    """

    def __init__(
        self,
        *,
        worker_factory: Callable[[ChildAgentHandle], Any] | Callable[[ChildAgentHandle, Mapping[str, Any]], Any] | None = None,
        event_sink: ChildAgentEventSink | Callable[[Mapping[str, Any]], Any] | None = None,
        event_reader: ChildAgentEventReader | None = None,
        runtime_event_sink: Any | None = None,
        result_resolver: Callable[[str], Mapping[str, Any] | None] | None = None,
        budget_admitter: Callable[[GraphExecutionIdentity, Mapping[str, Any]], bool] | None = None,
        owner_guard: Callable[[], Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        max_children: int = 32,
        default_lease_seconds: float = 30.0,
        cancel_timeout_seconds: float = 5.0,
        events: InMemoryChildAgentEventLog | None = None,
    ) -> None:
        if max_children < 1:
            raise ValueError("max_children must be positive")
        if default_lease_seconds <= 0:
            raise ValueError("default_lease_seconds must be positive")
        if cancel_timeout_seconds <= 0 or cancel_timeout_seconds > 60:
            raise ValueError("cancel_timeout_seconds must be in (0, 60]")
        self._worker_factory = worker_factory
        self._events = events or InMemoryChildAgentEventLog()
        self._event_sink = event_sink or self._events
        self._event_reader = (
            event_reader
            if event_reader is not None
            else self._event_sink
            if isinstance(self._event_sink, ChildAgentEventReader)
            else None
        )
        self._runtime_event_sink = runtime_event_sink
        self._result_resolver = result_resolver
        self._budget_admitter = budget_admitter
        self._owner_guard = owner_guard
        self._admission_closed = False
        self._recovery_blocked = False
        self._persistence_uncertain = False
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_children = max_children
        self._default_lease_seconds = float(default_lease_seconds)
        self._cancel_timeout_seconds = float(cancel_timeout_seconds)
        self._handles: dict[str, ChildAgentHandle] = {}
        self._operations: dict[str, ChildAgentOperationResult] = {}
        self._budget_reservations: dict[str, dict[str, int | float]] = {}
        self._budget_consumed: dict[str, dict[str, int | float]] = {}
        self._budget_limits_by_key: dict[str, dict[str, int | float]] = {}
        self._budget_by_operation: dict[str, tuple[str, dict[str, int | float]]] = {}
        self._workers: dict[str, Any] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._recovered_active_children: set[str] = set()
        self._reserved_spawn_operations: set[str] = set()
        self._executor = ThreadPoolExecutor(max_workers=max_children, thread_name_prefix="newsroom-child")
        self._lock = threading.RLock()

    @property
    def events(self) -> InMemoryChildAgentEventLog:
        return self._events

    @property
    def capacity(self) -> int:
        """Configured upper bound used by Harness admission control."""
        return self._max_children

    @property
    def available_capacity(self) -> int:
        """Current admission headroom without exposing mutable handles."""
        with self._lock:
            self._assert_admission()
            occupied = sum(
                1 for item in self._handles.values() if self._capacity_occupied(item)
            )
            return max(
                self._max_children
                - occupied
                - len(self._reserved_spawn_operations),
                0,
            )

    def spawn_batch(
        self,
        requests: Sequence[ChildAgentSpawnRequest | Mapping[str, Any]],
        *,
        workers: Sequence[Any] | None = None,
    ) -> tuple[ChildAgentHandle, ...]:
        """Atomically reserve capacity before admitting a child wave.

        Worker execution can finish while the caller is still admitting the
        rest of a wave. Terminal handles intentionally retain capacity until
        ``close``; the short-lived reservations below ensure those fast
        completions cannot make a previously admitted sibling look over
        capacity.
        """

        normalized = tuple(
            item
            if isinstance(item, ChildAgentSpawnRequest)
            else ChildAgentSpawnRequest.from_mapping(item)
            for item in requests
        )
        if not normalized:
            return ()
        supplied_workers = (
            tuple(_WORKER_NOT_SUPPLIED for _ in normalized)
            if workers is None
            else tuple(workers)
        )
        if len(supplied_workers) != len(normalized):
            raise ValueError("workers must match spawn batch size")
        operation_ids = tuple(item.operation_id for item in normalized)
        if len(operation_ids) != len(set(operation_ids)):
            raise ChildAgentOperationConflict(
                "spawn batch contains duplicate operation identities",
                code="operation_identity_conflict",
            )

        with self._lock:
            new_operations: set[str] = set()
            self._assert_admission()
            for request in normalized:
                previous = self._operations.get(request.operation_id)
                if previous is not None:
                    self._validate_replayed_spawn(request, previous.handle)
                    continue
                new_operations.add(request.operation_id)
            occupied = sum(
                1 for item in self._handles.values() if self._capacity_occupied(item)
            )
            if (
                occupied
                + len(self._reserved_spawn_operations)
                + len(new_operations)
                > self._max_children
            ):
                raise ChildAgentAdmissionError(
                    "child capacity is exhausted",
                    code="child_capacity_exhausted",
                )
            self._reserved_spawn_operations.update(new_operations)
            try:
                return tuple(
                    self.spawn(request, worker=worker)
                    for request, worker in zip(
                        normalized,
                        supplied_workers,
                        strict=True,
                    )
                )
            finally:
                self._reserved_spawn_operations.difference_update(new_operations)

    def spawn(
        self,
        request: ChildAgentSpawnRequest | Mapping[str, Any],
        *,
        worker: Any = _WORKER_NOT_SUPPLIED,
    ) -> ChildAgentHandle:
        if not isinstance(request, ChildAgentSpawnRequest):
            request = ChildAgentSpawnRequest.from_mapping(request)
        with self._lock:
            self._assert_admission()
            previous = self._operations.get(request.operation_id)
            if previous is not None:
                self._validate_replayed_spawn(request, previous.handle)
                return previous.handle
            reserved = request.operation_id in self._reserved_spawn_operations
            if reserved:
                self._reserved_spawn_operations.remove(request.operation_id)
            active = sum(1 for item in self._handles.values() if self._capacity_occupied(item))
            if not reserved and (
                active + len(self._reserved_spawn_operations) >= self._max_children
            ):
                raise ChildAgentAdmissionError("child capacity is exhausted", code="child_capacity_exhausted")
            parent = _identity(request.parent_graph_identity, "parent_graph_identity")
            if self._budget_admitter is not None:
                try:
                    admitted = bool(self._budget_admitter(parent, request.budget))
                except Exception as exc:
                    raise ChildAgentAdmissionError(
                        "parent Graph budget admission could not be verified",
                        code="child_budget_unavailable",
                    ) from exc
                if not admitted:
                    raise ChildAgentAdmissionError(
                        "child request exceeds the parent Graph budget",
                        code="child_budget_exhausted",
                    )
            budget_key = _budget_key(parent)
            self._reserve_budget(budget_key, request.operation_id, request.budget)
            child_id = request.child_id or f"child-{uuid4().hex}"
            child = (
                _identity(request.child_graph_identity, "child_graph_identity")
                if request.child_graph_identity is not None
                else _derived_child_identity(parent, child_id)
            )
            now = _utc(self._clock(), "clock")
            lease = ChildAgentLease(
                lease_id=f"lease-{uuid4().hex}",
                issued_at=now,
                expires_at=now + timedelta(seconds=float(request.lease_seconds or self._default_lease_seconds)),
            )
            handle = ChildAgentHandle(
                child_id=child_id,
                parent_graph_identity=parent,
                child_graph_identity=child,
                stage_id=request.stage_id,
                task_id=request.task_id,
                task_instance_id=request.task_instance_id,
                attempt=request.attempt,
                allowed_tools=request.allowed_tools,
                allowed_memory_namespaces=request.allowed_memory_namespaces,
                budget=request.budget,
                transcript_ref=request.transcript_ref,
                operation_id=request.operation_id,
                state=ChildAgentState.STARTING,
                lease=lease,
                created_at=now,
                updated_at=now,
            )
            # Persist the admission fact before exposing the handle.  A
            # canonical sink failure therefore cannot leave a half-admitted
            # child in the in-memory supervisor.
            try:
                self._emit("child_spawned", handle=handle)
            except Exception:
                if not self._persistence_uncertain:
                    self._finalize_budget_for_operation(request.operation_id, consume=False)
                raise
            self._handles[handle.child_id] = handle
            self._operations[request.operation_id] = ChildAgentOperationResult(request.operation_id, handle.child_id, handle)
            try:
                admitted_worker = (
                    self._build_worker(handle)
                    if worker is _WORKER_NOT_SUPPLIED
                    else worker
                )
            except Exception as exc:
                # The spawn fact is already durable. Retain the operation and
                # commit a terminal failure so recovery cannot resurrect a
                # phantom STARTING child or admit a duplicate side effect.
                try:
                    self._finish(
                        handle,
                        ChildAgentState.FAILED,
                        reason_code="worker_admission_failed",
                        termination_confirmed=True,
                        result={"error_type": type(exc).__name__},
                    )
                except Exception:
                    # If the terminal append is unavailable, keep the
                    # non-terminal handle and reservation in memory.
                    pass
                raise
            if admitted_worker is not None:
                try:
                    self._workers[handle.child_id] = admitted_worker
                    handle = replace(handle, state=ChildAgentState.RUNNING, updated_at=_utc(self._clock()))
                    self._emit("child_status", handle=handle, reason_code="worker_started")
                    self._replace(handle)
                    handle = self._start_worker(handle, admitted_worker)
                except Exception as exc:
                    # Durable spawn/status facts remain authoritative. Do not
                    # remove their indexes after a worker start failure.
                    self._futures.pop(handle.child_id, None)
                    self._workers.pop(handle.child_id, None)
                    try:
                        current = self._handles.get(handle.child_id, handle)
                        if current.state not in TERMINAL_CHILD_STATES:
                            self._finish(
                                current,
                                ChildAgentState.FAILED,
                                reason_code="worker_start_failed",
                                termination_confirmed=True,
                                result={"error_type": type(exc).__name__},
                            )
                    except Exception:
                        pass
                    raise
            return handle

    def status(self, child_id: str, *, operation_id: str | None = None) -> ChildAgentHandle:
        with self._lock:
            handle = self._require(child_id)
            if operation_id is not None and operation_id != handle.operation_id:
                raise ChildAgentOperationConflict("operation does not belong to child", code="operation_identity_conflict")
            handle = self._expire_if_stale(handle)
            return handle

    def wait(self, child_id: str, *, operation_id: str, timeout_seconds: float | None = None) -> ChildAgentOperationResult:
        handle = self.status(child_id, operation_id=operation_id)
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        future = self._futures.get(child_id)
        if future is not None and not future.done():
            try:
                future.result(timeout=timeout_seconds)
            except FutureTimeout:
                pass
            except Exception:
                pass
        if future is not None and future.done():
            self._settle_future(child_id, future)
        with self._lock:
            result = self._operations.get(operation_id)
            if result is None or result.child_id != child_id:
                raise ChildAgentOperationConflict("unknown wait operation", code="operation_not_found")
            return result

    def heartbeat(self, heartbeat: ChildAgentHeartbeat | Mapping[str, Any]) -> ChildAgentHandle:
        if not isinstance(heartbeat, ChildAgentHeartbeat):
            heartbeat = ChildAgentHeartbeat(**dict(heartbeat))
        with self._lock:
            handle = self._require(heartbeat.child_id)
            if handle.state in TERMINAL_CHILD_STATES:
                return handle
            if heartbeat.lease_id != handle.lease.lease_id:
                raise ChildAgentOperationConflict("heartbeat lease does not match handle", code="lease_conflict")
            if heartbeat.heartbeat_seq <= handle.lease.heartbeat_seq:
                return handle
            now = _utc(self._clock())
            observed_at = _utc(heartbeat.observed_at, "observed_at")
            if observed_at > now + timedelta(seconds=5):
                raise ChildAgentOperationConflict(
                    "heartbeat timestamp is too far in the future",
                    code="heartbeat_timestamp_invalid",
                )
            # An expired lease cannot be revived by a late heartbeat.  The
            # supervisor owns reclaim and records the uncertain termination.
            if handle.lease.is_expired(now):
                return self._expire_if_stale(handle, now=now)
            now = max(now, observed_at)
            duration = max(0.001, (handle.lease.expires_at - handle.lease.issued_at).total_seconds())
            lease = ChildAgentLease(handle.lease.lease_id, now, now + timedelta(seconds=duration), heartbeat.heartbeat_seq)
            handle = replace(handle, lease=lease, state=ChildAgentState.RUNNING, updated_at=now)
            self._emit("child_heartbeat", handle=handle)
            self._replace(handle)
            return handle

    def cancel(self, child_id: str, *, operation_id: str, reason: str = "operator_cancel") -> ChildAgentOperationResult:
        with self._lock:
            handle = self._require(child_id)
            existing = self._operations.get(operation_id)
            if (
                existing is None
                or existing.child_id != child_id
                or operation_id != handle.operation_id
            ):
                raise ChildAgentOperationConflict(
                    "operation does not belong to child",
                    code="operation_identity_conflict",
                )
            if existing is not None and existing.receipt is not None:
                return existing
            if handle.state is ChildAgentState.CANCELLED:
                return self._operations[operation_id]
            if handle.state in {ChildAgentState.SUCCEEDED, ChildAgentState.FAILED, ChildAgentState.LOST, ChildAgentState.CLOSED}:
                return self._operations[operation_id]
            if handle.state is not ChildAgentState.CANCEL_REQUESTED:
                handle = replace(handle, state=ChildAgentState.CANCEL_REQUESTED, updated_at=_utc(self._clock()))
                self._emit("child_cancel_requested", handle=handle, reason_code=reason)
                self._replace(handle)
            worker = self._workers.get(child_id)
            confirmed = self._cancel_worker(worker, handle)
            if confirmed:
                return self._finish(handle, ChildAgentState.CANCELLED, reason_code=reason, termination_confirmed=True)
            return self._finish(handle, ChildAgentState.LOST, reason_code="cancellation_unconfirmed", termination_confirmed=False)

    def close(self, child_id: str, *, operation_id: str) -> ChildAgentOperationResult:
        with self._lock:
            handle = self._require(child_id)
            result = self._operations.get(operation_id)
            if result is None or result.child_id != child_id:
                raise ChildAgentOperationConflict("unknown close operation", code="operation_identity_conflict")
            if handle.state is ChildAgentState.CLOSED:
                if result.receipt is None:
                    raise ChildAgentSupervisorError(
                        "closed child has no verified receipt",
                        code="terminal_receipt_missing",
                    )
                return result
            if handle.state not in TERMINAL_CHILD_STATES:
                raise ChildAgentSupervisorError("child must be terminal before close", code="child_not_terminal")
            if result.receipt is None:
                raise ChildAgentSupervisorError(
                    "terminal child has no verified receipt",
                    code="terminal_receipt_missing",
                )
            if handle.state is ChildAgentState.LOST and not result.receipt.termination_confirmed:
                raise ChildAgentSupervisorError(
                    "lost child termination is not confirmed",
                    code="termination_unconfirmed",
                )
            handle = replace(handle, state=ChildAgentState.CLOSED, updated_at=_utc(self._clock()))
            self._emit("child_closed", handle=handle)
            self._replace(handle)
            self._operations[operation_id] = replace(result, handle=handle)
            return self._operations[operation_id]

    def shutdown(self, *, wait: bool = True) -> None:
        """Release supervisor worker resources after all lifecycle facts settle."""
        self.stop_admission()
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def stop_admission(self) -> None:
        """Fence new work while already-admitted workers settle."""
        with self._lock:
            self._admission_closed = True

    def cancel_active(self, *, reason: str = "runtime_shutdown") -> None:
        with self._lock:
            active = tuple(h for h in self._handles.values() if h.state not in TERMINAL_CHILD_STATES)
        for handle in active:
            self.cancel(handle.child_id, operation_id=handle.operation_id, reason=reason)

    def _assert_admission(self) -> None:
        if self._admission_closed or self._recovery_blocked or self._persistence_uncertain:
            raise ChildAgentAdmissionError("child admission is closed", code="child_admission_closed")
        if self._owner_guard is not None:
            self._owner_guard()

    def __enter__(self) -> "ChildAgentSupervisor":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.shutdown()

    def reclaim_stale(self, *, now: datetime | None = None) -> tuple[ChildAgentHandle, ...]:
        now = _utc(now or self._clock())
        with self._lock:
            self._assert_persistence()
            if self._owner_guard is not None:
                self._owner_guard()
            changed: list[ChildAgentHandle] = []
            for handle in tuple(self._handles.values()):
                if handle.state in TERMINAL_CHILD_STATES or not handle.lease.is_expired(now):
                    continue
                changed.append(self._expire_if_stale(handle, now=now))
            return tuple(changed)

    def recover(self, events: Sequence[Mapping[str, Any]] | None = None) -> tuple[ChildAgentHandle, ...]:
        """Rebuild handles from lifecycle facts without starting workers."""
        with self._lock:
            try:
                return self._recover(events)
            except (TypeError, ValueError, KeyError, AttributeError) as exc:
                self._recovery_blocked = True
                raise ChildAgentSupervisorError("child recovery history is malformed", code="child_recovery_corrupt") from exc

    def _recover(self, events: Sequence[Mapping[str, Any]] | None) -> tuple[ChildAgentHandle, ...]:
        with self._lock:
            if self._workers or self._futures or self._reserved_spawn_operations:
                raise ChildAgentSupervisorError("cannot recover over live supervisor workers", code="child_recovery_live_runtime")
            self._recovery_blocked = True
            if self._owner_guard is not None:
                self._owner_guard()
        if events is not None:
            source = tuple(events)
        elif self._event_reader is not None:
            source = tuple(self._event_reader.read_events())
        else:
            source = tuple(self._events.events)
        with self._lock:
            # Recovery is a replacement of the committed view, not an
            # incremental merge.  Clear derived indexes first so a reused
            # supervisor cannot expose children absent from durable history.
            self._handles.clear()
            self._operations.clear()
            self._workers.clear()
            self._futures.clear()
            self._recovered_active_children.clear()
            # Reconstruct the in-memory reservation view from the durable
            # lifecycle facts as well as the handles. This prevents a parent
            # restart from admitting a second child against already consumed
            # or still-indeterminate Graph budget.
            self._budget_reservations.clear()
            self._budget_consumed.clear()
            self._budget_limits_by_key.clear()
            self._budget_by_operation.clear()
            grouped: dict[str, list[Mapping[str, Any]]] = {}
            for event in source:
                child_id = event.get("child_id")
                if not isinstance(child_id, str) or not child_id.strip():
                    raise ChildAgentSupervisorError("child history has no admission identity", code="child_recovery_corrupt")
                grouped.setdefault(child_id, []).append(event)
            recovered: list[ChildAgentHandle] = []
            for child_id, facts in grouped.items():
                facts = sorted(
                    facts,
                    key=lambda item: _event_sort_key(item),
                )
                spawned = next((item for item in facts if item.get("event_type") == "child_spawned"), None)
                if not spawned:
                    raise ChildAgentSupervisorError("child history has no spawn record", code="child_recovery_corrupt")
                try:
                    handle = _handle_from_event(spawned)
                except (TypeError, ValueError, KeyError) as exc:
                    raise ChildAgentSupervisorError("child spawn history is malformed", code="child_recovery_corrupt") from exc
                if handle.operation_id in self._operations:
                    raise ChildAgentSupervisorError("child operation is assigned to multiple children", code="child_recovery_corrupt")
                if sum(item.get("event_type") == "child_spawned" for item in facts) != 1:
                    raise ChildAgentSupervisorError("child has conflicting spawn records", code="child_recovery_corrupt")
                recovery_corrupt = False
                latest_state = handle.state
                admission_handle = handle
                admission_has_contract = bool(
                    {"handle_contract", "handle_schema_version", "handle_checksum"}
                    .intersection(spawned)
                )
                for event in facts:
                    try:
                        event_handle = _event_handle_contract(event)
                        if admission_has_contract and event_handle is None:
                            raise ValueError(
                                "versioned child history event is missing handle contract"
                            )
                        if event_handle is not None:
                            for field_name in (
                                "child_id",
                                "operation_id",
                                "parent_graph_identity",
                                "child_graph_identity",
                                "stage_id",
                                "task_id",
                                "task_instance_id",
                                "attempt",
                                "allowed_tools",
                                "allowed_memory_namespaces",
                                "budget",
                                "transcript_ref",
                            ):
                                if getattr(event_handle, field_name) != getattr(admission_handle, field_name):
                                    raise ValueError(
                                        f"child event handle contract {field_name} drifted"
                                    )
                            if event_handle.created_at != admission_handle.created_at:
                                raise ValueError("child event handle contract created_at drifted")
                    except (TypeError, ValueError, KeyError, AttributeError):
                        recovery_corrupt = True
                        continue
                    try:
                        event_metadata = event.get("metadata")
                        if not isinstance(event_metadata, Mapping):
                            event_metadata = {}
                        event_receipt = event.get("terminal_receipt")
                        if not isinstance(event_receipt, Mapping):
                            event_receipt = event_metadata.get("terminal_receipt")
                        if not isinstance(event_receipt, Mapping):
                            event_receipt = {}
                        expected_event_id = _event_id_from_fields(
                            {
                                "child_id": event.get("child_id"),
                                "operation_id": event.get("operation_id"),
                                "event_type": event.get("event_type"),
                                "state": event.get("state"),
                                "reason_code": event.get("reason_code"),
                                "heartbeat_seq": event.get("heartbeat_seq", 0),
                                "result_checksum": event.get("result_checksum")
                                or event_metadata.get("result_checksum"),
                                "receipt_checksum": event_receipt.get("receipt_checksum"),
                            }
                        )
                        if event.get("event_id") != expected_event_id:
                            recovery_corrupt = True
                        occurred_at = _event_time(event)
                    except (TypeError, ValueError):
                        recovery_corrupt = True
                        continue
                    try:
                        for field_name, expected_value in (
                            ("stage_id", handle.stage_id),
                            ("task_id", handle.task_id),
                            ("task_instance_id", handle.task_instance_id),
                            ("attempt", handle.attempt),
                            ("budget", dict(handle.budget)),
                            ("allowed_tools", list(handle.allowed_tools)),
                            ("allowed_memory_namespaces", list(handle.allowed_memory_namespaces)),
                            ("transcript_ref", handle.transcript_ref),
                        ):
                            if event.get(field_name) != expected_value:
                                recovery_corrupt = True
                        if (
                            event.get("operation_id") != handle.operation_id
                            or _identity(event.get("parent_graph_identity"), "parent_graph_identity")
                            != handle.parent_graph_identity
                            or (
                                event.get("child_graph_identity") is not None
                                and _identity(event.get("child_graph_identity"), "child_graph_identity")
                                != handle.child_graph_identity
                            )
                        ):
                            recovery_corrupt = True
                            continue
                    except (TypeError, ValueError):
                        recovery_corrupt = True
                        continue
                    state = event.get("state")
                    if state:
                        try:
                            lease_id = event.get("lease_id") or handle.lease.lease_id
                            lease_issued_at = event.get("lease_issued_at") or handle.lease.issued_at.isoformat()
                            lease_expires_at = event.get("lease_expires_at") or handle.lease.expires_at.isoformat()
                            heartbeat_seq = int(event.get("heartbeat_seq", handle.lease.heartbeat_seq))
                            if heartbeat_seq < handle.lease.heartbeat_seq:
                                recovery_corrupt = True
                                continue
                            lease = ChildAgentLease(
                                lease_id=lease_id,
                                issued_at=datetime.fromisoformat(lease_issued_at),
                                expires_at=datetime.fromisoformat(lease_expires_at),
                                heartbeat_seq=heartbeat_seq,
                            )
                            parsed_state = ChildAgentState(state)
                            latest_state = parsed_state
                            handle = replace(
                                handle,
                                state=parsed_state,
                                lease=lease,
                                updated_at=occurred_at,
                            )
                        except (TypeError, ValueError, KeyError):
                            recovery_corrupt = True
                self._handles[child_id] = handle
                operation = ChildAgentOperationResult(handle.operation_id, child_id, handle)
                terminal_event = next(
                    (item for item in reversed(facts) if item.get("event_type") == "child_terminal"),
                    None,
                )
                if terminal_event is None and latest_state in TERMINAL_CHILD_STATES:
                    recovery_corrupt = True
                if terminal_event is not None:
                    metadata = terminal_event.get("metadata") or {}
                    raw_receipt = (
                        terminal_event.get("terminal_receipt")
                        or metadata.get("terminal_receipt")
                    )
                    if isinstance(raw_receipt, Mapping):
                        try:
                            receipt = _terminal_receipt_from_dict(raw_receipt)
                            if (
                                receipt.child_id != handle.child_id
                                or receipt.operation_id != handle.operation_id
                                or receipt.parent_graph_identity != handle.parent_graph_identity
                            ):
                                raise ValueError("terminal receipt identity does not match recovered handle")
                            event_result_ref = terminal_event.get("result_ref")
                            event_result_checksum = terminal_event.get("result_checksum")
                            metadata_result_ref = metadata.get("result_ref")
                            metadata_result_checksum = metadata.get("result_checksum")
                            if event_result_ref is not None and event_result_ref != receipt.result_ref:
                                raise ValueError("terminal event result ref does not match receipt")
                            if event_result_checksum is not None and event_result_checksum != receipt.result_checksum:
                                raise ValueError("terminal event result checksum does not match receipt")
                            if metadata_result_ref is not None and metadata_result_ref != receipt.result_ref:
                                raise ValueError("terminal metadata result ref does not match receipt")
                            if metadata_result_checksum is not None and metadata_result_checksum != receipt.result_checksum:
                                raise ValueError("terminal metadata result checksum does not match receipt")
                            if terminal_event.get("state") not in {None, receipt.status.value}:
                                raise ValueError("terminal event state does not match receipt")
                            if latest_state not in {receipt.status, ChildAgentState.CLOSED}:
                                raise ValueError("latest lifecycle state does not match receipt")
                            handle = replace(
                                handle,
                                state=(
                                    ChildAgentState.CLOSED
                                    if latest_state is ChildAgentState.CLOSED
                                    else receipt.status
                                ),
                                terminal_receipt_ref=(
                                    f"child-receipt://{handle.child_id}/{handle.operation_id}"
                                ),
                            )
                            recovered_result = None
                            embedded_result = terminal_event.get("result")
                            metadata_result = metadata.get("result")
                            if embedded_result is not None and not isinstance(embedded_result, Mapping):
                                raise ValueError("terminal event result must be an object")
                            if metadata_result is not None and not isinstance(metadata_result, Mapping):
                                raise ValueError("terminal metadata result must be an object")
                            if (
                                isinstance(embedded_result, Mapping)
                                and isinstance(metadata_result, Mapping)
                                and dict(embedded_result) != dict(metadata_result)
                            ):
                                raise ValueError("terminal event result conflicts with metadata result")
                            durable_result = (
                                embedded_result
                                if isinstance(embedded_result, Mapping)
                                else metadata_result
                            )
                            if isinstance(durable_result, Mapping):
                                checksum = "sha256:" + hashlib.sha256(
                                    stable_json_dumps(durable_result).encode("utf-8")
                                ).hexdigest()
                                if checksum != receipt.result_checksum:
                                    raise ValueError("embedded result checksum does not match receipt")
                                recovered_result = dict(durable_result)
                            if receipt.result_ref and self._result_resolver is not None:
                                try:
                                    resolved_result = self._result_resolver(receipt.result_ref)
                                except Exception:
                                    resolved_result = None
                                if resolved_result is None:
                                    raise ValueError("terminal result reference could not be resolved")
                                checksum = "sha256:" + hashlib.sha256(
                                    stable_json_dumps(resolved_result).encode("utf-8")
                                ).hexdigest()
                                if checksum != receipt.result_checksum:
                                    raise ValueError("recovered result checksum does not match receipt")
                                if recovered_result is not None and recovered_result != dict(resolved_result):
                                    raise ValueError("resolved result conflicts with embedded result")
                                recovered_result = dict(resolved_result)
                            operation = ChildAgentOperationResult(
                                handle.operation_id,
                                child_id,
                                handle,
                                receipt,
                                recovered_result,
                            )
                        except (TypeError, ValueError, KeyError):
                            recovery_corrupt = True
                    else:
                        recovery_corrupt = True
                if recovery_corrupt:
                    # An ambiguous history cannot be replayed as a completed
                    # child. Mark it LOST without a confirmed receipt so its
                    # capacity remains occupied pending reconciliation.
                    handle = replace(
                        handle,
                        state=ChildAgentState.LOST,
                        terminal_receipt_ref=None,
                        updated_at=_utc(self._clock()),
                    )
                    operation = ChildAgentOperationResult(handle.operation_id, child_id, handle)
                self._handles[child_id] = handle
                self._operations[handle.operation_id] = operation
                self._restore_budget_for_recovered_operation(handle, operation)
                if handle.state not in TERMINAL_CHILD_STATES:
                    # A committed spawn/status fact does not prove that the
                    # provider process died with this Python process.  Until a
                    # provider reattaches with termination authority, cancel
                    # and lease reclaim must remain indeterminate.
                    self._recovered_active_children.add(child_id)
                recovered.append(handle)
            self._recovery_blocked = False
            self._persistence_uncertain = False
            return tuple(recovered)

    def validate_output(self, output: Mapping[str, Any] | Any, *, handle: ChildAgentHandle | str) -> dict[str, Any]:
        if isinstance(handle, str):
            handle = self._require(handle)
        if not isinstance(output, Mapping):
            raise ChildAgentSupervisorError("child output must be an object", code="child_output_invalid")
        forbidden = sorted(_find_forbidden_keys(output, _FORBIDDEN_OUTPUT_KEYS))
        if forbidden:
            self._emit("child_boundary_violation", handle=handle, reason_code="child_control_field_forbidden", metadata={"fields": forbidden})
            raise ChildAgentSupervisorError("child output contains control authority fields", code="child_control_field_forbidden")
        return dict(output)

    def _build_worker(self, handle: ChildAgentHandle) -> Any:
        if self._worker_factory is None:
            return None
        factory = self._worker_factory
        # Choose the supported call shape before invocation. Retrying a
        # factory after an in-body TypeError could duplicate admission or
        # external side effects.
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            return factory(handle)
        try:
            signature.bind(handle, {})
        except TypeError:
            return factory(handle)
        return factory(handle, {})  # type: ignore[misc]

    def _start_worker(self, handle: ChildAgentHandle, worker: Any) -> ChildAgentHandle:
        # The supervisor owns a bounded thread pool so status/wait/cancel can
        # observe a live child without handing lifecycle authority to the worker.
        if self._owner_guard is not None:
            self._owner_guard()
        if isinstance(worker, Mapping):
            return self.complete(
                handle.child_id,
                operation_id=handle.operation_id,
                output=worker,
            ).handle
            return
        run = getattr(worker, "run", None) or getattr(worker, "invoke", None)
        if run is None and callable(worker):
            run = worker
        if not callable(run):
            return self._finish(
                handle,
                ChildAgentState.FAILED,
                reason_code="worker_not_runnable",
                termination_confirmed=True,
                result={"error_code": "worker_not_runnable"},
            ).handle
        def invoke() -> Any:
            if self._owner_guard is not None:
                self._owner_guard()
            return run(handle)

        future = self._executor.submit(invoke)
        self._futures[handle.child_id] = future
        future.add_done_callback(
            lambda completed, child_id=handle.child_id: self._settle_future_safely(
                child_id, completed
            )
        )
        return handle

    def _settle_future_safely(self, child_id: str, future: Future[Any]) -> None:
        try:
            self._settle_future(child_id, future)
        except (ChildAgentSupervisorError, ChildAgentNotFoundError):
            # The operation may have been cancelled/reclaimed concurrently;
            # the committed supervisor state remains authoritative.
            return

    def _settle_future(self, child_id: str, future: Future[Any]) -> None:
        with self._lock:
            handle = self._require(child_id)
            if handle.state in TERMINAL_CHILD_STATES:
                return
            try:
                output = future.result()
                if output is None:
                    self._finish(
                        handle,
                        ChildAgentState.FAILED,
                        reason_code="worker_output_missing",
                        termination_confirmed=True,
                        result={"error_code": "worker_output_missing"},
                    )
                    return
                self.complete(child_id, operation_id=handle.operation_id, output=output)
            except ChildAgentSupervisorError as exc:
                if exc.code.startswith(("child_event_store_", "child_owner_")):
                    # An uncertain durable commit cannot be rewritten as a
                    # worker failure. Preserve occupancy and retry observation.
                    raise
                # A worker that returns an invalid candidate is a failed child,
                # not a permanently RUNNING lease.  ``complete`` may already
                # have committed this terminal state for boundary violations.
                current = self._handles.get(child_id)
                if current is not None and current.state not in TERMINAL_CHILD_STATES:
                    self._finish(
                        current,
                        ChildAgentState.FAILED,
                        reason_code=getattr(exc, "code", "worker_output_invalid"),
                        termination_confirmed=True,
                        result={"error_code": getattr(exc, "code", type(exc).__name__)},
                    )
            except Exception as exc:
                self._finish(handle, ChildAgentState.FAILED, reason_code="worker_failed", termination_confirmed=True, result={"error_type": type(exc).__name__})

    def complete(self, child_id: str, *, operation_id: str, output: Mapping[str, Any] | Any, result_ref: str | None = None) -> ChildAgentOperationResult:
        with self._lock:
            handle = self._require(child_id)
            if operation_id != handle.operation_id:
                raise ChildAgentOperationConflict("operation identity does not match child", code="operation_identity_conflict")
            existing = self._operations.get(operation_id)
            if existing is not None and existing.receipt is not None:
                return existing
            if handle.state in TERMINAL_CHILD_STATES:
                raise ChildAgentOperationConflict(
                    "child already has a terminal state without a receipt",
                    code="terminal_receipt_missing",
                )
            try:
                result = self.validate_output(output, handle=handle)
            except ChildAgentSupervisorError as exc:
                self._finish(
                    handle,
                    ChildAgentState.FAILED,
                    reason_code=getattr(exc, "code", "child_output_invalid"),
                    termination_confirmed=True,
                    result={"error_code": getattr(exc, "code", type(exc).__name__)},
                )
                raise
            return self._finish(handle, ChildAgentState.SUCCEEDED, reason_code="worker_completed", termination_confirmed=True, result=result, result_ref=result_ref)

    def _finish(self, handle: ChildAgentHandle, state: ChildAgentState, *, reason_code: str, termination_confirmed: bool, result: Mapping[str, Any] | None = None, result_ref: str | None = None) -> ChildAgentOperationResult:
        existing = self._operations.get(handle.operation_id)
        if existing is not None and existing.receipt is not None:
            return existing
        now = _utc(self._clock())
        result_ref = result_ref or (
            f"child-result://{handle.child_id}/{handle.operation_id}"
            if result is not None
            else None
        )
        result_checksum = (
            "sha256:" + hashlib.sha256(stable_json_dumps(result).encode("utf-8")).hexdigest()
            if result is not None
            else None
        )
        receipt = ChildAgentTerminalReceipt(
            child_id=handle.child_id,
            operation_id=handle.operation_id,
            parent_graph_identity=handle.parent_graph_identity,
            status=state,
            reason_code=reason_code,
            result_ref=result_ref,
            result_checksum=result_checksum,
            termination_confirmed=termination_confirmed,
            completed_at=now,
        )
        handle = replace(handle, state=state, updated_at=now, terminal_receipt_ref=f"child-receipt://{handle.child_id}/{handle.operation_id}")
        op = ChildAgentOperationResult(
            handle.operation_id,
            handle.child_id,
            handle,
            receipt,
            dict(result) if result is not None else None,
        )
        try:
            self._emit(
                "child_terminal",
                handle=handle,
                reason_code=reason_code,
                metadata={
                    "termination_confirmed": termination_confirmed,
                    "terminal_receipt": receipt.to_dict(),
                    "result_ref": result_ref,
                    "result_checksum": result_checksum,
                    "result": dict(result) if result is not None else None,
                },
            )
        except ChildAgentSupervisorError as exc:
            if exc.code != "child_event_too_large" or reason_code == "child_result_too_large":
                raise
            return self._finish(
                handle, ChildAgentState.FAILED, reason_code="child_result_too_large",
                termination_confirmed=termination_confirmed,
                result={"error_code": "child_result_too_large"},
            )
        self._replace(handle)
        self._operations[handle.operation_id] = op
        self._recovered_active_children.discard(handle.child_id)
        if termination_confirmed:
            self._finalize_budget_for_operation(
                handle.operation_id,
                consume=state in {ChildAgentState.SUCCEEDED, ChildAgentState.FAILED},
            )
        return op

    def _expire_if_stale(self, handle: ChildAgentHandle, *, now: datetime | None = None) -> ChildAgentHandle:
        now = _utc(now or self._clock())
        if handle.state not in TERMINAL_CHILD_STATES and handle.lease.is_expired(now):
            requested = replace(handle, state=ChildAgentState.CANCEL_REQUESTED, updated_at=now)
            if handle.state is not ChildAgentState.CANCEL_REQUESTED:
                self._emit("child_cancel_requested", handle=requested, reason_code="child_lease_expired")
                self._replace(requested)
            worker = self._workers.get(handle.child_id)
            confirmed = self._cancel_worker(worker, requested)
            result = self._finish(
                requested,
                ChildAgentState.LOST,
                reason_code=("child_lease_expired" if confirmed else "termination_unconfirmed"),
                termination_confirmed=confirmed,
            )
            return result.handle
        return handle

    def _cancel_worker(self, worker: Any, handle: ChildAgentHandle) -> bool:
        if worker is None:
            return handle.child_id not in self._recovered_active_children
        cancel = getattr(worker, "cancel", None)
        if not callable(cancel):
            return False
        outcome: list[bool] = []

        def invoke() -> None:
            try:
                outcome.append(bool(cancel(handle)))
            except Exception:
                outcome.append(False)

        thread = threading.Thread(
            target=invoke,
            name=f"newsroom-child-cancel-{handle.child_id}",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=self._cancel_timeout_seconds)
        if thread.is_alive():
            return False
        return bool(outcome and outcome[0])

    def _validate_replayed_spawn(
        self,
        request: ChildAgentSpawnRequest,
        handle: ChildAgentHandle,
    ) -> None:
        parent = _identity(request.parent_graph_identity, "parent_graph_identity")
        child = (
            _identity(request.child_graph_identity, "child_graph_identity")
            if request.child_graph_identity is not None
            else None
        )
        lease_seconds = (handle.lease.expires_at - handle.lease.issued_at).total_seconds()
        mismatch = (
            handle.parent_graph_identity != parent
            or (request.child_id is not None and request.child_id != handle.child_id)
            or (child is not None and child != handle.child_graph_identity)
            or request.stage_id != handle.stage_id
            or request.task_id != handle.task_id
            or request.task_instance_id != handle.task_instance_id
            or request.attempt != handle.attempt
            or tuple(sorted(request.allowed_tools)) != handle.allowed_tools
            or tuple(sorted(request.allowed_memory_namespaces))
            != handle.allowed_memory_namespaces
            or dict(request.budget) != dict(handle.budget)
            or request.transcript_ref != handle.transcript_ref
            or float(request.lease_seconds) != lease_seconds
        )
        if mismatch:
            raise ChildAgentOperationConflict(
                "operation identity was reused with a different child admission",
                code="operation_identity_conflict",
            )

    def _replace(self, handle: ChildAgentHandle) -> None:
        self._handles[handle.child_id] = handle
        result = self._operations.get(handle.operation_id)
        if result is not None and result.receipt is None:
            self._operations[handle.operation_id] = replace(result, handle=handle)

    def _capacity_occupied(self, handle: ChildAgentHandle) -> bool:
        if handle.state is ChildAgentState.CLOSED:
            return False
        if handle.state is not ChildAgentState.LOST:
            return True
        result = self._operations.get(handle.operation_id)
        return result is None or result.receipt is None or not result.receipt.termination_confirmed

    def _reserve_budget(
        self,
        budget_key: str,
        operation_id: str,
        budget: Mapping[str, Any],
    ) -> None:
        amounts = _budget_amounts(budget)
        if not amounts:
            return
        limits = _budget_limits(budget)
        existing_limits = dict(self._budget_limits_by_key.get(budget_key, {}))
        if limits:
            for dimension, limit in limits.items():
                previous = existing_limits.get(dimension)
                if previous is not None and previous != limit:
                    raise ChildAgentAdmissionError(
                        f"parent budget limit for {dimension} changed during admission",
                        code="child_budget_conflict",
                    )
                existing_limits[dimension] = limit
        reserved = dict(self._budget_reservations.get(budget_key, {}))
        consumed = self._budget_consumed.get(budget_key, {})
        known_limits = existing_limits
        for dimension, amount in amounts.items():
            limit = known_limits.get(dimension)
            if limit is not None and reserved.get(dimension, 0) + consumed.get(dimension, 0) + amount > limit:
                raise ChildAgentAdmissionError(
                    f"child budget exceeds remaining {dimension} capacity",
                    code="child_budget_exhausted",
                )
        for dimension, amount in amounts.items():
            reserved[dimension] = reserved.get(dimension, 0) + amount
        self._budget_limits_by_key[budget_key] = existing_limits
        self._budget_reservations[budget_key] = reserved
        self._budget_by_operation[operation_id] = (budget_key, amounts)

    def _finalize_budget_for_operation(self, operation_id: str, *, consume: bool) -> None:
        reservation = self._budget_by_operation.get(operation_id)
        if reservation is None:
            return
        budget_key, amounts = reservation
        current = self._budget_reservations.get(budget_key, {})
        consumed = self._budget_consumed.setdefault(budget_key, {})
        for dimension, amount in amounts.items():
            remaining = current.get(dimension, 0) - amount
            if remaining > 0:
                current[dimension] = remaining
            else:
                current.pop(dimension, None)
            if consume:
                consumed[dimension] = consumed.get(dimension, 0) + amount
        if not current:
            self._budget_reservations.pop(budget_key, None)
        self._budget_by_operation.pop(operation_id, None)

    def _restore_budget_for_recovered_operation(
        self,
        handle: ChildAgentHandle,
        operation: ChildAgentOperationResult,
    ) -> None:
        amounts = _budget_amounts(handle.budget)
        if not amounts:
            return
        budget_key = _budget_key(handle.parent_identity)
        limits = _budget_limits(handle.budget)
        if limits:
            existing = self._budget_limits_by_key.setdefault(budget_key, {})
            for dimension, limit in limits.items():
                if dimension in existing and existing[dimension] != limit:
                    # Conflicting parent budget facts are not safe to merge.
                    raise ChildAgentAdmissionError(
                        f"recovered parent budget limit for {dimension} conflicts",
                        code="child_budget_conflict",
                    )
                existing[dimension] = limit
        receipt = operation.receipt
        if receipt is None or not receipt.termination_confirmed:
            reserved = self._budget_reservations.setdefault(budget_key, {})
            for dimension, amount in amounts.items():
                reserved[dimension] = reserved.get(dimension, 0) + amount
            self._budget_by_operation[handle.operation_id] = (budget_key, amounts)
            return
        if receipt.status in {ChildAgentState.SUCCEEDED, ChildAgentState.FAILED}:
            consumed = self._budget_consumed.setdefault(budget_key, {})
            for dimension, amount in amounts.items():
                consumed[dimension] = consumed.get(dimension, 0) + amount

    def _require(self, child_id: str) -> ChildAgentHandle:
        self._assert_persistence()
        if self._owner_guard is not None:
            self._owner_guard()
        try:
            return self._handles[child_id]
        except KeyError as exc:
            raise ChildAgentNotFoundError("child agent was not found", code="child_not_found") from exc

    def _emit(self, event_type: str, *, handle: ChildAgentHandle, reason_code: str | None = None, metadata: Mapping[str, Any] | None = None) -> None:
        self._assert_persistence()
        if self._owner_guard is not None:
            self._owner_guard()
        metadata = dict(metadata or {})
        heartbeat_seq = handle.lease.heartbeat_seq
        terminal_receipt = metadata.get("terminal_receipt")
        result_checksum = (
            terminal_receipt.get("result_checksum")
            if isinstance(terminal_receipt, Mapping)
            else None
        )
        receipt_checksum = (
            terminal_receipt.get("receipt_checksum")
            if isinstance(terminal_receipt, Mapping)
            else None
        )
        stable_event_id = _event_id_from_fields(
            {
                "child_id": handle.child_id,
                "operation_id": handle.operation_id,
                "event_type": event_type,
                "state": handle.state.value,
                "reason_code": reason_code,
                "heartbeat_seq": heartbeat_seq,
                "result_checksum": result_checksum,
                "receipt_checksum": receipt_checksum,
            }
        )
        event = {
            "event_type": event_type,
            "event_id": stable_event_id,
            "child_id": handle.child_id,
            "operation_id": handle.operation_id,
            "state": handle.state.value,
            "parent_graph_identity": handle.parent_graph_identity.to_dict(),
            "child_graph_identity": handle.child_graph_identity.to_dict() if handle.child_graph_identity else None,
            "stage_id": handle.stage_id,
            "task_id": handle.task_id,
            "task_instance_id": handle.task_instance_id,
            "attempt": handle.attempt,
            "lease_id": handle.lease.lease_id,
            "lease_issued_at": handle.lease.issued_at.isoformat(),
            "lease_expires_at": handle.lease.expires_at.isoformat(),
            "heartbeat_seq": heartbeat_seq,
            "occurred_at": _utc(self._clock()).isoformat(),
            "reason_code": reason_code,
            "metadata": metadata,
            "allowed_tools": list(handle.allowed_tools),
            "allowed_memory_namespaces": list(handle.allowed_memory_namespaces),
            "budget": dict(handle.budget),
            "transcript_ref": handle.transcript_ref,
        }
        handle_contract = handle.handle_contract()
        event["handle_schema_version"] = handle_contract["schema_version"]
        event["handle_checksum"] = handle_contract["handle_checksum"]
        event["handle_contract"] = handle_contract
        terminal_receipt = metadata.get("terminal_receipt")
        if isinstance(terminal_receipt, Mapping):
            event["terminal_receipt"] = dict(terminal_receipt)
            event["result_ref"] = terminal_receipt.get("result_ref")
            event["result_checksum"] = terminal_receipt.get("result_checksum")
        if self._runtime_event_sink is not None:
            from framework.events.runtime.projection import RuntimeEventEmitter, RuntimeEventIdentity

            runtime_type = {
                "child_cancel_requested": "cancel_requested",
                "child_boundary_violation": "runtime_error",
            }.get(event_type, event_type)
            RuntimeEventEmitter(
                self._runtime_event_sink,
                identity=RuntimeEventIdentity(
                    graph_identity=handle.graph_identity,
                    activity_id=handle.graph_identity.activity_id,
                    node_id=handle.graph_identity.node_id,
                    node_instance_id=handle.graph_identity.node_instance_id,
                    attempt_id=handle.operation_id,
                ),
                source="child-agent-supervisor",
                stream_id=handle.parent_graph_identity.run_id,
            ).emit(
                runtime_type,
                event_id=stable_event_id,
                status=handle.state.value.lower(),
                reason_code=reason_code,
                refs=tuple(
                    ref
                    for ref in (handle.child_id, handle.transcript_ref, handle.terminal_receipt_ref)
                    if ref
                ),
                metadata={
                    "child_id": handle.child_id,
                    "operation_id": handle.operation_id,
                    "stage_id": handle.stage_id,
                    "task_id": handle.task_id,
                    "state": handle.state.value,
                    **metadata,
                },
            )
        sink = self._event_sink
        try:
            if callable(sink):
                sink(event)
            else:
                sink.record(event)
        except ChildAgentSupervisorError as exc:
            if exc.code in {"child_event_store_unavailable", "child_event_store_contention"}:
                self._persistence_uncertain = True
            raise
        except Exception:
            # A lost acknowledgement is indistinguishable from a failed
            # append. Do not use the stale memory view for further mutations.
            self._persistence_uncertain = True
            raise
        if sink is not self._events:
            self._events.record(event)

    def _assert_persistence(self) -> None:
        if self._persistence_uncertain:
            raise ChildAgentSupervisorError(
                "child lifecycle commit requires durable recovery",
                code="child_event_store_recovery_required",
            )


def _find_forbidden_keys(value: Any, forbidden: frozenset[str], *, path: str = "$") -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).casefold()
            if normalized in forbidden:
                found.add(path + "." + str(key))
            found.update(_find_forbidden_keys(child, forbidden, path=path + "." + str(key)))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            found.update(_find_forbidden_keys(child, forbidden, path=f"{path}[{index}]"))
    return found


def _event_handle_contract(event: Mapping[str, Any]) -> ChildAgentHandle | None:
    """Validate and restore a versioned handle contract when one is present."""
    marker_keys = {"handle_contract", "handle_schema_version", "handle_checksum"}
    if not marker_keys.intersection(event):
        return None
    raw_contract = event.get("handle_contract")
    if not isinstance(raw_contract, Mapping):
        raise ValueError("child event handle contract must be an object")
    expected_keys = {"schema_version", "canonical_payload", "handle_checksum"}
    if set(raw_contract) != expected_keys:
        raise ValueError("child event handle contract fields are invalid")
    schema_version = raw_contract.get("schema_version")
    if schema_version != CHILD_AGENT_HANDLE_SCHEMA_VERSION:
        raise ValueError("unsupported child handle contract schema")
    if event.get("handle_schema_version") != schema_version:
        raise ValueError("child event handle schema drift")
    supplied_checksum = raw_contract.get("handle_checksum")
    if event.get("handle_checksum") != supplied_checksum:
        raise ValueError("child event handle checksum drift")
    if not isinstance(supplied_checksum, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", supplied_checksum) is None:
        raise ValueError("child event handle checksum is invalid")
    payload = raw_contract.get("canonical_payload")
    if not isinstance(payload, Mapping):
        raise ValueError("child event canonical handle payload must be an object")
    if _checksum(dict(payload)) != supplied_checksum:
        raise ValueError("child event handle checksum does not match payload")
    handle = ChildAgentHandle.from_dict(payload)
    if handle.to_dict() != dict(payload):
        raise ValueError("child event canonical handle payload is not canonical")
    # A contract is canonical for the event, so duplicated lifecycle fields
    # must agree with it exactly. This catches identity/policy drift even when
    # an attacker recomputes the payload checksum.
    expected = {
        "child_id": handle.child_id,
        "operation_id": handle.operation_id,
        "state": handle.state.value,
        "parent_graph_identity": handle.parent_graph_identity.to_dict(),
        "child_graph_identity": handle.child_graph_identity.to_dict() if handle.child_graph_identity else None,
        "stage_id": handle.stage_id,
        "task_id": handle.task_id,
        "task_instance_id": handle.task_instance_id,
        "attempt": handle.attempt,
        "lease_id": handle.lease.lease_id,
        "lease_issued_at": handle.lease.issued_at.isoformat(),
        "lease_expires_at": handle.lease.expires_at.isoformat(),
        "heartbeat_seq": handle.lease.heartbeat_seq,
        "allowed_tools": list(handle.allowed_tools),
        "allowed_memory_namespaces": list(handle.allowed_memory_namespaces),
        "budget": dict(handle.budget),
        "transcript_ref": handle.transcript_ref,
    }
    for field_name, expected_value in expected.items():
        if event.get(field_name) != expected_value:
            raise ValueError(f"child event field {field_name} disagrees with handle contract")
    return handle


def _handle_from_event(event: Mapping[str, Any]) -> ChildAgentHandle:
    contracted = _event_handle_contract(event)
    if contracted is not None:
        return contracted
    lease = event.get("lease") or {
        "lease_id": event["lease_id"],
        "issued_at": event.get("lease_issued_at") or event["occurred_at"],
        "expires_at": event["lease_expires_at"],
        "heartbeat_seq": event.get("heartbeat_seq", 0),
    }
    return ChildAgentHandle(
        child_id=event["child_id"],
        parent_graph_identity=event["parent_graph_identity"],
        child_graph_identity=event.get("child_graph_identity"),
        stage_id=event["stage_id"],
        task_id=event["task_id"],
        task_instance_id=event["task_instance_id"],
        attempt=event["attempt"],
        allowed_tools=tuple(event.get("allowed_tools", ("unknown",))),
        allowed_memory_namespaces=tuple(event.get("allowed_memory_namespaces", ("unknown",))),
        budget=event.get("budget", {}),
        transcript_ref=event.get("transcript_ref"),
        operation_id=event["operation_id"],
        state=event.get("state", ChildAgentState.STARTING),
        lease=ChildAgentLease(
            lease_id=lease["lease_id"],
            issued_at=datetime.fromisoformat(lease["issued_at"]),
            expires_at=datetime.fromisoformat(lease["expires_at"]),
            heartbeat_seq=lease.get("heartbeat_seq", 0),
        ),
        created_at=datetime.fromisoformat(event["occurred_at"]),
        updated_at=datetime.fromisoformat(event["occurred_at"]),
    )


def _terminal_receipt_from_dict(value: Mapping[str, Any]) -> ChildAgentTerminalReceipt:
    payload = dict(value)
    supplied_checksum = payload.pop("receipt_checksum", None)
    completed_at = payload.get("completed_at")
    if isinstance(completed_at, str):
        payload["completed_at"] = datetime.fromisoformat(completed_at)
    receipt = ChildAgentTerminalReceipt(**payload)
    if supplied_checksum != receipt.receipt_checksum:
        raise ValueError("terminal receipt checksum does not match canonical projection")
    return receipt


def _event_time(event: Mapping[str, Any]) -> datetime:
    raw = event.get("occurred_at")
    if not isinstance(raw, str):
        raise ValueError("lifecycle event occurred_at is required")
    return _utc(datetime.fromisoformat(raw), "occurred_at")


def _event_sort_key(event: Mapping[str, Any]) -> datetime:
    try:
        return _event_time(event)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=UTC)


__all__ = [
    "CHILD_AGENT_HANDLE_SCHEMA_VERSION",
    "ChildAgentAdmissionError",
    "ChildAgentEventSink",
    "ChildAgentEventReader",
    "ChildAgentHandle",
    "ChildAgentHeartbeat",
    "ChildAgentLease",
    "ChildAgentNotFoundError",
    "ChildAgentOperationConflict",
    "ChildAgentOperationResult",
    "ChildAgentSpawnRequest",
    "ChildAgentState",
    "ChildAgentSupervisor",
    "ChildAgentSupervisorError",
    "ChildAgentTerminalReceipt",
    "ChildAgentWorker",
    "InMemoryChildAgentEventLog",
    "TERMINAL_CHILD_STATES",
]
