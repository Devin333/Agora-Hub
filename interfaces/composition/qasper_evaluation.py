"""Explicit local composition for offline QASPER application services."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from backend.research.application.qasper_assets import QasperAssetService
from backend.research.application.qasper_evaluation import (
    QasperEvaluationConfig, QasperEvaluationService, build_qasper_experiment_manifest,
)
from backend.research.application.qasper_answer_evaluation import (
    QasperAnswerRetrievalProfile,
)
from infrastructure.research.qasper_evaluation import (
    FilesystemQasperArtifacts,
    FilesystemQasperAssets,
    FilesystemQasperRunCheckpoints,
)


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
    max_context_items: int | None = None,
    max_context_tokens: int | None = None,
    evidence_pool_size: int | None = None,
    evidence_min_passages: int | None = None,
    evidence_min_relevance: int | None = None,
    evidence_render_mode: str | None = None,
    section_navigation: bool = False,
    navigation_max_sections: int | None = None,
    navigation_max_passages_per_section: int | None = None,
    case_ids: tuple[str, ...] | None = None,
    progress=None,
    case_started=None,
    rerun_failed_from: Path | None = None,
) -> "QasperAnswerEvaluationService":
    """Compose the live end-to-end answer evaluation.

    Unlike the offline retrieval service this reads model configuration from the
    environment: the answer model (Research route), the judge (``NEWS_RAG_EVAL_JUDGE_*``)
    and the embedding provider used only for query texts absent from the cache.
    ``evidence_pool_size`` enables answer-side evidence selection: the retrieval pack
    holds that many candidates, the evidence model (``NEWS_RESEARCH_EVIDENCE_LLM_*``)
    scores and condenses them, and Adaptive-k picks at most ``max_context_items``.
    ``section_navigation`` lets the same helper model read the paper outline after
    ranking and add passages from the sections it picks.
    """
    from backend.research.application.llm_client import (
        build_unity_evidence_model,
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
    recovery_source = None
    if rerun_failed_from is not None:
        if not rerun_failed_from.is_absolute():
            raise ValueError("rerun_failed_from must be absolute")
        if case_ids is not None or max_cases is not None:
            raise ValueError("rerun_failed_from cannot be combined with case_ids or max_cases")
        from backend.research.application.qasper_answer_merge import prepare_failed_answer_recovery

        recovery_source = prepare_failed_answer_recovery(
            FilesystemQasperArtifacts(), str(rerun_failed_from),
        )
        case_ids = tuple(recovery_source["case_ids"])
        if not case_ids:
            raise ValueError("the source run has no generation failures to re-run")
        seed = str(recovery_source["seed"])
        per_type = int(recovery_source["per_type"])
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

    answer_model = build_unity_paper_answer_model(max_tokens=600)
    evidence_selector = _qasper_evidence_selector(
        build_unity_evidence_model,
        pool_size=evidence_pool_size,
        min_passages=evidence_min_passages,
        min_relevance=evidence_min_relevance,
        render_mode=evidence_render_mode,
    )
    section_navigator, navigation_config = _qasper_section_navigation(
        build_unity_evidence_model,
        enabled=section_navigation,
        max_sections=navigation_max_sections,
        max_passages_per_section=navigation_max_passages_per_section,
    )
    judge_call = build_unity_judge_llm_call(max_tokens=400) if judge else None
    return QasperAnswerEvaluationService(
        assets,
        FilesystemQasperArtifacts(),
        query_embedder=ProviderQueryEmbedder.from_env(vector_size=dimension),
        query_vector_store=FilesystemQueryVectorStore(query_vector_path),
        answer_model=answer_model,
        judge_call=judge_call,
        retrieval_policy=retrieval_policy,
        field_index_factory=field_index_factory if field_enabled else None,
        reranker=lightweight,
        field_reranker=lightweight,
        retrieval_profile=QasperAnswerRetrievalProfile(
            name=profile_name,
            execution_profile="fixture",
            **{
                name: value
                for name, value in (
                    ("max_context_items", max_context_items),
                    ("max_context_tokens", max_context_tokens),
                )
                if value is not None
            },
            evidence_pool_size=evidence_pool_size,
            field_index_enabled=field_enabled,
            reranker_enabled=lightweight is not None,
            field_reranker_enabled=lightweight is not None,
        ),
        evidence_selector=evidence_selector,
        section_navigator=section_navigator,
        section_navigation=navigation_config,
        context_runtime=_reader_answer_context_runtime(output_directory),
        progress=progress,
        case_started=case_started,
        recovery_source=recovery_source,
        # Re-running the same command with the same output directory resumes it.
        checkpoints=FilesystemQasperRunCheckpoints(),
        answer_model_identity=getattr(answer_model, "measurement_identity", None),
        judge_model_identity=getattr(judge_call, "measurement_identity", None),
        config=QasperAnswerEvaluationConfig(
            output_directory=str(output_directory), seed=seed, per_type=per_type, max_cases=max_cases,
            case_ids=case_ids,
        ),
    )


def _qasper_evidence_selector(
    model_factory,
    *,
    pool_size: int | None,
    min_passages: int | None,
    min_relevance: int | None,
    render_mode: str | None,
):
    """Evidence selector for the run, or None when evidence selection is not requested."""
    tuning = {
        name: value
        for name, value in (
            ("min_passages", min_passages),
            ("min_relevance", min_relevance),
            ("render_mode", render_mode),
        )
        if value is not None
    }
    if pool_size is None:
        if tuning:
            raise ValueError("evidence selection options require evidence_pool_size")
        return None
    from backend.research.rag.retrieval.answer_evidence_selection import RelevanceEvidenceSelector
    from backend.research.rag.retrieval.paper_evidence_summarizer import LLMEvidenceSummarizer

    return RelevanceEvidenceSelector(
        LLMEvidenceSummarizer(model_factory()),
        pool_size=pool_size,
        **tuning,
    )


def _qasper_section_navigation(
    model_factory,
    *,
    enabled: bool,
    max_sections: int | None,
    max_passages_per_section: int | None,
):
    """Section navigator and its config for the run, or (None, None) when navigation is off."""
    tuning = {
        name: value
        for name, value in (
            ("max_sections", max_sections),
            ("max_passages_per_section", max_passages_per_section),
        )
        if value is not None
    }
    if not enabled:
        if tuning:
            raise ValueError("section navigation options require section_navigation")
        return None, None
    from backend.research.rag.retrieval.expanders.section_navigation import SectionNavigationConfig
    from backend.research.rag.retrieval.paper_section_navigator import (
        DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
        LLMSectionNavigator,
    )

    navigator = LLMSectionNavigator(model_factory(max_tokens=DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS))
    return navigator, SectionNavigationConfig(**tuning)


def _reader_answer_context_runtime(output_directory: Path):
    """Assemble context exactly as the desktop reader Q&A composition does.

    Snapshots go beside, never inside, the immutable result directory.
    """
    from backend.research.application.paper_rag_session import ContextWindowLimits
    from backend.research.application.qasper_answer_evaluation import AnswerContextRuntime
    from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
    from infrastructure.research.context_runtime import build_research_context_assembler
    from interfaces.composition.research_llm_deployment import resolve_research_llm_runtime
    from interfaces.composition.research_settings import ResearchRuntimeSettings

    # The same deployment-derived window as the desktop reader, not a bare default.
    _deployment, settings = resolve_research_llm_runtime(ResearchRuntimeSettings.from_env())
    llm = settings.llm
    context_root = output_directory.parent / f"{output_directory.name}.context"
    snapshots = FilesystemHarnessArtifactPort(context_root)
    events = _durable_context_event_port(context_root)

    def assembler_factory(_spec):
        return build_research_context_assembler(
            artifact_port=snapshots,
            event_port=events,
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


def _durable_context_event_port(context_root: Path):
    """Open the durable Harness event log owned by one answer output."""
    from framework.events.canonical import checksum_for
    from framework.events.projection import GraphEventContext, GraphEventExecutionVersion
    from framework.events.runtime.publisher import EventRuntime
    from framework.events.schema import default_event_schema_catalog
    from framework.harness.control_plane.durable_events import (
        DurableHarnessEventPort,
        HarnessEventCanonicalAdapter,
    )
    from framework.shared.graph_identity import GraphRunIdentity, GraphStageIdentity
    from infrastructure.storage.events import SQLiteEventStore

    graph_id = "research.qasper-answer-context"
    graph_version = "1"
    graph_ref = f"{graph_id}@{graph_version}"
    graph_schema_version = "newsroom.normalized-harness-graph/v3"
    compiler_version = "qasper-answer-context-v1"
    graph_checksum = checksum_for({
        "graph_id": graph_id,
        "graph_version": graph_version,
        "stages": ("context-compaction",),
    })

    class QasperContextEventPort(DurableHarnessEventPort):
        def graph_context_for_event(self, event):
            node_id = event.node_id or "context-compaction"
            identity = GraphRunIdentity(
                run_id=event.run_id,
                graph_id=graph_id,
                graph_version=graph_version,
                graph_ref=graph_ref,
                graph_checksum=graph_checksum,
            )
            return GraphEventContext(
                identity=identity,
                execution_version=GraphEventExecutionVersion(
                    graph_schema_version=graph_schema_version,
                    compiler_version=compiler_version,
                    normalized_graph_checksum=graph_checksum,
                ),
                stage_identity=GraphStageIdentity(
                    run_id=event.run_id,
                    graph_id=graph_id,
                    graph_version=graph_version,
                    graph_ref=graph_ref,
                    graph_checksum=graph_checksum,
                    node_id=node_id,
                    node_instance_id=f"{node_id}:1",
                ),
            )

    store = SQLiteEventStore(context_root / "events.sqlite3")
    runtime = EventRuntime(store=store, schema_catalog=default_event_schema_catalog())
    return QasperContextEventPort(
        runtime,
        reader=store,
        adapter=HarnessEventCanonicalAdapter(tenant_id="qasper-answer-evaluation"),
    )


def merge_qasper_answer_runs(
    *,
    base_directory: Path,
    rerun_directory: Path,
    output_directory: Path,
) -> dict[str, Any]:
    """Application-owned merge through the filesystem composition boundary."""
    from backend.research.application.qasper_answer_merge import merge_answer_runs

    for name, value in (
        ("base_directory", base_directory),
        ("rerun_directory", rerun_directory),
        ("output_directory", output_directory),
    ):
        if not value.is_absolute():
            raise ValueError(f"{name} must be absolute")
    return merge_answer_runs(
        FilesystemQasperArtifacts(),
        base_directory=str(base_directory),
        rerun_directory=str(rerun_directory),
        output_directory=str(output_directory),
    )


def build_qasper_answer_rejudge_service(
    *,
    source_directory: Path,
    output_directory: Path,
    dataset_path: Path | None = None,
    cache_path: Path | None = None,
    include_missing_judge: bool = False,
    legacy_judge_model: str | None = None,
    progress=None,
):
    """Compose judge-only recovery without answer, embedding, retrieval, or context runtimes."""
    from backend.research.application.llm_client import build_unity_judge_llm_call
    from backend.research.application.qasper_answer_rejudge import (
        QasperAnswerRejudgeConfig,
        QasperAnswerRejudgeService,
    )

    for name, value in (("source_directory", source_directory), ("output_directory", output_directory)):
        if not value.is_absolute():
            raise ValueError(f"{name} must be absolute")
    if (dataset_path is None) != (cache_path is None):
        raise ValueError("dataset_path and cache_path must be supplied together for legacy rejudge")
    assets = None
    if dataset_path is not None and cache_path is not None:
        if not dataset_path.is_absolute() or not cache_path.is_absolute():
            raise ValueError("legacy dataset_path and cache_path must be absolute")
        root = Path(__file__).resolve().parents[2]
        assets = QasperAssetService(
            FilesystemQasperAssets(dataset_path, cache_path, repository_root=root)
        )
    judge_call = build_unity_judge_llm_call(max_tokens=400)
    root = Path(__file__).resolve().parents[2]
    code_identity = QasperAssetService(
        FilesystemQasperAssets(None, None, repository_root=root)
    ).code_identity().model_dump(mode="json")
    legacy_identity = (
        {"model": legacy_judge_model.strip(), "source": "cli_user_confirmation"}
        if legacy_judge_model is not None and legacy_judge_model.strip()
        else None
    )
    return QasperAnswerRejudgeService(
        FilesystemQasperArtifacts(),
        judge_call=judge_call,
        config=QasperAnswerRejudgeConfig(
            source_directory=str(source_directory),
            output_directory=str(output_directory),
            include_missing_judge=include_missing_judge,
        ),
        assets=assets,
        judge_model_identity=getattr(judge_call, "measurement_identity", None),
        legacy_judge_model_identity=legacy_identity,
        code_identity=code_identity,
        progress=progress,
    )


def backfill_qasper_replay_query_vectors(
    *,
    dataset_path: Path,
    cache_path: Path,
    query_vector_path: Path,
    blocked_replay_directory: Path,
) -> dict[str, Any]:
    """Embed the query texts a blocked offline replay reported missing.

    This is the only replay-adjacent path that reads embedding-provider
    configuration; the replay itself stays filesystem-only.
    """
    from backend.research.application.qasper_query_vector_backfill import backfill_replay_query_vectors
    from backend.research.rag.evaluation.qasper_query_vectors import QueryVectorResolver
    from framework.shared.env import load_root_env
    from infrastructure.research.qasper_query_embedding import ProviderQueryEmbedder
    from infrastructure.research.qasper_query_vector_store import FilesystemQueryVectorStore

    for name, value in (("dataset_path", dataset_path), ("cache_path", cache_path),
                        ("query_vector_path", query_vector_path),
                        ("blocked_replay_directory", blocked_replay_directory)):
        if not value.is_absolute():
            raise ValueError(f"{name} must be absolute")
    if not blocked_replay_directory.is_dir():
        raise ValueError("blocked_replay_directory must be an existing directory")
    load_root_env()
    root = Path(__file__).resolve().parents[2]
    assets = QasperAssetService(FilesystemQasperAssets(dataset_path, cache_path, repository_root=root))
    cache = assets.embedding_cache()
    identity = assets.cache_identity()
    resolver = QueryVectorResolver(
        cache,
        embedder=ProviderQueryEmbedder.from_env(vector_size=identity.dimension),
        store=FilesystemQueryVectorStore(query_vector_path),
        model_id=identity.model_id,
    )
    return backfill_replay_query_vectors(
        FilesystemQasperArtifacts(),
        blocked_replay_directory=str(blocked_replay_directory),
        resolver=resolver,
        base_cache_sha256=identity.sha256,
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
