from __future__ import annotations

from dataclasses import dataclass, replace

from framework.harness.rag.gates import RAGGateResult, RAGLineageGate, RAGSourceQualityGate
from framework.harness.rag.models import EvidenceCandidate
from framework.harness.rag.policy import RAGExecutionPolicy
from framework.harness.rag.relevance import RAGRelevanceGate, RelevanceScorerPort
from framework.harness.rag.visibility import evidence_tenant_ids, evidence_visible_to_tenant


@dataclass(frozen=True)
class SourceVerificationResult:
    accepted: tuple[EvidenceCandidate, ...]
    rejected: tuple[EvidenceCandidate, ...]
    conflicting: tuple[EvidenceCandidate, ...]
    gate_results: tuple[RAGGateResult, ...]

    def to_dict(self) -> dict:
        return {
            "accepted": [item.to_dict() for item in self.accepted],
            "rejected": [item.to_dict() for item in self.rejected],
            "conflicting": [item.to_dict() for item in self.conflicting],
            "gate_results": [item.to_dict() for item in self.gate_results],
        }


class SourceVerifier:
    def __init__(
        self,
        *,
        relevance_scorer: RelevanceScorerPort | None = None,
        relevance_gate: RAGRelevanceGate | None = None,
    ) -> None:
        self.source_quality = RAGSourceQualityGate()
        self.lineage = RAGLineageGate()
        self.relevance_scorer = relevance_scorer
        self.relevance_gate = relevance_gate or RAGRelevanceGate()

    def verify(
        self,
        evidence: tuple[EvidenceCandidate, ...],
        *,
        policy: RAGExecutionPolicy,
        question: str | None = None,
    ) -> SourceVerificationResult:
        tenant_eligible = tuple(
            candidate
            for candidate in evidence
            if _tenant_scope_violation(candidate, policy) is None
        )
        relevance_result = _relevance_result(
            tenant_eligible,
            policy=policy,
            question=question,
            scorer=self.relevance_scorer,
        )
        accepted: list[EvidenceCandidate] = []
        rejected: list[EvidenceCandidate] = []
        conflicting: list[EvidenceCandidate] = []
        for candidate in evidence:
            relevance_threshold = _relevance_threshold(candidate, policy)
            tenant_violation = _tenant_scope_violation(candidate, policy)
            candidate_results = (
                self.source_quality.evaluate((candidate,), policy),
                self.lineage.evaluate((candidate,)),
            )
            if tenant_violation is not None:
                rejected.append(_with_tenant_rejection_metadata(candidate, violation=tenant_violation))
            elif candidate.metadata.get("conflict") is True or candidate.metadata.get("conflicts_with"):
                conflicting.append(candidate)
            elif (
                self.relevance_scorer is not None
                and candidate.evidence_id in relevance_result.scores_by_id
                and relevance_result.scores_by_id[candidate.evidence_id] < relevance_threshold
            ):
                rejected.append(_with_rejection_metadata(
                    candidate,
                    reason="low_relevance"
                    if len(relevance_result.ordered_scores) == len(evidence)
                    else "relevance_score_unavailable",
                    relevance_score=relevance_result.scores_by_id[candidate.evidence_id],
                    relevance_threshold=relevance_threshold,
                ))
            elif all(result.passed for result in candidate_results):
                accepted.append(candidate)
            else:
                rejected.append(candidate)
        gate_results = (
            self.source_quality.evaluate(evidence, policy),
            self.lineage.evaluate(evidence),
        )
        tenant_gate = _tenant_scope_gate(evidence, policy)
        if tenant_gate is not None:
            gate_results = (*gate_results, tenant_gate)
        if self.relevance_scorer is not None and question and tenant_eligible:
            gate_results = (
                *gate_results,
                self.relevance_gate.evaluate(
                    question,
                    tenant_eligible,
                    relevance_result.ordered_scores,
                    threshold=_default_relevance_threshold(policy),
                    thresholds_by_evidence_type=_typed_relevance_thresholds(policy),
                ),
            )
        return SourceVerificationResult(
            accepted=tuple(accepted),
            rejected=tuple(rejected),
            conflicting=tuple(conflicting),
            gate_results=gate_results,
        )


class FakeSourceVerifier(SourceVerifier):
    pass


__all__ = ["FakeSourceVerifier", "SourceVerificationResult", "SourceVerifier"]


@dataclass(frozen=True)
class _RelevanceResult:
    scores_by_id: dict[str, float]
    ordered_scores: tuple[float, ...]


def _relevance_result(
    evidence: tuple[EvidenceCandidate, ...],
    *,
    policy: RAGExecutionPolicy,
    question: str | None,
    scorer: RelevanceScorerPort | None,
) -> _RelevanceResult:
    if scorer is None or not question or not evidence:
        return _RelevanceResult(scores_by_id={}, ordered_scores=())
    scores = scorer.score(question, [item.summary for item in evidence])
    if len(scores) != len(evidence):
        threshold = float(policy.source_policy.get("min_relevance", 0.30))
        return _RelevanceResult(
            scores_by_id={item.evidence_id: threshold - 1.0 for item in evidence},
            ordered_scores=tuple(float(score) for score in scores),
        )
    clamped_scores = tuple(max(0.0, min(float(score), 1.0)) for score in scores)
    return _RelevanceResult(
        scores_by_id={
            item.evidence_id: score
            for item, score in zip(evidence, clamped_scores)
        },
        ordered_scores=clamped_scores,
    )


def _default_relevance_threshold(policy: RAGExecutionPolicy) -> float:
    return _safe_float(policy.source_policy.get("min_relevance"), default=0.30)


def _typed_relevance_thresholds(policy: RAGExecutionPolicy) -> dict[str, float]:
    raw = policy.source_policy.get("min_relevance_by_type", {})
    if not isinstance(raw, dict):
        return {}
    thresholds: dict[str, float] = {}
    for key, value in raw.items():
        thresholds[str(key)] = _safe_float(value, default=_default_relevance_threshold(policy))
    return thresholds


def _relevance_threshold(candidate: EvidenceCandidate, policy: RAGExecutionPolicy) -> float:
    typed_thresholds = _typed_relevance_thresholds(policy)
    for key in _threshold_keys(candidate):
        if key in typed_thresholds:
            return typed_thresholds[key]
    return _default_relevance_threshold(policy)


def _safe_float(value: object, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _threshold_keys(candidate: EvidenceCandidate) -> tuple[str, ...]:
    keys = [candidate.evidence_type]
    chunk_type = candidate.metadata.get("chunk_type")
    if chunk_type is not None:
        keys.append(str(chunk_type))
    return tuple(dict.fromkeys(key for key in keys if key))


def _tenant_scope_gate(
    evidence: tuple[EvidenceCandidate, ...],
    policy: RAGExecutionPolicy,
) -> RAGGateResult | None:
    tenant_id = _policy_tenant_id(policy)
    violations = [
        violation
        for candidate in evidence
        if (violation := _tenant_scope_violation(candidate, policy)) is not None
    ]
    if not tenant_id and not violations:
        return None
    details: dict[str, object] = {
        "visibility_scope": f"tenant:{tenant_id}" if tenant_id else "public",
        "checked_count": len(evidence),
        "violations": violations,
    }
    if tenant_id:
        details["tenant_id"] = tenant_id
    return RAGGateResult(
        "rag_tenant_scope",
        not violations,
        None if not violations else "one or more evidence candidates are outside the tenant scope",
        details,
    )


def _tenant_scope_violation(
    candidate: EvidenceCandidate,
    policy: RAGExecutionPolicy,
) -> dict[str, object] | None:
    tenant_id = _policy_tenant_id(policy)
    if evidence_visible_to_tenant(candidate, tenant_id=tenant_id or None):
        return None
    return {
        "evidence_id": candidate.evidence_id,
        "visibility_scope": f"tenant:{tenant_id}" if tenant_id else "public",
        "candidate_tenant_ids": sorted(evidence_tenant_ids(candidate)),
        **({"tenant_id": tenant_id} if tenant_id else {}),
    }


def _policy_tenant_id(policy: RAGExecutionPolicy) -> str:
    return str(policy.source_policy.get("tenant_id") or "").strip()


def _with_rejection_metadata(
    candidate: EvidenceCandidate,
    *,
    reason: str,
    relevance_score: float,
    relevance_threshold: float,
) -> EvidenceCandidate:
    return replace(
        candidate,
        metadata={
            **candidate.metadata,
            "rejection_reason": reason,
            "relevance_score": relevance_score,
            "relevance_threshold": relevance_threshold,
        },
    )


def _with_tenant_rejection_metadata(
    candidate: EvidenceCandidate,
    *,
    violation: dict[str, object],
) -> EvidenceCandidate:
    return replace(
        candidate,
        metadata={
            **candidate.metadata,
            "rejection_reason": "tenant_scope_violation",
            "tenant_scope_violation": violation,
        },
    )
