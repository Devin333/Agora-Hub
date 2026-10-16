from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from framework.shared.local_desktop import LocalDesktopConfig, LocalDesktopConfigError, utc_timestamp
from scripts.local_desktop.common import (
    DesktopOperationError,
    atomic_write_json,
    ensure_contained,
    read_bytes_long_path,
    reject_reparse_path,
    windows_extended_path,
)
from scripts.local_desktop.config import REAL_DATA_MODE, sanitized_environment
from scripts.local_desktop.safe_tree import SafeTreeSnapshot, scan_tree

RELEASE_SCHEMA = "newsroom.local-desktop-release/v1"
ACTIVE_RELEASE_SCHEMA = "newsroom.local-desktop-active-release/v1"
CONFIG_SCHEMA = "newsroom.local-desktop-config/v1"
from scripts.local_desktop.storage_contract import STORAGE_SCHEMAS

DATA_SCHEMAS = STORAGE_SCHEMAS
_BACKEND_SOURCE_ROOTS = ("backend", "framework", "infrastructure", "interfaces", "configs", "sdk", "scripts")
_LEGACY_FRONTEND_RUNTIME_ITEMS = (".next", "public", "node_modules", "package.json", "package-lock.json", "next.config.mjs", "root-env.mjs")
_FRONTEND_PRODUCTION_CLOSURE = "production-closure.json"
_FRONTEND_PRODUCTION_SCHEMA = "newsroom.next-standalone-closure/v1"
_SOURCE_INPUT_SCHEMA = "newsroom.local-desktop-source-inputs/v1"
_SOURCE_INPUT_MANIFEST = "app/.desktop-app/source-inputs.json"
_EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".newsroom", "node_modules", ".env"}
_EXTERNAL_CONFIG_ENV_NAMES = ("NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG", "NEWS_SOURCES_CONFIG")


@dataclass(frozen=True)
class ReleaseIdentity:
    release_id: str
    source_revision: str
    source_digest: str
    dependency_digest: str
    config_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "release_id": self.release_id,
            "source_revision": self.source_revision,
            "source_digest": self.source_digest,
            "dependency_digest": self.dependency_digest,
            "config_digest": self.config_digest,
        }


class ReleaseManager:
    def __init__(
        self,
        config: LocalDesktopConfig,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
    ) -> None:
        self.config = config
        self.command_runner = command_runner or subprocess.run

    @property
    def active_pointer(self) -> Path:
        return self.config.runtime_root / "active-release.json"

    def build(self) -> dict[str, Any]:
        self._require_build_inputs()
        windows_extended_path(self.config.release_root).mkdir(parents=True, exist_ok=True)
        staging_parent = self.config.release_root / ".staging"
        windows_extended_path(staging_parent).mkdir(parents=True, exist_ok=True)
        stage = _make_temp_directory_long_path(staging_parent, prefix="release-")
        try:
            environment = sanitized_environment(self.config)
            environment["NEXT_TELEMETRY_DISABLED"] = "1"
            npm = shutil.which("npm.cmd") or shutil.which("npm")
            if not npm:
                raise DesktopOperationError("desktop_npm_missing", "npm is required for the explicit build operation")
            app_root = stage / "app"
            frontend_root = stage / "frontend"
            for name in _BACKEND_SOURCE_ROOTS:
                source = self.config.project_root / name
                if windows_extended_path(source).exists():
                    self._copy_tree(source, app_root / name)
            self._copy_tree(self.config.frontend_root, frontend_root, ignore=_frontend_copy_ignore)
            source_revision = self._git_revision()
            if source_revision == "unavailable":
                raise DesktopOperationError("desktop_source_revision_unavailable", "a production release requires a resolvable source revision")
            source_digest = _source_input_digest(stage)
            _write_source_input_manifest(stage, source_digest=source_digest)
            self._run([npm, "ci"], cwd=frontend_root, env=environment, timeout=900)
            self._run([npm, "run", "build"], cwd=frontend_root, env=environment, timeout=1200)
            _remove_tree_long_path(frontend_root / ".next" / "cache", ignore_errors=True)
            if self.config.deployment_profile == "local-desktop":
                source_installed_tree = self._node_dependency_tree_identity(frontend_root, npm, environment=environment)
                source_installed_tree = {
                    **source_installed_tree,
                    "package_lock_sha256": _file_sha256(frontend_root / "package-lock.json"),
                }
                _materialize_next_standalone(
                    frontend_root,
                    source_digest=source_digest,
                    source_installed_tree=source_installed_tree,
                )
                frontend_entrypoint = ["node", "server.js"]
            else:
                for name in _LEGACY_FRONTEND_RUNTIME_ITEMS:
                    if not windows_extended_path(frontend_root / name).exists():
                        raise DesktopOperationError("desktop_release_incomplete", f"frontend build resource is missing: {name}")
                frontend_entrypoint = ["next", "start", "-H", self.config.frontend_host, "-p", str(self.config.frontend_port)]
            dependencies = self._dependency_identity(frontend_root)
            dependency_digest = _json_digest(dependencies)
            config_identity = {
                "schema": CONFIG_SCHEMA,
                "profile": self.config.deployment_profile,
                "canonical_origin": self.config.canonical_origin,
                "api_base_url": self.config.api_base_url,
                "parser_mode": self.config.parser_mode,
                "storage_mode": self.config.storage_mode,
                "data_mode": REAL_DATA_MODE,
                "external_config_dependencies": _external_config_identity(environment, project_root=self.config.project_root, release_path=stage),
            }
            config_digest = _json_digest(config_identity)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            release_id = f"{timestamp}-{source_digest[:12]}"
            resources = _resource_entries(stage)
            manifest = {
                "schema": RELEASE_SCHEMA,
                "created_at": utc_timestamp(),
                "identity": ReleaseIdentity(release_id, source_revision, source_digest, dependency_digest, config_digest).to_dict(),
                "config": config_identity,
                "dependencies": dependencies,
                "data_compatibility": {"readable_schemas": DATA_SCHEMAS, "writable_schemas": DATA_SCHEMAS},
                "entrypoints": {
                    "api": ["-m", "interfaces.cli.news", "api", "serve", "--host", self.config.api_host, "--port", str(self.config.api_port)],
                    "frontend": frontend_entrypoint,
                },
                "resources": resources,
                "exclusions": [".env", ".env.*", ".newsroom", "development caches", "provider credentials", "user data", "source links and junctions"],
            }
            _atomic_write_json_long_path(stage / "release-manifest.json", manifest)
            destination = self.config.release_root / release_id
            if windows_extended_path(destination).exists():
                raise DesktopOperationError("desktop_release_exists", f"release already exists: {release_id}")
            _replace_long_path(stage, destination)
            verified, _pointer = self.verify_and_activate(destination)
            return verified
        except Exception:
            if windows_extended_path(stage).exists():
                _remove_tree_long_path(stage, ignore_errors=True)
            raise

    def verify(self, release: str | Path) -> dict[str, Any]:
        return self._verify(release)

    def _verify(
        self,
        release: str | Path,
        *,
        runtime_config_document: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = self.resolve_release(release)
        manifest_path = path / "release-manifest.json"
        reject_reparse_path(manifest_path, root=path)
        if not _is_file_long_path(manifest_path):
            raise DesktopOperationError("desktop_release_manifest_missing", "release manifest is missing")
        snapshot = scan_tree(
            path,
            excluded={"release-manifest.json"},
            captured={"release-manifest.json", ".desktop-app/config/models.json", _SOURCE_INPUT_MANIFEST},
            filesystem_reason_code="desktop_release_filesystem_scan_failed",
            filesystem_message="release filesystem could not be inventoried safely",
            node_type_reason_code="desktop_release_resource_type_invalid",
        )
        manifest_data = snapshot.captured.get("release-manifest.json")
        if manifest_data is None:
            raise DesktopOperationError("desktop_release_manifest_missing", "release manifest is missing")
        manifest = _decode_json_object(
            manifest_data,
            reason_code="desktop_release_manifest_invalid",
            message="release manifest is invalid",
        )
        if manifest.get("schema") != RELEASE_SCHEMA:
            raise DesktopOperationError("desktop_release_schema_unsupported", "release manifest schema is unsupported")
        expected = manifest.get("resources")
        if not isinstance(expected, list) or not expected:
            raise DesktopOperationError("desktop_release_incomplete", "release resource inventory is empty")
        expected_by_path: dict[str, Mapping[str, Any]] = {}
        for entry in expected:
            if not isinstance(entry, dict):
                raise DesktopOperationError("desktop_release_manifest_invalid", "release resource entry is invalid")
            relative = _safe_relative(entry.get("path"))
            if relative in expected_by_path:
                raise DesktopOperationError("desktop_release_manifest_invalid", f"duplicate release resource: {relative}")
            expected_by_path[relative] = entry
        for relative, entry in expected_by_path.items():
            identity = snapshot.resources.get(relative)
            if identity is None:
                raise DesktopOperationError("desktop_release_resource_missing", f"release resource is missing: {relative}")
            if identity[0] != entry.get("size") or identity[1] != entry.get("sha256"):
                raise DesktopOperationError("desktop_release_checksum_mismatch", f"release resource checksum mismatch: {relative}")
        actual_paths = set(snapshot.resources)
        expected_paths = set(expected_by_path)
        if actual_paths != expected_paths:
            raise DesktopOperationError(
                "desktop_release_resource_inventory_mismatch",
                "release contains missing or unmanifested resources",
                details={"missing_count": len(expected_paths - actual_paths), "extra_count": len(actual_paths - expected_paths)},
            )
        entrypoints = manifest.get("entrypoints")
        if not isinstance(entrypoints, dict) or set(entrypoints) != {"api", "frontend"}:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release entrypoints are incomplete")
        expected_api = ["-m", "interfaces.cli.news", "api", "serve", "--host", self.config.api_host, "--port", str(self.config.api_port)]
        standalone_frontend = "frontend/production-closure.json" in snapshot.resources
        expected_frontend = (
            ["node", "server.js"]
            if standalone_frontend
            else ["next", "start", "-H", self.config.frontend_host, "-p", str(self.config.frontend_port)]
        )
        if entrypoints.get("api") != expected_api:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release API entrypoint is invalid")
        if entrypoints.get("frontend") != expected_frontend:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release frontend entrypoint is invalid")
        required_frontend_resource = "frontend/server.js" if standalone_frontend else "frontend/node_modules/next/dist/bin/next"
        if required_frontend_resource not in snapshot.resources:
            raise DesktopOperationError("desktop_release_incomplete", "release Next.js runtime is missing")
        identity = manifest.get("identity")
        if not isinstance(identity, Mapping):
            raise DesktopOperationError("desktop_release_manifest_invalid", "release identity is missing")
        source_revision = str(identity.get("source_revision") or "")
        source_digest = str(identity.get("source_digest") or "")
        if len(source_revision) not in {40, 64} or any(char not in "0123456789abcdef" for char in source_revision.casefold()):
            raise DesktopOperationError("desktop_release_manifest_invalid", "release source revision is invalid")
        if len(source_digest) != 64 or any(char not in "0123456789abcdef" for char in source_digest):
            raise DesktopOperationError("desktop_release_manifest_invalid", "release source digest is invalid")
        if _verified_source_input_digest(snapshot) != identity.get("source_digest"):
            raise DesktopOperationError("desktop_release_source_drift", "packaged source provenance does not match the release identity")
        config_identity = manifest.get("config")
        if not isinstance(config_identity, Mapping) or _json_digest(config_identity) != identity.get("config_digest"):
            raise DesktopOperationError("desktop_release_config_drift", "release configuration identity is invalid")
        if runtime_config_document is not None:
            expected_config = self._configuration_identity_for_document(
                path,
                runtime_config_document,
                bind_unconfigured=True,
                snapshot=snapshot,
            )
        else:
            from scripts.local_desktop.runtime_config import read_runtime_config_document, runtime_config_paths

            expected_config = self._configuration_identity_for_document(
                path,
                read_runtime_config_document(runtime_config_paths(self.config).document),
                bind_unconfigured=isinstance(manifest.get("desktop_runtime_configuration"), Mapping),
                snapshot=snapshot,
            )
        if dict(config_identity) != expected_config:
            raise DesktopOperationError("desktop_release_config_incompatible", "release was prepared for a different local desktop configuration")
        frontend_resources = {
            relative.removeprefix("frontend/"): value
            for relative, value in snapshot.resources.items()
            if relative.startswith("frontend/")
        }
        current_dependencies = self._dependency_identity(path / "frontend", verified_resources=frontend_resources)
        if _json_digest(current_dependencies) != identity.get("dependency_digest"):
            raise DesktopOperationError("desktop_release_dependency_drift", "current interpreter, Node runtime, or installed dependencies do not match the release")
        return manifest

    def configuration_identity_for_runtime(self, release_path: str | Path) -> dict[str, Any]:
        """Compute the current non-secret runtime configuration identity for a release."""
        from scripts.local_desktop.runtime_config import read_runtime_config_document, runtime_config_paths

        path = self.resolve_release(release_path)
        document = read_runtime_config_document(runtime_config_paths(self.config).document)
        manifest_path = path / "release-manifest.json"
        manifest = json.loads(_read_text_long_path(manifest_path))
        return self._configuration_identity_for_document(
            path,
            document,
            bind_unconfigured=isinstance(manifest.get("desktop_runtime_configuration"), Mapping),
        )

    def rebind_runtime_configuration(
        self,
        release: str | Path,
        *,
        runtime_config_document: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create a new immutable release bound to validated non-secret settings."""
        from scripts.local_desktop.runtime_config import models_config_payload, runtime_config_digest, validate_runtime_config_document

        source_path = self.resolve_release(release)
        source_manifest = self.verify(source_path)
        document = validate_runtime_config_document(runtime_config_document)
        models = models_config_payload(document)
        config_seed = runtime_config_digest(document)
        previous_runtime = source_manifest.get("desktop_runtime_configuration")
        source_id = str(
            previous_runtime.get("source_release_id")
            if isinstance(previous_runtime, Mapping)
            else source_manifest["identity"]["release_id"]
        )
        release_id = f"{source_id}-config-{config_seed[:12]}"
        destination = self.config.release_root / release_id
        if windows_extended_path(destination).exists():
            existing = self._verify(destination, runtime_config_document=document)
            return {"release_path": str(destination), "manifest": existing, "reused": True}
        staging_parent = self.config.release_root / ".staging"
        windows_extended_path(staging_parent).mkdir(parents=True, exist_ok=True)
        reject_reparse_path(staging_parent, root=self.config.release_root)
        stage = _make_temp_directory_long_path(staging_parent, prefix="config-rebind-")
        _remove_tree_long_path(stage)
        try:
            windows_extended_path(stage).mkdir(parents=True, exist_ok=False)
            for entry in source_manifest["resources"]:
                relative = _safe_relative(entry.get("path"))
                lexical_source = source_path / relative
                _reject_existing_reparse_chain(lexical_source)
                source = ensure_contained(source_path, lexical_source, field="source release resource")
                reject_reparse_path(source, root=source_path)
                target = stage / relative
                _copy_file_long_path(source, target)
            _copy_file_long_path(source_path / "release-manifest.json", stage / "release-manifest.json")
            bound_models = stage / ".desktop-app" / "config" / "models.json"
            if models is None:
                windows_extended_path(bound_models).unlink(missing_ok=True)
            else:
                _atomic_write_json_long_path(bound_models, models)
            manifest = json.loads(_read_text_long_path(stage / "release-manifest.json"))
            identity = dict(manifest["identity"])
            config_identity = self._configuration_identity_for_document(stage, document, bind_unconfigured=True)
            identity.update({"release_id": release_id, "config_digest": _json_digest(config_identity)})
            manifest.update(
                {
                    "created_at": utc_timestamp(),
                    "identity": identity,
                    "config": config_identity,
                    "resources": _resource_entries(stage),
                    "desktop_runtime_configuration": {
                        "schema": document["schema"],
                        "revision": document["revision"],
                        "digest": config_seed,
                        "source_release_id": source_id,
                    },
                }
            )
            _atomic_write_json_long_path(stage / "release-manifest.json", manifest)
            _replace_long_path(stage, destination)
            verified = self._verify(destination, runtime_config_document=document)
            return {"release_path": str(destination), "manifest": verified, "reused": False}
        except Exception:
            if windows_extended_path(stage).exists():
                _remove_tree_long_path(stage, ignore_errors=True)
            raise

    def _configuration_identity_for_document(
        self,
        release_path: Path,
        document: Mapping[str, Any],
        *,
        bind_unconfigured: bool = False,
        snapshot: SafeTreeSnapshot | None = None,
    ) -> dict[str, Any]:
        from scripts.local_desktop.runtime_config import models_config_payload, runtime_config_digest, validate_runtime_config_document

        validated = validate_runtime_config_document(document)
        base = {
            "schema": CONFIG_SCHEMA,
            "profile": self.config.deployment_profile,
            "canonical_origin": self.config.canonical_origin,
            "api_base_url": self.config.api_base_url,
            "parser_mode": self.config.parser_mode,
            "storage_mode": self.config.storage_mode,
            "data_mode": REAL_DATA_MODE,
        }
        models = models_config_payload(validated)
        if models is None and not bind_unconfigured:
            return {
                **base,
                "external_config_dependencies": _external_config_identity(
                    sanitized_environment(self.config),
                    project_root=self.config.project_root,
                    release_path=release_path,
                    verified_resources=snapshot.resources if snapshot is not None else None,
                ),
            }
        if models is None:
            environment = sanitized_environment(self.config)
            environment.pop("NEWS_MODELS_CONFIG", None)
            environment.pop("NEWS_RESEARCH_MODELS_CONFIG", None)
            dependencies = _external_config_identity(
                environment,
                project_root=self.config.project_root,
                release_path=release_path,
                verified_resources=snapshot.resources if snapshot is not None else None,
            )
            return {
                **base,
                "external_config_dependencies": dependencies,
                "runtime_configuration": {
                    "schema": validated["schema"],
                    "revision": validated["revision"],
                    "digest": runtime_config_digest(validated),
                    "research": {"llm": None, "source": validated["research"]["source"]},
                },
            }
        resource = Path(".desktop-app/config/models.json")
        expected_bytes = (json.dumps(models, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if snapshot is not None:
            actual_bytes = snapshot.captured.get(resource.as_posix())
            if actual_bytes is None:
                raise DesktopOperationError("desktop_runtime_models_missing", "bound runtime models configuration is missing")
        else:
            _reject_existing_reparse_chain(release_path / resource)
            bound_models = ensure_contained(release_path, release_path / resource, field="bound runtime models configuration")
            reject_reparse_path(bound_models, root=release_path)
            if not _is_file_long_path(bound_models):
                raise DesktopOperationError("desktop_runtime_models_missing", "bound runtime models configuration is missing")
            actual_bytes = read_bytes_long_path(bound_models)
        if actual_bytes != expected_bytes:
            raise DesktopOperationError("desktop_release_config_drift", "bound runtime models configuration does not match validated runtime settings")
        descriptor = {
            "mode": "bound",
            "resource": resource.as_posix(),
            "size": len(actual_bytes),
            "sha256": sha256(actual_bytes).hexdigest(),
        }
        public_llm = dict(validated["research"]["llm"])
        public_llm.pop("credential_ref", None)
        return {
            **base,
            "external_config_dependencies": [
                {"name": "NEWS_MODELS_CONFIG", **descriptor},
                {"name": "NEWS_RESEARCH_MODELS_CONFIG", **descriptor},
            ],
            "runtime_configuration": {
                "schema": validated["schema"],
                "revision": validated["revision"],
                "digest": runtime_config_digest(validated),
                "research": {"llm": public_llm, "source": validated["research"]["source"]},
            },
        }

    def bind_runtime_environment(self, release_path: Path, manifest: Mapping[str, Any], environment: Mapping[str, str]) -> dict[str, str]:
        config_identity = manifest.get("config")
        if not isinstance(config_identity, Mapping) or config_identity.get("data_mode") != REAL_DATA_MODE:
            raise DesktopOperationError(
                "desktop_release_data_mode_invalid",
                "release is not bound to the real local desktop data mode",
            )
        if config_identity.get("storage_mode") != self.config.storage_mode:
            raise DesktopOperationError(
                "desktop_release_storage_mode_invalid",
                "release is not bound to the selected local desktop storage mode",
            )
        try:
            result = sanitized_environment(self.config, base=environment)
        except LocalDesktopConfigError as exc:
            raise DesktopOperationError(exc.reason_code, str(exc)) from exc
        dependencies = config_identity.get("external_config_dependencies") if isinstance(config_identity, Mapping) else None
        if not isinstance(dependencies, list):
            raise DesktopOperationError("desktop_release_config_drift", "release runtime configuration dependencies are invalid")
        for dependency in dependencies:
            if not isinstance(dependency, Mapping):
                raise DesktopOperationError("desktop_release_config_drift", "release runtime configuration dependency is invalid")
            name = str(dependency.get("name") or "")
            mode = str(dependency.get("mode") or "")
            if name not in _EXTERNAL_CONFIG_ENV_NAMES:
                raise DesktopOperationError("desktop_release_config_drift", "release runtime configuration dependency name is invalid")
            if mode in {"packaged", "bound"}:
                lexical_resource = release_path / str(dependency.get("resource") or "")
                _reject_existing_reparse_chain(lexical_resource)
                resource = ensure_contained(release_path, lexical_resource, field="release configuration resource")
                reject_reparse_path(resource, root=release_path)
                if not _is_file_long_path(resource):
                    raise DesktopOperationError("desktop_release_config_drift", "packaged runtime configuration resource is missing")
                if mode == "bound":
                    data = read_bytes_long_path(resource)
                    if dependency.get("size") != len(data) or dependency.get("sha256") != sha256(data).hexdigest():
                        raise DesktopOperationError("desktop_release_config_drift", "bound runtime configuration resource has drifted")
                result[name] = str(resource)
            elif mode == "external":
                result[name] = str(dependency.get("path") or "")
            else:
                raise DesktopOperationError("desktop_release_config_drift", "release runtime configuration dependency mode is invalid")
        if isinstance(config_identity.get("runtime_configuration"), Mapping):
            bound_models_path = result.get("NEWS_RESEARCH_MODELS_CONFIG")
            from scripts.local_desktop.runtime_config import (
                FIXED_CREDENTIAL_ENV,
                read_runtime_config_document,
                runtime_config_paths,
                runtime_environment_overlay,
            )

            for name in (
                "NEWS_MODELS_CONFIG",
                "NEWS_RESEARCH_MODELS_CONFIG",
                "NEWS_RESEARCH_LLM_ROUTE_ID",
                "NEWS_RESEARCH_LLM_PROVIDER",
                "NEWS_RESEARCH_LLM_MODEL",
                "NEWS_RESEARCH_LLM_BASE_URL",
                "NEWS_RESEARCH_LLM_API_KEY_ENV",
                FIXED_CREDENTIAL_ENV,
            ):
                result.pop(name, None)
            document = read_runtime_config_document(runtime_config_paths(self.config).document)
            models_path = Path(bound_models_path) if document["research"]["llm"] is not None and bound_models_path else None
            result.update(runtime_environment_overlay(document, models_path=models_path))
        return result

    def activate(self, release: str | Path) -> dict[str, Any]:
        _manifest, pointer = self.verify_and_activate(release)
        return pointer

    def verify_and_activate(self, release: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
        """Verify once and publish the active pointer from that operation's result."""
        path = self.resolve_release(release)
        manifest = self.verify(path)
        pointer = self._write_active_pointer(path, manifest)
        return manifest, pointer

    def _write_active_pointer(self, path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
        pointer = {
            "schema": ACTIVE_RELEASE_SCHEMA,
            "activated_at": utc_timestamp(),
            "release_id": manifest["identity"]["release_id"],
            "release_path": str(path),
            "identity": manifest["identity"],
        }
        _atomic_write_json_long_path(self.active_pointer, pointer)
        return pointer

    def active(self) -> tuple[Path, dict[str, Any]]:
        path, manifest, _pointer = self.active_with_pointer()
        return path, manifest

    def active_with_pointer(self) -> tuple[Path, dict[str, Any], dict[str, Any]]:
        if not _is_file_long_path(self.active_pointer):
            raise DesktopOperationError("desktop_release_not_activated", "no production release is active; run build first")
        try:
            pointer = json.loads(_read_text_long_path(self.active_pointer))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopOperationError("desktop_release_pointer_invalid", "active release pointer is invalid") from exc
        if pointer.get("schema") != ACTIVE_RELEASE_SCHEMA:
            raise DesktopOperationError("desktop_release_pointer_invalid", "active release pointer schema is unsupported")
        path = self.resolve_release(pointer.get("release_path", ""))
        manifest = self.verify(path)
        if pointer.get("release_id") != manifest.get("identity", {}).get("release_id"):
            raise DesktopOperationError("desktop_release_pointer_mismatch", "active release pointer does not match its manifest")
        return path, manifest, pointer

    def resolve_release(self, release: str | Path) -> Path:
        raw = Path(release)
        path = raw if raw.is_absolute() else self.config.release_root / raw
        reject_reparse_path(path, root=self.config.release_root)
        resolved = path.resolve(strict=False)
        root = self.config.release_root.resolve(strict=False)
        if not resolved.is_relative_to(root) or resolved == root or ".staging" in resolved.parts:
            raise DesktopOperationError("desktop_release_path_unsafe", "release must be an owned child of the configured release root")
        return resolved

    def _require_build_inputs(self) -> None:
        if not _is_file_long_path(self.config.python_executable):
            raise DesktopOperationError("desktop_project_python_missing", "project .venv interpreter is missing")
        if os.path.normcase(str(Path(sys.executable).resolve(strict=False))) != os.path.normcase(str(self.config.python_executable.resolve(strict=False))):
            raise DesktopOperationError("desktop_wrong_python", "production release must be built with the project .venv interpreter")
        for path in (self.config.frontend_root / "package.json", self.config.frontend_root / "package-lock.json"):
            if not _is_file_long_path(path):
                raise DesktopOperationError("desktop_frontend_lock_missing", f"required frontend build input is missing: {path.name}")

    def dependency_identity(
        self,
        frontend_root: Path | None = None,
        *,
        verified_resources: Mapping[str, tuple[int, str]] | None = None,
    ) -> dict[str, Any]:
        """Return the dependency identity of the interpreter and tools running this manager."""
        dependency_root = frontend_root or self.config.frontend_root
        frontend_identity = _production_frontend_identity(dependency_root, verified_resources=verified_resources)
        lockfile = dependency_root / "package-lock.json"
        installed_lock = dependency_root / "node_modules" / ".package-lock.json"
        python_packages = sorted(
            [distribution.metadata.get("Name", ""), distribution.version]
            for distribution in importlib.metadata.distributions()
            if distribution.metadata.get("Name")
        )
        node_executable = shutil.which("node")
        npm_executable = shutil.which("npm.cmd") or shutil.which("npm")
        return {
            "python": {
                "executable": str(Path(sys.executable).resolve(strict=False)),
                "executable_sha256": _file_sha256(Path(sys.executable)),
                "version": sys.version.split()[0],
                "packages": python_packages,
                **({"runtime_profile": provenance} if (provenance := _python_runtime_provenance(Path(sys.executable))) is not None else {}),
            },
            "node": {
                "executable": str(Path(node_executable).resolve(strict=False)) if node_executable else None,
                "executable_sha256": _file_sha256(Path(node_executable)) if node_executable else None,
                "version": self._tool_version([node_executable, "--version"]) if node_executable else None,
                "npm_executable": str(Path(npm_executable).resolve(strict=False)) if npm_executable else None,
                "npm_executable_sha256": _file_sha256(Path(npm_executable)) if npm_executable else None,
                "npm_version": self._tool_version([npm_executable, "--version"]) if npm_executable else None,
                "package_lock_sha256": (
                    frontend_identity["package_lock_sha256"]
                    if frontend_identity is not None
                    else _optional_verified_sha256(lockfile, relative="package-lock.json", verified_resources=verified_resources)
                ),
                "installed_lock_sha256": (
                    None
                    if frontend_identity is not None
                    else _optional_verified_sha256(installed_lock, relative="node_modules/.package-lock.json", verified_resources=verified_resources)
                ),
                "installed_tree": (
                    frontend_identity["installed_tree"]
                    if frontend_identity is not None
                    else self._node_dependency_tree_identity(dependency_root, npm_executable) if npm_executable else None
                ),
                "runtime_mode": frontend_identity["runtime_mode"] if frontend_identity is not None else "next-start",
            },
        }

    def dependency_identity_for_runtime(
        self,
        frontend_root: str | Path,
        *,
        python_executable: str | Path,
        node_executable: str | Path,
        npm_executable: str | Path,
        application_root: str | Path,
        required_python_imports: Iterable[str] = (),
        required_python_attributes: Iterable[tuple[str, str]] = (),
    ) -> dict[str, Any]:
        """Probe an explicit local runtime without accepting caller-supplied identity values.

        This is the production extension used by portable bundle staging. The manager
        remains the sole owner of executable hashes, Python distribution metadata,
        lock hashes, and the installed npm tree identity.
        """
        dependency_root = self._required_runtime_directory(frontend_root, "frontend runtime root")
        application = self._required_runtime_directory(application_root, "application runtime root")
        python = self._required_runtime_file(python_executable, "Python executable")
        node = self._required_runtime_file(node_executable, "Node executable")
        npm = self._required_runtime_file(npm_executable, "npm executable")
        environment = dict(os.environ)
        environment.pop("PYTHONHOME", None)
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        imports = tuple(dict.fromkeys(str(item) for item in required_python_imports if str(item)))
        attributes = tuple((str(module), str(attribute)) for module, attribute in required_python_attributes)
        script = (
            "import importlib,importlib.metadata,json,sys;"
            f"sys.path.insert(0,{str(application)!r});"
            f"mods={list(imports)!r};attrs={list(attributes)!r};"
            "[importlib.import_module(name) for name in mods];"
            "[getattr(importlib.import_module(module),attribute) for module,attribute in attrs];"
            "packages=sorted([[d.metadata.get('Name',''),d.version] for d in importlib.metadata.distributions() if d.metadata.get('Name')]);"
            "print(json.dumps({'version':sys.version.split()[0],'packages':packages},sort_keys=True,separators=(',',':')))"
        )
        try:
            result = self.command_runner(
                [str(python), "-I", "-c", script],
                capture_output=True,
                text=False,
                timeout=60,
                check=False,
                shell=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise DesktopOperationError("desktop_python_dependency_probe_timeout", "Python dependency identity probe exceeded its deadline") from exc
        except OSError as exc:
            raise DesktopOperationError("desktop_python_dependency_probe_unavailable", "Python dependency identity could not be inspected") from exc
        stdout, _stderr = self._decode_command_output(result, [str(python), "-I", "-c", "<dependency-probe>"])
        if result.returncode != 0:
            raise DesktopOperationError(
                "desktop_python_dependency_probe_failed",
                "Python dependency identity probe failed",
                details={"returncode": result.returncode},
            )
        try:
            python_identity = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DesktopOperationError("desktop_python_dependency_probe_invalid", "Python dependency identity is not valid JSON") from exc
        if not isinstance(python_identity, Mapping) or not str(python_identity.get("version") or ""):
            raise DesktopOperationError("desktop_python_dependency_probe_invalid", "Python dependency identity is incomplete")
        packages = python_identity.get("packages")
        if not isinstance(packages, list) or any(
            not isinstance(item, list)
            or len(item) != 2
            or not all(isinstance(value, str) and value for value in item)
            for item in packages
        ):
            raise DesktopOperationError("desktop_python_dependency_probe_invalid", "Python package identity is incomplete")
        frontend_identity = _production_frontend_identity(dependency_root)
        lockfile = dependency_root / "package-lock.json"
        installed_lock = dependency_root / "node_modules" / ".package-lock.json"
        return {
            "python": {
                "executable": str(python),
                "executable_sha256": _file_sha256(python),
                "version": python_identity["version"],
                "packages": packages,
                **({"runtime_profile": provenance} if (provenance := _python_runtime_provenance(python)) is not None else {}),
            },
            "node": {
                "executable": str(node),
                "executable_sha256": _file_sha256(node),
                "version": self._required_tool_version([str(node), "--version"], environment=environment),
                "npm_executable": str(npm),
                "npm_executable_sha256": _file_sha256(npm),
                "npm_version": self._required_tool_version([str(npm), "--version"], environment=environment),
                "package_lock_sha256": frontend_identity["package_lock_sha256"] if frontend_identity is not None else _optional_file_sha256(lockfile),
                "installed_lock_sha256": None if frontend_identity is not None else _optional_file_sha256(installed_lock),
                "installed_tree": (
                    frontend_identity["installed_tree"]
                    if frontend_identity is not None
                    else self._node_dependency_tree_identity(dependency_root, str(npm), environment=environment)
                ),
                "runtime_mode": frontend_identity["runtime_mode"] if frontend_identity is not None else "next-start",
            },
        }

    def _dependency_identity(
        self,
        frontend_root: Path | None = None,
        *,
        verified_resources: Mapping[str, tuple[int, str]] | None = None,
    ) -> dict[str, Any]:
        if verified_resources is None:
            return self.dependency_identity(frontend_root)
        return self.dependency_identity(frontend_root, verified_resources=verified_resources)

    def _node_dependency_tree_identity(
        self,
        frontend_root: Path,
        npm_executable: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            result = self.command_runner(
                [npm_executable, "ls", "--json", "--all"],
                cwd=frontend_root,
                capture_output=True,
                text=False,
                timeout=120,
                check=False,
                shell=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DesktopOperationError(
                "desktop_node_dependency_tree_unavailable",
                "installed Node dependency tree could not be inspected",
            ) from exc
        stdout, _stderr = self._decode_command_output(result, [npm_executable, "ls", "--json", "--all"])
        try:
            payload = json.loads(stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise DesktopOperationError(
                "desktop_node_dependency_tree_invalid",
                "installed Node dependency tree is not valid JSON",
            ) from exc
        if not isinstance(payload, Mapping):
            raise DesktopOperationError(
                "desktop_node_dependency_tree_invalid",
                "installed Node dependency tree is invalid",
            )
        problems = payload.get("problems")
        problem_count = len(problems) if isinstance(problems, list) else 0
        if result.returncode != 0 or problem_count:
            raise DesktopOperationError(
                "desktop_node_dependency_tree_invalid",
                "installed Node dependency tree contains missing, invalid, or extraneous packages",
                details={
                    "returncode": result.returncode,
                    "problem_count": problem_count,
                },
            )
        normalized, package_count = _normalized_node_dependency_tree(payload)
        return {
            "package_count": package_count,
            "sha256": _json_digest(normalized),
        }

    def _required_runtime_file(self, value: str | Path, label: str) -> Path:
        path = Path(value).expanduser().absolute()
        _reject_existing_reparse_chain(path)
        reject_reparse_path(path, root=path.parent)
        if not _is_file_long_path(path):
            raise DesktopOperationError("desktop_runtime_dependency_missing", f"{label} is missing")
        return path.resolve(strict=True)

    def _required_runtime_directory(self, value: str | Path, label: str) -> Path:
        path = Path(value).expanduser().absolute()
        _reject_existing_reparse_chain(path)
        reject_reparse_path(path, root=path)
        if not windows_extended_path(path).is_dir():
            raise DesktopOperationError("desktop_runtime_dependency_missing", f"{label} is missing")
        return path.resolve(strict=True)

    def _required_tool_version(self, argv: list[str], *, environment: Mapping[str, str]) -> str:
        try:
            result = self.command_runner(
                argv,
                capture_output=True,
                text=False,
                timeout=15,
                check=False,
                shell=False,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise DesktopOperationError("desktop_runtime_dependency_probe_timeout", f"runtime dependency probe exceeded its deadline: {Path(argv[0]).name}") from exc
        except OSError as exc:
            raise DesktopOperationError("desktop_runtime_dependency_probe_unavailable", f"runtime dependency could not be inspected: {Path(argv[0]).name}") from exc
        stdout, _stderr = self._decode_command_output(result, argv)
        version = stdout.strip()
        if result.returncode != 0 or not version:
            raise DesktopOperationError(
                "desktop_runtime_dependency_probe_failed",
                f"runtime dependency probe failed: {Path(argv[0]).name}",
                details={"returncode": result.returncode},
            )
        return version

    def _tool_version(self, argv: list[str]) -> str | None:
        try:
            result = self.command_runner(argv, capture_output=True, text=False, timeout=10, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        stdout, _stderr = self._decode_command_output(result, argv)
        return stdout.strip() if result.returncode == 0 else None

    def _git_revision(self) -> str:
        try:
            argv = ["git", "rev-parse", "HEAD"]
            result = self.command_runner(argv, cwd=self.config.project_root, capture_output=True, text=False, timeout=10, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        stdout, _stderr = self._decode_command_output(result, argv)
        return stdout.strip() if result.returncode == 0 and stdout.strip() else "unavailable"

    def _run(self, argv: list[str], *, cwd: Path, env: Mapping[str, str], timeout: float) -> None:
        try:
            result = self.command_runner(argv, cwd=cwd, env=dict(env), capture_output=True, text=False, timeout=timeout, check=False, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise DesktopOperationError("desktop_build_timeout", f"build command exceeded its deadline: {Path(argv[0]).name}") from exc
        except OSError as exc:
            raise DesktopOperationError("desktop_build_command_failed", f"could not start build command: {Path(argv[0]).name}") from exc
        self._decode_command_output(result, argv)
        if result.returncode != 0:
            raise DesktopOperationError("desktop_build_failed", f"production build command failed: {Path(argv[0]).name}", details={"returncode": result.returncode})

    @staticmethod
    def _decode_command_output(
        result: subprocess.CompletedProcess[Any],
        argv: list[str],
    ) -> tuple[str, str]:
        command = Path(argv[0]).name
        decoded: list[str] = []
        for stream, value in (("stdout", result.stdout), ("stderr", result.stderr)):
            try:
                decoded.append(_decode_utf8_stream(value))
            except (TypeError, UnicodeDecodeError) as exc:
                raise DesktopOperationError(
                    "desktop_command_output_encoding_invalid",
                    f"command output is not valid UTF-8: {command}",
                    details={"command": command, "stream": stream},
                ) from exc
        return decoded[0], decoded[1]

    @staticmethod
    def _copy_tree(
        source: Path,
        destination: Path,
        *,
        ignore: Callable[[str, list[str]], set[str]] | None = None,
    ) -> None:
        lexical_source = source.expanduser().absolute()
        reject_reparse_path(lexical_source, root=lexical_source.parent)
        source = lexical_source.resolve(strict=True)
        reject_reparse_path(source, root=source)
        _require_directory_long_path(source)
        source_io = windows_extended_path(source)
        destination_io = windows_extended_path(destination)
        if destination_io.exists():
            raise FileExistsError(destination)
        destination_io.mkdir(parents=True, exist_ok=False)
        copied_directories: list[tuple[Path, Path]] = [(source, destination)]
        ignore_callback = ignore or _copy_ignore
        try:
            for directory, names, filenames in os.walk(
                source_io,
                topdown=True,
                onerror=_raise_release_walk_error,
                followlinks=False,
            ):
                names.sort()
                filenames.sort()
                lexical_directory = source / Path(directory).relative_to(source_io)
                reject_reparse_path(lexical_directory, root=source)
                ignored = set(ignore_callback(os.fspath(lexical_directory), [*names, *filenames]))
                for name in list(names):
                    lexical = lexical_directory / name
                    reject_reparse_path(lexical, root=source)
                    _require_directory_long_path(lexical)
                    if name in ignored:
                        names.remove(name)
                        continue
                    target = destination / lexical.relative_to(source)
                    windows_extended_path(target).mkdir(exist_ok=False)
                    copied_directories.append((lexical, target))
                for name in filenames:
                    lexical = lexical_directory / name
                    reject_reparse_path(lexical, root=source)
                    _require_regular_file_long_path(lexical)
                    if name in ignored:
                        continue
                    _copy_file_long_path(lexical, destination / lexical.relative_to(source))
            for source_directory, target_directory in reversed(copied_directories):
                shutil.copystat(
                    windows_extended_path(source_directory),
                    windows_extended_path(target_directory),
                    follow_symlinks=False,
                )
        except Exception:
            _remove_tree_long_path(destination, ignore_errors=True)
            raise



def _materialize_next_standalone(
    frontend_root: Path,
    *,
    source_digest: str,
    source_installed_tree: Mapping[str, Any],
) -> None:
    standalone = frontend_root / ".next" / "standalone"
    public = frontend_root / "public"
    static_assets = frontend_root / ".next" / "static"
    source_lock = frontend_root / "package-lock.json"
    for required in (standalone / "server.js", public, static_assets, source_lock):
        if not windows_extended_path(required).exists():
            raise DesktopOperationError(
                "desktop_frontend_standalone_incomplete",
                f"Next standalone build resource is missing: {required.relative_to(frontend_root).as_posix()}",
            )
    source_lock_sha256 = _file_sha256(source_lock)
    if (
        not isinstance(source_installed_tree.get("package_count"), int)
        or int(source_installed_tree["package_count"]) < 1
        or not _valid_sha256(source_installed_tree.get("sha256"))
        or source_installed_tree.get("package_lock_sha256") != source_lock_sha256
    ):
        raise DesktopOperationError(
            "desktop_frontend_source_lock_drift",
            "frontend source lock or strict installed dependency tree changed before standalone materialization",
        )
    runtime = _make_temp_directory_long_path(frontend_root.parent, prefix="frontend-standalone-")
    try:
        materialized_links = [
            *_materialize_traced_tree(standalone, runtime, allowed_root=frontend_root),
            *_materialize_traced_tree(public, runtime / "public", allowed_root=frontend_root),
            *_materialize_traced_tree(static_assets, runtime / ".next" / "static", allowed_root=frontend_root),
        ]
        materialized_links.sort(key=lambda item: item["source"])
        if not _is_file_long_path(runtime / "server.js"):
            raise DesktopOperationError("desktop_frontend_standalone_incomplete", "Next standalone server.js is missing after materialization")
        packages = _standalone_package_inventory(runtime, source_lock=source_lock)
        resources = _resource_entries(runtime)
        closure = {
            "schema": _FRONTEND_PRODUCTION_SCHEMA,
            "runtime_mode": "next-standalone",
            "entrypoint": "server.js",
            "source_digest": source_digest,
            "package_lock_sha256": source_lock_sha256,
            "source_installed_tree": dict(source_installed_tree),
            "packages": packages,
            "packages_sha256": _json_digest({"packages": packages}),
            "resources": resources,
            "resources_sha256": _json_digest({"resources": resources}),
            "materialized_links": materialized_links,
            "materialized_links_sha256": _json_digest({"materialized_links": materialized_links}),
        }
        _atomic_write_json_long_path(runtime / _FRONTEND_PRODUCTION_CLOSURE, closure)
        _remove_tree_long_path(frontend_root)
        _replace_long_path(runtime, frontend_root)
    except Exception:
        if windows_extended_path(runtime).exists():
            _remove_tree_long_path(runtime, ignore_errors=True)
        raise


def _materialize_traced_tree(source: Path, destination: Path, *, allowed_root: Path) -> list[dict[str, str]]:
    # Resolve and enumerate Win32 extended-length paths consistently. Mixing a
    # plain MAX_PATH-limited resolve() with long-path lstat/copy makes otherwise
    # valid traced resources appear missing before materialization.
    source = windows_extended_path(source).absolute()
    destination = windows_extended_path(destination).absolute()
    allowed = windows_extended_path(allowed_root).resolve(strict=True)
    evidence: list[dict[str, str]] = []
    symlink_tag = 0xA000000C
    mount_point_tag = 0xA0000003
    accepted_tags = {symlink_tag: "symlink", mount_point_tag: "mount-point"}

    def copy_entry(current_source: Path, current_destination: Path, ancestors: tuple[Path, ...]) -> None:
        try:
            metadata = windows_extended_path(current_source).lstat()
        except OSError as exc:
            raise DesktopOperationError("desktop_frontend_standalone_materialization_failed", "Next standalone source could not be inspected") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_attribute = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        is_symbolic = stat.S_ISLNK(metadata.st_mode)
        is_reparse = reparse_attribute or is_symbolic
        if is_reparse:
            tag = getattr(metadata, "st_reparse_tag", None)
            if tag not in accepted_tags:
                raise DesktopOperationError(
                    "desktop_frontend_standalone_reparse_tag_unsupported",
                    "Next standalone contains an unknown or unsupported reparse point",
                    details={"source": current_source.absolute().relative_to(allowed).as_posix(), "tag": tag},
                )
            try:
                resolved = current_source.resolve(strict=True)
            except OSError as exc:
                raise DesktopOperationError("desktop_frontend_standalone_link_broken", "Next standalone link target is unavailable") from exc
            if not resolved.is_relative_to(allowed):
                raise DesktopOperationError(
                    "desktop_frontend_standalone_link_outside_root",
                    "Next standalone link target is outside the controlled frontend build root",
                )
            evidence.append(
                {
                    "kind": accepted_tags[tag],
                    "tag": f"0x{tag:08x}",
                    "source": current_source.absolute().relative_to(allowed).as_posix(),
                    "target": resolved.relative_to(allowed).as_posix(),
                }
            )
        else:
            resolved = current_source
        resolved_key = resolved.resolve(strict=True)
        if resolved_key in ancestors:
            raise DesktopOperationError("desktop_frontend_standalone_link_cycle", "Next standalone contains a link cycle")
        if resolved.is_dir():
            windows_extended_path(current_destination).mkdir(parents=True, exist_ok=True)
            try:
                children = sorted(os.scandir(windows_extended_path(resolved)), key=lambda item: item.name.casefold())
            except OSError as exc:
                raise DesktopOperationError("desktop_frontend_standalone_materialization_failed", "Next standalone directory could not be enumerated") from exc
            for child in children:
                copy_entry(resolved / child.name, current_destination / child.name, (*ancestors, resolved_key))
            return
        if not resolved.is_file():
            raise DesktopOperationError("desktop_frontend_standalone_type_invalid", "Next standalone contains a non-file runtime resource")
        windows_extended_path(current_destination.parent).mkdir(parents=True, exist_ok=True)
        _copy_file_long_path(resolved, current_destination)

    copy_entry(source, destination, ())
    return evidence

def _standalone_package_inventory(root: Path, *, source_lock: Path) -> list[dict[str, Any]]:
    root = windows_extended_path(root)
    try:
        lock = json.loads(_read_text_long_path(source_lock))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError("desktop_frontend_source_lock_invalid", "frontend package lock is invalid") from exc
    lock_packages = lock.get("packages") if isinstance(lock, Mapping) else None
    if not isinstance(lock_packages, Mapping):
        raise DesktopOperationError("desktop_frontend_source_lock_invalid", "frontend package lock package map is missing")
    packages: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for package_json in sorted(root.rglob("package.json")):
        relative = package_json.relative_to(root).as_posix()
        if "node_modules/" not in relative and not relative.startswith("node_modules/"):
            continue
        _reject_existing_reparse_chain(package_json)
        try:
            payload = json.loads(_read_text_long_path(package_json))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopOperationError("desktop_frontend_standalone_package_invalid", f"invalid package metadata: {relative}") from exc
        if not isinstance(payload, Mapping):
            raise DesktopOperationError("desktop_frontend_standalone_package_invalid", f"invalid package metadata: {relative}")
        if not str(payload.get("name") or "") or not str(payload.get("version") or ""):
            # Node subpath export descriptors (for example @swc/helpers/_/*) are
            # package.json resources, not independently installed distributions.
            # Accept them only beneath a materialized, source-locked package owner.
            owner = package_json.parent.parent
            owned_descriptor = False
            while owner.is_relative_to(root) and owner != root:
                owner_relative = owner.relative_to(root).as_posix()
                locked_owner = lock_packages.get(owner_relative)
                if isinstance(locked_owner, Mapping) and locked_owner.get("version"):
                    owner_metadata_path = owner / "package.json"
                    if _is_file_long_path(owner_metadata_path):
                        owner_metadata = json.loads(_read_text_long_path(owner_metadata_path))
                        owned_descriptor = (
                            isinstance(owner_metadata, Mapping)
                            and bool(owner_metadata.get("name"))
                            and str(owner_metadata.get("version") or "") == str(locked_owner["version"])
                            and str(locked_owner.get("name") or owner_metadata["name"]) == owner_metadata["name"]
                        )
                    break
                owner = owner.parent
            if owned_descriptor:
                continue
            raise DesktopOperationError("desktop_frontend_standalone_package_invalid", f"unowned subpath metadata: {relative}")
        package_root = package_json.parent.relative_to(root).as_posix()
        if package_root in seen_paths:
            raise DesktopOperationError("desktop_frontend_standalone_package_invalid", "duplicate standalone package path")
        seen_paths.add(package_root)
        name = str(payload["name"])
        version = str(payload["version"])
        locked = lock_packages.get(package_root)
        if isinstance(locked, Mapping) and str(locked.get("version") or "") == version:
            locked_name = str(locked.get("name") or name)
            if locked_name != name:
                raise DesktopOperationError("desktop_frontend_standalone_package_unlocked", f"traced package name differs from source lock: {package_root}")
            association: dict[str, str] = {
                "kind": "source-lock",
                "path": package_root,
                "name": locked_name,
                "version": version,
            }
        elif package_root.startswith("node_modules/next/dist/compiled/"):
            next_locked = lock_packages.get("node_modules/next")
            next_version = str(next_locked.get("version") or "") if isinstance(next_locked, Mapping) else ""
            if not next_version:
                raise DesktopOperationError("desktop_frontend_standalone_package_unlocked", "Next vendored package has no locked Next owner")
            next_name = str(next_locked.get("name") or "next")
            if next_name != "next":
                raise DesktopOperationError("desktop_frontend_standalone_package_unlocked", "Next vendored package owner differs from source lock")
            association = {
                "kind": "next-vendored",
                "owner_path": "node_modules/next",
                "owner_name": next_name,
                "owner_version": next_version,
            }
        else:
            raise DesktopOperationError("desktop_frontend_standalone_package_unlocked", f"traced package is not owned by the source lock: {package_root}")
        packages.append({"path": package_root, "name": name, "version": version, "lock_association": association})
    if not packages or not any(item["name"] == "next" for item in packages):
        raise DesktopOperationError("desktop_frontend_standalone_incomplete", "Next standalone production package closure is incomplete")
    return sorted(packages, key=lambda item: item["path"])

def _production_frontend_identity(
    root: Path,
    *,
    verified_resources: Mapping[str, tuple[int, str]] | None = None,
) -> dict[str, Any] | None:
    closure_path = root / _FRONTEND_PRODUCTION_CLOSURE
    if not _is_file_long_path(closure_path):
        return None
    try:
        closure = json.loads(_read_text_long_path(closure_path))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend production closure is invalid JSON") from exc
    if not isinstance(closure, Mapping) or closure.get("schema") != _FRONTEND_PRODUCTION_SCHEMA or closure.get("runtime_mode") != "next-standalone":
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend production closure schema is unsupported")
    resources = closure.get("resources")
    packages = closure.get("packages")
    materialized_links = closure.get("materialized_links")
    if not isinstance(resources, list) or not isinstance(packages, list) or not isinstance(materialized_links, list):
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend production closure is incomplete")
    normalized_links: list[dict[str, str]] = []
    for item in materialized_links:
        if not isinstance(item, Mapping) or item.get("kind") not in {"symlink", "mount-point"}:
            raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend materialized link evidence is invalid")
        source = _safe_relative(item.get("source"))
        target = _safe_relative(item.get("target"))
        tag = str(item.get("tag") or "")
        expected_tag = "0xa000000c" if item.get("kind") == "symlink" else "0xa0000003"
        if tag != expected_tag:
            raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend materialized link tag evidence is invalid")
        normalized_links.append({"kind": str(item["kind"]), "tag": tag, "source": source, "target": target})
    normalized_links.sort(key=lambda item: item["source"])
    if normalized_links != materialized_links or closure.get("materialized_links_sha256") != _json_digest({"materialized_links": materialized_links}):
        raise DesktopOperationError("desktop_frontend_production_closure_drift", "frontend materialized link evidence changed")
    expected_resources: dict[str, tuple[int, str]] = {}
    for item in resources:
        if not isinstance(item, Mapping):
            raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend production resource entry is invalid")
        relative = _safe_relative(item.get("path"))
        expected_resources[relative] = (item.get("size"), item.get("sha256"))
    actual_resources = (
        {relative: identity for relative, identity in verified_resources.items() if relative != _FRONTEND_PRODUCTION_CLOSURE}
        if verified_resources is not None
        else _scan_release_tree(root, excluded={_FRONTEND_PRODUCTION_CLOSURE}).resources
    )
    if actual_resources != expected_resources or closure.get("resources_sha256") != _json_digest({"resources": resources}):
        raise DesktopOperationError("desktop_frontend_production_closure_drift", "frontend standalone resources do not match the production closure")
    source_installed_tree = closure.get("source_installed_tree")
    if (
        not isinstance(source_installed_tree, Mapping)
        or not isinstance(source_installed_tree.get("package_count"), int)
        or int(source_installed_tree["package_count"]) < 1
        or not _valid_sha256(source_installed_tree.get("sha256"))
        or source_installed_tree.get("package_lock_sha256") != closure.get("package_lock_sha256")
    ):
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend source npm tree identity is invalid")
    normalized_packages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in packages:
        if not isinstance(item, Mapping):
            raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend production package entry is invalid")
        path = _safe_relative(item.get("path"))
        name = str(item.get("name") or "")
        version = str(item.get("version") or "")
        association = item.get("lock_association")
        if not name or not version or path in seen or not isinstance(association, Mapping):
            raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend production package entry is incomplete")
        kind = association.get("kind")
        if kind == "source-lock":
            normalized_association: dict[str, str] = {
                "kind": "source-lock",
                "path": _safe_relative(association.get("path")),
                "name": str(association.get("name") or ""),
                "version": str(association.get("version") or ""),
            }
            if (
                normalized_association["path"] != path
                or normalized_association["name"] != name
                or normalized_association["version"] != version
            ):
                raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend package source-lock association is invalid")
        elif kind == "next-vendored":
            owner_path = _safe_relative(association.get("owner_path"))
            owner_name = str(association.get("owner_name") or "")
            owner_version = str(association.get("owner_version") or "")
            if (
                owner_path != "node_modules/next"
                or owner_name != "next"
                or not owner_version
                or not path.startswith("node_modules/next/dist/compiled/")
            ):
                raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend Next vendored package association is invalid")
            normalized_association = {
                "kind": "next-vendored",
                "owner_path": owner_path,
                "owner_name": owner_name,
                "owner_version": owner_version,
            }
        else:
            raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend package lock association kind is unsupported")
        seen.add(path)
        normalized_packages.append({"path": path, "name": name, "version": version, "lock_association": normalized_association})
    normalized_packages.sort(key=lambda item: item["path"])
    next_package = next((item for item in normalized_packages if item["path"] == "node_modules/next"), None)
    if next_package is None or next_package["name"] != "next":
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend Next owner package is missing from the production closure")
    if any(
        item["lock_association"]["kind"] == "next-vendored"
        and item["lock_association"]["owner_version"] != next_package["version"]
        for item in normalized_packages
    ):
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend Next vendored ownership version does not match the locked Next package")
    if normalized_packages != packages or closure.get("packages_sha256") != _json_digest({"packages": packages}):
        raise DesktopOperationError("desktop_frontend_production_closure_drift", "frontend standalone package closure changed")
    package_lock_sha256 = str(closure.get("package_lock_sha256") or "")
    if not _valid_sha256(package_lock_sha256):
        raise DesktopOperationError("desktop_frontend_production_closure_invalid", "frontend source lock identity is invalid")
    return {
        "runtime_mode": "next-standalone",
        "package_lock_sha256": package_lock_sha256,
        "installed_tree": {
            "package_count": len(packages),
            "sha256": str(closure["packages_sha256"]),
            "source_npm_tree": dict(source_installed_tree),
        },
    }


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)

def _reject_existing_reparse_chain(path: Path) -> None:
    lexical = path.expanduser().absolute()
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    for probe in (lexical, *lexical.parents):
        try:
            metadata = windows_extended_path(probe).lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DesktopOperationError("desktop_path_inspection_failed", "path could not be inspected safely") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or (os.name == "nt" and attributes & reparse):
            raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {probe.name}")


def _decode_utf8_stream(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    raise TypeError("captured command output must be bytes or text")


def _normalized_node_dependency_tree(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], int]:
    count = 0

    def visit(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal count
        for flag in ("extraneous", "invalid", "missing"):
            if raw.get(flag):
                raise DesktopOperationError(
                    "desktop_node_dependency_tree_invalid",
                    "installed Node dependency tree contains missing, invalid, or extraneous packages",
                )
        count += 1
        dependencies = raw.get("dependencies")
        if dependencies is None:
            dependencies = {}
        if not isinstance(dependencies, Mapping):
            raise DesktopOperationError(
                "desktop_node_dependency_tree_invalid",
                "installed Node dependency tree contains an invalid dependency entry",
            )
        normalized_dependencies: dict[str, Any] = {}
        for dependency_name in sorted(str(item) for item in dependencies):
            dependency = dependencies.get(dependency_name)
            if not isinstance(dependency, Mapping):
                raise DesktopOperationError(
                    "desktop_node_dependency_tree_invalid",
                    "installed Node dependency tree contains an invalid package",
                )
            normalized_dependencies[dependency_name] = visit(
                dependency_name,
                dependency,
            )
        result = {
            "name": name,
            "version": str(raw.get("version") or ""),
            "dependencies": normalized_dependencies,
        }
        for key in ("resolved", "integrity"):
            value = str(raw.get(key) or "").strip()
            if value:
                result[key] = value
        return result

    root_name = str(payload.get("name") or "<root>")
    return visit(root_name, payload), count


def _release_source_excluded(name: str) -> bool:
    normalized = str(name).casefold()
    return normalized == ".env" or normalized.startswith(".env.") or normalized in _EXCLUDED_PARTS or normalized in {".next", "coverage", "playwright-report", "test-results"} or normalized.endswith((".pyc", ".pyo"))


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    return {name for name in names if _release_source_excluded(name)}


def _frontend_generated_source_input(name: str) -> bool:
    normalized = str(name).casefold()
    return normalized in {"next-env.d.ts", ".next-dev.log", ".next-dev-error.log"} or normalized.endswith(".tsbuildinfo")


def _frontend_copy_ignore(directory: str, names: list[str]) -> set[str]:
    return _copy_ignore(directory, names) | {name for name in names if _frontend_generated_source_input(name)}


def _make_temp_directory_long_path(parent: Path, *, prefix: str) -> Path:
    parent_io = windows_extended_path(parent)
    created_io = Path(tempfile.mkdtemp(prefix=prefix, dir=parent_io))
    return parent / created_io.relative_to(parent_io)


def _remove_tree_long_path(path: Path, *, ignore_errors: bool = False) -> None:
    shutil.rmtree(windows_extended_path(path), ignore_errors=ignore_errors)


def _replace_long_path(source: Path, destination: Path) -> None:
    os.replace(windows_extended_path(source), windows_extended_path(destination))


def _atomic_write_json_long_path(path: Path, payload: dict[str, Any]) -> None:
    windows_extended_path(path.parent).mkdir(parents=True, exist_ok=True)
    atomic_write_json(windows_extended_path(path), payload)


def _copy_file_long_path(source: Path, destination: Path) -> None:
    _require_regular_file_long_path(source)
    destination_io = windows_extended_path(destination)
    destination_io.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(windows_extended_path(source), destination_io, follow_symlinks=False)


def _require_regular_file_long_path(path: Path) -> os.stat_result:
    try:
        metadata = windows_extended_path(path).lstat()
    except OSError as exc:
        raise DesktopOperationError(
            "desktop_release_filesystem_scan_failed",
            "release resource metadata could not be inspected safely",
            details={"winerror": getattr(exc, "winerror", None), "errno": exc.errno},
        ) from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or (os.name == "nt" and attributes & reparse):
        raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {path.name}")
    if not stat.S_ISREG(metadata.st_mode):
        raise DesktopOperationError(
            "desktop_release_resource_type_invalid",
            f"release resource is not a regular file: {path.name}",
        )
    return metadata


def _require_directory_long_path(path: Path) -> os.stat_result:
    try:
        metadata = windows_extended_path(path).lstat()
    except OSError as exc:
        raise DesktopOperationError(
            "desktop_release_filesystem_scan_failed",
            "release directory metadata could not be inspected safely",
            details={"winerror": getattr(exc, "winerror", None), "errno": exc.errno},
        ) from exc
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(metadata.st_mode) or (os.name == "nt" and attributes & reparse):
        raise DesktopOperationError("desktop_reparse_point_rejected", f"reparse point is not allowed: {path.name}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise DesktopOperationError(
            "desktop_release_resource_type_invalid",
            f"release tree entry is not a directory: {path.name}",
        )
    return metadata


def _resource_entries(root: Path) -> list[dict[str, Any]]:
    snapshot = _scan_release_tree(root, excluded={"release-manifest.json"})
    entries = [
        {"path": relative, "size": size, "sha256": digest}
        for relative, (size, digest) in sorted(snapshot.resources.items())
    ]
    if not entries:
        raise DesktopOperationError("desktop_release_incomplete", "release contains no runtime resources")
    return entries


def _trusted_tree_files(root: Path) -> list[Path]:
    """Inventory a trusted tree without dropping Win32 paths longer than MAX_PATH."""
    snapshot = _scan_release_tree(root)
    return [root / Path(relative) for relative in sorted(snapshot.resources)]


def _scan_release_tree(
    root: Path,
    *,
    excluded: set[str] | frozenset[str] = frozenset(),
    captured: set[str] | frozenset[str] = frozenset(),
) -> SafeTreeSnapshot:
    return scan_tree(
        root,
        excluded=excluded,
        captured=captured,
        filesystem_reason_code="desktop_release_filesystem_scan_failed",
        filesystem_message="release filesystem could not be inventoried safely",
        node_type_reason_code="desktop_release_resource_type_invalid",
    )


def _raise_release_walk_error(error: OSError) -> None:
    raise DesktopOperationError(
        "desktop_release_filesystem_scan_failed",
        "release filesystem could not be inventoried safely",
        details={"winerror": getattr(error, "winerror", None), "errno": error.errno},
    ) from error


def _is_file_long_path(path: Path) -> bool:
    return windows_extended_path(path).is_file()


def _read_text_long_path(path: Path) -> str:
    return read_bytes_long_path(path).decode("utf-8", errors="strict")



def _python_runtime_provenance(executable: Path) -> dict[str, Any] | None:
    attestation_path = executable.parent / "portable-python.json"
    if not _is_file_long_path(attestation_path):
        return None
    try:
        attestation = json.loads(_read_text_long_path(attestation_path))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError("desktop_python_runtime_attestation_invalid", "portable Python attestation is invalid") from exc
    profile = attestation.get("profile") if isinstance(attestation, Mapping) else None
    wheelhouse = attestation.get("wheelhouse") if isinstance(attestation, Mapping) else None
    base_python = attestation.get("base_python") if isinstance(attestation, Mapping) else None
    builder = attestation.get("builder") if isinstance(attestation, Mapping) else None
    if not all(isinstance(item, Mapping) for item in (profile, wheelhouse, base_python, builder)):
        raise DesktopOperationError("desktop_python_runtime_attestation_invalid", "portable Python provenance is incomplete")
    provenance = {
        "profile": dict(profile),
        "base_python": {
            "source_sha256": base_python.get("source_sha256"),
            "manifest_sha256": base_python.get("manifest_sha256"),
        },
        "wheelhouse": {
            "manifest_sha256": wheelhouse.get("manifest_sha256"),
            "receipt_sha256": wheelhouse.get("receipt_sha256"),
            "artifact_count": wheelhouse.get("artifact_count"),
        },
        "builder": {
            "implementation": builder.get("implementation"),
            "version": builder.get("version"),
            "cache_tag": builder.get("cache_tag"),
            "platform": builder.get("platform"),
            "executable_sha256": builder.get("executable_sha256"),
            "pip_version": builder.get("pip_version"),
        },
    }
    digests = [
        provenance["profile"].get("profile_sha256"),
        provenance["profile"].get("constraints_sha256"),
        provenance["profile"].get("requirements_sha256"),
        provenance["base_python"].get("source_sha256"),
        provenance["base_python"].get("manifest_sha256"),
        provenance["wheelhouse"].get("manifest_sha256"),
        provenance["wheelhouse"].get("receipt_sha256"),
        provenance["builder"].get("executable_sha256"),
    ]
    builder_strings = [
        provenance["builder"].get("implementation"),
        provenance["builder"].get("version"),
        provenance["builder"].get("cache_tag"),
        provenance["builder"].get("platform"),
        provenance["builder"].get("pip_version"),
    ]
    if (
        any(not _valid_sha256(value) for value in digests)
        or not isinstance(provenance["wheelhouse"]["artifact_count"], int)
        or any(not isinstance(value, str) or not value for value in builder_strings)
    ):
        raise DesktopOperationError("desktop_python_runtime_attestation_invalid", "portable Python provenance digests are invalid")
    return provenance

def _optional_file_sha256(path: Path) -> str | None:
    return _file_sha256(path) if _is_file_long_path(path) else None


def _optional_verified_sha256(
    path: Path,
    *,
    relative: str,
    verified_resources: Mapping[str, tuple[int, str]] | None,
) -> str | None:
    if verified_resources is None:
        return _optional_file_sha256(path)
    identity = verified_resources.get(relative)
    return identity[1] if identity is not None else None


def _tree_digest(root: Path) -> str:
    digest = sha256()
    for entry in _resource_entries(root):
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _json_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def _decode_json_object(data: bytes, *, reason_code: str, message: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DesktopOperationError(reason_code, message) from exc
    if not isinstance(value, dict):
        raise DesktopOperationError(reason_code, message)
    return value


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or any(":" in part or part.endswith((" ", ".")) for part in Path(value).parts):
        raise DesktopOperationError("desktop_release_manifest_invalid", "release resource path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DesktopOperationError("desktop_release_manifest_invalid", "release resource path is unsafe")
    return path.as_posix()


def _external_config_identity(
    environment: Mapping[str, str],
    *,
    project_root: Path | None = None,
    release_path: Path | None = None,
    verified_resources: Mapping[str, tuple[int, str]] | None = None,
) -> list[dict[str, Any]]:
    dependencies: list[dict[str, Any]] = []
    trusted_project_root = project_root.resolve(strict=False) if project_root is not None else None
    for name in _EXTERNAL_CONFIG_ENV_NAMES:
        raw = str(environment.get(name) or "").strip()
        if not raw:
            continue
        path = Path(raw).expanduser().resolve(strict=False)
        if trusted_project_root is not None and path.is_relative_to(trusted_project_root):
            relative = path.relative_to(trusted_project_root)
            if relative.parts and relative.parts[0] in _BACKEND_SOURCE_ROOTS:
                packaged_relative = Path("app") / relative
                packaged_key = packaged_relative.as_posix()
                if verified_resources is not None:
                    identity = verified_resources.get(packaged_key)
                    if identity is None:
                        raise DesktopOperationError("desktop_release_config_missing", f"packaged runtime configuration file is missing: {name}")
                    dependencies.append({"name": name, "mode": "packaged", "resource": packaged_key, "size": identity[0], "sha256": identity[1]})
                    continue
                packaged = (release_path / packaged_relative).resolve(strict=False) if release_path is not None else path
                if not _is_file_long_path(packaged):
                    raise DesktopOperationError("desktop_release_config_missing", f"packaged runtime configuration file is missing: {name}")
                reject_reparse_path(packaged, root=release_path if release_path is not None else packaged.parent)
                data = read_bytes_long_path(packaged)
                dependencies.append({"name": name, "mode": "packaged", "resource": packaged_key, "size": len(data), "sha256": sha256(data).hexdigest()})
                continue
        if not _is_file_long_path(path):
            raise DesktopOperationError("desktop_external_config_missing", f"declared runtime configuration file is missing: {name}")
        reject_reparse_path(path, root=path.parent)
        data = read_bytes_long_path(path)
        dependencies.append({"name": name, "mode": "external", "path": str(path), "size": len(data), "sha256": sha256(data).hexdigest()})
    return dependencies


def _write_source_input_manifest(root: Path, *, source_digest: str) -> None:
    entries = _source_input_entries(root)
    calculated = _source_digest_from_entries(entries)
    if calculated != source_digest:
        raise DesktopOperationError("desktop_release_source_identity_invalid", "source input inventory does not match its digest")
    _atomic_write_json_long_path(
        root / _SOURCE_INPUT_MANIFEST,
        {
            "schema": _SOURCE_INPUT_SCHEMA,
            "source_digest": source_digest,
            "entries": entries,
            "entries_sha256": _json_digest({"entries": entries}),
        },
    )


def _source_input_entries(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    app_root = root / "app" if windows_extended_path(root / "app").is_dir() else root
    selected_roots = [*((app_root / name, f"app/{name}") for name in _BACKEND_SOURCE_ROOTS), (root / "frontend", "frontend")]
    for source_root, label in selected_roots:
        if not windows_extended_path(source_root).is_dir():
            continue
        snapshot = _scan_release_tree(source_root)
        for relative, (size, digest) in sorted(snapshot.resources.items()):
            relative_path = Path(relative)
            if any(_release_source_excluded(part) for part in relative_path.parts):
                continue
            if label == "frontend" and _frontend_generated_source_input(relative_path.name):
                continue
            entries.append({"path": f"{label}/{relative}", "size": size, "sha256": digest})
    return entries


def _source_digest_from_entries(entries: Iterable[Mapping[str, Any]]) -> str:
    digest = sha256()
    seen: set[str] = set()
    for item in entries:
        path = str(item.get("path") or "")
        resource_digest = str(item.get("sha256") or "")
        size = item.get("size")
        if (
            not path
            or path in seen
            or not isinstance(size, int)
            or size < 0
            or len(resource_digest) != 64
            or any(character not in "0123456789abcdef" for character in resource_digest)
        ):
            raise DesktopOperationError("desktop_release_source_identity_invalid", "source input inventory entry is invalid")
        seen.add(path)
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(resource_digest))
    return digest.hexdigest()


def _verified_source_input_digest(snapshot: SafeTreeSnapshot) -> str:
    data = snapshot.captured.get(_SOURCE_INPUT_MANIFEST)
    if data is None:
        return _source_input_digest_from_snapshot(snapshot)
    manifest = _decode_json_object(
        data,
        reason_code="desktop_release_source_identity_invalid",
        message="source input provenance is invalid",
    )
    entries = manifest.get("entries")
    if manifest.get("schema") != _SOURCE_INPUT_SCHEMA or not isinstance(entries, list):
        raise DesktopOperationError("desktop_release_source_identity_invalid", "source input provenance schema is invalid")
    if manifest.get("entries_sha256") != _json_digest({"entries": entries}):
        raise DesktopOperationError("desktop_release_source_identity_invalid", "source input provenance inventory changed")
    calculated = _source_digest_from_entries(entries)
    if calculated != manifest.get("source_digest"):
        raise DesktopOperationError("desktop_release_source_identity_invalid", "source input provenance digest is invalid")
    return calculated

def _source_input_digest(root: Path) -> str:
    return _source_digest_from_entries(_source_input_entries(root))

def _source_input_digest_from_snapshot(snapshot: SafeTreeSnapshot) -> str:
    digest = sha256()
    app_prefix = "app" if "app" in snapshot.directories else ""
    for name in _BACKEND_SOURCE_ROOTS:
        physical_prefix = f"{app_prefix}/{name}" if app_prefix else name
        _update_source_digest(
            digest,
            snapshot.resources,
            label=f"app/{name}",
            physical_prefix=physical_prefix,
        )
    _update_source_digest(digest, snapshot.resources, label="frontend", physical_prefix="frontend")
    return digest.hexdigest()


def _update_source_digest(
    digest: Any,
    resources: Mapping[str, tuple[int, str]],
    *,
    label: str,
    physical_prefix: str = "",
) -> None:
    prefix = physical_prefix.rstrip("/")
    selected: list[tuple[str, str]] = []
    for resource_path, (_size, resource_digest) in resources.items():
        if prefix:
            marker = prefix + "/"
            if not resource_path.startswith(marker):
                continue
            source_relative = resource_path[len(marker) :]
        else:
            source_relative = resource_path
        relative_path = Path(source_relative)
        if any(_release_source_excluded(part) for part in relative_path.parts):
            continue
        if label == "frontend" and _frontend_generated_source_input(relative_path.name):
            continue
        selected.append((source_relative, resource_digest))
    for source_relative, resource_digest in sorted(selected):
        relative = f"{label}/{source_relative}"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(resource_digest))


def _file_sha256(path: Path) -> str:
    return sha256(read_bytes_long_path(path)).hexdigest()

__all__ = ["DATA_SCHEMAS", "RELEASE_SCHEMA", "ReleaseIdentity", "ReleaseManager"]
