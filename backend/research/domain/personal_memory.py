from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Iterable, Mapping


MAX_OWNER_ID_LENGTH = 128
MAX_NAMESPACE_LENGTH = 160
MAX_MEMORY_ID_LENGTH = 37
MAX_CONTENT_LENGTH = 8_000
MAX_SOURCE_REFS = 32
MAX_SOURCE_REF_LENGTH = 1_024
MAX_SOURCE_LABEL_LENGTH = 256
MAX_SOURCE_REVISION_LENGTH = 256
MAX_SOURCE_LOCATOR_LENGTH = 512
MAX_CLIENT_REQUEST_ID_LENGTH = 128
MAX_QUERY_LENGTH = 512
MAX_LIST_LIMIT = 100
MAX_RECALL_LIMIT = 20
MAX_RECALL_CANDIDATES = 500
MAX_RECORDS_PER_OWNER = 10_000
MAX_SNAPSHOT_RECORDS = 20

_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:+-]{0,127}$")
_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_MEMORY_ID_PATTERN = re.compile(r"^pmem_[0-9a-f]{32}$")
_CLIENT_REQUEST_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


class PersonalMemoryCategory(StrEnum):
    RESEARCH_INTEREST = "research_interest"
    READING_PREFERENCE = "reading_preference"
    BACKGROUND = "background"


class PersonalMemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class PersonalMemoryOrigin(StrEnum):
    MODEL_CANDIDATE = "model_candidate"
    USER_CONFIRMED = "user_confirmed"


class PersonalMemorySourceType(StrEnum):
    USER_INPUT = "user_input"
    READING_NOTE = "reading_note"
    PAPER = "paper"
    EVIDENCE = "evidence"
    CONVERSATION = "conversation"
    IMPORT = "import"
    OTHER = "other"


class PersonalMemoryRemoteChannel(StrEnum):
    MODEL = "model"
    TOOL = "tool"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class PersonalMemoryOwner:
    """Trusted application identity used for every personal-memory operation."""

    user_id: str
    namespace: str
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_id",
            _bounded_identifier(
                self.user_id,
                "user_id",
                MAX_OWNER_ID_LENGTH,
                _OWNER_PATTERN,
            ),
        )
        object.__setattr__(
            self,
            "namespace",
            _bounded_identifier(
                self.namespace,
                "namespace",
                MAX_NAMESPACE_LENGTH,
                _NAMESPACE_PATTERN,
            ),
        )
        namespace_segments = self.namespace.split("/")
        if any(segment in {"", ".", ".."} for segment in namespace_segments):
            raise ValueError("namespace must use non-empty canonical path segments")
        if self.tenant_id is not None:
            object.__setattr__(
                self,
                "tenant_id",
                _bounded_identifier(
                    self.tenant_id,
                    "tenant_id",
                    MAX_OWNER_ID_LENGTH,
                    _OWNER_PATTERN,
                ),
            )

    @property
    def tenant_scope(self) -> str:
        return self.tenant_id or ""

    def to_dict(self) -> dict[str, str | None]:
        return {
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "namespace": self.namespace,
        }


@dataclass(frozen=True, slots=True)
class PersonalMemorySourceRef:
    """Structured provenance for one memory without embedding source content."""

    source_type: PersonalMemorySourceType | str
    source_ref: str
    source_revision: str | None = None
    locator: str | None = None
    label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "source_type", PersonalMemorySourceType(self.source_type)
        )
        object.__setattr__(
            self,
            "source_ref",
            _bounded_text(self.source_ref, "source_ref", MAX_SOURCE_REF_LENGTH),
        )
        for field_name, maximum in (
            ("source_revision", MAX_SOURCE_REVISION_LENGTH),
            ("locator", MAX_SOURCE_LOCATOR_LENGTH),
            ("label", MAX_SOURCE_LABEL_LENGTH),
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _bounded_text(value, field_name, maximum),
                )

    def to_dict(self) -> dict[str, str | None]:
        return {
            "source_type": self.source_type.value,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "locator": self.locator,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PersonalMemorySourceRef":
        if not isinstance(value, Mapping):
            raise TypeError("source ref must be an object")
        allowed = {"source_type", "source_ref", "source_revision", "locator", "label"}
        if set(value) - allowed:
            raise ValueError("source ref contains unsupported fields")
        return cls(
            source_type=value.get("source_type", PersonalMemorySourceType.OTHER.value),
            source_ref=value.get("source_ref"),
            source_revision=value.get("source_revision"),
            locator=value.get("locator"),
            label=value.get("label"),
        )


@dataclass(frozen=True, slots=True)
class PersonalMemoryRecord:
    memory_id: str
    owner: PersonalMemoryOwner
    content: str
    category: PersonalMemoryCategory | str
    source_refs: tuple[PersonalMemorySourceRef, ...]
    status: PersonalMemoryStatus | str
    revision: int
    created_at: datetime
    updated_at: datetime
    origin: PersonalMemoryOrigin | str
    confirmed_at: datetime | None = None
    disabled_at: datetime | None = None
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner, PersonalMemoryOwner):
            raise TypeError("owner must be a PersonalMemoryOwner")
        if (
            not isinstance(self.memory_id, str)
            or _MEMORY_ID_PATTERN.fullmatch(self.memory_id) is None
        ):
            raise ValueError("memory_id must use the pmem_<32 lowercase hex> format")
        object.__setattr__(self, "content", validate_memory_content(self.content))
        object.__setattr__(self, "category", PersonalMemoryCategory(self.category))
        object.__setattr__(self, "status", PersonalMemoryStatus(self.status))
        object.__setattr__(self, "origin", PersonalMemoryOrigin(self.origin))
        object.__setattr__(self, "source_refs", normalize_source_refs(self.source_refs))
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("revision must be a positive integer")
        for field_name in ("created_at", "updated_at"):
            object.__setattr__(
                self, field_name, ensure_utc(getattr(self, field_name), field_name)
            )
        for field_name in ("confirmed_at", "disabled_at", "deleted_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, ensure_utc(value, field_name))
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        self._validate_lifecycle()

    def _validate_lifecycle(self) -> None:
        if self.status is PersonalMemoryStatus.CANDIDATE:
            if (
                self.confirmed_at is not None
                or self.disabled_at is not None
                or self.deleted_at is not None
            ):
                raise ValueError(
                    "candidate memory cannot have terminal lifecycle timestamps"
                )
        elif self.status is PersonalMemoryStatus.ACTIVE:
            if self.confirmed_at is None:
                raise ValueError("active memory requires confirmed_at")
            if self.disabled_at is not None or self.deleted_at is not None:
                raise ValueError("active memory cannot have disabled_at or deleted_at")
        elif self.status is PersonalMemoryStatus.DISABLED:
            if self.confirmed_at is None or self.disabled_at is None:
                raise ValueError(
                    "disabled memory requires confirmed_at and disabled_at"
                )
            if self.deleted_at is not None:
                raise ValueError("disabled memory cannot have deleted_at")
        elif self.status is PersonalMemoryStatus.DELETED:
            if self.deleted_at is None:
                raise ValueError("deleted memory requires deleted_at")

    @property
    def recallable(self) -> bool:
        return self.status is PersonalMemoryStatus.ACTIVE

    def evolve(self, *, updated_at: datetime, **changes: Any) -> "PersonalMemoryRecord":
        return replace(
            self,
            revision=self.revision + 1,
            updated_at=ensure_utc(updated_at, "updated_at"),
            **changes,
        )

    def to_dict(self, *, include_owner: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "memory_id": self.memory_id,
            "content": self.content,
            "category": self.category.value,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "status": self.status.value,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_owner:
            payload["owner"] = self.owner.to_dict()
        payload.update(
            {
                "origin": self.origin.value,
                "confirmed_at": _optional_datetime(self.confirmed_at),
                "disabled_at": _optional_datetime(self.disabled_at),
                "deleted_at": _optional_datetime(self.deleted_at),
            }
        )
        return payload


@dataclass(frozen=True, slots=True)
class PersonalMemoryRemotePolicy:
    owner: PersonalMemoryOwner
    allow_remote_processing: bool
    revision: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.owner, PersonalMemoryOwner):
            raise TypeError("owner must be a PersonalMemoryOwner")
        if type(self.allow_remote_processing) is not bool:
            raise TypeError("allow_remote_processing must be boolean")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("revision must be a positive integer")
        object.__setattr__(
            self, "created_at", ensure_utc(self.created_at, "created_at")
        )
        object.__setattr__(
            self, "updated_at", ensure_utc(self.updated_at, "updated_at")
        )
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")

    def evolve(
        self,
        *,
        allow_remote_processing: bool,
        updated_at: datetime,
    ) -> "PersonalMemoryRemotePolicy":
        return replace(
            self,
            allow_remote_processing=allow_remote_processing,
            revision=self.revision + 1,
            updated_at=ensure_utc(updated_at, "updated_at"),
        )

    def allows_remote_processing(
        self,
        channel: PersonalMemoryRemoteChannel | str,
        *,
        use_memory: bool,
    ) -> bool:
        PersonalMemoryRemoteChannel(channel)
        if type(use_memory) is not bool:
            raise TypeError("use_memory must be boolean")
        return self.allow_remote_processing and use_memory

    def to_dict(self, *, include_owner: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "allow_remote_processing": self.allow_remote_processing,
            "remote_processing_permissions": {
                channel.value: self.allows_remote_processing(
                    channel, use_memory=True
                )
                for channel in PersonalMemoryRemoteChannel
            },
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_owner:
            payload["owner"] = self.owner.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class PersonalMemorySelection:
    memory_id: str
    revision: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.memory_id, str)
            or _MEMORY_ID_PATTERN.fullmatch(self.memory_id) is None
        ):
            raise ValueError("memory_id must use the pmem_<32 lowercase hex> format")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("revision must be a positive integer")

    def to_dict(self) -> dict[str, str | int]:
        return {"memory_id": self.memory_id, "revision": self.revision}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PersonalMemorySelection":
        if not isinstance(value, Mapping) or set(value) != {"memory_id", "revision"}:
            raise ValueError(
                "memory selection must contain exactly memory_id and revision"
            )
        return cls(memory_id=value["memory_id"], revision=value["revision"])


@dataclass(frozen=True, slots=True)
class PersonalMemoryTaskSnapshot:
    owner: PersonalMemoryOwner
    records: tuple[PersonalMemoryRecord, ...]
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.owner, PersonalMemoryOwner):
            raise TypeError("owner must be a PersonalMemoryOwner")
        object.__setattr__(
            self, "created_at", ensure_utc(self.created_at, "created_at")
        )
        if len(self.records) > MAX_SNAPSHOT_RECORDS:
            raise ValueError(
                f"snapshot records must contain at most {MAX_SNAPSHOT_RECORDS} entries"
            )
        seen: set[str] = set()
        for record in self.records:
            if (
                record.owner != self.owner
                or record.status is not PersonalMemoryStatus.ACTIVE
            ):
                raise ValueError(
                    "snapshot records must be active and owned by the snapshot owner"
                )
            if record.memory_id in seen:
                raise ValueError("snapshot records must have unique memory ids")
            seen.add(record.memory_id)

    @property
    def used_memory_refs(self) -> tuple[dict[str, str | int], ...]:
        return tuple(
            {"memory_id": record.memory_id, "revision": record.revision}
            for record in self.records
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [record.to_dict(include_owner=False) for record in self.records],
            "used_memory_refs": list(self.used_memory_refs),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class PersonalMemoryPage:
    records: tuple[PersonalMemoryRecord, ...]
    total: int
    limit: int
    offset: int

    @property
    def next_offset(self) -> int | None:
        candidate = self.offset + len(self.records)
        return candidate if candidate < self.total else None


@dataclass(frozen=True, slots=True)
class PersonalMemoryRecallItem:
    record: PersonalMemoryRecord
    score: int
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = self.record.to_dict(include_owner=False)
        payload.update({"score": self.score, "reasons": list(self.reasons)})
        return payload


@dataclass(frozen=True, slots=True)
class PersonalMemoryRecallResult:
    query: str
    records: tuple[PersonalMemoryRecallItem, ...]
    memory_enabled: bool

    @property
    def used_memory_refs(self) -> tuple[dict[str, str | int], ...]:
        return tuple(
            {"memory_id": item.record.memory_id, "revision": item.record.revision}
            for item in self.records
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [item.to_dict() for item in self.records],
            "used_memory_refs": list(self.used_memory_refs),
            "query": self.query,
            "memory_enabled": self.memory_enabled,
        }


@dataclass(frozen=True, slots=True)
class PersonalMemoryExport:
    schema_version: str
    owner: PersonalMemoryOwner
    exported_at: datetime
    records: tuple[PersonalMemoryRecord, ...]
    policy: PersonalMemoryRemotePolicy
    notices: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "owner": self.owner.to_dict(),
            "exported_at": self.exported_at.isoformat(),
            "records": [record.to_dict(include_owner=False) for record in self.records],
            "policy": self.policy.to_dict(include_owner=False),
            "notices": list(self.notices),
        }


def validate_memory_content(value: str) -> str:
    return _bounded_text(value, "content", MAX_CONTENT_LENGTH)


def validate_client_request_id(value: str) -> str:
    return _bounded_identifier(
        value,
        "client_request_id",
        MAX_CLIENT_REQUEST_ID_LENGTH,
        _CLIENT_REQUEST_PATTERN,
    )


def normalize_source_refs(
    values: Iterable[PersonalMemorySourceRef | Mapping[str, Any]],
) -> tuple[PersonalMemorySourceRef, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("source_refs must be a sequence of structured source refs")
    normalized: list[PersonalMemorySourceRef] = []
    seen: set[tuple[str | None, ...]] = set()
    for value in values:
        source = (
            value
            if isinstance(value, PersonalMemorySourceRef)
            else PersonalMemorySourceRef.from_dict(value)
        )
        key = (
            source.source_type.value,
            source.source_ref,
            source.source_revision,
            source.locator,
            source.label,
        )
        if key not in seen:
            seen.add(key)
            normalized.append(source)
        if len(normalized) > MAX_SOURCE_REFS:
            raise ValueError(
                f"source_refs must contain at most {MAX_SOURCE_REFS} entries"
            )
    if not normalized:
        raise ValueError("source_refs must contain at least one structured source ref")
    return tuple(normalized)


def ensure_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_identifier(
    value: Any, field_name: str, maximum: int, pattern: re.Pattern[str]
) -> str:
    value = _bounded_text(value, field_name, maximum)
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} contains unsupported characters")
    return value


def _bounded_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be non-blank canonical text")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL characters")
    if len(value) > maximum:
        raise ValueError(f"{field_name} must be at most {maximum} characters")
    return value


def _optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


__all__ = [
    "MAX_CLIENT_REQUEST_ID_LENGTH",
    "MAX_CONTENT_LENGTH",
    "MAX_LIST_LIMIT",
    "MAX_MEMORY_ID_LENGTH",
    "MAX_NAMESPACE_LENGTH",
    "MAX_OWNER_ID_LENGTH",
    "MAX_QUERY_LENGTH",
    "MAX_RECALL_CANDIDATES",
    "MAX_RECALL_LIMIT",
    "MAX_RECORDS_PER_OWNER",
    "MAX_SOURCE_REFS",
    "MAX_SNAPSHOT_RECORDS",
    "PersonalMemoryCategory",
    "PersonalMemoryExport",
    "PersonalMemoryOrigin",
    "PersonalMemoryOwner",
    "PersonalMemoryPage",
    "PersonalMemoryRecallItem",
    "PersonalMemoryRecallResult",
    "PersonalMemoryRecord",
    "PersonalMemoryRemoteChannel",
    "PersonalMemoryRemotePolicy",
    "PersonalMemorySelection",
    "PersonalMemorySourceRef",
    "PersonalMemorySourceType",
    "PersonalMemoryStatus",
    "PersonalMemoryTaskSnapshot",
    "ensure_utc",
    "normalize_source_refs",
    "validate_client_request_id",
    "validate_memory_content",
]
