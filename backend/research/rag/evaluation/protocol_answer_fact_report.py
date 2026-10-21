"""Per-case and per-run results of automated answer fact evaluation.

A case is scored against every acceptable reference alternative and reports the
best one, mirroring how multi-annotator benchmarks are scored. Run metrics are
micro-averaged over cases whose selected observation is ``ok``; every other
status is counted, never dropped, so unknowns stay visible.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Literal

from pydantic import Field, model_validator

from backend.research.rag.evaluation.protocol_answer_scoring import (
    AbstentionAggregateResult,
    AnswerScoreResult,
    GroundedScoreResult,
    ReviewedClass,
    aggregate_abstention_metrics,
)
from backend.research.rag.evaluation.protocol_claim_decomposition import ClaimDecompositionReport
from backend.research.rag.evaluation.protocol_common import NonEmptyStr, ProtocolModel, content_digest
from backend.research.rag.evaluation.protocol_fact_extraction import FactVerificationReport
from backend.research.rag.evaluation.protocol_judge_calibration import JudgeCalibrationReport
from backend.research.rag.evaluation.protocol_judge_consensus import ConsensusReport
from backend.research.rag.evaluation.protocol_scoring import ScoreStatus

ANSWER_FACT_REPORT_VERSION = "answer-fact-report/v1"


class AlternativeOutcome(ProtocolModel):
    annotation_id: NonEmptyStr
    score: AnswerScoreResult
    reference_report: FactVerificationReport | None = None
    candidate_report: FactVerificationReport | None = None
    consensus: ConsensusReport | None = None


class AnswerFactCaseResult(ProtocolModel):
    case_id: NonEmptyStr
    question: NonEmptyStr
    status: Literal["scored", "runtime_error"]
    reason: NonEmptyStr | None = None
    decomposition: ClaimDecompositionReport | None = None
    decomposition_failure: NonEmptyStr | None = None
    evidence_report: FactVerificationReport | None = None
    judges_applied: bool = False
    outcomes: tuple[AlternativeOutcome, ...] = Field(min_length=1)
    selected_annotation_id: NonEmptyStr

    @model_validator(mode="after")
    def _selected_exists(self) -> "AnswerFactCaseResult":
        if self.selected_annotation_id not in {item.annotation_id for item in self.outcomes}:
            raise ValueError("selected alternative must be one of the scored outcomes")
        return self

    @property
    def selected(self) -> AlternativeOutcome:
        return next(item for item in self.outcomes if item.annotation_id == self.selected_annotation_id)


class GroundedCaseResult(ProtocolModel):
    """A reference-free case: the answer is judged only against its given context."""

    case_id: NonEmptyStr
    question: NonEmptyStr
    status: Literal["scored", "runtime_error", "not_scorable"]
    reason: NonEmptyStr | None = None
    decomposition: ClaimDecompositionReport | None = None
    decomposition_failure: NonEmptyStr | None = None
    evidence_report: FactVerificationReport | None = None
    candidate_report: FactVerificationReport | None = None
    consensus: ConsensusReport | None = None
    judges_applied: bool = False
    score: GroundedScoreResult | None = None

    @model_validator(mode="after")
    def _scored_has_score(self) -> "GroundedCaseResult":
        if (self.status == "scored") != (self.score is not None):
            raise ValueError("a scored case, and only a scored case, carries a score")
        if self.status != "scored" and self.reason is None:
            raise ValueError("an unscored case must state why")
        return self


def _value(score: AnswerScoreResult, metric_id: str, default: float) -> float:
    observation = score.observation(metric_id)  # type: ignore[arg-type]
    return observation.value if observation.status is ScoreStatus.OK and observation.value is not None else default


def select_best_alternative(outcomes: Sequence[AlternativeOutcome]) -> AlternativeOutcome:
    """The alternative the answer agrees with best, deterministically.

    Prefer an alternative whose answerability the answer's behaviour matches
    (answering an answerable question, abstaining on an unanswerable one), then
    a decided review, then higher fact F1, fewer contradictions, fewer unresolved
    claims; the annotation id breaks remaining ties.
    """

    if not outcomes:
        raise ValueError("at least one alternative outcome is required")

    def key(item: AlternativeOutcome) -> tuple[object, ...]:
        abstention = item.score.abstention
        matches = (
            (abstention.reviewed_class is ReviewedClass.POSITIVE and abstention.false_for_positive is False)
            or (abstention.reviewed_class is ReviewedClass.NEGATIVE and abstention.correct_for_negative is True)
        )
        return (
            not matches,
            abstention.reviewed_class is ReviewedClass.NEEDS_REVIEW,
            -_value(item.score, "answer.fact_f1", -1.0),
            _value(item.score, "answer.contradicted_fact_rate", 1.0),
            _value(item.score, "answer.unresolved_claim_rate", 1.0),
            item.annotation_id,
        )

    return min(outcomes, key=key)


class MetricAggregate(ProtocolModel):
    metric_id: NonEmptyStr
    ok_cases: int = Field(ge=0, strict=True)
    status_counts: dict[NonEmptyStr, int]
    numerator: int | None = None
    denominator: int | None = None
    value: float | None = None
    source_counts: dict[NonEmptyStr, int] = Field(default_factory=dict)


class StageTotals(ProtocolModel):
    targets: int = 0
    projected_targets: int = 0
    fixed_targets: int = 0
    drafts: int = 0
    accepted_drafts: int = 0
    rejections: int = 0


class AnswerFactRunReport(ProtocolModel):
    report_version: NonEmptyStr = ANSWER_FACT_REPORT_VERSION
    run_id: NonEmptyStr
    dataset_id: NonEmptyStr
    dataset_version: NonEmptyStr
    dataset_digest: NonEmptyStr
    cases: int = Field(ge=0, strict=True)
    runtime_errors: int = Field(ge=0, strict=True)
    metrics: tuple[MetricAggregate, ...]
    reference_mode: Literal["reference", "grounded"] = "reference"
    abstention: AbstentionAggregateResult | None = None
    judges_applied: bool
    calibration: JudgeCalibrationReport | None = None
    decomposition_proposed: int = 0
    decomposition_accepted: int = 0
    decomposition_failures: int = 0
    extraction: dict[NonEmptyStr, StageTotals] = Field(default_factory=dict)
    consensus_pending_pairs: int = 0
    consensus_resolved_pairs: int = 0
    consensus_disagreements: int = 0
    consensus_unjudged_pairs: int = 0
    digest: NonEmptyStr

    def verify_digest(self) -> None:
        if self.digest != content_digest(self.model_dump(mode="json", exclude={"digest"})):
            raise ValueError("answer fact run report digest mismatch")


def _aggregate_metric(
    metric_id: str, scores: Sequence[AnswerScoreResult | GroundedScoreResult],
) -> MetricAggregate:
    observations = [score.observation(metric_id) for score in scores]  # type: ignore[arg-type]
    statuses = Counter(item.status.value for item in observations)
    sources = Counter(item.source.value for item in observations if item.status is ScoreStatus.OK)
    ok = [item for item in observations if item.status is ScoreStatus.OK]
    if metric_id == "answer.fact_f1":
        value = sum(item.value or 0.0 for item in ok) / len(ok) if ok else None
        return MetricAggregate(
            metric_id=metric_id, ok_cases=len(ok), status_counts=dict(statuses),
            value=value, source_counts=dict(sources),
        )
    numerator = sum(item.numerator or 0 for item in ok)
    denominator = sum(item.denominator or 0 for item in ok)
    return MetricAggregate(
        metric_id=metric_id,
        ok_cases=len(ok),
        status_counts=dict(statuses),
        numerator=numerator,
        denominator=denominator,
        value=numerator / denominator if denominator else None,
        source_counts=dict(sources),
    )


def _add(totals: dict[str, StageTotals], report: FactVerificationReport | None) -> None:
    if report is None:
        return
    current = totals.setdefault(report.role.value, StageTotals())
    totals[report.role.value] = StageTotals(
        targets=current.targets + report.targets,
        projected_targets=current.projected_targets + report.projected_targets,
        fixed_targets=current.fixed_targets + report.fixed_targets,
        drafts=current.drafts + report.drafts,
        accepted_drafts=current.accepted_drafts + report.accepted_drafts,
        rejections=current.rejections + len(report.rejections),
    )


def summarize_answer_fact_run(
    results: Sequence[AnswerFactCaseResult],
    *,
    run_id: str,
    dataset_id: str,
    dataset_version: str,
    dataset_digest: str,
    calibration: JudgeCalibrationReport | None,
) -> AnswerFactRunReport:
    selected = [item.selected.score for item in results]
    metric_ids = [item.metric_id for item in selected[0].observations] if selected else []
    extraction: dict[str, StageTotals] = {}
    pending = resolved = disagreements = unjudged = 0
    for result in results:
        _add(extraction, result.evidence_report)
        outcome = result.selected
        _add(extraction, outcome.reference_report)
        _add(extraction, outcome.candidate_report)
        if outcome.consensus is not None:
            pending += outcome.consensus.pending_pairs
            resolved += outcome.consensus.resolved_pairs
            disagreements += len(outcome.consensus.disagreements)
            unjudged += outcome.consensus.unjudged_pairs
    draft = AnswerFactRunReport(
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_digest=dataset_digest,
        cases=len(results),
        runtime_errors=sum(item.status == "runtime_error" for item in results),
        metrics=tuple(_aggregate_metric(metric_id, selected) for metric_id in metric_ids),
        abstention=aggregate_abstention_metrics([score.abstention for score in selected]),
        judges_applied=any(item.judges_applied for item in results),
        calibration=calibration,
        decomposition_proposed=sum(item.decomposition.proposed for item in results if item.decomposition),
        decomposition_accepted=sum(item.decomposition.accepted for item in results if item.decomposition),
        decomposition_failures=sum(item.decomposition_failure is not None for item in results),
        extraction=extraction,
        consensus_pending_pairs=pending,
        consensus_resolved_pairs=resolved,
        consensus_disagreements=disagreements,
        consensus_unjudged_pairs=unjudged,
        digest="pending",
    )
    return draft.model_copy(update={
        "digest": content_digest(draft.model_dump(mode="json", exclude={"digest"})),
    })


def summarize_grounded_run(
    results: Sequence[GroundedCaseResult],
    *,
    run_id: str,
    dataset_id: str,
    dataset_version: str,
    dataset_digest: str,
    calibration: JudgeCalibrationReport | None,
) -> AnswerFactRunReport:
    scored = [item.score for item in results if item.score is not None]
    metric_ids = [item.metric_id for item in scored[0].observations] if scored else []
    extraction: dict[str, StageTotals] = {}
    pending = resolved = disagreements = unjudged = 0
    for result in results:
        _add(extraction, result.evidence_report)
        _add(extraction, result.candidate_report)
        if result.consensus is not None:
            pending += result.consensus.pending_pairs
            resolved += result.consensus.resolved_pairs
            disagreements += len(result.consensus.disagreements)
            unjudged += result.consensus.unjudged_pairs
    draft = AnswerFactRunReport(
        run_id=run_id,
        dataset_id=dataset_id,
        dataset_version=dataset_version,
        dataset_digest=dataset_digest,
        cases=len(results),
        runtime_errors=sum(item.status != "scored" for item in results),
        metrics=tuple(_aggregate_metric(metric_id, scored) for metric_id in metric_ids),
        reference_mode="grounded",
        abstention=None,
        judges_applied=any(item.judges_applied for item in results),
        calibration=calibration,
        decomposition_proposed=sum(item.decomposition.proposed for item in results if item.decomposition),
        decomposition_accepted=sum(item.decomposition.accepted for item in results if item.decomposition),
        decomposition_failures=sum(item.decomposition_failure is not None for item in results),
        extraction=extraction,
        consensus_pending_pairs=pending,
        consensus_resolved_pairs=resolved,
        consensus_disagreements=disagreements,
        consensus_unjudged_pairs=unjudged,
        digest="pending",
    )
    return draft.model_copy(update={
        "digest": content_digest(draft.model_dump(mode="json", exclude={"digest"})),
    })


__all__ = [
    "ANSWER_FACT_REPORT_VERSION",
    "AlternativeOutcome",
    "AnswerFactCaseResult",
    "AnswerFactRunReport",
    "GroundedCaseResult",
    "summarize_grounded_run",
    "MetricAggregate",
    "StageTotals",
    "select_best_alternative",
    "summarize_answer_fact_run",
]
