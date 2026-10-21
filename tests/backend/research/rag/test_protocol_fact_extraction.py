from __future__ import annotations

from backend.research.ports.answer_fact_extraction import (
    AlignmentTarget,
    ClaimDecompositionResult,
    ClaimDraft,
    DraftCondition,
    ExtractorIdentity,
    FactDraft,
    FactExtractionResult,
    FactSourceRole,
)
from backend.research.rag.evaluation.protocol_answer_scoring import ClaimVerdict, score_answer
from backend.research.rag.evaluation.protocol_claim_decomposition import (
    apply_claim_decomposition,
    citation_index_from_chunk_ids,
)
from backend.research.rag.evaluation.protocol_common import content_digest
from backend.research.rag.evaluation.protocol_dataset import (
    EvidenceAnchor,
    EvidenceLocation,
    ReferenceClaim,
    ReferenceLabel,
)
from backend.research.rag.evaluation.protocol_fact_extraction import (
    alignment_targets_from_reference,
    build_candidate_projections,
    build_evidence_projections,
    build_reference_projections,
    parse_value_text,
    stated_tolerance,
    verify_draft,
    vocabulary_from_reference,
)
from backend.research.rag.evaluation.protocol_scoring import ScoreStatus
from framework.harness.rag.models import GroundedAnswerCandidate
from tests.backend.research.rag.test_protocol_answer_scoring import (
    SOURCE_SHA,
    _context,
    _reference,
)

EXTRACTOR = ExtractorIdentity(
    provider="fixture", model_id="fixture-extractor", model_revision="r1", prompt_version="v1",
)
REFERENCE_TEXT = "Model A reaches 95.3% accuracy on the test split under seed 7."
EVIDENCE_TEXT = "The model obtains 0.953 accuracy on test under seed 7."


def _numeric_draft(statement: str, value_text: str, value: float, unit: str, **scope) -> FactDraft:
    return FactDraft(
        statement=statement,
        value_text=value_text,
        numeric_value=value,
        unit=unit,
        object_id=scope.get("object_id", "Model A"),
        dataset_id=scope.get("dataset_id", "dataset-a"),
        split_id=scope.get("split_id", "test"),
        statistic=scope.get("statistic", "Accuracy"),
        conditions=(DraftCondition(name="seed", value=scope.get("seed", "7")),),
        aligned_target_id=scope.get("aligned_target_id"),
    )


def _result(request_id: str, *drafts: FactDraft) -> FactExtractionResult:
    return FactExtractionResult(request_id=request_id, extractor=EXTRACTOR, drafts=drafts)


def _numeric_reference() -> ReferenceLabel:
    base = _reference()
    location = base.evidence_anchors[0].location
    anchor = EvidenceAnchor(
        anchor_id="anchor-1",
        paper_id="paper-1",
        document_version="v1",
        source_sha256=SOURCE_SHA,
        claim_ids=("ref-1",),
        location=EvidenceLocation.model_validate(location.model_dump()),
        source_excerpt=EVIDENCE_TEXT,
        source_excerpt_digest=content_digest(EVIDENCE_TEXT),
    )
    return ReferenceLabel.model_validate({
        **base.model_dump(),
        "reference_answer": REFERENCE_TEXT,
        "reference_claims": (ReferenceClaim(claim_id="ref-1", claim_text=REFERENCE_TEXT).model_dump(),),
        "evidence_anchors": (anchor.model_dump(),),
    })


def test_value_text_parsing_and_rounding_tolerance() -> None:
    assert parse_value_text("95.3%") == (95.3, 1)
    assert parse_value_text("1,024 tokens") == (1024.0, 0)
    assert parse_value_text("between 3 and 4") is None
    assert stated_tolerance(1) == 0.05


def test_draft_must_quote_source_and_carry_its_own_number() -> None:
    sources = (REFERENCE_TEXT,)
    ok, reason = verify_draft(
        _numeric_draft(REFERENCE_TEXT, "95.3%", 95.3, "percent"),
        source_texts=sources, role=FactSourceRole.REFERENCE,
    )
    assert ok is not None and reason is None
    assert ok.fact.tolerance == 0.05
    assert ok.fact.object_id == "model a" and ok.fact.statistic == "accuracy"

    paraphrase = _numeric_draft("Model A gets 95.3% accuracy.", "95.3%", 95.3, "percent")
    assert verify_draft(paraphrase, source_texts=sources, role=FactSourceRole.REFERENCE)[1] == (
        "statement is not a verbatim span of the source text"
    )
    wrong_number = _numeric_draft(REFERENCE_TEXT, "95.3%", 96.0, "percent")
    assert "does not equal" in (verify_draft(wrong_number, source_texts=sources, role=FactSourceRole.REFERENCE)[1] or "")
    wrong_unit = _numeric_draft(REFERENCE_TEXT, "95.3%", 95.3, "fraction")
    assert "percent sign" in (verify_draft(wrong_unit, source_texts=sources, role=FactSourceRole.REFERENCE)[1] or "")


def test_candidate_alignment_is_limited_to_offered_targets() -> None:
    draft = _numeric_draft("Model A reaches 95% accuracy.", "95%", 95.0, "percent", aligned_target_id="ref-9")
    targets = (AlignmentTarget(target_id="ref-1", text=REFERENCE_TEXT),)
    _, reason = verify_draft(
        draft, source_texts=(draft.statement,), role=FactSourceRole.CANDIDATE, alignment_targets=targets,
    )
    assert reason == "draft is aligned to a reference claim that was not offered"


def test_non_atomic_target_projects_nothing_and_is_reported() -> None:
    reference = _numeric_reference()
    two = _result(
        "ref-1",
        _numeric_draft(REFERENCE_TEXT, "95.3%", 95.3, "percent"),
        _numeric_draft(REFERENCE_TEXT, "7", 7.0, "scalar", statistic="seed"),
    )
    projections, report = build_reference_projections(reference, {"ref-1": two})
    assert projections == ()
    assert report.accepted_drafts == 0 and report.drafts == 2
    assert all("not atomic" in item.reason for item in report.rejections)


def _decompose(answer: str, *claims: ClaimDraft) -> tuple[GroundedAnswerCandidate, object]:
    candidate = GroundedAnswerCandidate(
        answer_id="answer-1",
        question="What accuracy does Model A reach on the test split?",
        answer_text=answer,
        cited_evidence_ids=("ev-1",),
        claims=(),
        metadata={},
    )
    result = ClaimDecompositionResult(request_id="answer-1", extractor=EXTRACTOR, claims=claims)
    return apply_claim_decomposition(candidate, result, citation_index=citation_index_from_chunk_ids(["ev-1"]))


def test_end_to_end_rounded_answer_is_correct_and_grounded() -> None:
    answer = "Model A reaches 95% accuracy on the test split under seed 7 [1]."
    claim_text = "Model A reaches 95% accuracy on the test split under seed 7."
    candidate, decomposition = _decompose(
        answer,
        ClaimDraft(claim_text=claim_text, grounding_span="Model A reaches 95% accuracy"),
    )
    assert decomposition.accepted == 1
    claim = candidate.claims[0]
    assert claim.evidence_ids == ("ev-1",)

    reference = _numeric_reference()
    reference_facts, reference_report = build_reference_projections(
        reference,
        {"ref-1": _result("ref-1", _numeric_draft(EVIDENCE_TEXT, "0.953", 0.953, "fraction"))},
    )
    assert reference_report.projected_targets == 1
    assert reference_facts[0].evidence_anchor_ids == ("anchor-1",)
    vocabulary = vocabulary_from_reference(reference_facts)
    assert vocabulary.statistics == ("accuracy",)

    candidate_facts, _ = build_candidate_projections(
        candidate.claims,
        {claim.claim_id: _result(claim.claim_id, _numeric_draft(
            claim_text, "95%", 95.0, "percent", aligned_target_id="ref-1",
        ))},
        alignment_targets=alignment_targets_from_reference(reference),
    )
    context = _context(EVIDENCE_TEXT)
    evidence_facts, evidence_report = build_evidence_projections(
        context,
        {"ev-1": _result("ev-1", _numeric_draft(EVIDENCE_TEXT, "0.953", 0.953, "fraction"))},
    )
    assert evidence_report.accepted_drafts == 1

    result = score_answer(
        case_id="case-1",
        reference=reference,
        candidate=candidate,
        actual_context=context,
        reference_facts=reference_facts,
        candidate_facts=candidate_facts,
        evidence_facts=evidence_facts,
    )
    assert result.claim_assessments[0].verdict is ClaimVerdict.CORRECT
    assert result.fact_precision.value == 1.0
    assert result.fact_completeness.value == 1.0
    assert result.numeric_accuracy.value == 1.0
    assert result.faithfulness.value == 1.0
    assert result.citation_support.value == 1.0
    assert result.citation_bindings[0].answer_span.text == "Model A reaches 95% accuracy"


def test_answer_outside_both_rounding_intervals_is_contradicted() -> None:
    answer = "Model A reaches 93% accuracy on the test split under seed 7 [1]."
    claim_text = "Model A reaches 93% accuracy on the test split under seed 7."
    candidate, _ = _decompose(answer, ClaimDraft(claim_text=claim_text, grounding_span="93% accuracy"))
    reference = _numeric_reference()
    reference_facts, _ = build_reference_projections(
        reference, {"ref-1": _result("ref-1", _numeric_draft(EVIDENCE_TEXT, "0.953", 0.953, "fraction"))},
    )
    claim = candidate.claims[0]
    candidate_facts, _ = build_candidate_projections(
        candidate.claims,
        {claim.claim_id: _result(claim.claim_id, _numeric_draft(
            claim_text, "93%", 93.0, "percent", aligned_target_id="ref-1",
        ))},
        alignment_targets=alignment_targets_from_reference(reference),
    )
    result = score_answer(
        case_id="case-1",
        reference=reference,
        candidate=candidate,
        actual_context=_context(EVIDENCE_TEXT),
        reference_facts=reference_facts,
        candidate_facts=candidate_facts,
    )
    assert result.claim_assessments[0].verdict is ClaimVerdict.CONTRADICTED
    assert result.numeric_accuracy.value == 0.0
    assert result.faithfulness.status is ScoreStatus.NEEDS_REVIEW


def test_decomposition_rejects_invented_numbers_bad_spans_and_duplicates() -> None:
    answer = "Model A reaches 95% accuracy [1]. It is fast."
    candidate, report = _decompose(
        answer,
        ClaimDraft(claim_text="Model A reaches 95% accuracy.", grounding_span="Model A reaches 95% accuracy"),
        ClaimDraft(claim_text="model a reaches 95%   accuracy.", grounding_span="Model A reaches 95% accuracy"),
        ClaimDraft(claim_text="Model A runs at 12 ms latency.", grounding_span="It is fast."),
        ClaimDraft(claim_text="Model A is slow.", grounding_span="It is slow."),
        ClaimDraft(claim_text="Model A reaches 95% accuracy [1].", grounding_span="Model A reaches 95% accuracy"),
    )
    reasons = sorted(item.reason for item in report.rejections)
    assert report.accepted == 1
    assert reasons == [
        "claim text must not carry citation markers",
        "claim text writes numbers the answer does not state",
        "duplicate atomic claim",
        "grounding span is not a verbatim span of the answer at the stated occurrence",
    ]
    assert candidate.metadata["claims_degraded"] is False


def test_trailing_markers_after_sentence_end_belong_to_that_sentence() -> None:
    answer = "Model A reaches 95% accuracy. [1] Model B reaches 90% accuracy."
    candidate, report = _decompose(
        answer,
        ClaimDraft(claim_text="Model A reaches 95% accuracy.", grounding_span="Model A reaches 95% accuracy"),
        ClaimDraft(claim_text="Model B reaches 90% accuracy.", grounding_span="Model B reaches 90% accuracy"),
        ClaimDraft(claim_text="Model A reaches 95% accuracy on dataset 2.", grounding_span="Model A reaches 95%"),
    )
    assert [claim.evidence_ids for claim in candidate.claims] == [("ev-1",), ()]
    assert report.rejections[0].reason == "claim text writes numbers the answer does not state"


def test_unresolvable_citation_marker_is_reported_not_invented() -> None:
    candidate, report = _decompose(
        "Model A reaches 95% accuracy [3].",
        ClaimDraft(claim_text="Model A reaches 95% accuracy.", grounding_span="Model A reaches 95% accuracy"),
    )
    assert candidate.claims[0].evidence_ids == ()
    assert report.unresolved_citation_markers == (3,)
