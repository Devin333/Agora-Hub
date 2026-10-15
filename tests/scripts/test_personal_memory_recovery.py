from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.domain.personal_memory import (
    PersonalMemoryOwner,
    PersonalMemoryStatus,
)
from framework.shared.local_desktop import resolve_local_desktop_config
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.recovery import (
    BackupManager,
    RestoreManager,
    StorageInventory,
    StorageInventoryResolver,
    StorageOwner,
)
from scripts.local_desktop.release import DATA_SCHEMAS


class _StaticInventory:
    def __init__(self, inventory: StorageInventory) -> None:
        self.inventory = inventory
        self.environ: dict[str, str] = {}

    def resolve(self, *, require_existing: bool = True) -> StorageInventory:
        return self.inventory


class _FakeReleaseManager:
    def active(self):
        raise DesktopOperationError("release_missing", "release missing")


class _FakeControl:
    def __init__(self) -> None:
        self.admission_open = True
        self.rotations: list[str] = []

    def status(self) -> dict[str, bool]:
        return {"admission_open": self.admission_open}

    def begin_drain(self, *, timeout_seconds: float) -> dict[str, float | int]:
        self.admission_open = False
        return {"active_count": 0, "timeout_seconds": timeout_seconds}

    def resume_admission(self) -> dict[str, bool]:
        self.admission_open = True
        return {"admission_open": True}

    def rotate_generation(self, *, reason: str) -> dict[str, str]:
        self.rotations.append(reason)
        return {"generation": "gen_" + "7" * 32}


def _config(tmp_path: Path):
    return resolve_local_desktop_config(
        project_root=tmp_path,
        cli_overrides={
            "runtime_root": str(tmp_path / "runtime"),
            "release_root": str(tmp_path / "releases"),
            "backup_root": str(tmp_path / "backups"),
            "control_path": str(tmp_path / "control" / "control.json"),
        },
    )


def _inventory(database: Path) -> StorageInventory:
    return StorageInventory(
        (
            StorageOwner(
                "research_personal_memory",
                database,
                "file",
                DATA_SCHEMAS["research_personal_memory"],
                True,
                True,
                "sqlite",
            ),
        )
    )


def _source_ref(reference: str) -> list[dict[str, str]]:
    return [{"source_type": "user_input", "source_ref": reference}]


def _read_records(database: Path, owner: PersonalMemoryOwner):
    with SQLitePersonalMemoryStore(database) as store:
        service = PersonalMemoryService(store)
        page = service.list(owner, statuses=tuple(PersonalMemoryStatus), limit=100)
        return {record.memory_id: record for record in page.records}, service.get_policy(owner)


def test_inventory_registers_personal_memory_with_controlled_default_and_override(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    research_root = tmp_path / "portable-research"
    default_inventory = StorageInventoryResolver(
        config, environ={"NEWS_RESEARCH_ROOT": str(research_root)}
    ).resolve(require_existing=False)

    default_owner = next(
        owner
        for owner in default_inventory.owners
        if owner.owner_id == "research_personal_memory"
    )
    assert default_owner.path == (research_root / "personal-memory.sqlite3").resolve()
    assert default_owner.kind == "file"
    assert default_owner.consistency == "sqlite"
    assert default_owner.schema == "newsroom.personal-memory-store/v1"
    assert DATA_SCHEMAS["research_personal_memory"] == default_owner.schema

    override = tmp_path / "portable-profile" / "memory.sqlite3"
    override_inventory = StorageInventoryResolver(
        config,
        environ={
            "NEWS_RESEARCH_ROOT": str(research_root),
            "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH": str(override),
        },
    ).resolve(require_existing=False)
    override_owner = next(
        owner
        for owner in override_inventory.owners
        if owner.owner_id == "research_personal_memory"
    )
    assert override_owner.path == override.resolve()


def test_personal_memory_sqlite_backup_preview_and_confirmed_activation_are_exact(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    database = (tmp_path / "profile" / "personal-memory.sqlite3").resolve()
    database.parent.mkdir()
    inventory = _inventory(database)
    static_inventory = _StaticInventory(inventory)
    owner = PersonalMemoryOwner(
        user_id="user-1", tenant_id="tenant-1", namespace="research/default"
    )
    store = SQLitePersonalMemoryStore(database)
    service = PersonalMemoryService(store)
    try:
        candidate = service.create_candidate(
            owner,
            content="Prefer concise evidence summaries.",
            category="reading_preference",
            source_refs=_source_ref("conversation:1"),
            client_request_id="candidate-1",
        )
        confirmed = service.confirm(
            owner, candidate.memory_id, expected_revision=candidate.revision
        )
        disabled_created = service.create_confirmed(
            owner,
            content="Track local retrieval systems.",
            category="research_interest",
            source_refs=_source_ref("conversation:2"),
            client_request_id="confirmed-2",
        )
        disabled = service.disable(
            owner,
            disabled_created.memory_id,
            expected_revision=disabled_created.revision,
        )
        deleted_created = service.create_confirmed(
            owner,
            content="Background retained as a tombstone.",
            category="background",
            source_refs=_source_ref("conversation:3"),
            client_request_id="confirmed-3",
        )
        deleted = service.delete(
            owner,
            deleted_created.memory_id,
            expected_revision=deleted_created.revision,
        )
        policy = service.get_policy(owner)
        policy = service.update_policy(
            owner,
            allow_remote_processing=True,
            expected_revision=policy.revision,
        )

        assert database.with_name(f"{database.name}-wal").exists()
        backup = tmp_path / "personal-memory-backup.zip"
        BackupManager(
            config,
            control_plane=_FakeControl(),
            inventory_resolver=static_inventory,
            release_manager=_FakeReleaseManager(),
            lifecycle_status=lambda: "STOPPED",
        ).create(destination=backup)

        with zipfile.ZipFile(backup) as archive:
            assert "data/research_personal_memory/value" in archive.namelist()
            assert not any(name.endswith(("-wal", "-shm")) for name in archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
        assert manifest["request_restore_policy"] == (
            "rotate_generation_and_mark_nonterminal_needs_review"
        )

        service.enable(owner, disabled.memory_id, expected_revision=disabled.revision)
        service.delete(owner, confirmed.memory_id, expected_revision=confirmed.revision)
        service.update_policy(
            owner,
            allow_remote_processing=False,
            expected_revision=policy.revision,
        )
    finally:
        store.close()

    blocked_control = _FakeControl()
    restore_manager = RestoreManager(
        config,
        control_plane=blocked_control,
        inventory_resolver=static_inventory,
        release_manager=_FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    )
    preview = restore_manager.restore(
        backup, target_root=tmp_path / "isolated-preview"
    )
    preview_contract = preview.details["personal_memory_restore"]
    assert preview_contract["requires_explicit_activation_confirmation"] is True
    assert preview_contract["status_counts"] == {
        "active": 1,
        "deleted": 1,
        "disabled": 1,
    }
    restored_database = (
        Path(preview.details["restore_root"])
        / "owners"
        / "research_personal_memory"
        / "value"
    )
    restored_records, restored_policy = _read_records(restored_database, owner)
    assert restored_records[confirmed.memory_id].status is PersonalMemoryStatus.ACTIVE
    assert restored_records[confirmed.memory_id].revision == confirmed.revision
    assert restored_records[disabled.memory_id].status is PersonalMemoryStatus.DISABLED
    assert restored_records[deleted.memory_id].status is PersonalMemoryStatus.DELETED
    assert restored_records[deleted.memory_id].content == deleted.content
    assert restored_policy.allow_remote_processing is True
    assert restored_policy.revision == policy.revision

    with pytest.raises(DesktopOperationError) as raised:
        restore_manager.restore(
            backup,
            target_root=tmp_path / "confirmation-required",
            activate=True,
        )
    assert raised.value.reason_code == (
        "desktop_restore_personal_memory_confirmation_required"
    )
    assert blocked_control.rotations == []
    assert blocked_control.admission_open is True
    live_records, live_policy = _read_records(database, owner)
    assert live_records[confirmed.memory_id].status is PersonalMemoryStatus.DELETED
    assert live_records[disabled.memory_id].status is PersonalMemoryStatus.ACTIVE
    assert live_policy.allow_remote_processing is False

    activation_control = _FakeControl()
    activated = RestoreManager(
        config,
        control_plane=activation_control,
        inventory_resolver=static_inventory,
        release_manager=_FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).restore(
        backup,
        target_root=tmp_path / "confirmed-activation",
        activate=True,
        personal_memory_confirmation=preview_contract["confirmation_token"],
    )
    assert activated.status == "ACTIVATED"
    assert activated.details["activation"]["generation"] == "gen_" + "7" * 32
    assert len(activation_control.rotations) == 1
    assert activation_control.rotations[0].startswith("restore:")
    activated_records, activated_policy = _read_records(database, owner)
    assert activated_records[confirmed.memory_id].status is PersonalMemoryStatus.ACTIVE
    assert activated_records[disabled.memory_id].status is PersonalMemoryStatus.DISABLED
    assert activated_records[deleted.memory_id].status is PersonalMemoryStatus.DELETED
    assert activated_records[deleted.memory_id].content == deleted.content
    assert activated_policy.allow_remote_processing is True


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        ("application_id", "desktop_restore_personal_memory_schema_mismatch"),
        ("user_version", "desktop_restore_personal_memory_schema_mismatch"),
        ("metadata", "desktop_restore_personal_memory_schema_mismatch"),
        ("table", "desktop_restore_sqlite_schema_mismatch"),
    ),
)
def test_restore_rejects_malformed_personal_memory_schema(
    tmp_path: Path, mutation: str, expected_reason: str
) -> None:
    config = _config(tmp_path)
    database = (tmp_path / "profile" / "personal-memory.sqlite3").resolve()
    database.parent.mkdir()
    with SQLitePersonalMemoryStore(database):
        pass
    with sqlite3.connect(database) as connection:
        if mutation == "application_id":
            connection.execute("PRAGMA application_id = 0")
        elif mutation == "user_version":
            connection.execute("PRAGMA user_version = 2")
        elif mutation == "metadata":
            connection.execute("DELETE FROM personal_memory_metadata")
        else:
            connection.execute("DROP TABLE personal_memory_create_requests")

    inventory = _inventory(database)
    static_inventory = _StaticInventory(inventory)
    backup = tmp_path / f"malformed-personal-memory-{mutation}.zip"
    BackupManager(
        config,
        control_plane=_FakeControl(),
        inventory_resolver=static_inventory,
        release_manager=_FakeReleaseManager(),
        lifecycle_status=lambda: "STOPPED",
    ).create(destination=backup)

    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(
            config,
            inventory_resolver=static_inventory,
            release_manager=_FakeReleaseManager(),
            lifecycle_status=lambda: "STOPPED",
        ).restore(backup, target_root=tmp_path / f"malformed-restore-{mutation}")
    assert raised.value.reason_code == expected_reason


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract")
def test_inventory_rejects_personal_memory_path_through_windows_junction(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    target = tmp_path / "junction-target"
    target.mkdir()
    sentinel = target / "sentinel.txt"
    sentinel.write_text("do not remove", encoding="utf-8")
    junction = tmp_path / "junction-profile"
    command = (
        "New-Item -ItemType Junction -Path $env:NEWSROOM_TEST_JUNCTION "
        "-Target $env:NEWSROOM_TEST_JUNCTION_TARGET "
        "-ErrorAction Stop | Out-Null"
    )
    process_environment = dict(os.environ)
    process_environment.update(
        {
            "NEWSROOM_TEST_JUNCTION": str(junction),
            "NEWSROOM_TEST_JUNCTION_TARGET": str(target),
        }
    )
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
        env=process_environment,
    )
    try:
        resolver = StorageInventoryResolver(
            config,
            environ={
                "NEWSROOM_RESEARCH_PERSONAL_MEMORY_PATH": str(
                    junction / "personal-memory.sqlite3"
                )
            },
        )
        with pytest.raises(DesktopOperationError) as raised:
            resolver.resolve(require_existing=False)
        assert raised.value.reason_code == "desktop_reparse_point_rejected"
    finally:
        os.rmdir(junction)
    assert sentinel.read_text(encoding="utf-8") == "do not remove"
