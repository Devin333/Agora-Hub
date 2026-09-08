from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.research.application import (
    AnalyzePaperRequest,
    AnalyzePaperUseCase,
    AskPaperUseCase,
    ResearchRunDispositionReconciler,
)
from backend.research.application.single_paper_runtime import (
    ResearchAnalysisResult,
    ResearchSinglePaperRuntime,
)
from backend.research.ports.run_store import (
    ResearchRunDisposition,
    ResearchRunDispositionReason,
    ResearchRunRecord,
    ResearchRunStoreConflictError,
    ResearchRunStoreReason,
    ResearchRunStoreUnavailableError,
)
from interfaces.services.research_service import (
    InMemoryResearchRunStore,
    ResearchActorAuthorizationError,
    ResearchActorInput,
    ResearchAnalyzeInput,
    ResearchApplicationService,
    ResearchAskInput,
    ResearchServiceError,
    bind_research_actor_input,
)
from framework.harness import FakeArtifactPort, InMemoryHarnessEventPort
from infrastructure.research.filesystem_run_store import FilesystemResearchRunStore
from interfaces.models import ActorContext
from tests.backend.research.fakes import (
    FakeGithubRepositoryPort,
    FakeResearchDocumentCompiler,
    FakeResearchLLMWorker,
    FakeResearchRAGRuntime,
    FakeResearchSourceProvider,
    in_memory_node_output_resource_factory,
)
from tests.interfaces.research_fixtures import (
    FakeAnalyzeUseCase,
    FakeResearchAnalysisResult,
    make_research_result,
)


def test_research_service_analyze_calls_research_application_and_stores_result() -> None:
    use_case = FakeAnalyzeUseCase()
    store = InMemoryResearchRunStore()
    service = ResearchApplicationService(analyze_use_case=use_case, run_store=store)

    response = service.analyze_paper(
        ResearchAnalyzeInput(
            paper_id="paper-1",
            source_url="https://arxiv.org/abs/2606.00001",
            run_id="research-run-1",
            user_id="user-1",
            metadata={"source": "arxiv"},
            options={"max_turns": 8},
            tenant_id="tenant-a",
            memory_namespace="research:tenant:tenant-a:user:user-1",
        )
    )

    assert response["runId"] == "research-run-1"
    assert response["paperId"] == "paper-1"
    assert response["analysisRef"] == "artifact://research-run-1/analysis"
    assert use_case.calls[0].source_ref == "https://arxiv.org/abs/2606.00001"
    assert use_case.calls[0].tenant_id == "tenant-a"
    assert use_case.calls[0].user_id == "user-1"
    assert (
        use_case.calls[0].memory_namespace
        == "research:tenant:tenant-a:user:user-1"
    )
    assert use_case.calls[0].options["metadata"] == {"source": "arxiv"}
    assert store.get_latest_by_paper_id("paper-1") is not None


def test_research_service_reader_and_analysis_return_research_payloads() -> None:
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(),
        run_store=InMemoryResearchRunStore(),
    )
    service.analyze_paper(ResearchAnalyzeInput(paper_id="paper-1", source_url="https://arxiv.org/abs/2606.00001"))

    analysis = service.get_analysis("paper-1")
    reader = service.get_reader("paper-1")

    assert analysis["analysis"]["summary"]["core_idea"] == "Separate Harness routing from LLM candidate generation."
    assert reader["paper"]["paper_id"] == "paper-1"
    assert reader["document"]["sections"][0]["section_id"] == "sec-intro"
    assert reader["metadata"]["readerPayloadRef"] == "artifact://research-run-1/reader"


def test_research_service_reads_catalog_artifact_with_metadata_default() -> None:
    class Reader:
        def __init__(self) -> None:
            self.calls = []

        def read(self, ref, *, actor_scope, include_payload=False, max_chars=200_000):
            self.calls.append((ref, actor_scope, include_payload, max_chars))
            return {
                "artifactRef": ref,
                "artifactType": "research-document",
                "contentChecksum": "sha256:" + "a" * 64,
                "metadata": {"paper_id": "paper-1"},
            }

    reader = Reader()
    service = ResearchApplicationService(artifact_reader=reader)

    response = service.get_artifact(
        "artifact://research/research-document/" + "a" * 64,
        actor=ResearchActorInput(
            tenant_id="tenant-a",
            user_id="user-a",
            memory_namespace="research:tenant:tenant-a:user:user-a",
        ),
    )

    assert "payload" not in response
    assert response["provenance"]["actorScope"]["tenant_id"] == "tenant-a"
    assert reader.calls[0][2] is False


def test_research_service_ask_returns_grounded_evidence_refs() -> None:
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(),
        run_store=InMemoryResearchRunStore(),
    )
    service.analyze_paper(ResearchAnalyzeInput(paper_id="paper-1", source_url="https://arxiv.org/abs/2606.00001"))

    answer = service.ask_paper(
        "paper-1",
        ResearchAskInput(question="What is the method?", selection={"sourceRefs": ["paper://paper-1/sec-intro"]}),
    )

    assert answer["answer"] == "A controlled PLAN EXECUTE VERIFY runtime."
    assert "paper://paper-1/sec-intro" in answer["evidenceRefs"]
    assert answer["traceRef"] == "harness-trace://research-run-1"


def test_research_service_dispatches_chunk_rag_mode_without_run_store_lookup() -> None:
    rag_ask_use_case = _CapturingRagAskUseCase()
    service = ResearchApplicationService(rag_ask_use_case=rag_ask_use_case)

    response = service.ask_paper(
        "paper-1",
        ResearchAskInput(
            question="What is the method?",
            mode="chunk_rag",
            section_index=2,
            limit=7,
            generate=True,
            gated=True,
            tenant_id="tenant-a",
            user_id="user-1",
        ),
    )

    assert response == {"mode": "chunk_rag", "paper_id": "paper-1"}
    assert rag_ask_use_case.calls == [
        (
            "paper-1",
            "What is the method?",
            {
                "section_index": 2,
                "limit": 7,
                "generate": True,
                "gated": True,
                "tenant_id": "tenant-a",
                "user_id": "user-1",
                "memory_namespace": "research:tenant:tenant-a:user:user-1",
            },
        )
    ]


@pytest.mark.parametrize(
    ("ask_input", "message"),
    [
        (ResearchAskInput(question="question", mode="unknown"), "mode must be"),
        (
            ResearchAskInput(question="question", mode="chunk_rag", section_index=-1),
            "sectionIndex must be",
        ),
        (
            ResearchAskInput(question="question", mode="chunk_rag", limit=21),
            "limit must be",
        ),
    ],
)
def test_research_service_rejects_invalid_ask_mode_or_chunk_bounds(
    ask_input: ResearchAskInput,
    message: str,
) -> None:
    service = ResearchApplicationService(rag_ask_use_case=_CapturingRagAskUseCase())

    with pytest.raises(ResearchServiceError, match=message) as failed:
        service.ask_paper("paper-1", ask_input)

    assert failed.value.code == "invalid_request"
    assert failed.value.status_code == 400


def test_research_service_ask_propagates_canonical_actor_scope() -> None:
    ask_use_case = _CapturingAskPaperUseCase()
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(),
        ask_use_case=ask_use_case,
        run_store=InMemoryResearchRunStore(),
    )
    service.analyze_paper(
        ResearchAnalyzeInput(
            paper_id="paper-1",
            source_url="https://arxiv.org/abs/2606.00001",
        )
    )

    service.ask_paper(
        "paper-1",
        ResearchAskInput(
            question="What is the method?",
            tenant_id="tenant-a",
            user_id="user-1",
            memory_namespace="research:tenant:tenant-a:user:user-1",
        ),
    )

    goal = ask_use_case.goals[0]
    assert goal.allowed_memory_namespaces == [
        "research:tenant:tenant-a:user:user-1"
    ]
    assert goal.metadata["tenant_id"] == "tenant-a"
    assert goal.metadata["user_id"] == "user-1"
    assert goal.metadata["memory_namespace"] == goal.allowed_memory_namespaces[0]


def test_research_service_rejects_tenant_namespace_without_tenant_actor() -> None:
    service = ResearchApplicationService(run_store=InMemoryResearchRunStore())

    with pytest.raises(ResearchServiceError) as failed:
        service.ask_paper(
            "paper-1",
            ResearchAskInput(
                question="What is the method?",
                memory_namespace="research:tenant:tenant-a:public",
            ),
        )

    assert failed.value.code == "invalid_request"
    assert failed.value.status_code == 400


@pytest.mark.parametrize(
    "requested",
    (
        ResearchActorInput(tenant_id="tenant-a"),
        ResearchActorInput(user_id="user-1"),
        ResearchActorInput(
            memory_namespace="research:tenant:tenant-a:user:user-1"
        ),
    ),
)
@pytest.mark.parametrize(
    "actor",
    (
        None,
        ActorContext(
            actor_id="anonymous",
            actor_type="anonymous",
            roles=[],
            request_id="anonymous-request",
            metadata={"tenant_id": "tenant-a"},
        ),
    ),
)
def test_untrusted_actor_cannot_claim_research_scope(requested, actor) -> None:
    with pytest.raises(ResearchActorAuthorizationError) as forbidden:
        bind_research_actor_input(requested, actor)
    assert forbidden.value.status_code == 403
    assert forbidden.value.code == "forbidden"


def test_untrusted_actor_without_scope_keeps_public_research_access() -> None:
    anonymous = ActorContext(
        actor_id="anonymous",
        actor_type="anonymous",
        roles=[],
        request_id="anonymous-request",
        metadata={"tenant_id": "must-not-be-trusted"},
    )
    assert (
        bind_research_actor_input(ResearchActorInput(), None)
        == ResearchActorInput()
    )
    assert (
        bind_research_actor_input(ResearchActorInput(), anonymous)
        == ResearchActorInput()
    )


def test_authenticated_actor_scope_overrides_or_rejects_requested_scope() -> None:
    actor = ActorContext(
        actor_id="service-a",
        actor_type="service",
        roles=["service"],
        request_id="request-1",
        metadata={"tenant_id": "tenant-a"},
    )

    implicit = bind_research_actor_input(ResearchActorInput(), actor)
    matching = bind_research_actor_input(
        ResearchActorInput(
            tenant_id="tenant-a",
            memory_namespace="research:tenant:tenant-a:public",
        ),
        actor,
    )

    assert implicit == ResearchActorInput(
        tenant_id="tenant-a",
        memory_namespace="research:tenant:tenant-a:public",
    )
    assert matching == implicit
    with pytest.raises(ResearchActorAuthorizationError) as forbidden:
        bind_research_actor_input(
            ResearchActorInput(tenant_id="tenant-b"),
            actor,
        )
    assert forbidden.value.status_code == 403
    assert forbidden.value.code == "forbidden"


def test_research_service_ask_preserves_options_user_id_compatibility() -> None:
    ask_use_case = _CapturingAskPaperUseCase()
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(),
        ask_use_case=ask_use_case,
        run_store=InMemoryResearchRunStore(),
    )
    service.analyze_paper(
        ResearchAnalyzeInput(
            paper_id="paper-1",
            source_url="https://arxiv.org/abs/2606.00001",
        )
    )

    service.ask_paper(
        "paper-1",
        ResearchAskInput(
            question="What is the method?",
            options={"userId": "legacy-user"},
        ),
    )

    assert ask_use_case.goals[0].allowed_memory_namespaces == [
        "research:user:legacy-user"
    ]


def test_research_service_does_not_expose_tenant_run_without_tenant_actor() -> None:
    result = make_research_result()
    result.trace["metadata"] = {
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "memory_namespace": "research:tenant:tenant-a:user:user-1",
    }
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(result),
        run_store=InMemoryResearchRunStore(),
    )
    service.analyze_paper(
        ResearchAnalyzeInput(
            paper_id="paper-1",
            source_url="https://arxiv.org/abs/2606.00001",
            tenant_id="tenant-a",
            user_id="user-1",
        )
    )

    with pytest.raises(ResearchServiceError) as hidden:
        service.ask_paper(
            "paper-1",
            ResearchAskInput(question="What is the method?"),
        )

    assert hidden.value.code == "paper_not_found"
    allowed = service.ask_paper(
        "paper-1",
        ResearchAskInput(
            question="What is the method?",
            tenant_id="tenant-a",
            user_id="user-1",
        ),
    )
    assert allowed["evidenceRefs"]


def test_research_queries_select_latest_record_visible_to_actor() -> None:
    store = InMemoryResearchRunStore()
    for run_id, tenant_id, user_id in (
        ("run-tenant-a", "tenant-a", "user-a"),
        ("run-tenant-b", "tenant-b", "user-b"),
    ):
        result = make_research_result(run_id=run_id, paper_id="paper-1")
        result.trace["metadata"] = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "memory_namespace": f"research:tenant:{tenant_id}:user:{user_id}",
        }
        result.actor_scope.clear()
        result.actor_scope.update(result.trace["metadata"])
        for entry in result.transcript.get("entries", []):
            entry["metadata"] = dict(result.trace["metadata"])
        store.save(
            ResearchRunRecord(
                run_id=run_id,
                paper_id="paper-1",
                result=result,
            )
        )
    service = ResearchApplicationService(run_store=store)
    actor_a = ResearchActorInput(
        tenant_id="tenant-a",
        user_id="user-a",
        memory_namespace="research:tenant:tenant-a:user:user-a",
    )

    analysis = service.get_analysis("paper-1", actor=actor_a)
    reader = service.get_reader("paper-1", actor=actor_a)
    trace = service.get_trace("run-tenant-a", actor=actor_a)

    assert analysis["runId"] == "run-tenant-a"
    assert reader["metadata"]["runId"] == "run-tenant-a"
    assert trace["runId"] == "run-tenant-a"

    for hidden_call in (
        lambda: service.get_analysis("paper-1"),
        lambda: service.get_reader("paper-1"),
        lambda: service.get_trace("run-tenant-a"),
        lambda: service.get_analysis(
            "paper-1",
            actor=ResearchActorInput(
                tenant_id="tenant-b",
                user_id="user-other",
            ),
        ),
    ):
        with pytest.raises(ResearchServiceError) as hidden:
            hidden_call()
        assert hidden.value.code == "paper_not_found"


class _CapturingAskPaperUseCase(AskPaperUseCase):
    def __init__(self) -> None:
        self.goals = []

    def build_retrieval_goal(self, goal):
        self.goals.append(goal)
        return super().build_retrieval_goal(goal)


class _CapturingRagAskUseCase:
    def __init__(self) -> None:
        self.calls = []

    def rag_ask(self, paper_id, question, **kwargs):
        self.calls.append((paper_id, question, kwargs))
        return {"mode": "chunk_rag", "paper_id": paper_id}


def test_research_service_standardizes_missing_and_quality_errors() -> None:
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(make_research_result(quality_passed=False)),
        run_store=InMemoryResearchRunStore(),
    )

    with pytest.raises(ResearchServiceError) as missing:
        service.get_analysis("missing-paper")
    assert missing.value.code == "paper_not_found"

    with pytest.raises(ResearchServiceError) as quality:
        service.analyze_paper(ResearchAnalyzeInput(paper_id="paper-1", source_url="https://arxiv.org/abs/2606.00001"))
    assert quality.value.code == "quality_gate_failed"
    assert quality.value.details["gateFailures"][0]["gate_name"] == "ResearchReportReadinessGate"


def test_research_service_does_not_depend_on_old_papers_application_service() -> None:
    project_root = Path(__file__).resolve().parents[3]
    assert not (project_root / "interfaces" / "services" / "paper_service.py").exists()

    source = (project_root / "interfaces" / "services" / "research_service.py").read_text(
        encoding="utf-8"
    )
    imports = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "interfaces.services.paper_service" not in imports

    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(),
        run_store=InMemoryResearchRunStore(),
    )

    response = service.analyze_paper(ResearchAnalyzeInput(paper_id="paper-1", source_url="https://arxiv.org/abs/2606.00001"))

    assert response["status"] == "succeeded"


def test_in_memory_store_rejects_cross_paper_run_identity_reuse() -> None:
    store = InMemoryResearchRunStore()
    first = ResearchRunRecord(
        run_id="shared-run",
        paper_id="paper-a",
        result=make_research_result(run_id="shared-run", paper_id="paper-a"),
    )
    conflicting = ResearchRunRecord(
        run_id="shared-run",
        paper_id="paper-b",
        result=make_research_result(run_id="shared-run", paper_id="paper-b"),
    )

    store.save(first)

    with pytest.raises(ResearchRunStoreConflictError):
        store.save(conflicting)

    assert store.get_latest_by_paper_id("paper-a") == first
    assert store.get_latest_by_paper_id("paper-b") is None


def test_run_store_errors_map_to_sanitized_service_error() -> None:
    store = _UnavailableRunStore()
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(),
        run_store=store,
    )

    with pytest.raises(ResearchServiceError) as failed:
        service.analyze_paper(
            ResearchAnalyzeInput(
                paper_id="paper-1",
                source_url="https://arxiv.org/abs/2606.00001",
                run_id="research-run-storage-failure",
            )
        )

    assert failed.value.code == "research_run_failed"
    assert failed.value.message == "research run storage operation failed"
    assert failed.value.details == {
        "operation": "save",
        "reason": "filesystem_unavailable",
    }
    assert failed.value.retryable is True
    assert failed.value.__cause__ is None
    assert "C:/private" not in str(failed.value.details)


class _UnavailableRunStore:
    def save(self, _record: ResearchRunRecord) -> None:
        raise ResearchRunStoreUnavailableError(
            ResearchRunStoreReason.FILESYSTEM_UNAVAILABLE
        )

    def get_by_run_id(self, _run_id: str) -> ResearchRunRecord | None:
        raise ResearchRunStoreUnavailableError(
            ResearchRunStoreReason.FILESYSTEM_UNAVAILABLE
        )

    def get_latest_by_paper_id(self, _paper_id: str) -> ResearchRunRecord | None:
        raise ResearchRunStoreUnavailableError(
            ResearchRunStoreReason.FILESYSTEM_UNAVAILABLE
        )

    def list_by_paper_id(self, _paper_id: str) -> list[ResearchRunRecord]:
        raise ResearchRunStoreUnavailableError(
            ResearchRunStoreReason.FILESYSTEM_UNAVAILABLE
        )


def test_research_service_queries_survive_durable_store_reconstruction(
    tmp_path: Path,
) -> None:
    """A reconstructed service must read the same typed result, not process state."""

    run_id = "research-run-service-restart"
    paper_id = "paper-harness-001"
    runtime = ResearchSinglePaperRuntime(
        source_provider=FakeResearchSourceProvider(),
        document_compiler=FakeResearchDocumentCompiler(),
        llm_worker=FakeResearchLLMWorker(),
        github_repository=FakeGithubRepositoryPort(),
        rag_runtime=FakeResearchRAGRuntime(),
        artifact_port=FakeArtifactPort(),
        event_port_factory=lambda _run_id: InMemoryHarnessEventPort(),
        node_output_resource_factory=in_memory_node_output_resource_factory,
    )
    result = AnalyzePaperUseCase(runtime).analyze(
        AnalyzePaperRequest(
            run_id=run_id,
            paper_id=paper_id,
            source_ref="https://arxiv.org/abs/2606.00123",
            user_id="user-1",
        )
    )
    assert isinstance(result, ResearchAnalysisResult)

    store_a = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    service_a = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(result),
        run_store=store_a,
    )
    service_a.analyze_paper(
        ResearchAnalyzeInput(
            paper_id=paper_id,
            source_url="https://arxiv.org/abs/2606.00123",
            run_id=run_id,
            user_id="user-1",
        )
    )
    actor = ResearchActorInput(user_id="user-1")

    expected = {
        "analysis": service_a.get_analysis(paper_id, actor=actor),
        "reader": service_a.get_reader(paper_id, actor=actor),
        "ask": service_a.ask_paper(
            paper_id,
            ResearchAskInput(
                question="What is the method?",
                selection={"sourceRefs": ["paper://paper-harness-001/sec-intro"]},
                user_id="user-1",
            ),
        ),
        "trace": service_a.get_trace(run_id, actor=actor),
    }

    # A fresh store and service model a process restart. No in-memory result is
    # passed to the reconstructed service; only the durable root is shared.
    store_b = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=ResearchAnalysisResult.from_dict,
    )
    service_b = ResearchApplicationService(run_store=store_b)

    actual = {
        "analysis": service_b.get_analysis(paper_id, actor=actor),
        "reader": service_b.get_reader(paper_id, actor=actor),
        "ask": service_b.ask_paper(
            paper_id,
            ResearchAskInput(
                question="What is the method?",
                selection={"sourceRefs": ["paper://paper-harness-001/sec-intro"]},
                user_id="user-1",
            ),
        ),
        "trace": service_b.get_trace(run_id, actor=actor),
    }

    assert actual == expected
    assert actual["analysis"]["metadata"]["artifactRefs"]
    assert actual["trace"]["transcript"]["run_id"] == run_id


@pytest.mark.parametrize("backend", ["memory", "filesystem"])
def test_accepted_then_failed_runs_do_not_shadow_normal_queries(
    tmp_path: Path,
    backend: str,
) -> None:
    accepted = make_research_result(
        run_id="run-accepted",
        paper_id="paper-disposition-service",
    )
    halted = make_research_result(
        run_id="run-halted",
        paper_id="paper-disposition-service",
        status="halted",
        quality_passed=False,
    )
    quality_failed = make_research_result(
        run_id="run-quality-failed",
        paper_id="paper-disposition-service",
        quality_passed=False,
    )
    store = (
        InMemoryResearchRunStore()
        if backend == "memory"
        else FilesystemResearchRunStore(
            tmp_path,
            result_decoder=FakeResearchAnalysisResult.from_dict,
        )
    )

    ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(accepted),
        run_store=store,
    ).analyze_paper(
        ResearchAnalyzeInput(
            run_id=accepted.run_id,
            paper_id="paper-disposition-service",
            source_url="https://arxiv.org/abs/2606.00123",
            user_id="user-1",
        )
    )
    with pytest.raises(ResearchServiceError) as halted_error:
        ResearchApplicationService(
            analyze_use_case=FakeAnalyzeUseCase(halted),
            run_store=store,
        ).analyze_paper(
            ResearchAnalyzeInput(
                run_id=halted.run_id,
                paper_id="paper-disposition-service",
                source_url="https://arxiv.org/abs/2606.00123",
                user_id="user-1",
            )
        )
    assert halted_error.value.code == "research_run_failed"
    with pytest.raises(ResearchServiceError) as quality_error:
        ResearchApplicationService(
            analyze_use_case=FakeAnalyzeUseCase(quality_failed),
            run_store=store,
        ).analyze_paper(
            ResearchAnalyzeInput(
                run_id=quality_failed.run_id,
                paper_id="paper-disposition-service",
                source_url="https://arxiv.org/abs/2606.00123",
                user_id="user-1",
            )
        )
    assert quality_error.value.code == "quality_gate_failed"

    if backend == "filesystem":
        store = FilesystemResearchRunStore(
            tmp_path,
            result_decoder=FakeResearchAnalysisResult.from_dict,
        )
    service = ResearchApplicationService(run_store=store)
    actor = ResearchActorInput(user_id="user-1")
    assert store.get_latest_by_paper_id("paper-disposition-service").run_id == accepted.run_id
    assert [
        record.run_id
        for record in store.list_by_paper_id("paper-disposition-service")
    ] == [accepted.run_id]
    assert service.get_analysis("paper-disposition-service", actor=actor)["runId"] == accepted.run_id
    assert service.get_reader("paper-disposition-service", actor=actor)["metadata"]["runId"] == accepted.run_id
    assert service.ask_paper(
        "paper-disposition-service",
        ResearchAskInput(question="What is the method?", user_id="user-1"),
    )["metadata"]["runId"] == accepted.run_id
    assert service.get_trace(halted.run_id, actor=actor)["status"] == "halted"
    assert service.get_trace(quality_failed.run_id, actor=actor)["status"] == "succeeded"
    assert store.get_by_run_id(halted.run_id).disposition is ResearchRunDisposition.QUARANTINE
    assert store.get_by_run_id(quality_failed.run_id).disposition is ResearchRunDisposition.QUARANTINE


class _FailingAnalyzeUseCase:
    def analyze(self, _request):
        raise RuntimeError("injected post-run failure")


class _TypedFailingAnalyzeUseCase:
    def __init__(self, error: ResearchServiceError) -> None:
        self._error = error

    def analyze(self, _request):
        raise self._error


class _ExceptionAnalyzeUseCase:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def analyze(self, _request):
        raise self._error


class _RecoverySource:
    def __init__(
        self,
        record: ResearchRunRecord,
        *,
        advertise_pending: bool = True,
    ) -> None:
        self.record = record
        self.advertise_pending = advertise_pending
        self.load_calls = 0

    def list_pending_run_ids(self, *, limit: int) -> tuple[str, ...]:
        if not self.advertise_pending:
            return ()
        return (self.record.run_id,)[:limit]

    def load_recovery_record(self, run_id: str) -> ResearchRunRecord | None:
        self.load_calls += 1
        return self.record if run_id == self.record.run_id else None


class _FailingRecoveryRunStore:
    def __init__(self) -> None:
        self._inner = InMemoryResearchRunStore()

    def save(self, _record: ResearchRunRecord) -> None:
        raise OSError("injected recovery persistence failure")

    def get_by_run_id(self, run_id: str) -> ResearchRunRecord | None:
        return self._inner.get_by_run_id(run_id)

    def get_latest_by_paper_id(self, paper_id: str) -> ResearchRunRecord | None:
        return self._inner.get_latest_by_paper_id(paper_id)

    def list_by_paper_id(self, paper_id: str) -> list[ResearchRunRecord]:
        return self._inner.list_by_paper_id(paper_id)


class _WorkerFailure(RuntimeError):
    """Representative exception raised by a candidate worker."""


class _TerminalPublicationFailure(RuntimeError):
    """Representative exception raised while finalizing a terminal effect."""


def _bind_fake_result_actor_scope(
    result: FakeResearchAnalysisResult,
    *,
    run_id: str,
    paper_id: str,
    tenant_id: str,
    user_id: str,
    memory_namespace: str,
) -> None:
    FakeAnalyzeUseCase(result).analyze(
        AnalyzePaperRequest(
            run_id=run_id,
            paper_id=paper_id,
            source_ref="https://arxiv.org/abs/2606.00123",
            tenant_id=tenant_id,
            user_id=user_id,
            memory_namespace=memory_namespace,
        )
    )


def test_post_run_exception_reconciles_quarantine_before_preserving_error_shape() -> None:
    run_id = "run-post-creation-failure"
    result = make_research_result(
        run_id=run_id,
        paper_id="paper-recovery",
        status="halted",
        quality_passed=False,
    )
    record = ResearchRunRecord(
        run_id=run_id,
        paper_id="paper-recovery",
        result=result,
    )
    store = InMemoryResearchRunStore()
    source = _RecoverySource(record)
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=source,
        max_runs=1,
    )
    service = ResearchApplicationService(
        analyze_use_case=_FailingAnalyzeUseCase(),
        run_store=store,
        run_reconciler=reconciler,
    )

    with pytest.raises(ResearchServiceError) as failed:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id=run_id,
                paper_id="paper-recovery",
                source_url="https://arxiv.org/abs/2606.00123",
            )
        )

    assert failed.value.code == "research_run_failed"
    assert failed.value.details == {"error_type": "RuntimeError"}
    diagnostic = store.get_by_run_id(run_id)
    assert diagnostic is not None and diagnostic.quarantined
    assert store.get_latest_by_paper_id("paper-recovery") is None
    assert source.load_calls == 1


def test_post_run_value_error_with_durable_history_is_runtime_failure() -> None:
    run_id = "run-post-creation-value-error"
    record = ResearchRunRecord(
        run_id=run_id,
        paper_id="paper-value-error-recovery",
        result=make_research_result(
            run_id=run_id,
            paper_id="paper-value-error-recovery",
            status="halted",
            quality_passed=False,
        ),
    )
    store = InMemoryResearchRunStore()
    source = _RecoverySource(record, advertise_pending=False)
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=source,
        max_runs=1,
    )
    service = ResearchApplicationService(
        analyze_use_case=_ExceptionAnalyzeUseCase(
            ValueError("candidate worker returned an invalid value")
        ),
        run_store=store,
        run_reconciler=reconciler,
    )

    with pytest.raises(ResearchServiceError) as raised:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id=run_id,
                paper_id=record.paper_id,
                source_url="https://arxiv.org/abs/2606.00123",
            )
        )

    assert raised.value.code == "research_run_failed"
    assert raised.value.status_code == 500
    assert raised.value.details == {"error_type": "ValueError"}
    assert store.get_by_run_id(run_id).quarantined
    assert source.load_calls == 1


def test_post_run_recovery_store_failure_is_visible() -> None:
    run_id = "run-post-creation-recovery-save-failure"
    record = ResearchRunRecord(
        run_id=run_id,
        paper_id="paper-recovery-save-failure",
        result=make_research_result(
            run_id=run_id,
            paper_id="paper-recovery-save-failure",
            status="halted",
            quality_passed=False,
        ),
    )
    store = _FailingRecoveryRunStore()
    source = _RecoverySource(record, advertise_pending=False)
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=source,
        max_runs=1,
    )
    expected = ResearchServiceError(
        "research_run_failed",
        "candidate worker failed",
        status_code=503,
        retryable=True,
    )
    service = ResearchApplicationService(
        analyze_use_case=_TypedFailingAnalyzeUseCase(expected),
        run_store=store,
        run_reconciler=reconciler,
    )

    with pytest.raises(ResearchServiceError) as raised:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id=run_id,
                paper_id=record.paper_id,
                source_url="https://arxiv.org/abs/2606.00123",
            )
        )

    assert raised.value is expected
    assert isinstance(raised.value.__cause__, OSError)
    assert any(
        "could not be durably reconciled" in note
        for note in getattr(raised.value, "__notes__", ())
    )
    assert store.get_by_run_id(run_id) is None
    assert source.load_calls == 1


def test_typed_post_run_exception_also_reconciles_before_rethrowing() -> None:
    run_id = "run-typed-post-creation-failure"
    result = make_research_result(
        run_id=run_id,
        paper_id="paper-typed-recovery",
        status="waiting_approval",
        quality_passed=False,
    )
    record = ResearchRunRecord(
        run_id=run_id,
        paper_id="paper-typed-recovery",
        result=result,
    )
    store = InMemoryResearchRunStore()
    source = _RecoverySource(record)
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=source,
        max_runs=1,
    )
    expected = ResearchServiceError(
        "research_run_failed",
        "handler failed",
        status_code=503,
        retryable=True,
    )
    service = ResearchApplicationService(
        analyze_use_case=_TypedFailingAnalyzeUseCase(expected),
        run_store=store,
        run_reconciler=reconciler,
    )

    with pytest.raises(ResearchServiceError) as raised:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id=run_id,
                paper_id="paper-typed-recovery",
                source_url="https://arxiv.org/abs/2606.00123",
            )
        )
    assert raised.value is expected
    assert source.load_calls == 1
    diagnostic = store.get_by_run_id(run_id)
    assert diagnostic is not None and diagnostic.quarantined
    assert store.get_latest_by_paper_id("paper-typed-recovery") is None


@pytest.mark.parametrize(
    ("failure_stage", "error_factory", "failed_status"),
    [
        ("worker", lambda: _WorkerFailure("candidate worker failed"), "halted"),
        (
            "handler",
            lambda: ResearchServiceError(
                "research_run_failed",
                "artifact preparation failed",
                status_code=503,
                details={
                    "error_type": "ResearchArtifactHandlerError",
                    "failure_stage": "handler",
                },
                retryable=True,
            ),
            "failed",
        ),
        (
            "terminal",
            lambda: _TerminalPublicationFailure("terminal publication failed"),
            "halted",
        ),
    ],
)
def test_post_creation_failure_recovers_scoped_quarantine_after_restart_without_shadowing_accepted(
    tmp_path: Path,
    failure_stage: str,
    error_factory,
    failed_status: str,
) -> None:
    """A failure after durable run creation is diagnostic, never canonical."""

    tenant_id = "tenant-recovery"
    user_id = "user-recovery"
    memory_namespace = f"research:tenant:{tenant_id}:user:{user_id}"
    actor = ResearchActorInput(
        tenant_id=tenant_id,
        user_id=user_id,
        memory_namespace=memory_namespace,
    )
    paper_id = "paper-post-creation-recovery"
    accepted_run_id = "run-accepted-before-failure"
    failed_run_id = f"run-{failure_stage}-post-creation"
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=FakeResearchAnalysisResult.from_dict,
    )

    accepted = make_research_result(
        run_id=accepted_run_id,
        paper_id=paper_id,
    )
    accepted_service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(accepted),
        run_store=store,
    )
    accepted_service.analyze_paper(
        ResearchAnalyzeInput(
            run_id=accepted_run_id,
            paper_id=paper_id,
            source_url="https://arxiv.org/abs/2606.00123",
            tenant_id=tenant_id,
            user_id=user_id,
            memory_namespace=memory_namespace,
        )
    )

    failed = make_research_result(
        run_id=failed_run_id,
        paper_id=paper_id,
        status=failed_status,
        quality_passed=False,
    )
    _bind_fake_result_actor_scope(
        failed,
        run_id=failed_run_id,
        paper_id=paper_id,
        tenant_id=tenant_id,
        user_id=user_id,
        memory_namespace=memory_namespace,
    )
    recovery_source = _RecoverySource(
        ResearchRunRecord(
            run_id=failed_run_id,
            paper_id=paper_id,
            result=failed,
        ),
        advertise_pending=False,
    )
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=recovery_source,
        max_runs=2,
    )
    service = ResearchApplicationService(
        analyze_use_case=_ExceptionAnalyzeUseCase(error_factory()),
        run_store=store,
        run_reconciler=reconciler,
    )

    # No pending ids were advertised at startup; the load must happen only from
    # the post-run exception path, preserving the original public error object.
    assert recovery_source.load_calls == 0
    with pytest.raises(ResearchServiceError) as raised:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id=failed_run_id,
                paper_id=paper_id,
                source_url="https://arxiv.org/abs/2606.00123",
                tenant_id=tenant_id,
                user_id=user_id,
                memory_namespace=memory_namespace,
            )
        )
    assert recovery_source.load_calls == 1
    if failure_stage == "handler":
        assert raised.value.status_code == 503
        assert raised.value.details["failure_stage"] == "handler"
    else:
        assert raised.value.status_code == 500
        assert raised.value.details == {"error_type": type(error_factory()).__name__}

    identity_scope_ref = store.get_by_run_id(failed_run_id).identity_scope_ref
    assert identity_scope_ref is not None
    diagnostic = store.get_diagnostic_by_run_id(
        failed_run_id,
        identity_scope_ref=identity_scope_ref,
    )
    assert diagnostic is not None
    assert diagnostic.quarantined
    assert diagnostic.paper_id == paper_id
    assert diagnostic.subject_scope_ref is not None
    assert store.get_latest_by_paper_id(paper_id).run_id == accepted_run_id
    assert [
        record.run_id for record in store.list_by_paper_id(paper_id)
    ] == [accepted_run_id]

    # Reconstructing from the filesystem preserves the accepted projection and
    # keeps the failed run available only through the scoped trace diagnostic.
    reopened_store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=FakeResearchAnalysisResult.from_dict,
    )
    reopened = ResearchApplicationService(run_store=reopened_store)
    assert reopened.get_analysis(paper_id, actor=actor)["runId"] == accepted_run_id
    assert reopened.get_reader(paper_id, actor=actor)["metadata"]["runId"] == accepted_run_id
    assert reopened.ask_paper(
        paper_id,
        ResearchAskInput(question="What is the method?", **actor.__dict__),
    )["metadata"]["runId"] == accepted_run_id
    failed_trace = reopened.get_trace(failed_run_id, actor=actor)
    assert failed_trace["runId"] == failed_run_id
    assert failed_trace["paperId"] == paper_id
    assert failed_trace["status"] == failed_status
    assert "diagnostics" in failed_trace["metadata"]

    with pytest.raises(ResearchServiceError) as hidden:
        reopened.get_trace(
            failed_run_id,
            actor=ResearchActorInput(
                tenant_id=tenant_id,
                user_id="different-user",
                memory_namespace=f"research:tenant:{tenant_id}:user:different-user",
            ),
        )
    assert hidden.value.code == "paper_not_found"


def test_invalid_request_without_durable_run_does_not_create_quarantine_record() -> None:
    store = InMemoryResearchRunStore()
    recovery_source = _RecoverySource(
        ResearchRunRecord(
            run_id="never-created",
            paper_id="paper-invalid-request",
            result=make_research_result(
                run_id="never-created",
                paper_id="paper-invalid-request",
                status="halted",
                quality_passed=False,
            ),
        ),
        advertise_pending=False,
    )
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=recovery_source,
        max_runs=1,
    )
    service = ResearchApplicationService(
        analyze_use_case=_ExceptionAnalyzeUseCase(
            AssertionError("invalid input must fail before worker invocation")
        ),
        run_store=store,
        run_reconciler=reconciler,
    )

    with pytest.raises(ResearchServiceError) as failed:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id="never-created",
                paper_id="paper-invalid-request",
            )
        )
    assert failed.value.code == "invalid_request"
    assert store.get_by_run_id("never-created") is None
    assert store.get_latest_by_paper_id("paper-invalid-request") is None
    assert recovery_source.load_calls == 0


@pytest.mark.parametrize(
    ("status", "quality_passed", "error_code"),
    [
        ("halted", False, "research_run_failed"),
        ("succeeded", False, "quality_gate_failed"),
    ],
)
def test_failure_only_historical_run_stays_diagnostic_after_reconstruction(
    tmp_path: Path,
    status: str,
    quality_passed: bool,
    error_code: str,
) -> None:
    run_id = f"run-failure-only-{status}"
    paper_id = f"paper-failure-only-{status}"
    result = make_research_result(
        run_id=run_id,
        paper_id=paper_id,
        status=status,
        quality_passed=quality_passed,
    )
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=FakeResearchAnalysisResult.from_dict,
    )
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(result),
        run_store=store,
    )
    with pytest.raises(ResearchServiceError) as failed:
        service.analyze_paper(
            ResearchAnalyzeInput(
                run_id=run_id,
                paper_id=paper_id,
                source_url="https://arxiv.org/abs/2606.00123",
                user_id="user-1",
            )
        )
    assert failed.value.code == error_code

    reconstructed_store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=FakeResearchAnalysisResult.from_dict,
    )
    reconstructed = ResearchApplicationService(run_store=reconstructed_store)
    actor = ResearchActorInput(user_id="user-1")
    assert reconstructed_store.get_latest_by_paper_id(paper_id) is None
    assert reconstructed_store.list_by_paper_id(paper_id) == []
    for query in (
        lambda: reconstructed.get_analysis(paper_id, actor=actor),
        lambda: reconstructed.get_reader(paper_id, actor=actor),
        lambda: reconstructed.ask_paper(
            paper_id,
            ResearchAskInput(question="What is the method?", user_id="user-1"),
        ),
    ):
        with pytest.raises(ResearchServiceError) as hidden:
            query()
        assert hidden.value.code == "paper_not_found"

    trace = reconstructed.get_trace(run_id, actor=actor)
    assert trace["status"] == status
    diagnostic = reconstructed_store.get_by_run_id(run_id)
    assert diagnostic is not None and diagnostic.quarantined
    assert (
        diagnostic.artifact_reference_disposition
        == ResearchRunDispositionReason.LEGACY_QUARANTINED.value
    )


def test_service_startup_reconciles_pending_runs_once_and_lazy_read_reuses_record() -> None:
    authority_ref = "sha256:" + "7" * 64
    run_id = "run-startup-recovery"
    result = make_research_result(
        run_id=run_id,
        paper_id="paper-startup-recovery",
    )
    result.diagnostics["publication_authority_ref"] = authority_ref
    record = ResearchRunRecord(
        run_id=run_id,
        paper_id="paper-startup-recovery",
        result=result,
        publication_authority_ref=authority_ref,
        schema_version="newsroom.research_run_record.v2",
    )
    store = InMemoryResearchRunStore()
    source = _RecoverySource(record)
    reconciler = ResearchRunDispositionReconciler(
        run_store=store,
        recovery_source=source,
        max_runs=1,
    )

    service = ResearchApplicationService(
        run_store=store,
        run_reconciler=reconciler,
    )
    assert source.load_calls == 1
    recovered = store.get_by_run_id(run_id)
    assert recovered is not None and recovered.accepted

    assert service.get_trace(run_id)["runId"] == run_id
    assert source.load_calls == 1
