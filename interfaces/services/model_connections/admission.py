"""Whether a catalog can serve every model purpose at all.

Per-connection admission happens when a user assigns a connection. This
module answers the deployment-level question asked once at startup: does the
operator's catalog offer anything that could serve each purpose? A catalog
whose every model is too small for, say, the answer purpose would let users
connect models that can never be assigned to it.
"""

from __future__ import annotations

from collections.abc import Mapping

from interfaces.services.model_connections.catalog import (
    CatalogModel,
    CatalogProvider,
    CustomProvider,
    ModelProviderCatalog,
)
from interfaces.services.model_connections.purposes import ModelPurpose, PurposeRequirement


def model_serves(model: CatalogModel, requirement: PurposeRequirement) -> bool:
    """True when some reasoning tier the model offers fits ``requirement``."""
    if requirement.structured_output and not model.structured_output.returns_json_objects:
        return False
    tiers = (None, *model.reasoning.efforts)
    return any(
        requirement.required_window(model.reasoning.setting(tier), max_output_tokens=model.max_output_tokens)
        <= model.context_window_tokens
        for tier in tiers
    )


def unservable_purposes(
    catalog: ModelProviderCatalog,
    requirements: Mapping[ModelPurpose, PurposeRequirement],
) -> tuple[ModelPurpose, ...]:
    """Purposes no catalog model can serve; empty when a custom provider is offered.

    A custom provider lets users bring a model with whatever window a purpose
    needs, so its presence makes every purpose servable in principle.
    """
    if any(isinstance(provider, CustomProvider) for provider in catalog.providers):
        return ()
    models = [
        model
        for provider in catalog.providers
        if isinstance(provider, CatalogProvider)
        for model in provider.models
    ]
    return tuple(
        purpose
        for purpose in ModelPurpose
        if not any(model_serves(model, requirements[purpose]) for model in models)
    )


__all__ = ["model_serves", "unservable_purposes"]
