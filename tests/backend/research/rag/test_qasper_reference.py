from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.research.rag.evaluation.protocol_answer_scoring import (
    CandidateFactProjection,
    ClaimVerdict,
    score_answer,
)
from backend.research.rag.evaluation.protocol_dataset import (
    AUTOMATED_REVIEW_POLICY,
    HUMAN_ONLY_REVIEW_POLICY,
)
from backend.research.rag.evaluation.protocol_fact_extraction import (
    alignment_targets_from_reference,
    build_candidate_projections,
)
from backend.research.rag.evaluation.protocol_semantic_facts import SemanticFact
from backend.research.rag.evaluation.qasper_baseline import _build_documents
from backend.research.rag.evaluation.qasper_reference import (
    QasperGoldIdentity,
    build_qasper_gold,
    yes_no_claim_text,
)
from framework.harness.rag.models import AnswerClaim, GroundedAnswerCandidate
from tests.backend.research.rag.test_protocol_fact_extraction import EXTRACTOR, _result

PARAGRAPH = "We fine-tune BERT on SQuAD. The model does not use any pretraining beyond BERT."
QUESTION = "Do they use additional pretraining?"
IDENTITY = QasperGoldIdentity(
    dataset_version="0.3",
    license_reference_id="cc-by-4.0",
    questions_frozen_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    answers_started_at=datetime(2020, 2, 1, tzinfo=timezone.utc),
    retrieved_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
)


def _raw() -> dict:
    return {"paper-1": {
        "title": "Paper", "abstract": "",
        "full_text": [{"section_name": "Method", "paragraphs": [PARAGRAPH]}],
        "qas": [
            {"question_id": "q-yes-no", "question": QUESTION, "question_type": "yes_no", "answers": [
                {"annotation_id": "a1", "worker_id": "w1", "answer": {
                    "unanswerable": False, "yes_no": False, "extractive_spans": [], "free_form_answer": "",
                    "evidence": ["The model does not use any pretraining beyond BERT."],
                }},
                {"annotation_id": "a2", "worker_id": "w2", "answer": {
                    "unanswerable": True, "yes_no": None, "extractive_spans": [], "free_form_answer": "",
                    "evidence": [],
                }},
            ]},
            {"question_id": "q-span", "question": "Which encoder is used?", "paper_read": "yes",
             "question_writer": "writer-7", "answers": [
                {"annotation_id": "a3", "worker_id": "w3", "answer": {
                    "unanswerable": False, "yes_no": None, "extractive_spans": ["BERT", "BERT"],
                    "free_form_answer": "", "evidence": ["We fine-tune BERT on SQuAD."],
                }},
                {"annotation_id": "a4", "worker_id": "w4", "answer": {
                    "unanswerable": False, "yes_no": None, "extractive_spans": [],
                    "free_form_answer": "BERT", "evidence": ["FLOAT SELECTED: Table 1"],
                }},
            ]},
            {"question_id": "q-empty", "question": "Unused?", "answers": [
                {"annotation_id": "a5", "answer": {"unanswerable": False, "evidence": []}},
            ]},
        ],
    }}


def _gold():
    raw = _raw()
    documents, _ = _build_documents(raw)
    return build_qasper_gold(raw, documents, IDENTITY)


def test_each_annotation_is_an_alternative_with_dataset_gold_authority() -> None:
    gold = _gold()
    assert gold.digest and gold.verify_digest() is None
    cases = {item.case_id: item for item in gold.cases}
    assert set(cases) == {"paper-1:q-yes-no", "paper-1:q-span"}
    assert gold.skipped_questions[0].annotation_id == "paper-1:q-empty"

    yes_no, unanswerable = cases["paper-1:q-yes-no"].alternatives
    assert yes_no.answer_type == "yes_no"
    assert yes_no.reference.reference_claims[0].claim_text == yes_no_claim_text(QUESTION, False)
    assert yes_no.reference.review.authority == "dataset_gold"
    assert yes_no.reference.review.gold_source.annotation_fields == ("yes_no", "evidence")
    assert AUTOMATED_REVIEW_POLICY.accepts(yes_no.reference.review)
    assert not HUMAN_ONLY_REVIEW_POLICY.accepts(yes_no.reference.review)
    assert yes_no.fixed_reference_facts[0].fact.polarity is False
    assert unanswerable.reference.answerability == "unanswerable"
    assert unanswerable.reference.expected_behavior == "abstain"

    assert yes_no.reference.provenance.authoring_method == "abstract_only"
    span, figure_only = cases["paper-1:q-span"].alternatives
    # A writer who read the paper produced a full-text-targeted question.
    assert span.reference.provenance.authoring_method == "fulltext_targeted"
    assert span.reference.provenance.question_author_id == "qasper-question-writer:writer-7"
    assert [item.claim_text for item in span.reference.reference_claims] == ["BERT"]
    assert span.reference.evidence_anchors[0].location.section == "Method"
    assert figure_only.reference.answerability == "needs_review"
    assert "figure/table" in (figure_only.reference.review.notes or "")


def test_build_is_deterministic() -> None:
    assert _gold().digest == _gold().digest


def test_timestamps_must_follow_the_collection_protocol() -> None:
    with pytest.raises(ValueError, match="after questions were frozen"):
        QasperGoldIdentity(
            dataset_version="0.3", license_reference_id="cc-by-4.0",
            questions_frozen_at=datetime(2020, 2, 1, tzinfo=timezone.utc),
            answers_started_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            retrieved_at=datetime(2026, 10, 22, tzinfo=timezone.utc),
        )


def _yes_no_candidate(polarity: bool):
    from backend.research.ports.answer_fact_extraction import FactDraft

    alternative = next(item for item in _gold().cases if item.case_id == "paper-1:q-yes-no").alternatives[0]
    text = "They do not use additional pretraining." if not polarity else "They use additional pretraining."
    claim = AnswerClaim(claim_id="answer-1:claim-1", text=text, evidence_ids=())
    candidate = GroundedAnswerCandidate(
        answer_id="answer-1", question=QUESTION, answer_text=text,
        cited_evidence_ids=(), claims=(claim,), metadata={},
    )
    reference = alternative.reference
    target_id = reference.reference_claims[0].claim_id
    # The extractor names the asked property its own way; alignment fixes scope.
    draft = FactDraft(statement=text, polarity=polarity, statistic="uses_pretraining",
                      object_id="authors", aligned_target_id=target_id)
    facts, _ = build_candidate_projections(
        (claim,), {claim.claim_id: _result(claim.claim_id, draft)},
        alignment_targets=alignment_targets_from_reference(reference),
        reference_facts=alternative.fixed_reference_facts,
    )
    return alternative, candidate, facts


def test_aligned_yes_no_answer_is_scored_against_the_fixed_gold_fact() -> None:
    for polarity, verdict in ((False, ClaimVerdict.CORRECT), (True, ClaimVerdict.CONTRADICTED)):
        alternative, candidate, facts = _yes_no_candidate(polarity)
        assert facts[0].fact.statistic == "answer" and facts[0].fact.object_id == "question"
        result = score_answer(
            case_id="paper-1:q-yes-no",
            reference=alternative.reference,
            candidate=candidate,
            actual_context=None,
            reference_facts=alternative.fixed_reference_facts,
            candidate_facts=facts,
        )
        assert result.claim_assessments[0].verdict is verdict
        assert result.fact_completeness.value == (1.0 if verdict is ClaimVerdict.CORRECT else 0.0)


def test_unaligned_yes_no_fact_keeps_its_own_scope() -> None:
    from backend.research.ports.answer_fact_extraction import FactDraft

    alternative = _gold().cases[0].alternatives[0]
    claim = AnswerClaim(claim_id="c", text="They do not use pretraining.", evidence_ids=())
    draft = FactDraft(statement=claim.text, polarity=False, statistic="uses_pretraining")
    facts, _ = build_candidate_projections(
        (claim,), {"c": _result("c", draft)},
        alignment_targets=alignment_targets_from_reference(alternative.reference),
        reference_facts=alternative.fixed_reference_facts,
    )
    assert isinstance(facts[0], CandidateFactProjection)
    assert facts[0].fact == SemanticFact(polarity=False, statistic="uses_pretraining")
    assert EXTRACTOR.provider == "fixture"
