"""Resolve the Research answer deployment and the context window it implies.

Every composition that sends Research context to the answer model (desktop
reader, Research runs, QASPER evaluation) resolves its settings here, so the
window the context gate admits against is the selected deployment's window.

With per-account credentials there is no operator deployment: each account's
connections serve the calls, and the configured input budget is the window
every assigned connection must hold.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from framework.llm.clients.endpoint import LLMCredentialMode, llm_credential_mode
from framework.llm.clients.config import (
    LLMConfigurationError,
    OpenAICompatibleDeploymentConfig,
    load_openai_compatible_deployment,
)
from interfaces.composition.research_errors import (
    ResearchCapability,
    ResearchConfigurationError,
)
from interfaces.composition.research_settings import ResearchRuntimeSettings


def resolve_research_llm_deployment(
    settings: ResearchRuntimeSettings,
) -> OpenAICompatibleDeploymentConfig:
    """Load the configured route and require it to match the settings' model and endpoint."""
    try:
        deployment = load_openai_compatible_deployment(
            settings.llm.models_config_path,
            route_id=settings.llm.route_id,
            apply_environment_overrides=False,
        )
    except LLMConfigurationError as exc:
        raise ResearchConfigurationError((ResearchCapability.LLM_ROUTE,)) from exc
    if deployment.config.model != settings.llm.model:
        raise ResearchConfigurationError((ResearchCapability.LLM_MODEL,))
    if deployment.config.base_url.rstrip("/") != settings.llm.base_url.rstrip("/"):
        raise ResearchConfigurationError((ResearchCapability.LLM_BASE_URL,))
    return deployment


def resolve_research_llm_runtime(
    settings: ResearchRuntimeSettings,
    env: Mapping[str, str] | None = None,
) -> tuple[OpenAICompatibleDeploymentConfig | None, ResearchRuntimeSettings]:
    """Return the deployment and the settings with a concrete, admissible context window.

    The RAG evidence budget must fit the resolved input window: the context gate
    measures against that window, so a larger budget would otherwise only surface
    per question as a rejected provider dispatch. With per-account credentials
    the deployment is ``None`` and the input window must be configured.
    """
    if llm_credential_mode(env) is LLMCredentialMode.INVOCATION:
        if settings.llm.max_input_tokens is None:
            raise ResearchConfigurationError((ResearchCapability.LLM_MAX_INPUT_TOKENS,))
        if settings.rag.max_context_tokens > settings.llm.max_input_tokens:
            raise ResearchConfigurationError((ResearchCapability.RAG_MAX_CONTEXT_TOKENS,))
        return None, settings
    deployment = resolve_research_llm_deployment(settings)
    llm = settings.llm.with_deployment_window(deployment.capabilities.context_window_tokens)
    if settings.rag.max_context_tokens > llm.max_input_tokens:
        raise ResearchConfigurationError((ResearchCapability.RAG_MAX_CONTEXT_TOKENS,))
    return deployment, replace(settings, llm=llm)


__all__ = ["resolve_research_llm_deployment", "resolve_research_llm_runtime"]
