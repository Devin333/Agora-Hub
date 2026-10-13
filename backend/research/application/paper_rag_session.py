from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from framework.harness.context.assembler import ContextAssembler
from framework.harness.context.models import ContextGraphIdentity
from framework.harness.memory.ports import MemoryPort
from framework.harness.rag.context_pack_assembler import RAGContextPackAssembler
from framework.harness.rag.session import (
    BoundedRAGSessionController,
    RAGEventRecorder,
    RAGSessionResult,
)
from framework.harness.rag.models import RAGBudget, RAGSessionSpec
from framework.harness.rag.relevance import RelevanceScorerPort
from framework.harness.rag.source_verifier import SourceVerifier

from backend.research.rag.models import ResearchRetrievalGoal
from backend.research.rag.adapters import ResearchRAGPlanWorker
from backend.research.rag.retrieval_port import PaperChunkRetrievalPort
from backend.research.rag.retrieval.paper_retriever import ResearchRetriever
from backend.research.rag.retrieval.candidate_experiment import (
    ResearchCandidatePlanner,
    RetrievalCandidateExperimentConfig,
)
from backend.research.rag.retrieval.policies import RetrievalPolicy
from backend.research.services.rag_policy import ResearchRAGPolicyBuilder
from backend.research.ports.chunk_store import ChunkStorePort
from backend.research.ports.field_embedding_index import FieldEmbeddingSearchPort
from backend.research.ports.visual_chunk_index import VisualChunkSearchPort

if TYPE_CHECKING:
    from backend.research.ports.llm_worker import ResearchCandidateWorkerPort
    from backend.research.ports.reranker import RerankerPort
    from framework.harness.rag.answer_worker import AnswerWorkerPort


class PaperRAGSession:
    """
    Wires ResearchRetriever into BoundedRAGSessionController via PaperChunkRetrievalPort.

    Usage:
        session = PaperRAGSession(chunk_store, reranker=reranker)
        result  = session.run(goal, graph_identity=..., session_id=...)
        pack    = result.context_pack   # EvidenceCandidate list for the LLM
    """

    def __init__(
        self,
        chunk_store: ChunkStorePort,
        *,
        budget: RAGBudget | None = None,
        reranker: "RerankerPort | None" = None,
        field_index: FieldEmbeddingSearchPort | None = None,
        field_reranker: "RerankerPort | None" = None,
        visual_store: VisualChunkSearchPort | None = None,
        retrieval_policy: RetrievalPolicy | None = None,
        plan_worker: "ResearchCandidateWorkerPort | None" = None,
        worker_planner_min_round_index: int = 1,
        candidate_experiment: RetrievalCandidateExperimentConfig | None = None,
        answer_worker: "AnswerWorkerPort | None" = None,
        generation_policy: dict[str, object] | None = None,
        relevance_scorer: RelevanceScorerPort | None = None,
        memory: MemoryPort | None = None,
        context_assembler_factory: (
            Callable[[RAGSessionSpec], ContextAssembler] | None
        ) = None,
        event_recorder: RAGEventRecorder | None = None,
    ) -> None:
        if context_assembler_factory is not None and not callable(
            context_assembler_factory
        ):
            raise TypeError("context_assembler_factory must be callable")
        self._chunk_store = chunk_store
        self._policy_builder = ResearchRAGPolicyBuilder()
        self._budget = budget or RAGBudget.safe_default()
        self._reranker = reranker
        self._field_index = field_index
        self._field_reranker = field_reranker
        self._visual_store = visual_store
        self._retrieval_policy = retrieval_policy
        self._plan_worker = plan_worker
        self._candidate_experiment = candidate_experiment or RetrievalCandidateExperimentConfig()
        self._worker_planner_min_round_index = max(0, worker_planner_min_round_index)
        self._answer_worker = answer_worker
        self._generation_policy = dict(generation_policy or {})
        self._relevance_scorer = relevance_scorer
        self._memory = memory
        self._context_assembler_factory = context_assembler_factory
        self._event_recorder = event_recorder

    def run(
        self,
        goal: ResearchRetrievalGoal,
        *,
        graph_identity: ContextGraphIdentity,
        session_id: str,
        current_section_index: int = 0,
    ) -> RAGSessionResult:
        spec = self._policy_builder.build_session_spec(
            goal=goal,
            graph_identity=graph_identity,
            session_id=session_id,
            budget=self._budget,
            generation_policy=self._generation_policy,
            candidate_experiment=self._candidate_experiment,
        )
        return self.run_spec(spec, current_section_index=current_section_index)

    def run_spec(
        self,
        spec: RAGSessionSpec,
        *,
        current_section_index: int = 0,
    ) -> RAGSessionResult:
        if not isinstance(spec, RAGSessionSpec):
            raise TypeError("spec must be RAGSessionSpec")

        # Per-run controller + port keep reader position and controller state scoped.
        retriever = ResearchRetriever(
            self._chunk_store,
            reranker=self._reranker,
            field_index=self._field_index,
            field_reranker=self._field_reranker,
            visual_store=self._visual_store,
            policy=self._retrieval_policy,
        )
        retrieval_port = PaperChunkRetrievalPort(
            retriever, default_section_index=current_section_index
        )
        planner = None
        if self._candidate_experiment.enabled:
            planner = ResearchCandidatePlanner(
                ResearchRAGPlanWorker(self._plan_worker) if self._plan_worker is not None else None,
                config=self._candidate_experiment,
                min_round_index=self._worker_planner_min_round_index,
            )
        controller_kwargs = {
            "retrieval": retrieval_port,
            "planner": planner,
            "memory": self._memory,
            "event_recorder": self._event_recorder,
        }
        if self._relevance_scorer is not None:
            controller_kwargs["source_verifier"] = SourceVerifier(
                relevance_scorer=self._relevance_scorer
            )
        if self._answer_worker is not None:
            controller_kwargs["answer_worker"] = self._answer_worker
        if self._context_assembler_factory is not None:
            context_assembler = self._context_assembler_factory(spec)
            if not isinstance(context_assembler, ContextAssembler):
                raise TypeError(
                    "context_assembler_factory must return ContextAssembler"
                )
            controller_kwargs["context_pack_assembler"] = RAGContextPackAssembler(
                context_assembler
            )
        controller = BoundedRAGSessionController(**controller_kwargs)
        return controller.run(spec)


__all__ = ["PaperRAGSession"]
