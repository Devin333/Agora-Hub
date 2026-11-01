from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import scripts.dev as dev

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _addopts_marker_terms() -> set[str]:
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    addopts = config["tool"]["pytest"]["ini_options"]["addopts"]
    return {term.strip() for term in addopts[addopts.index("-m") + 1].split(" and ")}


def test_ordinary_pytest_excludes_opt_in_tool_layers() -> None:
    assert {"not tool_stress", "not tool_live"} <= _addopts_marker_terms()


@pytest.mark.parametrize(
    ("layer", "expression"),
    [
        ("fault", "not tool_stress and not tool_live"),
        ("stress", "tool_stress"),
        ("all", "not tool_live"),
    ],
)
def test_tool_stability_command_selects_its_layer(layer: str, expression: str) -> None:
    command = dev._tool_stability_command(layer)

    assert dev.TOOL_STABILITY_TESTS in command
    assert ("-m", expression) in set(zip(command, command[1:]))


def test_tool_stability_command_is_registered_with_its_layers() -> None:
    args = dev.build_parser().parse_args(["tool-stability", "--layer", "stress"])

    assert args.command == "tool-stability"
    assert args.layer == "stress"
    assert dev.build_parser().parse_args(["tool-stability"]).layer == "fault"


def test_smoke_gate_runs_the_tool_fault_layer() -> None:
    source = (PROJECT_ROOT / "scripts" / "dev.py").read_text(encoding="utf-8")
    smoke_block = source.split('if args.command == "smoke":', 1)[1].split("return _run_many", 1)[0]

    assert "TOOL_STABILITY_TESTS" in smoke_block
