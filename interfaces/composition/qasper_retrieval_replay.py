"""Offline composition for deterministic QASPER retrieval-only replay."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.application.qasper_retrieval_replay import (
    QasperRetrievalReplayConfig,
    QasperRetrievalReplayProfile,
    QasperRetrievalReplayService,
)
from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureFieldIndex
from backend.research.rag.retrieval.paper_lightweight_reranker import LightweightLexicalReranker
from backend.research.rag.retrieval.policies import RetrievalPolicy
from infrastructure.research.qasper_evaluation import FilesystemQasperArtifacts, FilesystemQasperAssets
from infrastructure.research.qasper_query_vector_store import FilesystemQueryVectorStore


def build_qasper_retrieval_replay_service(
    *,
    dataset_path: Path,
    cache_path: Path,
    query_vector_path: Path,
    source_run_directory: Path,
    output_directory: Path,
) -> QasperRetrievalReplayService:
    """Build a filesystem-only replay; it never loads provider configuration."""
    for name, value in (
        ("dataset_path", dataset_path),
        ("cache_path", cache_path),
        ("query_vector_path", query_vector_path),
        ("source_run_directory", source_run_directory),
        ("output_directory", output_directory),
    ):
        if not isinstance(value, Path) or not value.is_absolute():
            raise ValueError(f"{name} must be an absolute pathlib.Path")
    if not source_run_directory.is_dir():
        raise ValueError("source_run_directory must be an existing directory")
    if not query_vector_path.is_file():
        raise ValueError("query_vector_path must be an existing read-only fixture")
    if output_directory.exists():
        raise ValueError("output_directory must not already exist")

    root = Path(__file__).resolve().parents[2]
    assets = QasperAssetService(
        FilesystemQasperAssets(dataset_path, cache_path, repository_root=root)
    )
    profiles = default_qasper_retrieval_replay_profiles()
    rerankers = {
        profile.name: LightweightLexicalReranker()
        for profile in profiles
        if profile.reranker_enabled or profile.field_reranker_enabled
    }

    def field_index_factory(chunks, cache, query_vector):
        return CachedFixtureFieldIndex(chunks, cache, query_vector=query_vector)

    return QasperRetrievalReplayService(
        assets,
        FilesystemQasperArtifacts(),
        query_vector_store=FilesystemQueryVectorStore(query_vector_path),
        profiles=profiles,
        config=QasperRetrievalReplayConfig(
            source_run_directory=str(source_run_directory),
            output_directory=str(output_directory),
        ),
        field_index_factory=field_index_factory,
        rerankers=rerankers,
    )


def default_qasper_retrieval_replay_profiles() -> tuple[QasperRetrievalReplayProfile, ...]:
    """Paired policies; only candidate/component settings vary across profiles."""
    baseline = RetrievalPolicy(overfetch_multiplier=3)
    expanded = replace(baseline, overfetch_multiplier=10)
    return (
        QasperRetrievalReplayProfile(name="baseline_chunk", policy=baseline),
        QasperRetrievalReplayProfile(name="expanded_chunk", policy=expanded),
        QasperRetrievalReplayProfile(
            name="expanded_field_without_rerank",
            policy=expanded,
            field_index_enabled=True,
        ),
        QasperRetrievalReplayProfile(
            name="expanded_field_rerank",
            policy=expanded,
            field_index_enabled=True,
            reranker_enabled=True,
            field_reranker_enabled=True,
        ),
    )


__all__ = [
    "build_qasper_retrieval_replay_service",
    "default_qasper_retrieval_replay_profiles",
]
