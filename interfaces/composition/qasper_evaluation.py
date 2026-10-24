"""Explicit local composition for offline QASPER application services."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.application.qasper_evaluation import (
    QasperEvaluationConfig, QasperEvaluationService, build_qasper_experiment_manifest,
)
from backend.research.application.qasper_answer_evaluation import (
    QasperAnswerRetrievalProfile,
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


def build_qasper_answer_evaluation_service(
    *,
    dataset_path: Path,
    cache_path: Path,
    query_vector_path: Path,
    output_directory: Path,
    seed: str,
    per_type: int,
    max_cases: int | None,
    judge: bool = True,
    retrieval_profile: str | None = None,
    retrieval_overfetch: int | None = None,
) -> "QasperAnswerEvaluationService":
    """Compose the live end-to-end answer evaluation.

    Unlike the offline retrieval service this reads model configuration from the
    environment: the answer model (Research route), the judge (``NEWS_RAG_EVAL_JUDGE_*``)
    and the embedding provider used only for query texts absent from the cache.
    """
    from backend.research.application.llm_client import (
        build_unity_judge_llm_call,
        build_unity_paper_answer_model,
    )
    from backend.research.application.qasper_answer_evaluation import (
        QasperAnswerEvaluationConfig,
        QasperAnswerEvaluationService,
    )
    from backend.research.rag.evaluation.qasper_retrieval import CachedFixtureFieldIndex
    from backend.research.rag.retrieval.paper_lightweight_reranker import LightweightLexicalReranker
    from backend.research.rag.retrieval.policies import build_retrieval_policy_from_env
    from framework.shared.env import load_root_env
    from infrastructure.research.qasper_query_embedding import ProviderQueryEmbedder
    from infrastructure.research.qasper_query_vector_store import FilesystemQueryVectorStore

    for name, value in (("dataset_path", dataset_path), ("cache_path", cache_path),
                        ("query_vector_path", query_vector_path), ("output_directory", output_directory)):
        if not value.is_absolute():
            raise ValueError(f"{name} must be absolute")
    if retrieval_overfetch is not None and (
        type(retrieval_overfetch) is not int or retrieval_overfetch < 1
    ):
        raise ValueError("retrieval_overfetch must be a positive integer")
    load_root_env()
    root = Path(__file__).resolve().parents[2]
    reader = FilesystemQasperAssets(dataset_path, cache_path, repository_root=root)
    assets = QasperAssetService(reader)
    dimension = assets.cache_identity().dimension
    retrieval_policy = build_retrieval_policy_from_env()
    if retrieval_overfetch is not None:
        retrieval_policy = replace(retrieval_policy, overfetch_multiplier=retrieval_overfetch)
    profile_name = (
        retrieval_profile
        or os.environ.get("NEWS_QASPER_RETRIEVAL_PROFILE")
        or "chunk_only"
    ).strip().casefold()
    if profile_name not in {"chunk_only", "field_only", "field_lightweight"}:
        raise ValueError(
            "retrieval_profile must be 'chunk_only', 'field_only', or 'field_lightweight'"
        )
    field_enabled = profile_name in {"field_only", "field_lightweight"}
    reranker_enabled = profile_name == "field_lightweight"
    # The explicit profile selects component availability. The retrieval pipeline
    # remains authoritative for intent gating; an empty tuple means all intents.
    lightweight = LightweightLexicalReranker() if reranker_enabled else None

    def field_index_factory(chunks, cache, query_vector):
        return CachedFixtureFieldIndex(chunks, cache, query_vector=query_vector)

    return QasperAnswerEvaluationService(
        assets,
        FilesystemQasperArtifacts(),
        query_embedder=ProviderQueryEmbedder.from_env(vector_size=dimension),
        query_vector_store=FilesystemQueryVectorStore(query_vector_path),
        answer_model=build_unity_paper_answer_model(max_tokens=600),
        judge_call=build_unity_judge_llm_call(max_tokens=400) if judge else None,
        retrieval_policy=retrieval_policy,
        field_index_factory=field_index_factory if field_enabled else None,
        reranker=lightweight,
        field_reranker=lightweight,
        retrieval_profile=QasperAnswerRetrievalProfile(
            name=profile_name,
            execution_profile="fixture",
            field_index_enabled=field_enabled,
            reranker_enabled=lightweight is not None,
            field_reranker_enabled=lightweight is not None,
        ),
        context_runtime=_reader_answer_context_runtime(output_directory),
        config=QasperAnswerEvaluationConfig(
            output_directory=str(output_directory), seed=seed, per_type=per_type, max_cases=max_cases,
        ),
    )


def _reader_answer_context_runtime(output_directory: Path):
    """Assemble context exactly as the desktop reader Q&A composition does.

    Snapshots go beside, never inside, the immutable result directory.
    """
    from backend.research.application.paper_rag_session import ContextWindowLimits
    from backend.research.application.qasper_answer_evaluation import AnswerContextRuntime
    from framework.harness.control_plane.harness import InMemoryHarnessEventPort
    from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
    from infrastructure.research.context_runtime import build_research_context_assembler
    from interfaces.composition.research_settings import ResearchRuntimeSettings

    llm = ResearchRuntimeSettings.from_env().llm
    snapshots = FilesystemHarnessArtifactPort(output_directory.parent / f"{output_directory.name}.context")

    def assembler_factory(_spec):
        return build_research_context_assembler(
            artifact_port=snapshots,
            event_port=InMemoryHarnessEventPort(),
            provider=llm.provider,
            model=llm.model,
            max_input_tokens=llm.max_input_tokens,
            max_output_tokens=llm.max_output_tokens,
        )

    return AnswerContextRuntime(
        assembler_factory=assembler_factory,
        window=ContextWindowLimits(llm.max_input_tokens, llm.max_output_tokens),
        run_scope=snapshots.bind_run,
    )


def build_qasper_answer_calibration_service(*, dataset_path: Path, cache_path: Path) -> "QasperAnswerCalibrationService":
    """Offline composition for judge calibration: filesystem only, no models or environment."""
    from backend.research.application.qasper_answer_calibration import QasperAnswerCalibrationService

    for name, value in (("dataset_path", dataset_path), ("cache_path", cache_path)):
        if not value.is_absolute():
            raise ValueError(f"{name} must be absolute")
    root = Path(__file__).resolve().parents[2]
    reader = FilesystemQasperAssets(dataset_path, cache_path, repository_root=root)
    return QasperAnswerCalibrationService(QasperAssetService(reader), FilesystemQasperArtifacts())


def build_qasper_evaluation_service(*, dataset_path: Path | None, cache_path: Path | None) -> QasperEvaluationService:
    """CLI composition: accept only explicit local paths, never read secrets or ambient env config."""
    root = Path(__file__).resolve().parents[2]
    paths = QasperEvaluationPaths(dataset=dataset_path, embedding_cache=cache_path, repository_root=root)
    return compose_qasper_evaluation(paths=paths, manifest_factory=build_qasper_experiment_manifest)
