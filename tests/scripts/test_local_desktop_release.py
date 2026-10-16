from __future__ import annotations

import json
import os
import subprocess
import stat
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

import scripts.local_desktop.release as release_module
import scripts.local_desktop.safe_tree as safe_tree_module
from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.config import sanitized_environment
from scripts.local_desktop.release import CONFIG_SCHEMA, DATA_SCHEMAS, RELEASE_SCHEMA, ReleaseManager, _external_config_identity, _frontend_copy_ignore, _json_digest, _source_input_digest


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
        lambda actual_config, base=None: sanitized_environment(
            actual_config,
            base=isolated_environment if base is None else base,
        ),
    )
    monkeypatch.setattr(manager, "_dependency_identity", lambda _root=None, **_kwargs: {"python": {"version": "3.12"}, "node": {"version": "v22"}})
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
        "storage_mode": config.storage_mode,
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


@pytest.mark.parametrize(
    "verified_resources",
    [None, {}, {"frontend/package-lock.json": (7, "a" * 64)}],
)
def test_private_dependency_identity_forwards_snapshot_only_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified_resources,
) -> None:
    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path))
    frontend_root = tmp_path / "frontend"
    expected = {"python": {"version": "test"}, "node": {}}
    calls = []

    def public_identity(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(manager, "dependency_identity", public_identity)

    assert manager._dependency_identity(frontend_root, verified_resources=verified_resources) is expected
    expected_kwargs = {} if verified_resources is None else {"verified_resources": verified_resources}
    assert calls == [((frontend_root,), expected_kwargs)]
    if verified_resources is not None:
        assert calls[0][1]["verified_resources"] is verified_resources


@pytest.mark.parametrize("operation", ["verify", "activate"])
def test_release_validation_operation_uses_bounded_node_validation_and_hashes_each_file_once(
    tmp_path: Path,
    monkeypatch,
    operation: str,
) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    expected_files = {path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_file()}
    expected_directories = {path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_dir()}
    validated: list[str] = []
    hashed: list[str] = []
    original_validate = safe_tree_module._validate_node
    original_hash = safe_tree_module._hash_file

    def count_validate(path: Path, metadata, **kwargs):
        validated.append(path.relative_to(release).as_posix())
        return original_validate(path, metadata, **kwargs)

    def count_hash(path: Path, metadata, **kwargs):
        hashed.append(path.relative_to(release).as_posix())
        return original_hash(path, metadata, **kwargs)

    monkeypatch.setattr(safe_tree_module, "_validate_node", count_validate)
    monkeypatch.setattr(safe_tree_module, "_hash_file", count_hash)

    getattr(manager, operation)(release)

    expected_validations = Counter({path: 2 for path in expected_files})
    expected_validations.update({path: 4 for path in expected_directories})
    expected_validations["."] = 4
    assert Counter(validated) == expected_validations
    assert Counter(hashed) == Counter({path: 1 for path in expected_files})

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
    (source / "next-env.d.ts").write_text("development routes", encoding="utf-8")
    (source / "tsconfig.tsbuildinfo").write_text("development graph", encoding="utf-8")
    (source / ".next-dev.log").write_text("development stdout", encoding="utf-8")
    (source / ".next-dev-error.log").write_text("development stderr", encoding="utf-8")

    ReleaseManager._copy_tree(source, destination, ignore=_frontend_copy_ignore)

    assert (destination / "src" / "page.tsx").is_file()
    assert not (destination / ".env.local").exists()
    assert not (destination / ".env.production").exists()
    assert not (destination / ".next").exists()
    assert not (destination / "test-results").exists()
    assert not (destination / "next-env.d.ts").exists()
    assert not (destination / "tsconfig.tsbuildinfo").exists()
    assert not (destination / ".next-dev.log").exists()
    assert not (destination / ".next-dev-error.log").exists()
    assert _source_input_digest(source_root) == _source_input_digest(destination_root)


def test_source_identity_ignores_frontend_generated_rewrites_but_tracks_real_source(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    source = frontend / "src" / "page.tsx"
    source.write_text("export default function Page() { return 'one' }", encoding="utf-8")
    backend_build_info = tmp_path / "backend" / "source-owned.tsbuildinfo"
    backend_build_info.parent.mkdir()
    backend_build_info.write_text("source-owned state", encoding="utf-8")
    (frontend / "next-env.d.ts").write_text("import './.next/dev/types/routes.d.ts'", encoding="utf-8")
    (frontend / "tsconfig.tsbuildinfo").write_text("development graph", encoding="utf-8")
    (frontend / ".next-dev.log").write_text("development stdout", encoding="utf-8")
    (frontend / ".next-dev-error.log").write_text("development stderr", encoding="utf-8")
    before = _source_input_digest(tmp_path)

    (frontend / "next-env.d.ts").write_text("import './.next/types/routes.d.ts'", encoding="utf-8")
    (frontend / "tsconfig.tsbuildinfo").write_text("production graph", encoding="utf-8")
    (frontend / ".next-dev.log").write_text("later development stdout", encoding="utf-8")
    (frontend / ".next-dev-error.log").write_text("later development stderr", encoding="utf-8")

    assert _source_input_digest(tmp_path) == before
    backend_build_info.write_text("changed source-owned state", encoding="utf-8")
    assert _source_input_digest(tmp_path) != before
    backend_build_info.write_text("source-owned state", encoding="utf-8")
    assert _source_input_digest(tmp_path) == before
    source.write_text("export default function Page() { return 'two' }", encoding="utf-8")
    assert _source_input_digest(tmp_path) != before


def test_generated_frontend_resource_remains_checksum_protected(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    generated = release / "frontend" / "next-env.d.ts"
    generated.write_text("production routes", encoding="utf-8")
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data = generated.read_bytes()
    manifest["resources"].append({
        "path": "frontend/next-env.d.ts",
        "size": len(data),
        "sha256": sha256(data).hexdigest(),
    })
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    manager.verify(release)
    generated.write_text("tampered production routes", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)
    assert raised.value.reason_code == "desktop_release_checksum_mismatch"


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
                "storage_mode": manager.config.storage_mode,
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


def test_release_verify_rejects_storage_mode_drift(tmp_path: Path, monkeypatch) -> None:
    manager, release = _manager(tmp_path, monkeypatch)
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["config"]["storage_mode"] = "owned-local"
    manifest["identity"]["config_digest"] = _json_digest(manifest["config"])
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        manager.verify(release)

    assert raised.value.reason_code == "desktop_release_config_incompatible"


def test_owned_local_runtime_binding_keeps_empty_dsn_shadow(tmp_path: Path) -> None:
    project_root = tmp_path
    config = resolve_local_desktop_config(
        project_root=project_root,
        runtime_config={"storage_mode": "owned-local"},
    )
    manager = ReleaseManager(config)

    bound = manager.bind_runtime_environment(
        project_root,
        {
            "config": {
                "storage_mode": "owned-local",
                "data_mode": "real",
                "external_config_dependencies": [],
            }
        },
        {"NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom"},
    )

    assert bound["NEWS_DATABASE_DSN"] == ""
    assert bound["NEWSROOM_DESKTOP_ROOT_DATABASE_DSN_SHADOWED"] == "1"


def test_release_subprocess_decodes_real_utf8_non_ascii_output_strictly(tmp_path: Path) -> None:
    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path))
    helper = [sys.executable, "-c", "import sys; sys.stdout.buffer.write('版本二'.encode('utf-8')); sys.stderr.buffer.write('构建完成'.encode('utf-8'))"]

    assert manager._tool_version(helper) == "版本二"
    manager._run(helper, cwd=tmp_path, env=os.environ, timeout=10)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_release_build_rejects_non_utf8_command_output_with_typed_error(tmp_path: Path, stream: str) -> None:
    invalid = b"\xb2"

    def runner(argv, **kwargs):
        assert kwargs["text"] is False
        return subprocess.CompletedProcess(
            argv,
            0,
            invalid if stream == "stdout" else b"ok",
            invalid if stream == "stderr" else b"",
        )

    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path), command_runner=runner)

    with pytest.raises(DesktopOperationError) as raised:
        manager._run(["npm.cmd", "run", "build"], cwd=tmp_path, env={}, timeout=10)

    assert raised.value.reason_code == "desktop_command_output_encoding_invalid"
    assert raised.value.details == {"command": "npm.cmd", "stream": stream}


def test_release_inspection_commands_capture_bytes_and_decode_utf8(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], bool]] = []
    dependency_payload = {"name": "前端", "version": "1.0.0", "dependencies": {}}

    def runner(argv, **kwargs):
        calls.append((tuple(argv), kwargs["text"]))
        if argv[:2] == ["npm.cmd", "ls"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(dependency_payload, ensure_ascii=False).encode("utf-8"), "依赖正常".encode("utf-8"))
        if argv[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(argv, 0, ("a" * 40 + "\n").encode("utf-8"), b"")
        return subprocess.CompletedProcess(argv, 0, "版本三\n".encode("utf-8"), b"")

    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path), command_runner=runner)

    assert manager._tool_version(["node.exe", "--version"]) == "版本三"
    assert manager._git_revision() == "a" * 40
    assert manager._node_dependency_tree_identity(tmp_path, "npm.cmd")["package_count"] == 1
    assert calls and all(text is False for _argv, text in calls)


def test_release_dependency_inspection_rejects_non_utf8_json_output(tmp_path: Path) -> None:
    def runner(argv, **kwargs):
        assert kwargs["text"] is False
        return subprocess.CompletedProcess(argv, 0, b"{\xb2}", b"")

    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path), command_runner=runner)

    with pytest.raises(DesktopOperationError) as raised:
        manager._node_dependency_tree_identity(tmp_path, "npm.cmd")

    assert raised.value.reason_code == "desktop_command_output_encoding_invalid"
    assert raised.value.details == {"command": "npm.cmd", "stream": "stdout"}



_STRICT_SOURCE_NPM_TREE_BASE = {"package_count": 3, "sha256": "e" * 64}


def _strict_source_npm_tree(frontend: Path) -> dict[str, object]:
    return {
        **_STRICT_SOURCE_NPM_TREE_BASE,
        "package_lock_sha256": sha256((frontend / "package-lock.json").read_bytes()).hexdigest(),
    }


def _write_standalone_build(frontend: Path) -> None:
    package_lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "newsroom-frontend", "version": "0.1.1"},
            "node_modules/next": {"name": "next", "version": "16.4.0"},
            "node_modules/react": {"name": "react", "version": "19.3.0"},
            "node_modules/traced-package": {"name": "traced-package", "version": "1.0.0"},
        },
    }
    for relative, data in {
        ".next/standalone/server.js": b"require('next')\n",
        ".next/standalone/node_modules/next/package.json": b'{"name":"next","version":"16.4.0"}',
        ".next/standalone/node_modules/react/package.json": b'{"name":"react","version":"19.3.0"}',
        ".next/static/chunks/app.js": b"static-chunk",
        "public/pdf.worker.min.mjs": b"pdf-worker",
        "package-lock.json": json.dumps(package_lock, sort_keys=True).encode("utf-8"),
    }.items():
        target = frontend / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def test_materialize_next_standalone_replaces_dev_tree_with_hashed_production_closure(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    _write_standalone_build(frontend)
    (frontend / "node_modules/typescript").mkdir(parents=True)
    (frontend / "node_modules/typescript/package.json").write_text('{"name":"typescript","version":"5.9.3"}', encoding="utf-8")
    source_installed_tree = _strict_source_npm_tree(frontend)

    release_module._materialize_next_standalone(frontend, source_digest="a" * 64, source_installed_tree=source_installed_tree)

    closure = json.loads((frontend / "production-closure.json").read_text(encoding="utf-8"))
    assert closure["schema"] == "newsroom.next-standalone-closure/v1"
    assert closure["runtime_mode"] == "next-standalone"
    assert closure["entrypoint"] == "server.js"
    assert closure["source_digest"] == "a" * 64
    assert closure["source_installed_tree"] == source_installed_tree
    assert closure["materialized_links"] == []
    assert closure["packages"] == [
        {
            "lock_association": {
                "kind": "source-lock",
                "name": "next",
                "path": "node_modules/next",
                "version": "16.4.0",
            },
            "name": "next",
            "path": "node_modules/next",
            "version": "16.4.0",
        },
        {
            "lock_association": {
                "kind": "source-lock",
                "name": "react",
                "path": "node_modules/react",
                "version": "19.3.0",
            },
            "name": "react",
            "path": "node_modules/react",
            "version": "19.3.0",
        },
    ]
    assert (frontend / "server.js").is_file()
    assert (frontend / "public/pdf.worker.min.mjs").is_file()
    assert (frontend / ".next/static/chunks/app.js").is_file()
    assert not (frontend / "node_modules/typescript").exists()
    identity = release_module._production_frontend_identity(frontend)
    assert identity is not None
    assert identity["runtime_mode"] == "next-standalone"
    assert identity["installed_tree"]["package_count"] == 2
    assert identity["installed_tree"]["source_npm_tree"] == source_installed_tree


def test_production_frontend_identity_rejects_unmanifested_runtime_file(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    _write_standalone_build(frontend)
    release_module._materialize_next_standalone(frontend, source_digest="a" * 64, source_installed_tree=_strict_source_npm_tree(frontend))
    (frontend / "unexpected.js").write_text("unexpected", encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        release_module._production_frontend_identity(frontend)

    assert raised.value.reason_code == "desktop_frontend_production_closure_drift"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junction reparse tags")
def test_materialize_next_standalone_accepts_controlled_junction_and_records_relative_evidence(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    _write_standalone_build(frontend)
    real_package = frontend / "node_modules-real" / "traced-package"
    real_package.mkdir(parents=True)
    (real_package / "package.json").write_text('{"name":"traced-package","version":"1.0.0"}', encoding="utf-8")
    junction = frontend / ".next/standalone/node_modules/traced-package"
    junction.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(real_package)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"junction unavailable: {result.stderr or result.stdout}")

    release_module._materialize_next_standalone(frontend, source_digest="b" * 64, source_installed_tree=_strict_source_npm_tree(frontend))

    closure = json.loads((frontend / "production-closure.json").read_text(encoding="utf-8"))
    assert closure["materialized_links"] == [
        {
            "kind": "mount-point",
            "tag": "0xa0000003",
            "source": ".next/standalone/node_modules/traced-package",
            "target": "node_modules-real/traced-package",
        }
    ]
    materialized = frontend / "node_modules/traced-package"
    metadata = materialized.lstat()
    assert not stat.S_ISLNK(metadata.st_mode)
    assert not (getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def test_source_input_manifest_preserves_prebuild_frontend_identity_after_standalone_replacement(tmp_path: Path) -> None:
    root = tmp_path / "release-stage"
    (root / "app/interfaces").mkdir(parents=True)
    (root / "app/interfaces/__init__.py").write_text("", encoding="utf-8")
    (root / "frontend/src").mkdir(parents=True)
    (root / "frontend/src/page.tsx").write_text("export default function Page() { return null }", encoding="utf-8")
    digest = release_module._source_input_digest(root)

    release_module._write_source_input_manifest(root, source_digest=digest)
    snapshot = release_module._scan_release_tree(
        root,
        captured={release_module._SOURCE_INPUT_MANIFEST},
    )

    assert release_module._verified_source_input_digest(snapshot) == digest



def test_materialize_next_standalone_rejects_traced_package_absent_from_source_lock(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    _write_standalone_build(frontend)
    unowned = frontend / ".next/standalone/node_modules/unowned/package.json"
    unowned.parent.mkdir(parents=True)
    unowned.write_text('{"name":"unowned","version":"1.0.0"}', encoding="utf-8")

    with pytest.raises(DesktopOperationError) as raised:
        release_module._materialize_next_standalone(
            frontend,
            source_digest="c" * 64,
            source_installed_tree=_strict_source_npm_tree(frontend),
        )

    assert raised.value.reason_code == "desktop_frontend_standalone_package_unlocked"
    assert (frontend / "package-lock.json").is_file()


def test_materialize_next_standalone_allows_only_explicit_next_vendored_compiled_ownership(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    _write_standalone_build(frontend)
    vendored = frontend / ".next/standalone/node_modules/next/dist/compiled/vendored/package.json"
    vendored.parent.mkdir(parents=True)
    vendored.write_text('{"name":"vendored","version":"2.0.0"}', encoding="utf-8")

    release_module._materialize_next_standalone(
        frontend,
        source_digest="d" * 64,
        source_installed_tree=_strict_source_npm_tree(frontend),
    )

    closure = json.loads((frontend / "production-closure.json").read_text(encoding="utf-8"))
    package = next(item for item in closure["packages"] if item["name"] == "vendored")
    assert package["lock_association"] == {
        "kind": "next-vendored",
        "owner_name": "next",
        "owner_path": "node_modules/next",
        "owner_version": "16.4.0",
    }


def test_local_desktop_build_runs_strict_source_npm_gate_before_standalone_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name":"fixture","version":"0.1.1"}', encoding="utf-8")
    (frontend / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": {"name": "fixture", "version": "0.1.1"}}}),
        encoding="utf-8",
    )
    config = resolve_local_desktop_config(project_root=tmp_path)
    manager = ReleaseManager(config)
    events: list[str] = []

    monkeypatch.setattr(manager, "_require_build_inputs", lambda: None)
    monkeypatch.setattr(manager, "_git_revision", lambda: "a" * 40)
    monkeypatch.setattr(release_module.shutil, "which", lambda _name: "npm.cmd")

    def run(argv, **_kwargs):
        events.append("npm-build" if argv[1:] == ["run", "build"] else "npm-ci")

    def installed_tree(frontend_root, npm_executable, **_kwargs):
        assert events == ["npm-ci", "npm-build"]
        assert npm_executable == "npm.cmd"
        assert (frontend_root / "package-lock.json").is_file()
        events.append("strict-installed-tree")
        return dict(_STRICT_SOURCE_NPM_TREE_BASE)

    def materialize(frontend_root, *, source_digest, source_installed_tree):
        assert events[-1] == "strict-installed-tree"
        assert len(source_digest) == 64
        assert source_installed_tree == _strict_source_npm_tree(frontend_root)
        events.append("materialize")
        (frontend_root / "server.js").write_text("server", encoding="utf-8")

    monkeypatch.setattr(manager, "_run", run)
    monkeypatch.setattr(manager, "_node_dependency_tree_identity", installed_tree)
    monkeypatch.setattr(manager, "_dependency_identity", lambda _root=None, **_kwargs: {"python": {}, "node": {}})
    monkeypatch.setattr(manager, "verify_and_activate", lambda destination: (json.loads((destination / "release-manifest.json").read_text(encoding="utf-8")), {}))
    monkeypatch.setattr(release_module, "_materialize_next_standalone", materialize)

    manifest = manager.build()

    assert events == ["npm-ci", "npm-build", "strict-installed-tree", "materialize"]
    assert manifest["entrypoints"]["frontend"] == ["node", "server.js"]


def test_materialize_traced_tree_rejects_unknown_reparse_tag_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    source = frontend / ".next/standalone"
    source.mkdir(parents=True)
    destination = tmp_path / "materialized"
    extended_source = release_module.windows_extended_path(source)
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == source or path == extended_source:
            return type(
                "UnknownReparseStat",
                (),
                {
                    "st_file_attributes": getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
                    "st_mode": stat.S_IFDIR,
                    "st_reparse_tag": 0xDEADBEEF,
                },
            )()
        return original_lstat(path, *args, **kwargs)

    # Inject the filesystem metadata, not a partial Path stand-in: path
    # normalization must retain real extended-length Path semantics.
    monkeypatch.setattr(Path, "lstat", lstat)

    with pytest.raises(DesktopOperationError) as raised:
        release_module._materialize_traced_tree(source, destination, allowed_root=frontend)

    assert raised.value.reason_code == "desktop_frontend_standalone_reparse_tag_unsupported"
    assert raised.value.details == {"source": ".next/standalone", "tag": 0xDEADBEEF}
    assert not destination.exists()


def test_materialize_next_standalone_rejects_source_lock_drift_before_copy(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    _write_standalone_build(frontend)
    source_tree = _strict_source_npm_tree(frontend)
    source_tree["package_lock_sha256"] = "0" * 64

    with pytest.raises(DesktopOperationError) as raised:
        release_module._materialize_next_standalone(
            frontend,
            source_digest="f" * 64,
            source_installed_tree=source_tree,
        )

    assert raised.value.reason_code == "desktop_frontend_source_lock_drift"
    assert (frontend / ".next/standalone/server.js").is_file()
    assert not (frontend / "production-closure.json").exists()


def test_standalone_subpath_descriptors_require_locked_materialized_owner(tmp_path):
    import scripts.local_desktop.release as release_module
    runtime = tmp_path / "runtime"
    helpers = runtime / "node_modules/@swc/helpers"
    descriptor = helpers / "_/interop/package.json"
    descriptor.parent.mkdir(parents=True)
    descriptor.write_text('{"main":"../../cjs/interop.cjs"}', encoding="utf-8")
    (helpers / "package.json").write_text('{"name":"@swc/helpers","version":"0.5.0"}', encoding="utf-8")
    next_root = runtime / "node_modules/next"
    next_root.mkdir(parents=True)
    (next_root / "package.json").write_text('{"name":"next","version":"15.0.0"}', encoding="utf-8")
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({"packages":{"node_modules/@swc/helpers":{"version":"0.5.0"},"node_modules/next":{"version":"15.0.0"}}}), encoding="utf-8")
    packages = release_module._standalone_package_inventory(runtime, source_lock=lock)
    assert {item["name"] for item in packages} == {"@swc/helpers", "next"}
    (helpers / "package.json").write_text('{"name":"@swc/helpers","version":"9.0.0"}', encoding="utf-8")
    with pytest.raises(DesktopOperationError):
        release_module._standalone_package_inventory(runtime, source_lock=lock)
