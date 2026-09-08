from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from framework.agent.models import AgentLoopResult, AgentLoopStatus
from framework.events.canonical import checksum_for
from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.control_plane.graph_runtime import (
    HarnessGraphActivityResultStatus,
)
from framework.harness.workers.result import HarnessWorkerResult
from framework.memory.namespace import MemoryNamespaceError
from infrastructure.research.artifact_port import FilesystemHarnessArtifactPort
from interfaces.composition.agent_loop_smoke import build_agent_loop_graph_smoke_service
from infrastructure.storage.memory.namespace import FilesystemMemoryNamespaceStore
from interfaces.services.agent_loop_smoke_service import (
    AGENT_LOOP_SMOKE_EVENT_TYPES,
    AGENT_LOOP_SMOKE_OUTCOME_SCHEMA,
    AgentLoopGraphSmokeVerifyGate,
    _deny_network_connections,
    build_test_agent_loop_graph,
)


_STARTED_AT = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self._current = _STARTED_AT

    def __call__(self) -> datetime:
        value = self._current
        self._current += timedelta(milliseconds=1)
        return value


def test_graph_smoke_persists_verified_events_metrics_and_node_identity(
    tmp_path,
) -> None:
    port = FilesystemHarnessArtifactPort(tmp_path)
    service = build_agent_loop_graph_smoke_service(
        artifact_root=tmp_path,
        clock=_Clock(),
    )

    result = service.run(
        topic="agentic research",
        run_id="test-agent-loop-success",
    )

    assert result.status == "succeeded"
    assert result.output == {
        "summary": "Deterministic AgentLoop analysis for agentic research.",
        "tool_used": "memory.recall",
        "confidence": "high",
    }
    assert result.metrics["llm_calls"] == 3
    assert result.metrics["tool_calls"] == 1
    assert result.metrics["judge_retries"] == 1
    assert result.metrics["token_usage"]["total_tokens"] == 60
    assert result.event_types == AGENT_LOOP_SMOKE_EVENT_TYPES
    assert result.network_calls == 0
    assert result.preflight_ref.startswith("sha256:")
    assert result.activity_receipt_ref.startswith("sha256:")
    assert result.verify_evidence_ref.startswith("sha256:")

    manifest = port.read_terminal_manifest(result.run_id)
    assert manifest.status.value == "succeeded"
    assert manifest.publication is None
    assert manifest.gate_evidence_refs == (result.verify_evidence_ref,)
    assert manifest.terminal_node_ids == ("run-agent-loop",)
    assert len(manifest.artifacts) == 4
    assert all(
        artifact.artifact_key.startswith("graph-result-")
        and artifact.metadata["graph_result_ref_only"] is True
        and artifact.metadata["node_instance_id"] == result.node_instance_id
        for artifact in manifest.artifacts
    )

    outcome = next(
        artifact
        for artifact in manifest.artifacts
        if artifact.metadata.get("artifact_role") == "agent_loop_smoke_outcome"
    )
    assert outcome.metadata["artifact_schema_version"] == AGENT_LOOP_SMOKE_OUTCOME_SCHEMA
    assert outcome.metadata["llm_calls"] == 3
    assert outcome.metadata["tool_calls"] == 1
    assert outcome.metadata["token_usage"]["total_tokens"] == 60
    assert outcome.metadata["attempt_id"] == (
        "test-agent-loop-success-agent-loop-attempt-1"
    )
    persisted = port.read_graph_result_artifact(
        outcome.ref,
        expected_run_id=result.run_id,
    )
    assert persisted["payload"]["verify"]["passed"] is True
    assert persisted["payload"]["verify_evidence_ref"] == result.verify_evidence_ref
    assert [
        item["event_type"] for item in persisted["payload"]["events"]
    ] == list(AGENT_LOOP_SMOKE_EVENT_TYPES)
    assert persisted["payload"]["activity_receipt"]["node_output_commit"][
        "commit_ref"
    ].startswith("sha256:")
    observation = next(
        item["observation"] for item in persisted["payload"]["events"]
        if item["event_type"] == "tool_observation"
    )
    assert observation["result"]["status"] == "succeeded"
    recall = observation["result"]["output"]
    assert recall["result_count"] == 1
    diagnostics = recall["diagnostics"]
    assert diagnostics["execution_identity"]["run_id"] == result.run_id
    assert diagnostics["execution_identity"]["node_instance_id"] == result.node_instance_id
    assert diagnostics["input_snapshot_ref"].startswith("sha256:")
    namespace_ref, = diagnostics["namespace_refs"]
    revision = FilesystemMemoryNamespaceStore(
        tmp_path / "_state" / "agent-loop-memory" / "namespaces"
    ).read(namespace_ref)
    assert diagnostics["namespace_checksums"][namespace_ref] == revision.descriptor.source_checksum
    assert recall["results"][0]["record"]["memory_id"] == revision.records()[0].memory_id
    assert revision.records()[0].content == "Fixture memory for agentic research"


def test_graph_smoke_rejects_failed_memory_tool_despite_accepted_llm_output(
    tmp_path, monkeypatch,
) -> None:
    def corrupt_read(self, ref):
        raise MemoryNamespaceError("corrupt smoke memory")

    service = build_agent_loop_graph_smoke_service(artifact_root=tmp_path, clock=_Clock())
    original_factory = service._memory_recall_factory

    def admitted_then_corrupt(activity, topic, agent):
        recall = original_factory(activity, topic, agent)
        monkeypatch.setattr(FilesystemMemoryNamespaceStore, "read", corrupt_read)
        return recall

    monkeypatch.setattr(service, "_memory_recall_factory", admitted_then_corrupt)
    with pytest.raises(HarnessValidationError) as error:
        service.run(topic="agentic research", run_id="smoke-corrupt")
    assert error.value.code == "test_agent_loop_graph_verify_failed", error.value.__cause__
    assert "memory_recall_evidence_invalid" in error.value.details["failures"]
    assert not (tmp_path / "smoke-corrupt" / "manifest.json").exists()


def test_graph_smoke_graph_is_single_node_graph_only_fixture() -> None:
    graph = build_test_agent_loop_graph()

    assert graph.graph_id == "test-agent-loop.graph"
    assert graph.entry_node_ids == ("run-agent-loop",)
    assert graph.terminal_node_ids == ("run-agent-loop",)
    assert graph.edges == ()
    assert graph.nodes[0].metadata["worker_type"] == "agent_loop"
    assert "workflow" not in graph.nodes[0].metadata


def test_network_guard_blocks_connection_attempts() -> None:
    with _deny_network_connections() as attempts:
        with pytest.raises(HarnessValidationError) as captured:
            socket.create_connection(("127.0.0.1", 9))

    assert captured.value.code == "test_agent_loop_network_access_forbidden"
    assert attempts == ["blocked"]


@pytest.mark.parametrize(
    ("diagnostics", "expected_failure"),
    [
        ({"requested_tools": ["memory.recall"]}, "agent_loop_output_missing"),
        ({"requested_tools": 7}, "tool_policy_evidence_invalid"),
    ],
)
def test_verify_gate_fails_closed_for_malformed_worker_candidate(
    diagnostics,
    expected_failure: str,
) -> None:
    worker = HarnessWorkerResult(
        status="succeeded",
        output={},
        diagnostics=diagnostics,
    )
    receipt = SimpleNamespace(
        worker_result=worker,
        node_output_commit=SimpleNamespace(
            candidate=SimpleNamespace(
                output_refs={"agent_loop_result": checksum_for({"unexpected": True})}
            ),
            commit_ref=checksum_for({"commit": "malformed-worker"}),
        ),
        graph_result=SimpleNamespace(
            status=HarnessGraphActivityResultStatus.SUCCEEDED,
            result_checksum=checksum_for({"result": "malformed-worker"}),
        ),
        activity=SimpleNamespace(node_instance_id="node-instance-1"),
    )
    context = SimpleNamespace(
        preflight=SimpleNamespace(is_valid=True),
        receipt=receipt,
        result=AgentLoopResult(
            success=True,
            status=AgentLoopStatus.SUCCEEDED,
        ),
        network_calls=0,
        input_ref=checksum_for({"input": "malformed-worker"}),
    )

    result = AgentLoopGraphSmokeVerifyGate().evaluate(context)

    assert result.passed is False
    assert expected_failure in result.details["failures"]
    assert "committed_output_ref_mismatch" in result.details["failures"]
