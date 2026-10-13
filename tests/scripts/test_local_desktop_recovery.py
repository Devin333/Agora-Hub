from __future__ import annotations

import gc
import json
import os
import sqlite3
import zipfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError
from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore
from scripts.local_desktop.release import DATA_SCHEMAS
from scripts.local_desktop.recovery import (
    BACKUP_SCHEMA,
    INVENTORY_SCHEMA,
    BackupManager,
    RestoreManager,
    StorageInventory,
    StorageInventoryResolver,
    StorageOwner,
    _activation_ownership_marker,
    _incompatible_schemas,
    _validate_restored_owners,
)


class FakeReleaseManager:
    def __init__(self) -> None:
        self.active_pointer = Path("missing")

    def active(self):
        raise DesktopOperationError("none", "none")


class FakeControl:
    def __init__(self, *, active_count: int = 0, fail_drain: bool = False) -> None:
        self.rotations: list[str] = []
        self.events: list[str] = []
        self.active_count = active_count
        self.fail_drain = fail_drain
        self.admission_open = True

    def status(self):
        return {"admission_open": self.admission_open}

    def begin_drain(self, *, timeout_seconds: float):
        self.events.append("begin_drain")
        self.admission_open = False
        if self.fail_drain:
            raise DesktopOperationError("desktop_drain_failed", "drain failed")
        return {"active_count": self.active_count, "timeout_seconds": timeout_seconds}

    def resume_admission(self):
        self.events.append("resume_admission")
        self.admission_open = True
        return {"admission_open": True}

    def rotate_generation(self, *, reason: str):
        self.rotations.append(reason)
        return {"generation": "gen_" + "1" * 32}


def _config(tmp_path: Path):
    return resolve_local_desktop_config(
        project_root=tmp_path,
        cli_overrides={
            "runtime_root": str(tmp_path / "runtime"),
            "release_root": str(tmp_path / "releases"),
            "backup_root": str(tmp_path / "backups"),
            "control_path": str(tmp_path / "data" / "control.json"),
        },
    )


def _owner_files(tmp_path: Path) -> tuple[StorageInventory, dict[str, Path]]:
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "auth_users": data / "users.json",
        "auth_sessions": data / "sessions.json",
        "research_history": data / "history.json",
        "research_imports": data / "imports",
        "runtime_artifacts": data / "runs",
        "desktop_control": data / "control.json",
    }
    paths["auth_users"].write_text('{"schemaVersion":"newsroom_auth_users.v2","users":[]}', encoding="utf-8")
    paths["auth_sessions"].write_text('{"schemaVersion":"newsroom_auth_sessions.v1","sessions":[]}', encoding="utf-8")
    paths["research_history"].write_text('{"schemaVersion":"newsroom_research_history.v1","users":{}}', encoding="utf-8")
    paths["research_imports"].mkdir()
    paths["runtime_artifacts"].mkdir()
    database = paths["runtime_artifacts"] / "events.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE events(id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO events(value) VALUES ('durable')")
        connection.commit()
    finally:
        connection.close()
    paths["desktop_control"].write_text('{"schema":"newsroom.research-desktop-control/v1","generation":"gen_00000000000000000000000000000000","operations":{},"reports":{}}', encoding="utf-8")
    inventory = StorageInventory(
        (
            StorageOwner("auth_users", paths["auth_users"], "file", "newsroom_auth_users.v2", True, True, "json_lock"),
            StorageOwner("auth_sessions", paths["auth_sessions"], "file", "newsroom_auth_sessions.v1", False, True, "json_lock"),
            StorageOwner("research_history", paths["research_history"], "file", "newsroom_research_history.v1", True, True, "json_lock"),
            StorageOwner("research_imports", paths["research_imports"], "directory", "newsroom.research-import/v1", True, True, "quiescent_tree"),
            StorageOwner("runtime_artifacts", paths["runtime_artifacts"], "directory", "newsroom.runtime-artifacts/v1", True, True, "mixed_tree"),
            StorageOwner("desktop_control", paths["desktop_control"], "file", "newsroom.research-desktop-control/v1", True, True, "json_lock"),
        )
    )
    return inventory, paths


class StaticInventory:
    def __init__(self, inventory: StorageInventory) -> None:
        self.inventory = inventory
        self.environ = {}

    def resolve(self, *, require_existing: bool = True) -> StorageInventory:
        return self.inventory


def test_inventory_rejects_external_database_backend(tmp_path: Path) -> None:
    config = _config(tmp_path)
    resolver = StorageInventoryResolver(config, environ={"NEWS_DATABASE_DSN": "postgresql://example.invalid/news"})

    with pytest.raises(DesktopOperationError) as raised:
        resolver.resolve(require_existing=False)
    assert raised.value.reason_code == "desktop_inventory_external_database_unsupported"


def test_inventory_resolves_custom_owner_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    custom = tmp_path / "custom" / "history.json"
    resolver = StorageInventoryResolver(config, environ={"NEWSROOM_RESEARCH_HISTORY_PATH": str(custom)})

    inventory = resolver.resolve(require_existing=False)

    history = next(item for item in inventory.owners if item.owner_id == "research_history")
    assert history.path == custom.resolve()
    assert any(item.owner_id == "desktop_control" and item.path == config.control_path for item in inventory.owners)
    assert next(item for item in inventory.owners if item.owner_id == "research_run_store").schema == DATA_SCHEMAS["research_run_store"]
    assert next(item for item in inventory.owners if item.owner_id == "research_rag_local").path == (tmp_path / ".newsroom" / "research" / "chunks").resolve()
    transcripts = next(item for item in inventory.owners if item.owner_id == "research_rag_transcripts")
    assert transcripts.path == (tmp_path / ".newsroom" / "research" / "rag" / "transcripts").resolve()
    assert transcripts.schema == "newsroom.paper_rag_transcript.v2"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_private_acl_removes_existing_everyone_access_and_preserves_owner_io(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    from scripts.local_desktop.common import _windows_current_user_sid, protect_private_path

    target = tmp_path / "private.json"
    target.write_text("before", encoding="utf-8")
    owner_sid = win32security.ConvertStringSidToSid(_windows_current_user_sid())
    everyone_sid = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
    permissive = win32security.ACL()
    permissive.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, 0, ntsecuritycon.FILE_ALL_ACCESS, owner_sid)
    permissive.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, 0, ntsecuritycon.FILE_GENERIC_READ, everyone_sid)
    win32security.SetNamedSecurityInfo(
        str(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        permissive,
        None,
    )

    protect_private_path(target)

    descriptor = win32security.GetNamedSecurityInfo(
        str(target),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    dacl = descriptor.GetSecurityDescriptorDacl()
    assert dacl is not None
    assert dacl.GetAceCount() == 1
    ace = dacl.GetAce(0)
    assert ace[2] == owner_sid
    assert ace[1] & ntsecuritycon.FILE_ALL_ACCESS == ntsecuritycon.FILE_ALL_ACCESS
    target.write_text("after", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_backup_output_does_not_change_parent_or_sibling_acl_and_archive_is_private(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    from scripts.local_desktop.common import _windows_current_user_sid

    def descriptor(path: Path) -> str:
        security = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )
        return win32security.ConvertSecurityDescriptorToStringSecurityDescriptor(
            security,
            win32security.SDDL_REVISION_1,
            win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
        )

    def set_permissive(path: Path) -> None:
        owner = win32security.ConvertStringSidToSid(_windows_current_user_sid())
        everyone = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
        acl = win32security.ACL()
        inheritance = (
            win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
            if path.is_dir()
            else 0
        )
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, ntsecuritycon.FILE_ALL_ACCESS, owner)
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, ntsecuritycon.FILE_GENERIC_READ, everyone)
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )

    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    output = tmp_path / "user-output"
    output.mkdir()
    sibling = output / "unrelated.txt"
    sibling.write_text("unrelated", encoding="utf-8")
    set_permissive(output)
    set_permissive(sibling)
    parent_before = descriptor(output)
    sibling_before = descriptor(sibling)
    backup = output / "chosen-backup.zip"

    BackupManager(
        config,
        control_plane=FakeControl(),
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).create(destination=backup)

    assert descriptor(output) == parent_before
    assert descriptor(sibling) == sibling_before
    backup_security = win32security.GetNamedSecurityInfo(
        str(backup),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    backup_acl = backup_security.GetSecurityDescriptorDacl()
    owner_sid = win32security.ConvertStringSidToSid(_windows_current_user_sid())
    assert backup_acl is not None and backup_acl.GetAceCount() == 1
    assert backup_acl.GetAce(0)[2] == owner_sid


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
def test_backup_snapshots_and_restore_staging_are_private_before_sensitive_writes_complete(tmp_path: Path) -> None:
    import ntsecuritycon
    import win32security

    from scripts.local_desktop.common import _windows_current_user_sid

    owner_sid = win32security.ConvertStringSidToSid(_windows_current_user_sid())

    def assert_owner_only(path: Path) -> None:
        security = win32security.GetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        acl = security.GetSecurityDescriptorDacl()
        assert acl is not None and acl.GetAceCount() == 1
        ace = acl.GetAce(0)
        assert ace[2] == owner_sid
        assert ace[1] & ntsecuritycon.FILE_ALL_ACCESS == ntsecuritycon.FILE_ALL_ACCESS

    class InspectingBackupManager(BackupManager):
        inspected: list[Path]

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.inspected = []

        def _snapshot_owner(self, owner: StorageOwner, destination: Path) -> None:
            assert_owner_only(destination.parent)
            super()._snapshot_owner(owner, destination)
            for item in (destination, *destination.rglob("*")):
                assert_owner_only(item)
                self.inspected.append(item)

    class InspectingRestoreManager(RestoreManager):
        inspected: list[Path]

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.inspected = []

        def _extract_and_validate(self, source: Path, target: Path, *, schema_compatibility=None):
            assert_owner_only(target)
            result = super()._extract_and_validate(source, target, schema_compatibility=schema_compatibility)
            for item in target.rglob("*"):
                assert_owner_only(item)
                self.inspected.append(item)
            return result

    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    backup = tmp_path / "private-staging.zip"
    backup_manager = InspectingBackupManager(
        config,
        control_plane=FakeControl(),
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    )
    backup_manager.create(destination=backup)
    assert backup_manager.inspected

    restore_manager = InspectingRestoreManager(
        config,
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
        validator=lambda _inventory, _paths: {"application_isolation": {"verified": True}},
    )
    restore_manager.restore(backup, target_root=tmp_path / "private-restore-staging")
    assert restore_manager.inspected


def test_owner_fixture_closes_sqlite_before_declaring_storage_quiescent(tmp_path: Path) -> None:
    _, paths = _owner_files(tmp_path)
    database = paths["runtime_artifacts"] / "events.sqlite3"
    original = database.read_bytes()

    assert not database.with_name(f"{database.name}-wal").exists()
    assert not database.with_name(f"{database.name}-shm").exists()
    gc.collect()
    assert database.read_bytes() == original


def test_backup_uses_native_sqlite_snapshot_including_wal_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    database = paths["runtime_artifacts"] / "events.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO events(value) VALUES ('wal-durable')")
        writer.commit()
        assert database.with_name(f"{database.name}-wal").exists()

        target = tmp_path / "backup.zip"
        result = BackupManager(
            config,
            control_plane=FakeControl(),
            inventory_resolver=StaticInventory(inventory),
            release_manager=FakeReleaseManager(),
            lifecycle_status=lambda: "STOPPED",
        ).create(destination=target)
    finally:
        writer.close()

    assert result.status == "COMPLETE"
    with zipfile.ZipFile(target) as archive:
        restored = tmp_path / "snapshot.sqlite3"
        restored.write_bytes(archive.read("data/runtime_artifacts/events.sqlite3"))
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT value FROM events ORDER BY id").fetchall() == [("durable",), ("wal-durable",)]


def test_restore_rejects_path_escape_before_writing_outside_target(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    archive_path = tmp_path / "malicious.zip"
    content = b"{}"
    manifest = {
        "schema": BACKUP_SCHEMA,
        "backup_id": "bad",
        "inventory": {"schema": INVENTORY_SCHEMA, "owners": [item.to_dict() for item in inventory.owners]},
        "files": [{"owner_id": "auth_users", "path": "../escape.json", "size": len(content), "sha256": sha256(content).hexdigest()}],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data/auth_users/../escape.json", content)

    manager = RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED")
    with pytest.raises(DesktopOperationError) as raised:
        manager.restore(archive_path, target_root=tmp_path / "restore")

    assert raised.value.reason_code == "desktop_restore_path_escape"
    assert not (tmp_path / "restore" / "escape.json").exists()


def test_restore_rejects_target_overlapping_original_storage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    archive_path = tmp_path / "backup.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=archive_path)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(archive_path, target_root=paths["runtime_artifacts"] / "restore")
    assert raised.value.reason_code == "desktop_restore_target_overlaps_storage"


def test_restore_rejects_windows_ads_archive_path(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    archive_path = tmp_path / "ads.zip"
    content = b"{}"
    manifest = {
        "schema": BACKUP_SCHEMA,
        "backup_id": "bad-ads",
        "inventory": {"schema": INVENTORY_SCHEMA, "owners": [item.to_dict() for item in inventory.owners]},
        "files": [{"owner_id": "auth_users", "path": "value:stream", "size": len(content), "sha256": sha256(content).hexdigest()}],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("data/auth_users/value:stream", content)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(archive_path, target_root=tmp_path / "isolated-ads")
    assert raised.value.reason_code == "desktop_restore_path_escape"


def test_restore_maps_manifest_owners_to_current_trusted_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    archive_path = tmp_path / "backup.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=archive_path)
    foreign_root = tmp_path / "attacker"
    with zipfile.ZipFile(archive_path, "r") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        members = {item.filename: archive.read(item.filename) for item in archive.infolist() if item.filename != "manifest.json"}
    for owner in manifest["inventory"]["owners"]:
        owner["source_path"] = str(foreign_root / owner["owner_id"])
    rewritten = tmp_path / "rewritten.zip"
    with zipfile.ZipFile(rewritten, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, content in members.items():
            archive.writestr(name, content)

    result = RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(rewritten, target_root=tmp_path / "isolated")

    assert result.status == "VERIFIED"
    assert not foreign_root.exists()
    assert all(str(path) not in json.dumps(result.details) for path in paths.values())


def test_restore_activation_invalidates_sessions_and_rotates_generation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    archive_path = tmp_path / "backup.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=archive_path)
    control = FakeControl()

    result = RestoreManager(
        config,
        control_plane=control,
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).restore(archive_path, target_root=tmp_path / "restore", activate=True)

    assert result.status == "ACTIVATED"
    assert json.loads(paths["auth_sessions"].read_text(encoding="utf-8"))["sessions"] == []
    assert result.details["activation"]["generation"] == "gen_" + "1" * 32
    assert control.rotations and control.rotations[0].startswith("restore:")


def test_restore_rejects_absolute_reference_to_original_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    paths["research_history"].write_text(json.dumps({"schemaVersion": "newsroom_research_history.v1", "users": {}, "artifact_path": str(paths["runtime_artifacts"] / "secret.json")}), encoding="utf-8")
    archive_path = tmp_path / "backup.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=archive_path)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(archive_path, target_root=tmp_path / "restore")

    assert raised.value.reason_code == "desktop_restore_absolute_reference_unsupported"


def test_restore_relocates_owned_absolute_path_references(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    artifact = paths["runtime_artifacts"] / "owned.json"
    artifact.write_text("{}", encoding="utf-8")
    import_record = paths["research_imports"] / "import.json"
    import_record.write_text(json.dumps({"path": str(artifact)}), encoding="utf-8")
    archive_path = tmp_path / "backup.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=archive_path)

    result = RestoreManager(
        config,
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).restore(archive_path, target_root=tmp_path / "isolated")

    restored_import = Path(result.details["restore_root"]) / "owners" / "research_imports" / "import.json"
    relocated = Path(json.loads(restored_import.read_text(encoding="utf-8"))["path"])
    assert relocated == Path(result.details["restore_root"]) / "owners" / "runtime_artifacts" / "owned.json"
    assert relocated.is_file()


def test_release_schema_compatibility_uses_inventory_contract(tmp_path: Path) -> None:
    inventory, _ = _owner_files(tmp_path)
    readable = {owner.owner_id: owner.schema for owner in inventory.owners}

    assert _incompatible_schemas(inventory, readable) == {}

    readable["desktop_control"] = "newsroom.research-desktop-control/v0"
    assert _incompatible_schemas(inventory, readable) == {
        "desktop_control": {
            "current": "newsroom.research-desktop-control/v1",
            "supported": "newsroom.research-desktop-control/v0",
        }
    }


def _activation_journal(inventory: StorageInventory, transaction: str) -> dict[str, object]:
    operations = []
    for owner in inventory.owners:
        destination = owner.path
        operations.append(
            {
                "owner_id": owner.owner_id,
                "destination": str(destination),
                "prepared": None,
                "rollback": str(destination.parent / f".{destination.name}.rollback-{transaction}"),
                "failed": str(destination.parent / f".{destination.name}.failed-{transaction}"),
                "ownership_marker": _activation_ownership_marker(transaction, owner.owner_id, destination),
                "had_destination": True,
                "had_prepared": False,
                "prepared_ready": False,
                "state": "old_moved",
            }
        )
    return {
        "schema": "newsroom.local-desktop-activation-journal/v1",
        "transaction": transaction,
        "status": "activating",
        "operations": operations,
    }


def test_activation_journal_validates_every_operation_before_moving_data(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    transaction = "a" * 32
    journal = _activation_journal(inventory, transaction)
    operations = journal["operations"]
    assert isinstance(operations, list)
    operations[0]["rollback"] = str(paths["auth_users"].parent / ".users.json.rollback-wrong")
    destination = paths["desktop_control"]
    destination.write_text("new", encoding="utf-8")
    rollback = destination.parent / f".{destination.name}.rollback-{transaction}"
    rollback.write_text("old", encoding="utf-8")
    manager = RestoreManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED")

    with pytest.raises(DesktopOperationError) as raised:
        manager._recover_activation_journal(journal, config.runtime_root / "restore-activation.json")

    assert raised.value.reason_code == "desktop_activation_journal_invalid"
    assert destination.read_text(encoding="utf-8") == "new"
    assert rollback.read_text(encoding="utf-8") == "old"


def test_activation_recovery_uses_real_paths_not_stale_state_or_restore_root(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    transaction = "b" * 32
    journal = _activation_journal(inventory, transaction)
    journal["restore_root"] = str(tmp_path / "untrusted-journal-target")
    operations = journal["operations"]
    assert isinstance(operations, list)
    operation = next(item for item in operations if item["owner_id"] == "desktop_control")
    operation["state"] = "old_moved"
    destination = paths["desktop_control"]
    destination.write_text("new", encoding="utf-8")
    rollback = Path(operation["rollback"])
    rollback.write_text("old", encoding="utf-8")
    control = FakeControl()
    manager = RestoreManager(config, control_plane=control, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED")

    manager._recover_activation_journal(journal, config.runtime_root / "restore-activation.json")

    failed = Path(operation["failed"])
    assert destination.read_text(encoding="utf-8") == "old"
    assert failed.read_text(encoding="utf-8") == "new"
    assert not (tmp_path / "untrusted-journal-target").exists()
    assert control.rotations == [f"restore-recovery:{transaction}"]


class RejectingRollbackReleaseManager:
    def __init__(self, readable_schemas: dict[str, str]) -> None:
        self.readable_schemas = readable_schemas
        self.activate_called = False

    def resolve_release(self, release: str | Path) -> Path:
        return Path(release)

    def verify(self, release: Path) -> dict[str, object]:
        return {"data_compatibility": {"readable_schemas": self.readable_schemas}}

    def activate(self, release: Path):
        self.activate_called = True
        return {"release_path": str(release)}


def test_rollback_rejects_incompatible_backup_before_release_activation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    backup = tmp_path / "current-schema.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)
    readable = {owner.owner_id: owner.schema for owner in inventory.owners}
    readable["desktop_control"] = "newsroom.research-desktop-control/v0"
    releases = RejectingRollbackReleaseManager(readable)
    manager = RestoreManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=releases, lifecycle_status=lambda: "STOPPED")

    with pytest.raises(DesktopOperationError) as raised:
        manager.rollback_release(tmp_path / "old-release", compatible_backup=backup)

    assert raised.value.reason_code == "desktop_backup_owner_incompatible"
    assert releases.activate_called is False


def test_restore_reads_rag_transcript_through_store_bound_to_isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    original_root = tmp_path / "original-transcripts"
    store = PaperRagTranscriptFileStore(original_root)
    actor_scope = {"tenant_id": "tenant-a", "user_id": "user-a", "memory_namespace": "research:user-a"}
    store.persist(
        {"transcript_id": "rag-transcript-1", "session_id": "session-1", "turns": [], "citations": [], "notes": []},
        actor_scope=actor_scope,
    )
    inventory = StorageInventory(
        (
            StorageOwner(
                "research_rag_transcripts",
                original_root,
                "directory",
                DATA_SCHEMAS["research_rag_transcripts"],
                True,
                True,
                "paper_rag_transcript_store",
            ),
        )
    )
    backup = tmp_path / "transcripts.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)
    original_load = PaperRagTranscriptFileStore.load
    observed_roots: list[Path] = []

    def guarded_load(self, transcript_id: str, *, actor_scope):
        observed_roots.append(self.root)
        if self.root == original_root.resolve():
            raise AssertionError("isolated restore validator accessed the original transcript root")
        return original_load(self, transcript_id, actor_scope=actor_scope)

    monkeypatch.setattr(PaperRagTranscriptFileStore, "load", guarded_load)
    result = RestoreManager(
        config,
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).restore(backup, target_root=tmp_path / "isolated-transcripts")

    isolation = result.details["validation"]["application_isolation"]
    assert result.status == "VERIFIED"
    assert isolation["verified"] is True
    assert isolation["validated_owner_ids"] == ["research_rag_transcripts"]
    assert observed_roots and all(root != original_root.resolve() for root in observed_roots)
    assert original_load(store, "rag-transcript-1", actor_scope=actor_scope)["transcript"]["notes"] == []



def test_backup_holds_application_drain_until_every_owner_snapshot_finishes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    control = FakeControl()

    class ObservingBackupManager(BackupManager):
        def _snapshot_owner(self, owner, destination):
            assert control.admission_open is False
            return super()._snapshot_owner(owner, destination)

    result = ObservingBackupManager(
        config,
        control_plane=control,
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).create(destination=tmp_path / "drained.zip")

    assert result.status == "COMPLETE"
    assert control.events == ["begin_drain", "resume_admission"]
    assert control.admission_open is True


def test_backup_restores_admission_when_drain_fails_after_closing_gate(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    control = FakeControl(fail_drain=True)

    with pytest.raises(DesktopOperationError) as raised:
        BackupManager(
            config,
            control_plane=control,
            inventory_resolver=StaticInventory(inventory),
            release_manager=FakeReleaseManager(),
            lifecycle_status=lambda: "STOPPED",
        ).create(destination=tmp_path / "failed.zip")

    assert raised.value.reason_code == "desktop_drain_failed"
    assert control.events == ["begin_drain", "resume_admission"]
    assert control.admission_open is True
    assert not (tmp_path / "failed.zip").exists()
    assert not list(config.runtime_root.glob("desktop-backup-*"))
    assert not list(tmp_path.glob(".failed.zip.*.tmp"))


def test_backup_rejects_active_application_writer_even_when_instance_record_is_stopped(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, _ = _owner_files(tmp_path)
    control = FakeControl(active_count=1)

    with pytest.raises(DesktopOperationError) as raised:
        BackupManager(
            config,
            control_plane=control,
            inventory_resolver=StaticInventory(inventory),
            release_manager=FakeReleaseManager(),
            lifecycle_status=lambda: "STOPPED",
        ).create(destination=tmp_path / "busy.zip")

    assert raised.value.reason_code == "desktop_backup_writer_detected"
    assert control.events == ["begin_drain", "resume_admission"]
    assert control.admission_open is True
    assert not (tmp_path / "busy.zip").exists()
    assert not list(config.runtime_root.glob("desktop-backup-*"))
    assert not list(tmp_path.glob(".busy.zip.*.tmp"))


def test_backup_copy_failure_preserves_source_and_existing_archive_and_cleans_pending_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    valid_control = FakeControl()
    valid_backup = tmp_path / "valid.zip"
    BackupManager(
        config,
        control_plane=valid_control,
        inventory_resolver=StaticInventory(inventory),
        release_manager=FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).create(destination=valid_backup)
    valid_bytes = valid_backup.read_bytes()
    source_bytes = paths["auth_users"].read_bytes()
    failing_control = FakeControl()

    class FailingBackupManager(BackupManager):
        def _snapshot_file(self, source, destination, *, consistency):
            raise OSError("simulated disk copy failure")

    pending = tmp_path / "pending.zip"
    with pytest.raises(OSError, match="simulated disk copy failure"):
        FailingBackupManager(
            config,
            control_plane=failing_control,
            inventory_resolver=StaticInventory(inventory),
            release_manager=FakeReleaseManager(),
            lifecycle_status=lambda: "STOPPED",
        ).create(destination=pending)

    assert paths["auth_users"].read_bytes() == source_bytes
    assert valid_backup.read_bytes() == valid_bytes
    assert not pending.exists()
    assert not list(pending.parent.glob(f".{pending.name}.*.tmp"))
    assert not list(config.runtime_root.glob("desktop-backup-*"))
    assert failing_control.events == ["begin_drain", "resume_admission"]
    assert failing_control.admission_open is True


def test_restore_validates_rag_event_journal_and_final_transcript_from_isolated_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _config(tmp_path)
    original_root = tmp_path / "rag-original"
    store = PaperRagTranscriptFileStore(original_root)
    actor_scope = {"tenant_id": "tenant-a", "user_id": "user-a", "memory_namespace": "research:user-a"}
    spec = SimpleNamespace(session_id="session-journal")
    store.record_event(spec=spec, sequence=0, event_type="question_received", payload={"question_id": "q-1"}, actor_scope=actor_scope)
    store.persist(
        {
            "transcript_id": "transcript-journal",
            "session_id": "session-journal",
            "events": [{"event_type": "question_received", "payload": {"question_id": "q-1"}}],
        },
        actor_scope=actor_scope,
    )
    inventory = StorageInventory((StorageOwner("research_rag_transcripts", original_root, "directory", DATA_SCHEMAS["research_rag_transcripts"], True, True, "paper_rag_transcript_store"),))
    backup = tmp_path / "rag-journal.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)
    original_load = PaperRagTranscriptFileStore.load
    original_load_journal = PaperRagTranscriptFileStore.load_journal
    observed_roots: list[Path] = []

    def guarded_load(self, transcript_id: str, *, actor_scope):
        observed_roots.append(self.root)
        if self.root == original_root.resolve():
            raise AssertionError("validator read original transcript root")
        return original_load(self, transcript_id, actor_scope=actor_scope)

    def guarded_load_journal(self, session_id: str, *, actor_scope):
        observed_roots.append(self.root)
        if self.root == original_root.resolve():
            raise AssertionError("validator read original journal root")
        return original_load_journal(self, session_id, actor_scope=actor_scope)

    monkeypatch.setattr(PaperRagTranscriptFileStore, "load", guarded_load)
    monkeypatch.setattr(PaperRagTranscriptFileStore, "load_journal", guarded_load_journal)
    result = RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(backup, target_root=tmp_path / "rag-isolated")

    receipt = result.details["validation"]["owners"]["research_rag_transcripts"]["application_validation"]
    assert receipt["journal_count"] == 1
    assert receipt["transcript_count"] == 1
    assert observed_roots and all(root != original_root.resolve() for root in observed_roots)


def test_restore_rejects_tampered_rag_journal_checksum(tmp_path: Path) -> None:
    config = _config(tmp_path)
    original_root = tmp_path / "rag-tampered"
    store = PaperRagTranscriptFileStore(original_root)
    actor_scope = {"tenant_id": "tenant-a", "user_id": "user-a", "memory_namespace": "research:user-a"}
    store.record_event(spec=SimpleNamespace(session_id="session-1"), sequence=0, event_type="asked", payload={"question_id": "q-1"}, actor_scope=actor_scope)
    journal_path = store.journal_path_for("session-1")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["journal_checksum"] = "sha256:" + "0" * 64
    journal_path.write_text(json.dumps(journal), encoding="utf-8")
    inventory = StorageInventory((StorageOwner("research_rag_transcripts", original_root, "directory", DATA_SCHEMAS["research_rag_transcripts"], True, True, "paper_rag_transcript_store"),))
    backup = tmp_path / "rag-tampered.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(backup, target_root=tmp_path / "rag-tampered-isolated")
    assert raised.value.reason_code == "desktop_restore_rag_transcript_invalid"


def test_restore_rejects_file_uri_in_operational_artifact_reference_but_preserves_opaque_source_anchor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    paths["research_history"].write_text(
        json.dumps({"schemaVersion": "newsroom_research_history.v1", "users": {}, "artifact_ref": "file:///C:/private/report.json", "source_ref": "file:///C:/immutable/source.pdf"}),
        encoding="utf-8",
    )
    backup = tmp_path / "file-uri.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(backup, target_root=tmp_path / "file-uri-isolated")
    assert raised.value.reason_code == "desktop_restore_absolute_reference_unsupported"


def test_restore_rejects_absolute_path_in_sqlite_operational_column(tmp_path: Path) -> None:
    config = _config(tmp_path)
    root = tmp_path / "sqlite-owner"
    root.mkdir()
    database = root / "records.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, artifact_path TEXT NOT NULL)")
        connection.execute("INSERT INTO records(artifact_path) VALUES (?)", (str(tmp_path / "outside" / "payload.json"),))
    inventory = StorageInventory((StorageOwner("runtime_artifacts", root, "directory", DATA_SCHEMAS["runtime_artifacts"], True, True, "mixed_tree"),))
    backup = tmp_path / "sqlite-path.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(backup, target_root=tmp_path / "sqlite-isolated")
    assert raised.value.reason_code == "desktop_restore_sqlite_reference_unsupported"



def test_isolated_validation_reads_real_owner_adapters_only_from_staging_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta
    from backend.research.domain.common import SourceLineage
    from backend.research.domain.document import ResearchDocument, ResearchSection
    from backend.research.domain.paper import ResearchPaper
    from backend.research.ports.run_store import ResearchRunRecord
    from framework.harness import ArtifactWriteRequest
    from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
    from infrastructure.research.catalog_store import FilesystemResearchCatalogStore
    from infrastructure.research.filesystem_run_store import (
        FilesystemResearchRunStore,
        RESEARCH_RUN_RECORD_SCHEMA_VERSION,
        RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    from infrastructure.research.local_chunk_store import LocalChunkPayloadStore
    from infrastructure.storage.artifacts.result_sqlite import SQLiteGraphResultStore
    from infrastructure.storage.harness.sqlite import SQLiteHarnessSideEffectStore
    from infrastructure.storage.harness.sqlite_node_output import SQLiteHarnessNodeOutputResource
    from interfaces.services.auth_service import AuthSession, LocalJsonAuthSessionRepository, LocalJsonAuthUserRepository, StoredAuthUser
    from interfaces.services.research_history_service import ResearchHistoryService
    from interfaces.services.research_import_service import ResearchImportService
    from tests.backend.research.integration.test_research_artifact_publication import _request, _runtime
    from tests.framework.harness.control_plane.test_node_output_resource import _activity, _admission, _candidate, _success_guard
    from tests.infrastructure.storage.test_graph_result_store import _binding, _envelope

    original_root = tmp_path / "original"
    staging_root = tmp_path / "staging"
    original_root.mkdir()
    staging_root.mkdir()
    staged = {
        "auth_users": staging_root / "users.json",
        "auth_sessions": staging_root / "sessions.json",
        "research_history": staging_root / "history.json",
        "research_imports": staging_root / "imports",
        "runtime_artifacts": staging_root / "artifacts",
        "research_run_store": staging_root / "runs",
        "research_rag_local": staging_root / "chunks",
        "research_rag_transcripts": staging_root / "transcripts",
        "research_paper_catalog": staging_root / "catalog",
        "research_graph_results": staging_root / "graph.sqlite3",
        "research_harness_side_effects": staging_root / "effects.sqlite3",
        "research_harness_node_output": staging_root / "node-output.sqlite3",
    }
    user_id = "user-publication"
    paper_id = "paper-harness-001"
    run_id = "restored-run-1"
    source_revision = "sha256:" + sha256(b"%PDF-1.4\n").hexdigest()
    scope = {"tenant_id": "tenant-1", "user_id": user_id, "memory_namespace": f"research:user:{user_id}"}
    LocalJsonAuthUserRepository(staged["auth_users"]).add_user(StoredAuthUser(userId=user_id, username="owner", passwordHash="hash", passwordSalt="salt"))
    now = datetime.now(UTC)
    LocalJsonAuthSessionRepository(staged["auth_sessions"]).create_session(AuthSession("session-1", user_id, "digest", now, now + timedelta(hours=1)))

    import_service = ResearchImportService(staged["research_imports"])
    import_id = "imp_" + "a" * 32
    import_service.create_received(user_id=user_id, content=b"%PDF-1.4\n", filename="paper.pdf", import_id=import_id, source_revision=source_revision, source_fingerprint=source_revision)
    import_record = import_service.mark_completed(user_id=user_id, import_id=import_id, result={"paperId": paper_id})

    catalog = FilesystemResearchCatalogStore(staged["research_paper_catalog"], tenant_id="tenant-1", user_id=user_id, memory_namespace=scope["memory_namespace"])
    catalog.save(ResearchPaper(paper_id=paper_id, title="Paper", actor_scope=scope))
    catalog.save(ResearchDocument(paper_id=paper_id, document_id="document-1", source_hash=source_revision, sections=[ResearchSection(section_id="section-1", title="Body", text="restored paper body", source_ref=f"source://{paper_id}/body")], lineage=SourceLineage(source_refs=[f"source://{paper_id}"], metadata=scope), actor_scope=scope))
    chunks = LocalChunkPayloadStore(staged["research_rag_local"])
    chunks.index_payloads([{"chunk_id": "chunk-1", "paper_id": paper_id, "document_id": "document-1", "content": "restored paper body"}])

    artifact_port = FilesystemHarnessArtifactPort(staged["runtime_artifacts"], accepted_run_resolver=lambda *_args: True)
    side_effect_store = SQLiteHarnessSideEffectStore(staged["research_harness_side_effects"])
    runtime = _runtime(artifact_port=artifact_port, side_effect_store=side_effect_store)
    try:
        result = runtime.run(_request(run_id))
    finally:
        side_effect_store.close()
    assert result.succeeded is True
    run_store = FilesystemResearchRunStore(
        staged["research_run_store"],
        result_decoder=type(result).from_dict,
        supported_schema_versions=(RESEARCH_RUN_RECORD_SCHEMA_VERSION, RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2),
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )
    run_store.save(ResearchRunRecord(run_id=run_id, paper_id=paper_id, result=result))

    history = ResearchHistoryService(staged["research_history"])
    history.replace(user_id=user_id, revision=0, visits=[], groups=[], workspace_items={"savedReports": [{"id": "saved-report-1", "reportId": "report-1", "runId": run_id, "kind": "accepted", "title": "Restored report", "savedAt": 1}]})

    rag_store = PaperRagTranscriptFileStore(staged["research_rag_transcripts"])
    rag_spec = SimpleNamespace(session_id="rag-session-1")
    rag_store.record_event(spec=rag_spec, sequence=0, event_type="answer_completed", payload={"run_id": run_id}, actor_scope=scope)
    rag_store.persist({"transcript_id": "rag-transcript-1", "session_id": "rag-session-1", "run_id": run_id, "events": [{"event_type": "answer_completed", "payload": {"run_id": run_id}}]}, actor_scope=scope)

    graph = SQLiteGraphResultStore(staged["research_graph_results"])
    graph.put(_envelope(_binding(run_id=run_id)))
    graph.verify_integrity()
    node_output = SQLiteHarnessNodeOutputResource(staged["research_harness_node_output"])
    activity = _activity()
    lease = node_output.acquire_after_admission(activity, _admission(activity, owner_attempt_id="restore-attempt-1"))
    node_output.commit(node_output.stage(lease, _candidate("restored"), staged_at=now), _success_guard(), committed_at=now)
    node_output.close()

    owners = []
    for owner_id, path in staged.items():
        kind = "file" if path.suffix in {".json", ".sqlite3"} else "directory"
        owners.append(StorageOwner(owner_id, original_root / path.name, kind, DATA_SCHEMAS[owner_id], True, True, "sqlite" if path.suffix == ".sqlite3" else "quiescent_tree"))
    inventory = StorageInventory(tuple(owners))
    original_prefix = original_root.resolve()
    observed: list[Path] = []

    def guard(value: Path) -> None:
        resolved = value.resolve(strict=False)
        assert resolved != original_prefix and not resolved.is_relative_to(original_prefix)
        observed.append(resolved)

    original_users = LocalJsonAuthUserRepository.list_users
    original_sessions = LocalJsonAuthSessionRepository._read_records
    original_history = ResearchHistoryService.read
    original_imports = ResearchImportService._read_records
    original_chunks = LocalChunkPayloadStore._read_snapshot
    original_catalog = FilesystemResearchCatalogStore._read
    original_artifact = FilesystemHarnessArtifactPort.read_artifact
    original_run = FilesystemResearchRunStore.get_by_run_id
    original_rag = PaperRagTranscriptFileStore.load
    original_rag_journal = PaperRagTranscriptFileStore.load_journal
    original_graph = SQLiteGraphResultStore.get
    original_decision = SQLiteHarnessSideEffectStore.get_decision
    original_outcome = SQLiteHarnessSideEffectStore.read_outcome
    original_node = SQLiteHarnessNodeOutputResource.committed_output
    monkeypatch.setattr(LocalJsonAuthUserRepository, "list_users", lambda self: (guard(self.path), original_users(self))[1])
    monkeypatch.setattr(LocalJsonAuthSessionRepository, "_read_records", lambda self: (guard(self.path), original_sessions(self))[1])
    monkeypatch.setattr(ResearchHistoryService, "read", lambda self, **kwargs: (guard(self.path), original_history(self, **kwargs))[1])
    monkeypatch.setattr(ResearchImportService, "_read_records", lambda self: (guard(self.root), original_imports(self))[1])
    monkeypatch.setattr(LocalChunkPayloadStore, "_read_snapshot", lambda self: (guard(self.root), original_chunks(self))[1])
    monkeypatch.setattr(FilesystemResearchCatalogStore, "_read", lambda self: (guard(self.root), original_catalog(self))[1])
    monkeypatch.setattr(FilesystemHarnessArtifactPort, "read_artifact", lambda self, ref: (guard(self.root), original_artifact(self, ref))[1])
    monkeypatch.setattr(FilesystemResearchRunStore, "get_by_run_id", lambda self, value: (guard(self.root), original_run(self, value))[1])
    monkeypatch.setattr(PaperRagTranscriptFileStore, "load", lambda self, value, **kwargs: (guard(self.root), original_rag(self, value, **kwargs))[1])
    monkeypatch.setattr(PaperRagTranscriptFileStore, "load_journal", lambda self, value, **kwargs: (guard(self.root), original_rag_journal(self, value, **kwargs))[1])
    monkeypatch.setattr(SQLiteGraphResultStore, "get", lambda self, binding: (guard(Path(self.path)), original_graph(self, binding))[1])
    monkeypatch.setattr(SQLiteHarnessSideEffectStore, "get_decision", lambda self, value: (guard(Path(self.path)), original_decision(self, value))[1])
    monkeypatch.setattr(SQLiteHarnessSideEffectStore, "read_outcome", lambda self, **kwargs: (guard(Path(self.path)), original_outcome(self, **kwargs))[1])
    monkeypatch.setattr(SQLiteHarnessNodeOutputResource, "committed_output", lambda self, resource: (guard(Path(self.path)), original_node(self, resource))[1])

    validation = _validate_restored_owners(inventory, staged)

    isolation = validation["application_isolation"]
    assert isolation["verified"] is True
    closure = isolation["reference_closure"]
    assert closure["identity_link_count"] >= 5
    assert closure["workspace_run_reference_count"] == 1
    assert closure["rag_run_reference_count"] == 1
    assert closure["run_artifact_reference_count"] >= 4
    assert closure["import_paper_reference_count"] == 1
    assert closure["import_source_revision_count"] == 1
    assert closure["chunk_paper_reference_count"] == 1
    assert closure["chunk_document_reference_count"] == 1
    owner_receipts = validation["owners"]
    assert owner_receipts["research_run_store"]["application_validation"]["record_count"] == 1
    assert owner_receipts["research_rag_transcripts"]["application_validation"]["transcript_count"] == 1
    assert owner_receipts["research_rag_transcripts"]["application_validation"]["journal_count"] == 1
    assert owner_receipts["research_graph_results"]["application_validation"]["attempt_count"] == 1
    assert owner_receipts["research_harness_side_effects"]["application_validation"]["decision_count"] >= 1
    assert owner_receipts["research_harness_node_output"]["application_validation"]["commit_count"] == 1
    assert observed and all(path == staging_root.resolve() or path.is_relative_to(staging_root.resolve()) for path in observed)
    assert import_record["status"] == "completed"


def test_restore_rejects_session_user_missing_from_isolated_auth_store(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta
    from interfaces.services.auth_service import AuthSession, LocalJsonAuthSessionRepository

    config = _config(tmp_path)
    inventory, paths = _owner_files(tmp_path)
    now = datetime.now(UTC)
    LocalJsonAuthSessionRepository(paths["auth_sessions"]).create_session(AuthSession("session-dangling", "missing-user", "digest", now, now + timedelta(hours=1)))
    backup = tmp_path / "dangling-session.zip"
    BackupManager(config, control_plane=FakeControl(), inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").create(destination=backup)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(config, inventory_resolver=StaticInventory(inventory), release_manager=FakeReleaseManager(), lifecycle_status=lambda: "STOPPED").restore(backup, target_root=tmp_path / "dangling-session-isolated")
    assert raised.value.reason_code == "desktop_restore_identity_reference_dangling"
