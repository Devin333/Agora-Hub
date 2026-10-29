from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import (
    RAGBudget,
    RAGBudgetSnapshot,
    RAGSessionSpec,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
)
from framework.shared.json import to_jsonable


EVIDENCE_TYPE_COVERAGE_REQUIRED = "required"
EVIDENCE_TYPE_COVERAGE_ADVISORY = "advisory"
EVIDENCE_TYPE_COVERAGE_MODES = (EVIDENCE_TYPE_COVERAGE_REQUIRED, EVIDENCE_TYPE_COVERAGE_ADVISORY)


class RAGDecisionType(StrEnum):
    CONTINUE_RETRIEVAL = "continue_retrieval"
    REPLAN_QUERIES = "replan_queries"
    READ_MORE_SOURCES = "read_more_sources"
    ASSEMBLE_CONTEXT = "assemble_context"
    RETURN_CONTEXT_PACK = "return_context_pack"
    RETURN_ANSWER = "return_answer"
    ABSTAIN = "abstain"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    WAIT_FOR_APPROVAL = "wait_for_approval"
    HALTED = "halted"
    FAILED = "failed"


@dataclass(frozen=True)
class RAGDecision:
    decision_type: RAGDecisionType | str
    reason: str
    gate_results: tuple[dict[str, Any], ...] = ()
    budget_snapshot: RAGBudgetSnapshot | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.reason).strip():
            raise HarnessValidationError("RAG decision reason is required")
        object.__setattr__(self, "decision_type", RAGDecisionType(self.decision_type))
        object.__setattr__(self, "gate_results", tuple(dict(item) for item in self.gate_results))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_type": self.decision_type.value,
            "reason": self.reason,
            "gate_results": to_jsonable(list(self.gate_results)),
            "budget_snapshot": self.budget_snapshot.to_dict() if self.budget_snapshot else None,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True)
class RAGExecutionPolicy:
    allowed_corpora: tuple[str, ...]
    allowed_memory_namespaces: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budget: RAGBudget
    source_policy: dict[str, Any] = field(default_factory=dict)
    context_policy: dict[str, Any] = field(default_factory=dict)
    generation_policy: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.allowed_corpora:
            raise HarnessValidationError("allowed_corpora must be declared")
        if not self.allowed_memory_namespaces:
            raise HarnessValidationError("allowed_memory_namespaces must be declared")
        if not self.allowed_tools:
            raise HarnessValidationError("allowed_tools must be declared")
        if not isinstance(self.budget, RAGBudget):
            raise HarnessValidationError("budget must be RAGBudget")
        object.__setattr__(self, "allowed_corpora", tuple(str(item) for item in self.allowed_corpora))
        object.__setattr__(
            self,
            "allowed_memory_namespaces",
            tuple(str(item) for item in self.allowed_memory_namespaces),
        )
        object.__setattr__(self, "allowed_tools", tuple(str(item) for item in self.allowed_tools))
        object.__setattr__(self, "source_policy", dict(self.source_policy))
        object.__setattr__(self, "context_policy", dict(self.context_policy))
        object.__setattr__(self, "generation_policy", dict(self.generation_policy))

    @classmethod
    def from_session_spec(cls, spec: RAGSessionSpec) -> "RAGExecutionPolicy":
        return cls(
            allowed_corpora=spec.allowed_corpora,
            allowed_memory_namespaces=spec.allowed_memory_namespaces,
            allowed_tools=spec.allowed_tools,
            budget=spec.budget,
            source_policy=spec.source_policy,
            context_policy=spec.context_policy,
            generation_policy=spec.generation_policy,
        )

    @property
    def generation_enabled(self) -> bool:
        return bool(self.generation_policy.get("enabled", False))

    @property
    def evidence_type_coverage(self) -> str:
        """``required`` blocks on a missing evidence type; ``advisory`` lets the answer worker judge it."""
        mode = str(self.source_policy.get("evidence_type_coverage") or EVIDENCE_TYPE_COVERAGE_REQUIRED)
        if mode not in EVIDENCE_TYPE_COVERAGE_MODES:
            raise HarnessValidationError(
                "evidence_type_coverage must be one of " + ", ".join(EVIDENCE_TYPE_COVERAGE_MODES),
                details={"evidence_type_coverage": mode},
            )
        return mode

    @property
    def max_generation_attempts(self) -> int:
        return max(1, int(self.generation_policy.get("max_attempts", 1)))

    @property
    def max_supplemental_rounds(self) -> int:
        default = 1 if self.generation_enabled and self.max_generation_attempts > 1 else 0
        return max(0, int(self.generation_policy.get("max_supplemental_rounds", default)))

    def allows_step(self, step: RetrievalStepSpec) -> bool:
        if step.operation == RetrievalOperation.SEARCH_CORPUS:
            return step.corpus in self.allowed_corpora
        if step.operation == RetrievalOperation.READ_SOURCE:
            requested_tool = step.metadata.get("tool_name", "retrieval.read_source")
            return str(requested_tool) in self.allowed_tools
        if step.operation == RetrievalOperation.RECALL_MEMORY:
            return step.memory_namespace in self.allowed_memory_namespaces
        if step.operation in {
            RetrievalOperation.VERIFY_SOURCE,
            RetrievalOperation.CHECK_GAP,
            RetrievalOperation.ASSEMBLE_CONTEXT,
        }:
            return True
        return False

    def projected_usage(self, plan: RetrievalPlanCandidate) -> RAGBudgetSnapshot:
        queries = sum(1 for step in plan.steps if step.operation == RetrievalOperation.SEARCH_CORPUS)
        source_reads = sum(
            max(step.max_source_reads, len(step.source_refs))
            for step in plan.steps
            if step.operation == RetrievalOperation.READ_SOURCE
        )
        memory_hits = sum(
            step.max_results
            for step in plan.steps
            if step.operation == RetrievalOperation.RECALL_MEMORY
        )
        worker_calls = len(plan.steps)
        return RAGBudgetSnapshot(
            queries_used=queries,
            source_reads_used=source_reads,
            memory_hits_used=memory_hits,
            worker_calls_used=worker_calls,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_corpora": list(self.allowed_corpora),
            "allowed_memory_namespaces": list(self.allowed_memory_namespaces),
            "allowed_tools": list(self.allowed_tools),
            "budget": self.budget.to_dict(),
            "source_policy": to_jsonable(self.source_policy),
            "context_policy": to_jsonable(self.context_policy),
            "generation_policy": to_jsonable(self.generation_policy),
        }


def normalize_query(query: str | None) -> str:
    return " ".join(str(query or "").strip().casefold().split())


def budget_exceeded(snapshot: RAGBudgetSnapshot, budget: RAGBudget) -> dict[str, dict[str, int]]:
    violations: dict[str, dict[str, int]] = {}
    checks = (
        ("rounds", snapshot.rounds_used, budget.max_rounds),
        ("replans", snapshot.replans_used, budget.max_replans),
        ("queries", snapshot.queries_used, budget.max_queries),
        ("source_reads", snapshot.source_reads_used, budget.max_source_reads),
        ("memory_hits", snapshot.memory_hits_used, budget.max_memory_hits),
        ("context_items", snapshot.context_items_used, budget.max_context_items),
        ("context_tokens", snapshot.context_tokens_used, budget.max_context_tokens),
        ("worker_calls", snapshot.worker_calls_used, budget.max_worker_calls),
    )
    for name, used, limit in checks:
        if used > limit:
            violations[name] = {"used": used, "max": limit}
    return violations


__all__ = [
    "EVIDENCE_TYPE_COVERAGE_REQUIRED",
    "EVIDENCE_TYPE_COVERAGE_MODES",
    "EVIDENCE_TYPE_COVERAGE_ADVISORY",
    "RAGDecision",
    "RAGDecisionType",
    "RAGExecutionPolicy",
    "budget_exceeded",
    "normalize_query",
]
