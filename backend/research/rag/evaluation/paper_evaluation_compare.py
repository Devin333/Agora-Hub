from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from backend.research.rag.evaluation.protocol_comparison import (
    PairedComparisonConfig, PairedComparisonResult, compare_saved_scores,
)
from backend.research.rag.evaluation.protocol_dataset import DatasetManifest
from backend.research.rag.evaluation.protocol_execution import EvaluationOptions
from backend.research.rag.evaluation.protocol_scoring import EvaluationScore, MetricRegistry

from backend.research.rag.evaluation.paper_evidence_eval import (
    EvidenceEvalResult,
    EvidenceQAPair,
    EvidenceRetrievalEvaluator,
)
from backend.research.rag.retrieval.paper_retriever import ResearchRetriever


@dataclass(frozen=True)
class EvidenceABDelta:
    metric: str
    k: int | None
    candidate_value: float
    baseline_value: float

    @property
    def delta(self) -> float:
        return self.candidate_value - self.baseline_value


@dataclass
class EvidenceABResult:
    baseline_name: str
    candidate_name: str
    baseline: EvidenceEvalResult
    candidate: EvidenceEvalResult
    deltas: list[EvidenceABDelta] = field(default_factory=list)

    def metric_delta(self, metric: str, k: int | None = None) -> float:
        for delta in self.deltas:
            if delta.metric == metric and delta.k == k:
                return delta.delta
        return 0.0

    def report(self) -> str:
        lines = [
            f"=== Evidence Retrieval A/B ({self.baseline_name} -> {self.candidate_name}) ===",
            f"  baseline_n={self.baseline.total} candidate_n={self.candidate.total}",
        ]
        for delta in self.deltas:
            suffix = f"@{delta.k}" if delta.k is not None else ""
            sign = "+" if delta.delta >= 0 else ""
            lines.append(
                f"  {delta.metric}{suffix:<4} "
                f"baseline={delta.baseline_value:.3f} "
                f"candidate={delta.candidate_value:.3f} "
                f"delta={sign}{delta.delta:.3f}"
            )
        return "\n".join(lines)


class EvidenceABComparator:
    """Runs the same evidence benchmark against two retrieval variants."""

    def __init__(
        self,
        *,
        baseline: ResearchRetriever,
        candidate: ResearchRetriever,
        baseline_name: str = "baseline",
        candidate_name: str = "candidate",
    ) -> None:
        self._baseline = baseline
        self._candidate = candidate
        self._baseline_name = baseline_name
        self._candidate_name = candidate_name

    @staticmethod
    def compare_saved_scores(
        *, baseline_options: EvaluationOptions, candidate_options: EvaluationOptions,
        baseline_scores: Iterable[EvaluationScore], candidate_scores: Iterable[EvaluationScore],
        dataset_manifest: DatasetManifest, registry: MetricRegistry, config: PairedComparisonConfig,
        baseline_name: str = "baseline", candidate_name: str = "candidate",
    ) -> PairedComparisonResult:
        """Formal comparison of saved scores; never calls either retriever."""
        return compare_saved_scores(
            baseline_options=baseline_options, candidate_options=candidate_options,
            baseline_scores=baseline_scores, candidate_scores=candidate_scores,
            dataset_manifest=dataset_manifest, registry=registry, config=config,
            baseline_name=baseline_name, candidate_name=candidate_name,
        )

    def compare(
        self,
        pairs: list[EvidenceQAPair],
        *,
        ks: tuple[int, ...] = (1, 3, 5, 10),
    ) -> EvidenceABResult:
        baseline_result = EvidenceRetrievalEvaluator(self._baseline).evaluate(pairs, ks=ks)
        candidate_result = EvidenceRetrievalEvaluator(self._candidate).evaluate(pairs, ks=ks)
        return compare_evidence_results(
            baseline_result,
            candidate_result,
            ks=ks,
            baseline_name=self._baseline_name,
            candidate_name=self._candidate_name,
        )


def compare_evidence_results(
    baseline: EvidenceEvalResult,
    candidate: EvidenceEvalResult,
    *,
    ks: tuple[int, ...] = (1, 3, 5, 10),
    baseline_name: str = "baseline",
    candidate_name: str = "candidate",
) -> EvidenceABResult:
    deltas: list[EvidenceABDelta] = []
    for k in ks:
        deltas.extend([
            EvidenceABDelta("Hit", k, candidate.hit_rate(k), baseline.hit_rate(k)),
            EvidenceABDelta(
                "EvidenceCoverage",
                k,
                candidate.evidence_coverage(k),
                baseline.evidence_coverage(k),
            ),
            EvidenceABDelta(
                "RequiredTypeCoverage",
                k,
                candidate.required_type_coverage(k),
                baseline.required_type_coverage(k),
            ),
            EvidenceABDelta(
                "SourceLocatorCoverage",
                k,
                candidate.source_locator_coverage(k),
                baseline.source_locator_coverage(k),
            ),
            EvidenceABDelta(
                "CitationAccuracy",
                k,
                candidate.citation_accuracy(k),
                baseline.citation_accuracy(k),
            ),
            EvidenceABDelta(
                "ImageRecall",
                k,
                candidate.image_recall(k),
                baseline.image_recall(k),
            ),
            EvidenceABDelta(
                "VisualEvidenceCoverage",
                k,
                candidate.visual_evidence_coverage(k),
                baseline.visual_evidence_coverage(k),
            ),
        ])
    deltas.extend([
        EvidenceABDelta("MRR", None, candidate.mrr(), baseline.mrr()),
        EvidenceABDelta("AnswerableTotal", None, float(candidate.answerable_total), float(baseline.answerable_total)),
    ])
    return EvidenceABResult(
        baseline_name=baseline_name,
        candidate_name=candidate_name,
        baseline=baseline,
        candidate=candidate,
        deltas=deltas,
    )


__all__ = [
    "EvidenceABComparator",
    "EvidenceABDelta",
    "EvidenceABResult",
    "PairedComparisonConfig",
    "PairedComparisonResult",
    "compare_evidence_results",
    "compare_saved_scores",
]
