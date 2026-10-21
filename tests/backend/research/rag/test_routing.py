from __future__ import annotations

import pytest

from backend.research.rag.retrieval.paper_policy import build_retrieval_route, classify_query_intent


@pytest.mark.parametrize(
    "question",
    [
        "What does Equation 2 mean in this paper?",
        "What does Eq. 3 mean?",
        "What does this latex expression mean?",
        "Which variable does this symbol represent?",
    ],
)
def test_formula_questions_route_to_formula_query(question: str) -> None:
    assert classify_query_intent(question) == "formula_query"

    route = build_retrieval_route(question)

    assert route.intent == "formula_query"
    assert route.extra_filters == {"has_formula": True}


def test_citation_questions_route_before_table_keywords() -> None:
    question = (
        "Which evidence supports the claim: Large language models pre-trained on "
        "web-scale datasets show strong zero-shot and few-shot generalization?"
    )

    route = build_retrieval_route(question)

    assert classify_query_intent(question) == "citation_query"
    assert route.intent == "citation_query"
    assert route.extra_filters == {}
    assert route.chunk_type_filter == ["abstract", "paragraph"]


def test_blind_semantic_citation_grounding_question_routes_to_citation() -> None:
    question = "Which passage grounds the paper's claim about vectors, objective, and examples?"

    route = build_retrieval_route(question)

    assert classify_query_intent(question) == "citation_query"
    assert route.intent == "citation_query"
    assert route.recall_routes == ("citation_claim", "abstract_body")


def test_explicit_figure_question_routes_before_formula_symbol_keywords() -> None:
    question = (
        "What does Figure 1, captioned Chain-of-thought prompting enables large "
        "language models to tackle complex arithmetic, commonsense, and symbolic "
        "reasoning tasks, show?"
    )

    route = build_retrieval_route(question)

    assert classify_query_intent(question) == "figure_query"
    assert route.intent == "figure_query"
    assert route.extra_filters == {"chunk_type": "figure"}


@pytest.mark.parametrize(
    ("question", "intent", "routes"),
    [
        (
            "What quantitative evidence do the reported experiments provide about BLEU performance?",
            "numerical_result",
            ("table_chunks", "result_paragraphs", "conclusion_context"),
        ),
        (
            "What visual evidence explains the architecture diagram and example images?",
            "figure_query",
            ("figure_chunks", "caption_fields", "referenced_context"),
        ),
        (
            "How does the paper define the mathematical relation for the loss objective?",
            "formula_query",
            ("formula_chunks", "equation_fields", "formula_context"),
        ),
    ],
)
def test_blind_semantic_natural_questions_route_to_v2_plans(
    question: str,
    intent: str,
    routes: tuple[str, ...],
) -> None:
    route = build_retrieval_route(question)

    assert classify_query_intent(question) == intent
    assert route.intent == intent
    assert route.recall_routes == routes


def test_numerical_result_uses_table_and_paragraph_candidate_routes() -> None:
    route = build_retrieval_route("What do the reported experiments suggest overall?")

    assert route.intent == "numerical_result"
    assert route.candidate_filter_groups == (
        {"chunk_type": "table"},
        {"chunk_type": "paragraph"},
    )


@pytest.mark.parametrize(
    "question",
    [
        (
            "During evaluation of Transformer component variations, which decoding method was used "
            "and what model-aggregation technique was explicitly not used?"
        ),
        "What do the results provide high-level perspective on regarding the promise of different avenues of research?",
        "What did the section on measuring and preventing memorization of benchmarks provide a high-level overview of?",
        "What does the appendix section on test set contamination say it provides details on?",
        "According to the text, what did the user study find about human subjects' preference for the reported results?",
        "What task is the approach evaluated on in the experiments text?",
        "Which GPT-4 model version is used for all experiments that generate win rate judgments in the summarization and dialogue evaluations?",
        "On what types of benchmarks and model variations are the experimental results reported?",
        "Why does the main body of the work focus on summarizing and analyzing overall results?",
        "What full set of assumptions should be stated when including theoretical results?",
        "When theoretical results are included, what must be provided to make the proofs complete?",
        "What are the false positive test execution rates reported for MBPP Python and HumanEval Python?",
    ],
)
def test_live_answer_result_failure_questions_route_to_result_context(question: str) -> None:
    route = build_retrieval_route(question)

    assert classify_query_intent(question) == "numerical_result"
    assert route.intent == "numerical_result"
    assert route.section_role_filter == ["experiment"]
    assert route.recall_routes == ("table_chunks", "result_paragraphs", "conclusion_context")
    assert route.candidate_filter_groups == (
        {"chunk_type": "table"},
        {"chunk_type": "paragraph"},
    )


@pytest.mark.parametrize(
    "question",
    [
        "In the ablation study, which two prediction targets are compared to verify the effectiveness of the parameterization?",
        "How do the baseline pass@1 accuracies compare between HumanEval Python and MBPP Python?",
    ],
)
def test_live_answer_comparison_failure_questions_route_to_comparison_context(question: str) -> None:
    route = build_retrieval_route(question)

    assert classify_query_intent(question) == "comparison"
    assert route.intent == "comparison"
    assert route.recall_routes == ("comparison_paragraphs", "table_chunks", "result_context")


@pytest.mark.parametrize(
    "question",
    [
        "Which dataset splits are explicitly mentioned as having their numbers of datapoints shown in Table `tab:num_instances`?",
        "What experimental comparisons are included in the section's tables regarding prompting methods?",
    ],
)
def test_live_answer_table_result_questions_keep_table_route_precedence(question: str) -> None:
    route = build_retrieval_route(question)

    assert classify_query_intent(question) == "table_query"
    assert route.intent == "table_query"
    assert route.recall_routes == ("table_chunks", "caption_fields", "table_context", "result_context")
    assert route.candidate_filter_groups == (
        {"chunk_type": "table"},
        {"chunk_type": "paragraph"},
    )
