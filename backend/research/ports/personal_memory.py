from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from backend.research.domain.personal_memory import (
    PersonalMemoryOwner,
    PersonalMemoryRecord,
    PersonalMemoryRemotePolicy,
    PersonalMemorySelection,
    PersonalMemoryStatus,
)


class PersonalMemoryStoreReason(StrEnum):
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    REVISION_CONFLICT = "revision_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    OWNER_CAPACITY_EXCEEDED = "owner_capacity_exceeded"
    RECORD_NOT_ACTIVE = "record_not_active"
    STORE_CLOSED = "store_closed"
    STORE_UNAVAILABLE = "store_unavailable"
    STORE_CORRUPT = "store_corrupt"
    SCHEMA_UNSUPPORTED = "schema_unsupported"


class PersonalMemoryStoreError(RuntimeError):
    code = "personal_memory_store_error"
    public_message = "Personal memory storage operation failed."
    retryable = False

    def __init__(self, reason: PersonalMemoryStoreReason) -> None:
        if not isinstance(reason, PersonalMemoryStoreReason):
            raise TypeError("reason must be a PersonalMemoryStoreReason")
        self.reason = reason
        super().__init__(self.public_message)

    @property
    def reason_code(self) -> str:
        return self.reason.value

    def to_public_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.public_message,
            "reason": self.reason_code,
            "retryable": self.retryable,
        }


class PersonalMemoryStoreValidationError(PersonalMemoryStoreError, ValueError):
    code = "personal_memory_store_invalid"
    public_message = "Personal memory storage input is invalid."


class PersonalMemoryStoreNotFoundError(PersonalMemoryStoreError, LookupError):
    code = "personal_memory_not_found"
    public_message = "Personal memory record was not found."


class PersonalMemoryStoreConflictError(PersonalMemoryStoreError):
    code = "personal_memory_conflict"
    public_message = "Personal memory storage state conflicts with the request."


class PersonalMemoryStoreUnavailableError(PersonalMemoryStoreError):
    code = "personal_memory_store_unavailable"
    public_message = "Personal memory storage is unavailable."
    retryable = True


class PersonalMemoryStoreCorruptionError(PersonalMemoryStoreError):
    code = "personal_memory_store_corrupt"
    public_message = "Personal memory storage failed integrity validation."


@runtime_checkable
class PersonalMemoryStore(Protocol):
    def create(
        self,
        record: PersonalMemoryRecord,
        *,
        client_request_id: str,
        request_fingerprint: str,
        max_records_per_owner: int,
    ) -> PersonalMemoryRecord: ...

    def get(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
    ) -> PersonalMemoryRecord | None: ...

    def list(
        self,
        owner: PersonalMemoryOwner,
        *,
        statuses: tuple[PersonalMemoryStatus, ...],
        limit: int,
        offset: int,
    ) -> tuple[tuple[PersonalMemoryRecord, ...], int]: ...

    def compare_and_swap(
        self,
        record: PersonalMemoryRecord,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRecord: ...

    def recall_candidates(
        self,
        owner: PersonalMemoryOwner,
        *,
        normalized_query: str,
        terms: tuple[str, ...],
        limit: int,
    ) -> tuple[PersonalMemoryRecord, ...]: ...

    def rebuild_recall_index(self, owner: PersonalMemoryOwner) -> int: ...

    def get_or_create_policy(
        self,
        owner: PersonalMemoryOwner,
        *,
        created_at: datetime,
    ) -> PersonalMemoryRemotePolicy: ...

    def compare_and_swap_policy(
        self,
        policy: PersonalMemoryRemotePolicy,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRemotePolicy: ...

    def load_active_snapshot(
        self,
        owner: PersonalMemoryOwner,
        selections: tuple[PersonalMemorySelection, ...],
    ) -> tuple[PersonalMemoryRecord, ...]: ...

    def close(self) -> None: ...


__all__ = [
    "PersonalMemoryStore",
    "PersonalMemoryStoreConflictError",
    "PersonalMemoryStoreCorruptionError",
    "PersonalMemoryStoreError",
    "PersonalMemoryStoreNotFoundError",
    "PersonalMemoryStoreReason",
    "PersonalMemoryStoreUnavailableError",
    "PersonalMemoryStoreValidationError",
]
