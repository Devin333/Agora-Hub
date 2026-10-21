from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.research.ports.evaluation_dataset import DatasetBundleBytes
from interfaces.cli.commands import rag_eval
from tests.backend.research.rag.test_protocol_dataset_io import _frozen_bundle


def _write_bundle(root: Path, objects: DatasetBundleBytes) -> None:
    revision_dir = root / "fixture-dataset" / "1.0.0"
    revision_dir.mkdir(parents=True)
    (revision_dir / "manifest.json").write_bytes(objects.manifest_json)
    (revision_dir / "questions.jsonl").write_bytes(objects.questions_jsonl)
    (revision_dir / "references.jsonl").write_bytes(objects.references_jsonl)


def _argv(root: Path, *extra: str) -> list[str]:
    return [
        "rag-eval",
        "validate-dataset",
        "--dataset-root",
        str(root),
        "--dataset-id",
        "fixture-dataset",
        "--dataset-version",
        "1.0.0",
        *extra,
    ]


def test_validate_dataset_real_files_roundtrip_is_contract_success_not_quality_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, objects = _frozen_bundle()
    _write_bundle(tmp_path, objects)

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("validate-dataset must not access the network")

    monkeypatch.setattr(socket.socket, "connect", deny_network)

    assert rag_eval.main(_argv(tmp_path, "--require-frozen")) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["scope"] == "dataset_contract"
    assert payload["valid"] is True
    assert payload["ready_for_execution_data"] is True
    assert payload["formal_ready"] is False
    assert payload["issues"] == []
    assert [item["code"] for item in payload["readiness_requirements"]] == [
        "readiness.source_corpus_validation_required"
    ]
    assert payload["quality_gate"]["evaluated"] is False
    assert payload["quality_gate"]["passed"] is None
    assert payload["network_calls"] == 0
    assert payload["paid_calls"] == 0
    assert payload["diagnostics"]["schema"]["status"] == "validated"
    assert payload["diagnostics"]["review"]["status"] == "approved"
    assert payload["diagnostics"]["materials"]["source_count"] == 1
    assert payload["diagnostics"]["split"]["case_counts"] == {"dev": 1}
    assert payload["diagnostics"]["summary"]["case_count"] == 1
    assert payload["diagnostics"]["mapping"]["status"] == "not_evaluated"


def test_validate_dataset_delegates_to_application_service(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, objects = _frozen_bundle()
    _write_bundle(tmp_path, objects)
    calls: list[tuple[object, object]] = []

    class RecordingService:
        def validate_dataset(self, revision, *, mode):
            calls.append((revision, mode))
            from backend.research.rag.evaluation.protocol_dataset_io import load_dataset_bundle

            return load_dataset_bundle(objects, mode=mode)

        def describe_dataset(self, result):
            return {"projected_by": "application_service"}

    monkeypatch.setattr(
        rag_eval,
        "build_evaluation_application_service",
        lambda *, dataset_root: RecordingService(),
    )

    assert rag_eval.main(_argv(tmp_path, "--require-frozen")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["diagnostics"] == {"projected_by": "application_service"}
    assert len(calls) == 1
    revision, mode = calls[0]
    assert revision.dataset_id == "fixture-dataset"
    assert revision.dataset_version == "1.0.0"
    assert mode.value == "frozen"


def test_runtime_service_type_error_is_not_misreported_as_invalid_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenService:
        def validate_dataset(self, revision, *, mode):
            raise TypeError("DatasetReadPort returned an invalid runtime object")

    monkeypatch.setattr(
        rag_eval,
        "build_evaluation_application_service",
        lambda *, dataset_root: BrokenService(),
    )

    with pytest.raises(TypeError, match="invalid runtime object"):
        rag_eval.main(_argv(tmp_path))



def test_validate_dataset_tamper_returns_structured_nonzero_without_secret_echo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, objects = _frozen_bundle()
    secret = "TOP-SECRET-REFERENCE-CONTENT"
    tampered = DatasetBundleBytes(
        manifest_json=objects.manifest_json,
        questions_jsonl=objects.questions_jsonl,
        references_jsonl=(secret + " not-json\n").encode(),
    )
    _write_bundle(tmp_path, tampered)

    assert rag_eval.main(_argv(tmp_path, "--require-frozen")) == 1
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["valid"] is False
    assert payload["formal_ready"] is False
    assert payload["issues"]
    assert payload["quality_gate"]["evaluated"] is False
    assert secret not in output
    assert str(tmp_path) not in output


def test_validate_dataset_missing_object_returns_sanitized_read_issue(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, objects = _frozen_bundle()
    _write_bundle(tmp_path, objects)
    (tmp_path / "fixture-dataset" / "1.0.0" / "references.jsonl").unlink()

    assert rag_eval.main(_argv(tmp_path)) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["issues"] == [
        {
            "code": "dataset.object_unavailable",
            "location": "references.jsonl",
            "reason": "required dataset object is unavailable",
        }
    ]
    assert str(tmp_path) not in output


def test_validate_dataset_rejects_floating_revision_as_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _argv(tmp_path)
    argv[argv.index("1.0.0")] = "latest"

    assert rag_eval.main(argv) == 2
    payload = json.loads(capsys.readouterr().out)

    assert payload["issues"][0]["code"] == "dataset.invalid_revision"
    assert payload["issues"][0]["location"] == "revision"
    assert payload["dataset"] == {
        "dataset_id": "<invalid>",
        "dataset_version": "<invalid>",
    }


def test_help_registers_only_validate_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    parser = rag_eval.build_parser()
    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["rag-eval", "--help"])

    assert captured.value.code == 0
    help_text = capsys.readouterr().out
    assert "validate-dataset" in help_text
    for unavailable in ("run", "score", "export-review", "import-review", "report"):
        assert f"{{{unavailable}" not in help_text
        assert f"  {unavailable} " not in help_text



