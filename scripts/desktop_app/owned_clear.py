from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.desktop_app.credentials import CredentialError, CredentialVault, delete_slot, inspect_slot
from scripts.local_desktop.common import (
    DesktopOperationError,
    OperationResult,
    atomic_write_json,
    exclusive_lock,
    protect_private_path,
    read_json_object,
    reject_reparse_path,
    windows_extended_path,
)
from scripts.local_desktop.processes import ProcessIdentity, ProcessInspector

CLEAR_CONFIRMATION_TEXT = "删除本机全部 Agora 数据"
MARKER_SCHEMA = "agorahub.desktop-owned-profile/v1"
JOURNAL_SCHEMA = "agorahub.desktop-owned-clear-journal/v1"
RECEIPT_SCHEMA = "agorahub.desktop-owned-clear-receipt/v1"
MARKER_NAME = ".agora-owned-profile.json"
EXTERNAL_OWNER_DIRECTORY = ".agora-paper-agent-clear"
PREVIEW_TTL_SECONDS = 300
FINALIZER_TTL_SECONDS = 600
FINALIZER_WAIT_SECONDS = 120.0
TREE_OPERATION_SECONDS = 60.0
FINALIZER_DELETE_SECONDS = 180.0
RELEASE_MANIFEST_MAX_BYTES = 64 * 1024 * 1024
RELEASE_MANIFEST_CHUNK_BYTES = 256 * 1024
RELEASE_MANIFEST_HASH_SECONDS = 30.0
_MAX_EPOCH_SECONDS = 253402300799.0
BLOCKING_STATES = frozenset({"PREPARED", "FINALIZING", "FAILED_PARTIAL"})
VOLATILE_CLASSES = (
    "Electron browser session/storage/cache/preferences",
    "Electron/main-process host audit and log tail",
)
_HOST_VOLATILE_NAMES = frozenset(
    name.casefold()
    for name in (
        "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
        "GrShaderCache", "GraphiteDawnCache", "GPUPersistentCache", "ShaderCache", "Crashpad", "Network", "Sessions", "Session Storage",
        "Local Storage", "IndexedDB", "Service Worker", "Storage", "WebStorage", "Shared Dictionary",
        "SharedStorage", "blob_storage", "databases", "dictionaries", "logs", "Cookies", "Cookies-journal",
        "Preferences", "Secure Preferences", "Local State", "TransportSecurity", "DIPS", "DIPS-journal", "QuotaManager",
        "QuotaManager-journal", "VideoDecodeStats", "BrowserMetrics", "DevToolsActivePort", "CertificateRevocation",
        "AutofillStrikeDatabase", "OriginTrials", "segmentation_platform", "declarative_performance_observer.db", "declarative_performance_observer.db-journal", "declarative_performance_observer.db-wal", "declarative_performance_observer.db-shm", "component_crx_cache",
        "extensions_crx_cache", "SingletonCookie", "SingletonLock", "SingletonSocket",
    )
)

@dataclass(frozen=True)
class ClearPaths:
    resource_root: Path
    runtime_root: Path
    profile_root: Path
    marker: Path
    external_root: Path
    journal: Path
    receipt: Path
    lock: Path


class OwnedDataClearOwner:
    """Own the fixed current Electron profile; no arbitrary delete target is accepted."""

    def __init__(
        self,
        *,
        resource_root: str | os.PathLike[str],
        data_root: str | os.PathLike[str],
        credential_vault: CredentialVault | None = None,
        process_inspector: ProcessInspector | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        preview_ttl_seconds: int = PREVIEW_TTL_SECONDS,
        finalizer_ttl_seconds: int = FINALIZER_TTL_SECONDS,
        finalizer_wait_seconds: float = FINALIZER_WAIT_SECONDS,
    ) -> None:
        self.paths = clear_paths(resource_root=resource_root, data_root=data_root)
        self.credential_vault = credential_vault
        self.process_inspector = process_inspector or ProcessInspector()
        self._now = now
        self._sleep = sleep
        self.preview_ttl_seconds = _positive_int(preview_ttl_seconds, "preview_ttl_seconds")
        self.finalizer_ttl_seconds = _positive_int(finalizer_ttl_seconds, "finalizer_ttl_seconds")
        self.finalizer_wait_seconds = _positive_float(finalizer_wait_seconds, "finalizer_wait_seconds")

    @contextmanager
    def normal_command_guard(self, *, writer: bool) -> Iterator[None]:
        _validate_static_paths(self.paths)
        with _owner_lock(self.paths):
            journal = _read_journal(self.paths, missing_ok=True)
            state = str(journal.get("state") or "IDLE")
            maintenance_state = _maintenance_state(journal)
            if maintenance_state in BLOCKING_STATES:
                raise _fence_error(maintenance_state, journal)
            if writer and state == "PREVIEW":
                marker = _read_marker(self.paths)
                _write_idle(self.paths, marker, self._now(), "normal-writer-invalidated-preview")
            yield

    def startup_guard(self):
        return self.normal_command_guard(writer=True)

    def register_for_start(self, *, lock_held: bool = False) -> bool:
        if lock_held:
            return self._register_for_start_locked()
        with self.startup_guard():
            return self._register_for_start_locked()

    def _register_for_start_locked(self) -> bool:
        paths = self.paths
        state = _read_journal(paths, missing_ok=True)
        maintenance_state = _maintenance_state(state)
        if maintenance_state in BLOCKING_STATES:
            raise _fence_error(maintenance_state, state)
        if _lexists(paths.marker):
            marker = _read_marker(paths, check_release=False)
            marker["releaseBinding"] = _release_binding(paths.resource_root)
            marker["updatedAt"] = _timestamp(self._now())
            atomic_write_json(paths.marker, marker)
            protect_private_path(paths.marker)
            if state and state.get("state") in {"PREVIEW", "CLEARED"}:
                _write_idle(paths, marker, self._now(), "normal-start")
            return True
        if _lexists(paths.runtime_root):
            return False
        _ensure_profile_directory(paths.profile_root)
        paths.runtime_root.mkdir()
        protect_private_path(paths.runtime_root)
        marker = {
            "schema": MARKER_SCHEMA,
            "generation": uuid4().hex,
            "profilePath": os.fspath(paths.profile_root),
            "runtimePath": os.fspath(paths.runtime_root),
            "profileIdentity": _directory_identity(paths.profile_root),
            "runtimeIdentity": _directory_identity(paths.runtime_root),
            "releaseBinding": _release_binding(paths.resource_root),
            "createdAt": _timestamp(self._now()),
            "updatedAt": _timestamp(self._now()),
        }
        atomic_write_json(paths.marker, marker)
        protect_private_path(paths.marker)
        _write_idle(paths, marker, self._now(), "registered")
        return True


    def validate_registered_profile(self, *, allow_partial: bool = False) -> dict[str, Any]:
        _validate_static_paths(self.paths)
        journal = _read_journal(self.paths, missing_ok=True)
        if allow_partial and (
            journal.get("state") in {"PREPARED", "FINALIZING", "FAILED_PARTIAL"}
            or (journal.get("state") == "PREVIEW" and journal.get("retryOf"))
        ):
            _validate_journal(self.paths, journal)
            return journal
        return _read_marker(self.paths)

    def preview(
        self,
        *,
        stop_runtime: Callable[[float], OperationResult] | None,
        timeout: float,
    ) -> OperationResult:
        paths = self.paths
        _validate_static_paths(paths)
        with _owner_lock(paths):
            previous = _read_journal(paths, missing_ok=True)
            state = previous.get("state")
            if state == "FINALIZING":
                if _recorded_finalizer_active(previous, self.process_inspector):
                    raise _fence_error("FINALIZING", previous)
                previous = {
                    **previous,
                    "revision": int(previous["revision"]) + 1,
                    "state": "FAILED_PARTIAL",
                    "failedAt": _timestamp(self._now()),
                    "failure": {"code": "desktop_data_clear_finalizer_abandoned", "message": "finalizer exited before verified completion; explicit reconfirmation is required"},
                    "finalizerCapabilityHash": None,
                }
                _write_journal(paths, previous)
                state = "FAILED_PARTIAL"
            retry = state in {"PREPARED", "FAILED_PARTIAL"} or (state == "PREVIEW" and bool(previous.get("retryOf")))
            if retry:
                marker = _validate_partial_binding(paths, previous)
            else:
                marker = _read_marker(paths)
                if stop_runtime is None:
                    raise DesktopOperationError(
                        "desktop_data_clear_stop_owner_missing",
                        "owned runtime stop control is required before preview",
                    )
                stopped = stop_runtime(timeout)
                if not stopped.ok or stopped.status != "STOPPED":
                    raise DesktopOperationError(
                        "desktop_data_clear_stop_failed",
                        "owned runtime did not reach verified STOPPED",
                        details={"state": stopped.status, "code": stopped.reason_code},
                    )
                marker = _read_marker(paths)
            _assert_no_recorded_processes(paths.runtime_root, self.process_inspector)
            snapshot = _snapshot(paths, allow_missing=retry)
            preview_id = secrets.token_urlsafe(32)
            expires = self._now() + self.preview_ttl_seconds
            journal = {
                "schema": JOURNAL_SCHEMA,
                "revision": int(previous.get("revision") or 0) + 1,
                "state": "PREVIEW",
                "profileKey": _profile_key(paths.profile_root),
                "profilePath": os.fspath(paths.profile_root),
                "runtimePath": os.fspath(paths.runtime_root),
                "generation": str(marker.get("generation") or previous.get("generation") or ""),
                "profileIdentity": snapshot["profileIdentity"],
                "runtimeIdentity": snapshot["runtimeIdentity"],
                "releaseBinding": _release_binding(paths.resource_root),
                "businessSnapshot": snapshot["businessSnapshot"],
                "volatileTopLevel": snapshot["volatileTopLevel"],
                "scopeCount": snapshot["scopeCount"],
                "scopeBytes": snapshot["scopeBytes"],
                "previewIdHash": _secret_hash(preview_id),
                "createdAt": _timestamp(self._now()),
                "expiresAt": _timestamp(expires),
                "expiresEpoch": expires,
                "retryOf": (previous.get("retryOf") or previous.get("operationId")) if retry else None,
            }
            _write_journal(paths, journal)
            return OperationResult(
                True,
                "data-clear-preview",
                "CONFIRMATION_REQUIRED",
                "desktop_data_clear_preview_ready",
                _public_message("desktop_data_clear_preview_ready"),
                {
                    "previewId": preview_id,
                    "expiresAt": journal["expiresAt"],
                    "scope": _public_scope(paths.profile_root, snapshot),
                    "stopped": True,
                },
            )

    def apply(self, request: Mapping[str, Any]) -> OperationResult:
        parsed = _parse_apply(request)
        if parsed["confirmationText"] != CLEAR_CONFIRMATION_TEXT:
            raise DesktopOperationError(
                "desktop_data_clear_confirmation_mismatch",
                "the exact native confirmation phrase is required; no data was changed",
            )
        paths = self.paths
        _validate_static_paths(paths)
        with _owner_lock(paths):
            journal = _read_journal(paths)
            if journal.get("state") != "PREVIEW":
                raise DesktopOperationError(
                    "desktop_data_clear_preview_replayed",
                    "the data-clear preview is absent, consumed, or no longer applicable",
                )
            if self._now() > _number(journal.get("expiresEpoch"), "expiresEpoch"):
                raise DesktopOperationError(
                    "desktop_data_clear_preview_expired",
                    "the data-clear preview expired; create a new preview",
                )
            if not secrets.compare_digest(
                str(journal.get("previewIdHash") or ""), _secret_hash(parsed["previewId"])
            ):
                raise DesktopOperationError(
                    "desktop_data_clear_preview_mismatch",
                    "the data-clear preview identity does not match",
                )
            _validate_binding(paths, journal, allow_missing=journal.get("retryOf") is not None)
            current = _snapshot(paths, allow_missing=journal.get("retryOf") is not None)
            if current["runtimeIdentity"] != journal.get("runtimeIdentity"):
                raise DesktopOperationError(
                    "desktop_data_clear_runtime_replaced",
                    "owned runtime directory identity changed after preview",
                )
            if current["businessSnapshot"] != journal.get("businessSnapshot"):
                raise DesktopOperationError(
                    "desktop_data_clear_business_changed",
                    "owned business runtime changed after preview; create a new preview",
                )
            allowed = set(journal.get("volatileTopLevel") or ()) | _HOST_VOLATILE_NAMES
            if any(name.casefold() not in allowed for name in current["volatileTopLevel"]):
                raise DesktopOperationError(
                    "desktop_data_clear_unknown_profile_change",
                    "an unknown top-level profile owner appeared after preview",
                )
            _assert_no_recorded_processes(paths.runtime_root, self.process_inspector)
            electron = _verify_live_electron(parsed["electron"], self.process_inspector)
            operation_id = uuid4().hex
            capability = secrets.token_urlsafe(48)
            expires = self._now() + self.finalizer_ttl_seconds
            prepared = {
                **journal,
                "revision": int(journal["revision"]) + 1,
                "state": "PREPARED",
                "operationId": operation_id,
                "previewIdHash": None,
                "preparedAt": _timestamp(self._now()),
                "finalizerExpiresAt": _timestamp(expires),
                "finalizerExpiresEpoch": expires,
                "finalizerCapabilityHash": _secret_hash(capability),
                "electron": electron,
                "failure": None,
            }
            _write_journal(paths, prepared)
            return OperationResult(
                True,
                "data-clear-apply",
                "PREPARED",
                "desktop_data_clear_prepared",
                _public_message("desktop_data_clear_prepared"),
                {"operationId": operation_id, "exitRequired": True, "finalizerCapability": capability},
            )

    def status(self) -> OperationResult:
        paths = self.paths
        _validate_static_paths(paths, require_resource=False)
        journal = _read_journal(paths, missing_ok=True)
        if not journal:
            return OperationResult(True, "data-clear-status", "IDLE", "desktop_data_clear_idle", _public_message("desktop_data_clear_idle"), {})
        state = str(journal.get("state") or "UNKNOWN")
        details: dict[str, Any] = {}
        if isinstance(journal.get("operationId"), str):
            details["operationId"] = journal["operationId"]
        if state == "CLEARED":
            if isinstance(journal.get("completedAt"), str):
                details["completedAt"] = journal["completedAt"]
            return OperationResult(True, "data-clear-status", state, "desktop_data_clear_complete", _public_message("desktop_data_clear_complete"), details)
        if state == "PREVIEW" and journal.get("retryOf"):
            details["operationId"] = str(journal["retryOf"])
            code = "desktop_data_clear_preview_expired" if self._now() > _number(journal.get("expiresEpoch"), "expiresEpoch") else "desktop_data_clear_failed_partial"
            return OperationResult(True, "data-clear-status", "FAILED_PARTIAL", code, _public_message(code), details)
        if state == "PREVIEW" and self._now() > _number(journal.get("expiresEpoch"), "expiresEpoch"):
            return OperationResult(True, "data-clear-status", "EXPIRED", "desktop_data_clear_preview_expired", _public_message("desktop_data_clear_preview_expired"), {})
        code = {
            "IDLE": "desktop_data_clear_idle",
            "PREVIEW": "desktop_data_clear_confirmation_required",
            "PREPARED": "desktop_data_clear_prepared",
            "FINALIZING": "desktop_data_clear_finalizing",
            "FAILED_PARTIAL": "desktop_data_clear_failed_partial",
        }.get(state, "desktop_data_clear_state_unknown")
        message = _public_message(code)
        return OperationResult(state != "UNKNOWN", "data-clear-status", state, code, message, details, 0 if state != "UNKNOWN" else 2)

    def finalize(self, request: Mapping[str, Any], *, ready: Callable[[str, str], None]) -> OperationResult:
        parsed = _parse_finalize(request)
        paths = self.paths
        _validate_static_paths(paths)
        operation_id = parsed["operationId"]
        finalizing: dict[str, Any] | None = None
        try:
            with _owner_lock(paths):
                journal = _read_journal(paths)
                _validate_finalizer_request(journal, parsed, self._now())
                _validate_binding(paths, journal, allow_missing=journal.get("retryOf") is not None)
                _assert_no_recorded_processes(paths.runtime_root, self.process_inspector)
                electron = _verify_live_electron(parsed["electron"], self.process_inspector)
                finalizer_process = _current_process_identity(self.process_inspector)
                if electron != journal.get("electron"):
                    raise DesktopOperationError(
                        "desktop_data_clear_electron_identity_mismatch",
                        "finalizer Electron identity does not match the prepared operation",
                    )
                finalizing = {
                    **journal,
                    "revision": int(journal["revision"]) + 1,
                    "state": "FINALIZING",
                    "finalizingAt": _timestamp(self._now()),
                    "failure": None,
                    "finalizerProcess": finalizer_process,
                }
                _write_journal(paths, finalizing)
            ready(operation_id, parsed["readyNonce"])
            self._wait_for_exit(finalizing["electron"])
            self._delete_and_verify(finalizing)
        except Exception as exc:
            if finalizing is not None:
                self._record_failure(operation_id, exc)
            raise
        completed = _timestamp(self._now())
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "profileKey": _profile_key(paths.profile_root),
            "operationId": operation_id,
            "generation": str(finalizing.get("generation") or ""),
            "completedAt": completed,
            "contentRetained": False,
        }
        with _owner_lock(paths):
            atomic_write_json(paths.receipt, receipt)
            protect_private_path(paths.receipt)
            _write_journal(
                paths,
                {
                    "schema": JOURNAL_SCHEMA,
                    "revision": int(finalizing["revision"]) + 1,
                    "state": "CLEARED",
                    "profileKey": receipt["profileKey"],
                    "generation": receipt["generation"],
                    "operationId": operation_id,
                    "completedAt": completed,
                    "receiptSchema": RECEIPT_SCHEMA,
                },
            )
        return OperationResult(True, "data-clear-finalize", "CLEARED", "desktop_data_clear_complete", _public_message("desktop_data_clear_complete"), {"operationId": operation_id, "completedAt": completed})

    def _wait_for_exit(self, electron: Mapping[str, Any]) -> None:
        pid = int(electron["pid"])
        created = int(str(electron["creationTime"]), 10)
        executable = str(electron["executable"])
        deadline = time.monotonic() + self.finalizer_wait_seconds
        while True:
            actual = self.process_inspector.identity(pid)
            if actual is None:
                return
            if actual[0] != created or _norm(actual[1]) != _norm(executable):
                return
            if time.monotonic() >= deadline:
                raise DesktopOperationError(
                    "desktop_data_clear_electron_exit_timeout",
                    "the exact requesting Electron process did not exit before the bounded finalizer deadline",
                )
            self._sleep(min(0.1, max(0.01, deadline - time.monotonic())))

    def _delete_and_verify(self, journal: Mapping[str, Any]) -> None:
        paths = self.paths
        deadline = time.monotonic() + FINALIZER_DELETE_SECONDS
        _validate_binding(paths, journal, allow_missing=journal.get("retryOf") is not None)
        _assert_no_recorded_processes(paths.runtime_root, self.process_inspector)
        _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")
        if _lexists(paths.profile_root):
            _scan_tree(paths.profile_root, deadline=deadline)
        _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")
        try:
            delete_slot(paths.runtime_root, vault=self.credential_vault)
            if inspect_slot(paths.runtime_root, vault=self.credential_vault).present:
                raise DesktopOperationError("desktop_data_clear_vault_verification_failed", "the fixed credential slot is still present after deletion")
        except CredentialError as exc:
            raise DesktopOperationError(exc.code, str(exc)) from exc
        _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")
        if _lexists(paths.profile_root):
            _delete_tree(paths.profile_root, deadline=deadline)
        _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")
        if _lexists(paths.profile_root):
            raise DesktopOperationError("desktop_data_clear_profile_verification_failed", "owned Electron profile still exists after deletion")
        if inspect_slot(paths.runtime_root, vault=self.credential_vault).present:
            raise DesktopOperationError("desktop_data_clear_vault_verification_failed", "the fixed credential slot reappeared during final verification")
        _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")

    def _record_failure(self, operation_id: str, error: Exception) -> None:
        try:
            with _owner_lock(self.paths):
                journal = _read_journal(self.paths, missing_ok=True)
                if not journal or journal.get("operationId") != operation_id or journal.get("state") == "CLEARED":
                    return
                _write_journal(
                    self.paths,
                    {
                        **journal,
                        "revision": int(journal.get("revision") or 0) + 1,
                        "state": "FAILED_PARTIAL",
                        "failedAt": _timestamp(self._now()),
                        "failure": {
                            "code": str(getattr(error, "reason_code", "desktop_data_clear_finalizer_failed")),
                            "message": "owned data clear did not complete; explicit reconfirmation is required",
                        },
                        "finalizerCapabilityHash": None,
                    },
                )
        except Exception:
            pass


def clear_paths(*, resource_root: str | os.PathLike[str], data_root: str | os.PathLike[str]) -> ClearPaths:
    resource = _canonical_directory(_absolute(resource_root, "resource_root"), field="resource_root", required=True)
    runtime_input = _absolute(data_root, "data_root")
    if runtime_input.name.casefold() != "runtime":
        raise DesktopOperationError("desktop_data_clear_layout_invalid", "owned data clear requires data_root to be the fixed <Electron userData>/runtime directory")
    profile_input = runtime_input.parent
    profile = _canonical_directory(profile_input, field="profile_root", required=False)
    runtime = profile / "runtime"
    if _lexists(runtime_input):
        canonical_runtime = _canonical_directory(runtime_input, field="data_root", required=True)
        if _norm(canonical_runtime) != _norm(runtime):
            raise DesktopOperationError("desktop_data_clear_alias_mismatch", "runtime alias does not resolve to the canonical Electron profile")
        runtime = canonical_runtime
    if runtime.name.casefold() != "runtime":
        raise DesktopOperationError(
            "desktop_data_clear_layout_invalid",
            "owned data clear requires data_root to be the fixed <Electron userData>/runtime directory",
        )
    profile = runtime.parent
    if profile == profile.parent or not profile.name:
        raise DesktopOperationError("desktop_data_clear_target_invalid", "Electron userData cannot be a filesystem root")
    external = profile.parent / EXTERNAL_OWNER_DIRECTORY / _profile_key(profile)
    return ClearPaths(resource, runtime, profile, profile / MARKER_NAME, external, external / "state.json", external / "receipt.json", external / "operation.lock")


def fence_normal_initialization(data_root: str | os.PathLike[str]) -> None:
    """Run before config recovery/bootstrap. An absent journal causes no filesystem writes."""
    raw = Path(data_root)
    if not raw.is_absolute() or raw.name.casefold() != "runtime":
        return
    runtime_input = _absolute(data_root, "data_root")
    profile = _canonical_directory(runtime_input.parent, field="profile_root", required=False)
    runtime = profile / "runtime"
    external = profile.parent / EXTERNAL_OWNER_DIRECTORY / _profile_key(profile)
    journal_path = external / "state.json"
    if not _lexists(journal_path):
        return
    paths = ClearPaths(Path(profile.anchor), runtime, profile, profile / MARKER_NAME, external, journal_path, external / "receipt.json", external / "operation.lock")
    journal = _read_journal(paths)
    state = _maintenance_state(journal)
    if state in BLOCKING_STATES:
        raise _fence_error(state, journal)


def _maintenance_state(journal: Mapping[str, Any]) -> str:
    state = str(journal.get("state") or "UNKNOWN")
    if state == "PREVIEW" and journal.get("retryOf"):
        return "FAILED_PARTIAL"
    return state


def _validate_static_paths(paths: ClearPaths, *, require_resource: bool = True) -> None:
    if paths.runtime_root.parent != paths.profile_root:
        raise DesktopOperationError("desktop_data_clear_target_invalid", "runtime root is not the direct child of Electron userData")
    if _overlap(paths.profile_root, paths.resource_root):
        raise DesktopOperationError("desktop_data_clear_roots_overlap", "Electron profile overlaps immutable application resources")
    if _descendant(paths.external_root, paths.profile_root):
        raise DesktopOperationError("desktop_data_clear_journal_invalid", "clear journal must remain outside the deletion target")
    if require_resource and not paths.resource_root.is_dir():
        raise DesktopOperationError("desktop_app_resource_root_missing", "desktop app resource root does not exist")
    for candidate, root in ((paths.resource_root, paths.resource_root), (paths.profile_root, paths.profile_root), (paths.runtime_root, paths.profile_root), (paths.external_root, paths.external_root)):
        if _lexists(candidate):
            reject_reparse_path(candidate, root=root)


def _ensure_profile_directory(profile: Path) -> None:
    reject_reparse_path(profile.parent, root=profile.parent)
    if _lexists(profile):
        metadata = windows_extended_path(profile).lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise DesktopOperationError("desktop_data_clear_target_invalid", "Electron userData is not a directory")
        reject_reparse_path(profile, root=profile)
        return
    profile.mkdir()
    protect_private_path(profile)


def _read_marker(paths: ClearPaths, *, check_release: bool = True, allow_missing_runtime: bool = False) -> dict[str, Any]:
    if not _lexists(paths.marker):
        raise DesktopOperationError(
            "desktop_data_clear_marker_missing",
            "current Electron profile has no owned registration marker; preview will not adopt it",
        )
    reject_reparse_path(paths.marker, root=paths.profile_root)
    marker = read_json_object(paths.marker)
    required = {"schema", "generation", "profilePath", "runtimePath", "profileIdentity", "runtimeIdentity", "releaseBinding", "createdAt", "updatedAt"}
    if set(marker) != required or marker.get("schema") != MARKER_SCHEMA:
        raise DesktopOperationError("desktop_data_clear_marker_invalid", "owned profile marker is invalid")
    if not _hex(marker.get("generation"), 32):
        raise DesktopOperationError("desktop_data_clear_generation_invalid", "owned profile generation is invalid")
    if _norm(marker.get("profilePath")) != _norm(paths.profile_root) or _norm(marker.get("runtimePath")) != _norm(paths.runtime_root):
        raise DesktopOperationError("desktop_data_clear_marker_root_mismatch", "owned profile marker is bound to a different root")
    if _directory_identity(paths.profile_root) != marker.get("profileIdentity"):
        raise DesktopOperationError("desktop_data_clear_profile_replaced", "Electron profile directory identity changed")
    if _lexists(paths.runtime_root):
        if _directory_identity(paths.runtime_root) != marker.get("runtimeIdentity"):
            raise DesktopOperationError("desktop_data_clear_runtime_replaced", "owned runtime directory identity changed")
    elif not allow_missing_runtime:
        raise DesktopOperationError("desktop_data_clear_runtime_missing", "owned runtime directory is missing")
    if check_release and marker.get("releaseBinding") != _release_binding(paths.resource_root):
        raise DesktopOperationError("desktop_data_clear_release_mismatch", "owned profile marker is bound to a different application release")
    return marker


def _validate_partial_binding(paths: ClearPaths, journal: Mapping[str, Any]) -> dict[str, Any]:
    _validate_journal(paths, journal)
    if _lexists(paths.profile_root):
        if journal.get("profileIdentity") is not None and _directory_identity(paths.profile_root) != journal.get("profileIdentity"):
            raise DesktopOperationError("desktop_data_clear_profile_replaced", "partially-cleared profile was replaced")
        if _lexists(paths.runtime_root) and journal.get("runtimeIdentity") is not None:
            if _directory_identity(paths.runtime_root) != journal.get("runtimeIdentity"):
                raise DesktopOperationError("desktop_data_clear_runtime_replaced", "partially-cleared runtime directory was replaced")
        if _lexists(paths.marker):
            marker = _read_marker(paths, allow_missing_runtime=True)
            if marker.get("generation") != journal.get("generation") or marker.get("runtimeIdentity") != journal.get("runtimeIdentity"):
                raise DesktopOperationError("desktop_data_clear_generation_changed", "partially-cleared marker binding changed")
            return marker
    return {"generation": journal.get("generation"), "runtimeIdentity": journal.get("runtimeIdentity")}


def _validate_binding(paths: ClearPaths, journal: Mapping[str, Any], *, allow_missing: bool) -> dict[str, Any]:
    _validate_journal(paths, journal)
    if journal.get("releaseBinding") != _release_binding(paths.resource_root):
        raise DesktopOperationError("desktop_data_clear_release_mismatch", "application release changed after preview")
    if not _lexists(paths.profile_root):
        if allow_missing:
            return {"generation": journal.get("generation")}
        raise DesktopOperationError("desktop_data_clear_profile_missing", "owned Electron profile disappeared after preview")
    if journal.get("profileIdentity") != _directory_identity(paths.profile_root):
        raise DesktopOperationError("desktop_data_clear_profile_replaced", "Electron profile directory identity changed after preview")
    if _lexists(paths.runtime_root) and journal.get("runtimeIdentity") is not None:
        if _directory_identity(paths.runtime_root) != journal.get("runtimeIdentity"):
            raise DesktopOperationError("desktop_data_clear_runtime_replaced", "owned runtime directory identity changed after preview")
    if _lexists(paths.marker):
        marker = _read_marker(paths, allow_missing_runtime=allow_missing)
        if marker.get("generation") != journal.get("generation") or marker.get("runtimeIdentity") != journal.get("runtimeIdentity"):
            raise DesktopOperationError("desktop_data_clear_generation_changed", "owned profile generation changed after preview")
        return marker
    if allow_missing:
        return {"generation": journal.get("generation")}
    raise DesktopOperationError("desktop_data_clear_marker_missing", "owned profile marker disappeared after preview")


def _read_journal(paths: ClearPaths, *, missing_ok: bool = False) -> dict[str, Any]:
    if not _lexists(paths.journal):
        if missing_ok:
            return {}
        raise DesktopOperationError("desktop_data_clear_journal_missing", "owned data-clear journal is missing")
    reject_reparse_path(paths.journal, root=paths.external_root)
    payload = read_json_object(paths.journal)
    _validate_journal(paths, payload)
    return payload


def _validate_journal(paths: ClearPaths, payload: Mapping[str, Any]) -> None:
    if payload.get("schema") != JOURNAL_SCHEMA or payload.get("profileKey") != _profile_key(paths.profile_root):
        raise DesktopOperationError("desktop_data_clear_journal_invalid", "owned data-clear journal identity is invalid")
    revision = payload.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise DesktopOperationError("desktop_data_clear_journal_invalid", "owned data-clear journal revision is invalid")
    if payload.get("state") not in {"IDLE", "PREVIEW", "PREPARED", "FINALIZING", "FAILED_PARTIAL", "CLEARED"}:
        raise DesktopOperationError("desktop_data_clear_journal_invalid", "owned data-clear journal state is invalid")
    for field in ("expiresEpoch", "finalizerExpiresEpoch"):
        if field in payload:
            _number(payload[field], field)


def _write_journal(paths: ClearPaths, payload: dict[str, Any]) -> None:
    paths.external_root.mkdir(parents=True, exist_ok=True)
    protect_private_path(paths.external_root)
    atomic_write_json(paths.journal, payload)
    protect_private_path(paths.journal)


def _write_idle(paths: ClearPaths, marker: Mapping[str, Any], now: float, reason: str) -> None:
    previous = _read_journal(paths, missing_ok=True)
    _write_journal(paths, {
        "schema": JOURNAL_SCHEMA,
        "revision": int(previous.get("revision") or 0) + 1,
        "state": "IDLE",
        "profileKey": _profile_key(paths.profile_root),
        "generation": str(marker["generation"]),
        "updatedAt": _timestamp(now),
        "reason": reason,
    })


def _owner_lock(paths: ClearPaths):
    paths.external_root.mkdir(parents=True, exist_ok=True)
    protect_private_path(paths.external_root)
    reject_reparse_path(paths.external_root, root=paths.external_root)
    return exclusive_lock(paths.lock, timeout_seconds=5.0)


def _snapshot(paths: ClearPaths, *, allow_missing: bool) -> dict[str, Any]:
    if not _lexists(paths.profile_root):
        if allow_missing:
            missing = _digest_entries([], marker_generation=str(_read_journal(paths).get("generation") or ""))
            return {"profileIdentity": None, "runtimeIdentity": None, "businessSnapshot": missing, "volatileTopLevel": [], "scopeCount": 0, "scopeBytes": 0}
        raise DesktopOperationError("desktop_data_clear_profile_missing", "owned Electron profile is missing")
    profile_identity, all_entries = _scan_tree(paths.profile_root)
    partial = _read_journal(paths) if allow_missing else {}
    if _lexists(paths.marker):
        marker = _read_marker(paths, allow_missing_runtime=allow_missing)
        marker_generation = str(marker["generation"])
    elif allow_missing:
        marker_generation = str(partial.get("generation") or "")
    else:
        raise DesktopOperationError("desktop_data_clear_marker_missing", "owned profile marker disappeared after preview")
    if _lexists(paths.runtime_root):
        runtime_identity, business_entries = _scan_tree(paths.runtime_root)
    elif allow_missing:
        runtime_identity, business_entries = None, []
    else:
        raise DesktopOperationError("desktop_data_clear_runtime_missing", "owned runtime directory disappeared after preview")
    volatile_top = sorted({entry["path"].split("/", 1)[0] for entry in all_entries if not entry["path"].casefold().startswith("runtime/") and entry["path"].casefold() not in {"runtime", MARKER_NAME.casefold()}}, key=str.casefold)
    unknown = [name for name in volatile_top if name.casefold() not in _HOST_VOLATILE_NAMES]
    if unknown:
        raise DesktopOperationError("desktop_data_clear_unknown_profile_owner", "Electron profile contains an unknown top-level owner and cannot be cleared", details={"names": unknown[:10]})
    scope_count = sum(1 for entry in all_entries if entry["kind"] == "file")
    scope_bytes = sum(int(entry["size"]) for entry in all_entries if entry["kind"] == "file")
    return {
        "profileIdentity": profile_identity,
        "runtimeIdentity": runtime_identity,
        "businessSnapshot": _digest_entries(business_entries, marker_generation=marker_generation),
        "volatileTopLevel": volatile_top,
        "scopeCount": scope_count,
        "scopeBytes": scope_bytes,
    }


def _digest_entries(entries: list[dict[str, Any]], *, marker_generation: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    total = 0
    digest.update(("generation:" + marker_generation + "\n").encode("utf-8"))
    for item in entries:
        digest.update(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
        if item["kind"] == "file":
            count += 1
            total += int(item["size"])
    return {"fileCount": count, "totalBytes": total, "digest": digest.hexdigest()}


def _scan_tree(root: Path, *, deadline: float | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = _operation_deadline(deadline)
    _check_operation_deadline(deadline, "desktop_data_clear_scan_timeout")
    reject_reparse_path(root, root=root)
    identity = _directory_identity(root)
    entries: list[dict[str, Any]] = []
    pending = [root]
    while pending:
        _check_operation_deadline(deadline, "desktop_data_clear_scan_timeout")
        directory = pending.pop()
        try:
            children = sorted(os.scandir(windows_extended_path(directory)), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise DesktopOperationError("desktop_data_clear_scan_failed", "owned profile could not be inspected safely") from exc
        for child in children:
            _check_operation_deadline(deadline, "desktop_data_clear_scan_timeout")
            path = directory / child.name
            try:
                metadata = windows_extended_path(path).lstat()
            except OSError as exc:
                raise DesktopOperationError("desktop_data_clear_scan_failed", "owned profile entry could not be inspected safely") from exc
            attributes = getattr(metadata, "st_file_attributes", 0)
            if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise DesktopOperationError("desktop_data_clear_reparse_rejected", "owned profile contains a reparse point and was not deleted")
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(_entry(relative, "directory", metadata, path=path))
                pending.append(path)
            elif stat.S_ISREG(metadata.st_mode):
                if int(getattr(metadata, "st_nlink", 1)) != 1:
                    raise DesktopOperationError("desktop_data_clear_hardlink_rejected", "owned profile contains a hard-linked file and was not deleted", details={"path": relative, "links": int(getattr(metadata, "st_nlink", 1))})
                entries.append(_entry(relative, "file", metadata, path=path))
            else:
                raise DesktopOperationError("desktop_data_clear_special_file_rejected", "owned profile contains an unsupported special file")
    entries.sort(key=lambda item: str(item["path"]).casefold())
    return identity, entries


def _entry(relative: str, kind: str, metadata: os.stat_result, *, path: Path | None = None) -> dict[str, Any]:
    result = {"path": relative, "kind": kind, "size": int(metadata.st_size), "mtimeNs": int(metadata.st_mtime_ns), "device": int(metadata.st_dev), "inode": int(metadata.st_ino), "links": int(getattr(metadata, "st_nlink", 1))}
    if os.name == "nt":
        if path is None:
            raise DesktopOperationError("desktop_data_clear_identity_failed", "Windows inventory entry has no exact path identity")
        result.update(_windows_handle_identity(path))
    return result


def _delete_tree(root: Path, *, deadline: float | None = None) -> None:
    deadline = _operation_deadline(time.monotonic() + FINALIZER_DELETE_SECONDS if deadline is None else deadline)
    root_identity, entries = _scan_tree(root, deadline=deadline)
    root_metadata = windows_extended_path(root).lstat()
    root_record = _entry("", "directory", root_metadata, path=root)
    directory_records = {"": root_record}
    directory_records.update({str(item["path"]): item for item in entries if item["kind"] == "directory"})
    files = [item for item in entries if item["kind"] == "file"]
    directories = sorted(directory_records.values(), key=lambda item: len(Path(str(item["path"])).parts), reverse=True)
    try:
        for record in (*files, *directories):
            _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")
            _delete_collected_entry(root, root_identity, directory_records, record, deadline=deadline)
    except DesktopOperationError:
        raise
    except OSError as exc:
        raise DesktopOperationError(
            "desktop_data_clear_delete_failed",
            "owned profile deletion failed because a file is locked, readonly protection could not be changed, or storage rejected the operation",
            details={"winerror": getattr(exc, "winerror", None), "errno": getattr(exc, "errno", None)},
        ) from exc


def _delete_collected_entry(
    root: Path,
    root_identity: Mapping[str, Any],
    directory_records: Mapping[str, Mapping[str, Any]],
    record: Mapping[str, Any],
    *,
    deadline: float,
) -> None:
    _check_operation_deadline(deadline, "desktop_data_clear_delete_timeout")
    relative = str(record["path"])
    path = root if not relative else root / Path(relative)
    _verify_delete_chain(root, root_identity, directory_records, path, record)
    if os.name == "nt":
        _windows_delete_exact(path, record)
        return
    _make_writable(path, directory=record["kind"] == "directory")
    _verify_delete_chain(root, root_identity, directory_records, path, record)
    if record["kind"] == "directory":
        os.rmdir(windows_extended_path(path))
    else:
        os.unlink(windows_extended_path(path))


def _verify_delete_chain(
    root: Path,
    root_identity: Mapping[str, Any],
    directory_records: Mapping[str, Mapping[str, Any]],
    path: Path,
    record: Mapping[str, Any],
) -> None:
    reject_reparse_path(path, root=root)
    if _directory_identity(root) != dict(root_identity):
        raise DesktopOperationError("desktop_data_clear_profile_replaced", "owned profile root changed during deletion")
    relative_parent = path.parent.relative_to(root) if path != root else Path()
    current = root
    parts: list[str] = []
    for part in relative_parent.parts:
        parts.append(part)
        current = current / part
        key = Path(*parts).as_posix()
        expected = directory_records.get(key)
        if expected is None:
            raise DesktopOperationError("desktop_data_clear_delete_refused", "owned deletion ancestry was not present in the verified inventory")
        _verify_lstat_identity(current, expected)
    _verify_lstat_identity(path, record)


def _verify_lstat_identity(path: Path, expected: Mapping[str, Any]) -> None:
    try:
        metadata = windows_extended_path(path).lstat()
    except OSError as exc:
        raise DesktopOperationError("desktop_data_clear_delete_refused", "owned profile changed before a destructive operation") from exc
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    if stat.S_ISLNK(metadata.st_mode) or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise DesktopOperationError("desktop_data_clear_reparse_rejected", "owned profile changed to contain a reparse point during deletion")
    expected_kind = str(expected["kind"])
    kind_matches = stat.S_ISDIR(metadata.st_mode) if expected_kind == "directory" else stat.S_ISREG(metadata.st_mode)
    if not kind_matches or int(metadata.st_dev) != int(expected["device"]) or int(metadata.st_ino) != int(expected["inode"]):
        raise DesktopOperationError("desktop_data_clear_delete_refused", "owned profile entry identity changed during deletion")
    links = int(getattr(metadata, "st_nlink", 1))
    if expected_kind == "file" and (links != 1 or links != int(expected["links"])):
        raise DesktopOperationError("desktop_data_clear_hardlink_rejected", "owned profile file link identity changed during deletion")



def _windows_handle_identity(path: Path) -> dict[str, int]:
    from ctypes import wintypes

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION))
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        os.fspath(windows_extended_path(path)),
        0x00000080,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        raise DesktopOperationError("desktop_data_clear_identity_failed", "exact Windows inventory identity could not be opened", details={"winerror": ctypes.get_last_error()})
    try:
        info = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise DesktopOperationError("desktop_data_clear_identity_failed", "exact Windows inventory identity could not be read", details={"winerror": ctypes.get_last_error()})
        return {
            "winVolumeSerial": int(info.dwVolumeSerialNumber),
            "winFileIndex": (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow),
        }
    finally:
        kernel32.CloseHandle(handle)

def _windows_delete_exact(path: Path, expected: Mapping[str, Any]) -> None:
    from ctypes import wintypes

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong), ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong), ("FileAttributes", wintypes.DWORD)]

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION))
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(
        os.fspath(windows_extended_path(path)),
        0x00010000 | 0x00000080 | 0x00000100,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x00200000 | 0x02000000,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        raise OSError(ctypes.get_last_error(), "could not open exact owned entry for deletion", os.fspath(path))
    try:
        info = BY_HANDLE_FILE_INFORMATION()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "could not verify exact owned entry", os.fspath(path))
        inode = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        attributes = int(info.dwFileAttributes)
        expected_directory = str(expected["kind"]) == "directory"
        if attributes & 0x400 or bool(attributes & 0x10) != expected_directory:
            raise DesktopOperationError("desktop_data_clear_reparse_rejected", "exact deletion handle resolved to an unsafe entry")
        if int(info.dwVolumeSerialNumber) != int(expected["winVolumeSerial"]) or inode != int(expected["winFileIndex"]):
            raise DesktopOperationError("desktop_data_clear_delete_refused", "exact deletion handle identity differs from the verified inventory")
        if not expected_directory and (int(info.nNumberOfLinks) != 1 or int(info.nNumberOfLinks) != int(expected["links"])):
            raise DesktopOperationError("desktop_data_clear_hardlink_rejected", "exact deletion handle has an unsafe link count")
        basic = FILE_BASIC_INFO()
        if not kernel32.GetFileInformationByHandleEx(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            raise OSError(ctypes.get_last_error(), "could not read exact owned entry attributes", os.fspath(path))
        if basic.FileAttributes & 0x1:
            basic.FileAttributes &= ~0x1
            if not kernel32.SetFileInformationByHandle(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic)):
                raise OSError(ctypes.get_last_error(), "could not clear readonly attribute on exact owned entry", os.fspath(path))
        disposition_ex = wintypes.DWORD(0x00000001 | 0x00000002 | 0x00000010)
        if not kernel32.SetFileInformationByHandle(handle, 21, ctypes.byref(disposition_ex), ctypes.sizeof(disposition_ex)):
            disposition = wintypes.BOOL(True)
            if not kernel32.SetFileInformationByHandle(handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
                raise OSError(ctypes.get_last_error(), "could not delete exact owned entry", os.fspath(path))
    finally:
        kernel32.CloseHandle(handle)


def _make_writable(path: Path, *, directory: bool) -> None:
    mode = stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if directory else 0)
    os.chmod(windows_extended_path(path), mode, follow_symlinks=False)


def _operation_deadline(deadline: float | None) -> float:
    if deadline is None:
        return time.monotonic() + TREE_OPERATION_SECONDS
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(float(deadline)):
        raise DesktopOperationError("desktop_data_clear_deadline_invalid", "owned filesystem operation deadline is invalid")
    return float(deadline)


def _check_operation_deadline(deadline: float, code: str) -> None:
    if time.monotonic() > deadline:
        raise DesktopOperationError(code, "owned filesystem operation exceeded its bounded deadline")

def _assert_no_recorded_processes(runtime_root: Path, inspector: ProcessInspector) -> None:
    instance_path = runtime_root / "instance.json"
    if not _lexists(instance_path):
        return
    reject_reparse_path(instance_path, root=runtime_root)
    instance = read_json_object(instance_path)
    processes = instance.get("processes", [])
    if not isinstance(processes, list):
        raise DesktopOperationError("desktop_data_clear_process_state_unknown", "runtime process ownership record is invalid")
    for raw in processes:
        if not isinstance(raw, Mapping):
            raise DesktopOperationError("desktop_data_clear_process_state_unknown", "runtime process ownership record is invalid")
        process = ProcessIdentity.from_dict(raw)
        ownership = inspector.ownership(process)
        if ownership == "owned":
            raise DesktopOperationError("desktop_data_clear_process_active", "an owned runtime process is still active after drain")
        if ownership == "foreign":
            raise DesktopOperationError("desktop_data_clear_process_not_owned", "runtime process identity belongs to a reused or unowned PID")


def _parse_apply(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _strict(value, {"version", "previewId", "confirmationText", "electron"}, "apply request")
    if type(request["version"]) is not int or request["version"] != 1:
        raise DesktopOperationError("desktop_data_clear_protocol_version_invalid", "unsupported data-clear protocol version")
    return {
        "version": 1,
        "previewId": _bounded(request["previewId"], "previewId", 256),
        "confirmationText": _bounded(request["confirmationText"], "confirmationText", 128),
        "electron": _parse_electron(request["electron"]),
    }


def _parse_finalize(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _strict(value, {"version", "operationId", "finalizerCapability", "electron", "readyNonce"}, "finalizer request")
    if type(request["version"]) is not int or request["version"] != 1:
        raise DesktopOperationError("desktop_data_clear_protocol_version_invalid", "unsupported data-clear protocol version")
    operation_id = _bounded(request["operationId"], "operationId", 64)
    if not _hex(operation_id, 32):
        raise DesktopOperationError("desktop_data_clear_operation_invalid", "data-clear operation identity is invalid")
    return {
        "version": 1,
        "operationId": operation_id,
        "finalizerCapability": _bounded(request["finalizerCapability"], "finalizerCapability", 256),
        "readyNonce": _bounded(request["readyNonce"], "readyNonce", 128),
        "electron": _parse_electron(request["electron"]),
    }


def _parse_electron(value: Any) -> dict[str, Any]:
    electron = _strict(value, {"pid", "creationTime"}, "electron identity")
    pid = electron["pid"]
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1 or pid > 2**31 - 1:
        raise DesktopOperationError("desktop_data_clear_electron_identity_invalid", "Electron PID is invalid")
    creation = _bounded(electron["creationTime"], "creationTime", 32)
    if not creation.isascii() or not creation.isdecimal() or int(creation, 10) < 1:
        raise DesktopOperationError("desktop_data_clear_electron_identity_invalid", "Electron creation identity is invalid")
    return {"pid": pid, "creationTime": creation}



def _current_process_identity(inspector: ProcessInspector) -> dict[str, Any]:
    actual = inspector.identity(os.getpid())
    if actual is None or not actual[1]:
        raise DesktopOperationError("desktop_data_clear_finalizer_identity_unknown", "finalizer process identity is unavailable")
    return {"pid": os.getpid(), "creationTime": str(actual[0]), "executable": os.fspath(actual[1])}


def _recorded_finalizer_active(journal: Mapping[str, Any], inspector: ProcessInspector) -> bool:
    record = journal.get("finalizerProcess")
    if not isinstance(record, Mapping):
        raise DesktopOperationError("desktop_data_clear_finalizer_identity_unknown", "FINALIZING journal has no exact finalizer identity")
    try:
        pid = int(record["pid"])
        created = int(str(record["creationTime"]), 10)
        executable = str(record["executable"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DesktopOperationError("desktop_data_clear_finalizer_identity_unknown", "FINALIZING journal identity is invalid") from exc
    actual = inspector.identity(pid)
    if actual is None:
        return False
    return actual[0] == created and _norm(actual[1]) == _norm(executable)

def _verify_live_electron(electron: Mapping[str, Any], inspector: ProcessInspector) -> dict[str, Any]:
    actual = inspector.identity(int(electron["pid"]))
    if actual is None:
        raise DesktopOperationError("desktop_data_clear_electron_not_running", "requesting Electron process is not running")
    created, executable = actual
    if created != int(str(electron["creationTime"]), 10):
        raise DesktopOperationError("desktop_data_clear_electron_identity_mismatch", "requesting Electron process creation identity changed")
    if not executable:
        raise DesktopOperationError("desktop_data_clear_electron_identity_unknown", "requesting Electron executable identity is unavailable")
    return {"pid": int(electron["pid"]), "creationTime": str(created), "executable": os.fspath(executable)}


def _validate_finalizer_request(journal: Mapping[str, Any], request: Mapping[str, Any], now: float) -> None:
    if journal.get("state") != "PREPARED":
        raise DesktopOperationError("desktop_data_clear_finalizer_replayed", "finalizer capability is absent, consumed, or no longer applicable")
    if journal.get("operationId") != request["operationId"]:
        raise DesktopOperationError("desktop_data_clear_operation_mismatch", "finalizer operation identity does not match")
    if now > _number(journal.get("finalizerExpiresEpoch"), "finalizerExpiresEpoch"):
        raise DesktopOperationError("desktop_data_clear_finalizer_expired", "finalizer capability expired; explicit reconfirmation is required")
    expected = str(journal.get("finalizerCapabilityHash") or "")
    if not expected or not secrets.compare_digest(expected, _secret_hash(str(request["finalizerCapability"]))):
        raise DesktopOperationError("desktop_data_clear_finalizer_capability_invalid", "finalizer capability is invalid")


def _public_message(code: str) -> str:
    return {
        "desktop_data_clear_preview_ready": "\u5f53\u524d\u5e94\u7528\u6570\u636e\u5df2\u505c\u6b62\u5199\u5165\u5e76\u5b8c\u6210\u6838\u9a8c\uff0c\u8bf7\u8fdb\u884c\u72ec\u7acb\u786e\u8ba4",
        "desktop_data_clear_prepared": "\u6e05\u9664\u64cd\u4f5c\u5df2\u51c6\u5907\uff0c\u4f46\u5c1a\u672a\u5220\u9664\uff1b\u5fc5\u987b\u5728\u5f53\u524d Electron \u8fdb\u7a0b\u9000\u51fa\u540e\u7531\u56fa\u5b9a finalizer \u5b8c\u6210",
        "desktop_data_clear_idle": "\u5f53\u524d\u6ca1\u6709\u5f85\u5904\u7406\u7684\u5e94\u7528\u6570\u636e\u6e05\u9664\u64cd\u4f5c",
        "desktop_data_clear_complete": "\u5f53\u524d\u5e94\u7528 profile \u53ca\u5176\u4e13\u5c5e\u51ed\u636e\u69fd\u5df2\u5168\u90e8\u6e05\u9664\u5e76\u5b8c\u6210\u6838\u9a8c",
        "desktop_data_clear_preview_expired": "\u6e05\u9664\u9884\u89c8\u5df2\u8fc7\u671f\uff0c\u672a\u5220\u9664\u4efb\u4f55\u6570\u636e",
        "desktop_data_clear_confirmation_required": "\u5f53\u524d\u5e94\u7528\u6570\u636e\u5df2\u505c\u6b62\u5199\u5165\uff0c\u6b63\u5728\u7b49\u5f85\u7cbe\u786e\u786e\u8ba4",
        "desktop_data_clear_finalizing": "\u6e05\u9664\u64cd\u4f5c\u6b63\u5728\u6536\u5c3e\uff0c\u6b63\u5e38\u542f\u52a8\u5df2\u88ab\u5b89\u5168\u963b\u65ad",
        "desktop_data_clear_failed_partial": "\u6e05\u9664\u64cd\u4f5c\u672a\u5b8c\u6574\u5b8c\u6210\uff0c\u6b63\u5e38\u542f\u52a8\u4ecd\u88ab\u5b89\u5168\u963b\u65ad",
        "desktop_data_clear_state_unknown": "\u5e94\u7528\u6570\u636e\u6e05\u9664\u72b6\u6001\u65e0\u6548",
    }.get(code, "\u5e94\u7528\u6570\u636e\u6e05\u9664\u72b6\u6001\u65e0\u6548")

def _public_scope(profile: Path, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "profilePath": os.fspath(profile),
        "fileCount": int(snapshot["scopeCount"]),
        "totalBytes": int(snapshot["scopeBytes"]),
        "includes": [
            "\u6e05\u9664\u5f53\u524d\u6574\u4e2a\u5e94\u7528 profile \u4e2d\u7684\u8d26\u53f7\u3001\u8bba\u6587\u3001\u7b14\u8bb0\u3001\u8bb0\u5fc6\u3001\u914d\u7f6e\u7b49\u4e1a\u52a1\u6570\u636e\uff08\u4e25\u683c CAS \u6838\u9a8c\uff09",
            "\u6e05\u9664\u5f53\u524d profile \u7684\u6d4f\u89c8\u5668\u4f1a\u8bdd\u3001\u7f13\u5b58\u3001\u504f\u597d\u8bbe\u7f6e\u3001\u5bbf\u4e3b\u5ba1\u8ba1\u4e0e\u65e5\u5fd7\uff08\u786e\u8ba4\u671f\u95f4\u4ec5\u5141\u8bb8\u53d7\u63a7\u6536\u5c3e\u5199\u5165\uff09",
            "\u6e05\u9664\u4e14\u4ec5\u6e05\u9664\u5f53\u524d profile \u4e13\u5c5e\u7684 Research \u51ed\u636e\u69fd\uff1b\u4e0d\u4f1a\u8bfb\u53d6\u51ed\u636e\u503c",
        ],
        "preserves": [
            "\u4fdd\u7559\u5e94\u7528\u5b89\u88c5\u76ee\u5f55\u3001\u8d44\u6e90\u4e0e\u6e90 runtime",
            "\u4fdd\u7559\u5f53\u524d profile \u5916\u7684\u7528\u6237\u539f\u59cb\u5bfc\u5165\u6587\u4ef6",
            "\u4fdd\u7559\u6240\u6709\u5916\u90e8 backup \u4e0e export",
            "\u4fdd\u7559\u5176\u4ed6 profile \u53ca\u5176\u51ed\u636e\u69fd",
            "\u5728\u76ee\u6807\u5916\u4ec5\u4fdd\u7559\u4e0d\u542b\u4e1a\u52a1\u5185\u5bb9\u7684\u6700\u5c0f\u7ef4\u62a4\u56de\u6267",
        ],
        "credentialPolicy": "\u4ec5\u5220\u9664\u5f53\u524d profile \u56fa\u5b9a\u7ed1\u5b9a\u7684\u4e13\u5c5e\u51ed\u636e\u69fd\uff0c\u5168\u7a0b\u4e0d\u8bfb\u53d6\u3001\u4e0d\u5c55\u793a\u51ed\u636e\u503c",
    }

def _release_binding(resource_root: Path) -> dict[str, Any]:
    reject_reparse_path(resource_root, root=resource_root)
    manifest = resource_root / "desktop-bundle.json"
    manifest_digest = None
    if _lexists(manifest):
        reject_reparse_path(manifest, root=resource_root)
        metadata = windows_extended_path(manifest).lstat()
        if not stat.S_ISREG(metadata.st_mode) or int(getattr(metadata, "st_nlink", 1)) != 1:
            raise DesktopOperationError("desktop_data_clear_release_invalid", "desktop bundle manifest is not a regular owned file")
        if metadata.st_size > RELEASE_MANIFEST_MAX_BYTES:
            raise DesktopOperationError("desktop_data_clear_release_invalid", "desktop bundle manifest exceeds the bounded identity size")
        expected_identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        deadline = time.monotonic() + RELEASE_MANIFEST_HASH_SECONDS
        digest = hashlib.sha256()
        total = 0
        try:
            with open(windows_extended_path(manifest), "rb") as handle:
                opened = os.fstat(handle.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != expected_identity:
                    raise DesktopOperationError("desktop_data_clear_release_invalid", "desktop bundle manifest changed before identity hashing")
                while True:
                    _check_operation_deadline(deadline, "desktop_data_clear_release_timeout")
                    chunk = handle.read(RELEASE_MANIFEST_CHUNK_BYTES)
                    _check_operation_deadline(deadline, "desktop_data_clear_release_timeout")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > RELEASE_MANIFEST_MAX_BYTES:
                        raise DesktopOperationError("desktop_data_clear_release_invalid", "desktop bundle manifest exceeds the bounded identity size")
                    digest.update(chunk)
                completed = os.fstat(handle.fileno())
            current = windows_extended_path(manifest).lstat()
        except OSError as exc:
            raise DesktopOperationError("desktop_data_clear_release_invalid", "desktop bundle manifest could not be hashed safely") from exc
        for observed in (completed, current):
            if (observed.st_dev, observed.st_ino, observed.st_size, observed.st_mtime_ns) != expected_identity or total != observed.st_size:
                raise DesktopOperationError("desktop_data_clear_release_invalid", "desktop bundle manifest changed during identity hashing")
        manifest_digest = digest.hexdigest()
    payload = {"resourcePath": _norm(resource_root), "bundleManifestSha256": manifest_digest}
    payload["digest"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return payload


def _directory_identity(path: Path) -> dict[str, Any]:
    reject_reparse_path(path, root=path)
    try:
        metadata = windows_extended_path(path).lstat()
    except OSError as exc:
        raise DesktopOperationError("desktop_data_clear_identity_failed", "owned directory identity could not be read") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise DesktopOperationError("desktop_data_clear_identity_failed", "owned identity target is not a directory")
    return {"device": int(metadata.st_dev), "inode": int(metadata.st_ino)}


def _fence_error(state: str, journal: Mapping[str, Any]) -> DesktopOperationError:
    return DesktopOperationError("desktop_data_clear_start_blocked", f"normal desktop startup is blocked by owned data-clear state {state}", details={"state": state, "operationId": journal.get("operationId")}, exit_code=4)


def _strict(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys or any(not isinstance(key, str) for key in value):
        raise DesktopOperationError("desktop_data_clear_protocol_invalid", f"{field} has unknown or missing fields")
    return dict(value)


def _bounded(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise DesktopOperationError("desktop_data_clear_protocol_invalid", f"{field} is invalid")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DesktopOperationError("desktop_data_clear_journal_invalid", f"{field} is invalid")
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > _MAX_EPOCH_SECONDS:
        raise DesktopOperationError("desktop_data_clear_journal_invalid", f"{field} is outside the supported epoch range")
    return number


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > 3600:
        raise ValueError(f"{field} must be an integer between 1 and 3600 seconds")
    return value


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0.01 or float(value) > 600.0:
        raise ValueError(f"{field} must be finite and between 0.01 and 600 seconds")
    return float(value)


def _timestamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _profile_key(profile: Path) -> str:
    return hashlib.sha256(_norm(profile).encode("utf-8", errors="strict")).hexdigest()


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="strict")).hexdigest()


def _hex(value: Any, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(char in "0123456789abcdef" for char in value)


def _absolute(value: str | os.PathLike[str], field: str) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise DesktopOperationError("desktop_data_clear_path_invalid", f"{field} must be a non-empty absolute path")
    normalized_raw = raw.replace("/", "\\") if os.name == "nt" else raw
    if os.name == "nt" and normalized_raw.casefold().startswith(("\\\\?\\", "\\\\.\\", "\\??\\")):
        raise DesktopOperationError("desktop_data_clear_path_invalid", f"{field} must not use a Windows device namespace")
    selected = Path(raw)
    if not selected.is_absolute() or not selected.anchor or ".." in selected.parts:
        raise DesktopOperationError("desktop_data_clear_path_invalid", f"{field} must be an absolute traversal-free path")
    if os.name == "nt":
        reserved = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
        for index, part in enumerate(selected.parts):
            if index == 0:
                continue
            if part.endswith((" ", ".")) or ":" in part or part.split(".", 1)[0].casefold() in reserved:
                raise DesktopOperationError("desktop_data_clear_path_invalid", f"{field} contains an unsafe Windows path component")
    return Path(os.path.normpath(raw))


def _canonical_directory(path: Path, *, field: str, required: bool) -> Path:
    if not _lexists(path):
        if required:
            raise DesktopOperationError("desktop_data_clear_path_missing", f"{field} does not exist")
        parent = path.parent
        if parent == path:
            raise DesktopOperationError("desktop_data_clear_path_missing", f"{field} has no existing canonical parent")
        canonical_parent = _canonical_directory(parent, field=f"{field}.parent", required=True)
        return canonical_parent / path.name
    reject_reparse_path(path, root=path)
    metadata = windows_extended_path(path).lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise DesktopOperationError("desktop_data_clear_path_invalid", f"{field} must be a directory")
    if os.name != "nt":
        return path.resolve(strict=True)
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateFileW(os.fspath(path), 0, 0x00000007, None, 3, 0x02000000, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in (None, 0, invalid):
        raise DesktopOperationError("desktop_data_clear_identity_failed", f"{field} canonical identity could not be opened", details={"winerror": ctypes.get_last_error()})
    try:
        size = kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not size:
            raise DesktopOperationError("desktop_data_clear_identity_failed", f"{field} canonical path could not be read", details={"winerror": ctypes.get_last_error()})
        buffer = ctypes.create_unicode_buffer(size + 1)
        if not kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0):
            raise DesktopOperationError("desktop_data_clear_identity_failed", f"{field} canonical path could not be read", details={"winerror": ctypes.get_last_error()})
        final = buffer.value
    finally:
        kernel32.CloseHandle(handle)
    if final.startswith("\\\\?\\UNC\\"):
        final = "\\\\" + final[8:]
    elif final.startswith("\\\\?\\"):
        final = final[4:]
    return Path(final)

def _norm(value: Any) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _descendant(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(candidate), os.fspath(root)))
    except ValueError:
        return False
    return _norm(common) == _norm(root)


def _overlap(first: Path, second: Path) -> bool:
    return _descendant(first, second) or _descendant(second, first)


def _lexists(path: Path) -> bool:
    return os.path.lexists(windows_extended_path(path))


__all__ = ["BLOCKING_STATES", "CLEAR_CONFIRMATION_TEXT", "MARKER_NAME", "OwnedDataClearOwner", "clear_paths", "fence_normal_initialization"]

