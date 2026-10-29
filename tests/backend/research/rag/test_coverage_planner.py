from __future__ import annotations

from dataclasses import replace

from framework.harness.rag.fake import fake_rag_session_spec
from framework.harness.rag.models import RetrievalOperation
from framework.harness.rag.policy import normalize_query
from backend.research.rag.coverage_planner import (
    ResearchEvidenceCoveragePlanner,
    section_roles_by_evidence_type,
)
from backend.research.rag.evidence_typing import RESEARCH_EVIDENCE_TYPE_MAPPING

REQUIRED = ("method", "experiment", "limitation", "claim_support")


def _spec(*, max_queries: int = 12):
    base = fake_rag_session_spec()
    return replace(
        base,
        goal=replace(
            base.goal,
            question="Build grounded evidence for the accepted paper.",
            required_evidence_types=REQUIRED,
        ),
        budget=replace(base.budget, max_queries=max_queries),
    )


def test_roles_are_the_inverse_of_the_evidence_typing_table() -> None:
    roles = section_roles_by_evidence_type()

    for evidence_type, values in roles.items():
        for role in values:
            assert RESEARCH_EVIDENCE_TYPE_MAPPING["section_role"][role] == evidence_type
    assert roles["experiment"] == ("experiment", "analysis")
    assert roles["limitation"] == ("conclusion",)


def test_each_missing_type_gets_a_query_scoped_to_its_sections() -> None:
    plan = ResearchEvidenceCoveragePlanner().plan(
        _spec(),
        round_index=0,
        gap_report={"missing_evidence_types": list(REQUIRED)},
    )

    generic, *typed = plan.queries
    assert "filters" not in generic.metadata
    assert [step.metadata["evidence_type"] for step in typed] == list(REQUIRED)
    assert [step.metadata["filters"] for step in typed] == [
        {"section_role": "method"},
        {"section_role": "experiment"},
        {"section_role": "conclusion"},
        {"section_role": "background"},
    ]
    assert all(step.operation == RetrievalOperation.SEARCH_CORPUS for step in plan.queries)
    assert len({normalize_query(step.query) for step in plan.queries}) == len(plan.queries)


def test_a_later_round_tries_the_next_section_role_and_skips_searched_ones() -> None:
    planner = ResearchEvidenceCoveragePlanner()
    spec = _spec()
    first = planner.plan(spec, round_index=0, gap_report={"missing_evidence_types": ["experiment", "limitation"]})
    executed = tuple(normalize_query(step.query) for step in first.queries)

    second = planner.plan(
        spec,
        round_index=1,
        gap_report={"missing_evidence_types": ["experiment", "limitation"]},
        executed_queries=executed,
    )

    filters = [step.metadata.get("filters") for step in second.queries]
    # Results may sit under "Analysis"; the conclusion was already searched.
    assert {"section_role": "analysis"} in filters
    assert {"section_role": "conclusion"} not in filters
    assert not set(executed) & {normalize_query(step.query) for step in second.queries}


def test_the_plan_stays_within_the_query_budget() -> None:
    plan = ResearchEvidenceCoveragePlanner().plan(
        _spec(max_queries=3),
        round_index=0,
        gap_report={"missing_evidence_types": list(REQUIRED)},
    )

    assert len(plan.queries) == 3


def test_nothing_missing_uses_the_generic_plan() -> None:
    plan = ResearchEvidenceCoveragePlanner().plan(_spec(), round_index=0, gap_report={})

    assert all("filters" not in step.metadata for step in plan.queries)
