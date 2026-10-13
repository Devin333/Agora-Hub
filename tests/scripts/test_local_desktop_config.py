from __future__ import annotations

from pathlib import Path

import pytest

from framework.shared.local_desktop import LocalDesktopConfigError, resolve_local_desktop_config
from scripts.local_desktop.config import sanitized_environment


def test_sanitized_environment_absolutizes_storage_paths_and_unifies_origin(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    values = sanitized_environment(
        config,
        base={
            "NEWS_RESEARCH_ROOT": ".newsroom/custom-research",
            "NEWS_RESEARCH_RUN_STORE_ROOT": ".newsroom/custom-runs",
            "NEWSROOM_PAPERS_DATA_PATH": ".newsroom/papers/arxiv-papers.json",
        },
    )

    assert values["NEWS_RESEARCH_ROOT"] == str((tmp_path / ".newsroom/custom-research").resolve())
    assert values["NEWS_RESEARCH_RUN_STORE_ROOT"] == str((tmp_path / ".newsroom/custom-runs").resolve())
    assert values["NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT"] == str((tmp_path / ".newsroom/custom-research/rag/transcripts").resolve())
    assert values["NEWSROOM_AUTH_USERS_PATH"] == str((tmp_path / ".newsroom/auth/users.json").resolve())
    assert values["NEWSROOM_AUTH_SESSIONS_PATH"] == str((tmp_path / ".newsroom/auth/sessions.json").resolve())
    assert values["NEWSROOM_RESEARCH_HISTORY_PATH"] == str((tmp_path / ".newsroom/research/history.json").resolve())
    assert values["NEWSROOM_RESEARCH_IMPORT_DIR"] == str((tmp_path / ".newsroom/research/imports").resolve())
    assert values["NEWSROOM_PAPERS_DATA_PATH"] == str((tmp_path / ".newsroom/papers/arxiv-papers.json").resolve())
    assert values["NEWSROOM_CANONICAL_ORIGIN"] == config.canonical_origin
    assert values["NEWSROOM_DESKTOP_CANONICAL_ORIGIN"] == config.canonical_origin


def test_sanitized_environment_rejects_public_secret(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    with pytest.raises(LocalDesktopConfigError) as raised:
        sanitized_environment(config, base={"NEXT_PUBLIC_API_TOKEN": "secret"})

    assert raised.value.reason_code == "desktop_public_secret_rejected"


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_sanitized_environment_rejects_browser_mock_data_mode(tmp_path: Path, value: str) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    with pytest.raises(LocalDesktopConfigError) as raised:
        sanitized_environment(
            config,
            base={"NEXT_PUBLIC_NEWSROOM_USE_MOCKS": value},
        )

    assert raised.value.reason_code == "desktop_mock_data_rejected"
    assert raised.value.field_name == "NEXT_PUBLIC_NEWSROOM_USE_MOCKS"


def test_sanitized_environment_removes_explicit_false_mock_switch(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    values = sanitized_environment(
        config,
        base={"NEXT_PUBLIC_NEWSROOM_USE_MOCKS": "false"},
    )

    assert "NEXT_PUBLIC_NEWSROOM_USE_MOCKS" not in values


def test_sanitized_environment_absolutizes_research_specific_models_config(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    values = sanitized_environment(
        config,
        base={"NEWS_RESEARCH_MODELS_CONFIG": "configs/research-models.yaml"},
    )

    assert values["NEWS_RESEARCH_MODELS_CONFIG"] == str(
        (tmp_path / "configs/research-models.yaml").resolve()
    )


def test_cli_data_and_artifact_roots_override_runtime_and_environment(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        root_env={
            "NEWS_RESEARCH_ROOT": "environment-data",
            "NEWS_RESEARCH_ARTIFACT_ROOT": "environment-artifacts",
        },
        runtime_config={
            "research_root": "runtime-data",
            "artifact_root": "runtime-artifacts",
        },
        cli_overrides={
            "research_root": "cli-data",
            "artifact_root": "cli-artifacts",
        },
    )

    assert config.research_root == (tmp_path / "cli-data").resolve()
    assert config.artifact_root == (tmp_path / "cli-artifacts").resolve()
    environment = sanitized_environment(config, base={})
    assert environment["NEWS_RESEARCH_ROOT"] == str(config.research_root)
    assert environment["NEWS_RESEARCH_ARTIFACT_ROOT"] == str(config.artifact_root)
