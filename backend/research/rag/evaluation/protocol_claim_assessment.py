"""Per-claim fact assessment against the reference and the actual context.

Each answer claim is decided twice: against verified reference facts (is it
correct?) and against the passages the model actually saw (is it grounded?).
Deterministic fact comparison decides what it can; every pair it cannot decide
is kept as a pending judgment. A separate consensus workflow may resolve
exactly those pairs and feed them back as ``SemanticResolution`` records, which
are applied only to pending pairs and are always attributed to their resolver.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from pydantic import Field, model_validator

from backend.research.rag.evaluation.protocol_common import NonEmptyStr, ProtocolModel, content_digest
from backend.research.rag.evaluation.protocol_fact_projections import (
    CandidateFactProjection,
    EvidenceFactProjection,
    ReferenceFactProjection,
    rule_key,
)
from backend.research.rag.evaluation.protocol_semantic_facts import (
    DimensionOutcome,
    FactComparison,
    FactDimension,
    FactRelation,
    SemanticFact,
    compare_semantic_facts,
)
from framework.harness.rag.models import AnswerClaim, RAGContextPack


class SemanticDecision(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    UNSUPPORTED = "unsupported"
    NEEDS_REVIEW = "needs_review"


class ClaimVerdict(StrEnum):
    """Combined outcome of one answer claim against reference and context.

    The reference (gold) decision is authoritative when it is decided. Only when
    the reference does not cover the claim does actual context decide whether
    the claim is grounded extra information or an over-inference.
    """

    CORRECT = "correct"
    CONTRADICTED = "contradicted"
    GROUNDED_EXTRA = "grounded_extra"
    OVER_INFERENCE = "over_inference"
    UNRESOLVED = "unresolved"


class ComparisonTargetKind(StrEnum):
    REFERENCE = "reference"
    EVIDENCE = "evidence"


class TargetComparison(ProtocolModel):
    """One deterministic comparison of an answer claim fact against one target fact."""

    target_kind: ComparisonTargetKind
    target_id: NonEmptyStr
    evidence_id: NonEmptyStr | None = None
    target_rule: NonEmptyStr
    comparison: FactComparison

    @model_validator(mode="after")
    def _evidence_contract(self) -> "TargetComparison":
        if (self.target_kind is ComparisonTargetKind.EVIDENCE) != (self.evidence_id is not None):
            raise ValueError("evidence comparisons, and only they, carry an evidence_id")
        return self


_ACCURACY_DIMENSIONS = frozenset({
    FactDimension.NUMERIC_VALUE,
    FactDimension.UNIT,
    FactDimension.DATASET,
    FactDimension.SPLIT,
    FactDimension.CONDITION,
})
_RESOLVED_DIMENSION_OUTCOMES = frozenset({
    DimensionOutcome.MATCH,
    DimensionOutcome.MISMATCH,
    DimensionOutcome.UNDETERMINED,
})


class ResolvedDimension(ProtocolModel):
    dimension: FactDimension
    outcome: DimensionOutcome
    name: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _accuracy_dimension(self) -> "ResolvedDimension":
        if self.dimension not in _ACCURACY_DIMENSIONS:
            raise ValueError("resolved dimensions are limited to value, unit, and experimental conditions")
        if self.outcome not in _RESOLVED_DIMENSION_OUTCOMES:
            raise ValueError("resolved dimension outcome must be match, mismatch, or undetermined")
        return self


class SemanticResolution(ProtocolModel):
    """An externally resolved decision for one pending (claim, target) pair."""

    claim_id: NonEmptyStr
    target_kind: ComparisonTargetKind
    target_id: NonEmptyStr
    decision: SemanticDecision
    dimensions: tuple[ResolvedDimension, ...] = ()
    resolver: NonEmptyStr
    evidence: tuple[NonEmptyStr, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _decided(self) -> "SemanticResolution":
        if self.decision is SemanticDecision.NEEDS_REVIEW:
            raise ValueError("a resolution must decide the pair")
        if self.dimensions and self.target_kind is not ComparisonTargetKind.REFERENCE:
            raise ValueError("dimension outcomes are only resolved against reference claims")
        return self

    @property
    def key(self) -> tuple[str, ComparisonTargetKind, str]:
        return self.claim_id, self.target_kind, self.target_id


class PendingJudgment(ProtocolModel):
    """One pair the deterministic rules could not decide."""

    claim_id: NonEmptyStr
    target_kind: ComparisonTargetKind
    target_id: NonEmptyStr
    bound: bool


class PassageDecision(ProtocolModel):
    evidence_id: NonEmptyStr
    decision: SemanticDecision
    resolved_by: NonEmptyStr | None = None


class ClaimFactAssessment(ProtocolModel):
    """Auditable per-claim record behind every fact and context metric."""

    claim_id: NonEmptyStr
    candidate_rule: NonEmptyStr | None
    candidate_fact: SemanticFact | None
    bound_reference_claim_id: NonEmptyStr | None = None
    reference_decision: SemanticDecision
    aligned_reference_claim_id: NonEmptyStr | None
    supported_reference_ids: tuple[NonEmptyStr, ...] = ()
    pending_reference_ids: tuple[NonEmptyStr, ...] = ()
    recall_pending_reference_ids: tuple[NonEmptyStr, ...] = ()
    context_decision: SemanticDecision
    passages: tuple[PassageDecision, ...] = ()
    verdict: ClaimVerdict
    reason: NonEmptyStr
    reference_comparisons: tuple[TargetComparison, ...] = ()
    context_comparisons: tuple[TargetComparison, ...] = ()
    resolutions: tuple[SemanticResolution, ...] = ()

    def pending_judgments(self) -> tuple[PendingJudgment, ...]:
        bound = self.bound_reference_claim_id is not None
        reference = tuple(
            PendingJudgment(
                claim_id=self.claim_id,
                target_kind=ComparisonTargetKind.REFERENCE,
                target_id=target_id,
                bound=bound,
            )
            for target_id in (*self.pending_reference_ids, *self.recall_pending_reference_ids)
        )
        evidence = tuple(
            PendingJudgment(
                claim_id=self.claim_id,
                target_kind=ComparisonTargetKind.EVIDENCE,
                target_id=item.evidence_id,
                bound=False,
            )
            for item in self.passages
            if item.decision is SemanticDecision.NEEDS_REVIEW
        )
        return (*reference, *evidence)

    def decision_over_evidence(self, evidence_ids: Sequence[str]) -> SemanticDecision:
        by_id = {item.evidence_id: item.decision for item in self.passages}
        return combine_decisions([
            by_id.get(evidence_id, SemanticDecision.NEEDS_REVIEW) for evidence_id in evidence_ids
        ])

    def aligned_dimension_outcomes(self) -> tuple[ResolvedDimension, ...] | None:
        """Value/unit/condition outcomes against the single aligned reference fact."""

        target_id = self.aligned_reference_claim_id
        if target_id is None:
            return None
        for resolution in self.resolutions:
            if resolution.target_kind is ComparisonTargetKind.REFERENCE and resolution.target_id == target_id:
                return resolution.dimensions
        for item in self.reference_comparisons:
            if item.target_id == target_id:
                return tuple(
                    ResolvedDimension(dimension=dim.dimension, outcome=dim.outcome, name=dim.name)
                    for dim in item.comparison.dimensions
                    if dim.dimension in _ACCURACY_DIMENSIONS
                    and dim.outcome in (DimensionOutcome.MATCH, DimensionOutcome.MISMATCH)
                )
        return None


_RELATION_DECISION: dict[FactRelation, SemanticDecision] = {
    FactRelation.MATCH: SemanticDecision.SUPPORTED,
    FactRelation.CONFLICT: SemanticDecision.CONTRADICTED,
    FactRelation.UNDETERMINED: SemanticDecision.NEEDS_REVIEW,
    FactRelation.SCOPE_MISMATCH: SemanticDecision.UNSUPPORTED,
}


def combine_decisions(decisions: Iterable[SemanticDecision]) -> SemanticDecision:
    """Combine one claim's decisions over several targets.

    Any support wins. An unresolved target keeps the claim unresolved even if
    another target conflicts, so a contradiction is reported only when every
    target is decided and at least one disagrees.
    """

    values = set(decisions)
    if not values:
        return SemanticDecision.NEEDS_REVIEW
    for decision in (
        SemanticDecision.SUPPORTED,
        SemanticDecision.NEEDS_REVIEW,
        SemanticDecision.CONTRADICTED,
    ):
        if decision in values:
            return decision
    return SemanticDecision.UNSUPPORTED


ResolutionIndex = Mapping[tuple[str, ComparisonTargetKind, str], SemanticResolution]


def assess_claim(
    claim: AnswerClaim,
    *,
    projection: CandidateFactProjection | None,
    reference_by_id: Mapping[str, ReferenceFactProjection],
    reference_claim_ids: Sequence[str],
    actual_context: RAGContextPack | None,
    evidence_by_id: Mapping[str, tuple[EvidenceFactProjection, ...]],
    resolutions: ResolutionIndex,
) -> ClaimFactAssessment:
    fact = projection.fact if projection is not None else None
    bound_id = projection.reference_claim_id if projection is not None else None
    reference = _assess_reference(
        claim.claim_id,
        fact,
        bound_id=bound_id,
        reference_by_id=reference_by_id,
        reference_claim_ids=reference_claim_ids,
        resolutions=resolutions,
    )
    context = _assess_context(
        claim.claim_id,
        fact,
        actual_context=actual_context,
        evidence_by_id=evidence_by_id,
        resolutions=resolutions,
    )
    verdict, reason = derive_verdict(reference.decision, context.decision)
    if fact is None and verdict is ClaimVerdict.UNRESOLVED:
        reason = "answer claim has no verified semantic projection"
    return ClaimFactAssessment(
        claim_id=claim.claim_id,
        candidate_rule=None if projection is None else rule_key(projection.rule_id, projection.rule_version),
        candidate_fact=fact,
        bound_reference_claim_id=bound_id,
        reference_decision=reference.decision,
        aligned_reference_claim_id=reference.aligned_id,
        supported_reference_ids=reference.supported,
        pending_reference_ids=reference.pending,
        recall_pending_reference_ids=reference.recall_pending,
        context_decision=context.decision,
        passages=context.passages,
        verdict=verdict,
        reason=reason,
        reference_comparisons=reference.comparisons,
        context_comparisons=context.comparisons,
        resolutions=(*reference.applied, *context.applied),
    )


@dataclass(frozen=True)
class _ReferenceAssessment:
    decision: SemanticDecision
    aligned_id: str | None
    supported: tuple[str, ...]
    pending: tuple[str, ...]
    recall_pending: tuple[str, ...]
    comparisons: tuple[TargetComparison, ...]
    applied: tuple[SemanticResolution, ...]


@dataclass(frozen=True)
class _ContextAssessment:
    decision: SemanticDecision
    passages: tuple[PassageDecision, ...]
    comparisons: tuple[TargetComparison, ...]
    applied: tuple[SemanticResolution, ...]


def _assess_reference(
    claim_id: str,
    fact: SemanticFact | None,
    *,
    bound_id: str | None,
    reference_by_id: Mapping[str, ReferenceFactProjection],
    reference_claim_ids: Sequence[str],
    resolutions: ResolutionIndex,
) -> _ReferenceAssessment:
    """Decide one claim against the reference claims it may address.

    A bound claim (aligned upstream to one reference claim) is decided only
    against that claim; an unbound claim against every reference claim, where
    a reference claim without a verified fact can only be decided by judgment.
    """

    precision_ids = (bound_id,) if bound_id is not None else tuple(reference_claim_ids)
    # Completeness asks whether *any* claim covers a reference claim. A bound
    # alignment is evidence for one target, not proof that a reference claim
    # without a verified fact is uncovered, so those stay judgment targets.
    recall_only_ids = tuple(
        target_id for target_id in reference_claim_ids
        if target_id not in precision_ids and target_id not in reference_by_id
    )
    comparisons: list[TargetComparison] = []
    applied: list[SemanticResolution] = []
    per_target: dict[str, SemanticDecision] = {}
    for target_id in (*precision_ids, *recall_only_ids):
        target = reference_by_id.get(target_id)
        decision = SemanticDecision.NEEDS_REVIEW
        if fact is not None and target is not None:
            comparison = compare_semantic_facts(fact, target.fact, bound=bound_id is not None)
            comparisons.append(TargetComparison(
                target_kind=ComparisonTargetKind.REFERENCE,
                target_id=target_id,
                target_rule=rule_key(target.rule_id, target.rule_version),
                comparison=comparison,
            ))
            decision = _RELATION_DECISION[comparison.relation]
        if decision is SemanticDecision.NEEDS_REVIEW:
            resolution = resolutions.get((claim_id, ComparisonTargetKind.REFERENCE, target_id))
            if resolution is not None:
                applied.append(resolution)
                decision = resolution.decision
        per_target[target_id] = decision
    precision_decisions = {key: per_target[key] for key in precision_ids}
    supported = tuple(key for key, value in per_target.items() if value is SemanticDecision.SUPPORTED)
    pending = tuple(
        key for key, value in precision_decisions.items() if value is SemanticDecision.NEEDS_REVIEW
    )
    recall_pending = tuple(
        key for key in recall_only_ids if per_target[key] is SemanticDecision.NEEDS_REVIEW
    )
    contradicted = tuple(
        key for key, value in precision_decisions.items() if value is SemanticDecision.CONTRADICTED
    )
    decision = combine_decisions(precision_decisions.values())
    supported_precision = tuple(key for key in supported if key in precision_decisions)
    if bound_id is not None:
        aligned_id: str | None = bound_id
    elif decision is SemanticDecision.SUPPORTED:
        aligned_id = supported_precision[0] if len(supported_precision) == 1 else None
    elif decision is SemanticDecision.CONTRADICTED:
        aligned_id = contradicted[0] if len(contradicted) == 1 else None
    else:
        aligned_id = None
    return _ReferenceAssessment(
        decision=decision,
        aligned_id=aligned_id,
        supported=supported,
        pending=pending,
        recall_pending=recall_pending,
        comparisons=tuple(comparisons),
        applied=tuple(applied),
    )


def _assess_context(
    claim_id: str,
    fact: SemanticFact | None,
    *,
    actual_context: RAGContextPack | None,
    evidence_by_id: Mapping[str, tuple[EvidenceFactProjection, ...]],
    resolutions: ResolutionIndex,
) -> _ContextAssessment:
    """Decide one claim against each passage the model actually saw."""

    if actual_context is None:
        return _ContextAssessment(SemanticDecision.NEEDS_REVIEW, (), (), ())
    if not actual_context.accepted_evidence:
        # Nothing was shown to the model, so no claim can be grounded in it.
        return _ContextAssessment(SemanticDecision.UNSUPPORTED, (), (), ())
    passages: list[PassageDecision] = []
    comparisons: list[TargetComparison] = []
    applied: list[SemanticResolution] = []
    seen: set[str] = set()
    for evidence in actual_context.accepted_evidence:
        evidence_id = evidence.evidence_id
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        relations: list[FactRelation] = []
        if fact is not None:
            for item in evidence_by_id.get(evidence_id, ()):
                comparison = compare_semantic_facts(fact, item.fact, bound=False)
                relations.append(comparison.relation)
                comparisons.append(TargetComparison(
                    target_kind=ComparisonTargetKind.EVIDENCE,
                    target_id=f"{evidence_id}#{content_digest(item.fact.model_dump(mode='json'))}",
                    evidence_id=evidence_id,
                    target_rule=rule_key(item.rule_id, item.rule_version),
                    comparison=comparison,
                ))
        decision = combine_decisions(_RELATION_DECISION[relation] for relation in relations)
        resolved_by = None
        if decision is SemanticDecision.NEEDS_REVIEW:
            resolution = resolutions.get((claim_id, ComparisonTargetKind.EVIDENCE, evidence_id))
            if resolution is not None:
                applied.append(resolution)
                decision = resolution.decision
                resolved_by = resolution.resolver
        passages.append(PassageDecision(evidence_id=evidence_id, decision=decision, resolved_by=resolved_by))
    return _ContextAssessment(
        decision=combine_decisions(item.decision for item in passages),
        passages=tuple(passages),
        comparisons=tuple(comparisons),
        applied=tuple(applied),
    )


def derive_verdict(
    reference_decision: SemanticDecision,
    context_decision: SemanticDecision,
) -> tuple[ClaimVerdict, str]:
    if reference_decision is SemanticDecision.CONTRADICTED:
        return ClaimVerdict.CONTRADICTED, "claim conflicts with a verified reference fact"
    if reference_decision is SemanticDecision.SUPPORTED:
        return ClaimVerdict.CORRECT, "claim matches a verified reference fact"
    if reference_decision is SemanticDecision.NEEDS_REVIEW:
        return ClaimVerdict.UNRESOLVED, "reference comparison requires semantic judgment"
    if context_decision is SemanticDecision.SUPPORTED:
        return ClaimVerdict.GROUNDED_EXTRA, "claim is outside the reference but grounded in actual context"
    if context_decision is SemanticDecision.CONTRADICTED:
        return ClaimVerdict.CONTRADICTED, "claim is outside the reference and conflicts with actual context"
    if context_decision is SemanticDecision.UNSUPPORTED:
        return ClaimVerdict.OVER_INFERENCE, "claim is supported by neither the reference nor actual context"
    return ClaimVerdict.UNRESOLVED, "context comparison requires semantic judgment"


def index_resolutions(resolutions: Sequence[SemanticResolution]) -> dict[
    tuple[str, ComparisonTargetKind, str], SemanticResolution
]:
    result: dict[tuple[str, ComparisonTargetKind, str], SemanticResolution] = {}
    for item in resolutions:
        if item.key in result:
            raise ValueError("one pending pair may have only one resolution")
        result[item.key] = item
    return result


__all__ = [
    "ClaimFactAssessment",
    "ClaimVerdict",
    "ComparisonTargetKind",
    "PassageDecision",
    "PendingJudgment",
    "ResolvedDimension",
    "SemanticDecision",
    "SemanticResolution",
    "TargetComparison",
    "assess_claim",
    "combine_decisions",
    "derive_verdict",
    "index_resolutions",
]
