from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.tool.builtin.artifact import register_artifact_tools
from framework.tool.builtin.control import register_control_tools
from framework.tool.builtin.memory import register_memory_tools
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.memory.policy import MemoryPolicy
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool.models import ToolDefinition, ToolPolicy
from framework.tool.registry.registry import ToolRegistry


_SAFE_SIDE_EFFECTS = {"", "none", "read_only"}
_DANGEROUS_TOOL_NAMES = {
    "artifact.write",
    "control.delegate_to_subagent",
    "control.escalate",
    "control.request_human_review",
}
_DANGEROUS_TOOL_PREFIXES = ("mcp.",)


@dataclass(frozen=True)
class ToolCatalogEntry:
    definition: ToolDefinition
    namespace: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace or self.definition.namespace,
            "definition": self.definition.to_dict(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class ToolCatalogNamespace:
    namespace: str
    tool_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"namespace": self.namespace, "tool_count": self.tool_count}


@dataclass
class ToolCatalog:
    tools: list[ToolDefinition] = field(default_factory=list)
    namespaces: list[ToolCatalogNamespace] = field(default_factory=list)
    registry_valid: bool = True
    registry_errors: list[str] = field(default_factory=list)
    duplicate_risk_count: int = 0
    duplicate_risk_namespaces: list[str] = field(default_factory=list)
    agent_id: str | None = None

    @property
    def tool_count(self) -> int:
        return len(self.tools)

    @property
    def namespace_count(self) -> int:
        return len(self.namespaces)

    def add(self, entry: ToolCatalogEntry) -> None:
        self.tools.append(entry.definition)
        self.namespaces = _catalog_namespaces(self.tools)

    def list_namespaces(self) -> list[str]:
        return [namespace.namespace for namespace in self.namespaces]

    def list_tools(self, namespace: str | None = None) -> list[ToolDefinition]:
        if namespace is None:
            return list(self.tools)
        return [tool for tool in self.tools if tool.namespace == namespace]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "tool_count": self.tool_count,
            "namespace_count": self.namespace_count,
            "namespaces": [namespace.to_dict() for namespace in self.namespaces],
            "tools": [definition.to_dict() for definition in self.tools],
            "registry_valid": self.registry_valid,
            "registry_errors": list(self.registry_errors),
            "duplicate_risk_count": self.duplicate_risk_count,
            "duplicate_risk_namespaces": list(self.duplicate_risk_namespaces or []),
        }


def build_builtin_tool_registry(
    *,
    artifact_manager: Any | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    memory_runtime: Any | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    vector_store: Any | None = None,
    approval_store: Any | None = None,
    task_queue: Any | None = None,
    include_network_tools: bool = False,
    include_dangerous_tools: bool = False,
    **_: Any,
) -> ToolRegistry:
    registry = _build_unfiltered_builtin_tool_registry(
        artifact_manager=artifact_manager,
        run_id=run_id,
        execution_identity=execution_identity,
        memory_runtime=memory_runtime,
        memory_recall=memory_recall,
        memory_policy=memory_policy,
        vector_store=vector_store,
        approval_store=approval_store,
        task_queue=task_queue,
        include_network_tools=include_network_tools or include_dangerous_tools,
    )
    if include_dangerous_tools:
        return registry
    return _filtered_builtin_registry(registry, dangerous_only=False)


def build_builtin_safe_tool_registry(**kwargs: Any) -> ToolRegistry:
    options: dict[str, Any] = dict(kwargs)
    options["include_network_tools"] = False
    options["include_dangerous_tools"] = False
    return build_builtin_tool_registry(**options)


def build_builtin_dangerous_tool_registry(**kwargs: Any) -> ToolRegistry:
    options = dict(kwargs)
    options.pop("include_dangerous_tools", None)
    registry = _build_unfiltered_builtin_tool_registry(**options)
    return _filtered_builtin_registry(registry, dangerous_only=True)


def build_builtin_safe_registry(**kwargs: Any) -> ToolRegistry:
    return build_builtin_safe_tool_registry(**kwargs)


def build_builtin_dangerous_registry(**kwargs: Any) -> ToolRegistry:
    return build_builtin_dangerous_tool_registry(**kwargs)


def build_tool_catalog(
    registry: ToolRegistry,
    *,
    agent_id: str | None = None,
    policy: ToolPolicy | None = None,
) -> ToolCatalog:
    if policy is None:
        tools = registry.list_tools()
    else:
        tools = registry.list_tools_for_agent(agent_id or "", policy)
    sorted_tools = sorted(tools, key=lambda definition: (definition.namespace, definition.name))
    validation = registry.validate_no_conflicts()
    return ToolCatalog(
        tools=sorted_tools,
        namespaces=_catalog_namespaces(sorted_tools),
        registry_valid=validation.ok,
        registry_errors=list(validation.errors),
        duplicate_risk_count=_duplicate_risk_count(sorted_tools),
        duplicate_risk_namespaces=_duplicate_risk_namespaces(sorted_tools),
        agent_id=agent_id,
    )


def _build_unfiltered_builtin_tool_registry(
    *,
    artifact_manager: Any | None = None,
    run_id: str | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    memory_runtime: Any | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    vector_store: Any | None = None,
    approval_store: Any | None = None,
    task_queue: Any | None = None,
    include_network_tools: bool = False,
    **_: Any,
) -> ToolRegistry:
    registry = ToolRegistry()
    register_control_tools(
        registry,
        approval_store=approval_store,
        task_queue=task_queue,
        execution_identity=execution_identity,
        run_id=run_id,
    )
    if artifact_manager is not None and execution_identity is not None:
        register_artifact_tools(
            registry,
            artifact_manager=artifact_manager,
            execution_identity=execution_identity,
        )
    if vector_store is not None or memory_runtime is not None or memory_recall is not None:
        register_memory_tools(
            registry,
            vector_store=vector_store,
            memory_runtime=memory_runtime,
            memory_recall=memory_recall,
            memory_policy=memory_policy,
            execution_identity=execution_identity,
        )
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


def _catalog_namespaces(tools: list[ToolDefinition]) -> list[ToolCatalogNamespace]:
    counts: dict[str, int] = {}
    for definition in tools:
        counts[definition.namespace] = counts.get(definition.namespace, 0) + 1
    return [
        ToolCatalogNamespace(namespace=namespace, tool_count=counts[namespace])
        for namespace in sorted(counts)
    ]


def _duplicate_risk_count(tools: list[ToolDefinition]) -> int:
    return sum(
        len(definitions)
        for definitions in _definitions_by_leaf_name(tools).values()
        if len(definitions) > 1
    )


def _duplicate_risk_namespaces(tools: list[ToolDefinition]) -> list[str]:
    namespaces = {
        definition.namespace
        for definitions in _definitions_by_leaf_name(tools).values()
        if len(definitions) > 1
        for definition in definitions
    }
    return sorted(namespaces)


def _definitions_by_leaf_name(tools: list[ToolDefinition]) -> dict[str, list[ToolDefinition]]:
    grouped: dict[str, list[ToolDefinition]] = {}
    for definition in tools:
        grouped.setdefault(definition.name.rsplit(".", maxsplit=1)[-1], []).append(definition)
    return grouped
