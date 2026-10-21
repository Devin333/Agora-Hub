"""Multi-provider consensus for pairs deterministic fact comparison left pending.

For one scored answer this module builds exactly the judgment requests its
pending pairs need, asks every configured judge, and turns only unanimous
labels into ``SemanticResolution`` records. A pair whose judges disagree, or
where any judge failed, stays pending and is reported, never guessed.

Consensus is only trusted when judges are independent: at least two judges from
distinct providers are required.
"""
from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence

from pydantic import Field, model_validator

from backend.research.ports.fact_entailment_judge import (
    EntailmentLabel,
    EntailmentTarget,
    FactEntailmentJudgePort,
    FactEntailmentJudgment,
    FactEntailmentRequest,
    FactJudgeError,
    JudgedDimensionOutcome,
    JudgeTargetKind,
)
from backend.research.rag.evaluation.protocol_claim_assessment import (
    ComparisonTargetKind,
    PendingJudgment,
    ResolvedDimension,
    SemanticDecision,
    SemanticResolution,
)
from backend.research.rag.evaluation.protocol_common import NonEmptyStr, ProtocolModel
from backend.research.rag.evaluation.protocol_dataset import ReferenceLabel
from backend.research.rag.evaluation.protocol_semantic_facts import DimensionOutcome, FactDimension
from framework.harness.rag.models import GroundedAnswerCandidate, RAGContextPack

JUDGE_CONSENSUS_RULE_VERSION = "judge-consensus/v1"

_LABEL_DECISION: dict[EntailmentLabel, SemanticDecision] = {
    EntailmentLabel.ENTAILED: SemanticDecision.SUPPORTED,
    EntailmentLabel.CONTRADICTED: SemanticDecision.CONTRADICTED,
    EntailmentLabel.NEUTRAL: SemanticDecision.UNSUPPORTED,
}
_KIND: dict[ComparisonTargetKind, JudgeTargetKind] = {
    ComparisonTargetKind.REFERENCE: JudgeTargetKind.REFERENCE,
    ComparisonTargetKind.EVIDENCE: JudgeTargetKind.EVIDENCE,
}
_DIMENSION: dict[str, FactDimension] = {
    "numeric_value": FactDimension.NUMERIC_VALUE,
    "unit": FactDimension.UNIT,
    "dataset": FactDimension.DATASET,
    "split": FactDimension.SPLIT,
    "condition": FactDimension.CONDITION,
}
_OUTCOME: dict[JudgedDimensionOutcome, DimensionOutcome] = {
    JudgedDimensionOutcome.MATCH: DimensionOutcome.MATCH,
    JudgedDimensionOutcome.MISMATCH: DimensionOutcome.MISMATCH,
}


class ConsensusDisagreement(ProtocolModel):
    claim_id: NonEmptyStr
    target_kind: ComparisonTargetKind
    target_id: NonEmptyStr
    labels: dict[NonEmptyStr, EntailmentLabel]


class ConsensusFailure(ProtocolModel):
    request_id: NonEmptyStr
    judge: NonEmptyStr
    reason: NonEmptyStr


class ConsensusReport(ProtocolModel):
    rule_version: NonEmptyStr = JUDGE_CONSENSUS_RULE_VERSION
    judges: tuple[NonEmptyStr, ...] = Field(min_length=2)
    requests: int = Field(ge=0, strict=True)
    judge_calls: int = Field(ge=0, strict=True)
    pending_pairs: int = Field(ge=0, strict=True)
    resolved_pairs: int = Field(ge=0, strict=True)
    disagreements: tuple[ConsensusDisagreement, ...] = ()
    failures: tuple[ConsensusFailure, ...] = ()
    unjudged_pairs: int = Field(ge=0, strict=True)
    label_counts: dict[NonEmptyStr, dict[NonEmptyStr, int]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _accounted(self) -> "ConsensusReport":
        if self.resolved_pairs + len(self.disagreements) + self.unjudged_pairs != self.pending_pairs:
            raise ValueError("every pending pair must be resolved, disagreed on, or unjudged")
        return self

    @property
    def agreement_rate(self) -> float | None:
        judged = self.resolved_pairs + len(self.disagreements)
        return None if judged == 0 else self.resolved_pairs / judged


def build_entailment_requests(
    pending: Sequence[PendingJudgment],
    *,
    candidate: GroundedAnswerCandidate,
    reference: ReferenceLabel,
    actual_context: RAGContextPack | None,
) -> tuple[FactEntailmentRequest, ...]:
    """Group pending pairs into one request per (claim, target kind)."""

    claims = {item.claim_id: item for item in candidate.claims}
    reference_claims = {item.claim_id: item for item in reference.reference_claims}
    passages = (
        {item.evidence_id: item for item in actual_context.accepted_evidence}
        if actual_context is not None else {}
    )
    grouped: dict[tuple[str, ComparisonTargetKind], list[str]] = defaultdict(list)
    for item in pending:
        targets = grouped[(item.claim_id, item.target_kind)]
        if item.target_id not in targets:
            targets.append(item.target_id)
    requests: list[FactEntailmentRequest] = []
    for (claim_id, kind), target_ids in grouped.items():
        claim = claims.get(claim_id)
        if claim is None:
            raise ValueError(f"pending judgment references unknown answer claim {claim_id!r}")
        targets: list[EntailmentTarget] = []
        for target_id in target_ids:
            if kind is ComparisonTargetKind.REFERENCE:
                reference_claim = reference_claims.get(target_id)
                if reference_claim is None:
                    raise ValueError(f"pending judgment references unknown reference claim {target_id!r}")
                excerpts = tuple(
                    anchor.source_excerpt for anchor in reference.evidence_anchors
                    if target_id in anchor.claim_ids
                )
                targets.append(EntailmentTarget(
                    target_id=target_id, text=reference_claim.claim_text, supporting_text=excerpts,
                ))
            else:
                passage = passages.get(target_id)
                if passage is None:
                    raise ValueError(f"pending judgment references unknown passage {target_id!r}")
                # The exact text the answering model was shown, never the full source.
                targets.append(EntailmentTarget(target_id=target_id, text=passage.summary))
        requests.append(FactEntailmentRequest(
            request_id=f"{claim_id}::{kind.value}",
            question=candidate.question,
            claim_id=claim_id,
            claim_text=claim.text,
            target_kind=_KIND[kind],
            targets=tuple(targets),
        ))
    return tuple(requests)


def _validated(
    request: FactEntailmentRequest,
    judgment: FactEntailmentJudgment,
) -> str | None:
    """Why a judgment cannot be used for its request, or None when it can."""

    if judgment.request_id != request.request_id:
        return "judgment answers a different request"
    expected = {item.target_id for item in request.targets}
    actual = {item.target_id for item in judgment.judgments}
    if actual != expected:
        return "judgment does not label exactly the requested targets"
    if request.target_kind is JudgeTargetKind.EVIDENCE and any(
        item.dimensions for item in judgment.judgments
    ):
        return "dimension outcomes are only judged against reference claims"
    return None


def _merge_dimensions(
    per_judge: Sequence[Mapping[tuple[str, str | None], JudgedDimensionOutcome]],
) -> tuple[ResolvedDimension, ...]:
    """Keep a dimension outcome only where every judge agrees.

    A dimension one judge omits counts as ``not_stated`` for that judge, so a
    partial report becomes undetermined instead of silently shrinking counts.
    """

    keys = sorted({key for item in per_judge for key in item}, key=lambda key: (key[0], key[1] or ""))
    merged: list[ResolvedDimension] = []
    for dimension, name in keys:
        outcomes = {
            item.get((dimension, name), JudgedDimensionOutcome.NOT_STATED) for item in per_judge
        }
        if outcomes == {JudgedDimensionOutcome.NOT_STATED}:
            continue
        outcome = (
            _OUTCOME[next(iter(outcomes))] if len(outcomes) == 1 else DimensionOutcome.UNDETERMINED
        )
        merged.append(ResolvedDimension(dimension=_DIMENSION[dimension], outcome=outcome, name=name))
    return tuple(merged)


async def _ask(
    judge: FactEntailmentJudgePort,
    request: FactEntailmentRequest,
    semaphore: asyncio.Semaphore,
) -> FactEntailmentJudgment | str:
    async with semaphore:
        try:
            judgment = await judge.judge(request)
        except FactJudgeError as exc:
            return f"judge failed: {exc}"
    return _validated(request, judgment) or judgment


async def resolve_by_consensus(
    requests: Sequence[FactEntailmentRequest],
    judges: Sequence[FactEntailmentJudgePort],
    *,
    max_concurrency: int = 4,
) -> tuple[tuple[SemanticResolution, ...], ConsensusReport]:
    """Resolve pending pairs only where all independent judges agree."""

    if len(judges) < 2:
        raise ValueError("consensus requires at least two judges")
    keys = [judge.identity.key for judge in judges]
    providers = [judge.identity.provider.casefold() for judge in judges]
    if len(set(providers)) != len(providers):
        raise ValueError("consensus judges must come from distinct providers")
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")
    semaphore = asyncio.Semaphore(max_concurrency)
    answers = await asyncio.gather(*(
        asyncio.gather(*(_ask(judge, request, semaphore) for judge in judges))
        for request in requests
    ))
    resolver = f"{JUDGE_CONSENSUS_RULE_VERSION}:" + "|".join(sorted(keys))
    resolutions: list[SemanticResolution] = []
    disagreements: list[ConsensusDisagreement] = []
    failures: list[ConsensusFailure] = []
    label_counts: dict[str, Counter[str]] = {key: Counter() for key in keys}
    pending_pairs = unjudged = 0
    for request, per_request in zip(requests, answers, strict=True):
        pending_pairs += len(request.targets)
        failed = [
            (key, item) for key, item in zip(keys, per_request, strict=True) if isinstance(item, str)
        ]
        for key, reason in failed:
            failures.append(ConsensusFailure(request_id=request.request_id, judge=key, reason=reason))
        if failed:
            unjudged += len(request.targets)
            continue
        judgments = [item for item in per_request if not isinstance(item, str)]
        kind = (
            ComparisonTargetKind.REFERENCE if request.target_kind is JudgeTargetKind.REFERENCE
            else ComparisonTargetKind.EVIDENCE
        )
        for target in request.targets:
            labels: dict[str, EntailmentLabel] = {}
            dimensions: list[dict[tuple[str, str | None], JudgedDimensionOutcome]] = []
            for key, judgment in zip(keys, judgments, strict=True):
                item = next(entry for entry in judgment.judgments if entry.target_id == target.target_id)
                labels[key] = item.label
                label_counts[key][item.label.value] += 1
                dimensions.append({(dim.dimension, dim.name): dim.outcome for dim in item.dimensions})
            if len(set(labels.values())) != 1:
                disagreements.append(ConsensusDisagreement(
                    claim_id=request.claim_id, target_kind=kind, target_id=target.target_id, labels=labels,
                ))
                continue
            decision = _LABEL_DECISION[next(iter(labels.values()))]
            keep_dimensions = (
                kind is ComparisonTargetKind.REFERENCE
                and decision in (SemanticDecision.SUPPORTED, SemanticDecision.CONTRADICTED)
            )
            resolutions.append(SemanticResolution(
                claim_id=request.claim_id,
                target_kind=kind,
                target_id=target.target_id,
                decision=decision,
                dimensions=_merge_dimensions(dimensions) if keep_dimensions else (),
                resolver=resolver,
                evidence=tuple(
                    f"judgment://{key}/{request.request_id}/{target.target_id}" for key in sorted(keys)
                ),
            ))
    report = ConsensusReport(
        judges=tuple(sorted(keys)),
        requests=len(requests),
        judge_calls=len(requests) * len(judges),
        pending_pairs=pending_pairs,
        resolved_pairs=len(resolutions),
        disagreements=tuple(disagreements),
        failures=tuple(failures),
        unjudged_pairs=unjudged,
        label_counts={key: dict(value) for key, value in label_counts.items()},
    )
    return tuple(resolutions), report


__all__ = [
    "JUDGE_CONSENSUS_RULE_VERSION",
    "ConsensusDisagreement",
    "ConsensusFailure",
    "ConsensusReport",
    "build_entailment_requests",
    "resolve_by_consensus",
]
