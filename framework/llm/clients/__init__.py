from __future__ import annotations

from framework.llm.models import LLMClient
from framework.llm.clients.config import (
    DEFAULT_MODEL_ROUTE_ID,
    DEFAULT_MODELS_CONFIG_PATH,
    OpenAICompatibleDeploymentConfig,
    build_openai_compatible_client_from_config,
    load_openai_compatible_deployment,
    validate_openai_compatible_models_config,
)
from framework.llm.clients.endpoint import (
    CREDENTIAL_MODE_ENV,
    LLMCredentialMode,
    LLMCredentialUnavailableError,
    LLMEndpoint,
    LLMEndpointSource,
    bind_llm_endpoint_source,
    credential_unavailable_cause,
    llm_credential_mode,
    require_invocation_credential,
    resolve_invocation_endpoint,
    validate_endpoint_base_url,
)
from framework.llm.clients.fake import FakeLLMClient
from framework.llm.clients.openai_compatible import (
    LLMConfigurationError,
    LLMProviderContextOverflow,
    LLMProviderError,
    LLMRetryPolicy,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from framework.llm.clients.tool_adapters import (
    LLMToolCallParseError,
    LLMToolSchemaError,
    openai_tool_name_map,
    parse_openai_tool_calls,
    to_openai_tools,
)

__all__ = [
    "CREDENTIAL_MODE_ENV",
    "DEFAULT_MODEL_ROUTE_ID",
    "DEFAULT_MODELS_CONFIG_PATH",
    "FakeLLMClient",
    "LLMClient",
    "LLMConfigurationError",
    "LLMCredentialMode",
    "LLMCredentialUnavailableError",
    "LLMEndpoint",
    "LLMEndpointSource",
    "LLMProviderContextOverflow",
    "LLMProviderError",
    "LLMRetryPolicy",
    "LLMToolCallParseError",
    "LLMToolSchemaError",
    "OpenAICompatibleClient",
    "OpenAICompatibleConfig",
    "OpenAICompatibleDeploymentConfig",
    "bind_llm_endpoint_source",
    "credential_unavailable_cause",
    "build_openai_compatible_client_from_config",
    "llm_credential_mode",
    "require_invocation_credential",
    "load_openai_compatible_deployment",
    "openai_tool_name_map",
    "parse_openai_tool_calls",
    "resolve_invocation_endpoint",
    "to_openai_tools",
    "validate_endpoint_base_url",
    "validate_openai_compatible_models_config",
]
