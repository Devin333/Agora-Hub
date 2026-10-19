from __future__ import annotations

import json

from scripts import dev


def write_jsonl(path, rows) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def test_validate_command_is_offline_and_machine_readable(tmp_path, capsys) -> None:
    tasks = tmp_path / "tasks.jsonl"
    results = tmp_path / "results.jsonl"
    write_jsonl(tasks, [{"task_id": "case-1", "question": "What does the evidence show?"}])
    write_jsonl(results, [{
        "task_id": "case-1",
        "question": "What does the evidence show?",
        "report": "Findings",
        "sources": [{"source_id": "s1", "url": "https://example.com/a", "title": "A", "kind": "article"}],
        "citations": [],
        "execution": {},
        "status": "succeeded",
    }])
    assert dev.main(["reportbench", "validate", "--tasks", str(tasks), "--results", str(results)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["network_calls"] == 0
    assert payload["paid_calls"] == 0


def test_run_plan_does_not_create_result_or_invoke_model(tmp_path, capsys) -> None:
    tasks = tmp_path / "tasks.jsonl"
    plan = tmp_path / "plan.json"
    write_jsonl(tasks, [
        {"task_id": "case-1", "question": "Q1"},
        {"task_id": "case-2", "question": "Q2"},
    ])
    assert dev.main(["reportbench", "run-plan", "--tasks", str(tasks), "--output", str(plan), "--limit", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["execution_started"] is False
    assert payload["requires_approval"] is True
    assert payload["selected_task_ids"] == ["case-1"]
