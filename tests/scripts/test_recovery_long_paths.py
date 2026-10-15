from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import pytest

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.domain.personal_memory import (
    PersonalMemoryOwner,
    PersonalMemoryRecord,
    PersonalMemoryRemotePolicy,
    PersonalMemoryStatus,
)
from framework.shared.local_desktop import resolve_local_desktop_config
from framework.shared.windows_paths import resolve_storage_path, storage_io_path
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore
from scripts.local_desktop.common import DesktopOperationError, windows_extended_path
from scripts.local_desktop.recovery import (
    BackupManager,
    RestoreManager,
    StorageInventory,
    StorageOwner,
    _file_digest,
    _sqlite_file_set_signature,
    _sqlite_snapshot,
    _validate_sqlite,
)


class _Control:
    def __init__(self) -> None:
        self.admission_open = True
        self.rotations: list[str] = []

    def status(self) -> dict[str, bool]:
        return {"admission_open": self.admission_open}

    def begin_drain(self, *, timeout_seconds: float) -> dict[str, int | float]:
        self.admission_open = False
        return {"active_count": 0, "timeout_seconds": timeout_seconds}

    def resume_admission(self) -> dict[str, bool]:
        self.admission_open = True
        return {"admission_open": True}

    def rotate_generation(self, *, reason: str) -> dict[str, str]:
        self.rotations.append(reason)
        return {"generation": "gen_" + "1" * 32, "reason": reason}


class _ReleaseManager:
    def active(self):
        raise DesktopOperationError("none", "no active release in isolated test")


class _InventoryResolver:
    def __init__(self, inventory: StorageInventory) -> None:
        self.inventory = inventory
        self.environ: dict[str, str] = {}

    def resolve(self, *, require_existing: bool = True) -> StorageInventory:
        return self.inventory


@pytest.fixture
def long_recovery_root(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "recovery-long-path-owned"
    index = 0
    while len(os.fspath(root / "runtime" / "desktop-backup-placeholder" / "auth_users" / "value")) <= 310:
        root /= f"segment-{index:02d}-abcdefghijklmnopqrstuvwxyz"
        index += 1
    windows_extended_path(root).mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(windows_extended_path(tmp_path / "recovery-long-path-owned"))


def test_backup_restore_long_path_full_chain_keeps_public_paths_logical(
    long_recovery_root: Path,
    tmp_path: Path,
) -> None:
    source = long_recovery_root / "source" / "auth" / "users.json"
    windows_extended_path(source.parent).mkdir(parents=True)
    source_payload = {
        "schemaVersion": "newsroom_auth_users.v2",
        "users": [
            {
                "userId": "long-path-user",
                "username": "long-path-user",
                "passwordHash": "hash",
                "passwordSalt": "salt",
                "roles": [],
                "createdAt": "2026-10-15T00:00:00+00:00",
                "updatedAt": "2026-10-15T00:00:00+00:00",
            }
        ],
    }
    encoded = json.dumps(source_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    windows_extended_path(source).write_bytes(encoded)
    source_before = windows_extended_path(source).read_bytes()
    inventory = StorageInventory(
        (
            StorageOwner(
                "auth_users",
                source,
                "file",
                "newsroom_auth_users.v2",
                True,
                True,
                "json_lock",
            ),
        )
    )
    short_config = resolve_local_desktop_config(project_root=Path.cwd())
    config = replace(
        short_config,
        runtime_root=long_recovery_root / "runtime",
        release_root=long_recovery_root / "releases",
        backup_root=long_recovery_root / "backups",
        control_path=long_recovery_root / "control" / "control.json",
    )
    resolver = _InventoryResolver(inventory)
    control = _Control()
    backup = long_recovery_root / "archives" / "backup # 中文 %.zip"

    backup_result = BackupManager(
        config,
        control_plane=control,
        inventory_resolver=resolver,
        release_manager=_ReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).create(destination=backup)

    assert len(os.fspath(source)) > 260
    assert len(os.fspath(backup)) > 260
    assert backup_result.details["backup_path"] == str(backup)
    assert "\\\\?\\" not in json.dumps(backup_result.to_dict(), ensure_ascii=False)
    assert windows_extended_path(source).read_bytes() == source_before
    assert windows_extended_path(backup).is_file()
    with zipfile.ZipFile(windows_extended_path(backup)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        archived = archive.read("data/auth_users/value")
    assert archived == source_before
    assert manifest["files"] == [
        {
            "file_type": "file",
            "owner_id": "auth_users",
            "path": "value",
            "sha256": sha256(source_before).hexdigest(),
            "size": len(source_before),
        }
    ]
    assert manifest["inventory"]["owners"][0]["source_path"] == str(source)
    assert "\\\\?\\" not in json.dumps(manifest, ensure_ascii=False)

    restore_root = long_recovery_root / "isolated restore # 中文 %"
    restore_result = RestoreManager(
        config,
        control_plane=control,
        inventory_resolver=resolver,
        release_manager=_ReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).restore(backup, target_root=restore_root)

    assert restore_result.status == "VERIFIED"
    assert restore_result.details["restore_root"] == str(restore_root)
    report_path = restore_root / "restore-report.json"
    report = json.loads(windows_extended_path(report_path).read_text(encoding="utf-8"))
    restored = Path(report["owner_paths"]["auth_users"])
    assert windows_extended_path(restored).read_bytes() == source_before
    assert report["validation"]["owners"]["auth_users"]["application_validation"] == {
        "configured_root": str(restored.resolve(strict=False)),
        "record_count": 1,
        "store": "LocalJsonAuthUserRepository",
    }
    assert report["application_isolation"]["verified"] is True
    assert "\\\\?\\" not in json.dumps(report, ensure_ascii=False)
    assert windows_extended_path(source).read_bytes() == source_before


def test_sqlite_snapshot_digest_uri_validation_and_source_audit_support_long_paths(
    long_recovery_root: Path,
) -> None:
    source = long_recovery_root / "sqlite source # 中文 %" / "source.sqlite3"
    destination = long_recovery_root / "sqlite snapshot # 中文 %" / "snapshot.sqlite3"
    windows_extended_path(source.parent).mkdir(parents=True)
    windows_extended_path(destination.parent).mkdir(parents=True)
    with closing(sqlite3.connect(windows_extended_path(source))) as connection:
        connection.execute("CREATE TABLE proof(value TEXT NOT NULL)")
        connection.execute("INSERT INTO proof VALUES ('durable')")
        connection.commit()

    source_before = windows_extended_path(source).read_bytes()
    signature_before = _sqlite_file_set_signature(source)
    digest_before = _file_digest(source)

    _sqlite_snapshot(source, destination)
    _validate_sqlite(destination, required_tables=("proof",))

    assert len(os.fspath(source)) > 260
    assert len(os.fspath(destination)) > 260
    assert windows_extended_path(source).read_bytes() == source_before
    assert _sqlite_file_set_signature(source) == signature_before
    assert _file_digest(source) == digest_before
    with closing(sqlite3.connect(windows_extended_path(destination))) as connection:
        assert connection.execute("SELECT value FROM proof").fetchall() == [("durable",)]
    assert not tuple(windows_extended_path(destination.parent).glob("sqlite-source-*"))


@pytest.fixture
def long_wal_manager_root(tmp_path: Path) -> Iterator[Path]:
    """Own a unique subtree; do not shorten the real manager/app-store roots."""
    fixture_parent = resolve_storage_path(tmp_path)
    owned_name = f"recovery-wal-manager-{uuid4().hex}"
    owned_root = fixture_parent / owned_name
    root = owned_root
    index = 0
    while len(os.fspath(root)) <= 300:
        root /= f"segment-{index:02d}-abcdefghijklmnopqrstuvwxyz"
        index += 1
    storage_io_path(root).mkdir(parents=True)
    try:
        yield root
    finally:
        resolved_parent = resolve_storage_path(fixture_parent)
        resolved_owned = resolve_storage_path(owned_root)
        if resolved_owned.parent != resolved_parent or resolved_owned.name != owned_name:
            raise RuntimeError("refusing to remove an unowned WAL manager fixture")
        shutil.rmtree(storage_io_path(resolved_owned))


def _wal_owner_state(database: Path) -> dict[str, bytes | None]:
    return {
        suffix: native.read_bytes() if native.exists() else None
        for suffix in ("", "-wal", "-shm")
        for native in (storage_io_path(Path(f"{database}{suffix}")),)
    }


def _wal_state_receipt(state: dict[str, bytes | None]) -> dict[str, dict[str, str | int | bool | None]]:
    return {
        suffix: {
            "exists": payload is not None,
            "size": len(payload) if payload is not None else None,
            "sha256": sha256(payload).hexdigest() if payload is not None else None,
        }
        for suffix, payload in state.items()
    }


def _seed_wal_memories(
    service: PersonalMemoryService, owner: PersonalMemoryOwner,
) -> tuple[dict[str, PersonalMemoryRecord], PersonalMemoryRemotePolicy]:
    candidate = service.create_candidate(
        owner, content="Prefer bounded local evidence.", category="reading_preference",
        source_refs=[{"source_type": "user_input", "source_ref": "test:wal-candidate"}],
        client_request_id="wal-candidate",
    )
    active = service.confirm(owner, candidate.memory_id, expected_revision=candidate.revision)
    active = service.update(
        owner, active.memory_id, expected_revision=active.revision,
        content="Prefer bounded local evidence and preserve revisions.",
    )
    disabled = service.create_confirmed(
        owner, content="Keep disabled memory disabled.", category="research_interest",
        source_refs=[{"source_type": "user_input", "source_ref": "test:wal-disabled"}],
        client_request_id="wal-disabled",
    )
    disabled = service.disable(owner, disabled.memory_id, expected_revision=disabled.revision)
    deleted = service.create_confirmed(
        owner, content="Keep deletion tombstone and its exact revision.", category="background",
        source_refs=[{"source_type": "user_input", "source_ref": "test:wal-deleted"}],
        client_request_id="wal-deleted",
    )
    deleted = service.delete(owner, deleted.memory_id, expected_revision=deleted.revision)
    policy = service.get_policy(owner)
    policy = service.update_policy(owner, allow_remote_processing=True, expected_revision=policy.revision)
    policy = service.update_policy(owner, allow_remote_processing=False, expected_revision=policy.revision)
    assert (active.revision, disabled.revision, deleted.revision, policy.revision) == (3, 2, 2, 3)
    assert policy.allow_remote_processing is False
    return {record.memory_id: record for record in (active, disabled, deleted)}, policy


def _assert_logical_memory_records(
    database: Path, owner: PersonalMemoryOwner,
    expected_records: dict[str, PersonalMemoryRecord], expected_policy: PersonalMemoryRemotePolicy,
) -> None:
    # Pass the public logical long path itself, never an I/O-namespaced substitute.
    assert len(os.fspath(database)) > 260
    assert not str(database).startswith("\\\\?\\")
    with SQLitePersonalMemoryStore(database) as store:
        assert store.database_path == database
        service = PersonalMemoryService(store)
        page = service.list(owner, statuses=tuple(PersonalMemoryStatus), limit=100)
        assert page.total == len(expected_records)
        assert {record.memory_id: record for record in page.records} == expected_records
        for memory_id, expected_record in expected_records.items():
            assert service.get(owner, memory_id) == expected_record
        assert service.get_policy(owner) == expected_policy
        assert service.get_policy(owner).allow_remote_processing is False


@pytest.mark.parametrize("wal_mode", ["active", "stopped-with-shm", "stopped-without-shm"])
def test_personal_memory_long_path_wal_manager_round_trip_preserves_source_and_generation(
    long_wal_manager_root: Path, wal_mode: str,
) -> None:
    root = long_wal_manager_root
    source = root / "source # space 中文 %" / "personal-memory.sqlite3"
    assert len(os.fspath(source)) > 260
    assert not str(source).startswith("\\\\?\\")
    storage_io_path(source.parent).mkdir(parents=True)
    owner = PersonalMemoryOwner(user_id="wal-manager-reader", namespace="research:wal-manager")
    store = SQLitePersonalMemoryStore(source)
    try:
        assert store.database_path == source
        records, policy = _seed_wal_memories(PersonalMemoryService(store), owner)
        live_state = _wal_owner_state(source)
        assert all(live_state[suffix] is not None for suffix in ("", "-wal", "-shm"))
        live_wal = live_state["-wal"]
        assert live_wal is not None and len(live_wal) > 32
        if wal_mode != "active":
            # Materialize an owned stopped-image shadow from the quiescent live
            # main/WAL/SHM set. Never reopen that source: recovery must use a private copy.
            shadow = root / "stopped-owned-shadow"
            storage_io_path(shadow).mkdir()
            for suffix in ("", "-wal", "-shm"):
                shutil.copy2(storage_io_path(Path(f"{source}{suffix}")), storage_io_path(shadow / f"value{suffix}"))
            store.close()
            for suffix in ("", "-wal", "-shm"):
                destination = storage_io_path(Path(f"{source}{suffix}"))
                if suffix == "-shm" and wal_mode == "stopped-without-shm":
                    destination.unlink(missing_ok=True)
                else:
                    os.replace(storage_io_path(shadow / f"value{suffix}"), destination)

        before = _wal_owner_state(source)
        signature_before = _sqlite_file_set_signature(source)
        assert before[""] is not None and before["-wal"] is not None
        assert (before["-shm"] is not None) is (wal_mode != "stopped-without-shm")
        assert before[""] == live_state[""] and before["-wal"] == live_state["-wal"]
        if before["-shm"] is not None:
            assert before["-shm"] == live_state["-shm"]

        config = replace(
            resolve_local_desktop_config(project_root=Path.cwd()),
            runtime_root=root / "runtime", release_root=root / "releases",
            backup_root=root / "backups", control_path=root / "control" / "control.json",
        )
        storage_owner = StorageOwner(
            "research_personal_memory", source, "file", "newsroom.personal-memory-store/v1",
            True, True, "sqlite",
        )
        resolver = _InventoryResolver(StorageInventory((storage_owner,)))
        control = _Control()
        backup = root / "archives # reserved %" / "wal-owner.zip"
        backed_up = BackupManager(
            config, control_plane=control, inventory_resolver=resolver,
            release_manager=_ReleaseManager(), lifecycle_status=lambda: "STOPPED",
        ).create(destination=backup)
        assert backed_up.status == "COMPLETE"
        assert _wal_owner_state(source) == before
        assert _sqlite_file_set_signature(source) == signature_before
        with zipfile.ZipFile(storage_io_path(backup)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            archived = archive.read("data/research_personal_memory/value")
            assert set(archive.namelist()) == {"manifest.json", "data/research_personal_memory/value"}
        assert archived != before[""], "snapshot must include uncheckpointed WAL records"
        assert manifest["files"] == [{
            "file_type": "file", "owner_id": "research_personal_memory", "path": "value",
            "sha256": sha256(archived).hexdigest(), "size": len(archived),
        }]
        assert manifest["inventory"]["owners"][0]["source_path"] == str(source)
        assert manifest["sessions_restore_policy"] == "invalidate_all"
        assert manifest["request_restore_policy"] == "rotate_generation_and_mark_nonterminal_needs_review"
        archive_audit = root / "archive-audit" / "personal-memory.sqlite3"
        storage_io_path(archive_audit.parent).mkdir()
        storage_io_path(archive_audit).write_bytes(archived)
        _assert_logical_memory_records(archive_audit, owner, records, policy)

        manager = RestoreManager(
            config, control_plane=control, inventory_resolver=resolver,
            release_manager=_ReleaseManager(), lifecycle_status=lambda: "STOPPED",
        )
        restore_root = root / "isolated restore # space 中文 %"
        restored = manager.restore(backup, target_root=restore_root)
        assert len(os.fspath(source)) > 260 and len(os.fspath(restore_root)) > 260
        assert restored.status == "VERIFIED" and restored.details["restore_root"] == str(restore_root)
        report = json.loads(storage_io_path(restore_root / "restore-report.json").read_text(encoding="utf-8"))
        restored_database = Path(report["owner_paths"]["research_personal_memory"])
        receipt = report["validation"]["owners"]["research_personal_memory"]["application_validation"]
        assert receipt == {
            "store": "SQLitePersonalMemoryStore", "configured_root": str(restored_database),
            "application_id": 0x504D454D, "schema_version": 1, "record_count": 3, "policy_count": 1,
        }
        assert report["application_isolation"]["verified"] is True
        preview = restored.details["personal_memory_restore"]
        assert preview["status_counts"] == {"active": 1, "disabled": 1, "deleted": 1}
        assert preview["requires_explicit_activation_confirmation"] is True
        _assert_logical_memory_records(restored_database, owner, records, policy)
        assert control.rotations == []
        assert _wal_owner_state(source) == before
        assert _sqlite_file_set_signature(source) == signature_before

        # Activate only into a second owned shadow owner, never over the audited
        # source. The real activation contract must rotate once after confirmation.
        activation_destination = root / "shadow activation # reserved %" / "personal-memory.sqlite3"
        activation_control = _Control()
        activation_manager = RestoreManager(
            config, control_plane=activation_control,
            inventory_resolver=_InventoryResolver(StorageInventory((replace(storage_owner, path=activation_destination),))),
            release_manager=_ReleaseManager(), lifecycle_status=lambda: "STOPPED",
        )
        with pytest.raises(DesktopOperationError) as raised:
            activation_manager.restore(backup, target_root=root / "confirmation-required", activate=True)
        assert raised.value.reason_code == "desktop_restore_personal_memory_confirmation_required"
        assert activation_control.rotations == []
        assert not storage_io_path(activation_destination).exists()
        assert _wal_owner_state(source) == before
        assert _sqlite_file_set_signature(source) == signature_before
        activated = activation_manager.restore(
            backup, target_root=root / "confirmed-activation", activate=True,
            personal_memory_confirmation=preview["confirmation_token"],
        )
        assert activated.status == "ACTIVATED"
        assert activated.details["activation"]["generation"] == "gen_" + "1" * 32
        assert len(activation_control.rotations) == 1
        assert activation_control.rotations[0].startswith("restore:")
        assert activation_control.admission_open is True
        _assert_logical_memory_records(activation_destination, owner, records, policy)
        assert _wal_owner_state(source) == before
        assert _sqlite_file_set_signature(source) == signature_before
        for public_value in (backed_up.to_dict(), manifest, restored.to_dict(), report, activated.to_dict()):
            assert "\\\\?\\" not in json.dumps(public_value, ensure_ascii=False)
        print(json.dumps({
            "case": wal_mode, "source": str(source), "source_length": len(os.fspath(source)),
            "restore_root": str(restore_root), "restore_root_length": len(os.fspath(restore_root)),
            "sidecars_before": _wal_state_receipt(before),
            "sidecars_after": _wal_state_receipt(_wal_owner_state(source)),
            "source_signature_unchanged": _sqlite_file_set_signature(source) == signature_before,
            "archive_sha256": sha256(archived).hexdigest(),
            "record_revisions": sorted(record.revision for record in records.values()),
            "policy_revision": policy.revision, "allow_remote_processing": policy.allow_remote_processing,
            "public_paths_logical": True,
            "confirmation_gate_blocked": True,
            "generation_rotations": len(activation_control.rotations),
            "source_audit_stages": ["backup", "preview", "blocked_activation", "shadow_activation"],
            "restored_generation": activated.details["activation"]["generation"],
        }, ensure_ascii=True, sort_keys=True))
    finally:
        store.close()
