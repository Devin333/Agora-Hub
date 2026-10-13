from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from framework.shared.local_desktop import LocalDesktopConfig, utc_timestamp
from scripts.local_desktop.common import (
    DesktopOperationError,
    OperationResult,
    atomic_write_json,
    ensure_contained,
    exclusive_lock,
    protect_private_path,
    read_json_object,
    reject_reparse_path,
)
from scripts.local_desktop.control import ApplicationControlPlane
from scripts.local_desktop.release import ReleaseManager
from scripts.local_desktop.storage_contract import resolve_research_storage_paths, storage_schema

BACKUP_SCHEMA = "newsroom.local-desktop-backup/v1"
INVENTORY_SCHEMA = "newsroom.local-desktop-storage-inventory/v1"
RESTORE_SCHEMA = "newsroom.local-desktop-restore/v1"
ACTIVATION_SCHEMA = "newsroom.local-desktop-activation-journal/v1"
MAX_BACKUP_FILES = 200_000
MAX_BACKUP_BYTES = 50 * 1024 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 8 * 1024 * 1024 * 1024
_EXCLUDED_NAMES = {
    ".env",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "models",
    "cache",
    "caches",
    "releases",
    "logs",
}
_EXCLUDED_SUFFIXES = {".log", ".tmp", ".pyc", ".pyo"}


@dataclass(frozen=True)
class StorageOwner:
    owner_id: str
    path: Path
    kind: str
    schema: str
    required: bool
    sensitive: bool
    consistency: str
    references: tuple[str, ...] = ()
    external_requirements: tuple[str, ...] = ()
    backup_source_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_id": self.owner_id,
            "source_path": str(self.path),
            "kind": self.kind,
            "schema": self.schema,
            "required": self.required,
            "sensitive": self.sensitive,
            "consistency": self.consistency,
            "references": list(self.references),
            "external_requirements": list(self.external_requirements),
        }


@dataclass(frozen=True)
class StorageInventory:
    owners: tuple[StorageOwner, ...]
    schema: str = INVENTORY_SCHEMA

    def __post_init__(self) -> None:
        identifiers = [item.owner_id for item in self.owners]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("storage owner identifiers must be unique")
        resolved = [(item.owner_id, item.path.resolve(strict=False)) for item in self.owners]
        for index, (owner, path) in enumerate(resolved):
            for other_owner, other in resolved[index + 1 :]:
                if path == other or path.is_relative_to(other) or other.is_relative_to(path):
                    raise DesktopOperationError(
                        "desktop_inventory_roots_overlap",
                        "storage owner roots must not overlap",
                        details={"owner": owner, "other_owner": other_owner},
                    )

    def to_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "owners": [item.to_dict() for item in self.owners]}


@dataclass(frozen=True)
class BackupFile:
    owner_id: str
    path: str
    size: int
    sha256: str
    file_type: str = "file"

    def to_dict(self) -> dict[str, Any]:
        return {"owner_id": self.owner_id, "path": self.path, "size": self.size, "sha256": self.sha256, "file_type": self.file_type}


@dataclass(frozen=True)
class BackupManifest:
    backup_id: str
    created_at: str
    release_identity: Mapping[str, Any]
    inventory: StorageInventory
    files: tuple[BackupFile, ...]
    excluded: tuple[str, ...]
    external_requirements: tuple[str, ...]
    schema: str = BACKUP_SCHEMA

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "release_identity": dict(self.release_identity),
            "inventory": self.inventory.to_dict(),
            "files": [item.to_dict() for item in self.files],
            "file_count": len(self.files),
            "total_bytes": self.total_bytes,
            "excluded": list(self.excluded),
            "external_requirements": list(self.external_requirements),
            "sessions_restore_policy": "invalidate_all",
            "request_restore_policy": "rotate_generation_and_mark_nonterminal_needs_review",
        }


class StorageInventoryResolver:
    def __init__(self, config: LocalDesktopConfig, *, environ: Mapping[str, str] | None = None) -> None:
        self.config = config
        self.environ = dict(environ if environ is not None else os.environ)

    def resolve(self, *, require_existing: bool = True) -> StorageInventory:
        if str(self.environ.get("NEWS_DATABASE_DSN") or "").strip():
            raise DesktopOperationError(
                "desktop_inventory_external_database_unsupported",
                "local desktop backup and restore require owned local storage; NEWS_DATABASE_DSN is external",
            )
        try:
            settings = resolve_research_storage_paths(self.config.project_root, self.environ)
        except ValueError as exc:
            raise DesktopOperationError("desktop_research_storage_settings_invalid", "Research storage settings are invalid") from exc
        owners = [
            self._file("auth_users", "NEWSROOM_AUTH_USERS_PATH", ".newsroom/auth/users.json", storage_schema("auth_users"), required=True, sensitive=True, consistency="json_lock"),
            self._file("auth_sessions", "NEWSROOM_AUTH_SESSIONS_PATH", ".newsroom/auth/sessions.json", storage_schema("auth_sessions"), required=False, sensitive=True, consistency="json_lock"),
            self._file("auth_otp", "NEWSROOM_AUTH_OTP_PATH", ".newsroom/auth/otp_challenges.json", storage_schema("auth_otp"), required=False, sensitive=True, consistency="json_lock"),
            self._file("auth_oauth", "NEWSROOM_AUTH_OAUTH_PATH", ".newsroom/auth/oauth_states.json", storage_schema("auth_oauth"), required=False, sensitive=True, consistency="json_lock"),
            self._file("research_history", "NEWSROOM_RESEARCH_HISTORY_PATH", ".newsroom/research/history.json", storage_schema("research_history"), required=True, sensitive=True, consistency="json_lock", references=("research_imports", "runtime_artifacts")),
            self._directory("research_imports", "NEWSROOM_RESEARCH_IMPORT_DIR", ".newsroom/research/imports", storage_schema("research_imports"), required=True, sensitive=True, consistency="quiescent_tree", references=("runtime_artifacts",)),
            StorageOwner("runtime_artifacts", settings.artifact_root, "directory", storage_schema("runtime_artifacts"), True, True, "mixed_tree", (), ("provider credentials",)),
            StorageOwner("research_run_store", settings.run_store_root, "directory", storage_schema("research_run_store"), True, True, "research_run_store", ("runtime_artifacts",), ()),
            StorageOwner("research_rag_transcripts", settings.rag_transcript_root, "directory", storage_schema("research_rag_transcripts"), False, True, "paper_rag_transcript_store", ("research_run_store", "runtime_artifacts"), ()),
            StorageOwner("research_paper_catalog", settings.research_root / "paper-catalog", "directory", storage_schema("research_paper_catalog"), False, True, "quiescent_tree", ("research_rag_local",), ()),
            StorageOwner("research_graph_results", settings.research_root / "graph-results.sqlite3", "file", storage_schema("research_graph_results"), False, True, "sqlite", ("runtime_artifacts",), ()),
            StorageOwner("research_harness_side_effects", settings.research_root / "harness-side-effects.sqlite3", "file", storage_schema("research_harness_side_effects"), False, True, "sqlite", ("runtime_artifacts",), ()),
            StorageOwner("research_harness_node_output", settings.research_root / "harness-node-output.sqlite3", "file", storage_schema("research_harness_node_output"), False, True, "sqlite", ("runtime_artifacts",), ()),
            StorageOwner("desktop_control", self.config.control_path.resolve(strict=False), "file", storage_schema("desktop_control"), True, True, "json_lock", ("runtime_artifacts",), ()),
        ]
        if settings.rag_backend == "local":
            owners.append(StorageOwner("research_rag_local", settings.rag_local_root, "directory", storage_schema("research_rag_local"), True, True, "local_chunk_store", ("research_run_store",), ()))
        else:
            run_owner = next(owner for owner in owners if owner.owner_id == "research_run_store")
            owners[owners.index(run_owner)] = replace(run_owner, external_requirements=("external vector index must be rebuilt or restored independently",))
        self._validate_known_paths(owners, require_existing=require_existing)
        return StorageInventory(tuple(owners))

    def _file(self, owner_id: str, env_name: str, default: str, schema: str, *, required: bool, sensitive: bool, consistency: str, references: tuple[str, ...] = (), external_requirements: tuple[str, ...] = ()) -> StorageOwner:
        return StorageOwner(owner_id, self._path(self.environ.get(env_name) or default), "file", schema, required, sensitive, consistency, references, external_requirements)

    def _directory(self, owner_id: str, env_name: str, default: str, schema: str, *, required: bool, sensitive: bool, consistency: str, references: tuple[str, ...] = (), external_requirements: tuple[str, ...] = ()) -> StorageOwner:
        return StorageOwner(owner_id, self._path(self.environ.get(env_name) or default), "directory", schema, required, sensitive, consistency, references, external_requirements)

    def _path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.config.project_root / path
        resolved = path.resolve(strict=False)
        if resolved == Path(resolved.anchor):
            raise DesktopOperationError("desktop_inventory_path_unsafe", "storage owner cannot resolve to a filesystem root")
        return resolved

    @staticmethod
    def _validate_known_paths(owners: list[StorageOwner], *, require_existing: bool) -> None:
        for owner in owners:
            if owner.path.exists() and owner.kind == "file" and not owner.path.is_file():
                raise DesktopOperationError("desktop_inventory_kind_mismatch", f"storage owner is not a file: {owner.owner_id}")
            if owner.path.exists() and owner.kind == "directory" and not owner.path.is_dir():
                raise DesktopOperationError("desktop_inventory_kind_mismatch", f"storage owner is not a directory: {owner.owner_id}")
            if require_existing and owner.required and not owner.path.exists():
                raise DesktopOperationError("desktop_inventory_required_owner_missing", f"required storage owner is missing: {owner.owner_id}")


class BackupManager:
    def __init__(self, config: LocalDesktopConfig, *, control_plane: ApplicationControlPlane | None = None, inventory_resolver: StorageInventoryResolver | None = None, release_manager: ReleaseManager | None = None, lifecycle_status: Callable[[], str] | None = None) -> None:
        self.config = config
        self.control_plane = control_plane or ApplicationControlPlane(config)
        self.inventory_resolver = inventory_resolver or StorageInventoryResolver(config)
        self.release_manager = release_manager or ReleaseManager(config)
        self.lifecycle_status = lifecycle_status or self._instance_status

    def create(self, *, destination: str | Path | None = None) -> OperationResult:
        if self.lifecycle_status() not in {"STOPPED", "FAILED"}:
            raise DesktopOperationError("desktop_backup_requires_stopped_instance", "backup requires the local desktop instance to be stopped and quiescent")
        inventory = self.inventory_resolver.resolve()
        release_identity = self._release_identity()
        backup_id = f"backup-{utc_timestamp().replace(':', '').replace('-', '')}-{uuid4().hex[:8]}"
        target = Path(destination).expanduser() if destination else self.config.backup_root / f"{backup_id}.zip"
        if not target.is_absolute():
            target = self.config.project_root / target
        target = target.resolve(strict=False)
        if target.exists():
            raise DesktopOperationError("desktop_backup_exists", "backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        snapshots_root: Path | None = None
        temp_target = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        admission_was_open = False
        try:
            admission_was_open = bool(
                self.control_plane.status().get("admission_open", True)
            )
            self.config.runtime_root.mkdir(parents=True, exist_ok=True)
            snapshots_root = Path(
                tempfile.mkdtemp(
                    prefix="desktop-backup-",
                    dir=self.config.runtime_root,
                )
            )
            protect_private_path(snapshots_root)
            drain = self.control_plane.begin_drain(timeout_seconds=30.0)
            if int(drain.get("active_count", 0)) != 0:
                raise DesktopOperationError("desktop_backup_writer_detected", "application writers did not drain before backup")
            files: list[BackupFile] = []
            total_bytes = 0
            for owner in inventory.owners:
                if not owner.path.exists():
                    continue
                owner_snapshot = snapshots_root / owner.owner_id
                self._snapshot_owner(owner, owner_snapshot)
                for source, relative in _iter_snapshot_files(owner_snapshot):
                    size = source.stat().st_size
                    if size > MAX_SINGLE_FILE_BYTES:
                        raise DesktopOperationError("desktop_backup_file_too_large", f"backup item exceeds the per-file limit: {owner.owner_id}/{relative}")
                    data = source.read_bytes()
                    files.append(BackupFile(owner.owner_id, relative, size, sha256(data).hexdigest()))
                    total_bytes += size
                    if len(files) > MAX_BACKUP_FILES or total_bytes > MAX_BACKUP_BYTES:
                        raise DesktopOperationError("desktop_backup_limit_exceeded", "backup exceeds configured file or byte limits")
            requirements = sorted({item for owner in inventory.owners for item in owner.external_requirements})
            if self.inventory_resolver.environ.get("NEWSROOM_PAPERS_DATA_PATH"):
                requirements.append("rebuildable public paper cache")
            manifest = BackupManifest(backup_id, utc_timestamp(), release_identity, inventory, tuple(files), tuple(sorted(_EXCLUDED_NAMES | {f"*{suffix}" for suffix in _EXCLUDED_SUFFIXES})), tuple(requirements))
            temp_target.touch(exist_ok=False)
            protect_private_path(temp_target)
            with zipfile.ZipFile(temp_target, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                for item in files:
                    archive.write(snapshots_root / item.owner_id / Path(item.path), arcname=f"data/{item.owner_id}/{item.path}")
                archive.writestr("manifest.json", json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            protect_private_path(temp_target)
            os.replace(temp_target, target)
            protect_private_path(target)
            return OperationResult(True, "backup", "COMPLETE", "desktop_backup_created", "consistent local desktop backup created", {"backup_path": str(target), "backup_id": backup_id, "file_count": len(files), "total_bytes": manifest.total_bytes, "external_requirements": requirements})
        except Exception:
            if temp_target.exists():
                temp_target.unlink()
            raise
        finally:
            if snapshots_root is not None:
                shutil.rmtree(snapshots_root, ignore_errors=True)
            if admission_was_open:
                self.control_plane.resume_admission()

    def _snapshot_owner(self, owner: StorageOwner, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        if owner.kind == "file":
            self._snapshot_file(owner.path, destination / "value", consistency=owner.consistency)
            return
        reject_reparse_path(owner.path, root=owner.path)
        with ExitStack() as locks:
            for lock_path in _owner_lock_paths(owner):
                locks.enter_context(exclusive_lock(lock_path, timeout_seconds=0.1))
            before = _tree_signature(owner.path)
            for path in sorted(owner.path.rglob("*")):
                relative = path.relative_to(owner.path)
                if _excluded(relative, path):
                    continue
                if path.is_symlink() or _is_reparse_point(path):
                    raise DesktopOperationError("desktop_backup_reparse_point", f"storage owner contains a link or junction: {owner.owner_id}/{relative.as_posix()}")
                if path.is_dir():
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                self._snapshot_file(path, target, consistency="sqlite" if _is_sqlite(path) else "file")
            after = _tree_signature(owner.path)
            if before != after:
                raise DesktopOperationError("desktop_backup_writer_detected", f"storage owner changed while being copied: {owner.owner_id}")

    def _snapshot_file(self, source: Path, destination: Path, *, consistency: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if consistency == "sqlite" or _is_sqlite(source):
            _sqlite_snapshot(source, destination)
            return
        lock = Path(f"{source}.lock")
        with exclusive_lock(lock, timeout_seconds=0.1):
            before = source.stat()
            before_digest = _file_digest(source)
            shutil.copy2(source, destination)
            after = source.stat()
            after_digest = _file_digest(source)
            copied_digest = _file_digest(destination)
            if (before.st_size, before.st_mtime_ns, before_digest) != (after.st_size, after.st_mtime_ns, after_digest) or copied_digest != before_digest:
                raise DesktopOperationError("desktop_backup_writer_detected", f"storage file changed while being copied: {source.name}")

    def _release_identity(self) -> dict[str, Any]:
        try:
            _, manifest = self.release_manager.active()
            return dict(manifest.get("identity", {}))
        except DesktopOperationError:
            return {"release_id": "unavailable", "source_revision": "unavailable"}

    def _instance_status(self) -> str:
        return str(read_json_object(self.config.runtime_root / "instance.json", missing={"status": "STOPPED"}).get("status") or "STOPPED")


class RestoreManager:
    def __init__(self, config: LocalDesktopConfig, *, control_plane: ApplicationControlPlane | None = None, release_manager: ReleaseManager | None = None, inventory_resolver: StorageInventoryResolver | None = None, lifecycle_status: Callable[[], str] | None = None, validator: Callable[[StorageInventory, Mapping[str, Path]], dict[str, Any]] | None = None) -> None:
        self.config = config
        self.control_plane = control_plane or ApplicationControlPlane(config)
        self.release_manager = release_manager or ReleaseManager(config)
        self.inventory_resolver = inventory_resolver or StorageInventoryResolver(config)
        self.lifecycle_status = lifecycle_status or self._instance_status
        self.validator = validator or _validate_restored_owners

    def restore(self, backup: str | Path, *, target_root: str | Path | None = None, activate: bool = False, _schema_compatibility: Mapping[str, Any] | None = None) -> OperationResult:
        try:
            source = Path(backup).expanduser().resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise DesktopOperationError("desktop_backup_missing", "backup archive does not exist") from exc
        if not source.is_file():
            raise DesktopOperationError("desktop_backup_missing", "backup archive does not exist")
        target = Path(target_root).expanduser() if target_root else self.config.backup_root / "restores" / f"restore-{uuid4().hex}"
        if not target.is_absolute():
            target = self.config.project_root / target
        target = target.resolve(strict=False)
        if target == Path(target.anchor):
            raise DesktopOperationError("desktop_restore_target_unsafe", "restore target cannot be a filesystem root")
        expected_inventory = self.inventory_resolver.resolve(require_existing=False)
        for owner in expected_inventory.owners:
            original = owner.path.resolve(strict=False)
            if target == original or target.is_relative_to(original) or original.is_relative_to(target):
                raise DesktopOperationError("desktop_restore_target_overlaps_storage", "restore target must be isolated from every original storage root")
        if target.exists() and any(target.iterdir()):
            raise DesktopOperationError("desktop_restore_target_not_empty", "restore target must be absent or empty")
        target.mkdir(parents=True, exist_ok=True)
        reject_reparse_path(target, root=target)
        protect_private_path(target)
        manifest, inventory, owner_paths = self._extract_and_validate(source, target, schema_compatibility=_schema_compatibility)
        relocation_count = _rewrite_path_references(
            owner_paths,
            _owner_path_mapping(inventory, owner_paths, use_backup_sources=True),
        )
        validation = self.validator(inventory, owner_paths)
        _invalidate_staged_sessions(owner_paths.get("auth_sessions"))
        isolation = validation.get("application_isolation", {}) if isinstance(validation, Mapping) else {}
        if not isolation.get("verified"):
            raise DesktopOperationError("desktop_restore_application_validation_missing", "restored storage was not read through an isolated application store")
        restore_report = {
            "schema": RESTORE_SCHEMA,
            "backup_id": manifest["backup_id"],
            "validated_at": utc_timestamp(),
            "source_backup": str(source),
            "target_root": str(target),
            "owner_paths": {owner: str(path) for owner, path in owner_paths.items()},
            "validation": validation,
            "application_isolation": isolation,
            "relocated_reference_count": relocation_count,
            "activation": "pending" if activate else "not_requested",
        }
        atomic_write_json(target / "restore-report.json", restore_report)
        activation = None
        if activate:
            admission_was_open = bool(self.control_plane.status().get("admission_open", True))
            try:
                drain = self.control_plane.begin_drain(timeout_seconds=30.0)
            except Exception:
                if admission_was_open:
                    self.control_plane.resume_admission()
                raise
            if int(drain.get("active_count", 0)) != 0:
                if admission_was_open:
                    self.control_plane.resume_admission()
                raise DesktopOperationError("desktop_restore_writer_detected", "application writers did not drain before restore activation")
            try:
                activation = self._activate(inventory, owner_paths, target, backup=source)
                restore_report["activation"] = "complete"
                restore_report["generation"] = activation["generation"]
                atomic_write_json(target / "restore-report.json", restore_report)
            finally:
                if admission_was_open:
                    self.control_plane.resume_admission()
        return OperationResult(True, "restore", "ACTIVATED" if activate else "VERIFIED", "desktop_restore_activated" if activate else "desktop_restore_verified", "backup restored and validated in an isolated root", {"restore_root": str(target), "backup_id": manifest["backup_id"], "validation": validation, "activation": activation})

    def _extract_and_validate(self, source: Path, target: Path, *, schema_compatibility: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], StorageInventory, dict[str, Path]]:
        with zipfile.ZipFile(source, "r") as archive:
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
                raise DesktopOperationError("desktop_backup_manifest_invalid", "backup manifest is missing or invalid") from exc
            inventory = _inventory_from_manifest(manifest, self.inventory_resolver.resolve(require_existing=False), allowed_schemas=schema_compatibility)
            files = _files_from_manifest(manifest)
            known_owner_ids = {owner.owner_id for owner in inventory.owners}
            unknown_file_owners = sorted({item.owner_id for item in files} - known_owner_ids)
            if unknown_file_owners:
                raise DesktopOperationError("desktop_backup_owner_unknown", "backup files reference an unknown storage owner", details={"owners": unknown_file_owners})
            if len(files) > MAX_BACKUP_FILES or sum(item.size for item in files) > MAX_BACKUP_BYTES:
                raise DesktopOperationError("desktop_restore_limit_exceeded", "backup exceeds restore limits")
            expected_members = {f"data/{item.owner_id}/{item.path}": item for item in files}
            archive_members = {item.filename: item for item in archive.infolist() if item.filename != "manifest.json" and not item.is_dir()}
            if set(archive_members) != set(expected_members):
                raise DesktopOperationError("desktop_backup_inventory_mismatch", "archive members do not match the manifest")
            owner_paths = {owner.owner_id: target / "owners" / owner.owner_id / ("value" if owner.kind == "file" else "") for owner in inventory.owners}
            for owner in inventory.owners:
                if owner.kind == "directory":
                    owner_paths[owner.owner_id].mkdir(parents=True, exist_ok=True)
            for name, entry in expected_members.items():
                info = archive_members[name]
                _validate_zip_member(info)
                if info.file_size != entry.size or entry.size > MAX_SINGLE_FILE_BYTES:
                    raise DesktopOperationError("desktop_backup_size_mismatch", f"backup member size mismatch: {name}")
                destination_root = target / "owners" / entry.owner_id
                destination = ensure_contained(destination_root, destination_root / Path(entry.path), field="restore member")
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256()
                written = 0
                with archive.open(info, "r") as reader, destination.open("xb") as writer:
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > entry.size:
                            raise DesktopOperationError("desktop_backup_size_mismatch", f"backup member expanded beyond declared size: {name}")
                        digest.update(chunk)
                        writer.write(chunk)
                if written != entry.size or digest.hexdigest() != entry.sha256:
                    destination.unlink(missing_ok=True)
                    raise DesktopOperationError("desktop_backup_checksum_mismatch", f"backup member checksum mismatch: {name}")
                reject_reparse_path(destination, root=target)
            return manifest, inventory, owner_paths

    def _activate(self, inventory: StorageInventory, owner_paths: Mapping[str, Path], restore_root: Path, *, backup: Path) -> dict[str, Any]:
        if self.lifecycle_status() not in {"STOPPED", "FAILED"}:
            raise DesktopOperationError("desktop_restore_requires_stopped_instance", "restore activation requires the local desktop instance to be stopped")
        journal_path = self.config.runtime_root / "restore-activation.json"
        if journal_path.exists():
            existing = read_json_object(journal_path)
            if existing.get("status") not in {"complete", "rolled_back"}:
                self._recover_activation_journal(existing, journal_path)
        transaction = uuid4().hex
        operations: list[dict[str, Any]] = []
        owners_by_id = {owner.owner_id: owner for owner in inventory.owners}
        for owner in inventory.owners:
            staged = owner_paths[owner.owner_id]
            destination = owner.path
            prepared = destination.parent / f".{destination.name}.restore-{transaction}"
            rollback = destination.parent / f".{destination.name}.rollback-{transaction}"
            failed = destination.parent / f".{destination.name}.failed-{transaction}"
            if prepared.exists() or rollback.exists() or failed.exists():
                raise DesktopOperationError("desktop_activation_path_exists", "owned restore preparation, rollback, or failure path already exists")
            if not staged.exists() and owner.required:
                raise DesktopOperationError("desktop_restore_required_owner_missing", f"required restored owner is missing: {owner.owner_id}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            reject_reparse_path(destination.parent, root=destination.parent)
            operations.append({"owner_id": owner.owner_id, "destination": str(destination), "prepared": str(prepared) if staged.exists() else None, "rollback": str(rollback), "failed": str(failed), "ownership_marker": _activation_ownership_marker(transaction, owner.owner_id, destination), "had_destination": destination.exists(), "had_prepared": staged.exists(), "prepared_ready": False, "state": "preparing"})
        journal = {"schema": ACTIVATION_SCHEMA, "transaction": transaction, "status": "preparing", "created_at": utc_timestamp(), "backup": str(backup), "restore_root": str(restore_root), "operations": operations}
        atomic_write_json(journal_path, journal)
        try:
            for operation in operations:
                if operation["prepared"] is not None:
                    staged = owner_paths[str(operation["owner_id"])]
                    prepared = Path(str(operation["prepared"]))
                    owner = owners_by_id[str(operation["owner_id"])]
                    if owner.kind == "file":
                        prepared.touch(exist_ok=False)
                        protect_private_path(prepared)
                        shutil.copy2(staged, prepared)
                    else:
                        prepared.mkdir()
                        protect_private_path(prepared)
                        shutil.copytree(staged, prepared, dirs_exist_ok=True)
                    operation["prepared_ready"] = True
                operation["state"] = "prepared"
                atomic_write_json(journal_path, journal)
            prepared_paths = {operation["owner_id"]: Path(operation["prepared"]) for operation in operations if operation["prepared"]}
            destination_paths = {owner.owner_id: owner.path for owner in inventory.owners}
            _rewrite_path_references(prepared_paths, _owner_path_mapping(inventory, destination_paths, use_backup_sources=False, staged_paths=owner_paths))
            journal["status"] = "activating"
            atomic_write_json(journal_path, journal)
            for operation in operations:
                destination = Path(str(operation["destination"]))
                prepared = Path(str(operation["prepared"])) if operation["prepared"] else None
                rollback = Path(str(operation["rollback"]))
                if destination.exists():
                    os.replace(destination, rollback)
                    operation["state"] = "old_moved"
                    atomic_write_json(journal_path, journal)
                if prepared is not None:
                    os.replace(prepared, destination)
                operation["state"] = "activated"
                atomic_write_json(journal_path, journal)
            journal["status"] = "rotating_generation"
            atomic_write_json(journal_path, journal)
            rotation = self.control_plane.rotate_generation(reason=f"restore:{transaction}")
            journal["generation"] = rotation["generation"]
            journal["status"] = "complete"
            journal["completed_at"] = utc_timestamp()
            atomic_write_json(journal_path, journal)
            return {"transaction": transaction, "generation": rotation["generation"], "journal": str(journal_path), "rollback_paths": {operation["owner_id"]: operation["rollback"] for operation in operations}}
        except Exception:
            self._recover_activation_journal(journal, journal_path)
            raise

    def _recover_activation_journal(self, journal: dict[str, Any], journal_path: Path) -> None:
        transaction, operations = self._validated_activation_journal(journal)
        journal["status"] = "rolling_back"
        atomic_write_json(journal_path, journal)
        changed = False
        for operation, destination, prepared, rollback, failed in reversed(operations):
            if rollback.exists():
                if destination.exists():
                    if failed.exists():
                        raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation recovery path combination is ambiguous")
                    os.replace(destination, failed)
                    changed = True
                if prepared is not None and prepared.exists():
                    _remove_activation_path(prepared)
                os.replace(rollback, destination)
                changed = True
            elif bool(operation["had_destination"]):
                if failed.exists():
                    changed = True
                if prepared is not None and prepared.exists():
                    _remove_activation_path(prepared)
            else:
                if destination.exists():
                    if failed.exists():
                        raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation recovery path combination is ambiguous")
                    os.replace(destination, failed)
                    changed = True
                elif failed.exists():
                    changed = True
                if prepared is not None and prepared.exists():
                    _remove_activation_path(prepared)
            operation["state"] = "rolled_back"
            atomic_write_json(journal_path, journal)
        rotation = self.control_plane.rotate_generation(reason=f"restore-recovery:{transaction}") if changed else None
        journal["status"] = "rolled_back"
        journal["recovered_at"] = utc_timestamp()
        journal["recovery_generation"] = rotation["generation"] if rotation else None
        atomic_write_json(journal_path, journal)

    def _validated_activation_journal(self, journal: Mapping[str, Any]) -> tuple[str, list[tuple[dict[str, Any], Path, Path | None, Path, Path]]]:
        transaction = str(journal.get("transaction") or "")
        raw_operations = journal.get("operations")
        if journal.get("schema") != ACTIVATION_SCHEMA or journal.get("status") not in {"preparing", "prepared", "activating", "rotating_generation", "rolling_back"} or len(transaction) != 32 or any(character not in "0123456789abcdef" for character in transaction) or not isinstance(raw_operations, list):
            raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal is invalid")
        trusted = {owner.owner_id: owner.path for owner in self.inventory_resolver.resolve(require_existing=False).owners}
        if len(raw_operations) != len(trusted):
            raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal does not contain the complete storage inventory")
        validated: list[tuple[dict[str, Any], Path, Path | None, Path, Path]] = []
        seen: set[str] = set()
        for raw in raw_operations:
            if not isinstance(raw, dict):
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal is invalid")
            owner_id = str(raw.get("owner_id") or "")
            if owner_id in seen or owner_id not in trusted:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal contains an unknown or duplicate owner")
            seen.add(owner_id)
            destination = _lexical_absolute_path(raw.get("destination"), field="activation destination")
            trusted_destination = _lexical_absolute_path(str(trusted[owner_id]), field="trusted activation destination")
            if os.path.normcase(str(destination)) != os.path.normcase(str(trusted_destination)):
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal targets an unowned path")
            expected_prepared = destination.parent / f".{destination.name}.restore-{transaction}"
            expected_rollback = destination.parent / f".{destination.name}.rollback-{transaction}"
            expected_failed = destination.parent / f".{destination.name}.failed-{transaction}"
            prepared_raw = raw.get("prepared")
            prepared = _lexical_absolute_path(prepared_raw, field="activation prepared path") if prepared_raw is not None else None
            rollback = _lexical_absolute_path(raw.get("rollback"), field="activation rollback path")
            failed = _lexical_absolute_path(raw.get("failed"), field="activation failed path")
            if (prepared is not None and os.path.normcase(str(prepared)) != os.path.normcase(str(expected_prepared))) or os.path.normcase(str(rollback)) != os.path.normcase(str(expected_rollback)) or os.path.normcase(str(failed)) != os.path.normcase(str(expected_failed)):
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal contains a path not bound to its transaction")
            if raw.get("ownership_marker") != _activation_ownership_marker(transaction, owner_id, destination):
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal ownership marker is invalid")
            had_destination = raw.get("had_destination")
            had_prepared = raw.get("had_prepared")
            prepared_ready = raw.get("prepared_ready")
            if not isinstance(had_destination, bool) or not isinstance(had_prepared, bool) or not isinstance(prepared_ready, bool) or had_prepared != (prepared is not None):
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal presence metadata is invalid")
            if str(raw.get("state") or "") not in {"preparing", "prepared", "old_moved", "activated", "rolled_back"}:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal state is invalid")
            reject_reparse_path(destination.parent, root=destination.parent)
            for existing in (destination, prepared, rollback, failed):
                if existing is not None and existing.exists():
                    reject_reparse_path(existing, root=existing)
            destination_exists = destination.exists()
            prepared_exists = prepared is not None and prepared.exists()
            rollback_exists = rollback.exists()
            failed_exists = failed.exists()
            if rollback_exists and not had_destination:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation rollback presence is inconsistent")
            if had_destination and not destination_exists and not rollback_exists:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation lost both current and rollback data")
            if destination_exists and prepared_exists and rollback_exists:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation path combination is ambiguous")
            if failed_exists and destination_exists and rollback_exists:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation recovery path combination is ambiguous")
            if failed_exists and destination_exists and not had_destination:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation recovery ownership is inconsistent")
            if not had_destination and destination_exists and prepared_exists:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation path combination is ambiguous")
            if prepared_ready and had_prepared and not prepared_exists and not destination_exists:
                raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation prepared data is missing")
            validated.append((raw, destination, prepared, rollback, failed))
        if seen != set(trusted):
            raise DesktopOperationError("desktop_activation_journal_invalid", "restore activation journal does not contain the complete storage inventory")
        return transaction, validated
    def rollback_release(self, release: str | Path, *, compatible_backup: str | Path | None = None) -> OperationResult:
        if self.lifecycle_status() not in {"STOPPED", "FAILED"}:
            raise DesktopOperationError("desktop_rollback_requires_stopped_instance", "release rollback requires the local desktop instance to be stopped")
        target_path = self.release_manager.resolve_release(release)
        target_manifest = self.release_manager.verify(target_path)
        compatibility = target_manifest.get("data_compatibility", {}).get("readable_schemas", {})
        if not isinstance(compatibility, Mapping):
            raise DesktopOperationError("desktop_release_manifest_invalid", "target release data compatibility is invalid")
        current_inventory = self.inventory_resolver.resolve()
        incompatible = _incompatible_schemas(current_inventory, compatibility)
        restore_result: OperationResult | None = None
        activation: dict[str, Any] | None = None
        if incompatible:
            if compatible_backup is None:
                raise DesktopOperationError("desktop_release_data_incompatible", "target release cannot read the current data schemas", details={"incompatible": incompatible})
            preflight_root = self.config.backup_root / "restores" / f"rollback-{uuid4().hex}"
            restore_result = self.restore(compatible_backup, target_root=preflight_root, activate=False, _schema_compatibility=compatibility)
            backup_source = Path(compatible_backup).expanduser().resolve(strict=True)
            with zipfile.ZipFile(backup_source, "r") as archive:
                try:
                    backup_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
                    raise DesktopOperationError("desktop_backup_manifest_invalid", "compatible rollback backup manifest is invalid") from exc
            restored_inventory = _inventory_from_manifest(backup_manifest, self.inventory_resolver.resolve(require_existing=False), allowed_schemas=compatibility)
            staged_root = Path(str(restore_result.details["restore_root"]))
            owner_paths = {owner.owner_id: staged_root / "owners" / owner.owner_id / ("value" if owner.kind == "file" else "") for owner in restored_inventory.owners}
            self.validator(restored_inventory, owner_paths)
            activation = self._activate(restored_inventory, owner_paths, staged_root, backup=backup_source)
        try:
            pointer = self.release_manager.activate(target_path)
        except Exception:
            if activation is not None:
                journal_path = self.config.runtime_root / "restore-activation.json"
                journal = read_json_object(journal_path)
                journal["status"] = "rolling_back"
                atomic_write_json(journal_path, journal)
                self._recover_activation_journal(journal, journal_path)
            raise
        return OperationResult(True, "rollback", "ACTIVATED", "desktop_release_rolled_back", "compatible verified release activated", {"release": pointer, "data_restore": restore_result.to_dict() if restore_result else None, "activation": activation})
    def _instance_status(self) -> str:
        return str(read_json_object(self.config.runtime_root / "instance.json", missing={"status": "STOPPED"}).get("status") or "STOPPED")


def _activation_ownership_marker(transaction: str, owner_id: str, destination: Path) -> str:
    payload = f"{ACTIVATION_SCHEMA}\0{transaction}\0{owner_id}\0{os.path.normcase(str(destination))}"
    return sha256(payload.encode("utf-8")).hexdigest()

def _lexical_absolute_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise DesktopOperationError("desktop_activation_journal_invalid", f"{field} is invalid")
    path = Path(value).expanduser()
    if not path.is_absolute() or path == Path(path.anchor):
        raise DesktopOperationError("desktop_activation_journal_invalid", f"{field} is not an owned absolute path")
    return Path(os.path.abspath(os.path.normpath(str(path))))


def _remove_activation_path(path: Path) -> None:
    reject_reparse_path(path, root=path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()

def _incompatible_schemas(inventory: StorageInventory, readable_schemas: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        owner.owner_id: {"current": owner.schema, "supported": readable_schemas.get(owner.owner_id)}
        for owner in inventory.owners
        if owner.required and readable_schemas.get(owner.owner_id) != owner.schema
    }


def _inventory_from_manifest(manifest: Mapping[str, Any], expected: StorageInventory, *, allowed_schemas: Mapping[str, Any] | None = None) -> StorageInventory:
    if manifest.get("schema") != BACKUP_SCHEMA:
        raise DesktopOperationError("desktop_backup_schema_unsupported", "backup schema is unsupported")
    raw_inventory = manifest.get("inventory")
    if not isinstance(raw_inventory, Mapping) or raw_inventory.get("schema") != INVENTORY_SCHEMA:
        raise DesktopOperationError("desktop_inventory_schema_unsupported", "backup storage inventory schema is unsupported")
    expected_by_id = {owner.owner_id: owner for owner in expected.owners}
    owners: list[StorageOwner] = []
    seen: set[str] = set()
    for raw in raw_inventory.get("owners", []):
        if not isinstance(raw, Mapping):
            raise DesktopOperationError("desktop_backup_manifest_invalid", "storage inventory owner is invalid")
        owner_id = str(raw.get("owner_id") or "")
        if owner_id in seen or owner_id not in expected_by_id:
            raise DesktopOperationError("desktop_backup_owner_unknown", "backup contains an unknown or duplicate storage owner")
        seen.add(owner_id)
        current = expected_by_id[owner_id]
        raw_schema = str(raw.get("schema") or "")
        supported_schema = allowed_schemas.get(owner_id) if allowed_schemas is not None else current.schema
        if isinstance(supported_schema, (list, tuple, set, frozenset)):
            schema_supported = raw_schema in {str(item) for item in supported_schema}
        else:
            schema_supported = raw_schema == str(supported_schema or "")
        if str(raw.get("kind")) != current.kind or not schema_supported:
            raise DesktopOperationError("desktop_backup_owner_incompatible", f"backup owner contract is incompatible: {owner_id}")
        raw_source = Path(str(raw.get("source_path") or "")).expanduser()
        if not raw_source.is_absolute() or raw_source == Path(raw_source.anchor):
            raise DesktopOperationError("desktop_backup_manifest_invalid", f"backup owner source path is invalid: {owner_id}")
        owners.append(replace(current, schema=raw_schema, backup_source_path=raw_source.resolve(strict=False)))
    missing = [owner.owner_id for owner in expected.owners if owner.required and owner.owner_id not in seen]
    if missing:
        raise DesktopOperationError("desktop_backup_required_owner_missing", "backup omits required storage owners", details={"owners": missing})
    return StorageInventory(tuple(owners))

def _files_from_manifest(manifest: Mapping[str, Any]) -> tuple[BackupFile, ...]:
    result: list[BackupFile] = []
    seen: set[tuple[str, str]] = set()
    for raw in manifest.get("files", []):
        if not isinstance(raw, Mapping):
            raise DesktopOperationError("desktop_backup_manifest_invalid", "backup file entry is invalid")
        owner_id = str(raw.get("owner_id") or "")
        relative = _safe_archive_relative(raw.get("path"))
        key = (owner_id, relative)
        if key in seen:
            raise DesktopOperationError("desktop_backup_manifest_invalid", "backup contains duplicate paths")
        seen.add(key)
        try:
            size = int(raw["size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DesktopOperationError("desktop_backup_manifest_invalid", "backup file size is invalid") from exc
        digest = str(raw.get("sha256") or "")
        if size < 0 or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise DesktopOperationError("desktop_backup_manifest_invalid", "backup file checksum is invalid")
        result.append(BackupFile(owner_id, relative, size, digest, str(raw.get("file_type") or "file")))
    return tuple(result)


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    _safe_archive_relative(info.filename)
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise DesktopOperationError("desktop_restore_link_rejected", f"backup contains a symbolic link: {info.filename}")
    windows_attributes = info.external_attr & 0xFFFF
    if windows_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise DesktopOperationError("desktop_restore_link_rejected", f"backup contains a reparse point: {info.filename}")


def _safe_archive_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DesktopOperationError("desktop_restore_path_invalid", "backup path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or not _safe_windows_path_segment(part) for part in path.parts):
        raise DesktopOperationError("desktop_restore_path_escape", "backup path escapes the restore root")
    return path.as_posix()


def _owner_path_mapping(
    inventory: StorageInventory,
    target_paths: Mapping[str, Path],
    *,
    use_backup_sources: bool,
    staged_paths: Mapping[str, Path] | None = None,
) -> tuple[tuple[Path, Path, str], ...]:
    mappings: list[tuple[Path, Path, str]] = []
    for owner in inventory.owners:
        target = target_paths.get(owner.owner_id)
        if target is None:
            continue
        sources = [staged_paths[owner.owner_id]] if staged_paths is not None else []
        sources.append((owner.backup_source_path if use_backup_sources else None) or owner.path)
        sources.append(owner.path)
        for source in sources:
            mappings.append((source.resolve(strict=False), target.resolve(strict=False), owner.kind))
    return tuple(mappings)


def _rewrite_path_references(owner_paths: Mapping[str, Path], mappings: Iterable[tuple[Path, Path, str]]) -> int:
    replacements = tuple(mappings)
    changed = 0
    for owner_id, owner_root in owner_paths.items():
        if owner_id != "research_imports":
            continue
        files = [owner_root] if owner_root.is_file() else list(owner_root.rglob("*.json")) if owner_root.is_dir() else []
        for file in files:
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            rewritten, count = _rewrite_value(payload, replacements)
            if count:
                atomic_write_json(file, rewritten)
                changed += count
    return changed


def _rewrite_value(value: Any, mappings: tuple[tuple[Path, Path, str], ...], *, field_name: str | None = None) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            result[str(key)], item_count = _rewrite_value(item, mappings, field_name=str(key))
            count += item_count
        return result, count
    if isinstance(value, list):
        result_list: list[Any] = []
        count = 0
        for item in value:
            rewritten, item_count = _rewrite_value(item, mappings, field_name=field_name)
            result_list.append(rewritten)
            count += item_count
        return result_list, count
    if isinstance(value, str) and field_name is not None:
        normalized = field_name.casefold()
        if normalized == "path" or normalized.endswith("_path") or normalized.endswith("path_ref"):
            candidate = _absolute_reference_path(value)
            if candidate is not None:
                mapped = _relocated_reference(candidate, mappings)
                if mapped is None:
                    raise DesktopOperationError("desktop_restore_absolute_reference_unsupported", "restored data contains an absolute path reference outside its owned storage inventory")
                return str(mapped), 1
    return value, 0


def _validate_restored_owners(inventory: StorageInventory, owner_paths: Mapping[str, Path]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    application_receipts: dict[str, dict[str, Any]] = {}
    relocation_roots: list[tuple[Path, Path, str]] = []
    for item in inventory.owners:
        staged = owner_paths[item.owner_id]
        for source in {item.path.resolve(strict=False), (item.backup_source_path or item.path).resolve(strict=False)}:
            relocation_roots.append((source, staged, item.kind))
        relocation_roots.append((staged.resolve(strict=False), staged.resolve(strict=False), item.kind))
    for owner in inventory.owners:
        restored = owner_paths[owner.owner_id]
        if owner.required and not restored.exists():
            raise DesktopOperationError("desktop_restore_required_owner_missing", f"required restored owner is missing: {owner.owner_id}")
        if not restored.exists():
            results[owner.owner_id] = {"status": "absent_optional"}
            continue
        reject_reparse_path(restored, root=restored if restored.is_dir() else restored.parent)
        files = [restored] if restored.is_file() else [item for item in restored.rglob("*") if item.is_file() and not _excluded(item.relative_to(restored), item)]
        relocated_references = 0
        if owner.owner_id == "auth_users":
            application_receipts[owner.owner_id] = _validate_auth_users(restored)
        elif owner.owner_id == "auth_sessions":
            application_receipts[owner.owner_id] = _validate_auth_sessions(restored)
        elif owner.owner_id == "auth_otp":
            application_receipts[owner.owner_id] = _validate_auth_transaction(restored, collection="challenges", schema=owner.schema)
        elif owner.owner_id == "auth_oauth":
            application_receipts[owner.owner_id] = _validate_auth_transaction(restored, collection="states", schema=owner.schema)
        elif owner.owner_id == "research_history":
            application_receipts[owner.owner_id] = _validate_research_history(restored)
        elif owner.owner_id == "research_imports":
            application_receipts[owner.owner_id] = _validate_research_imports(restored)
        elif owner.owner_id == "runtime_artifacts":
            application_receipts[owner.owner_id] = _validate_runtime_artifacts(restored, run_store_root=owner_paths.get("research_run_store"))
        elif owner.owner_id == "research_run_store":
            application_receipts[owner.owner_id] = _validate_research_run_store(restored)
        elif owner.owner_id == "research_rag_local":
            application_receipts[owner.owner_id] = _validate_local_chunk_store(restored)
        elif owner.owner_id == "research_rag_transcripts":
            application_receipts[owner.owner_id] = _validate_paper_rag_transcript_store(restored)
        elif owner.owner_id == "research_paper_catalog":
            application_receipts[owner.owner_id] = _validate_research_catalog_store(restored)
        elif owner.owner_id == "research_graph_results":
            application_receipts[owner.owner_id] = _validate_graph_result_store(restored)
        elif owner.owner_id == "research_harness_side_effects":
            application_receipts[owner.owner_id] = _validate_harness_side_effect_store(restored)
        elif owner.owner_id == "research_harness_node_output":
            application_receipts[owner.owner_id] = _validate_harness_node_output_store(restored)
        for file in files:
            if _is_sqlite(file):
                _validate_sqlite(file, required_tables=_required_sqlite_tables(owner.owner_id))
                _validate_sqlite_path_columns(file)
                continue
            if file.suffix.casefold() != ".json" and owner.kind != "file":
                continue
            try:
                payload = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise DesktopOperationError("desktop_restore_json_invalid", f"restored JSON is invalid: {owner.owner_id}") from exc
            if owner.kind == "file":
                actual_schema = payload.get("schemaVersion") or payload.get("schema_version") or payload.get("schema") if isinstance(payload, Mapping) else None
                if actual_schema != owner.schema:
                    raise DesktopOperationError("desktop_restore_schema_mismatch", f"restored owner schema is incompatible: {owner.owner_id}")
            for reference in _walk_path_references(payload):
                candidate = _absolute_reference_path(reference)
                if candidate is None:
                    continue
                if owner.owner_id != "research_imports":
                    raise DesktopOperationError("desktop_restore_absolute_reference_unsupported", f"checksum-protected restored owner contains an absolute reference that cannot be rewritten: {owner.owner_id}")
                mapped = _relocated_reference(candidate, relocation_roots)
                if mapped is None or not mapped.exists():
                    raise DesktopOperationError("desktop_restore_absolute_reference_unsupported", f"restored owner contains an absolute reference that cannot be safely relocated: {owner.owner_id}")
                relocated_references += 1
            _validate_declared_file_checksums(payload, file.parent)
        results[owner.owner_id] = {
            "status": "verified",
            "schema": owner.schema,
            "file_count": len(files),
            "isolated_path": str(restored),
            "relocated_reference_count": relocated_references,
            **({"application_validation": _public_validation_receipt(application_receipts[owner.owner_id])} if owner.owner_id in application_receipts else {}),
        }
    reference_closure = _validate_application_reference_closure(owner_paths, application_receipts)
    isolated_owner_ids = sorted(application_receipts)
    present_application_owners = sorted(
        owner.owner_id
        for owner in inventory.owners
        if owner_paths[owner.owner_id].exists() and owner.owner_id in _APPLICATION_VALIDATED_OWNERS
    )
    missing_application_validation = sorted(set(present_application_owners) - set(isolated_owner_ids))
    if missing_application_validation:
        raise DesktopOperationError("desktop_restore_application_validation_missing", "restored application owner was not read through its isolated adapter")
    for owner_id, receipt in application_receipts.items():
        configured = Path(str(receipt["configured_root"])).resolve(strict=False)
        staged = owner_paths[owner_id].resolve(strict=False)
        if configured != staged:
            raise DesktopOperationError("desktop_restore_application_root_mismatch", "restored application adapter was not configured to the isolated owner root")
        original = next(owner.path.resolve(strict=False) for owner in inventory.owners if owner.owner_id == owner_id)
        if configured == original or configured.is_relative_to(original):
            raise DesktopOperationError("desktop_restore_application_root_mismatch", "restored application adapter resolved an original storage root")
    return {
        "owners": results,
        "application_isolation": {
            "verified": bool(present_application_owners) and not missing_application_validation,
            "validated_owner_ids": isolated_owner_ids,
            "configured_roots": {owner_id: str(owner_paths[owner_id]) for owner_id in isolated_owner_ids},
            "reference_closure": reference_closure,
        },
    }



def _public_validation_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in receipt.items() if not str(key).startswith("_")}


def _validate_application_reference_closure(owner_paths: Mapping[str, Path], receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    auth_users = set(receipts.get("auth_users", {}).get("_user_ids", ()))
    identity_links = 0
    if "auth_users" in receipts:
        for owner_id in ("auth_sessions", "research_history", "research_imports", "research_run_store", "research_rag_transcripts"):
            referenced = {str(value) for value in receipts.get(owner_id, {}).get("_user_ids", ()) if str(value)}
            dangling = sorted(referenced - auth_users)
            if dangling:
                raise DesktopOperationError("desktop_restore_identity_reference_dangling", f"restored {owner_id} contains a user reference absent from the isolated auth store")
            identity_links += len(referenced)

    run_receipt = receipts.get("research_run_store", {})
    run_ids = set(run_receipt.get("_run_ids", ()))
    history_runs = set(receipts.get("research_history", {}).get("_run_ids", ()))
    rag_runs = set(receipts.get("research_rag_transcripts", {}).get("_run_ids", ()))
    dangling_runs = sorted((history_runs | rag_runs) - run_ids)
    if dangling_runs:
        raise DesktopOperationError("desktop_restore_run_reference_dangling", "restored workspace or RAG transcript references a run absent from the isolated Research run store")

    artifact_refs = tuple(str(value) for value in run_receipt.get("_artifact_refs", ()))
    artifact_root = owner_paths.get("runtime_artifacts")
    if artifact_refs and (artifact_root is None or not artifact_root.exists()):
        raise DesktopOperationError("desktop_restore_artifact_reference_dangling", "restored Research runs reference an absent isolated artifact store")
    if artifact_refs:
        from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort

        artifact_store = FilesystemHarnessArtifactPort(artifact_root)
        for ref in artifact_refs:
            parsed = urlsplit(ref)
            if parsed.scheme != "artifact" or not parsed.netloc:
                raise DesktopOperationError("desktop_restore_artifact_reference_invalid", "restored Research run contains a non-canonical artifact reference")
            try:
                artifact_store.verify_artifact_ref(ref, expected_run_id=parsed.netloc)
            except Exception as exc:
                raise DesktopOperationError("desktop_restore_artifact_reference_dangling", "restored Research run contains a dangling artifact reference") from exc

    catalog_receipt = receipts.get("research_paper_catalog", {})
    catalog_papers = set(catalog_receipt.get("_paper_ids", ()))
    catalog_documents = set(catalog_receipt.get("_document_ids", ()))
    catalog_revisions = set(catalog_receipt.get("_source_revisions", ()))
    imported_papers = set(receipts.get("research_imports", {}).get("_paper_ids", ()))
    imported_revisions = set(receipts.get("research_imports", {}).get("_source_revisions", ()))
    run_papers = set(run_receipt.get("_paper_ids", ()))
    if (imported_papers | run_papers) - catalog_papers:
        raise DesktopOperationError("desktop_restore_source_reference_dangling", "restored import or run references a paper absent from the isolated catalog")
    if imported_revisions - catalog_revisions:
        raise DesktopOperationError("desktop_restore_source_reference_dangling", "restored completed import references a source revision absent from the isolated catalog")

    chunk_receipt = receipts.get("research_rag_local", {})
    chunk_papers = set(chunk_receipt.get("_paper_ids", ()))
    if chunk_papers - catalog_papers:
        raise DesktopOperationError("desktop_restore_chunk_reference_dangling", "restored chunk index references a paper absent from the isolated catalog")
    chunk_documents = set(chunk_receipt.get("_document_ids", ()))
    if chunk_documents and chunk_documents - catalog_documents:
        raise DesktopOperationError("desktop_restore_chunk_reference_dangling", "restored chunk index references a document absent from the isolated catalog")
    return {
        "identity_link_count": identity_links,
        "workspace_run_reference_count": len(history_runs),
        "rag_run_reference_count": len(rag_runs),
        "run_artifact_reference_count": len(artifact_refs),
        "import_paper_reference_count": len(imported_papers),
        "import_source_revision_count": len(imported_revisions),
        "chunk_paper_reference_count": len(chunk_papers),
        "chunk_document_reference_count": len(chunk_documents),
    }

def _invalidate_staged_sessions(path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"schemaVersion": "newsroom_auth_sessions.v1", "sessions": []})


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    write_uri = f"file:{source.as_posix()}?mode=rw"
    read_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        guard = sqlite3.connect(write_uri, uri=True, timeout=0, isolation_level=None)
    except sqlite3.Error as exc:
        raise DesktopOperationError("desktop_sqlite_open_failed", f"could not open SQLite owner: {source.name}") from exc
    try:
        guard.execute("PRAGMA busy_timeout=0")
        guard.execute("BEGIN IMMEDIATE")
        source_db = sqlite3.connect(read_uri, uri=True, timeout=0, isolation_level=None)
        destination_db = sqlite3.connect(destination)
        try:
            source_db.backup(destination_db)
            integrity = destination_db.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise DesktopOperationError("desktop_sqlite_integrity_failed", f"SQLite snapshot failed integrity check: {source.name}")
        finally:
            destination_db.close()
            source_db.close()
        guard.execute("ROLLBACK")
    except sqlite3.OperationalError as exc:
        raise DesktopOperationError("desktop_backup_writer_detected", f"SQLite owner has an active or unknown writer: {source.name}") from exc
    finally:
        guard.close()


def _validate_sqlite(path: Path, *, required_tables: tuple[str, ...] = ()) -> None:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if required_tables:
                tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                if not set(required_tables).issubset(tables):
                    raise DesktopOperationError("desktop_restore_sqlite_schema_mismatch", f"restored SQLite owner is missing required tables: {path.name}")
        finally:
            connection.close()
    except DesktopOperationError:
        raise
    except sqlite3.Error as exc:
        raise DesktopOperationError("desktop_restore_sqlite_invalid", f"restored SQLite is invalid: {path.name}") from exc
    if not result or result[0] != "ok":
        raise DesktopOperationError("desktop_restore_sqlite_invalid", f"restored SQLite failed integrity check: {path.name}")



_APPLICATION_VALIDATED_OWNERS = frozenset(
    {
        "auth_users",
        "auth_sessions",
        "auth_otp",
        "auth_oauth",
        "research_history",
        "research_imports",
        "runtime_artifacts",
        "research_run_store",
        "research_rag_local",
        "research_rag_transcripts",
        "research_paper_catalog",
        "research_graph_results",
        "research_harness_side_effects",
        "research_harness_node_output",
    }
)


def _validate_auth_users(path: Path) -> dict[str, Any]:
    from interfaces.services.auth_service import LocalJsonAuthUserRepository

    repository = LocalJsonAuthUserRepository(path)
    users = repository.list_users()
    return {"store": "LocalJsonAuthUserRepository", "configured_root": str(repository.path.resolve(strict=False)), "record_count": len(users), "_user_ids": tuple(user.userId for user in users)}


def _validate_auth_sessions(path: Path) -> dict[str, Any]:
    from interfaces.services.auth_service import LocalJsonAuthSessionRepository

    repository = LocalJsonAuthSessionRepository(path)
    sessions = repository._read_records()
    return {"store": "LocalJsonAuthSessionRepository", "configured_root": str(repository.path.resolve(strict=False)), "record_count": len(sessions), "_user_ids": tuple(session.userId for session in sessions.values())}


def _validate_auth_transaction(path: Path, *, collection: str, schema: str) -> dict[str, Any]:
    from interfaces.services.account_auth.storage import JsonTransactionStore

    store = JsonTransactionStore(path, collection=collection, schema_version=schema)
    records = store.read()
    return {"store": "JsonTransactionStore", "configured_root": str(store.path.resolve(strict=False)), "record_count": len(records)}


def _validate_research_history(path: Path) -> dict[str, Any]:
    from interfaces.services.research_history_service import ResearchHistoryService

    payload = json.loads(path.read_text(encoding="utf-8"))
    users = payload.get("users") if isinstance(payload, Mapping) else None
    if not isinstance(users, Mapping):
        raise DesktopOperationError("desktop_restore_research_history_invalid", "restored Research history owner is invalid")
    service = ResearchHistoryService(path)
    try:
        for user_id in users:
            service.read(user_id=str(user_id))
    except Exception as exc:
        raise DesktopOperationError("desktop_restore_research_history_invalid", "restored Research history could not be read by the application service") from exc
    saved_run_ids: set[str] = set()
    saved_report_ids: set[str] = set()
    for raw in users.values():
        if not isinstance(raw, Mapping):
            continue
        for saved in raw.get("savedReports") or ():
            if isinstance(saved, Mapping) and saved.get("kind") == "accepted":
                if saved.get("runId"):
                    saved_run_ids.add(str(saved["runId"]))
                if saved.get("reportId"):
                    saved_report_ids.add(str(saved["reportId"]))
        for operation in raw.get("pendingOperations") or ():
            if isinstance(operation, Mapping) and operation.get("runId"):
                saved_run_ids.add(str(operation["runId"]))
    return {
        "store": "ResearchHistoryService",
        "configured_root": str(service.path.resolve(strict=False)),
        "user_count": len(users),
        "_user_ids": tuple(str(user_id) for user_id in users),
        "_run_ids": tuple(sorted(saved_run_ids)),
        "_report_ids": tuple(sorted(saved_report_ids)),
    }


def _validate_research_imports(root: Path) -> dict[str, Any]:
    from interfaces.services.research_import_service import ResearchImportService

    service = ResearchImportService(root)
    try:
        records = service._read_records()
        source_count = 0
        for import_id, record in records.items():
            user_id = str(record.get("userId") or "")
            service.get(user_id=user_id, import_id=str(import_id))
            if record.get("path"):
                service.source_path(user_id=user_id, import_id=str(import_id))
                source_count += 1
    except Exception as exc:
        raise DesktopOperationError("desktop_restore_research_import_invalid", "restored Research import owner could not be read by the application service") from exc
    completed = tuple(record for record in records.values() if record.get("status") == "completed")
    return {
        "store": "ResearchImportService",
        "configured_root": str(service.root),
        "record_count": len(records),
        "source_count": source_count,
        "_user_ids": tuple(str(record.get("userId") or "") for record in records.values()),
        "_paper_ids": tuple(sorted(str(record.get("paperId")) for record in completed if record.get("paperId"))),
        "_source_revisions": tuple(sorted(str(record.get("sourceRevision")) for record in completed if record.get("sourceRevision"))),
    }


def _validate_runtime_artifacts(root: Path, *, run_store_root: Path | None) -> dict[str, Any]:
    from framework.agent.artifacts.models.reference import ArtifactRef
    from framework.agent.artifacts.stores.filesystem import FilesystemArtifactStore
    from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
    from interfaces.composition.research import _resolve_research_artifact_run

    generic_store = FilesystemArtifactStore(root)
    generic_refs: dict[tuple[str, str], ArtifactRef] = {}
    canonical_refs: set[str] = set()
    terminal_runs: set[str] = set()
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopOperationError("desktop_restore_runtime_artifact_invalid", "restored runtime artifact metadata is invalid") from exc
        canonical_refs.update(_walk_canonical_artifact_refs(payload))
        for candidate in _walk_mappings(payload):
            if not {"artifact_id", "run_id", "scope_kind"}.issubset(candidate):
                continue
            try:
                ref = ArtifactRef.from_dict(dict(candidate))
                generic_store.read(ref)
            except Exception as exc:
                raise DesktopOperationError("desktop_restore_runtime_artifact_invalid", "restored runtime artifact reference could not be read by the application store") from exc
            generic_refs[(ref.run_id, ref.artifact_id)] = ref
    manifest_paths = sorted(root.glob("*/manifest.json"))
    if manifest_paths or canonical_refs:
        if run_store_root is None or not run_store_root.exists():
            raise DesktopOperationError(
                "desktop_restore_runtime_artifact_invalid",
                "restored Harness artifacts require their isolated Research run store",
            )
        run_store = _research_run_store_reader(run_store_root)
        harness_store = FilesystemHarnessArtifactPort(
            root,
            accepted_run_resolver=lambda claim: _resolve_research_artifact_run(run_store, claim=claim),
        )
    else:
        harness_store = FilesystemHarnessArtifactPort(root)
    for manifest_path in manifest_paths:
        try:
            manifest = harness_store.read_terminal_manifest(manifest_path.parent.name)
            terminal_runs.add(manifest.run_id)
            for artifact in manifest.artifacts:
                harness_store.read_artifact(artifact.ref)
                canonical_refs.add(artifact.ref)
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_runtime_artifact_invalid", "restored Harness artifact manifest or content is invalid") from exc
    for run_directory in sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else ():
        try:
            for artifact in harness_store.list_staged_artifacts(run_directory.name):
                harness_store.verify_artifact_ref(artifact.ref, expected_run_id=run_directory.name)
                canonical_refs.add(artifact.ref)
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_runtime_artifact_invalid", "restored staged Harness artifact is invalid") from exc
    for ref in sorted(canonical_refs):
        try:
            harness_store.read_artifact(ref)
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_runtime_artifact_invalid", "restored canonical Harness artifact reference is dangling") from exc
    return {
        "store": "FilesystemHarnessArtifactPort",
        "configured_root": str(harness_store.root.resolve(strict=False)),
        "artifact_reference_count": len(generic_refs) + len(canonical_refs),
        "terminal_manifest_count": len(terminal_runs),
        "_canonical_refs": tuple(sorted(canonical_refs)),
    }


def _walk_canonical_artifact_refs(value: Any) -> Iterable[str]:
    if isinstance(value, str) and value.startswith("artifact://"):
        parsed = urlsplit(value)
        if parsed.netloc and parsed.path.count("/") == 1:
            yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_canonical_artifact_refs(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_canonical_artifact_refs(item)

def _walk_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for item in value.values():
            yield from _walk_mappings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_mappings(item)


def _research_run_store_reader(root: Path):
    from backend.research.application.single_paper_runtime import ResearchAnalysisResult
    from infrastructure.research.filesystem_run_store import (
        FilesystemResearchRunStore,
        RESEARCH_RUN_RECORD_SCHEMA_VERSION,
        RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )

    return FilesystemResearchRunStore(
        root,
        result_decoder=ResearchAnalysisResult.from_dict,
        supported_schema_versions=(RESEARCH_RUN_RECORD_SCHEMA_VERSION, RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2),
        write_schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
    )


def _validate_research_run_store(root: Path) -> dict[str, Any]:
    store = _research_run_store_reader(root)
    artifact_refs: set[str] = set()
    record_ids: set[str] = set()
    paper_ids: set[str] = set()
    actor_user_ids: set[str] = set()
    for record_path in sorted((root / "records").rglob("*.json")) if (root / "records").is_dir() else ():
        try:
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            run_id = str(payload["run_id"])
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise DesktopOperationError("desktop_restore_run_store_invalid", "restored Research run record is invalid") from exc
        try:
            restored_record = store.get_by_run_id(run_id)
            if restored_record is not None and restored_record.identity_scope_ref:
                diagnostic = store.get_diagnostic_by_run_id(run_id, identity_scope_ref=restored_record.identity_scope_ref)
                if diagnostic is None or diagnostic.run_id != run_id:
                    raise ValueError("diagnostic identity lookup did not resolve")
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_run_store_invalid", "restored Research run record could not be read by the application store") from exc
        if restored_record is None:
            raise DesktopOperationError("desktop_restore_run_store_invalid", "restored Research run record could not be read by the application store")
        record_ids.add(run_id)
        paper_ids.add(str(restored_record.paper_id))
        result_payload = restored_record.result.to_dict() if hasattr(restored_record.result, "to_dict") else payload.get("result")
        if isinstance(result_payload, Mapping):
            actor_scope = result_payload.get("actor_scope")
            if isinstance(actor_scope, Mapping) and actor_scope.get("user_id"):
                actor_user_ids.add(str(actor_scope["user_id"]))
        artifact_refs.update(_walk_canonical_artifact_refs(result_payload))
    record_count = len(record_ids)
    latest_count = 0
    for index_path in sorted((root / "latest").rglob("*.json")) if (root / "latest").is_dir() else ():
        latest_count += 1
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            record = store.get_by_run_id(str(index["run_id"]))
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_run_store_invalid", "restored Research latest index is invalid") from exc
        if record is None or record.paper_id != str(index.get("paper_id")):
            raise DesktopOperationError("desktop_restore_run_store_invalid", "restored Research latest index does not resolve to its owned record")
    return {
        "store": "FilesystemResearchRunStore",
        "configured_root": str(store.root.resolve(strict=False)),
        "record_count": record_count,
        "latest_index_count": latest_count,
        "_run_ids": tuple(sorted(record_ids)),
        "_paper_ids": tuple(sorted(paper_ids)),
        "_user_ids": tuple(sorted(actor_user_ids)),
        "_artifact_refs": tuple(sorted(artifact_refs)),
    }

def _validate_local_chunk_store(root: Path) -> dict[str, Any]:
    from infrastructure.research.local_chunk_store import LocalChunkPayloadStore

    collections: list[str] = []
    paper_ids: set[str] = set()
    document_ids: set[str] = set()
    chunk_count = 0
    for path in sorted(root.glob("*.json")):
        collection = path.stem
        collections.append(collection)
        try:
            store = LocalChunkPayloadStore(root, collection=collection)
            payloads = store._read_snapshot() or []
            chunk_count += len(payloads)
            paper_ids.update(str(item["paper_id"]) for item in payloads)
            document_ids.update(str(item["document_id"]) for item in payloads if item.get("document_id"))
        except (OSError, ValueError, RuntimeError) as exc:
            raise DesktopOperationError("desktop_restore_chunk_store_invalid", "restored local Research chunk store is invalid") from exc
    return {"store": "LocalChunkPayloadStore", "configured_root": str(root.resolve(strict=False)), "collections": collections, "chunk_count": chunk_count, "_paper_ids": tuple(sorted(paper_ids)), "_document_ids": tuple(sorted(document_ids))}


def _validate_paper_rag_transcript_store(root: Path) -> dict[str, Any]:
    from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore

    store = PaperRagTranscriptFileStore(root)
    transcript_count = 0
    journal_count = 0
    transcripts_by_session: dict[tuple[str, str], dict[str, Any]] = {}
    journals: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
    run_ids: set[str] = set()
    actor_user_ids: set[str] = set()
    for path in sorted(root.glob("*.json")):
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            actor_scope = envelope["actor_scope"]
            if envelope.get("kind") == "event_journal":
                session_id = str(envelope["session_id"])
                if store.journal_path_for(session_id).resolve(strict=False) != path.resolve(strict=False):
                    raise ValueError("RAG journal identity does not own its path")
                journal = store.load_journal(session_id, actor_scope=actor_scope)
                if journal is None:
                    raise ValueError("RAG journal disappeared during validation")
                journals.append((journal, actor_scope))
                journal_count += 1
                continue
            transcript_id = str(envelope["transcript_id"])
            if store.path_for(transcript_id).resolve(strict=False) != path.resolve(strict=False):
                raise ValueError("transcript identity does not own its path")
            loaded = store.load(transcript_id, actor_scope=actor_scope)
            scope_key = json.dumps(dict(actor_scope), sort_keys=True, separators=(",", ":"))
            transcripts_by_session[(scope_key, str(loaded["session_id"]))] = loaded
            transcript = loaded.get("transcript")
            if isinstance(transcript, Mapping) and transcript.get("run_id"):
                run_ids.add(str(transcript["run_id"]))
            if isinstance(actor_scope, Mapping) and actor_scope.get("user_id"):
                actor_user_ids.add(str(actor_scope["user_id"]))
            transcript_count += 1
        except (KeyError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, PermissionError) as exc:
            raise DesktopOperationError("desktop_restore_rag_transcript_invalid", "restored Paper RAG transcript or journal is invalid") from exc
    for journal, actor_scope in journals:
        scope_key = json.dumps(dict(actor_scope), sort_keys=True, separators=(",", ":"))
        transcript = transcripts_by_session.get((scope_key, str(journal["session_id"])))
        if journal.get("finalized"):
            if transcript is None or journal.get("transcript_id") != transcript.get("transcript_id"):
                raise DesktopOperationError("desktop_restore_rag_transcript_invalid", "finalized RAG journal does not resolve to its transcript")
            journal_events = [{"event_type": item["event_type"], "payload": item["payload"]} for item in journal.get("events", [])]
            if journal_events != list(transcript.get("transcript", {}).get("events") or ()):
                raise DesktopOperationError("desktop_restore_rag_transcript_invalid", "RAG journal events do not match the finalized transcript")
    return {
        "store": "PaperRagTranscriptFileStore",
        "configured_root": str(store.root),
        "transcript_count": transcript_count,
        "journal_count": journal_count,
        "_run_ids": tuple(sorted(run_ids)),
        "_user_ids": tuple(sorted(actor_user_ids)),
    }


def _validate_research_catalog_store(root: Path) -> dict[str, Any]:
    from backend.research.domain.catalog import ResearchPaperCatalogEntry, ResearchPaperIdentity, ResearchPaperRelation, ResearchSourceSnapshot
    from backend.research.domain.code_repository import CodeRepositoryProfile
    from backend.research.domain.document import ResearchDocument
    from backend.research.domain.evidence import ResearchEvidencePack
    from backend.research.domain.paper import ResearchPaper
    from backend.research.ports.catalog_models import ResearchScore, ResearchSOTAClaim
    from infrastructure.research.catalog_store import FilesystemResearchCatalogStore, _model

    model_types = {
        "catalog": ResearchPaperCatalogEntry,
        "identities": ResearchPaperIdentity,
        "relations": ResearchPaperRelation,
        "snapshots": ResearchSourceSnapshot,
        "papers": ResearchPaper,
        "documents": ResearchDocument,
        "evidence": ResearchEvidencePack,
        "scores": ResearchScore,
        "sota_claims": ResearchSOTAClaim,
        "code_profiles": CodeRepositoryProfile,
    }
    scope_count = 0
    model_count = 0
    paper_ids: set[str] = set()
    document_ids: set[str] = set()
    source_revisions: set[str] = set()
    for path in sorted(root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            scope_ref = str(payload["scope_ref"])
            dimensions = {}
            for component in scope_ref.split("|"):
                key, separator, value = component.partition("=")
                if not separator or key not in {"tenant", "user", "memory"}:
                    raise ValueError("catalog scope is invalid")
                dimensions[key] = value
            store = FilesystemResearchCatalogStore(root, tenant_id=dimensions.get("tenant"), user_id=dimensions.get("user"), memory_namespace=dimensions.get("memory"))
            if store.path.resolve(strict=False) != path.resolve(strict=False):
                raise ValueError("catalog scope does not own its path")
            state = store._read()
            for collection, model_type in model_types.items():
                for raw in state[collection].values():
                    value = _model(model_type, raw)
                    if value is None:
                        raise ValueError("catalog model is missing")
                    model_count += 1
                    paper_id = getattr(value, "paper_id", None)
                    if paper_id:
                        paper_ids.add(str(paper_id))
                    document_id = getattr(value, "document_id", None)
                    if document_id:
                        document_ids.add(str(document_id))
                    source_revision = getattr(value, "source_hash", None)
                    if source_revision:
                        source_revisions.add(str(source_revision))
            scope_count += 1
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_paper_catalog_invalid", "restored Research paper catalog is invalid") from exc
    artifact_root = root / "artifacts"
    artifact_count = 0
    for path in sorted(artifact_root.glob("*.json")) if artifact_root.is_dir() else ():
        if path.name.endswith(".commit.json"):
            continue
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            artifact_type = str(envelope["artifact_type"])
            metadata = envelope["metadata"]
            actor_scope = metadata.get("actor_scope") if isinstance(metadata, Mapping) else None
            encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            digest = sha256(encoded).hexdigest()
            FilesystemResearchCatalogStore(root).read(f"artifact://research/{artifact_type}/{digest}", actor_scope=actor_scope if isinstance(actor_scope, Mapping) else {}, include_payload=True)
            artifact_count += 1
        except Exception as exc:
            raise DesktopOperationError("desktop_restore_paper_catalog_invalid", "restored Research catalog artifact is invalid") from exc
    return {
        "store": "FilesystemResearchCatalogStore",
        "configured_root": str(root.resolve(strict=False)),
        "scope_count": scope_count,
        "model_count": model_count,
        "artifact_count": artifact_count,
        "_paper_ids": tuple(sorted(paper_ids)),
        "_document_ids": tuple(sorted(document_ids)),
        "_source_revisions": tuple(sorted(source_revisions)),
    }

def _validate_graph_result_store(path: Path) -> dict[str, Any]:
    from framework.harness.runtime.result_models import NodeResultBinding
    from infrastructure.storage.artifacts.result_sqlite import SQLiteGraphResultStore

    store = SQLiteGraphResultStore(path)
    store.verify_integrity()
    attempt_count = 0
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        for (binding_json,) in connection.execute("SELECT binding_json FROM graph_result_attempts"):
            binding = NodeResultBinding.from_dict(json.loads(binding_json))
            if store.get(binding) is None:
                raise DesktopOperationError("desktop_restore_graph_result_invalid", "restored graph result binding does not resolve through its application store")
            attempt_count += 1
    return {"store": "SQLiteGraphResultStore", "configured_root": str(store.path), "attempt_count": attempt_count}


def _validate_harness_side_effect_store(path: Path) -> dict[str, Any]:
    from infrastructure.storage.harness.sqlite import SQLiteHarnessSideEffectStore

    store = SQLiteHarnessSideEffectStore(path)
    store.verify_integrity()
    decision_count = 0
    outcome_count = 0
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            for (decision_ref,) in connection.execute("SELECT decision_ref FROM harness_side_effect_decisions"):
                if store.get_decision(str(decision_ref)) is None:
                    raise DesktopOperationError("desktop_restore_harness_side_effect_invalid", "restored side-effect decision does not resolve through its application store")
                decision_count += 1
            for effect_id, identity_scope_ref, subject_scope_ref in connection.execute("SELECT json_extract(outcome_json, '$.effect_id'), identity_scope_ref, subject_scope_ref FROM harness_side_effect_outcomes"):
                if store.read_outcome(effect_id=str(effect_id), identity_scope_ref=str(identity_scope_ref), subject_scope_ref=str(subject_scope_ref)) is None:
                    raise DesktopOperationError("desktop_restore_harness_side_effect_invalid", "restored side-effect outcome does not resolve through its application store")
                outcome_count += 1
    finally:
        store.close()
    return {"store": "SQLiteHarnessSideEffectStore", "configured_root": str(path.resolve(strict=False)), "decision_count": decision_count, "outcome_count": outcome_count}


def _validate_harness_node_output_store(path: Path) -> dict[str, Any]:
    from framework.harness.control_plane.node_output import HarnessNodeOutputCommit, HarnessNodeOutputLease
    from infrastructure.storage.harness.sqlite_node_output import SQLiteHarnessNodeOutputResource

    store = SQLiteHarnessNodeOutputResource(path)
    lease_count = 0
    commit_count = 0
    leases_by_ref: dict[str, HarnessNodeOutputLease] = {}
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            for lease_json, in connection.execute("SELECT lease_json FROM leases"):
                lease = HarnessNodeOutputLease.from_dict(json.loads(lease_json))
                current = store.current_lease(lease.resource)
                if current is not None and current.lease_ref != lease.lease_ref:
                    raise DesktopOperationError("desktop_restore_harness_node_output_invalid", "restored node-output lease index resolves to a different lease")
                leases_by_ref[lease.lease_ref] = lease
                lease_count += 1
            for committed_json, in connection.execute("SELECT committed_json FROM resource_state WHERE committed_json IS NOT NULL"):
                commit = HarnessNodeOutputCommit.from_dict(json.loads(committed_json))
                lease = leases_by_ref.get(commit.lease_ref)
                if lease is None:
                    raise DesktopOperationError("desktop_restore_harness_node_output_invalid", "restored node-output commit has a dangling lease")
                resolved = store.committed_output(lease.resource)
                if resolved is None or resolved.commit_ref != commit.commit_ref:
                    raise DesktopOperationError("desktop_restore_harness_node_output_invalid", "restored node-output commit does not resolve through its application store")
                commit_count += 1
    finally:
        store.close()
    return {"store": "SQLiteHarnessNodeOutputResource", "configured_root": str(Path(store.path).resolve(strict=False)), "lease_count": lease_count, "commit_count": commit_count}



def _validate_sqlite_path_columns(path: Path) -> None:
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
            tables = [str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
            for table in tables:
                quoted_table = '"' + table.replace('"', '""') + '"'
                columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({quoted_table})")]
                candidate_columns = [column for column in columns if _sqlite_reference_column(column)]
                if not candidate_columns:
                    continue
                selection = ", ".join('"' + column.replace('"', '""') + '"' for column in candidate_columns)
                for row in connection.execute(f"SELECT {selection} FROM {quoted_table}"):
                    for value in row:
                        if not isinstance(value, str):
                            continue
                        candidate = _absolute_reference_path(value)
                        if candidate is not None:
                            raise DesktopOperationError("desktop_restore_sqlite_reference_unsupported", "restored SQLite owner contains an absolute path reference")
    except DesktopOperationError:
        raise
    except sqlite3.Error as exc:
        raise DesktopOperationError("desktop_restore_sqlite_invalid", "restored SQLite reference columns could not be validated") from exc


def _sqlite_reference_column(name: str) -> bool:
    normalized = str(name).casefold().replace("-", "_")
    return normalized == "path" or normalized.endswith("_path") or normalized.endswith("path_ref") or normalized in {"artifact_ref", "artifactref"}

def _required_sqlite_tables(owner_id: str) -> tuple[str, ...]:
    return {
        "research_graph_results": ("graph_result_store_metadata", "graph_result_attempts"),
        "research_harness_side_effects": ("harness_side_effect_store_metadata", "harness_side_effect_decisions"),
        "research_harness_node_output": ("resource_state", "leases", "staged"),
    }.get(owner_id, ())


def _walk_path_references(value: Any, *, field_name: str | None = None) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_path_references(item, field_name=str(key))
    elif isinstance(value, list):
        for item in value:
            yield from _walk_path_references(item, field_name=field_name)
    elif isinstance(value, str):
        normalized = (field_name or "").casefold().replace("-", "_")
        reference_field = normalized == "path" or normalized.endswith("_path") or normalized.endswith("path_ref") or normalized in {"artifact_ref", "artifactref"}
        if reference_field:
            yield value


def _relocated_reference(candidate: Path, roots: Iterable[tuple[Path, Path, str]]) -> Path | None:
    lexical = Path(os.path.normpath(str(candidate)))
    for source, staged, kind in roots:
        source_lexical = Path(os.path.normpath(str(source)))
        if os.path.normcase(str(lexical)) == os.path.normcase(str(source_lexical)):
            return staged
        if kind != "directory":
            continue
        try:
            relative_text = os.path.relpath(str(lexical), str(source_lexical))
        except ValueError:
            continue
        relative = Path(relative_text)
        if relative_text != os.pardir and not relative_text.startswith(os.pardir + os.sep) and not relative.is_absolute():
            return staged / relative
    return None


def _absolute_reference_path(value: str) -> Path | None:
    if not isinstance(value, str) or "\x00" in value:
        raise DesktopOperationError("desktop_restore_path_invalid", "restored path reference is invalid")
    windows_drive = len(value) >= 3 and value[1] == ":" and value[2] in {"/", "\\"}
    windows_unc = value.startswith(("\\\\", "//"))
    if windows_drive or windows_unc:
        candidate = Path(value)
    else:
        parsed = urlsplit(value)
        if parsed.scheme.casefold() == "file":
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                candidate = Path(f"//{parsed.netloc}{unquote(parsed.path)}")
            else:
                candidate = Path(unquote(parsed.path.lstrip("/")) if os.name == "nt" else unquote(parsed.path))
        elif parsed.scheme:
            return None
        else:
            if ":" in value:
                raise DesktopOperationError("desktop_restore_path_invalid", "alternate data stream path references are not allowed")
            candidate = Path(value)
    return candidate if candidate.is_absolute() else None


def _safe_windows_path_segment(segment: str) -> bool:
    if not segment or segment[-1] in {" ", "."} or ":" in segment or any(ord(char) < 32 for char in segment):
        return False
    stem = segment.split(".", 1)[0].casefold()
    reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    return stem not in reserved


def _validate_declared_file_checksums(value: Any, base: Path) -> None:
    if isinstance(value, Mapping):
        raw_path = value.get("path")
        raw_checksum = value.get("sha256") or value.get("checksum")
        if isinstance(raw_path, str) and isinstance(raw_checksum, str):
            absolute_candidate = _absolute_reference_path(raw_path)
            candidate = absolute_candidate or (base / raw_path).resolve(strict=False)
            if absolute_candidate is None and candidate != base.resolve(strict=False) and not candidate.is_relative_to(base.resolve(strict=False)):
                raise DesktopOperationError("desktop_restore_artifact_checksum_mismatch", "restored file reference escapes its declaring metadata directory")
            expected = raw_checksum.removeprefix("sha256:")
            if len(expected) == 64 and all(char in "0123456789abcdef" for char in expected.casefold()):
                if not candidate.is_file() or _file_digest(candidate) != expected.casefold():
                    raise DesktopOperationError("desktop_restore_artifact_checksum_mismatch", "restored file reference failed its declared checksum")
        for item in value.values():
            _validate_declared_file_checksums(item, base)
    elif isinstance(value, list):
        for item in value:
            _validate_declared_file_checksums(item, base)


def _iter_snapshot_files(root: Path) -> Iterable[tuple[Path, str]]:
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path, path.relative_to(root).as_posix()


def _excluded(relative: Path, path: Path) -> bool:
    names = {part.casefold() for part in relative.parts}
    return bool(names & _EXCLUDED_NAMES) or path.suffix.casefold() in (_EXCLUDED_SUFFIXES | {".lock"}) or path.name.casefold().startswith(".env") or path.name.casefold().endswith(("-wal", "-shm"))


def _owner_lock_paths(owner: StorageOwner) -> tuple[Path, ...]:
    if owner.consistency == "research_run_store":
        return (owner.path / ".research-run-store.lock",)
    if owner.consistency == "local_chunk_store":
        return tuple(sorted(owner.path.glob(".*.lock"))) if owner.path.exists() else ()
    return ()


def _tree_signature(root: Path) -> str:
    digest = sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if _excluded(relative, path):
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_file_digest(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite(path: Path) -> bool:
    if path.suffix.casefold() in {".sqlite", ".sqlite3", ".db"}:
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _is_reparse_point(path: Path) -> bool:
    return bool(getattr(path.stat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


__all__ = [
    "ACTIVATION_SCHEMA",
    "BACKUP_SCHEMA",
    "BackupManager",
    "BackupManifest",
    "RestoreManager",
    "StorageInventory",
    "StorageInventoryResolver",
    "StorageOwner",
]
