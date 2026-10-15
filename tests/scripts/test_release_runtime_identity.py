from __future__ import annotations

import inspect
import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from framework.shared.local_desktop import resolve_local_desktop_config
from scripts.local_desktop.common import DesktopOperationError
from scripts.local_desktop.release import ReleaseManager


def _runtime_layout(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    application = tmp_path / "application"
    frontend = application / "frontend"
    python = application / ".venv/Scripts/python.exe"
    node = application / "node/node.exe"
    npm = application / "node/npm.cmd"
    for path, data in (
        (python, b"python-runtime"),
        (node, b"node-runtime"),
        (npm, b"npm-runtime"),
        (frontend / "package-lock.json", b'{"lockfileVersion":3}'),
        (frontend / "node_modules/.package-lock.json", b'{"lockfileVersion":3,"packages":{}}'),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return application, frontend, python, node, npm


def test_runtime_dependency_identity_is_computed_by_release_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    application, frontend, python, node, npm = _runtime_layout(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []
    python_payload = {"version": "3.12.14", "packages": [["fastapi", "1.0"], ["numpy", "2.0"]]}
    node_tree = {"name": "frontend", "version": "1.0.0", "dependencies": {"next": {"version": "15.0.0"}}}

    def runner(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        if Path(argv[0]) == python:
            return subprocess.CompletedProcess(argv, 0, json.dumps(python_payload).encode("utf-8"), b"")
        if Path(argv[0]) == node:
            return subprocess.CompletedProcess(argv, 0, b"v24.21.0\n", b"")
        if Path(argv[0]) == npm and argv[1:] == ["--version"]:
            return subprocess.CompletedProcess(argv, 0, b"11.19.0\n", b"")
        if Path(argv[0]) == npm and argv[1:3] == ["ls", "--json"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(node_tree).encode("utf-8"), b"")
        raise AssertionError(argv)

    monkeypatch.setenv("PYTHONHOME", r"C:\unsafe-python")
    monkeypatch.setenv("PYTHONPATH", r"C:\unsafe-modules")
    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path), command_runner=runner)

    identity = manager.dependency_identity_for_runtime(
        frontend,
        python_executable=python,
        node_executable=node,
        npm_executable=npm,
        application_root=application,
        required_python_imports=("scripts.desktop_app", "fastapi"),
        required_python_attributes=(("scripts.local_desktop.common", "protect_private_path"),),
    )

    assert identity["python"] == {
        "executable": str(python.resolve()),
        "executable_sha256": sha256(python.read_bytes()).hexdigest(),
        "version": "3.12.14",
        "packages": python_payload["packages"],
    }
    assert identity["node"]["executable_sha256"] == sha256(node.read_bytes()).hexdigest()
    assert identity["node"]["npm_executable_sha256"] == sha256(npm.read_bytes()).hexdigest()
    assert identity["node"]["version"] == "v24.21.0"
    assert identity["node"]["npm_version"] == "11.19.0"
    assert identity["node"]["package_lock_sha256"] == sha256((frontend / "package-lock.json").read_bytes()).hexdigest()
    assert identity["node"]["installed_lock_sha256"] == sha256((frontend / "node_modules/.package-lock.json").read_bytes()).hexdigest()
    assert identity["node"]["installed_tree"]["package_count"] == 2
    python_call = calls[0]
    assert python_call[0][1:3] == ["-I", "-c"]
    assert repr(str(application.resolve())) in python_call[0][3]
    assert python_call[1]["env"]["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONHOME" not in python_call[1]["env"]
    assert "PYTHONPATH" not in python_call[1]["env"]
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["timeout"] > 0 for call in calls)


def test_runtime_dependency_identity_public_contract_accepts_no_caller_digest() -> None:
    parameters = inspect.signature(ReleaseManager.dependency_identity_for_runtime).parameters

    assert "dependency_digest" not in parameters
    assert "executable_sha256" not in parameters
    assert "packages" not in parameters
    assert "node_version" not in parameters
    assert "npm_version" not in parameters


def test_private_dependency_identity_delegates_to_public_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path))
    expected = {"python": {"version": "test"}, "node": {}}
    monkeypatch.setattr(manager, "dependency_identity", lambda frontend_root=None: expected)

    assert manager._dependency_identity(tmp_path / "frontend") is expected


def test_runtime_dependency_identity_rejects_failed_python_probe(tmp_path: Path) -> None:
    application, frontend, python, node, npm = _runtime_layout(tmp_path)

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 7, b"", b"failed")

    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path), command_runner=runner)
    with pytest.raises(DesktopOperationError) as raised:
        manager.dependency_identity_for_runtime(
            frontend,
            python_executable=python,
            node_executable=node,
            npm_executable=npm,
            application_root=application,
        )

    assert raised.value.reason_code == "desktop_python_dependency_probe_failed"



@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_runtime_dependency_identity_rejects_lexical_junction_before_probe(tmp_path: Path) -> None:
    target = tmp_path / "runtime-target"
    application, frontend, python, node, npm = _runtime_layout(target)
    junction = tmp_path / "runtime-link"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(application)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")
    calls = 0

    def runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("runtime probe must not start through a junction")

    manager = ReleaseManager(resolve_local_desktop_config(project_root=tmp_path), command_runner=runner)
    try:
        with pytest.raises(DesktopOperationError) as raised:
            manager.dependency_identity_for_runtime(
                junction / "frontend",
                python_executable=junction / ".venv/Scripts/python.exe",
                node_executable=junction / "node/node.exe",
                npm_executable=junction / "node/npm.cmd",
                application_root=junction,
            )
        assert raised.value.reason_code == "desktop_reparse_point_rejected"
        assert calls == 0
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)
