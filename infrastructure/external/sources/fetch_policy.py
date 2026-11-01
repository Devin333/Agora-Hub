from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone as _tz
from email.utils import parsedate_to_datetime
from math import ceil
from threading import Lock
from typing import Any, Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from urllib.request import Request

from framework.shared.errors import BoundaryViolationError
from infrastructure.external.egress import (
    EgressPolicy,
    TooManyRedirectsError,
    open_guarded,
)
from infrastructure.external.fetch_deadline import (
    FetchDeadlineExceeded,
    deadline_passed,
    fetch_deadline,
    remaining_seconds,
)
from infrastructure.external.sources.models import SourceDefinition


UTC = _tz.utc

_SOURCE_DOMAIN_ALIASES = {
    "export.arxiv.org": "arxiv.org",
}
Now = Callable[[], datetime]
OpenRequest = Callable[[Request, "SourceFetchPolicy"], Any]
T = TypeVar("T")
DEFAULT_RETRY_ON_STATUS_CODES = (429, 500, 502, 503, 504)


class UnsupportedContentTypeError(ValueError):
    def __init__(self, content_type: str, supported_content_types: tuple[str, ...]) -> None:
        self.content_type = content_type
        self.supported_content_types = supported_content_types
        supported = ", ".join(supported_content_types)
        super().__init__(f"unsupported content type: {content_type}; supported: {supported}")


class RobotsDisallowedError(ValueError):
    def __init__(self, url: str, robots_url: str, user_agent: str) -> None:
        self.url = url
        self.robots_url = robots_url
        self.user_agent = user_agent
        super().__init__(f"robots.txt disallows fetching {url} for user-agent {user_agent}")


@dataclass(frozen=True)
class SourceFetchPolicy:
    timeout_seconds: float = 15.0
    max_bytes: int = 1_000_000
    max_redirects: int = 3
    user_agent: str = "news-intelligence-system"
    respect_robots: bool = True
    rate_limit_per_domain_per_minute: int | None = None
    retry_times: int = 2
    retry_on_status_codes: tuple[int, ...] = DEFAULT_RETRY_ON_STATUS_CODES
    # timeout_seconds is the budget for the whole fetch: robots.txt, every
    # redirect hop and every retry. Empty allowed_domains means any public host;
    # non-public addresses need an explicit allowed_private_networks entry.
    allowed_domains: tuple[str, ...] = ()
    allowed_private_networks: tuple[str, ...] = ()
    _egress_policy: EgressPolicy = field(init=False, repr=False, compare=False, default=EgressPolicy())

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
        if self.retry_times < 0:
            raise ValueError("retry_times must be non-negative")
        retry_on_status_codes = tuple(int(code) for code in self.retry_on_status_codes)
        for status_code in retry_on_status_codes:
            if status_code < 100 or status_code > 599:
                raise ValueError("retry_on_status_codes must contain valid HTTP status codes")
        object.__setattr__(self, "retry_on_status_codes", retry_on_status_codes)
        egress_policy = EgressPolicy(
            allowed_domains=tuple(self.allowed_domains),
            allowed_private_networks=tuple(self.allowed_private_networks),
            max_redirects=int(self.max_redirects),
        )
        object.__setattr__(self, "_egress_policy", egress_policy)
        object.__setattr__(self, "allowed_domains", egress_policy.allowed_domains)
        object.__setattr__(self, "allowed_private_networks", egress_policy.allowed_private_networks)

    @property
    def egress_policy(self) -> EgressPolicy:
        return self._egress_policy


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    domain: str
    limit_per_minute: int | None
    window_seconds: int = 60
    retry_after_seconds: int | None = None


class SourceRateLimitExceededError(ValueError):
    def __init__(self, url: str, decision: RateLimitDecision) -> None:
        if decision.allowed:
            raise ValueError("SourceRateLimitExceededError requires a denied decision")
        self.url = url
        self.decision = decision
        self.domain = decision.domain
        self.limit_per_minute = decision.limit_per_minute
        self.window_seconds = decision.window_seconds
        self.retry_after_seconds = decision.retry_after_seconds
        super().__init__(f"source fetch rate limit reached for domain: {decision.domain}")


@dataclass(frozen=True)
class SourceFetchRetryDecision:
    attempts: int
    max_attempts: int
    retryable: bool
    should_retry: bool
    status_code: int | None = None

    @property
    def remaining_attempts(self) -> int:
        return max(0, self.max_attempts - self.attempts)


class DomainRateLimiter:
    def __init__(self, *, now: Now | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._requests: dict[str, deque[datetime]] = defaultdict(deque)
        self._lock = Lock()

    def reserve(self, url: str, *, limit_per_minute: int | None) -> RateLimitDecision:
        domain = source_domain_key(url)
        if limit_per_minute is None:
            return RateLimitDecision(allowed=True, domain=domain, limit_per_minute=None)
        if limit_per_minute < 1:
            raise ValueError("limit_per_minute must be at least 1")

        now = self._current_time()
        window_start = now - timedelta(seconds=60)
        with self._lock:
            bucket = self._requests[domain]
            while bucket and bucket[0] <= window_start:
                bucket.popleft()

            if len(bucket) >= limit_per_minute:
                retry_at = bucket[0] + timedelta(seconds=60)
                retry_after = max(1, ceil((retry_at - now).total_seconds()))
                return RateLimitDecision(
                    allowed=False,
                    domain=domain,
                    limit_per_minute=limit_per_minute,
                    retry_after_seconds=retry_after,
                )

            bucket.append(now)
            return RateLimitDecision(allowed=True, domain=domain, limit_per_minute=limit_per_minute)

    def _current_time(self) -> datetime:
        current = self._now()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current.astimezone(UTC)


def run_with_fetch_retries(operation: Callable[[], T], policy: SourceFetchPolicy) -> T:
    """Run ``operation`` with paced retries, all inside one ``timeout_seconds`` budget.

    Before each retry the loop waits: the upstream's ``Retry-After`` when it
    sent one, otherwise a short exponential backoff. A wait that does not fit
    in the remaining budget ends the retries, since retrying early would only
    hit the same rate limit or overload again.
    """

    attempts = 0
    with fetch_deadline(policy.timeout_seconds):
        while True:
            attempts += 1
            try:
                return operation()
            except Exception as exc:
                decision = decide_source_fetch_retry(exc, policy, attempts=attempts)
                wait_seconds = retry_wait_seconds(exc, attempts) if decision.should_retry else 0.0
                remaining = remaining_seconds()
                if decision.should_retry and (deadline_passed() or (remaining is not None and wait_seconds >= remaining)):
                    decision = replace(decision, should_retry=False)
                _set_retry_state(exc, decision)
                if not decision.should_retry:
                    raise
                if wait_seconds > 0:
                    time.sleep(wait_seconds)


RETRY_BACKOFF_BASE_SECONDS = 0.1
RETRY_BACKOFF_MAX_SECONDS = 1.0
_RETRY_AFTER_STATUS_CODES = frozenset({429, 503})


def retry_wait_seconds(exc: Exception, attempts: int) -> float:
    """Seconds to wait before retry number ``attempts`` (1 = the first retry)."""

    retry_after = retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    return min(RETRY_BACKOFF_MAX_SECONDS, RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)))


def retry_after_seconds(exc: Exception) -> float | None:
    """The ``Retry-After`` of a 429/503 response, in seconds (delta or HTTP date)."""

    if not isinstance(exc, HTTPError) or exc.code not in _RETRY_AFTER_STATUS_CODES:
        return None
    headers = getattr(exc, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    text = str(value).strip()
    if text.isdigit():
        return float(text)
    try:
        retry_at = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def decide_source_fetch_retry(
    exc: Exception,
    policy: SourceFetchPolicy,
    *,
    attempts: int = 1,
) -> SourceFetchRetryDecision:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    max_attempts = policy.retry_times + 1
    status_code = exc.code if isinstance(exc, HTTPError) else None
    if isinstance(exc, HTTPError):
        retryable = exc.code in policy.retry_on_status_codes
    elif isinstance(exc, FetchDeadlineExceeded) or _is_refused_target(exc):
        # The budget is spent, or the outbound policy refused the target: a
        # refused target stays refused on every attempt.
        retryable = False
    elif _is_timeout_exception(exc):
        retryable = True
    elif isinstance(exc, URLError):
        retryable = True
    elif isinstance(exc, ValueError):
        retryable = False
    else:
        retryable = True
    return SourceFetchRetryDecision(
        attempts=attempts,
        max_attempts=max_attempts,
        retryable=retryable,
        should_retry=retryable and attempts < max_attempts,
        status_code=status_code,
    )


def _is_refused_target(exc: BaseException) -> bool:
    # Both outbound guards (egress for source fetches, the pinned transport for
    # model and research endpoints) raise a BoundaryViolationError subclass,
    # sometimes wrapped (URLError) by the caller.
    return isinstance(exc, BoundaryViolationError) or isinstance(exc.__cause__, BoundaryViolationError)


def fetch_attempts(exc: Exception) -> int | None:
    attempts = getattr(exc, "source_fetch_attempts", None)
    return attempts if isinstance(attempts, int) else None


def fetch_retry_decision(exc: Exception) -> SourceFetchRetryDecision | None:
    decision = getattr(exc, "source_fetch_retry_decision", None)
    return decision if isinstance(decision, SourceFetchRetryDecision) else None


def ensure_supported_content_type(
    content_type: str | None,
    supported_content_types: tuple[str, ...],
) -> None:
    if not content_type:
        return
    normalized = content_type.split(";", 1)[0].strip().casefold()
    supported = tuple(content_type.casefold() for content_type in supported_content_types)
    if normalized not in supported:
        raise UnsupportedContentTypeError(normalized, supported)


def open_request_with_fetch_policy(request: Request, policy: SourceFetchPolicy) -> Any:
    """Every source fetch, robots.txt check and redirect goes through the egress guard."""

    return open_guarded(request, policy.egress_policy, timeout_seconds=policy.timeout_seconds)


def effective_fetch_policy(policy: SourceFetchPolicy, source: SourceDefinition) -> SourceFetchPolicy:
    user_agent = source.user_agent or policy.user_agent
    return replace(
        policy,
        respect_robots=policy.respect_robots and source.respect_robots,
        user_agent=user_agent,
    )


def ensure_robots_allowed(
    url: str,
    policy: SourceFetchPolicy,
    *,
    open_request: OpenRequest | None = None,
) -> None:
    if not policy.respect_robots:
        return
    robots_url = _robots_url_for(url)
    if robots_url is None:
        return

    parser = RobotFileParser(robots_url)
    try:
        request = Request(robots_url, headers={"User-Agent": policy.user_agent})
        robots_policy = replace(policy, respect_robots=False, max_bytes=min(policy.max_bytes, 128_000))
        opener = open_request or open_request_with_fetch_policy
        with opener(request, robots_policy) as response:
            body = response.read(robots_policy.max_bytes + 1)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise RobotsDisallowedError(url, robots_url, policy.user_agent) from exc
        if 400 <= exc.code < 500:
            return
        raise

    if len(body) > robots_policy.max_bytes:
        raise ValueError(f"robots.txt response exceeds max_bytes: {robots_policy.max_bytes}")
    parser.parse(body.decode("utf-8", errors="replace").splitlines())
    if not parser.can_fetch(policy.user_agent, url):
        raise RobotsDisallowedError(url, robots_url, policy.user_agent)


def _set_retry_state(exc: Exception, decision: SourceFetchRetryDecision) -> None:
    try:
        setattr(exc, "source_fetch_attempts", decision.attempts)
        setattr(exc, "source_fetch_retryable", decision.retryable)
        setattr(exc, "source_fetch_retry_decision", decision)
    except Exception:
        pass


def _is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    if isinstance(exc, URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return True
        return "timed out" in str(reason).casefold() or "timeout" in str(reason).casefold()
    return False


def source_domain_key(url: str) -> str:
    value = str(url).strip()
    try:
        parsed = urlsplit(value)
        domain = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid source URL for rate limiting: {url}") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or not domain:
        raise ValueError(f"source rate-limit URL must use http or https with a hostname: {url}")
    normalized_domain = domain.casefold()
    return _SOURCE_DOMAIN_ALIASES.get(normalized_domain, normalized_domain)


def _robots_url_for(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
