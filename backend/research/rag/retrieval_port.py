from __future__ import annotations

from framework.harness.retrieval.evidence_pack import EvidencePackCollection
from framework.harness.retrieval.ports import RetrievalPort
from framework.harness.retrieval.request import RetrievalRequest
from framework.harness.rag.kernel_evidence_adapter import KernelRAGRetrieverHarnessAdapter
from framework.rag.core import RAGEvidence, RAGQuery

from backend.research.document.models import PaperChunk
from backend.research.rag.adapters import PaperChunkAdapter
from backend.research.rag.evidence_typing import build_research_evidence_type_resolver
from backend.research.rag.retrieval.contracts import RetrievalRequest as ResearchRetrievalRequest
from backend.research.rag.retrieval.paper_retriever import ResearchRetriever


def _extract_paper_id(request: RetrievalRequest) -> str:
    """Extract paper_id from context_refs (arxiv://id/...) or fall back to scope."""
    explicit = str(request.metadata.get("paper_id") or "").strip()
    if explicit:
        return explicit
    for ref in request.context_refs:
        for prefix in ("arxiv://", "paper://"):
            if ref.startswith(prefix):
                part = ref.removeprefix(prefix).split("/")[0]
                if part:
                    return part
    return request.scope


class PaperChunkRetrievalPort:
    """
    Adapter: framework RetrievalPort → ResearchRetriever.

    Convention:
      RetrievalRequest.scope     = paper_id
      RetrievalRequest.metadata  may include current_section_index (int)

    The reader's position is resolved with this precedence:
      1. request.metadata["current_section_index"]  (per-query override)
      2. default_section_index                       (per-session, from ReadingSession)
      3. 0                                           (reader hasn't picked a section)
    """

    def __init__(self, retriever: ResearchRetriever, *, default_section_index: int = 0) -> None:
        self._kernel_retriever = PaperKernelRAGRetriever(
            retriever,
            default_section_index=default_section_index,
        )
        self._harness_adapter = KernelRAGRetrieverHarnessAdapter(
            self._kernel_retriever,
            default_intent="paper_query",
            default_evidence_type="paper_chunk",
            evidence_type_resolver=build_research_evidence_type_resolver(),
        )
        self._retriever = retriever
        self._default_section_index = max(0, default_section_index)

    def retrieve(self, request: RetrievalRequest) -> EvidencePackCollection:
        collection = self._harness_adapter.retrieve(_request_with_paper_metadata(
            request,
            paper_id=_extract_paper_id(request),
            section_index=self._resolve_section_index(request),
        ))
        return EvidencePackCollection(
            packs=collection.packs,
            request_ref=collection.request_ref,
            metadata={
                **collection.metadata,
                **dict(self._kernel_retriever.last_metadata),
            },
        )

    def _resolve_section_index(self, request: RetrievalRequest) -> int:
        raw = request.metadata.get("current_section_index")
        if raw is None:
            return self._default_section_index
        try:
            index = int(raw)
        except (TypeError, ValueError):
            return self._default_section_index
        return index if index >= 0 else self._default_section_index


class PaperKernelRAGRetriever:
    """Research-owned adapter from generic RAG queries to Paper RAG evidence."""

    def __init__(self, retriever: ResearchRetriever, *, default_section_index: int = 0) -> None:
        self._retriever = retriever
        self._default_section_index = max(0, default_section_index)
        self._chunk_adapter = PaperChunkAdapter()
        self.last_metadata: dict[str, object] = {}

    def retrieve(self, query: RAGQuery) -> tuple[RAGEvidence, ...]:
        paper_id = _paper_id_from_rag_query(query)
        if not paper_id:
            self.last_metadata = {"error": "no paper_id in scope"}
            return ()
        section_index = _section_index_from_rag_query(query, default=self._default_section_index)
        result = self._retriever.retrieve(ResearchRetrievalRequest(
            paper_id=paper_id,
            question=query.query,
            current_section_index=section_index,
            limit=query.limit,
            filters=_research_filters_from_rag_query(query),
            metadata=dict(query.metadata),
        ))
        chunks = _dedupe_chunks((*result.child_chunks, *result.ref_chunks, *result.parent_chunks))
        self.last_metadata = {
            "intent": result.intent,
            "recall_routes": tuple(result.metadata.get("recall_routes") or ()),
            "route_plan": dict(result.metadata.get("route_plan") or {}),
            "child_count": len(result.child_chunks),
            "ref_count": len(result.ref_chunks),
            "section_index": section_index,
        }
        return tuple(self._chunk_adapter.to_rag_evidence(chunk) for chunk in chunks)


def _dedupe_chunks(chunks: tuple[PaperChunk, ...]) -> tuple[PaperChunk, ...]:
    seen: set[str] = set()
    result: list[PaperChunk] = []
    for chunk in chunks:
        if chunk.chunk_id in seen:
            continue
        seen.add(chunk.chunk_id)
        result.append(chunk)
    return tuple(result)


def _request_with_paper_metadata(
    request: RetrievalRequest,
    *,
    paper_id: str,
    section_index: int,
) -> RetrievalRequest:
    return RetrievalRequest(
        query=request.query,
        scope=request.scope,
        filters=request.filters,
        limit=request.limit,
        context_refs=request.context_refs,
        metadata={
            **dict(request.metadata),
            "paper_id": paper_id,
            "current_section_index": section_index,
        },
    )


def _paper_id_from_rag_query(query: RAGQuery) -> str:
    raw = query.metadata.get("paper_id") or query.filters.get("paper_id")
    if raw:
        return str(raw)
    for ref in query.filters.get("context_refs", ()):
        text = str(ref)
        if text.startswith("arxiv://"):
            part = text.removeprefix("arxiv://").split("/")[0]
            if part:
                return part
    scope = str(query.metadata.get("scope") or "")
    return scope if scope and scope != "default" else ""


def _section_index_from_rag_query(query: RAGQuery, *, default: int) -> int:
    raw = query.metadata.get("current_section_index")
    if raw is None:
        return default
    try:
        index = int(raw)
    except (TypeError, ValueError):
        return default
    return index if index >= 0 else default


def _research_filters_from_rag_query(query: RAGQuery) -> dict[str, object]:
    filters = dict(query.filters)
    filters.pop("context_refs", None)
    return filters


# Verify structural compliance at import time
assert isinstance(PaperChunkRetrievalPort(None), RetrievalPort)  # type: ignore[arg-type]

__all__ = ["PaperChunkRetrievalPort", "PaperKernelRAGRetriever"]
