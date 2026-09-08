from __future__ import annotations

from pathlib import Path
from typing import Any

from framework.tool.builtin import (
    register_artifact_tools,
    register_control_tools,
    register_memory_tools,
)
from framework.tool.models import ToolDefinition, ToolPolicy
from framework.tool.registry import ToolCatalog, ToolCatalogNamespace, ToolRegistry
from framework.tool.registry.catalog import build_tool_catalog
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.memory.policy import MemoryPolicy
from infrastructure.tools.local_json_tools import register_local_json_tools
from infrastructure.tools.notification_tools import register_notification_tools
from infrastructure.tools.qdrant_tools import register_qdrant_tools
from infrastructure.tools.web_search_tools import WebSearchProvider, register_web_search_tools


_SAFE_SIDE_EFFECTS = {"", "none", "read_only"}
_DANGEROUS_TOOL_NAMES = {
    "artifact.write",
    "control.delegate_to_subagent",
    "control.escalate",
    "control.request_human_review",
    "local_json.save",
    "qdrant.upsert",
}
_DANGEROUS_TOOL_PREFIXES = (
    "arxiv.",
    "github.",
    "notification.",
    "postgres.",
    "web.",
)


def build_builtin_tool_registry(
    *,
    artifact_manager: Any | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    local_json_root: str | Path | None = None,
    web_search_provider: WebSearchProvider | None = None,
    vector_store: Any | None = None,
    memory_runtime: Any | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    qdrant_vector_store: Any | None = None,
    qdrant_document_store: Any | None = None,
    approval_store: Any | None = None,
    task_queue: Any | None = None,
    notification_options: dict[str, Any] | None = None,
    include_network_tools: bool = False,
    include_dangerous_tools: bool = False,
) -> ToolRegistry:
    registry = _build_unfiltered_builtin_tool_registry(
        artifact_manager=artifact_manager,
        run_id=run_id,
        execution_identity=execution_identity,
        local_json_root=local_json_root,
        web_search_provider=web_search_provider,
        vector_store=vector_store,
        memory_runtime=memory_runtime,
        memory_recall=memory_recall,
        memory_policy=memory_policy,
        qdrant_vector_store=qdrant_vector_store,
        qdrant_document_store=qdrant_document_store,
        approval_store=approval_store,
        task_queue=task_queue,
        notification_options=notification_options,
        include_network_tools=include_network_tools or include_dangerous_tools,
    )
    if include_dangerous_tools:
        return registry
    return _filtered_builtin_registry(registry, dangerous_only=False)


def build_builtin_safe_tool_registry(**kwargs: Any) -> ToolRegistry:
    options = dict(kwargs)
    options["include_network_tools"] = False
    options["include_dangerous_tools"] = False
    return build_builtin_tool_registry(**options)


def build_builtin_dangerous_tool_registry(**kwargs: Any) -> ToolRegistry:
    options = dict(kwargs)
    include_network_tools = bool(options.pop("include_network_tools", True))
    options.pop("include_dangerous_tools", None)
    registry = _build_unfiltered_builtin_tool_registry(
        **options,
        include_network_tools=include_network_tools,
    )
    return _filtered_builtin_registry(registry, dangerous_only=True)


def build_builtin_safe_registry(**kwargs: Any) -> ToolRegistry:
    return build_builtin_safe_tool_registry(**kwargs)


def build_builtin_dangerous_registry(**kwargs: Any) -> ToolRegistry:
    return build_builtin_dangerous_tool_registry(**kwargs)


def _build_unfiltered_builtin_tool_registry(
    *,
    artifact_manager: Any | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    local_json_root: str | Path | None = None,
    web_search_provider: WebSearchProvider | None = None,
    vector_store: Any | None = None,
    memory_runtime: Any | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    qdrant_vector_store: Any | None = None,
    qdrant_document_store: Any | None = None,
    approval_store: Any | None = None,
    task_queue: Any | None = None,
    notification_options: dict[str, Any] | None = None,
    include_network_tools: bool = True,
) -> ToolRegistry:
    registry = ToolRegistry()
    register_control_tools(
        registry,
        approval_store=approval_store,
        task_queue=task_queue,
        execution_identity=execution_identity,
        run_id=run_id,
    )
    if include_network_tools:
        register_web_search_tools(registry, provider=web_search_provider)
    if artifact_manager is not None and execution_identity is not None:
        register_artifact_tools(
            registry,
            artifact_manager=artifact_manager,
            execution_identity=execution_identity,
        )
    if local_json_root is not None:
        register_local_json_tools(registry, root=local_json_root)
    if vector_store is not None or memory_runtime is not None or memory_recall is not None:
        register_memory_tools(
            registry,
            vector_store=vector_store,
            memory_runtime=memory_runtime,
            memory_recall=memory_recall,
            memory_policy=memory_policy,
            execution_identity=execution_identity,
        )
    if qdrant_vector_store is not None:
        register_qdrant_tools(
            registry,
            vector_store=qdrant_vector_store,
            document_store=qdrant_document_store,
        )
    if notification_options is not None:
        register_notification_tools(registry, **dict(notification_options))
    return registry


def _filtered_builtin_registry(registry: ToolRegistry, *, dangerous_only: bool) -> ToolRegistry:
    filtered = ToolRegistry()
    for registered in registry.list_registered_tools():
        is_dangerous = _is_dangerous_builtin_tool(registered.definition)
        if dangerous_only != is_dangerous:
            continue
        filtered.register(
            registered.definition,
            registered.executor,
            graph_identity=registered.graph_identity,
        )
    return filtered


def _is_dangerous_builtin_tool(definition: ToolDefinition) -> bool:
    if definition.is_dangerous or definition.requires_approval:
        return True
    if definition.side_effect_value not in _SAFE_SIDE_EFFECTS:
        return True
    if definition.name in _DANGEROUS_TOOL_NAMES:
        return True
    if any(definition.name.startswith(prefix) for prefix in _DANGEROUS_TOOL_PREFIXES):
        return True
    return any(str(key).startswith("writes_") for key in definition.metadata)


__all__ = [
    "ToolCatalog",
    "ToolCatalogNamespace",
    "ToolPolicy",
    "build_builtin_dangerous_registry",
    "build_builtin_dangerous_tool_registry",
    "build_builtin_safe_registry",
    "build_builtin_safe_tool_registry",
    "build_builtin_tool_registry",
    "build_tool_catalog",
]
