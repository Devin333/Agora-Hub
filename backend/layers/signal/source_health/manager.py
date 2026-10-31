from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone as _tz
from threading import Lock, RLock
UTC = _tz.utc
from typing import Protocol

from backend.foundation.models.source import SourceError, SourceHealth, SourceHealthStatus
from backend.layers.signal.source_processing.error_metadata import (
    SourceErrorMetadataInput,
    source_error_metadata,
)

HEALTH_WINDOW = timedelta(hours=24)


@dataclass(frozen=True)
class _HealthEvent:
    occurred_at: datetime
    succeeded: bool
    latency_ms: float | None = None


@dataclass(frozen=True)
class _HealthWindowStats:
    success_count_24h: int = 0
    failure_count_24h: int = 0
    avg_latency_ms_24h: float | None = None
    latency_count_24h: int = 0


@dataclass(frozen=True)
class SourceFetchDecision:
    should_fetch: bool
    health: SourceHealth
    skip_reason: str | None = None
    cooldown_until: datetime | None = None
    next_fetch_at: datetime | None = None


class SourceHealthStore(Protocol):
    def get_source_health(self, source_id: str) -> SourceHealth | None: ...

    def update_source_health(self, health: SourceHealth) -> None: ...


class BasicSourceHealthManager:
    def __init__(
        self,
        *,
        failure_threshold: int = 4,
        degraded_threshold: int = 2,
        cooldown_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
        health_store: SourceHealthStore | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if degraded_threshold < 1:
            raise ValueError("degraded_threshold must be at least 1")
        self.failure_threshold = failure_threshold
        self.degraded_threshold = degraded_threshold
        self.cooldown_seconds = cooldown_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._health_store = health_store
        self._health: dict[str, SourceHealth] = {}
        self._events: dict[str, list[_HealthEvent]] = {}
        # One lock per source: health store reads and writes happen under the
        # lock, and one slow or stalled store call must not block every other
        # source's fetches behind it.
        self._source_locks: dict[str, RLock] = {}
        self._source_locks_guard = Lock()

    def _lock_for(self, source_id: str) -> RLock:
        with self._source_locks_guard:
            lock = self._source_locks.get(source_id)
            if lock is None:
                lock = self._source_locks[source_id] = RLock()
            return lock

    def get(
        self,
        source_id: str,
        *,
        source_name: str | None = None,
        url: str | None = None,
    ) -> SourceHealth:
        with self._lock_for(source_id):
            health = self._health.get(source_id)
            if health is None and self._health_store is not None:
                health = self._health_store.get_source_health(source_id)
            health = health or SourceHealth(source_id=source_id)
            stats = self._window_stats(source_id, now=self._now())
            if stats is not None:
                health = _with_window_stats(health, stats)
            health = _with_source_context(health, source_name=source_name, url=url)
            if source_name is not None or url is not None:
                self._health[source_id] = health
                self._persist_health(health)
            else:
                self._health[source_id] = health
            return health

    def fetch_decision(
        self,
        source_id: str,
        *,
        source_name: str | None = None,
        url: str | None = None,
        min_interval_seconds: int | None = None,
        now: datetime | None = None,
    ) -> SourceFetchDecision:
        with self._lock_for(source_id):
            health = self.get(source_id, source_name=source_name, url=url)
            current_time = _as_utc(now or self._now())
            if health.status == SourceHealthStatus.DISABLED:
                return SourceFetchDecision(
                    should_fetch=False,
                    health=health,
                    skip_reason="disabled",
                )
            if (
                health.status == SourceHealthStatus.DOWN
                and health.cooldown_until is not None
                and _as_utc(health.cooldown_until) > current_time
            ):
                return SourceFetchDecision(
                    should_fetch=False,
                    health=health,
                    skip_reason="cooldown",
                    cooldown_until=health.cooldown_until,
                )
            if min_interval_seconds is not None and min_interval_seconds > 0 and health.last_success_at:
                next_fetch_at = _as_utc(health.last_success_at) + timedelta(
                    seconds=min_interval_seconds
                )
                if next_fetch_at > current_time:
                    return SourceFetchDecision(
                        should_fetch=False,
                        health=health,
                        skip_reason="fetch_interval",
                        next_fetch_at=next_fetch_at,
                    )
            return SourceFetchDecision(should_fetch=True, health=health)

    def should_skip(
        self,
        source_id: str,
        *,
        min_interval_seconds: int | None = None,
    ) -> bool:
        return not self.fetch_decision(
            source_id,
            min_interval_seconds=min_interval_seconds,
        ).should_fetch

    def should_fetch(
        self,
        source_id: str,
        *,
        min_interval_seconds: int | None = None,
    ) -> bool:
        return self.fetch_decision(
            source_id,
            min_interval_seconds=min_interval_seconds,
        ).should_fetch

    def should_probe(self, source_id: str) -> bool:
        with self._lock_for(source_id):
            health = self.get(source_id)
            return (
                health.status == SourceHealthStatus.DOWN
                and health.cooldown_until is not None
                and health.cooldown_until <= self._now()
            )

    def record_success(
        self,
        source_id: str,
        *,
        latency_ms: float | None = None,
        source_name: str | None = None,
        url: str | None = None,
    ) -> SourceHealth:
        with self._lock_for(source_id):
            previous = self.get(source_id)
            now = self._now()
            had_events = bool(self._events.get(source_id))
            stats = self._record_event(source_id, succeeded=True, latency_ms=latency_ms, now=now)
            if not had_events:
                stats = _merge_persisted_stats(previous, stats, now=now)
            health = SourceHealth(
                source_id=source_id,
                source_name=source_name or previous.source_name,
                url=url or previous.url,
                status=SourceHealthStatus.HEALTHY,
                consecutive_failures=0,
                success_count_24h=stats.success_count_24h,
                failure_count_24h=stats.failure_count_24h,
                avg_latency_ms_24h=stats.avg_latency_ms_24h,
                last_success_at=now,
                last_failure_at=previous.last_failure_at,
                metadata=dict(previous.metadata),
            )
            self._health[source_id] = health
            self._persist_health(health)
            return health

    def record_disabled(
        self,
        source_id: str,
        *,
        reason: str | None = None,
        source_name: str | None = None,
        url: str | None = None,
    ) -> SourceHealth:
        with self._lock_for(source_id):
            previous = self.get(source_id)
            error = SourceError(
                source_id=source_id,
                source_name=source_name or previous.source_name,
                error_type="source_disabled",
                error_message=reason or "source is disabled",
                url=url or previous.url,
                retryable=False,
                metadata=source_error_metadata(
                    SourceErrorMetadataInput(
                        phase="health",
                        retryable=False,
                        source_health_affecting=False,
                        workflow_blocking=False,
                        operator_action_required=True,
                    )
                ),
            )
            health = SourceHealth(
                source_id=source_id,
                source_name=source_name or previous.source_name,
                url=url or previous.url,
                status=SourceHealthStatus.DISABLED,
                consecutive_failures=0,
                success_count_24h=previous.success_count_24h,
                failure_count_24h=previous.failure_count_24h,
                avg_latency_ms_24h=previous.avg_latency_ms_24h,
                last_error=error,
                metadata={**previous.metadata, "disabled_reason": reason or "source is disabled"},
            )
            self._health[source_id] = health
            self._persist_health(health)
            return health

    def record_failure(
        self,
        source_id: str,
        error: SourceError,
        *,
        latency_ms: float | None = None,
        source_name: str | None = None,
        url: str | None = None,
    ) -> SourceHealth:
        with self._lock_for(source_id):
            previous = self.get(source_id)
            failures = previous.consecutive_failures + 1
            now = self._now()
            had_events = bool(self._events.get(source_id))
            stats = self._record_event(source_id, succeeded=False, latency_ms=latency_ms, now=now)
            if not had_events:
                stats = _merge_persisted_stats(previous, stats, now=now)
            if failures >= self.failure_threshold:
                status = SourceHealthStatus.DOWN
                cooldown_until = now + timedelta(seconds=self.cooldown_seconds)
            elif failures >= self.degraded_threshold:
                status = SourceHealthStatus.DEGRADED
                cooldown_until = None
            else:
                status = SourceHealthStatus.HEALTHY
                cooldown_until = None
            health = SourceHealth(
                source_id=source_id,
                source_name=source_name or error.source_name or previous.source_name,
                url=url or error.url or previous.url,
                status=status,
                consecutive_failures=failures,
                success_count_24h=stats.success_count_24h,
                failure_count_24h=stats.failure_count_24h,
                avg_latency_ms_24h=stats.avg_latency_ms_24h,
                last_success_at=previous.last_success_at,
                last_failure_at=now,
                cooldown_until=cooldown_until,
                last_error=error,
                metadata=dict(previous.metadata),
            )
            self._health[source_id] = health
            self._persist_health(health)
            return health

    def _record_event(
        self,
        source_id: str,
        *,
        succeeded: bool,
        latency_ms: float | None,
        now: datetime,
    ) -> _HealthWindowStats:
        events = self._events.setdefault(source_id, [])
        events.append(
            _HealthEvent(
                occurred_at=_as_utc(now),
                succeeded=succeeded,
                latency_ms=_latency(latency_ms),
            )
        )
        return self._window_stats(source_id, now=now) or _HealthWindowStats()

    def _window_stats(self, source_id: str, *, now: datetime) -> _HealthWindowStats | None:
        events = self._events.get(source_id)
        if not events:
            return None
        cutoff = _as_utc(now) - HEALTH_WINDOW
        retained = [event for event in events if _as_utc(event.occurred_at) >= cutoff]
        self._events[source_id] = retained
        latencies = [event.latency_ms for event in retained if event.latency_ms is not None]
        return _HealthWindowStats(
            success_count_24h=sum(1 for event in retained if event.succeeded),
            failure_count_24h=sum(1 for event in retained if not event.succeeded),
            avg_latency_ms_24h=(
                round(sum(latencies) / len(latencies), 3) if latencies else None
            ),
            latency_count_24h=len(latencies),
        )

    def _persist_health(self, health: SourceHealth) -> None:
        if self._health_store is not None:
            self._health_store.update_source_health(health)


def _with_source_context(
    health: SourceHealth,
    *,
    source_name: str | None,
    url: str | None,
) -> SourceHealth:
    if source_name is None and url is None:
        return health
    return replace(
        health,
        source_name=source_name or health.source_name,
        url=url or health.url,
    )


def _with_window_stats(health: SourceHealth, stats: _HealthWindowStats) -> SourceHealth:
    return replace(
        health,
        success_count_24h=stats.success_count_24h,
        failure_count_24h=stats.failure_count_24h,
        avg_latency_ms_24h=stats.avg_latency_ms_24h,
    )


def _merge_persisted_stats(
    previous: SourceHealth,
    current: _HealthWindowStats,
    *,
    now: datetime,
) -> _HealthWindowStats:
    cutoff = _as_utc(now) - HEALTH_WINDOW
    previous_success_count = (
        previous.success_count_24h
        if previous.last_success_at is not None and _as_utc(previous.last_success_at) >= cutoff
        else 0
    )
    previous_failure_count = (
        previous.failure_count_24h
        if previous.last_failure_at is not None and _as_utc(previous.last_failure_at) >= cutoff
        else 0
    )
    previous_latency_count = (
        previous_success_count + previous_failure_count
        if previous.avg_latency_ms_24h is not None
        else 0
    )
    total_latency_count = previous_latency_count + current.latency_count_24h
    if total_latency_count:
        current_total = (current.avg_latency_ms_24h or 0.0) * current.latency_count_24h
        previous_total = (previous.avg_latency_ms_24h or 0.0) * previous_latency_count
        avg_latency = round((previous_total + current_total) / total_latency_count, 3)
    else:
        avg_latency = None
    return _HealthWindowStats(
        success_count_24h=previous_success_count + current.success_count_24h,
        failure_count_24h=previous_failure_count + current.failure_count_24h,
        avg_latency_ms_24h=avg_latency,
        latency_count_24h=total_latency_count,
    )


def _latency(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, float(value))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
