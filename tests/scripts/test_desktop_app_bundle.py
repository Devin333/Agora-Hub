from __future__ import annotations

import json
import os
import subprocess
import zipfile
from collections import Counter
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.desktop_app.bundle as bundle_module
import scripts.desktop_app.runtime_profile as runtime_profile_module
import scripts.local_desktop.safe_tree as safe_tree_module
from scripts.desktop_app.bundle import BUNDLE_SCHEMA, PortableBundleManager, stage_portable_bundle
from scripts.desktop_app.config import resolve_desktop_app_config
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.release import DATA_SCHEMAS, RELEASE_SCHEMA, ReleaseManager


def _digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _entry(root: Path, relative: str) -> dict[str, object]:
    data = (root / relative).read_bytes()
    return {"path": relative, "size": len(data), "sha256": sha256(data).hexdigest()}


def _inventory(root: Path) -> list[dict[str, object]]:
    return [_entry(root, path.relative_to(root).as_posix()) for path in sorted(root.rglob("*")) if path.is_file() and path.name != "desktop-bundle.json"]


def _probe() -> dict[str, object]:
    return {
        "version": "3.12.14",
        "implementation": "CPython",
        "cache_tag": "cpython-312",
        "abi": "",
        "platform": "win-amd64",
        "packages": [[name, version] for name, version in bundle_module._DEFAULT_RUNTIME_PROFILE.packages],
        "sqlite_version": "3.53.1",
        "openssl_version": "OpenSSL 3.5.8",
        "capabilities": {
            "api_routes": 42,
            "api_health": True,
            "text_pdf": True,
            "sqlite_wal": True,
            "auth": True,
            "credential_acl": True,
            "tls": True,
            "optional_modules_loaded": [],
        },
    }


def _profile_provenance() -> dict[str, object]:
    artifacts = [
        {"name": name, "version": version, "file": f"{name}-{version}.whl", "sha256": "a" * 64, "requires_dist": []}
        for name, version in bundle_module._DEFAULT_RUNTIME_PROFILE.packages
    ]
    return {
        "profile": bundle_module._DEFAULT_RUNTIME_PROFILE.identity(),
        "builder": {
            "implementation": "CPython",
            "version": "3.12.7",
            "cache_tag": "cpython-312",
            "platform": "win-amd64",
            "executable_sha256": "f" * 64,
            "pip_version": "25.2",
        },
        "base_python": {"source_sha256": "b" * 64, "manifest_sha256": "c" * 64},
        "wheelhouse": {
            "manifest_sha256": "d" * 64,
            "receipt_sha256": _digest({"artifacts": artifacts}),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        },
        "record_count": len(artifacts),
        "licenses": [{"name": name, "version": version} for name, version in bundle_module._DEFAULT_RUNTIME_PROFILE.packages],
    }


def _manifest(root: Path) -> dict[str, object]:
    release_resources = [
        _entry(root, "interfaces/__init__.py") | {"path": "app/interfaces/__init__.py"},
        _entry(root, "configs/models.yaml") | {"path": "app/configs/models.yaml"},
        _entry(root, "configs/sources.yaml") | {"path": "app/configs/sources.yaml"},
        _entry(root, "frontend/package.json") | {"path": "frontend/package.json"},
        _entry(root, "frontend/package-lock.json") | {"path": "frontend/package-lock.json"},
        _entry(root, "frontend/node_modules/.package-lock.json") | {"path": "frontend/node_modules/.package-lock.json"},
        _entry(root, "frontend/node_modules/next/dist/bin/next") | {"path": "frontend/node_modules/next/dist/bin/next"},
    ]
    model = _entry(root, "configs/models.yaml")
    source = _entry(root, "configs/sources.yaml")
    config_dependencies = [
        {"name": "NEWS_MODELS_CONFIG", "mode": "packaged", "resource": "app/configs/models.yaml", "size": model["size"], "sha256": model["sha256"]},
        {"name": "NEWS_RESEARCH_MODELS_CONFIG", "mode": "packaged", "resource": "app/configs/models.yaml", "size": model["size"], "sha256": model["sha256"]},
        {"name": "NEWS_SOURCES_CONFIG", "mode": "packaged", "resource": "app/configs/sources.yaml", "size": source["size"], "sha256": source["sha256"]},
    ]
    return {
        "schema": RELEASE_SCHEMA,
        "created_at": "2026-10-14T00:00:00Z",
        "identity": {"release_id": "20261014T000000Z-test", "source_revision": "a" * 40, "source_digest": "b" * 64, "dependency_digest": "c" * 64, "config_digest": "d" * 64},
        "config": {"schema": "newsroom.local-desktop-config/v1", "profile": "local-desktop", "canonical_origin": "http://127.0.0.1:3000", "api_base_url": "http://127.0.0.1:8000", "parser_mode": "pymupdf", "storage_mode": "owned-local", "data_mode": "real", "external_config_dependencies": config_dependencies},
        "dependencies": {
            "python": {
                "executable": "C:/build/python.exe",
                "executable_sha256": "1" * 64,
                "version": "3.12.14",
                "packages": _probe()["packages"],
                "runtime_profile": {
                    "profile": _profile_provenance()["profile"],
                    "builder": _profile_provenance()["builder"],
                    "base_python": _profile_provenance()["base_python"],
                    "wheelhouse": {
                        key: _profile_provenance()["wheelhouse"][key]
                        for key in ("manifest_sha256", "receipt_sha256", "artifact_count")
                    },
                },
            },
            "node": {"executable": "C:/build/node.exe", "executable_sha256": "2" * 64, "version": "v24.10.0", "npm_executable": "C:/build/npm.cmd", "npm_executable_sha256": "3" * 64, "npm_version": "11.6.1"},
        },
        "data_compatibility": {"readable_schemas": DATA_SCHEMAS, "writable_schemas": DATA_SCHEMAS},
        "entrypoints": {"api": ["-m", "interfaces.cli.news", "api", "serve", "--host", "127.0.0.1", "--port", "8000"], "frontend": ["next", "start", "-H", "127.0.0.1", "-p", "3000"]},
        "resources": release_resources,
        "exclusions": [],
    }


def _write_bundle(tmp_path: Path):
    root = tmp_path / "runtime"
    for relative, data in {
        ".venv/Scripts/python.exe": b"python-runtime",
        "node/node.exe": b"node-runtime",
        "node/npm.cmd": b"npm-runtime",
        "node/node_modules/npm/bin/npm-cli.js": b"npm-cli",
        "interfaces/__init__.py": b"",
        "configs/models.yaml": b"providers: {}\n",
        "configs/sources.yaml": b"sources: {}\n",
        "frontend/package.json": b"{}",
        "frontend/package-lock.json": b"{}",
        "frontend/node_modules/.package-lock.json": b"{}",
        "frontend/node_modules/next/dist/bin/next": b"next",
    }.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = _manifest(root)
    original = root / ".desktop-app/original-release-manifest.json"
    original.parent.mkdir(parents=True)
    original.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    probe = _probe()
    envelope = {
        "schema": BUNDLE_SCHEMA,
        "release": {"manifest_path": ".desktop-app/original-release-manifest.json", "manifest_sha256": sha256(original.read_bytes()).hexdigest(), "original_identity": manifest["identity"]},
        "runtime": {
            "python": {"path": ".venv/Scripts/python.exe", "sha256": sha256((root / ".venv/Scripts/python.exe").read_bytes()).hexdigest(), "required_imports": list(bundle_module._DEFAULT_IMPORTS), "required_attributes": bundle_module._attribute_descriptors(bundle_module._DEFAULT_IMPORT_ATTRIBUTES), "probe_digest": _digest(probe), "distribution_count": len(probe["packages"]), "excluded_input_paths": [], "launch_requirements": {"arguments": ["-B", "-s"], "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"}}, "sqlite_version": probe["sqlite_version"], "openssl_version": probe["openssl_version"], **_profile_provenance(), "license_count": len(_profile_provenance()["licenses"])},
            "node": {"path": "node/node.exe", "sha256": sha256((root / "node/node.exe").read_bytes()).hexdigest(), "version": "v24.10.0", "npm": {"path": "node/npm.cmd", "sha256": sha256((root / "node/npm.cmd").read_bytes()).hexdigest(), "version": "11.6.1", "cli_path": "node/node_modules/npm/bin/npm-cli.js"}},
        },
        "relocation_policy": {"allowed": list(bundle_module._ALLOWED_RELOCATIONS), "forbidden": list(bundle_module._FORBIDDEN_RELOCATIONS)},
        "resources": _inventory(root),
    }
    envelope["bundle_id"] = bundle_module._bundle_id(envelope["release"]["original_identity"], envelope["runtime"], envelope["resources"], envelope["relocation_policy"])
    (root / "desktop-bundle.json").write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    return root, manifest, envelope, probe


class FakeRunner:
    def __init__(self, probe: dict[str, object]) -> None:
        self.probe = probe

    def __call__(self, argv, **kwargs):
        if str(argv[0]).endswith("python.exe"):
            stdout = json.dumps(self.probe, sort_keys=True, separators=(",", ":")).encode()
        elif len(argv) == 2 and argv[1] == "--version":
            stdout = b"v24.10.0\n"
        else:
            stdout = b"11.6.1\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")


class FakeReleaseManager:
    def __init__(self, config=None) -> None:
        self.config = config
        self.verified: list[Path] = []
        self.activated: list[Path] = []

    def configuration_identity_for_runtime(self, release_path):
        assert self.config is not None
        return ReleaseManager(self.config.desktop).configuration_identity_for_runtime(release_path)

    def dependency_identity(self, frontend_root: Path):
        provenance = _profile_provenance()
        return {
            "python": {
                "executable": str(Path(bundle_module.sys.executable)),
                "executable_sha256": "1" * 64,
                "version": "3.12.14",
                "packages": _probe()["packages"],
                "runtime_profile": {
                    "profile": provenance["profile"],
                    "builder": provenance["builder"],
                    "base_python": provenance["base_python"],
                    "wheelhouse": {key: provenance["wheelhouse"][key] for key in ("manifest_sha256", "receipt_sha256", "artifact_count")},
                },
            },
            "node": {"executable": "node.exe", "executable_sha256": "2" * 64, "version": "v24.10.0", "npm_executable": "npm.cmd", "npm_executable_sha256": "3" * 64, "npm_version": "11.6.1"},
        }

    def verify(self, release):
        path = Path(release)
        self.verified.append(path)
        return json.loads((path / "release-manifest.json").read_text(encoding="utf-8"))

    def activate(self, release):
        path = Path(release)
        self.activated.append(path)
        return {"release_path": str(path)}

    def verify_and_activate(self, release):
        verified = self.verify(release)
        return verified, self.activate(release)

    def active_with_pointer(self):
        if not self.activated:
            raise DesktopOperationError("desktop_release_not_activated", "no release is active")
        path = self.activated[-1]
        manifest = self.verify(path)
        return path, manifest, {"release_path": str(path), "release_id": manifest["identity"]["release_id"]}


def _manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root, manifest, envelope, probe = _write_bundle(tmp_path)
    config = resolve_desktop_app_config(resource_root=root, data_root=tmp_path / "user-data", environ={})
    monkeypatch.setattr(bundle_module.sys, "executable", str(config.python_executable))
    release_manager = FakeReleaseManager(config)
    manager = PortableBundleManager(config, release_manager=release_manager, command_runner=FakeRunner(probe))
    return manager, release_manager, root, manifest, envelope


def test_verified_bundle_installs_only_controlled_binding_and_preserves_original_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, release_manager, root, original, _envelope = _manager(tmp_path, monkeypatch)
    payload_before = _inventory(root)
    result = manager.install_and_activate()
    assert _inventory(root) == payload_before
    assert not list(root.rglob("*.pyc"))
    destination = release_manager.activated[-1]
    local = json.loads((destination / "release-manifest.json").read_text(encoding="utf-8"))
    assert result["reused"] is False
    assert local["identity"]["release_id"] == original["identity"]["release_id"]
    assert local["identity"]["source_revision"] == original["identity"]["source_revision"]
    assert local["identity"]["source_digest"] == original["identity"]["source_digest"]
    assert local["resources"] == original["resources"]
    assert local["data_compatibility"] == original["data_compatibility"]
    assert local["config"]["canonical_origin"] == "http://127.0.0.1:3120"
    assert local["config"]["api_base_url"] == "http://127.0.0.1:8120"
    assert local["desktop_app_binding"]["original_identity"] == original["identity"]
    assert set(local["desktop_app_binding"]["relocated_fields"]) == {"release_path", "config.canonical_origin", "config.api_base_url", "entrypoints.api.host_port", "entrypoints.frontend.host_port", "dependencies.python.executable", "dependencies.node.executable", "dependencies.node.npm_executable"}


def test_binding_rejects_dependency_identity_drift_outside_executable_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, release_manager, _root, _manifest, _envelope = _manager(tmp_path, monkeypatch)
    original_dependency_identity = release_manager.dependency_identity

    def drifted(frontend_root: Path):
        value = original_dependency_identity(frontend_root)
        value["python"]["packages"] = [["fastapi", "9.9"]]
        return value

    release_manager.dependency_identity = drifted

    with pytest.raises(DesktopOperationError) as raised:
        manager.install_and_activate()

    assert raised.value.reason_code == "desktop_app_release_dependency_drift"


def test_binding_rejects_executable_hash_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, release_manager, _root, _manifest, _envelope = _manager(tmp_path, monkeypatch)
    original_dependency_identity = release_manager.dependency_identity

    def drifted(frontend_root: Path):
        value = original_dependency_identity(frontend_root)
        value["python"]["executable_sha256"] = "0" * 64
        return value

    release_manager.dependency_identity = drifted

    with pytest.raises(DesktopOperationError) as raised:
        manager.install_and_activate()

    assert raised.value.reason_code == "desktop_app_release_dependency_drift"


def test_binding_verifies_packaged_config_independently_of_disabled_runtime_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, release_manager, _root, original, _envelope = _manager(tmp_path, monkeypatch)
    for key in ("NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG", "NEWS_SOURCES_CONFIG"):
        manager.config.environment.pop(key, None)

    manager.install_and_activate()

    bound = json.loads((release_manager.activated[-1] / "release-manifest.json").read_text(encoding="utf-8"))
    assert bound["config"]["external_config_dependencies"] == []
    assert original["config"]["external_config_dependencies"]
    assert "NEWS_MODELS_CONFIG" not in manager.config.environment


def test_verify_bundle_uses_bounded_node_validation_and_hashes_each_file_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _release_manager, root, _manifest_value, _envelope = _manager(tmp_path, monkeypatch)
    expected_files = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    expected_directories = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}
    validated: list[str] = []
    hashed: list[str] = []
    original_validate = safe_tree_module._validate_node
    original_hash = safe_tree_module._hash_file

    def count_validate(path: Path, metadata, **kwargs):
        validated.append(path.relative_to(root).as_posix())
        return original_validate(path, metadata, **kwargs)

    def count_hash(path: Path, metadata, **kwargs):
        hashed.append(path.relative_to(root).as_posix())
        return original_hash(path, metadata, **kwargs)

    monkeypatch.setattr(safe_tree_module, "_validate_node", count_validate)
    monkeypatch.setattr(safe_tree_module, "_hash_file", count_hash)

    manager.verify_bundle()

    expected_validations = Counter({path: 2 for path in expected_files})
    expected_validations.update({path: 4 for path in expected_directories})
    expected_validations["."] = 4
    assert Counter(validated) == expected_validations
    assert Counter(hashed) == Counter({path: 1 for path in expected_files})


def test_verify_bundle_rejects_unmanifested_resource_after_single_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _release_manager, root, _manifest_value, _envelope = _manager(tmp_path, monkeypatch)
    (root / "unexpected.txt").write_text("drift", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify_bundle()

    assert raised.value.reason_code == "desktop_app_bundle_resource_inventory_mismatch"

def test_bundle_rejects_runtime_checksum_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _release_manager, root, _manifest, _envelope = _manager(tmp_path, monkeypatch)
    (root / "node/node.exe").write_bytes(b"unknown-binary")
    with pytest.raises(DesktopOperationError) as raised:
        manager.verify_bundle()
    assert raised.value.reason_code in {"desktop_app_bundle_checksum_mismatch", "desktop_app_bundle_executable_drift"}


def test_bundle_rejects_unknown_bundle_and_old_data_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _release_manager, root, _manifest, envelope = _manager(tmp_path, monkeypatch)
    envelope["schema"] = "newsroom.desktop-app-bundle/v0"
    (root / "desktop-bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(DesktopOperationError) as bundle_error:
        manager.verify_bundle()
    assert bundle_error.value.reason_code == "desktop_app_bundle_schema_unsupported"

    manager, _release_manager, root, manifest, envelope = _manager(tmp_path / "old", monkeypatch)
    manifest["data_compatibility"]["readable_schemas"] = {}
    original = root / ".desktop-app/original-release-manifest.json"
    original.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    envelope["release"]["manifest_sha256"] = sha256(original.read_bytes()).hexdigest()
    envelope["resources"] = _inventory(root)
    envelope["bundle_id"] = bundle_module._bundle_id(envelope["release"]["original_identity"], envelope["runtime"], envelope["resources"], envelope["relocation_policy"])
    (root / "desktop-bundle.json").write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    with pytest.raises(DesktopOperationError) as schema_error:
        manager.verify_bundle()
    assert schema_error.value.reason_code == "desktop_app_release_schema_incompatible"


def test_bundle_rejects_unsafe_resource_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _release_manager, root, _manifest, envelope = _manager(tmp_path, monkeypatch)
    envelope["resources"][0]["path"] = "../escape"
    (root / "desktop-bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(DesktopOperationError) as raised:
        manager.verify_bundle()
    assert raised.value.reason_code == "desktop_app_bundle_manifest_invalid"


def test_bundle_rejects_modified_relocation_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _release_manager, root, _manifest, envelope = _manager(tmp_path, monkeypatch)
    envelope["relocation_policy"]["allowed"].append("source_digest")
    (root / "desktop-bundle.json").write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify_bundle()

    assert raised.value.reason_code == "desktop_app_bundle_relocation_policy_invalid"


def test_bundle_rejects_external_config_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, _release_manager, root, manifest, envelope = _manager(tmp_path, monkeypatch)
    manifest["config"]["external_config_dependencies"] = [
        {"name": "NEWS_MODELS_CONFIG", "mode": "external", "path": "C:/outside/models.yaml"}
    ]
    original = root / ".desktop-app/original-release-manifest.json"
    original.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    envelope["release"]["manifest_sha256"] = sha256(original.read_bytes()).hexdigest()
    envelope["resources"] = _inventory(root)
    envelope["bundle_id"] = bundle_module._bundle_id(envelope["release"]["original_identity"], envelope["runtime"], envelope["resources"], envelope["relocation_policy"])
    (root / "desktop-bundle.json").write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify_bundle()

    assert raised.value.reason_code == "desktop_app_external_config_not_portable"


def test_existing_bound_release_with_modified_binding_is_not_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, release_manager, _root, _manifest, _envelope = _manager(tmp_path, monkeypatch)
    manager.install_and_activate()
    destination = release_manager.activated[-1]
    local_manifest = json.loads((destination / "release-manifest.json").read_text(encoding="utf-8"))
    local_manifest["desktop_app_binding"]["binding_digest"] = "0" * 64
    (destination / "release-manifest.json").write_text(json.dumps(local_manifest), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.install_and_activate()

    assert raised.value.reason_code == "desktop_app_release_binding_conflict"

def test_staging_rejects_absolute_pyvenv_binding_before_copy(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    manifest = {"schema": RELEASE_SCHEMA, "identity": {"release_id": "release", "source_revision": "a" * 40, "source_digest": "b" * 64, "dependency_digest": "c" * 64, "config_digest": "d" * 64}, "config": {"external_config_dependencies": []}, "dependencies": {"python": {"version": "3.12.7", "packages": []}, "node": {"version": "v24.10.0", "npm_version": "11.6.1"}}, "data_compatibility": {"readable_schemas": DATA_SCHEMAS, "writable_schemas": DATA_SCHEMAS}, "resources": [{"path": "app/interfaces/__init__.py", "size": 0, "sha256": sha256(b"").hexdigest()}]}
    python_root = tmp_path / "python"
    python_root.mkdir()
    (python_root / "portable-python.json").write_text(json.dumps({"schema": "newsroom.portable-python/v1", "portable": True, "executable": "python.exe"}), encoding="utf-8")
    (python_root / "pyvenv.cfg").write_text("home = C:\\Python312\n", encoding="utf-8")
    node_root = tmp_path / "node"
    node_root.mkdir()
    (node_root / "portable-node.json").write_text(json.dumps({"schema": "newsroom.portable-node/v1", "portable": True, "executable": "node.exe", "npm_executable": "npm.cmd"}), encoding="utf-8")

    class SourceManager:
        def verify(self, source):
            return manifest

        def resolve_release(self, source):
            return release

    with pytest.raises(DesktopOperationError) as raised:
        stage_portable_bundle(output_root=tmp_path / "output", source_release=release, python_root=python_root, node_root=node_root, release_manager=SourceManager())
    assert raised.value.reason_code == "desktop_app_nonportable_python_rejected"


def test_staging_uses_probed_portable_identity_and_excludes_only_duplicate_pip_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_view = tmp_path / "source-view"
    for relative, data in {
        "interfaces/__init__.py": b"",
        "configs/models.yaml": b"providers: {}\n",
        "configs/sources.yaml": b"sources: {}\n",
        "frontend/package.json": b"{}",
        "frontend/package-lock.json": b"{}",
        "frontend/node_modules/.package-lock.json": b"{}",
        "frontend/node_modules/next/dist/bin/next": b"next",
    }.items():
        target = source_view / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    source_manifest = _manifest(source_view)
    source_release = tmp_path / "source-release"
    for item in source_manifest["resources"]:
        release_relative = Path(str(item["path"]))
        view_relative = Path(*release_relative.parts[1:]) if release_relative.parts[0] == "app" else release_relative
        target = source_release / release_relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source_view / view_relative).read_bytes())

    python_probe = {
        "version": "3.12.14",
        "implementation": "CPython",
        "cache_tag": "cpython-312",
        "abi": "",
        "platform": "win-amd64",
        "packages": [[name, version] for name, version in bundle_module._DEFAULT_RUNTIME_PROFILE.packages],
        "sqlite_version": "3.53.1",
        "openssl_version": "OpenSSL 3.5.8",
        "capabilities": {
            "api_routes": 42, "api_health": True, "text_pdf": True, "sqlite_wal": True,
            "auth": True, "credential_acl": True, "tls": True, "optional_modules_loaded": [],
        },
    }
    python_root = tmp_path / "portable-python"
    (python_root / "Lib/site-packages/pip-26.2.1.dist-info").mkdir(parents=True)
    (python_root / "Lib/site-packages/pip-26.2.1.dist-info/METADATA").write_text("Name: pip\nVersion: 26.2.1\n", encoding="utf-8")
    (python_root / "Lib/site-packages/pip-25.0.dist-info").mkdir(parents=True)
    (python_root / "Lib/site-packages/pip-25.0.dist-info/METADATA").write_text("Name: pip\nVersion: 25.0\n", encoding="utf-8")
    (python_root / "python.exe").write_bytes(b"portable-python-3.12.14")
    (python_root / "portable-python.json").write_text(
        json.dumps(
            {
                "schema": "newsroom.portable-python/v1",
                "portable": True,
                "executable": "python.exe",
                "executable_sha256": sha256((python_root / "python.exe").read_bytes()).hexdigest(),
                **{key: python_probe[key] for key in ("version", "implementation", "cache_tag", "abi", "platform")},
                **_profile_provenance(),
                "packages": python_probe["packages"],
            }
        ),
        encoding="utf-8",
    )
    node_root = tmp_path / "portable-node"
    (node_root / "node_modules/npm/bin").mkdir(parents=True)
    (node_root / "node.exe").write_bytes(b"portable-node-24.21.0")
    (node_root / "npm.cmd").write_bytes(b"portable-npm")
    (node_root / "node_modules/npm/bin/npm-cli.js").write_bytes(b"npm-cli")
    (node_root / "portable-node.json").write_text(
        json.dumps(
            {
                "schema": "newsroom.portable-node/v1",
                "portable": True,
                "executable": "node.exe",
                "npm_executable": "npm.cmd",
                "version": "v24.21.0",
                "npm_version": "11.6.2",
            }
        ),
        encoding="utf-8",
    )

    class StagingReleaseManager:
        def verify(self, source):
            assert Path(source) == source_release
            return source_manifest

        def resolve_release(self, source):
            assert Path(source) == source_release
            return source_release

        def dependency_identity_for_runtime(self, frontend_root: Path, **runtime):
            assert frontend_root.name == "frontend"
            assert runtime["application_root"] == frontend_root.parent
            assert "scripts.desktop_app" in runtime["required_python_imports"]
            return {
                "python": {
                    "executable": str(runtime["python_executable"]),
                    "executable_sha256": sha256(runtime["python_executable"].read_bytes()).hexdigest(),
                    "version": python_probe["version"],
                    "packages": python_probe["packages"],
                    "runtime_profile": {
                        "profile": bundle_module._DEFAULT_RUNTIME_PROFILE.identity(),
                        "builder": _profile_provenance()["builder"],
                        "base_python": {"source_sha256": "b" * 64, "manifest_sha256": "c" * 64},
                        "wheelhouse": {
                            "manifest_sha256": "d" * 64,
                            "receipt_sha256": _profile_provenance()["wheelhouse"]["receipt_sha256"],
                            "artifact_count": len(bundle_module._DEFAULT_RUNTIME_PROFILE.packages),
                        },
                    },
                },
                "node": {
                    "executable": str(runtime["node_executable"]),
                    "executable_sha256": sha256(runtime["node_executable"].read_bytes()).hexdigest(),
                    "version": "v24.21.0",
                    "npm_executable": str(runtime["npm_executable"]),
                    "npm_executable_sha256": sha256(runtime["npm_executable"].read_bytes()).hexdigest(),
                    "npm_version": "11.6.2",
                    "package_lock_sha256": sha256((frontend_root / "package-lock.json").read_bytes()).hexdigest(),
                    "installed_lock_sha256": sha256((frontend_root / "node_modules/.package-lock.json").read_bytes()).hexdigest(),
                    "installed_tree": {"next": "test"},
                },
            }

    class IsolatedRunner:
        def __init__(self) -> None:
            self.environments: list[dict[str, str]] = []
            self.timeouts: list[float] = []
            self.argv: list[list[str]] = []

        def __call__(self, argv, **kwargs):
            self.argv.append(list(argv))
            self.environments.append(dict(kwargs["env"]))
            self.timeouts.append(kwargs["timeout"])
            if str(argv[0]).endswith("python.exe"):
                stdout = json.dumps(python_probe, sort_keys=True, separators=(",", ":")).encode()
            elif len(argv) == 2:
                stdout = b"v24.21.0\n"
            else:
                stdout = b"11.6.2\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    monkeypatch.setenv("PYTHONHOME", r"C:\unsafe-python")
    monkeypatch.setenv("PYTHONPATH", r"C:\unsafe-modules")
    runner = IsolatedRunner()
    output = tmp_path / "bundle"
    envelope = stage_portable_bundle(
        output_root=output,
        source_release=source_release,
        python_root=python_root,
        node_root=node_root,
        release_manager=StagingReleaseManager(),
        command_runner=runner,
    )

    portable_manifest = json.loads((output / ".desktop-app/original-release-manifest.json").read_text(encoding="utf-8"))
    assert envelope["release"]["source_release_identity"] == source_manifest["identity"]
    assert envelope["release"]["original_identity"] == portable_manifest["identity"]
    assert portable_manifest["identity"]["release_id"] != source_manifest["identity"]["release_id"]
    assert portable_manifest["identity"]["source_digest"] == source_manifest["identity"]["source_digest"]
    assert portable_manifest["dependencies"]["python"]["version"] == "3.12.14"
    assert portable_manifest["dependencies"]["python"]["packages"] == python_probe["packages"]
    assert envelope["runtime"]["python"]["distribution_count"] == len(python_probe["packages"])
    assert set(envelope["runtime"]["python"]["required_imports"]) == set(bundle_module._DEFAULT_IMPORTS)
    assert not {"numpy", "scipy", "torch", "sklearn", "transformers", "sentence_transformers"}.intersection(
        envelope["runtime"]["python"]["required_imports"]
    )
    assert envelope["runtime"]["python"]["required_attributes"] == [
        {"module": "scripts.local_desktop.common", "attribute": "protect_private_path"}
    ]
    assert envelope["runtime"]["python"]["sqlite_version"] == "3.53.1"
    assert envelope["runtime"]["python"]["openssl_version"] == "OpenSSL 3.5.8"
    assert envelope["runtime"]["python"]["excluded_input_paths"] == ["Lib/site-packages/pip-26.2.1.dist-info"]
    assert envelope["runtime"]["python"]["launch_requirements"] == {
        "arguments": ["-B", "-s"],
        "environment": {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
    }
    assert not (output / ".venv/Scripts/Lib/site-packages/pip-26.2.1.dist-info").exists()
    assert (output / ".venv/Scripts/Lib/site-packages/pip-25.0.dist-info/METADATA").is_file()
    assert runner.environments
    assert runner.timeouts == [90, 15, 20]
    assert runner.argv[0][1:4] == ["-B", "-I", "-c"]
    assert all(environment.get("PYTHONNOUSERSITE") == "1" for environment in runner.environments)
    assert all("PYTHONHOME" not in environment and "PYTHONPATH" not in environment for environment in runner.environments)


def test_portable_python_attestation_rejects_executable_hash_drift(tmp_path: Path) -> None:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"managed-python")

    with pytest.raises(DesktopOperationError) as raised:
        bundle_module._require_attested_executable_hash(
            {"executable_sha256": "0" * 64},
            executable,
            runtime="Python",
        )

    assert raised.value.reason_code == "desktop_app_portable_runtime_attestation_mismatch"


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_bundle_rejects_inward_junction_resource_before_checksum_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _release_manager, root, _manifest_value, _envelope = _manager(tmp_path, monkeypatch)
    target = root / "configs-real"
    (root / "configs").rename(target)
    junction = root / "configs"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")
    try:
        with pytest.raises(DesktopOperationError) as raised:
            manager.verify_bundle()
        assert raised.value.reason_code == "desktop_reparse_point_rejected"
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)


def _long_relative_resource() -> Path:
    return Path("packages") / Path(*(("segment-" + "x" * 32,) * 7)) / "module.py"


def test_verify_bundle_checks_long_path_resource_without_ordinary_path_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "long-verify"
    relative = _long_relative_resource()
    resource = root / relative
    bundle_module.windows_extended_path(resource.parent).mkdir(parents=True)
    payload = b"verified long path payload\n"
    with open(bundle_module.windows_extended_path(resource), "wb") as handle:
        handle.write(payload)
    envelope = {
        "schema": BUNDLE_SCHEMA,
        "bundle_id": "long-path-bundle",
        "release": {"original_identity": {}},
        "runtime": {},
        "relocation_policy": {},
        "resources": [
            {"path": relative.as_posix(), "size": len(payload), "sha256": sha256(payload).hexdigest()}
        ],
    }
    (root / "desktop-bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    manager = PortableBundleManager(SimpleNamespace(resource_root=root), release_manager=FakeReleaseManager())
    monkeypatch.setattr(manager, "_original_manifest", lambda _envelope, **_kwargs: {})
    monkeypatch.setattr(manager, "_verify_runtime", lambda _envelope, _original, **_kwargs: None)
    monkeypatch.setattr(bundle_module, "_validate_release_compatibility", lambda _manifest: None)
    monkeypatch.setattr(bundle_module, "_reject_external_release_config", lambda _manifest: None)
    monkeypatch.setattr(bundle_module, "_validate_relocation_policy", lambda _envelope: None)
    monkeypatch.setattr(bundle_module, "_bundle_id", lambda *_args: "long-path-bundle")

    verified = manager.verify_bundle()

    assert verified["bundle_id"] == "long-path-bundle"
    assert len(str(resource.resolve())) > 260


def test_materialize_release_uses_long_path_safe_source_and_destination_io(tmp_path: Path) -> None:
    root = tmp_path / "long-materialize-source"
    relative = _long_relative_resource()
    source = root / relative
    bundle_module.windows_extended_path(source.parent).mkdir(parents=True)
    payload = b"materialized long path payload\n"
    with open(bundle_module.windows_extended_path(source), "wb") as handle:
        handle.write(payload)
    original_relative = (Path("app") / relative).as_posix()
    original = {
        "resources": [
            {"path": original_relative, "size": len(payload), "sha256": sha256(payload).hexdigest()}
        ]
    }
    manager = PortableBundleManager(SimpleNamespace(resource_root=root), release_manager=FakeReleaseManager())
    destination = tmp_path / "long-materialize-destination"

    manager._materialize_release(destination, original)

    target = destination / original_relative
    assert bundle_module.read_bytes_long_path(target) == payload
    assert len(str(target.resolve())) > 260

def test_copy_tree_checked_preserves_long_nested_file_and_inventory(tmp_path: Path) -> None:
    source = tmp_path / "portable-python"
    nested = source / "Lib/site-packages/torch-2.13.0.dist-info/licenses/third_party/dynolog/third_party/libkineto/third_party/dynolog/third_party/libs_3rdparty/civetweb/third_party/googletest/googlemock/test"
    bundle_module.windows_extended_path(nested).mkdir(parents=True)
    license_file = nested / "LICENSE.txt"
    payload = b"portable runtime license\n"
    with open(bundle_module.windows_extended_path(license_file), "wb") as handle:
        handle.write(payload)
    destination = tmp_path / ("staged-" + "x" * 80) / "runtime"

    applied = bundle_module._copy_tree_checked(source, destination)
    inventory = bundle_module._resource_entries(destination, excluded=set())

    relative = license_file.relative_to(source).as_posix()
    assert applied == ()
    assert len(str(license_file.resolve())) > 260
    assert inventory == [{"path": relative, "size": len(payload), "sha256": sha256(payload).hexdigest()}]


def test_packaged_reopen_preserves_active_runtime_config_rebind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager, release_manager, _root, _manifest, _envelope = _manager(tmp_path, monkeypatch)
    first = manager.install_and_activate()
    base_path = release_manager.activated[-1]
    base_manifest = json.loads((base_path / "release-manifest.json").read_text(encoding="utf-8"))
    binding = dict(base_manifest["desktop_app_binding"])
    rebound_path = base_path.parent / (base_path.name + "-config-user")
    rebound_path.mkdir()
    rebound_manifest = dict(base_manifest)
    rebound_manifest["identity"] = dict(base_manifest["identity"], release_id=base_manifest["identity"]["release_id"] + "-config-user")
    rebound_manifest["desktop_runtime_configuration"] = {
        "schema": "newsroom.desktop-runtime-settings/v1",
        "revision": 2,
        "digest": "a" * 64,
        "source_release_id": base_manifest["identity"]["release_id"],
    }
    (rebound_path / "release-manifest.json").write_text(json.dumps(rebound_manifest), encoding="utf-8")

    def active_with_pointer():
        verified = release_manager.verify(rebound_path)
        pointer = {"release_path": str(rebound_path), "release_id": verified["identity"]["release_id"]}
        return rebound_path, verified, pointer

    release_manager.active_with_pointer = active_with_pointer
    original_verify = release_manager.verify

    def verify_active_before_base(release):
        selected = Path(release)
        if selected == base_path:
            raise AssertionError("configured active rebind must be selected before verifying the initial base")
        return original_verify(selected)

    release_manager.verify = verify_active_before_base
    second = manager.install_and_activate()

    assert first["preserved_runtime_configuration"] if "preserved_runtime_configuration" in first else True
    assert second["preserved_runtime_configuration"] is True
    assert second["release"]["release_id"].endswith("-config-user")
    assert second["activation"]["release_path"] == str(rebound_path)
    assert release_manager.verified[-1] == rebound_path
    assert second["binding"] == binding


@pytest.mark.parametrize("persisted_null", [False, True])
def test_first_binding_and_reopen_use_real_release_configuration_and_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persisted_null: bool
) -> None:
    import scripts.local_desktop.release as release_module
    from scripts.desktop_app.config import controlled_process_environment
    from scripts.local_desktop.common import atomic_write_json
    from scripts.local_desktop.runtime_config import (
        RuntimeConfigManager,
        candidate_document,
        runtime_config_paths_for_data_root,
    )

    root, original, envelope, probe = _write_bundle(tmp_path)
    data = tmp_path / "real-owner-data"
    if persisted_null:
        atomic_write_json(
            runtime_config_paths_for_data_root(data).document,
            candidate_document({"llm": None}, revision=2),
        )
    original["identity"]["source_digest"] = release_module._source_input_digest(root)
    original["identity"]["config_digest"] = _digest(original["config"])
    original_path = root / ".desktop-app/original-release-manifest.json"
    original_path.write_text(json.dumps(original, sort_keys=True), encoding="utf-8")
    envelope["release"]["original_identity"] = original["identity"]
    envelope["release"]["manifest_sha256"] = sha256(original_path.read_bytes()).hexdigest()
    envelope["resources"] = _inventory(root)
    envelope["bundle_id"] = bundle_module._bundle_id(
        original["identity"], envelope["runtime"], envelope["resources"], envelope["relocation_policy"]
    )
    (root / "desktop-bundle.json").write_text(json.dumps(envelope), encoding="utf-8")
    config = resolve_desktop_app_config(resource_root=root, data_root=data, environ={})
    monkeypatch.setattr(bundle_module.sys, "executable", str(config.python_executable))
    # Small portable dependency probe fixture only. Source/config/resource
    # identities, manifest verification, activation and rebind are real owners.
    class FixtureDependencyReleaseManager(ReleaseManager):
        def dependency_identity(self, frontend_root=None, *, verified_resources=None):
            identity = json.loads(json.dumps(original["dependencies"]))
            identity["python"]["executable"] = str(config.python_executable)
            identity["node"]["executable"] = str(config.node_executable)
            identity["node"]["npm_executable"] = str(config.npm_executable)
            return identity

    owner = FixtureDependencyReleaseManager(config.desktop)
    manager = PortableBundleManager(config, release_manager=owner, command_runner=FakeRunner(probe))
    payload_before = _inventory(root)
    result = manager.install_and_activate()
    base = Path(result["activation"]["release_path"])
    with controlled_process_environment(config):
        bound = owner.verify(base)
        calculated = owner.configuration_identity_for_runtime(base)
    assert bound["config"] == calculated
    assert bound["identity"]["config_digest"] == _digest(calculated)
    assert [item["name"] for item in original["config"]["external_config_dependencies"]] == [
        "NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG", "NEWS_SOURCES_CONFIG"
    ]
    assert [item["name"] for item in calculated["external_config_dependencies"]] == ["NEWS_SOURCES_CONFIG"]
    assert config.environment["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "0"

    settings_owner = RuntimeConfigManager(config, release_manager=owner)
    candidate = {"llm": {"model": "configured-model", "base_url": "https://synthetic.example/v1"}}
    preview = settings_owner.preview(candidate)
    with controlled_process_environment(config):
        applied = settings_owner.apply(
            candidate,
            expected_revision=2 if persisted_null else 0,
            confirmation_token=preview.details["confirmation_token"],
        )
    configured = resolve_desktop_app_config(resource_root=root, data_root=data, environ=config.environment)
    owner.config = configured.desktop
    manager.config = configured
    configured_reopen = manager.install_and_activate()
    assert configured_reopen["preserved_runtime_configuration"] is True
    assert configured_reopen["release"]["release_id"] == applied.details["release_id"]
    with controlled_process_environment(configured):
        active, active_manifest = owner.active()
        assert active_manifest["config"] == owner.configuration_identity_for_runtime(active)
    assert configured.environment["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "1"

    settings_owner.config = configured
    disabled_candidate = {"llm": None}
    disabled_preview = settings_owner.preview(disabled_candidate)
    with controlled_process_environment(configured):
        disabled_result = settings_owner.apply(
            disabled_candidate,
            expected_revision=3 if persisted_null else 1,
            confirmation_token=disabled_preview.details["confirmation_token"],
        )
    disabled = resolve_desktop_app_config(resource_root=root, data_root=data, environ=configured.environment)
    owner.config = disabled.desktop
    manager.config = disabled
    disabled_reopen = manager.install_and_activate()
    assert disabled_reopen["preserved_runtime_configuration"] is True
    assert disabled_reopen["release"]["release_id"] == disabled_result.details["release_id"]
    assert disabled.environment["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "0"
    assert "NEWS_MODELS_CONFIG" not in disabled.environment
    with controlled_process_environment(disabled):
        active, active_manifest = owner.active()
        assert active_manifest["config"] == owner.configuration_identity_for_runtime(active)
        assert active_manifest["config"]["runtime_configuration"]["research"]["llm"] is None
    assert _inventory(root) == payload_before



def _write_fixture_wheel(path: Path, *, name: str, version: str, requires_dist: tuple[str, ...] = ()) -> None:
    dist_info = f"{name.replace('-', '_')}-{version}.dist-info"
    requirements = "".join(f"Requires-Dist: {requirement}\n" for requirement in requires_dist)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\nLicense: MIT\n{requirements}",
        )
        archive.writestr(f"{dist_info}/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")


def test_desktop_runtime_profile_is_fixed_to_verified_python_and_explicit_optional_boundary() -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()

    assert profile.constraints["python"] == {
        "implementation": "CPython",
        "version": "3.12.14",
        "cache_tag": "cpython-312",
        "abi": "",
        "platform": "win-amd64",
    }
    assert {"psycopg", "psycopg-binary", "psycopg-pool"}.issubset({name for name, _version in profile.packages})
    assert {"numpy", "scipy", "torch", "sklearn", "transformers", "sentence_transformers"} == set(profile.forbidden_default_modules)
    assert {"numpy", "scipy", "torch", "scikit-learn", "transformers", "sentence-transformers"} == set(
        profile.excluded_default_distributions
    )
    assert profile.identity()["profile_sha256"] == sha256(profile.path.read_bytes()).hexdigest()
    assert profile.identity()["constraints_sha256"] == sha256(profile.constraints_path.read_bytes()).hexdigest()
    assert profile.identity()["requirements_sha256"] == sha256(profile.requirements_path.read_bytes()).hexdigest()


def test_wheelhouse_receipt_hashes_every_exact_constrained_artifact(tmp_path: Path) -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for index, (name, version) in enumerate(profile.packages):
        requirements = ("attrs==26.1.0",) if index == 0 else ()
        _write_fixture_wheel(
            wheelhouse / f"artifact-{index}.whl",
            name=name,
            version=version,
            requires_dist=requirements,
        )

    receipt = runtime_profile_module.create_wheelhouse_receipt(wheelhouse_root=wheelhouse)

    assert receipt["profile"] == profile.identity()
    assert receipt["artifacts_sha256"] == _digest({"artifacts": receipt["artifacts"]})
    assert len(receipt["artifacts"]) == len(profile.packages)
    assert all(len(item["sha256"]) == 64 for item in receipt["artifacts"])
    assert any(item["requires_dist"] == ["attrs==26.1.0"] for item in receipt["artifacts"])
    persisted = json.loads((wheelhouse / "desktop-wheelhouse.json").read_text(encoding="utf-8"))
    assert persisted == receipt


def test_wheelhouse_receipt_rejects_hash_drift_before_runtime_install(tmp_path: Path) -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for index, (name, version) in enumerate(profile.packages):
        _write_fixture_wheel(wheelhouse / f"artifact-{index}.whl", name=name, version=version)
    receipt = runtime_profile_module.create_wheelhouse_receipt(wheelhouse_root=wheelhouse)
    selected = wheelhouse / receipt["artifacts"][0]["file"]
    selected.write_bytes(b"tampered")

    with pytest.raises(DesktopOperationError) as raised:
        runtime_profile_module._validate_wheelhouse(profile, wheelhouse, receipt)

    assert raised.value.reason_code == "desktop_wheelhouse_artifact_mismatch"


def test_prepare_base_python_excludes_old_site_packages_and_scripts_but_preserves_runtime_and_license(
    tmp_path: Path,
) -> None:
    source = tmp_path / "portable-source"
    for relative, data in {
        "python.exe": b"python-build-standalone",
        "python312.dll": b"python-dll",
        "Lib/os.py": b"stdlib",
        "Lib/site-packages/torch/__init__.py": b"large-dev-package",
        "Scripts/pip.exe": b"pip",
        "LICENSE.txt": b"python license",
    }.items():
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    source_receipt = {
        "schema": "newsroom.portable-python/v1",
        "portable": True,
        "executable": "python.exe",
        "executable_sha256": sha256((source / "python.exe").read_bytes()).hexdigest(),
    }
    (source / "portable-python.json").write_text(json.dumps(source_receipt), encoding="utf-8")
    profile = runtime_profile_module.load_desktop_runtime_profile()
    expected_python = profile.constraints["python"]

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(expected_python).encode("utf-8"), b"")

    output = tmp_path / "base-python"
    manifest = runtime_profile_module.prepare_base_python(
        source_root=source,
        output_root=output,
        command_runner=runner,
    )

    assert manifest["schema"] == "newsroom.desktop-base-python/v1"
    assert manifest["source_kind"] == "python-build-standalone-portable-runtime"
    assert manifest["excluded_source_paths"] == ["Lib/site-packages", "Scripts", "portable-python.json"]
    assert (output / "python.exe").is_file()
    assert (output / "python312.dll").is_file()
    assert (output / "Lib/os.py").is_file()
    assert (output / "LICENSE.txt").is_file()
    assert not (output / "Lib/site-packages").exists()
    assert not (output / "Scripts").exists()
    expected_resources = runtime_profile_module._regular_file_inventory(output, excluded={"base-python.json"})
    assert runtime_profile_module._resource_map(manifest["resources"], reason_code="test") == expected_resources


def test_portable_attestation_requires_profile_base_and_hashed_wheel_receipt() -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()
    artifacts = [
        {"name": name, "version": version, "file": f"{name}-{version}.whl", "sha256": "a" * 64, "requires_dist": []}
        for name, version in profile.packages
    ]
    attestation = {
        "schema": "newsroom.portable-python/v1",
        "portable": True,
        "executable": "python.exe",
        "executable_sha256": "e" * 64,
        **dict(profile.constraints["python"]),
        "profile": profile.identity(),
        "builder": {
            "implementation": "CPython",
            "version": "3.12.7",
            "cache_tag": "cpython-312",
            "platform": "win-amd64",
            "executable_sha256": "f" * 64,
            "pip_version": "25.2",
        },
        "base_python": {"source_sha256": "b" * 64, "manifest_sha256": "c" * 64},
        "wheelhouse": {
            "manifest_sha256": "d" * 64,
            "receipt_sha256": _digest({"artifacts": artifacts}),
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        },
        "packages": [[name, version] for name, version in profile.packages],
        "record_count": len(profile.packages),
        "licenses": [{"name": name, "version": version} for name, version in profile.packages],
    }

    runtime_profile_module.validate_portable_runtime_attestation(attestation, profile)
    attestation["wheelhouse"]["receipt_sha256"] = "0" * 64
    with pytest.raises(DesktopOperationError) as raised:
        runtime_profile_module.validate_portable_runtime_attestation(attestation, profile)
    assert raised.value.reason_code == "desktop_app_portable_runtime_attestation_invalid"


def test_build_installer_is_explicit_cp312_windows_tooling_and_not_the_runtime_base(tmp_path: Path) -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()
    installer = tmp_path / "builder-python.exe"
    installer.write_bytes(b"controlled-builder")
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        if argv[-1] == "--version":
            stdout = b"pip 25.2 from controlled-site-packages (python 3.12)\n"
        else:
            stdout = json.dumps(
                {
                    "version": "3.12.7",
                    "implementation": "CPython",
                    "cache_tag": "cpython-312",
                    "platform": "win-amd64",
                }
            ).encode("utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    identity = runtime_profile_module._build_installer_identity(installer, profile, runner)

    assert identity["version"] == "3.12.7"
    assert identity["pip_version"] == "25.2"
    assert identity["executable_sha256"] == sha256(installer.read_bytes()).hexdigest()
    assert calls[0][0] == str(installer.absolute())
    assert calls[1][1:] == ["-I", "-m", "pip", "--version"]


def test_runtime_profile_build_command_requires_explicit_installer_python() -> None:
    parser = runtime_profile_module._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "build",
                "--base-python-root", "base",
                "--wheelhouse-root", "wheels",
                "--output-root", "runtime",
            ]
        )

    parsed = parser.parse_args(
        [
            "build",
            "--base-python-root", "base",
            "--wheelhouse-root", "wheels",
            "--output-root", "runtime",
            "--installer-python", r"E:\Anaconda3\python.exe",
        ]
    )
    assert parsed.installer_python == r"E:\Anaconda3\python.exe"


def test_wheelhouse_receipt_rejects_unreceipted_input_file(tmp_path: Path) -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    for index, (name, version) in enumerate(profile.packages):
        _write_fixture_wheel(wheelhouse / f"artifact-{index}.whl", name=name, version=version)
    (wheelhouse / "unexpected.txt").write_text("not a wheel", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        runtime_profile_module.create_wheelhouse_receipt(wheelhouse_root=wheelhouse)

    assert raised.value.reason_code == "desktop_wheelhouse_manifest_invalid"


def test_wheel_metadata_dependency_closure_rejects_active_requirement_outside_profile() -> None:
    profile = runtime_profile_module.load_desktop_runtime_profile()
    receipt = [
        {
            "name": name,
            "version": version,
            "file": f"{name}-{version}.whl",
            "sha256": "a" * 64,
            "requires_dist": ["not-in-desktop-profile>=1"] if index == 0 else [],
        }
        for index, (name, version) in enumerate(profile.packages)
    ]

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 1, b'{"invalid":["not-in-desktop-profile>=1"]}', b"")

    with pytest.raises(DesktopOperationError) as raised:
        runtime_profile_module._validate_wheel_dependency_closure(
            profile,
            receipt,
            {"executable": r"E:\Anaconda3\python.exe"},
            runner,
        )

    assert raised.value.reason_code == "desktop_wheelhouse_dependency_closure_invalid"
    assert raised.value.details == {"returncode": 1, "invalid": ["not-in-desktop-profile>=1"]}
