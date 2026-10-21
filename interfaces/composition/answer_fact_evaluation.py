"""Composition for automated answer fact evaluation.

Every model endpoint is explicit configuration (provider, base URL, model, and
the *name* of the environment variable holding its key); keys are resolved only
by the provider client at call time. All model calls go through a file-backed
record/replay layer so a run is reproducible and a replay never reaches a
provider.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from backend.research.application.answer_fact_evaluation import (
    AnswerFactEvaluationConfig,
    AnswerFactEvaluator,
)
from backend.research.application.qasper_answer_evaluation import (
    QasperAnswerEvaluationConfig,
    QasperAnswerEvaluationService,
)
from backend.research.application.qasper_assets import QasperAssetService
from backend.research.application.reportbench_grounding import ReportBenchGroundingEvaluator
from backend.research.rag.evaluation.protocol_judge_calibration import JudgeCalibrationGate
from backend.research.rag.evaluation.qasper_experiments import SweepParameters
from backend.research.rag.evaluation.qasper_reference import QasperGoldIdentity
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from framework.llm.clients.openai_compatible import (
    LLMRetryPolicy,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from framework.llm.models import LLMClient, LLMRequest
from infrastructure.research.llm_fact_extractor import LLMAnswerFactExtractor
from infrastructure.research.llm_fact_judge import LLMFactEntailmentJudge
from infrastructure.research.llm_replay_client import FilesystemLLMReplayClient, ReplayMode
from infrastructure.research.qasper_evaluation import FilesystemQasperArtifacts, FilesystemQasperAssets


@dataclass(frozen=True)
class ModelEndpoint:
    provider: str
    base_url: str
    model: str
    api_key_env: str
    model_revision: str | None = None
    timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        for name in ("provider", "base_url", "model", "api_key_env"):
            if not getattr(self, name).strip():
                raise ValueError(f"model endpoint {name} must not be empty")
        if not self.base_url.startswith("https://"):
            raise ValueError("model endpoint base_url must use HTTPS")
        if self.timeout_seconds <= 0:
            raise ValueError("model endpoint timeout must be positive")


@dataclass(frozen=True)
class ModelPanelSettings:
    """The extraction model, the judge panel, and their record/replay store."""

    replay_directory: Path
    replay_mode: ReplayMode
    extractor_model: ModelEndpoint
    judge_models: tuple[ModelEndpoint, ...] = ()
    max_concurrency: int = 4

    def __post_init__(self) -> None:
        if not self.replay_directory.is_absolute():
            raise ValueError("replay_directory must be an absolute path")
        if len(self.judge_models) == 1:
            raise ValueError("configure at least two judge models from distinct providers, or none")
        providers = [item.provider.casefold() for item in self.judge_models]
        if len(set(providers)) != len(providers):
            raise ValueError("judge models must come from distinct providers")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")


@dataclass(frozen=True)
class AnswerFactEvaluationSettings:
    dataset: Path
    embedding_cache: Path | None
    panel: ModelPanelSettings
    answer_model: ModelEndpoint
    gold_identity: QasperGoldIdentity
    profile: str = "full_hybrid"
    parameters: SweepParameters = field(default_factory=SweepParameters)
    case_ids: tuple[str, ...] = ()
    max_cases: int | None = None
    calibration_gate: JudgeCalibrationGate = field(default_factory=JudgeCalibrationGate)
    run_id: str | None = None

    def __post_init__(self) -> None:
        if not self.dataset.is_absolute():
            raise ValueError("dataset must be an absolute path")
        if self.embedding_cache is not None and not self.embedding_cache.is_absolute():
            raise ValueError("embedding_cache must be an absolute path")


def _client(endpoint: ModelEndpoint, panel: ModelPanelSettings) -> LLMClient:
    provider_client = None
    if panel.replay_mode == "record":
        provider_client = OpenAICompatibleClient(
            OpenAICompatibleConfig(
                provider=endpoint.provider,
                base_url=endpoint.base_url,
                model=endpoint.model,
                api_key_env=endpoint.api_key_env,
                timeout_seconds=endpoint.timeout_seconds,
            ),
            retry_policy=LLMRetryPolicy(max_attempts=3, retry_delay_seconds=(2.0, 5.0)),
        )
    return FilesystemLLMReplayClient(
        provider_client,
        provider=endpoint.provider,
        model=endpoint.model,
        directory=panel.replay_directory,
        mode=panel.replay_mode,
    )


def _answer_call(client: LLMClient, endpoint: ModelEndpoint):
    """The production generator's ``llm_call`` contract over an injected client."""

    async def llm_call(prompt: str, **kwargs: object) -> str:
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            model=endpoint.model,
            temperature=0,
            max_tokens=600,
            timeout_seconds=endpoint.timeout_seconds,
        )
        response = await asyncio.to_thread(client.complete, request)
        return response.content or ""

    return llm_call


def _extractor(panel: ModelPanelSettings) -> LLMAnswerFactExtractor:
    endpoint = panel.extractor_model
    return LLMAnswerFactExtractor(
        _client(endpoint, panel),
        provider=endpoint.provider,
        model_id=endpoint.model,
        model_revision=endpoint.model_revision,
        timeout_seconds=endpoint.timeout_seconds,
    )


def _judges(panel: ModelPanelSettings) -> tuple[LLMFactEntailmentJudge, ...]:
    return tuple(
        LLMFactEntailmentJudge(
            _client(endpoint, panel),
            provider=endpoint.provider,
            model_id=endpoint.model,
            model_revision=endpoint.model_revision,
            timeout_seconds=endpoint.timeout_seconds,
        )
        for endpoint in panel.judge_models
    )


def compose_qasper_answer_evaluation(settings: AnswerFactEvaluationSettings) -> QasperAnswerEvaluationService:
    root = Path(__file__).resolve().parents[2]
    assets = QasperAssetService(FilesystemQasperAssets(
        settings.dataset, settings.embedding_cache, repository_root=root,
    ))
    panel = settings.panel
    extractor = _extractor(panel)
    evaluator = AnswerFactEvaluator(
        decomposer=extractor,
        extractor=extractor,
        judges=_judges(panel),
        config=AnswerFactEvaluationConfig(
            max_concurrency=panel.max_concurrency,
            calibration_gate=settings.calibration_gate,
        ),
    )
    generator = AnswerGenerator(_answer_call(_client(settings.answer_model, panel), settings.answer_model))
    return QasperAnswerEvaluationService(
        assets=assets,
        artifacts=FilesystemQasperArtifacts(),
        generator=generator,
        evaluator=evaluator,
        config=QasperAnswerEvaluationConfig(
            gold_identity=settings.gold_identity,
            profile=settings.profile,
            parameters=settings.parameters,
            case_ids=settings.case_ids,
            max_cases=settings.max_cases,
            run_id=settings.run_id,
        ),
    )


def compose_reportbench_grounding(panel: ModelPanelSettings) -> ReportBenchGroundingEvaluator:
    extractor = _extractor(panel)
    return ReportBenchGroundingEvaluator(
        decomposer=extractor,
        extractor=extractor,
        judges=_judges(panel),
        max_concurrency=panel.max_concurrency,
    )


def publish_run(directory: Path, objects: dict[str, bytes]) -> None:
    if not directory.is_absolute():
        raise ValueError("output directory must be absolute")
    FilesystemQasperArtifacts().publish(str(directory), objects)


__all__ = [
    "AnswerFactEvaluationSettings",
    "ModelEndpoint",
    "ModelPanelSettings",
    "compose_qasper_answer_evaluation",
    "compose_reportbench_grounding",
    "publish_run",
]
