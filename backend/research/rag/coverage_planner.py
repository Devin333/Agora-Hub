"""Plan retrieval rounds that cover every evidence type a Research goal requires.

The generic planner sends one query per round. Over a whole paper, lexical
ranking of one goal sentence returns the same few passages every round, so a
goal that needs method, experiment, limitation and claim-support evidence
never reaches the result or conclusion sections. This planner asks for each
missing type separately and restricts each query to the sections that carry
that type, using the same section-role mapping that types retrieved evidence.
"""

from __future__ import annotations

from typing import Any

from framework.harness.rag.models import (
    RAGSessionSpec,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
)
from framework.harness.rag.planner import DeterministicRAGPlanner, RAGPlanner
from framework.harness.rag.policy import normalize_query
from backend.research.rag.evidence_typing import RESEARCH_EVIDENCE_TYPE_MAPPING

SECTION_ROLE_KEY = "section_role"
# Results per evidence type and round; one accepted passage covers a type.
_RESULTS_PER_TYPE = 3


def section_roles_by_evidence_type() -> dict[str, tuple[str, ...]]:
    """Invert the evidence typing table: the section roles that yield each type."""

    roles: dict[str, list[str]] = {}
    for role, evidence_type in RESEARCH_EVIDENCE_TYPE_MAPPING[SECTION_ROLE_KEY].items():
        roles.setdefault(evidence_type, []).append(role)
    return {evidence_type: tuple(values) for evidence_type, values in roles.items()}


class ResearchEvidenceCoveragePlanner:
    """The generic query plus one section-scoped query per missing type.

    Round ``r`` scopes a type to its ``r``-th section role (wrapping), so a
    paper whose results sit under "Analysis" rather than "Experiments" is
    still reached. Types with no section role (for example ``figure``) and
    rounds where nothing is missing use the generic planner alone.
    """

    def __init__(self, fallback: RAGPlanner | None = None) -> None:
        self._fallback = fallback or DeterministicRAGPlanner()
        self._roles = section_roles_by_evidence_type()

    def plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...] = (),
    ) -> RetrievalPlanCandidate:
        baseline = self._fallback.plan(
            spec,
            round_index=round_index,
            gap_report=gap_report,
            executed_queries=executed_queries,
        )
        missing = tuple(
            str(item)
            for item in gap_report.get("missing_evidence_types", ())
            if str(item).strip() in self._roles
        )
        if not missing:
            return baseline
        executed = {normalize_query(query) for query in executed_queries}
        # The generic query still runs: answers draw on passages of every type.
        steps = [step for step in baseline.queries if normalize_query(step.query) not in executed]
        for evidence_type in missing:
            step = self._typed_step(spec, evidence_type, round_index=round_index)
            # A type with a single section role is searched once.
            if normalize_query(step.query) not in executed:
                steps.append(step)
        remaining_queries = max(1, spec.budget.max_queries - len(executed_queries))
        steps = steps[:remaining_queries]
        if not steps:
            return baseline
        return RetrievalPlanCandidate(
            candidate_id=f"{spec.session_id}:coverage-plan:round-{round_index + 1}",
            queries=tuple(steps),
            memory_recall_plan=baseline.memory_recall_plan,
            expected_evidence=spec.goal.required_evidence_types,
            expected_gaps=missing,
            risks=("bounded_retrieval_budget",),
            confidence=0.7,
            metadata={"planner": "research_evidence_coverage"},
        )

    def _typed_step(
        self,
        spec: RAGSessionSpec,
        evidence_type: str,
        *,
        round_index: int,
    ) -> RetrievalStepSpec:
        roles = self._roles[evidence_type]
        role = roles[round_index % len(roles)]
        return RetrievalStepSpec(
            step_id=f"{spec.session_id}:query:{evidence_type}:round-{round_index + 1}",
            operation=RetrievalOperation.SEARCH_CORPUS,
            # Role and type in the text keep every scoped query distinct.
            query=f"{spec.goal.question} {evidence_type} {role}",
            corpus=spec.allowed_corpora[0],
            max_results=_RESULTS_PER_TYPE,
            metadata={
                "scope_ref": spec.source_policy.get("scope_ref"),
                "evidence_type": evidence_type,
                "filters": {SECTION_ROLE_KEY: role},
            },
        )


__all__ = ["ResearchEvidenceCoveragePlanner", "section_roles_by_evidence_type"]
