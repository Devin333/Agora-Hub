from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from threading import Condition, Lock
from typing import Any

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
from backend.research.application.run_disposition import (
    ResearchRunDispositionReconciler,
    classify_research_run_record,
)
from backend.research.application.single_paper_runtime import (
    AnalyzePaperRequest,
    ResearchAnalysisResult,
    ResearchSinglePaperRuntime,
    build_research_harness_run_spec,
)
from backend.research.graphs import (
    build_dynamic_paper_analysis_graph_definition,
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
from backend.research.document.format_router import MultiFormatDocumentParser
from backend.research.document.chunk_manifest import ChunkManifestManager
from backend.research.document.chunker import PaperDocumentChunker
from backend.research.document.latex_compiler import ArxivLatexDocumentCompiler
from backend.research.document.marker_pdf_parser import MarkerPdfDocumentParser
from backend.research.document.mineru_pdf_parser import MinerUPdfDocumentParser
from backend.research.rag.adapters.memory_port import ResearchRAGMemoryPort
from backend.research.ports.artifact_publication import (
    RESEARCH_ARTIFACT_MANIFEST_VERSION,
    ResearchArtifactDiagnosticClaim,
    ResearchArtifactReadClaim,
    ResearchArtifactReadResolution,
)
from backend.research.ports.chunk_store import ChunkStorePort
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
from interfaces.composition.runtime_execution import (
    RESEARCH_MARKER_PROFILE_ID,
    RESEARCH_MINERU_PROFILE_ID,
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
from infrastructure.research.source_provider import ArxivResearchSourceProvider
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
from backend.research.graphs import (
    RESEARCH_DYNAMIC_CAPABILITIES,
    RESEARCH_DYNAMIC_STAGE_ID,
    RESEARCH_DYNAMIC_SUBAGENT_IDS,
    build_paper_analysis_gate_registry,
    build_research_analysis_capability_registry,
    build_research_analysis_task_gate_registry,
    build_research_analysis_task_plan_policy,
    ResearchAnalysisTaskPlanStageWorker,
    ResearchAnalysisPlanCandidateBuilder,
)
from interfaces.composition.research_errors import (
    ResearchCapability,
    ResearchCompositionError,
    ResearchConfigurationError,
    ResearchRuntimeUnavailableError,
)
from interfaces.composition.research_settings import (
    ResearchParserSettings,
    ResearchRAGSettings,
    ResearchRuntimeSettings,
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
        method = self._methods.get(self.worker_id)
        if method is None:
            raise RuntimeError("Research dynamic capability binding is unavailable")
        return method(
            self._dependencies,
            task,
            self._workspace,
            execution_identity=execution_identity,
        )


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
        outputs={},
        worker_result=worker_result,
        budget=HarnessBudgetSnapshot.from_budget(HarnessBudget.safe_default()),
    )


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
        "_resources",
        "_service",
        "_settings",
        "_source_runtime_provider",
    )

    def __init__(
        self,
        *,
        settings: ResearchRuntimeSettings | None,
        service: ResearchApplicationService,
        source_runtime_provider: SourceRuntimeProvider,
        resources: Iterable[Any] = (),
        availability_error: ResearchCompositionError | None = None,
        graph_artifact_governance_service: (
            ResearchGraphArtifactGovernanceService | None
        ) = None,
        reader_repair_service: ReaderRepairGraphApplicationService | None = None,
        harness_wait_runtime_registry: HarnessWaitRuntimeRegistry | None = None,
        harness_wait_approval_resolver: DurableHarnessWaitApprovalResolver | None = None,
        harness_wait_actor_scope_resolver: _ResearchHarnessWaitActorScopeResolver | None = None,
        execution_composition: Any | None = None,
    ) -> None:
        if settings is not None and not isinstance(settings, ResearchRuntimeSettings):
            raise TypeError("settings must be ResearchRuntimeSettings")
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
        self._resources = tuple(unique_resources)
        self._availability_error = availability_error
        self._graph_artifact_governance_service = (
            graph_artifact_governance_service
        )
        self._reader_repair_service = reader_repair_service
        self._harness_wait_runtime_registry = harness_wait_runtime_registry
        self._harness_wait_approval_resolver = harness_wait_approval_resolver
        self._harness_wait_actor_scope_resolver = harness_wait_actor_scope_resolver
        self._close_lock = Lock()
        self._closed = False

    @property
    def settings(self) -> ResearchRuntimeSettings | None:
        return self._settings

    @property
    def service(self) -> ResearchApplicationService:
        return self._service

    @property
    def source_runtime_provider(self) -> SourceRuntimeProvider:
        return self._source_runtime_provider

    @property
    def execution_composition(self) -> Any | None:
        """Process-scoped execution composition used by external adapters."""

        return self._execution_composition

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
        "_service",
    )

    def __init__(self, ask_use_case: AskPaperUseCase) -> None:
        self._ask_use_case = ask_use_case
        self._closed = False
        self._lock = Lock()
        self._runtime_resources: Any | None = None
        self._service: Any | None = None

    def rag_ask(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._get().rag_ask(*args, **kwargs)

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

                runtime_resources = PaperRagRuntimeResources()
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
) -> ResearchRuntimeComposition:
    if settings is not None and settings_factory is not None:
        raise ValueError("settings and settings_factory are mutually exclusive")
    provider = source_runtime_provider or SourceRuntimeProvider()

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
        composition = (configured_factory or _build_configured_composition)(
            actual_settings,
            provider,
        )
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
        source_provider = ArxivResearchSourceProvider(
            metadata_connector,
            api_url=settings.source.api_url,
            cache_size=settings.source.cache_size,
        )
        document_compiler = _compose_component(
            ResearchCapability.DOCUMENT_COMPILER,
            lambda: ResearchDocumentCompilerAdapter(
                package_connector,
                latex_compiler=ArxivLatexDocumentCompiler(package_connector),
                pdf_parser=_build_research_pdf_parser(
                    settings.parser,
                    execution_composition=execution_composition,
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
        catalog_store = FilesystemResearchCatalogStore(
            settings.research_root / "paper-catalog"
        )
        from infrastructure.research.document_repository import FilesystemResearchDocumentRepository

        documents = FilesystemResearchDocumentRepository(catalog_store)
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
            arxiv_provider=source_provider,
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
        dynamic_task_plan_store = DurableTaskPlanStore(
            durable_events.event_runtime,
            durable_events.event_store,
            artifact_store=artifact_port.store,
            tenant_id=_RESEARCH_EVENT_TENANT_ID,
        )
        from interfaces.composition.harness_memory import build_harness_ref_admission_service
        from framework.harness.ref_results import HarnessResultRefAuthority
        from framework.harness.ref_snapshot_store import DurableRefAuthoritySnapshotStore

        dynamic_ref_admission_service = build_harness_ref_admission_service(
            snapshot_store=DurableRefAuthoritySnapshotStore(
                durable_events.event_runtime,
                durable_events.event_store,
                artifact_store=artifact_port.store,
                tenant_id=_RESEARCH_EVENT_TENANT_ID,
            ),
            namespace_root=settings.artifact.root / "memory-namespaces",
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
            actor_metadata = {
                "tenant_id": workspace.request.tenant_id,
                "user_id": workspace.request.user_id,
                "memory_namespace": workspace.request.memory_namespace,
            }
            result_tenant_id = research_event_tenant_id(actor_metadata)
            try:
                child_runtime_binding = dynamic_child_runtime.start_for_run(
                    run_id=workspace.request.run_id,
                    tenant_id=result_tenant_id,
                )
            except (ResearchChildRuntimeUnavailableError, ChildAgentSupervisorError):
                raise ResearchRuntimeUnavailableError(
                    (ResearchCapability.EVENT_LOG,),
                    retryable=False,
                ) from None
            graph = HarnessGraphCompiler().compile(
                build_dynamic_paper_analysis_graph_definition()
            ).graph
            stage_binding = TaskPlanStageBinding(
                graph,
                RESEARCH_DYNAMIC_STAGE_ID,
            )
            policy = build_research_analysis_task_plan_policy()
            task_workers: dict[str, Any] = {}
            bindings: dict[str, HarnessWorkerBinding] = {}
            for capability in RESEARCH_DYNAMIC_CAPABILITIES:
                worker = _ProductionResearchAnalysisWorker(
                    capability,
                    dependencies=dependencies,
                    workspace=workspace,
                )
                task_workers[capability] = worker
                bindings[capability] = HarnessWorkerBinding(
                    HarnessContractReference(
                        HarnessContractKind.WORKER,
                        capability,
                        "1",
                    ),
                    HarnessWorkerType.SUBAGENT,
                    worker,
                )
            capability_registry = build_research_analysis_capability_registry(bindings)
            result_ref_authority = HarnessResultRefAuthority(
                dynamic_ref_admission_service.store,
                transcript_store=subagent_transcript_store,
                artifact_descriptors=graph_result_catalog,
                tenant_id=result_tenant_id,
            )
            subagent_runtime = SubAgentRuntime(
                workers={
                    RESEARCH_DYNAMIC_SUBAGENT_IDS[capability]: worker
                    for capability, worker in task_workers.items()
                },
                transcript_store=subagent_transcript_store,
                result_ref_authority=result_ref_authority,
                runtime_event_sink=runtime_event_publisher,
            )
            subagent_adapter = ResolvedSubAgentTaskAdapter(
                subagent_runtime,
                ref_admission_service=dynamic_ref_admission_service,
                dependency_result_resolver=AcceptedDependencyResultResolver(
                    store=dynamic_task_plan_store, authority=result_ref_authority, policy=policy,
                ),
            )
            # Keep Research on the same Harness-owned TaskPlan execution
            # boundary as generic AgentLoop delegation.  The stage runner
            # still owns routing and verification; this executor owns the
            # admitted invocation, dependency refs, transcript reuse, and
            # recovery identity for each physical child attempt.
            task_plan_worker_executor = HarnessSubAgentTaskExecutor(
                store=dynamic_task_plan_store,
                runtime=subagent_runtime,
                ref_admission_service=dynamic_ref_admission_service,
                task_policy=policy,
            )
            gate_registry = build_paper_analysis_gate_registry()

            def gate_context(request):
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
                        "evidence_pack": {
                            "evidence_pack": (
                                workspace.evidence_pack.to_dict()
                                if workspace.evidence_pack is not None
                                else {}
                            )
                        }
                    },
                    worker_result=request.worker_result,
                    budget=HarnessBudgetSnapshot.from_budget(HarnessBudget.safe_default()),
                )

            result_verifier = TaskPlanResultVerifier(
                build_research_analysis_task_gate_registry(
                    gate_registry,
                    context_factory=gate_context,
                ),
                transcript_store=subagent_transcript_store,
                artifact_reference_verifier=artifact_port,
                result_ref_authority=result_ref_authority,
                gate_artifact_writer=dynamic_task_plan_store,
            )
            def task_context_pack(plan, instance, execution_identity):
                if not isinstance(execution_identity, GraphExecutionIdentity):
                    raise HarnessValidationError(
                        "Research TaskPlan SubAgent requires physical Graph identity",
                        code="task_plan_execution_identity_required",
                    )
                graph_identity, task_identity = task_plan_context_identities(
                    plan,
                    instance,
                    execution_identity=execution_identity,
                )
                return ContextEnvelope.for_graph(
                    envelope_id=(
                        "research-task-plan-context:"
                        f"{instance.task_instance_id}:{execution_identity.activity_id}"
                    ),
                    graph_identity=graph_identity,
                    task_execution_identity=task_identity,
                    phase="EXECUTE",
                    worker_id="research.task-plan",
                    worker_type=HarnessWorkerType.TASK_PLAN.value,
                    dynamic_tail={
                        "input_refs": ["document", "evidence_pack"],
                        "raw_parent_messages_included": False,
                    },
                )

            if (
                settings.graph_artifact_persistence.mode
                is GraphArtifactRolloutMode.ENFORCE
            ):
                transition_port = workspace.graph_transition_port
                if not isinstance(transition_port, HarnessTransitionPort):
                    raise TypeError(
                        "dynamic TaskPlan requires the bound Graph transition port"
                    )
                if not isinstance(graph_result_materializer, ResultMaterializer):
                    raise ResearchRuntimeUnavailableError(
                        (ResearchCapability.GRAPH_ARTIFACT_PERSISTENCE,),
                        retryable=False,
                    )
                child_adapter = HarnessSubAgentResultAdapter(
                    materializer=graph_result_materializer,
                    graph_result_runtime=HarnessGraphResultRuntime(
                        HarnessGraphControlPlaneRuntime(transition_port)
                    ),
                    transcript_store=subagent_transcript_store,
                    result_ref_authority=result_ref_authority,
                )

                def child_invocation(plan, resolved, instance, execution_identity):
                    binding = capability_registry.resolve(
                        resolved.task.worker_capability,
                        policy,
                    )
                    return subagent_adapter.build_invocation(
                        plan=plan,
                        resolved_task=resolved,
                        binding=binding,
                        instance=instance,
                        context_pack=task_context_pack(
                            plan,
                            instance,
                            execution_identity,
                        ),
                        budget_snapshot=HarnessBudgetSnapshot.from_budget(
                            HarnessBudget.safe_default()
                        ),
                        execution_identity=execution_identity,
                    )

                result_verifier = ResearchTaskPlanResultMaterializer(
                    verifier=result_verifier,
                    adapter=child_adapter,
                    config=settings.graph_artifact_persistence,
                    tenant_id=result_tenant_id,
                    tenant_scope_ref=checksum_for(result_tenant_id),
                    invocation_factory=child_invocation,
                )

            return ResearchAnalysisTaskPlanStageWorker(
                stage_binding=stage_binding,
                accepted_at=utc_now().isoformat().replace("+00:00", "Z"),
                candidate_builder=ResearchAnalysisPlanCandidateBuilder(candidate_worker),
                capability_registry=capability_registry,
                store=dynamic_task_plan_store,
                worker_executor=task_plan_worker_executor,
                worker_result_recovery=task_plan_worker_executor.recover,
                result_verifier=result_verifier,
                policy=policy,
                parallel_coordinator=child_runtime_binding.parallel_coordinator,
                child_agent_supervisor=child_runtime_binding.supervisor,
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
        rag_ask_provider = _PaperRagUseCaseProvider(ask_use_case)
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
            harness_wait_runtime_registry=harness_wait_runtime_registry,
            harness_wait_approval_resolver=harness_wait_approval_resolver,
            harness_wait_actor_scope_resolver=harness_wait_actor_scope_resolver,
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
) -> CascadeDocumentParser:
    factories: dict[str, Callable[[], Any]] = {
        "marker": MarkerPdfDocumentParser,
        "mineru": MinerUPdfDocumentParser,
    }
    profile_ids = {
        "marker": RESEARCH_MARKER_PROFILE_ID,
        "mineru": RESEARCH_MINERU_PROFILE_ID,
    }
    backends = [
        (
            backend,
            factories[backend](
                command_runner=(
                    ResearchParserExecutionAdapter(
                        execution_environment=execution_composition.execution_registry,
                        profile=execution_composition.resolve_profile(profile_ids[backend]),
                    )
                    if execution_composition is not None
                    else None
                )
            ),
        )
        for backend in settings.backends
        if backend != "pymupdf"
    ]
    return CascadeDocumentParser(
        backends,
        fallback=PyMuPDFTextDocumentParser(),
    )


def _build_research_multi_format_parser(
    settings: ResearchParserSettings,
    *,
    execution_composition: Any | None = None,
) -> CascadeArxivDocumentParser:
    return CascadeArxivDocumentParser(
        pdf_parser=_build_research_pdf_parser(
            settings,
            execution_composition=execution_composition,
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


__all__ = [
    "ConfiguredCompositionFactory",
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
