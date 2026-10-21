from __future__ import annotations

import json
from pathlib import Path

import pytest

from interfaces.cli.commands import qasper_eval


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.result: dict[str, object] = {"valid": True, "diagnostic": "fixture-only"}

    def freeze_mapping(self, output_dir: Path):
        self.calls.append(("freeze_mapping", output_dir))
        return self.result

    def validate_mapping(self, mapping_dir: Path):
        self.calls.append(("validate_mapping", mapping_dir))
        return self.result

    def run(self, output_dir: Path, mapping_dir: Path, profile: str, parameters: dict):
        self.calls.append(("run", output_dir, mapping_dir, profile, parameters))
        return self.result

    def sweep(self, output_dir: Path, mapping_dir: Path, grid: dict, profiles: list[str]):
        self.calls.append(("sweep", output_dir, mapping_dir, grid, profiles))
        return self.result

    def score(self, run_dir: Path):
        self.calls.append(("score", run_dir))
        return self.result

    def report(self, run_dir: Path):
        self.calls.append(("report", run_dir))
        return self.result


def _install(monkeypatch: pytest.MonkeyPatch, service: FakeService, calls: list[tuple]) -> None:
    def build(dataset_path, cache_path):
        calls.append((dataset_path, cache_path))
        return service

    monkeypatch.setattr(qasper_eval, "_build_service", build)


def _params() -> list[str]:
    return [
        "--profile", "bm25_only",
        "--candidate-limit", "8",
        "--overfetch", "3",
        "--rrf-k", "60",
        "--rerank", "false",
        "--threshold", "0.25",
        "--weights", '{"bm25": 1.0}',
    ]


def _replace_option(args: list[str], option: str, value: str) -> list[str]:
    updated = list(args)
    index = updated.index(option)
    updated[index + 1] = value
    return updated


def test_run_forwards_application_compatible_parameter_types_before_builder(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = FakeService()
    builder_calls: list[tuple] = []
    _install(monkeypatch, service, builder_calls)
    output, mapping = tmp_path / "out", tmp_path / "mapping"

    code = qasper_eval.main([
        "qasper", "ablation", "run", "--output-dir", str(output),
        "--mapping-dir", str(mapping), *_params(),
    ])

    assert code == 0
    assert builder_calls == [(None, None)]
    assert service.calls == [("run", output, mapping, "bm25_only", {
        "candidate_limit": 8, "overfetch": 3, "rrf_k": 60,
        "rerank": False, "threshold": 0.25, "weights": {"bm25": 1.0},
    })]
    parameters = service.calls[0][-1]
    assert type(parameters["candidate_limit"]) is int
    assert type(parameters["overfetch"]) is int
    assert type(parameters["rrf_k"]) is int
    assert type(parameters["threshold"]) is float
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_freeze_validate_score_report_forward_paths_and_builder_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = FakeService()
    builder_calls: list[tuple] = []
    _install(monkeypatch, service, builder_calls)
    dataset, cache, target = tmp_path / "dataset", tmp_path / "cache", tmp_path / "target"
    for args in [
        ["mapping", "freeze", "--output-dir", str(target)],
        ["mapping", "validate", "--mapping-dir", str(target)],
        ["score", "--run-dir", str(target)],
        ["report", "--run-dir", str(target)],
    ]:
        assert qasper_eval.main([
            "qasper", "--dataset-path", str(dataset), "--cache-path", str(cache), *args,
        ]) == 0
        json.loads(capsys.readouterr().out)
    assert service.calls == [
        ("freeze_mapping", target), ("validate_mapping", target),
        ("score", target), ("report", target),
    ]
    assert builder_calls == [(dataset, cache)] * 4


def test_sweep_forwards_strict_integer_grid_and_real_profiles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = FakeService()
    _install(monkeypatch, service, [])
    output, mapping = tmp_path / "out", tmp_path / "mapping"
    args = [
        "qasper", "sweep", "--output-dir", str(output), "--mapping-dir", str(mapping),
        "--profiles", "semantic_bm25", "full_hybrid",
        "--candidate-limit-grid", "[5,10]", "--overfetch-grid", "[1,2]",
        "--rrf-k-grid", "[20,60]", "--rerank-grid", "[true,false]",
        "--threshold-grid", "[0.1,0.5]",
        "--weights-grid", '[{"semantic":1.0,"bm25":0.5}]',
    ]

    assert qasper_eval.main(args) == 0
    json.loads(capsys.readouterr().out)
    assert service.calls == [("sweep", output, mapping, {
        "candidate_limit": [5, 10], "overfetch": [1, 2], "rrf_k": [20, 60],
        "rerank": [True, False], "threshold": [0.1, 0.5],
        "weights": [{"semantic": 1.0, "bm25": 0.5}],
    }, ["semantic_bm25", "full_hybrid"])]
    grid = service.calls[0][3]
    assert all(type(value) is int for value in grid["rrf_k"])


@pytest.mark.parametrize(("option", "value", "reason"), [
    ("--candidate-limit", "0", "candidate_limit must be a positive integer"),
    ("--candidate-limit", "-1", "candidate_limit must be a positive integer"),
    ("--candidate-limit", "1.0", "candidate_limit must be a positive integer"),
    ("--candidate-limit", "true", "candidate_limit must be a positive integer"),
    ("--overfetch", "0", "overfetch must be a positive integer"),
    ("--overfetch", "-1", "overfetch must be a positive integer"),
    ("--overfetch", "2.5", "overfetch must be a positive integer"),
    ("--rrf-k", "0", "rrf_k must be a positive integer"),
    ("--rrf-k", "-1", "rrf_k must be a positive integer"),
    ("--rrf-k", "60.0", "rrf_k must be a positive integer"),
    ("--rrf-k", "NaN", "rrf_k must be a positive integer"),
    ("--rrf-k", "Inf", "rrf_k must be a positive integer"),
    ("--threshold", "NaN", "threshold must be a finite number"),
    ("--weights", "not-json", "weights must be valid JSON"),
    ("--weights", '[]', "weights must be a JSON object"),
    ("--weights", '{"bm25":true}', "finite positive weights"),
    ("--weights", '{"bm25":0}', "finite positive weights"),
    ("--weights", '{"bm25":-1}', "finite positive weights"),
    ("--weights", '{"bm25":NaN}', "finite positive weights"),
    ("--weights", '{"answer":1}', "channels enabled by every selected profile"),
])
def test_invalid_run_contract_is_rejected_before_composition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
    option: str, value: str, reason: str,
) -> None:
    service = FakeService()
    builder_calls: list[tuple] = []
    _install(monkeypatch, service, builder_calls)
    args = ["qasper", "ablation", "run", "--output-dir", str(tmp_path / "run"),
            "--mapping-dir", str(tmp_path / "mapping"), *_params()]

    assert qasper_eval.main(_replace_option(args, option, value)) == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert reason in payload["reason"]
    assert builder_calls == []
    assert service.calls == []


def test_unknown_profile_is_rejected_before_composition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = FakeService()
    builder_calls: list[tuple] = []
    _install(monkeypatch, service, builder_calls)
    args = ["qasper", "ablation", "run", "--output-dir", str(tmp_path / "run"),
            "--mapping-dir", str(tmp_path / "mapping"), *_params()]

    assert qasper_eval.main(_replace_option(args, "--profile", "baseline")) == 2
    assert "supported QASPER retrieval profiles" in json.loads(capsys.readouterr().out)["reason"]
    assert builder_calls == []
    assert service.calls == []


@pytest.mark.parametrize(("option", "value", "reason"), [
    ("--candidate-limit-grid", "[true]", "candidate_limit grid value must be a positive integer"),
    ("--overfetch-grid", "[0]", "overfetch grid value must be a positive integer"),
    ("--overfetch-grid", "[1.5]", "overfetch grid value must be a positive integer"),
    ("--rrf-k-grid", "[60.0]", "rrf_k grid value must be a positive integer"),
    ("--rrf-k-grid", "[NaN]", "rrf_k grid value must be a positive integer"),
    ("--rerank-grid", "[1]", "rerank grid values must be booleans"),
    ("--threshold-grid", "[Infinity]", "threshold grid value must be a finite number"),
    ("--weights-grid", '[{"semantic":1}]', "channels enabled by every selected profile"),
    ("--weights-grid", '[{"bm25":false}]', "finite positive weights"),
])
def test_invalid_sweep_contract_is_rejected_before_composition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
    option: str, value: str, reason: str,
) -> None:
    service = FakeService()
    builder_calls: list[tuple] = []
    _install(monkeypatch, service, builder_calls)
    args = [
        "qasper", "sweep", "--output-dir", str(tmp_path / "out"),
        "--mapping-dir", str(tmp_path / "mapping"), "--profiles", "bm25_only",
        "--candidate-limit-grid", "[5]", "--overfetch-grid", "[1]",
        "--rrf-k-grid", "[60]", "--rerank-grid", "[false]",
        "--threshold-grid", "[0.25]", "--weights-grid", '[{"bm25":1.0}]',
    ]

    assert qasper_eval.main(_replace_option(args, option, value)) == 2
    assert reason in json.loads(capsys.readouterr().out)["reason"]
    assert builder_calls == []
    assert service.calls == []


def test_duplicate_or_unknown_sweep_profiles_are_rejected_before_composition(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = FakeService()
    builder_calls: list[tuple] = []
    _install(monkeypatch, service, builder_calls)
    common = [
        "--candidate-limit-grid", "[5]", "--overfetch-grid", "[1]",
        "--rrf-k-grid", "[60]", "--rerank-grid", "[false]",
        "--threshold-grid", "[0.25]", "--weights-grid", "[{}]",
    ]
    for profiles in (("bm25_only", "bm25_only"), ("bm25_only", "reranked")):
        args = ["qasper", "sweep", "--output-dir", str(tmp_path / "out"),
                "--mapping-dir", str(tmp_path / "mapping"), "--profiles", *profiles, *common]
        assert qasper_eval.main(args) == 2
        assert "unique supported" in json.loads(capsys.readouterr().out)["reason"]
    assert builder_calls == []
    assert service.calls == []


def test_internal_positive_integer_contract_rejects_bool() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        qasper_eval._positive_int(True, "overfetch")


def test_value_error_is_sanitized_and_needs_review_remains_diagnostic(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    service = FakeService()
    service.result = {"status": "needs_review", "diagnostics": ["mapping incomplete"]}
    _install(monkeypatch, service, [])
    assert qasper_eval.main(["qasper", "score", "--run-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "operation": "score", "status": "needs_review", "diagnostics": ["mapping incomplete"]
    }

    def broken(_dataset, _cache):
        raise ValueError("secret/path/reference text")

    monkeypatch.setattr(qasper_eval, "_build_service", broken)
    assert qasper_eval.main(["qasper", "score", "--run-dir", str(tmp_path)]) == 2
    output = capsys.readouterr().out
    assert "secret/path" not in output
    assert json.loads(output)["reason"].startswith("ValueError:")


def test_expected_runtime_operation_error_returns_sanitized_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    error = RuntimeError("cache failed at C:/private/secret-cache.npz")
    service = FakeService()
    _install(monkeypatch, service, [])

    def fail_operation(_run_dir: Path):
        raise error

    monkeypatch.setattr(service, "score", fail_operation)
    assert qasper_eval.main(["qasper", "score", "--run-dir", str(tmp_path)]) == 2

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "operation": "score",
        "reason": f"{type(error).__name__}: QASPER operation could not be completed",
        "valid": False,
    }
    assert "secret" not in output
    assert "private" not in output


def test_runtime_error_during_composition_returns_sanitized_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fail_build(_dataset, _cache):
        raise RuntimeError("git probe failed: private-secret-path")

    monkeypatch.setattr(qasper_eval, "_build_service", fail_build)
    assert qasper_eval.main(["qasper", "score", "--run-dir", str(tmp_path)]) == 2

    output = capsys.readouterr().out
    assert json.loads(output) == {
        "operation": "score",
        "reason": "RuntimeError: QASPER operation could not be completed",
        "valid": False,
    }
    assert "private-secret-path" not in output


def test_rag_eval_registration_keeps_legacy_command_and_adds_qasper_namespace() -> None:
    from interfaces.cli.commands import rag_eval

    parser = rag_eval.build_parser()
    legacy = parser.parse_args([
        "rag-eval", "validate-dataset", "--dataset-root", "dataset",
        "--dataset-id", "existing", "--dataset-version", "v1",
    ])
    assert legacy.dataset_id == "existing"
    assert legacy.handler is rag_eval.validate_dataset

    parsed = parser.parse_args([
        "rag-eval", "qasper", "mapping", "freeze", "--output-dir", "out",
    ])
    assert parsed._qasper_action == "freeze_mapping"
    assert parsed.handler is qasper_eval._dispatch
