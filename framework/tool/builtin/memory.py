from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.memory.policy import MemoryPolicy
from framework.shared.graph_identity import GraphExecutionIdentity
from framework.tool.models.definition import ToolDefinition
from framework.tool.registry.registry import ToolRegistry


DEFAULT_MEMORY_COLLECTION = "memories"


def register_memory_tools(
    registry: ToolRegistry,
    *,
    vector_store: Any | None = None,
    default_collection: str = DEFAULT_MEMORY_COLLECTION,
    memory_runtime: Any | None = None,
    memory_recall: ExecutionMemoryRecallPort | None = None,
    memory_policy: MemoryPolicy | None = None,
    execution_identity: GraphExecutionIdentity | None = None,
    standalone: bool = False,
) -> None:
    if not isinstance(standalone, bool):
        raise TypeError("standalone must be boolean")
    if execution_identity is not None and not isinstance(
        execution_identity, GraphExecutionIdentity
    ):
        raise TypeError("execution_identity must be GraphExecutionIdentity")
    if execution_identity is None and not standalone:
        raise ValueError(
            "memory tools require an exact GraphExecutionIdentity; "
            "use standalone=True for an explicitly isolated read-only registry"
        )
    if standalone and execution_identity is not None:
        raise ValueError("standalone memory tools cannot carry Graph identity")
    if memory_recall is not None:
        if standalone or not isinstance(memory_recall, ExecutionMemoryRecallPort):
            raise TypeError("Graph memory tools require an execution-bound recall port")
        if memory_runtime is not None or vector_store is not None:
            raise ValueError("execution-bound memory cannot have a mutable fallback")
        if default_collection != DEFAULT_MEMORY_COLLECTION:
            raise ValueError("collection cannot select namespace authority")
        memory_recall.validate_execution(execution_identity)
    elif execution_identity is not None:
        raise ValueError("Graph memory tools require an execution-bound recall port")
    runtime = memory_recall or memory_runtime or _runtime_from_vector_store(
        vector_store,
        default_collection=default_collection,
    )
    if runtime is None:
        raise ValueError("memory_runtime or vector_store is required")
    registry.register(
        ToolDefinition(
            name="memory.recall",
            description="Recall relevant memory records and assemble a compact context block.",
            input_schema={
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "scopes": {"type": "array", "items": {"type": "string"}},
                    "kinds": {"type": "array", "items": {"type": "string"}},
                    "filters": {"type": "object"},
                    "collection": {"type": "string"},
                    "limit": {"type": "integer"},
                    "min_score": {"type": "number"},
                    "score_threshold": {"type": "number"},
                    "max_context_tokens": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            side_effect="read_only",
            concurrency_safe=True,
            max_result_bytes=500_000,
        ),
        lambda args: _recall_memory(
            args,
            runtime=runtime,
            default_collection=default_collection,
            execution_identity=execution_identity,
            memory_policy=memory_policy,
        ),
        graph_identity=execution_identity,
    )
    registry.register(
        ToolDefinition(
            name="memory.explain",
            description="Describe the configured framework memory runtime and policy state.",
            input_schema={"properties": {}, "additionalProperties": False},
            side_effect="read_only",
            concurrency_safe=True,
            max_result_bytes=100_000,
        ),
        lambda args: _explain_memory_runtime(args, runtime=runtime),
        graph_identity=execution_identity,
    )


def _recall_memory(
    args: dict[str, Any],
    *,
    runtime: Any,
    default_collection: str,
    execution_identity: GraphExecutionIdentity | None,
    memory_policy: MemoryPolicy | None = None,
) -> dict[str, Any]:
    query_text = str(args["query"]).strip()
    if not query_text:
        raise ValueError("query is required")
    collection = str(args.get("collection") or default_collection).strip()
    if not collection:
        raise ValueError("collection is required")
    filters = args.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValueError("filters must be an object")
    recall_filters = dict(filters)
    recall_filters.setdefault("collection", collection)
    if execution_identity is not None:
        for key, value in execution_identity.to_dict().items():
            supplied = recall_filters.get(key)
            if supplied is not None and supplied != value:
                raise ValueError(
                    f"memory recall filter {key} conflicts with Graph identity"
                )
            if not isinstance(runtime, ExecutionMemoryRecallPort):
                recall_filters[key] = value
        if isinstance(runtime, ExecutionMemoryRecallPort):
            runtime.validate_execution(execution_identity)
            if collection != DEFAULT_MEMORY_COLLECTION:
                raise ValueError("collection cannot select namespace authority")
    limit = _limit(args.get("limit"))
    min_score = args.get("min_score", args.get("score_threshold"))
    recall = runtime.recall(
        {
            "query": query_text,
            "scopes": [str(value) for value in args.get("scopes") or []],
            "kinds": [str(value) for value in args.get("kinds") or []],
            "filters": recall_filters,
            "limit": limit,
            "min_score": float(min_score) if min_score is not None else None,
            "max_context_tokens": _optional_int(args.get("max_context_tokens")),
        },
        **({"policy": memory_policy} if memory_policy is not None else {}),
    )
    payload = _to_dict(recall)
    payload["collection"] = collection
    return payload


def _explain_memory_runtime(args: dict[str, Any], *, runtime: Any) -> dict[str, Any]:
    if args:
        raise ValueError("memory.explain does not accept arguments")
    return {
        "runtime_type": type(runtime).__name__,
        "store_type": type(getattr(runtime, "store", runtime)).__name__,
        "policy_type": type(getattr(runtime, "policy", None)).__name__ if getattr(runtime, "policy", None) is not None else None,
        "operations": {
            "recall": callable(getattr(runtime, "recall", None)),
            "get": callable(getattr(runtime, "get", None)),
        },
        "tools": {
            "memory.recall": "available",
            "memory.explain": "available",
        },
    }


def _runtime_from_vector_store(vector_store: Any | None, *, default_collection: str) -> Any | None:
    if vector_store is None:
        return None
    return _VectorMemoryRuntime(vector_store, default_collection=default_collection)


@dataclass(frozen=True)
class _VectorSearchQuery:
    collection: str
    text: str
    vector: list[float] | None = None
    filters: dict[str, Any] = field(default_factory=dict)
    limit: int = 10
    offset: int = 0
    score_threshold: float | None = None


@dataclass(frozen=True)
class _MemoryContextBlock:
    content: str
    token_estimate: int
    memory_ids: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls) -> "_MemoryContextBlock":
        return cls(content="", token_estimate=0, memory_ids=[])

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "token_estimate": self.token_estimate,
            "memory_ids": list(self.memory_ids),
        }


@dataclass(frozen=True)
class _VectorRecallResult:
    query: dict[str, Any]
    results: list[Any]
    context_block: _MemoryContextBlock
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def result_count(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.get("query") or "",
            "scopes": list(self.query.get("scopes") or []),
            "kinds": list(self.query.get("kinds") or []),
            "filters": dict(self.query.get("filters") or {}),
            "limit": int(self.query.get("limit") or 10),
            "result_count": self.result_count,
            "results": [_to_dict(result) for result in self.results],
            "context_block": self.context_block.to_dict(),
            "diagnostics": dict(self.diagnostics),
        }


class _VectorMemoryRuntime:
    def __init__(self, vector_store: Any, *, default_collection: str) -> None:
        self.store = vector_store
        self.default_collection = default_collection

    def recall(self, query: dict[str, Any] | str) -> _VectorRecallResult:
        if isinstance(query, str):
            query = {"query": query}
        filters = dict(query.get("filters") or {})
        collection = str(filters.pop("collection", self.default_collection))
        search_query = _VectorSearchQuery(
            collection=collection,
            text=str(query.get("query") or ""),
            filters=filters,
            limit=_limit(query.get("limit")),
            score_threshold=query.get("min_score"),
        )
        results = self.store.search(search_query)
        memory_ids = [
            str(getattr(result, "document_id", _to_dict(result).get("document_id", "")))
            for result in results
        ]
        context = "\n".join(str(_to_dict(result).get("text") or "") for result in results)
        return _VectorRecallResult(
            query={**dict(query), "filters": {"collection": collection, **filters}},
            results=list(results),
            context_block=_MemoryContextBlock(
                content=context,
                token_estimate=len(context.split()),
                memory_ids=[memory_id for memory_id in memory_ids if memory_id],
            ),
        )

def _limit(value: Any) -> int:
    if value is None:
        return 5
    return max(1, min(int(value), 100))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    return dict(getattr(value, "__dict__", {}) or {})
