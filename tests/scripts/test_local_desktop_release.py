from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.config import sanitized_environment
from scripts.local_desktop.release import CONFIG_SCHEMA, DATA_SCHEMAS, RELEASE_SCHEMA, ReleaseManager, _external_config_identity, _json_digest, _source_input_digest


def _manager(
    tmp_path: Path,
    monkeypatch,
    *,
    external_environment: dict[str, str] | None = None,
) -> tuple[ReleaseManager, Path]:
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_bytes(b"python")
    config = resolve_local_desktop_config(project_root=tmp_path)
    manager = ReleaseManager(config)
    isolated_environment = dict(external_environment or {})
    monkeypatch.setattr(
        "scripts.local_desktop.release.sanitized_environment",
        lambda actual_config: sanitized_environment(
            actual_config,
            base=isolated_environment,
        ),
    )
    monkeypatch.setattr(manager, "_dependency_identity", lambda _root=None: {"python": {"version": "3.12"}, "node": {"version": "v22"}})
    release = config.release_root / "release-one"
    (release / "app" / "interfaces").mkdir(parents=True)
    (release / "app" / "interfaces" / "__init__.py").write_text("", encoding="utf-8")
    (release / "frontend" / "node_modules" / "next" / "dist" / "bin").mkdir(parents=True)
    (release / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next").write_text("runtime", encoding="utf-8")
    resources = []
    for path in sorted(release.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            resources.append({"path": path.relative_to(release).as_posix(), "size": len(data), "sha256": sha256(data).hexdigest()})
    dependencies = manager._dependency_identity()
    config_identity = {
        "schema": CONFIG_SCHEMA,
        "profile": config.deployment_profile,
        "canonical_origin": config.canonical_origin,
        "api_base_url": config.api_base_url,
        "parser_mode": config.parser_mode,
        "data_mode": "real",
        "external_config_dependencies": [],
    }
    manifest = {
        "schema": RELEASE_SCHEMA,
        "identity": {
            "release_id": "release-one",
            "source_revision": "a" * 40,
            "source_digest": _source_input_digest(release),
            "dependency_digest": _json_digest(dependencies),
            "config_digest": _json_digest(config_identity),
        },
        "config": config_identity,
        "entrypoints": {
            "api": ["-m", "interfaces.cli.news", "api", "serve", "--host", "127.0.0.1", "--port", "8000"],
            "frontend": ["next", "start", "-H", "127.0.0.1", "-p", "3000"],
        },
        "data_compatibility": {"readable_schemas": DATA_SCHEMAS, "writable_schemas": DATA_SCHEMAS},
        "resources": resources,
    }
    (release / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manager, release


def test_release_verify_rejects_unmanifested_file(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    (release / "unexpected.txt").write_text("drift", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code == "desktop_release_resource_inventory_mismatch"


def test_release_verify_rejects_packaged_source_identity_drift(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["identity"]["source_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code == "desktop_release_source_drift"


def test_release_verify_rejects_checksum_drift(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    (release / "app" / "interfaces" / "__init__.py").write_text("changed", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code == "desktop_release_checksum_mismatch"


def test_release_verify_requires_both_production_entrypoints(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entrypoints"].pop("frontend")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code == "desktop_release_manifest_invalid"


def test_activate_keeps_previous_pointer_when_target_is_invalid(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    manager.activate(release)
    previous = manager.active_pointer.read_bytes()
    invalid = manager.config.release_root / "invalid"
    invalid.mkdir()

    with pytest.raises(DesktopOperationError):
        manager.activate(invalid)
    assert manager.active_pointer.read_bytes() == previous



def test_release_copy_excludes_all_environment_files_and_development_outputs(tmp_path: Path) -> None:
    source_root = tmp_path / "source-root"
    source = source_root / "frontend"
    destination_root = tmp_path / "release-copy"
    destination = destination_root / "frontend"
    (source / ".next" / "cache").mkdir(parents=True)
    (source / "test-results").mkdir()
    (source / "src").mkdir()
    (source / ".env.local").write_text("PROVIDER_TOKEN=secret", encoding="utf-8")
    (source / ".env.production").write_text("COOKIE_SECRET=secret", encoding="utf-8")
    (source / ".next" / "cache" / "trace").write_text("developer-cache", encoding="utf-8")
    (source / "test-results" / "browser.json").write_text("{}", encoding="utf-8")
    (source / "src" / "page.tsx").write_text("export default function Page() {}", encoding="utf-8")

    ReleaseManager._copy_tree(source, destination)

    assert (destination / "src" / "page.tsx").is_file()
    assert not (destination / ".env.local").exists()
    assert not (destination / ".env.production").exists()
    assert not (destination / ".next").exists()
    assert not (destination / "test-results").exists()
    assert _source_input_digest(source_root) == _source_input_digest(destination_root)


def test_release_copy_rejects_source_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "private.env"
    outside.write_text("TOKEN=secret", encoding="utf-8")
    link = source / "linked-config"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("test environment cannot create symlinks")

    with pytest.raises(DesktopOperationError) as raised:
        ReleaseManager._copy_tree(source, tmp_path / "destination")
    assert raised.value.reason_code in {"desktop_path_escape", "desktop_reparse_point_rejected"}


@pytest.mark.parametrize("config_key", ["NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG"])
def test_release_runtime_config_identity_packages_project_config_and_binds_release_copy(tmp_path: Path, monkeypatch, config_key: str) -> None:
    project_root = tmp_path / "project"
    source = project_root / "configs" / "models.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("routes: {}", encoding="utf-8")
    release = tmp_path / "release"
    packaged = release / "app" / "configs" / "models.yaml"
    packaged.parent.mkdir(parents=True)
    packaged.write_bytes(source.read_bytes())
    environment = {config_key: str(source)}

    dependencies = _external_config_identity(environment, project_root=project_root, release_path=release)
    source.unlink()
    assert _external_config_identity(environment, project_root=project_root, release_path=release) == dependencies

    assert dependencies == [{
        "name": config_key,
        "mode": "packaged",
        "resource": "app/configs/models.yaml",
        "size": packaged.stat().st_size,
        "sha256": sha256(packaged.read_bytes()).hexdigest(),
    }]
    manager, _prepared = _manager(tmp_path / "manager", monkeypatch)
    bound = manager.bind_runtime_environment(
        release,
        {
            "config": {
                "data_mode": "real",
                "external_config_dependencies": dependencies,
            }
        },
        environment,
    )
    assert bound[config_key] == str(packaged.resolve())


def test_release_runtime_config_identity_keeps_project_external_config_checksum_bound(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    external = tmp_path / "operator" / "sources.yaml"
    external.parent.mkdir()
    external.write_text("sources: {}", encoding="utf-8")

    dependencies = _external_config_identity({"NEWS_SOURCES_CONFIG": str(external)}, project_root=project_root)

    assert dependencies == [{
        "name": "NEWS_SOURCES_CONFIG",
        "mode": "external",
        "path": str(external.resolve()),
        "size": external.stat().st_size,
        "sha256": sha256(external.read_bytes()).hexdigest(),
    }]


@pytest.mark.parametrize("config_key", ["NEWS_MODELS_CONFIG", "NEWS_RESEARCH_MODELS_CONFIG"])
def test_release_verify_rejects_external_runtime_config_drift(tmp_path: Path, monkeypatch, config_key: str) -> None:
    models = tmp_path / "models.yaml"
    models.write_text("routes: {}", encoding="utf-8")
    monkeypatch.setenv(config_key, str(models))
    manager, release = _manager(
        tmp_path,
        monkeypatch,
        external_environment={config_key: str(models)},
    )
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    dependency = {"name": config_key, "mode": "external", "path": str(models.resolve()), "size": models.stat().st_size, "sha256": sha256(models.read_bytes()).hexdigest()}
    manifest["config"]["external_config_dependencies"] = [dependency]
    manifest["identity"]["config_digest"] = _json_digest(manifest["config"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    models.write_text("routes:\n  changed: true", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code == "desktop_release_config_incompatible"



def test_release_verify_rejects_manifested_resource_replaced_by_symlink(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    resource = release / "app" / "interfaces" / "__init__.py"
    target = release / "app" / "same-content.py"
    target.write_bytes(resource.read_bytes())
    resource.unlink()
    try:
        resource.symlink_to(target)
    except OSError:
        pytest.skip("test environment cannot create symlinks")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code in {"desktop_path_escape", "desktop_reparse_point_rejected"}


def test_release_runtime_binding_rejects_browser_mock_data_mode(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    manifest = json.loads((release / "release-manifest.json").read_text(encoding="utf-8"))

    with pytest.raises(DesktopOperationError) as raised:
        manager.bind_runtime_environment(
            release,
            manifest,
            {"NEXT_PUBLIC_NEWSROOM_USE_MOCKS": "true"},
        )

    assert raised.value.reason_code == "desktop_mock_data_rejected"


def test_release_verify_rejects_manifest_without_real_data_identity(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config"]["data_mode"] = "mock"
    manifest["identity"]["config_digest"] = _json_digest(manifest["config"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)

    assert raised.value.reason_code == "desktop_release_config_incompatible"


def test_node_dependency_tree_identity_binds_actual_nested_versions(tmp_path: Path) -> None:
    payload = {
        "name": "frontend",
        "version": "1.0.0",
        "dependencies": {
            "next": {
                "version": "15.0.0",
                "resolved": "https://registry.example/next.tgz",
                "integrity": "sha512-next",
                "dependencies": {
                    "react": {"version": "19.0.0"},
                },
            },
        },
    }

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    manager = ReleaseManager(
        resolve_local_desktop_config(project_root=tmp_path),
        command_runner=runner,
    )
    first = manager._node_dependency_tree_identity(tmp_path, "npm.cmd")
    payload["dependencies"]["next"]["dependencies"]["react"]["version"] = "19.0.1"
    second = manager._node_dependency_tree_identity(tmp_path, "npm.cmd")

    assert first["package_count"] == second["package_count"] == 3
    assert first["sha256"] != second["sha256"]


def test_node_dependency_tree_identity_rejects_problems_even_when_npm_exits_zero(tmp_path: Path) -> None:
    payload = {
        "name": "frontend",
        "version": "1.0.0",
        "problems": ["extraneous: unexpected-package@1.2.3"],
        "dependencies": {
            "unexpected-package": {
                "version": "1.2.3",
                "extraneous": True,
            },
        },
    }

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    manager = ReleaseManager(
        resolve_local_desktop_config(project_root=tmp_path),
        command_runner=runner,
    )

    with pytest.raises(DesktopOperationError) as raised:
        manager._node_dependency_tree_identity(tmp_path, "npm.cmd")

    assert raised.value.reason_code == "desktop_node_dependency_tree_invalid"
    assert raised.value.details == {"returncode": 0, "problem_count": 1}


def test_research_models_config_has_business_priority_and_is_release_bound(tmp_path: Path) -> None:
    from interfaces.composition.research_settings import ResearchRuntimeSettings

    general = tmp_path / "general-models.yaml"
    research = tmp_path / "research-models.yaml"
    general.write_text("routes: {}", encoding="utf-8")
    research.write_text("routes: {}", encoding="utf-8")
    environment = {
        "NEWS_MODELS_CONFIG": str(general),
        "NEWS_RESEARCH_MODELS_CONFIG": str(research),
        "OPENAI_API_KEY": "test-credential",
    }

    settings = ResearchRuntimeSettings.from_env(environment, cwd=tmp_path)
    dependencies = _external_config_identity(environment, project_root=tmp_path / "project")

    assert settings.llm.models_config_path == research.resolve()
    assert [item["name"] for item in dependencies] == [
        "NEWS_MODELS_CONFIG",
        "NEWS_RESEARCH_MODELS_CONFIG",
    ]
