"""Native durable-journal adapter for Research evaluation bookkeeping.

This adapter intentionally delegates persistence, actor scoping, hashing,
locking, checksums, atomic writes and contiguous sequencing to
``PaperRagTranscriptFileStore``. It does not define another filesystem schema.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any, Mapping

from backend.research.ports.evaluation_journal import (
    EvaluationJournal,
    EvaluationJournalConflict,
    EvaluationJournalEvent,
    EvaluationJournalPort,
)
from framework.shared.json import to_jsonable
from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore


class PaperRagEvaluationJournal(EvaluationJournalPort):
    """Evaluation bookkeeping namespace backed by the native RAG journal."""

    _SESSION_PREFIX = "research-evaluation-bookkeeping"

    def __init__(
        self,
        store: PaperRagTranscriptFileStore,
        *,
        run_id: str,
        tenant_id: str,
        user_id: str,
    ) -> None:
        for field_name, value in (("run_id", run_id), ("tenant_id", tenant_id), ("user_id", user_id)):
            if type(value) is not str or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} must be a non-empty normalized string")
        self._store = store
        self._run_id = run_id
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        self._session_id = f"{self._SESSION_PREFIX}:{digest}"
        self._actor_scope = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "memory_namespace": f"research:evaluation:bookkeeping:{digest}",
        }
        self._spec = SimpleNamespace(session_id=self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def actor_scope(self) -> Mapping[str, str]:
        return dict(self._actor_scope)

    def read(self) -> EvaluationJournal:
        value = self._store.load_journal(self._session_id, actor_scope=self._actor_scope)
        if value is None:
            return EvaluationJournal(session_id=self._session_id, events=())
        events = tuple(
            EvaluationJournalEvent(
                sequence=int(item["sequence"]),
                event_type=str(item["event_type"]),
                payload=dict(item["payload"]),
            )
            for item in value["events"]
        )
        return EvaluationJournal(session_id=self._session_id, events=events)

    def append(
        self,
        *,
        expected_sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> EvaluationJournalEvent:
        if type(expected_sequence) is not int or expected_sequence < 0:
            raise ValueError("expected_sequence must be a non-negative integer")
        normalized = EvaluationJournalEvent(
            sequence=expected_sequence,
            event_type=event_type,
            payload=to_jsonable(dict(payload)),
        )
        before = self.read()
        if before.next_sequence != expected_sequence:
            return self._existing_or_conflict(normalized, before)
        try:
            self._store.record_event(
                spec=self._spec,
                sequence=expected_sequence,
                event_type=normalized.event_type,
                payload=normalized.payload,
                actor_scope=self._actor_scope,
            )
        except ValueError as error:
            # The native lock performs the real compare-and-append. A competing
            # writer may win after our read; reload to turn that race into the
            # business port's optimistic conflict without replacing native IO.
            after = self.read()
            if after.next_sequence != expected_sequence:
                return self._existing_or_conflict(normalized, after)
            raise error
        return normalized

    @staticmethod
    def _existing_or_conflict(
        requested: EvaluationJournalEvent,
        journal: EvaluationJournal,
    ) -> EvaluationJournalEvent:
        if journal.next_sequence > requested.sequence:
            existing = journal.events[requested.sequence]
            if existing.event_type == requested.event_type and dict(existing.payload) == dict(requested.payload):
                return existing
        raise EvaluationJournalConflict(
            expected_sequence=requested.sequence,
            actual_sequence=journal.next_sequence,
        )


__all__ = ["PaperRagEvaluationJournal"]
