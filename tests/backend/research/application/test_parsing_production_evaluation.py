from __future__ import annotations

import pytest

from backend.research.application.parsing_production_evaluation import (
    ProductionParsingEvaluationError,
    _validate_chunks,
)
from framework.rag.core import content_fingerprint


def _chunks():
    source = "a" * 64
    document = "document-1"
    content = "Persisted production content"
    digest = content_fingerprint(content)
    entry = {
        "chunk_id": "chunk-1",
        "document_id": document,
        "source_hash": source,
        "content_hash": digest,
        "parent_chunk_id": None,
    }
    chunk = {
        "chunk_id": "chunk-1",
        "content": content,
        "parent_chunk_id": None,
        "metadata": {"content_hash": digest},
    }
    manifest = {
        "document_id": document,
        "source_hash": source,
        "pipeline_fingerprint": "sha256:pipeline",
        "chunks": [entry],
    }
    payload = {
        "document_id": document,
        "source_hash": source,
        "pipeline_fingerprint": "sha256:pipeline",
        "chunks": [chunk],
    }
    return source, document, manifest, payload


def test_persisted_chunk_snapshot_accepts_exact_content_and_lineage() -> None:
    source, document, manifest, payload = _chunks()
    _validate_chunks(source, document, manifest, payload)


def test_persisted_chunk_snapshot_recomputes_hash_and_rejects_tamper() -> None:
    source, document, manifest, payload = _chunks()
    payload["chunks"][0]["content"] = "tampered"
    with pytest.raises(ProductionParsingEvaluationError) as captured:
        _validate_chunks(source, document, manifest, payload)
    assert captured.value.code == "chunk_content_hash_mismatch"


def test_persisted_chunk_snapshot_rejects_duplicate_ids() -> None:
    source, document, manifest, payload = _chunks()
    payload["chunks"].append(dict(payload["chunks"][0]))
    with pytest.raises(ProductionParsingEvaluationError) as captured:
        _validate_chunks(source, document, manifest, payload)
    assert captured.value.code == "duplicate_chunk_id"


def test_persisted_chunk_snapshot_rejects_foreign_source() -> None:
    source, document, manifest, payload = _chunks()
    manifest["chunks"][0]["source_hash"] = "b" * 64
    with pytest.raises(ProductionParsingEvaluationError) as captured:
        _validate_chunks(source, document, manifest, payload)
    assert captured.value.code == "chunk_lineage_mismatch"


def test_persisted_chunk_snapshot_fails_closed_when_store_payload_is_missing() -> None:
    source, document, manifest, payload = _chunks()
    payload["chunks"] = []
    with pytest.raises(ProductionParsingEvaluationError) as captured:
        _validate_chunks(source, document, manifest, payload)
    assert captured.value.code == "chunk_payload_missing"


@pytest.mark.parametrize("document_id", [
    "../escape", "a/b", "a\\b", "C:escape", "a:stream", "a<bad", "a>bad",
    "a|bad", "a?bad", "a*bad", "a\x00bad", "a\nbad", " trailing", "trailing ",
    "trailing.", ".", "..", "CON", "con.txt", "PRN", "aux.json", "NUL",
    "COM1", "lpt9.txt", "CONIN$", "a" * 129, None, 123,
])
def test_production_document_identifier_rejects_nonportable_paths(document_id, tmp_path):
    from backend.research.application.parsing_production_evaluation import ProductionParsingDocumentInput
    with pytest.raises(ValueError, match="document_id"):
        ProductionParsingDocumentInput(document_id, tmp_path / "input.pdf", "a" * 64,
                                       "raster_pdf", "exposed_dev", 1)


def test_production_document_identifiers_reject_casefold_collision(tmp_path):
    from backend.research.application.parsing_production_evaluation import (
        ProductionParsingDocumentInput, validate_unique_document_sources,
    )
    documents = [
        ProductionParsingDocumentInput("Paper-A", tmp_path / "a.pdf", "a" * 64,
                                       "raster_pdf", "exposed_dev", 1),
        ProductionParsingDocumentInput("paper-a", tmp_path / "b.pdf", "b" * 64,
                                       "raster_pdf", "exposed_dev", 1),
    ]
    with pytest.raises(ValueError, match="document_id"):
        validate_unique_document_sources(documents)


def _v9_fixture():
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parents[3] / "fixtures" / "research" /
                       "production_parsing_v9_durable_events.json").read_text(encoding="utf-8"))


def _validate_v9(fixture):
    from framework.execution_environment.models import ExecutionReceipt
    from backend.research.application.parsing_production_evaluation import validate_durable_parsing_harness_evidence
    phases = [event["payload"]["graph_phase_transition"] for event in fixture["events"]
              if event["event_type"] == "graph_phase_transition_recorded"]
    validate_durable_parsing_harness_evidence(
        ExecutionReceipt.from_dict(fixture["receipt"]), phases, fixture["events"]
    )


def test_real_v9_sqlite_envelopes_join_stage_phases_to_physical_receipt():
    fixture = _v9_fixture()
    phases = [event for event in fixture["events"]
              if event["event_type"] == "graph_phase_transition_recorded"]
    assert [event["stream_sequence"] for event in phases] == [7, 12, 21, 22, 25, 49]
    assert all(event["payload"]["graph_phase_transition"]["context"]["execution_identity"] is None
               for event in phases)
    assert [event["payload"]["graph_phase_transition"]["attempt"] for event in phases] == [0, 0, 1, 1, 1, 1]
    _validate_v9(fixture)


@pytest.mark.parametrize("mutation", ["checksum", "order", "missing_result", "receipt_attempt", "phase_sequence"])
def test_real_v9_envelopes_fail_closed_on_tamper_or_broken_activity_join(mutation):
    from framework.events.errors import EventRuntimeError
    fixture = _v9_fixture()
    if mutation == "checksum":
        fixture["events"][0]["record_checksum"] = "sha256:" + "0" * 64
    elif mutation == "order":
        fixture["events"] = list(reversed(fixture["events"]))
    elif mutation == "missing_result":
        fixture["events"] = [event for event in fixture["events"]
                             if event["event_type"] != "graph_worker_result_recorded"]
    elif mutation == "receipt_attempt":
        from dataclasses import replace
        from framework.execution_environment.models import ExecutionReceipt
        from backend.research.application.parsing_production_evaluation import validate_durable_parsing_harness_evidence
        receipt = ExecutionReceipt.from_dict(fixture["receipt"])
        receipt = replace(receipt, graph_identity=replace(receipt.graph_identity, attempt=2))
        phases = [event["payload"]["graph_phase_transition"] for event in fixture["events"]
                  if event["event_type"] == "graph_phase_transition_recorded"]
        with pytest.raises(ProductionParsingEvaluationError):
            validate_durable_parsing_harness_evidence(receipt, phases, fixture["events"])
        return
    else:
        fixture["events"][0]["payload"]["graph_phase_transition"]["event_sequence"] = 8
    with pytest.raises((ProductionParsingEvaluationError, EventRuntimeError)):
        _validate_v9(fixture)


@pytest.mark.parametrize("mutation", ["phase_sequence", "phase_attempt", "stage_node", "result_activity"])
def test_real_v9_checksum_valid_envelope_still_requires_phase_and_activity_join(mutation):
    from dataclasses import replace
    from framework.events.canonical import StoredEvent, thaw_canonical_json
    from framework.events.graph_phase import GraphPhaseTransitionRecord
    fixture = _v9_fixture()
    index = 0 if mutation != "result_activity" else 3
    event = StoredEvent.from_dict(fixture["events"][index])
    payload = thaw_canonical_json(event.payload or {})
    extensions = thaw_canonical_json(event.extensions)
    if mutation == "result_activity":
        extensions["graph_context"]["execution_identity"]["activity_id"] = "foreign-activity"
        # BusinessContext is separately authoritative; keep it unchanged so the
        # application's actual receipt join (not mere checksum verification) rejects.
    else:
        phase = GraphPhaseTransitionRecord.from_dict(payload["graph_phase_transition"])
        if mutation == "phase_sequence":
            phase = replace(phase, event_sequence=phase.event_sequence + 1)
        elif mutation == "phase_attempt":
            phase = replace(phase, attempt=1)
        else:
            context = replace(phase.context, stage_identity=replace(
                phase.context.stage_identity, node_instance_id="foreign-node-instance"))
            phase = replace(phase, context=context)
            extensions["graph_context"] = context.to_dict()
        payload["graph_phase_transition"] = phase.to_dict()
    candidate = replace(event.candidate, payload=payload or None, extensions=extensions)
    tampered = replace(event, candidate=candidate)
    tampered.verify_integrity()
    fixture["events"][index] = tampered.to_dict()
    from framework.events.errors import EventRuntimeError
    with pytest.raises((ProductionParsingEvaluationError, EventRuntimeError)):
        _validate_v9(fixture)
