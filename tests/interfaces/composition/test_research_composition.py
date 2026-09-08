from __future__ import annotations

import inspect
import json
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

import interfaces.composition.research as research_composition
from backend.research.application.analyze_paper import AnalyzePaperUseCase
from backend.research.application.ask_paper import AskPaperUseCase
from backend.research.application.bounded_document_rag import (
    BoundedDocumentRAGRuntime,
)
from backend.research.application.graph_result_committer import (
    ResearchGraphResultCommitter,
    ResearchTaskPlanResultMaterializer,
)
from backend.research.application.reader_repair_runtime import (
    ReaderRepairGraphApplicationService,
)
from backend.research.application.artifact_context import (
    ResearchGraphArtifactContextProvider,
)
from backend.research.application.single_paper_runtime import (
    AnalyzePaperRequest,
    ResearchSinglePaperRuntime,
    _ResearchRunWorkspace,
)
from backend.research.document.cascade_parser import CascadeDocumentParser
from backend.research.domain.paper import PaperSourceRecord
from backend.research.document.chunk_storage import PaperChunkStoreAdapter
from backend.research.document.latex_compiler import ArxivLatexDocumentCompiler
from framework.execution_environment import (
    ExecutionCapabilityProfile,
    ExecutionEnvironmentRegistry,
    ExecutionEnvironmentUnavailableError,
    ExecutionProfile,
    ExecutionProfileRegistry,
    FakeExecutionEnvironment,
    RuntimeCompositionManifest,
    RuntimeExecutionComposition,
)
from framework.harness import ArtifactReferenceVerifierPort, ContextAssembler
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.ref_admission import HarnessRefAdmissionService
from framework.harness.ref_snapshot_store import DurableRefAuthoritySnapshotStore
from framework.harness.subagents.supervisor import ChildAgentSupervisor
from framework.harness.task_plan.parallel import ParallelAgentCoordinator
from framework.harness.control_plane.durable_events import (
    DurableHarnessTransitionPort,
)
from framework.harness.runtime import (
    GraphArtifactRolloutMode,
    ResultMaterializer,
)
from infrastructure.external.sources.arxiv import (
    ArxivConnector,
    ArxivSourceConnector,
)
from infrastructure.external.sources.github import GithubConnector
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from infrastructure.research.artifact_publication import ResearchArtifactBundleHandler
from infrastructure.research.candidate_worker import (
    StructuredResearchCandidateWorker,
)
from infrastructure.research.document_compiler import (
    ResearchDocumentCompilerAdapter,
)
from infrastructure.research.filesystem_run_store import (
    FilesystemResearchRunStore,
)
from infrastructure.research.github_repository import (
    GithubResearchRepositoryAdapter,
)
from infrastructure.research.local_chunk_store import LocalChunkPayloadStore
from infrastructure.research.source_provider import ArxivResearchSourceProvider
from infrastructure.storage.harness import SQLiteHarnessSideEffectStore
from infrastructure.storage.artifacts import (
    LocalJsonArtifactCatalog,
    SQLiteGraphResultStore,
)
from interfaces.composition.research import (
    ResearchRuntimeComposition,
    ResearchRuntimeProvider,
    build_research_application_service,
    build_research_runtime_composition,
    close_default_research_runtime,
    default_research_runtime_provider,
    reset_default_research_runtime,
)
from interfaces.composition.research_settings import ResearchRuntimeSettings
from interfaces.composition.runtime_execution import (
    RESEARCH_MARKER_PROFILE_ID,
    RESEARCH_MINERU_PROFILE_ID,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from interfaces.services.research_service import (
    InMemoryResearchRunStore,
    ResearchAnalyzeInput,
    ResearchApplicationService,
    ResearchRunRecord,
    ResearchServiceError,
)
from interfaces.services.source_runtime import SourceRuntimeProvider
from tests.interfaces.research_fixtures import FakeAnalyzeUseCase


class _ExplicitRunStore:
    def __init__(self) -> None:
        self.records: dict[str, ResearchRunRecord] = {}

    def save(self, record: ResearchRunRecord) -> None:
        self.records[record.run_id] = record

    def get_by_run_id(self, run_id: str) -> ResearchRunRecord | None:
        return self.records.get(run_id)

    def get_latest_by_paper_id(self, paper_id: str) -> ResearchRunRecord | None:
        matches = [record for record in self.records.values() if record.paper_id == paper_id]
        return matches[-1] if matches else None

    def list_by_paper_id(self, paper_id: str) -> list[ResearchRunRecord]:
        matches = [record for record in self.records.values() if record.paper_id == paper_id]
        return list(reversed(matches))


class _Closable:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _BlockingClosable(_Closable):
    def __init__(self) -> None:
        super().__init__()
        self.started = Event()
        self.release = Event()

    def close(self) -> None:
        self.close_calls += 1
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("blocking close was not released")


class _ReaderRepairMemoryAdapter:
    def recall_cases(self, _query):
        return ()

    def recall_strategies(self, _issue_type, *, namespace):
        del namespace
        return []


class _ReaderRepairMemoryCommitAdapter:
    def commit(self, _request):
        raise AssertionError("Reader Repair memory commit was not expected")


class _ReaderRepairFailureCommitAdapter:
    def commit_failure_diagnostic(self, _request):
        raise AssertionError("Reader Repair diagnostic commit was not expected")


def _settings(tmp_path: Path) -> ResearchRuntimeSettings:
    return ResearchRuntimeSettings.from_env(
        {"DASHSCOPE_API_KEY": "sk-explicit-test-only"},
        cwd=tmp_path,
    )


def test_provider_caches_explicit_composition_and_reuses_source_provider(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source_provider = SourceRuntimeProvider()
    resources: list[_Closable] = []
    calls: list[tuple[ResearchRuntimeSettings, SourceRuntimeProvider]] = []

    def configured_factory(
        actual_settings: ResearchRuntimeSettings,
        actual_source_provider: SourceRuntimeProvider,
    ) -> ResearchRuntimeComposition:
        calls.append((actual_settings, actual_source_provider))
        resource = _Closable()
        resources.append(resource)
        return ResearchRuntimeComposition(
            settings=actual_settings,
            service=ResearchApplicationService(
                analyze_use_case=FakeAnalyzeUseCase(),
                run_store=_ExplicitRunStore(),
            ),
            source_runtime_provider=actual_source_provider,
            resources=(resource, resource),
        )

    provider = ResearchRuntimeProvider(
        settings_factory=lambda: settings,
        configured_factory=configured_factory,
        source_runtime_provider=source_provider,
    )

    first = provider.get()
    second = provider.get()

    assert first is second
    assert provider.service_factory() is first.service
    assert first.resources == (resources[0],)
    assert calls == [(settings, source_provider)]
    assert (
        calls[0][0].graph_artifact_persistence
        is settings.graph_artifact_persistence
    )
    assert (
        first.settings.graph_artifact_persistence.policy_version
        == "graph-artifact-policy@1"
    )
    assert provider.initialized is True

    provider.reset()
    assert resources[0].close_calls == 1
    assert provider.initialized is False
    assert provider.get() is not first
    assert calls[-1] == (settings, source_provider)

    provider.close()
    assert resources[1].close_calls == 1
    assert provider.closed is True
    with pytest.raises(RuntimeError, match="ResearchRuntimeProvider is closed"):
        provider.get()

    provider.reset()
    assert provider.closed is False
    assert provider.get().service is not first.service
    provider.close()


def test_missing_configuration_returns_sanitized_typed_unavailable_service(
    tmp_path: Path,
) -> None:
    secret_url = "https://private-research-host.example/v1"
    secret_path = tmp_path / "private-run-root"
    source_provider = SourceRuntimeProvider()
    provider = ResearchRuntimeProvider(
        settings_factory=lambda: ResearchRuntimeSettings.from_env(
            {
                "NEWS_RESEARCH_LLM_BASE_URL": secret_url,
                "NEWS_RESEARCH_LLM_MODEL": "private-model",
                "NEWS_RESEARCH_LLM_API_KEY_ENV": "MISSING_PRIVATE_KEY",
                "NEWS_RESEARCH_RUN_STORE_ROOT": str(secret_path),
            },
            cwd=tmp_path,
        ),
        source_runtime_provider=source_provider,
    )

    composition = provider.get()
    service = composition.service

    assert composition.available is False
    assert composition.settings is None
    assert composition.source_runtime_provider is source_provider
    assert type(service._analyze_use_case).__name__ != "_UnconfiguredAnalyzeUseCase"
    assert not isinstance(service._run_store, InMemoryResearchRunStore)

    with pytest.raises(ResearchServiceError) as exc_info:
        service.analyze_paper(
            ResearchAnalyzeInput(
                paper_id="2607.00001",
                source_url="https://arxiv.org/abs/2607.00001",
            )
        )

    error = exc_info.value
    assert error.code == "research_runtime_unavailable"
    assert error.status_code == 503
    assert error.details == {
        "capabilities": ["research.llm.credential"],
        "remediation": {
            "code": "configure_research_llm_credential",
            "message": (
                "Provide the configured Research LLM credential through deployment secret "
                "management."
            ),
        },
    }
    public = json.dumps(
        {
            "code": error.code,
            "message": error.message,
            "details": error.details,
        },
        sort_keys=True,
    )
    assert secret_url not in public
    assert str(secret_path) not in public
    assert "private-model" not in public
    assert "MISSING_PRIVATE_KEY" not in public


def test_valid_settings_without_activity_key_fail_closed_as_event_log_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("NEWS_ACTIVITY_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("NEWS_DATABASE_DSN", raising=False)
    settings = _settings(tmp_path)
    composition = build_research_runtime_composition(settings=settings)

    assert composition.available is False
    assert composition.settings == settings
    assert composition.availability_error is not None
    assert composition.availability_error.capabilities == ("research.event_log",)
    assert type(composition.service._analyze_use_case).__name__ != (
        "_UnconfiguredAnalyzeUseCase"
    )
    assert not isinstance(composition.service._run_store, InMemoryResearchRunStore)
    event_database = settings.artifact.root / "_records" / "events.sqlite3"
    moved_database = event_database.with_name("events-after-failure.sqlite3")
    event_database.replace(moved_database)
    moved_database.replace(event_database)
    composition.close()
    composition.close()


def test_default_research_pdf_parser_fails_closed_when_docker_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("NEWSROOM_PARSER_RUN_ROOT", str(tmp_path / "parser-runs"))
    settings = _settings(tmp_path)
    capabilities = ExecutionCapabilityProfile(
        provider_id="docker",
        available=False,
    )
    registry = ExecutionEnvironmentRegistry()
    provider = FakeExecutionEnvironment(
        capabilities,
        lambda _request: pytest.fail("unavailable Docker provider must not execute"),
    )
    registry.register(provider)
    profiles = ExecutionProfileRegistry()
    profiles.register(
        RESEARCH_MINERU_PROFILE_ID,
        ExecutionProfile.external_process(
            provider_id="docker",
            allowed_argv_prefixes=(("mineru",),),
        ),
    )
    profiles.register(
        RESEARCH_MARKER_PROFILE_ID,
        ExecutionProfile.external_process(
            provider_id="docker",
            allowed_argv_prefixes=(("marker_single",),),
        ),
    )
    manifest = RuntimeCompositionManifest.from_registries(
        composition_id="research-parser-unavailable",
        profile_registry=profiles,
        execution_registry=registry,
    )
    execution_composition = RuntimeExecutionComposition(
        manifest=manifest,
        profile_registry=profiles,
        execution_registry=registry,
        required_provider_ids=("docker",),
    )
    parser = research_composition._build_research_pdf_parser(
        settings.parser,
        execution_composition=execution_composition,
    )
    compiler = ResearchDocumentCompilerAdapter(
        SimpleNamespace(
            fetch_pdf_package=lambda _arxiv_id: SimpleNamespace(
                content=b"%PDF-1.7\n",
                checksum="recorded-pdf-checksum",
            )
        ),
        pdf_parser=parser,
        allow_abstract_fallback=True,
    )
    source = PaperSourceRecord(
        source_id="arxiv:2606.00001",
        paper_id="2606.00001",
        source_type="arxiv",
        source_url="https://arxiv.org/abs/2606.00001",
        source_hash="a" * 64,
        metadata={
            "arxiv_id": "2606.00001",
            "title": "Harness-grounded Research",
            "abstract": "This fallback must not be used.",
        },
    )
    identity = GraphExecutionIdentity(
        run_id="run-parser-unavailable",
        graph_id="research-graph",
        graph_version="1.0.0",
        graph_ref="research-graph@1.0.0",
        graph_checksum="sha256:" + "a" * 64,
        node_id="compile_document",
        node_instance_id="compile-document-1",
        activity_id="compile-document-activity",
        attempt=1,
    )

    with pytest.raises(ExecutionEnvironmentUnavailableError) as raised:
        compiler.compile(source, execution_identity=identity)

    assert raised.value.reason_code == "execution_environment_unavailable"
    assert provider.requests == []


def test_valid_settings_compose_full_durable_production_graph(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.delenv("NEWS_DATABASE_DSN", raising=False)
    monkeypatch.setenv(
        "NEWS_ACTIVITY_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(settings.artifact.root))
    monkeypatch.setenv(settings.llm.api_key_env, "sk-composition-object-graph-only")

    composition = build_research_runtime_composition(settings=settings)

    try:
        assert composition.available is True
        assert composition.availability_error is None
        service = composition.service
        assert isinstance(service._analyze_use_case, AnalyzePaperUseCase)
        assert isinstance(service._run_store, FilesystemResearchRunStore)
        assert service._run_store.root == settings.run_store.root
        assert service._run_store.write_schema_version.endswith(".v2")
        assert service._run_store.supported_schema_versions == (
            "newsroom.research_run_record.v1",
            "newsroom.research_run_record.v2",
        )
        assert service._run_reconciler is not None
        assert composition.reader_repair_service is None

        runtime = service._analyze_use_case._runtime
        assert isinstance(runtime, ResearchSinglePaperRuntime)
        assert runtime.context_assembler is None
        assert runtime.context_assembler_factory is not None
        assert runtime.context_max_input_tokens == settings.llm.max_input_tokens
        assert runtime.context_max_output_tokens == settings.llm.max_output_tokens
        assert isinstance(runtime.source_provider, ArxivResearchSourceProvider)
        assert isinstance(runtime.source_provider._connector, ArxivConnector)
        assert isinstance(runtime.document_compiler, ResearchDocumentCompilerAdapter)
        assert isinstance(
            runtime.document_compiler._latex_compiler,
            ArxivLatexDocumentCompiler,
        )
        assert isinstance(runtime.document_compiler._pdf_parser, CascadeDocumentParser)
        assert isinstance(runtime.llm_worker, StructuredResearchCandidateWorker)
        assert isinstance(
            runtime.github_repository,
            GithubResearchRepositoryAdapter,
        )
        assert isinstance(runtime.github_repository._connector, GithubConnector)
        assert isinstance(runtime.rag_runtime, BoundedDocumentRAGRuntime)
        assert isinstance(runtime.rag_runtime._chunk_store, PaperChunkStoreAdapter)
        assert isinstance(
            runtime.rag_runtime._chunk_store._store,
            LocalChunkPayloadStore,
        )
        assert isinstance(runtime.artifact_port, FilesystemHarnessArtifactPort)
        assert runtime.artifact_port.root == settings.artifact.root
        assert service._diagnostic_artifact_reader is runtime.artifact_port
        assert runtime.artifact_port._diagnostic_run_resolver is not None
        assert isinstance(
            runtime.event_port_factory("research-object-graph"),
            DurableHarnessTransitionPort,
        )

        candidate_workspace = _ResearchRunWorkspace(
                request=AnalyzePaperRequest(
                    run_id="candidate-object-graph",
                    paper_id="paper-candidate-object-graph",
                    source_ref="https://arxiv.org/abs/2606.00001",
                    tenant_id="tenant-candidate-object-graph",
                    user_id="user-candidate-object-graph",
                    memory_namespace="candidate-object-graph",
                ),
            context_assembler=ContextAssembler(),
            graph_transition_port=runtime.event_port_factory(
                "candidate-object-graph"
            ),
        )
        dynamic_stage = runtime.dynamic_task_plan_runner_factory(
            workspace=candidate_workspace,
            dependencies=object(),
        )
        assert isinstance(
            dynamic_stage._parallel_coordinator,
            ParallelAgentCoordinator,
        )
        assert isinstance(
            dynamic_stage._child_agent_supervisor,
            ChildAgentSupervisor,
        )
        assert (
            dynamic_stage._parallel_coordinator.max_workers
            == dynamic_stage._child_agent_supervisor.capacity
            == dynamic_stage._policy.max_parallelism
        )
        admission = dynamic_stage._ref_admission_service
        assert isinstance(admission, HarnessRefAdmissionService)
        assert isinstance(admission.store, DurableRefAuthoritySnapshotStore)
        assert admission.store.is_durable is True
        assert admission.store._artifacts is runtime.artifact_port.store
        stage_worker_type = research_composition.ResearchAnalysisTaskPlanStageWorker
        volatile_admission = HarnessRefAdmissionService(SimpleNamespace(
            commit=admission.store.commit,
            get=admission.store.get,
            find=admission.store.find,
        ))
        for unavailable_admission in (None, volatile_admission):
            def stage_without_durable_authority(**kwargs):
                return stage_worker_type(**{
                    **kwargs, "ref_admission_service": unavailable_admission,
                })

            with monkeypatch.context() as scope:
                scope.setattr(
                    research_composition,
                    "ResearchAnalysisTaskPlanStageWorker",
                    stage_without_durable_authority,
                )
                with pytest.raises(HarnessValidationError) as error:
                    runtime.dynamic_task_plan_runner_factory(
                        workspace=candidate_workspace, dependencies=object(),
                    )
                assert error.value.code == "research_task_plan_ref_authority_required"
        configured_verifier = dynamic_stage._runner.result_verifier
        assert configured_verifier.result_ref_authority.store is admission.store
        assert configured_verifier.result_ref_authority.is_durable is True
        def stage_without_result_authority(**kwargs):
            kwargs["result_verifier"].result_ref_authority = None
            return stage_worker_type(**kwargs)

        with monkeypatch.context() as scope:
            scope.setattr(research_composition, "ResearchAnalysisTaskPlanStageWorker", stage_without_result_authority)
            with pytest.raises(HarnessValidationError) as error:
                runtime.dynamic_task_plan_runner_factory(workspace=candidate_workspace, dependencies=object())
            assert error.value.code == "research_task_plan_result_authority_required"
        while hasattr(configured_verifier, "_verifier"):
            configured_verifier = configured_verifier._verifier
        configured_verifier = configured_verifier._artifact_reference_verifier
        assert configured_verifier is runtime.artifact_port
        assert isinstance(configured_verifier, ArtifactReferenceVerifierPort)
        publish_worker = runtime._worker_registry(candidate_workspace)[
            "publish_artifacts"
        ]
        closure = inspect.getclosurevars(publish_worker)
        assert "self" not in closure.nonlocals
        reachable = (
            tuple(closure.globals.values())
            + tuple(closure.nonlocals.values())
            + tuple(publish_worker.__defaults__ or ())
        )
        assert not any(
            isinstance(
                value,
                (
                    FilesystemHarnessArtifactPort,
                    ResearchArtifactBundleHandler,
                    SQLiteHarnessSideEffectStore,
                ),
            )
            for value in reachable
        )

        source_runtime = composition.source_runtime_provider.get()
        package_connector = source_runtime.research_arxiv_connector
        assert isinstance(package_connector, ArxivSourceConnector)
        assert runtime.document_compiler._source_fetcher is package_connector
        assert runtime.document_compiler._latex_compiler._fetcher is package_connector
        assert runtime.source_provider._connector._rate_limiter is (
            source_runtime.reservation_ledger
        )
        assert package_connector._rate_limiter is source_runtime.reservation_ledger
        assert runtime.github_repository._connector._rate_limiter is (
            source_runtime.reservation_ledger
        )
        general_arxiv_connector = source_runtime.source_service.arxiv_connector
        assert general_arxiv_connector is source_runtime.source_router.arxiv_connector
        assert general_arxiv_connector is not package_connector
        assert general_arxiv_connector.fetch_policy is source_runtime.fetch_policy
        assert general_arxiv_connector.fetch_policy is not package_connector.fetch_policy
        assert package_connector.fetch_policy.max_bytes == min(
            settings.source.package_max_bytes,
            settings.parser.max_document_bytes,
        )
        assert package_connector.fetch_policy.timeout_seconds == (
            settings.source.timeout_seconds
        )
        assert runtime.source_provider._connector.fetch_policy.max_bytes == (
            settings.source.metadata_max_bytes
        )
        assert runtime.llm_worker._client._retry_policy.max_attempts == (
            settings.llm.max_attempts
        )

        graph = (
            service._analyze_use_case,
            service._run_store,
            runtime,
            runtime.source_provider,
            runtime.document_compiler,
            runtime.llm_worker,
            runtime.github_repository,
            runtime.rag_runtime,
            runtime.artifact_port,
        )
        assert not any("fake" in type(component).__module__.casefold() for component in graph)
        assert not any(
            "paper_radar" in type(component).__module__
            for component in graph
        )
        assert not isinstance(service._run_store, InMemoryResearchRunStore)
        assert (
            settings.artifact.root / "_records" / "events.sqlite3"
        ).is_file()
        event_database = settings.artifact.root / "_records" / "events.sqlite3"
        moved_database = event_database.with_name("events-after-close.sqlite3")
        composition.close()
        composition.close()
        event_database.replace(moved_database)
        moved_database.replace(event_database)
    finally:
        composition.close()


def test_production_composition_registers_reader_repair_only_with_durable_ports(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.delenv("NEWS_DATABASE_DSN", raising=False)
    monkeypatch.setenv(
        "NEWS_ACTIVITY_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(settings.artifact.root))
    monkeypatch.setenv(settings.llm.api_key_env, "sk-composition-reader-repair")
    monkeypatch.setattr(
        research_composition,
        "build_reader_repair_memory_from_env",
        lambda: _ReaderRepairMemoryAdapter(),
    )
    monkeypatch.setattr(
        research_composition,
        "build_reader_repair_memory_commit_port_from_env",
        lambda: _ReaderRepairMemoryCommitAdapter(),
    )
    monkeypatch.setattr(
        research_composition,
        "build_reader_repair_failure_diagnostic_commit_port_from_env",
        lambda: _ReaderRepairFailureCommitAdapter(),
    )

    composition = build_research_runtime_composition(settings=settings)
    try:
        assert isinstance(
            composition.reader_repair_service,
            ReaderRepairGraphApplicationService,
        )
    finally:
        composition.close()


def test_enforce_mode_composes_real_graph_result_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = ResearchRuntimeSettings.from_env(
        {
            "DASHSCOPE_API_KEY": "sk-explicit-test-only",
            "NEWS_RESEARCH_GRAPH_ARTIFACT_MODE": "enforce",
        },
        cwd=tmp_path,
    )
    monkeypatch.delenv("NEWS_DATABASE_DSN", raising=False)
    monkeypatch.setenv(
        "NEWS_ACTIVITY_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv("NEWS_ARTIFACT_ROOT", str(settings.artifact.root))
    monkeypatch.setenv(settings.llm.api_key_env, "sk-composition-graph-result")

    composition = build_research_runtime_composition(settings=settings)

    try:
        assert composition.available is True
        runtime = composition.service._analyze_use_case._runtime
        request = AnalyzePaperRequest(
            run_id="research-graph-result-composition",
            paper_id="2608.00001",
            source_ref="https://arxiv.org/abs/2608.00001",
            tenant_id="tenant-graph-result",
            user_id="user-graph-result",
            memory_namespace="research-graph-result",
        )
        event_port = runtime.event_port_factory(request.run_id)
        committer = runtime.graph_result_committer_factory(
            event_port=event_port,
            request=request,
        )

        assert settings.graph_artifact_persistence.mode is (
            GraphArtifactRolloutMode.ENFORCE
        )
        assert isinstance(committer, ResearchGraphResultCommitter)
        context_assembler = runtime.context_assembler_factory(
            request.run_id,
            event_port,
        )
        assert context_assembler.requires_approved_artifact_context is True
        assert isinstance(
            context_assembler.artifact_context_provider,
            ResearchGraphArtifactContextProvider,
        )
        assert not hasattr(runtime, "graph_result_observer_factory")

        materializer = committer._materializer
        assert isinstance(materializer, ResultMaterializer)
        assert isinstance(materializer._catalog, LocalJsonArtifactCatalog)
        assert materializer._catalog.root == (
            settings.artifact.root / "_records" / "graph_artifact_catalog"
        )
        assert isinstance(materializer._attempts, SQLiteGraphResultStore)
        assert materializer._quota is materializer._attempts
        assert materializer._cache is materializer._attempts
        assert materializer._attempts.path == (
            settings.research_root / "graph-results.sqlite3"
        ).resolve()
        governance_service = composition.graph_artifact_governance_service
        assert governance_service is not None
        governance_runtime = governance_service.runtime
        assert governance_runtime._catalog is materializer._catalog
        assert governance_runtime._ledger is materializer._attempts
        assert governance_runtime._lifecycle.artifact_port is runtime.artifact_port
        assert materializer._attempts.max_artifacts_per_tenant == (
            settings.graph_artifact_persistence.max_artifacts_per_tenant
        )
        assert materializer._attempts.max_materialized_bytes_per_tenant == (
            settings.graph_artifact_persistence.max_materialized_bytes_per_tenant
        )
        assert materializer._attempts.max_artifacts_per_class == (
            settings.graph_artifact_persistence.max_artifacts_per_class
        )
        assert materializer._attempts.max_materialized_bytes_per_class == (
            settings.graph_artifact_persistence.max_materialized_bytes_per_class
        )

        workspace = _ResearchRunWorkspace(
            request=request,
            context_assembler=ContextAssembler(),
            graph_transition_port=event_port,
        )
        dynamic_stage = runtime.dynamic_task_plan_runner_factory(
            workspace=workspace,
            dependencies=object(),
        )
        child_verifier = dynamic_stage._runner.result_verifier
        assert isinstance(child_verifier, ResearchTaskPlanResultMaterializer)
        assert child_verifier._adapter._materializer is materializer
        assert child_verifier.result_ref_authority is child_verifier._adapter.result_ref_authority
        assert child_verifier.result_ref_authority is child_verifier._verifier.result_ref_authority
        assert child_verifier.result_ref_authority._artifacts is materializer._catalog
    finally:
        composition.close()


def test_publish_fingerprint_rebuilds_from_provider_after_partial_restart() -> None:
    fingerprint = "sha256:" + "f" * 64
    calls: list[dict[str, object]] = []

    class RestartArtifactContextProvider:
        def load_artifact_context(self, request):
            calls.append(dict(request))
            return SimpleNamespace(context_fingerprint=fingerprint)

    request = AnalyzePaperRequest(
        run_id="research-context-restart",
        paper_id="2608.00002",
        source_ref="https://arxiv.org/abs/2608.00002",
        tenant_id="tenant-restart",
        user_id="user-restart",
        memory_namespace="context-restart",
    )
    workspace = _ResearchRunWorkspace(
        request=request,
        context_assembler=ContextAssembler(
            artifact_context_provider=RestartArtifactContextProvider()
        ),
    )

    resolved = research_composition._research_context_fingerprint(
        workspace,
        node_id="publish_artifacts",
    )

    assert resolved == fingerprint
    assert calls == [
        {
            "run_id": request.run_id,
            "step_id": "publish_artifacts",
            "metadata": {
                "memory_namespace": "context-restart",
                "tenant_id": "tenant-restart",
                "user_id": "user-restart",
            },
        }
    ]


def test_default_provider_is_lazy_and_reset_does_not_load_live_settings() -> None:
    reset_default_research_runtime()
    provider = default_research_runtime_provider()

    assert provider.initialized is False
    assert provider.closed is False

    reset_default_research_runtime()
    assert provider.initialized is False


def test_public_default_factory_close_and_reset_hooks_are_consistent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source_provider = SourceRuntimeProvider()
    resources: list[_Closable] = []

    def configured_factory(
        actual_settings: ResearchRuntimeSettings,
        actual_source_provider: SourceRuntimeProvider,
    ) -> ResearchRuntimeComposition:
        resource = _Closable()
        resources.append(resource)
        return ResearchRuntimeComposition(
            settings=actual_settings,
            service=ResearchApplicationService(
                analyze_use_case=FakeAnalyzeUseCase(),
                run_store=_ExplicitRunStore(),
            ),
            source_runtime_provider=actual_source_provider,
            resources=(resource,),
        )

    provider = ResearchRuntimeProvider(
        settings_factory=lambda: settings,
        configured_factory=configured_factory,
        source_runtime_provider=source_provider,
    )
    monkeypatch.setattr(
        research_composition,
        "_DEFAULT_RESEARCH_RUNTIME_PROVIDER",
        provider,
    )

    first = build_research_application_service()
    second = build_research_application_service()
    assert first is second
    assert len(resources) == 1

    close_default_research_runtime()
    close_default_research_runtime()
    assert resources[0].close_calls == 1
    assert provider.closed is True

    reset_default_research_runtime()
    third = build_research_application_service()
    assert third is not first
    assert len(resources) == 2

    close_default_research_runtime()
    assert resources[1].close_calls == 1


def test_chunk_rag_provider_is_lazy_reused_and_closed_by_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import interfaces.services.paper_rag_factory as paper_rag_factory

    class _VectorStore:
        def __init__(self) -> None:
            self.search_started = Event()
            self.release_search = Event()
            self.close_calls = 0

        def ensure_collections(self, _collections):
            return []

        def ensure_payload_indexes(self, _collections, _indexes):
            return []

        def search(self, _query):
            self.search_started.set()
            if not self.release_search.wait(timeout=5):
                raise TimeoutError("Paper RAG search was not released")
            return []

        def get_document(self, _collection, _document_id):
            return None

        def list_payloads(self, _collection, *, filters=None):
            return []

        def close(self) -> None:
            self.close_calls += 1

    class _Reranker:
        def score(self, _query, passages):
            return [1.0 for _ in passages]

    vector_store = _VectorStore()
    monkeypatch.delenv("NEWS_RAG_LLM_PLANNER", raising=False)
    monkeypatch.delenv("NEWS_RAG_MEMORY", raising=False)
    monkeypatch.setattr(
        paper_rag_factory,
        "qdrant_store_from_env",
        lambda: vector_store,
    )
    monkeypatch.setattr(
        paper_rag_factory,
        "paper_visual_chunk_store_from_env",
        lambda: None,
    )
    monkeypatch.setattr(
        paper_rag_factory,
        "CrossEncoderReranker",
        _Reranker,
    )
    ask_use_case = AskPaperUseCase()
    provider = research_composition._PaperRagUseCaseProvider(ask_use_case)

    results: list[dict[str, object]] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(provider.rag_ask("paper-1", "question", limit=5))
        except BaseException as exc:
            failures.append(exc)

    request_thread = Thread(target=invoke)
    request_thread.start()
    assert vector_store.search_started.wait(timeout=2)

    service = provider._service
    runtime_resources = provider._runtime_resources
    assert service is provider._get()
    assert runtime_resources is service._runtime_resources

    close_thread = Thread(target=provider.close)
    close_thread.start()
    close_thread.join(timeout=0.1)
    assert close_thread.is_alive()
    assert vector_store.close_calls == 0

    vector_store.release_search.set()
    request_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert not request_thread.is_alive()
    assert not close_thread.is_alive()
    assert failures == []
    assert results[0]["paper_id"] == "paper-1"
    assert results[0]["passages"] == []
    assert service.closed is True
    assert runtime_resources.closed is True
    assert vector_store.close_calls == 1

    provider.close()
    with pytest.raises(RuntimeError, match="provider is closed"):
        provider.rag_ask("paper-3", "question", limit=1)


def test_runtime_provider_reset_rebuilds_real_paper_rag_resource_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import interfaces.services.paper_rag_factory as paper_rag_factory

    class _VectorStore:
        def __init__(self) -> None:
            self.close_calls = 0

        def ensure_collections(self, _collections):
            return []

        def ensure_payload_indexes(self, _collections, _indexes):
            return []

        def close(self) -> None:
            self.close_calls += 1

    class _Reranker:
        def score(self, _query, passages):
            return [1.0 for _ in passages]

    vector_stores: list[_VectorStore] = []

    def build_vector_store() -> _VectorStore:
        store = _VectorStore()
        vector_stores.append(store)
        return store

    monkeypatch.delenv("NEWS_RAG_LLM_PLANNER", raising=False)
    monkeypatch.delenv("NEWS_RAG_MEMORY", raising=False)
    monkeypatch.setattr(
        paper_rag_factory,
        "qdrant_store_from_env",
        build_vector_store,
    )
    monkeypatch.setattr(
        paper_rag_factory,
        "paper_visual_chunk_store_from_env",
        lambda: None,
    )
    monkeypatch.setattr(
        paper_rag_factory,
        "CrossEncoderReranker",
        _Reranker,
    )

    settings = _settings(tmp_path)
    source_provider = SourceRuntimeProvider()

    def configured_factory(
        actual_settings: ResearchRuntimeSettings,
        actual_source_provider: SourceRuntimeProvider,
    ) -> ResearchRuntimeComposition:
        ask_use_case = AskPaperUseCase()
        rag_provider = research_composition._PaperRagUseCaseProvider(ask_use_case)
        return ResearchRuntimeComposition(
            settings=actual_settings,
            service=ResearchApplicationService(
                analyze_use_case=FakeAnalyzeUseCase(),
                ask_use_case=ask_use_case,
                rag_ask_use_case=rag_provider,
                run_store=_ExplicitRunStore(),
            ),
            source_runtime_provider=actual_source_provider,
            resources=(rag_provider,),
        )

    provider = ResearchRuntimeProvider(
        settings_factory=lambda: settings,
        configured_factory=configured_factory,
        source_runtime_provider=source_provider,
    )

    first = provider.get()
    first_rag_provider = first.service._rag_ask_use_case
    first_reranker = first_rag_provider.get_reranker()
    assert first_reranker is first_rag_provider.get_reranker()
    assert len(vector_stores) == 1

    provider.reset()

    assert vector_stores[0].close_calls == 1
    second = provider.get()
    second_rag_provider = second.service._rag_ask_use_case
    second_reranker = second_rag_provider.get_reranker()
    assert second is not first
    assert second_reranker is not first_reranker
    assert len(vector_stores) == 2

    provider.close()
    assert vector_stores[1].close_calls == 1


def test_reset_finishes_closing_old_resources_before_rebuilding(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    source_provider = SourceRuntimeProvider()
    resources: list[_Closable] = []
    factory_calls: list[int] = []

    def configured_factory(
        actual_settings: ResearchRuntimeSettings,
        actual_source_provider: SourceRuntimeProvider,
    ) -> ResearchRuntimeComposition:
        factory_calls.append(len(factory_calls) + 1)
        resource: _Closable = (
            _BlockingClosable() if len(factory_calls) == 1 else _Closable()
        )
        resources.append(resource)
        return ResearchRuntimeComposition(
            settings=actual_settings,
            service=ResearchApplicationService(
                analyze_use_case=FakeAnalyzeUseCase(),
                run_store=_ExplicitRunStore(),
            ),
            source_runtime_provider=actual_source_provider,
            resources=(resource,),
        )

    provider = ResearchRuntimeProvider(
        settings_factory=lambda: settings,
        configured_factory=configured_factory,
        source_runtime_provider=source_provider,
    )
    provider.get()
    blocking = resources[0]
    assert isinstance(blocking, _BlockingClosable)

    reset_thread = Thread(target=provider.reset)
    reset_thread.start()
    assert blocking.started.wait(timeout=2)

    rebuilt: list[ResearchRuntimeComposition] = []
    get_thread = Thread(target=lambda: rebuilt.append(provider.get()))
    get_thread.start()

    assert factory_calls == [1]
    assert rebuilt == []

    blocking.release.set()
    reset_thread.join(timeout=2)
    get_thread.join(timeout=2)

    assert not reset_thread.is_alive()
    assert not get_thread.is_alive()
    assert factory_calls == [1, 2]
    assert len(rebuilt) == 1
    provider.close()
