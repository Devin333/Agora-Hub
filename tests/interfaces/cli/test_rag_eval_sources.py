from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from backend.research.ports.evaluation_dataset import DatasetRevision
from backend.research.rag.evaluation.protocol_dataset_io import serialize_dataset_bundle
from infrastructure.research.evaluation_dataset import FilesystemEvaluationDatasetPublisher
from interfaces.cli.commands import rag_eval
from tests.backend.research.application.test_evaluation_sources_application import (
    ATTACHMENT,
    build_mapping,
    validation_fixture,
    write_corpus,
    write_mapping,
)
from backend.research.rag.evaluation.protocol_corpus import sha256_bytes


def _write_all(tmp_path: Path, *, missing_attachment: bool = False, stale: bool = False):
    manifest, cases, corpus, materials = validation_fixture()
    dataset_root = tmp_path / "datasets"
    corpus_root = tmp_path / "corpus"
    mapping_root = tmp_path / "mapping"
    dataset_root.mkdir()
    corpus_root.mkdir()
    mapping_root.mkdir()
    revision = DatasetRevision(manifest.dataset_id, manifest.dataset_version)
    FilesystemEvaluationDatasetPublisher(dataset_root).publish_dataset_objects(
        revision,
        serialize_dataset_bundle(manifest, cases),
        dataset_digest=manifest.dataset_digest,
    )
    if missing_attachment:
        materials.pop(sha256_bytes(ATTACHMENT))
    write_corpus(corpus_root, corpus, materials)
    write_mapping(
        mapping_root,
        manifest,
        cases[0].case_id,
        build_mapping(manifest, cases[0], stale_dataset_digest=stale),
    )
    return dataset_root, corpus_root, mapping_root


def _argv(dataset_root: Path, corpus_root: Path, mapping_root: Path) -> list[str]:
    return [
        "rag-eval",
        "validate-dataset",
        "--dataset-root",
        str(dataset_root),
        "--dataset-id",
        "fixture-dataset",
        "--dataset-version",
        "1.0.0",
        "--require-frozen",
        "--corpus-root",
        str(corpus_root),
        "--require-complete-corpus",
        "--mapping-root",
        str(mapping_root),
    ]


def test_cli_validates_real_corpus_and_mapping_without_network_or_paid_calls(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = _write_all(tmp_path)

    def deny_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("validate-dataset must remain offline")

    monkeypatch.setattr(socket.socket, "connect", deny_network)

    assert rag_eval.main(_argv(*roots)) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["valid"] is True
    assert payload["preflight_complete"] is True
    assert payload["formal_ready"] is False
    assert payload["diagnostics"]["source_corpus"]["status"] == "verified"
    assert payload["diagnostics"]["mapping"]["status"] == "valid"
    assert payload["diagnostics"]["mapping"]["actual_index_binding"]["status"] == "not_evaluated"
    assert payload["quality_gate"]["evaluated"] is False
    assert payload["network_calls"] == 0
    assert payload["paid_calls"] == 0


def test_cli_missing_source_bytes_is_needs_review_and_nonzero_not_negative_case(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots = _write_all(tmp_path, missing_attachment=True)

    assert rag_eval.main(_argv(*roots)) == 1
    payload = json.loads(capsys.readouterr().out)

    corpus = payload["diagnostics"]["source_corpus"]
    assert payload["valid"] is True
    assert payload["preflight_status"] == "needs_review"
    assert payload["preflight_complete"] is False
    assert corpus["planned_case_ids"] == ["case-001"]
    assert corpus["case_statuses"] == [
        {"case_id": "case-001", "disposition": "needs_review"}
    ]
    assert payload["paid_calls"] == 0


def test_cli_rejects_stale_mapping_and_help_exposes_no_empty_run_or_score(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roots = _write_all(tmp_path, stale=True)

    assert rag_eval.main(_argv(*roots)) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["diagnostics"]["mapping"]["status"] == "invalid"
    assert {item["code"] for item in payload["issues"]} == {"mapping.binding_stale"}

    parser = rag_eval.build_parser()
    with pytest.raises(SystemExit) as captured:
        parser.parse_args(["rag-eval", "--help"])
    assert captured.value.code == 0
    help_text = capsys.readouterr().out
    assert "validate-dataset" in help_text
    assert "run" not in help_text
    assert "score" not in help_text
