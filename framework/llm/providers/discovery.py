"""List a vendor's models with a user's key and describe the one they pick.

Discovery only reads: it never stores the key and never sends a completion.
What a vendor's list does not report (window, output cap, reasoning tiers) is
filled in from the known-model table; anything still missing stays ``None`` so
the user is asked for it instead of being given a guess.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from framework.llm.clients.anthropic_messages import ANTHROPIC_API_VERSION
from framework.llm.clients.provider_errors import (
    LLMProviderError,
    decode_provider_error_payload,
    error_type_from_http_status,
    network_failure_diagnostic,
    provider_failure_diagnostic,
    read_bounded_http_error_body,
)
from framework.llm.clients.transport import Transport, urlopen_transport
from framework.llm.models.reasoning import (
    ReasoningEffort,
    ReasoningParameterStyle,
    default_effort,
    ordered_efforts,
)
from framework.llm.providers.catalog import (
    AnthropicThinkingOff,
    ProviderSpec,
    WireProtocol,
)
from framework.shared.secret_text import SecretText

_ANTHROPIC_PAGE_LIMIT = 1000
_MAX_ANTHROPIC_PAGES = 20
_MAX_LISTED_MODELS = 5000
_DISCOVERY_TIMEOUT_SECONDS = 20.0
# Keys vendors use for a model's input window and output cap in their model lists.
_WINDOW_KEYS = ("context_length", "context_window", "max_context_length", "max_input_tokens")
_OUTPUT_KEYS = ("max_completion_tokens", "max_output_tokens", "max_tokens")


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    display_name: str | None = None
    context_window_tokens: int | None = None
    max_output_tokens: int | None = None
    # Reported by the vendor itself (Anthropic capabilities); None when not reported.
    reasoning_efforts: tuple[ReasoningEffort, ...] | None = None
    thinking_off: AnthropicThinkingOff | None = None


@dataclass(frozen=True)
class ModelProfile:
    """What a connection needs to know about one model before it is used."""

    model_id: str
    context_window_tokens: int | None
    max_output_tokens: int | None
    reasoning_parameter: ReasoningParameterStyle
    reasoning_efforts: tuple[ReasoningEffort, ...]
    default_reasoning_effort: ReasoningEffort | None
    thinking_off: AnthropicThinkingOff | None = None

    def accepts(self, effort: ReasoningEffort | None) -> bool:
        if effort is None:
            return not self.reasoning_efforts
        return effort in self.reasoning_efforts


class ModelDiscovery:
    """Reads a vendor's model list through an injectable transport."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        timeout_seconds: float = _DISCOVERY_TIMEOUT_SECONDS,
    ) -> None:
        self._transport = transport or urlopen_transport
        self._timeout_seconds = timeout_seconds

    def list_models(
        self,
        spec: ProviderSpec,
        *,
        base_url: str,
        api_key: SecretText,
    ) -> tuple[DiscoveredModel, ...]:
        if spec.protocol is WireProtocol.ANTHROPIC_MESSAGES:
            models = self._list_anthropic(spec, base_url=base_url, api_key=api_key)
        else:
            models = self._list_openai(spec, base_url=base_url, api_key=api_key)
        unique: dict[str, DiscoveredModel] = {}
        for model in models:
            unique.setdefault(model.model_id, model)
        return tuple(sorted(unique.values(), key=lambda item: item.model_id))

    def _list_openai(
        self,
        spec: ProviderSpec,
        *,
        base_url: str,
        api_key: SecretText,
    ) -> list[DiscoveredModel]:
        payload = self._get_json(
            spec,
            f"{base_url.rstrip('/')}/models",
            {"Authorization": f"Bearer {api_key.reveal()}"},
        )
        return [_openai_model(item) for item in _model_items(spec, payload)]

    def _list_anthropic(
        self,
        spec: ProviderSpec,
        *,
        base_url: str,
        api_key: SecretText,
    ) -> list[DiscoveredModel]:
        headers = {"x-api-key": api_key.reveal(), "anthropic-version": ANTHROPIC_API_VERSION}
        root = f"{base_url.rstrip('/')}/v1/models"
        models: list[DiscoveredModel] = []
        after_id: str | None = None
        for _ in range(_MAX_ANTHROPIC_PAGES):
            query: dict[str, Any] = {"limit": _ANTHROPIC_PAGE_LIMIT}
            if after_id is not None:
                query["after_id"] = after_id
            payload = self._get_json(spec, f"{root}?{urlencode(query)}", headers)
            models.extend(_anthropic_model(item) for item in _model_items(spec, payload))
            last_id = payload.get("last_id")
            if not payload.get("has_more") or not isinstance(last_id, str) or not last_id:
                return models
            after_id = last_id
        raise _discovery_error(spec, "model list did not finish paging", error_type="schema_error")

    def _get_json(self, spec: ProviderSpec, url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = Request(url, headers={**headers, "Accept": "application/json"}, method="GET")
        try:
            raw = self._transport(request, self._timeout_seconds)
        except HTTPError as exc:
            raise _http_error(spec, exc) from exc
        except (TimeoutError, URLError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            timeout = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
            raise _discovery_error(
                spec,
                "model list request failed",
                error_type="timeout" if timeout else "transient_network",
                retryable=True,
                diagnostics=(network_failure_diagnostic(exc, reason=reason),),
            ) from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _discovery_error(
                spec,
                "model list is not valid JSON",
                error_type="schema_error",
                diagnostics=(provider_failure_diagnostic(raw_body=raw, stage="model_list_decode"),),
            ) from exc
        if not isinstance(payload, dict):
            raise _discovery_error(spec, "model list is not an object", error_type="schema_error")
        return payload


def describe_model(
    spec: ProviderSpec,
    model_id: str,
    *,
    discovered: DiscoveredModel | None = None,
    reasoning_parameter: ReasoningParameterStyle | None = None,
) -> ModelProfile:
    """Combine the vendor's live facts with the known-model table, live facts first."""
    known = spec.known_model(model_id)
    style = spec.reasoning_parameter_for(model_id, selected=reasoning_parameter)
    if discovered is not None and discovered.reasoning_efforts is not None:
        efforts = discovered.reasoning_efforts
        thinking_off = discovered.thinking_off
    elif known is not None:
        efforts = known.reasoning_efforts
        thinking_off = known.thinking_off
    else:
        efforts = spec.efforts_for(model_id, reasoning_parameter=reasoning_parameter)
        thinking_off = None
    window = _first_int(
        discovered.context_window_tokens if discovered else None,
        known.context_window_tokens if known else None,
    )
    output = _first_int(
        discovered.max_output_tokens if discovered else None,
        known.max_output_tokens if known else None,
    )
    preferred = known.default_reasoning_effort if known is not None else None
    if preferred is None or preferred not in efforts:
        preferred = default_effort(efforts) if efforts else None
    return ModelProfile(
        model_id=model_id,
        context_window_tokens=window,
        max_output_tokens=output,
        reasoning_parameter=style,
        reasoning_efforts=efforts,
        default_reasoning_effort=preferred,
        thinking_off=thinking_off,
    )


def _model_items(spec: ProviderSpec, payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise _discovery_error(spec, "model list has no data array", error_type="schema_error")
    items = [item for item in data[:_MAX_LISTED_MODELS] if isinstance(item, dict)]
    return [item for item in items if isinstance(item.get("id"), str) and item["id"].strip()]


def _openai_model(item: dict[str, Any]) -> DiscoveredModel:
    top_provider = item.get("top_provider") if isinstance(item.get("top_provider"), dict) else {}
    return DiscoveredModel(
        model_id=item["id"].strip(),
        display_name=_optional_text(item.get("name")),
        context_window_tokens=_first_int(*(item.get(key) for key in _WINDOW_KEYS)),
        max_output_tokens=_first_int(
            top_provider.get("max_completion_tokens"),
            *(item.get(key) for key in _OUTPUT_KEYS),
        ),
    )


def _anthropic_model(item: dict[str, Any]) -> DiscoveredModel:
    capabilities = item.get("capabilities")
    efforts: tuple[ReasoningEffort, ...] | None = None
    thinking_off: AnthropicThinkingOff | None = None
    if isinstance(capabilities, dict):
        efforts, thinking_off = _anthropic_reasoning(capabilities)
    return DiscoveredModel(
        model_id=item["id"].strip(),
        display_name=_optional_text(item.get("display_name")),
        context_window_tokens=_first_int(item.get("max_input_tokens")),
        max_output_tokens=_first_int(item.get("max_tokens")),
        reasoning_efforts=efforts,
        thinking_off=thinking_off,
    )


def _anthropic_reasoning(
    capabilities: dict[str, Any],
) -> tuple[tuple[ReasoningEffort, ...] | None, AnthropicThinkingOff | None]:
    effort = capabilities.get("effort")
    if not isinstance(effort, dict) or not _supported(effort):
        return None, None
    levels = [
        level
        for level in (ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH,
                      ReasoningEffort.XHIGH, ReasoningEffort.MAX)
        if _supported(effort.get(level.value))
    ]
    thinking = capabilities.get("thinking")
    types = thinking.get("types") if isinstance(thinking, dict) else None
    thinking_off: AnthropicThinkingOff | None = None
    if isinstance(types, dict):
        if _supported(types.get("disabled")):
            thinking_off = AnthropicThinkingOff.DISABLED
        elif _supported(types.get("between_tools")):
            thinking_off = AnthropicThinkingOff.BETWEEN_TOOLS
    if thinking_off is not None:
        levels.append(ReasoningEffort.OFF)
    return (ordered_efforts(levels) or None), thinking_off


def _supported(value: Any) -> bool:
    return isinstance(value, dict) and value.get("supported") is True


def _first_int(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


def _optional_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _http_error(spec: ProviderSpec, exc: HTTPError) -> LLMProviderError:
    status = int(exc.code)
    raw, truncated = read_bounded_http_error_body(exc)
    payload = decode_provider_error_payload(raw, truncated=truncated)
    return _discovery_error(
        spec,
        f"model list request failed: HTTP {status}",
        error_type=error_type_from_http_status(status),
        retryable=status in (408, 429) or status >= 500,
        status_code=status,
        diagnostics=(
            provider_failure_diagnostic(
                payload=payload,
                raw_body=raw,
                raw_body_truncated=truncated,
                stage="model_list_http_error",
            ),
        ),
    )


def _discovery_error(
    spec: ProviderSpec,
    message: str,
    *,
    error_type: str,
    retryable: bool = False,
    status_code: int | None = None,
    diagnostics: tuple[dict[str, Any], ...] = (),
) -> LLMProviderError:
    return LLMProviderError(
        f"{spec.kind.value} {message}",
        provider=spec.kind.value,
        error_type=error_type,
        retryable=retryable,
        status_code=status_code,
        diagnostics=diagnostics,
    )


__all__ = [
    "ANTHROPIC_API_VERSION",
    "DiscoveredModel",
    "ModelDiscovery",
    "ModelProfile",
    "describe_model",
]
