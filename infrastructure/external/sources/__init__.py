"""Source connector implementations."""

from typing import Any

from infrastructure.external.sources.arxiv import (
    ARXIV_API_URL,
    ArxivConnector,
    ArxivQuery,
    ArxivRequestPacer,
    ArxivSourceConnector,
    ArxivSourcePackage,
    build_arxiv_source_url,
    build_arxiv_src_url,
    normalize_arxiv_id,
)
from infrastructure.external.sources.community import (
    DEVTO_API_URL,
    LOBSTERS_BASE_URL,
    MEDIUM_BASE_URL,
    STACKOVERFLOW_API_URL,
    DevToConnector,
    LobstersConnector,
    MediumConnector,
    StackOverflowConnector,
    build_devto_articles_url,
    build_lobsters_url,
    build_medium_feed_url,
    build_stackoverflow_questions_url,
)
from infrastructure.external.sources.feed import FeedConnector
from infrastructure.external.sources.fetch_policy import (
    DomainRateLimiter,
    RateLimitDecision,
    RobotsDisallowedError,
    SourceFetchPolicy,
    SourceRateLimitExceededError,
    TooManyRedirectsError,
    effective_fetch_policy,
    ensure_robots_allowed,
    open_request_with_fetch_policy,
    run_with_fetch_retries,
)
from infrastructure.external.sources.errors import rate_limited_source_error
from infrastructure.external.sources.github import (
    GITHUB_API_URL,
    GithubConnector,
    GithubConnectorRuntimeOptions,
    GithubRepository,
    GithubRepositoryMetadata,
    GithubRepositorySearchResult,
    build_github_graphql_url,
)
from infrastructure.external.sources.hackernews import (
    HACKERNEWS_API_URL,
    HackerNewsConnector,
    build_hackernews_item_url,
    build_hackernews_story_list_url,
)
from infrastructure.external.sources.html import HtmlConnector, HtmlExtractionResult, extract_html, extract_html_with_fallbacks
from infrastructure.external.sources.manual import ManualConnector
from infrastructure.external.sources.models import RawSourceItem, SourceError, SourceFetchRequest, SourceFetchResult, SourceType
from infrastructure.external.sources.protocol import SourceConnector, SourceFetchContext, SyncSourceConnectorAdapter
from infrastructure.external.sources.reddit import REDDIT_BASE_URL, RedditConnector, build_reddit_listing_url


def default_arxiv_connector(**kwargs: Any) -> ArxivConnector:
    return ArxivConnector(**kwargs)


def default_arxiv_source_connector(**kwargs: Any) -> ArxivSourceConnector:
    return ArxivSourceConnector(**kwargs)


def default_github_connector(**kwargs: Any) -> GithubConnector:
    return GithubConnector(**kwargs)

__all__ = [
    "ARXIV_API_URL",
    "ArxivConnector",
    "ArxivQuery",
    "ArxivRequestPacer",
    "ArxivSourceConnector",
    "ArxivSourcePackage",
    "DEVTO_API_URL",
    "DevToConnector",
    "DomainRateLimiter",
    "FeedConnector",
    "GITHUB_API_URL",
    "GithubConnector",
    "GithubConnectorRuntimeOptions",
    "GithubRepository",
    "GithubRepositoryMetadata",
    "GithubRepositorySearchResult",
    "build_github_graphql_url",
    "build_arxiv_source_url",
    "build_arxiv_src_url",
    "HACKERNEWS_API_URL",
    "HackerNewsConnector",
    "HtmlConnector",
    "HtmlExtractionResult",
    "LOBSTERS_BASE_URL",
    "LobstersConnector",
    "MEDIUM_BASE_URL",
    "MediumConnector",
    "ManualConnector",
    "RawSourceItem",
    "RateLimitDecision",
    "REDDIT_BASE_URL",
    "RedditConnector",
    "RobotsDisallowedError",
    "STACKOVERFLOW_API_URL",
    "SourceFetchPolicy",
    "SourceRateLimitExceededError",
    "SourceError",
    "SourceFetchRequest",
    "SourceFetchResult",
    "SourceConnector",
    "SourceFetchContext",
    "SourceType",
    "StackOverflowConnector",
    "SyncSourceConnectorAdapter",
    "TooManyRedirectsError",
    "build_devto_articles_url",
    "build_hackernews_item_url",
    "build_hackernews_story_list_url",
    "build_lobsters_url",
    "build_medium_feed_url",
    "build_reddit_listing_url",
    "build_stackoverflow_questions_url",
    "default_arxiv_connector",
    "default_arxiv_source_connector",
    "default_github_connector",
    "effective_fetch_policy",
    "ensure_robots_allowed",
    "extract_html",
    "extract_html_with_fallbacks",
    "open_request_with_fetch_policy",
    "rate_limited_source_error",
    "run_with_fetch_retries",
    "normalize_arxiv_id",
]
