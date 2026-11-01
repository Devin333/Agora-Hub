"""Fault-injecting stand-ins for the stores and services tools depend on.

Each store keeps real in-memory state (wrapping the project's own in-memory
implementations where one exists) and routes every operation through a
``FaultPlan``. A plan scripts faults per method by call order and repeats the
last entry, so ``plan.script("save_report", Raise(OSError()), Pass())`` fails
the first call only.

``RaiseAfter`` applies the operation and then raises: the write landed but the
caller never saw the acknowledgement. That is the case retry and idempotency
handling must survive.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar, Union

from framework.memory.models import MemoryQuery, MemoryRecallResult
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.workers.approval.store import InMemoryApprovalStore
from framework.workers.queue.in_memory import InMemoryTaskQueue
from infrastructure.storage.records import ReportSummaryRecord
from infrastructure.storage.vector.fake_store import InMemoryVectorStore

MAX_STALL_SECONDS = 30.0
T = TypeVar("T")


@dataclass(frozen=True)
class Pass:
    """Run the operation normally."""


@dataclass(frozen=True)
class Raise:
    """Fail before the operation touches any state."""

    error: BaseException


@dataclass(frozen=True)
class RaiseAfter:
    """Apply the operation, then lose the acknowledgement."""

    error: BaseException


@dataclass(frozen=True)
class Stall:
    """Block until released (or ``seconds`` pass), then apply ``then``."""

    seconds: float = MAX_STALL_SECONDS
    then: "Fault" = field(default_factory=Pass)


Fault = Union[Pass, Raise, RaiseAfter, Stall]


class FaultPlan:
    def __init__(self) -> None:
        self._scripts: dict[str, deque[Fault]] = {}
        self._calls: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._released = threading.Event()

    def script(self, method: str, *faults: Fault) -> "FaultPlan":
        if not faults:
            raise ValueError("a fault script needs at least one entry")
        with self._lock:
            self._scripts[method] = deque(faults)
        return self

    def calls(self, method: str) -> int:
        with self._lock:
            return self._calls[method]

    def release(self) -> None:
        self._released.set()

    def run(self, method: str, operation: Callable[[], T], *, key: str | None = None) -> T:
        """Apply the script for ``method:key`` if one exists, else for ``method``."""

        with self._lock:
            self._calls[method] += 1
            script = self._scripts.get(f"{method}:{key}") if key is not None else None
            if not script:
                script = self._scripts.get(method)
            if not script:
                fault: Fault = Pass()
            else:
                fault = script.popleft() if len(script) > 1 else script[0]
        return self._apply(fault, operation)

    def _apply(self, fault: Fault, operation: Callable[[], T]) -> T:
        if isinstance(fault, Pass):
            return operation()
        if isinstance(fault, Raise):
            raise _fresh(fault.error)
        if isinstance(fault, RaiseAfter):
            operation()
            raise _fresh(fault.error)
        if isinstance(fault, Stall):
            self._released.wait(min(fault.seconds, MAX_STALL_SECONDS))
            return self._apply(fault.then, operation)
        raise TypeError(f"unknown fault: {fault!r}")  # pragma: no cover


def _fresh(error: BaseException) -> BaseException:
    """One scripted error may be raised hundreds of times; re-raising an
    instance prepends to its old traceback, so each raise starts clean, the way
    a real client raises a new error every time."""

    return error.with_traceback(None)


class FaultyReportRepository:
    """Report and source-health persistence with upsert-by-key semantics.

    Mirrors ``INSERT ... ON CONFLICT (key) DO UPDATE``: the last write for a key
    wins, and ``write_log`` keeps every accepted write in order so tests can
    tell an overwrite from a duplicate.
    """

    def __init__(self, plan: FaultPlan | None = None) -> None:
        self.plan = plan or FaultPlan()
        self._lock = threading.Lock()
        self.reports: dict[str, Any] = {}
        self.source_health: dict[str, Any] = {}
        self.write_log: list[tuple[str, str]] = []

    def save_report(self, record: Any) -> None:
        self.plan.run("save_report", lambda: self._store_report(record))

    def update_source_health(self, health: Any) -> None:
        self.plan.run("update_source_health", lambda: self._store_health(health))

    def search_reports(self, query: str, *, limit: int = 20) -> list[ReportSummaryRecord]:
        return self.plan.run("search_reports", lambda: self._search(query, limit))

    def _store_report(self, record: Any) -> None:
        with self._lock:
            self.reports[str(record.report_id)] = record
            self.write_log.append(("report", str(record.report_id)))

    def _store_health(self, health: Any) -> None:
        with self._lock:
            self.source_health[str(health.source_id)] = health
            self.write_log.append(("source_health", str(health.source_id)))

    def _search(self, query: str, limit: int) -> list[ReportSummaryRecord]:
        needle = query.casefold()
        with self._lock:
            matches = [
                record
                for record in self.reports.values()
                if needle in str(record.title or "").casefold()
            ]
        return [
            ReportSummaryRecord(
                report_id=str(record.report_id),
                run_id=str(record.run_id),
                status=str(record.status),
                finished_at="",
                title=record.title,
                quality_score=record.quality_score,
                manifest_path=record.manifest_path,
            )
            for record in matches[:limit]
        ]


class FaultySourceHealthStore:
    """``SourceHealthStore`` behind ``BasicSourceHealthManager`` (Postgres in production)."""

    def __init__(self, plan: FaultPlan | None = None) -> None:
        self.plan = plan or FaultPlan()
        self._lock = threading.Lock()
        self.records: dict[str, Any] = {}

    def get_source_health(self, source_id: str) -> Any:
        return self.plan.run("get_source_health", lambda: self._get(source_id), key=source_id)

    def update_source_health(self, health: Any) -> None:
        self.plan.run("update_source_health", lambda: self._put(health), key=str(health.source_id))

    def _get(self, source_id: str) -> Any:
        with self._lock:
            return self.records.get(source_id)

    def _put(self, health: Any) -> None:
        with self._lock:
            self.records[str(health.source_id)] = health


class FaultyVectorStore:
    """Qdrant search/upsert over the project's in-memory vector store."""

    def __init__(self, plan: FaultPlan | None = None) -> None:
        self.plan = plan or FaultPlan()
        self.inner = InMemoryVectorStore()

    def search(self, query: Any) -> list[Any]:
        return self.plan.run("search", lambda: self.inner.search(query))

    def upsert_documents(self, docs: list[Any]) -> None:
        self.plan.run("upsert_documents", lambda: self.inner.upsert_documents(docs))


class FaultyMemoryRecall:
    """Execution-bound read-only recall port (``ExecutionMemoryRecallPort``)."""

    def __init__(
        self,
        execution_identity: GraphExecutionIdentity,
        plan: FaultPlan | None = None,
    ) -> None:
        self.plan = plan or FaultPlan()
        self._execution_identity = execution_identity
        self.queries: list[dict[str, Any]] = []

    @property
    def execution_identity(self) -> GraphExecutionIdentity:
        return self._execution_identity

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None:
        if execution_identity != self._execution_identity:
            raise ValueError("memory caller conflicts with the bound Graph identity")

    def recall(self, query: Any, *, policy: Any = None) -> MemoryRecallResult:
        return self.plan.run("recall", lambda: self._recall(query))

    def _recall(self, query: Any) -> MemoryRecallResult:
        payload = dict(query) if isinstance(query, dict) else {"query": str(query)}
        self.queries.append(payload)
        return MemoryRecallResult(query=MemoryQuery(query=str(payload.get("query") or "")))


class FaultyApprovalStore:
    """Approval persistence; only the write used by control tools is faulted."""

    def __init__(self, plan: FaultPlan | None = None) -> None:
        self.plan = plan or FaultPlan()
        self.inner = InMemoryApprovalStore()

    def upsert_approval(self, request: Any) -> Any:
        return self.plan.run("upsert_approval", lambda: self.inner.upsert_approval(request))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class FaultyTaskQueue:
    """Worker queue; only ``enqueue`` (used by delegation) is faulted."""

    def __init__(self, plan: FaultPlan | None = None) -> None:
        self.plan = plan or FaultPlan()
        self.inner = InMemoryTaskQueue()

    def enqueue(self, task: Any) -> Any:
        return self.plan.run("enqueue", lambda: self.inner.enqueue(task))

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)
