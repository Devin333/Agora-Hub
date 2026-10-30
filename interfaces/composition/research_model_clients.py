"""The model client each Research purpose calls.

With process credentials (desktop, evaluation) every purpose shares the one
configured deployment client. With per-account credentials (hosted) every
purpose gets its own invocation client, which resolves the connection the
signed-in account assigned to that purpose on every call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from framework.llm.clients.config import OpenAICompatibleDeploymentConfig
from framework.llm.clients.endpoint import LLMCredentialMode, llm_credential_mode
from framework.llm.clients.openai_compatible import (
    LLMRetryPolicy,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from framework.llm.models import LLMClient
from interfaces.composition.model_endpoint_clients import EndpointClientPolicy, invocation_client
from interfaces.composition.research_settings import ResearchRAGSettings, ResearchRuntimeSettings
from interfaces.services.model_connections.purposes import ModelPurpose


@dataclass(frozen=True, slots=True)
class ResearchModelClients:
    candidate_generation: LLMClient
    answer: LLMClient
    evidence_summary: LLMClient
    section_navigation: LLMClient

    def for_purpose(self, purpose: ModelPurpose) -> LLMClient:
        return getattr(self, ModelPurpose(purpose).value)

    def close(self) -> None:
        seen: set[int] = set()
        for purpose in ModelPurpose:
            client = self.for_purpose(purpose)
            if id(client) in seen:
                continue
            seen.add(id(client))
            close = getattr(client, "close", None)
            if callable(close):
                close()


def build_research_model_clients(
    settings: ResearchRuntimeSettings,
    deployment: OpenAICompatibleDeploymentConfig | None,
    env: Mapping[str, str] | None = None,
) -> ResearchModelClients:
    llm = settings.llm
    if llm_credential_mode(env) is LLMCredentialMode.INVOCATION:
        policy = EndpointClientPolicy(
            timeout_seconds=llm.timeout_seconds, max_attempts=llm.max_attempts
        )
        return ResearchModelClients(
            **{purpose.value: invocation_client(purpose.value, policy) for purpose in ModelPurpose}
        )
    if deployment is None:
        raise ValueError("process-credential Research needs its configured deployment")
    shared = OpenAICompatibleClient(
        OpenAICompatibleConfig(
            provider=deployment.config.provider,
            base_url=llm.base_url,
            model=llm.model,
            api_key_env=llm.api_key_env,
            timeout_seconds=llm.timeout_seconds,
        ),
        retry_policy=LLMRetryPolicy(max_attempts=llm.max_attempts),
        structured_output_capability=deployment.structured_output_capability,
    )
    return ResearchModelClients(
        candidate_generation=shared,
        answer=shared,
        evidence_summary=shared,
        section_navigation=shared,
    )


def research_operation_purposes(rag: ResearchRAGSettings) -> dict[str, tuple[str, ...]]:
    """Which purposes each model-backed Research operation calls."""
    ask = [ModelPurpose.ANSWER.value]
    if rag.evidence_selection_enabled:
        ask.append(ModelPurpose.EVIDENCE_SUMMARY.value)
    if rag.section_navigation_enabled:
        ask.append(ModelPurpose.SECTION_NAVIGATION.value)
    return {
        "analyze": (ModelPurpose.CANDIDATE_GENERATION.value,),
        "ask": tuple(ask),
    }


__all__ = [
    "ResearchModelClients",
    "build_research_model_clients",
    "research_operation_purposes",
]
