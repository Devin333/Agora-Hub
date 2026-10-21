from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.research.rag.evaluation.document_parsing_protocol import (
    write_parsing_evaluation_manifest,
)
from interfaces.cli.commands import parsing_eval
from tests.backend.research.rag.test_document_parsing_protocol import _manifest


def test_parsing_eval_cli_outputs_summary_from_manifest_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    write_parsing_evaluation_manifest(_manifest(), manifest_path)

    assert parsing_eval.main([str(manifest_path)]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["operation"] == "summarize-parsing"
    assert payload["run_id"] == "run-001"
    assert payload["metric_denominators"]["CDM:page"]["scored_denominator"] == 1
    assert payload["metric_denominators"]["CDM:page"]["planned_pages"] == 1
    assert payload["metric_denominators"]["CDM:page"]["reported_pages"] == 1
    assert payload["page_summary"]["reported_pages"] == 1
    assert payload["quality_gate"]["evaluated"] is False
    assert payload["quality_gate"]["passed"] is None
    assert payload["metric_value_summary"]["CDM:page"]["successful_value_mean"] == 0.5
    assert output == json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"


def test_parsing_eval_cli_returns_sanitized_error_for_invalid_manifest(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "malformed.json"
    manifest_path.write_text("{", encoding="utf-8")

    assert parsing_eval.main([str(manifest_path)]) == 2
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert payload["valid"] is False
    assert payload["issues"] == [
        {
            "code": "parsing_manifest.invalid",
            "reason": "manifest file is unavailable or invalid",
        }
    ]
    assert payload["quality_gate"]["evaluated"] is False
    assert payload["quality_gate"]["passed"] is None
    assert str(manifest_path) not in output
