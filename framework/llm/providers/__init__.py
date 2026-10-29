"""Model vendors: catalog, model discovery, connection probe and client construction."""
from __future__ import annotations

from framework.llm.providers.catalog import (
    PROVIDERS,
    SELECTABLE_REASONING_PARAMETERS,
    AnthropicThinkingOff,
    KnownModel,
    ProviderKind,
    ProviderSpec,
    WireProtocol,
    provider_spec,
)
from framework.llm.providers.discovery import (
    DiscoveredModel,
    ModelDiscovery,
    ModelProfile,
    describe_model,
)
from framework.llm.providers.factory import ProviderClientSettings, build_provider_client
from framework.llm.providers.probe import ProbeOutcome, probe_model

__all__ = [
    "PROVIDERS",
    "SELECTABLE_REASONING_PARAMETERS",
    "AnthropicThinkingOff",
    "DiscoveredModel",
    "KnownModel",
    "ModelDiscovery",
    "ModelProfile",
    "ProbeOutcome",
    "ProviderClientSettings",
    "ProviderKind",
    "ProviderSpec",
    "WireProtocol",
    "build_provider_client",
    "describe_model",
    "probe_model",
    "provider_spec",
]
