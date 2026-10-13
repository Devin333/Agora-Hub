from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from framework.shared.local_desktop import LocalDesktopConfig, LocalDesktopConfigError, utc_timestamp
from scripts.local_desktop.common import DesktopOperationError, atomic_write_json, ensure_contained, reject_reparse_path
from scripts.local_desktop.config import REAL_DATA_MODE, require_real_data_environment, sanitized_environment

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
        command_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.config = config
        self.command_runner = command_runner or subprocess.run

    @property
    def active_pointer(self) -> Path:
        return self.config.runtime_root / "active-release.json"

    def build(self) -> dict[str, Any]:
        self._require_build_inputs()
        self.config.release_root.mkdir(parents=True, exist_ok=True)
        staging_parent = self.config.release_root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=staging_parent))
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
                if source.exists():
                    self._copy_tree(source, app_root / name)
            self._copy_tree(self.config.frontend_root, frontend_root)
            source_revision = self._git_revision()
            if source_revision == "unavailable":
                raise DesktopOperationError("desktop_source_revision_unavailable", "a production release requires a resolvable source revision")
            source_digest = _source_input_digest(stage)
            self._run([npm, "ci"], cwd=frontend_root, env=environment, timeout=900)
            self._run([npm, "run", "build"], cwd=frontend_root, env=environment, timeout=1200)
            shutil.rmtree(frontend_root / ".next" / "cache", ignore_errors=True)
            for name in _FRONTEND_RUNTIME_ITEMS:
                if not (frontend_root / name).exists():
                    raise DesktopOperationError("desktop_release_incomplete", f"frontend build resource is missing: {name}")
            dependencies = self._dependency_identity(frontend_root)
            dependency_digest = _json_digest(dependencies)
            config_identity = {
                "schema": CONFIG_SCHEMA,
                "profile": self.config.deployment_profile,
                "canonical_origin": self.config.canonical_origin,
                "api_base_url": self.config.api_base_url,
                "parser_mode": self.config.parser_mode,
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
            atomic_write_json(stage / "release-manifest.json", manifest)
            destination = self.config.release_root / release_id
            if destination.exists():
                raise DesktopOperationError("desktop_release_exists", f"release already exists: {release_id}")
            os.replace(stage, destination)
            verified = self.verify(destination)
            self.activate(destination)
            return verified
        except Exception:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            raise

    def verify(self, release: str | Path) -> dict[str, Any]:
        path = self.resolve_release(release)
        manifest_path = path / "release-manifest.json"
        if not manifest_path.is_file():
            raise DesktopOperationError("desktop_release_manifest_missing", "release manifest is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
            resource = ensure_contained(path, path / relative, field="release resource")
            reject_reparse_path(resource, root=path)
            if not resource.is_file():
                raise DesktopOperationError("desktop_release_resource_missing", f"release resource is missing: {relative}")
            data = resource.read_bytes()
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
        if not (path / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next").is_file():
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
        expected_config = {
            "schema": CONFIG_SCHEMA,
            "profile": self.config.deployment_profile,
            "canonical_origin": self.config.canonical_origin,
            "api_base_url": self.config.api_base_url,
            "parser_mode": self.config.parser_mode,
            "data_mode": REAL_DATA_MODE,
            "external_config_dependencies": _external_config_identity(sanitized_environment(self.config), project_root=self.config.project_root, release_path=path),
        }
        if dict(config_identity) != expected_config:
            raise DesktopOperationError("desktop_release_config_incompatible", "release was prepared for a different local desktop configuration")
        current_dependencies = self._dependency_identity(path / "frontend")
        if _json_digest(current_dependencies) != identity.get("dependency_digest"):
            raise DesktopOperationError("desktop_release_dependency_drift", "current interpreter, Node runtime, or installed dependencies do not match the release")
        return manifest

    def bind_runtime_environment(self, release_path: Path, manifest: Mapping[str, Any], environment: Mapping[str, str]) -> dict[str, str]:
        config_identity = manifest.get("config")
        if not isinstance(config_identity, Mapping) or config_identity.get("data_mode") != REAL_DATA_MODE:
            raise DesktopOperationError(
                "desktop_release_data_mode_invalid",
                "release is not bound to the real local desktop data mode",
            )
        try:
            result = require_real_data_environment(environment)
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
            if mode == "packaged":
                resource = ensure_contained(release_path, release_path / str(dependency.get("resource") or ""), field="release configuration resource")
                reject_reparse_path(resource, root=release_path)
                if not resource.is_file():
                    raise DesktopOperationError("desktop_release_config_drift", "packaged runtime configuration resource is missing")
                result[name] = str(resource)
            elif mode == "external":
                result[name] = str(dependency.get("path") or "")
            else:
                raise DesktopOperationError("desktop_release_config_drift", "release runtime configuration dependency mode is invalid")
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
        atomic_write_json(self.active_pointer, pointer)
        return pointer

    def active(self) -> tuple[Path, dict[str, Any]]:
        if not self.active_pointer.is_file():
            raise DesktopOperationError("desktop_release_not_activated", "no production release is active; run build first")
        try:
            pointer = json.loads(self.active_pointer.read_text(encoding="utf-8"))
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
        resolved = path.resolve(strict=False)
        root = self.config.release_root.resolve(strict=False)
        if not resolved.is_relative_to(root) or resolved == root or ".staging" in resolved.parts:
            raise DesktopOperationError("desktop_release_path_unsafe", "release must be an owned child of the configured release root")
        return resolved

    def _require_build_inputs(self) -> None:
        if not self.config.python_executable.is_file():
            raise DesktopOperationError("desktop_project_python_missing", "project .venv interpreter is missing")
        if os.path.normcase(str(Path(sys.executable).resolve(strict=False))) != os.path.normcase(str(self.config.python_executable.resolve(strict=False))):
            raise DesktopOperationError("desktop_wrong_python", "production release must be built with the project .venv interpreter")
        for path in (self.config.frontend_root / "package.json", self.config.frontend_root / "package-lock.json"):
            if not path.is_file():
                raise DesktopOperationError("desktop_frontend_lock_missing", f"required frontend build input is missing: {path.name}")

    def _dependency_identity(self, frontend_root: Path | None = None) -> dict[str, Any]:
        dependency_root = frontend_root or self.config.frontend_root
        lockfile = dependency_root / "package-lock.json"
        installed_lock = dependency_root / "node_modules" / ".package-lock.json"
        python_packages = sorted(
            (distribution.metadata.get("Name", ""), distribution.version)
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
                "package_lock_sha256": sha256(lockfile.read_bytes()).hexdigest() if lockfile.is_file() else None,
                "installed_lock_sha256": sha256(installed_lock.read_bytes()).hexdigest() if installed_lock.is_file() else None,
                "installed_tree": (
                    self._node_dependency_tree_identity(
                        dependency_root,
                        npm_executable,
                    )
                    if npm_executable
                    else None
                ),
            },
        }

    def _node_dependency_tree_identity(
        self,
        frontend_root: Path,
        npm_executable: str,
    ) -> dict[str, Any]:
        try:
            result = self.command_runner(
                [npm_executable, "ls", "--json", "--all"],
                cwd=frontend_root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DesktopOperationError(
                "desktop_node_dependency_tree_unavailable",
                "installed Node dependency tree could not be inspected",
            ) from exc
        try:
            payload = json.loads(result.stdout)
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

    def _tool_version(self, argv: list[str]) -> str | None:
        try:
            result = self.command_runner(argv, capture_output=True, text=True, timeout=10, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def _git_revision(self) -> str:
        try:
            result = self.command_runner(["git", "rev-parse", "HEAD"], cwd=self.config.project_root, capture_output=True, text=True, timeout=10, check=False, shell=False)
        except (OSError, subprocess.TimeoutExpired):
            return "unavailable"
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unavailable"

    def _run(self, argv: list[str], *, cwd: Path, env: Mapping[str, str], timeout: float) -> None:
        try:
            result = self.command_runner(argv, cwd=cwd, env=dict(env), capture_output=True, text=True, timeout=timeout, check=False, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise DesktopOperationError("desktop_build_timeout", f"build command exceeded its deadline: {Path(argv[0]).name}") from exc
        except OSError as exc:
            raise DesktopOperationError("desktop_build_command_failed", f"could not start build command: {Path(argv[0]).name}") from exc
        if result.returncode != 0:
            raise DesktopOperationError("desktop_build_failed", f"production build command failed: {Path(argv[0]).name}", details={"returncode": result.returncode})

    @staticmethod
    def _copy_tree(source: Path, destination: Path) -> None:
        lexical_source = source.expanduser().absolute()
        reject_reparse_path(lexical_source, root=lexical_source.parent)
        source = lexical_source.resolve(strict=True)
        reject_reparse_path(source, root=source)
        for item in source.rglob("*"):
            reject_reparse_path(item, root=source)
        shutil.copytree(source, destination, ignore=_copy_ignore, copy_function=shutil.copy2, symlinks=False)


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


def _resource_entries(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        reject_reparse_path(path, root=root)
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative == "release-manifest.json":
            continue
        data = path.read_bytes()
        entries.append({"path": relative, "size": len(data), "sha256": sha256(data).hexdigest()})
    if not entries:
        raise DesktopOperationError("desktop_release_incomplete", "release contains no runtime resources")
    return entries


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
                if not packaged.is_file():
                    raise DesktopOperationError("desktop_release_config_missing", f"packaged runtime configuration file is missing: {name}")
                reject_reparse_path(packaged, root=release_path if release_path is not None else packaged.parent)
                data = packaged.read_bytes()
                dependencies.append({"name": name, "mode": "packaged", "resource": packaged_relative.as_posix(), "size": len(data), "sha256": sha256(data).hexdigest()})
                continue
        if not path.is_file():
            raise DesktopOperationError("desktop_external_config_missing", f"declared runtime configuration file is missing: {name}")
        reject_reparse_path(path, root=path.parent)
        data = path.read_bytes()
        dependencies.append({"name": name, "mode": "external", "path": str(path), "size": len(data), "sha256": sha256(data).hexdigest()})
    return dependencies

def _source_input_digest(root: Path) -> str:
    digest = sha256()
    app_root = root / "app" if (root / "app").is_dir() else root
    roots = [*((app_root / name, f"app/{name}") for name in _BACKEND_SOURCE_ROOTS), (root / "frontend", "frontend")]
    for source_root, label in roots:
        if not source_root.exists():
            continue
        reject_reparse_path(source_root, root=source_root.parent)
        for source_path in sorted(source_root.rglob("*")):
            reject_reparse_path(source_path, root=source_root)
            if not source_path.is_file():
                continue
            relative_parts = source_path.relative_to(source_root).parts
            if any(_release_source_excluded(part) for part in relative_parts):
                continue
            relative = f"{label}/{source_path.relative_to(source_root).as_posix()}"
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(sha256(source_path.read_bytes()).digest())
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()

__all__ = ["DATA_SCHEMAS", "RELEASE_SCHEMA", "ReleaseIdentity", "ReleaseManager"]
