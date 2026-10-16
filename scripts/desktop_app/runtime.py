from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.desktop_app.bundle import DesktopAppReleaseManager, PortableBundleManager, stage_portable_bundle
from scripts.desktop_app.config import DesktopAppConfig, controlled_process_environment, initialize_data_root, resolve_desktop_app_config
from scripts.desktop_app.owned_clear import OwnedDataClearOwner
from scripts.desktop_app.credentials import (
    AppCredentialProvider,
    CredentialError,
    CredentialVault,
    FIXED_CREDENTIAL_ENV,
    FIXED_CREDENTIAL_REF,
    delete_slot,
    inspect_slot,
    replace_slot_with_native_prompt,
)
from scripts.local_desktop.common import DesktopOperationError, OperationResult
from scripts.local_desktop.doctor import LocalDesktopDoctor
from scripts.local_desktop.lifecycle_cancellation import check_startup_cancellation
from scripts.local_desktop.processes import LifecycleManager
from scripts.local_desktop.recovery import BackupManager, RestoreManager, StorageInventoryResolver
from scripts.local_desktop.runtime_config import RuntimeConfigManager
from framework.llm import LLMConfigurationError, load_openai_compatible_deployment
from interfaces.composition.research_settings import DESKTOP_RESEARCH_LLM_ENABLED_ENV


class _DesktopAppDoctor(LocalDesktopDoctor):
    """Apply the App startup profile without overstating remote business readiness."""

    _STARTUP_REQUIRED_CAPABILITIES = frozenset({"pdf_import", "artifact_storage"})

    def __init__(self, config, *, environ, credential_provider) -> None:
        super().__init__(
            config,
            environ=environ,
            startup_required_capability_ids=self._STARTUP_REQUIRED_CAPABILITIES,
        )
        self.credential_provider = credential_provider

    def _llm_configured(self, settings) -> bool:
        try:
            deployment = load_openai_compatible_deployment(
                settings.llm.models_config_path,
                route_id=settings.llm.route_id,
                apply_environment_overrides=False,
            )
            return (
                deployment.config.model == settings.llm.model
                and deployment.config.base_url.rstrip("/") == settings.llm.base_url.rstrip("/")
                and deployment.config.api_key_env == FIXED_CREDENTIAL_ENV
                and self.credential_provider.configured(FIXED_CREDENTIAL_REF)
            )
        except (LLMConfigurationError, CredentialError, ImportError, OSError, RuntimeError, TypeError, ValueError):
            return False


class DesktopAppRuntime:
    def __init__(
        self,
        config: DesktopAppConfig,
        *,
        release_manager: Any | None = None,
        bundle_manager: PortableBundleManager | None = None,
        doctor: LocalDesktopDoctor | None = None,
        lifecycle: LifecycleManager | None = None,
        inventory_resolver: StorageInventoryResolver | None = None,
        backup_manager: BackupManager | None = None,
        restore_manager: RestoreManager | None = None,
        runtime_config_manager: RuntimeConfigManager | None = None,
        credential_vault: CredentialVault | None = None,
    ) -> None:
        self.config = config
        self._owns_doctor = doctor is None
        self._owns_lifecycle = lifecycle is None
        self._owns_inventory = inventory_resolver is None
        self._owns_backup = backup_manager is None
        self._owns_restore = restore_manager is None
        self.credential_vault = credential_vault
        self.credential_provider = AppCredentialProvider(config.data_root, vault=credential_vault)
        self.release_manager = release_manager or DesktopAppReleaseManager(config.desktop)
        self.bundle_manager = bundle_manager or PortableBundleManager(config, release_manager=self.release_manager)
        self.doctor_manager = doctor or _DesktopAppDoctor(
            config.desktop,
            environ=config.environment,
            credential_provider=self.credential_provider,
        )
        self.lifecycle = lifecycle or LifecycleManager(
            config.desktop,
            release_manager=self.release_manager,
            doctor=self.doctor_manager,
            api_environment_provider=self._api_environment,
            api_environment_names=(FIXED_CREDENTIAL_ENV,),
        )
        self.inventory_resolver = inventory_resolver or StorageInventoryResolver(config.desktop, environ=config.environment)
        self.backup_manager = backup_manager or BackupManager(
            config.desktop,
            inventory_resolver=self.inventory_resolver,
            release_manager=self.release_manager,
            lifecycle_status=lambda: self.lifecycle.status().status,
        )
        self.restore_manager = restore_manager or RestoreManager(
            config.desktop,
            inventory_resolver=self.inventory_resolver,
            release_manager=self.release_manager,
            lifecycle_status=lambda: self.lifecycle.status().status,
        )
        self.runtime_config_manager = runtime_config_manager or RuntimeConfigManager(
            config,
            release_manager=self.release_manager,
            credential_provider=self.credential_provider,
        )

    def _api_environment(self) -> Mapping[str, str]:
        # Only the configuration owner's explicit enabled projection authorizes a
        # vault read. A retained slot must not reactivate llm=null on next launch.
        if self.config.environment.get(DESKTOP_RESEARCH_LLM_ENABLED_ENV) != "1":
            return {}
        return self.credential_provider.project_environment(
            FIXED_CREDENTIAL_REF,
            environment_name=FIXED_CREDENTIAL_ENV,
        )

    def _clear_owner(self) -> OwnedDataClearOwner:
        return OwnedDataClearOwner(
            resource_root=self.config.resource_root,
            data_root=self.config.data_root,
            credential_vault=self.credential_vault,
        )

    def start(self, *, timeout: float = 60.0) -> OperationResult:
        check_startup_cancellation()
        owner = self._clear_owner() if self.config.data_root.name.casefold() == "runtime" else None
        guard_held = bool(getattr(self, "_desktop_clear_guard_held", False))
        guard = owner.startup_guard() if owner is not None and not guard_held else nullcontext()
        with guard:
            check_startup_cancellation()
            clear_registered = owner.register_for_start(lock_held=True) if owner is not None else None
            check_startup_cancellation()
            initialize_data_root(self.config)
            check_startup_cancellation()
            configuration_recovery = self.runtime_config_manager.recover_pending()
            check_startup_cancellation()
            self._reproject_runtime_configuration()
            check_startup_cancellation()
            with controlled_process_environment(self.config):
                check_startup_cancellation()
                activation = self.bundle_manager.install_and_activate() if self.config.packaged else None
                check_startup_cancellation()
                result = self.lifecycle.start(open_browser=False, readiness_timeout=timeout)
            details = dict(result.details)
            if configuration_recovery is not None:
                details["runtime_configuration_recovery"] = configuration_recovery
            if activation is not None:
                details["bundle_activation"] = activation
            if clear_registered is not None:
                details["clearAvailable"] = clear_registered
                details["markerRegistered"] = clear_registered
            return OperationResult(result.ok, result.action, result.status, result.reason_code, result.message, details, result.exit_code)

    def _reproject_runtime_configuration(self) -> None:
        refreshed = resolve_desktop_app_config(
            resource_root=self.config.resource_root,
            data_root=self.config.data_root,
            frontend_port=self.config.desktop.frontend_port,
            api_port=self.config.desktop.api_port,
            environ=self.config.environment,
        )
        self.config = refreshed
        self.bundle_manager.config = refreshed
        self.runtime_config_manager.config = refreshed
        if self._owns_doctor:
            self.doctor_manager = _DesktopAppDoctor(
                refreshed.desktop,
                environ=refreshed.environment,
                credential_provider=self.credential_provider,
            )
        if self._owns_lifecycle:
            self.lifecycle = LifecycleManager(
                refreshed.desktop,
                release_manager=self.release_manager,
                doctor=self.doctor_manager,
                api_environment_provider=self._api_environment,
                api_environment_names=(FIXED_CREDENTIAL_ENV,),
            )
        if self._owns_inventory:
            self.inventory_resolver = StorageInventoryResolver(refreshed.desktop, environ=refreshed.environment)
        if self._owns_backup:
            self.backup_manager = BackupManager(
                refreshed.desktop,
                inventory_resolver=self.inventory_resolver,
                release_manager=self.release_manager,
                lifecycle_status=lambda: self.lifecycle.status().status,
            )
        if self._owns_restore:
            self.restore_manager = RestoreManager(
                refreshed.desktop,
                inventory_resolver=self.inventory_resolver,
                release_manager=self.release_manager,
                lifecycle_status=lambda: self.lifecycle.status().status,
            )

    def data_clear_preview(self, *, timeout: float = 30.0) -> OperationResult:
        owner = self._clear_owner()
        owner.validate_registered_profile(allow_partial=True)
        return owner.preview(stop_runtime=lambda bounded: self.stop(timeout=bounded), timeout=timeout)

    def data_clear_apply(self, *, request: Mapping[str, Any]) -> OperationResult:
        return self._clear_owner().apply(request)

    def data_clear_status(self) -> OperationResult:
        return self._clear_owner().status()

    def data_clear_finalize(
        self,
        *,
        request: Mapping[str, Any],
        ready: Callable[[str, str], None],
    ) -> OperationResult:
        return self._clear_owner().finalize(request, ready=ready)

    def status(self) -> OperationResult:
        with controlled_process_environment(self.config):
            return self.lifecycle.status()

    def stop(self, *, timeout: float = 30.0) -> OperationResult:
        # The host passes 120 seconds for drain admission. Process termination uses
        # one shared topology deadline capped at 30 seconds, so the Python owner
        # budget is at most 150 seconds before final endpoint/state verification.
        with controlled_process_environment(self.config):
            return self.lifecycle.stop(drain_timeout=timeout, process_timeout=min(timeout, 30.0))

    def doctor(self) -> OperationResult:
        # Explicitly offline: LocalDesktopDoctor only probes network when
        # probe_network=True. Desktop App never changes that default implicitly.
        with controlled_process_environment(self.config):
            report = self.doctor_manager.run(probe_network=False)
        ok = report.status != "error"
        return OperationResult(
            ok,
            "doctor",
            report.status.upper(),
            "desktop_doctor_ready" if ok else "desktop_doctor_failed",
            "offline desktop diagnostics completed",
            {
                "report": report.to_dict(),
                "network_probe": False,
            },
            0 if ok else 2,
        )

    def inventory(self) -> OperationResult:
        inventory = self.inventory_resolver.resolve(require_existing=False)
        missing = [owner.owner_id for owner in inventory.owners if owner.required and not owner.path.exists()]
        return OperationResult(
            True,
            "inventory",
            "READY" if not missing else "UNINITIALIZED",
            "desktop_inventory_resolved",
            "desktop storage inventory was resolved without fabricating business records",
            {
                "inventory": inventory.to_dict(),
                "missing_required_owner_ids": missing,
            },
        )

    def backup(self, *, destination: str | Path | None = None) -> OperationResult:
        initialize_data_root(self.config)
        with controlled_process_environment(self.config):
            return self.backup_manager.create(destination=destination)

    def restore(
        self,
        *,
        backup: str | Path,
        target_root: str | Path | None = None,
        activate: bool = False,
        personal_memory_confirmation: str | None = None,
    ) -> OperationResult:
        initialize_data_root(self.config)
        with controlled_process_environment(self.config):
            return self.restore_manager.restore(
                backup,
                target_root=target_root,
                activate=activate,
                personal_memory_confirmation=personal_memory_confirmation,
            )

    def credential_inspect(self) -> OperationResult:
        try:
            presence = inspect_slot(self.config.data_root, vault=self.credential_vault)
        except CredentialError as exc:
            raise DesktopOperationError(exc.code, str(exc)) from exc
        return OperationResult(
            True,
            "credential-inspect",
            "CONFIGURED" if presence.present else "UNCONFIGURED",
            "desktop_credential_inspected",
            "desktop Research credential presence was inspected without reading its value",
            {"configured": presence.present},
        )

    def credential_prompt(self, *, parent_window: int) -> OperationResult:
        if isinstance(parent_window, bool) or not isinstance(parent_window, int) or not 1 <= parent_window <= (2**63 - 1):
            raise DesktopOperationError("desktop_credential_parent_invalid", "native parent window is invalid")
        try:
            result = replace_slot_with_native_prompt(
                self.config.data_root,
                parent_window=parent_window,
                vault=self.credential_vault,
            )
        except CredentialError as exc:
            raise DesktopOperationError(exc.code, str(exc)) from exc
        return OperationResult(
            True,
            "credential-prompt",
            "CANCELLED" if result.cancelled else "CONFIGURED",
            "desktop_credential_prompt_cancelled" if result.cancelled else "desktop_credential_replaced",
            "native credential prompt was cancelled" if result.cancelled else "desktop Research credential was replaced",
            {"configured": result.present, "changed": result.changed, "cancelled": result.cancelled},
        )

    def credential_delete(self, *, confirmation_token: str, timeout: float = 30.0) -> OperationResult:
        if confirmation_token != "delete-research-credential":
            raise DesktopOperationError(
                "desktop_credential_delete_confirmation_invalid",
                "explicit credential deletion confirmation is required",
            )
        stop_result = self.stop(timeout=timeout)
        if not stop_result.ok or stop_result.status != "STOPPED":
            raise DesktopOperationError(
                "desktop_credential_delete_stop_failed",
                "owned desktop runtime must stop before deleting its launch credential",
            )
        try:
            presence = delete_slot(self.config.data_root, vault=self.credential_vault)
        except CredentialError as exc:
            raise DesktopOperationError(exc.code, str(exc)) from exc
        return OperationResult(
            True,
            "credential-delete",
            "UNCONFIGURED",
            "desktop_credential_deleted",
            "desktop Research credential was deleted after the owned runtime stopped",
            {
                "configured": presence.present,
                "lifecycle": {"status": stop_result.status, "reason_code": stop_result.reason_code},
            },
        )

    def config_show(self) -> OperationResult:
        return self.runtime_config_manager.show()

    def config_preview(self, *, candidate: Mapping[str, Any]) -> OperationResult:
        initialize_data_root(self.config)
        return self.runtime_config_manager.preview(candidate)

    def config_apply(
        self,
        *,
        candidate: Mapping[str, Any],
        expected_revision: int,
        confirmation_token: str,
        timeout: float = 30.0,
    ) -> OperationResult:
        self.runtime_config_manager.admit(
            candidate,
            expected_revision=expected_revision,
            confirmation_token=confirmation_token,
        )
        initialize_data_root(self.config)
        stop_result = self.stop(timeout=timeout)
        if not stop_result.ok or stop_result.status != "STOPPED":
            raise DesktopOperationError("desktop_runtime_config_stop_failed", "owned desktop runtime must stop before applying configuration")
        with controlled_process_environment(self.config):
            result = self.runtime_config_manager.apply(
                candidate,
                expected_revision=expected_revision,
                confirmation_token=confirmation_token,
            )
        self._reproject_runtime_configuration()
        details = dict(result.details)
        details["lifecycle"] = {"status": stop_result.status, "reason_code": stop_result.reason_code}
        details["restart_required"] = True
        return OperationResult(result.ok, result.action, result.status, result.reason_code, result.message, details, result.exit_code)

    def build(
        self,
        *,
        bundle_output: str | Path | None = None,
        portable_python_root: str | Path | None = None,
        portable_node_root: str | Path | None = None,
        release: str | Path | None = None,
    ) -> OperationResult:
        initialize_data_root(self.config)
        with controlled_process_environment(self.config):
            if self.config.packaged:
                activation = self.bundle_manager.install_and_activate()
                return OperationResult(
                    True,
                    "build",
                    "READY",
                    "desktop_app_bundle_activated",
                    "verified desktop app bundle was bound and activated locally",
                    activation,
                )
            if bundle_output is not None:
                if portable_python_root is None or portable_node_root is None:
                    raise DesktopOperationError("desktop_app_portable_runtime_inputs_missing", "portable Python and Node roots are required for bundle staging")
                selected_release = release
                if selected_release is None:
                    selected_release, _manifest = self.release_manager.active()
                envelope = stage_portable_bundle(
                    output_root=bundle_output,
                    source_release=selected_release,
                    python_root=portable_python_root,
                    node_root=portable_node_root,
                    release_manager=self.release_manager,
                )
                return OperationResult(
                    True,
                    "build",
                    "STAGED",
                    "desktop_app_bundle_staged",
                    "portable desktop app bundle was staged from verified runtime inputs",
                    {"bundle_id": envelope["bundle_id"], "bundle_output": str(Path(bundle_output).resolve(strict=False))},
                )
            manifest = self.release_manager.build()
            return OperationResult(
                True,
                "build",
                "READY",
                "desktop_release_built",
                "development release build completed and activated",
                {"release": manifest.get("identity", {})},
            )


__all__ = ["DesktopAppRuntime"]
