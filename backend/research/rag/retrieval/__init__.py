from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "AnswerContextAssembler",
    "CANDIDATE_EXPERIMENT_VERSION",
    "AnswerContextSelection",
    "AnswerGenerator",
    "ChannelSpec",
    "CandidateRecallResult",
    "CandidateRecallStage",
    "ClaimRecord",
    "ClaimSearchHit",
    "ChildCandidateScorer",
    "ChildRankingResult",
    "ChildRankingStage",
    "ContextExpander",
    "CrossRefContextExpander",
    "DEFAULT_RETRIEVAL_POLICY",
    "GeneratedAnswer",
    "ExpanderSpec",
    "FusionSpec",
    "FormulaContextExpander",
    "NEWS_PAPER_RAG_POLICY_CONFIG_ENV",
    "NEWS_PAPER_RAG_POLICY_ENV",
    "PAPER_BLIND_SEMANTIC_RAG_V1_POLICY",
    "PAPER_FORMULA_RAG_V1_POLICY",
    "PAPER_HYBRID_RRF_RAG_V1_POLICY",
    "PAPER_VISUAL_RAG_TUNED_POLICY",
    "PaperVisualFusionWeights",
    "ParentContextExpander",
    "PaperClaimIndex",
    "PaperClaimSearchPort",
    "QueryIntent",
    "QueryPlanner",
    "RankedHit",
    "RankedList",
    "RecallChannel",
    "ResearchRetriever",
    "RetrievalPolicy",
    "RetrievalPlan",
    "RetrievalPipeline",
    "RetrievalRequest",
    "RetrievalResult",
    "RetrievalRoute",
    "RerankSpec",
    "RerankCascade",
    "RetrievalDegradation",
    "RetrievalMetricsBuilder",
    "RetrievalTrace",
    "StructuralContextExpander",
    "SupplementalTableHitExpander",
    "TableContextExpander",
    "build_retrieval_policy",
    "build_retrieval_policy_from_config",
    "build_retrieval_policy_from_config_file",
    "build_retrieval_policy_from_env",
    "build_retrieval_route",
    "build_retrieval_pipeline",
    "classify_query_intent",
    "extract_claim_records",
    "field_rerank_passage",
    "fuse_chunk_rankings",
    "fuse_ranked_hits",
    "fuse_visual_retrieval_scores",
    "retrieval_policy_enables_lightweight_reranker",
    "policy_config_hash",
    "stable_policy_config",
    "visual_fusion_score",
    "CandidatePlanEvaluation",
    "ResearchCandidatePlanner",
    "RetrievalCandidateExperimentConfig",
    "evaluate_retrieval_candidate",
    "with_retrieval_scores",
]

_LAZY = LazyExports(
    __name__,
    {
        "CANDIDATE_EXPERIMENT_VERSION": ("backend.research.rag.retrieval.candidate_experiment", "CANDIDATE_EXPERIMENT_VERSION"),
        "CandidatePlanEvaluation": ("backend.research.rag.retrieval.candidate_experiment", "CandidatePlanEvaluation"),
        "ResearchCandidatePlanner": ("backend.research.rag.retrieval.candidate_experiment", "ResearchCandidatePlanner"),
        "RetrievalCandidateExperimentConfig": ("backend.research.rag.retrieval.candidate_experiment", "RetrievalCandidateExperimentConfig"),
        "evaluate_retrieval_candidate": ("backend.research.rag.retrieval.candidate_experiment", "evaluate_retrieval_candidate"),
        "AnswerContextAssembler": ("backend.research.rag.retrieval.paper_answer_generator", "AnswerContextAssembler"),
        "AnswerContextSelection": ("backend.research.rag.retrieval.paper_answer_generator", "AnswerContextSelection"),
        "AnswerGenerator": ("backend.research.rag.retrieval.paper_answer_generator", "AnswerGenerator"),
        "GeneratedAnswer": ("backend.research.rag.retrieval.paper_answer_generator", "GeneratedAnswer"),
        "QueryIntent": ("backend.research.rag.retrieval.paper_policy", "QueryIntent"),
        "RetrievalRoute": ("backend.research.rag.retrieval.paper_policy", "RetrievalRoute"),
        "build_retrieval_route": ("backend.research.rag.retrieval.paper_policy", "build_retrieval_route"),
        "classify_query_intent": ("backend.research.rag.retrieval.paper_policy", "classify_query_intent"),
        "ChannelSpec": ("backend.research.rag.retrieval.plan", "ChannelSpec"),
        "ExpanderSpec": ("backend.research.rag.retrieval.plan", "ExpanderSpec"),
        "FusionSpec": ("backend.research.rag.retrieval.plan", "FusionSpec"),
        "RerankSpec": ("backend.research.rag.retrieval.plan", "RerankSpec"),
        "RetrievalPlan": ("backend.research.rag.retrieval.plan", "RetrievalPlan"),
        "QueryPlanner": ("backend.research.rag.retrieval.planner", "QueryPlanner"),
        "ClaimRecord": ("backend.research.rag.retrieval.paper_claim_index", "ClaimRecord"),
        "ClaimSearchHit": ("backend.research.rag.retrieval.paper_claim_index", "ClaimSearchHit"),
        "PaperClaimIndex": ("backend.research.rag.retrieval.paper_claim_index", "PaperClaimIndex"),
        "PaperClaimSearchPort": ("backend.research.rag.retrieval.paper_claim_index", "PaperClaimSearchPort"),
        "extract_claim_records": ("backend.research.rag.retrieval.paper_claim_index", "extract_claim_records"),
        "RetrievalRequest": ("backend.research.rag.retrieval.contracts", "RetrievalRequest"),
        "RetrievalResult": ("backend.research.rag.retrieval.contracts", "RetrievalResult"),
        "RetrievalMetricsBuilder": ("backend.research.rag.retrieval.metrics", "RetrievalMetricsBuilder"),
        "ResearchRetriever": ("backend.research.rag.retrieval.paper_retriever", "ResearchRetriever"),
        "DEFAULT_RETRIEVAL_POLICY": ("backend.research.rag.retrieval.policies", "DEFAULT_RETRIEVAL_POLICY"),
        "NEWS_PAPER_RAG_POLICY_CONFIG_ENV": ("backend.research.rag.retrieval.policies", "NEWS_PAPER_RAG_POLICY_CONFIG_ENV"),
        "NEWS_PAPER_RAG_POLICY_ENV": ("backend.research.rag.retrieval.policies", "NEWS_PAPER_RAG_POLICY_ENV"),
        "PAPER_BLIND_SEMANTIC_RAG_V1_POLICY": ("backend.research.rag.retrieval.policies", "PAPER_BLIND_SEMANTIC_RAG_V1_POLICY"),
        "PAPER_FORMULA_RAG_V1_POLICY": ("backend.research.rag.retrieval.policies", "PAPER_FORMULA_RAG_V1_POLICY"),
        "PAPER_HYBRID_RRF_RAG_V1_POLICY": ("backend.research.rag.retrieval.policies", "PAPER_HYBRID_RRF_RAG_V1_POLICY"),
        "PAPER_VISUAL_RAG_TUNED_POLICY": ("backend.research.rag.retrieval.policies", "PAPER_VISUAL_RAG_TUNED_POLICY"),
        "RetrievalPolicy": ("backend.research.rag.retrieval.policies", "RetrievalPolicy"),
        "build_retrieval_policy": ("backend.research.rag.retrieval.policies", "build_retrieval_policy"),
        "build_retrieval_policy_from_config": ("backend.research.rag.retrieval.policies", "build_retrieval_policy_from_config"),
        "build_retrieval_policy_from_config_file": ("backend.research.rag.retrieval.policies", "build_retrieval_policy_from_config_file"),
        "build_retrieval_policy_from_env": ("backend.research.rag.retrieval.policies", "build_retrieval_policy_from_env"),
        "retrieval_policy_enables_lightweight_reranker": ("backend.research.rag.retrieval.policies", "retrieval_policy_enables_lightweight_reranker"),
        "PaperVisualFusionWeights": ("backend.research.rag.retrieval.paper_visual_retrieval", "PaperVisualFusionWeights"),
        "fuse_visual_retrieval_scores": ("backend.research.rag.retrieval.paper_visual_retrieval", "fuse_visual_retrieval_scores"),
        "visual_fusion_score": ("backend.research.rag.retrieval.paper_visual_retrieval", "visual_fusion_score"),
        "with_retrieval_scores": ("backend.research.rag.retrieval.paper_visual_retrieval", "with_retrieval_scores"),
        "RetrievalPipeline": ("backend.research.rag.retrieval.pipeline", "RetrievalPipeline"),
        "RankedHit": ("backend.research.rag.retrieval.channels", "RankedHit"),
        "RankedList": ("backend.research.rag.retrieval.channels", "RankedList"),
        "RecallChannel": ("backend.research.rag.retrieval.channels", "RecallChannel"),
        "ContextExpander": ("backend.research.rag.retrieval.expanders", "ContextExpander"),
        "CrossRefContextExpander": ("backend.research.rag.retrieval.expanders", "CrossRefContextExpander"),
        "FormulaContextExpander": ("backend.research.rag.retrieval.expanders", "FormulaContextExpander"),
        "ParentContextExpander": ("backend.research.rag.retrieval.expanders", "ParentContextExpander"),
        "StructuralContextExpander": ("backend.research.rag.retrieval.expanders", "StructuralContextExpander"),
        "SupplementalTableHitExpander": ("backend.research.rag.retrieval.expanders", "SupplementalTableHitExpander"),
        "TableContextExpander": ("backend.research.rag.retrieval.expanders", "TableContextExpander"),
        "build_retrieval_pipeline": ("backend.research.rag.retrieval.factory", "build_retrieval_pipeline"),
        "fuse_chunk_rankings": ("backend.research.rag.retrieval.fusion", "fuse_chunk_rankings"),
        "fuse_ranked_hits": ("backend.research.rag.retrieval.fusion", "fuse_ranked_hits"),
        "policy_config_hash": ("backend.research.rag.retrieval.policy_config", "policy_config_hash"),
        "stable_policy_config": ("backend.research.rag.retrieval.policy_config", "stable_policy_config"),
        "ChildRankingResult": ("backend.research.rag.retrieval.ranking_stage", "ChildRankingResult"),
        "ChildRankingStage": ("backend.research.rag.retrieval.ranking_stage", "ChildRankingStage"),
        "CandidateRecallResult": ("backend.research.rag.retrieval.recall_stage", "CandidateRecallResult"),
        "CandidateRecallStage": ("backend.research.rag.retrieval.recall_stage", "CandidateRecallStage"),
        "RerankCascade": ("backend.research.rag.retrieval.rerank", "RerankCascade"),
        "field_rerank_passage": ("backend.research.rag.retrieval.rerank", "field_rerank_passage"),
        "ChildCandidateScorer": ("backend.research.rag.retrieval.scoring", "ChildCandidateScorer"),
        "RetrievalDegradation": ("backend.research.rag.retrieval.trace", "RetrievalDegradation"),
        "RetrievalTrace": ("backend.research.rag.retrieval.trace", "RetrievalTrace"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.rag.retrieval.candidate_experiment import (
        CANDIDATE_EXPERIMENT_VERSION,
        CandidatePlanEvaluation,
        ResearchCandidatePlanner,
        RetrievalCandidateExperimentConfig,
        evaluate_retrieval_candidate,
    )
    from backend.research.rag.retrieval.paper_answer_generator import (
        AnswerContextAssembler,
        AnswerContextSelection,
        AnswerGenerator,
        GeneratedAnswer,
    )
    from backend.research.rag.retrieval.paper_policy import (
        QueryIntent,
        RetrievalRoute,
        build_retrieval_route,
        classify_query_intent,
    )
    from backend.research.rag.retrieval.plan import (
        ChannelSpec,
        ExpanderSpec,
        FusionSpec,
        RerankSpec,
        RetrievalPlan,
    )
    from backend.research.rag.retrieval.planner import (
        QueryPlanner,
    )
    from backend.research.rag.retrieval.paper_claim_index import (
        ClaimRecord,
        ClaimSearchHit,
        PaperClaimIndex,
        PaperClaimSearchPort,
        extract_claim_records,
    )
    from backend.research.rag.retrieval.contracts import (
        RetrievalRequest,
        RetrievalResult,
    )
    from backend.research.rag.retrieval.metrics import (
        RetrievalMetricsBuilder,
    )
    from backend.research.rag.retrieval.paper_retriever import (
        ResearchRetriever,
    )
    from backend.research.rag.retrieval.policies import (
        DEFAULT_RETRIEVAL_POLICY,
        NEWS_PAPER_RAG_POLICY_CONFIG_ENV,
        NEWS_PAPER_RAG_POLICY_ENV,
        PAPER_BLIND_SEMANTIC_RAG_V1_POLICY,
        PAPER_FORMULA_RAG_V1_POLICY,
        PAPER_HYBRID_RRF_RAG_V1_POLICY,
        PAPER_VISUAL_RAG_TUNED_POLICY,
        RetrievalPolicy,
        build_retrieval_policy,
        build_retrieval_policy_from_config,
        build_retrieval_policy_from_config_file,
        build_retrieval_policy_from_env,
        retrieval_policy_enables_lightweight_reranker,
    )
    from backend.research.rag.retrieval.paper_visual_retrieval import (
        PaperVisualFusionWeights,
        fuse_visual_retrieval_scores,
        visual_fusion_score,
        with_retrieval_scores,
    )
    from backend.research.rag.retrieval.pipeline import (
        RetrievalPipeline,
    )
    from backend.research.rag.retrieval.channels import (
        RankedHit,
        RankedList,
        RecallChannel,
    )
    from backend.research.rag.retrieval.expanders import (
        ContextExpander,
        CrossRefContextExpander,
        FormulaContextExpander,
        ParentContextExpander,
        StructuralContextExpander,
        SupplementalTableHitExpander,
        TableContextExpander,
    )
    from backend.research.rag.retrieval.factory import (
        build_retrieval_pipeline,
    )
    from backend.research.rag.retrieval.fusion import (
        fuse_chunk_rankings,
        fuse_ranked_hits,
    )
    from backend.research.rag.retrieval.policy_config import (
        policy_config_hash,
        stable_policy_config,
    )
    from backend.research.rag.retrieval.ranking_stage import (
        ChildRankingResult,
        ChildRankingStage,
    )
    from backend.research.rag.retrieval.recall_stage import (
        CandidateRecallResult,
        CandidateRecallStage,
    )
    from backend.research.rag.retrieval.rerank import (
        RerankCascade,
        field_rerank_passage,
    )
    from backend.research.rag.retrieval.scoring import (
        ChildCandidateScorer,
    )
    from backend.research.rag.retrieval.trace import (
        RetrievalDegradation,
        RetrievalTrace,
    )
