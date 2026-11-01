from __future__ import annotations

import json
import os
try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from backend.foundation.models.source import SourceDefinition, SourceFetchPolicy
from backend.foundation.registry.source_registry import FETCHABLE_SOURCE_TYPES, SourceRegistry


class SourceConfigError(ValueError):
    pass


def build_default_source_registry(*, source_config_path: str | Path | None = None) -> SourceRegistry:
    configured_path, required = _default_source_config_path(source_config_path)
    if configured_path is not None:
        if not configured_path.exists():
            if required:
                raise SourceConfigError(f"source config file does not exist: {configured_path}")
        else:
            return load_source_registry(configured_path)
    return SourceRegistry(
        [
            SourceDefinition(
                source_id="openai-news",
                name="OpenAI News",
                source_type="rss",
                url="https://openai.com/news/rss.xml",
                reliability="high",
                authority_score=0.9,
                topics=["ai", "models"],
            ),
            SourceDefinition(
                source_id="google-ai-blog",
                name="Google AI Blog",
                source_type="rss",
                url="https://blog.google/technology/ai/rss/",
                reliability="high",
                authority_score=0.85,
                topics=["ai", "research"],
            ),
        ]
    )


def build_default_source_fetch_policy(
    *,
    source_config_path: str | Path | None = None,
) -> SourceFetchPolicy:
    configured_path, required = _default_source_config_path(source_config_path)
    if configured_path is not None:
        if not configured_path.exists():
            if required:
                raise SourceConfigError(f"source config file does not exist: {configured_path}")
        else:
            return load_source_fetch_policy(configured_path)
    return SourceFetchPolicy()


_PRD_SOURCE_SECTIONS = {
    "rss_feeds": "rss",
    "atom_feeds": "atom",
    "official_blogs": "official_blog",
    "github_lists": "github",
    "arxiv_categories": "arxiv",
    "hackernews_sources": "hackernews",
    "reddit_sources": "reddit",
    "lobsters_sources": "lobsters",
    "stackoverflow_tags": "stackoverflow",
    "devto_tags": "devto",
    "medium_feeds": "medium",
    "web_pages": "web_page",
    "manual_sources": "manual",
}
_SOURCE_DEFINITION_FIELDS = {
    "source_id",
    "id",
    "name",
    "source_type",
    "url",
    "reliability",
    "authority_score",
    "enabled",
    "fetch_interval_seconds",
    "respect_robots",
    "user_agent",
    "topics",
    "category",
    "language",
    "region",
    "metadata",
}
_FETCH_POLICY_FIELDS = {
    "timeout_seconds",
    "max_bytes",
    "max_redirects",
    "user_agent",
    "respect_robots",
    "rate_limit_per_domain_per_minute",
    "allowed_domains",
    "allowed_source_domains",
    "allowed_private_networks",
    "retry_times",
    "retry_on_status_codes",
}
_TOP_LEVEL_FIELDS = {"fetch", "sources", *_PRD_SOURCE_SECTIONS}


def load_source_definitions(path: str | Path, *, validate: bool = True) -> list[SourceDefinition]:
    config_path = Path(path)
    payload = _load_payload(config_path)
    definitions = _source_definitions_from_payload(payload)
    if validate:
        _validate_source_definitions(definitions)
    return definitions


def _source_definitions_from_payload(payload: Any) -> list[SourceDefinition]:
    source_payloads = _source_payloads(payload)
    definitions = []
    for index, source_payload in enumerate(source_payloads):
        try:
            definitions.append(_source_definition(source_payload))
        except Exception as exc:
            raise SourceConfigError(f"invalid source config at index {index}: {exc}") from exc
    return definitions


def load_source_registry(path: str | Path, *, validate: bool = True) -> SourceRegistry:
    payload = _load_payload(Path(path))
    definitions = _source_definitions_from_payload(payload)
    fetch_policy = _source_fetch_policy_from_payload(payload)
    registry = SourceRegistry(definitions)
    if validate:
        _validate_source_url_allowlist(definitions, allowed_domains=fetch_policy.allowed_domains)
        _validate_source_definitions(definitions)
    return registry


def load_source_fetch_policy(path: str | Path) -> SourceFetchPolicy:
    payload = _load_payload(Path(path))
    return _source_fetch_policy_from_payload(payload)


def _source_fetch_policy_from_payload(payload: Any) -> SourceFetchPolicy:
    if isinstance(payload, list):
        return SourceFetchPolicy()
    if not isinstance(payload, dict):
        raise SourceConfigError("source config must be a list or an object")
    _validate_top_level_fields(payload)
    fetch_payload = payload.get("fetch")
    if fetch_payload is None:
        return SourceFetchPolicy()
    if not isinstance(fetch_payload, dict):
        raise SourceConfigError("source fetch config must be an object")
    return _source_fetch_policy(fetch_payload)


def _default_source_config_path(path: str | Path | None) -> tuple[Path | None, bool]:
    if path is not None:
        return Path(path), True
    env_path = os.getenv("NEWS_SOURCES_CONFIG")
    if env_path:
        return Path(env_path), True
    default_path = Path("configs/sources.yaml")
    if default_path.exists():
        return default_path, False
    return None, False


def _load_payload(path: Path) -> Any:
    if not path.exists():
        raise SourceConfigError(f"source config file does not exist: {path}")
    suffix = path.suffix.casefold()
    try:
        if suffix == ".json":
            return json.loads(path.read_text(encoding="utf-8"))
        if suffix == ".toml":
            return tomllib.loads(path.read_text(encoding="utf-8"))
        if suffix in {".yaml", ".yml"}:
            return _load_yaml(path)
    except OSError as exc:
        raise SourceConfigError(f"could not read source config file: {path}") from exc
    except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SourceConfigError(_config_decode_error_message(path, exc, suffix=suffix)) from exc
    raise SourceConfigError(f"unsupported source config file type: {path.suffix}")


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ModuleNotFoundError as exc:
        raise SourceConfigError(
            "YAML source configs require PyYAML; use JSON/TOML or install pyyaml"
        ) from exc
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SourceConfigError(
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
    location = (
        f" at line {line}, column {column}"
        if line is not None and column is not None
        else ""
    )
    return f"source config file is not valid {format_name}{location}: {path}"


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


def _source_payloads(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict) and isinstance(payload.get("sources"), list):
        _validate_top_level_fields(payload)
        explicit_sources = _object_list(payload["sources"], field="sources")
        _validate_explicit_source_payloads(explicit_sources, field="sources")
        values = [
            *explicit_sources,
            *_prd_section_payloads(payload),
        ]
    elif isinstance(payload, dict):
        _validate_top_level_fields(payload)
        values = _prd_section_payloads(payload)
    else:
        raise SourceConfigError("source config must be a list or an object with a sources list")
    if not values:
        raise SourceConfigError("source config must define at least one source")
    source_payloads = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise SourceConfigError(f"source config entry at index {index} must be an object")
        source_payloads.append(value)
    return source_payloads


def _validate_top_level_fields(payload: dict[str, Any]) -> None:
    unknown_fields = sorted(set(payload) - _TOP_LEVEL_FIELDS)
    if unknown_fields:
        joined = ", ".join(unknown_fields)
        raise SourceConfigError(f"unsupported source config field(s): {joined}")


def _validate_explicit_source_payloads(values: list[dict[str, Any]], *, field: str) -> None:
    for index, value in enumerate(values):
        unknown_fields = sorted(set(value) - _SOURCE_DEFINITION_FIELDS)
        if unknown_fields:
            joined = ", ".join(unknown_fields)
            raise SourceConfigError(
                f"{field}[{index}] contains unsupported field(s): {joined}; "
                "put connector-specific fields under metadata"
            )


def _object_list(value: Any, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceConfigError(f"{field} must be a list")
    objects = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SourceConfigError(f"{field}[{index}] must be an object")
        objects.append(dict(item))
    return objects


def _prd_section_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    source_payloads: list[dict[str, Any]] = []
    for section, source_type in _PRD_SOURCE_SECTIONS.items():
        for entry in _object_list(payload.get(section), field=section):
            source_payloads.append(_normalize_section_payload(entry, section=section, source_type=source_type))
    return source_payloads


def _normalize_section_payload(
    payload: dict[str, Any],
    *,
    section: str,
    source_type: str,
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized.setdefault("source_type", source_type)
    if "source_id" not in normalized and "id" in normalized:
        normalized["source_id"] = normalized["id"]
    metadata = _dict_value(normalized.get("metadata"))
    for key, value in payload.items():
        if key not in _SOURCE_DEFINITION_FIELDS and value is not None:
            metadata[key] = value
    metadata["config_section"] = section
    if section == "arxiv_categories" and "query" not in metadata:
        arxiv_category = payload.get("arxiv_category") or payload.get("category")
        if arxiv_category:
            metadata["query"] = f"cat:{arxiv_category}"
    normalized["metadata"] = metadata
    return normalized


def _source_definition(payload: dict[str, Any]) -> SourceDefinition:
    return SourceDefinition(
        source_id=str(payload.get("source_id") or ""),
        name=str(payload.get("name") or ""),
        source_type=str(payload.get("source_type") or ""),
        url=str(payload.get("url") or ""),
        reliability=str(payload.get("reliability") or "medium"),
        authority_score=float(payload.get("authority_score", 0.5)),
        enabled=_bool_value(payload.get("enabled"), default=True),
        fetch_interval_seconds=_positive_int_value(
            payload.get("fetch_interval_seconds"),
            default=3600,
            field_name="fetch_interval_seconds",
        ),
        respect_robots=_bool_value(payload.get("respect_robots"), default=True),
        user_agent=_optional_text(payload.get("user_agent")),
        topics=_string_list(payload.get("topics")),
        category=_optional_text(payload.get("category")),
        language=_optional_text(payload.get("language")),
        region=_optional_text(payload.get("region")),
        metadata=_dict_value(payload.get("metadata")),
    )


def _source_fetch_policy(payload: dict[str, Any]) -> SourceFetchPolicy:
    unknown_fields = sorted(set(payload) - _FETCH_POLICY_FIELDS)
    if unknown_fields:
        joined = ", ".join(unknown_fields)
        raise SourceConfigError(f"unsupported source fetch config field(s): {joined}")

    values: dict[str, Any] = {}
    if "timeout_seconds" in payload:
        values["timeout_seconds"] = _float_value(
            payload["timeout_seconds"],
            field_name="fetch.timeout_seconds",
        )
    if "max_bytes" in payload:
        values["max_bytes"] = _int_value(payload["max_bytes"], field_name="fetch.max_bytes")
    if "max_redirects" in payload:
        values["max_redirects"] = _int_value(
            payload["max_redirects"],
            field_name="fetch.max_redirects",
        )
    if "user_agent" in payload:
        values["user_agent"] = _required_text_value(
            payload["user_agent"],
            field_name="fetch.user_agent",
        )
    if "respect_robots" in payload:
        values["respect_robots"] = _strict_bool_value(
            payload["respect_robots"],
            field_name="fetch.respect_robots",
        )
    if "rate_limit_per_domain_per_minute" in payload:
        rate_limit = payload["rate_limit_per_domain_per_minute"]
        values["rate_limit_per_domain_per_minute"] = (
            None
            if rate_limit is None
            else _int_value(rate_limit, field_name="fetch.rate_limit_per_domain_per_minute")
        )
    allowed_domains = _first_present(payload, "allowed_domains", "allowed_source_domains")
    if allowed_domains is not None:
        values["allowed_domains"] = _list_value(
            allowed_domains,
            field_name="fetch.allowed_domains",
        )
    if "allowed_private_networks" in payload:
        values["allowed_private_networks"] = _list_value(
            payload["allowed_private_networks"],
            field_name="fetch.allowed_private_networks",
        )
    if "retry_times" in payload:
        values["retry_times"] = _int_value(payload["retry_times"], field_name="fetch.retry_times")
    if "retry_on_status_codes" in payload:
        values["retry_on_status_codes"] = _int_tuple_value(
            payload["retry_on_status_codes"],
            field_name="fetch.retry_on_status_codes",
        )
    try:
        return SourceFetchPolicy(**values)
    except ValueError as exc:
        raise SourceConfigError(f"invalid source fetch config: {exc}") from exc


def _validate_source_url_allowlist(
    definitions: list[SourceDefinition],
    *,
    allowed_domains: tuple[str, ...],
) -> None:
    if not allowed_domains:
        return
    issues: list[str] = []
    for source in definitions:
        if source.source_type not in FETCHABLE_SOURCE_TYPES:
            continue
        host = (urlsplit(source.url).hostname or "").casefold()
        if _host_matches_allowed_domain(host, allowed_domains):
            continue
        issues.append(
            f"{source.source_id}.url: source URL host is not allowed by fetch.allowed_domains: {host or '<missing>'}"
        )
    if issues:
        raise SourceConfigError(f"source config validation failed: {'; '.join(issues)}")


def _validate_source_definitions(definitions: list[SourceDefinition]) -> None:
    validation = SourceRegistry(definitions).validate()
    if not validation.is_valid:
        issues = "; ".join(
            f"{issue.source_id}.{issue.field}: {issue.message}"
            for issue in validation.errors
        )
        raise SourceConfigError(f"source config validation failed: {issues}")


def _host_matches_allowed_domain(host: str, allowed_domains: tuple[str, ...]) -> bool:
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise SourceConfigError("source topics must be a list")
    return [str(item) for item in value if str(item).strip()]


def _dict_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceConfigError("source metadata must be an object")
    return dict(value)


def _list_value(value: Any, *, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SourceConfigError(f"{field_name} must be a list")
    return list(value)


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


def _strict_bool_value(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise SourceConfigError(f"{field_name} must be a boolean")


def _required_text_value(value: Any, *, field_name: str) -> str:
    if value is None:
        raise SourceConfigError(f"{field_name} is required")
    text = str(value).strip()
    if not text:
        raise SourceConfigError(f"{field_name} is required")
    return text


def _float_value(value: Any, *, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SourceConfigError(f"{field_name} must be a number") from exc


def _int_value(value: Any, *, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SourceConfigError(f"{field_name} must be an integer") from exc


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _int_tuple_value(value: Any, *, field_name: str) -> tuple[int, ...]:
    if not isinstance(value, list | tuple):
        raise SourceConfigError(f"{field_name} must be a list")
    return tuple(_int_value(item, field_name=field_name) for item in value)


def _positive_int_value(value: Any, *, default: int, field_name: str) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SourceConfigError(f"{field_name} must be an integer") from exc
    if parsed < 1:
        raise SourceConfigError(f"{field_name} must be at least 1")
    return parsed
