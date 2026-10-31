"""Build the complete production tool registry over fault-injecting dependencies.

``build_tool_environment`` wires every optional dependency of
``build_business_tool_registry`` so all tools are registered, and keeps a handle
on each stand-in so a test can script its faults. Keyword overrides replace a
dependency (for example ``source_fetch_policy`` or ``web_search_provider``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.foundation.registry.source_registry import SourceRegistry
from backend.tools import build_business_tool_registry
from framework.agent.artifacts import ArtifactManager
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool import ToolRegistry
from interfaces.services.report_service import ReportApplicationService
from tests.tools.harness.fault_stores import (
    FaultyApprovalStore,
    FaultyMemoryRecall,
    FaultyReportRepository,
    FaultyTaskQueue,
    FaultyVectorStore,
)

WEBHOOK_DOMAIN = "hooks.example.com"
QDRANT_COLLECTIONS = ("research_documents", "news_items")
SLACK_WEBHOOK_URL = "https://hooks.slack.com/services/T0000/B0000/fault-harness"
EMAIL_FROM = "newsroom-bot@example.com"
EMAIL_DOMAIN = "example.com"


def tool_test_identity(run_id: str = "run-tool-stability") -> GraphExecutionIdentity:
    return GraphExecutionIdentity(
        run_id=run_id,
        graph_id="graph.tool_stability",
        graph_version="1",
        graph_ref="graph.tool_stability@1",
        graph_checksum="sha256:" + "0" * 64,
        node_id="node.tools",
        node_instance_id="node.tools:1",
        activity_id="activity.tools",
        attempt=1,
    )


@dataclass(frozen=True)
class ToolEnvironment:
    root: Path
    identity: GraphExecutionIdentity
    artifact_manager: ArtifactManager
    reports: FaultyReportRepository
    vectors: FaultyVectorStore
    memory: FaultyMemoryRecall
    approvals: FaultyApprovalStore
    tasks: FaultyTaskQueue
    local_json_root: Path
    rss_feed_path: Path
    registry: ToolRegistry

    def release(self) -> None:
        """Unblock every stalled stand-in so its worker thread can finish."""

        for plan in (
            self.reports.plan,
            self.vectors.plan,
            self.memory.plan,
            self.approvals.plan,
            self.tasks.plan,
        ):
            plan.release()


def build_tool_environment(root: Path, **overrides: Any) -> ToolEnvironment:
    identity = overrides.pop("execution_identity", None) or tool_test_identity()
    reports = FaultyReportRepository()
    vectors = FaultyVectorStore()
    memory = FaultyMemoryRecall(identity)
    approvals = FaultyApprovalStore()
    tasks = FaultyTaskQueue()
    artifact_manager = ArtifactManager(root / "artifacts")
    local_json_root = root / "local_json"
    rss_feed_path = root / "feeds" / "updates.xml"
    notification_options: dict[str, Any] = {
        "allowed_webhook_domains": [WEBHOOK_DOMAIN, "127.0.0.1"],
        # Fault servers listen on loopback; an operator would list an intranet range.
        "allowed_private_networks": ["127.0.0.0/8"],
        "slack_webhook_url": SLACK_WEBHOOK_URL,
        "allowed_slack_domains": ["hooks.slack.com"],
        # Construction does not connect; email tests point this at a FaultSMTPServer.
        "smtp_host": "127.0.0.1",
        "smtp_port": 9,
        "smtp_use_starttls": False,
        "email_from_address": EMAIL_FROM,
        "allowed_email_domains": [EMAIL_DOMAIN],
        "rss_feed_path": rss_feed_path,
    }
    notification_options.update(overrides.pop("notification_options", None) or {})
    options: dict[str, Any] = {
        "include_network_tools": True,
        "include_dangerous_tools": True,
        "run_id": identity.run_id,
        "execution_identity": identity,
        "artifact_manager": artifact_manager,
        "local_json_root": local_json_root,
        "memory_recall": memory,
        "qdrant_vector_store": vectors,
        "qdrant_document_store": vectors,
        "qdrant_collections": list(QDRANT_COLLECTIONS),
        "postgres_repository": reports,
        "postgres_source_health_repository": reports,
        "persistence_repository": reports,
        "report_service": ReportApplicationService(repository=reports),
        "approval_store": approvals,
        "task_queue": tasks,
        "source_registry": SourceRegistry([]),
        "notification_options": notification_options,
    }
    options.update(overrides)
    return ToolEnvironment(
        root=root,
        identity=identity,
        artifact_manager=artifact_manager,
        reports=reports,
        vectors=vectors,
        memory=memory,
        approvals=approvals,
        tasks=tasks,
        local_json_root=local_json_root,
        rss_feed_path=rss_feed_path,
        registry=build_business_tool_registry(**options),
    )
