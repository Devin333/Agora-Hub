from __future__ import annotations

import asyncio
import json

from interfaces.cli.commands.answer_eval import main

ENDPOINT = json.dumps({
    "provider": "dashscope",
    "base_url": "https://dashscope.example/v1",
    "model": "deepseek-v4-flash",
    "api_key_env": "DASHSCOPE_API_KEY",
})


def _run(argv, capsys) -> tuple[int, dict]:
    code = main(argv)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _common(tmp_path) -> list[str]:
    return [
        "answer", "qasper-run",
        "--dataset-path", str(tmp_path / "qasper.json"),
        "--output-dir", str(tmp_path / "out"),
        "--replay-dir", str(tmp_path / "replay"),
        "--dataset-version", "0.3",
        "--questions-frozen-at", "2020-01-01T00:00:00+00:00",
        "--answers-started-at", "2020-02-01T00:00:00+00:00",
        "--retrieved-at", "2026-10-22T00:00:00+00:00",
        "--answer-model", ENDPOINT,
        "--extractor-model", ENDPOINT,
    ]


def test_a_single_judge_is_rejected_before_any_model_is_built(tmp_path, capsys) -> None:
    code, payload = _run([*_common(tmp_path), "--judge-model", ENDPOINT], capsys)
    assert code == 2 and payload["valid"] is False
    assert "at least two judge models" in payload["reason"]


def test_keys_are_never_accepted_on_the_command_line(tmp_path, capsys) -> None:
    leaked = json.dumps({**json.loads(ENDPOINT), "api_key": "sk-secret"})
    argv = _common(tmp_path)
    argv[argv.index("--extractor-model") + 1] = leaked
    code, payload = _run(argv, capsys)
    assert code == 2
    assert "sk-secret" not in json.dumps(payload)


def test_collection_timestamps_need_a_timezone(tmp_path, capsys) -> None:
    argv = _common(tmp_path)
    argv[argv.index("--retrieved-at") + 1] = "2026-10-22T00:00:00"
    code, payload = _run(argv, capsys)
    assert code == 2 and "timezone" in payload["reason"]


def test_report_command_verifies_a_finished_run(tmp_path, capsys) -> None:
    from tests.backend.research.application.test_answer_fact_evaluation import _service

    service, _ = _service()
    report = asyncio.run(service.run("ignored"))
    (tmp_path / "report.json").write_text(report.model_dump_json(), encoding="utf-8")
    code, payload = _run(["answer", "report", "--run-dir", str(tmp_path)], capsys)
    assert code == 0 and payload["digest"] == report.digest
    assert payload["metrics"]["answer.fact_precision"]["status_counts"]

    tampered = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    tampered["cases"] = 99
    (tmp_path / "report.json").write_text(json.dumps(tampered), encoding="utf-8")
    code, payload = _run(["answer", "report", "--run-dir", str(tmp_path)], capsys)
    assert code == 2 and payload["valid"] is False
