from __future__ import annotations

import json
from pathlib import Path

import pytest

from interfaces.cli.commands import qasper_eval


class _Service:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls = 0

    def run(self):
        self.calls += 1
        return self.result


def test_answer_replay_forwards_explicit_offline_paths(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    service = _Service({"status": "completed", "case_count": 300})
    captured = {}

    def build(args):
        captured.update(vars(args))
        return service

    monkeypatch.setattr(qasper_eval, "_build_replay_service", build)
    dataset = tmp_path / "dataset.json"
    cache = tmp_path / "cache.npz"
    query_vectors = tmp_path / "query.npz"
    source = tmp_path / "source"
    output = tmp_path / "output"

    code = qasper_eval.main([
        "qasper", "--dataset-path", str(dataset), "--cache-path", str(cache),
        "answer", "replay", "--source-run-dir", str(source),
        "--output-dir", str(output), "--query-vector-path", str(query_vectors),
    ])

    assert code == 0 and service.calls == 1
    assert captured["dataset_path"] == dataset
    assert captured["cache_path"] == cache
    assert captured["source_run_dir"] == source
    assert captured["output_dir"] == output
    assert captured["query_vector_path"] == query_vectors
    assert json.loads(capsys.readouterr().out)["operation"] == "answer_replay"


def test_answer_replay_returns_nonzero_for_blocked_preflight(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    service = _Service({"status": "blocked", "blocked": {"code": "query_vectors_missing"}})
    monkeypatch.setattr(qasper_eval, "_build_replay_service", lambda _args: service)

    code = qasper_eval.main([
        "qasper", "--dataset-path", str(tmp_path / "dataset"),
        "--cache-path", str(tmp_path / "cache"), "answer", "replay",
        "--source-run-dir", str(tmp_path / "source"),
        "--output-dir", str(tmp_path / "output"),
        "--query-vector-path", str(tmp_path / "query"),
    ])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert payload["blocked"]["code"] == "query_vectors_missing"


def test_answer_run_forwards_optional_retrieval_overfetch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    service = _Service({"evaluated_cases": 1})
    captured = {}

    def build(args):
        captured.update(vars(args))
        return service

    monkeypatch.setattr(qasper_eval, "_build_answer_service", build)

    code = qasper_eval.main([
        "qasper", "--dataset-path", str(tmp_path / "dataset"),
        "--cache-path", str(tmp_path / "cache"), "answer", "run",
        "--output-dir", str(tmp_path / "output"),
        "--query-vector-path", str(tmp_path / "query"),
        "--retrieval-overfetch", "10",
    ])

    assert code == 0
    assert captured["retrieval_overfetch"] == 10
    assert json.loads(capsys.readouterr().out)["operation"] == "answer_run"
