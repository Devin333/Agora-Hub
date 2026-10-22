from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import (
    EvidenceCandidate,
    FORBIDDEN_RAG_PLAN_KEYS,
    RAGBudgetSnapshot,
    RAGContextPack,
    RAGSessionSpec,
    RetrievalOperation,
    RetrievalPlanCandidate,
    present_evidence_types,
)
from framework.harness.rag.policy import RAGExecutionPolicy, budget_exceeded, normalize_query
from framework.shared.json import to_jsonable


@dataclass(frozen=True)
class RAGGateResult:
    gate_name: str
    passed: bool
    reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.gate_name).strip():
            raise HarnessValidationError("gate_name is required")
        object.__setattr__(self, "details", dict(self.details))

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate_name,
            "passed": self.passed,
            "reason": self.reason,
            "details": to_jsonable(self.details),
        }


@dataclass(frozen=True)
class RAGEvidenceGateResult:
    passed: bool
    missing_evidence_ids: tuple[str, ...] = ()
    missing_source_refs: tuple[str, ...] = ()
    missing_lineage: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "missing_evidence_ids": list(self.missing_evidence_ids),
            "missing_source_refs": list(self.missing_source_refs),
            "missing_lineage": list(self.missing_lineage),
        }


class RAGPlanSchemaGate:
    gate_name = "rag_plan_schema"

    def evaluate(self, plan: RetrievalPlanCandidate) -> RAGGateResult:
        forbidden = sorted(FORBIDDEN_RAG_PLAN_KEYS.intersection(plan.metadata))
        invalid_steps = [
            step.step_id
            for step in plan.steps
            if step.operation == RetrievalOperation.SEARCH_CORPUS and not str(step.query or "").strip()
        ]
        passed = not forbidden and not invalid_steps and bool(plan.queries)
        return RAGGateResult(
            self.gate_name,
            passed,
            None if passed else "retrieval plan candidate schema is invalid",
            {
                "forbidden": forbidden,
                "invalid_steps": invalid_steps,
                "query_count": len(plan.queries),
            },
        )


class RAGToolAllowlistGate:
    gate_name = "rag_tool_allowlist"

    def evaluate(self, plan: RetrievalPlanCandidate, policy: RAGExecutionPolicy) -> RAGGateResult:
        violations = []
        for step in plan.steps:
            if policy.allows_step(step):
                continue
            violations.append(
                {
                    "step_id": step.step_id,
                    "operation": step.operation.value,
                    "corpus": step.corpus,
                    "memory_namespace": step.memory_namespace,
                    "tool_name": step.metadata.get("tool_name"),
                }
            )
        return RAGGateResult(
            self.gate_name,
            not violations,
            None if not violations else "retrieval plan uses disallowed corpus, memory namespace, or tool",
            {"violations": violations},
        )


class RAGQueryDedupGate:
    gate_name = "rag_query_dedup"

    def evaluate(self, plan: RetrievalPlanCandidate, executed_queries: set[str] | tuple[str, ...]) -> RAGGateResult:
        seen = set(str(item) for item in executed_queries)
        duplicates = []
        for step in plan.queries:
            normalized = normalize_query(step.query)
            if not normalized:
                continue
            if normalized in seen:
                duplicates.append(step.step_id)
            seen.add(normalized)
        return RAGGateResult(
            self.gate_name,
            not duplicates,
            None if not duplicates else "retrieval plan repeats an executed or equivalent query",
            {"duplicates": duplicates},
        )


class RAGScopeGate:
    gate_name = "rag_scope"

    def evaluate(self, plan: RetrievalPlanCandidate, spec: RAGSessionSpec) -> RAGGateResult:
        required_scope = spec.source_policy.get("scope_ref") or spec.goal.constraints.get("scope_ref")
        raw_allowed_source_refs = spec.source_policy.get("allowed_source_refs")
        if not required_scope and raw_allowed_source_refs is None:
            return RAGGateResult(self.gate_name, True, "no explicit scope_ref policy configured")

        invalid_source_policy = raw_allowed_source_refs is not None and not isinstance(
            raw_allowed_source_refs,
            (list, tuple, set, frozenset),
        )
        allowed_source_refs = (
            set()
            if invalid_source_policy or raw_allowed_source_refs is None
            else {
                str(source_ref).strip()
                for source_ref in raw_allowed_source_refs
                if str(source_ref).strip()
            }
        )
        violations: list[str] = []
        missing_source_refs: list[str] = []
        out_of_scope_source_refs: list[dict[str, Any]] = []
        for step in plan.steps:
            if required_scope and step.metadata.get("scope_ref") != required_scope:
                violations.append(step.step_id)
            if raw_allowed_source_refs is None or invalid_source_policy:
                continue
            if step.operation == RetrievalOperation.READ_SOURCE and not step.source_refs:
                missing_source_refs.append(step.step_id)
                violations.append(step.step_id)
            unexpected = sorted(set(step.source_refs) - allowed_source_refs)
            if unexpected:
                out_of_scope_source_refs.append(
                    {"step_id": step.step_id, "source_refs": unexpected}
                )
                violations.append(step.step_id)
        violations = list(dict.fromkeys(violations))
        passed = not invalid_source_policy and not violations
        return RAGGateResult(
            self.gate_name,
            passed,
            None if passed else "retrieval plan step escapes the declared source scope",
            {
                "required_scope": required_scope,
                "allowed_source_refs": sorted(allowed_source_refs),
                "invalid_source_policy": invalid_source_policy,
                "missing_source_refs": missing_source_refs,
                "out_of_scope_source_refs": out_of_scope_source_refs,
                "violations": violations,
            },
        )


class RAGBudgetGate:
    gate_name = "rag_budget"

    def evaluate(self, snapshot: RAGBudgetSnapshot, policy: RAGExecutionPolicy) -> RAGGateResult:
        violations = budget_exceeded(snapshot, policy.budget)
        return RAGGateResult(
            self.gate_name,
            not violations,
            None if not violations else "RAG budget exceeded",
            {"violations": violations, "snapshot": snapshot.to_dict(), "budget": policy.budget.to_dict()},
        )


class RAGSourceQualityGate:
    gate_name = "rag_source_quality"

    def evaluate(self, evidence: tuple[EvidenceCandidate, ...], policy: RAGExecutionPolicy) -> RAGGateResult:
        min_confidence = float(policy.source_policy.get("min_confidence", 0.0))
        allowed_freshness = tuple(policy.source_policy.get("allowed_freshness", ()))
        missing_lineage = [item.evidence_id for item in evidence if not item.lineage]
        missing_span_refs = [item.evidence_id for item in evidence if not item.span_refs]
        low_confidence = [item.evidence_id for item in evidence if item.confidence < min_confidence]
        stale = [
            item.evidence_id
            for item in evidence
            if allowed_freshness and item.freshness not in allowed_freshness
        ]
        violations = {
            "missing_lineage": missing_lineage,
            "missing_span_refs": missing_span_refs,
            "low_confidence": low_confidence,
            "disallowed_freshness": stale,
        }
        passed = not any(violations.values())
        return RAGGateResult(
            self.gate_name,
            passed,
            None if passed else "source quality policy rejected one or more evidence candidates",
            violations,
        )


class RAGEvidenceCoverageGate:
    gate_name = "rag_evidence_coverage"

    def evaluate(self, goal_types: tuple[str, ...], evidence: tuple[EvidenceCandidate, ...]) -> RAGGateResult:
        present = present_evidence_types(evidence)
        missing = [item for item in goal_types if item not in present]
        return RAGGateResult(
            self.gate_name,
            not missing,
            None if not missing else "required evidence types are missing",
            {"required": list(goal_types), "present": sorted(present), "missing": missing},
        )


class RAGEvidenceConflictGate:
    gate_name = "rag_evidence_conflict"

    def evaluate(self, evidence: tuple[EvidenceCandidate, ...]) -> RAGGateResult:
        conflicts = [
            item.evidence_id
            for item in evidence
            if item.metadata.get("conflict") is True or item.metadata.get("conflicts_with")
        ]
        return RAGGateResult(
            self.gate_name,
            not conflicts,
            None if not conflicts else "conflicting evidence candidates detected",
            {"conflicts": conflicts},
        )


class RAGContextSizeGate:
    gate_name = "rag_context_size"

    def evaluate(self, pack: RAGContextPack, policy: RAGExecutionPolicy) -> RAGGateResult:
        snapshot = pack.budget_snapshot or RAGBudgetSnapshot()
        item_count = len(pack.accepted_evidence) + len(pack.memory_context)
        token_count = snapshot.context_tokens_used
        violations = {}
        if item_count > policy.budget.max_context_items:
            violations["context_items"] = {"used": item_count, "max": policy.budget.max_context_items}
        if token_count > policy.budget.max_context_tokens:
            violations["context_tokens"] = {"used": token_count, "max": policy.budget.max_context_tokens}
        return RAGGateResult(
            self.gate_name,
            not violations,
            None if not violations else "RAG context pack exceeds context budget",
            {"violations": violations, "item_count": item_count, "token_count": token_count},
        )


class RAGMemoryRelevanceGate:
    gate_name = "rag_memory_relevance"

    def evaluate(self, memory_context: tuple[dict[str, Any], ...], spec: RAGSessionSpec) -> RAGGateResult:
        allowed = set(spec.allowed_memory_namespaces)
        irrelevant = []
        for index, item in enumerate(memory_context):
            namespace = str(item.get("namespace", ""))
            if namespace not in allowed:
                irrelevant.append({"index": index, "namespace": namespace, "reason": "namespace_not_allowed"})
                continue
            relevance = float(item.get("relevance", item.get("score", 1.0)))
            if relevance <= 0:
                irrelevant.append({"index": index, "namespace": namespace, "reason": "not_relevant"})
        return RAGGateResult(
            self.gate_name,
            not irrelevant,
            None if not irrelevant else "memory recall returned irrelevant or unauthorized context",
            {"irrelevant": irrelevant},
        )


class RAGLineageGate:
    gate_name = "rag_lineage"

    def evaluate(self, evidence: tuple[EvidenceCandidate, ...]) -> RAGGateResult:
        missing_source = [item.evidence_id for item in evidence if not item.source_ref]
        missing_spans = [item.evidence_id for item in evidence if not item.span_refs]
        missing_lineage = [item.evidence_id for item in evidence if not item.lineage]
        passed = not missing_source and not missing_spans and not missing_lineage
        return RAGGateResult(
            self.gate_name,
            passed,
            None if passed else "accepted evidence is missing source, span, or lineage refs",
            {
                "missing_source": missing_source,
                "missing_spans": missing_spans,
                "missing_lineage": missing_lineage,
            },
        )


class RAGGateSuite:
    def __init__(self) -> None:
        self.plan_schema = RAGPlanSchemaGate()
        self.tool_allowlist = RAGToolAllowlistGate()
        self.query_dedup = RAGQueryDedupGate()
        self.scope = RAGScopeGate()
        self.budget = RAGBudgetGate()
        self.source_quality = RAGSourceQualityGate()
        self.evidence_coverage = RAGEvidenceCoverageGate()
        self.evidence_conflict = RAGEvidenceConflictGate()
        self.context_size = RAGContextSizeGate()
        self.memory_relevance = RAGMemoryRelevanceGate()
        self.lineage = RAGLineageGate()

    def verify_plan(
        self,
        plan: RetrievalPlanCandidate,
        *,
        spec: RAGSessionSpec,
        policy: RAGExecutionPolicy,
        executed_queries: set[str] | tuple[str, ...],
        projected_snapshot: RAGBudgetSnapshot,
    ) -> tuple[RAGGateResult, ...]:
        return (
            self.plan_schema.evaluate(plan),
            self.tool_allowlist.evaluate(plan, policy),
            self.query_dedup.evaluate(plan, executed_queries),
            self.scope.evaluate(plan, spec),
            self.budget.evaluate(projected_snapshot, policy),
        )

    def verify_sources(
        self,
        evidence: tuple[EvidenceCandidate, ...],
        *,
        spec: RAGSessionSpec,
        policy: RAGExecutionPolicy,
        memory_context: tuple[dict[str, Any], ...] = (),
    ) -> tuple[RAGGateResult, ...]:
        return (
            self.source_quality.evaluate(evidence, policy),
            self.evidence_coverage.evaluate(spec.goal.required_evidence_types, evidence),
            self.evidence_conflict.evaluate(evidence),
            self.memory_relevance.evaluate(memory_context, spec),
            self.lineage.evaluate(evidence),
        )

    def verify_context_pack(self, pack: RAGContextPack, *, policy: RAGExecutionPolicy) -> tuple[RAGGateResult, ...]:
        return (
            self.context_size.evaluate(pack, policy),
            self.budget.evaluate(pack.budget_snapshot or RAGBudgetSnapshot(), policy),
        )


def validate_rag_evidence_refs(pack: RAGContextPack) -> RAGEvidenceGateResult:
    evidence = pack.evidence or tuple(item.to_evidence_pack() for item in pack.accepted_evidence)
    missing_source_refs = tuple(item.evidence_id for item in evidence if not item.source_refs)
    missing_lineage = tuple(item.evidence_id for item in evidence if not item.lineage)
    missing_ids = tuple(item.title for item in evidence if not item.evidence_id)
    return RAGEvidenceGateResult(
        passed=not missing_source_refs and not missing_lineage and not missing_ids,
        missing_evidence_ids=missing_ids,
        missing_source_refs=missing_source_refs,
        missing_lineage=missing_lineage,
    )


def failed_gate_dicts(results: tuple[RAGGateResult, ...]) -> tuple[dict[str, Any], ...]:
    return tuple(result.to_dict() for result in results if result.passed is False)


__all__ = [
    "RAGEvidenceConflictGate",
    "RAGEvidenceCoverageGate",
    "RAGEvidenceGateResult",
    "RAGBudgetGate",
    "RAGContextSizeGate",
    "RAGGateResult",
    "RAGGateSuite",
    "RAGLineageGate",
    "RAGMemoryRelevanceGate",
    "RAGPlanSchemaGate",
    "RAGQueryDedupGate",
    "RAGScopeGate",
    "RAGSourceQualityGate",
    "RAGToolAllowlistGate",
    "failed_gate_dicts",
    "validate_rag_evidence_refs",
]
