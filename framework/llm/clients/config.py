from __future__ import annotations

import json
import os
import re
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import urlsplit

from framework.llm.context.profile import ModelContextProfile
from framework.llm.models.capabilities import ModelCapabilities, _normalize_capability_name
from framework.llm.structured_output import ProviderStructuredOutputCapability
from framework.llm.structured_output.release import (
    ProviderStructuredOutputRelease,
    ProviderStructuredOutputReleaseError,
    provider_release_records_from_payload,
)
from framework.llm.clients.openai_compatible import (
    LLMConfigurationError,
    LLMRetryPolicy,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)

if TYPE_CHECKING:
    from framework.llm.routing.deployment import ModelDeployment


DEFAULT_MODELS_CONFIG_PATH = Path("configs/models.yaml")
DEFAULT_MODEL_ROUTE_ID = "writer-primary"
_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_VALUE_RE = re.compile(r"(?i)(sk-[A-Za-z0-9_-]{8,}|bearer\s+[A-Za-z0-9_.-]{8,})")
_SENSITIVE_KEY_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "secret",
    "token",
}
_TOP_LEVEL_MODEL_CONFIG_KEYS = {
    "default_model_route",
    "default_route",
    "default_route_id",
    "deployments",
    "embeddings",
    "model_groups",
    "pricing",
    "routes",
    "schema_version",
    "structured_output_releases",
}
_MODEL_GROUP_CONFIG_KEYS = {"deployments"}
_DEPLOYMENT_CONFIG_KEYS = {
    "api_base",
    "api_key",
    "api_key_env",
    "api_url",
    "base_url",
    "capabilities",
    "cooldown_seconds",
    "context_profile",
    "deployment_id",
    "enabled",
    "max_retries",
    "model",
    "model_capabilities",
    "provider",
    "provider_name",
    "structured_output_capability",
    "timeout_seconds",
}
_STRUCTURED_OUTPUT_CAPABILITY_KEYS = {
    "max_schema_bytes",
    "max_schema_depth",
    "mode",
    "revision",
    "supported_dialect",
    "supported_keywords",
    "supports_json_object_fallback",
    "supports_local_refs",
    "supports_stream_terminal_validation",
    "release_id",
    "release_digest",
}
_CONTEXT_PROFILE_CONFIG_KEYS = {
    "allow_conservative_fallback",
    "context_window_tokens",
    "default_output_tokens",
    "max_output_tokens",
    "normalizer_revision",
    "operational_input_fraction",
    "physical_context_window_tokens",
    "profile_revision",
    "provider_auto_truncation",
    "safety_margin_tokens",
    "tokenizer_family",
    "tokenizer_revision",
}
_ROUTE_CONFIG_KEYS = {
    "budget_policy",
    "deployment_id",
    "fallback_deployment_ids",
    "fallback_deployments",
    "group",
    "model_group",
    "model_group_id",
    "primary_deployment_id",
    "required_capabilities",
}
_CAPABILITY_CONFIG_KEYS = {
    "context_window_tokens",
    "json",
    "json_mode",
    "max_output_tokens",
    "multimodal",
    "multimodal_input",
    "parallel_tool_calls",
    "prompt_cache",
    "reasoning_tokens",
    "streaming",
    "structured_output",
    "supports_json_mode",
    "supports_multimodal_input",
    "supports_parallel_tool_calls",
    "supports_prompt_cache",
    "supports_reasoning_tokens",
    "supports_streaming",
    "supports_structured_output",
    "supports_tool_calling",
    "tool_calling",
    "tools",
}


@dataclass(frozen=True)
class OpenAICompatibleDeploymentConfig:
    deployment_id: str
    route_id: str
    config: OpenAICompatibleConfig
    capabilities: ModelCapabilities = ModelCapabilities()
    structured_output_capability: ProviderStructuredOutputCapability | None = None
    required_capabilities: tuple[str, ...] = ()
    fallback_deployment_ids: tuple[str, ...] = ()
    enabled: bool = True
    cooldown_seconds: int | None = None
    max_retries: int = 0
    budget_policy: dict[str, Any] = None  # type: ignore[assignment]
    context_profile: ModelContextProfile | None = None

    def retry_policy(self) -> LLMRetryPolicy:
        return LLMRetryPolicy(max_attempts=max(1, self.max_retries + 1))

    def build_client(self) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            self.config,
            retry_policy=self.retry_policy(),
            structured_output_capability=self.structured_output_capability,
        )

    def build_model_deployment(self) -> ModelDeployment:
        from framework.llm.routing.deployment import ModelDeployment

        return ModelDeployment(
            deployment_id=self.deployment_id,
            provider=self.config.provider,
            model=self.config.model,
            client=self.build_client(),
            capabilities=self.capabilities,
            structured_output_capability=self.structured_output_capability,
            enabled=self.enabled,
            metadata={
                "route_id": self.route_id,
                "cooldown_seconds": self.cooldown_seconds,
            },
            context_profile=self.context_profile,
        )


def build_openai_compatible_client_from_config(
    path: str | Path | None = None,
    *,
    route_id: str = DEFAULT_MODEL_ROUTE_ID,
) -> OpenAICompatibleClient:
    return load_openai_compatible_deployment(path, route_id=route_id).build_client()


def load_openai_compatible_deployment(
    path: str | Path | None = None,
    *,
    route_id: str | None = None,
    apply_environment_overrides: bool = True,
) -> OpenAICompatibleDeploymentConfig:
    configured_path, required = _default_model_config_path(path)
    if configured_path is None:
        selected_route_id = route_id or DEFAULT_MODEL_ROUTE_ID
        return OpenAICompatibleDeploymentConfig(
            deployment_id="dashscope-default",
            route_id=selected_route_id,
            config=OpenAICompatibleConfig.dashscope_defaults(),
        )
    if not configured_path.exists():
        if required:
            raise LLMConfigurationError(f"model config file does not exist: {configured_path}")
        selected_route_id = route_id or DEFAULT_MODEL_ROUTE_ID
        return OpenAICompatibleDeploymentConfig(
            deployment_id="dashscope-default",
            route_id=selected_route_id,
            config=OpenAICompatibleConfig.dashscope_defaults(),
        )

    payload = _load_payload(configured_path)
    validate_openai_compatible_models_config(payload)
    release_records = _structured_output_releases_from_payload(payload)
    selected_route_id = _select_route_id(payload, route_id=route_id)
    routes = _dict_value(payload.get("routes"), field="routes", default={})
    route_payload = _dict_value(routes.get(selected_route_id), field=f"routes.{selected_route_id}", default={})
    deployment_payload = _select_deployment_payload(payload, route_id=selected_route_id)
    return _deployment_config(
        deployment_payload,
        route_id=selected_route_id,
        route_payload=route_payload,
        apply_environment_overrides=apply_environment_overrides,
        release_records=release_records,
    )


def _default_model_config_path(path: str | Path | None) -> tuple[Path | None, bool]:
    if path is not None:
        return Path(path), True
    env_path = os.getenv("NEWS_MODELS_CONFIG")
    if env_path:
        return Path(env_path), True
    if DEFAULT_MODELS_CONFIG_PATH.exists():
        return DEFAULT_MODELS_CONFIG_PATH, False
    return None, False


def _load_payload(path: Path) -> Any:
    suffix = path.suffix.casefold()
    try:
        if suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        if suffix == ".toml":
            return tomllib.loads(path.read_text(encoding="utf-8"))
        if suffix in {".yaml", ".yml"}:
            return _load_yaml(path)
    except OSError as exc:
        raise LLMConfigurationError(f"could not read model config file: {path}") from exc
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise LLMConfigurationError(_config_decode_error_message(path, exc, suffix=suffix)) from exc
    raise LLMConfigurationError(f"unsupported model config file type: {path.suffix}")


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise LLMConfigurationError(
            "YAML model configs require PyYAML; use JSON/TOML or install pyyaml"
        ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise LLMConfigurationError(
            _config_decode_error_message(path, exc, suffix=path.suffix.casefold())
        ) from exc


def _config_decode_error_message(path: Path, exc: BaseException, *, suffix: str) -> str:
    format_name = {
        ".json": "JSON",
        ".toml": "TOML",
        ".yaml": "YAML",
        ".yml": "YAML",
    }.get(suffix, suffix.lstrip(".").upper() or "configuration")
    line, column = _decode_error_location(exc)
    location = f" at line {line}, column {column}" if line is not None and column is not None else ""
    return f"model config file is not valid {format_name}{location}: {path}"


def _decode_error_location(exc: BaseException) -> tuple[int | None, int | None]:
    line = getattr(exc, "lineno", None)
    column = getattr(exc, "colno", None)
    if isinstance(line, int) and isinstance(column, int):
        return line, column
    problem_mark = getattr(exc, "problem_mark", None)
    mark_line = getattr(problem_mark, "line", None)
    mark_column = getattr(problem_mark, "column", None)
    if isinstance(mark_line, int) and isinstance(mark_column, int):
        return mark_line + 1, mark_column + 1
    return None, None


def validate_openai_compatible_models_config(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise LLMConfigurationError("model config must be an object")
    _assert_no_literal_secrets(payload)
    _assert_allowed_keys(payload, allowed=_TOP_LEVEL_MODEL_CONFIG_KEYS, field="model config")
    release_records = _structured_output_releases_from_payload(payload)

    groups = _dict_value(payload.get("model_groups"), field="model_groups", default={})
    routes = _dict_value(payload.get("routes"), field="routes", default={})
    top_level_deployments = _deployment_list(payload.get("deployments"), field="deployments")
    if not groups and not top_level_deployments:
        raise LLMConfigurationError("model config must define model_groups or deployments")

    deployment_ids: set[str] = set()
    group_deployment_ids: dict[str, set[str]] = {}
    for group_id, group_payload in groups.items():
        group_key = _schema_key_text(group_id, field="model_groups")
        if not isinstance(group_payload, dict):
            raise LLMConfigurationError(f"model_groups.{group_key} must be an object")
        _assert_allowed_keys(
            group_payload,
            allowed=_MODEL_GROUP_CONFIG_KEYS,
            field=f"model_groups.{group_key}",
        )
        deployments = _deployment_list(
            group_payload.get("deployments"),
            field=f"model_groups.{group_key}.deployments",
        )
        if not deployments:
            raise LLMConfigurationError(
                f"model_groups.{group_key}.deployments must contain at least one deployment"
            )
        group_ids: set[str] = set()
        for index, deployment in enumerate(deployments):
            deployment_id = _validate_deployment_schema(
                deployment,
                field=f"model_groups.{group_key}.deployments[{index}]",
                release_records=release_records,
            )
            if deployment_id in deployment_ids:
                raise LLMConfigurationError(f"model deployment_id is duplicated: {deployment_id}")
            deployment_ids.add(deployment_id)
            group_ids.add(deployment_id)
        group_deployment_ids[group_key] = group_ids

    top_level_deployment_ids: set[str] = set()
    for index, deployment in enumerate(top_level_deployments):
        deployment_id = _validate_deployment_schema(
            deployment,
            field=f"deployments[{index}]",
            release_records=release_records,
        )
        if deployment_id in deployment_ids:
            raise LLMConfigurationError(f"model deployment_id is duplicated: {deployment_id}")
        deployment_ids.add(deployment_id)
        top_level_deployment_ids.add(deployment_id)

    for route_id, route_payload in routes.items():
        route_key = _schema_key_text(route_id, field="routes")
        if not isinstance(route_payload, dict):
            raise LLMConfigurationError(f"routes.{route_key} must be an object")
        _validate_route_schema(
            route_payload,
            route_id=route_key,
            group_deployment_ids=group_deployment_ids,
            top_level_deployment_ids=top_level_deployment_ids,
            all_deployment_ids=deployment_ids,
        )

    configured_default = _optional_text(
        payload.get("default_route_id")
        or payload.get("default_route")
        or payload.get("default_model_route")
    )
    if configured_default is not None:
        if routes and configured_default not in routes:
            raise LLMConfigurationError(f"default_route_id is not configured: {configured_default}")
        if not routes and groups and configured_default not in groups and not top_level_deployments:
            raise LLMConfigurationError(f"default_route_id does not reference a model group: {configured_default}")


def _validate_deployment_schema(
    payload: dict[str, Any],
    *,
    field: str,
    release_records: Mapping[str, ProviderStructuredOutputRelease],
) -> str:
    if not isinstance(payload, dict):
        raise LLMConfigurationError(f"{field} must be an object")
    _assert_allowed_keys(payload, allowed=_DEPLOYMENT_CONFIG_KEYS, field=field)
    deployment_id = _required_text(payload.get("deployment_id"), field=f"{field}.deployment_id")
    provider_kind = _required_text(payload.get("provider"), field=f"{field}.provider")
    if _ENV_PLACEHOLDER_RE.fullmatch(provider_kind) is None and provider_kind not in {
        "openai-compatible",
        "openai_compatible",
        "dashscope",
    }:
        raise LLMConfigurationError(f"{field}.provider is unsupported: {provider_kind}")
    _required_text(payload.get("model"), field=f"{field}.model")
    api_base = _required_text(
        _first_present(payload, "api_base", "base_url", "api_url"),
        field=f"{field}.api_base",
    )
    if _ENV_PLACEHOLDER_RE.fullmatch(api_base) is None:
        _validate_url(api_base, field=f"{field}.api_base")
    _validate_api_key_reference(payload, field=field)
    if "timeout_seconds" in payload:
        _positive_float(payload.get("timeout_seconds"), field=f"{field}.timeout_seconds")
    if "max_retries" in payload:
        _non_negative_int(payload.get("max_retries"), field=f"{field}.max_retries")
    if "cooldown_seconds" in payload:
        _optional_positive_int(payload.get("cooldown_seconds"), field=f"{field}.cooldown_seconds")
    if "enabled" in payload:
        _validate_boolish(payload.get("enabled"), field=f"{field}.enabled")
    _capabilities_from_payload(payload)
    _structured_output_capability_from_payload(
        payload,
        provider=_provider_name_for_payload(payload),
        deployment=deployment_id,
        release_records=release_records,
    )
    _validate_context_profile_schema(payload)
    return deployment_id


def _validate_route_schema(
    route_payload: dict[str, Any],
    *,
    route_id: str,
    group_deployment_ids: dict[str, set[str]],
    top_level_deployment_ids: set[str],
    all_deployment_ids: set[str],
) -> None:
    _assert_allowed_keys(route_payload, allowed=_ROUTE_CONFIG_KEYS, field=f"routes.{route_id}")
    group_id = _optional_text(
        route_payload.get("model_group")
        or route_payload.get("model_group_id")
        or route_payload.get("group")
    )
    deployment_scope: set[str]
    if group_id is not None:
        if group_id not in group_deployment_ids:
            raise LLMConfigurationError(f"routes.{route_id}.model_group is not configured: {group_id}")
        deployment_scope = group_deployment_ids[group_id]
    elif route_id in group_deployment_ids:
        deployment_scope = group_deployment_ids[route_id]
    elif top_level_deployment_ids:
        deployment_scope = top_level_deployment_ids
    else:
        raise LLMConfigurationError(f"routes.{route_id} must reference a configured model_group")

    deployment_id = _optional_text(
        route_payload.get("deployment_id") or route_payload.get("primary_deployment_id")
    )
    if deployment_id is not None and deployment_id not in deployment_scope:
        raise LLMConfigurationError(f"routes.{route_id}.deployment_id is not configured: {deployment_id}")

    _required_capabilities_from_route_payload(route_payload=route_payload)
    fallback_deployment_ids = _fallback_deployment_ids_from_route_payload(route_payload=route_payload)
    for fallback_id in fallback_deployment_ids:
        if fallback_id not in all_deployment_ids:
            raise LLMConfigurationError(f"routes.{route_id}.fallback_deployment_ids is not configured: {fallback_id}")
    _budget_policy_from_route_payload(route_payload)


def _validate_api_key_reference(payload: dict[str, Any], *, field: str) -> None:
    api_key_env = _optional_text(payload.get("api_key_env"))
    if api_key_env is not None:
        if _ENV_NAME_RE.fullmatch(api_key_env) is None:
            raise LLMConfigurationError(f"{field}.api_key_env must be an environment variable name")
        return
    api_key = _optional_text(payload.get("api_key"))
    if api_key is None:
        raise LLMConfigurationError(f"{field}.api_key or {field}.api_key_env is required")
    if _ENV_PLACEHOLDER_RE.fullmatch(api_key) is None:
        raise LLMConfigurationError(f"{field}.api_key must use an environment placeholder")


def _schema_key_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise LLMConfigurationError(f"{field} keys must be non-empty strings")
    return text


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None


def _validate_boolish(value: Any, *, field: str) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, str) and value.strip().casefold() in {
        "1",
        "0",
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
    }:
        return
    raise LLMConfigurationError(f"{field} must be a boolean")


def _select_deployment_payload(payload: dict[str, Any], *, route_id: str) -> dict[str, Any]:
    routes = _dict_value(payload.get("routes"), field="routes", default={})
    groups = _dict_value(payload.get("model_groups"), field="model_groups", default={})
    if routes and route_id not in routes:
        raise LLMConfigurationError(f"model route is not configured: {route_id}")
    route_payload = _dict_value(routes.get(route_id), field=f"routes.{route_id}", default={})

    group_id = _optional_text(
        route_payload.get("model_group")
        or route_payload.get("model_group_id")
        or route_payload.get("group")
    )
    deployment_id = _optional_text(
        route_payload.get("deployment_id") or route_payload.get("primary_deployment_id")
    )
    if group_id is None and route_id in groups:
        group_id = route_id

    if group_id is not None:
        group_payload = groups.get(group_id)
        if not isinstance(group_payload, dict):
            raise LLMConfigurationError(f"model group is not configured: {group_id}")
        deployments = _deployment_list(group_payload.get("deployments"), field=f"model_groups.{group_id}.deployments")
        return _select_from_deployments(deployments, deployment_id=deployment_id, field=f"model_groups.{group_id}.deployments")

    deployments = _deployment_list(payload.get("deployments"), field="deployments")
    if deployments:
        return _select_from_deployments(deployments, deployment_id=deployment_id, field="deployments")

    if groups:
        raise LLMConfigurationError(f"model route does not reference a configured model group: {route_id}")

    raise LLMConfigurationError("model config must define at least one deployment")


def _select_route_id(payload: dict[str, Any], *, route_id: str | None) -> str:
    explicit_route_id = _optional_text(route_id)
    if explicit_route_id:
        return explicit_route_id
    routes = _dict_value(payload.get("routes"), field="routes", default={})
    configured_default = _optional_text(
        payload.get("default_route_id")
        or payload.get("default_route")
        or payload.get("default_model_route")
    )
    if configured_default:
        if routes and configured_default not in routes:
            raise LLMConfigurationError(f"default_route_id is not configured: {configured_default}")
        return configured_default
    return DEFAULT_MODEL_ROUTE_ID


def _deployment_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        raise LLMConfigurationError(f"{field} must be a list or object")
    deployments = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise LLMConfigurationError(f"{field}[{index}] must be an object")
        deployments.append(item)
    return deployments


def _select_from_deployments(
    deployments: list[dict[str, Any]],
    *,
    deployment_id: str | None,
    field: str,
) -> dict[str, Any]:
    if not deployments:
        raise LLMConfigurationError(f"{field} must contain at least one deployment")
    if deployment_id is not None:
        for deployment in deployments:
            if str(deployment.get("deployment_id") or "") == deployment_id:
                return deployment
        raise LLMConfigurationError(f"model deployment is not configured: {deployment_id}")
    for deployment in deployments:
        if _bool_value(deployment.get("enabled"), default=True):
            return deployment
    raise LLMConfigurationError(f"{field} has no enabled deployment")


def _deployment_config(
    payload: dict[str, Any],
    *,
    route_id: str,
    route_payload: dict[str, Any] | None = None,
    apply_environment_overrides: bool = True,
    release_records: Mapping[str, ProviderStructuredOutputRelease] | None = None,
) -> OpenAICompatibleDeploymentConfig:
    llm_override_names = apply_environment_overrides
    provider_kind = _env_override_text(
        payload,
        "provider",
        env_names=("NEWS_LLM_PROVIDER",) if llm_override_names else (),
        field="provider",
        required=True,
    )
    if provider_kind not in {"openai-compatible", "openai_compatible", "dashscope"}:
        raise LLMConfigurationError(f"unsupported OpenAI-compatible provider: {provider_kind}")
    provider_name = _optional_text(
        _env_override_text(
            payload,
            "provider_name",
            env_names=("NEWS_LLM_PROVIDER_NAME",) if llm_override_names else (),
            field="provider_name",
            required=False,
        )
    ) or (
        "dashscope" if provider_kind == "dashscope" else provider_kind
    )
    base_url = _env_override_text(
        payload,
        "api_base",
        aliases=("base_url", "api_url"),
        env_names=("NEWS_LLM_BASE_URL",) if llm_override_names else (),
        field="api_base",
        required=True,
    )
    _validate_url(base_url, field="api_base")
    api_key_env = _api_key_env(
        payload,
        apply_environment_overrides=apply_environment_overrides,
    )
    timeout_seconds = _positive_float(
        payload.get("timeout_seconds", 90.0),
        field="timeout_seconds",
    )
    max_retries = _non_negative_int(payload.get("max_retries", 0), field="max_retries")
    capabilities = _capabilities_from_payload(payload)
    required_capabilities = _required_capabilities_from_route_payload(
        route_payload=route_payload or {}
    )
    fallback_deployment_ids = _fallback_deployment_ids_from_route_payload(
        route_payload=route_payload or {}
    )
    enabled = _bool_value(payload.get("enabled"), default=True)
    cooldown_seconds = _optional_positive_int(
        payload.get("cooldown_seconds"),
        field="cooldown_seconds",
    )
    budget_policy = _budget_policy_from_route_payload(route_payload or {})
    deployment_id = _required_text(payload.get("deployment_id"), field="deployment_id")
    model = _env_override_text(
        payload,
        "model",
        env_names=("NEWS_LLM_MODEL",) if llm_override_names else (),
        field="model",
        required=True,
    )
    context_profile = _context_profile_from_payload(
        payload,
        deployment_id=deployment_id,
        provider=provider_name,
        model=model,
        capabilities=capabilities,
    )
    structured_output_capability = _structured_output_capability_from_payload(
        payload,
        provider=provider_name,
        deployment=deployment_id,
        release_records=release_records or {},
    )
    return OpenAICompatibleDeploymentConfig(
        deployment_id=deployment_id,
        route_id=route_id,
        config=OpenAICompatibleConfig(
            provider=provider_name,
            base_url=base_url,
            model=model,
            api_key_env=api_key_env,
            timeout_seconds=timeout_seconds,
        ),
        capabilities=capabilities,
        structured_output_capability=structured_output_capability,
        required_capabilities=required_capabilities,
        fallback_deployment_ids=fallback_deployment_ids,
        enabled=enabled,
        cooldown_seconds=cooldown_seconds,
        max_retries=max_retries,
        budget_policy=budget_policy,
        context_profile=context_profile,
    )


def _api_key_env(
    payload: dict[str, Any],
    *,
    apply_environment_overrides: bool = True,
) -> str:
    api_key_env = _optional_text(
        (
            os.getenv("NEWS_LLM_API_KEY_ENV")
            if apply_environment_overrides
            else None
        )
        or payload.get("api_key_env")
    )
    if api_key_env:
        if _ENV_NAME_RE.fullmatch(api_key_env) is None:
            raise LLMConfigurationError("api_key_env must be an environment variable name")
        return api_key_env
    override_secret = _optional_text(
        os.getenv("NEWS_LLM_API_KEY") if apply_environment_overrides else None
    )
    if override_secret:
        raise LLMConfigurationError(
            "NEWS_LLM_API_KEY must not be used in model diagnostics; set NEWS_LLM_API_KEY_ENV"
        )
    api_key = _required_text(payload.get("api_key"), field="api_key")
    match = _ENV_PLACEHOLDER_RE.fullmatch(api_key)
    if match is None:
        raise LLMConfigurationError("api_key must use an environment placeholder such as ${DASHSCOPE_API_KEY}")
    return match.group(1)


def _capabilities_from_payload(payload: dict[str, Any]) -> ModelCapabilities:
    capability_payload = payload.get("capabilities")
    if capability_payload is None:
        capability_payload = payload.get("model_capabilities")
    if capability_payload is None:
        return ModelCapabilities()
    if not isinstance(capability_payload, dict):
        raise LLMConfigurationError("capabilities must be an object")
    _assert_allowed_keys(
        capability_payload,
        allowed=_CAPABILITY_CONFIG_KEYS,
        field="capabilities",
    )
    normalized = dict(capability_payload)
    context_window_tokens = normalized.pop("context_window_tokens", None)
    max_output_tokens = normalized.pop("max_output_tokens", None)
    bool_capability_values: dict[str, bool] = {}
    for key, value in normalized.items():
        if not isinstance(value, bool):
            raise LLMConfigurationError(f"capabilities.{key} must be a boolean")
        capability_name = _normalize_capability_name(key)
        bool_capability_values[capability_name] = value
    context_window = None
    max_output = None
    if context_window_tokens is not None:
        context_window = _positive_int(
            context_window_tokens,
            field="capabilities.context_window_tokens",
        )
    if max_output_tokens is not None:
        max_output = _positive_int(
            max_output_tokens,
            field="capabilities.max_output_tokens",
        )
    try:
        return ModelCapabilities(
            supports_streaming=bool_capability_values.get("supports_streaming", False),
            supports_tool_calling=bool_capability_values.get("supports_tool_calling", False),
            supports_parallel_tool_calls=bool_capability_values.get("supports_parallel_tool_calls", False),
            supports_structured_output=bool_capability_values.get("supports_structured_output", False),
            supports_json_mode=bool_capability_values.get("supports_json_mode", False),
            supports_multimodal_input=bool_capability_values.get("supports_multimodal_input", False),
            supports_reasoning_tokens=bool_capability_values.get("supports_reasoning_tokens", False),
            supports_prompt_cache=bool_capability_values.get("supports_prompt_cache", False),
            context_window_tokens=context_window,
            max_output_tokens=max_output,
        )
    except TypeError as exc:
        raise LLMConfigurationError(f"invalid capabilities config: {exc}") from exc


def _structured_output_capability_from_payload(
    payload: dict[str, Any],
    *,
    provider: str,
    deployment: str,
    release_records: Mapping[str, ProviderStructuredOutputRelease] | None = None,
) -> ProviderStructuredOutputCapability | None:
    raw = payload.get("structured_output_capability")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LLMConfigurationError("structured_output_capability must be an object")
    _assert_allowed_keys(
        raw,
        allowed=_STRUCTURED_OUTPUT_CAPABILITY_KEYS,
        field="structured_output_capability",
    )
    supported_keywords = raw.get("supported_keywords", [])
    if not isinstance(supported_keywords, list) or not all(
        isinstance(keyword, str) and keyword.strip()
        for keyword in supported_keywords
    ):
        raise LLMConfigurationError(
            "structured_output_capability.supported_keywords must be a list of strings"
        )
    release_id = _optional_text(raw.get("release_id"))
    release_digest = _optional_text(raw.get("release_digest"))
    if (release_id is None) != (release_digest is None):
        raise LLMConfigurationError(
            "structured_output_capability release_id and release_digest must be provided together"
        )
    release: ProviderStructuredOutputRelease | None = None
    if release_id is not None:
        release = (release_records or {}).get(release_id)
        if release is None:
            raise LLMConfigurationError(
                "structured_output_capability.release_id is not configured: "
                + release_id
            )
        if release.digest != release_digest:
            raise LLMConfigurationError(
                "structured_output_capability.release_digest does not match release record"
            )
    try:
        return ProviderStructuredOutputCapability(
            provider=provider,
            deployment=deployment,
            mode=_required_text(
                raw.get("mode"),
                field="structured_output_capability.mode",
            ),
            supported_dialect=_optional_text(raw.get("supported_dialect")),
            supported_keywords=frozenset(supported_keywords),
            supports_local_refs=_strict_bool(
                raw.get("supports_local_refs", False),
                field="structured_output_capability.supports_local_refs",
            ),
            supports_json_object_fallback=_strict_bool(
                raw.get("supports_json_object_fallback", False),
                field="structured_output_capability.supports_json_object_fallback",
            ),
            max_schema_bytes=_optional_positive_int(
                raw.get("max_schema_bytes"),
                field="structured_output_capability.max_schema_bytes",
            ),
            max_schema_depth=_optional_positive_int(
                raw.get("max_schema_depth"),
                field="structured_output_capability.max_schema_depth",
            ),
            supports_stream_terminal_validation=_strict_bool(
                raw.get("supports_stream_terminal_validation", False),
                field=(
                    "structured_output_capability."
                    "supports_stream_terminal_validation"
                ),
            ),
            revision=_required_text(
                raw.get("revision"),
                field="structured_output_capability.revision",
            ),
            release=release,
        )
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(
            f"invalid structured_output_capability config: {exc}"
        ) from exc


def _structured_output_releases_from_payload(
    payload: Mapping[str, Any],
) -> dict[str, ProviderStructuredOutputRelease]:
    try:
        return provider_release_records_from_payload(
            payload.get("structured_output_releases")
        )
    except ProviderStructuredOutputReleaseError as exc:
        raise LLMConfigurationError(
            f"invalid structured_output_releases config: {exc}"
        ) from exc


def _provider_name_for_payload(payload: dict[str, Any]) -> str:
    provider_kind = _required_text(payload.get("provider"), field="provider")
    return _optional_text(payload.get("provider_name")) or (
        "dashscope" if provider_kind == "dashscope" else provider_kind
    )


def _strict_bool(value: Any, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise LLMConfigurationError(f"{field} must be a boolean")
    return value


def _validate_context_profile_schema(payload: dict[str, Any]) -> None:
    raw_profile = payload.get("context_profile")
    if raw_profile is None:
        return
    if not isinstance(raw_profile, dict):
        raise LLMConfigurationError("context_profile must be an object")
    _assert_allowed_keys(
        raw_profile,
        allowed=_CONTEXT_PROFILE_CONFIG_KEYS,
        field="context_profile",
    )
    capabilities = _capabilities_from_payload(payload)
    physical_limit = raw_profile.get(
        "physical_context_window_tokens",
        raw_profile.get("context_window_tokens", capabilities.context_window_tokens),
    )
    max_output = raw_profile.get("max_output_tokens", capabilities.max_output_tokens)
    if physical_limit is None:
        raise LLMConfigurationError(
            "context_profile.physical_context_window_tokens or "
            "capabilities.context_window_tokens is required"
        )
    if max_output is None:
        raise LLMConfigurationError(
            "context_profile.max_output_tokens or capabilities.max_output_tokens is required"
        )
    physical_limit_value = _positive_int(
        physical_limit,
        field="context_profile.physical_context_window_tokens",
    )
    max_output_value = _positive_int(
        max_output,
        field="context_profile.max_output_tokens",
    )
    default_output = _positive_int(
        raw_profile.get("default_output_tokens"),
        field="context_profile.default_output_tokens",
    )
    if default_output > max_output_value:
        raise LLMConfigurationError(
            "context_profile.default_output_tokens must not exceed max_output_tokens"
        )
    fraction = _positive_float(
        raw_profile.get("operational_input_fraction", 0.9),
        field="context_profile.operational_input_fraction",
    )
    if fraction > 1:
        raise LLMConfigurationError("context_profile.operational_input_fraction must not exceed 1")
    _non_negative_int(
        raw_profile.get("safety_margin_tokens", 0),
        field="context_profile.safety_margin_tokens",
    )
    for field_name in (
        "tokenizer_family",
        "tokenizer_revision",
        "normalizer_revision",
        "profile_revision",
    ):
        _required_text(raw_profile.get(field_name), field=f"context_profile.{field_name}")
    for field_name in ("allow_conservative_fallback", "provider_auto_truncation"):
        if field_name in raw_profile:
            _validate_boolish(raw_profile.get(field_name), field=f"context_profile.{field_name}")
    if physical_limit_value <= max_output_value:
        raise LLMConfigurationError(
            "context_profile physical context window must be greater than max_output_tokens"
        )


def _context_profile_from_payload(
    payload: dict[str, Any],
    *,
    deployment_id: str,
    provider: str,
    model: str,
    capabilities: ModelCapabilities,
) -> ModelContextProfile | None:
    raw_profile = payload.get("context_profile")
    if raw_profile is None:
        return None
    if not isinstance(raw_profile, dict):
        raise LLMConfigurationError("context_profile must be an object")
    physical_limit = raw_profile.get(
        "physical_context_window_tokens",
        raw_profile.get("context_window_tokens", capabilities.context_window_tokens),
    )
    max_output = raw_profile.get("max_output_tokens", capabilities.max_output_tokens)
    try:
        return ModelContextProfile(
            provider=provider,
            model=model,
            deployment_id=deployment_id,
            physical_context_window_tokens=_positive_int(
                physical_limit,
                field="context_profile.physical_context_window_tokens",
            ),
            max_output_tokens=_positive_int(
                max_output,
                field="context_profile.max_output_tokens",
            ),
            default_output_tokens=_positive_int(
                raw_profile.get("default_output_tokens"),
                field="context_profile.default_output_tokens",
            ),
            tokenizer_family=_required_text(
                raw_profile.get("tokenizer_family"),
                field="context_profile.tokenizer_family",
            ),
            tokenizer_revision=_required_text(
                raw_profile.get("tokenizer_revision"),
                field="context_profile.tokenizer_revision",
            ),
            normalizer_revision=_required_text(
                raw_profile.get("normalizer_revision"),
                field="context_profile.normalizer_revision",
            ),
            profile_revision=_required_text(
                raw_profile.get("profile_revision"),
                field="context_profile.profile_revision",
            ),
            operational_input_fraction=_positive_float(
                raw_profile.get("operational_input_fraction", 0.9),
                field="context_profile.operational_input_fraction",
            ),
            safety_margin_tokens=_non_negative_int(
                raw_profile.get("safety_margin_tokens", 0),
                field="context_profile.safety_margin_tokens",
            ),
            allow_conservative_fallback=_bool_value(
                raw_profile.get("allow_conservative_fallback"),
                default=False,
            ),
            provider_auto_truncation=_bool_value(
                raw_profile.get("provider_auto_truncation"),
                default=False,
            ),
        )
    except ValueError as exc:
        raise LLMConfigurationError(f"invalid context_profile config: {exc}") from exc


def _required_capabilities_from_route_payload(*, route_payload: dict[str, Any]) -> tuple[str, ...]:
    raw = route_payload.get("required_capabilities")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LLMConfigurationError("required_capabilities must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip():
            raise LLMConfigurationError("required_capabilities must contain non-empty strings")
        capability = value.strip().lower().replace("-", "_")
        if capability not in seen:
            normalized.append(capability)
            seen.add(capability)
    return tuple(normalized)


def _fallback_deployment_ids_from_route_payload(*, route_payload: dict[str, Any]) -> tuple[str, ...]:
    raw = route_payload.get("fallback_deployment_ids") or route_payload.get("fallback_deployments")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LLMConfigurationError("fallback_deployment_ids must be a list")
    values: list[str] = []
    seen: set[str] = set()
    for value in raw:
        text = _optional_text(value)
        if text is None:
            raise LLMConfigurationError("fallback_deployment_ids must contain non-empty strings")
        if text not in seen:
            values.append(text)
            seen.add(text)
    return tuple(values)


def _budget_policy_from_route_payload(route_payload: dict[str, Any]) -> dict[str, Any]:
    raw = route_payload.get("budget_policy")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LLMConfigurationError("budget_policy must be an object")
    return dict(raw)


def _assert_no_literal_secrets(value: Any, *, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            if _is_sensitive_key(key_text):
                if key_text in {"api_key", "api-key"} and isinstance(item, str) and _ENV_PLACEHOLDER_RE.fullmatch(item):
                    continue
                if key_text in {"api_key_env", "api-key-env"} and isinstance(item, str) and _ENV_NAME_RE.fullmatch(item):
                    continue
                raise LLMConfigurationError(f"model config field {item_path} must use an environment placeholder")
            _assert_no_literal_secrets(item, path=item_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_literal_secrets(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise LLMConfigurationError(f"model config field {path or '<root>'} contains a literal secret")


def _assert_allowed_keys(
    payload: dict[str, Any],
    *,
    allowed: set[str],
    field: str,
) -> None:
    unknown = sorted(str(key) for key in payload if str(key) not in allowed)
    if unknown:
        allowed_text = ", ".join(sorted(allowed))
        unknown_text = ", ".join(unknown)
        raise LLMConfigurationError(
            f"{field} contains unsupported field(s): {unknown_text}; allowed fields: {allowed_text}"
        )


def _is_sensitive_key(key: str) -> bool:
    normalized = key.replace("-", "_").casefold()
    if normalized in _SENSITIVE_KEY_PARTS:
        return True
    parts = [part for part in normalized.split("_") if part]
    if any(part in _SENSITIVE_KEY_PARTS for part in parts):
        return True
    return any(left == "api" and right == "key" for left, right in zip(parts, parts[1:]))


def _validate_url(value: str, *, field: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMConfigurationError(f"{field} must be an http or https URL")


def _dict_value(value: Any, *, field: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if value is None:
        return dict(default or {})
    if not isinstance(value, dict):
        raise LLMConfigurationError(f"{field} must be an object")
    return dict(value)


def _required_text(value: Any, *, field: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise LLMConfigurationError(f"{field} is required")
    return text


def _env_override_text(
    payload: dict[str, Any],
    key: str,
    *,
    field: str,
    required: bool,
    aliases: tuple[str, ...] = (),
    env_names: tuple[str, ...] = (),
) -> str:
    for env_name in env_names:
        env_value = _optional_text(os.getenv(env_name))
        if env_value:
            return env_value
    for candidate in (key, *aliases):
        value = _optional_text(payload.get(candidate))
        if value:
            return _resolve_env_placeholder(value, field=field)
    if required:
        raise LLMConfigurationError(f"{field} is required")
    return ""


def _resolve_env_placeholder(value: str, *, field: str) -> str:
    match = _ENV_PLACEHOLDER_RE.fullmatch(value)
    if match is None:
        return value
    env_name = match.group(1)
    resolved = _optional_text(os.getenv(env_name))
    if resolved is None:
        raise LLMConfigurationError(f"{field} environment variable is not set: {env_name}")
    return resolved


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _positive_float(value: Any, *, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(f"{field} must be a number") from exc
    if parsed <= 0:
        raise LLMConfigurationError(f"{field} must be greater than zero")
    return parsed


def _positive_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise LLMConfigurationError(f"{field} must be greater than zero")
    return parsed


def _optional_positive_int(value: Any, *, field: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field=field)


def _non_negative_int(value: Any, *, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LLMConfigurationError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise LLMConfigurationError(f"{field} must be non-negative")
    return parsed

