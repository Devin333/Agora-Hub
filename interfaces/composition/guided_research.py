from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

from backend.research.application.guided_research import GuidedResearchApplication
from framework.llm.clients.config import load_openai_compatible_deployment
from framework.llm.clients.openai_compatible import LLMRetryPolicy, OpenAICompatibleClient
from infrastructure.research.guided_intent_candidate import StructuredGuidedIntentCandidateWorker
from infrastructure.research.github_guided_search import GithubGuidedSearch
from infrastructure.external.sources.github import GithubConnector
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy
from infrastructure.research.public_paper_cache import (
    PublicPaperCacheGuidedSearch,
    PublicPaperCacheRepository,
)
from interfaces.composition.research import build_research_application_service
from interfaces.services.guided_research_service import (
    GuidedResearchService,
    ProjectGuidedSearch,
)
from interfaces.services.project_service import ProjectApplicationService
from interfaces.services.research_history_service import ResearchHistoryService


@lru_cache(maxsize=1)
def build_guided_research_service() -> GuidedResearchService:
    deployment = load_openai_compatible_deployment()
    client = OpenAICompatibleClient(
        replace(deployment.config, timeout_seconds=min(deployment.config.timeout_seconds, 45.0)),
        retry_policy=LLMRetryPolicy(max_attempts=1),
        structured_output_capability=deployment.structured_output_capability,
    )
    research = build_research_application_service()
    projects = ProjectApplicationService()

    application = GuidedResearchApplication(
        intent_candidate=StructuredGuidedIntentCandidateWorker(
            client,
            managed_output=bool(
                deployment.structured_output_capability
                and deployment.structured_output_capability.mode != "none"
            ),
        ),
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
