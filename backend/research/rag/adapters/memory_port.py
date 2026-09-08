from __future__ import annotations

from dataclasses import replace
from typing import Any, Sequence
from urllib.parse import quote

from framework.harness.control_plane.errors import HarnessValidationError
from framework.harness.memory.ports import MemoryWriteCandidate, MemoryWriteStatus
from framework.memory import MemoryKind, MemoryQuery, MemoryScope
from framework.memory.namespace import namespace_revision
from framework.memory.recall_port import ExecutionMemoryRecallPort
from framework.shared.graph_identity import GraphExecutionIdentity


DEFAULT_RAG_MEMORY_KINDS: tuple[MemoryKind, ...] = (MemoryKind.EPISODIC,)
DEFAULT_RAG_MEMORY_SCOPES: tuple[MemoryScope, ...] = (
    MemoryScope.SESSION,
    MemoryScope.GRAPH,
)


class ResearchRAGMemoryPort:
    """Expose only the caller's admitted immutable memory to RAG."""

    def __init__(
        self,
        memory_recall: ExecutionMemoryRecallPort,
        *,
        kinds: Sequence[MemoryKind] = DEFAULT_RAG_MEMORY_KINDS,
        scopes: Sequence[MemoryScope] = DEFAULT_RAG_MEMORY_SCOPES,
        min_score: float | None = None,
    ) -> None:
        if not isinstance(memory_recall, ExecutionMemoryRecallPort):
            raise TypeError("RAG memory requires execution-bound recall")
        self._memory_recall = memory_recall
        self._kinds = tuple(kinds)
        self._scopes = tuple(scopes)
        self._min_score = min_score

    @property
    def execution_identity(self) -> GraphExecutionIdentity:
        return self._memory_recall.execution_identity

    def validate_execution(self, execution_identity: GraphExecutionIdentity) -> None:
        self._memory_recall.validate_execution(execution_identity)

    def recall(self, request: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        if request.get("execution_identity") != self.execution_identity.to_dict():
            raise HarnessValidationError("RAG memory caller differs from admitted execution", code="REF_SNAPSHOT_BINDING_MISMATCH")
        self.validate_execution(self.execution_identity)
        if set(request) - {"query", "namespace", "limit", "goal", "tenant_id", "owner_id", "execution_identity"}:
            raise HarnessValidationError("RAG memory request contains unsupported selectors", code="REF_UNAUTHORIZED")
        query = str(request.get("query") or "").strip()
        namespace = str(request.get("namespace") or "").strip() or None
        limit = max(1, int(request.get("limit") or 5))
        result = self._memory_recall.recall(
            MemoryQuery(
                query=query,
                namespace=namespace,
                tenant_id=request.get("tenant_id"),
                filters={"owner_id": request["owner_id"]} if request.get("owner_id") is not None else {},
                limit=limit,
                kinds=list(self._kinds),
                scopes=list(self._scopes),
                min_score=self._min_score,
            )
        )
        lineage = result.diagnostics
        if (
            lineage.get("execution_identity") != self.execution_identity.to_dict()
            or not lineage.get("input_snapshot_ref")
        ):
            raise HarnessValidationError("RAG memory result lacks admitted lineage", code="REF_SNAPSHOT_BINDING_MISMATCH")
        hits: list[dict[str, Any]] = []
        for item in result.results[:limit]:
            record = item.record
            hit_namespace = record.namespace or namespace
            if namespace is not None and hit_namespace != namespace:
                continue
            namespace_ref = lineage.get("record_namespace_refs", {}).get(record.memory_id)
            checksum = lineage.get("namespace_checksums", {}).get(namespace_ref)
            if not namespace_ref or not checksum or namespace_ref not in lineage.get("namespace_refs", ()):
                raise HarnessValidationError("RAG memory record lacks revision lineage", code="REF_UNRESOLVED")
            if checksum != f"sha256:{namespace_revision(namespace_ref)}":
                raise HarnessValidationError("RAG memory revision checksum conflicts with its reference", code="REF_CHECKSUM_MISMATCH")
            hits.append({
                "memory_id": record.memory_id,
                "memory_ref": f"{namespace_ref}#record={quote(record.memory_id, safe='')}",
                "namespace_ref": namespace_ref,
                "namespace_checksum": checksum,
                "input_snapshot_ref": lineage["input_snapshot_ref"],
                "execution_identity": self.execution_identity.to_dict(),
                "namespace": hit_namespace,
                "kind": record.kind.value,
                "scope": record.scope.value,
                "summary": record.summary,
                "content": record.content,
                "text": record.content,
                "relevance": float(item.score),
                "score": float(item.score),
                "source": item.source,
                "refs": dict(record.refs) if isinstance(record.refs, dict) else {},
                "metadata": dict(record.metadata),
                "match_reasons": list(item.match_reasons),
            })
        return tuple(hits)

    def propose_write(self, candidate: MemoryWriteCandidate) -> MemoryWriteCandidate:
        return replace(candidate, status=MemoryWriteStatus.PROPOSED)


__all__ = [
    "DEFAULT_RAG_MEMORY_KINDS",
    "DEFAULT_RAG_MEMORY_SCOPES",
    "ResearchRAGMemoryPort",
]
