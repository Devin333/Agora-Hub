from __future__ import annotations

import json
import shutil
import socket
import tempfile
from pathlib import Path

import numpy as np
import pytest

from interfaces.cli.commands import qasper_eval


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        pytest.fail("QASPER CLI integration must remain offline")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


@pytest.fixture
def short_tmp_path() -> Path:
    path = Path(tempfile.mkdtemp(prefix="qcli-"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def _fixture_files(tmp_path: Path) -> tuple[Path, Path]:
    dataset = tmp_path / "qasper.json"
    dataset.write_text(json.dumps({
        "paper-1": {
            "title": "Local QASPER Paper",
            "abstract": "",
            "full_text": [{
                "section_name": "Results",
                "paragraphs": ["Evidence supports the result."],
            }],
            "qas": [{
                "question_id": "q1",
                "question": "What evidence supports the result?",
                "question_type": "extractive",
                "answers": [{
                    "answer": {
                        "free_form_answer": "Evidence supports the result.",
                        "evidence": ["Evidence supports the result."],
                    }
                }],
            }],
        }
    }), encoding="utf-8")
    cache = tmp_path / "cache.npz"
    # BM25-only execution still binds an explicit cache identity but never reads
    # vectors for retrieval, so an empty, shape-valid local archive is sufficient.
    np.savez(
        cache,
        keys=np.asarray([], dtype=str),
        vectors=np.ones((0, 2), dtype=np.float32),
    )
    return dataset, cache


def _invoke(capsys: pytest.CaptureFixture[str], dataset: Path, cache: Path, *args: str) -> tuple[int, dict]:
    code = qasper_eval.main([
        "qasper", "--dataset-path", str(dataset), "--cache-path", str(cache), *args,
    ])
    return code, json.loads(capsys.readouterr().out)


def test_real_cli_composition_application_and_filesystem_round_trip(
    capsys: pytest.CaptureFixture[str], short_tmp_path: Path
) -> None:
    dataset, cache = _fixture_files(short_tmp_path)
    mapping = short_tmp_path / "mapping"
    run = short_tmp_path / "ablation"
    sweep = short_tmp_path / "sweep"

    code, frozen = _invoke(capsys, dataset, cache, "mapping", "freeze", "--output-dir", str(mapping))
    assert code == 0
    assert frozen["operation"] == "freeze_mapping"
    assert (mapping / "mapping.json").is_file()

    code, validated = _invoke(capsys, dataset, cache, "mapping", "validate", "--mapping-dir", str(mapping))
    assert code == 0
    assert validated["valid"] is True
    assert type(validated["case_count"]) is int and validated["case_count"] == 1

    code, ablation = _invoke(
        capsys, dataset, cache, "ablation", "run",
        "--output-dir", str(run), "--mapping-dir", str(mapping),
        "--profile", "bm25_only", "--candidate-limit", "5", "--overfetch", "1",
        "--rrf-k", "60", "--rerank", "false", "--threshold", "0.25",
        "--weights", '{"bm25":1.0}',
    )
    assert code == 0, ablation
    assert ablation["valid"] is True
    assert ablation["profile"] == "bm25_only"
    assert type(ablation["planned"]) is int and ablation["planned"] == 1
    assert type(ablation["valid_cases"]) is int
    assert ablation["external_requests"] == 0
    assert ablation["external_cost"] is None

    code, swept = _invoke(
        capsys, dataset, cache, "sweep", "--output-dir", str(sweep),
        "--mapping-dir", str(mapping), "--profiles", "bm25_only",
        "--candidate-limit-grid", "[5]", "--overfetch-grid", "[1]",
        "--rrf-k-grid", "[60]", "--rerank-grid", "[false]",
        "--threshold-grid", "[0.25]", "--weights-grid", '[{"bm25":1.0}]',
    )
    assert code == 0
    assert swept["valid"] is True
    assert swept["profiles"] == ["bm25_only"]
    assert type(swept["experiments"]) is int and swept["experiments"] == 1
    assert swept["external_requests"] == 0

    code, scored = _invoke(capsys, dataset, cache, "score", "--run-dir", str(run))
    assert code == 0
    assert scored["valid"] is True
    assert type(scored["validated_cases"]) is int and scored["validated_cases"] == 1
    assert (run / "score.json").is_file()

    code, reported = _invoke(capsys, dataset, cache, "report", "--run-dir", str(run))
    assert code == 0
    assert reported["valid"] is True
    assert reported["report_artifact"] == "report.json"
    assert (run / "report.json").is_file()

    run_index = json.loads((run / "run-index.json").read_text(encoding="utf-8"))
    assert len(run_index["profiles"]) == 1
    report = json.loads((run / f"{run_index['profiles'][0]}-report.json").read_text(encoding="utf-8"))
    denominators = report["denominators"]
    assert all(type(denominators[key]) is int for key in (
        "planned", "valid", "needs_review", "system_failure", "not_applicable",
    ))
    assert denominators["planned"] == 1
    assert denominators["system_failure"] == 0

