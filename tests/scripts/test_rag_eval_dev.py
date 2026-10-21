from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.dev as dev
from backend.research.ports.evaluation_dataset import DatasetBundleBytes
from tests.backend.research.rag.test_protocol_dataset_io import _frozen_bundle


def _write_bundle(root: Path, objects: DatasetBundleBytes) -> None:
    revision_dir = root / "fixture-dataset" / "1.0.0"
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(objects.manifest_json)
    (revision_dir / "questions.jsonl").write_bytes(objects.questions_jsonl)
    (revision_dir / "references.jsonl").write_bytes(objects.references_jsonl)


def test_scripts_dev_rag_eval_runs_in_process_without_loading_live_environment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, objects = _frozen_bundle()
    _write_bundle(tmp_path, objects)

    def reject_environment_load() -> dict[str, str]:
        raise AssertionError("validate-dataset must not read live credentials")

    monkeypatch.setattr(dev, "_command_env", reject_environment_load)

    exit_code = dev.main(
        [
            "rag-eval",
            "validate-dataset",
            "--dataset-root",
            str(tmp_path),
            "--dataset-id",
            "fixture-dataset",
            "--dataset-version",
            "1.0.0",
            "--require-frozen",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scope"] == "dataset_contract"
    assert payload["valid"] is True
    assert payload["formal_ready"] is False


def test_scripts_dev_help_exposes_only_implemented_rag_eval_operation(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = dev.build_parser()
    args = parser.parse_args(
        [
            "rag-eval",
            "validate-dataset",
            "--dataset-root",
            "datasets",
            "--dataset-id",
            "dataset-a",
            "--dataset-version",
            "1.0.0",
        ]
    )
    assert callable(args.handler)

    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["rag-eval", "--help"])
    assert captured.value.code == 0
    help_text = capsys.readouterr().out
    assert "validate-dataset" in help_text
    assert "run" not in help_text
    assert "score" not in help_text


def test_existing_scripts_dev_parser_command_remains_available() -> None:
    args = dev.build_parser().parse_args(["compile"])
    assert args.command == "compile"




def test_fixture_protocol_gate_does_not_load_or_forward_live_credentials(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(dev, "RAG_EVAL_CONTRACT_ARTIFACT_ROOT", str(tmp_path / "contract-tests"))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-test-process")
    monkeypatch.setenv("OPENAI_BASE_URL", "must-not-reach-test-process")
    monkeypatch.setenv("OTHER_API_KEY", "must-not-reach-test-process")
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setattr(dev, "_command_env", lambda: (_ for _ in ()).throw(AssertionError("no .env reads")))
    calls = []

    def run(command, *, env):
        calls.append((command, env))
        return 3

    monkeypatch.setattr(dev, "_run", run)
    assert dev.main(["test-rag-eval-contracts"]) == 3
    assert len(calls) == 1
    command, env = calls[0]
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env
    assert "OTHER_API_KEY" not in env
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["NEWS_RAG_EVAL_EXECUTION_PROFILE"] == "fixture"
    assert "tests/backend/research/rag/test_protocol_dataset.py" in command
    assert "tests/backend/research/rag/test_protocol_execution.py" in command
    assert "tests/backend/research/rag/test_protocol_scoring.py" in command
    assert "tests/architecture/test_infrastructure_boundary.py" in command
    artifact = json.loads((tmp_path / "contract-tests/classification.json").read_text(encoding="utf-8"))
    assert artifact["scope"] == "fixture_contract_tests"
    assert artifact["product_quality_measured"] is False
    assert artifact["production_qualification"] is False


def test_ci_keeps_fixture_protocol_artifacts_on_failure() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python -m scripts.dev test-rag-eval-contracts" in workflow
    upload = workflow.split("- name: Upload offline RAG protocol test artifacts", 1)[1]
    assert "if: ${{ always() }}" in upload
    assert "name: rag-eval-protocol-fixture-contracts" in upload
    assert "path: .newsroom/eval/protocol-contracts" in upload
