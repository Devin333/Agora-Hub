from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from framework.rag.retrieval import (
    QueryIntentRule,
    build_query_intent_rules,
    classify_query_intent_by_rules,
)

QueryIntent = Literal[
    "citation_query",
    "concept_method",
    "numerical_result",
    "contribution",
    "comparison",
    "figure_query",
    "table_query",
    "formula_query",
]

_INTENT_SIGNALS: tuple[QueryIntentRule, ...] = build_query_intent_rules([
    ("table_query", [
        "table",
        "tab.",
        "tab ",
        "row",
        "column",
        "\u8868\u683c",
    ]),
    ("figure_query", [
        "visual evidence",
        "diagram",
        "plot",
        "mask",
        "architecture figure",
        "example image",
        "example images",
        "figure",
        "fig",
        "\u56fe",
        "\u56fe\u8868",
    ]),
    ("formula_query", [
        "mathematical relation",
        "objective",
        "loss",
        "optimization",
        "optimize",
        "variable",
        "define",
        "relation",
        "symbol",
        "latex",
        "formula",
        "equation",
        "eq.",
        " eq ",
        "\u516c\u5f0f",
        "\u7b26\u53f7",
        "\u53d8\u91cf",
    ]),
    ("numerical_result", [
        "\u5b9e\u9a8c\u7ed3\u679c",
        "\u7ed3\u679c",
        "\u8868\u660e",
        "according to the results",
        "aggregated metrics",
        "appendix report",
        "appendix section",
        "benchmark",
        "benchmarks",
        "contamination",
        "dataset split",
        "dataset splits",
        "datapoints",
        "during evaluation",
        "evaluated on",
        "evaluation",
        "experiment",
        "experiments",
        "experiment results",
        "experimental comparisons",
        "experimental results",
        "few-shot results",
        "higher threshold",
        "human subjects",
        "label probability threshold",
        "main paper",
        "mmlu",
        "model performance",
        "overall results",
        "reported results",
        "reported for",
        "reported experiments",
        "reported rates",
        "rates reported",
        "results provide",
        "results reported",
        "quantitative evidence",
        "standard deviation",
        "test set",
        "test execution rates",
        "theoretical results",
        "threshold value",
        "user study",
        "win rate",
        "performance",
        "results show",
        "what do results show",
        "what the results show",
        "takeaway",
        "suggest overall",
        "accuracy",
        "f1",
        "bleu",
        "rouge",
        "outperform",
        "score",
        "\u51c6\u786e\u7387",
        "\u591a\u5c11",
    ]),
    ("comparison", [
        "\u76f8\u6bd4",
        "\u5bf9\u6bd4",
        "compared",
        "compared to",
        "comparison",
        "comparisons",
        "versus",
        " vs ",
        "prior work",
        "baseline",
        "baselines",
        "prompting methods",
        "prediction targets",
        "\u76f8\u5173\u5de5\u4f5c",
    ]),
    ("contribution", [
        "\u8d21\u732e",
        "contribution",
        "novelty",
        "propose",
        "\u63d0\u51fa",
        "\u521b\u65b0",
        "what does this paper",
    ]),
    ("concept_method", [
        "\u5982\u4f55",
        "\u600e\u4e48",
        "how",
        "what is",
        "why",
        "method",
        "approach",
        "architecture",
    ]),
])

_FIGURE_ID_RE = re.compile(r"\u56fe\s*([\w-]+)?|\bfig(?:ure)?\.?\s*([A-Za-z0-9_-]+)?", flags=re.IGNORECASE)
_CITATION_QUERY_RE = re.compile(
    r"\b(?:which|what)\s+evidence\s+supports\s+(?:the\s+)?claim\b|"
    r"\bsupport(?:ing)?\s+evidence\s+for\s+(?:the\s+)?claim\b|"
    r"\bwhich\s+passage\s+grounds\s+(?:the\s+paper'?s\s+)?claim\b|"
    r"\bwhat\s+passage\s+grounds\s+(?:the\s+paper'?s\s+)?claim\b|"
    r"\bgrounds\s+(?:the\s+paper'?s\s+)?claim\b",
    flags=re.IGNORECASE,
)


@dataclass
class RetrievalRoute:
    intent: QueryIntent
    section_role_filter: list[str] = field(default_factory=list)
    chunk_type_filter: list[str] = field(default_factory=list)
    figure_id: str = ""
    use_propositions: bool = False
    prefer_abstract: bool = False
    recall_routes: tuple[str, ...] = ()
    candidate_filter_groups: tuple[dict[str, Any], ...] = ()
    extra_filters: dict[str, Any] = field(default_factory=dict)


def classify_query_intent(question: str) -> QueryIntent:
    text = str(question or "")
    if _CITATION_QUERY_RE.search(text):
        return "citation_query"
    if _FIGURE_ID_RE.search(text):
        return "figure_query"
    intent = classify_query_intent_by_rules(
        text,
        _INTENT_SIGNALS,
        default_intent="concept_method",
    )
    return cast(QueryIntent, intent)


def build_retrieval_route(question: str) -> RetrievalRoute:
    """Map a user question to the primary intent plus explanatory recall routes."""
    intent = classify_query_intent(question)

    if intent == "citation_query":
        return RetrievalRoute(
            intent=intent,
            chunk_type_filter=["abstract", "paragraph"],
            recall_routes=("citation_claim", "abstract_body"),
            use_propositions=True,
        )

    if intent == "figure_query":
        return RetrievalRoute(
            intent=intent,
            chunk_type_filter=["figure"],
            recall_routes=("figure_chunks", "caption_fields", "referenced_context"),
            extra_filters={"chunk_type": "figure"},
        )

    if intent == "table_query":
        return RetrievalRoute(
            intent=intent,
            # A table question is often answered by the prose that introduces,
            # interprets, or summarizes a table. Keep table chunks first, but
            # do not hard-filter paragraphs: parsers may expose the table
            # reference without producing a standalone table chunk.
            chunk_type_filter=["table", "paragraph"],
            recall_routes=("table_chunks", "caption_fields", "table_context", "result_context"),
            candidate_filter_groups=(
                {"chunk_type": "table"},
                {"chunk_type": "paragraph"},
            ),
        )

    if intent == "formula_query":
        return RetrievalRoute(
            intent=intent,
            recall_routes=("formula_chunks", "equation_fields", "formula_context"),
            extra_filters={"has_formula": True},
        )

    if intent == "numerical_result":
        return RetrievalRoute(
            intent=intent,
            section_role_filter=["experiment"],
            chunk_type_filter=["table", "paragraph"],
            recall_routes=("table_chunks", "result_paragraphs", "conclusion_context"),
            candidate_filter_groups=(
                {"chunk_type": "table"},
                {"chunk_type": "paragraph"},
            ),
            use_propositions=True,
        )

    if intent == "contribution":
        return RetrievalRoute(
            intent=intent,
            chunk_type_filter=["abstract", "paragraph"],
            recall_routes=("abstract_body", "method_context"),
            prefer_abstract=True,
            use_propositions=True,
        )

    if intent == "comparison":
        return RetrievalRoute(
            intent=intent,
            section_role_filter=["related_work"],
            chunk_type_filter=["paragraph", "table"],
            recall_routes=("comparison_paragraphs", "table_chunks", "result_context"),
            candidate_filter_groups=(
                {"chunk_type": "paragraph"},
                {"chunk_type": "table"},
            ),
            use_propositions=True,
        )

    return RetrievalRoute(
        intent=intent,
        section_role_filter=["method"],
        recall_routes=("method_body",),
    )


__all__ = ["QueryIntent", "RetrievalRoute", "build_retrieval_route", "classify_query_intent"]
