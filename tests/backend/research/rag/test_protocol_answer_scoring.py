from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_dataset import (
    AlternativeSufficientSet,
    AuthorInputManifest,
    CaseProvenance,
    EvidenceAnchor,
    EvidenceLocation,
    HumanReview,
    QuestionScope,
    ReferenceClaim,
    ReferenceLabel,
    RequiredEvidenceGroup,
    ScopedPaper,
    SourceRecord,
)
from backend.research.rag.evaluation.protocol_answer_scoring import (
    AbstentionClassification,
    AnswerScoreIdentity,
    CandidateFactProjection,
    CitationParseState,
    EvidenceFactProjection,
    ExactSourceExcerpt,
    NumericUnit,
    ReferenceFactProjection,
    SemanticCondition,
    SemanticFact,
    aggregate_abstention_metrics,
    build_answer_metric_registry,
    classify_abstention,
    score_answer,
)
from backend.research.rag.evaluation.protocol_scoring import ScoreStatus
from framework.harness.rag.models import (
    AnswerClaim,
    EvidenceCandidate,
    GroundedAnswerCandidate,
    RAGContextPack,
)

SOURCE_SHA = "a" * 64
DATASET_SHA = "b" * 64
VARIANT_SHA = "c" * 64
NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
SCOPE = QuestionScope(
    sources=(ScopedPaper(paper_id="paper-1", document_version="v1", source_sha256=SOURCE_SHA),)
)


def _reference(answerability: str = "answerable", *, claim_count: int = 1) -> ReferenceLabel:
    question_manifest = AuthorInputManifest(
        visible_materials=("title", "abstract", "declared_scope"),
        input_digest="d" * 64,
    )
    annotation_manifest = AuthorInputManifest(
        visible_materials=("fulltext", "declared_scope"),
        input_digest="e" * 64,
    )
    provenance = CaseProvenance(
        authoring_method="abstract_only",
        original_dataset_id=None,
        original_case_id=None,
        derived_from_case_ids=(),
        question_author_id="author-1",
        author_input_manifest=question_manifest,
        question_digest="f" * 64,
        question_frozen_at=datetime(2026, 10, 13, tzinfo=timezone.utc),
        annotator_id="annotator-1",
        annotation_input_manifest=annotation_manifest,
        annotation_started_at=NOW,
        assistance=(),
        sources=(SourceRecord(
            paper_id="paper-1",
            document_version="v1",
            source_uri="artifact://paper-1/source",
            source_sha256=SOURCE_SHA,
            retrieved_at=NOW,
            fulltext_artifact_digest="1" * 64,
            completeness="complete",
            access_status="available",
            source_format="pdf",
            license_reference_id="license-1",
        ),),
    )
    review_status = "needs_review" if answerability == "needs_review" else "approved"
    review = HumanReview(
        status=review_status,
        reviewer_id=None if review_status == "needs_review" else "reviewer-1",
        reviewer_kind=None if review_status == "needs_review" else "human",
        reviewed_at=None if review_status == "needs_review" else NOW,
        notes="frozen answerability review",
        source_completeness="complete" if review_status == "approved" else "unknown",
        checked_scope=SCOPE if review_status == "approved" else None,
        unanswerable_reason="full paper checked" if answerability == "unanswerable" else None,
        evidence_reference="review://case-1" if review_status == "approved" else None,
    )
    if answerability != "answerable":
        return ReferenceLabel(
            answerability=answerability,
            expected_behavior="abstain" if answerability == "unanswerable" else None,
            reference_answer=None,
            reference_claims=(),
            evidence_anchors=(),
            required_evidence_groups=(),
            provenance=provenance,
            review=review,
        )
    claims = tuple(
        ReferenceClaim(claim_id=f"ref-{index}", claim_text=f"Reference claim {index}")
        for index in range(1, claim_count + 1)
    )
    anchors = []
    groups = []
    for index, claim in enumerate(claims, start=1):
        excerpt = f"Verified source excerpt {index}."
        location = EvidenceLocation(
            content_type="text",
            pdf_page_index=index,
            printed_page=None,
            section="Results",
            paragraph_id=None,
            char_start=None,
            char_end=None,
            element_id=None,
            table_id=None,
            row_labels=(),
            column_labels=(),
            cell_references=(),
            figure_id=None,
            caption=None,
            normalized_fulltext_sha256=None,
        )
        anchors.append(EvidenceAnchor(
            anchor_id=f"anchor-{index}",
            paper_id="paper-1",
            document_version="v1",
            source_sha256=SOURCE_SHA,
            claim_ids=(claim.claim_id,),
            location=location,
            source_excerpt=excerpt,
            source_excerpt_digest=content_digest(excerpt),
        ))
        groups.append(RequiredEvidenceGroup(
            group_id=f"group-{index}",
            claim_ids=(claim.claim_id,),
            alternative_sufficient_sets=(AlternativeSufficientSet(
                set_id=f"set-{index}",
                required_anchor_ids=(f"anchor-{index}",),
            ),),
        ))
    return ReferenceLabel(
        answerability="answerable",
        expected_behavior="answer",
        reference_answer="Reviewed answer",
        reference_claims=claims,
        evidence_anchors=tuple(anchors),
        required_evidence_groups=tuple(groups),
        provenance=provenance,
        review=review,
    )


def _candidate(
    text: str,
    *,
    claim_text: str | None = None,
    evidence_ids: tuple[str, ...] = ("ev-1",),
    abstained: bool = False,
) -> GroundedAnswerCandidate:
    claims = () if claim_text is None else (
        AnswerClaim(
            claim_id="claim-1",
            text=claim_text,
            evidence_ids=evidence_ids,
            span_refs=("span-1",),
        ),
    )
    return GroundedAnswerCandidate(
        answer_id="answer-1",
        question="What accuracy was reported?",
        answer_text=text,
        cited_evidence_ids=evidence_ids,
        claims=claims,
        abstained=abstained,
        metadata={},
    )


def _context(source_text: str = "The model obtains 0.95 accuracy on test under seed 7.") -> RAGContextPack:
    evidence = EvidenceCandidate(
        evidence_id="ev-1",
        title="Results",
        summary=source_text,
        source_ref="paper://paper-1/pdf#page=2",
        span_refs=("span-1",),
        evidence_type="result",
        confidence=1.0,
        lineage=("test",),
        metadata={
            "paper_id": "paper-1",
            "document_version": "v1",
            "source_sha256": SOURCE_SHA,
            "source_locator": "paper://paper-1/pdf#page=2",
        },
    )
    return RAGContextPack(
        pack_id="pack-1",
        query="What accuracy was reported?",
        accepted_evidence=(evidence,),
        gap_report={},
        metadata={},
    )


def _source(source_text: str) -> ExactSourceExcerpt:
    return ExactSourceExcerpt(
        context_pack_id="pack-1",
        evidence_id="ev-1",
        paper_id="paper-1",
        document_version="v1",
        source_sha256=SOURCE_SHA,
        source_locator="paper://paper-1/pdf#page=2",
        location=None,
        content_type="text",
        source_text=source_text,
        source_text_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
        quote_start=0,
        quote_end=len(source_text),
        quote_text=source_text,
        quote_sha256=hashlib.sha256(source_text.encode()).hexdigest(),
    )


def _fact(
    value: float,
    unit: NumericUnit,
    *,
    seed: str = "7",
    modality: str = "text",
) -> SemanticFact:
    return SemanticFact(
        numeric_value=value,
        unit=unit,
        tolerance=0.0001,
        object_id="model-a",
        dataset_id="dataset-a",
        split_id="test",
        statistic="accuracy",
        conditions=(SemanticCondition(name="seed", value=seed),),
        modality=modality,
    )


def _projections(
    candidate_fact: SemanticFact,
    evidence_fact: SemanticFact,
    source_text: str,
) -> tuple[ReferenceFactProjection, CandidateFactProjection, EvidenceFactProjection]:
    reference = ReferenceFactProjection(
        reference_claim_id="ref-1",
        fact=_fact(0.95, NumericUnit.FRACTION),
        evidence_anchor_ids=("anchor-1",),
        rule_id="numeric-result",
        rule_version="1.0",
    )
    candidate = CandidateFactProjection(
        answer_claim_id="claim-1",
        reference_claim_id="ref-1",
        fact=candidate_fact,
        rule_id="numeric-result",
        rule_version="1.0",
    )
    evidence = EvidenceFactProjection(
        evidence_id="ev-1",
        fact=evidence_fact,
        source=_source(source_text),
        rule_id="numeric-result",
        rule_version="1.0",
    )
    return reference, candidate, evidence


def _identity() -> AnswerScoreIdentity:
    return AnswerScoreIdentity(
        dataset_id="research-dev",
        dataset_version="2026-10-14",
        dataset_digest=DATASET_SHA,
        run_id="run-real-1",
        case_id="case-1",
        attempt_id="attempt-1",
        track="end_to_end",
        execution_profile="production",
        variant_fingerprint=VARIANT_SHA,
        repetition=1,
        scope="full_task",
        scoring_revision="score-v1",
    )


def test_exact_citation_and_fraction_percent_conversion_are_supported() -> None:
    source_text = "The model obtains 0.95 accuracy on test under seed 7."
    claim_text = "Model A obtains 95% accuracy on the test split under seed 7."
    reference, candidate_fact, evidence = _projections(
        _fact(95.0, NumericUnit.PERCENT),
        _fact(0.95, NumericUnit.FRACTION),
        source_text,
    )
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context(source_text),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence,),
    )

    assert result.citation_bindings[0].state is CitationParseState.PARSED
    assert result.fact_precision.value == 1.0
    assert result.fact_completeness.value == 1.0
    assert result.faithfulness.value == 1.0
    assert result.citation_support.value == 1.0
    assert result.citation_coverage.value == 1.0

    scores = result.to_evaluation_scores(_identity())
    registry = build_answer_metric_registry()
    assert all(registry.validate_score(score) for score in scores)
    assert all(score.run_id == "run-real-1" for score in scores)


def test_same_number_under_wrong_condition_is_not_supported() -> None:
    source_text = "The model obtains 0.95 accuracy on test under seed 8."
    claim_text = "Model A obtains 95% accuracy on the test split under seed 7."
    reference, candidate_fact, evidence = _projections(
        _fact(95.0, NumericUnit.PERCENT, seed="7"),
        _fact(0.95, NumericUnit.FRACTION, seed="8"),
        source_text,
    )
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context(source_text),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence,),
    )

    assert result.fact_precision.value == 1.0
    assert result.faithfulness.value == 0.0
    assert result.citation_support.value == 0.0
    assert result.citation_coverage.value == 0.0


def test_percentage_points_and_relative_gain_are_not_absolute_percent() -> None:
    source_text = "The method improves accuracy by 5 percentage points."
    reference, candidate_fact, evidence = _projections(
        _fact(5.0, NumericUnit.RELATIVE_PERCENT),
        _fact(5.0, NumericUnit.PERCENTAGE_POINT),
        source_text,
    )
    claim_text = "Model A improves by 5% relative on the test split under seed 7."
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context(source_text),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence,),
    )
    assert result.faithfulness.value == 0.0
    assert result.citation_support.value == 0.0


def test_fact_correctness_is_independent_from_missing_context_and_citation_coverage() -> None:
    claim_text = "Model A obtains 95% accuracy on the test split under seed 7."
    reference, candidate_fact, _ = _projections(
        _fact(95.0, NumericUnit.PERCENT),
        _fact(0.95, NumericUnit.FRACTION),
        "unused",
    )
    empty_context = RAGContextPack(
        pack_id="pack-empty",
        query="q",
        accepted_evidence=(),
        gap_report={},
        metadata={},
    )
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text, evidence_ids=()),
        actual_context=empty_context,
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(),
    )
    assert result.fact_precision.value == 1.0
    assert result.fact_completeness.value == 1.0
    assert result.faithfulness.value == 0.0
    assert result.citation_support.status is ScoreStatus.NOT_APPLICABLE
    assert result.citation_coverage.value == 0.0


def test_faithful_answer_can_still_be_incomplete() -> None:
    source_text = "The model obtains 0.95 accuracy on test under seed 7."
    claim_text = "Model A obtains 95% accuracy on the test split under seed 7."
    reference, candidate_fact, evidence = _projections(
        _fact(95.0, NumericUnit.PERCENT),
        _fact(0.95, NumericUnit.FRACTION),
        source_text,
    )
    second_reference = ReferenceFactProjection(
        reference_claim_id="ref-2",
        fact=SemanticFact(
            exact_quote="Latency is 12 ms.",
            object_id="model-a",
            dataset_id="dataset-a",
            split_id="test",
            statistic="latency",
        ),
        evidence_anchor_ids=("anchor-2",),
        rule_id="exact-result",
        rule_version="1.0",
    )
    result = score_answer(
        case_id="case-1",
        reference=_reference(claim_count=2),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context(source_text),
        reference_facts=(reference, second_reference),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence,),
    )
    assert result.faithfulness.value == 1.0
    assert result.fact_completeness.value == 0.5


def test_same_paper_but_wrong_locator_needs_review_not_support() -> None:
    source_text = "The model obtains 0.95 accuracy on test under seed 7."
    claim_text = "Model A obtains 95% accuracy on the test split under seed 7."
    reference, candidate_fact, evidence = _projections(
        _fact(95.0, NumericUnit.PERCENT),
        _fact(0.95, NumericUnit.FRACTION),
        source_text,
    )
    wrong_source = evidence.source.model_copy(
        update={"source_locator": "paper://paper-1/pdf#page=99"}
    )
    wrong_evidence = evidence.model_copy(update={"source": wrong_source})
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context(source_text),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(wrong_evidence,),
    )
    assert result.citation_bindings[0].state is CitationParseState.NEEDS_REVIEW
    assert result.citation_support.status is ScoreStatus.NEEDS_REVIEW
    assert result.citation_coverage.status is ScoreStatus.NEEDS_REVIEW


def test_text_table_projection_cannot_claim_full_figure_scope() -> None:
    source_text = "Table 2 reports 0.95."
    reference, candidate_fact, evidence = _projections(
        _fact(0.95, NumericUnit.FRACTION, modality="figure"),
        _fact(0.95, NumericUnit.FRACTION, modality="table"),
        source_text,
    )
    claim_text = "Figure 2 shows 0.95 accuracy on the test split under seed 7."
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context(source_text),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence,),
    )
    assert result.faithfulness.status is ScoreStatus.NEEDS_REVIEW
    assert result.citation_support.status is ScoreStatus.NEEDS_REVIEW


def test_missing_semantic_projection_routes_to_review_not_string_similarity() -> None:
    text = "Reference claim 1"
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(text, claim_text=text, evidence_ids=()),
        actual_context=RAGContextPack(
            pack_id="pack-empty",
            query="q",
            accepted_evidence=(),
            gap_report={},
            metadata={},
        ),
    )
    assert result.fact_precision.status is ScoreStatus.NEEDS_REVIEW
    assert result.fact_completeness.status is ScoreStatus.NEEDS_REVIEW
    assert result.faithfulness.value == 0.0
    assert result.citation_coverage.value == 0.0


def _refusal(*, explicit: bool = True) -> GroundedAnswerCandidate:
    return GroundedAnswerCandidate(
        answer_id="refusal",
        question="q",
        answer_text="I cannot answer this based on the provided context.",
        cited_evidence_ids=(),
        claims=(),
        abstained=explicit,
        metadata={},
    )


def _answer() -> GroundedAnswerCandidate:
    text = "The answer is 42."
    return GroundedAnswerCandidate(
        answer_id="answered",
        question="q",
        answer_text=text,
        cited_evidence_ids=(),
        claims=(AnswerClaim(claim_id="c", text=text, evidence_ids=()),),
        abstained=False,
        metadata={},
    )


def test_abstention_metrics_use_distinct_three_of_eight_and_two_of_twelve_denominators() -> None:
    observations = []
    for index in range(8):
        observations.append(classify_abstention(
            case_id=f"negative-{index}",
            reference=_reference("unanswerable"),
            candidate=_refusal() if index < 3 else _answer(),
        ))
    for index in range(12):
        observations.append(classify_abstention(
            case_id=f"positive-{index}",
            reference=_reference("answerable"),
            candidate=_refusal() if index < 2 else _answer(),
        ))
    aggregate = aggregate_abstention_metrics(observations)
    recall, false_rate = aggregate.observations
    assert (recall.numerator, recall.denominator, recall.value) == (3, 8, 3 / 8)
    assert (false_rate.numerator, false_rate.denominator, false_rate.value) == (2, 12, 2 / 12)
    assert aggregate.planned == 20


def test_empty_and_runtime_failures_are_not_correct_refusals_and_remain_in_denominator() -> None:
    empty = GroundedAnswerCandidate(
        answer_id="empty",
        question="q",
        answer_text="",
        cited_evidence_ids=(),
        claims=(),
        abstained=True,
        metadata={},
    )
    observations = (
        classify_abstention(
            case_id="negative-empty",
            reference=_reference("unanswerable"),
            candidate=empty,
        ),
        classify_abstention(
            case_id="negative-runtime",
            reference=_reference("unanswerable"),
            candidate=None,
            runtime_error="timeout",
        ),
    )
    aggregate = aggregate_abstention_metrics(observations)
    recall = aggregate.observations[0]
    assert recall.value == 0.0
    assert recall.denominator == 2
    assert aggregate.negative_known_failure == 2
    assert aggregate.runtime_failures == 1


def test_zero_denominator_is_explicit_na_zero_over_zero() -> None:
    aggregate = aggregate_abstention_metrics(())
    for observation in aggregate.observations:
        assert observation.status is ScoreStatus.NOT_APPLICABLE
        assert (observation.numerator, observation.denominator) == (0, 0)
        assert observation.value is None


def test_partial_refusal_with_claim_is_unknown_and_ratio_stays_nullable() -> None:
    text = "I cannot answer this based on the provided context. However, the value is 42."
    candidate = GroundedAnswerCandidate(
        answer_id="partial",
        question="q",
        answer_text=text,
        cited_evidence_ids=(),
        claims=(AnswerClaim(claim_id="c", text="the value is 42", evidence_ids=()),),
        abstained=True,
        metadata={},
    )
    observation = classify_abstention(
        case_id="negative-partial",
        reference=_reference("unanswerable"),
        candidate=candidate,
    )
    assert observation.classification is AbstentionClassification.NEEDS_REVIEW
    aggregate = aggregate_abstention_metrics((observation,))
    metric = aggregate.observations[0]
    assert metric.status is ScoreStatus.NEEDS_REVIEW
    assert metric.value is None
    assert metric.numerator is None and metric.denominator is None
    assert aggregate.negative_unknown == 1


def test_correct_refusal_does_not_receive_vacuous_answer_metric_ones() -> None:
    result = score_answer(
        case_id="case-1",
        reference=_reference("unanswerable"),
        candidate=_refusal(),
        actual_context=RAGContextPack(
            pack_id="pack-empty",
            query="q",
            accepted_evidence=(),
            gap_report={},
            metadata={},
        ),
    )
    assert result.abstention.correct_for_negative is True
    for observation in result.observations:
        assert observation.status is ScoreStatus.NOT_APPLICABLE
        assert observation.value is None


def test_runtime_failure_is_model_error_not_judge_unknown() -> None:
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=None,
        actual_context=None,
        runtime_error="generation timeout",
    )
    assert result.abstention.runtime_failure is True
    assert all(item.status is ScoreStatus.ERROR for item in result.observations)
    assert all(item.provenance["failure_kind"] == "runtime_model_failure" for item in result.observations)


def test_text_refusal_prefix_with_conclusion_needs_review_without_native_flag() -> None:
    text = "I cannot answer this based on the provided context. However, the value is 42."
    candidate = GroundedAnswerCandidate(
        answer_id="partial-text",
        question="q",
        answer_text=text,
        cited_evidence_ids=(),
        claims=(AnswerClaim(claim_id="c", text="the value is 42", evidence_ids=()),),
        abstained=False,
        metadata={},
    )
    observation = classify_abstention(
        case_id="negative-partial-text",
        reference=_reference("unanswerable"),
        candidate=candidate,
    )
    assert observation.classification is AbstentionClassification.NEEDS_REVIEW


def test_unanswerable_na_scores_validate_without_fake_scalar_counts() -> None:
    result = score_answer(
        case_id="case-1",
        reference=_reference("unanswerable"),
        candidate=_refusal(),
        actual_context=RAGContextPack(
            pack_id="pack-empty",
            query="q",
            accepted_evidence=(),
            gap_report={},
            metadata={},
        ),
    )
    registry = build_answer_metric_registry()
    scores = result.to_evaluation_scores(_identity())
    assert all(registry.validate_score(score) for score in scores)
    f1 = next(score for score in scores if score.metric_id == "answer.fact_f1")
    assert f1.numerator is None and f1.denominator is None


def test_source_quote_absent_from_actual_context_cannot_be_borrowed_for_support() -> None:
    source_text = "The model obtains 0.95 accuracy on test under seed 7."
    claim_text = "Model A obtains 95% accuracy on the test split under seed 7."
    reference, candidate_fact, evidence = _projections(
        _fact(95.0, NumericUnit.PERCENT),
        _fact(0.95, NumericUnit.FRACTION),
        source_text,
    )
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text),
        actual_context=_context("The context was truncated before the result."),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
        evidence_facts=(evidence,),
    )
    assert result.fact_precision.value == 1.0
    assert result.faithfulness.status is ScoreStatus.NEEDS_REVIEW
    assert result.citation_support.status is ScoreStatus.NEEDS_REVIEW
    assert result.citation_coverage.status is ScoreStatus.NEEDS_REVIEW


def test_non_identical_exact_quote_wording_routes_to_review() -> None:
    source_text = "Accuracy reached ninety five percent."
    reference = ReferenceFactProjection(
        reference_claim_id="ref-1",
        fact=SemanticFact(
            exact_quote="Accuracy reached 95%.",
            object_id="model-a",
            dataset_id="dataset-a",
            split_id="test",
            statistic="accuracy",
        ),
        evidence_anchor_ids=("anchor-1",),
        rule_id="exact-result",
        rule_version="1.0",
    )
    candidate_fact = CandidateFactProjection(
        answer_claim_id="claim-1",
        reference_claim_id="ref-1",
        fact=SemanticFact(
            exact_quote="Accuracy reached ninety five percent.",
            object_id="model-a",
            dataset_id="dataset-a",
            split_id="test",
            statistic="accuracy",
        ),
        rule_id="exact-result",
        rule_version="1.0",
    )
    claim_text = "Accuracy reached ninety five percent."
    result = score_answer(
        case_id="case-1",
        reference=_reference(),
        candidate=_candidate(claim_text, claim_text=claim_text, evidence_ids=()),
        actual_context=RAGContextPack(
            pack_id="pack-empty",
            query="q",
            accepted_evidence=(),
            gap_report={},
            metadata={},
        ),
        reference_facts=(reference,),
        candidate_facts=(candidate_fact,),
    )
    assert result.fact_precision.status is ScoreStatus.NEEDS_REVIEW
    assert result.fact_completeness.status is ScoreStatus.NEEDS_REVIEW
