from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from hashlib import sha256
from typing import Callable
from urllib.parse import quote, urlsplit
from urllib.parse import urlencode
from urllib.request import Request
from xml.etree import ElementTree

from infrastructure.external.fetch_deadline import remaining_seconds
from infrastructure.external.sources.models import RawSourceItem, SourceDefinition, SourceError
from infrastructure.external.sources.diagnostics import (
    SourceFetchResponseMetadataContext,
    SourceFetchResponseMetadata,
    attach_response_metadata_to_error,
    attach_response_metadata_to_items,
    response_metadata_from_http_response,
    source_fetch_response_scope,
)
from infrastructure.external.sources.fetch_policy import (
    DomainRateLimiter,
    RateLimitDecision,
    SourceRateLimitExceededError,
    SourceFetchPolicy,
    effective_fetch_policy,
    ensure_robots_allowed,
    ensure_supported_content_type,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
)
from infrastructure.external.sources.metadata import source_item_metadata
from infrastructure.external.sources.errors import (
    SourceErrorContext,
    SourceTaxonomyExtension,
    build_source_error,
    rate_limited_source_error,
    source_error_from_exception,
)


FetchText = Callable[[str], str]
ARXIV_API_URL = "https://export.arxiv.org/api/query"
# arXiv's API terms ask clients for at most one request every three seconds.
# Its robots.txt disallows every path for crawlers; the API is governed by
# those terms instead (https://info.arxiv.org/help/api/tou.html).
ARXIV_API_HOSTS = frozenset({"export.arxiv.org", "arxiv.org"})
ARXIV_MIN_REQUEST_INTERVAL_SECONDS = 3.0
# A call queued behind more than this many slots fails as rate-limited
# instead of spending its whole time budget waiting.
ARXIV_MAX_PACING_WAIT_SECONDS = 9.0


class ArxivRequestPacer:
    """Spaces requests to one arXiv host ``min_interval`` seconds apart, in arrival order.

    Each caller reserves the next free slot, then sleeps until it. The wait
    is spent inside the caller's fetch budget, so a slot further off than
    ``max_wait_seconds`` or than the budget left is refused (and not
    reserved): a burst of calls turns into rate-limited errors rather than
    calls that time out queued.
    """

    def __init__(
        self,
        *,
        min_interval_seconds: float = ARXIV_MIN_REQUEST_INTERVAL_SECONDS,
        max_wait_seconds: float = ARXIV_MAX_PACING_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_seconds = float(min_interval_seconds)
        self.max_wait_seconds = float(max_wait_seconds)
        self._clock = clock
        self._sleep = sleep
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait_turn(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").casefold()
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot.get(host, now))
            wait = slot - now
            remaining = remaining_seconds()
            if wait > self.max_wait_seconds or (remaining is not None and wait >= remaining):
                raise SourceRateLimitExceededError(
                    url,
                    RateLimitDecision(
                        allowed=False,
                        domain=host,
                        limit_per_minute=int(60 / self.min_interval_seconds),
                        retry_after_seconds=max(1, int(wait + 0.999)),
                    ),
                )
            self._next_slot[host] = slot + self.min_interval_seconds
        if wait > 0:
            self._sleep(wait)


# One pacer per process: every connector instance talks to the same arXiv.
DEFAULT_ARXIV_PACER = ArxivRequestPacer()


def is_arxiv_api_url(url: str) -> bool:
    return (urlsplit(url).hostname or "").casefold() in ARXIV_API_HOSTS


ARXIV_NAMESPACE = "http://arxiv.org/schemas/atom"
ARXIV_CONTENT_TYPES = (
    "application/atom+xml",
    "application/xml",
    "text/xml",
)
ARXIV_SOURCE_CONTENT_TYPES = (
    "application/gzip",
    "application/x-gzip",
    "application/octet-stream",
    "application/x-tar",
    "application/x-eprint-tar",
    "application/pdf",
)
ARXIV_TAXONOMY_EXTENSION = SourceTaxonomyExtension(
    invalid_config_keywords=("query",),
)
ARXIV_PDF_CONTENT_TYPES = (
    "application/pdf",
    "application/octet-stream",
)
DEFAULT_ARXIV_SOURCE_MAX_BYTES = 120_000_000


@dataclass(frozen=True)
class ArxivQuery:
    query: str
    start: int = 0
    max_results: int = 10
    sort_by: str = "submittedDate"
    sort_order: str = "descending"

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("arxiv query is required")
        if self.start < 0:
            raise ValueError("start must be non-negative")
        if self.max_results <= 0:
            raise ValueError("max_results must be greater than zero")


@dataclass(frozen=True)
class ArxivSourcePackage:
    arxiv_id: str
    url: str
    content: bytes
    content_type: str | None = None
    file_name: str | None = None
    fetched_at: datetime | None = None
    response_metadata: SourceFetchResponseMetadata | None = None

    @property
    def checksum(self) -> str:
        return sha256(self.content).hexdigest()


class ArxivConnector:
    def __init__(
        self,
        fetch_text: FetchText | None = None,
        *,
        fetch_policy: SourceFetchPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
        pacer: ArxivRequestPacer | None = None,
    ) -> None:
        self.fetch_policy = fetch_policy or SourceFetchPolicy()
        self._rate_limiter = rate_limiter or DomainRateLimiter()
        self.pacer = pacer or DEFAULT_ARXIV_PACER
        self._uses_default_fetch = fetch_text is None
        self._fetch_text = fetch_text or self._default_fetch_text
        self._response_metadata_context = SourceFetchResponseMetadataContext()

    @source_fetch_response_scope
    def fetch(
        self,
        source: SourceDefinition,
        *,
        query: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            actual_query = _query_from_source(source, query=query)
            request = ArxivQuery(query=actual_query, max_results=limit or 10)
            api_url = build_arxiv_query_url(source.url or ARXIV_API_URL, request)
            rate_limit = self._rate_limiter.reserve(
                api_url,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return [], [rate_limited_source_error(source, rate_limit, url=api_url)]
            xml_text = run_with_fetch_retries(
                lambda: self._fetch_source_text(api_url, policy),
                policy,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="fetch")
            return [], [attach_response_metadata_to_error(error, self._response_metadata_context.get())]
        response_metadata = self._response_metadata_context.get()

        if not xml_text.strip():
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_source_response",
                        "arXiv API returned an empty response",
                        context=SourceErrorContext(phase="fetch"),
                        retryable=True,
                        source_health_affecting=True,
                    ),
                    response_metadata,
                )
            ]

        try:
            items = self.parse(source, xml_text, limit=limit)
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="parse")
            return [], [attach_response_metadata_to_error(error, response_metadata)]
        items = attach_response_metadata_to_items(items, response_metadata)

        if not items:
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_arxiv_feed",
                        "arXiv feed contained no paper entries",
                        context=SourceErrorContext(phase="parse"),
                        retryable=False,
                        source_health_affecting=False,
                    ),
                    response_metadata,
                )
            ]
        return items, []

    def parse(
        self,
        source: SourceDefinition,
        xml_text: str,
        *,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        root = ElementTree.fromstring(xml_text)
        if _local_name(root.tag) != "feed":
            raise ValueError(f"unsupported arXiv feed root: {_local_name(root.tag)}")
        fetched_at = datetime.now(UTC)
        items = [_raw_item_from_entry(source, entry, fetched_at) for entry in _children(root, "entry")]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def _default_fetch_text(self, url: str, policy: SourceFetchPolicy | None = None) -> str:
        policy = policy or self.fetch_policy
        request = Request(url, headers={"User-Agent": policy.user_agent})
        with open_request_with_fetch_policy(request, policy) as response:
            response_metadata = response_metadata_from_http_response(response, url=url)
            self._response_metadata_context.set(response_metadata)
            ensure_supported_content_type(response_metadata.content_type, ARXIV_CONTENT_TYPES)
            body = response.read(policy.max_bytes + 1)
        if len(body) > policy.max_bytes:
            raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
        return body.decode("utf-8", errors="replace")

    def _fetch_source_text(self, url: str, policy: SourceFetchPolicy) -> str:
        if not self._uses_default_fetch:
            # An injected transport owns its own pacing and access rules.
            return self._fetch_text(url)
        if is_arxiv_api_url(url):
            # Every attempt, retries included, takes its own paced slot.
            self.pacer.wait_turn(url)
            policy = replace(policy, respect_robots=False)
        ensure_robots_allowed(url, policy)
        return self._default_fetch_text(url, policy)


class ArxivSourceConnector:
    """Fetch arXiv TeX source packages from the official e-print endpoint."""

    @staticmethod
    def default_fetch_policy() -> SourceFetchPolicy:
        return SourceFetchPolicy(
            timeout_seconds=90,
            max_bytes=DEFAULT_ARXIV_SOURCE_MAX_BYTES,
            user_agent="AgoraHub/0.1 (arxiv-source-fetch; contact: local-dev)",
            respect_robots=False,
            retry_times=1,
        )

    def __init__(
        self,
        *,
        fetch_policy: SourceFetchPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
    ) -> None:
        self.fetch_policy = fetch_policy or self.default_fetch_policy()
        self._rate_limiter = rate_limiter or DomainRateLimiter()

    def fetch_source_package(
        self,
        arxiv_id: str,
        *,
        max_bytes: int | None = None,
    ) -> ArxivSourcePackage:
        normalized_id = normalize_arxiv_id(arxiv_id)
        if not normalized_id:
            raise ValueError("arxiv id is required")
        url = build_arxiv_source_url(normalized_id)
        policy = self.fetch_policy
        if max_bytes is not None and max_bytes > 0 and max_bytes != policy.max_bytes:
            from dataclasses import replace

            policy = replace(policy, max_bytes=max_bytes)
        rate_limit = self._rate_limiter.reserve(
            url,
            limit_per_minute=policy.rate_limit_per_domain_per_minute,
        )
        if not rate_limit.allowed:
            raise SourceRateLimitExceededError(url, rate_limit)

        def operation() -> ArxivSourcePackage:
            ensure_robots_allowed(url, policy)
            request = Request(
                url,
                headers={
                    "Accept": "application/gzip,application/x-gzip,application/octet-stream,*/*",
                    "User-Agent": policy.user_agent,
                },
            )
            with open_request_with_fetch_policy(request, policy) as response:
                response_metadata = response_metadata_from_http_response(response, url=url)
                ensure_supported_content_type(response_metadata.content_type, ARXIV_SOURCE_CONTENT_TYPES)
                content_length = response.headers.get("Content-Length") if response.headers is not None else None
                if content_length and int(content_length) > policy.max_bytes:
                    raise ValueError("arXiv source package exceeds configured maximum size")
                body = response.read(policy.max_bytes + 1)
            if len(body) > policy.max_bytes:
                raise ValueError("arXiv source package exceeds configured maximum size")
            if not body:
                raise ValueError("arXiv source package is empty")
            return ArxivSourcePackage(
                arxiv_id=normalized_id,
                url=response_metadata.url or url,
                content=body,
                content_type=response_metadata.content_type,
                file_name=_content_disposition_file_name((response_metadata.headers or {}).get("Content-Disposition")),
                fetched_at=datetime.now(UTC),
                response_metadata=response_metadata,
            )

        return run_with_fetch_retries(operation, policy)

    def fetch_pdf_package(
        self,
        arxiv_id: str,
        *,
        max_bytes: int | None = None,
    ) -> ArxivSourcePackage:
        normalized_id = normalize_arxiv_id(arxiv_id)
        if not normalized_id:
            raise ValueError("arxiv id is required")
        url = build_arxiv_pdf_url(normalized_id)
        policy = self.fetch_policy
        if max_bytes is not None and max_bytes > 0 and max_bytes != policy.max_bytes:
            from dataclasses import replace

            policy = replace(policy, max_bytes=max_bytes)
        rate_limit = self._rate_limiter.reserve(
            url,
            limit_per_minute=policy.rate_limit_per_domain_per_minute,
        )
        if not rate_limit.allowed:
            raise SourceRateLimitExceededError(url, rate_limit)

        def operation() -> ArxivSourcePackage:
            ensure_robots_allowed(url, policy)
            request = Request(
                url,
                headers={
                    "Accept": "application/pdf,application/octet-stream,*/*",
                    "User-Agent": policy.user_agent,
                },
            )
            with open_request_with_fetch_policy(request, policy) as response:
                response_metadata = response_metadata_from_http_response(response, url=url)
                ensure_supported_content_type(response_metadata.content_type, ARXIV_PDF_CONTENT_TYPES)
                content_length = response.headers.get("Content-Length") if response.headers is not None else None
                if content_length and int(content_length) > policy.max_bytes:
                    raise ValueError("arXiv PDF exceeds configured maximum size")
                body = response.read(policy.max_bytes + 1)
            if len(body) > policy.max_bytes:
                raise ValueError("arXiv PDF exceeds configured maximum size")
            if not body.startswith(b"%PDF"):
                raise ValueError("arXiv PDF response did not contain a PDF document")
            return ArxivSourcePackage(
                arxiv_id=normalized_id,
                url=response_metadata.url or url,
                content=body,
                content_type=response_metadata.content_type,
                file_name=(
                    _content_disposition_file_name((response_metadata.headers or {}).get("Content-Disposition"))
                    or f"{normalized_id.replace('/', '_')}.pdf"
                ),
                fetched_at=datetime.now(UTC),
                response_metadata=response_metadata,
            )

        return run_with_fetch_retries(operation, policy)


def build_arxiv_query_url(base_url: str, query: ArxivQuery) -> str:
    params = {
        "search_query": query.query,
        "start": query.start,
        "max_results": query.max_results,
        "sortBy": query.sort_by,
        "sortOrder": query.sort_order,
    }
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode(params)}"


def build_arxiv_source_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/e-print/{quote(normalize_arxiv_id(arxiv_id), safe='.')}"


def build_arxiv_src_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/src/{quote(normalize_arxiv_id(arxiv_id), safe='.')}"


def build_arxiv_pdf_url(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{quote(normalize_arxiv_id(arxiv_id), safe='/.')}"


def normalize_arxiv_id(value: str) -> str:
    text = value.strip().rstrip(".,;:)]}>'\"")
    marker_found = False
    for marker in ("arxiv.org/abs/", "arxiv.org/pdf/", "arxiv.org/e-print/", "arxiv.org/src/"):
        if marker in text:
            text = text.rsplit(marker, 1)[-1]
            marker_found = True
            break
    if "://" in text and not marker_found:
        return ""
    if text.endswith(".pdf"):
        text = text[:-4]
    return text.split("?", 1)[0].split("#", 1)[0].strip("/")


def _raw_item_from_entry(
    source: SourceDefinition,
    entry: ElementTree.Element,
    fetched_at: datetime,
) -> RawSourceItem | None:
    title = _normalize_text(_child_text(entry, "title"))
    url = _entry_url(entry)
    if not title or not url:
        return None
    arxiv_id = _child_text(entry, "id") or url
    categories = _category_terms(entry)
    item_hash = sha256(f"{source.source_id}|{arxiv_id}".encode("utf-8")).hexdigest()
    arxiv_item_id = arxiv_id.rsplit("/", 1)[-1]
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=title,
        url=url,
        fetched_at=fetched_at,
        published_at=_parse_datetime(_child_text(entry, "published") or _child_text(entry, "updated")),
        summary=_normalize_text(_child_text(entry, "summary")),
        raw_content=ElementTree.tostring(entry, encoding="unicode"),
        authors=[name for name in (_child_text(author, "name") for author in _children(entry, "author")) if name],
        tags=categories,
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "arxiv_id": arxiv_item_id,
                "pdf_url": _entry_pdf_url(entry, arxiv_item_id),
                "primary_category": _primary_category(entry),
                "doi": _arxiv_child_text(entry, "doi"),
                "journal_ref": _arxiv_child_text(entry, "journal_ref"),
                "comment": _arxiv_child_text(entry, "comment"),
            },
        ),
    )


def _query_from_source(source: SourceDefinition, *, query: str | None) -> str:
    if query and query.strip():
        return query.strip()
    legacy_query = _legacy_arxiv_query(source)
    if legacy_query:
        return legacy_query
    raise ValueError("arxiv query is required")


def _legacy_arxiv_query(source: SourceDefinition) -> str | None:
    query = source.metadata.get("query")
    if isinstance(query, str) and query.strip():
        return query.strip()
    return None


def _entry_url(entry: ElementTree.Element) -> str | None:
    for child in _children(entry, "link"):
        href = child.attrib.get("href")
        if href and child.attrib.get("rel", "alternate") == "alternate":
            return href.strip()
    return _child_text(entry, "id")


def _entry_pdf_url(entry: ElementTree.Element, arxiv_id: str) -> str:
    for child in _children(entry, "link"):
        href = child.attrib.get("href")
        title = child.attrib.get("title", "")
        content_type = child.attrib.get("type", "")
        if href and (title.casefold() == "pdf" or content_type == "application/pdf"):
            return _normalize_arxiv_pdf_url(href.strip())
    return f"https://arxiv.org/pdf/{arxiv_id}.pdf"


def _normalize_arxiv_pdf_url(url: str) -> str:
    normalized = url.replace("http://", "https://", 1)
    return normalized if normalized.endswith(".pdf") else f"{normalized}.pdf"


def _category_terms(entry: ElementTree.Element) -> list[str]:
    terms = []
    for child in _children(entry, "category"):
        term = child.attrib.get("term")
        if term:
            terms.append(term)
    return terms


def _primary_category(entry: ElementTree.Element) -> str | None:
    for child in list(entry):
        if child.tag == f"{{{ARXIV_NAMESPACE}}}primary_category":
            return child.attrib.get("term")
    return None


def _exception_source_error(source: SourceDefinition, exc: Exception, *, phase: str) -> SourceError:
    return source_error_from_exception(
        source,
        exc,
        context=SourceErrorContext(phase=phase),
        extension=ARXIV_TAXONOMY_EXTENSION,
    )


def _children(parent: ElementTree.Element, local_name: str) -> list[ElementTree.Element]:
    return [child for child in list(parent) if _local_name(child.tag) == local_name]


def _child_text(parent: ElementTree.Element, local_name: str) -> str | None:
    for child in _children(parent, local_name):
        return child.text.strip() if child.text else None
    return None


def _arxiv_child_text(parent: ElementTree.Element, local_name: str) -> str | None:
    for child in list(parent):
        if child.tag == f"{{{ARXIV_NAMESPACE}}}{local_name}" and child.text:
            return _normalize_text(child.text)
    return None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _normalize_text(value: str | None) -> str | None:
    if not value:
        return None
    return " ".join(value.split())


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _content_disposition_file_name(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()
