from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import scripts.desktop_app.config as config_module
from framework.shared.local_desktop import LocalDesktopConfigError
from scripts.desktop_app.config import (
    PERSONAL_MEMORY_ENV,
    controlled_process_environment,
    initialize_data_root,
    resolve_desktop_app_config,
)
from scripts.local_desktop.common import DesktopOperationError


def _resource_root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime"
    (root / "configs").mkdir(parents=True)
    (root / "configs" / "models.yaml").write_text("providers: {}\n", encoding="utf-8")
    (root / "configs" / "sources.yaml").write_text("sources: {}\n", encoding="utf-8")
    (root / ".venv/Scripts").mkdir(parents=True)
    (root / ".venv/Scripts/python.exe").write_bytes(b"python")
    (root / "node").mkdir()
    (root / "node/node.exe").write_bytes(b"node")
    return root


def test_config_maps_every_owned_writer_to_separate_data_root(tmp_path: Path) -> None:
    resource_root = _resource_root(tmp_path)
    data_root = tmp_path / "user-data"

    config = resolve_desktop_app_config(
        resource_root=resource_root,
        data_root=data_root,
        frontend_port=3120,
        api_port=8120,
        environ={"NEWS_DATABASE_DSN": "postgresql://legacy.invalid/news"},
    )

    assert config.desktop.project_root == resource_root.resolve()
    assert config.desktop.runtime_root == data_root.resolve()
    assert config.desktop.release_root == (data_root / "releases").resolve()
    assert config.desktop.backup_root == (data_root / "backups").resolve()
    assert config.desktop.control_path == (data_root / "desktop/control/control.json").resolve()
    assert config.desktop.research_root == (data_root / "research").resolve()
    assert config.desktop.artifact_root == (data_root / "runs/artifacts").resolve()
    assert config.desktop.storage_mode == "owned-local"
    assert config.desktop.canonical_origin == "http://127.0.0.1:3120"
    assert config.desktop.api_base_url == "http://127.0.0.1:8120"
    assert config.environment["NEWS_DATABASE_DSN"] == ""
    assert config.environment["NEWSROOM_AUTH_USERS_PATH"] == str((data_root / "auth/users.json").resolve())
    assert config.environment["NEWSROOM_RESEARCH_HISTORY_PATH"] == str((data_root / "research/history/history.json").resolve())
    assert config.environment["NEWSROOM_RESEARCH_IMPORT_DIR"] == str((data_root / "research/imports").resolve())
    assert config.environment["NEWSROOM_PAPERS_DATA_PATH"] == str((data_root / "research/catalog/arxiv-papers.json").resolve())
    assert config.environment["NEWS_RESEARCH_RUN_STORE_ROOT"] == str((data_root / "runs/research").resolve())
    assert config.environment["NEWS_RESEARCH_ARTIFACT_ROOT"] == str((data_root / "runs/artifacts").resolve())
    assert config.environment[PERSONAL_MEMORY_ENV] == str((data_root / "research/personal-memory.sqlite3").resolve())
    assert not (resource_root / ".newsroom").exists()



def test_first_run_llm_null_does_not_inherit_packaged_or_parent_provider(tmp_path: Path) -> None:
    resource_root = _resource_root(tmp_path)
    data_root = tmp_path / "user-data"
    inherited = {
        "NEWS_MODELS_CONFIG": r"C:\parent\models.json",
        "NEWS_RESEARCH_MODELS_CONFIG": r"C:\parent\models.json",
        "NEWS_RESEARCH_LLM_PROVIDER": "openai-compatible",
        "NEWS_RESEARCH_LLM_MODEL": "remote-model",
        "NEWS_RESEARCH_LLM_BASE_URL": "https://remote.example/v1",
        "NEWS_RESEARCH_LLM_API_KEY_ENV": "REMOTE_KEY",
    }

    config = resolve_desktop_app_config(resource_root=resource_root, data_root=data_root, environ=inherited)

    assert not (data_root / "desktop/config/research-runtime.json").exists()
    assert not (data_root / "desktop/config/models.json").exists()
    for key in (
        "NEWS_MODELS_CONFIG",
        "NEWS_RESEARCH_MODELS_CONFIG",
        "NEWS_RESEARCH_LLM_PROVIDER",
        "NEWS_RESEARCH_LLM_MODEL",
        "NEWS_RESEARCH_LLM_BASE_URL",
        "NEWS_RESEARCH_LLM_API_KEY_ENV",
        "NEWS_RESEARCH_LLM_ROUTE_ID",
    ):
        assert key not in config.environment

def test_initialize_creates_only_controlled_directories_not_business_records(tmp_path: Path) -> None:
    config = resolve_desktop_app_config(resource_root=_resource_root(tmp_path), data_root=tmp_path / "user-data", environ={})

    created = initialize_data_root(config)

    assert config.data_root / "auth" in created
    assert (config.data_root / "research").is_dir()
    assert (config.data_root / "logs").is_dir()
    assert not (config.data_root / "auth/users.json").exists()
    assert not (config.data_root / "research/history/history.json").exists()
    assert not (config.data_root / "research/personal-memory.sqlite3").exists()


def test_initialize_applies_existing_private_path_contract_to_every_owned_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = resolve_desktop_app_config(resource_root=_resource_root(tmp_path), data_root=tmp_path / "user-data", environ={})
    protected: list[Path] = []
    monkeypatch.setattr(config_module, "protect_private_path", lambda path: protected.append(path))

    directories = initialize_data_root(config)

    assert set(directories).issubset(set(protected))
    assert len(protected) == len(set(protected))
    assert all(path == config.data_root or path.is_relative_to(config.data_root) for path in protected)


def test_initialize_propagates_write_failure_without_fabricating_success(tmp_path: Path) -> None:
    config = resolve_desktop_app_config(resource_root=_resource_root(tmp_path), data_root=tmp_path / "user-data", environ={})
    config.data_root.mkdir()
    (config.data_root / "auth").write_text("not-a-directory", encoding="utf-8")

    with pytest.raises((FileExistsError, NotADirectoryError, DesktopOperationError)):
        initialize_data_root(config)
    assert not (config.data_root / "auth/users.json").exists()


def test_config_rejects_install_and_user_data_root_overlap(tmp_path: Path) -> None:
    resource_root = _resource_root(tmp_path)

    with pytest.raises(LocalDesktopConfigError) as raised:
        resolve_desktop_app_config(resource_root=resource_root, data_root=resource_root / "data", environ={})

    assert raised.value.reason_code == "desktop_app_roots_overlap"


def test_controlled_environment_prepends_bundle_tools_and_restores_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = resolve_desktop_app_config(resource_root=_resource_root(tmp_path), data_root=tmp_path / "user-data", environ={})
    monkeypatch.setenv("PATH", r"C:\existing")
    monkeypatch.setenv("PYTHONHOME", r"C:\unsafe-python")
    monkeypatch.setenv("PYTHONPATH", r"C:\unsafe-modules")
    before = dict(os.environ)

    with controlled_process_environment(config):
        parts = os.environ["PATH"].split(os.pathsep)
        assert parts[:2] == [str(config.node_root), str(config.python_executable.parent)]
        assert os.environ["NEWS_DATABASE_DSN"] == ""
        assert os.environ["PYTHONNOUSERSITE"] == "1"
        assert "PYTHONHOME" not in os.environ
        assert "PYTHONPATH" not in os.environ

    assert os.environ["PATH"] == before["PATH"]
    assert os.environ["PYTHONHOME"] == before["PYTHONHOME"]
    assert os.environ["PYTHONPATH"] == before["PYTHONPATH"]
    assert os.environ.get("NEWSROOM_RESEARCH_HISTORY_PATH") == before.get("NEWSROOM_RESEARCH_HISTORY_PATH")



def _create_windows_junction(link: Path, target: Path) -> None:
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(f"Windows junction creation unavailable: {result.stderr or result.stdout}")


def _remove_windows_junction(link: Path) -> None:
    if os.path.lexists(link):
        os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_config_rejects_resource_root_junction_before_resolution(tmp_path: Path) -> None:
    target = tmp_path / "resource-target"
    target.mkdir()
    marker = target / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    junction = tmp_path / "resource-junction"
    _create_windows_junction(junction, target)
    try:
        with pytest.raises(LocalDesktopConfigError) as raised:
            resolve_desktop_app_config(resource_root=junction, data_root=tmp_path / "user-data", environ={})
        assert raised.value.reason_code == "desktop_app_reparse_path_rejected"
        assert marker.read_text(encoding="utf-8") == "untouched"
        assert sorted(path.name for path in target.iterdir()) == ["marker.txt"]
    finally:
        _remove_windows_junction(junction)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_config_rejects_data_root_junction_ancestor_without_writing_target(tmp_path: Path) -> None:
    resource_root = _resource_root(tmp_path)
    target = tmp_path / "data-target"
    target.mkdir()
    marker = target / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    junction = tmp_path / "data-parent-junction"
    _create_windows_junction(junction, target)
    try:
        with pytest.raises(LocalDesktopConfigError) as raised:
            resolve_desktop_app_config(resource_root=resource_root, data_root=junction / "user-data", environ={})
        assert raised.value.reason_code == "desktop_app_reparse_path_rejected"
        assert marker.read_text(encoding="utf-8") == "untouched"
        assert not (target / "user-data").exists()
    finally:
        _remove_windows_junction(junction)


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows junction")
def test_initialize_rechecks_owner_child_chain_before_any_write(tmp_path: Path) -> None:
    data_root = tmp_path / "user-data"
    config = resolve_desktop_app_config(resource_root=_resource_root(tmp_path), data_root=data_root, environ={})
    data_root.mkdir()
    target = tmp_path / "owner-target"
    target.mkdir()
    marker = target / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    junction = data_root / "research"
    _create_windows_junction(junction, target)
    try:
        with pytest.raises(DesktopOperationError) as raised:
            initialize_data_root(config)
        assert raised.value.reason_code == "desktop_reparse_point_rejected"
        assert marker.read_text(encoding="utf-8") == "untouched"
        assert sorted(path.name for path in target.iterdir()) == ["marker.txt"]
        assert not (data_root / "auth").exists()
    finally:
        _remove_windows_junction(junction)



def test_controlled_environment_masks_root_model_defaults_and_restores_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.local_desktop.config import sanitized_environment

    config = resolve_desktop_app_config(
        resource_root=_resource_root(tmp_path), data_root=tmp_path / "data", environ={}
    )
    monkeypatch.setenv("NEWS_MODELS_CONFIG", "parent-models.json")
    monkeypatch.setenv("NEWS_RESEARCH_MODELS_CONFIG", "parent-research-models.json")
    with controlled_process_environment(config):
        assert os.environ["NEWS_MODELS_CONFIG"] == ""
        assert os.environ["NEWS_RESEARCH_MODELS_CONFIG"] == ""
        environment = sanitized_environment(config.desktop)
        assert environment["NEWS_MODELS_CONFIG"] == ""
        assert environment["NEWS_RESEARCH_MODELS_CONFIG"] == ""
        assert environment["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "0"
    assert os.environ["NEWS_MODELS_CONFIG"] == "parent-models.json"
    assert os.environ["NEWS_RESEARCH_MODELS_CONFIG"] == "parent-research-models.json"
