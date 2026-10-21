from __future__ import annotations

import hashlib
import json

import numpy as np
import pytest
from pydantic import ValidationError

from backend.research.rag.evaluation.protocol_execution import CodeIdentity
from backend.research.rag.evaluation.qasper_baseline import (
    _build_documents,
    QasperBaselineCaptureError,
    capture_qasper_baseline,
    sha256_file,
    validate_embedding_cache,
)
from backend.research.rag.evaluation.qasper_protocol import HistoricalCodeClaim, QasperBaselineSnapshot


PAPER_ID = "paper-1"
EVIDENCE = "A deterministic evidence paragraph."


def _historical_claim() -> HistoricalCodeClaim:
    return HistoricalCodeClaim(
        git_revision="fixture-handoff-20261021",
        source="unit-test fixture",
        claim="fixture declaration only; not a historical observation",
    )


def _code_identity() -> CodeIdentity:
    return CodeIdentity.create(
        git_revision="3d20584500000000000000000000000000000000",
        workspace_content_fingerprint="1" * 64,
        mutable_patch_fingerprints=(),
        dirty=False,
    )


def _dataset() -> dict:
    return {
        PAPER_ID: {
            "title": "Fixture paper",
            "abstract": "Short abstract",
            "full_text": [
                {"section_name": "Introduction", "paragraphs": ["Background paragraph."]},
                {"section_name": "Method", "paragraphs": ["Method paragraph."]},
                {"section_name": "Findings", "paragraphs": [EVIDENCE]},
            ],
            "qas": [
                {
                    "question_id": "q1",
                    "question": "What was found?",
                    "answers": [
                        {"answer": {"unanswerable": False, "evidence": [EVIDENCE]}},
                        {"answer": {"unanswerable": True, "evidence": []}},
                        {"answer": {"unanswerable": False, "evidence": ["FLOAT SELECTED: 4.2"]}},
                    ],
                },
                {
                    "question_id": "q2",
                    "question": "Is there another result?",
                    "answers": [{"answer": {"unanswerable": True, "evidence": []}}],
                },
            ],
        }
    }


def _summary() -> dict:
    return {
        "dataset": "QASPER v0.3 dev",
        "strict_metrics": {"hit_at_5": 0.5, "hit_at_10": 0.75},
        "equivalent_metrics": {"equivalent_hit_at_5": 0.6},
        "embedding_identity": {
            "provider": "fixture-provider",
            "model": "fixture-model",
            "dimension": 3,
        },
        "mapping": {
            "papers": 1,
            "questions_total": 2,
            "questions_with_text_evidence": 1,
            "questions_without_mappable_text_evidence": 1,
            "text_evidence_items_total": 1,
            "text_evidence_items_mapped": 1,
            "float_evidence_items_excluded": 1,
            "answer_annotations_unanswerable": 2,
        },
    }


def _write_inputs(tmp_path, *, dataset=None, summary=None, cache_keys=None, vectors=None):
    dataset_path = tmp_path / "dataset.json"
    result_path = tmp_path / "legacy.json"
    cache_path = tmp_path / "embeddings.npz"
    source_path = tmp_path / "evaluate_qasper.py"
    dataset_path.write_text(json.dumps(dataset or _dataset()), encoding="utf-8")
    result_path.write_text(json.dumps(summary or _summary()), encoding="utf-8")
    source_path.write_text("# deterministic fixture adapter\n", encoding="utf-8")
    keys = cache_keys if cache_keys is not None else [
        hashlib.sha256(f"fixture embedding key {index}".encode()).hexdigest()
        for index in range(32)
    ]
    matrix = vectors if vectors is not None else np.tile(
        np.asarray([[0.25, 0.5, 0.75]], dtype=np.float32), (len(keys), 1)
    )
    np.savez_compressed(cache_path, keys=np.asarray(keys), vectors=matrix)
    return dataset_path, cache_path, result_path, source_path


def _capture(tmp_path, **overrides):
    dataset, cache, result, source = _write_inputs(tmp_path)
    args = {
        "dataset_path": dataset,
        "embedding_cache_path": cache,
        "legacy_result_path": result,
        "source_script_paths": [source],
        "observed_reconstruction_code_identity": _code_identity(),
        "historical_code_claim": _historical_claim(),
        "embedding_model_version": "fixture-model-v1",
        "embedding_dimension": 3,
    }
    args.update(overrides)
    return capture_qasper_baseline(**args)


def test_capture_returns_protocol_snapshot_and_keeps_denominators_distinct(tmp_path):
    snapshot = _capture(tmp_path)

    assert isinstance(snapshot, QasperBaselineSnapshot)
    assert snapshot.qualification == "diagnostic_only"
    assert snapshot.dataset_case_count == 2
    assert snapshot.evaluable_case_count == 1
    assert snapshot.chunk_count > 0
    assert snapshot.corpus_sha256 != "0" * 64
    assert snapshot.historical_code_claim == _historical_claim()
    assert snapshot.embedding_model_id == "fixture-provider/fixture-model"
    assert snapshot.embedding_model_version == "fixture-model-v1"
    assert snapshot.execution_profile == "fixture"
    assert snapshot.observed_reconstruction_code_identity == _code_identity()
    assert {item.metric_id for item in snapshot.legacy_summary} == {
        "legacy_strict_hit_at_5",
        "legacy_strict_hit_at_10",
        "legacy_equivalent_hit_at_5",
    }
    snapshot.verify_digest()


def test_capture_is_deterministic_for_fixed_inputs_and_identity(tmp_path):
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _capture(first_dir)
    second = _capture(second_dir)

    assert first.corpus_sha256 == second.corpus_sha256
    assert first.digest == second.digest


def test_tampered_expected_file_digest_fails(tmp_path):
    dataset, cache, result, source = _write_inputs(tmp_path)
    with pytest.raises(QasperBaselineCaptureError, match="tampered dataset"):
        capture_qasper_baseline(
            dataset_path=dataset,
            embedding_cache_path=cache,
            legacy_result_path=result,
            source_script_paths=[source],
            observed_reconstruction_code_identity=_code_identity(),
            historical_code_claim=_historical_claim(),
            embedding_model_version="fixture-model-v1",
            expected_file_sha256={"dataset": "0" * 64},
        )


def test_summary_count_mismatch_fails_and_float_or_annotations_are_not_cases(tmp_path):
    wrong = _summary()
    wrong["mapping"]["questions_total"] = 4
    dataset, cache, result, source = _write_inputs(tmp_path, summary=wrong)
    with pytest.raises(QasperBaselineCaptureError, match="counts disagree"):
        capture_qasper_baseline(
            dataset_path=dataset,
            embedding_cache_path=cache,
            legacy_result_path=result,
            source_script_paths=[source],
            observed_reconstruction_code_identity=_code_identity(),
            historical_code_claim=_historical_claim(),
            embedding_model_version="fixture-model-v1",
        )


def test_legacy_count_mismatch_requires_explicit_diagnostic_override(tmp_path):
    summary = _summary()
    summary["mapping"]["questions_total"] = 3
    summary["mapping"]["questions_with_text_evidence"] = 2
    dataset, cache, result, source = _write_inputs(tmp_path, summary=summary)
    common = {
        "dataset_path": dataset,
        "embedding_cache_path": cache,
        "legacy_result_path": result,
        "source_script_paths": [source],
        "observed_reconstruction_code_identity": _code_identity(),
        "historical_code_claim": _historical_claim(),
        "embedding_model_version": "fixture-model-v1",
    }

    with pytest.raises(QasperBaselineCaptureError, match="counts disagree"):
        capture_qasper_baseline(**common)

    snapshot = capture_qasper_baseline(**common, allow_legacy_count_mismatch=True)
    assert snapshot.legacy_count_policy == "explicit_diagnostic_mismatch"
    assert snapshot.dataset_case_count == 2
    assert snapshot.evaluable_case_count == 1
    assert {item.denominator for item in snapshot.legacy_summary} == {2}
    snapshot.verify_digest()


def test_missing_dataset_cache_result_or_source_fails_clearly(tmp_path):
    dataset, cache, result, source = _write_inputs(tmp_path)
    common = {
        "dataset_path": dataset,
        "embedding_cache_path": cache,
        "legacy_result_path": result,
        "source_script_paths": [source],
        "observed_reconstruction_code_identity": _code_identity(),
        "historical_code_claim": _historical_claim(),
        "embedding_model_version": "fixture-model-v1",
    }
    for field, phrase in (("dataset_path", "dataset"), ("embedding_cache_path", "external file"), ("legacy_result_path", "external file")):
        changed = dict(common)
        changed[field] = tmp_path / "missing.json"
        with pytest.raises(QasperBaselineCaptureError, match=phrase):
            capture_qasper_baseline(**changed)
    with pytest.raises(QasperBaselineCaptureError, match="source script"):
        capture_qasper_baseline(**{**common, "source_script_paths": []})
    with pytest.raises(QasperBaselineCaptureError, match="external file"):
        capture_qasper_baseline(**{**common, "source_script_paths": [tmp_path / "missing.py"]})


def test_repeated_chunk_text_may_share_one_unique_cache_key(tmp_path):
    dataset = _dataset()
    dataset[PAPER_ID]["full_text"][1]["paragraphs"] = [EVIDENCE]
    dataset[PAPER_ID]["full_text"][2]["paragraphs"] = [EVIDENCE]
    dataset[PAPER_ID]["qas"][0]["answers"] = [
        {"answer": {"unanswerable": False, "evidence": [EVIDENCE]}}
    ]
    summary = _summary()
    summary["mapping"]["answer_annotations_unanswerable"] = 1
    summary["mapping"]["float_evidence_items_excluded"] = 0
    dataset, cache, result, source = _write_inputs(
        tmp_path,
        dataset=dataset,
        summary=summary,
        cache_keys=[hashlib.sha256(EVIDENCE.encode("utf-8")).hexdigest()],
        vectors=np.asarray([[0.25, 0.5, 0.75]], dtype=np.float32),
    )
    snapshot = capture_qasper_baseline(
        dataset_path=dataset,
        embedding_cache_path=cache,
        legacy_result_path=result,
        source_script_paths=[source],
        observed_reconstruction_code_identity=_code_identity(),
        historical_code_claim=_historical_claim(),
        embedding_model_version="fixture-model-v1",
    )

    assert snapshot.chunk_count > snapshot.embedding_cache_identity.rows
    assert snapshot.embedding_cache_identity.rows == 1
    assert snapshot.embedding_cache_identity.keys_sha256


def test_cache_dimension_keys_shape_and_finite_values_are_validated(tmp_path):
    keys = [hashlib.sha256(b"key1").hexdigest(), hashlib.sha256(b"key2").hexdigest()]
    cache = tmp_path / "cache.npz"
    np.savez(cache, keys=np.asarray(keys), vectors=np.ones((2, 4), dtype=np.float32))
    assert validate_embedding_cache(cache, expected_dimension=4)["rows"] == 2
    with pytest.raises(QasperBaselineCaptureError, match="dimension"):
        validate_embedding_cache(cache, expected_dimension=3)

    np.savez(cache, keys=np.asarray([keys[0], keys[0]]), vectors=np.ones((2, 4), dtype=np.float32))
    with pytest.raises(QasperBaselineCaptureError, match="duplicate keys"):
        validate_embedding_cache(cache)
    np.savez(cache, keys=np.asarray(["not-a-digest"]), vectors=np.ones((1, 4), dtype=np.float32))
    with pytest.raises(QasperBaselineCaptureError, match="SHA-256"):
        validate_embedding_cache(cache)
    np.savez(cache, keys=np.asarray([keys[0]]), vectors=np.ones((2, 4), dtype=np.float32))
    with pytest.raises(QasperBaselineCaptureError, match="row counts"):
        validate_embedding_cache(cache)
    np.savez(cache, keys=np.asarray([keys[0]]), vectors=np.asarray([[np.nan, 0, 0, 0]], dtype=np.float32))
    with pytest.raises(QasperBaselineCaptureError, match="finite"):
        validate_embedding_cache(cache)


def test_expected_script_digests_are_typed_and_unknown_keys_rejected(tmp_path):
    dataset, cache, result, source = _write_inputs(tmp_path)
    expected = {
        "dataset": sha256_file(dataset),
        "embedding_cache": sha256_file(cache),
        "legacy_result": sha256_file(result),
        "source_scripts": (sha256_file(source),),
    }
    snapshot = capture_qasper_baseline(
        dataset_path=dataset,
        embedding_cache_path=cache,
        legacy_result_path=result,
        source_script_paths=[source],
        observed_reconstruction_code_identity=_code_identity(),
        historical_code_claim=_historical_claim(),
        embedding_model_version="fixture-model-v1",
        expected_file_sha256=expected,
    )
    assert isinstance(snapshot, QasperBaselineSnapshot)
    with pytest.raises(QasperBaselineCaptureError, match="tuple of SHA-256"):
        capture_qasper_baseline(
            dataset_path=dataset,
            embedding_cache_path=cache,
            legacy_result_path=result,
            source_script_paths=[source],
            observed_reconstruction_code_identity=_code_identity(),
            historical_code_claim=_historical_claim(),
            embedding_model_version="fixture-model-v1",
            expected_file_sha256={**expected, "source_scripts": sha256_file(source)},
        )
    with pytest.raises(QasperBaselineCaptureError, match="unknown expected_file_sha256"):
        capture_qasper_baseline(
            dataset_path=dataset,
            embedding_cache_path=cache,
            legacy_result_path=result,
            source_script_paths=[source],
            observed_reconstruction_code_identity=_code_identity(),
            historical_code_claim=_historical_claim(),
            embedding_model_version="fixture-model-v1",
            expected_file_sha256={**expected, "mystery": "0" * 64},
        )


def test_hashing_is_content_based_and_streaming(tmp_path):
    path = tmp_path / "large.bin"
    content = b"abc" * 10000
    path.write_bytes(content)
    assert sha256_file(path, chunk_size=17) == hashlib.sha256(content).hexdigest()


def test_supplied_documents_and_chunks_must_match_reconstruction(tmp_path):
    dataset, cache, result, source = _write_inputs(tmp_path)
    documents, chunks = _build_documents(_dataset())
    altered = documents[0].model_copy(update={"title": "tampered"})
    with pytest.raises(QasperBaselineCaptureError, match="do not match deterministic"):
        capture_qasper_baseline(
            dataset_path=dataset,
            embedding_cache_path=cache,
            legacy_result_path=result,
            source_script_paths=[source],
            observed_reconstruction_code_identity=_code_identity(),
            historical_code_claim=_historical_claim(),
            embedding_model_version="fixture-model-v1",
            documents=[altered],
            chunks=chunks,
        )


def test_capture_does_not_open_network_connections(tmp_path, monkeypatch):
    import socket

    dataset, cache, result, source = _write_inputs(tmp_path)

    def fail_network(*_args, **_kwargs):
        raise AssertionError("offline baseline capture attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket, "socket", fail_network)
    snapshot = capture_qasper_baseline(
        dataset_path=dataset,
        embedding_cache_path=cache,
        legacy_result_path=result,
        source_script_paths=[source],
        observed_reconstruction_code_identity=_code_identity(),
        historical_code_claim=_historical_claim(),
        embedding_model_version="fixture-model-v1",
    )
    assert isinstance(snapshot, QasperBaselineSnapshot)


def test_duplicate_json_keys_and_unknown_chunk_ids_are_rejected(tmp_path):
    dataset, cache, result, source = _write_inputs(tmp_path)
    dataset.write_text('{"paper-1": {}, "paper-1": {}}', encoding="utf-8")
    with pytest.raises(QasperBaselineCaptureError, match="duplicate JSON"):
        capture_qasper_baseline(
            dataset_path=dataset,
            embedding_cache_path=cache,
            legacy_result_path=result,
            source_script_paths=[source],
            observed_reconstruction_code_identity=_code_identity(),
            historical_code_claim=_historical_claim(),
            embedding_model_version="fixture-model-v1",
        )
