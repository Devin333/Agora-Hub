from __future__ import annotations

from typing import TYPE_CHECKING, Any

from framework.shared.lazy_exports import LazyExports

__all__ = [
    "LLMCacheConfigurationError",
    "LLMCacheSettings",
    "ResearchArtifactSettings",
    "ResearchCapability",
    "ResearchCompositionError",
    "ResearchConfigurationError",
    "ResearchGraphArtifactRuntimeComponents",
    "ResearchLLMSettings",
    "ResearchParserSettings",
    "ResearchRAGSettings",
    "ResearchRemediation",
    "ResearchRuntimeComposition",
    "ResearchRuntimeProvider",
    "ResearchRunStoreSettings",
    "ResearchRuntimeSettings",
    "ResearchRuntimeUnavailableError",
    "ResearchSourceSettings",
    "build_research_application_service",
    "build_source_runtime_provider",
    "build_research_execution_composition",
    "build_process_execution_composition",
    "PRODUCTION_RUNTIME_COMPOSITION_ID",
    "PRODUCTION_RUNTIME_COMPOSITION_VERSION",
    "RUNTIME_TRUSTED_PROFILE_ID",
    "build_research_graph_artifact_governance_service",
    "build_research_runtime_composition",
    "build_cache_aware_llm_router",
    "build_llm_cache_runtime",
    "build_llm_cache_runtime_from_env",
    "close_default_research_runtime",
    "default_source_runtime_provider",
    "compose_research_graph_artifact_runtime",
    "default_research_runtime_provider",
    "reset_default_research_runtime",
]

_LAZY = LazyExports(
    __name__,
    {
        "ResearchCapability": ("interfaces.composition.research_errors", "ResearchCapability"),
        "ResearchCompositionError": ("interfaces.composition.research_errors", "ResearchCompositionError"),
        "ResearchConfigurationError": ("interfaces.composition.research_errors", "ResearchConfigurationError"),
        "ResearchRemediation": ("interfaces.composition.research_errors", "ResearchRemediation"),
        "ResearchRuntimeUnavailableError": ("interfaces.composition.research_errors", "ResearchRuntimeUnavailableError"),
        "LLMCacheConfigurationError": ("interfaces.composition.llm_cache", "LLMCacheConfigurationError"),
        "LLMCacheSettings": ("interfaces.composition.llm_cache", "LLMCacheSettings"),
        "build_cache_aware_llm_router": ("interfaces.composition.llm_cache", "build_cache_aware_llm_router"),
        "build_llm_cache_runtime": ("interfaces.composition.llm_cache", "build_llm_cache_runtime"),
        "build_llm_cache_runtime_from_env": ("interfaces.composition.llm_cache", "build_llm_cache_runtime_from_env"),
        "ResearchRuntimeComposition": ("interfaces.composition.research", "ResearchRuntimeComposition"),
        "ResearchRuntimeProvider": ("interfaces.composition.research", "ResearchRuntimeProvider"),
        "build_research_application_service": ("interfaces.composition.research", "build_research_application_service"),
        "build_research_runtime_composition": ("interfaces.composition.research", "build_research_runtime_composition"),
        "close_default_research_runtime": ("interfaces.composition.research", "close_default_research_runtime"),
        "default_source_runtime_provider": ("interfaces.composition.research", "default_source_runtime_provider"),
        "default_research_runtime_provider": ("interfaces.composition.research", "default_research_runtime_provider"),
        "reset_default_research_runtime": ("interfaces.composition.research", "reset_default_research_runtime"),
        "ResearchArtifactSettings": ("interfaces.composition.research_settings", "ResearchArtifactSettings"),
        "ResearchLLMSettings": ("interfaces.composition.research_settings", "ResearchLLMSettings"),
        "ResearchParserSettings": ("interfaces.composition.research_settings", "ResearchParserSettings"),
        "ResearchRAGSettings": ("interfaces.composition.research_settings", "ResearchRAGSettings"),
        "ResearchRunStoreSettings": ("interfaces.composition.research_settings", "ResearchRunStoreSettings"),
        "ResearchRuntimeSettings": ("interfaces.composition.research_settings", "ResearchRuntimeSettings"),
        "ResearchSourceSettings": ("interfaces.composition.research_settings", "ResearchSourceSettings"),
        "ResearchGraphArtifactRuntimeComponents": ("interfaces.composition.research_graph_artifacts", "ResearchGraphArtifactRuntimeComponents"),
        "build_research_graph_artifact_governance_service": ("interfaces.composition.research_graph_artifacts", "build_research_graph_artifact_governance_service"),
        "compose_research_graph_artifact_runtime": ("interfaces.composition.research_graph_artifacts", "compose_research_graph_artifact_runtime"),
        "build_source_runtime_provider": ("interfaces.composition.source", "build_source_runtime_provider"),
        "PRODUCTION_RUNTIME_COMPOSITION_ID": ("interfaces.composition.runtime_execution", "PRODUCTION_RUNTIME_COMPOSITION_ID"),
        "PRODUCTION_RUNTIME_COMPOSITION_VERSION": ("interfaces.composition.runtime_execution", "PRODUCTION_RUNTIME_COMPOSITION_VERSION"),
        "RUNTIME_TRUSTED_PROFILE_ID": ("interfaces.composition.runtime_execution", "RUNTIME_TRUSTED_PROFILE_ID"),
        "build_research_execution_composition": ("interfaces.composition.runtime_execution", "build_research_execution_composition"),
        "build_process_execution_composition": ("interfaces.composition.runtime_execution", "build_process_execution_composition"),
    },
)


def __getattr__(name: str) -> Any:
    return _LAZY.getattr(name)


def __dir__() -> list[str]:
    return _LAZY.dir()


if TYPE_CHECKING:
    from interfaces.composition.research_errors import (
        ResearchCapability,
        ResearchCompositionError,
        ResearchConfigurationError,
        ResearchRemediation,
        ResearchRuntimeUnavailableError,
    )
    from interfaces.composition.llm_cache import (
        LLMCacheConfigurationError,
        LLMCacheSettings,
        build_cache_aware_llm_router,
        build_llm_cache_runtime,
        build_llm_cache_runtime_from_env,
    )
    from interfaces.composition.research import (
        ResearchRuntimeComposition,
        ResearchRuntimeProvider,
        build_research_application_service,
        build_research_runtime_composition,
        close_default_research_runtime,
        default_source_runtime_provider,
        default_research_runtime_provider,
        reset_default_research_runtime,
    )
    from interfaces.composition.research_settings import (
        ResearchArtifactSettings,
        ResearchLLMSettings,
        ResearchParserSettings,
        ResearchRAGSettings,
        ResearchRunStoreSettings,
        ResearchRuntimeSettings,
        ResearchSourceSettings,
    )
    from interfaces.composition.research_graph_artifacts import (
        ResearchGraphArtifactRuntimeComponents,
        build_research_graph_artifact_governance_service,
        compose_research_graph_artifact_runtime,
    )
    from interfaces.composition.source import (
        build_source_runtime_provider,
    )
    from interfaces.composition.runtime_execution import (
        PRODUCTION_RUNTIME_COMPOSITION_ID,
        PRODUCTION_RUNTIME_COMPOSITION_VERSION,
        RUNTIME_TRUSTED_PROFILE_ID,
        build_research_execution_composition,
        build_process_execution_composition,
    )
