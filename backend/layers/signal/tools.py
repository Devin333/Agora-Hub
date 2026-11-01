from __future__ import annotations

from datetime import datetime, timezone as _tz
from typing import Any
from urllib.parse import urlsplit

from framework.tool.models import ToolDefinition
from framework.tool.registry import ToolRegistry
from backend.foundation.models.source import (
    RawSourceItem,
    SourceDefinition,
    SourceError,
    SourceFetchPolicy,
    SourceReliability,
    SourceType,
)
from backend.foundation.primitives.source_ref import canonicalize_url as canonicalize_source_url
from backend.foundation.registry.source_registry import SourceRegistry
from backend.layers.signal.source_health import BasicSourceHealthManager
from backend.layers.signal.source_processing.error_metadata import (
    SourceErrorMetadataInput,
    source_error_metadata,
)
from backend.layers.signal.source_processing.error_taxonomy import (
    SourceTaxonomyExtension,
    classify_source_exception,
    effective_source_retryable,
)
from backend.layers.signal.source_tool_runtime import (
    FetchText,
    SourceRateLimiter,
    SourceTextFetchResult,
    SourceToolRuntime,
    effective_source_fetch_policy,
    source_fetch_policy_without_rate_limit,
    source_rate_limited_error,
)


UTC = _tz.utc
# The fetch itself is bounded by fetch_policy.timeout_seconds (the whole-fetch
# budget, retries included); the margin covers rate limiting and parsing.
FETCH_TOOL_TIMEOUT_MARGIN_SECONDS = 1.0
# Health is read from the configured health store, which may be remote.
SOURCE_HEALTH_READ_TIMEOUT_SECONDS = 10.0


def register_source_tools(
    registry: ToolRegistry,
    *,
    fetch_text: FetchText | None = None,
    fetch_policy: SourceFetchPolicy | None = None,
    allowed_domains: list[str] | None = None,
    health_manager: BasicSourceHealthManager | None = None,
    source_registry: SourceRegistry | None = None,
    rate_limiter: SourceRateLimiter | None = None,
    source_runtime: SourceToolRuntime | None = None,
) -> None:
    fetch_policy = _coerce_fetch_policy(fetch_policy)
    allowed_domain_tuple = _allowed_domains(allowed_domains) or tuple(fetch_policy.allowed_domains)
    health_manager = health_manager or BasicSourceHealthManager()
    registry.register(
        ToolDefinition(
            name="source.fetch_url",
            description="Fetch text content for a configured source URL with source fetch policy.",
            input_schema={
                "required": ["source"],
                "properties": {
                    "source": {"type": "object"},
                    "timeout_seconds": {"type": "number"},
                    "max_bytes": {"type": "integer"},
                    "max_redirects": {"type": "integer"},
                    "respect_robots": {"type": "boolean"},
                    "user_agent": {"type": "string"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
            timeout_seconds=fetch_policy.timeout_seconds + FETCH_TOOL_TIMEOUT_MARGIN_SECONDS,
            max_result_bytes=1_100_000,
        ),
        lambda args: _fetch_url(
            args,
            default_policy=fetch_policy,
            fetch_text=fetch_text,
            allowed_domains=allowed_domain_tuple,
            health_manager=health_manager,
            rate_limiter=rate_limiter,
            source_runtime=source_runtime,
        ),
    )
    registry.register(
        ToolDefinition(
            name="source.parse_rss",
            description="Parse RSS XML into raw source items.",
            input_schema=_parse_feed_schema(),
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _parse_feed(
            args,
            default_source_type=SourceType.RSS,
            source_runtime=source_runtime,
        ),
    )
    registry.register(
        ToolDefinition(
            name="source.extract_items",
            description="Extract raw source items from fetched RSS, Atom, or HTML content.",
            input_schema={
                "required": ["source", "content"],
                "properties": {
                    "source": {"type": "object"},
                    "content": {"type": ["string", "array"]},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _extract_items(args, source_runtime=source_runtime),
    )
    registry.register(
        ToolDefinition(
            name="source.fetch_official_blog",
            description="Fetch and parse a marked official blog RSS, Atom, or HTML source.",
            input_schema={
                "required": [],
                "properties": {
                    "source": {"type": "object"},
                    "source_id": {"type": "string"},
                    "topic": {"type": "string"},
                    "language": {"type": "string"},
                    "region": {"type": "string"},
                    "limit": {"type": "integer"},
                    "timeout_seconds": {"type": "number"},
                    "max_bytes": {"type": "integer"},
                    "max_redirects": {"type": "integer"},
                    "respect_robots": {"type": "boolean"},
                    "user_agent": {"type": "string"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
            timeout_seconds=fetch_policy.timeout_seconds + FETCH_TOOL_TIMEOUT_MARGIN_SECONDS,
            max_result_bytes=1_000_000,
            metadata={"source_kind": "official_blog"},
        ),
        lambda args: _fetch_official_blog(
            args,
            source_registry=source_registry,
            default_policy=fetch_policy,
            fetch_text=fetch_text,
            allowed_domains=allowed_domain_tuple,
            health_manager=health_manager,
            rate_limiter=rate_limiter,
            source_runtime=source_runtime,
        ),
    )
    registry.register(
        ToolDefinition(
            name="source.check_health",
            description="Read current source health for a source id.",
            input_schema={
                "required": [],
                "properties": {
                    "source_id": {"type": "string"},
                    "source": {"type": "object"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
            timeout_seconds=SOURCE_HEALTH_READ_TIMEOUT_SECONDS,
        ),
        lambda args: {"health": _source_health(args, health_manager=health_manager).to_dict()},
    )
    registry.register(
        ToolDefinition(
            name="source.probe",
            description="Probe a source URL and update in-memory source health.",
            input_schema={
                "required": ["source"],
                "properties": {
                    "source": {"type": "object"},
                    "timeout_seconds": {"type": "number"},
                    "max_bytes": {"type": "integer"},
                    "max_redirects": {"type": "integer"},
                    "respect_robots": {"type": "boolean"},
                    "user_agent": {"type": "string"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=False,
            timeout_seconds=fetch_policy.timeout_seconds + FETCH_TOOL_TIMEOUT_MARGIN_SECONDS,
            max_result_bytes=100_000,
            metadata={"updates_source_health": True},
        ),
        lambda args: _probe_source(
            args,
            default_policy=fetch_policy,
            fetch_text=fetch_text,
            allowed_domains=allowed_domain_tuple,
            health_manager=health_manager,
            rate_limiter=rate_limiter,
            source_runtime=source_runtime,
        ),
    )
    if source_registry is not None:
        registry.register(
            ToolDefinition(
                name="source.search",
                description="Search configured sources from SourceRegistry.",
                input_schema={
                    "required": [],
                    "properties": {
                        "query": {"type": "string"},
                        "enabled_only": {"type": "boolean"},
                        "language": {"type": "string"},
                        "region": {"type": "string"},
                        "reliability": {"type": "string"},
                        "source_type": {"type": "string"},
                        "fallback_to_enabled": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
                side_effect="read_only",
                concurrency_safe=True,
            ),
            lambda args: _search_sources(args, source_registry=source_registry),
        )
    registry.register(
        ToolDefinition(
            name="source.parse_atom",
            description="Parse Atom XML into raw source items.",
            input_schema=_parse_feed_schema(),
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _parse_feed(
            args,
            default_source_type=SourceType.ATOM,
            source_runtime=source_runtime,
        ),
    )
    registry.register(
        ToolDefinition(
            name="source.extract_html",
            description="Extract visible text and metadata from fetched HTML content.",
            input_schema={
                "required": ["source", "html"],
                "properties": {
                    "source": {"type": "object"},
                    "html": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _parse_html(args, source_runtime=source_runtime),
    )
    registry.register(
        ToolDefinition(
            name="source.extract_manual",
            description="Extract raw source items from human-curated manual source records.",
            input_schema={
                "required": ["source", "records"],
                "properties": {
                    "source": {"type": "object"},
                    "records": {"type": "array"},
                    "limit": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        lambda args: _parse_manual(args, source_runtime=source_runtime),
    )
    registry.register(
        ToolDefinition(
            name="source.normalize_url",
            description="Canonicalize a source URL and strip known tracking parameters.",
            input_schema={
                "required": ["url"],
                "properties": {
                    "url": {"type": "string"},
                    "base_url": {"type": "string"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
        ),
        _normalize_url,
    )


def _normalize_url(args: dict[str, Any]) -> dict[str, str]:
    """Canonical form of an absolute http(s) URL.

    The shared canonicalizer passes relative or empty input through unchanged
    (stored identities may be relative); as a tool answer that would hand the
    model "" or "/path" as a canonical URL, so anything that does not resolve
    to an absolute http(s) URL is refused here.
    """

    raw_url = str(args["url"])
    base_url = str(args["base_url"]) if args.get("base_url") is not None else None
    canonical = canonicalize_source_url(raw_url, base_url=base_url)
    parts = urlsplit(canonical)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        hint = "" if base_url else "; pass base_url to resolve a relative URL"
        raise ValueError(f"url does not resolve to an absolute http(s) URL{hint}")
    return {"canonical_url": canonical}


def _parse_feed_schema() -> dict[str, Any]:
    return {
        "required": ["source", "xml"],
        "properties": {
            "source": {"type": "object"},
            "xml": {"type": "string"},
            "limit": {"type": "integer"},
        },
        "additionalProperties": False,
    }


def _parse_feed(
    args: dict[str, Any],
    *,
    default_source_type: SourceType,
    source_runtime: SourceToolRuntime | None,
) -> dict[str, Any]:
    source = _source_definition(args["source"], default_source_type=default_source_type)
    limit = args.get("limit")
    runtime = _require_source_runtime(source_runtime, "source.parse_rss")
    items = runtime.parse_feed(
        source,
        str(args["xml"]),
        limit=int(limit) if limit is not None else None,
    )
    return {
        "item_count": len(items),
        "items": [_raw_source_item_to_dict(item) for item in items],
    }


def _extract_items(args: dict[str, Any], *, source_runtime: SourceToolRuntime | None) -> dict[str, Any]:
    source = _source_definition(args["source"], default_source_type=SourceType.RSS)
    if _is_html_backed_source_type(SourceType(source.source_type)):
        return _parse_html(
            {"source": args["source"], "html": args["content"], "limit": args.get("limit")},
            source_runtime=source_runtime,
        )
    if source.source_type == SourceType.MANUAL:
        return _parse_manual(
            {"source": args["source"], "records": args["content"], "limit": args.get("limit")},
            source_runtime=source_runtime,
        )
    return _parse_feed(
        {"source": args["source"], "xml": args["content"], "limit": args.get("limit")},
        default_source_type=SourceType.RSS,
        source_runtime=source_runtime,
    )


def _parse_html(args: dict[str, Any], *, source_runtime: SourceToolRuntime | None) -> dict[str, Any]:
    source = _source_definition(args["source"], default_source_type=SourceType.HTML)
    if not _is_html_backed_source_type(SourceType(source.source_type)):
        raise ValueError("source.extract_html requires source_type=html, official_blog, or web_page")
    limit = args.get("limit")
    runtime = _require_source_runtime(source_runtime, "source.extract_html")
    items = runtime.parse_html(
        source,
        str(args["html"]),
        limit=int(limit) if limit is not None else None,
    )
    return {
        "item_count": len(items),
        "items": [_raw_source_item_to_dict(item) for item in items],
    }


def _parse_manual(args: dict[str, Any], *, source_runtime: SourceToolRuntime | None) -> dict[str, Any]:
    source = _source_definition(args["source"], default_source_type=SourceType.MANUAL)
    if source.source_type != SourceType.MANUAL:
        raise ValueError("source.extract_manual requires source_type=manual")
    records = args["records"]
    if not isinstance(records, list):
        raise ValueError("source.extract_manual records must be an array")
    runtime = _require_source_runtime(source_runtime, "source.extract_manual")
    items, errors = runtime.fetch_manual(
        source,
        records=records,
        limit=_optional_limit(args.get("limit")),
    )
    return {
        "item_count": len(items),
        "error_count": len(errors),
        "items": [_raw_source_item_to_dict(item) for item in items],
        "errors": [error.to_dict() for error in errors],
    }


def _fetch_official_blog(
    args: dict[str, Any],
    *,
    source_registry: SourceRegistry | None,
    default_policy: SourceFetchPolicy,
    fetch_text: FetchText | None,
    allowed_domains: tuple[str, ...],
    health_manager: BasicSourceHealthManager,
    rate_limiter: SourceRateLimiter | None,
    source_runtime: SourceToolRuntime | None,
) -> dict[str, Any]:
    source = _official_blog_source(args, source_registry=source_registry)
    _ensure_official_blog(source)
    _ensure_official_blog_source_type(source)
    _ensure_http_url(source.url)
    _ensure_allowed_domain(source.url, allowed_domains)
    _ensure_source_health_allows_fetch(source, health_manager, respect_interval=True)
    policy = effective_source_fetch_policy(_fetch_policy(args, default_policy), source)
    rate_limit_error = _source_rate_limit_error(source, policy, rate_limiter)
    if rate_limit_error is not None:
        return _official_blog_error_result(source, [rate_limit_error])
    runtime = _require_source_runtime(source_runtime, "source.fetch_official_blog")
    items, errors = runtime.fetch_official_blog(
        source,
        policy=source_fetch_policy_without_rate_limit(policy),
        limit=_optional_limit(args.get("limit")),
    )
    return {
        "source": _source_definition_to_dict(source),
        "item_count": len(items),
        "items": [_raw_source_item_to_dict(item) for item in items],
        "error_count": len(errors),
        "errors": [error.to_dict() for error in errors],
    }


def _fetch_url(
    args: dict[str, Any],
    *,
    default_policy: SourceFetchPolicy,
    fetch_text: FetchText | None,
    allowed_domains: tuple[str, ...],
    health_manager: BasicSourceHealthManager,
    rate_limiter: SourceRateLimiter | None,
    source_runtime: SourceToolRuntime | None,
) -> dict[str, Any]:
    source = _source_definition(args["source"], default_source_type=SourceType.RSS)
    _ensure_http_url(source.url)
    _ensure_allowed_domain(source.url, allowed_domains)
    _ensure_source_health_allows_fetch(source, health_manager, respect_interval=True)
    policy = effective_source_fetch_policy(_fetch_policy(args, default_policy), source)
    _ensure_source_rate_limit_allows_fetch(source, policy, rate_limiter)
    fetch_result = _fetch_text(
        source.url,
        policy,
        fetch_text,
        source_runtime=source_runtime,
    )
    content = fetch_result.content
    content_bytes = len(content.encode("utf-8"))
    if content_bytes > policy.max_bytes:
        raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
    return {
        "source_id": source.source_id,
        "source_name": source.name,
        "source_type": SourceType(source.source_type).value,
        "url": source.url,
        "canonical_url": canonicalize_source_url(source.url),
        "content": content,
        "content_bytes": content_bytes,
        "status_code": fetch_result.status_code,
        "content_type": fetch_result.content_type,
        "fetched_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "fetch_policy": {
            "timeout_seconds": policy.timeout_seconds,
            "max_bytes": policy.max_bytes,
            "max_redirects": policy.max_redirects,
            "respect_robots": policy.respect_robots,
            "user_agent": policy.user_agent,
        },
    }


def _probe_source(
    args: dict[str, Any],
    *,
    default_policy: SourceFetchPolicy,
    fetch_text: FetchText | None,
    allowed_domains: tuple[str, ...],
    health_manager: BasicSourceHealthManager,
    rate_limiter: SourceRateLimiter | None,
    source_runtime: SourceToolRuntime | None,
) -> dict[str, Any]:
    source = _source_definition(args["source"], default_source_type=SourceType.RSS)
    _ensure_http_url(source.url)
    _ensure_allowed_domain(source.url, allowed_domains)
    policy = effective_source_fetch_policy(_fetch_policy(args, default_policy), source)
    blocked = _source_health_blocked_result(
        source,
        health_manager,
        respect_interval=False,
    )
    if blocked is not None:
        return blocked
    rate_limit_error = _source_rate_limit_error(source, policy, rate_limiter)
    if rate_limit_error is not None:
        return {
            "ok": False,
            "source_id": source.source_id,
            "url": source.url,
            "canonical_url": canonicalize_source_url(source.url),
            "error": rate_limit_error.to_dict(),
            "health": health_manager.get(
                source.source_id,
                source_name=source.name,
                url=source.url,
            ).to_dict(),
        }
    try:
        fetch_result = _fetch_text(
            source.url,
            policy,
            fetch_text,
            source_runtime=source_runtime,
        )
    except Exception as exc:
        error = _probe_exception_error(source, exc)
        health = health_manager.record_failure(
            source.source_id,
            error,
            source_name=source.name,
            url=source.url,
        )
        return {
            "ok": False,
            "source_id": source.source_id,
            "url": source.url,
            "canonical_url": canonicalize_source_url(source.url),
            "error": error.to_dict(),
            "health": health.to_dict(),
        }

    health = health_manager.record_success(
        source.source_id,
        source_name=source.name,
        url=source.url,
    )
    return {
        "ok": True,
        "source_id": source.source_id,
        "url": source.url,
        "canonical_url": canonicalize_source_url(source.url),
        "status_code": fetch_result.status_code,
        "content_type": fetch_result.content_type,
        "content_bytes": len(fetch_result.content.encode("utf-8")),
        "health": health.to_dict(),
    }


def _search_sources(
    args: dict[str, Any],
    *,
    source_registry: SourceRegistry,
) -> dict[str, Any]:
    enabled_only = bool(args.get("enabled_only", True))
    language = args.get("language")
    region = args.get("region")
    reliability = args.get("reliability")
    source_type = args.get("source_type")
    limit = _optional_limit(args.get("limit"))
    query = str(args.get("query") or "").strip()
    if query:
        sources = source_registry.select_sources(
            topic=query,
            enabled_only=enabled_only,
            language=str(language) if language is not None else None,
            region=str(region) if region is not None else None,
            reliability=str(reliability) if reliability is not None else None,
            fallback_to_enabled=bool(args.get("fallback_to_enabled", True)),
        )
    else:
        sources = source_registry.list_sources(enabled_only=enabled_only)
        if language is not None:
            sources = [source for source in sources if source.language == str(language)]
        if region is not None:
            sources = [source for source in sources if source.region == str(region)]
        if reliability is not None:
            expected_reliability = SourceReliability(str(reliability))
            sources = [source for source in sources if source.reliability == expected_reliability]
    if source_type is not None:
        expected_type = SourceType(str(source_type))
        sources = [source for source in sources if source.source_type == expected_type]
    if limit is not None:
        sources = sources[:limit]
    return {
        "query": query or None,
        "source_count": len(sources),
        "sources": [_source_definition_to_dict(source) for source in sources],
    }


def _fetch_policy(args: dict[str, Any], default_policy: SourceFetchPolicy) -> SourceFetchPolicy:
    """The operator policy, tightened (never loosened) by the model's arguments.

    The tool's declared timeout is derived from the operator timeout, so a
    longer model-requested budget would only get the attempt killed from
    outside; it is capped instead.
    """

    timeout_seconds = min(
        float(args.get("timeout_seconds", default_policy.timeout_seconds)),
        default_policy.timeout_seconds,
    )
    max_bytes = int(args.get("max_bytes", default_policy.max_bytes))
    max_redirects = min(
        int(args.get("max_redirects", default_policy.max_redirects)),
        default_policy.max_redirects,
    )
    respect_robots = _optional_bool(args.get("respect_robots"), default_policy.respect_robots)
    if max_bytes > 1_000_000:
        raise ValueError("max_bytes must not exceed 1000000 for source.fetch_url")
    return SourceFetchPolicy(
        timeout_seconds=timeout_seconds,
        max_bytes=min(max_bytes, default_policy.max_bytes),
        max_redirects=max_redirects,
        respect_robots=respect_robots,
        user_agent=str(args.get("user_agent") or default_policy.user_agent),
        rate_limit_per_domain_per_minute=default_policy.rate_limit_per_domain_per_minute,
        allowed_domains=default_policy.allowed_domains,
        retry_times=default_policy.retry_times,
        retry_on_status_codes=default_policy.retry_on_status_codes,
        allowed_private_networks=default_policy.allowed_private_networks,
    )


def _coerce_fetch_policy(policy: Any | None) -> SourceFetchPolicy:
    if policy is None:
        return SourceFetchPolicy()
    if isinstance(policy, SourceFetchPolicy):
        return policy
    return SourceFetchPolicy(
        timeout_seconds=float(getattr(policy, "timeout_seconds")),
        max_bytes=int(getattr(policy, "max_bytes")),
        max_redirects=int(getattr(policy, "max_redirects")),
        respect_robots=bool(getattr(policy, "respect_robots")),
        user_agent=str(getattr(policy, "user_agent")),
        rate_limit_per_domain_per_minute=getattr(policy, "rate_limit_per_domain_per_minute"),
        allowed_domains=tuple(getattr(policy, "allowed_domains", ()) or ()),
        retry_times=int(getattr(policy, "retry_times")),
        retry_on_status_codes=tuple(getattr(policy, "retry_on_status_codes")),
        allowed_private_networks=tuple(getattr(policy, "allowed_private_networks", ()) or ()),
    )


def _ensure_source_health_allows_fetch(
    source: SourceDefinition,
    health_manager: BasicSourceHealthManager,
    *,
    respect_interval: bool,
) -> None:
    if not source.enabled:
        raise ValueError(f"source fetch skipped: disabled for source {source.source_id}")
    decision = health_manager.fetch_decision(
        source.source_id,
        source_name=source.name,
        url=source.url,
        min_interval_seconds=source.fetch_interval_seconds if respect_interval else None,
    )
    if decision.should_fetch:
        return
    reason = decision.skip_reason or "skipped"
    raise ValueError(f"source fetch skipped: {reason} for source {source.source_id}")


def _source_health_blocked_result(
    source: SourceDefinition,
    health_manager: BasicSourceHealthManager,
    *,
    respect_interval: bool,
) -> dict[str, Any] | None:
    if not source.enabled:
        health = health_manager.get(source.source_id, source_name=source.name, url=source.url)
        return _probe_blocked_result(
            source,
            error_type="source_fetch_skipped",
            error_message=f"source fetch skipped: disabled for source {source.source_id}",
            health=health,
            retryable=False,
            source_health_affecting=False,
            metadata={"skip_reason": "disabled"},
        )
    decision = health_manager.fetch_decision(
        source.source_id,
        source_name=source.name,
        url=source.url,
        min_interval_seconds=source.fetch_interval_seconds if respect_interval else None,
    )
    if decision.should_fetch:
        return None
    reason = decision.skip_reason or "skipped"
    metadata: dict[str, Any] = {
        "skip_reason": reason,
        "source_health_affecting": False,
    }
    if decision.cooldown_until is not None:
        metadata["cooldown_until"] = _dt(decision.cooldown_until)
    if decision.next_fetch_at is not None:
        metadata["next_fetch_at"] = _dt(decision.next_fetch_at)
    return _probe_blocked_result(
        source,
        error_type="source_fetch_skipped",
        error_message=f"source fetch skipped: {reason} for source {source.source_id}",
        health=decision.health,
        retryable=True,
        source_health_affecting=False,
        metadata=metadata,
    )


def _probe_blocked_result(
    source: SourceDefinition,
    *,
    error_type: str,
    error_message: str,
    health,
    retryable: bool,
    source_health_affecting: bool,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    error = SourceError(
        source_id=source.source_id,
        source_name=source.name,
        error_type=error_type,
        error_message=error_message,
        url=source.url,
        retryable=retryable,
        metadata=source_error_metadata(
            SourceErrorMetadataInput(
                phase="probe",
                retryable=retryable,
                source_health_affecting=source_health_affecting,
                workflow_blocking=False,
                extra={"tool": "source.probe", **metadata},
            )
        ),
    )
    return {
        "ok": False,
        "source_id": source.source_id,
        "url": source.url,
        "canonical_url": canonicalize_source_url(source.url),
        "error": error.to_dict(),
        "health": health.to_dict(),
    }


def _probe_exception_error(source: SourceDefinition, exc: Exception) -> SourceError:
    classification = classify_source_exception(
        exc,
        phase="probe",
        extension=SourceTaxonomyExtension(invalid_config_keywords=("source url",)),
        effective_retryable=effective_source_retryable(exc),
    )
    extra: dict[str, Any] = {"tool": "source.probe"}
    attempts = getattr(exc, "source_fetch_attempts", None)
    if attempts is not None:
        extra["attempts"] = attempts
    return SourceError(
        source_id=source.source_id,
        source_name=source.name,
        error_type=classification.error_type,
        error_message=str(exc),
        url=source.url,
        retryable=classification.retryable,
        metadata=source_error_metadata(
            SourceErrorMetadataInput(
                phase="probe",
                retryable=classification.retryable,
                source_health_affecting=classification.source_health_affecting,
                workflow_blocking=classification.workflow_blocking,
                operator_action_required=classification.operator_action_required,
                original_exception_type=type(exc).__name__,
                extra=extra,
            )
        ),
    )


def _ensure_source_rate_limit_allows_fetch(
    source: SourceDefinition,
    policy: SourceFetchPolicy,
    rate_limiter: SourceRateLimiter | None,
) -> None:
    error = _source_rate_limit_error(source, policy, rate_limiter)
    if error is None:
        return
    retry_after = error.metadata.get("retry_after_seconds")
    suffix = f"; retry_after_seconds={retry_after}" if retry_after is not None else ""
    raise ValueError(f"{error.error_type}: {error.error_message}{suffix}")


def _source_rate_limit_error(
    source: SourceDefinition,
    policy: SourceFetchPolicy,
    rate_limiter: SourceRateLimiter | None,
) -> SourceError | None:
    if policy.rate_limit_per_domain_per_minute is None:
        return None
    if rate_limiter is None:
        raise RuntimeError("source rate limiter adapter is required when rate limiting is enabled")
    decision = rate_limiter.reserve(
        source.url,
        limit_per_minute=policy.rate_limit_per_domain_per_minute,
    )
    if decision.allowed:
        return None
    return source_rate_limited_error(source, decision, url=source.url)


def _fetch_text(
    url: str,
    policy: SourceFetchPolicy,
    fetch_text: FetchText | None,
    *,
    source_runtime: SourceToolRuntime | None,
) -> SourceTextFetchResult:
    if source_runtime is not None:
        return source_runtime.fetch_text(url, policy)
    if fetch_text is not None:
        return _call_fetch_text(fetch_text, url, policy)
    raise RuntimeError("source.fetch_url requires a source tool runtime adapter")


def _call_fetch_text(fetch_text: FetchText, url: str, policy: SourceFetchPolicy) -> SourceTextFetchResult:
    content = fetch_text(url)
    if len(content.encode("utf-8")) > policy.max_bytes:
        raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
    return SourceTextFetchResult(content=content)


def _require_source_runtime(
    source_runtime: SourceToolRuntime | None,
    tool_name: str,
) -> SourceToolRuntime:
    if source_runtime is None:
        raise RuntimeError(f"{tool_name} requires a source tool runtime adapter")
    return source_runtime


def _ensure_http_url(url: str) -> None:
    scheme = urlsplit(url).scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError(f"source.fetch_url only supports http and https URLs: {url}")


def _allowed_domains(allowed_domains: list[str] | None) -> tuple[str, ...]:
    return tuple(
        domain.strip().casefold().lstrip(".")
        for domain in allowed_domains or []
        if domain.strip()
    )


def _ensure_allowed_domain(url: str, allowed_domains: tuple[str, ...]) -> None:
    if not allowed_domains:
        return
    host = (urlsplit(url).hostname or "").casefold()
    if any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
        return
    raise ValueError(f"source.fetch_url host is not in allowed domains: {host}")


def _official_blog_source(
    args: dict[str, Any],
    *,
    source_registry: SourceRegistry | None,
) -> SourceDefinition:
    source_payload = args.get("source")
    if source_payload is not None:
        return _source_definition(source_payload, default_source_type=SourceType.RSS)
    source_id = args.get("source_id")
    if source_id:
        if source_registry is None:
            raise ValueError("source_registry is required to resolve source_id")
        return source_registry.get(str(source_id))
    topic = str(args.get("topic") or "").strip()
    if topic:
        if source_registry is None:
            raise ValueError("source_registry is required to resolve topic")
        sources = source_registry.select_sources(
            topic=topic,
            enabled_only=True,
            language=str(args["language"]) if args.get("language") is not None else None,
            region=str(args["region"]) if args.get("region") is not None else None,
            fallback_to_enabled=True,
        )
        for source in sources:
            if _is_official_blog(source):
                return source
        raise ValueError(f"no official blog source matched topic: {topic}")
    raise ValueError("source, source_id, or topic is required")


def _ensure_official_blog(source: SourceDefinition) -> None:
    if not _is_official_blog(source):
        raise ValueError(f"source is not marked as an official blog: {source.source_id}")


def _is_official_blog(source: SourceDefinition) -> bool:
    if source.source_type == SourceType.OFFICIAL_BLOG:
        return True
    metadata = source.metadata
    if _truthy(metadata.get("official_blog")) or _truthy(metadata.get("official")):
        return True
    for key in ["source_kind", "kind", "category"]:
        marker = str(metadata.get(key) or "").casefold().replace("-", "_").replace(" ", "_")
        if marker == "official_blog":
            return True
    return False


def _ensure_official_blog_source_type(source: SourceDefinition) -> None:
    if source.source_type not in {
        SourceType.RSS,
        SourceType.ATOM,
        SourceType.HTML,
        SourceType.OFFICIAL_BLOG,
    }:
        raise ValueError(
            f"official blog source must be rss, atom, html, or official_blog: {source.source_id}"
        )


def _official_blog_error_result(
    source: SourceDefinition,
    errors: list[SourceError],
) -> dict[str, Any]:
    return {
        "source": _source_definition_to_dict(source),
        "item_count": 0,
        "items": [],
        "error_count": len(errors),
        "errors": [error.to_dict() for error in errors],
    }



def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _is_html_backed_source_type(source_type: SourceType | str) -> bool:
    return SourceType(source_type) in {SourceType.HTML, SourceType.OFFICIAL_BLOG, SourceType.WEB_PAGE}


def _optional_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return _truthy(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_definition(payload: Any, *, default_source_type: SourceType) -> SourceDefinition:
    if not isinstance(payload, dict):
        raise ValueError("source must be an object")
    return SourceDefinition(
        source_id=str(payload.get("source_id") or ""),
        name=str(payload.get("name") or ""),
        source_type=str(payload.get("source_type") or default_source_type.value),
        url=str(payload.get("url") or ""),
        reliability=str(payload.get("reliability") or "medium"),
        authority_score=float(payload.get("authority_score", 0.5)),
        enabled=_optional_bool(payload.get("enabled"), True),
        fetch_interval_seconds=int(payload.get("fetch_interval_seconds") or 3600),
        respect_robots=_optional_bool(payload.get("respect_robots"), True),
        user_agent=_optional_text(payload.get("user_agent")),
        topics=[str(topic) for topic in payload.get("topics", []) if str(topic).strip()]
        if isinstance(payload.get("topics"), list)
        else [],
        category=_optional_text(payload.get("category")),
        language=payload.get("language"),
        region=payload.get("region"),
        metadata=dict(payload.get("metadata") or {}),
    )


def _source_id(args: dict[str, Any]) -> str:
    source_id = args.get("source_id")
    if source_id:
        return str(source_id)
    source = args.get("source")
    if isinstance(source, dict) and source.get("source_id"):
        return str(source["source_id"])
    raise ValueError("source_id or source.source_id is required")


def _source_health(args: dict[str, Any], *, health_manager: BasicSourceHealthManager):
    source = args.get("source")
    if isinstance(source, dict):
        source_id = str(source.get("source_id") or "")
        if not source_id:
            raise ValueError("source.source_id is required")
        return health_manager.get(
            source_id,
            source_name=_optional_text(source.get("name")),
            url=_optional_text(source.get("url")),
        )
    return health_manager.get(_source_id(args))


def _optional_limit(value: Any) -> int | None:
    if value is None:
        return None
    return max(1, min(int(value), 100))


def _source_definition_to_dict(source: SourceDefinition) -> dict[str, Any]:
    return {
        "source_id": source.source_id,
        "name": source.name,
        "source_type": SourceType(source.source_type).value,
        "url": source.url,
        "reliability": SourceReliability(source.reliability).value,
        "authority_score": source.authority_score,
        "enabled": source.enabled,
        "fetch_interval_seconds": source.fetch_interval_seconds,
        "respect_robots": source.respect_robots,
        "user_agent": source.user_agent,
        "topics": list(source.topics),
        "category": source.category,
        "language": source.language,
        "region": source.region,
        "metadata": dict(source.metadata),
    }


def _raw_source_item_to_dict(item: RawSourceItem) -> dict[str, Any]:
    return item.to_dict()


def _dt(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None
