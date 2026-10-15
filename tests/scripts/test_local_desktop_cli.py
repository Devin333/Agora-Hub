from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.local_desktop.cli import _config_cli_overrides, build_parser, dispatch, main


def test_cli_exposes_required_operations() -> None:
    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert {
        "doctor",
        "build",
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "backup",
        "restore",
        "rollback",
        "open",
        "desktop-entry",
        "autostart",
    } <= set(commands)


def test_json_flag_is_supported_before_or_after_command() -> None:
    assert build_parser().parse_args(["--json", "status"]).json_output is True
    assert build_parser().parse_args(["status", "--json"]).json_output is True


def test_restore_activation_is_explicit() -> None:
    args = build_parser().parse_args(["restore", "backup.zip"])
    assert args.activate is False


def test_cli_configuration_overrides_are_supported_before_or_after_command() -> None:
    before = build_parser().parse_args([
        "--frontend-port", "3300",
        "--canonical-origin", "http://127.0.0.1:3300",
        "--data-root", "data",
        "--artifact-root", "artifacts",
        "status",
    ])
    after = build_parser().parse_args([
        "status",
        "--frontend-port", "3300",
        "--canonical-origin", "http://127.0.0.1:3300",
        "--data-root", "data",
        "--artifact-root", "artifacts",
    ])

    expected = {
        "frontend_port": 3300,
        "canonical_origin": "http://127.0.0.1:3300",
        "research_root": "data",
        "artifact_root": "artifacts",
    }
    assert _config_cli_overrides(before) == expected
    assert _config_cli_overrides(after) == expected


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "301"])
def test_cli_rejects_unbounded_lifecycle_timeouts(value: str) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["start", "--readiness-timeout", value])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["stop", "--process-timeout", value])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["stop", "--drain-timeout", value])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["restart", "--readiness-timeout", value])
    with pytest.raises(SystemExit):
        build_parser().parse_args(["restart", "--drain-timeout", value])


def test_main_passes_cli_configuration_to_single_resolver(monkeypatch) -> None:
    captured = {}
    sentinel = object()

    def load(*, cli_overrides):
        captured.update(cli_overrides)
        return sentinel

    monkeypatch.setattr("scripts.local_desktop.cli.load_config", load)
    monkeypatch.setattr(
        "scripts.local_desktop.cli.dispatch",
        lambda args, config: {"status": "ok", "config_is_sentinel": config is sentinel},
    )

    exit_code = main([
        "--deployment-profile", "local-desktop",
        "status",
        "--frontend-port", "3300",
        "--canonical-origin", "http://127.0.0.1:3300",
        "--research-root", "data",
        "--artifact-root", "artifacts",
        "--json",
    ])

    assert exit_code == 0
    assert captured == {
        "deployment_profile": "local-desktop",
        "frontend_port": 3300,
        "canonical_origin": "http://127.0.0.1:3300",
        "research_root": "data",
        "artifact_root": "artifacts",
    }


@pytest.mark.parametrize("command", ["doctor", "inventory"])
def test_json_cli_stdout_is_a_single_json_document(command: str, tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    runtime_root = tmp_path / "runtime"
    release_root = tmp_path / "releases"
    backup_root = tmp_path / "backups"
    data_root = tmp_path / "research"
    artifact_root = tmp_path / "artifacts"
    environment = dict(os.environ)
    environment.update(
        {
            "NEWSROOM_DESKTOP_CONFIG_PATH": str(runtime_root / "config.json"),
            "NEWSROOM_AUTH_USERS_PATH": str(data_root / "auth" / "users.json"),
            "NEWSROOM_AUTH_SESSIONS_PATH": str(data_root / "auth" / "sessions.json"),
            "NEWSROOM_AUTH_OTP_PATH": str(data_root / "auth" / "otp.json"),
            "NEWSROOM_AUTH_OAUTH_PATH": str(data_root / "auth" / "oauth.json"),
            "NEWSROOM_RESEARCH_HISTORY_PATH": str(data_root / "history.json"),
            "NEWSROOM_RESEARCH_IMPORT_DIR": str(data_root / "imports"),
            "NEWS_RESEARCH_RUN_STORE_ROOT": str(data_root / "runs"),
            "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(data_root / "chunks"),
            "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(data_root / "rag" / "transcripts"),
            "NEWS_RESEARCH_MODELS_CONFIG": str(data_root / "models.yaml"),
            "NEWS_MODELS_CONFIG": str(data_root / "models.yaml"),
            "NEWS_SOURCES_CONFIG": str(data_root / "sources.yaml"),
            "NEWS_AUDIT_LOG_PATH": str(data_root / "audit.jsonl"),
            "NEWS_VISUAL_IMAGE_ROOT": str(data_root / "visual" / "images"),
            "NEWS_VISUAL_DESCRIPTION_IMAGE_ROOT": str(data_root / "visual" / "descriptions"),
            "NEWSROOM_PARSER_RUN_ROOT": str(data_root / "parser-runs"),
            "NEWSROOM_MARKER_CACHE_DIR": str(data_root / "marker-cache"),
            "NEWSROOM_MINERU_CACHE_DIR": str(data_root / "mineru-cache"),
            "NEWSROOM_MINERU_CONFIG_DIR": str(data_root / "mineru-config"),
            "NEWSROOM_PAPERS_DATA_PATH": str(data_root / "papers.json"),
            "NEWS_DATABASE_DSN": "",
            "NEWS_MEMORY_POSTGRES_ENABLED": "",
            "NEXT_PUBLIC_NEWSROOM_USE_MOCKS": "",
            "NEWS_RESEARCH_RAG_BACKEND": "local",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.local_desktop",
            "--json",
            "--runtime-root",
            str(runtime_root),
            "--release-root",
            str(release_root),
            "--backup-root",
            str(backup_root),
            "--control-path",
            str(data_root / "control.json"),
            "--data-root",
            str(data_root),
            "--artifact-root",
            str(artifact_root),
            "--parser-mode",
            "pymupdf",
            command,
        ],
        cwd=project_root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode in {0, 2}
    payload = json.loads(completed.stdout)
    assert isinstance(payload, dict)
    assert payload.get("status") in {"ok", "warning", "error"} or payload.get("ok") is False
    if command == "doctor":
        release_check = next(item for item in payload["checks"] if item["check_id"] == "production_release")
        assert release_check["reason_code"] == "desktop_release_not_prepared"
    else:
        assert payload["owners"]
        assert all(Path(item["source_path"]).is_relative_to(tmp_path) for item in payload["owners"])


def test_dispatch_uses_one_sanitized_environment_for_doctor_and_inventory(monkeypatch) -> None:
    captured: dict[str, object] = {}
    config = object()
    effective = {"NEWS_DATABASE_DSN": "", "NEWSROOM_DESKTOP_ROOT_DATABASE_DSN_SHADOWED": "1"}

    monkeypatch.setattr("scripts.local_desktop.cli.sanitized_environment", lambda actual: effective if actual is config else pytest.fail("unexpected config"))

    class FakeDoctor:
        def __init__(self, actual_config, *, environ):
            assert actual_config is config
            captured["doctor_environment"] = environ

    class FakeLifecycle:
        def __init__(self, actual_config, *, doctor):
            assert actual_config is config
            self.doctor = doctor

    class FakeInventoryResolver:
        def __init__(self, actual_config, *, environ):
            assert actual_config is config
            captured["inventory_environment"] = environ

        def resolve(self, *, require_existing):
            assert require_existing is False
            return type("Inventory", (), {"schema": "inventory/v1", "owners": ()})()

    monkeypatch.setattr("scripts.local_desktop.cli.LocalDesktopDoctor", FakeDoctor)
    monkeypatch.setattr("scripts.local_desktop.cli.LifecycleManager", FakeLifecycle)
    monkeypatch.setattr("scripts.local_desktop.cli.StorageInventoryResolver", FakeInventoryResolver)

    result = dispatch(build_parser().parse_args(["inventory"]), config)

    assert result["status"] == "ok"
    assert captured == {
        "doctor_environment": effective,
        "inventory_environment": effective,
    }
