from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from backend.research.domain.personal_memory import (
    MAX_LIST_LIMIT,
    MAX_QUERY_LENGTH,
    MAX_RECALL_CANDIDATES,
    MAX_RECALL_LIMIT,
    MAX_RECORDS_PER_OWNER,
    MAX_SNAPSHOT_RECORDS,
    PersonalMemoryCategory,
    PersonalMemoryExport,
    PersonalMemoryOrigin,
    PersonalMemoryOwner,
    PersonalMemoryPage,
    PersonalMemoryRecallItem,
    PersonalMemoryRecallResult,
    PersonalMemoryRecord,
    PersonalMemoryRemotePolicy,
    PersonalMemorySelection,
    PersonalMemorySourceRef,
    PersonalMemoryStatus,
    PersonalMemoryTaskSnapshot,
    normalize_source_refs,
    validate_client_request_id,
    validate_memory_content,
)
from backend.research.ports.personal_memory import (
    PersonalMemoryStore,
    PersonalMemoryStoreConflictError,
    PersonalMemoryStoreNotFoundError,
    PersonalMemoryStoreReason,
)


PERSONAL_MEMORY_EXPORT_SCHEMA = "newsroom.personal-memory-export/v1"
GENERIC_READING_PREFERENCE_REASON = "已确认的通用阅读偏好"
GENERIC_READING_PREFERENCE_SCORE = 1
_MAX_QUERY_TERMS = 24
_MAX_QUERY_TERM_LENGTH = 64
_UNSET = object()
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


class PersonalMemoryStateError(ValueError):
    code = "personal_memory_state_invalid"


class PersonalMemoryService:
    """Deterministic owner-scoped personal-memory application service.

    The caller supplies a trusted owner separately from request payload data.
    This service never calls a model, embedding provider, tool, or remote store.
    """

    def __init__(
        self,
        store: PersonalMemoryStore,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(store, PersonalMemoryStore):
            raise TypeError("store must implement PersonalMemoryStore")
        self._store = store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: f"pmem_{uuid.uuid4().hex}")

    def list(
        self,
        owner: PersonalMemoryOwner,
        *,
        statuses: Iterable[PersonalMemoryStatus | str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> PersonalMemoryPage:
        owner = _require_owner(owner)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        normalized_statuses = _normalize_statuses(statuses)
        records, total = self._store.list(
            owner,
            statuses=normalized_statuses,
            limit=limit,
            offset=offset,
        )
        return PersonalMemoryPage(
            records=records, total=total, limit=limit, offset=offset
        )

    def get(self, owner: PersonalMemoryOwner, memory_id: str) -> PersonalMemoryRecord:
        owner = _require_owner(owner)
        record = self._store.get(owner, _validate_memory_id(memory_id))
        if record is None:
            raise PersonalMemoryStoreNotFoundError(PersonalMemoryStoreReason.NOT_FOUND)
        return record

    def create_candidate(
        self,
        owner: PersonalMemoryOwner,
        *,
        content: str,
        category: PersonalMemoryCategory | str,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]],
        client_request_id: str,
    ) -> PersonalMemoryRecord:
        return self._create(
            owner,
            content=content,
            category=category,
            source_refs=source_refs,
            client_request_id=client_request_id,
            status=PersonalMemoryStatus.CANDIDATE,
            origin=PersonalMemoryOrigin.MODEL_CANDIDATE,
        )

    def create_confirmed(
        self,
        owner: PersonalMemoryOwner,
        *,
        content: str,
        category: PersonalMemoryCategory | str,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]],
        client_request_id: str,
    ) -> PersonalMemoryRecord:
        return self._create(
            owner,
            content=content,
            category=category,
            source_refs=source_refs,
            client_request_id=client_request_id,
            status=PersonalMemoryStatus.ACTIVE,
            origin=PersonalMemoryOrigin.USER_CONFIRMED,
        )

    def confirm(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        *,
        expected_revision: int,
        content: str | None = None,
        category: PersonalMemoryCategory | str | None = None,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]]
        | None = None,
    ) -> PersonalMemoryRecord:
        current = self._get_for_mutation(owner, memory_id, expected_revision)
        if current.status is PersonalMemoryStatus.ACTIVE:
            return self._update_if_changed(
                current,
                expected_revision=expected_revision,
                content=content,
                category=category,
                source_refs=source_refs,
            )
        if current.status is not PersonalMemoryStatus.CANDIDATE:
            raise PersonalMemoryStateError("only candidate memory can be confirmed")
        now = self._now()
        next_record = current.evolve(
            updated_at=now,
            content=current.content
            if content is None
            else validate_memory_content(content),
            category=current.category
            if category is None
            else PersonalMemoryCategory(category),
            source_refs=current.source_refs
            if source_refs is None
            else normalize_source_refs(source_refs),
            status=PersonalMemoryStatus.ACTIVE,
            confirmed_at=now,
            disabled_at=None,
            deleted_at=None,
        )
        return self._store.compare_and_swap(
            next_record, expected_revision=expected_revision
        )

    def reject_candidate(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRecord:
        current = self._get_for_mutation(owner, memory_id, expected_revision)
        if current.status is not PersonalMemoryStatus.CANDIDATE:
            raise PersonalMemoryStateError("only candidate memory can be rejected")
        now = self._now()
        return self._store.compare_and_swap(
            current.evolve(
                updated_at=now,
                status=PersonalMemoryStatus.DELETED,
                deleted_at=now,
            ),
            expected_revision=expected_revision,
        )

    def update(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        *,
        expected_revision: int,
        content: str | object = _UNSET,
        category: PersonalMemoryCategory | str | object = _UNSET,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]]
        | object = _UNSET,
    ) -> PersonalMemoryRecord:
        current = self._get_for_mutation(owner, memory_id, expected_revision)
        if current.status is PersonalMemoryStatus.DELETED:
            raise PersonalMemoryStateError("deleted memory cannot be updated")
        return self._update_if_changed(
            current,
            expected_revision=expected_revision,
            content=None if content is _UNSET else content,
            category=None if category is _UNSET else category,
            source_refs=None if source_refs is _UNSET else source_refs,
        )

    def disable(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRecord:
        current = self._get_for_mutation(owner, memory_id, expected_revision)
        if current.status is PersonalMemoryStatus.DISABLED:
            return current
        if current.status is not PersonalMemoryStatus.ACTIVE:
            raise PersonalMemoryStateError("only active memory can be disabled")
        now = self._now()
        return self._store.compare_and_swap(
            current.evolve(
                updated_at=now,
                status=PersonalMemoryStatus.DISABLED,
                disabled_at=now,
            ),
            expected_revision=expected_revision,
        )

    def enable(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRecord:
        current = self._get_for_mutation(owner, memory_id, expected_revision)
        if current.status is PersonalMemoryStatus.ACTIVE:
            return current
        if current.status is not PersonalMemoryStatus.DISABLED:
            raise PersonalMemoryStateError("only disabled memory can be enabled")
        return self._store.compare_and_swap(
            current.evolve(
                updated_at=self._now(),
                status=PersonalMemoryStatus.ACTIVE,
                disabled_at=None,
            ),
            expected_revision=expected_revision,
        )

    def delete(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRecord:
        current = self._get_for_mutation(owner, memory_id, expected_revision)
        if current.status is PersonalMemoryStatus.DELETED:
            return current
        now = self._now()
        return self._store.compare_and_swap(
            current.evolve(
                updated_at=now,
                status=PersonalMemoryStatus.DELETED,
                deleted_at=now,
            ),
            expected_revision=expected_revision,
        )

    def get_policy(self, owner: PersonalMemoryOwner) -> PersonalMemoryRemotePolicy:
        owner = _require_owner(owner)
        return self._store.get_or_create_policy(owner, created_at=self._now())

    def update_policy(
        self,
        owner: PersonalMemoryOwner,
        *,
        allow_remote_processing: bool,
        expected_revision: int,
    ) -> PersonalMemoryRemotePolicy:
        owner = _require_owner(owner)
        if type(allow_remote_processing) is not bool:
            raise TypeError("allow_remote_processing must be boolean")
        _validate_expected_revision(expected_revision)
        current = self._store.get_or_create_policy(owner, created_at=self._now())
        if current.revision != expected_revision:
            raise PersonalMemoryStoreConflictError(
                PersonalMemoryStoreReason.REVISION_CONFLICT
            )
        if current.allow_remote_processing is allow_remote_processing:
            return current
        return self._store.compare_and_swap_policy(
            current.evolve(
                allow_remote_processing=allow_remote_processing,
                updated_at=self._now(),
            ),
            expected_revision=expected_revision,
        )

    def select_active(
        self,
        owner: PersonalMemoryOwner,
        *,
        memory_refs: Iterable[PersonalMemorySelection | Mapping[str, Any]],
    ) -> PersonalMemoryTaskSnapshot:
        owner = _require_owner(owner)
        selections = _normalize_selections(memory_refs)
        records = self._store.load_active_snapshot(owner, selections)
        return PersonalMemoryTaskSnapshot(
            owner=owner,
            records=records,
            created_at=self._now(),
        )

    def export(
        self,
        owner: PersonalMemoryOwner,
        *,
        include_deleted: bool = True,
    ) -> PersonalMemoryExport:
        owner = _require_owner(owner)
        if type(include_deleted) is not bool:
            raise TypeError("include_deleted must be boolean")
        statuses = (
            tuple(PersonalMemoryStatus)
            if include_deleted
            else (
                PersonalMemoryStatus.CANDIDATE,
                PersonalMemoryStatus.ACTIVE,
                PersonalMemoryStatus.DISABLED,
            )
        )
        records: list[PersonalMemoryRecord] = []
        offset = 0
        while True:
            page, total = self._store.list(
                owner,
                statuses=statuses,
                limit=MAX_LIST_LIMIT,
                offset=offset,
            )
            records.extend(page)
            offset += len(page)
            if offset >= total or not page:
                break
        policy = self.get_policy(owner)
        return PersonalMemoryExport(
            schema_version=PERSONAL_MEMORY_EXPORT_SCHEMA,
            owner=owner,
            exported_at=self._now(),
            records=tuple(records),
            policy=policy,
            notices=(
                "Deleted records are tombstones retained locally to prevent accidental resurrection.",
                "Previously exported files and historical conversations are separate copies and are not rewritten by later changes.",
                "Source references identify provenance; this export does not embed the source document itself.",
            ),
        )

    def recall(
        self,
        owner: PersonalMemoryOwner,
        *,
        query: str,
        limit: int = 5,
        use_memory: bool,
    ) -> PersonalMemoryRecallResult:
        owner = _require_owner(owner)
        normalized_query, terms = _normalize_query(query)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_RECALL_LIMIT
        ):
            raise ValueError(f"limit must be between 1 and {MAX_RECALL_LIMIT}")
        if type(use_memory) is not bool:
            raise TypeError("use_memory must be boolean")
        if not use_memory:
            return PersonalMemoryRecallResult(
                query=query,
                records=(),
                memory_enabled=False,
            )
        candidates = self._store.recall_candidates(
            owner,
            normalized_query=normalized_query,
            terms=terms,
            limit=MAX_RECALL_CANDIDATES,
        )
        scored = tuple(
            _score_record(record, normalized_query, terms) for record in candidates
        )
        ranked = tuple(
            sorted(
                (item for item in scored if item.score > 0),
                key=lambda item: (
                    -item.score,
                    -int(item.record.updated_at.timestamp() * 1_000_000),
                    item.record.memory_id,
                ),
            )[:limit]
        )
        return PersonalMemoryRecallResult(
            query=query,
            records=ranked,
            memory_enabled=True,
        )

    def rebuild_recall_index(self, owner: PersonalMemoryOwner) -> int:
        return self._store.rebuild_recall_index(_require_owner(owner))

    def _create(
        self,
        owner: PersonalMemoryOwner,
        *,
        content: str,
        category: PersonalMemoryCategory | str,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]],
        client_request_id: str,
        status: PersonalMemoryStatus,
        origin: PersonalMemoryOrigin,
    ) -> PersonalMemoryRecord:
        owner = _require_owner(owner)
        client_request_id = validate_client_request_id(client_request_id)
        content = validate_memory_content(content)
        category = PersonalMemoryCategory(category)
        normalized_sources = normalize_source_refs(source_refs)
        now = self._now()
        record = PersonalMemoryRecord(
            memory_id=self._id_factory(),
            owner=owner,
            content=content,
            category=category,
            source_refs=normalized_sources,
            status=status,
            revision=1,
            created_at=now,
            updated_at=now,
            origin=origin,
            confirmed_at=now if status is PersonalMemoryStatus.ACTIVE else None,
        )
        return self._store.create(
            record,
            client_request_id=client_request_id,
            request_fingerprint=_create_fingerprint(record),
            max_records_per_owner=MAX_RECORDS_PER_OWNER,
        )

    def _get_for_mutation(
        self,
        owner: PersonalMemoryOwner,
        memory_id: str,
        expected_revision: int,
    ) -> PersonalMemoryRecord:
        _validate_expected_revision(expected_revision)
        current = self.get(owner, memory_id)
        if current.revision != expected_revision:
            raise PersonalMemoryStoreConflictError(
                PersonalMemoryStoreReason.REVISION_CONFLICT
            )
        return current

    def _update_if_changed(
        self,
        current: PersonalMemoryRecord,
        *,
        expected_revision: int,
        content: str | None,
        category: PersonalMemoryCategory | str | None,
        source_refs: Iterable[PersonalMemorySourceRef | Mapping[str, Any]] | None,
    ) -> PersonalMemoryRecord:
        next_content = (
            current.content if content is None else validate_memory_content(content)
        )
        next_category = (
            current.category if category is None else PersonalMemoryCategory(category)
        )
        next_sources = (
            current.source_refs
            if source_refs is None
            else normalize_source_refs(source_refs)
        )
        if (
            next_content == current.content
            and next_category is current.category
            and next_sources == current.source_refs
        ):
            return current
        return self._store.compare_and_swap(
            current.evolve(
                updated_at=self._now(),
                content=next_content,
                category=next_category,
                source_refs=next_sources,
            ),
            expected_revision=expected_revision,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _normalize_selections(
    values: Iterable[PersonalMemorySelection | Mapping[str, Any]],
) -> tuple[PersonalMemorySelection, ...]:
    if isinstance(values, (str, bytes)):
        raise TypeError("memory_refs must be a sequence of memory_id/revision objects")
    selections: list[PersonalMemorySelection] = []
    by_id: dict[str, int] = {}
    for value in values:
        selection = (
            value
            if isinstance(value, PersonalMemorySelection)
            else PersonalMemorySelection.from_dict(value)
        )
        previous = by_id.get(selection.memory_id)
        if previous is not None:
            if previous != selection.revision:
                raise ValueError(
                    "one memory_id cannot be selected at multiple revisions"
                )
            continue
        by_id[selection.memory_id] = selection.revision
        selections.append(selection)
        if len(selections) > MAX_SNAPSHOT_RECORDS:
            raise ValueError(
                f"memory_refs must contain at most {MAX_SNAPSHOT_RECORDS} entries"
            )
    if not selections:
        raise ValueError("memory_refs must contain at least one memory selection")
    return tuple(selections)


def _normalize_statuses(
    statuses: Iterable[PersonalMemoryStatus | str] | None,
) -> tuple[PersonalMemoryStatus, ...]:
    if statuses is None:
        return (
            PersonalMemoryStatus.CANDIDATE,
            PersonalMemoryStatus.ACTIVE,
            PersonalMemoryStatus.DISABLED,
        )
    if isinstance(statuses, (str, bytes)):
        raise TypeError("statuses must be a sequence")
    normalized = tuple(dict.fromkeys(PersonalMemoryStatus(value) for value in statuses))
    if not normalized:
        raise ValueError("statuses must not be empty")
    return normalized


def _normalize_query(query: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(query, str) or not query.strip() or query != query.strip():
        raise ValueError("query must be non-blank canonical text")
    if "\x00" in query or len(query) > MAX_QUERY_LENGTH:
        raise ValueError(
            f"query must be at most {MAX_QUERY_LENGTH} characters and contain no NUL"
        )
    normalized = " ".join(query.casefold().split())
    terms: list[str] = []
    for token in _TOKEN_PATTERN.findall(normalized):
        if len(token) > _MAX_QUERY_TERM_LENGTH:
            token = token[:_MAX_QUERY_TERM_LENGTH]
        if token not in terms:
            terms.append(token)
        if len(terms) >= _MAX_QUERY_TERMS:
            break
    if not terms:
        raise ValueError("query must contain searchable text")
    return normalized, tuple(terms)


def _score_record(
    record: PersonalMemoryRecord,
    normalized_query: str,
    terms: tuple[str, ...],
) -> PersonalMemoryRecallItem:
    if not record.recallable:
        return PersonalMemoryRecallItem(record=record, score=0, reasons=())
    content = record.content.casefold()
    category = record.category.value.casefold().replace("_", " ")
    sources = " ".join(
        " ".join(
            value
            for value in (
                source.source_type.value,
                source.source_ref,
                source.source_revision,
                source.locator,
                source.label,
            )
            if value
        )
        for source in record.source_refs
    ).casefold()
    score = 0
    reasons: list[str] = []
    if normalized_query in content:
        score += 100
        reasons.append("exact query phrase matched memory content")
    for term in terms:
        occurrences = min(content.count(term), 10)
        if occurrences:
            score += occurrences * 10
            reasons.append(
                f"term {term!r} matched memory content {occurrences} time(s)"
            )
        if term in category:
            score += 15
            reasons.append(f"term {term!r} matched category {record.category.value!r}")
        if term in sources:
            score += 5
            reasons.append(f"term {term!r} matched a source reference")
    if record.category is PersonalMemoryCategory.READING_PREFERENCE:
        score += GENERIC_READING_PREFERENCE_SCORE
        reasons.append(GENERIC_READING_PREFERENCE_REASON)
    return PersonalMemoryRecallItem(record=record, score=score, reasons=tuple(reasons))


def _create_fingerprint(record: PersonalMemoryRecord) -> str:
    payload = {
        "owner": record.owner.to_dict(),
        "content": record.content,
        "category": record.category.value,
        "source_refs": [source.to_dict() for source in record.source_refs],
        "status": record.status.value,
        "origin": record.origin.value,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _require_owner(value: PersonalMemoryOwner) -> PersonalMemoryOwner:
    if not isinstance(value, PersonalMemoryOwner):
        raise TypeError(
            "owner must be a PersonalMemoryOwner supplied by the application"
        )
    return value


def _validate_memory_id(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"pmem_[0-9a-f]{32}", value) is None:
        raise ValueError("memory_id is invalid")
    return value


def _validate_expected_revision(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_revision must be a positive integer")
    return value


__all__ = [
    "GENERIC_READING_PREFERENCE_REASON",
    "GENERIC_READING_PREFERENCE_SCORE",
    "PERSONAL_MEMORY_EXPORT_SCHEMA",
    "PersonalMemoryService",
    "PersonalMemoryStateError",
]
