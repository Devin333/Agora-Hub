from __future__ import annotations

from backend.research.rag.retrieval.paper_retriever import (
    PAPER_FORMULA_RAG_V1_POLICY,
    RetrievalPolicy,
    RetrievalRequest,
    build_retrieval_policy,
)
from backend.research.rag.retrieval.planner import QueryPlanner


def test_formula_sparse_policy_plans_formula_chunk_filter() -> None:
    policy = build_retrieval_policy(PAPER_FORMULA_RAG_V1_POLICY)
    plan = QueryPlanner(policy).build(
        RetrievalRequest(
            paper_id="p1",
            question="What does Equation 7 mean?",
            limit=4,
        )
    )

    assert plan.intent == "formula_query"
    assert plan.filters == {"has_formula": True}
    assert plan.candidate_filters == ({"chunk_type": "formula"},)
    assert plan.element_query_labels == ("7",)
    assert plan.candidate_limit == policy.element_label_overfetch_multiplier * 4


def test_element_label_overfetch_uses_policy_multiplier() -> None:
    policy = RetrievalPolicy(overfetch_multiplier=2, element_label_overfetch_multiplier=11)
    plan = QueryPlanner(policy).build(
        RetrievalRequest(
            paper_id="p1",
            question="What does Figure 2 show?",
            limit=3,
        )
    )

    assert plan.intent == "figure_query"
    assert plan.candidate_filters == ({"chunk_type": "figure"},)
    assert plan.element_query_labels == ("2",)
    assert plan.candidate_limit == 33


def test_citation_query_overfetch_uses_claim_multiplier() -> None:
    policy = RetrievalPolicy(overfetch_multiplier=2, citation_claim_overfetch_multiplier=17)
    plan = QueryPlanner(policy).build(
        RetrievalRequest(
            paper_id="p1",
            question="Which evidence supports the claim: the method improves accuracy?",
            limit=5,
        )
    )

    assert plan.intent == "citation_query"
    assert plan.candidate_filters == (
        {"chunk_type": "abstract"},
        {"chunk_type": "paragraph"},
    )
    assert plan.candidate_limit == 85


def test_route_candidate_filter_groups_are_deduplicated_and_serializable() -> None:
    policy = RetrievalPolicy()
    plan = QueryPlanner(policy).build(
        RetrievalRequest(
            paper_id="p1",
            question="What do the reported experiments suggest overall?",
            limit=2,
        )
    )

    assert plan.intent == "numerical_result"
    assert plan.candidate_filters == (
        {"chunk_type": "table"},
        {"chunk_type": "paragraph"},
    )
    assert plan.candidate_limit == policy.overfetch_multiplier * 2

    payload = plan.to_dict()
    assert payload["intent"] == "numerical_result"
    assert payload["candidate_filters"] == [
        {"chunk_type": "table"},
        {"chunk_type": "paragraph"},
    ]
    assert payload["channels"][0]["name"] == "dense_text"
    assert payload["fusion"]["rrf_k"] == policy.rrf_k


def test_candidate_lineage_remains_serializable_in_research_retrieval_plan() -> None:
    plan = QueryPlanner(RetrievalPolicy()).build(
        RetrievalRequest(
            paper_id="p1",
            question="What do the reported experiments suggest overall? experiment",
            limit=2,
            metadata={
                "candidate_experiment": {
                    "accepted": True,
                    "original_query": "What do the reported experiments suggest overall?",
                    "reason": "candidate_accepted",
                },
                "candidate_experiment_config": {
                    "config_version": "research-retrieval-candidate-config-v1",
                    "enabled": True,
                    "max_candidate_queries": 2,
                },
                "original_query": "What do the reported experiments suggest overall?",
            },
        )
    )

    payload = plan.to_dict()

    assert payload["metadata"]["candidate_experiment"]["accepted"] is True
    assert payload["metadata"]["candidate_experiment"]["reason"] == "candidate_accepted"
    assert payload["metadata"]["original_query"] == "What do the reported experiments suggest overall?"
    assert payload["metadata"]["candidate_experiment_config"] == {
        "config_version": "research-retrieval-candidate-config-v1",
        "enabled": True,
        "max_candidate_queries": 2,
    }
