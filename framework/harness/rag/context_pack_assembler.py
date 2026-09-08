from __future__ import annotations

from typing import Any

from framework.harness.context.assembler import ContextAssembler
from framework.harness.context.models import ContextBudget, ContextEnvelope
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.rag.models import (
    EvidenceCandidate,
    RAGBudgetSnapshot,
    RAGContextPack,
    RAGSessionSpec,
)
from framework.harness.rag.policy import RAGExecutionPolicy
from framework.harness.rag.visibility import evidence_visible_to_tenant
from framework.shared.json import to_jsonable


class RAGContextPackAssembler:
    def __init__(self, context_assembler: ContextAssembler | None = None) -> None:
        self.context_assembler = context_assembler or ContextAssembler()
        self.envelopes: list[ContextEnvelope] = []

    def assemble(
        self,
        *,
        spec: RAGSessionSpec,
        accepted_evidence: tuple[EvidenceCandidate, ...],
        rejected_evidence: tuple[EvidenceCandidate, ...] = (),
        conflicting_evidence: tuple[EvidenceCandidate, ...] = (),
        memory_context: tuple[dict[str, Any], ...] = (),
        artifact_refs: tuple[str, ...] = (),
        gap_report: dict[str, Any] | None = None,
        budget_snapshot: RAGBudgetSnapshot | None = None,
        policy: RAGExecutionPolicy | None = None,
    ) -> RAGContextPack:
        policy = policy or RAGExecutionPolicy.from_session_spec(spec)
        tenant_id = str(policy.source_policy.get("tenant_id") or "").strip() or None
        accepted_evidence = _visible_evidence(accepted_evidence, tenant_id=tenant_id)
        rejected_evidence = _visible_evidence(rejected_evidence, tenant_id=tenant_id)
        conflicting_evidence = _visible_evidence(conflicting_evidence, tenant_id=tenant_id)
        snapshot = budget_snapshot or RAGBudgetSnapshot()
        context_budget = _context_budget_from_rag_policy(policy)
        source_refs = _source_refs(accepted_evidence, rejected_evidence, conflicting_evidence)
        artifact_refs = tuple(dict.fromkeys((
            *_artifact_refs(accepted_evidence, rejected_evidence, conflicting_evidence),
            *tuple(str(ref) for ref in artifact_refs if str(ref).strip()),
        )))
        retained_memory_context = tuple(_trim_memory_hit(item) for item in memory_context[: policy.budget.max_memory_hits])
        retained_evidence = accepted_evidence[: policy.budget.max_context_items]
        evidence_trace = _evidence_trace(
            accepted=retained_evidence,
            rejected=rejected_evidence,
            conflicting=conflicting_evidence,
        )
        context_tokens = min(
            _estimate_context_tokens(accepted_evidence, rejected_evidence, conflicting_evidence, memory_context),
            policy.budget.max_context_tokens,
        )
        context_items = len(retained_evidence) + len(retained_memory_context)
        snapshot = RAGBudgetSnapshot(
            rounds_used=snapshot.rounds_used,
            replans_used=snapshot.replans_used,
            queries_used=snapshot.queries_used,
            source_reads_used=snapshot.source_reads_used,
            memory_hits_used=snapshot.memory_hits_used,
            context_items_used=context_items,
            context_tokens_used=context_tokens,
            worker_calls_used=snapshot.worker_calls_used,
        )
        pack = RAGContextPack(
            pack_id=f"rag-context://{spec.session_id}",
            query=spec.goal.question,
            evidence=tuple(item.to_evidence_pack() for item in retained_evidence),
            context_refs=spec.goal.known_context_refs,
            goal=spec.goal,
            accepted_evidence=retained_evidence,
            rejected_evidence=rejected_evidence,
            conflicting_evidence=conflicting_evidence,
            memory_context=retained_memory_context,
            source_refs=source_refs,
            artifact_refs=artifact_refs,
            evidence_trace=evidence_trace,
            gap_report=dict(gap_report or {}),
            budget_snapshot=snapshot,
            assembly_summary=_assembly_summary(accepted_evidence, rejected_evidence, conflicting_evidence, memory_context),
            metadata={
                "context_assembly_required": True,
                "stable_prefix_contains_dynamic_rag": False,
                "context_policy": to_jsonable(policy.context_policy),
                "artifact_refs": list(artifact_refs),
                "evidence_trace": to_jsonable(list(evidence_trace)),
            },
        )
        envelope = self.to_context_envelope(pack, spec=spec, context_budget=context_budget)
        self.envelopes.append(envelope)
        return RAGContextPack(
            pack_id=pack.pack_id,
            query=pack.query,
            evidence=pack.evidence,
            context_refs=pack.context_refs,
            goal=pack.goal,
            accepted_evidence=pack.accepted_evidence,
            rejected_evidence=pack.rejected_evidence,
            conflicting_evidence=pack.conflicting_evidence,
            memory_context=pack.memory_context,
            source_refs=pack.source_refs,
            artifact_refs=pack.artifact_refs,
            evidence_trace=pack.evidence_trace,
            gap_report=pack.gap_report,
            budget_snapshot=pack.budget_snapshot,
            assembly_summary=pack.assembly_summary,
            metadata={
                **pack.metadata,
                "context_envelope_id": envelope.envelope_id,
                "context_snapshot_ref": envelope.snapshot_ref,
                "context_dynamic_tail_keys": sorted(envelope.dynamic_tail),
            },
        )

    def to_context_envelope(
        self,
        pack: RAGContextPack,
        *,
        spec: RAGSessionSpec,
        context_budget: ContextBudget | None = None,
    ) -> ContextEnvelope:
        if pack.metadata.get("stable_prefix_contains_dynamic_rag") is True:
            raise HarnessValidationError("RAG dynamic results must not be placed in stable prefix")
        budget = context_budget or _context_budget_from_rag_policy(RAGExecutionPolicy.from_session_spec(spec))
        return self.context_assembler.assemble(
            {
                "envelope_id": f"context://rag/{spec.session_id}",
                "graph_identity": spec.graph_identity,
                "phase": "VERIFY",
                "worker_id": "rag-context-pack-assembler",
                "worker_type": "context",
                "budget": budget,
                "worker_contract_ref": "worker://rag-context-pack",
                "allowed_tools": spec.allowed_tools,
                "allowed_memory_namespaces": spec.allowed_memory_namespaces,
                "source_refs": pack.source_refs,
                "artifact_refs": pack.artifact_refs,
                "memory_refs": tuple(str(item.get("memory_ref", item.get("ref", ""))) for item in pack.memory_context if item.get("memory_ref") or item.get("ref")),
                "evidence_refs": tuple(item.evidence_id for item in pack.accepted_evidence),
                "evidence_memory_ref": pack.pack_id,
                "evidence_memory_tokens": pack.budget_snapshot.context_tokens_used if pack.budget_snapshot else 0,
                "current_task_ref": f"goal://{spec.goal.goal_id}",
                "current_instruction": spec.goal.question,
                "current_task_tokens": min(len(spec.goal.question.split()) + 32, 160),
                "metadata": {
                    "rag_context_pack_id": pack.pack_id,
                    "dynamic_rag_context": True,
                    "accepted_evidence": [item.to_dict() for item in pack.accepted_evidence],
                    "rejected_evidence": [item.to_dict() for item in pack.rejected_evidence],
                    "conflicting_evidence": [item.to_dict() for item in pack.conflicting_evidence],
                    "evidence_trace": to_jsonable(list(pack.evidence_trace)),
                    "gap_report": to_jsonable(pack.gap_report),
                    "budget_snapshot": pack.budget_snapshot.to_dict() if pack.budget_snapshot else None,
                },
            }
        )


class FakeRAGContextPackAssembler(RAGContextPackAssembler):
    pass


def _visible_evidence(
    evidence: tuple[EvidenceCandidate, ...],
    *,
    tenant_id: str | None,
) -> tuple[EvidenceCandidate, ...]:
    return tuple(
        candidate
        for candidate in evidence
        if evidence_visible_to_tenant(candidate, tenant_id=tenant_id)
    )


def _context_budget_from_rag_policy(policy: RAGExecutionPolicy) -> ContextBudget:
    return ContextBudget(
        max_input_tokens=_context_policy_integer(
            policy,
            "max_input_tokens",
            max(policy.budget.max_context_tokens, 1),
            minimum=1,
        ),
        max_output_tokens=_context_policy_integer(
            policy,
            "max_output_tokens",
            1_024,
            minimum=1,
        ),
        max_context_segments=6,
        max_evidence_items=policy.budget.max_context_items,
        max_memory_items=policy.budget.max_memory_hits,
        max_artifact_refs=_context_policy_integer(
            policy,
            "max_artifact_refs",
            12,
            minimum=0,
        ),
        reserved_output_tokens=_context_policy_integer(
            policy,
            "reserved_output_tokens",
            256,
            minimum=0,
        ),
    )


def _context_policy_integer(
    policy: RAGExecutionPolicy,
    field_name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    value = policy.context_policy.get(field_name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HarnessValidationError(
            f"context_policy.{field_name} must be an integer greater than or equal to {minimum}"
        )
    return value


def _source_refs(*groups: tuple[EvidenceCandidate, ...]) -> tuple[str, ...]:
    refs: list[str] = []
    for group in groups:
        for candidate in group:
            refs.append(candidate.source_ref)
            refs.extend(candidate.span_refs)
    return tuple(dict.fromkeys(ref for ref in refs if ref))


def _artifact_refs(*groups: tuple[EvidenceCandidate, ...]) -> tuple[str, ...]:
    refs: list[str] = []
    for group in groups:
        for candidate in group:
            refs.extend(candidate.artifact_refs)
            raw = candidate.metadata.get("artifact_refs", ())
            if isinstance(raw, str):
                refs.append(raw)
            elif isinstance(raw, (list, tuple)):
                refs.extend(str(ref) for ref in raw)
    return tuple(dict.fromkeys(ref for ref in refs if str(ref).strip()))


def _evidence_trace(
    *,
    accepted: tuple[EvidenceCandidate, ...],
    rejected: tuple[EvidenceCandidate, ...],
    conflicting: tuple[EvidenceCandidate, ...],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for status, group in (
        ("accepted", accepted),
        ("rejected", rejected),
        ("conflicting", conflicting),
    ):
        for candidate in group:
            rows.append({
                "status": status,
                "evidence_id": candidate.evidence_id,
                "evidence_type": candidate.evidence_type,
                "source_ref": candidate.source_ref,
                "span_refs": list(candidate.span_refs),
                "artifact_refs": list(candidate.artifact_refs),
                "lineage": list(candidate.lineage),
                "confidence": candidate.confidence,
                "score_breakdown": to_jsonable(candidate.metadata.get("rag_score_breakdown", {})),
            })
    return tuple(rows)


def _estimate_context_tokens(
    accepted: tuple[EvidenceCandidate, ...],
    rejected: tuple[EvidenceCandidate, ...],
    conflicting: tuple[EvidenceCandidate, ...],
    memory_context: tuple[dict[str, Any], ...],
) -> int:
    text_parts: list[str] = []
    for item in accepted + rejected + conflicting:
        text_parts.extend([item.title, item.summary])
    for item in memory_context:
        text_parts.append(str(item.get("summary", item.get("content", ""))))
    return max(sum(max(len(part.split()), 1) for part in text_parts), 1)


def _assembly_summary(
    accepted: tuple[EvidenceCandidate, ...],
    rejected: tuple[EvidenceCandidate, ...],
    conflicting: tuple[EvidenceCandidate, ...],
    memory_context: tuple[dict[str, Any], ...],
) -> str:
    return (
        f"Accepted {len(accepted)} evidence candidates, rejected {len(rejected)}, "
        f"flagged {len(conflicting)} conflicts, retained {len(memory_context)} memory hits."
    )


def _trim_memory_hit(item: dict[str, Any]) -> dict[str, Any]:
    preserved = {
        "memory_ref",
        "memory_id",
        "namespace_ref",
        "namespace_checksum",
        "input_snapshot_ref",
        "execution_identity",
        "ref",
        "namespace",
        "title",
        "summary",
        "case_type",
        "outcome",
        "relevance",
        "score",
        "source_refs",
    }
    result = {key: value for key, value in item.items() if key in preserved}
    if not result.get("summary") and "content" in item:
        result["summary"] = str(item["content"])[:500]
    return result


__all__ = ["FakeRAGContextPackAssembler", "RAGContextPackAssembler"]
