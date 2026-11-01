from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone as _tz
UTC = _tz.utc
from hashlib import sha256
from typing import Callable, Any
from urllib.parse import quote, urlencode
from urllib.request import Request

from infrastructure.external.egress import url_origin
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
FetchGraphQL = Callable[[str, dict[str, Any]], str]
GITHUB_API_URL = "https://api.github.com"
# GitHub logins: alphanumerics and single inner hyphens, at most 39 characters.
_GITHUB_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
# Repository names: letters, digits, '.', '_', '-', at most 100 characters.
_GITHUB_REPOSITORY_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
GITHUB_CONTENT_TYPES = (
    "application/json",
    "application/vnd.github+json",
)
GITHUB_TAXONOMY_EXTENSION = SourceTaxonomyExtension(
    invalid_config_keywords=("repository", "token"),
)
GITHUB_DISCUSSIONS_QUERY = """
query RepositoryDiscussions($owner: String!, $name: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    discussions(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        id
        number
        title
        url
        bodyText
        createdAt
        updatedAt
        author {
          login
        }
        category {
          name
        }
        comments {
          totalCount
        }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class GithubRepository:
    owner: str
    name: str

    def __post_init__(self) -> None:
        if not self.owner.strip():
            raise ValueError("github repository owner is required")
        if not self.name.strip():
            raise ValueError("github repository name is required")
        # Owner and name are spliced into API paths next to the token, so
        # "..", "/", "?" or "#" would retarget the authenticated request.
        if _GITHUB_OWNER_RE.fullmatch(self.owner) is None:
            raise ValueError(f"github repository owner is not a valid GitHub login: {self.owner!r}")
        if _GITHUB_REPOSITORY_NAME_RE.fullmatch(self.name) is None or self.name in {".", ".."}:
            raise ValueError(f"github repository name is not a valid repository name: {self.name!r}")

    @classmethod
    def parse(cls, value: str) -> GithubRepository:
        if "/" not in value:
            raise ValueError("github repository must use owner/repo format")
        owner, name = value.split("/", 1)
        return cls(owner=owner.strip(), name=name.strip())

    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class GithubConnectorRuntimeOptions:
    discussion_category: str | None = None
    token_env: str = "GITHUB_TOKEN"

    @classmethod
    def from_source(
        cls,
        source: SourceDefinition,
        *,
        discussion_category: str | None = None,
        token_env: str | None = None,
    ) -> GithubConnectorRuntimeOptions:
        return cls(
            discussion_category=(
                _optional_text(discussion_category)
                or _legacy_github_option(source, "discussion_category")
            ),
            token_env=(
                _optional_text(token_env)
                or _legacy_github_option(source, "token_env")
                or "GITHUB_TOKEN"
            ),
        )


@dataclass(frozen=True)
class GithubRepositorySearchResult:
    repository_id: int | None
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    stargazers_count: int
    forks_count: int
    open_issues_count: int
    archived: bool
    disabled: bool
    visibility: str | None
    topics: list[str]
    updated_at: datetime | None
    score: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "full_name": self.full_name,
            "html_url": self.html_url,
            "description": self.description,
            "language": self.language,
            "stargazers_count": self.stargazers_count,
            "forks_count": self.forks_count,
            "open_issues_count": self.open_issues_count,
            "archived": self.archived,
            "disabled": self.disabled,
            "visibility": self.visibility,
            "topics": list(self.topics),
            "updated_at": _dt(self.updated_at),
            "score": self.score,
        }


@dataclass(frozen=True)
class GithubRepositoryMetadata:
    repository_id: int | None
    full_name: str
    html_url: str
    description: str | None
    language: str | None
    stargazers_count: int
    forks_count: int
    open_issues_count: int
    archived: bool
    disabled: bool
    visibility: str | None
    topics: list[str]
    pushed_at: datetime | None
    updated_at: datetime | None
    watchers_count: int | None = None
    default_branch: str | None = None
    license_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "repository_id": self.repository_id,
            "full_name": self.full_name,
            "html_url": self.html_url,
            "description": self.description,
            "language": self.language,
            "stargazers_count": self.stargazers_count,
            "forks_count": self.forks_count,
            "open_issues_count": self.open_issues_count,
            "archived": self.archived,
            "disabled": self.disabled,
            "visibility": self.visibility,
            "topics": list(self.topics),
            "pushed_at": _dt(self.pushed_at),
            "updated_at": _dt(self.updated_at),
            "watchers_count": self.watchers_count,
            "default_branch": self.default_branch,
            "license_name": self.license_name,
        }


class GithubConnector:
    def __init__(
        self,
        fetch_text: FetchText | None = None,
        *,
        fetch_graphql: FetchGraphQL | None = None,
        fetch_policy: SourceFetchPolicy | None = None,
        rate_limiter: DomainRateLimiter | None = None,
        api_base_url: str = GITHUB_API_URL,
    ) -> None:
        self.fetch_policy = fetch_policy or SourceFetchPolicy()
        self._rate_limiter = rate_limiter or DomainRateLimiter()
        self._uses_default_fetch = fetch_text is None
        self._fetch_text = fetch_text or self._default_fetch_text
        self._fetch_graphql = fetch_graphql
        self._response_metadata_context = SourceFetchResponseMetadataContext()
        # The GitHub token is sent only to this origin. A source URL can be
        # chosen by a model; anything else is fetched without credentials.
        self._credential_origin = url_origin(str(api_base_url).strip() or GITHUB_API_URL)

    def _sends_credentials_to(self, url: str) -> bool:
        return url_origin(url) == self._credential_origin

    @source_fetch_response_scope
    def fetch_releases(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            repo = _repository_from_source(source, repository=repository)
            api_url = build_github_releases_url(source.url or GITHUB_API_URL, repo, limit=limit or 10)
            rate_limit = self._rate_limiter.reserve(
                api_url,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return [], [rate_limited_source_error(source, rate_limit, url=api_url)]
            payload = run_with_fetch_retries(
                lambda: self._fetch_source_text(api_url, policy),
                policy,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="fetch")
            return [], [
                attach_response_metadata_to_error(
                    error,
                    self._response_metadata_context.get(),
                )
            ]
        response_metadata = self._response_metadata_context.get()

        if not payload.strip():
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_source_response",
                        "GitHub API returned an empty response",
                        context=SourceErrorContext(phase="fetch"),
                        retryable=True,
                        source_health_affecting=True,
                    ),
                    response_metadata,
                )
            ]

        try:
            items = self.parse_releases(source, payload, repository=repo, limit=limit)
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="parse")
            return [], [attach_response_metadata_to_error(error, response_metadata)]
        items = attach_response_metadata_to_items(items, response_metadata)

        if not items:
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_github_releases",
                        "GitHub repository returned no releases",
                        context=SourceErrorContext(phase="parse"),
                        retryable=False,
                        source_health_affecting=False,
                    ),
                    response_metadata,
                )
            ]
        return items, []

    def fetch(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        query: str | None = None,
        mode: str | None = None,
        discussion_category: str | None = None,
        token_env: str | None = None,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        options = GithubConnectorRuntimeOptions.from_source(
            source,
            discussion_category=discussion_category,
            token_env=token_env,
        )
        resolved_mode = _github_mode(source, mode=mode)
        if resolved_mode == "releases":
            return self.fetch_releases(source, repository=repository, limit=limit)
        if resolved_mode == "commits":
            return self.fetch_commits(source, repository=repository, limit=limit)
        if resolved_mode == "issues":
            return self.fetch_issues(source, repository=repository, limit=limit)
        if resolved_mode in {"pull_requests", "pulls"}:
            return self.fetch_pull_requests(source, repository=repository, limit=limit)
        if resolved_mode in {"security_advisories", "advisories"}:
            return self.fetch_security_advisories(source, repository=repository, limit=limit)
        if resolved_mode in {"discussions", "discussion"}:
            return self.fetch_discussions(
                source,
                repository=repository,
                limit=limit,
                discussion_category=options.discussion_category,
                token_env=options.token_env,
            )
        if resolved_mode in {"repository_search", "trending", "stars"}:
            search_query = query or _legacy_github_query(source)
            return self.fetch_repository_search_items(
                source,
                query=str(search_query or ""),
                limit=limit,
                sort="stars" if resolved_mode in {"trending", "stars"} else None,
                order="desc" if resolved_mode in {"trending", "stars"} else None,
            )
        return [], [
            build_source_error(
                source,
                "invalid_source_config",
                f"unsupported github collection mode: {resolved_mode}",
                context=SourceErrorContext(phase="fetch"),
                retryable=False,
                source_health_affecting=False,
                operator_action_required=True,
            )
        ]

    def fetch_commits(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        return self._fetch_repo_items(
            source,
            repository=repository,
            limit=limit,
            url_builder=build_github_commits_url,
            parser=self.parse_commits,
            empty_error_type="empty_github_commits",
            empty_error_message="GitHub repository returned no commits",
        )

    def fetch_issues(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
        state: str = "open",
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        return self._fetch_repo_items(
            source,
            repository=repository,
            limit=limit,
            url_builder=lambda base, repo, *, limit: build_github_issues_url(
                base,
                repo,
                limit=limit,
                state=state,
            ),
            parser=self.parse_issues,
            empty_error_type="empty_github_issues",
            empty_error_message="GitHub repository returned no issues",
        )

    def fetch_pull_requests(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
        state: str = "open",
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        return self._fetch_repo_items(
            source,
            repository=repository,
            limit=limit,
            url_builder=lambda base, repo, *, limit: build_github_pull_requests_url(
                base,
                repo,
                limit=limit,
                state=state,
            ),
            parser=self.parse_pull_requests,
            empty_error_type="empty_github_pull_requests",
            empty_error_message="GitHub repository returned no pull requests",
        )

    def fetch_security_advisories(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        return self._fetch_repo_items(
            source,
            repository=repository,
            limit=limit,
            url_builder=build_github_security_advisories_url,
            parser=self.parse_security_advisories,
            empty_error_type="empty_github_security_advisories",
            empty_error_message="GitHub returned no security advisories",
        )

    def fetch_repository_search_items(
        self,
        source: SourceDefinition,
        *,
        query: str,
        limit: int | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        repositories, errors = self.search_repositories(
            source,
            query=query,
            limit=limit,
            sort=sort,
            order=order,
        )
        if errors:
            return [], errors
        fetched_at = datetime.now(UTC)
        items = [
            _raw_item_from_repository_search(source, repository, fetched_at=fetched_at)
            for repository in repositories
        ]
        return items, []

    @source_fetch_response_scope
    def fetch_repository_metadata(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository,
    ) -> tuple[GithubRepositoryMetadata | None, list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            repo = _repository_from_source(source, repository=repository)
            api_url = build_github_repository_metadata_url(source.url or GITHUB_API_URL, repo)
            rate_limit = self._rate_limiter.reserve(
                api_url,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return None, [rate_limited_source_error(source, rate_limit, url=api_url)]
            payload = run_with_fetch_retries(
                lambda: self._fetch_source_text(api_url, policy),
                policy,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="fetch")
            return None, [
                attach_response_metadata_to_error(
                    error,
                    self._response_metadata_context.get(),
                )
            ]

        if not payload.strip():
            return None, [
                build_source_error(
                    source,
                    "empty_source_response",
                    "GitHub API returned an empty repository response",
                    context=SourceErrorContext(phase="fetch"),
                    retryable=True,
                    source_health_affecting=True,
                )
            ]
        try:
            return self.parse_repository_metadata(payload), []
        except Exception as exc:
            return None, [_exception_source_error(source, exc, phase="parse")]

    @source_fetch_response_scope
    def fetch_repository_file(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository,
        path: str,
        ref: str | None = None,
    ) -> tuple[str | None, list[SourceError]]:
        """Fetch one text file through the same GitHub policy boundary."""

        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            repo = _repository_from_source(source, repository=repository)
            clean_path = _validate_repository_path(path)
            endpoint = build_github_contents_url(
                source.url or GITHUB_API_URL,
                repo,
                clean_path,
                ref=ref,
            )
            rate_limit = self._rate_limiter.reserve(
                endpoint,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return None, [rate_limited_source_error(source, rate_limit, url=endpoint)]
            payload = run_with_fetch_retries(
                lambda: self._fetch_source_text(endpoint, policy),
                policy,
            )
            decoded = json.loads(payload)
            if isinstance(decoded, list):
                return "__directory__", []
            if not isinstance(decoded, dict):
                raise ValueError("GitHub file response must be an object")
            encoded = decoded.get("content")
            if not isinstance(encoded, str):
                raise ValueError("GitHub file response did not contain content")
            return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace"), []
        except Exception as exc:
            return None, [_exception_source_error(source, exc, phase="fetch")]

    @source_fetch_response_scope
    def fetch_discussions(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
        category: str | None = None,
        discussion_category: str | None = None,
        token_env: str | None = None,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            options = GithubConnectorRuntimeOptions.from_source(
                source,
                discussion_category=discussion_category or category,
                token_env=token_env,
            )
            repo = _repository_from_source(source, repository=repository)
            endpoint = build_github_graphql_url(source.url or GITHUB_API_URL)
            rate_limit = self._rate_limiter.reserve(
                endpoint,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return [], [rate_limited_source_error(source, rate_limit, url=endpoint)]
            variables = {
                "owner": repo.owner,
                "name": repo.name,
                "first": limit or 10,
            }
            payload = run_with_fetch_retries(
                lambda: self._fetch_graphql_source_text(
                    source,
                    endpoint,
                    {
                        "query": GITHUB_DISCUSSIONS_QUERY,
                        "variables": variables,
                    },
                    policy,
                    options=options,
                ),
                policy,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="fetch")
            return [], [attach_response_metadata_to_error(error, self._response_metadata_context.get())]
        response_metadata = self._response_metadata_context.get()

        if not payload.strip():
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_source_response",
                        "GitHub GraphQL returned an empty response",
                        context=SourceErrorContext(phase="fetch"),
                        retryable=True,
                        source_health_affecting=True,
                    ),
                    response_metadata,
                )
            ]

        try:
            items = self.parse_discussions(
                source,
                payload,
                repository=repo,
                limit=limit,
                category=options.discussion_category,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="parse")
            return [], [attach_response_metadata_to_error(error, response_metadata)]
        items = attach_response_metadata_to_items(items, response_metadata)

        if not items:
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_github_discussions",
                        "GitHub repository returned no discussions",
                        context=SourceErrorContext(phase="parse"),
                        retryable=False,
                        source_health_affecting=False,
                    ),
                    response_metadata,
                )
            ]
        return items, []

    @source_fetch_response_scope
    def search_repositories(
        self,
        source: SourceDefinition,
        *,
        query: str,
        limit: int | None = None,
        sort: str | None = None,
        order: str | None = None,
    ) -> tuple[list[GithubRepositorySearchResult], list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            query_text = query.strip()
            if not query_text:
                raise ValueError("github repository search query is required")
            api_url = build_github_repository_search_url(
                source.url or GITHUB_API_URL,
                query_text,
                limit=limit or 10,
                sort=sort,
                order=order,
            )
            rate_limit = self._rate_limiter.reserve(
                api_url,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return [], [rate_limited_source_error(source, rate_limit, url=api_url)]
            payload = run_with_fetch_retries(
                lambda: self._fetch_source_text(api_url, policy),
                policy,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="fetch")
            return [], [
                attach_response_metadata_to_error(
                    error,
                    self._response_metadata_context.get(),
                )
            ]

        if not payload.strip():
            return [], [
                build_source_error(
                    source,
                    "empty_source_response",
                    "GitHub API returned an empty response",
                    context=SourceErrorContext(phase="fetch"),
                    retryable=True,
                    source_health_affecting=True,
                )
            ]

        try:
            repositories = self.parse_repository_search(payload, limit=limit)
        except Exception as exc:
            return [], [_exception_source_error(source, exc, phase="parse")]

        if not repositories:
            return [], [
                build_source_error(
                    source,
                    "empty_github_repositories",
                    "GitHub repository search returned no repositories",
                    context=SourceErrorContext(phase="parse"),
                    retryable=False,
                    source_health_affecting=False,
                )
            ]
        return repositories, []

    def parse_releases(
        self,
        source: SourceDefinition,
        content: str,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        payload = json.loads(content)
        if not isinstance(payload, list):
            raise ValueError("GitHub releases response must be a JSON array")
        repo = _repository_from_source(source, repository=repository)
        fetched_at = datetime.now(UTC)
        items = [
            _raw_item_from_release(source=source, repository=repo, release=release, fetched_at=fetched_at)
            for release in payload
            if isinstance(release, dict)
        ]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def parse_commits(
        self,
        source: SourceDefinition,
        content: str,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        payload = json.loads(content)
        if not isinstance(payload, list):
            raise ValueError("GitHub commits response must be a JSON array")
        repo = _repository_from_source(source, repository=repository)
        fetched_at = datetime.now(UTC)
        items = [
            _raw_item_from_commit(source=source, repository=repo, commit=item, fetched_at=fetched_at)
            for item in payload
            if isinstance(item, dict)
        ]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def parse_issues(
        self,
        source: SourceDefinition,
        content: str,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        payload = json.loads(content)
        if not isinstance(payload, list):
            raise ValueError("GitHub issues response must be a JSON array")
        repo = _repository_from_source(source, repository=repository)
        fetched_at = datetime.now(UTC)
        items = [
            _raw_item_from_issue(source=source, repository=repo, issue=item, fetched_at=fetched_at)
            for item in payload
            if isinstance(item, dict) and "pull_request" not in item
        ]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def parse_pull_requests(
        self,
        source: SourceDefinition,
        content: str,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        payload = json.loads(content)
        if not isinstance(payload, list):
            raise ValueError("GitHub pull requests response must be a JSON array")
        repo = _repository_from_source(source, repository=repository)
        fetched_at = datetime.now(UTC)
        items = [
            _raw_item_from_pull_request(source=source, repository=repo, pull=item, fetched_at=fetched_at)
            for item in payload
            if isinstance(item, dict)
        ]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def parse_security_advisories(
        self,
        source: SourceDefinition,
        content: str,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
    ) -> list[RawSourceItem]:
        payload = json.loads(content)
        if not isinstance(payload, list):
            raise ValueError("GitHub security advisories response must be a JSON array")
        repo = _repository_from_source(source, repository=repository)
        fetched_at = datetime.now(UTC)
        items = [
            _raw_item_from_security_advisory(
                source=source,
                repository=repo,
                advisory=item,
                fetched_at=fetched_at,
            )
            for item in payload
            if isinstance(item, dict)
        ]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def parse_repository_search(
        self,
        content: str,
        *,
        limit: int | None = None,
    ) -> list[GithubRepositorySearchResult]:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("GitHub repository search response must be a JSON object")
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("GitHub repository search response items must be an array")
        repositories = [
            _repository_search_result(item)
            for item in items
            if isinstance(item, dict)
        ]
        repositories = [repository for repository in repositories if repository is not None]
        return repositories[:limit] if limit else repositories

    def parse_repository_metadata(self, content: str) -> GithubRepositoryMetadata:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("GitHub repository response must be a JSON object")
        metadata = _repository_metadata(payload)
        if metadata is None:
            raise ValueError("GitHub repository response did not include repository identity")
        return metadata

    def parse_discussions(
        self,
        source: SourceDefinition,
        content: str,
        *,
        repository: str | GithubRepository | None = None,
        limit: int | None = None,
        category: str | None = None,
    ) -> list[RawSourceItem]:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            raise ValueError("GitHub discussions response must be a JSON object")
        if payload.get("errors"):
            raise ValueError("GitHub discussions response contains GraphQL errors")
        repo = _repository_from_source(source, repository=repository)
        discussions = (
            payload.get("data", {})
            .get("repository", {})
            .get("discussions", {})
            .get("nodes")
        )
        if not isinstance(discussions, list):
            raise ValueError("GitHub discussions response nodes must be an array")
        fetched_at = datetime.now(UTC)
        expected_category = category.casefold() if category else None
        items = [
            _raw_item_from_discussion(source=source, repository=repo, discussion=discussion, fetched_at=fetched_at)
            for discussion in discussions
            if isinstance(discussion, dict)
            and _discussion_category_matches(discussion, expected_category)
        ]
        items = [item for item in items if item is not None]
        return items[:limit] if limit else items

    def _default_fetch_text(self, url: str, policy: SourceFetchPolicy | None = None) -> str:
        policy = policy or self.fetch_policy
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": policy.user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token and self._sends_credentials_to(url):
            headers["Authorization"] = f"Bearer {token}"
        request = Request(
            url,
            headers=headers,
        )
        with open_request_with_fetch_policy(request, policy) as response:
            response_metadata = response_metadata_from_http_response(response, url=url)
            self._response_metadata_context.set(response_metadata)
            ensure_supported_content_type(response_metadata.content_type, GITHUB_CONTENT_TYPES)
            body = response.read(policy.max_bytes + 1)
        if len(body) > policy.max_bytes:
            raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
        return body.decode("utf-8", errors="replace")

    def _fetch_source_text(self, url: str, policy: SourceFetchPolicy) -> str:
        if self._uses_default_fetch:
            ensure_robots_allowed(url, policy)
            return self._default_fetch_text(url, policy)
        return self._fetch_text(url)

    def _fetch_graphql_source_text(
        self,
        source: SourceDefinition,
        url: str,
        payload: dict[str, Any],
        policy: SourceFetchPolicy,
        *,
        options: GithubConnectorRuntimeOptions,
    ) -> str:
        if self._fetch_graphql is not None:
            return self._fetch_graphql(url, payload)
        token_env = options.token_env
        token = os.getenv(token_env)
        if not token:
            raise ValueError(f"github discussions require token env {token_env}")
        if not self._sends_credentials_to(url):
            raise ValueError("github discussions only query the configured GitHub API origin")
        request = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": policy.user_agent,
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="POST",
        )
        with open_request_with_fetch_policy(request, policy) as response:
            response_metadata = response_metadata_from_http_response(response, url=url)
            self._response_metadata_context.set(response_metadata)
            ensure_supported_content_type(response_metadata.content_type, GITHUB_CONTENT_TYPES)
            body = response.read(policy.max_bytes + 1)
        if len(body) > policy.max_bytes:
            raise ValueError(f"source response exceeds max_bytes: {policy.max_bytes}")
        return body.decode("utf-8", errors="replace")

    @source_fetch_response_scope
    def _fetch_repo_items(
        self,
        source: SourceDefinition,
        *,
        repository: str | GithubRepository | None,
        limit: int | None,
        url_builder,
        parser,
        empty_error_type: str,
        empty_error_message: str,
    ) -> tuple[list[RawSourceItem], list[SourceError]]:
        policy = effective_fetch_policy(self.fetch_policy, source)
        try:
            repo = _repository_from_source(source, repository=repository)
            api_url = url_builder(source.url or GITHUB_API_URL, repo, limit=limit or 10)
            rate_limit = self._rate_limiter.reserve(
                api_url,
                limit_per_minute=self.fetch_policy.rate_limit_per_domain_per_minute,
            )
            if not rate_limit.allowed:
                return [], [rate_limited_source_error(source, rate_limit, url=api_url)]
            payload = run_with_fetch_retries(
                lambda: self._fetch_source_text(api_url, policy),
                policy,
            )
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="fetch")
            return [], [attach_response_metadata_to_error(error, self._response_metadata_context.get())]
        response_metadata = self._response_metadata_context.get()

        if not payload.strip():
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        "empty_source_response",
                        "GitHub API returned an empty response",
                        context=SourceErrorContext(phase="fetch"),
                        retryable=True,
                        source_health_affecting=True,
                    ),
                    response_metadata,
                )
            ]

        try:
            items = parser(source, payload, repository=repo, limit=limit)
        except Exception as exc:
            error = _exception_source_error(source, exc, phase="parse")
            return [], [attach_response_metadata_to_error(error, response_metadata)]
        items = attach_response_metadata_to_items(items, response_metadata)

        if not items:
            return [], [
                attach_response_metadata_to_error(
                    build_source_error(
                        source,
                        empty_error_type,
                        empty_error_message,
                        context=SourceErrorContext(phase="parse"),
                        retryable=False,
                        source_health_affecting=False,
                    ),
                    response_metadata,
                )
            ]
        return items, []


def build_github_releases_url(base_url: str, repository: GithubRepository, *, limit: int) -> str:
    base = base_url.rstrip("/")
    params = urlencode({"per_page": limit})
    return f"{base}/repos/{repository.owner}/{repository.name}/releases?{params}"


def build_github_contents_url(
    base_url: str,
    repository: GithubRepository,
    path: str,
    *,
    ref: str | None = None,
) -> str:
    base = base_url.rstrip("/")
    clean_path = _validate_repository_path(path)
    endpoint = f"{base}/repos/{repository.owner}/{repository.name}/contents/{quote(clean_path, safe='/')}"
    return f"{endpoint}?{urlencode({'ref': ref})}" if ref else endpoint


def _validate_repository_path(path: str) -> str:
    """Reject traversal and sensitive/binary paths before URL construction."""

    raw = str(path).replace("\\", "/")
    parts = raw.split("/")
    if not raw or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("repository file path contains an unsafe segment")
    if any("\x00" in part for part in parts):
        raise ValueError("repository file path contains NUL")
    lowered = [part.casefold() for part in parts]
    blocked_segments = {
        ".env",
        ".git",
        ".github",
        "workflows",
        "secrets",
        "secret",
        "credentials",
        "notebooks",
    }
    if any(part in blocked_segments for part in lowered):
        raise ValueError("repository file path is outside the allowed observation scope")
    blocked_names = {"id_rsa", "id_ed25519", "authorized_keys"}
    if any(part in blocked_names or "token" in part or "password" in part for part in lowered):
        raise ValueError("repository file path is outside the allowed observation scope")
    blocked_suffixes = {
        ".ipynb",
        ".bin",
        ".ckpt",
        ".h5",
        ".onnx",
        ".pb",
        ".pt",
        ".pth",
        ".safetensors",
        ".zip",
        ".tar",
        ".gz",
    }
    if any(part.endswith(tuple(blocked_suffixes)) for part in lowered):
        raise ValueError("repository file path is outside the allowed observation scope")
    return "/".join(parts)


def build_github_commits_url(base_url: str, repository: GithubRepository, *, limit: int) -> str:
    base = base_url.rstrip("/")
    params = urlencode({"per_page": limit})
    return f"{base}/repos/{repository.owner}/{repository.name}/commits?{params}"


def build_github_issues_url(
    base_url: str,
    repository: GithubRepository,
    *,
    limit: int,
    state: str,
) -> str:
    base = base_url.rstrip("/")
    params = urlencode({"state": state, "per_page": limit})
    return f"{base}/repos/{repository.owner}/{repository.name}/issues?{params}"


def build_github_pull_requests_url(
    base_url: str,
    repository: GithubRepository,
    *,
    limit: int,
    state: str,
) -> str:
    base = base_url.rstrip("/")
    params = urlencode({"state": state, "per_page": limit})
    return f"{base}/repos/{repository.owner}/{repository.name}/pulls?{params}"


def build_github_security_advisories_url(
    base_url: str,
    repository: GithubRepository,
    *,
    limit: int,
) -> str:
    base = base_url.rstrip("/")
    params = urlencode({"affects": repository.slug(), "per_page": limit})
    return f"{base}/advisories?{params}"


def build_github_repository_search_url(
    base_url: str,
    query: str,
    *,
    limit: int,
    sort: str | None = None,
    order: str | None = None,
) -> str:
    base = base_url.rstrip("/")
    params: dict[str, Any] = {"q": query, "per_page": limit}
    if sort:
        params["sort"] = sort
    if order:
        params["order"] = order
    return f"{base}/search/repositories?{urlencode(params)}"


def build_github_repository_metadata_url(base_url: str, repository: GithubRepository) -> str:
    base = base_url.rstrip("/")
    return f"{base}/repos/{repository.owner}/{repository.name}"


def build_github_graphql_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/graphql"):
        return base
    if base.endswith("/api/v3"):
        return f"{base[:-7]}/api/graphql"
    return f"{base}/graphql"


def _repository_search_result(item: dict[str, Any]) -> GithubRepositorySearchResult | None:
    full_name = item.get("full_name")
    html_url = item.get("html_url")
    if not full_name or not html_url:
        return None
    return GithubRepositorySearchResult(
        repository_id=_optional_int(item.get("id")),
        full_name=str(full_name),
        html_url=str(html_url),
        description=_optional_text(item.get("description")),
        language=_optional_text(item.get("language")),
        stargazers_count=_int_or_zero(item.get("stargazers_count")),
        forks_count=_int_or_zero(item.get("forks_count")),
        open_issues_count=_int_or_zero(item.get("open_issues_count")),
        archived=bool(item.get("archived", False)),
        disabled=bool(item.get("disabled", False)),
        visibility=_optional_text(item.get("visibility")),
        topics=[str(topic) for topic in item.get("topics") or []],
        updated_at=_parse_datetime(item.get("updated_at")),
        score=_optional_float(item.get("score")),
    )


def _repository_metadata(item: dict[str, Any]) -> GithubRepositoryMetadata | None:
    full_name = item.get("full_name")
    html_url = item.get("html_url")
    if not full_name or not html_url:
        return None
    return GithubRepositoryMetadata(
        repository_id=_optional_int(item.get("id")),
        full_name=str(full_name),
        html_url=str(html_url),
        description=_optional_text(item.get("description")),
        language=_optional_text(item.get("language")),
        stargazers_count=_int_or_zero(item.get("stargazers_count")),
        forks_count=_int_or_zero(item.get("forks_count")),
        open_issues_count=_int_or_zero(item.get("open_issues_count")),
        archived=bool(item.get("archived", False)),
        disabled=bool(item.get("disabled", False)),
        visibility=_optional_text(item.get("visibility")),
        topics=[str(topic) for topic in item.get("topics") or []],
        pushed_at=_parse_datetime(item.get("pushed_at")),
        updated_at=_parse_datetime(item.get("updated_at")),
        watchers_count=_optional_int(item.get("subscribers_count")),
        default_branch=_optional_text(item.get("default_branch")),
        license_name=_optional_text(
            (_dict_or_empty(item.get("license")).get("spdx_id"))
            or (_dict_or_empty(item.get("license")).get("name"))
        ),
    )


def _raw_item_from_release(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    release: dict[str, Any],
    fetched_at: datetime,
) -> RawSourceItem | None:
    url = release.get("html_url")
    tag_name = release.get("tag_name")
    title = release.get("name") or tag_name
    if not url or not title:
        return None
    item_hash = sha256(f"{source.source_id}|{repository.slug()}|{url}".encode("utf-8")).hexdigest()
    author = _dict_or_empty(release.get("author"))
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=str(title).strip(),
        url=str(url).strip(),
        fetched_at=fetched_at,
        published_at=_parse_datetime(release.get("published_at") or release.get("created_at")),
        summary=_normalize_text(release.get("body")),
        raw_content=json.dumps(release, ensure_ascii=False, sort_keys=True),
        authors=[str(author["login"])] if author.get("login") else [],
        tags=[str(tag_name)] if tag_name else [],
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "repository": repository.slug(),
                "release_id": release.get("id"),
                "tag_name": tag_name,
                "target_commitish": release.get("target_commitish"),
                "draft": bool(release.get("draft", False)),
                "prerelease": bool(release.get("prerelease", False)),
                "api_url": release.get("url"),
            },
        ),
    )


def _raw_item_from_commit(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    commit: dict[str, Any],
    fetched_at: datetime,
) -> RawSourceItem | None:
    sha = _optional_text(commit.get("sha"))
    html_url = _optional_text(commit.get("html_url"))
    commit_payload = _dict_or_empty(commit.get("commit"))
    message = _optional_text(commit_payload.get("message"))
    if not sha or not html_url or not message:
        return None
    author_payload = _dict_or_empty(commit_payload.get("author"))
    committer_payload = _dict_or_empty(commit_payload.get("committer"))
    github_author = _dict_or_empty(commit.get("author"))
    title = message.splitlines()[0].strip()
    item_hash = sha256(f"{source.source_id}|{repository.slug()}|commit|{sha}".encode("utf-8")).hexdigest()
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=title,
        url=html_url,
        fetched_at=fetched_at,
        published_at=_parse_datetime(author_payload.get("date") or committer_payload.get("date")),
        summary=_normalize_text(message),
        raw_content=json.dumps(commit, ensure_ascii=False, sort_keys=True),
        authors=[_author for _author in [_optional_text(github_author.get("login")) or _optional_text(author_payload.get("name"))] if _author],
        tags=["commit", sha[:7]],
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "repository": repository.slug(),
                "github_surface": "commits",
                "sha": sha,
                "api_url": commit.get("url"),
            },
        ),
    )


def _raw_item_from_issue(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    issue: dict[str, Any],
    fetched_at: datetime,
) -> RawSourceItem | None:
    return _raw_item_from_issue_like(
        source=source,
        repository=repository,
        item=issue,
        fetched_at=fetched_at,
        surface="issues",
        default_tag="issue",
    )


def _raw_item_from_pull_request(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    pull: dict[str, Any],
    fetched_at: datetime,
) -> RawSourceItem | None:
    return _raw_item_from_issue_like(
        source=source,
        repository=repository,
        item=pull,
        fetched_at=fetched_at,
        surface="pull_requests",
        default_tag="pull_request",
    )


def _raw_item_from_issue_like(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    item: dict[str, Any],
    fetched_at: datetime,
    surface: str,
    default_tag: str,
) -> RawSourceItem | None:
    url = _optional_text(item.get("html_url"))
    title = _optional_text(item.get("title"))
    number = item.get("number")
    if not url or not title:
        return None
    user = _dict_or_empty(item.get("user"))
    labels = [
        str(label.get("name"))
        for label in item.get("labels") or []
        if isinstance(label, dict) and label.get("name")
    ]
    item_hash = sha256(f"{source.source_id}|{repository.slug()}|{surface}|{url}".encode("utf-8")).hexdigest()
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=title,
        url=url,
        fetched_at=fetched_at,
        published_at=_parse_datetime(item.get("created_at") or item.get("updated_at")),
        summary=_normalize_text(item.get("body")),
        raw_content=json.dumps(item, ensure_ascii=False, sort_keys=True),
        authors=[str(user["login"])] if user.get("login") else [],
        tags=[default_tag, *labels],
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "repository": repository.slug(),
                "github_surface": surface,
                "number": number,
                "state": item.get("state"),
                "api_url": item.get("url"),
            },
        ),
    )


def _raw_item_from_security_advisory(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    advisory: dict[str, Any],
    fetched_at: datetime,
) -> RawSourceItem | None:
    ghsa_id = _optional_text(advisory.get("ghsa_id"))
    title = _optional_text(advisory.get("summary") or advisory.get("title"))
    url = _optional_text(advisory.get("html_url"))
    if not url and ghsa_id:
        url = f"https://github.com/advisories/{ghsa_id}"
    if not title or not url:
        return None
    severity = _optional_text(advisory.get("severity"))
    item_hash = sha256(f"{source.source_id}|{repository.slug()}|advisory|{url}".encode("utf-8")).hexdigest()
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=title,
        url=url,
        fetched_at=fetched_at,
        published_at=_parse_datetime(advisory.get("published_at") or advisory.get("updated_at")),
        summary=_normalize_text(advisory.get("description")),
        raw_content=json.dumps(advisory, ensure_ascii=False, sort_keys=True),
        tags=[tag for tag in ["security_advisory", severity] if tag],
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "repository": repository.slug(),
                "github_surface": "security_advisories",
                "ghsa_id": ghsa_id,
                "severity": severity,
                "api_url": advisory.get("url"),
            },
        ),
    )


def _raw_item_from_repository_search(
    source: SourceDefinition,
    repository: GithubRepositorySearchResult,
    *,
    fetched_at: datetime,
) -> RawSourceItem:
    item_hash = sha256(f"{source.source_id}|repository_search|{repository.full_name}".encode("utf-8")).hexdigest()
    tags = ["repository_search", *repository.topics]
    if repository.language:
        tags.append(repository.language)
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=repository.full_name,
        url=repository.html_url,
        fetched_at=fetched_at,
        published_at=repository.updated_at,
        summary=repository.description,
        raw_content=json.dumps(repository.to_dict(), ensure_ascii=False, sort_keys=True),
        tags=tags,
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "github_surface": "repository_search",
                "repository": repository.full_name,
                "stargazers_count": repository.stargazers_count,
                "forks_count": repository.forks_count,
                "open_issues_count": repository.open_issues_count,
                "archived": repository.archived,
            },
        ),
    )


def _raw_item_from_discussion(
    *,
    source: SourceDefinition,
    repository: GithubRepository,
    discussion: dict[str, Any],
    fetched_at: datetime,
) -> RawSourceItem | None:
    discussion_id = _optional_text(discussion.get("id"))
    title = _optional_text(discussion.get("title"))
    url = _optional_text(discussion.get("url"))
    if not title or not url:
        return None
    author = _dict_or_empty(discussion.get("author"))
    category = _dict_or_empty(discussion.get("category"))
    category_name = _optional_text(category.get("name"))
    comments = _dict_or_empty(discussion.get("comments"))
    item_hash = sha256(f"{source.source_id}|{repository.slug()}|discussion|{discussion_id or url}".encode("utf-8")).hexdigest()
    tags = [tag for tag in ["discussion", category_name] if tag]
    return RawSourceItem(
        source_item_id=f"raw_{item_hash[:16]}",
        source_id=source.source_id,
        source_name=source.name,
        source_type=source.source_type,
        title=title,
        url=url,
        fetched_at=fetched_at,
        published_at=_parse_datetime(discussion.get("updatedAt") or discussion.get("createdAt")),
        summary=_normalize_text(discussion.get("bodyText")),
        raw_content=json.dumps(discussion, ensure_ascii=False, sort_keys=True),
        authors=[str(author["login"])] if author.get("login") else [],
        tags=tags,
        language=source.language,
        metadata=source_item_metadata(
            source,
            extra={
                "repository": repository.slug(),
                "github_surface": "discussions",
                "discussion_id": discussion_id,
                "number": discussion.get("number"),
                "category": category_name,
                "comment_count": _int_or_zero(comments.get("totalCount")),
            },
        ),
    )


def _discussion_category_matches(discussion: dict[str, Any], expected_category: str | None) -> bool:
    if expected_category is None:
        return True
    category = _dict_or_empty(discussion.get("category"))
    return str(category.get("name") or "").casefold() == expected_category


def _repository_from_source(
    source: SourceDefinition,
    *,
    repository: str | GithubRepository | None,
) -> GithubRepository:
    if isinstance(repository, GithubRepository):
        return repository
    if isinstance(repository, str) and repository.strip():
        return GithubRepository.parse(repository)
    legacy_repository = _legacy_github_option(source, "repository")
    if legacy_repository:
        return GithubRepository.parse(legacy_repository)
    raise ValueError("github repository must use owner/repo format")


def _github_mode(source: SourceDefinition, *, mode: str | None = None) -> str:
    return (
        _optional_text(mode)
        or _legacy_github_option(source, "github_mode")
        or _legacy_github_option(source, "mode")
        or "releases"
    ).casefold()


def _legacy_github_query(source: SourceDefinition) -> str | None:
    return _legacy_github_option(source, "query")


def _legacy_github_option(source: SourceDefinition, key: str) -> str | None:
    return _optional_text(source.metadata.get(key))


def _exception_source_error(source: SourceDefinition, exc: Exception, *, phase: str) -> SourceError:
    return source_error_from_exception(
        source,
        exc,
        context=SourceErrorContext(phase=phase),
        extension=GITHUB_TAXONOMY_EXTENSION,
    )


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _dt(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value else None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _int_or_zero(value: Any) -> int:
    if value is None:
        return 0
    return int(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _normalize_text(value: Any) -> str | None:
    if not value:
        return None
    return " ".join(str(value).split())
