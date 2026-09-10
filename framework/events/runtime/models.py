from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
import re
from collections.abc import Iterable, Mapping
from typing import Final, TypeVar

from framework.events.canonical import (
    StoredEvent,
    checksum_for,
    normalize_canonical_json,
    thaw_canonical_json,
)


DEFAULT_PAGE_LIMIT: Final = 100
MAX_PAGE_LIMIT: Final = 1_000
DEFAULT_MAX_ATTEMPTS: Final = 5
# A subscription may be stricter than the runtime default, never more permissive.
MAX_CONFIGURED_ATTEMPTS: Final = DEFAULT_MAX_ATTEMPTS
MIN_LEASE_SECONDS: Final = 5.0
MAX_LEASE_SECONDS: Final = 300.0
MAX_REDACTED_DIAGNOSTIC_LENGTH: Final = 2_048
MAX_REASON_CLASS_LENGTH: Final = 128
MAX_AUTHORIZATION_EVIDENCE_REF_LENGTH: Final = 512
MAX_REDELIVERY_ITEMS: Final = 1_000
MAX_RETIREMENT_CANCELLATION_ITEMS: Final = 1_000
_CHECKSUM_PATTERN: Final = re.compile(r"sha256:[0-9a-f]{64}\Z")
_T = TypeVar("_T")


def stable_text_order_key(value: str) -> bytes:
    """Match SQLite BINARY and PostgreSQL C ordering for durable text identities."""

    return value.encode("utf-8")


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, field_name)


def _positive_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _positive_float(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    normalized = float(value)
    if not isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{field_name} must be finite and greater than zero")
    return normalized


def _nonnegative_float(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    normalized = float(value)
    if not isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return normalized


def _utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _required_utc(value: datetime, field_name: str) -> datetime:
    normalized = _utc(value, field_name)
    if normalized is None:
        raise ValueError(f"{field_name} is required")
    return normalized


def _boolean(value: bool, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _checksum(value: str | None, field_name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field_name} is required")
        return None
    normalized = _required_text(value, field_name).lower()
    if _CHECKSUM_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be sha256:<64 lowercase hex>")
    return normalized


def _typed_tuple(
    values: Iterable[_T],
    expected_type: type[_T],
    field_name: str,
) -> tuple[_T, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be an iterable of {expected_type.__name__}")
    normalized = tuple(values)
    if any(not isinstance(value, expected_type) for value in normalized):
        raise ValueError(f"{field_name} must contain only {expected_type.__name__}")
    return normalized


def _string_frozenset(values: Iterable[str], field_name: str) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a collection of strings")
    return frozenset(_required_text(value, field_name) for value in values)


def _diagnostic(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("redacted_diagnostic must be a string")
    normalized = value.strip()
    if len(normalized) > MAX_REDACTED_DIAGNOSTIC_LENGTH:
        raise ValueError(
            "redacted_diagnostic exceeds "
            f"{MAX_REDACTED_DIAGNOSTIC_LENGTH} characters"
        )
    return normalized or None


def _reason_class(value: str | None, *, required: bool = False) -> str | None:
    normalized = _optional_text(value, "reason_class")
    if normalized is None:
        if required:
            raise ValueError("reason_class is required")
        return None
    if len(normalized) > MAX_REASON_CLASS_LENGTH:
        raise ValueError(
            f"reason_class exceeds {MAX_REASON_CLASS_LENGTH} characters"
        )
    return normalized


def _page_limit(value: int) -> int:
    normalized = _positive_int(value, "limit")
    if normalized > MAX_PAGE_LIMIT:
        raise ValueError(f"limit must be less than or equal to {MAX_PAGE_LIMIT}")
    return normalized


def _sequence(value: int | None, field_name: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, field_name)


class SubscriptionStartPolicy(str, Enum):
    EARLIEST = "earliest"
    LATEST = "latest"
    AT_SEQUENCE = "at_sequence"


class SubscriptionStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    RETIRED = "retired"


class EffectIdempotencyStrategy(str, Enum):
    INBOX_TRANSACTION = "inbox_transaction"
    TARGET_IDEMPOTENCY_KEY = "target_idempotency_key"
    IDEMPOTENT_OVERWRITE = "idempotent_overwrite"


class DeliveryState(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RETRY_WAIT = "retry_wait"
    ACKED = "acked"
    DROPPED = "dropped"
    DEAD_LETTER = "dead_letter"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DeliveryState.ACKED,
            DeliveryState.DROPPED,
            DeliveryState.DEAD_LETTER,
        }


class DeadLetterDisposition(str, Enum):
    OPEN = "open"
    REQUEUED = "requeued"
    RESOLVED = "resolved"


class QuarantineReason(str, Enum):
    UNKNOWN_ENVELOPE_SCHEMA = "unknown_envelope_schema"
    UNKNOWN_DATA_SCHEMA = "unknown_data_schema"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    MISSING_OCCURRED_AT = "missing_occurred_at"
    INVALID_OCCURRED_AT = "invalid_occurred_at"
    CONTEXT_CONFLICT = "context_conflict"
    IDENTITY_COLLISION = "identity_collision"
    CORRUPT_RECORD = "corrupt_record"
    UNSUPPORTED_LEGACY_MAPPING = "unsupported_legacy_mapping"
    UPCAST_FAILED = "upcast_failed"
    SECURITY_SCOPE_AMBIGUOUS = "security_scope_ambiguous"


class QuarantineDisposition(str, Enum):
    PENDING = "pending"
    RELEASED = "released"
    REJECTED = "rejected"


class ReplayMode(str, Enum):
    REBUILD_STATE = "rebuild_state"
    VERIFY_HISTORY = "verify_history"
    REDELIVER = "redeliver"


class ReplayStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


_TRANSACTIONAL_STATE_FIELDS: Final = frozenset(
    {"namespace", "key", "revision", "checksum", "payload"}
)


@dataclass(frozen=True, slots=True)
class TransactionalStateSnapshot:
    """Backend-neutral, checksum-bound state committed beside canonical events."""

    namespace: str
    key: str
    revision: int
    checksum: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        namespace = _required_text(self.namespace, "namespace")
        key = _required_text(self.key, "key")
        revision = _positive_int(self.revision, "revision")
        payload = normalize_canonical_json(self.payload, path="$.payload")
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        checksum = _checksum(self.checksum, "checksum", required=True)
        if checksum is None:  # pragma: no cover - required=True is authoritative
            raise ValueError("checksum is required")
        expected_checksum = checksum_for(
            {
                "namespace": namespace,
                "key": key,
                "revision": revision,
                "payload": thaw_canonical_json(payload),
            }
        )
        if checksum != expected_checksum:
            raise ValueError("checksum does not match transactional state projection")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "checksum", checksum)
        object.__setattr__(self, "payload", payload)

    @classmethod
    def create(
        cls,
        *,
        namespace: str,
        key: str,
        revision: int,
        payload: Mapping[str, object],
    ) -> TransactionalStateSnapshot:
        normalized_namespace = _required_text(namespace, "namespace")
        normalized_key = _required_text(key, "key")
        normalized_revision = _positive_int(revision, "revision")
        normalized_payload = normalize_canonical_json(payload, path="$.payload")
        if not isinstance(normalized_payload, Mapping):
            raise ValueError("payload must be an object")
        return cls(
            namespace=normalized_namespace,
            key=normalized_key,
            revision=normalized_revision,
            checksum=checksum_for(
                {
                    "namespace": normalized_namespace,
                    "key": normalized_key,
                    "revision": normalized_revision,
                    "payload": thaw_canonical_json(normalized_payload),
                }
            ),
            payload=normalized_payload,
        )

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, object],
    ) -> TransactionalStateSnapshot:
        if not isinstance(value, Mapping):
            raise TypeError("transactional state snapshot must be an object")
        if any(not isinstance(field_name, str) for field_name in value):
            raise ValueError("transactional state snapshot fields must be strings")
        fields = frozenset(value)
        if fields != _TRANSACTIONAL_STATE_FIELDS:
            missing = sorted(_TRANSACTIONAL_STATE_FIELDS - fields)
            unknown = sorted(fields - _TRANSACTIONAL_STATE_FIELDS)
            details: list[str] = []
            if missing:
                details.append(f"missing fields: {', '.join(missing)}")
            if unknown:
                details.append(f"unknown fields: {', '.join(unknown)}")
            raise ValueError(
                "invalid transactional state snapshot; " + "; ".join(details)
            )
        payload = value["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be an object")
        return cls(
            namespace=value["namespace"],  # type: ignore[arg-type]
            key=value["key"],  # type: ignore[arg-type]
            revision=value["revision"],  # type: ignore[arg-type]
            checksum=value["checksum"],  # type: ignore[arg-type]
            payload=payload,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "key": self.key,
            "revision": self.revision,
            "checksum": self.checksum,
            "payload": thaw_canonical_json(self.payload),
        }


@dataclass(frozen=True, slots=True, order=True)
class LegacyEventOffset:
    """A 0-based compatibility position, never a durable stream sequence."""

    value: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _nonnegative_int(self.value, "legacy_event_offset"),
        )


@dataclass(frozen=True, slots=True)
class StreamSequenceCursor:
    """Exclusive position bound to one finite stream-prefix snapshot."""

    stream_id: str
    after_sequence: int
    high_watermark: int
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stream_id", _required_text(self.stream_id, "stream_id"))
        after_sequence = _positive_int(self.after_sequence, "after_sequence")
        high_watermark = _positive_int(self.high_watermark, "high_watermark")
        if after_sequence >= high_watermark:
            raise ValueError("cursor after_sequence must be below high_watermark")
        object.__setattr__(self, "after_sequence", after_sequence)
        object.__setattr__(self, "high_watermark", high_watermark)
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))


@dataclass(frozen=True, slots=True)
class StreamReadRequest:
    stream_id: str
    cursor: StreamSequenceCursor | None = None
    limit: int = DEFAULT_PAGE_LIMIT
    through_sequence: int | None = None
    tenant_id: str | None = None
    event_types: frozenset[str] = field(default_factory=frozenset)
    data_schemas: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        stream_id = _required_text(self.stream_id, "stream_id")
        tenant_id = _optional_text(self.tenant_id, "tenant_id")
        if self.cursor is not None and self.cursor.stream_id != stream_id:
            raise ValueError("cursor stream_id must match request stream_id")
        if self.cursor is not None and self.cursor.tenant_id != tenant_id:
            raise ValueError("cursor tenant_id must match request tenant_id")
        through_sequence = _sequence(self.through_sequence, "through_sequence")
        if self.cursor is not None:
            if through_sequence is None:
                through_sequence = self.cursor.high_watermark
            elif through_sequence != self.cursor.high_watermark:
                raise ValueError("through_sequence must match cursor high_watermark")
        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "limit", _page_limit(self.limit))
        object.__setattr__(self, "through_sequence", through_sequence)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(
            self,
            "event_types",
            _string_frozenset(self.event_types, "event_type"),
        )
        object.__setattr__(
            self,
            "data_schemas",
            _string_frozenset(self.data_schemas, "data_schema"),
        )


@dataclass(frozen=True, slots=True)
class EventPage:
    stream_id: str
    events: tuple[StoredEvent, ...]
    high_watermark: int | None
    next_cursor: StreamSequenceCursor | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        stream_id = _required_text(self.stream_id, "stream_id")
        tenant_id = _optional_text(self.tenant_id, "tenant_id")
        events = _typed_tuple(self.events, StoredEvent, "events")
        sequences = tuple(event.stream_sequence for event in events)
        if any(event.stream_id != stream_id for event in events):
            raise ValueError("event page cannot contain another stream")
        if any(event.tenant_id != tenant_id for event in events):
            raise ValueError("event page cannot cross tenant scope")
        if tuple(sorted(sequences)) != sequences or len(set(sequences)) != len(sequences):
            raise ValueError("event page must be strictly ordered by stream_sequence")
        object.__setattr__(self, "stream_id", stream_id)
        object.__setattr__(self, "events", events)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(
            self,
            "high_watermark",
            _sequence(self.high_watermark, "high_watermark"),
        )
        if events and self.high_watermark is None:
            raise ValueError("non-empty event page requires a high_watermark")
        if events and events[-1].stream_sequence > self.high_watermark:
            raise ValueError("event page cannot exceed high_watermark")
        if self.next_cursor is not None:
            if not events:
                raise ValueError("empty event page cannot have a next_cursor")
            if self.next_cursor.stream_id != stream_id:
                raise ValueError("next_cursor stream_id must match event page")
            if self.next_cursor.tenant_id != tenant_id:
                raise ValueError("next_cursor tenant_id must match event page")
            if self.next_cursor.high_watermark != self.high_watermark:
                raise ValueError("next_cursor must preserve the page high_watermark")
            if self.next_cursor.after_sequence != events[-1].stream_sequence:
                raise ValueError("next_cursor must follow the last returned event")

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True, slots=True)
class SubscriptionKey:
    subscription_id: str
    subscription_version: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _required_text(self.subscription_id, "subscription_id"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )


@dataclass(frozen=True, slots=True)
class SubscriptionStart:
    policy: SubscriptionStartPolicy = SubscriptionStartPolicy.EARLIEST
    start_sequence: int | None = None

    def __post_init__(self) -> None:
        policy = SubscriptionStartPolicy(self.policy)
        sequence = _sequence(self.start_sequence, "start_sequence")
        if policy is SubscriptionStartPolicy.AT_SEQUENCE and sequence is None:
            raise ValueError("AT_SEQUENCE requires a 1-based start_sequence")
        if policy is not SubscriptionStartPolicy.AT_SEQUENCE and sequence is not None:
            raise ValueError("start_sequence is only valid with AT_SEQUENCE")
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "start_sequence", sequence)


@dataclass(frozen=True, slots=True)
class SubscriptionFilter:
    event_types: frozenset[str] = field(default_factory=frozenset)
    data_schemas: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "event_types",
            _string_frozenset(self.event_types, "event_type"),
        )
        object.__setattr__(
            self,
            "data_schemas",
            _string_frozenset(self.data_schemas, "data_schema"),
        )


@dataclass(frozen=True, slots=True)
class ConsumerEffectContract:
    performs_external_effects: bool = False
    consumer_effect_id: str | None = None
    idempotency_strategy: EffectIdempotencyStrategy | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "performs_external_effects",
            _boolean(self.performs_external_effects, "performs_external_effects"),
        )
        effect_id = _optional_text(self.consumer_effect_id, "consumer_effect_id")
        strategy = (
            None
            if self.idempotency_strategy is None
            else EffectIdempotencyStrategy(self.idempotency_strategy)
        )
        if self.performs_external_effects and (effect_id is None or strategy is None):
            raise ValueError(
                "external-effect consumers require consumer_effect_id and "
                "a declared idempotency_strategy"
            )
        if strategy is not None and effect_id is None:
            raise ValueError("idempotency_strategy requires consumer_effect_id")
        object.__setattr__(self, "consumer_effect_id", effect_id)
        object.__setattr__(self, "idempotency_strategy", strategy)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    initial_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 60.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        attempts = _positive_int(self.max_attempts, "max_attempts")
        if attempts > MAX_CONFIGURED_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be less than or equal to {MAX_CONFIGURED_ATTEMPTS}"
            )
        initial = _positive_float(self.initial_delay_seconds, "initial_delay_seconds")
        multiplier = _positive_float(self.multiplier, "multiplier")
        maximum = _positive_float(self.max_delay_seconds, "max_delay_seconds")
        if isinstance(self.jitter_ratio, bool) or not isinstance(
            self.jitter_ratio,
            (int, float),
        ):
            raise ValueError("jitter_ratio must be a number")
        jitter = float(self.jitter_ratio)
        if multiplier < 1:
            raise ValueError("multiplier must be greater than or equal to one")
        if maximum < initial:
            raise ValueError("max_delay_seconds cannot be less than initial_delay_seconds")
        if not isfinite(jitter) or not 0 <= jitter < 1:
            raise ValueError("jitter_ratio must be finite and in [0, 1)")
        object.__setattr__(self, "max_attempts", attempts)
        object.__setattr__(self, "initial_delay_seconds", initial)
        object.__setattr__(self, "multiplier", multiplier)
        object.__setattr__(self, "max_delay_seconds", maximum)
        object.__setattr__(self, "jitter_ratio", jitter)

    def can_retry(self, attempt_count: int) -> bool:
        return _positive_int(attempt_count, "attempt_count") < self.max_attempts

    def base_delay_seconds(self, attempt_count: int) -> float:
        attempt = _positive_int(attempt_count, "attempt_count")
        return min(
            self.max_delay_seconds,
            self.initial_delay_seconds * self.multiplier ** (attempt - 1),
        )


@dataclass(frozen=True, slots=True)
class LeasePolicy:
    duration_seconds: float = 30.0

    def __post_init__(self) -> None:
        duration = _positive_float(self.duration_seconds, "duration_seconds")
        if not MIN_LEASE_SECONDS <= duration <= MAX_LEASE_SECONDS:
            raise ValueError(
                f"duration_seconds must be in [{MIN_LEASE_SECONDS:g}, "
                f"{MAX_LEASE_SECONDS:g}]"
            )
        object.__setattr__(self, "duration_seconds", duration)


@dataclass(frozen=True, slots=True)
class DeliveryLimits:
    batch_size: int = 100
    max_in_flight: int = 100
    max_concurrency: int = 1
    pending_warning_threshold: int = 10_000
    pending_hard_limit: int = 100_000

    def __post_init__(self) -> None:
        batch = _page_limit(self.batch_size)
        in_flight = _positive_int(self.max_in_flight, "max_in_flight")
        concurrency = _positive_int(self.max_concurrency, "max_concurrency")
        warning = _positive_int(
            self.pending_warning_threshold,
            "pending_warning_threshold",
        )
        hard = _positive_int(self.pending_hard_limit, "pending_hard_limit")
        if batch > in_flight:
            raise ValueError("batch_size cannot exceed max_in_flight")
        if concurrency > in_flight:
            raise ValueError("max_concurrency cannot exceed max_in_flight")
        if warning >= hard:
            raise ValueError("pending_warning_threshold must be below pending_hard_limit")
        object.__setattr__(self, "batch_size", batch)
        object.__setattr__(self, "max_in_flight", in_flight)
        object.__setattr__(self, "max_concurrency", concurrency)
        object.__setattr__(self, "pending_warning_threshold", warning)
        object.__setattr__(self, "pending_hard_limit", hard)


@dataclass(frozen=True, slots=True)
class DurableSubscription:
    subscription_id: str
    subscription_version: int
    consumer_id: str
    event_filter: SubscriptionFilter = field(default_factory=SubscriptionFilter)
    start: SubscriptionStart = field(default_factory=SubscriptionStart)
    effect: ConsumerEffectContract = field(default_factory=ConsumerEffectContract)
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    lease_policy: LeasePolicy = field(default_factory=LeasePolicy)
    limits: DeliveryLimits = field(default_factory=DeliveryLimits)
    status: SubscriptionStatus = SubscriptionStatus.ACTIVE
    supports_out_of_order_repair: bool = False
    tenant_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name, expected_type in (
            ("event_filter", SubscriptionFilter),
            ("start", SubscriptionStart),
            ("effect", ConsumerEffectContract),
            ("retry_policy", RetryPolicy),
            ("lease_policy", LeasePolicy),
            ("limits", DeliveryLimits),
        ):
            if not isinstance(getattr(self, field_name), expected_type):
                raise ValueError(f"{field_name} must be {expected_type.__name__}")
        object.__setattr__(
            self,
            "subscription_id",
            _required_text(self.subscription_id, "subscription_id"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )
        object.__setattr__(self, "consumer_id", _required_text(self.consumer_id, "consumer_id"))
        object.__setattr__(self, "status", SubscriptionStatus(self.status))
        object.__setattr__(
            self,
            "supports_out_of_order_repair",
            _boolean(
                self.supports_out_of_order_repair,
                "supports_out_of_order_repair",
            ),
        )
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")

    @property
    def key(self) -> SubscriptionKey:
        return SubscriptionKey(self.subscription_id, self.subscription_version)


@dataclass(frozen=True, slots=True)
class SubscriptionQuery:
    tenant_id: str | None = None
    status: SubscriptionStatus | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        if self.status is not None:
            object.__setattr__(self, "status", SubscriptionStatus(self.status))
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class SubscriptionStreamState:
    """Registration/retirement boundary for one tenant-scoped stream."""

    subscription_id: str
    subscription_version: int
    stream_id: str
    start_sequence: int
    registration_watermark: int
    tenant_id: str | None = None
    retirement_watermark: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        key = SubscriptionKey(self.subscription_id, self.subscription_version)
        object.__setattr__(self, "subscription_id", key.subscription_id)
        object.__setattr__(self, "subscription_version", key.subscription_version)
        object.__setattr__(self, "stream_id", _required_text(self.stream_id, "stream_id"))
        object.__setattr__(
            self,
            "start_sequence",
            _positive_int(self.start_sequence, "start_sequence"),
        )
        registration = _nonnegative_int(
            self.registration_watermark,
            "registration_watermark",
        )
        retirement = (
            None
            if self.retirement_watermark is None
            else _nonnegative_int(
                self.retirement_watermark,
                "retirement_watermark",
            )
        )
        if retirement is not None and retirement < registration:
            raise ValueError("retirement_watermark cannot precede registration_watermark")
        if self.start_sequence > registration + 1:
            raise ValueError(
                "start_sequence cannot exceed registration_watermark plus one"
            )
        object.__setattr__(self, "registration_watermark", registration)
        object.__setattr__(self, "retirement_watermark", retirement)
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        created_at = _utc(self.created_at, "created_at")
        updated_at = _utc(self.updated_at, "updated_at")
        if created_at is not None and updated_at is not None and updated_at < created_at:
            raise ValueError("updated_at cannot precede created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @property
    def key(self) -> SubscriptionKey:
        return SubscriptionKey(self.subscription_id, self.subscription_version)


@dataclass(frozen=True, slots=True)
class SubscriptionStreamStateQuery:
    subscription_id: str | None = None
    subscription_version: int | None = None
    stream_id: str | None = None
    tenant_id: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _optional_text(self.subscription_id, "subscription_id"),
        )
        if self.subscription_version is not None:
            object.__setattr__(
                self,
                "subscription_version",
                _positive_int(self.subscription_version, "subscription_version"),
            )
        if self.subscription_version is not None and self.subscription_id is None:
            raise ValueError("subscription_version requires subscription_id")
        object.__setattr__(self, "stream_id", _optional_text(self.stream_id, "stream_id"))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class SubscriptionStreamStatePage:
    states: tuple[SubscriptionStreamState, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "states",
            _typed_tuple(self.states, SubscriptionStreamState, "states"),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class SubscriptionPage:
    subscriptions: tuple[DurableSubscription, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscriptions",
            _typed_tuple(
                self.subscriptions,
                DurableSubscription,
                "subscriptions",
            ),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class DeliveryKey:
    event_id: str
    subscription_id: str
    subscription_version: int
    delivery_generation: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "subscription_id",
            _required_text(self.subscription_id, "subscription_id"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )
        object.__setattr__(
            self,
            "delivery_generation",
            _positive_int(self.delivery_generation, "delivery_generation"),
        )


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: str
    event_id: str
    stream_id: str
    stream_sequence: int
    subscription_id: str
    subscription_version: int
    consumer_id: str
    consumer_effect_id: str | None = None
    tenant_id: str | None = None
    delivery_generation: int = 1
    state: DeliveryState = DeliveryState.PENDING
    attempt_count: int = 0
    available_at: datetime | None = None
    lease_owner: str | None = None
    lease_generation: int | None = None
    lease_expires_at: datetime | None = None
    first_failure_at: datetime | None = None
    last_failure_at: datetime | None = None
    reason_class: str | None = None
    redacted_diagnostic: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "delivery_id",
            "event_id",
            "stream_id",
            "subscription_id",
            "consumer_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "stream_sequence",
            _positive_int(self.stream_sequence, "stream_sequence"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )
        object.__setattr__(
            self,
            "delivery_generation",
            _positive_int(self.delivery_generation, "delivery_generation"),
        )
        object.__setattr__(
            self,
            "attempt_count",
            _nonnegative_int(self.attempt_count, "attempt_count"),
        )
        object.__setattr__(self, "state", DeliveryState(self.state))
        object.__setattr__(
            self,
            "consumer_effect_id",
            _optional_text(self.consumer_effect_id, "consumer_effect_id"),
        )
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "lease_owner", _optional_text(self.lease_owner, "lease_owner"))
        if self.lease_generation is not None:
            object.__setattr__(
                self,
                "lease_generation",
                _positive_int(self.lease_generation, "lease_generation"),
            )
        for field_name in (
            "available_at",
            "lease_expires_at",
            "first_failure_at",
            "last_failure_at",
            "created_at",
            "updated_at",
        ):
            object.__setattr__(self, field_name, _utc(getattr(self, field_name), field_name))
        lease_values = (self.lease_owner, self.lease_generation, self.lease_expires_at)
        if any(value is not None for value in lease_values) and not all(
            value is not None for value in lease_values
        ):
            raise ValueError("lease_owner, lease_generation, and lease_expires_at are atomic")
        if self.state is DeliveryState.CLAIMED:
            if not all(value is not None for value in lease_values):
                raise ValueError("CLAIMED delivery requires a complete lease")
        elif any(value is not None for value in lease_values):
            raise ValueError("only CLAIMED delivery may retain a lease")
        if self.state is DeliveryState.PENDING and self.attempt_count != 0:
            raise ValueError("PENDING delivery must have zero attempts")
        if self.state is not DeliveryState.PENDING and self.attempt_count == 0:
            raise ValueError("non-pending delivery requires at least one attempt")
        object.__setattr__(self, "reason_class", _reason_class(self.reason_class))
        object.__setattr__(self, "redacted_diagnostic", _diagnostic(self.redacted_diagnostic))
        if (self.first_failure_at is None) != (self.last_failure_at is None):
            raise ValueError("first_failure_at and last_failure_at must be set together")
        if (
            self.first_failure_at is not None
            and self.last_failure_at is not None
            and self.last_failure_at < self.first_failure_at
        ):
            raise ValueError("last_failure_at cannot precede first_failure_at")
        if self.state in {DeliveryState.RETRY_WAIT, DeliveryState.DEAD_LETTER}:
            if self.first_failure_at is None or self.reason_class is None:
                raise ValueError(f"{self.state.value} delivery requires failure details")
        if (
            self.created_at is not None
            and self.updated_at is not None
            and self.updated_at < self.created_at
        ):
            raise ValueError("updated_at cannot precede created_at")

    @property
    def key(self) -> DeliveryKey:
        return DeliveryKey(
            event_id=self.event_id,
            subscription_id=self.subscription_id,
            subscription_version=self.subscription_version,
            delivery_generation=self.delivery_generation,
        )


@dataclass(frozen=True, slots=True)
class DeliveryQuery:
    subscription_id: str | None = None
    subscription_version: int | None = None
    stream_id: str | None = None
    state: DeliveryState | None = None
    tenant_id: str | None = None
    after_sequence: int | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _optional_text(self.subscription_id, "subscription_id"),
        )
        if self.subscription_version is not None:
            object.__setattr__(
                self,
                "subscription_version",
                _positive_int(self.subscription_version, "subscription_version"),
            )
        if self.subscription_version is not None and self.subscription_id is None:
            raise ValueError("subscription_version requires subscription_id")
        object.__setattr__(self, "stream_id", _optional_text(self.stream_id, "stream_id"))
        if self.state is not None:
            object.__setattr__(self, "state", DeliveryState(self.state))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(
            self,
            "after_sequence",
            _sequence(self.after_sequence, "after_sequence"),
        )
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class DeliveryPage:
    records: tuple[DeliveryRecord, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            _typed_tuple(self.records, DeliveryRecord, "records"),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class DeliveryClaimRequest:
    subscription_id: str
    subscription_version: int
    lease_owner: str
    requested_at: datetime
    lease_duration_seconds: float = 30.0
    limit: int = DEFAULT_PAGE_LIMIT
    stream_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _required_text(self.subscription_id, "subscription_id"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )
        object.__setattr__(self, "lease_owner", _required_text(self.lease_owner, "lease_owner"))
        object.__setattr__(
            self,
            "requested_at",
            _required_utc(self.requested_at, "requested_at"),
        )
        lease = LeasePolicy(self.lease_duration_seconds)
        object.__setattr__(self, "lease_duration_seconds", lease.duration_seconds)
        object.__setattr__(self, "limit", _page_limit(self.limit))
        object.__setattr__(self, "stream_id", _optional_text(self.stream_id, "stream_id"))


@dataclass(frozen=True, slots=True)
class DeliveryLeaseToken:
    delivery_id: str
    delivery_generation: int
    lease_owner: str
    lease_generation: int
    lease_expires_at: datetime
    lease_started_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "delivery_id", _required_text(self.delivery_id, "delivery_id"))
        object.__setattr__(
            self,
            "delivery_generation",
            _positive_int(self.delivery_generation, "delivery_generation"),
        )
        object.__setattr__(self, "lease_owner", _required_text(self.lease_owner, "lease_owner"))
        object.__setattr__(
            self,
            "lease_generation",
            _positive_int(self.lease_generation, "lease_generation"),
        )
        object.__setattr__(
            self,
            "lease_expires_at",
            _required_utc(self.lease_expires_at, "lease_expires_at"),
        )
        lease_started_at = _utc(self.lease_started_at, "lease_started_at")
        if (
            lease_started_at is not None
            and lease_started_at >= self.lease_expires_at
        ):
            raise ValueError("lease_started_at must precede lease_expires_at")
        object.__setattr__(self, "lease_started_at", lease_started_at)


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    delivery: DeliveryRecord
    event: StoredEvent
    lease: DeliveryLeaseToken

    def __post_init__(self) -> None:
        if not isinstance(self.delivery, DeliveryRecord):
            raise ValueError("delivery must be DeliveryRecord")
        if not isinstance(self.event, StoredEvent):
            raise ValueError("event must be StoredEvent")
        if not isinstance(self.lease, DeliveryLeaseToken):
            raise ValueError("lease must be DeliveryLeaseToken")
        if self.delivery.state is not DeliveryState.CLAIMED:
            raise ValueError("claimed delivery must have CLAIMED state")
        if self.delivery.event_id != self.event.event_id:
            raise ValueError("delivery and event event_id must match")
        if self.delivery.stream_id != self.event.stream_id:
            raise ValueError("delivery and event stream_id must match")
        if self.delivery.stream_sequence != self.event.stream_sequence:
            raise ValueError("delivery and event stream_sequence must match")
        if self.delivery.delivery_id != self.lease.delivery_id:
            raise ValueError("delivery and lease delivery_id must match")
        if self.delivery.delivery_generation != self.lease.delivery_generation:
            raise ValueError("delivery and lease generation must match")
        if self.delivery.lease_owner != self.lease.lease_owner:
            raise ValueError("delivery and lease owner must match")
        if self.delivery.lease_generation != self.lease.lease_generation:
            raise ValueError("delivery and lease fencing generation must match")
        if self.delivery.lease_expires_at != self.lease.lease_expires_at:
            raise ValueError("delivery and lease expiry must match")
        if (
            self.lease.lease_started_at is not None
            and self.lease.lease_expires_at <= self.lease.lease_started_at
        ):
            raise ValueError("claimed delivery has an invalid lease time range")


@dataclass(frozen=True, slots=True)
class InboxKey:
    event_id: str
    consumer_effect_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "consumer_effect_id",
            _required_text(self.consumer_effect_id, "consumer_effect_id"),
        )


@dataclass(frozen=True, slots=True)
class InboxEntry:
    event_id: str
    consumer_effect_id: str
    completed_at: datetime
    delivery_id: str | None = None
    result_checksum: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "consumer_effect_id",
            _required_text(self.consumer_effect_id, "consumer_effect_id"),
        )
        object.__setattr__(
            self,
            "completed_at",
            _required_utc(self.completed_at, "completed_at"),
        )
        object.__setattr__(self, "delivery_id", _optional_text(self.delivery_id, "delivery_id"))
        object.__setattr__(
            self,
            "result_checksum",
            _checksum(self.result_checksum, "result_checksum"),
        )

    @property
    def key(self) -> InboxKey:
        return InboxKey(self.event_id, self.consumer_effect_id)


@dataclass(frozen=True, slots=True)
class CheckpointKey:
    subscription_id: str
    subscription_version: int
    stream_id: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _required_text(self.subscription_id, "subscription_id"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )
        object.__setattr__(self, "stream_id", _required_text(self.stream_id, "stream_id"))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))


@dataclass(frozen=True, slots=True)
class ConsumerCheckpoint:
    subscription_id: str
    subscription_version: int
    stream_id: str
    highest_contiguous_terminal_sequence: int | None
    last_event_id: str | None
    terminal_disposition: DeliveryState | None
    updated_at: datetime
    checksum: str
    checkpoint_version: int = 1
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        key = CheckpointKey(
            self.subscription_id,
            self.subscription_version,
            self.stream_id,
            self.tenant_id,
        )
        sequence = _sequence(
            self.highest_contiguous_terminal_sequence,
            "highest_contiguous_terminal_sequence",
        )
        event_id = _optional_text(self.last_event_id, "last_event_id")
        disposition = (
            None
            if self.terminal_disposition is None
            else DeliveryState(self.terminal_disposition)
        )
        if (sequence is None) != (event_id is None):
            raise ValueError("checkpoint sequence and last_event_id must both be set or absent")
        if sequence is None and disposition is not None:
            raise ValueError("empty checkpoint cannot have a terminal disposition")
        if disposition is not None and not disposition.is_terminal:
            raise ValueError("checkpoint disposition must be terminal")
        if sequence is not None and disposition is None:
            raise ValueError("non-empty checkpoint requires a terminal disposition")
        object.__setattr__(self, "subscription_id", key.subscription_id)
        object.__setattr__(self, "subscription_version", key.subscription_version)
        object.__setattr__(self, "stream_id", key.stream_id)
        object.__setattr__(self, "tenant_id", key.tenant_id)
        object.__setattr__(self, "highest_contiguous_terminal_sequence", sequence)
        object.__setattr__(self, "last_event_id", event_id)
        object.__setattr__(self, "terminal_disposition", disposition)
        object.__setattr__(
            self,
            "updated_at",
            _required_utc(self.updated_at, "updated_at"),
        )
        object.__setattr__(
            self,
            "checksum",
            _checksum(self.checksum, "checksum", required=True),
        )
        object.__setattr__(
            self,
            "checkpoint_version",
            _positive_int(self.checkpoint_version, "checkpoint_version"),
        )

    @property
    def key(self) -> CheckpointKey:
        return CheckpointKey(
            self.subscription_id,
            self.subscription_version,
            self.stream_id,
            self.tenant_id,
        )


@dataclass(frozen=True, slots=True)
class CheckpointQuery:
    subscription_id: str | None = None
    subscription_version: int | None = None
    stream_id: str | None = None
    tenant_id: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _optional_text(self.subscription_id, "subscription_id"),
        )
        if self.subscription_version is not None:
            object.__setattr__(
                self,
                "subscription_version",
                _positive_int(self.subscription_version, "subscription_version"),
            )
        if self.subscription_version is not None and self.subscription_id is None:
            raise ValueError("subscription_version requires subscription_id")
        object.__setattr__(self, "stream_id", _optional_text(self.stream_id, "stream_id"))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class CheckpointPage:
    checkpoints: tuple[ConsumerCheckpoint, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checkpoints",
            _typed_tuple(
                self.checkpoints,
                ConsumerCheckpoint,
                "checkpoints",
            ),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class DeliverySettlement:
    lease: DeliveryLeaseToken
    target_state: DeliveryState
    settled_at: datetime
    reason_class: str | None = None
    redacted_diagnostic: str | None = None
    retry_available_at: datetime | None = None
    inbox_entry: InboxEntry | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, DeliveryLeaseToken):
            raise ValueError("lease must be DeliveryLeaseToken")
        if self.inbox_entry is not None and not isinstance(self.inbox_entry, InboxEntry):
            raise ValueError("inbox_entry must be InboxEntry")
        target = DeliveryState(self.target_state)
        if target not in {
            DeliveryState.ACKED,
            DeliveryState.DROPPED,
            DeliveryState.RETRY_WAIT,
            DeliveryState.DEAD_LETTER,
        }:
            raise ValueError("delivery settlement target must be terminal or RETRY_WAIT")
        reason = _reason_class(self.reason_class)
        settled_at = _required_utc(self.settled_at, "settled_at")
        retry_at = _utc(self.retry_available_at, "retry_available_at")
        if target is DeliveryState.RETRY_WAIT and retry_at is None:
            raise ValueError("RETRY_WAIT settlement requires retry_available_at")
        if target is not DeliveryState.RETRY_WAIT and retry_at is not None:
            raise ValueError("retry_available_at is only valid for RETRY_WAIT")
        if retry_at is not None and retry_at <= settled_at:
            raise ValueError("retry_available_at must be after settled_at")
        if target in {
            DeliveryState.RETRY_WAIT,
            DeliveryState.DROPPED,
            DeliveryState.DEAD_LETTER,
        } and reason is None:
            raise ValueError(f"{target.value} settlement requires reason_class")
        if self.inbox_entry is not None and target is not DeliveryState.ACKED:
            raise ValueError("inbox_entry is only valid for ACKED settlement")
        object.__setattr__(self, "target_state", target)
        object.__setattr__(
            self,
            "settled_at",
            settled_at,
        )
        object.__setattr__(self, "reason_class", reason)
        object.__setattr__(self, "redacted_diagnostic", _diagnostic(self.redacted_diagnostic))
        object.__setattr__(self, "retry_available_at", retry_at)


@dataclass(frozen=True, slots=True)
class AppendResult:
    event: StoredEvent
    created: bool
    pending_delivery_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.event, StoredEvent):
            raise ValueError("event must be StoredEvent")
        object.__setattr__(self, "created", _boolean(self.created, "created"))
        object.__setattr__(
            self,
            "pending_delivery_count",
            _nonnegative_int(self.pending_delivery_count, "pending_delivery_count"),
        )
        if not self.created and self.pending_delivery_count != 0:
            raise ValueError("idempotent append cannot materialize new delivery rows")


@dataclass(frozen=True, slots=True)
class DeliverySettlementResult:
    delivery: DeliveryRecord
    checkpoint: ConsumerCheckpoint | None = None
    dead_letter_id: str | None = None
    inbox_recorded: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.delivery, DeliveryRecord):
            raise ValueError("delivery must be DeliveryRecord")
        if self.checkpoint is not None and not isinstance(
            self.checkpoint,
            ConsumerCheckpoint,
        ):
            raise ValueError("checkpoint must be ConsumerCheckpoint")
        object.__setattr__(
            self,
            "inbox_recorded",
            _boolean(self.inbox_recorded, "inbox_recorded"),
        )
        object.__setattr__(
            self,
            "dead_letter_id",
            _optional_text(self.dead_letter_id, "dead_letter_id"),
        )
        if self.delivery.state is DeliveryState.DEAD_LETTER and self.dead_letter_id is None:
            raise ValueError("DEAD_LETTER settlement requires dead_letter_id")
        if self.delivery.state is not DeliveryState.DEAD_LETTER and self.dead_letter_id is not None:
            raise ValueError("dead_letter_id is only valid for DEAD_LETTER state")


@dataclass(frozen=True, slots=True)
class PendingDeliveryStats:
    pending_count: int
    lag: int
    oldest_pending_at: datetime | None = None
    oldest_pending_age_seconds: float | None = None
    late_repair_pending_count: int = 0
    warning_threshold_reached: bool = False
    capacity_remaining: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pending_count",
            _nonnegative_int(self.pending_count, "pending_count"),
        )
        object.__setattr__(self, "lag", _nonnegative_int(self.lag, "lag"))
        late_repair_pending_count = _nonnegative_int(
            self.late_repair_pending_count,
            "late_repair_pending_count",
        )
        if late_repair_pending_count > self.pending_count:
            raise ValueError(
                "late_repair_pending_count cannot exceed pending_count"
            )
        if self.lag + late_repair_pending_count != self.pending_count:
            raise ValueError(
                "lag plus late_repair_pending_count must equal pending_count"
            )
        object.__setattr__(
            self,
            "late_repair_pending_count",
            late_repair_pending_count,
        )
        object.__setattr__(
            self,
            "oldest_pending_at",
            _utc(self.oldest_pending_at, "oldest_pending_at"),
        )
        oldest_pending_age_seconds = (
            None
            if self.oldest_pending_age_seconds is None
            else _nonnegative_float(
                self.oldest_pending_age_seconds,
                "oldest_pending_age_seconds",
            )
        )
        object.__setattr__(
            self,
            "oldest_pending_age_seconds",
            oldest_pending_age_seconds,
        )
        object.__setattr__(
            self,
            "warning_threshold_reached",
            _boolean(
                self.warning_threshold_reached,
                "warning_threshold_reached",
            ),
        )
        capacity_remaining = (
            None
            if self.capacity_remaining is None
            else _nonnegative_int(self.capacity_remaining, "capacity_remaining")
        )
        object.__setattr__(self, "capacity_remaining", capacity_remaining)
        if self.pending_count == 0 and (
            self.oldest_pending_at is not None
            or oldest_pending_age_seconds is not None
        ):
            raise ValueError(
                "empty pending set cannot have oldest pending time or age"
            )
        if (
            oldest_pending_age_seconds is not None
            and self.oldest_pending_at is None
        ):
            raise ValueError(
                "oldest_pending_age_seconds requires oldest_pending_at"
            )


@dataclass(frozen=True, slots=True)
class DeadLetterRecord:
    dead_letter_id: str
    delivery_id: str
    event_id: str
    stream_id: str
    stream_sequence: int
    subscription_id: str
    subscription_version: int
    consumer_id: str
    consumer_effect_id: str | None
    delivery_generation: int
    attempt_count: int
    first_failure_at: datetime
    last_failure_at: datetime
    reason_class: str
    redacted_diagnostic: str | None = None
    tenant_id: str | None = None
    disposition: DeadLetterDisposition = DeadLetterDisposition.OPEN
    operator_id: str | None = None
    operator_reason: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "dead_letter_id",
            "delivery_id",
            "event_id",
            "stream_id",
            "subscription_id",
            "consumer_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "reason_class",
            _reason_class(self.reason_class, required=True),
        )
        object.__setattr__(
            self,
            "stream_sequence",
            _positive_int(self.stream_sequence, "stream_sequence"),
        )
        object.__setattr__(
            self,
            "subscription_version",
            _positive_int(self.subscription_version, "subscription_version"),
        )
        object.__setattr__(
            self,
            "delivery_generation",
            _positive_int(self.delivery_generation, "delivery_generation"),
        )
        object.__setattr__(
            self,
            "attempt_count",
            _positive_int(self.attempt_count, "attempt_count"),
        )
        first_failure_at = _required_utc(self.first_failure_at, "first_failure_at")
        last_failure_at = _required_utc(self.last_failure_at, "last_failure_at")
        if last_failure_at < first_failure_at:
            raise ValueError("last_failure_at cannot precede first_failure_at")
        object.__setattr__(self, "first_failure_at", first_failure_at)
        object.__setattr__(self, "last_failure_at", last_failure_at)
        object.__setattr__(
            self,
            "consumer_effect_id",
            _optional_text(self.consumer_effect_id, "consumer_effect_id"),
        )
        object.__setattr__(self, "redacted_diagnostic", _diagnostic(self.redacted_diagnostic))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "disposition", DeadLetterDisposition(self.disposition))
        object.__setattr__(self, "operator_id", _optional_text(self.operator_id, "operator_id"))
        object.__setattr__(
            self,
            "operator_reason",
            _optional_text(self.operator_reason, "operator_reason"),
        )
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        operator_fields = (self.operator_id, self.operator_reason, self.updated_at)
        if self.disposition is DeadLetterDisposition.OPEN and any(
            value is not None for value in operator_fields
        ):
            raise ValueError("OPEN dead letter cannot have an operator disposition")
        if self.disposition is not DeadLetterDisposition.OPEN and not all(
            value is not None for value in operator_fields
        ):
            raise ValueError("terminal dead-letter disposition requires operator audit fields")


@dataclass(frozen=True, slots=True)
class DeadLetterQuery:
    subscription_id: str | None = None
    subscription_version: int | None = None
    tenant_id: str | None = None
    disposition: DeadLetterDisposition | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "subscription_id",
            _optional_text(self.subscription_id, "subscription_id"),
        )
        if self.subscription_version is not None:
            object.__setattr__(
                self,
                "subscription_version",
                _positive_int(self.subscription_version, "subscription_version"),
            )
        if self.subscription_version is not None and self.subscription_id is None:
            raise ValueError("subscription_version requires subscription_id")
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        if self.disposition is not None:
            object.__setattr__(
                self,
                "disposition",
                DeadLetterDisposition(self.disposition),
            )
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class DeadLetterPage:
    records: tuple[DeadLetterRecord, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            _typed_tuple(self.records, DeadLetterRecord, "records"),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class DeadLetterAction:
    dead_letter_id: str
    operator_id: str
    reason: str
    requested_at: datetime
    idempotency_ready: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "dead_letter_id",
            _required_text(self.dead_letter_id, "dead_letter_id"),
        )
        object.__setattr__(self, "operator_id", _required_text(self.operator_id, "operator_id"))
        object.__setattr__(self, "reason", _required_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "requested_at",
            _required_utc(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "idempotency_ready",
            _boolean(self.idempotency_ready, "idempotency_ready"),
        )


@dataclass(frozen=True, slots=True)
class RetirementCancellationRequest:
    """Authorized bounded command for terminally cancelling retired work."""

    cancellation_id: str
    subscription: SubscriptionKey
    requested_at: datetime
    operator_id: str
    operator_reason: str
    authorization_evidence_ref: str
    tenant_id: str | None = None
    limit: int = MAX_RETIREMENT_CANCELLATION_ITEMS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cancellation_id",
            _required_text(self.cancellation_id, "cancellation_id"),
        )
        if not isinstance(self.subscription, SubscriptionKey):
            raise ValueError("subscription must be SubscriptionKey")
        object.__setattr__(
            self,
            "requested_at",
            _required_utc(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "operator_id",
            _required_text(self.operator_id, "operator_id"),
        )
        object.__setattr__(
            self,
            "operator_reason",
            _required_text(self.operator_reason, "operator_reason"),
        )
        evidence_ref = _required_text(
            self.authorization_evidence_ref,
            "authorization_evidence_ref",
        )
        if len(evidence_ref) > MAX_AUTHORIZATION_EVIDENCE_REF_LENGTH:
            raise ValueError(
                "authorization_evidence_ref exceeds the bounded audit reference limit"
            )
        object.__setattr__(self, "authorization_evidence_ref", evidence_ref)
        object.__setattr__(
            self,
            "tenant_id",
            _optional_text(self.tenant_id, "tenant_id"),
        )
        limit = _positive_int(self.limit, "limit")
        if limit > MAX_RETIREMENT_CANCELLATION_ITEMS:
            raise ValueError(
                "retirement cancellation limit cannot exceed "
                f"{MAX_RETIREMENT_CANCELLATION_ITEMS}"
            )
        object.__setattr__(self, "limit", limit)


@dataclass(frozen=True, slots=True)
class RetirementCancellationItem:
    """Per-delivery audit disposition produced by a cancellation command."""

    cancellation_id: str
    delivery_id: str
    event_id: str
    stream_id: str
    stream_sequence: int
    subscription: SubscriptionKey
    delivery_generation: int
    previous_state: DeliveryState
    previous_attempt_count: int
    cancelled_at: datetime
    previous_reason_class: str | None = None
    terminal_state: DeliveryState = DeliveryState.DROPPED
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "cancellation_id",
            "delivery_id",
            "event_id",
            "stream_id",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.subscription, SubscriptionKey):
            raise ValueError("subscription must be SubscriptionKey")
        object.__setattr__(
            self,
            "stream_sequence",
            _positive_int(self.stream_sequence, "stream_sequence"),
        )
        object.__setattr__(
            self,
            "delivery_generation",
            _positive_int(self.delivery_generation, "delivery_generation"),
        )
        previous_state = DeliveryState(self.previous_state)
        if previous_state.is_terminal:
            raise ValueError("retirement cancellation item requires a nonterminal prior state")
        object.__setattr__(self, "previous_state", previous_state)
        previous_attempt_count = _nonnegative_int(
            self.previous_attempt_count,
            "previous_attempt_count",
        )
        if (
            previous_state is DeliveryState.PENDING
            and previous_attempt_count != 0
        ) or (
            previous_state in {DeliveryState.CLAIMED, DeliveryState.RETRY_WAIT}
            and previous_attempt_count < 1
        ):
            raise ValueError(
                "retirement cancellation prior state and attempt count disagree"
            )
        object.__setattr__(
            self,
            "previous_attempt_count",
            previous_attempt_count,
        )
        terminal_state = DeliveryState(self.terminal_state)
        if terminal_state is not DeliveryState.DROPPED:
            raise ValueError("retirement cancellation terminal state must be DROPPED")
        object.__setattr__(self, "terminal_state", terminal_state)
        object.__setattr__(
            self,
            "previous_reason_class",
            _reason_class(self.previous_reason_class),
        )
        object.__setattr__(
            self,
            "cancelled_at",
            _required_utc(self.cancelled_at, "cancelled_at"),
        )
        object.__setattr__(
            self,
            "tenant_id",
            _optional_text(self.tenant_id, "tenant_id"),
        )


@dataclass(frozen=True, slots=True)
class RetirementCancellationReport:
    """Durable audit for one bounded terminal-cancellation transaction."""

    cancellation_id: str
    subscription: SubscriptionKey
    requested_at: datetime
    cancelled_at: datetime
    operator_id: str
    operator_reason: str
    authorization_evidence_ref: str
    item_limit: int
    remaining_nonterminal_count: int
    items: tuple[RetirementCancellationItem, ...]
    remaining_nonterminal_count_truncated: bool = False
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cancellation_id",
            _required_text(self.cancellation_id, "cancellation_id"),
        )
        if not isinstance(self.subscription, SubscriptionKey):
            raise ValueError("subscription must be SubscriptionKey")
        requested_at = _required_utc(self.requested_at, "requested_at")
        cancelled_at = _required_utc(self.cancelled_at, "cancelled_at")
        if cancelled_at < requested_at:
            raise ValueError("cancelled_at cannot precede requested_at")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "cancelled_at", cancelled_at)
        object.__setattr__(
            self,
            "operator_id",
            _required_text(self.operator_id, "operator_id"),
        )
        object.__setattr__(
            self,
            "operator_reason",
            _required_text(self.operator_reason, "operator_reason"),
        )
        evidence_ref = _required_text(
            self.authorization_evidence_ref,
            "authorization_evidence_ref",
        )
        if len(evidence_ref) > MAX_AUTHORIZATION_EVIDENCE_REF_LENGTH:
            raise ValueError(
                "authorization_evidence_ref exceeds the bounded audit reference limit"
            )
        object.__setattr__(self, "authorization_evidence_ref", evidence_ref)
        item_limit = _positive_int(self.item_limit, "item_limit")
        if item_limit > MAX_RETIREMENT_CANCELLATION_ITEMS:
            raise ValueError(
                "retirement cancellation item_limit cannot exceed "
                f"{MAX_RETIREMENT_CANCELLATION_ITEMS}"
            )
        object.__setattr__(self, "item_limit", item_limit)
        remaining_count = _nonnegative_int(
            self.remaining_nonterminal_count,
            "remaining_nonterminal_count",
        )
        count_truncated = _boolean(
            self.remaining_nonterminal_count_truncated,
            "remaining_nonterminal_count_truncated",
        )
        if count_truncated:
            if remaining_count != item_limit + 1:
                raise ValueError(
                    "truncated remaining count must be the item_limit plus one"
                )
        elif remaining_count > item_limit:
            raise ValueError(
                "exact remaining count cannot exceed the bounded item_limit"
            )
        object.__setattr__(self, "remaining_nonterminal_count", remaining_count)
        object.__setattr__(
            self,
            "remaining_nonterminal_count_truncated",
            count_truncated,
        )
        tenant_id = _optional_text(self.tenant_id, "tenant_id")
        object.__setattr__(self, "tenant_id", tenant_id)
        items = _typed_tuple(
            self.items,
            RetirementCancellationItem,
            "items",
        )
        if len(items) > item_limit:
            raise ValueError("retirement cancellation report exceeded its item_limit")
        ordering = tuple(
            (
                stable_text_order_key(item.stream_id),
                item.stream_sequence,
                item.delivery_generation,
                stable_text_order_key(item.delivery_id),
            )
            for item in items
        )
        if ordering != tuple(sorted(ordering)) or len(
            {item.delivery_id for item in items}
        ) != len(items):
            raise ValueError(
                "retirement cancellation items must be unique and deterministically ordered"
            )
        for item in items:
            if (
                item.cancellation_id != self.cancellation_id
                or item.subscription != self.subscription
                or item.cancelled_at != cancelled_at
                or item.tenant_id != tenant_id
            ):
                raise ValueError(
                    "retirement cancellation item falls outside its report identity"
                )
        object.__setattr__(self, "items", items)

    @property
    def cancelled_count(self) -> int:
        return len(self.items)

    @property
    def completed(self) -> bool:
        return self.remaining_nonterminal_count == 0


@dataclass(frozen=True, slots=True)
class RedeliveryRequest:
    """Authorized command selecting a finite event-consumer range.

    ``authorization_evidence_ref`` identifies an application-owned authorization
    decision.  It is audit evidence, not an idempotency capability; the calling
    runtime must verify ``AutomaticDeliveryOperation.REDELIVERY`` before this
    command reaches a store.
    """

    redelivery_id: str
    subscription: SubscriptionKey
    source_stream_id: str
    from_sequence: int
    requested_at: datetime
    operator_id: str
    operator_reason: str
    authorization_evidence_ref: str
    through_sequence: int | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "redelivery_id",
            _required_text(self.redelivery_id, "redelivery_id"),
        )
        if not isinstance(self.subscription, SubscriptionKey):
            raise ValueError("subscription must be SubscriptionKey")
        object.__setattr__(
            self,
            "source_stream_id",
            _required_text(self.source_stream_id, "source_stream_id"),
        )
        start = _positive_int(self.from_sequence, "from_sequence")
        end = _sequence(self.through_sequence, "through_sequence")
        if end is not None and end < start:
            raise ValueError("through_sequence cannot precede from_sequence")
        if end is not None and end - start + 1 > MAX_REDELIVERY_ITEMS:
            raise ValueError(
                "redelivery range cannot exceed "
                f"{MAX_REDELIVERY_ITEMS} stream positions"
            )
        object.__setattr__(self, "from_sequence", start)
        object.__setattr__(self, "through_sequence", end)
        object.__setattr__(
            self,
            "requested_at",
            _required_utc(self.requested_at, "requested_at"),
        )
        object.__setattr__(self, "operator_id", _required_text(self.operator_id, "operator_id"))
        object.__setattr__(
            self,
            "operator_reason",
            _required_text(self.operator_reason, "operator_reason"),
        )
        evidence_ref = _required_text(
            self.authorization_evidence_ref,
            "authorization_evidence_ref",
        )
        if len(evidence_ref) > MAX_AUTHORIZATION_EVIDENCE_REF_LENGTH:
            raise ValueError(
                "authorization_evidence_ref exceeds the bounded audit reference limit"
            )
        object.__setattr__(self, "authorization_evidence_ref", evidence_ref)
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))


@dataclass(frozen=True, slots=True)
class RedeliveryItem:
    redelivery_id: str
    event_id: str
    stream_id: str
    stream_sequence: int
    subscription: SubscriptionKey
    delivery_id: str
    delivery_generation: int
    created_at: datetime
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("redelivery_id", "event_id", "stream_id", "delivery_id"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )
        if not isinstance(self.subscription, SubscriptionKey):
            raise ValueError("subscription must be SubscriptionKey")
        object.__setattr__(
            self,
            "stream_sequence",
            _positive_int(self.stream_sequence, "stream_sequence"),
        )
        object.__setattr__(
            self,
            "delivery_generation",
            _positive_int(self.delivery_generation, "delivery_generation"),
        )
        object.__setattr__(
            self,
            "created_at",
            _required_utc(self.created_at, "created_at"),
        )
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))


@dataclass(frozen=True, slots=True)
class RedeliveryReport:
    """Durable audit result for one atomically materialized redelivery command."""

    redelivery_id: str
    subscription: SubscriptionKey
    source_stream_id: str
    from_sequence: int
    through_sequence: int
    captured_high_watermark: int
    requested_at: datetime
    scheduled_at: datetime
    operator_id: str
    operator_reason: str
    authorization_evidence_ref: str
    items: tuple[RedeliveryItem, ...]
    requested_through_sequence: int | None = None
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "redelivery_id",
            _required_text(self.redelivery_id, "redelivery_id"),
        )
        if not isinstance(self.subscription, SubscriptionKey):
            raise ValueError("subscription must be SubscriptionKey")
        object.__setattr__(
            self,
            "source_stream_id",
            _required_text(self.source_stream_id, "source_stream_id"),
        )
        start = _positive_int(self.from_sequence, "from_sequence")
        end = _positive_int(self.through_sequence, "through_sequence")
        requested_end = _sequence(
            self.requested_through_sequence,
            "requested_through_sequence",
        )
        high = _positive_int(
            self.captured_high_watermark,
            "captured_high_watermark",
        )
        if end < start:
            raise ValueError("through_sequence cannot precede from_sequence")
        if requested_end is not None and requested_end != end:
            raise ValueError("requested through_sequence must match the scheduled range")
        if end > high:
            raise ValueError("redelivery range cannot exceed captured_high_watermark")
        object.__setattr__(self, "from_sequence", start)
        object.__setattr__(self, "through_sequence", end)
        object.__setattr__(self, "requested_through_sequence", requested_end)
        object.__setattr__(self, "captured_high_watermark", high)
        requested_at = _required_utc(self.requested_at, "requested_at")
        scheduled_at = _required_utc(self.scheduled_at, "scheduled_at")
        if scheduled_at < requested_at:
            raise ValueError("scheduled_at cannot precede requested_at")
        object.__setattr__(self, "requested_at", requested_at)
        object.__setattr__(self, "scheduled_at", scheduled_at)
        object.__setattr__(self, "operator_id", _required_text(self.operator_id, "operator_id"))
        object.__setattr__(
            self,
            "operator_reason",
            _required_text(self.operator_reason, "operator_reason"),
        )
        evidence_ref = _required_text(
            self.authorization_evidence_ref,
            "authorization_evidence_ref",
        )
        if len(evidence_ref) > MAX_AUTHORIZATION_EVIDENCE_REF_LENGTH:
            raise ValueError(
                "authorization_evidence_ref exceeds the bounded audit reference limit"
            )
        object.__setattr__(self, "authorization_evidence_ref", evidence_ref)
        tenant_id = _optional_text(self.tenant_id, "tenant_id")
        object.__setattr__(self, "tenant_id", tenant_id)
        items = _typed_tuple(self.items, RedeliveryItem, "items")
        if not items:
            raise ValueError("redelivery report requires at least one scheduled item")
        if len(items) > MAX_REDELIVERY_ITEMS:
            raise ValueError(
                "redelivery report cannot exceed "
                f"{MAX_REDELIVERY_ITEMS} scheduled items"
            )
        sequences = tuple(item.stream_sequence for item in items)
        if sequences != tuple(sorted(sequences)) or len(set(sequences)) != len(sequences):
            raise ValueError("redelivery items must be strictly ordered by stream_sequence")
        if len({item.event_id for item in items}) != len(items):
            raise ValueError("redelivery report cannot schedule one event pair twice")
        for item in items:
            if (
                item.redelivery_id != self.redelivery_id
                or item.subscription != self.subscription
                or item.stream_id != self.source_stream_id
                or item.tenant_id != tenant_id
                or not start <= item.stream_sequence <= end
            ):
                raise ValueError("redelivery item falls outside its report identity")
        object.__setattr__(self, "items", items)

    @property
    def scheduled_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    quarantine_id: str
    source: str
    reason: QuarantineReason
    created_at: datetime
    envelope_schema: str | None = None
    event_type: str | None = None
    data_schema: str | None = None
    tenant_id: str | None = None
    redacted_diagnostic: str | None = None
    disposition: QuarantineDisposition = QuarantineDisposition.PENDING
    operator_id: str | None = None
    operator_reason: str | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "quarantine_id",
            _required_text(self.quarantine_id, "quarantine_id"),
        )
        object.__setattr__(self, "source", _required_text(self.source, "source"))
        object.__setattr__(self, "reason", QuarantineReason(self.reason))
        object.__setattr__(
            self,
            "created_at",
            _required_utc(self.created_at, "created_at"),
        )
        for field_name in (
            "envelope_schema",
            "event_type",
            "data_schema",
            "tenant_id",
            "operator_id",
            "operator_reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "redacted_diagnostic", _diagnostic(self.redacted_diagnostic))
        object.__setattr__(self, "disposition", QuarantineDisposition(self.disposition))
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))
        operator_fields = (self.operator_id, self.operator_reason, self.updated_at)
        if self.disposition is QuarantineDisposition.PENDING and any(
            value is not None for value in operator_fields
        ):
            raise ValueError("PENDING quarantine cannot have an operator disposition")
        if self.disposition is not QuarantineDisposition.PENDING and not all(
            value is not None for value in operator_fields
        ):
            raise ValueError("resolved quarantine requires operator audit fields")


@dataclass(frozen=True, slots=True)
class QuarantineQuery:
    reason: QuarantineReason | None = None
    tenant_id: str | None = None
    disposition: QuarantineDisposition | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        if self.reason is not None:
            object.__setattr__(self, "reason", QuarantineReason(self.reason))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        if self.disposition is not None:
            object.__setattr__(
                self,
                "disposition",
                QuarantineDisposition(self.disposition),
            )
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class QuarantinePage:
    records: tuple[QuarantineRecord, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "records",
            _typed_tuple(self.records, QuarantineRecord, "records"),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


@dataclass(frozen=True, slots=True)
class ReplayVersion:
    component: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "component", _required_text(self.component, "component"))
        object.__setattr__(self, "version", _required_text(self.version, "version"))


@dataclass(frozen=True, slots=True)
class ReplayStartRequest:
    """Command whose durable result fixes one finite source prefix."""

    replay_id: str
    mode: ReplayMode
    source_stream_id: str
    requested_at: datetime
    from_sequence: int | None = None
    checkpoint_ref: str | None = None
    tenant_id: str | None = None
    operator_id: str | None = None
    operator_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "replay_id", _required_text(self.replay_id, "replay_id"))
        object.__setattr__(self, "mode", ReplayMode(self.mode))
        object.__setattr__(
            self,
            "source_stream_id",
            _required_text(self.source_stream_id, "source_stream_id"),
        )
        object.__setattr__(
            self,
            "requested_at",
            _required_utc(self.requested_at, "requested_at"),
        )
        object.__setattr__(
            self,
            "from_sequence",
            _sequence(self.from_sequence, "from_sequence"),
        )
        for field_name in (
            "checkpoint_ref",
            "tenant_id",
            "operator_id",
            "operator_reason",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        if self.mode is ReplayMode.REDELIVER and (
            self.operator_id is None or self.operator_reason is None
        ):
            raise ValueError("REDELIVER replay requires operator context")


@dataclass(frozen=True, slots=True)
class ReplayReport:
    replay_id: str
    mode: ReplayMode
    source_stream_id: str
    high_watermark: int
    status: ReplayStatus
    started_at: datetime
    from_sequence: int | None = None
    to_sequence: int | None = None
    checkpoint_ref: str | None = None
    versions: tuple[ReplayVersion, ...] = ()
    applied_upcasters: tuple[str, ...] = ()
    quarantine_refs: tuple[str, ...] = ()
    mismatch_sequence: int | None = None
    reason_class: str | None = None
    result_checksum: str | None = None
    finished_at: datetime | None = None
    tenant_id: str | None = None
    operator_id: str | None = None
    operator_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "replay_id", _required_text(self.replay_id, "replay_id"))
        object.__setattr__(self, "mode", ReplayMode(self.mode))
        object.__setattr__(
            self,
            "source_stream_id",
            _required_text(self.source_stream_id, "source_stream_id"),
        )
        high = _nonnegative_int(self.high_watermark, "high_watermark")
        start = _sequence(self.from_sequence, "from_sequence")
        end = _sequence(self.to_sequence, "to_sequence")
        mismatch = _sequence(self.mismatch_sequence, "mismatch_sequence")
        if start is not None and end is not None and end < start:
            raise ValueError("to_sequence cannot precede from_sequence")
        if start is not None and start > high:
            raise ValueError("from_sequence cannot exceed high_watermark")
        if end is not None and end > high:
            raise ValueError("to_sequence cannot exceed high_watermark")
        if mismatch is not None and mismatch > high:
            raise ValueError("mismatch_sequence cannot exceed high_watermark")
        object.__setattr__(self, "high_watermark", high)
        object.__setattr__(self, "from_sequence", start)
        object.__setattr__(self, "to_sequence", end)
        object.__setattr__(self, "mismatch_sequence", mismatch)
        object.__setattr__(self, "status", ReplayStatus(self.status))
        object.__setattr__(
            self,
            "started_at",
            _required_utc(self.started_at, "started_at"),
        )
        object.__setattr__(self, "finished_at", _utc(self.finished_at, "finished_at"))
        if self.status in {ReplayStatus.SUCCEEDED, ReplayStatus.FAILED} and self.finished_at is None:
            raise ValueError("terminal replay report requires finished_at")
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        object.__setattr__(
            self,
            "checkpoint_ref",
            _optional_text(self.checkpoint_ref, "checkpoint_ref"),
        )
        object.__setattr__(
            self,
            "versions",
            _typed_tuple(self.versions, ReplayVersion, "versions"),
        )
        object.__setattr__(
            self,
            "applied_upcasters",
            tuple(_required_text(value, "applied_upcaster") for value in self.applied_upcasters),
        )
        object.__setattr__(
            self,
            "quarantine_refs",
            tuple(_required_text(value, "quarantine_ref") for value in self.quarantine_refs),
        )
        for field_name in ("tenant_id", "operator_id", "operator_reason"):
            object.__setattr__(
                self,
                field_name,
                _optional_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "reason_class", _reason_class(self.reason_class))
        object.__setattr__(
            self,
            "result_checksum",
            _checksum(self.result_checksum, "result_checksum"),
        )
        if self.status is ReplayStatus.SUCCEEDED:
            if self.result_checksum is None:
                raise ValueError("successful replay report requires result_checksum")
            if self.reason_class is not None or self.mismatch_sequence is not None:
                raise ValueError("successful replay report cannot contain failure details")
        elif self.status is ReplayStatus.FAILED:
            if self.reason_class is None:
                raise ValueError("failed replay report requires reason_class")
            if self.result_checksum is not None:
                raise ValueError("failed replay report cannot contain result_checksum")
        elif any(
            value is not None
            for value in (
                self.finished_at,
                self.reason_class,
                self.result_checksum,
                self.mismatch_sequence,
            )
        ):
            raise ValueError("nonterminal replay report cannot contain terminal fields")
        if self.mode is ReplayMode.REDELIVER and (
            self.operator_id is None or self.operator_reason is None
        ):
            raise ValueError("REDELIVER replay report requires operator context")


@dataclass(frozen=True, slots=True)
class ReplayReportQuery:
    source_stream_id: str | None = None
    mode: ReplayMode | None = None
    status: ReplayStatus | None = None
    tenant_id: str | None = None
    cursor: str | None = None
    limit: int = DEFAULT_PAGE_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_stream_id",
            _optional_text(self.source_stream_id, "source_stream_id"),
        )
        if self.mode is not None:
            object.__setattr__(self, "mode", ReplayMode(self.mode))
        if self.status is not None:
            object.__setattr__(self, "status", ReplayStatus(self.status))
        object.__setattr__(self, "tenant_id", _optional_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "cursor", _optional_text(self.cursor, "cursor"))
        object.__setattr__(self, "limit", _page_limit(self.limit))


@dataclass(frozen=True, slots=True)
class ReplayReportPage:
    reports: tuple[ReplayReport, ...]
    next_cursor: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reports",
            _typed_tuple(self.reports, ReplayReport, "reports"),
        )
        object.__setattr__(self, "next_cursor", _optional_text(self.next_cursor, "next_cursor"))


__all__ = [
    "AppendResult",
    "CheckpointPage",
    "CheckpointQuery",
    "CheckpointKey",
    "ClaimedDelivery",
    "ConsumerCheckpoint",
    "ConsumerEffectContract",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_PAGE_LIMIT",
    "DeadLetterAction",
    "DeadLetterDisposition",
    "DeadLetterPage",
    "DeadLetterQuery",
    "DeadLetterRecord",
    "DeliveryClaimRequest",
    "DeliveryKey",
    "DeliveryLeaseToken",
    "DeliveryLimits",
    "DeliveryPage",
    "DeliveryQuery",
    "DeliveryRecord",
    "DeliverySettlement",
    "DeliverySettlementResult",
    "DeliveryState",
    "DurableSubscription",
    "EffectIdempotencyStrategy",
    "EventPage",
    "InboxEntry",
    "InboxKey",
    "LeasePolicy",
    "LegacyEventOffset",
    "MAX_LEASE_SECONDS",
    "MAX_PAGE_LIMIT",
    "MAX_REDELIVERY_ITEMS",
    "MAX_RETIREMENT_CANCELLATION_ITEMS",
    "MIN_LEASE_SECONDS",
    "PendingDeliveryStats",
    "RedeliveryItem",
    "RedeliveryReport",
    "RedeliveryRequest",
    "RetirementCancellationItem",
    "RetirementCancellationReport",
    "RetirementCancellationRequest",
    "QuarantineDisposition",
    "QuarantinePage",
    "QuarantineQuery",
    "QuarantineReason",
    "QuarantineRecord",
    "ReplayMode",
    "ReplayReport",
    "ReplayReportPage",
    "ReplayReportQuery",
    "ReplayStartRequest",
    "ReplayStatus",
    "ReplayVersion",
    "RetryPolicy",
    "StreamReadRequest",
    "StreamSequenceCursor",
    "SubscriptionFilter",
    "SubscriptionKey",
    "SubscriptionPage",
    "SubscriptionQuery",
    "SubscriptionStart",
    "SubscriptionStartPolicy",
    "SubscriptionStatus",
    "SubscriptionStreamState",
    "SubscriptionStreamStatePage",
    "SubscriptionStreamStateQuery",
]
