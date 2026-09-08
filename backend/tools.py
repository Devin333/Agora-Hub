from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.layers.analysis.tools import register_quality_tools
from backend.layers.output.postgres_tools import register_postgres_tools
from backend.layers.output.tools import register_report_tools
from backend.layers.signal.connector_tools import register_arxiv_tools, register_github_tools
from backend.layers.signal.tools import FetchText, register_source_tools
from framework.agent.artifacts import ArtifactManager
from framework.memory import MemoryRuntime
from framework.memory.policy import MemoryPolicy
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.tool import (
    ToolRegistry,
)
from framework.shared.graph_identity import GraphExecutionIdentity
from infrastructure.tools import (
    build_builtin_dangerous_tool_registry,
    build_builtin_tool_registry,
)
from infrastructure.external.sources import default_arxiv_connector, default_github_connector
from infrastructure.tools.web_search_tools import WebSearchProvider


def build_business_tool_registry(
    *,
    artifact_manager: ArtifactManager | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    local_json_root: str | Path | None = None,
    source_registry: Any | None = None,
    source_fetch_text: FetchText | None = None,
    source_fetch_policy: Any | None = None,
    source_tool_runtime: Any | None = None,
    source_rate_limiter: Any | None = None,
    allowed_source_domains: list[str] | None = None,
    source_health_manager: Any | None = None,
    arxiv_connector: Any | None = None,
    github_connector: Any | None = None,
    web_search_provider: WebSearchProvider | None = None,
    vector_store: Any | None = None,
    memory_ingestion_service: Any | None = None,
    memory_runtime: MemoryRuntime | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    qdrant_vector_store: Any | None = None,
    qdrant_document_store: Any | None = None,
    report_service: Any | None = None,
    persistence_repository: Any | None = None,
    postgres_repository: Any | None = None,
    postgres_source_health_repository: Any | None = None,
    approval_store: Any | None = None,
    task_queue: Any | None = None,
    notification_options: dict[str, Any] | None = None,
    include_network_tools: bool = False,
    include_dangerous_tools: bool = False,
) -> ToolRegistry:
    registry = build_builtin_tool_registry(
        artifact_manager=artifact_manager,
        run_id=run_id,
        execution_identity=execution_identity,
        local_json_root=local_json_root,
        vector_store=vector_store,
        memory_runtime=memory_runtime,
        memory_recall=memory_recall,
        memory_policy=memory_policy,
        qdrant_vector_store=qdrant_vector_store,
        qdrant_document_store=qdrant_document_store,
        approval_store=approval_store,
        task_queue=task_queue,
        notification_options=notification_options,
        web_search_provider=web_search_provider,
        include_network_tools=include_network_tools or include_dangerous_tools,
        include_dangerous_tools=True,
    )
    register_business_tools(
        registry,
        artifact_manager=artifact_manager,
        run_id=run_id,
        execution_identity=execution_identity,
        source_registry=source_registry,
        source_fetch_text=source_fetch_text,
        source_fetch_policy=source_fetch_policy,
        source_tool_runtime=source_tool_runtime,
        source_rate_limiter=source_rate_limiter,
        allowed_source_domains=allowed_source_domains,
        source_health_manager=source_health_manager,
        arxiv_connector=arxiv_connector,
        github_connector=github_connector,
        web_search_provider=web_search_provider,
        report_service=report_service,
        persistence_repository=persistence_repository,
        postgres_repository=postgres_repository,
        postgres_source_health_repository=postgres_source_health_repository,
        memory_ingestion_service=memory_ingestion_service,
        include_network_tools=include_network_tools or include_dangerous_tools,
    )
    if include_dangerous_tools:
        return registry
    return _filter_business_registry(registry, dangerous_only=False)


def build_business_safe_tool_registry(**kwargs: Any) -> ToolRegistry:
    options = dict(kwargs)
    options["include_network_tools"] = False
    options["include_dangerous_tools"] = False
    return build_business_tool_registry(**options)


def build_business_dangerous_tool_registry(**kwargs: Any) -> ToolRegistry:
    options = dict(kwargs)
    include_network_tools = bool(options.pop("include_network_tools", True))
    options["include_network_tools"] = include_network_tools
    options["include_dangerous_tools"] = True
    registry = build_builtin_dangerous_tool_registry(
        artifact_manager=options.get("artifact_manager"),
        run_id=options.get("run_id"),
        execution_identity=options.get("execution_identity"),
        local_json_root=options.get("local_json_root"),
        vector_store=options.get("vector_store"),
        memory_runtime=options.get("memory_runtime"),
        memory_recall=options.get("memory_recall"),
        memory_policy=options.get("memory_policy"),
        qdrant_vector_store=options.get("qdrant_vector_store"),
        qdrant_document_store=options.get("qdrant_document_store"),
        approval_store=options.get("approval_store"),
        task_queue=options.get("task_queue"),
        notification_options=options.get("notification_options"),
        web_search_provider=options.get("web_search_provider"),
        include_network_tools=include_network_tools,
    )
    register_business_tools(
        registry,
        artifact_manager=options.get("artifact_manager"),
        run_id=options.get("run_id"),
        execution_identity=options.get("execution_identity"),
        source_registry=options.get("source_registry"),
        source_fetch_text=options.get("source_fetch_text"),
        source_fetch_policy=options.get("source_fetch_policy"),
        source_tool_runtime=options.get("source_tool_runtime"),
        source_rate_limiter=options.get("source_rate_limiter"),
        allowed_source_domains=options.get("allowed_source_domains"),
        source_health_manager=options.get("source_health_manager"),
        arxiv_connector=options.get("arxiv_connector"),
        github_connector=options.get("github_connector"),
        web_search_provider=options.get("web_search_provider"),
        report_service=options.get("report_service"),
        persistence_repository=options.get("persistence_repository"),
        postgres_repository=options.get("postgres_repository"),
        postgres_source_health_repository=options.get("postgres_source_health_repository"),
        memory_ingestion_service=options.get("memory_ingestion_service"),
        include_network_tools=include_network_tools,
    )
    return _filter_business_registry(registry, dangerous_only=True)


def register_business_tools(
    registry: ToolRegistry,
    *,
    artifact_manager: ArtifactManager | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    source_registry: Any | None = None,
    source_fetch_text: FetchText | None = None,
    source_fetch_policy: Any | None = None,
    source_tool_runtime: Any | None = None,
    source_rate_limiter: Any | None = None,
    allowed_source_domains: list[str] | None = None,
    source_health_manager: Any | None = None,
    arxiv_connector: Any | None = None,
    github_connector: Any | None = None,
    web_search_provider: WebSearchProvider | None = None,
    report_service: Any | None = None,
    persistence_repository: Any | None = None,
    postgres_repository: Any | None = None,
    postgres_source_health_repository: Any | None = None,
    memory_ingestion_service: Any | None = None,
    include_network_tools: bool = False,
) -> None:
    register_source_tools(
        registry,
        fetch_text=source_fetch_text,
        fetch_policy=source_fetch_policy,
        source_runtime=source_tool_runtime,
        rate_limiter=source_rate_limiter,
        allowed_domains=allowed_source_domains,
        health_manager=source_health_manager,
        source_registry=source_registry,
    )
    register_report_tools(
        registry,
        artifact_manager=artifact_manager,
        run_id=run_id,
        execution_identity=execution_identity,
        persistence_repository=persistence_repository,
        report_service=report_service,
    )
    register_quality_tools(registry)
    # Memory writes require Harness candidates and terminal-side-effect policy.
    if include_network_tools:
        register_arxiv_tools(registry, connector=arxiv_connector or default_arxiv_connector())
        register_github_tools(registry, connector=github_connector or default_github_connector())
    if postgres_repository is not None:
        register_postgres_tools(
            registry,
            repository=postgres_repository,
            source_health_repository=postgres_source_health_repository,
        )


def _filter_business_registry(registry: ToolRegistry, *, dangerous_only: bool) -> ToolRegistry:
    filtered = ToolRegistry()
    for registered in registry.list_registered_tools():
        is_dangerous = _is_dangerous_business_tool(registered.definition)
        if dangerous_only != is_dangerous:
            continue
        filtered.register(
            registered.definition,
            registered.executor,
            graph_identity=registered.graph_identity,
        )
    return filtered


def _is_dangerous_business_tool(definition) -> bool:
    if definition.is_dangerous or definition.requires_approval:
        return True
    if definition.side_effect not in {"", "none", "read_only"}:
        return True
    if definition.name in {
        "artifact.write",
        "control.delegate_to_subagent",
        "control.escalate",
        "control.request_human_review",
        "local_json.save",
        "qdrant.upsert",
        "report.export",
        "report.publish",
        "source.fetch_official_blog",
        "source.fetch_url",
        "source.probe",
    }:
        return True
    if any(
        definition.name.startswith(prefix)
        for prefix in ("arxiv.", "github.", "notification.", "postgres.", "web.")
    ):
        return True
    return any(key.startswith("writes_") for key in definition.metadata)
