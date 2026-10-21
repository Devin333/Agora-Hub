from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest
import yaml

from scripts import dev


def test_ci_workflow_fetches_history_for_release_policy_ancestry() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]

    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["fetch-depth"] == 0


def _assert_offline_rag_ci_contract(steps: list[dict[str, Any]]) -> None:
    # Both commands are offline regression checks, not production promotion.
    required_commands = {
        "Run offline RAG diagnostic gate": "python -m scripts.dev test-rag-eval-gate",
        "Run offline RAG evaluation protocol contracts": (
            "python -m scripts.dev test-rag-eval-contracts"
        ),
    }
    gate_positions = []
    for name, command in required_commands.items():
        matches = [index for index, step in enumerate(steps) if step.get("name") == name]
        assert len(matches) == 1, f"Expected exactly one CI step: {name}"
        index = matches[0]
        step = steps[index]
        assert step.get("run") == command, f"Unexpected CI command: {name}"
        assert "if" not in step, f"Offline gate must not be conditional: {name}"
        assert step.get("continue-on-error", False) is False
        gate_positions.append(index)

    uploads = [
        (index, step)
        for index, step in enumerate(steps)
        if step.get("name") == "Upload offline RAG protocol test artifacts"
    ]
    assert len(uploads) == 1, "Expected exactly one offline protocol artifact upload"
    upload_index, upload = uploads[0]
    assert upload.get("uses") == "actions/upload-artifact@v4"
    assert upload.get("if") == "${{ always() }}"
    artifact = upload.get("with", {})
    assert artifact.get("name") == "rag-eval-protocol-fixture-contracts"
    assert artifact.get("path") == ".newsroom/eval/protocol-contracts"
    assert artifact.get("path") == dev.RAG_EVAL_CONTRACT_ARTIFACT_ROOT
    assert artifact.get("if-no-files-found") == "warn"
    assert gate_positions[0] < gate_positions[1] < upload_index


def test_ci_workflow_runs_offline_rag_diagnostic_and_protocol_contract_gates() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    _assert_offline_rag_ci_contract(workflow["jobs"]["test"]["steps"])


@pytest.mark.parametrize("command", ["test-rag-eval-gate", "test-rag-eval-contracts"])
def test_ci_offline_rag_commands_are_registered(command: str) -> None:
    assert dev.build_parser().parse_args([command]).command == command
    assert dev._rag_eval_gate_command() == [
        sys.executable,
        "-m",
        "backend.research.rag.cli.run_ci_eval_gate",
    ]


@pytest.mark.parametrize(
    "missing_step",
    [
        "Run offline RAG diagnostic gate",
        "Run offline RAG evaluation protocol contracts",
        "Upload offline RAG protocol test artifacts",
    ],
)
def test_ci_offline_rag_contract_rejects_missing_steps(missing_step: str) -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = [
        step for step in workflow["jobs"]["test"]["steps"]
        if step.get("name") != missing_step
    ]
    with pytest.raises(AssertionError):
        _assert_offline_rag_ci_contract(steps)


@pytest.mark.parametrize(
    ("step_name", "field", "replacement"),
    [
        ("Run offline RAG diagnostic gate", "run", "python -m scripts.dev compile"),
        (
            "Run offline RAG evaluation protocol contracts",
            "run",
            "python -m scripts.dev test-rag-eval-gate",
        ),
        ("Run offline RAG diagnostic gate", "continue-on-error", True),
        ("Run offline RAG evaluation protocol contracts", "if", "${{ false }}"),
        ("Run offline RAG diagnostic gate", "name", "Run RAG eval promotion gate"),
        ("Upload offline RAG protocol test artifacts", "if", None),
        ("Upload offline RAG protocol test artifacts", "if", "${{ success() }}"),
        ("Upload offline RAG protocol test artifacts", "uses", "actions/cache@v4"),
    ],
)
def test_ci_offline_rag_contract_rejects_invalid_steps(
    step_name: str, field: str, replacement: Any,
) -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    step = next(step for step in steps if step.get("name") == step_name)
    if replacement is None:
        step.pop(field)
    else:
        step[field] = replacement
    with pytest.raises(AssertionError):
        _assert_offline_rag_ci_contract(steps)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("path", ".newsroom/eval/ci-gate"),
        ("name", "rag-eval-production-promotion"),
    ],
)
def test_ci_offline_rag_contract_rejects_misclassified_artifacts(
    field: str, replacement: str,
) -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]
    upload = next(
        step for step in steps
        if step.get("name") == "Upload offline RAG protocol test artifacts"
    )
    upload["with"][field] = replacement
    with pytest.raises(AssertionError):
        _assert_offline_rag_ci_contract(steps)


def test_ci_workflow_runs_registered_prd_daily_sweep() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]

    assert any(
        step.get("name") == "Run PRD-aligned daily regression sweep"
        and step.get("run") == "python -m scripts.dev test-prd-daily"
        for step in steps
    )


def test_ci_workflow_runs_durable_event_compatibility_gate() -> None:
    workflow = yaml.safe_load(
        Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["test"]["steps"]

    assert any(
        step.get("name") == "Run durable event compatibility gate"
        and step.get("run")
        == "python -m pytest tests/infrastructure/storage/events/"
        "test_durable_event_compatibility_release.py -q"
        for step in steps
    )
