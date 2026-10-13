from __future__ import annotations

import pytest

from scripts.local_desktop.cli import _config_cli_overrides, build_parser, main


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
