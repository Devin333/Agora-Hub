from __future__ import annotations

from enum import Enum
from typing import Any, Iterable


class ResearchCapability(str, Enum):
    SOURCE = "research.source"
    SOURCE_PROVIDER = "research.source.provider"
    SOURCE_API_URL = "research.source.api_url"
    SOURCE_CACHE_SIZE = "research.source.cache_size"
    SOURCE_TIMEOUT = "research.source.timeout"
    SOURCE_METADATA_MAX_BYTES = "research.source.metadata_max_bytes"
    SOURCE_PACKAGE_MAX_BYTES = "research.source.package_max_bytes"
    LLM = "research.llm"
    LLM_ROUTE = "research.llm.route"
    LLM_PROVIDER = "research.llm.provider"
    LLM_BASE_URL = "research.llm.base_url"
    LLM_MODEL = "research.llm.model"
    LLM_CREDENTIAL = "research.llm.credential"
    LLM_TIMEOUT = "research.llm.timeout"
    LLM_MAX_ATTEMPTS = "research.llm.max_attempts"
    LLM_MAX_INPUT_TOKENS = "research.llm.max_input_tokens"
    LLM_MAX_OUTPUT_TOKENS = "research.llm.max_output_tokens"
    PARSER = "research.parser"
    PARSER_BACKENDS = "research.parser.backends"
    PARSER_ABSTRACT_FALLBACK = "research.parser.abstract_fallback"
    PARSER_TIMEOUT = "research.parser.timeout"
    PARSER_MAX_DOCUMENT_BYTES = "research.parser.max_document_bytes"
    RAG = "research.rag"
    RAG_BACKEND = "research.rag.backend"
    RAG_LOCAL_ROOT = "research.rag.local_root"
    RAG_VECTOR_BACKEND = "research.rag.vector_backend"
    RAG_COLLECTION = "research.rag.collection"
    RAG_VECTOR_SIZE = "research.rag.vector_size"
    RAG_MAX_ROUNDS = "research.rag.max_rounds"
    RAG_MAX_REPLANS = "research.rag.max_replans"
    RAG_MAX_QUERIES = "research.rag.max_queries"
    RAG_MAX_SOURCE_READS = "research.rag.max_source_reads"
    RAG_MAX_MEMORY_HITS = "research.rag.max_memory_hits"
    RAG_MEMORY = "research.rag.memory"
    RAG_MEMORY_NAMESPACE_REFS = "research.rag.memory_namespace_refs"
    RAG_MAX_CONTEXT_ITEMS = "research.rag.max_context_items"
    RAG_MAX_CONTEXT_TOKENS = "research.rag.max_context_tokens"
    RAG_MAX_WORKER_CALLS = "research.rag.max_worker_calls"
    STORAGE_ROOT = "research.storage.root"
    ARTIFACT = "research.storage.artifact"
    ARTIFACT_ROOT = "research.storage.artifact_root"
    ARTIFACT_MAX_BYTES = "research.storage.artifact_max_bytes"
    RUN_STORE = "research.storage.run_store"
    RUN_STORE_ROOT = "research.storage.run_store_root"
    RUN_RECORD_MAX_BYTES = "research.storage.run_record_max_bytes"
    GRAPH_ARTIFACT_PERSISTENCE = "research.graph_artifact_persistence"
    EVENT_LOG = "research.event_log"
    DOCUMENT_COMPILER = "research.document_compiler"
    CANDIDATE_WORKER = "research.candidate_worker"
    GITHUB_REPOSITORY = "research.github_repository"


class ResearchRemediation(str, Enum):
    REVIEW_RUNTIME_CONFIGURATION = "review_research_runtime_configuration"
    CONFIGURE_LLM_CREDENTIAL = "configure_research_llm_credential"
    RESTORE_REQUIRED_CAPABILITY = "restore_research_runtime_capability"

    @property
    def message(self) -> str:
        return _REMEDIATION_MESSAGES[self]


_REMEDIATION_MESSAGES = {
    ResearchRemediation.REVIEW_RUNTIME_CONFIGURATION: (
        "Review the named Research capability settings and use documented supported values."
    ),
    ResearchRemediation.CONFIGURE_LLM_CREDENTIAL: (
        "Provide the configured Research LLM credential through deployment secret management."
    ),
    ResearchRemediation.RESTORE_REQUIRED_CAPABILITY: (
        "Restore the named Research capability and retry the request."
    ),
}


class ResearchCompositionError(RuntimeError):
    """Sanitized base error for production Research composition failures."""

    error_code = "research_composition_failed"
    message_prefix = "Research runtime composition failed for capabilities"

    def __init__(
        self,
        capabilities: Iterable[str | ResearchCapability],
        *,
        remediation: ResearchRemediation = ResearchRemediation.REVIEW_RUNTIME_CONFIGURATION,
        retryable: bool = False,
    ) -> None:
        normalized = _normalize_capabilities(capabilities)
        if not isinstance(remediation, ResearchRemediation):
            raise TypeError("remediation must be ResearchRemediation")
        self.capabilities = normalized
        self.remediation_code = remediation.value
        self.remediation = remediation.message
        self.retryable = bool(retryable)
        self.message = (
            f"{self.message_prefix}: {', '.join(normalized)}. "
            f"{self.remediation}"
        )
        super().__init__(self.message)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "code": self.error_code,
            "message": self.message,
            "capabilities": list(self.capabilities),
            "remediation": {
                "code": self.remediation_code,
                "message": self.remediation,
            },
            "retryable": self.retryable,
        }


class ResearchConfigurationError(ResearchCompositionError):
    """Raised when a named Research setting violates its public contract."""

    error_code = "research_configuration_invalid"
    message_prefix = "Research runtime configuration is invalid for capabilities"

    def __init__(self, capabilities: Iterable[str | ResearchCapability]) -> None:
        super().__init__(
            capabilities,
            remediation=ResearchRemediation.REVIEW_RUNTIME_CONFIGURATION,
            retryable=False,
        )


class ResearchRuntimeUnavailableError(ResearchCompositionError):
    """Raised when valid composition requires a currently absent capability."""

    error_code = "research_runtime_unavailable"
    message_prefix = "Research runtime is unavailable for capabilities"

    def __init__(
        self,
        capabilities: Iterable[str | ResearchCapability],
        *,
        remediation: ResearchRemediation = ResearchRemediation.RESTORE_REQUIRED_CAPABILITY,
        retryable: bool = False,
    ) -> None:
        super().__init__(
            capabilities,
            remediation=remediation,
            retryable=retryable,
        )


def _normalize_capabilities(
    capabilities: Iterable[str | ResearchCapability],
) -> tuple[str, ...]:
    if isinstance(capabilities, (str, bytes)):
        raise ValueError("capability names must be provided as a collection")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in capabilities:
        try:
            capability = (
                value.value
                if isinstance(value, ResearchCapability)
                else ResearchCapability(str(value).strip().lower()).value
            )
        except ValueError:
            raise ValueError("capability names must be registered identifiers") from None
        if capability not in seen:
            seen.add(capability)
            normalized.append(capability)
    if not normalized:
        raise ValueError("at least one capability name is required")
    return tuple(normalized)


__all__ = [
    "ResearchCapability",
    "ResearchCompositionError",
    "ResearchConfigurationError",
    "ResearchRemediation",
    "ResearchRuntimeUnavailableError",
]
