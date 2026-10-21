"""Explicit local composition for offline QASPER application services."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.application.qasper_evaluation import (
    QasperEvaluationConfig, QasperEvaluationService, build_qasper_experiment_manifest,
)
from infrastructure.research.qasper_evaluation import FilesystemQasperAssets, FilesystemQasperArtifacts


@dataclass(frozen=True)
class QasperEvaluationPaths:
    dataset: Path | None
    embedding_cache: Path | None
    repository_root: Path

    def __post_init__(self) -> None:
        for name in ("dataset", "embedding_cache", "repository_root"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Path):
                raise TypeError(f"{name} must be pathlib.Path or None")
        if (self.dataset is not None and not self.dataset.is_absolute()
                or self.embedding_cache is not None and not self.embedding_cache.is_absolute()
                or not self.repository_root.is_absolute()):
            raise ValueError("configured QASPER input paths must be absolute")


def compose_qasper_evaluation(*, paths: QasperEvaluationPaths, manifest_factory=None,
                             output_directory: Path | None = None, candidate_limits: tuple[int, ...] = (100,),
                             overfetch_multipliers: tuple[int, ...] = (3,),
                             rrf_ks: tuple[int, ...] = (60,),
                             rerank_score_thresholds: tuple[float | None, ...] = (None,),
                             channel_weights: tuple[dict[str, float], ...] = ({},),
                             rerank_enabled: tuple[bool, ...] = (False,),
                             run_id: str | None = None,
                             embedding_model_id: str = "dashscope/text-embedding-v4",
                             embedding_model_version: str = "text-embedding-v4") -> QasperEvaluationService:
    """Build a filesystem-only service; never reads environment variables or secrets."""
    reader = FilesystemQasperAssets(paths.dataset, paths.embedding_cache, repository_root=paths.repository_root)
    assets = QasperAssetService(reader, embedding_model_id=embedding_model_id,
        embedding_model_version=embedding_model_version)
    artifacts = FilesystemQasperArtifacts()
    if output_directory is not None and not output_directory.is_absolute():
        raise ValueError("output_directory must be absolute when provided")
    config = QasperEvaluationConfig(output_directory=str(output_directory) if output_directory else "",
        manifest_factory=manifest_factory or build_qasper_experiment_manifest,
        candidate_limits=candidate_limits, overfetch_multipliers=overfetch_multipliers, rrf_ks=rrf_ks,
        rerank_score_thresholds=rerank_score_thresholds, channel_weights=channel_weights,
        rerank_enabled=rerank_enabled, run_id=run_id)
    return QasperEvaluationService(assets, artifacts, config)


def build_qasper_evaluation_service(*, dataset_path: Path | None, cache_path: Path | None) -> QasperEvaluationService:
    """CLI composition: accept only explicit local paths, never read secrets or ambient env config."""
    root = Path(__file__).resolve().parents[2]
    paths = QasperEvaluationPaths(dataset=dataset_path, embedding_cache=cache_path, repository_root=root)
    return compose_qasper_evaluation(paths=paths, manifest_factory=build_qasper_experiment_manifest)
