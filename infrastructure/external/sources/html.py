from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from typing import Callable
from urllib.request import Request

from infrastructure.external.sources.models import RawSourceItem, SourceDefinition, SourceError
from infrastructure.external.sources.diagnostics import (
    SourceFetchResponseMetadataContext,
    attach_response_metadata_to_error,
    attach_response_metadata_to_items,
    response_metadata_from_http_response,
    source_fetch_response_scope,
)
from infrastructure.external.sources.fetch_policy import (
    DomainRateLimiter,
    OpenRequest,
    SourceFetchPolicy,
    effective_fetch_policy,
    ensure_robots_allowed,
    ensure_supported_content_type,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
)
from infrastructure.external.sources.metadata import source_item_metadata
from infrastructure.external.sources.url_utils import canonicalize_url
from infrastructure.external.sources.errors import (
    SourceErrorContext,
    build_source_error,
    rate_limited_source_error,
    source_error_from_exception,
)

FetchText = Callable[[str], str]
HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


EXTRACTOR_NAME = "stdlib_html_extractor"
TRAFILATURA_EXTRACTOR_NAME = "trafilatura_extractor"
SKIP_TEXT_TAGS = {"script", "style", "noscript", "svg"}
LOW_VALUE_TEXT_TAGS = {"nav", "header", "footer", "aside", "form"}
PUBLISHED_META_KEYS = {
    "article:published_time",
    "date",
    "dc.date",
    "dc.date.issued",
    "pubdate",
    "publishdate",
    "publish_date",
}
AUTHOR_META_KEYS = {"author", "article:author", "dc.creator"}


@dataclass(frozen=True)
class HtmlExtractionResult:
    title: str | None
    text: str | None
    summary: str | None
    published_at: datetime | None
    authors: list[str]
    canonical_url: str | None
    language: str | None
    confidence: float
    extractor_name: str = EXTRACTOR_NAME
    attempted_extractors: tuple[str, ...] = (EXTRACTOR_NAME,)

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "text": self.text,
            "summary": self.summary,
            "published_at": _dt(self.published_at),
            "authors": list(self.authors),
            "canonical_url": self.canonical_url,
            "language": self.language,
            "confidence": self.confidence,
            "extractor_name": self.extractor_name,
            "attempted_extractors": list(self.attempted_extractors),
        }


class HtmlConnector:
    def __init__(
        self,
        fetch_text: FetchText | None = None,
        *,
        fetch_policy: SourceFetchPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
        open_request: OpenRequest | None = None,
    ) -> None:
        self.fetch_policy = fetch_policy or SourceFetchPolicy()
        self._rate_limiter = rate_limiter or DomainRateLimiter()
        self._open_request = open_request
        self._uses_default_fetch = fetch_text is None
        self._fetch_text = fetch_text or self._default_fetch_text
        self._response_metadata_context = SourceFetchResponseMetadataContext()

    @source_fetch_response_scope
    def fetch(
        self,
        source: SourceDefinition,
        *,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        rate_limit = self._rate_limiter.reserve(
            source.url,
            limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
        )
        if not rate_limit.allowed:
            return [], [rate_limited_source_error(source, rate_limit, url=source.url)]

        try:
            html_text = run_with_fetch_retries(
                lambda: self._fetch_source_text(source.url, policy),
                policy,
            )
        except Exception as exc:
            error = source_error_from_exception(
                source,
                exc,
                context=SourceErrorContext(phase="fetch"),
            )
            return [], [attach_response_metadata_to_error(error, self._response_metadata_context.get())]
        response_metadata = self._response_metadata_context.get()

        if not html_text.strip():
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_source_response",
                        "HTML source returned an empty response",
                        context=SourceErrorContext(phase="fetch"),
                        retryable=True,
                        source_health_affecting=True,
                    ),
                    response_metadata,
                )
            ]

        try:
            items = self.parse(source, html_text, limit=limit)
        except Exception as exc:
            error = source_error_from_exception(
                source,
                exc,
                context=SourceErrorContext(phase="parse"),
            )
            return [], [attach_response_metadata_to_error(error, response_metadata)]
        items = attach_response_metadata_to_items(items, response_metadata)

        if not items:
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_html_extraction",
                        "HTML source did not contain extractable text",
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
        html_text: str,
        *,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        extraction = extract_html(html_text)
        if not extraction.text and not extraction.title:
            return []
        fetched_at = datetime.now(UTC)
        try:
            url = canonicalize_url(extraction.canonical_url or source.url, base_url=source.url)
        except (TypeError, ValueError) as exc:
            raise ValueError("HTML canonical URL is invalid") from exc
        title = extraction.title or source.name
        item_hash = sha256(f"{source.source_id}|{url}".encode("utf-8")).hexdigest()
        item = RawSourceItem(
            source_item_id=f"raw_{item_hash[:16]}",
            source_id=source.source_id,
            source_name=source.name,
            source_type=source.source_type,
            title=title,
            url=url,
            fetched_at=fetched_at,
            published_at=extraction.published_at,
            summary=extraction.summary,
            raw_content=extraction.text,
            authors=extraction.authors,
            language=extraction.language or source.language,
            metadata=source_item_metadata(
                source,
                extra={
                    "extractor_name": extraction.extractor_name,
                    "attempted_extractors": list(extraction.attempted_extractors),
                    "extraction_confidence": extraction.confidence,
                    "canonical_url": url,
                    "raw_html_bytes": len(html_text.encode("utf-8")),
                },
            ),
        )
        items = [item]
        return items[:limit] if limit else items

    def _default_fetch_text(self, url: str, policy: SourceFetchPolicy | None = None) -> str:
        policy = policy or self.fetch_policy
        request = Request(url, headers={"User-Agent": policy.user_agent})
        with self._resolved_open_request()(request, policy) as response:
            response_metadata = response_metadata_from_http_response(response, url=url)
            self._response_metadata_context.set(response_metadata)
            ensure_supported_content_type(response_metadata.content_type, HTML_CONTENT_TYPES)
            body = response.read(policy.max_bytes + 1)
        if len(body) > policy.max_bytes:
            raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
        return body.decode("utf-8", errors="replace")

    def _fetch_source_text(self, url: str, policy: SourceFetchPolicy) -> str:
        if self._uses_default_fetch:
            ensure_robots_allowed(url, policy, open_request=self._resolved_open_request())
            return self._default_fetch_text(url, policy)
        return self._fetch_text(url)

    def _resolved_open_request(self) -> OpenRequest:
        return self._open_request or open_request_with_fetch_policy


def extract_html(html_text: str) -> HtmlExtractionResult:
    return extract_html_with_fallbacks(html_text)


def extract_html_with_fallbacks(html_text: str) -> HtmlExtractionResult:
    stdlib_result = _extract_html_stdlib(html_text)
    attempted = [EXTRACTOR_NAME]
    if stdlib_result.confidence >= 0.85:
        return _with_attempted_extractors(stdlib_result, attempted)
    trafilatura_result = _extract_html_trafilatura(html_text)
    attempted.append(TRAFILATURA_EXTRACTOR_NAME)
    if trafilatura_result is not None and trafilatura_result.confidence > stdlib_result.confidence:
        return _with_attempted_extractors(trafilatura_result, attempted)
    return _with_attempted_extractors(stdlib_result, attempted)


def _extract_html_stdlib(html_text: str) -> HtmlExtractionResult:
    parser = _SourceHtmlParser()
    parser.feed(html_text)
    parser.close()
    title = _first_text(parser.meta.get("og:title"), parser.title_text, parser.h1_text)
    text = _normalize_text(" ".join(parser.body_text))
    summary = _first_text(parser.meta.get("description"), parser.meta.get("og:description"))
    if summary is None and text:
        summary = _truncate(text, 240)
    canonical_url = _first_text(parser.canonical_url, parser.meta.get("og:url"))
    published_at = _parse_datetime(
        _first_text(*[parser.meta.get(key) for key in PUBLISHED_META_KEYS], parser.time_datetime)
    )
    authors = _authors(parser.meta)
    confidence = _confidence(
        title=title,
        text=text,
        summary=summary,
        published_at=published_at,
        authors=authors,
        canonical_url=canonical_url,
    )
    return HtmlExtractionResult(
        title=title,
        text=text or None,
        summary=summary,
        published_at=published_at,
        authors=authors,
        canonical_url=canonical_url,
        language=parser.language,
        confidence=confidence,
        attempted_extractors=(EXTRACTOR_NAME,),
    )


def _extract_html_trafilatura(html_text: str) -> HtmlExtractionResult | None:
    try:
        import trafilatura  # type: ignore
    except Exception:
        return None
    try:
        extracted = trafilatura.extract(
            html_text,
            output_format="json",
            include_comments=False,
            include_tables=False,
        )
    except Exception:
        return None
    if not extracted:
        return None
    try:
        import json

        payload = json.loads(extracted)
    except Exception:
        return None
    text = _normalize_text(str(payload.get("text") or ""))
    title = _first_text(payload.get("title"))
    summary = _first_text(payload.get("description")) or (_truncate(text, 240) if text else None)
    published_at = _parse_datetime(_first_text(payload.get("date")))
    author_text = _first_text(payload.get("author"))
    authors = [author.strip() for author in (author_text or "").split(";") if author.strip()]
    confidence = _confidence(
        title=title,
        text=text,
        summary=summary,
        published_at=published_at,
        authors=authors,
        canonical_url=_first_text(payload.get("url")),
    )
    return HtmlExtractionResult(
        title=title,
        text=text or None,
        summary=summary,
        published_at=published_at,
        authors=authors,
        canonical_url=_first_text(payload.get("url")),
        language=_first_text(payload.get("language")),
        confidence=confidence,
        extractor_name=TRAFILATURA_EXTRACTOR_NAME,
        attempted_extractors=(TRAFILATURA_EXTRACTOR_NAME,),
    )


def _with_attempted_extractors(
    result: HtmlExtractionResult,
    attempted_extractors: list[str],
) -> HtmlExtractionResult:
    return HtmlExtractionResult(
        title=result.title,
        text=result.text,
        summary=result.summary,
        published_at=result.published_at,
        authors=list(result.authors),
        canonical_url=result.canonical_url,
        language=result.language,
        confidence=result.confidence,
        extractor_name=result.extractor_name,
        attempted_extractors=tuple(dict.fromkeys(attempted_extractors)),
    )


class _SourceHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.body_text: list[str] = []
        self.title_parts: list[str] = []
        self.h1_parts: list[str] = []
        self.title_text: str | None = None
        self.h1_text: str | None = None
        self.canonical_url: str | None = None
        self.time_datetime: str | None = None
        self.language: str | None = None
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attr_map = {key.casefold(): value for key, value in attrs if value is not None}
        self._tag_stack.append(tag)
        if tag == "html" and attr_map.get("lang"):
            self.language = attr_map["lang"].strip() or None
        elif tag == "meta":
            key = _first_text(attr_map.get("property"), attr_map.get("name"))
            content = attr_map.get("content")
            if key and content:
                self.meta[key.strip().casefold()] = _normalize_text(content)
        elif tag == "link" and attr_map.get("href"):
            rel_tokens = {token.casefold() for token in (attr_map.get("rel") or "").split()}
            if "canonical" in rel_tokens:
                self.canonical_url = attr_map["href"].strip()
        elif tag == "time" and attr_map.get("datetime") and self.time_datetime is None:
            self.time_datetime = attr_map["datetime"].strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self.title_text = _normalize_text(" ".join(self.title_parts)) or None
        elif tag == "h1" and self.h1_text is None:
            self.h1_text = _normalize_text(" ".join(self.h1_parts)) or None
        while self._tag_stack:
            popped = self._tag_stack.pop()
            if popped == tag:
                break

    def handle_data(self, data: str) -> None:
        text = _normalize_text(unescape(data))
        if not text:
            return
        if "title" in self._tag_stack:
            self.title_parts.append(text)
            return
        if "h1" in self._tag_stack:
            self.h1_parts.append(text)
        if self._should_collect_body_text():
            self.body_text.append(text)

    def _should_collect_body_text(self) -> bool:
        if "body" not in self._tag_stack:
            return False
        if any(tag in SKIP_TEXT_TAGS or tag in LOW_VALUE_TEXT_TAGS for tag in self._tag_stack):
            return False
        return True


def _authors(meta: dict[str, str]) -> list[str]:
    authors: list[str] = []
    for key in AUTHOR_META_KEYS:
        value = meta.get(key)
        if value:
            authors.extend(part.strip() for part in re.split(r",|;", value) if part.strip())
    return list(dict.fromkeys(authors))


def _confidence(
    *,
    title: str | None,
    text: str | None,
    summary: str | None,
    published_at: datetime | None,
    authors: list[str],
    canonical_url: str | None,
) -> float:
    score = 0.0
    if title:
        score += 0.2
    if text:
        score += 0.35 if len(text) >= 80 else 0.15
    if summary:
        score += 0.15
    if published_at:
        score += 0.1
    if authors:
        score += 0.1
    if canonical_url:
        score += 0.1
    return round(min(1.0, score), 4)


def _first_text(*values: str | None) -> str | None:
    for value in values:
        if value and value.strip():
            return _normalize_text(value)
    return None


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 1].rstrip() + "..."


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except ValueError:
        return None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None
