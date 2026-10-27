"""Operator catalog of the model providers hosted accounts may use."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from framework.llm.clients import validate_endpoint_base_url
from interfaces.services.model_credentials.errors import (
    ModelCredentialConfigurationError,
    provider_unsupported,
)

CATALOG_SCHEMA_VERSION = "hosted-llm-providers.v1"
DEFAULT_CATALOG_PATH = Path("configs/hosted-llm-providers.yaml")
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True, slots=True)
class CatalogModel:
    id: str
    label: str
    context_window_tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "contextWindowTokens": self.context_window_tokens,
        }


@dataclass(frozen=True, slots=True)
class CatalogProvider:
    id: str
    label: str
    base_url: str
    default_model: str
    models: tuple[CatalogModel, ...]

    def model(self, model_id: str) -> CatalogModel | None:
        return next((item for item in self.models if item.id == model_id), None)

    def to_dict(self) -> dict[str, Any]:
        # The base URL is operator configuration, not something the user
        # chooses; it is still shown so users know where their key is sent.
        return {
            "id": self.id,
            "label": self.label,
            "baseUrl": self.base_url,
            "defaultModel": self.default_model,
            "models": [item.to_dict() for item in self.models],
        }


@dataclass(frozen=True, slots=True)
class ModelProviderCatalog:
    providers: tuple[CatalogProvider, ...]

    def provider(self, provider_id: str) -> CatalogProvider | None:
        return next((item for item in self.providers if item.id == provider_id), None)

    def require(self, provider_id: str, model_id: str) -> tuple[CatalogProvider, CatalogModel]:
        provider = self.provider(provider_id)
        model = provider.model(model_id) if provider is not None else None
        if provider is None or model is None:
            raise provider_unsupported()
        return provider, model

    def require_context_window(self, minimum_tokens: int) -> None:
        """Refuse a catalog whose models cannot hold one research request."""

        too_small = [
            f"{provider.id}/{model.id}"
            for provider in self.providers
            for model in provider.models
            if model.context_window_tokens < minimum_tokens
        ]
        if too_small:
            raise ModelCredentialConfigurationError(
                "catalog models are smaller than the research request budget "
                f"({minimum_tokens} tokens): {', '.join(too_small)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {"providers": [item.to_dict() for item in self.providers]}


def load_model_provider_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> ModelProviderCatalog:
    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ModelCredentialConfigurationError(f"{source} could not be read") from exc
    return parse_model_provider_catalog(payload, source=str(source))


def parse_model_provider_catalog(payload: Any, *, source: str = "catalog") -> ModelProviderCatalog:
    if not isinstance(payload, dict) or payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ModelCredentialConfigurationError(
            f"{source}: schema_version must be {CATALOG_SCHEMA_VERSION}"
        )
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ModelCredentialConfigurationError(f"{source}: providers must be a non-empty list")
    providers = tuple(_provider(item, source=source) for item in raw_providers)
    ids = [item.id for item in providers]
    if len(set(ids)) != len(ids):
        raise ModelCredentialConfigurationError(f"{source}: provider ids must be unique")
    return ModelProviderCatalog(providers=providers)


def _provider(raw: Any, *, source: str) -> CatalogProvider:
    if not isinstance(raw, dict):
        raise ModelCredentialConfigurationError(f"{source}: every provider must be a mapping")
    provider_id = _text(raw, "id", source=source)
    if not _IDENTIFIER.fullmatch(provider_id):
        raise ModelCredentialConfigurationError(f"{source}: invalid provider id {provider_id!r}")
    where = f"{source}: provider {provider_id}"
    base_url = _text(raw, "base_url", source=where)
    if not base_url.lower().startswith("https://"):
        raise ModelCredentialConfigurationError(f"{where}: base_url must use https")
    try:
        base_url = validate_endpoint_base_url(base_url)
    except ValueError as exc:
        raise ModelCredentialConfigurationError(f"{where}: {exc}") from exc
    raw_models = raw.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ModelCredentialConfigurationError(f"{where}: models must be a non-empty list")
    models = tuple(_model(item, source=where) for item in raw_models)
    model_ids = [item.id for item in models]
    if len(set(model_ids)) != len(model_ids):
        raise ModelCredentialConfigurationError(f"{where}: model ids must be unique")
    default_model = _text(raw, "default_model", source=where)
    if default_model not in model_ids:
        raise ModelCredentialConfigurationError(f"{where}: default_model must be one of its models")
    return CatalogProvider(
        id=provider_id,
        label=_text(raw, "label", source=where),
        base_url=base_url,
        default_model=default_model,
        models=models,
    )


def _model(raw: Any, *, source: str) -> CatalogModel:
    if not isinstance(raw, dict):
        raise ModelCredentialConfigurationError(f"{source}: every model must be a mapping")
    model_id = _text(raw, "id", source=source)
    if not _MODEL_IDENTIFIER.fullmatch(model_id):
        raise ModelCredentialConfigurationError(f"{source}: invalid model id {model_id!r}")
    window = raw.get("context_window_tokens")
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ModelCredentialConfigurationError(
            f"{source}: model {model_id} needs a positive context_window_tokens"
        )
    return CatalogModel(
        id=model_id,
        label=_text(raw, "label", source=f"{source} model {model_id}"),
        context_window_tokens=window,
    )


def _text(raw: dict[str, Any], key: str, *, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelCredentialConfigurationError(f"{source}: {key} is required")
    return value.strip()


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogModel",
    "CatalogProvider",
    "DEFAULT_CATALOG_PATH",
    "ModelProviderCatalog",
    "load_model_provider_catalog",
    "parse_model_provider_catalog",
]
