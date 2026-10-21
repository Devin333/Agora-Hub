from __future__ import annotations

from pathlib import Path

import pytest

from backend.research.application.parse_paper import ParsePaperRequest, ParsePaperResult, ParsePaperUseCase
from backend.research.application.production_parsing_graph import ProductionParsingGraphApplicationService
from backend.research.application.single_paper_runtime import ResearchPhysicalGraphRunner
from backend.research.domain.common import SourceLineage
from backend.research.domain.document import ResearchDocument
from framework.harness import (
    InMemoryHarnessEventPort,
    InMemoryHarnessNodeOutputResource,
    InMemoryHarnessSideEffectStore,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.harness.control_plane.errors import HarnessValidationError


class _ScopedEventPort(InMemoryHarnessEventPort):
    def graph_scope_metadata(self):
        return {"identity_scope_ref": "sha256:" + "d" * 64}


class _UseCase(ParsePaperUseCase):
    def __init__(self, *, status: str = "parsed") -> None:
        self.execution_identity = None
        self.status = status

    def parse(self, request, *, execution_identity=None):
        self.execution_identity = execution_identity
        document = ResearchDocument(
            paper_id="paper-1",
            source_hash="a" * 64,
            lineage=SourceLineage(source_refs=["file://source.pdf"], source_hash="a" * 64),
            metadata={
                "execution_receipt_ref": "execution-receipt://exec-1",
                "execution_evidence_artifact_ref": "artifact://research/parser-execution-evidence/evidence-1",
            },
        )
        return ParsePaperResult(
            run_id=request.run_id,
            paper_id="paper-1",
            status=self.status,
            document=document,
            chunk_manifest_ref="artifact://research/research-chunk-manifest/" + "b" * 64,
        )


class _Chunks:
    def read(self, ref, *, actor_scope, include_payload=False, max_chars=0):
        assert include_payload is True
        if "parser-execution-evidence" in ref:
            return {
                "payload": {
                    "schema_version": "research-parser-execution-evidence/v1",
                    "ref": "parser-execution-evidence://" + "c" * 64,
                    "checksum": "sha256:" + "c" * 64,
                    "request": {},
                    "receipt": {"execution_id": "exec-1"},
                }
            }
        return {"payload": {"chunks": [{"chunk_id": "chunk-1", "content": "real"}]}}


def test_production_parsing_graph_supplies_harness_identity_and_reads_durable_phases() -> None:
    use_case = _UseCase()
    event_port = _ScopedEventPort()
    side_effect_store = InMemoryHarnessSideEffectStore()
    service = ProductionParsingGraphApplicationService(
        parse_use_case=use_case,
        harness_runner=ResearchPhysicalGraphRunner(
            event_port_factory=lambda _run_id: event_port,
            node_output_resource=InMemoryHarnessNodeOutputResource(),
            side_effect_store=side_effect_store,
        ),
        side_effect_store=side_effect_store,
        chunk_artifact_reader=_Chunks(),
    )
    result = service.run(ParsePaperRequest(source="file:///source.pdf", run_id="run-1"))

    assert result.harness_result.succeeded
    assert isinstance(use_case.execution_identity, GraphExecutionIdentity)
    assert use_case.execution_identity.run_id == "run-1"
    assert [(item["phase"], item["boundary"]) for item in result.durable_phase_records] == [
        ("plan", "entry"), ("plan", "exit"),
        ("execute", "entry"), ("execute", "exit"),
        ("verify", "entry"), ("verify", "exit"),
    ]


def test_production_parsing_graph_gate_records_verify_failure_for_failed_parse() -> None:
    use_case = _UseCase(status="failed")
    event_port = _ScopedEventPort()
    side_effect_store = InMemoryHarnessSideEffectStore()
    service = ProductionParsingGraphApplicationService(
        parse_use_case=use_case,
        harness_runner=ResearchPhysicalGraphRunner(
            event_port_factory=lambda _run_id: event_port,
            node_output_resource=InMemoryHarnessNodeOutputResource(),
            side_effect_store=side_effect_store,
        ),
        side_effect_store=side_effect_store,
        chunk_artifact_reader=_Chunks(),
    )

    with pytest.raises(HarnessValidationError) as captured:
        service.run(ParsePaperRequest(source="file:///source.pdf", run_id="run-failed"))

    assert captured.value.code == "production_parsing_graph_failed"
    phases = [
        (event.payload.get("phase"), event.payload.get("boundary"))
        for event in event_port.read_history("run-failed")
        if event.event_type.value == "graph_phase_transition_recorded"
    ]
    assert ("verify", "entry") in phases
    assert ("verify", "exit") in phases


@pytest.mark.parametrize("structured", [False, True])
def test_sqlite_durable_graph_roundtrip_and_final_evaluation_receipt_join(tmp_path, structured):
    """Fake parsing data, real Harness owner/EventRuntime/SQLite readback and caller."""
    from datetime import UTC, datetime
    from hashlib import sha256
    from cryptography.fernet import Fernet
    from infrastructure.storage.events.factory import durable_event_storage_from_env
    from backend.research.application.parsing_production_evaluation import (
        DurableExecutionEvidence, ProductionParsingDocumentInput,
        ProductionParsingEvaluationService, ProductionParsingRuntimeResult,
    )
    from framework.execution_environment.models import ExecutionReceipt, ExecutionStatus
    from framework.harness.graph.canonical import canonical_checksum
    from framework.rag.core import content_fingerprint

    source = tmp_path / "input.pdf"
    source.write_bytes(b"%PDF fake source bytes for application contract")
    output = tmp_path / "marker-output"
    output.mkdir()
    source_hash = sha256(source.read_bytes()).hexdigest()
    evidence = {}
    import json
    from backend.research.document.chunker import PaperDocumentChunker
    from backend.research.document.chunk_manifest import ChunkManifestManager
    from backend.research.domain.document import ResearchSection, ResearchEquation, ResearchFigure, ResearchTable
    from infrastructure.research.catalog_store import FilesystemResearchCatalogStore
    catalog_store = FilesystemResearchCatalogStore(tmp_path / "catalog")
    content = "Persisted fixture body. Equation 1, Figure 1, and Table 1 are discussed here."
    locator = f"urn:sha256:{source_hash}#page=1"
    template = ResearchDocument(
        paper_id="paper-1", document_id="doc-1", source_hash=source_hash,
        lineage=SourceLineage(source_refs=[locator], source_hash=source_hash),
        status="degraded", parser_backend="marker",
        sections=[ResearchSection(section_id=f"body-{n}", title=f"Body {n}",
                                  text=content + "\n\nSecond paragraph with contextual overlap.", source_ref=locator)
                  for n in range(3 if structured else 1)],
        equations=[ResearchEquation(equation_id="eq1", latex=r"E=mc^2", source_ref=locator)],
        figures=[ResearchFigure(figure_id="fig1", caption="Figure 1: real caption", source_ref=locator)],
        tables=[ResearchTable(table_id="table1", caption="Table 1: results", columns=["metric"],
                              rows=[{"metric": str(n)} for n in range(25)], source_ref=locator)],
        metadata={"parse_source": "marker", "parser_backend": "marker"},
    )
    # This is the real manifest owner path: chunk once, resolve persisted IDs,
    # publish full PaperChunks, and read the existing catalog's immutable ref.
    manager = ChunkManifestManager(catalog_store.root / "chunk-manifests")
    chunks = manager.resolve_chunk_ids(template.paper_id, PaperDocumentChunker().chunk(template, "marker"),
                                       document_id=template.document_id, source_hash=source_hash)
    manifest_path = manager.write(template.paper_id, chunks, document_id=template.document_id,
                                  source_hash=source_hash, document_metadata=dict(template.metadata))
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    common = {"document_id": template.document_id, "source_hash": source_hash,
              "paper_id": template.paper_id, "pipeline_fingerprint": None}
    chunk_payload = {**common, "manifest_path": str(manifest_path),
                     "chunks": [chunk.model_dump(mode="json", exclude_none=True) for chunk in chunks]}
    chunk_ref = catalog_store.publish(artifact_type="research-chunk-manifest", payload=chunk_payload,
                                      metadata={"paper_id": template.paper_id, "source_hash": source_hash})

    class _ParsingUseCase(_UseCase):
        def parse(self, request, *, execution_identity=None):
            self.execution_identity = execution_identity
            now = datetime.now(UTC)
            receipt = ExecutionReceipt(
                execution_id="exec:marker:sqlite-fixture", tool_id="research.document-parser.marker",
                graph_identity=execution_identity, operation_id="research-parser:marker:paper-1",
                attempt_id="attempt:marker:sqlite-fixture", provider_id="docker",
                provider_capability_checksum="sha256:" + "e" * 64,
                status=ExecutionStatus.SUCCEEDED, started_at=now, finished_at=now,
                termination_confirmed=True, exit_code=0, reason_code="process_exit",
            )
            request_payload = {
                "execution_id": receipt.execution_id, "tool_id": receipt.tool_id,
                "graph_identity": execution_identity.to_dict(),
                "operation_id": receipt.operation_id, "attempt_id": receipt.attempt_id,
                "profile": {"provider_id": "docker"},
                "argv": ["/usr/local/bin/marker_single", str(source), "--output_dir", str(output)],
                "read_roots": [str(tmp_path)], "write_roots": [str(output)],
            }
            body = {"schema_version": "research-parser-execution-evidence/v1",
                    "request": request_payload, "receipt": receipt.to_dict()}
            checksum = canonical_checksum(body)
            evidence.update(body, checksum=checksum,
                            ref="parser-execution-evidence://" + checksum.removeprefix("sha256:"))
            document = template.model_copy(update={"metadata": {
                **template.metadata,
                "execution_receipt_ref": "execution-receipt://" + receipt.execution_id,
                "execution_evidence_artifact_ref": "artifact://research/parser-execution-evidence/test",
            }})
            return ParsePaperResult(run_id=request.run_id, paper_id="paper-1", status="parsed",
                                   document=document, chunk_manifest_ref=chunk_ref)

    class _Artifacts:
        def read(self, ref, **kwargs):
            if "parser-execution-evidence" in ref:
                return {"payload": evidence}
            return catalog_store.read(ref, actor_scope=kwargs.get("actor_scope", {}),
                                      include_payload=True, max_chars=1_000_000)

    storage = durable_event_storage_from_env(
        artifact_root=tmp_path / "events",
        env={"NEWS_ACTIVITY_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii")},
    )
    ports = []
    def port_factory(run_id):
        port = storage.create_harness_transition_port(tenant_id="research-runtime")
        ports.append(port)
        return port

    use_case = _ParsingUseCase()
    side_effects = InMemoryHarnessSideEffectStore()
    graph_service = ProductionParsingGraphApplicationService(
        parse_use_case=use_case,
        harness_runner=ResearchPhysicalGraphRunner(
            event_port_factory=port_factory,
            node_output_resource=InMemoryHarnessNodeOutputResource(),
            side_effect_store=side_effects,
        ),
        side_effect_store=side_effects, chunk_artifact_reader=_Artifacts(),
        durable_event_reader=storage.event_store, event_tenant_id="research-runtime",
    )
    captured = []
    runtime_results = []
    class _Runtime:
        def parse_document(self, request):
            result = graph_service.run(ParsePaperRequest(source=source.as_uri(), run_id=request.run_id))
            captured.append(result)
            snapshot = ProductionParsingRuntimeResult(
                document=result.parse_result.document, chunk_manifest=manifest, chunk_payload=result.chunk_payload,
                backend_raw_refs={"marker_json": "fixture-marker.json"},
                execution_evidence=DurableExecutionEvidence.from_dict(result.execution_evidence),
                durable_phase_records=result.durable_phase_records,
                durable_harness_events=result.durable_harness_events,
                chunk_artifact=result.chunk_artifact,
                runtime_fingerprints={key: "sha256:" + "a" * 64 for key in (
                    "composition", "runtime_config", "parser", "parser_image_digest",
                    "parser_weights_digest", "parser_config_sha256", "document_schema",
                    "normalizer", "exporter", "chunker", "code_revision",
                    "source_content_sha256", "source_manifest_sha256")},
            )
            runtime_results.append(snapshot)
            return snapshot

    result = ProductionParsingEvaluationService(_Runtime(), artifact_reader=catalog_store).evaluate_document(
        run_id="sqlite-parsing-join", document=ProductionParsingDocumentInput(
            "paper-1", source, source_hash, "raster_pdf", "exposed_dev", 1),
    )
    assert result.evidence_checks["harness_plan_execute_verify_verified"] is True
    assert result.document.status == "degraded"
    assert result.execution_evidence.receipt.graph_identity == use_case.execution_identity
    assert len(result.durable_harness_events) == 9
    assert [record["attempt"] for record in result.durable_phase_records] == [0, 0, 1, 1, 1, 1]
    assert all(record["context"]["execution_identity"] is None for record in result.durable_phase_records)
    history = ports[0].read_history("sqlite-parsing-join")
    wrapped = [event.payload["graph_phase_transition"] for event in history
               if event.event_type.value == "graph_phase_transition_recorded"]
    from framework.events.graph_phase import GraphPhaseTransitionRecord
    assert [GraphPhaseTransitionRecord.from_dict(item).to_dict() for item in wrapped] == list(
        captured[0].durable_phase_records
    )

    assert {"paragraph", "formula", "figure", "table"} <= {chunk["chunk_type"] for chunk in result.chunk_payload["chunks"]}
    assert any(chunk["metadata"]["content_hash"] != content_fingerprint(chunk["content"])
               for chunk in result.chunk_payload["chunks"] if chunk["chunk_type"] == "formula")
    # Operational manifest_path differs from the portable stored path: integrity
    # must be checked by read(), not by hashing the operational response.
    assert Path(result.chunk_payload["manifest_path"]).is_absolute()
    from copy import deepcopy
    from dataclasses import replace
    from backend.research.application.parsing_production_evaluation import ProductionParsingEvaluationError
    good = runtime_results[0]
    class _SnapshotRuntime:
        def __init__(self, value):
            self.value = value
        def parse_document(self, request):
            return self.value
    def evaluate_snapshot(value):
        return ProductionParsingEvaluationService(
            _SnapshotRuntime(value), artifact_reader=catalog_store,
        ).evaluate_document(run_id="sqlite-parsing-join", document=ProductionParsingDocumentInput(
            "paper-1", source, source_hash, "raster_pdf", "exposed_dev", 1))

    for chunk_type in ("formula", "figure", "table"):
        tampered = deepcopy(dict(good.chunk_payload))
        special = next(chunk for chunk in tampered["chunks"] if chunk["chunk_type"] == chunk_type)
        special["content"] += "\nTampered context but original semantic hash."
        with pytest.raises(ProductionParsingEvaluationError) as error:
            evaluate_snapshot(replace(good, chunk_payload=tampered))
        assert error.value.code == "chunk_payload_integrity_mismatch"

    forged = deepcopy(dict(good.chunk_artifact))
    forged["artifactRef"] = "artifact://research/research-chunk-manifest/" + "0" * 64
    forged["contentChecksum"] = "sha256:" + "0" * 64
    with pytest.raises(ProductionParsingEvaluationError) as error:
        evaluate_snapshot(replace(good, chunk_artifact=forged))
    assert error.value.code == "chunk_artifact_integrity_failed"

    # Even mutating both in-memory copies cannot defeat a fresh immutable read.
    tampered = deepcopy(dict(good.chunk_payload))
    tampered["chunks"][0]["content"] += " changed"
    forged = deepcopy(dict(good.chunk_artifact))
    forged["payload"] = tampered
    with pytest.raises(ProductionParsingEvaluationError) as error:
        evaluate_snapshot(replace(good, chunk_payload=tampered, chunk_artifact=forged))
    assert error.value.code == "chunk_payload_integrity_mismatch"

    # An on-disk edit is rejected by the existing reader's stored-envelope hash.
    artifact_file = catalog_store.artifact_root / (
        "research-chunk-manifest-" + chunk_ref.rsplit("/", 1)[1][:32] + ".json")
    on_disk = json.loads(artifact_file.read_text(encoding="utf-8"))
    on_disk["payload"]["chunks"][0]["content"] += " persisted tamper"
    artifact_file.write_text(json.dumps(on_disk), encoding="utf-8")
    with pytest.raises(ProductionParsingEvaluationError) as error:
        evaluate_snapshot(good)
    assert error.value.code == "chunk_artifact_integrity_failed"
