"""Harness-owned deterministic production parsing graph."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.research.application.parse_paper import (
    ParsePaperRequest,
    ParsePaperResult,
    ParsePaperUseCase,
)
from backend.research.ports.artifact_store import ResearchArtifactReadPort
from backend.research.domain.run_disposition import research_subject_scope_ref
from framework.harness import (
    DeterministicGate, DeterministicGateRegistry, GateContext, GateReference,
    GateRegistration, HarnessBudget, HarnessGateResult, HarnessRunSpec,
)
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.graph.bindings import (
    HarnessActivityContractBinding,
    HarnessActivityCapabilities,
    HarnessLeafActivityBinding,
    HarnessRuntimeBindingAuthority,
    HarnessWorkerBinding,
)
from framework.harness.graph.canonical import canonical_checksum
from framework.harness.graph.compiler import HarnessGraphCompiler
from framework.harness.graph.definition import HarnessGraphDefinition, HarnessGraphLeafBinding
from framework.harness.graph.dsl import HarnessGraphSpec, StepRef
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.graph.activity import HarnessStepSpec, HarnessWorkerType
from framework.harness.side_effects.models import (
    HarnessSideEffectDisposition,
    HarnessSideEffectOutcome,
    HarnessTerminalSideEffectPolicy,
    side_effect_record_identity_key,
)
from framework.events.canonical import checksum_for
from framework.events.ports import EventReaderPort
from framework.events.graph_phase import GraphPhaseTransitionRecord
from framework.events.runtime.models import StreamReadRequest
from framework.execution_environment.models import ExecutionReceipt
from backend.research.application.parsing_production_evaluation import (
    validate_durable_parsing_harness_evidence,
)
from framework.harness.side_effects.ports import HarnessSideEffectStorePort
from framework.harness.side_effects.registry import (
    HarnessSideEffectCapabilities,
    HarnessSideEffectHandlerBinding,
    HarnessSideEffectRegistry,
)
from framework.harness.workers.result import HarnessWorkerResult
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.shared.time import utc_now

_GRAPH_ID = "research-production-parsing-evaluation"
_GRAPH_VERSION = "1"
_STEP_ID = "parse_document"
_WORKER_REF = HarnessContractReference(HarnessContractKind.WORKER, _STEP_ID, "1")
_ACTIVITY_REF = HarnessContractReference(
    HarnessContractKind.ACTIVITY, "research.production-parsing.parse", "1"
)
_TERMINAL_HANDLER_REF = "research.production-parsing.no-side-effect@1"
_PARSE_GATE_REF = GateReference("ResearchProductionParseGate", "1")


@runtime_checkable
class ProductionParsingHarnessRunner(Protocol):
    def run(
        self,
        *,
        run_spec: HarnessRunSpec,
        authority: HarnessRuntimeBindingAuthority,
        gate_registry: DeterministicGateRegistry,
        side_effect_registry: HarnessSideEffectRegistry,
        compiled_graph: Any,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class ProductionParsingGraphResult:
    parse_result: ParsePaperResult
    harness_result: Any
    durable_phase_records: tuple[Mapping[str, Any], ...]
    execution_evidence: Mapping[str, Any]
    chunk_payload: Mapping[str, Any]
    durable_harness_events: tuple[Mapping[str, Any], ...] = ()
    chunk_artifact: Mapping[str, Any] = field(default_factory=dict)


class _NoSideEffectHandler:
    """Commit only a durable no-op terminal fact; publish no business effect."""

    def __init__(self, store: HarnessSideEffectStorePort) -> None:
        self._store = store

    def commit(self, intent: Any, authorization: Any) -> HarnessSideEffectOutcome:
        existing = self._store.get_outcome(
            effect_id=intent.effect_id,
            identity_scope_ref=intent.identity_scope_ref,
            subject_scope_ref=intent.subject_scope_ref,
            idempotency_key=intent.idempotency_key,
        )
        if existing is not None:
            return existing
        outcome = HarnessSideEffectOutcome(
            outcome_id="harness-outcome:" + side_effect_record_identity_key(intent),
            effect_id=intent.effect_id,
            decision_ref=authorization.checksum,
            run_id=intent.run_id,
            graph_id=intent.graph_id,
            graph_version=intent.graph_version,
            graph_ref=intent.graph_ref,
            graph_checksum=intent.graph_checksum,
            origin=intent.origin,
            kind=intent.kind,
            handler=authorization.handler,
            idempotency_key=intent.idempotency_key,
            identity_scope_ref=intent.identity_scope_ref,
            subject_scope_ref=intent.subject_scope_ref,
            atomic_group=intent.atomic_group,
            disposition=HarnessSideEffectDisposition.ACCEPTED,
            node_id=intent.node_id,
            node_instance_id=intent.node_instance_id,
            activity_id=intent.activity_id,
            terminal_action=intent.terminal_action,
            attempt=intent.attempt,
            candidate_refs=intent.candidate_refs,
            public_refs=(f"effect://{intent.effect_id}",),
            result_ref=checksum_for({
                "effect_id": intent.effect_id,
                "disposition": "accepted_no_effect",
            }),
            metadata={"business_effect": "none"},
        )
        return self._store.put_outcome(outcome)


class _ProductionParseGate(DeterministicGate):
    gate_name = _PARSE_GATE_REF.gate_id
    gate_version = _PARSE_GATE_REF.version

    def evaluate(self, context: GateContext) -> HarnessGateResult:
        output = (
            context.worker_result.output.get("parse_result")
            if context.worker_result is not None
            else None
        )
        request = context.run_spec.inputs.get("request")
        expected_source = (
            request.get("metadata", {}).get("expected_source_sha256")
            if isinstance(request, Mapping)
            and isinstance(request.get("metadata"), Mapping)
            else None
        )
        violations: dict[str, Any] = {}
        if not isinstance(output, Mapping):
            violations["parse_result"] = "missing"
        else:
            if output.get("status") in {None, "failed", "metadata_only", "quarantined"}:
                violations["status"] = output.get("status")
            if output.get("document_status") not in {"parsed", "degraded"}:
                violations["document_status"] = output.get("document_status")
            if not str(output.get("chunk_manifest_ref") or "").strip():
                violations["chunk_manifest_ref"] = "missing"
            if not str(output.get("execution_receipt_ref") or "").startswith(
                "execution-receipt://"
            ):
                violations["execution_receipt_ref"] = "missing"
            if not str(output.get("execution_evidence_artifact_ref") or "").startswith(
                "artifact://"
            ):
                violations["execution_evidence_artifact_ref"] = "missing"
            if expected_source and output.get("document_source_hash") != expected_source:
                violations["source_hash"] = "mismatch"
        return HarnessGateResult(
            gate_name=self.gate_name,
            passed=not violations,
            reason=None if not violations else "production parse verification failed",
            details={
                "reason_code": (
                    "production_parse_verified" if not violations
                    else "production_parse_verification_failed"
                ),
                "violations": violations,
            },
        )


class _ParseActivityMarker:
    activity_contract_id = _ACTIVITY_REF.contract_id
    activity_contract_version = _ACTIVITY_REF.version
    capabilities = HarnessActivityCapabilities(stable_idempotency=True)

    def dispatch(self, _request: Mapping[str, Any]) -> None:
        raise HarnessValidationError(
            "production parsing must execute through the physical Graph dispatcher",
            code="production_parsing_direct_dispatch_forbidden",
        )


class _ParseWorker:
    worker_id = _WORKER_REF.contract_id
    worker_version = _WORKER_REF.version
    worker_type = HarnessWorkerType.FUNCTION

    def __init__(self, *, use_case: ParsePaperUseCase, graph: Any) -> None:
        self._use_case = use_case
        self._graph = graph
        self.result: ParsePaperResult | None = None

    def execute(self, task: dict[str, Any]) -> HarnessWorkerResult:
        context_payload = task.get(HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY)
        if not isinstance(context_payload, Mapping):
            raise HarnessValidationError(
                "production parsing worker lacks Harness activity context",
                code="production_parsing_activity_context_missing",
            )
        context = HarnessGraphActivityTaskContext.from_dict(context_payload)
        activity = context.activity
        if activity.graph_ref.checksum != self._graph.checksum:
            raise HarnessValidationError(
                "production parsing activity belongs to another Graph definition",
                code="production_parsing_graph_identity_mismatch",
            )
        task_inputs = task.get("inputs")
        request_payload = (
            task_inputs.get("request") if isinstance(task_inputs, Mapping) else None
        )
        if not isinstance(request_payload, Mapping):
            raise HarnessValidationError(
                f"production parsing request is missing; task_keys={sorted(task)}",
                code="production_parsing_request_missing",
                details={"task_keys": sorted(task)},
            )
        execution_identity = GraphExecutionIdentity(
            run_id=activity.run_id,
            graph_id=self._graph.graph_id,
            graph_version=self._graph.graph_ref.version,
            graph_ref=f"{self._graph.graph_id}@{self._graph.graph_ref.version}",
            graph_checksum=self._graph.checksum,
            node_id=activity.node_id,
            node_instance_id=activity.node_instance_id,
            activity_id=activity.activity_id,
            attempt=activity.attempt,
        )
        result = self._use_case.parse(
            ParsePaperRequest.model_validate(request_payload),
            execution_identity=execution_identity,
        )
        self.result = result
        return HarnessWorkerResult(
            "succeeded",
            output={
                "parse_result": {
                    "run_id": result.run_id,
                    "paper_id": result.paper_id,
                    "status": result.status,
                    "document_source_hash": (
                        result.document.source_hash if result.document is not None else None
                    ),
                    "document_status": (
                        result.document.status if result.document is not None else None
                    ),
                    "execution_receipt_ref": (
                        result.document.metadata.get("execution_receipt_ref")
                        if result.document is not None else None
                    ),
                    "execution_evidence_artifact_ref": (
                        result.document.metadata.get("execution_evidence_artifact_ref")
                        if result.document is not None else None
                    ),
                    "chunk_manifest_ref": result.chunk_manifest_ref,
                }
            },
        )


class ProductionParsingGraphApplicationService:
    """Run one source through a real durable Harness parse-only graph."""

    def __init__(
        self,
        *,
        parse_use_case: ParsePaperUseCase,
        harness_runner: ProductionParsingHarnessRunner,
        side_effect_store: HarnessSideEffectStorePort,
        chunk_artifact_reader: ResearchArtifactReadPort,
        durable_event_reader: EventReaderPort | None = None,
        event_tenant_id: str | None = None,
    ) -> None:
        if not isinstance(parse_use_case, ParsePaperUseCase):
            raise TypeError("parse_use_case must be ParsePaperUseCase")
        if not isinstance(harness_runner, ProductionParsingHarnessRunner):
            raise TypeError("harness_runner must implement ProductionParsingHarnessRunner")
        if not isinstance(side_effect_store, HarnessSideEffectStorePort):
            raise TypeError("side_effect_store must implement HarnessSideEffectStorePort")
        if not isinstance(chunk_artifact_reader, ResearchArtifactReadPort):
            raise TypeError("chunk_artifact_reader must implement ResearchArtifactReadPort")
        self._parse_use_case = parse_use_case
        self._harness_runner = harness_runner
        self._side_effect_store = side_effect_store
        self._chunk_artifact_reader = chunk_artifact_reader
        self._durable_event_reader = durable_event_reader
        self._event_tenant_id = event_tenant_id
        self._definition = _definition()

    @property
    def graph_definition(self) -> HarnessGraphDefinition:
        return self._definition

    def run(self, request: ParsePaperRequest) -> ProductionParsingGraphResult:
        if not isinstance(request, ParsePaperRequest):
            raise TypeError("request must be ParsePaperRequest")
        if not request.run_id:
            raise ValueError("production parsing request requires run_id")
        compiled = HarnessGraphCompiler().compile(self._definition).graph
        worker = _ParseWorker(use_case=self._parse_use_case, graph=compiled)
        side_effect_registry = HarnessSideEffectRegistry(
            (
                HarnessSideEffectHandlerBinding(
                    _TERMINAL_HANDLER_REF,
                    "none",
                    _NoSideEffectHandler(self._side_effect_store),
                    capabilities=HarnessSideEffectCapabilities(stable_idempotency=True),
                ),
            )
        )
        gate_registry = DeterministicGateRegistry((
            GateRegistration(_PARSE_GATE_REF, _ProductionParseGate()),
        ))
        authority = HarnessRuntimeBindingAuthority(
            workers=(HarnessWorkerBinding(_WORKER_REF, HarnessWorkerType.FUNCTION, worker),),
            activities=(HarnessActivityContractBinding(_ACTIVITY_REF, _ParseActivityMarker()),),
            leaf_activities=(
                HarnessLeafActivityBinding(
                    leaf_activity_kind="function",
                    worker_ref=_WORKER_REF,
                    activity_ref=_ACTIVITY_REF,
                ),
            ),
            gate_registry=gate_registry,
            side_effect_registry=side_effect_registry,
        )
        run_spec = HarnessRunSpec(
            run_id=request.run_id,
            graph=self._definition,
            inputs={"request": request.model_dump(mode="json")},
            budget=HarnessBudget(
                max_turns=6,
                max_replans=0,
                max_retries_per_step=0,
                max_worker_calls=1,
            ),
            metadata={
                "research_runtime": "production_parsing_evaluation",
                "subject_scope_ref": research_subject_scope_ref(
                    str(request.metadata.get("paper_id") or request.source)
                ),
            },
            created_at=utc_now(),
        )
        execution = self._harness_runner.run(
            run_spec=run_spec,
            authority=authority,
            gate_registry=gate_registry,
            side_effect_registry=side_effect_registry,
            compiled_graph=compiled,
        )
        harness_result = execution.harness_result
        event_port = execution.event_port
        if not harness_result.succeeded:
            raise HarnessValidationError(
                "production parsing graph did not succeed",
                code="production_parsing_graph_failed",
                details={"status": harness_result.status.value, "worker_results": {key: value.to_dict() for key, value in harness_result.worker_results.items()}},
            )
        parse_result = worker.result
        if parse_result is None:
            raise HarnessValidationError(
                "production parsing result cannot be recovered without its typed output",
                code="production_parsing_typed_result_missing",
            )
        document = parse_result.document
        if document is None:
            raise HarnessValidationError(
                "production parsing graph returned no ResearchDocument",
                code="production_parsing_document_missing",
            )
        execution_ref = str(document.metadata.get("execution_receipt_ref") or "")
        prefix = "execution-receipt://"
        if not execution_ref.startswith(prefix):
            raise HarnessValidationError(
                "production parsing document lacks an execution receipt reference",
                code="production_parsing_execution_receipt_missing",
            )
        actor_scope = {
            key: str(value).strip()
            for key, value in {
                **dict(request.actor_scope),
                "tenant_id": request.tenant_id,
                "user_id": request.user_id,
                "memory_namespace": request.memory_namespace,
            }.items()
            if key in {"tenant_id", "user_id", "memory_namespace"}
            and value is not None
            and str(value).strip()
        }
        evidence_artifact_ref = str(
            document.metadata.get("execution_evidence_artifact_ref") or ""
        ).strip()
        if not evidence_artifact_ref:
            raise HarnessValidationError(
                "production parsing document lacks durable execution evidence",
                code="production_parsing_execution_evidence_missing",
            )
        evidence_envelope = self._chunk_artifact_reader.read(
            evidence_artifact_ref,
            actor_scope=actor_scope,
            include_payload=True,
            max_chars=200_000,
        )
        evidence_payload = evidence_envelope.get("payload")
        if not isinstance(evidence_payload, Mapping):
            raise HarnessValidationError(
                "production parser execution evidence is unavailable or truncated",
                code="production_parsing_execution_evidence_missing",
                details={"artifact_ref": evidence_artifact_ref},
            )
        receipt_payload = evidence_payload.get("receipt")
        if (
            not isinstance(receipt_payload, Mapping)
            or receipt_payload.get("execution_id") != execution_ref.removeprefix(prefix)
        ):
            raise HarnessValidationError(
                "production parser evidence does not match document receipt",
                code="production_parsing_execution_evidence_mismatch",
            )
        chunk_ref = str(parse_result.chunk_manifest_ref or "").strip()
        if not chunk_ref:
            raise HarnessValidationError(
                "production parsing graph returned no persisted chunk artifact",
                code="production_parsing_chunk_artifact_missing",
            )
        chunk_envelope = self._chunk_artifact_reader.read(
            chunk_ref,
            actor_scope=actor_scope,
            include_payload=True,
            max_chars=1_000_000,
        )
        chunk_payload = chunk_envelope.get("payload")
        if not isinstance(chunk_payload, Mapping):
            raise HarnessValidationError(
                "persisted production chunk payload is unavailable or truncated",
                code="production_parsing_chunk_payload_missing",
                details={"artifact_ref": chunk_ref},
            )
        durable_history = event_port.read_history(request.run_id)
        phase_records = tuple(
            GraphPhaseTransitionRecord.from_dict(
                event.payload.get("graph_phase_transition", event.payload)
            ).to_dict()
            for event in durable_history
            if event.event_type.value == "graph_phase_transition_recorded"
        )
        if not phase_records:
            raise HarnessValidationError(
                "production parsing durable Harness phase history is missing",
                code="production_parsing_phase_history_missing",
            )
        durable_envelopes: tuple[Mapping[str, Any], ...] = ()
        if self._durable_event_reader is not None:
            durable_envelopes = _read_parsing_harness_envelopes(
                self._durable_event_reader, run_id=request.run_id,
                tenant_id=self._event_tenant_id,
            )
            validate_durable_parsing_harness_evidence(
                ExecutionReceipt.from_dict(receipt_payload), phase_records, durable_envelopes
            )
        return ProductionParsingGraphResult(
            parse_result=parse_result,
            harness_result=harness_result,
            durable_phase_records=phase_records,
            execution_evidence=evidence_payload,
            chunk_payload=dict(chunk_payload),
            chunk_artifact=dict(chunk_envelope),
            durable_harness_events=durable_envelopes,
        )


_PARSING_EVIDENCE_EVENT_TYPES = frozenset({
    "graph_phase_transition_recorded", "graph_worker_called",
    "graph_worker_result_recorded", "harness_graph_activity_result_committed",
})


def _read_parsing_harness_envelopes(
    reader: EventReaderPort, *, run_id: str, tenant_id: str | None,
) -> tuple[Mapping[str, Any], ...]:
    """Read canonical immutable facts through the injected existing event port."""
    stream_id = f"run:{run_id}"
    high_watermark = reader.get_stream_high_watermark(stream_id, tenant_id=tenant_id) or 0
    cursor = None
    evidence: list[Mapping[str, Any]] = []
    previous_sequence = 0
    while high_watermark:
        page = reader.read_stream(StreamReadRequest(
            stream_id=stream_id, tenant_id=tenant_id, cursor=cursor,
            through_sequence=high_watermark, limit=500,
        ))
        for event in page.events:
            event.verify_integrity()
            if (event.stream_id != stream_id or event.tenant_id != tenant_id
                    or event.stream_sequence <= previous_sequence):
                raise HarnessValidationError(
                    "production parsing durable event stream is inconsistent",
                    code="production_parsing_phase_history_invalid",
                )
            previous_sequence = event.stream_sequence
            if event.event_type in _PARSING_EVIDENCE_EVENT_TYPES:
                evidence.append(event.to_dict())
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return tuple(evidence)


def _definition() -> HarnessGraphDefinition:
    step = HarnessStepSpec(
        _STEP_ID,
        HarnessWorkerType.FUNCTION,
        input_keys=("request",),
        output_key="parse_result",
        quality_gate=str(_PARSE_GATE_REF),
    )
    graph = HarnessGraphSpec(
        _GRAPH_ID,
        StepRef(_STEP_ID),
        input_keys=("request",),
        terminal_output_keys=("parse_result",),
    )
    return HarnessGraphDefinition(
        graph_id=_GRAPH_ID,
        graph_version=_GRAPH_VERSION,
        root=graph,
        activities=(step,),
        leaf_activity_bindings=(
            HarnessGraphLeafBinding(
                _STEP_ID,
                "function",
                _WORKER_REF,
                _ACTIVITY_REF,
            ),
        ),
        task_plan_stage_bindings=(),
        committed_output_bindings=(),
        repair_bindings=(),
        terminal_side_effect_policy=HarnessTerminalSideEffectPolicy(
            policy_id="research.production-parsing.no-side-effect",
            version="1",
            handler=_TERMINAL_HANDLER_REF,
            kind="none",
            requires_approval=False,
            retry_limit=1,
            not_required_evidence_ref=canonical_checksum(
                {"reason": "production-parsing-evaluation-has-no-terminal-side-effect"}
            ),
        ),
    )


__all__ = [
    "ProductionParsingHarnessRunner",
    "ProductionParsingGraphApplicationService",
    "ProductionParsingGraphResult",
]
