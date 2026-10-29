"""Model connection storage against a real PostgreSQL.

Runs when ``NEWS_TEST_POSTGRES_DSN`` points at a disposable database, e.g.
``docker run --rm -e POSTGRES_PASSWORD=pw -p 127.0.0.1:55432:5432 postgres:17-alpine``
and ``NEWS_TEST_POSTGRES_DSN=postgresql://postgres:pw@127.0.0.1:55432/postgres``.
Every test works in its own schema, which is dropped afterwards.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from backend.foundation.llm_connections import (
    MAX_CONNECTIONS_PER_OWNER,
    ConnectionOwner,
    CredentialBinding,
    LLMPurpose,
    ModelCapabilities,
    ModelConnection,
    ModelConnectionConflictError,
    ModelConnectionNotFoundError,
    ModelConnectionStoreReason,
    ModelConnectionStoreUnavailableError,
)
from framework.llm.models.reasoning import ReasoningEffort
from framework.shared.secret_text import SecretText
from infrastructure.security.credential_cipher import (
    AesGcmCredentialCipher,
    CredentialKeyRing,
    generate_credential_key,
)
from infrastructure.storage.postgres.migrations import load_migration_sql
from infrastructure.storage.postgres.model_connection_repository import PostgresModelConnectionRepository

psycopg = pytest.importorskip("psycopg")
DSN = os.getenv("NEWS_TEST_POSTGRES_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="NEWS_TEST_POSTGRES_DSN is not set")

ALICE = ConnectionOwner("alice", tenant_id="lab")
BOB = ConnectionOwner("bob", tenant_id="lab")
CIPHER = AesGcmCredentialCipher(CredentialKeyRing(current_version=1, keys={1: os.urandom(32)}))
E = ReasoningEffort


@pytest.fixture
def repository():
    schema = f"test_llm_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(f"CREATE SCHEMA {schema}")
        admin.execute(f"SET search_path TO {schema}")
        admin.execute(load_migration_sql())

    def connect():
        return psycopg.connect(DSN, options=f"-c search_path={schema}")

    try:
        yield PostgresModelConnectionRepository(DSN, connection_factory=connect)
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA {schema} CASCADE")


def _connection(owner=ALICE, connection_id="conn-1", **changes) -> ModelConnection:
    values = dict(
        connection_id=connection_id,
        owner=owner,
        provider="deepseek",
        display_name="DeepSeek",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        context_window_tokens=1_000_000,
        max_output_tokens=393_216,
        reasoning_effort=E.HIGH,
        capabilities=ModelCapabilities(reasoning_efforts=(E.OFF, E.LOW, E.HIGH, E.MAX)),
    )
    values.update(changes)
    return ModelConnection(**values)


def _sealed(owner=ALICE, connection_id="conn-1", secret="sk-alice"):
    return CIPHER.encrypt(SecretText(secret), binding=CredentialBinding(owner, connection_id))


def test_create_read_and_decrypt_round_trip(repository) -> None:
    created = repository.create_connection(_connection(), _sealed())
    assert created.revision == 1 and created.credential_configured
    assert created.created_at is not None and created.verified_at is None
    assert created.capabilities.reasoning_efforts == (E.OFF, E.LOW, E.HIGH, E.MAX)

    stored = repository.load_with_credential(ALICE, "conn-1")
    assert stored.connection == created
    assert CIPHER.decrypt(stored.credential, binding=CredentialBinding(ALICE, "conn-1")).reveal() == "sk-alice"
    assert repository.list_connections(ALICE) == [created]


def test_owners_are_fully_isolated(repository) -> None:
    repository.create_connection(_connection(), _sealed())
    assert repository.list_connections(BOB) == []
    with pytest.raises(ModelConnectionNotFoundError):
        repository.get_connection(BOB, "conn-1")
    with pytest.raises(ModelConnectionNotFoundError):
        repository.load_with_credential(ConnectionOwner("alice", tenant_id="other-lab"), "conn-1")
    with pytest.raises(ModelConnectionNotFoundError):
        repository.update_connection(_connection(owner=BOB), expected_revision=1)
    with pytest.raises(ModelConnectionNotFoundError):
        repository.delete_connection(BOB, "conn-1", expected_revision=1)
    with pytest.raises(ModelConnectionNotFoundError):
        repository.replace_assignments(BOB, {LLMPurpose.ANSWER: "conn-1"})
    # The same id under another owner is a separate row.
    repository.create_connection(_connection(owner=BOB), _sealed(owner=BOB, secret="sk-bob"))
    assert repository.get_connection(ALICE, "conn-1").owner == ALICE


def test_update_bumps_revision_keeps_key_unless_replaced_and_clears_verification(repository) -> None:
    repository.create_connection(_connection(), _sealed())
    verified = repository.mark_verified(ALICE, "conn-1", revision=1, verified_at=datetime(2026, 10, 29, tzinfo=UTC))
    assert verified.verified_at is not None

    renamed = repository.update_connection(_connection(display_name="Fast"), expected_revision=1)
    assert (renamed.revision, renamed.display_name, renamed.verified_at) == (2, "Fast", None)
    kept = repository.load_with_credential(ALICE, "conn-1").credential
    assert CIPHER.decrypt(kept, binding=CredentialBinding(ALICE, "conn-1")).reveal() == "sk-alice"

    rekeyed = repository.update_connection(_connection(), expected_revision=2, credential=_sealed(secret="sk-new"))
    assert rekeyed.revision == 3
    new = repository.load_with_credential(ALICE, "conn-1").credential
    assert CIPHER.decrypt(new, binding=CredentialBinding(ALICE, "conn-1")).reveal() == "sk-new"


def test_stale_revisions_conflict(repository) -> None:
    repository.create_connection(_connection(), _sealed())
    repository.update_connection(_connection(display_name="v2"), expected_revision=1)
    with pytest.raises(ModelConnectionConflictError):
        repository.update_connection(_connection(display_name="lost"), expected_revision=1)
    with pytest.raises(ModelConnectionConflictError):
        repository.mark_verified(ALICE, "conn-1", revision=1, verified_at=datetime.now(UTC))
    with pytest.raises(ModelConnectionConflictError):
        repository.delete_connection(ALICE, "conn-1", expected_revision=1)
    with pytest.raises(ModelConnectionConflictError):
        repository.create_connection(_connection(), _sealed())


def test_assignments_replace_resolve_and_follow_deletes(repository) -> None:
    repository.create_connection(_connection(), _sealed())
    repository.create_connection(
        _connection(connection_id="conn-2", provider="anthropic", base_url="https://api.anthropic.com",
                    model="claude-haiku-5-5", reasoning_effort=E.LOW,
                    capabilities=ModelCapabilities(reasoning_efforts=(E.LOW, E.MEDIUM))),
        _sealed(connection_id="conn-2"),
    )
    assignments = repository.replace_assignments(ALICE, {
        LLMPurpose.ANSWER: "conn-1",
        LLMPurpose.EVIDENCE_SUMMARY: "conn-2",
    })
    assert assignments == {LLMPurpose.ANSWER: "conn-1", LLMPurpose.EVIDENCE_SUMMARY: "conn-2"}
    assert repository.resolve_purpose(ALICE, LLMPurpose.EVIDENCE_SUMMARY).model == "claude-haiku-5-5"
    assert repository.resolve_purpose(ALICE, LLMPurpose.SECTION_NAVIGATION) is None
    assert repository.resolve_purpose(BOB, LLMPurpose.ANSWER) is None

    with pytest.raises(ModelConnectionNotFoundError):
        repository.replace_assignments(ALICE, {LLMPurpose.ANSWER: "missing"})
    assert repository.list_assignments(ALICE) == assignments

    repository.delete_connection(ALICE, "conn-2", expected_revision=1)
    assert repository.list_assignments(ALICE) == {LLMPurpose.ANSWER: "conn-1"}
    assert repository.replace_assignments(ALICE, {}) == {}


def test_capacity_is_enforced_per_owner(repository) -> None:
    for index in range(MAX_CONNECTIONS_PER_OWNER):
        repository.create_connection(_connection(connection_id=f"c{index}"), _sealed(connection_id=f"c{index}"))
    with pytest.raises(ModelConnectionConflictError) as caught:
        repository.create_connection(_connection(connection_id="one-too-many"), _sealed(connection_id="x"))
    assert caught.value.reason is ModelConnectionStoreReason.OWNER_CAPACITY_EXCEEDED
    repository.create_connection(_connection(owner=BOB), _sealed(owner=BOB))


def test_database_checks_reject_rows_the_domain_would_refuse(repository) -> None:
    repository.create_connection(_connection(), _sealed())
    with pytest.raises(psycopg.errors.CheckViolation):
        with repository._connect() as connection:
            connection.execute(
                "UPDATE llm_model_connections SET credential_nonce = %s WHERE connection_id = 'conn-1'",
                (b"short",),
            )


def test_unreachable_database_is_reported_as_unavailable() -> None:
    def refuse():
        raise psycopg.OperationalError("connection refused")

    repository = PostgresModelConnectionRepository("postgresql://unused", connection_factory=refuse)
    with pytest.raises(ModelConnectionStoreUnavailableError):
        repository.list_connections(ALICE)
