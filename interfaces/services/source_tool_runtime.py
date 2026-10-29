from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlsplit
from urllib.request import Request

from backend.foundation.models.source import (
    RawSourceItem,
    SourceDefinition,
    SourceError,
    SourceFetchPolicy as BusinessSourceFetchPolicy,
)
from backend.layers.signal.source_tool_runtime import (
    FetchText,
    SourceTextFetchResult,
)
from infrastructure.external.sources import (
    DomainRateLimiter,
    FeedConnector,
    HtmlConnector,
    ManualConnector,
)
from infrastructure.external.sources.fetch_policy import (
    OpenRequest,
    SourceFetchPolicy as InfraSourceFetchPolicy,
    ensure_robots_allowed,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
)
from infrastructure.external.sources.models import (
    SourceDefinition as InfraSourceDefinition,
    SourceType as InfraSourceType,
)
from interfaces.services.source_mapping import (
    to_business_raw_source_item as _business_raw_source_item,
    to_business_source_error as _business_source_error,
    to_infrastructure_fetch_policy as _infra_fetch_policy,
    to_infrastructure_source_definition as _infra_source,
)


class InfrastructureSourceToolRuntime:
    def __init__(
        self,
        *,
        fetch_text: FetchText | None = None,
        open_request: OpenRequest | None = None,
        rate_limiter: DomainRateLimiter | None = None,
    ) -> None:
        self._fetch_text = fetch_text
        self._open_request = open_request
        self._rate_limiter = rate_limiter or DomainRateLimiter()

    def fetch_text(
        self,
        url: str,
        policy: BusinessSourceFetchPolicy,
    ) -> SourceTextFetchResult:
        _ensure_http_url(url)
        infra_policy = _infra_fetch_policy(policy)
        if self._fetch_text is not None:
            return run_with_fetch_retries(
                lambda: _fetch_with_callable(self._fetch_text, url, infra_policy),
                infra_policy,
            )
        open_request = self._resolved_open_request()
        return run_with_fetch_retries(
            lambda: _default_fetch_text(url, infra_policy, open_request),
            infra_policy,
        )

    def parse_feed(
        self,
        source: SourceDefinition,
        content: str,
        *,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        items = FeedConnector().parse(_infra_source(source), content, limit=limit)
        return [_business_raw_source_item(item) for item in items]

    def parse_html(
        self,
        source: SourceDefinition,
        content: str,
        *,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        items = HtmlConnector().parse(_infra_source(source), content, limit=limit)
        return [_business_raw_source_item(item) for item in items]

    def fetch_manual(
        self,
        source: SourceDefinition,
        *,
        records: Sequence[dict[str, object]],
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        items, errors = ManualConnector().fetch(
            _infra_source(source),
            records=records,
            limit=limit,
        )
        return (
            [_business_raw_source_item(item) for item in items],
            [_business_source_error(error) for error in errors],
        )

    def fetch_official_blog(
        self,
        source: SourceDefinition,
        *,
        policy: BusinessSourceFetchPolicy,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        infra_source = _infra_source(source)
        infra_policy = _infra_fetch_policy(policy)
        connector = _official_blog_connector(
            infra_source,
            fetch_text=self._fetch_text,
            open_request=self._resolved_open_request(),
            policy=infra_policy,
            rate_limiter=self._rate_limiter,
        )
        items, errors = connector.fetch(infra_source, limit=limit)
        return (
            [_business_raw_source_item(item) for item in items],
            [_business_source_error(error) for error in errors],
        )

    def _resolved_open_request(self) -> OpenRequest:
        return self._open_request or open_request_with_fetch_policy


def default_source_tool_runtime(
    *,
    fetch_text: FetchText | None = None,
    open_request: OpenRequest | None = None,
    rate_limiter: DomainRateLimiter | None = None,
) -> InfrastructureSourceToolRuntime:
    return InfrastructureSourceToolRuntime(
        fetch_text=fetch_text,
        open_request=open_request,
        rate_limiter=rate_limiter,
    )


def _fetch_with_callable(
    fetch_text: FetchText,
    url: str,
    policy: InfraSourceFetchPolicy,
) -> SourceTextFetchResult:
    content = fetch_text(url)
    if len(content.encode("utf-8")) > policy.max_bytes:
        raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
    return SourceTextFetchResult(content=content)


def _default_fetch_text(
    url: str,
    policy: InfraSourceFetchPolicy,
    open_request: OpenRequest,
) -> SourceTextFetchResult:
    ensure_robots_allowed(url, policy, open_request=open_request)
    request = Request(url, headers={"User-Agent": policy.user_agent})
    with open_request(request, policy) as response:
        body = response.read(policy.max_bytes + 1)
        status_code = getattr(response, "status", None)
        headers = getattr(response, "headers", None)
        content_type = headers.get_content_type() if headers is not None else None
    if len(body) > policy.max_bytes:
        raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
    return SourceTextFetchResult(
        content=body.decode("utf-8", errors="replace"),
        status_code=int(status_code) if status_code is not None else None,
        content_type=content_type,
    )


def _official_blog_connector(
    source: InfraSourceDefinition,
    *,
    fetch_text: FetchText | None,
    open_request: OpenRequest,
    policy: InfraSourceFetchPolicy,
    rate_limiter: DomainRateLimiter,
) -> FeedConnector | HtmlConnector:
    connector_type = HtmlConnector if _is_html_backed_source_type(source.source_type) else FeedConnector
    return connector_type(
        fetch_text=_policy_fetch_text(fetch_text, policy),
        fetch_policy=policy,
        rate_limiter=rate_limiter,
        open_request=open_request,
    )


def _policy_fetch_text(
    fetch_text: FetchText | None,
    policy: InfraSourceFetchPolicy,
) -> FetchText | None:
    if fetch_text is None:
        return None

    def wrapped(url: str) -> str:
        content = fetch_text(url)
        if len(content.encode("utf-8")) > policy.max_bytes:
            raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
        return content

    return wrapped


def _is_html_backed_source_type(source_type: InfraSourceType | str) -> bool:
    return InfraSourceType(source_type) in {
        InfraSourceType.HTML,
        InfraSourceType.OFFICIAL_BLOG,
        InfraSourceType.WEB_PAGE,
    }


def _ensure_http_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("source URL must use http or https")


__all__ = [
    "InfrastructureSourceToolRuntime",
    "default_source_tool_runtime",
]
