from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from uuid import uuid4

import pytest

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.domain.personal_memory import (
    PersonalMemoryOwner,
    PersonalMemorySourceRef,
    PersonalMemoryStatus,
)
from backend.research.ports.personal_memory import (
    PersonalMemoryStoreConflictError,
    PersonalMemoryStoreCorruptionError,
    PersonalMemoryStoreReason,
    PersonalMemoryStoreUnavailableError,
)
from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore


def _owner(
    *,
    user_id: str = "user-a",
    tenant_id: str | None = "tenant-a",
    namespace: str = "research/personal",
) -> PersonalMemoryOwner:
    return PersonalMemoryOwner(
        user_id=user_id,
        tenant_id=tenant_id,
        namespace=namespace,
    )


def _source(ref: str = "paper://paper-1") -> tuple[PersonalMemorySourceRef, ...]:
    return (
        PersonalMemorySourceRef(
            source_type="paper",
            source_ref=ref,
            source_revision="sha256:" + "b" * 64,
            locator="section:methods",
        ),
    )


def test_store_persists_owner_scoped_records_history_and_idempotency(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal-memory.sqlite3"
    owner = _owner()
    service = PersonalMemoryService(SQLitePersonalMemoryStore(path))

    first = service.create_confirmed(
        owner,
        content="Prefer benchmark papers with released code.",
        category="reading_preference",
        source_refs=_source(),
        client_request_id="desktop-create-1",
    )
    duplicate = service.create_confirmed(
        owner,
        content="Prefer benchmark papers with released code.",
        category="reading_preference",
        source_refs=_source(),
        client_request_id="desktop-create-1",
    )
    assert duplicate == first

    with pytest.raises(PersonalMemoryStoreConflictError) as conflict:
        service.create_confirmed(
            owner,
            content="Different content must not overwrite the request identity.",
            category="reading_preference",
            source_refs=_source(),
            client_request_id="desktop-create-1",
        )
    assert conflict.value.reason is PersonalMemoryStoreReason.IDEMPOTENCY_CONFLICT

    updated = service.update(
        owner,
        first.memory_id,
        expected_revision=1,
        content="Prefer benchmark papers with released code and data.",
    )
    assert updated.revision == 2
    service._store.close()  # type: ignore[attr-defined]

    reopened_store = SQLitePersonalMemoryStore(path)
    reopened_service = PersonalMemoryService(reopened_store)
    assert reopened_service.get(owner, first.memory_id) == updated
    reopened_store.close()

    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM personal_memory_revisions WHERE memory_id = ?",
                (first.memory_id,),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute("PRAGMA journal_mode").fetchone()[0].casefold() == "wal"
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_tenant_user_and_namespace_are_independent_isolation_dimensions(
    tmp_path: Path,
) -> None:
    store = SQLitePersonalMemoryStore(tmp_path / "personal-memory.sqlite3")
    service = PersonalMemoryService(store)
    owner = _owner()
    record = service.create_confirmed(
        owner,
        content="Focus on trustworthy retrieval systems.",
        category="research_interest",
        source_refs=_source(),
        client_request_id="isolation-1",
    )

    variants = (
        _owner(user_id="user-b"),
        _owner(tenant_id="tenant-b"),
        _owner(namespace="research/other"),
        _owner(tenant_id=None),
    )
    for foreign_owner in variants:
        assert store.get(foreign_owner, record.memory_id) is None
        records, total = store.list(
            foreign_owner,
            statuses=tuple(PersonalMemoryStatus),
            limit=10,
            offset=0,
        )
        assert records == ()
        assert total == 0
    store.close()


def test_disable_delete_reopen_and_index_rebuild_cannot_revive_memory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal-memory.sqlite3"
    owner = _owner()
    store = SQLitePersonalMemoryStore(path)
    service = PersonalMemoryService(store)
    disabled = service.create_confirmed(
        owner,
        content="Disabled unique-token preference.",
        category="background",
        source_refs=_source("user://manual/disabled"),
        client_request_id="lifecycle-disabled",
    )
    deleted = service.create_confirmed(
        owner,
        content="Deleted tombstone-token preference.",
        category="background",
        source_refs=_source("user://manual/deleted"),
        client_request_id="lifecycle-deleted",
    )
    disabled = service.disable(owner, disabled.memory_id, expected_revision=1)
    deleted = service.delete(owner, deleted.memory_id, expected_revision=1)

    assert service.recall(owner, query="unique-token", use_memory=True).records == ()
    assert service.recall(owner, query="tombstone-token", use_memory=True).records == ()
    assert service.rebuild_recall_index(owner) == 0
    store.close()

    reopened_store = SQLitePersonalMemoryStore(path)
    reopened_service = PersonalMemoryService(reopened_store)
    assert reopened_service.rebuild_recall_index(owner) == 0
    assert (
        reopened_service.recall(owner, query="unique-token", use_memory=True).records
        == ()
    )
    assert (
        reopened_service.recall(owner, query="tombstone-token", use_memory=True).records
        == ()
    )
    assert (
        reopened_service.get(owner, disabled.memory_id).status
        is PersonalMemoryStatus.DISABLED
    )
    tombstone = reopened_service.get(owner, deleted.memory_id)
    assert tombstone.status is PersonalMemoryStatus.DELETED
    assert tombstone.content == "Deleted tombstone-token preference."
    reopened_store.close()


def test_store_serializes_threaded_writers_without_lost_records(tmp_path: Path) -> None:
    store = SQLitePersonalMemoryStore(tmp_path / "personal-memory.sqlite3")
    service = PersonalMemoryService(store)
    owner = _owner()

    def create(index: int) -> str:
        return service.create_confirmed(
            owner,
            content=f"Concurrent memory {index}",
            category="background",
            source_refs=_source(f"user://thread/{index}"),
            client_request_id=f"thread-{index}",
        ).memory_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        memory_ids = tuple(executor.map(create, range(24)))

    assert len(set(memory_ids)) == 24
    page = service.list(owner, limit=100)
    assert page.total == 24
    store.close()


def test_path_validation_corruption_closed_store_and_write_failures_are_explicit(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="absolute"):
        SQLitePersonalMemoryStore(Path("relative.sqlite3"))
    missing_parent = tmp_path / "missing" / "memory.sqlite3"
    with pytest.raises(ValueError, match="existing directory"):
        SQLitePersonalMemoryStore(missing_parent)
    directory_target = tmp_path / "directory.sqlite3"
    directory_target.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        SQLitePersonalMemoryStore(directory_target)

    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(PersonalMemoryStoreCorruptionError):
        SQLitePersonalMemoryStore(corrupt)

    path = tmp_path / "personal-memory.sqlite3"
    store = SQLitePersonalMemoryStore(path)
    service = PersonalMemoryService(store)
    assert store._connection is not None  # type: ignore[attr-defined]
    store._connection.execute("PRAGMA query_only = ON")  # type: ignore[union-attr]
    with pytest.raises(PersonalMemoryStoreUnavailableError):
        service.create_confirmed(
            _owner(),
            content="This write must fail loudly.",
            category="background",
            source_refs=_source(),
            client_request_id="write-failure",
        )
    store._connection.execute("PRAGMA query_only = OFF")  # type: ignore[union-attr]
    store.close()
    with pytest.raises(PersonalMemoryStoreUnavailableError) as closed:
        store.get(_owner(), "pmem_" + "1" * 32)
    assert closed.value.reason is PersonalMemoryStoreReason.STORE_CLOSED


def test_symlinked_database_file_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.touch()
    link = tmp_path / "linked.sqlite3"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symlink|reparse"):
        SQLitePersonalMemoryStore(link)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction coverage")
def test_junctioned_database_parent_is_rejected_without_deleting_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "junction-target"
    target.mkdir()
    sentinel = target / "must-survive.txt"
    sentinel.write_text("target data", encoding="utf-8")
    junction = tmp_path / "junction-parent"
    database_path = junction / "personal-memory.sqlite3"

    environment = os.environ.copy()
    environment.update(
        {
            "NEWSROOM_TEST_JUNCTION": str(junction),
            "NEWSROOM_TEST_JUNCTION_TARGET": str(target),
        }
    )
    create = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$ErrorActionPreference = 'Stop'; "
            "New-Item -ItemType Junction "
            "-Path $env:NEWSROOM_TEST_JUNCTION "
            "-Target $env:NEWSROOM_TEST_JUNCTION_TARGET | Out-Null",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert create.returncode == 0, create.stderr
    assert junction.exists()

    try:
        with pytest.raises(ValueError, match="symlink|reparse"):
            SQLitePersonalMemoryStore(database_path)
        assert not (target / database_path.name).exists()
    finally:
        resolved_tmp = tmp_path.resolve(strict=True)
        junction.parent.resolve(strict=True).relative_to(resolved_tmp)
        target.resolve(strict=True).relative_to(resolved_tmp)
        remove = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference = 'Stop'; "
                "if (Test-Path -LiteralPath $env:NEWSROOM_TEST_JUNCTION) { "
                "[System.IO.Directory]::Delete($env:NEWSROOM_TEST_JUNCTION) }",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        assert remove.returncode == 0, remove.stderr

    assert not junction.exists()
    assert target.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "target data"


def test_owner_record_limit_is_enforced_in_the_create_transaction(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from backend.research.domain.personal_memory import (
        PersonalMemoryOrigin,
        PersonalMemoryRecord,
    )

    store = SQLitePersonalMemoryStore(tmp_path / "personal-memory.sqlite3")
    owner = _owner()
    now = datetime(2026, 10, 14, 8, 0, tzinfo=UTC)
    first = PersonalMemoryRecord(
        memory_id="pmem_" + "1" * 32,
        owner=owner,
        content="First bounded record",
        category="background",
        source_refs=_source("user://limit/1"),
        status="active",
        revision=1,
        created_at=now,
        updated_at=now,
        origin=PersonalMemoryOrigin.USER_CONFIRMED,
        confirmed_at=now,
    )
    second = PersonalMemoryRecord(
        memory_id="pmem_" + "2" * 32,
        owner=owner,
        content="Second bounded record",
        category="background",
        source_refs=_source("user://limit/2"),
        status="active",
        revision=1,
        created_at=now,
        updated_at=now,
        origin=PersonalMemoryOrigin.USER_CONFIRMED,
        confirmed_at=now,
    )
    store.create(
        first,
        client_request_id="limit-1",
        request_fingerprint="sha256:" + "1" * 64,
        max_records_per_owner=1,
    )

    with pytest.raises(PersonalMemoryStoreConflictError) as conflict:
        store.create(
            second,
            client_request_id="limit-2",
            request_fingerprint="sha256:" + "2" * 64,
            max_records_per_owner=1,
        )
    assert conflict.value.reason is PersonalMemoryStoreReason.OWNER_CAPACITY_EXCEEDED
    store.close()


def test_store_refuses_to_claim_an_unversioned_foreign_sqlite_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE foreign_data(value TEXT NOT NULL)")

    with pytest.raises(PersonalMemoryStoreCorruptionError):
        SQLitePersonalMemoryStore(path)


def test_remote_policy_is_owner_scoped_persistent_and_failed_cas_is_not_reported(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal-memory.sqlite3"
    owner = _owner()
    other = _owner(user_id="user-b")
    store = SQLitePersonalMemoryStore(path)
    service = PersonalMemoryService(store)

    owner_default = service.get_policy(owner)
    other_default = service.get_policy(other)
    assert owner_default.allow_remote_processing is False
    assert other_default.allow_remote_processing is False

    owner_enabled = service.update_policy(
        owner,
        allow_remote_processing=True,
        expected_revision=1,
    )
    assert owner_enabled.revision == 2
    assert service.get_policy(other).revision == 1

    assert store._connection is not None  # type: ignore[attr-defined]
    store._connection.execute("PRAGMA query_only = ON")  # type: ignore[union-attr]
    with pytest.raises(PersonalMemoryStoreUnavailableError):
        service.update_policy(
            other,
            allow_remote_processing=True,
            expected_revision=1,
        )
    store._connection.execute("PRAGMA query_only = OFF")  # type: ignore[union-attr]
    assert service.get_policy(other).allow_remote_processing is False
    assert service.get_policy(other).revision == 1
    store.close()

    reopened_store = SQLitePersonalMemoryStore(path)
    reopened_service = PersonalMemoryService(reopened_store)
    assert reopened_service.get_policy(owner).allow_remote_processing is True
    assert reopened_service.get_policy(owner).revision == 2
    assert reopened_service.get_policy(other).allow_remote_processing is False
    reopened_store.close()


def test_schema_contains_versioned_memory_policy_history_and_idempotency_tables(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal-memory.sqlite3"
    SQLitePersonalMemoryStore(path).close()

    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {
            "personal_memory_metadata",
            "personal_memory_policies",
            "personal_memories",
            "personal_memory_revisions",
            "personal_memory_create_requests",
        } <= tables
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.fixture
def long_memory_database(tmp_path: Path) -> Iterator[Path]:
    fixture_parent = resolve_storage_path(tmp_path)
    owned_name = f"personal-memory-long-path-{uuid4().hex}"
    owned_root = fixture_parent / owned_name
    directory = owned_root
    index = 0
    while len(os.fspath(directory)) <= 300:
        directory /= f"segment-{index:02d}-abcdefghijklmnopqrstuvwxyz"
        index += 1
    storage_io_path(directory).mkdir(parents=True)
    try:
        yield directory / "personal-memory # space 中文 %.sqlite3"
    finally:
        resolved = resolve_storage_path(owned_root)
        if resolved.parent != resolve_storage_path(fixture_parent) or resolved.name != owned_name:
            raise RuntimeError("refusing to remove an unowned personal memory test directory")
        shutil.rmtree(storage_io_path(resolved))


@pytest.mark.parametrize("path_type", [str, Path], ids=["str", "path"])
def test_public_logical_long_path_persists_records_policy_and_wal(
    long_memory_database: Path, path_type: type[str] | type[Path],
) -> None:
    database = long_memory_database
    owner = _owner()
    assert len(os.fspath(database)) > 260
    assert not str(database).startswith("\\\\?\\")
    with SQLitePersonalMemoryStore(path_type(database)) as store:
        assert store.database_path == database
        service = PersonalMemoryService(store)
        first = service.create_confirmed(
            owner, content="Persist real owner-scoped data on long paths.",
            category="reading_preference", source_refs=_source(), client_request_id="long-create",
        )
        updated = service.update(
            owner, first.memory_id, expected_revision=first.revision,
            content="Persist exact revisions on long paths.",
        )
        policy = service.get_policy(owner)
        policy = service.update_policy(owner, allow_remote_processing=True, expected_revision=policy.revision)
        policy = service.update_policy(owner, allow_remote_processing=False, expected_revision=policy.revision)
        assert (updated.revision, policy.revision) == (2, 3)
        for suffix in ("", "-wal", "-shm"):
            assert storage_io_path(Path(f"{database}{suffix}")).is_file()
        assert service.get_policy(owner).allow_remote_processing is False

    with SQLitePersonalMemoryStore(path_type(database)) as reopened:
        assert reopened.database_path == database
        service = PersonalMemoryService(reopened)
        assert service.get(owner, updated.memory_id) == updated
        assert service.get_policy(owner) == policy

    # Trusted internal callers may supply a namespace, but it must never leak
    # through the store's public identity. On non-Windows this is the same path.
    with SQLitePersonalMemoryStore(storage_io_path(database)) as internal:
        assert internal.database_path == database
        assert not str(internal.database_path).startswith("\\\\?\\")
        assert PersonalMemoryService(internal).get(owner, updated.memory_id) == updated


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_long_path_rejects_nonregular_sqlite_sidecar_without_creating_database(
    long_memory_database: Path, suffix: str,
) -> None:
    database = long_memory_database
    sidecar = storage_io_path(Path(f"{database}{suffix}"))
    sidecar.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        SQLitePersonalMemoryStore(database)
    assert not storage_io_path(database).exists()
    assert sidecar.is_dir()


def test_long_path_dangling_link_is_rejected_before_normalization_or_connect(
    long_memory_database: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import infrastructure.research.personal_memory_store as memory_module

    # Windows CI cannot create file symlinks without extra privilege. Exercise
    # the dangling-link metadata branch without bypassing real long parent I/O;
    # the existing junction test separately verifies a real reparse boundary.
    database = long_memory_database
    native_database = storage_io_path(database)
    original_lstat = Path.lstat

    def dangling_lstat(path: Path, *args, **kwargs):
        if path == native_database:
            return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_file_attributes=0)
        return original_lstat(path, *args, **kwargs)

    def unexpected_io(*args, **kwargs):
        raise AssertionError("linked input must be rejected before resolving or opening SQLite")

    monkeypatch.setattr(Path, "lstat", dangling_lstat)
    monkeypatch.setattr(memory_module, "resolve_storage_path", unexpected_io)
    monkeypatch.setattr(memory_module.sqlite3, "connect", unexpected_io)
    with pytest.raises(ValueError, match="symlink|reparse"):
        SQLitePersonalMemoryStore(database)
    assert not native_database.exists()
