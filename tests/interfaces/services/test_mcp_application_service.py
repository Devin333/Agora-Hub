import json
from dataclasses import dataclass

import pytest

from framework.agent.artifacts import (
    ArtifactChecksumMismatchError,
    ArtifactStoreMetadataError,
    ArtifactStoreRequiredError,
)
from interfaces.models import ActorContext
from interfaces.services.artifact_service import ArtifactInspectionService
from interfaces.services.mcp_service import MCPApplicationService
from interfaces.services.report_service import ReportApplicationService
from interfaces.services.run_inspection_service import GraphRunInspectionService
from tests.fixtures.graph_runs import (
    graph_index_reader,
    rewrite_graph_terminal_manifest,
    write_graph_terminal_run,
)


def test_mcp_catalog_lists_research_tools_without_calling_factories() -> None:
    service = MCPApplicationService(
        worker_service_factory=_raising_factory,
        run_service_factory=_raising_factory,
        report_service_factory=_raising_factory,
        source_service_factory=_raising_factory,
        entity_service_factory=_raising_factory,
        subscription_service_factory=_raising_factory,
        memory_service_factory=_raising_factory,
        diagnostic_service_factory=_raising_factory,
        graph_run_inspection_service_factory=_raising_factory,
        artifact_service_factory=_raising_factory,
        storage_service_factory=_raising_factory,
        research_service_factory=_raising_factory,
    )

    catalog = service.catalog().to_dict()
    tool_names = {tool["name"] for tool in catalog["tools"]}
    resource_uris = {resource["uri"] for resource in catalog["resources"]}
    prompt_names = {prompt["name"] for prompt in catalog["prompts"]}

    assert {
        "news.research.analyze_paper",
        "news.research.paper_analysis",
        "news.research.reader",
        "news.research.ask",
        "news.research.trace",
        "news.report.list",
        "news.run.show",
        "news.source.arxiv.fetch",
        "news.source.github.releases",
        "news.worker.status",
        "news.queue.status",
        "news.graph.wait.inspect",
        "news.graph.wait.signal",
        "news.graph.approval.decision",
        "news.graph.wait.cancel",
    } <= tool_names
    assert not {"news.daily.enqueue", "news.daily.run", "news.weekly.run"} & tool_names
    assert {
        "news://reports/latest",
        "news://runs/{run_id}/manifest",
        "news://runs/{run_id}/events",
        "news://runs/{run_id}/replay",
        "news://storage/metrics",
        "news://workers",
        "news://queues",
    } <= resource_uris
    assert "news.research.paper_briefing" in prompt_names
    assert "news.evidence_audit" in prompt_names


def test_mcp_capability_manifest_describes_research_permissions() -> None:
    service = MCPApplicationService(
        worker_service_factory=_raising_factory,
        run_service_factory=_raising_factory,
        report_service_factory=_raising_factory,
        source_service_factory=_raising_factory,
        entity_service_factory=_raising_factory,
        subscription_service_factory=_raising_factory,
        memory_service_factory=_raising_factory,
        diagnostic_service_factory=_raising_factory,
        graph_run_inspection_service_factory=_raising_factory,
        artifact_service_factory=_raising_factory,
        storage_service_factory=_raising_factory,
        research_service_factory=_raising_factory,
    )

    manifest = service.capability_manifest().to_dict()
    capabilities = {capability["name"]: capability for capability in manifest["capabilities"]}

    assert manifest["schema_version"] == "newsroom.mcp_capability_manifest.v1"
    assert capabilities["news.research.analyze_paper"]["permission"] == "write:runs"
    assert capabilities["news.research.analyze_paper"]["read_only"] is False
    assert capabilities["news.research.analyze_paper"]["category"] == "research"
    assert capabilities["news.research.reader"]["permission"] == "read:reports"
    assert capabilities["news.research.reader"]["read_only"] is True
    assert capabilities["news.report.publish"]["requires_approval"] is True
    assert capabilities["news://runs/{run_id}/manifest"]["kind"] == "resource"
    assert capabilities["news://storage/metrics"]["permission"] == "admin:storage"
    assert capabilities["news.research.paper_briefing"]["kind"] == "prompt"


def test_mcp_research_tools_use_configured_research_service_factory() -> None:
    research_service = _FakeResearchService()
    service = MCPApplicationService(research_service_factory=lambda: research_service).for_actor(
        ActorContext(
            actor_id="user-1", actor_type="user", roles=["mcp_client"],
            request_id="request-scoped-research",
            metadata={"tenant_id": "tenant-a"},
        )
    )
    actor_args = {
        "tenant_id": "tenant-a",
        "user_id": "user-1",
        "memory_namespace": "research:tenant:tenant-a:user:user-1",
    }

    analyzed = service.call_tool(
        "news.research.analyze_paper",
        {
            "paper_id": "paper-1",
            "source_url": "https://arxiv.org/abs/2605.00001",
            "run_id": "research-run-1",
            "metadata": {"collection": "benchmarks"},
            **actor_args,
        },
    )
    analysis = service.call_tool(
        "news.research.paper_analysis",
        {"paper_id": "paper-1", **actor_args},
    )
    reader = service.call_tool(
        "news.research.reader",
        {"paper_id": "paper-1", **actor_args},
    )
    answer = service.call_tool(
        "news.research.ask",
        {
            "paper_id": "paper-1",
            "question": "What is the method?",
            "locale": "en",
            **actor_args,
        },
    )
    trace = service.call_tool(
        "news.research.trace",
        {"run_id": "research-run-1", **actor_args},
    )

    assert analyzed.success is True
    assert analysis.success is True
    assert reader.success is True
    assert answer.success is True
    assert trace.success is True
    assert analyzed.data["runId"] == "research-run-1"
    assert analysis.data["analysis"]["summary"] == "Grounded analysis"
    assert reader.data["paper"]["paperId"] == "paper-1"
    assert answer.data["evidenceRefs"] == ["ev-1"]
    assert trace.data["trace"]["phases"] == ["PLAN", "EXECUTE", "VERIFY"]
    assert research_service.calls[0][0] == "analyze_paper"
    analyze_input = research_service.calls[0][1]
    ask_input = research_service.calls[3][2]
    assert (
        analyze_input.tenant_id,
        analyze_input.user_id,
        analyze_input.memory_namespace,
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
    for actor in (
        research_service.calls[1][2],
        research_service.calls[2][2],
        research_service.calls[4][2],
    ):
        assert (
            actor.tenant_id,
            actor.user_id,
            actor.memory_namespace,
        ) == (
            "tenant-a",
            "user-1",
            "research:tenant:tenant-a:user:user-1",
        )

    tools = {tool.name: tool for tool in service.catalog().tools}
    analyze_properties = tools["news.research.analyze_paper"].input_schema[
        "properties"
    ]
    ask_properties = tools["news.research.ask"].input_schema["properties"]
    analysis_properties = tools["news.research.paper_analysis"].input_schema[
        "properties"
    ]
    reader_properties = tools["news.research.reader"].input_schema["properties"]
    trace_properties = tools["news.research.trace"].input_schema["properties"]
    assert {"tenant_id", "user_id", "memory_namespace"} <= analyze_properties.keys()
    assert {"tenant_id", "user_id", "memory_namespace"} <= ask_properties.keys()
    for properties in (analysis_properties, reader_properties, trace_properties):
        assert {"tenant_id", "user_id", "memory_namespace"} <= properties.keys()


@pytest.mark.parametrize(
    "field,value",
    (
        ("tenant_id", "tenant-a"),
        ("user_id", "user-1"),
        ("memory_namespace", "research:tenant:tenant-a:user:user-1"),
    ),
)
def test_mcp_research_rejects_untrusted_scope_before_service_call(
    monkeypatch,
    field,
    value,
) -> None:
    monkeypatch.delenv("NEWS_EVENT_OPERATOR_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("NEWS_TENANT_ID", raising=False)
    research_service = _FakeResearchService()
    service = MCPApplicationService(research_service_factory=lambda: research_service)
    result = service.call_tool(
        "news.research.analyze_paper",
        {
            "paper_id": "paper-1",
            "source_url": "https://arxiv.org/abs/2605.00001",
            field: value,
        },
    )
    assert result.success is False
    assert result.error_type == "ResearchActorAuthorizationError"
    assert research_service.calls == []


def test_mcp_research_without_actor_keeps_public_scope(monkeypatch) -> None:
    monkeypatch.delenv("NEWS_EVENT_OPERATOR_PRINCIPAL_ID", raising=False)
    monkeypatch.delenv("NEWS_TENANT_ID", raising=False)
    research_service = _FakeResearchService()
    service = MCPApplicationService(research_service_factory=lambda: research_service)

    result = service.call_tool(
        "news.research.analyze_paper",
        {
            "paper_id": "paper-1",
            "source_url": "https://arxiv.org/abs/2605.00001",
        },
    )

    assert result.success is True
    command = research_service.calls[0][1]
    assert (command.tenant_id, command.user_id, command.memory_namespace) == (
        None, None, None,
    )


def test_mcp_research_uses_authenticated_deployment_actor_scope(monkeypatch) -> None:
    monkeypatch.setenv("NEWS_EVENT_OPERATOR_PRINCIPAL_ID", "research-service")
    monkeypatch.setenv("NEWS_EVENT_OPERATOR_ROLE", "service")
    monkeypatch.setenv("NEWS_TENANT_ID", "tenant-a")
    research_service = _FakeResearchService()
    service = MCPApplicationService(research_service_factory=lambda: research_service)

    result = service.call_tool(
        "news.research.analyze_paper",
        {
            "paper_id": "paper-1",
            "source_url": "https://arxiv.org/abs/2605.00001",
        },
    )

    assert result.success is True
    command = research_service.calls[0][1]
    assert (command.tenant_id, command.user_id, command.memory_namespace) == (
        "tenant-a", None, "research:tenant:tenant-a:public",
    )


def test_mcp_research_scope_is_bound_to_transport_actor() -> None:
    research_service = _FakeResearchService()
    actor = ActorContext(
        actor_id="mcp-client-a",
        actor_type="mcp_client",
        roles=["mcp_client"],
        request_id="request-1",
        metadata={"tenant_id": "tenant-a"},
    )
    service = MCPApplicationService(
        research_service_factory=lambda: research_service,
    ).for_actor(actor)

    allowed = service.call_tool(
        "news.research.analyze_paper",
        {
            "paper_id": "paper-1",
            "source_url": "https://arxiv.org/abs/2605.00001",
            "run_id": "run-1",
        },
    )
    spoofed = service.call_tool(
        "news.research.ask",
        {
            "paper_id": "paper-1",
            "question": "What is the method?",
            "tenant_id": "tenant-b",
        },
    )

    assert allowed.success is True
    analyze_input = research_service.calls[0][1]
    assert analyze_input.tenant_id == "tenant-a"
    assert analyze_input.memory_namespace == "research:tenant:tenant-a:public"
    assert spoofed.success is False
    assert spoofed.error_type == "ResearchActorAuthorizationError"
    assert "tenant-b" not in str(spoofed.to_dict())
    assert len(research_service.calls) == 1


def test_mcp_source_tools_call_source_service() -> None:
    service = MCPApplicationService(source_service_factory=lambda: _FakeSourceService())

    health = service.call_tool("news.source.health", {"include_disabled": True})
    arxiv = service.call_tool("news.source.arxiv.fetch", {"query": "cat:cs.AI", "limit": 1})
    github = service.call_tool("news.source.github.releases", {"repository": "owner/repo", "limit": 1})

    assert health.success is True
    assert health.data["health"][0]["source_id"] == "source-1"
    assert arxiv.success is True
    assert arxiv.data["items"][0]["title"] == "Agent Runtime Evaluation"
    assert github.success is True
    assert github.data["items"][0]["title"] == "Version 1.0.0"


def test_mcp_worker_and_queue_tools_call_worker_service() -> None:
    fake_worker = _FakeWorkerService()
    service = MCPApplicationService(worker_service_factory=lambda: fake_worker)

    worker = service.call_tool("news.worker.status", {"worker_id": "worker-1"})
    queue = service.call_tool("news.queue.status", {"queue_names": ["news:queue:memory"]})
    queue_resource = service.read_resource("news://queues?queue_name=news:queue:memory")

    assert worker.success is True
    assert queue.success is True
    assert queue_resource.success is True
    assert fake_worker.queue_calls == [["news:queue:memory"], ["news:queue:memory"]]


def test_mcp_graph_wait_tools_use_harness_application_service() -> None:
    wait_service = _FakeGraphWaitService()
    actor = ActorContext(
        actor_id="operator",
        actor_type="mcp_client",
        roles=["admin"],
        request_id="request-1",
    )
    service = MCPApplicationService(
        harness_wait_service_factory=lambda received_actor: wait_service,
        operator_actor=actor,
    )

    inspected = service.call_tool(
        "news.graph.wait.inspect",
        {"run_id": "run-1", "node_instance_id": "node-1"},
    )
    decided = service.call_tool(
        "news.graph.approval.decision",
        {
            "run_id": "run-1",
            "node_instance_id": "node-1",
            "approval_id": "approval-1",
            "approved": True,
        },
    )
    rejected_legacy = service.call_tool(
        "news.graph.approval.decision",
        {
            "run_id": "run-1",
            "node_instance_id": "node-1",
            "approval_id": "approval-1",
            "approved": True,
            "node_updates": {},
        },
    )

    assert inspected.success is True
    assert decided.success is True
    assert rejected_legacy.success is False
    assert rejected_legacy.error_type == "ValueError"
    assert wait_service.calls == [
        ("inspect_wait", "run-1", "node-1"),
        ("decide_approval", "run-1", "node-1", "approval-1", True),
    ]


def test_mcp_report_list_reads_real_local_report_artifacts(tmp_path) -> None:
    _write_report_run(tmp_path, "run-1", "Research Report", "Grounded paper analysis.")
    service = MCPApplicationService(
        report_service_factory=lambda: ReportApplicationService(artifact_root=tmp_path)
    )

    result = service.call_tool("news.report.list", {"limit": 5})

    assert result.success is True
    assert result.data["report_count"] == 1
    assert result.data["reports"][0]["title"] == "Research Report"


def test_mcp_get_research_prompt_renders_arguments() -> None:
    service = MCPApplicationService()

    result = service.get_prompt(
        "news.research.paper_briefing",
        {"paper_id": "paper-1", "question": "What is new?"},
    )

    assert result.success is True
    content = result.to_dict()["messages"][0]["content"]
    assert "paper-1" in content
    assert "What is new?" in content


def test_mcp_unknown_tool_and_resource_fail_safely() -> None:
    service = MCPApplicationService()

    tool = service.call_tool("news.unknown", {})
    resource = service.read_resource("news://unknown")

    assert tool.success is False
    assert tool.error_type == "MCPToolNotFound"
    assert resource.success is False
    assert resource.error_type == "MCPResourceNotFound"


def test_mcp_unknown_tool_resource_and_prompt_exceptions_are_sanitized() -> None:
    secret = "postgresql://operator:password@db.internal/news"
    service = MCPApplicationService(
        report_service_factory=lambda: _ExplodingMCPService(secret),
        graph_run_inspection_service_factory=lambda: _ExplodingMCPService(secret),
    )

    results = [
        service.call_tool("news.report.latest", {}),
        service.read_resource("news://reports/latest"),
        service.get_prompt(
            "news.research.paper_briefing",
            {"paper_id": _ExplodingText(secret)},
        ),
    ]

    for result in results:
        payload = result.to_dict()
        assert payload["success"] is False
        assert payload["error_type"] == "MCPInternalError"
        assert payload["error_message"] == "internal error"
        assert payload["error_id"].startswith("err_")
        assert secret not in json.dumps(payload)


def test_mcp_artifact_path_failures_preserve_typed_failure_envelopes(tmp_path) -> None:
    workflow_fixture = write_graph_terminal_run(
        tmp_path,
        "run-workflow-unsafe",
    )
    graph_fixture = write_graph_terminal_run(tmp_path, "run-graph-unsafe")
    (tmp_path / "outside.txt").write_text("artifact-secret", encoding="utf-8")
    workflow_manifest = workflow_fixture.manifest.to_dict()
    workflow_manifest["artifacts"][0]["relative_path"] = "../outside.txt"
    rewrite_graph_terminal_manifest(workflow_fixture, workflow_manifest)
    graph_manifest = graph_fixture.manifest.to_dict()
    graph_manifest["artifacts"][0]["relative_path"] = "../outside.txt"
    rewrite_graph_terminal_manifest(graph_fixture, graph_manifest)
    service = MCPApplicationService(
        graph_run_inspection_service_factory=lambda: GraphRunInspectionService(
            tmp_path,
            graph_index_reader=graph_index_reader(tmp_path),
        ),
        artifact_service_factory=lambda: ArtifactInspectionService(tmp_path),
    )

    results = [
        service.read_resource("news://runs/run:stream/artifacts/output"),
        service.call_tool("news.run.replay", {"run_id": "run:stream"}),
        service.read_resource("news://runs/run-graph-unsafe/artifacts/output"),
        service.read_resource("news://runs/run-workflow-unsafe/replay"),
        service.call_tool(
            "news.run.replay",
            {"run_id": "run-workflow-unsafe"},
        ),
    ]

    for result in results[:2]:
        assert result.success is False
        assert result.error_type == "ArtifactPathError"
        assert result.data is None
        assert "artifact-secret" not in json.dumps(result.to_dict())
    for result in results[2:]:
        assert result.success is False
        assert result.error_type == "ArtifactStoreMetadataError"
        assert result.data is None
        assert "artifact-secret" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(
    "error_type",
    [
        ArtifactChecksumMismatchError,
        ArtifactStoreMetadataError,
        ArtifactStoreRequiredError,
    ],
)
def test_mcp_integrity_failures_preserve_typed_failure_envelopes(error_type) -> None:
    error = error_type("artifact integrity verification failed")
    failing_service = _ArtifactIntegrityFailureService(error)
    service = MCPApplicationService(
        graph_run_inspection_service_factory=lambda: failing_service,
        artifact_service_factory=lambda: failing_service,
    )

    results = [
        service.call_tool("news.run.replay", {"run_id": "run-1"}),
        service.read_resource("news://runs/run-1/replay"),
        service.read_resource("news://runs/run-1/artifacts/output"),
    ]

    for result in results:
        assert result.success is False
        assert result.error_type == error_type.__name__
        assert result.error_message == "artifact integrity verification failed"
        assert result.data is None


def test_mcp_real_filesystem_integrity_failures_preserve_typed_envelopes(tmp_path) -> None:
    workflow_fixture = write_graph_terminal_run(tmp_path, "run-replay")
    graph_fixture = write_graph_terminal_run(
        tmp_path,
        "run-artifact",
        files={"output": ("output.json", {"token": "fixture-secret-token"})},
    )
    service = MCPApplicationService(
        graph_run_inspection_service_factory=lambda: GraphRunInspectionService(
            tmp_path,
            graph_index_reader=graph_index_reader(tmp_path),
        ),
        artifact_service_factory=lambda: ArtifactInspectionService(tmp_path),
    )
    workflow_fixture.artifact_path("output").write_text(
        json.dumps({"result": "tampered-mcp-secret"}),
        encoding="utf-8",
    )
    graph_fixture.artifact_path("output").write_text(
        json.dumps({"result": "tampered-mcp-secret"}),
        encoding="utf-8",
    )

    replay_results = [
        service.call_tool("news.run.replay", {"run_id": "run-replay"}),
        service.read_resource("news://runs/run-replay/replay"),
        service.read_resource("news://runs/run-artifact/artifacts/output"),
    ]
    for result in replay_results:
        assert result.success is False
        assert result.error_type == "ArtifactChecksumMismatchError"
        assert result.data is None
        assert "tampered-mcp-secret" not in json.dumps(result.to_dict())

    manifest = graph_fixture.manifest.to_dict()
    manifest["artifacts"][0].pop("content_checksum")
    rewrite_graph_terminal_manifest(graph_fixture, manifest)
    missing_checksum = service.read_resource(
        "news://runs/run-artifact/artifacts/output"
    )

    assert missing_checksum.success is False
    assert missing_checksum.error_type == "ArtifactStoreMetadataError"
    assert missing_checksum.data is None
    assert "fixture-secret-token" not in json.dumps(missing_checksum.to_dict())


class _ArtifactIntegrityFailureService:
    def __init__(self, error) -> None:
        self.error = error

    def replay_run(self, run_id):
        raise self.error

    def get_artifact(self, run_id, artifact_key):
        raise self.error


class _ExplodingMCPService:
    def __init__(self, secret) -> None:
        self.secret = secret

    def latest_report(self):
        raise RuntimeError(self.secret)

    def replay_run(self, run_id):
        raise RuntimeError(self.secret)


class _ExplodingText:
    def __init__(self, secret) -> None:
        self.secret = secret

    def __str__(self):
        raise RuntimeError(self.secret)


def _raising_factory():
    raise AssertionError("factory should not be called")


class _FakeResearchService:
    def __init__(self) -> None:
        self.calls = []

    def analyze_paper(self, command):
        self.calls.append(("analyze_paper", command))
        return {
            "runId": command.run_id,
            "paperId": command.paper_id,
            "status": "succeeded",
            "analysisRef": "artifact://analysis",
        }

    def get_analysis(self, paper_id, *, actor=None):
        self.calls.append(("get_analysis", paper_id, actor))
        return {
            "runId": "research-run-1",
            "paperId": paper_id,
            "status": "succeeded",
            "analysis": {"summary": "Grounded analysis"},
        }

    def get_reader(self, paper_id, *, actor=None):
        self.calls.append(("get_reader", paper_id, actor))
        return {
            "paper": {"paperId": paper_id},
            "document": {"sections": []},
            "analysis": {"summary": "Grounded analysis"},
            "evidence": {"items": [{"evidenceId": "ev-1"}]},
            "navigation": [],
            "quality": [],
            "metadata": {"runId": "research-run-1"},
        }

    def ask_paper(self, paper_id, request):
        self.calls.append(("ask_paper", paper_id, request))
        return {
            "answer": "The method is evidence grounded.",
            "evidenceRefs": ["ev-1"],
            "confidence": 0.8,
        }

    def get_trace(self, run_id, *, actor=None):
        self.calls.append(("get_trace", run_id, actor))
        return {
            "runId": run_id,
            "paperId": "paper-1",
            "status": "succeeded",
            "trace": {"phases": ["PLAN", "EXECUTE", "VERIFY"]},
        }


class _FakeSourceService:
    def source_health(self, *, enabled_only):
        return _FakeResult(
            {
                "source_count": 1,
                "health": [{"source_id": "source-1", "status": "healthy"}],
            }
        )

    def fetch_arxiv(self, *, query, limit):
        return _FakeResult(
            {
                "source_id": "arxiv",
                "source_type": "arxiv",
                "query": query,
                "item_count": 1,
                "error_count": 0,
                "items": [{"title": "Agent Runtime Evaluation"}],
                "errors": [],
            }
        )

    def fetch_github_releases(self, *, repository, limit):
        return _FakeResult(
            {
                "source_id": "github",
                "source_type": "github",
                "query": repository,
                "item_count": 1,
                "error_count": 0,
                "items": [{"title": "Version 1.0.0"}],
                "errors": [],
            }
        )


class _FakeWorkerService:
    def __init__(self) -> None:
        self.calls = []
        self.queue_calls = []

    def list_worker_status(self, *, worker_id=None, stale_after_seconds=60):
        self.calls.append({"worker_id": worker_id, "stale_after_seconds": stale_after_seconds})
        return _FakeResult(
            {
                "worker_id": worker_id,
                "worker_count": 1,
                "unhealthy_count": 0,
                "stale_after_seconds": stale_after_seconds,
                "workers": [{"worker_id": "worker-1", "status": "running"}],
            }
        )

    def queue_status(self, *, queue_names=None):
        actual_queue_names = queue_names or ["news:queue:memory", "news:queue:sources"]
        self.queue_calls.append(actual_queue_names)
        return _FakeResult(
            {
                "queue_count": len(actual_queue_names),
                "total_stream_length": len(actual_queue_names),
                "total_pending_count": 0,
                "queues": [
                    {"queue_name": queue_name, "stream_length": 1, "pending_count": 0}
                    for queue_name in actual_queue_names
                ],
            }
        )


class _FakeGraphWaitService:
    def __init__(self) -> None:
        self.calls = []

    def inspect_wait(self, run_id, node_instance_id):
        self.calls.append(("inspect_wait", run_id, node_instance_id))
        return {"run_id": run_id, "node_instance_id": node_instance_id, "status": "ready"}

    def decide_approval(self, run_id, node_instance_id, *, approval_id, approved):
        self.calls.append(
            ("decide_approval", run_id, node_instance_id, approval_id, approved)
        )
        return {
            "operation": "approval",
            "wait": {
                "run_id": run_id,
                "node_instance_id": node_instance_id,
                "status": "ready",
            },
        }

class _FakeResult:
    def __init__(self, payload) -> None:
        self.payload = payload

    def to_dict(self):
        return self.payload


@dataclass(frozen=True)
class _ReportRecord:
    report_id: str
    run_id: str
    title: str
    status: str
    report_json: dict
    report_markdown: str
    quality_score: float
    manifest_path: str

    def to_dict(self):
        return self.__dict__.copy()


def _write_report_run(root, run_id: str, title: str, body: str) -> None:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps({"title": title, "sections": [{"title": "Summary", "content": body}]}),
        encoding="utf-8",
    )
    (run_dir / "report.md").write_text(f"# {title}\n\n{body}\n", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "graph_id": "research.paper-analysis",
                "graph_version": "1.0.0",
                "status": "succeeded",
                "finished_at": "2026-05-11T00:00:00Z",
                "quality_score": 0.9,
                "artifacts": {
                    "report_json": "report.json",
                    "report_markdown": "report.md",
                },
            }
        ),
        encoding="utf-8",
    )
