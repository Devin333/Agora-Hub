"""What a stored connection means once the catalog is applied.

Catalog connections take their address, window, output limit, reasoning
style and structured-output capability from the operator catalog; custom
connections take them from the record. Both end up as one ``ConnectionSpec``,
which is what window admission checks and what an endpoint is built from.
"""

from __future__ import annotations

from dataclasses import dataclass

from framework.llm.clients import LLMEndpoint, LLMWireProtocol
from framework.llm.models.reasoning import ReasoningEffort, ReasoningSetting, ReasoningWire
from framework.llm.structured_output import ProviderStructuredOutputCapability
from interfaces.services.model_connections.catalog import (
    CatalogProvider,
    CustomProvider,
    ModelProviderCatalog,
)
from interfaces.services.model_connections.errors import (
    ModelConnectionError,
    connection_withdrawn,
    structured_output_required,
    window_too_small,
)
from interfaces.services.model_connections.purposes import ModelPurpose, PurposeRequirement
from interfaces.services.model_connections.records import ModelConnectionRecord


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    provider_id: str
    provider_label: str
    custom: bool
    protocol: LLMWireProtocol
    base_url: str
    model: str
    model_label: str
    context_window_tokens: int
    max_output_tokens: int | None
    reasoning: ReasoningSetting
    structured_output: ProviderStructuredOutputCapability

    @property
    def returns_json_objects(self) -> bool:
        return self.structured_output.mode == "json_object"

    def required_window(self, requirement: PurposeRequirement) -> int:
        return requirement.required_window(self.reasoning, max_output_tokens=self.max_output_tokens)

    def admission_error(self, purpose: ModelPurpose, requirement: PurposeRequirement) -> ModelConnectionError | None:
        """Why this connection may not serve ``purpose``, or ``None`` when it may."""
        if requirement.structured_output and not self.returns_json_objects:
            return structured_output_required(purpose)
        required = self.required_window(requirement)
        if required > self.context_window_tokens:
            return window_too_small(
                purpose, required_tokens=required, context_window_tokens=self.context_window_tokens
            )
        return None

    def endpoint(self, *, api_key: str, connection_ref: str) -> LLMEndpoint:
        return LLMEndpoint(
            base_url=self.base_url,
            model=self.model,
            api_key=api_key,
            provider=self.provider_id,
            protocol=self.protocol,
            structured_output_capability=self.structured_output,
            reasoning=self.reasoning,
            context_window_tokens=self.context_window_tokens,
            max_output_tokens=self.max_output_tokens,
            connection_ref=connection_ref,
        )


def resolve_connection_spec(record: ModelConnectionRecord, catalog: ModelProviderCatalog) -> ConnectionSpec:
    """Apply the catalog to ``record``; raise ``model_connection_withdrawn`` if it no longer can be."""

    provider = catalog.provider(record.provider_id)
    if isinstance(provider, CatalogProvider):
        return _catalog_spec(record, provider)
    if isinstance(provider, CustomProvider):
        return _custom_spec(record, provider)
    raise connection_withdrawn()


def _catalog_spec(record: ModelConnectionRecord, provider: CatalogProvider) -> ConnectionSpec:
    model = provider.model(record.model)
    if model is None:
        raise connection_withdrawn()
    effort = record.reasoning_effort
    if not model.reasoning.offers(effort):
        # The operator withdrew this tier; the model's own default replaces it.
        effort = model.reasoning.default
    return ConnectionSpec(
        provider_id=provider.id,
        provider_label=provider.label,
        custom=False,
        protocol=provider.protocol,
        base_url=provider.base_url,
        model=model.id,
        model_label=model.label,
        context_window_tokens=model.context_window_tokens,
        max_output_tokens=model.max_output_tokens,
        reasoning=model.reasoning.setting(effort),
        structured_output=model.structured_output.capability(provider_id=provider.id, model_id=model.id),
    )


def _custom_spec(record: ModelConnectionRecord, provider: CustomProvider) -> ConnectionSpec:
    wire = record.reasoning_parameter or ReasoningWire.NONE
    mode = record.structured_output or "none"
    if (
        record.base_url is None
        or record.context_window_tokens is None
        or (wire is not ReasoningWire.NONE and not provider.offers_reasoning_parameter(wire))
        or not provider.offers_structured_output(mode)
    ):
        raise connection_withdrawn()
    effort: ReasoningEffort | None = record.reasoning_effort
    if effort is not None and effort not in wire.offered_efforts():
        effort = None
    return ConnectionSpec(
        provider_id=provider.id,
        provider_label=provider.label,
        custom=True,
        protocol=provider.protocol,
        base_url=record.base_url,
        model=record.model,
        model_label=record.model,
        context_window_tokens=record.context_window_tokens,
        max_output_tokens=record.max_output_tokens,
        reasoning=ReasoningSetting(wire=wire, effort=effort),
        structured_output=provider.structured_output_capability(mode),
    )


__all__ = ["ConnectionSpec", "resolve_connection_spec"]
