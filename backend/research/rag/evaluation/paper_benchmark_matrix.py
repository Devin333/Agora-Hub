from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from backend.research.rag.evaluation.paper_evaluation_compare import EvidenceABComparator
from backend.research.rag.evaluation.protocol_comparison import PairedComparisonConfig, PairedComparisonResult
from backend.research.rag.evaluation.protocol_dataset import DatasetManifest
from backend.research.rag.evaluation.protocol_execution import EvaluationOptions, EvaluationTrack
from backend.research.rag.evaluation.protocol_scoring import EvaluationScore, MetricRegistry

from backend.research.rag.evaluation.paper_benchmark_suite import (
    BenchmarkSuiteConfig,
    BenchmarkSuiteResult,
    GoldEvidenceJudge,
    LLMCall,
    run_benchmark_suite,
)


@dataclass(frozen=True)
class BenchmarkMatrixDataset:
    name: str
    papers_dir: Path
    image_root: Path | None = None

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        if not name:
            raise ValueError("dataset name is required")
        object.__setattr__(self, "name", _safe_dataset_name(name))
        object.__setattr__(self, "papers_dir", Path(self.papers_dir))
        if self.image_root is not None:
            object.__setattr__(self, "image_root", Path(self.image_root))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BenchmarkMatrixDataset":
        return cls(
            name=str(data.get("name") or ""),
            papers_dir=Path(str(data.get("papers_dir") or data.get("path") or "")),
            image_root=Path(str(data["image_root"])) if data.get("image_root") else None,
        )


@dataclass
class BenchmarkMatrixConfig:
    datasets: tuple[BenchmarkMatrixDataset, ...]
    output_dir: Path
    retrieval_policy: str = "paper_blind_semantic_rag_v1"
    question_profile: str = "blind_semantic"
    max_pairs_per_type: int = 100
    min_papers: int = 20
    target_min_per_type: int = 50
    split_seed: str = "paper-rag-benchmark-v1"
    visual: bool = True
    page_visual: bool = True
    render_page_visual: bool = True
    lightweight_reranker: bool = False
    gold_audit_sample_size: int = 30
    gold_judge_mode: str = "none"
    gold_judge_sample_size: int | None = None
    gold_judge_max_evidence_chars: int = 1600
    gold_evidence_judge: GoldEvidenceJudge | None = None
    answer_eval_enabled: bool = False
    answer_eval_sample_size: int | None = None
    answer_llm_call: LLMCall | None = None
    answer_judge_mode: str = "none"
    answer_judge_sample_size: int | None = None
    answer_judge_llm_call: LLMCall | None = None
    spot_check_sample_size: int = 0
    spot_check_annotations_path: Path | None = None
    quality_thresholds: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkMatrixResult:
    output_dir: Path
    dataset_results: dict[str, BenchmarkSuiteResult]
    protocol_comparisons: dict[str, PairedComparisonResult] = field(default_factory=dict)

    @property
    def ready_for_promotion(self) -> bool:
        # A saved-score comparison is observation, never a publication gate.
        if self.protocol_comparisons:
            return False
        return all(
            result.policy_promotion_checklist.ready_for_promotion
            for result in self.dataset_results.values()
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "output_dir": str(self.output_dir),
            "ready_for_promotion": self.ready_for_promotion,
            "datasets": {
                name: _dataset_summary(result)
                for name, result in sorted(self.dataset_results.items())
            },
        }

        if self.protocol_comparisons:
            payload["protocol_comparisons"] = {
                name: result.to_dict() for name, result in sorted(self.protocol_comparisons.items())
            }
        return payload

    def to_markdown(self) -> str:
        payload = self.to_dict()
        lines = [
            "# Paper RAG Benchmark Matrix",
            "",
            f"- ready_for_promotion: `{payload['ready_for_promotion']}`",
            f"- datasets: `{len(payload['datasets'])}`",
            "",
            "| Dataset | Papers | Pairs | Hit@10 | Eq Hit@10 | MRR | Answer success | Gold judge pass | Ready | Warnings |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
        ]
        for name, item in payload["datasets"].items():
            lines.append(
                f"| `{name}` | `{item['papers_total']}` | `{item['pairs_total']}` | "
                f"`{item['strict_hit_at_10']:.3f}` | `{item['equivalent_hit_at_10']:.3f}` | "
                f"`{item['mrr']:.3f}` | `{_format_optional_metric(item['answer_success_rate'])}` | "
                f"`{_format_optional_metric(item['gold_quality']['judge_pass_rate'])}` | "
                f"`{item['ready_for_promotion']}` | `{item['warning_count']}` |"
            )
        lines.extend([
            "",
            "| Dataset | Claim support | Citation support | Unsupported claims | Human agreement |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for name, item in payload["datasets"].items():
            lines.append(
                f"| `{name}` | `{_format_optional_metric(item['claim_support_rate'])}` | "
                f"`{_format_optional_metric(item['citation_claim_support_rate'])}` | "
                f"`{_format_optional_metric(item['unsupported_claim_rate'])}` | "
                f"`{_format_optional_metric(item['judge_human_agreement'])}` |"
            )
        if self.protocol_comparisons:
            lines.extend(["", "## Saved-score paired comparisons (not a promotion decision)", ""])
            for name, result in sorted(self.protocol_comparisons.items()):
                lines.extend([f"### {name}", "", result.report(), ""])
        return "\n".join(lines).rstrip() + "\n"


def compare_saved_benchmark_matrix(
    *, output_dir: Path, baseline_options: EvaluationOptions,
    baseline_scores: Iterable[EvaluationScore],
    candidate_options: Mapping[str, EvaluationOptions],
    candidate_scores: Mapping[str, Iterable[EvaluationScore]],
    dataset_manifest: DatasetManifest, registry: MetricRegistry,
    config: PairedComparisonConfig, baseline_name: str = "baseline",
) -> BenchmarkMatrixResult:
    """Extend the existing matrix with pure saved-score comparisons.

    Names are display keys only. Missing variants remain explicitly incomplete;
    oracle entries remain diagnostic and cannot become production rankings.
    This function does not create directories, artifacts, or execute the suite.
    """
    unknown = candidate_scores.keys() - candidate_options.keys()
    if unknown:
        raise ValueError(f"scores supplied without typed variant options: {sorted(unknown)}")
    baseline_observations = tuple(baseline_scores)
    results: dict[str, PairedComparisonResult] = {}
    for name, options in candidate_options.items():
        observations = tuple(candidate_scores.get(name, ()))
        oracle = options.selected_variant.track is EvaluationTrack.ORACLE_GENERATION
        results[name] = EvidenceABComparator.compare_saved_scores(
            baseline_options=options if oracle else baseline_options,
            candidate_options=options,
            baseline_scores=observations if oracle else baseline_observations,
            candidate_scores=observations, dataset_manifest=dataset_manifest,
            registry=registry, config=config,
            baseline_name=name if oracle else baseline_name, candidate_name=name,
        )
    return BenchmarkMatrixResult(output_dir=Path(output_dir), dataset_results={}, protocol_comparisons=results)


def run_benchmark_matrix(config: BenchmarkMatrixConfig) -> BenchmarkMatrixResult:
    if not config.datasets:
        raise ValueError("at least one benchmark dataset is required")
    _validate_dataset_inputs(config.datasets)
    output_dir = Path(config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, BenchmarkSuiteResult] = {}
    for dataset in config.datasets:
        dataset_output = output_dir / dataset.name
        results[dataset.name] = run_benchmark_suite(
            BenchmarkSuiteConfig(
                papers_dir=dataset.papers_dir,
                output_dir=dataset_output,
                image_root=dataset.image_root or dataset.papers_dir,
                retrieval_policy=config.retrieval_policy,
                max_pairs_per_type=config.max_pairs_per_type,
                min_papers=config.min_papers,
                target_min_per_type=config.target_min_per_type,
                split_seed=config.split_seed,
                question_profile=config.question_profile,  # type: ignore[arg-type]
                visual=config.visual,
                page_visual=config.page_visual,
                render_page_visual=config.render_page_visual,
                lightweight_reranker=config.lightweight_reranker,
                gold_audit_sample_size=config.gold_audit_sample_size,
                gold_judge_mode=config.gold_judge_mode,
                gold_judge_sample_size=config.gold_judge_sample_size,
                gold_judge_max_evidence_chars=config.gold_judge_max_evidence_chars,
                gold_evidence_judge=config.gold_evidence_judge,
                answer_eval_enabled=config.answer_eval_enabled,
                answer_eval_sample_size=config.answer_eval_sample_size,
                answer_llm_call=config.answer_llm_call,
                answer_judge_mode=config.answer_judge_mode,
                answer_judge_sample_size=config.answer_judge_sample_size,
                answer_judge_llm_call=config.answer_judge_llm_call,
                spot_check_sample_size=config.spot_check_sample_size,
                spot_check_annotations_path=_dataset_annotation_path(
                    config.spot_check_annotations_path,
                    dataset.name,
                ),
                quality_thresholds=dict(config.quality_thresholds),
            )
        )
    result = BenchmarkMatrixResult(output_dir=output_dir, dataset_results=results)
    (output_dir / "benchmark_matrix_report.json").write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "benchmark_matrix_report.md").write_text(result.to_markdown(), encoding="utf-8")
    return result


def load_benchmark_matrix_datasets(path: Path) -> tuple[BenchmarkMatrixDataset, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    raw_datasets = payload.get("datasets") if isinstance(payload, dict) else payload
    if not isinstance(raw_datasets, list):
        raise ValueError("benchmark matrix manifest must contain a 'datasets' list")
    datasets = tuple(
        BenchmarkMatrixDataset.from_dict(item)
        for item in raw_datasets
        if isinstance(item, dict)
    )
    if len(datasets) != len(raw_datasets):
        raise ValueError("each benchmark matrix dataset entry must be an object")
    return datasets


def _validate_dataset_inputs(datasets: tuple[BenchmarkMatrixDataset, ...]) -> None:
    names: set[str] = set()
    for dataset in datasets:
        if dataset.name in names:
            raise ValueError(f"duplicate benchmark dataset name: {dataset.name}")
        names.add(dataset.name)
        if not dataset.papers_dir.exists():
            raise FileNotFoundError(f"benchmark dataset does not exist: {dataset.name}={dataset.papers_dir}")
        if not any(dataset.papers_dir.glob("*/research_document.json")):
            raise FileNotFoundError(
                f"benchmark dataset has no research_document.json files: {dataset.name}={dataset.papers_dir}"
            )


def _dataset_summary(result: BenchmarkSuiteResult) -> dict[str, Any]:
    report = result.candidate_test_report
    retrieval = report.get("retrieval") or {}
    by_10 = (retrieval.get("by_k") or {}).get("10") or {}
    answer = report.get("answer") or None
    generation = report.get("generation") or None
    spot_check = report.get("spot_check") or None
    calibration = ((spot_check or {}).get("judge_human_calibration") or {}) if isinstance(spot_check, dict) else {}
    gold_judge = result.gold_judge
    gold_quality = result.to_dict().get("gold_quality") or {}
    return {
        "output_dir": str(result.output_dir),
        "papers_total": result.papers_total,
        "chunks_total": result.chunks_total,
        "pairs_total": result.pairs_total,
        "strict_hit_at_10": _float_metric(by_10, "strict_hit_rate"),
        "equivalent_hit_at_10": _float_metric(by_10, "equivalent_hit_rate"),
        "mrr": _float_metric(retrieval, "mrr"),
        "equivalent_mrr": _float_metric(retrieval, "equivalent_mrr"),
        "answer_success_rate": _float_metric(answer, "success_rate") if isinstance(answer, dict) else None,
        "true_missing_gold_rate": _float_metric(answer, "true_missing_gold_rate") if isinstance(answer, dict) else None,
        "claim_support_rate": _float_metric(generation, "claim_support_rate") if isinstance(generation, dict) else None,
        "citation_claim_support_rate": (
            _float_metric(generation, "citation_claim_support_rate") if isinstance(generation, dict) else None
        ),
        "unsupported_claim_rate": (
            _float_metric(generation, "unsupported_claim_rate") if isinstance(generation, dict) else None
        ),
        "wrong_citation_rate": _float_metric(generation, "wrong_citation_rate") if isinstance(generation, dict) else None,
        "missing_citation_rate": _float_metric(generation, "missing_citation_rate") if isinstance(generation, dict) else None,
        "judge_human_agreement": (
            _float_metric(calibration, "judge_human_agreement") if calibration else None
        ),
        "gold_judge_sample_size": gold_judge.sample_size if gold_judge is not None else 0,
        "gold_judge_pass_rate": gold_judge.pass_rate if gold_judge is not None else None,
        "gold_judge_failed": gold_judge.failed if gold_judge is not None else 0,
        "gold_judge_error_rate": gold_judge.error_rate if gold_judge is not None else None,
        "gold_quality": gold_quality,
        "ready_for_promotion": result.policy_promotion_checklist.ready_for_promotion,
        "warning_count": len(result.warnings),
        "warnings": list(result.warnings),
    }


def _safe_dataset_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in name.strip())


def _float_metric(values: dict[str, Any] | None, key: str) -> float:
    try:
        return float((values or {}).get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _format_optional_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _dataset_annotation_path(path: Path | None, dataset_name: str) -> Path | None:
    if path is None:
        return None
    path = Path(path)
    if path.suffix:
        return path
    return path / f"{dataset_name}.jsonl"


__all__ = [
    "BenchmarkMatrixConfig",
    "BenchmarkMatrixDataset",
    "BenchmarkMatrixResult",
    "load_benchmark_matrix_datasets",
    "run_benchmark_matrix",
    "compare_saved_benchmark_matrix",
]
