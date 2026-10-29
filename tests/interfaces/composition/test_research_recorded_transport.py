from __future__ import annotations

import argparse
import json
from email.message import Message
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.request import Request

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import framework.llm.clients.openai_compatible as openai_transport
import infrastructure.external.sources.arxiv as arxiv_transport
from backend.research.application.single_paper_runtime import (
    ResearchAnalysisResult,
    ResearchSinglePaperRuntime,
)
from backend.research.graphs import PAPER_ANALYSIS_GATE_REFERENCES
from framework.harness import HarnessEventType
from framework.llm import (
    LOCAL_STRUCTURED_OUTPUT_DIALECT,
    compile_structured_output_contract,
    structured_output_enforcement_keywords,
)
from infrastructure.research import (
    ArxivResearchSourceProvider,
    CANDIDATE_TASK_SCHEMAS,
    CatalogBoundResearchSourceProvider,
    FilesystemHarnessArtifactPort,
    FilesystemResearchRunStore,
    StructuredResearchCandidateWorker,
)
from infrastructure.storage.indexing import (
    GraphStorageIndexIdentity,
    GraphStorageIndexSnapshot,
    LocalGraphStorageIndexStore,
)
from interfaces.api.app import create_app
from interfaces.cli.commands import mcp as mcp_commands
from interfaces.composition import research as research_composition
from interfaces.composition.research import build_research_runtime_composition
from interfaces.composition.research_settings import ResearchRuntimeSettings
from interfaces.mcp.server import NewsMCPServerAdapter
from interfaces.mcp.stdio_server import run_stdio
from interfaces.services.mcp_service import MCPApplicationService
from interfaces.services.research_service import (
    ResearchActorInput,
    ResearchAnalyzeInput,
    ResearchServiceError,
)


_PAPER_ID = "2607.00001"
_RUN_ID = "recorded-production-analysis"
_MCP_RUN_ID = "recorded-production-mcp-analysis"
_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "research_runtime_production"
)
_LLM_FIXTURES = {
    "candidate_three_minute_read": "openai_candidate_three_minute_read.json",
    "candidate_taxonomy": "openai_candidate_taxonomy.json",
    "candidate_experiment_claims": "openai_candidate_experiment_claims.json",
}


class _RecordedHTTPResponse:
    def __init__(
        self,
        body: bytes,
        *,
        url: str,
        content_type: str,
        file_name: str | None = None,
    ) -> None:
        headers = Message()
        headers["Content-Type"] = content_type
        headers["Content-Length"] = str(len(body))
        if file_name is not None:
            headers["Content-Disposition"] = f'attachment; filename="{file_name}"'
        self.body = body
        self.headers = headers
        self.status = 200
        self.url = url

    def __enter__(self) -> "_RecordedHTTPResponse":
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def geturl(self) -> str:
        return self.url

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


def _recorded_environment(tmp_path: Path) -> dict[str, str]:
    models_config = _write_recorded_models_config(tmp_path)
    return {
        "RECORDED_RESEARCH_API_KEY": "recorded-transport-only",
        "NEWS_RESEARCH_LLM_API_KEY_ENV": "RECORDED_RESEARCH_API_KEY",
        "NEWS_RESEARCH_LLM_BASE_URL": "https://recorded-llm.invalid/v1",
        "NEWS_RESEARCH_LLM_MODEL": "recorded-research-model",
        "NEWS_RESEARCH_LLM_ROUTE_ID": "recorded-research",
        "NEWS_RESEARCH_LLM_MAX_ATTEMPTS": "1",
        "NEWS_MODELS_CONFIG": str(models_config),
        "NEWS_RESEARCH_MODELS_CONFIG": str(models_config),
        "NEWS_RESEARCH_SOURCE_PROVIDER": "arxiv",
        "NEWS_RESEARCH_PARSER_BACKENDS": "marker",
        "NEWS_RESEARCH_ALLOW_ABSTRACT_FALLBACK": "1",
        "NEWS_RESEARCH_RAG_BACKEND": "local",
        "NEWS_RESEARCH_ROOT": str(tmp_path / "research"),
        "NEWS_RESEARCH_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
        "NEWS_RESEARCH_RUN_STORE_ROOT": str(tmp_path / "run-store"),
        "NEWS_RESEARCH_RAG_LOCAL_ROOT": str(tmp_path / "chunks"),
        "NEWS_RESEARCH_RAG_TRANSCRIPT_ROOT": str(
            tmp_path / "research" / "rag" / "transcripts"
        ),
    }


def _write_recorded_models_config(tmp_path: Path) -> Path:
    supported_keywords: set[str] = set()
    for schema in CANDIDATE_TASK_SCHEMAS.values():
        contract = compile_structured_output_contract(schema)
        supported_keywords.update(
            structured_output_enforcement_keywords(contract.canonical_schema)
        )
    release_path = (
        Path(__file__).resolve().parents[3]
        / "configs"
        / "llm"
        / "structured_output"
        / "releases"
        / "recorded-reference-native-v1.json"
    )
    release_record = json.loads(release_path.read_text(encoding="utf-8"))
    config_path = tmp_path / "recorded-models.json"
    config_path.write_text(
        json.dumps(
            {
                "structured_output_releases": {
                    release_record["release_id"]: release_record
                },
                "model_groups": {
                    "recorded-research": {
                        "deployments": [
                            {
                                "deployment_id": "recorded-research-model",
                                "provider": "openai-compatible",
                                "provider_name": "recorded",
                                "model": "recorded-research-model",
                                "api_base": "https://recorded-llm.invalid/v1",
                                "api_key_env": "RECORDED_RESEARCH_API_KEY",
                                "structured_output_capability": {
                                    "mode": "native_strict",
                                    "supported_dialect": (
                                        LOCAL_STRUCTURED_OUTPUT_DIALECT
                                    ),
                                    "supported_keywords": sorted(
                                        supported_keywords
                                    ),
                                    "supports_local_refs": True,
                                    "supports_stream_terminal_validation": True,
                                    "revision": "recorded-research-native-v1",
                                    "release_id": release_record["release_id"],
                                    "release_digest": release_record["record_digest"],
                                },
                            }
                        ]
                    }
                },
                "routes": {
                    "recorded-research": {
                        "model_group": "recorded-research"
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return config_path


def _settings(tmp_path: Path) -> ResearchRuntimeSettings:
    return ResearchRuntimeSettings.from_env(
        _recorded_environment(tmp_path),
        cwd=tmp_path,
    )


def _install_recorded_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> ResearchRuntimeSettings:
    for name, value in _recorded_environment(tmp_path).items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(
        "NEWS_ACTIVITY_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.setenv("NEWS_PRELOAD_RERANKER", "0")
    for name in (
        "NEWS_API_KEYS",
        "NEWS_DATABASE_DSN",
        "NEWS_EVENT_OPERATOR_PRINCIPAL_ID",
        "NEWS_EVENT_OPERATOR_ROLE",
        "NEWS_TENANT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    return ResearchRuntimeSettings.from_env(cwd=tmp_path)


def _install_recorded_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[str]]:
    source_requests: list[str] = []
    llm_tasks: list[str] = []
    metadata = (_FIXTURE_ROOT / "arxiv_metadata.xml").read_bytes()
    source = (_FIXTURE_ROOT / "arxiv_source.tex").read_bytes()

    def source_transport(request: Request, _policy: Any) -> _RecordedHTTPResponse:
        url = request.full_url
        source_requests.append(url)
        if "/api/query" in url:
            return _RecordedHTTPResponse(
                metadata,
                url=url,
                content_type="application/atom+xml",
            )
        if f"/e-print/{_PAPER_ID}" in url:
            return _RecordedHTTPResponse(
                source,
                url=url,
                content_type="application/octet-stream",
                file_name=f"{_PAPER_ID}.tex",
            )
        raise AssertionError(f"unexpected external source request: {url}")

    def llm_transport(request: Request, timeout_seconds: float) -> bytes:
        assert request.full_url == "https://recorded-llm.invalid/v1/chat/completions"
        assert timeout_seconds > 0
        payload = json.loads(bytes(request.data or b"").decode("utf-8"))
        schema_name = payload["response_format"]["json_schema"]["name"]
        task = str(schema_name).removeprefix("research_")
        fixture_name = _LLM_FIXTURES.get(task)
        if fixture_name is None:
            raise AssertionError(f"unexpected Research candidate task: {task}")
        llm_tasks.append(task)
        return (_FIXTURE_ROOT / fixture_name).read_bytes()

    monkeypatch.setattr(arxiv_transport, "ensure_robots_allowed", lambda *_args: None)
    monkeypatch.setattr(
        arxiv_transport,
        "open_request_with_fetch_policy",
        source_transport,
    )
    monkeypatch.setattr(openai_transport, "_urlopen_transport", llm_transport)
    return source_requests, llm_tasks


def test_recorded_transports_execute_full_production_research_analysis(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _install_recorded_environment(monkeypatch, tmp_path)
    source_requests, llm_tasks = _install_recorded_transports(monkeypatch)

    provider = research_composition.default_research_runtime_provider()
    provider.reset()
    original_service_factory = type(provider).service_factory
    resolved_service_ids: list[int] = []

    def tracked_service_factory(actual_provider):
        service = original_service_factory(actual_provider)
        if actual_provider is provider:
            resolved_service_ids.append(id(service))
        return service

    monkeypatch.setattr(type(provider), "service_factory", tracked_service_factory)

    try:
        with TestClient(create_app(audit_emitter_factory=None)) as client:
            analyzed = client.post(
                "/api/v1/research/papers/analyze",
                json={
                    "paperId": _PAPER_ID,
                    "sourceUrl": f"https://arxiv.org/abs/{_PAPER_ID}",
                    "runId": _RUN_ID,
                },
            )
            mcp_analyzed = client.post(
                "/api/v1/mcp/tools/news.research.analyze_paper/call",
                json={
                    "arguments": {
                        "paper_id": _PAPER_ID,
                        "source_url": f"https://arxiv.org/abs/{_PAPER_ID}",
                        "run_id": _MCP_RUN_ID,
                    }
                },
            )
            http_research = client.get(
                f"/api/v1/research/papers/{_PAPER_ID}/analysis"
            )
            http_mcp = client.post(
                "/api/v1/mcp/tools/news.research.paper_analysis/call",
                json={"arguments": {"paper_id": _PAPER_ID}},
            )

        local_mcp = MCPApplicationService().call_tool(
            "news.research.paper_analysis",
            {"paper_id": _PAPER_ID},
        )
        cli_exit = mcp_commands.mcp_call(
            argparse.Namespace(
                tool_name="news.research.paper_analysis",
                args_json=json.dumps({"paper_id": _PAPER_ID}),
                json=True,
            )
        )
        cli_payload = json.loads(capsys.readouterr().out)

        stdio_output = StringIO()
        run_stdio(
            input_stream=StringIO(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "recorded-stdio",
                        "method": "tools/call",
                        "params": {
                            "name": "news.research.paper_analysis",
                            "arguments": {"paper_id": _PAPER_ID},
                        },
                    }
                )
                + "\n"
            ),
            output_stream=stdio_output,
        )
        stdio_payload = json.loads(stdio_output.getvalue())["result"]
        server_payload = NewsMCPServerAdapter().call_tool(
            "news.research.paper_analysis",
            {"paper_id": _PAPER_ID},
        )

        assert analyzed.status_code == 200, analyzed.text
        response = analyzed.json()["data"]
        assert mcp_analyzed.status_code == 200
        mcp_analyze_payload = mcp_analyzed.json()["data"]
        assert mcp_analyze_payload["success"] is True
        assert {
            key: mcp_analyze_payload["data"][key]
            for key in ("paperId", "status")
        } == {
            key: response[key]
            for key in ("paperId", "status")
        }
        assert mcp_analyze_payload["data"]["runId"] == _MCP_RUN_ID
        assert http_research.status_code == 200
        assert http_mcp.status_code == 200
        assert local_mcp.success is True
        assert cli_exit == 0
        assert cli_payload["success"] is True
        assert stdio_payload["success"] is True
        assert server_payload["success"] is True

        surface_payloads = {
            "http_research": http_research.json()["data"],
            "http_mcp": http_mcp.json()["data"]["data"],
            "local_mcp": local_mcp.data,
            "stdio_mcp": stdio_payload["data"],
            "cli_mcp": cli_payload["data"],
            "server_adapter": server_payload["data"],
        }
        expected_surface_payload = surface_payloads["http_research"]
        assert all(
            payload == expected_surface_payload
            for payload in surface_payloads.values()
        )
        assert expected_surface_payload["runId"] == _MCP_RUN_ID
        assert expected_surface_payload["paperId"] == _PAPER_ID
        assert expected_surface_payload["status"] == "succeeded"
        assert expected_surface_payload["quality"]["passed"] is True

        composition = provider.get()
        assert composition.available is True
        assert composition.settings == settings
        service = composition.service
        assert resolved_service_ids == [id(service)] * 8
        runtime = service._analyze_use_case._runtime
        assert isinstance(runtime, ResearchSinglePaperRuntime)
        assert isinstance(runtime.source_provider, CatalogBoundResearchSourceProvider)
        assert isinstance(runtime.source_provider._fallback, ArxivResearchSourceProvider)
        assert isinstance(runtime.llm_worker, StructuredResearchCandidateWorker)
        assert isinstance(runtime.artifact_port, FilesystemHarnessArtifactPort)
        assert isinstance(service._run_store, FilesystemResearchRunStore)
        index_files = sorted(
            (settings.artifact.root / "graph-index").glob("index-*.json")
        )
        assert len(index_files) == 2
        index_snapshots = tuple(
            GraphStorageIndexSnapshot.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
            for path in index_files
        )
        assert {
            snapshot.identity.run_id for snapshot in index_snapshots
        } == {_RUN_ID, _MCP_RUN_ID}
        assert all(snapshot.artifact_records for snapshot in index_snapshots)
        assert all(snapshot.event_records for snapshot in index_snapshots)

        record = service._run_store.get_by_run_id(_RUN_ID)
        assert record is not None
        assert isinstance(record.result, ResearchAnalysisResult)
        result = record.result

        assert response["status"] == "succeeded"
        assert result.succeeded is True
        assert result.quality.passed is True
        assert result.rag_context is not None
        assert {
            item.evidence_type for item in result.rag_context.accepted_evidence
        } == {"method", "experiment", "limitation", "claim_support"}
        assert result.rag_context.metadata["budget"]["max_replans"] == 3
        assert result.rag_context.gap_report.missing_information == []
        assert len(source_requests) == 3
        assert "/api/query?" in source_requests[0]
        assert f"id%3A{_PAPER_ID}" in source_requests[0]
        assert source_requests[1] == f"https://arxiv.org/e-print/{_PAPER_ID}"
        assert source_requests[2] == f"https://arxiv.org/e-print/{_PAPER_ID}"
        expected_llm_tasks = [
            "candidate_three_minute_read",
            "candidate_taxonomy",
            "candidate_experiment_claims",
        ]
        assert llm_tasks == expected_llm_tasks * 2

        phases = {entry.phase for entry in result.transcript.entries()}
        assert {"PLAN", "EXECUTE", "VERIFY"}.issubset(phases)
        phase_events = [
            event
            for event in result.trace.events
            if event.event_type is HarnessEventType.GRAPH_PHASE_TRANSITION_RECORDED
        ]
        assert {
            (
                str(event.payload["graph_phase_transition"]["phase"]).upper(),
                event.payload["graph_phase_transition"]["boundary"],
            )
            for event in phase_events
        } >= {
            (phase, boundary)
            for phase in ("PLAN", "EXECUTE", "VERIFY")
            for boundary in ("entry", "exit")
        }
        gate_events = [
            event
            for event in result.trace.events
            if event.event_type is HarnessEventType.GATE_EVALUATED
        ]
        assert gate_events
        assert all(event.payload["passed"] is True for event in gate_events)
        recorded_gate_refs = {
            str(event.payload.get("reference"))
            for event in gate_events
        }
        assert set(PAPER_ANALYSIS_GATE_REFERENCES).issubset(recorded_gate_refs)

        scoped_event_port_factory = runtime.scoped_event_port_factory
        assert scoped_event_port_factory is not None
        graph_event_port = scoped_event_port_factory(
            _RUN_ID,
            result.actor_scope.to_metadata(),
        )
        graph_recovery = graph_event_port.recover_graph(_RUN_ID)
        assert graph_recovery.graph is not None
        assert graph_recovery.state is not None
        assert graph_recovery.decision_commits
        assert graph_recovery.projection_commits
        assert graph_recovery.pending_decisions == ()
        assert graph_recovery.pending_activity_results == ()
        assert graph_recovery.pending_observations == ()
        graph_ref = graph_recovery.state.graph_ref
        assert graph_ref.graph_id == graph_recovery.graph.graph_id
        assert graph_ref.graph_ref == graph_recovery.graph.graph_ref
        assert graph_ref.schema_version == graph_recovery.graph.schema_version
        assert graph_ref.compiler_version == graph_recovery.graph.compiler_version
        assert (
            graph_ref.condition_policy_version
            == graph_recovery.graph.condition_policy_version
        )
        assert graph_ref.checksum == graph_recovery.graph.checksum
        assert graph_ref.graph_ref.contract_id == "research.paper_analysis.graph"
        assert graph_ref.graph_ref.version == "1"
        projection_by_cause = {
            commit.cause_checksum: commit
            for commit in graph_recovery.projection_commits
        }
        for decision_commit in graph_recovery.decision_commits:
            decision = decision_commit.decision
            assert decision.graph_ref == graph_ref
            projection = projection_by_cause[decision.decision_checksum]
            assert decision_commit.sequence < projection.sequence
            assert projection.state.graph_ref == graph_ref

        legacy_transition_count = sum(
            1
            for event in result.trace.events
            if str(getattr(event.event_type, "value", event.event_type))
            == "transition_committed"
        )
        assert legacy_transition_count == 0

        rag_metadata = result.rag_context.metadata
        rag_graph_identity = rag_metadata["graph_identity"]
        assert rag_graph_identity["run_id"] == _RUN_ID
        assert rag_graph_identity["graph_id"] == graph_ref.graph_id
        assert rag_graph_identity["graph_version"] == graph_ref.graph_ref.version
        assert rag_graph_identity["graph_ref"] == graph_ref.graph_ref.exact_ref
        assert rag_graph_identity["graph_checksum"] == graph_ref.checksum
        assert rag_graph_identity["stage_id"] == "run_research_rag"
        rag_transcript = rag_metadata["transcript"]
        assert rag_transcript["session_id"] == rag_metadata["session_id"]
        rag_started = next(
            event
            for event in rag_transcript["events"]
            if event["event_type"] == "rag_session_started"
        )
        rag_session = rag_started["payload"]["session"]
        assert rag_session["session_id"] == rag_metadata["session_id"]
        assert rag_session["graph_identity"] == rag_graph_identity

        artifact_refs = response["metadata"]["artifactRefs"]
        assert {
            "research-analysis",
            "research-reader-payload",
            "research-quality-result",
            "harness-trace",
            "harness-transcript",
        }.issubset(artifact_refs)
        manifest_path = settings.artifact.root / _RUN_ID / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "newsroom.graph-terminal-manifest/v2"
        assert manifest["graph_id"] == graph_ref.graph_id
        assert manifest["graph_version"] == graph_ref.graph_ref.version
        assert manifest["normalized_graph_checksum"] == graph_ref.checksum
        assert manifest["status"] == "succeeded"
        assert manifest["publication"] is not None
        assert all(
            item["content_checksum"]
            for item in manifest["artifacts"]
        )
        assert len(list((settings.run_store.root / "records").glob("*.json"))) == 2
        chunk_state = json.loads(
            (settings.rag.local_root / "research_paper_chunks.json").read_text(
                encoding="utf-8"
            )
        )
        assert chunk_state["payloads"]
        assert any("\n" in item["content"] for item in chunk_state["payloads"])

        actor = ResearchActorInput()
        analysis_before_restart = service.get_analysis(_PAPER_ID, actor=actor)
        trace_before_restart = service.get_trace(_RUN_ID, actor=actor)
        provider.reset()

        reopened = provider.get()
        assert reopened.available is True
        assert reopened.service is not service
        assert reopened.service._run_store is not service._run_store
        assert (
            reopened.service.get_analysis(_PAPER_ID, actor=actor)
            == analysis_before_restart
        )
        assert reopened.service.get_trace(_RUN_ID, actor=actor) == trace_before_restart

        restored_record = reopened.service._run_store.get_by_run_id(_RUN_ID)
        assert restored_record is not None
        restored_rag_entry = next(
            entry
            for entry in restored_record.result.transcript.entries()
                if entry.node_id == "run_research_rag"
            and entry.metadata["event_type"] == "graph_worker_result_recorded"
        )
        assert restored_rag_entry.rag_session_refs
        assert restored_rag_entry.context_pack_refs
        assert set(restored_rag_entry.context_pack_refs).issubset(
            restored_rag_entry.output_refs
        )
        assert restored_record.result.rag_context is not None
        assert (
            restored_record.result.rag_context.metadata["transcript_ref"]
            in restored_rag_entry.output_refs
        )
        reopened_runtime = reopened.service._analyze_use_case._runtime
        restored_artifact = reopened_runtime.artifact_port.read_artifact(
            artifact_refs["research-analysis"]
        )
        assert restored_artifact["artifact_type"] == "research-analysis"
        assert restored_artifact["payload"]["paper_id"] == _PAPER_ID
    finally:
        provider.reset()


def test_explicit_low_rag_replan_budget_halts_and_persists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("RECORDED_RESEARCH_API_KEY", "recorded-transport-only")
    monkeypatch.setenv(
        "NEWS_ACTIVITY_ENCRYPTION_KEY",
        Fernet.generate_key().decode("ascii"),
    )
    monkeypatch.delenv("NEWS_DATABASE_DSN", raising=False)
    source_requests, llm_tasks = _install_recorded_transports(monkeypatch)

    composition = build_research_runtime_composition(settings=settings)
    reopened = None
    try:
        service = composition.service
        with pytest.raises(ResearchServiceError) as failed:
            service.analyze_paper(
                ResearchAnalyzeInput(
                    paper_id=_PAPER_ID,
                    source_url=f"https://arxiv.org/abs/{_PAPER_ID}",
                    run_id="recorded-production-low-rag-budget",
                    user_id="recorded-smoke-user",
                    # One query and no replans: the first round can only run
                    # the generic query, which cannot cover every required type.
                    options={"max_replans": 0, "rag_max_replans": 0, "rag_max_queries": 1},
                )
            )

        assert failed.value.code == "research_run_failed"
        assert failed.value.details["status"] == "halted"
        record = service._run_store.get_by_run_id(
            "recorded-production-low-rag-budget"
        )
        assert record is not None
        result = record.result
        assert result.status == "halted"
        assert result.rag_context is not None
        assert result.rag_context.metadata["budget"]["max_replans"] == 0
        assert result.rag_context.metadata["budget_snapshot"]["replans_used"] == 0
        assert result.rag_context.gap_report.missing_information
        halted_manifest_path = (
            settings.artifact.root
            / "recorded-production-low-rag-budget"
            / "manifest.json"
        )
        halted_manifest = json.loads(
            halted_manifest_path.read_text(encoding="utf-8")
        )
        assert halted_manifest["schema_version"] == (
            "newsroom.graph-terminal-manifest/v2"
        )
        assert halted_manifest["status"] == "halted"
        assert halted_manifest["publication"] is None
        halted_identity = GraphStorageIndexIdentity.from_manifest(
            service._analyze_use_case._runtime.artifact_port.read_terminal_manifest(
                "recorded-production-low-rag-budget"
            )
        )
        halted_snapshot = LocalGraphStorageIndexStore(
            settings.artifact.root / "graph-index"
        ).read(halted_identity)
        assert halted_snapshot.identity.run_id == "recorded-production-low-rag-budget"
        assert halted_snapshot.identity.terminal_manifest_hash == halted_manifest[
            "manifest_hash"
        ]
        assert halted_snapshot.artifact_records
        assert halted_snapshot.event_records
        reread_snapshot = LocalGraphStorageIndexStore(
            settings.artifact.root / "graph-index"
        ).read(halted_identity)
        assert reread_snapshot.snapshot_checksum == halted_snapshot.snapshot_checksum
        halted_rag_entry = next(
            entry
            for entry in result.transcript.entries()
                if entry.node_id == "run_research_rag"
            and entry.metadata["event_type"] == "graph_worker_result_recorded"
        )
        assert halted_rag_entry.context_pack_refs == (
            result.rag_context.metadata["context_pack_id"],
        )
        assert halted_rag_entry.context_pack_refs[0].endswith("/empty")
        assert halted_rag_entry.rag_session_refs
        assert len(result.quality.quality_flags) == len(
            {
                json.dumps(flag.to_dict(), sort_keys=True)
                for flag in result.quality.quality_flags
            }
        )
        assert source_requests
        assert llm_tasks == []

        persisted = result.to_persistence_dict()
        assert (
            ResearchAnalysisResult.from_dict(persisted).to_persistence_dict()
            == persisted
        )
        composition.close()

        reopened = build_research_runtime_composition(settings=settings)
        restored = reopened.service._run_store.get_by_run_id(
            "recorded-production-low-rag-budget"
        )
        assert restored is not None
        assert restored.result.to_persistence_dict() == persisted
        restored_rag_entry = next(
            entry
            for entry in restored.result.transcript.entries()
                if entry.node_id == "run_research_rag"
            and entry.metadata["event_type"] == "graph_worker_result_recorded"
        )
        assert restored.result.rag_context is not None
        assert (
            restored.result.rag_context.metadata["transcript_ref"]
            in restored_rag_entry.output_refs
        )
    finally:
        if reopened is not None:
            reopened.close()
        composition.close()
