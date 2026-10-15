from __future__ import annotations

import json
import shutil
import zipfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pymupdf
import pytest

from backend.research.application.personal_memory import PersonalMemoryService
from backend.research.domain.personal_memory import PersonalMemoryOwner, PersonalMemoryStatus
from infrastructure.research.catalog_store import FilesystemResearchCatalogStore
from infrastructure.research.local_chunk_store import LocalChunkPayloadStore
from infrastructure.research.personal_memory_store import SQLitePersonalMemoryStore
from interfaces.composition.research_local import build_local_research_service
from interfaces.composition.research_settings import DESKTOP_RESEARCH_LLM_ENABLED_ENV, ResearchLocalRuntimeSettings
from interfaces.services.auth_service import AuthSession, LocalJsonAuthSessionRepository, LocalJsonAuthUserRepository, StoredAuthUser
from interfaces.services.research_desktop_control import LocalJsonResearchDesktopRequestStore
from interfaces.services.research_history_service import ResearchHistoryService
from interfaces.services.research_import_application import ResearchImportApplication
from interfaces.services.research_import_service import ResearchImportService
from scripts.desktop_app.config import initialize_data_root, resolve_desktop_app_config
from scripts.local_desktop.common import DesktopOperationError, windows_extended_path
from scripts.local_desktop.recovery import BackupManager, RestoreManager, StorageInventoryResolver, _canonical_source_revision, _validate_application_reference_closure


@pytest.fixture
def local_library(request):
    # Keep real parser artifact paths below Windows MAX_PATH in test runners.
    suffix = " # space 中文 %" if getattr(request, "param", None) == "reserved_uri" else ""
    fixture_parent = (Path.cwd() / ".tmp" / "local-recovery").resolve()
    owned_uuid = uuid4().hex[:12]
    root = (fixture_parent / f"{owned_uuid}{suffix}").resolve()
    root.mkdir(parents=True, exist_ok=False)
    try:
        resources = root / "app"
        resources.mkdir()
        config = resolve_desktop_app_config(resource_root=resources, data_root=root / "data", environ={"PYTHONNOUSERSITE": "1"})
        initialize_data_root(config)
        env = config.environment
        assert env[DESKTOP_RESEARCH_LLM_ENABLED_ENV] == "0"
        user_id = "reader"
        users = LocalJsonAuthUserRepository(env["NEWSROOM_AUTH_USERS_PATH"])
        users.add_user(StoredAuthUser(userId=user_id, username="reader", passwordHash="test-hash", passwordSalt="test-salt"))
        sessions = LocalJsonAuthSessionRepository(env["NEWSROOM_AUTH_SESSIONS_PATH"])
        now = datetime.now(UTC)
        sessions.create_session(AuthSession("session-before-backup", user_id, "test-digest", now, now + timedelta(hours=1)))
        control = LocalJsonResearchDesktopRequestStore(config.desktop.control_path)
        control.resume_admission()
        settings = ResearchLocalRuntimeSettings.from_env(env, cwd=resources)
        research = build_local_research_service(settings)
        imports = ResearchImportService(env["NEWSROOM_RESEARCH_IMPORT_DIR"])
        document = pymupdf.open()
        try:
            for index, heading in enumerate(("Introduction", "Method", "Results"), start=1):
                page = document.new_page()
                paragraph = (
                    f"{heading}\n\nLocal recovery section {index} validates deterministic PDF identity. "
                    "The document contains selectable text for the local parser and evidence builder. "
                )
                page.insert_textbox(pymupdf.Rect(50, 60, 545, 790), paragraph * 10, fontsize=10, lineheight=1.25)
            pdf = document.tobytes()
        finally:
            document.close()
        fingerprint = "sha256:" + sha256(pdf).hexdigest()
        received = imports.create_received(user_id=user_id, content=pdf, filename="original.pdf", source_revision=fingerprint, source_fingerprint=fingerprint)
        imported = ResearchImportApplication(imports, research).process(user_id=user_id, import_id=received["importId"])
        assert imported["status"] == "completed"
        paper_id = imported["paperId"]
        note = "My durable reader note: check the method against the original PDF."
        history = ResearchHistoryService(env["NEWSROOM_RESEARCH_HISTORY_PATH"])
        history.replace(user_id=user_id, revision=0, visits=[], groups=[], workspace_items={"readerNotebooks": [{"paperId": paper_id, "sourceRevision": imported["sourceRevision"], "note": note, "question": "", "fontSize": 18, "sectionId": "", "pdfPage": 2, "readerView": "pdf", "updatedAt": 1760000000000}]})
        memory_path = config.data_root / "research" / "personal-memory.sqlite3"
        memory_owner = PersonalMemoryOwner(user_id=user_id, namespace=f"research:user:{user_id}")
        with SQLitePersonalMemoryStore(memory_path) as store:
            memory = PersonalMemoryService(store)
            active = memory.create_confirmed(memory_owner, content="Prefer concise summaries.", category="reading_preference", source_refs=[{"source_type": "user_input", "source_ref": "test:active"}], client_request_id="active")
            record = memory.create_confirmed(memory_owner, content="Old preference remains disabled.", category="reading_preference", source_refs=[{"source_type": "user_input", "source_ref": "test:disabled"}], client_request_id="disabled")
            disabled = memory.disable(memory_owner, record.memory_id, expected_revision=record.revision)
            policy = memory.get_policy(memory_owner)
            assert policy.allow_remote_processing is False
        resolver = StorageInventoryResolver(config.desktop, environ=env)
        owners = {owner.owner_id: owner for owner in resolver.resolve().owners}
        assert not owners["research_rag_local"].path.exists()
        assert owners["research_rag_local"].required is False
        yield SimpleNamespace(root=root, config=config, env=env, resolver=resolver, owners=owners, imports=imports, imported=imported, user_id=user_id, pdf=pdf, note=note, sessions=sessions, memory_owner=memory_owner, memory_path=memory_path, active=active, disabled=disabled, control=control)
    finally:
        resolved_parent = fixture_parent.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        if resolved_root.parent != resolved_parent or resolved_root.name != f"{owned_uuid}{suffix}":
            raise RuntimeError("refusing to clean an unowned local recovery fixture root")
        shutil.rmtree(windows_extended_path(resolved_root))


def _backup(library, name="backup.zip"):
    destination = library.root / name
    result = BackupManager(library.config.desktop, inventory_resolver=library.resolver).create(destination=destination)
    assert result.ok and result.status == "COMPLETE"
    assert not list(library.config.data_root.glob("desktop-backup-*"))
    return destination


def _restore(library, backup, name="restored", **kwargs):
    return RestoreManager(library.config.desktop, inventory_resolver=library.resolver).restore(backup, target_root=library.root / name, **kwargs)


def test_real_local_only_library_backup_restore_without_vector_index(local_library):
    library = local_library
    generation = library.control.generation()
    original_history = Path(library.env["NEWSROOM_RESEARCH_HISTORY_PATH"]).read_bytes()
    original_memory = library.memory_path.read_bytes()
    backup = _backup(library)
    with zipfile.ZipFile(backup) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        chunk_owner = next(owner for owner in manifest["inventory"]["owners"] if owner["owner_id"] == "research_rag_local")
        assert chunk_owner["required"] is False
        assert not any(entry["owner_id"] == "research_rag_local" for entry in manifest["files"])
        for entry in manifest["files"]:
            content = archive.read(f"data/{entry['owner_id']}/{entry['path']}")
            assert len(content) == entry["size"]
            assert sha256(content).hexdigest() == entry["sha256"]
    result = _restore(library, backup)
    assert result.ok and result.status == "VERIFIED"
    root = Path(result.details["restore_root"])
    report = json.loads((root / "restore-report.json").read_text(encoding="utf-8"))
    paths = {key: Path(value) for key, value in report["owner_paths"].items()}
    assert not paths["research_rag_local"].exists()
    assert report["validation"]["owners"]["research_rag_local"]["status"] == "absent_optional"
    assert report["application_isolation"]["verified"] is True
    assert report["activation"] == "not_requested"
    assert "generation" not in report
    assert library.control.generation() == generation
    restored_imports = ResearchImportService(paths["research_imports"])
    restored_import = restored_imports.get(user_id=library.user_id, import_id=library.imported["importId"])
    assert restored_import["sourceRevision"] == library.imported["sourceRevision"]
    assert restored_imports.source_path(user_id=library.user_id, import_id=library.imported["importId"]).read_bytes() == library.pdf
    assert paths["research_history"].read_bytes() == original_history
    restored_history = ResearchHistoryService(paths["research_history"]).read(user_id=library.user_id)
    assert restored_history.workspace_items["readerNotebooks"][0]["note"] == library.note
    assert not LocalJsonAuthSessionRepository(paths["auth_sessions"])._read_records()
    assert len(library.sessions._read_records()) == 1  # Staging invalidation never logs out the live owner.
    preview = result.details["personal_memory_restore"]
    assert preview["requires_explicit_activation_confirmation"] is True
    assert preview["status_counts"] == {"active": 1, "disabled": 1}
    with SQLitePersonalMemoryStore(paths["research_personal_memory"]) as store:
        service = PersonalMemoryService(store)
        records = {record.memory_id: record for record in service.list(library.memory_owner, statuses=tuple(PersonalMemoryStatus), limit=100).records}
        assert records[library.active.memory_id].content == library.active.content
        assert records[library.disabled.memory_id].status is PersonalMemoryStatus.DISABLED
        assert service.get_policy(library.memory_owner).allow_remote_processing is False
    with pytest.raises(DesktopOperationError, match="explicit confirmation") as raised:
        _restore(library, backup, name="unconfirmed-activation", activate=True)
    assert raised.value.reason_code == "desktop_restore_personal_memory_confirmation_required"
    assert library.memory_path.read_bytes() == original_memory
    assert library.control.generation() == generation
    assert len(library.sessions._read_records()) == 1
    assert not library.owners["research_rag_local"].path.exists()


@pytest.mark.parametrize("enabled", [None, "1"])
def test_enabled_or_unspecified_local_rag_still_requires_chunks(local_library, enabled):
    env = dict(local_library.env)
    if enabled is None:
        env.pop(DESKTOP_RESEARCH_LLM_ENABLED_ENV)
    else:
        env[DESKTOP_RESEARCH_LLM_ENABLED_ENV] = enabled
    resolver = StorageInventoryResolver(local_library.config.desktop, environ=env)
    with pytest.raises(DesktopOperationError) as raised:
        BackupManager(local_library.config.desktop, inventory_resolver=resolver).create(destination=local_library.root / "must-not-exist.zip")
    assert raised.value.reason_code == "desktop_inventory_required_owner_missing"
    assert "research_rag_local" in str(raised.value)
    assert not (local_library.root / "must-not-exist.zip").exists()


def test_disabled_runtime_rejects_reference_to_absent_chunks(local_library):
    history_path = Path(local_library.env["NEWSROOM_RESEARCH_HISTORY_PATH"])
    state = json.loads(history_path.read_text(encoding="utf-8"))
    state["chunk_path"] = str(local_library.owners["research_rag_local"].path / "research_paper_chunks.json")
    history_path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(DesktopOperationError) as raised:
        _backup(local_library)
    assert raised.value.reason_code == "desktop_backup_chunk_reference_dangling"
    assert not (local_library.root / "backup.zip").exists()
    assert not local_library.owners["research_rag_local"].path.exists()


def _index_chunks(library):
    scope = {"user_id": library.user_id, "memory_namespace": f"research:user:{library.user_id}"}
    catalog = FilesystemResearchCatalogStore(library.owners["research_paper_catalog"].path)
    document = catalog.get_document(library.imported["paperId"], actor_scope=scope)
    assert document is not None
    chunks = LocalChunkPayloadStore(library.owners["research_rag_local"].path)
    chunks.index_payloads([{"chunk_id": "stored-chunk", "paper_id": document.paper_id, "document_id": document.document_id, "content": "evidence"}])
    return chunks


def test_disabled_runtime_includes_and_validates_existing_chunks(local_library):
    chunks = _index_chunks(local_library)
    inventory = local_library.resolver.resolve()
    assert next(owner for owner in inventory.owners if owner.owner_id == "research_rag_local").required is True
    backup = _backup(local_library)
    with zipfile.ZipFile(backup) as archive:
        assert f"data/research_rag_local/{chunks.path.name}" in archive.namelist()
    result = _restore(local_library, backup)
    receipt = result.details["validation"]["owners"]["research_rag_local"]["application_validation"]
    assert receipt["chunk_count"] == 1


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_existing_chunk_checksum_corruption_fails_in_both_runtime_modes(local_library, enabled):
    chunks = _index_chunks(local_library)
    local_library.resolver = StorageInventoryResolver(local_library.config.desktop, environ={**local_library.env, DESKTOP_RESEARCH_LLM_ENABLED_ENV: enabled})
    state = json.loads(chunks.path.read_text(encoding="utf-8"))
    state["payloads"][0]["content"] = "corrupted"
    chunks.path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(DesktopOperationError) as raised:
        _backup(local_library)
    assert raised.value.reason_code == "desktop_backup_local_chunk_invalid"
    assert not (local_library.root / "backup.zip").exists()


def test_disabled_runtime_restore_rejects_archive_member_checksum_corruption(local_library):
    backup = _backup(local_library)
    corrupted = local_library.root / "corrupt.zip"
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(corrupted, "w") as target:
        changed = False
        for entry in source.infolist():
            content = source.read(entry.filename)
            if entry.filename.endswith(".pdf"):
                content = bytes([content[0] ^ 1]) + content[1:]
                changed = True
            target.writestr(entry, content)
    assert changed
    with pytest.raises(DesktopOperationError) as raised:
        _restore(local_library, corrupted, name="checksum-failed")
    assert raised.value.reason_code == "desktop_backup_checksum_mismatch"
    assert len(local_library.sessions._read_records()) == 1


def test_qdrant_keeps_external_vector_recovery_contract(local_library):
    env = {**local_library.env, "NEWS_RESEARCH_RAG_BACKEND": "qdrant"}
    inventory = StorageInventoryResolver(local_library.config.desktop, environ=env).resolve()
    assert not any(owner.owner_id == "research_rag_local" for owner in inventory.owners)
    run_owner = next(owner for owner in inventory.owners if owner.owner_id == "research_run_store")
    assert run_owner.external_requirements == ("external vector index must be rebuilt or restored independently",)


@pytest.mark.parametrize("enabled", [None, "1"])
def test_rag_runtime_cannot_restore_backup_without_required_chunks(local_library, enabled):
    backup = _backup(local_library)
    env = dict(local_library.env)
    if enabled is None:
        env.pop(DESKTOP_RESEARCH_LLM_ENABLED_ENV)
    else:
        env[DESKTOP_RESEARCH_LLM_ENABLED_ENV] = enabled
    resolver = StorageInventoryResolver(local_library.config.desktop, environ=env)
    with pytest.raises(DesktopOperationError) as raised:
        RestoreManager(local_library.config.desktop, inventory_resolver=resolver).restore(backup, target_root=local_library.root / "enabled-restore")
    assert raised.value.reason_code == "desktop_backup_required_owner_missing"


@pytest.mark.parametrize("kind", ["relative_json", "journal", "sqlite"])
def test_absent_chunks_references_fail_across_owned_metadata_formats(local_library, kind):
    import sqlite3

    artifact_root = local_library.owners["runtime_artifacts"].path
    chunk_file = local_library.owners["research_rag_local"].path / "research_paper_chunks.json"
    if kind == "relative_json":
        relative = Path("..") / ".." / "research" / "catalog" / "chunks" / chunk_file.name
        (artifact_root / "reference.json").write_text(json.dumps({"chunk_path": str(relative)}), encoding="utf-8")
    elif kind == "journal":
        (artifact_root / "reference.jsonl").write_text(json.dumps({"chunk_path": str(chunk_file)}) + "\n", encoding="utf-8")
    else:
        connection = sqlite3.connect(artifact_root / "reference.sqlite3")
        try:
            connection.execute("CREATE TABLE references_to_chunks(chunk_path TEXT)")
            connection.execute("INSERT INTO references_to_chunks VALUES (?)", (str(chunk_file),))
            connection.commit()
        finally:
            connection.close()
    with pytest.raises(DesktopOperationError) as raised:
        _backup(local_library)
    assert raised.value.reason_code == "desktop_backup_chunk_reference_dangling"
    assert not (local_library.root / "backup.zip").exists()
    assert not list(local_library.config.data_root.glob("desktop-backup-*"))


@pytest.mark.parametrize("revision", ["sha256:" + "0" * 64, "opaque-wrong-revision", "padded-matching-hash"])
def test_restore_rejects_different_source_revision_not_just_prefix_difference(local_library, revision):
    path = local_library.owners["research_imports"].path / "index.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    records = state["imports"]
    for record in records.values():
        record["sourceRevision"] = (" " + record["sourceRevision"] + " ") if revision == "padded-matching-hash" else revision
    path.write_text(json.dumps(state), encoding="utf-8")
    backup = _backup(local_library)
    with pytest.raises(DesktopOperationError) as raised:
        _restore(local_library, backup)
    assert raised.value.reason_code == "desktop_restore_source_reference_dangling"
    assert len(local_library.sessions._read_records()) == 1


@pytest.mark.parametrize(("revision", "expected"), [("sha256:" + "a" * 64, "a" * 64), ("A" * 64, "a" * 64), ("opaque-ID", "opaque-ID"), ("sha256:opaque-ID", "sha256:opaque-ID"), (" opaque-ID", " opaque-ID"), ("opaque-ID ", "opaque-ID "), (" opaque-ID ", " opaque-ID ")])
def test_source_revision_canonicalization_preserves_opaque_identity(revision, expected):
    assert _canonical_source_revision(revision) == expected


@pytest.mark.parametrize("revision", [" opaque-ID", "opaque-ID ", " opaque-ID "])
def test_restore_reference_closure_rejects_whitespace_changed_opaque_revision(revision):
    receipts = {
        "research_imports": {"_source_revisions": (revision,)},
        "research_paper_catalog": {"_source_revisions": ("opaque-ID",)},
    }
    with pytest.raises(DesktopOperationError) as raised:
        _validate_application_reference_closure({}, receipts)
    assert raised.value.reason_code == "desktop_restore_source_reference_dangling"


@pytest.mark.parametrize("local_library", ["reserved_uri"], indirect=True)
def test_real_sqlite_snapshot_and_isolated_restore_escape_reserved_data_root(local_library):
    backup = _backup(local_library)
    result = _restore(local_library, backup, name="restored # space 中文 %")
    assert result.status == "VERIFIED"
    report = json.loads((Path(result.details["restore_root"]) / "restore-report.json").read_text(encoding="utf-8"))
    memory_path = Path(report["owner_paths"]["research_personal_memory"])
    with SQLitePersonalMemoryStore(memory_path) as store:
        service = PersonalMemoryService(store)
        assert service.get(local_library.memory_owner, local_library.disabled.memory_id).revision == local_library.disabled.revision
        assert service.get(local_library.memory_owner, local_library.disabled.memory_id).status is PersonalMemoryStatus.DISABLED
        assert service.get_policy(local_library.memory_owner).allow_remote_processing is False
    assert report["validation"]["owners"]["research_personal_memory"]["status"] == "verified"
    assert not list(local_library.config.data_root.glob("desktop-backup-*"))

