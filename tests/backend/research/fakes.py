from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from framework.harness import EvidencePack, RAGContextPack
from framework.harness.control_plane.node_output import InMemoryHarnessNodeOutputResource
from framework.shared.graph_identity import GraphExecutionIdentity

from backend.research.code_repository import CodeRepositoryObservation, CodeRepositoryProfile, compute_star_growth
from backend.research.domain import (
    ResearchDocument,
    ResearchEvidenceItem,
    ResearchPaper,
    ResearchSection,
    SourceLineage,
    stable_research_id,
)
from backend.research.domain.paper import PaperSourceRecord
from backend.research.rag import ResearchRAGContext, ResearchRAGGapReport


FIXED_NOW = datetime(2026, 6, 5, 8, 0, tzinfo=UTC)


def in_memory_node_output_resource_factory(
    _run_id: str,
) -> InMemoryHarnessNodeOutputResource:
    """Explicit test-only Graph physical resource; production uses SQLite."""

    return InMemoryHarnessNodeOutputResource()


class FakeResearchSourceProvider:
    def __init__(self) -> None:
        self.paper = ResearchPaper(
            paper_id="paper-harness-001",
            title="Harnessed Research Agents for Reliable Paper Intelligence",
            authors=["Ada Lovelace", "Grace Hopper"],
            abstract=(
                "The paper studies a Harness-controlled research runtime with bounded retrieval, "
                "deterministic gates, and auditable reader artifacts."
            ),
            published_at=FIXED_NOW,
            source="arxiv",
            source_url="https://arxiv.org/abs/2606.00123",
            pdf_url="https://arxiv.org/pdf/2606.00123",
            code_url="https://github.com/newsroom/harnessed-research-agents",
            topics=["research agents", "bounded rag", "reader repair"],
        )

    def fetch_paper(self, source_url: str) -> ResearchPaper:
        return self.paper

    def fetch_source_record(self, paper_id: str) -> PaperSourceRecord:
        return PaperSourceRecord(
            source_id="source-paper-harness-001",
            paper_id=paper_id,
            source_type="arxiv",
            source_url="https://arxiv.org/abs/2606.00123",
            fetched_at=FIXED_NOW,
            source_hash="sha256-harness-paper-source",
        )


class FakeResearchDocumentCompiler:
    def __init__(self, *, omit_sections: bool = False) -> None:
        self.omit_sections = omit_sections

    def compile(self, source: PaperSourceRecord) -> ResearchDocument:
        sections = [] if self.omit_sections else [
            ResearchSection(
                section_id="sec-introduction",
                title="Introduction",
                level=1,
                text=(
                    "Research agents often mix workflow routing with candidate generation. "
                    "The Harness separates these responsibilities."
                ),
                page_start=1,
                page_end=2,
                source_ref="paper://paper-harness-001/sec-introduction",
            ),
            ResearchSection(
                section_id="sec-method",
                title="Method",
                level=1,
                text=(
                    "The method uses PLAN EXECUTE VERIFY phases, bounded RAG, context snapshots, "
                    "and deterministic evidence gates."
                ),
                page_start=3,
                page_end=5,
                source_ref="paper://paper-harness-001/sec-method",
            ),
            ResearchSection(
                section_id="sec-experiments",
                title="Experiments",
                level=1,
                text=(
                    "Experiments compare claim verification accuracy and reader payload fidelity "
                    "against an uncontrolled agent baseline."
                ),
                page_start=6,
                page_end=8,
                source_ref="paper://paper-harness-001/sec-experiments",
            ),
            ResearchSection(
                section_id="sec-limitations",
                title="Limitations",
                level=1,
                text="The first backend loop handles one paper at a time and routes reader repair to a later workflow.",
                page_start=9,
                page_end=9,
                source_ref="paper://paper-harness-001/sec-limitations",
            ),
        ]
        source_refs = [section.source_ref for section in sections] or ["paper://paper-harness-001/missing-sections"]
        return ResearchDocument(
            paper_id=source.paper_id,
            source_hash=source.source_hash or "sha256-harness-paper-source",
            sections=sections,
            lineage=SourceLineage(source_refs=source_refs, source_hash=source.source_hash),
            metadata={"compiler": "fake_research_document_compiler"},
        )


class FakeResearchLLMWorker:
    def __init__(
        self,
        *,
        include_flow_control_field: bool = False,
        missing_evidence: bool = False,
        invented_taxonomy: bool = False,
        duplicate_claim: bool = False,
        score_value: float = 0.87,
    ) -> None:
        self.include_flow_control_field = include_flow_control_field
        self.missing_evidence = missing_evidence
        self.invented_taxonomy = invented_taxonomy
        self.duplicate_claim = duplicate_claim
        self.score_value = score_value

    def generate_candidate(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        execution_identity: GraphExecutionIdentity | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
    ) -> dict[str, Any]:
        del execution_identity
        if task == "candidate_task_plan":
            return {
                "tasks": [
                    {
                        "task_id": "analyze-structure",
                        "objective": "Analyze structure from accepted Research evidence.",
                        "worker_capability": "research.analysis.structure",
                        "input_refs": ["document", "evidence_pack"],
                        "depends_on": [],
                        "priority": 10,
                    },
                    {
                        "task_id": "analyze-contribution",
                        "objective": "Analyze contributions from accepted Research evidence.",
                        "worker_capability": "research.analysis.contribution",
                        "input_refs": ["document", "evidence_pack"],
                        "depends_on": [],
                        "priority": 10,
                    },
                    {
                        "task_id": "analyze-experiments",
                        "objective": "Analyze experiments after structure is available.",
                        "worker_capability": "research.analysis.experiments",
                        "input_refs": ["document", "evidence_pack"],
                        "depends_on": ["analyze-structure"],
                        "priority": 5,
                    },
                ],
                "requested_max_parallelism": 3,
            }
        if task == "candidate_three_minute_read":
            method_evidence_id = stable_research_id("evidence", "paper-harness-001", "sec-method")
            result = {
                "three_minute_read": {
                    "problem": "Research agents need auditable workflow control.",
                    "core_idea": "Harness owns routing while LLM workers generate candidate research content.",
                    "key_contributions": ["Bounded RAG", "Context snapshots", "Deterministic evidence gates"],
                    "method_summary": "The runtime runs PLAN EXECUTE VERIFY with explicit budgets.",
                    "experiment_summary": "The paper reports improved claim verification reliability.",
                    "limitations": ["The first loop is single-paper only"],
                    "why_it_matters": "It prevents candidate text from deciding publication.",
                    "read_next": ["Reader repair memory"],
                    "evidence_refs": [] if self.missing_evidence else [
                        {
                            "evidence_id": method_evidence_id,
                            "source_ref": "paper://paper-harness-001/sec-method",
                            "section_id": "sec-method",
                            "confidence": 0.95,
                        }
                    ],
                    "confidence": 0.92,
                }
            }
            if self.include_flow_control_field:
                result["next_step"] = "publish_artifacts"
            return result
        if task == "candidate_taxonomy":
            term = "invented_agent_task" if self.invented_taxonomy else "paper_reading"
            label = "invented agent task" if self.invented_taxonomy else "paper reading"
            return {
                "taxonomy_candidates": [
                    {
                        "level": "domain",
                        "term_id": "code",
                        "label": "Code",
                        "evidence_refs": ["paper://paper-harness-001/sec-method"],
                        "confidence": 0.86,
                    },
                    {
                        "level": "area",
                        "term_id": "agent",
                        "label": "Agent",
                        "evidence_refs": ["paper://paper-harness-001/sec-introduction"],
                        "confidence": 0.84,
                    },
                    {
                        "level": "task",
                        "term_id": term,
                        "label": label,
                        "evidence_refs": ["paper://paper-harness-001/sec-method"],
                        "confidence": 0.8,
                    },
                ]
            }
        if task == "candidate_experiment_claims":
            method_evidence_id = stable_research_id("evidence", "paper-harness-001", "sec-method")
            experiment_evidence_id = stable_research_id("evidence", "paper-harness-001", "sec-experiments")
            claims = [
                {
                    "claim_id": "claim-method-control",
                    "text": "Harness separates workflow routing from LLM candidate generation.",
                    "claim_type": "method",
                    "section_id": "sec-method",
                    "evidence_ids": [] if self.missing_evidence else [method_evidence_id],
                    "confidence": 0.9,
                },
                {
                    "claim_id": "claim-experiment-reader",
                    "text": "Experiments evaluate reader payload fidelity.",
                    "claim_type": "experiment",
                    "section_id": "sec-experiments",
                    "evidence_ids": [] if self.missing_evidence else [experiment_evidence_id],
                    "confidence": 0.88,
                },
            ]
            if self.duplicate_claim:
                claims.append(dict(claims[-1], claim_id="claim-experiment-reader-duplicate"))
            return {
                "claims": claims,
                "scores": [
                    {
                        "score_id": "score-claim-verification",
                        "benchmark_id": "claim_verification",
                        "dataset_id": "single_paper_eval",
                        "metric_id": "accuracy",
                        "value": self.score_value,
                        "source_refs": ["paper://paper-harness-001/sec-experiments"],
                    }
                ],
            }
        return {}


class FakeGithubRepositoryPort:
    def fetch_profile(self, repo_url: str) -> CodeRepositoryProfile:
        previous = CodeRepositoryObservation(
            repo_url="https://github.com/newsroom/harnessed-research-agents",
            observed_at=FIXED_NOW - timedelta(days=2),
            stars=100,
            forks=12,
            watchers=24,
        )
        current = CodeRepositoryObservation(
            repo_url="https://github.com/newsroom/harnessed-research-agents",
            observed_at=FIXED_NOW,
            stars=124,
            forks=14,
            watchers=27,
        )
        growth = compute_star_growth(current, previous)
        return CodeRepositoryProfile(
            repo_url="https://github.com/newsroom/harnessed-research-agents",
            owner="newsroom",
            name="harnessed-research-agents",
            stars=current.stars,
            forks=current.forks,
            watchers=current.watchers,
            open_issues=3,
            license="MIT",
            default_branch="main",
            last_commit_at=FIXED_NOW,
            release_count=2,
            has_readme=True,
            has_examples=True,
            has_training_script=False,
            has_inference_demo=True,
            paper_code_alignment="verified",
            star_growth_daily=growth["star_growth_daily"],
            trend_label=growth["trend_label"],
            observations=[previous, current],
            metadata={"metrics_source": "github_repository_port"},
        )

    def fetch_observation(self, repo_url: str) -> CodeRepositoryObservation:
        return CodeRepositoryObservation(
            repo_url="https://github.com/newsroom/harnessed-research-agents",
            observed_at=FIXED_NOW,
            stars=124,
            forks=14,
            watchers=27,
        )


class FakeResearchRAGRuntime:
    def __init__(
        self,
        *,
        missing_required_evidence: bool = False,
        conflict: bool = False,
        query_budget_exhausted: bool = False,
    ) -> None:
        self.missing_required_evidence = missing_required_evidence
        self.conflict = conflict
        self.query_budget_exhausted = query_budget_exhausted
        self.last_context_pack: RAGContextPack | None = None

    def run(self, *, session_spec: Any, document: ResearchDocument) -> ResearchRAGContext:
        if self.query_budget_exhausted:
            gap = ResearchRAGGapReport(missing_information=["rag_query_budget_exhausted"])
            context_pack_id = f"rag-context://{session_spec.session_id}/budget-exhausted"
            self.last_context_pack = RAGContextPack(
                pack_id=context_pack_id,
                query=session_spec.goal.question,
                context_refs=[],
                source_refs=[],
                gap_report=gap.to_dict(),
                metadata={
                    **_rag_session_identity(session_spec),
                    "halted": True,
                    "reason": "rag query budget exhausted",
                },
            )
            return ResearchRAGContext(
                context_id="research-rag-context-budget-exhausted",
                paper_id=document.paper_id,
                goal=_research_goal_from_session(session_spec),
                accepted_evidence=[],
                rejected_evidence=[],
                conflicting_evidence=[],
                gap_report=gap,
                source_refs=[document.lineage.source_refs[0]],
                lineage=SourceLineage(source_refs=[document.lineage.source_refs[0]], source_hash=document.source_hash),
                metadata=_rag_context_metadata(
                    session_spec,
                    context_pack_id=context_pack_id,
                ),
            )
        accepted: list[ResearchEvidenceItem] = []
        source_sections = document.sections or [
            ResearchSection(
                section_id="sec-method",
                title="Recovered Method Evidence",
                level=1,
                text="Recovered raw source text says Harness controls routing and gates.",
                source_ref=document.lineage.source_refs[0],
            ),
            ResearchSection(
                section_id="sec-experiments",
                title="Recovered Experiment Evidence",
                level=1,
                text="Recovered raw source text says experiments evaluate reader payload fidelity.",
                source_ref=document.lineage.source_refs[0],
            ),
        ]
        for section in source_sections:
            evidence_type = {
                "sec-method": "method",
                "sec-experiments": "experiment",
                "sec-limitations": "limitation",
            }.get(section.section_id, "claim_support")
            accepted.append(
                ResearchEvidenceItem(
                    evidence_id=stable_research_id("evidence", document.paper_id, section.section_id),
                    paper_id=document.paper_id,
                    title=section.title,
                    summary=section.text[:220],
                    evidence_type=evidence_type,
                    source_ref=section.source_ref,
                    span_refs=[section.section_id],
                    confidence=0.9,
                    lineage=SourceLineage(source_refs=[section.source_ref], source_hash=document.source_hash),
                )
            )
        missing = ["experiment", "limitation"] if self.missing_required_evidence else []
        conflicting = [accepted[-1]] if self.conflict and accepted else []
        gap = ResearchRAGGapReport(
            missing_information=missing,
            conflicting_evidence=[item.evidence_id for item in conflicting],
            rejected_reasons=[],
        )
        context_pack_id = f"rag-context://{session_spec.session_id}/fake"
        self.last_context_pack = RAGContextPack(
            pack_id=context_pack_id,
            query=session_spec.goal.question,
            evidence=[
                EvidencePack(
                    evidence_id=item.evidence_id,
                    title=item.title,
                    summary=item.summary,
                    source_refs=(item.source_ref, *item.span_refs),
                    confidence=item.confidence,
                    freshness="fresh",
                    lineage=tuple(item.lineage.source_refs),
                )
                for item in accepted
            ],
            context_refs=[section.section_id for section in document.sections],
            source_refs=[item.source_ref for item in accepted],
            gap_report=gap.to_dict(),
            metadata=_rag_session_identity(session_spec),
        )
        return ResearchRAGContext(
            context_id=stable_research_id("research_rag_context", document.paper_id, session_spec.session_id),
            paper_id=document.paper_id,
            goal=_research_goal_from_session(session_spec),
            accepted_evidence=accepted,
            rejected_evidence=[],
            conflicting_evidence=conflicting,
            gap_report=gap,
            source_refs=[item.source_ref for item in accepted],
            lineage=SourceLineage(source_refs=[item.source_ref for item in accepted], source_hash=document.source_hash),
            metadata=_rag_context_metadata(
                session_spec,
                context_pack_id=context_pack_id,
            ),
        )


def _research_goal_from_session(session_spec: Any):
    from backend.research.rag import ResearchRetrievalGoal

    return ResearchRetrievalGoal(
        goal_id=session_spec.goal.goal_id,
        paper_id=session_spec.goal.metadata["paper_id"],
        question=session_spec.goal.question,
        required_evidence_types=list(session_spec.goal.required_evidence_types),
        target_sections=list(session_spec.goal.target_entities),
        allowed_source_refs=list(session_spec.source_policy["allowed_source_refs"]),
        allowed_memory_namespaces=list(session_spec.allowed_memory_namespaces),
        constraints=session_spec.goal.constraints,
        metadata=_rag_actor_metadata(session_spec),
    )


def _rag_actor_metadata(session_spec: Any) -> dict[str, Any]:
    metadata = dict(session_spec.goal.metadata)
    metadata["memory_namespace"] = session_spec.allowed_memory_namespaces[0]
    return metadata


def _rag_session_identity(session_spec: Any) -> dict[str, Any]:
    graph_identity = session_spec.graph_identity.to_dict()
    return {
        **graph_identity,
        "graph_identity": graph_identity,
        "session_id": session_spec.session_id,
    }


def _rag_context_metadata(
    session_spec: Any,
    *,
    context_pack_id: str,
) -> dict[str, Any]:
    return {
        **_rag_actor_metadata(session_spec),
        **_rag_session_identity(session_spec),
        "context_pack_id": context_pack_id,
        "transcript_ref": f"rag-transcript://{session_spec.session_id}/fake",
    }
