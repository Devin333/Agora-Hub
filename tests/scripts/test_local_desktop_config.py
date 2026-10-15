from __future__ import annotations

from pathlib import Path

import pytest

from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.docker_pdf_parser import stage_pdf_for_docker
from framework.shared.env import env_values_from_root
from interfaces.composition.research_settings import research_storage_paths_from_env
from interfaces.events import audit_emitter_from_env
from interfaces.models import actor_context_from_headers
from framework.shared.local_desktop import LocalDesktopConfigError, resolve_local_desktop_config
from scripts.local_desktop.config import load_config, sanitized_environment


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


def test_sanitized_environment_binds_artifact_and_parser_writers_outside_packaged_app(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"runtime_root": "owned-runtime"},
    )
    packaged_app = tmp_path / "packaged" / "app"
    packaged_app.mkdir(parents=True)

    values = sanitized_environment(config, base={})
    research_paths = research_storage_paths_from_env(values, cwd=packaged_app)
    artifact_root = Path(values["NEWS_ARTIFACT_ROOT"])
    parser_run_root = Path(values["NEWSROOM_PARSER_RUN_ROOT"])

    assert artifact_root == research_paths.artifact_root
    assert artifact_root == Path(values["NEWS_RESEARCH_ARTIFACT_ROOT"])
    assert artifact_root.is_absolute()
    assert parser_run_root == (config.runtime_root / "parser-runs").resolve()
    assert parser_run_root.is_absolute()

    monkeypatch.chdir(packaged_app)
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("NEWSROOM_PARSER_RUN_ROOT", str(parser_run_root))
    manifest_path = ChunkManifestManager().write(
        "desktop-runtime-path-test",
        [],
        document_metadata={"source": "test"},
    )
    input_dir, output_dir, pdf_path = stage_pdf_for_docker(
        backend="marker",
        paper_id="desktop-runtime-path-test",
        pdf_bytes=b"synthetic-pdf-contract-test",
    )

    assert manifest_path.is_file()
    assert manifest_path.is_relative_to(artifact_root.parent)
    assert input_dir.is_relative_to(parser_run_root)
    assert output_dir.is_relative_to(parser_run_root)
    assert pdf_path.is_file()
    assert not (packaged_app / ".newsroom").exists()


def test_sanitized_environment_preserves_explicit_artifact_and_parser_paths(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"runtime_root": "owned-runtime"},
    )

    values = sanitized_environment(
        config,
        base={
            "NEWS_ARTIFACT_ROOT": "operator-artifacts/runs",
            "NEWSROOM_PARSER_RUN_ROOT": "operator-parser-runs",
        },
    )

    expected_artifact_root = (tmp_path / "operator-artifacts" / "runs").resolve()
    assert values["NEWS_ARTIFACT_ROOT"] == str(expected_artifact_root)
    assert values["NEWS_RESEARCH_ARTIFACT_ROOT"] == str(expected_artifact_root)
    assert values["NEWSROOM_PARSER_RUN_ROOT"] == str((tmp_path / "operator-parser-runs").resolve())


def test_sanitized_environment_uses_research_artifact_override_for_generic_writers(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    values = sanitized_environment(
        config,
        base={"NEWS_RESEARCH_ARTIFACT_ROOT": "research-owned/runs"},
    )

    expected = str((tmp_path / "research-owned" / "runs").resolve())
    assert values["NEWS_RESEARCH_ARTIFACT_ROOT"] == expected
    assert values["NEWS_ARTIFACT_ROOT"] == expected


def test_sanitized_environment_binds_default_audit_log_to_runtime_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"runtime_root": "owned-runtime"},
    )
    packaged_app = tmp_path / "packaged" / "app"
    packaged_app.mkdir(parents=True)

    values = sanitized_environment(config, base={})
    expected_path = (config.runtime_root / "audit" / "audit.jsonl").resolve()

    assert Path(values["NEWS_AUDIT_LOG_PATH"]) == expected_path
    assert expected_path.is_absolute()

    monkeypatch.chdir(packaged_app)
    emitter = audit_emitter_from_env(env=values)
    record = emitter.emit(
        actor=actor_context_from_headers(
            {"X-News-Actor": "desktop-acceptance", "X-News-Actor-Type": "system"},
            request_id="audit-runtime-path-test",
        ),
        action="desktop_runtime_path_verified",
        resource_type="local_desktop",
        result="succeeded",
        metadata={"source": "test"},
    )

    assert record is not None
    assert expected_path.is_file()
    assert '"action": "desktop_runtime_path_verified"' in expected_path.read_text(encoding="utf-8")
    assert not (packaged_app / ".newsroom").exists()


def test_sanitized_environment_preserves_explicit_audit_log_path(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"runtime_root": "owned-runtime"},
    )
    explicit_path = Path("operator-configured/audit.jsonl")

    values = sanitized_environment(
        config,
        base={"NEWS_AUDIT_LOG_PATH": str(explicit_path)},
    )

    assert values["NEWS_AUDIT_LOG_PATH"] == str((tmp_path / explicit_path).resolve())


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


def test_storage_mode_is_strict_runtime_configuration_without_root_environment_override(tmp_path: Path) -> None:
    default_config = resolve_local_desktop_config(
        project_root=tmp_path,
        root_env={"NEWSROOM_DESKTOP_STORAGE_MODE": "owned-local"},
    )
    owned_config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"storage_mode": "owned-local"},
    )

    assert default_config.storage_mode == "root-environment"
    assert owned_config.storage_mode == "owned-local"
    assert owned_config.to_dict()["storage_mode"] == "owned-local"

    with pytest.raises(LocalDesktopConfigError) as raised:
        resolve_local_desktop_config(
            project_root=tmp_path,
            runtime_config={"storage_mode": "automatic"},
        )

    assert raised.value.reason_code == "desktop_storage_mode_invalid"


def test_owned_local_storage_shadows_root_dsn_without_mutating_input_or_reload(tmp_path: Path, monkeypatch) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"storage_mode": "owned-local"},
    )
    root_environment = {"NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom"}

    child = sanitized_environment(config, base=root_environment)

    assert root_environment["NEWS_DATABASE_DSN"] == "postgresql://local.invalid/newsroom"
    assert child["NEWS_DATABASE_DSN"] == ""
    assert child["NEWSROOM_DESKTOP_ROOT_DATABASE_DSN_SHADOWED"] == "1"

    (tmp_path / ".env").write_text(
        "NEWS_DATABASE_DSN=postgresql://root.invalid/newsroom\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("framework.shared.env._repo_root", lambda: tmp_path)
    monkeypatch.setenv("NEWS_DATABASE_DSN", child["NEWS_DATABASE_DSN"])

    reloaded = env_values_from_root()

    assert reloaded["NEWS_DATABASE_DSN"] == ""


def test_root_environment_storage_keeps_external_database_selection(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(project_root=tmp_path)

    values = sanitized_environment(
        config,
        base={"NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom"},
    )

    assert values["NEWS_DATABASE_DSN"] == "postgresql://local.invalid/newsroom"
    assert "NEWSROOM_DESKTOP_ROOT_DATABASE_DSN_SHADOWED" not in values


def test_owned_local_storage_rejects_enabled_postgres_only_memory(tmp_path: Path) -> None:
    config = resolve_local_desktop_config(
        project_root=tmp_path,
        runtime_config={"storage_mode": "owned-local"},
    )

    with pytest.raises(LocalDesktopConfigError) as raised:
        sanitized_environment(
            config,
            base={
                "NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom",
                "NEWS_MEMORY_POSTGRES_ENABLED": "true",
            },
        )

    assert raised.value.reason_code == "desktop_owned_local_storage_conflict"
    assert raised.value.field_name == "NEWS_MEMORY_POSTGRES_ENABLED"


def test_load_config_reads_owned_local_mode_from_default_runtime_json(tmp_path: Path, monkeypatch) -> None:
    runtime_root = tmp_path / ".newsroom" / "desktop"
    runtime_root.mkdir(parents=True)
    (runtime_root / "config.json").write_text(
        '{"schema":"newsroom.local-desktop-config/v1","storage_mode":"owned-local"}',
        encoding="utf-8",
    )
    monkeypatch.setattr("scripts.local_desktop.config.project_root", lambda: tmp_path)

    config = load_config(environ={"NEWS_DATABASE_DSN": "postgresql://local.invalid/newsroom"})

    assert config.runtime_config_path == runtime_root / "config.json"
    assert config.storage_mode == "owned-local"
