from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from framework.shared.local_desktop import utc_timestamp
from scripts.desktop_app.config import DesktopAppConfig, _reject_lexical_reparse_chain, controlled_process_environment
from scripts.desktop_app.runtime_profile import (
    DEFAULT_PROFILE_NAME,
    load_desktop_runtime_profile,
    validate_portable_runtime_attestation,
)
from scripts.local_desktop.lifecycle_cancellation import check_startup_cancellation
from scripts.local_desktop.common import (
    DesktopOperationError,
    atomic_write_json,
    ensure_contained,
    read_bytes_long_path,
    reject_reparse_path,
    windows_extended_path,
)
from scripts.local_desktop.release import DATA_SCHEMAS, RELEASE_SCHEMA, ReleaseManager
from scripts.local_desktop.safe_tree import SafeTreeSnapshot, scan_tree

BUNDLE_SCHEMA = "newsroom.desktop-app-bundle/v1"
BINDING_SCHEMA = "newsroom.desktop-app-release-binding/v1"
PORTABLE_PYTHON_SCHEMA = "newsroom.portable-python/v1"
PORTABLE_NODE_SCHEMA = "newsroom.portable-node/v1"
_ORIGINAL_MANIFEST_PATH = ".desktop-app/original-release-manifest.json"
_DEFAULT_RUNTIME_PROFILE = load_desktop_runtime_profile(DEFAULT_PROFILE_NAME)
_DEFAULT_IMPORTS = _DEFAULT_RUNTIME_PROFILE.required_imports
_DEFAULT_IMPORT_ATTRIBUTES = _DEFAULT_RUNTIME_PROFILE.required_attributes
_PORTABLE_PYTHON_EXCLUDED_INPUT_PATHS = ("Lib/site-packages/pip-26.2.1.dist-info",)
_EXTERNAL_CONFIG_ENV_NAMES = ("NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG", "NEWS_SOURCES_CONFIG")
_SOURCE_PREFIXES = {"app": "", "frontend": "frontend"}
_ALLOWED_RELOCATIONS = (
    "resource_root",
    "data_root",
    "release_path",
    "canonical_origin",
    "api_base_url",
    "python_executable_absolute_path",
    "node_executable_absolute_path",
    "npm_executable_absolute_path",
)
_PYTHON_LAUNCH_REQUIREMENTS = {
    "arguments": ["-B", "-s"],
    "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
}
_FORBIDDEN_RELOCATIONS = (
    "source_revision",
    "source_digest",
    "release_resources",
    "data_compatibility",
    "python_version",
    "python_packages",
    "node_version",
    "npm_version",
)


class DependencyIdentityProvider(Protocol):
    def dependency_identity(self, frontend_root: Path) -> dict[str, Any]: ...

    def dependency_identity_for_runtime(
        self,
        frontend_root: str | Path,
        *,
        python_executable: str | Path,
        node_executable: str | Path,
        npm_executable: str | Path,
        application_root: str | Path,
        required_python_imports: Sequence[str] = (),
        required_python_attributes: Sequence[tuple[str, str]] = (),
    ) -> dict[str, Any]: ...

    def configuration_identity_for_runtime(self, release_path: str | Path) -> dict[str, Any]: ...

    def verify(self, release: str | Path) -> dict[str, Any]: ...

    def activate(self, release: str | Path) -> dict[str, Any]: ...

    def verify_and_activate(self, release: str | Path) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def active_with_pointer(self) -> tuple[Path, dict[str, Any], dict[str, Any]]: ...


class DesktopAppReleaseManager(ReleaseManager):
    """Desktop package integration that retains ReleaseManager as identity owner.

    The public runtime identity extension is a production dependency-validation
    responsibility: executable, package, lock, and installed-tree identities are
    always computed by ReleaseManager rather than accepted from this adapter.
    """


class PortableBundleManager:
    def __init__(
        self,
        config: DesktopAppConfig,
        *,
        release_manager: DependencyIdentityProvider | None = None,
        command_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    ) -> None:
        self.config = config
        self.release_manager = release_manager or DesktopAppReleaseManager(config.desktop)
        self.command_runner = command_runner or subprocess.run

    def verify_bundle(self) -> dict[str, Any]:
        envelope, _original = self._verify_bundle_details()
        return envelope

    def _verify_bundle_details(self) -> tuple[dict[str, Any], dict[str, Any]]:
        check_startup_cancellation()
        root = self.config.resource_root
        _reject_lexical_reparse_chain(root)
        envelope_path = root / "desktop-bundle.json"
        reject_reparse_path(envelope_path, root=root)
        if not _is_file_long_path(envelope_path):
            raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle manifest is missing")
        snapshot = scan_tree(
            root,
            excluded={"desktop-bundle.json"},
            captured={"desktop-bundle.json", _ORIGINAL_MANIFEST_PATH},
            deferred=("node/npm.cmd", "node/node.exe", ".venv/Scripts/python.exe"),
            filesystem_reason_code="desktop_app_bundle_filesystem_scan_failed",
            filesystem_message="portable runtime filesystem scan failed",
            node_type_reason_code="desktop_app_bundle_resource_type_invalid",
        )
        envelope_data = snapshot.captured.get("desktop-bundle.json")
        if envelope_data is None:
            raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle manifest is missing")
        envelope = _decode_json_object(
            envelope_data,
            name="desktop-bundle.json",
            reason_code="desktop_app_bundle_manifest_invalid",
        )
        if envelope.get("schema") != BUNDLE_SCHEMA:
            raise DesktopOperationError("desktop_app_bundle_schema_unsupported", "desktop app bundle schema is unsupported")
        resources = envelope.get("resources")
        if not isinstance(resources, list) or not resources:
            raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle resource inventory is empty")
        expected: dict[str, Mapping[str, Any]] = {}
        for item in resources:
            check_startup_cancellation()
            if not isinstance(item, Mapping):
                raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle resource entry is invalid")
            relative = _safe_relative(item.get("path"))
            if relative in expected or relative == "desktop-bundle.json":
                raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle resource inventory contains duplicates")
            expected[relative] = item
        for relative, item in expected.items():
            identity = snapshot.resources.get(relative)
            if identity is None:
                raise DesktopOperationError("desktop_app_bundle_resource_missing", f"desktop app bundle resource is missing: {relative}")
            if identity[0] != item.get("size") or identity[1] != item.get("sha256"):
                raise DesktopOperationError("desktop_app_bundle_checksum_mismatch", f"desktop app bundle checksum mismatch: {relative}")
        actual = set(snapshot.resources)
        expected_paths = set(expected)
        if actual != expected_paths:
            raise DesktopOperationError(
                "desktop_app_bundle_resource_inventory_mismatch",
                "desktop app bundle contains missing or unmanifested resources",
                details={"missing_count": len(expected_paths - actual), "extra_count": len(actual - expected_paths)},
            )
        original = self._original_manifest(envelope, verified_data=snapshot.captured.get(_ORIGINAL_MANIFEST_PATH))
        _validate_release_compatibility(original)
        _reject_external_release_config(original)
        _validate_relocation_policy(envelope)
        expected_bundle_id = _bundle_id(envelope["release"]["original_identity"], envelope["runtime"], resources, envelope["relocation_policy"])
        if envelope.get("bundle_id") != expected_bundle_id:
            raise DesktopOperationError("desktop_app_bundle_identity_mismatch", "desktop app bundle identity does not match its envelope")
        self._verify_runtime(envelope, original, snapshot=snapshot)
        check_startup_cancellation()
        return envelope, original

    def install_and_activate(self) -> dict[str, Any]:
        # Configuration calculation and verification must see the same trusted
        # App projection, including its explicit disabled-LLM sentinel.
        with controlled_process_environment(self.config):
            return self._install_and_activate()

    def _install_and_activate(self) -> dict[str, Any]:
        envelope, original = self._verify_bundle_details()
        binding_digest = _json_digest(
            {
                "bundle_id": envelope["bundle_id"],
                "canonical_origin": self.config.desktop.canonical_origin,
                "api_base_url": self.config.desktop.api_base_url,
                "python_executable": str(self.config.python_executable),
                "node_executable": str(self.config.node_executable),
                "npm_executable": str(self.config.npm_executable),
                "storage_mode": self.config.desktop.storage_mode,
            }
        )
        release_id = str(original.get("identity", {}).get("release_id") or "")
        if not release_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for char in release_id):
            raise DesktopOperationError("desktop_app_bundle_release_identity_invalid", "desktop app bundle release identity is invalid")
        destination = self.config.desktop.release_root / f"{release_id}-desktop-{binding_digest[:12]}"
        if destination.exists():
            reject_reparse_path(destination, root=self.config.desktop.release_root)
            manifest = _read_json(destination / "release-manifest.json", "desktop_release_manifest_invalid")
            binding = manifest.get("desktop_app_binding")
            if not isinstance(binding, Mapping) or binding.get("binding_digest") != binding_digest or binding.get("bundle_id") != envelope.get("bundle_id"):
                raise DesktopOperationError("desktop_app_release_binding_conflict", "existing local release does not match the verified bundle binding")
            preserved = self._active_runtime_rebind(
                base_release=destination,
                base_release_id=str(manifest.get("identity", {}).get("release_id") or ""),
                binding_digest=binding_digest,
                bundle_id=str(envelope["bundle_id"]),
            )
            if preserved is not None:
                active_manifest, pointer, preserved_runtime_configuration = preserved
            else:
                check_startup_cancellation()
                active_manifest, pointer = self.release_manager.verify_and_activate(destination)
                preserved_runtime_configuration = False
            return {
                "release": active_manifest.get("identity", {}),
                "binding": dict(binding),
                "activation": pointer,
                "reused": True,
                "preserved_runtime_configuration": preserved_runtime_configuration,
            }

        self.config.desktop.release_root.mkdir(parents=True, exist_ok=True)
        staging_root = self.config.desktop.release_root / ".desktop-app-staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        reject_reparse_path(staging_root, root=self.config.desktop.release_root)
        stage = Path(tempfile.mkdtemp(prefix="binding-", dir=staging_root))
        try:
            check_startup_cancellation()
            self._materialize_release(stage, original)
            check_startup_cancellation()
            dependencies = self.release_manager.dependency_identity(stage / "frontend")
            _validate_relocated_dependency_identity(original["dependencies"], dependencies)
            config_identity = {
                "schema": "newsroom.local-desktop-config/v1",
                "profile": self.config.desktop.deployment_profile,
                "canonical_origin": self.config.desktop.canonical_origin,
                "api_base_url": self.config.desktop.api_base_url,
                "parser_mode": self.config.desktop.parser_mode,
                "storage_mode": self.config.desktop.storage_mode,
                "data_mode": "real",
                "external_config_dependencies": _external_config_identity(
                    stage,
                    declared=original.get("config", {}).get("external_config_dependencies"),
                ),
            }
            _validate_relocated_config_identity(original["config"], config_identity)
            local_identity = dict(original["identity"])
            local_identity["config_digest"] = _json_digest(config_identity)
            local_identity["dependency_digest"] = _json_digest(dependencies)
            manifest = {
                **dict(original),
                "identity": local_identity,
                "config": config_identity,
                "dependencies": dependencies,
                "entrypoints": {
                    "api": ["-m", "interfaces.cli.news", "api", "serve", "--host", self.config.desktop.api_host, "--port", str(self.config.desktop.api_port)],
                    "frontend": _bound_frontend_entrypoint(original, host=self.config.desktop.frontend_host, port=self.config.desktop.frontend_port),
                },
                "desktop_app_binding": {
                    "schema": BINDING_SCHEMA,
                    "bundle_id": envelope["bundle_id"],
                    "binding_digest": binding_digest,
                    "bound_at": utc_timestamp(),
                    "original_identity": dict(original["identity"]),
                    "relocated_fields": [
                        "release_path",
                        "config.canonical_origin",
                        "config.api_base_url",
                        "entrypoints.api.host_port",
                        "entrypoints.frontend.host_port",
                        "dependencies.python.executable",
                        "dependencies.node.executable",
                        "dependencies.node.npm_executable",
                    ],
                },
            }
            # The owner calculator reads the manifest to distinguish a base
            # binding from a runtime rebind. Publish the original relocated
            # metadata in the private stage first, then replace its config with
            # the exact owner-computed runtime identity before any verification.
            atomic_write_json(stage / "release-manifest.json", manifest)
            config_identity = self.release_manager.configuration_identity_for_runtime(stage)
            manifest["config"] = config_identity
            manifest["identity"] = {
                **local_identity,
                "config_digest": _json_digest(config_identity),
            }
            atomic_write_json(stage / "release-manifest.json", manifest)
            self.release_manager.verify(stage)
            os.replace(stage, destination)
            verified, pointer = self.release_manager.verify_and_activate(destination)
            return {
                "release": verified.get("identity", {}),
                "binding": verified.get("desktop_app_binding", {}),
                "activation": pointer,
                "reused": False,
            }
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise

    def _active_runtime_rebind(
        self,
        *,
        base_release: Path,
        base_release_id: str,
        binding_digest: str,
        bundle_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool] | None:
        try:
            active_path, active_manifest, active_pointer = self.release_manager.active_with_pointer()
        except DesktopOperationError as exc:
            if exc.reason_code == "desktop_release_not_activated":
                return None
            raise
        active_path = Path(active_path)
        if active_path == base_release:
            return active_manifest, active_pointer, False
        runtime_binding = active_manifest.get("desktop_runtime_configuration")
        app_binding = active_manifest.get("desktop_app_binding")
        if not isinstance(runtime_binding, Mapping) or runtime_binding.get("source_release_id") != base_release_id:
            return None
        if not isinstance(app_binding, Mapping) or app_binding.get("binding_digest") != binding_digest or app_binding.get("bundle_id") != bundle_id:
            raise DesktopOperationError(
                "desktop_app_active_release_binding_conflict",
                "active runtime-configured release does not match the verified desktop bundle binding",
            )
        return active_manifest, active_pointer, True

    def _original_manifest(self, envelope: Mapping[str, Any], *, verified_data: bytes | None = None) -> dict[str, Any]:
        descriptor = envelope.get("release")
        if not isinstance(descriptor, Mapping) or descriptor.get("manifest_path") != _ORIGINAL_MANIFEST_PATH:
            raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle release descriptor is invalid")
        data = verified_data or b""
        if not data or sha256(data).hexdigest() != descriptor.get("manifest_sha256"):
            raise DesktopOperationError("desktop_app_bundle_release_manifest_mismatch", "original release manifest does not match the bundle envelope")
        manifest = _decode_json_object(
            data,
            name=Path(_ORIGINAL_MANIFEST_PATH).name,
            reason_code="desktop_app_bundle_release_manifest_invalid",
        )
        if manifest.get("identity") != descriptor.get("original_identity"):
            raise DesktopOperationError("desktop_app_bundle_release_identity_invalid", "original release identity does not match the bundle envelope")
        return manifest

    def _verify_runtime(
        self,
        envelope: Mapping[str, Any],
        original: Mapping[str, Any],
        *,
        snapshot: SafeTreeSnapshot,
    ) -> None:
        runtime = envelope.get("runtime")
        if not isinstance(runtime, Mapping):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle runtime descriptor is missing")
        python = runtime.get("python")
        node = runtime.get("node")
        if not isinstance(python, Mapping) or not isinstance(node, Mapping):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle runtime descriptor is incomplete")
        _verify_executable(snapshot.resources, python, expected_relative=".venv/Scripts/python.exe")
        if python.get("launch_requirements") != _PYTHON_LAUNCH_REQUIREMENTS:
            raise DesktopOperationError(
                "desktop_app_bundle_runtime_invalid",
                "desktop app Python launch requirements do not protect the immutable payload",
            )
        excluded_input_paths = python.get("excluded_input_paths")
        if not isinstance(excluded_input_paths, list) or any(
            not isinstance(item, str) or item not in _PORTABLE_PYTHON_EXCLUDED_INPUT_PATHS
            for item in excluded_input_paths
        ) or len(excluded_input_paths) != len(set(excluded_input_paths)):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle Python input exclusions are invalid")
        for relative in excluded_input_paths:
            excluded = (Path(".venv/Scripts") / relative).as_posix()
            if excluded in snapshot.nodes or any(path.startswith(excluded + "/") for path in snapshot.nodes):
                raise DesktopOperationError("desktop_app_python_runtime_drift", "excluded portable Python metadata is present in the bundle")
        _verify_executable(snapshot.resources, node, expected_relative="node/node.exe")
        npm = node.get("npm")
        if not isinstance(npm, Mapping):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle npm descriptor is missing")
        _verify_executable(snapshot.resources, npm, expected_relative="node/npm.cmd")
        actual_python = Path(sys.executable).resolve(strict=False)
        if os.path.normcase(str(actual_python)) != os.path.normcase(str(self.config.python_executable.resolve(strict=False))):
            raise DesktopOperationError("desktop_app_wrong_python", "desktop app CLI must run with the bundled interpreter")
        required_imports = tuple(python.get("required_imports") or ())
        if set(required_imports) != set(_DEFAULT_IMPORTS):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle Python capability imports do not match the selected profile")
        if python.get("profile") != _DEFAULT_RUNTIME_PROFILE.identity():
            raise DesktopOperationError("desktop_app_runtime_profile_mismatch", "desktop app bundle Python profile identity is invalid")
        if not isinstance(python.get("builder"), Mapping) or not isinstance(python.get("base_python"), Mapping) or not isinstance(python.get("wheelhouse"), Mapping):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle Python build provenance is incomplete")
        if not isinstance(python.get("record_count"), int) or int(python["record_count"]) < 1 or not isinstance(python.get("license_count"), int) or int(python["license_count"]) < 1:
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle Python package evidence is incomplete")
        if python.get("required_attributes") != _attribute_descriptors(_DEFAULT_IMPORT_ATTRIBUTES):
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle Python capability attributes are incomplete")
        python_probe = _python_probe(
            self.config.python_executable,
            required_imports,
            _DEFAULT_IMPORT_ATTRIBUTES,
            self.config.resource_root,
            self.command_runner,
        )
        if _normalized_package_identity(python_probe.get("packages")) != {
            _normalize_distribution_name(name): version for name, version in _DEFAULT_RUNTIME_PROFILE.packages
        }:
            raise DesktopOperationError("desktop_app_runtime_profile_mismatch", "bundled Python distributions do not match the selected profile")
        if (
            _json_digest(python_probe) != python.get("probe_digest")
            or python.get("distribution_count") != len(python_probe["packages"])
            or python.get("sqlite_version") != python_probe.get("sqlite_version")
            or python.get("openssl_version") != python_probe.get("openssl_version")
        ):
            raise DesktopOperationError("desktop_app_python_runtime_drift", "bundled Python runtime or installed packages drifted from the envelope")
        node_version = _tool_output([str(self.config.node_executable), "--version"], self.command_runner, timeout=15)
        if npm.get("cli_path") != "node/node_modules/npm/bin/npm-cli.js":
            raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle npm CLI path is invalid")
        npm_cli = self.config.node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
        if not _is_file_long_path(npm_cli):
            raise DesktopOperationError("desktop_app_npm_runtime_missing", "bundled npm CLI resource is missing")
        npm_version = _tool_output([str(self.config.node_executable), str(npm_cli), "--version"], self.command_runner, timeout=20)
        if node_version != node.get("version") or npm_version != npm.get("version"):
            raise DesktopOperationError("desktop_app_node_runtime_drift", "bundled Node.js or npm runtime drifted from the envelope")
        original_dependencies = original.get("dependencies")
        if not isinstance(original_dependencies, Mapping):
            raise DesktopOperationError("desktop_app_bundle_release_manifest_invalid", "original release dependency identity is missing")
        original_python = original_dependencies.get("python")
        original_node = original_dependencies.get("node")
        if not isinstance(original_python, Mapping) or not isinstance(original_node, Mapping):
            raise DesktopOperationError("desktop_app_bundle_release_manifest_invalid", "original release dependency identity is incomplete")
        if python_probe.get("version") != original_python.get("version") or python_probe.get("packages") != original_python.get("packages"):
            raise DesktopOperationError("desktop_app_python_dependency_mismatch", "portable Python dependencies do not match the verified release")
        expected_profile_provenance = original_python.get("runtime_profile")
        actual_profile_provenance = {
            "profile": dict(python["profile"]),
            "base_python": {
                "source_sha256": python["base_python"].get("source_sha256"),
                "manifest_sha256": python["base_python"].get("manifest_sha256"),
            },
            "wheelhouse": {
                "manifest_sha256": python["wheelhouse"].get("manifest_sha256"),
                "receipt_sha256": python["wheelhouse"].get("receipt_sha256"),
                "artifact_count": python["wheelhouse"].get("artifact_count"),
            },
            "builder": dict(python["builder"]),
        }
        if expected_profile_provenance != actual_profile_provenance:
            raise DesktopOperationError("desktop_app_runtime_profile_mismatch", "portable Python provenance does not match the verified release dependency identity")
        if node_version != original_node.get("version") or npm_version != original_node.get("npm_version"):
            raise DesktopOperationError("desktop_app_node_dependency_mismatch", "portable Node.js dependencies do not match the verified release")

    def _materialize_release(self, destination: Path, original: Mapping[str, Any]) -> None:
        resources = original.get("resources")
        if not isinstance(resources, list) or not resources:
            raise DesktopOperationError("desktop_app_bundle_release_manifest_invalid", "original release resource inventory is empty")
        for item in resources:
            if not isinstance(item, Mapping):
                raise DesktopOperationError("desktop_app_bundle_release_manifest_invalid", "original release resource entry is invalid")
            original_relative = _safe_relative(item.get("path"))
            source_relative = _bundle_source_path(original_relative)
            source = ensure_contained(self.config.resource_root, self.config.resource_root / source_relative, field="bundle release resource")
            _reject_lexical_reparse_chain(self.config.resource_root / source_relative)
            reject_reparse_path(source, root=self.config.resource_root)
            data = read_bytes_long_path(source) if _is_file_long_path(source) else b""
            if len(data) != item.get("size") or sha256(data).hexdigest() != item.get("sha256"):
                raise DesktopOperationError("desktop_app_bundle_release_resource_mismatch", f"bundle release resource does not match original identity: {original_relative}")
            target = ensure_contained(destination, destination / original_relative, field="local release resource")
            windows_extended_path(target.parent).mkdir(parents=True, exist_ok=True)
            with open(windows_extended_path(target), "wb") as handle:
                handle.write(data)


def stage_portable_bundle(
    *,
    output_root: str | Path,
    source_release: str | Path,
    python_root: str | Path,
    node_root: str | Path,
    release_manager: DesktopAppReleaseManager,
    required_imports: Sequence[str] = _DEFAULT_IMPORTS,
    command_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> dict[str, Any]:
    runner = command_runner or subprocess.run
    output = _absolute_bundle_path(output_root, field="bundle output")
    _reject_lexical_reparse_chain(output)
    if output.exists():
        reject_reparse_path(output, root=output)
        if not output.is_dir() or any(output.iterdir()):
            raise DesktopOperationError("desktop_app_bundle_output_not_empty", "portable bundle output directory must be absent or empty")
    lexical_release = _source_release_lexical_path(source_release, release_manager)
    _reject_lexical_reparse_chain(lexical_release)
    source_manifest = release_manager.verify(source_release)
    _validate_release_compatibility(source_manifest)
    _reject_external_release_config(source_manifest)
    release_path = release_manager.resolve_release(source_release).resolve(strict=True)
    python_lexical = _absolute_bundle_path(python_root, field="portable Python root")
    node_lexical = _absolute_bundle_path(node_root, field="portable Node root")
    _reject_lexical_reparse_chain(python_lexical)
    _reject_lexical_reparse_chain(node_lexical)
    python_source = python_lexical.resolve(strict=True)
    node_source = node_lexical.resolve(strict=True)
    _reject_overlapping_bundle_paths(
        output,
        {"source_release": release_path, "portable_python": python_source, "portable_node": node_source},
    )
    _reject_nonportable_venv(python_source)
    python_attestation = _portable_attestation(python_source / "portable-python.json", PORTABLE_PYTHON_SCHEMA)
    validate_portable_runtime_attestation(python_attestation, _DEFAULT_RUNTIME_PROFILE)
    node_attestation = _portable_attestation(node_source / "portable-node.json", PORTABLE_NODE_SCHEMA)
    _reject_nonportable_venv(python_source)
    _require_attested_relative(python_attestation, "executable", "python.exe")
    _require_attested_executable_hash(python_attestation, python_source / "python.exe", runtime="Python")
    _require_attested_relative(node_attestation, "executable", "node.exe")
    _require_attested_relative(node_attestation, "npm_executable", "npm.cmd")

    output.parent.mkdir(parents=True, exist_ok=True)
    reject_reparse_path(output.parent, root=output.parent.parent)
    stage = Path(tempfile.mkdtemp(prefix="desktop-bundle-", dir=output.parent))
    try:
        python_excluded_input_paths = _copy_tree_checked(
            python_source,
            stage / ".venv" / "Scripts",
            excluded_relative_paths=_PORTABLE_PYTHON_EXCLUDED_INPUT_PATHS,
        )
        _copy_tree_checked(node_source, stage / "node")
        for item in source_manifest["resources"]:
            check_startup_cancellation()
            original_relative = _safe_relative(item.get("path"))
            source = ensure_contained(release_path, release_path / original_relative, field="source release resource")
            reject_reparse_path(source, root=release_path)
            target = stage / _bundle_source_path(original_relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_long_path(source, target)
        original_manifest_path = stage / _ORIGINAL_MANIFEST_PATH
        original_manifest_path.parent.mkdir(parents=True, exist_ok=True)

        python_executable = stage / ".venv" / "Scripts" / "python.exe"
        node_executable = stage / "node" / "node.exe"
        npm_executable = stage / "node" / "npm.cmd"
        npm_cli = stage / "node" / "node_modules" / "npm" / "bin" / "npm-cli.js"
        for required in (python_executable, node_executable, npm_executable, npm_cli):
            if not _is_file_long_path(required):
                raise DesktopOperationError("desktop_app_portable_runtime_incomplete", f"portable runtime resource is missing: {required.relative_to(stage).as_posix()}")
        normalized_imports = tuple(dict.fromkeys(required_imports))
        if set(normalized_imports) != set(_DEFAULT_IMPORTS):
            raise DesktopOperationError(
                "desktop_app_portable_python_capabilities_incomplete",
                "portable Python validation imports must match the selected desktop capability profile",
            )
        check_startup_cancellation()
        python_probe = _python_probe(
            python_executable,
            normalized_imports,
            _DEFAULT_IMPORT_ATTRIBUTES,
            stage,
            runner,
        )
        check_startup_cancellation()
        node_version = _tool_output([str(node_executable), "--version"], runner, timeout=15)
        npm_version = _tool_output([str(node_executable), str(npm_cli), "--version"], runner, timeout=20)
        _validate_attested_probe(python_attestation, python_probe, runtime="Python")
        if str(node_attestation.get("version") or "") != node_version:
            raise DesktopOperationError("desktop_app_portable_node_attestation_mismatch", "portable Node.js version does not match its attestation")
        if str(node_attestation.get("npm_version") or "") != npm_version:
            raise DesktopOperationError("desktop_app_portable_node_attestation_mismatch", "portable npm version does not match its attestation")
        check_startup_cancellation()
        runtime_dependencies = release_manager.dependency_identity_for_runtime(
            stage / "frontend",
            python_executable=python_executable,
            node_executable=node_executable,
            npm_executable=npm_executable,
            application_root=stage,
            required_python_imports=normalized_imports,
            required_python_attributes=_DEFAULT_IMPORT_ATTRIBUTES,
        )
        _validate_runtime_identity_probe(runtime_dependencies, python_probe, node_version, npm_version)
        portable_dependencies = _portable_dependency_paths(runtime_dependencies)
        portable_manifest = _portable_release_manifest(source_manifest, portable_dependencies)
        atomic_write_json(original_manifest_path, portable_manifest)

        check_startup_cancellation()
        resources = _resource_entries(stage, excluded={"desktop-bundle.json"})
        check_startup_cancellation()
        original_bytes = read_bytes_long_path(original_manifest_path)
        runtime = {
            "python": {
                "path": ".venv/Scripts/python.exe",
                "sha256": _file_sha256(python_executable),
                "required_imports": list(normalized_imports),
                "required_attributes": _attribute_descriptors(_DEFAULT_IMPORT_ATTRIBUTES),
                "probe_digest": _json_digest(python_probe),
                "version": python_probe["version"],
                "abi": python_probe["abi"],
                "platform": python_probe["platform"],
                "sqlite_version": python_probe["sqlite_version"],
                "openssl_version": python_probe["openssl_version"],
                "distribution_count": len(python_probe["packages"]),
                "excluded_input_paths": list(python_excluded_input_paths),
                "launch_requirements": {
                    "arguments": list(_PYTHON_LAUNCH_REQUIREMENTS["arguments"]),
                    "environment": dict(_PYTHON_LAUNCH_REQUIREMENTS["environment"]),
                },
                "profile": dict(python_attestation["profile"]),
                "builder": dict(python_attestation["builder"]),
                "base_python": dict(python_attestation["base_python"]),
                "wheelhouse": dict(python_attestation["wheelhouse"]),
                "record_count": python_attestation["record_count"],
                "license_count": len(python_attestation["licenses"]),
            },
            "node": {
                "path": "node/node.exe",
                "sha256": _file_sha256(node_executable),
                "version": node_version,
                "npm": {
                    "path": "node/npm.cmd",
                    "sha256": _file_sha256(npm_executable),
                    "version": npm_version,
                    "cli_path": "node/node_modules/npm/bin/npm-cli.js",
                },
            },
        }
        relocation_policy = {"allowed": list(_ALLOWED_RELOCATIONS), "forbidden": list(_FORBIDDEN_RELOCATIONS)}
        envelope = {
            "schema": BUNDLE_SCHEMA,
            "bundle_id": _bundle_id(portable_manifest["identity"], runtime, resources, relocation_policy),
            "created_at": utc_timestamp(),
            "release": {
                "manifest_path": _ORIGINAL_MANIFEST_PATH,
                "manifest_sha256": sha256(original_bytes).hexdigest(),
                "original_identity": portable_manifest["identity"],
                "source_release_identity": source_manifest["identity"],
            },
            "runtime": runtime,
            "relocation_policy": relocation_policy,
            "resources": resources,
        }
        atomic_write_json(stage / "desktop-bundle.json", envelope)
        check_startup_cancellation()
        if output.exists():
            output.rmdir()
        os.replace(stage, output)
        return envelope
    except Exception:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
        raise



def _bound_frontend_entrypoint(original: Mapping[str, Any], *, host: str, port: int) -> list[str]:
    entrypoints = original.get("entrypoints")
    frontend = entrypoints.get("frontend") if isinstance(entrypoints, Mapping) else None
    if frontend == ["node", "server.js"]:
        return ["node", "server.js"]
    if not isinstance(frontend, list) or len(frontend) != 6 or frontend[:3] != ["next", "start", "-H"] or frontend[4] != "-p":
        raise DesktopOperationError("desktop_release_manifest_invalid", "frontend entrypoint is invalid")
    return ["next", "start", "-H", host, "-p", str(port)]

def _absolute_bundle_path(value: str | Path, *, field: str) -> Path:
    selected = Path(value).expanduser()
    if ".." in selected.parts:
        raise DesktopOperationError("desktop_app_bundle_path_invalid", f"{field} must not contain parent traversal")
    return Path(os.path.abspath(os.fspath(selected)))


def _source_release_lexical_path(source_release: str | Path, release_manager: DesktopAppReleaseManager) -> Path:
    selected = Path(source_release).expanduser()
    if selected.is_absolute():
        return _absolute_bundle_path(selected, field="source release")
    config = getattr(release_manager, "config", None)
    release_root = getattr(config, "release_root", None)
    if release_root is None:
        raise DesktopOperationError("desktop_app_bundle_release_path_invalid", "relative source release requires an owned release root")
    return _absolute_bundle_path(Path(release_root) / selected, field="source release")


def _reject_overlapping_bundle_paths(output: Path, inputs: Mapping[str, Path]) -> None:
    for name, input_root in inputs.items():
        if output == input_root or output.is_relative_to(input_root) or input_root.is_relative_to(output):
            raise DesktopOperationError(
                "desktop_app_bundle_path_overlap",
                f"portable bundle output must not overlap {name}",
            )


def _python_probe(
    executable: Path,
    required_imports: tuple[str, ...],
    required_attributes: tuple[tuple[str, str], ...],
    application_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[Any]],
) -> dict[str, Any]:
    forbidden = list(_DEFAULT_RUNTIME_PROFILE.forbidden_default_modules)
    script = (
        "import importlib,importlib.metadata,json,os,platform,sqlite3,ssl,sys,sysconfig,tempfile;"
        f"sys.path.insert(0,{str(application_root)!r});"
        f"mods={list(required_imports)!r};attrs={list(required_attributes)!r};forbidden={forbidden!r};"
        "loaded={name:importlib.import_module(name) for name in mods};"
        "[getattr(importlib.import_module(module),attribute) for module,attribute in attrs];"
        "api=loaded['interfaces.api.app'].app;routes=sorted({getattr(route,'path','') for route in api.routes if getattr(route,'path','')});"
        "document=loaded['pymupdf'].open();page=document.new_page();page.insert_text((72,72),'Agora desktop text PDF');blob=document.tobytes();document.close();"
        "reopened=loaded['pymupdf'].open(stream=blob,filetype='pdf');pdf_text=''.join(page.get_text() for page in reopened);reopened.close();"
        "database=tempfile.NamedTemporaryFile(suffix='.sqlite3',delete=False);database.close();"
        "connection=sqlite3.connect(database.name);journal=connection.execute('pragma journal_mode=wal').fetchone()[0];"
        "connection.execute('create table capability(value text not null)');connection.execute(\"insert into capability values ('memory')\");connection.commit();"
        "stored=connection.execute('select value from capability').fetchone()[0];sqlite_runtime=connection.execute('select sqlite_version()').fetchone()[0];connection.close();os.unlink(database.name);"
        "ssl.create_default_context();"
        "email=loaded['email_validator'].validate_email('reader@example.com',check_deliverability=False).normalized;"
        "phone=loaded['phonenumbers'].is_valid_number(loaded['phonenumbers'].parse('+14155552671',None));"
        "loaded['authlib.jose'].JsonWebToken(['HS256']);"
        "username=loaded['win32api'].GetUserName();token=loaded['win32security'].OpenProcessToken(loaded['win32api'].GetCurrentProcess(),8);"
        "sid=loaded['win32security'].GetTokenInformation(token,loaded['win32security'].TokenUser)[0];sid_text=loaded['win32security'].ConvertSidToStringSid(sid);"
        "unexpected=sorted(name for name in forbidden if name in sys.modules);"
        "packages=sorted([[d.metadata.get('Name',''),d.version] for d in importlib.metadata.distributions() if d.metadata.get('Name')]);"
        "capabilities={'api_routes':len(routes),'api_health':('/api/v1/health' in routes or '/health' in routes),'text_pdf':('Agora desktop text PDF' in pdf_text),"
        "'sqlite_wal':(journal.lower()=='wal' and stored=='memory'),'auth':(email=='reader@example.com' and phone),"
        "'credential_acl':(bool(username) and bool(sid_text)),'tls':True,'optional_modules_loaded':unexpected};"
        "print(json.dumps({'version':sys.version.split()[0],'implementation':platform.python_implementation(),"
        "'cache_tag':sys.implementation.cache_tag,'abi':sysconfig.get_config_var('SOABI') or '',"
        "'platform':sysconfig.get_platform(),'packages':packages,'sqlite_version':sqlite_runtime,"
        "'openssl_version':ssl.OPENSSL_VERSION,'capabilities':capabilities},sort_keys=True,separators=(',',':')))"
    )
    try:
        result = runner(
            [str(executable), "-B", "-I", "-c", script],
            capture_output=True,
            text=False,
            timeout=90,
            check=False,
            shell=False,
            env=_isolated_runtime_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopOperationError("desktop_app_python_probe_timeout", "portable Python validation exceeded its deadline") from exc
    except OSError as exc:
        raise DesktopOperationError("desktop_app_python_probe_failed", "portable Python interpreter could not be started") from exc
    if result.returncode != 0:
        raise DesktopOperationError("desktop_app_python_probe_failed", "portable Python imports or native capability validation failed", details={"returncode": result.returncode})
    try:
        payload = json.loads(_decode_stdout(result.stdout))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise DesktopOperationError("desktop_app_python_probe_invalid", "portable Python probe returned invalid UTF-8 JSON") from exc
    capabilities = payload.get("capabilities") if isinstance(payload, Mapping) else None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("packages"), list)
        or not str(payload.get("sqlite_version") or "")
        or not str(payload.get("openssl_version") or "")
        or not isinstance(capabilities, Mapping)
        or not all(capabilities.get(name) is True for name in ("api_health", "text_pdf", "sqlite_wal", "auth", "credential_acl", "tls"))
        or int(capabilities.get("api_routes") or 0) < 1
        or capabilities.get("optional_modules_loaded") != []
    ):
        raise DesktopOperationError("desktop_app_python_capability_probe_failed", "portable Python default desktop capability probe did not pass")
    return payload

def _normalize_distribution_name(value: str) -> str:
    return value.strip().casefold().replace("_", "-").replace(".", "-")


def _normalized_package_identity(value: Any) -> dict[str, str]:
    if not isinstance(value, list) or any(
        not isinstance(item, list)
        or len(item) != 2
        or not all(isinstance(part, str) and part for part in item)
        for item in value
    ):
        return {}
    result: dict[str, str] = {}
    for name, version in value:
        normalized = _normalize_distribution_name(name)
        if normalized in result:
            return {}
        result[normalized] = version
    return result


def _attribute_descriptors(attributes: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"module": module, "attribute": attribute} for module, attribute in attributes]


def _tool_output(argv: list[str], runner: Callable[..., subprocess.CompletedProcess[Any]], *, timeout: float) -> str:
    try:
        result = runner(
            argv,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            shell=False,
            env=_isolated_runtime_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise DesktopOperationError("desktop_app_runtime_probe_timeout", f"runtime probe exceeded its deadline: {Path(argv[0]).name}") from exc
    except OSError as exc:
        raise DesktopOperationError("desktop_app_runtime_probe_failed", f"runtime probe could not start: {Path(argv[0]).name}") from exc
    if result.returncode != 0:
        raise DesktopOperationError("desktop_app_runtime_probe_failed", f"runtime probe failed: {Path(argv[0]).name}", details={"returncode": result.returncode})
    return _decode_stdout(result.stdout).strip()


def _isolated_runtime_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _portable_attestation(path: Path, schema: str) -> dict[str, Any]:
    value = _read_json(path, "desktop_app_portable_runtime_attestation_invalid")
    if value.get("schema") != schema or value.get("portable") is not True:
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", "portable runtime attestation is missing or unsupported")
    return value


def _require_attested_relative(attestation: Mapping[str, Any], field: str, expected: str) -> None:
    raw = _safe_relative(attestation.get(field))
    if raw != expected:
        raise DesktopOperationError("desktop_app_portable_runtime_layout_invalid", f"portable runtime {field} must map to {expected}")


def _validate_attested_probe(attestation: Mapping[str, Any], probe: Mapping[str, Any], *, runtime: str) -> None:
    for field in ("version", "implementation", "cache_tag", "abi", "platform"):
        if str(attestation.get(field) or "") != str(probe.get(field) or ""):
            raise DesktopOperationError("desktop_app_portable_python_attestation_mismatch", f"portable {runtime} {field} does not match its attestation")


def _reject_nonportable_venv(root: Path) -> None:
    config = root / "pyvenv.cfg"
    if not _is_file_long_path(config):
        return
    try:
        config_text = read_bytes_long_path(config).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise DesktopOperationError("desktop_app_nonportable_python", "portable Python pyvenv.cfg could not be read") from exc
    for raw in config_text.splitlines():
        if "=" not in raw:
            continue
        name, value = (item.strip() for item in raw.split("=", 1))
        if name.casefold() not in {"home", "executable", "command"}:
            continue
        if _looks_absolute(value):
            raise DesktopOperationError("desktop_app_nonportable_python_rejected", "portable Python input contains absolute pyvenv.cfg bindings")


def _looks_absolute(value: str) -> bool:
    normalized = value.strip().strip('"').strip("'")
    return bool(normalized) and (Path(normalized).is_absolute() or (len(normalized) >= 3 and normalized[1:3] in {":\\", ":/"}))


def _require_attested_executable_hash(attestation: Mapping[str, Any], executable: Path, *, runtime: str) -> None:
    expected = str(attestation.get("executable_sha256") or "").casefold()
    if len(expected) != 64 or any(character not in "0123456789abcdef" for character in expected):
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_invalid", f"portable {runtime} executable hash attestation is missing or invalid")
    if not _is_file_long_path(executable) or _file_sha256(executable) != expected:
        raise DesktopOperationError("desktop_app_portable_runtime_attestation_mismatch", f"portable {runtime} executable does not match its attestation")


def _validate_runtime_identity_probe(
    identity: Mapping[str, Any],
    python_probe: Mapping[str, Any],
    node_version: str,
    npm_version: str,
) -> None:
    python = identity.get("python")
    node = identity.get("node")
    if not isinstance(python, Mapping) or not isinstance(node, Mapping):
        raise DesktopOperationError("desktop_app_portable_dependency_identity_invalid", "portable runtime dependency identity is incomplete")
    if python.get("version") != python_probe.get("version") or python.get("packages") != python_probe.get("packages"):
        raise DesktopOperationError("desktop_app_portable_dependency_identity_mismatch", "portable Python dependency identity changed between controlled probes")
    if node.get("version") != node_version or node.get("npm_version") != npm_version:
        raise DesktopOperationError("desktop_app_portable_dependency_identity_mismatch", "portable Node.js dependency identity changed between controlled probes")


def _portable_dependency_paths(identity: Mapping[str, Any]) -> dict[str, Any]:
    python = identity.get("python")
    node = identity.get("node")
    if not isinstance(python, Mapping) or not isinstance(node, Mapping):
        raise DesktopOperationError("desktop_app_portable_dependency_identity_invalid", "portable runtime dependency identity is incomplete")
    return {
        "python": {**dict(python), "executable": ".venv/Scripts/python.exe"},
        "node": {
            **dict(node),
            "executable": "node/node.exe",
            "npm_executable": "node/npm.cmd",
        },
    }


def _portable_release_manifest(source_manifest: Mapping[str, Any], dependencies: Mapping[str, Any]) -> dict[str, Any]:
    identity = dict(source_manifest["identity"])
    dependency_digest = _json_digest(dependencies)
    portable_release_id = f"{identity['release_id']}-portable-{dependency_digest[:12]}"
    identity.update({"release_id": portable_release_id, "dependency_digest": dependency_digest})
    return {
        **dict(source_manifest),
        "identity": identity,
        "dependencies": dict(dependencies),
        "desktop_app_source_release_identity": dict(source_manifest["identity"]),
        "desktop_runtime_profile_transition": {
            "profile": _DEFAULT_RUNTIME_PROFILE.identity(),
            "source_dependency_digest": str(source_manifest["identity"]["dependency_digest"]),
            "selected_dependency_digest": dependency_digest,
        },
    }


def _validate_relocated_dependency_identity(original: Any, bound: Any) -> None:
    if not isinstance(original, Mapping) or not isinstance(bound, Mapping):
        raise DesktopOperationError("desktop_app_release_dependency_drift", "release dependency identity is incomplete")
    original_python = original.get("python")
    bound_python = bound.get("python")
    original_node = original.get("node")
    bound_node = bound.get("node")
    if not all(isinstance(item, Mapping) for item in (original_python, bound_python, original_node, bound_node)):
        raise DesktopOperationError("desktop_app_release_dependency_drift", "release dependency identity is incomplete")
    python_allowed = {"executable"}
    node_allowed = {"executable", "npm_executable"}
    if {key: value for key, value in original_python.items() if key not in python_allowed} != {key: value for key, value in bound_python.items() if key not in python_allowed}:
        raise DesktopOperationError("desktop_app_release_dependency_drift", "Python dependency identity changed outside allowed executable relocation")
    if {key: value for key, value in original_node.items() if key not in node_allowed} != {key: value for key, value in bound_node.items() if key not in node_allowed}:
        raise DesktopOperationError("desktop_app_release_dependency_drift", "Node dependency identity changed outside allowed executable relocation")


def _validate_relocated_config_identity(original: Any, bound: Any) -> None:
    if not isinstance(original, Mapping) or not isinstance(bound, Mapping):
        raise DesktopOperationError("desktop_app_release_config_drift", "release configuration identity is incomplete")
    allowed = {"canonical_origin", "api_base_url"}
    if {key: value for key, value in original.items() if key not in allowed} != {key: value for key, value in bound.items() if key not in allowed}:
        raise DesktopOperationError("desktop_app_release_config_drift", "release configuration changed outside allowed endpoint relocation")


def _validate_relocation_policy(envelope: Mapping[str, Any]) -> None:
    policy = envelope.get("relocation_policy")
    if not isinstance(policy, Mapping):
        raise DesktopOperationError("desktop_app_bundle_relocation_policy_invalid", "desktop app bundle relocation policy is missing")
    if policy.get("allowed") != list(_ALLOWED_RELOCATIONS) or policy.get("forbidden") != list(_FORBIDDEN_RELOCATIONS):
        raise DesktopOperationError("desktop_app_bundle_relocation_policy_invalid", "desktop app bundle relocation policy is not supported")


def _bundle_id(original_identity: Mapping[str, Any], runtime: Mapping[str, Any], resources: list[Any], relocation_policy: Mapping[str, Any]) -> str:
    seed = {
        "original_identity": dict(original_identity),
        "runtime": dict(runtime),
        "resource_digest": _json_digest({"resources": resources}),
        "relocation_policy": dict(relocation_policy),
    }
    return f"desktop-{_json_digest(seed)[:24]}"


def _validate_release_compatibility(manifest: Mapping[str, Any]) -> None:
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise DesktopOperationError("desktop_app_release_schema_unsupported", "desktop app bundle release schema is unsupported")
    compatibility = manifest.get("data_compatibility")
    if not isinstance(compatibility, Mapping):
        raise DesktopOperationError("desktop_app_release_schema_incompatible", "desktop app release data compatibility is missing")
    for mode in ("readable_schemas", "writable_schemas"):
        declared = compatibility.get(mode)
        if not isinstance(declared, Mapping) or dict(declared) != dict(DATA_SCHEMAS):
            raise DesktopOperationError("desktop_app_release_schema_incompatible", "desktop app release data schemas do not match the runtime owner schemas")


def _reject_external_release_config(manifest: Mapping[str, Any]) -> None:
    config = manifest.get("config")
    dependencies = config.get("external_config_dependencies") if isinstance(config, Mapping) else None
    if not isinstance(dependencies, list):
        raise DesktopOperationError("desktop_app_bundle_release_manifest_invalid", "release configuration dependency identity is invalid")
    if any(not isinstance(item, Mapping) or item.get("mode") != "packaged" for item in dependencies):
        raise DesktopOperationError("desktop_app_external_config_not_portable", "portable desktop bundle requires all runtime configuration dependencies to be packaged")


def _external_config_identity(
    release_path: Path,
    *,
    declared: Any,
) -> list[dict[str, Any]]:
    """Verify immutable packaged config independently of the user runtime overlay."""
    if not isinstance(declared, list):
        raise DesktopOperationError(
            "desktop_app_bundle_release_manifest_invalid",
            "release configuration dependency identity is invalid",
        )
    dependencies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in declared:
        if not isinstance(item, Mapping):
            raise DesktopOperationError(
                "desktop_app_bundle_release_manifest_invalid",
                "release configuration dependency identity is invalid",
            )
        name = str(item.get("name") or "")
        if name not in _EXTERNAL_CONFIG_ENV_NAMES or name in seen or item.get("mode") != "packaged":
            raise DesktopOperationError(
                "desktop_app_bundle_release_manifest_invalid",
                "release configuration dependency identity is invalid",
            )
        seen.add(name)
        packaged_relative = Path(_safe_relative(item.get("resource")))
        packaged = ensure_contained(release_path, release_path / packaged_relative, field="packaged runtime configuration")
        _reject_lexical_reparse_chain(release_path / packaged_relative)
        reject_reparse_path(packaged, root=release_path)
        if not _is_file_long_path(packaged):
            raise DesktopOperationError("desktop_release_config_missing", f"packaged runtime configuration file is missing: {name}")
        data = read_bytes_long_path(packaged)
        identity = {
            "name": name,
            "mode": "packaged",
            "resource": packaged_relative.as_posix(),
            "size": len(data),
            "sha256": sha256(data).hexdigest(),
        }
        if identity != dict(item):
            raise DesktopOperationError(
                "desktop_release_config_drift",
                f"packaged runtime configuration identity changed: {name}",
            )
        dependencies.append(identity)
    return dependencies


def _verify_executable(
    resources: Mapping[str, tuple[int, str]],
    descriptor: Mapping[str, Any],
    *,
    expected_relative: str,
) -> None:
    if descriptor.get("path") != expected_relative:
        raise DesktopOperationError("desktop_app_bundle_runtime_invalid", "desktop app bundle executable path is not an allowed relocation")
    identity = resources.get(expected_relative)
    if identity is None or identity[1] != descriptor.get("sha256"):
        raise DesktopOperationError("desktop_app_bundle_executable_drift", f"bundled executable does not match its envelope: {expected_relative}")


def _bundle_source_path(original_relative: str) -> str:
    path = Path(original_relative)
    if not path.parts or path.parts[0] not in _SOURCE_PREFIXES or len(path.parts) < 2:
        raise DesktopOperationError("desktop_app_bundle_release_manifest_invalid", "release resource is outside the supported app/frontend layout")
    prefix = _SOURCE_PREFIXES[path.parts[0]]
    remainder = Path(*path.parts[1:])
    return (Path(prefix) / remainder).as_posix() if prefix else remainder.as_posix()


def _copy_file_long_path(source: Path, target: Path) -> None:
    target_io = windows_extended_path(target)
    target_io.parent.mkdir(parents=True, exist_ok=True)
    with open(windows_extended_path(source), "rb") as source_handle, open(target_io, "wb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle)
    shutil.copystat(windows_extended_path(source), target_io, follow_symlinks=False)


def _copy_tree_checked(
    source: Path,
    destination: Path,
    *,
    excluded_relative_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    _reject_lexical_reparse_chain(source)
    reject_reparse_path(source, root=source)
    normalized_exclusions = tuple(_safe_relative(item) for item in excluded_relative_paths)
    excluded_lookup = {item.casefold(): item for item in normalized_exclusions}
    applied: list[str] = []
    source_files: dict[str, tuple[int, str]] = {}
    destination.mkdir(parents=True, exist_ok=False)
    for directory, names, files in os.walk(
        windows_extended_path(source),
        topdown=True,
        onerror=_raise_bundle_walk_error,
        followlinks=False,
    ):
        check_startup_cancellation()
        lexical_directory = source / Path(directory).relative_to(windows_extended_path(source))
        _reject_lexical_reparse_chain(lexical_directory)
        reject_reparse_path(lexical_directory, root=source)
        for name in list(names):
            check_startup_cancellation()
            lexical = lexical_directory / name
            _reject_lexical_reparse_chain(lexical)
            reject_reparse_path(lexical, root=source)
            relative = lexical.relative_to(source).as_posix()
            canonical = excluded_lookup.get(relative.casefold())
            if canonical is not None:
                names.remove(name)
                if canonical not in applied:
                    applied.append(canonical)
                continue
            target = destination / lexical.relative_to(source)
            windows_extended_path(target).mkdir(exist_ok=True)
        for name in files:
            check_startup_cancellation()
            lexical = lexical_directory / name
            _reject_lexical_reparse_chain(lexical)
            reject_reparse_path(lexical, root=source)
            relative = lexical.relative_to(source).as_posix()
            canonical = excluded_lookup.get(relative.casefold())
            if canonical is not None:
                if canonical not in applied:
                    applied.append(canonical)
                continue
            data = read_bytes_long_path(lexical)
            target = destination / Path(relative)
            target_io = windows_extended_path(target)
            target_io.parent.mkdir(parents=True, exist_ok=True)
            with open(target_io, "wb") as handle:
                handle.write(data)
            shutil.copystat(windows_extended_path(lexical), target_io, follow_symlinks=False)
            source_files[relative] = (len(data), sha256(data).hexdigest())
    check_startup_cancellation()
    copied = _resource_identity_long_path(destination)
    if copied != source_files:
        raise DesktopOperationError(
            "desktop_app_bundle_copy_mismatch",
            "portable runtime copy did not preserve every source file",
            details={"source_count": len(source_files), "destination_count": len(copied)},
        )
    return tuple(applied)


def _raise_bundle_walk_error(error: OSError) -> None:
    raise DesktopOperationError(
        "desktop_app_bundle_filesystem_scan_failed",
        "portable runtime filesystem scan failed",
        details={"winerror": getattr(error, "winerror", None), "errno": error.errno},
    ) from error


def _resource_identity_long_path(root: Path, *, excluded: set[str] | frozenset[str] = frozenset()) -> dict[str, tuple[int, str]]:
    return scan_tree(
        root,
        excluded=excluded,
        filesystem_reason_code="desktop_app_bundle_filesystem_scan_failed",
        filesystem_message="portable runtime filesystem scan failed",
        node_type_reason_code="desktop_app_bundle_resource_type_invalid",
    ).resources

def _resource_entries(root: Path, *, excluded: set[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for relative, (size, digest) in sorted(_resource_identity_long_path(root, excluded=excluded).items()):
        entries.append({"path": relative, "size": size, "sha256": digest})
    if not entries:
        raise DesktopOperationError("desktop_app_bundle_incomplete", "desktop app bundle contains no resources")
    return entries


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle resource path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} or ":" in part or part.endswith((" ", ".")) for part in path.parts):
        raise DesktopOperationError("desktop_app_bundle_manifest_invalid", "desktop app bundle resource path is unsafe")
    return path.as_posix()


def _is_file_long_path(path: Path) -> bool:
    try:
        return windows_extended_path(path).is_file()
    except OSError:
        return False


def _read_json(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        data = read_bytes_long_path(path)
    except OSError as exc:
        raise DesktopOperationError(reason_code, f"invalid UTF-8 JSON: {path.name}") from exc
    return _decode_json_object(data, name=path.name, reason_code=reason_code)


def _decode_json_object(data: bytes, *, name: str, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError(reason_code, f"invalid UTF-8 JSON: {name}") from exc
    if not isinstance(value, dict):
        raise DesktopOperationError(reason_code, f"JSON document must be an object: {name}")
    return value


def _decode_stdout(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        return value
    raise TypeError("captured command output must be bytes or text")


def _json_digest(value: Mapping[str, Any]) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return sha256(read_bytes_long_path(path)).hexdigest()


__all__ = [
    "BINDING_SCHEMA",
    "BUNDLE_SCHEMA",
    "DesktopAppReleaseManager",
    "PortableBundleManager",
    "stage_portable_bundle",
]
