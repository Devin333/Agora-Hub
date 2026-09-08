from __future__ import annotations

import io
import urllib.error
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from interfaces.api import create_app
from interfaces.models import ActorContext
from interfaces.sdk import NewsApiError, NewsClient
from interfaces.services.research_service import (
    InMemoryResearchRunStore,
    ResearchAnalyzeInput,
    ResearchApplicationService,
)
from backend.research.ports.run_store import ResearchRunRecord
from infrastructure.research.filesystem_run_store import FilesystemResearchRunStore
from tests.interfaces.research_fixtures import (
    FakeAnalyzeUseCase,
    FakeResearchAnalysisResult,
    make_research_result,
)


def _client(*, result=None):
    store = InMemoryResearchRunStore()
    use_case = FakeAnalyzeUseCase(result or make_research_result())
    service = ResearchApplicationService(analyze_use_case=use_case, run_store=store)
    return TestClient(
        create_app(
            research_service_factory=lambda: service,
            audit_emitter_factory=None,
        )
    )


def _scoped_client(service) -> TestClient:
    app = create_app(
        research_service_factory=lambda: service,
        audit_emitter_factory=None,
    )

    @app.middleware("http")
    async def authenticated_test_actor(request, call_next):
        request.state.actor_context = ActorContext(
            actor_id="user-1", actor_type="user", roles=["service"],
            request_id="authenticated-test-request", metadata={"tenant_id": "tenant-a"},
        )
        return await call_next(request)

    return TestClient(app)


def test_research_analyze_endpoint_returns_run_id_and_refs() -> None:
    client = _client()

    response = client.post(
        "/api/v1/research/papers/analyze",
        json={
            "paperId": "paper-1",
            "sourceUrl": "https://arxiv.org/abs/2606.00001",
            "metadata": {"source": "arxiv"},
            "options": {"max_turns": 8},
            "runId": "research-run-1",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["success"] is True
    assert payload["data"]["runId"] == "research-run-1"
    assert payload["data"]["analysisRef"] == "artifact://research-run-1/analysis"
    assert payload["data"]["readerPayloadRef"] == "artifact://research-run-1/reader"
    assert payload["data"]["traceRef"] == "harness-trace://research-run-1"


def test_research_http_transports_actor_scope_for_analyze_and_both_ask_modes() -> None:
    service = _CapturingResearchService()
    client = _scoped_client(service)
    actor = {
        "tenantId": "tenant-a",
        "userId": "user-1",
        "memoryNamespace": "research:tenant:tenant-a:user:user-1",
    }

    analyzed = client.post(
        "/api/v1/research/papers/analyze",
        json={
            "paperId": "paper-1",
            "sourceUrl": "https://arxiv.org/abs/2606.00001",
            **actor,
        },
    )
    asked = client.post(
        "/api/v1/research/papers/paper-1/ask",
        json={"question": "What is the method?", **actor},
    )
    rag_asked = client.post(
        "/api/v1/research/papers/paper-1/rag-ask",
        json={
            "question": "What evidence supports the method?",
            "sectionIndex": 2,
            "limit": 7,
            "generate": True,
            **actor,
        },
    )

    assert analyzed.status_code == 200
    assert asked.status_code == 200
    assert rag_asked.status_code == 200
    analyze_input = service.analyze_inputs[0]
    ask_input, rag_ask_input = service.ask_inputs
    assert (
        analyze_input.tenant_id,
        analyze_input.user_id,
        analyze_input.memory_namespace,
    ) == (
        "tenant-a",
        "user-1",
        "research:tenant:tenant-a:user:user-1",
    )
    assert rag_ask_input.mode == "chunk_rag"
    assert rag_ask_input.section_index == 2
    assert rag_ask_input.limit == 7
    assert rag_ask_input.generate is True
    assert (
        rag_ask_input.tenant_id,
        rag_ask_input.user_id,
        rag_ask_input.memory_namespace,
    ) == (
        "tenant-a",
        "user-1",
        "research:tenant:tenant-a:user:user-1",
    )
    assert (
        ask_input.tenant_id,
        ask_input.user_id,
        ask_input.memory_namespace,
    ) == (
        "tenant-a",
        "user-1",
        "research:tenant:tenant-a:user:user-1",
    )


@pytest.mark.parametrize(
    "field,value",
    (
        ("tenantId", "tenant-a"),
        ("userId", "user-1"),
        ("memoryNamespace", "research:tenant:tenant-a:user:user-1"),
    ),
)
def test_research_http_rejects_untrusted_scope_before_service_call(field, value):
    service = _CapturingResearchService()
    client = TestClient(
        create_app(
            research_service_factory=lambda: service,
            audit_emitter_factory=None,
        )
    )
    response = client.post(
        "/api/v1/research/papers/analyze",
        json={
            "paperId": "paper-1",
            "sourceUrl": "https://arxiv.org/abs/2606.00001",
            field: value,
        },
        headers={"x-news-actor": "user-1", "x-news-tenant-id": "tenant-a"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert service.analyze_inputs == []


def test_authenticated_research_http_binds_deployment_tenant_and_rejects_spoof(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NEWS_TENANT_ID", "tenant-a")
    service = _CapturingResearchService()
    client = TestClient(
        create_app(
            research_service_factory=lambda: service,
            api_keys={"token-a": ["service"]},
            audit_emitter_factory=None,
        )
    )
    headers = {"Authorization": "Bearer token-a"}

    analyzed = client.post(
        "/api/v1/research/papers/analyze",
        headers=headers,
        json={
            "paperId": "paper-1",
            "sourceUrl": "https://arxiv.org/abs/2606.00001",
        },
    )
    spoofed = client.post(
        "/api/v1/research/papers/paper-1/ask",
        headers=headers,
        json={
            "question": "What is the method?",
            "tenantId": "tenant-b",
        },
    )

    assert analyzed.status_code == 200
    assert service.analyze_inputs[0].tenant_id == "tenant-a"
    assert (
        service.analyze_inputs[0].memory_namespace
        == "research:tenant:tenant-a:public"
    )
    assert spoofed.status_code == 403
    assert spoofed.json()["error"]["code"] == "forbidden"
    assert service.ask_inputs == []


def test_research_reader_analysis_ask_and_trace_endpoints_return_research_payloads() -> None:
    client = _client()
    client.post(
        "/api/v1/research/papers/analyze",
        json={"paperId": "paper-1", "sourceUrl": "https://arxiv.org/abs/2606.00001", "runId": "research-run-1"},
    )

    analysis = client.get("/api/v1/research/papers/paper-1/analysis")
    reader = client.get("/api/v1/research/papers/paper-1/reader")
    ask = client.post("/api/v1/research/papers/paper-1/ask", json={"question": "What is the method?"})
    trace = client.get("/api/v1/research/runs/research-run-1/trace")

    assert analysis.status_code == 200
    assert analysis.json()["data"]["analysis"]["summary"]["method_summary"] == "A controlled PLAN EXECUTE VERIFY runtime."
    assert reader.status_code == 200
    assert reader.json()["data"]["paper"]["paper_id"] == "paper-1"
    assert ask.status_code == 200
    assert ask.json()["data"]["evidenceRefs"]
    assert trace.status_code == 200
    assert trace.json()["data"]["trace"]["events"][0]["event_type"] == "phase_started"


def test_research_http_mcp_sdk_shapes_survive_filesystem_reconstruction(
    tmp_path,
) -> None:
    """All public read surfaces project the same reconstructed accepted run."""

    paper_id = "paper-surface-reconstruction"
    accepted_run_id = "run-surface-accepted"
    failed_run_id = "run-surface-quarantine"
    store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=FakeResearchAnalysisResult.from_dict,
    )
    accepted = make_research_result(
        run_id=accepted_run_id,
        paper_id=paper_id,
    )
    ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(accepted),
        run_store=store,
    ).analyze_paper(
        # The public scope keeps this matrix compatible with every transport's
        # default actor while the failed run remains available by run id only.
        ResearchAnalyzeInput(
            run_id=accepted_run_id,
            paper_id=paper_id,
            source_url="https://arxiv.org/abs/2606.00123",
        )
    )
    failed = make_research_result(
        run_id=failed_run_id,
        paper_id=paper_id,
        status="halted",
        quality_passed=False,
    )
    store.save(
        ResearchRunRecord(
            run_id=failed_run_id,
            paper_id=paper_id,
            result=failed,
        )
    )

    # Reconstruct the service from durable run records; no accepted result is
    # carried over in memory.
    reopened_store = FilesystemResearchRunStore(
        tmp_path,
        result_decoder=FakeResearchAnalysisResult.from_dict,
    )
    service = ResearchApplicationService(
        run_store=reopened_store,
        rag_ask_use_case=_SurfaceRagAskUseCase(),
    )
    client = TestClient(
        create_app(
            research_service_factory=lambda: service,
            audit_emitter_factory=None,
        )
    )

    http_analysis = client.get(
        f"/api/v1/research/papers/{paper_id}/analysis"
    )
    http_reader = client.get(f"/api/v1/research/papers/{paper_id}/reader")
    http_ask = client.post(
        f"/api/v1/research/papers/{paper_id}/ask",
        json={"question": "What is the method?"},
    )
    http_rag_ask = client.post(
        f"/api/v1/research/papers/{paper_id}/rag-ask",
        json={
            "question": "Which evidence supports the method?",
            "sectionIndex": 1,
            "limit": 3,
            "generate": True,
        },
    )
    http_trace = client.get(
        f"/api/v1/research/runs/{failed_run_id}/trace"
    )

    assert http_analysis.status_code == 200
    assert http_reader.status_code == 200
    assert http_ask.status_code == 200
    assert http_rag_ask.status_code == 200
    assert http_trace.status_code == 200
    assert http_analysis.json()["data"]["runId"] == accepted_run_id
    assert http_reader.json()["data"]["metadata"]["runId"] == accepted_run_id
    assert http_ask.json()["data"]["metadata"]["runId"] == accepted_run_id
    assert http_rag_ask.json()["data"] == {
        "mode": "chunk_rag",
        "paper_id": paper_id,
        "trace": {"run_id": "rag-run"},
    }
    assert http_trace.json()["data"]["runId"] == failed_run_id
    assert http_trace.json()["data"]["status"] == "halted"
    assert "diagnostics" in http_trace.json()["data"]["metadata"]

    # The HTTP MCP adapter uses the same application service and must retain
    # the read payload shape, including trace metadata for quarantine runs.
    mcp_analysis = client.post(
        "/api/v1/mcp/tools/news.research.paper_analysis/call",
        json={"arguments": {"paper_id": paper_id}},
    )
    mcp_reader = client.post(
        "/api/v1/mcp/tools/news.research.reader/call",
        json={"arguments": {"paper_id": paper_id}},
    )
    mcp_ask = client.post(
        "/api/v1/mcp/tools/news.research.ask/call",
        json={
            "arguments": {
                "paper_id": paper_id,
                "question": "What is the method?",
            }
        },
    )
    mcp_trace = client.post(
        "/api/v1/mcp/tools/news.research.trace/call",
        json={"arguments": {"run_id": failed_run_id}},
    )
    for response in (mcp_analysis, mcp_reader, mcp_ask, mcp_trace):
        assert response.status_code == 200
        assert response.json()["success"] is True
        assert response.json()["data"]["success"] is True
    assert mcp_analysis.json()["data"]["data"] == http_analysis.json()["data"]
    assert mcp_reader.json()["data"]["data"] == http_reader.json()["data"]
    assert mcp_ask.json()["data"]["data"] == http_ask.json()["data"]
    assert mcp_trace.json()["data"]["data"] == http_trace.json()["data"]

    # The SDK consumes the ordinary HTTP envelope and retains the same data
    # and typed error contracts after reconstruction.
    sdk = NewsClient(
        "http://testserver",
        opener=_TestClientOpener(client),
    )
    assert sdk.research.analysis(paper_id) == http_analysis.json()["data"]
    assert sdk.research.reader(paper_id) == http_reader.json()["data"]
    assert sdk.research.ask(
        paper_id,
        question="What is the method?",
    ) == http_ask.json()["data"]
    assert sdk.research.trace(failed_run_id) == http_trace.json()["data"]
    with pytest.raises(NewsApiError) as missing:
        sdk.research.analysis("missing-surface-paper")
    assert missing.value.code == "paper_not_found"
    assert missing.value.status_code == 404


def test_research_api_returns_stable_error_codes_without_tracebacks() -> None:
    client = _client(result=make_research_result(quality_passed=False))

    missing = client.get("/api/v1/research/papers/missing/analysis")
    failed_quality = client.post(
        "/api/v1/research/papers/analyze",
        json={"paperId": "paper-1", "sourceUrl": "https://arxiv.org/abs/2606.00001"},
    )

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "paper_not_found"
    assert "Traceback" not in missing.text
    assert failed_quality.status_code == 422
    assert failed_quality.json()["error"]["code"] == "quality_gate_failed"
    assert "Traceback" not in failed_quality.text


def test_research_api_requires_matching_actor_for_tenant_queries() -> None:
    result = make_research_result(run_id="run-tenant-a", paper_id="paper-1")
    result.trace["metadata"] = {
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "memory_namespace": "research:tenant:tenant-a:user:user-1",
    }
    service = ResearchApplicationService(
        analyze_use_case=FakeAnalyzeUseCase(result),
        run_store=InMemoryResearchRunStore(),
    )
    anonymous_client = TestClient(
        create_app(
            research_service_factory=lambda: service,
            audit_emitter_factory=None,
        )
    )
    client = _scoped_client(service)
    actor = {
        "tenantId": "tenant-a",
        "userId": "user-1",
        "memoryNamespace": "research:tenant:tenant-a:user:user-1",
    }
    client.post(
        "/api/v1/research/papers/analyze",
        json={
            "paperId": "paper-1",
            "sourceUrl": "https://arxiv.org/abs/2606.00001",
            "runId": "run-tenant-a",
            **actor,
        },
    )

    hidden_analysis = anonymous_client.get("/api/v1/research/papers/paper-1/analysis")
    hidden_trace = anonymous_client.get("/api/v1/research/runs/run-tenant-a/trace")
    visible_analysis = client.get(
        "/api/v1/research/papers/paper-1/analysis",
        params=actor,
    )
    visible_reader = client.get(
        "/api/v1/research/papers/paper-1/reader",
        params=actor,
    )
    visible_trace = client.get(
        "/api/v1/research/runs/run-tenant-a/trace",
        params=actor,
    )

    assert hidden_analysis.status_code == 404
    assert hidden_trace.status_code == 404
    assert visible_analysis.status_code == 200
    assert visible_analysis.json()["data"]["runId"] == "run-tenant-a"
    assert visible_reader.status_code == 200
    assert visible_reader.json()["data"]["metadata"]["runId"] == "run-tenant-a"
    assert visible_trace.status_code == 200
    assert visible_trace.json()["data"]["runId"] == "run-tenant-a"


def test_research_api_does_not_use_old_paper_service_or_old_papers_routes() -> None:
    client = _client()
    response = client.post(
        "/api/v1/research/papers/analyze",
        json={"paperId": "paper-1", "sourceUrl": "https://arxiv.org/abs/2606.00001"},
    )
    old_route = client.get("/api/v1/papers/paper-1")

    assert response.status_code == 200
    assert response.json()["data"]["paperId"] == "paper-1"
    assert old_route.status_code == 404


class _CapturingResearchService:
    def __init__(self) -> None:
        self.analyze_inputs = []
        self.ask_inputs = []

    def analyze_paper(self, command):
        self.analyze_inputs.append(command)
        return {
            "runId": command.run_id or "run-actor",
            "paperId": command.paper_id,
            "status": "succeeded",
        }

    def ask_paper(self, paper_id, request):
        self.ask_inputs.append(request)
        return {
            "answer": "Grounded answer",
            "evidenceRefs": [f"paper://{paper_id}/sec-method"],
            "confidence": 1.0,
        }


class _SurfaceRagAskUseCase:
    def rag_ask(self, paper_id, _question, **_kwargs):
        return {
            "mode": "chunk_rag",
            "paper_id": paper_id,
            "trace": {"run_id": "rag-run"},
        }


class _SdkResponse:
    status = 200

    def __init__(self, response) -> None:
        self.status = response.status_code
        self._body = response.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


class _TestClientOpener:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def __call__(self, request, _timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        response = self.client.request(
            request.get_method(),
            path,
            headers=dict(request.header_items()),
            content=request.data,
        )
        if response.status_code >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                response.status_code,
                "HTTP error",
                hdrs=None,
                fp=io.BytesIO(response.content),
            )
        return _SdkResponse(response)
