from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Condition, Lock
from typing import Any, Protocol
from urllib.parse import urlsplit

from backend.research.application.analyze_paper import AnalyzePaperUseCase
from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.catalog import ResearchPaperCatalogService
from backend.research.application.parse_paper import ParsePaperUseCase
from backend.research.application.bounded_document_rag import (
    BoundedDocumentRAGRuntime,
)
from backend.research.application.paper_rag_session import PaperRAGSession
from backend.research.application.rag_memory_admission import (
    HarnessResearchRAGMemoryAdmission,
)
from backend.research.application.research_input_contract import (
    research_input_quality_enabled,
)
from backend.research.application.run_disposition import (
    ResearchRunDispositionReconciler,
    classify_research_run_record,
)
from backend.research.application.single_paper_runtime import (
    AnalyzePaperRequest,
    ResearchAnalysisResult,
    ResearchPhysicalGraphRunner,
    ResearchSinglePaperRuntime,
    build_research_harness_run_spec,
)
from backend.research.graphs import (
    build_dynamic_paper_analysis_graph_definition,
    build_quality_dynamic_paper_analysis_graph_definition,
)
from backend.research.application.graph_result_committer import (
    ResearchGraphResultCommitter,
    ResearchTaskPlanResultMaterializer,
)
from backend.research.application.artifact_context import (
    ResearchGraphArtifactContextProvider,
)
from backend.research.application.graph_artifact_governance import (
    ResearchGraphArtifactGovernanceService,
)
from backend.research.application.reader_repair_runtime import (
    ReaderRepairGraphApplicationService,
)
from backend.research.document.chunk_storage import PaperChunkStoreAdapter
from backend.research.domain import (
    research_event_tenant_id,
    research_identity_scope_ref,
    research_subject_scope_ref,
)
from backend.research.document.cascade_parser import (
    CascadeArxivDocumentParser,
    CascadeDocumentParser,
    PyMuPDFTextDocumentParser,
    SelectableTextPdfDocumentParser,
)
from backend.research.document.recovery_policy import DocumentRecoveryPolicy
from backend.research.ports.document_parser import DocumentParserPort
from backend.research.document.format_router import MultiFormatDocumentParser
from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.latex_compiler import ArxivLatexDocumentCompiler
from backend.research.document.marker_pdf_parser import (
    MarkerPdfDocumentParser,
    MarkerReadingOrderProfile,
)
from backend.research.rag.adapters import PaperAnswerWorker
from backend.research.rag.adapters.memory_port import ResearchRAGMemoryPort
from backend.research.rag.retrieval.paper_answer_generator import AnswerGenerator
from backend.research.rag.retrieval.paper_retriever import ResearchRetriever
from backend.research.ports.artifact_publication import (
    RESEARCH_ARTIFACT_MANIFEST_VERSION,
    ResearchArtifactDiagnosticClaim,
    ResearchArtifactReadClaim,
    ResearchArtifactReadResolution,
)
from backend.research.ports.chunk_store import ChunkStorePort
from backend.research.ports.artifact_store import ResearchArtifactStorePort
from framework.llm.clients.openai_compatible import (
    LLMConfigurationError,
    LLMRetryPolicy,
    OpenAICompatibleClient,
    OpenAICompatibleConfig,
)
from framework.llm.clients.config import (
    OpenAICompatibleDeploymentConfig,
    load_openai_compatible_deployment,
)
from framework.events.canonical import checksum_for
from framework.events.application import DurableGraphEventProjectionAdapter
from framework.events.runtime.projection import CanonicalRuntimeEventPublisher
from framework.harness import (
    ContextAssembler,
    ContextEnvelope,
    ContextGraphIdentity,
    HarnessEvent,
    HarnessEventType,
    HarnessSideEffectDisposition,
    HarnessSideEffectOrigin,
    HarnessSideEffectOutcome,
    HarnessSideEffectStorePort,
    HarnessTrace,
    HarnessTranscript,
    HarnessWorkerResult,
    HarnessBudgetSnapshot,
    HarnessBudget,
    ResolvedSubAgentTaskAdapter,
    RAGSessionSpec,
    SubAgentRuntime,
    ChildAgentSupervisor,
    TaskPlanResultVerifier,
    subagent_attempt_evidence,
    DurableTaskPlanStore,
    transcript_entry_from_event,
)
from framework.harness.subagents import ChildAgentSupervisorError
from framework.harness.agent_loop.child_executor import HarnessSubAgentTaskExecutor
from framework.harness.subagents.agent_runner import (
    AuthorizedChildInputReaderPort,
    ChildAgentRunnerAdapter,
)
from framework.harness.subagents.execution import HarnessChildExecutionService
from framework.harness.subagents.execution_providers import (
    AdmissionChildToolEvidenceLimitsProvider,
    CanonicalChildBudgetTrackerProvider,
    CanonicalChildExecutionUsageMeter,
)
from framework.agent.models import AgentSpec
from framework.llm.budget import GlobalBudgetPolicy, GlobalBudgetTracker
from framework.llm.models import LLMRequest, LLMResponse
from framework.tool.registry.registry import ToolRegistry
from framework.harness.graph import HarnessWorkerType
from framework.harness.graph.compiler import HarnessGraphCompiler
from framework.harness.graph.bindings import HarnessWorkerBinding
from framework.harness.graph.model import HarnessContractKind, HarnessContractReference
from framework.harness.task_plan.stage_binding import TaskPlanStageBinding
from framework.harness.task_plan.capability import task_plan_context_identities
from framework.harness.task_plan.dependency_refs import AcceptedDependencyResultResolver
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.harness.task_plan.checkpoint import JsonlTaskPlanCheckpointStore
from framework.harness.control_plane.gates import GateContext
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_application import (
    HarnessGraphControlPlaneRuntime,
)
from framework.harness.control_plane.graph_runtime import initial_graph_state
from framework.harness.control_plane.state import run_spec_checksum
from framework.harness.graph.validation import HarnessGraphPreflightPolicy
from framework.harness.artifacts import GraphTerminalManifestV2
from framework.harness.runtime import (
    GraphArtifactRolloutMode,
    HarnessGraphResultRuntime,
    HarnessSubAgentResultAdapter,
    ResultMaterializer,
)
from framework.shared.time import utc_now
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.research.document_execution_adapter import ResearchParserExecutionAdapter
from backend.research.application.production_parsing_graph import (
    ProductionParsingGraphApplicationService,
)
from interfaces.composition.runtime_execution import (
    RESEARCH_MARKER_PROFILE_ID,
    build_research_execution_composition,
)
from framework.harness.ports import HarnessTransitionPort
from infrastructure.external.sources.arxiv import (
    ArxivConnector,
    ArxivSourceConnector,
)
from infrastructure.external.sources.fetch_policy import SourceFetchPolicy
from infrastructure.external.sources.github import GithubConnector
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.research.artifact_publication import ResearchArtifactBundleHandler
from infrastructure.research.candidate_worker import (
    StructuredResearchCandidateWorker,
)
from infrastructure.research.context_runtime import (
    build_research_context_assembler,
)
from infrastructure.research.document_compiler import (
    ResearchDocumentCompilerAdapter,
)
from infrastructure.research.filesystem_run_store import (
    FilesystemResearchRunStore,
    RESEARCH_RUN_RECORD_SCHEMA_VERSION,
    RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
)
from infrastructure.research.github_repository import (
    GithubResearchRepositoryAdapter,
)
from infrastructure.research.catalog_store import (
    FilesystemResearchCatalogStore,
    FilesystemResearchEventSink,
)
from infrastructure.research.source_resolver import ResearchSourceResolverAdapter
from infrastructure.research.local_chunk_store import LocalChunkPayloadStore
from infrastructure.research.source_provider import (
    ArxivResearchSourceProvider,
    CatalogBoundResearchSourceProvider,
)
from infrastructure.research.reader_repair_failure_diagnostic_side_effect import (
    ReaderRepairFailureDiagnosticSideEffectHandler,
)
from infrastructure.research.reader_repair_memory_side_effect import (
    ReaderRepairMemorySideEffectHandler,
)
from infrastructure.storage.events.factory import durable_event_storage_from_env
from infrastructure.storage.indexing import (
    GraphStorageIndexPublisher,
    LocalGraphStorageIndexStore,
)
from infrastructure.storage.harness import (
    FilesystemSubAgentTranscriptStore,
    SQLiteHarnessNodeOutputResource,
    SQLiteHarnessSideEffectStore,
)
from infrastructure.storage.conversation import LocalJsonConversationStore
from backend.research.graphs import (
    RESEARCH_DYNAMIC_CAPABILITIES,
    RESEARCH_DYNAMIC_INPUT_REFS,
    RESEARCH_DYNAMIC_STAGE_ID,
    RESEARCH_DYNAMIC_SUBAGENT_IDS,
    build_paper_analysis_gate_registry,
    build_research_analysis_capability_registry,
    build_research_analysis_task_gate_registry,
    build_research_analysis_task_plan_policy,
    ResearchAnalysisTaskPlanStageWorker,
    ResearchAnalysisPlanCandidateBuilder,
    build_research_analysis_subagent_specs,
)
from interfaces.composition.research_errors import (
    ResearchCapability,
    ResearchCompositionError,
    ResearchConfigurationError,
    ResearchRuntimeUnavailableError,
)
from interfaces.composition.research_settings import (
    ResearchLocalRuntimeSettings,
    ResearchParserSettings,
    ResearchRAGSettings,
    ResearchRuntimeSettings,
    desktop_research_llm_enabled,
)
from interfaces.composition.research_graph_artifacts import (
    compose_research_graph_artifact_runtime,
)
from interfaces.composition.research_child_runtime import (
    LazyResearchChildRuntime,
    ResearchChildRuntimeUnavailableError,
)
from interfaces.services.research_service import (
    ResearchApplicationService,
    ResearchRunRecord,
    ResearchServiceError,
)
from interfaces.services.source_runtime import SourceRuntimeProvider
from interfaces.services.reader_repair_factory import (
    build_reader_repair_failure_diagnostic_commit_port_from_env,
    build_reader_repair_memory_commit_port_from_env,
    build_reader_repair_memory_from_env,
)
from interfaces.services.harness_wait_runtime import (
    DurableHarnessWaitApprovalResolver,
    HarnessWaitRuntimeRegistry,
)
from interfaces.services.harness_wait_service import (
    HarnessWaitActorScope,
    HarnessWaitApplicationService,
    HarnessWaitApplicationError,
)
from interfaces.models.actor import ActorContext


@dataclass(frozen=True, slots=True)
class ResearchDynamicRolloutAdmissionReceipt:
    """Composition-authorized evidence for one dynamic Research admission."""

    run_id: str
    scope_ref: str
    policy_ref: str
    policy_checksum: str
    allowlist_evidence_ref: str
    golden_parity_evidence_ref: str
    quality_gate_evidence_ref: str
    dependency_provenance_ref: str
    golden_parity_passed: bool
    quality_gates_passed: bool
    dependency_provenance_passed: bool
    rollout_state: str = "ENABLED_PARALLEL"

    def __post_init__(self) -> None:
        values = {
            "run_id": self.run_id,
            "scope_ref": self.scope_ref,
            "policy_ref": self.policy_ref,
            "policy_checksum": self.policy_checksum,
            "allowlist_evidence_ref": self.allowlist_evidence_ref,
            "golden_parity_evidence_ref": self.golden_parity_evidence_ref,
            "quality_gate_evidence_ref": self.quality_gate_evidence_ref,
            "dependency_provenance_ref": self.dependency_provenance_ref,
        }
        if any(
            not isinstance(value, str) or not value.strip() or value != value.strip()
            for value in values.values()
        ):
            raise ValueError("dynamic Research rollout evidence fields must be non-blank text")
        if self.rollout_state != "ENABLED_PARALLEL":
            raise ValueError("dynamic Research admission must be ENABLED_PARALLEL")
        passed = {
            "golden_parity_passed": self.golden_parity_passed,
            "quality_gates_passed": self.quality_gates_passed,
            "dependency_provenance_passed": self.dependency_provenance_passed,
        }
        if any(type(value) is not bool for value in passed.values()):
            raise TypeError("dynamic Research rollout evidence decisions must be boolean")
        failed = sorted(name for name, value in passed.items() if not value)
        if failed:
            raise ValueError(
                "dynamic Research rollout evidence has not passed: "
                + ", ".join(failed)
            )


class ResearchDynamicRolloutAdmissionPort(Protocol):
    """Trusted composition authority for controlled dynamic Research rollout."""

    def admit(
        self,
        *,
        run_id: str,
        scope_ref: str,
        policy_ref: str,
        policy_checksum: str,
    ) -> ResearchDynamicRolloutAdmissionReceipt: ...


class _ProductionResearchAnalysisWorker:
    """Adapter that keeps dynamic task execution on existing Research logic."""

    worker_version = "1"
    worker_type = HarnessWorkerType.SUBAGENT

    def __init__(self, capability: str, *, dependencies: Any, workspace: Any) -> None:
        self.worker_id = capability
        self._dependencies = dependencies
        self._workspace = workspace
        self._methods = {
            "research.analysis.structure": ResearchSinglePaperRuntime._analyze_structure,
            "research.analysis.contribution": ResearchSinglePaperRuntime._analyze_contribution,
            "research.analysis.experiments": ResearchSinglePaperRuntime._analyze_experiments,
        }

    def execute(
        self,
        task: Mapping[str, Any],
        *,
        execution_identity: GraphExecutionIdentity | None = None,
    ) -> HarnessWorkerResult:
        if not isinstance(task, Mapping):
            raise HarnessValidationError(
                "Research dynamic child task must be a mapping",
                code="research_dynamic_child_input_invalid",
            )
        input_refs = task.get("input_refs")
        if (
            not isinstance(input_refs, (list, tuple))
            or any(not isinstance(ref, str) or not ref.strip() for ref in input_refs)
            or not set(RESEARCH_DYNAMIC_INPUT_REFS).issubset(input_refs)
        ):
            raise HarnessValidationError(
                "Research dynamic child requires the admitted document and evidence references",
                code="research_dynamic_child_input_refs_invalid",
                details={"required": list(RESEARCH_DYNAMIC_INPUT_REFS)},
            )
        expected_input_context = getattr(self._workspace, "input_context", None)
        supplied_prepared_input = task.get("prepared_input")
        if expected_input_context is None:
            if supplied_prepared_input is not None:
                raise HarnessValidationError(
                    "dynamic child supplied an unconfigured prepared input",
                    code="research_input_projection_unexpected",
                )
        elif (
            not isinstance(supplied_prepared_input, Mapping)
            or dict(supplied_prepared_input)
            != expected_input_context.candidate_projection()
        ):
            raise HarnessValidationError(
                "dynamic child prepared input differs from the accepted profile",
                code="research_input_projection_mismatch",
            )
        forbidden_context_fields = {
            "parent_messages",
            "raw_parent_messages",
            "private_notes",
            "conversation_history",
        }
        leaked_fields = sorted(forbidden_context_fields.intersection(task))
        if leaked_fields:
            raise HarnessValidationError(
                "Research dynamic child input contains private parent context",
                code="research_dynamic_child_private_context",
                details={"fields": leaked_fields},
            )
        method = self._methods.get(self.worker_id)
        if method is None:
            raise RuntimeError("Research dynamic capability binding is unavailable")
        return method(
            self._dependencies,
            task,
            self._workspace,
            execution_identity=execution_identity,
        )


class _ResearchDynamicInputReader(AuthorizedChildInputReaderPort):
    """Resolve only the document/evidence grant and admitted child outputs."""

    def __init__(self, *, workspace: Any, result_authority: HarnessResultRefAuthority) -> None:
        self._workspace = workspace
        self._result_authority = result_authority

    def read(self, grant: Any) -> Mapping[str, Any]:
        if getattr(grant, "attempt_identity", None) is None:
            raise HarnessValidationError(
                "dynamic child input grant has no attempt identity",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        descriptors = tuple(
            item for item in grant.descriptors
            if getattr(item, "ref_kind", None) != "memory"
        )
        refs = tuple(item.ref for item in descriptors)
        values: dict[str, Any] = {}
        for ref in refs:
            if ref == "document":
                document = getattr(self._workspace, "document", None)
                if document is None:
                    raise HarnessValidationError(
                        "dynamic child document input is unavailable",
                        code="REF_UNRESOLVED",
                    )
                values[ref] = document.to_dict()
            elif ref == "evidence_pack":
                evidence_pack = getattr(self._workspace, "evidence_pack", None)
                if evidence_pack is None:
                    raise HarnessValidationError(
                        "dynamic child evidence input is unavailable",
                        code="REF_UNRESOLVED",
                    )
                values[ref] = evidence_pack.to_dict()

        # Dependency refs are already rewritten to Harness-issued result refs
        # in the child grant. Resolve them through the result authority and
        # expose only those exact descriptors to AgentRunner. The logical ref
        # remains inside the value envelope so the Research worker can address
        # the public dependency by the name admitted in the candidate plan.
        if getattr(grant, "dependency_bindings", ()):
            outputs = self._result_authority.read_dependency_outputs(
                grant.attempt_identity,
                input_refs=refs,
            )
            for binding in grant.dependency_bindings:
                if binding.logical_ref not in outputs:
                    raise HarnessValidationError(
                        "dynamic child dependency output is unavailable",
                        code="REF_UNRESOLVED",
                    )
                values[binding.descriptor.ref] = {
                    "logical_ref": binding.logical_ref,
                    "output": outputs[binding.logical_ref],
                }
        if set(values) != set(refs):
            raise HarnessValidationError(
                "dynamic child input reader returned values outside its grant",
                code="REF_SNAPSHOT_BINDING_MISMATCH",
            )
        return values


class _PreparedResearchCandidateWorker:
    """Inject one application-validated projection into real provider requests."""

    def __init__(self, worker: Any, *, workspace: Any) -> None:
        self._worker = worker
        self._workspace = workspace

    def generate_candidate(
        self,
        *,
        task: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        context = self._workspace.input_context
        prepared_payload = dict(payload)
        if context is not None:
            prepared_payload["prepared_input"] = context.candidate_projection()
        return self._worker.generate_candidate(
            task=task,
            payload=prepared_payload,
            **kwargs,
        )


class _ResearchCandidateLLMClient:
    """Adapt the existing Research candidate worker to the AgentRunner port.

    AgentRunner remains the physical child execution boundary.  This adapter
    only translates its final action request into the existing structured
    candidate operation; it cannot route, gate, publish, or mutate memory.
    """

    def __init__(self, worker: _ProductionResearchAnalysisWorker) -> None:
        self._worker = worker

    def complete(self, request: LLMRequest) -> LLMResponse:
        if not isinstance(request, LLMRequest):
            raise TypeError("request must be LLMRequest")
        user_message = request.messages[-1].get("content", "")
        if not isinstance(user_message, str) or "Inputs:" not in user_message:
            raise HarnessValidationError(
                "dynamic child AgentRunner request is missing its input envelope",
                code="subagent_runner_input_resolution_invalid",
            )
        try:
            encoded = (
                user_message.split("Inputs:", 1)[1]
                .split("\nPrepared input projection JSON:", 1)[0]
                .split("\nMemory context:", 1)[0]
                .strip()
            )
            inputs = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HarnessValidationError(
                "dynamic child AgentRunner input envelope is invalid",
                code="subagent_runner_input_resolution_invalid",
            ) from exc
        if not isinstance(inputs, Mapping):
            raise HarnessValidationError(
                "dynamic child AgentRunner inputs must be an object",
                code="subagent_runner_input_resolution_invalid",
            )
        input_refs = inputs.get("input_refs")
        if not isinstance(input_refs, list | tuple):
            raise HarnessValidationError(
                "dynamic child AgentRunner input refs are missing",
                code="subagent_runner_input_resolution_invalid",
            )
        dependency_outputs = {
            key: value
            for key, value in inputs.items()
            if key
            not in {
                "objective",
                "input_refs",
                "resolved_inputs",
                "research_input",
            }
        }
        resolved = inputs.get("resolved_inputs")
        if isinstance(resolved, Mapping):
            for key, value in resolved.items():
                if key in {"document", "evidence_pack"}:
                    continue
                if (
                    not isinstance(value, Mapping)
                    or set(value) != {"logical_ref", "output"}
                    or not isinstance(value.get("logical_ref"), str)
                    or not value["logical_ref"].strip()
                ):
                    raise HarnessValidationError(
                        "dynamic child dependency input envelope is invalid",
                        code="subagent_runner_input_resolution_invalid",
                    )
                logical_ref = value["logical_ref"]
                if logical_ref in dependency_outputs:
                    raise HarnessValidationError(
                        "dynamic child dependency input is duplicated",
                        code="subagent_runner_input_resolution_invalid",
                    )
                dependency_outputs[logical_ref] = value["output"]
        task: dict[str, Any] = {"input_refs": list(input_refs)}
        prepared_input = inputs.get("research_input")
        if prepared_input is not None:
            if not isinstance(prepared_input, Mapping):
                raise HarnessValidationError(
                    "dynamic child prepared input envelope is invalid",
                    code="research_input_projection_invalid",
                )
            task["prepared_input"] = dict(prepared_input)
        if dependency_outputs:
            task["dependency_outputs"] = dependency_outputs
        result = self._worker.execute(
            task,
            execution_identity=request.execution_identity,
        )
        if not isinstance(result, HarnessWorkerResult) or result.status.value != "succeeded":
            raise HarnessValidationError(
                "dynamic Research candidate worker did not produce a candidate",
                code="subagent_worker_execution_failed",
            )
        content = json.dumps(
            {"action_type": "final_output", "output": dict(result.output)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return LLMResponse(content=content, execution_identity=request.execution_identity)


def _dynamic_research_gate_context(
    workspace: Any,
    worker_result: HarnessWorkerResult,
) -> GateContext:
    run_spec = build_research_harness_run_spec(
        workspace.request,
        created_at=utc_now(),
    )
    compiled_graph = HarnessGraphCompiler().compile(run_spec.graph).graph
    graph_state = initial_graph_state(
        run_spec,
        compiled_graph,
        HarnessGraphPreflightPolicy(),
        run_spec_checksum=run_spec_checksum(run_spec),
    )
    step_spec = next(
        item for item in run_spec.graph.activities
        if item.step_id == "dynamic_analysis_stage"
    )
    return GateContext(
        run_spec=run_spec,
        graph_state=graph_state,
        step_spec=step_spec,
        outputs={
            "document": {"document": workspace.document.to_dict()},
            "evidence_pack": {"evidence_pack": workspace.evidence_pack.to_dict()},
        },
        worker_result=worker_result,
        budget=HarnessBudgetSnapshot.from_budget(HarnessBudget.safe_default()),
    )


def _admit_dynamic_research_rollout(
    authority: ResearchDynamicRolloutAdmissionPort | None,
    *,
    run_id: str,
    scope_ref: str,
    policy: Any,
) -> ResearchDynamicRolloutAdmissionReceipt:
    if authority is None:
        raise HarnessValidationError(
            "dynamic Research rollout is disabled until release evidence is admitted",
            code="research_dynamic_rollout_disabled",
            details={"composition_state": "FEATURE_DISABLED"},
        )
    admit = getattr(authority, "admit", None)
    if not callable(admit):
        raise TypeError("dynamic_rollout_admission must implement admit")
    receipt = admit(
        run_id=run_id,
        scope_ref=scope_ref,
        policy_ref=policy.exact_ref,
        policy_checksum=policy.policy_checksum,
    )
    if not isinstance(receipt, ResearchDynamicRolloutAdmissionReceipt):
        raise HarnessValidationError(
            "dynamic Research rollout authority returned an invalid receipt",
            code="research_dynamic_rollout_evidence_invalid",
        )
    if (
        receipt.run_id != run_id
        or receipt.scope_ref != scope_ref
        or receipt.policy_ref != policy.exact_ref
        or receipt.policy_checksum != policy.policy_checksum
    ):
        raise HarnessValidationError(
            "dynamic Research rollout receipt does not match this admission",
            code="research_dynamic_rollout_evidence_mismatch",
        )
    return receipt


_RESEARCH_EVENT_TENANT_ID = "research-runtime"


class _ResearchHarnessWaitActorScopeResolver:
    """Derive Wait scope only from authenticated actor metadata."""

    def resolve(self, actor: ActorContext) -> HarnessWaitActorScope:
        if not isinstance(actor, ActorContext):
            raise TypeError("actor must be ActorContext")
        metadata = actor.metadata if isinstance(actor.metadata, Mapping) else {}
        scope = AskPaperUseCase().resolve_actor_scope(
            tenant_id=metadata.get("tenant_id"),
            user_id=metadata.get("user_id"),
            memory_namespace=metadata.get("memory_namespace"),
        )
        scope_ref = research_identity_scope_ref(scope.to_metadata())
        actor_ref = checksum_for(
            {
                "actor_id": actor.actor_id,
                "tenant_scope_ref": scope_ref,
                "identity_scope_ref": scope_ref,
            }
        )
        return HarnessWaitActorScope(
            tenant_scope_ref=scope_ref,
            identity_scope_ref=scope_ref,
            actor_identity_scope_ref=actor_ref,
        )


class ResearchRuntimeComposition:
    """Own one composed Research service and its process-scoped resources."""

    __slots__ = (
        "_availability_error",
        "_close_lock",
        "_closed",
        "_execution_composition",
        "_graph_artifact_governance_service",
        "_harness_wait_approval_resolver",
        "_harness_wait_actor_scope_resolver",
        "_harness_wait_runtime_registry",
        "_reader_repair_service",
        "_production_parsing_evaluation_service",
        "_runtime_event_publisher",
        "_resources",
        "_service",
        "_settings",
        "_subagent_transcript_store",
        "_source_runtime_provider",
    )

    def __init__(
        self,
        *,
        settings: ResearchRuntimeSettings | ResearchLocalRuntimeSettings | None,
        service: ResearchApplicationService,
        source_runtime_provider: SourceRuntimeProvider,
        resources: Iterable[Any] = (),
        availability_error: ResearchCompositionError | None = None,
        graph_artifact_governance_service: (
            ResearchGraphArtifactGovernanceService | None
        ) = None,
        reader_repair_service: ReaderRepairGraphApplicationService | None = None,
        production_parsing_evaluation_service: (
            ProductionParsingGraphApplicationService | None
        ) = None,
        harness_wait_runtime_registry: HarnessWaitRuntimeRegistry | None = None,
        harness_wait_approval_resolver: DurableHarnessWaitApprovalResolver | None = None,
        harness_wait_actor_scope_resolver: _ResearchHarnessWaitActorScopeResolver | None = None,
        runtime_event_publisher: CanonicalRuntimeEventPublisher | None = None,
        execution_composition: Any | None = None,
        subagent_transcript_store: Any | None = None,
    ) -> None:
        if settings is not None and not isinstance(
            settings, (ResearchRuntimeSettings, ResearchLocalRuntimeSettings)
        ):
            raise TypeError("settings must be ResearchRuntimeSettings or ResearchLocalRuntimeSettings")
        if not isinstance(service, ResearchApplicationService):
            raise TypeError("service must be ResearchApplicationService")
        if not isinstance(source_runtime_provider, SourceRuntimeProvider):
            raise TypeError("source_runtime_provider must be SourceRuntimeProvider")
        if availability_error is not None and not isinstance(
            availability_error,
            ResearchCompositionError,
        ):
            raise TypeError("availability_error must be ResearchCompositionError")
        if settings is None and availability_error is None:
            raise ValueError("available Research composition requires settings")
        if graph_artifact_governance_service is not None and not isinstance(
            graph_artifact_governance_service,
            ResearchGraphArtifactGovernanceService,
        ):
            raise TypeError(
                "graph_artifact_governance_service must be "
                "ResearchGraphArtifactGovernanceService"
            )
        if reader_repair_service is not None and not isinstance(
            reader_repair_service,
            ReaderRepairGraphApplicationService,
        ):
            raise TypeError(
                "reader_repair_service must be "
                "ReaderRepairGraphApplicationService"
            )

        if subagent_transcript_store is not None and not getattr(
            subagent_transcript_store,
            "is_durable",
            False,
        ):
            raise TypeError(
                "subagent_transcript_store must be a durable transcript store"
            )
        if runtime_event_publisher is not None and not isinstance(
            runtime_event_publisher,
            CanonicalRuntimeEventPublisher,
        ):
            raise TypeError(
                "runtime_event_publisher must be CanonicalRuntimeEventPublisher"
            )

        unique_resources: list[Any] = []
        seen: set[int] = set()
        for resource in resources:
            if resource is None or id(resource) in seen:
                continue
            seen.add(id(resource))
            unique_resources.append(resource)

        self._settings = settings
        self._execution_composition = execution_composition
        self._service = service
        self._source_runtime_provider = source_runtime_provider
        self._subagent_transcript_store = subagent_transcript_store
        self._resources = tuple(unique_resources)
        self._availability_error = availability_error
        self._graph_artifact_governance_service = (
            graph_artifact_governance_service
        )
        self._reader_repair_service = reader_repair_service
        self._production_parsing_evaluation_service = (
            production_parsing_evaluation_service
        )
        self._harness_wait_runtime_registry = harness_wait_runtime_registry
        self._harness_wait_approval_resolver = harness_wait_approval_resolver
        self._harness_wait_actor_scope_resolver = harness_wait_actor_scope_resolver
        self._runtime_event_publisher = runtime_event_publisher
        self._close_lock = Lock()
        self._closed = False

    @property
    def settings(self) -> ResearchRuntimeSettings | ResearchLocalRuntimeSettings | None:
        return self._settings

    @property
    def service(self) -> ResearchApplicationService:
        return self._service

    @property
    def source_runtime_provider(self) -> SourceRuntimeProvider:
        return self._source_runtime_provider

    @property
    def subagent_transcript_store(self) -> Any | None:
        """Durable transcript adapter owned by this process composition."""

        return self._subagent_transcript_store

    @property
    def execution_composition(self) -> Any | None:
        """Process-scoped execution composition used by external adapters."""

        return self._execution_composition

    @property
    def runtime_event_publisher(self) -> CanonicalRuntimeEventPublisher | None:
        """Canonical durable runtime event owner shared by wait and child paths."""

        return self._runtime_event_publisher

    @property
    def resources(self) -> tuple[Any, ...]:
        return self._resources

    @property
    def availability_error(self) -> ResearchCompositionError | None:
        return self._availability_error

    @property
    def graph_artifact_governance_service(
        self,
    ) -> ResearchGraphArtifactGovernanceService | None:
        return self._graph_artifact_governance_service

    @property
    def reader_repair_service(
        self,
    ) -> ReaderRepairGraphApplicationService | None:
        return self._reader_repair_service

    @property
    def production_parsing_evaluation_service(
        self,
    ) -> ProductionParsingGraphApplicationService | None:
        """Composition-owned deterministic parse-only Harness service."""

        return self._production_parsing_evaluation_service

    def harness_wait_service_factory(
        self,
    ) -> Callable[[ActorContext], HarnessWaitApplicationService] | None:
        if (
            self._harness_wait_runtime_registry is None
            or self._harness_wait_approval_resolver is None
            or self._harness_wait_actor_scope_resolver is None
        ):
            return None

        def factory(actor: ActorContext) -> HarnessWaitApplicationService:
            return HarnessWaitApplicationService(
                actor=actor,
                runtime_resolver=self._harness_wait_runtime_registry,
                actor_scope_resolver=self._harness_wait_actor_scope_resolver,
                approval_resolver=self._harness_wait_approval_resolver,
                runtime_event_sink=self._runtime_event_publisher,
            )

        return factory

    @property
    def available(self) -> bool:
        return self._availability_error is None

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True

        first_error: Exception | None = None
        for resource in reversed(self._resources):
            close = (
                resource.shutdown
                if isinstance(resource, ChildAgentSupervisor)
                else getattr(resource, "close", None)
            )
            if not callable(close):
                close = getattr(resource, "shutdown", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:  # close every owned resource before reporting
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


class _PaperRagUseCaseProvider:
    """Own the chunk-RAG service under the Research composition lifecycle."""

    __slots__ = (
        "_ask_use_case",
        "_closed",
        "_lock",
        "_runtime_resources",
        "_runtime_resources_factory",
        "_service_factory",
        "_service",
    )

    def __init__(
        self,
        ask_use_case: AskPaperUseCase,
        *,
        runtime_resources_factory: Callable[[], Any] | None = None,
        service_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._ask_use_case = ask_use_case
        self._runtime_resources_factory = runtime_resources_factory
        self._service_factory = service_factory
        self._closed = False
        self._lock = Lock()
        self._runtime_resources: Any | None = None
        self._service: Any | None = None

    def rag_ask(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._get().rag_ask(*args, **kwargs)

    def recover_gated_answer(self, **kwargs: Any) -> dict[str, Any] | None:
        return self._get().recover_gated_answer(**kwargs)

    def get_reranker(self) -> Any:
        return self._get().get_reranker()

    def preload_reranker(self) -> None:
        self._get().preload_reranker()

    def runtime_resources(self) -> Any:
        self._get()
        with self._lock:
            if self._closed or self._runtime_resources is None:
                raise RuntimeError("Research chunk RAG provider is closed")
            return self._runtime_resources

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            service = self._service
            self._service = None
            runtime_resources = self._runtime_resources
            self._runtime_resources = None

        first_error: Exception | None = None
        for resource in (service, runtime_resources):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _get(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("Research chunk RAG provider is closed")
            if self._service is None:
                from interfaces.services.paper_rag_service import (
                    PaperRagApplicationService,
                )
                from interfaces.services.paper_rag_factory import (
                    PaperRagRuntimeResources,
                )

                if self._service_factory is not None:
                    service = self._service_factory()
                    if not isinstance(service, PaperRagApplicationService):
                        raise TypeError("service_factory must return PaperRagApplicationService")
                    runtime_resources = None
                else:
                    runtime_resources = (
                        self._runtime_resources_factory()
                        if self._runtime_resources_factory is not None
                        else PaperRagRuntimeResources()
                    )
                    if not isinstance(runtime_resources, PaperRagRuntimeResources):
                        raise TypeError("runtime_resources_factory must return PaperRagRuntimeResources")
                    try:
                        service = PaperRagApplicationService(
                            ask_use_case=self._ask_use_case,
                            runtime_resources=runtime_resources,
                        )
                    except BaseException:
                        _close_quietly(runtime_resources)
                        raise
                self._runtime_resources = runtime_resources
                self._service = service
            return self._service


SettingsFactory = Callable[[], ResearchRuntimeSettings]
ConfiguredCompositionFactory = Callable[
    [ResearchRuntimeSettings, SourceRuntimeProvider],
    ResearchRuntimeComposition,
]
CompositionFactory = Callable[[], ResearchRuntimeComposition]


def build_research_runtime_composition(
    *,
    settings: ResearchRuntimeSettings | None = None,
    settings_factory: SettingsFactory | None = None,
    source_runtime_provider: SourceRuntimeProvider | None = None,
    configured_factory: ConfiguredCompositionFactory | None = None,
    dynamic_rollout_admission: ResearchDynamicRolloutAdmissionPort | None = None,
) -> ResearchRuntimeComposition:
    if settings is not None and settings_factory is not None:
        raise ValueError("settings and settings_factory are mutually exclusive")
    provider = source_runtime_provider or SourceRuntimeProvider()

    # Explicit desktop disablement is process-owned authorization. Do not enter
    # the remote settings/factory path, even if credentials were inherited.
    try:
        if desktop_research_llm_enabled() is False:
            return _build_local_only_composition(provider)
    except ResearchCompositionError as exc:
        return _unavailable_composition(
            exc, settings=None, source_runtime_provider=provider,
        )

    actual_settings = settings
    if actual_settings is None:
        try:
            actual_settings = (settings_factory or ResearchRuntimeSettings.from_env)()
        except ResearchCompositionError as exc:
            return _unavailable_composition(
                exc,
                settings=None,
                source_runtime_provider=provider,
            )

    try:
        if configured_factory is None:
            composition = _build_configured_composition(
                actual_settings,
                provider,
                dynamic_rollout_admission=dynamic_rollout_admission,
            )
        else:
            if dynamic_rollout_admission is not None:
                raise ValueError(
                    "dynamic_rollout_admission is owned by the configured composition"
                )
            composition = configured_factory(actual_settings, provider)
    except ResearchCompositionError as exc:
        return _unavailable_composition(
            exc,
            settings=actual_settings,
            source_runtime_provider=provider,
        )
    if not isinstance(composition, ResearchRuntimeComposition):
        raise TypeError("configured_factory must return ResearchRuntimeComposition")
    if composition.settings != actual_settings:
        raise ValueError("configured_factory returned a composition for different settings")
    if composition.source_runtime_provider is not provider:
        raise ValueError(
            "configured_factory must use the supplied SourceRuntimeProvider"
        )
    return composition


class ResearchRuntimeProvider:
    """Lazily cache one explicit Research composition for an owning process."""

    def __init__(
        self,
        factory: CompositionFactory | None = None,
        *,
        settings_factory: SettingsFactory | None = None,
        configured_factory: ConfiguredCompositionFactory | None = None,
        source_runtime_provider: SourceRuntimeProvider | None = None,
    ) -> None:
        if factory is not None and (
            settings_factory is not None or configured_factory is not None
        ):
            raise ValueError(
                "factory cannot be combined with settings_factory or configured_factory"
            )
        self._source_runtime_provider = source_runtime_provider or SourceRuntimeProvider()
        self._factory = factory or (
            lambda: build_research_runtime_composition(
                settings_factory=settings_factory,
                source_runtime_provider=self._source_runtime_provider,
                configured_factory=configured_factory,
            )
        )
        self._composition: ResearchRuntimeComposition | None = None
        self._condition = Condition(Lock())
        self._transitioning = False
        self._closed = False

    @property
    def source_runtime_provider(self) -> SourceRuntimeProvider:
        return self._source_runtime_provider

    @property
    def initialized(self) -> bool:
        with self._condition:
            self._wait_for_transition()
            return self._composition is not None

    @property
    def closed(self) -> bool:
        with self._condition:
            self._wait_for_transition()
            return self._closed

    def get(self) -> ResearchRuntimeComposition:
        with self._condition:
            self._wait_for_transition()
            if self._closed:
                raise RuntimeError("ResearchRuntimeProvider is closed")
            if self._composition is not None:
                return self._composition
            self._transitioning = True

        try:
            composition = self._factory()
            if not isinstance(composition, ResearchRuntimeComposition):
                raise TypeError("factory must return ResearchRuntimeComposition")
            if composition.source_runtime_provider is not self._source_runtime_provider:
                raise ValueError(
                    "factory must use the provider's SourceRuntimeProvider"
                )
        except BaseException:
            with self._condition:
                self._transitioning = False
                self._condition.notify_all()
            raise

        with self._condition:
            self._composition = composition
            self._transitioning = False
            self._condition.notify_all()
            return composition

    def service_factory(self) -> ResearchApplicationService:
        return self.get().service

    def reset(self) -> None:
        with self._condition:
            self._wait_for_transition()
            self._transitioning = True
            composition = self._composition
            self._composition = None
        try:
            if composition is not None:
                composition.close()
        finally:
            with self._condition:
                self._closed = False
                self._transitioning = False
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._wait_for_transition()
            if self._closed:
                return
            self._transitioning = True
            composition = self._composition
            self._composition = None
        try:
            if composition is not None:
                composition.close()
        finally:
            with self._condition:
                self._closed = True
                self._transitioning = False
                self._condition.notify_all()

    def _wait_for_transition(self) -> None:
        while self._transitioning:
            self._condition.wait()


def default_research_runtime_provider() -> ResearchRuntimeProvider:
    return _DEFAULT_RESEARCH_RUNTIME_PROVIDER


def default_source_runtime_provider() -> SourceRuntimeProvider:
    """Return the Source provider owned by the default Research process root."""

    return _DEFAULT_RESEARCH_RUNTIME_PROVIDER.source_runtime_provider


def build_research_application_service(
    *,
    source_runtime_provider: SourceRuntimeProvider | None = None,
) -> ResearchApplicationService:
    """Resolve Research through its composition root.

    Entry-point compositions may supply their already-owned Source provider so
    Research observes the same source policy and quota state.
    """

    if (
        source_runtime_provider is None
        or source_runtime_provider is default_source_runtime_provider()
    ):
        return _DEFAULT_RESEARCH_RUNTIME_PROVIDER.service_factory()
    return ResearchRuntimeProvider(
        source_runtime_provider=source_runtime_provider,
    ).service_factory()


def build_default_harness_wait_service(
    actor: ActorContext,
) -> HarnessWaitApplicationService:
    """Resolve the composition-owned Wait service lazily for the production app."""

    composition = _DEFAULT_RESEARCH_RUNTIME_PROVIDER.get()
    factory = composition.harness_wait_service_factory()
    if factory is None:
        raise HarnessWaitApplicationError(
            "Research Harness Wait capability is unavailable",
            code="wait_runtime_resolver_missing",
        )
    return factory(actor)


def reset_default_research_runtime() -> None:
    _DEFAULT_RESEARCH_RUNTIME_PROVIDER.reset()


def close_default_research_runtime() -> None:
    _DEFAULT_RESEARCH_RUNTIME_PROVIDER.close()


def get_default_research_reranker() -> Any:
    provider = _default_paper_rag_use_case_provider()
    return provider.get_reranker()


def get_default_paper_rag_runtime_resources() -> Any:
    provider = _default_paper_rag_use_case_provider()
    return provider.runtime_resources()


def preload_default_research_reranker() -> None:
    provider = _default_paper_rag_use_case_provider()
    provider.preload_reranker()


def _default_paper_rag_use_case_provider() -> _PaperRagUseCaseProvider:
    rag_use_case = _DEFAULT_RESEARCH_RUNTIME_PROVIDER.service_factory()._rag_ask_use_case
    if not isinstance(rag_use_case, _PaperRagUseCaseProvider):
        raise RuntimeError(
            "default Research composition does not own a Paper RAG provider"
        )
    return rag_use_case


class _UnavailableAnalyzeUseCase:
    def __init__(self, error: ResearchCompositionError) -> None:
        self._error = error

    def analyze(self, _request: Any) -> Any:
        raise _research_service_error(self._error)


class _UnavailableResearchRunStore:
    def __init__(self, error: ResearchCompositionError) -> None:
        self._error = error

    def save(self, _record: ResearchRunRecord) -> None:
        raise _research_service_error(self._error)

    def get_by_run_id(self, _run_id: str) -> ResearchRunRecord | None:
        raise _research_service_error(self._error)

    def get_latest_by_paper_id(self, _paper_id: str) -> ResearchRunRecord | None:
        raise _research_service_error(self._error)

    def list_by_paper_id(self, _paper_id: str) -> list[ResearchRunRecord]:
        raise _research_service_error(self._error)


class _DurableResearchRunRecoverySource:
    """Rebuild a missing run disposition from immutable publication evidence."""

    def __init__(
        self,
        *,
        artifact_port: FilesystemHarnessArtifactPort,
        run_store: Any,
        side_effect_store: HarnessSideEffectStorePort,
        scoped_event_port_factory: Callable[
            [str, Mapping[str, Any]],
            HarnessTransitionPort,
        ],
    ) -> None:
        if not isinstance(artifact_port, FilesystemHarnessArtifactPort):
            raise TypeError("artifact_port must be FilesystemHarnessArtifactPort")
        if not isinstance(side_effect_store, HarnessSideEffectStorePort):
            raise TypeError("side_effect_store must implement HarnessSideEffectStorePort")
        if not callable(scoped_event_port_factory):
            raise TypeError("scoped_event_port_factory must be callable")
        self._artifact_root = Path(artifact_port.root)
        self._manifest_reader = artifact_port.read_terminal_manifest
        self._run_store = run_store
        self._side_effect_store = side_effect_store
        self._scoped_event_port_factory = scoped_event_port_factory
        # This port is retained only by the recovery reader.  Its resolver is
        # deliberately diagnostic: normal artifact reads remain bound to the
        # accepted run resolver installed on the production port.
        self._diagnostic_artifact_reader = FilesystemHarnessArtifactPort(
            artifact_port.root,
            artifact_manager=artifact_port.manager,
            artifact_store=artifact_port.store,
            terminal_store=artifact_port.terminal_store,
            max_write_bytes=artifact_port.max_write_bytes,
            accepted_run_resolver=lambda *_args: True,
        )

    def list_pending_run_ids(self, *, limit: int) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        try:
            candidates = sorted(self._artifact_root.iterdir(), key=lambda path: path.name)
        except FileNotFoundError:
            return ()
        pending: list[str] = []
        for candidate in candidates:
            if len(pending) >= limit:
                break
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            try:
                manifest = self._manifest_reader(candidate.name)
            except FileNotFoundError:
                continue
            if not _is_recoverable_research_manifest(manifest):
                continue
            if self._run_store.get_by_run_id(candidate.name) is None:
                pending.append(candidate.name)
        return tuple(pending)

    def load_recovery_record(self, run_id: str) -> ResearchRunRecord | None:
        manifest = self._manifest_reader(run_id)
        if not _is_recoverable_research_manifest(manifest):
            return None

        publication = manifest.publication
        if publication is None:
            raise ValueError("Research recovery manifest has no publication evidence")
        identity_scope_ref = publication.identity_scope_ref
        subject_scope_ref = publication.subject_scope_ref
        authority_ref = publication.publication_authority_ref
        artifact_evidence_ref = publication.artifact_evidence_ref
        outcome_ref = publication.terminal_side_effect_outcome_ref
        raw_outcome = publication.metadata.get("terminal_side_effect_outcome")
        if not isinstance(raw_outcome, Mapping):
            raise ValueError("Research recovery manifest has no terminal outcome")
        outcome = HarnessSideEffectOutcome.from_dict(raw_outcome)
        if (
            outcome.checksum != outcome_ref
            or outcome.run_id != run_id
            or outcome.decision_ref != authority_ref
            or outcome.identity_scope_ref != identity_scope_ref
            or outcome.subject_scope_ref != subject_scope_ref
            or outcome.disposition is not HarnessSideEffectDisposition.ACCEPTED
        ):
            raise ValueError("Research recovery terminal outcome conflicts with manifest")

        decision = self._side_effect_store.get_decision(authority_ref)
        if (
            decision is None
            or decision.checksum != authority_ref
            or decision.run_id != run_id
            or decision.origin is not HarnessSideEffectOrigin.CONTROLLER_TERMINAL
            or decision.identity_scope_ref != identity_scope_ref
            or decision.subject_scope_ref != subject_scope_ref
            or decision.disposition is not HarnessSideEffectDisposition.ACCEPTED
        ):
            raise ValueError("Research recovery publication decision is unavailable")
        stored_outcome = self._side_effect_store.get_outcome(
            effect_id=outcome.effect_id,
            identity_scope_ref=identity_scope_ref,
            subject_scope_ref=subject_scope_ref,
            idempotency_key=outcome.idempotency_key,
        )
        if stored_outcome is None or stored_outcome.checksum != outcome_ref:
            raise ValueError("Research recovery publication outcome is unavailable")

        artifact_refs = _recovery_artifact_refs(outcome, run_id=run_id)
        if checksum_for({"artifact_refs": artifact_refs}) != artifact_evidence_ref:
            raise ValueError("Research recovery artifact evidence checksum is invalid")
        payloads = {
            artifact_type: self._read_recovery_artifact(
                artifact_type=artifact_type,
                ref=ref,
            )
            for artifact_type, ref in artifact_refs.items()
        }
        analysis = _required_artifact_payload(payloads, "research-analysis")
        quality = _required_artifact_payload(payloads, "research-quality-result")
        published_trace = HarnessTrace.from_dict(
            _required_artifact_payload(payloads, "harness-trace")
        )
        published_transcript = HarnessTranscript.from_dict(
            _required_artifact_payload(payloads, "harness-transcript")
        )
        paper_id = _required_text(analysis.get("paper_id"), "Research paper id")
        if quality.get("target_id") != paper_id:
            raise ValueError("Research recovery quality target conflicts with paper")

        actor_metadata = _recovery_actor_metadata(published_trace.metadata)
        if research_identity_scope_ref(actor_metadata) != identity_scope_ref:
            raise ValueError("Research recovery actor scope conflicts with publication")
        if research_subject_scope_ref(paper_id) != subject_scope_ref:
            raise ValueError("Research recovery paper scope conflicts with publication")
        for artifact_type, envelope in payloads.items():
            metadata = envelope.get("metadata")
            if not isinstance(metadata, Mapping):
                raise ValueError(
                    f"Research recovery artifact metadata is invalid: {artifact_type}"
                )
            if research_identity_scope_ref(metadata) != identity_scope_ref:
                raise ValueError(
                    f"Research recovery artifact scope conflicts: {artifact_type}"
                )

        event_port = self._scoped_event_port_factory(run_id, actor_metadata)
        history = event_port.read_history(run_id)
        if not isinstance(history, tuple) or not all(
            isinstance(event, HarnessEvent) and event.run_id == run_id
            for event in history
        ):
            raise ValueError("Research recovery event history is invalid")
        cutoff = _required_text(
            outcome.metadata.get("history_cutoff"),
            "Research terminal history cutoff",
        )
        cutoff_indexes = [
            index for index, event in enumerate(history) if event.event_id == cutoff
        ]
        if len(cutoff_indexes) != 1:
            raise ValueError("Research recovery history cutoff is not unique")
        cutoff_index = cutoff_indexes[0]
        if tuple(published_trace.events) != history[: cutoff_index + 1]:
            raise ValueError("Research published trace conflicts with durable history")
        if [entry.entry_id for entry in published_transcript.entries()] != [
            event.event_id for event in history[: cutoff_index + 1]
        ]:
            raise ValueError("Research published transcript conflicts with durable history")
        recover_graph = getattr(event_port, "recover_graph", None)
        if not callable(recover_graph):
            return None
        graph_recovery = recover_graph(run_id)
        if graph_recovery.state is None:
            return None
        status = graph_recovery.state.outcome.value
        graph_id = graph_recovery.state.graph_ref.graph_id
        graph_version = graph_recovery.state.graph_ref.identity_version
        # A finalized manifest can become durable immediately before the
        # controller commits COMPLETE_RUN.  It is not an accepted run yet,
        # and persisting that transient state as quarantine would make the
        # immutable run id impossible to accept after the next recovery pass.
        if status != "succeeded":
            return None
        trace = HarnessTrace(
            run_id=run_id,
            events=history,
            metadata=dict(published_trace.metadata),
        )
        transcript = HarnessTranscript(run_id, published_transcript.entries())
        for index, event in enumerate(
            history[cutoff_index + 1 :],
            start=cutoff_index + 1,
        ):
            entry = transcript_entry_from_event(event, phase_index=index)
            transcript.append(
                replace(
                    entry,
                    metadata={**entry.metadata, **actor_metadata},
                )
            )

        optional = {
            "analysis": analysis,
            "paper_card": _optional_artifact_payload(
                payloads,
                "research-paper-card",
            ),
            "reader_payload": _optional_artifact_payload(
                payloads,
                "research-reader-payload",
            ),
            "rag_context": _optional_artifact_payload(
                payloads,
                "research-rag-context-pack",
            ),
            "reader_issue": _optional_artifact_payload(payloads, "reader-issue"),
            "context_snapshot": _optional_artifact_payload(
                payloads,
                "research-context-snapshot",
            ),
        }
        compression = _optional_artifact_payload(
            payloads,
            "research-context-compression-records",
        )
        compression_records = [] if compression is None else compression.get("records")
        if not isinstance(compression_records, list):
            raise ValueError("Research recovery compression records are invalid")
        result = ResearchAnalysisResult.from_dict(
            {
                "run_id": run_id,
                "graph_id": graph_id,
                "graph_version": graph_version,
                "status": status,
                **optional,
                "quality": quality,
                "artifact_refs": artifact_refs,
                "trace": trace.to_dict(include_deterministic_history=True),
                "transcript": transcript.to_dict(),
                "context_envelope": None,
                "compression_records": compression_records,
                "skill_experience_refs": [],
                "actor_scope": actor_metadata,
                "diagnostics": {
                    "harness_status": status,
                    "publication_authority_ref": authority_ref,
                    "terminal_side_effect_outcome_ref": outcome_ref,
                    "terminal_history_cutoff": cutoff,
                    "artifact_evidence_ref": artifact_evidence_ref,
                    "recovered_from_durable_publication": True,
                },
                "trace_ref": f"harness-trace://{run_id}",
                "reader_payload_ref": artifact_refs.get(
                    "research-reader-payload"
                ),
            }
        )
        return ResearchRunRecord(
            run_id=run_id,
            paper_id=paper_id,
            result=result,
            identity_scope_ref=identity_scope_ref,
            subject_scope_ref=subject_scope_ref,
            publication_authority_ref=authority_ref,
            artifact_evidence_ref=artifact_evidence_ref,
            schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
        )

    def load_failure_record(
        self,
        request: AnalyzePaperRequest,
    ) -> ResearchRunRecord | None:
        """Rebuild a raised run from history without resuming workers or effects."""

        if not isinstance(request, AnalyzePaperRequest):
            raise TypeError("request must be AnalyzePaperRequest")
        try:
            manifest = self._manifest_reader(request.run_id)
        except FileNotFoundError:
            manifest = None
        if manifest is not None and _is_recoverable_research_manifest(manifest):
            recovered = self.load_recovery_record(request.run_id)
            if recovered is not None:
                return recovered
            # The publication is durable but the terminal success transition
            # is not.  Treat this as an in-flight failure diagnostic below;
            # startup reconciliation will defer it until COMPLETE_RUN exists.
        actor_metadata = {
            "memory_namespace": request.memory_namespace,
            **(
                {"tenant_id": request.tenant_id}
                if request.tenant_id is not None
                else {}
            ),
            **(
                {"user_id": request.user_id}
                if request.user_id is not None
                else {}
            ),
        }
        if not isinstance(actor_metadata["memory_namespace"], str) or not str(
            actor_metadata["memory_namespace"]
        ).strip():
            raise ValueError("Research failure recovery actor scope is incomplete")

        event_port = self._scoped_event_port_factory(request.run_id, actor_metadata)
        history = event_port.read_history(request.run_id)
        if not history:
            return None
        if not isinstance(history, tuple) or any(
            not isinstance(event, HarnessEvent) or event.run_id != request.run_id
            for event in history
        ):
            raise ValueError("Research failure recovery event history is invalid")
        created = tuple(
            event
            for event in history
            if event.event_type is HarnessEventType.RUN_CREATED
        )
        if not created:
            return None
        if len(created) != 1:
            raise ValueError("Research failure recovery run creation is ambiguous")

        run_spec = build_research_harness_run_spec(
            request,
            created_at=created[0].occurred_at,
        )
        recover_graph = getattr(event_port, "recover_graph", None)
        if not callable(recover_graph):
            return None
        recovery = recover_graph(run_spec.run_id)
        if recovery.state is None:
            return None
        result = ResearchAnalysisResult.from_durable_failure(
            request=request,
            events=history,
            harness_status=recovery.state.outcome.value,
        )
        return ResearchRunRecord(
            run_id=request.run_id,
            paper_id=request.paper_id,
            result=result,
            identity_scope_ref=research_identity_scope_ref(actor_metadata),
            subject_scope_ref=research_subject_scope_ref(request.paper_id),
            schema_version=RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2,
        )

    def _read_recovery_artifact(
        self,
        *,
        artifact_type: str,
        ref: str,
    ) -> dict[str, Any]:
        envelope = self._diagnostic_artifact_reader.read_artifact(ref)
        if envelope.get("artifact_type") != artifact_type:
            raise ValueError(
                f"Research recovery artifact identity conflicts: {artifact_type}"
            )
        return envelope



def _configured_paper_answer_worker_factory(
    llm_client: OpenAICompatibleClient,
    *,
    max_output_tokens: int,
) -> Any:
    async def answer_llm_call(
        prompt: str,
        *,
        execution_identity: GraphExecutionIdentity | None = None,
        timeout_seconds: float | None = None,
        max_transport_attempts: int | None = None,
    ) -> str:
        request = LLMRequest(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_output_tokens,
            temperature=0.0,
            execution_identity=execution_identity,
            timeout_seconds=timeout_seconds,
            max_transport_attempts=max_transport_attempts,
        )
        response = await asyncio.to_thread(llm_client.complete, request)
        return response.content or ""

    return lambda: PaperAnswerWorker(AnswerGenerator(answer_llm_call))

def _local_reader_rag_service(
    *,
    ask_use_case: AskPaperUseCase,
    chunk_store: ChunkStorePort,
    answer_worker_factory: Callable[[], Any],
    context_assembler_factory: Callable[[RAGSessionSpec], ContextAssembler],
    transcript_root: Path,
) -> Any:
    from interfaces.services.paper_rag_service import PaperRagApplicationService
    from interfaces.services.paper_rag_transcript_store import PaperRagTranscriptFileStore

    return PaperRagApplicationService(
        with_reranker=False,
        retriever=ResearchRetriever(chunk_store),
        session_factory=lambda **_kwargs: PaperRAGSession(
            chunk_store,
            answer_worker=answer_worker_factory(),
            generation_policy={"enabled": True, "max_attempts": 2},
            context_assembler_factory=context_assembler_factory,
            event_recorder=_kwargs.get("event_recorder"),
        ),
        ask_use_case=ask_use_case,
        transcript_store=PaperRagTranscriptFileStore(transcript_root),
    )


def _build_local_only_composition(
    source_runtime_provider: SourceRuntimeProvider,
) -> ResearchRuntimeComposition:
    from interfaces.composition.research_local import build_local_research_service

    settings = ResearchLocalRuntimeSettings.from_env()
    error = ResearchRuntimeUnavailableError(
        (ResearchCapability.LLM_ROUTE,),
        retryable=False,
    )
    service = build_local_research_service(settings)
    return ResearchRuntimeComposition(
        settings=settings,
        service=service,
        source_runtime_provider=source_runtime_provider,
        availability_error=error,
    )


def _unavailable_composition(
    error: ResearchCompositionError,
    *,
    settings: ResearchRuntimeSettings | None,
    source_runtime_provider: SourceRuntimeProvider,
) -> ResearchRuntimeComposition:
    ask_use_case = AskPaperUseCase()
    rag_ask_provider = _PaperRagUseCaseProvider(ask_use_case)
    service = ResearchApplicationService(
        analyze_use_case=_UnavailableAnalyzeUseCase(error),
        ask_use_case=ask_use_case,
        rag_ask_use_case=rag_ask_provider,
        run_store=_UnavailableResearchRunStore(error),
        runtime_config_identity=(
            _research_runtime_config_identity(settings) if settings is not None else "unconfigured"
        ),
    )
    return ResearchRuntimeComposition(
        settings=settings,
        service=service,
        source_runtime_provider=source_runtime_provider,
        resources=(rag_ask_provider,),
        availability_error=error,
        reader_repair_service=None,
    )


def _research_service_error(error: ResearchCompositionError) -> ResearchServiceError:
    public = error.to_public_dict()
    return ResearchServiceError(
        str(public["code"]),
        str(public["message"]),
        status_code=503,
        details={
            "capabilities": list(public["capabilities"]),
            "remediation": dict(public["remediation"]),
        },
        retryable=bool(public["retryable"]),
        user_action_required=True,
    )


def _resolve_research_llm_deployment(
    settings: ResearchRuntimeSettings,
) -> OpenAICompatibleDeploymentConfig:
    try:
        deployment = load_openai_compatible_deployment(
            settings.llm.models_config_path,
            route_id=settings.llm.route_id,
            apply_environment_overrides=False,
        )
    except LLMConfigurationError as exc:
        raise ResearchConfigurationError((ResearchCapability.LLM_ROUTE,)) from exc
    if deployment.config.model != settings.llm.model:
        raise ResearchConfigurationError((ResearchCapability.LLM_MODEL,))
    if deployment.config.base_url.rstrip("/") != settings.llm.base_url.rstrip("/"):
        raise ResearchConfigurationError((ResearchCapability.LLM_BASE_URL,))
    return deployment


def _build_research_chunk_store(
    settings: ResearchRAGSettings,
    *,
    qdrant_client_factory: Callable[..., Any] | None = None,
    embedding_model_factory: Callable[..., Any] | None = None,
) -> tuple[ChunkStorePort, tuple[Any, ...]]:
    if not isinstance(settings, ResearchRAGSettings):
        raise TypeError("settings must be ResearchRAGSettings")

    client: Any | None = None
    try:
        if settings.backend == "local":
            payload_store = LocalChunkPayloadStore(
                settings.local_root,
                collection=settings.collection,
            )
            store = PaperChunkStoreAdapter(payload_store)
            resources: tuple[Any, ...] = ()
        else:
            # Qdrant is optional for baseline readiness. Keep every Qdrant import
            # inside this selected branch so the local backend remains importable
            # without the optional client/runtime.
            from qdrant_client import QdrantClient

            from infrastructure.storage.vector.embeddings import embedding_model_from_env
            from infrastructure.storage.vector.paper_chunk_store import PaperChunkStore
            from infrastructure.storage.vector.qdrant_store import QdrantVectorStore

            client_builder = qdrant_client_factory or QdrantClient
            embedding_builder = embedding_model_factory or embedding_model_from_env
            client = client_builder(url=settings.qdrant_url)
            embedding_model = embedding_builder(vector_size=settings.vector_size)
            vector_store = QdrantVectorStore(
                client,
                embedding_model=embedding_model,
                vector_size=settings.vector_size,
            )
            store = PaperChunkStoreAdapter(
                PaperChunkStore(vector_store, collection=settings.collection)
            )
            resources = (client,)
        store.ensure_collection()
        return store, resources
    except ResearchCompositionError:
        _close_quietly(client)
        raise
    except Exception:
        _close_quietly(client)
        capability = (
            ResearchCapability.RAG_LOCAL_ROOT
            if settings.backend == "local"
            else ResearchCapability.RAG_VECTOR_BACKEND
        )
        raise ResearchRuntimeUnavailableError(
            (capability,),
            retryable=True,
        ) from None


def _close_quietly(resource: Any | None) -> None:
    close = getattr(resource, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        return


def _build_configured_composition(
    settings: ResearchRuntimeSettings,
    source_runtime_provider: SourceRuntimeProvider,
    *,
    dynamic_rollout_admission: ResearchDynamicRolloutAdmissionPort | None = None,
) -> ResearchRuntimeComposition:
    owned_resources: list[Any] = []
    try:
        execution_composition = _compose_component(
            ResearchCapability.DOCUMENT_COMPILER,
            build_research_execution_composition,
        )
        source_runtime = _compose_component(
            ResearchCapability.SOURCE,
            source_runtime_provider.get,
        )
        metadata_policy = _research_source_policy(
            source_runtime.fetch_policy,
            timeout_seconds=settings.source.timeout_seconds,
            max_bytes=settings.source.metadata_max_bytes,
        )
        package_policy = _research_source_policy(
            ArxivSourceConnector.default_fetch_policy(),
            timeout_seconds=settings.source.timeout_seconds,
            max_bytes=min(
                settings.source.package_max_bytes,
                settings.parser.max_document_bytes,
            ),
            rate_limit_per_domain_per_minute=(
                source_runtime.fetch_policy.rate_limit_per_domain_per_minute
            ),
        )
        metadata_connector = ArxivConnector(
            fetch_policy=metadata_policy,
            rate_limiter=source_runtime.reservation_ledger,
        )
        package_connector = _configure_research_source_connector(
            source_runtime.research_arxiv_connector,
            package_policy,
        )
        arxiv_source_provider = ArxivResearchSourceProvider(
            metadata_connector,
            api_url=settings.source.api_url,
            cache_size=settings.source.cache_size,
        )
        catalog_store = FilesystemResearchCatalogStore(
            settings.research_root / "paper-catalog"
        )
        document_compiler = _compose_component(
            ResearchCapability.DOCUMENT_COMPILER,
            lambda: ResearchDocumentCompilerAdapter(
                package_connector,
                latex_compiler=ArxivLatexDocumentCompiler(package_connector),
                pdf_parser=_build_research_pdf_parser(
                    settings.parser,
                    execution_composition=execution_composition,
                    execution_evidence_store=catalog_store,
                ),
                allow_abstract_fallback=settings.parser.allow_abstract_fallback,
            ),
        )

        llm_deployment = _resolve_research_llm_deployment(settings)
        llm_client = OpenAICompatibleClient(
            OpenAICompatibleConfig(
                provider=llm_deployment.config.provider,
                base_url=settings.llm.base_url,
                model=settings.llm.model,
                api_key_env=settings.llm.api_key_env,
                timeout_seconds=settings.llm.timeout_seconds,
            ),
            retry_policy=LLMRetryPolicy(
                max_attempts=settings.llm.max_attempts,
            ),
            structured_output_capability=(
                llm_deployment.structured_output_capability
            ),
        )
        candidate_worker = _compose_component(
            ResearchCapability.CANDIDATE_WORKER,
            lambda: StructuredResearchCandidateWorker(
                llm_client,
                max_input_tokens=settings.llm.max_input_tokens,
                max_output_tokens=settings.llm.max_output_tokens,
            ),
        )
        github_repository = GithubResearchRepositoryAdapter(
            GithubConnector(
                fetch_policy=metadata_policy,
                rate_limiter=source_runtime.reservation_ledger,
            )
        )
        from infrastructure.research.document_repository import FilesystemResearchDocumentRepository

        documents = FilesystemResearchDocumentRepository(catalog_store)
        source_provider = CatalogBoundResearchSourceProvider(arxiv_source_provider, documents)
        catalog_service = ResearchPaperCatalogService(
            catalog_repository=catalog_store,
            identity_repository=catalog_store,
            relation_repository=catalog_store,
            source_snapshot_repository=catalog_store,
            paper_repository=catalog_store,
            document_repository=documents,
            evidence_repository=catalog_store,
            code_profile_repository=catalog_store,
            sota_claim_repository=catalog_store,
            github_repository=github_repository,
        )
        source_resolver = ResearchSourceResolverAdapter(
            arxiv_provider=arxiv_source_provider,
            arxiv_fetcher=package_connector,
            github_repository=github_repository,
            fetch_policy=package_policy,
            rate_limiter=source_runtime.reservation_ledger,
            local_root=settings.research_root,
            artifact_reader=catalog_store,
        )
        parse_use_case = ParsePaperUseCase(
            source_resolver=source_resolver,
            paper_repository=catalog_store,
            identity_repository=catalog_store,
            source_snapshot_repository=catalog_store,
            document_repository=documents,
            evidence_repository=catalog_store,
            document_parser=MultiFormatDocumentParser(
                arxiv_parser=_build_research_multi_format_parser(
                    settings.parser,
                    execution_composition=execution_composition,
                    execution_evidence_store=catalog_store,
                ),
                pdf_parser=SelectableTextPdfDocumentParser(),
            ),
            document_compiler=document_compiler,
            artifact_store=catalog_store,
            event_sink=FilesystemResearchEventSink(
                settings.research_root / "paper-catalog"
            ),
            catalog_projection=catalog_service,
            chunker=PaperDocumentChunker(),
            chunk_manifest=ChunkManifestManager(
                settings.research_root / "paper-catalog" / "chunk-manifests"
            ),
        )
        production_evaluation_parse_use_case = ParsePaperUseCase(
            source_resolver=source_resolver,
            paper_repository=catalog_store,
            identity_repository=catalog_store,
            source_snapshot_repository=catalog_store,
            document_repository=documents,
            evidence_repository=catalog_store,
            document_parser=_build_research_pdf_parser(
                settings.parser,
                execution_composition=execution_composition,
                execution_evidence_store=catalog_store,
                allow_selectable_text_fallback=False,
            ),
            artifact_store=catalog_store,
            event_sink=FilesystemResearchEventSink(
                settings.research_root / "paper-catalog"
            ),
            catalog_projection=catalog_service,
            chunker=PaperDocumentChunker(),
            chunk_manifest=ChunkManifestManager(
                settings.research_root / "paper-catalog" / "chunk-manifests"
            ),
            max_retries=0,
        )

        chunk_store, chunk_resources = _build_research_chunk_store(settings.rag)
        owned_resources.extend(chunk_resources)
        artifact_port = _compose_component(
            ResearchCapability.ARTIFACT,
            lambda: FilesystemHarnessArtifactPort(
                settings.artifact.root,
                max_write_bytes=settings.artifact.max_bytes,
            ),
        )
        graph_artifact_components = _compose_component(
            ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,
            lambda: compose_research_graph_artifact_runtime(
                settings,
                artifact_port=artifact_port,
            ),
        )
        graph_result_catalog = graph_artifact_components.catalog
        graph_result_store = graph_artifact_components.store
        graph_result_materializer = graph_artifact_components.materializer
        side_effect_store = _compose_component(
            ResearchCapability.ARTIFACT,
            lambda: SQLiteHarnessSideEffectStore(
                settings.research_root / "harness-side-effects.sqlite3"
            ),
        )
        owned_resources.append(side_effect_store)
        owned_resources.append(execution_composition)
        node_output_resource = _compose_component(
            ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,
            lambda: SQLiteHarnessNodeOutputResource(
                settings.research_root / "harness-node-output.sqlite3"
            ),
        )
        owned_resources.append(node_output_resource)
        run_store = _compose_component(
            ResearchCapability.RUN_STORE,
            lambda: FilesystemResearchRunStore(
                settings.run_store.root,
                result_decoder=ResearchAnalysisResult.from_dict,
                max_record_bytes=settings.run_store.max_record_bytes,
                write_schema_version=_research_run_store_schema_version(
                    settings.run_store.write_schema_version
                ),
                supported_schema_versions=tuple(
                    _research_run_store_schema_version(version)
                    for version in settings.run_store.supported_schema_versions
                ),
            ),
        )
        harness_wait_runtime_registry = HarnessWaitRuntimeRegistry(
            settings.run_store.root / "harness-waits"
        )
        harness_wait_actor_scope_resolver = _ResearchHarnessWaitActorScopeResolver()
        harness_wait_approval_resolver = DurableHarnessWaitApprovalResolver(
            runtime_resolver=harness_wait_runtime_registry,
            actor_scope_resolver=harness_wait_actor_scope_resolver,
            root=settings.run_store.root / "harness-waits",
        )
        owned_resources.append(harness_wait_runtime_registry)
        durable_events = _compose_component(
            ResearchCapability.EVENT_LOG,
            lambda: durable_event_storage_from_env(
                artifact_root=settings.artifact.root,
            ),
        )
        owned_resources.extend(
            resource
            for resource in (
                durable_events.event_store,
                durable_events.replay_checkpoint_store,
                durable_events.activity_store,
            )
            if resource is not None
        )
        graph_event_projection = DurableGraphEventProjectionAdapter(
            reader=durable_events.event_store,
            schema_catalog=durable_events.schema_catalog,
        )
        graph_index_store = _compose_component(
            ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,
            lambda: LocalGraphStorageIndexStore(
                settings.artifact.root / "graph-index",
            ),
        )
        graph_index_publisher = _compose_component(
            ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,
            lambda: GraphStorageIndexPublisher(
                manifest_reader=artifact_port,
                event_reader=durable_events.event_store,
                index_store=graph_index_store,
            ),
        )
        from interfaces.composition.harness_memory import build_harness_ref_admission_service
        from framework.harness.ref_results import HarnessResultRefAuthority
        from framework.harness.ref_snapshot_store import DurableRefAuthoritySnapshotStore

        def dynamic_ref_admission_service_for_tenant(
            tenant_id: str,
        ) -> HarnessRefAdmissionService:
            return build_harness_ref_admission_service(
                snapshot_store=DurableRefAuthoritySnapshotStore(
                    durable_events.event_runtime,
                    durable_events.event_store,
                    artifact_store=artifact_port.store,
                    tenant_id=tenant_id,
                ),
                namespace_root=settings.artifact.root / "memory-namespaces",
            )

        def dynamic_task_plan_store_for_tenant(
            tenant_id: str,
        ) -> DurableTaskPlanStore:
            return DurableTaskPlanStore(
                durable_events.event_runtime,
                durable_events.event_store,
                artifact_store=artifact_port.store,
                tenant_id=tenant_id,
            )
        dynamic_checkpoint_store = JsonlTaskPlanCheckpointStore(
            settings.artifact.root / "task-plan-checkpoints.jsonl"
        )
        subagent_transcript_store = FilesystemSubAgentTranscriptStore(
            settings.artifact.root,
            max_output_bytes=settings.artifact.max_bytes,
            max_bundle_bytes=max(
                settings.artifact.max_bytes + 4 * 1024 * 1024,
                12 * 1024 * 1024,
            ),
        )
        # Keep the production transcript adapter under composition ownership.
        # Every dynamic ChildAgentRunnerAdapter shares this durable store
        # instead of constructing a second transcript authority.
        owned_resources.append(subagent_transcript_store)
        runtime_event_publisher = CanonicalRuntimeEventPublisher(
            durable_events.event_runtime,
            tenant_id=_RESEARCH_EVENT_TENANT_ID,
        )
        dynamic_child_runtime = LazyResearchChildRuntime(
            state_runtime=durable_events.event_runtime,
            state_reader=durable_events.event_store,
            max_children=build_research_analysis_task_plan_policy().max_parallelism,
            runtime_event_sink=runtime_event_publisher,
        )

        def dynamic_task_plan_runner_factory(*, workspace: Any, dependencies: Any):
            if not hasattr(workspace, "request") or not hasattr(workspace, "document"):
                raise HarnessValidationError(
                    "dynamic Research workspace is not a run-bound workspace",
                    code="research_dynamic_workspace_invalid",
                )
            request = workspace.request
            actor_scope = {
                "tenant_id": request.tenant_id,
                "user_id": request.user_id,
                "memory_namespace": request.memory_namespace,
            }
            tenant_id = research_event_tenant_id(actor_scope)
            policy = build_research_analysis_task_plan_policy()
            _admit_dynamic_research_rollout(
                dynamic_rollout_admission,
                run_id=request.run_id,
                scope_ref=research_identity_scope_ref(actor_scope),
                policy=policy,
            )
            dynamic_task_plan_store = dynamic_task_plan_store_for_tenant(tenant_id)
            dynamic_ref_admission_service = (
                dynamic_ref_admission_service_for_tenant(tenant_id)
            )
            binding = dynamic_child_runtime.start_for_run(
                run_id=request.run_id,
                tenant_id=tenant_id,
            )
            graph_definition = (
                build_quality_dynamic_paper_analysis_graph_definition()
                if research_input_quality_enabled(request.options)
                else build_dynamic_paper_analysis_graph_definition()
            )
            graph = HarnessGraphCompiler().compile(graph_definition).graph
            stage_binding = TaskPlanStageBinding(
                graph,
                RESEARCH_DYNAMIC_STAGE_ID,
            )

            transcript_store = subagent_transcript_store
            result_authority = HarnessResultRefAuthority(
                dynamic_ref_admission_service.store,
                transcript_store=transcript_store,
                artifact_descriptors=artifact_port,
                tenant_id=tenant_id,
            )
            input_reader = _ResearchDynamicInputReader(
                workspace=workspace,
                result_authority=result_authority,
            )

            child_specs = build_research_analysis_subagent_specs()
            workers: dict[str, Any] = {}
            worker_bindings: dict[str, HarnessWorkerBinding] = {}
            for child_spec in child_specs:
                capability = str(child_spec.metadata["worker_capability"])
                worker = _ProductionResearchAnalysisWorker(
                    capability,
                    dependencies=dependencies,
                    workspace=workspace,
                )
                registered_agent = AgentSpec(
                    agent_id=child_spec.subagent_id,
                    name=child_spec.role,
                    role=child_spec.role,
                    goal=child_spec.purpose,
                    instructions=(
                        "Generate only the admitted Research candidate payload. "
                        "Harness owns routing, gates, memory, and publication."
                    ),
                    output_key=str(child_spec.output_schema["required"][0]),
                    output_schema=dict(child_spec.output_schema),
                    allowed_tools=list(child_spec.allowed_tools),
                    memory_enabled=False,
                    max_iterations=int(child_spec.budget["max_turns"]),
                )
                worker_ref = policy.pinned_capability_bindings[capability]
                worker_id, worker_version = worker_ref.rsplit("@", 1)
                runner = ChildAgentRunnerAdapter(
                    registered_agent=registered_agent,
                    llm_client=_ResearchCandidateLLMClient(worker),
                    tool_registry=ToolRegistry(),
                    conversation_store=LocalJsonConversationStore(
                        settings.artifact.root
                        / "_state"
                        / "research-dynamic-conversations"
                        / child_spec.subagent_id
                    ),
                    execution_environment=execution_composition.execution_registry,
                    require_explicit_execution_profile=True,
                    worker_id=worker_id,
                    worker_version=worker_version,
                    prepared_input_projection=(
                        workspace.input_context.projection
                        if workspace.input_context is not None
                        else None
                    ),
                )
                workers[child_spec.subagent_id] = runner
                worker_bindings[capability] = HarnessWorkerBinding(
                    HarnessContractReference(
                        HarnessContractKind.WORKER,
                        worker_id,
                        worker_version,
                    ),
                    HarnessWorkerType.SUBAGENT,
                    runner,
                )

            capability_registry = build_research_analysis_capability_registry(
                worker_bindings
            )
            runtime = SubAgentRuntime(
                workers=workers,
                transcript_store=transcript_store,
                result_ref_authority=result_authority,
                runtime_event_sink=runtime_event_publisher,
            )
            budget_trackers = CanonicalChildBudgetTrackerProvider(
                GlobalBudgetTracker(GlobalBudgetPolicy(max_llm_calls=10_000))
            )
            execution_service = HarnessChildExecutionService(
                store=dynamic_task_plan_store,
                ref_admission_service=dynamic_ref_admission_service,
                state_runtime=durable_events.event_runtime,
                state_reader=durable_events.event_store,
                artifact_store=artifact_port.store,
                budget_trackers=budget_trackers,
                leases=None,
                evidence_limits=AdmissionChildToolEvidenceLimitsProvider(),
                usage_meter=CanonicalChildExecutionUsageMeter(budget_trackers),
                input_reader=input_reader,
            )
            executor = HarnessSubAgentTaskExecutor(
                store=dynamic_task_plan_store,
                runtime=runtime,
                ref_admission_service=dynamic_ref_admission_service,
                task_policy=policy,
                execution_service=execution_service,
            )
            gate_registry = build_research_analysis_task_gate_registry(
                build_paper_analysis_gate_registry(),
                context_factory=lambda gate_request: _dynamic_research_gate_context(
                    workspace,
                    gate_request.worker_result,
                ),
            )
            verifier = TaskPlanResultVerifier(
                gate_registry,
                transcript_store=transcript_store,
                artifact_reference_verifier=artifact_port,
                result_ref_authority=result_authority,
                gate_artifact_writer=dynamic_task_plan_store,
            )
            executor.require_production_bindings(
                store=dynamic_task_plan_store,
                admission=dynamic_ref_admission_service,
                verifier=verifier,
                bindings=tuple(capability_registry.resolve(capability, policy) for capability in policy.allowed_worker_capabilities),
                gate_refs=policy.allowed_gate_refs,
                task_policy=policy,
            )
            return ResearchAnalysisTaskPlanStageWorker(
                stage_binding=stage_binding,
                accepted_at=utc_now().isoformat(),
                candidate_builder=ResearchAnalysisPlanCandidateBuilder(
                    _PreparedResearchCandidateWorker(
                        candidate_worker,
                        workspace=workspace,
                    ),
                ),
                capability_registry=capability_registry,
                store=dynamic_task_plan_store,
                worker_executor=executor,
                worker_result_recovery=executor.recover,
                result_verifier=verifier,
                policy=policy,
                parallel_coordinator=binding.parallel_coordinator,
                child_agent_supervisor=binding.supervisor,
                checkpoint_store=dynamic_checkpoint_store,
                ref_admission_service=dynamic_ref_admission_service,
            )
        def event_port_factory(_run_id: str) -> HarnessTransitionPort:
            return durable_events.create_harness_transition_port(
                tenant_id=_RESEARCH_EVENT_TENANT_ID,
            )

        def scoped_event_port_factory(
            _run_id: str,
            actor_metadata: Mapping[str, Any],
        ) -> HarnessTransitionPort:
            return durable_events.create_harness_transition_port(
                tenant_id=research_event_tenant_id(actor_metadata),
            )

        production_parsing_evaluation_service = (
            ProductionParsingGraphApplicationService(
                parse_use_case=production_evaluation_parse_use_case,
                harness_runner=ResearchPhysicalGraphRunner(
                    event_port_factory=event_port_factory,
                    node_output_resource=node_output_resource,
                    side_effect_store=side_effect_store,
                ),
                side_effect_store=side_effect_store,
                chunk_artifact_reader=catalog_store,
                durable_event_reader=durable_events.event_store,
                event_tenant_id=_RESEARCH_EVENT_TENANT_ID,
            )
        )

        reader_repair_service: ReaderRepairGraphApplicationService | None = None
        reader_repair_memory = build_reader_repair_memory_from_env()
        reader_repair_memory_commit = build_reader_repair_memory_commit_port_from_env()
        reader_repair_failure_commit = (
            build_reader_repair_failure_diagnostic_commit_port_from_env()
        )
        if (
            reader_repair_memory is not None
            and reader_repair_memory_commit is not None
            and reader_repair_failure_commit is not None
        ):
            reader_repair_memory_handler = ReaderRepairMemorySideEffectHandler(
                commit_port=reader_repair_memory_commit,
                side_effect_store=side_effect_store,
            )
            reader_repair_failure_handler = (
                ReaderRepairFailureDiagnosticSideEffectHandler(
                    reader_repair_failure_commit
                )
            )
            reader_repair_service = ReaderRepairGraphApplicationService(
                event_port_factory=event_port_factory,
                scoped_event_port_factory=scoped_event_port_factory,
                node_output_resource=node_output_resource,
                side_effect_store=side_effect_store,
                memory=reader_repair_memory,
                memory_side_effect_handler=reader_repair_memory_handler,
                failure_diagnostic_side_effect_handler=reader_repair_failure_handler,
                candidate_worker=candidate_worker,
            )

        def graph_result_committer_factory(
            *,
            event_port: HarnessTransitionPort,
            request: AnalyzePaperRequest,
            workspace: Any | None = None,
        ):
            if not isinstance(graph_result_materializer, ResultMaterializer):
                raise ResearchRuntimeUnavailableError(
                    (ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,),
                    retryable=False,
                )
            actor_metadata = {
                "tenant_id": request.tenant_id,
                "user_id": request.user_id,
                "memory_namespace": request.memory_namespace,
            }
            result_tenant_id = research_event_tenant_id(actor_metadata)
            return ResearchGraphResultCommitter(
                materializer=graph_result_materializer,
                graph_result_runtime=HarnessGraphResultRuntime(
                    HarnessGraphControlPlaneRuntime(event_port)
                ),
                config=settings.graph_artifact_persistence,
                tenant_id=result_tenant_id,
                tenant_scope_ref=checksum_for(result_tenant_id),
                context_fingerprint_resolver=(
                    None
                    if workspace is None
                    else lambda node_id: _research_context_fingerprint(
                        workspace,
                        node_id=node_id,
                    )
                ),
            )

        def context_assembler_factory(
            _run_id: str,
            event_port: HarnessTransitionPort,
        ) -> ContextAssembler:
            if graph_result_catalog is None:
                raise ResearchRuntimeUnavailableError(
                    (ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,),
                    retryable=False,
                )
            artifact_context_provider = ResearchGraphArtifactContextProvider(
                event_port=event_port,
                catalog=graph_result_catalog,
                reader=artifact_port,
                usage=graph_result_store,
                config=settings.graph_artifact_persistence,
            )
            return build_research_context_assembler(
                artifact_port=artifact_port,
                event_port=event_port,
                provider=settings.llm.provider,
                model=settings.llm.model,
                max_input_tokens=settings.llm.max_input_tokens,
                max_output_tokens=settings.llm.max_output_tokens,
                artifact_context_provider=artifact_context_provider,
            )

        def rag_context_assembler_factory(spec: RAGSessionSpec) -> ContextAssembler:
            actor_metadata = {
                key: spec.source_policy[key]
                for key in ("tenant_id", "user_id", "memory_namespace")
                if spec.source_policy.get(key)
            }
            return build_research_context_assembler(
                artifact_port=artifact_port,
                event_port=scoped_event_port_factory(spec.run_id, actor_metadata),
                provider=settings.llm.provider,
                model=settings.llm.model,
                max_input_tokens=settings.llm.max_input_tokens,
                max_output_tokens=settings.llm.max_output_tokens,
            )

        rag_runtime = BoundedDocumentRAGRuntime(
            chunk_store,
            session_factory=lambda scoped_store: PaperRAGSession(
                scoped_store,
                context_assembler_factory=rag_context_assembler_factory,
            ),
            authorized_session_factory=lambda scoped_store, memory_recall: PaperRAGSession(
                scoped_store,
                context_assembler_factory=rag_context_assembler_factory,
                memory=ResearchRAGMemoryPort(memory_recall),
            ),
        )
        rag_memory_admission = (
            HarnessResearchRAGMemoryAdmission(
                dynamic_ref_admission_service,
                namespace_refs=settings.rag.memory_namespace_refs,
            )
            if settings.rag.memory_enabled
            else None
        )

        recovery_source = _DurableResearchRunRecoverySource(
            artifact_port=artifact_port,
            run_store=run_store,
            side_effect_store=side_effect_store,
            scoped_event_port_factory=scoped_event_port_factory,
        )
        run_reconciler = ResearchRunDispositionReconciler(
            run_store=run_store,
            recovery_source=recovery_source,
            max_runs=settings.run_store.reconciliation_max_runs,
        )
        artifact_port.set_accepted_run_resolver(
            lambda claim: (
                _resolve_research_artifact_run(
                    run_store,
                    claim=claim,
                    reconciler=run_reconciler,
                )
            )
        )
        artifact_port.set_diagnostic_run_resolver(
            lambda claim: _research_artifact_diagnostic_is_authorized(
                run_store,
                claim=claim,
            )
        )

        # Validate secure activity storage at composition time. This keeps a
        # missing encryption capability on the typed unavailable path rather
        # than failing the first production request after startup.
        _compose_component(
            ResearchCapability.EVENT_LOG,
            lambda: event_port_factory("research-composition-validation"),
        )

        runtime = ResearchSinglePaperRuntime(
            source_provider=source_provider,
            document_compiler=document_compiler,
            llm_worker=candidate_worker,
            github_repository=github_repository,
            rag_runtime=rag_runtime,
            rag_memory_admission=rag_memory_admission,
            artifact_port=artifact_port,
            event_port_factory=event_port_factory,
            scoped_event_port_factory=scoped_event_port_factory,
            context_assembler_factory=context_assembler_factory,
            context_max_input_tokens=settings.llm.max_input_tokens,
            context_max_output_tokens=settings.llm.max_output_tokens,
            side_effect_store=side_effect_store,
            artifact_handler_factory=ResearchArtifactBundleHandler,
            dynamic_task_plan_runner_factory=dynamic_task_plan_runner_factory,
            graph_result_committer_factory=graph_result_committer_factory,
            graph_event_projection=graph_event_projection,
            graph_index_publisher=graph_index_publisher,
            node_output_resource_factory=lambda _run_id: node_output_resource,
            runtime_binding_registrar=harness_wait_runtime_registry,
        )
        ask_use_case = AskPaperUseCase()
        paper_answer_worker_factory = _configured_paper_answer_worker_factory(
            llm_client,
            max_output_tokens=min(settings.llm.max_output_tokens, 900),
        )
        rag_ask_provider = _PaperRagUseCaseProvider(
            ask_use_case,
            service_factory=lambda: _local_reader_rag_service(
                ask_use_case=ask_use_case,
                chunk_store=chunk_store,
                answer_worker_factory=paper_answer_worker_factory,
                context_assembler_factory=rag_context_assembler_factory,
                transcript_root=settings.rag.transcript_root,
            ),
        )
        owned_resources.append(rag_ask_provider)
        # Registered last so reverse-order close releases the lifecycle owner
        # and stops child workers before the canonical durable backend closes.
        owned_resources.append(dynamic_child_runtime)
        service = ResearchApplicationService(
            analyze_use_case=AnalyzePaperUseCase(runtime),
            ask_use_case=ask_use_case,
            rag_ask_use_case=rag_ask_provider,
            run_store=run_store,
            run_reconciler=run_reconciler,
            diagnostic_artifact_reader=artifact_port,
            artifact_reader=catalog_store,
            parse_use_case=parse_use_case,
            catalog_service=catalog_service,
            runtime_config_identity=_research_runtime_config_identity(settings),
        )
        return ResearchRuntimeComposition(
            settings=settings,
            service=service,
            source_runtime_provider=source_runtime_provider,
            resources=owned_resources,
            execution_composition=execution_composition,
            graph_artifact_governance_service=(
                graph_artifact_components.governance_service
            ),
            reader_repair_service=reader_repair_service,
            production_parsing_evaluation_service=(
                production_parsing_evaluation_service
            ),
            harness_wait_runtime_registry=harness_wait_runtime_registry,
            harness_wait_approval_resolver=harness_wait_approval_resolver,
            harness_wait_actor_scope_resolver=harness_wait_actor_scope_resolver,
            runtime_event_publisher=runtime_event_publisher,
            subagent_transcript_store=subagent_transcript_store,
        )
    except BaseException:
        for resource in reversed(owned_resources):
            _close_quietly(resource)
        raise


def _research_artifact_run_is_accepted(
    run_store: Any,
    *,
    claim: ResearchArtifactReadClaim,
    reconciler: ResearchRunDispositionReconciler | None = None,
) -> bool:
    return _resolve_research_artifact_run(
        run_store,
        claim=claim,
        reconciler=reconciler,
    ).accepted


def _resolve_research_artifact_run(
    run_store: Any,
    *,
    claim: ResearchArtifactReadClaim,
    reconciler: ResearchRunDispositionReconciler | None = None,
) -> ResearchArtifactReadResolution:
    if not isinstance(claim, ResearchArtifactReadClaim):
        return ResearchArtifactReadResolution(accepted=False)
    record = run_store.get_by_run_id(claim.run_id)
    if record is None and reconciler is not None:
        record = reconciler.reconcile_run(
            claim.run_id,
            identity_scope_ref=claim.identity_scope_ref,
        )
    if record is None:
        return ResearchArtifactReadResolution(accepted=False)
    record_schema = getattr(record, "schema_version", None)
    if record_schema != RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2:
        return ResearchArtifactReadResolution(accepted=False)
    try:
        classified = classify_research_run_record(
            record,
            require_publication_authority=True,
            schema_version=record_schema,
        )
    except (TypeError, ValueError):
        return ResearchArtifactReadResolution(accepted=False)
    disposition = getattr(classified, "disposition", None)
    disposition_value = getattr(disposition, "value", disposition)
    result = getattr(classified, "result", None)
    artifact_refs = _research_result_field(result, "artifact_refs")
    diagnostics = _research_result_field(result, "diagnostics")
    if not isinstance(artifact_refs, Mapping) or not isinstance(diagnostics, Mapping):
        return ResearchArtifactReadResolution(accepted=False)
    record_identity_scope_ref = getattr(classified, "identity_scope_ref", None)
    record_subject_scope_ref = getattr(classified, "subject_scope_ref", None)
    record_authority_ref = getattr(classified, "publication_authority_ref", None)
    common_matches = (
        disposition_value == "accepted"
        and getattr(classified, "run_id", None) == claim.run_id
        and isinstance(record_identity_scope_ref, str)
        and bool(record_identity_scope_ref)
        and isinstance(record_subject_scope_ref, str)
        and bool(record_subject_scope_ref)
        and getattr(classified, "artifact_evidence_ref", None)
        == claim.artifact_evidence_ref
        and tuple(
            sorted((str(key), str(value)) for key, value in artifact_refs.items())
        )
        == claim.artifact_refs
        and (
            claim.terminal_side_effect_outcome_ref is None
            or diagnostics.get("terminal_side_effect_outcome_ref")
            == claim.terminal_side_effect_outcome_ref
        )
    )
    if not common_matches:
        return ResearchArtifactReadResolution(accepted=False)

    if claim.schema_version != RESEARCH_ARTIFACT_MANIFEST_VERSION:
        return ResearchArtifactReadResolution(accepted=False)
    exact_v2_evidence = (
        record_identity_scope_ref == claim.identity_scope_ref
        and record_subject_scope_ref == claim.subject_scope_ref
        and record_authority_ref == claim.publication_authority_ref
    )
    if not exact_v2_evidence:
        return ResearchArtifactReadResolution(accepted=False)
    return ResearchArtifactReadResolution(
        accepted=True,
        identity_scope_ref=record_identity_scope_ref,
    )


def _research_artifact_diagnostic_is_authorized(
    run_store: Any,
    *,
    claim: ResearchArtifactDiagnosticClaim,
) -> bool:
    if not isinstance(claim, ResearchArtifactDiagnosticClaim):
        return False
    getter = getattr(run_store, "get_diagnostic_by_run_id", None)
    if not callable(getter) or claim.subject_scope_ref is None:
        return False
    record = getter(
        claim.run_id,
        identity_scope_ref=claim.identity_scope_ref,
    )
    if record is None or not getattr(record, "quarantined", False):
        return False
    result = getattr(record, "result", None)
    artifact_refs = _research_result_field(result, "artifact_refs")
    if not isinstance(artifact_refs, Mapping):
        return False
    expected_ref = f"artifact://{claim.run_id}/{claim.artifact_type}"
    record_schema = getattr(record, "schema_version", None)
    expected_manifest_schema = (
        RESEARCH_ARTIFACT_MANIFEST_VERSION
        if record_schema == RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2
        else None
    )
    return (
        getattr(record, "run_id", None) == claim.run_id
        and getattr(record, "identity_scope_ref", None)
        == claim.identity_scope_ref
        and getattr(record, "subject_scope_ref", None)
        == claim.subject_scope_ref
        and getattr(record, "artifact_reference_disposition", None)
        == claim.disposition
        and claim.schema_version == expected_manifest_schema
        and artifact_refs.get(claim.artifact_type) == expected_ref
    )


def _research_result_field(result: Any, name: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def _research_context_fingerprint(
    workspace: Any,
    *,
    node_id: str,
) -> str | None:
    if node_id != "publish_artifacts":
        return None
    envelope = getattr(workspace, "context_envelope", None)
    if envelope is None:
        assembler = getattr(workspace, "context_assembler", None)
        provider = getattr(assembler, "artifact_context_provider", None)
        load_artifact_context = getattr(provider, "load_artifact_context", None)
        request = getattr(workspace, "request", None)
        if not callable(load_artifact_context) or not isinstance(
            request,
            AnalyzePaperRequest,
        ):
            raise HarnessValidationError(
                "Research publication result requires approved artifact context",
                code="research_artifact_context_fingerprint_missing",
            )
        actor_metadata = {"memory_namespace": str(request.memory_namespace)}
        if request.tenant_id:
            actor_metadata["tenant_id"] = request.tenant_id
        if request.user_id:
            actor_metadata["user_id"] = request.user_id
        loaded = load_artifact_context(
            {
                "run_id": request.run_id,
                "step_id": node_id,
                "metadata": actor_metadata,
            }
        )
        fingerprint = getattr(loaded, "context_fingerprint", None)
    elif isinstance(envelope, ContextEnvelope):
        fingerprint = envelope.metadata.get("artifact_context_fingerprint")
    else:
        raise HarnessValidationError(
            "Research publication result requires approved artifact context",
            code="research_artifact_context_fingerprint_missing",
        )
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
    ):
        raise HarnessValidationError(
            "Research publication context fingerprint is invalid",
            code="research_artifact_context_fingerprint_missing",
        )
    return fingerprint


def _research_run_store_schema_version(version: str) -> str:
    normalized = str(version or "").strip().lower()
    if normalized == "v1":
        return RESEARCH_RUN_RECORD_SCHEMA_VERSION
    if normalized == "v2":
        return RESEARCH_RUN_RECORD_SCHEMA_VERSION_V2
    raise ResearchRuntimeUnavailableError(
        (ResearchCapability.RUN_STORE,),
        retryable=False,
    )


def _is_recoverable_research_manifest(manifest: GraphTerminalManifestV2) -> bool:
    return (
        isinstance(manifest, GraphTerminalManifestV2)
        and manifest.graph_id.startswith("research.")
        and manifest.publication is not None
    )


def _required_checksum(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is missing")
    return value


def _recovery_artifact_refs(
    outcome: HarnessSideEffectOutcome,
    *,
    run_id: str,
) -> dict[str, str]:
    raw = outcome.metadata.get("artifact_refs")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError("Research terminal outcome has no artifact refs")
    refs: dict[str, str] = {}
    for artifact_type, ref in raw.items():
        if not isinstance(artifact_type, str) or not artifact_type.strip():
            raise ValueError("Research terminal artifact type is invalid")
        expected = f"artifact://{run_id}/{artifact_type}"
        if ref != expected:
            raise ValueError("Research terminal artifact ref is not run bound")
        refs[artifact_type] = ref
    if not {
        "research-analysis",
        "research-quality-result",
        "harness-trace",
        "harness-transcript",
    }.issubset(refs):
        raise ValueError("Research terminal artifact group is incomplete")
    if set(outcome.public_refs) != set(refs.values()):
        raise ValueError("Research terminal public refs conflict with artifact map")
    return refs


def _required_artifact_payload(
    payloads: Mapping[str, Mapping[str, Any]],
    artifact_type: str,
) -> dict[str, Any]:
    envelope = payloads.get(artifact_type)
    if not isinstance(envelope, Mapping):
        raise ValueError(f"Research recovery artifact is missing: {artifact_type}")
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError(f"Research recovery artifact payload is invalid: {artifact_type}")
    return dict(payload)


def _optional_artifact_payload(
    payloads: Mapping[str, Mapping[str, Any]],
    artifact_type: str,
) -> dict[str, Any] | None:
    envelope = payloads.get(artifact_type)
    if envelope is None:
        return None
    return _required_artifact_payload(payloads, artifact_type)


def _recovery_actor_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise ValueError("Research recovery trace metadata is invalid")
    result = {
        key: metadata.get(key)
        for key in ("tenant_id", "user_id", "memory_namespace")
        if key in metadata
    }
    if not isinstance(result.get("memory_namespace"), str) or not str(
        result["memory_namespace"]
    ).strip():
        raise ValueError("Research recovery actor scope is incomplete")
    return result


def _research_source_policy(
    base: SourceFetchPolicy,
    *,
    timeout_seconds: float,
    max_bytes: int,
    rate_limit_per_domain_per_minute: int | None = None,
) -> SourceFetchPolicy:
    return replace(
        base,
        timeout_seconds=timeout_seconds,
        max_bytes=max_bytes,
        rate_limit_per_domain_per_minute=(
            base.rate_limit_per_domain_per_minute
            if rate_limit_per_domain_per_minute is None
            else rate_limit_per_domain_per_minute
        ),
    )


def _configure_research_source_connector(
    connector: Any,
    policy: SourceFetchPolicy,
) -> ArxivSourceConnector:
    if not isinstance(connector, ArxivSourceConnector):
        raise ResearchRuntimeUnavailableError(
            (ResearchCapability.SOURCE_PROVIDER,),
            retryable=False,
        )
    # SourceRuntimeComposition owns this Research-specific connector. Applying
    # the validated policy here preserves its shared reservation ledger while
    # keeping parser/package limits settings-driven.
    connector.fetch_policy = policy
    return connector


def _build_research_pdf_parser(
    settings: ResearchParserSettings,
    *,
    execution_composition: Any | None = None,
    execution_evidence_store: ResearchArtifactStorePort | None = None,
    allow_selectable_text_fallback: bool = True,
) -> DocumentParserPort:
    factories: dict[str, Callable[[], Any]] = {
        "marker": MarkerPdfDocumentParser,
    }
    profile_ids = {
        "marker": RESEARCH_MARKER_PROFILE_ID,
    }
    recovery = settings.recovery
    recovery_policy = DocumentRecoveryPolicy(
        enabled=recovery.enabled,
        parser_recovery_enabled=recovery.parser_recovery_enabled,
        formula_roi_enabled=recovery.formula_roi_enabled,
        table_cell_roi_enabled=recovery.table_cell_roi_enabled,
        allowed_parser_backends=recovery.allowed_backends,
        authorized_provider_ids=recovery.authorized_provider_ids,
        max_attempts=recovery.max_attempts,
        max_pages=recovery.max_pages,
        max_regions=recovery.max_regions,
        max_seconds=recovery.max_seconds,
        max_memory_bytes=recovery.max_memory_bytes,
        max_cpu_seconds=recovery.max_cpu_seconds,
        max_turns=recovery.max_turns,
        max_replans=recovery.max_replans,
    )
    resolved_profiles = {
        backend: execution_composition.resolve_profile(profile_ids[backend])
        for backend in settings.backends
        if backend != "pymupdf" and execution_composition is not None
    }
    backends = [
        (
            backend,
            factories[backend](
                command_runner=(
                    ResearchParserExecutionAdapter(
                        execution_environment=execution_composition.execution_registry,
                        profile=resolved_profiles[backend],
                        resource_limits={
                            "max_memory_bytes": recovery.max_memory_bytes,
                            "max_processes": 1,
                            **(
                                {"max_cpu_seconds": recovery.max_cpu_seconds}
                                if recovery.enabled
                                else {}
                            ),
                        },
                        max_timeout_seconds=min(
                            settings.timeout_seconds,
                            recovery.max_seconds if recovery.enabled else settings.timeout_seconds,
                        ),
                        max_attempts=recovery.max_attempts,
                        allowed_backends=(backend,),
                        execution_evidence_store=execution_evidence_store,
                    )
                    if execution_composition is not None
                    else None
                ),
                reading_order_profile=MarkerReadingOrderProfile(
                    settings.reading_order_profile
                ),
            ),
        )
        for backend in settings.backends
        if backend != "pymupdf"
    ]
    if not backends:
        return PyMuPDFTextDocumentParser()
    if not allow_selectable_text_fallback:
        if len(backends) != 1:
            raise ValueError(
                "production parsing evaluation requires exactly one physical parser backend"
            )
        return backends[0][1]
    return CascadeDocumentParser(
        backends,
        fallback=PyMuPDFTextDocumentParser(),
        recovery_policy=recovery_policy,
        backend_provider_ids={
            backend: profile.provider_id
            for backend, profile in resolved_profiles.items()
            if profile.provider_id is not None
        },
        backend_profile_ids={
            backend: profile_ids[backend]
            for backend in resolved_profiles
        },
    )


def _build_research_multi_format_parser(
    settings: ResearchParserSettings,
    *,
    execution_composition: Any | None = None,
    execution_evidence_store: ResearchArtifactStorePort | None = None,
) -> CascadeArxivDocumentParser:
    return CascadeArxivDocumentParser(
        pdf_parser=_build_research_pdf_parser(
            settings,
            execution_composition=execution_composition,
            execution_evidence_store=execution_evidence_store,
        )
    )


def _compose_component(
    capability: ResearchCapability,
    factory: Callable[[], Any],
) -> Any:
    try:
        return factory()
    except ResearchCompositionError:
        raise
    except Exception:
        raise ResearchRuntimeUnavailableError(
            (capability,),
            retryable=True,
        ) from None


_DEFAULT_RESEARCH_RUNTIME_PROVIDER = ResearchRuntimeProvider()



def _research_runtime_config_identity(settings: ResearchRuntimeSettings) -> str:
    def origin(value: str | None) -> str | None:
        if not value:
            return None
        parsed = urlsplit(value)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else str(value)

    payload = {
        "source": {
            "provider": settings.source.provider,
            "apiOrigin": origin(settings.source.api_url),
        },
        "llm": {
            "routeId": settings.llm.route_id,
            "provider": settings.llm.provider,
            "baseOrigin": origin(settings.llm.base_url),
            "model": settings.llm.model,
        },
        "parser": {
            "backends": list(settings.parser.backends),
            "allowAbstractFallback": settings.parser.allow_abstract_fallback,
            "maxDocumentBytes": settings.parser.max_document_bytes,
            "readingOrderProfile": settings.parser.reading_order_profile,
            "recovery": {
                "enabled": settings.parser.recovery.enabled,
                "parserRecoveryEnabled": settings.parser.recovery.parser_recovery_enabled,
                "formulaRoiEnabled": settings.parser.recovery.formula_roi_enabled,
                "tableCellRoiEnabled": settings.parser.recovery.table_cell_roi_enabled,
                "allowedBackends": list(settings.parser.recovery.allowed_backends),
                "authorizedProviders": list(
                    settings.parser.recovery.authorized_provider_ids
                ),
                "maxAttempts": settings.parser.recovery.max_attempts,
                "maxPages": settings.parser.recovery.max_pages,
                "maxRegions": settings.parser.recovery.max_regions,
                "maxSeconds": settings.parser.recovery.max_seconds,
                "maxMemoryBytes": settings.parser.recovery.max_memory_bytes,
                "maxCpuSeconds": settings.parser.recovery.max_cpu_seconds,
                "maxProcesses": 1,
                "maxTurns": settings.parser.recovery.max_turns,
                "maxReplans": settings.parser.recovery.max_replans,
            },
        },
        "rag": {
            "backend": settings.rag.backend,
            "collection": settings.rag.collection,
            "vectorSize": settings.rag.vector_size,
        },
        "storage": {
            "researchRoot": str(settings.research_root),
            "artifactRoot": str(settings.artifact.root),
            "runStoreRoot": str(settings.run_store.root),
            "ragLocalRoot": str(settings.rag.local_root),
            "ragTranscriptRoot": str(settings.rag.transcript_root),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

__all__ = [
    "ConfiguredCompositionFactory",
    "ResearchDynamicRolloutAdmissionPort",
    "ResearchDynamicRolloutAdmissionReceipt",
    "ResearchRuntimeComposition",
    "ResearchRuntimeProvider",
    "build_research_application_service",
    "build_research_runtime_composition",
    "build_default_harness_wait_service",
    "close_default_research_runtime",
    "default_source_runtime_provider",
    "default_research_runtime_provider",
    "get_default_paper_rag_runtime_resources",
    "get_default_research_reranker",
    "preload_default_research_reranker",
    "reset_default_research_runtime",
]
