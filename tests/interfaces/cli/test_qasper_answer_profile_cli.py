from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

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
        rerun_failed_from=None,
    )

    assert qasper_eval._build_answer_service(args) is sentinel
    assert captured["retrieval_profile"] == "field_only"
    assert captured["retrieval_overfetch"] == 9
    assert captured["judge"] is False


def test_answer_cli_parses_and_forwards_evidence_selection_options(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    monkeypatch.setattr(
        composition, "build_qasper_answer_evaluation_service",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    parser = argparse.ArgumentParser()
    qasper_eval.register(parser.add_subparsers())
    args = parser.parse_args([
        "qasper", "--dataset-path", str(tmp_path / "d.json"), "--cache-path", str(tmp_path / "c.npz"),
        "answer", "run", "--output-dir", str(tmp_path / "out"),
        "--query-vector-path", str(tmp_path / "q.npz"),
        "--max-context-items", "16", "--evidence-pool-size", "48",
        "--evidence-min-passages", "3", "--evidence-min-relevance", "5",
        "--evidence-render-mode", "passage",
    ])

    qasper_eval._build_answer_service(args)

    assert captured["evidence_pool_size"] == 48
    assert captured["evidence_min_passages"] == 3
    assert captured["evidence_min_relevance"] == 5
    assert captured["evidence_render_mode"] == "passage"


def test_answer_cli_parses_and_forwards_section_navigation_options(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    monkeypatch.setattr(
        composition, "build_qasper_answer_evaluation_service",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    parser = argparse.ArgumentParser()
    qasper_eval.register(parser.add_subparsers())
    base = [
        "qasper", "--dataset-path", str(tmp_path / "d.json"), "--cache-path", str(tmp_path / "c.npz"),
        "answer", "run", "--output-dir", str(tmp_path / "out"),
        "--query-vector-path", str(tmp_path / "q.npz"),
    ]

    qasper_eval._build_answer_service(parser.parse_args([
        *base, "--section-navigation", "--navigation-max-sections", "3",
        "--navigation-max-passages-per-section", "1",
    ]))
    assert captured["section_navigation"] is True
    assert captured["navigation_max_sections"] == 3
    assert captured["navigation_max_passages_per_section"] == 1

    qasper_eval._build_answer_service(parser.parse_args(base))
    assert captured["section_navigation"] is False
    assert captured["navigation_max_sections"] is None


def test_section_navigation_composition_builds_navigator_with_its_own_output_room() -> None:
    from backend.research.rag.retrieval.paper_section_navigator import (
        DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS,
        LLMSectionNavigator,
    )
    from tests.fixtures.paper_answer_model import FakePaperAnswerModel

    requested = {}

    def model_factory(**kwargs):
        requested.update(kwargs)
        return FakePaperAnswerModel("{}")

    navigator, config = composition._qasper_section_navigation(
        model_factory, enabled=True, max_sections=3, max_passages_per_section=None,
    )

    assert isinstance(navigator, LLMSectionNavigator)
    assert requested == {"max_tokens": DEFAULT_NAVIGATION_MAX_OUTPUT_TOKENS}
    assert config.max_sections == 3
    assert config.max_passages_per_section == 2
    assert composition._qasper_section_navigation(
        model_factory, enabled=False, max_sections=None, max_passages_per_section=None,
    ) == (None, None)
    with pytest.raises(ValueError, match="require section_navigation"):
        composition._qasper_section_navigation(
            model_factory, enabled=False, max_sections=2, max_passages_per_section=None,
        )


def test_answer_cli_emits_start_and_end_progress_only_on_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class Service:
        def __init__(self, started, progress) -> None:
            self.started = started
            self.progress = progress

        def run(self):
            self.started(1, 1, "paper:q1")
            self.progress(1, 1, SimpleNamespace(
                case_id="paper:q1",
                outcome=SimpleNamespace(value="answered"),
                end_to_end_ms=12.5,
                generation_error="",
                judge=None,
            ))
            return {"evaluated_cases": 1}

    def build(**kwargs):
        return Service(kwargs["case_started"], kwargs["progress"])

    monkeypatch.setattr(composition, "build_qasper_answer_evaluation_service", build)

    code = qasper_eval.main([
        "qasper", "--dataset-path", str(tmp_path / "dataset.json"),
        "--cache-path", str(tmp_path / "cache.npz"),
        "answer", "run", "--output-dir", str(tmp_path / "out"),
        "--query-vector-path", str(tmp_path / "queries.json"),
    ])

    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["operation"] == "answer_run"
    assert captured.out.count("\n") == 1
    assert captured.err.splitlines() == [
        "[1/1] START paper:q1",
        "[1/1] END paper:q1 answered 12ms",
    ]
    assert "prompt" not in captured.err.casefold()


def test_rejudge_cli_forwards_legacy_model_without_answer_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    captured = {}

    class Service:
        def run(self):
            return {"rejudge": {"attempts": 0}}

    def build(**kwargs):
        captured.update(kwargs)
        return Service()

    monkeypatch.setattr(composition, "build_qasper_answer_rejudge_service", build)

    code = qasper_eval.main([
        "qasper", "--dataset-path", str(tmp_path / "dataset.json"),
        "--cache-path", str(tmp_path / "cache.npz"),
        "answer", "rejudge", "--source-run-dir", str(tmp_path / "source"),
        "--output-dir", str(tmp_path / "output"),
        "--legacy-judge-model", "historical-judge",
    ])

    assert code == 0
    assert captured["legacy_judge_model"] == "historical-judge"
    assert captured["dataset_path"] == (tmp_path / "dataset.json").resolve()
    assert json.loads(capsys.readouterr().out)["operation"] == "answer_rejudge"


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
