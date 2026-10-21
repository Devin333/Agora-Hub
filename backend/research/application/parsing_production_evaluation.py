"""Fail-closed application boundary for production PDF parsing evaluation."""
from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from backend.research.domain.document import ResearchDocument
from backend.research.ports.artifact_store import ResearchArtifactReadPort
from framework.events.graph_phase import GraphPhaseTransitionRecord
from framework.events.canonical import StoredEvent, thaw_canonical_json
from framework.events.projection import GraphEventContext
from framework.execution_environment.models import ExecutionReceipt
from framework.harness.graph.canonical import canonical_checksum
from framework.rag.core import content_fingerprint

_INPUT_MODES = frozenset({"native_pdf", "raster_pdf"})
_EXPOSURES = frozenset({"exposed_dev", "frozen_heldout"})
_EXPECTED_PHASES = (
    ("plan", "entry"),
    ("plan", "exit"),
    ("execute", "entry"),
    ("execute", "exit"),
    ("verify", "entry"),
    ("verify", "exit"),
)


class ProductionParsingEvaluationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = str(code).strip()
        self.message = str(message).strip()
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class ProductionParsingDocumentInput:
    document_id: str
    source_path: Path
    source_sha256: str
    input_mode: str
    exposure: str
    expected_pages: int

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str):
            raise ValueError("document_id must be a string")
        document_id = self.document_id
        reserved = {"con", "prn", "aux", "nul", "conin$", "conout$"}
        reserved.update(f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10))
        if (
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", document_id) is None
            or document_id.endswith(".")
            or document_id.split(".", 1)[0].casefold() in reserved
        ):
            raise ValueError("document_id must be a safe portable path segment")
        digest = str(self.source_sha256).strip().casefold()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
        mode = str(self.input_mode).strip()
        exposure = str(self.exposure).strip()
        if mode not in _INPUT_MODES:
            raise ValueError("input_mode must be native_pdf or raster_pdf")
        if exposure not in _EXPOSURES:
            raise ValueError("exposure must be exposed_dev or frozen_heldout")
        if type(self.expected_pages) is not int or self.expected_pages <= 0:
            raise ValueError("expected_pages must be a positive integer")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "source_path", Path(self.source_path))
        object.__setattr__(self, "source_sha256", digest)
        object.__setattr__(self, "input_mode", mode)
        object.__setattr__(self, "exposure", exposure)


@dataclass(frozen=True, slots=True)
class ProductionParsingRuntimeRequest:
    run_id: str
    document: ProductionParsingDocumentInput


@dataclass(frozen=True, slots=True)
class DurableExecutionEvidence:
    ref: str
    checksum: str
    request: Mapping[str, Any]
    receipt: ExecutionReceipt

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DurableExecutionEvidence":
        if not isinstance(value, Mapping):
            raise ValueError("execution evidence must be an object")
        expected = {"schema_version", "ref", "checksum", "request", "receipt"}
        if set(value) != expected or value.get("schema_version") != "research-parser-execution-evidence/v1":
            raise ValueError("execution evidence schema is invalid")
        request = value.get("request")
        receipt_payload = value.get("receipt")
        if not isinstance(request, Mapping) or not isinstance(receipt_payload, Mapping):
            raise ValueError("execution evidence request and receipt must be objects")
        body = {
            "schema_version": value["schema_version"],
            "request": dict(request),
            "receipt": dict(receipt_payload),
        }
        checksum = canonical_checksum(body)
        ref = f"parser-execution-evidence://{checksum.removeprefix('sha256:')}"
        if value.get("checksum") != checksum or value.get("ref") != ref:
            raise ValueError("execution evidence checksum is invalid")
        return cls(
            ref=ref,
            checksum=checksum,
            request=dict(request),
            receipt=ExecutionReceipt.from_dict(receipt_payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "research-parser-execution-evidence/v1",
            "ref": self.ref,
            "checksum": self.checksum,
            "request": dict(self.request),
            "receipt": self.receipt.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProductionParsingRuntimeResult:
    document: ResearchDocument
    chunk_manifest: Mapping[str, Any]
    chunk_payload: Mapping[str, Any]
    backend_raw_refs: Mapping[str, str]
    execution_evidence: DurableExecutionEvidence
    durable_phase_records: tuple[Mapping[str, Any], ...]
    runtime_fingerprints: Mapping[str, str]
    parser_attempts: tuple[Mapping[str, Any], ...] = ()
    durable_harness_events: tuple[Mapping[str, Any], ...] = ()
    chunk_artifact: Mapping[str, Any] = field(default_factory=dict)


class ProductionParsingRuntimePort(Protocol):
    def parse_document(self, request: ProductionParsingRuntimeRequest) -> ProductionParsingRuntimeResult: ...


@dataclass(frozen=True, slots=True)
class ProductionParsingEvaluationResult:
    run_id: str
    document_input: ProductionParsingDocumentInput
    document: ResearchDocument
    chunk_manifest: Mapping[str, Any]
    chunk_payload: Mapping[str, Any]
    backend_raw_refs: Mapping[str, str]
    execution_evidence: DurableExecutionEvidence
    durable_phase_records: tuple[Mapping[str, Any], ...]
    runtime_fingerprints: Mapping[str, str]
    parser_attempts: tuple[Mapping[str, Any], ...] = ()
    durable_harness_events: tuple[Mapping[str, Any], ...] = ()
    chunk_artifact: Mapping[str, Any] = field(default_factory=dict)
    evidence_checks: Mapping[str, bool] = field(default_factory=dict)


class ProductionParsingEvaluationService:
    def __init__(
        self, runtime: ProductionParsingRuntimePort, *, artifact_reader: ResearchArtifactReadPort,
    ) -> None:
        if not callable(getattr(runtime, "parse_document", None)):
            raise TypeError("runtime must implement ProductionParsingRuntimePort")
        if not isinstance(artifact_reader, ResearchArtifactReadPort):
            raise TypeError("artifact_reader must implement ResearchArtifactReadPort")
        self._runtime = runtime
        self._artifact_reader = artifact_reader

    def evaluate_document(
        self, *, run_id: str, document: ProductionParsingDocumentInput
    ) -> ProductionParsingEvaluationResult:
        normalized_run_id = str(run_id).strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        source_path = document.source_path.resolve(strict=True)
        if not source_path.is_file():
            raise ProductionParsingEvaluationError("source_not_file", "production source is not a file")
        actual = _sha256_file(source_path)
        if actual != document.source_sha256:
            raise ProductionParsingEvaluationError(
                "source_hash_mismatch",
                "production source does not match the frozen manifest",
                details={"expected": document.source_sha256, "actual": actual},
            )
        runtime_input = ProductionParsingDocumentInput(
            document_id=document.document_id,
            source_path=source_path,
            source_sha256=document.source_sha256,
            input_mode=document.input_mode,
            exposure=document.exposure,
            expected_pages=document.expected_pages,
        )
        result = self._runtime.parse_document(
            ProductionParsingRuntimeRequest(run_id=normalized_run_id, document=runtime_input)
        )
        self._validate_runtime_result(run_id=normalized_run_id, document=runtime_input, result=result)
        return ProductionParsingEvaluationResult(
            run_id=normalized_run_id,
            document_input=runtime_input,
            document=result.document,
            chunk_manifest=dict(result.chunk_manifest),
            chunk_payload=dict(result.chunk_payload),
            backend_raw_refs=dict(result.backend_raw_refs),
            execution_evidence=result.execution_evidence,
            durable_phase_records=tuple(dict(item) for item in result.durable_phase_records),
            runtime_fingerprints=dict(result.runtime_fingerprints),
            parser_attempts=tuple(dict(item) for item in result.parser_attempts),
            durable_harness_events=tuple(dict(item) for item in result.durable_harness_events),
            chunk_artifact=dict(result.chunk_artifact),
            evidence_checks={
                "source_hash_verified": True,
                "durable_execution_request_receipt_verified": True,
                "harness_plan_execute_verify_verified": True,
                "persisted_chunk_payload_verified": True,
            },
        )

    def _validate_runtime_result(
        self,
        *,
        run_id: str,
        document: ProductionParsingDocumentInput,
        result: ProductionParsingRuntimeResult,
    ) -> None:
        if not isinstance(result, ProductionParsingRuntimeResult):
            raise TypeError("runtime returned invalid ProductionParsingRuntimeResult")
        parsed = result.document
        if parsed.source_hash != document.source_sha256:
            raise ProductionParsingEvaluationError(
                "document_source_hash_mismatch", "ResearchDocument source hash differs from frozen source"
            )
        backend = str(parsed.metadata.get("parser_backend") or parsed.parser_backend or "").strip()
        parse_source = str(parsed.metadata.get("parse_source") or "").strip()
        if backend != "marker" or parse_source != "marker":
            raise ProductionParsingEvaluationError(
                "production_marker_not_used",
                "production evaluation requires admitted Marker output",
                details={"parser_backend": backend or None, "parse_source": parse_source or None},
            )
        if parsed.status not in {"parsed", "degraded"}:
            raise ProductionParsingEvaluationError("document_not_parse_terminal", "document parse did not succeed")
        if not result.backend_raw_refs:
            raise ProductionParsingEvaluationError("backend_raw_refs_missing", "backend raw refs are missing")
        _validate_execution_evidence(
            run_id, document.source_sha256, result.execution_evidence,
            result.durable_phase_records,
            result.durable_harness_events,
        )
        _validate_chunk_artifact_snapshot(
            reader=self._artifact_reader, artifact=result.chunk_artifact,
            payload=result.chunk_payload, actor_scope=parsed.actor_scope,
        )
        _validate_chunks(
            document.source_sha256, parsed.document_id,
            result.chunk_manifest, result.chunk_payload, document=parsed,
        )
        required = {
            "composition", "runtime_config", "parser", "parser_image_digest",
            "parser_weights_digest", "parser_config_sha256", "document_schema",
            "normalizer", "exporter", "chunker", "code_revision",
            "source_content_sha256", "source_manifest_sha256",
        }
        missing = sorted(key for key in required if not str(result.runtime_fingerprints.get(key) or "").strip())
        if missing:
            raise ProductionParsingEvaluationError(
                "runtime_fingerprints_missing", "runtime fingerprints are incomplete", details={"missing": missing}
            )


def _validate_execution_evidence(
    run_id: str,
    source_sha256: str,
    evidence: DurableExecutionEvidence,
    phase_payloads: Sequence[Mapping[str, Any]],
    durable_harness_events: Sequence[Mapping[str, Any]],
) -> None:
    receipt = evidence.receipt
    request = evidence.request
    if receipt.status.value != "succeeded" or receipt.provider_id != "docker":
        raise ProductionParsingEvaluationError("execution_receipt_not_succeeded", "parser receipt is not successful")
    if receipt.graph_identity.run_id != run_id:
        raise ProductionParsingEvaluationError("execution_run_id_mismatch", "receipt belongs to another Harness run")
    expected = {
        "execution_id": receipt.execution_id,
        "tool_id": receipt.tool_id,
        "graph_identity": receipt.graph_identity.to_dict(),
        "operation_id": receipt.operation_id,
        "attempt_id": receipt.attempt_id,
    }
    mismatches = [key for key, value in expected.items() if request.get(key) != value]
    profile = request.get("profile")
    if not isinstance(profile, Mapping) or profile.get("provider_id") != receipt.provider_id:
        mismatches.append("provider_id")
    argv = request.get("argv")
    read_roots = request.get("read_roots")
    write_roots = request.get("write_roots")
    if (
        not isinstance(argv, list)
        or not argv
        or argv[0] != "/usr/local/bin/marker_single"
    ):
        mismatches.append("argv")
    if not isinstance(read_roots, list) or not read_roots:
        mismatches.append("read_roots")
    if not isinstance(write_roots, list) or len(write_roots) != 1:
        mismatches.append("write_roots")
    if isinstance(argv, list) and len(argv) >= 2:
        source_path = Path(str(argv[1]))
        if (
            not source_path.is_file()
            or _sha256_file(source_path) != source_sha256
            or not isinstance(read_roots, list)
            or not any(_path_within(source_path, Path(str(root))) for root in read_roots)
        ):
            mismatches.append("source")
        if "--output_dir" not in argv:
            mismatches.append("output_argv")
        else:
            output_index = argv.index("--output_dir") + 1
            if output_index >= len(argv) or not isinstance(write_roots, list) or not any(
                _path_within(Path(str(argv[output_index])), Path(str(root)))
                for root in write_roots
            ):
                mismatches.append("output_root")
    if receipt.graph_identity.attempt <= 0:
        mismatches.append("attempt")
    if mismatches:
        raise ProductionParsingEvaluationError(
            "execution_request_receipt_mismatch",
            "durable execution request does not match its receipt",
            details={"mismatches": sorted(set(mismatches))},
        )
    validate_durable_parsing_harness_evidence(
        receipt, phase_payloads, durable_harness_events
    )


def validate_durable_parsing_harness_evidence(
    receipt: ExecutionReceipt,
    phase_payloads: Sequence[Mapping[str, Any]],
    event_payloads: Sequence[Mapping[str, Any]],
) -> None:
    """Join immutable phase/stage facts to the physical activity's actual identity.

    Phase records legitimately carry stage identity only (PLAN uses attempt 0).
    Execution identity comes from the durable activity result context, never by
    filling an absent phase field from a receipt or a worker return value.
    """
    def reject(message: str) -> None:
        raise ProductionParsingEvaluationError("harness_durable_evidence_invalid", message)

    identity = receipt.graph_identity
    stage = {key: value for key, value in identity.to_dict().items()
             if key not in {"activity_id", "attempt"}}
    events = tuple(StoredEvent.from_dict(item) for item in event_payloads)
    if not events:
        reject("durable canonical Harness envelopes are required")
    sequences = tuple(item.stream_sequence for item in events)
    if sequences != tuple(sorted(set(sequences))):
        reject("durable envelopes must retain unique increasing stream sequences")
    phases = []
    activity_events: dict[str, StoredEvent] = {}
    version = None
    for event in events:
        event.verify_integrity()
        expected_schema = (
            "newsroom.harness-graph-control-commit/v1"
            if event.event_type == "harness_graph_activity_result_committed"
            else "newsroom.harness-graph-event/v1"
        )
        if (event.stream_id != f"run:{identity.run_id}"
                or event.data_schema != expected_schema
                or event.source != "io.newsroom.harness.control-plane"):
            reject("durable Harness envelope stream/schema/source mismatch")
        context = GraphEventContext.from_dict(
            thaw_canonical_json(event.extensions.get("graph_context", {}))
        )
        if context.identity != identity.run_identity:
            reject("durable Harness Graph identity differs from receipt")
        if (event.business_context.run_id != identity.run_id
                or event.business_context.stage_id != identity.node_id
                or event.business_context.node_instance_id != identity.node_instance_id):
            reject("durable envelope business node differs from receipt")
        if version is None:
            version = context.execution_version
        if context.execution_version != version:
            reject("durable Harness compilation version drift")
        if context.stage_identity is not None and context.stage_identity.to_dict() != stage:
            reject("durable Harness stage identity differs from receipt")
        if context.execution_identity is not None and context.execution_identity != identity:
            reject("durable Harness activity identity differs from receipt")
        payload = thaw_canonical_json(event.payload or {})
        if event.event_type == "graph_phase_transition_recorded":
            record = GraphPhaseTransitionRecord.from_dict(payload.get("graph_phase_transition", {}))
            record.verify_integrity()
            record.assert_envelope_sequence(event.stream_sequence)
            if record.context != context or record.context.stage_identity is None:
                reject("phase record context differs from its durable envelope")
            expected_attempt = 0 if record.phase.value == "plan" else identity.attempt
            if record.attempt != expected_attempt:
                reject("phase attempt differs from PLAN0/physical receipt attempt")
            phases.append(record)
        elif event.event_type in {
            "graph_worker_called", "graph_worker_result_recorded",
            "harness_graph_activity_result_committed",
        }:
            if event.event_type in activity_events:
                reject("duplicate durable activity evidence")
            if event.event_type == "graph_worker_called":
                if (context.stage_identity is None
                        or payload.get("activity_id") != identity.activity_id
                        or payload.get("activity_attempt") != identity.attempt
                        or payload.get("node_id") != identity.node_id
                        or payload.get("node_instance_id") != identity.node_instance_id):
                    reject("durable worker call differs from receipt activity")
            elif context.execution_identity != identity:
                reject("durable activity result lacks the receipt execution identity")
            activity_events[event.event_type] = event
        else:
            reject("unexpected event in production parsing evidence")
    actual = tuple((record.phase.value, record.boundary.value) for record in phases)
    if actual != _EXPECTED_PHASES:
        reject("durable PLAN/EXECUTE/VERIFY boundaries are incomplete or out of order")
    if [record.to_dict() for record in phases] != [dict(item) for item in phase_payloads]:
        reject("exported phase records differ from the canonical durable envelopes")
    required = ("graph_worker_called", "graph_worker_result_recorded",
                "harness_graph_activity_result_committed")
    if set(activity_events) != set(required):
        reject("durable physical activity call/result/commit evidence is incomplete")
    call, result, commit = (activity_events[key].stream_sequence for key in required)
    # Harness dispatches the physical activity before persisting EXECUTE phase
    # boundaries. Do not invent an EXECUTE-entry-before-worker ordering.
    if not (phases[1].event_sequence < call < result < commit < phases[4].event_sequence):
        reject("durable activity lifecycle is outside its PLAN-to-VERIFY stage")



def _validate_chunk_artifact_snapshot(
    *, reader: ResearchArtifactReadPort, artifact: Mapping[str, Any],
    payload: Mapping[str, Any], actor_scope: Mapping[str, str],
) -> None:
    """Re-read the immutable artifact; its owner verifies stored-byte integrity.

    Catalog read operationalizes portable paths. Therefore never hash the read
    response as if it were the original stored envelope. Instead require the
    trusted reader to verify ref/digest/commit/scope again and compare the full
    returned operational payload to the snapshot consumed by the evaluator.
    """
    ref = str(artifact.get("artifactRef") or "")
    match = re.fullmatch(r"artifact://research/research-chunk-manifest/([0-9a-f]{64})", ref)
    if match is None or artifact.get("contentChecksum") != f"sha256:{match.group(1)}":
        raise ProductionParsingEvaluationError(
            "chunk_artifact_identity_invalid", "persisted chunks require a checksum-bound immutable artifact ref"
        )
    try:
        verified = reader.read(ref, actor_scope=actor_scope, include_payload=True, max_chars=1_000_000)
    except Exception as error:
        raise ProductionParsingEvaluationError(
            "chunk_artifact_integrity_failed", "persisted chunk artifact integrity could not be verified",
            details={"artifact_ref": ref, "cause_type": type(error).__name__},
        ) from error
    if (verified.get("artifactRef") != ref
            or verified.get("artifactType") != "research-chunk-manifest"
            or verified.get("contentChecksum") != artifact.get("contentChecksum")
            or verified.get("metadata") != artifact.get("metadata")):
        raise ProductionParsingEvaluationError(
            "chunk_artifact_identity_mismatch", "verified chunk artifact identity or metadata differs"
        )
    stored_payload = verified.get("payload")
    if (not isinstance(stored_payload, Mapping)
            or dict(stored_payload) != dict(payload)
            or artifact.get("payload") != stored_payload):
        raise ProductionParsingEvaluationError(
            "chunk_payload_integrity_mismatch", "chunk payload differs from the checksum-verified persisted artifact"
        )


def _validate_chunks(
    source_hash: str,
    document_id: str,
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    *, document: ResearchDocument | None = None,
) -> None:
    manifest_entries = manifest.get("chunks")
    stored_chunks = payload.get("chunks")
    if not isinstance(manifest_entries, list) or not manifest_entries:
        raise ProductionParsingEvaluationError("chunk_manifest_missing", "chunk manifest has no entries")
    if not isinstance(stored_chunks, list) or not stored_chunks:
        raise ProductionParsingEvaluationError("chunk_payload_missing", "persisted chunk payload has no chunks")
    if payload.get("source_hash") != source_hash or manifest.get("source_hash") != source_hash:
        raise ProductionParsingEvaluationError("chunk_source_hash_mismatch", "chunk source hash is inconsistent")
    if payload.get("document_id") != document_id or manifest.get("document_id") != document_id:
        raise ProductionParsingEvaluationError("chunk_document_id_mismatch", "chunk document identity is inconsistent")
    manifest_ids = [
        str(item.get("chunk_id"))
        for item in manifest_entries if isinstance(item, Mapping)
    ]
    stored_ids = [
        str(item.get("chunk_id"))
        for item in stored_chunks if isinstance(item, Mapping)
    ]
    if len(manifest_ids) != len(manifest_entries) or len(stored_ids) != len(stored_chunks):
        raise ProductionParsingEvaluationError("chunk_entry_invalid", "chunk entries must be objects")
    if len(set(manifest_ids)) != len(manifest_ids) or len(set(stored_ids)) != len(stored_ids):
        raise ProductionParsingEvaluationError("duplicate_chunk_id", "chunk ids must be unique")
    entries = {str(item["chunk_id"]): item for item in manifest_entries}
    chunks = {str(item["chunk_id"]): item for item in stored_chunks}
    if not entries or set(entries) != set(chunks):
        raise ProductionParsingEvaluationError(
            "chunk_store_coverage_mismatch", "manifest and persisted chunk ids differ"
        )
    for chunk_id, chunk in chunks.items():
        content = chunk.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProductionParsingEvaluationError(
                "chunk_content_missing", "persisted chunk content is missing", details={"chunk_id": chunk_id}
            )
        metadata = chunk.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        metadata_hash = str(metadata.get("content_hash") or "")
        manifest_hash = str(entries[chunk_id].get("content_hash") or "")
        chunk_type = str(chunk.get("chunk_type") or "paragraph")
        # Existing content_hash is a semantic identity, not a digest of the
        # enriched consumer payload (which may include Context/Normalized text).
        if chunk_type == "formula":
            equation_id = str(metadata.get("equation_id") or "")
            equations = [eq for eq in document.equations if eq.equation_id == equation_id] if document else []
            if len(equations) != 1 or chunk.get("formula_latex") != equations[0].latex:
                raise ProductionParsingEvaluationError(
                    "chunk_element_identity_mismatch", "formula chunk differs from its ResearchDocument equation",
                    details={"chunk_id": chunk_id},
                )
            semantic_hash = content_fingerprint(f"{equation_id}\n{equations[0].latex}")
        elif chunk_type == "figure":
            figures = [fig for fig in document.figures if fig.figure_id == chunk.get("figure_id")] if document else []
            hashes = {content_fingerprint(f"{fig.figure_id}\n{fig.caption}") for fig in figures}
            if metadata_hash not in hashes:
                raise ProductionParsingEvaluationError(
                    "chunk_element_identity_mismatch", "figure semantic identity differs from ResearchDocument",
                    details={"chunk_id": chunk_id},
                )
            semantic_hash = metadata_hash
        elif chunk_type == "table":
            # Reuse the chunker's native identity-text helper only. Never invoke
            # chunk() or regenerate the persisted consumer payload in evaluation.
            from backend.research.document.chunker import _table_semantic_text, _TABLE_PARENT_ROW_LIMIT
            tables = [table for table in document.tables
                      if table.table_id == metadata.get("table_id")] if document else []
            hashes = set()
            for table in tables:
                rows = table.rows[:_TABLE_PARENT_ROW_LIMIT]
                if metadata.get("is_table_row_group"):
                    start = metadata.get("row_start")
                    end = metadata.get("row_end")
                    if type(start) is not int or type(end) is not int or not 0 <= start <= end < len(table.rows):
                        continue
                    rows = table.rows[start:end + 1]
                hashes.add(content_fingerprint(_table_semantic_text(table, rows)))
            if metadata_hash not in hashes:
                raise ProductionParsingEvaluationError(
                    "chunk_element_identity_mismatch", "table semantic identity differs from ResearchDocument",
                    details={"chunk_id": chunk_id},
                )
            semantic_hash = metadata_hash
        elif document is not None and metadata.get("section_id"):
            # Structured paragraph payloads may prepend overlap or append figure
            # context. Their existing identity hashes the ORIGINAL source para.
            from backend.research.document.chunker import _split_paragraphs
            sections = [section for section in document.sections
                        if section.section_id == metadata.get("section_id")]
            texts = []
            for section in sections:
                if metadata.get("is_parent") is False:
                    index = metadata.get("para_index")
                    paragraphs = _split_paragraphs(section.text)
                    if type(index) is not int or not 0 <= index < len(paragraphs):
                        continue
                    texts.append(paragraphs[index])
                else:
                    texts.append(section.text)
            if metadata_hash not in {content_fingerprint(text) for text in texts}:
                raise ProductionParsingEvaluationError(
                    "chunk_content_hash_mismatch", "paragraph semantic identity differs from ResearchDocument source text",
                    details={"chunk_id": chunk_id},
                )
            semantic_hash = metadata_hash
        else:
            semantic_hash = content_fingerprint(content)
        if not metadata_hash or metadata_hash != semantic_hash or manifest_hash != metadata_hash:
            raise ProductionParsingEvaluationError(
                "chunk_content_hash_mismatch", "persisted chunk semantic hash differs from its source or manifest",
                details={"chunk_id": chunk_id},
            )
        entry = entries[chunk_id]
        if (
            str(entry.get("document_id") or "") != document_id
            or str(entry.get("source_hash") or "") != source_hash
        ):
            raise ProductionParsingEvaluationError(
                "chunk_lineage_mismatch", "chunk belongs to another document or source",
                details={"chunk_id": chunk_id},
            )
        parent = chunk.get("parent_chunk_id")
        if parent is not None:
            parent_id = str(parent)
            parent_chunk = chunks.get(parent_id)
            if parent_chunk is None:
                raise ProductionParsingEvaluationError(
                    "chunk_parent_missing", "persisted chunk parent is missing", details={"chunk_id": chunk_id}
                )
            parent_entry = entries[parent_id]
            if (
                str(parent_entry.get("document_id") or "") != document_id
                or str(parent_entry.get("source_hash") or "") != source_hash
            ):
                raise ProductionParsingEvaluationError(
                    "chunk_parent_lineage_mismatch", "chunk parent belongs to another source",
                    details={"chunk_id": chunk_id, "parent_chunk_id": parent_id},
                )
        if str(entry.get("parent_chunk_id") or "") != str(parent or ""):
            raise ProductionParsingEvaluationError(
                "chunk_parent_manifest_mismatch", "chunk parent differs from manifest",
                details={"chunk_id": chunk_id},
            )
    manifest_pipeline = str(manifest.get("pipeline_fingerprint") or "")
    payload_pipeline = str(payload.get("pipeline_fingerprint") or "")
    if manifest_pipeline != payload_pipeline:
        raise ProductionParsingEvaluationError(
            "chunk_pipeline_identity_mismatch", "chunk manifest and persisted payload use different pipeline identities"
        )


def validate_unique_document_sources(
    documents: Sequence[ProductionParsingDocumentInput],
) -> tuple[ProductionParsingDocumentInput, ...]:
    frozen = tuple(documents)
    if not frozen:
        raise ValueError("production input manifest must contain at least one document")
    ids = [item.document_id.casefold() for item in frozen]
    paths = [str(item.source_path.resolve()) for item in frozen]
    hashes = [item.source_sha256 for item in frozen]
    if len(ids) != len(set(ids)):
        raise ValueError("document_id values must be unique")
    if len(paths) != len(set(paths)):
        raise ValueError("each production document must have a unique source_path")
    if len(hashes) != len(set(hashes)):
        raise ValueError("each production document must have a unique source_sha256")
    return frozen



def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True

def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "DurableExecutionEvidence",
    "ProductionParsingDocumentInput",
    "ProductionParsingEvaluationError",
    "ProductionParsingEvaluationResult",
    "ProductionParsingEvaluationService",
    "ProductionParsingRuntimePort",
    "ProductionParsingRuntimeRequest",
    "ProductionParsingRuntimeResult",
    "validate_unique_document_sources",
]
