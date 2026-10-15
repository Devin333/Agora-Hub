from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

import scripts.local_desktop.safe_tree as safe_tree_module
from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError, read_bytes_long_path, windows_extended_path
from scripts.local_desktop.release import (
    CONFIG_SCHEMA,
    DATA_SCHEMAS,
    RELEASE_SCHEMA,
    ReleaseManager,
    _json_digest,
    _make_temp_directory_long_path,
    _remove_tree_long_path,
    _resource_entries,
    _source_input_digest,
    _trusted_tree_files,
)


def _write_bytes(path: Path, data: bytes) -> None:
    io_path = windows_extended_path(path)
    io_path.parent.mkdir(parents=True, exist_ok=True)
    with open(io_path, "wb") as handle:
        handle.write(data)


def _prepare_release(
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    release_root: Path | None = None,
) -> tuple[ReleaseManager, Path, dict[str, object]]:
    overrides = {"release_root": release_root} if release_root is not None else None
    config = resolve_local_desktop_config(project_root=project_root, cli_overrides=overrides)
    manager = ReleaseManager(config)
    dependencies = {"python": {"version": "3.12"}, "node": {"version": "v22"}}
    monkeypatch.setattr(manager, "_dependency_identity", lambda _root=None, **_kwargs: dependencies)
    monkeypatch.setattr("scripts.local_desktop.release.sanitized_environment", lambda _config, base=None: {})

    release = config.release_root / "release-long-path"
    resources = {
        "app/interfaces/__init__.py": b"",
        "frontend/node_modules/next/dist/bin/next": b"next-runtime\n",
    }
    for relative, data in resources.items():
        _write_bytes(release / relative, data)

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
    manifest: dict[str, object] = {
        "schema": RELEASE_SCHEMA,
        "identity": {
            "release_id": release.name,
            "source_revision": "a" * 40,
            "source_digest": _source_input_digest(release),
            "dependency_digest": _json_digest(dependencies),
            "config_digest": _json_digest(config_identity),
        },
        "config": config_identity,
        "entrypoints": {
            "api": ["-m", "interfaces.cli.news", "api", "serve", "--host", config.api_host, "--port", str(config.api_port)],
            "frontend": ["next", "start", "-H", config.frontend_host, "-p", str(config.frontend_port)],
        },
        "data_compatibility": {"readable_schemas": DATA_SCHEMAS, "writable_schemas": DATA_SCHEMAS},
        "resources": [
            {"path": relative, "size": len(data), "sha256": sha256(data).hexdigest()}
            for relative, data in sorted(resources.items())
        ],
    }
    _write_bytes(release / "release-manifest.json", json.dumps(manifest).encode("utf-8"))
    return manager, release, manifest


def _create_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended-length paths")
def test_release_verify_accepts_complete_resource_inventory_beyond_max_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_data_root = tmp_path / "desktop-data"
    next_relative = Path("release-long-path/frontend/node_modules/next/dist/bin/next")
    index = 0
    while len(os.fspath((long_data_root / "releases" / next_relative).absolute())) <= 280:
        long_data_root /= f"segment-{index:02d}-abcdefghij"
        index += 1
    release_root = long_data_root / "releases"
    windows_extended_path(release_root).mkdir(parents=True)

    manager, release, expected_manifest = _prepare_release(tmp_path, monkeypatch, release_root=release_root)
    next_resource = release / "frontend" / "node_modules" / "next" / "dist" / "bin" / "next"

    assert len(os.fspath(next_resource.absolute())) > 260
    assert windows_extended_path(next_resource).is_file()
    assert manager.verify(release) == expected_manifest
    assert read_bytes_long_path(next_resource) == b"next-runtime\n"


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_release_verify_rejects_junction_resource_without_symlink_privilege(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, release, _manifest = _prepare_release(tmp_path, monkeypatch)
    interfaces = release / "app" / "interfaces"
    windows_extended_path(interfaces / "__init__.py").unlink()
    windows_extended_path(interfaces).rmdir()
    target = tmp_path / "junction-target"
    target.mkdir()
    (target / "__init__.py").write_bytes(b"")
    _create_junction(interfaces, target)
    try:
        with pytest.raises(DesktopOperationError) as raised:
            manager.verify(release)

        assert raised.value.reason_code == "desktop_reparse_point_rejected"
        assert (target / "__init__.py").read_bytes() == b""
    finally:
        if os.path.lexists(interfaces):
            os.rmdir(interfaces)


def test_dependency_identity_uses_json_native_stable_python_package_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Distribution:
        def __init__(self, name: str, version: str) -> None:
            self.metadata = {"Name": name}
            self.version = version

    packages = [
        Distribution("zeta-package", "2.4.0"),
        Distribution("alpha-package", "1.3.5"),
    ]
    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path))
    monkeypatch.setattr("scripts.local_desktop.release.importlib.metadata.distributions", lambda: packages)
    monkeypatch.setattr("scripts.local_desktop.release.shutil.which", lambda _name: sys.executable)
    monkeypatch.setattr(manager, "_tool_version", lambda _argv: "test-version")
    monkeypatch.setattr(
        manager,
        "_node_dependency_tree_identity",
        lambda _root, _npm, environment=None: {"package_count": 0, "sha256": "0" * 64},
    )

    identity = manager.dependency_identity(tmp_path)
    python_packages = identity["python"]["packages"]
    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))

    assert python_packages == [
        ["alpha-package", "1.3.5"],
        ["zeta-package", "2.4.0"],
    ]
    assert len(python_packages) == 2
    assert all(isinstance(package, list) for package in python_packages)
    assert json.loads(serialized) == identity
    assert _json_digest(identity) == sha256(serialized.encode("utf-8")).hexdigest()


@pytest.mark.skipif(os.name != "nt", reason="requires Windows extended-length paths")
def test_release_copy_and_cleanup_support_long_staging_targets(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "runtime.txt").write_bytes(b"runtime-copy")
    destination = tmp_path / "staging"
    index = 0
    while len(os.fspath((destination / "runtime.txt").absolute())) <= 280:
        destination /= f"segment-{index:02d}-abcdefghij"
        index += 1

    ReleaseManager._copy_tree(source, destination)

    copied = destination / "runtime.txt"
    assert len(os.fspath(copied.absolute())) > 260
    assert read_bytes_long_path(copied) == b"runtime-copy"
    assert _resource_entries(destination) == [
        {
            "path": "runtime.txt",
            "size": len(b"runtime-copy"),
            "sha256": sha256(b"runtime-copy").hexdigest(),
        }
    ]

    _remove_tree_long_path(destination)
    assert not windows_extended_path(destination).exists()


def test_release_inventory_fails_closed_when_scan_reports_non_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "release"
    root.mkdir()

    class NonRegularEntry:
        name = "not-a-file"
        path = str(root / name)

        @staticmethod
        def stat(*, follow_symlinks: bool):
            assert follow_symlinks is False
            return os.stat_result((stat.S_IFIFO, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    class ScanResult:
        def __enter__(self):
            return iter([NonRegularEntry()])

        def __exit__(self, _type, _value, _traceback):
            return False

    original_lstat = safe_tree_module._lstat

    def non_regular_lstat(path: Path, **kwargs):
        if path.name == NonRegularEntry.name:
            return type("Metadata", (), {
                "st_mode": stat.S_IFIFO,
                "st_dev": 1,
                "st_ino": 1,
                "st_size": 0,
                "st_mtime_ns": 0,
                "st_ctime_ns": 0,
                "st_file_attributes": 0,
            })()
        return original_lstat(path, **kwargs)

    monkeypatch.setattr(safe_tree_module.os, "scandir", lambda _path: ScanResult())
    monkeypatch.setattr(safe_tree_module, "_lstat", non_regular_lstat)

    with pytest.raises(DesktopOperationError) as raised:
        _trusted_tree_files(root)

    assert raised.value.reason_code == "desktop_release_resource_type_invalid"


def test_release_build_resolves_and_calls_real_long_path_staging_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StagingHelperCalled(RuntimeError):
        pass

    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path))
    created: list[Path] = []

    def create_then_stop(parent: Path, *, prefix: str) -> Path:
        stage = _make_temp_directory_long_path(parent, prefix=prefix)
        created.append(stage)
        raise StagingHelperCalled

    monkeypatch.setattr(manager, "_require_build_inputs", lambda: None)
    monkeypatch.setattr(
        "scripts.local_desktop.release._make_temp_directory_long_path",
        create_then_stop,
    )

    with pytest.raises(StagingHelperCalled):
        manager.build()

    assert len(created) == 1
    assert created[0].parent == manager.config.release_root / ".staging"
    assert created[0].name.startswith("release-")
    assert windows_extended_path(created[0]).is_dir()
    _remove_tree_long_path(created[0])
    assert not windows_extended_path(created[0]).exists()
