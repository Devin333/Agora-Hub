from __future__ import annotations

from pathlib import Path

from scripts.desktop_app.config import resolve_desktop_app_config
from framework.shared.local_desktop import CapabilityProjection, CapabilityState
from scripts.desktop_app.runtime import DesktopAppRuntime, _DesktopAppDoctor
from scripts.local_desktop.common import OperationResult
from scripts.local_desktop.doctor import DoctorCheck
from scripts.local_desktop.recovery import StorageInventory, StorageInventoryResolver, StorageOwner


class FakeReleaseManager:
    def build(self):
        return {"identity": {"release_id": "release-1"}}


class FakeBundleManager:
    def __init__(self) -> None:
        self.activations = 0

    def install_and_activate(self):
        self.activations += 1
        return {"binding": {"bundle_id": "desktop-test"}}


class FakeLifecycle:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self, *, open_browser: bool, readiness_timeout: float):
        assert open_browser is False
        self.starts += 1
        return OperationResult(True, "start", "READY", "desktop_started", "ready", {"start_count": self.starts})

    def status(self):
        return OperationResult(True, "status", "READY", "desktop_ready", "ready", {})

    def stop(self, *, drain_timeout: float, process_timeout: float):
        self.stops += 1
        return OperationResult(True, "stop", "STOPPED", "desktop_stopped", "stopped", {"drain_timeout": drain_timeout, "process_timeout": process_timeout})


class FakeDoctorReport:
    status = "warning"

    def to_dict(self):
        return {"status": self.status, "capabilities": []}


class FakeDoctor:
    def __init__(self) -> None:
        self.probe_network: list[bool] = []

    def run(self, *, probe_network: bool):
        self.probe_network.append(probe_network)
        return FakeDoctorReport()


class FakeInventoryResolver:
    def __init__(self, root: Path) -> None:
        self.require_existing: list[bool] = []
        self.inventory = StorageInventory(
            (
                StorageOwner("auth_users", root / "auth/users.json", "file", "newsroom_auth_users.v2", True, True, "json_lock"),
                StorageOwner(
                    "research_personal_memory",
                    root / "research/personal-memory.sqlite3",
                    "file",
                    "newsroom.personal-memory-store/v1",
                    False,
                    True,
                    "sqlite",
                ),
            )
        )

    def resolve(self, *, require_existing: bool = True):
        self.require_existing.append(require_existing)
        return self.inventory


class FakeBackupManager:
    def __init__(self) -> None:
        self.destinations: list[object] = []

    def create(self, *, destination=None):
        self.destinations.append(destination)
        return OperationResult(True, "backup", "READY", "desktop_backup_created", "created", {})




class FakeRuntimeConfigManager:
    def __init__(self, *, reject_admission: bool = False) -> None:
        self.reject_admission = reject_admission
        self.admissions = 0
        self.applies = 0
        self.recoveries = 0

    def admit(self, candidate, *, expected_revision, confirmation_token):
        self.admissions += 1
        if self.reject_admission:
            from scripts.local_desktop.common import DesktopOperationError

            raise DesktopOperationError("desktop_runtime_config_confirmation_invalid", "invalid confirmation")
        return {}

    def apply(self, candidate, *, expected_revision, confirmation_token):
        self.applies += 1
        return OperationResult(True, "config-apply", "APPLIED", "desktop_runtime_config_applied", "applied", {})

    def recover_pending(self):
        self.recoveries += 1
        return None


class FakeRestoreManager:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, bool, object]] = []

    def restore(self, backup, *, target_root=None, activate=False, personal_memory_confirmation=None):
        self.calls.append((backup, target_root, activate, personal_memory_confirmation))
        return OperationResult(True, "restore", "READY", "desktop_restore_complete", "restored", {})


def _runtime(tmp_path: Path, *, packaged: bool = False):
    resource = tmp_path / "runtime"
    resource.mkdir()
    if packaged:
        (resource / "desktop-bundle.json").write_text("{}", encoding="utf-8")
    config = resolve_desktop_app_config(resource_root=resource, data_root=tmp_path / "user-data", environ={})
    bundle = FakeBundleManager()
    lifecycle = FakeLifecycle()
    doctor = FakeDoctor()
    inventory = FakeInventoryResolver(config.data_root)
    backup = FakeBackupManager()
    restore = FakeRestoreManager()
    runtime = DesktopAppRuntime(
        config,
        release_manager=FakeReleaseManager(),
        bundle_manager=bundle,
        lifecycle=lifecycle,
        doctor=doctor,
        inventory_resolver=inventory,
        backup_manager=backup,
        restore_manager=restore,
    )
    return runtime, bundle, lifecycle, doctor, inventory, backup, restore


def test_packaged_start_activates_verified_bundle_then_reuses_lifecycle_on_reopen(tmp_path: Path) -> None:
    runtime, bundle, lifecycle, _doctor, _inventory, _backup, _restore = _runtime(tmp_path, packaged=True)

    first = runtime.start(timeout=15)
    second = runtime.start(timeout=15)

    assert first.ok and second.ok
    assert lifecycle.starts == 2
    assert bundle.activations == 2
    assert first.details["bundle_activation"]["binding"]["bundle_id"] == "desktop-test"
    assert (runtime.config.data_root / "logs").is_dir()
    assert not (runtime.config.data_root / "auth/users.json").exists()


def test_doctor_never_probes_network_by_default(tmp_path: Path) -> None:
    runtime, _bundle, _lifecycle, doctor, _inventory, _backup, _restore = _runtime(tmp_path)

    result = runtime.doctor()

    assert result.ok is True
    assert result.status == "WARNING"
    assert result.details["network_probe"] is False
    assert doctor.probe_network == [False]


def test_inventory_backup_restore_and_stop_delegate_to_existing_managers(tmp_path: Path) -> None:
    runtime, _bundle, lifecycle, _doctor, inventory, backup, restore = _runtime(tmp_path)

    inventory_result = runtime.inventory()
    backup_result = runtime.backup(destination=tmp_path / "backup.zip")
    confirmation = "restore-personal-memory:backup-20261014"
    restore_result = runtime.restore(
        backup=tmp_path / "backup.zip",
        target_root=tmp_path / "restore",
        activate=True,
        personal_memory_confirmation=confirmation,
    )
    stop_result = runtime.stop(timeout=20)

    assert inventory.require_existing == [False]
    assert inventory_result.status == "UNINITIALIZED"
    owners = {owner["owner_id"]: owner for owner in inventory_result.details["inventory"]["owners"]}
    assert owners["research_personal_memory"]["source_path"] == str((runtime.config.data_root / "research/personal-memory.sqlite3").resolve())
    assert owners["research_personal_memory"]["schema"] == "newsroom.personal-memory-store/v1"
    assert backup_result.reason_code == "desktop_backup_created"
    assert backup.destinations == [tmp_path / "backup.zip"]
    assert restore_result.reason_code == "desktop_restore_complete"
    assert restore.calls == [(tmp_path / "backup.zip", tmp_path / "restore", True, confirmation)]
    assert stop_result.status == "STOPPED"
    assert lifecycle.stops == 1



def test_real_inventory_owner_maps_personal_memory_to_owned_data_root(tmp_path: Path) -> None:
    resource = tmp_path / "runtime"
    resource.mkdir()
    config = resolve_desktop_app_config(resource_root=resource, data_root=tmp_path / "user-data", environ={})

    inventory = StorageInventoryResolver(config.desktop, environ=config.environment).resolve(require_existing=False)

    owner = next(item for item in inventory.owners if item.owner_id == "research_personal_memory")
    assert owner.path == (config.data_root / "research/personal-memory.sqlite3").resolve()
    assert owner.schema == "newsroom.personal-memory-store/v1"
    assert owner.consistency == "sqlite"


def test_config_apply_invalid_admission_does_not_stop_or_write(tmp_path: Path) -> None:
    runtime, _bundle, lifecycle, _doctor, _inventory, _backup, _restore = _runtime(tmp_path)
    manager = FakeRuntimeConfigManager(reject_admission=True)
    runtime.runtime_config_manager = manager

    import pytest
    from scripts.local_desktop.common import DesktopOperationError

    with pytest.raises(DesktopOperationError) as raised:
        runtime.config_apply(candidate={}, expected_revision=0, confirmation_token="bad")

    assert raised.value.reason_code == "desktop_runtime_config_confirmation_invalid"
    assert lifecycle.stops == 0
    assert manager.applies == 0


def test_config_apply_stop_failure_preserves_configuration_and_release(tmp_path: Path) -> None:
    runtime, _bundle, lifecycle, _doctor, _inventory, _backup, _restore = _runtime(tmp_path)
    manager = FakeRuntimeConfigManager()
    runtime.runtime_config_manager = manager

    def failed_stop(*, drain_timeout: float, process_timeout: float):
        lifecycle.stops += 1
        return OperationResult(False, "stop", "FAILED", "desktop_stop_failed", "failed", {})

    lifecycle.stop = failed_stop

    import pytest
    from scripts.local_desktop.common import DesktopOperationError

    with pytest.raises(DesktopOperationError) as raised:
        runtime.config_apply(candidate={}, expected_revision=0, confirmation_token="valid", timeout=5)

    assert raised.value.reason_code == "desktop_runtime_config_stop_failed"
    assert lifecycle.stops == 1
    assert manager.applies == 0


def test_desktop_app_doctor_allows_local_start_without_remote_business_configuration(tmp_path: Path, monkeypatch) -> None:
    resource = tmp_path / "runtime"
    resource.mkdir()
    config = resolve_desktop_app_config(resource_root=resource, data_root=tmp_path / "user-data", environ={})
    doctor = _DesktopAppDoctor(
        config.desktop,
        environ=config.environment,
        credential_provider=type("Credentials", (), {"configured": lambda *_args: False})(),
    )
    ok_check = DoctorCheck("local", "ok", "desktop_local_ready", "local service prerequisite is ready")
    for method_name in (
        "_profile_check",
        "_python_check",
        "_node_check",
        "_frontend_lock_check",
        "_release_check",
        "_storage_check",
    ):
        monkeypatch.setattr(doctor, method_name, lambda *_args, check=ok_check: check)
    monkeypatch.setattr(doctor, "_port_check", lambda *_args: ok_check)
    monkeypatch.setattr(
        doctor,
        "_offline_capabilities",
        lambda: (
            CapabilityProjection("real_source", True, CapabilityState.NO, reason_code="desktop_source_config_missing"),
            CapabilityProjection("pdf_import", True, CapabilityState.YES),
            CapabilityProjection("single_paper_analysis", True, CapabilityState.NO, reason_code="desktop_llm_not_configured"),
            CapabilityProjection("paper_question_answering", True, CapabilityState.NO, reason_code="desktop_llm_not_configured"),
            CapabilityProjection("artifact_storage", True, CapabilityState.YES),
        ),
    )

    report = doctor.run()

    assert report.status == "warning"
    by_id = {item.capability_id: item for item in report.capabilities}
    assert by_id["single_paper_analysis"].configured is CapabilityState.NO
    assert by_id["single_paper_analysis"].verified is CapabilityState.UNKNOWN
    assert by_id["paper_question_answering"].reason_code == "desktop_llm_not_configured"


def test_desktop_app_doctor_keeps_local_service_and_parser_failures_fatal(tmp_path: Path, monkeypatch) -> None:
    resource = tmp_path / "runtime"
    resource.mkdir()
    config = resolve_desktop_app_config(resource_root=resource, data_root=tmp_path / "user-data", environ={})
    doctor = _DesktopAppDoctor(
        config.desktop,
        environ=config.environment,
        credential_provider=type("Credentials", (), {"configured": lambda *_args: False})(),
    )
    ok_check = DoctorCheck("local", "ok", "desktop_local_ready", "local service prerequisite is ready")
    for method_name in (
        "_profile_check",
        "_python_check",
        "_node_check",
        "_frontend_lock_check",
        "_release_check",
        "_storage_check",
    ):
        monkeypatch.setattr(doctor, method_name, lambda *_args, check=ok_check: check)
    monkeypatch.setattr(doctor, "_port_check", lambda *_args: ok_check)
    monkeypatch.setattr(
        doctor,
        "_offline_capabilities",
        lambda: (
            CapabilityProjection("pdf_import", True, CapabilityState.NO, reason_code="desktop_pdf_parser_missing"),
            CapabilityProjection("artifact_storage", True, CapabilityState.YES),
            CapabilityProjection("single_paper_analysis", True, CapabilityState.NO, reason_code="desktop_llm_not_configured"),
        ),
    )

    assert doctor.run().status == "error"

    monkeypatch.setattr(
        doctor,
        "_storage_check",
        lambda: DoctorCheck("storage", "error", "desktop_storage_unavailable", "storage unavailable"),
    )
    monkeypatch.setattr(
        doctor,
        "_offline_capabilities",
        lambda: (
            CapabilityProjection("pdf_import", True, CapabilityState.YES),
            CapabilityProjection("artifact_storage", True, CapabilityState.YES),
        ),
    )
    assert doctor.run().status == "error"


def test_actual_lifecycle_provider_does_not_read_retained_vault_when_llm_is_null(tmp_path: Path, monkeypatch) -> None:
    from scripts.desktop_app.credentials import FIXED_CREDENTIAL_ENV

    class RetainedVault:
        def __init__(self):
            self.reads = 0

        def read(self, descriptor):
            self.reads += 1
            return "retained-fake-key"

    resource = tmp_path / "runtime"
    resource.mkdir()
    config = resolve_desktop_app_config(resource_root=resource, data_root=tmp_path / "data", environ={})
    vault = RetainedVault()
    runtime = DesktopAppRuntime(config, credential_vault=vault)
    monkeypatch.setenv(FIXED_CREDENTIAL_ENV, "inherited-global-key")

    assert runtime.config.environment["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "0"
    assert runtime.lifecycle.api_environment_provider() == {}
    assert runtime.lifecycle._child_environment("api", {FIXED_CREDENTIAL_ENV: "stale"}) == {}
    assert vault.reads == 0
    runtime._reproject_runtime_configuration()
    assert runtime.lifecycle.api_environment_provider() == {}
    assert vault.reads == 0


def test_actual_lifecycle_provider_projects_vault_only_after_owner_enables_llm(tmp_path: Path) -> None:
    import json

    from scripts.desktop_app.credentials import FIXED_CREDENTIAL_ENV, FIXED_CREDENTIAL_REF
    from scripts.local_desktop.runtime_config import (
        candidate_document, models_config_payload, runtime_config_paths_for_data_root,
    )

    class RetainedVault:
        def __init__(self):
            self.reads = 0

        def read(self, descriptor):
            self.reads += 1
            return "enabled-fake-key"

    resource = tmp_path / "runtime"
    resource.mkdir()
    data = tmp_path / "data"
    config = resolve_desktop_app_config(resource_root=resource, data_root=data, environ={})
    vault = RetainedVault()
    runtime = DesktopAppRuntime(config, credential_vault=vault)
    document = candidate_document({"llm": {
        "route_id": "app-guided", "provider": "openai-compatible", "model": "app-model",
        "base_url": "https://app.example.test/v1", "credential_ref": FIXED_CREDENTIAL_REF,
        "credential_env": FIXED_CREDENTIAL_ENV,
    }}, revision=1)
    paths = runtime_config_paths_for_data_root(data)
    paths.document.parent.mkdir(parents=True, exist_ok=True)
    paths.document.write_text(json.dumps(document), encoding="utf-8")
    paths.models.write_text(json.dumps(models_config_payload(document)), encoding="utf-8")
    runtime._reproject_runtime_configuration()

    assert runtime.config.environment["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "1"
    assert runtime.lifecycle.api_environment_provider() == {FIXED_CREDENTIAL_ENV: "enabled-fake-key"}
    assert vault.reads == 1
    assert runtime.lifecycle._child_environment("frontend", {FIXED_CREDENTIAL_ENV: "stale"}) == {}
    assert vault.reads == 1

    previous_provider = runtime.lifecycle.api_environment_provider
    disabled = candidate_document({"llm": None}, revision=2)
    paths.document.write_text(json.dumps(disabled), encoding="utf-8")
    runtime._reproject_runtime_configuration()
    assert runtime.lifecycle.api_environment_provider() == {}
    assert previous_provider() == {}
    assert vault.reads == 1
