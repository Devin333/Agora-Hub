"""Business-owned durable journal contract for Research evaluation bookkeeping.

The port is deliberately storage-agnostic. Implementations provide one atomic,
optimistic append operation and a replayable ordered read; Research code never
imports an interface or filesystem implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class EvaluationJournalEvent:
    sequence: int
    event_type: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("journal event sequence must be a non-negative integer")
        if type(self.event_type) is not str or not self.event_type.strip():
            raise ValueError("journal event_type must be a non-empty string")
        if not isinstance(self.payload, Mapping):
            raise TypeError("journal event payload must be a mapping")


@dataclass(frozen=True, slots=True)
class EvaluationJournal:
    session_id: str
    events: tuple[EvaluationJournalEvent, ...]

    def __post_init__(self) -> None:
        if type(self.session_id) is not str or not self.session_id.strip():
            raise ValueError("journal session_id must be a non-empty string")
        for expected, event in enumerate(self.events):
            if event.sequence != expected:
                raise ValueError("journal events must have contiguous sequence numbers")

    @property
    def next_sequence(self) -> int:
        return len(self.events)


class EvaluationJournalConflict(RuntimeError):
    """The caller appended against a stale journal sequence."""

    def __init__(self, *, expected_sequence: int, actual_sequence: int) -> None:
        self.expected_sequence = expected_sequence
        self.actual_sequence = actual_sequence
        super().__init__(
            f"evaluation journal sequence conflict: expected {expected_sequence}, actual {actual_sequence}"
        )


class EvaluationJournalPort(Protocol):
    """Atomic append-only journal used by the shared evaluation budget ledger."""

    def read(self) -> EvaluationJournal:
        """Return the complete actor-scoped journal in sequence order."""

    def append(
        self,
        *,
        expected_sequence: int,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> EvaluationJournalEvent:
        """Atomically append only when ``expected_sequence`` is current.

        Implementations may return an already-present byte-for-byte equivalent
        event for idempotent retry. Different content at the same sequence must
        raise :class:`EvaluationJournalConflict`.
        """


__all__ = [
    "EvaluationJournal",
    "EvaluationJournalConflict",
    "EvaluationJournalEvent",
    "EvaluationJournalPort",
]
