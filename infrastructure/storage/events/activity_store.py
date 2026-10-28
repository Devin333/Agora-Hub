from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NoReturn
from urllib.parse import quote, unquote

from cryptography.fernet import Fernet, InvalidToken

from framework.events.canonical import PayloadReference, canonical_json_bytes
from framework.events.errors import (
    EventStoreCapacityError,
    EventStoreContentionError,
    EventStoreCorruptionError,
    EventStoreError,
    EventStoreUnavailableError,
)
from framework.events.runtime.activities import (
    REPLAY_ACTIVITY_RECORD_CONTENT_TYPE,
    RecordedActivityPayloadWrite,
    RecordedActivityWrite,
    ReplayActivityPayload,
    ReplayActivityRecord,
    ReplayActivityRecordingConflictError,
)
from framework.events.schema.security import SecurityClassification
from framework.events.schema.security import (
    REQUIRED_SECURE_PAYLOAD_CAPABILITIES,
    SecurePayloadValidation,
)
from framework.shared.json import json_loads
from framework.shared.time import format_datetime, utc_now


SQLiteConnectionFactory = Callable[[], sqlite3.Connection]
PostgresConnectionFactory = Callable[[], Any]

_SQLITE_SCHEMA = r"""
DROP TABLE IF EXISTS harness_activity_results;

CREATE TABLE IF NOT EXISTS event_activity_payloads (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    activity_id TEXT NOT NULL,
    payload_role TEXT NOT NULL,
    activity_kind TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    handler_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    security_classification TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content_checksum TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, activity_id, payload_role),
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (payload_role IN ('input', 'output', 'error')),
    CHECK (attempt >= 1 AND size_bytes >= 0),
    CHECK (security_classification IN ('public', 'internal', 'confidential', 'restricted')),
    CHECK (content_checksum GLOB 'sha256:[0-9a-f]*' AND length(content_checksum) = 71),
    CHECK (length(ciphertext) > 0)
);

CREATE TABLE IF NOT EXISTS event_activity_records (
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    activity_id TEXT NOT NULL,
    activity_kind TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    contract_version TEXT NOT NULL,
    handler_version TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    security_classification TEXT NOT NULL,
    status TEXT NOT NULL,
    record_checksum TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    ciphertext BLOB NOT NULL,
    accepted_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tenant_scope, activity_id),
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (attempt >= 1 AND size_bytes >= 0),
    CHECK (security_classification IN ('public', 'internal', 'confidential', 'restricted')),
    CHECK (status IN ('pending', 'succeeded', 'failed')),
    CHECK (record_checksum GLOB 'sha256:[0-9a-f]*' AND length(record_checksum) = 71),
    CHECK (length(ciphertext) > 0),
    CHECK (
        (status = 'pending' AND completed_at IS NULL)
        OR (status <> 'pending' AND completed_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS event_activity_access_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_scope TEXT NOT NULL,
    tenant_id TEXT,
    activity_id TEXT NOT NULL,
    object_role TEXT NOT NULL,
    operation TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    CHECK (tenant_scope = COALESCE(tenant_id, '')),
    CHECK (object_role IN ('record', 'input', 'output', 'error')),
    CHECK (operation IN ('write', 'read'))
);

CREATE INDEX IF NOT EXISTS idx_event_activity_audit_scope_activity
    ON event_activity_access_audit (tenant_scope, activity_id, audit_id);
"""


class _ActivityCipher:
    def __init__(self, key: str | bytes) -> None:
        value = key.encode("ascii") if isinstance(key, str) else bytes(key)
        try:
            self._fernet = Fernet(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "activity encryption key must be a URL-safe base64 Fernet key"
            ) from exc

    def encrypt(self, value: Mapping[str, Any]) -> bytes:
        return self._fernet.encrypt(canonical_json_bytes(value))

    def decrypt(self, value: bytes) -> Mapping[str, Any]:
        try:
            payload = json_loads(self._fernet.decrypt(bytes(value)).decode("utf-8"))
        except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EventStoreCorruptionError(
                "recorded activity ciphertext cannot be authenticated or decoded"
            ) from exc
        if not isinstance(payload, Mapping):
            raise EventStoreCorruptionError(
                "recorded activity ciphertext is not an object"
            )
        return payload



def validate_activity_encryption_key(key: str | bytes) -> None:
    """Raise ``ValueError`` unless ``key`` is a key the activity stores accept."""

    _ActivityCipher(key)


class SQLiteRecordedActivityStore:
    """Tenant-scoped encrypted activity history for local single-host runs."""

    def __init__(
        self,
        database: str | Path,
        *,
        encryption_key: str | bytes,
        busy_timeout_seconds: float = 5.0,
        synchronous: str = "FULL",
        connection_factory: SQLiteConnectionFactory | None = None,
    ) -> None:
        timeout = float(busy_timeout_seconds)
        if timeout < 0:
            raise ValueError("busy_timeout_seconds must be non-negative")
        policy = str(synchronous).strip().upper()
        if policy not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError("synchronous must be one of OFF, NORMAL, FULL, or EXTRA")
        self.database = str(database)
        if self.database == ":memory:" and connection_factory is None:
            raise ValueError("durable activity records require a file-backed database")
        self.busy_timeout_seconds = timeout
        self.synchronous = policy
        self._connection_factory = connection_factory
        self._uri = self.database.startswith("file:")
        self._cipher = _ActivityCipher(encryption_key)
        if connection_factory is None and not self._uri:
            try:
                Path(self.database).parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise EventStoreUnavailableError(
                    "create SQLite activity-store directory failed"
                ) from exc
        self._initialize_schema()

    def put_payload(
        self,
        payload: ReplayActivityPayload,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> RecordedActivityPayloadWrite:
        _validate_payload_scope(payload, tenant_id, classification)
        content = canonical_json_bytes(payload.content)
        reference = _payload_reference(payload, len(content))
        ciphertext = self._cipher.encrypt({"content": payload.content})
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM event_activity_payloads WHERE tenant_scope = ? "
                "AND activity_id = ? AND payload_role = ?",
                (_tenant_scope(tenant_id), payload.activity_id, payload.role.value),
            ).fetchone()
            if existing is not None:
                _validate_payload_row(existing, payload, reference)
                if self._cipher.decrypt(existing["ciphertext"]) != {
                    "content": payload.content
                }:
                    raise EventStoreCorruptionError(
                        "recorded activity payload ciphertext conflicts with indexes"
                    )
                self._audit_row(connection, existing, operation="read")
                return RecordedActivityPayloadWrite(payload, reference)
            try:
                connection.execute(
                    "INSERT INTO event_activity_payloads (tenant_scope, tenant_id, "
                    "activity_id, payload_role, activity_kind, attempt, contract_version, "
                    "handler_version, idempotency_key, security_classification, content_type, "
                    "content_checksum, size_bytes, ciphertext, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _tenant_scope(tenant_id),
                        tenant_id,
                        payload.activity_id,
                        payload.role.value,
                        payload.activity_kind.value,
                        payload.attempt,
                        payload.contract_version,
                        payload.handler_version,
                        payload.idempotency_key,
                        payload.security_classification.value,
                        payload.content_type,
                        payload.content_checksum,
                        len(content),
                        ciphertext,
                        format_datetime(utc_now()),
                    ),
                )
                self._audit(connection, payload, operation="write")
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    "SELECT * FROM event_activity_payloads WHERE tenant_scope = ? "
                    "AND activity_id = ? AND payload_role = ?",
                    (_tenant_scope(tenant_id), payload.activity_id, payload.role.value),
                ).fetchone()
                if existing is None:
                    raise
                _validate_payload_row(existing, payload, reference)
        return RecordedActivityPayloadWrite(payload, reference)

    def validate_reference(
        self,
        reference: Mapping[str, Any],
        *,
        tenant_id: str,
        classification: SecurityClassification,
    ) -> SecurePayloadValidation:
        parsed = PayloadReference.from_dict(reference)
        tenant = _required_tenant(tenant_id)
        classification = SecurityClassification(classification)
        with self._connection() as connection:
            row = _secure_reference_row(connection, parsed, tenant)
        if row is None:
            raise LookupError("secure activity reference is missing")
        if (
            row["content_checksum"] != parsed.expected_checksum
            or row["content_type"] != parsed.content_type
            or row["size_bytes"] != parsed.size_bytes
            or row["security_classification"] != classification.value
        ):
            raise EventStoreCorruptionError(
                "secure activity reference conflicts with durable storage"
            )
        return SecurePayloadValidation.for_reference(
            parsed.to_dict(),
            tenant_id=tenant,
            classification=classification,
            capabilities=REQUIRED_SECURE_PAYLOAD_CAPABILITIES,
        )

    def access_audit(
        self,
        activity_id: str,
        *,
        tenant_id: str | None,
    ) -> tuple[dict[str, Any], ...]:
        normalized_activity_id = str(activity_id).strip()
        if not normalized_activity_id:
            raise ValueError("activity_id is required")
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT object_role, operation, accessed_at FROM "
                "event_activity_access_audit WHERE tenant_scope = ? "
                "AND activity_id = ? ORDER BY audit_id",
                (_tenant_scope(tenant_id), normalized_activity_id),
            ).fetchall()
        return tuple(
            {
                "object_role": str(row["object_role"]),
                "operation": str(row["operation"]),
                "accessed_at": str(row["accessed_at"]),
            }
            for row in rows
        )

    def accept_record(
        self,
        record: ReplayActivityRecord,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> RecordedActivityWrite:
        _validate_record_scope(record, tenant_id, classification)
        with self._write() as connection:
            existing = self._read_record_row(
                connection, record.activity.activity_id, tenant_id
            )
            if existing is not None:
                return self._record_write_from_row(
                    existing, tenant_id, audit_connection=connection
                )
            ciphertext, size = _encrypted_record(self._cipher, record)
            try:
                connection.execute(
                    "INSERT INTO event_activity_records (tenant_scope, tenant_id, activity_id, "
                    "activity_kind, attempt, contract_version, handler_version, idempotency_key, "
                    "security_classification, status, record_checksum, size_bytes, ciphertext, "
                    "accepted_at, started_at, completed_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _sqlite_record_params(record, tenant_id, ciphertext, size),
                )
                self._audit_record(connection, record, tenant_id, operation="write")
            except sqlite3.IntegrityError:
                existing = self._read_record_row(
                    connection, record.activity.activity_id, tenant_id
                )
                if existing is None:
                    raise
                return self._record_write_from_row(
                    existing, tenant_id, audit_connection=connection
                )
        return RecordedActivityWrite(record, _record_reference(record, size))

    def complete_record(
        self,
        accepted_ref: PayloadReference,
        record: ReplayActivityRecord,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> RecordedActivityWrite:
        _validate_record_scope(record, tenant_id, classification)
        if record.outcome.status.value == "pending":
            raise ReplayActivityRecordingConflictError(
                "terminal activity record is required"
            )
        with self._write() as connection:
            existing = self._read_record_row(
                connection, record.activity.activity_id, tenant_id
            )
            if existing is None:
                raise ReplayActivityRecordingConflictError(
                    "accepted activity record is missing"
                )
            current = self._record_write_from_row(
                existing, tenant_id, audit_connection=connection
            )
            if current.record.outcome.status.value != "pending":
                if current.record != record:
                    raise ReplayActivityRecordingConflictError(
                        "activity already completed with a different outcome"
                    )
                return current
            if current.recorded_ref != accepted_ref:
                raise ReplayActivityRecordingConflictError(
                    "accepted activity reference is stale"
                )
            ciphertext, size = _encrypted_record(self._cipher, record)
            cursor = connection.execute(
                "UPDATE event_activity_records SET status = ?, record_checksum = ?, "
                "size_bytes = ?, ciphertext = ?, completed_at = ?, updated_at = ? "
                "WHERE tenant_scope = ? AND activity_id = ? AND status = 'pending' "
                "AND record_checksum = ?",
                (
                    record.outcome.status.value,
                    record.record_checksum,
                    size,
                    ciphertext,
                    format_datetime(record.outcome.completed_at),
                    format_datetime(utc_now()),
                    _tenant_scope(tenant_id),
                    record.activity.activity_id,
                    accepted_ref.expected_checksum,
                ),
            )
            if cursor.rowcount != 1:
                raise ReplayActivityRecordingConflictError(
                    "accepted activity changed before completion"
                )
            self._audit_record(connection, record, tenant_id, operation="write")
        return RecordedActivityWrite(record, _record_reference(record, size))

    def get_record(
        self,
        recorded_ref: PayloadReference,
        *,
        tenant_id: str | None,
    ) -> ReplayActivityRecord | None:
        activity_id = _activity_id_from_reference(
            recorded_ref, tenant_id, role="record"
        )
        with self._write() as connection:
            row = self._read_record_row(connection, activity_id, tenant_id)
            if row is None:
                return None
            write = self._record_write_from_row(
                row, tenant_id, audit_connection=connection
            )
        if write.recorded_ref != recorded_ref:
            raise EventStoreCorruptionError(
                "recorded activity reference conflicts with durable record"
            )
        return write.record

    def get_payload(
        self,
        reference: PayloadReference,
        *,
        tenant_id: str | None,
    ) -> Any:
        activity_id, role = _activity_payload_identity(reference, tenant_id)
        with self._write() as connection:
            row = connection.execute(
                "SELECT * FROM event_activity_payloads WHERE tenant_scope = ? "
                "AND activity_id = ? AND payload_role = ?",
                (_tenant_scope(tenant_id), activity_id, role),
            ).fetchone()
            if row is None:
                raise LookupError("recorded activity payload is missing")
            if (
                row["content_checksum"] != reference.expected_checksum
                or row["content_type"] != reference.content_type
                or row["size_bytes"] != reference.size_bytes
            ):
                raise EventStoreCorruptionError(
                    "recorded activity payload reference conflicts with durable payload"
                )
            value = self._cipher.decrypt(row["ciphertext"])
            self._audit_row(connection, row, operation="read")
        if set(value) != {"content"}:
            raise EventStoreCorruptionError(
                "recorded activity payload envelope is corrupt"
            )
        if len(canonical_json_bytes(value["content"])) != row["size_bytes"]:
            raise EventStoreCorruptionError("recorded activity payload size is corrupt")
        return value["content"]

    def _initialize_schema(self) -> None:
        with self._connection() as connection:
            try:
                journal_mode = str(
                    connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                )
                if self.database != ":memory:" and journal_mode.casefold() != "wal":
                    raise EventStoreUnavailableError(
                        f"SQLite durable activity records require WAL mode; got {journal_mode}"
                    )
                connection.executescript(_SQLITE_SCHEMA)
                connection.commit()
            except sqlite3.Error as exc:
                raise _map_sqlite_error(
                    exc, operation="initialize SQLite activity store"
                ) from exc

    def _open_connection(self) -> sqlite3.Connection:
        try:
            connection = (
                self._connection_factory()
                if self._connection_factory is not None
                else sqlite3.connect(
                    self.database,
                    timeout=self.busy_timeout_seconds,
                    isolation_level=None,
                    uri=self._uri,
                    check_same_thread=False,
                )
            )
            connection.row_factory = sqlite3.Row
            connection.execute(
                f"PRAGMA busy_timeout={int(self.busy_timeout_seconds * 1000)}"
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(f"PRAGMA synchronous={self.synchronous}")
            return connection
        except sqlite3.Error as exc:
            raise _map_sqlite_error(
                exc, operation="open SQLite activity store"
            ) from exc

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except (ReplayActivityRecordingConflictError, EventStoreCorruptionError):
            connection.rollback()
            raise
        except sqlite3.Error as exc:
            connection.rollback()
            raise _map_sqlite_error(
                exc, operation="write SQLite activity store"
            ) from exc
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read_record_row(
        self,
        connection: sqlite3.Connection,
        activity_id: str,
        tenant_id: str | None,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM event_activity_records WHERE tenant_scope = ? AND activity_id = ?",
            (_tenant_scope(tenant_id), activity_id),
        ).fetchone()

    def _record_write_from_row(
        self,
        row: Mapping[str, Any],
        tenant_id: str | None,
        *,
        audit_connection: sqlite3.Connection,
    ) -> RecordedActivityWrite:
        if row["tenant_scope"] != _tenant_scope(tenant_id):
            raise EventStoreCorruptionError("recorded activity tenant scope is corrupt")
        record = ReplayActivityRecord.from_dict(self._cipher.decrypt(row["ciphertext"]))
        reference = _record_reference(record, int(row["size_bytes"]))
        _validate_record_row(row, record, reference)
        self._audit_row(audit_connection, row, operation="read", object_role="record")
        return RecordedActivityWrite(record, reference)

    def _audit(
        self,
        connection: sqlite3.Connection,
        payload: ReplayActivityPayload,
        *,
        operation: str,
    ) -> None:
        connection.execute(
            "INSERT INTO event_activity_access_audit (tenant_scope, tenant_id, activity_id, "
            "object_role, operation, accessed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                _tenant_scope(payload.tenant_id),
                payload.tenant_id,
                payload.activity_id,
                payload.role.value,
                operation,
                format_datetime(utc_now()),
            ),
        )

    def _audit_record(
        self,
        connection: sqlite3.Connection,
        record: ReplayActivityRecord,
        tenant_id: str | None,
        *,
        operation: str,
    ) -> None:
        connection.execute(
            "INSERT INTO event_activity_access_audit (tenant_scope, tenant_id, activity_id, "
            "object_role, operation, accessed_at) VALUES (?, ?, ?, 'record', ?, ?)",
            (
                _tenant_scope(tenant_id),
                tenant_id,
                record.activity.activity_id,
                operation,
                format_datetime(utc_now()),
            ),
        )

    def _audit_row(
        self,
        connection: sqlite3.Connection,
        row: Mapping[str, Any],
        *,
        operation: str,
        object_role: str | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO event_activity_access_audit (tenant_scope, tenant_id, activity_id, "
            "object_role, operation, accessed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                row["tenant_scope"],
                row["tenant_id"],
                row["activity_id"],
                object_role or row["payload_role"],
                operation,
                format_datetime(utc_now()),
            ),
        )

    def _audit_values(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        activity_id: str,
        object_role: str,
        operation: str,
    ) -> None:
        connection.execute(
            "INSERT INTO event_activity_access_audit (tenant_scope, tenant_id, activity_id, "
            "object_role, operation, accessed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                tenant_id,
                activity_id,
                object_role,
                operation,
                format_datetime(utc_now()),
            ),
        )


class PostgresRecordedActivityStore:
    """Tenant-scoped encrypted activity history backed by migration 009."""

    def __init__(
        self,
        dsn: str,
        *,
        encryption_key: str | bytes,
        connection_factory: PostgresConnectionFactory | None = None,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("dsn is required")
        try:
            from infrastructure.storage.postgres.dsn import normalize_dsn

            if connection_factory is None:
                import psycopg
        except ModuleNotFoundError as exc:  # pragma: no cover - optional boundary
            if not _is_missing_psycopg(exc):
                raise
            raise EventStoreUnavailableError(
                "PostgreSQL activity-store adapter requires psycopg"
            ) from exc

        self.dsn = normalize_dsn(dsn.strip())
        if connection_factory is None:

            def connect() -> Any:
                return psycopg.connect(self.dsn)

            connection_factory = connect
        self._connection_factory = connection_factory
        self._cipher = _ActivityCipher(encryption_key)

    def put_payload(
        self,
        payload: ReplayActivityPayload,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> RecordedActivityPayloadWrite:
        _validate_payload_scope(payload, tenant_id, classification)
        content = canonical_json_bytes(payload.content)
        reference = _payload_reference(payload, len(content))
        ciphertext = self._cipher.encrypt({"content": payload.content})
        with self._transaction() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    "INSERT INTO event_activity_payloads (tenant_id, activity_id, "
                    "payload_role, activity_kind, attempt, contract_version, "
                    "handler_version, idempotency_key, security_classification, "
                    "content_type, content_checksum, size_bytes, ciphertext) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_scope, activity_id, payload_role) DO NOTHING "
                    "RETURNING *",
                    (
                        tenant_id,
                        payload.activity_id,
                        payload.role.value,
                        payload.activity_kind.value,
                        payload.attempt,
                        payload.contract_version,
                        payload.handler_version,
                        payload.idempotency_key,
                        payload.security_classification.value,
                        payload.content_type,
                        payload.content_checksum,
                        len(content),
                        ciphertext,
                    ),
                )
                row = cursor.fetchone()
                if row is None:
                    row = self._read_payload_row(
                        cursor,
                        payload.activity_id,
                        payload.role.value,
                        tenant_id,
                    )
                    if row is None:
                        raise EventStoreCorruptionError(
                            "PostgreSQL activity payload conflict returned no durable row"
                        )
                    _validate_payload_row(row, payload, reference)
                    if self._cipher.decrypt(row["ciphertext"]) != {
                        "content": payload.content
                    }:
                        raise EventStoreCorruptionError(
                            "recorded activity payload ciphertext conflicts with indexes"
                        )
                    self._audit_row(cursor, row, operation="read")
                    return RecordedActivityPayloadWrite(payload, reference)
                _validate_payload_row(row, payload, reference)
                self._audit_payload(cursor, payload, operation="write")
        return RecordedActivityPayloadWrite(payload, reference)

    def validate_reference(
        self,
        reference: Mapping[str, Any],
        *,
        tenant_id: str,
        classification: SecurityClassification,
    ) -> SecurePayloadValidation:
        parsed = PayloadReference.from_dict(reference)
        tenant = _required_tenant(tenant_id)
        classification = SecurityClassification(classification)
        with self._transaction(read_only=True) as connection:
            with self._cursor(connection) as cursor:
                row = self._secure_reference_row(cursor, parsed, tenant)
        if row is None:
            raise LookupError("secure activity reference is missing")
        if (
            row["content_checksum"] != parsed.expected_checksum
            or row["content_type"] != parsed.content_type
            or int(row["size_bytes"]) != parsed.size_bytes
            or row["security_classification"] != classification.value
        ):
            raise EventStoreCorruptionError(
                "secure activity reference conflicts with durable storage"
            )
        return SecurePayloadValidation.for_reference(
            parsed.to_dict(),
            tenant_id=tenant,
            classification=classification,
            capabilities=REQUIRED_SECURE_PAYLOAD_CAPABILITIES,
        )

    def access_audit(
        self,
        activity_id: str,
        *,
        tenant_id: str | None,
    ) -> tuple[dict[str, Any], ...]:
        normalized_activity_id = str(activity_id).strip()
        if not normalized_activity_id:
            raise ValueError("activity_id is required")
        with self._transaction(read_only=True) as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    "SELECT object_role, operation, accessed_at FROM "
                    "event_activity_access_audit WHERE tenant_scope = %s "
                    "AND activity_id = %s ORDER BY audit_id",
                    (_tenant_scope(tenant_id), normalized_activity_id),
                )
                rows = cursor.fetchall()
        return tuple(
            {
                "object_role": str(row["object_role"]),
                "operation": str(row["operation"]),
                "accessed_at": str(format_datetime(row["accessed_at"])),
            }
            for row in rows
        )

    def accept_record(
        self,
        record: ReplayActivityRecord,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> RecordedActivityWrite:
        _validate_record_scope(record, tenant_id, classification)
        ciphertext, size = _encrypted_record(self._cipher, record)
        with self._transaction() as connection:
            with self._cursor(connection) as cursor:
                cursor.execute(
                    "INSERT INTO event_activity_records (tenant_id, activity_id, "
                    "activity_kind, attempt, contract_version, handler_version, "
                    "idempotency_key, security_classification, status, record_checksum, "
                    "size_bytes, ciphertext, accepted_at, started_at, completed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (tenant_scope, activity_id) DO NOTHING RETURNING *",
                    _postgres_record_params(record, tenant_id, ciphertext, size),
                )
                row = cursor.fetchone()
                if row is None:
                    row = self._read_record_row(
                        cursor,
                        record.activity.activity_id,
                        tenant_id,
                    )
                    if row is None:
                        raise EventStoreCorruptionError(
                            "PostgreSQL activity record conflict returned no durable row"
                        )
                    return self._record_write_from_row(
                        cursor,
                        row,
                        tenant_id,
                        audit=True,
                    )
                _validate_record_row(
                    row,
                    record,
                    _record_reference(record, size),
                )
                self._audit_record(cursor, record, tenant_id, operation="write")
        return RecordedActivityWrite(record, _record_reference(record, size))

    def complete_record(
        self,
        accepted_ref: PayloadReference,
        record: ReplayActivityRecord,
        *,
        tenant_id: str | None,
        classification: SecurityClassification,
    ) -> RecordedActivityWrite:
        _validate_record_scope(record, tenant_id, classification)
        if record.outcome.status.value == "pending":
            raise ReplayActivityRecordingConflictError(
                "terminal activity record is required"
            )
        with self._transaction() as connection:
            with self._cursor(connection) as cursor:
                row = self._read_record_row(
                    cursor,
                    record.activity.activity_id,
                    tenant_id,
                    for_update=True,
                )
                if row is None:
                    raise ReplayActivityRecordingConflictError(
                        "accepted activity record is missing"
                    )
                current = self._record_write_from_row(
                    cursor,
                    row,
                    tenant_id,
                    audit=True,
                )
                if current.record.outcome.status.value != "pending":
                    if current.record != record:
                        raise ReplayActivityRecordingConflictError(
                            "activity already completed with a different outcome"
                        )
                    return current
                if current.recorded_ref != accepted_ref:
                    raise ReplayActivityRecordingConflictError(
                        "accepted activity reference is stale"
                    )
                ciphertext, size = _encrypted_record(self._cipher, record)
                cursor.execute(
                    "UPDATE event_activity_records SET status = %s, "
                    "record_checksum = %s, size_bytes = %s, ciphertext = %s, "
                    "completed_at = %s, updated_at = now() WHERE tenant_scope = %s "
                    "AND activity_id = %s AND status = 'pending' "
                    "AND record_checksum = %s RETURNING *",
                    (
                        record.outcome.status.value,
                        record.record_checksum,
                        size,
                        ciphertext,
                        record.outcome.completed_at,
                        _tenant_scope(tenant_id),
                        record.activity.activity_id,
                        accepted_ref.expected_checksum,
                    ),
                )
                updated = cursor.fetchone()
                if updated is None:
                    raise ReplayActivityRecordingConflictError(
                        "accepted activity changed before completion"
                    )
                _validate_record_row(
                    updated,
                    record,
                    _record_reference(record, size),
                )
                self._audit_record(cursor, record, tenant_id, operation="write")
        return RecordedActivityWrite(record, _record_reference(record, size))

    def get_record(
        self,
        recorded_ref: PayloadReference,
        *,
        tenant_id: str | None,
    ) -> ReplayActivityRecord | None:
        activity_id = _activity_id_from_reference(
            recorded_ref, tenant_id, role="record"
        )
        with self._transaction() as connection:
            with self._cursor(connection) as cursor:
                row = self._read_record_row(cursor, activity_id, tenant_id)
                if row is None:
                    return None
                write = self._record_write_from_row(
                    cursor,
                    row,
                    tenant_id,
                    audit=True,
                )
        if write.recorded_ref != recorded_ref:
            raise EventStoreCorruptionError(
                "recorded activity reference conflicts with durable record"
            )
        return write.record

    def get_payload(
        self,
        reference: PayloadReference,
        *,
        tenant_id: str | None,
    ) -> Any:
        activity_id, role = _activity_payload_identity(reference, tenant_id)
        with self._transaction() as connection:
            with self._cursor(connection) as cursor:
                row = self._read_payload_row(cursor, activity_id, role, tenant_id)
                if row is None:
                    raise LookupError("recorded activity payload is missing")
                if (
                    row["content_checksum"] != reference.expected_checksum
                    or row["content_type"] != reference.content_type
                    or int(row["size_bytes"]) != reference.size_bytes
                ):
                    raise EventStoreCorruptionError(
                        "recorded activity payload reference conflicts with durable payload"
                    )
                value = self._cipher.decrypt(row["ciphertext"])
                self._audit_row(cursor, row, operation="read")
        if set(value) != {"content"}:
            raise EventStoreCorruptionError(
                "recorded activity payload envelope is corrupt"
            )
        payload = ReplayActivityPayload(
            activity_id=str(row["activity_id"]),
            activity_kind=str(row["activity_kind"]),
            role=str(row["payload_role"]),
            content=value["content"],
            idempotency_key=str(row["idempotency_key"]),
            attempt=int(row["attempt"]),
            contract_version=str(row["contract_version"]),
            handler_version=str(row["handler_version"]),
            tenant_id=row["tenant_id"],
            security_classification=str(row["security_classification"]),
            content_type=str(row["content_type"]),
        )
        _validate_payload_row(row, payload, reference)
        return value["content"]

    @contextmanager
    def _transaction(self, *, read_only: bool = False) -> Iterator[Any]:
        connection: Any | None = None
        try:
            connection = self._connection_factory()
            if getattr(connection, "autocommit", False):
                connection.autocommit = False
            if read_only:
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            yield connection
            connection.commit()
        except (ReplayActivityRecordingConflictError, EventStoreCorruptionError):
            if connection is not None:
                connection.rollback()
            raise
        except BaseException as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except BaseException:
                    pass
            _reraise_postgres_activity_exception(exc)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except BaseException:
                    pass

    @contextmanager
    def _cursor(self, connection: Any) -> Iterator[Any]:
        try:
            from psycopg.rows import dict_row
        except ModuleNotFoundError as exc:  # pragma: no cover - optional boundary
            if not _is_missing_psycopg(exc):
                raise
            raise EventStoreUnavailableError(
                "PostgreSQL activity-store adapter requires psycopg"
            ) from exc
        with connection.cursor(row_factory=dict_row) as cursor:
            yield cursor

    def _read_payload_row(
        self,
        cursor: Any,
        activity_id: str,
        role: str,
        tenant_id: str | None,
    ) -> Mapping[str, Any] | None:
        cursor.execute(
            "SELECT * FROM event_activity_payloads WHERE tenant_scope = %s "
            "AND activity_id = %s AND payload_role = %s",
            (_tenant_scope(tenant_id), activity_id, role),
        )
        return cursor.fetchone()

    def _read_record_row(
        self,
        cursor: Any,
        activity_id: str,
        tenant_id: str | None,
        *,
        for_update: bool = False,
    ) -> Mapping[str, Any] | None:
        suffix = " FOR UPDATE" if for_update else ""
        cursor.execute(
            "SELECT * FROM event_activity_records WHERE tenant_scope = %s "
            "AND activity_id = %s" + suffix,
            (_tenant_scope(tenant_id), activity_id),
        )
        return cursor.fetchone()

    def _record_write_from_row(
        self,
        cursor: Any,
        row: Mapping[str, Any],
        tenant_id: str | None,
        *,
        audit: bool,
    ) -> RecordedActivityWrite:
        if row["tenant_scope"] != _tenant_scope(tenant_id):
            raise EventStoreCorruptionError("recorded activity tenant scope is corrupt")
        record = ReplayActivityRecord.from_dict(self._cipher.decrypt(row["ciphertext"]))
        reference = _record_reference(record, int(row["size_bytes"]))
        _validate_record_row(row, record, reference)
        if audit:
            self._audit_row(cursor, row, operation="read", object_role="record")
        return RecordedActivityWrite(record, reference)

    def _secure_reference_row(
        self,
        cursor: Any,
        reference: PayloadReference,
        tenant_id: str,
    ) -> Mapping[str, Any] | None:
        if reference.uri.endswith("/record"):
            activity_id = _activity_id_from_reference(
                reference,
                tenant_id,
                role="record",
            )
            cursor.execute(
                "SELECT tenant_scope, tenant_id, activity_id, "
                "security_classification, %s AS content_type, "
                "record_checksum AS content_checksum, size_bytes "
                "FROM event_activity_records WHERE tenant_scope = %s "
                "AND activity_id = %s",
                (REPLAY_ACTIVITY_RECORD_CONTENT_TYPE, tenant_id, activity_id),
            )
            return cursor.fetchone()
        activity_id, role = _activity_payload_identity(reference, tenant_id)
        cursor.execute(
            "SELECT tenant_scope, tenant_id, activity_id, security_classification, "
            "content_type, content_checksum, size_bytes FROM event_activity_payloads "
            "WHERE tenant_scope = %s AND activity_id = %s AND payload_role = %s",
            (tenant_id, activity_id, role),
        )
        return cursor.fetchone()

    def _audit_payload(
        self,
        cursor: Any,
        payload: ReplayActivityPayload,
        *,
        operation: str,
    ) -> None:
        self._audit_values(
            cursor,
            tenant_id=payload.tenant_id,
            activity_id=payload.activity_id,
            object_role=payload.role.value,
            operation=operation,
        )

    def _audit_record(
        self,
        cursor: Any,
        record: ReplayActivityRecord,
        tenant_id: str | None,
        *,
        operation: str,
    ) -> None:
        self._audit_values(
            cursor,
            tenant_id=tenant_id,
            activity_id=record.activity.activity_id,
            object_role="record",
            operation=operation,
        )

    def _audit_row(
        self,
        cursor: Any,
        row: Mapping[str, Any],
        *,
        operation: str,
        object_role: str | None = None,
    ) -> None:
        self._audit_values(
            cursor,
            tenant_id=row["tenant_id"],
            activity_id=str(row["activity_id"]),
            object_role=object_role or str(row["payload_role"]),
            operation=operation,
        )

    def _audit_values(
        self,
        cursor: Any,
        *,
        tenant_id: str | None,
        activity_id: str,
        object_role: str,
        operation: str,
    ) -> None:
        cursor.execute(
            "INSERT INTO event_activity_access_audit (tenant_id, activity_id, "
            "object_role, operation) VALUES (%s, %s, %s, %s)",
            (tenant_id, activity_id, object_role, operation),
        )


def _validate_payload_scope(
    payload: ReplayActivityPayload,
    tenant_id: str | None,
    classification: SecurityClassification,
) -> None:
    if not isinstance(payload, ReplayActivityPayload):
        raise TypeError("payload must be ReplayActivityPayload")
    if (
        payload.tenant_id != tenant_id
        or payload.security_classification is not classification
    ):
        raise ReplayActivityRecordingConflictError(
            "activity payload scope does not match storage authority"
        )


def _validate_record_scope(
    record: ReplayActivityRecord,
    tenant_id: str | None,
    classification: SecurityClassification,
) -> None:
    if not isinstance(record, ReplayActivityRecord):
        raise TypeError("record must be ReplayActivityRecord")
    record.verify_integrity()
    activity = record.activity
    if (
        activity.tenant_id != tenant_id
        or activity.security_classification is not classification
    ):
        raise ReplayActivityRecordingConflictError(
            "activity record scope does not match storage authority"
        )


def _payload_reference(
    payload: ReplayActivityPayload, size_bytes: int
) -> PayloadReference:
    return PayloadReference(
        uri=_activity_uri(payload.tenant_id, payload.activity_id, payload.role.value),
        expected_checksum=payload.content_checksum,
        content_type=payload.content_type,
        size_bytes=size_bytes,
    )


def _record_reference(
    record: ReplayActivityRecord, size_bytes: int
) -> PayloadReference:
    return PayloadReference(
        uri=_activity_uri(
            record.activity.tenant_id, record.activity.activity_id, "record"
        ),
        expected_checksum=record.record_checksum,
        content_type=REPLAY_ACTIVITY_RECORD_CONTENT_TYPE,
        size_bytes=size_bytes,
    )


def _activity_uri(tenant_id: str | None, activity_id: str, role: str) -> str:
    tenant = "_global" if tenant_id is None else quote(tenant_id, safe="")
    return f"secure-activity://{tenant}/{quote(activity_id, safe='')}/{role}"


def _activity_id_from_reference(
    reference: PayloadReference,
    tenant_id: str | None,
    *,
    role: str,
    scheme: str = "secure-activity",
) -> str:
    tenant = "_global" if tenant_id is None else quote(tenant_id, safe="")
    expected_prefix = f"{scheme}://{tenant}/"
    if not reference.uri.startswith(expected_prefix) or not reference.uri.endswith(
        f"/{role}"
    ):
        raise ReplayActivityRecordingConflictError(
            "activity reference does not match tenant or role"
        )
    encoded = reference.uri[len(expected_prefix) : -len(f"/{role}")]
    if not encoded or "/" in encoded:
        raise ReplayActivityRecordingConflictError(
            "activity reference identity is invalid"
        )
    return unquote(encoded)


def _activity_payload_identity(
    reference: PayloadReference,
    tenant_id: str | None,
) -> tuple[str, str]:
    for role in ("input", "output", "error"):
        try:
            return _activity_id_from_reference(reference, tenant_id, role=role), role
        except ReplayActivityRecordingConflictError:
            continue
    raise ReplayActivityRecordingConflictError(
        "activity payload reference does not match tenant or role"
    )


def _encrypted_record(
    cipher: _ActivityCipher,
    record: ReplayActivityRecord,
) -> tuple[bytes, int]:
    value = record.to_dict()
    return cipher.encrypt(value), len(canonical_json_bytes(value))


def _secure_reference_row(
    connection: sqlite3.Connection,
    reference: PayloadReference,
    tenant_id: str,
) -> sqlite3.Row | None:
    uri = reference.uri
    if uri.endswith("/record"):
        activity_id = _activity_id_from_reference(
            reference,
            tenant_id,
            role="record",
        )
        return connection.execute(
            "SELECT tenant_scope, tenant_id, activity_id, security_classification, "
            "? AS content_type, record_checksum AS content_checksum, size_bytes "
            "FROM event_activity_records WHERE tenant_scope = ? AND activity_id = ?",
            (REPLAY_ACTIVITY_RECORD_CONTENT_TYPE, tenant_id, activity_id),
        ).fetchone()
    activity_id, role = _activity_payload_identity(reference, tenant_id)
    return connection.execute(
        "SELECT tenant_scope, tenant_id, activity_id, security_classification, "
        "content_type, content_checksum, size_bytes FROM event_activity_payloads "
        "WHERE tenant_scope = ? AND activity_id = ? AND payload_role = ?",
        (tenant_id, activity_id, role),
    ).fetchone()


def _sqlite_record_params(
    record: ReplayActivityRecord,
    tenant_id: str | None,
    ciphertext: bytes,
    size_bytes: int,
) -> tuple[Any, ...]:
    activity = record.activity
    outcome = record.outcome
    now = format_datetime(utc_now())
    return (
        _tenant_scope(tenant_id),
        tenant_id,
        activity.activity_id,
        activity.activity_kind.value,
        activity.attempt,
        activity.contract_version,
        activity.handler_version,
        activity.idempotency_key,
        activity.security_classification.value,
        outcome.status.value,
        record.record_checksum,
        size_bytes,
        ciphertext,
        format_datetime(activity.accepted_at),
        format_datetime(outcome.started_at),
        format_datetime(outcome.completed_at),
        now,
        now,
    )


def _postgres_record_params(
    record: ReplayActivityRecord,
    tenant_id: str | None,
    ciphertext: bytes,
    size_bytes: int,
) -> tuple[Any, ...]:
    activity = record.activity
    outcome = record.outcome
    return (
        tenant_id,
        activity.activity_id,
        activity.activity_kind.value,
        activity.attempt,
        activity.contract_version,
        activity.handler_version,
        activity.idempotency_key,
        activity.security_classification.value,
        outcome.status.value,
        record.record_checksum,
        size_bytes,
        ciphertext,
        activity.accepted_at,
        outcome.started_at,
        outcome.completed_at,
    )


def _payload_identity(payload: ReplayActivityPayload) -> tuple[Any, ...]:
    return (
        _tenant_scope(payload.tenant_id),
        payload.tenant_id,
        payload.activity_id,
        payload.role.value,
        payload.activity_kind.value,
        payload.attempt,
        payload.contract_version,
        payload.handler_version,
        payload.idempotency_key,
        payload.security_classification.value,
        payload.content_type,
        payload.content_checksum,
        len(canonical_json_bytes(payload.content)),
    )


def _validate_payload_row(
    row: Mapping[str, Any],
    payload: ReplayActivityPayload,
    reference: PayloadReference,
) -> None:
    actual = tuple(
        row[name]
        for name in (
            "tenant_scope",
            "tenant_id",
            "activity_id",
            "payload_role",
            "activity_kind",
            "attempt",
            "contract_version",
            "handler_version",
            "idempotency_key",
            "security_classification",
            "content_type",
            "content_checksum",
            "size_bytes",
        )
    )
    if (
        actual != _payload_identity(payload)
        or reference.size_bytes != row["size_bytes"]
    ):
        raise ReplayActivityRecordingConflictError(
            "recorded activity payload identity or content conflicts"
        )


def _validate_record_row(
    row: Mapping[str, Any],
    record: ReplayActivityRecord,
    reference: PayloadReference,
) -> None:
    activity = record.activity
    outcome = record.outcome
    expected = (
        _tenant_scope(activity.tenant_id),
        activity.tenant_id,
        activity.activity_id,
        activity.activity_kind.value,
        activity.attempt,
        activity.contract_version,
        activity.handler_version,
        activity.idempotency_key,
        activity.security_classification.value,
        outcome.status.value,
        record.record_checksum,
        reference.size_bytes,
        format_datetime(activity.accepted_at),
        format_datetime(outcome.started_at),
        format_datetime(outcome.completed_at),
    )
    actual = tuple(
        (
            _row_datetime(row[name])
            if name in {"accepted_at", "started_at", "completed_at"}
            else row[name]
        )
        for name in (
            "tenant_scope",
            "tenant_id",
            "activity_id",
            "activity_kind",
            "attempt",
            "contract_version",
            "handler_version",
            "idempotency_key",
            "security_classification",
            "status",
            "record_checksum",
            "size_bytes",
            "accepted_at",
            "started_at",
            "completed_at",
        )
    )
    if actual != expected:
        raise EventStoreCorruptionError(
            "recorded activity indexes disagree with encrypted record"
        )


def _tenant_scope(tenant_id: str | None) -> str:
    if tenant_id is None:
        return ""
    value = str(tenant_id).strip()
    if not value:
        raise ValueError("tenant_id must not be blank")
    return value


def _required_tenant(tenant_id: str) -> str:
    value = _tenant_scope(tenant_id)
    if not value:
        raise ValueError("tenant_id is required")
    return value


def _row_datetime(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "tzinfo"):
        return format_datetime(value)
    return str(value)


def _is_missing_psycopg(exc: ModuleNotFoundError) -> bool:
    missing = str(exc.name or "")
    return missing == "psycopg" or missing.startswith("psycopg.")


def _map_sqlite_error(exc: sqlite3.Error, *, operation: str) -> EventStoreError:
    message = str(exc).lower()
    code = getattr(exc, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    if primary_code == sqlite3.SQLITE_FULL or "database or disk is full" in message:
        return EventStoreCapacityError(f"{operation}: durable capacity is exhausted")
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
        )
    ):
        return EventStoreCorruptionError(f"{operation}: SQLite store is corrupt")
    if primary_code in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_AUTH,
    } or any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "readonly database",
            "read-only database",
            "unable to open database file",
            "disk i/o error",
            "permission denied",
        )
    ):
        return EventStoreUnavailableError(f"{operation}: durable store is unavailable")
    return EventStoreCorruptionError(f"{operation}: SQLite operation failed")


def _reraise_postgres_activity_exception(exc: BaseException) -> NoReturn:
    if isinstance(
        exc,
        (
            ReplayActivityRecordingConflictError,
            EventStoreError,
            LookupError,
            TypeError,
            ValueError,
        ),
    ):
        raise exc
    try:
        import psycopg
    except ImportError:
        raise EventStoreUnavailableError(
            "PostgreSQL activity-store adapter requires psycopg"
        ) from exc
    if not isinstance(exc, psycopg.Error):
        raise exc
    sqlstate = str(getattr(exc, "sqlstate", "") or "")
    if sqlstate in {"40001", "40P01", "55P03"}:
        raise EventStoreContentionError(
            "PostgreSQL activity-store operation encountered retryable contention"
        ) from exc
    if sqlstate in {"53100", "53200", "53400"}:
        raise EventStoreCapacityError(
            "PostgreSQL activity-store capacity is exhausted"
        ) from exc
    if (
        isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))
        or sqlstate.startswith("08")
        or sqlstate in {"53300", "57P01", "57P02", "57P03"}
    ):
        raise EventStoreUnavailableError(
            "PostgreSQL activity store is unavailable"
        ) from exc
    raise EventStoreCorruptionError(
        "PostgreSQL activity-store operation failed"
    ) from exc


__all__ = [
    "PostgresRecordedActivityStore",
    "SQLiteRecordedActivityStore",
    "validate_activity_encryption_key",
]
