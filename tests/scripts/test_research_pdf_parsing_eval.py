from __future__ import annotations

from pathlib import Path
import json

import pymupdf
import pytest

from scripts.research_pdf_parsing_eval import (
    _parser_config_fingerprint,
    _safe_error_diagnostic,
    production_environment,
    run_production_evaluation,
    verify_marker_weights_manifest,
)


def _evidence(*, device: str = "cpu", timeout: int = 1800) -> dict:
    return {
        "request": {
            "image": "newsroom-marker@sha256:" + "a" * 64,
            "argv": [
                "/usr/local/bin/marker_single",
                "/input/input.pdf",
                "--disable_multiprocessing",
                "--disable_tqdm",
            ],
            "environment": {
                "TORCH_DEVICE": device,
                "OPENBLAS_NUM_THREADS": "1",
                "OMP_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
            },
            "profile": {"profile_id": "research-marker"},
            "resource_limits": {"max_processes": 1, "max_memory_bytes": 1024},
            "timeout_seconds": timeout,
        }
    }


def test_marker_manifest_requires_verified_offline_font(
    monkeypatch, tmp_path: Path,
) -> None:
    from hashlib import sha256
    import scripts.research_pdf_parsing_eval as runner

    cache = tmp_path / "cache"
    model = cache / "datalab" / "models" / "model.bin"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": runner.WEIGHTS_MANIFEST_SCHEMA,
        "files": [{
            "path": "datalab/models/model.bin",
            "sha256": sha256(b"model").hexdigest(),
            "size": len(b"model"),
        }],
    }), encoding="utf-8")

    try:
        verify_marker_weights_manifest(cache, manifest)
    except ValueError as error:
        assert "official Marker offline font" in str(error)
    else:
        raise AssertionError("missing offline font must fail closed")

    font = cache / runner.MARKER_OFFLINE_FONT_PATH
    font.parent.mkdir(parents=True)
    font_bytes = b"official-font-fixture"
    font.write_bytes(font_bytes)
    monkeypatch.setattr(runner, "MARKER_OFFLINE_FONT_SHA256", sha256(font_bytes).hexdigest())
    monkeypatch.setattr(runner, "MARKER_OFFLINE_FONT_SIZE", len(font_bytes))
    manifest.write_text(json.dumps({
        "schema": runner.WEIGHTS_MANIFEST_SCHEMA,
        "files": [
            {
                "path": "datalab/models/model.bin",
                "sha256": sha256(b"model").hexdigest(),
                "size": len(b"model"),
            },
            {
                "path": runner.MARKER_OFFLINE_FONT_PATH,
                "sha256": sha256(font_bytes).hexdigest(),
                "size": len(font_bytes),
            },
        ],
    }), encoding="utf-8")

    assert verify_marker_weights_manifest(cache, manifest).startswith("sha256:")


def test_marker_manifest_rejects_substituted_offline_font(
    monkeypatch, tmp_path: Path,
) -> None:
    from hashlib import sha256
    import scripts.research_pdf_parsing_eval as runner

    cache = tmp_path / "cache"
    font = cache / runner.MARKER_OFFLINE_FONT_PATH
    font.parent.mkdir(parents=True)
    substituted = b"substituted-font"
    font.write_bytes(substituted)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema": runner.WEIGHTS_MANIFEST_SCHEMA,
        "files": [{
            "path": runner.MARKER_OFFLINE_FONT_PATH,
            "sha256": sha256(substituted).hexdigest(),
            "size": len(substituted),
        }],
    }), encoding="utf-8")

    try:
        verify_marker_weights_manifest(cache, manifest)
    except ValueError as error:
        assert "official Marker offline font" in str(error)
    else:
        raise AssertionError("substituted offline font must fail closed")




def test_parser_config_fingerprint_tracks_effective_execution_and_typed_settings(
    tmp_path: Path,
) -> None:
    weights = tmp_path / "weights.json"
    weights.write_text("{}", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()

    base = _parser_config_fingerprint(
        execution_evidence=_evidence(),
        cache=cache,
        weights_manifest=weights,
        reading_order_profile="backend_preserved",
        recovery_enabled=False,
        allow_abstract_fallback=False,
    )
    assert base != _parser_config_fingerprint(
        execution_evidence=_evidence(timeout=900),
        cache=cache,
        weights_manifest=weights,
        reading_order_profile="backend_preserved",
        recovery_enabled=False,
        allow_abstract_fallback=False,
    )
    assert base != _parser_config_fingerprint(
        execution_evidence=_evidence(device="cuda"),
        cache=cache,
        weights_manifest=weights,
        reading_order_profile="layout_admitted_v1",
        recovery_enabled=True,
        allow_abstract_fallback=False,
    )


def test_production_environment_freezes_parser_policy(
    tmp_path: Path,
) -> None:
    env = production_environment(
        research_root=tmp_path / "research",
        marker_image="newsroom-marker@sha256:" + "a" * 64,
        marker_cache=tmp_path / "cache",
        marker_weights_manifest=tmp_path / "weights.json",
        marker_timeout_seconds=1800,
        marker_device="cpu",
        reading_order_profile="backend_preserved",
    )
    assert env["NEWSROOM_DESKTOP_RESEARCH_LLM_ENABLED"] == "1"
    assert env["NEWSROOM_PARSER_RUN_ROOT"] == str(tmp_path / "research" / "parser-runs")
    assert env["NEWS_ARTIFACT_ROOT"] == str(tmp_path / "research" / "artifacts")
    assert env["NEWS_DATABASE_DSN"] == ""
    assert env["NEWS_RESEARCH_ALLOW_ABSTRACT_FALLBACK"] == "false"
    assert env["NEWS_RESEARCH_PARSER_RECOVERY_ENABLED"] == "false"
    assert env["NEWS_RESEARCH_PARSER_TIMEOUT_SECONDS"] == "1800"
    assert env["NEWS_RESEARCH_PARSER_READING_ORDER_PROFILE"] == "backend_preserved"
    assert env["NEWSROOM_MARKER_TORCH_DEVICE"] == "cpu"


def test_preflight_failure_writes_not_run_ledger_for_every_requested_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    document = pymupdf.open()
    document.new_page()
    source.write_bytes(document.tobytes())
    document.close()
    from hashlib import sha256
    digest = sha256(source.read_bytes()).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "research-pdf-parsing-inputs/v1",
        "run_id": "preflight-ledger",
        "baseline_identity": "B1_current_not_historical_preoptimization",
        "documents": [{
            "document_id": "document-1",
            "source_path": str(source.resolve()),
            "source_sha256": digest,
            "input_mode": "raster_pdf",
            "exposure": "exposed_dev",
            "expected_pages": 1,
        }],
    }), encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "model.bin").write_bytes(b"model")
    weights = tmp_path / "weights.json"
    weights.write_text("{}", encoding="utf-8")
    output = tmp_path / "output"

    def fail_composition():
        raise RuntimeError("invalid runtime route")

    code = run_production_evaluation(
        input_manifest=manifest,
        output=output,
        marker_image="newsroom-marker@sha256:" + "a" * 64,
        marker_cache=cache,
        marker_weights_manifest=weights,
        repository_root=Path.cwd(),
        composition_factory=fail_composition,
    )

    assert code == 2
    run = json.loads((output / "run.json").read_text(encoding="utf-8"))
    assert run["not_run_documents"] == 1
    assert run["preflight_error"]["cause_type"] == "RuntimeError"
    result = json.loads(
        (output / "documents" / "document-1" / "result.json").read_text(encoding="utf-8")
    )
    ledger = json.loads(
        (output / "documents" / "document-1" / "page-ledger.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "not_run"
    assert result["reason_code"] == "configuration_preflight_failed"
    assert ledger["pages"] == [{
        "page": 1,
        "status": "not_run",
        "reason": "configuration_preflight_failed",
    }]


def test_harness_error_diagnostic_preserves_typed_code_and_sanitizes_secrets() -> None:
    from framework.harness.control_plane.errors import HarnessValidationError

    error = HarnessValidationError(
        "binding rejected api_key=super-secret",
        code="graph_binding_rejected",
        details={
            "reason_code": "activity_unavailable",
            "api_key": "super-secret",
            "nested": {"authorization": "Bearer secret", "safe": "kept"},
        },
    )

    code, details = _safe_error_diagnostic(error, fallback="failed")

    assert code == "graph_binding_rejected"
    assert details["reason_code"] == "activity_unavailable"
    assert details["api_key"] == "<redacted>"
    assert details["nested"]["authorization"] == "<redacted>"
    assert details["nested"]["safe"] == "kept"
    assert "super-secret" not in details["message"]


@pytest.mark.parametrize("document_id", ["trailing ", "C:stream", "CON.txt", None])
def test_input_manifest_does_not_normalize_unsafe_document_identifier(document_id, tmp_path):
    from scripts.research_pdf_parsing_eval import load_input_manifest
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps({
        "schema_version": "research-pdf-parsing-inputs/v1",
        "run_id": "safe-admission", "baseline_identity": "B1_current_not_historical_preoptimization",
        "documents": [{"document_id": document_id, "source_path": str((tmp_path / "input.pdf").resolve()),
                       "source_sha256": "a" * 64, "input_mode": "raster_pdf",
                       "exposure": "exposed_dev", "expected_pages": 1}],
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="document_id"):
        load_input_manifest(path)
