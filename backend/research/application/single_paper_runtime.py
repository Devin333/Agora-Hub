from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from framework.agent.artifacts.paths import validate_artifact_path_segment
from framework.events.canonical import checksum_for
from framework.events.application import (
    GraphEventProjectionApplicationPort,
    GraphEventProjectionApplicationRequest,
    GraphEventProjectionApplicationStatus,
)
from framework.shared.graph_identity import GraphExecutionIdentity, GraphRunIdentity
from framework.harness import (
    ArtifactPort,
    ArtifactWriteRequest,
    ContextAssembler,
    ContextBudget,
    ContextEnvelope,
    ContextGraphIdentity,
    ContextSnapshot,
    HarnessBudget,
    HarnessControlPlane,
    HarnessEvent,
    HarnessEventType,
    HarnessGraphPreflight,
    HarnessGraphPreflightPolicy,
    HarnessGraphResultCommitterPort,
    HarnessTransitionPort,
    HarnessRunSpec,
    HarnessRunStatus,
    HarnessTrace,
    HarnessTranscript,
    HarnessWorkerResult,
    HarnessWorkerStatus,
    HarnessValidationError,
    HarnessSideEffectDecision,
    HarnessSideEffectDisposition,
    HarnessSideEffectHandlerBinding,
    HarnessSideEffectIntent,
    HarnessSideEffectOrigin,
    HarnessSideEffectOutcome,
    HarnessSideEffectRegistry,
    HarnessSideEffectStorePort,
    InMemoryHarnessSideEffectStore,
    RunBoundArtifactPort,
    RAGBudget,
    RAGContextPack,
    SkillExperience,
    SkillExperienceOutcome,
    transcript_entry_from_event,
)
from framework.harness.graph import GraphExecutionVersionManifest, HarnessGraphCompiler
from framework.harness.control_plane.node_output import (
    HarnessNodeOutputResourcePort,
)
from framework.harness.waits.ports import HarnessWaitRuntimeRegistrationPort
from framework.harness.control_plane.activity_execution import (
    HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY,
    HarnessGraphActivityTaskContext,
)
from framework.harness.runtime.activity_executor import HarnessGraphPhysicalActivityExecutor
from framework.harness.runtime.graph_dispatcher import (
    HarnessGraphPhysicalActivityDispatcher,
)
from framework.shared.attempts import AttemptSupervisor
from framework.harness.artifacts import (
    GraphTerminalManifestCommitRequest,
    GraphTerminalManifestContext,
    GraphTerminalManifestRecorderPort,
    GraphTerminalStatus,
)
from framework.shared.json import to_jsonable
from framework.shared.time import utc_now

from backend.research.application.ask_paper import AskPaperUseCase, ResearchActorScope
from backend.research.application.rag_memory_admission import (
    ResearchRAGMemoryAdmissionPort,
)
from backend.research.benchmark.models import ResearchScore
from backend.research.domain import (
    EvidenceRef,
    GateResult,
    PaperSourceRecord,
    ReaderIssue,
    ResearchAnalysis,
    ResearchClaim,
    ResearchDocument,
    ResearchEvidencePack,
    ResearchPaper,
    ResearchQualityResult,
    ResearchReaderPayload,
    ThreeMinuteRead,
    research_event_tenant_id,
    research_identity_scope_ref,
    research_subject_scope_ref,
    stable_research_id,
)
from backend.research.paper_card import PaperCardBuilder, ResearchPaperCard
from backend.research.rag import ResearchRAGContext, ResearchRetrievalGoal
from backend.research.reader import ReaderPayloadBuilder
from backend.research.services import (
    CitationVerifier,
    ReaderIssueDetector,
    ResearchEvidenceBuilder,
    ResearchQualityGate,
    ResearchRAGPolicyBuilder,
)
from backend.research.taxonomy import TaxonomyAssignment, TaxonomyAssignmentBuilder, TaxonomyCandidate, TaxonomyRegistry
from backend.research.graphs import (
    build_dynamic_paper_analysis_graph_definition,
    build_paper_analysis_context_graph_identity,
    build_paper_analysis_gate_registry,
    build_paper_analysis_graph_definition,
    build_paper_analysis_runtime_binding_authority,
)
from backend.research.ports.artifact_publication import (
    RESEARCH_ARTIFACT_EFFECT_KIND,
    RESEARCH_ARTIFACT_HANDLER_REF,
    RESEARCH_ARTIFACT_SCHEMA_VERSION,
    ResearchGraphStorageIndexPublisherPort,
)


LOGGER = logging.getLogger(__name__)


class ResearchDynamicTaskPlanUnavailableError(RuntimeError):
    """Raised before activity when dynamic production dependencies are absent."""

    code = "research_dynamic_task_plan_unavailable"
    missing_capabilities = (
        "research.task_plan.builder",
        "research.task_plan.worker_bindings",
        "research.task_plan.store",
    )

    def __init__(self) -> None:
        super().__init__("Research dynamic TaskPlan runtime is unavailable")


@dataclass(frozen=True)
class AnalyzePaperRequest:
    run_id: str
    paper_id: str
    source_ref: str
    user_id: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    tenant_id: str | None = None
    memory_namespace: str | None = None


def build_research_harness_run_spec(
    request: AnalyzePaperRequest,
    *,
    created_at: datetime,
) -> HarnessRunSpec:
    """Build the canonical run specification used by execution and recovery."""

    actor_metadata = _request_actor_metadata(request)
    identity_scope_ref = research_identity_scope_ref(actor_metadata)
    return HarnessRunSpec(
        run_id=request.run_id,
        graph=(
            build_dynamic_paper_analysis_graph_definition()
            if _dynamic_task_plan_requested(request.options)
            else build_paper_analysis_graph_definition()
        ),
        inputs={
            "paper_id": request.paper_id,
            "source_ref": request.source_ref,
            **actor_metadata,
        },
        budget=_budget_from_options(request.options),
        metadata={
            "research_runtime": "single_paper",
            "paper_id": request.paper_id,
            "graph_terminal_tenant_id": research_event_tenant_id(actor_metadata),
            "tenant_scope_ref": identity_scope_ref,
            "identity_scope_ref": identity_scope_ref,
            "subject_scope_ref": research_subject_scope_ref(request.paper_id),
            **actor_metadata,
        },
        created_at=created_at,
    )


def _research_graph_identity_for_request(
    request: AnalyzePaperRequest,
) -> tuple[str, str]:
    """Resolve the immutable Research Graph identity for result projection."""

    definition = (
        build_dynamic_paper_analysis_graph_definition()
        if _dynamic_task_plan_requested(request.options)
        else build_paper_analysis_graph_definition()
    )
    return definition.graph_id, definition.graph_version


def _context_graph_identity_for_request(
    request: AnalyzePaperRequest,
    *,
    stage_id: str,
) -> ContextGraphIdentity:
    """Build the exact Graph identity required by the context owner."""

    return build_paper_analysis_context_graph_identity(
        run_id=request.run_id,
        stage_id=stage_id,
        dynamic_task_plan=_dynamic_task_plan_requested(request.options),
    )


def _context_graph_identity_for_activity(
    task: Mapping[str, Any],
    request: AnalyzePaperRequest,
    *,
    stage_id: str,
) -> ContextGraphIdentity:
    """Derive RAG context identity from the Harness-owned activity envelope."""

    raw_context = task.get(HARNESS_GRAPH_ACTIVITY_TASK_CONTEXT_KEY)
    if not isinstance(raw_context, Mapping):
        raise HarnessValidationError(
            "Graph-bound Research RAG requires Harness activity context",
            code="research_rag_activity_context_missing",
        )
    activity_context = HarnessGraphActivityTaskContext.from_dict(raw_context)
    activity = activity_context.activity
    expected = _context_graph_identity_for_request(request, stage_id=stage_id)
    if (
        activity.run_id != request.run_id
        or activity.graph_ref.graph_id != expected.graph_id
        or activity.graph_ref.identity_version != expected.graph_version
        or activity.graph_ref.checksum != expected.graph_checksum
        or activity.node_id != stage_id
    ):
        raise HarnessValidationError(
            "Research RAG activity identity conflicts with the admitted Graph",
            code="research_rag_activity_identity_mismatch",
        )
    return expected.with_physical_activity(
        node_id=activity.node_id,
        node_instance_id=activity.node_instance_id,
        activity_id=activity.activity_id,
        activity_attempt=activity.attempt,
    )


@dataclass(frozen=True)
class ResearchAnalysisResult:
    run_id: str
    graph_id: str
    graph_version: str
    status: str
    analysis: ResearchAnalysis | None
    quality: ResearchQualityResult
    paper_card: ResearchPaperCard | None
    reader_payload: ResearchReaderPayload | None
    rag_context: ResearchRAGContext | None
    reader_issue: ReaderIssue | None
    artifact_refs: dict[str, str]
    trace: HarnessTrace
    transcript: HarnessTranscript
    context_snapshot: ContextSnapshot | None
    context_envelope: ContextEnvelope | None
    compression_records: list[dict[str, Any]]
    skill_experience_refs: list[str]
    actor_scope: ResearchActorScope
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == HarnessRunStatus.SUCCEEDED.value and self.analysis is not None

    @property
    def reader_payload_ref(self) -> str | None:
        return self.artifact_refs.get("research-reader-payload")

    @property
    def trace_ref(self) -> str:
        return f"harness-trace://{self.run_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "graph_id": self.graph_id,
            "graph_version": self.graph_version,
            "status": self.status,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "quality": self.quality.to_dict(),
            "paper_card": self.paper_card.to_dict() if self.paper_card else None,
            "reader_payload": self.reader_payload.to_dict() if self.reader_payload else None,
            "rag_context": self.rag_context.to_dict() if self.rag_context else None,
            "reader_issue": self.reader_issue.to_dict() if self.reader_issue else None,
            "artifact_refs": dict(self.artifact_refs),
            "trace": self.trace.to_dict(),
            "transcript": self.transcript.to_dict(),
            "context_snapshot": self.context_snapshot.to_dict() if self.context_snapshot else None,
            "context_envelope": self.context_envelope.to_dict() if self.context_envelope else None,
            "compression_records": to_jsonable(self.compression_records),
            "skill_experience_refs": list(self.skill_experience_refs),
            "actor_scope": self.actor_scope.to_metadata(),
            "diagnostics": to_jsonable(self.diagnostics),
            "trace_ref": self.trace_ref,
            "reader_payload_ref": self.reader_payload_ref,
        }

    def to_persistence_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload["trace"] = self.trace.to_dict(
            include_deterministic_history=True
        )
        return payload

    def to_disposition_payload(self) -> dict[str, Any]:
        """Return the bounded evidence projection used by run disposition."""

        return {
            "run_id": self.run_id,
            "status": self.status,
            "quality": self.quality,
            "artifact_refs": dict(self.artifact_refs),
            "actor_scope": self.actor_scope,
            "trace": {"metadata": dict(self.trace.metadata)},
            "transcript": {
                "entries": [
                    {"metadata": dict(entry.metadata)}
                    for entry in self.transcript.entries()
                ]
            },
            "diagnostics": dict(self.diagnostics),
        }

    @classmethod
    def from_durable_failure(
        cls,
        *,
        request: AnalyzePaperRequest,
        events: tuple[HarnessEvent, ...],
        harness_status: str,
    ) -> "ResearchAnalysisResult":
        """Project a raised run into a scoped, non-published diagnostic result."""

        if not events or any(event.run_id != request.run_id for event in events):
            raise ValueError("durable failure history is missing or run-mismatched")
        if sum(
            event.event_type is HarnessEventType.RUN_CREATED for event in events
        ) != 1:
            raise ValueError("durable failure history must contain one run creation")
        actor_scope = AskPaperUseCase().resolve_actor_scope(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            memory_namespace=request.memory_namespace,
        )
        actor_metadata = actor_scope.to_metadata()
        quality = ResearchQualityResult(
            result_id=stable_research_id(
                "quality",
                request.run_id,
                "durable_runtime_failure",
            ),
            target_id=request.paper_id,
            target_type="summary",
            passed=False,
            score=0.0,
            gate_results=[
                GateResult.fail(
                    "ResearchRuntimeCompletionGate",
                    "runtime raised after durable run creation",
                )
            ],
        )
        graph_id, graph_version = _research_graph_identity_for_request(request)
        return cls(
            run_id=request.run_id,
            graph_id=graph_id,
            graph_version=graph_version,
            status=HarnessRunStatus.FAILED.value,
            analysis=None,
            quality=quality,
            paper_card=None,
            reader_payload=None,
            rag_context=None,
            reader_issue=None,
            artifact_refs={},
            trace=HarnessTrace(
                run_id=request.run_id,
                events=events,
                metadata={"paper_id": request.paper_id, **actor_metadata},
            ),
            transcript=_transcript_from_events(
                request.run_id,
                events,
                metadata=actor_metadata,
                research_rag_context=None,
                rag_context_pack=None,
            ),
            context_snapshot=None,
            context_envelope=None,
            compression_records=[],
            skill_experience_refs=[],
            actor_scope=actor_scope,
            diagnostics={
                "harness_status": harness_status,
                "terminal_reason": "runtime_exception_after_durable_run",
                "durable_history_cutoff": events[-1].event_id,
                "recovered_from_durable_history": True,
            },
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchAnalysisResult":
        if not isinstance(value, Mapping):
            raise ValueError("ResearchAnalysisResult payload must be an object")
        payload = dict(value)
        try:
            run_id = validate_artifact_path_segment(
                payload.pop("run_id"),
                field="run_id",
            )
            graph_id = str(payload.pop("graph_id"))
            graph_version = str(payload.pop("graph_version"))
            if not graph_id.strip() or not graph_version.strip():
                raise ValueError("ResearchAnalysisResult Graph identity is required")
            status = HarnessRunStatus(payload.pop("status")).value
            quality_payload = _result_mapping(
                payload.pop("quality"),
                "quality",
            )
            trace = HarnessTrace.from_dict(
                _result_mapping(payload.pop("trace"), "trace")
            )
            transcript = HarnessTranscript.from_dict(
                _result_mapping(payload.pop("transcript"), "transcript")
            )
        except KeyError as exc:
            raise ValueError(
                f"ResearchAnalysisResult field is required: {exc.args[0]}"
            ) from exc

        analysis_payload = _result_optional_mapping(
            payload.pop("analysis", None),
            "analysis",
        )
        paper_card_payload = _result_optional_mapping(
            payload.pop("paper_card", None),
            "paper_card",
        )
        reader_payload_value = _result_optional_mapping(
            payload.pop("reader_payload", None),
            "reader_payload",
        )
        rag_context_payload = _result_optional_mapping(
            payload.pop("rag_context", None),
            "rag_context",
        )
        reader_issue_payload = _result_optional_mapping(
            payload.pop("reader_issue", None),
            "reader_issue",
        )
        context_snapshot_payload = _result_optional_mapping(
            payload.pop("context_snapshot", None),
            "context_snapshot",
        )
        context_envelope_payload = _result_optional_mapping(
            payload.pop("context_envelope", None),
            "context_envelope",
        )
        artifact_refs = _result_text_mapping(
            payload.pop("artifact_refs", {}),
            "artifact_refs",
        )
        compression_records = _result_mapping_list(
            payload.pop("compression_records", []),
            "compression_records",
        )
        skill_experience_refs = _result_text_list(
            payload.pop("skill_experience_refs", []),
            "skill_experience_refs",
        )
        try:
            actor_scope_payload = _result_mapping(
                payload.pop("actor_scope"),
                "actor_scope",
            )
        except KeyError as exc:
            raise ValueError(
                "ResearchAnalysisResult field is required: actor_scope"
            ) from exc
        diagnostics = _result_mapping(
            payload.pop("diagnostics", {}),
            "diagnostics",
        )
        try:
            persisted_trace_ref = _result_optional_text(
                payload.pop("trace_ref"),
                "trace_ref",
            )
            persisted_reader_ref = _result_optional_text(
                payload.pop("reader_payload_ref"),
                "reader_payload_ref",
            )
        except KeyError as exc:
            raise ValueError(
                f"ResearchAnalysisResult field is required: {exc.args[0]}"
            ) from exc
        if payload:
            raise ValueError(
                "ResearchAnalysisResult payload contains unsupported fields: "
                + ", ".join(sorted(payload))
            )

        analysis = (
            ResearchAnalysis.model_validate(analysis_payload)
            if analysis_payload is not None
            else None
        )
        paper_card = (
            ResearchPaperCard.model_validate(paper_card_payload)
            if paper_card_payload is not None
            else None
        )
        reader_payload = (
            ResearchReaderPayload.model_validate(reader_payload_value)
            if reader_payload_value is not None
            else None
        )
        rag_context = (
            ResearchRAGContext.model_validate(rag_context_payload)
            if rag_context_payload is not None
            else None
        )
        reader_issue = (
            ReaderIssue.model_validate(reader_issue_payload)
            if reader_issue_payload is not None
            else None
        )
        context_snapshot = (
            ContextSnapshot.from_dict(context_snapshot_payload)
            if context_snapshot_payload is not None
            else None
        )
        context_envelope = (
            ContextEnvelope.from_dict(context_envelope_payload)
            if context_envelope_payload is not None
            else None
        )
        actor_scope = _persisted_actor_scope(
            actor_scope_payload,
            trace=trace,
            transcript=transcript,
            context_envelope=context_envelope,
            rag_context=rag_context,
        )
        result = cls(
            run_id=run_id,
            graph_id=graph_id,
            graph_version=graph_version,
            status=status,
            analysis=analysis,
            quality=ResearchQualityResult.model_validate(quality_payload),
            paper_card=paper_card,
            reader_payload=reader_payload,
            rag_context=rag_context,
            reader_issue=reader_issue,
            artifact_refs=artifact_refs,
            trace=trace,
            transcript=transcript,
            context_snapshot=context_snapshot,
            context_envelope=context_envelope,
            compression_records=compression_records,
            skill_experience_refs=skill_experience_refs,
            actor_scope=actor_scope,
            diagnostics=diagnostics,
        )
        _validate_persisted_result_identity(
            result,
            persisted_trace_ref=persisted_trace_ref,
            persisted_reader_ref=persisted_reader_ref,
        )
        return result


def _validate_persisted_result_identity(
    result: ResearchAnalysisResult,
    *,
    persisted_trace_ref: str | None,
    persisted_reader_ref: str | None,
) -> None:
    if result.trace.run_id != result.run_id:
        raise ValueError("Research result trace run_id mismatch")
    if result.transcript.run_id != result.run_id:
        raise ValueError("Research result transcript run_id mismatch")
    if any(
        entry.run_id != result.run_id
        for entry in result.transcript.entries()
    ):
        raise ValueError("Research result transcript entry run_id mismatch")
    if result.reader_issue is not None and result.reader_issue.run_id is not None:
        if result.reader_issue.run_id != result.run_id:
            raise ValueError("Research result reader issue run_id mismatch")
    if result.context_snapshot is not None:
        if (
            result.context_snapshot.run_id is not None
            and result.context_snapshot.run_id != result.run_id
        ):
            raise ValueError("Research result context snapshot run_id mismatch")
    if result.context_envelope is not None:
        if (
            result.context_envelope.run_id is not None
            and result.context_envelope.run_id != result.run_id
        ):
            raise ValueError("Research result context envelope run_id mismatch")
    if result.context_snapshot is not None and result.context_envelope is not None:
        if result.context_snapshot.envelope_id != result.context_envelope.envelope_id:
            raise ValueError("Research result context identity mismatch")
    paper_ids = _persisted_result_paper_ids(result)
    if len(paper_ids) > 1:
        raise ValueError("Research result paper identity mismatch")
    if result.status == HarnessRunStatus.SUCCEEDED.value and result.analysis is None:
        raise ValueError("succeeded Research result requires analysis")
    if persisted_trace_ref is not None and persisted_trace_ref != result.trace_ref:
        raise ValueError("Research result trace_ref mismatch")
    if persisted_reader_ref != result.reader_payload_ref:
        raise ValueError("Research result reader_payload_ref mismatch")


def _persisted_actor_scope(
    explicit: Mapping[str, Any],
    *,
    trace: HarnessTrace,
    transcript: HarnessTranscript,
    context_envelope: ContextEnvelope | None,
    rag_context: ResearchRAGContext | None,
) -> ResearchActorScope:
    projections: list[tuple[str, ResearchActorScope | None]] = [
        ("trace", _actor_scope_projection(trace.metadata)),
    ]
    projections.extend(
        (
            f"transcript[{index}]",
            _actor_scope_projection(entry.metadata),
        )
        for index, entry in enumerate(transcript.entries())
    )
    if context_envelope is not None:
        projections.append(
            (
                "context_envelope",
                _actor_scope_projection(context_envelope.metadata),
            )
        )
    if rag_context is not None:
        projections.extend(
            (
                label,
                _actor_scope_projection(metadata),
            )
            for label, metadata in (
                ("rag_context", rag_context.metadata),
                ("rag_goal", rag_context.goal.metadata),
            )
        )

    expected = _actor_scope_projection(explicit)
    if expected is None:
        raise ValueError("Research result actor_scope is incomplete")
    mismatched = [
        label
        for label, projection in projections
        if projection is None or projection != expected
    ]
    if mismatched:
        raise ValueError(
            "Research result actor scope mismatch: " + ", ".join(mismatched)
        )
    return expected


def _actor_scope_projection(
    metadata: Mapping[str, Any],
) -> ResearchActorScope | None:
    actor_keys = {"tenant_id", "user_id", "memory_namespace"}
    if not any(key in metadata for key in actor_keys):
        return None
    memory_namespace = str(metadata.get("memory_namespace") or "").strip()
    if not memory_namespace:
        raise ValueError("Research result actor scope requires memory_namespace")
    try:
        return AskPaperUseCase().resolve_actor_scope(
            tenant_id=str(metadata.get("tenant_id") or "").strip() or None,
            user_id=str(metadata.get("user_id") or "").strip() or None,
            memory_namespace=memory_namespace,
        )
    except ValueError:
        raise ValueError("Research result actor scope is invalid") from None


def _persisted_result_paper_ids(result: ResearchAnalysisResult) -> set[str]:
    values: list[Any] = [
        result.quality.target_id,
        result.trace.metadata.get("paper_id"),
    ]
    if result.analysis is not None:
        values.append(result.analysis.paper_id)
    if result.paper_card is not None:
        values.append(result.paper_card.paper_id)
    if result.reader_payload is not None:
        values.extend(
            (
                result.reader_payload.paper.paper_id,
                result.reader_payload.document.paper_id,
                result.reader_payload.analysis.paper_id,
                result.reader_payload.evidence.paper_id,
            )
        )
    if result.rag_context is not None:
        values.extend((result.rag_context.paper_id, result.rag_context.goal.paper_id))
    if result.reader_issue is not None:
        values.append(result.reader_issue.paper_id)
    if result.context_envelope is not None:
        values.append(result.context_envelope.metadata.get("paper_id"))
    if result.context_snapshot is not None:
        values.append(result.context_snapshot.metadata.get("paper_id"))
    return {
        value.strip()
        for value in values
        if isinstance(value, str) and value.strip()
    }


def _result_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"ResearchAnalysisResult {field_name} must be an object")
    return dict(value)


def _result_optional_mapping(
    value: Any,
    field_name: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return _result_mapping(value, field_name)


def _result_text_mapping(value: Any, field_name: str) -> dict[str, str]:
    mapping = _result_mapping(value, field_name)
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(item, str)
        or not item.strip()
        for key, item in mapping.items()
    ):
        raise ValueError(
            f"ResearchAnalysisResult {field_name} must map strings to strings"
        )
    return {key: item for key, item in mapping.items()}


def _result_mapping_list(value: Any, field_name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(
        not isinstance(item, Mapping) for item in value
    ):
        raise ValueError(
            f"ResearchAnalysisResult {field_name} must be a list of objects"
        )
    return [dict(item) for item in value]


def _result_text_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(
            f"ResearchAnalysisResult {field_name} must be a list of strings"
        )
    return list(value)


def _result_optional_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"ResearchAnalysisResult {field_name} must be a string or null"
        )
    return value


@dataclass(frozen=True, slots=True)
class _ResearchWorkerDependencies:
    source_provider: Any
    document_compiler: Any
    llm_worker: Any
    github_repository: Any
    rag_runtime: Any
    rag_memory_admission: ResearchRAGMemoryAdmissionPort | None
    taxonomy_registry: TaxonomyRegistry
    quality_gate: ResearchQualityGate
    evidence_builder: ResearchEvidenceBuilder
    reader_builder: ReaderPayloadBuilder
    paper_card_builder: PaperCardBuilder
    reader_issue_detector: ReaderIssueDetector
    rag_policy_builder: ResearchRAGPolicyBuilder
    citation_verifier: CitationVerifier


def _bind_research_worker(
    worker: Callable[..., HarnessWorkerResult],
    dependencies: _ResearchWorkerDependencies,
    workspace: "_ResearchRunWorkspace",
) -> Callable[[dict[str, Any]], HarnessWorkerResult]:
    def invoke(task: dict[str, Any]) -> HarnessWorkerResult:
        return worker(dependencies, task, workspace)

    return invoke


def _bind_research_workspace_worker(
    worker: Callable[..., HarnessWorkerResult],
    workspace: "_ResearchRunWorkspace",
) -> Callable[[dict[str, Any]], HarnessWorkerResult]:
    def invoke(task: dict[str, Any]) -> HarnessWorkerResult:
        return worker(task, workspace)

    return invoke


def _generate_research_candidate(
    worker: Any,
    *,
    task: str,
    payload: dict[str, Any],
    execution_identity: GraphExecutionIdentity | None,
) -> dict[str, Any]:
    if execution_identity is None:
        return worker.generate_candidate(task=task, payload=payload)
    return worker.generate_candidate(
        task=task,
        payload=payload,
        execution_identity=execution_identity,
    )


class ResearchSinglePaperRuntime:
    def __init__(
        self,
        *,
        source_provider: Any,
        document_compiler: Any,
        llm_worker: Any,
        github_repository: Any,
        rag_runtime: Any,
        rag_memory_admission: ResearchRAGMemoryAdmissionPort | None = None,
        artifact_port: ArtifactPort,
        event_port_factory: Callable[[str], HarnessTransitionPort],
        scoped_event_port_factory: (
            Callable[[str, Mapping[str, Any]], HarnessTransitionPort] | None
        ) = None,
        taxonomy_registry: TaxonomyRegistry | None = None,
        context_assembler: ContextAssembler | None = None,
        context_assembler_factory: (
            Callable[[str, HarnessTransitionPort], ContextAssembler] | None
        ) = None,
        context_max_input_tokens: int = 4_096,
        context_max_output_tokens: int = 1_024,
        quality_gate: ResearchQualityGate | None = None,
        side_effect_store: HarnessSideEffectStorePort | None = None,
        artifact_handler_factory: Callable[..., Any] | None = None,
        dynamic_task_plan_runner_factory: Callable[..., Any] | None = None,
        graph_result_committer_factory: Callable[..., Any] | None = None,
        graph_event_projection: GraphEventProjectionApplicationPort | None = None,
        graph_index_publisher: ResearchGraphStorageIndexPublisherPort | None = None,
        node_output_resource_factory: Callable[[str], HarnessNodeOutputResourcePort]
        | None = None,
        runtime_binding_registrar: HarnessWaitRuntimeRegistrationPort | None = None,
    ) -> None:
        self.source_provider = source_provider
        self.document_compiler = document_compiler
        self.llm_worker = llm_worker
        self.github_repository = github_repository
        self.rag_runtime = rag_runtime
        if rag_memory_admission is not None and not isinstance(
            rag_memory_admission,
            ResearchRAGMemoryAdmissionPort,
        ):
            raise TypeError(
                "rag_memory_admission must implement ResearchRAGMemoryAdmissionPort"
            )
        self.rag_memory_admission = rag_memory_admission
        self.artifact_port = artifact_port
        if graph_event_projection is not None and not isinstance(
            graph_event_projection,
            GraphEventProjectionApplicationPort,
        ):
            raise TypeError(
                "graph_event_projection must implement "
                "GraphEventProjectionApplicationPort"
            )
        self.graph_event_projection = graph_event_projection
        if graph_index_publisher is not None and not isinstance(
            graph_index_publisher,
            ResearchGraphStorageIndexPublisherPort,
        ):
            raise TypeError(
                "graph_index_publisher must implement "
                "ResearchGraphStorageIndexPublisherPort"
            )
        self.graph_index_publisher = graph_index_publisher
        if node_output_resource_factory is not None and not callable(
            node_output_resource_factory
        ):
            raise TypeError("node_output_resource_factory must be callable")
        self.node_output_resource_factory = node_output_resource_factory
        if runtime_binding_registrar is not None and not isinstance(
            runtime_binding_registrar,
            HarnessWaitRuntimeRegistrationPort,
        ):
            raise TypeError(
                "runtime_binding_registrar must implement "
                "HarnessWaitRuntimeRegistrationPort"
            )
        self.runtime_binding_registrar = runtime_binding_registrar
        if not callable(event_port_factory):
            raise TypeError("event_port_factory must be callable")
        if scoped_event_port_factory is not None and not callable(
            scoped_event_port_factory
        ):
            raise TypeError("scoped_event_port_factory must be callable")
        self.event_port_factory = event_port_factory
        self.scoped_event_port_factory = scoped_event_port_factory
        self.taxonomy_registry = taxonomy_registry or TaxonomyRegistry.default()
        if context_assembler_factory is not None and not callable(
            context_assembler_factory
        ):
            raise TypeError("context_assembler_factory must be callable")
        if context_assembler is not None and context_assembler_factory is not None:
            raise TypeError(
                "configure context_assembler or context_assembler_factory, not both"
            )
        self.context_assembler = context_assembler
        self.context_assembler_factory = context_assembler_factory
        for field_name, value in (
            ("context_max_input_tokens", context_max_input_tokens),
            ("context_max_output_tokens", context_max_output_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise TypeError(f"{field_name} must be a positive integer")
        self.context_max_input_tokens = context_max_input_tokens
        self.context_max_output_tokens = context_max_output_tokens
        self.side_effect_store = side_effect_store or InMemoryHarnessSideEffectStore()
        if not isinstance(self.side_effect_store, HarnessSideEffectStorePort):
            raise TypeError("side_effect_store must implement HarnessSideEffectStorePort")
        if artifact_handler_factory is not None and not callable(artifact_handler_factory):
            raise TypeError("artifact_handler_factory must be callable")
        self.artifact_handler_factory = artifact_handler_factory
        if dynamic_task_plan_runner_factory is not None and not callable(dynamic_task_plan_runner_factory):
            raise TypeError("dynamic_task_plan_runner_factory must be callable")
        self.dynamic_task_plan_runner_factory = dynamic_task_plan_runner_factory
        if graph_result_committer_factory is not None and not callable(
            graph_result_committer_factory
        ):
            raise TypeError("graph_result_committer_factory must be callable")
        self.graph_result_committer_factory = graph_result_committer_factory
        self.ask_use_case = AskPaperUseCase()
        self.quality_gate = quality_gate or ResearchQualityGate()
        self.evidence_builder = ResearchEvidenceBuilder()
        self.reader_builder = ReaderPayloadBuilder()
        self.paper_card_builder = PaperCardBuilder()
        self.reader_issue_detector = ReaderIssueDetector()
        self.rag_policy_builder = ResearchRAGPolicyBuilder()
        self.citation_verifier = CitationVerifier()
        self.gate_registry = build_paper_analysis_gate_registry()

    def run(self, request: AnalyzePaperRequest) -> ResearchAnalysisResult:
        if _dynamic_task_plan_requested(request.options) and (
            self.dynamic_task_plan_runner_factory is None
        ):
            raise ResearchDynamicTaskPlanUnavailableError()
        actor_scope = self.ask_use_case.resolve_actor_scope(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            memory_namespace=request.memory_namespace,
        )
        request = replace(
            request,
            tenant_id=actor_scope.tenant_id,
            user_id=actor_scope.user_id,
            memory_namespace=actor_scope.memory_namespace,
        )
        run_id = validate_artifact_path_segment(request.run_id, field="run_id")
        if not isinstance(self.artifact_port, RunBoundArtifactPort):
            raise TypeError("artifact_port must implement RunBoundArtifactPort")
        with self.artifact_port.bind_run(run_id):
            return self._run_bound(request, run_id)

    def _run_bound(
        self,
        request: AnalyzePaperRequest,
        run_id: str,
    ) -> ResearchAnalysisResult:
        actor_metadata = _request_actor_metadata(request)
        event_port = (
            self.event_port_factory(run_id)
            if self.scoped_event_port_factory is None
            else self.scoped_event_port_factory(run_id, actor_metadata)
        )
        if not isinstance(event_port, HarnessTransitionPort):
            raise TypeError("event port factory must return HarnessTransitionPort")
        context_assembler = self.context_assembler
        if self.context_assembler_factory is not None:
            context_assembler = self.context_assembler_factory(run_id, event_port)
            if not isinstance(context_assembler, ContextAssembler):
                raise TypeError(
                    "context_assembler_factory must return ContextAssembler"
                )
        workspace = _ResearchRunWorkspace(
            request=request,
            context_assembler=context_assembler or ContextAssembler(),
            context_max_input_tokens=self.context_max_input_tokens,
            context_max_output_tokens=self.context_max_output_tokens,
            graph_transition_port=event_port,
        )
        identity_scope_ref = research_identity_scope_ref(actor_metadata)
        subject_scope_ref = research_subject_scope_ref(request.paper_id)
        run_spec = build_research_harness_run_spec(
            request,
            created_at=_research_run_created_at(event_port, run_id),
        )
        graph_result_committer = (
            None
            if self.graph_result_committer_factory is None
            else self.graph_result_committer_factory(
                event_port=event_port,
                request=request,
                workspace=workspace,
            )
        )
        if graph_result_committer is not None and not isinstance(
            graph_result_committer,
            HarnessGraphResultCommitterPort,
        ):
            raise TypeError(
                "graph_result_committer_factory must return "
                "HarnessGraphResultCommitterPort or None"
            )
        terminal_payload_factory = lambda cutoff: self._terminal_artifact_payloads(
            event_port,
            workspace,
            run_id,
            cutoff,
            actor_metadata,
        )
        candidate_payload_factory = lambda intent: self._candidate_artifact_payloads(
            workspace,
            intent,
        )
        if self.artifact_handler_factory is None:
            artifact_handler = _CompatibilityResearchArtifactBundleHandler(
                artifact_port=self.artifact_port,
                side_effect_store=self.side_effect_store,
                terminal_payload_factory=terminal_payload_factory,
                candidate_payload_factory=candidate_payload_factory,
                graph_event_projection=self.graph_event_projection,
            )
        else:
            artifact_handler = self.artifact_handler_factory(
                artifact_port=self.artifact_port,
                side_effect_store=self.side_effect_store,
                terminal_payload_factory=terminal_payload_factory,
                candidate_payload_factory=candidate_payload_factory,
                graph_event_projection=self.graph_event_projection,
                graph_index_publisher=self.graph_index_publisher,
            )
        side_effect_registry = HarnessSideEffectRegistry(
            (
                HarnessSideEffectHandlerBinding(
                    reference=RESEARCH_ARTIFACT_HANDLER_REF,
                    kind=RESEARCH_ARTIFACT_EFFECT_KIND,
                    handler=artifact_handler,
                    supports_origins=(
                        HarnessSideEffectOrigin.WORKER.value,
                        HarnessSideEffectOrigin.CONTROLLER_TERMINAL.value,
                    ),
                ),
            )
        )
        runtime_binding_authority = build_paper_analysis_runtime_binding_authority(
            definition=run_spec.graph,
            worker_implementations=self._worker_registry(workspace),
            gate_registry=self.gate_registry,
            side_effect_registry=side_effect_registry,
        )
        control_plane = HarnessControlPlane(
            event_port=event_port,
            runtime_binding_authority=runtime_binding_authority,
            gate_registry=self.gate_registry,
            graph_preflight=HarnessGraphPreflight(
                policy=HarnessGraphPreflightPolicy(
                    max_active_nodes=4,
                    max_parallelism=1,
                )
            ),
            side_effect_registry=side_effect_registry,
            side_effect_store=self.side_effect_store,
            graph_result_committer=graph_result_committer,
        )
        if self.node_output_resource_factory is None:
            raise HarnessValidationError(
                "Research production Graph execution requires durable node-output storage",
                code="graph_node_output_resource_missing",
            )
        node_output_resource = self.node_output_resource_factory(run_id)
        if not isinstance(node_output_resource, HarnessNodeOutputResourcePort):
            raise TypeError(
                "node_output_resource_factory must return HarnessNodeOutputResourcePort"
            )
        physical_executor = HarnessGraphPhysicalActivityExecutor(
            binding_authority=runtime_binding_authority,
            input_resolver=control_plane,
            node_output_resource=node_output_resource,
            result_committer=None,
            supervisor=AttemptSupervisor(),
        )
        dispatcher = HarnessGraphPhysicalActivityDispatcher(
            executor=physical_executor,
            graph_resolver=control_plane.graph_for_activity,
            input_resolver=control_plane,
            accept=control_plane.accept_graph_activity_for_execution,
            record_call_marker=control_plane.record_graph_activity_call_marker,
            record_result=control_plane.record_graph_activity_result_event,
            apply_result=control_plane.commit_physical_graph_result,
            durable_recovery_resolver=event_port.recover_graph,
            capabilities_resolver=lambda activity_ref: runtime_binding_authority.resolve_activity(
                activity_ref,
                required_usage="parallel",
            ).capabilities,
        )
        # The installed physical dispatcher owns both execution and the
        # cooperative cancellation events referenced by durable Graph decisions.
        control_plane.install_graph_activity_dispatcher(dispatcher)
        if self.runtime_binding_registrar is not None:
            self.runtime_binding_registrar.register(
                run_spec,
                control_plane,
                tenant_scope_ref=run_spec.metadata["tenant_scope_ref"],
                identity_scope_ref=run_spec.metadata["identity_scope_ref"],
            )
        # The control plane persists quarantine before this lifecycle hook
        # removes owned hidden candidates. Preserve a primary worker or
        # handler failure if cleanup itself encounters a secondary I/O error.
        cleanup_candidates = getattr(artifact_handler, "cleanup_candidates", None)
        failed_run_statuses = frozenset(
            {
                HarnessRunStatus.BLOCKED,
                HarnessRunStatus.CANCELLED,
                HarnessRunStatus.FAILED,
                HarnessRunStatus.HALTED,
            }
        )
        try:
            harness_result = control_plane.run(run_spec)
        except Exception as primary_error:
            if callable(cleanup_candidates):
                try:
                    cleanup_candidates(run_id)
                except Exception as cleanup_error:
                    primary_error.add_note(
                        "Research candidate cleanup failed: "
                        f"{type(cleanup_error).__name__}"
                    )
                    LOGGER.error(
                        "Research candidate cleanup failed after a primary run error",
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
            raise
        else:
            if callable(cleanup_candidates):
                try:
                    cleanup_candidates(run_id)
                except Exception as cleanup_error:
                    if harness_result.status not in failed_run_statuses:
                        raise
                    LOGGER.error(
                        "Research candidate cleanup failed after a terminal Graph failure",
                        exc_info=(
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        ),
                    )
        terminal_outcomes = [
            outcome
            for slot, outcome in harness_result.side_effect_outcomes.items()
            if isinstance(slot, str) and slot.startswith("terminal:")
        ]
        if len(terminal_outcomes) > 1:
            raise HarnessValidationError(
                "Graph run produced multiple terminal side-effect outcomes"
            )
        terminal_outcome = terminal_outcomes[0] if terminal_outcomes else None
        if (
            terminal_outcome is None
            and harness_result.status in failed_run_statuses
            and self.artifact_handler_factory is not None
            and not _harness_result_has_execution_error(harness_result)
        ):
            if not isinstance(
                self.artifact_port,
                GraphTerminalManifestRecorderPort,
            ):
                raise TypeError(
                    "production artifact_port must implement "
                    "GraphTerminalManifestRecorderPort"
                )
            self._stage_unpublished_graph_event_projection(
                run_spec=run_spec,
                harness_result=harness_result,
            )
            persisted_manifest = self.artifact_port.commit_unpublished_terminal_manifest(
                _unpublished_terminal_manifest_request(
                    run_spec=run_spec,
                    harness_result=harness_result,
                )
            )
            if self.graph_index_publisher is not None:
                if persisted_manifest.manifest_hash is None:
                    raise HarnessValidationError(
                        "unpublished Graph terminal manifest has no canonical hash",
                        code="research_graph_index_manifest_hash_missing",
                    )
                self.graph_index_publisher.publish(
                    run_id=persisted_manifest.run_id,
                    expected_manifest_hash=persisted_manifest.manifest_hash,
                )
        if (
            workspace.analysis is None
            and harness_result.status is HarnessRunStatus.SUCCEEDED
            and terminal_outcome is not None
            and terminal_outcome.disposition is HarnessSideEffectDisposition.ACCEPTED
        ):
            return self._recover_published_result(
                request=request,
                actor_metadata=actor_metadata,
                event_port=event_port,
                outcome=terminal_outcome,
                events=harness_result.events,
            )
        trace = HarnessTrace(
            run_id=run_id,
            events=harness_result.events,
            metadata={"paper_id": request.paper_id, **actor_metadata},
        )
        transcript = _transcript_from_events(
            run_id,
            harness_result.events,
            metadata=actor_metadata,
            research_rag_context=workspace.research_rag_context,
            rag_context_pack=workspace.rag_context_pack,
            expected_graph_identity=workspace.rag_graph_identity
            or _context_graph_identity_for_request(
                request,
                stage_id="run_research_rag",
            ),
        )
        artifacts = dict(workspace.artifact_refs)
        if (
            harness_result.status == HarnessRunStatus.SUCCEEDED
            and terminal_outcome is not None
            and terminal_outcome.disposition is HarnessSideEffectDisposition.ACCEPTED
        ):
            published = terminal_outcome.metadata.get("artifact_refs")
            if isinstance(published, Mapping):
                artifacts.update(
                    {
                        str(key): str(value)
                        for key, value in published.items()
                        if isinstance(key, str) and isinstance(value, str)
                    }
                )
        workspace.artifact_refs = dict(artifacts)
        quality = workspace.quality or ResearchQualityResult(
            result_id=stable_research_id("quality", run_id, "halted"),
            target_id=request.paper_id,
            target_type="summary",
            passed=False,
            score=0.0,
            gate_results=[GateResult.fail("ResearchBudgetGate", "run halted before quality result was produced")],
        )
        worker_results = {}
        nodes_by_instance = {
            node.instance_id: node for node in harness_result.state.node_instances
        }
        for node_instance_id, worker_result in harness_result.worker_results.items():
            node = nodes_by_instance.get(node_instance_id)
            if node is None:
                raise HarnessValidationError(
                    "Graph worker result has no matching node instance",
                    code="graph_worker_result_node_missing",
                    details={"node_instance_id": node_instance_id},
                )
            result_payload = worker_result.to_dict()
            result_payload.update(
                {
                    "node_id": node.identity.node_id,
                    "step_id": node.step_id,
                    "node_instance_id": node.instance_id,
                    "attempt": node.attempt,
                }
            )
            worker_results[node_instance_id] = result_payload
        diagnostics = {
            "harness_status": harness_result.status.value,
            "terminal_reason": harness_result.state.terminal_reason_code,
            "worker_results": worker_results,
            "gate_failures": _gate_failures(harness_result.events),
            "research_diagnostics": list(workspace.diagnostics),
        }
        if terminal_outcome is not None:
            diagnostics.update(
                {
                    "terminal_side_effect_outcome_ref": terminal_outcome.checksum,
                    "publication_authority_ref": terminal_outcome.decision_ref,
                    "terminal_history_cutoff": terminal_outcome.metadata.get(
                        "history_cutoff"
                    ),
                    "artifact_evidence_ref": checksum_for(
                        {"artifact_refs": dict(artifacts)}
                    ),
                }
            )
        return ResearchAnalysisResult(
            run_id=run_id,
            graph_id=run_spec.graph.graph_id,
            graph_version=run_spec.graph.graph_version,
            status=harness_result.status.value,
            analysis=workspace.analysis,
            quality=quality,
            paper_card=workspace.paper_card,
            reader_payload=workspace.reader_payload,
            rag_context=workspace.research_rag_context,
            reader_issue=workspace.reader_issue,
            artifact_refs=artifacts,
            trace=trace,
            transcript=transcript,
            context_snapshot=workspace.context_snapshot,
            context_envelope=workspace.context_envelope,
            compression_records=list(workspace.compression_records),
            skill_experience_refs=list(workspace.skill_experience_refs),
            actor_scope=self.ask_use_case.resolve_actor_scope(
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                memory_namespace=request.memory_namespace,
            ),
            diagnostics=diagnostics,
        )

    def _recover_published_result(
        self,
        *,
        request: AnalyzePaperRequest,
        actor_metadata: Mapping[str, Any],
        event_port: HarnessTransitionPort,
        outcome: HarnessSideEffectOutcome,
        events: tuple[HarnessEvent, ...],
    ) -> ResearchAnalysisResult:
        """Rehydrate a terminally published run without invoking workers.

        A crash can occur after the terminal manifest is visible but before
        the application has rebuilt its in-memory workspace.  Artifact reads
        remain subject to the normal accepted-run resolver; this method only
        projects already-authorized bytes back into the domain result.
        """

        raw_refs = outcome.metadata.get("artifact_refs")
        if not isinstance(raw_refs, Mapping):
            raise HarnessValidationError(
                "terminal publication outcome has no artifact refs",
                code="research_recovery_artifact_refs_missing",
            )
        artifact_refs = {
            str(key): str(value)
            for key, value in raw_refs.items()
            if isinstance(key, str) and isinstance(value, str)
        }

        def payload(artifact_type: str, *, required: bool = False) -> dict[str, Any] | None:
            ref = artifact_refs.get(artifact_type)
            if ref is None:
                if required:
                    raise HarnessValidationError(
                        f"terminal publication is missing {artifact_type}",
                        code="research_recovery_artifact_missing",
                    )
                return None
            envelope = self.artifact_port.read_artifact(ref)
            if envelope.get("artifact_type") != artifact_type:
                raise HarnessValidationError(
                    f"terminal publication artifact identity mismatch: {artifact_type}",
                    code="research_recovery_artifact_identity_mismatch",
                )
            value = envelope.get("payload")
            if not isinstance(value, Mapping):
                raise HarnessValidationError(
                    f"terminal publication artifact payload is invalid: {artifact_type}",
                    code="research_recovery_artifact_payload_invalid",
                )
            return dict(value)

        analysis = payload("research-analysis", required=True)
        quality = payload("research-quality-result", required=True)
        trace = HarnessTrace(
            run_id=request.run_id,
            events=events,
            metadata={"paper_id": request.paper_id, **dict(actor_metadata)},
        )
        transcript = _transcript_from_events(
            request.run_id,
            events,
            metadata=actor_metadata,
            research_rag_context=None,
            rag_context_pack=None,
        )
        compression = payload("research-context-compression-records") or {}
        compression_records = compression.get("records", [])
        if not isinstance(compression_records, list):
            raise HarnessValidationError(
                "terminal publication compression records are invalid",
                code="research_recovery_compression_invalid",
            )
        result = ResearchAnalysisResult.from_dict(
            {
                "run_id": request.run_id,
                "graph_id": _research_graph_identity_for_request(request)[0],
                "graph_version": _research_graph_identity_for_request(request)[1],
                "status": HarnessRunStatus.SUCCEEDED.value,
                "analysis": analysis,
                "quality": quality,
                "paper_card": payload("research-paper-card"),
                "reader_payload": payload("research-reader-payload"),
                "rag_context": payload("research-rag-context-pack"),
                "reader_issue": payload("reader-issue"),
                "artifact_refs": artifact_refs,
                "trace": trace.to_dict(include_deterministic_history=True),
                "transcript": transcript.to_dict(),
                "context_snapshot": payload("research-context-snapshot"),
                "context_envelope": None,
                "compression_records": compression_records,
                "skill_experience_refs": [],
                "actor_scope": self.ask_use_case.resolve_actor_scope(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    memory_namespace=request.memory_namespace,
                ).to_metadata(),
                "diagnostics": {
                    "harness_status": HarnessRunStatus.SUCCEEDED.value,
                    "publication_authority_ref": outcome.decision_ref,
                    "terminal_side_effect_outcome_ref": outcome.checksum,
                    "terminal_history_cutoff": outcome.metadata.get("history_cutoff"),
                    "artifact_evidence_ref": outcome.metadata.get(
                        "artifact_evidence_ref"
                    ),
                    "recovered_from_durable_publication": True,
                },
                "trace_ref": f"harness-trace://{request.run_id}",
                "reader_payload_ref": artifact_refs.get("research-reader-payload"),
            }
        )
        if result.analysis is None or not result.quality.passed:
            raise HarnessValidationError(
                "terminal publication did not contain an accepted Research result",
                code="research_recovery_result_not_accepted",
            )
        return result

    def _terminal_artifact_payloads(
        self,
        event_port: HarnessTransitionPort,
        workspace: "_ResearchRunWorkspace",
        run_id: str,
        cutoff: str | None,
        actor_metadata: Mapping[str, Any],
    ) -> tuple[ArtifactWriteRequest, ...]:
        """Build trace/transcript candidates from committed history only.

        The cutoff is supplied by the controller-terminal intent.  It keeps
        the publication payload independent from the decision/outcome that
        will reference it, while the complete event history remains the
        replay source.
        """

        events = list(event_port.read_history(run_id))
        if cutoff is not None:
            indexes = [index for index, event in enumerate(events) if event.event_id == cutoff]
            if len(indexes) != 1:
                raise HarnessValidationError(
                    "terminal artifact history cutoff is not present in committed history"
                )
            events = events[: indexes[0] + 1]
        trace = HarnessTrace(
            run_id=run_id,
            events=events,
            metadata={"paper_id": workspace.request.paper_id, **dict(actor_metadata)},
        )
        transcript = _transcript_from_events(
            run_id,
            events,
            metadata=actor_metadata,
            research_rag_context=workspace.research_rag_context,
            rag_context_pack=workspace.rag_context_pack,
            expected_graph_identity=workspace.rag_graph_identity
            or _context_graph_identity_for_request(
                workspace.request,
                stage_id="run_research_rag",
            ),
        )
        metadata = {"run_id": run_id, **dict(actor_metadata)}
        return (
            ArtifactWriteRequest(
                artifact_type="harness-trace",
                payload=trace.to_dict(),
                metadata=metadata,
            ),
            ArtifactWriteRequest(
                artifact_type="harness-transcript",
                payload=transcript.to_dict(),
                metadata=metadata,
            ),
        )

    @staticmethod
    def _candidate_artifact_payloads(
        workspace: "_ResearchRunWorkspace",
        intent: HarnessSideEffectIntent,
    ) -> tuple[ArtifactWriteRequest, ...]:
        bundle_ref = intent.payload.get("bundle_ref")
        if not isinstance(bundle_ref, str):
            raise HarnessValidationError("Research artifact intent has no bundle ref")
        requests = workspace.pending_artifact_bundles.get(bundle_ref)
        if requests is None:
            raise HarnessValidationError(
                "Research artifact candidate bundle is unavailable",
                code="research_artifact_candidate_missing",
            )
        return requests

    def _stage_unpublished_graph_event_projection(
        self,
        *,
        run_spec: HarnessRunSpec,
        harness_result: Any,
    ) -> None:
        """Stage the Graph projection before a failed terminal manifest commit."""

        if self.graph_event_projection is None:
            return
        graph_state = harness_result.state
        tenant_id = run_spec.metadata.get("graph_terminal_tenant_id")
        if graph_state is None or not isinstance(tenant_id, str) or not tenant_id.strip():
            raise HarnessValidationError(
                "unpublished terminal Graph projection context is incomplete",
                code="research_graph_event_projection_context_missing",
            )
        identity = GraphRunIdentity(
            run_id=run_spec.run_id,
            graph_id=graph_state.graph_ref.graph_id,
            graph_version=graph_state.graph_ref.identity_version,
            graph_ref=(
                f"{graph_state.graph_ref.graph_id}@"
                f"{graph_state.graph_ref.identity_version}"
            ),
            graph_checksum=graph_state.graph_ref.checksum,
        )
        result = self.graph_event_projection.project_graph_history(
            GraphEventProjectionApplicationRequest(
                graph_identity=identity,
                target=self.artifact_port.manager.run_dir(run_spec.run_id)
                / "events.jsonl",
                tenant_id=tenant_id,
            )
        )
        if (
            result.status is not GraphEventProjectionApplicationStatus.PROJECTED
            or result.projection is None
        ):
            raise HarnessValidationError(
                "unpublished terminal Graph event projection is not projectable",
                code="research_graph_event_projection_not_projectable",
            )
        self.artifact_port.stage_graph_event_projection(
            result.projection,
            graph_identity=identity,
            tenant_id=tenant_id,
        )

    def _worker_registry(self, workspace: "_ResearchRunWorkspace") -> dict[str, Any]:
        dependencies = _ResearchWorkerDependencies(
            source_provider=self.source_provider,
            document_compiler=self.document_compiler,
            llm_worker=self.llm_worker,
            github_repository=self.github_repository,
            rag_runtime=self.rag_runtime,
            rag_memory_admission=self.rag_memory_admission,
            taxonomy_registry=self.taxonomy_registry,
            quality_gate=self.quality_gate,
            evidence_builder=self.evidence_builder,
            reader_builder=self.reader_builder,
            paper_card_builder=self.paper_card_builder,
            reader_issue_detector=self.reader_issue_detector,
            rag_policy_builder=self.rag_policy_builder,
            citation_verifier=self.citation_verifier,
        )
        worker_type = type(self)
        registry = {
            "load_paper_source": worker_type._load_paper_source,
            "compile_document": worker_type._compile_document,
            "run_research_rag": worker_type._run_research_rag,
            "build_evidence_pack": worker_type._build_evidence_pack,
            "analyze_structure": worker_type._analyze_structure,
            "analyze_contribution": worker_type._analyze_contribution,
            "analyze_experiments": worker_type._analyze_experiments,
            "verify_claims": worker_type._verify_claims,
            "quality_gate": worker_type._quality_gate,
            "build_reader_payload": worker_type._build_reader_payload,
            "build_paper_card": worker_type._build_paper_card,
        }
        if _dynamic_task_plan_requested(workspace.request.options):
            for activity_id in (
                "analyze_structure",
                "analyze_contribution",
                "analyze_experiments",
            ):
                registry.pop(activity_id)
        bound_registry = {
            worker_name: _bind_research_worker(
                worker,
                dependencies,
                workspace,
            )
            for worker_name, worker in registry.items()
        }
        bound_registry["publish_artifacts"] = _bind_research_workspace_worker(
            worker_type._publish_artifacts,
            workspace,
        )
        if _dynamic_task_plan_requested(workspace.request.options):
            if self.dynamic_task_plan_runner_factory is None:
                raise ResearchDynamicTaskPlanUnavailableError()
            dynamic_worker = self.dynamic_task_plan_runner_factory(
                workspace=workspace,
                dependencies=dependencies,
            )
            if not callable(dynamic_worker):
                execute = getattr(dynamic_worker, "run", None)
                if not callable(execute):
                    raise TypeError("dynamic_task_plan_runner_factory must return a callable or runner")
                dynamic_worker = execute
            bound_registry["dynamic_analysis_stage"] = dynamic_worker
        return bound_registry

    def _load_paper_source(self, task: dict[str, Any], workspace: "_ResearchRunWorkspace") -> HarnessWorkerResult:
        paper = self.source_provider.fetch_paper(workspace.request.source_ref)
        source_record = self.source_provider.fetch_source_record(paper.paper_id)
        workspace.paper = paper
        workspace.source_record = source_record
        return _ok(
            {
                "paper": paper.to_dict(),
                "source_record": source_record.to_dict(),
                "source_refs": [workspace.request.source_ref],
            }
        )

    def _compile_document(self, task: dict[str, Any], workspace: "_ResearchRunWorkspace") -> HarnessWorkerResult:
        if workspace.paper is None or workspace.source_record is None:
            return _failed("paper source must be loaded before compile_document")
        compile_method = self.document_compiler.compile
        execution_identity = _context_graph_identity_for_activity(
            task,
            workspace.request,
            stage_id="compile_document",
        ).to_graph_execution_identity()
        if _accepts_keyword(compile_method, "execution_identity"):
            document = compile_method(
                workspace.source_record,
                execution_identity=execution_identity,
            )
        else:
            # Contract fakes and deterministic in-process compilers may keep
            # the original one-argument application port. Inspect before
            # calling so compatibility does not execute a side effect twice.
            document = compile_method(workspace.source_record)
        workspace.document = document
        return _ok({"document": document.to_dict(), "source_refs": document.lineage.source_refs})

    def _run_research_rag(self, task: dict[str, Any], workspace: "_ResearchRunWorkspace") -> HarnessWorkerResult:
        if workspace.paper is None or workspace.document is None:
            return _failed("paper and document are required before RAG")
        goal = ResearchRetrievalGoal(
            goal_id=stable_research_id("research_goal", workspace.request.run_id, workspace.paper.paper_id),
            paper_id=workspace.paper.paper_id,
            question="Build grounded evidence for the accepted paper.",
            required_evidence_types=["method", "experiment", "limitation", "claim_support"],
            target_sections=[section.section_id for section in workspace.document.sections],
            allowed_source_refs=[
                source_ref for section in workspace.document.sections for source_ref in [section.source_ref]
            ]
            or list(workspace.document.lineage.source_refs),
            allowed_memory_namespaces=[
                str(workspace.request.memory_namespace)
            ],
            constraints={"paper_only": True},
            metadata=_request_actor_metadata(workspace.request),
        )
        rag_graph_identity = _context_graph_identity_for_activity(
            task,
            workspace.request,
            stage_id="run_research_rag",
        )
        session_spec = self.rag_policy_builder.build_session_spec(
            graph_identity=rag_graph_identity,
            session_id=stable_research_id("research_rag", workspace.request.run_id, workspace.paper.paper_id),
            goal=goal,
            budget=_rag_budget_from_options(workspace.request.options),
        )
        session_spec = replace(
            session_spec,
            context_policy={
                **session_spec.context_policy,
                "max_input_tokens": _effective_context_input_limit(workspace),
                "max_output_tokens": workspace.context_max_output_tokens,
            },
        )
        memory_recall = None
        if self.rag_memory_admission is not None:
            memory_recall = self.rag_memory_admission.admit(
                task,
                actor_scope=ResearchActorScope(
                    tenant_id=workspace.request.tenant_id,
                    user_id=workspace.request.user_id,
                    memory_namespace=str(workspace.request.memory_namespace),
                ),
                graph_identity=rag_graph_identity,
                dynamic_task_plan=_dynamic_task_plan_requested(workspace.request.options),
            )
        rag_context = self.rag_runtime.run(
            session_spec=session_spec,
            document=workspace.document,
            **({"memory_recall": memory_recall} if memory_recall is not None else {}),
        )
        context_pack_for_run = getattr(self.rag_runtime, "context_pack_for_run", None)
        if callable(context_pack_for_run):
            workspace.rag_context_pack = context_pack_for_run(workspace.request.run_id)
        else:
            workspace.rag_context_pack = getattr(self.rag_runtime, "last_context_pack", None)
        workspace.research_rag_context = rag_context
        workspace.rag_graph_identity = rag_graph_identity
        output = {
            "research_rag_context": rag_context.to_dict(),
            "source_refs": rag_context.source_refs,
            "rag_budget": session_spec.budget.to_dict(),
        }
        if rag_context.gap_report.missing_information:
            output["rag_gap_report"] = rag_context.gap_report.to_dict()
        return _ok(output)

    def _build_evidence_pack(self, task: dict[str, Any], workspace: "_ResearchRunWorkspace") -> HarnessWorkerResult:
        if workspace.document is None:
            return _failed("document is required before evidence pack")
        pack = self.evidence_builder.build_from_document(document=workspace.document)
        if workspace.research_rag_context is not None:
            existing = {item.evidence_id for item in pack.items}
            merged = list(pack.items)
            for item in workspace.research_rag_context.accepted_evidence:
                if item.evidence_id not in existing:
                    merged.append(item)
                    existing.add(item.evidence_id)
            pack = ResearchEvidencePack(
                pack_id=pack.pack_id,
                paper_id=pack.paper_id,
                items=merged,
                coverage={
                    **pack.coverage,
                    "rag_required_evidence": 0.0
                    if workspace.research_rag_context.gap_report.missing_information
                    else 1.0,
                },
                missing_information=workspace.research_rag_context.gap_report.missing_information,
                lineage=pack.lineage,
            )
        workspace.evidence_pack = pack
        return _ok({"evidence_pack": pack.to_dict(), "source_refs": pack.lineage.source_refs})

    def _analyze_structure(
        self,
        task: dict[str, Any],
        workspace: "_ResearchRunWorkspace",
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> HarnessWorkerResult:
        candidate = _generate_research_candidate(
            self.llm_worker,
            task="candidate_three_minute_read",
            payload={
                **({"dependency_outputs": task["dependency_outputs"]} if task.get("dependency_outputs") else {}),
                "paper": workspace.paper.to_dict() if workspace.paper else {},
                "evidence_pack": workspace.evidence_pack.to_dict() if workspace.evidence_pack else {},
            },
            execution_identity=execution_identity,
        )
        workspace.llm_candidate_warnings.extend(_forbidden_candidate_keys(candidate))
        summary_payload = dict(candidate.get("three_minute_read", {}))
        evidence_by_id = {
            item.evidence_id: item
            for item in (workspace.evidence_pack.items if workspace.evidence_pack else ())
        }
        evidence_refs: list[EvidenceRef] = []
        for item in summary_payload.get("evidence_refs", []):
            evidence_ref = EvidenceRef(**item) if isinstance(item, dict) else item
            canonical_evidence = evidence_by_id.get(evidence_ref.evidence_id)
            if canonical_evidence is not None and evidence_ref.source_ref != canonical_evidence.source_ref:
                evidence_ref = evidence_ref.model_copy(
                    update={
                        "source_ref": canonical_evidence.source_ref,
                        "metadata": {
                            **evidence_ref.metadata,
                            "candidate_source_ref": evidence_ref.source_ref,
                            "source_ref_normalized": True,
                        },
                    }
                )
            evidence_refs.append(evidence_ref)
        summary = ThreeMinuteRead(
            problem=str(summary_payload.get("problem") or ""),
            core_idea=str(summary_payload.get("core_idea") or ""),
            key_contributions=[str(item) for item in summary_payload.get("key_contributions", [])],
            method_summary=str(summary_payload.get("method_summary") or ""),
            experiment_summary=str(summary_payload.get("experiment_summary") or ""),
            limitations=[str(item) for item in summary_payload.get("limitations", [])],
            why_it_matters=str(summary_payload.get("why_it_matters") or ""),
            read_next=[str(item) for item in summary_payload.get("read_next", [])],
            evidence_refs=evidence_refs,
            confidence=float(summary_payload.get("confidence", 0.0) or 0.0),
        )
        workspace.summary = summary
        return _ok(
            {
                "candidate_ref": stable_research_id(
                    "candidate", workspace.request.run_id, "summary"
                ),
                "three_minute_read": summary.to_dict(),
                "claims": [summary.core_idea],
                "warnings": list(workspace.llm_candidate_warnings),
            }
        )

    def _analyze_contribution(
        self,
        task: dict[str, Any],
        workspace: "_ResearchRunWorkspace",
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> HarnessWorkerResult:
        workspace.taxonomy_candidates = [
            TaxonomyCandidate(
                candidate_id=stable_research_id(
                    "taxonomy_candidate",
                    workspace.request.run_id,
                    item["level"],
                    item["term_id"],
                ),
                level=item["level"],
                term_id=item["term_id"],
                label=item["label"],
                evidence_refs=item["evidence_refs"],
                confidence=float(item.get("confidence", 0.0)),
            )
            for item in _generate_research_candidate(
                self.llm_worker,
                task="candidate_taxonomy",
                payload={
                    **({"dependency_outputs": task["dependency_outputs"]} if task.get("dependency_outputs") else {}),
                    "paper": workspace.paper.to_dict() if workspace.paper else {},
                    "evidence_pack": (
                        workspace.evidence_pack.to_dict()
                        if workspace.evidence_pack
                        else {}
                    ),
                },
                execution_identity=execution_identity,
            ).get("taxonomy_candidates", [])
        ]
        assignment = TaxonomyAssignmentBuilder(self.taxonomy_registry).build(
            workspace.request.paper_id, workspace.taxonomy_candidates
        )
        workspace.taxonomy_assignment = assignment
        evidence_refs = [
            item.as_ref().to_dict()
            for item in (
                workspace.evidence_pack.items if workspace.evidence_pack else ()
            )
        ]
        return _ok(
            {
                "contributions": [
                    candidate.label for candidate in workspace.taxonomy_candidates
                ],
                "taxonomy_assignment": assignment.to_dict(),
                "taxonomy_review_candidate_ids": assignment.review_candidate_ids,
                "summary_evidence_refs": evidence_refs,
            }
        )

    def _analyze_experiments(
        self,
        task: dict[str, Any],
        workspace: "_ResearchRunWorkspace",
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> HarnessWorkerResult:
        candidate = _generate_research_candidate(
            self.llm_worker,
            task="candidate_experiment_claims",
            payload={
                **({"dependency_outputs": task["dependency_outputs"]} if task.get("dependency_outputs") else {}),
                "evidence_pack": workspace.evidence_pack.to_dict()
                if workspace.evidence_pack
                else {}
            },
            execution_identity=execution_identity,
        )
        claims: list[ResearchClaim] = []
        scores: list[ResearchScore] = []
        candidate_scores: list[dict[str, Any]] = []
        for item in candidate.get("claims", []):
            claims.append(
                ResearchClaim(
                    claim_id=str(item.get("claim_id")),
                    text=str(item.get("text")),
                    claim_type=str(item.get("claim_type", "experiment")),
                    section_id=item.get("section_id"),
                    evidence_ids=[str(ref) for ref in item.get("evidence_ids", [])],
                    confidence=float(item.get("confidence", 0.0)),
                )
            )
        for item in candidate.get("scores", []):
            candidate_source_refs = [str(ref) for ref in item.get("source_refs", [])]
            canonical_source_refs = _canonicalize_evidence_source_refs(
                candidate_source_refs,
                workspace.evidence_pack,
            )
            candidate_score = {
                "score_id": str(item.get("score_id")),
                "paper_id": workspace.request.paper_id,
                "benchmark_id": str(item.get("benchmark_id")),
                "dataset_id": str(item.get("dataset_id")),
                "metric_id": str(item.get("metric_id")),
                "value": float(item.get("value")),
                "source_refs": canonical_source_refs,
            }
            if canonical_source_refs != candidate_source_refs:
                candidate_score["metadata"] = {
                    "candidate_source_refs": candidate_source_refs,
                    "source_refs_normalized": True,
                }
            candidate_scores.append(candidate_score)
            if _score_candidate_is_in_supported_range(candidate_score):
                scores.append(ResearchScore(**candidate_score))
        workspace.candidate_scores = candidate_scores
        if len(scores) != len(candidate_scores):
            workspace.score_gate_results.append(
                GateResult.fail(
                    "ResearchScoreRangeGate",
                    "candidate benchmark score is outside supported range",
                    metadata={
                        "violations": {
                            str(item.get("score_id", "score")): item.get("value")
                            for item in candidate_scores
                            if not _score_candidate_is_in_supported_range(item)
                        }
                    },
                )
            )
        workspace.claims = claims
        workspace.scores = scores
        return _ok(
            {
                "claims": [claim.text for claim in claims],
                "claim_models": [claim.to_dict() for claim in claims],
                "scores": candidate_scores,
                "claim_confidence_observation": min(
                    [claim.confidence for claim in claims] or [1.0]
                ),
            }
        )

    def _verify_claims(
        self, task: dict[str, Any], workspace: "_ResearchRunWorkspace"
    ) -> HarnessWorkerResult:
        if workspace.evidence_pack is None:
            return _failed("evidence pack is required before claim verification")
        branch_refs = _analysis_branch_refs_from_input(
            task.get("inputs", {}).get("analysis_branch_refs")
        )
        if not isinstance(branch_refs, tuple | list):
            return _failed(
                "analysis branch refs are required before claim verification"
            )
        expected_outputs = {
            ("analyze_structure", "structure_candidate"),
            ("analyze_contribution", "contribution_candidate"),
            ("analyze_experiments", "experiment_candidate"),
        }
        actual_outputs = {
            (item.get("producer_node_id"), item.get("output_key"))
            for item in branch_refs
            if isinstance(item, Mapping)
        }
        if (
            len(branch_refs) != len(expected_outputs)
            or actual_outputs != expected_outputs
        ):
            return _failed("analysis branch refs are incomplete or invalid")
        if workspace.summary is None:
            return _failed("summary candidate is required before claim verification")
        workspace.contributions = list(workspace.summary.key_contributions)
        gate_results = self.citation_verifier.verify_claims(
            workspace.claims, workspace.evidence_pack
        )
        workspace.claim_gate_results = gate_results
        return _ok(
            {
                "analysis_branch_refs": [dict(item) for item in branch_refs],
                "claim_gate_results": [result.to_dict() for result in gate_results],
                "claim_models": [claim.to_dict() for claim in workspace.claims],
                "evidence_pack": workspace.evidence_pack.to_dict(),
            }
        )

    def _quality_gate(
        self, task: dict[str, Any], workspace: "_ResearchRunWorkspace"
    ) -> HarnessWorkerResult:
        if (
            workspace.paper is None
            or workspace.summary is None
            or workspace.evidence_pack is None
        ):
            return _failed(
                "paper, summary, and evidence are required before quality gate"
            )
        analysis = ResearchAnalysis(
            paper_id=workspace.paper.paper_id,
            summary=workspace.summary,
            contributions=workspace.contributions,
            methods=[
                candidate.term_id
                for candidate in workspace.taxonomy_candidates
                if candidate.level == "area"
            ],
            experiments=[
                claim.text
                for claim in workspace.claims
                if claim.claim_type == "experiment"
            ],
            limitations=workspace.summary.limitations,
            reproducibility=[],
            related_work=[],
            claims=workspace.claims,
            evidence_pack_id=workspace.evidence_pack.pack_id,
            quality={"llm_candidate_warnings": list(workspace.llm_candidate_warnings)},
        )
        gate_results = [*workspace.claim_gate_results, *workspace.score_gate_results]
        if not analysis.summary.evidence_refs:
            gate_results.append(GateResult.fail("ResearchEvidenceCoverageGate", "analysis summary requires evidence refs"))
        if not analysis.claims:
            gate_results.append(GateResult.fail("ResearchReportReadinessGate", "analysis requires claims"))
        if workspace.research_rag_context and workspace.research_rag_context.gap_report.missing_information:
            gate_results.append(GateResult.fail("ResearchRAGEvidenceNeedGate", "required RAG evidence is missing"))
        quality = self.quality_gate.evaluate(target_id=workspace.paper.paper_id, target_type="summary", gate_results=gate_results)
        workspace.analysis = analysis
        workspace.quality = quality
        return _ok(
            {
                "analysis": analysis.to_dict(),
                "research_quality": quality.to_dict(),
                "gate_failures": [flag.to_dict() for flag in quality.quality_flags],
            }
        )

    def _build_reader_payload(self, task: dict[str, Any], workspace: "_ResearchRunWorkspace") -> HarnessWorkerResult:
        if workspace.paper is None or workspace.document is None:
            return _failed("paper and document are required before reader payload")
        reader_payload = self.reader_builder.build(
            paper=workspace.paper,
            document=workspace.document,
            analysis=workspace.analysis,
            evidence=workspace.evidence_pack,
        )
        issues = self.reader_issue_detector.detect(reader_payload)
        workspace.reader_payload = reader_payload
        workspace.reader_issue = issues[0] if issues else None
        return _ok(
            {
                "reader_payload": reader_payload.to_dict(),
                "reader_issue": workspace.reader_issue.to_dict() if workspace.reader_issue else None,
            }
        )

    def _build_paper_card(self, task: dict[str, Any], workspace: "_ResearchRunWorkspace") -> HarnessWorkerResult:
        if workspace.paper is None:
            return _failed("paper is required before paper card")
        github_profile = None
        repository_status = "missing"
        repository_diagnostics: list[str] = []
        if workspace.paper.code_url:
            github_profile = self.github_repository.fetch_profile(workspace.paper.code_url)
            repository_status = "available"
        else:
            repository_diagnostics.append("code_repository_missing")
            if "code_repository_missing" not in workspace.diagnostics:
                workspace.diagnostics.append("code_repository_missing")
        taxonomy = workspace.taxonomy_assignment or TaxonomyAssignment(paper_id=workspace.paper.paper_id)
        card = self.paper_card_builder.build(
            paper=workspace.paper,
            three_minute_read=workspace.summary,
            taxonomy={
                "domains": taxonomy.domains,
                "areas": taxonomy.areas,
                "tasks": taxonomy.tasks,
                "methods": [candidate.term_id for candidate in workspace.taxonomy_candidates if candidate.level == "area"],
                "benchmarks": [score.benchmark_id for score in workspace.scores],
            },
            github=github_profile.to_dict() if github_profile is not None else None,
            reader_payload_status="needs_repair" if workspace.reader_issue else ("ready" if workspace.reader_payload else "missing"),
            metadata={
                "source_lineage": [workspace.request.source_ref],
                "code_repository_status": repository_status,
                "code_repository_diagnostics": repository_diagnostics,
            },
        )
        workspace.paper_card = card
        return _ok(
            {
                "paper_card": card.to_dict(),
                "code_repository_status": repository_status,
                "code_repository_diagnostics": repository_diagnostics,
            }
        )

    @staticmethod
    def _publish_artifacts(
        task: dict[str, Any],
        workspace: "_ResearchRunWorkspace",
    ) -> HarnessWorkerResult:
        members: list[dict[str, Any]] = []
        actor_metadata = _request_actor_metadata(workspace.request)

        def add_member(
            artifact_type: str,
            payload: Mapping[str, Any],
            *,
            metadata: Mapping[str, Any] | None = None,
        ) -> None:
            workspace.planned_artifact_refs[artifact_type] = (
                f"artifact://{workspace.request.run_id}/{artifact_type}"
            )
            members.append(
                ArtifactWriteRequest(
                    artifact_type=artifact_type,
                    payload=dict(payload),
                    metadata={
                        "run_id": workspace.request.run_id,
                        **actor_metadata,
                        **dict(metadata or {}),
                    },
                ).to_dict()
            )

        if workspace.analysis:
            add_member("research-analysis", workspace.analysis.to_dict())
        if workspace.reader_payload:
            add_member("research-reader-payload", workspace.reader_payload.to_dict())
        if workspace.paper_card:
            add_member("research-paper-card", workspace.paper_card.to_dict())
        if workspace.quality:
            add_member("research-quality-result", workspace.quality.to_dict())
        if workspace.research_rag_context:
            add_member(
                "research-rag-context-pack",
                workspace.research_rag_context.to_dict(),
            )
        if workspace.reader_issue:
            add_member("reader-issue", workspace.reader_issue.to_dict())
        workspace.context_envelope = ResearchSinglePaperRuntime._assemble_context(
            workspace
        )
        workspace.context_snapshot = workspace.context_assembler.snapshot_store.load(
            workspace.context_envelope.snapshot_ref or ""
        )
        workspace.compression_records = _research_ordered_compression_records(
            list(
                workspace.context_assembler.compression_record_projections(
                    envelope_id=workspace.context_envelope.envelope_id,
                )
            )
        )
        add_member(
            "research-context-snapshot",
            workspace.context_snapshot.to_dict(),
        )
        add_member(
            "research-context-compression-records",
            {"records": list(workspace.compression_records)},
        )
        if workspace.research_rag_context and workspace.research_rag_context.gap_report.missing_information:
            add_member(
                "research-rag-gap-report",
                workspace.research_rag_context.gap_report.to_dict(),
            )
        skill_experience = ResearchSinglePaperRuntime._record_skill_experience(workspace)
        workspace.skill_experience_refs.append(skill_experience.experience_id)
        bundle_ref = checksum_for(
            {
                "schema_version": RESEARCH_ARTIFACT_SCHEMA_VERSION,
                "run_id": workspace.request.run_id,
                "paper_id": workspace.request.paper_id,
                "members": members,
            }
        )
        requests = tuple(
            ArtifactWriteRequest(
                artifact_type=str(member["artifact_type"]),
                payload=dict(member["payload"]),
                media_type=str(member["media_type"]),
                metadata=dict(member["metadata"]),
            )
            for member in members
        )
        workspace.pending_artifact_bundles[bundle_ref] = requests
        member_refs = [
            {
                "artifact_type": request.artifact_type,
                "request_ref": checksum_for(request.to_dict()),
            }
            for request in requests
        ]
        raw_activity_context = task.get("harness_graph_activity")
        if not isinstance(raw_activity_context, Mapping):
            raise HarnessValidationError(
                "publish_artifacts requires a Graph activity identity"
            )
        activity_context = HarnessGraphActivityTaskContext.from_dict(
            raw_activity_context
        )
        attempt = activity_context.activity.attempt
        effect_digest = checksum_for(
            {
                "run_id": workspace.request.run_id,
                "step_id": "publish_artifacts",
                "attempt": attempt,
                "bundle_ref": bundle_ref,
            }
        ).removeprefix("sha256:")
        intent = HarnessSideEffectIntent(
            effect_id=f"research-artifact-effect:{effect_digest}",
            kind=RESEARCH_ARTIFACT_EFFECT_KIND,
            run_id=workspace.request.run_id,
            graph_id=activity_context.activity.graph_ref.graph_id,
            graph_version=activity_context.activity.graph_ref.identity_version,
            graph_ref=activity_context.activity.graph_ref.identity_ref.exact_ref,
            graph_checksum=activity_context.activity.graph_ref.checksum,
            origin=HarnessSideEffectOrigin.WORKER,
            atomic_group=f"research-artifacts:{effect_digest}",
            identity_scope_ref=research_identity_scope_ref(actor_metadata),
            subject_scope_ref=research_subject_scope_ref(workspace.request.paper_id),
            attempt=attempt,
            node_id=activity_context.activity.node_id,
            node_instance_id=activity_context.activity.node_instance_id,
            activity_id=activity_context.activity.activity_id,
            worker_result_ref=(
                f"worker-result://{workspace.request.run_id}/publish_artifacts/{attempt}"
            ),
            candidate_checksum=bundle_ref,
            handler=RESEARCH_ARTIFACT_HANDLER_REF,
            payload={
                "schema_version": RESEARCH_ARTIFACT_SCHEMA_VERSION,
                "run_id": workspace.request.run_id,
                "paper_id": workspace.request.paper_id,
                "bundle_ref": bundle_ref,
                "member_refs": member_refs,
            },
            retention_until=None,
        )
        return _ok(
            {
                "artifact_bundle_ref": bundle_ref,
                "artifact_types": [member["artifact_type"] for member in members],
                "skill_experience_refs": list(workspace.skill_experience_refs),
            },
            effect_intent=intent,
        )

    @staticmethod
    def _assemble_context(workspace: "_ResearchRunWorkspace") -> ContextEnvelope:
        source_refs = []
        if workspace.research_rag_context:
            source_refs = list(workspace.research_rag_context.source_refs)
        return workspace.context_assembler.assemble(
            {
                "run_id": workspace.request.run_id,
                "step_id": "publish_artifacts",
                "phase": "verify",
                "worker_id": "research-analysis-worker",
                "worker_type": "subagent",
                "graph_identity": _context_graph_identity_for_request(
                    workspace.request,
                    stage_id="publish_artifacts",
                ),
                "worker_contract_ref": "schema://research.analysis.output",
                "run_state_ref": f"run-state://{workspace.request.run_id}",
                "evidence_memory_ref": workspace.research_rag_context.context_id if workspace.research_rag_context else "evidence-memory://empty",
                "current_task_ref": "task://publish_artifacts",
                "current_instruction": "Publish verified Research artifacts only after deterministic gates pass.",
                "source_refs": source_refs,
                "artifact_refs": (
                    ()
                    if workspace.context_assembler.requires_approved_artifact_context
                    else tuple(workspace.planned_artifact_refs.values())
                ),
                "evidence_refs": tuple(workspace.evidence_pack.evidence_ids if workspace.evidence_pack else ()),
                "allowed_tools": ("retrieval.search", "retrieval.read_source"),
                "allowed_memory_namespaces": (
                    str(workspace.request.memory_namespace),
                ),
                "budget": ContextBudget(
                    max_input_tokens=_effective_context_input_limit(workspace),
                    max_output_tokens=workspace.context_max_output_tokens,
                    max_context_segments=6,
                    max_evidence_items=8,
                    max_memory_items=6,
                    max_artifact_refs=24,
                    reserved_output_tokens=512,
                    compression_threshold=0.8,
                ),
                "evidence_memory_tokens": int(workspace.request.options.get("evidence_memory_tokens", 120)),
                "metadata": {
                    "paper_id": workspace.request.paper_id,
                    **_request_actor_metadata(workspace.request),
                    "stable_prefix_excludes": ["full_paper_text", "github_metrics", "user_notes", "dynamic_rag_results"],
                },
            }
        )

    @staticmethod
    def _record_skill_experience(workspace: "_ResearchRunWorkspace") -> SkillExperience:
        research_quality_score = workspace.quality.score if workspace.quality else 0.0
        return SkillExperience(
            experience_id=stable_research_id("skill_experience", workspace.request.run_id, workspace.request.paper_id),
            run_id=workspace.request.run_id,
            step_id="quality_gate",
            skill_name="research-contribution-analysis",
            skill_version="1.0.0",
            domain="research",
            task_type="paper_analysis",
            input_refs=[workspace.request.source_ref],
            output_refs=list(workspace.planned_artifact_refs.values()),
            transcript_refs=[f"harness-transcript://{workspace.request.run_id}"],
            gate_results=[result.to_dict() for result in workspace.claim_gate_results],
            score=research_quality_score,
            outcome=(
                SkillExperienceOutcome.SUCCESS
                if research_quality_score >= 0.8
                else SkillExperienceOutcome.FAILURE
            ),
            evidence_refs=workspace.evidence_pack.evidence_ids if workspace.evidence_pack else (),
            source="research_single_paper_run",
            summary="Single-paper Research analysis experience recorded for offline skill evolution.",
            metadata={"skill_promotion_triggered": False, "package_hash": "sha256:fake-research-skill"},
        )

@dataclass
class _ResearchRunWorkspace:
    request: AnalyzePaperRequest
    context_assembler: ContextAssembler
    context_max_input_tokens: int = 4_096
    context_max_output_tokens: int = 1_024
    graph_transition_port: HarnessTransitionPort | None = None
    paper: ResearchPaper | None = None
    source_record: PaperSourceRecord | None = None
    document: ResearchDocument | None = None
    evidence_pack: ResearchEvidencePack | None = None
    rag_context_pack: RAGContextPack | None = None
    research_rag_context: ResearchRAGContext | None = None
    rag_graph_identity: ContextGraphIdentity | None = None
    summary: ThreeMinuteRead | None = None
    contributions: list[str] = field(default_factory=list)
    claims: list[ResearchClaim] = field(default_factory=list)
    candidate_scores: list[dict[str, Any]] = field(default_factory=list)
    scores: list[ResearchScore] = field(default_factory=list)
    taxonomy_candidates: list[TaxonomyCandidate] = field(default_factory=list)
    taxonomy_assignment: TaxonomyAssignment | None = None
    analysis: ResearchAnalysis | None = None
    quality: ResearchQualityResult | None = None
    reader_payload: ResearchReaderPayload | None = None
    paper_card: ResearchPaperCard | None = None
    reader_issue: ReaderIssue | None = None
    claim_gate_results: list[GateResult] = field(default_factory=list)
    score_gate_results: list[GateResult] = field(default_factory=list)
    llm_candidate_warnings: list[str] = field(default_factory=list)
    artifact_refs: dict[str, str] = field(default_factory=dict)
    planned_artifact_refs: dict[str, str] = field(default_factory=dict)
    pending_artifact_bundles: dict[str, tuple[ArtifactWriteRequest, ...]] = field(
        default_factory=dict
    )
    context_snapshot: ContextSnapshot | None = None
    context_envelope: ContextEnvelope | None = None
    compression_records: list[dict[str, Any]] = field(default_factory=list)
    skill_experience_refs: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)


def _effective_context_input_limit(workspace: _ResearchRunWorkspace) -> int:
    requested = workspace.request.options.get(
        "context_max_input_tokens",
        workspace.context_max_input_tokens,
    )
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise HarnessValidationError(
            "context_max_input_tokens must be a positive integer"
        )
    return min(requested, workspace.context_max_input_tokens)


class _CompatibilityResearchArtifactBundleHandler:
    """Test-only adapter for simple in-memory ``ArtifactPort`` fakes.

    Production composition always injects the filesystem handler.  Keeping
    this small adapter local preserves existing unit-test ports without
    granting any worker direct access to their commit method.
    """

    def __init__(
        self,
        *,
        artifact_port: ArtifactPort,
        side_effect_store: HarnessSideEffectStorePort,
        terminal_payload_factory: Callable[[str | None], tuple[ArtifactWriteRequest, ...]],
        candidate_payload_factory: Callable[
            [HarnessSideEffectIntent], tuple[ArtifactWriteRequest, ...]
        ],
        graph_event_projection: GraphEventProjectionApplicationPort | None = None,
    ) -> None:
        self._artifact_port = artifact_port
        self._side_effect_store = side_effect_store
        self._terminal_payload_factory = terminal_payload_factory
        self._candidate_payload_factory = candidate_payload_factory
        self._graph_event_projection = graph_event_projection
        self._prepared: dict[str, tuple[tuple[ArtifactWriteRequest, ...], HarnessSideEffectOutcome]] = {}

    def prepare(
        self,
        intent: HarnessSideEffectIntent,
        authorization: HarnessSideEffectDecision,
    ) -> HarnessSideEffectOutcome:
        _validate_compat_authority(
            intent,
            authorization,
            origin=HarnessSideEffectOrigin.WORKER,
            disposition=HarnessSideEffectDisposition.PREPARED,
        )
        requests = list(self._candidate_payload_factory(intent))
        if not requests or not all(
            isinstance(request, ArtifactWriteRequest) for request in requests
        ):
            raise HarnessValidationError("Research artifact bundle requires members")
        committed_at = utc_now()
        candidate_refs = tuple(
            f"artifact-candidate://{intent.run_id}/{intent.effect_id.rsplit(':', 1)[-1]}/{request.artifact_type}"
            for request in requests
        )
        outcome = HarnessSideEffectOutcome(
            outcome_id=f"research-artifact-prepared:{intent.effect_id.rsplit(':', 1)[-1]}",
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
            disposition=HarnessSideEffectDisposition.PREPARED,
            node_id=intent.node_id,
            node_instance_id=intent.node_instance_id,
            activity_id=intent.activity_id,
            terminal_action=intent.terminal_action,
            attempt=intent.attempt,
            candidate_refs=candidate_refs,
            result_ref=checksum_for(
                {"members": [request.to_dict() for request in requests]}
            ),
            reason_code="prepared_test_fake",
            committed_at=committed_at,
            retention_until=committed_at + timedelta(hours=1),
            metadata={
                "test_only": True,
                "members": [request.to_dict() for request in requests],
            },
        )
        assert outcome.checksum is not None
        self._prepared[outcome.checksum] = (tuple(requests), outcome)
        return outcome

    def commit(
        self,
        intent: HarnessSideEffectIntent,
        authorization: HarnessSideEffectDecision,
    ) -> HarnessSideEffectOutcome:
        _validate_compat_authority(
            intent,
            authorization,
            origin=HarnessSideEffectOrigin.CONTROLLER_TERMINAL,
            disposition=HarnessSideEffectDisposition.ACCEPTED,
        )
        prepared_refs = intent.payload.get("prepared_outcome_refs")
        if not isinstance(prepared_refs, (list, tuple)):
            raise HarnessValidationError("terminal Research intent requires prepared outcomes")
        prepared = [
            self._prepared[ref]
            for ref in prepared_refs
            if isinstance(ref, str) and ref in self._prepared
        ]
        if len(prepared) != len(prepared_refs):
            raise HarnessValidationError("terminal Research prepared outcome is unavailable")
        requests = [request for group, _ in prepared for request in group]
        cutoff = intent.payload.get("history_cutoff")
        requests.extend(
            self._terminal_payload_factory(
                cutoff if isinstance(cutoff, str) else None
            )
        )
        refs = [self._artifact_port.write_artifact(request) for request in requests]
        artifact_refs = {ref.artifact_type: ref.ref for ref in refs}
        committed_at = utc_now()
        return HarnessSideEffectOutcome(
            outcome_id=f"research-artifact-published:{intent.effect_id.rsplit(':', 1)[-1]}",
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
            candidate_refs=tuple(
                ref
                for _, outcome in prepared
                for ref in outcome.candidate_refs
            ),
            public_refs=tuple(ref.ref for ref in refs),
            result_ref=checksum_for(
                {
                    "artifact_refs": artifact_refs,
                    "authority": authorization.checksum,
                    "history_cutoff": cutoff,
                }
            ),
            reason_code="published_test_fake",
            committed_at=committed_at,
            metadata={
                "test_only": True,
                "artifact_refs": artifact_refs,
                "publication_authority_ref": authorization.checksum,
                "history_cutoff": cutoff,
            },
        )


def _validate_compat_authority(
    intent: HarnessSideEffectIntent,
    authorization: HarnessSideEffectDecision,
    *,
    origin: HarnessSideEffectOrigin,
    disposition: HarnessSideEffectDisposition,
) -> None:
    if (
        intent.origin is not origin
        or authorization.origin is not origin
        or authorization.disposition is not disposition
        or authorization.intent_ref != intent.checksum
        or authorization.effect_id != intent.effect_id
        or authorization.run_id != intent.run_id
        or authorization.identity_scope_ref != intent.identity_scope_ref
        or authorization.subject_scope_ref != intent.subject_scope_ref
        or authorization.atomic_group != intent.atomic_group
    ):
        raise HarnessValidationError("Research artifact authority does not match intent")


def _research_run_created_at(
    event_port: HarnessTransitionPort,
    run_id: str,
) -> datetime:
    """Reuse the durable run identity timestamp during crash recovery."""

    history = event_port.read_history(run_id)
    if not history:
        return utc_now()
    created = tuple(
        event
        for event in history
        if event.event_type is HarnessEventType.RUN_CREATED
    )
    if len(created) == 1:
        return created[0].occurred_at
    if not created:
        recover_graph = getattr(event_port, "recover_graph", None)
        if callable(recover_graph):
            recovery = recover_graph(run_id)
            initialization = tuple(
                commit
                for commit in recovery.projection_commits
                if commit.commit_kind.value == "initialize"
            )
            if recovery.state is not None and len(initialization) == 1:
                return initialization[0].occurred_at
    if history or created:
        raise HarnessValidationError(
            "Research durable history must contain one RUN_CREATED event",
            code="research_run_created_event_invalid",
            details={
                "code": "research_run_created_event_invalid",
                "run_id": run_id,
                "count": len(created),
            },
        )
    return utc_now()


def _unpublished_terminal_manifest_request(
    *,
    run_spec: HarnessRunSpec,
    harness_result,
) -> GraphTerminalManifestCommitRequest:
    graph_state = harness_result.state
    terminal_node_ids = tuple(harness_result.graph_terminal_node_ids)
    if graph_state is None or not terminal_node_ids:
        raise HarnessValidationError(
            "unpublished terminal record requires explicit Graph state",
            code="research_graph_terminal_state_missing",
        )
    if graph_state.projection_checksum is None:
        raise HarnessValidationError(
            "unpublished terminal record requires a durable state checksum",
            code="research_graph_terminal_state_missing",
        )
    tenant_id = run_spec.metadata.get("graph_terminal_tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise HarnessValidationError(
            "unpublished terminal record requires a tenant identity",
            code="research_graph_terminal_tenant_missing",
        )
    try:
        execution_versions = GraphExecutionVersionManifest.from_normalized_graph(
            HarnessGraphCompiler().compile(run_spec.graph).graph
        )
    except (TypeError, ValueError, HarnessValidationError) as exc:
        raise HarnessValidationError(
            "unpublished terminal record requires a compilable Graph definition",
            code="research_graph_terminal_execution_versions_missing",
        ) from exc
    return GraphTerminalManifestCommitRequest(
        context=GraphTerminalManifestContext(
            tenant_id=tenant_id,
            graph_id=graph_state.graph_ref.graph_id,
            graph_version=graph_state.graph_ref.identity_version,
            graph_schema_version=graph_state.graph_ref.schema_version,
            compiler_version=graph_state.graph_ref.compiler_version,
            normalized_graph_checksum=graph_state.graph_ref.checksum,
            execution_versions=execution_versions,
            started_at=run_spec.created_at,
            terminal_node_ids=terminal_node_ids,
        ),
        run_id=run_spec.run_id,
        status=GraphTerminalStatus(harness_result.status.value),
        completed_at=max(
            (event.occurred_at for event in harness_result.events),
            default=run_spec.created_at,
        ),
        terminal_state_ref=graph_state.projection_checksum,
        gate_evidence_refs=_unpublished_terminal_gate_evidence_refs(
            harness_result.events,
            terminal_evidence_ref=graph_state.terminal_evidence_ref,
            terminal_state_ref=graph_state.projection_checksum,
        ),
    )


def _unpublished_terminal_gate_evidence_refs(
    events: tuple[HarnessEvent, ...],
    *,
    terminal_evidence_ref: str | None,
    terminal_state_ref: str,
) -> tuple[str, ...]:
    refs: list[str] = []
    for event in events:
        if event.event_type is not HarnessEventType.GATE_EVALUATED:
            continue
        details = event.payload.get("details")
        harness_gate = (
            details.get("harness_gate") if isinstance(details, Mapping) else None
        )
        result_ref = (
            harness_gate.get("result_ref")
            if isinstance(harness_gate, Mapping)
            else event.payload.get("result_ref")
        )
        if isinstance(result_ref, str) and result_ref not in refs:
            refs.append(result_ref)
    for fallback in (terminal_evidence_ref, terminal_state_ref):
        if isinstance(fallback, str) and fallback not in refs:
            refs.append(fallback)
    return tuple(refs)


def _budget_from_options(options: dict[str, Any]) -> HarnessBudget:
    return HarnessBudget(
        max_turns=min(int(options.get("max_turns", 40)), 64),
        max_replans=min(int(options.get("max_replans", 3)), 6),
        max_retries_per_step=min(int(options.get("max_retries_per_step", 2)), 4),
        max_worker_calls=min(int(options.get("max_worker_calls", 32)), 64),
    )


def _dynamic_task_plan_requested(options: Mapping[str, Any]) -> bool:
    return bool(options.get("dynamic_analysis") or options.get("dynamic_task_plan"))


def _rag_budget_from_options(options: dict[str, Any]) -> RAGBudget:
    return RAGBudget(
        max_rounds=min(int(options.get("rag_max_rounds", 6)), 8),
        max_replans=_rag_max_replans_from_options(options),
        max_queries=min(int(options.get("rag_max_queries", 12)), 16),
        max_source_reads=min(int(options.get("rag_max_source_reads", 24)), 32),
        max_memory_hits=min(int(options.get("rag_max_memory_hits", 8)), 12),
        max_context_items=8,
        max_context_tokens=4096,
        max_worker_calls=16,
    )


def _rag_max_replans_from_options(options: dict[str, Any]) -> int:
    raw_value = options.get("rag_max_replans", 3)
    if isinstance(raw_value, bool):
        raise ValueError("rag_max_replans must be an integer between 0 and 4")
    if isinstance(raw_value, int):
        value = raw_value
    elif isinstance(raw_value, str):
        text = raw_value.strip()
        digits = text[1:] if text[:1] in {"+", "-"} else text
        if not digits.isascii() or not digits.isdigit():
            raise ValueError("rag_max_replans must be an integer between 0 and 4")
        value = int(text)
    else:
        raise ValueError("rag_max_replans must be an integer between 0 and 4")
    if value < 0 or value > 4:
        raise ValueError("rag_max_replans must be an integer between 0 and 4")
    return value


def _accepts_keyword(callable_value: Any, keyword: str) -> bool:
    """Return whether a callable explicitly exposes the optional keyword."""

    try:
        parameter = inspect.signature(callable_value).parameters.get(keyword)
    except (TypeError, ValueError):
        return False
    return parameter is not None and parameter.kind in {
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }


def _ok(
    output: dict[str, Any],
    *,
    effect_intent: HarnessSideEffectIntent | None = None,
) -> HarnessWorkerResult:
    return HarnessWorkerResult(
        status=HarnessWorkerStatus.SUCCEEDED,
        output=output,
        effect_intent=effect_intent,
    )


def _failed(error: str) -> HarnessWorkerResult:
    return HarnessWorkerResult(status=HarnessWorkerStatus.FAILED, error=error)


def _forbidden_candidate_keys(candidate: dict[str, Any]) -> list[str]:
    forbidden = {"next_step", "route", "quality_passed", "write_memory", "publish_artifact", "promote_skill"}
    return sorted(forbidden.intersection(candidate))


def _analysis_branch_refs_from_input(value: Any) -> Any:
    """Normalize static merge and dynamic-stage output envelopes.

    A verified static merge exposes ``analysis_branch_refs`` directly. A
    single-output dynamic graph node exposes its complete worker output under
    that output key, so the established branch-ref contract is one level
    deeper. Only that exact envelope shape is unwrapped.
    """

    if isinstance(value, Mapping) and set(value) == {
        "aggregate_ref",
        "aggregate_checksum",
        "output_refs_by_role",
        "analysis_branch_refs",
    }:
        return value.get("analysis_branch_refs")
    return value


def _score_candidate_is_in_supported_range(candidate: dict[str, Any]) -> bool:
    value = candidate.get("value")
    refs = candidate.get("source_refs")
    return isinstance(value, int | float) and -1_000_000_000 <= value <= 1_000_000_000 and bool(refs)


def _canonicalize_evidence_source_refs(
    source_refs: list[str],
    evidence_pack: ResearchEvidencePack | None,
) -> list[str]:
    if evidence_pack is None:
        return source_refs
    allowed = {
        ref
        for item in evidence_pack.items
        for ref in (item.source_ref, *item.lineage.source_refs)
    }
    normalized: list[str] = []
    for source_ref in source_refs:
        if source_ref in allowed:
            normalized.append(source_ref)
            continue
        matches = {
            item.source_ref
            for item in evidence_pack.items
            if any(
                source_ref == span_ref
                or source_ref.endswith(f"/{span_ref}")
                or source_ref.endswith(f"#{span_ref}")
                for span_ref in item.span_refs
            )
        }
        normalized.append(next(iter(matches)) if len(matches) == 1 else source_ref)
    return list(dict.fromkeys(normalized))


def _research_ordered_compression_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        records,
        key=lambda record: (
            str(record.get("record_ref", {}).get("ref", "")),
            str(record.get("activation_event_id", "")),
        ),
    )


def _transcript_from_events(
    run_id: str,
    events: list[HarnessEvent],
    *,
    metadata: dict[str, str] | None = None,
    research_rag_context: ResearchRAGContext | None = None,
    rag_context_pack: RAGContextPack | None = None,
    expected_graph_identity: ContextGraphIdentity | None = None,
) -> HarnessTranscript:
    entries = []
    for index, event in enumerate(events):
        entry = transcript_entry_from_event(event, phase_index=index)
        if metadata:
            entry = replace(
                entry,
                metadata={**entry.metadata, **metadata},
            )
        entries.append(entry)

    rag_projection = _validated_rag_transcript_projection(
        run_id=run_id,
        research_rag_context=research_rag_context,
        rag_context_pack=rag_context_pack,
        expected_graph_identity=expected_graph_identity,
    )
    if rag_projection is not None:
        matching_indexes = [
            index
            for index, entry in enumerate(entries)
            if entry.node_id == "run_research_rag"
            and entry.metadata.get("event_type")
            == HarnessEventType.GRAPH_WORKER_RESULT_RECORDED.value
        ]
        if len(matching_indexes) != 1:
            raise ValueError(
                "Research RAG transcript projection requires exactly one "
                "run_research_rag worker result"
            )
        entry_index = matching_indexes[0]
        entry = entries[entry_index]
        context_pack_id = rag_projection["context_pack_id"]
        transcript_ref = rag_projection["transcript_ref"]
        entries[entry_index] = replace(
            entry,
            rag_session_refs=(rag_projection["rag_session_ref"],),
            context_pack_refs=(context_pack_id,),
            output_refs=tuple(
                dict.fromkeys((*entry.output_refs, transcript_ref, context_pack_id))
            ),
            metadata={
                **entry.metadata,
                "parent_run_id": run_id,
                "parent_graph_id": rag_projection["graph_id"],
                "parent_graph_version": rag_projection["graph_version"],
                "parent_graph_ref": rag_projection["graph_ref"],
                "parent_graph_checksum": rag_projection["graph_checksum"],
                "parent_stage_id": rag_projection["stage_id"],
                **rag_projection["graph_identity"],
                "graph_identity": rag_projection["graph_identity"],
                "session_id": rag_projection["session_id"],
            },
        )

    transcript = HarnessTranscript(run_id)
    for entry in entries:
        transcript.append(entry)
    return transcript


def _validated_rag_transcript_projection(
    *,
    run_id: str,
    research_rag_context: ResearchRAGContext | None,
    rag_context_pack: RAGContextPack | None,
    expected_graph_identity: ContextGraphIdentity | None,
) -> dict[str, Any] | None:
    if research_rag_context is None and rag_context_pack is None:
        return None
    if research_rag_context is None or rag_context_pack is None:
        raise ValueError(
            "Research RAG context and context pack must be provided together"
        )
    if not isinstance(research_rag_context, ResearchRAGContext):
        raise TypeError("research_rag_context must be ResearchRAGContext")
    if not isinstance(rag_context_pack, RAGContextPack):
        raise TypeError("rag_context_pack must be RAGContextPack")
    if not isinstance(expected_graph_identity, ContextGraphIdentity):
        raise ValueError(
            "Research RAG transcript requires the expected Graph identity"
        )

    context_metadata = research_rag_context.metadata
    context_graph_identity = _required_rag_graph_identity(
        context_metadata,
        source="context",
    )
    pack_graph_identity = _required_rag_graph_identity(
        rag_context_pack.metadata,
        source="context_pack",
    )
    expected_identity = expected_graph_identity.to_dict()
    if context_graph_identity != expected_identity:
        raise ValueError("Research RAG transcript graph_identity identity mismatch")
    if pack_graph_identity != expected_identity:
        raise ValueError(
            "Research RAG transcript context_pack graph_identity identity mismatch"
        )
    identity = {
        field_name: _required_rag_identity(context_metadata, field_name)
        for field_name in (
            "session_id",
            "context_pack_id",
            "transcript_ref",
        )
    }
    if expected_graph_identity.run_id != run_id:
        raise ValueError("Research RAG transcript run_id identity mismatch")
    for field_name, expected_value in expected_identity.items():
        if _required_rag_identity(context_metadata, field_name) != expected_value:
            raise ValueError(
                f"Research RAG transcript {field_name} identity mismatch"
            )

    session_id = identity["session_id"]
    context_pack_id = identity["context_pack_id"]
    transcript_ref = identity["transcript_ref"]
    if context_pack_id != rag_context_pack.context_pack_id:
        raise ValueError("Research RAG transcript context_pack identity mismatch")
    if not _rag_ref_matches_session(
        context_pack_id,
        scheme="rag-context",
        session_id=session_id,
    ):
        raise ValueError("Research RAG transcript context_pack session mismatch")
    if not _rag_ref_matches_session(
        transcript_ref,
        scheme="rag-transcript",
        session_id=session_id,
    ):
        raise ValueError("Research RAG transcript child transcript session mismatch")

    return {
        **expected_identity,
        "graph_identity": expected_identity,
        "session_id": session_id,
        "context_pack_id": context_pack_id,
        "transcript_ref": transcript_ref,
        "rag_session_ref": f"rag-session://{session_id}",
    }


def _required_rag_graph_identity(
    metadata: Mapping[str, Any],
    *,
    source: str,
) -> dict[str, Any]:
    value = metadata.get("graph_identity")
    if not isinstance(value, Mapping):
        raise ValueError(
            f"Research RAG transcript {source} graph_identity is required"
        )
    try:
        return ContextGraphIdentity.from_dict(value).to_dict()
    except (TypeError, ValueError, HarnessValidationError) as exc:
        raise ValueError(
            f"Research RAG transcript {source} graph_identity is invalid"
        ) from exc


def _required_rag_identity(
    metadata: Mapping[str, Any],
    field_name: str,
) -> Any:
    value = metadata.get(field_name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(
            f"Research RAG transcript {field_name} identity is required"
        )
    if isinstance(value, str):
        return value.strip()
    if field_name == "activity_attempt":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(
                "Research RAG transcript activity_attempt identity is invalid"
            )
        return value
    raise ValueError(
        f"Research RAG transcript {field_name} identity must be text"
    )


def _rag_ref_matches_session(
    ref: str,
    *,
    scheme: str,
    session_id: str,
) -> bool:
    expected = f"{scheme}://{session_id}"
    return ref == expected or ref.startswith(f"{expected}/")


def _request_actor_metadata(request: AnalyzePaperRequest) -> dict[str, str]:
    metadata = {"memory_namespace": str(request.memory_namespace)}
    if request.tenant_id:
        metadata["tenant_id"] = request.tenant_id
    if request.user_id:
        metadata["user_id"] = request.user_id
    return metadata


def _gate_failures(events: list[HarnessEvent]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for event in events:
        payload = event.to_dict().get("payload", {})
        if isinstance(payload, dict) and payload.get("passed") is False:
            failures.append(payload)
    return failures


def _harness_result_has_execution_error(harness_result: Any) -> bool:
    worker_results = getattr(harness_result, "worker_results", None)
    if not isinstance(worker_results, Mapping):
        return False
    return any(
        isinstance(diagnostics, Mapping)
        and isinstance(diagnostics.get("execution_error_type"), str)
        and bool(diagnostics["execution_error_type"].strip())
        for worker_result in worker_results.values()
        if isinstance(worker_result, HarnessWorkerResult)
        for diagnostics in (worker_result.diagnostics,)
    )


__all__ = [
    "AnalyzePaperRequest",
    "ResearchAnalysisResult",
    "ResearchDynamicTaskPlanUnavailableError",
    "ResearchSinglePaperRuntime",
    "build_research_harness_run_spec",
]
