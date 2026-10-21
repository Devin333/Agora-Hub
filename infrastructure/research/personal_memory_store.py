from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Any

from backend.research.domain.personal_memory import (
    PersonalMemoryOwner,
    PersonalMemoryRecord,
    PersonalMemoryRemotePolicy,
    PersonalMemorySelection,
    PersonalMemorySourceRef,
    PersonalMemoryStatus,
    ensure_utc,
    validate_client_request_id,
)
from backend.research.ports.personal_memory import (
    PersonalMemoryStoreConflictError,
    PersonalMemoryStoreCorruptionError,
    PersonalMemoryStoreReason,
    PersonalMemoryStoreUnavailableError,
    PersonalMemoryStoreValidationError,
)
from framework.shared.windows_paths import resolve_storage_path, storage_io_path


PERSONAL_MEMORY_SCHEMA_VERSION = 1
PERSONAL_MEMORY_APPLICATION_ID = 0x504D454D
DEFAULT_BUSY_TIMEOUT_SECONDS = 5.0

_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS personal_memory_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personal_memory_policies (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    allow_remote_processing INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, user_id, namespace),
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (allow_remote_processing IN (0, 1)),
    CHECK (revision >= 1),
    CHECK (updated_at >= created_at)
);

CREATE TABLE IF NOT EXISTS personal_memories (
    memory_id TEXT PRIMARY KEY,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    disabled_at TEXT,
    deleted_at TEXT,
    search_text TEXT NOT NULL,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (trim(user_id) <> '' AND trim(namespace) <> ''),
    CHECK (trim(content) <> ''),
    CHECK (category IN ('research_interest', 'reading_preference', 'background')),
    CHECK (status IN ('candidate', 'active', 'disabled', 'deleted')),
    CHECK (origin IN ('model_candidate', 'user_confirmed')),
    CHECK (revision >= 1),
    CHECK ((status = 'active' AND length(search_text) > 0) OR (status <> 'active' AND search_text = ''))
);

CREATE INDEX IF NOT EXISTS idx_personal_memories_owner_status_updated
ON personal_memories (tenant_scope, user_id, namespace, status, updated_at DESC, memory_id ASC);

CREATE TABLE IF NOT EXISTS personal_memory_revisions (
    memory_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    source_refs_json TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    disabled_at TEXT,
    deleted_at TEXT,
    PRIMARY KEY (memory_id, revision),
    FOREIGN KEY (memory_id) REFERENCES personal_memories(memory_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS personal_memory_create_requests (
    tenant_scope TEXT NOT NULL,
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    client_request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, user_id, namespace, client_request_id),
    FOREIGN KEY (memory_id) REFERENCES personal_memories(memory_id) ON DELETE RESTRICT,
    CHECK (operation IN ('create_candidate', 'create_confirmed'))
);
"""


class SQLitePersonalMemoryStore:
    """Durable owner-scoped SQLite adapter for local personal memory."""

    def __init__(
        self,
        database_path: str | os.PathLike[str],
        *,
        busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS,
    ) -> None:
        self._path = _validate_database_path(database_path)
        if (
            isinstance(busy_timeout_seconds, bool)
            or not isinstance(busy_timeout_seconds, (int, float))
            or not 0 < float(busy_timeout_seconds) <= 60
        ):
            raise ValueError("busy_timeout_seconds must be in the range (0, 60]")
        self._lock = RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                str(storage_io_path(self._path)),
                timeout=float(busy_timeout_seconds),
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout = {int(float(busy_timeout_seconds) * 1000)}"
            )
            connection.execute("PRAGMA foreign_keys = ON")
            self._connection = connection
            self._initialize_schema()
            journal_mode = str(
                connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            ).casefold()
            if journal_mode != "wal":
                raise sqlite3.OperationalError("WAL journal mode is unavailable")
            connection.execute("PRAGMA synchronous = FULL")
            _assert_regular_database_files(self._path)
        except sqlite3.Error as exc:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise _map_sqlite_error(
                exc, operation="initialize personal memory store"
            ) from exc
        except Exception:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            raise

    @property
    def database_path(self) -> Path:
        return self._path

    def create(
        self,
        record: PersonalMemoryRecord,
        *,
        client_request_id: str,
        request_fingerprint: str,
        max_records_per_owner: int,
    ) -> PersonalMemoryRecord:
        _require_record(record)
        client_request_id = validate_client_request_id(client_request_id)
        _require_fingerprint(request_fingerprint)
        if (
            isinstance(max_records_per_owner, bool)
            or not isinstance(max_records_per_owner, int)
            or max_records_per_owner < 1
        ):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        operation = (
            "create_candidate"
            if record.status is PersonalMemoryStatus.CANDIDATE
            else "create_confirmed"
        )
        if record.revision != 1 or record.status not in {
            PersonalMemoryStatus.CANDIDATE,
            PersonalMemoryStatus.ACTIVE,
        }:
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        owner_values = _owner_values(record.owner)
        with self._write_transaction() as connection:
            self._ensure_policy(connection, record.owner, record.created_at)
            request_row = connection.execute(
                """
                SELECT operation, request_fingerprint, memory_id
                FROM personal_memory_create_requests
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ? AND client_request_id = ?
                """,
                (*owner_values, client_request_id),
            ).fetchone()
            if request_row is not None:
                if (
                    str(request_row["operation"]) != operation
                    or str(request_row["request_fingerprint"]) != request_fingerprint
                ):
                    raise PersonalMemoryStoreConflictError(
                        PersonalMemoryStoreReason.IDEMPOTENCY_CONFLICT
                    )
                existing = self._get_with_connection(
                    connection, record.owner, str(request_row["memory_id"])
                )
                if existing is None:
                    raise PersonalMemoryStoreCorruptionError(
                        PersonalMemoryStoreReason.STORE_CORRUPT
                    )
                return existing
            count = int(
                connection.execute(
                    """
                SELECT COUNT(*) FROM personal_memories
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                """,
                    owner_values,
                ).fetchone()[0]
            )
            if count >= max_records_per_owner:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.OWNER_CAPACITY_EXCEEDED
                )
            try:
                self._insert_current(connection, record)
                self._insert_revision(connection, record)
                connection.execute(
                    """
                    INSERT INTO personal_memory_create_requests (
                        tenant_scope, user_id, namespace, client_request_id,
                        operation, request_fingerprint, memory_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *owner_values,
                        client_request_id,
                        operation,
                        request_fingerprint,
                        record.memory_id,
                        _datetime_text(record.created_at),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.IDEMPOTENCY_CONFLICT
                ) from exc
        return record

    def get(
        self, owner: PersonalMemoryOwner, memory_id: str
    ) -> PersonalMemoryRecord | None:
        _require_owner(owner)
        _require_memory_id(memory_id)
        with self._read_connection() as connection:
            return self._get_with_connection(connection, owner, memory_id)

    def list(
        self,
        owner: PersonalMemoryOwner,
        *,
        statuses: tuple[PersonalMemoryStatus, ...],
        limit: int,
        offset: int,
    ) -> tuple[tuple[PersonalMemoryRecord, ...], int]:
        _require_owner(owner)
        statuses = _require_statuses(statuses)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 100
        ):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        placeholders = ",".join("?" for _ in statuses)
        parameters = (*_owner_values(owner), *(status.value for status in statuses))
        with self._read_connection() as connection:
            total = int(
                connection.execute(
                    f"""
                SELECT COUNT(*) FROM personal_memories
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                  AND status IN ({placeholders})
                """,
                    parameters,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT * FROM personal_memories
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                  AND status IN ({placeholders})
                ORDER BY updated_at DESC, memory_id ASC
                LIMIT ? OFFSET ?
                """,
                (*parameters, limit, offset),
            ).fetchall()
            return tuple(_record_from_row(row) for row in rows), total

    def compare_and_swap(
        self,
        record: PersonalMemoryRecord,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRecord:
        _require_record(record)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or record.revision != expected_revision + 1
        ):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        with self._write_transaction() as connection:
            current = self._get_with_connection(
                connection, record.owner, record.memory_id
            )
            if current is None:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.NOT_FOUND
                )
            if current.revision != expected_revision:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.REVISION_CONFLICT
                )
            updated = connection.execute(
                """
                UPDATE personal_memories SET
                    content = ?, category = ?, source_refs_json = ?, status = ?,
                    origin = ?, revision = ?, updated_at = ?, confirmed_at = ?,
                    disabled_at = ?, deleted_at = ?, search_text = ?
                WHERE memory_id = ? AND tenant_scope = ? AND user_id = ?
                  AND namespace = ? AND revision = ?
                """,
                (
                    record.content,
                    record.category.value,
                    _source_refs_json(record),
                    record.status.value,
                    record.origin.value,
                    record.revision,
                    _datetime_text(record.updated_at),
                    _optional_datetime_text(record.confirmed_at),
                    _optional_datetime_text(record.disabled_at),
                    _optional_datetime_text(record.deleted_at),
                    _search_text(record),
                    record.memory_id,
                    *_owner_values(record.owner),
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.REVISION_CONFLICT
                )
            try:
                self._insert_revision(connection, record)
            except sqlite3.IntegrityError as exc:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.REVISION_CONFLICT
                ) from exc
        return record

    def recall_candidates(
        self,
        owner: PersonalMemoryOwner,
        *,
        normalized_query: str,
        terms: tuple[str, ...],
        limit: int,
    ) -> tuple[PersonalMemoryRecord, ...]:
        _require_owner(owner)
        if not isinstance(normalized_query, str) or not normalized_query:
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        if (
            not isinstance(terms, tuple)
            or not terms
            or any(not isinstance(term, str) or not term for term in terms)
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        score_parts = [
            "CASE WHEN instr(search_text, ?) > 0 THEN 100 ELSE 0 END",
            "CASE WHEN category = 'reading_preference' THEN 1 ELSE 0 END",
        ]
        score_parameters: list[str] = [normalized_query]
        predicates = ["instr(search_text, ?) > 0"]
        predicate_parameters: list[str] = [normalized_query]
        for term in terms:
            score_parts.append("CASE WHEN instr(search_text, ?) > 0 THEN 10 ELSE 0 END")
            score_parameters.append(term)
            predicates.append("instr(search_text, ?) > 0")
            predicate_parameters.append(term)
        score_sql = " + ".join(score_parts)
        predicate_sql = " OR ".join(predicates)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT *, ({score_sql}) AS recall_score
                FROM personal_memories
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                  AND status = 'active' AND search_text <> ''
                  AND (({predicate_sql}) OR category = 'reading_preference')
                ORDER BY recall_score DESC, updated_at DESC, memory_id ASC
                LIMIT ?
                """,
                (
                    *score_parameters,
                    *_owner_values(owner),
                    *predicate_parameters,
                    limit,
                ),
            ).fetchall()
            return tuple(_record_from_row(row) for row in rows)

    def rebuild_recall_index(self, owner: PersonalMemoryOwner) -> int:
        _require_owner(owner)
        with self._write_transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM personal_memories
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                """,
                _owner_values(owner),
            ).fetchall()
            active_count = 0
            for row in rows:
                record = _record_from_row(row)
                if record.status is PersonalMemoryStatus.ACTIVE:
                    active_count += 1
                connection.execute(
                    "UPDATE personal_memories SET search_text = ? WHERE memory_id = ?",
                    (_search_text(record), record.memory_id),
                )
            return active_count

    def get_or_create_policy(
        self,
        owner: PersonalMemoryOwner,
        *,
        created_at: datetime,
    ) -> PersonalMemoryRemotePolicy:
        _require_owner(owner)
        created_at = ensure_utc(created_at, "created_at")
        with self._write_transaction() as connection:
            return self._ensure_policy(connection, owner, created_at)

    def compare_and_swap_policy(
        self,
        policy: PersonalMemoryRemotePolicy,
        *,
        expected_revision: int,
    ) -> PersonalMemoryRemotePolicy:
        if not isinstance(policy, PersonalMemoryRemotePolicy):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
            or policy.revision != expected_revision + 1
        ):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        with self._write_transaction() as connection:
            updated = connection.execute(
                """
                UPDATE personal_memory_policies SET
                    allow_remote_processing = ?, revision = ?, updated_at = ?
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                  AND revision = ?
                """,
                (
                    int(policy.allow_remote_processing),
                    policy.revision,
                    _datetime_text(policy.updated_at),
                    *_owner_values(policy.owner),
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise PersonalMemoryStoreConflictError(
                    PersonalMemoryStoreReason.REVISION_CONFLICT
                )
        return policy

    def load_active_snapshot(
        self,
        owner: PersonalMemoryOwner,
        selections: tuple[PersonalMemorySelection, ...],
    ) -> tuple[PersonalMemoryRecord, ...]:
        _require_owner(owner)
        if (
            not isinstance(selections, tuple)
            or not selections
            or len(selections) > 20
            or any(not isinstance(item, PersonalMemorySelection) for item in selections)
        ):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        memory_ids = tuple(item.memory_id for item in selections)
        if len(set(memory_ids)) != len(memory_ids):
            raise PersonalMemoryStoreValidationError(
                PersonalMemoryStoreReason.INVALID_INPUT
            )
        placeholders = ",".join("?" for _ in memory_ids)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM personal_memories
                WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
                  AND memory_id IN ({placeholders})
                """,
                (*_owner_values(owner), *memory_ids),
            ).fetchall()
            by_id = {str(row["memory_id"]): _record_from_row(row) for row in rows}
            selected: list[PersonalMemoryRecord] = []
            for selection in selections:
                record = by_id.get(selection.memory_id)
                if record is None:
                    raise PersonalMemoryStoreConflictError(
                        PersonalMemoryStoreReason.NOT_FOUND
                    )
                if record.revision != selection.revision:
                    raise PersonalMemoryStoreConflictError(
                        PersonalMemoryStoreReason.REVISION_CONFLICT
                    )
                if record.status is not PersonalMemoryStatus.ACTIVE:
                    raise PersonalMemoryStoreConflictError(
                        PersonalMemoryStoreReason.RECORD_NOT_ACTIVE
                    )
                selected.append(record)
            return tuple(selected)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            connection = self._connection
            self._connection = None
            self._closed = True
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error as exc:
                    raise _map_sqlite_error(
                        exc, operation="close personal memory store"
                    ) from exc

    def __enter__(self) -> "SQLitePersonalMemoryStore":
        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _initialize_schema(self) -> None:
        connection = self._require_connection()
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > PERSONAL_MEMORY_SCHEMA_VERSION:
            raise PersonalMemoryStoreCorruptionError(
                PersonalMemoryStoreReason.SCHEMA_UNSUPPORTED
            )
        if version == 0:
            existing_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            if existing_tables:
                raise PersonalMemoryStoreCorruptionError(
                    PersonalMemoryStoreReason.STORE_CORRUPT
                )
            created_at = datetime.now(UTC).isoformat()
            script = (
                "BEGIN IMMEDIATE;\n"
                + _SCHEMA
                + f"\nPRAGMA application_id = {PERSONAL_MEMORY_APPLICATION_ID};"
                + f"\nPRAGMA user_version = {PERSONAL_MEMORY_SCHEMA_VERSION};"
                + "\nINSERT INTO personal_memory_metadata(singleton, schema_version, created_at) "
                + f"VALUES (1, {PERSONAL_MEMORY_SCHEMA_VERSION}, {json.dumps(created_at)});"
                + "\nCOMMIT;"
            )
            try:
                connection.executescript(script)
            except sqlite3.Error:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        if application_id != PERSONAL_MEMORY_APPLICATION_ID:
            raise PersonalMemoryStoreCorruptionError(
                PersonalMemoryStoreReason.STORE_CORRUPT
            )
        metadata = connection.execute(
            "SELECT schema_version FROM personal_memory_metadata WHERE singleton = 1"
        ).fetchone()
        if metadata is None or int(metadata[0]) != PERSONAL_MEMORY_SCHEMA_VERSION:
            raise PersonalMemoryStoreCorruptionError(
                PersonalMemoryStoreReason.STORE_CORRUPT
            )
        integrity = str(
            connection.execute("PRAGMA quick_check").fetchone()[0]
        ).casefold()
        if integrity != "ok":
            raise PersonalMemoryStoreCorruptionError(
                PersonalMemoryStoreReason.STORE_CORRUPT
            )

    def _ensure_policy(
        self,
        connection: sqlite3.Connection,
        owner: PersonalMemoryOwner,
        created_at: datetime,
    ) -> PersonalMemoryRemotePolicy:
        timestamp = _datetime_text(created_at)
        connection.execute(
            """
            INSERT OR IGNORE INTO personal_memory_policies (
                tenant_scope, tenant_id, user_id, namespace,
                allow_remote_processing, revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 0, 1, ?, ?)
            """,
            (
                owner.tenant_scope,
                owner.tenant_id,
                owner.user_id,
                owner.namespace,
                timestamp,
                timestamp,
            ),
        )
        row = connection.execute(
            """
            SELECT * FROM personal_memory_policies
            WHERE tenant_scope = ? AND user_id = ? AND namespace = ?
            """,
            _owner_values(owner),
        ).fetchone()
        if row is None:
            raise PersonalMemoryStoreCorruptionError(
                PersonalMemoryStoreReason.STORE_CORRUPT
            )
        return _policy_from_row(row)

    def _insert_current(
        self, connection: sqlite3.Connection, record: PersonalMemoryRecord
    ) -> None:
        connection.execute(
            """
            INSERT INTO personal_memories (
                memory_id, tenant_scope, tenant_id, user_id, namespace,
                content, category, source_refs_json, status, origin, revision,
                created_at, updated_at, confirmed_at, disabled_at, deleted_at,
                search_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _record_values(record),
        )

    def _insert_revision(
        self, connection: sqlite3.Connection, record: PersonalMemoryRecord
    ) -> None:
        connection.execute(
            """
            INSERT INTO personal_memory_revisions (
                memory_id, revision, tenant_scope, tenant_id, user_id, namespace,
                content, category, source_refs_json, status, origin,
                created_at, updated_at, confirmed_at, disabled_at, deleted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.memory_id,
                record.revision,
                record.owner.tenant_scope,
                record.owner.tenant_id,
                record.owner.user_id,
                record.owner.namespace,
                record.content,
                record.category.value,
                _source_refs_json(record),
                record.status.value,
                record.origin.value,
                _datetime_text(record.created_at),
                _datetime_text(record.updated_at),
                _optional_datetime_text(record.confirmed_at),
                _optional_datetime_text(record.disabled_at),
                _optional_datetime_text(record.deleted_at),
            ),
        )

    def _get_with_connection(
        self,
        connection: sqlite3.Connection,
        owner: PersonalMemoryOwner,
        memory_id: str,
    ) -> PersonalMemoryRecord | None:
        row = connection.execute(
            """
            SELECT * FROM personal_memories
            WHERE memory_id = ? AND tenant_scope = ? AND user_id = ? AND namespace = ?
            """,
            (memory_id, *_owner_values(owner)),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            try:
                yield connection
            except sqlite3.Error as exc:
                raise _map_sqlite_error(exc, operation="read personal memory") from exc

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except (
                PersonalMemoryStoreConflictError,
                PersonalMemoryStoreCorruptionError,
                PersonalMemoryStoreValidationError,
            ):
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise _map_sqlite_error(exc, operation="write personal memory") from exc
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _require_connection(self) -> sqlite3.Connection:
        if self._closed or self._connection is None:
            raise PersonalMemoryStoreUnavailableError(
                PersonalMemoryStoreReason.STORE_CLOSED
            )
        return self._connection


def _policy_from_row(row: sqlite3.Row) -> PersonalMemoryRemotePolicy:
    try:
        raw_allow_remote = int(row["allow_remote_processing"])
        if raw_allow_remote not in {0, 1}:
            raise ValueError("stored remote policy boolean is invalid")
        return PersonalMemoryRemotePolicy(
            owner=PersonalMemoryOwner(
                tenant_id=None if row["tenant_id"] is None else str(row["tenant_id"]),
                user_id=str(row["user_id"]),
                namespace=str(row["namespace"]),
            ),
            allow_remote_processing=bool(raw_allow_remote),
            revision=int(row["revision"]),
            created_at=_datetime_value(row["created_at"]),
            updated_at=_datetime_value(row["updated_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersonalMemoryStoreCorruptionError(
            PersonalMemoryStoreReason.STORE_CORRUPT
        ) from exc


def _record_values(record: PersonalMemoryRecord) -> tuple[Any, ...]:
    return (
        record.memory_id,
        record.owner.tenant_scope,
        record.owner.tenant_id,
        record.owner.user_id,
        record.owner.namespace,
        record.content,
        record.category.value,
        _source_refs_json(record),
        record.status.value,
        record.origin.value,
        record.revision,
        _datetime_text(record.created_at),
        _datetime_text(record.updated_at),
        _optional_datetime_text(record.confirmed_at),
        _optional_datetime_text(record.disabled_at),
        _optional_datetime_text(record.deleted_at),
        _search_text(record),
    )


def _record_from_row(row: sqlite3.Row) -> PersonalMemoryRecord:
    try:
        raw_sources = json.loads(str(row["source_refs_json"]))
        if not isinstance(raw_sources, list):
            raise ValueError("source refs are not an array")
        return PersonalMemoryRecord(
            memory_id=str(row["memory_id"]),
            owner=PersonalMemoryOwner(
                tenant_id=None if row["tenant_id"] is None else str(row["tenant_id"]),
                user_id=str(row["user_id"]),
                namespace=str(row["namespace"]),
            ),
            content=str(row["content"]),
            category=str(row["category"]),
            source_refs=tuple(
                PersonalMemorySourceRef.from_dict(value) for value in raw_sources
            ),
            status=str(row["status"]),
            revision=int(row["revision"]),
            created_at=_datetime_value(row["created_at"]),
            updated_at=_datetime_value(row["updated_at"]),
            origin=str(row["origin"]),
            confirmed_at=_optional_datetime_value(row["confirmed_at"]),
            disabled_at=_optional_datetime_value(row["disabled_at"]),
            deleted_at=_optional_datetime_value(row["deleted_at"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PersonalMemoryStoreCorruptionError(
            PersonalMemoryStoreReason.STORE_CORRUPT
        ) from exc


def _source_refs_json(record: PersonalMemoryRecord) -> str:
    return json.dumps(
        [source.to_dict() for source in record.source_refs],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _search_text(record: PersonalMemoryRecord) -> str:
    if record.status is not PersonalMemoryStatus.ACTIVE:
        return ""
    source_text = " ".join(
        " ".join(str(value) for value in source.to_dict().values() if value)
        for source in record.source_refs
    )
    return " ".join(
        (record.content, record.category.value.replace("_", " "), source_text)
    ).casefold()


def _owner_values(owner: PersonalMemoryOwner) -> tuple[str, str, str]:
    return owner.tenant_scope, owner.user_id, owner.namespace


def _require_owner(owner: PersonalMemoryOwner) -> None:
    if not isinstance(owner, PersonalMemoryOwner):
        raise PersonalMemoryStoreValidationError(
            PersonalMemoryStoreReason.INVALID_INPUT
        )


def _require_record(record: PersonalMemoryRecord) -> None:
    if not isinstance(record, PersonalMemoryRecord):
        raise PersonalMemoryStoreValidationError(
            PersonalMemoryStoreReason.INVALID_INPUT
        )


def _require_memory_id(memory_id: str) -> None:
    if (
        not isinstance(memory_id, str)
        or not memory_id.startswith("pmem_")
        or len(memory_id) != 37
        or any(character not in "0123456789abcdef" for character in memory_id[5:])
    ):
        raise PersonalMemoryStoreValidationError(
            PersonalMemoryStoreReason.INVALID_INPUT
        )


def _require_statuses(
    statuses: tuple[PersonalMemoryStatus, ...],
) -> tuple[PersonalMemoryStatus, ...]:
    if (
        not isinstance(statuses, tuple)
        or not statuses
        or any(not isinstance(status, PersonalMemoryStatus) for status in statuses)
    ):
        raise PersonalMemoryStoreValidationError(
            PersonalMemoryStoreReason.INVALID_INPUT
        )
    return tuple(dict.fromkeys(statuses))


def _require_fingerprint(value: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise PersonalMemoryStoreValidationError(
            PersonalMemoryStoreReason.INVALID_INPUT
        )


def _datetime_text(value: datetime) -> str:
    return ensure_utc(value, "datetime").isoformat()


def _optional_datetime_text(value: datetime | None) -> str | None:
    return None if value is None else _datetime_text(value)


def _datetime_value(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    return ensure_utc(parsed, "stored datetime")


def _optional_datetime_value(value: Any) -> datetime | None:
    return None if value is None else _datetime_value(value)


def _validate_database_path(value: str | os.PathLike[str]) -> Path:
    if isinstance(value, bytes):
        raise TypeError("database_path must be a text path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("database_path must be an absolute path")
    if not path.name or path.name in {".", ".."}:
        raise ValueError("database_path must identify a file")
    parent = path.parent
    parent_io = storage_io_path(parent)
    path_io = storage_io_path(path)
    if not parent_io.exists() or not parent_io.is_dir():
        raise ValueError("database_path parent must be an existing directory")
    _reject_linked_path(parent)
    # Check the leaf with lstat semantics before exists()/is_symlink() can
    # follow it; dangling links may appear absent in the Windows I/O namespace.
    if _is_link_or_reparse(path):
        raise ValueError("database files must not be symlinks or reparse points")
    if path_io.exists() or path_io.is_symlink():
        _assert_regular_file(path)
        if not os.access(path_io, os.W_OK):
            raise ValueError("database_path must be writable")
    if not os.access(parent_io, os.W_OK):
        raise ValueError("database_path parent must be writable")
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        sidecar_io = storage_io_path(sidecar)
        if sidecar_io.exists() or sidecar_io.is_symlink():
            _assert_regular_file(sidecar)
    # Resolve only after rejecting linked components, keeping the public path
    # logical even when a trusted recovery caller supplied an I/O namespace.
    return resolve_storage_path(path) if os.name == "nt" else path


def _reject_linked_path(path: Path) -> None:
    current = path
    while True:
        if _is_link_or_reparse(current):
            raise ValueError(
                "database_path must not traverse symlinks or reparse points"
            )
        if current.parent == current:
            break
        current = current.parent


def _assert_regular_database_files(path: Path) -> None:
    _assert_regular_file(path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        sidecar_io = storage_io_path(sidecar)
        if sidecar_io.exists() or sidecar_io.is_symlink():
            _assert_regular_file(sidecar)


def _assert_regular_file(path: Path) -> None:
    if _is_link_or_reparse(path):
        raise ValueError("database files must not be symlinks or reparse points")
    try:
        mode = storage_io_path(path).lstat().st_mode
    except OSError as exc:
        raise ValueError("database file cannot be inspected") from exc
    if not stat.S_ISREG(mode):
        raise ValueError("database_path must identify a regular file")


def _is_link_or_reparse(path: Path) -> bool:
    try:
        details = storage_io_path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ValueError("database path cannot be inspected") from exc
    return stat.S_ISLNK(details.st_mode) or bool(
        getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _map_sqlite_error(
    exc: sqlite3.Error,
    *,
    operation: str,
) -> PersonalMemoryStoreUnavailableError | PersonalMemoryStoreCorruptionError:
    message = str(exc).casefold()
    code = getattr(exc, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "database corruption",
            "malformed",
        )
    ):
        return PersonalMemoryStoreCorruptionError(
            PersonalMemoryStoreReason.STORE_CORRUPT
        )
    if primary_code in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_AUTH,
    } or any(
        marker in message
        for marker in (
            "database is locked",
            "readonly database",
            "read-only database",
            "unable to open database file",
            "disk i/o error",
            "database or disk is full",
            "permission denied",
            "wal journal mode is unavailable",
        )
    ):
        return PersonalMemoryStoreUnavailableError(
            PersonalMemoryStoreReason.STORE_UNAVAILABLE
        )
    return PersonalMemoryStoreCorruptionError(PersonalMemoryStoreReason.STORE_CORRUPT)


__all__ = [
    "DEFAULT_BUSY_TIMEOUT_SECONDS",
    "PERSONAL_MEMORY_SCHEMA_VERSION",
    "SQLitePersonalMemoryStore",
]
