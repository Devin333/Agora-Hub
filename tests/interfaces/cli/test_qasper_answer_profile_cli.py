from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from interfaces.cli.commands import qasper_eval
from interfaces.composition import qasper_evaluation as composition


def test_answer_builder_forwards_explicit_retrieval_profile(
    monkeypatch, tmp_path: Path
) -> None:
    captured = {}
    sentinel = object()

    def build(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(composition, "build_qasper_answer_evaluation_service", build)
    args = argparse.Namespace(
        dataset_path=tmp_path / "dataset.json",
        cache_path=tmp_path / "cache.npz",
        query_vector_path=tmp_path / "queries.json",
        output_dir=tmp_path / "out",
        seed="seed",
        per_type=3,
        max_cases=7,
        no_judge=True,
        retrieval_profile="field_only",
        retrieval_overfetch=9,
    )

    assert qasper_eval._build_answer_service(args) is sentinel
    assert captured["retrieval_profile"] == "field_only"
    assert captured["retrieval_overfetch"] == 9
    assert captured["judge"] is False


def test_answer_cli_accepts_field_only_profile(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured = {}

    class Service:
        def run(self):
            return {"evaluated_cases": 0}

    def build(args):
        captured["profile"] = args.retrieval_profile
        return Service()

    monkeypatch.setattr(qasper_eval, "_build_answer_service", build)

    code = qasper_eval.main([
        "qasper",
        "--dataset-path", str(tmp_path / "dataset.json"),
        "--cache-path", str(tmp_path / "cache.npz"),
        "answer", "run",
        "--output-dir", str(tmp_path / "out"),
        "--query-vector-path", str(tmp_path / "queries.json"),
        "--retrieval-profile", "field_only",
        "--no-judge",
    ])

    assert code == 0
    assert captured["profile"] == "field_only"
    assert '"valid": true' in capsys.readouterr().out
