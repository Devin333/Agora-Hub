from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from backend.research.ports.fact_entailment_judge import (
    DimensionJudgment,
    EntailmentLabel,
    FactEntailmentJudgment,
    FactEntailmentRequest,
    FactJudgeError,
    FactJudgeIdentity,
    JudgedDimensionOutcome,
    JudgeTargetKind,
    TargetJudgment,
)
from backend.research.rag.evaluation.protocol_answer_scoring import (
    CandidateFactProjection,
    ClaimVerdict,
    ComparisonTargetKind,
    EvidenceFactProjection,
    NumericUnit,
    ReferenceFactProjection,
    SemanticDecision,
    SemanticResolution,
    build_answer_metric_registry,
    score_answer,
)
from backend.research.rag.evaluation.protocol_judge_consensus import (
    build_entailment_requests,
    resolve_by_consensus,
)
from backend.research.rag.evaluation.protocol_scoring import ScoreSource, ScoreStatus
from tests.backend.research.rag.test_protocol_answer_scoring import (
    _candidate,
    _context,
    _fact,
    _identity,
    _reference,
    _source,
)

SOURCE_TEXT = "The model obtains 0.95 accuracy on test under seed 7."
CLAIM_TEXT = "Model A obtains 95% accuracy under seed 7."

Decide = Callable[[FactEntailmentRequest, str], EntailmentLabel]


class _Judge:
    def __init__(self, provider: str, decide: Decide, *, fail: bool = False,
                 mismatch_dimension: bool = False) -> None:
        self._identity = FactJudgeIdentity(
            provider=provider, model_id=f"{provider}-model", model_revision="r1", prompt_version="v1",
        )
        self._decide = decide
        self._fail = fail
        self._mismatch_dimension = mismatch_dimension
        self.requests: list[FactEntailmentRequest] = []

    @property
    def identity(self) -> FactJudgeIdentity:
        return self._identity

    async def judge(self, request: FactEntailmentRequest) -> FactEntailmentJudgment:
        self.requests.append(request)
        if self._fail:
            raise FactJudgeError("provider unavailable")
        judgments = []
        for target in request.targets:
            label = self._decide(request, target.target_id)
            dimensions = ()
            if request.target_kind is JudgeTargetKind.REFERENCE and label is not EntailmentLabel.NEUTRAL:
                dimensions = (
                    DimensionJudgment(dimension="numeric_value", outcome=JudgedDimensionOutcome.MATCH),
                    DimensionJudgment(
                        dimension="split",
                        outcome=JudgedDimensionOutcome.MISMATCH if self._mismatch_dimension
                        else JudgedDimensionOutcome.NOT_STATED,
                    ),
                    DimensionJudgment(dimension="condition", name="seed", outcome=JudgedDimensionOutcome.MATCH),
                )
            judgments.append(TargetJudgment(target_id=target.target_id, label=label, dimensions=dimensions))
        return FactEntailmentJudgment(request_id=request.request_id, judge=self._identity, judgments=tuple(judgments))


def _always(label: EntailmentLabel) -> Decide:
    return lambda request, target_id: label


def _inputs(*, with_split: bool = False):
    """A claim that omits the split, so deterministic comparison cannot decide it."""

    fact = _fact(95.0, NumericUnit.PERCENT)
    if not with_split:
        fact = fact.model_copy(update={"split_id": None})
    reference_fact = ReferenceFactProjection(
        reference_claim_id="ref-1",
        fact=_fact(0.95, NumericUnit.FRACTION),
        evidence_anchor_ids=("anchor-1",),
        rule_id="numeric-result",
        rule_version="1.0",
    )
    candidate_fact = CandidateFactProjection(
        answer_claim_id="claim-1",
        reference_claim_id="ref-1",
        fact=fact,
        rule_id="numeric-result",
        rule_version="1.0",
    )
    evidence_fact = EvidenceFactProjection(
        evidence_id="ev-1",
        fact=_fact(0.95, NumericUnit.FRACTION),
        source=_source(SOURCE_TEXT),
        rule_id="numeric-result",
        rule_version="1.0",
    )
    return dict(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(CLAIM_TEXT, claim_text=CLAIM_TEXT),
        actual_context=_context(SOURCE_TEXT),
        reference_facts=(reference_fact,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence_fact,),
    )


def _consensus(inputs, judges):
    first = score_answer(**inputs)
    requests = build_entailment_requests(
        first.pending_judgments(),
        candidate=inputs["candidate"],
        reference=inputs["reference"],
        actual_context=inputs["actual_context"],
    )
    resolutions, report = asyncio.run(resolve_by_consensus(requests, judges))
    return first, requests, resolutions, report


def test_pending_pairs_become_requests_with_exact_target_texts() -> None:
    first = score_answer(**_inputs())
    pending = {(item.target_kind, item.target_id) for item in first.pending_judgments()}
    assert pending == {(ComparisonTargetKind.REFERENCE, "ref-1"), (ComparisonTargetKind.EVIDENCE, "ev-1")}
    inputs = _inputs()
    requests = build_entailment_requests(
        first.pending_judgments(),
        candidate=inputs["candidate"],
        reference=inputs["reference"],
        actual_context=inputs["actual_context"],
    )
    by_kind = {item.target_kind: item for item in requests}
    assert by_kind[JudgeTargetKind.REFERENCE].targets[0].text == "Reference claim 1"
    assert by_kind[JudgeTargetKind.REFERENCE].targets[0].supporting_text == ("Verified source excerpt 1.",)
    assert by_kind[JudgeTargetKind.EVIDENCE].targets[0].text == SOURCE_TEXT


def test_decided_pairs_are_never_sent_to_judges() -> None:
    first = score_answer(**_inputs(with_split=True))
    assert first.pending_judgments() == ()


def test_unanimous_judges_resolve_pending_pairs_and_attribute_the_source() -> None:
    inputs = _inputs()
    judges = [_Judge("openai", _always(EntailmentLabel.ENTAILED)), _Judge("dashscope", _always(EntailmentLabel.ENTAILED))]
    first, _, resolutions, report = _consensus(inputs, judges)
    assert first.fact_precision.status is ScoreStatus.NEEDS_REVIEW
    assert report.resolved_pairs == 2 and report.agreement_rate == 1.0

    final = score_answer(**inputs, resolutions=resolutions)
    assessment = final.claim_assessments[0]
    assert assessment.verdict is ClaimVerdict.CORRECT
    assert final.fact_precision.value == 1.0
    assert final.fact_precision.source is ScoreSource.SEMANTIC_JUDGE
    assert final.faithfulness.value == 1.0
    assert final.faithfulness.source is ScoreSource.SEMANTIC_JUDGE
    # numeric and seed were stated and matched; the split was not stated.
    assert (final.numeric_accuracy.numerator, final.numeric_accuracy.denominator) == (1, 1)
    assert (final.condition_accuracy.numerator, final.condition_accuracy.denominator) == (1, 1)
    assert final.pending_judgments() == ()
    scores = final.to_evaluation_scores(_identity())
    registry = build_answer_metric_registry()
    assert all(registry.validate_score(score) for score in scores)
    precision = next(item for item in scores if item.metric_id == "answer.fact_precision")
    assert precision.source is ScoreSource.SEMANTIC_JUDGE
    assert precision.provenance["reference_resolutions"] == 1


def test_disagreement_keeps_the_pair_pending() -> None:
    inputs = _inputs()
    judges = [_Judge("openai", _always(EntailmentLabel.ENTAILED)), _Judge("dashscope", _always(EntailmentLabel.NEUTRAL))]
    _, _, resolutions, report = _consensus(inputs, judges)
    assert resolutions == ()
    assert len(report.disagreements) == 2
    assert report.agreement_rate == 0.0
    final = score_answer(**inputs, resolutions=resolutions)
    assert final.fact_precision.status is ScoreStatus.NEEDS_REVIEW


def test_dimension_disagreement_is_undetermined_not_dropped() -> None:
    inputs = _inputs()
    judges = [
        _Judge("openai", _always(EntailmentLabel.CONTRADICTED), mismatch_dimension=True),
        _Judge("dashscope", _always(EntailmentLabel.CONTRADICTED)),
    ]
    _, _, resolutions, _ = _consensus(inputs, judges)
    reference = next(item for item in resolutions if item.target_kind is ComparisonTargetKind.REFERENCE)
    split = next(item for item in reference.dimensions if item.dimension.value == "split")
    assert split.outcome.value == "undetermined"
    final = score_answer(**inputs, resolutions=resolutions)
    assert final.claim_assessments[0].verdict is ClaimVerdict.CONTRADICTED
    assert final.condition_accuracy.status is ScoreStatus.NEEDS_REVIEW


def test_a_failed_judge_leaves_its_requests_unjudged() -> None:
    inputs = _inputs()
    judges = [_Judge("openai", _always(EntailmentLabel.ENTAILED)), _Judge("dashscope", _always(EntailmentLabel.ENTAILED), fail=True)]
    _, _, resolutions, report = _consensus(inputs, judges)
    assert resolutions == ()
    assert report.unjudged_pairs == 2 and len(report.failures) == 2


def test_consensus_requires_independent_providers() -> None:
    same = [_Judge("openai", _always(EntailmentLabel.ENTAILED)), _Judge("OpenAI", _always(EntailmentLabel.ENTAILED))]
    with pytest.raises(ValueError, match="distinct providers"):
        asyncio.run(resolve_by_consensus((), same))
    with pytest.raises(ValueError, match="at least two"):
        asyncio.run(resolve_by_consensus((), same[:1]))


def test_resolution_for_an_already_decided_pair_is_rejected() -> None:
    stale = SemanticResolution(
        claim_id="claim-1",
        target_kind=ComparisonTargetKind.REFERENCE,
        target_id="ref-1",
        decision=SemanticDecision.SUPPORTED,
        resolver="fixture",
        evidence=("judgment://fixture",),
    )
    with pytest.raises(ValueError, match="not pending"):
        score_answer(**_inputs(with_split=True), resolutions=(stale,))


def test_claim_without_a_projection_can_still_be_judged() -> None:
    inputs = {**_inputs(), "candidate_facts": ()}
    judges = [_Judge("openai", _always(EntailmentLabel.NEUTRAL)), _Judge("dashscope", _always(EntailmentLabel.NEUTRAL))]
    first, _, resolutions, _ = _consensus(inputs, judges)
    assert first.claim_assessments[0].verdict is ClaimVerdict.UNRESOLVED
    final = score_answer(**inputs, resolutions=resolutions)
    assert final.claim_assessments[0].verdict is ClaimVerdict.OVER_INFERENCE
    assert final.unsupported_inference_rate.value == 1.0


def test_grounded_scoring_without_reference_decides_only_context_metrics() -> None:
    from backend.research.rag.evaluation.protocol_answer_scoring import score_grounded_answer

    inputs = _inputs(with_split=True)
    unbound = inputs["candidate_facts"][0].model_copy(update={"reference_claim_id": None})
    result = score_grounded_answer(
        case_id="report-1",
        candidate=inputs["candidate"],
        actual_context=inputs["actual_context"],
        context_kind="cited_quotes",
        candidate_facts=(unbound,),
        evidence_facts=inputs["evidence_facts"],
    )
    assert result.context_kind == "cited_quotes"
    assert result.claim_assessments[0].verdict is ClaimVerdict.GROUNDED_EXTRA
    assert result.faithfulness.value == 1.0
    assert result.unsupported_inference_rate.value == 0.0
    for metric in ("answer.fact_precision", "answer.fact_recall", "answer.numeric_accuracy"):
        assert result.observation(metric).status is ScoreStatus.NOT_APPLICABLE
    assert all(item.target_kind is ComparisonTargetKind.EVIDENCE for item in result.pending_judgments())


def test_grounded_scoring_rejects_reference_alignment() -> None:
    from backend.research.rag.evaluation.protocol_answer_scoring import score_grounded_answer

    inputs = _inputs()
    with pytest.raises(ValueError, match="no reference exists"):
        score_grounded_answer(
            case_id="report-1",
            candidate=inputs["candidate"],
            actual_context=inputs["actual_context"],
            context_kind="cited_quotes",
            candidate_facts=inputs["candidate_facts"],
            evidence_facts=inputs["evidence_facts"],
        )
