"""PostgreSQL storage for user model connections and purpose assignments.

Every statement is scoped by ``(tenant_scope, user_id)``, so one user can never
read, change or route to another user's connection. Writes use the revision the
caller read as an optimistic lock.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Callable

import psycopg

from backend.foundation.llm_connections.errors import (
    ModelConnectionConflictError,
    ModelConnectionNotFoundError,
    ModelConnectionStoreError,
    ModelConnectionStoreReason,
    ModelConnectionStoreUnavailableError,
)
from backend.foundation.llm_connections.models import (
    MAX_CONNECTIONS_PER_OWNER,
    ConnectionOwner,
    LLMPurpose,
    ModelCapabilities,
    ModelConnection,
)
from backend.foundation.llm_connections.ports import EncryptedCredential, StoredConnection
from infrastructure.storage.postgres.dsn import normalize_dsn

ConnectionFactory = Callable[[], Any]

_COLUMNS = """
    connection_id, provider, display_name, base_url, model,
    context_window_tokens, max_output_tokens, reasoning_effort, reasoning_parameter,
    capabilities, revision, verified_at, created_at, updated_at
"""
_CREDENTIAL_COLUMNS = "credential_ciphertext, credential_nonce, credential_key_version"
_OWNER = "tenant_scope = %s AND user_id = %s"

_SELECT_ONE = f"SELECT {_COLUMNS} FROM llm_model_connections WHERE {_OWNER} AND connection_id = %s"
_SELECT_ONE_WITH_CREDENTIAL = (
    f"SELECT {_COLUMNS}, {_CREDENTIAL_COLUMNS} FROM llm_model_connections "
    f"WHERE {_OWNER} AND connection_id = %s"
)
_SELECT_ALL = f"SELECT {_COLUMNS} FROM llm_model_connections WHERE {_OWNER} ORDER BY created_at, connection_id"
_COUNT_FOR_UPDATE = (
    # Locks the owner's rows so concurrent creates cannot both pass the capacity check.
    f"SELECT connection_id FROM llm_model_connections WHERE {_OWNER} FOR UPDATE"
)
_INSERT = f"""
INSERT INTO llm_model_connections (
    tenant_scope, user_id, connection_id, provider, display_name, base_url, model,
    context_window_tokens, max_output_tokens, reasoning_effort, reasoning_parameter,
    capabilities, {_CREDENTIAL_COLUMNS}, revision
)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,1)
ON CONFLICT (tenant_scope, user_id, connection_id) DO NOTHING
RETURNING {_COLUMNS}
"""
_UPDATE = f"""
UPDATE llm_model_connections SET
    provider = %s, display_name = %s, base_url = %s, model = %s,
    context_window_tokens = %s, max_output_tokens = %s,
    reasoning_effort = %s, reasoning_parameter = %s, capabilities = %s::jsonb,
    revision = revision + 1, verified_at = NULL, updated_at = now()
WHERE {_OWNER} AND connection_id = %s AND revision = %s
RETURNING {_COLUMNS}
"""
_UPDATE_WITH_CREDENTIAL = f"""
UPDATE llm_model_connections SET
    provider = %s, display_name = %s, base_url = %s, model = %s,
    context_window_tokens = %s, max_output_tokens = %s,
    reasoning_effort = %s, reasoning_parameter = %s, capabilities = %s::jsonb,
    credential_ciphertext = %s, credential_nonce = %s, credential_key_version = %s,
    revision = revision + 1, verified_at = NULL, updated_at = now()
WHERE {_OWNER} AND connection_id = %s AND revision = %s
RETURNING {_COLUMNS}
"""
_MARK_VERIFIED = f"""
UPDATE llm_model_connections SET verified_at = %s
WHERE {_OWNER} AND connection_id = %s AND revision = %s
RETURNING {_COLUMNS}
"""
_DELETE = f"DELETE FROM llm_model_connections WHERE {_OWNER} AND connection_id = %s AND revision = %s"
_EXISTS = f"SELECT revision FROM llm_model_connections WHERE {_OWNER} AND connection_id = %s"
_SELECT_ASSIGNMENTS = f"SELECT purpose, connection_id FROM llm_purpose_assignments WHERE {_OWNER} ORDER BY purpose"
_DELETE_ASSIGNMENTS = f"DELETE FROM llm_purpose_assignments WHERE {_OWNER}"
_INSERT_ASSIGNMENT = """
INSERT INTO llm_purpose_assignments (tenant_scope, user_id, purpose, connection_id)
VALUES (%s,%s,%s,%s)
"""
_RESOLVE_PURPOSE = f"""
SELECT {", ".join(f"c.{column.strip()}" for column in _COLUMNS.split(","))}
FROM llm_purpose_assignments a
JOIN llm_model_connections c
  ON c.tenant_scope = a.tenant_scope AND c.user_id = a.user_id AND c.connection_id = a.connection_id
WHERE a.tenant_scope = %s AND a.user_id = %s AND a.purpose = %s
"""


class PostgresModelConnectionRepository:
    def __init__(self, dsn: str, *, connection_factory: ConnectionFactory | None = None) -> None:
        self._dsn = normalize_dsn(dsn)
        self._connect = connection_factory or (lambda: psycopg.connect(self._dsn))

    def list_connections(self, owner: ConnectionOwner) -> list[ModelConnection]:
        rows = self._read(lambda cur: _fetchall(cur, _SELECT_ALL, _owner_params(owner)))
        return [_connection(owner, row) for row in rows]

    def get_connection(self, owner: ConnectionOwner, connection_id: str) -> ModelConnection:
        row = self._read(lambda cur: _fetchone(cur, _SELECT_ONE, (*_owner_params(owner), connection_id)))
        if row is None:
            raise ModelConnectionNotFoundError()
        return _connection(owner, row)

    def load_with_credential(self, owner: ConnectionOwner, connection_id: str) -> StoredConnection:
        row = self._read(
            lambda cur: _fetchone(cur, _SELECT_ONE_WITH_CREDENTIAL, (*_owner_params(owner), connection_id))
        )
        if row is None:
            raise ModelConnectionNotFoundError()
        return StoredConnection(connection=_connection(owner, row[:14]), credential=_credential(row[14:]))

    def create_connection(self, connection: ModelConnection, credential: EncryptedCredential) -> ModelConnection:
        owner = connection.owner

        def write(cur) -> tuple:
            existing = _fetchall(cur, _COUNT_FOR_UPDATE, _owner_params(owner))
            if len(existing) >= MAX_CONNECTIONS_PER_OWNER:
                raise ModelConnectionConflictError(ModelConnectionStoreReason.OWNER_CAPACITY_EXCEEDED)
            row = _fetchone(
                cur,
                _INSERT,
                (
                    *_owner_params(owner),
                    connection.connection_id,
                    *_settings_params(connection),
                    credential.ciphertext,
                    credential.nonce,
                    credential.key_version,
                ),
            )
            if row is None:
                raise ModelConnectionConflictError()
            return row

        return _connection(owner, self._write(write))

    def update_connection(
        self,
        connection: ModelConnection,
        *,
        expected_revision: int,
        credential: EncryptedCredential | None = None,
    ) -> ModelConnection:
        owner = connection.owner
        if credential is None:
            sql = _UPDATE
            params = (*_settings_params(connection), *_owner_params(owner), connection.connection_id, expected_revision)
        else:
            sql = _UPDATE_WITH_CREDENTIAL
            params = (
                *_settings_params(connection),
                credential.ciphertext,
                credential.nonce,
                credential.key_version,
                *_owner_params(owner),
                connection.connection_id,
                expected_revision,
            )

        def write(cur) -> tuple:
            row = _fetchone(cur, sql, params)
            if row is None:
                self._raise_missing_or_conflict(cur, owner, connection.connection_id)
            return row

        return _connection(owner, self._write(write))

    def mark_verified(
        self,
        owner: ConnectionOwner,
        connection_id: str,
        *,
        revision: int,
        verified_at: datetime,
    ) -> ModelConnection:
        def write(cur) -> tuple:
            row = _fetchone(cur, _MARK_VERIFIED, (verified_at, *_owner_params(owner), connection_id, revision))
            if row is None:
                self._raise_missing_or_conflict(cur, owner, connection_id)
            return row

        return _connection(owner, self._write(write))

    def delete_connection(self, owner: ConnectionOwner, connection_id: str, *, expected_revision: int) -> None:
        def write(cur) -> None:
            cur.execute(_DELETE, (*_owner_params(owner), connection_id, expected_revision))
            if cur.rowcount == 0:
                self._raise_missing_or_conflict(cur, owner, connection_id)

        # Purpose assignments go with it through ON DELETE CASCADE.
        self._write(write)

    def list_assignments(self, owner: ConnectionOwner) -> dict[LLMPurpose, str]:
        rows = self._read(lambda cur: _fetchall(cur, _SELECT_ASSIGNMENTS, _owner_params(owner)))
        return _assignments(rows)

    def replace_assignments(
        self,
        owner: ConnectionOwner,
        assignments: Mapping[LLMPurpose, str],
    ) -> dict[LLMPurpose, str]:
        normalized = {LLMPurpose(purpose): connection_id for purpose, connection_id in assignments.items()}

        def write(cur) -> list:
            # Lock the owner's connections so none disappears between the check and the insert.
            owned = {row[0] for row in _fetchall(cur, _COUNT_FOR_UPDATE, _owner_params(owner))}
            if any(connection_id not in owned for connection_id in normalized.values()):
                raise ModelConnectionNotFoundError()
            cur.execute(_DELETE_ASSIGNMENTS, _owner_params(owner))
            for purpose, connection_id in sorted(normalized.items()):
                cur.execute(_INSERT_ASSIGNMENT, (*_owner_params(owner), purpose.value, connection_id))
            return _fetchall(cur, _SELECT_ASSIGNMENTS, _owner_params(owner))

        return _assignments(self._write(write))

    def resolve_purpose(self, owner: ConnectionOwner, purpose: LLMPurpose) -> ModelConnection | None:
        row = self._read(
            lambda cur: _fetchone(cur, _RESOLVE_PURPOSE, (*_owner_params(owner), LLMPurpose(purpose).value))
        )
        return None if row is None else _connection(owner, row)

    @staticmethod
    def _raise_missing_or_conflict(cur, owner: ConnectionOwner, connection_id: str) -> None:
        if _fetchone(cur, _EXISTS, (*_owner_params(owner), connection_id)) is None:
            raise ModelConnectionNotFoundError()
        raise ModelConnectionConflictError()

    def _read(self, operation):
        try:
            with self._connect() as connection:
                with connection.cursor() as cur:
                    return operation(cur)
        except ModelConnectionStoreError:
            raise
        except psycopg.Error as exc:
            raise ModelConnectionStoreUnavailableError() from exc

    def _write(self, operation):
        try:
            with self._connect() as connection:
                try:
                    with connection.cursor() as cur:
                        result = operation(cur)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise
        except ModelConnectionStoreError:
            raise
        except psycopg.errors.IntegrityError as exc:
            raise ModelConnectionConflictError() from exc
        except psycopg.Error as exc:
            raise ModelConnectionStoreUnavailableError() from exc


def _owner_params(owner: ConnectionOwner) -> tuple[str, str]:
    return owner.tenant_scope, owner.user_id


def _settings_params(connection: ModelConnection) -> tuple:
    return (
        connection.provider.value,
        connection.display_name,
        connection.base_url,
        connection.model,
        connection.context_window_tokens,
        connection.max_output_tokens,
        connection.reasoning_effort.value if connection.reasoning_effort is not None else None,
        connection.reasoning_parameter.value if connection.reasoning_parameter is not None else None,
        json.dumps(connection.capabilities.to_dict(), sort_keys=True),
    )


def _connection(owner: ConnectionOwner, row) -> ModelConnection:
    try:
        (
            connection_id, provider, display_name, base_url, model,
            context_window, max_output, effort, parameter,
            capabilities, revision, verified_at, created_at, updated_at,
        ) = row
        if isinstance(capabilities, str):
            capabilities = json.loads(capabilities)
        return ModelConnection(
            connection_id=connection_id,
            owner=owner,
            provider=provider,
            display_name=display_name,
            base_url=base_url,
            model=model,
            context_window_tokens=context_window,
            max_output_tokens=max_output,
            reasoning_effort=effort,
            reasoning_parameter=parameter,
            capabilities=ModelCapabilities.from_dict(capabilities or {}),
            # Rows cannot exist without a credential (NOT NULL columns).
            credential_configured=True,
            revision=revision,
            verified_at=verified_at,
            created_at=created_at,
            updated_at=updated_at,
        )
    except (TypeError, ValueError) as exc:
        raise ModelConnectionStoreError(ModelConnectionStoreReason.STORE_CORRUPT) from exc


def _credential(row) -> EncryptedCredential:
    ciphertext, nonce, key_version = row
    try:
        return EncryptedCredential(ciphertext=bytes(ciphertext), nonce=bytes(nonce), key_version=key_version)
    except (TypeError, ValueError) as exc:
        raise ModelConnectionStoreError(ModelConnectionStoreReason.STORE_CORRUPT) from exc


def _assignments(rows) -> dict[LLMPurpose, str]:
    try:
        return {LLMPurpose(purpose): connection_id for purpose, connection_id in rows}
    except ValueError as exc:
        raise ModelConnectionStoreError(ModelConnectionStoreReason.STORE_CORRUPT) from exc


def _fetchone(cur, sql: str, params: tuple):
    cur.execute(sql, params)
    return cur.fetchone()


def _fetchall(cur, sql: str, params: tuple) -> list:
    cur.execute(sql, params)
    return list(cur.fetchall())


__all__ = ["PostgresModelConnectionRepository"]
