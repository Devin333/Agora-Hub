"""Operator catalog of the model providers hosted accounts may connect to.

Two kinds of entries:

* catalog providers: the operator fixes the address, the models, their
  windows and reasoning tiers; users only pick one and bring a key;
* custom providers: users enter the address, model id, window and how the
  endpoint takes reasoning settings. The address still has to pass the
  outbound policy (public HTTPS, or an administrator allowlist).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from framework.llm.clients import LLMWireProtocol, validate_endpoint_base_url
from framework.llm.models.reasoning import (
    ReasoningEffort,
    ReasoningSetting,
    ReasoningWire,
    ordered_efforts,
)
from framework.llm.structured_output import ProviderStructuredOutputCapability
from interfaces.services.model_connections.errors import ModelConnectionConfigurationError

CATALOG_SCHEMA_VERSION = "hosted-llm-providers.v2"
DEFAULT_CATALOG_PATH = Path("configs/hosted-llm-providers.yaml")
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MODEL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
# Native schema modes need a reviewed release record before they may enforce
# anything; the catalog only declares what a model's API offers without one.
STRUCTURED_OUTPUT_MODES = ("json_object", "none")
_STRUCTURED_OUTPUT_KEYS = frozenset({"mode", "revision", "source"})
_REASONING_KEYS = frozenset({"parameter", "efforts", "default"})
_PROTOCOL_WIRES: dict[LLMWireProtocol, frozenset[ReasoningWire]] = {
    LLMWireProtocol.OPENAI_CHAT: frozenset(ReasoningWire) - {ReasoningWire.ANTHROPIC_EFFORT},
    LLMWireProtocol.ANTHROPIC_MESSAGES: frozenset({ReasoningWire.NONE, ReasoningWire.ANTHROPIC_EFFORT}),
}


@dataclass(frozen=True, slots=True)
class CatalogStructuredOutput:
    """What a catalog model's API can enforce for a structured-output request."""

    mode: str
    revision: str
    source: str

    @property
    def returns_json_objects(self) -> bool:
        return self.mode == "json_object"

    def capability(self, *, provider_id: str, model_id: str) -> ProviderStructuredOutputCapability:
        return ProviderStructuredOutputCapability(
            provider=provider_id,
            deployment=f"{provider_id}/{model_id}",
            mode=self.mode,  # type: ignore[arg-type]
            revision=self.revision,
        )


@dataclass(frozen=True, slots=True)
class CatalogReasoning:
    """The reasoning tiers one model offers and how its API takes them."""

    parameter: ReasoningWire
    efforts: tuple[ReasoningEffort, ...]
    # ``None`` sends nothing and leaves the provider's own default in force.
    default: ReasoningEffort | None

    def setting(self, effort: ReasoningEffort | None) -> ReasoningSetting:
        # The declared default sizes the reasoning budget when no tier is
        # chosen; it is not sent, the provider applies it on its own.
        return ReasoningSetting(wire=self.parameter, effort=effort, provider_default=self.default)

    def offers(self, effort: ReasoningEffort | None) -> bool:
        return effort is None or effort in self.efforts

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter": self.parameter.value,
            "efforts": [item.value for item in self.efforts],
            "default": self.default.value if self.default is not None else None,
        }


NO_REASONING = CatalogReasoning(parameter=ReasoningWire.NONE, efforts=(), default=None)


@dataclass(frozen=True, slots=True)
class CatalogModel:
    id: str
    label: str
    context_window_tokens: int
    max_output_tokens: int | None
    reasoning: CatalogReasoning
    structured_output: CatalogStructuredOutput

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "contextWindowTokens": self.context_window_tokens,
            "maxOutputTokens": self.max_output_tokens,
            "reasoning": self.reasoning.to_dict(),
            "structuredOutput": self.structured_output.returns_json_objects,
        }


@dataclass(frozen=True, slots=True)
class CatalogProvider:
    """An operator-configured provider: fixed address and model list."""

    id: str
    label: str
    protocol: LLMWireProtocol
    base_url: str
    default_model: str
    models: tuple[CatalogModel, ...]

    custom = False

    def model(self, model_id: str) -> CatalogModel | None:
        return next((item for item in self.models if item.id == model_id), None)

    def to_dict(self) -> dict[str, Any]:
        # The base URL is operator configuration, not something the user
        # chooses; it is still shown so users know where their key is sent.
        return {
            "id": self.id,
            "label": self.label,
            "kind": "catalog",
            "protocol": self.protocol.value,
            "baseUrl": self.base_url,
            "defaultModel": self.default_model,
            "models": [item.to_dict() for item in self.models],
        }


@dataclass(frozen=True, slots=True)
class CustomProvider:
    """A provider whose address, model and limits the user supplies."""

    id: str
    label: str
    protocol: LLMWireProtocol
    reasoning_parameters: tuple[ReasoningWire, ...]
    structured_output_modes: tuple[str, ...]

    custom = True

    def offers_reasoning_parameter(self, wire: ReasoningWire) -> bool:
        return wire in self.reasoning_parameters

    def offers_structured_output(self, mode: str) -> bool:
        return mode in self.structured_output_modes

    def structured_output_capability(self, mode: str) -> ProviderStructuredOutputCapability:
        # The user's declaration is the only source there is; replies are
        # still validated locally against every contract.
        return ProviderStructuredOutputCapability(
            provider=self.id,
            deployment=f"{self.id}/user-declared",
            mode=mode,  # type: ignore[arg-type]
            revision=f"{self.id}-user-declared-{mode}",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": "custom",
            "protocol": self.protocol.value,
            "reasoningParameters": [item.value for item in self.reasoning_parameters],
            "reasoningEfforts": {
                item.value: [effort.value for effort in item.offered_efforts()]
                for item in self.reasoning_parameters
            },
            "structuredOutputModes": list(self.structured_output_modes),
        }


Provider = CatalogProvider | CustomProvider


@dataclass(frozen=True, slots=True)
class ModelProviderCatalog:
    providers: tuple[Provider, ...]

    def provider(self, provider_id: str) -> Provider | None:
        return next((item for item in self.providers if item.id == provider_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {"providers": [item.to_dict() for item in self.providers]}


def load_model_provider_catalog(path: str | Path = DEFAULT_CATALOG_PATH) -> ModelProviderCatalog:
    source = Path(path)
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ModelConnectionConfigurationError(f"{source} could not be read") from exc
    return parse_model_provider_catalog(payload, source=str(source))


def parse_model_provider_catalog(payload: Any, *, source: str = "catalog") -> ModelProviderCatalog:
    if not isinstance(payload, dict) or payload.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ModelConnectionConfigurationError(
            f"{source}: schema_version must be {CATALOG_SCHEMA_VERSION}"
        )
    raw_providers = payload.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        raise ModelConnectionConfigurationError(f"{source}: providers must be a non-empty list")
    providers = tuple(_provider(item, source=source) for item in raw_providers)
    ids = [item.id for item in providers]
    if len(set(ids)) != len(ids):
        raise ModelConnectionConfigurationError(f"{source}: provider ids must be unique")
    return ModelProviderCatalog(providers=providers)


def _provider(raw: Any, *, source: str) -> Provider:
    if not isinstance(raw, dict):
        raise _error(source, "every provider must be a mapping")
    provider_id = _text(raw, "id", source=source)
    if not IDENTIFIER.fullmatch(provider_id):
        raise _error(source, f"invalid provider id {provider_id!r}")
    where = f"{source}: provider {provider_id}"
    try:
        protocol = LLMWireProtocol(_text(raw, "protocol", source=where))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in LLMWireProtocol)
        raise _error(where, f"protocol must be one of {allowed}") from exc
    label = _text(raw, "label", source=where)
    custom = raw.get("custom", False)
    if not isinstance(custom, bool):
        raise _error(where, "custom must be true or false")
    if custom:
        return _custom_provider(raw, provider_id=provider_id, label=label, protocol=protocol, source=where)
    base_url = _text(raw, "base_url", source=where)
    if not base_url.lower().startswith("https://"):
        raise _error(where, "base_url must use https")
    try:
        base_url = validate_endpoint_base_url(base_url)
    except ValueError as exc:
        raise _error(where, str(exc)) from exc
    raw_models = raw.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise _error(where, "models must be a non-empty list")
    models = tuple(_model(item, protocol=protocol, source=where) for item in raw_models)
    model_ids = [item.id for item in models]
    if len(set(model_ids)) != len(model_ids):
        raise _error(where, "model ids must be unique")
    default_model = _text(raw, "default_model", source=where)
    if default_model not in model_ids:
        raise _error(where, "default_model must be one of its models")
    return CatalogProvider(
        id=provider_id,
        label=label,
        protocol=protocol,
        base_url=base_url,
        default_model=default_model,
        models=models,
    )


def _custom_provider(
    raw: dict[str, Any], *, provider_id: str, label: str, protocol: LLMWireProtocol, source: str
) -> CustomProvider:
    for key in ("base_url", "models", "default_model"):
        if key in raw:
            raise _error(source, f"a custom provider takes its {key} from the user")
    wires = _string_list(raw, "reasoning_parameters", source=source)
    try:
        parameters = tuple(dict.fromkeys(ReasoningWire.parse(item) for item in wires))
    except ValueError as exc:
        raise _error(source, str(exc)) from exc
    unsupported = [item.value for item in parameters if item not in _PROTOCOL_WIRES[protocol]]
    if unsupported:
        raise _error(source, f"{protocol.value} cannot send reasoning as {', '.join(unsupported)}")
    modes = tuple(dict.fromkeys(_string_list(raw, "structured_output_modes", source=source)))
    if not set(modes) <= set(STRUCTURED_OUTPUT_MODES):
        raise _error(
            source, f"structured_output_modes must be among {', '.join(STRUCTURED_OUTPUT_MODES)}"
        )
    return CustomProvider(
        id=provider_id,
        label=label,
        protocol=protocol,
        reasoning_parameters=parameters,
        structured_output_modes=modes,
    )


def _model(raw: Any, *, protocol: LLMWireProtocol, source: str) -> CatalogModel:
    if not isinstance(raw, dict):
        raise _error(source, "every model must be a mapping")
    model_id = _text(raw, "id", source=source)
    if not MODEL_IDENTIFIER.fullmatch(model_id):
        raise _error(source, f"invalid model id {model_id!r}")
    where = f"{source} model {model_id}"
    window = _positive_int(raw.get("context_window_tokens"), name="context_window_tokens", source=where)
    max_output = raw.get("max_output_tokens")
    if max_output is not None:
        max_output = _positive_int(max_output, name="max_output_tokens", source=where)
        if max_output >= window:
            raise _error(where, "max_output_tokens must be smaller than context_window_tokens")
    return CatalogModel(
        id=model_id,
        label=_text(raw, "label", source=where),
        context_window_tokens=window,
        max_output_tokens=max_output,
        reasoning=_reasoning(raw.get("reasoning"), protocol=protocol, source=where),
        structured_output=_structured_output(raw.get("structured_output"), source=where),
    )


def _reasoning(raw: Any, *, protocol: LLMWireProtocol, source: str) -> CatalogReasoning:
    # Required: a model whose reasoning controls nobody checked must not
    # receive parameters its API might reject or silently ignore.
    if not isinstance(raw, dict):
        raise _error(source, "reasoning must be a mapping")
    unknown = sorted(set(raw) - _REASONING_KEYS)
    if unknown:
        raise _error(source, f"reasoning has unsupported keys: {', '.join(unknown)}")
    try:
        wire = ReasoningWire.parse(_text(raw, "parameter", source=f"{source} reasoning"))
    except ValueError as exc:
        raise _error(source, str(exc)) from exc
    if wire not in _PROTOCOL_WIRES[protocol]:
        raise _error(source, f"{protocol.value} cannot send reasoning as {wire.value}")
    if wire is ReasoningWire.NONE:
        if raw.get("efforts") or raw.get("default") is not None:
            raise _error(source, "reasoning parameter none takes no efforts or default")
        return NO_REASONING
    try:
        efforts = ordered_efforts(_string_list(raw, "efforts", source=f"{source} reasoning"))
        default = ReasoningEffort.parse(raw["default"]) if raw.get("default") is not None else None
    except ValueError as exc:
        raise _error(source, str(exc)) from exc
    inexpressible = [item.value for item in efforts if item not in wire.offered_efforts()]
    if inexpressible:
        raise _error(source, f"{wire.value} cannot express {', '.join(inexpressible)}")
    if default is not None and default not in efforts:
        raise _error(source, "reasoning default must be one of its efforts")
    return CatalogReasoning(parameter=wire, efforts=efforts, default=default)


def _structured_output(raw: Any, *, source: str) -> CatalogStructuredOutput:
    # Required: a model whose structured-output support nobody checked must
    # not silently inherit some other deployment's capability.
    if not isinstance(raw, dict):
        raise _error(source, "structured_output must be a mapping")
    unknown = sorted(set(raw) - _STRUCTURED_OUTPUT_KEYS)
    if unknown:
        raise _error(source, f"structured_output has unsupported keys: {', '.join(unknown)}")
    mode = _text(raw, "mode", source=f"{source} structured_output")
    if mode not in STRUCTURED_OUTPUT_MODES:
        raise _error(source, f"structured_output.mode must be one of {', '.join(STRUCTURED_OUTPUT_MODES)}")
    return CatalogStructuredOutput(
        mode=mode,
        revision=_text(raw, "revision", source=f"{source} structured_output"),
        source=_text(raw, "source", source=f"{source} structured_output"),
    )


def _text(raw: dict[str, Any], key: str, *, source: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _error(source, f"{key} is required")
    return value.strip()


def _string_list(raw: dict[str, Any], key: str, *, source: str) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise _error(source, f"{key} must be a non-empty list of strings")
    return value


def _positive_int(value: Any, *, name: str, source: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _error(source, f"{name} must be a positive integer")
    return value


def _error(source: str, message: str) -> ModelConnectionConfigurationError:
    return ModelConnectionConfigurationError(f"{source}: {message}")


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "CatalogModel",
    "CatalogProvider",
    "CatalogReasoning",
    "CatalogStructuredOutput",
    "CustomProvider",
    "DEFAULT_CATALOG_PATH",
    "IDENTIFIER",
    "MODEL_IDENTIFIER",
    "ModelProviderCatalog",
    "NO_REASONING",
    "Provider",
    "STRUCTURED_OUTPUT_MODES",
    "load_model_provider_catalog",
    "parse_model_provider_catalog",
]
