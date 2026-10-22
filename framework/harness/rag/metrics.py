from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import RAGBudgetSnapshot, RAGSessionStatus
from framework.harness.rag.policy import RAGDecision


@dataclass(frozen=True)
class RAGSessionMetrics:
    status: RAGSessionStatus | str
    decision_type: str
    transcript_event_count: int
    budget_snapshot: dict[str, int]
    accepted_evidence_count: int = 0
    rejected_evidence_count: int = 0
    conflicting_evidence_count: int = 0
    memory_context_count: int = 0
    artifact_ref_count: int = 0
    answer_present: bool = False
    answer_abstained: bool = False
    answer_attempts: int = 0
    answer_generation_failures: int = 0
    supplemental_rounds_started: int = 0
    supplemental_rounds_completed: int = 0
    supplemental_rounds_failed: int = 0
    supplemental_rounds_skipped: int = 0
    supplemental_round_skip_reasons: dict[str, int] = field(default_factory=dict)
    gate_failures_count: int = 0
    gate_failures_by_gate: dict[str, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", RAGSessionStatus(self.status))
        object.__setattr__(self, "decision_type", str(self.decision_type))
        object.__setattr__(self, "budget_snapshot", dict(self.budget_snapshot))
        object.__setattr__(
            self,
            "supplemental_round_skip_reasons",
            dict(self.supplemental_round_skip_reasons),
        )
        object.__setattr__(self, "gate_failures_by_gate", dict(self.gate_failures_by_gate))
        object.__setattr__(self, "rejection_reasons", dict(self.rejection_reasons))
        for field_name in (
            "transcript_event_count",
            "accepted_evidence_count",
            "rejected_evidence_count",
            "conflicting_evidence_count",
            "memory_context_count",
            "artifact_ref_count",
            "answer_attempts",
            "answer_generation_failures",
            "supplemental_rounds_started",
            "supplemental_rounds_completed",
            "supplemental_rounds_failed",
            "supplemental_rounds_skipped",
            "gate_failures_count",
        ):
            value = getattr(self, field_name)
            if int(value) < 0:
                raise HarnessValidationError(f"{field_name} must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "decision_type": self.decision_type,
            "transcript_event_count": self.transcript_event_count,
            "budget_snapshot": dict(self.budget_snapshot),
            "accepted_evidence_count": self.accepted_evidence_count,
            "rejected_evidence_count": self.rejected_evidence_count,
            "conflicting_evidence_count": self.conflicting_evidence_count,
            "memory_context_count": self.memory_context_count,
            "artifact_ref_count": self.artifact_ref_count,
            "answer_present": self.answer_present,
            "answer_abstained": self.answer_abstained,
            "answer_attempts": self.answer_attempts,
            "answer_generation_failures": self.answer_generation_failures,
            "supplemental_rounds_started": self.supplemental_rounds_started,
            "supplemental_rounds_completed": self.supplemental_rounds_completed,
            "supplemental_rounds_failed": self.supplemental_rounds_failed,
            "supplemental_rounds_skipped": self.supplemental_rounds_skipped,
            "supplemental_round_skip_reasons": dict(self.supplemental_round_skip_reasons),
            "gate_failures_count": self.gate_failures_count,
            "gate_failures_by_gate": dict(self.gate_failures_by_gate),
            "rejection_reasons": dict(self.rejection_reasons),
        }


def build_rag_session_metrics(
    *,
    status: RAGSessionStatus,
    decision: RAGDecision,
    events: tuple[dict[str, Any], ...],
    budget_snapshot: RAGBudgetSnapshot,
    accepted_evidence: tuple[Any, ...],
    rejected_evidence: tuple[Any, ...],
    conflicting_evidence: tuple[Any, ...],
    memory_context: tuple[dict[str, Any], ...],
    artifact_refs: tuple[str, ...],
    answer: Any | None,
) -> RAGSessionMetrics:
    event_types = [str(event.get("event_type") or "") for event in events]
    gate_failures_by_gate = _gate_failures_by_gate(events)
    return RAGSessionMetrics(
        status=status,
        decision_type=decision.decision_type.value,
        transcript_event_count=len(events),
        budget_snapshot=budget_snapshot.to_dict(),
        accepted_evidence_count=len(accepted_evidence),
        rejected_evidence_count=len(rejected_evidence),
        conflicting_evidence_count=len(conflicting_evidence),
        memory_context_count=len(memory_context),
        artifact_ref_count=len(artifact_refs),
        answer_present=answer is not None,
        answer_abstained=bool(getattr(answer, "abstained", False)) if answer is not None else False,
        answer_attempts=event_types.count("rag_answer_candidate_created"),
        answer_generation_failures=event_types.count("rag_answer_generation_failed"),
        supplemental_rounds_started=event_types.count("rag_answer_supplemental_round_started"),
        supplemental_rounds_completed=event_types.count("rag_answer_supplemental_round_completed"),
        supplemental_rounds_failed=event_types.count("rag_answer_supplemental_round_failed"),
        supplemental_rounds_skipped=event_types.count("rag_answer_supplemental_round_skipped"),
        supplemental_round_skip_reasons=_supplemental_round_skip_reasons(events),
        gate_failures_count=sum(gate_failures_by_gate.values()),
        gate_failures_by_gate=gate_failures_by_gate,
        rejection_reasons=_rejection_reasons(rejected_evidence),
    )


def _gate_failures_by_gate(events: tuple[dict[str, Any], ...]) -> dict[str, int]:
    failures: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if event_type == "rag_gate_failed":
            _increment(failures, _gate_name(payload))
            continue
        if event_type == "rag_answer_verified":
            for result in payload.get("gate_results") or ():
                if isinstance(result, dict) and result.get("passed") is False:
                    _increment(failures, _gate_name(result))
    return failures


def _rejection_reasons(rejected_evidence: tuple[Any, ...]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for item in rejected_evidence:
        metadata = getattr(item, "metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}
        reason = str(metadata.get("rejection_reason") or "source_verification_failed")
        _increment(reasons, reason)
    return reasons


def _supplemental_round_skip_reasons(events: tuple[dict[str, Any], ...]) -> dict[str, int]:
    reasons: dict[str, int] = {}
    for event in events:
        if str(event.get("event_type") or "") != "rag_answer_supplemental_round_skipped":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        reason_code = str(payload.get("reason_code") or "unknown")
        _increment(reasons, reason_code)
    return reasons


def _gate_name(payload: dict[str, Any]) -> str:
    return str(payload.get("gate") or payload.get("gate_name") or "unknown")


def _increment(values: dict[str, int], key: str) -> None:
    values[key] = values.get(key, 0) + 1


__all__ = ["RAGSessionMetrics", "build_rag_session_metrics"]
