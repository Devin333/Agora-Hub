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

RELEASE_SCHEMA = "newsroom.local-desktop-release/v1"
ACTIVE_RELEASE_SCHEMA = "newsroom.local-desktop-active-release/v1"
CONFIG_SCHEMA = "newsroom.local-desktop-config/v1"
from scripts.local_desktop.storage_contract import STORAGE_SCHEMAS

DATA_SCHEMAS = STORAGE_SCHEMAS
_BACKEND_SOURCE_ROOTS = ("backend", "framework", "infrastructure", "interfaces", "configs", "sdk", "scripts")
_FRONTEND_RUNTIME_ITEMS = (".next", "public", "node_modules", "package.json", "package-lock.json", "next.config.mjs", "root-env.mjs")
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
            self._run([npm, "ci"], cwd=frontend_root, env=environment, timeout=900)
            self._run([npm, "run", "build"], cwd=frontend_root, env=environment, timeout=1200)
            _remove_tree_long_path(frontend_root / ".next" / "cache", ignore_errors=True)
            for name in _FRONTEND_RUNTIME_ITEMS:
                if not windows_extended_path(frontend_root / name).exists():
                    raise DesktopOperationError("desktop_release_incomplete", f"frontend build resource is missing: {name}")
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
                    "frontend": ["next", "start", "-H", self.config.frontend_host, "-p", str(self.config.frontend_port)],
                },
                "resources": resources,
                "exclusions": [".env", ".env.*", ".newsroom", "development caches", "provider credentials", "user data", "source links and junctions"],
            }
            _atomic_write_json_long_path(stage / "release-manifest.json", manifest)
            destination = self.config.release_root / release_id
            if windows_extended_path(destination).exists():
                raise DesktopOperationError("desktop_release_exists", f"release already exists: {release_id}")
            _replace_long_path(stage, destination)
            verified = self.verify(destination)
            self.activate(destination)
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
        try:
            manifest = json.loads(_read_text_long_path(manifest_path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release manifest is invalid") from exc
        if manifest.get("schema") != RELEASE_SCHEMA:
            raise DesktopOperationError("desktop_release_schema_unsupported", "release manifest schema is unsupported")
        expected = manifest.get("resources")
        if not isinstance(expected, list) or not expected:
            raise DesktopOperationError("desktop_release_incomplete", "release resource inventory is empty")
        seen: set[str] = set()
        for entry in expected:
            if not isinstance(entry, dict):
                raise DesktopOperationError("desktop_release_manifest_invalid", "release resource entry is invalid")
            relative = _safe_relative(entry.get("path"))
            if relative in seen:
                raise DesktopOperationError("desktop_release_manifest_invalid", f"duplicate release resource: {relative}")
            seen.add(relative)
            lexical_resource = path / relative
            reject_reparse_path(lexical_resource, root=path)
            resource = ensure_contained(path, lexical_resource, field="release resource")
            if not _is_file_long_path(resource):
                raise DesktopOperationError("desktop_release_resource_missing", f"release resource is missing: {relative}")
            data = read_bytes_long_path(resource)
            if len(data) != entry.get("size") or sha256(data).hexdigest() != entry.get("sha256"):
                raise DesktopOperationError("desktop_release_checksum_mismatch", f"release resource checksum mismatch: {relative}")
        actual_paths = {entry["path"] for entry in _resource_entries(path)}
        if actual_paths != seen:
            raise DesktopOperationError(
                "desktop_release_resource_inventory_mismatch",
                "release contains missing or unmanifested resources",
                details={"missing_count": len(seen - actual_paths), "extra_count": len(actual_paths - seen)},
            )
        entrypoints = manifest.get("entrypoints")
        if not isinstance(entrypoints, dict) or set(entrypoints) != {"api", "frontend"}:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release entrypoints are incomplete")
        expected_api = ["-m", "interfaces.cli.news", "api", "serve", "--host", self.config.api_host, "--port", str(self.config.api_port)]
        expected_frontend = ["next", "start", "-H", self.config.frontend_host, "-p", str(self.config.frontend_port)]
        if entrypoints.get("api") != expected_api:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release API entrypoint is invalid")
        if entrypoints.get("frontend") != expected_frontend:
            raise DesktopOperationError("desktop_release_manifest_invalid", "release frontend entrypoint is invalid")
        if not _is_file_long_path(path / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"):
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
        if _source_input_digest(path) != identity.get("source_digest"):
            raise DesktopOperationError("desktop_release_source_drift", "packaged source does not match the release identity")
        config_identity = manifest.get("config")
        if not isinstance(config_identity, Mapping) or _json_digest(config_identity) != identity.get("config_digest"):
            raise DesktopOperationError("desktop_release_config_drift", "release configuration identity is invalid")
        if runtime_config_document is not None:
            expected_config = self._configuration_identity_for_document(
                path,
                runtime_config_document,
                bind_unconfigured=True,
            )
        else:
            from scripts.local_desktop.runtime_config import read_runtime_config_document, runtime_config_paths

            expected_config = self._configuration_identity_for_document(
                path,
                read_runtime_config_document(runtime_config_paths(self.config).document),
                bind_unconfigured=isinstance(manifest.get("desktop_runtime_configuration"), Mapping),
            )
        if dict(config_identity) != expected_config:
            raise DesktopOperationError("desktop_release_config_incompatible", "release was prepared for a different local desktop configuration")
        current_dependencies = self._dependency_identity(path / "frontend")
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
        _reject_existing_reparse_chain(release_path / resource)
        bound_models = ensure_contained(release_path, release_path / resource, field="bound runtime models configuration")
        reject_reparse_path(bound_models, root=release_path)
        if not _is_file_long_path(bound_models):
            raise DesktopOperationError("desktop_runtime_models_missing", "bound runtime models configuration is missing")
        expected_bytes = (json.dumps(models, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
        path = self.resolve_release(release)
        manifest = self.verify(path)
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
        return path, manifest

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

    def dependency_identity(self, frontend_root: Path | None = None) -> dict[str, Any]:
        """Return the dependency identity of the interpreter and tools running this manager."""
        dependency_root = frontend_root or self.config.frontend_root
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
            },
            "node": {
                "executable": str(Path(node_executable).resolve(strict=False)) if node_executable else None,
                "executable_sha256": _file_sha256(Path(node_executable)) if node_executable else None,
                "version": self._tool_version([node_executable, "--version"]) if node_executable else None,
                "npm_executable": str(Path(npm_executable).resolve(strict=False)) if npm_executable else None,
                "npm_executable_sha256": _file_sha256(Path(npm_executable)) if npm_executable else None,
                "npm_version": self._tool_version([npm_executable, "--version"]) if npm_executable else None,
                "package_lock_sha256": _optional_file_sha256(lockfile),
                "installed_lock_sha256": _optional_file_sha256(installed_lock),
                "installed_tree": self._node_dependency_tree_identity(dependency_root, npm_executable) if npm_executable else None,
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
        lockfile = dependency_root / "package-lock.json"
        installed_lock = dependency_root / "node_modules" / ".package-lock.json"
        return {
            "python": {
                "executable": str(python),
                "executable_sha256": _file_sha256(python),
                "version": python_identity["version"],
                "packages": packages,
            },
            "node": {
                "executable": str(node),
                "executable_sha256": _file_sha256(node),
                "version": self._required_tool_version([str(node), "--version"], environment=environment),
                "npm_executable": str(npm),
                "npm_executable_sha256": _file_sha256(npm),
                "npm_version": self._required_tool_version([str(npm), "--version"], environment=environment),
                "package_lock_sha256": _optional_file_sha256(lockfile),
                "installed_lock_sha256": _optional_file_sha256(installed_lock),
                "installed_tree": self._node_dependency_tree_identity(dependency_root, str(npm), environment=environment),
            },
        }

    def _dependency_identity(self, frontend_root: Path | None = None) -> dict[str, Any]:
        return self.dependency_identity(frontend_root)

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
    entries: list[dict[str, Any]] = []
    for path in _trusted_tree_files(root):
        relative = path.relative_to(root).as_posix()
        if relative == "release-manifest.json":
            continue
        data = read_bytes_long_path(path)
        entries.append({"path": relative, "size": len(data), "sha256": sha256(data).hexdigest()})
    if not entries:
        raise DesktopOperationError("desktop_release_incomplete", "release contains no runtime resources")
    return entries


def _trusted_tree_files(root: Path) -> list[Path]:
    """Inventory a trusted tree without dropping Win32 paths longer than MAX_PATH."""
    reject_reparse_path(root, root=root)
    _require_directory_long_path(root)
    root_io = windows_extended_path(root)
    files: list[Path] = []
    for directory, names, filenames in os.walk(
        root_io,
        topdown=True,
        onerror=_raise_release_walk_error,
        followlinks=False,
    ):
        names.sort()
        filenames.sort()
        lexical_directory = root / Path(directory).relative_to(root_io)
        reject_reparse_path(lexical_directory, root=root)
        for name in names:
            directory_path = lexical_directory / name
            reject_reparse_path(directory_path, root=root)
            _require_directory_long_path(directory_path)
        for name in filenames:
            path = lexical_directory / name
            reject_reparse_path(path, root=root)
            _require_regular_file_long_path(path)
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


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


def _optional_file_sha256(path: Path) -> str | None:
    return _file_sha256(path) if _is_file_long_path(path) else None


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
                packaged = (release_path / packaged_relative).resolve(strict=False) if release_path is not None else path
                if not _is_file_long_path(packaged):
                    raise DesktopOperationError("desktop_release_config_missing", f"packaged runtime configuration file is missing: {name}")
                reject_reparse_path(packaged, root=release_path if release_path is not None else packaged.parent)
                data = read_bytes_long_path(packaged)
                dependencies.append({"name": name, "mode": "packaged", "resource": packaged_relative.as_posix(), "size": len(data), "sha256": sha256(data).hexdigest()})
                continue
        if not _is_file_long_path(path):
            raise DesktopOperationError("desktop_external_config_missing", f"declared runtime configuration file is missing: {name}")
        reject_reparse_path(path, root=path.parent)
        data = read_bytes_long_path(path)
        dependencies.append({"name": name, "mode": "external", "path": str(path), "size": len(data), "sha256": sha256(data).hexdigest()})
    return dependencies

def _source_input_digest(root: Path) -> str:
    digest = sha256()
    app_root = root / "app" if windows_extended_path(root / "app").is_dir() else root
    roots = [*((app_root / name, f"app/{name}") for name in _BACKEND_SOURCE_ROOTS), (root / "frontend", "frontend")]
    for source_root, label in roots:
        if not windows_extended_path(source_root).exists():
            continue
        reject_reparse_path(source_root, root=source_root.parent)
        for source_path in _trusted_tree_files(source_root):
            source_relative = source_path.relative_to(source_root)
            relative_parts = source_relative.parts
            if any(_release_source_excluded(part) for part in relative_parts):
                continue
            if label == "frontend" and _frontend_generated_source_input(source_relative.name):
                continue
            relative = f"{label}/{source_relative.as_posix()}"
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha256(read_bytes_long_path(source_path)).digest())
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return sha256(read_bytes_long_path(path)).hexdigest()

__all__ = ["DATA_SCHEMAS", "RELEASE_SCHEMA", "ReleaseIdentity", "ReleaseManager"]
