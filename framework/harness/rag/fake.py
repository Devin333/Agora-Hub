from __future__ import annotations

from typing import Any

from framework.events.canonical import checksum_for
from framework.harness.context.fake import FakeContextAssembler
from framework.harness.context.models import (
    CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2,
    ContextGraphIdentity,
)
from framework.harness.graph.versioning import (
    GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
    HARNESS_CONDITION_POLICY_VERSION,
    HARNESS_GRAPH_ONLY_COMPILER_VERSION,
)
from framework.harness.memory.fake import FakeMemoryPort
from framework.harness.rag.context_pack_assembler import FakeRAGContextPackAssembler
from framework.harness.rag.gates import RAGGateSuite
from framework.harness.rag.models import (
    RAGBudget,
    RAGSessionSpec,
    RetrievalGoal,
    RetrievalOperation,
    RetrievalPlanCandidate,
    RetrievalStepSpec,
)
from framework.harness.rag.planner import DeterministicRAGPlanner, deterministic_query_text
from framework.harness.rag.session import BoundedRAGSessionController, RAGSessionResult
from framework.harness.rag.source_verifier import FakeSourceVerifier
from framework.harness.retrieval.evidence_pack import EvidencePack
from framework.harness.retrieval.fake import FakeRetrievalPort


class FakeRAGPlanner(DeterministicRAGPlanner):
    def __init__(self, plans: tuple[RetrievalPlanCandidate, ...] = ()) -> None:
        self.plans = list(plans)
        self.requests: list[dict[str, Any]] = []

    def plan(
        self,
        spec: RAGSessionSpec,
        *,
        round_index: int,
        gap_report: dict[str, Any],
        executed_queries: tuple[str, ...] = (),
    ) -> RetrievalPlanCandidate:
        self.requests.append({
            "spec": spec.to_dict(),
            "round_index": round_index,
            "gap_report": dict(gap_report),
            "executed_queries": list(executed_queries),
        })
        if self.plans:
            return self.plans.pop(0) if len(self.plans) > 1 else self.plans[0]
        query_text = deterministic_query_text(
            spec,
            gap_report=gap_report,
            executed_queries=executed_queries,
        )
        missing = tuple(gap_report.get("missing_evidence_types", ()))
        return RetrievalPlanCandidate(
            candidate_id=f"fake-plan:{spec.session_id}:{round_index + 1}",
            queries=(
                RetrievalStepSpec(
                    step_id=f"fake-query:{round_index + 1}",
                    operation=RetrievalOperation.SEARCH_CORPUS,
                    query=query_text,
                    corpus=spec.allowed_corpora[0],
                    max_results=5,
                    metadata={
                        "scope_ref": spec.source_policy.get("scope_ref"),
                        "evidence_type": spec.goal.required_evidence_types[min(round_index, len(spec.goal.required_evidence_types) - 1)],
                    },
                ),
            ),
            source_reading_plan=(
                RetrievalStepSpec(
                    step_id=f"fake-source-read:{round_index + 1}",
                    operation=RetrievalOperation.READ_SOURCE,
                    query=query_text,
                    source_refs=("source://paper/sparse-mixture-reader#section=method",),
                    max_source_reads=1,
                    metadata={"tool_name": "retrieval.read_source", "scope_ref": spec.source_policy.get("scope_ref")},
                ),
            ),
            memory_recall_plan=(
                RetrievalStepSpec(
                    step_id=f"fake-memory:{round_index + 1}",
                    operation=RetrievalOperation.RECALL_MEMORY,
                    query=query_text,
                    memory_namespace=spec.allowed_memory_namespaces[0],
                    max_results=_per_round_memory_limit(spec.budget.max_memory_hits, spec.budget.max_rounds),
                    metadata={"scope_ref": spec.source_policy.get("scope_ref")},
                ),
            ),
            expected_evidence=spec.goal.required_evidence_types,
            expected_gaps=missing,
            risks=("source_conflict_needs_review",),
            confidence=0.74,
            metadata={"planner": "fake-bounded-rag"},
        )


class FakeRAGGateSuite(RAGGateSuite):
    pass


class FakeRAGSessionController(BoundedRAGSessionController):
    def __init__(
        self,
        retrieval: FakeRetrievalPort | None = None,
        planner: FakeRAGPlanner | None = None,
        memory: FakeMemoryPort | None = None,
    ) -> None:
        self.fake_context_assembler = FakeContextAssembler()
        super().__init__(
            retrieval=retrieval or FakeRetrievalPort(fake_research_evidence_packs()),
            memory=memory or FakeMemoryPort(fake_reader_repair_memory()),
            planner=planner or FakeRAGPlanner(),
            source_verifier=FakeSourceVerifier(),
            context_pack_assembler=FakeRAGContextPackAssembler(self.fake_context_assembler),
            gates=FakeRAGGateSuite(),
        )

    def run_fake_session(self, spec: RAGSessionSpec | None = None) -> RAGSessionResult:
        return self.run(spec or fake_rag_session_spec())

def fake_rag_session_spec(
    *,
    session_id: str = "rag-session:sparse-mixture-reader",
    required_evidence_types: tuple[str, ...] = ("method",),
    budget: RAGBudget | None = None,
) -> RAGSessionSpec:
    return RAGSessionSpec(
        session_id=session_id,
        graph_identity=_fake_rag_graph_identity(),
        goal=RetrievalGoal(
            goal_id="goal:verify-reader-method",
            question="How does the sparse mixture reader repair missing method sections?",
            required_evidence_types=required_evidence_types,
            target_entities=("Sparse Mixture Reader", "reader repair"),
            known_context_refs=("paper://sparse-mixture-reader",),
            missing_information=("method evidence", "repair precedent"),
            constraints={"scope_ref": "paper://sparse-mixture-reader"},
        ),
        allowed_corpora=("research-papers",),
        allowed_memory_namespaces=("research.reader_repair",),
        allowed_tools=("retrieval.read_source",),
        source_policy={"scope_ref": "paper://sparse-mixture-reader", "min_confidence": 0.5},
        budget=budget or RAGBudget.safe_default(),
        context_policy={"max_output_tokens": 1024, "reserved_output_tokens": 256},
        metadata={"fixture": "bounded_agentic_rag"},
    )


def _fake_rag_graph_identity() -> ContextGraphIdentity:
    projection = {
        "schema_version": CONTEXT_GRAPH_TASK_PLAN_STAGE_IDENTITY_SCHEMA_V2,
        "run_id": "run:research-runtime-fixture",
        "graph_schema_version": GRAPH_ONLY_NORMALIZED_HARNESS_GRAPH_SCHEMA,
        "compiler_version": HARNESS_GRAPH_ONLY_COMPILER_VERSION,
        "condition_policy_version": HARNESS_CONDITION_POLICY_VERSION,
        "graph_id": "research.bounded_rag.graph",
        "graph_version": "1",
        "graph_checksum": "sha256:" + "1" * 64,
        "stage_id": "build_evidence_context",
        "stage_binding_checksum": "sha256:" + "2" * 64,
        "graph_ref": "research.bounded_rag.graph@1",
    }
    return ContextGraphIdentity(
        run_id=projection["run_id"],
        graph_id=projection["graph_id"],
        graph_version=projection["graph_version"],
        graph_ref=projection["graph_ref"],
        graph_schema_version=projection["graph_schema_version"],
        compiler_version=projection["compiler_version"],
        condition_policy_version=projection["condition_policy_version"],
        graph_checksum=projection["graph_checksum"],
        stage_id=projection["stage_id"],
        stage_binding_checksum=projection["stage_binding_checksum"],
        stage_identity_schema=projection["schema_version"],
        stage_identity_checksum=checksum_for(projection),
    )


def fake_research_evidence_packs() -> tuple[EvidencePack, ...]:
    return (
        EvidencePack(
            evidence_id="evidence:sparse-mixture-reader:method",
            title="Sparse Mixture Reader method paragraph",
            summary=(
                "The method section describes a bounded reader that retrieves abstract, method, "
                "experiment table, and citation spans before synthesizing a repair candidate."
            ),
            source_refs=(
                "source://paper/sparse-mixture-reader#section=method",
                "source://paper/sparse-mixture-reader#paragraph=method-3",
            ),
            confidence=0.91,
            freshness="static-fixture",
            lineage=("retrieval.fake", "paper-section.method", "bounded-rag.fixture"),
            metadata={
                "section_refs": ["source://paper/sparse-mixture-reader#paragraph=method-3"],
                "claim_refs": ["claim://reader-repair#bounded-method-retrieval"],
            },
        ),
        EvidencePack(
            evidence_id="evidence:sparse-mixture-reader:experiment",
            title="Experiment table snippet for repair accuracy",
            summary=(
                "Table 2 reports reader repair accuracy after adding citation-aware source reads "
                "and procedural repair memory recall."
            ),
            source_refs=(
                "source://paper/sparse-mixture-reader#table=2",
                "source://paper/sparse-mixture-reader#citation=14",
            ),
            confidence=0.86,
            freshness="static-fixture",
            lineage=("retrieval.fake", "paper-table.table-2", "bounded-rag.fixture"),
            metadata={
                "section_refs": ["source://paper/sparse-mixture-reader#table=2"],
                "claim_refs": ["claim://reader-repair#citation-aware-repair"],
            },
        ),
    )


def fake_reader_repair_memory() -> tuple[dict[str, Any], ...]:
    return (
        {
            "memory_ref": "memory://research.reader_repair/success/method-gap-2026-01",
            "namespace": "research.reader_repair",
            "case_type": "successful_repair_case",
            "title": "Recovered missing method section with citation-adjacent source read",
            "summary": (
                "A prior reader repair recovered an omitted method paragraph by reading the "
                "citation-adjacent source and preserving section lineage before synthesis."
            ),
            "outcome": "success",
            "relevance": 0.93,
            "source_refs": ("source://paper/sparse-mixture-reader#paragraph=method-3",),
        },
        {
            "memory_ref": "memory://research.reader_repair/failure/table-only-2026-02",
            "namespace": "research.reader_repair",
            "case_type": "failed_repair_case",
            "title": "Table-only repair failed when lineage was missing",
            "summary": (
                "A previous repair that used only experiment table snippets failed the evidence "
                "gate because paragraph-level method lineage was absent."
            ),
            "outcome": "failure",
            "relevance": 0.78,
            "source_refs": ("source://paper/sparse-mixture-reader#table=2",),
        },
        {
            "memory_ref": "memory://research.reader_repair/procedure/citation-aware-method",
            "namespace": "research.reader_repair",
            "case_type": "procedural_strategy",
            "title": "Citation-aware method recovery strategy",
            "summary": (
                "When method evidence is sparse, retrieve abstract, method paragraph, table note, "
                "and cited baseline refs before accepting the repair."
            ),
            "outcome": "strategy",
            "relevance": 0.88,
            "source_refs": ("procedure://reader-repair/citation-aware-method",),
        },
    )


def _per_round_memory_limit(total: int, rounds: int) -> int:
    return max(1, min(4, total // max(rounds, 1) or total or 1))


__all__ = [
    "FakeRAGGateSuite",
    "FakeRAGPlanner",
    "FakeRAGSessionController",
    "fake_rag_session_spec",
    "fake_reader_repair_memory",
    "fake_research_evidence_packs",
]
