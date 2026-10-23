from __future__ import annotations

from dataclasses import dataclass

from framework.harness.context.models import ContextBudget, ContextEnvelope


@dataclass(frozen=True)
class ContextBudgetUsage:
    input_tokens: int
    output_tokens: int
    context_segments: int
    evidence_items: int
    memory_items: int
    artifact_refs: int

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "context_segments": self.context_segments,
            "evidence_items": self.evidence_items,
            "memory_items": self.memory_items,
            "artifact_refs": self.artifact_refs,
        }


class ContextBudgetEstimator:
    def estimate(self, envelope: ContextEnvelope) -> ContextBudgetUsage:
        # The evidence-memory segment is the carrier of the evidence items, not an item
        # itself; its cost is already charged to input tokens and context segments.
        return ContextBudgetUsage(
            input_tokens=envelope.token_estimate or sum(segment.token_estimate for segment in envelope.segments),
            output_tokens=envelope.budget.reserved_output_tokens if envelope.budget else 0,
            context_segments=len(envelope.segments),
            evidence_items=len(set(envelope.evidence_refs)),
            memory_items=len(envelope.memory_refs),
            artifact_refs=len(envelope.artifact_refs),
        )

    def is_over_budget(self, envelope: ContextEnvelope, budget: ContextBudget | None = None) -> bool:
        actual_budget = budget or envelope.budget or ContextBudget.safe_default()
        usage = self.estimate(envelope)
        return (
            usage.input_tokens > actual_budget.max_input_tokens
            or usage.context_segments > actual_budget.max_context_segments
            or usage.evidence_items > actual_budget.max_evidence_items
            or usage.memory_items > actual_budget.max_memory_items
            or usage.artifact_refs > actual_budget.max_artifact_refs
        )


__all__ = ["ContextBudgetEstimator", "ContextBudgetUsage"]
