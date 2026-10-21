from __future__ import annotations

import json
from pathlib import Path

from backend.research.rag.evaluation.document_parsing_acceptance import (
    write_acceptance_profile,
)
from interfaces.cli.commands import parsing_acceptance
from tests.backend.research.rag.test_document_parsing_acceptance import _manifest, _profile
from backend.research.rag.evaluation.document_parsing_protocol import (
    write_parsing_evaluation_manifest,
)


def test_acceptance_cli_fails_closed_when_evidence_is_missing(capsys):
    assert parsing_acceptance.main([]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["quality_gate_decision"] == "not_evaluated"
    assert "acceptance_profile_missing" in payload["reasons"]


def test_acceptance_cli_reads_frozen_profile_and_paired_manifests(tmp_path: Path, capsys):
    baseline_path = tmp_path / "baseline.json"
    candidate_path = tmp_path / "candidate.json"
    profile_path = tmp_path / "profile.json"
    write_parsing_evaluation_manifest(
        _manifest("baseline", candidate=False, edit_values=(0.3, 0.25)), baseline_path
    )
    write_parsing_evaluation_manifest(
        _manifest("candidate", candidate=True, edit_values=(0.2, 0.15)), candidate_path
    )
    write_acceptance_profile(_profile(), str(profile_path))

    code = parsing_acceptance.main([
        "--baseline", str(baseline_path),
        "--candidate", str(candidate_path),
        "--profile", str(profile_path),
    ])

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["status"] == "pass"
    assert payload["quality_gate_decision"] == "evaluated"
