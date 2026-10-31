from __future__ import annotations


import ipaddress
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from enum import Enum
from typing import Any
from urllib.parse import urlsplit


class SourceType(str, Enum):
    RSS = "rss"
    ATOM = "atom"
    ARXIV = "arxiv"
    GITHUB = "github"
    GITHUB_DISCUSSION = "github_discussion"
    HTML = "html"
    OFFICIAL_BLOG = "official_blog"
    PAPER_INDEX = "paper_index"
    WEB_PAGE = "web_page"
    MANUAL = "manual"
    HACKERNEWS = "hackernews"
    REDDIT = "reddit"
    LOBSTERS = "lobsters"
    STACKOVERFLOW = "stackoverflow"
    DEVTO = "devto"
    MEDIUM = "medium"


class SourceReliability(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceHealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    COOLING_DOWN = "cooling_down"
    DISABLED = "disabled"


DEFAULT_SOURCE_FETCH_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)
_DOMAIN_NAME_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Lineage:
    source_id: str
    source_item_id: str | None = None
    normalized_item_id: str | None = None
    ranked_item_id: str | None = None
    raw_url: str | None = None
    canonical_url: str | None = None
    fetched_at: datetime | None = None
    published_at: datetime | None = None
    raw_artifact_ref: Any | None = None
    parse_artifact_ref: Any | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "source_id": self.source_id,
            "source_item_id": self.source_item_id,
            "normalized_item_id": self.normalized_item_id,
            "ranked_item_id": self.ranked_item_id,
            "raw_url": self.raw_url,
            "canonical_url": self.canonical_url,
            "fetched_at": _dt(self.fetched_at),
            "published_at": _dt(self.published_at),
            "raw_artifact_ref": _artifact_ref(self.raw_artifact_ref),
            "parse_artifact_ref": _artifact_ref(self.parse_artifact_ref),
            "metadata": dict(self.metadata),
        }
        return {key: value for key, value in payload.items() if value not in (None, {}, [])}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Lineage:
        return cls(
            source_id=str(payload["source_id"]),
            source_item_id=_optional_str(payload.get("source_item_id")),
            normalized_item_id=_optional_str(payload.get("normalized_item_id")),
            ranked_item_id=_optional_str(payload.get("ranked_item_id")),
            raw_url=_optional_str(payload.get("raw_url")),
            canonical_url=_optional_str(payload.get("canonical_url")),
            fetched_at=_parse_datetime_optional(payload.get("fetched_at")),
            published_at=_parse_datetime_optional(payload.get("published_at")),
            raw_artifact_ref=payload.get("raw_artifact_ref"),
            parse_artifact_ref=payload.get("parse_artifact_ref"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    name: str
    source_type: SourceType | str
    url: str
    reliability: SourceReliability | str = SourceReliability.MEDIUM
    authority_score: float = 0.5
    enabled: bool = True
    fetch_interval_seconds: int = 3600
    respect_robots: bool = True
    user_agent: str | None = None
    topics: list[str] = field(default_factory=list)
    category: str | None = None
    language: str | None = None
    region: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", SourceType(self.source_type))
        object.__setattr__(self, "reliability", SourceReliability(self.reliability))
        if not self.source_id:
            raise ValueError("source_id is required")
        if not self.name:
            raise ValueError("source name is required")
        if not self.url:
            raise ValueError("source url is required")
        if self.fetch_interval_seconds < 1:
            raise ValueError("fetch_interval_seconds must be at least 1")
        if self.user_agent is not None:
            user_agent = str(self.user_agent).strip()
            if not user_agent:
                raise ValueError("user_agent must not be blank")
            object.__setattr__(self, "user_agent", user_agent)


@dataclass(frozen=True)
class SourceFetchRequest:
    request_id: str
    source_id: str
    source_type: SourceType | str
    url: str | None = None
    query: str | None = None
    timeout_seconds: float = 15.0
    max_bytes: int = 1_000_000
    max_redirects: int = 3
    user_agent: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    since: datetime | None = None
    limit: int | None = None
    connector_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", SourceType(self.source_type))
        if not self.request_id:
            raise ValueError("request_id is required")
        if not self.source_id:
            raise ValueError("source_id is required")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if self.connector_name is not None:
            connector_name = str(self.connector_name).strip()
            object.__setattr__(self, "connector_name", connector_name or None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_id": self.source_id,
            "source_type": SourceType(self.source_type).value,
            "url": self.url,
            "query": self.query,
            "timeout_seconds": self.timeout_seconds,
            "max_bytes": self.max_bytes,
            "max_redirects": self.max_redirects,
            "user_agent": self.user_agent,
            "headers": dict(self.headers),
            "since": _dt(self.since),
            "limit": self.limit,
            "connector_name": self.connector_name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SourceFetchPolicy:
    timeout_seconds: float = 15.0
    max_bytes: int = 1_000_000
    max_redirects: int = 3
    user_agent: str = "news-intelligence-system"
    respect_robots: bool = True
    rate_limit_per_domain_per_minute: int | None = None
    allowed_domains: tuple[str, ...] = ()
    retry_times: int = 2
    retry_on_status_codes: tuple[int, ...] = DEFAULT_SOURCE_FETCH_RETRY_STATUS_CODES
    # Non-public networks (CIDR) a fetch may reach, e.g. an intranet source.
    # Everything non-public is refused by the egress guard unless listed here.
    allowed_private_networks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be at least 1")
        if self.max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        if not self.user_agent:
            raise ValueError("user_agent is required")
        if (
            self.rate_limit_per_domain_per_minute is not None
            and self.rate_limit_per_domain_per_minute < 1
        ):
            raise ValueError("rate_limit_per_domain_per_minute must be at least 1")
        object.__setattr__(
            self,
            "allowed_domains",
            _normalize_allowed_domains(self.allowed_domains),
        )
        if self.retry_times < 0:
            raise ValueError("retry_times must be non-negative")
        retry_on_status_codes = tuple(int(code) for code in self.retry_on_status_codes)
        for status_code in retry_on_status_codes:
            if status_code < 100 or status_code > 599:
                raise ValueError("retry_on_status_codes must contain valid HTTP status codes")
        object.__setattr__(self, "retry_on_status_codes", retry_on_status_codes)
        object.__setattr__(
            self,
            "allowed_private_networks",
            _normalize_private_networks(self.allowed_private_networks),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_bytes": self.max_bytes,
            "max_redirects": self.max_redirects,
            "user_agent": self.user_agent,
            "respect_robots": self.respect_robots,
            "rate_limit_per_domain_per_minute": self.rate_limit_per_domain_per_minute,
            "allowed_domains": list(self.allowed_domains),
            "retry_times": self.retry_times,
            "retry_on_status_codes": list(self.retry_on_status_codes),
            "allowed_private_networks": list(self.allowed_private_networks),
        }


@dataclass(frozen=True)
class SourceFetchResult:
    request_id: str
    source_id: str
    success: bool
    status_code: int | None = None
    content_type: str | None = None
    content_bytes: int | None = None
    latency_ms: float | None = None
    raw_artifact_ref: Any | None = None
    error_type: str | None = None
    error_message: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    fetched_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("request_id is required")
        if not self.source_id:
            raise ValueError("source_id is required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_id": self.source_id,
            "success": self.success,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "content_bytes": self.content_bytes,
            "latency_ms": self.latency_ms,
            "raw_artifact_ref": (
                self.raw_artifact_ref.to_dict()
                if self.raw_artifact_ref is not None and hasattr(self.raw_artifact_ref, "to_dict")
                else self.raw_artifact_ref
            ),
            "error_type": self.error_type,
            "error_message": self.error_message,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "fetched_at": _dt(self.fetched_at),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class RawSourceItem:
    source_item_id: str
    source_id: str
    source_name: str
    source_type: SourceType | str
    title: str
    url: str
    fetched_at: datetime
    published_at: datetime | None = None
    summary: str | None = None
    raw_content: str | None = None
    raw_artifact_ref: Any | None = None
    parse_artifact_ref: Any | None = None
    authors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    language: str | None = None
    lineage: Lineage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_type", SourceType(self.source_type))
        object.__setattr__(self, "fetched_at", _parse_datetime_required(self.fetched_at, "fetched_at"))
        object.__setattr__(self, "published_at", _parse_datetime_optional(self.published_at))
        if isinstance(self.lineage, dict):
            object.__setattr__(self, "lineage", Lineage.from_dict(self.lineage))
        if self.lineage is None:
            object.__setattr__(
                self,
                "lineage",
                Lineage(
                    source_id=self.source_id,
                    source_item_id=self.source_item_id,
                    raw_url=self.url,
                    fetched_at=self.fetched_at,
                    published_at=self.published_at,
                    raw_artifact_ref=self.raw_artifact_ref,
                    parse_artifact_ref=self.parse_artifact_ref,
                ),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_item_id": self.source_item_id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "source_type": SourceType(self.source_type).value,
            "title": self.title,
            "url": self.url,
            "fetched_at": _dt(self.fetched_at),
            "published_at": _dt(self.published_at),
            "summary": self.summary,
            "raw_content": self.raw_content,
            "raw_artifact_ref": _artifact_ref(self.raw_artifact_ref),
            "parse_artifact_ref": _artifact_ref(self.parse_artifact_ref),
            "authors": list(self.authors),
            "tags": list(self.tags),
            "language": self.language,
            "lineage": self.lineage.to_dict() if self.lineage else None,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SourceDuplicateCluster:
    cluster_id: str | None = None
    cluster_size: int = 1
    duplicate_item_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    canonical_urls: list[str] = field(default_factory=list)
    same_event_cluster: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "cluster_size", max(1, int(self.cluster_size or 1)))
        object.__setattr__(
            self,
            "duplicate_item_ids",
            [str(item_id) for item_id in self.duplicate_item_ids],
        )
        object.__setattr__(self, "reasons", [str(reason) for reason in self.reasons])
        object.__setattr__(self, "canonical_urls", [str(url) for url in self.canonical_urls])

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "cluster_size": self.cluster_size,
            "duplicate_item_ids": list(self.duplicate_item_ids),
            "reasons": list(self.reasons),
            "canonical_urls": list(self.canonical_urls),
            "same_event_cluster": self.same_event_cluster,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> SourceDuplicateCluster:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            cluster_id=_optional_str(payload.get("cluster_id")),
            cluster_size=int(_float_or_default(payload.get("cluster_size"), 1)),
            duplicate_item_ids=list(payload.get("duplicate_item_ids") or []),
            reasons=list(payload.get("reasons") or []),
            canonical_urls=list(payload.get("canonical_urls") or []),
            same_event_cluster=_metadata_bool(payload.get("same_event_cluster"), default=False),
        )


@dataclass(frozen=True)
class SourceRankingSignals:
    authority_score: float = 0.5
    duplicate_cluster: SourceDuplicateCluster = field(default_factory=SourceDuplicateCluster)
    historical_importance_score: float = 0.5
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority_score", _clamp_float(self.authority_score))
        object.__setattr__(
            self,
            "historical_importance_score",
            _clamp_float(self.historical_importance_score),
        )
        duplicate_cluster = self.duplicate_cluster
        if isinstance(duplicate_cluster, dict):
            duplicate_cluster = SourceDuplicateCluster.from_dict(duplicate_cluster)
        object.__setattr__(self, "duplicate_cluster", duplicate_cluster)
        object.__setattr__(
            self,
            "tags",
            [str(tag).casefold() for tag in self.tags if str(tag).strip()],
        )

    def with_duplicate_cluster(self, duplicate_cluster: SourceDuplicateCluster) -> SourceRankingSignals:
        return SourceRankingSignals(
            authority_score=self.authority_score,
            duplicate_cluster=duplicate_cluster,
            historical_importance_score=self.historical_importance_score,
            tags=list(self.tags),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority_score": self.authority_score,
            "duplicate_cluster": self.duplicate_cluster.to_dict(),
            "historical_importance_score": self.historical_importance_score,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SourceRankingSignals:
        return cls(
            authority_score=_float_or_default(payload.get("authority_score"), 0.5),
            duplicate_cluster=SourceDuplicateCluster.from_dict(payload.get("duplicate_cluster")),
            historical_importance_score=_float_or_default(
                payload.get("historical_importance_score"),
                0.5,
            ),
            tags=list(payload.get("tags") or []),
        )

    @classmethod
    def from_metadata(
        cls,
        metadata: dict[str, Any],
        *,
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> SourceRankingSignals:
        historical_importance = 0.5
        for key in (
            "historical_importance_score",
            "historical_accuracy_score",
            "source_historical_importance",
        ):
            if key in metadata:
                historical_importance = _float_or_default(metadata.get(key), 0.5)
                break
        raw_tags = list(tags or [])
        if not raw_tags:
            metadata_tags = metadata.get("tags") or metadata.get("source_tags") or []
            raw_tags = list(metadata_tags) if isinstance(metadata_tags, list) else []
        return cls(
            authority_score=_float_or_default(metadata.get("source_authority_score"), 0.5),
            duplicate_cluster=SourceDuplicateCluster.from_dict(metadata.get("duplicate_cluster")),
            historical_importance_score=historical_importance,
            tags=raw_tags,
        )


@dataclass(frozen=True)
class NormalizedSourceItem:
    normalized_item_id: str
    source_item_id: str
    source_id: str
    title: str
    normalized_title: str
    url: str
    canonical_url: str
    canonical_url_hash: str
    title_hash: str
    content_hash: str
    source_reliability: SourceReliability
    fetched_at: datetime
    published_at: datetime | None = None
    summary: str | None = None
    normalized_summary: str | None = None
    language: str | None = None
    ranking_signals: SourceRankingSignals | None = None
    lineage: Lineage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_reliability", SourceReliability(self.source_reliability))
        if self.ranking_signals is None:
            raw_signals = self.metadata.get("ranking_signals")
            if isinstance(raw_signals, dict):
                object.__setattr__(self, "ranking_signals", SourceRankingSignals.from_dict(raw_signals))
            else:
                object.__setattr__(
                    self,
                    "ranking_signals",
                    SourceRankingSignals.from_metadata(self.metadata),
                )
        elif isinstance(self.ranking_signals, dict):
            object.__setattr__(
                self,
                "ranking_signals",
                SourceRankingSignals.from_dict(self.ranking_signals),
            )
        if self.lineage is None:
            metadata_lineage = self.metadata.get("lineage") if isinstance(self.metadata.get("lineage"), dict) else None
            if metadata_lineage is not None:
                object.__setattr__(self, "lineage", Lineage.from_dict(metadata_lineage))
            else:
                object.__setattr__(
                    self,
                    "lineage",
                    Lineage(
                        source_id=self.source_id,
                        source_item_id=self.source_item_id,
                        normalized_item_id=self.normalized_item_id,
                        raw_url=self.url,
                        canonical_url=self.canonical_url,
                        fetched_at=self.fetched_at,
                        published_at=self.published_at,
                    ),
                )


@dataclass(frozen=True)
class SourceRankingTrace:
    lineage: Lineage
    relevance_score: float
    recency_score: float
    reliability_score: float
    authority_score: float
    novelty_score: float
    duplicate_cluster_score: float
    historical_importance_score: float
    subscription_match_score: float
    final_score: float
    source_quality_score: float | None = None
    ranking_fallback: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            **self.lineage.to_dict(),
            "relevance_score": round(self.relevance_score, 4),
            "recency_score": round(self.recency_score, 4),
            "reliability_score": round(self.reliability_score, 4),
            "authority_score": round(self.authority_score, 4),
            "novelty_score": round(self.novelty_score, 4),
            "duplicate_cluster_score": round(self.duplicate_cluster_score, 4),
            "historical_importance_score": round(self.historical_importance_score, 4),
            "subscription_match_score": round(self.subscription_match_score, 4),
            "source_quality_score": self.source_quality_score,
            "final_score": round(self.final_score, 4),
            "ranking_fallback": self.ranking_fallback,
            "metadata": dict(self.metadata),
        }
        return {
            key: value
            for key, value in payload.items()
            if value not in (None, {}, []) and (key != "ranking_fallback" or value is True)
        }

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        lineage: Lineage | None = None,
    ) -> SourceRankingTrace:
        trace_lineage = lineage
        if trace_lineage is None and payload.get("source_id"):
            trace_lineage = Lineage.from_dict(payload)
        if trace_lineage is None:
            raise ValueError("ranking trace requires lineage")
        return cls(
            lineage=trace_lineage,
            relevance_score=_float_or_default(payload.get("relevance_score"), 0.0),
            recency_score=_float_or_default(payload.get("recency_score"), 0.0),
            reliability_score=_float_or_default(payload.get("reliability_score"), 0.0),
            authority_score=_float_or_default(payload.get("authority_score"), 0.0),
            novelty_score=_float_or_default(payload.get("novelty_score"), 0.0),
            duplicate_cluster_score=_float_or_default(payload.get("duplicate_cluster_score"), 0.0),
            historical_importance_score=_float_or_default(
                payload.get("historical_importance_score"),
                0.0,
            ),
            subscription_match_score=_float_or_default(payload.get("subscription_match_score"), 0.0),
            source_quality_score=_optional_float(payload.get("source_quality_score")),
            final_score=_float_or_default(payload.get("final_score"), 0.0),
            ranking_fallback=_metadata_bool(payload.get("ranking_fallback"), default=False),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(frozen=True)
class RankedSourceItem:
    ranked_item_id: str
    item: NormalizedSourceItem
    relevance_score: float
    recency_score: float
    reliability_score: float
    novelty_score: float
    final_score: float
    authority_score: float = 0.0
    duplicate_cluster_score: float = 0.0
    historical_importance_score: float = 0.0
    subscription_match_score: float = 0.0
    source_quality_score: float | None = None
    source_quality: SourceItemQualityScore | None = None
    ranking_trace: SourceRankingTrace | None = None
    rank_reason: str = ""
    lineage: Lineage | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.lineage is None:
            metadata_lineage = self.metadata.get("lineage") if isinstance(self.metadata.get("lineage"), dict) else None
            if metadata_lineage is not None:
                object.__setattr__(self, "lineage", Lineage.from_dict(metadata_lineage))
            else:
                item_lineage = self.item.lineage
                object.__setattr__(
                    self,
                    "lineage",
                    Lineage(
                        source_id=self.item.source_id,
                        source_item_id=self.item.source_item_id,
                        normalized_item_id=self.item.normalized_item_id,
                        ranked_item_id=self.ranked_item_id,
                        raw_url=self.item.url,
                        canonical_url=self.item.canonical_url,
                        fetched_at=self.item.fetched_at,
                        published_at=self.item.published_at,
                        raw_artifact_ref=(item_lineage.raw_artifact_ref if item_lineage else None),
                        parse_artifact_ref=(item_lineage.parse_artifact_ref if item_lineage else None),
                    ),
                )
        if self.source_quality is None:
            raw_quality = self.metadata.get("source_quality")
            if isinstance(raw_quality, dict):
                object.__setattr__(self, "source_quality", SourceItemQualityScore.from_dict(raw_quality))
        elif isinstance(self.source_quality, dict):
            object.__setattr__(self, "source_quality", SourceItemQualityScore.from_dict(self.source_quality))
        if self.ranking_trace is None:
            metadata_lineage = self.metadata.get("lineage") if isinstance(self.metadata.get("lineage"), dict) else None
            trace_quality_score = self.source_quality_score
            if trace_quality_score is None and self.source_quality is not None:
                trace_quality_score = self.source_quality.quality_score
            trace_defaults = {
                "relevance_score": self.relevance_score,
                "recency_score": self.recency_score,
                "reliability_score": self.reliability_score,
                "authority_score": self.authority_score,
                "novelty_score": self.novelty_score,
                "duplicate_cluster_score": self.duplicate_cluster_score,
                "historical_importance_score": self.historical_importance_score,
                "subscription_match_score": self.subscription_match_score,
                "source_quality_score": trace_quality_score,
                "final_score": self.final_score,
            }
            if metadata_lineage is not None:
                object.__setattr__(
                    self,
                    "ranking_trace",
                    SourceRankingTrace.from_dict(
                        {**trace_defaults, **metadata_lineage},
                        lineage=self.lineage,
                    ),
                )
            else:
                object.__setattr__(
                    self,
                    "ranking_trace",
                    SourceRankingTrace.from_dict(trace_defaults, lineage=self.lineage),
                )


@dataclass(frozen=True)
class DuplicateGroup:
    group_id: str
    kept_item_id: str
    duplicate_item_ids: list[str]
    reasons: list[str] = field(default_factory=list)
    canonical_urls: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "kept_item_id": self.kept_item_id,
            "duplicate_item_ids": list(self.duplicate_item_ids),
            "reasons": list(self.reasons),
            "canonical_urls": list(self.canonical_urls),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DedupResult:
    kept_items: list[NormalizedSourceItem]
    duplicate_groups: list[DuplicateGroup]
    dropped_items: list[NormalizedSourceItem]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kept_item_count": len(self.kept_items),
            "duplicate_group_count": len(self.duplicate_groups),
            "dropped_item_count": len(self.dropped_items),
            "kept_item_ids": [item.normalized_item_id for item in self.kept_items],
            "dropped_item_ids": [item.normalized_item_id for item in self.dropped_items],
            "duplicate_groups": [group.to_dict() for group in self.duplicate_groups],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SourceError:
    source_id: str
    error_type: str
    error_message: str
    source_name: str | None = None
    url: str | None = None
    retryable: bool | None = None
    request_ref: Any | None = None
    response_ref: Any | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.retryable is None:
            object.__setattr__(
                self,
                "retryable",
                _metadata_bool(self.metadata.get("retryable"), default=True),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "error_type": self.error_type,
            "error_message": self.error_message,
            "url": self.url,
            "retryable": self.retryable,
            "request_ref": _artifact_ref(self.request_ref),
            "response_ref": _artifact_ref(self.response_ref),
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SourcePipelineEvent:
    event_type: str
    source_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "source_id": self.source_id,
            "occurred_at": self.occurred_at.isoformat().replace("+00:00", "Z"),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    source_name: str | None = None
    url: str | None = None
    status: SourceHealthStatus = SourceHealthStatus.HEALTHY
    consecutive_failures: int = 0
    success_count_24h: int = 0
    failure_count_24h: int = 0
    avg_latency_ms_24h: float | None = None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    cooldown_until: datetime | None = None
    last_error: SourceError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        status = SourceHealthStatus(self.status)
        if status == SourceHealthStatus.COOLING_DOWN:
            status = SourceHealthStatus.DOWN
        object.__setattr__(self, "status", status)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_name": self.source_name,
            "url": self.url,
            "status": self.status.value,
            "health_status": self.status.value,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_failure_count": self.consecutive_failures,
            "success_count_24h": self.success_count_24h,
            "failure_count_24h": self.failure_count_24h,
            "avg_latency_ms_24h": self.avg_latency_ms_24h,
            "last_success_at": _dt(self.last_success_at),
            "last_failure_at": _dt(self.last_failure_at),
            "cooldown_until": _dt(self.cooldown_until),
            "last_error_type": self.last_error.error_type if self.last_error else None,
            "last_error_message": self.last_error.error_message if self.last_error else None,
            "last_error": self.last_error.to_dict() if self.last_error else None,
            "metadata": dict(self.metadata),
        }


@dataclass
class SourcePipelineMetrics:
    sources_total: int = 0
    sources_fetched: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    raw_items_count: int = 0
    normalized_items_count: int = 0
    deduplicated_items_count: int = 0
    ranked_items_count: int = 0
    duplicate_count: int = 0
    avg_fetch_latency_ms: float | None = None
    errors_by_type: dict[str, int] = field(default_factory=dict)
    items_by_source: dict[str, int] = field(default_factory=dict)
    sources_by_type: dict[str, int] = field(default_factory=dict)
    sources_by_reliability: dict[str, int] = field(default_factory=dict)
    fetched_by_type: dict[str, int] = field(default_factory=dict)
    failed_by_type: dict[str, int] = field(default_factory=dict)
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    items_by_source_type: dict[str, int] = field(default_factory=dict)
    items_by_reliability: dict[str, int] = field(default_factory=dict)
    _fetch_latency_total_ms: float = field(default=0.0, init=False, repr=False)
    _fetch_latency_count: int = field(default=0, init=False, repr=False)

    def record_error(self, error: SourceError) -> None:
        self.errors_by_type[error.error_type] = self.errors_by_type.get(error.error_type, 0) + 1

    def record_source_seen(self, source_type: Any, reliability: Any) -> None:
        _increment(self.sources_by_type, _metric_key(source_type))
        _increment(self.sources_by_reliability, _metric_key(reliability))

    def record_source_fetched(
        self,
        *,
        source_id: str,
        source_type: Any,
        reliability: Any,
        item_count: int,
    ) -> None:
        _increment(self.fetched_by_type, _metric_key(source_type))
        self.items_by_source[source_id] = item_count
        _increment(self.items_by_source_type, _metric_key(source_type), item_count)
        _increment(self.items_by_reliability, _metric_key(reliability), item_count)

    def record_source_failed(self, source_type: Any) -> None:
        _increment(self.failed_by_type, _metric_key(source_type))

    def record_source_skipped(self, source_type: Any) -> None:
        _increment(self.skipped_by_type, _metric_key(source_type))

    def record_fetch_latency(self, latency_ms: float) -> None:
        latency = max(0.0, float(latency_ms))
        self._fetch_latency_total_ms += latency
        self._fetch_latency_count += 1
        self.avg_fetch_latency_ms = round(
            self._fetch_latency_total_ms / self._fetch_latency_count,
            3,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources_total": self.sources_total,
            "sources_fetched": self.sources_fetched,
            "sources_failed": self.sources_failed,
            "sources_skipped": self.sources_skipped,
            "raw_items_count": self.raw_items_count,
            "normalized_items_count": self.normalized_items_count,
            "deduplicated_items_count": self.deduplicated_items_count,
            "ranked_items_count": self.ranked_items_count,
            "duplicate_count": self.duplicate_count,
            "avg_fetch_latency_ms": self.avg_fetch_latency_ms,
            "errors_by_type": dict(self.errors_by_type),
            "items_by_source": dict(self.items_by_source),
            "sources_by_type": dict(self.sources_by_type),
            "sources_by_reliability": dict(self.sources_by_reliability),
            "fetched_by_type": dict(self.fetched_by_type),
            "failed_by_type": dict(self.failed_by_type),
            "skipped_by_type": dict(self.skipped_by_type),
            "items_by_source_type": dict(self.items_by_source_type),
            "items_by_reliability": dict(self.items_by_reliability),
        }


@dataclass(frozen=True)
class SourceCoverageReport:
    coverage_status: str
    selected_source_count: int
    attempted_source_count: int
    fetched_source_count: int
    failed_source_count: int
    skipped_source_count: int
    unattempted_source_count: int
    raw_item_count: int
    normalized_item_count: int
    deduplicated_item_count: int
    ranked_item_count: int
    duplicate_item_count: int
    error_count: int
    fetch_success_ratio: float
    attempted_source_ratio: float
    item_yield_ratio: float
    avg_fetch_latency_ms: float | None = None
    sources_by_type: dict[str, int] = field(default_factory=dict)
    sources_by_reliability: dict[str, int] = field(default_factory=dict)
    fetched_by_type: dict[str, int] = field(default_factory=dict)
    failed_by_type: dict[str, int] = field(default_factory=dict)
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    items_by_source: dict[str, int] = field(default_factory=dict)
    items_by_source_type: dict[str, int] = field(default_factory=dict)
    items_by_reliability: dict[str, int] = field(default_factory=dict)
    errors_by_type: dict[str, int] = field(default_factory=dict)
    skipped_source_ids: list[str] = field(default_factory=list)
    failed_source_ids: list[str] = field(default_factory=list)
    partial_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coverage_status": self.coverage_status,
            "selected_source_count": self.selected_source_count,
            "attempted_source_count": self.attempted_source_count,
            "fetched_source_count": self.fetched_source_count,
            "failed_source_count": self.failed_source_count,
            "skipped_source_count": self.skipped_source_count,
            "unattempted_source_count": self.unattempted_source_count,
            "raw_item_count": self.raw_item_count,
            "normalized_item_count": self.normalized_item_count,
            "deduplicated_item_count": self.deduplicated_item_count,
            "ranked_item_count": self.ranked_item_count,
            "duplicate_item_count": self.duplicate_item_count,
            "error_count": self.error_count,
            "fetch_success_ratio": self.fetch_success_ratio,
            "attempted_source_ratio": self.attempted_source_ratio,
            "item_yield_ratio": self.item_yield_ratio,
            "avg_fetch_latency_ms": self.avg_fetch_latency_ms,
            "sources_by_type": dict(self.sources_by_type),
            "sources_by_reliability": dict(self.sources_by_reliability),
            "fetched_by_type": dict(self.fetched_by_type),
            "failed_by_type": dict(self.failed_by_type),
            "skipped_by_type": dict(self.skipped_by_type),
            "items_by_source": dict(self.items_by_source),
            "items_by_source_type": dict(self.items_by_source_type),
            "items_by_reliability": dict(self.items_by_reliability),
            "errors_by_type": dict(self.errors_by_type),
            "skipped_source_ids": list(self.skipped_source_ids),
            "failed_source_ids": list(self.failed_source_ids),
            "partial_reasons": list(self.partial_reasons),
        }


@dataclass(frozen=True)
class SourceConnectorDispatchReport:
    total_dispatch_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    connector_counts: dict[str, int] = field(default_factory=dict)
    success_by_connector: dict[str, int] = field(default_factory=dict)
    failed_by_connector: dict[str, int] = field(default_factory=dict)
    skipped_by_connector: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_dispatch_count": self.total_dispatch_count,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "connector_counts": dict(self.connector_counts),
            "success_by_connector": dict(self.success_by_connector),
            "failed_by_connector": dict(self.failed_by_connector),
            "skipped_by_connector": dict(self.skipped_by_connector),
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True)
class SourceItemQualityScore:
    normalized_item_id: str
    source_item_id: str
    source_id: str
    quality_score: float
    reliability_score: float
    authority_score: float
    traceability_score: float
    freshness_score: float
    content_score: float
    language_score: float
    penalties: list[str] = field(default_factory=list)
    score_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_item_id": self.normalized_item_id,
            "source_item_id": self.source_item_id,
            "source_id": self.source_id,
            "quality_score": self.quality_score,
            "reliability_score": self.reliability_score,
            "authority_score": self.authority_score,
            "traceability_score": self.traceability_score,
            "freshness_score": self.freshness_score,
            "content_score": self.content_score,
            "language_score": self.language_score,
            "penalties": list(self.penalties),
            "score_reason": self.score_reason,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SourceItemQualityScore:
        return cls(
            normalized_item_id=str(payload.get("normalized_item_id") or ""),
            source_item_id=str(payload.get("source_item_id") or ""),
            source_id=str(payload.get("source_id") or ""),
            quality_score=_float_or_default(payload.get("quality_score"), 0.0),
            reliability_score=_float_or_default(payload.get("reliability_score"), 0.0),
            authority_score=_float_or_default(payload.get("authority_score"), 0.0),
            traceability_score=_float_or_default(payload.get("traceability_score"), 0.0),
            freshness_score=_float_or_default(payload.get("freshness_score"), 0.0),
            content_score=_float_or_default(payload.get("content_score"), 0.0),
            language_score=_float_or_default(payload.get("language_score"), 0.0),
            penalties=[str(penalty) for penalty in payload.get("penalties") or []],
            score_reason=str(payload.get("score_reason") or ""),
        )


@dataclass(frozen=True)
class SourceSelectionReport:
    topic: str
    selected_source_count: int
    matched_source_count: int
    fallback_used: bool
    selected_source_ids: list[str] = field(default_factory=list)
    selected_sources: list[dict[str, Any]] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "selected_source_count": self.selected_source_count,
            "matched_source_count": self.matched_source_count,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "selected_source_ids": list(self.selected_source_ids),
            "selected_sources": [dict(source) for source in self.selected_sources],
            "filters": dict(self.filters),
        }


@dataclass(frozen=True)
class SourceGovernanceFinding:
    finding_type: str
    severity: str
    source_id: str
    message: str
    action: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_type": self.finding_type,
            "severity": self.severity,
            "source_id": self.source_id,
            "message": self.message,
            "action": self.action,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SourceGovernanceReport:
    finding_count: int
    blocking_finding_count: int
    requires_strict_verification_source_ids: list[str] = field(default_factory=list)
    findings: list[SourceGovernanceFinding] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_count": self.finding_count,
            "blocking_finding_count": self.blocking_finding_count,
            "requires_strict_verification_source_ids": list(
                self.requires_strict_verification_source_ids
            ),
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class SourceRankingScore:
    ranked_item_id: str
    normalized_item_id: str
    source_item_id: str
    source_id: str
    title: str
    url: str
    relevance_score: float
    recency_score: float
    reliability_score: float
    authority_score: float
    novelty_score: float
    final_score: float
    duplicate_cluster_score: float = 0.0
    historical_importance_score: float = 0.0
    subscription_match_score: float = 0.0
    source_quality_score: float | None = None
    rank_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked_item_id": self.ranked_item_id,
            "normalized_item_id": self.normalized_item_id,
            "source_item_id": self.source_item_id,
            "source_id": self.source_id,
            "title": self.title,
            "url": self.url,
            "relevance_score": self.relevance_score,
            "recency_score": self.recency_score,
            "reliability_score": self.reliability_score,
            "authority_score": self.authority_score,
            "novelty_score": self.novelty_score,
            "duplicate_cluster_score": self.duplicate_cluster_score,
            "historical_importance_score": self.historical_importance_score,
            "subscription_match_score": self.subscription_match_score,
            "source_quality_score": self.source_quality_score,
            "final_score": self.final_score,
            "rank_reason": self.rank_reason,
        }


@dataclass(frozen=True)
class SourceFreshnessReport:
    freshness_status: str
    ranked_item_count: int
    fresh_item_count: int
    stale_item_count: int
    missing_published_at_count: int
    future_timestamp_count: int
    buckets: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "freshness_status": self.freshness_status,
            "ranked_item_count": self.ranked_item_count,
            "fresh_item_count": self.fresh_item_count,
            "stale_item_count": self.stale_item_count,
            "missing_published_at_count": self.missing_published_at_count,
            "future_timestamp_count": self.future_timestamp_count,
            "buckets": dict(self.buckets),
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True)
class SourceFallbackReport:
    total_fallback_count: int
    selection_fallback_used: bool
    item_fallback_count: int
    error_fallback_count: int
    selection_fallback_reason: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_fallback_count": self.total_fallback_count,
            "selection_fallback_used": self.selection_fallback_used,
            "selection_fallback_reason": self.selection_fallback_reason,
            "item_fallback_count": self.item_fallback_count,
            "error_fallback_count": self.error_fallback_count,
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True)
class SourceErrorPolicyReport:
    total_error_count: int
    retryable_error_count: int
    non_retryable_error_count: int
    health_affecting_error_count: int
    workflow_blocking_error_count: int
    operator_action_required_count: int
    errors_by_type: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_error_count": self.total_error_count,
            "retryable_error_count": self.retryable_error_count,
            "non_retryable_error_count": self.non_retryable_error_count,
            "health_affecting_error_count": self.health_affecting_error_count,
            "workflow_blocking_error_count": self.workflow_blocking_error_count,
            "operator_action_required_count": self.operator_action_required_count,
            "errors_by_type": dict(self.errors_by_type),
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True)
class SourceHealthReport:
    health_update_count: int
    status_counts: dict[str, int] = field(default_factory=dict)
    down_source_ids: list[str] = field(default_factory=list)
    cooling_down_source_ids: list[str] = field(default_factory=list)
    degraded_source_ids: list[str] = field(default_factory=list)
    disabled_source_ids: list[str] = field(default_factory=list)
    max_consecutive_failures: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "health_update_count": self.health_update_count,
            "status_counts": dict(self.status_counts),
            "down_source_ids": list(self.down_source_ids),
            "cooling_down_source_ids": list(self.cooling_down_source_ids),
            "degraded_source_ids": list(self.degraded_source_ids),
            "disabled_source_ids": list(self.disabled_source_ids),
            "max_consecutive_failures": self.max_consecutive_failures,
            "rows": [dict(row) for row in self.rows],
        }


@dataclass(frozen=True)
class SourceTraceabilityIssue:
    ranked_item_id: str
    normalized_item_id: str
    source_item_id: str
    source_id: str
    issue_type: str
    field: str
    expected: str | None = None
    actual: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ranked_item_id": self.ranked_item_id,
            "normalized_item_id": self.normalized_item_id,
            "source_item_id": self.source_item_id,
            "source_id": self.source_id,
            "issue_type": self.issue_type,
            "field": self.field,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(frozen=True)
class SourceTraceabilityReport:
    traceability_status: str
    ranked_item_count: int
    traceable_item_count: int
    untraceable_item_count: int
    issue_count: int
    rows: list[dict[str, Any]] = field(default_factory=list)
    issues: list[SourceTraceabilityIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "traceability_status": self.traceability_status,
            "ranked_item_count": self.ranked_item_count,
            "traceable_item_count": self.traceable_item_count,
            "untraceable_item_count": self.untraceable_item_count,
            "issue_count": self.issue_count,
            "rows": [dict(row) for row in self.rows],
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class SourceQualitySummaryReport:
    item_count: int
    average_quality_score: float | None = None
    min_quality_score: float | None = None
    max_quality_score: float | None = None
    low_quality_count: int = 0
    weak_traceability_count: int = 0
    penalty_counts: dict[str, int] = field(default_factory=dict)
    rows: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_count": self.item_count,
            "average_quality_score": self.average_quality_score,
            "min_quality_score": self.min_quality_score,
            "max_quality_score": self.max_quality_score,
            "low_quality_count": self.low_quality_count,
            "weak_traceability_count": self.weak_traceability_count,
            "penalty_counts": dict(self.penalty_counts),
            "rows": [dict(row) for row in self.rows],
        }


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat().replace("+00:00", "Z")


def _metadata_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp_float(value: Any, *, default: float = 0.5) -> float:
    return min(1.0, max(0.0, _float_or_default(value, default)))


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return _float_or_default(value, 0.0)


def _artifact_ref(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return value


def _metric_key(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _increment(metrics: dict[str, int], key: str, amount: int = 1) -> None:
    metrics[key] = metrics.get(key, 0) + amount


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_datetime_optional(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _parse_datetime_required(value: Any, label: str) -> datetime:
    parsed = _parse_datetime_optional(value)
    if parsed is None:
        raise ValueError(f"{label} is required")
    return parsed


def _normalize_private_networks(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("allowed_private_networks must be a list")
    networks: list[str] = []
    for item in value:
        try:
            network = str(ipaddress.ip_network(str(item).strip(), strict=True))
        except ValueError as exc:
            raise ValueError(f"allowed_private_networks entry is not a network: {item!r}") from exc
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def _normalize_allowed_domains(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("allowed_domains must be a list")
    domains: list[str] = []
    seen: set[str] = set()
    for item in value:
        domain = _normalize_allowed_domain(item)
        if domain not in seen:
            domains.append(domain)
            seen.add(domain)
    return tuple(domains)


def _normalize_allowed_domain(value: Any) -> str:
    text = str(value or "").strip().casefold().rstrip(".").lstrip(".")
    if not text:
        raise ValueError("allowed_domains must contain non-empty domain names")
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc or parsed.path != text or parsed.query or parsed.fragment:
        raise ValueError("allowed_domains entries must be domain names, not URLs")
    if "@" in text or ":" in text or "/" in text or "\\" in text:
        raise ValueError("allowed_domains entries must be domain names, not URLs")
    if _DOMAIN_NAME_RE.fullmatch(text) is None:
        raise ValueError(f"allowed_domains entry is not a valid domain name: {text}")
    return text
