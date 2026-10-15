from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from hashlib import sha256
from pathlib import Path

import pytest

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
