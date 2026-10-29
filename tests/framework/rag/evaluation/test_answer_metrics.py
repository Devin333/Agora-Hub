from __future__ import annotations

import pytest

from framework.rag.evaluation import (
    AnswerMetricCase,
    AnswerOutcome,
    RAGFailureReason,
    evaluate_answer_case,
    score_answer_case,
)


def test_answer_metrics_are_deterministic_and_grounded_in_context():
    case = AnswerMetricCase(
        case_id="case-1",
        question="What does the experiment show?",
        answer="The experiment shows higher accuracy and lower loss. [ev-1]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=("higher accuracy", "lower loss"),
        cited_evidence_ids=("ev-1",),
        context_evidence_ids=("ev-1", "ev-2"),
    )

    metrics = {metric.name: metric.value for metric in evaluate_answer_case(case)}

    assert metrics["fact_coverage"] == 1.0
    assert metrics["citation_grounding"] == 1.0
    assert metrics["answer_relevance"] > 0
    assert metrics["faithfulness_proxy"] == 1.0
    assert metrics["abstention_accuracy"] == 1.0


def test_answer_metrics_detect_expected_abstention():
    case = AnswerMetricCase(
        case_id="case-2",
        question="What is missing?",
        answer="",
        outcome=AnswerOutcome.ABSTAINED,
        expected_abstain=True,
    )

    metrics = {metric.name: metric.value for metric in evaluate_answer_case(case)}

    assert metrics["faithfulness_proxy"] == 1.0
    assert metrics["abstention_accuracy"] == 1.0


def test_score_answer_case_tracks_context_citation_locator_and_success():
    case = AnswerMetricCase(
        case_id="case-3",
        question="What do the results show?",
        answer="Table 1 reports accuracy and F1, and the model improves accuracy. [tbl-1] [para-1]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=(
            "Table 1 reports accuracy and F1.",
            "The model improves accuracy.",
        ),
        cited_evidence_ids=("tbl-1", "para-1"),
        context_evidence_ids=("tbl-1", "para-1"),
        gold_evidence_ids=("tbl-1", "para-1"),
        cited_source_locators=("paper://p/pdf#page=6&rect=1,2,3,4", "paper://p/pdf#page=7"),
        gold_source_locators=("paper://p/pdf#page=6", "paper://p/pdf#page=7"),
        retrieved_evidence_ids=("tbl-1", "para-1"),
    )

    score = score_answer_case(case)

    assert score.fact_coverage == 1.0
    assert score.retrieval_context_coverage == 1.0
    assert score.citation_grounding == 1.0
    assert score.source_locator_grounding == 1.0
    assert score.answer_success is True
    assert score.failure_reason == ""


def test_score_answer_case_explains_missing_retrieval_and_structured_fact_match():
    structured = AnswerMetricCase(
        case_id="case-4",
        question="What does the figure show?",
        answer="The figure shows a schematic of the baseline objective. [fig-1]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=(
            "[Figure fig_1] Caption: Schematic of the objective we use in our baseline model. "
            "Nearby Context: In this example, we process a long sentence with multiple labels.",
        ),
        cited_evidence_ids=("fig-1",),
        context_evidence_ids=("fig-1",),
        gold_evidence_ids=("fig-1",),
        retrieved_evidence_ids=("fig-1",),
    )
    missing = AnswerMetricCase(
        case_id="case-5",
        question="What does the formula mean?",
        answer="The context discusses attention instead. [other]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=("The formula computes a convolution kernel.",),
        cited_evidence_ids=("other",),
        context_evidence_ids=("other",),
        gold_evidence_ids=("eq-1",),
        retrieved_evidence_ids=("other",),
    )

    structured_score = score_answer_case(structured)
    missing_score = score_answer_case(missing)

    assert structured_score.fact_coverage == 1.0
    assert structured_score.answer_success is True
    assert missing_score.failure_reason == RAGFailureReason.MISSING_GOLD_IN_RETRIEVAL.value


def test_score_answer_case_preserves_latex_display_math_for_formula_match():
    case = AnswerMetricCase(
        case_id="case-latex-display",
        question="How is the objective defined?",
        answer=(
            "The objective is\n"
            "\\[\n"
            "\\arg \\max_\\pi \\mathbb{E}_{p \\sim \\mathcal{D}, g \\sim \\pi}[R(g \\mid p)]\n"
            "\\]\n"
            "where prompts come from the dataset and generations come from the policy. [eq-1]"
        ),
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=(
            "\\begin{equation}\n"
            "   \\arg \\max _\\pi \\mathbb{E}_{p \\sim \\mathcal{D}, g \\sim \\pi}[R(g \\mid p)]\n"
            "\\end{equation}",
        ),
        cited_evidence_ids=("eq-1",),
        context_evidence_ids=("eq-1",),
        gold_evidence_ids=("eq-1",),
        retrieved_evidence_ids=("eq-1",),
    )

    score = score_answer_case(case)

    assert score.fact_coverage == 1.0
    assert score.answer_success is True


def test_score_answer_case_accepts_equivalent_gold_evidence_support():
    case = AnswerMetricCase(
        case_id="case-equivalent-gold",
        question="How is the Hamiltonian ODE defined?",
        answer="The Hamiltonian ODE is defined as dot y equals J inverse times the Hamiltonian gradient. [para-1]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=("Hamiltonian ODE is defined as dot y equals J inverse times the Hamiltonian gradient.",),
        cited_evidence_ids=("para-1",),
        context_evidence_ids=("para-1",),
        gold_evidence_ids=("eq-1",),
        equivalent_gold_evidence_ids=("eq-1", "para-1"),
        retrieved_evidence_ids=("para-1",),
    )

    score = score_answer_case(case)

    assert score.answer_success is True
    assert score.failure_reason == ""
    assert score.strict_retrieval_context_coverage == 0.0
    assert score.equivalent_retrieval_context_coverage == 1.0
    assert score.strict_citation_gold_coverage == 0.0
    assert score.equivalent_citation_gold_coverage == 1.0
    assert score.equivalent_gold_supported is True


def test_score_answer_case_uses_canonical_failure_reason_taxonomy():
    context_missing = AnswerMetricCase(
        case_id="case-6",
        question="What does the table show?",
        answer="Only the table is cited. [tbl-1]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=("The table conclusion is explained in the paragraph.",),
        cited_evidence_ids=("tbl-1",),
        context_evidence_ids=("tbl-1",),
        gold_evidence_ids=("tbl-1", "para-1"),
        retrieved_evidence_ids=("tbl-1", "para-1"),
    )
    citation_missing = AnswerMetricCase(
        case_id="case-7",
        question="What does the table show?",
        answer="The table conclusion is explained in the paragraph. [other]",
        outcome=AnswerOutcome.ANSWERED,
        expected_facts=("The table conclusion is explained in the paragraph.",),
        cited_evidence_ids=("other",),
        context_evidence_ids=("tbl-1", "para-1"),
        gold_evidence_ids=("tbl-1", "para-1"),
        retrieved_evidence_ids=("tbl-1", "para-1"),
    )
    abstention_wrong = AnswerMetricCase(
        case_id="case-8",
        question="What is missing?",
        answer="This paper has enough evidence.",
        outcome=AnswerOutcome.ANSWERED,
        expected_abstain=True,
    )

    assert score_answer_case(context_missing).failure_reason == RAGFailureReason.CONTEXT_MISSING_GOLD.value
    assert score_answer_case(citation_missing).failure_reason == RAGFailureReason.CITATION_MISSING_SOURCE.value
    assert score_answer_case(abstention_wrong).failure_reason == RAGFailureReason.ABSTENTION_WRONG.value


@pytest.mark.parametrize("outcome", [AnswerOutcome.HALTED, AnswerOutcome.FAILED])
@pytest.mark.parametrize("expected_abstain", [False, True])
def test_system_failures_are_excluded_from_quality_and_refusal_metrics(outcome, expected_abstain):
    case = AnswerMetricCase(
        case_id="case-failure",
        question="What does the table show?",
        answer="",
        outcome=outcome,
        expected_facts=("Average NLU is 76.",),
        gold_evidence_ids=("tbl-1",),
        context_evidence_ids=("tbl-1",),
        expected_abstain=expected_abstain,
    )

    score = score_answer_case(case)

    assert score.answer_success is False
    assert score.failure_reason == RAGFailureReason.SYSTEM_FAILURE.value
    assert score.abstention_correct is None
    assert score.fact_coverage is None
    assert score.retrieval_context_coverage is None


@pytest.mark.parametrize("outcome", [AnswerOutcome.ABSTAINED, AnswerOutcome.INSUFFICIENT_EVIDENCE])
def test_refusing_an_answerable_question_is_over_conservative(outcome):
    case = AnswerMetricCase(
        case_id="case-over",
        question="What does the table show?",
        answer="",
        outcome=outcome,
        expected_facts=("Average NLU is 76.",),
        gold_evidence_ids=("tbl-1",),
        context_evidence_ids=("tbl-1",),
    )

    score = score_answer_case(case)

    assert score.answer_success is False
    assert score.failure_reason == RAGFailureReason.ABSTAINED_OVER_CONSERVATIVE.value
    assert score.fact_coverage is None
    assert score.citation_grounding is None
    # Context coverage still explains whether the refusal had the evidence available.
    assert score.retrieval_context_coverage == 1.0


@pytest.mark.parametrize("outcome", [AnswerOutcome.ABSTAINED, AnswerOutcome.INSUFFICIENT_EVIDENCE])
def test_refusing_an_unanswerable_question_is_correct(outcome):
    case = AnswerMetricCase(
        case_id="case-refuse",
        question="What latency does the system achieve?",
        answer="",
        outcome=outcome,
        expected_abstain=True,
    )

    score = score_answer_case(case)

    assert score.abstention_correct == 1.0
    assert score.answer_success is True
    assert score.failure_reason == ""


def test_hedged_answer_text_is_never_reinterpreted_as_a_refusal():
    case = AnswerMetricCase(
        case_id="case-hedge",
        question="What latency does the system achieve?",
        answer="The provided context does not mention latency.",
        outcome=AnswerOutcome.ANSWERED,
        expected_abstain=True,
    )

    assert score_answer_case(case).abstention_correct == 0.0


def test_non_answered_case_rejects_answer_text():
    with pytest.raises(ValueError, match="only answered cases"):
        AnswerMetricCase(
            case_id="case-bad",
            question="Q?",
            answer="Synthesized refusal text.",
            outcome=AnswerOutcome.HALTED,
        )


def _abstain_case(question: str, answer: str, *, cited=("ev-1",), outcome=AnswerOutcome.ANSWERED):
    return AnswerMetricCase(
        case_id="case-negation",
        question=question,
        answer=answer,
        outcome=outcome,
        cited_evidence_ids=cited,
        expected_abstain=True,
    )


def test_cited_explicit_negation_to_a_yes_no_question_is_a_correct_abstention():
    score = score_answer_case(
        _abstain_case("Does this paper disclose a private API key?", "No. The passage does not disclose one. [1]")
    )

    assert score.abstention_correct == 1.0
    assert score.answer_success is True
    assert score.abstention_basis == "cited_negation"
    assert score.failure_reason == ""


@pytest.mark.parametrize(
    ("question", "answer", "cited"),
    [
        # No citation: the negation is not grounded in retrieved context.
        ("Does this paper disclose a private API key?", "No. It does not.", ()),
        # Not a yes/no question.
        ("What API key does the paper use?", "No API key is disclosed. [1]", ("ev-1",)),
        # Not an explicit negation.
        ("Does this paper disclose a private API key?", "The passage mentions keys. [1]", ("ev-1",)),
        # "No" must be a word, not a prefix of another word.
        ("Does this paper report notation details?", "Notation is described in Section 2. [1]", ("ev-1",)),
    ],
)
def test_only_cited_explicit_negations_to_yes_no_questions_count(question, answer, cited):
    score = score_answer_case(_abstain_case(question, answer, cited=cited))

    assert score.abstention_correct == 0.0
    assert score.abstention_basis == ""
    assert score.failure_reason == RAGFailureReason.ABSTENTION_WRONG.value


def test_refusal_basis_is_recorded():
    score = score_answer_case(
        _abstain_case("Does this paper disclose a key?", "", cited=(), outcome=AnswerOutcome.ABSTAINED)
    )

    assert score.abstention_basis == "refusal"
