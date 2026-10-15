from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
from typing import Any

from backend.research.application.guided_research import GuidedResearchApplication
from backend.research.ports.guided_research import GuidedIntentCandidatePort, GuidedResearchError
from framework.llm.clients.config import load_openai_compatible_deployment
from framework.llm.clients.openai_compatible import LLMConfigurationError, LLMRetryPolicy, OpenAICompatibleClient
from infrastructure.research.guided_intent_candidate import StructuredGuidedIntentCandidateWorker
from infrastructure.research.github_guided_search import GithubGuidedSearch
from infrastructure.external.sources.github import GithubConnector
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy
from infrastructure.research.public_paper_cache import (
    PublicPaperCacheGuidedSearch,
    PublicPaperCacheRepository,
)
from interfaces.composition.research import build_research_application_service
from interfaces.composition.research_errors import ResearchCompositionError, ResearchConfigurationError
from interfaces.composition.research_settings import (
    DESKTOP_RESEARCH_LLM_ENABLED_ENV,
    ResearchRuntimeSettings,
    desktop_research_llm_enabled,
)
from interfaces.services.guided_research_service import (
    GuidedResearchService,
    ProjectGuidedSearch,
)
from interfaces.services.project_service import ProjectApplicationService
from interfaces.services.research_history_service import ResearchHistoryService


class _UnavailableGuidedIntentCandidate:
    """Keep deterministic searches independent of unavailable model admission."""

    def __init__(self, error: ResearchCompositionError) -> None:
        self._error = error

    def generate_candidate(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise GuidedResearchError(
            "guided_intent_unavailable",
            "请先检查并配置研究模型，再使用问题理解。",
            status_code=503,
            retryable=False,
            user_action_required=True,
            details=self._error.to_public_dict(),
        )


def _build_intent_candidate() -> GuidedIntentCandidatePort:
    if os.environ.get(DESKTOP_RESEARCH_LLM_ENABLED_ENV) is None:
        # Unmarked profiles retain the existing shared-model loader contract.
        deployment = load_openai_compatible_deployment()
    else:
        try:
            # Admit the marker and owner path before full settings resolution:
            # that loader may inspect shared defaults and require credentials.
            # An inherited NEWS_MODELS_CONFIG is not a desktop-owned route.
            enabled = desktop_research_llm_enabled()
            if enabled is True and not os.environ.get("NEWS_RESEARCH_MODELS_CONFIG", "").strip():
                raise ResearchConfigurationError(("research.llm.route",))
            settings = ResearchRuntimeSettings.from_env()
            if settings.llm.models_config_path is None:
                raise ResearchConfigurationError(("research.llm.route",))
            deployment = load_openai_compatible_deployment(
                settings.llm.models_config_path,
                route_id=settings.llm.route_id,
                apply_environment_overrides=False,
            )
            bindings = (
                (deployment.route_id, settings.llm.route_id, "research.llm.route"),
                (deployment.config.provider, settings.llm.provider, "research.llm.provider"),
                (deployment.config.model, settings.llm.model, "research.llm.model"),
                (deployment.config.base_url.rstrip("/"), settings.llm.base_url.rstrip("/"), "research.llm.base_url"),
                (deployment.config.api_key_env, settings.llm.api_key_env, "research.llm.credential"),
            )
            mismatches = tuple(capability for actual, expected, capability in bindings if actual != expected)
            if mismatches or not deployment.enabled:
                raise ResearchConfigurationError(mismatches or ("research.llm.route",))
        except ResearchCompositionError as exc:
            return _UnavailableGuidedIntentCandidate(exc)
        except LLMConfigurationError:
            return _UnavailableGuidedIntentCandidate(ResearchConfigurationError(("research.llm.route",)))
    client = OpenAICompatibleClient(
        replace(deployment.config, timeout_seconds=min(deployment.config.timeout_seconds, 45.0)),
        retry_policy=LLMRetryPolicy(max_attempts=1),
        structured_output_capability=deployment.structured_output_capability,
    )
    return StructuredGuidedIntentCandidateWorker(
        client,
        managed_output=bool(
            deployment.structured_output_capability
            and deployment.structured_output_capability.mode != "none"
        ),
    )


@lru_cache(maxsize=1)
def build_guided_research_service() -> GuidedResearchService:
    intent_candidate = _build_intent_candidate()
    research = build_research_application_service()
    projects = ProjectApplicationService()

    application = GuidedResearchApplication(
        intent_candidate=intent_candidate,
        paper_search=PublicPaperCacheGuidedSearch(
            PublicPaperCacheRepository(),
        ),
        project_search=ProjectGuidedSearch(projects, public_search=GithubGuidedSearch(GithubConnector(
            fetch_policy=SourceFetchPolicy(timeout_seconds=12, retry_times=0, max_bytes=1_000_000, rate_limit_per_domain_per_minute=10),
        ))),
    )
    return GuidedResearchService(application=application, history=ResearchHistoryService(), research=research)


def reset_guided_research_service() -> None:
    build_guided_research_service.cache_clear()


__all__ = ["build_guided_research_service", "reset_guided_research_service"]
