from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "EvalResult",
    "EvidenceABComparator",
    "EvidenceABDelta",
    "EvidenceABResult",
    "EvidenceAnswerEvalResult",
    "EvidenceAnswerEvaluator",
    "EvidenceAnswerSample",
    "EvidenceAnswerScores",
    "EvidenceEvalResult",
    "EvidenceGoldenSetBuilder",
    "EvidenceQAPair",
    "EvidenceRegressionReport",
    "EvidenceRetrievalEvaluator",
    "EvidenceSampleResult",
    "FixedWindowBaselineChunker",
    "FixedWindowChunkerConfig",
    "GenerationEvaluator",
    "GenerationEvalResult",
    "GoldenSetBuilder",
    "QAPair",
    "RetrievalEvaluator",
    "build_evidence_pairs_from_chunks",
    "compare_evidence_results",
    "load_evidence_golden_set",
    "save_evidence_golden_set",
]

_LAZY = LazyExports(
    __name__,
    {
        "EvidenceAnswerEvalResult": ("backend.research.rag.evaluation.paper_answer_eval", "EvidenceAnswerEvalResult"),
        "EvidenceAnswerEvaluator": ("backend.research.rag.evaluation.paper_answer_eval", "EvidenceAnswerEvaluator"),
        "EvidenceAnswerSample": ("backend.research.rag.evaluation.paper_answer_eval", "EvidenceAnswerSample"),
        "EvidenceAnswerScores": ("backend.research.rag.evaluation.paper_answer_eval", "EvidenceAnswerScores"),
        "EvidenceEvalResult": ("backend.research.rag.evaluation.paper_evidence_eval", "EvidenceEvalResult"),
        "EvidenceGoldenSetBuilder": ("backend.research.rag.evaluation.paper_evidence_eval", "EvidenceGoldenSetBuilder"),
        "EvidenceQAPair": ("backend.research.rag.evaluation.paper_evidence_eval", "EvidenceQAPair"),
        "EvidenceRetrievalEvaluator": ("backend.research.rag.evaluation.paper_evidence_eval", "EvidenceRetrievalEvaluator"),
        "EvidenceSampleResult": ("backend.research.rag.evaluation.paper_evidence_eval", "EvidenceSampleResult"),
        "build_evidence_pairs_from_chunks": ("backend.research.rag.evaluation.paper_evidence_eval", "build_evidence_pairs_from_chunks"),
        "load_evidence_golden_set": ("backend.research.rag.evaluation.paper_evidence_eval", "load_evidence_golden_set"),
        "save_evidence_golden_set": ("backend.research.rag.evaluation.paper_evidence_eval", "save_evidence_golden_set"),
        "EvidenceABComparator": ("backend.research.rag.evaluation.paper_evaluation_compare", "EvidenceABComparator"),
        "EvidenceABDelta": ("backend.research.rag.evaluation.paper_evaluation_compare", "EvidenceABDelta"),
        "EvidenceABResult": ("backend.research.rag.evaluation.paper_evaluation_compare", "EvidenceABResult"),
        "compare_evidence_results": ("backend.research.rag.evaluation.paper_evaluation_compare", "compare_evidence_results"),
        "EvidenceRegressionReport": ("backend.research.rag.evaluation.paper_evaluation_report", "EvidenceRegressionReport"),
        "FixedWindowBaselineChunker": ("backend.research.rag.evaluation.paper_fixed_window_baseline", "FixedWindowBaselineChunker"),
        "FixedWindowChunkerConfig": ("backend.research.rag.evaluation.paper_fixed_window_baseline", "FixedWindowChunkerConfig"),
        "GenerationEvaluator": ("backend.research.rag.evaluation.paper_generation_eval", "GenerationEvaluator"),
        "GenerationEvalResult": ("backend.research.rag.evaluation.paper_generation_eval", "GenerationEvalResult"),
        "EvalResult": ("backend.research.rag.evaluation.paper_gold_builder", "EvalResult"),
        "GoldenSetBuilder": ("backend.research.rag.evaluation.paper_gold_builder", "GoldenSetBuilder"),
        "QAPair": ("backend.research.rag.evaluation.paper_gold_builder", "QAPair"),
        "RetrievalEvaluator": ("backend.research.rag.evaluation.paper_gold_builder", "RetrievalEvaluator"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from backend.research.rag.evaluation.paper_answer_eval import (
        EvidenceAnswerEvalResult,
        EvidenceAnswerEvaluator,
        EvidenceAnswerSample,
        EvidenceAnswerScores,
    )
    from backend.research.rag.evaluation.paper_evidence_eval import (
        EvidenceEvalResult,
        EvidenceGoldenSetBuilder,
        EvidenceQAPair,
        EvidenceRetrievalEvaluator,
        EvidenceSampleResult,
        build_evidence_pairs_from_chunks,
        load_evidence_golden_set,
        save_evidence_golden_set,
    )
    from backend.research.rag.evaluation.paper_evaluation_compare import (
        EvidenceABComparator,
        EvidenceABDelta,
        EvidenceABResult,
        compare_evidence_results,
    )
    from backend.research.rag.evaluation.paper_evaluation_report import (
        EvidenceRegressionReport,
    )
    from backend.research.rag.evaluation.paper_fixed_window_baseline import (
        FixedWindowBaselineChunker,
        FixedWindowChunkerConfig,
    )
    from backend.research.rag.evaluation.paper_generation_eval import (
        GenerationEvaluator,
        GenerationEvalResult,
    )
    from backend.research.rag.evaluation.paper_gold_builder import (
        EvalResult,
        GoldenSetBuilder,
        QAPair,
        RetrievalEvaluator,
    )
