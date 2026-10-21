from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from backend.research.rag.evaluation import omnidocbench_adapter as adapter


_COMMIT = "a" * 40


@pytest.fixture
def fixture_paths(tmp_path: Path) -> dict[str, Path | str]:
    root = tmp_path / "OmniDocBench"
    (root / ".git").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "pdf_validation.py").write_text("# fixture only; never executed\n", encoding="utf-8")
    config = root / "configs" / "end2end.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "end2end_eval": {
                    "metrics": {
                        "text_block": {"metric": ["Edit_dist"]},
                        "display_formula": {"metric": ["Edit_dist", "CDM"], "cdm_workers": 9},
                        "table": {"metric": ["TEDS"], "teds_workers": 6},
                    },
                    "dataset": {
                        "dataset_name": "end2end_dataset",
                        "ground_truth": {"data_path": "old-annotation.json"},
                        "prediction": {"data_path": "old-predictions"},
                        "match_workers": 12,
                    },
                }
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    prediction = tmp_path / "predictions"
    prediction.mkdir()
    (prediction / "page.md").write_text("prediction", encoding="utf-8")
    annotation = tmp_path / "annotations.json"
    annotation.write_text('[{"page_info":{"image_path":"page.png"}}]', encoding="utf-8")
    output = tmp_path / "run-output"
    return {
        "root": root,
        "config": config,
        "prediction": prediction,
        "annotation": annotation,
        "output": output,
        "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
    }


class FakeProcess:
    def __init__(self, returncode: int = 0, timeout: bool = False):
        self.pid = 12345
        self.returncode = returncode
        self._timeout = timeout
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self._timeout:
            raise subprocess.TimeoutExpired(cmd="fake-scorer", timeout=timeout)
        return self.returncode

    def poll(self):
        return None if self._timeout else self.returncode

    def kill(self):
        self.returncode = -9
        self._timeout = False

    def confirm_terminated(self):
        self.returncode = 1
        self._timeout = False


def _mock_git(*, dirty: str = ""):
    def run(args, **kwargs):
        assert kwargs["shell"] is False
        if args[0] == "git":
            if "--show-toplevel" in args:
                return SimpleNamespace(returncode=0, stdout=args[2] + "\n", stderr="")
            if "rev-parse" in args:
                return SimpleNamespace(returncode=0, stdout=_COMMIT + "\n", stderr="")
            return SimpleNamespace(returncode=0, stdout=dirty, stderr="")
        raise AssertionError(f"unexpected subprocess command: {args}")

    return run


def _mock_popen(
    *,
    scorer_returncode: int = 0,
    timeout: bool = False,
    calls: list | None = None,
    output_dir: Path | None = None,
):
    calls = calls if calls is not None else []
    process = FakeProcess(returncode=scorer_returncode, timeout=timeout)

    def popen(args, **kwargs):
        kwargs["stdout"].write(b"official stdout\n")
        kwargs["stderr"].write(b"official stderr\n")
        kwargs["runtime_config"] = Path(args[-1]).read_text(encoding="utf-8")
        calls.append((args, kwargs))
        if output_dir is not None and scorer_returncode == 0 and not timeout:
            result_dir = output_dir / "result"
            result_dir.mkdir()
            (result_dir / "official_metric_result.json").write_text('{"score":0.5}', encoding="utf-8")
        return process

    return process, popen


def _run(paths: dict[str, Path | str], **overrides):
    options = {
        "prediction_path": paths["prediction"],
        "annotation_path": paths["annotation"],
        "output_path": paths["output"],
        "scorer_root": paths["root"],
        "expected_commit": _COMMIT,
        "expected_config_sha256": paths["config_sha256"],
        "timeout_seconds": 30,
        "max_workers": 3,
    }
    options.update(overrides)
    return adapter.run_omnidocbench_official_scorer(**options)


def _mock_execution(paths, *, scorer_returncode=0, timeout=False, dirty="", terminate="terminated"):
    calls: list = []
    _, fake_popen = _mock_popen(
        scorer_returncode=scorer_returncode,
        timeout=timeout,
        calls=calls,
        output_dir=Path(paths["output"]),
    )
    return calls, fake_popen, _mock_git(dirty=dirty), terminate


def test_runs_frozen_cli_and_persists_output_and_input_identities(fixture_paths):
    calls, fake_popen, fake_git, _ = _mock_execution(fixture_paths)
    with (
        patch.object(adapter.subprocess, "run", side_effect=fake_git),
        patch.object(adapter.subprocess, "Popen", side_effect=fake_popen),
    ):
        result = _run(fixture_paths)

    assert result.status == "succeeded"
    assert result.exit_code == 0
    assert result.scorer_commit == _COMMIT
    assert result.runtime_seconds >= 0
    assert result.termination_status == "not_needed"
    assert Path(result.stdout_path).read_text(encoding="utf-8") == "official stdout\n"
    assert Path(result.stderr_path).read_text(encoding="utf-8") == "official stderr\n"
    assert Path(result.scorer_result_dir, "official_metric_result.json").is_file()
    expected_annotation_hash = hashlib.sha256(Path(fixture_paths["annotation"]).read_bytes()).hexdigest()
    expected_prediction_hash = hashlib.sha256(b"prediction").hexdigest()
    assert result.annotation_sha256 == expected_annotation_hash
    assert result.prediction_file_count == 1
    assert result.prediction_total_bytes == len(b"prediction")
    assert result.prediction_manifest[0].content_sha256 == expected_prediction_hash
    assert result.prediction_manifest[0].relative_path_sha256 == hashlib.sha256(b"page.md").hexdigest()
    assert result.prediction_manifest_sha256 == adapter._prediction_manifest_sha256(result.prediction_manifest)
    summary = json.loads(Path(result.summary_path).read_text(encoding="utf-8"))
    assert summary["status"] == "succeeded"
    assert summary["annotation_sha256"] == expected_annotation_hash
    assert summary["prediction_manifest_sha256"] == result.prediction_manifest_sha256
    assert summary["prediction_file_count"] == 1
    assert summary["prediction_total_bytes"] == len(b"prediction")

    scorer_args, scorer_kwargs = calls[-1]
    assert scorer_args == [
        adapter.sys.executable,
        str(Path(fixture_paths["root"]) / "pdf_validation.py"),
        "--config",
        str(Path(fixture_paths["output"]) / "omnidocbench-runtime.yaml"),
    ]
    assert scorer_kwargs["shell"] is False
    assert scorer_kwargs["cwd"] == Path(fixture_paths["output"])
    if os.name == "nt":
        assert scorer_kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert scorer_kwargs["start_new_session"] is False
    else:
        assert scorer_kwargs["creationflags"] == 0
        assert scorer_kwargs["start_new_session"] is True
    runtime_config = yaml.safe_load(scorer_kwargs["runtime_config"])
    dataset = runtime_config["end2end_eval"]["dataset"]
    assert dataset["ground_truth"]["data_path"] == str(Path(fixture_paths["output"]) / "input" / "annotations.json")
    assert dataset["prediction"]["data_path"] == str(Path(fixture_paths["output"]) / "input" / "predictions")
    assert result.runtime_config_sha256 == hashlib.sha256(scorer_kwargs["runtime_config"].encode()).hexdigest()
    assert result.runtime_config_sha256 != result.config_sha256
    assert result.staging_seconds >= 0
    staged_prediction = Path(result.staged_input_dir) / "predictions" / "page.md"
    assert staged_prediction.read_text(encoding="utf-8") == "prediction"
    assert "page.md" not in Path(result.summary_path).read_text(encoding="utf-8")


def test_timeout_records_confirmed_termination_and_partial_logs(fixture_paths):
    calls, fake_popen, fake_git, _ = _mock_execution(fixture_paths, timeout=True)
    with (
        patch.object(adapter.subprocess, "run", side_effect=fake_git),
        patch.object(adapter.subprocess, "Popen", side_effect=fake_popen),
        patch.object(adapter, "_terminate_process_tree", return_value="terminated"),
    ):
        result = _run(fixture_paths)
    assert result.status == "failed"
    assert result.exit_code is None
    assert result.termination_status == "terminated"
    assert "timeout of 30 seconds" in result.error
    assert Path(result.stdout_path).read_text(encoding="utf-8") == "official stdout\n"
    assert Path(result.stderr_path).read_text(encoding="utf-8") == "official stderr\n"
    assert json.loads(Path(result.summary_path).read_text(encoding="utf-8"))["status"] == "failed"
    assert calls


def test_timeout_with_unconfirmed_tree_is_indeterminate(fixture_paths):
    _, fake_popen, fake_git, _ = _mock_execution(fixture_paths, timeout=True)
    with (
        patch.object(adapter.subprocess, "run", side_effect=fake_git),
        patch.object(adapter.subprocess, "Popen", side_effect=fake_popen),
        patch.object(adapter, "_terminate_process_tree", return_value="indeterminate"),
    ):
        result = _run(fixture_paths)
    assert result.status == "indeterminate"
    assert result.termination_status == "indeterminate"
    assert "could not be confirmed" in result.error
    assert json.loads(Path(result.summary_path).read_text(encoding="utf-8"))["status"] == "indeterminate"


def test_windows_tree_termination_uses_safe_bounded_taskkill_argv(tmp_path, monkeypatch):
    system_root = tmp_path / "Windows"
    taskkill = system_root / "System32" / "taskkill.exe"
    taskkill.parent.mkdir(parents=True)
    taskkill.write_bytes(b"fixture")
    monkeypatch.setenv("SystemRoot", str(system_root))
    process = FakeProcess(timeout=True)
    commands = []

    def run(args, **kwargs):
        commands.append((args, kwargs))
        process.confirm_terminated()
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    with patch.object(adapter.subprocess, "run", side_effect=run):
        assert adapter._terminate_windows_process_tree(process) == "terminated"
    args, kwargs = commands[0]
    assert args == [str(taskkill), "/PID", "12345", "/T", "/F"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 15


def test_nonzero_official_exit_is_failed_without_synthetic_result(fixture_paths):
    _, fake_popen, fake_git, _ = _mock_execution(fixture_paths, scorer_returncode=7)
    with (
        patch.object(adapter.subprocess, "run", side_effect=fake_git),
        patch.object(adapter.subprocess, "Popen", side_effect=fake_popen),
    ):
        result = _run(fixture_paths)
    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.error == "Official scorer exited with code 7"
    assert result.scorer_result_dir is None



def test_popen_failure_still_records_failed_summary_and_empty_logs(fixture_paths):
    with (
        patch.object(adapter.subprocess, "run", side_effect=_mock_git()),
        patch.object(adapter.subprocess, "Popen", side_effect=OSError("simulated launch failure")),
    ):
        result = _run(fixture_paths)
    assert result.status == "failed"
    assert "simulated launch failure" in result.error
    assert Path(result.stdout_path).is_file()
    assert Path(result.stderr_path).is_file()
    assert Path(result.stdout_path).read_bytes() == b""
    assert Path(result.stderr_path).read_bytes() == b""
    assert json.loads(Path(result.summary_path).read_text(encoding="utf-8"))["status"] == "failed"

def test_missing_scorer_returns_not_available_without_fallback_or_output(fixture_paths, tmp_path):
    result = _run(fixture_paths, scorer_root=tmp_path / "missing-scorer")
    assert result.status == "not_available"
    assert "unavailable" in result.error
    assert not Path(fixture_paths["output"]).exists()


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"expected_commit": "main"}, "full, lowercase 40-character"),
        ({"expected_config_sha256": "floating"}, "lowercase 64-character"),
        ({"timeout_seconds": 86_401}, "1 through 86400"),
        ({"max_workers": 65}, "1 through 64"),
        ({"max_prediction_files": 100_001}, "1 through 100000"),
        ({"max_prediction_bytes": 2 * 1024**3 + 1}, "1 through 2147483648"),
        ({"max_annotation_bytes": 256 * 1024**2 + 1}, "1 through 268435456"),
    ],
)
def test_rejects_floating_versions_and_over_budget_values(fixture_paths, override, message):
    with pytest.raises(ValueError, match=message):
        _run(fixture_paths, **override)


def test_rejects_commit_mismatch_and_dirty_tree_before_creating_output(fixture_paths):
    with patch.object(adapter.subprocess, "run", side_effect=_mock_git()):
        with pytest.raises(ValueError, match="commit mismatch"):
            _run(fixture_paths, expected_commit="b" * 40)
    assert not Path(fixture_paths["output"]).exists()

    with patch.object(adapter.subprocess, "run", side_effect=_mock_git(dirty=" M src/core/pipeline.py\n")):
        with pytest.raises(ValueError, match="dirty"):
            _run(fixture_paths)
    assert not Path(fixture_paths["output"]).exists()


def test_rejects_config_fingerprint_mismatch_and_existing_output(fixture_paths):
    with patch.object(adapter.subprocess, "run", side_effect=_mock_git()):
        with pytest.raises(ValueError, match="config fingerprint mismatch"):
            _run(fixture_paths, expected_config_sha256="0" * 64)
    assert not Path(fixture_paths["output"]).exists()

    Path(fixture_paths["output"]).mkdir()
    with pytest.raises(FileExistsError, match="new, non-existing"):
        _run(fixture_paths)


def test_rejects_missing_invalid_and_symlink_escaping_predictions(fixture_paths, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="does not exist"):
        _run(fixture_paths, prediction_path=Path(fixture_paths["root"]) / "missing")
    empty = tmp_path / "empty-predictions"
    empty.mkdir()
    with pytest.raises(ValueError, match="at least one .md or .mmd"):
        _run(fixture_paths, prediction_path=empty)

    class SymlinkEntry:
        name = "escape.md"
        path = str(Path(fixture_paths["prediction"]) / name)

        @staticmethod
        def is_symlink():
            return True

    class ScandirFixture:
        def __enter__(self):
            return iter([SymlinkEntry()])

        @staticmethod
        def __exit__(*_args):
            return False

    monkeypatch.setattr(adapter.os, "scandir", lambda _path: ScandirFixture())
    with pytest.raises(ValueError, match="symlinks"):
        _run(fixture_paths)


def test_enforces_prediction_count_total_bytes_and_annotation_size_before_read(fixture_paths, tmp_path):
    predictions = Path(fixture_paths["prediction"])
    (predictions / "second.mmd").write_text("second", encoding="utf-8")
    with pytest.raises(ValueError, match="file count exceeds"):
        _run(fixture_paths, max_prediction_files=1)
    with pytest.raises(ValueError, match="total size exceeds"):
        _run(fixture_paths, max_prediction_bytes=1)
    with pytest.raises(ValueError, match="annotation exceeds"):
        _run(fixture_paths, max_annotation_bytes=1)


def test_runtime_config_write_failure_leaves_failed_summary(fixture_paths, monkeypatch):
    original_write_bytes = Path.write_bytes

    def fail_runtime_config(path: Path, data: bytes):
        if path.name == "omnidocbench-runtime.yaml":
            raise OSError("simulated config write failure")
        return original_write_bytes(path, data)

    monkeypatch.setattr(Path, "write_bytes", fail_runtime_config)
    with patch.object(adapter.subprocess, "run", side_effect=_mock_git()):
        result = _run(fixture_paths)
    assert result.status == "failed"
    assert "simulated config write failure" in result.error
    assert json.loads(Path(result.summary_path).read_text(encoding="utf-8"))["status"] == "failed"
    assert not (Path(fixture_paths["output"]) / "omnidocbench-runtime.yaml").exists()



def test_input_staging_failure_leaves_terminal_summary_and_empty_logs(fixture_paths):
    with (
        patch.object(adapter, "_stage_inputs", side_effect=OSError("simulated staging failure")),
        patch.object(adapter.subprocess, "run", side_effect=_mock_git()),
    ):
        result = _run(fixture_paths)

    assert result.status == "failed"
    assert "simulated staging failure" in result.error
    assert Path(result.stdout_path).read_bytes() == b""
    assert Path(result.stderr_path).read_bytes() == b""
    assert json.loads(Path(result.summary_path).read_text(encoding="utf-8"))["status"] == "failed"
