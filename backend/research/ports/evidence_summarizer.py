"""Port for question-conditioned evidence summarization before answering (RCS).

A summarizer reads retrieved passages for one question and returns, per passage, a
relevance score, a short question-focused summary, and verbatim supporting quotes.
The answer generator uses the scores to choose how many passages to keep and the
summaries to compress them; citations still point at the original passages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from framework.llm.models.usage import TokenUsage
from framework.shared.graph_identity import GraphExecutionIdentity

EVIDENCE_RELEVANCE_MIN = 0
EVIDENCE_RELEVANCE_MAX = 10


@dataclass(frozen=True)
class EvidencePassage:
    passage_id: str
    text: str

    def __post_init__(self) -> None:
        if not str(self.passage_id).strip():
            raise ValueError("passage_id is required")


@dataclass(frozen=True)
class EvidenceSummaryRequest:
    question: str
    passages: tuple[EvidencePassage, ...]
    execution_identity: GraphExecutionIdentity | None = None

    def __post_init__(self) -> None:
        if not str(self.question).strip():
            raise ValueError("question is required")
        ids = [passage.passage_id for passage in self.passages]
        if len(set(ids)) != len(ids):
            raise ValueError("passage ids must be unique")


@dataclass(frozen=True)
class PassageSummary:
    passage_id: str
    # None when the passage could not be assessed (for example a malformed batch
    # reply); such passages keep their retrieval order and original text.
    relevance: int | None
    summary: str = ""
    quotes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.relevance is not None and (
            type(self.relevance) is not int
            or not EVIDENCE_RELEVANCE_MIN <= self.relevance <= EVIDENCE_RELEVANCE_MAX
        ):
            raise ValueError(
                f"relevance must be an integer in [{EVIDENCE_RELEVANCE_MIN}, {EVIDENCE_RELEVANCE_MAX}]"
            )

    @property
    def assessed(self) -> bool:
        return self.relevance is not None


@dataclass(frozen=True)
class EvidenceSummaryResult:
    summaries: tuple[PassageSummary, ...]
    model: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def by_passage_id(self) -> dict[str, PassageSummary]:
        return {summary.passage_id: summary for summary in self.summaries}


@runtime_checkable
class EvidenceSummarizerPort(Protocol):
    @property
    def measurement_identity(self) -> Mapping[str, Any]:
        """Stable non-secret identity (prompt version, model, batching) for evaluation."""
        ...

    async def summarize(self, request: EvidenceSummaryRequest) -> EvidenceSummaryResult:
        """Return one summary per request passage, in request order.

        Transport failures raise ``PaperAnswerModelError``; unusable model output for
        some passages is reported as unassessed summaries, never silently dropped.
        """
        ...


__all__ = [
    "EVIDENCE_RELEVANCE_MAX",
    "EVIDENCE_RELEVANCE_MIN",
    "EvidencePassage",
    "EvidenceSummarizerPort",
    "EvidenceSummaryRequest",
    "EvidenceSummaryResult",
    "PassageSummary",
]
